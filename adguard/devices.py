"""Who is who on the LAN, for the admin page (Task 20): IP, MAC, vendor, hostname, and how
much each device already uses this DNS - so the children's devices can be recognised and
given a nickname (the second column of child-policy/clients.conf).

Sources, none of them magic: the kernel neighbour table (IP->MAC for devices the server has
talked to recently, so it is incomplete), AdGuard Home's own runtime clients (reverse-DNS
name from the router), its 30-day statistics (query counts), and the IEEE OUI list from the
`ieee-data` package for the vendor. A MAC with the "locally administered" bit set is a
randomised or virtual address: it has no vendor, and it is exactly the kind that changes
between networks - which is why the policy keys children by IP (static lease) plus MAC.
"""
import ipaddress
import re
import subprocess
from pathlib import Path

LAN = ipaddress.ip_network("192.168.31.0/24")
OUI_FILE = Path("/usr/share/ieee-data/oui.txt")
_oui = None


def _load_oui():
    global _oui
    if _oui is None:
        _oui = {}
        try:
            for line in OUI_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.match(r"^([0-9A-F]{2})-([0-9A-F]{2})-([0-9A-F]{2})\s+\(hex\)\s+(.+)$", line)
                if m:
                    _oui["".join(m.groups()[:3])] = m.group(4).strip()
        except OSError:
            pass
    return _oui


def vendor(mac):
    """Returns (vendor name or None, randomised-or-virtual flag)."""
    mac = (mac or "").lower()
    if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
        return None, False
    if int(mac[:2], 16) & 0x02:
        return None, True
    return _load_oui().get(mac.replace(":", "")[:6].upper()), False


def neighbours():
    """IP -> MAC from the kernel neighbour table, LAN only."""
    out = {}
    try:
        text = subprocess.run(["ip", "-4", "neigh", "show"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    for line in text.splitlines():
        m = re.match(r"^(\S+) dev \S+ lladdr ([0-9a-f:]{17})", line)
        if m and ipaddress.ip_address(m.group(1)) in LAN:
            out[m.group(1)] = m.group(2)
    return out


def list_devices(api, kids):
    """kids: {ip: (nickname, mac_from_clients_conf)}. Children first, then by activity."""
    neigh = neighbours()
    try:
        auto = {a["ip"]: a for a in (api.call("GET", "/clients").get("auto_clients") or []) if a.get("ip")}
    except Exception:  # noqa: BLE001
        auto = {}
    try:
        stats = api.call("GET", "/stats")
        queries = {ip: n for d in stats.get("top_clients") or [] for ip, n in d.items()}
    except Exception:  # noqa: BLE001
        queries = {}
    ips = set(kids) | set(neigh) | {ip for ip in queries if _in_lan(ip)}
    devs = []
    for ip in ips:
        mac = neigh.get(ip) or (kids.get(ip, ("", ""))[1] or "").lower() or None
        vend, rnd = vendor(mac)
        host = (auto.get(ip) or {}).get("name") or ""
        devs.append({"ip": ip, "nick": kids[ip][0] if ip in kids else "", "kid": ip in kids,
                     "mac": mac, "vendor": vend, "random": rnd, "host": host,
                     "queries": queries.get(ip, 0)})
    devs.sort(key=lambda d: (not d["kid"], -d["queries"], ipaddress.ip_address(d["ip"])))
    return devs


def _in_lan(ip):
    try:
        return ipaddress.ip_address(ip) in LAN
    except ValueError:
        return False
