# Clock sync for MeshCore repeaters

Runs beside meshcore-bot on the same Pi and companion radio. On a schedule, it
stops the bot, logs into each listed repeater as admin, sends `clock sync`,
then starts the bot again. A repeater's clock drifts and resets when it
reboots; this keeps them right.

## Parts

| Path | What it does |
|---|---|
| `clock_sync/clock_sync.py` | The run: login, `clock sync`, auto-`clkreboot` for a clock stuck ahead, re-adds repeaters meshcore-bot purged from the radio, measures how far each clock was off |
| `clock_sync/run-clock-sync.sh` | Wrapper: one run at a time (`flock`), stops/starts meshcore-bot around it |
| `clock_sync/peer_parse.py` | Reads other time-sync bots' posts on the shared channel |
| `clock_sync/schedule.py` | Days / time / skip window / pause (`/etc/clock_sync/schedule.json`) |
| `runner/` | Root timer that starts scheduled runs; path unit for the page's Run buttons |
| `web/` | Web page on :8090: edit the repeater list, schedule, Run now, results, shared-channel messages, permanent message archive |
| `watchdog/` | Restarts meshcore-bot when it stops hearing the radio for 30 min; persistent journal |

## Sharing a channel with another time-sync bot

Each result is posted to `summary_channel` as

    Name [ecf7f5]: SUCCESS (5s off)

(first 3 bytes of the repeater's key), between a start and a finish line:

    Time Sync started Wed Nov 04 04:00: 12 repeaters to check, 1 skipped (synced by PMD-TimeSync in last 24h)
    Time Sync finished Wed Nov 04 04:14: 11/12 OK, 1 NOT done, 1 skipped. Done until Fri 04:00

(`announce_runs: false` turns those two off). Bots listed in `peer_bots` are read
back from meshcore-bot's message log: a repeater one of them reported `SUCCESS`
or `OK` within the skip window is skipped on scheduled runs and Run all (never
on a single-node Run). Posts without a key (`4/11 Mt Benson Top N: OK (5s off)`)
are matched by advert name, only when exactly one repeater has that name.

## Install

In order, each with `sudo bash`:

1. `install.sh` - watchdog, persistent journal, the web page (asks for its password)
2. `install-run-button.sh` - Run now buttons
3. `install-peer.sh` - shared channel + peer skip + message archive
4. `install-offset.sh` - meshcore lib >= 2.3.17 for the clock offset
5. `install-scheduler.sh` - schedule on the page (replaces the cron line)
6. `install-announce.sh` - start / finish lines on the shared channel

Config: `/etc/clock_sync/clock_sync.yaml` - `companion`, `admin_password`,
`repeaters` (edited on the page), `summary_channel`, `peer_bots`, optional
`bot_name`, `announce_runs`. Log: `/var/log/clock_sync/clock_sync.log`.
