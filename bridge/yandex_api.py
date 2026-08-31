"""Yandex Mail mobile API client (mobapi), reverse-engineered from
ru.yandex.mail 9.27.0 (classes3.dex, com.yandex.mail.network.*).

Protocol summary:
  base:   https://mobapi.mail.yandex.net/api/mobile/{v1,v2}/
  auth:   Authorization: OAuth <token>
  UA:     mail2-v123612_productionCommonStoreRelease

  GET  settings                     -> account settings (email, aliases)
  GET  xlist                        -> folders + labels
  POST messages       {"requests":[{fid,first,last,md5,...}]}  -> envelopes
  POST message_body   form: mids=a,b;c  -> bodies
  POST mark_read/mark_unread  form: mids=...
  POST move_to_folder form: mids,fid,current_folder
  POST delete_items   form: mids,current_folder
  POST generate_operation_id -> operation id for send
  POST send           JSON MailSendRequest
  POST store          JSON (draft)
"""
import re
import ssl
import uuid

from . import http

HOST = "mobapi.mail.yandex.net"
V1 = "/api/mobile/v1/"
V2 = "/api/mobile/v2/"
UA = "mail2-v123612_productionCommonStoreRelease"

FOLDER_TYPE = {
    1: "INBOX", 2: "Sent", 3: "Trash", 4: "Spam", 5: "Drafts",
    6: "Archive", 500: "Outgoing",
}


