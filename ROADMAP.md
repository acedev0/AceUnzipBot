# Roadmap

AceUnzipBot stays intentionally focused on safe Telegram archive extraction.

## Near term

- Broader real-world archive fixture coverage and platform deployment smoke tests.
- Export/import command for owner settings and peer registry backups.
- Optional external database/session storage profile for ephemeral platforms.
- More granular job-history retention controls.
- Evaluate a maintained MTProto migration path as the Telethon ecosystem evolves.

## Explicitly out of scope

- Remote shell, `/exec`, `/eval`, arbitrary plugins, or code execution.
- Torrent clients, copyrighted-content discovery, URL leeching, and cloud-drive credential storage.
- Telegram user-account/userbot login.
- Pretending a serverless function host can run a continuously connected worker.

Roadmap entries are intentions, not promises. Open a feature request describing the user problem,
security implications, and simplest possible solution.
