#!/bin/bash
# Guided installer for Linux and Raspberry Pi. From a clone of this repo, run:
#
#   ./install.sh
#
# It asks a few questions, then uses install-service.sh for the service, the
# user and Python. Running it again updates the bot and keeps your settings.
# Channels, areas and plugin settings are done on the web page afterwards.
set -euo pipefail
# Questions run in subshells, so a closed input has to stop the whole script.
shopt -s inherit_errexit
cd "$(dirname "$(readlink -f "$0")")"

INSTALL_DIR=/opt/meshcore-bot
CONFIG_FILE=/etc/meshcore-bot/config.ini
LOCAL_DIR=/var/lib/meshcore-bot/local
LOG_FILE=/var/log/meshcore-bot/meshcore_bot.log
UNIT=/etc/systemd/system/meshcore-bot.service
SERVICE_USER=meshcore
WEB_PORT=8080
START_TIMEOUT=150

say() { printf '\n\033[1m%s\033[0m\n' "$1"; }
stop() { printf '\n\033[0;31m%s\033[0m\n' "$1" >&2; exit 1; }

[ "$(uname -s)" = Linux ] || stop "This installer is for Linux and Raspberry Pi."
[ -f meshcore_bot.py ] && [ -f install-service.sh ] || stop "Run this from a copy of the meshcore-bot repo."
command -v systemctl >/dev/null || stop "This installer needs systemd."
if [ "$EUID" -ne 0 ]; then
	exec sudo bash "$(readlink -f "$0")" "$@"
fi

# --- an existing bot ---

UPDATE=false
if [ -f "$UNIT" ]; then
	# install-service.sh would move an older layout's files, so leave that to a person.
	grep -q -- "--config $CONFIG_FILE" "$UNIT" || stop "A bot is already installed here, set up a different way.
Nothing has been changed. See docs/service-installation.md to update it by hand."
	UPDATE=true
elif [ -e "$INSTALL_DIR" ]; then
	stop "$INSTALL_DIR already exists but isn't a service install.
Nothing has been changed. Move it aside or see docs/service-installation.md."
fi

# --- questions, all before anything is changed ---

ask() { # prompt, default
	local answer
	# A last line with no newline still counts; only nothing at all stops.
	if ! read -r -p "$1${2:+ [$2]}: " answer && [ -z "$answer" ]; then
		stop "Stopped. Nothing has been changed."
	fi
	printf '%s' "${answer:-$2}"
}

ask_number() { # prompt, lowest, highest
	local answer
	while true; do
		answer=$(ask "$1" "")
		# Plain shell, since Python may not be installed yet.
		if [[ "$answer" =~ ^-?[0-9]+(\.[0-9]+)?$ ]] \
			&& awk -v n="$answer" -v lo="$2" -v hi="$3" 'BEGIN { exit !(n >= lo && n <= hi) }'; then
			printf '%s' "$answer"
			return
		fi
		echo "Enter a number from $2 to $3." >&2
	done
}

