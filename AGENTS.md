# AGENTS.md — mail-bridge

Local IMAP/SMTP bridge to Yandex Mail proprietary mobile APIs.
Pure stdlib Python (no external deps besides `cryptography`). Runs on Windows.

## Commands

```
python -m bridge                    # start IMAP+SMTP servers
python -m bridge check              # verify tokens, print folders/messages
python -m unittest tests.test_bridge -v   # run tests (no pytest)
```

Tests use unittest only. There is no lint/typecheck config.

## Architecture

```
bridge/
  yandex_api.py   — Yandex mobapi client (OAuth bearer)
  http.py         — stdlib HTTPS client
  rfc822.py       — JSON <-> MIME converters (key file)
  backends.py     — MailboxBackend base + YandexBackend
  imap_server.py  — IMAP4rev1 server (asyncio Protocol)
  smtp_server.py  — SMTP server (asyncio Protocol)
  main.py         — config loading, server startup, check command
```

Entry point: `bridge/__main__.py` -> `main.py:main()`.

## Critical conventions

- **UID generation**: `uid = max(prev_uid+1, utc_timestamp_seconds)` — monotonic, unique, stable. Do NOT use hash-based UIDs; Thunderbird uses UIDNEXT as high-water mark and will show "1 message" with random UIDs.
- **SMTP send is async**: `_finish_data` uses `ensure_future` + `run_in_executor` so the event loop doesn't block during Yandex HTTP calls. If you make IMAP `_load_message` async, use the same pattern.
- **Attachment embedding**: `api_fetch_message` downloads real attachment bytes and embeds them in MIME via `base64.b64encode(data)` -> `part.set_payload()`. `BODY[]` must contain real content — Thunderbird extracts attachments from the full message, it does NOT fetch individual `BODY[n]` parts.
- **Attachment upload**: `parse_outgoing()` extracts `attachments` list (bytes). `YandexMailApi.send()` / `store_draft()` accept `attachments=[{filename, content_type, data}]`. Each uploaded via `_upload_attachment()` → `POST messages/attaches/add`, returning `att_id` for `att_ids` field.
- **Attachment parts** use custom headers `X-Bridge-Hid` and `X-Bridge-Hid` — these are read by `_bodystructure` (BODYSTRUCTURE response) and `_section_bytes` (BODY[n] fetch).
- **SEARCH charset literals**: `_dispatch_with_literal` stores raw literal in `_lit_val`; `_search` replaces `{N}` placeholder with `_q(lit_val)`. The literal is also spliced into the command line by `_dispatch_with_literal` itself.
- **`notify_on_send`**: Must be `False` in Yandex send API — `True` causes Yandex to send DSN (delivery status notification) emails back to the sender for every message.
- **Literal framing**: For `{N}` commands, server waits for exactly N bytes. For `{N+}` (LITERAL+), no continuation `+` is sent; client sends literal immediately.
- **Tests only send to the account's own address** (the `email` field from `config.json`) — never external addresses.

## Yandex attachment upload protocol (frida-verified 2026-08-28)

- **Endpoint**: `POST https://mail.yandex.ru/api/mobile/v1/upload?client=aphone&app_state=foreground&uuid=<device-hex>` — NOT `mobapi.mail.yandex.net`, NOT cloud-api Disk.
- **Body**: multipart/form-data with exactly two parts: `filename` (text part with the name) and `attachment` (binary part, Content-Type application/octet-stream).
- **Auth**: same `Authorization: OAuth <y0-token>` as mobapi.
- **Response**: `{"status":{"status":1}, "id":"YWVzX3NpZDp7...==", "hash":..., "url":...}` — `id` is an opaque base64 `aes_sid:{...}` blob (server-generated). Pass it verbatim in `v1/send` request field `att_ids: [id]`.
- **DO NOT** use `mobapi .../messages/attaches/add` — that method never existed (403 "No such method"). The Disk `attach:/` area (`cloud-api.yandex.ru`) returns `attach_not_allowed` for our OAuth client ID — the official app client only. Don't retry these.
- **Failure mode**: if upload fails, `api_send` raises — SMTP replies 550. Never send silently without requested attachments.
- Capture tooling: emulator + frida hooking `libssl.so` SSL_write/SSL_read exports — now in project `scripts/` (`hook_native.js`, `attach_hook.py`, `run_hook.py`, `hook_ssl.js`, `capture_attach.ps1`); platform `NativeCrypto.SSL_write` never fires (OkHttp uses ConscryptEngine → ENGINE_SSL_write_BIO_heap, but libssl native export hook covers everything).

## Backend body cache

- `YandexBackend._body_cache` dict keyed by mid — fetched messages cached in memory.
- **Disk persistence**: `.body_cache/<email>/<mid>.eml` with `_index.pkl`. After restart, 77/83 Sent messages cached; full Sent sync ~1.4s instead of ~30s.
- **Pre-warm on startup**: `_pewarm_cache()` fetches all Sent messages into body cache after servers start (background, non-blocking).

