#!/bin/sh
# MACsec (IEEE 802.1AE) on a host NIC with static keys: no MKA, no supplicant. qkd-exchange writes the QKD key and the peer's MAC
# address to /macsec/key.env; the key is the SAK. When the file changes, the new key goes on the next association number.
# Settings from /etc/macsec/macsec.env: MACSEC_IFACE (empty = idle), MACSEC_ADDR (optional CIDR on the macsec interface).
# The kernel module must be loaded on the host (modprobe macsec): a container cannot load it.
set -u
[ -r /etc/macsec/macsec.env ] && . /etc/macsec/macsec.env
. /usr/local/bin/apply.sh
KEYENV=/macsec/key.env
publish() { if [ -d /state ]; then { ip -br link show type macsec; ip macsec show 2>/dev/null; } > /state/macsec.tmp 2>/dev/null && mv /state/macsec.tmp /state/macsec.txt; fi; return 0; }
[ -n "${MACSEC_IFACE:-}" ] || { echo "MACSEC_IFACE not set: idle"; while true; do publish; sleep 10; done; }
echo "MACsec on $MACSEC_IFACE: waiting for the first QKD key"
last=""
while true; do
  if [ -s "$KEYENV" ] && [ "$(md5sum < "$KEYENV")" != "$last" ]; then
    . "$KEYENV"
    if [ -z "${PEER_MAC:-}" ]; then echo "the peer's MAC address is not known yet"
    elif apply_key; then last="$(md5sum < "$KEYENV")"; echo "MACsec key applied: association $AN, key id ${KEY_ID%????????????????????}..."
    else echo "applying the MACsec key failed, retrying"; fi
  fi
  publish
  [ -s /state/macsec.txt ] || echo "waiting for the first key" > /state/macsec.txt
  sleep 5
done
