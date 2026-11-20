"""关键词绑定素材：摄入、gen 记录注册与注入解析。"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from tl.keyword_media import KeywordMediaService, media_job_id

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_REL = "files/keyword_trigger/bindings/templates/binding/ref_files/a.png"


def _validate(data: bytes) -> str:
    if not data.startswith(b"\x89PNG"):
        raise ValueError("不支持的图片格式")
    return "png"


class _Library:
    def __init__(self, tmp_path):
        self.dir = tmp_path / "library"
        self.dir.mkdir()

    async def add_bytes(self, data, *, label, source="upload", **kwargs):
        name = f"lib-{hashlib.sha256(data).hexdigest()[:12]}.png"
        (self.dir / name).write_bytes(data)
        return {"file": name}

    def file_path(self, name):
        return self.dir / name


class _Tracker:
    def __init__(self):
        self.records = {}
        self.enabled = True

    def get(self, job_id):
        return self.records.get(job_id)

    async def begin(self, *, source, prompt, params, requester, job_id=None, **kwargs):
        record = {
            "job_id": job_id,
            "source": source,
            "status": "running",
            "images": [],
            "shared": False,
            "requester": requester,
        }
        self.records[job_id] = record
        return record

    async def complete(
        self,
        job_id,
        *,
        image_files,
        text_content,
        stats,
        status="succeeded",
        source_urls=None,
    ):
        record = self.records[job_id]
        record["images"] = list(image_files)
        record["status"] = status
        record["source_urls"] = list(source_urls or [])

    async def update(self, job_id, **changes):
        self.records[job_id].update(changes)

    async def set_shared(self, job_id, shared, *, user_id, umo):
        self.records[job_id]["shared"] = shared


def _service(tmp_path):
    files_root = tmp_path / "files"
    (files_root / "keyword_trigger/bindings/templates/binding/ref_files").mkdir(
        parents=True
    )
    return KeywordMediaService(
        library=_Library(tmp_path),
        tracker=_Tracker(),
        data_dir=tmp_path,
        files_root=files_root,
        validate_fn=_validate,
    )


def _write_upload(service):
    upload = service.data_dir / _REL
    upload.write_bytes(_PNG)
    return upload


@pytest.mark.asyncio
async def test_upload_ingested_into_library_and_registered_as_shared_gen(tmp_path):
    service = _service(tmp_path)
    _write_upload(service)
    bindings = [{"words": ["水彩"], "ref_files": [_REL]}]
    await service.ensure_registered(bindings)
    job_id = media_job_id(_REL)
    record = service.tracker.get(job_id)
    assert record is not None and record["shared"] is True
    assert record["source"] == "keyword"
    library_file = record["images"][0]
    assert (service.library.dir / library_file).read_bytes() == _PNG
    assert service.protected_names() == {library_file}
    state = json.loads((tmp_path / "keyword_media.json").read_text(encoding="utf-8"))
    assert state[_REL]["job_id"] == job_id
    await service.ensure_registered(bindings)
    assert len(service.tracker.records) == 1


@pytest.mark.asyncio
async def test_local_address_ingested_and_resolved_to_library_copy(tmp_path):
    service = _service(tmp_path)
    local = tmp_path / "local.png"
    local.write_bytes(_PNG)
    await service.ensure_registered(
        [{"words": ["水彩"], "ref_addresses": [str(local)]}]
    )
    job_id = media_job_id(str(local))
    record = service.tracker.get(job_id)
    assert record is not None and record["shared"] is True
    resolved = service.resolve_address(str(local))
    assert resolved == {
        "label": record["images"][0],
        "path": str(service.library.dir / record["images"][0]),
    }


@pytest.mark.asyncio
async def test_url_address_cached_and_injected_as_url(tmp_path, monkeypatch):
    service = _service(tmp_path)
    url = "https://example.com/a.png"

    async def fake_fetch(u, **kwargs):
        assert u == url
        return _PNG

    monkeypatch.setattr(service._fetcher, "fetch", fake_fetch)
    import tl.keyword_media as mod

    monkeypatch.setattr(mod, "REFERENCE_IMAGE_CACHE_DIR", tmp_path / "cache")
    await service.ensure_registered([{"words": ["水彩"], "ref_addresses": [url]}])
    assert service.resolve_address(url) == {"label": "a.png", "path": url}
    assert list((tmp_path / "cache").glob("*.png"))
    record = service.tracker.get(media_job_id(url))
    assert record["source_urls"] == [url]

    async def fail_fetch(u, **kwargs):
        raise AssertionError("已注册的 URL 不应重复抓取")

    monkeypatch.setattr(service._fetcher, "fetch", fail_fetch)
    await service.ensure_registered([{"words": ["水彩"], "ref_addresses": [url]}])


@pytest.mark.asyncio
async def test_evicted_record_rebuilt_with_same_handle(tmp_path):
    service = _service(tmp_path)
    _write_upload(service)
    bindings = [{"words": ["水彩"], "ref_files": [_REL]}]
    await service.ensure_registered(bindings)
    job_id = media_job_id(_REL)
    del service.tracker.records[job_id]
    await service.ensure_registered(bindings)
    record = service.tracker.get(job_id)
    assert record is not None and record["shared"] is True


@pytest.mark.asyncio
async def test_missing_upload_reported_without_failure(tmp_path):
    service = _service(tmp_path)
    await service.ensure_registered([{"words": ["水彩"], "ref_files": [_REL]}])
    assert service.tracker.records == {}
    assert service.resolve_address("/data/missing.png") is None
