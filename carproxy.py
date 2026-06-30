#!/usr/bin/env python3
"""
carproxy.py - transparent TCP proxy + full-capture MITM between the Magic
Systems "Agent MS" tracker and the real Car-Online server (v5.car-online.ru).

Why: the device (repointed to our IP) speaks a proprietary protocol and won't
emit position data without the correct server handshake, which we can't
synthesize blind. By transparently relaying device <-> real Car-Online and
logging BOTH directions, the device works normally (gets real ACKs -> streams
positions) AND we capture the complete protocol (handshake + position frames +
server commands) needed to build a standalone server. Car-Online keeps working
in the meantime.

    device --> [ :11111 this proxy ] --> v5.car-online.ru:11111
           <--                       <--

Everything is logged to proxy_*.log (hex+ascii, direction-tagged) and to raw
per-direction .bin files. Stdlib only.

By default it runs CAPTURE-ONLY (logs the device's data, forwards nothing).
Pass --relay to also forward to the upstream (Car-Online) — "proxy mode".

    python3 carproxy.py                 # capture-only (default)
    python3 carproxy.py --relay         # proxy mode: also forward to Car-Online
"""

import argparse
import datetime
import json
import os
import socket
import threading


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def hexdump(data, indent="    "):
    out = []
    w = 16
    for off in range(0, len(data), w):
        chunk = data[off:off + w]
        hexs = " ".join("%02x" % b for b in chunk) + "   " * (w - len(chunk))
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append("%s%04x  %s  |%s|" % (indent, off, hexs, asc))
    return "\n".join(out)


# Bytes 12-13 of every device frame are a little-endian sequence counter that
# increments per packet (2d18, 2d19, 2d1a, ...). A reply template can echo it
# back so the ACK tracks the live counter instead of matching one cycle only.
SEQ_OFFSET = 12
SEQ_LEN = 2


def render_reply(template, is_hex, data):
    """Build reply bytes from a template, substituting per-frame fields sliced
    from the just-received frame `data`:

        {seq}     -> the raw 2 seq bytes, data[12:14]
        {seqhex}  -> their ASCII hex (e.g. "2d18")

    `is_hex` selects how the literal (non-token) parts are read: hex digits
    (like --reply 'hex:...') when True, else UTF-8 text. Returns the reply
    bytes, or None when the frame is too short to supply a referenced field
    (guards keepalives shorter than 14 bytes). Raises ValueError on a
    malformed hex template."""
    needs_seq = "{seq}" in template or "{seqhex}" in template
    if needs_seq and len(data) < SEQ_OFFSET + SEQ_LEN:
        return None  # frame too short to slice the seq counter -- skip this reply
    seq = data[SEQ_OFFSET:SEQ_OFFSET + SEQ_LEN]
    # {seqhex} is a textual substitution, valid in both hex and text templates.
    template = template.replace("{seqhex}", seq.hex())
    # {seq} injects raw bytes, so assemble the literal segments around it and
    # rejoin them with the raw seq bytes as the separator.
    parts = template.split("{seq}")
    chunks = [bytes.fromhex(p) if is_hex else p.encode("utf-8") for p in parts]
    return seq.join(chunks)


