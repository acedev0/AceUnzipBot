# Research notes and design decisions

Research date: 21–22 August 2026.

This implementation is original. The projects below were reviewed for product patterns and reported weaknesses; their code was not copied.

## Open-source and deployment review for v2.0

- [EDM115/unzip-bot](https://github.com/EDM115/unzip-bot) provides a strong public project surface
  with release history, Docker/Heroku files, runtime configuration, contributor information, and
  extensive feature documentation. Ace adopts the discoverability pattern, not its source or broad
  remote-execution surface.
- [mirror-leech-telegram-bot](https://github.com/anasty17/mirror-leech-telegram-bot) demonstrates
  mature queue, bulk, Docker, and runtime configuration expectations at a much larger scale. Ace
  remains intentionally narrower: no torrent, rclone, URL leech, shell, eval, or userbot features.
- [remote-terminal-bot](https://github.com/AndreaBonn/remote-terminal-bot) demonstrates how
  architecture diagrams, explicit service hardening, and automated installation make operational
  projects easier to audit. Ace uses original systemd/CLI code and does not adopt remote terminal
  behavior.
- GitHub's [community profile guidance](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/about-community-profiles-for-public-repositories)
  drove the addition of contribution, conduct, support, security, issue, and pull-request files.
  GitHub's [Dependabot configuration guidance](https://docs.github.com/en/code-security/dependabot/dependabot-version-updates/configuration-options-for-the-dependabot.yml-file)
  drove explicit weekly pip, Docker, and Actions update checks.
- The requested `curl` installer is dual-mode: the same reviewed `install.sh` bootstraps a clean
  `/opt/aceunzipbot` Git checkout when streamed, or installs an extracted release locally. It stops
  on dirty Git state, uses only fast-forward updates, keeps secrets outside the repository, and
  verifies new Telegram credentials before restarting the live service.

Platform claims were limited to what each runtime can actually sustain:

- Railway recognizes root Dockerfiles and offers persistent volumes, but one volume attaches to
  one service and constrains replication. Ace therefore documents one replica and `/app/data`.
- Fly.io root filesystems are ephemeral while Fly Volumes are durable local storage, so the
  template mounts `ace_data` at `/data`.
- Render background workers can run continuously, but persistent disks require a paid service and
  a single disk-backed instance.
- Replit Reserved VM deployments are suitable for always-on chat bots, but published filesystems
  reset during publication; the target is labeled experimental and requires backups.
- Heroku supports a `worker` Procfile/container process, but dyno filesystems are ephemeral. Its
  manifests are compatibility aids, not a durable production recommendation.
- Vercel Functions have bounded request execution. A continuously connected MTProto listener and
  long file transfers do not fit that model, so no misleading Vercel deploy button was added.

The exact operational references are linked from [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Platform findings

- [Telethon 1.44 documentation](https://docs.telethon.dev/en/stable/) documents asynchronous MTProto bots, download/upload progress callbacks, custom document thumbnails, video attributes, and `supports_streaming` for Telegram-compatible MP4 media. The v1 GitHub repository is now read-only, so the project pins the June 2026 stable release instead of tracking an unbounded branch.
- Telethon's [performance FAQ](https://docs.telethon.dev/en/stable/quick-references/faq.html#file-download-is-slow-or-sending-files-takes-too-long) recommends `cryptg` because it replaces slow pure-Python Telegram encryption with native routines. Ace Extractor pins current `cryptg` for Python 3.11+ and a compatible build for Ubuntu 22.04's Python 3.10.
- [Pyrogram's GitHub repository](https://github.com/pyrogram/pyrogram) was archived on 23 December 2024 and says the project is no longer maintained. This project instead pins Telethon's newer June 2026 stable release and records the fact that its v1 repository is now read-only, rather than pretending either dependency has no lifecycle risk.
- Telegram's [Bot API file documentation](https://core.telegram.org/bots/api#sending-files) documents 320×320 JPEG thumbnails below 200 KB and a 2000 MB upload ceiling when running the local Bot API server. Ace Extractor stays below 1.95 GB per MTProto bot upload and splits larger output files.
- [Telethon's Bot API vs MTProto explanation](https://docs.telethon.dev/en/stable/concepts/botapi-vs-mtproto.html) notes that direct MTProto avoids the HTTP Bot API intermediary and requires the operator's API ID/hash.

## Existing bot review

- [EDM115/unzip-bot](https://github.com/EDM115/unzip-bot) demonstrates demand for passwords, thumbnails, media/document modes, logs, broadcasts, bans, and runtime stats.
- Its public [v7 roadmap](https://github.com/EDM115/unzip-bot/issues/296) also documents recurring problems worth designing out: non-working cancellation, stale temporary archives, path escaping, high cyclomatic complexity, fragmented settings, weak archive browsing, and whitelist needs.
- [TorToolkit-Telegram](https://github.com/yash-dk/TorToolkit-Telegram) shows the value of a user settings menu, persistent thumbnails, archive limits, overall progress, and startup alerts, but also illustrates how quickly an archive bot becomes operationally heavy when it requires PostgreSQL, rclone, torrent clients, and many unrelated downloaders.
- [partiallywritten/Unzipper-Bot](https://github.com/partiallywritten/Unzipper-Bot) and [IDN-Unzip-Bot](https://github.com/IDN-C-X/IDN-Unzip-Bot) include common archive/password/log/admin patterns but depend on a separate MongoDB deployment and older Pyrogram-era architecture.

## Resulting choices

- Pinned stable Telethon 1.44 + `cryptg` + `uvloop`, rather than building on the older archived Pyrogram API.
- SQLite/WAL, not MongoDB/PostgreSQL, for a portable single-VPS deployment.
- One coherent `/admin` control centre instead of numerous undocumented admin commands.
- Real cancellation propagated into Telegram transfer callbacks and the 7-Zip subprocess.
- Preflight and live archive limits, resolved-path validation, and link rejection.
- No `/eval`, `/exec`, URL downloader, torrent engine, or userbot login. These expand the attack surface and are unrelated to a safe archive extractor.
- Transparent archive-copy disclosure and sanitized log captions so a password caption is not leaked.
- Generated Ace artwork packaged locally, so `/start` has no third-party image dependency.

## v1.1 failure and mirror research

The `MEDIA_FILE_INVALID` report was treated as a Telegram media-form rejection, not an extraction failure:

- Telethon's [`send_file`](https://docs.telethon.dev/en/stable/modules/client.html) documentation confirms that `force_document`, `thumb`, `attributes`, `supports_streaming`, and progress callbacks are independent upload controls, and that Telegram recognizes only some formats such as MP4 as streamable.
- Telethon's [RPC error guide](https://docs.telethon.dev/en/stable/concepts/errors.html) explains that unknown request-specific failures may surface only as a generic `BadRequestError`/`RPCError`. The public issue [#4597](https://github.com/LonamiWebs/Telethon/issues/4597) also demonstrates inconsistent custom-thumbnail behavior around video uploads.
- The repair therefore uses deterministic degradation: requested media with thumbnail, the same media without thumbnail, then `application/octet-stream` as a plain document with video attributes removed. A raw Telegram exception is no longer the user-facing result.
- Telegram's [file documentation](https://core.telegram.org/bots/api#sending-files) recommends reusing a server-side file identifier and states that thumbnails cannot be reused. Owner mirroring follows the same principle through the existing MTProto media reference, omitting the thumbnail on copies and re-uploading only if that reference is rejected.
- Telegram documents that bots can delete their own recent outgoing messages in private chats, groups, and channels where they can post. Since editing a message does not change its chronological position, the progress UI sends one fresh status below each completed file and deletes the previous status; this maintains one visible progress card rather than accumulating messages.
- Bot API 9.6 added [`setMyProfilePhoto`](https://core.telegram.org/bots/api#setmyprofilephoto) and the JPEG-only [`InputProfilePhotoStatic`](https://core.telegram.org/bots/api#inputprofilephotostatic). The bundled PFP is now normalized to a square JPEG and applied automatically at first startup, while custom owner uploads are applied immediately.
- Telegram's [`KeyboardButtonRequestChat`](https://core.telegram.org/bots/api#keyboardbuttonrequestchat) can help users share a chat ID but explicitly warns that the bot may still lack access. Ace instead resolves and remembers destinations from IDs/usernames and separately verifies broadcast-channel admin rights before a job starts.

The mirror workflow was also compared with feature-heavy leech/mirror projects such as [mirror-leech-telegram-bot](https://github.com/anasty17/mirror-leech-telegram-bot) and [LeechBot](https://github.com/Shineii86/LeechBot). Ace intentionally keeps only Telegram destination routing: no torrent clients, cloud-drive credentials, remote downloaders, or shell execution are added.

## v1.2 queue and destination research

- Telegram's [`messages.getDialogs`](https://core.telegram.org/method/messages.getDialogs) method is explicitly user-only. A bot-token deployment cannot truthfully enumerate every chat it belongs to, even though Telethon exposes `get_dialogs` as a general client helper. The implementation therefore does not call that prohibited method or claim that it can.
- Telegram's [Bots FAQ](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get) documents which private, group, and channel messages bots receive. Ace records private users, every group/channel delivered through message or membership-action updates, the configured log chat, and successful mirror targets. If a bot is added to a previously quiet chat, one message or command makes the peer appear automatically; no ID is typed.
- Every picker entry is re-resolved when tapped. Groups must still contain the bot, while a broadcast channel is accepted only if current permissions show administrator and posting rights. The bot itself, the owner shortcut, bot accounts, left/banned chats, stale peers, and non-postable channels are excluded or removed.
- Telegram's native [chat-selection buttons](https://core.telegram.org/bots/features#chat-and-user-selection) can return a user-selected ID but explicitly do not guarantee that the bot can access it. The registry plus live revalidation provides a faster repeatable menu without pretending selection alone grants access.
- The picker is paginated at eight destinations per screen and all callback payloads are regression-tested below Telegram's 64-byte limit. A 60-second cache makes repeated owner batch routing fast; **Refresh** immediately reloads the latest registry.
- The scheduler holds archive message references without downloading them, so a batch does not reserve maximum extraction disk for every waiting archive. A single global FIFO worker is the safe default. If the owner raises concurrency, the earliest eligible job starts, but an active user's next job is skipped until that user's current one ends; this preserves per-user ordering while allowing other users to use free workers.
- No configured queue-length cap was added. This meets the batch-forwarding requirement without claiming infinite physical capacity: Telegram flood controls, RAM, and VPS storage remain operational constraints.

## v1.3 explicit bulk and FloodWait research

- Telethon 1.44's [RPC error guide](https://docs.telethon.dev/en/stable/concepts/errors.html) documents `FloodWaitError.seconds` as Telegram's required pause and states that waits below the client threshold are automatically slept and retried. Its [client reference](https://docs.telethon.dev/en/stable/modules/client.html) confirms that the default 60-second threshold is inclusive.
- Ace keeps that safe 60-second automatic threshold. When Telegram raises a longer wait, the download, upload, media-reference mirror, original-archive log copy, queue-card send, or broadcast is paused for the full `.seconds` value. A status card shows a countdown at a conservative ten-second cadence, then the exact operation retries. FloodWait does not consume an ordinary RPC attempt and does not incorrectly trigger `MEDIA_FILE_INVALID` fallback logic.
- `/bulk` is a collection transaction, not a command that immediately starts every forwarded document. Incoming documents are retained only as in-memory Telegram message references. `/donebulk` preserves arrival order, performs the cheap extension/size filter, publishes every accepted and ignored filename over as many Telegram-safe pages as needed, and only then commits accepted files to the existing FIFO.
- Actual format trust is deferred to the existing 7-Zip technical listing. This avoids downloading obvious unsupported files merely to identify them while still rejecting renamed/corrupt candidates safely when their worker turn begins.
- Owner bulk routing reuses the verified destination picker but applies one selection to the full accepted set. This prevents dozens of per-file mirror prompts while preserving the owner-only redirection boundary.
