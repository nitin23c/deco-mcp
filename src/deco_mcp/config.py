"""Settings, read once from the environment."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field


def _secret(name: str) -> str:
    """Read NAME, or the file named by NAME_FILE (Docker/Podman secrets).

    The file wins if both are set. Trailing newlines are stripped.
    """
    path = os.environ.get(f"{name}_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().rstrip("\r\n")
        except OSError:
            raise SystemExit(f"deco-mcp: cannot read {name}_FILE") from None
    return os.environ.get(name, "")


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    host: str
    username: str
    password: str
    token: str
    bind: str
    port: int
    lan: ipaddress.IPv4Network
    allow_nets: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    read_only: bool
    allow_reboot: bool
    timeout: float = 15.0
    problems: tuple[str, ...] = field(default=())

    @classmethod
    def from_env(cls) -> Config:
        # Never echo setting values in errors: a password typed into the wrong
        # prompt would otherwise end up in the logs.
        host = os.environ.get("DECO_HOST", "192.168.68.1").strip()
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            raise SystemExit("deco-mcp: DECO_HOST must be the router's IPv4 address") from None
        try:
            lan = ipaddress.ip_network(os.environ.get("DECO_LAN", f"{host}/24"), strict=False)
        except ValueError:
            raise SystemExit("deco-mcp: DECO_LAN must be a network such as 192.168.68.0/24") from None
        allow = os.environ.get("MCP_ALLOW_NETS", f"127.0.0.0/8,::1/128,{lan}")
        try:
            allow_nets = tuple(
                ipaddress.ip_network(n.strip(), strict=False) for n in allow.split(",") if n.strip()
            )
            port = int(os.environ.get("MCP_PORT", "8080"))
            timeout = float(os.environ.get("DECO_TIMEOUT", "15"))
        except ValueError:
            raise SystemExit("deco-mcp: MCP_ALLOW_NETS, MCP_PORT or DECO_TIMEOUT is not valid") from None
        cfg = cls(
            host=host,
            username=os.environ.get("DECO_USERNAME", "admin"),
            password=_secret("DECO_PASSWORD"),
            token=_secret("MCP_TOKEN"),
            bind=os.environ.get("MCP_BIND", "127.0.0.1"),
            port=port,
            lan=lan,
            allow_nets=allow_nets,
            read_only=_flag("DECO_READ_ONLY"),
            allow_reboot=_flag("DECO_ALLOW_REBOOT"),
            timeout=timeout,
        )
        problems = []
        if not cfg.password:
            problems.append("DECO_PASSWORD (or DECO_PASSWORD_FILE) is not set")
        if len(cfg.token) < 32:
            problems.append("MCP_TOKEN (or MCP_TOKEN_FILE) is not set or shorter than 32 characters")
        return cls(**{**cfg.__dict__, "problems": tuple(problems)})
