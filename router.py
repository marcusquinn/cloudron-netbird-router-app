#!/usr/bin/env python3
"""NetBird routing peer for Cloudron VPN-protected apps.

`router.py sync` connects the NetBird daemon, then polls Cloudron's
`/vpn-access-config` app-bridge endpoint and keeps one NetBird network in step:
this peer as its masquerading router, and one domain resource per protected
FQDN (plus CIDR resources in Cloudron's "custom" tunnel mode).

`router.py serve` is the unprivileged status page.

Only resources this script created (identified by RESOURCE_MARKER in their
description) are ever changed or deleted. Access policies are left to the
NetBird admin.
"""

import html
import ipaddress
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NETBIRD = "/app/code/bin/netbird"
STATE_FILE = "/app/data/router-state.json"
STATUS_FILE = "/run/router/status.json"
RESOURCE_MARKER = "managed by cloudron-netbird-router"
SETUP_KEY_FILE = "/run/netbird/setup-key"

BRIDGE = os.environ.get("CLOUDRON_APP_BRIDGE_ORIGIN", "").rstrip("/")
MANAGEMENT_URL = os.environ.get("NETBIRD_MANAGEMENT_URL", "").rstrip("/")
API_URL = os.environ.get("NETBIRD_API_URL", "").rstrip("/")
API_TOKEN = os.environ.get("NETBIRD_API_TOKEN", "")
SETUP_KEY = os.environ.get("NETBIRD_SETUP_KEY", "")
INTERVAL = max(10, int(os.environ.get("SYNC_INTERVAL", "30") or "30"))
GROUP_NAME = os.environ.get("NETBIRD_RESOURCE_GROUP", "") or "Cloudron protected apps"
NETWORK_NAME = os.environ.get("NETBIRD_NETWORK_NAME", "")
HOSTNAME = os.environ.get("NETBIRD_HOSTNAME", "") or "cloudron-router-" + os.environ.get("CLOUDRON_APP_DOMAIN", "app").split(".")[0]
# App ID of a NetBird server app on the same Cloudron. Cloudron's host firewall drops
# container traffic to other apps' published TCP ports, so the router must reach the
# server container directly. The NetBird package listens on container port 33074.
SERVER_APP_ID = os.environ.get("NETBIRD_SERVER_APP_ID", "").strip()
SERVER_CONTAINER_PORT = int(os.environ.get("NETBIRD_SERVER_CONTAINER_PORT", "33074") or "33074")
HAIRPIN_TABLE = "cloudron_router_hairpin"
RECONNECT_AFTER = 4  # sync cycles without a management connection before `netbird up` again


def log(message):
    print(f"router: {message}", flush=True)


class ApiError(Exception):
    pass


# --- persistence -----------------------------------------------------------

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def save_json(path, data, mode=0o600):
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


# --- HTTP ------------------------------------------------------------------

def http_json(method, url, body=None, headers=None, timeout=20):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            try:
                return response.status, (json.loads(raw) if raw else None)
            except ValueError as error:
                # e.g. NETBIRD_API_URL pointing at a page that answers 200 with HTML
                raise ApiError(f"{method} {urllib.parse.urlsplit(url).path} returned non-JSON (HTTP {response.status})") from error
    except urllib.error.HTTPError as error:
        detail = error.read()[:300].decode(errors="replace")
        return error.code, {"error": detail}


def api(method, path, body=None):
    status, payload = http_json(method, f"{API_URL}/api{path}", body, {"Authorization": f"Token {API_TOKEN}"})
    if status >= 400:
        # Never echo request bodies or headers: they can carry the token.
        raise ApiError(f"{method} /api{path} -> HTTP {status}: {(payload or {}).get('error', '')[:200]}")
    return payload


# --- NetBird daemon --------------------------------------------------------

def netbird(*args, check=False):
    return subprocess.run([NETBIRD, *args], capture_output=True, text=True, check=check, timeout=120)


def resolve(host):
    v4, v6 = set(), set()
    for family, _, _, _, sockaddr in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP):
        (v4 if family == socket.AF_INET else v6).add(sockaddr[0])
    return sorted(v4), sorted(v6)


