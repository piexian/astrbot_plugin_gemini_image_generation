from __future__ import annotations

import ipaddress
import socket
import urllib.parse

import pytest

from tl.core import path_policy
from tl.core.errors import PathPolicyError
from tl.core.path_policy import PathPolicy


def _policy(tmp_path, **kwargs) -> PathPolicy:
    return PathPolicy((tmp_path / "allowed",), **kwargs)


def _make_tree(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    return root


# ---- 本地路径 ----


def test_bare_path_inside_root(tmp_path) -> None:
    root = _make_tree(tmp_path)
    target = root / "ref.png"
    target.write_bytes(b"png")
    policy = _policy(tmp_path)
    assert policy.validate_local_path(str(target)) == target.resolve()
    assert policy.validate_local_path(target) == target.resolve()


def test_file_uri_inside_root(tmp_path) -> None:
    root = _make_tree(tmp_path)
    target = root / "ref.png"
    target.write_bytes(b"png")
    assert _policy(tmp_path).validate_local_path(target.as_uri()) == target.resolve()


def test_percent_encoded_file_uri(tmp_path) -> None:
    root = _make_tree(tmp_path)
    target = root / "a b.png"
    target.write_bytes(b"png")
    quoted = urllib.parse.quote(str(target))
    assert _policy(tmp_path).validate_local_path(f"file://{quoted}") == target.resolve()


def test_root_itself_allowed(tmp_path) -> None:
    root = _make_tree(tmp_path)
    assert _policy(tmp_path).validate_local_path(root) == root.resolve()


def test_relative_path_rejected(tmp_path) -> None:
    with pytest.raises(PathPolicyError):
        _policy(tmp_path).validate_local_path("allowed/ref.png")


def test_empty_path_rejected(tmp_path) -> None:
    with pytest.raises(PathPolicyError):
        _policy(tmp_path).validate_local_path("")


def test_path_outside_root_rejected(tmp_path) -> None:
    outside = tmp_path / "outside" / "ref.png"
    outside.parent.mkdir()
    outside.write_bytes(b"x")
    with pytest.raises(PathPolicyError):
        _policy(tmp_path).validate_local_path(outside)


def test_dotdot_traversal_rejected(tmp_path) -> None:
    root = _make_tree(tmp_path)
    traversing = root / ".." / "outside.png"
    with pytest.raises(PathPolicyError):
        _policy(tmp_path).validate_local_path(traversing)


def test_symlink_escape_rejected(tmp_path) -> None:
    root = _make_tree(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    link = root / "escape.png"
    link.symlink_to(outside)
    with pytest.raises(PathPolicyError):
        _policy(tmp_path).validate_local_path(link)


def test_symlink_within_root_allowed(tmp_path) -> None:
    root = _make_tree(tmp_path)
    inner = root / "real.png"
    inner.write_bytes(b"x")
    link = root / "alias.png"
    link.symlink_to(inner)
    assert _policy(tmp_path).validate_local_path(link) == inner.resolve()


def test_file_uri_remote_host_rejected(tmp_path) -> None:
    with pytest.raises(PathPolicyError):
        _policy(tmp_path).validate_local_path("file://evil.example/etc/passwd")


def test_file_uri_with_query_rejected(tmp_path) -> None:
    with pytest.raises(PathPolicyError):
        _policy(tmp_path).validate_local_path("file:///tmp/a.png?x=1")


# ---- 远端 URL ----


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/img.png",
        "http://[::1]/img.png",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.1.2.3/img.png",
        "http://172.16.0.9/img.png",
        "http://192.168.1.1/img.png",
        "http://[fe80::1]/img.png",
        "http://[fc00::1]/img.png",
        "http://0.0.0.0/img.png",
        "http://224.0.0.1/img.png",
        "http://240.0.0.1/img.png",
        "http://100.64.1.1/img.png",
    ],
)
def test_protected_ip_literals_rejected(url) -> None:
    with pytest.raises(PathPolicyError):
        PathPolicy(()).validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://93.184.216.34/img.png",
        "https://[2606:4700::1111]/img.png",
        "HTTP://93.184.216.34/img.png",
    ],
)
def test_public_ip_literals_allowed(url) -> None:
    assert PathPolicy(()).validate_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "ftp://93.184.216.34/img.png",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http:///img.png",
    ],
)
def test_url_scheme_and_host_rejected(url) -> None:
    with pytest.raises(PathPolicyError):
        PathPolicy(()).validate_url(url)


def test_domain_resolving_public_allowed(monkeypatch) -> None:
    monkeypatch.setattr(
        path_policy,
        "_resolve_host",
        lambda host: (ipaddress.ip_address("93.184.216.34"),),
    )
    url = "https://example.com/img.png"
    assert PathPolicy(()).validate_url(url) == url


def test_domain_resolving_private_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        path_policy,
        "_resolve_host",
        lambda host: (ipaddress.ip_address("10.0.0.7"),),
    )
    with pytest.raises(PathPolicyError):
        PathPolicy(()).validate_url("https://example.com/img.png")


def test_domain_mixed_addresses_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        path_policy,
        "_resolve_host",
        lambda host: (
            ipaddress.ip_address("93.184.216.34"),
            ipaddress.ip_address("192.168.0.1"),
        ),
    )
    with pytest.raises(PathPolicyError):
        PathPolicy(()).validate_url("https://example.com/img.png")


def test_domain_dns_failure_rejected(monkeypatch) -> None:
    def _fail(*args, **kwargs):
        raise socket.gaierror("resolve failed")

    monkeypatch.setattr(path_policy.socket, "getaddrinfo", _fail)
    with pytest.raises(PathPolicyError):
        PathPolicy(()).validate_url("https://example.com/img.png")


def test_allow_private_networks_permits_loopback_and_private() -> None:
    policy = PathPolicy((), allow_private_networks=True)
    assert policy.validate_url("http://127.0.0.1:8300/img.png") == (
        "http://127.0.0.1:8300/img.png"
    )
    assert policy.validate_url("http://10.0.0.5/img.png") == "http://10.0.0.5/img.png"
