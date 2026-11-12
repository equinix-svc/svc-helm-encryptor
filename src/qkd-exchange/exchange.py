#!/usr/bin/env python3
"""qkd-exchange - fetch keys from an ETSI GS QKD 014 KME and hand them to IPsec, MACsec and SSH consumers.

  exchange.py run  -c /etc/qkd/config.json     the appliance exchange (KME client + peer API + appliers)
  exchange.py sim  --port 8443                 a throw-away KME simulator for tests (NOT secure, in-memory)

ETSI 014 roles: the *master* SAE calls enc_keys(slave_SAE_ID) and gets {key_ID, key}; it tells the slave SAE the
key_ID in-band (here: the peer API); the *slave* SAE calls dec_keys(master_SAE_ID, key_ID). Keys are never logged.
Rotation is two-phase so both ends switch together: stage (slave fetches and holds) -> commit (both apply).
Stdlib only, plus `cryptography` for the ssh consumer.
"""
import argparse, base64, hashlib, hmac, json, logging, os, re, socket, ssl, sys, tempfile, threading, time, uuid
import urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("qkd-exchange")


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


# ---------------------------------------------------------------- exchange
class Exchange:
    def __init__(self, cfg):
        self.cfg = cfg
        self.kme = KME(cfg["kme"])
        self.cons = {c["name"]: c for c in cfg["consumers"]}
        self.staged = {}   # (name, seq) -> (key, key_id)
        self.state = {n: {"seq": 0, "last_ok": None, "last_error": None} for n in self.cons}
        self.lock = threading.Lock()
        self.wake = {n: threading.Event() for n in self.cons}
        self.kme_ms = None                       # TCP connect time to this site's KME, refreshed in the background

    def start_probes(self):
        """Measure the KME in the background so the status calls never wait for it."""
        u = urllib.parse.urlparse(self.cfg["kme"]["url"])
        host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)

        def loop():
            while True:
                self.kme_ms = tcp_latency_ms(host, port)
                time.sleep(5)
        threading.Thread(target=loop, daemon=True).start()

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

    def peer_get(self, path, timeout=3):
        req = urllib.request.Request(self.cfg["peer"]["url"].rstrip("/") + path)
        with urllib.request.urlopen(req, context=self.peer_ctx(), timeout=timeout) as r:
            return json.load(r)

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
            self.wake[c["name"]].wait(wait)       # "Rotate now" (console) sets the event to skip the wait
            self.wake[c["name"]].clear()

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
        return {"sae_id": self.cfg["sae_id"], "kme": self.cfg["kme"]["url"], "kme_ms": self.kme_ms, "consumers": self.state}


def make_server(addr, port, handler, what):
    """Listen on addr:port. If the address does not exist yet (an interface still coming up), keep trying instead of
    stopping the whole process: the other services of the appliance keep running meanwhile."""
    while True:
        try:
            return ThreadingHTTPServer((addr, port), handler)
        except OSError as e:
            log.error("%s: cannot listen on %s:%s (%s); retrying in 15 s", what, addr, port, e)
            time.sleep(15)


def serve_peer(ex):
    cfg = ex.cfg["peer"]

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def do_GET(self):
            self._send(200, ex.status() if self.path == "/v1/status" else {"ok": True})

        def do_POST(self):
            try:
                b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                {"/v1/stage": ex.on_stage, "/v1/commit": ex.on_commit}[self.path](b)
                self._send(200, {"ok": True})
            except Exception as e:  # noqa: BLE001
                log.error("peer %s failed: %s", self.path, e)
                self._send(400, {"error": str(e)})

    srv = make_server(cfg.get("bind", "0.0.0.0"), cfg.get("listen", 8444), H, "peer API")
    if cfg.get("cert"):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cfg["cert"], cfg["key"])
        ctx.load_verify_locations(cfg["ca"])
        ctx.verify_mode = ssl.CERT_REQUIRED          # mutual TLS: only the peer appliance may stage/commit keys
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.serve_forever()


# ---------------------------------------------------------------- console (master only): status API + web page
def read_state(path, max_age=40):
    try:
        if time.time() - os.path.getmtime(path) > max_age:
            return None
        return open(path).read()
    except OSError:
        return None


def parse_ipsec(txt):
    """swanctl --list-sas text (written by the strongswan container) -> {state, age_s, children}."""
    if txt is None:
        return {"state": "unknown"}
    if "ESTABLISHED" not in txt:
        return {"state": "down", "children": 0}
    m = re.search(r"established (\d+)s ago", txt)
    return {"state": "established", "age_s": int(m.group(1)) if m else None, "children": txt.count("INSTALLED")}


def parse_macsec(txt):
    if txt is None:
        return {"state": "unknown"}
    if not txt.strip():
        return {"state": "idle"}
    return {"state": "up" if " UP" in txt or "UP " in txt else "down"}


