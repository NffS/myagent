#!/usr/bin/env python3
"""
gps_sniffer.py - protocol-agnostic TCP+UDP listener for GPS / GSM trackers.

Goal: capture and identify the wire protocol of an unknown tracker (e.g.
"AgentMS3") so we can build a real server for it. It:

  * listens on several ports at once, both TCP and UDP
  * hex+ascii dumps every byte received, with timestamps
  * fingerprints the common tracker protocols (GT06/Concox, TK103/Coban,
    H02, Teltonika, Meitrack, Queclink, ...)
  * optionally answers GT06/Concox login + heartbeat packets with a correct
    ACK so chatty devices stay connected and keep streaming location data
  * appends a full capture log AND raw .bin files you can replay later

Standard library only. Python 3.7+. Runs on Linux, Windows and macOS.

Examples:
  python3 gps_sniffer.py                         # default common ports
  python3 gps_sniffer.py --ports 5023            # one port
  python3 gps_sniffer.py --ports 5023,5000,8090  # several ports
  python3 gps_sniffer.py --no-smart-ack          # observe only, never reply
  python3 gps_sniffer.py --ack hex:7878...0d0a   # send a fixed reply to TCP
"""

import argparse
import datetime
import os
import selectors
import socket
import sys

# ---------------------------------------------------------------------------
# Defaults: a handful of ports cheap car trackers commonly ship with. We don't
# yet know which one AgentMS3 uses, so we listen on all of them at once. When
# you reconfigure the device, point it at ONE of these (and tell me which).
DEFAULT_PORTS = [5023, 5020, 5013, 5001, 5000, 8090, 7700, 9000,
                 20332, 21013, 20963,  # Wialon IPS / Combine / Retranslator
                 11111]                # Magic Systems Car-Online (Agent MS family)

# GT06/Concox protocol numbers we should ACK to keep the device online.
# (login + the various heartbeat/status variants). Location packets are NOT
# normally ACKed, so we leave them alone.
GT06_ACK_PROTOS = {0x01, 0x08, 0x13, 0x18, 0x19, 0x23}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def hexdump(data, indent="    "):
    """`hexdump -C` style: offset, hex columns, ascii gutter."""
    out = []
    width = 16
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        hexs = " ".join("%02x" % b for b in chunk)
        hexs = hexs + "   " * (width - len(chunk))  # pad short last row
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append("%s%04x  %s  |%s|" % (indent, off, hexs, asc))
    return "\n".join(out)


def fingerprint(data):
    """Best-guess of the tracker protocol from the first bytes."""
    if len(data) >= 2 and data[0] == 0x78 and data[1] == 0x78:
        return "GT06 / Concox family (short frame 0x78 0x78) - VERY common car-tracker protocol"
    if len(data) >= 2 and data[0] == 0x79 and data[1] == 0x79:
        return "GT06 / Concox family (long frame 0x79 0x79)"
    head = bytes(data[:32]).decode("ascii", "replace").lower()
    if head.startswith("imei:"):
        return "TK103 / Coban family (ASCII, starts with 'imei:')"
    if data[:1] == b"(":
        return "TK103 / Coban variant (ASCII framed with parentheses, e.g. '(027...BR00')"
    if data[:4] == b"*HQ," or data[:3] == b"*HQ":
        return "H02 protocol (ASCII '*HQ,')"
    if data[:2] == b"$$":
        return "Meitrack protocol (ASCII '$$')"
    if data[:6] == b"+RESP:" or data[:5] == b"+ACK:" or data[:6] == b"+BUFF:":
        return "Queclink / GL family ('+RESP:' / '+ACK:')"
    if len(data) >= 3 and data[0] == 0x00 and data[1] == 0x0F:
        return "Possibly Teltonika (IMEI handshake: 00 0F <imei ascii>)"
    if data[:4] == b"\x00\x00\x00\x00":
        return "Possibly Teltonika Codec8 AVL packet (0x00000000 preamble)"
    if data[:3] == b"#L#" or data[:4] == b"#SD#" or data[:3] in (b"#D#", b"#P#", b"#B#", b"#M#"):
        return "Wialon IPS (#...# ASCII frames) - used by AGENT-brand / Wialon trackers (often :20332)"
    if data[:4] == b"@NTC":
        return "Navtelecom NTCB/FLEX (ASCII '@NTC' handshake)"
    if len(data) >= 2 and data[0] == 0x01 and data[1] == 0x00:
        return "Possibly EGTS (Russian GOST R 56360, binary: PRV=01 SKID=00)"
    if all(32 <= b < 127 or b in (9, 13, 10) for b in data[:64]):
        return "ASCII / text-based protocol (NMEA-like or TK-style) - read the |ascii| gutter"
    return ("UNKNOWN / likely proprietary (e.g. Magic Systems Car-Online, usually :11111) "
            "- capture several packets and share the hex dump for reverse-engineering")


