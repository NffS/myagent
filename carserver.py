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
import time

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
    sats = int(p[9][1:]) if len(p) > 9 and p[9][:1] == "U" and p[9][1:].isdigit() else None
    hdop = None
    if len(p) > 10 and p[10]:
        try:
            hdop = float(p[10])
        except ValueError:
            pass
    return {"lat": round(lat, 6), "lon": round(lon, 6),
            "speed_knots": float(p[6]) if p[6] else 0.0,
            "course": float(p[7]) if p[7] else None, "dev_time": dev_time,
            "sats": sats, "hdop": hdop}


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
        self.db.execute("CREATE TABLE IF NOT EXISTS journal(id INTEGER PRIMARY KEY,"
                        "ts TEXT, dir TEXT, summary TEXT)")
        self.db.commit()
        self._jn = 0
        self._jlast = {}
        self.dblock = threading.Lock()
        self._trace_last = {}
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.loglock = threading.Lock()
        self.logfh = open(os.path.join(capdir, "carserver_%s.log" % ts), "a", encoding="utf-8")
        self.unsupfh = open(os.path.join(capdir, "unsupported_%s.jsonl" % ts), "a", encoding="utf-8")
        self.backfill()

    def backfill(self):
        """On startup, seed kv voltage/temperature from the most recent stored
        frames so the dashboard shows last-known values immediately (rather than
        blank until the next, possibly minutes-away, telemetry frame)."""
        cur = self.db.cursor()
        r = cur.execute("SELECT hex FROM telemetry WHERE type='0x0110' ORDER BY id DESC LIMIT 1").fetchone()
        if r:
            d = bytes.fromhex(r[0])
            if len(d) >= 16:
                self._kv(cur, "main_voltage", round((d[14] | (d[15] << 8)) * 0.019388, 2))
                self._kv(cur, "pin_voltage", round((d[12] | (d[13] << 8)) * 0.02857, 2))
                self._kv(cur, "status_word", "%04x %04x" % (d[8] | (d[9] << 8), d[10] | (d[11] << 8)))
        r = cur.execute("SELECT hex FROM telemetry WHERE type='0x0270' ORDER BY id DESC LIMIT 1").fetchone()
        if r:
            d = bytes.fromhex(r[0])
            if len(d) >= 3:
                self._kv(cur, "temperature", d[2] - 256 if d[2] >= 128 else d[2])
        r = cur.execute("SELECT hex FROM telemetry WHERE type='0x0250' ORDER BY id DESC LIMIT 1").fetchone()
        if r:
            d = bytes.fromhex(r[0])
            if len(d) >= 2:
                self._kv(cur, "backup_voltage", round(d[1] * 0.042234, 2))
        r = cur.execute("SELECT v FROM kv WHERE k='last_cell'").fetchone()
        if r:
            self._cell_signal(cur, r[0])
        self.db.commit()

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

    def _cell_signal(self, cur, txt):
        """0x0230 LBS 'MCC,MNC,LAC,CID-N,99': the -N suffix is a per-cell signal index.
        Its mapping to the Car-Online app's displayed dBm is non-standard; calibrated
        to two paired app readings (N=18 -> 65, N=21 -> 47) => dBm ~= 173 - 6N. This is
        a device-only APPROXIMATION (the app's exact value is vendor-computed and not
        transmitted). Trailing ",99" is the AT+CSQ "unknown" sentinel."""
        try:
            parts = txt.strip().split(",")
            if len(parts) >= 4 and "-" in parts[3]:
                n = int(parts[3].rpartition("-")[2])
                if 0 <= n <= 31:
                    self._kv(cur, "signal_csq", n)
                    self._kv(cur, "signal_dbm", max(1, min(113, 173 - 6 * n)))
        except (ValueError, IndexError):
            pass

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
                    if g["sats"] is not None:
                        self._kv(cur, "satellites", g["sats"])
                    if g["hdop"] is not None:
                        self._kv(cur, "hdop", g["hdop"])
            elif typ == 0x0230:
                self._kv(cur, "last_cell", txt)
                self._cell_signal(cur, txt)
            elif typ == 0x0260:
                self._kv(cur, "sim_balance", txt)
            elif typ == 0x0302:
                self._kv(cur, "version", txt)
            elif typ == 0x0304:
                self._kv(cur, "devinfo", txt)
            elif typ in (0x0110, 0x0240, 0x0250, 0x0270, 0x0290, 0x0100, 0x0610):
                cur.execute("INSERT INTO telemetry(recv_ts,type,hex) VALUES(?,?,?)",
                            (now(), "0x%04x" % typ, data.hex()))
                # 0x0110 record layout (after counter(4)+recordid(4)):
                #   data[8:10]  = status word A (base 0x0200) -- BITFIELD, not a voltage
                #   data[10:12] = status word B (base 0x0003; bit 0x4000 seen set on a
                #                 valet/mode toggle 2026-07-06) -- BITFIELD, not a voltage
                #   data[12:14] = pin/tag analog input (varies), *0.02857 -> V
                #   data[14:16] = main supply (637 const) *0.019388 -> 12.35V
                # NOTE: the old "backup=data[8:10]*0.007754" was WRONG -- that field is a
                # status bitfield (it flips on valet toggle), so backup V is not sourced here.
                if typ == 0x0110 and len(data) >= 16:
                    sa = data[8] | (data[9] << 8)
                    sb = data[10] | (data[11] << 8)
                    pin = data[12] | (data[13] << 8)
                    mn = data[14] | (data[15] << 8)
                    self._kv(cur, "main_raw", mn)
                    self._kv(cur, "pin_raw", pin)
                    self._kv(cur, "status_word", "%04x %04x" % (sa, sb))
                    self._kv(cur, "main_voltage", round(mn * 0.019388, 2))
                    self._kv(cur, "pin_voltage", round(pin * 0.02857, 2))
                elif typ == 0x0270 and len(data) >= 3:
                    t = data[2] - 256 if data[2] >= 128 else data[2]
                    self._kv(cur, "temp_raw", data[2])
                    self._kv(cur, "temperature", t)
                elif typ == 0x0250 and len(data) >= 2:
                    # data[1] = internal backup (Li-ion) battery: byte 94 -> 3.97V
                    # (calibrated vs app). Session range 79-110 -> 3.3-4.65V, a
                    # textbook Li-ion span; updates slowly (~every few min, value
                    # near-constant). data[0] is a fast counter; data[2:4] constant.
                    self._kv(cur, "backup_raw", data[1])
                    self._kv(cur, "backup_voltage", round(data[1] * 0.042234, 2))
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

    # server->device frame types we recognise (handshake + record ACKs + cmds)
    SRV_NAMES = {0x0e01: "login-ack", 0x0f01: "handshake", 0x0405: "handshake",
                 0x0292: "set-time", 0x0115: "record-ack", 0x0450: "ack",
                 0x0600: "ack", 0x0210: "command", 0x0230: "command"}

    def server_summary(self, f):
        """Compact description of a Car-Online -> device frame (relay only)."""
        nm = self.SRV_NAMES.get(f["typ"])
        return ("%s 0x%04x" % (nm, f["typ"])) if nm else ("0x%04x %dB" % (f["typ"], len(f["data"])))

    def journal_add(self, direction, typ, text, throttle=2.0):
        """Append a message to the journal (throttled per direction+type so a
        history dump doesn't flood it). direction is 'device' or 'server'."""
        key = (direction, typ)
        t = time.time()
        if throttle and t - self._jlast.get(key, 0) < throttle:
            return
        self._jlast[key] = t
        with self.dblock:
            cur = self.db.cursor()
            cur.execute("INSERT INTO journal(ts,dir,summary) VALUES(?,?,?)", (now(), direction, text))
            self._jn += 1
            if self._jn % 50 == 0:
                cur.execute("DELETE FROM journal WHERE id < (SELECT max(id)-800 FROM journal)")
            self.db.commit()

    def summary(self, f):
        """One-line, minimal, human-readable trace of a supported frame."""
        typ = f["typ"]
        d = f["data"]
        try:
            if typ == 0x0220:
                g = parse_gps(d.decode("ascii", "replace"))
                if not g:
                    return "gps (no fix)"
                return "gps %.5f,%.5f %.0fkn%s" % (g["lat"], g["lon"], g["speed_knots"],
                                                   " %dsat" % g["sats"] if g["sats"] is not None else "")
            if typ == 0x0110 and len(d) >= 16:
                return "rec main=%.2fV tag=%.2fV st=%04x/%04x" % (
                    (d[14] | (d[15] << 8)) * 0.019388, (d[12] | (d[13] << 8)) * 0.02857,
                    d[8] | (d[9] << 8), d[10] | (d[11] << 8))
            if typ == 0x0230:
                return "cell %s" % d.decode("ascii", "replace")[:24]
            if typ == 0x0260:
                return "bal %s" % d.decode("ascii", "replace").strip()[:24]
            if typ == 0x0270 and len(d) >= 3:
                return "temp %dC" % (d[2] - 256 if d[2] >= 128 else d[2])
            if typ == 0x0250 and len(d) >= 2:
                return "backup %.2fV" % (d[1] * 0.042234)
            if typ == 0x0302:
                return "ver %s" % d.decode("ascii", "replace")[:24]
            if typ == 0x0e00:
                return "login %s" % d.decode("ascii", "replace")
        except Exception:
            pass
        return "0x%04x %dB" % (typ, len(d))

    def trace(self, f):
        """Emit a throttled (>=2s per type) minimal trace line -- proves liveness
        without flooding the log during history dumps."""
        typ = f["typ"]
        t = time.time()
        if t - self._trace_last.get(typ, 0) < 2.0:
            return
        self._trace_last[typ] = t
        self.log("~ " + self.summary(f))

    def process(self, f, peer):
        try:
            self.store(f)
        except Exception as e:
            self.log("store err 0x%04x: %s" % (f["typ"], e))
        if f["typ"] in self.supported:
            self.trace(f)
        else:
            self.log_unsupported(f, peer)
        self.journal_add("device", f["typ"], self.summary(f))

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
        """upstream(Car-Online) -> device: forward verbatim AND journal each
        frame so we can see what the server sends back (record-acks, commands)."""
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
                    self.journal_add("server", f["typ"], self.server_summary(f))
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
