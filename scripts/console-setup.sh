#!/usr/bin/env bash
# console-setup.sh - publish the master's console at https://<FQDN> with a Let's Encrypt certificate. Run by Day 0 on the master.
# Environment: K (kubectl command), MAN (k3s manifests directory), NODE_IP (address the console listens on), FQDN,
# ADMIN_EMAIL (login name and certificate contact), ADMIN_PASSWORD (generated when empty), CRED_DIR (default /etc/encryptor).
# It creates Secret console-auth (the console's login, as a PBKDF2 hash), /etc/encryptor/credentials.env (root only) and
# /etc/encryptor/fqdn. The node's public IP is only known after creation, so the DNS record comes later: traefik-config.yaml
# (Let's Encrypt resolver, persistent certificate store) and console-ingress.yaml (Service and EndpointSlice pointing at the
# node address, plus the Ingress) are written into a PENDING directory, and a systemd service moves them into MAN as soon as
# FQDN resolves to this node: the first Let's Encrypt attempt then succeeds. The service state is read by the login banner.
set -euo pipefail
K="${K:-k3s kubectl}"
MAN="${MAN:-/var/lib/rancher/k3s/server/manifests}"
CRED_DIR="${CRED_DIR:-/etc/encryptor}"
PENDING="${PENDING:-/var/lib/encryptor/pending}"
BIN="${BIN:-/usr/local/bin/enc-dns-wait.sh}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
: "${NODE_IP:?NODE_IP is required}"

FQDN="$(printf '%s' "${FQDN:-}" | tr 'A-Z' 'a-z')"
printf '%s' "$FQDN" | grep -Eq '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$' || { echo "FQDN is not a valid name: '$FQDN'"; exit 1; }
case "${ADMIN_EMAIL:-}" in *@*.*) ;; *) echo "the console needs a real e-mail address in ADMIN_EMAIL (got: '${ADMIN_EMAIL:-}')"; exit 1 ;; esac
if [ -z "${ADMIN_PASSWORD:-}" ]; then ADMIN_PASSWORD="$(head -c 12 /dev/urandom | od -An -vtx1 | tr -d ' \n')"; fi

# console login: PBKDF2-HMAC-SHA256 hash, read by qkd-exchange from the Secret
AUTH="$(PW="$ADMIN_PASSWORD" US="$ADMIN_EMAIL" python3 - <<'PY'
import hashlib, os
salt = os.urandom(16); it = 200000
h = hashlib.pbkdf2_hmac("sha256", os.environ["PW"].encode(), salt, it)
print("%s:pbkdf2:%d:%s:%s" % (os.environ["US"], it, salt.hex(), h.hex()))
PY
)"
$K create namespace encryptor --dry-run=client -o yaml | $K apply -f -
$K -n encryptor create secret generic console-auth --from-literal=auth="$AUTH" --dry-run=client -o yaml | $K apply -f -

install -d -m 0755 "$CRED_DIR"
( umask 077; printf 'ADMIN_EMAIL=%s\nADMIN_PASSWORD=%s\n' "$ADMIN_EMAIL" "$ADMIN_PASSWORD" > "$CRED_DIR/credentials.env" )
printf '%s\n' "$FQDN" > "$CRED_DIR/fqdn"

mkdir -p "$PENDING"
rm -f "$MAN/traefik-config.yaml" "$MAN/console-ingress.yaml"
cat > "$PENDING/traefik-config.yaml" <<EOF
apiVersion: helm.cattle.io/v1
kind: HelmChartConfig
metadata: { name: traefik, namespace: kube-system }
spec:
  valuesContent: |-
    persistence: { enabled: true, size: 128Mi }
    certificatesResolvers:
      le:
        acme:
          email: "$ADMIN_EMAIL"
          caServer: "https://acme-v02.api.letsencrypt.org/directory"
          storage: /data/acme.json
          httpChallenge: { entryPoint: web }
    deployment:
      initContainers:
        - name: volume-permissions
          image: busybox:1.37
          command: ["sh", "-c", "touch /data/acme.json; chmod -v 600 /data/acme.json"]
          volumeMounts: [{ name: data, mountPath: /data }]
    podSecurityContext: { fsGroup: 65532, fsGroupChangePolicy: OnRootMismatch }
