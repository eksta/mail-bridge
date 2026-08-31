"""End-to-end smoke: real TCP IMAP+SMTP servers with a fake backend."""
import asyncio
import base64
import sys

sys.path.insert(0, ".")

from bridge.backends import MailboxBackend
from bridge.imap_server import start_imap_server
from bridge.smtp_server import start_smtp_server


class FakeBackend(MailboxBackend):
    def api_folders(self):
        return [{"id": "1", "name": "INBOX", "type": 1, "unread": 1, "total": 1}]

    def api_fetch_page(self, folder_id, offset, limit):
        return [{"mid": "m1", "subject": "hello", "from": [{"email": "a@b.c"}],
                 "to": [], "date": 1, "unread": True, "snippet": "sn"}]

    def api_fetch_message(self, folder_id, mid):
        from email.message import EmailMessage
        m = EmailMessage()
        m["Subject"] = "hello"
        m["From"] = "a@b.c"
        m.set_content("smoke body")
        return m

    def api_set_seen(self, folder_id, mid, seen):
        pass

    def api_send(self, parsed, envelope_from):
        print("SEND CALLED:", parsed["subject"], "->", parsed["to"])
        return {"status": 200}


async def main():
    imap = await start_imap_server(FakeBackend(), "127.0.0.1", 13143, "u", "p")
    smtp = await start_smtp_server(FakeBackend(), "127.0.0.1", 13025, "u", "p")

    # IMAP conversation
    r, w = await asyncio.open_connection("127.0.0.1", 13143)
    async def cmd(c):
        w.write(c.encode() + b"\r\n")
        await asyncio.sleep(0.05)
        try:
            return await asyncio.wait_for(r.read(65536), 0.5)
        except asyncio.TimeoutError:
            return b""
    print((await cmd("x1 LOGIN u p")).decode())
    print((await cmd("x2 LIST \"\" *")).decode())
    print((await cmd("x3 SELECT INBOX")).decode())
    resp = await cmd("x4 FETCH 1 (BODY[])")
    assert b"smoke body" in resp, resp
    print("IMAP BODY ok")
    await cmd("x5 LOGOUT")
    w.close()

    # SMTP conversation
    r, w = await asyncio.open_connection("127.0.0.1", 13025)
    async def cmd2(c):
        w.write(c if isinstance(c, bytes) else c.encode() + b"\r\n")
        await asyncio.sleep(0.05)
        try:
            return await asyncio.wait_for(r.read(65536), 0.5)
        except asyncio.TimeoutError:
            return b""
    print((await cmd2("EHLO smoke")).decode())
    print((await cmd2("AUTH PLAIN " + base64.b64encode(b"\x00u\x00p").decode())).decode())
    print((await cmd2("MAIL FROM:<me@x.ru>")).decode())
    print((await cmd2("RCPT TO:<you@y.ru>")).decode())
    print((await cmd2("DATA")).decode())
    resp = await cmd2(b"Subject: smoke sub\r\n\r\nhello there\r\n.\r\n")
    assert b"250" in resp, resp
    print("SMTP send ok")
    await cmd2("QUIT")
    w.close()

    imap.close()
    smtp.close()
    print("SMOKE PASSED")


asyncio.run(main())
