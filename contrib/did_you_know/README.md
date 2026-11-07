# DidYouKnow

Posts one "Did you know?" tip a day to a channel, explaining what a bot command
does or what its reply means. Anyone can also send `dyk` for a random tip.

Install on the bot: `sudo bash contrib/did_you_know/install.sh`

Change the channel or time in `[DidYouKnow]` in `local/config.ini`, then restart
the bot. Edit tips in `local/did_you_know_tips.txt`; changes apply at the next
send. A tip can be up to 116 bytes, and its `command:` must be the command's
plugin name (`ferry`, not `ferries`; `hacker`, not `sudo`).

Tips for disabled, admin-only or channel-blocked commands are left out. If the
bot is down at send time, that day's tip is skipped.

Tests: `python -m pytest --no-cov contrib/did_you_know/test_did_you_know.py`
