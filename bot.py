import configparser
import argparse
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta
from html import escape
import re
from urllib.parse import urljoin, urldefrag

import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

CONFIG_FILE = "config.ini"
WINDSPOTS_API = "https://api.windspots.org/mobile"
LOGIN_URL = "https://www2.unil.ch/sportres/nautique/login.php"
BASE_URL = "https://www2.unil.ch/sportres/nautique/"
SPORT_BASE_URL = "https://sport.unil.ch/"
NAVIGATION_LIBRE_URL = "https://sport.unil.ch/?pid=80&aid=92"
NAVIGATION_LIBRE_LOGIN_URL = "https://sport.unil.ch/?pid=29"

REQUIRED_KEYS = {
    "credentials": ["email", "password"],
    "telegram": ["bot_token", "chat_id"],
    "settings": ["poll_interval_seconds", "course_url", "log_file"],
}


def _normalize(s):
    """Strip diacritics and lowercase — so 'Dériveur' matches 'deriveur' or 'Deriveur'."""
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower()


def _parse_filter_list(raw):
    """Split a comma-separated config value into normalized tokens, dropping empties."""
    return [_normalize(v.strip()) for v in raw.split(",") if v.strip()]


def _config_bool(config, section, option, fallback=False):
    if not config.has_option(section, option):
        return fallback
    return config.getboolean(section, option)


def _looks_empty_or_placeholder(value):
    value = (value or "").strip()
    return not value or value.startswith("your_") or value.endswith("_here")


def _config_int(config, section, option, fallback):
    if not config.has_option(section, option):
        return fallback
    return config.getint(section, option)


def _parse_time_minutes(raw_time):
    match = re.search(r"(\d{1,2}):(\d{2})", raw_time or "")
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


_WEEKDAY_ALIASES = {
    "lun": 0, "lundi": 0, "mon": 0, "monday": 0,
    "mar": 1, "mardi": 1, "tue": 1, "tues": 1, "tuesday": 1,
    "mer": 2, "mercredi": 2, "wed": 2, "wednesday": 2,
    "jeu": 3, "jeudi": 3, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "ven": 4, "vendredi": 4, "fri": 4, "friday": 4,
    "sam": 5, "samedi": 5, "sat": 5, "saturday": 5,
    "dim": 6, "dimanche": 6, "sun": 6, "sunday": 6,
}


def _parse_day_token(token, today=None):
    """Resolve a day token to a concrete date.

    Accepts today/tomorrow (en+fr), weekday names (next occurrence, en+fr),
    DD.MM[.YYYY] (rolls to next year if already past) and ISO YYYY-MM-DD.
    """
    today = today or date.today()
    t = _normalize(token.strip())
    if t in ("today", "auj", "aujourdhui"):
        return today
    if t in ("tomorrow", "demain", "tmrw", "tmr"):
        return today + timedelta(days=1)
    if t in _WEEKDAY_ALIASES:
        delta = (_WEEKDAY_ALIASES[t] - today.weekday()) % 7
        return today + timedelta(days=delta)

    match = re.match(r"^(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?$", t)
    if match:
        day, month = int(match.group(1)), int(match.group(2))
        raw_year = match.group(3)
        if raw_year:
            year = int(raw_year)
            if year < 100:
                year += 2000
            try:
                return date(year, month, day)
            except ValueError:
                return None
        try:
            candidate = date(today.year, month, day)
        except ValueError:
            return None
        return candidate if candidate >= today else candidate.replace(year=today.year + 1)

    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", t)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


def _parse_hour_token(token):
    """Resolve an hour token to minutes-since-midnight, or the sentinel "any".

    Returns None if the token is unparseable.
    """
    t = _normalize(token.strip())
    if t in ("any", "all", "*", "toutes", "tout", "toute"):
        return "any"
    minutes = _parse_time_minutes(t)  # matches an embedded HH:MM (e.g. a range start)
    if minutes is not None:
        return minutes
    match = re.match(r"^(\d{1,2})h?(\d{2})?$", t)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        if hour <= 23 and minute <= 59:
            return hour * 60 + minute
    return None


