from __future__ import annotations

import asyncio
import os
import struct
import zlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.test_web_studio_service import _config, _png
from tl.generation_tracker import GenerationTracker
from tl.plugin_config import ProviderCandidate
from tl.provider_capabilities import candidate_reference_limit, select_candidates
from tl.provider_metadata import get_provider_spec
from tl.web_studio_service import StudioServiceError, WebStudioService


@pytest.mark.asyncio
async def test_archive_protects_new_files_until_tracker_completion(tmp_path):
    tracker = GenerationTracker(tmp_path, 20)
    path = _png(tmp_path / "input.png")
    size = path.stat().st_size
    service = WebStudioService(
        None, tracker, _config(webui_gallery_max_size_mb=size / 1024**2), tmp_path
    )
    service.gallery_dir.mkdir()
    old = _png(service.gallery_dir / "old.png")
    os.utime(old, (1, 1))
    job = await tracker.begin(source="webui", prompt="draw", params={}, requester={})
    names = await service.archive_images([], [str(path)], job_id=job["job_id"])
    await service.enforce_gallery_quota()
    assert names
    assert all(service.gallery_file(name).exists() for name in names)
    assert tracker.get(job["job_id"])["images"] == names
    assert not old.exists()


@pytest.mark.asyncio
async def test_gallery_quota_counts_and_removes_thumbnails(tmp_path):
    tracker = GenerationTracker(tmp_path, 20)
    service = WebStudioService(None, tracker, _config(), tmp_path)
    service.gallery_dir.mkdir()
    source = _png(service.gallery_dir / "old.png")
    await service.gallery_image_base64(source.name, thumbnail=True)
    cache = service.gallery_dir / ".thumbs" / "old.png.jpg"
    assert cache.is_file()
    service.config.webui_gallery_max_size_mb = source.stat().st_size / 1024**2
    await service.enforce_gallery_quota()
    assert source.is_file()
    assert not cache.exists()


@pytest.mark.asyncio
async def test_deleting_job_removes_thumbnail(tmp_path):
    tracker = GenerationTracker(tmp_path, 20)
    service = WebStudioService(None, tracker, _config(), tmp_path)
    service.gallery_dir.mkdir()
    source = _png(service.gallery_dir / "delete.png")
    await service.gallery_image_base64(source.name, thumbnail=True)
    job = await tracker.begin(source="webui", prompt="draw", params={}, requester={})
    await tracker.complete(
        job["job_id"], image_files=[source.name], text_content=None, stats={}
    )
    await tracker.delete([job["job_id"]])
    assert not source.exists()
    assert not (service.gallery_dir / ".thumbs" / "delete.png.jpg").exists()


@pytest.mark.asyncio
async def test_history_disabled_keeps_live_updates_without_disk_io(tmp_path):
    tracker = GenerationTracker(tmp_path, 20, enabled=False)
    queue = tracker.subscribe()
    job = await tracker.begin(source="webui", prompt="draw", params={}, requester={})
    await tracker.update(job["job_id"], generated_images=1)
    await tracker.complete(
        job["job_id"], image_files=["one.png"], text_content=None, stats={}
    )
    assert [queue.get_nowait()["data"]["status"] for _ in range(3)] == [
        "running",
        "running",
        "succeeded",
    ]
    assert tracker.active_and_recent()[0]["generated_images"] == 1
    await tracker.close()
    assert not tracker.path.exists()


def test_candidate_id_filters_duplicate_provider_model_entries():
    candidates = [
        ProviderCandidate(
            id=f"google#{index}", api_type="google", settings={"model": "same"}
        )
        for index in (1, 2)
    ]
    assert select_candidates(candidates, candidate_id="google#2") == [candidates[1]]
    assert select_candidates(candidates, candidate_id="missing") == []


def test_capabilities_expose_effective_limits(tmp_path):
    service = WebStudioService(
        None,
        GenerationTracker(tmp_path, 20),
        _config(webui_batch_total_budget=12, webui_upload_max_mb=3, batch_max_tasks=2),
        tmp_path,
    )
    payload = service.capabilities()
    assert payload["limits"] == {
        "batch_total_budget": 12,
        "upload_max_mb": 3,
        "batch_max_tasks": 2,
    }
    assert payload["models"][0]["candidate_id"] == "google#1"
    assert payload["models"][0]["max_reference_images"] == 6


