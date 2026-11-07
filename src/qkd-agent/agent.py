#!/usr/bin/env python3
"""qkd-agent - fetch keys from an ETSI GS QKD 014 KME and hand them to IPsec, MACsec and SSH consumers.

  agent.py run  -c /etc/qkd/config.json     the appliance agent (KME client + peer API + appliers)
  agent.py sim  --port 8443                 a throw-away KME simulator for tests (NOT secure, in-memory)

ETSI 014 roles: the *master* SAE calls enc_keys(slave_SAE_ID) and gets {key_ID, key}; it tells the slave SAE the
key_ID in-band (here: the peer API); the *slave* SAE calls dec_keys(master_SAE_ID, key_ID). Keys are never logged.
Rotation is two-phase so both ends switch together: stage (slave fetches and holds) -> commit (both apply).
Stdlib only, plus `cryptography` for the ssh consumer.
"""
import argparse, base64, hashlib, hmac, json, logging, os, ssl, sys, tempfile, threading, time, uuid
import urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("qkd-agent")


# ---------------------------------------------------------------- ETSI 014 client
class KME:
    def __init__(self, cfg):
        self.base = cfg["url"].rstrip("/")
        self.ctx = None
        if self.base.startswith("https"):
            self.ctx = ssl.create_default_context(cafile=cfg.get("ca"))
            if cfg.get("cert"):
                self.ctx.load_cert_chain(cfg["cert"], cfg["key"])
            if cfg.get("insecure"):
                self.ctx.check_hostname = False
                self.ctx.verify_mode = ssl.CERT_NONE

    def _get(self, path, **q):
        url = self.base + path + ("?" + urllib.parse.urlencode(q, doseq=True) if q else "")
        with urllib.request.urlopen(urllib.request.Request(url), context=self.ctx, timeout=15) as r:
            return json.load(r)

    def status(self, slave):
        return self._get("/api/v1/keys/%s/status" % slave)

    def enc_keys(self, slave, size=256, number=1):
        return self._get("/api/v1/keys/%s/enc_keys" % slave, number=number, size=size)["keys"]

    def dec_keys(self, master, key_ids):
        return self._get("/api/v1/keys/%s/dec_keys" % master, key_ID=key_ids)["keys"]


