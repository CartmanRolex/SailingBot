import configparser
import logging
import logging.handlers
import sys

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


def main():
    bot = SailingBot(CONFIG_FILE)
    bot.log.info("SailingBot initialized successfully.")


if __name__ == "__main__":
    main()
