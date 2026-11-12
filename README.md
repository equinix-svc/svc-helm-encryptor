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
| 4. Parameters | Five required for BASIC, plus `FQDN` and `ADMIN_EMAIL` on the master, see below. Full list: [Day 0 templates](#3-day-0-templates). Certificates for a real KME are loaded after boot: [section 4](#4-load-kme-certificates-after-boot). |
| 5. Check | On the node: `curl -fsSL https://raw.githubusercontent.com/equinix-svc/svc-helm-encryptor/main/scripts/check-install.sh \| sudo bash`. It is read-only, prints PASS/FAIL and never prints secrets. |
| Login banner | Shown on each interactive SSH login: role, peer, KME, live status (pods, IPsec SA, last key), the JSON status endpoints (peer API, KME simulator on the BASIC master) and tips. Installed by Day 0 from `scripts/motd.sh` into `/etc/profile.d/`; never shows a secret. On an existing node: `sudo curl -fsSL https://raw.githubusercontent.com/equinix-svc/svc-helm-encryptor/main/scripts/motd.sh -o /etc/profile.d/qkd-encryptor-motd.sh` |

## 3. Day 0 templates

Two templates, same structure. Both do what the k3s-node image's own Day 0 does, then on a `single` node or the first `server` create ConfigMap `site` and Secret `site-secrets` in namespace `encryptor` and download `encryptor.yaml` (and `dev-kme-sim.yaml` on the BASIC master) into the k3s manifests directory. Render locally with `DAY0_FILE=<file> scripts/render-day0.sh`. Log: `/var/log/day0.log`.

| Template | Use | Required parameters |
|---|---|---|
| [`day0/day0-encryptor-basic.yaml`](day0/day0-encryptor-basic.yaml) | **BASIC** (lab): bring up a pair quickly with the KME simulator | 5, plus 2 on the master, the rest optional |
| [`day0/day0-encryptor-advanced.yaml`](day0/day0-encryptor-advanced.yaml) | **ADVANCED**: real KME, per-site values | 8, plus 2 on the master, the rest optional |

Equinix predefined (both): `hostname` `userName` `publicKey` `adminPwd` `ipAddressWithCidr` `gateway`. Declare every other variable as a custom parameter in the portal.

### Parameters

The **BASIC** and **ADVANCED** columns show whether the parameter is required in that template: `yes`, `no`, or `-` (not used). **Default** is what the template uses when a parameter that is not required is left empty.

| Parameter | BASIC | ADVANCED | Default | Example (paste in the portal) | Meaning |
|---|---|---|---|---|---|
| `QKD_ROLE` | yes | yes | - | `master` | `master` (calls `enc_keys`, initiates IPsec) or `slave` (calls `dec_keys`). In BASIC, the master is SAE-A and also runs the KME simulator; the slave is SAE-B. |
| `PEER_ADDR` | yes | yes | - | `172.16.0.12` | The other appliance's address, used for IKE and the peer API on 8444. Any address reachable from this appliance works, a private one included. With mutual TLS (ADVANCED) it must match a name or IP in the peer certificate. |
| `SAE_ID` | - | yes | - | `SAE-A` | This appliance's SAE ID on its KME. BASIC fixes it to `SAE-A` (master) or `SAE-B` (slave). |
| `PEER_SAE_ID` | - | yes | - | `SAE-B` | The remote appliance's SAE ID. BASIC fixes it to the opposite of the local one. |
| `KME_URL` | - | yes | - | `https://172.16.1.5:8443` | ETSI GS QKD 014 address of this site's KME, the QKD device (not the other encryptor). Any subnet reachable from the appliance works. `https://` means mutual TLS: the certificates are loaded after boot (section 4), and the host in the URL (name or IP) must be in the KME certificate. BASIC sets it itself: `http://127.0.0.1:8443` on the master and `http://<PEER_ADDR>:8443` on the slave. |
| `IKE_PSK` | yes | yes | - | `4f9c2a7e1b8d3c6a5e0f9b2d7c4a1e83` | IKEv2 secret, identical on both ends, never stored in the repo. The IPsec tunnel is always configured, so both templates refuse to start without it. Generate with `openssl rand -hex 16`. |
| `REGISTRY_USER` | yes | yes | - | `my-github-user` | GitHub login used to pull the private images from ghcr.io. Day 0 stops the encryptor install without it. |
| `REGISTRY_TOKEN` | yes | yes | - | `ghp_AbCdEf1234567890AbCdEf1234567890AbCd` | Classic personal access token with only `read:packages`. Written to `/etc/rancher/k3s/registries.yaml` (root only) before k3s starts. Expires: set a reminder to rotate it. |
| `FQDN` | yes | yes | - | `fred-enc-par01.example.net` | Master only (ignored on a slave). Public name of the console, served at `https://<FQDN>` with a Let's Encrypt certificate. The master's public IP is only known after creation, so create the DNS record afterwards: the node waits (checks every 30 seconds) until `<FQDN>` resolves to it, then enables the certificate, so the first Let's Encrypt attempt succeeds. The login banner shows the IP to use and what the name resolves to now (`not found` or `WRONG` when it points elsewhere, for example after a rebuild with a new IP). Keep port 80 reachable from the internet. |
| `ADMIN_EMAIL` | yes | yes | - | `xxxx@eu.equinix.com` | Master only. Login name of the console, and contact address for the Let's Encrypt certificate: it must be a real address. |
| `LOCAL_ADDR` | no | no | address of the default route | `172.16.0.11` | This appliance's own IP towards the peer. It is the IKE source address and the only address the peer API (and, in BASIC, the KME simulator) listens on. Leave it empty and they listen on every address of the node, so restrict TCP 8443 and 8444 to the peer in the ACL. Set it when the peer is reached over a second interface or subnet. |
| `LINK_IFACE` | no | no | `ens4` | `ens4` | NIC that carries `LOCAL_ADDR` (portal interface2). Day 0 writes the address to a netplan file on it. |
| `LOCAL_TS` | no | no | BASIC: `10.1.0.0/24` on the master, `10.2.0.0/24` on the slave. ADVANCED: `LOCAL_ADDR` | `10.1.0.0/24` | Local protected subnet. In BASIC a simulated laptop lives at `.10` of it (see Console). Never `0.0.0.0/0` on a node you manage over its public address: it would send all the node's traffic, SSH included, into the tunnel. |
| `REMOTE_TS` | no | no | BASIC: the other side's subnet. ADVANCED: `PEER_ADDR` | `10.2.0.0/24` | Remote protected subnet. Same warning. |
| `MACSEC_IFACE` | no | no | empty (no MACsec) | `ens4` | Host NIC for MACsec. |
| `MACSEC_ADDR` | no | no | empty (no address) | `10.99.0.1/24` | CIDR on the macsec interface. |
| `K3S_ROLE` | no | no | `single` | `single` | `single`: standalone node. `server`: control-plane node. `agent`: k3s worker node that joins an existing cluster; the encryptor is not installed on it. |
| `K3S_SERVER_URL` | no | no | empty | `https://10.0.0.5:6443` | Agent only. URL used by an agent to join the k3s cluster. Empty for `single` or the first `server`. |
| `K3S_TOKEN` | no | no | empty | `K10abc123def456::server:789xyz` | Agent only. Secret used to join the k3s cluster. |
| `K3S_TLS_SAN` | no | no | empty | `172.16.0.11` | Extra name or IP in the k3s API server certificate, for `kubectl` or agents reaching port 6443 from outside. Not needed for local `sudo k3s kubectl`. |
| `ADMIN_PASSWORD` | no | no | generated | `ChangeMe-2026` | Master only. Console password. Written to `/etc/encryptor/credentials.env` (root only). |

BASIC uses plain HTTP for the KME and the peer API. ADVANCED does too when `KME_URL` is `http://`. Its simulator gives random keys, **not QKD**, with no authentication.

| Template | ACL |
|---|---|
| BASIC | allow 22, UDP 500/4500, ESP, and TCP 8443 and 8444 from the peer only; the master also needs 80 and 443 from anywhere |
| ADVANCED | allow 22, UDP 500/4500, ESP and TCP 8444 from the peer; the master also needs 80 and 443 from anywhere |

### Network between the appliances

A Network Edge device created with 5 interfaces has one Linux NIC per portal interface, in order from `ens3`. Day 0 configures `ens3` and, when `LOCAL_ADDR` is set, the link NIC (`LINK_IFACE`, default `ens4`) through a netplan file, so the address survives a reboot.

| Portal interface | NIC | Use |
|---|---|---|
| interface1 | `ens3` | management: SSH, Internet Access, the public console |
| interface2 | `ens4` | link to the other appliance: `LOCAL_ADDR` and `PEER_ADDR` (IKE, peer API, KME simulator) |
| interface3 | `ens5` | `MACSEC_IFACE`: a NIC dedicated to MACsec |
| interface4, interface5 | `ens6`, `ens7` | spare |

The portal lists each interface's MAC address (device details) and `ip -br link` shows it on the node, so the mapping can be checked.

1. In the portal, create a Layer 2 link between `interface2` of both devices (a device link in the same metro, or a Fabric connection).
2. Day 0 has already written `/etc/netplan/60-qkd-encryptor.yaml` with `LOCAL_ADDR/24` on `ens4`. The address shows up as soon as the link carries traffic; to set it by hand instead: `sudo ip link set ens4 up && sudo ip addr add 172.16.0.11/24 dev ens4`.
3. Check with `ping <PEER_ADDR>`, then `check-install.sh`. The console shows the IPsec link once the peer answers.

IPsec only encrypts traffic that matches `LOCAL_TS` and `REMOTE_TS`, and the strongSwan image leaves traffic inside a directly attached subnet (here `172.16.0.0/24`) outside the tunnel. Pinging `PEER_ADDR` therefore proves the link, not the encryption. Protect subnets behind the appliances, as the BASIC simulated laptops do.

Until `LOCAL_ADDR` exists on an interface, the peer API and the simulator retry every 15 seconds, the IPsec tunnel stays `CONNECTING`, and the Day 0 log carries a warning. With both addresses private, the key exchange does not use the public interface.

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

## 5. Console (master only)

The master serves a small web console: the appliance pair as two nodes joined by the IPsec link, with the key rotation of each consumer. It runs inside the `qkd-exchange` container and is published at `https://<FQDN>` through Traefik with a Let's Encrypt certificate. It has its own login (`ADMIN_EMAIL` and `ADMIN_PASSWORD`).

| Item | Detail |
|---|---|
| Open | `https://<FQDN>`, login `ADMIN_EMAIL` and the password in `/etc/encryptor/credentials.env` on the master (generated when `ADMIN_PASSWORD` is empty) |
| Setup | Day 0 runs `scripts/console-setup.sh` on the master: the login (a PBKDF2 hash in Secret `console-auth`), Traefik's Let's Encrypt resolver with a persistent certificate store, and a Service, EndpointSlice and Ingress that point Traefik at the console on the node address (the Pod uses the host network). The last two are staged and enabled by the `enc-dns-wait` service once `FQDN` resolves to the master, so until you create the DNS record the console is not routed |
| Master side | SAE, KME, peer SAE; consumers IPsec, MACsec, SSH with interval, sequence, last key age and state |
| Slave side | greyed until you click "Link slave". The peer address is read-only, taken from the portal parameter `PEER_ADDR`. Linking checks that the peer answers on its API, then shows the slave's SAE and KME |
| Fabric connection | a text label stored on the node (`/var/lib/encryptor/state/console.json`) and shown on the slave card. The service token and the virtual connection itself are created outside the node: it has no Fabric credentials and never reads the connection's real state |
| Test the slave side | button under the Fabric label, usable before linking: 5 TCP connects to the slave's peer API port (loss; min, median and max latency), then the peer API call itself (certificates included in ADVANCED). The result is green when both pass |
| Laptop simulators (BASIC) | Day 0 gives each appliance a dummy interface `laptop0` with a simulated laptop at `.10` of its protected subnet (`10.1.0.10` behind the master, `10.2.0.10` behind the slave), written as systemd-networkd files (the node's netplan has no dummy device). Each appliance runs an echo service on it and, every 5 seconds, sends a few exchanges from its laptop to the other one. Source and destination are inside the protected subnets, so they only get through the tunnel. The console draws both laptops above their appliances with the answer time in each direction, and the link box shows the ESP packet counters |
| KME links | each appliance is drawn with its QKD appliance (KME) and an ETSI 014 line showing the TCP connect time to it, red and dashed when it does not answer. The slave's figure comes from its own peer API status. When both use the same KME (BASIC: the master's simulator) it is drawn once; with two KMEs the QKD link between them is dotted and not monitored |
| Link line | IPsec state and uptime, number of child SAs, latency, age of the last key |
| Latency | median TCP connect time from the master to the peer API port (8444), measured every 5 seconds. It needs no ping tool or root |
| IPsec and MACsec state | the `strongswan` and `macsec` containers publish them to a shared in-memory volume every 5 to 10 seconds; older than 40 seconds counts as unknown |
| Rotate keys now | asks the master to fetch new keys for all consumers immediately; the slave follows through the normal stage and commit |
| Protection | login on every request, checked by the console itself (so its port is protected even if reached directly); Host header must be `FQDN` or localhost; every change needs an `X-Console: 1` header that a foreign web page cannot send; a wrong password costs one second. Keep TCP 8080 closed in the ACL: only Traefik (on the same node) needs it |
| Listening address | port 8080 on the node's pod-network gateway (the first address of its pod range, `10.42.0.1` on a single node), so only Traefik reaches it. Like the daihub stack, the node exposes 80 and 443 for the web interface and nothing else on its public address |
| Slave nodes | have no console and need no `FQDN`; their login banner shows the status |

## 6. What was tested

- The exchange service and simulator ran end to end with a master and a slave. IPsec and MACsec key files matched on both sides (only the MKA priority differs), and the SSH public key passed its authentication check and was installed.
- Two strongSwan containers established IKEv2 with the PPK. A wrong PPK failed with `AUTH_FAILED`.
- Two `macsec` containers joined by a veth pair established MKA with GCM-AES-256 from a config generated by qkd-exchange. Ping passed and the wire carried 802.1AE frames.
- The peer API ran over mutual TLS with generated certificates: `qkd-exchange` waited until the certificates existed, accepted the valid client, and refused a client with no certificate and one from another CA.
- The console ran against the real simulator with a master and a slave: status, linking with a peer check, Rotate now, persistence, the Host and header guards, and the page in a headless browser (linked and not linked), with simulated tunnel and interface state files.
- Two throwaway k3s nodes in Docker (v1.36.5, one master and one slave) ran the real manifest and the BASIC Day 0 logic: both Pods start with their three containers on the node's own address; the master's KME simulator serves keys; the master rotates and the slave applies; IKEv2 comes up with the quantum-key PPK and traffic crosses the ESP tunnel (packet counters match); with a virtual cable and `MACSEC_IFACE` set through the ConfigMap, MKA connects, `macsec0` is up with GCM-AES-256 and traffic crosses it; the console shows state, tests and links the slave, rotates keys on demand and refuses requests without its header or from another Host; it is published through the bundled Traefik at a test name: no login or a wrong password gives 401, the right login gives the page and the status, the Host and header guards hold, and the console's own port also asks for the login. The Let's Encrypt issuance itself was not tested (no real DNS).
- The three images build, the manifest parses, and Day 0 renders and passes a shell syntax check.

## 7. Findings and limits

- **MACsec on Equinix vNICs:** the virtual switch must pass EAPOL (`01:80:c2:00:00:03`) and EtherType `0x88e5` frames between the two appliances. This is untested. Use a dedicated NIC (`MACSEC_IFACE`), not the management one.
- **MACsec image:** 256-bit MACsec needs `wpa_supplicant` 2.11 (`macsec_csindex`). Debian trixie ships 2.10 and Alpine has no MACsec driver, so the image uses `debian:sid-slim`. Pin it by digest once validated.
- **MACsec rotation:** a new CAK starts a new MKA session, which can interrupt traffic briefly. The default interval is 24 hours.
- **PPK timing:** a new PPK only takes effect at the next IKE reauthentication, not on child-SA rekeys.
- **PPK ID:** it is fixed, so a brief key-switch window can fail an authentication. That is retried.
- **SSH:** QKD can't plug into the SSH transport itself. qkd-exchange only rotates key pairs, and you still need to wire them into `sshd` or a CA.
- **Public name:** Let's Encrypt issued the console certificate on a real node. Traefik requests it about a second after it starts, so its Deployment uses the `Recreate` strategy and its Service publishes not-ready addresses; otherwise the validation reaches the old pod or no pod and fails. `sudo cat /etc/encryptor/dns-wait.state` shows the state, and `check-install.sh` shows the certificate issuer.
- **Untested:** the netplan file that keeps the `ens4` address across reboots, a real KME (certificate and SAE ID mapping vary by vendor), the images pulled from ghcr.io on a real node, Equinix Network Edge devices, and the Fabric link between them (including whether the virtual switch passes MACsec frames).
- **IKE authentication:** it uses a shared secret (`IKE_PSK`) for now. Certificates would be better for production.
- **Day 0 size:** the templates are about 14 KB, under the portal limit of 20,000 characters. No certificate goes through Day 0.
- **Secrets in Day 0:** the Day 0 files hold no secret. `IKE_PSK` (and `K3S_TOKEN` for an agent, `REGISTRY_TOKEN`) still travel as portal parameters and end up in a Kubernetes Secret on the node, readable by root.
- **Images:** `encryptor.yaml` pulls `ghcr.io/equinix-svc/svc-helm-encryptor/{qkd-exchange,strongswan,macsec}:0.1.0`. Build them from `src/<name>/` and push them to that registry (or change the names in `encryptor.yaml.in`) before the first boot. A private registry needs a pull secret.

## 8. Build and validate

Edit `encryptor.yaml.in` and `src/`, then run `scripts/validate.sh`. Pushing a change under `src/` rebuilds and publishes the images (workflow `images`). It builds `encryptor.yaml` and the images and checks Day 0. Commit both the sources and the generated file.
