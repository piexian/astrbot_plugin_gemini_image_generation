"""file_uri_to_path：file URI 按规范解析，替代历史固定切片。"""

from pathlib import Path

from tl.file_uri import file_uri_to_path


def test_standard_triple_slash_uri_keeps_absolute_root() -> None:
    # file:///tmp/result.png 必须解析为绝对路径 /tmp/result.png，
    # 而不是历史实现 source[8:] 得到的相对路径 tmp/result.png
    assert file_uri_to_path("file:///tmp/result.png") == Path("/tmp/result.png")


def test_round_trips_path_as_uri() -> None:
    target = Path.cwd() / "a b" / "图.png"
    assert file_uri_to_path(target.as_uri()) == target


def test_percent_encoded_name_is_decoded() -> None:
    resolved = file_uri_to_path("file:///tmp/my%20cat%20%E5%9B%BE.png")
    assert resolved is not None
    assert resolved.name == "my cat 图.png"


def test_localhost_host_form_is_accepted() -> None:
    assert file_uri_to_path("file://localhost/tmp/result.png") == Path(
        "/tmp/result.png"
    )


def test_non_file_uri_returns_none() -> None:
    assert file_uri_to_path("https://example.test/a.png") is None
    assert file_uri_to_path("/plain/local/path.png") is None


def test_remote_host_uri_returns_none() -> None:
    assert file_uri_to_path("file://server/share/result.png") is None
