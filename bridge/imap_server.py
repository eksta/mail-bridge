"""Minimal single-user IMAP4rev1 server (asyncio, stdlib only).

Supports: CAPABILITY NOOP LOGIN AUTHENTICATE(PLAIN) LIST LSUB SELECT EXAMINE
STATUS UID/FETCH SEARCH STORE(close) CLOSE LOGOUT IDLE(NOOP-poll).
Enough for Thunderbird/other MUA to read mail from a bridge backend.
"""
import asyncio
import email.utils
import logging
import re
import time

from .mail_state import mail_state

CAPABILITIES = "IMAP4rev1 LITERAL+ IDLE MOVE AUTH=LOGIN AUTH=PLAIN"

LOG = logging.getLogger("bridge.imap")
CLOG = logging.getLogger("bridge.imap.cmd")

FLAGS = r"(\Answered \Flagged \Deleted \Seen \Draft)"

# Global list of active IMAP sessions for IDLE notifications
_ACTIVE_SESSIONS: list = []


def notify_folder(backend, folder: str):
    """Refresh the shared FolderView and notify subscribers.

    Dovecot-style: EXPUNGE responses are sent inline to every subscriber
    computed from the single shared list — this keeps all clients exactly
    in sync (no stale queue replay).  Works during IDLE too, like dovecot.
    """
    view = mail_state(backend).get_view(folder)
    if view is None:
        return
    added, expunge_seqs = view.refresh()
    if expunge_seqs:
        for s in list(view.subscribers):
            try:
                for seq in expunge_seqs:
                    s._send(f"* {seq} EXPUNGE")
            except Exception:  # noqa: BLE001
                pass
    if added or expunge_seqs:
        for s in list(view.subscribers):
            try:
                s._send(f"* {len(view.msgs)} EXISTS")
            except Exception:  # noqa: BLE001
                pass
        # Closed-folder nudge: EXISTS is only legal on sessions with the
        # folder selected.  TB's queued FCC check ("did my auto-filed copy
        # appear in Sent?") stays dormant until the folder is touched, so
        # if no session has Sent open the event must reach the client on
        # its other (IDLE) connections — unsolicited STATUS is how
        # closed-folder counts are refreshed and TB accepts it mid-IDLE.
        status = (f"* STATUS {_q(folder)} (MESSAGES {len(view.msgs)} "
                  f"UIDNEXT {view.uidnext} UNSEEN {view.unseen} "
                  f"UIDVALIDITY 1 RECENT 0)")
        for s in list(_ACTIVE_SESSIONS):
            try:
                if s.state != "AUTH" or s.view is view:
                    continue
                s._send(status)
            except Exception:  # noqa: BLE001
                pass


