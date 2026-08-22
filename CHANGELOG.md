# Changelog

## 2.1.0 — 2026-08-22

- Fixed `aceunzip` reconfiguration and diagnostics from `/root` or any other working directory;
  validation now always runs from the installed source tree before touching the live service.
- Added version-pinned bootstrap and update commands. `v2.1.0`, `v2.0.1`, and future semantic
  release tags can be selected without changing repository files or losing protected settings.
- Reworked the main Telegram interface into shorter bullet-style welcome, help, privacy, bulk,
  owner, settings, progress, FloodWait, completion, and failure cards.
- Migrated untouched legacy welcome/help defaults to compact copy while preserving every
  owner-customized value.
- Added shell-level regressions for arbitrary-directory CLI execution and exact historical-tag
  selection, plus compact UI and settings-migration coverage.

## 2.0.1 — 2026-08-22

- Fixed first-time group/channel discovery during the first five minutes after a fresh VPS or CI
  runner boot. The peer cache now throttles only IDs that were actually observed previously.
- Added a deterministic low-uptime regression test and republished the corrected GitHub metadata.

## 2.0.0 — 2026-08-22

- Added a dual-mode one-line Ubuntu/Debian bootstrap installer designed for `bash <(curl ...)`,
  including dependency installation, a dedicated `aceunzip` system user, protected credentials,
  a hardened systemd service, live Telegram credential verification, and clean fast-forward updates.
- Added the global `aceunzip` control command. Running it without arguments reopens the same ASCII
  ACE credential wizard, validates new values, synchronizes the optional log preference, restarts
  the service, and restores the prior configuration if validation or restart fails.
- Made log delivery fully optional and failure-safe. Blank/disabled channels are no-ops,
  inaccessible channels are circuit-broken per process, and invalid stored archive media falls
  back to sanitized metadata without interrupting extraction.
- Added production-oriented Docker Compose, Railway, Render, Fly.io, Replit Reserved VM, and Heroku
  manifests/documentation, with explicit persistence and one-replica requirements. Vercel is
  documented as unsupported because it cannot host a continuous MTProto worker.
- Rebuilt the repository presentation with branded README artwork, a verified one-page workflow
  PDF, Mermaid architecture, CLI/deployment/troubleshooting guides, security/support/contribution
  policies, structured issue forms, pull-request template, Dependabot, matrix CI, Docker CI, and
  tag-based release packaging with SHA-256 checksums.
- Added optional-logging regressions and retained the complete queue, bulk, upload-fallback,
  zero-byte, FloodWait, mirror-routing, media, archive-safety, database, and UI test coverage.

## 1.5.0 — 2026-08-22

- Replaced per-archive bulk queue replies with one aggregate “all N archives queued” confirmation. Queued bulk reporters stay fully silent until their own worker turn starts.
- Added deferred live progress cards, so the first active stage creates exactly one card and does not perform the normal queue-card relocation/double-send.
- Suppressed bulk queue-position edit RPCs while retaining queue positions and cancel buttons for normal single-archive submissions.
- Throttled the reusable bulk collection counter during rapid forwarding, preventing 100 incoming documents from causing 100 consecutive message-edit requests.
- Preserved FIFO order, one active archive per user, `/cancel` handling, owner batch routing, per-archive log-channel copies, and visible FloodWait handling through the aggregate batch status.
- Added 100-archive stress regressions for collection throttling, FloodWait edit suppression, zero immediate queue-card sends, deferred activation, owner routing, and single dispatcher invocation; the complete suite now covers 54 cases.

## 1.4.0 — 2026-08-22

- Changed extracted-output handling so every non-empty regular file is attempted regardless of extension; only confirmed Telegram-compatible photos/videos use media mode and only confirmed streamable videos receive explicit thumbnails.
- Changed zero-byte handling to skip each empty member individually, report the count in the completion card/log, and continue sending every remaining file instead of rejecting the archive.
- Isolated upload failures per file and destination. If Telegram exhausts all media/document fallbacks for one item, the bot records a warning and continues with the rest of the archive and mirror targets.
- Expanded Auto mode to send verified JPG/PNG photos as media while keeping unknown, malformed, non-streamable, and arbitrary file types as plain documents.
- Rebuilt download, extraction, upload, queue, completion, and FloodWait cards around a compact 12-cell progress bar and short phone-width lines for a cleaner business-class Telegram UI.
- Added regression coverage for arbitrary extensions, real-vs-fake media detection, video-only thumbnails, zero-byte skipping, partial upload continuation, and compact progress rendering.

## 1.3.0 — 2026-08-21

- Added `/bulk`, `/donebulk`, and `/cancelbulk` as an explicit in-memory collection workflow with no configured document-count cap.
- Added complete paginated filename reports that preserve FIFO order, separate supported archives from ignored documents, and flag unsupported, invalid-size, and oversized inputs before queueing.
- Added one owner delivery/mirror selection for an entire accepted bulk batch; ordinary authorized users' batches remain locked to their own chat.
- Kept collection non-downloading and non-reserving until `/donebulk`, then reused the one-active-job-per-user FIFO scheduler for sequential extraction.
- Added visible FloodWait countdowns and automatic retry for downloads, uploads, mirrors, log-channel copies, queue messages, password prompts, and broadcasts. FloodWait pauses do not consume ordinary RPC retries.
- Added one-hour stale bulk-session expiry and regression tests for collection, validation, pagination, owner routing, ordering, and FloodWait retry behaviour.

## 1.2.0 — 2026-08-21

- Added a real in-memory FIFO scheduler with no configured per-user submission cap.
- Made the safe default one global worker and guaranteed that each user's archives run sequentially even if the owner later increases global concurrency.
- Added queue positions, queue-aware cancellation, queued/running owner statistics, and bottom relocation when a queued task begins.
- Changed fresh deployments to Owner-only access; one-time migration upgrades the combined v1.1 Private/2-worker baseline while preserving Public mode and its worker setting.
- Replaced manual mirror IDs/usernames with an automatic, paginated registry of known reachable users, groups, and channels learned from Telegram updates.
- Added live revalidation for users, groups, and channels; stale or non-postable channel entries are removed when selected.
- Kept multiple owner delivery prompts active so forwarding another archive never replaces the previous archive's route choice.
- Added regression tests for queue selection, default migration, automatic mirror discovery, channel permissions, pagination, and callback sizes.

## 1.1.0 — 2026-08-21

- Fixed `MEDIA_FILE_INVALID` failures with thumbnail-free and plain-document fallbacks.
- Reworked progress into one bottom-anchored message that relocates after every uploaded file.
- Made video captions use the literal extracted filename.
- Added owner-only delivery choices: owner, mirror destination, or both.
- Added saved user/group/channel destinations and broadcast-channel permission checks.
- Added fast server-side media reuse for secondary mirror destinations with re-upload fallback.
- Added automatic Telegram profile-picture setup and in-bot profile-picture replacement.
- Preserved zero-byte extracted files inside `empty-files.zip` instead of failing their upload.
- Added regression tests for upload fallbacks, progress relocation, profile images, multipart profile updates, callbacks, and mirror destination persistence.
