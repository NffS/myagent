#!/usr/bin/env python3
"""Watch the carproxy log and exit as soon as a non-keepalive packet appears
(any SRV>DEV reply, or a DEV>SRV packet that isn't the 50-byte login / 9-byte
AT+CREG keepalive) -- i.e. a likely position/status frame. Times out in 1h."""
import time, glob, re, os, sys

pat = re.compile(r'(DEV>SRV|SRV>DEV)\s+\S+\s+(\d+) bytes')
deadline = time.time() + 3600


def newest():
    fs = glob.glob('/root/captures/proxy_*.log')
    return max(fs, key=os.path.getmtime) if fs else None


path = newest()
while not path and time.time() < deadline:
    time.sleep(2)
    path = newest()
if not path:
    print('no proxy log found')
    sys.exit(0)

f = open(path)
f.seek(0, 2)
print('monitoring %s for the first position/status frame...' % path)
while time.time() < deadline:
    line = f.readline()
    if not line:
        time.sleep(1)
        np = newest()
        if np and np != path:
            path = np
            f = open(path)
            f.seek(0, 0)
        continue
    m = pat.search(line)
    if m:
        dirn, n = m.group(1), int(m.group(2))
        if dirn == 'SRV>DEV' or (dirn == 'DEV>SRV' and n not in (50, 9)):
            print('=== INTERESTING (non-keepalive) PACKET ===')
            print(line.rstrip())
            for _ in range(16):
                l = f.readline()
                if l:
                    print(l.rstrip())
            sys.exit(0)
print('monitor timed out after 1h with only keepalives')