EOF

cat > "$PENDING/console-ingress.yaml" <<EOF
# https://$FQDN/ -> the master console, which listens on the node address $NODE_IP:8080 (the Pod uses the host network)
apiVersion: v1
kind: Service
metadata: { name: console, namespace: encryptor }
spec:
  ports: [{ name: http, port: 8080, targetPort: 8080 }]
---
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata:
  name: console-node
  namespace: encryptor
  labels: { kubernetes.io/service-name: console }
addressType: IPv4
ports: [{ name: http, port: 8080, protocol: TCP }]
endpoints: [{ addresses: ["$NODE_IP"] }]
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: console
  namespace: encryptor
  annotations:
    traefik.ingress.kubernetes.io/router.entrypoints: websecure
    traefik.ingress.kubernetes.io/router.tls.certresolver: le
spec:
  tls: [{ hosts: ["$FQDN"] }]
  rules:
    - host: "$FQDN"
      http: { paths: [{ path: /, pathType: Prefix, backend: { service: { name: console, port: { number: 8080 } } } }] }
EOF

# --- wait for DNS, then enable the certificates
cat > "$CRED_DIR/dns-wait.conf" <<DNSEOF
NAMES="$FQDN"
PENDING="$PENDING"
MAN="$MAN"
STATE="$CRED_DIR/dns-wait.state"
DNSEOF
cat > "$BIN" <<'WAITEOF'
#!/usr/bin/env bash
# enc-dns-wait.sh <conf> - wait until every name in NAMES resolves to this node (a local address or the address the internet
# sees), then move the pending manifests into the k3s manifests directory: Traefik then asks Let's Encrypt for the certificate.
# State (one line) goes to $STATE: "waiting ip=<address to use> names=<a,b>" or "ready <date>".
set -u
. "$1"
if grep -q '^ready' "$STATE" 2>/dev/null; then exit 0; fi          # done before (reboot): the manifests are already in place
while true; do
  ext=$(curl -fsS -m 5 https://api.ipify.org 2>/dev/null | grep -E '^[0-9.]+$' || true)
  mine=$( { ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1; [ -n "$ext" ] && echo "$ext"; } | sort -u )
  use=${ext:-$(echo "$mine" | head -1)}
  ok=1
  for n in $NAMES; do
    got=$(getent ahostsv4 "$n" 2>/dev/null | awk '{print $1}' | sort -u)
    if [ -z "$got" ] || ! comm -12 <(echo "$got") <(echo "$mine") | grep -q .; then ok=0; fi
  done
  if [ "$ok" = 1 ]; then
    cp "$PENDING"/*.yaml "$MAN"/ && echo "ready $(date -Is)" > "$STATE"
    exit 0
  fi
  echo "waiting ip=$use names=$(echo $NAMES | tr ' ' ',')" > "$STATE"
  sleep "${DNS_WAIT_INTERVAL:-30}"
done
WAITEOF
chmod 0755 "$BIN"
echo "waiting ip=pending names=$FQDN" > "$CRED_DIR/dns-wait.state"
cat > "$UNIT_DIR/enc-dns-wait.service" <<UNITEOF
[Unit]
Description=Wait for the DNS record of the console name, then enable the certificate
After=network-online.target
Wants=network-online.target
[Service]
ExecStart=$BIN $CRED_DIR/dns-wait.conf
Restart=on-failure
RestartSec=30
[Install]
WantedBy=multi-user.target
UNITEOF
if [ -d /run/systemd/system ] && [ -z "${NO_SYSTEMD:-}" ]; then systemctl daemon-reload; systemctl enable --now enc-dns-wait.service; fi
echo "console: https://$FQDN will answer as soon as the DNS record of $FQDN points to this node (login: $ADMIN_EMAIL, password in $CRED_DIR/credentials.env)"
