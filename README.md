# svc-helm-encryptor

QKD-keyed encryptor appliance (IPsec, MACsec, SSH) for a single k3s node. It is one `encryptor.yaml` for a k3s node, plus an Equinix Day 0 template that creates the site-specific Secret and ConfigMap. It has not been deployed on a device or tested against a real KME. The pieces below were tested in Docker.

## Proposed stack

| Role | Component |
|---|---|
| KME client and key distribution | `qkd-agent`, written for this project. It is about 300 lines of Python and talks ETSI 014 (`enc_keys`, `dec_keys`, `status`) over mTLS. No maintained open-source ETSI 014 consumer exists. |
| IPsec | strongSwan 5.9 with the QKD key injected as an RFC 8784 post-quantum preshared key (PPK), set to `ppk_required`. |
| MACsec | `wpa_supplicant` 2.11 (`macsec_linux` driver, Debian sid image). The QKD key is the MKA connectivity association key (CAK, 256-bit, GCM-AES-256) and the key ID is the CKN. |
| SSH key pairs | `qkd-agent` rotates Ed25519 pairs. The QKD key is mixed into the key seed and authenticates the public key sent to the peer. |
| Lab KME | `agent.py sim`, deployed with `dev-kme-sim.yaml`. It gives random in-memory keys, not QKD. |

It all runs as one hostNetwork Pod with three containers (`strongswan`, `macsec`, `qkd-agent`) built from `src/`. Keys pass between containers through RAM-only volumes.

One master/slave pair rotates keys. The master requests keys from its KME, sends only the key ID to the slave over an mTLS peer API (port 8444), and the slave fetches the same key from its own KME. Both ends then apply it together (stage, then commit). Keys are never logged.

| Use case | Key use | Rotation | Takes effect |
|---|---|---|---|
| IPsec | PPK `qkd-s2s` in `/swanctl/conf.d` | 1 hour | next IKE reauthentication (1 hour) |
| MACsec | CAK/CKN in `/macsec/macsec.conf` | 24 hours | `wpa_supplicant` reconfigures within 5 seconds |
| SSH | Ed25519 pair under `/var/lib/encryptor/ssh/ssh` | 24 hours | at rotation |

## What was tested

- The agent and simulator ran end to end with a master and a slave. IPsec and MACsec key files matched on both sides (only the MKA priority differs), and the SSH public key passed its authentication check and was installed.
- Two strongSwan containers established IKEv2 with the PPK. A wrong PPK failed with `AUTH_FAILED`.
- Two `macsec` containers joined by a veth pair established MKA with GCM-AES-256 from an agent-generated config. Ping passed and the wire carried 802.1AE frames.
- The three images build, the manifest parses, and Day 0 renders and passes a shell syntax check.

## Findings and limits

- **MACsec on Equinix vNICs:** the virtual switch must pass EAPOL (`01:80:c2:00:00:03`) and EtherType `0x88e5` frames between the two appliances. This is untested. Use a dedicated NIC (`MACSEC_IFACE`), not the management one.
- **MACsec image:** 256-bit MACsec needs `wpa_supplicant` 2.11 (`macsec_csindex`). Debian trixie ships 2.10 and Alpine has no MACsec driver, so the image uses `debian:sid-slim`. Pin it by digest once validated.
- **MACsec rotation:** a new CAK starts a new MKA session, which can interrupt traffic briefly. The default interval is 24 hours.
- **PPK timing:** a new PPK only takes effect at the next IKE reauthentication, not on child-SA rekeys.
- **PPK ID:** it is fixed, so a brief key-switch window can fail an authentication. That is retried.
- **SSH:** QKD can't plug into the SSH transport itself. The agent only rotates key pairs, and you still need to wire them into `sshd` or a CA.
- **Untested:** a real KME (certificate and SAE ID mapping vary by vendor), the peer API over mTLS, and deployment on k3s.
- **IKE authentication:** it uses a shared secret (`IKE_PSK`) for now. Certificates would be better for production.
- **Day 0 size:** the template is about 13.8 KB. The portal limit is 20,000 characters, and the three base64 certificate parameters can push the rendered text over it.
- **Images:** `encryptor.yaml` pulls `ghcr.io/equinix-svc/svc-helm-encryptor/{qkd-agent,strongswan,macsec}:0.1.0`. Build them from `src/<name>/` and push them to that registry (or change the names in `encryptor.yaml.in`) before the first boot. A private registry needs a pull secret.

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
| `MACSEC_IFACE` | no | Host NIC to protect with MACsec, e.g. `ens4`. Empty means no MACsec. |
| `MACSEC_ADDR` | no | CIDR to put on the macsec interface, e.g. `10.99.0.1/24`. |
| `STACK_REF` | no | Branch or tag of this repo to fetch. Default `main`. |
| `STACK_TOKEN` | no | Read-only GitHub token, only for a private fork. |

ACL: allow 22, UDP 500/4500, ESP and TCP 8444 from the peer. Log: `/var/log/day0.log`.