# CRC-16/X.25 (a.k.a. CRC-ITU) - the checksum GT06/Concox uses.
def crc16_x25(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x8408
            else:
                crc >>= 1
    return crc ^ 0xFFFF


def iter_gt06_frames(buf):
    """
    Pull complete GT06 frames out of a stream buffer.
    Yields (proto, serial_bytes, frame_len). Stops at the first incomplete
    frame; caller keeps the unconsumed tail. Returns total consumed length.
    """
    frames = []
    i = 0
    n = len(buf)
    while i + 5 <= n:
        if buf[i] == 0x78 and buf[i + 1] == 0x78:        # short frame
            length = buf[i + 2]
            total = length + 5                            # 78 78 LEN ...(LEN) 0d 0a
            if i + total > n:
                break
            if buf[i + total - 2] == 0x0D and buf[i + total - 1] == 0x0A:
                proto = buf[i + 3]
                serial = bytes(buf[i + 3 + length - 4:i + 3 + length - 2])
                frames.append((proto, serial, total))
                i += total
                continue
        elif buf[i] == 0x79 and buf[i + 1] == 0x79:      # long frame
            length = (buf[i + 2] << 8) | buf[i + 3]
            total = length + 6                            # 79 79 LEN(2) ...(LEN) 0d 0a
            if i + total > n:
                break
            if buf[i + total - 2] == 0x0D and buf[i + total - 1] == 0x0A:
                proto = buf[i + 4]
                serial = bytes(buf[i + 4 + length - 4:i + 4 + length - 2])
                frames.append((proto, serial, total))
                i += total
                continue
        # not a frame start (or corrupt) - skip one byte and resync
        i += 1
    return frames, i


def gt06_ack(proto, serial):
    """Build the standard GT06 server response for login/heartbeat."""
    content = bytes([0x05, proto]) + serial      # LEN=5 = proto(1)+serial(2)+crc(2)
    crc = crc16_x25(content)
    return b"\x78\x78" + content + bytes([(crc >> 8) & 0xFF, crc & 0xFF]) + b"\x0d\x0a"


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------
class Sniffer:
    def __init__(self, ports, capdir, smart_ack=True, fixed_ack=None, echo=False):
        self.ports = ports
        self.capdir = capdir
        self.smart_ack = smart_ack
        self.fixed_ack = fixed_ack          # bytes or None
        self.echo = echo
        self.sel = selectors.DefaultSelector()
        self.conns = {}                     # fileno -> connection state
        os.makedirs(capdir, exist_ok=True)
        self.logpath = os.path.join(
            capdir, "capture_%s.log" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        self.logfh = open(self.logpath, "a", encoding="utf-8")

    def log(self, msg):
        line = "[%s] %s" % (now(), msg)
        print(line, flush=True)
        self.logfh.write(line + "\n")
        self.logfh.flush()

    def raw_append(self, tag, data):
        path = os.path.join(self.capdir, "raw_%s.bin" % tag.replace(":", "_").replace(".", "-"))
        with open(path, "ab") as fh:
            fh.write(data)

    # -- socket setup --------------------------------------------------------
    def listen(self):
        for port in self.ports:
            # TCP
            t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                t.bind(("0.0.0.0", port))
                t.listen(64)
                t.setblocking(False)
                self.sel.register(t, selectors.EVENT_READ, ("tcp_listen", port))
            except OSError as e:
                self.log("WARN  could not bind TCP :%d (%s)" % (port, e))
                t.close()
            # UDP
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                u.bind(("0.0.0.0", port))
                u.setblocking(False)
                self.sel.register(u, selectors.EVENT_READ, ("udp", port))
            except OSError as e:
                self.log("WARN  could not bind UDP :%d (%s)" % (port, e))
                u.close()

    # -- event loop ----------------------------------------------------------
    def serve_forever(self):
        self.log("capture log: %s" % self.logpath)
        self.log("listening (TCP+UDP) on ports: %s" % ", ".join(str(p) for p in self.ports))
        if self.smart_ack:
            self.log("smart-ack: ON  (will answer GT06/Concox login + heartbeat to keep device online)")
        if self.fixed_ack:
            self.log("fixed-ack: %s" % self.fixed_ack.hex())
        self.log("waiting for the tracker to connect ... (Ctrl+C to stop)")
        try:
            while True:
                for key, _ in self.sel.select(timeout=None):
                    kind, port = key.data[0], key.data[1]
                    if kind == "tcp_listen":
                        self.on_accept(key.fileobj, port)
                    elif kind == "tcp_client":
                        self.on_tcp_data(key.fileobj)
                    elif kind == "udp":
                        self.on_udp_data(key.fileobj, port)
        except KeyboardInterrupt:
            self.log("shutting down (Ctrl+C)")
        finally:
            self.logfh.close()

    def on_accept(self, lsock, port):
        try:
            csock, addr = lsock.accept()
        except OSError:
            return
        csock.setblocking(False)
        peer = "%s:%d" % addr
        self.conns[csock.fileno()] = {
            "sock": csock, "peer": peer, "port": port,
            "buf": bytearray(), "first": True, "tag": "%s_p%d" % (addr[0], port),
        }
        self.sel.register(csock, selectors.EVENT_READ, ("tcp_client", port))
        self.log("TCP  +++ connect  %s  -> :%d" % (peer, port))

    def on_tcp_data(self, csock):
        st = self.conns.get(csock.fileno())
        if st is None:
            return
        try:
            data = csock.recv(65535)
        except OSError as e:
            self.log("TCP  recv error %s: %s" % (st["peer"], e))
            data = b""
        if not data:
            self.log("TCP  --- disconnect %s" % st["peer"])
            self.sel.unregister(csock)
            csock.close()
            self.conns.pop(csock.fileno(), None)
            return
        self.report("TCP", st["peer"], st["port"], data, st)
        self.raw_append(st["tag"], data)
        self.maybe_reply_tcp(csock, st, data)

    def on_udp_data(self, usock, port):
        try:
            data, addr = usock.recvfrom(65535)
        except OSError:
            return
        peer = "%s:%d" % addr
        st = {"first": True, "peer": peer}      # UDP is connectionless; no per-peer state kept
        self.report("UDP", peer, port, data, st)
        self.raw_append("%s_p%d_udp" % (addr[0], port), data)
        reply = self.build_reply(data)
        if reply:
            try:
                usock.sendto(reply, addr)
                self.log("UDP  >>> reply %s  %s" % (peer, reply.hex()))
            except OSError as e:
                self.log("UDP  reply error %s: %s" % (peer, e))

    # -- logging + protocol guess -------------------------------------------
    def report(self, proto, peer, port, data, st):
        self.log("%s  <<< %d bytes from %s (:%d)" % (proto, len(data), peer, port))
        print(hexdump(data), flush=True)
        self.logfh.write(hexdump(data) + "\n")
        if st.get("first"):
            self.log("%s  fingerprint %s: %s" % (proto, peer, fingerprint(data)))
            st["first"] = False
        self.logfh.flush()

    # -- replies -------------------------------------------------------------
    def build_reply(self, data):
        """Return bytes to send back, or None. Used for both TCP and UDP."""
        if self.fixed_ack:
            return self.fixed_ack
        if self.echo:
            return data
        if self.smart_ack and len(data) >= 2 and data[0] in (0x78, 0x79) and data[1] == data[0]:
            frames, _ = iter_gt06_frames(bytearray(data))
            out = b""
            for proto, serial, _ in frames:
                if proto in GT06_ACK_PROTOS:
                    out += gt06_ack(proto, serial)
            return out or None
        return None

    def maybe_reply_tcp(self, csock, st, data):
        # For TCP, smart-ack works on the per-connection buffer so frames that
        # are split across recv() calls are still handled.
        if self.fixed_ack:
            self._send(csock, st, self.fixed_ack)
            return
        if self.echo:
            self._send(csock, st, data)
            return
        if not self.smart_ack:
            return
        st["buf"].extend(data)
        if len(st["buf"]) >= 2 and st["buf"][0] in (0x78, 0x79) and st["buf"][1] == st["buf"][0]:
            frames, consumed = iter_gt06_frames(st["buf"])
            del st["buf"][:consumed]
            for proto, serial, _ in frames:
                if proto in GT06_ACK_PROTOS:
                    ack = gt06_ack(proto, serial)
                    self._send(csock, st, ack)
                    self.log("TCP  >>> ack %s  proto=0x%02x  %s" % (st["peer"], proto, ack.hex()))
        else:
            # not GT06 - don't accumulate forever
            if len(st["buf"]) > 4096:
                st["buf"].clear()

    def _send(self, csock, st, payload):
        try:
            csock.sendall(payload)
        except OSError as e:
            self.log("TCP  send error %s: %s" % (st["peer"], e))


# ---------------------------------------------------------------------------
def parse_args(argv):
    p = argparse.ArgumentParser(description="Protocol-agnostic GPS tracker sniffer")
    p.add_argument("--ports", default=",".join(str(x) for x in DEFAULT_PORTS),
                   help="comma-separated ports to listen on (TCP+UDP). Default: %(default)s")
    p.add_argument("--capdir", default="captures", help="directory for capture files")
    p.add_argument("--no-smart-ack", action="store_true",
                   help="observe only; never send GT06/Concox keep-alive ACKs")
    p.add_argument("--echo", action="store_true",
                   help="echo every received payload straight back (debugging)")
    p.add_argument("--ack", default=None,
                   help="send a fixed reply to every packet, e.g. --ack hex:787805...0d0a")
    return p.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    ports = [int(x) for x in args.ports.split(",") if x.strip()]
    fixed = None
    if args.ack:
        h = args.ack[4:] if args.ack.startswith("hex:") else args.ack
        fixed = bytes.fromhex(h)
    s = Sniffer(ports, args.capdir,
                smart_ack=not args.no_smart_ack,
                fixed_ack=fixed,
                echo=args.echo)
    s.listen()
    s.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
