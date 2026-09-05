from __future__ import annotations

import asyncio
import base64
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from tl.api_types import ApiRequestConfig
from tl.generation_tracker import GenerationTracker
from tl.plugin_config import ConfigLoader, ProviderCandidate
from tl.web_studio_service import StudioServiceError, WebStudioService


def _config(**overrides):
    values = {
        "webui_gallery_max_size_mb": 512,
        "webui_upload_max_mb": 20,
        "webui_max_concurrent_jobs": 2,
        "webui_batch_total_budget": 40,
        "batch_max_tasks": 20,
        "batch_concurrency": 3,
        "max_attempts_per_key": 3,
        "total_timeout": 120,
        "provider_candidates": [
            ProviderCandidate(
                id="google#1",
                api_type="google",
                settings={"api_keys": ["key"], "model": "image-model"},
                supports_image_edit=True,
            )
        ],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _png(path: Path, value: int = 0) -> Path:
    image = np.full((4, 4, 3), value, dtype=np.uint8)
    assert cv2.imwrite(str(path), image)
    return path


def test_webui_config_values_are_clamped() -> None:
    config = ConfigLoader(
        {
            "webui": {
                "history_enabled": "false",
                "history_max_records": 1,
                "gallery_max_size_mb": 0,
                "upload_max_mb": 1000,
                "max_concurrent_jobs": 0,
                "batch_total_budget": 999,
            }
        }
    ).load()

    assert config.webui_history_enabled is False
    assert config.webui_history_max_records == 50
    assert config.webui_gallery_max_size_mb == 0
    assert config.webui_upload_max_mb == 64
    assert config.webui_max_concurrent_jobs == 1
    assert config.webui_batch_total_budget == 200


@pytest.mark.parametrize("value", [True, 1.0, "1.0", "2e0"])
def test_image_count_rejects_non_integer_representations(tmp_path, value) -> None:
    service = WebStudioService(
        None, GenerationTracker(tmp_path, 20), _config(), tmp_path
    )

    with pytest.raises(StudioServiceError, match="image_count"):
        service.validate_payload({"prompt": "draw", "image_count": value})


async def _wait_terminal(tracker: GenerationTracker, job_id: str) -> dict:
    for _ in range(200):
        record = tracker.get(job_id)
        if record and record["status"] != "running":
            return record
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish")


async def _inline_to_thread(function, /, *args, **kwargs):
    return function(*args, **kwargs)


class _SequenceClient:
    def __init__(self, results) -> None:
        self.results = list(results)
        self.counts = []
        self.seeds = []
        self.provider_settings = []

    async def generate_image(self, config, **kwargs):
        self.counts.append(config.image_count)
        self.seeds.append(config.seed)
        self.provider_settings.append(config.provider_settings)
        return self.results.pop(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate_refs", [False, True])
async def test_single_generation_loops_until_target(
    tmp_path, monkeypatch, duplicate_refs
) -> None:
    import tl.web_studio_service as studio_module

    messages: list[str] = []
    monkeypatch.setattr(
        studio_module,
        "logger",
        SimpleNamespace(
            debug=messages.append,
            info=messages.append,
            warning=messages.append,
            error=messages.append,
        ),
    )
    paths = [_png(tmp_path / f"{index}.png", index) for index in range(3)]
    client = _SequenceClient(
        [
            (
                [str(paths[0]), str(path), str(path)] if duplicate_refs else [],
                [str(path)],
                None,
                None,
            )
            for path in paths
        ]
    )
    tracker = GenerationTracker(tmp_path, 20)
    service = WebStudioService(client, tracker, _config(), tmp_path)

    accepted = await service.generate({"prompt": "draw", "image_count": 3})
    runtime_task = service._runtime_tasks[accepted["job_id"]]
    await runtime_task
    record = tracker.get(accepted["job_id"])

    assert client.counts == [3, 2, 1]
    assert record["status"] == "succeeded"
    assert record["generated_images"] == 3
    assert all((tmp_path / "gallery" / name).is_file() for name in record["images"])
    joined = "\n".join(messages)
    assert f"工作台任务已受理: job_id={accepted['job_id']}" in joined
    assert "目标张数=3, 批量条目数=0" in joined
    assert f"工作台任务完成: job_id={accepted['job_id']}" in joined
    assert "产出张数=3, 供应商=" in joined
    assert "gallery 归档完成: 张数=3" in joined


@pytest.mark.asyncio
async def test_generation_with_some_images_is_partial_success(tmp_path) -> None:
    path = _png(tmp_path / "one.png")
    client = _SequenceClient([([], [str(path)], "first", None), ([], [], None, None)])
    tracker = GenerationTracker(tmp_path, 20)
    service = WebStudioService(client, tracker, _config(), tmp_path)

    accepted = await service.generate({"prompt": "draw", "image_count": 2})
    record = await _wait_terminal(tracker, accepted["job_id"])

    assert record["status"] == "partial_success"
    assert record["generated_images"] == 1


@pytest.mark.asyncio
async def test_concurrency_limit_rejects_without_queueing(tmp_path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingClient:
        async def generate_image(self, config, **kwargs):
            started.set()
            await release.wait()
            return [], [], None, None

    tracker = GenerationTracker(tmp_path, 20)
    service = WebStudioService(
        BlockingClient(),
        tracker,
        _config(webui_max_concurrent_jobs=1),
        tmp_path,
    )
    await service.generate({"prompt": "first"})
    await started.wait()

    with pytest.raises(StudioServiceError) as exc_info:
        await service.generate({"prompt": "second"})
    assert exc_info.value.status_code == 429
    release.set()
    await service.close()


@pytest.mark.asyncio
async def test_batch_budget_is_validated_before_start(tmp_path) -> None:
    tracker = GenerationTracker(tmp_path, 20)
    service = WebStudioService(
        _SequenceClient([]),
        tracker,
        _config(webui_batch_total_budget=4),
        tmp_path,
    )

    with pytest.raises(StudioServiceError) as exc_info:
        await service.generate(
            {
                "batch": [
                    {"name": "one", "prompt": "1", "image_count": 3},
                    {"name": "two", "prompt": "2", "image_count": 2},
                ]
            }
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_archive_supports_local_and_remote_sources(tmp_path, monkeypatch) -> None:
    local = _png(tmp_path / "local.png", 1)
    ok, encoded = cv2.imencode(".png", np.ones((3, 3, 3), dtype=np.uint8))
    assert ok
    tracker = GenerationTracker(tmp_path, 20)
    service = WebStudioService(None, tracker, _config(), tmp_path)

    async def fake_download(url: str, *, candidate_id=None) -> bytes:
        return encoded.tobytes()

    monkeypatch.setattr(service, "_download_remote_image", fake_download)
    names = await service.archive_images(
        ["https://example.test/image.png"], [str(local)]
    )

    assert len(names) == 2
    assert all((tmp_path / "gallery" / name).is_file() for name in names)


@pytest.mark.asyncio
async def test_terminate_cancels_runtime_jobs(tmp_path) -> None:
    started = asyncio.Event()

    class BlockingClient:
        async def generate_image(self, config, **kwargs):
            started.set()
            await asyncio.Event().wait()

    tracker = GenerationTracker(tmp_path, 20)
    service = WebStudioService(BlockingClient(), tracker, _config(), tmp_path)
    accepted = await service.generate({"prompt": "draw"})
    await started.wait()

    await service.close()

    assert tracker.get(accepted["job_id"])["status"] == "interrupted"


@pytest.mark.asyncio
async def test_upload_is_chunked_and_validated(tmp_path, monkeypatch) -> None:
    path = _png(tmp_path / "upload-source.png")
    data = path.read_bytes()

    class Upload:
        def __init__(self) -> None:
            self.offset = 0
            self.read_sizes = []
            self.closed = False
            self.filename = "客户端原名-secret.png"

        async def read(self, size: int = -1) -> bytes:
            self.read_sizes.append(size)
            chunk = data[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

        async def close(self) -> None:
            self.closed = True

    import tl.web_studio_service as studio_module

    messages: list[str] = []
    monkeypatch.setattr(
        studio_module,
        "logger",
        SimpleNamespace(debug=messages.append, info=messages.append),
    )
    upload = Upload()
    service = WebStudioService(
        None, GenerationTracker(tmp_path, 20), _config(), tmp_path
    )

    names = await service.save_uploads([upload])

    assert len(names) == 1
    assert upload.closed is True
    assert all(size > 0 for size in upload.read_sizes)
    assert (tmp_path / "webui_uploads" / names[0]).is_file()
    joined = "\n".join(messages)
    assert "上传文件校验通过" in joined
    assert names[0] in joined
    assert "客户端原名-secret.png" not in joined
    assert f"文件数=1, 总大小={len(data)} 字节" in joined


def test_capabilities_returns_flat_candidate_models(tmp_path) -> None:
    """同 api_type 的多条候选不去重，且字段为白名单。"""
    config = _config(
        provider_candidates=[
            ProviderCandidate(
                id="google#1",
                api_type="google",
                settings={"api_keys": ["k1"], "model": "model-a"},
                supports_image_edit=True,
                model_alias="别名A",
            ),
            ProviderCandidate(
                id="google#2",
                api_type="google",
                settings={"api_keys": ["k2"], "model": "model-b"},
                supports_image_edit=True,
            ),
        ]
    )
    service = WebStudioService(None, GenerationTracker(tmp_path, 20), config, tmp_path)

    payload = service.capabilities()

    models = payload["models"]
    assert len(models) == 2
    assert {entry["model"] for entry in models} == {"model-a", "model-b"}
    assert models[0]["model_alias"] == "别名A"
    assert models[1]["model_alias"] is None
    for entry in models:
        assert set(entry) == {
            "id",
            "candidate_id",
            "max_reference_images",
            "native_max_reference_images",
            "generation_fields",
            "provider",
            "provider_display",
            "model",
            "model_alias",
            "resolutions",
            "aspect_ratios",
            "parameters",
        }
        assert "api_keys" not in str(entry)


def test_capabilities_and_validation_expose_seed(tmp_path) -> None:
    candidate = ProviderCandidate(
        id="modelscope#1",
        api_type="modelscope",
        settings={"api_keys": ["key"], "model": "Qwen/Qwen-Image"},
    )
    service = WebStudioService(
        None,
        GenerationTracker(tmp_path, 20),
        _config(provider_candidates=[candidate]),
        tmp_path,
    )

    descriptor = service.capabilities()["models"][0]["parameters"]["seed"]
    normalized, warning = service.validate_payload(
        {
            "prompt": "draw",
            "provider": "modelscope",
            "model": "Qwen/Qwen-Image",
            "seed": 42,
        }
    )

    assert descriptor == {"type": "integer"}
    assert normalized["seed"] == 42
    assert warning is None


@pytest.mark.parametrize("seed", [True, 1.5, "42", [], {}])
def test_seed_rejects_non_integer_values(tmp_path, seed) -> None:
    candidate = ProviderCandidate(
        id="modelscope#1",
        api_type="modelscope",
        settings={"api_keys": ["key"], "model": "Qwen/Qwen-Image"},
    )
    service = WebStudioService(
        None,
        GenerationTracker(tmp_path, 20),
        _config(provider_candidates=[candidate]),
        tmp_path,
    )

    with pytest.raises(StudioServiceError, match="seed 必须是整数"):
        service.validate_payload({"prompt": "draw", "seed": seed})


@pytest.mark.asyncio
async def test_generation_passes_seed_to_request_settings(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(asyncio, "to_thread", _inline_to_thread)
    path = _png(tmp_path / "seed.png")
    client = _SequenceClient([([], [str(path)], None, None)])
    candidate = ProviderCandidate(
        id="modelscope#1",
        api_type="modelscope",
        settings={"api_keys": ["key"], "model": "Qwen/Qwen-Image"},
    )
    tracker = GenerationTracker(tmp_path, 20)
    service = WebStudioService(
        client,
        tracker,
        _config(provider_candidates=[candidate]),
        tmp_path,
    )

    accepted = await service.generate(
        {
            "prompt": "draw",
            "provider": "modelscope",
            "model": "Qwen/Qwen-Image",
            "seed": 42,
            "negative_prompt": "blurry",
        }
    )
    record = await _wait_terminal(tracker, accepted["job_id"])

    assert client.seeds == [42]
    assert client.provider_settings == [{"seed": 42}]
    assert record["params"]["seed"] == 42
    assert record["params"]["negative_prompt"] == "blurry"
    await service.close()
    await tracker.close()
    restored = GenerationTracker(tmp_path, 20)
    restored_service = WebStudioService(None, restored, service.config, tmp_path)
    reused, warning = restored_service.validate_payload(
        {"reuse_job_id": accepted["job_id"]}
    )
    assert warning is None
    assert reused["seed"] == 42
    assert reused["negative_prompt"] == "blurry"
    await restored.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("supports_advanced", [False, True])
async def test_reuse_missing_route_keeps_only_supported_inherited_parameters(
    tmp_path, supports_advanced
) -> None:
    tracker = GenerationTracker(tmp_path, 20)
    record = await tracker.begin(
        source="webui",
        prompt="draw",
        requester={},
        params={
            "provider": "modelscope",
            "model": "Qwen/Qwen-Image",
            "candidate_id": "removed",
            "seed": 42,
            "negative_prompt": "blurry",
        },
    )
    await tracker.complete(
        record["job_id"], image_files=[], text_content=None, stats={}
    )
    config = _config()
    if supports_advanced:
        config.provider_candidates = [
            ProviderCandidate(
                id="replacement",
                api_type="modelscope",
                settings={"model": "Qwen/Qwen-Image", "api_keys": ["test"]},
            )
        ]
    service = WebStudioService(None, tracker, config, tmp_path)
    reused, warning = service.validate_payload({"reuse_job_id": record["job_id"]})
    assert warning == "原记录的供应商或模型已不可用，已回退默认配置"
    assert reused["candidate_id"] == config.provider_candidates[0].id
    assert reused["seed"] == (42 if supports_advanced else None)
    assert reused["negative_prompt"] == ("blurry" if supports_advanced else None)
    if not supports_advanced:
        for explicit in ({"seed": 42}, {"negative_prompt": "explicit"}):
            with pytest.raises(StudioServiceError, match="没有匹配本次请求能力"):
                service.validate_payload({"reuse_job_id": record["job_id"], **explicit})
    await tracker.close()


def test_seed_overrides_candidate_provider_settings_after_replace() -> None:
    request = ApiRequestConfig(model="model", prompt="draw", seed=42)

    candidate_request = replace(request, provider_settings={"seed": 7})

    assert candidate_request.provider_settings == {"seed": 42}


@pytest.mark.asyncio
async def test_gallery_image_base64_builds_and_invalidates_thumbnail_cache(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(asyncio, "to_thread", _inline_to_thread)
    gallery_dir = tmp_path / "gallery"
    gallery_dir.mkdir()
    source = gallery_dir / "wide.png"
    image = np.zeros((320, 960, 3), dtype=np.uint8)
    assert cv2.imwrite(str(source), image)
    service = WebStudioService(
        None,
        GenerationTracker(tmp_path, 20),
        _config(),
        tmp_path,
    )

    first = await service.gallery_image_base64("wide.png", thumbnail=True)
    decoded = cv2.imdecode(
        np.frombuffer(base64.b64decode(first["b64"]), dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    cache_path = gallery_dir / ".thumbs" / "wide.png.jpg"

    assert first["mime"] == "image/jpeg"
    assert max(decoded.shape[:2]) <= 512
    assert len(first["b64"]) <= 2 * 1024 * 1024
    assert cache_path.is_file()
    assert cache_path.stat().st_mtime_ns == source.stat().st_mtime_ns

    old_mtime = source.stat().st_mtime_ns
    image[:] = 255
    assert cv2.imwrite(str(source), image)
    os.utime(source, ns=(old_mtime + 1_000_000_000, old_mtime + 1_000_000_000))
    second = await service.gallery_image_base64("wide.png", thumbnail=True)

    assert second["b64"] != first["b64"]
    assert cache_path.stat().st_mtime_ns == source.stat().st_mtime_ns


@pytest.mark.asyncio
async def test_gallery_image_base64_returns_original_bytes(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(asyncio, "to_thread", _inline_to_thread)
    gallery_dir = tmp_path / "gallery"
    gallery_dir.mkdir()
    source = _png(gallery_dir / "original.png")
    service = WebStudioService(
        None,
        GenerationTracker(tmp_path, 20),
        _config(),
        tmp_path,
    )

    payload = await service.gallery_image_base64("original.png")

    assert payload["mime"] == "image/png"
    assert payload["preview"] is False
    assert base64.b64decode(payload["b64"]) == source.read_bytes()


@pytest.mark.asyncio
async def test_gallery_image_base64_compresses_large_preview_keeps_original(
    tmp_path,
) -> None:
    gallery_dir = tmp_path / "gallery"
    gallery_dir.mkdir()
    source = gallery_dir / "large.png"
    rng = np.random.default_rng(102)
    pixels = rng.integers(0, 256, size=(1800, 2400, 3), dtype=np.uint8)
    assert cv2.imwrite(str(source), pixels)
    original = source.read_bytes()
    assert len(original) > 6 * 1024 * 1024
    service = WebStudioService(
        None, GenerationTracker(tmp_path, 20), _config(), tmp_path
    )

    payload = await service.gallery_image_base64(source.name)

    assert payload["preview"] is True
    assert payload["mime"] == "image/jpeg"
    assert len(payload["b64"]) <= 8 * 1024 * 1024
    preview = cv2.imdecode(
        np.frombuffer(base64.b64decode(payload["b64"]), dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    assert preview.shape[:2] == (1536, 2048)
    assert service.gallery_file(source.name).read_bytes() == original
    assert sorted(path.name for path in gallery_dir.iterdir()) == [source.name]


@pytest.mark.asyncio
async def test_large_preview_rejects_undecodable_image(tmp_path) -> None:
    service = WebStudioService(
        None, GenerationTracker(tmp_path, 20), _config(), tmp_path
    )
    service.gallery_dir.mkdir()
    (service.gallery_dir / "invalid.png").write_bytes(b"x" * (6 * 1024 * 1024 + 1))
    with pytest.raises(StudioServiceError) as error:
        await service.gallery_image_base64("invalid.png")
    assert error.value.status_code == 422


def test_import_legacy_images_moves_records_and_dedupes(tmp_path) -> None:
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    _png(images_dir / "gemini_advanced_image_20260101_000000_000_abc123.png")
    tracked = _png(
        images_dir / "gemini_advanced_image_20260102_000000_000_def456.png", value=1
    )
    (images_dir / "gemini_advanced_image_bad.png").write_bytes(b"not-an-image")
    (images_dir / "help_2026.png").write_bytes(b"help-cache")  # 非生成前缀不处理

    tracker = GenerationTracker(tmp_path, 20)
    service = WebStudioService(None, tracker, _config(), tmp_path)

    # 已归档内容：同字节已存在于 gallery，应只删除冗余副本不重复建档
    gallery_dir = tmp_path / "gallery"
    gallery_dir.mkdir()
    gallery_copy = gallery_dir / "gemini_studio_20260103_000000_ff00ee.png"
    gallery_copy.write_bytes(tracked.read_bytes())

    imported = service.import_legacy_images()

    assert imported == 1
    assert not (
        images_dir / "gemini_advanced_image_20260101_000000_000_abc123.png"
    ).exists()
    assert not tracked.exists()  # 内容重复 -> 冗余副本被移除
    assert not (images_dir / "gemini_advanced_image_bad.png").exists()  # 不可解码被删除
    assert (images_dir / "help_2026.png").exists()  # 非生成前缀保留
    remaining = list(gallery_dir.iterdir())
    assert len(remaining) == 2
    record = tracker.query_history(
        page=1, size=10, keyword="", source="", group_id="", user_id=""
    )["items"][0]
    assert record["source"] == "legacy"
    assert record["status"] == "succeeded"
    # 重复导入幂等
    assert service.import_legacy_images() == 0
