#!/bin/sh
# Wait for the QKD-derived PSK files, run stunnel, SIGHUP it when a PSK file changes (new key = new handshakes).
set -eu
CONF=/etc/stunnel/stunnel.conf
for f in $(sed -n 's/^PSKsecrets *= *//p' "$CONF"); do
  until [ -s "$f" ]; do echo "waiting for $f (first QKD key)"; sleep 3; done
done
stunnel "$CONF" &
PID=$!
last="$(cat /psk/*.txt | md5sum)"
while kill -0 "$PID" 2>/dev/null; do
  sleep 5
  cur="$(cat /psk/*.txt | md5sum)"
  [ "$cur" != "$last" ] && { kill -HUP "$PID"; last="$cur"; echo "PSK changed, stunnel reloaded"; }
done
wait "$PID"
