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
 #mapwrap{position:relative;flex:1 1 auto;min-height:200px}
 #map{position:absolute;inset:0;width:100%;height:100%}
 /* big state badges overlaid on the map */
 /* straddle the map/panel seam: icon on the seam, caption below it on the panel */
 #mapbadges{position:absolute;left:12px;bottom:-40px;z-index:800;display:flex;gap:14px;pointer-events:none}
 .mbadge{display:flex;flex-direction:column;align-items:center;gap:3px}
 .mbadge .disc{width:56px;height:56px;border-radius:18px;display:flex;align-items:center;justify-content:center;color:#fff;background:#9e9e9e;box-shadow:0 3px 12px rgba(0,0,0,.32)}
 .mbadge .disc svg{width:30px;height:30px;stroke-width:2}
 .mbadge .cap{font-size:11px;font-weight:600;color:#444;white-space:nowrap;line-height:1.1}
 .mbadge.on .disc{background:#12a594}
 .mbadge.off .disc{background:#2a6fd6}
 .mbadge.alarm .disc{background:#d32f2f}
 .mbadge.valet .disc{background:#e08600}
 .mbadge.unk .disc,.mbadge.ignoff .disc{background:#9e9e9e}
 /* track time-range selector */
 #trackbar{display:none;align-items:center;gap:6px;flex-wrap:wrap;padding:6px 12px;background:#fff;border-bottom:1px solid #e3e3e3;font-size:12px}
 #trackbar.show{display:flex}
 #trackbtn{background:none;border:none;cursor:pointer;color:#333;padding:0;line-height:0;display:flex;align-items:center}
 #trackbtn svg{width:22px;height:22px}
 #trackbtn.on{color:#0b6}
 #trackbar .tlabel{color:#888;font-weight:700;margin-right:2px}
 #trackbar .tbtn{border:1px solid #ccc;background:#fff;border-radius:6px;padding:3px 9px;cursor:pointer;color:#444;white-space:nowrap;font-family:system-ui;font-size:12px}
 #trackbar .tbtn:hover{border-color:#0b6}
 #trackbar .tbtn.on{background:#0b6;color:#fff;border-color:#0b6}
 #tbcustom{display:none;align-items:center;gap:4px}
 #tbcustom.show{display:inline-flex}
 #tbcustom input{font-size:12px;padding:2px 4px;border:1px solid #ccc;border-radius:5px}
 #tbinfo{color:#999;margin-left:auto;white-space:nowrap;font-size:11px}
 /* bottom address / armed bar */
 #bottom{padding:46px 16px 9px;background:#fff;border-top:1px solid #e3e3e3;
         display:flex;flex-direction:column;gap:4px}
 .brow{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
 #statetime{font-size:12.5px;color:#666}
 .speedleg{background:rgba(255,255,255,.82);padding:2px 7px;border-radius:6px;font-size:11px;color:#444}
 /* (armed/ignition now shown as #mapbadges overlaid on the map) */
 #addrwrap{flex:1 1 auto;min-width:0}
 #addr{font-size:13.5px;line-height:1.25} #evt{font-size:12px;color:#888}
 /* journal */
 .pane{max-height:26vh;overflow:auto;background:#fafafa;border-top:1px solid #e3e3e3;
       font-family:ui-monospace,Consolas,monospace;font-size:12px}
 #tabs{display:flex;background:#f0f0f0;border-top:1px solid #e3e3e3}
 #tabs .tab{border:none;background:none;padding:6px 16px;cursor:pointer;font-size:13px;color:#666;border-bottom:2px solid transparent;font-family:system-ui}
 #tabs .tab.on{color:#0b6;border-bottom-color:#0b6;font-weight:600}
 .er{display:flex;gap:10px;padding:2px 16px;border-bottom:1px solid #f0f0f0}
 .et{color:#aaa;flex:0 0 150px} .ee{font-weight:600}
 .ek-security{color:#c77700} .ek-ignition{color:#2a6fd6} .ek-door{color:#8a6d1c} .ek-conn{color:#999} .ek-motion{color:#1c8a4e} .ek-engine{color:#0b6} .ek-label{color:#7b1fa2}
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
 /* control buttons */
 #menubtn{background:none;border:none;font-size:20px;cursor:pointer;color:#333;padding:0 8px 0 0;line-height:1}
 #sidebar{position:fixed;top:0;left:0;height:100%;width:250px;max-width:80vw;background:#fff;box-shadow:2px 0 14px rgba(0,0,0,.25);transform:translateX(-100%);transition:transform .2s ease;z-index:1100;padding:14px 16px;overflow:auto}
 #sidebar.open{transform:translateX(0)}
 #sbback{display:none;position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:1099}
 #sbback.open{display:block}
 .sbhead{display:flex;justify-content:space-between;align-items:center;font-weight:700;font-size:15px;margin-bottom:2px}
 .sbhead button{background:none;border:none;font-size:18px;color:#999;cursor:pointer}
 .cbtns{display:flex;flex-direction:column;gap:2px;margin-top:8px}
 .cbtn{display:flex;align-items:center;gap:12px;border:none;background:none;cursor:pointer;padding:5px 6px;width:100%;text-align:left;font-size:13.5px;color:#222;border-radius:8px}
 .cbtn:hover{background:#f2f2f2} .cbtn:active{background:#e8e8e8}
 .cico{width:38px;height:38px;border-radius:50%;display:flex;align-items:center;justify-content:center;flex-shrink:0}
 .cico svg{width:21px;height:21px;fill:none;stroke:#fff;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}
 .cico.g{background:#12a594} .cico.k{background:#9e9e9e} .cico.o{background:#f4511e}
 #ctoast{font-size:12px;color:#777;margin-top:10px;min-height:16px}
 .gtip{position:absolute;pointer-events:none;background:#0b6;color:#fff;font-size:11px;font-weight:600;padding:1px 6px;border-radius:4px;transform:translate(10px,-26px);white-space:nowrap;z-index:5}
</style></head><body>
<div id="top">
  <button id="menubtn" title="Controls">☰</button>
  <button id="trackbtn" title="Track period"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg></button>
  <div class="brand">Fiesta<small id="online">connecting…</small></div>
</div>
<div id="trackbar">
  <span class="tlabel">Track</span>
  <button class="tbtn on" data-mode="h:1">1 h</button>
  <button class="tbtn" data-mode="h:24">24 h</button>
  <button class="tbtn" data-mode="prevday">Yesterday</button>
  <button class="tbtn" data-mode="prevweek">Prev. week</button>
  <button class="tbtn" data-mode="prevmonth">Prev. month</button>
  <button class="tbtn" data-mode="custom">Custom…</button>
  <span id="tbcustom">
    <input type="datetime-local" id="tbfrom">
    <span style="color:#888">→</span>
    <input type="datetime-local" id="tbto">
    <button class="tbtn" id="tbapply">Apply</button>
  </span>
  <span id="tbinfo"></span>
</div>
<div id="mapwrap">
  <div id="map"></div>
  <div id="mapbadges">
    <div class="mbadge unk" id="mb_armed"><div class="disc"></div><div class="cap"></div></div>
    <div class="mbadge unk" id="mb_move"><div class="disc"></div><div class="cap"></div></div>
    <div class="mbadge unk" id="mb_label"><div class="disc"></div><div class="cap"></div></div>
  </div>
</div>
<div id="bottom">
  <div class="brow"><span id="statetime"></span></div>
  <div id="addr">locating…</div>
  <div id="evt"></div>
</div>
<div id="sbback"></div>
<div id="sidebar">
  <div class="sbhead"><span>Controls</span><button id="sbclose" title="close">✕</button></div>
  <div style="font-size:11px;color:#aaa">not wired to the device yet</div>
  <div class="cbtns">
    <button data-cmd="search" class="cbtn"><span class="cico g"><svg viewBox="0 0 24 24"><path d="M12 21s6-5.7 6-11a6 6 0 1 0-12 0c0 5.3 6 11 6 11z"/><circle cx="12" cy="10" r="2.3"/></svg></span>Search car</button>
    <button data-cmd="doors_open" class="cbtn"><span class="cico g"><svg viewBox="0 0 24 24"><path d="M5 19l1-8.5 7-2.5 5 2V19z"/><path d="M8.5 9.5V13H15"/><circle cx="15" cy="15.6" r=".7" fill="#fff" stroke="none"/></svg></span>Doors open</button>
    <button data-cmd="doors_close" class="cbtn"><span class="cico k"><svg viewBox="0 0 24 24"><path d="M5 19l1-8.5 7-2.5 5 2V19z"/><path d="M8.5 9.5V13H15"/><circle cx="15" cy="15.6" r=".7" fill="#fff" stroke="none"/></svg></span>Doors close</button>
    <button data-cmd="arm" class="cbtn"><span class="cico g"><svg viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg></span>Arm</button>
    <button data-cmd="disarm" class="cbtn"><span class="cico k"><svg viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 7.5-1.8"/></svg></span>Disarm</button>
    <button data-cmd="valet_on" class="cbtn"><span class="cico g"><svg viewBox="0 0 24 24"><path d="M3.5 17l1.3-4.5h8l2.5 3.5"/><path d="M2.5 17h13"/><circle cx="6" cy="18" r="1.1"/><circle cx="13" cy="18" r="1.1"/><circle cx="18.5" cy="9.5" r="1.5"/><path d="M18.5 11.5v3M17 13h3"/></svg></span>Valet mode on</button>
    <button data-cmd="valet_off" class="cbtn"><span class="cico k"><svg viewBox="0 0 24 24"><path d="M3.5 17l1.3-4.5h8l2.5 3.5"/><path d="M2.5 17h13"/><circle cx="6" cy="18" r="1.1"/><circle cx="13" cy="18" r="1.1"/><circle cx="18.5" cy="9.5" r="1.5"/><path d="M18.5 11.5v3M17 13h3"/></svg></span>Valet mode off</button>
    <button data-cmd="motor_on" class="cbtn"><span class="cico g"><svg viewBox="0 0 24 24"><path d="M4 10h7l2-2h3v3h2l2 2v3h-4v2H9l-2-2H4z"/><path d="M7 10V8h3"/></svg></span>Motor on</button>
    <button data-cmd="motor_off" class="cbtn"><span class="cico k"><svg viewBox="0 0 24 24"><path d="M4 10h7l2-2h3v3h2l2 2v3h-4v2H9l-2-2H4z"/><path d="M7 10V8h3"/></svg></span>Motor off</button>
    <button data-cmd="trunk_open" class="cbtn"><span class="cico g"><svg viewBox="0 0 24 24"><path d="M3.5 17l1.3-4.5h6.5l1 .5"/><path d="M2.5 17h12"/><circle cx="6" cy="18" r="1.1"/><circle cx="12.5" cy="18" r="1.1"/><path d="M11.5 12.5l8-4.5"/></svg></span>Open trunk</button>
    <button data-cmd="block" class="cbtn"><span class="cico o"><svg viewBox="0 0 24 24"><path d="M3 10h6l2-2h2v3h1l2 2"/><path d="M3 10v5h8"/><rect x="14" y="14" width="7" height="6" rx="1"/><path d="M15.5 14v-1.3a2 2 0 0 1 4 0V14"/></svg></span>Remote blocking</button>
  </div>
  <div id="ctoast"></div>
</div>
<div id="tabs">
  <button class="tab on" data-tab="events">Events</button>
  <button class="tab" data-tab="journal">Raw journal</button>
</div>
<div id="ewrap" class="pane"><div id="elist"></div></div>
<div id="jwrap" class="pane" style="display:none"><div id="jlist"></div></div>
<div id="gmodal" onclick="if(event.target===this)closeGraph()">
  <div id="gbox"><div id="ghead"><span id="gtitle"></span><span id="gclose" onclick="closeGraph()">✕</span></div>
  <div id="gperiods"><button data-h="6">6h</button><button data-h="12">12h</button><button data-h="24">24h</button><button data-h="168">7d</button><button data-h="720">30d</button><button data-h="2160">90d</button></div>
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
var legend=L.control({position:'bottomright'});
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
// small direction arrow placed along the track (points to travel direction)
function trackArrowIcon(deg){return L.divIcon({className:'',iconSize:[15,15],iconAnchor:[7.5,7.5],
  html:'<div style="transform:rotate('+deg+'deg)"><svg viewBox="0 0 24 24" width="15" height="15">'+
  '<path d="M12 4l6 15-6-3.2-6 3.2z" fill="#fff" stroke="#222" stroke-width="1.7" stroke-linejoin="round"/></svg></div>'});}
var TZ=Intl.DateTimeFormat().resolvedOptions().timeZone||'local';
function z2(n){return (n<10?'0':'')+n;}
function localTime(s){ if(!s) return '-'; var d=new Date(String(s).replace(' ','T')+'Z'); if(isNaN(d.getTime())) return s;
  return z2(d.getDate())+'/'+z2(d.getMonth()+1)+'/'+d.getFullYear()+', '+z2(d.getHours())+':'+z2(d.getMinutes())+':'+z2(d.getSeconds()); }
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
 key:S+'<circle cx="12" cy="13.5" r="6.5"/><path d="M12 2.5V9"/></svg>',
 engine:S+'<rect x="8" y="8.5" width="9.5" height="7.5" rx="1.5"/><rect x="10" y="5.5" width="5" height="3.2" rx=".8"/><circle cx="5" cy="12.3" r="2"/><path d="M7 12.3h1"/><path d="M17.5 11h2.5v3h-2.5"/><path d="M11 16v-2M14 16v-2"/></svg>',
 car:S+'<rect x="3.5" y="9.5" width="17" height="4.3" rx="1.3"/><path d="M7 9.6L9.2 6h5.6l2.2 3.6"/><circle cx="8" cy="14.2" r="1.7"/><circle cx="16" cy="14.2" r="1.7"/></svg>'
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
// ---- track time-range selector ----
var trackMode={type:'roll',h:1};   // default: last 1 hour
function rangeEpochs(){
  var now=Date.now();
  if(trackMode.type==='roll') return [Math.floor((now-trackMode.h*3600000)/1000), Math.floor(now/1000)];
  return [trackMode.from, trackMode.to];
}
function fmtEpoch(e){var d=new Date(e*1000);return z2(d.getDate())+'/'+z2(d.getMonth()+1)+'/'+d.getFullYear()+' '+z2(d.getHours())+':'+z2(d.getMinutes());}
var lastTrackSig=null;
async function drawTrack(fit){
  var r=rangeEpochs(), tr;
  try{ tr=await (await fetch('/api/track?from='+r[0]+'&to='+r[1],{cache:'no-store'})).json(); }catch(e){ return; }
  var sig=tr.length+'|'+(tr[0]?tr[0][3]:'')+'|'+(tr.length?tr[tr.length-1][3]:'');
  if(!fit && sig===lastTrackSig) return;   // unchanged -> keep layers (and any open tooltip)
  lastTrackSig=sig;
  trackLayer.clearLayers();
  for(var i=1;i<tr.length;i++){
    var kmh=tr[i][2], tt=tr[i][3];
    L.polyline([[tr[i-1][0],tr[i-1][1]],[tr[i][0],tr[i][1]]],
      {color:speedColor(kmh),weight:5,opacity:.9})
      .bindTooltip('<b>'+(kmh||0).toFixed(1)+' km/h</b>'+(tt?'<br><span style="color:#888;font-size:11px">'+timeOnly(tt)+'</span>':''),
                   {sticky:true,direction:'top'})
      .addTo(trackLayer);
  }
  var astep=Math.max(6,Math.ceil(tr.length/40));   // ~40 direction arrows max, evenly spaced
  for(var k=astep;k<tr.length;k+=astep){
    if((tr[k][2]||0)<2) continue;                   // only where the car was actually moving
    L.marker([tr[k][0],tr[k][1]],{icon:trackArrowIcon(bearing(tr[k-1],tr[k])),interactive:false,keyboard:false}).addTo(trackLayer);
  }
  document.getElementById('tbinfo').textContent = tr.length
    ? (tr.length+' pts · '+fmtEpoch(r[0])+' → '+fmtEpoch(r[1]))
    : ('no track · '+fmtEpoch(r[0])+' → '+fmtEpoch(r[1]));
  if(fit && tr.length){ try{ map.fitBounds(L.latLngBounds(tr.map(function(pt){return [pt[0],pt[1]];})),{padding:[30,30],maxZoom:17}); }catch(e){} }
}
function setTrackMode(m,btn){
  trackMode=m;
  document.querySelectorAll('#trackbar .tbtn[data-mode]').forEach(function(b){b.classList.toggle('on', b===btn);});
  drawTrack(true);
}
(function(){
  var tb=document.getElementById('trackbar'); if(!tb) return;
  var tbtn=document.getElementById('trackbtn');
  if(tbtn) tbtn.addEventListener('click',function(){ var on=tb.classList.toggle('show'); tbtn.classList.toggle('on',on); });
  var midnight=function(x){var d=new Date(x);d.setHours(0,0,0,0);return d;};
  var toLocalInput=function(d){return d.getFullYear()+'-'+z2(d.getMonth()+1)+'-'+z2(d.getDate())+'T'+z2(d.getHours())+':'+z2(d.getMinutes());};
  document.getElementById('tbfrom').value=toLocalInput(new Date(Date.now()-86400000));
  document.getElementById('tbto').value=toLocalInput(new Date());
  tb.addEventListener('click',function(e){
    var b=e.target.closest&&e.target.closest('button.tbtn'); if(!b) return;
    var mode=b.getAttribute('data-mode'); if(!mode) return;
    var cust=document.getElementById('tbcustom');
    if(mode==='custom'){ cust.classList.toggle('show'); return; }
    cust.classList.remove('show');
    var now=new Date(), start, end;
    if(mode==='h:1') return setTrackMode({type:'roll',h:1},b);
    if(mode==='h:24') return setTrackMode({type:'roll',h:24},b);
    if(mode==='prevday'){ end=midnight(now).getTime(); start=end-86400000; }
    else if(mode==='prevweek'){ var m0=midnight(now); var dow=(m0.getDay()+6)%7; var thisMon=m0.getTime()-dow*86400000; end=thisMon; start=thisMon-7*86400000; }
    else if(mode==='prevmonth'){ end=new Date(now.getFullYear(),now.getMonth(),1).getTime(); start=new Date(now.getFullYear(),now.getMonth()-1,1).getTime(); }
    else return;
    setTrackMode({type:'fixed',from:Math.floor(start/1000),to:Math.floor(end/1000)},b);
  });
  document.getElementById('tbapply').addEventListener('click',function(){
    var f=document.getElementById('tbfrom').value, t=document.getElementById('tbto').value;
    var fe=Math.floor(new Date(f).getTime()/1000), te=Math.floor(new Date(t).getTime()/1000);
    if(!f||!t||isNaN(fe)||isNaN(te)||te<=fe){ document.getElementById('tbinfo').textContent='pick a valid start & end'; return; }
    trackMode={type:'fixed',from:fe,to:te};
    document.querySelectorAll('#trackbar .tbtn[data-mode]').forEach(function(b){b.classList.remove('on');});
    var cb=document.querySelector('#trackbar .tbtn[data-mode="custom"]'); if(cb) cb.classList.add('on');
    drawTrack(true);
  });
})();
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
  var a=document.getElementById('mb_armed'), ad=a.firstElementChild, ac=a.lastElementChild, av=(kv.armed||'').toLowerCase();
  if(av==='valet'){ ad.innerHTML=ICONS.unlock; ac.textContent='Valet mode'; a.className='mbadge valet'; }
  else if(av.indexOf('arm')>=0 && av.indexOf('dis')<0){ ad.innerHTML=ICONS.lock; ac.textContent='Armed'; a.className='mbadge on'; }
  else if(av.indexOf('dis')>=0 || av==='off'){ ad.innerHTML=ICONS.unlock; ac.textContent='Disarmed'; a.className='mbadge off'; }
  else { ad.innerHTML=ICONS.lock; ac.textContent='—'; a.className='mbadge unk'; }
  var mv=document.getElementById('mb_move'), mvd=mv.firstElementChild, mvc=mv.lastElementChild, mvv=(kv.moving||'').toLowerCase();
  if(mvv==='yes'){ mvd.innerHTML=ICONS.car; mvc.textContent='Ride started'; mv.className='mbadge on'; }
  else if(mvv==='no'){ mvd.innerHTML=ICONS.car; mvc.textContent='Parked'; mv.className='mbadge ignoff'; }
  else { mvd.innerHTML=ICONS.car; mvc.textContent='—'; mv.className='mbadge unk'; }
  var lb=document.getElementById('mb_label'), lbd=lb.firstElementChild, lbc=lb.lastElementChild, lv=(kv.label||'').toLowerCase();
  if(lv==='found'){ lbd.innerHTML=ICONS.key; lbc.textContent='Label'; lb.className='mbadge on'; }
  else if(lv==='absent'){ lbd.innerHTML=ICONS.key; lbc.textContent='No label'; lb.className='mbadge ignoff'; }
  else { lbd.innerHTML=ICONS.key; lbc.textContent='—'; lb.className='mbadge unk'; }
  document.getElementById('statetime').textContent = kv.last_seen? localTime(kv.last_seen) : (p?localTime(p.dev_time||p.recv_ts):'');
  document.getElementById('evt').textContent =
    (p?'':'waiting for data')+
    (d.speed_kmh!=null? d.speed_kmh+' km/h':'')+
    (kv.moving==='yes'?' · moving':'');
  if(trackMode.type==='roll'){ await drawTrack(false); }
  if(p){ var ll=[p.lat,p.lon];
    var moving=(kv.moving==='yes')||((d.speed_kmh||0)>3);
    var hd=(p.course!=null&&!isNaN(p.course))?p.course:null;
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
  var ev=await (await fetch('/api/events',{cache:'no-store'})).json();
  document.getElementById('elist').innerHTML=ev.map(function(e){
    return '<div class="er"><span class="et">'+localTime(e.ts)+'</span>'+
      '<span class="ee ek-'+(e.kind||'')+'">'+e.event+'</span></div>';
  }).join('') || '<div style="padding:16px;color:#888">no events yet — they log as the car changes state</div>';
 }catch(e){ document.getElementById('online').textContent='error: '+e; }
}
var uplotInst=null, gMetric=null, gLabel=null, gHours=6;
function openGraph(metric,label){
  gMetric=metric; gLabel=label; gHours=6;
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
        axes:[ {values:function(u,sp){return sp.map(function(v){var d=new Date(v*1000);
          return (d.getHours()===0&&d.getMinutes()===0)?(z2(d.getDate())+'/'+z2(d.getMonth()+1)):(z2(d.getHours())+':'+z2(d.getMinutes()));});}}, {} ],
        cursor:{ drag:{x:true,y:false} },
        hooks:{
          init:[function(u){ var t=document.createElement('div'); t.className='gtip'; t.style.display='none'; u.over.appendChild(t); u.__tip=t; }],
          setCursor:[function(u){ var i=u.cursor.idx, t=u.__tip; if(!t) return;
            if(i==null||u.cursor.left<0){ t.style.display='none'; return; }
            var y=u.data[1][i];
            t.textContent=(y==null?'—':y);
            t.style.display='block'; t.style.left=u.cursor.left+'px'; t.style.top=u.cursor.top+'px';
          }] } };
      uplotInst=new uPlot(opts,data,chart);
    })
    .catch(function(e){ chart.innerHTML='<div style="padding:40px;color:#c0392b">error: '+e+'</div>'; });
}
document.getElementById('sidebar').addEventListener('click',function(e){
  var b=e.target.closest&&e.target.closest('button[data-cmd]');
  if(b){ sendCommand(b.getAttribute('data-cmd'), b.textContent.trim()); }
});
function toggleSidebar(o){ document.getElementById('sidebar').classList.toggle('open',o); document.getElementById('sbback').classList.toggle('open',o); }
document.getElementById('menubtn').onclick=function(){toggleSidebar(true);};
document.getElementById('sbclose').onclick=function(){toggleSidebar(false);};
document.getElementById('sbback').onclick=function(){toggleSidebar(false);};
function sendCommand(cmd,label){
  var t=document.getElementById('ctoast'); t.textContent=label+' …';
  fetch('/api/command?cmd='+encodeURIComponent(cmd),{cache:'no-store'})
    .then(function(r){return r.json();})
    .then(function(j){ t.textContent=label+' → '+(j.msg||(j.ok?'sent':'no response')); })
    .catch(function(e){ t.textContent=label+' → error: '+e; });
}
document.getElementById('tabs').addEventListener('click',function(e){
  if(!e.target.classList.contains('tab')) return;
  var t=e.target.getAttribute('data-tab');
  var bs=document.querySelectorAll('#tabs .tab');
  for(var i=0;i<bs.length;i++) bs[i].classList.toggle('on', bs[i].getAttribute('data-tab')===t);
  document.getElementById('ewrap').style.display=(t==='events')?'':'none';
  document.getElementById('jwrap').style.display=(t==='journal')?'':'none';
});
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
                qs = parse_qs(urlparse(self.path).query)
                def _pe(k):
                    v = (qs.get(k) or [None])[0]
                    try:
                        return int(float(v))
                    except (TypeError, ValueError):
                        return None
                EPOCH = datetime.datetime(1970, 1, 1)
                to = _pe("to"); frm = _pe("from")
                to_dt = (EPOCH + datetime.timedelta(seconds=to)) if to is not None else datetime.datetime.utcnow()
                frm_dt = (EPOCH + datetime.timedelta(seconds=frm)) if frm is not None else (to_dt - datetime.timedelta(hours=1))
                frm_s = frm_dt.strftime("%Y-%m-%d %H:%M:%S")
                to_s = to_dt.strftime("%Y-%m-%d %H:%M:%S") + ".999999"
                MAXP = 3000  # cap points sent to the browser; even sampling for big ranges
                rows = q("SELECT lat,lon,speed_knots,t FROM ("
                         "SELECT lat,lon,speed_knots,COALESCE(dev_time,recv_ts) t,"
                         "(ROW_NUMBER() OVER (ORDER BY id)-1) rn, COUNT(*) OVER () cnt "
                         "FROM position WHERE recv_ts>=? AND recv_ts<=?) "
                         "WHERE rn % max(1,(cnt+?-1)/?)=0 ORDER BY rn",
                         (frm_s, to_s, MAXP, MAXP))
                self._send(200, json.dumps([[r[0], r[1], round((r[2] or 0) * 1.852, 1), r[3]]
                                            for r in rows]), "application/json")
            elif self.path.startswith("/api/journal"):
                rows = q("SELECT ts,dir,summary FROM journal ORDER BY id DESC LIMIT 400")
                self._send(200, json.dumps([{"ts": r[0], "dir": r[1], "summary": r[2]}
                                            for r in rows]), "application/json")
            elif self.path.startswith("/api/events"):
                rows = q("SELECT ts,kind,event FROM events ORDER BY id DESC LIMIT 100")
                self._send(200, json.dumps([{"ts": r[0], "kind": r[1], "event": r[2]}
                                            for r in rows]), "application/json")
            elif self.path.startswith("/api/command"):
                cmd = (parse_qs(urlparse(self.path).query).get("cmd") or [""])[0]
                self._send(200, json.dumps({"ok": False, "cmd": cmd,
                    "msg": "not wired yet (independent app — SMS control planned)"}),
                    "application/json")
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
