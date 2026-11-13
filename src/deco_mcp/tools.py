"""The MCP tools: thin, validated wrappers around ``tplink-deco-api``."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import Config

log = logging.getLogger("deco_mcp.tools")

REBOOT_PHRASE = "reboot the whole mesh"


class ToolError(Exception):
    """A problem reported back to the model as a tool result, not a crash."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    writes: bool
    run: Callable[[Any, dict[str, Any]], Any]


def norm_mac(value: object) -> str:
    v = str(value).strip().upper().replace("-", ":")
    parts = v.split(":")
    if len(parts) != 6 or not all(
        len(p) == 2 and all(c in "0123456789ABCDEF" for c in p) for p in parts
    ):
        raise ToolError(f"Not a MAC address: {value!r}")
    return v


def _mac_or_raw(value: object) -> str:
    """Normalise a MAC if it is one; some firmware puts other values in MAC-ish fields."""
    try:
        return norm_mac(value)
    except ToolError:
        return "" if value is None else str(value)


_STOK = re.compile(r"stok=[0-9A-Za-z]+")


def redact(text: str) -> str:
    """Strip the router session token from error text (it appears in request URLs)."""
    return _STOK.sub("stok=<redacted>", text)


def _ip_key(ip: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in ip.split("."))
    except ValueError:
        return (999,)


class Router:
    """Opens one short session per call. The Deco allows a single admin session."""

    def __init__(self, cfg: Config, client_factory: Callable[..., Any] | None = None) -> None:
        self.cfg = cfg
        if client_factory is None:
            from tplink_deco_api import DecoClient

            client_factory = DecoClient
        self._factory = client_factory
        self._lock = threading.Lock()

    def call(self, fn: Callable[[Any], Any]) -> Any:
        with self._lock:
            deco = self._factory(self.cfg.host, self.cfg.username, self.cfg.password, timeout=self.cfg.timeout)
            deco.login()
            try:
                return fn(deco)
            finally:
                deco.logout()

    def check_ip(self, value: object) -> str:
        try:
            ip = ipaddress.ip_address(str(value).strip())
        except ValueError as exc:
            raise ToolError(f"Not an IPv4 address: {value!r}") from exc
        lan = self.cfg.lan
        if ip.version != 4 or ip not in lan or ip in (lan.network_address, lan.broadcast_address):
            raise ToolError(f"{ip} is not a usable address in {lan}")
        if str(ip) == self.cfg.host:
            raise ToolError(f"{ip} is the router itself")
        return str(ip)


def _plain(obj: Any) -> Any:
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _plain(getattr(obj, k)) for k in obj.__dataclass_fields__}
    if isinstance(obj, (list, tuple)):
        return [_plain(x) for x in obj]
    return obj


def _find(table: Any, mac: str) -> Any:
    for r in table.reservations:
        if norm_mac(r.mac) == mac:
            return r
    return None


# ------------------------------------------------------------------ reads


def status(router: Router, _args: dict[str, Any]) -> Any:
    def run(deco: Any) -> Any:
        out: dict[str, Any] = {"nodes": []}
        for d in deco.get_device_list():
            out["nodes"].append({
                "nickname": d.custom_nickname or d.nickname,
                "model": d.device_model,
                "hardware": d.hardware_ver,
                "firmware": d.software_ver,
                "role": d.role,
                "ip": d.device_ip,
                "mac": norm_mac(d.mac),
                "internet": d.inet_status,
                "group": d.group_status,
            })
        extras: dict[str, Callable[[], Any]] = {
            "internet": lambda: _plain(deco.get_internet_status()),
            "performance": lambda: _plain(deco.get_performance()),
            "dhcp": lambda: deco.request("admin/dhcp", "dhcp_info", {"operation": "read"}),
        }
        for key, fetch in extras.items():
            try:
                out[key] = fetch()
            except Exception as exc:  # noqa: BLE001 - optional, firmware dependent
                out[key] = f"unavailable on this firmware ({type(exc).__name__})"
                log.info("status extra %s unavailable: %s", key, redact(str(exc)))
        return out

    return router.call(run)


