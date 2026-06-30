# myagent — self-hosted server for an "AgentMS3" car tracker

Self-hosted replacement server + protocol-reversing toolkit for a car anti-theft
tracker labeled **AgentMS3**, whose hosted tracking server was lost. Goal:
receive the device's data on our own server, then drive an Android app from it.

## What the device actually is

- **`AgentMS3`** self-reports as `AgentMS3 ver.02.23d` (2018) — a **Magic Systems**
  (Меджик Системс, St. Petersburg) hardwired car alarm + satellite immobilizer
  with GPS/GLONASS. Not a generic Chinese GT06/Meitrack beacon.
- Native cloud: **Car-Online**, device endpoint **`v5.car-online.ru:11111`** (app
  "Car-Online"). The wire protocol is **proprietary binary**, undocumented, and
  not supported by Traccar/Wialon.
- It can be repointed to a custom server with the **undocumented SMS commands**
  `Server <ip>` and `Rserver <ip>` (port is fixed at 11111). Confirmed via the
  `VERSION?` reply.

### Protocol notes (reverse-engineering in progress)

- Device check-in / login packet (50 bytes):
  `40 00 32 00 1c 46 00 00 00 00 00 00 <16-bit seq LE> 00 0e 1e 00 00 00`
  followed by a **30-digit ASCII device code** (e.g. `000000000065568704089418044847`).
  `40`=`@` start, offset 2 = `0x32` = total length, offset 16 = `0x1e` = 30 = id length.
- The real Car-Online server sends **no ACK** — it's one-way ingest. The device
  also emits a stray `AT+CREG?` (modem) keepalive.
- A parked device sends **only keepalives**; **position frames appear on
  motion/ignition events**. Capturing those (via the proxy) is the next step.

## Files

| File | Purpose |
|---|---|
| `carproxy.py` | Capture endpoint for the device, **logging to** `proxy_*.log` (human hex+ascii), `frames_*.jsonl` (one JSON record per frame, for offline analysis), and `px_*_{DEV2SRV,SRV2DEV}.bin` (raw). **Default: capture-only** (log the device's data, forward nothing). Pass `--relay` for proxy/MITM mode (also forward to Car-Online and log both directions). |
| `gps_sniffer.py` | Standalone protocol-agnostic TCP+UDP logger. Hex-dumps + fingerprints common tracker protocols (GT06/Concox, Meitrack, Coban/TK103, H02, Teltonika, Wialon IPS, EGTS, Navtelecom) and auto-ACKs GT06. Stdlib only. |
| `monitor.py` | Watches the proxy log and exits/alerts on the first **non-keepalive** (likely position) frame. |
| `http_server.py` | Minimal HTTP endpoint (`/`, `/health`) — seed for the Android app's JSON/WebSocket API. |

## Running

```bash
python3 carproxy.py                            # capture-only (default): log device data
python3 carproxy.py --relay                    # proxy mode: also forward to Car-Online
python3 gps_sniffer.py --ports 11111          # standalone capture (no upstream)
python3 monitor.py                            # alert on first position frame
python3 http_server.py --port 80
```

Captures (git-ignored) are written to `./captures/`.

## Deployment

On the server (`173.242.49.128`) as `systemd` services — auto-restart, enabled on boot:

- `gps-proxy` — `carproxy.py` on `:11111` (the active capture path)
- `gps-http` — `http_server.py` on `:80`
- `gps-sniffer` — standalone sniffer (available; stopped while the proxy owns `:11111`)

```bash
journalctl -u gps-proxy -f
systemctl status|restart|stop gps-proxy
```

## Repointing the device

From the registered owner phone, SMS the device's SIM (port is always 11111):

```
Server 173.242.49.128       → Server ok
Rserver 173.242.49.128      → Reserve server ok   (Agent MS has a reserve slot — set it too)
version?                    → confirms model, firmware, SERVER <ip>, device code
```

## Status / next

- ✅ Device identified, repointed, connecting to our server, traffic captured both ways.
- ⏳ Only keepalives captured so far (device parked). **Next:** capture position
  frames on a real drive → decode lat/lon/speed/time → standalone parser →
  SQLite → JSON/WebSocket API → Android app (and drop the Car-Online dependency).
