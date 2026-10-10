"""tl/core service 门面与 stack 装配测试。"""

from __future__ import annotations

import json
import sqlite3
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image as PILImage

from tl.core.requests import GenerationRequest
from tl.core.stack import build_generation_stack
from tl.core.states import JobState
from tl.reference_image import REFERENCE_IMAGE_CACHE_DIR


class _FakeClient:
    """鸭子类型 legacy client：记录 config，按 provider 可选抛异常。"""

    def __init__(self, fail_providers: frozenset[str] = frozenset()) -> None:
        self.fail_providers = fail_providers
        self.configs: list = []

    async def generate_image(self, config, *, max_retries=3, total_timeout=120):
        self.configs.append(config)
        if config.requested_provider in self.fail_providers:
            # 直接抛异常走 Router 异常 fallback（RuntimeError 不可重试，
            # 立即转下一候选）；parse_response error 字段路径二选一不覆盖。
            raise RuntimeError(f"{config.requested_provider} candidate failed")
        return (["https://img.test/generated.png"], [], "ok", None)


def _png_bytes() -> bytes:
    buffer = BytesIO()
    PILImage.new("RGB", (8, 8), (1, 2, 3)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_stack_assembles_and_submits(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    ref = root / "ref.png"
    ref.write_bytes(_png_bytes())
    client = _FakeClient()
    service = build_generation_stack(
        client,
        [
            {
                "provider": "gemini",
                "model": "gemini-x",
                "candidate_id": "cand-a",
                "proxy": "http://proxy.test:7890",
                "provider_settings": {"seed": "42"},
            },
            {"provider": "doubao", "model": "doubao-x"},
        ],
        tmp_path / "store",
        (root,),
    )
    try:
        request = GenerationRequest(
            prompt="draw with reference",
            source="test",
            reference_images=(str(ref),),
        )
        result = await service.wait((await service.submit(request)).job_id)
        assert result.state is JobState.SUCCEEDED
        assert result.result is not None
        assert result.result.provider == "gemini"
        assert len(client.configs) == 1
        config = client.configs[0]
        assert config.model == "gemini-x"
        assert config.requested_provider == "gemini"
        assert config.candidate_id == "cand-a"
        assert config.proxy == "http://proxy.test:7890"
        # 参考图已被 ReferenceService 解析为落盘缓存路径。
        assert len(config.reference_images) == 1
        location = Path(config.reference_images[0])
        assert location.parent == REFERENCE_IMAGE_CACHE_DIR
        assert location.is_file()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_stack_falls_back_to_next_candidate(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    client = _FakeClient(fail_providers=frozenset({"alpha"}))
    service = build_generation_stack(
        client,
        [
            {"provider": "alpha", "model": "alpha-x"},
            {"provider": "beta", "model": "beta-x"},
        ],
        tmp_path / "store",
        (root,),
    )
    try:
        result = await service.wait(
            (
                await service.submit(GenerationRequest(prompt="draw", source="test"))
            ).job_id
        )
        assert result.state is JobState.SUCCEEDED
        assert result.result is not None
        assert result.result.provider == "beta"
        # 候选 A 抛异常后 fallback，仅 A、B 各收到一次请求。
        assert [item.requested_provider for item in client.configs] == [
            "alpha",
            "beta",
        ]
    finally:
        await service.close()


def test_stack_rejects_empty_candidates():
    with pytest.raises(ValueError):
        build_generation_stack(candidates=[])


@pytest.mark.asyncio
async def test_stack_close_is_idempotent(tmp_path):
    data_dir = tmp_path / "store"
    service = build_generation_stack(
        _FakeClient(),
        [{"provider": "gemini", "model": "gemini-x"}],
        data_dir,
        (),
    )
    assert (data_dir / "jobs.sqlite3").exists()
    await service.close()
    await service.close()
    assert (data_dir / "jobs.sqlite3").exists()


@pytest.mark.asyncio
async def test_stack_import_legacy_disabled_by_default(tmp_path):
    data_dir = tmp_path / "store"
    data_dir.mkdir()
    (data_dir / "background_tasks.json").write_text(
        json.dumps(
            {
                "tasks": {
                    "legacy-1": {
                        "task_id": "legacy-1",
                        "status": "succeeded",
                        "message": "legacy",
                        "total_items": 1,
                        "items": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    service = build_generation_stack(
        _FakeClient(),
        [{"provider": "gemini", "model": "gemini-x"}],
        data_dir,
        (),
    )
    await service.close()
    connection = sqlite3.connect(data_dir / "jobs.sqlite3")
    try:
        jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        imports = connection.execute(
            "SELECT COUNT(*) FROM legacy_imports WHERE source = ?",
            ("background_tasks.json",),
        ).fetchone()[0]
    finally:
        connection.close()
    assert jobs == 0
    assert imports == 0
