# svc-helm-encryptor

<img src="assets/logo.svg" alt="QKD Encryptor" width="120">

QKD-keyed encryptor appliance (IPsec, MACsec, SSH) for a single k3s node. It is one `encryptor.yaml` for a k3s node, plus an Equinix Day 0 template that creates the site-specific Secret and ConfigMap. It has not been deployed on a device or tested against a real KME. The pieces were tested in Docker (section 4).

## 1. Stack

| Role | Component |
|---|---|
| KME client and key distribution | `qkd-exchange`, written for this project. It is about 300 lines of Python and talks ETSI 014 (`enc_keys`, `dec_keys`, `status`) over mTLS. No maintained open-source ETSI 014 consumer exists. |
| IPsec | strongSwan 5.9 with the QKD key injected as an RFC 8784 post-quantum preshared key (PPK), set to `ppk_required`. |
| MACsec | `wpa_supplicant` 2.11 (`macsec_linux` driver, Debian sid image). The QKD key is the MKA connectivity association key (CAK, 256-bit, GCM-AES-256) and the key ID is the CKN. |
| SSH key pairs | `qkd-exchange` rotates Ed25519 pairs. The QKD key is mixed into the key seed and authenticates the public key sent to the peer. |
| Lab KME | `exchange.py sim`, deployed with `dev-kme-sim.yaml`. It gives random in-memory keys, not QKD. |

It all runs as one hostNetwork Pod with three containers (`strongswan`, `macsec`, `qkd-exchange`) built from `src/`. Keys pass between containers through RAM-only volumes.

One master/slave pair rotates keys. The master requests keys from its KME, sends only the key ID to the slave over an mTLS peer API (port 8444), and the slave fetches the same key from its own KME. Both ends then apply it together (stage, then commit). Keys are never logged.

| Use case | Key use | Rotation | Takes effect |
|---|---|---|---|
| IPsec | PPK `qkd-s2s` in `/swanctl/conf.d` | 1 hour | next IKE reauthentication (1 hour) |
| MACsec | CAK/CKN in `/macsec/macsec.conf` | 24 hours | `wpa_supplicant` reconfigures within 5 seconds |
| SSH | Ed25519 pair under `/var/lib/encryptor/ssh/ssh` | 24 hours | at rotation |

## 2. Quick start

