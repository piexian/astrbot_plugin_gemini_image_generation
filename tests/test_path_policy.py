from __future__ import annotations

import urllib.parse

import pytest

from tl.core.errors import PathPolicyError
from tl.core.path_policy import PathPolicy, check_remote_url


def _policy(tmp_path) -> PathPolicy:
    return PathPolicy((tmp_path / "allowed",))


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
        "ftp://93.184.216.34/img.png",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http:///img.png",
        "https://user:pass@example.com/a.png",
    ],
)
def test_url_scheme_host_and_userinfo_rejected(url) -> None:
    with pytest.raises(PathPolicyError):
        check_remote_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://93.184.216.34/img.png",
        "https://[2606:4700::1111]/img.png",
        "HTTP://93.184.216.34/img.png",
        "https://example.com/img.png?token=secret",
    ],
)
def test_public_urls_allowed(url) -> None:
    assert check_remote_url(url) == url


def test_remote_url_is_stripped() -> None:
    assert check_remote_url("  https://example.com/a.png  ") == (
        "https://example.com/a.png"
    )
