"""Bridge entry point: reads config.json, starts IMAP+SMTP per account."""
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

from .backends import YandexBackend
from .imap_server import start_imap_server
from .smtp_server import start_smtp_server
from .yandex_api import YandexMailApi

DEFAULTS = {
    "yandex": {"imap_port": 1143, "smtp_port": 1025},
}


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value, where):
    if isinstance(value, str):
        def _sub(m):
            name = m.group(1)
            if name not in os.environ:
                sys.exit(f"{where}: env var {name} is not set")
            return os.environ[name]
        return _ENV_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _expand_env(v, where) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v, where) for v in value]
    return value


def load_config(path="config.json"):
    p = Path(path)
    if not p.exists():
        sys.exit(f"config {p} not found (copy config.example.json)")
    cfg = _expand_env(json.loads(p.read_text(encoding="utf-8")), p)
    return cfg


def build_backends(cfg):
    out = []
    for acc in cfg.get("accounts", []):
        kind = acc.get("type")
        if kind != "yandex":
            logging.getLogger("bridge").warning(
                "account type %r skipped (release supports 'yandex' only)",
                kind)
            continue
        api = YandexMailApi(acc["token"], acc.get("email"))
        out.append(("yandex", acc, YandexBackend(api)))
    return out


async def run(cfg):
    servers = []
    for kind, acc, backend in build_backends(cfg):
        user = acc.get("local_user") or acc["email"]
        pw = acc.get("local_password") or "bridge"
        host = acc.get("bind", "127.0.0.1")
        imap_port = acc.get("imap_port", DEFAULTS[kind]["imap_port"])
        smtp_port = acc.get("smtp_port", DEFAULTS[kind]["smtp_port"])
        servers.append(await start_imap_server(backend, host, imap_port, user, pw))
        servers.append(await start_smtp_server(backend, host, smtp_port, user, pw))
        print(f"[{kind}] {acc['email']}: IMAP {host}:{imap_port}  SMTP {host}:{smtp_port}  (user={user})")
        # Pre-warm body cache in background so first Sent sync is fast
        asyncio.ensure_future(_pewarm_cache(backend))
    print("bridge running. Ctrl+C to stop.")
    await asyncio.Event().wait()


async def _pewarm_cache(backend):
    """Pre-fetch all folder listings + recent bodies (background thread).

    Listings are walked fully so the first SELECT of any folder serves
    from the meta cache; the newest 50 bodies per folder are cached to
    speed up first renders.  INBOX and Sent are warmed first.
    """
    import logging
    log = logging.getLogger("bridge.warmup")
    loop = asyncio.get_event_loop()

    def _warm():
        try:
            folders = backend.folders()

            def prio(f):
                n = str(f.get("name", "")).upper()
                if n == "INBOX":
                    return 0
                if n in ("SENT", "ОТПРАВЛЕННЫЕ"):
                    return 1
                return 2

            for f in sorted(folders, key=prio):
                name = str(f.get("name", ""))
                total = int(f.get("total") or 0)
                if total <= 0:
                    continue
                fid = f["id"]
                log.info("warmup: walking %s (total=%s)...", name, total)
                try:
                    msgs = backend.list_messages_all(fid, cap=2000,
                                                     total=total)
                except Exception as e:
                    log.warning("warmup: %s listing failed: %s", name, e)
                    continue
                log.info("warmup: %s listing done (%d msgs)", name,
                         len(msgs))
                n_bodies = 0
                for m in msgs[:50]:
                    try:
                        backend.api_fetch_message(fid, m["mid"])
                        n_bodies += 1
                    except Exception:
                        pass
                log.info("warmup: %s bodies cached (%d/50)", name, n_bodies)
            log.info("warmup complete")
        except Exception as e:
            log.warning("warmup failed: %s", e)

    await asyncio.sleep(1)  # let servers start first
    await loop.run_in_executor(None, _warm)


def check(cfg, kind):
    """Verify tokens & print folders/messages count."""
    for k, acc, backend in build_backends(cfg):
        if kind and k != kind:
            continue
        print(f"== {k} {acc.get('email')}")
        try:
            folders = backend.folders()
            for f in folders[:10]:
                print(f"  folder {f['name']!r} id={f['id']} unread={f['unread']} total={f['total']}")
            msgs = backend.list_messages(folders[0]["id"], limit=3)
            for m in msgs:
                print(f"  msg {m['mid']}: {m['subject'][:60]!r}")
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR: {e}")


def main():
    args = sys.argv[1:]
    try:  # Windows console is cp866/cp1251 — subjects are UTF-8
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    cfg_path = "config.json"
    if "--config" in args:
        i = args.index("--config")
        cfg_path = args[i + 1]
        args = [a for j, a in enumerate(args) if j not in (i, i + 1)]
    write_log = "--log" in args
    args = [a for a in args if a != "--log"]
    handlers = [logging.StreamHandler(sys.stdout)]
    if write_log:
        handlers.append(logging.FileHandler("bridge.log",
                                            encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
        handlers=handlers)
    cfg = load_config(cfg_path)
    if args and args[0] == "check":
        check(cfg, args[1] if len(args) > 1 else "")
        return
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
