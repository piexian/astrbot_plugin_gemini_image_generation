"""WebUI 模型目录与图片归档共用的出站地址策略。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

from .core.errors import PathPolicyError, redact_url
from .core.path_policy import PathPolicy, _resolve_host

_PRIVATE_HINT = "内网地址需先保存到供应商配置后再使用"
_METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(value)
    for value in ("169.254.169.254", "fd00:ec2::254", "100.100.100.200")
)
_SHARED_NETWORK = ipaddress.ip_network("100.64.0.0/10")


class PinnedResolver:
    """aiohttp resolver that only returns addresses approved before connecting."""

    def __init__(self) -> None:
        self._pins: dict[str, tuple[ipaddress._BaseAddress, ...]] = {}

    def pin(self, host: str, addresses: Iterable[ipaddress._BaseAddress]) -> None:
        self._pins[host.lower().rstrip(".")] = tuple(addresses)

    async def resolve(self, host: str, port: int = 0, family: int = 0):
        addresses = self._pins.get(host.lower().rstrip("."))
        if not addresses:
            raise OSError(f"未验证的出站地址: {host}")
        result = []
        for address in addresses:
            address_family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            if family not in (0, address_family):
                continue
            result.append(
                {
                    "hostname": host,
                    "host": str(address),
                    "port": port,
                    "family": address_family,
                    "proto": 0,
                    "flags": 0,
                }
            )
        if not result:
            raise OSError(f"未验证的出站地址: {host}")
        return result

    async def close(self) -> None:
        return None


def _origin(parsed) -> tuple[str, str, int] | None:
    try:
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except (TypeError, ValueError):
        return None
    return parsed.scheme.lower(), parsed.hostname.lower().rstrip("."), port


def _saved_origins(api_bases: Iterable[str]) -> set[tuple[str, str, int]]:
    origins = set()
    for value in api_bases:
        if not isinstance(value, str) or not value.strip():
            continue
        origin = _origin(urlsplit(value.strip()))
        if origin is not None:
            origins.add(origin)
    return origins


async def check_outbound_url(
    url: str,
    *,
    saved_api_bases: Iterable[str] = (),
    resolver: Callable = _resolve_host,
    pin: PinnedResolver | None = None,
) -> str:
    """检查目标及全部 DNS 结果；私网仅放行已保存供应商 origin。"""

    text = str(url or "").strip()
    parsed = urlsplit(text)
    origin = _origin(parsed)
    if origin is None:
        raise PathPolicyError(f"仅允许不带用户信息的 http(s) URL: {redact_url(text)}")
    allow_saved_private = origin in _saved_origins(saved_api_bases)
    host = parsed.hostname.rstrip(".")
    addresses = await asyncio.to_thread(resolver, host)
    if pin is not None:
        pin.pin(host, addresses)
    policy = PathPolicy((), allow_private_networks=False)
    for address in addresses:
        if address.is_link_local or address in _METADATA_ADDRESSES:
            raise PathPolicyError("禁止访问 link-local 或云元数据地址")
        if (
            allow_saved_private
            and (address.is_loopback or address.is_private)
            and not (
                address in _SHARED_NETWORK
                or address.is_multicast
                or address.is_reserved
                or address.is_unspecified
            )
        ):
            continue
        try:
            policy.validate_addresses(host, (address,))
        except PathPolicyError as exc:
            raise PathPolicyError(_PRIVATE_HINT) from exc
    return text
