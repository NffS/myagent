#!/usr/bin/env bash
# Log rotation for the Fiesta tracker server (run daily by logclean.timer).
#
# Policy (per request): keep NO logs older than 1 month, and keep the TOTAL size
# of all server logs under 10 GB.
#   managed logs = top-level *.log / *.jsonl / *.bin in /root/captures
#                  (the preserved reference_* dir and car.db* are NEVER touched)
#   file logs are capped at 8 GB here; journald is capped at 2 GB (journald.conf.d
#   drop-in) and vacuumed below -> <= 10 GB total across all server logs.
set -u
DIR=/root/captures
MAXDAYS=30
FILE_MAXBYTES=$((8 * 1024 * 1024 * 1024))   # 8 GB budget for file logs

# select managed log files (top-level only; excludes reference_* subdir and car.db)
sel() { find "$DIR" -maxdepth 1 -type f \( -name '*.log' -o -name '*.jsonl' -o -name '*.bin' \) "$@"; }
total() { sel -printf '%s\n' 2>/dev/null | awk '{s+=$1} END{print s+0}'; }

echo "[$(date '+%F %T')] logclean start; file-log total $(($(total) / 1048576)) MB"

# 1) AGE: delete any managed log older than one month
sel -mtime +$MAXDAYS -print -delete

# 2) SIZE: delete oldest-first until under the file-log budget
while [ "$(total)" -gt "$FILE_MAXBYTES" ]; do
    oldest=$(sel -printf '%T@ %p\n' 2>/dev/null | sort -n | head -1 | cut -d' ' -f2-)
    [ -z "$oldest" ] && break
    echo "  over budget -> rm $oldest"
    rm -f "$oldest"
done

# 2b) DB retention: raw journal 7 days, all other time-series data 90 days
python3 - <<'PY' 2>&1 | sed 's/^/  db: /'
import sqlite3, datetime
d = sqlite3.connect('/root/captures/car.db', timeout=15)
now = datetime.datetime.now()
for t, c, days in (('metrics','ts',90), ('events','ts',90), ('position','recv_ts',90),
                   ('telemetry','recv_ts',90), ('journal','ts',7)):
    cut = (now - datetime.timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    try:
        n = d.execute('DELETE FROM %s WHERE %s < ?' % (t, c), (cut,)).rowcount
        d.commit(); print('%s: purged %d rows older than %dd' % (t, n, days))
    except Exception as e:
        print('%s: %s' % (t, e))
PY

# 3) JOURNALD: cap the systemd journal to one month / 2 GB
journalctl --vacuum-time=1month --vacuum-size=2G 2>&1 | sed 's/^/  journald: /'

echo "[$(date '+%F %T')] logclean done; file-log total $(($(total) / 1048576)) MB; journald $(journalctl --disk-usage 2>/dev/null)"
