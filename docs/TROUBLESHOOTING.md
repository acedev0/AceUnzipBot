# Troubleshooting

## Start with the doctor

VPS installation:

```bash
aceunzip doctor
aceunzip status
aceunzip logs
```

Source or Docker workspace:

```bash
.venv/bin/python -m acebot --check
```

Diagnostics list paths and dependency status but never print the API hash or bot token.

## `MEDIA_FILE_INVALID`

The upload pipeline automatically retries a confirmed media file without its thumbnail, then as a
plain document with media-only attributes removed. If all safe forms for one extracted file fail,
that file is counted as skipped and later files continue. The same failure while copying the input
archive to a log channel falls back to sanitized text metadata.

If failures persist, update with `aceunzip update`, check free disk, and inspect the journal for the
filename and exception class.

## A zero-byte file was not delivered

Telegram cannot upload an empty document. AceUnzipBot skips each zero-byte member, counts it in the
completion card, and continues. It does not reject the containing archive.

## Bulk forwarding produces too many messages

Current releases maintain one throttled collection card and one aggregate **All N archives queued**
confirmation. Each archive creates a progress card only when its worker starts. If an older
installation sends one queue reply per ZIP, update to v2.0.0 or newer.

## FloodWait countdown

A FloodWait is Telegram instructing the client to pause. Short waits are handled internally by
Telethon. Longer waits display a countdown when a status card exists, sleep for the full server
value, and retry without consuming a normal upload attempt. Do not restart unless the timer is
clearly stuck beyond the shown duration.

## Log channel is unavailable

Logging is optional. Open `/admin → Logs`, set the bot as channel administrator, and tap **Test
channel**. A failed test disables delivery to that channel for the current process only; jobs still
run. Send `off` when prompted for a channel to disable it intentionally.

## Mirror destination is missing

Telegram bot accounts cannot list all dialogs. Send a message or command in the group/channel after
adding the bot, then tap **Refresh accessible chats**. Broadcast channels require administrator and
posting permission. Stale or non-postable entries are removed during selection.

## Service restarts repeatedly

Run `aceunzip doctor`, then inspect `aceunzip logs`. Common causes are:

- malformed API ID/hash/token;
- revoked bot token;
- missing Telegram egress/network access;
- full data/work filesystem;
- unreadable `/etc/aceunzip/aceunzip.env` after manual permission changes.

Running `aceunzip` reopens the credential wizard and restores the previous configuration if the new
one fails validation.
