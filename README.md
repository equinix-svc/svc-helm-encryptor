# svc-helm-encryptor

QKD-keyed encryptor appliance for a single k3s node. It is one `encryptor.yaml` for a k3s node, plus an Equinix Day 0 template that creates the site-specific Secret and ConfigMap. It has not been deployed on a device or tested against a real KME. The pieces below were tested in Docker.

## Proposed stack

| Role | Component |
|---|---|
| KME client and key distribution | `qkd-agent`, written for this project. It is about 300 lines of Python and talks ETSI 014 (`enc_keys`, `dec_keys`, `status`) over mTLS. No maintained open-source ETSI 014 consumer exists. |
| IPsec | strongSwan 5.9 with the QKD key injected as an RFC 8784 post-quantum preshared key (PPK), set to `ppk_required`. |
| TLS tunnels (HTTPS or any TCP) | stunnel 5.75 on OpenSSL 3.5, using TLS 1.3 with the QKD key as an external preshared key. |
| SSH key pairs | `qkd-agent` rotates Ed25519 pairs. The QKD key is mixed into the key seed and authenticates the public key sent to the peer. |
| Lab KME | `agent.py sim`, deployed with `dev-kme-sim.yaml`. It gives random in-memory keys, not QKD. |

It all runs as one hostNetwork Pod with three containers, plus two small custom images (strongSwan and stunnel). Keys pass between containers through RAM-only volumes.

One master/slave pair rotates keys. The master requests keys from its KME, sends only the key ID to the slave over an mTLS peer API (port 8444), and the slave fetches the same key from its own KME. Both ends then apply it together (stage, then commit). The default interval is 1 hour for IPsec and TLS and 24 hours for SSH. Keys are never logged.

## What was tested

- The agent and simulator ran end to end with a master and a slave. IPsec and TLS key files matched on both sides, and the SSH public key passed its authentication check and was installed.
- Two strongSwan containers established IKEv2 with the PPK. A wrong PPK failed with `AUTH_FAILED`.
- stunnel carried traffic over a TLS 1.3 preshared-key tunnel.
- The three images build, the manifest parses, and Day 0 renders and passes a shell syntax check.

## Findings and limits

- **stunnel preshared keys:** they only work with SHA-256 TLS 1.3 suites. With AES-256-GCM-SHA384 the handshake failed, so the config forces the SHA-256 suites.
- **PPK timing:** a new PPK only takes effect at the next IKE reauthentication (set to 1 hour), not on child-SA rekeys.
- **PPK ID:** it is fixed, so a brief key-switch window can fail an authentication. That is retried.
- **SSH:** QKD can't plug into the SSH transport itself. The agent only rotates key pairs (under `/var/lib/encryptor/ssh`), and you still need to wire them into `sshd` or a CA.
- **Untested:** a real KME (certificate and SAE ID mapping vary by vendor), the peer API over mTLS, and deployment on k3s.
- **IKE authentication:** it uses a shared secret (`IKE_PSK`) for now. Certificates would be better for production.
- **Images:** `encryptor.yaml` pulls `ghcr.io/equinix-svc/svc-helm-encryptor/{qkd-agent,strongswan,stunnel}:0.1.0`. Build them from `src/<name>/` and push them to that registry (or change the names in `encryptor.yaml.in`) before the first boot. A private registry needs a pull secret.

## Build and validate

Edit `encryptor.yaml.in` and `src/`, then run `scripts/validate.sh`. It builds `encryptor.yaml` and the images and checks Day 0. Commit both the sources and the generated file.

## Day 0 parameters

[`day0/day0-encryptor.yaml`](day0/day0-encryptor.yaml) does what the k3s-node image's own Day 0 does, then on a `single` node or the first `server` creates ConfigMap `site` and Secret `site-secrets` in namespace `encryptor` and downloads `encryptor.yaml` into the k3s manifests directory. Render it locally with `scripts/render-day0.sh`.

Equinix predefined: `hostname` `userName` `publicKey` `adminPwd` `ipAddressWithCidr` `gateway`.

Custom parameters:

| Parameter | Required | Meaning |
|---|---|---|
| `K3S_ROLE` `K3S_SERVER_URL` `K3S_TOKEN` `K3S_TLS_SAN` | no | Same as the k3s-node image. The encryptor installs only on `single` or the first `server`. |
| `SAE_ID` | yes | This appliance's SAE ID on its KME. |
| `PEER_SAE_ID` | yes | The remote appliance's SAE ID. |
| `QKD_ROLE` | yes | `master` (calls `enc_keys`, initiates IPsec) or `slave` (calls `dec_keys`). |
| `KME_URL` | yes | ETSI GS QKD 014 base URL, e.g. `https://kme.local:8443`. |
| `PEER_ADDR` | yes | Remote appliance IP, used for IKE and the peer API on 8444. |
| `KME_CA_B64` `KME_CERT_B64` `KME_KEY_B64` | no | Base64 PEM CA, client certificate and key. Used for both the KME and the peer API. |
| `IKE_PSK` | for IPsec | IKEv2 authentication secret, identical on both ends. |
| `LOCAL_TS` `REMOTE_TS` | no | Protected subnets, e.g. `10.1.0.0/24` and `10.2.0.0/24`. Default `0.0.0.0/0`. |
| `TLS_MODE` | no | `server`, `client`, or empty for no TLS tunnel. |
| `TLS_ACCEPT` | with `TLS_MODE` | Port to listen on (public for `server`, local for `client`). |
| `TLS_CONNECT` | with `TLS_MODE` | `host:port` to connect to. |
| `STACK_REF` | no | Branch or tag of this repo to fetch. Default `main`. |
| `STACK_TOKEN` | no | Read-only GitHub token, only for a private fork. |

ACL: allow 22, UDP 500/4500, ESP, TCP 8444 from the peer, and the `TLS_ACCEPT` port. Log: `/var/log/day0.log`.