@pytest.mark.asyncio
async def test_upload_rejects_large_dimensions_before_decoding(tmp_path, monkeypatch):
    import tl.web_studio_service as module

    data = bytearray(_png(tmp_path / "large.png").read_bytes())
    data[16:24] = struct.pack(">II", 8001, 1)
    data[29:33] = struct.pack(">I", zlib.crc32(data[12:29]))

    def fail_decode(*args):
        pytest.fail("oversized image reached pixel decoder")

    monkeypatch.setattr(module.cv2, "imdecode", fail_decode)
    with pytest.raises(StudioServiceError, match="8000"):
        WebStudioService._validate_image_bytes(bytes(data))


@pytest.mark.parametrize(
    ("api_type", "model", "configured", "expected"),
    [
        ("google", "gemini", 30, 14),
        ("gemini_interactions", "gemini", 30, 14),
        ("openai", "model", 20, 6),
        ("xai", "grok-imagine-image", 10, 3),
        ("xai", "grok-imagine-image", 2, 2),
        ("doubao", "doubao-seedream-4-0", 20, 14),
        ("doubao", "doubao-seedream-5-0-pro", 20, 10),
        ("minimax", "image-01", 20, 9),
        ("stepfun", "step-image-edit-2", 20, 1),
        ("openai_images", "dall-e-2", 20, 1),
        ("openai_images", "gpt-image-1", 7, 7),
        ("dashscope", "qwen-image-3.0", 9, 3),
        ("dashscope", "qwen-image-edit-plus", 12, 9),
        ("sensenova", "sensenova-u1.5-lite", 10, 4),
        ("modelscope", "Qwen/Qwen-Image-Edit", 2, 2),
        ("siliconflow", "Qwen/Qwen-Image-Edit", 10, 1),
        ("siliconflow", "Qwen/Qwen-Image-Edit-2509", 10, 3),
        ("siliconflow", "Kwai-Kolors/Kolors", 10, 1),
        ("google", "gemini", 0, 0),
    ],
)
def test_reference_limits_match_provider_truncation(
    api_type, model, configured, expected
):
    candidate = ProviderCandidate(
        id="candidate",
        api_type=api_type,
        settings={
            get_provider_spec(api_type).model_field: model,
            "max_reference_images": configured,
        },
        supports_image_edit=True,
    )
    assert candidate_reference_limit(candidate) == expected
    assert candidate_reference_limit(replace(candidate, supports_image_edit=False)) == 0


@pytest.mark.asyncio
async def test_quota_cannot_evict_running_group_or_keep_oversized_batch(tmp_path):
    tracker = GenerationTracker(tmp_path, 20)
    path = _png(tmp_path / "input.png")
    service = WebStudioService(
        None,
        tracker,
        _config(webui_gallery_max_size_mb=path.stat().st_size / 1024**2),
        tmp_path,
    )
    parent = await tracker.begin(
        source="webui", prompt="parent", params={}, requester={}
    )
    child = await tracker.begin(
        source="webui",
        prompt="child",
        params={},
        requester={},
        parent_job_id=parent["job_id"],
    )
    names = await service.archive_images([], [str(path)], job_id=child["job_id"])
    await tracker.complete(
        child["job_id"], image_files=names, text_content=None, stats={}
    )
    with pytest.raises(StudioServiceError) as error:
        await service.archive_images([], [str(path)])
    assert error.value.status_code == 507
    assert [file.name for file in service.gallery_dir.iterdir()] == names


@pytest.mark.asyncio
async def test_orphan_thumbnail_cleanup_and_cache_symlink_rejection(tmp_path):
    service = WebStudioService(
        None, GenerationTracker(tmp_path, 20), _config(), tmp_path
    )
    cache = service.gallery_dir / ".thumbs"
    cache.mkdir(parents=True)
    orphan = cache / "missing.png.jpg"
    orphan.write_bytes(b"old-cache")
    await service.enforce_gallery_quota()
    assert not orphan.exists()
    source = _png(service.gallery_dir / "source.png")
    external = tmp_path / "private.txt"
    external.write_text("sensitive")
    (cache / "source.png.jpg").symlink_to(external)
    with pytest.raises(StudioServiceError):
        await service.gallery_image_base64(source.name, thumbnail=True)
    assert external.read_text() == "sensitive"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate_proxy", ["http://candidate-proxy:8080", "socks5://candidate-proxy:1080"]
)
async def test_proxy_for_archived_result_uses_successful_candidate(
    tmp_path, monkeypatch, candidate_proxy
):
    from aiohttp_socks import ProxyConnector

    import tl.web_studio_service as module

    content = _png(tmp_path / "input.png").read_bytes()
    observed = []
    connectors = []
    connector = object()
    monkeypatch.setattr(ProxyConnector, "from_url", lambda proxy: connector)

    class Response:
        content_length = None
        content = SimpleNamespace()

        def raise_for_status(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    async def chunks(size):
        yield content

    Response.content.iter_chunked = chunks

    class Session:
        def __init__(self, **kwargs):
            connectors.append(kwargs["connector"])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, url, **kwargs):
            observed.append(kwargs["proxy"])
            return Response()

    monkeypatch.setattr(module.aiohttp, "ClientSession", Session)
    config = _config(
        provider_candidates=[
            ProviderCandidate(
                id="chosen",
                api_type="google",
                settings={"proxy": candidate_proxy},
            )
        ]
    )
    service = WebStudioService(
        SimpleNamespace(_default_proxy="http://default-proxy:8080"),
        GenerationTracker(tmp_path, 20),
        config,
        tmp_path,
    )
    assert (
        await service._download_remote_image(
            "https://example.test/img", candidate_id="chosen"
        )
        == content
    )
    assert observed == [
        None if candidate_proxy.startswith("socks") else candidate_proxy
    ]
    assert connectors == [connector if candidate_proxy.startswith("socks") else None]
    await service._download_remote_image("https://example.test/img")
    assert observed[-1] == "http://default-proxy:8080"


