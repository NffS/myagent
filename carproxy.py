#!/usr/bin/env python3
"""
carproxy.py - transparent TCP proxy + full-capture MITM between the Magic
Systems "Agent MS" tracker and the real Car-Online server (v5.car-online.ru).

Why: the device (repointed to our IP) speaks a proprietary protocol and won't
emit position data without the correct server handshake, which we can't
synthesize blind. By transparently relaying device <-> real Car-Online and
logging BOTH directions, the device works normally (gets real ACKs -> streams
positions) AND we capture the complete protocol (handshake + position frames +
server commands) needed to build a standalone server. Car-Online keeps working
in the meantime.

    device --> [ :11111 this proxy ] --> v5.car-online.ru:11111
           <--                       <--

Everything is logged to proxy_*.log (hex+ascii, direction-tagged) and to raw
per-direction .bin files. Stdlib only.

    python3 carproxy.py [--listen-port 11111] [--upstream v5.car-online.ru:11111]
"""

import argparse
import datetime
import json
import os
import socket
import threading


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def hexdump(data, indent="    "):
    out = []
    w = 16
    for off in range(0, len(data), w):
        chunk = data[off:off + w]
        hexs = " ".join("%02x" % b for b in chunk) + "   " * (w - len(chunk))
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append("%s%04x  %s  |%s|" % (indent, off, hexs, asc))
    return "\n".join(out)


class Proxy:
    def __init__(self, listen_port, up_host, up_port, capdir, no_upstream=False):
        self.listen_port = listen_port
        self.up_host = up_host
        self.up_port = up_port
        self.capdir = capdir
        self.no_upstream = no_upstream
        os.makedirs(capdir, exist_ok=True)
        self.logpath = os.path.join(
            capdir, "proxy_%s.log" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        self.framespath = os.path.join(
            capdir, "frames_%s.jsonl" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        self.lock = threading.Lock()
        self.logfh = open(self.logpath, "a", encoding="utf-8")
        # machine-parseable: one JSON record per frame, for offline analysis
        self.framesfh = open(self.framespath, "a", encoding="utf-8")

    def log(self, msg, dump=None):
        line = "[%s] %s" % (now(), msg)
        with self.lock:
            print(line, flush=True)
            self.logfh.write(line + "\n")
            if dump is not None:
                self.logfh.write(hexdump(dump) + "\n")
                print(hexdump(dump), flush=True)
            self.logfh.flush()

    def serve(self):
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.bind(("0.0.0.0", self.listen_port))
        ls.listen(64)
        mode = ("CAPTURE-ONLY (forwarding to Car-Online DISABLED)" if self.no_upstream
                else "relay -> %s:%d" % (self.up_host, self.up_port))
        self.log("PROXY listening :%d   mode: %s" % (self.listen_port, mode))
        self.log("  human log : %s" % self.logpath)
        self.log("  frames    : %s" % self.framespath)
        while True:
            c, a = ls.accept()
            threading.Thread(target=self.handle, args=(c, a), daemon=True).start()

    def handle(self, client, addr):
        peer = "%s:%d" % addr
        self.log("+++ device connected %s" % peer)
        base = addr[0].replace(".", "-")
        rawc = os.path.join(self.capdir, "px_%s_DEV2SRV.bin" % base)
        raws = os.path.join(self.capdir, "px_%s_SRV2DEV.bin" % base)
        if self.no_upstream:
            # Forwarding disabled: capture the device's data, send nothing onward.
            self.log("=== CAPTURE-ONLY (forwarding to Car-Online DISABLED) %s" % peer)
            self.pump(client, None, "DEV>SRV(capture-only)", peer, rawc)
            self.log("--- closed %s (capture-only)" % peer)
            return
        try:
            up = socket.create_connection((self.up_host, self.up_port), timeout=15)
        except Exception as e:
            # Upstream (Car-Online) is down: still capture the device's bytes --
            # we just can't forward them. A one-sided capture beats losing the data.
            self.log("!!! upstream connect FAILED for %s: %s -- CAPTURE-ONLY mode" % (peer, e))
            self.pump(client, None, "DEV>SRV(no-upstream)", peer, rawc)
            self.log("--- closed %s (capture-only)" % peer)
            return
        self.log("=== relaying %s <-> %s:%d" % (peer, self.up_host, self.up_port))
        t1 = threading.Thread(target=self.pump, args=(
            client, up, "DEV>SRV", peer, rawc), daemon=True)
        t2 = threading.Thread(target=self.pump, args=(
            up, client, "SRV>DEV", peer, raws), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.log("--- closed %s" % peer)

    def record(self, tag, peer, data):
        rec = {"ts": now(), "dir": tag, "peer": peer, "len": len(data), "hex": data.hex()}
        with self.lock:
            self.framesfh.write(json.dumps(rec) + "\n")
            self.framesfh.flush()

    def pump(self, src, dst, tag, peer, rawpath):
        """Relay src->dst while logging every chunk. dst=None => capture-only
        (log + save to disk but don't forward), used when upstream is down."""
        try:
            while True:
                data = src.recv(65535)
                if not data:
                    break
                self.log("%s  %s  %d bytes" % (tag, peer, len(data)), dump=data)
                self.record(tag, peer, data)
                with open(rawpath, "ab") as fh:
                    fh.write(data)
                if dst is not None:
                    dst.sendall(data)
        except Exception:
            pass
        finally:
            for s in (src, dst):
                if s is None:
                    continue
                try:
                    s.close()
                except Exception:
                    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen-port", type=int, default=11111)
    ap.add_argument("--upstream", default="v5.car-online.ru:11111")
    ap.add_argument("--capdir", default="/root/captures")
    ap.add_argument("--no-upstream", action="store_true",
                    help="capture-only: do NOT forward to Car-Online (just log the device's data)")
    a = ap.parse_args()
    host, port = a.upstream.rsplit(":", 1)
    Proxy(a.listen_port, host, int(port), a.capdir, no_upstream=a.no_upstream).serve()


if __name__ == "__main__":
    main()
