import unittest

from bridge import rfc822


class TestOutgoing(unittest.TestCase):
    def _raw(self):
        return (b"From: me@mail.ru\r\n"
                b"To: a@b.ru, C D <c@d.ru>\r\n"
                b"Cc: x@y.ru\r\n"
                b"Subject: =?utf-8?B?0J/RgNC40LLQtdGCIQ==?=\r\n"
                b"Message-ID: <1@x>\r\n"
                b"Content-Type: text/html; charset=utf-8\r\n"
                b"\r\n" +
                "<html><body><p>Привет</p></body></html>".encode("utf-8"))

    def test_parse(self):
        p = rfc822.parse_outgoing(self._raw())
        self.assertEqual(p["to"], ["a@b.ru", "c@d.ru"])
        self.assertEqual(p["cc"], ["x@y.ru"])
        self.assertEqual(p["subject"], "Привет!")
        self.assertIn("<p>", p["html"] or "")
        self.assertIn("Привет", p["text"])


class TestYandexEnvelope(unittest.TestCase):
    def test_build(self):
        meta = {"mid": 123, "subjText": "Тест", "subjPrefix": "",
                "from": [{"name": "Иван", "email": "i@ya.ru"}],
                "recipients": [{"email": "me@ya.ru"}],
                "utc_timestamp": 1700000000}
        body_json = {"body": {"body": "<b>hi</b>"}}
        msg = rfc822.yandex_envelope_to_message(meta, body_json, "me@ya.ru")
        raw = msg.as_bytes()
        self.assertIn(b"hi", raw)
        parsed = rfc822.parse_outgoing(raw)
        self.assertEqual(parsed["to"], ["me@ya.ru"])


class TestImapProtocol(unittest.TestCase):
    def _session(self):
        from bridge.imap_server import ImapSession
        from bridge.backends import MailboxBackend

        class FakeBackend(MailboxBackend):
            name = "fake"

            def api_folders(self):
                return [{"id": "1", "name": "INBOX", "type": 1,
                         "unread": 1, "total": 1}]

            def api_fetch_page(self, folder_id, offset, limit):
                return [{
                    "mid": "m1", "subject": "hello",
                    "from": [{"email": "a@b.c"}], "to": [],
                    "date": 1, "unread": True, "snippet": "sn",
                }]

            def api_fetch_message(self, folder_id, mid):
                from email.message import EmailMessage
                m = EmailMessage()
                m["Subject"] = "hello"
                m["From"] = "a@b.c"
                m.set_content("body text")
                return m

            def api_set_seen(self, folder_id, mid, seen):
                pass

            def api_send(self, parsed, envelope_from):
                return {"status": 200}

        return ImapSession(FakeBackend(), "u", "p")

    def test_login_and_fetch(self):
        import asyncio

        async def run():
            s = self._session()
            r, w = await asyncio.open_connection()
            # in-memory pipe instead: use StreamsMock
            return s

        # simpler: feed lines directly
        s = self._session()
        out = []

        class W:
            def write(self, data):
                out.append(data)

            def close(self):
                pass

        s.connection_made(W())
        s.data_received(b'a1 LOGIN "u" "p"\r\n')
        s.data_received(b'a2 SELECT INBOX\r\n')
        s.data_received(b'a3 FETCH 1 (FLAGS UID RFC822.SIZE BODY[])\r\n')
        s.data_received(b'a4 LOGOUT\r\n')
        blob = b"".join(out)
        self.assertIn(b"a1 OK", blob)
        self.assertIn(b"EXISTS", blob)
        self.assertIn(b"body text", blob)
        self.assertIn(b"a3 OK", blob)


class TestSmtpProtocol(unittest.TestCase):
    def test_send(self):
        import base64
        from bridge.smtp_server import SmtpSession
        from bridge.backends import MailboxBackend
        sent = []

        class FakeBackend(MailboxBackend):
            def api_send(self, parsed, envelope_from):
                sent.append(parsed)
                return {"status": 200}

        s = SmtpSession(FakeBackend(), "u", "p")
        out = []

        class W:
            def write(self, data):
                out.append(data)

            def close(self):
                pass

        s.connection_made(W())
        s.data_received(b"EHLO test\r\n")
        s.data_received(b"AUTH PLAIN " +
                        base64.b64encode(b"\x00u\x00p") + b"\r\n")
        s.data_received(b"MAIL FROM:<me@mail.ru>\r\n")
        s.data_received(b"RCPT TO:<a@b.ru>\r\n")
        s.data_received(b"DATA\r\n")
        s.data_received(b"Subject: hi\r\n\r\nbody\r\n.\r\n")
        s.data_received(b"QUIT\r\n")
        blob = b"".join(out)
        self.assertIn(b"235", blob)


