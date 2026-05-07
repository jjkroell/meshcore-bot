# MeshCore Bot Web Viewer

A web-based interface for viewing, managing, and sending data through your MeshCore Bot.

## Features

- **Dashboard**: Live bot health, database statistics, top users/channels, and real-time packet feed
- **Repeater Contacts**: Active repeater contacts with location, device type, and status
- **Contact Tracking**: Full history of heard contacts with signal strength and routing data
- **Send Message**: Send to one or more channels directly from the browser
- **Scheduled Messages**: Create and manage fixed-time or interval-based automated messages
- **Cache Data**: View cached geocoding and API responses
- **Config Panel**: Structured settings with categorized topics and database tools
- **Purging Log**: Audit trail of contact purging operations
- **Real-time Updates**: Auto-refreshes every 30 seconds; live packet/command/message feed via WebSocket
- **API Endpoints**: JSON API for programmatic access
- **Password Authentication**: bcrypt-hashed login with brute-force rate limiting

---

## Quick Start

### Option 1: Integrated with Bot (recommended)

Edit `config.ini`:

```ini
[Web_Viewer]
enabled = true
auto_start = true
host = 127.0.0.1
port = 5000
web_viewer_password = $2b$12$...   # see Password Setup below
```

The web viewer starts automatically with the bot.

### Option 2: Standalone Mode

```bash
python3 -m modules.web_viewer.app --config config.ini --host 0.0.0.0 --port 5000
```

---

## Configuration

```ini
[Web_Viewer]
# Enable or disable the web viewer
enabled = true

# Host address
# 127.0.0.1: localhost only
# 0.0.0.0: accessible from any network interface
host = 127.0.0.1

# Port
port = 5000

# Debug mode (development only)
debug = false

# Auto-start with bot
auto_start = false

# bcrypt password hash — generate with hash_password.py (see below)
web_viewer_password = $2b$12$...
```

---

## Password Setup

The web viewer is protected by a bcrypt-hashed password. Plain-text passwords are **not** accepted.

Generate a hash with the included helper script:

```bash
sudo /opt/meshcore-bot/venv/bin/python3 /opt/meshcore-bot/hash_password.py
```

Paste the output into `config.ini`:

```ini
[Web_Viewer]
web_viewer_password = $2b$12$...
```

Restart the bot after changing the password.

> **Tip:** `config.ini` should be readable only by the bot user. Check permissions with `ls -l config.ini` and tighten with `chmod 640 config.ini` if needed.

---

## Accessing the Viewer

Once started, open your browser and navigate to:

- **Local access**: `http://localhost:5000` (or your configured port)
- **Network access**: `http://YOUR_BOT_IP:5000` (requires `host = 0.0.0.0`)

---

## Pages Overview

### Dashboard

- Bot health status (uptime, connected clients, radio status)
- Database statistics (contacts, messages, commands)
- Top users, top channels, most popular commands
- Longest path records
- Live real-time feed of packets, commands, and messages

### Repeater Contacts

- Active repeater contacts with location info
- Device types and status indicators
- First/last seen timestamps and purge count

### Contact Tracking

- Complete history of all heard contacts
- Signal strength, hop count, and routing data
- Advertisement data and currently-tracked status

### Send Message

Send a message to one or more channels directly from the browser. Select multiple channels to send the same message to all of them (sent sequentially with automatic rate-limit spacing).

### Scheduled Messages

Manage automated messages on a fixed daily schedule or repeating interval. See [Scheduled Messages](scheduled-messages.md) for full documentation.

### Cache Data

- Geocoding cache entries
- Generic cache entries (weather, sports, etc.)
- Expiration status and cache value previews

### Config

- Categorized configuration view with left-nav topics
- Core settings: notifications, log rotation, backup, maintenance
- Database operations and database info

### Purging Log

- Audit trail of contact purging operations with timestamps and reasons

---

## Public Access via Cloudflare Tunnel

For secure remote access without opening firewall ports, use a Cloudflare tunnel as a reverse proxy. Install `cloudflared` on the bot host and run the token command provided by the Cloudflare dashboard. The tunnel handles TLS termination; the web viewer stays on `host = 127.0.0.1` and is never exposed directly.

Real client IPs are forwarded in the `CF-Connecting-IP` header, which the web viewer uses for login rate limiting.

---

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/stats` | Database statistics |
| `GET /api/health` | Bot health and connected client count |
| `GET /api/contacts` | Repeater contacts data |
| `GET /api/tracking` | Contact tracking data |
| `GET /api/scheduled-messages` | Current scheduled message entries |

Example:

```bash
curl http://localhost:5000/api/stats
```

---

## Security

- **Authentication**: bcrypt password hashing (cost factor 12) with constant-time comparison
- **Brute-force protection**: Login rate limited to 10 attempts per 5-minute window per IP; `CF-Connecting-IP` is used when behind Cloudflare
- **Session security**: Server-side sessions with a random `SECRET_KEY`; sessions expire after 8 hours of inactivity
- **Input validation**: Channel and message fields reject newline and bracket injection
- **Config file**: Store `config.ini` with `chmod 640` so only the bot user can read the password hash

---

## Database Requirements

The viewer uses the same database as the bot (`[Bot] db_path`, typically `meshcore_bot.db`). Dashboard stats (message/command counts, top users, etc.) come from the `message_stats` and `command_stats` tables. To populate these when the `stats` chat command is disabled, set `collect_stats = true` under `[Stats_Command]`.

---

## Migrating from a Separate Web Viewer Database

If you previously used a separate database (`[Web_Viewer] db_path = bot_data.db`):

1. **Stop the bot and web viewer.**

2. **Optionally migrate packet stream history:**
   ```bash
   python3 migrate_webviewer_db.py bot_data.db meshcore_bot.db
   ```

3. **Point the viewer at the main database:**
   ```ini
   [Web_Viewer]
   db_path = meshcore_bot.db
   ```

4. **Start the bot.** The viewer will now share one database with the bot.

---

## Troubleshooting

### Web viewer not accessible (SBC / Orange Pi)

1. **Confirm config** — `enabled = true`, `auto_start = true`, `host = 0.0.0.0`, correct `port`. Restart after changes.

2. **Check the process is listening:**
   ```bash
   ss -tlnp | grep 5000
   ```

3. **Inspect logs:**
   - `logs/web_viewer_stdout.log`
   - `logs/web_viewer_stderr.log`

4. **Firewall (if using ufw):**
   ```bash
   sudo ufw allow 5000/tcp && sudo ufw reload
   ```

5. **Test locally first:**
   ```bash
   curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5000/
   ```

### Flask / dependencies not found

```bash
pip3 install flask flask-socketio bcrypt
```

### Port already in use

```bash
ss -tlnp | grep 5000
# or
lsof -i :5000
```

Change the port in `config.ini` or stop the conflicting process.
