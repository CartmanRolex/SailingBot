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

## Running on a server

```bash
nohup python bot.py &
```

Or with systemd, screen, or tmux. The bot handles session expiry automatically (re-logins when the UNIL session expires) and catches all transient errors without crashing.
