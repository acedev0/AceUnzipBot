# `aceunzip` CLI

The Ubuntu/Debian installer adds `/usr/local/bin/aceunzip`. Commands that change service state use
root privileges; invoking the command as a normal sudo-capable user escalates automatically.

## Reconfigure

```bash
aceunzip
# equivalent
aceunzip configure
```

The wizard asks for API ID, API hash, bot token, owner ID, and optional log channel. Existing
values are preserved when Enter is pressed. API hash and bot token input is masked.

Before touching the live service, the command:

1. validates formats and required binaries;
2. opens an in-memory Telegram session to verify API ID/hash and bot token;
3. stops the service only after verification succeeds;
4. synchronizes the log-channel preference and restarts systemd;
5. restores the prior environment file if validation or restart fails.

For a network-isolated maintenance window, skip only the live Telegram test:

```bash
sudo ACE_SKIP_TELEGRAM_VERIFY=1 aceunzip
```

The local format/dependency check still runs.

## Operations

```bash
aceunzip status
aceunzip logs
aceunzip restart
aceunzip doctor
aceunzip update v2.1.0
```

- `status` shows the full systemd status without paging.
- `logs` follows the journal. Bot secrets and archive passwords are never intentionally logged.
- `restart` restarts the process and prints its status.
- `doctor` validates active configuration, FFmpeg, 7-Zip, and native `cryptg` loading.
- `update [VERSION]` requires a clean Git checkout, accepts `main` or an exact semantic release
  such as `v2.1.0`, and reruns the dependency/service installer. A version tag is checked out
  exactly; ZIP installations are never silently overwritten.

To return to an older release while preserving the protected environment and data directories:

```bash
aceunzip update v2.0.1
```

## Installed paths

| Path | Purpose |
| --- | --- |
| `/opt/aceunzipbot` | Git source and virtual environment |
| `/etc/aceunzip/aceunzip.env` | Root-owned credentials, group-readable by service only |
| `/etc/aceunzip/install.conf` | Non-secret CLI installation record |
| `/var/lib/aceunzip/data` | SQLite, MTProto session, customized assets |
| `/var/lib/aceunzip/work` | Isolated temporary extraction jobs |
| `/etc/systemd/system/ace-unzip-bot.service` | Hardened unit |

Local ZIP installations use the extracted project directory and its `.env`, `data/`, and `work/`
paths, while retaining the same CLI behavior.
