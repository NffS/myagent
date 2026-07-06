#!/usr/bin/env python3
"""
carserver.py - self-hosted server for the Magic Systems "Agent MS" / Super Agent
MS 3 (Car-Online) tracker. Two modes:

  standalone (default): we answer the device ourselves (handshake + record ACKs)
                        so it streams to us with no vendor server involved.
  relay (--relay):      transparently relay device <-> the real Car-Online server
                        (--upstream), letting the vendor answer while we still
                        parse + store every device frame. Keeps the device fully
                        working (and visible in the Car-Online app) while we
                        capture; robust against any gap in our own ACK logic.

Either way we parse position + telemetry into SQLite (car.db) for the webapp.

WIRE FORMAT (LE): 40 00 | len(2) | from(4) | to(4) | counter(2) | type(2) | datalen(4) | data
Handshake (standalone, on login 0x0e00): 0x0e01, 0x0f01, 0x0405, 0x0292+time.
Record ACK: device 0x0110 (id at data[4:8]) -> 0x0115 echoing that id; also
0x0400->0x0450, 0x0610->0x0600.

The device->server types we understand are listed in supported_types.json; any
type NOT listed there is written to unsupported_*.jsonl and logged (this is how
new packet types get surfaced). Supported frames are parsed/stored silently.

  python3 carserver.py                                   # standalone :11111
  python3 carserver.py --relay --upstream free.car-online.pro:11111
"""

import argparse
import datetime
import json
import os
import socket
import sqlite3
import struct
import threading

MAGIC = b"\x40\x00"
HDR = 20
HS_EMPTY = [0x0e01, 0x0f01, 0x0405]
TIME_BLOB = bytes.fromhex("32614b6ae385c501")
DEFAULT_SUPPORTED = {0x0e00, 0x0110, 0x0220, 0x0230, 0x0209, 0x0240, 0x0250,
                     0x0260, 0x0270, 0x0290, 0x0100, 0x0302, 0x0304, 0x0400, 0x0610}


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def build(frm, to, ctr, typ, data=b""):
    return (MAGIC + struct.pack("<H", HDR + len(data)) + struct.pack("<I", frm)
            + struct.pack("<I", to) + struct.pack("<H", ctr) + struct.pack("<H", typ)
            + struct.pack("<I", len(data)) + data)


def parse(buf):
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
            break
        f = buf[i:i + ln]
        out.append({"frm": struct.unpack("<I", f[4:8])[0], "to": struct.unpack("<I", f[8:12])[0],
                    "ctr": struct.unpack("<H", f[12:14])[0], "typ": struct.unpack("<H", f[14:16])[0],
                    "data": bytes(f[HDR:])})
        i += ln
    return out, i


def parse_gps(text):
    p = text.split(",")
    if len(p) < 9 or p[1] != "A":
        return None

    def dm(v, dd):
        if not v or "." not in v:
            return None
        return int(v[:dd]) + float(v[dd:]) / 60.0
    lat = dm(p[2], 2)
    lon = dm(p[4], 3)
    if lat is None or lon is None:
        return None
    if p[3] == "S":
        lat = -lat
    if p[5] == "W":
        lon = -lon
    t, d = p[0], p[8]
    dev_time = None
    if len(d) == 6 and len(t) >= 6 and d.isdigit() and t[:6].isdigit():
        dev_time = "20%s-%s-%s %s:%s:%s" % (d[4:6], d[2:4], d[0:2], t[0:2], t[2:4], t[4:6])
    return {"lat": round(lat, 6), "lon": round(lon, 6),
            "speed_knots": float(p[6]) if p[6] else 0.0,
            "course": float(p[7]) if p[7] else None, "dev_time": dev_time}


