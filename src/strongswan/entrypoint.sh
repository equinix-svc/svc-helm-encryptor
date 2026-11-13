#!/bin/sh
# Run charon in the foreground; reload swanctl config whenever it (or a QKD PPK file) changes.
set -eu
mkdir -p /etc/swanctl/conf.d
# IPsec state in the kernel outlives this container: a restart with a changed config left an old SA and policy whose reqid charon
# no longer knew ("trap not found"), so no tunnel came up. This container owns all IPsec on the node: start from a clean kernel.
ip xfrm state flush 2>/dev/null || true; ip xfrm policy flush 2>/dev/null || true
ipsec start --nofork &
CH=$!
for i in $(seq 1 30); do swanctl --stats >/dev/null 2>&1 && break; sleep 1; done
last=""; idle=0
while kill -0 "$CH" 2>/dev/null; do
  cur="$(cat /etc/swanctl/swanctl.conf /etc/swanctl/conf.d/* 2>/dev/null | md5sum)"
  if [ "$cur" != "$last" ]; then
    swanctl --load-all && last="$cur" || echo "swanctl --load-all failed, will retry"
  fi
  if [ -d /state ]; then swanctl --list-sas > /state/ipsec.tmp 2>/dev/null && mv /state/ipsec.tmp /state/ipsec.txt || true; fi
  # Initiator side (start_action = start) with its keys loaded but no tunnel for 60 s: when both ends reload at the same moment the first
  # IKE exchange can hang. End that attempt and start again.
  if grep -q 'start_action = start' /etc/swanctl/swanctl.conf 2>/dev/null && ls /etc/swanctl/conf.d/*.conf >/dev/null 2>&1; then
    if grep -q ESTABLISHED /state/ipsec.txt 2>/dev/null; then idle=0; else idle=$((idle+1)); fi
    if [ "$idle" -ge 12 ]; then
      echo "no IKE SA for 60 s: terminating the attempt and initiating again"
      swanctl --terminate --ike s2s >/dev/null 2>&1 || true
      timeout 30 swanctl --initiate --child s2s >/dev/null 2>&1 || true
      idle=0
    fi
  fi
  sleep 5
done
wait "$CH"
