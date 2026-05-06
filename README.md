# SailingBot

Monitors the [UNIL/EPFL Centre Nautique](https://www2.unil.ch/sportres/nautique/) for available sailing course slots and sends a Telegram notification when one opens up.

## Features

- Checks course availability across the current week + next 3 weeks
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

## Notifications

| Event | Telegram message |
|---|---|
| New slot opens | 🚣 alert with date, time, course, instructor, places |
| Nothing changed | silence |
| Every 8 hours | 💓 heartbeat with slot count |

## Running as a systemd service

This is the recommended way to run the bot on a Linux server — it starts automatically on boot and restarts itself if it crashes.

### 1. Create the service file

```bash
sudo nano /etc/systemd/system/sailingbot.service
```

Paste the following, adjusting the paths and user to match your setup:

```ini
[Unit]
Description=SailingBot — UNIL course availability monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/SailingBot
ExecStart=/usr/bin/python3 /path/to/SailingBot/bot.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

> If you're using a virtual environment, replace `ExecStart` with:
> ```
> ExecStart=/path/to/SailingBot/venv/bin/python /path/to/SailingBot/bot.py
> ```

### 2. Enable and start the service

```bash
# Reload systemd to pick up the new file
sudo systemctl daemon-reload

# Enable it so it starts automatically on boot
sudo systemctl enable sailingbot

# Start it now
sudo systemctl start sailingbot
```

### Useful commands

```bash
# Check if the bot is running
sudo systemctl status sailingbot

# View live logs
sudo journalctl -u sailingbot -f

# View last 100 lines of logs
sudo journalctl -u sailingbot -n 100

# Restart the bot (e.g. after editing config.ini)
sudo systemctl restart sailingbot

# Stop the bot
sudo systemctl stop sailingbot

# Disable auto-start on boot
sudo systemctl disable sailingbot
```

> **After editing `config.ini`**, always restart the service for changes to take effect:
> ```bash
> sudo systemctl restart sailingbot
> ```
