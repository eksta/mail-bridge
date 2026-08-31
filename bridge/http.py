"""Minimal HTTP client for the bridge (stdlib + cryptography for pinning)."""
import gzip
import hashlib
import http.client
import json
import logging
import ssl
import urllib.parse
from pathlib import Path


class HttpError(Exception):
    def __init__(self, status, body, url):
        super().__init__(f"HTTP {status} for {url}: {body[:500]}")
        self.status = status
        self.body = body
        self.url = url


_ssl_ctx = ssl.create_default_context()


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection verifying the leaf cert's SPKI sha256 == pin.

    For hosts whose chain terminates at a private root that no public
    store contains and the server never sends.  Trust-on-first-sight
    pinning of the leaf public key keeps the connection MITM-resistant
    without the private root.
    """

    def __init__(self, pin, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pin = pin

    def connect(self):
        super().connect()
        der = self.sock.getpeercert(binary_form=True)
        if not der:
            raise ssl.SSLError("pinned connection: no server certificate")
        import cryptography.x509 as x509
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PublicFormat)
        cert = x509.load_der_x509_certificate(der)
        spki = cert.public_key().public_bytes(
            Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        digest = hashlib.sha256(spki).hexdigest()
        if digest != self._pin:
            self.sock.close()
            raise ssl.SSLCertVerificationError(
                f"SPKI pin mismatch: {digest}")


def _spki_pin(der):
    import cryptography.x509 as x509
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PublicFormat)
    cert = x509.load_der_x509_certificate(der)
    spki = cert.public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(spki).hexdigest()


def request(method, host, path, params=None, headers=None,
            json_body=None, form=None, timeout=30, port=443, spki_pin=None):
    """Perform one HTTPS request. Returns (status, body_str, headers).

    spki_pin: expected sha256 of the server leaf's SubjectPublicKeyInfo
    (hex).  When set, the connection uses a no-verify context plus the
    explicit pin check above.
    """
    if params:
        qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        path = f"{path}?{qs}"
    hdrs = {
        "Accept": "*/*",
        "Accept-Encoding": "gzip",
        "Connection": "close",
    }
    if headers:
        hdrs.update(headers)
    data = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json; charset=utf-8"
    elif form is not None:
        # preserve explicit ordering & repeated keys
        parts = []
        for k, v in form:
            parts.append(f"{urllib.parse.quote(str(k), safe='')}={urllib.parse.quote(str(v), safe='')}")
        data = "&".join(parts).encode("utf-8")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"
    hdrs["Content-Length"] = str(len(data)) if data else "0"

    if spki_pin:
        noverify = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        noverify.check_hostname = False
        noverify.verify_mode = ssl.CERT_NONE
        conn = PinnedHTTPSConnection(spki_pin, host, port, timeout=timeout,
                                     context=noverify)
    else:
        conn = http.client.HTTPSConnection(host, port, timeout=timeout,
                                           context=_ssl_ctx)
    try:
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        if resp.getheader("Content-Encoding") == "gzip":
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        text = raw.decode("utf-8", "replace")
        return resp.status, text, dict(resp.getheaders())
    finally:
        conn.close()


def request_json(method, host, path, params=None, headers=None,
                 json_body=None, form=None, timeout=30, spki_pin=None):
    status, text, hdrs = request(method, host, path, params, headers,
                                 json_body, form, timeout,
                                 spki_pin=spki_pin)
    if status >= 400:
        raise HttpError(status, text, path)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise HttpError(status, f"non-JSON response: {text[:500]}", path)
