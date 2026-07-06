#!/usr/bin/env python3
"""
carserver.py - standalone server for the Magic Systems "Agent MS" / Super Agent
MS 3 (Car-Online) tracker, reverse-engineered from a captured free.car-online.pro
session (2026-07-06). Replaces the vendor server: the device streams position +
telemetry directly to us.

WIRE FORMAT (both directions, little-endian):
    40 00 | len(2) | from(4) | to(4) | counter(2) | type(2) | datalen(4) | data
  - 20-byte header; len = 20 + datalen (total frame size).
  - device id = the `from` value in the login frame (e.g. 0x0000461c); server id = 0.
  - each side keeps its own incrementing `counter`.

HANDSHAKE (what makes the device stream): device sends login type 0x0e00, server
must reply (counters 1..4): 0x0e01, 0x0f01, 0x0405, then 0x0292 + 8-byte time blob.

KEEP-ALIVE ACK (what stops the 20-90s retry loop): every device "record" frame
(type 0x0110) carries a 4-byte record-id at data[4:8]; the server must reply with
type 0x0115 echoing that id. We also ack 0x0400->0x0450 and 0x0610->0x0600.

Device data frame types seen: 0x0220 GPS (ASCII RMC-like), 0x0230 cell/LBS,
0x0110 status record, 0x0302/0x0304 version/device info, 0x0240/50/60/70/90 misc
telemetry. Everything is logged to carserver_frames_*.jsonl for offline decoding.

Stdlib only.  python3 carserver.py [--port 11111]
"""

import argparse
import datetime
import json
import os
import socket
import struct
import threading

MAGIC = b"\x40\x00"
HDR = 20

# server handshake replies, in order, sent on device login (type 0x0e00)
HS_EMPTY = [0x0e01, 0x0f01, 0x0405]
# 8-byte blob from the captured session's 0x0292 frame (time/session); replayed.
TIME_BLOB = bytes.fromhex("32614b6ae385c501")

# device data types we recognise (for nicer logging)
TYPE_NAMES = {
    0x0e00: "LOGIN", 0x0110: "record", 0x0220: "GPS", 0x0230: "cell",
    0x0209: "t209", 0x0240: "t240", 0x0250: "t250", 0x0260: "t260",
    0x0270: "t270", 0x0290: "t290", 0x0302: "version", 0x0304: "devinfo",
    0x0400: "t400", 0x0610: "t610",
}


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def build(frm, to, ctr, typ, data=b""):
    return (MAGIC + struct.pack("<H", HDR + len(data)) + struct.pack("<I", frm)
            + struct.pack("<I", to) + struct.pack("<H", ctr) + struct.pack("<H", typ)
            + struct.pack("<I", len(data)) + data)


def parse(buf):
    """Pull complete frames out of buf. Returns (list_of_frames, bytes_consumed)."""
    out = []
    i = 0
    n = len(buf)
    while i + HDR <= n:
        if buf[i] != 0x40 or buf[i + 1] != 0x00:
            i += 1
            continue
        ln = buf[i + 2] | (buf[i + 3] << 8)
        if ln < HDR or ln > 65535:
            i += 1
            continue
        if i + ln > n:
            break  # incomplete frame, wait for more
        f = buf[i:i + ln]
        out.append({
            "frm": struct.unpack("<I", f[4:8])[0],
            "to": struct.unpack("<I", f[8:12])[0],
            "ctr": struct.unpack("<H", f[12:14])[0],
            "typ": struct.unpack("<H", f[14:16])[0],
            "data": bytes(f[HDR:]),
        })
        i += ln
    return out, i


class CarServer:
    def __init__(self, port, capdir):
        self.port = port
        self.capdir = capdir
        os.makedirs(capdir, exist_ok=True)
        self.framespath = os.path.join(
            capdir, "carserver_frames_%s.jsonl" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        self.lock = threading.Lock()
        self.logfh = open(os.path.join(
            capdir, "carserver_%s.log" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S")),
            "a", encoding="utf-8")
        self.framesfh = open(self.framespath, "a", encoding="utf-8")

    def log(self, msg):
        line = "[%s] %s" % (now(), msg)
        with self.lock:
            print(line, flush=True)
            self.logfh.write(line + "\n")
            self.logfh.flush()

    def record(self, peer, direction, f):
        rec = {"ts": now(), "peer": peer, "dir": direction, "ctr": f["ctr"],
               "type": "0x%04x" % f["typ"], "hex": f["data"].hex()}
        with self.lock:
            self.framesfh.write(json.dumps(rec) + "\n")
            self.framesfh.flush()

    def serve(self):
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.bind(("0.0.0.0", self.port))
        ls.listen(64)
        self.log("carserver listening on :%d  (frames -> %s)" % (self.port, self.framespath))
        while True:
            c, a = ls.accept()
            threading.Thread(target=self.handle, args=(c, a), daemon=True).start()

    def handle(self, conn, addr):
        peer = "%s:%d" % addr
        self.log("+++ device connected %s" % peer)
        dev_id = 0
        srv_ctr = 0
        buf = bytearray()

        def send(typ, data=b""):
            nonlocal srv_ctr
            srv_ctr += 1
            fr = build(0, dev_id, srv_ctr, typ, data)
            try:
                conn.sendall(fr)
            except OSError as e:
                self.log("send err %s: %s" % (peer, e))
                return
            self.log("   S>D ctr=%d type=0x%04x %s" % (srv_ctr, typ, data.hex()))

        try:
            while True:
                d = conn.recv(65535)
                if not d:
                    break
                buf.extend(d)
                frames, consumed = parse(buf)
                del buf[:consumed]
                for f in frames:
                    typ = f["typ"]
                    name = TYPE_NAMES.get(typ, "0x%04x" % typ)
                    asc = "".join(chr(b) if 32 <= b < 127 else "." for b in f["data"])
                    self.log("D>S ctr=%d %-8s dl=%d |%s" % (f["ctr"], name, len(f["data"]), asc[:60]))
                    self.record(peer, "D>S", f)

                    if typ == 0x0e00:                       # login -> handshake
                        dev_id = f["frm"]
                        self.log("   LOGIN dev_id=0x%08x  id/pass=%s" % (dev_id, asc))
                        for t in HS_EMPTY:
                            send(t)
                        send(0x0292, TIME_BLOB)
                    elif typ == 0x0110:                     # record -> ack the record-id
                        if len(f["data"]) >= 8:
                            send(0x0115, f["data"][4:8])
                    elif typ == 0x0400:
                        send(0x0450)
                    elif typ == 0x0610:
                        send(0x0600)
        except OSError as e:
            self.log("recv err %s: %s" % (peer, e))
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self.log("--- disconnect %s" % peer)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11111)
    ap.add_argument("--capdir", default="/root/captures")
    a = ap.parse_args()
    CarServer(a.port, a.capdir).serve()


if __name__ == "__main__":
    main()
