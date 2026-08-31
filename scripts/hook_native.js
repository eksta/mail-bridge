// hook_native.js — hook BoringSSL SSL_write/SSL_read at native level
function dumpBuf(ptr, len, tag) {
    try {
        var max = Math.min(len, 6000);
        var bytes = ptr.readByteArray(max);
        var u8 = new Uint8Array(bytes);
        var s = "";
        for (var i = 0; i < u8.length; i++) {
            var c = u8[i];
            if (c === 13) continue;
            s += (c >= 32 && c < 127) ? String.fromCharCode(c)
                : (c === 10 ? "\n" : ".");
        }
        console.log("=====" + tag + " " + len + " bytes=====");
        console.log(s);
        console.log("=====END=====");
    } catch (e) {
        console.log("[dump err] " + e);
    }
}

function isInteresting(ptr, len) {
    try {
        var head = ptr.readUtf8String(Math.min(len, 10));
        if (head && /^(PUT |POST |GET |PATCH )/.test(head)) return true;
        var sample = ptr.readCString(Math.min(len, 4000));
        if (sample && (/attach/i.test(sample) || /boundary=/i.test(sample))) {
            return true;
        }
    } catch (e) {}
    return false;
}

var hooked = 0;

function tryHookNative() {
    var m = Process.findModuleByName("libssl.so");
    if (!m) {
        setTimeout(tryHookNative, 500);
        return;
    }
    ["SSL_write", "SSL_read"].forEach(function (name) {
        var addr = Module.findExportByName("libssl.so", name);
        if (!addr) { console.log("[i] missing export " + name); return; }
        if (name === "SSL_write") {
            Interceptor.attach(addr, {
                onEnter: function (args) {
                    this.buf = args[1];
                    this.num = args[2].toInt32();
                },
                onLeave: function (retval) {
                    try {
                        var n = this.num;
                        if (n > 0 && n < 400000 &&
                            isInteresting(this.buf, n)) {
                            dumpBuf(this.buf, n, "SSL_write");
                        }
                    } catch (e) {}
                }
            });
        } else {
            Interceptor.attach(addr, {
                onEnter: function (args) {
                    this.buf = args[1];
                },
                onLeave: function (retval) {
                    try {
                        var n = retval.toInt32();
                        if (n > 0 && n < 200000) {
                            var s = this.buf.readCString(Math.min(n, 2500));
                            if (s && (/attach|upload|disk|href/i.test(s))) {
                                dumpBuf(this.buf, n, "SSL_read");
                            }
                        }
                    } catch (e) {}
                }
            });
        }
        hooked++;
        console.log("[*] hooked " + name + " @ " + addr);
    });
    console.log("[js] native hooks done, total=" + hooked);
}

tryHookNative();
console.log("[js] native hooks setup started");
