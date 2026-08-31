"""Canonical mailbox state shared by all IMAP sessions of a backend.

One FolderView per folder holds the authoritative message list (mids,
uids, flags, envelopes).  Sessions subscribe to a view; when the view
changes (copy/move, background refresh) each subscriber gets the same
EXPUNGE sequence numbers queued and flushes them at RFC-legal points
(NOOP / CHECK / right after IDLE DONE).  EXISTS updates are allowed
anytime, so they are sent immediately to IDLE subscribers.
"""
import logging
import threading
import time

LOG = logging.getLogger("bridge.state")

# How long to keep just-moved mids out of listings: the upstream API is
# eventually consistent and may still return them for a few seconds after
# a server-side move — without suppression they "come back" client-side.
MOVED_TTL = 120


def _ts(m):
    try:
        v = int(str(m.get("date") or 0))
        return v // 1000 if v > 10 ** 12 else v
    except (ValueError, TypeError):
        return 0


class FolderView:
    """Authoritative, shared message list of one folder."""

    def __init__(self, backend, fid, name):
        self.backend = backend
        self.fid = fid
        self.name = name
        self.msgs = []       # [{mid, uid, flags, env, seq}] oldest first
        self.uidnext = 1
        self.unseen = 0
        self.subscribers = []  # ImapSessions currently selected on us
        self._moved_recently = {}  # mid -> monotonic time of move
        self._lock = threading.RLock()

    # -- move suppression ------------------------------------------------
    def mark_moved(self, mids):
        """Suppress mids from listings right after a server-side move."""
        now = time.monotonic()
        with self._lock:
            for mid in mids:
                self._moved_recently[mid] = now
            self._moved_recently = {
                k: v for k, v in self._moved_recently.items()
                if now - v < MOVED_TTL}

    def _suppressed(self, mid):
        t = self._moved_recently.get(mid)
        return t is not None and (time.monotonic() - t) < MOVED_TTL

    # -- population ----------------------------------------------------
    def refresh(self, append_only=False):
        """Re-fetch listing from backend.

        append_only=True (safe mid-command): only append newly arrived
        messages at the tail — never removes, so no EXPUNGE desync.
        Returns (added, removed_mids).
        """
        old = list(self.msgs)
        try:
            if append_only:
                page = self.backend.list_messages(self.fid, limit=100)
                known = {x["mid"] for x in self.msgs}
                fresh = []
                for m in page:
                    if m.get("mid") not in known:
                        fresh.append(m)
                if fresh:
                    with self._lock:
                        self._append_sorted(fresh)
                return len(fresh), []
            total = None
            for f in self.backend.folders(refresh=True):
                if str(f.get("id")) == str(self.fid):
                    total = f.get("total")
                    break
            pre = getattr(self.backend, "pre_refresh", None)
            if callable(pre):
                try:
                    pre(self.fid, total)
                except Exception:  # noqa: BLE001
                    LOG.warning("pre_refresh %s failed", self.name,
                                exc_info=True)
            msgs = self.backend.list_messages(self.fid, limit=100)
            if len(msgs) >= 100:
                msgs = self.backend.list_messages_all(self.fid,
                                                      cap=2000,
                                                      total=total)
        except Exception:
            LOG.warning("refresh %s failed", self.name, exc_info=True)
            return 0, []
        # snapshot seqs BEFORE rebuild: removed messages are already
        # gone from the rebuilt list, so their EXPUNGE seqs must be
        # taken from the pre-refresh state
        old_seqs = {x["mid"]: x["seq"] for x in old}
        self._rebuild(msgs)
        new_mids = {x["mid"] for x in self.msgs}
        removed_mids = [x["mid"] for x in old if x["mid"] not in new_mids]
        expunge_seqs = sorted((old_seqs[m] for m in removed_mids
                               if m in old_seqs), reverse=True)
        added = max(0, len(self.msgs) - (len(old) - len(removed_mids)))
        return added, expunge_seqs

    def _append_sorted(self, metas):
        """Append metas with ts >= tail ts at the end (ascending)."""
        for m in sorted(metas, key=_ts):
            mid = m.get("mid")
            if any(x["mid"] == mid for x in self.msgs):
                continue
            if self._suppressed(mid):
                continue
            prev = self.msgs[-1]["uid"] if self.msgs else 0
            uid = max(prev + 1, _ts(m))
            flags = [] if m.get("unread") else ["\\Seen"]
            self.msgs.append({"mid": mid, "uid": uid,
                              "flags": flags, "env": m,
                              "seq": len(self.msgs) + 1})
        self.uidnext = (self.msgs[-1]["uid"] + 1) if self.msgs else 1
        self.unseen = sum(1 for x in self.msgs if "\\Seen" not in x["flags"])

    def _rebuild(self, metas):
        """Replace the message list, preserving locally applied flags."""
        with self._lock:
            metas = [m for m in metas if not self._suppressed(m.get("mid"))]
            old_flags = {x["mid"]: x["flags"] for x in self.msgs}

            def key(m):
                return (_ts(m), str(m.get("mid", "")))

            msgs = []
            prev = 0
            for m in sorted(metas, key=key):
                uid = max(prev + 1, _ts(m))
                prev = uid
                flags = old_flags.get(m["mid"]) if m["mid"] in old_flags \
                    else ([] if m.get("unread") else ["\\Seen"])
                msgs.append({"mid": m["mid"], "uid": uid, "flags": flags,
                             "env": m, "seq": len(msgs) + 1})
            self.msgs = msgs
            self.uidnext = prev + 1
            self.unseen = sum(1 for x in msgs if "\\Seen" not in x["flags"])

    # -- mutations ------------------------------------------------------
    def remove_mids(self, mids):
        """Remove messages by mid; return their pre-removal seq numbers.

        Returned list is descending — safe to replay as EXPUNGE responses
        without renumbering surprises.
        """
        seqs = []
        with self._lock:
            for mid in mids:
                for idx, x in enumerate(self.msgs):
                    if x["mid"] == mid:
                        seqs.append(idx + 1)
                        del self.msgs[idx]
                        break
            for i, x in enumerate(self.msgs):
                x["seq"] = i + 1
            self.unseen = sum(1 for x in self.msgs
                              if "\\Seen" not in x["flags"])
        return sorted(seqs, reverse=True)

    def set_flags(self, mid, flags):
        with self._lock:
            for x in self.msgs:
                if x["mid"] == mid:
                    x["flags"] = list(flags)
                    break
            self.unseen = sum(1 for x in self.msgs
                              if "\\Seen" not in x["flags"])


class MailboxState:
    """Per-backend holder of FolderViews + folder name resolution."""

    def __init__(self, backend):
        self.backend = backend
        self.views = {}  # fid -> FolderView
        self._lock = threading.RLock()

    def resolve(self, folder_name):
        """Case-insensitive folder name -> (fid, canonical name)."""
        for f in self.backend.folders():
            if str(f.get("name", "")).upper() == str(folder_name).upper():
                return f.get("id"), f.get("name") or folder_name
        return None, None

    def view_for(self, fid, name=None):
        with self._lock:
            v = self.views.get(str(fid))
            if v is None:
                v = FolderView(self.backend, fid, name or str(fid))
                self.views[str(fid)] = v
            return v

    def get_view(self, folder_name):
        fid, name = self.resolve(folder_name)
        if fid is None:
            return None
        return self.view_for(fid, name)


def mail_state(backend):
    """Lazy singleton MailboxState attached to a backend instance."""
    st = getattr(backend, "_mail_state", None)
    if st is None:
        st = MailboxState(backend)
        try:
            backend._mail_state = st
        except Exception:  # read-only backend objects (tests)
            pass
    return st
