"""PathPolicy：参考图来源安全校验（本地根目录约束与远端地址防内网绕过）。"""

from __future__ import annotations

from collections.abc import Iterable
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from .errors import PathPolicyError

LOCAL_URL_SCHEMES = frozenset({"file"})
REMOTE_URL_SCHEMES = frozenset({"http", "https"})


def _resolve_host(
    host: str,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    """返回 IP 字面量或域名全部 A/AAAA 解析结果。

    结果只供单次校验使用：真实连接时系统会重新解析，校验与连接之间的
    DNS 重绑定（TOCTOU）由调用方以连接层手段处理，不在本层防御范围。
    """
    try:
        return (ipaddress.ip_address(host),)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise PathPolicyError(f"域名解析失败: {host}") from exc
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:  # pragma: no cover - getaddrinfo 只返回 IP
            continue
    if not addresses:
        raise PathPolicyError(f"域名没有可用 A/AAAA 记录: {host}")
    return tuple(addresses)


class PathPolicy:
    """校验本地路径落在允许根内、远端地址不指向本机或内网。"""

    def __init__(
        self,
        allowed_local_roots: tuple[Path, ...],
        *,
        allow_private_networks: bool = False,
    ) -> None:
        self.allowed_local_roots = tuple(
            Path(root).expanduser().resolve() for root in allowed_local_roots
        )
        self.allow_private_networks = bool(allow_private_networks)

    def validate_local_path(self, path: str | Path) -> Path:
        """校验 file:// URI 或裸绝对路径，返回 resolve 后的路径。"""

        text = str(path or "").strip()
        if not text:
            raise PathPolicyError("本地路径为空")
        if text.lower().startswith("file:"):
            candidate = self._path_from_file_uri(text)
        else:
            candidate = Path(text)
        if not candidate.is_absolute():
            raise PathPolicyError(f"本地路径必须是绝对路径: {text}")
        # resolve 展开符号链接与 ..，之后再次校验以阻止逃逸。
        resolved = candidate.resolve()
        self._ensure_within_roots(resolved)
        return resolved

    def validate_url(self, url: str) -> str:
        """校验远端 URL 只指向公网 http/https 地址，原样返回。"""

        text = str(url or "").strip()
        parsed = urlsplit(text)
        if parsed.scheme.lower() not in REMOTE_URL_SCHEMES:
            raise PathPolicyError(f"仅允许 http/https URL: {text}")
        hostname = parsed.hostname
        if not hostname:
            raise PathPolicyError(f"URL 缺少 host: {text}")
        self.validate_addresses(hostname, _resolve_host(hostname.rstrip(".")))
        return text

    def validate_addresses(
        self,
        host: str,
        addresses: Iterable[str | ipaddress.IPv4Address | ipaddress.IPv6Address],
    ) -> None:
        """校验实际连接候选地址，供 DNS resolver 复用同一策略。"""

        for address in addresses:
            parsed = (
                address
                if isinstance(
                    address, (ipaddress.IPv4Address, ipaddress.IPv6Address)
                )
                else ipaddress.ip_address(address)
            )
            self._ensure_address_allowed(parsed, host)

    def _path_from_file_uri(self, text: str) -> Path:
        parsed = urlsplit(text)
        if parsed.scheme.lower() != "file":
            raise PathPolicyError(f"仅支持 file:// URI: {text}")
        if parsed.query or parsed.fragment:
            raise PathPolicyError(f"file:// URI 不允许携带 query/fragment: {text}")
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        if netloc not in ("", "localhost"):
            raise PathPolicyError(f"file:// URI 不允许远端 host: {text}")
        return Path(url2pathname(parsed.path))

    def _ensure_within_roots(self, resolved: Path) -> None:
        for root in self.allowed_local_roots:
            if resolved.is_relative_to(root):
                return
        raise PathPolicyError(f"本地路径越出允许目录: {resolved}")

    def _ensure_address_allowed(
        self,
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        host: str,
    ) -> None:
        if self.allow_private_networks:
            return
        # is_private 覆盖 10/8、172.16/12、192.168/16、fc00::/7 等私网段；
        # is_loopback 含 127/8 与 ::1；is_link_local 含 169.254/16（云元数据）
        # 与 fe80::/10。
        # 100.64/10（RFC 6598 CGNAT）显式封禁：is_private 对它的判定随
        # Python 版本变化，安全检查不依赖版本差异。
        shared = ipaddress.ip_network("100.64.0.0/10")
        if (
            address in shared
            or address.is_loopback
            or address.is_link_local
            or address.is_private
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise PathPolicyError(f"禁止访问受保护网络地址: {host} -> {address}")