class Proxy:
    def __init__(self, listen_port, up_host, up_port, capdir, relay=False,
                 reply=None, reply_template=None, reply_sweep=None):
        self.listen_port = listen_port
        self.up_host = up_host
        self.up_port = up_port
        self.capdir = capdir
        self.relay = relay
        self.reply = reply  # capture-only: bytes to send back to the device after each frame
        # capture-only: (template_str, is_hex) rendered per-frame; takes precedence over reply
        self.reply_template = reply_template
        # capture-only: list of (template_str, is_hex) rotated one-per-connection
        # to test many ACK candidates hands-off; takes precedence over reply_template
        self.reply_sweep = reply_sweep
        self.sweep_i = 0
        os.makedirs(capdir, exist_ok=True)
        self.logpath = os.path.join(
            capdir, "proxy_%s.log" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        self.framespath = os.path.join(
            capdir, "frames_%s.jsonl" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        self.lock = threading.Lock()
        self.logfh = open(self.logpath, "a", encoding="utf-8")
        # machine-parseable: one JSON record per frame, for offline analysis
        self.framesfh = open(self.framespath, "a", encoding="utf-8")

    def log(self, msg, dump=None):
        line = "[%s] %s" % (now(), msg)
        with self.lock:
            print(line, flush=True)
            self.logfh.write(line + "\n")
            if dump is not None:
                self.logfh.write(hexdump(dump) + "\n")
                print(hexdump(dump), flush=True)
            self.logfh.flush()

    def serve(self):
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.bind(("0.0.0.0", self.listen_port))
        ls.listen(64)
        mode = ("relay -> %s:%d (forwarding to Car-Online)" % (self.up_host, self.up_port)
                if self.relay else "CAPTURE-ONLY (default; pass --relay to forward to Car-Online)")
        self.log("PROXY listening :%d   mode: %s" % (self.listen_port, mode))
        self.log("  human log : %s" % self.logpath)
        self.log("  frames    : %s" % self.framespath)
        while True:
            c, a = ls.accept()
            threading.Thread(target=self.handle, args=(c, a), daemon=True).start()

    def handle(self, client, addr):
        peer = "%s:%d" % addr
        self.log("+++ device connected %s" % peer)
        base = addr[0].replace(".", "-")
        rawc = os.path.join(self.capdir, "px_%s_DEV2SRV.bin" % base)
        raws = os.path.join(self.capdir, "px_%s_SRV2DEV.bin" % base)
        if not self.relay:
            # Default: capture the device's data, forward nothing to Car-Online.
            rtmpl = self.reply_template
            if self.reply_sweep:
                # rotate a different candidate ACK per connection (round-robin)
                with self.lock:
                    idx = self.sweep_i
                    self.sweep_i += 1
                rtmpl = self.reply_sweep[idx % len(self.reply_sweep)]
                self.log("=== CAPTURE-ONLY %s  [SWEEP candidate #%d/%d: %s]" % (
                    peer, idx % len(self.reply_sweep), len(self.reply_sweep), rtmpl[0]))
            elif rtmpl is not None:
                self.log("=== CAPTURE-ONLY %s (template: %s)" % (peer, rtmpl[0]))
            elif self.reply:
                self.log("=== CAPTURE-ONLY %s (reply %s)" % (peer, self.reply.hex()))
            else:
                self.log("=== CAPTURE-ONLY %s" % peer)
            self.pump(client, None, "DEV>SRV(capture-only)", peer, rawc,
                      reply=self.reply, reply_template=rtmpl)
            self.log("--- closed %s (capture-only)" % peer)
            return
        try:
            up = socket.create_connection((self.up_host, self.up_port), timeout=15)
        except Exception as e:
            # Upstream (Car-Online) is down: still capture the device's bytes --
            # we just can't forward them. A one-sided capture beats losing the data.
            self.log("!!! upstream connect FAILED for %s: %s -- CAPTURE-ONLY mode" % (peer, e))
            self.pump(client, None, "DEV>SRV(no-upstream)", peer, rawc)
            self.log("--- closed %s (capture-only)" % peer)
            return
        self.log("=== relaying %s <-> %s:%d" % (peer, self.up_host, self.up_port))
        t1 = threading.Thread(target=self.pump, args=(
            client, up, "DEV>SRV", peer, rawc), daemon=True)
        t2 = threading.Thread(target=self.pump, args=(
            up, client, "SRV>DEV", peer, raws), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.log("--- closed %s" % peer)

    def record(self, tag, peer, data):
        rec = {"ts": now(), "dir": tag, "peer": peer, "len": len(data), "hex": data.hex()}
        with self.lock:
            self.framesfh.write(json.dumps(rec) + "\n")
            self.framesfh.flush()

    def pump(self, src, dst, tag, peer, rawpath, reply=None, reply_template=None):
        """Relay src->dst while logging every chunk. dst=None => capture-only
        (log + save to disk but don't forward). reply (bytes) is sent back to
        src after each chunk -- an ACK experiment in capture-only mode.
        reply_template (template_str, is_hex), when set, is rendered per-frame
        against the just-received chunk (see render_reply) and takes precedence
        over the static reply -- this lets the ACK echo the live seq counter."""
        try:
            while True:
                data = src.recv(65535)
                if not data:
                    break
                self.log("%s  %s  %d bytes" % (tag, peer, len(data)), dump=data)
                self.record(tag, peer, data)
                with open(rawpath, "ab") as fh:
                    fh.write(data)
                if dst is not None:
                    dst.sendall(data)
                out = reply
                if reply_template is not None:
                    out = render_reply(reply_template[0], reply_template[1], data)
                    if out is None:
                        self.log("    >>> reply skipped for %s: frame too short (%d bytes)"
                                 " for template fields" % (peer, len(data)))
                if out is not None:
                    try:
                        src.sendall(out)
                        self.log("    >>> replied to %s: %s |%s|" % (
                            peer, out.hex(),
                            "".join(chr(b) if 32 <= b < 127 else "." for b in out)))
                    except Exception as e:
                        self.log("    >>> reply to %s FAILED: %s" % (peer, e))
        except Exception:
            pass
        finally:
            for s in (src, dst):
                if s is None:
                    continue
                try:
                    s.close()
                except Exception:
                    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen-port", type=int, default=11111)
    ap.add_argument("--upstream", default="v5.car-online.ru:11111")
    ap.add_argument("--capdir", default="/root/captures")
    ap.add_argument("--relay", action="store_true",
                    help="enable proxy/relay mode: forward to Car-Online (default: capture-only)")
    ap.add_argument("--reply", default=None,
                    help="capture-only: send this FIXED reply back to the device after each "
                         "frame (text, or 'hex:...' for raw bytes), e.g. --reply '200 OK'")
    ap.add_argument("--reply-template", default=None,
                    help="capture-only: like --reply, but tokens are substituted per-frame "
                         "from the just-received frame before sending, so the ACK tracks the "
                         "device's live sequence counter. {seq} -> the raw 2-byte LE seq "
                         "counter at bytes 12-13; {seqhex} -> its ASCII hex. Text by default, "
                         "or 'hex:...' for hex bytes. Frames shorter than 14 bytes get no "
                         "reply. Takes precedence over --reply. "
                         "e.g. --reply-template 'hex:40000300 46 {seq}'")
    ap.add_argument("--reply-sweep", default=None,
                    help="capture-only: comma-separated list of reply templates (each like "
                         "--reply-template). Rotate a DIFFERENT one per connection (round-robin) "
                         "to test many candidate ACKs hands-off; the chosen candidate index is "
                         "logged per connection. Use hex templates (commas only separate items).")
    # deprecated no-op, kept so older service invocations don't break (capture-only is the default now)
    ap.add_argument("--no-upstream", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    host, port = a.upstream.rsplit(":", 1)
    reply = None
    if a.reply is not None:
        reply = bytes.fromhex(a.reply[4:]) if a.reply.startswith("hex:") else a.reply.encode("utf-8")
    reply_template = None
    if a.reply_template is not None:
        if a.reply_template.startswith("hex:"):
            reply_template = (a.reply_template[4:], True)
        else:
            reply_template = (a.reply_template, False)
        # fail fast on a malformed template rather than at the first device frame
        try:
            render_reply(reply_template[0], reply_template[1], b"\x00" * (SEQ_OFFSET + SEQ_LEN))
        except ValueError as e:
            ap.error("invalid --reply-template %r: %s" % (a.reply_template, e))
    reply_sweep = None
    if a.reply_sweep is not None:
        reply_sweep = []
        for item in a.reply_sweep.split(","):
            item = item.strip()
            if not item:
                continue
            tmpl = (item[4:], True) if item.startswith("hex:") else (item, False)
            try:
                render_reply(tmpl[0], tmpl[1], b"\x00" * (SEQ_OFFSET + SEQ_LEN))
            except ValueError as e:
                ap.error("invalid --reply-sweep item %r: %s" % (item, e))
            reply_sweep.append(tmpl)
    Proxy(a.listen_port, host, int(port), a.capdir, relay=a.relay,
          reply=reply, reply_template=reply_template, reply_sweep=reply_sweep).serve()


if __name__ == "__main__":
    main()
