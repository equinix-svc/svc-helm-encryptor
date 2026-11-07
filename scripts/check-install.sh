#!/usr/bin/env bash
# check-install.sh - read-only health check of an encryptor (or DAI Hub) node. Run ON the node:
#   curl -fsSL https://raw.githubusercontent.com/equinix-svc/svc-helm-encryptor/main/scripts/check-install.sh | sudo bash
# Prints PASS/FAIL lines and a few log tails. It never prints secrets (no Secret contents, no registries.yaml).
K="k3s kubectl"; NS=encryptor
pass() { printf 'PASS  %s\n' "$*"; }; fail() { printf 'FAIL  %s\n' "$*"; }; info() { printf '      %s\n' "$*"; }
hdr() { printf '\n== %s\n' "$*"; }

hdr "Day 0"
[ -e /var/log/day0-done ] && pass "day0 finished ($(cat /var/log/day0-done))" || fail "day0 not finished (/var/log/day0-done missing)"
if [ -r /var/log/day0.log ]; then
  grep -qE "failed|could not download|missing required" /var/log/day0.log && { fail "errors in /var/log/day0.log:"; grep -E "failed|could not download|missing required" /var/log/day0.log | tail -5 | sed 's/^/      /'; } || pass "no error in /var/log/day0.log"
  grep -E "installed in|registry credentials|installing the" /var/log/day0.log | tail -4 | sed 's/^/      /'
fi

hdr "k3s"
command -v k3s >/dev/null || { fail "k3s not installed"; exit 1; }
systemctl is-active --quiet k3s && pass "k3s service active" || fail "k3s service not active"
$K get nodes 2>&1 | sed 's/^/      /'
ls /var/lib/rancher/k3s/server/manifests 2>/dev/null | grep -E "encryptor|ai-gateway|kme" | sed 's/^/      manifest: /'
[ -f /etc/rancher/k3s/registries.yaml ] && pass "registries.yaml present (private image pull configured)" || info "no registries.yaml (public images only)"

if $K get ns $NS >/dev/null 2>&1; then
  hdr "encryptor pods"
  $K -n $NS get pods -o wide 2>&1 | sed 's/^/      /'
  bad=$($K -n $NS get pods --no-headers 2>/dev/null | awk '$3!="Terminating"{split($2,a,"/"); if ($3!~/Running|Completed/ || a[1]!=a[2]) n++} END{print n+0}')
  [ "$bad" -eq 0 ] && pass "all pods Running and ready" || { fail "$bad pod(s) not Running"; $K -n $NS get events --sort-by=.lastTimestamp 2>/dev/null | grep -iE "fail|error|pull|back-off" | tail -6 | sed 's/^/      /'; }
  ready=$($K -n $NS get deploy encryptor -o jsonpath='{.status.readyReplicas}' 2>/dev/null)
  [ "${ready:-0}" -ge 1 ] && pass "deployment encryptor ready" || fail "deployment encryptor not ready"

  hdr "site config (no secrets)"
  $K -n $NS get cm site -o jsonpath='{.data.config\.json}' 2>/dev/null | python3 -m json.tool 2>/dev/null | sed 's/^/      /' | head -40
  $K -n $NS get cm site -o jsonpath='{.data.macsec\.env}' 2>/dev/null | sed 's/^/      /'
  $K -n $NS get secret kme-tls >/dev/null 2>&1 && pass "Secret kme-tls present" || info "Secret kme-tls absent (fine for BASIC / http KME)"

  hdr "qkd-exchange"
  $K -n $NS logs deploy/encryptor -c qkd-exchange --tail=15 2>&1 | sed 's/^/      /'
  $K -n $NS logs deploy/encryptor -c qkd-exchange --tail=200 2>/dev/null | grep -qE "rotated|applied" && pass "keys rotated/applied" || fail "no key rotation yet (peer up? KME reachable?)"

  hdr "IPsec"
  $K -n $NS exec deploy/encryptor -c strongswan -- swanctl --list-sas 2>&1 | sed 's/^/      /' | head -20
  $K -n $NS exec deploy/encryptor -c strongswan -- swanctl --list-sas 2>/dev/null | grep -q ESTABLISHED && pass "IKE SA established" || fail "no established IKE SA"

  hdr "macsec"
  $K -n $NS logs deploy/encryptor -c macsec --tail=6 2>&1 | sed 's/^/      /'
  ip -br link show type macsec 2>/dev/null | sed 's/^/      /'

  hdr "KME simulator (BASIC master only)"
  $K -n $NS get deploy kme-sim >/dev/null 2>&1 && $K -n $NS get pods -l app=kme-sim --no-headers | sed 's/^/      /' || info "not deployed here (expected on the BASIC slave and in ADVANCED)"

  hdr "listening ports"
  ss -lntu 2>/dev/null | grep -E ":(8443|8444|500|4500)\b" | sed 's/^/      /'
elif $K get ns ai-gateway >/dev/null 2>&1; then
  hdr "ai-gateway pods"
  $K -n ai-gateway get pods 2>&1 | sed 's/^/      /'
  bad=$($K -n ai-gateway get pods --no-headers 2>/dev/null | awk '$3!="Terminating"{split($2,a,"/"); if ($3!~/Running|Completed/ || a[1]!=a[2]) n++} END{print n+0}')
  [ "$bad" -eq 0 ] && pass "all pods Running and ready" || fail "$bad pod(s) not ready yet (LLM Guard needs ~10 min on first start; a rollout in progress is normal)"
  curl -ks -o /dev/null -w "      LiteLLM /health/liveliness -> %{http_code}\n" https://localhost/health/liveliness
else
  fail "neither namespace encryptor nor ai-gateway exists: the manifest was not applied (see Day 0 above)"
fi
echo
