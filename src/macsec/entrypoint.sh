#!/bin/sh
# MACsec (IEEE 802.1AE) with MKA (802.1X-2010) on a host NIC. The CAK/CKN come from /macsec/macsec.conf, written
# by qkd-exchange from a QKD key. When the file changes, wpa_supplicant is reconfigured (new CAK = new MKA session).
# Settings from /etc/macsec/macsec.env: MACSEC_IFACE (empty = idle), MACSEC_ADDR (optional CIDR on the macsec interface).
set -eu
[ -r /etc/macsec/macsec.env ] && . /etc/macsec/macsec.env
CONF=/macsec/macsec.conf
[ -n "${MACSEC_IFACE:-}" ] || { echo "MACSEC_IFACE not set: idle"; exec sleep infinity; }
until [ -s "$CONF" ]; do echo "waiting for $CONF (first QKD key)"; sleep 3; done
modprobe macsec 2>/dev/null || true
wpa_supplicant -i "$MACSEC_IFACE" -D macsec_linux -c "$CONF" &
PID=$!
last="$(md5sum < "$CONF")"
while kill -0 "$PID" 2>/dev/null; do
  sleep 5
  cur="$(md5sum < "$CONF")"
  if [ "$cur" != "$last" ]; then
    wpa_cli -p /run/wpa_supplicant -i "$MACSEC_IFACE" reconfigure >/dev/null && last="$cur" && echo "CAK changed, wpa_supplicant reconfigured"
  fi
  if [ -n "${MACSEC_ADDR:-}" ]; then
    M="$(ip -br link show type macsec 2>/dev/null | awk '{print $1}' | head -1 | cut -d@ -f1)"
    [ -n "$M" ] && ! ip -br addr show "$M" | grep -q "${MACSEC_ADDR%/*}" && ip addr add "$MACSEC_ADDR" dev "$M" && ip link set "$M" up && echo "address $MACSEC_ADDR on $M"
  fi
done
wait "$PID"
