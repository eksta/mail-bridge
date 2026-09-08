"""Backend abstraction shared by IMAP/SMTP servers."""
import asyncio
import email as email_mod
import logging
import os
import pickle
import time

from . import rfc822


class MailboxBackend:
    """One bridged account. Implemented by YandexBackend."""

    name = "account"
    display = "Account"

    def __init__(self):
        self._folder_cache = None
        self._folder_ts = 0

    def invalidate_folder_cache(self):
        """Force the next folders() call to re-query the API."""
        self._folder_cache = None
        self._folder_ts = 0

    def pre_refresh(self, folder_id, total):
        """Hook before FolderView.refresh() listing.

        `total` is the server-side folder count (may be None).  Backends
        with accumulating caches use it to prune entries deleted outside
        the bridge (web UI, phone).
        """

    # --- to implement ------------------------------------------------
    def api_folders(self):
        raise NotImplementedError

    def api_fetch_page(self, folder_id, offset, limit):
        """Return list of dicts {mid, subject, from, to, date, unread, raw}."""
        raise NotImplementedError

    def api_fetch_message(self, folder_id, mid):
        """Return EmailMessage for one message."""
        raise NotImplementedError

    def api_set_seen(self, folder_id, mid, seen):
        raise NotImplementedError

    def api_send(self, parsed, envelope_from):
        raise NotImplementedError

    def api_append(self, folder, parsed):
        """Store an appended message (draft). May raise NotImplementedError."""
        raise NotImplementedError

    def api_copy(self, folder_id, mids, dest_folder_id):
        """Move messages (list of mids) to another folder.

        May raise NotImplementedError.
        """
        raise NotImplementedError

    # --- shared -------------------------------------------------------
    # SMTP client header -> original-header dict key (FCC support)
    ORIGINAL_HEADER_KEYS = (
        ("Message-ID", "message_id"), ("To", "to_header"),
        ("Cc", "cc_header"), ("Date", "date_header"),
        ("In-Reply-To", "in_reply_to"), ("References", "references"),
    )

    def original_headers(self, mid):
        """Original client headers for an auto-filed Sent copy (or None).

        Backends that remember SMTP send headers keep them in
        self._sent_originals[mid].
        """
        return getattr(self, "_sent_originals", {}).get(str(mid))

    def apply_original_headers(self, mid, msg):
        """Rewrite bridge-generated headers with the client's originals.

        Returns True when originals were applied.
        """
        orig = self.original_headers(mid)
        if not orig:
            return False
        for hdr, key in self.ORIGINAL_HEADER_KEYS:
            val = orig.get(key)
            if not val:
                continue
            if msg.get(hdr) is not None:
                del msg[hdr]  # EmailMessage: set after delete, else duplicate
            msg[hdr] = val
        return True

    def folders(self, refresh=False):
        now = time.time()
        if refresh or not self._folder_cache or now - self._folder_ts > 120:
            try:
                self._folder_cache = self.api_folders()
                self._folder_ts = now
            except Exception:  # noqa: BLE001
                import logging
                logging.getLogger("bridge").warning(
                    "folders() failed, using stale cache", exc_info=True)
                if not self._folder_cache:
                    self._folder_cache = [
                        {"id": "1", "name": "INBOX", "type": 1,
                         "unread": 0, "total": 0},
                        {"id": "2", "name": "Sent", "type": 2,
                         "unread": 0, "total": 0},
                    ]
        return self._folder_cache

    def list_messages(self, folder_id, limit=50):
        return self.api_fetch_page(folder_id, 0, limit)

    def list_messages_all(self, folder_id, cap=2000, total=None):
        """All messages in folder, paged (100 per request), up to cap."""
        page = 100
        out = []
        seen = set()
        offset = 0
        target = min(cap, total) if (total and total > 0) else cap
        while offset < target:
            batch = self.api_fetch_page(folder_id, offset, page)
            if not batch:
                break
            fresh = 0
            for it in batch:
                if it["mid"] not in seen:
                    seen.add(it["mid"])
                    out.append(it)
                    fresh += 1
            if fresh == 0:
                break  # page fully duplicated — nothing more to load
            offset += len(batch)
        return out

    def fetch_message(self, folder_id, mid):
        return self.api_fetch_message(folder_id, mid)


