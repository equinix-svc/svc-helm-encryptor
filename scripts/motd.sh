# QKD Encryptor login banner. Day 0 installs this file as /etc/profile.d/qkd-encryptor-motd.sh; it is shown on interactive
# logins only (not for scp, "ssh host command" or VS Code Remote-SSH). It never prints a secret. MOTD_FORCE=1 shows it anywhere.
[ -n "${BASH_VERSION:-}" ] || return 0 2>/dev/null
if [ -z "${MOTD_FORCE:-}" ]; then
  case $- in *i*) ;; *) return 0 2>/dev/null ;; esac
  [ -t 1 ] || return 0
  [ -z "${QKDENC_MOTD_SHOWN:-}" ] || return 0
fi
export QKDENC_MOTD_SHOWN=1

_qkdenc_motd() {
  local B="" D="" G="" Y="" N="" ip host cfg role sae peer kme sim pods_r pods_t pstat sas klog last age scheme
  if [ -z "${NO_COLOR:-}" ]; then B=$'\e[1m'; D=$'\e[2m'; G=$'\e[32m'; Y=$'\e[33m'; N=$'\e[0m'; fi
  _k() {   # kubectl as the login user (kubeconfig copied by Day 0), else sudo; never blocks the login for long
    if [ -r "$HOME/.kube/config" ]; then KUBECONFIG="$HOME/.kube/config" timeout 3 k3s kubectl "$@" 2>/dev/null
    else timeout 3 sudo -n k3s kubectl "$@" 2>/dev/null; fi
  }
  host=$(hostname)
  ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}'); ip=${ip:-<node-ip>}

  cfg=$(_k -n encryptor get cm site -o jsonpath='{.data.config\.json}')
  read -r sae peer kme role < <(printf '%s' "$cfg" | python3 -c '
import json,sys
try:
    d=json.load(sys.stdin); c=d["consumers"][0]
    print(d["sae_id"], c["peer_sae"], d["kme"]["url"], c["role"])
except Exception:
    print("? ? ? ?")' 2>/dev/null)
  peeraddr=$(printf '%s' "$cfg" | python3 -c 'import json,sys;print(json.load(sys.stdin)["peer"]["url"].split("//")[1].split(":")[0])' 2>/dev/null)
  scheme=$(printf '%s' "$cfg" | python3 -c 'import json,sys;print(json.load(sys.stdin)["peer"]["url"].split(":")[0])' 2>/dev/null)

  read -r pods_r pods_t < <(_k -n encryptor get pods --no-headers | awk '$3!="Terminating"{t++; split($2,a,"/"); if($3=="Running" && a[1]==a[2]) r++} END{print r+0, t+0}')
  sas=$(_k -n encryptor exec deploy/encryptor -c strongswan -- swanctl --list-sas 2>/dev/null | grep -c ESTABLISHED)
  klog=$(_k -n encryptor logs deploy/encryptor -c qkd-exchange --tail=200 | grep -E "rotated|applied" | tail -1 | awk '{print $1" "$2}' | cut -d, -f1)
  if [ -n "$klog" ]; then
    last=$(date -d "$klog" +%s 2>/dev/null); age=$(( ( $(date +%s) - ${last:-0} ) / 60 )); klog="last key ${age} min ago"
  else klog="no key yet"; fi

  if [ "${pods_t:-0}" -eq 0 ]; then pstat="${Y}not available yet (k3s or the stack is still starting)${N}"
  else
    pstat="pods ${pods_r}/${pods_t} ready"; [ "$pods_r" -eq "$pods_t" ] && pstat="${G}${pstat}${N}" || pstat="${Y}${pstat}${N}"
    if [ "${sas:-0}" -ge 1 ]; then pstat="$pstat · ${G}IPsec: $sas SA established${N}"; else pstat="$pstat · ${Y}IPsec: no SA yet${N}"; fi
    pstat="$pstat · $klog"
  fi
  sim=""; _k -n encryptor get deploy kme-sim >/dev/null && sim=yes

  printf '\n%sQKD Encryptor%s · IPsec · MACsec · SSH          %s%s%s\n' "$B" "$N" "$D" "$host" "$N"
  printf '%s\n' "──────────────────────────────────────────────────────────────────────────────"
  if [ -n "$peeraddr" ]; then
    printf '  Role        %s (%s)  <->  peer %s (%s)\n' "$role" "$sae" "$peeraddr" "$peer"
    printf '  KME         %s\n' "$kme"
  fi
  printf '  Status      %s\n\n' "$pstat"
  [ -n "$scheme" ] && printf '  %sWEB INTERFACES%s\n' "$B" "$N"
  [ "$role" = master ] && printf '  %-15s %-38s %s\n' "Console" "http://localhost:8080" "SSH tunnel: ssh -L 8080:localhost:8080 ${USER:-<user>}@$ip"
  [ -n "$scheme" ] && printf '  %-15s %-38s %s\n' "Peer API"   "$scheme://$ip:8444/v1/status" "JSON, key rotation state (mTLS in ADVANCED)"
  [ -n "$sim" ] && [ -n "$peer" ] && printf '  %-15s %-38s %s\n' "KME simulator" "http://$ip:8443/api/v1/keys/$peer/status" "JSON, ETSI 014 status (lab, BASIC master)"
  printf '\n  %sTIPS%s\n' "$B" "$N"
  printf '   • Tunnel:    sudo k3s kubectl -n encryptor exec deploy/encryptor -c strongswan -- swanctl --list-sas\n'
  printf '   • Keys:      sudo k3s kubectl -n encryptor logs deploy/encryptor -c qkd-exchange --tail=20\n'
  printf '   • Real KME:  load the certificates after boot (README, section 4)\n'
  printf '   • MACsec:    set MACSEC_IFACE later: patch ConfigMap site, then rollout restart deploy/encryptor\n'
  printf '   • SSH keys:  /var/lib/encryptor/ssh/ssh/\n'
  printf '   • Health:    curl -fsSL https://raw.githubusercontent.com/equinix-svc/svc-helm-encryptor/main/scripts/check-install.sh \\\n'
  printf '                  | sudo bash\n'
  printf '   • Day 0 log: /var/log/day0.log\n\n'
}
_qkdenc_motd
unset -f _qkdenc_motd _k 2>/dev/null
