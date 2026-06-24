# SailingBot

Monitors the [UNIL/EPFL Centre Nautique](https://www2.unil.ch/sportres/nautique/) for available sailing course slots and sends a Telegram notification when one opens up.

## Features

- Checks course availability within a configurable lookahead window
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
days_ahead = 21   # how many days ahead to check courses
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
days_ahead = 21   # how many days ahead to check Navigation libre
supports =        # e.g. Catamaran, Dériveur double (empty = all)

[navigation_libre_time_filters]
# Optional per-boat/support rules. Keys are partial matches.
# after/before filter slot start time; end_before/end_after filter slot end time.
# catamaran = after 17:00
# hobie cat 18 = after 12:00, before 18:00
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

`days_ahead` limits how far into the future the bot checks. Courses and Navigation libre have separate values, so you can monitor, for example, courses 21 days ahead but Navigation libre only 3 days ahead.

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

### Navigation libre time filters

Add optional rules under `[navigation_libre_time_filters]` when different boats need different time windows:

```ini
[navigation_libre_time_filters]
catamaran = after 17:00
hobie cat 18 = after 12:00, before 18:00
laser simple = before 12:00
rs aero = after 09:00, end_before 18:00
```

The key is matched case-insensitively and accent-insensitively against the full support name. If multiple keys match a slot, the most specific key wins.

## Notifications

| Event | Telegram message |
|---|---|
| New slot opens | 🆕 alert listing **only the new** slot(s) |
| Slot booked/expires | silence (query on demand with `/courses` or `/nav`) |
| Nothing changed | silence |
| Fetch failed (network) | silence — previous state kept, no false "removed/new" |
| Every 8 hours | 💓 heartbeat with slot count |

Alerts fire **only on newly-appeared slots**, never on removals, so messages stay
actionable. State is persisted to `state.json`, so restarting the bot does **not**
re-announce everything.

## Telegram commands

Send these to the bot in chat:

| Command | Action |
|---|---|
| `/help` | List all commands |
| `/status` | What's being monitored, filters, last counts, paused state |
| `/courses` | Fetch & show current course slots **now** |
| `/nav` | Fetch & show current Navigation libre slots **now** (is it still free?) |
| `/wind` | Current wind at Dorigny |
| `/pause` / `/resume` | Stop / resume all push alerts (persisted) |
| `/watch <boat>` | Add a boat to the Navigation libre filter (e.g. `/watch RS aéro`) |
| `/unwatch <boat>` | Remove a boat from the filter |
| `/threshold <kt>` | Set the wind alert threshold |

Settings changed via chat are saved to `state.json` and survive restarts; `config.ini`
(with its comments) is never rewritten.

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
# Optional: pin away from a faulty CPU core (see Troubleshooting below)
# CPUAffinity=0-7 10-31
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

## Troubleshooting

### Random crashes / SEGV / "ASCII_SPACES" errors

If the bot crash-loops with `Segmentation fault (core dumped)`, wrong results, or
`AttributeError: 'str' object has no attribute 'ASCII_SPACES'`, this is **not** a bug
in `bot.py` — it indicates a **faulty CPU core** corrupting any process scheduled on it.

Diagnose by checking the kernel log for segfaults that all name the **same CPU**:

```bash
journalctl -k | grep -i segfault
# e.g. every line says "... likely on CPU 8 (core 16, socket 0)"
```

Confirm which logical CPUs share that physical core:

```bash
cat /sys/devices/system/cpu/cpu8/topology/thread_siblings_list   # -> 8-9
```

Mitigate by pinning the service away from those CPUs in
`~/.config/systemd/user/sailingbot.service`:

```ini
[Service]
CPUAffinity=0-7 10-31     # all cores except the faulty 8 & 9
```

Then `systemctl --user daemon-reload && systemctl --user restart sailingbot`.
For a permanent fix, run a memory/CPU test (e.g. `memtest86+`, `stress-ng --cpu`)
and have the hardware serviced.

> This bot currently runs in the conda env **`sailingbot`** (Python 3.13). The older
> `bot` env used Python 3.14, whose Anaconda build was unstable on this machine.
