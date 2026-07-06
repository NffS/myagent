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
import datetime
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DB = "/root/captures/car.db"
AUTH = None  # expected "Basic <base64(user:pass)>" header, or None to disable
# build id = this file's mtime; changes on every deploy so open pages auto-reload
try:
    BUILD = str(int(os.path.getmtime(os.path.abspath(__file__))))
except OSError:
    BUILD = "0"


def q(sql, args=()):
    db = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Fiesta tracker</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache"><meta http-equiv="Expires" content="0">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<link rel="stylesheet" href="https://unpkg.com/uplot@1.6.31/dist/uPlot.min.css">
<script src="https://unpkg.com/uplot@1.6.31/dist/uPlot.iife.min.js"></script>
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
 .chip{display:flex;flex-direction:column;align-items:center;min-width:52px;cursor:pointer}
 .chip:hover{opacity:.7}
 .chip .ic{line-height:0}
 .chip .ic svg{width:22px;height:22px;display:block;color:#3a3a3c}
 .chip .cv{font-weight:600;font-size:14px;margin-top:2px;white-space:nowrap}
 .chip .cl{font-size:9.5px;color:#9a9a9a;text-transform:uppercase;letter-spacing:.3px}
 #map{flex:1 1 auto;width:100%;min-height:200px}
 /* bottom address / armed bar */
 #bottom{padding:9px 16px;background:#fff;border-top:1px solid #e3e3e3;
         display:flex;flex-direction:column;gap:4px}
 .brow{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
 #statetime{font-size:12.5px;color:#666}
 .speedleg{background:rgba(255,255,255,.82);padding:2px 7px;border-radius:6px;font-size:11px;color:#444}
 #armed,#ign{font-weight:700;padding:3px 10px;border-radius:14px;font-size:13px;white-space:nowrap}
 #armed.on,#ign.on{background:#e7f6ec;color:#1c8a4e} #armed.off{background:#fdeaea;color:#c0392b}
 #armed.unk,#ign.unk,#ign.off{background:#eee;color:#888}
 #armed svg,#ign svg{width:15px;height:15px;vertical-align:-3px;margin-right:3px}
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
 /* metric graph modal */
 #gmodal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:1000;align-items:center;justify-content:center}
 #gbox{background:#fff;border-radius:10px;padding:14px 16px;width:680px;max-width:92vw;box-shadow:0 10px 40px rgba(0,0,0,.35)}
 #ghead{display:flex;justify-content:space-between;align-items:center;font-weight:600;margin-bottom:8px}
 #gclose{cursor:pointer;color:#999;font-size:18px;padding:0 6px;line-height:1}
 #gperiods{display:flex;gap:6px;margin-bottom:8px}
 #gperiods button{border:1px solid #ccc;background:#fff;border-radius:6px;padding:3px 10px;cursor:pointer;font-size:12px}
 #gperiods button.on{background:#0b6;color:#fff;border-color:#0b6}
 #gchart{min-height:240px}
 #ghint{font-size:11px;color:#aaa;margin-top:6px}
 .u-legend{font-size:12px}
</style></head><body>
<div id="top">
  <div class="brand">Fiesta<small id="online">connecting…</small></div>
</div>
<div id="map"></div>
<div id="bottom">
  <div class="brow"><span id="armed" class="unk">—</span><span id="ign" class="unk">—</span><span id="statetime"></span></div>
  <div id="addr">locating…</div>
  <div id="evt"></div>
</div>
<div id="jhdr">Journal — <span style="color:#1c8a4e">device→</span> / <span style="color:#2a6fd6">←server</span></div>
<div id="jwrap"><div id="jlist"></div></div>
<div id="gmodal" onclick="if(event.target===this)closeGraph()">
  <div id="gbox"><div id="ghead"><span id="gtitle"></span><span id="gclose" onclick="closeGraph()">✕</span></div>
  <div id="gperiods"><button data-h="24">24h</button><button data-h="168">7d</button><button data-h="720">30d</button><button data-h="2160">90d</button></div>
  <div id="gchart"></div>
  <div id="ghint">drag across the chart to zoom · double-click to reset</div></div>
</div>
<script>
var BUILD='__BUILD__';
var map=L.map('map').setView([0,0],2), marker=null, trackLayer=null, centered=false;
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom:19,attribution:'© OpenStreetMap'}).addTo(map);
trackLayer=L.layerGroup().addTo(map);
// track colour by speed: red (slow) -> green (~55) -> blue (100+ km/h)
function speedColor(kmh){ return 'hsl('+Math.min(240,(kmh||0)*2.4)+',90%,45%)'; }
var legend=L.control({position:'bottomleft'});
legend.onAdd=function(){var d=L.DomUtil.create('div','speedleg');
  d.innerHTML='track: <b style="color:hsl(0,90%,45%)">slow</b> · <b style="color:hsl(120,90%,45%)">~55</b> · <b style="color:hsl(240,90%,45%)">100+ km/h</b>';return d;};
legend.addTo(map);
var pinIcon=new L.Icon.Default();
// bearing (deg) from point a[lat,lon] to b[lat,lon]
function bearing(a,b){var la1=a[0]*Math.PI/180,la2=b[0]*Math.PI/180,dl=(b[1]-a[1])*Math.PI/180;
  var y=Math.sin(dl)*Math.cos(la2),x=Math.cos(la1)*Math.sin(la2)-Math.sin(la1)*Math.cos(la2)*Math.cos(dl);
  return (Math.atan2(y,x)*180/Math.PI+360)%360;}
// heading arrow (points to travel direction) shown while moving
function arrowIcon(deg){return L.divIcon({className:'',iconSize:[30,30],iconAnchor:[15,15],
  html:'<div style="transform:rotate('+deg+'deg)"><svg viewBox="0 0 24 24" width="30" height="30">'+
  '<path d="M12 2l6 18-6-4-6 4z" fill="#1565c0" stroke="#fff" stroke-width="1.3" stroke-linejoin="round"/></svg></div>'});}
var TZ=Intl.DateTimeFormat().resolvedOptions().timeZone||'local';
function localTime(s){ if(!s) return '-'; var d=new Date(String(s).replace(' ','T')+'Z'); return isNaN(d.getTime())?s:d.toLocaleString(); }
function timeOnly(s){ if(!s) return ''; var d=new Date(String(s).replace(' ','T')+'Z'); return isNaN(d.getTime())?s:d.toLocaleTimeString(); }
function num(s){ var m=String(s==null?'':s).match(/-?[\\d.]+/); return m?m[0]:null; }
// inline line-art pictograms (stroke = currentColor) — no emoji
var S='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">';
var ICONS={
 main:S+'<rect x="2" y="8" width="20" height="11" rx="1.5"/><path d="M6 8V5.5M18 8V5.5M6.5 13.5h3M14.5 13.5h3M16 12v3"/></svg>',
 temp:S+'<path d="M14 14.5V5a2 2 0 1 0-4 0v9.5a4 4 0 1 0 4 0z"/><path d="M12 9.5v5.5"/></svg>',
 money:'<svg viewBox="0 0 24 24"><text x="12" y="17.5" text-anchor="middle" font-size="17" font-weight="700" fill="currentColor" font-family="system-ui,Arial">₴</text></svg>',
 signal:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 18v-3M10 18v-6M15 18v-9M20 18v-12"/></svg>',
 sat:S+'<circle cx="12" cy="12" r="2.6"/><path d="M12 3a9 9 0 0 1 9 9M12 21a9 9 0 0 1-9-9M15 12a3 3 0 0 0-3-3"/></svg>',
 backup:S+'<rect x="3" y="9" width="16" height="9" rx="1.5"/><path d="M21 12v3"/><rect x="5.2" y="11" width="8" height="5" rx=".6" fill="currentColor" stroke="none"/></svg>',
 pin:S+'<circle cx="8.5" cy="8.5" r="4.5"/><path d="M11.7 11.7l6.3 6.3M15.5 15.5l2-2M18 18l2-2"/></svg>',
 lock:S+'<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>',
 unlock:S+'<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 7.5-1.8"/></svg>',
 key:S+'<circle cx="12" cy="13.5" r="6.5"/><path d="M12 2.5V9"/></svg>'
};
function chip(icon,val,unit,label,metric){
  if(val==null||val===undefined||val==='') return '';
  return '<div class="chip" data-m="'+metric+'" data-l="'+label+'"><span class="ic">'+icon+
         '</span><span class="cv">'+val+(unit||'')+'</span><span class="cl">'+label+'</span></div>';
}
document.getElementById('top').addEventListener('click',function(e){
  var c=e.target.closest&&e.target.closest('.chip');
  if(c&&c.getAttribute('data-m')) openGraph(c.getAttribute('data-m'),c.getAttribute('data-l'));
});
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
  try{ var bld=(await (await fetch('/api/build',{cache:'no-store'})).text()).trim(); if(bld&&bld!==BUILD){ location.reload(); return; } }catch(e){}
  var d=await (await fetch('/api/latest',{cache:'no-store'})).json();
  var p=d.position, kv=d.kv||{};
  var stale = kv.last_seen ? (Date.now()-new Date(kv.last_seen.replace(' ','T')+'Z').getTime())>120000 : true;
  document.getElementById('online').textContent=(stale?'offline':'online');
  document.getElementById('online').style.color=stale?'#c0392b':'#3aa76d';
  // rebuild pictogram bar
  var top=document.getElementById('top');
  top.querySelectorAll('.chip').forEach(function(n){n.remove();});
  top.insertAdjacentHTML('beforeend',
    chip(ICONS.main, kv.main_voltage, ' V', 'main', 'main_voltage')+
    chip(ICONS.temp, kv.temperature, ' °C', 'temp', 'temperature')+
    chip(ICONS.money, num(kv.sim_balance), '', 'balance', 'balance')+
    chip(ICONS.signal, kv.signal_dbm, ' dBm', 'signal', 'signal_dbm')+
    chip(ICONS.sat, kv.satellites, '', 'sats', 'satellites')+
    chip(ICONS.backup, kv.backup_voltage, ' V', 'backup', 'backup_voltage')+
    chip(ICONS.pin, kv.pin_voltage, ' V', 'tag', 'tag_voltage'));
  // armed state (decoded into kv.armed when available)
  var a=document.getElementById('armed'), av=(kv.armed||'').toLowerCase();
  if(av.indexOf('arm')>=0 && av.indexOf('dis')<0){ a.innerHTML=ICONS.lock+'Armed'; a.className='on'; }
  else if(av.indexOf('dis')>=0 || av==='off'){ a.innerHTML=ICONS.unlock+'Disarmed'; a.className='off'; }
  else { a.innerHTML=ICONS.lock+'—'; a.className='unk'; }
  var ig=document.getElementById('ign'), iv=(kv.ignition||'').toLowerCase();
  if(iv==='on'){ ig.innerHTML=ICONS.key+'Ignition on'; ig.className='on'; }
  else if(iv==='off'){ ig.innerHTML=ICONS.key+'Ignition off'; ig.className='off'; }
  else { ig.innerHTML=ICONS.key+'—'; ig.className='unk'; }
  document.getElementById('statetime').textContent = kv.last_seen? localTime(kv.last_seen) : (p?localTime(p.dev_time||p.recv_ts):'');
  document.getElementById('evt').textContent =
    (p?'':'waiting for data')+
    (d.speed_kmh!=null? d.speed_kmh+' km/h':'')+
    (kv.moving==='yes'?' · moving':'');
  var tr=await (await fetch('/api/track',{cache:'no-store'})).json();
  trackLayer.clearLayers();
  for(var i=1;i<tr.length;i++){
    L.polyline([[tr[i-1][0],tr[i-1][1]],[tr[i][0],tr[i][1]]],
      {color:speedColor(tr[i][2]),weight:4,opacity:.9}).addTo(trackLayer);
  }
  if(p){ var ll=[p.lat,p.lon];
    var moving=(kv.moving==='yes')||((d.speed_kmh||0)>3);
    var hd=(tr.length>=2)?bearing(tr[tr.length-2],tr[tr.length-1]):null;
    if(!marker){marker=L.marker(ll).addTo(map);}
    marker.setLatLng(ll);
    marker.setIcon(moving&&hd!=null?arrowIcon(hd):pinIcon);
    if(!centered){map.setView(ll,16);centered=true;}
    geocode(p.lat,p.lon); }
  var jr=await (await fetch('/api/journal',{cache:'no-store'})).json();
  document.getElementById('jlist').innerHTML=jr.map(function(e){
    var dev=e.dir==='device';
    return '<div class="jr"><span class="jt">'+timeOnly(e.ts)+'</span>'+
      '<span class="jd '+(dev?'dev':'srv')+'">'+(dev?'DEV →':'← SRV')+'</span>'+
      '<span class="js">'+e.summary+'</span></div>';
  }).join('');
 }catch(e){ document.getElementById('online').textContent='error: '+e; }
}
var uplotInst=null, gMetric=null, gLabel=null, gHours=24;
function openGraph(metric,label){
  gMetric=metric; gLabel=label; gHours=24;
  document.getElementById('gtitle').textContent=label;
  document.getElementById('gmodal').style.display='flex';
  markPeriod(); loadGraph();
}
function closeGraph(){
  document.getElementById('gmodal').style.display='none';
  if(uplotInst){ uplotInst.destroy(); uplotInst=null; }
}
function markPeriod(){
  var bs=document.querySelectorAll('#gperiods button');
  for(var i=0;i<bs.length;i++){ bs[i].className=(parseFloat(bs[i].getAttribute('data-h'))===gHours)?'on':''; }
}
document.getElementById('gperiods').addEventListener('click',function(e){
  if(e.target.tagName==='BUTTON'){ gHours=parseFloat(e.target.getAttribute('data-h')); markPeriod(); loadGraph(); }
});
function loadGraph(){
  var chart=document.getElementById('gchart');
  chart.innerHTML='<div style="padding:40px;color:#888">loading…</div>';
  fetch('/api/metric?name='+encodeURIComponent(gMetric)+'&hours='+gHours,{cache:'no-store'})
    .then(function(r){return r.json();})
    .then(function(data){
      if(uplotInst){ uplotInst.destroy(); uplotInst=null; }
      chart.innerHTML='';
      if(!data[0]||!data[0].length){ chart.innerHTML='<div style="padding:40px;color:#888">No data in this period yet — it fills in as data is collected (~1 point/min).</div>'; return; }
      var w=Math.max(300,(chart.clientWidth||640));
      var opts={ width:w, height:300, scales:{x:{time:true}},
        series:[ {}, {label:gLabel, stroke:'#0b6', width:2} ],
        cursor:{ drag:{x:true,y:false} } };
      uplotInst=new uPlot(opts,data,chart);
    })
    .catch(function(e){ chart.innerHTML='<div style="padding:40px;color:#c0392b">error: '+e+'</div>'; });
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
                self._send(200, PAGE.replace("__BUILD__", BUILD), "text/html; charset=utf-8")
            elif self.path.startswith("/api/build"):
                self._send(200, BUILD, "text/plain")
            elif self.path.startswith("/api/metric"):
                qs = parse_qs(urlparse(self.path).query)
                name = (qs.get("name") or [""])[0]
                try:
                    hours = float((qs.get("hours") or ["24"])[0])
                except ValueError:
                    hours = 24.0
                cutoff = (datetime.datetime.now() - datetime.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
                rows = q("SELECT ts,value FROM metrics WHERE name=? AND ts>=? ORDER BY id", (name, cutoff))
                step = max(1, len(rows) // 1500)  # downsample to <=1500 points
                epoch0 = datetime.datetime(1970, 1, 1)
                xs, ys = [], []
                for i in range(0, len(rows), step):
                    try:
                        e = (datetime.datetime.strptime(rows[i][0][:19], "%Y-%m-%d %H:%M:%S") - epoch0).total_seconds()
                    except ValueError:
                        continue
                    xs.append(e)
                    ys.append(rows[i][1])
                self._send(200, json.dumps([xs, ys]), "application/json")
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
                rows = q("SELECT lat,lon,speed_knots FROM position ORDER BY id DESC LIMIT 400")
                self._send(200, json.dumps([[r[0], r[1], round((r[2] or 0) * 1.852, 1)]
                                            for r in rows][::-1]), "application/json")
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
