"""tl/core service 门面与 stack 装配测试。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image as PILImage

from tl.core.errors import InvalidRequestError
from tl.core.models import Artifact
from tl.core.requests import MAX_REFERENCE_VALUE_CHARS, GenerationRequest
from tl.core.stack import _ReferenceLeaseStore, build_generation_stack
from tl.core.states import JobState
from tl.reference_image import REFERENCE_IMAGE_CACHE_DIR


class _FakeClient:
    """鸭子类型 legacy client：记录 config，按 provider 可选抛异常。"""

    def __init__(self, fail_providers: frozenset[str] = frozenset()) -> None:
        self.fail_providers = fail_providers
        self.configs: list = []

    async def generate_image(
        self, config, *, max_retries=3, total_timeout=120, **kwargs
    ):
        self.configs.append(config)
        # 候选标签经 config.model 兜底传入（requested_* 只承载入口真实路由）。
        if config.model in self.fail_providers:
            # 直接抛异常走 Router 异常 fallback（RuntimeError 不可重试，
            # 立即转下一候选）；parse_response error 字段路径二选一不覆盖。
            raise RuntimeError(f"{config.model} candidate failed")
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
        assert config.requested_provider is None
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
    client = _FakeClient(fail_providers=frozenset({"alpha-x"}))
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
        assert [item.model for item in client.configs] == [
            "alpha-x",
            "beta-x",
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
    assert service._scheduler.store._closed is True
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


class _RecordingReferenceService:
    """记录 resolve 入参的假 ReferenceService（async generator 形态）。"""

    def __init__(self):
        self.seen = []

    async def resolve(self, references, *, deadline_at=None, requester=None):
        self.seen.append((tuple(references), deadline_at, dict(requester or {})))
        yield Artifact(artifact_id="ref-1", kind="reference", location="/tmp/ref-1")


@pytest.mark.asyncio
async def test_reference_lease_store_forwards_deadline_and_requester():
    service = _RecordingReferenceService()
    store = _ReferenceLeaseStore(service)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=5)
    artifacts = await store.resolve(
        ("ref://a",), deadline_at=deadline, requester={"user_id": "u1"}
    )
    # _ReferenceLeaseStore 把 deadline_at/requester 原样转发给 ReferenceService。
    assert service.seen == [(("ref://a",), deadline, {"user_id": "u1"})]
    assert [item.location for item in artifacts] == ["/tmp/ref-1"]
    # 未传 deadline/requester 时透传 None，保持默认抓取超时行为。
    assert await store.resolve(("ref://b",)) == list(artifacts)
    assert service.seen[-1] == (("ref://b",), None, {})


@pytest.mark.asyncio
async def test_prepare_references_requires_reference_service():
    from tl.core.service import GenerationService

    service = GenerationService(None)
    with pytest.raises(RuntimeError, match="ReferenceService"):
        await service.prepare_references(
            ("data:image/png;base64,AAA",), requester={"user_id": "u1"}
        )


@pytest.mark.asyncio
async def test_stack_inline_reference_end_to_end(tmp_path):
    """≥2MB 内联图经 prepare_references 换句柄后走完整 v2 栈。"""

    import base64
    import os

    raw = os.urandom(3 * 1024 * 1024)
    image = PILImage.frombytes("RGB", (1024, 1024), raw)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    payload = buffer.getvalue()
    assert len(payload) > 2 * 1024 * 1024
    data_uri = f"data:image/png;base64,{base64.b64encode(payload).decode()}"
    assert len(data_uri) > MAX_REFERENCE_VALUE_CHARS
    # 未换句柄的原始 data URI 撞引用长度上限，被请求校验拒绝。
    with pytest.raises(InvalidRequestError):
        GenerationRequest(prompt="draw", source="test", reference_images=(data_uri,))

    client = _FakeClient()
    service = build_generation_stack(
        client,
        [{"provider": "gemini", "model": "gemini-x"}],
        tmp_path / "store",
        (),
    )
    try:
        requester = {"user_id": "u1", "umo": "qq:g1"}
        prepared = await service.prepare_references([data_uri], requester=requester)
        assert len(prepared) == 1
        handle = prepared[0]
        assert handle.startswith("ref:tmp:")
        assert len(handle) <= MAX_REFERENCE_VALUE_CHARS

        request = GenerationRequest(
            prompt="draw with inline reference",
            source="command",
            reference_images=prepared,
            requester=requester,
        )
        assert all(
            len(item) <= MAX_REFERENCE_VALUE_CHARS for item in request.reference_images
        )
        result = await service.wait((await service.submit(request)).job_id)
        assert result.state is JobState.SUCCEEDED

        # 假 client 收到的参考图是落盘路径，字节与原图一致。
        assert len(client.configs) == 1
        location = Path(client.configs[0].reference_images[0])
        assert location.is_file()
        assert location.read_bytes() == payload
    finally:
        await service.close()

    # JobStore 持久化的请求只有句柄/描述符：无 base64、无落盘路径。
    connection = sqlite3.connect(tmp_path / "store" / "jobs.sqlite3")
    try:
        rows = [row[0] for row in connection.execute("SELECT request_json FROM jobs")]
    finally:
        connection.close()
    assert rows
    for request_json in rows:
        stored = json.loads(request_json)
        assert "base64," not in request_json
        assert str(location) not in request_json
        assert data_uri[:64] not in request_json
        # 引用一律是描述符对象：无字节、无本地路径，原始句柄可经描述符脱敏保留。
        assert stored["reference_images"]
        assert all(
            isinstance(item, dict) and {"reference_id", "artifact_id"} <= set(item)
            for item in stored["reference_images"]
        )
