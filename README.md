# deco-mcp

An [MCP](https://modelcontextprotocol.io) server for **TP-Link Deco** mesh
routers. It lets an AI assistant such as Claude Code read the router and
manage DHCP address reservations, using the router's own local web API.

- List connected clients with name, MAC, IP and the node they're on.
- List, add, change and remove DHCP address reservations.
- Read status and settings: mesh nodes, model, firmware, internet, WAN, LAN, Wi-Fi
  and more, with secrets redacted.
- Reboot the mesh (off by default).

Everything stays on your network. The server talks only to the router's
address, over the same encrypted login the Deco web UI uses, and needs no
TP-Link cloud account access.

> **Not affiliated with TP-Link.** This uses an undocumented local API through
> the community library
> [`tplink-deco-api`](https://github.com/roquerodrigo/tplink-deco-api).
> Firmware updates can change or break it.

## How it works

```
Claude Code ──HTTP + bearer token──▶ deco-mcp (small LXC/VM) ──HTTPS, encrypted login──▶ Deco router
```

- The server is the standard library's HTTP server speaking MCP's
  [streamable HTTP transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
  with plain JSON responses: no SSE and no sessions. The whole package is
  under 600 lines, with no web framework.
- Each tool call logs in to the router, does one job and drops the session.
  Calls run one at a time, because the Deco allows only one admin session.
- Its one dependency is `tplink-deco-api`, which itself needs only
  `cryptography`.

Tested on a three-unit **Deco M4R v2** mesh running firmware 1.8.2. The
library has also been checked against a BE65 by its author. Other models that
use the same web UI should work. Please open an issue with your model,
whether it works or not.

## Tools

| Tool | Does | Changes the router |
|---|---|---|
| `deco_status` | Mesh nodes (model, firmware, role, IP), internet status, CPU and memory, and the DHCP pool where the firmware exposes it (not on the M4R) | no |
| `deco_clients` | Clients with name, MAC, IP, online state, connection and node. `online_only` filter | no |
| `deco_reservations` | Reservations (MAC to IP), each with the device's name and current IP | no |
| `deco_config` | Router settings (system, WAN, LAN, IPv6, Wi-Fi, NAT, services, security). Secret-looking fields are redacted on the server. Forms the firmware serves only to the Deco app are listed as unavailable | no |
| `deco_reservation_add` | Add a reservation. Refuses duplicates and addresses outside the LAN | yes |
| `deco_reservation_change` | Change the IP of a reservation | yes |
| `deco_reservation_remove` | Remove a reservation | yes |
| `deco_reboot` | Reboot every node. Needs `DECO_ALLOW_REBOOT=1` and `confirm: "reboot the whole mesh"` | yes |

Input is checked before any request reaches the router: MACs must be valid,
and IPs must be usable addresses inside the LAN that aren't the router itself.
Every change is logged at `WARNING` with a `WRITE` prefix.

A new or changed reservation takes effect when the device next renews its
DHCP lease or reconnects.

## Install

The server is meant to run on an always-on Linux box on the same LAN as the
router: a small Proxmox LXC or VM is ideal (1 core, 512 MB RAM, 4 GB disk).
It needs Python 3.14, which the installer fetches with
[uv](https://github.com/astral-sh/uv).

On a Debian or Ubuntu host, as root:

```sh
git clone https://github.com/<you>/deco-mcp.git
cd deco-mcp
./deploy/install.sh          # packages, uv, Python 3.14, pinned library, systemd unit
deco-mcp-setup               # router address and password (hidden), makes a token
systemctl start deco-mcp
journalctl -u deco-mcp -n 5  # "deco-mcp 0.1.0 on 0.0.0.0:8080, ..."
```

`deploy/install.sh` installs the library from
[`deploy/requirements.in`](deploy/requirements.in), pinned to a reviewed
git commit, and compiles a hashed lock from it. It's safe to run again to
update.

Check the login without starting the server:

```sh
set -a; . /etc/deco-mcp.env; set +a
/opt/deco-mcp/venv/bin/python -I -m deco_mcp --check
```

### The router password

Deco routers have no separate local admin account. The web UI at
`https://<router>` logs in as `admin` with the **owner's password**, the one
used to sign in to the Deco app. `deco-mcp-setup` asks for it without echoing
it and stores it only in `/etc/deco-mcp.env` (mode 600, root). systemd reads
that file before dropping to the unprivileged `deco-mcp` user.

Run `deco-mcp-setup` again after changing the password. `--new-token`
replaces the token, and `--print-token` prints it.

## Configuration

All settings are environment variables, normally in `/etc/deco-mcp.env`.

| Variable | Default | Meaning |
|---|---|---|
| `DECO_HOST` | `192.168.68.1` | Router address (the Deco default) |
| `DECO_USERNAME` | `admin` | Login user. Leave as `admin` |
| `DECO_PASSWORD` | (required) | Owner password |
| `DECO_LAN` | `DECO_HOST`/24 | Network that reserved IPs must be inside |
| `DECO_TIMEOUT` | `15` | Seconds per router request |
| `DECO_READ_ONLY` | `0` | `1` hides every tool that changes the router |
| `DECO_ALLOW_REBOOT` | `0` | `1` offers `deco_reboot` |
| `MCP_TOKEN` | (required) | Bearer token, at least 32 characters |
| `MCP_BIND` | `127.0.0.1` | Listen address. `deco-mcp-setup` writes `0.0.0.0` |
| `MCP_PORT` | `8080` | Listen port. The endpoint is `/mcp` |
| `MCP_ALLOW_NETS` | loopback and `DECO_LAN` | Comma-separated CIDRs allowed to connect |

## Connect Claude Code

Store the token in the macOS Keychain (paste it at the hidden prompt):

```sh
security add-generic-password -a deco-mcp -s deco-mcp-token -w
```

Add the server for your user, with a
[`headersHelper`](https://docs.anthropic.com/en/docs/claude-code/mcp) that
reads the token from the Keychain, so the token isn't stored in any config
file:

```sh
claude mcp add-json --scope user deco '{
  "type": "http",
  "url": "http://<server-ip>:8080/mcp",
  "headersHelper": "/path/to/deco-mcp/examples/mcp-headers.sh"
}'
```

Then make Claude Code always ask before a change. Merge
[`examples/claude-settings.json`](examples/claude-settings.json) into
`~/.claude/settings.json`: read tools run without a prompt, and the write
tools always ask. On Linux, replace the helper with one that reads the token
from your own secret store.

Other MCP clients that support streamable HTTP and custom headers work the
same way: send `Authorization: Bearer <token>`.

## Security

Read this before exposing anything.

- **The server holds your router admin password.** Anyone with root on the
  host running it, or on its hypervisor, can read it. Give it its own small
  container.
- **Plain HTTP.** The token travels unencrypted across the LAN. If your LAN
  isn't trusted, put the server behind a TLS reverse proxy (Traefik, Caddy,
  nginx) and keep `MCP_BIND=127.0.0.1`.
- **Access control:** a source-address allowlist (`MCP_ALLOW_NETS`), then a
  constant-time bearer token check. Any request carrying an `Origin` header
  is refused, so a web page in your browser can't reach the server through
  DNS rebinding. Bodies are capped at 64 KB.
- **Never forward the port to the internet.**
- **The router's certificate isn't checked.** Decos use self-signed
  certificates, so the library turns off certificate checks. A device already
  on your LAN could pose as the router and capture the login.
- **Writes need consent.** Write tools are marked `destructiveHint`, and their
  descriptions tell the model to get your yes first. The real safeguard is
  your client's permission settings, so set the write tools to *ask* (see
  above), or run with `DECO_READ_ONLY=1`.
- **Dependency pinning.** `deploy/requirements.in` pins `tplink-deco-api` to a
  commit that was read in full (about 1,950 lines: one network call site,
  which posts only to the configured router; no `eval`, `exec` or subprocess;
  the PyPI source matched the git tag). Review the diff before bumping it.
- The systemd unit runs as an unprivileged user with no capabilities, a
  read-only system, private `/tmp`, and other hardening.

Report security problems privately through GitHub's *Report a vulnerability*
button. Please don't open a public issue.

## Development

```sh
uv sync            # Python 3.14 + dev tools
uv run pytest      # runs against a fake router; never touches a real one
```

## Licence

MIT. See [LICENSE](LICENSE).
