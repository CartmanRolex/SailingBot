import configparser
import logging
import logging.handlers
import sys

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


def main():
    bot = SailingBot(CONFIG_FILE)
    bot.log.info("SailingBot initialized successfully.")


if __name__ == "__main__":
    main()
