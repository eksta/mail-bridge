"""Attach to running Yandex Mail, load SSL hook."""
import sys
import time
from pathlib import Path

import frida

SCRIPTS_DIR = Path(__file__).resolve().parent
LOG = str(SCRIPTS_DIR / "frida_log.txt")
log_f = open(LOG, "w", encoding="utf-8", buffering=1)


def on_message(message, data):
    t = message.get("type")
    if t == "log":
        log_f.write(str(message.get("payload")) + "\n")
    elif t == "send":
        log_f.write(str(message.get("payload")) + "\n")
    elif t == "error":
        log_f.write("[ERROR] " + str(message.get("description")) + "\n")
    else:
        log_f.write(str(message) + "\n")


dev = frida.get_usb_device(timeout=10)
target = None
for p in dev.enumerate_processes():
    if p.pid == 7360 or p.name == "Yandex Mail":
        target = p.pid
        break
if target is None:
    print("app not running")
    sys.exit(1)
session = dev.attach(target)
script = session.create_script(
    open(SCRIPTS_DIR / "hook_native.js", encoding="utf-8").read())
script.on("message", on_message)


def on_log(level, text):
    log_f.write(text + "\n")


try:
    script.set_log_handler(on_log)
except Exception as e:
    log_f.write(f"[no log handler: {e}]\n")
script.load()
log_f.write("[*] attached, hooks active\n")
print("attached ok")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass


