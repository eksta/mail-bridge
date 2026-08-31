"""Decrypt Yandex passport SDK credentials (Credentials.java -> util.b.c()).

Scheme (com.yandex.passport.internal.util.b):
  key  = 32 zero bytes XOR hex(sha256(w))[0:32] for w in "yandex account manager"
  AES-256-CFB8, IV = 16 zero bytes, ciphertext = base64(str)
  plaintext = utf8 -> split("^")[0], must be 32 chars
"""
import base64
import hashlib
import sys

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms  # noqa: F401


def derive_key():
    buf = bytearray(32)
    for w in ("yandex", "account", "manager"):
        hexs = hashlib.sha256(w.encode()).hexdigest()
        wb = hexs.encode()[:32]
        for i in range(32):
            buf[i] ^= wb[i]
    return bytes(buf)


def decrypt_credential(enc, mode="cfb8"):
    if len(enc) != 64:
        raise ValueError(f"expected 64 chars, got {len(enc)}")
    key = derive_key()
    iv = b"\x00" * 16
    ct = base64.b64decode(enc)
    if mode == "cfb8":
        from cryptography.hazmat.decrepit.ciphers.modes import CFB8
        m = CFB8(iv)
    else:
        from cryptography.hazmat.primitives.ciphers.modes import CFB
        m = CFB(iv)
    cipher = Cipher(algorithms.AES(key), m)
    dec = cipher.decryptor()
    pt = dec.update(ct) + dec.finalize()
    return pt


if __name__ == "__main__":
    vals = sys.argv[1:]
    for v in vals:
        for mode in ("cfb8", "cfb128"):
            pt = decrypt_credential(v, mode)
            print(f"[{mode}] hex: {pt.hex()}")
            print(f"[{mode}] raw: {pt!r}")