class YandexMailApi:
    def __init__(self, token, email=None):
        self.token = token
        self.email = email
        self._md5s = {}  # fid -> last known md5 (delta-sync token)
        self._compose_check = None
        self.uuid = uuid.uuid4().hex  # stable per-session device uuid
        self._headers = {
            "Authorization": f"OAuth {token}",
            "User-Agent": UA,
            "X-Yandex-HTTP-Timeout": "30000",
        }

    # -- low level ---------------------------------------------------
    def _get(self, path, params=None):
        return http.request_json("GET", HOST, path, params=params,
                                 headers=self._headers)

    def _post_json(self, path, body, params=None):
        return http.request_json("POST", HOST, path, params=params,
                                 headers=self._headers, json_body=body)

    def _post_form(self, path, form, params=None):
        return http.request_json("POST", HOST, path, params=params,
                                 headers=self._headers, form=form)

    # -- public ------------------------------------------------------
    def whoami(self):
        """GET settings -> dict with user emails; used to verify token."""
        data = self._get(V1 + "settings")
        return data

    def folders(self):
        """Return normalized folder list [{fid,name,type,unread,total}].

        Real xlist format: JSON array; first element = status header
        (has "status"/"md5"), remaining = folder objects with
        fid/display_name/count_unread/count_all/type.
        Also caches per-folder md5 (xlist[0].md5) for delta sync.
        """
        data = self._get(V1 + "xlist")
        items = data if isinstance(data, list) else data.get("body", [data])
        out = []
        common_md5 = ""
        for f in items:
            if not isinstance(f, dict):
                continue
            if "fid" not in f:
                common_md5 = f.get("md5", "")
                continue
            name = (f.get("display_name") or f.get("name")
                    or str(f.get("fid")))
            name = _canon_folder_name(name)
            fid = str(f.get("fid"))
            if common_md5:
                self._md5s[fid] = common_md5
            out.append({
                "id": fid,
                "name": name,
                "type": f.get("type"),
                "unread": f.get("count_unread", 0),
                "total": f.get("count_all", 0),
            })
        return out
        if not out:
            out = [
                {"id": "1", "name": "INBOX", "type": 1, "unread": 0, "total": 0},
                {"id": "4", "name": "Sent", "type": 3, "unread": 0, "total": 0},
                {"id": "3", "name": "Trash", "type": 2, "unread": 0, "total": 0},
                {"id": "6", "name": "Spam", "type": 6, "unread": 0, "total": 0},
                {"id": "7", "name": "Drafts", "type": 7, "unread": 0, "total": 0},
            ]
        return out

    def messages(self, fid, first=1, last=25):
        """POST messages -> envelope list.

        returnIfModified=True with md5="" always returns the full slice
        (verified empirically); false returns metadata only.
        """
        body = {"requests": [{
            "fid": str(fid),
            "first": int(first),
            "last": int(last),
            "md5": "",
            "recipientsCount": 0,
            "returnIfModified": True,
            "threaded": False,
            "unread": False,
        }]}
        data = self._post_json(V1 + "messages", body)
        items = []
        arr = data if isinstance(data, list) else data.get("body", data.get("messages", []))
        for chunk in arr:
            if not isinstance(chunk, dict):
                continue
            batch = chunk.get("messageBatch") or chunk.get("messages") or {}
            items.extend(batch.get("messages") or [])
        return items

    def message_body(self, mids):
        """POST message_body -> list of bodies."""
        if isinstance(mids, (str, int)):
            mids = [mids]
        form = [("mids", ",".join(str(m) for m in mids))]
        data = self._post_form(V1 + "message_body", form,
                               params={"novdirect": "yes"})
        arr = data if isinstance(data, list) else data.get("body", [])
        return arr if isinstance(arr, list) else []

    def attach_url(self, mid, hid, name):
        """GET attach -> signed CDN url."""
        data = self._get(V1 + "attach", params=[
            ("mid", str(mid)), ("hid", str(hid)), ("name", name)])
        return data.get("url") or ""

    def download_attachment(self, mid, hid, name="attachment"):
        """attach -> signed url -> raw bytes (auth header required)."""
        url = self.attach_url(mid, hid, name)
        if not url:
            raise RuntimeError(f"no attach url for hid={hid}")
        import http.client
        import urllib.parse
        from .http import _ssl_ctx
        p = urllib.parse.urlparse(url)
        conn = http.client.HTTPSConnection(
            p.netloc, timeout=120, context=_ssl_ctx)
        try:
            conn.request("GET", p.path + ("?" + p.query if p.query else ""),
                         headers=self._headers)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status != 200:
                raise RuntimeError(f"attach download HTTP {resp.status}")
            return raw
        finally:
            conn.close()

    def mark_read(self, mids, read=True):
        method = "mark_read" if read else "mark_unread"
        if isinstance(mids, (str, int)):
            mids = [mids]
        return self._post_form(V1 + method,
                               [("mids", ",".join(str(m) for m in mids))])

    def move_to_folder(self, mids, fid, current_folder=1):
        return self._post_form(V1 + "move_to_folder", [
            ("mids", ",".join(str(m) for m in mids)),
            ("fid", str(fid)),
            ("current_folder", str(current_folder)),
        ])

    def delete(self, mids, current_folder=1):
        return self._post_form(V1 + "delete_items", [
            ("mids", ",".join(str(m) for m in mids)),
            ("current_folder", str(current_folder)),
        ])

    def _operation_id(self):
        try:
            data = self._post_json(V2 + "generate_operation_id", {})
            return (data.get("operation_id")
                    or data.get("body", {}).get("operation_id"))
        except Exception:
            return None

    def compose_check(self):
        """Anti-abuse token from v1/settings (account-information)."""
        if not self._compose_check:
            data = self._get(V1 + "settings")
            info = data.get("account_information", {}) \
                .get("account-information", {})
            self._compose_check = info.get("compose-check") or ""
        return self._compose_check

    def send(self, to, subject, html, cc=None, bcc=None, text=None,
             from_name="", reply_message_id=None, forward_message_id=None,
             attachments=None):
        """POST send (RetrofitComposeApi.sendMail / MailSendRequest).

        attachments: list of dicts [{filename, content_type, data (bytes)}]
        """
        op = self._operation_id() or uuid.uuid4().hex
        att_ids = []
        for att in (attachments or []):
            try:
                aid = self._upload_attachment(att["data"],
                                              att.get("filename", "file"),
                                              att.get("content_type",
                                                      "application/octet-stream"),
                                              operation_id=op)
                if aid:
                    att_ids.append(aid)
            except Exception:
                pass
        import logging
        logging.getLogger("bridge.yandex").info(
            "send: att_ids=%s op=%s", att_ids, op)
        if attachments and not att_ids:
            # Never send silently without the attachments the user asked
            # to include — fail loudly instead.
            logging.getLogger("bridge.yandex").warning(
                "send aborted: %d attachment(s) could not be uploaded",
                len(attachments))
            raise RuntimeError(
                "attachment upload failed (attach area is not accessible "
                "with this token)")
        req = {
            "compose_check": self.compose_check() or "1",
            "subj": subject or "",
            "to": _addr_list(to),
            "cc": _addr_list(cc),
            "bcc": _addr_list(bcc),
            "ttype": "html",
            "from_name": from_name or "",
            "from_mailbox": self.email or "",
            "send": html or (text or ""),
            "references": "",
            "inreplyto": "",
            "draft_base": "",
            "disk_att": None,
            "parts": None,
            "reply": str(reply_message_id) if reply_message_id else "",
            "forward": str(forward_message_id) if forward_message_id else "",
            "template_base": "",
            "att_ids": att_ids,
            "attaches_count": len(att_ids),
            "operation_id": op,
            "notify_on_send": False,
            "send_time": None,
            "send_type": None,
        }
        data = self._post_json(V1 + "send", req)
        # status.status == 1 -> OK; raise otherwise
        st = data.get("status", {})
        code = st.get("status", 0) if isinstance(st, dict) else st
        if code != 1:
            raise RuntimeError(f"yandex send failed: {data}")
        if data.get("captcha"):
            raise RuntimeError("yandex requested captcha — "
                               "send from web/app once, then retry")
        return data

    def _upload_attachment(self, data, filename, content_type,
                           operation_id=None):
        """POST mail.yandex.ru/api/mobile/v1/upload (multipart) -> att_id.

        Endpoint reverse-engineered from the official app (frida capture):
        multipart fields: filename (text) + attachment (raw bytes).
        Response JSON: {status, id, hash, content_type, url} where id is
        the opaque att_id passed to v1/send att_ids.
        """
        import http.client
        import json
        import logging
        import urllib.parse
        log = logging.getLogger("bridge.yandex")
        boundary = uuid.uuid4().hex
        p1 = (f"--{boundary}\r\n"
              f'Content-Disposition: form-data; name="filename"\r\n'
              f"Content-Transfer-Encoding: binary\r\n"
              f"Content-Type: text/plain; charset=UTF-8\r\n"
              f"Content-Length: {len(filename.encode('utf-8'))}\r\n\r\n"
              f"{filename}\r\n").encode()
        p2 = (f"--{boundary}\r\n"
              f'Content-Disposition: form-data; name="attachment"; '
              f'filename="{filename}"\r\n'
              f"Content-Type: application/octet-stream\r\n"
              f"Content-Length: {len(data)}\r\n\r\n").encode() + data + \
             f"\r\n--{boundary}--\r\n".encode()
        body = p1 + p2
        conn = http.client.HTTPSConnection("mail.yandex.ru", timeout=120,
                                           context=ssl.create_default_context())
        try:
            path = (f"/api/mobile/v1/upload?client=aphone"
                    f"&app_state=foreground"
                    f"&uuid={urllib.parse.quote(self.uuid or '')}")
            conn.request("POST", path, body=body, headers={
                **self._headers,
                "Accept-Encoding": "gzip",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            })
            resp = conn.getresponse()
            raw = resp.read()
            if resp.getheader("Content-Encoding") == "gzip":
                import gzip
                raw = gzip.decompress(raw)
            raw = raw.decode("utf-8", "replace")
            log.info("upload %s -> HTTP %s: %s", filename, resp.status,
                     raw[:300])
            if resp.status >= 400:
                return None
            result = json.loads(raw)
            st = result.get("status", {})
            code = st.get("status", 0) if isinstance(st, dict) else st
            if code != 1:
                log.warning("upload status != 1: %s", raw[:200])
                return None
            att_id = result.get("id")
            if not att_id:
                log.warning("upload response missing id: %s", raw[:200])
                return None
            return att_id
        except Exception as e:
            log.warning("upload failed for %s: %s", filename, e)
            return None
        finally:
            conn.close()

    def store_draft(self, to, subject, html, text=None, cc=None, bcc=None,
                    attachments=None):
        """POST store — save a draft (RetrofitComposeApi.store)."""
        op = self._operation_id() or uuid.uuid4().hex
        att_ids = []
        for att in (attachments or []):
            try:
                aid = self._upload_attachment(att["data"],
                                              att.get("filename", "file"),
                                              att.get("content_type",
                                                      "application/octet-stream"),
                                              operation_id=op)
                if aid:
                    att_ids.append(aid)
            except Exception:
                pass
        req = {
            "compose_check": self.compose_check() or "1",
            "subj": subject or "",
            "to": _addr_list(to),
            "cc": _addr_list(cc),
            "bcc": _addr_list(bcc),
            "ttype": "html",
            "from_name": "",
            "from_mailbox": self.email or "",
            "send": html or (text or ""),
            "references": "", "inreplyto": "", "draft_base": "",
            "disk_att": None, "parts": None,
            "reply": "", "forward": "", "template_base": "",
            "att_ids": att_ids, "attaches_count": len(att_ids),
            "operation_id": op,
            "notify_on_send": False, "send_time": None, "send_type": None,
        }
        return self._post_json(V1 + "store", req)

    # -- backend helpers ----------------------------------------------
    def folder_id_by_imap_name(self, imap_name):
        for f in self.folders():
            if f["name"].upper() == imap_name.upper():
                return f["id"]
        return None


def _addr_list(v):
    if not v:
        return ""
    if isinstance(v, (list, tuple)):
        return ",".join(str(x) for x in v)
    return str(v)


_CANON = {
    "INBOX": "INBOX", "ВХОДЯЩИЕ": "INBOX",
    "SENT": "Sent", "ОТПРАВЛЕННЫЕ": "Sent",
    "TRASH": "Trash", "КОРЗИНА": "Trash", "УДАЛЁННЫЕ": "Trash",
    "SPAM": "Spam", "СПАМ": "Spam",
    "DRAFTS": "Drafts", "ЧЕРНОВИКИ": "Drafts",
    "ARCHIVE": "Archive", "АРХИВ": "Archive",
}


def _canon_folder_name(name):
    return _CANON.get((name or "").strip().upper(), name)
