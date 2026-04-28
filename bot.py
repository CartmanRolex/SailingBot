import configparser
import logging
import logging.handlers
import signal
import sys
import time
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

CONFIG_FILE = "config.ini"
LOGIN_URL = "https://www2.unil.ch/sportres/nautique/login.php"
BASE_URL = "https://www2.unil.ch/sportres/nautique/"

REQUIRED_KEYS = {
    "credentials": ["email", "password"],
    "telegram": ["bot_token", "chat_id"],
    "settings": ["poll_interval_seconds", "course_url", "log_file"],
}


def _parse_filter_list(raw):
    """Split a comma-separated config value into a lowercase stripped list, dropping empties."""
    return [v.strip().lower() for v in raw.split(",") if v.strip()]


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
        self.log = self._setup_logging()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; SailingBot/1.0)"})
        self.form_action = LOGIN_URL
        self._running = False
        self._already_notified = False

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

    def _detect_login_fields(self, soup):
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

        action = form.get("action", LOGIN_URL)
        if action and not action.startswith("http"):
            action = BASE_URL + action.lstrip("/")
        self.form_action = action or LOGIN_URL

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
                payload[name] = self.password
            elif input_type in ("text", "email") and not email_filled:
                payload[name] = self.email
                email_filled = True

        self.log.debug(f"Detected form action: {self.form_action}, fields: {list(payload.keys())}")
        return payload

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
            payload = self._detect_login_fields(soup)
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
                if any(f in s["course"].lower() for f in self.filter_courses)
            ]
        if self.filter_instructors:
            available = [
                s for s in available
                if any(f in s["instructor"].lower() for f in self.filter_instructors)
            ]

        return available

    def send_telegram(self, message):
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"}
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if not resp.ok:
                self.log.error(f"Telegram send failed: {resp.status_code} {resp.text[:200]}")
        except requests.RequestException as e:
            self.log.error(f"Telegram request error: {e}")

    def _format_notification(self, slots):
        lines = [f"<b>🚣 {len(slots)} slot(s) available (next 3 weeks)</b>", ""]
        for slot in slots[:15]:
            lines.append(
                f"<b>{slot['date']}</b>  {slot['time']}  |  {slot['course']}  |  "
                f"{slot['instructor']}  |  {slot['enrollment']}"
            )
        lines.append("")
        lines.append(f"<a href=\"{self.course_url}\">View &amp; register</a>")
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

        if not self.login():
            self.log.critical("Initial login failed. Check your credentials in config.ini.")
            sys.exit(1)

        self.log.info(f"Polling every {self.interval}s. Press Ctrl+C to stop.")

        while self._running:
            try:
                slots = self.check_availability()
                self.log.info(f"Check complete: {len(slots)} available slot(s) found")

                if slots and not self._already_notified:
                    message = self._format_notification(slots)
                    self.send_telegram(message)
                    self._already_notified = True
                    self.log.info("Telegram notification sent")
                elif not slots:
                    self._already_notified = False

            except Exception as e:
                self.log.error(f"Unexpected error during check: {e}", exc_info=True)

            # Sleep in small increments so Ctrl+C is responsive
            for _ in range(self.interval):
                if not self._running:
                    break
                time.sleep(1)

        self.log.info("SailingBot stopped.")


def main():
    bot = SailingBot(CONFIG_FILE)
    bot.run()


if __name__ == "__main__":
    main()