# ---------------------------------------------------------------- appliers (consumers of a key)
def atomic_write(path, data, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def apply_ipsec(c, key, key_id, extra):
    """RFC 8784 post-quantum preshared key for strongSwan. Takes effect on the next IKE_AUTH (reauth)."""
    ppk_id = "qkd-" + c["name"]
    conf = "secrets {\n  ppk-%s {\n    id = %s\n    secret = 0x%s\n  }\n}\n" % (c["name"], ppk_id, key.hex())
    atomic_write(os.path.join(c.get("dir", "/swanctl/conf.d"), "10-ppk-%s.conf" % c["name"]), conf)


def apply_macsec(c, key, key_id, extra):
    """MACsec: the QKD key is the MKA CAK (128 or 256 bit), the key_ID (a UUID, 16 bytes) is the CKN. wpa_supplicant config."""
    if len(key) not in (16, 32):
        raise ValueError("MACsec CAK must be 128 or 256 bits, got %d" % (8 * len(key)))
    ckn = key_id.replace("-", "")
    prio = 16 if c["role"] == "master" else 64      # lowest priority value becomes MKA key server
    conf = ("ctrl_interface=/run/wpa_supplicant\nap_scan=0\neapol_version=3\nnetwork={\n  key_mgmt=NONE\n  eapol_flags=0\n"
            "  macsec_policy=1\n  macsec_csindex=%d\n  mka_cak=%s\n  mka_ckn=%s\n  mka_priority=%d\n}\n"
            % (1 if len(key) == 32 else 0, key.hex(), ckn, prio))
    atomic_write(os.path.join(c.get("dir", "/macsec"), "macsec.conf"), conf)


def _ssh_pub(priv):
    from cryptography.hazmat.primitives import serialization as s
    return priv.public_key().public_bytes(s.Encoding.OpenSSH, s.PublicFormat.OpenSSH).decode()


def make_ssh_extra(c, key):
    """Master only: new Ed25519 pair whose seed mixes the QKD key with local randomness; pubkey is HMAC'd with the QKD key."""
    from cryptography.hazmat.primitives import serialization as s
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes
    seed = HKDF(hashes.SHA256(), 32, salt=os.urandom(32), info=b"qkd-ssh-seed").derive(key + os.urandom(32))
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    pem = priv.private_bytes(s.Encoding.PEM, s.PrivateFormat.OpenSSH, s.NoEncryption())
    pub = _ssh_pub(priv)
    d = os.path.join(c.get("dir", "/ssh"), c["name"])
    atomic_write(os.path.join(d, "id_ed25519"), pem)
    atomic_write(os.path.join(d, "id_ed25519.pub"), pub + "\n", 0o644)
    return {"pub": pub, "mac": hmac.new(key, pub.encode(), hashlib.sha256).hexdigest()}


def apply_ssh_slave(c, key, key_id, extra):
    pub = extra["pub"]
    if not hmac.compare_digest(extra["mac"], hmac.new(key, pub.encode(), hashlib.sha256).hexdigest()):
        raise ValueError("ssh public key failed QKD-key authentication")
    atomic_write(os.path.join(c.get("dir", "/ssh"), c["name"], "authorized_keys"), pub + "\n", 0o644)


APPLY = {"ipsec": apply_ipsec, "macsec": apply_macsec}


def apply(c, key, key_id, extra):
    if c["type"] == "ssh":
        if c["role"] == "slave":
            apply_ssh_slave(c, key, key_id, extra)
    else:
        APPLY[c["type"]](c, key, key_id, extra)


# ---------------------------------------------------------------- agent
class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        self.kme = KME(cfg["kme"])
        self.cons = {c["name"]: c for c in cfg["consumers"]}
        self.staged = {}   # (name, seq) -> (key, key_id)
        self.state = {n: {"seq": 0, "last_ok": None, "last_error": None} for n in self.cons}
        self.lock = threading.Lock()

    # master side
    def rotate(self, c):
        name, peer = c["name"], c["peer_sae"]
        seq = self.state[name]["seq"] + 1
        k = self.kme.enc_keys(peer, size=c.get("key_size", 256))[0]
        key, key_id = base64.b64decode(k["key"]), k["key_ID"]
        extra = make_ssh_extra(c, key) if c["type"] == "ssh" else {}
        self.peer_post("/v1/stage", {"consumer": name, "seq": seq, "key_id": key_id})
        self.peer_post("/v1/commit", {"consumer": name, "seq": seq, "extra": extra})
        apply(c, key, key_id, extra)
        self.state[name].update(seq=seq, last_ok=time.time(), last_error=None, key_id=key_id)
        log.info("%s: rotated seq=%d key_id=%s", name, seq, key_id)

    def peer_post(self, path, body):
        req = urllib.request.Request(self.cfg["peer"]["url"].rstrip("/") + path, json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, context=self.peer_ctx(), timeout=30) as r:
            if r.status != 200:
                raise RuntimeError("peer %s -> %s" % (path, r.status))

    def peer_ctx(self):
        p = self.cfg["peer"]
        ctx = ssl.create_default_context(cafile=p.get("ca"))
        if p.get("cert"):
            ctx.load_cert_chain(p["cert"], p["key"])
        return ctx

    def master_loop(self, c):
        while True:
            wait = c.get("interval", 3600)
            try:
                self.rotate(c)
            except Exception as e:  # noqa: BLE001 - keep the loop alive, retry sooner
                self.state[c["name"]]["last_error"] = "%s: %s" % (type(e).__name__, e)
                log.error("%s: rotation failed: %s", c["name"], e)
                wait = c.get("retry", 30)
            time.sleep(wait)

    # slave side (called by the peer API)
    def on_stage(self, b):
        c = self.cons[b["consumer"]]
        if c["role"] != "slave":
            raise ValueError("not a slave consumer")
        k = self.kme.dec_keys(c["peer_sae"], [b["key_id"]])[0]
        with self.lock:
            self.staged[(c["name"], b["seq"])] = (base64.b64decode(k["key"]), k["key_ID"])

    def on_commit(self, b):
        c = self.cons[b["consumer"]]
        with self.lock:
            key, key_id = self.staged.pop((c["name"], b["seq"]))
        apply(c, key, key_id, b.get("extra", {}))
        self.state[c["name"]].update(seq=b["seq"], last_ok=time.time(), last_error=None, key_id=key_id)
        log.info("%s: applied seq=%d key_id=%s", c["name"], b["seq"], key_id)

    def status(self):
        return {"sae_id": self.cfg["sae_id"], "consumers": self.state}


def serve_peer(agent):
    cfg = agent.cfg["peer"]

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def do_GET(self):
            self._send(200, agent.status() if self.path == "/v1/status" else {"ok": True})

        def do_POST(self):
            try:
                b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                {"/v1/stage": agent.on_stage, "/v1/commit": agent.on_commit}[self.path](b)
                self._send(200, {"ok": True})
            except Exception as e:  # noqa: BLE001
                log.error("peer %s failed: %s", self.path, e)
                self._send(400, {"error": str(e)})

    srv = ThreadingHTTPServer(("0.0.0.0", cfg.get("listen", 8444)), H)
    if cfg.get("cert"):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cfg["cert"], cfg["key"])
        ctx.load_verify_locations(cfg["ca"])
        ctx.verify_mode = ssl.CERT_REQUIRED          # mutual TLS: only the peer appliance may stage/commit keys
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.serve_forever()