def clients(router: Router, args: dict[str, Any]) -> Any:
    online_only = bool(args.get("online_only", False))

    def run(deco: Any) -> Any:
        rows = [
            {
                "name": c.name,
                "mac": _mac_or_raw(c.mac),
                "ip": c.ip,
                "online": c.online,
                "connection": c.wire_type or c.connection_type,
                "deco": _mac_or_raw(c.access_host),
                "type": c.client_type,
            }
            for c in deco.get_client_list()
            if c.mac
            if c.online or not online_only
        ]
        rows.sort(key=lambda r: _ip_key(r["ip"]))
        return {"count": len(rows), "clients": rows}

    return router.call(run)


# Read-only endpoints tried by deco_config: (section, path, form, operation).
# Many forms are served only to the Deco app; those answer 404 on the web API
# and are reported as unavailable. Only read-style operations belong here.
CONFIG_READS: tuple[tuple[str, str, str, str], ...] = (
    ("system", "admin/device", "mode", "read"),
    ("system", "admin/device", "timesetting", "read"),
    ("system", "admin/device", "gateway", "read"),
    ("system", "admin/system", "sysmode", "read"),
    ("system", "admin/eco_mode", "eco_mode", "read"),
    ("wan", "admin/network", "wan_mode", "read"),
    ("wan", "admin/network", "wan_ipv4", "read"),
    ("wan", "admin/network", "dhcp_dial", "read"),
    ("wan", "admin/network", "mac_clone", "read"),
    ("wan", "admin/network", "vlan", "read"),
    ("lan", "admin/network", "lan_ipv4", "read"),
    ("lan", "admin/network", "lan_ip", "read"),
    ("lan", "admin/dhcp", "dhcp_info", "read"),
    ("lan", "admin/network", "igmp_setting", "read"),
    ("lan", "admin/network", "routes_static", "getlist"),
    ("lan", "admin/network", "routes_system", "getlist"),
    ("ipv6", "admin/network", "ipv6", "read"),
    ("ipv6", "admin/ipv6_firewall", "firewall", "read"),
    ("wifi", "admin/wireless", "wlan", "read"),
    ("wifi", "admin/wireless", "operation_mode", "read"),
    ("wifi", "admin/wireless", "ieee80211r", "read"),
    ("wifi", "admin/wireless", "beamforming", "read"),
    ("wifi", "admin/wireless", "bandwidth_enhance", "read"),
    ("wifi", "admin/wireless", "power", "read"),
    ("wifi", "admin/wireless", "ofdma", "read"),
    ("wifi", "admin/wireless", "wifi_schedule", "read"),
    ("wifi", "admin/network", "wifi_network", "read"),
    ("wifi", "admin/wps", "status", "get"),
    ("nat", "admin/nat", "setting", "read"),
    ("nat", "admin/nat", "vs", "getlist"),
    ("nat", "admin/nat", "dmz", "read"),
    ("nat", "admin/nat", "alg", "get"),
    ("nat", "admin/nat", "sip_alg", "get"),
    ("nat", "admin/network", "upnp", "read"),
    ("services", "admin/ddns", "ddns", "get"),
    ("services", "admin/iptv", "iptv", "get"),
    ("services", "admin/vpn_client", "config", "read"),
    ("services", "admin/vpn_server", "server", "read"),
    ("security", "admin/security", "info", "read"),
    ("security", "admin/administration", "remote", "read"),
    ("security", "admin/client", "black_list", "list"),
    ("security", "admin/client", "client_isolation", "read"),
    ("security", "admin/qos", "tm_qos", "read"),
)

_SECRET_KEY = re.compile(
    r"pass|pwd|psk|key|secret|token|(?:^|_)pin(?:$|_)|cert|cookie|account|email|user", re.IGNORECASE
)


