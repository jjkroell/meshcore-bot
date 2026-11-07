# Installation

Choose how to run the bot:

| Method | Best for |
|--------|----------|
| **[Guided install](#guided-install)** | A Linux PC or Raspberry Pi, answering a few questions |
| **[Docker](docker.md)** | Containers, consistent environments, easy updates |
| **[Service (systemd)](service-installation.md)** | Linux servers, run at boot, no containers |
| **Debian package** | `make deb` in the repo — see [README](https://github.com/agessaman/meshcore-bot/blob/main/README.md) |
| **[pipx](#pipx)** | A single-user install with no repo checkout and no venv management |

## Guided install

On a Linux PC or Raspberry Pi with systemd:

```bash
git clone https://github.com/jjkroell/meshcore-bot.git
cd meshcore-bot
./install.sh
```

It asks how the radio is connected, the bot's name, location and channel, a
password for the settings page, and which plugins to add. Nothing changes until
every question is answered. It then installs Python if needed, sets the bot up
as a service with [install-service.sh](service-installation.md), starts it and
prints the settings page address. Plugins install turned off: set them up and
turn them on from that page.

Run `./install.sh` again later to update. It asks nothing and keeps your
settings. It refuses to touch a bot installed some other way.

The bot stops if it can't reach the radio, so plug the radio in first.

## Requirements

- **Python 3.10+**
- MeshCore-compatible device (USB, BLE, or TCP)

## pipx

`pipx` installs the `meshcore-bot` and `meshcore-viewer` console scripts into their own
isolated environment, which sidesteps PEP 668 (`externally-managed-environment`) on
Debian 12+, Ubuntu 23.04+, Fedora and Arch without you managing a virtualenv:

```bash
pipx install "git+https://github.com/agessaman/meshcore-bot@v1.1.0"
```

Upgrade to a newer tag with `pipx install --force "git+https://github.com/agessaman/meshcore-bot@vX.Y.Z"`.

### Where your data lives

The bot has no fixed data directory. **Everything is resolved relative to the directory
containing your `config.ini`**, so that directory is effectively your install:

| What | Default | Resolved against |
|------|---------|------------------|
| Config | `config.ini` | your working directory, unless you pass `--config` |
| Database | `meshcore_bot.db` (`[Bot] db_path`) | the config file's directory |
| Local plugins | `local/` (`[Bot] local_dir_path`) | the config file's directory |

Because a bare `meshcore-bot` looks for `config.ini` in the *current* directory, running
it from somewhere else silently starts a different, empty install. **Pass an absolute
`--config` so it is unambiguous:**

```bash
mkdir -p ~/.local/share/meshcore-bot
cd ~/.local/share/meshcore-bot
# put your config.ini here, then:
meshcore-bot --config ~/.local/share/meshcore-bot/config.ini
```

The database and `local/` are then created alongside that `config.ini` no matter where
you launch from. Absolute paths in `db_path` or `local_dir_path` are used as-is.

For a systemd unit, set `WorkingDirectory=` to that directory and use the absolute
`--config` in `ExecStart=`. Note the [service installer](service-installation.md) is a
separate, self-contained path — it builds its own virtualenv under `/opt/meshcore-bot`
and does not use pipx.

## Development setup

See [Getting started](getting-started.md) for a quick development setup (run from the repo with `python meshcore_bot.py`).

## Upgrading

If you are upgrading from an older release, read the [Upgrade guide](upgrade.md) before restarting the bot.