def run(path):
    cfg = json.load(open(path))
    agent = Agent(cfg)
    threading.Thread(target=serve_peer, args=(agent,), daemon=True).start()
    for c in cfg["consumers"]:
        if c["role"] == "master":
            threading.Thread(target=agent.master_loop, args=(c,), daemon=True).start()
    while True:
        time.sleep(3600)


# ---------------------------------------------------------------- KME simulator (tests only)
def sim(port):
    store, lock = {}, threading.Lock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
            parts = u.path.strip("/").split("/")          # api v1 keys <sae> <op>
            out, code = {}, 200
            if len(parts) == 5 and parts[:3] == ["api", "v1", "keys"]:
                op = parts[4]
                if op == "status":
                    out = {"source_KME_ID": "SIM", "target_KME_ID": "SIM", "master_SAE_ID": "sim",
                           "slave_SAE_ID": parts[3], "key_size": 256, "stored_key_count": 1000,
                           "max_key_count": 1000, "max_key_per_request": 128, "max_key_size": 1024,
                           "min_key_size": 64, "max_SAE_ID_count": 0}
                elif op == "enc_keys":
                    n, size = int(q.get("number", ["1"])[0]), int(q.get("size", ["256"])[0])
                    keys = []
                    for _ in range(n):
                        kid, k = str(uuid.uuid4()), os.urandom(size // 8)
                        with lock:
                            store[kid] = k
                        keys.append({"key_ID": kid, "key": base64.b64encode(k).decode()})
                    out = {"keys": keys}
                elif op == "dec_keys":
                    keys = []
                    for kid in q.get("key_ID", []):
                        with lock:
                            k = store.pop(kid, None)      # one-time delivery, like a real KME
                        if k is None:
                            code, out = 400, {"message": "unknown key_ID " + kid}; break
                        keys.append({"key_ID": kid, "key": base64.b64encode(k).decode()})
                    else:
                        out = {"keys": keys}
                else:
                    code = 404
            else:
                code = 404
            body = json.dumps(out).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    log.warning("KME simulator on :%d - keys are random and in memory, NOT QKD", port)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("-c", "--config", default="/etc/qkd/config.json")
    s = sub.add_parser("sim"); s.add_argument("--port", type=int, default=8443)
    a = ap.parse_args()
    run(a.config) if a.cmd == "run" else sim(a.port)
