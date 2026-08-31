// hook_ssl.js — dump plaintext HTTP from Conscrypt NativeCrypto (Android 11)
console.log("[js] script loaded");
Java.perform(function () {
    console.log("[js] perform entered");
    var NativeCrypto = Java.use("com.android.org.conscrypt.NativeCrypto");
    var SIG = ['long',
               'com.android.org.conscrypt.NativeSsl',
               'java.io.FileDescriptor',
               'com.android.org.conscrypt.NativeCrypto$SSLHandshakeCallbacks',
               '[B', 'int', 'int', 'int'];

    function ascii(b, off, len, max) {
        var s = "";
        var n = Math.min(len, max || 3000);
        for (var i = 0; i < n; i++) {
            var c = b[off + i] & 0xff;
            if (c === 13) continue;
            s += (c >= 32 && c < 127) ? String.fromCharCode(c)
                : (c === 10 ? "\n" : ".");
        }
        return s;
    }

    try {
        NativeCrypto.SSL_write.overload.apply(NativeCrypto.SSL_write, SIG)
            .implementation = function (ssl, ptr, fd, cb, b, off, len, to) {
                try {
                    if (len > 0 && len < 300000) {
                        var head = ascii(b, off, Math.min(len, 10), 10);
                        var sample = ascii(b, off, len, 4000);
                        if (/^(PUT |POST |GET |PATCH )/.test(head) ||
                            /attach/i.test(sample) ||
                            /boundary=/i.test(sample)) {
                            console.log("=====SSL_write " + len +
                                " bytes=====");
                            console.log(sample.substring(0, 6000));
                            console.log("=====END SSL_write=====");
                        }
                    }
                } catch (e) {
                    console.log("[hook err] " + e);
                }
                return this.SSL_write(ssl, ptr, fd, cb, b, off, len, to);
            };
        console.log("[*] SSL_write hooked");
    } catch (e) {
        console.log("[!] SSL_write hook failed: " + e);
    }

    var BIO_SIG = ['long',
                   'com.android.org.conscrypt.NativeSsl',
                   'long',
                   '[B', 'int', 'int',
                   'com.android.org.conscrypt.NativeCrypto$SSLHandshakeCallbacks'];

    try {
        NativeCrypto.ENGINE_SSL_write_BIO_heap.overload.apply(
            NativeCrypto.ENGINE_SSL_write_BIO_heap, BIO_SIG)
            .implementation = function (ssl, ptr, bio, b, off, len, cb) {
                try {
                    if (len > 0 && len < 300000) {
                        var head = ascii(b, off, Math.min(len, 10), 10);
                        var sample = ascii(b, off, len, 4000);
                        if (/^(PUT |POST |GET |PATCH )/.test(head) ||
                            /attach/i.test(sample) ||
                            /boundary=/i.test(sample)) {
                            console.log("=====BIO_write " + len +
                                " bytes=====");
                            console.log(sample.substring(0, 6000));
                            console.log("=====END BIO_write=====");
                        }
                    }
                } catch (e) {
                    console.log("[hook err] " + e);
                }
                return this.ENGINE_SSL_write_BIO_heap(ssl, ptr, bio, b,
                    off, len, cb);
            };
        console.log("[*] ENGINE_SSL_write_BIO_heap hooked");
    } catch (e) {
        console.log("[!] write_BIO failed: " + e);
    }

    try {
        NativeCrypto.ENGINE_SSL_read_BIO_heap.overload.apply(
            NativeCrypto.ENGINE_SSL_read_BIO_heap, BIO_SIG)
            .implementation = function (ssl, ptr, bio, b, off, len, cb) {
                var n = this.ENGINE_SSL_read_BIO_heap(ssl, ptr, bio, b,
                    off, len, cb);
                try {
                    if (n > 0 && n < 200000) {
                        var s = ascii(b, off, n, 2500);
                        if (/attach|upload|disk|href/i.test(s)) {
                            console.log("=====BIO_read " + n + " bytes=====");
                            console.log(s.substring(0, 4000));
                            console.log("=====END BIO_read=====");
                        }
                    }
                } catch (e) {}
                return n;
            };
        console.log("[*] ENGINE_SSL_read_BIO_heap hooked");
    } catch (e) {
        console.log("[!] read_BIO failed: " + e);
    }
});
