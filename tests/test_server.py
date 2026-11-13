"""Tests run against a fake router; nothing here talks to a real Deco."""

from __future__ import annotations

import http.client
import json
import threading
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer

import pytest

from deco_mcp.config import Config
from deco_mcp.server import McpApp, make_handler
from deco_mcp.tools import REBOOT_PHRASE, Router

TOKEN = "t" * 64


@dataclass(frozen=True)
class Res:
    mac: str
    ip: str


@dataclass
class Table:
    reservations: tuple
    max_count: int = 64


@dataclass
class Client:
    mac: str
    ip: str
    name: str
    online: bool = True
    wire_type: str = "wired"
    connection_type: str = ""
    access_host: str = "02-00-00-00-00-AA"
    client_type: str = "pc"


@dataclass
class Node:
    mac: str = "02-00-00-00-00-AA"
    device_ip: str = "10.9.0.1"
    device_model: str = "X55"
    hardware_ver: str = "1.0"
    software_ver: str = "1.0.0"
    role: str = "master"
    nickname: str = "living_room"
    custom_nickname: str = ""
    inet_status: str = "online"
    group_status: str = "connected"


@dataclass
class FakeState:
    reservations: list = field(default_factory=lambda: [Res("02-00-00-00-00-01", "10.9.0.50")])
    clients: list = field(default_factory=lambda: [
        Client("02-00-00-00-00-01", "10.9.0.50", "server"),
        Client("02-00-00-00-00-02", "10.9.0.77", "laptop", online=False),
    ])
    calls: list = field(default_factory=list)
    logins: int = 0
    logouts: int = 0


class FakeReservations:
    def __init__(self, st: FakeState) -> None:
        self.st = st

    def get_table(self):
        return Table(tuple(self.st.reservations))

    def add(self, r):
        self.st.calls.append(("add", r.mac, r.ip))
        self.st.reservations.append(Res(r.mac, r.ip))

    def modify(self, current, new):
        self.st.calls.append(("modify", current.ip, new.ip))
        self.st.reservations = [new if x == current else x for x in self.st.reservations]

    def remove(self, mac):
        self.st.calls.append(("remove", mac))
        self.st.reservations = [x for x in self.st.reservations if x.mac.replace("-", ":") != mac]


def fake_factory(st: FakeState):
    class FakeDeco:
        def __init__(self, host, username, password, timeout):
            assert (host, username, password) == ("10.9.0.1", "admin", "pw")
            self.address_reservations = FakeReservations(st)

        def login(self):
            st.logins += 1

        def logout(self):
            st.logouts += 1

        def get_device_list(self):
            return [Node()]

        def get_client_list(self):
            return list(st.clients)

        def get_internet_status(self):
            raise RuntimeError("not on this firmware")

        def get_performance(self):
            return {"cpu": 0.1}

        def request(self, path, form, data):
            st.calls.append(("request", path, form, data["operation"]))
            return {"startIpAddress": "10.9.0.100"}

    return FakeDeco


def make_cfg(**over) -> Config:
    import ipaddress

    base = dict(
        host="10.9.0.1", username="admin", password="pw", token=TOKEN, bind="127.0.0.1", port=0,
        lan=ipaddress.ip_network("10.9.0.0/24"),
        allow_nets=(ipaddress.ip_network("127.0.0.0/8"),),
        read_only=False, allow_reboot=True,
    )
    base.update(over)
    return Config(**base)


@pytest.fixture
def st():
    return FakeState()


def app_for(st, **over) -> McpApp:
    cfg = make_cfg(**over)
    return McpApp(cfg, Router(cfg, client_factory=fake_factory(st)))


def call(app, name, **args):
    reply = app.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": name, "arguments": args}})
    res = reply["result"]
    text = res["content"][0]["text"]
    return res["isError"], (text if res["isError"] else json.loads(text))


# ------------------------------------------------------------- protocol


def test_initialize_negotiates_version(st):
    app = app_for(st)
    r = app.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"}})
    assert r["result"]["protocolVersion"] == "2025-06-18"
    r = app.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                    "params": {"protocolVersion": "1999-01-01"}})
    assert r["result"]["protocolVersion"] == "2025-11-25"


def test_notification_gets_no_reply(st):
    assert app_for(st).handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_and_bad_request(st):
    app = app_for(st)
    assert app.handle({"jsonrpc": "2.0", "id": 1, "method": "nope"})["error"]["code"] == -32601
    assert app.handle({"id": 1})["error"]["code"] == -32600
    assert app.handle("x")["error"]["code"] == -32600


def test_tool_list_respects_modes(st):
    def names(app):
        r = app.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        return {t["name"] for t in r["result"]["tools"]}

    assert "deco_reboot" in names(app_for(st))
    assert "deco_reboot" not in names(app_for(st, allow_reboot=False))
    assert names(app_for(st, read_only=True)) == {"deco_status", "deco_clients", "deco_reservations"}


def test_disabled_tool_cannot_be_called(st):
    app = app_for(st, read_only=True)
    r = app.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "deco_reservation_add", "arguments": {}}})
    assert r["error"]["code"] == -32602
    assert st.calls == []