class CarServer:
    def __init__(self, port, capdir, relay=False, upstream=None, types_path=None):
        self.port = port
        self.capdir = capdir
        self.relay = relay
        self.up_host = self.up_port = None
        if upstream:
            h, p = upstream.rsplit(":", 1)
            self.up_host, self.up_port = h, int(p)
        os.makedirs(capdir, exist_ok=True)
        # supported device->server types (anything else is logged as UNSUPPORTED)
        self.supported = set(DEFAULT_SUPPORTED)
        if types_path and os.path.exists(types_path):
            try:
                j = json.load(open(types_path))
                self.supported = set(int(k, 16) for k in j.get("device_to_server", {}))
            except Exception as e:
                print("types load err:", e)
        # SQLite store
        self.dbpath = os.path.join(capdir, "car.db")
        self.db = sqlite3.connect(self.dbpath, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS position(id INTEGER PRIMARY KEY,"
                        "recv_ts TEXT, dev_time TEXT, lat REAL, lon REAL, speed_knots REAL, course REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS telemetry(id INTEGER PRIMARY KEY,"
                        "recv_ts TEXT, type TEXT, hex TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT, updated TEXT)")
        self.db.commit()
        self.dblock = threading.Lock()
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.loglock = threading.Lock()
        self.logfh = open(os.path.join(capdir, "carserver_%s.log" % ts), "a", encoding="utf-8")
        self.unsupfh = open(os.path.join(capdir, "unsupported_%s.jsonl" % ts), "a", encoding="utf-8")

    def log(self, msg):
        line = "[%s] %s" % (now(), msg)
        with self.loglock:
            print(line, flush=True)
            self.logfh.write(line + "\n")
            self.logfh.flush()

    def _kv(self, cur, k, v):
        cur.execute("INSERT INTO kv(k,v,updated) VALUES(?,?,?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated=excluded.updated",
                    (k, str(v), now()))

    def store(self, f):
        typ = f["typ"]
        data = f["data"]
        txt = data.decode("ascii", "replace")
        with self.dblock:
            cur = self.db.cursor()
            self._kv(cur, "last_seen", now())
            if typ == 0x0e00:
                self._kv(cur, "device_login", txt)
                self._kv(cur, "device_id", "0x%08x" % f["frm"])
            elif typ == 0x0220:
                g = parse_gps(txt)
                if g:
                    cur.execute("INSERT INTO position(recv_ts,dev_time,lat,lon,speed_knots,course)"
                                " VALUES(?,?,?,?,?,?)",
                                (now(), g["dev_time"], g["lat"], g["lon"], g["speed_knots"], g["course"]))
                    self._kv(cur, "last_lat", g["lat"])
                    self._kv(cur, "last_lon", g["lon"])
                    self._kv(cur, "last_speed_knots", g["speed_knots"])
                    self._kv(cur, "last_fix_time", g["dev_time"])
            elif typ == 0x0230:
                self._kv(cur, "last_cell", txt)
            elif typ == 0x0260:
                self._kv(cur, "sim_balance", txt)
            elif typ == 0x0302:
                self._kv(cur, "version", txt)
            elif typ == 0x0304:
                self._kv(cur, "devinfo", txt)
            elif typ in (0x0110, 0x0240, 0x0250, 0x0270, 0x0290, 0x0100, 0x0610):
                cur.execute("INSERT INTO telemetry(recv_ts,type,hex) VALUES(?,?,?)",
                            (now(), "0x%04x" % typ, data.hex()))
            self.db.commit()

    def log_unsupported(self, f, peer):
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in f["data"])
        rec = {"ts": now(), "peer": peer, "type": "0x%04x" % f["typ"], "ctr": f["ctr"],
               "len": len(f["data"]), "hex": f["data"].hex(), "ascii": asc}
        with self.loglock:
            self.unsupfh.write(json.dumps(rec) + "\n")
            self.unsupfh.flush()
        self.log("UNSUPPORTED type=0x%04x len=%d hex=%s |%s|"
                 % (f["typ"], len(f["data"]), f["data"].hex()[:64], asc[:40]))

    def process(self, f, peer):
        try:
            self.store(f)
        except Exception as e:
            self.log("store err 0x%04x: %s" % (f["typ"], e))
        if f["typ"] not in self.supported:
            self.log_unsupported(f, peer)

    def serve(self):
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.bind(("0.0.0.0", self.port))
        ls.listen(64)
        mode = "relay -> %s:%d" % (self.up_host, self.up_port) if self.relay else "standalone"
        self.log("carserver listening on :%d  mode=%s  db=%s  supported=%d types"
                 % (self.port, mode, self.dbpath, len(self.supported)))
        while True:
            c, a = ls.accept()
            threading.Thread(target=self.handle, args=(c, a), daemon=True).start()

    def handle(self, conn, addr):
        peer = "%s:%d" % addr
        self.log("+++ device connected %s [%s]" % (peer, "relay" if self.relay else "standalone"))
        try:
            if self.relay:
                self._relay(conn, peer)
            else:
                self._standalone(conn, peer)
        finally:
            self.log("--- disconnect %s" % peer)

    def _standalone(self, conn, peer):
        dev_id = [0]
        srv_ctr = [0]
        buf = bytearray()

        def send(typ, data=b""):
            srv_ctr[0] += 1
            try:
                conn.sendall(build(0, dev_id[0], srv_ctr[0], typ, data))
            except OSError:
                pass
        try:
            while True:
                d = conn.recv(65535)
                if not d:
                    break
                buf.extend(d)
                frames, c = parse(buf)
                del buf[:c]
                for f in frames:
                    t = f["typ"]
                    if t == 0x0e00:
                        dev_id[0] = f["frm"]
                        self.log("LOGIN %s id=%s" % (peer, f["data"].decode("ascii", "replace")))
                        for ht in HS_EMPTY:
                            send(ht)
                        send(0x0292, TIME_BLOB)
                    elif t == 0x0110 and len(f["data"]) >= 8:
                        send(0x0115, f["data"][4:8])
                    elif t == 0x0400:
                        send(0x0450)
                    elif t == 0x0610:
                        send(0x0600)
                    self.process(f, peer)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _relay(self, conn, peer):
        try:
            up = socket.create_connection((self.up_host, self.up_port), timeout=15)
        except Exception as e:
            self.log("!!! upstream %s:%d connect FAILED for %s: %s -- capture-only (no ACKs)"
                     % (self.up_host, self.up_port, peer, e))
            self._capture_only(conn, peer)
            return
        self.log("=== relaying %s <-> %s:%d" % (peer, self.up_host, self.up_port))
        t1 = threading.Thread(target=self._pump_dev, args=(conn, up, peer), daemon=True)
        t2 = threading.Thread(target=self._pump_raw, args=(up, conn), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    def _pump_dev(self, src, dst, peer):
        """device -> upstream: forward verbatim AND parse/store/flag each frame."""
        buf = bytearray()
        try:
            while True:
                d = src.recv(65535)
                if not d:
                    break
                dst.sendall(d)
                buf.extend(d)
                frames, c = parse(buf)
                del buf[:c]
                for f in frames:
                    if f["typ"] == 0x0e00:
                        self.log("LOGIN %s id=%s" % (peer, f["data"].decode("ascii", "replace")))
                    self.process(f, peer)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.close()
                except OSError:
                    pass

    def _pump_raw(self, src, dst):
        try:
            while True:
                d = src.recv(65535)
                if not d:
                    break
                dst.sendall(d)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.close()
                except OSError:
                    pass

    def _capture_only(self, conn, peer):
        buf = bytearray()
        try:
            while True:
                d = conn.recv(65535)
                if not d:
                    break
                buf.extend(d)
                frames, c = parse(buf)
                del buf[:c]
                for f in frames:
                    self.process(f, peer)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11111)
    ap.add_argument("--capdir", default="/root/captures")
    ap.add_argument("--relay", action="store_true",
                    help="relay to the real Car-Online server (--upstream) instead of answering ourselves")
    ap.add_argument("--upstream", default="free.car-online.pro:11111")
    ap.add_argument("--types", default=os.path.join(here, "supported_types.json"),
                    help="JSON file listing supported device->server frame types")
    a = ap.parse_args()
    CarServer(a.port, a.capdir, relay=a.relay, upstream=a.upstream, types_path=a.types).serve()


if __name__ == "__main__":
    main()
