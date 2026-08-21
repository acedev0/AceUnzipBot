# GitHub release checklist

Use this once for the first public release.

## Repository settings

- Repository: `acedev0/AceUnzipBot` with default branch `main`.
- Description: `Fast, secure Telegram archive extraction with FIFO bulk mode, live progress,
  thumbnails, owner controls, and one-command VPS deployment.`
- Website: `https://t.me/Aceddev`
- Topics: `telegram-bot`, `telegram`, `telethon`, `unzip`, `archive`, `python`, `ffmpeg`, `docker`,
  `systemd`, `self-hosted`.
- Upload `assets/start_banner.png` as the social preview.
- Enable Issues, Discussions if desired, private vulnerability reporting, Dependabot alerts, and
  Actions read/write permission needed by the tag release workflow.

## Protection

- Protect `main` from force pushes and deletion.
- Require the `CI` and `Docker` checks before merging.
- Require pull requests for outside contributions.
- Keep secret scanning and push protection enabled when available.

## First release

1. Confirm no `.env`, session, database, archive, or runtime work file is tracked.
2. Push the reviewed source to `main`.
3. Verify both Actions workflows pass.
4. Run the one-line installer against a clean Ubuntu VPS.
5. Create and push tag `v2.0.0`.
6. Confirm the release workflow attaches `AceUnzipBot-v2.0.0.zip` and its SHA-256 file.
7. Test the public raw installer URL from the README.

If the GitHub username or repository name differs, update every `acedev0/AceUnzipBot` URL before
publishing so badges, release links, the installer, security advisory link, and deployment buttons
all point to the exact repository.
