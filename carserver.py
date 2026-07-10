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
import re
import socket
import sqlite3
import struct
import threading
import time

MAGIC = b"\x40\x00"
HDR = 20
HS_EMPTY = [0x0e01, 0x0f01, 0x0405]
# 0x0292 "set-time" payload = [unix_seconds LE u32][unix_minutes LE u32], UTC
# (minutes = round(seconds/60)). This captured constant (=2026-07-06 08:02:58Z) is
# kept only as a decode reference; standalone mode sends live time via build_time().
TIME_BLOB = bytes.fromhex("32614b6ae385c501")
DEFAULT_SUPPORTED = {0x0e00, 0x0110, 0x0220, 0x0230, 0x0209, 0x0240, 0x0250,
                     0x0260, 0x0270, 0x0290, 0x0100, 0x0302, 0x0304, 0x0400, 0x0610}
SERVER_VERSION = "1.0.0"   # semantic server version (bump on release)


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
        self.db.execute("CREATE TABLE IF NOT EXISTS metrics(id INTEGER PRIMARY KEY,"
                        "ts TEXT, name TEXT, value REAL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_metrics ON metrics(name, id)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,"
                        "ts TEXT, kind TEXT, event TEXT)")
        self.db.commit()
        # record this server's version + build time (file mtime) for the web UI
        try:
            _bt = datetime.datetime.utcfromtimestamp(os.path.getmtime(os.path.abspath(__file__))).strftime("%Y-%m-%d %H:%M UTC")
            self.db.execute("INSERT OR REPLACE INTO kv(k,v,updated) VALUES('server_version',?,?)", (SERVER_VERSION, now()))
            self.db.execute("INSERT OR REPLACE INTO kv(k,v,updated) VALUES('server_build',?,?)", (_bt, now()))
            self.db.commit()
        except Exception:
            pass
        self._jn = 0
        self._jlast = {}
        self._sigN = []
        self._metric_last = 0
        self._estate = {}   # last-seen state per event category (for transition detection)
        self._elast = {}    # last emit epoch per category (debounce rapid flips)
        self._en = 0
        self.dblock = threading.Lock()
        self._trace_last = {}
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.loglock = threading.Lock()
        self.logfh = open(os.path.join(capdir, "carserver_%s.log" % ts), "a", encoding="utf-8")
        self.unsupfh = open(os.path.join(capdir, "unsupported_%s.jsonl" % ts), "a", encoding="utf-8")
        self.srvfh = open(os.path.join(capdir, "srv_frames_%s.jsonl" % ts), "a", encoding="utf-8")
        self.backfill()
        self.backfill_metrics()
        self.backfill_events()
        self.purge_old()

    def backfill(self):
        """On startup, seed kv voltage/temperature from the most recent stored
        frames so the dashboard shows last-known values immediately (rather than
        blank until the next, possibly minutes-away, telemetry frame)."""
        cur = self.db.cursor()
        r = cur.execute("SELECT hex FROM telemetry WHERE type='0x0110' ORDER BY id DESC LIMIT 1").fetchone()
        if r:
            d = bytes.fromhex(r[0])
            if len(d) >= 16:
                d8 = d[8] | (d[9] << 8); d10 = d[10] | (d[11] << 8)
                self._kv(cur, "backup_voltage", round(d[14] * 0.03176, 2))
                self._kv(cur, "status_word", "%04x %04x %02x" % (d8, d10, d[15]))
                self._kv(cur, "armed", "valet" if (d10 & 0x4000) else ("armed" if (d8 & 0x0200) else "disarmed"))
                self._kv(cur, "valet", "on" if (d10 & 0x4000) else "off")
                self._kv(cur, "moving", "yes" if (d8 & 0x0400) else "no")
                self._kv(cur, "label", "found" if not (d[15] & 0x02) else "absent")
        # seed tag voltage from the most recent in-cluster (90-106) 0x0110 record
        for (hx,) in cur.execute("SELECT hex FROM telemetry WHERE type='0x0110' ORDER BY id DESC LIMIT 40"):
            b = bytes.fromhex(hx)
            if len(b) >= 16 and 90 <= b[12] <= 106:
                self._kv(cur, "pin_voltage", round(b[12] * 0.02755, 2))
                break
        r = cur.execute("SELECT hex FROM telemetry WHERE type='0x0270' ORDER BY id DESC LIMIT 1").fetchone()
        if r:
            d = bytes.fromhex(r[0])
            if len(d) >= 3:
                self._kv(cur, "temperature", d[2] - 256 if d[2] >= 128 else d[2])
        r = cur.execute("SELECT hex FROM telemetry WHERE type='0x0250' ORDER BY id DESC LIMIT 1").fetchone()
        if r:
            d = bytes.fromhex(r[0])
            if len(d) >= 2:
                self._kv(cur, "main_voltage", round(d[1] * 0.13138, 2))
        r = cur.execute("SELECT v FROM kv WHERE k='last_cell'").fetchone()
        if r:
            self._cell_signal(cur, r[0])
        self.db.commit()

    # dashboard-graph metrics: (metric name in `metrics` table, kv key it comes from)
    METRIC_KEYS = (("main_voltage", "main_voltage"), ("temperature", "temperature"),
                   ("balance", "sim_balance"), ("signal_dbm", "signal_dbm"),
                   ("satellites", "satellites"), ("backup_voltage", "backup_voltage"),
                   ("tag_voltage", "pin_voltage"))

    @staticmethod
    def _num(v):
        if v is None:
            return None
        m = re.search(r"-?\d+(?:\.\d+)?", str(v))
        return float(m.group()) if m else None

    def _snapshot_metrics(self, cur):
        """Every 60s, append each metric's current numeric value to the time-series
        `metrics` table so the dashboard can graph it."""
        t = time.time()
        if t - self._metric_last < 60:
            return
        self._metric_last = t
        kv = {k: v for k, v in cur.execute("SELECT k,v FROM kv")}
        ts = now()
        for name, key in self.METRIC_KEYS:
            v = self._num(kv.get(key))
            if v is not None:
                cur.execute("INSERT INTO metrics(ts,name,value) VALUES(?,?,?)", (ts, name, v))

    def backfill_metrics(self):
        """Seed the metrics table from stored telemetry (main/backup/tag/temp have raw
        history), sampled ~1/min, only for rows newer than what's already stored."""
        cur = self.db.cursor()
        jobs = (
            ("main_voltage", "0x0250", lambda d: round(d[1] * 0.13138, 2) if len(d) >= 2 else None),
            ("backup_voltage", "0x0110", lambda d: round(d[14] * 0.03176, 2) if len(d) >= 16 else None),
            ("tag_voltage", "0x0110", lambda d: round(d[12] * 0.02755, 2) if len(d) >= 16 and 90 <= d[12] <= 106 else None),
            ("temperature", "0x0270", lambda d: (d[2] - 256 if d[2] >= 128 else d[2]) if len(d) >= 3 else None),
        )
        for name, typ, fn in jobs:
            last = cur.execute("SELECT MAX(ts) FROM metrics WHERE name=?", (name,)).fetchone()[0] or ""
            rows = cur.execute("SELECT recv_ts,hex FROM telemetry WHERE type=? AND recv_ts>? ORDER BY id",
                               (typ, last)).fetchall()
            ins, lastmin = [], None
            for ts, hx in rows:
                if ts[:16] == lastmin:
                    continue
                lastmin = ts[:16]
                try:
                    v = fn(bytes.fromhex(hx))
                except Exception:
                    v = None
                if v is not None:
                    ins.append((ts, name, v))
            if ins:
                cur.executemany("INSERT INTO metrics(ts,name,value) VALUES(?,?,?)", ins)
        self.db.commit()

    # derived event log: map a state category+value to (kind, human label)
    EV_LABELS = {
        ("guard", "armed"): ("security", "Armed"), ("guard", "disarmed"): ("security", "Disarmed"),
        ("valet", "on"): ("security", "Valet mode on"), ("valet", "off"): ("security", "Valet mode off"),
        ("moving", "yes"): ("motion", "Moving"), ("moving", "no"): ("motion", "Parked"),
        ("door", "open"): ("door", "Door open"), ("door", "closed"): ("door", "Door closed"),
        ("label", "found"): ("label", "Label found"), ("label", "absent"): ("label", "Label not found"),
    }

    def _emit_event(self, cur, ts, kind, event):
        cur.execute("INSERT INTO events(ts,kind,event) VALUES(?,?,?)", (ts, kind, event))

    @staticmethod
    def _ts_epoch(ts):
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return (datetime.datetime.strptime(ts[:26], fmt) - datetime.datetime(1970, 1, 1)).total_seconds()
            except ValueError:
                continue
        return 0.0

    def _detect_events(self, cur, ts, d8, d10, d15, state, last):
        """Emit an event when a category SETTLES at a new value. `state` = last EMITTED
        value per category (not merely last-seen), `last` = last emit epoch. This kills
        the duplicate bursts from buffered-reconnect replay: when the device reconnects it
        dumps buffered records whose recv_ts bunch into 1-2s and whose bits bounce
        (armed->disarmed->armed, etc.). Comparing to the last EMITTED value means a value
        that flips away and back never re-fires, and a flip that reverts within DEBOUNCE
        is dropped entirely. Real transitions in a drive are seconds+ apart, so untouched."""
        # d8 0x0400 = moving/driving (motion; clears when the car stops -- NOT the engine
        # key); data[15] 0x02 set = immobilizer tag absent. (Both confirmed vs Car-Online.)
        DEBOUNCE = 2.0
        new = {"guard": "armed" if (d8 & 0x0200) else "disarmed",
               "valet": "on" if (d10 & 0x4000) else "off",
               "moving": "yes" if (d8 & 0x0400) else "no",
               "door": "open" if (d8 & 0x0004) else "closed",
               "label": "found" if not (d15 & 0x02) else "absent"}
        t = self._ts_epoch(ts)
        # Skip buffered-reconnect BURST frames: after a dropout the device replays buffered
        # records <~1.2s apart whose bits bounce (e.g. a 1ms door blip while armed). Real
        # states persist and are caught on the next normal-cadence frame, so nothing real
        # is lost -- only the transient replay artifacts.
        prevf = last.get("_frame")
        last["_frame"] = t
        if prevf is not None and (t - prevf) < 1.2:
            return
        for cat, val in new.items():
            prev = state.get(cat)            # last EMITTED value for this category
            if prev is None:
                state[cat] = val             # seed on first sight -- no event
                continue
            if val == prev:
                continue                     # unchanged vs last emitted -> no duplicate
            if t - last.get(cat, 0.0) < DEBOUNCE:
                continue                     # flipped/reverted within the settle window -> drop
            last[cat] = t
            state[cat] = val                 # commit the newly emitted value
            kind, label = self.EV_LABELS.get((cat, val), (cat, val))
            self._emit_event(cur, ts, kind, label)

    def _event_locked(self, kind, event, ts=None):
        """Log a standalone event (e.g. connectivity) taking the db lock ourselves."""
        with self.dblock:
            cur = self.db.cursor()
            self._emit_event(cur, ts or now(), kind, event)
            self._en += 1
            if self._en % 40 == 0:
                cur.execute("DELETE FROM events WHERE id < (SELECT max(id)-3000 FROM events)")
            self.db.commit()

    def backfill_events(self):
        """On first run, seed the event log from the last 24h of stored 0x0110 status
        transitions so the Events tab isn't empty. Then live detection takes over."""
        cur = self.db.cursor()
        if cur.execute("SELECT count(*) FROM events").fetchone()[0] > 0:
            return
        cut = (datetime.datetime.now() - datetime.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        # fetchall FIRST: _detect_events reuses cur for INSERTs, which would clobber a live SELECT cursor
        rows = cur.execute("SELECT recv_ts,hex FROM telemetry WHERE type='0x0110' AND recv_ts>? ORDER BY id", (cut,)).fetchall()
        state, last = {}, {}
        for ts, hx in rows:
            b = bytes.fromhex(hx)
            if len(b) >= 16:
                self._detect_events(cur, ts, b[8] | (b[9] << 8), b[10] | (b[11] << 8), b[15], state, last)
        self.db.commit()
        self._estate, self._elast = state, last  # continue live detection from the last state

    # retention per table: (table, time-column, days) -- raw journal 7d, everything else 90d
    RETENTION = (("metrics", "ts", 90), ("events", "ts", 90), ("position", "recv_ts", 90),
                 ("telemetry", "recv_ts", 90), ("journal", "ts", 7))

    def purge_old(self):
        """Retention: keep the raw journal 7 days, all other history 90 days."""
        now = datetime.datetime.now()
        cur = self.db.cursor()
        for tbl, col, days in self.RETENTION:
            cut = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            cur.execute("DELETE FROM %s WHERE %s < ?" % (tbl, col), (cut,))
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
        Its mapping to the app's displayed dBm is non-standard; refit to three paired app
        readings (N=13->75, 18->65, 21->47, 25->39) => dBm ~= 114 - 3N (hits the two
        freshest stable-N points N=13 and N=25 exactly; the mid points ~+/-5, noisy
        driving samples). Device-only APPROXIMATION -- the app's exact value is a vendor
        server-side derivation, not on the wire, so it stays +/- a few dB. ",99" = CSQ-unknown."""
        try:
            parts = txt.strip().split(",")
            if len(parts) >= 4 and "-" in parts[3]:
                n = int(parts[3].rpartition("-")[2])
                if 0 <= n <= 31:
                    self._sigN.append(n)
                    del self._sigN[:-5]  # keep last 5
                    m = sorted(self._sigN)[len(self._sigN) // 2]  # rolling median damps per-scan jitter
                    self._kv(cur, "signal_csq", m)
                    self._kv(cur, "signal_dbm", max(1, min(113, round(114 - 3 * m))))
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
            elif typ in (0x0110, 0x0209, 0x0240, 0x0250, 0x0270, 0x0290, 0x0100, 0x0610):
                # 0x0209 = discrete EVENT records (transponder/parking-brake/Event#NN/
                # armed-by-remote etc. that Car-Online decodes). Stored raw for decoding.
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
                    d8 = data[8] | (data[9] << 8)
                    d10 = data[10] | (data[11] << 8)
                    self._kv(cur, "status_word", "%04x %04x %02x" % (d8, d10, data[15]))
                    self._kv(cur, "pin_raw", data[12] | (data[13] << 8))
                    # data[12] is TIME-MULTIPLEXED: the ~90-106 cluster is the tag/analog
                    # voltage (98 -> 2.70V); other values (~39-41 + transients) are a 2nd
                    # signal on the same byte. Publish tag only from the tag cluster so it
                    # stays stable (ignore the interleaved other-signal readings).
                    if 90 <= data[12] <= 106:
                        self._kv(cur, "pin_voltage", round(data[12] * 0.02755, 2))
                    # data[14] = 125 (constant) -> internal BACKUP battery, stable ~3.97V.
                    self._kv(cur, "backup_voltage", round(data[14] * 0.03176, 2))
                    # valet mode = d10 & 0x4000 (takes priority; guard bit d8 0x0200 is
                    # cleared in valet, which otherwise reads as "disarmed").
                    self._kv(cur, "armed", "valet" if (d10 & 0x4000) else ("armed" if (d8 & 0x0200) else "disarmed"))
                    self._kv(cur, "valet", "on" if (d10 & 0x4000) else "off")
                    # moving / driving (debounced ride state) = d8 & 0x0400: sets when the
                    # car is actually being driven, clears the moment it stops (tracks GPS
                    # speed, with hysteresis through brief stops). This is MOTION, not the
                    # engine key -- a true ACC/ignition signal isn't cleanly in this word.
                    self._kv(cur, "moving", "yes" if (d8 & 0x0400) else "no")
                    # label / transponder (метка) = data[15] & 0x02: SET -> tag absent,
                    # 0 -> tag found (confirmed vs Car-Online "Transponder found/not found").
                    self._kv(cur, "label", "found" if not (data[15] & 0x02) else "absent")
                    self._detect_events(cur, now(), d8, d10, data[15], self._estate, self._elast)
                elif typ == 0x0270 and len(data) >= 3:
                    t = data[2] - 256 if data[2] >= 128 else data[2]
                    self._kv(cur, "temp_raw", data[2])
                    self._kv(cur, "temperature", t)
                elif typ == 0x0250 and len(data) >= 2:
                    # data[1] = MAIN supply voltage. It RISES when the engine runs
                    # (alternator): 94 -> 12.35V parked (calib vs app), ~99-102 -> ~13V
                    # driving; session range 79-110 -> 10.4-14.5V (textbook car range).
                    # data[0] is a fast counter; data[2:4] constant.
                    self._kv(cur, "main_raw", data[1])
                    self._kv(cur, "main_voltage", round(data[1] * 0.13138, 2))
            self._snapshot_metrics(cur)
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

    # frequent keepalive/handshake frames from the server (everything else = candidate command)
    SRV_ACK_TYPES = {0x0e01, 0x0f01, 0x0405, 0x0292, 0x0115, 0x0450, 0x0600}

    def log_srv(self, f):
        """Capture EVERY Car-Online -> device frame (relay) with full hex to
        srv_frames_*.jsonl, and log prominently any non-keepalive frame -- these
        are the candidate commands (arm/disarm/etc. issued from the app)."""
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in f["data"])
        rec = {"ts": now(), "type": "0x%04x" % f["typ"], "ctr": f["ctr"],
               "len": len(f["data"]), "hex": f["data"].hex(), "ascii": asc}
        with self.loglock:
            self.srvfh.write(json.dumps(rec) + "\n")
            self.srvfh.flush()
        if f["typ"] not in self.SRV_ACK_TYPES:
            self.log("<<< SRV frame type=0x%04x ctr=%d len=%d hex=%s |%s|"
                     % (f["typ"], f["ctr"], len(f["data"]), f["data"].hex()[:96], asc[:48]))

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
            if self._jn % 500 == 0:   # keep the raw journal to ~7 days (time-based)
                cut = (datetime.datetime.now() - datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
                cur.execute("DELETE FROM journal WHERE ts < ?", (cut,))
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
                d8 = d[8] | (d[9] << 8); d10 = d[10] | (d[11] << 8)
                tagv = ("tag=%.2fV" % (d[12] * 0.02755)) if 90 <= d[12] <= 106 else ("d12=%d" % d[12])
                state = "VALET" if (d10 & 0x4000) else ("ARMED" if (d8 & 0x0200) else "disarmed")
                return "rec bk=%.2fV %s %s ign=%s%s" % (
                    d[14] * 0.03176, tagv, state, "off" if (d[15] & 0x02) else "on",
                    " moving" if (d10 & 0x0010) else "")
            if typ == 0x0230:
                return "cell %s" % d.decode("ascii", "replace")[:24]
            if typ == 0x0260:
                return "bal %s" % d.decode("ascii", "replace").strip()[:24]
            if typ == 0x0270 and len(d) >= 3:
                return "temp %dC" % (d[2] - 256 if d[2] >= 128 else d[2])
            if typ == 0x0250 and len(d) >= 2:
                return "main %.2fV" % (d[1] * 0.13138)
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
        # Only log an offline/online pair if the device was gone > 60s (a real outage);
        # ignore the frequent quick reloginks / modem-resets that would otherwise spam.
        off = getattr(self, "_offline_since", None)
        if off is not None and (time.time() - off[0]) > 60:
            self._event_locked("conn", "Device offline", ts=off[1])   # when it actually dropped
            self._event_locked("conn", "Device online")               # now, on reconnect
        self._offline_since = None
        try:
            if self.relay:
                self._relay(conn, peer)
            else:
                self._standalone(conn, peer)
        finally:
            self.log("--- disconnect %s" % peer)
            self._offline_since = (time.time(), now())   # decide whether to log on next connect

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
                        send(0x0292, build_time())
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
                    self.log_srv(f)
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