ask_serial_port() {
	local ports=() port choice i
	for port in /dev/serial/by-id/* /dev/ttyUSB* /dev/ttyACM*; do
		[ -e "$port" ] && ports+=("$port")
	done
	if [ "${#ports[@]}" -eq 0 ]; then
		echo "No radio found on USB. Plug it in, or type where it will be." >&2
		ask "Serial port" /dev/ttyUSB0
		return
	fi
	echo "Serial ports found:" >&2
	for i in "${!ports[@]}"; do
		echo "  $((i + 1))) ${ports[$i]}" >&2
	done
	choice=$(ask "Port number, or type a path" 1)
	if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#ports[@]}" ]; then
		printf '%s' "${ports[$((choice - 1))]}"
	else
		printf '%s' "$choice"
	fi
}

ask_yes() { # prompt, default y or n
	local answer
	answer=$(ask "$1 (y/n)" "$2")
	[[ "$answer" =~ ^[Yy] ]]
}

if [ "$UPDATE" = false ]; then
	say "MeshCore bot setup"
	echo "Nothing changes until you've answered these."

	say "Radio"
	echo "  1) USB"
	echo "  2) Network (TCP)"
	echo "  3) Bluetooth"
	CONNECTION_CHOICE=$(ask "How is the radio connected" 1)
	case "$CONNECTION_CHOICE" in
		2)
			MB_CONNECTION=tcp
			MB_HOSTNAME=$(ask "Radio address" "")
			[ -n "$MB_HOSTNAME" ] || stop "The radio's address is needed. Nothing has been changed."
			MB_TCP_PORT=$(ask "Radio port" 5000)
			;;
		3)
			MB_CONNECTION=ble
			MB_BLE_NAME=$(ask "Radio's Bluetooth name (blank to find it automatically)" "")
			;;
		*)
			MB_CONNECTION=serial
			MB_SERIAL_PORT=$(ask_serial_port)
			;;
	esac

	say "Bot"
	MB_BOT_NAME=$(ask "Bot name" MeshCoreBot)
	echo "Location is used for weather and distances. Paste it from a map."
	MB_LATITUDE=$(ask_number "Latitude, like 48.85" -90 90)
	MB_LONGITUDE=$(ask_number "Longitude, like -123.50" -180 180)
	while true; do
		MB_CHANNEL=$(ask "Channel the bot answers on, like #mybot" "")
		MB_CHANNEL=${MB_CHANNEL#\#}
		if [ -z "$MB_CHANNEL" ]; then
			echo "Enter a channel." >&2
		elif [ "${MB_CHANNEL,,}" = public ]; then
			echo "A bot can't run on the Public channel. Pick another." >&2
		else
			MB_CHANNEL="#$MB_CHANNEL"
			break
		fi
	done

	say "Web page"
	echo "You'll use this password to sign in to the bot's settings page."
	while true; do
		read -r -s -p "Password (8 or more characters): " MB_PASSWORD || stop "Stopped. Nothing has been changed."
		echo
		read -r -s -p "Same password again: " again || stop "Stopped. Nothing has been changed."
		echo
		if [ "${#MB_PASSWORD}" -lt 8 ]; then
			echo "That's too short." >&2
		elif [ "$MB_PASSWORD" != "$again" ]; then
			echo "Those don't match." >&2
		else
			break
		fi
	done
	unset again

	say "Plugins"
	echo "These install turned off. Set them up and turn them on from the web page."
	WANT_ALERTS=false
	WANT_TIPS=false
	if ask_yes "Public alerts (Alert Ready, weather, water notices)" y; then WANT_ALERTS=true; fi
	if ask_yes "Did You Know daily tips" n; then WANT_TIPS=true; fi
fi

# --- what the installer needs ---

python3_ready() {
	python3 -c 'import sys, venv, ensurepip; raise SystemExit(sys.version_info < (3, 10))' 2>/dev/null
}
if ! python3_ready || ! command -v rsync >/dev/null; then
	command -v apt-get >/dev/null || stop "Install Python 3.10 or newer, its venv module, and rsync, then run this again."
	say "Installing Python and rsync"
	apt-get update -qq
	DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-pip rsync
	python3_ready || stop "This system's Python is too old. The bot needs 3.10 or newer."
fi

# --- the bot ---

if [ "$UPDATE" = true ]; then
	say "Updating the bot"
	# Answers install-service.sh's two questions: no profanity filter, yes geocoding.
	bash install-service.sh --upgrade <<< $'n\ny'
else
	say "Installing the bot"
	echo "This can take a while, up to an hour on a small Pi."
	bash install-service.sh <<< $'n\ny'
fi

# --- settings ---

if [ "$UPDATE" = false ]; then
	export MB_CONNECTION MB_SERIAL_PORT="${MB_SERIAL_PORT:-}" MB_HOSTNAME="${MB_HOSTNAME:-}" \
		MB_TCP_PORT="${MB_TCP_PORT:-}" MB_BLE_NAME="${MB_BLE_NAME:-}" MB_BOT_NAME MB_LATITUDE \
		MB_LONGITUDE MB_CHANNEL MB_PASSWORD
	PYTHONPATH="$INSTALL_DIR" "$INSTALL_DIR/venv/bin/python" scripts/install_settings.py "$CONFIG_FILE"
	unset MB_PASSWORD
	chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_FILE"
	chmod 600 "$CONFIG_FILE"
fi

# --- plugins ---

copy_plugin_file() { # source, path under local/
	install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 700 "$(dirname "$LOCAL_DIR/$2")"
	install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 644 "$1" "$LOCAL_DIR/$2"
}

add_settings() { # section, file
	# Installed turned off, with no channel chosen for the user.
	if ! grep -q "^\[$1\]" "$LOCAL_DIR/config.ini" 2>/dev/null; then
		{ [ -s "$LOCAL_DIR/config.ini" ] && echo; sed '0,/^enabled = true$/s//enabled = false/; s/^channel = .*/channel =/' "$2"; } >> "$LOCAL_DIR/config.ini"
		chown "$SERVICE_USER:$SERVICE_USER" "$LOCAL_DIR/config.ini"
		chmod 600 "$LOCAL_DIR/config.ini"
	fi
}

