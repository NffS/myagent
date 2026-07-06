#!/usr/bin/env python3
"""
webapp.py - live dashboard for the AgentMS3 tracker, laid out like the
Car-Online app: a top row of pictograms (main/backup/pin voltage, temperature,
SIM balance, signal, satellites), a map in the middle, and a bottom bar with
the current street address (reverse-geocoded to English) + armed state + time.
A journal panel underneath shows recent protocol messages with their direction
(device -> us, and Car-Online server -> device when relaying).

Reads the SQLite DB that carserver.py writes (position / telemetry / kv /
journal). Stdlib only.

Routes:
  GET /             dashboard HTML
  GET /api/latest   JSON: latest position + kv values
  GET /api/track    JSON: recent positions [[lat,lon],...] for the polyline
  GET /api/journal  JSON: recent journal messages [{ts,dir,summary},...]

  python3 webapp.py [--port 3322] [--db /root/captures/car.db] [--auth user:pass]
"""

import argparse
import base64
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = "/root/captures/car.db"
AUTH = None  # expected "Basic <base64(user:pass)>" header, or None to disable


def q(sql, args=()):
    db = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Fiesta tracker</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
 *{box-sizing:border-box}
 html,body{height:100%}
 body{font-family:system-ui,Arial,sans-serif;margin:0;color:#1c1c1e;
      display:flex;flex-direction:column;height:100vh}
 /* top pictogram bar */
 #top{display:flex;flex-wrap:wrap;align-items:center;gap:6px 18px;
      padding:8px 16px;background:#fff;border-bottom:1px solid #e3e3e3}
 #top .brand{font-weight:700;margin-right:8px;display:flex;flex-direction:column;line-height:1.1}
 #top .brand small{font-weight:400;color:#3aa76d;font-size:11px}
 .chip{display:flex;flex-direction:column;align-items:center;min-width:52px}
 .chip .ic{line-height:0}
 .chip .ic svg{width:22px;height:22px;display:block;color:#3a3a3c}
 .chip .cv{font-weight:600;font-size:14px;margin-top:2px;white-space:nowrap}
 .chip .cl{font-size:9.5px;color:#9a9a9a;text-transform:uppercase;letter-spacing:.3px}
 #map{flex:1 1 auto;width:100%;min-height:200px}
 /* bottom address / armed bar */
 #bottom{padding:9px 16px;background:#fff;border-top:1px solid #e3e3e3;
         display:flex;align-items:center;gap:12px}
 #armed{font-weight:700;padding:3px 10px;border-radius:14px;font-size:13px;white-space:nowrap}
 #armed.on{background:#e7f6ec;color:#1c8a4e} #armed.off{background:#fdeaea;color:#c0392b}
 #armed.unk{background:#eee;color:#888}
 #armed svg{width:15px;height:15px;vertical-align:-3px;margin-right:3px}
 #addrwrap{flex:1 1 auto;min-width:0}
 #addr{font-size:13.5px;line-height:1.25} #evt{font-size:12px;color:#888}
 /* journal */
 #jwrap{max-height:26vh;overflow:auto;background:#fafafa;border-top:1px solid #e3e3e3;
        font-family:ui-monospace,Consolas,monospace;font-size:12px}
 #jhdr{position:sticky;top:0;background:#f0f0f0;padding:4px 16px;font-weight:600;
       color:#555;border-bottom:1px solid #e3e3e3;font-family:system-ui}
 .jr{display:flex;gap:10px;padding:2px 16px;border-bottom:1px solid #f0f0f0}
 .jt{color:#aaa;flex:0 0 88px} .js{color:#333;overflow:hidden;text-overflow:ellipsis}
 .jd{flex:0 0 62px;font-weight:700}
 .jd.dev{color:#1c8a4e} .jd.srv{color:#2a6fd6}
</style></head><body>
<div id="top">
  <div class="brand">Fiesta<small id="online">connecting…</small></div>
</div>
<div id="map"></div>
<div id="bottom">
  <span id="armed" class="unk">—</span>
  <div id="addrwrap"><div id="addr">locating…</div><div id="evt"></div></div>
</div>
<div id="jhdr">Journal — <span style="color:#1c8a4e">device→</span> / <span style="color:#2a6fd6">←server</span></div>
<div id="jwrap"><div id="jlist"></div></div>
<script>
var map=L.map('map').setView([0,0],2), marker=null, line=null, centered=false;
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom:19,attribution:'© OpenStreetMap'}).addTo(map);
var TZ=Intl.DateTimeFormat().resolvedOptions().timeZone||'local';
function localTime(s){ if(!s) return '-'; var d=new Date(String(s).replace(' ','T')+'Z'); return isNaN(d.getTime())?s:d.toLocaleString(); }
function timeOnly(s){ if(!s) return ''; var d=new Date(String(s).replace(' ','T')+'Z'); return isNaN(d.getTime())?s:d.toLocaleTimeString(); }
function num(s){ var m=String(s==null?'':s).match(/-?[\\d.]+/); return m?m[0]:null; }
// inline line-art pictograms (stroke = currentColor) — no emoji
var S='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">';
var ICONS={
 main:S+'<rect x="2" y="8" width="20" height="11" rx="1.5"/><path d="M6 8V5.5M18 8V5.5M6.5 13.5h3M14.5 13.5h3M16 12v3"/></svg>',
 temp:S+'<path d="M14 14.5V5a2 2 0 1 0-4 0v9.5a4 4 0 1 0 4 0z"/><path d="M12 9.5v5.5"/></svg>',
 money:S+'<circle cx="12" cy="12" r="8.5"/><path d="M12 7.4v9.2M9.6 10h4.8M9.6 13.6h4.8"/></svg>',
 signal:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 18v-3M10 18v-6M15 18v-9M20 18v-12"/></svg>',
 sat:S+'<circle cx="12" cy="12" r="2.6"/><path d="M12 3a9 9 0 0 1 9 9M12 21a9 9 0 0 1-9-9M15 12a3 3 0 0 0-3-3"/></svg>',
 backup:S+'<rect x="3" y="9" width="16" height="9" rx="1.5"/><path d="M21 12v3"/><rect x="5.2" y="11" width="8" height="5" rx=".6" fill="currentColor" stroke="none"/></svg>',
 pin:S+'<circle cx="8.5" cy="8.5" r="4.5"/><path d="M11.7 11.7l6.3 6.3M15.5 15.5l2-2M18 18l2-2"/></svg>',
 lock:S+'<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>',
 unlock:S+'<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 7.5-1.8"/></svg>'
};
function chip(icon,val,unit,label){
  if(val==null||val===undefined||val==='') return '';
  return '<div class="chip"><span class="ic">'+icon+'</span><span class="cv">'+val+(unit||'')+
         '</span><span class="cl">'+label+'</span></div>';
}
// reverse-geocode to English, only when the position moves noticeably
var lastGeo=null;
async function geocode(lat,lon){
  if(lastGeo && Math.abs(lastGeo[0]-lat)<5e-4 && Math.abs(lastGeo[1]-lon)<5e-4) return;
  lastGeo=[lat,lon];
  try{
    var j=await (await fetch('https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat='+
        lat+'&lon='+lon+'&accept-language=en&zoom=18',{cache:'no-store'})).json();
    if(j && j.display_name) document.getElementById('addr').textContent=j.display_name;
  }catch(e){}
}
async function tick(){
 try{
  var d=await (await fetch('/api/latest',{cache:'no-store'})).json();
  var p=d.position, kv=d.kv||{};
  var stale = kv.last_seen ? (Date.now()-new Date(kv.last_seen.replace(' ','T')+'Z').getTime())>120000 : true;
  document.getElementById('online').textContent=(stale?'offline':'online')+' · '+TZ;
  document.getElementById('online').style.color=stale?'#c0392b':'#3aa76d';
  // rebuild pictogram bar
  var top=document.getElementById('top');
  top.querySelectorAll('.chip').forEach(function(n){n.remove();});
  top.insertAdjacentHTML('beforeend',
    chip(ICONS.main, kv.main_voltage, ' V', 'main')+
    chip(ICONS.temp, kv.temperature, ' °C', 'temp')+
    chip(ICONS.money, num(kv.sim_balance), '', 'balance')+
    chip(ICONS.signal, kv.signal_dbm, ' dBm', 'signal')+
    chip(ICONS.sat, kv.satellites, '', 'sats')+
    chip(ICONS.backup, kv.backup_voltage, ' V', 'backup')+
    chip(ICONS.pin, kv.pin_voltage, ' V', 'pin'));
  // armed state (decoded into kv.armed when available)
  var a=document.getElementById('armed'), av=(kv.armed||'').toLowerCase();
  if(av.indexOf('arm')>=0 && av.indexOf('dis')<0){ a.innerHTML=ICONS.lock+'Armed'; a.className='on'; }
  else if(av.indexOf('dis')>=0 || av==='off'){ a.innerHTML=ICONS.unlock+'Disarmed'; a.className='off'; }
  else { a.innerHTML=ICONS.lock+'—'; a.className='unk'; }
  document.getElementById('evt').textContent =
    (p?('fix '+localTime(p.dev_time||p.recv_ts)):'waiting for data')+
    (kv.speed_kmh!=null?'  ·  '+kv.speed_kmh+' km/h':'')+
    (d.speed_kmh!=null?'  ·  '+d.speed_kmh+' km/h':'');
  if(p){ var ll=[p.lat,p.lon];
    if(!marker){marker=L.marker(ll).addTo(map);} else {marker.setLatLng(ll);}
    if(!centered){map.setView(ll,16);centered=true;}
    geocode(p.lat,p.lon); }
  var tr=await (await fetch('/api/track',{cache:'no-store'})).json();
  if(tr.length){ if(line){line.remove();} line=L.polyline(tr,{color:'#0b6',weight:3}).addTo(map);}
  var jr=await (await fetch('/api/journal',{cache:'no-store'})).json();
  document.getElementById('jlist').innerHTML=jr.map(function(e){
    var dev=e.dir==='device';
    return '<div class="jr"><span class="jt">'+timeOnly(e.ts)+'</span>'+
      '<span class="jd '+(dev?'dev':'srv')+'">'+(dev?'DEV →':'← SRV')+'</span>'+
      '<span class="js">'+e.summary+'</span></div>';
  }).join('');
 }catch(e){ document.getElementById('online').textContent='error: '+e; }
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

    def _authed(self):
        if not AUTH:
            return True
        return self.headers.get("Authorization", "") == AUTH

    def do_GET(self):
        if not self._authed():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Fiesta tracker"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
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
            elif self.path.startswith("/api/journal"):
                rows = q("SELECT ts,dir,summary FROM journal ORDER BY id DESC LIMIT 60")
                self._send(200, json.dumps([{"ts": r[0], "dir": r[1], "summary": r[2]}
                                            for r in rows]), "application/json")
            else:
                self._send(404, "not found", "text/plain")
        except Exception as e:
            self._send(500, "error: %s" % e, "text/plain")

    def log_message(self, *a):
        pass


def main():
    global DB, AUTH
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=3322)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--auth", default=None, help="require HTTP Basic auth, 'user:pass'")
    a = ap.parse_args()
    DB = a.db
    if a.auth:
        AUTH = "Basic " + base64.b64encode(a.auth.encode()).decode()
    print("webapp on :%d  (db %s)  auth=%s" % (a.port, DB, "on" if AUTH else "off"), flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