def apply_hairpin():
    """Send this container's management/signal/relay traffic straight to a same-Cloudron server.

    The NetBird server publishes one TCP port for all native traffic. From inside
    Cloudron that published port is unreachable, so DNAT it, in this container's own
    network namespace only, to the server container's fixed internal port.
    Returns a short description, or None when not configured.
    """
    if not SERVER_APP_ID:
        return None
    parsed = urllib.parse.urlsplit(MANAGEMENT_URL)
    port = parsed.port or 443
    public_v4, public_v6 = resolve(parsed.hostname)
    server_v4, server_v6 = resolve(SERVER_APP_ID)
    rules = []
    if public_v4 and server_v4:
        rules.append(f"ip daddr {{ {', '.join(public_v4)} }} tcp dport {port} dnat ip to {server_v4[0]}:{SERVER_CONTAINER_PORT}")
    if public_v6 and server_v6:
        rules.append(f"ip6 daddr {{ {', '.join(public_v6)} }} tcp dport {port} dnat ip6 to [{server_v6[0]}]:{SERVER_CONTAINER_PORT}")
    if not rules:
        raise RuntimeError(f"could not resolve {parsed.hostname} or NetBird server app {SERVER_APP_ID}")
    for address in public_v4 + public_v6:
        if ipaddress.ip_address(address).is_private:
            raise RuntimeError(f"{parsed.hostname} resolves to private address {address}; leave NETBIRD_SERVER_APP_ID unset")
    ruleset = (f"table inet {HAIRPIN_TABLE} {{}}\nflush table inet {HAIRPIN_TABLE}\n"
               f"table inet {HAIRPIN_TABLE} {{\n  chain output {{\n    type nat hook output priority -100; policy accept;\n"
               + "".join(f"    {rule}\n" for rule in rules) + "  }\n}\n")
    subprocess.run(["nft", "-f", "-"], input=ruleset, text=True, capture_output=True, check=True, timeout=30)
    return f"{parsed.hostname}:{port} -> {SERVER_APP_ID} container port {SERVER_CONTAINER_PORT}"


def wait_for_daemon():
    while netbird("status", "--check", "live").returncode != 0:
        time.sleep(2)