@pytest.mark.asyncio
async def test_studio_pins_duplicate_candidate_through_runtime(tmp_path, monkeypatch):
    from tl.tl_api import GeminiAPIClient

    candidates = [
        ProviderCandidate(
            id=f"google#{index}",
            api_type="google",
            settings={
                "api_keys": [f"key-{index}"],
                "model": "same",
                "max_reference_images": index,
            },
            supports_image_edit=True,
        )
        for index in (1, 2)
    ]
    client = GeminiAPIClient(api_keys=["key"])
    client.provider_candidates = candidates
    path = _png(tmp_path / "result.png")
    seen = []

    async def generate(config, **kwargs):
        seen.append(config.candidate_id)
        return [], [str(path)], None, None

    monkeypatch.setattr(client, "_generate_image_single", generate)
    tracker = GenerationTracker(tmp_path, 20)
    service = WebStudioService(
        client, tracker, _config(provider_candidates=candidates), tmp_path
    )
    payload = {
        "prompt": "draw",
        "provider": "google",
        "model": "same",
        "candidate_id": "google#2",
    }
    accepted = await service.generate(payload)
    await service._runtime_tasks[accepted["job_id"]]
    assert seen == ["google#2"]
    assert tracker.get(accepted["job_id"])["params"]["candidate_id"] == "google#2"
    with pytest.raises(StudioServiceError):
        service.validate_payload({**payload, "candidate_id": "missing"})
    service.upload_dir.mkdir()
    _png(service.upload_dir / "ref.png")
    with pytest.raises(StudioServiceError, match="参考图片"):
        service.validate_payload(
            {
                **payload,
                "candidate_id": "google#1",
                "upload_names": ["ref.png", "ref.png"],
            }
        )


@pytest.mark.asyncio
async def test_upload_quota_serializes_concurrent_writes(tmp_path, monkeypatch):
    import tl.web_studio_service as module

    data = _png(tmp_path / "input.png").read_bytes()
    service = WebStudioService(
        None, GenerationTracker(tmp_path, 20), _config(), tmp_path
    )
    monkeypatch.setattr(module, "_UPLOAD_QUOTA_BYTES", len(data))
    entered = asyncio.Event()
    release = asyncio.Event()

    class Upload:
        def __init__(self, block=False):
            self.sent = False
            self.block = block
            self.closed = False

        async def read(self, size):
            if self.sent:
                return b""
            self.sent = True
            if self.block:
                entered.set()
                await release.wait()
            return data

        async def close(self):
            self.closed = True

    first, second = Upload(True), Upload()
    task1 = asyncio.create_task(service.save_uploads([first]))
    await entered.wait()
    task2 = asyncio.create_task(service.save_uploads([second]))
    await asyncio.sleep(0)
    assert not second.sent
    release.set()
    names1, names2 = await asyncio.gather(task1, task2)
    assert first.closed and second.closed
    assert not (service.upload_dir / names1[0]).exists()
    assert (service.upload_dir / names2[0]).is_file()


@pytest.mark.asyncio
async def test_shutdown_interrupts_job_during_archival(tmp_path, monkeypatch):
    from tests.test_web_studio_service import _SequenceClient

    path = _png(tmp_path / "result.png")
    tracker = GenerationTracker(tmp_path, 20)
    service = WebStudioService(
        _SequenceClient([([], [str(path)], None, None)]), tracker, _config(), tmp_path
    )
    started = asyncio.Event()

    async def archive(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "archive_sources", archive)
    accepted = await service.generate({"prompt": "draw"})
    await started.wait()
    await service.close()
    assert tracker.get(accepted["job_id"])["status"] == "interrupted"
