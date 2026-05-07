# SailingBot

Monitors the [UNIL/EPFL Centre Nautique](https://www2.unil.ch/sportres/nautique/) for available sailing course slots and sends a Telegram notification when one opens up.

## Features

- Checks course availability across the current week + next 3 weeks
- Can also monitor “Navigation libre” reservation slots from `sport.unil.ch`
- Notifies only when a **new** slot appears (no spam on every poll)
- Filter by course type and/or instructor
- Sends a **heartbeat message every 8 hours** so you know the bot is still running
- Graceful shutdown on Ctrl+C
- Rotating log file (5 MB, 3 backups)

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure

Copy the example config and fill in your credentials:

```bash
cp config.ini.example config.ini
```

Edit `config.ini`:

```ini
[credentials]
email = your_unil_email@epfl.ch
password = your_password

[telegram]
bot_token = 123456789:ABCdef...   # from @BotFather
chat_id = 123456789               # from @userinfobot

[settings]
poll_interval_seconds = 300       # how often to check
course_url = https://www2.unil.ch/sportres/nautique/inscriptions.php
log_file = sailing_bot.log

[filters]
course_types =    # e.g. Catamaran, Dériveur simple  (empty = all)
instructors =     # e.g. Marine, Gaël               (empty = all)

[navigation_libre]
enabled = false
login = your_navigation_libre_login_here
password = your_navigation_libre_password_here
activity_url = https://sport.unil.ch/?pid=80&aid=92
login_url = https://sport.unil.ch/?pid=29
html_dump_file = navigation_libre_authenticated.html

[navigation_libre_filters]
supports =        # e.g. Catamaran, Dériveur double (empty = all)
```

> `config.ini` is gitignored and will never be pushed.

### 3. Get your Telegram credentials

- **Bot token**: message [@BotFather](https://t.me/BotFather) → `/newbot`
- **Chat ID**: message [@userinfobot](https://t.me/userinfobot) → copy the `id` field

### 4. Run

```bash
python bot.py
```

## Filters

Filters are **case-insensitive** and **accent-insensitive** (so `Deriveur` matches `Dériveur`), and support **partial matching** (`Mar` matches `Marine`).

Multiple values are comma-separated:

```ini
course_types = Catamaran, Dériveur simple
instructors = Marine
```

Leave a filter empty to receive notifications for all courses / all instructors.

## Navigation libre

UNIL uses a separate account for the `sport.unil.ch` “Navigation libre” reservations. Fill the `[navigation_libre]` login/password with that account and set:

```ini
enabled = true
```

Before running the monitor, you can inspect the authenticated page structure:

```bash
python bot.py --inspect-navigation-libre
```

This logs in, fetches `https://sport.unil.ch/?pid=80&aid=92`, and saves the HTML to `navigation_libre_authenticated.html`.

## Notifications

| Event | Telegram message |
|---|---|
| New slot opens | 🚣 alert with date, time, course, instructor, places |
| Nothing changed | silence |
| Every 8 hours | 💓 heartbeat with slot count |

## Running as a systemd service

This is the recommended way to run the bot on a Linux server — it starts automatically on boot and restarts itself if it crashes.

This uses a **user-level systemd service** (no `sudo` needed), which starts automatically on login and restarts on crash.

### 1. Create the service file

```bash
mkdir -p ~/.config/systemd/user
nano ~/.config/systemd/user/sailingbot.service
```

Paste the following, adjusting the paths to match your setup:

```ini
[Unit]
Description=SailingBot sailing course monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/SailingBot
ExecStart=/path/to/SailingBot/venv/bin/python /path/to/SailingBot/bot.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

> If you're not using a virtual environment, replace `ExecStart` with:
> ```
> ExecStart=/usr/bin/python3 /path/to/SailingBot/bot.py
> ```

### 2. Enable and start the service

```bash
# Reload systemd to pick up the new file
systemctl --user daemon-reload

# Enable it so it starts automatically on login
systemctl --user enable sailingbot

# Start it now
systemctl --user start sailingbot
```

### Useful commands

```bash
# Check if the bot is running
systemctl --user status sailingbot

# View live logs
journalctl --user -u sailingbot -f

# View last 100 lines of logs
journalctl --user -u sailingbot -n 100

# Restart the bot (e.g. after editing config.ini)
systemctl --user restart sailingbot

# Stop the bot
systemctl --user stop sailingbot

# Disable auto-start on boot
systemctl --user disable sailingbot
```

> **After editing `config.ini`**, always restart the service for changes to take effect:
> ```bash
> systemctl --user restart sailingbot
> ```
