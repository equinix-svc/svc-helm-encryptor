#!/usr/bin/env bash
# console-setup.sh - publish the master's console at https://<FQDN> with a Let's Encrypt certificate. Run by Day 0 on the master.
# Environment: K (kubectl command), MAN (k3s manifests directory), NODE_IP (address the console listens on), FQDN,
# ADMIN_EMAIL (login name and certificate contact), ADMIN_PASSWORD (generated when empty), CRED_DIR (default /etc/encryptor).
# It creates Secret console-auth (the console's login, as a PBKDF2 hash), /etc/encryptor/credentials.env (root only) and
# /etc/encryptor/fqdn, and writes traefik-config.yaml (Let's Encrypt resolver, persistent certificate store) and
# console-ingress.yaml (Service and EndpointSlice pointing at the node address, plus the Ingress) into MAN.
set -euo pipefail
K="${K:-k3s kubectl}"
MAN="${MAN:-/var/lib/rancher/k3s/server/manifests}"
CRED_DIR="${CRED_DIR:-/etc/encryptor}"
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

cat > "$MAN/traefik-config.yaml" <<EOF
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

cat > "$MAN/console-ingress.yaml" <<EOF
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
echo "console published at https://$FQDN (login: $ADMIN_EMAIL, password in $CRED_DIR/credentials.env)"