def _q(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _hidden_folder(name):
    """Yandex-internal pseudo-folders that must not appear in LIST/LSUB.

    - names with '|' (e.g. 'Drafts|template' — Yandex templates store):
      not a real message folder; contents are not servable via IMAP;
    - 'Outbox' — Yandex outgoing queue for timed sends; TB's Outbox is a
      local-folders concept and this IMAP twin only confuses clients.
    """
    n = (name or "").strip()
    return "|" in n or n.upper() == "OUTBOX"


def _uid_for(mid):
    h = 0
    for ch in str(mid):
        h = (h * 31 + ord(ch)) & 0x7FFFFFFF
    return h or 1


class ImapSession(asyncio.Protocol):
    def __init__(self, backend, username, password):
        self.backend = backend
        self.username = username
        self.password = password
        self.reader = None
        self.writer = None
        self.state = "NOT_AUTH"
        self.view = None            # shared FolderView (selected folder)
        self._pending_expunge = []  # EXPUNGE seqs queued for legal flush
        self._buf = b""
        self._tag = "*"

    # -- asyncio ------------------------------------------------------
    def connection_made(self, transport):
        self.writer = transport
        _ACTIVE_SESSIONS.append(self)
        LOG.info("IMAP connection opened (active sessions: %d)", len(_ACTIVE_SESSIONS))
        self._send(f"* OK [CAPABILITY {CAPABILITIES}] bridge IMAP ready")

    def data_received(self, data):
        CLOG.info("recv %d bytes", len(data))
        self._buf += data
        while True:
            if getattr(self, "_pending_auth", False):
                if b"\r\n" in self._buf:
                    line, self._buf = self._buf.split(b"\r\n", 1)
                    CLOG.info("C: %s", line[:300].decode("utf-8", "replace"))
                    self._continue_auth(line.decode("utf-8", "replace").strip())
                return
            if getattr(self, "_lit", None) is not None:
                need = self._lit["need"] - len(self._lit["got"])
                if len(self._buf) < need + 2:  # literal + CRLF
                    CLOG.info("literal: waiting %d more bytes "
                              "(have %d)", need + 2 - len(self._buf),
                              len(self._buf))
                    return
                self._lit["got"] += self._buf[:need]
                self._buf = self._buf[need:]
                if self._buf.startswith(b"\r\n"):
                    self._buf = self._buf[2:]
                elif self._buf[:2] == b"\r\n":
                    self._buf = self._buf[2:]
                lit, self._lit = self._lit, None
                CLOG.info("C: <literal %d bytes>", len(lit["got"]))
                self._dispatch_with_literal(lit["prefix"], lit["got"])
                continue
            if b"\r\n" not in self._buf:
                return
            line, self._buf = self._buf.split(b"\r\n", 1)
            CLOG.info("C: %s", line[:300].decode("utf-8", "replace"))
            # client literal: command ends with {N} or {N+}
            m = re.search(rb"\{(\d+)(\+)?\}$", line)
            if m:
                if not m.group(2):  # classic literal: send continuation
                    self._send("+ Ready for literal data")
                self._lit = {"need": int(m.group(1)), "got": b"",
                             "prefix": line.decode("utf-8", "replace")}
                continue
            try:
                text = line.decode("utf-8", "replace")
                self._handle_line(text)
            except Exception as e:  # noqa: BLE001
                LOG.exception("handler error")
                self._send(f"{self._tag} BAD internal error: {e}")

    def connection_lost(self, exc):
        try:
            _ACTIVE_SESSIONS.remove(self)
        except ValueError:
            pass
        if self.view is not None:
            try:
                self.view.subscribers.remove(self)
            except ValueError:
                pass
            self.view = None
        LOG.info("IMAP connection lost for %s (active sessions: %d, exc: %s)",
                 self.username, len(_ACTIVE_SESSIONS), exc)

    def _send(self, raw):
        if isinstance(raw, str):
            raw = raw.encode("utf-8") + b"\r\n"
        CLOG.info("S: %s", raw[:300].decode("utf-8", "replace").rstrip())
        self.writer.write(raw)

    # -- parser -------------------------------------------------------
    def _handle_line(self, line):
        line = line.strip()
        if not line:
            return
        # bare DONE terminates IDLE (no tag per RFC 2177)
        if line.upper() == "DONE":
            tag = getattr(self, "_idle_tag", None) or self._tag
            self._idle_tag = None
            self._send(f"{tag} OK IDLE terminated")
            return
        parts = line.split(" ", 2)
        if len(parts) < 2:
            self._send(f"{self._tag} BAD malformed")
            return
        tag, cmd = parts[0], parts[1].upper()
        rest = parts[2] if len(parts) > 2 else ""
        self._tag = tag
        handler = getattr(self, f"_cmd_{cmd.lower()}", None)
        if handler is None:
            self._send(f"{tag} BAD unknown command {cmd}")
            return
        handler(rest)
        self._flush_pending_expunge()

    # -- commands -----------------------------------------------------
    def _cmd_capability(self, rest):
        self._send("* CAPABILITY " + CAPABILITIES)
        self._send(f"{self._tag} OK CAPABILITY completed")

    def _flush_pending_expunge(self):
        """Send EXPUNGEs queued mid-command — now RFC-legal (between
        commands, sequence numbers already final)."""
        if not self._pending_expunge:
            return
        seqs, self._pending_expunge = self._pending_expunge, []
        LOG.info("flushing pending EXPUNGE: %s", seqs)
        for seq in seqs:
            self._send(f"* {seq} EXPUNGE")
        if self.view is not None:
            self._send(f"* {len(self.view.msgs)} EXISTS")

    def _cmd_noop(self, rest):
        if self.view is not None:
            self._send(f"* {len(self.view.msgs)} EXISTS")
        self._send(f"{self._tag} OK NOOP completed")

    def _cmd_check(self, rest):
        if not self._require_auth():
            return
        if self.view is not None:
            self._send(f"* {len(self.view.msgs)} EXISTS")
        self._send(f"{self._tag} OK CHECK completed")

    def _cmd_enable(self, rest):
        # CONDSTORE etc. not supported; silently accept
        self._send(f"{self._tag} OK ENABLE completed")

    def _cmd_namespace(self, rest):
        if not self._require_auth():
            return
        self._send('* NAMESPACE (("" "/")) NIL NIL')
        self._send(f"{self._tag} OK NAMESPACE completed")

    def _cmd_subscribe(self, rest):
        if not self._require_auth():
            return
        self._send(f"{self._tag} OK SUBSCRIBE completed")

    def _cmd_unsubscribe(self, rest):
        if not self._require_auth():
            return
        self._send(f"{self._tag} OK UNSUBSCRIBE completed")

    def _cmd_id(self, rest):
        self._send('* ID ("name" "bridge")')
        self._send(f"{self._tag} OK ID completed")

    def _cmd_login(self, rest):
        m = re.match(r'"?([^"\s]+)"?\s+"?([^"\s]+)"?$', rest.strip())
        if not m:
            self._send(f"{self._tag} BAD LOGIN arguments")
            return
        if m.group(1) != self.username or m.group(2) != self.password:
            self._send(f"{self._tag} NO [AUTHENTICATIONFAILED] invalid credentials")
            return
        self.state = "AUTH"
        self._send(f"{self._tag} OK LOGIN completed")

    def _cmd_authenticate(self, rest):
        if not rest.upper().startswith("PLAIN"):
            self._send(f"{self._tag} NO unsupported mechanism")
            return
        self._send("+ ")
        # next data_received line contains base64
        self._pending_auth = True

    def _continue_auth(self, line):
        import base64
        self._pending_auth = False
        try:
            raw = base64.b64decode(line)
            _authz, user, pw = raw.split(b"\x00")
            ok = user.decode() == self.username and pw.decode() == self.password
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            self.state = "AUTH"
            self._send(f"{self._tag} OK AUTHENTICATE completed")
        else:
            self._send(f"{self._tag} NO [AUTHENTICATIONFAILED] invalid credentials")

    def _cmd_logout(self, rest):
        self._send("* BYE bridge logging out")
        self._send(f"{self._tag} OK LOGOUT completed")
        try:
            self.writer.close()
        except Exception:  # noqa: BLE001
            pass

    def _require_auth(self):
        if self.state != "AUTH":
            self._send(f"{self._tag} NO not authenticated")
            return False
        return True

    def _cmd_list(self, rest):
        if not self._require_auth():
            return
        self._send('* LIST (\\HasNoChildren) "/" "INBOX"')
        for f in self.backend.folders():
            name = f["name"]
            if name.upper() == "INBOX" or _hidden_folder(name):
                continue
            self._send(f'* LIST (\\HasNoChildren) "/" {_q(name)}')
        self._send(f"{self._tag} OK LIST completed")

    def _cmd_lsub(self, rest):
        if not self._require_auth():
            return
        self._send('* LSUB () "/" "INBOX"')
        for f in self.backend.folders():
            name = f["name"]
            if name.upper() == "INBOX" or _hidden_folder(name):
                continue
            self._send(f'* LSUB () "/" {_q(name)}')
        self._send(f"{self._tag} OK LSUB completed")

    def _cmd_status(self, rest):
        if not self._require_auth():
            return
        m = re.match(r'"?([^"]+)"?\s+\((.*)\)', rest.strip())
        if not m:
            self._send(f"{self._tag} BAD STATUS arguments")
            return
        folder, items = m.group(1), m.group(2).upper()
        view = mail_state(self.backend).get_view(folder)
        if view is None:
            self._send(f"{self._tag} NO STATUS failed for {_q(folder)}")
            return
        try:
            view.refresh()
            view.last_full_refresh = time.monotonic()
        except Exception as e:
            LOG.warning("STATUS failed for %s: %s", folder, e)
            self._send(f"{self._tag} NO STATUS failed for {_q(folder)}")
            return
        vals = []
        for it in items.split():
            if it == "MESSAGES":
                vals.append(f"MESSAGES {len(view.msgs)}")
            elif it == "UNSEEN":
                vals.append(f"UNSEEN {view.unseen}")
            elif it == "UIDNEXT":
                vals.append(f"UIDNEXT {view.uidnext}")
            elif it == "UIDVALIDITY":
                vals.append(f"UIDVALIDITY 1")
            elif it == "RECENT":
                vals.append("RECENT 0")
        self._send(f"* STATUS {_q(folder)} ({' '.join(vals)})")
        self._send(f"{self._tag} OK STATUS completed")

    def _cmd_select(self, rest):
        self._cmd_select_impl(rest, "SELECT")

    def _cmd_examine(self, rest):
        self._cmd_select_impl(rest, "EXAMINE")

    def _cmd_select_impl(self, rest, kind):
        if not self._require_auth():
            return
        folder = rest.strip().strip('"')
        view = mail_state(self.backend).get_view(folder)
        if view is None:
            self._send(f"{self._tag} NO {kind} failed for {_q(folder)}")
            return
        try:
            view.refresh()
            view.last_full_refresh = time.monotonic()
        except Exception as e:
            LOG.warning("SELECT/EXAMINE failed for %s: %s", folder, e)
            self._send(f"{self._tag} NO {kind} failed for {_q(folder)}")
            return
        if self.view is not None and self.view is not view:
            try:
                self.view.subscribers.remove(self)
            except ValueError:
                pass
        self.view = view
        if self not in view.subscribers:
            view.subscribers.append(self)
        n = len(view.msgs)
        self._send(f"* {n} EXISTS")
        self._send("* 0 RECENT")
        self._send(f"* OK [UIDVALIDITY 1] UIDs valid")
        self._send(f"* OK [UIDNEXT {view.uidnext}] Predicted next UID")
        self._send(f"* FLAGS {FLAGS}")
        self._send(f"* OK [PERMANENTFLAGS {FLAGS} \\*)] Limited")
        self._send(f"{self._tag} OK [{kind == 'SELECT' and 'READ-WRITE' or 'READ-ONLY'}] {kind} completed")

    def _cmd_idle(self, rest):
        if not self._require_auth():
            return
        self._idle_tag = self._tag  # remembered for the bare DONE
        self._idle_msg_count = len(self.view.msgs) if self.view is not None else 0
        self._send("+ idling")
        # After 2 s, re-check the folder for new messages.  This covers
        # the case where a background SMTP send added a message to Sent
        # but the notify_folder() call arrived BEFORE we entered IDLE.
        try:
            loop = asyncio.get_event_loop()
            loop.call_later(2, self._idle_check_new_msgs)
        except Exception:  # noqa: BLE001
            pass

    def _idle_check_new_msgs(self):
        """Called 2 s after entering IDLE — append-only tail refresh."""
        if not getattr(self, "_idle_tag", None) or self.view is None:
            return  # not in IDLE anymore or no folder selected
        try:
            old = len(self.view.msgs)
            added, _removed = self.view.refresh(append_only=True)
            if added > 0:
                self._send(f"* {len(self.view.msgs)} EXISTS")
                LOG.info("IDLE check: %s grew %d->%d, sent EXISTS",
                         self.view.name, old, len(self.view.msgs))
        except Exception:  # noqa: BLE001
            pass

    def _cmd_done(self, rest):
        # handled in _handle_line for bare DONE; tagged form just in case
        self._idle_tag = None
        self._send(f"{self._tag} OK IDLE terminated")

    def _cmd_uid(self, rest):
        parts = rest.split(" ", 1)
        if len(parts) != 2:
            self._send(f"{self._tag} BAD UID arguments")
            return
        sub, subrest = parts[0].upper(), parts[1]
        if sub == "FETCH":
            self._fetch(subrest, uid_mode=True)
        elif sub == "SEARCH":
            self._search(subrest, uid_mode=True)
        elif sub == "STORE":
            self._cmd_store(subrest, uid_mode=True)
        elif sub == "COPY":
            self._cmd_copy(subrest, uid_mode=True)
        elif sub == "MOVE":
            self._cmd_move(subrest, uid_mode=True)
        else:
            self._send(f"{self._tag} BAD UID subcommand {sub}")

    def _cmd_copy(self, rest, uid_mode=False):
        self._copy_move(rest, uid_mode, "COPY")

    def _cmd_move(self, rest, uid_mode=False):
        self._copy_move(rest, uid_mode, "MOVE")

    def _copy_move(self, rest, uid_mode, command):
        """COPY/UID COPY and MOVE/UID MOVE (RFC 6851).

        Implemented as a server-side move: messages are relocated via the
        backend API (one batched call) and the source copies are reported
        as EXPUNGED to every subscriber.  Thunderbird deletes mail with
        UID MOVE when MOVE is advertised - one atomic command, and the
        client-side selection behaviour is the standard well-tested path.
        """
        if not self._require_auth() or self.view is None:
            self._send(f"{self._tag} NO nothing selected")
            return
        m = re.match(r'([0-9*:,]+)\s+"?([^"\s]+)"?\s*$', rest.strip())
        if not m:
            self._send(f"{self._tag} BAD {command} arguments")
            return
        seqset, folder = m.group(1), m.group(2)
        targets = self._resolve_set(seqset, uid_mode)
        dest_id = None
        for f in self.backend.folders():
            if str(f.get("name", "")).upper() == folder.upper():
                dest_id = f.get("id")
                break
        if dest_id is None:
            self._send(f"{self._tag} NO [TRYCREATE] no such mailbox: "
                       f"{_q(folder)}")
            return
        if str(dest_id) == str(self.view.fid) or not targets:
            self._send(f"{self._tag} OK {command} completed")
            return
        mids = [t["mid"] for t in targets]
        try:
            self.backend.api_copy(self.view.fid, mids, dest_id)
        except NotImplementedError:
            self._send(f"{self._tag} NO {command} not supported")
            return
        except Exception:
            LOG.exception("%s failed for mids=%s", command, mids)
            self._send(f"{self._tag} NO {command} failed")
            return
        # suppress just-moved mids from listings: the upstream listing is
        # eventually consistent and would otherwise resurrect them
        self.view.mark_moved(mids)
        # one removal pass on the shared view; the same EXPUNGE list
        # goes inline to every subscriber immediately (dovecot-style)
        seqs = self.view.remove_mids(mids)
        for s in list(self.view.subscribers):
            try:
                for seq in seqs:
                    s._send(f"* {seq} EXPUNGE")
            except Exception:  # noqa: BLE001
                pass
        self.backend.invalidate_folder_cache()
        self._send(f"{self._tag} OK {command} completed")

    def _cmd_fetch(self, rest):
        self._fetch(rest, uid_mode=False)

    def _cmd_search(self, rest):
        self._search(rest, uid_mode=False)

    def _resolve_set(self, seqset, uid_mode):
        if self.view is None:
            return []
        # After SMTP send, new messages must be visible to Thunderbird's
        # UID fetch uid:* — append-only refresh (safe mid-command: never
        # removes, so sequence numbers stay valid).
        inv = getattr(self.backend, "_cache_invalidated", False)
        if inv:
            self.backend._cache_invalidated = False
            try:
                added, _removed = self.view.refresh(append_only=True)
                if added:
                    self._send(f"* {len(self.view.msgs)} EXISTS")
            except Exception:
                pass  # keep stale list rather than crash
        # Web-deletion sync: a full (removing) refresh every 60s so
        # messages deleted via the web UI disappear from live sessions.
        # EXPUNGEs are queued and flushed after this command completes.
        now = time.monotonic()
        if now - getattr(self.view, "last_full_refresh", 0) > 60:
            self.view.last_full_refresh = now
            LOG.info("full refresh %s (throttle fired)", self.view.name)
            try:
                _added, expunge_seqs = self.view.refresh()
            except Exception:
                expunge_seqs = []
            LOG.info("full refresh %s expunged=%d", self.view.name,
                     len(expunge_seqs))
            if expunge_seqs:
                self._pending_expunge.extend(expunge_seqs)
                for s in list(self.view.subscribers):
                    if s is self:
                        continue
                    try:
                        for seq in expunge_seqs:
                            s._send(f"* {seq} EXPUNGE")
                    except Exception:  # noqa: BLE001
                        pass
        out = []
        for m in self.view.msgs:
            key = m["uid"] if uid_mode else m["seq"]
            if _seq_match(seqset, key):
                out.append(m)
        return out

    def _search(self, rest, uid_mode=False):
        if not self._require_auth() or self.view is None:
            self._send(f"{self._tag} NO nothing selected")
            return
        # Replace literal placeholder {N} with the actual text stored by
        # _dispatch_with_literal (avoids _q quoting the literal text).
        lit_val = getattr(self, "_lit_val", None)
        self._lit_val = None
        if lit_val is not None:
            rest = re.sub(r'\{(\d+)\+?\}', _q(lit_val), rest, count=1)
        crit = rest.strip()
        items = self._resolve_set("1:*", uid_mode)

        def _tok(s):
            parts = s.split(" ", 1)
            return parts[0].upper(), (parts[1].strip() if len(parts) > 1
                                      else "")

        def _unq(v):
            v = v.strip()
            if v.startswith('"') and v.endswith('"') and len(v) >= 2:
                return v[1:-1]
            return v

        def _match(item, crit):
            crit = crit.strip()
            if not crit or crit.upper() == "ALL":
                return True
            kw, tail = _tok(crit)
            env = item["env"] or {}
            snippet = (env.get("snippet") or "").lower()
            subject = (env.get("subject") or "").lower()
            frm = str(env.get("from") or "").lower()
            to = str(env.get("to") or "").lower()
            if kw == "CHARSET":
                # skip the charset name token, match the real criteria
                rest2 = tail.split(" ", 1)[1] if " " in tail else ""
                return _match(item, rest2)
            if kw == "SUBJECT":
                v = _unq(tail.split(" ", 1)[0]).lower()
                return v in subject
            if kw == "FROM":
                v = _unq(tail.split(" ", 1)[0]).lower()
                return v in frm
            if kw == "TO":
                v = _unq(tail.split(" ", 1)[0]).lower()
                return v in to
            if kw in ("BODY", "TEXT"):
                v = _unq(tail.split(" ", 1)[0]).lower()
                return (v in snippet or v in subject or v in frm or v in to)
            if kw == "SEEN":
                return "\\Seen" in item["flags"]
            if kw == "UNSEEN":
                return "\\Seen" not in item["flags"]
            if kw == "UID":
                v = _unq(tail.split(" ", 1)[0])
                return _seq_match(v, item["uid"])
            if kw == "NOT":
                inner, rem = _split_criteria(tail)
                return not _match(item, inner)
            if kw == "OR":
                c1, rem = _split_criteria(tail)
                c2, _ = _split_criteria(rem)
                return _match(item, c1) or _match(item, c2)
            if kw == "HEADER":
                # HEADER <field-name> <value>: match against the real
                # (originals-overlaid) header.  Thunderbird's FCC check
                # does UID SEARCH HEADER Message-ID "<...>" after each
                # send; with the old "unknown -> match everything"
                # fallback it returned the whole folder and the dupe
                # check never recognized the Sent copy.
                parts2 = tail.split(" ", 1)
                field = parts2[0].strip()
                # cut the value off from any following criteria
                # (e.g. HEADER Message-ID "x" SEEN) — they must not
                # pollute the substring match
                val = _unq(_split_criteria(parts2[1])[0]) \
                    if len(parts2) > 1 else ""
                try:
                    hv = str(self._load_headers(item).get(field, "") or "")
                except Exception:  # noqa: BLE001
                    return False
                return val.strip().lower() in hv.lower()
            # unknown criterion: match everything
            return True

        out = [i for i in items if _match(i, crit)]
        prefix = "UID " if uid_mode else ""
        ids = " ".join(str(i["uid"] if uid_mode else i["seq"]) for i in out)
        self._send(f"* SEARCH {ids}".rstrip())
        self._send(f"{self._tag} OK SEARCH completed")

    def _fetch(self, rest, uid_mode):
        if not self._require_auth() or self.view is None:
            self._send(f"{self._tag} NO nothing selected")
            return
        m = re.match(r'([0-9*:,]+)\s+(.*)$', rest.strip(), re.S)
        if not m:
            self._send(f"{self._tag} BAD FETCH arguments")
            return
        seqset, items = m.group(1), m.group(2)
        targets = self._resolve_set(seqset, uid_mode)
        items = items.strip()
        if items.startswith("(") and items.endswith(")"):
            items = items[1:-1]
        tokens = _split_items(items)

        for t in targets:
            header_msg = self._load_headers(t)   # no HTTP, from envelope

            def _needs_full(tok):
                tu = tok.upper()
                if tu in ("RFC822", "BODY[]", "RFC822.TEXT",
                          "BODYSTRUCTURE"):
                    return True  # structure includes attachments
                if tu in ("RFC822.SIZE", "RFC822.HEADER"):
                    return False
                if tu == "BODY":
                    return True  # bare BODY = structure w/ attachments
                m = re.match(r'(?:BODY|RFC822)(?:\.PEEK)?\[(.*)\]$', tok, re.S)
                if not m:
                    return False
                sec = m.group(1).upper()
                # content sections need the real body; HEADER* do not
                return sec in ("", "TEXT") or sec[:1].isdigit()

            full_needed = any(_needs_full(tok) for tok in tokens)
            # Also load full message when RFC822.SIZE is requested but
            # we only need headers вЂ” we need the real full-message size
            # for RFC822.SIZE to be consistent across all FETCH responses.
            has_size = any(t.upper() == "RFC822.SIZE" for t in tokens)
            if has_size and not full_needed:
                # Try body cache first (fast, no HTTP); only do HTTP if
                # the message hasn't been fetched yet.
                cached = getattr(self.backend, '_body_cache', {}).get(t["mid"])
                if cached is not None:
                    msg = cached
                else:
                    full_needed = True
                    msg = self._load_message(t) if full_needed else header_msg
            else:
                msg = self._load_message(t) if full_needed else header_msg
            rawb = _crlf(msg.as_bytes())
            header, body = _split_mime(rawb)
            parts = []          # non-literal response items
            literal = None      # bytes for the single literal item
            lit_name = None     # its announcement, sent LAST before bytes
            for tok in tokens:
                tu = tok.upper()
                if tu == "FLAGS":
                    parts.append(f"FLAGS ({' '.join(t['flags'])})")
                elif tu == "UID":
                    pass  # appended below (once, also for uid_mode)
                elif tu == "RFC822.SIZE":
                    parts.append(f"RFC822.SIZE {len(rawb)}")
                elif tu == "ENVELOPE":
                    parts.append("ENVELOPE " + _envelope(header_msg))
                elif tu == "BODYSTRUCTURE" or tu == "BODY":
                    parts.append(("BODYSTRUCTURE " if tu == "BODYSTRUCTURE"
                                  else "BODY ") + _bodystructure(msg))
                elif tu == "RFC822" or tu == "BODY[]":
                    literal = rawb
                    lit_name = f"BODY[] {{{len(rawb)}}}"
                elif tu == "RFC822.HEADER":
                    literal = header
                    lit_name = f"BODY[HEADER] {{{len(header)}}}"
                elif tu == "RFC822.TEXT":
                    literal = body
                    lit_name = f"BODY[TEXT] {{{len(body)}}}"
                elif tu.startswith("BODY") or tu.startswith("RFC822"):
                    # BODY / BODY.PEEK with [section]
                    sec_m = re.match(r'(?:BODY|RFC822)(?:\.PEEK)?\[(.*)\]$',
                                     tok, re.S)
                    if not sec_m:
                        parts.append(f"{tu} NIL")
                        continue
                    section = sec_m.group(1)
                    sec_up = section.upper()
                    if sec_up.startswith("HEADER.FIELDS.NOT"):
                        names = _field_names(section)
                        data = _filter_header(header, names, negate=True)
                        resp_sec = section
                    elif sec_up.startswith("HEADER.FIELDS"):
                        names = _field_names(section)
                        data = _filter_header(header, names)
                        resp_sec = section
                    elif sec_up == "HEADER":
                        data = header
                        resp_sec = "HEADER"
                    elif sec_up == "TEXT":
                        data = body
                        resp_sec = "TEXT"
                    elif sec_up == "":
                        # BODY.PEEK[] or BODY[] with empty section = full message
                        data = rawb
                        resp_sec = ""
                    else:
                        # dotted path: 1, 2, 1.2, 2.MIME ...
                        data = self._section_bytes(msg, t, section)
                        resp_sec = section
                    literal = _crlf(data)
                    lit_name = f"BODY[{resp_sec}] {{{len(literal)}}}"
            # RFC 3501 6.4.8: UID FETCH responses MUST include the UID;
            # non-UID FETCH must NOT include it.
            if uid_mode:
                parts.append(f"UID {t['uid']}")
            # literal announcement must be the LAST item before CRLF+bytes
            if literal is not None:
                parts.append(lit_name)
                self._send_raw(
                    (f"* {t['seq']} FETCH (" + " ".join(parts) + "\r\n").encode()
                    + literal + b")\r\n")
            else:
                self._send(f"* {t['seq']} FETCH ({' '.join(parts)})")
        self._send(f"{self._tag} OK FETCH completed")

    def _send_raw(self, blob):
        CLOG.info("S: %s", blob[:300].decode("utf-8", "replace").rstrip())
        self.writer.write(blob)

    def _section_bytes(self, msg, t, section):
        """Resolve BODY[<dotted.path>] / BODY[<path>.MIME] to bytes."""
        sec = section.strip()
        want_mime = False
        if sec.upper().endswith(".MIME"):
            want_mime = True
            sec = sec[:-5]
        part = msg
        try:
            for p in sec.split("."):
                part = list(part.iter_parts())[int(p) - 1]
        except (IndexError, ValueError, AttributeError, TypeError):
            return b""
        if want_mime or not part:
            hdrs = [f"{k}: {v}" for k, v in part.items()]
            return ("\r\n".join(hdrs) + "\r\n\r\n").encode("utf-8")
        hid = part.get("X-Bridge-Hid")
        if hid:
            cache = t.setdefault("att_cache", {})
            key = str(hid)
            if key not in cache:
                name = part.get_filename() or "attachment"
                cache[key] = self.backend.attachment(
                    self.view.fid, t["mid"], hid, name)
            return cache[key]
        try:
            return part.get_content() \
                .encode("utf-8", "replace") if isinstance(
                    part.get_content(), str) else part.get_content()
        except Exception:  # noqa: BLE001
            return b""

    def _load_headers(self, t):
        """Header-only message built from cached envelope (no HTTP)."""
        if "hdr_msg" in t:
            return t["hdr_msg"]
        from email.message import EmailMessage
        env = t.get("env") or {}
        em = EmailMessage()
        em["Subject"] = env.get("subject") or "(no subject)"
        frm = env.get("from")
        if isinstance(frm, dict):
            em["From"] = email.utils.formataddr(
                (frm.get("name") or "", frm.get("email") or ""))
        elif isinstance(frm, list) and frm:
            first = frm[0] if isinstance(frm[0], dict) else {"email": str(frm[0])}
            em["From"] = email.utils.formataddr(
                (first.get("name") or "", first.get("email") or ""))
        else:
            em["From"] = "unknown@bridge"
        to = env.get("to")
        if isinstance(to, list) and to:
            vals = []
            for x in to:
                if isinstance(x, dict):
                    vals.append(email.utils.formataddr(
                        (x.get("name") or "", x.get("email") or "")))
                else:
                    vals.append(str(x))
            em["To"] = ", ".join(vals)
        ts = env.get("date") or 0
        try:
            ts = int(str(ts)) if str(ts).isdigit() else 0
        except (ValueError, TypeError):
            ts = 0
        if ts > 10 ** 12:
            ts //= 1000
        em["Date"] = email.utils.formatdate(ts) if ts \
            else email.utils.formatdate()
        em["Message-ID"] = f"<{t['mid']}@bridge>"
        em.set_content(env.get("snippet") or " ")
        # Auto-filed Sent copies: overlay the SMTP client's original
        # headers (Message-ID etc.) so header-only fetches (RFC822.HEADER,
        # BODY[HEADER.FIELDS]) and ENVELOPE report the same values as the
        # full BODY[] — Thunderbird's FCC dupe-check matches on them.
        apply_orig = getattr(self.backend, "apply_original_headers", None)
        if apply_orig is not None:
            try:
                apply_orig(str(t["mid"]), em)
            except Exception:  # noqa: BLE001
                pass
        t["hdr_msg"] = em
        return em

    def _load_message(self, t):
        if "msg_obj" in t:
            return t["msg_obj"]
        msg = self.backend.fetch_message(self.view.fid, t["mid"])
        t["msg_obj"] = msg
        return msg

    def _cmd_store(self, rest, uid_mode=False):
        if not self._require_auth() or self.view is None:
            self._send(f"{self._tag} NO nothing selected")
            return
        m = re.match(r'([0-9*:,]+)\s+(\+|\-)?FLAGS(\.SILENT)?\s+\(([^)]*)\)', rest, re.I)
        if not m:
            self._send(f"{self._tag} BAD STORE arguments")
            return
        seqset, op, _silent, flagstr = m.groups()
        # Diagnostic: Thunderbird's FCC marks the replied-to message
        if "$Forwarded" in flagstr and op == "+":
            LOG.info("FCC $Forwarded STORE detected on %s", self.username)
        seen = "\\Seen" in flagstr
        for t in self._resolve_set(seqset, uid_mode=uid_mode):
            if seen:
                if op == "-":
                    t["flags"] = [f for f in t["flags"] if f != "\\Seen"]
                    try:
                        self.backend.api_set_seen(self.view.fid, t["mid"], False)
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    if "\\Seen" not in t["flags"]:
                        t["flags"].append("\\Seen")
                    try:
                        self.backend.api_set_seen(self.view.fid, t["mid"], True)
                    except Exception:  # noqa: BLE001
                        pass
            else:
                for flag in flagstr.split():
                    if op == "-":
                        t["flags"] = [f for f in t["flags"] if f != flag]
                    else:
                        if flag not in t["flags"]:
                            t["flags"].append(flag)
            uid_part = f" UID {t['uid']}" if uid_mode else ""
            self._send(f"* {t['seq']} FETCH (FLAGS ({' '.join(t['flags'])})"
                       f"{uid_part})")
        self._send(f"{self._tag} OK STORE completed")

    def _cmd_close(self, rest):
        # RFC 3501 6.4.2: CLOSE expunges \Deleted messages then deselects.
        # Our backend doesn't support deletion, so just deselect.
        if self.view is not None:
            try:
                self.view.subscribers.remove(self)
            except ValueError:
                pass
        self.view = None
        self._send(f"{self._tag} OK CLOSE completed")

    def _cmd_expunge(self, rest):
        self._send(f"{self._tag} OK EXPUNGE completed (no-op)")

    def _dispatch_with_literal(self, prefix_line, literal_bytes):
        """Command line ending with {N} + collected literal bytes."""
        try:
            tag, _, rest = prefix_line.partition(" ")
            self._tag = tag
            cmd = rest.split(" ", 1)[0].upper()
            LOG.info("Literal dispatch: cmd=%s, literal=%d bytes", cmd, len(literal_bytes))
            if cmd == "APPEND":
                self._append(rest, literal_bytes)
                return
            # splice literal into the command line, then handle normally
            txt = literal_bytes.decode("utf-8", "replace")
            # Store raw text for _search (no _q quoting вЂ” avoids quote mismatch)
            self._lit_val = txt
            full = re.sub(r"\s*\{\d+\+?\}\s*$", " " + _q(txt),
                          prefix_line.strip())
            self._handle_line(full)
        except Exception as e:  # noqa: BLE001
            LOG.exception("literal dispatch error")
            self._send(f"{self._tag} BAD internal error: {e}")

    def _cmd_append(self, rest):
        # without literal вЂ” TB always uses literals for APPEND
        LOG.warning("APPEND without literal received: %s", rest[:200])
        self._send(f"{self._tag} NO APPEND requires literal data")

    def _append(self, rest, raw_msg):
        """APPEND folder [(flags)] [date] {N} вЂ” literal in raw_msg."""
        if not self._require_auth():
            return
        LOG.info("APPEND request: %s", rest[:200])
        m = re.match(
            r'(?:APPEND\s+)"?([^"\s]+)"?\s*'
            r'(?:\(.*?\))?\s*(?:"[^"]*")?\s*\{',
            rest, re.I)
        if not m:
            LOG.warning("APPEND regex failed for: %s", rest[:200])
            self._send(f"{self._tag} BAD APPEND arguments")
            return
        folder = m.group(1)
        from . import rfc822 as _r
        try:
            parsed = _r.parse_outgoing(raw_msg)
        except Exception as e:  # noqa: BLE001
            self._send(f"{self._tag} NO cannot parse message: {e}")
            return
        try:
            self.backend.api_append(folder, parsed)
        except NotImplementedError:
            pass  # swallow (e.g. Sent copies вЂ” the server files them itself)
        except Exception as e:  # noqa: BLE001
            LOG.exception("APPEND failed")
            self._send(f"{self._tag} NO APPEND failed: {e}")
            return
        # Return APPENDUID so TB accepts the append and stops waiting.
        # Yandex auto-files Sent; for other folders the message may not
        # actually exist on the server but we must not block the client.
        uid = int(time.time())
        uidvalidity = 1
        LOG.info("APPEND to %s succeeded, returning APPENDUID %d %d",
                 folder, uidvalidity, uid)
        self._send(f"{self._tag} OK [APPENDUID {uidvalidity} {uid}] APPEND completed")
        # Invalidate folder cache so the next STATUS/LIST reflects reality.
        self.backend._folder_cache = None

    # pending auth continuation
    def _handle_line_post(self, line):
        pass


def _split_criteria(s):
    """Split first IMAP search criterion from the rest."""
    s = s.strip()
    if not s:
        return "", ""
    if s[0] == "(":
        depth = 0
        for i, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return s[1:i], s[i + 1:]
    if s[0] == '"':
        end = s.find('"', 1)
        return s[:end + 1], s[end + 1:]
    toks = s.split(" ", 1)
    head = toks[0]
    # keyword with argument (SUBJECT/FROM/TO/BODY/TEXT/UID/HEADER...)
    if head.upper() in ("SUBJECT", "FROM", "TO", "CC", "BCC", "BODY", "TEXT",
                        "UID", "HEADER", "KEYWORD", "UNKEYWORD"):
        rest = toks[1] if len(toks) > 1 else ""
        arg, rem = _split_criteria(rest)
        return f"{head} {arg}".strip(), rem
    return head, toks[1] if len(toks) > 1 else ""


def _crlf(raw: bytes) -> bytes:
    """EmailMessage.as_bytes uses LF; IMAP requires CRLF."""
    return re.sub(rb"(?<!\r)\n", b"\r\n", raw)


def _split_mime(rawb):
    """-> (header_bytes_incl_crlfcrlf, body_bytes)."""
    if b"\r\n\r\n" in rawb:
        i = rawb.index(b"\r\n\r\n") + 4
        return rawb[:i], rawb[i:]
    hdr = rawb.rstrip(b"\r\n") + b"\r\n\r\n"
    return hdr, b""


def _split_items(s):
    """Split FETCH item list on top-level spaces (parens/brackets aware)."""
    items, depth, cur = [], 0, []
    for ch in s.strip():
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == " " and depth <= 0:
            if cur:
                items.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        items.append("".join(cur))
    return items


def _field_names(section):
    m = re.search(r"\(([^)]*)\)", section)
    if not m:
        return []
    return [w for w in re.split(r"[\s,]+", m.group(1).strip()) if w]


def _filter_header(header, names, negate=False):
    want = {n.upper() for n in names}
    out = []
    for line in header.decode("utf-8", "replace").split("\r\n"):
        if not line or line[:1] in " \t":
            if out:
                out.append(line)
            continue
        name = line.split(":", 1)[0].strip().upper()
        keep = (name in want) if not negate else (name not in want)
        if keep:
            out.append(line)
    txt = "\r\n".join(out) + "\r\n"
    return txt.encode("utf-8")


def _nq(v):
    """Quote string or NIL."""
    if v is None or v == "":
        return "NIL"
    return _q(str(v))


def _addr_struct(header_value):
    """From/To/Cc header -> IMAP address list ((name NIL mailbox host)...)."""
    lst = email.utils.getaddresses([str(header_value or "")])
    if not lst:
        return "NIL"
    parts = []
    for name, mail in lst:
        if "@" in mail:
            mb, host = mail.rsplit("@", 1)
        else:
            mb, host = mail, ""
        parts.append(f"({_nq(name)} NIL {_nq(mb)} {_nq(host)})")
    return "(" + "".join(parts) + ")"


def _envelope(msg):
    date = msg.get("Date", "")
    subj = msg.get("Subject", "")
    from_ = _addr_struct(msg.get("From", ""))
    to = _addr_struct(msg.get("To", ""))
    msgid = _nq(msg.get("Message-ID", ""))
    inreply = _nq(msg.get("In-Reply-To"))
    return (f"({_nq(date)} {_nq(subj)} {from_} {from_} {from_} "
            f"{to} NIL NIL {inreply} {msgid})")


def _bodystructure(msg):
    """Build BODYSTRUCTURE reflecting real parts (incl. attachments)."""
    def _params(part):
        ps = (part.get_params() or [])[1:]  # drop the (ctype, '') pair
        if not ps:
            return "NIL"
        return "(" + " ".join(f'{_q(k)} {_q(v)}' for k, v in ps) + ")"

    def _one(part):
        ctype = part.get_content_type()
        main, _, sub = ctype.partition("/")
        hid = part.get("X-Bridge-Hid")
        if hid:
            try:
                size = int(part.get("X-Bridge-Size", "0"))
            except ValueError:
                size = 0
            name = part.get_filename() or "attachment"
            disp = ('("attachment" ("filename" ' + _q(name) + "))")
            return (f'("{main}" "{sub}" ("name" {_q(name)}) NIL NIL '
                    f'"base64" {size} NIL {disp})')
        try:
            payload = part.get_content()
            if isinstance(payload, str):
                data = payload.encode("utf-8")
            else:
                data = payload or b""
        except Exception:  # noqa: BLE001
            data = b""
        lines = data.count(b"\n")
        return (f'("{main}" "{sub}" {_params(part)} NIL NIL "7bit" '
                f'{len(data)} {lines})')

    def _walk(node):
        if not node.is_multipart():
            return _one(node)
        subs = " ".join(_walk(p) for p in node.iter_parts())
        ctype = node.get_content_type()
        main, _, sub = ctype.partition("/")
        return (f"({subs} \"{sub}\" (\"boundary\" \"b\") "
                f"NIL NIL NIL)")

    return _walk(msg)


def _seq_match(seqset, n):
    for part in seqset.split(","):
        if part == "*":
            return True
        if ":" in part:
            a, b = part.split(":")
            lo = int(a) if a != "*" else 1
            if b == "*":
                if n >= lo:  # '*' = largest in mailbox (unbounded)
                    return True
            else:
                hi = int(b)
                if min(lo, hi) <= n <= max(lo, hi):
                    return True
        elif part and int(part) == n:
            return True
    return False


# (old data_received monkeypatch removed вЂ” literal/auth handling is native)


class _Adapter(asyncio.Protocol):
    """Adapt asyncio.Protocol session to streams server."""

    def __init__(self, session):
        self.session = session

    def connection_made(self, transport):
        self.session.connection_made(transport)

    def data_received(self, data):
        self.session.data_received(data)

    def connection_lost(self, exc):
        self.session.connection_lost(exc)


async def start_imap_server(backend, host, port, username, password):
    loop = asyncio.get_event_loop()
    srv = await loop.create_server(
        lambda: _Adapter(ImapSession(backend, username, password)),
        host, port)
    return srv

