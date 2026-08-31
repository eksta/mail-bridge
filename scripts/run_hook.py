"""Spawn Yandex Mail with SSL hook, log plaintext to file.

Usage: python run_hook.py [package]     (default ru.yandex.mail)
Switch the hook file below if needed (hook_ssl.js = Java Conscrypt,
hook_native.js = native libssl exports).
"""
import sys
import time
from pathlib import Path

import frida

SCRIPTS_DIR = Path(__file__).resolve().parent
LOG = str(SCRIPTS_DIR / "frida_log.txt")
HOOK = SCRIPTS_DIR / "hook_ssl.js"
PACKAGE = sys.argv[1] if len(sys.argv) > 1 else "ru.yandex.mail"

log_f = open(LOG, "w", encoding="utf-8", buffering=1)


def on_message(message, data):
    t = message.get("type")
    if t == "send":
        log_f.write(str(message.get("payload")) + "\n")
    elif t == "log":
        log_f.write(str(message.get("payload")) + "\n")
    elif t == "error":
        log_f.write("[ERROR] " + str(message.get("description")) + "\n")
    else:
        log_f.write(str(message) + "\n")


dev = frida.get_usb_device(timeout=10)
pid = dev.spawn([PACKAGE])
session = dev.attach(pid)
script = session.create_script(
    HOOK.read_text(encoding="utf-8"))
script.on("message", on_message)
script.load()
dev.resume(pid)
log_f.write("[*] resumed, hooks active\n")
print("hooks active, pid", pid)

# block until killed
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
