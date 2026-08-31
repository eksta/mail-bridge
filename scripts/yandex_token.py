"""Exchange Yandex browser session cookies for an OAuth master token.

Uses the exact flow of the official app (passport SDK, GetMasterTokenByCookieRequest):
  POST https://mobileproxy.passport.yandex.net/1/bundle/oauth/token_by_sessionid
  Ya-Client-Host:   passport.yandex.ru
  Ya-Client-Cookie: Session_id=<..>; sessionid2=<..>     (Cookie.java:59 format)
  client_id / client_secret of ru.yandex.mail            (MailApplication.java:295)

Response: {"status":"ok","access_token":"y0_..."}  -> paste into config.json.

Usage:
  python scripts/yandex_token.py --sessionid2 "<cookie value>" [--session-id "<value>"]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge import http  # noqa: E402

HOST = "mobileproxy.passport.yandex.net"
PATH = "/1/bundle/oauth/token_by_sessionid"

# ru.yandex.mail 9.27.0, KPassportEnvironment.PRODUCTION
# (encrypted in MailApplication.java:295, decrypted per
#  com.yandex.passport.internal.util.b.c: AES-256-CFB128, zero IV,
#  key = XOR of hex(sha256(w))[0:32] for w in "yandex account manager")
CLIENT_ID = "7a54f58d4ebe431caaaa53895522bf2d"
CLIENT_SECRET = "52cf87e7968c43e993ccb6c4ea67190a"


def _clean(v):
    v = (v or "").strip().strip('"').strip("'").strip()
    return v.rstrip(";").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessionid2", default=None,
                    help="значение куки sessionid2 из браузера")
    ap.add_argument("--session-id", default=None,
                    help="значение куки Session_id (рекомендуется)")
    ap.add_argument("--host", default="passport.yandex.ru",
                    help="Ya-Client-Host (по умолчанию passport.yandex.ru; "
                         "попробуйте домен, где получили куку: yandex.ru, mail.yandex.ru)")
    ap.add_argument("--all-cookies", default=None,
                    help="вся строка cookie целиком, например: "
                         '"Session_id=X; sessionid2=Y; yandexuid=Z"')
    args = ap.parse_args()

    if not args.all_cookies and not args.sessionid2:
        ap.error("нужен --sessionid2 или --all-cookies")

    if args.all_cookies:
        cookie = _clean(args.all_cookies)
    else:
        sid2 = _clean(args.sessionid2)
        sid = _clean(args.session_id) if args.session_id else None
        # Cookie.java:59: Session_id добавляется только если он есть
        cookie = (f"Session_id={sid}; sessionid2={sid2}" if sid
                  else f"sessionid2={sid2}")
    print(f"Ya-Client-Cookie: {cookie[:40]}...")
    print(f"Ya-Client-Host:   {args.host}")

    status, text, _ = http.request(
        "POST", HOST, PATH,
        headers={
            "Ya-Client-Host": args.host,
            "Ya-Client-Cookie": cookie,
            "User-Agent": "ru.yandex.mail/9.27.0 (Android; bridge)",
        },
        form=[("client_id", CLIENT_ID), ("client_secret", CLIENT_SECRET)],
    )
    print(f"HTTP {status}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        print(text[:1000])
        return 1
    if data.get("status") == "ok" and data.get("access_token"):
        print("OAuth token:")
        print(data["access_token"])
        print("\nВставьте в config.json: \"token\": \"...\"")
        return 0
    print(json.dumps(data, ensure_ascii=False, indent=2)[:1000])
    return 1


if __name__ == "__main__":
    sys.exit(main())
