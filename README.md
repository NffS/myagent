# myagent — self-hosted GPS tracker server

Self-hosted server for a car GPS anti-theft tracker (sold as "AgentMS3") whose
original hosted platform went offline. Goal: receive the device's data on our
own server, then drive an Android app from it.

## Status

1. **Protocol capture** — done. `gps_sniffer.py` listens for the device and
   logs/identifies its protocol.
2. **Protocol parsing + app API** — next, once the real device checks in.

The name "AgentMS3" appears to refer to **Meitrack's MS03 hosted platform**, so
the device most likely speaks **Meitrack** (TCP 5020, ASCII `$$...*<chk>\r\n`)
or **GT06/Concox** (TCP 5023, binary `78 78 ... 0D 0A`). Confirmed from the
first real packet.

## Files

| File | What it does |
|---|---|
| `gps_sniffer.py` | Protocol-agnostic TCP+UDP listener. Hex-dumps every byte, fingerprints common tracker protocols (GT06, TK103/Coban, H02, Teltonika, Meitrack, Queclink), and auto-ACKs GT06 login/heartbeat (CRC-16/X.25) so the device stays online. Stdlib only. |
| `http_server.py` | Minimal HTTP endpoint (`/`, `/health`). Placeholder for the Android app's JSON/WebSocket API. Stdlib only. |

## Running

```bash
python3 gps_sniffer.py                 # listen on the default common ports
python3 gps_sniffer.py --ports 5020    # one port
python3 gps_sniffer.py --no-smart-ack  # observe only, never reply
python3 http_server.py --port 80
```

Captures are written to `./captures/` (git-ignored): a human-readable
`capture_*.log` (hex + ASCII), raw `raw_*.bin` per peer, and `console.log`.

### Deployment

Both run as `systemd` services on the server (`gps-sniffer.service`,
`gps-http.service`) — auto-restart, enabled on boot.

```bash
journalctl -u gps-sniffer -f          # watch the tracker connect
systemctl status|restart|stop gps-sniffer
```

## Pointing the device at the server

Send the SIM an SMS with the new server IP + port (exact syntax is
device-specific):

```
# Meitrack family
0000,A21,1,<SERVER_IP>,5020,<APN>,,

# GT06 / Coban family
APN,<APN>#
SERVER,0,<SERVER_IP>,5023,0#
STATUS#
```

The listener identifies the protocol from packet contents, not the port, so any
of the listened ports will be captured.