| Step | What |
|---|---|
| 1. VM image | Plain `k3s-node` image (`k3s-node.qcow2`) from [svc-ne-byoi-builder](https://github.com/equinix-svc/svc-ne-byoi-builder). Portal settings: `docs/portal-values.md` in that repo. |
| 2. Container images | `ghcr.io/equinix-svc/svc-helm-encryptor/{qkd-exchange,strongswan,macsec}:0.1.0`, built by the `images` GitHub Actions workflow on every change under `src/` (or by hand with `scripts/push-images.sh`). The packages are private: the node pulls them with `REGISTRY_USER` and `REGISTRY_TOKEN` (required). The images must exist before the first boot. |
| 3. Day 0 file | [`day0/day0-encryptor-basic.yaml`](day0/day0-encryptor-basic.yaml) for a first test, [`day0/day0-encryptor-advanced.yaml`](day0/day0-encryptor-advanced.yaml) for a real KME. |
| 4. Parameters | Five required for BASIC, see below. Full list: [Day 0 templates](#3-day-0-templates). Certificates for a real KME are loaded after boot: [section 4](#4-load-kme-certificates-after-boot). |
| 5. Check | On the node: `curl -fsSL https://raw.githubusercontent.com/equinix-svc/svc-helm-encryptor/main/scripts/check-install.sh \| sudo bash`. It is read-only, prints PASS/FAIL and never prints secrets. |

BASIC, one appliance of each role (the master also runs the KME simulator):

| Parameter | Master | Slave |
|---|---|---|
| `QKD_ROLE` | `master` | `slave` |
| `PEER_ADDR` | the slave's IP, e.g. `172.16.0.12` | the master's IP, e.g. `172.16.0.11` |
| `IKE_PSK` | the same secret, e.g. `openssl rand -hex 16` | the same secret |
| `REGISTRY_USER` | your GitHub login | your GitHub login |
| `REGISTRY_TOKEN` | a `read:packages` token (the same on both) | the same token |

Optional: `LOCAL_ADDR` (this appliance's own IP towards the peer, e.g. `172.16.0.11`; needed when the peer is reached over a second interface), `MACSEC_IFACE` (e.g. `ens4`) and `LOCAL_TS` / `REMOTE_TS` (e.g. `10.1.0.0/24` / `10.2.0.0/24`). ACL: allow 22, UDP 500/4500, ESP, TCP 8443 and 8444 from the peer.

## 3. Day 0 templates

Two templates, same structure. Both do what the k3s-node image's own Day 0 does, then on a `single` node or the first `server` create ConfigMap `site` and Secret `site-secrets` in namespace `encryptor` and download `encryptor.yaml` (and `dev-kme-sim.yaml` on the BASIC master) into the k3s manifests directory. Render locally with `DAY0_FILE=<file> scripts/render-day0.sh`. Log: `/var/log/day0.log`.

| Template | Use | Required parameters |
|---|---|---|
| [`day0/day0-encryptor-basic.yaml`](day0/day0-encryptor-basic.yaml) | **BASIC** (lab): bring up a pair quickly with the KME simulator | 5, the rest optional |
| [`day0/day0-encryptor-advanced.yaml`](day0/day0-encryptor-advanced.yaml) | **ADVANCED**: real KME, per-site values | 8, the rest optional |

Equinix predefined (both): `hostname` `userName` `publicKey` `adminPwd` `ipAddressWithCidr` `gateway`. Declare every other variable as a custom parameter in the portal.

### Parameters

The **BASIC** and **ADVANCED** columns show whether the parameter is required in that template: `yes`, `no`, or `-` (not used). **Default** is what the template uses when a parameter that is not required is left empty.

| Parameter | BASIC | ADVANCED | Default | Example (paste in the portal) | Meaning |
|---|---|---|---|---|---|
| `QKD_ROLE` | yes | yes | - | `master` | `master` (calls `enc_keys`, initiates IPsec) or `slave` (calls `dec_keys`). In BASIC, the master is SAE-A and also runs the KME simulator; the slave is SAE-B. |
| `PEER_ADDR` | yes | yes | - | `172.16.0.12` | The other appliance's address, used for IKE and the peer API on 8444. Any address reachable from this appliance works, a private one included. With mutual TLS (ADVANCED) it must match a name or IP in the peer certificate. |
| `LOCAL_ADDR` | no | no | address of the default route | `172.16.0.11` | This appliance's own IP towards the peer. It is the IKE source address and the only address the peer API listens on. Set it when the peer is reached over a second interface or subnet. |
| `SAE_ID` | - | yes | - | `SAE-A` | This appliance's SAE ID on its KME. BASIC fixes it to `SAE-A` (master) or `SAE-B` (slave). |
| `PEER_SAE_ID` | - | yes | - | `SAE-B` | The remote appliance's SAE ID. BASIC fixes it to the opposite of the local one. |
| `KME_URL` | - | yes | - | `https://172.16.1.5:8443` | ETSI GS QKD 014 address of this site's KME, the QKD device (not the other encryptor). Any subnet reachable from the appliance works. `https://` means mutual TLS: the certificates are loaded after boot (section 4), and the host in the URL (name or IP) must be in the KME certificate. BASIC sets it itself: `http://127.0.0.1:8443` on the master and `http://<PEER_ADDR>:8443` on the slave. |
| `IKE_PSK` | yes | yes | - | `4f9c2a7e1b8d3c6a5e0f9b2d7c4a1e83` | IKEv2 secret, identical on both ends, never stored in the repo. The IPsec tunnel is always configured, so both templates refuse to start without it. Generate with `openssl rand -hex 16`. |
| `LOCAL_TS` | no | no | `0.0.0.0/0` | `10.1.0.0/24` | Local protected subnet. |
| `REMOTE_TS` | no | no | `0.0.0.0/0` | `10.2.0.0/24` | Remote protected subnet. |
| `MACSEC_IFACE` | no | no | empty (no MACsec) | `ens4` | Host NIC for MACsec. |
| `MACSEC_ADDR` | no | no | empty (no address) | `10.99.0.1/24` | CIDR on the macsec interface. |
| `K3S_ROLE` | no | no | `single` | `single` | `single`: standalone node. `server`: control-plane node. `agent`: k3s worker node that joins an existing cluster; the encryptor is not installed on it. |
| `K3S_SERVER_URL` | no (agent only) | no (agent only) | empty | `https://10.0.0.5:6443` | URL used by an agent to join the k3s cluster. Empty for `single` or the first `server`. |
| `K3S_TOKEN` | no (agent only) | no (agent only) | empty | `K10abc123def456::server:789xyz` | Secret used to join the k3s cluster. |
| `K3S_TLS_SAN` | no | no | empty | `172.16.0.11` | Extra name or IP in the k3s API server certificate, for `kubectl` or agents reaching port 6443 from outside. Not needed for local `sudo k3s kubectl`. |
| `STACK_REF` | no | no | `main` | `main` | Repo branch or tag to fetch. Pin a tag for repeatable boots. |
| `STACK_TOKEN` | no | no | empty | leave empty | Read-only GitHub token, only for a private fork. |
| `REGISTRY_USER` | yes | yes | - | `my-github-user` | GitHub login used to pull the private images from ghcr.io. Day 0 stops the encryptor install without it. |
| `REGISTRY_TOKEN` | yes | yes | - | `ghp_AbCdEf1234567890AbCdEf1234567890AbCd` | Classic personal access token with only `read:packages`. Written to `/etc/rancher/k3s/registries.yaml` (root only) before k3s starts. Expires: set a reminder to rotate it. |

BASIC uses plain HTTP for the KME and the peer API. ADVANCED does too when `KME_URL` is `http://`. Its simulator gives random keys, **not QKD**, with no authentication.

| Template | ACL |
|---|---|
| BASIC | allow 22, UDP 500/4500, ESP, and TCP 8443 and 8444 from the peer only |
| ADVANCED | allow 22, UDP 500/4500, ESP and TCP 8444 from the peer |

## 4. Load KME certificates after boot

The KME client certificates are **not** Day 0 parameters, so the private key never goes through the portal. They are loaded into Secret `kme-tls` once the node is up. Used with ADVANCED when `KME_URL` is `https://`; the same files also protect the peer API (port 8444).

| File | Content |
|---|---|
| `ca.crt` | CA that signed the KME certificate and the peer appliance's certificate |
| `tls.crt` | This appliance's client certificate (the KME maps it to the SAE ID). It is also the server certificate of the peer API, so its SAN must contain the value other appliances use for it in `PEER_ADDR` (an IP or a DNS name) |
| `tls.key` | Its private key |

1. Copy the three PEM files to the node (`scp`).
2. Create the Secret:
   ```bash
   sudo k3s kubectl -n encryptor create secret generic kme-tls \
     --from-file=ca.crt=ca.pem --from-file=tls.crt=client.crt --from-file=tls.key=client.key
   ```
3. Check: `sudo k3s kubectl -n encryptor logs deploy/encryptor -c qkd-exchange --tail=20`. Until the Secret exists the log says `waiting for KME certificates`. Then `qkd-exchange` starts by itself (the Secret volume refreshes within a minute or two) and no restart is needed.
4. Delete the PEM files from the node: `shred -u ca.pem client.crt client.key`.

To rotate them, update the Secret (`... --dry-run=client -o yaml | sudo k3s kubectl apply -f -`) and run `sudo k3s kubectl -n encryptor rollout restart deploy/encryptor`.

## 5. What was tested

- The exchange service and simulator ran end to end with a master and a slave. IPsec and MACsec key files matched on both sides (only the MKA priority differs), and the SSH public key passed its authentication check and was installed.
- Two strongSwan containers established IKEv2 with the PPK. A wrong PPK failed with `AUTH_FAILED`.
- Two `macsec` containers joined by a veth pair established MKA with GCM-AES-256 from a config generated by qkd-exchange. Ping passed and the wire carried 802.1AE frames.
- The peer API ran over mutual TLS with generated certificates: `qkd-exchange` waited until the certificates existed, accepted the valid client, and refused a client with no certificate and one from another CA.
- The three images build, the manifest parses, and Day 0 renders and passes a shell syntax check.

## 6. Findings and limits

- **MACsec on Equinix vNICs:** the virtual switch must pass EAPOL (`01:80:c2:00:00:03`) and EtherType `0x88e5` frames between the two appliances. This is untested. Use a dedicated NIC (`MACSEC_IFACE`), not the management one.
- **MACsec image:** 256-bit MACsec needs `wpa_supplicant` 2.11 (`macsec_csindex`). Debian trixie ships 2.10 and Alpine has no MACsec driver, so the image uses `debian:sid-slim`. Pin it by digest once validated.
- **MACsec rotation:** a new CAK starts a new MKA session, which can interrupt traffic briefly. The default interval is 24 hours.
- **PPK timing:** a new PPK only takes effect at the next IKE reauthentication, not on child-SA rekeys.
- **PPK ID:** it is fixed, so a brief key-switch window can fail an authentication. That is retried.
- **SSH:** QKD can't plug into the SSH transport itself. qkd-exchange only rotates key pairs, and you still need to wire them into `sshd` or a CA.
- **Untested:** a real KME (certificate and SAE ID mapping vary by vendor) and deployment on k3s.
- **IKE authentication:** it uses a shared secret (`IKE_PSK`) for now. Certificates would be better for production.
- **Day 0 size:** the templates are about 14 KB, under the portal limit of 20,000 characters. No certificate goes through Day 0.
- **Secrets in Day 0:** the Day 0 files hold no secret. `IKE_PSK` (and `K3S_TOKEN` for an agent, `REGISTRY_TOKEN`) still travel as portal parameters and end up in a Kubernetes Secret on the node, readable by root.
- **Images:** `encryptor.yaml` pulls `ghcr.io/equinix-svc/svc-helm-encryptor/{qkd-exchange,strongswan,macsec}:0.1.0`. Build them from `src/<name>/` and push them to that registry (or change the names in `encryptor.yaml.in`) before the first boot. A private registry needs a pull secret.

## 7. Build and validate

Edit `encryptor.yaml.in` and `src/`, then run `scripts/validate.sh`. Pushing a change under `src/` rebuilds and publishes the images (workflow `images`). It builds `encryptor.yaml` and the images and checks Day 0. Commit both the sources and the generated file.
