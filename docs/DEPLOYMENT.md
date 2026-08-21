# Deployment guide

AceUnzipBot is a continuously connected MTProto worker, not a web application. It needs one
long-running process, enough temporary disk for extraction, and durable storage for SQLite plus the
Telethon session.

## Platform matrix

| Target | Status | Durable data |
| --- | --- | --- |
| Ubuntu/Debian VPS | Recommended | Installed under `/var/lib/aceunzip` |
| Docker Compose | Supported | Named volume `ace-data` |
| Railway | Supported | Volume mounted at `/app/data` |
| Fly.io | Supported | Fly Volume mounted at `/data` |
| Render | Supported | Persistent disk on a background worker |
| Replit Reserved VM | Experimental | Back up before every publish |
| Heroku container | Compatibility only | No native durable filesystem |
| Vercel | Unsupported | Function runtime is not a continuous worker |

Deploy exactly one replica per bot token/data directory. Multiple replicas would contend for the
same MTProto updates, SQLite database, and job queue.

## Ubuntu / Debian VPS

Recommended baseline: Ubuntu 22.04/24.04 or current Debian, two CPU threads, 2 GB RAM, and at least
10 GB free disk. Extraction capacity should exceed the maximum configured output size plus the
incoming archive and upload staging space.

```bash
bash <(curl -fLsS --proto '=https' --tlsv1.2 https://raw.githubusercontent.com/acedev0/AceUnzipBot/main/install.sh)
```

No inbound port is required. The bot makes outbound connections to Telegram and package mirrors.
The installer creates an unprivileged service account, protected credential file, virtual
environment, hardened systemd unit, and `aceunzip` control command.

```bash
aceunzip status
aceunzip logs
aceunzip update
```

To install from a release ZIP instead, extract it, run `chmod +x install.sh setup.sh run.sh
scripts/*.sh scripts/aceunzip`, then `./install.sh`.

## Docker Compose

```bash
cp .env.example .env
# Edit the values. LOG_CHANNEL_ID may remain blank.
docker compose up -d --build
docker compose logs -f
```

The named `ace-data` volume keeps the database/session. `ace-work` is separate so operators can
inspect disk usage and remove it only while the bot is stopped. The image health check reads the
bot heartbeat.

## Railway

Railway builds the root `Dockerfile` through `railway.json`.

1. Create a project from this GitHub repository.
2. Add `API_ID`, `API_HASH`, `BOT_TOKEN`, and `OWNER_ID`; optionally add `LOG_CHANNEL_ID`.
3. Add a volume and mount it at `/app/data`.
4. Set `DATA_DIR=/app/data`, `WORK_DIR=/app/work`, and `LOG_LEVEL=INFO`.
5. Keep the service at one replica.

Railway volumes are persistent but attach to one service and constrain horizontal replication; see
the official [volume reference](https://docs.railway.com/volumes/reference) and
[Dockerfile builds](https://docs.railway.com/builds/dockerfiles).

## Fly.io

```bash
cp fly.template.toml fly.toml
# Add: app = "your-unique-app-name"
fly apps create your-unique-app-name
fly volumes create ace_data --region sin --size 10 --app your-unique-app-name
fly secrets set API_ID=... API_HASH=... BOT_TOKEN=... OWNER_ID=... --app your-unique-app-name
fly scale count 1 --app your-unique-app-name
fly deploy --app your-unique-app-name
```

Change `primary_region` and volume region together. The root filesystem is ephemeral, so the
`/data` mount is mandatory. Fly documents the distinction in its
[Volumes overview](https://fly.io/docs/volumes/overview/).

## Render

The included `render.yaml` declares a Docker background worker and a 10 GB disk at `/app/data`.
Create a Blueprint, then enter the four required secret values and optionally the log channel.
Persistent disks are available only on paid services, and the disk-backed worker must remain a
single instance. See Render's [background worker](https://render.com/docs/background-workers) and
[persistent disk](https://render.com/docs/disks) documentation.

## Replit Reserved VM

Import the repository, add the five configuration variables in Replit Secrets, and run the project.
The included `.replit`, `replit.nix`, and `scripts/replit-run.sh` install the Python environment and
system tools. For deployment choose an always-on **Reserved VM**, which Replit identifies as the
deployment type for continuously connected chat bots.

Treat this target as experimental: Replit states that a published app's filesystem is not
persistent and can reset on publish. Back up `data/`, or use a VPS/Docker platform when owner menu
settings and session continuity matter. See [Reserved VM deployments](https://docs.replit.com/cloud-services/deployments/reserved-vm-deployments)
and [filesystem troubleshooting](https://docs.replit.com/build/troubleshooting).

## Heroku compatibility

`Procfile`, `heroku.yml`, and `app.json` are included for a container worker. Heroku can run the
process, but its dyno filesystem is ephemeral: SQLite settings, the MTProto session, and customized
assets disappear after replacement/restart. This makes it a demonstration target unless you add an
external persistence design.

Heroku requires scaling the non-web process explicitly:

```bash
heroku stack:set container
heroku config:set API_ID=... API_HASH=... BOT_TOKEN=... OWNER_ID=... \
  DATA_DIR=/tmp/ace-data WORK_DIR=/tmp/ace-work
git push heroku main
heroku ps:scale worker=1
```

Read Heroku's [Procfile process model](https://devcenter.heroku.com/articles/procfile) and
[dyno filesystem behavior](https://devcenter.heroku.com/articles/dynos) before choosing it.

## Why Vercel is not supported

Vercel Functions run per request and have bounded execution duration. AceUnzipBot needs a persistent
MTProto connection and multi-minute background transfers, so a Vercel button would be misleading.
Use a VPS, container worker, or always-on VM instead. See Vercel's
[function duration](https://vercel.com/docs/functions/configuring-functions/duration) and
[platform limits](https://vercel.com/docs/limits).

## Backups

Stop the worker before taking a consistent SQLite/session backup:

```bash
sudo systemctl stop ace-unzip-bot
sudo tar -C /var/lib/aceunzip -czf aceunzip-data-backup.tar.gz data
sudo systemctl start ace-unzip-bot
```

Store backups outside the VPS and protect them like credentials: the Telethon session can act as
the bot until revoked.