ALERTS_PLUGIN=service_plugins/public_alerts_service.py
TIPS_PLUGIN=service_plugins/did_you_know_service.py
if [ "$UPDATE" = true ]; then
	# An update refreshes the plugins already there and adds none.
	WANT_ALERTS=false
	WANT_TIPS=false
	[ -f "$LOCAL_DIR/$ALERTS_PLUGIN" ] && WANT_ALERTS=true
	[ -f "$LOCAL_DIR/$TIPS_PLUGIN" ] && WANT_TIPS=true
fi
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 700 "$LOCAL_DIR"
if [ "$WANT_ALERTS" = true ]; then
	copy_plugin_file contrib/public_alerts/public_alerts_service.py "$ALERTS_PLUGIN"
	add_settings PublicAlerts contrib/public_alerts/config.ini.publicalerts
fi
if [ "$WANT_TIPS" = true ]; then
	copy_plugin_file contrib/did_you_know/did_you_know_service.py "$TIPS_PLUGIN"
	copy_plugin_file contrib/did_you_know/dyk_command.py commands/dyk_command.py
	[ -f "$LOCAL_DIR/did_you_know_tips.txt" ] || copy_plugin_file contrib/did_you_know/did_you_know_tips.txt did_you_know_tips.txt
	add_settings DidYouKnow contrib/did_you_know/config.ini.didyouknow
fi

# --- start it and see ---

say "Starting the bot"
START_TIME=$(date '+%Y-%m-%d %H:%M:%S')
systemctl restart meshcore-bot

radio_connected() {
	[ -f "$LOG_FILE" ] && sed 's/\x1b\[[0-9;]*m//g' "$LOG_FILE" | tail -2000 \
		| awk -v since="$START_TIME" 'substr($0, 1, 19) >= since' | grep -q "Successfully fetched"
}
web_up() {
	python3 -c "import urllib.request as u; u.urlopen('http://127.0.0.1:$WEB_PORT/login', timeout=3)" 2>/dev/null
}

RADIO=false
WEB=false
for _ in $(seq 1 $((START_TIMEOUT / 5))); do
	radio_connected && RADIO=true
	web_up && WEB=true
	[ "$RADIO" = true ] && [ "$WEB" = true ] && break
	sleep 5
done

ADDRESS=$(hostname -I 2>/dev/null | awk '{print $1}')
say "Done"
if systemctl is-active --quiet meshcore-bot; then
	echo "The bot is running and will start by itself after a reboot."
else
	echo "The bot isn't running. It stops if it can't reach the radio, so plug the radio in"
	echo "or check its settings in $CONFIG_FILE, then run:"
	echo "  sudo systemctl restart meshcore-bot"
	echo "To see what happened: sudo journalctl -u meshcore-bot -n 50"
fi
if [ "$RADIO" = true ]; then
	echo "It's connected to the radio."
else
	echo "It hasn't connected to the radio yet. Check the radio is plugged in and running"
	echo "companion firmware. Connection settings are in $CONFIG_FILE."
fi
if [ "$WEB" = true ]; then
	echo "Settings page: http://${ADDRESS:-this-machine}:$WEB_PORT/plugins"
else
	echo "The settings page isn't up yet. It should appear at http://${ADDRESS:-this-machine}:$WEB_PORT/plugins"
fi
if [ "$UPDATE" = false ]; then
	echo
	echo "The bot's channel needs to exist on the radio. To turn on a plugin, open it on the"
	echo "settings page, choose its channels, tick Enabled, Save, then press Restart bot."
fi