def tcp_latency_ms(host, port, n=3):
    """Median TCP connect time to the peer API: a latency figure that needs no ping binary or root."""
    vals = []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=2):
                vals.append((time.perf_counter() - t0) * 1000)
        except OSError:
            pass
    return round(sorted(vals)[len(vals) // 2], 1) if vals else None


def probe(host, port, n=5):
    """Reachability of the slave side: TCP connects to the peer API port (loss and latency), then the API call itself."""
    ms = []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=2):
                ms.append((time.perf_counter() - t0) * 1000)
        except OSError:
            pass
        time.sleep(0.1)
    ms.sort()
    return {"sent": n, "ok": len(ms), "loss_pct": round(100 * (n - len(ms)) / n),
            "min": round(ms[0], 1) if ms else None, "median": round(ms[len(ms) // 2], 1) if ms else None, "max": round(ms[-1], 1) if ms else None}


def load_auth(path):
    """Console login: a file with one line  user:pbkdf2:<iterations>:<salt hex>:<hash hex>  (written by Day 0)."""
    try:
        user, kind, it, salt, h = open(path).read().strip().split(":")
        assert kind == "pbkdf2"
        return {"user": user, "iter": int(it), "salt": bytes.fromhex(salt), "hash": bytes.fromhex(h)}
    except (OSError, ValueError, AssertionError):
        return None


def check_login(header, creds):
    """Basic auth against the stored hash; constant-time comparisons; a failure costs one second."""
    ok = False
    try:
        scheme, _, b64 = (header or "").partition(" ")
        user, _, pw = base64.b64decode(b64).decode().partition(":")
        if scheme.lower() == "basic" and creds:
            h = hashlib.pbkdf2_hmac("sha256", pw.encode(), creds["salt"], creds["iter"])
            ok = hmac.compare_digest(h, creds["hash"]) & hmac.compare_digest(user.encode(), creds["user"].encode())
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        time.sleep(1)
    return ok


class Console:
    def __init__(self, ex):
        self.ex, self.cfg = ex, ex.cfg.get("console", {})
        self.persist = self.cfg.get("persist", "/persist/console.json")
        self.web = self.cfg.get("web", "/app/console")
        self.state = {"linked": False, "fabric": ""}
        try:
            self.state.update(json.load(open(self.persist)))
        except (OSError, ValueError):
            pass
        self.hosts = {"localhost", "127.0.0.1", "::1"} | set(self.cfg.get("hosts", []))
        self.auth_file = self.cfg.get("auth_file")          # set: every request needs the login (fail closed if the file is missing)
        self.creds = load_auth(self.auth_file) if self.auth_file else None
        self._lat = (0, None)
        u = urllib.parse.urlparse(ex.cfg["peer"]["url"])
        self.peer_host, self.peer_port = u.hostname, u.port or 8444

    def save(self):
        atomic_write(self.persist, json.dumps(self.state), 0o600)

    def latency(self):
        return self._lat[1]

    def poll_latency(self):
        """Peer API latency, measured in the background: the status call returns at once even when the peer is down."""
        while True:
            self._lat = (time.time(), tcp_latency_ms(self.peer_host, self.peer_port))
            time.sleep(5)

    def status(self):
        ex = self.ex
        mine = [dict(name=n, type=c["type"], interval=c.get("interval", 3600), **ex.state[n]) for n, c in ex.cons.items()]
        out = {"now": time.time(), "sae_id": ex.cfg["sae_id"], "kme": ex.cfg["kme"]["url"], "peer_addr": self.peer_host,
               "peer_sae": next(iter(ex.cons.values()))["peer_sae"], "consumers": mine,
               "ipsec": parse_ipsec(read_state(self.cfg.get("ipsec_file", "/state/ipsec.txt"))),
               "macsec": parse_macsec(read_state(self.cfg.get("macsec_file", "/state/macsec.txt"))),
               "latency_ms": self.latency(), "kme_ms": ex.kme_ms, "link": dict(self.state), "peer": None}
        if self.state["linked"]:
            try:
                out["peer"] = ex.peer_get("/v1/status")
            except Exception as e:  # noqa: BLE001
                out["peer"] = {"error": "%s: %s" % (type(e).__name__, e)}
        return out

    def link(self, body):
        """Link the slave: the peer must answer on its API. The Fabric connection is only a label (no Fabric credentials here)."""
        fabric = str(body.get("fabric", ""))[:120]
        try:
            peer = self.ex.peer_get("/v1/status")
        except Exception as e:  # noqa: BLE001
            return 502, {"error": "peer %s:%s does not answer (%s)" % (self.peer_host, self.peer_port, e)}
        self.state.update(linked=True, fabric=fabric); self.save()
        return 200, {"ok": True, "peer": peer}

    def test(self, body):
        """Test button: TCP reachability and latency to the slave, then its peer API (certificates included in ADVANCED)."""
        out = {"address": "%s:%s" % (self.peer_host, self.peer_port), "tcp": probe(self.peer_host, self.peer_port), "peer_api": None}
        if out["tcp"]["ok"]:
            t0 = time.perf_counter()
            try:
                p = self.ex.peer_get("/v1/status")
                out["peer_api"] = {"ok": True, "sae_id": p.get("sae_id"), "ms": round((time.perf_counter() - t0) * 1000)}
            except Exception as e:  # noqa: BLE001
                out["peer_api"] = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        out["ok"] = bool(out["tcp"]["ok"]) and bool(out["peer_api"] and out["peer_api"]["ok"])
        return 200, out

    def unlink(self):
        self.state.update(linked=False); self.save()
        return 200, {"ok": True}

    def rotate_now(self):
        for n, c in self.ex.cons.items():
            if c["role"] == "master":
                self.ex.wake[n].set()
        return 200, {"ok": True}


def serve_console(con):
    cfg = con.cfg

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            b = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(b)

        def _guard(self):                     # Host guard (DNS rebinding), then the login when one is configured
            if urllib.parse.urlsplit("//" + self.headers.get("Host", "")).hostname not in con.hosts:
                self._send(403, {"error": "bad host"}); return False
            if con.auth_file and not check_login(self.headers.get("Authorization"), con.creds):
                b = b'{"error": "login required"}'
                self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="QKD Encryptor"')
                self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
                self.end_headers(); self.wfile.write(b); return False
            return True

        def do_GET(self):
            if not self._guard():
                return
            if self.path == "/api/status":
                return self._send(200, con.status())
            files = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"), "/equinix-logo.svg": ("equinix-logo.svg", "image/svg+xml")}
            if self.path in files:
                name, ctype = files[self.path]
                try:
                    return self._send(200, open(os.path.join(con.web, name), "rb").read(), ctype)
                except OSError:
                    pass
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._guard():
                return
            if self.headers.get("X-Console") != "1":
                return self._send(403, {"error": "missing X-Console header"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                fn = {"/api/link": lambda: con.link(body), "/api/unlink": con.unlink, "/api/test": lambda: con.test(body), "/api/rotate": con.rotate_now}.get(self.path)
                if not fn:
                    return self._send(404, {"error": "not found"})
                code, out = fn()
                self._send(code, out)
            except Exception as e:  # noqa: BLE001
                self._send(400, {"error": str(e)})

    srv = make_server(cfg.get("bind", "127.0.0.1"), cfg.get("port", 8080), H, "console")
    log.info("console on http://%s:%s (master only)", cfg.get("bind", "127.0.0.1"), cfg.get("port", 8080))
    srv.serve_forever()


def wait_for_files(cfg):
    """With https the KME/peer certificates are loaded after boot (Secret kme-tls): wait until they exist."""
    paths = [v for sec in ("kme", "peer") for k, v in cfg[sec].items() if k in ("ca", "cert", "key") and v]
    while not all(os.path.exists(p) for p in paths):
        log.warning("waiting for KME certificates: %s (see README: load them after boot)",
                    ", ".join(p for p in paths if not os.path.exists(p)))
        time.sleep(10)


def serve_health(port=8445):
    """Liveness endpoint on the loopback only: 200 while the process runs. The peer API and the console listen on addresses
    that may not exist yet (LOCAL_ADDR), so the probe must not depend on them."""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a):
            pass
    make_server("127.0.0.1", port, H, "health").serve_forever()


def run(path):
    cfg = json.load(open(path))
    wait_for_files(cfg)
    ex = Exchange(cfg)
    ex.start_probes()
    threading.Thread(target=serve_health, daemon=True).start()
    threading.Thread(target=serve_peer, args=(ex,), daemon=True).start()
    for c in cfg["consumers"]:
        if c["role"] == "master":
            threading.Thread(target=ex.master_loop, args=(c,), daemon=True).start()
    if cfg.get("console") and any(c["role"] == "master" for c in cfg["consumers"]):
        con = Console(ex)
        threading.Thread(target=con.poll_latency, daemon=True).start()
        threading.Thread(target=serve_console, args=(con,), daemon=True).start()
    while True:
        time.sleep(3600)


# ---------------------------------------------------------------- KME simulator (tests only)
def sim(port, bind="0.0.0.0"):
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

    log.warning("KME simulator on %s:%d - keys are random and in memory, NOT QKD", bind, port)
    make_server(bind, port, H, "KME simulator").serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("-c", "--config", default="/etc/qkd/config.json")
    s = sub.add_parser("sim"); s.add_argument("--port", type=int, default=8443); s.add_argument("--bind", default="0.0.0.0")
    a = ap.parse_args()
    run(a.config) if a.cmd == "run" else sim(a.port, a.bind)
