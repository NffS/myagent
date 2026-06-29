#!/usr/bin/env python3
"""
http_server.py - minimal HTTP server for the GPS tracker project.

For now it just proves HTTP works on the box and gives a URL to hit. Later
this is where the Android app's JSON/WebSocket API will live (read positions,
send commands), reading from the same store the tracker listener writes to.

Stdlib only. Run: python3 http_server.py [--port 80]
"""

import argparse
import datetime
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


PAGE = """<!doctype html><meta charset=utf-8>
<title>GPS server</title>
<body style="font-family:system-ui;max-width:40em;margin:3em auto">
<h1>GPS tracker server &mdash; HTTP endpoint is up</h1>
<p>This is the placeholder web endpoint. The Android app API will live here.</p>
<ul>
<li><a href="/health">/health</a> &mdash; JSON status</li>
</ul>
<p style="color:#888">server time: {time}</p>
</body>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "gps-http/0.1"

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/health", "/healthz"):
            self._send(200, json.dumps({"status": "ok", "time": now()}),
                       "application/json")
        elif self.path == "/":
            self._send(200, PAGE.format(time=now()))
        else:
            self._send(404, json.dumps({"error": "not found", "path": self.path}),
                       "application/json")

    def log_message(self, fmt, *args):
        print("[%s] %s - %s" % (now(), self.client_address[0], fmt % args), flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=80)
    p.add_argument("--host", default="0.0.0.0")
    args = p.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("[%s] HTTP server listening on %s:%d" % (now(), args.host, args.port), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
