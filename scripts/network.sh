#!/usr/bin/env bash
# network.sh - addresses of the appliance NICs, written as files that survive a reboot. Run by Day 0. ens4 (the k3s private path) is
# configured by the Day 0 bootstrap itself, before k3s starts. Environment, all optional, each address gets a /24:
#   LOCAL_ADDR    ens5  encryptor to encryptor: IKE, peer API                  e.g. 172.16.0.11
#   KME_ADDR      ens6  KME to KME channel (the lab simulators' QKD link)      e.g. 192.168.50.11
#   LAPTOP_IP / LAPTOP_MASK  BASIC lab: a simulated laptop on a dummy interface laptop0
# Portal interface N is the Linux NIC ens(N+2): interface2 = ens4 ... interface7 = ens9. ens3 (interface1) is the management NIC and
# is not touched. NIC names can be overridden with NIC_LINK, NIC_KME. The NICs are addressed with a netplan file; the
# dummy interface uses systemd-networkd files because this netplan has no dummy device.
set -u
NP="${NP:-/etc/netplan/60-qkd-encryptor.yaml}"
NDIR="${NDIR:-/etc/systemd/network}"
y=""
add() { y="$y    $1:\n      addresses: [$2/24]\n"; }
[ -n "${LOCAL_ADDR:-}" ]   && add "${NIC_LINK:-ens5}" "$LOCAL_ADDR"
[ -n "${KME_ADDR:-}" ]     && add "${NIC_KME:-ens6}" "$KME_ADDR"
if [ -n "$y" ]; then
  mkdir -p "$(dirname "$NP")"
  printf 'network:\n  version: 2\n  ethernets:\n' > "$NP"; printf "$y" >> "$NP"; chmod 0600 "$NP"
fi
if [ -n "${LAPTOP_IP:-}" ]; then
  mkdir -p "$NDIR"
  printf '[NetDev]\nName=laptop0\nKind=dummy\n' > "$NDIR/40-laptop0.netdev"
  printf '[Match]\nName=laptop0\n\n[Network]\nAddress=%s/%s\n' "$LAPTOP_IP" "${LAPTOP_MASK:-24}" > "$NDIR/40-laptop0.network"
fi
[ -n "$y" ] || [ -n "${LAPTOP_IP:-}" ] || exit 0
[ -n "${NO_APPLY:-}" ] && exit 0
netplan generate 2>/dev/null; networkctl reload || { echo "network files written but not applied"; exit 1; }
networkctl reconfigure "${NIC_LINK:-ens5}" "${NIC_KME:-ens6}" laptop0 2>/dev/null || true
sleep 3