def connect(state):
    """Run `netbird up`; the setup key is used only until the first successful login.

    If management forgets the peer later (deleted, or login expired) the daemon reports
    NeedsLogin. Then only a setup key helps: `netbird up` without one would wait for an
    interactive SSO login that never comes.
    """
    args = ["up", "--management-url", MANAGEMENT_URL, "--hostname", HOSTNAME,
            "--disable-client-routes", "--enable-rosenpass", "--rosenpass-permissive"]
    needs_login = "NeedsLogin" in netbird("status").stdout
    if needs_login and state.get("registered"):
        state["registered"] = False
        save_json(STATE_FILE, state)
    use_key = bool(SETUP_KEY) and not state.get("registered")
    if needs_login and not use_key:
        raise RuntimeError("NetBird peer needs a new login (deleted or expired): set a new one-off NETBIRD_SETUP_KEY")
    if use_key:
        with open(os.open(SETUP_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
            handle.write(SETUP_KEY)
        args += ["--setup-key-file", SETUP_KEY_FILE]
    try:
        result = netbird(*args)
    finally:
        if use_key and os.path.exists(SETUP_KEY_FILE):
            os.unlink(SETUP_KEY_FILE)
    if result.returncode != 0:
        # netbird up does not print the setup key; keep only the last lines.
        message = " | ".join((result.stderr or result.stdout).strip().splitlines()[-3:])
        if use_key:
            message += " (a used one-off NETBIRD_SETUP_KEY must be replaced)"
        raise RuntimeError("netbird up failed: " + message)
    state["registered"] = True
    save_json(STATE_FILE, state)


def own_netbird_ip():
    result = netbird("status", "--json")
    if result.returncode != 0:
        return None
    try:
        status = json.loads(result.stdout)
    except ValueError:
        return None
    if status.get("management", {}).get("connected") is not True:
        return None
    return (status.get("netbirdIp") or "").split("/")[0] or None


# --- NetBird API reconciliation -------------------------------------------

def find_peer_id(netbird_ip):
    for peer in api("GET", "/peers") or []:
        if peer.get("ip") == netbird_ip:
            return peer["id"]
    raise ApiError(f"no NetBird peer has IP {netbird_ip}; check that the API token belongs to the same account")


def ensure_group(state):
    groups = api("GET", "/groups") or []
    for group in groups:  # saved ID first, so a rename or a same-named group can't move resources
        if group.get("id") == state.get("group_id"):
            return group["id"]
    for group in groups:
        if group.get("name") == GROUP_NAME:
            return group["id"]
    created = api("POST", "/groups", {"name": GROUP_NAME})
    log(f"created NetBird group '{GROUP_NAME}'")
    return created["id"]


def ensure_network(state, name):
    networks = api("GET", "/networks") or []
    for network in networks:
        if network.get("id") == state.get("network_id"):
            return network["id"]
    for network in networks:
        if network.get("name") == name:
            return network["id"]
    created = api("POST", "/networks", {"name": name, "description": "Cloudron apps marked 'VPN access only'. " + RESOURCE_MARKER})
    log(f"created NetBird network '{name}'")
    return created["id"]


def ensure_router(network_id, peer_id):
    routers = api("GET", f"/networks/{network_id}/routers") or []
    warnings = []
    mine = None
    for router in routers:
        if router.get("peer") == peer_id:
            mine = router
        else:
            warnings.append("another routing peer serves this network; Cloudron only trusts traffic from this app")
    desired = {"peer": peer_id, "metric": 9999, "masquerade": True, "enabled": True}
    if mine is None:
        api("POST", f"/networks/{network_id}/routers", desired)
        log("added this peer as the network's routing peer")
    elif not mine.get("masquerade") or not mine.get("enabled"):
        # Without masquerade, traffic would reach Cloudron from mesh IPs and be denied.
        api("PUT", f"/networks/{network_id}/routers/{mine['id']}", desired)
        log("re-enabled masquerade on this peer's router")
    return warnings


def desired_addresses(config):
    addresses = {}
    for app in config.get("apps") or []:
        fqdn = (app.get("fqdn") or "").strip().lower()
        if fqdn:
            addresses[fqdn] = fqdn
    routing = config.get("tunnelRouting") or {}
    if routing.get("mode") == "custom":
        for cidr in routing.get("customCidrs") or []:
            addresses[cidr] = f"Cloudron range {cidr}"
    return addresses


def reconcile_resources(network_id, group_id, desired):
    resources = api("GET", f"/networks/{network_id}/resources") or []
    managed = {r["address"]: r for r in resources if r.get("description") == RESOURCE_MARKER}
    changes = []
    for address, name in sorted(desired.items()):
        body = {"name": name, "description": RESOURCE_MARKER, "address": address, "enabled": True, "groups": [group_id]}
        current = managed.get(address)
        if current is None:
            api("POST", f"/networks/{network_id}/resources", body)
            changes.append(f"added {address}")
            continue
        group_ids = sorted(g.get("id") for g in current.get("groups") or [])
        if not current.get("enabled") or group_ids != [group_id]:
            api("PUT", f"/networks/{network_id}/resources/{current['id']}", body)
            changes.append(f"updated {address}")
    for address, resource in sorted(managed.items()):
        if address not in desired:
            api("DELETE", f"/networks/{network_id}/resources/{resource['id']}")
            changes.append(f"removed {address}")
    return changes


def sync_once(state, status):
    code, config = http_json("GET", f"{BRIDGE}/vpn-access-config", timeout=10)
    if code != 200 or not isinstance(config, dict):
        raise RuntimeError(f"Cloudron app bridge returned HTTP {code}; nothing changed")

    status["dashboardDomain"] = config.get("dashboardDomain") or ""
    routing = config.get("tunnelRouting")
    if not routing:
        # Not the selected VPN app. Leave NetBird untouched: deselection must not delete
        # resources an admin may re-enable, and Cloudron already stops trusting us.
        status.update(selected=False, phase="waiting", message="Not selected as the VPN app in Cloudron (Network > VPN Access).")
        return

    status.update(selected=True, tunnelMode=routing.get("mode"))
    netbird_ip = own_netbird_ip()
    if not netbird_ip:
        status.update(phase="connecting", message="NetBird daemon is not connected to management.")
        status["offlineCycles"] = status.get("offlineCycles", 0) + 1
        if status["offlineCycles"] >= RECONNECT_AFTER:
            status["offlineCycles"] = 0
            connect(state)  # raises with the reason, which the sync loop records
        return
    status.pop("offlineCycles", None)
    status["netbirdIp"] = netbird_ip

    desired = desired_addresses(config)
    name = NETWORK_NAME or f"Cloudron {status['dashboardDomain'] or 'apps'}"
    peer_id = state.get("peer_id") if state.get("peer_ip") == netbird_ip else None
    peer_id = peer_id or find_peer_id(netbird_ip)
    group_id = ensure_group(state)
    network_id = ensure_network(state, name)
    warnings = ensure_router(network_id, peer_id)
    changes = reconcile_resources(network_id, group_id, desired)

    if routing.get("mode") == "full":
        warnings.append("Cloudron tunnel mode is 'full'; exit-node routing is not automated, configure it in NetBird if wanted")
    if (state.get("peer_id"), state.get("group_id"), state.get("network_id")) != (peer_id, group_id, network_id):
        state.update(peer_id=peer_id, peer_ip=netbird_ip, group_id=group_id, network_id=network_id)
        save_json(STATE_FILE, state)
    for change in changes:
        log(change)
    status.update(phase="synced", message="In sync.", resources=sorted(desired), warnings=warnings,
                  network=name, group=GROUP_NAME, lastChange=time.time() if changes else status.get("lastChange"))


def run_sync():
    missing = [name for name, value in (("CLOUDRON_APP_BRIDGE_ORIGIN", BRIDGE), ("NETBIRD_MANAGEMENT_URL", MANAGEMENT_URL),
                                        ("NETBIRD_API_URL", API_URL), ("NETBIRD_API_TOKEN", API_TOKEN)) if not value]
    status = {"phase": "starting", "selected": None, "resources": [], "warnings": []}
    state = load_json(STATE_FILE, {})
    if missing:
        status.update(phase="unconfigured", message="Set " + ", ".join(missing) + " with `cloudron env set` (see README).")
        save_json(STATUS_FILE, status, 0o644)
        log(status["message"])
        while True:
            time.sleep(3600)

    wait_for_daemon()
    while True:
        try:
            hairpin = apply_hairpin()
            if hairpin:
                status["sameCloudronServer"] = hairpin
                log(f"same-Cloudron NetBird server: {hairpin}")
            connect(state)
            break
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            status.update(phase="connecting", message=str(error))
            save_json(STATUS_FILE, status, 0o644)
            log(str(error))
            time.sleep(30)

    while True:
        try:
            # Container IPs survive restarts but not every restore; keep the DNAT current.
            if SERVER_APP_ID:
                status["sameCloudronServer"] = apply_hairpin()
            sync_once(state, status)
            status.pop("error", None)
        except (ApiError, RuntimeError, urllib.error.URLError, OSError, subprocess.SubprocessError,
                KeyError, TypeError, AttributeError) as error:  # last three: unexpected API response shapes
            status.update(phase="error", error=f"{type(error).__name__}: {error}")
            log(f"sync failed: {error}")
        status["lastSync"] = time.time()
        save_json(STATUS_FILE, status, 0o644)
        time.sleep(INTERVAL)


# --- status page -----------------------------------------------------------

class StatusHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send(self, code, body, content_type):
        payload = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        status = load_json(STATUS_FILE, {"phase": "starting"})
        if self.path == "/healthz":
            return self._send(200, "ok\n", "text/plain")
        if self.path == "/status.json":
            return self._send(200, json.dumps(status, indent=2), "application/json")
        if self.path != "/":
            return self._send(404, "not found\n", "text/plain")
        rows = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(json.dumps(v) if not isinstance(v, str) else v)}</td></tr>"
                       for k, v in sorted(status.items()))
        page = ("<!doctype html><meta charset=utf-8><title>NetBird Router</title>"
                "<style>body{font-family:system-ui;margin:2rem}th{text-align:left;padding-right:1rem;vertical-align:top}</style>"
                f"<h1>NetBird Router</h1><table>{rows}</table>")
        return self._send(200, page, "text/html; charset=utf-8")


def run_serve():
    bind = os.environ.get("STATUS_BIND", "127.0.0.1")
    ThreadingHTTPServer((bind, 8000), StatusHandler).serve_forever()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("sync", "serve"):
        sys.exit("usage: router.py sync|serve")
    run_sync() if sys.argv[1] == "sync" else run_serve()
