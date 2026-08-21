# Security policy

## Supported versions

| Version | Security fixes |
| --- | --- |
| 2.x | Yes |
| 1.x and older | No; upgrade before reporting runtime behavior |

## Operator responsibilities

- Use a dedicated, patched VPS and a dedicated bot token.
- Keep the environment file, `data/acebot.session`, and `data/acebot.sqlite3` private.
- Make the log channel private and grant access only to trusted operators.
- Tell users why archives are copied and how long the log channel retains them.
- Keep the factory-default Owner-only mode until you intentionally select Private or Public in `/admin → Access`.
- Set limits below the VPS's real disk capacity. The default 6 GB extracted limit needs substantially more than 6 GB free for safe operation.
- Never run untrusted plugins or owner-supplied shell/eval commands in the same service. This project intentionally has no `/exec`, `/shell`, or remote-code feature.

## Archive controls

The bot blocks archive traversal, absolute paths, Windows drive paths, symlinks, hard links, excessive file counts, declared expansion beyond the limit, excessive compression ratios, and actual output growth beyond the limit. The systemd unit uses an unprivileged deployment user, `NoNewPrivileges`, a read-only system/home view, a private temporary namespace, and explicit writable data/work paths.

No archive extractor can make an undersized VPS invulnerable to every malformed-format bug. Keep 7-Zip and Ubuntu security updates current, use conservative limits, and do not run the service as root when avoidable.

## Secrets and passwords

- API hash and bot token are read from the protected environment file; no owner menu exposes them.
- The VPS installer stores credentials root-owned with mode `0640`, readable only by the bot's
  service group. The `aceunzip` wizard masks secrets and rolls back invalid reconfiguration.
- Configuration diagnostics print only a redacted summary.
- Archive passwords are never written to SQLite or the log channel.
- Original archive captions are not forwarded to the log channel.
- Logging is optional. A missing/disabled channel is a no-op; an unreachable channel is disabled
  for the current process without stopping or rejecting archive jobs.
- The bot attempts to delete `/password ...` messages after reading them; Telegram may refuse deletion in some circumstances.

## Owner mirror boundaries

- Only the exact configured `OWNER_ID` receives the archive delivery router and can select a mirror destination.
- Archives submitted by ordinary users always return to their own private chat and cannot be redirected by bot admins or allowlisted users.
- The mirror menu is built from private users and group/channel peers the bot has observed through Telegram updates, plus the configured log chat and successful mirrors. It never accepts a typed ID or username.
- Every selected destination is resolved again before queueing. Broadcast channels are checked for bot administrator/posting rights.
- Saved mirror destinations contain only Telegram IDs, display labels, usernames, destination type, and last-used time. They do not contain credentials or invitation links.
- “You + mirror” reuses the media object already accepted by Telegram. If Telegram rejects that reference, the bot performs one normal upload through the same validation fallbacks.

## Queue boundaries

- There is no application-level cap on how many valid archives a user may queue. Telegram rate limits, the bot process's memory, and VPS capacity remain practical limits.
- `/bulk` holds only in-memory Telegram message references until `/donebulk`; unfinished collectors expire after one hour and are lost safely on restart.
- `/donebulk` rejects obvious unsupported, invalid-size, and oversized documents before queueing. This is deliberately a filename/size pre-check; accepted files are not trusted until 7-Zip's full content inspection runs in their worker turn.
- The factory setting is one global worker. The owner may select 1–8 workers from `/admin → Limits`, but a single user's archives always remain sequential.
- Queued archives are not downloaded or assigned work-disk reservations until a worker is available.
- A service restart marks waiting/running database records interrupted; Telegram files must be submitted again after a restart.

## Telegram rate limits

- Telethon automatically sleeps and retries waits at or below the configured 60-second threshold.
- A longer raised `FloodWaitError` is read from its documented `.seconds` value. The affected transfer/log/broadcast pauses, exposes a countdown where a status message exists, and retries after the full wait.
- FloodWait pauses do not consume the three ordinary RPC retries or force a media fallback. The configured overall task timeout still prevents an unbounded stuck job.

## Reporting

Use a [private GitHub security advisory](https://github.com/acedev0/AceUnzipBot/security/advisories/new)
or contact Ace at [Acedevmc@gmail.com](mailto:Acedevmc@gmail.com). Telegram
[@Aceddev](https://t.me/Aceddev) may be used to alert the maintainer, but do not send a working
exploit, token, session, password, or private user data in an ordinary chat.

Include the affected version, deployment method, impact, reproduction steps, and a minimal
sanitized proof. Please allow a reasonable private investigation window before public disclosure.
