# Scheduled Messages

MeshCore Bot can send messages to one or more channels on a fixed daily schedule or on a repeating interval. Everything is managed through the web viewer UI or directly in `config.ini` — no bot restart required.

---

## How it works

Scheduled messages are stored in the `[Scheduled_Messages]` section of `config.ini`. When the bot starts, or when a change is saved via the web UI, APScheduler loads the entries and fires them at the configured time or interval.

**Changes take effect immediately** — the web viewer triggers a hot-reload of the scheduler without restarting the bot.

---

## Config format

```ini
[Scheduled_Messages]
HHMM = #channel:message text
```

For multiple channels, use a comma-separated list:

```ini
[Scheduled_Messages]
0800 = #general,#announcements:Good morning from the bot!
every_30h = #kod-bot:Automated status check
```

---

## Fixed daily time

Use a 4-digit 24-hour time as the key:

```ini
0800 = #general:Good morning!
1730 = #kod-bot,#ve7kod:Evening update
2359 = #general:End of day
```

- Fires **once per day** at the specified time (local timezone)
- `0800` = 8:00 AM, `1730` = 5:30 PM, `2359` = 11:59 PM

---

## Interval (rotating)

Use `every_Xh`, `every_Xm`, or `every_XhYm` as the key:

```ini
every_30h = #general:Rotating update — fires at a different time each day
every_6h30m = #kod-bot:Status ping every 6.5 hours
every_2h = #general:Every 2 hours
```

**First fire:** The first message fires **one full interval after the entry is loaded** (on bot start or after a web UI save). This prevents a flood of messages on restart.

- `every_30h` saved at 14:00 → first fire at **20:00 the next day**, then every 30 hours
- `every_2h` saved at 09:00 → first fire at **11:00**, then every 2 hours

Because the interval can be longer than 24 hours, the message rotates through different times of the day across multiple days — useful for reaching users in different time zones.

---

## Multi-channel support

A single scheduled entry can target multiple channels by separating them with commas:

```ini
1800 = #general,#announcements,#kod-bot:Evening network status
```

Messages are sent **sequentially** — the bot waits for the rate limiter to clear between each channel send (typically 2–3 seconds apart). All channels receive the same message text.

---

## Message placeholders

Messages can include live mesh network data using placeholders:

| Placeholder | Description |
|---|---|
| `{total_contacts}` | Total contacts ever heard |
| `{total_repeaters}` | Total repeaters ever heard |
| `{recent_activity_24h}` | Unique users active in last 24 hours |
| `{total_contacts_30d}` | Contacts active in last 30 days |
| `{total_repeaters_30d}` | Repeaters active in last 30 days |
| `{new_companions_7d}` | New companion devices in last 7 days |
| `{new_repeaters_7d}` | New repeaters in last 7 days |

Example:

```ini
0900 = #general:{total_contacts_30d} contacts active in the last 30 days — {recent_activity_24h} active in the last 24h
```

Line breaks can be embedded using `\n`:

```ini
0800 = #general:Good morning!\nToday's network: {total_contacts} contacts heard.
```

---

## Web viewer UI

The **Scheduled** page (`/scheduled`) provides full CRUD management:

- **Add** — click **Add Message**, choose Fixed or Interval, select one or more channels, enter the message
- **Edit** — click the pencil icon on any row to modify time, channels, or message text
- **Delete** — click the trash icon, confirm in the dialog

All changes are written to `config.ini` and the scheduler is reloaded immediately — no restart needed.

### Fixed time entry

Enter hour (0–23) and minute (0–59) separately in 24-hour format:

- Hour `8`, Minute `0` → fires at 08:00 (8:00 AM)
- Hour `17`, Minute `30` → fires at 17:30 (5:30 PM)

### Interval entry

Enter hours and minutes separately:

- `30h 0m` → every 30 hours
- `6h 30m` → every 6 hours 30 minutes
- `0h 45m` → every 45 minutes

Minutes are optional — entering only hours is valid.

---

## Relationship with channelpause

The `channelpause` admin command suspends bot **responses** on public channels (greetings, commands, keywords), but **does not block scheduled messages**. Scheduled posts will still fire even while channel responses are paused.

---

## Changing the web viewer password

The web viewer password is stored as a bcrypt hash in `config.ini`. To change it, run the helper script:

```bash
sudo /opt/meshcore-bot/venv/bin/python3 /opt/meshcore-bot/hash_password.py
```

Paste the printed hash into `config.ini` under `[Web_Viewer]`:

```ini
web_viewer_password = $2b$12$...
```

Then restart the bot for the new password to take effect.
