"""WireGuard-Hub-Agent.

* erzeugt/persistiert den Hub-Schlüssel (/data/hub.key)
* registriert den Public-Key bei der Control-Plane
* synchronisiert alle 10 s die Peers (``wg syncconf``) aus ``/api/v1/internal/hub/peers``. Maßgeblich ist der
  Ist-Zustand des Interfaces (``wg show <iface> dump``), nicht die Datei ``/data/<iface>.conf``: Das Volume überlebt
  Neustarts, das Interface wird beim Start aber leer angelegt. Beim Start wird immer einmal synchronisiert.
* Healthcheck (``python agent.py --health``): unhealthy, wenn der letzte erfolgreiche Sync älter als 3 min ist oder die
  Peer-Anzahl im Interface nicht der API-Liste entspricht (Status in ``HEALTH_FILE``).
* meldet alle 30 s Handshake-/Traffic-Statistiken
* Firewall: Router dürfen nur Antworten an die Control-Plane schicken, kein Router->Router
  über den Hub, keine neuen Verbindungen vom Router ins Docker-Netz.

Nur Standardbibliothek, damit das Image klein bleibt.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request

API = os.environ.get("CONTROL_PLANE_URL", "http://api:8000").rstrip("/")
TOKEN = os.environ.get("HUB_TOKEN", "")
IFACE = os.environ.get("WG_INTERFACE", "wg0")
ENDPOINT = os.environ.get("WG_HUB_ENDPOINT", "")
DOCKER_NET = os.environ.get("DOCKER_NETWORK", "172.30.0.0/24")
KEY_FILE = "/data/hub.key"
CONF_FILE = f"/data/{IFACE}.conf"
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "10"))
STATS_INTERVAL = int(os.environ.get("STATS_INTERVAL", "30"))
HEALTH_FILE = os.environ.get("HEALTH_FILE", "/tmp/hub-health.json")
HEALTH_MAX_AGE_S = int(os.environ.get("HEALTH_MAX_AGE_S", "180"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s hub %(levelname)s %(message)s")
log = logging.getLogger()


def sh(*args: str, check: bool = True, inp: str | None = None) -> str:
    res = subprocess.run(args, capture_output=True, text=True, input=inp)
    if check and res.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {res.stderr.strip()}")
    return res.stdout.strip()


def api(method: str, path: str, body: object | None = None) -> object:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"X-Hub-Token": TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"null")


def ensure_key() -> tuple[str, str]:
    os.makedirs("/data", exist_ok=True)
    if not os.path.exists(KEY_FILE):
        priv = sh("wg", "genkey")
        with open(KEY_FILE, "w") as f:
            f.write(priv)
        os.chmod(KEY_FILE, 0o600)
    priv = open(KEY_FILE).read().strip()
    pub = sh("wg", "pubkey", inp=priv)
    return priv, pub


def ensure_interface(address: str, port: int) -> None:
    if sh("ip", "link", "show", IFACE, check=False) == "":
        try:
            sh("ip", "link", "add", IFACE, "type", "wireguard")
            log.info("Kernel-WireGuard aktiv")
        except RuntimeError:
            log.warning("Kein Kernel-Modul – nutze wireguard-go (Userspace)")
            sh("wireguard-go", IFACE)
    sh("wg", "set", IFACE, "private-key", KEY_FILE, "listen-port", str(port))
    sh("ip", "address", "replace", address, "dev", IFACE)
    sh("ip", "link", "set", "mtu", "1420", "up", "dev", IFACE)


def ensure_firewall() -> None:
    rules = [
        ("nat", "POSTROUTING", ["-s", DOCKER_NET, "-o", IFACE, "-j", "MASQUERADE"]),
        ("filter", "FORWARD", ["-i", IFACE, "-o", IFACE, "-j", "DROP"]),
        ("filter", "FORWARD", ["-i", IFACE, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"]),
        ("filter", "FORWARD", ["-i", IFACE, "-j", "DROP"]),
        ("filter", "FORWARD", ["-s", DOCKER_NET, "-o", IFACE, "-j", "ACCEPT"]),
    ]
    for table, chain, spec in rules:
        if subprocess.run(["iptables", "-t", table, "-C", chain, *spec], capture_output=True).returncode != 0:
            sh("iptables", "-t", table, "-A", chain, *spec)


def desired_state(peers: list[dict]) -> dict[str, frozenset[str]]:
    """Soll-Zustand aus der API: Public-Key -> AllowedIPs."""
    return {p["public_key"]: frozenset(x.strip() for x in str(p["allowed_ips"]).split(",") if x.strip()) for p in peers}


def interface_state() -> dict[str, frozenset[str]]:
    """Ist-Zustand des Interfaces (``wg show <iface> dump``, erste Zeile = Interface): Public-Key -> AllowedIPs."""
    out: dict[str, frozenset[str]] = {}
    for line in sh("wg", "show", IFACE, "dump").splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 4:
            allowed = "" if parts[3] == "(none)" else parts[3]
            out[parts[0]] = frozenset(x.strip() for x in allowed.split(",") if x.strip())
    return out


class SyncState:
    def __init__(self) -> None:
        self.last_ok = 0.0
        self.resync = False  # Peer-Anzahl wich nach dem letzten Sync ab -> beim nächsten Durchlauf erneut syncconf
        self.api_peers = 0
        self.iface_peers = 0


STATE = SyncState()


def write_health() -> None:
    try:
        with open(HEALTH_FILE, "w") as f:
            json.dump({"last_ok": STATE.last_ok, "api_peers": STATE.api_peers, "iface_peers": STATE.iface_peers}, f)
    except OSError as exc:
        log.warning("Health-Datei nicht schreibbar: %s", exc)


def sync_peers(priv_port: int, force: bool = False) -> bool:
    """Peers abgleichen. ``syncconf`` nur, wenn der Ist-Zustand vom Soll abweicht, ``force`` gesetzt ist oder der
    vorige Sync eine falsche Peer-Anzahl hinterließ. Rückgabe: ob ``syncconf`` lief."""
    peers: list[dict] = api("GET", "/api/v1/internal/hub/peers")  # type: ignore[assignment]
    lines = ["[Interface]", f"PrivateKey = {open(KEY_FILE).read().strip()}", f"ListenPort = {priv_port}", ""]
    for p in peers:
        lines += ["[Peer]", f"# device {p['device_id']}", f"PublicKey = {p['public_key']}", f"AllowedIPs = {p['allowed_ips']}", ""]
    content = "\n".join(lines)
    old = open(CONF_FILE).read() if os.path.exists(CONF_FILE) else ""
    if content != old:
        with open(CONF_FILE, "w") as f:
            f.write(content)
        os.chmod(CONF_FILE, 0o600)
    want = desired_state(peers)
    ran = False
    if force or STATE.resync or interface_state() != want:
        sh("wg", "syncconf", IFACE, CONF_FILE)
        ran = True
        log.info("Peers synchronisiert: %d%s", len(peers), " (Start)" if force else "")
    have = interface_state() if ran else want
    STATE.api_peers, STATE.iface_peers = len(want), len(have)
    if len(have) != len(want):
        log.warning("Peer-Anzahl im Interface (%d) weicht von der API-Liste (%d) ab – erneuter Sync im nächsten Durchlauf",
                    len(have), len(want))
        STATE.resync = True
    else:
        STATE.resync = False
        STATE.last_ok = time.time()
    write_health()
    return ran


def health(now: float | None = None, path: str | None = None) -> tuple[bool, str]:
    """Für den Docker-Healthcheck: (ok, Text)."""
    try:
        with open(path or HEALTH_FILE) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return False, "noch kein Sync"
    if st.get("api_peers") != st.get("iface_peers"):
        return False, f"Peers im Interface {st.get('iface_peers')} ≠ API {st.get('api_peers')}"
    if not st.get("last_ok"):
        return False, "noch kein erfolgreicher Sync"
    age = (now or time.time()) - float(st["last_ok"])
    if age > HEALTH_MAX_AGE_S:
        return False, f"letzter erfolgreicher Sync vor {int(age)} s"
    return True, f"ok, {st.get('iface_peers')} Peers"


def report_stats() -> None:
    out = sh("wg", "show", IFACE, "dump")
    stats = []
    for line in out.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 8:
            continue
        pub, _psk, endpoint, _allowed, hs, rx, tx, _ka = parts[:8]
        stats.append({"public_key": pub, "endpoint": None if endpoint == "(none)" else endpoint,
                      "latest_handshake": int(hs), "rx_bytes": int(rx), "tx_bytes": int(tx)})
    api("POST", "/api/v1/internal/hub/stats", stats)


def main() -> None:
    _priv, pub = ensure_key()
    log.info("Hub Public-Key: %s", pub)
    while True:
        try:
            cfg = api("PUT", "/api/v1/internal/hub/register", {"public_key": pub, "endpoint": ENDPOINT})
            break
        except (urllib.error.URLError, OSError) as exc:
            log.info("Control-Plane noch nicht erreichbar (%s) – retry", exc)
            time.sleep(3)
    port = int(cfg["listen_port"])  # type: ignore[index]
    ensure_interface(cfg["address"], port)  # type: ignore[index]
    ensure_firewall()
    first = True  # nach dem Start immer einmal syncconf: das Interface ist leer, die Datei im Volume evtl. unverändert
    last_stats = 0.0
    while True:
        try:
            sync_peers(port, force=first)
            first = False
            if time.time() - last_stats > STATS_INTERVAL:
                report_stats()
                last_stats = time.time()
        except Exception as exc:  # noqa: BLE001
            log.warning("Sync-Fehler: %s", exc)
        time.sleep(SYNC_INTERVAL)


if __name__ == "__main__":
    import sys

    if "--health" in sys.argv:
        ok, text = health()
        print(text)
        sys.exit(0 if ok else 1)
    main()