def scrub(obj: Any) -> Any:
    """Replace the value of any secret-looking field, at any depth."""
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if _SECRET_KEY.search(str(k)) and v not in ("", None, [], {}) else scrub(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    return obj


def config(router: Router, args: dict[str, Any]) -> Any:
    wanted = args.get("sections") or []
    if not isinstance(wanted, list):
        raise ToolError("sections must be a list of section names")
    known = sorted({s for s, *_ in CONFIG_READS})
    unknown = [s for s in wanted if s not in known]
    if unknown:
        raise ToolError(f"Unknown sections {unknown}; choose from {known}")

    def run(deco: Any) -> Any:
        out: dict[str, dict[str, Any]] = {}
        unavailable: list[str] = []
        for section, path, form, op in CONFIG_READS:
            if wanted and section not in wanted:
                continue
            name = f"{path.removeprefix('admin/')}:{form}"
            try:
                result = deco.request(path, form, {"operation": op})
            except Exception as exc:  # noqa: BLE001 - firmware dependent
                unavailable.append(f"{name} ({type(exc).__name__})")
                log.info("config %s unavailable: %s", name, redact(str(exc)))
                continue
            out.setdefault(section, {})[name] = scrub(result)
        return {"config": out, "unavailable": unavailable,
                "note": "Fields that look like passwords, keys, tokens or accounts are redacted."}

    return router.call(run)


def reservations(router: Router, _args: dict[str, Any]) -> Any:
    def run(deco: Any) -> Any:
        table = deco.address_reservations.get_table()
        known = {_mac_or_raw(c.mac): c for c in deco.get_client_list()}
        rows = []
        for r in table.reservations:
            c = known.get(norm_mac(r.mac))
            rows.append({
                "mac": norm_mac(r.mac),
                "ip": r.ip,
                "name": c.name if c else "",
                "current_ip": c.ip if c else "",
                "online": bool(c and c.online),
            })
        rows.sort(key=lambda r: _ip_key(r["ip"]))
        return {"count": len(rows), "max": table.max_count, "reservations": rows}

    return router.call(run)


GENERIC_NAME = re.compile(
    r"^(device_[0-9a-f]{4}|lwip\d*|wlan\d*|espressif|esp[-_]?[0-9a-f]*|unknown|android-[0-9a-f]+|)$",
    re.IGNORECASE,
)


def traffic(router: Router, args: dict[str, Any]) -> Any:
    try:
        top = max(1, min(100, int(args.get("top", 10))))
    except (TypeError, ValueError) as exc:
        raise ToolError("top must be a number") from exc

    def run(deco: Any) -> Any:
        online = [c for c in deco.get_client_list() if c.online and c.mac]
        rows = [
            {
                "name": c.name,
                "mac": _mac_or_raw(c.mac),
                "ip": c.ip,
                "down": c.down_speed,
                "up": c.up_speed,
                "connection": c.wire_type or c.connection_type,
            }
            for c in online
        ]
        rows.sort(key=lambda r: (r["down"] or 0) + (r["up"] or 0), reverse=True)
        return {
            "units": "as reported by the router; the library documents KB/s",
            "total": {"down": sum(r["down"] or 0 for r in rows), "up": sum(r["up"] or 0 for r in rows),
                      "online_clients": len(rows)},
            "busiest": rows[:top],
            "note": "A snapshot of current rates, not usage over time.",
        }

    return router.call(run)


def _expected(args: dict[str, Any], router: Router) -> list[dict[str, Any]]:
    raw = args.get("expected") or []
    if not isinstance(raw, list):
        raise ToolError("expected must be a list of objects")
    out = []
    for i, e in enumerate(raw):
        if not isinstance(e, dict) or "mac" not in e:
            raise ToolError(f"expected[{i}] needs at least a mac")
        item = {"mac": norm_mac(e["mac"]), "name": str(e.get("name", ""))[:80],
                "static": bool(e.get("static", False))}
        if e.get("ip"):
            item["ip"] = router.check_ip(e["ip"])
        out.append(item)
    return out


def audit(router: Router, args: dict[str, Any]) -> Any:
    expected = _expected(args, router)

    def run(deco: Any) -> Any:
        table = deco.address_reservations.get_table()
        reserved = {norm_mac(r.mac): r.ip for r in table.reservations}
        by_ip: dict[str, list[str]] = {}
        for mac, ip in reserved.items():
            by_ip.setdefault(ip, []).append(mac)
        clients = {_mac_or_raw(c.mac): c for c in deco.get_client_list() if c.mac}
        holder = {c.ip: mac for mac, c in clients.items() if c.online and c.ip}
        findings: list[dict[str, Any]] = []

        def add(severity: str, kind: str, detail: str, **extra: Any) -> None:
            findings.append({"severity": severity, "kind": kind, "detail": detail, **extra})

        for ip, macs in by_ip.items():
            if len(macs) > 1:
                add("problem", "duplicate_reservation", f"{ip} is reserved for {', '.join(macs)}", ip=ip)
        for mac, ip in reserved.items():
            c = clients.get(mac)
            if c is None:
                add("info", "reserved_unknown", f"{ip} is reserved for {mac}, which the router doesn't know",
                    mac=mac, ip=ip)
            elif not c.online:
                add("info", "reserved_offline", f"{ip} is reserved for {c.name or mac}, offline", mac=mac, ip=ip)
            elif c.ip != ip:
                add("warning", "not_on_reserved_ip",
                    f"{c.name or mac} is on {c.ip}, reserved {ip}; it moves at its next lease renewal",
                    mac=mac, ip=ip, current_ip=c.ip)
            other = holder.get(ip)
            if other and other != mac:
                add("problem", "reserved_ip_in_use",
                    f"{ip} is reserved for {mac} but {clients[other].name or other} is using it",
                    mac=mac, ip=ip, user=other)
        for mac, c in clients.items():
            if c.online and GENERIC_NAME.match((c.name or "").strip()):
                add("info", "generic_name", f"{c.ip} {mac} is named {c.name!r}; consider renaming",
                    mac=mac, ip=c.ip)
        for e in expected:
            mac, ip, label = e["mac"], e.get("ip"), e["name"] or e["mac"]
            have = reserved.get(mac)
            if ip and ip in by_ip and mac not in by_ip[ip]:
                add("problem", "expected_ip_reserved_for_other",
                    f"{label} expects {ip}, but it is reserved for {', '.join(by_ip[ip])}", mac=mac, ip=ip)
            if have and ip and have != ip:
                add("problem", "reservation_mismatch", f"{label} expects {ip} but is reserved {have}",
                    mac=mac, ip=ip, reserved_ip=have)
            elif not have:
                sev = "warning" if e["static"] else "info"
                why = ("static address with no reservation; the DHCP server could hand it out"
                       if e["static"] else "no reservation")
                add(sev, "missing_reservation", f"{label}{' at ' + ip if ip else ''}: {why}", mac=mac, ip=ip)
            user = holder.get(ip) if ip else None
            if user and user != mac:
                add("problem", "expected_ip_in_use",
                    f"{label} expects {ip}, but {clients[user].name or user} is using it now",
                    mac=mac, ip=ip, user=user)
            c = clients.get(mac)
            if ip and c and c.online and c.ip and c.ip != ip:
                add("warning", "expected_ip_mismatch", f"{label} expects {ip} but is on {c.ip}",
                    mac=mac, ip=ip, current_ip=c.ip)

        order = {"problem": 0, "warning": 1, "info": 2}
        findings.sort(key=lambda f: (order[f["severity"]], _ip_key(f.get("ip") or "")))
        counts = {s: sum(f["severity"] == s for f in findings) for s in order}
        return {"reservations": len(reserved), "max": table.max_count, "counts": counts, "findings": findings}

    return router.call(run)


# ----------------------------------------------------------------- writes


def client_rename(router: Router, args: dict[str, Any]) -> Any:
    mac = norm_mac(args.get("mac", ""))
    name = str(args.get("name", "")).strip()
    if not name or len(name) > 64 or any(ord(ch) < 32 for ch in name):
        raise ToolError("name must be 1 to 64 printable characters")

    def run(deco: Any) -> Any:
        known = {_mac_or_raw(c.mac): c for c in deco.get_client_list()}
        if mac not in known:
            raise ToolError(f"{mac} is not in the router's client list")
        old = known[mac].name
        deco.clients.update(mac, name)
        log.warning("WRITE client rename %s %r -> %r", mac, old, name)
        return {"renamed": {"mac": mac, "from": old, "to": name}}

    return router.call(run)


def reservation_add(router: Router, args: dict[str, Any]) -> Any:
    from tplink_deco_api import AddressReservation

    mac, ip = norm_mac(args.get("mac", "")), router.check_ip(args.get("ip", ""))

    def run(deco: Any) -> Any:
        table = deco.address_reservations.get_table()
        if _find(table, mac):
            raise ToolError(f"{mac} already has a reservation; use deco_reservation_change")
        for r in table.reservations:
            if r.ip == ip:
                raise ToolError(f"{ip} is already reserved for {norm_mac(r.mac)}")
        deco.address_reservations.add(AddressReservation(mac, ip))
        log.warning("WRITE reservation add %s -> %s", mac, ip)
        return {
            "added": {"mac": mac, "ip": ip},
            "note": "The device gets the address when it next renews its lease or reconnects.",
        }

    return router.call(run)


def reservation_change(router: Router, args: dict[str, Any]) -> Any:
    from tplink_deco_api import AddressReservation

    mac, ip = norm_mac(args.get("mac", "")), router.check_ip(args.get("ip", ""))

    def run(deco: Any) -> Any:
        table = deco.address_reservations.get_table()
        current = _find(table, mac)
        if not current:
            raise ToolError(f"{mac} has no reservation; use deco_reservation_add")
        for r in table.reservations:
            if r.ip == ip and norm_mac(r.mac) != mac:
                raise ToolError(f"{ip} is already reserved for {norm_mac(r.mac)}")
        deco.address_reservations.modify(current, AddressReservation(mac, ip))
        log.warning("WRITE reservation change %s %s -> %s", mac, current.ip, ip)
        return {"changed": {"mac": mac, "from": current.ip, "to": ip}}

    return router.call(run)


def reservation_remove(router: Router, args: dict[str, Any]) -> Any:
    mac = norm_mac(args.get("mac", ""))

    def run(deco: Any) -> Any:
        current = _find(deco.address_reservations.get_table(), mac)
        if not current:
            raise ToolError(f"{mac} has no reservation")
        deco.address_reservations.remove(mac)
        log.warning("WRITE reservation remove %s (was %s)", mac, current.ip)
        return {"removed": {"mac": mac, "ip": current.ip}}

    return router.call(run)


def reboot(router: Router, args: dict[str, Any]) -> Any:
    if args.get("confirm") != REBOOT_PHRASE:
        raise ToolError(
            f'Refused: set confirm to exactly "{REBOOT_PHRASE}", and only after the user has said yes.'
        )

    def run(deco: Any) -> Any:
        macs = [{"mac": d.mac} for d in deco.get_device_list()]
        log.warning("WRITE reboot %d node(s)", len(macs))
        deco.request("admin/device", "system", {"operation": "reboot", "params": {"mac_list": macs}})
        return {"rebooting": len(macs), "note": "The network drops for a few minutes."}

    return router.call(run)


# ---------------------------------------------------------------- catalog

_MAC = {"type": "string", "description": "Device MAC address, e.g. AA:BB:CC:DD:EE:FF"}
_IP = {"type": "string", "description": "IPv4 address in the router's LAN"}
_NONE: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
_ASK = " Changes the router: only call after the user has agreed to this exact change."

ALL_TOOLS: tuple[Tool, ...] = (
    Tool("deco_status",
         "Router status: mesh nodes (model, firmware, role, IP), internet status, CPU and memory, DHCP pool.",
         _NONE, False, status),
    Tool("deco_clients",
         "List clients with name, MAC, IP, online state and the node they are connected to.",
         {"type": "object", "additionalProperties": False,
          "properties": {"online_only": {"type": "boolean", "description": "Only clients online now"}}},
         False, clients),
    Tool("deco_config",
         "Read the router's settings (system, WAN, LAN, IPv6, Wi-Fi, NAT and port forwarding, services, "
         "security). Read-only; secrets are redacted. Endpoints this firmware doesn't serve are listed "
         "as unavailable.",
         {"type": "object", "additionalProperties": False,
          "properties": {"sections": {"type": "array", "items": {"type": "string"},
                                      "description": "Limit to these sections (default all)"}}},
         False, config),
    Tool("deco_reservations",
         "List DHCP address reservations (MAC to IP), with each device's name and current IP where known.",
         _NONE, False, reservations),
    Tool("deco_traffic",
         "Current upload and download rate per online client, busiest first, with totals.",
         {"type": "object", "additionalProperties": False,
          "properties": {"top": {"type": "integer", "description": "How many clients to list (default 10)"}}},
         False, traffic),
    Tool("deco_audit",
         "Check DHCP reservations for problems: duplicates, reserved IPs used by another device, devices "
         "not on their reserved IP, reservations for unknown or offline devices, and generic client names. "
         "Pass `expected` (e.g. from a hypervisor's guest list) to also check that each device has the "
         "reservation it should. Read-only.",
         {"type": "object", "additionalProperties": False,
          "properties": {"expected": {"type": "array", "description": "Devices that should be reserved",
                                      "items": {"type": "object", "additionalProperties": False,
                                                "required": ["mac"],
                                                "properties": {"mac": _MAC, "ip": _IP,
                                                               "name": {"type": "string"},
                                                               "static": {"type": "boolean",
                                                                          "description": "Device sets this IP itself"}}}}}},
         False, audit),
    Tool("deco_client_rename", "Rename a client as shown in the Deco app and router." + _ASK,
         {"type": "object", "additionalProperties": False,
          "properties": {"mac": _MAC, "name": {"type": "string", "description": "New name, 1-64 characters"}},
          "required": ["mac", "name"]},
         True, client_rename),
    Tool("deco_reservation_add", "Add a DHCP address reservation." + _ASK,
         {"type": "object", "additionalProperties": False,
          "properties": {"mac": _MAC, "ip": _IP}, "required": ["mac", "ip"]},
         True, reservation_add),
    Tool("deco_reservation_change", "Change the IP of an existing DHCP address reservation." + _ASK,
         {"type": "object", "additionalProperties": False,
          "properties": {"mac": _MAC, "ip": _IP}, "required": ["mac", "ip"]},
         True, reservation_change),
    Tool("deco_reservation_remove", "Remove a DHCP address reservation." + _ASK,
         {"type": "object", "additionalProperties": False,
          "properties": {"mac": _MAC}, "required": ["mac"]},
         True, reservation_remove),
    Tool("deco_reboot",
         "Reboot every Deco node. Drops the whole network for minutes." + _ASK,
         {"type": "object", "additionalProperties": False,
          "properties": {"confirm": {"type": "string", "description": f'Must be exactly "{REBOOT_PHRASE}"'}},
          "required": ["confirm"]},
         True, reboot),
)


def enabled_tools(cfg: Config) -> dict[str, Tool]:
    tools = {}
    for t in ALL_TOOLS:
        if t.writes and cfg.read_only:
            continue
        if t.name == "deco_reboot" and not cfg.allow_reboot:
            continue
        tools[t.name] = t
    return tools