class YandexBackend(MailboxBackend):
    def __init__(self, api):
        super().__init__()
        self.api = api
        self.display = f"Yandex {api.email or ''}"
        self._meta_cache = {}  # fid -> {mid: meta}
        self._body_cache = {}  # mid -> EmailMessage
        self._cache_invalidated = False
        # FCC support: Thunderbird (>=102) searches Sent for a copy with
        # matching Message-ID after each send; if it can't recognize the
        # auto-filed copy it never completes onStopCopy and the send
        # progressbar hangs.  Remember the original headers from SMTP
        # DATA and restore them on the bridge-served Sent copy.
        self._pending_send = None          # last SMTP send, awaiting mid
        self._sent_originals = {}          # mid -> original header dict
        self._seen_mids = set()            # mids already seen in listing
        self._full_loaded = set()          # fids whose full listing was walked
        self._cache_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            ".body_cache", api.email or "unknown")
        self._load_body_cache()

    def api_folders(self):
        return self.api.folders()

    def _cache(self, folder_id):
        return self._meta_cache.setdefault(str(folder_id), {})

    def invalidate_folder_cache(self):
        """Mark listings stale so shared views re-query the API.

        Deliberately does NOT clear _meta_cache: it is an accumulator of
        everything we have ever listed (pagination depends on it), and
        api_fetch_page always re-queries the API anyway.
        """
        self._cache_invalidated = True

    def pre_refresh(self, folder_id, total):
        """Prune meta-cache entries deleted outside the bridge.

        The newest page is delta-refreshed anyway; if the server-side
        folder total diverges from the accumulator size, something was
        removed via web/phone — rebuild the cache with a fresh walk.
        """
        cache = self._cache(folder_id)
        if total is None:
            return
        try:
            self.api_fetch_page(folder_id, 0, 100)
        except Exception:  # noqa: BLE001
            return
        import logging
        logging.getLogger("bridge").info(
            "pre_refresh fid=%s total=%s cache=%s", folder_id, total,
            len(cache))
        # only ">": folders larger than the walk cap legitimately keep
        # cache < total, that must not trigger endless rebuilds
        if len(cache) > int(total):
            self._meta_cache[str(folder_id)] = {}
            self._full_loaded.discard(str(folder_id))

    def _load_body_cache(self):
        idx_path = os.path.join(self._cache_dir, "_index.pkl")
        if not os.path.exists(idx_path):
            return
        try:
            with open(idx_path, "rb") as f:
                index = pickle.load(f)
            for mid, fname in index.items():
                fpath = os.path.join(self._cache_dir, fname)
                if os.path.exists(fpath):
                    with open(fpath, "rb") as f:
                        raw = f.read()
                    self._body_cache[str(mid)] = email_mod.message_from_bytes(raw)
        except Exception:
            pass

    def _save_body_cache(self, mid, msg):
        try:
            os.makedirs(self._cache_dir, exist_ok=True)
            idx_path = os.path.join(self._cache_dir, "_index.pkl")
            index = {}
            if os.path.exists(idx_path):
                with open(idx_path, "rb") as f:
                    index = pickle.load(f)
            fname = f"{mid}.eml"
            fpath = os.path.join(self._cache_dir, fname)
            with open(fpath, "wb") as f:
                f.write(msg.as_bytes())
            index[str(mid)] = fname
            with open(idx_path, "wb") as f:
                pickle.dump(index, f)
        except Exception:
            pass

    def api_fetch_page(self, folder_id, offset, limit):
        # Yandex window is 0-based, last-exclusive: [first, last)
        metas = self.api.messages(folder_id, first=offset,
                                  last=offset + limit)
        cache = self._cache(folder_id)
        for meta in metas:
            mid = meta.get("mid")
            if mid is not None:
                cache[str(mid)] = meta
        self._match_pending_send(folder_id, metas)
        return self._format_page(folder_id, offset, limit)

    def _format_page(self, folder_id, offset, limit):
        cache = self._cache(folder_id)
        ordered = sorted(cache.values(),
                         key=lambda m: int(m.get("mid", 0) or 0),
                         reverse=True)
        out = []
        for meta in ordered[offset:offset + limit]:
            mid = meta.get("mid")
            status = meta.get("status") or []
            if not isinstance(status, list):
                status = [status]
            out.append({
                "mid": str(mid),
                "subject": (meta.get("subjPrefix") or "") + (meta.get("subjText") or ""),
                "from": meta.get("from") or [],
                "to": meta.get("recipients") or [],
                "date": meta.get("utc_timestamp") or 0,
                "unread": 1 in status,
                "snippet": meta.get("firstLine") or "",
                "has_attach": bool(meta.get("hasAttach")),
                "raw": meta,
            })
        return out

    def list_messages_all(self, folder_id, cap=2000, total=None):
        """All messages in folder; serves from meta cache once walked.

        First SELECT walks every page (up to cap / folder total) so IMAP
        sees the whole mailbox, not just the newest 100.  Later calls are
        served from the accumulated meta cache without API traffic.
        """
        cache = self._cache(folder_id)
        if str(folder_id) in self._full_loaded:
            return self._format_page(folder_id, 0, cap)
        target = min(cap, total) if (total and total > 0) else cap
        while len(cache) < target:
            offset = len(cache)
            self.api_fetch_page(folder_id, offset, 100)
            if len(cache) <= offset:
                break  # no progress — end of folder reached
        self._full_loaded.add(str(folder_id))
        return self._format_page(folder_id, 0, cap)

    def _match_pending_send(self, folder_id, metas):
        """Associate the latest SMTP send with its auto-filed Sent copy.

        The first time a mid shows up in the Sent listing we check whether
        a recent SMTP send is still unassigned; if so, remember the
        original client headers for that mid.
        """
        try:
            sent_fid = next(
                (str(f["id"]) for f in self.folders()
                 if str(f.get("name", "")).upper()
                 in ("SENT", "ОТПРАВЛЕННЫЕ")),
                None)
        except Exception:  # noqa: BLE001
            sent_fid = None
        if sent_fid is None or str(folder_id) != sent_fid:
            return
        for meta in metas:
            mid = meta.get("mid")
            if mid is None:
                continue
            key = str(mid)
            if key in self._seen_mids:
                continue
            self._seen_mids.add(key)
            pend = self._pending_send
            if pend is not None and time.time() - pend["ts"] < 600:
                self._sent_originals[key] = pend
                self._pending_send = None
                logging.getLogger("bridge").info(
                    "FCC: mapped sent copy mid=%s to original Message-ID %s",
                    key, pend.get("message_id", ""))
        if len(self._seen_mids) > 5000:  # bound memory
            self._seen_mids = set(list(self._seen_mids)[-2000:])

    def api_fetch_message(self, folder_id, mid):
        cached = self._body_cache.get(str(mid))
        if cached is not None:
            return cached
        meta = self._cache(folder_id).get(str(mid), {})
        if not meta:  # not in cache (e.g. after restart) — fetch page
            self.api_fetch_page(folder_id, 0, 100)
            meta = self._cache(folder_id).get(str(mid), {"mid": mid})
        bodies = self.api.message_body(mid)
        body_json = {}
        for b in bodies:
            if str(b.get("mid", "")) == str(mid) or len(bodies) == 1:
                body_json = b
                break
        msg = rfc822.yandex_envelope_to_message(
            meta, body_json, self.api.email or "")
        # Restore the client's original headers (Message-ID etc.) so
        # Thunderbird's Sent-folder dupe check recognizes the copy.
        self.apply_original_headers(mid, msg)
        # Replace placeholder b"" attachment content with real bytes
        # so BODY[] (full message fetch) contains actual attachment data.
        import base64
        if msg.is_multipart():
            for part in msg.iter_parts():
                hid = part.get("X-Bridge-Hid")
                if hid:
                    try:
                        name = part.get_filename() or "attachment"
                        data = self.api.download_attachment(mid, hid, name)
                        # CTE is already base64 from add_attachment();
                        # just replace the empty payload with encoded data.
                        part.set_payload(base64.b64encode(data).decode("ascii"))
                    except Exception:
                        pass  # keep placeholder on failure
        self._body_cache[str(mid)] = msg
        self._save_body_cache(mid, msg)
        return msg

    def attachment(self, folder_id, mid, hid, name):
        return self.api.download_attachment(mid, hid, name)

    def api_append(self, folder, parsed):
        if folder.upper() in ("DRAFTS", "ЧЕРНОВИКИ"):
            return self.api.store_draft(
                to=parsed["to"], subject=parsed["subject"],
                html=parsed["html"], text=parsed["text"],
                cc=parsed["cc"], bcc=parsed["bcc"],
                attachments=parsed.get("attachments"))
        # Sent copies: the server files sent mail automatically;
        # others (Templates etc.) — swallow to keep the client happy
        return None

    def api_set_seen(self, folder_id, mid, seen):
        self.api.mark_read(mid, read=seen)

    def api_copy(self, folder_id, mids, dest_folder_id):
        # mobapi move_to_folder accepts multiple mids in one call
        return self.api.move_to_folder([str(m) for m in mids],
                                       dest_folder_id, folder_id)

    def api_send(self, parsed, envelope_from):
        # Remember the client's original headers for the Sent auto-copy
        if parsed.get("message_id"):
            self._pending_send = {
                "message_id": parsed["message_id"],
                "to_header": parsed.get("to_header", ""),
                "cc_header": parsed.get("cc_header", ""),
                "date_header": parsed.get("date_header", ""),
                "in_reply_to": parsed.get("in_reply_to", ""),
                "references": parsed.get("references", ""),
                "ts": time.time(),
            }
        return self.api.send(
            to=parsed["to"], subject=parsed["subject"], html=parsed["html"],
            cc=parsed["cc"], bcc=parsed["bcc"], text=parsed["text"],
            from_name="", attachments=parsed.get("attachments"))


def run_sync(coro):
    """Run a coroutine from sync context (helper for CLI checks)."""
    return asyncio.run(coro)