# ---------------------------------------------------------------- tools


def test_status_survives_missing_extras(st):
    err, out = call(app_for(st), "deco_status")
    assert not err
    assert out["nodes"][0]["model"] == "X55"
    assert out["internet"].startswith("unavailable")
    assert st.logins == st.logouts == 1


def test_clients_and_reservations(st):
    app = app_for(st)
    _, out = call(app, "deco_clients", online_only=True)
    assert [c["name"] for c in out["clients"]] == ["server"]
    _, out = call(app, "deco_reservations")
    assert out["reservations"] == [{"mac": "02:00:00:00:00:01", "ip": "10.9.0.50",
                                    "name": "server", "current_ip": "10.9.0.50", "online": True}]


def test_add_validates_before_touching_router(st):
    app = app_for(st)
    for args in ({"mac": "nope", "ip": "10.9.0.60"}, {"mac": "02:00:00:00:00:09", "ip": "8.8.8.8"},
                 {"mac": "02:00:00:00:00:09", "ip": "10.9.0.1"}, {"mac": "02:00:00:00:00:09", "ip": "10.9.0.255"}):
        err, _ = call(app, "deco_reservation_add", **args)
        assert err
    assert st.logins == 0


def test_add_refuses_duplicates(st):
    app = app_for(st)
    err, msg = call(app, "deco_reservation_add", mac="02-00-00-00-00-01", ip="10.9.0.60")
    assert err and "already has" in msg
    err, msg = call(app, "deco_reservation_add", mac="02:00:00:00:00:09", ip="10.9.0.50")
    assert err and "already reserved" in msg
    assert not [c for c in st.calls if c[0] == "add"]


def test_add_change_remove(st):
    app = app_for(st)
    err, out = call(app, "deco_reservation_add", mac="02-00-00-00-00-02", ip="10.9.0.60")
    assert not err and out["added"] == {"mac": "02:00:00:00:00:02", "ip": "10.9.0.60"}
    err, out = call(app, "deco_reservation_change", mac="02:00:00:00:00:02", ip="10.9.0.61")
    assert not err and out["changed"]["to"] == "10.9.0.61"
    err, out = call(app, "deco_reservation_remove", mac="02:00:00:00:00:02")
    assert not err and out["removed"]["ip"] == "10.9.0.61"
    assert [c[0] for c in st.calls] == ["add", "modify", "remove"]


def test_change_unknown_mac(st):
    err, msg = call(app_for(st), "deco_reservation_change", mac="02:00:00:00:00:09", ip="10.9.0.61")
    assert err and "no reservation" in msg


def test_reboot_needs_phrase(st):
    app = app_for(st)
    err, _ = call(app, "deco_reboot", confirm="yes")
    assert err and st.logins == 0
    err, out = call(app, "deco_reboot", confirm=REBOOT_PHRASE)
    assert not err and out["rebooting"] == 1
    assert ("request", "admin/device", "system", "reboot") in st.calls


# ----------------------------------------------------------------- HTTP


@pytest.fixture
def http_server(st):
    app = app_for(st)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def post(port, body, headers=None, path="/mcp"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"}
    h.update(headers or {})
    conn.request("POST", path, body=json.dumps(body), headers={k: v for k, v in h.items() if v is not None})
    r = conn.getresponse()
    data = r.read()
    return r.status, (json.loads(data) if data else None)


def test_http_round_trip(http_server):
    status, body = post(http_server, {"jsonrpc": "2.0", "id": 7, "method": "ping"})
    assert status == 200 and body == {"jsonrpc": "2.0", "id": 7, "result": {}}
    status, body = post(http_server, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status == 202 and body is None


def test_http_auth_and_origin(http_server):
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert post(http_server, ping, {"Authorization": None})[0] == 401
    assert post(http_server, ping, {"Authorization": "Bearer wrong"})[0] == 401
    assert post(http_server, ping, {"Origin": "http://evil.example"})[0] == 403
    assert post(http_server, ping, path="/other")[0] == 404


def test_http_rejects_disallowed_source(st):
    import ipaddress

    cfg = make_cfg(allow_nets=(ipaddress.ip_network("192.0.2.0/24"),))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(McpApp(cfg, Router(cfg, fake_factory(st)))))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert post(srv.server_address[1], {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0] == 403
    finally:
        srv.shutdown()


def test_bad_host_error_does_not_echo_value(monkeypatch):
    secret = "not-an-ip-s3cret!"
    monkeypatch.setenv("DECO_HOST", secret)
    with pytest.raises(SystemExit) as exc:
        Config.from_env()
    assert secret not in str(exc.value)


def test_clients_tolerate_non_mac_node_field(st):
    st.clients.append(Client("02-00-00-00-00-03", "10.9.0.80", "odd", access_host="1"))
    err, out = call(app_for(st), "deco_clients")
    assert not err
    assert {c["name"]: c["deco"] for c in out["clients"]}["odd"] == "1"


def test_router_errors_are_redacted(st):
    from deco_mcp.tools import redact

    assert redact("POST https://r/cgi-bin/luci/;stok=abc123/admin") == "POST https://r/cgi-bin/luci/;stok=<redacted>/admin"