## IMAP server patterns

- **Web-deletion sync**: `_resolve_set` throttles (60s) a FULL view
  refresh on every poll; messages deleted via the web UI are removed
  from the shared view and `* n EXPUNGE` is queued per-session and
  flushed after the command completes (RFC-legal point), sent inline to
  other subscribers. FolderView.refresh() returns `(added,
  expunge_seqs)` — seqs are snapshotted BEFORE `_rebuild` (calling
  remove_mids after rebuild finds nothing — that was the bug).

- **`_resolve_set` invalidation**: When `_cache_invalidated` flag is set, `_resolve_set()` calls `_select_folder_state()` to re-fetch fresh message list before resolving UID/seq sets.
- **`notify_folder()`**: Sends `* {n} EXISTS` as unsolicited data to all active sessions watching *folder*. Does NOT terminate IDLE — RFC 2177 says server should NOT terminate IDLE for EXISTS.
- **IDLE lifecycle**: `_cmd_idle` records `_idle_tag`/`_idle_msg_count`, schedules `_idle_check_new_msgs` via `loop.call_later(2)`. The 2s delayed callback re-queries backend, sends EXISTS if count grew. `_cmd_done` clears `_idle_tag = None`.
- **STATUS must include RECENT**: TB requests `(UIDNEXT MESSAGES UNSEEN RECENT)` — bridge must respond with all four. EXAMINE/SELECT already send `* 0 RECENT`.
- **`_cmd_store`** handles arbitrary flags (`$Forwarded`), not just `\Seen`. UID mode (`uid_mode=True`) works with `UID STORE`.
- **`BODY.PEEK[]` returns full content**: `sec_up == ""` branch returns `rawb`.

## SMTP server patterns

- **250 sent after API call completes** (not immediately): immediate 250 races TB's FCC check — TB fetches Sent before the auto-filed copy exists, never resolves its promise, progressbar hangs forever. `_finish_data` defers 250 to `_do_send_bg` after `api_send` returns.
- **`_do_send_bg`**: after send → `invalidate_folder_cache()` → short delay → `notify_folder("Sent")`.
- **TB FCC dupe-check (critical)**: TB ≥102 searches Sent for a copy with matching Message-ID after each send; if not found it waits forever (no APPEND fallback), hanging the progressbar. The bridge remembers original client headers from SMTP DATA (`parse_outgoing` → `message_id`, `to_header`, etc.), maps them to the Yandex mid when the auto-filed copy appears (`_match_pending_send`), and `apply_original_headers` (base `MailboxBackend`, delete-then-set) rewrites the bridge-served copy so TB recognizes it. The overlay must apply to ALL serve paths: `api_fetch_message` (BODY[]), `_load_headers` (RFC822.HEADER / BODY[HEADER.FIELDS] / ENVELOPE), and `SEARCH HEADER` must match against the overlaid values (`_search._match` HEADER criterion). If any of these serve `<mid@bridge>` instead, symptom = progressbar hangs + `Processing fcc` never appears in TB's `mailnews.send` MOZ_LOG. SMTP `connection lost: None` in the log is a normal clean client close, not an error.
- **Send-window hang diagnostics (2026-09-09)**: if the letter is delivered + auto-filed but TB's send dialog hangs with NO "Processing fcc" line in the `mailnews.send` console log (enable via pref `mailnews.send.loglevel=All`, mirror to stdout with `devtools.console.stdout.chrome=true`), the FCC step died before starting — check `mail.identity.idN.fcc_folder` in prefs.js for a stale URI of a removed server (e.g. `imap://...@.yandex.ru/Sent` from a pre-bridge direct account). Point it at the bridge's `imap://<user>@127.0.0.1/Sent`. That stale pref was the real cause of multi-hour "hanging send" debugging; bridge-side FCC machinery was fine. Also: `notify_folder` pushes unsolicited `* STATUS` for the changed folder to all authenticated sessions (EXISTS only reaches sessions with that folder selected).

## IMAP APPEND

- **APPEND regex**: `r'(?:APPEND\s+)"?([^"\s]+)"?\s*(?:\(.*?\))?\s*(?:"[^"]*")?\s*\{'` — beware: a stray `=` (`="?`) makes it never match; TB sends `append "Sent" (\Seen) {337+}`. Sent APPENDs are no-ops (server auto-files) that return fake `APPENDUID`.

## Runtime

- Config: `config.json` (copy from `config.example.json`)
 - Logs: console only by default; `--log` also appends `bridge.log` (UTF-8)
- Default ports: Yandex IMAP 1143 / SMTP 1025
- All on 127.0.0.1, no TLS
- Auth is optional for localhost SMTP (bridge accepts unauthenticated sends)
