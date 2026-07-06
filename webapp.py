#!/usr/bin/env python3
"""
webapp.py - simple live dashboard for the AgentMS3 tracker.

Reads the SQLite DB that carserver.py writes (position / telemetry / kv) and
serves a one-page dashboard: an OpenStreetMap map with the car's latest
position + track, and a panel of live values (speed, last fix, SIM balance,
cell, firmware, last-seen). Auto-refreshes every few seconds.

Routes:
  GET /            dashboard HTML
  GET /api/latest  JSON: latest position + kv values
  GET /api/track   JSON: recent positions [[lat,lon],...] for the polyline

Stdlib only.  python3 webapp.py [--port 80] [--db /root/captures/car.db]
"""

import argparse
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = "/root/captures/car.db"


def q(sql, args=()):
    db = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>AgentMS3 tracker</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
 body{font-family:system-ui,Arial,sans-serif;margin:0;color:#1c1c1e}
 header{padding:10px 16px;background:#0b3d2e;color:#fff}
 header b{font-size:17px} #st{float:right;font-size:13px;opacity:.85}
 #map{height:58vh;width:100%}
 .panel{padding:14px 16px}
 table{border-collapse:collapse;width:100%;max-width:620px}
 td{padding:6px 10px;border-bottom:1px solid #eee;font-size:14px}
 td.k{color:#888;width:190px} td.v{font-weight:600}
 .big{font-size:20px}
</style></head><body>
<header><b>🚗 AgentMS3 — live tracker</b><span id="st">connecting…</span></header>
<div id="map"></div>
<div class="panel"><table id="tbl"></table></div>
<script>
var map=L.map('map').setView([0,0],2), marker=null, line=null, centered=false;
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom:19,attribution:'© OpenStreetMap'}).addTo(map);
function row(k,v,big){return '<tr><td class="k">'+k+'</td><td class="v'+(big?' big':'')+'">'+(v==null?'-':v)+'</td></tr>';}
async function tick(){
 try{
  var d=await (await fetch('/api/latest',{cache:'no-store'})).json();
  var p=d.position, kv=d.kv||{};
  document.getElementById('st').textContent = p? ('last fix '+(p.dev_time||p.recv_ts)) : 'waiting for data';
  document.getElementById('tbl').innerHTML =
    row('Position', p? (p.lat.toFixed(6)+', '+p.lon.toFixed(6)):null, true)+
    row('Speed', d.speed_kmh!=null? d.speed_kmh+' km/h':null, true)+
    row('Main voltage', kv.main_voltage? kv.main_voltage+' V':null, true)+
    row('Backup battery', kv.backup_voltage? kv.backup_voltage+' V':null)+
    row('Temperature', kv.temperature!=null&&kv.temperature!==undefined? kv.temperature+' °C':null)+
    row('Last fix (device UTC)', p?p.dev_time:null)+
    row('Received', p?p.recv_ts:null)+
    row('SIM balance', kv.sim_balance)+
    row('Cell MCC,MNC,LAC,CID', kv.last_cell)+
    row('Firmware', kv.version)+
    row('Device id', kv.device_id)+
    row('Last seen', kv.last_seen)+
    row('Fixes stored', d.positions);
  if(p){ var ll=[p.lat,p.lon];
    if(!marker){marker=L.marker(ll).addTo(map);} else {marker.setLatLng(ll);}
    if(!centered){map.setView(ll,15);centered=true;} }
  var tr=await (await fetch('/api/track',{cache:'no-store'})).json();
  if(tr.length){ if(line){line.remove();} line=L.polyline(tr,{color:'#0b6',weight:3}).addTo(map);}
 }catch(e){ document.getElementById('st').textContent='error: '+e; }
}
tick(); setInterval(tick,5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        try:
            if self.path == "/" or self.path.startswith("/?") or self.path.startswith("/index"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif self.path.startswith("/api/latest"):
                kv = {k: v for k, v in q("SELECT k,v FROM kv")}
                rows = q("SELECT recv_ts,dev_time,lat,lon,speed_knots,course "
                         "FROM position ORDER BY id DESC LIMIT 1")
                pos = None
                if rows:
                    r = rows[0]
                    pos = {"recv_ts": r[0], "dev_time": r[1], "lat": r[2],
                           "lon": r[3], "speed_knots": r[4], "course": r[5]}
                cnt = q("SELECT count(*) FROM position")[0][0]
                out = {"kv": kv, "position": pos, "positions": cnt,
                       "speed_kmh": round(pos["speed_knots"] * 1.852, 1) if pos else None}
                self._send(200, json.dumps(out), "application/json")
            elif self.path.startswith("/api/track"):
                rows = q("SELECT lat,lon FROM position ORDER BY id DESC LIMIT 400")
                self._send(200, json.dumps([[r[0], r[1]] for r in rows][::-1]),
                           "application/json")
            else:
                self._send(404, "not found", "text/plain")
        except Exception as e:
            self._send(500, "error: %s" % e, "text/plain")

    def log_message(self, *a):
        pass


def main():
    global DB
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--db", default=DB)
    a = ap.parse_args()
    DB = a.db
    print("webapp on :%d  (db %s)" % (a.port, DB), flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
