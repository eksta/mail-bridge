"""Minimal SMTP server (asyncio, stdlib only).

EHLO/HELO -> AUTH LOGIN/PLAIN -> MAIL FROM -> RCPT TO -> DATA -> send via backend.
"""
import asyncio
import base64
import logging

LOG = logging.getLogger("bridge.smtp")
CLOG = logging.getLogger("bridge.smtp.cmd")


class SmtpSession(asyncio.Protocol):
    def __init__(self, backend, username, password):
        self.backend = backend
        self.username = username
        self.password = password
        self.writer = None
        self._buf = b""
        self._authed = False
        self._auth_stage = None  # 'user' | 'pass'
        self._auth_user = ""
        self._mail_from = None
        self._rcpts = []
        self._data_mode = False
        self._data = b""

    def connection_made(self, transport):
        self.writer = transport
        LOG.info("SMTP connection opened")
        self._reply(220, "bridge SMTP ready")

    def data_received(self, data):
        self._buf += data
        while b"\r\n" in self._buf:
            line, self._buf = self._buf.split(b"\r\n", 1)
            CLOG.info("C: %s", line[:300].decode("utf-8", "replace"))
            try:
                self._handle(line)
            except Exception as e:  # noqa: BLE001
                LOG.exception("handler error")
                self._reply(451, f"internal error: {e}")

    def connection_lost(self, exc):
        LOG.info("SMTP connection lost: %s", exc)

    def _reply(self, code, text):
        CLOG.info("S: %d %s", code, text)
        self.writer.write(f"{code} {text}\r\n".encode())

    def _reply_raw(self, line):
        CLOG.info("S: %s", line)
        self.writer.write((line + "\r\n").encode())

    def _handle(self, line: bytes):
        if self._data_mode:
            if line == b".":
                self._data_mode = False
                self._finish_data()
            else:
                if line.startswith(b".."):
                    line = line[1:]
                self._data += line + b"\r\n"
            return

        text = line.decode("utf-8", "replace").strip()
        upper = text.upper()

        if self._auth_stage == "user":
            try:
                self._auth_user = base64.b64decode(text).decode()
            except Exception:  # noqa: BLE001
                self._reply(501, "bad base64")
                self._auth_stage = None
                return
            self._auth_stage = "pass"
            self._reply(334, base64.b64encode(b"Password:").decode())
            return
        if self._auth_stage == "pass":
            try:
                pw = base64.b64decode(text).decode()
            except Exception:  # noqa: BLE001
                self._reply(501, "bad base64")
                self._auth_stage = None
                return
            self._auth_stage = None
            if self._auth_user == self.username and pw == self.password:
                self._authed = True
                self._reply(235, "2.7.0 Authentication successful")
            else:
                self._reply(535, "5.7.8 authentication credentials invalid")
            return

        if upper.startswith("EHLO") or upper.startswith("HELO"):
            if upper.startswith("EHLO"):
                lines = ["bridge", "AUTH LOGIN PLAIN", "8BITMIME",
                         "SMTPUTF8", "SIZE 26214400", "OK"]
                for i, ln in enumerate(lines):
                    sep = "250 " if i == len(lines) - 1 else "250-"
                    self._reply_raw(sep + ln)
            else:
                self._reply(250, "OK")
        elif upper.startswith("AUTH PLAIN"):
            arg = text[10:].strip()
            try:
                _z, user, pw = base64.b64decode(arg).split(b"\x00")
                if user.decode() == self.username and pw.decode() == self.password:
                    self._authed = True
                    self._reply(235, "2.7.0 Authentication successful")
                else:
                    self._reply(535, "5.7.8 authentication credentials invalid")
            except Exception:  # noqa: BLE001
                self._reply(535, "5.7.8 authentication failed")
        elif upper.startswith("AUTH LOGIN"):
            self._auth_stage = "user"
            self._reply(334, base64.b64encode(b"Username:").decode())
        elif upper.startswith("MAIL FROM:"):
            if not self._authed:
                # local bridge: auth optional (localhost only)
                CLOG.info("unauthenticated send allowed (local bridge)")
            self._mail_from = text[10:].strip().strip("<>").split(" ")[0]
            self._rcpts = []
            self._reply(250, "OK")
        elif upper.startswith("RCPT TO:"):
            self._rcpts.append(text[8:].strip().strip("<>").split(" ")[0])
            self._reply(250, "OK")
        elif upper == "DATA":
            if not self._rcpts:
                self._reply(554, "no valid recipients")
                return
            self._data_mode = True
            self._data = b""
            self._reply(354, "End data with <CR><LF>.<CR><LF>")
        elif upper == "NOOP":
            self._reply(250, "OK")
        elif upper == "RSET":
            self._mail_from = None
            self._rcpts = []
            self._reply(250, "OK")
        elif upper == "QUIT":
            LOG.info("SMTP QUIT received")
            self._reply(221, "bye")
            try:
                self.writer.close()
            except Exception:  # noqa: BLE001
                pass
        else:
            self._reply(502, "5.5.2 command not implemented")

    def _finish_data(self):
        data = bytes(self._data)
        rcpts = list(self._rcpts)
        mail_from = self._mail_from
        self._data = b""
        self._mail_from = None
        self._rcpts = []
        LOG.info("SMTP DATA complete, rcpts=%s, from=%s, size=%d",
                 rcpts, mail_from, len(data))
        # Do NOT send 250 yet — wait for the API call to complete so the
        # message is already filed to Sent by the time Thunderbird's FCC
        # connection opens.  Sending 250 immediately causes a race where
        # TB FETCHes Sent before the message exists, then never resolves
        # its _mimeDoFcc promise (progressbar stuck).
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            asyncio.ensure_future(self._do_send_bg(data, rcpts, mail_from))
        else:
            self._do_send_sync(data, rcpts, mail_from)

    def _do_send_sync(self, data, rcpts, mail_from):
        from . import rfc822
        try:
            parsed = rfc822.parse_outgoing(data)
            if not parsed["to"]:
                parsed["to"] = rcpts
            self.backend.api_send(parsed, mail_from or "")
            self._reply(250, "2.0.0 OK: queued via bridge")
        except Exception as e:  # noqa: BLE001
            LOG.exception("background send failed: %s", e)
            self._reply(550, f"send failed: {e}")

    async def _do_send_bg(self, data, rcpts, mail_from):
        from . import rfc822
        from .imap_server import notify_folder
        try:
            parsed = rfc822.parse_outgoing(data)
            if not parsed["to"]:
                parsed["to"] = rcpts
            LOG.info("Background send starting, to=%s", parsed["to"])
            await asyncio.get_event_loop().run_in_executor(
                None, self.backend.api_send, parsed, mail_from or "")
            LOG.info("Background send API call completed")
            # NOW send 250 — message is sent and will be filed to Sent
            # by Yandex.  TB's FCC will find it in Sent immediately.
            self._reply(250, "2.0.0 OK: queued via bridge")
            # Invalidate meta cache so IMAP Sent re-fetches from API
            self.backend.invalidate_folder_cache()
            # Short delay for Yandex to file to Sent, then notify
            await asyncio.sleep(1)
            LOG.info("Sending notify_folder(Sent) EXISTS notification")
            notify_folder(self.backend, "Sent")
        except Exception as e:  # noqa: BLE001
            LOG.exception("background send failed: %s", e)
            try:
                self._reply(550, f"send failed: {e}")
            except Exception:  # noqa: BLE001
                pass


async def start_smtp_server(backend, host, port, username, password):
    loop = asyncio.get_event_loop()
    srv = await loop.create_server(
        lambda: SmtpSession(backend, username, password), host, port)
    return srv