class SailingBot:
    def __init__(self, config_path=CONFIG_FILE):
        self.config = self._load_config(config_path)
        self.email = self.config["credentials"]["email"]
        self.password = self.config["credentials"]["password"]
        self.bot_token = self.config["telegram"]["bot_token"]
        self.chat_id = self.config["telegram"]["chat_id"]
        self.interval = int(self.config["settings"]["poll_interval_seconds"])
        self.course_url = self.config["settings"]["course_url"]
        self.log_file = self.config["settings"]["log_file"]
        self.state_file = self.config.get("settings", "state_file", fallback="state.json")
        self.course_types_raw = [
            v.strip() for v in self.config.get("filters", "course_types", fallback="").split(",") if v.strip()
        ]
        self.instructors_raw = [
            v.strip() for v in self.config.get("filters", "instructors", fallback="").split(",") if v.strip()
        ]
        # Overrides (set via /watchcourse, /watchinstructor …) replace the
        # config lists when non-None, like support_overrides does for boats.
        self.course_type_overrides = None
        self.instructor_overrides = None
        self.filter_courses = _parse_filter_list(self.config.get("filters", "course_types", fallback=""))
        self.filter_instructors = _parse_filter_list(self.config.get("filters", "instructors", fallback=""))
        self.course_days_ahead = _config_int(self.config, "filters", "days_ahead", fallback=21)
        self.navigation_enabled = _config_bool(self.config, "navigation_libre", "enabled", fallback=False)
        self.navigation_login = self.config.get("navigation_libre", "login", fallback="")
        self.navigation_password = self.config.get("navigation_libre", "password", fallback="")
        self.navigation_url = self.config.get(
            "navigation_libre", "activity_url", fallback=NAVIGATION_LIBRE_URL
        )
        self.navigation_login_url = self.config.get(
            "navigation_libre", "login_url", fallback=NAVIGATION_LIBRE_LOGIN_URL
        )
        self.navigation_html_dump_file = self.config.get(
            "navigation_libre", "html_dump_file", fallback="navigation_libre_authenticated.html"
        )
        self.navigation_supports_raw = [
            v.strip()
            for v in self.config.get("navigation_libre_filters", "supports", fallback="").split(",")
            if v.strip()
        ]
        # support_overrides (set via /watch /unwatch) replace the config list when non-None.
        self.support_overrides = None
        self.filter_navigation_supports = _parse_filter_list(
            self.config.get("navigation_libre_filters", "supports", fallback="")
        )
        self.navigation_days_ahead = _config_int(
            self.config, "navigation_libre_filters", "days_ahead", fallback=21
        )
        # Default partner name used to auto-register team boats (2-person supports).
        # May be overridden at runtime via /partner; that override lives in state.json.
        self.navigation_partner = self.config.get("navigation_libre", "partner", fallback="").strip()
        if _looks_empty_or_placeholder(self.navigation_partner):
            self.navigation_partner = ""
        # Pending auto-registration requests (list of dicts, kept sorted by
        # priority then creation time); see _cmd_register.
        self.registration_requests = []
        # The one slot we're currently registered to (see
        # _set_current_registration) or None. sport.unil.ch allows a single
        # active registration at a time, so an upgrade must cancel this first.
        self.current_registration = None
        self.navigation_time_filters = self._load_navigation_time_filters()
        self.log = self._setup_logging()
        self.session = self._build_session()
        self.navigation_session = self._build_session()
        self.form_action = LOGIN_URL
        self.navigation_form_action = self.navigation_login_url
        self.windspots_enabled = _config_bool(self.config, "windspots", "enabled", fallback=False)
        self.windspots_station = self.config.get("windspots", "station", fallback="CHVD05")
        alert_knots = _config_int(self.config, "windspots", "alert_threshold_knots", fallback=7)
        self.wind_alert_threshold_kt = float(alert_knots)
        self.max_alerts_per_hour = _config_int(self.config, "windspots", "max_alerts_per_hour", fallback=2)
        self._wind_alert_times = []
        self._telegram_offset = 0
        self._stop_event = threading.Event()
        self._running = False
        self._prev_slot_keys = None
        self._prev_navigation_slot_keys = None
        self._last_heartbeat = 0.0
        self.paused = False
        self._last_slot_count = 0
        self._last_navigation_slot_count = 0
        self._last_check_time = None
        # Guards the shared sessions and mutable settings touched by both the
        # poll loop and the Telegram command thread.
        self._fetch_lock = threading.RLock()
        self._load_state()

    def _build_session(self):
        """A requests session with automatic retry/backoff on transient failures."""
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; SailingBot/1.0)"})
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _state_path(self):
        if os.path.isabs(self.state_file):
            return self.state_file
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), self.state_file)

    # Chat-editable alert filters: override attr (persisted in state.json,
    # None = use config), config source attr, and recompute hook.
    _FILTER_SPECS = {
        "boat": {
            "plural": "boats",
            "example": "RS aéro",
            "override_attr": "support_overrides",
            "raw_attr": "navigation_supports_raw",
            "apply": "_apply_support_overrides",
        },
        "course type": {
            "plural": "course types",
            "example": "Catamaran",
            "override_attr": "course_type_overrides",
            "raw_attr": "course_types_raw",
            "apply": "_apply_course_filter_overrides",
        },
        "instructor": {
            "plural": "instructors",
            "example": "Marine",
            "override_attr": "instructor_overrides",
            "raw_attr": "instructors_raw",
            "apply": "_apply_course_filter_overrides",
        },
    }

    def _filter_values(self, kind):
        """Effective raw (display) values of one filter list."""
        spec = self._FILTER_SPECS[kind]
        override = getattr(self, spec["override_attr"])
        return list(override) if override is not None else list(getattr(self, spec["raw_attr"]))

    def _apply_support_overrides(self):
        """Recompute the effective normalized support filter from overrides or config."""
        source = self.support_overrides if self.support_overrides is not None else self.navigation_supports_raw
        self.filter_navigation_supports = [_normalize(v) for v in source if v.strip()]

    def _apply_course_filter_overrides(self):
        """Recompute the normalized course-alert filters from overrides or config."""
        self.filter_courses = [_normalize(v) for v in self._filter_values("course type") if v.strip()]
        self.filter_instructors = [_normalize(v) for v in self._filter_values("instructor") if v.strip()]

    def _load_state(self):
        path = self._state_path()
        try:
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
        except FileNotFoundError:
            self._apply_support_overrides()
            self._apply_course_filter_overrides()
            return
        except (json.JSONDecodeError, OSError) as e:
            self.log.warning(f"Could not read state file {path}: {e}; starting fresh.")
            self._apply_support_overrides()
            self._apply_course_filter_overrides()
            return

        course_keys = state.get("course_slot_keys")
        if course_keys is not None:
            self._prev_slot_keys = set(course_keys)
        nav_keys = state.get("nav_slot_keys")
        if nav_keys is not None:
            self._prev_navigation_slot_keys = set(nav_keys)
        self._telegram_offset = state.get("telegram_offset", 0)
        self.paused = bool(state.get("paused", False))
        overrides = state.get("support_overrides")
        self.support_overrides = list(overrides) if overrides is not None else None
        course_overrides = state.get("course_type_overrides")
        self.course_type_overrides = list(course_overrides) if course_overrides is not None else None
        instructor_overrides = state.get("instructor_overrides")
        self.instructor_overrides = list(instructor_overrides) if instructor_overrides is not None else None
        if state.get("wind_threshold_kt") is not None:
            self.wind_alert_threshold_kt = float(state["wind_threshold_kt"])
        self._last_heartbeat = state.get("last_heartbeat", 0.0)
        self.registration_requests = state.get("registration_requests") or []
        for req in self.registration_requests:
            req.setdefault("priority", 5)
        self._sort_requests()
        self.current_registration = state.get("current_registration")
        if state.get("navigation_partner") is not None:
            self.navigation_partner = state["navigation_partner"]
        self._apply_support_overrides()
        self._apply_course_filter_overrides()
        self.log.info(
            f"Loaded state from {path} "
            f"({len(self._prev_slot_keys or [])} course key(s), "
            f"{len(self._prev_navigation_slot_keys or [])} nav key(s), "
            f"paused={self.paused})"
        )

    def _save_state(self):
        path = self._state_path()
        state = {
            "course_slot_keys": sorted(self._prev_slot_keys) if self._prev_slot_keys is not None else None,
            "nav_slot_keys": sorted(self._prev_navigation_slot_keys) if self._prev_navigation_slot_keys is not None else None,
            "telegram_offset": self._telegram_offset,
            "paused": self.paused,
            "support_overrides": self.support_overrides,
            "course_type_overrides": self.course_type_overrides,
            "instructor_overrides": self.instructor_overrides,
            "wind_threshold_kt": self.wind_alert_threshold_kt,
            "last_heartbeat": self._last_heartbeat,
            "registration_requests": self.registration_requests,
            "current_registration": self.current_registration,
            "navigation_partner": self.navigation_partner,
        }
        tmp = f"{path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError as e:
            self.log.error(f"Failed to save state file {path}: {e}")

    def _slot_key(self, slot):
        return slot["href"] if slot["href"] else f"{slot['date']}|{slot['time']}|{slot['course']}"

    def _navigation_slot_key(self, slot):
        # Free-calendar blocks all share the activity URL and their time range
        # shifts as the clock advances, so key them on boat+day+planning (one
        # entry per boat/day) to avoid flapping. Inscription slots have a stable
        # unique href.
        if slot.get("planning_id"):
            return f"{slot['date']}|{slot['support']}|{slot['planning_id']}"
        href = slot.get("href") or ""
        if href and href != self.navigation_url:
            return href
        return f"{slot['date']}|{slot['support']}|{slot['time']}"

    def _parse_slot_date(self, raw_date):
        match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", raw_date or "")
        if not match:
            return None

        day, month, year = [int(part) for part in match.groups()]
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None

    def _within_days_ahead(self, raw_date, days_ahead):
        slot_date = self._parse_slot_date(raw_date)
        if slot_date is None:
            return True
        return date.today() <= slot_date <= date.today() + timedelta(days=days_ahead)

    def _format_short_date(self, raw_date):
        slot_date = self._parse_slot_date(raw_date)
        if slot_date is None:
            return raw_date

        weekdays = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
        return f"{weekdays[slot_date.weekday()]} {slot_date.strftime('%d.%m.%Y')}"

    def _group_slots_by_date(self, slots):
        grouped = []
        index_by_date = {}
        for slot in slots:
            raw_date = slot["date"]
            if raw_date not in index_by_date:
                index_by_date[raw_date] = len(grouped)
                grouped.append((raw_date, []))
            grouped[index_by_date[raw_date]][1].append(slot)
        return grouped

    def _short_navigation_support(self, support):
        parts = [part.strip() for part in support.split("/") if part.strip()]
        drop = {"navigation libre", "centre nautique", "voile"}
        kept = [part for part in parts if _normalize(part) not in drop]
        if len(kept) >= 2:
            return " · ".join(kept[:2])
        if kept:
            return kept[0]
        return support

    def _parse_slot_time_range(self, raw_time):
        parts = re.findall(r"\d{1,2}:\d{2}", raw_time or "")
        if not parts:
            return None, None

        start = _parse_time_minutes(parts[0])
        end = _parse_time_minutes(parts[1]) if len(parts) > 1 else None
        return start, end

    def _load_navigation_time_filters(self):
        section = "navigation_libre_time_filters"
        if not self.config.has_section(section):
            return []

        filters = []
        aliases = {
            "after": "start_after",
            "start_after": "start_after",
            "from": "start_after",
            "before": "start_before",
            "start_before": "start_before",
            "until": "start_before",
            "end_after": "end_after",
            "end_before": "end_before",
        }
        pattern = re.compile(
            r"\b(after|start_after|from|before|start_before|until|end_after|end_before)"
            r"\s*(?:=|:)?\s*(\d{1,2}:\d{2})",
            re.IGNORECASE,
        )

        for support_pattern, raw_rule in self.config.items(section):
            constraints = {}
            for match in pattern.finditer(raw_rule):
                key = aliases[match.group(1).lower()]
                minutes = _parse_time_minutes(match.group(2))
                if minutes is not None:
                    constraints[key] = minutes

            normalized_pattern = _normalize(support_pattern)
            if normalized_pattern and constraints:
                filters.append({
                    "pattern": normalized_pattern,
                    "raw_pattern": support_pattern,
                    "constraints": constraints,
                })

        return filters

    def _passes_navigation_time_filters(self, slot):
        if not self.navigation_time_filters:
            return True

        support = _normalize(slot["support"])
        matching_filters = [
            rule for rule in self.navigation_time_filters
            if rule["pattern"] in support
        ]
        if not matching_filters:
            return True

        rule = max(matching_filters, key=lambda r: len(r["pattern"]))
        start, end = self._parse_slot_time_range(slot["time"])
        if start is None:
            return True

        constraints = rule["constraints"]
        if "start_after" in constraints and start < constraints["start_after"]:
            return False
        if "start_before" in constraints and start > constraints["start_before"]:
            return False
        if end is not None:
            if "end_after" in constraints and end < constraints["end_after"]:
                return False
            if "end_before" in constraints and end > constraints["end_before"]:
                return False

        return True

    def _load_config(self, path):
        config = configparser.ConfigParser()
        if not config.read(path):
            print(
                f"ERROR: '{path}' not found. "
                f"Copy 'config.ini.example' to '{path}' and fill in your credentials.",
                file=sys.stderr,
            )
            sys.exit(1)
        for section, keys in REQUIRED_KEYS.items():
            if not config.has_section(section):
                print(f"ERROR: Missing section [{section}] in {path}", file=sys.stderr)
                sys.exit(1)
            for key in keys:
                if not config.has_option(section, key):
                    print(f"ERROR: Missing key '{key}' under [{section}] in {path}", file=sys.stderr)
                    sys.exit(1)
        return config

    def _setup_logging(self):
        logger = logging.getLogger("SailingBot")
        logger.setLevel(logging.DEBUG)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setLevel(logging.INFO)
        stream_handler.setFormatter(fmt)

        file_handler = logging.handlers.RotatingFileHandler(
            self.log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)

        logger.addHandler(stream_handler)
        logger.addHandler(file_handler)
        return logger

    def _detect_login_fields(self, soup, username, password, login_url, base_url):
        # Find the form that contains a password input
        form = None
        for f in soup.find_all("form"):
            if f.find("input", {"type": "password"}):
                form = f
                break
        if form is None:
            form = soup.find("form")
        if form is None:
            raise RuntimeError("No form found on the login page")

        action = form.get("action", login_url)
        if action and not action.startswith("http"):
            action = urljoin(base_url, action)

        payload = {}
        email_filled = False
        for inp in form.find_all("input"):
            input_type = inp.get("type", "text").lower()
            name = inp.get("name", "").strip()
            if not name:
                continue
            if input_type == "hidden":
                payload[name] = inp.get("value", "")
            elif input_type == "password":
                payload[name] = password
            elif input_type in ("text", "email") and not email_filled:
                payload[name] = username
                email_filled = True

        self.log.debug(f"Detected form action: {action}, fields: {list(payload.keys())}")
        return action or login_url, payload

    def login(self):
        self.log.info("Attempting login...")
        try:
            resp = self.session.get(LOGIN_URL, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            self.log.error(f"Failed to fetch login page: {e}")
            return False

        soup = BeautifulSoup(resp.text, "lxml")
        try:
            self.form_action, payload = self._detect_login_fields(
                soup, self.email, self.password, LOGIN_URL, BASE_URL
            )
        except RuntimeError as e:
            self.log.error(f"Login form detection failed: {e}")
            return False

        if not payload:
            self.log.error("No fields detected in login form — cannot log in")
            return False

        try:
            post_resp = self.session.post(self.form_action, data=payload, timeout=15)
            post_resp.raise_for_status()
        except requests.RequestException as e:
            self.log.error(f"Login POST failed: {e}")
            return False

        # Detect success: redirected away from login page, or login form no longer present
        final_url = post_resp.url
        post_soup = BeautifulSoup(post_resp.text, "lxml")
        still_on_login = bool(post_soup.find("input", {"type": "password"}))

        if still_on_login or "login" in final_url.lower():
            self.log.error("Login failed — still on login page. Check your credentials.")
            return False

        self.log.info(f"Login successful (landed on: {final_url})")
        return True

    def login_navigation_libre(self):
        if _looks_empty_or_placeholder(self.navigation_login) or _looks_empty_or_placeholder(self.navigation_password):
            self.log.warning("Navigation libre credentials are empty/placeholders; skipping login.")
            return False

        self.log.info("Attempting navigation libre login...")
        try:
            resp = self.navigation_session.get(self.navigation_login_url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            self.log.error(f"Failed to fetch navigation libre login page: {e}")
            return False

        soup = BeautifulSoup(resp.text, "lxml")
        try:
            self.navigation_form_action, payload = self._detect_login_fields(
                soup,
                self.navigation_login,
                self.navigation_password,
                self.navigation_login_url,
                SPORT_BASE_URL,
            )
        except RuntimeError as e:
            self.log.error(f"Navigation libre login form detection failed: {e}")
            return False

        if not payload:
            self.log.error("No fields detected in navigation libre login form; cannot log in")
            return False

        try:
            post_resp = self.navigation_session.post(self.navigation_form_action, data=payload, timeout=15)
            post_resp.raise_for_status()
        except requests.RequestException as e:
            self.log.error(f"Navigation libre login POST failed: {e}")
            return False

        post_soup = BeautifulSoup(post_resp.text, "lxml")
        still_on_login = bool(post_soup.find("input", {"type": "password"}))
        if still_on_login:
            self.log.error("Navigation libre login failed; still on login page. Check your credentials.")
            return False

        self.log.info(f"Navigation libre login successful (landed on: {post_resp.url})")
        return True

    def _looks_logged_out(self, soup):
        return bool(soup.find("input", {"type": "password"})) or "Se connecter" in soup.get_text(
            " ", strip=True
        )

    def _fetch_navigation_libre_page(self):
        try:
            resp = self.navigation_session.get(self.navigation_url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            self.log.error(f"Failed to fetch navigation libre page: {e}")
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        if self._looks_logged_out(soup):
            self.log.warning("Navigation libre session expired or not authenticated; re-logging in...")
            if not self.login_navigation_libre():
                raise RuntimeError("Navigation libre re-login failed")
            try:
                resp = self.navigation_session.get(self.navigation_url, timeout=15)
                resp.raise_for_status()
            except requests.RequestException as e:
                self.log.error(f"Failed to fetch navigation libre page after re-login: {e}")
                return None

        return resp

    def inspect_navigation_libre(self):
        if not self.login_navigation_libre():
            return False
        resp = self._fetch_navigation_libre_page()
        if resp is None:
            return False

        with open(self.navigation_html_dump_file, "w", encoding="utf-8") as f:
            f.write(resp.text)

        soup = BeautifulSoup(resp.text, "lxml")
        calendars = soup.select(".calendars")
        free_blocks = soup.select(".calendar.planning div.loc.free:not(.lock)")
        navigation_slots = self._parse_navigation_libre_page(soup)
        self.log.info(
            f"Saved authenticated navigation libre HTML to {self.navigation_html_dump_file} "
            f"({len(navigation_slots)} navigation libre slot(s), "
            f"{len(calendars)} rental calendar container(s), {len(free_blocks)} rental free block(s) detected)"
        )
        return True

    def _fetch_week(self, refdate=None):
        url = self.course_url
        if refdate:
            url += f"?refdate={refdate}"
        try:
            resp = self.session.get(url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            self.log.error(f"Failed to fetch course page ({refdate or 'current'}): {e}")
            return None

        if "login" in resp.url.lower():
            self.log.warning("Session expired — re-logging in...")
            if not self.login():
                raise RuntimeError("Re-login failed")
            try:
                resp = self.session.get(url, timeout=15)
                resp.raise_for_status()
            except requests.RequestException as e:
                self.log.error(f"Failed to fetch course page after re-login: {e}")
                return None

        return resp

    def _parse_week(self, soup):
        available = []
        for item in soup.find_all("div", class_="cours_item"):
            # Skip past courses
            if "cours_item_old" in item.get("class", []):
                continue

            # The enrollment container is the direct-child col-md-3 of the item.
            # (col-md-3 also appears nested inside col-md-5 for the level field,
            #  so we must use recursive=False to avoid matching it.)
            enrollment_container = item.find("div", class_="col-md-3", recursive=False)
            if enrollment_container is None:
                continue

            # Bold enrollment = full (e.g. <strong>6 / 6</strong>)
            if enrollment_container.find("strong"):
                continue

            # The actual count sits in the inner col-md-3 inside the container
            inner_count = enrollment_container.find("div", class_="col-md-3")
            enrollment = inner_count.get_text(strip=True) if inner_count else enrollment_container.get_text(strip=True)

            # Parse X / Y to confirm there are actually free spots
            try:
                parts = enrollment.split("/")
                current = int(parts[0].strip())
                maximum = int(parts[1].strip())
                if current >= maximum:
                    continue
            except (ValueError, IndexError):
                # If we can't parse it and it's not bold, include it anyway
                pass

            # Use recursive=False to get only the direct-child col-md-4 and col-md-5,
            # avoiding accidental matches from nested divs.
            col4 = item.find("div", class_="col-md-4", recursive=False)
            col5 = item.find("div", class_="col-md-5", recursive=False)

            # Prefer the mobile date (always full: "mar 28.04.26") over the desktop
            # short version ("mar 28") which is empty for subsequent courses on same day.
            date_el = col4.select_one(".hidden-lg.hidden-md.col-md-2") if col4 else None
            if not date_el or not date_el.get_text(strip=True):
                date_el = col4.select_one(".hidden-xs.hidden-sm") if col4 else None
            date_str = date_el.get_text(strip=True) if date_el else ""

            # Time: the small col-md-4 div inside col4
            time_el = col4.select_one(".col-md-4.small") if col4 else None
            time_str = time_el.get_text(strip=True) if time_el else ""

            # Course name: the col-md-6 inside col4
            name_el = col4.select_one(".col-md-6") if col4 else None
            course_name = name_el.get_text(strip=True) if name_el else ""

            # Level and instructor from col5
            level_el = col5.select_one(".col-md-3") if col5 else None
            instructor_el = col5.select_one(".col-md-5") if col5 else None
            level = level_el.get_text(strip=True) if level_el else ""
            instructor = instructor_el.get_text(strip=True) if instructor_el else ""

            # Extract link from onclick attribute
            onclick = item.get("onclick", "")
            href = ""
            if "window.location='" in onclick:
                href = "https://www2.unil.ch" + onclick.split("'")[1]

            available.append({
                "date": date_str,
                "course": course_name,
                "time": time_str,
                "level": level,
                "instructor": instructor,
                "enrollment": enrollment,
                "href": href,
            })

        return available

    def check_availability(self):
        monday = date.today() - timedelta(days=date.today().weekday())
        end_date = date.today() + timedelta(days=self.course_days_ahead)
        weeks_to_fetch = ((end_date - monday).days // 7) + 1
        weeks = [None] + [
            (monday + timedelta(weeks=i)).strftime("%Y%m%d") for i in range(1, weeks_to_fetch)
        ]

        available = []
        for refdate in weeks:
            resp = self._fetch_week(refdate)
            if resp is None:
                # A failed fetch must not look like "everything disappeared".
                # Abort this pass so the caller keeps the previous state.
                self.log.warning("Course fetch failed this pass; skipping diff.")
                return None
            soup = BeautifulSoup(resp.text, "lxml")
            available.extend(self._parse_week(soup))

        # Apply filters (case-insensitive, partial match)
        if self.filter_courses:
            available = [
                s for s in available
                if any(f in _normalize(s["course"]) for f in self.filter_courses)
            ]
        if self.filter_instructors:
            available = [
                s for s in available
                if any(f in _normalize(s["instructor"]) for f in self.filter_instructors)
            ]
        available = [
            s for s in available
            if self._within_days_ahead(s["date"], self.course_days_ahead)
        ]

        seen_in_pass = set()
        deduped = []
        for s in available:
            k = self._slot_key(s)
            if k not in seen_in_pass:
                seen_in_pass.add(k)
                deduped.append(s)
        return deduped

    def _calendar_header_text(self, calendar):
        header = calendar.find("header")
        if header:
            return " ".join(header.get_text(" ", strip=True).split())
        heading = calendar.find(["h2", "h3", "h4"])
        return " ".join(heading.get_text(" ", strip=True).split()) if heading else ""

    def _parse_navigation_libre_page(self, soup):
        available = []

        for category in soup.select("dl.nav.calendar > dd > dl"):
            title_el = category.find("dt", recursive=False)
            slots_el = category.find("dd", recursive=False)
            if not title_el or not slots_el:
                continue

            support = " ".join(title_el.get_text(" ", strip=True).split())
            if "navigation libre" not in _normalize(support):
                continue

            for item in slots_el.select(".cours_items .item"):
                link = item.select_one(".inscr a.btn_insc[href]")
                if link is None:
                    continue

                date_el = item.select_one(".date .dt")
                hour_el = item.select_one(".date .hour")
                infos_el = item.select_one(".infos")
                href = urljoin(SPORT_BASE_URL, link.get("href", ""))

                available.append({
                    "date": date_el.get_text(strip=True) if date_el else "",
                    "support": support,
                    "time": hour_el.get_text(" ", strip=True).replace("\u2013", "-") if hour_el else "",
                    "planning_id": "",
                    "infos": infos_el.get_text(" ", strip=True) if infos_el else "",
                    "href": href,
                })

        for calendars in soup.select(".calendars"):
            day = calendars.get("data-day", "").strip()
            current_support = ""

            for calendar in calendars.select(".calendar"):
                classes = calendar.get("class", [])
                if "day" in classes:
                    current_support = self._calendar_header_text(calendar) or current_support
                    continue
                if "planning" not in classes:
                    continue

                support = self._calendar_header_text(calendar) or current_support or "Navigation libre"
                planning_id = calendar.get("data-planning", "").strip()
                free_blocks = []

                for loc in calendar.select("div.loc.free"):
                    if "lock" in loc.get("class", []):
                        continue
                    hr_from = loc.get("data-from", "").strip()
                    hr_to = loc.get("data-to", "").strip()
                    if hr_from and hr_to:
                        free_blocks.append((hr_from, hr_to))

                if not free_blocks:
                    continue

                free_blocks.sort()
                merged = []
                for hr_from, hr_to in free_blocks:
                    if merged and merged[-1][1] == hr_from:
                        merged[-1] = (merged[-1][0], hr_to)
                    else:
                        merged.append((hr_from, hr_to))

                for hr_from, hr_to in merged:
                    available.append({
                        "date": day,
                        "support": support,
                        "time": f"{hr_from}-{hr_to}",
                        "planning_id": planning_id,
                        "infos": "",
                        "href": self.navigation_url,
                    })

        if self.filter_navigation_supports:
            available = [
                s for s in available
                if any(f in _normalize(s["support"]) for f in self.filter_navigation_supports)
            ]
        available = [
            s for s in available
            if self._within_days_ahead(s["date"], self.navigation_days_ahead)
        ]
        available = [
            s for s in available
            if self._passes_navigation_time_filters(s)
        ]

        seen_in_pass = set()
        deduped = []
        for s in available:
            k = self._navigation_slot_key(s)
            if k not in seen_in_pass:
                seen_in_pass.add(k)
                deduped.append(s)
        return deduped

    def check_navigation_libre_availability(self):
        resp = self._fetch_navigation_libre_page()
        if resp is None:
            # Distinguish fetch failure (None) from a genuinely empty page ([]).
            self.log.warning("Navigation libre fetch failed this pass; skipping diff.")
            return None
        soup = BeautifulSoup(resp.text, "lxml")
        return self._parse_navigation_libre_page(soup)

    def _parse_inscription_slots(self, soup):
        """All Navigation libre inscription slots on the page, plus our booking.

        Unlike _parse_navigation_libre_page this ignores the monitoring filters
        (support/time/days_ahead) — auto-registration must see every slot the
        request could match, not just the ones being watched for alerts.

        Slots carry "registered": the slot we're booked on shows an
        `a.in` "inscrit" link instead of the `a.btn_insc` one (that marker is
        the ONLY place the site reveals the current booking — the account page
        doesn't list Navigation libre bookings at all). "dtype" is "1"/"2"
        (solo/team) read from the item's type icon, or None.
        """
        slots = []
        for category in soup.select("dl.nav.calendar > dd > dl"):
            title_el = category.find("dt", recursive=False)
            slots_el = category.find("dd", recursive=False)
            if not title_el or not slots_el:
                continue
            support = " ".join(title_el.get_text(" ", strip=True).split())
            if "navigation libre" not in _normalize(support):
                continue
            for item in slots_el.select(".cours_items .item"):
                registered = False
                link = item.select_one(".inscr a.btn_insc[href]")
                if link is None:
                    link = item.select_one(".inscr a.in[href]")
                    registered = link is not None
                if link is None:
                    continue  # "complet" — no capacity and not ours
                type_img = item.select_one(".type img[alt]")
                type_alt = _normalize(type_img.get("alt", "")) if type_img else ""
                dtype = "2" if "equipe" in type_alt else ("1" if "individuel" in type_alt else None)
                date_el = item.select_one(".date .dt")
                hour_el = item.select_one(".date .hour")
                slots.append({
                    "date": date_el.get_text(strip=True) if date_el else "",
                    "support": support,
                    "time": hour_el.get_text(" ", strip=True).replace("–", "-") if hour_el else "",
                    "href": urljoin(SPORT_BASE_URL, link.get("href", "")),
                    "registered": registered,
                    "dtype": dtype,
                })
        return slots

    def register_navigation_slot(self, href, partner=None):
        """Attempt to register for one inscription slot.

        Returns (ok, note, dtype) where dtype is "1" (solo) / "2" (team) / None.
        note is "registered", "already registered", "needs partner", "full",
        "quota" (blocked by the account's existing active registration), or an
        error description. See the reverse-engineered flow: the hidden `type` field
        must be set from the availability button's data-type or the POST is ignored.
        """
        try:
            resp = self.navigation_session.get(href, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            return False, f"fetch failed: {e}", None

        soup = BeautifulSoup(resp.text, "lxml")
        if self._looks_logged_out(soup):
            # An expired session must not be mistaken for "full": the poll
            # loop's page fetch re-logs in, so the next pass will see truth.
            return False, "not logged in", None
        if soup.select_one("a[href*='a=remove']"):
            return True, "already registered", None

        button = soup.select_one("button.btn_insc[data-type]")
        if button is None:
            scope = soup.select_one("#inscriptions") or soup
            if "quota" in _normalize(scope.get_text(" ", strip=True)):
                # "Quota d'inscription(s) active(s) atteint": the account already
                # holds an active registration (only one allowed at a time), so
                # EVERY other slot is blocked — not just this one.
                return False, "quota", None
            return False, "full", None
        dtype = (button.get("data-type") or "").strip()

        form = None
        for candidate in soup.find_all("form"):
            if candidate.find("input", {"name": "confirm_valid"}):
                form = candidate
                break
        if form is None:
            return False, "no inscription form", dtype

        if dtype == "2" and not partner:
            return False, "needs partner", dtype

        payload = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if not name:
                continue
            input_type = (inp.get("type") or "text").lower()
            if input_type == "checkbox":
                if name == "confirm_valid":
                    payload[name] = inp.get("value", "1")
            elif name == "groupe_nom":
                if dtype == "2":
                    payload[name] = partner
            elif name == "type":
                payload[name] = dtype
            else:
                payload[name] = inp.get("value", "")
        if dtype == "2" and "quantity" not in payload:
            payload["quantity"] = "2"

        post_url = urldefrag(resp.url)[0]
        try:
            post_resp = self.navigation_session.post(post_url, data=payload, timeout=20)
            post_resp.raise_for_status()
        except requests.RequestException as e:
            return False, f"post failed: {e}", dtype

        post_soup = BeautifulSoup(post_resp.text, "lxml")
        ok = bool(post_soup.select_one("a[href*='a=remove']")) and not post_soup.find(
            "input", {"name": "confirm_valid"}
        )
        return (True, "registered", dtype) if ok else (False, "submit rejected", dtype)

    def _format_iso_day(self, iso):
        try:
            d = date.fromisoformat(iso)
        except (ValueError, TypeError):
            return iso
        weekdays = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
        return f"{weekdays[d.weekday()]} {d.strftime('%d.%m.%Y')}"

    def _format_request(self, req):
        boats = ", ".join(req.get("boats_label") or req.get("boats") or [])
        hour = req["hour_label"] if req.get("hour_min") is not None else "any time"
        return (
            f"[p{req.get('priority', 5)}] 📅 {escape(self._format_iso_day(req['day']))}"
            f" · ⏱ {escape(hour)} · ⛵ {escape(boats)}"
        )

    def _format_registration(self, reg):
        boat = self._short_navigation_support(reg.get("support") or "?")
        text = (
            f"⛵ {escape(boat)} · 📅 {escape(self._format_iso_day(reg.get('day') or '?'))}"
            f" · ⏱ {escape(reg.get('time') or '?')}"
        )
        if reg.get("priority") is not None:
            text += f" · p{reg['priority']}"
        return text

    def _sort_requests(self):
        self.registration_requests.sort(
            key=lambda r: (r.get("priority", 5), r.get("created", 0))
        )

    def _registration_times(self, reg):
        """(start, end) datetimes of a booking; either may be None if unparseable."""
        try:
            day = date.fromisoformat(reg.get("day", ""))
        except (TypeError, ValueError):
            return None, None
        midnight = datetime.combine(day, datetime.min.time())
        start_min, end_min = self._parse_slot_time_range(reg.get("time", ""))
        start = midnight + timedelta(minutes=start_min) if start_min is not None else None
        end = midnight + timedelta(minutes=end_min) if end_min is not None else None
        return start, end

    def _slot_status(self, href):
        """Probe one slot's rid page (read-only GET).

        Returns (status, dtype, soup). status is one of:
          "registered" — this booking is ours (remove link present)
          "open"       — inscription button present, can register
          "quota"      — blocked: the account already holds an active registration
          "closed"     — full or not open (no button, no quota message)
          "error"      — fetch failed (soup is None)
        dtype is "1" (solo) / "2" (team) when determinable — from the button, or
        from the "Type Individuel/Equipes" text that stays visible even when the
        inscription form is hidden by the quota.
        """
        try:
            resp = self.navigation_session.get(href, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            self.log.warning(f"Slot status fetch failed ({href}): {e}")
            return "error", None, None
        soup = BeautifulSoup(resp.text, "lxml")
        if self._looks_logged_out(soup):
            # Session expired: a login page has no remove link / button, which
            # must read as "don't know", never as "closed" or "cancelled".
            self.log.warning(f"Slot status probe hit the login page ({href})")
            return "error", None, None
        scope = soup.select_one("#inscriptions") or soup
        text = _normalize(scope.get_text(" ", strip=True))
        button = soup.select_one("button.btn_insc[data-type]")
        dtype = None
        if button is not None:
            dtype = (button.get("data-type") or "").strip() or None
        elif "type equipes" in text:
            dtype = "2"
        elif "type individuel" in text:
            dtype = "1"
        if soup.select_one("a[href*='a=remove']"):
            return "registered", dtype, soup
        if button is not None:
            return "open", dtype, soup
        if "quota" in text:
            return "quota", dtype, soup
        return "closed", dtype, soup

    def cancel_navigation_slot(self, href):
        """Cancel our booking on one slot. Returns (ok, note).

        Idempotent: cancelling a slot we're not booked on returns
        (True, "not registered"). The site refuses cancellation once the slot
        has started — that surfaces as (False, "still registered after removal").
        """
        try:
            resp = self.navigation_session.get(href, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            return False, f"fetch failed: {e}"
        soup = BeautifulSoup(resp.text, "lxml")
        if self._looks_logged_out(soup):
            return False, "not logged in"
        remove_link = soup.select_one("a[href*='a=remove']")
        if remove_link is None:
            return True, "not registered"
        remove_url = urldefrag(urljoin(resp.url, remove_link.get("href", "")))[0]
        try:
            confirm = self.navigation_session.get(remove_url, timeout=15)
            confirm.raise_for_status()
        except requests.RequestException as e:
            return False, f"remove failed: {e}"
        status, _, _ = self._slot_status(href)
        if status == "registered":
            return False, "still registered after removal"
        self.log.info(f"Cancelled booking: {href}")
        return True, "cancelled"

    def _set_current_registration(self, slot, req=None, dtype=None):
        slot_date = self._parse_slot_date(slot["date"])
        self.current_registration = {
            "href": slot["href"],
            "day": slot_date.isoformat() if slot_date else str(slot.get("date", "")),
            "time": slot.get("time", ""),
            "support": slot.get("support", ""),
            # None = booking made outside the bot: any armed request outranks it.
            "priority": req.get("priority", 5) if req is not None else None,
            "partner": (self.navigation_partner or "") if dtype == "2" else "",
            "dtype": dtype,
            "registered_at": time.time(),
        }

    def _notify_registered(self, slot, note, dtype, replaced=None):
        boat = self._short_navigation_support(slot["support"])
        if replaced is not None:
            head = "🔁 <b>Upgraded booking!</b>"
        elif note == "already registered":
            head = "✅ <b>Already registered</b> for a matching slot"
        else:
            head = "✅ <b>Registered!</b>"
        lines = [
            head,
            f"⛵ {escape(boat)}",
            f"📅 {escape(self._format_short_date(slot['date']))}  ⏱ {escape(slot['time'])}",
        ]
        if dtype == "2" and self.navigation_partner:
            lines.append(f"👥 with {escape(self.navigation_partner)}")
        if replaced is not None:
            lines.append(f"🗑️ cancelled: {self._format_registration(replaced)}")
        lines.append(f'\n🔗 <a href="{escape(self.navigation_url)}">View</a>')
        self.send_telegram("\n".join(lines))
        self.log.info(
            f"Auto-registered ({note}): {slot['support']} {slot['date']} {slot['time']}"
            + (f" — replaced {replaced.get('support')} {replaced.get('day')}" if replaced else "")
        )

    def _warn_needs_partner(self, req, slot):
        if not req.get("warned_partner"):
            self.send_telegram(
                "⚠️ A matching slot is open but it's a <b>team boat</b> and no "
                "partner is set. Send /partner &lt;name&gt; and I'll grab it, or "
                "keep only solo boats in the request."
            )
            req["warned_partner"] = True
        self.log.info(
            f"Skip team slot (no partner): {slot['support']} {slot['date']} {slot['time']}"
        )

    def _request_candidates(self, req, slots):
        """Open slots matching one request, best first (boat order, then start)."""
        candidates = []
        for slot in slots:
            if slot.get("registered"):
                continue  # that's our current booking, not an open spot
            slot_date = self._parse_slot_date(slot["date"])
            if slot_date is None or slot_date.isoformat() != req["day"]:
                continue
            start, _ = self._parse_slot_time_range(slot["time"])
            if req.get("hour_min") is not None and start != req["hour_min"]:
                continue
            support = _normalize(slot["support"])
            rank = next((i for i, boat in enumerate(req["boats"]) if boat in support), None)
            if rank is None:
                continue
            candidates.append((rank, start if start is not None else 9999, slot))
        candidates.sort(key=lambda c: (c[0], c[1]))
        return [slot for _, _, slot in candidates]

    def _register_from_candidates(self, req, candidates, replaced=None):
        """Try candidates best-first. Returns (booked_slot_or_None, quota_blocked).

        On success also records current_registration and sends the notification
        (worded as an upgrade when `replaced` is given). quota_blocked=True means
        the account holds an active registration the site won't let us add to —
        no point trying further slots.
        """
        for slot in candidates:
            ok, note, dtype = self.register_navigation_slot(
                slot["href"], partner=self.navigation_partner or None
            )
            if ok:
                self._set_current_registration(slot, req, dtype)
                self._notify_registered(slot, note, dtype, replaced)
                return slot, False
            if note == "needs partner":
                self._warn_needs_partner(req, slot)
            elif note == "quota":
                self.log.info(
                    f"Blocked by active-registration quota: "
                    f"{slot['support']} {slot['date']} {slot['time']}"
                )
                return None, True
            else:
                self.log.info(
                    f"Register attempt failed ({note}): {slot['support']} {slot['date']} {slot['time']}"
                )
        return None, False

    def _verify_current_registration(self):
        """Check the held booking against the site; clear it when it's gone.

        A booking whose slot has started is left alone (the site refuses
        cancellation after the start, so it can't be upgraded either); once the
        slot is over it's cleared silently — the booking was simply used.
        """
        reg = self.current_registration
        if reg is None:
            return
        now = datetime.now()
        start_dt, end_dt = self._registration_times(reg)
        try:
            reg_day = date.fromisoformat(reg.get("day", ""))
        except (TypeError, ValueError):
            reg_day = None
        over = (end_dt is not None and now >= end_dt) or (
            end_dt is None and (reg_day is None or reg_day < date.today())
        )
        if over:
            self.log.info(
                f"Booking over — clearing: {reg.get('support')} {reg.get('day')} {reg.get('time')}"
            )
            self.current_registration = None
            return
        in_progress = (start_dt is not None and now >= start_dt) or (
            start_dt is None and reg_day == date.today()
        )
        if in_progress:
            return
        status, _, _ = self._slot_status(reg["href"])
        if status == "error":
            return  # transient fetch problem; keep our belief
        if status != "registered":
            self.send_telegram(
                "ℹ️ Your booking is no longer on the site (cancelled outside the bot?):\n"
                f"{self._format_registration(reg)}\n"
                "Pending requests stay armed."
            )
            self.log.info(f"Booking disappeared from the site: {reg}")
            self.current_registration = None

    def _discover_registration(self, slots):
        """Find a booking made outside the bot from the parsed listing slots.

        The listing flags the booked slot with an `a.in` "inscrit" link (the
        account page does NOT list Navigation libre bookings — verified live).
        A booking whose slot already started has left the listing and stays
        undiscoverable; it can't be cancelled anyway.
        """
        for slot in slots:
            if not slot.get("registered"):
                continue
            slot_date = self._parse_slot_date(slot["date"])
            reg = {
                "href": slot["href"],
                "day": slot_date.isoformat() if slot_date else "",
                "time": slot.get("time", ""),
                "support": slot.get("support", ""),
                "priority": None,
                "partner": "",
                "dtype": slot.get("dtype"),
                "registered_at": time.time(),
            }
            self.log.info(f"Discovered existing booking on the site: {reg}")
            return reg
        return None

    def _adopt_matching_requests(self):
        """Fold pending requests already satisfied by the held booking into it.

        The booking takes the best (lowest) priority among the requests it
        satisfies, so only strictly better requests will replace it later.
        """
        reg = self.current_registration
        if reg is None or not self.registration_requests:
            return
        start_min, _ = self._parse_slot_time_range(reg.get("time", ""))
        support = _normalize(reg.get("support", ""))
        kept, adopted = [], []
        for req in self.registration_requests:
            matches = (
                req["day"] == reg.get("day")
                and (req.get("hour_min") is None or req["hour_min"] == start_min)
                and any(boat in support for boat in req["boats"])
            )
            (adopted if matches else kept).append(req)
        if not adopted:
            return
        best = min(r.get("priority", 5) for r in adopted)
        if reg.get("priority") is None or best < reg["priority"]:
            reg["priority"] = best
        self.registration_requests = kept
        for req in adopted:
            self.log.info(f"Request satisfied by existing booking: {req}")
        label = "this request" if len(adopted) == 1 else f"{len(adopted)} requests"
        self.send_telegram(
            f"ℹ️ Your existing booking already covers {label}:\n"
            + "\n".join(self._format_request(r) for r in adopted)
            + f"\n\nTracking it as:\n{self._format_registration(reg)}"
        )

    def _try_upgrade(self, slots):
        """Swap the held booking for the best strictly-higher-priority open slot.

        Keeps the no-booking window small: a candidate is first confirmed viable
        (page reachable, right type, partner available), only then the current
        booking is cancelled and the new one registered; if that registration
        fails the old slot is re-registered immediately.
        """
        reg = self.current_registration
        reg_priority = reg.get("priority")  # None = manual booking, always outranked
        now = datetime.now()
        start_dt, _ = self._registration_times(reg)
        if start_dt is not None:
            if now >= start_dt:
                return  # started: the site refuses cancellation
        else:
            try:
                reg_day = date.fromisoformat(reg.get("day", ""))
            except (TypeError, ValueError):
                return
            if reg_day <= date.today():
                return  # can't prove it hasn't started yet — don't touch it
        if reg.get("dtype") == "2" and not (reg.get("partner") or self.navigation_partner):
            # A team booking whose partner name we don't know could not be
            # re-registered if the upgrade failed — never risk it.
            self.log.info("Upgrade skipped: held team booking has no partner name for rollback.")
            return
        for req in self.registration_requests:
            if reg_priority is not None and req.get("priority", 5) >= reg_priority:
                return  # requests are sorted: nothing better follows
            candidates = [
                s for s in self._request_candidates(req, slots)
                if urldefrag(s["href"])[0] != urldefrag(reg["href"])[0]
            ]
            viable = []
            for slot in candidates[:3]:
                status, dtype, _ = self._slot_status(slot["href"])
                # While we hold a booking every other slot reads "quota"; the
                # fresh listing already said it has capacity, so accept both.
                if status not in ("open", "quota"):
                    continue
                if dtype == "2" and not self.navigation_partner:
                    self._warn_needs_partner(req, slot)
                    continue
                viable.append(slot)
            if not viable:
                continue
            old = dict(reg)
            ok, note = self.cancel_navigation_slot(reg["href"])
            if not ok:
                self.log.warning(f"Upgrade aborted — could not cancel current booking: {note}")
                return
            self.current_registration = None
            booked, _ = self._register_from_candidates(req, viable, replaced=old)
            if booked is not None:
                self.registration_requests = [r for r in self.registration_requests if r is not req]
                return
            # Roll back: get the old slot back before someone else takes it.
            ok2, note2, _ = self.register_navigation_slot(
                old["href"], partner=old.get("partner") or self.navigation_partner or None
            )
            if ok2:
                self.current_registration = old
                self.send_telegram(
                    "⚠️ Tried to upgrade to a better slot but the registration "
                    f"failed — your original booking is safe:\n{self._format_registration(old)}"
                )
                self.log.warning("Upgrade failed; original booking restored.")
            else:
                self.send_telegram(
                    "🚨 <b>Upgrade went wrong.</b> I cancelled your booking\n"
                    f"{self._format_registration(old)}\n"
                    f"but the new registration failed and re-booking the old slot also "
                    f"failed ({escape(note2)}). <b>You have no booking right now</b> — "
                    f'check <a href="{escape(self.navigation_url)}">the site</a>.'
                )
                self.log.error(f"Upgrade rollback failed ({note2}); no booking held.")
            return

    def _process_registration_requests(self):
        """Poll-loop reconciler for auto-registration.

        Invariants: at most one active booking (site rule); the booking held is
        the one for the highest-priority fulfillable request; pending requests
        stay armed until they expire, are cancelled, or are satisfied by (folded
        into) a booking. Bookings made by hand on the site are discovered via
        the account page and replaced when any armed request becomes fulfillable.
        """
        with self._fetch_lock:
            if not self.navigation_enabled:
                return
            if not self.registration_requests and self.current_registration is None:
                return

            self._verify_current_registration()

            today_iso = date.today().isoformat()
            kept = []
            for req in self.registration_requests:
                if req["day"] < today_iso:
                    self.send_telegram(
                        "⌛️ Registration request expired (no slot opened in time):\n"
                        f"{self._format_request(req)}"
                    )
                    self.log.info(f"Registration request expired: {req}")
                else:
                    kept.append(req)
            self.registration_requests = kept

            if not self.registration_requests:
                self._save_state()
                return

            resp = self._fetch_navigation_libre_page()
            if resp is None:
                self.log.warning("Registration fetch failed this pass; will retry next cycle.")
                self._save_state()
                return
            slots = self._parse_inscription_slots(BeautifulSoup(resp.text, "lxml"))

            if self.current_registration is None:
                discovered = self._discover_registration(slots)
                if discovered is not None:
                    self.current_registration = discovered
            self._adopt_matching_requests()

            if not self.registration_requests:
                self._save_state()
                return

            if self.current_registration is None:
                for req in list(self.registration_requests):
                    candidates = self._request_candidates(req, slots)
                    if not candidates:
                        continue
                    booked, quota_blocked = self._register_from_candidates(req, candidates)
                    if booked is not None:
                        self.registration_requests = [
                            r for r in self.registration_requests if r is not req
                        ]
                        break
                    if quota_blocked:
                        if not req.get("warned_quota"):
                            self.send_telegram(
                                "⚠️ A slot you want is open, but your account already has "
                                "an active registration that I can't locate (its slot may "
                                "already be underway), and only one is allowed at a time. "
                                "If you have a booking you don't need, cancel it on the "
                                "site and I'll grab the slot:\n"
                                f"{self._format_request(req)}"
                            )
                            req["warned_quota"] = True
                        break
            else:
                self._try_upgrade(slots)
            self._save_state()

    def send_telegram(self, message):
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"}
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if not resp.ok:
                self.log.error(f"Telegram send failed: {resp.status_code} {resp.text[:200]}")
        except requests.RequestException as e:
            self.log.error(f"Telegram request error: {e}")

    def _fetch_wind(self):
        try:
            resp = requests.get(
                f"{WINDSPOTS_API}/stationdata",
                params={"station": self.windspots_station, "duration": 1},
                timeout=10,
            )
            resp.raise_for_status()
            sd = resp.json().get("stationData", {})
            if not sd:
                return None
            return {
                "speed_kmh": float(sd.get("speed") or 0),
                "gust_kmh": float(sd.get("gust") or 0),
                "direction": sd.get("direction", ""),
                "direction_alpha": sd.get("directionAlpha", ""),
                "temperature": sd.get("temperature", ""),
                "water": sd.get("water", ""),
                "last_update": sd.get("lastUpdate", ""),
            }
        except (requests.RequestException, ValueError, KeyError) as e:
            self.log.error(f"Failed to fetch wind data: {e}")
            return None

    @staticmethod
    def _kmh_to_knots(kmh):
        return kmh / 1.852

    def _wind_data_lines(self, data):
        speed_kt = self._kmh_to_knots(data["speed_kmh"])
        gust_kt = self._kmh_to_knots(data["gust_kmh"])
        lines = [f"{speed_kt:.1f} kt ({data['speed_kmh']:.1f} km/h) — gusts to {gust_kt:.1f} kt"]
        if data["direction_alpha"]:
            lines.append(f"Direction: {data['direction']}° {data['direction_alpha']}")
        parts = []
        if data["temperature"]:
            parts.append(f"Air {data['temperature']}°C")
        if data["water"]:
            parts.append(f"Water {data['water']}°C")
        if parts:
            lines.append(" · ".join(parts))
        return lines

    def _can_send_wind_alert(self):
        now = time.time()
        self._wind_alert_times = [t for t in self._wind_alert_times if now - t < 3600]
        return len(self._wind_alert_times) < self.max_alerts_per_hour

    def _check_wind_alert(self):
        if not self.windspots_enabled or self.paused:
            return
        data = self._fetch_wind()
        if data is None:
            return
        speed_kt = self._kmh_to_knots(data["speed_kmh"])
        if speed_kt >= self.wind_alert_threshold_kt and self._can_send_wind_alert():
            lines = [f"💨 <b>Wind alert!</b> Check it out — it's blowing at Dorigny!\n"]
            lines += self._wind_data_lines(data)
            self.send_telegram("\n".join(lines))
            self._wind_alert_times.append(time.time())
            self.log.info(f"Wind alert sent: {data['speed_kmh']:.1f} km/h ({speed_kt:.1f} kt)")

    def _handle_update(self, update):
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        parts = text.split()
        command = parts[0].lower().split("@")[0]  # tolerate /cmd@BotName
        args = parts[1:]

        handlers = {
            "/help": self._cmd_help,
            "/start": self._cmd_help,
            "/status": self._cmd_status,
            "/wind": self._cmd_wind,
            "/courses": self._cmd_courses,
            "/nav": self._cmd_nav,
            "/pause": self._cmd_pause,
            "/resume": self._cmd_resume,
            "/watch": self._cmd_watch,
            "/unwatch": self._cmd_unwatch,
            "/filters": self._cmd_filters,
            "/watchcourse": self._cmd_watchcourse,
            "/unwatchcourse": self._cmd_unwatchcourse,
            "/watchinstructor": self._cmd_watchinstructor,
            "/unwatchinstructor": self._cmd_unwatchinstructor,
            "/threshold": self._cmd_threshold,
            "/register": self._cmd_register,
            "/requests": self._cmd_requests,
            "/unregister": self._cmd_unregister,
            "/partner": self._cmd_partner,
        }
        handler = handlers.get(command)
        if handler is None:
            self.send_telegram("❓ Unknown command. Send /help for the list.")
            return
        try:
            handler(args)
        except Exception as e:
            self.log.error(f"Error handling command {command}: {e}", exc_info=True)
            self.send_telegram("⚠️ Something went wrong handling that command.")

    def _cmd_help(self, args):
        self.send_telegram(
            "<b>SailingBot commands</b>\n"
            "/status — what's being monitored\n"
            "/courses — current course slots now\n"
            "/nav — current navigation libre slots now\n"
            "/wind — current wind at Dorigny\n"
            "/pause — stop alerts\n"
            "/resume — resume alerts\n"
            "/filters — show all alert filters\n"
            "/watchcourse /unwatchcourse &lt;type&gt; — edit the course-type filter\n"
            "/watchinstructor /unwatchinstructor &lt;name&gt; — edit the instructor filter\n"
            "/watch /unwatch &lt;boat&gt; — edit the navigation libre boat filter\n"
            "/threshold &lt;kt&gt; — set the wind alert threshold\n"
            "\n<b>Auto-registration</b>\n"
            "/register [p1-9] &lt;day&gt; &lt;hour&gt; &lt;boats&gt; — auto-sign-up for a nav libre slot\n"
            "   e.g. <code>/register p1 sat 10:00 RS aéro, Laser simple</code>\n"
            "   day: today/tomorrow/sat/04.07 · hour: 10:00 or <code>any</code> · boats: comma-separated alternatives\n"
            "   p1 = most wanted (default p5). Only one live booking at a time; when a\n"
            "   higher-priority slot opens I cancel the current booking and switch to it.\n"
            "/requests — pending auto-registrations + current booking\n"
            "/unregister &lt;n&gt; — cancel pending request number n\n"
            "/unregister booking — cancel the current booking on the site\n"
            "/partner &lt;name&gt; — set the partner name for team boats"
        )

    def _cmd_status(self, args):
        lines = ["<b>SailingBot status</b>"]
        lines.append("⏸️ Alerts: <b>paused</b>" if self.paused else "▶️ Alerts: <b>active</b>")
        lines.append(f"⏱ Poll interval: {self.interval}s")
        lines.append(f"📅 Course lookahead: {self.course_days_ahead} day(s)")
        course_types = self._filter_values("course type")
        if course_types:
            lines.append(f"⛵ Course filter: {escape(', '.join(course_types))}")
        instructors = self._filter_values("instructor")
        if instructors:
            lines.append(f"👤 Instructor filter: {escape(', '.join(instructors))}")
        if self.navigation_enabled:
            supports = self.support_overrides if self.support_overrides is not None else self.navigation_supports_raw
            lines.append(
                f"🧭 Navigation libre: on · lookahead {self.navigation_days_ahead} day(s)"
            )
            lines.append(f"   Boats: {', '.join(supports) if supports else 'all'}")
            if self.current_registration:
                lines.append(f"✅ Booked: {self._format_registration(self.current_registration)}")
            if self.registration_requests:
                lines.append(f"🎯 Auto-register: {len(self.registration_requests)} pending (/requests)")
            if self.navigation_partner:
                lines.append(f"   Partner: {escape(self.navigation_partner)}")
        else:
            lines.append("🧭 Navigation libre: off")
        if self.windspots_enabled:
            lines.append(f"💨 Wind alert threshold: {self.wind_alert_threshold_kt:.0f} kt")
        lines.append("")
        last = self._last_check_time.strftime("%d.%m.%Y %H:%M") if self._last_check_time else "never"
        lines.append(
            f"📊 Last check ({last}): {self._last_slot_count} course slot(s)"
            + (f", {self._last_navigation_slot_count} nav slot(s)" if self.navigation_enabled else "")
        )
        self.send_telegram("\n".join(lines))

    def _cmd_wind(self, args):
        data = self._fetch_wind()
        if data is None:
            self.send_telegram("⚠️ Could not fetch wind data right now. Please try again later.")
            return
        lines = [f"🌊 <b>Dorigny — Centre Nautique</b>\n"]
        lines += self._wind_data_lines(data)
        if data["last_update"]:
            lines.append(f"<i>Updated: {data['last_update']}</i>")
        self.send_telegram("\n".join(lines))

    def _cmd_courses(self, args):
        with self._fetch_lock:
            slots = self.check_availability()
        if slots is None:
            self.send_telegram("⚠️ Could not fetch courses right now. Please try again later.")
            return
        self.send_telegram(self._format_course_list(slots))

    def _cmd_nav(self, args):
        if not self.navigation_enabled:
            self.send_telegram("🧭 Navigation libre monitoring is disabled.")
            return
        with self._fetch_lock:
            slots = self.check_navigation_libre_availability()
        if slots is None:
            self.send_telegram("⚠️ Could not fetch navigation libre right now. Please try again later.")
            return
        self.send_telegram(self._format_navigation_list(slots))

    def _cmd_pause(self, args):
        with self._fetch_lock:
            self.paused = True
            self._save_state()
        self.send_telegram("⏸️ Alerts paused. Send /resume to turn them back on.")

    def _cmd_resume(self, args):
        with self._fetch_lock:
            self.paused = False
            self._save_state()
        self.send_telegram("▶️ Alerts resumed.")

    def _edit_filter(self, args, kind, add, command):
        """Add/remove one entry of a chat-editable alert filter (see _FILTER_SPECS).

        An empty resulting list means "no filter" — everything matches.
        """
        spec = self._FILTER_SPECS[kind]
        if not args:
            self.send_telegram(
                f"Usage: /{command} &lt;{kind}&gt;  (e.g. /{command} {spec['example']})"
            )
            return
        name = " ".join(args).strip()
        with self._fetch_lock:
            current = self._filter_values(kind)
            if add:
                if any(_normalize(name) == _normalize(c) for c in current):
                    self.send_telegram(f"⚠️ Already watching {kind} “{escape(name)}”.")
                    return
                current.append(name)
            else:
                kept = [c for c in current if _normalize(c) != _normalize(name)]
                if len(kept) == len(current):
                    self.send_telegram(f"⚠️ “{escape(name)}” is not in the {kind} filter.")
                    return
                current = kept
            setattr(self, spec["override_attr"], current)
            getattr(self, spec["apply"])()
            self._save_state()
        shown = escape(", ".join(current)) if current else f"all {spec['plural']}"
        self.send_telegram(f"✅ Now watching {spec['plural']}: {shown}")

    def _cmd_watch(self, args):
        self._edit_filter(args, "boat", add=True, command="watch")

    def _cmd_unwatch(self, args):
        self._edit_filter(args, "boat", add=False, command="unwatch")

    def _cmd_watchcourse(self, args):
        self._edit_filter(args, "course type", add=True, command="watchcourse")

    def _cmd_unwatchcourse(self, args):
        self._edit_filter(args, "course type", add=False, command="unwatchcourse")

    def _cmd_watchinstructor(self, args):
        self._edit_filter(args, "instructor", add=True, command="watchinstructor")

    def _cmd_unwatchinstructor(self, args):
        self._edit_filter(args, "instructor", add=False, command="unwatchinstructor")

    def _format_time_rule(self, rule):
        words = {"start_after": "from", "start_before": "start by", "end_after": "end after", "end_before": "until"}
        parts = [
            f"{words[key]} {minutes // 60:02d}:{minutes % 60:02d}"
            for key, minutes in rule["constraints"].items()
        ]
        return f"{rule['raw_pattern']} {', '.join(parts)}"

    def _cmd_filters(self, args):
        def shown(kind):
            values = self._filter_values(kind)
            return escape(", ".join(values)) if values else "<i>all</i>"

        with self._fetch_lock:
            lines = ["<b>Alert filters</b>", ""]
            lines.append("⛵ <b>Courses</b>")
            lines.append(f"Types: {shown('course type')}")
            lines.append(f"Instructors: {shown('instructor')}")
            lines.append(f"Lookahead: {self.course_days_ahead} day(s)")
            lines.append("<i>Edit: /watchcourse /unwatchcourse /watchinstructor /unwatchinstructor</i>")
            if self.navigation_enabled:
                lines.append("")
                lines.append("🧭 <b>Navigation libre</b>")
                lines.append(f"Boats: {shown('boat')}")
                if self.navigation_time_filters:
                    rules = " · ".join(self._format_time_rule(r) for r in self.navigation_time_filters)
                    lines.append(f"Time rules: {escape(rules)} <i>(config.ini)</i>")
                lines.append(f"Lookahead: {self.navigation_days_ahead} day(s)")
                lines.append("<i>Edit: /watch /unwatch</i>")
        self.send_telegram("\n".join(lines))

    def _cmd_threshold(self, args):
        if not args:
            self.send_telegram(f"Current wind alert threshold: {self.wind_alert_threshold_kt:.0f} kt\nUsage: /threshold &lt;kt&gt;")
            return
        try:
            value = float(args[0].replace(",", "."))
        except ValueError:
            self.send_telegram("⚠️ Please give a number, e.g. /threshold 10")
            return
        if value <= 0 or value > 100:
            self.send_telegram("⚠️ Threshold must be between 1 and 100 kt.")
            return
        with self._fetch_lock:
            self.wind_alert_threshold_kt = value
            self._save_state()
        self.send_telegram(f"✅ Wind alert threshold set to {value:.0f} kt.")

    _REGISTER_USAGE = (
        "Usage: /register [p1-p9] &lt;day&gt; &lt;hour&gt; &lt;boats&gt;\n"
        "e.g. <code>/register p1 sat 10:00 RS aéro, Laser simple</code>\n"
        "• p1–p9: optional priority, p1 = most wanted (default p5). Only one live "
        "booking is possible — when a strictly higher-priority slot opens, I cancel "
        "the current booking and take it\n"
        "• day: today, tomorrow, a weekday (sat/samedi), 04.07 or 04.07.2026\n"
        "• hour: a start time like 10:00, or <code>any</code> for any time that day\n"
        "• boats: comma-separated alternatives — I grab the first one that opens"
    )

    def _parse_register_command(self, args):
        """Parse /register args into a request dict. Returns (req, error_message)."""
        args = list(args)
        priority = 5
        if args:
            match = re.fullmatch(r"[pP]([1-9])", args[0].strip())
            if match:
                priority = int(match.group(1))
                args = args[1:]
        if len(args) < 3:
            return None, self._REGISTER_USAGE
        day = _parse_day_token(args[0])
        if day is None:
            return None, f"⚠️ Couldn't read the day “{escape(args[0])}”.\n\n{self._REGISTER_USAGE}"
        if day < date.today():
            return None, f"⚠️ {escape(self._format_iso_day(day.isoformat()))} is in the past."
        hour = _parse_hour_token(args[1])
        if hour is None:
            return None, f"⚠️ Couldn't read the hour “{escape(args[1])}”.\n\n{self._REGISTER_USAGE}"
        boats_label = [b.strip() for b in " ".join(args[2:]).split(",") if b.strip()]
        if not boats_label:
            return None, f"⚠️ List at least one boat.\n\n{self._REGISTER_USAGE}"
        hour_min = None if hour == "any" else hour
        req = {
            "day": day.isoformat(),
            "hour_min": hour_min,
            "hour_label": "any" if hour_min is None else f"{hour_min // 60:02d}:{hour_min % 60:02d}",
            "boats": [_normalize(b) for b in boats_label],
            "boats_label": boats_label,
            "priority": priority,
            "created": time.time(),
        }
        return req, None

    def _cmd_register(self, args):
        if not self.navigation_enabled:
            self.send_telegram("🧭 Navigation libre is disabled; I can't auto-register.")
            return
        req, error = self._parse_register_command(args)
        if error:
            self.send_telegram(error)
            return
        with self._fetch_lock:
            self.registration_requests.append(req)
            self._sort_requests()
            self._save_state()
            # One reconcile pass right away: books it if a matching slot is
            # already open, upgrading away from a lesser booking if needed.
            self._process_registration_requests()
            still_pending = any(r is req for r in self.registration_requests)
            reg = self.current_registration
        if still_pending:
            lines = [f"🎯 <b>Auto-register armed</b>\n{self._format_request(req)}", ""]
            if reg is None:
                lines.append("I'll grab the first matching slot the moment it opens and message you.")
            elif reg.get("priority") is None or req["priority"] < reg["priority"]:
                lines.append(
                    f"You're currently booked on:\n{self._format_registration(reg)}\n"
                    "I'll switch to this request the moment a matching slot opens."
                )
            else:
                lines.append(
                    f"You're currently booked on:\n{self._format_registration(reg)}\n"
                    "This request doesn't outrank that booking, so it stays armed as a "
                    "fallback. Give it a lower p-number than the booking to make me switch."
                )
            lines.append("Send /requests to review or /unregister to cancel.")
            self.send_telegram("\n".join(lines))

    def _cmd_requests(self, args):
        with self._fetch_lock:
            reqs = list(self.registration_requests)
            reg = self.current_registration
        lines = []
        if reg is not None:
            lines.append(f"✅ <b>Current booking</b>\n{self._format_registration(reg)}")
            lines.append("")
        if not reqs:
            lines.append("🎯 No pending auto-registrations. Add one with /register.")
            self.send_telegram("\n".join(lines))
            return
        lines.append(f"🎯 <b>Pending auto-registrations</b> — {len(reqs)}")
        for i, req in enumerate(reqs, 1):
            lines.append(f"\n<b>{i}.</b> {self._format_request(req)}")
        lines.append("\n<i>Cancel one with /unregister &lt;n&gt;"
                     + (" · cancel the booking with /unregister booking</i>" if reg else "</i>"))
        self.send_telegram("\n".join(lines))

    def _cmd_unregister(self, args):
        if args and args[0].lower() in ("booking", "current", "held"):
            with self._fetch_lock:
                # Refresh the session first (re-logs in if expired) so the
                # cancel below sees the real page, not a login redirect; the
                # fetched listing doubles as the discovery source.
                resp = self._fetch_navigation_libre_page()
                reg = self.current_registration
                if reg is None and resp is not None:
                    reg = self._discover_registration(
                        self._parse_inscription_slots(BeautifulSoup(resp.text, "lxml"))
                    )
                if reg is None:
                    self.send_telegram(
                        "✅ No active booking that I know of."
                        if resp is not None
                        else "⚠️ Couldn't reach the site to check for a booking. Try again."
                    )
                    return
                ok, note = self.cancel_navigation_slot(reg["href"])
                if ok:
                    self.current_registration = None
                    self._save_state()
            if ok:
                self.send_telegram(f"🗑️ Booking cancelled:\n{self._format_registration(reg)}")
            else:
                self.send_telegram(
                    f"⚠️ Couldn't cancel the booking ({escape(note)}). "
                    "It may have already started."
                )
            return
        with self._fetch_lock:
            count = len(self.registration_requests)
            if not count:
                self.send_telegram(
                    "🎯 No pending auto-registrations to cancel."
                    + ("\nCancel the booking itself with /unregister booking."
                       if self.current_registration else "")
                )
                return
            if not args or not args[0].isdigit():
                self.send_telegram(
                    f"Usage: /unregister &lt;n&gt;  (1–{count}, see /requests), "
                    "or /unregister booking to cancel the current booking."
                )
                return
            index = int(args[0])
            if not 1 <= index <= count:
                self.send_telegram(f"⚠️ No request #{index}. You have {count}. See /requests.")
                return
            removed = self.registration_requests.pop(index - 1)
            self._save_state()
        self.send_telegram(f"🗑️ Cancelled request:\n{self._format_request(removed)}")

    def _cmd_partner(self, args):
        if not args:
            current = self.navigation_partner or "<i>none set</i>"
            self.send_telegram(
                f"👥 Team-boat partner: {escape(self.navigation_partner) if self.navigation_partner else current}\n"
                "Set with /partner &lt;name&gt;, clear with /partner clear."
            )
            return
        with self._fetch_lock:
            if len(args) == 1 and args[0].lower() in ("clear", "none", "remove"):
                self.navigation_partner = ""
                self._save_state()
                self.send_telegram("👥 Partner cleared. Team boats won't be auto-registered.")
                return
            self.navigation_partner = " ".join(args).strip()
            self._save_state()
        self.send_telegram(f"✅ Team-boat partner set to: {escape(self.navigation_partner)}")

    def _poll_telegram_updates(self):
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        while not self._stop_event.is_set():
            try:
                resp = requests.get(
                    url,
                    params={"timeout": 30, "offset": self._telegram_offset},
                    timeout=35,
                )
                if resp.ok:
                    updates = resp.json().get("result", [])
                    for update in updates:
                        self._telegram_offset = update["update_id"] + 1
                        self._handle_update(update)
                    if updates:
                        with self._fetch_lock:
                            self._save_state()
            except requests.RequestException as e:
                self.log.error(f"Telegram getUpdates error: {e}")
                self._stop_event.wait(5)

    def _format_heartbeat(self):
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        lines = [
            f"💓 <b>Bot alive</b>  —  {now}",
            f"📊 {self._last_slot_count} course slot(s) currently available (next {self.course_days_ahead} day(s))",
        ]
        if self.navigation_enabled:
            lines.append(
                f"🧭 {self._last_navigation_slot_count} navigation libre slot(s) currently available "
                f"(next {self.navigation_days_ahead} day(s))"
            )
        if self.paused:
            lines.append("⏸️ <i>Alerts are paused (/resume to re-enable)</i>")
        return "\n".join(lines)

    def _render_course_slot_lines(self, slots, limit=20):
        lines = []
        for raw_date, day_slots in self._group_slots_by_date(slots[:limit]):
            lines.append("")
            lines.append(f"📅 <b>{escape(self._format_short_date(raw_date))}</b>")
            for slot in day_slots:
                item_lines = [
                    f"⏱ {escape(slot['time'])}",
                    f"⛵ {escape(slot['course'])}",
                ]
                details = []
                if slot.get("instructor"):
                    details.append(f"👤 {escape(slot['instructor'])}")
                if slot.get("enrollment"):
                    details.append(f"🪑 {escape(slot['enrollment'])}")
                if details:
                    item_lines.append(" · ".join(details))
                lines.append("\n".join(item_lines))
        if len(slots) > limit:
            lines.append("")
            lines.append(f"<i>… and {len(slots) - limit} more</i>")
        return lines

    def _render_navigation_slot_lines(self, slots, limit=25):
        lines = []
        for raw_date, day_slots in self._group_slots_by_date(slots[:limit]):
            lines.append("")
            lines.append(f"📅 <b>{escape(self._format_short_date(raw_date))}</b>")
            for slot in day_slots:
                boat = self._short_navigation_support(slot["support"])
                item_lines = [
                    f"⏱ {escape(slot['time'])}",
                    f"⛵ {escape(boat)}",
                ]
                if slot.get("infos"):
                    item_lines.append(escape(slot["infos"]))
                lines.append("\n".join(item_lines))
        if len(slots) > limit:
            lines.append("")
            lines.append(f"<i>… and {len(slots) - limit} more</i>")
        return lines

    def _format_update(self, new_slots, total):
        lines = [f"🆕 <b>Courses</b> — {len(new_slots)} new · {total} available"]
        lines += self._render_course_slot_lines(new_slots)
        lines.append("")
        lines.append(f'🔗 <a href="{self.course_url}">View &amp; register</a>')
        return "\n".join(lines)

    def _format_navigation_update(self, new_slots, total):
        lines = [f"🧭 <b>Navigation libre</b> — {len(new_slots)} new · {total} available"]
        lines += self._render_navigation_slot_lines(new_slots)
        lines.append("")
        lines.append(f'🔗 <a href="{escape(self.navigation_url)}">View &amp; reserve</a>')
        return "\n".join(lines)

    def _format_course_list(self, slots):
        if not slots:
            return "⛵ <b>Courses</b>\n\n<i>No course slots currently available.</i>"
        lines = [f"⛵ <b>Courses</b> — {len(slots)} available"]
        lines += self._render_course_slot_lines(slots)
        lines.append("")
        lines.append(f'🔗 <a href="{self.course_url}">View &amp; register</a>')
        return "\n".join(lines)

    def _format_navigation_list(self, slots):
        if not slots:
            return "🧭 <b>Navigation libre</b>\n\n<i>No slots currently available.</i>"
        lines = [f"🧭 <b>Navigation libre</b> — {len(slots)} available"]
        lines += self._render_navigation_slot_lines(slots)
        lines.append("")
        lines.append(f'🔗 <a href="{escape(self.navigation_url)}">View &amp; reserve</a>')
        return "\n".join(lines)

    def _process_course_slots(self, slots):
        if slots is None:
            return  # fetch failure already logged; keep previous state
        self._last_slot_count = len(slots)
        current_keys = {self._slot_key(s) for s in slots}
        if self._prev_slot_keys is None:
            # First data we've ever seen: adopt as baseline silently (no alert).
            self.log.info(f"Course baseline established: {len(slots)} slot(s) (no alert)")
            self._prev_slot_keys = current_keys
            return
        new_keys = current_keys - self._prev_slot_keys
        removed_keys = self._prev_slot_keys - current_keys
        self.log.info(
            f"Course check: {len(slots)} available, {len(new_keys)} new, {len(removed_keys)} removed"
        )
        new_slots = [s for s in slots if self._slot_key(s) in new_keys]
        if new_slots and not self.paused:
            self.send_telegram(self._format_update(new_slots, len(slots)))
            self.log.info(f"Telegram notification sent ({len(new_slots)} new course slot(s))")
        self._prev_slot_keys = current_keys

    def _process_navigation_slots(self, slots):
        if slots is None:
            return  # fetch failure already logged; keep previous state
        self._last_navigation_slot_count = len(slots)
        current_keys = {self._navigation_slot_key(s) for s in slots}
        if self._prev_navigation_slot_keys is None:
            self.log.info(f"Navigation libre baseline established: {len(slots)} slot(s) (no alert)")
            self._prev_navigation_slot_keys = current_keys
            return
        new_keys = current_keys - self._prev_navigation_slot_keys
        removed_keys = self._prev_navigation_slot_keys - current_keys
        self.log.info(
            f"Navigation libre check: {len(slots)} available, "
            f"{len(new_keys)} new, {len(removed_keys)} removed"
        )
        new_slots = [s for s in slots if self._navigation_slot_key(s) in new_keys]
        if new_slots and not self.paused:
            self.send_telegram(self._format_navigation_update(new_slots, len(slots)))
            self.log.info(f"Telegram notification sent ({len(new_slots)} new navigation libre slot(s))")
        self._prev_navigation_slot_keys = current_keys

    def _shutdown(self, signum, frame):
        self.log.info("Shutdown signal received — stopping...")
        self._running = False

    def run(self):
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)
        self._running = True
        self._stop_event.clear()

        self.log.info("SailingBot starting up")
        if self.filter_courses:
            self.log.info(f"Course filter: {', '.join(self.filter_courses)}")
        if self.filter_instructors:
            self.log.info(f"Instructor filter: {', '.join(self.filter_instructors)}")
        self.log.info(f"Course lookahead: {self.course_days_ahead} day(s)")
        if self.navigation_enabled:
            self.log.info("Navigation libre monitoring enabled")
            self.log.info(f"Navigation libre lookahead: {self.navigation_days_ahead} day(s)")
            if self.filter_navigation_supports:
                self.log.info(f"Navigation libre support filter: {', '.join(self.filter_navigation_supports)}")
            if self.navigation_time_filters:
                self.log.info(
                    "Navigation libre time filters: "
                    + ", ".join(rule["raw_pattern"] for rule in self.navigation_time_filters)
                )
            if self.registration_requests:
                self.log.info(f"Pending auto-registration requests: {len(self.registration_requests)}")
            if self.navigation_partner:
                self.log.info(f"Team-boat partner: {self.navigation_partner}")

        update_thread = threading.Thread(
            target=self._poll_telegram_updates, daemon=True, name="telegram-updates"
        )
        update_thread.start()
        self.log.info("Telegram update listener started (send /help for commands)")

        if self.windspots_enabled:
            self.log.info(
                f"Wind alerts enabled — station {self.windspots_station}, "
                f"threshold {self.wind_alert_threshold_kt:.0f} kt, "
                f"max {self.max_alerts_per_hour}/hour"
            )

        if not self.login():
            self.log.critical("Initial login failed. Check your credentials in config.ini.")
            sys.exit(1)
        if self.navigation_enabled and not self.login_navigation_libre():
            self.log.warning("Navigation libre monitoring disabled for this run because login failed.")
            self.navigation_enabled = False

        self.log.info(f"Polling every {self.interval}s. Press Ctrl+C to stop.")
        self._last_heartbeat = time.time()

        while self._running:
            try:
                with self._fetch_lock:
                    slots = self.check_availability()
                    navigation_slots = (
                        self.check_navigation_libre_availability()
                        if self.navigation_enabled else []
                    )

                self._last_check_time = datetime.now()
                self._process_course_slots(slots)
                if self.navigation_enabled:
                    self._process_navigation_slots(navigation_slots)
                    self._process_registration_requests()

                self._check_wind_alert()

                if time.time() - self._last_heartbeat >= 8 * 3600:
                    self.send_telegram(self._format_heartbeat())
                    self._last_heartbeat = time.time()
                    self.log.info("Heartbeat sent")

                with self._fetch_lock:
                    self._save_state()

            except Exception as e:
                self.log.error(f"Unexpected error during check: {e}", exc_info=True)

            # Sleep in small increments so Ctrl+C is responsive
            for _ in range(self.interval):
                if not self._running:
                    break
                time.sleep(1)

        self._stop_event.set()
        update_thread.join(timeout=40)
        self.log.info("SailingBot stopped.")


def main():
    parser = argparse.ArgumentParser(description="Monitor UNIL sailing and navigation libre slots.")
    parser.add_argument(
        "--inspect-navigation-libre",
        action="store_true",
        help="Log in to sport.unil.ch and save the authenticated navigation libre HTML, then exit.",
    )
    args = parser.parse_args()

    bot = SailingBot(CONFIG_FILE)
    if args.inspect_navigation_libre:
        ok = bot.inspect_navigation_libre()
        sys.exit(0 if ok else 1)
    bot.run()


if __name__ == "__main__":
    main()
