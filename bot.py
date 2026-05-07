import configparser
import argparse
import logging
import logging.handlers
import signal
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta
from html import escape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

CONFIG_FILE = "config.ini"
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
        self.filter_courses = _parse_filter_list(self.config.get("filters", "course_types", fallback=""))
        self.filter_instructors = _parse_filter_list(self.config.get("filters", "instructors", fallback=""))
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
        self.filter_navigation_supports = _parse_filter_list(
            self.config.get("navigation_libre_filters", "supports", fallback="")
        )
        self.log = self._setup_logging()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; SailingBot/1.0)"})
        self.navigation_session = requests.Session()
        self.navigation_session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; SailingBot/1.0)"})
        self.form_action = LOGIN_URL
        self.navigation_form_action = self.navigation_login_url
        self._running = False
        self._prev_slot_keys = None
        self._prev_navigation_slot_keys = None
        self._last_heartbeat = 0.0

    def _slot_key(self, slot):
        return slot["href"] if slot["href"] else f"{slot['date']}|{slot['time']}|{slot['course']}"

    def _navigation_slot_key(self, slot):
        return slot["href"] if slot.get("href") else f"{slot['date']}|{slot['support']}|{slot['time']}|{slot['planning_id']}"

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

    def _fetch_navigation_libre_page(self):
        try:
            resp = self.navigation_session.get(self.navigation_url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            self.log.error(f"Failed to fetch navigation libre page: {e}")
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        if soup.find("input", {"type": "password"}) or "Se connecter" in soup.get_text(" ", strip=True):
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
        # Check current week + next 3 weeks
        monday = date.today() - timedelta(days=date.today().weekday())
        weeks = [None] + [
            (monday + timedelta(weeks=i)).strftime("%Y%m%d") for i in range(1, 4)
        ]

        available = []
        for refdate in weeks:
            resp = self._fetch_week(refdate)
            if resp is None:
                continue
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
            return []
        soup = BeautifulSoup(resp.text, "lxml")
        return self._parse_navigation_libre_page(soup)

    def send_telegram(self, message):
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"}
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if not resp.ok:
                self.log.error(f"Telegram send failed: {resp.status_code} {resp.text[:200]}")
        except requests.RequestException as e:
            self.log.error(f"Telegram request error: {e}")

    def _format_heartbeat(self, slots, navigation_slots=None):
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        lines = [
            f"💓 <b>Bot alive</b>  —  {now}",
            f"📊 {len(slots)} course slot(s) currently available (next 3 weeks)",
        ]
        if navigation_slots is not None:
            lines.append(f"🧭 {len(navigation_slots)} navigation libre slot(s) currently available")
        return "\n".join(lines)

    def _format_update(self, slots, new_keys):
        appeared = len(new_keys)
        disappeared = (
            len(self._prev_slot_keys - {self._slot_key(s) for s in slots})
            if self._prev_slot_keys is not None else 0
        )

        if appeared and disappeared:
            header = f"🔄 <b>Schedule changed</b> — {appeared} new, {disappeared} removed"
        elif appeared:
            header = f"🆕 <b>{appeared} new slot(s) appeared!</b>"
        elif disappeared:
            header = f"❌ <b>{disappeared} slot(s) no longer available</b>"
        else:
            header = "ℹ️ <b>Schedule updated</b>"

        lines = [header, ""]
        if slots:
            lines.append(f"<b>All available slots ({len(slots)} total):</b>")
            for slot in slots[:20]:
                k = self._slot_key(slot)
                line = (
                    f"  📅 <b>{slot['date']}</b>  🕐 {slot['time']}\n"
                    f"  ⛵ {slot['course']}  •  👤 {slot['instructor']}  •  🪑 {slot['enrollment']}"
                )
                if k in new_keys:
                    line = f"<b>{line}</b>"
                lines.append(line)
        else:
            lines.append("<i>No slots currently available.</i>")

        lines.append("")
        lines.append(f'🔗 <a href="{self.course_url}">View &amp; register</a>')
        return "\n".join(lines)

    def _format_navigation_update(self, slots, new_keys):
        appeared = len(new_keys)
        disappeared = (
            len(self._prev_navigation_slot_keys - {self._navigation_slot_key(s) for s in slots})
            if self._prev_navigation_slot_keys is not None else 0
        )

        if appeared and disappeared:
            header = f"🔄 <b>Navigation libre changed</b> — {appeared} new, {disappeared} removed"
        elif appeared:
            header = f"🧭 <b>{appeared} new navigation libre slot(s) appeared!</b>"
        elif disappeared:
            header = f"❌ <b>{disappeared} navigation libre slot(s) no longer available</b>"
        else:
            header = "ℹ️ <b>Navigation libre updated</b>"

        lines = [header, ""]
        if slots:
            lines.append(f"<b>All available navigation libre slots ({len(slots)} total):</b>")
            for slot in slots[:25]:
                k = self._navigation_slot_key(slot)
                line = (
                    f"  📅 <b>{escape(slot['date'])}</b>  🕐 {escape(slot['time'])}\n"
                    f"  ⛵ {escape(slot['support'])}"
                )
                if slot.get("infos"):
                    line += f"  •  {escape(slot['infos'])}"
                if k in new_keys:
                    line = f"<b>{line}</b>"
                lines.append(line)
        else:
            lines.append("<i>No navigation libre slots currently available.</i>")

        lines.append("")
        lines.append(f'🔗 <a href="{escape(self.navigation_url)}">View &amp; reserve</a>')
        return "\n".join(lines)

    def _shutdown(self, signum, frame):
        self.log.info("Shutdown signal received — stopping...")
        self._running = False

    def run(self):
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)
        self._running = True

        self.log.info("SailingBot starting up")
        if self.filter_courses:
            self.log.info(f"Course filter: {', '.join(self.filter_courses)}")
        if self.filter_instructors:
            self.log.info(f"Instructor filter: {', '.join(self.filter_instructors)}")
        if self.navigation_enabled:
            self.log.info("Navigation libre monitoring enabled")
            if self.filter_navigation_supports:
                self.log.info(f"Navigation libre support filter: {', '.join(self.filter_navigation_supports)}")

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
                slots = self.check_availability()
                navigation_slots = []
                if self.navigation_enabled:
                    navigation_slots = self.check_navigation_libre_availability()
                current_keys = {self._slot_key(s) for s in slots}

                if self._prev_slot_keys is None:
                    new_keys = current_keys
                    changed = bool(current_keys)
                    self.log.info(f"First check: {len(slots)} slot(s) available")
                else:
                    new_keys = current_keys - self._prev_slot_keys
                    removed_keys = self._prev_slot_keys - current_keys
                    changed = bool(new_keys or removed_keys)
                    self.log.info(
                        f"Check complete: {len(slots)} available, "
                        f"{len(new_keys)} new, {len(removed_keys)} removed"
                    )

                if changed:
                    message = self._format_update(slots, new_keys)
                    self.send_telegram(message)
                    self.log.info("Telegram notification sent (schedule changed)")

                self._prev_slot_keys = current_keys

                if self.navigation_enabled:
                    navigation_current_keys = {
                        self._navigation_slot_key(s) for s in navigation_slots
                    }
                    if self._prev_navigation_slot_keys is None:
                        navigation_new_keys = navigation_current_keys
                        navigation_changed = bool(navigation_current_keys)
                        self.log.info(
                            f"First navigation libre check: {len(navigation_slots)} slot(s)"
                        )
                    else:
                        navigation_new_keys = navigation_current_keys - self._prev_navigation_slot_keys
                        navigation_removed_keys = self._prev_navigation_slot_keys - navigation_current_keys
                        navigation_changed = bool(navigation_new_keys or navigation_removed_keys)
                        self.log.info(
                            f"Navigation libre check complete: {len(navigation_slots)} available, "
                            f"{len(navigation_new_keys)} new, {len(navigation_removed_keys)} removed"
                        )

                    if navigation_changed:
                        message = self._format_navigation_update(navigation_slots, navigation_new_keys)
                        self.send_telegram(message)
                        self.log.info("Telegram notification sent (navigation libre changed)")

                    self._prev_navigation_slot_keys = navigation_current_keys

                if time.time() - self._last_heartbeat >= 8 * 3600:
                    self.send_telegram(
                        self._format_heartbeat(
                            slots,
                            navigation_slots if self.navigation_enabled else None,
                        )
                    )
                    self._last_heartbeat = time.time()
                    self.log.info("Heartbeat sent")

            except Exception as e:
                self.log.error(f"Unexpected error during check: {e}", exc_info=True)

            # Sleep in small increments so Ctrl+C is responsive
            for _ in range(self.interval):
                if not self._running:
                    break
                time.sleep(1)

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