class TestImapSearchLiteral(unittest.TestCase):
    """Test SEARCH with UTF-8 literals (charset) and plain ASCII literals."""

    def _make_session(self):
        from bridge.imap_server import ImapSession
        from bridge.backends import MailboxBackend

        class FakeBackend(MailboxBackend):
            name = "fake"

            def api_folders(self):
                return [{"id": "1", "name": "INBOX", "type": 1,
                         "unread": 2, "total": 2}]

            def api_fetch_page(self, folder_id, offset, limit):
                return [
                    {"mid": "m1", "subject": "test",
                     "from": [{"email": "a@b.c"}], "to": [],
                     "date": 1, "unread": False, "snippet": "ok"},
                    {"mid": "m2", "subject": "Снимок экрана",
                     "from": [{"email": "b@c.d"}], "to": [],
                     "date": 2, "unread": True, "snippet": "topic"},
                ]

            def api_fetch_message(self, folder_id, mid):
                from email.message import EmailMessage
                m = EmailMessage()
                m["Subject"] = "hello"
                m["From"] = "a@b.c"
                m.set_content("body text")
                return m

            def api_set_seen(self, folder_id, mid, seen):
                pass

            def api_send(self, parsed, envelope_from):
                return {"status": 200}

        return ImapSession(FakeBackend(), "u", "p")

    def _run(self, lines):
        """Feed raw IMAP lines (bytes) into a session, return output bytes."""
        s = self._make_session()
        out = []

        class W:
            def write(self, data):
                out.append(data)
            def close(self):
                pass

        s.connection_made(W())
        s.data_received(b'a1 LOGIN "u" "p"\r\n')
        s.data_received(b'a2 SELECT INBOX\r\n')
        for line in lines:
            s.data_received(line)
        return b"".join(out)

    def test_search_cyrillic_literal(self):
        """SEARCH CHARSET UTF-8 SUBJECT {N+} with Cyrillic text."""
        utf8_bytes = "Снимок".encode("utf-8")
        blob = self._run([
            f'a3 SEARCH CHARSET UTF-8 SUBJECT {{{len(utf8_bytes)}+}}\r\n'.encode(),
            utf8_bytes + b"\r\n",
        ])
        self.assertIn(b"OK", blob)
        # Should find seq=2 (subject="Снимок экрана") — not seq=1 ("test")
        self.assertIn(b"* SEARCH 2", blob)
        self.assertNotIn(b"* SEARCH 1\r\n", blob)
        self.assertNotIn(b"* SEARCH 1 2\r\n", blob)
        # Case-insensitive: lowercase needle matches too (RFC 3501)
        low = "снимок".encode("utf-8")
        blob2 = self._run([
            f'a3 SEARCH CHARSET UTF-8 SUBJECT {{{len(low)}+}}\r\n'.encode(),
            low + b"\r\n",
        ])
        self.assertIn(b"* SEARCH 2", blob2)

    def test_search_ascii_literal_no_match(self):
        """SEARCH SUBJECT {N+} with literal not in any subject — returns empty."""
        blob = self._run([
            b'a3 SEARCH SUBJECT {6+}\r\n',
            b"digest\r\n",
        ])
        self.assertIn(b"OK", blob)
        # "digest" is not a substring of any subject → empty
        self.assertIn(b"* SEARCH\r\n", blob)

    def test_search_ascii_literal_match(self):
        """SEARCH SUBJECT {N+} with literal that IS a substring of a subject."""
        literal = b"test"
        blob = self._run([
            f'a3 SEARCH SUBJECT {{{len(literal)}+}}\r\n'.encode(),
            literal + b"\r\n",
        ])
        self.assertIn(b"OK", blob)
        self.assertIn(b"* SEARCH 1", blob)


if __name__ == "__main__":
    unittest.main()
