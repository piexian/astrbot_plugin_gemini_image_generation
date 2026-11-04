"""生成核心路径的“无图结果”按失败处理；命令快捷路径不受影响。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_limit_admission import _run_quick
from tests.test_v2_command_paths import _make_plugin, _wire_sender
from tl.core.stack import build_generation_stack
from tl.core.states import JobState
from tl.generation_errors import IMAGE_FILE_MISSING_MESSAGE
from tl.generation_tracker import GenerationTracker
from tl.image_generator import ImageGenerator
from tl.reference_image import REFERENCE_IMAGE_CACHE_DIR
from tl.v2_tracking import TrackerProjection

EVENT = SimpleNamespace(unified_msg_origin="platform:FriendMessage:user-1")


class TextOnlyClient:
    """供应商只返回文本、没有任何图片 URL/路径。"""

    def __init__(self, text="只有文本"):
        self.text = text
        self.configs = []

    async def generate_image(self, config, **kwargs):
        self.configs.append(config)
        return [], [], self.text, None


def make_generator(client):
    return ImageGenerator(
        context=None,
        api_client=client,
        filter_valid_fn=lambda images, source: images or [],
    )


def make_stack(tmp_path, client, **options):
    return build_generation_stack(
        client,
        [{"provider": "legacy", "model": "auto"}],
        tmp_path / "store",
        (REFERENCE_IMAGE_CACHE_DIR,),
        **options,
    )


async def archive_nothing(urls, paths, **kwargs):
    return []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["只有文本", None])
async def test_core_path_text_only_result_fails(tmp_path, text):
    tracker = GenerationTracker(tmp_path / "v2", 30)
    service = make_stack(
        tmp_path,
        TextOnlyClient(text),
        observers=[TrackerProjection(tracker, archive_nothing)],
    )
    try:
        result = await make_generator(TextOnlyClient(text)).generate_image_core(
            event=EVENT,
            prompt="draw",
            reference_images=[],
            avatar_reference=[],
            v2_service=service,
        )

        # 入口声明 require_images：没有图片就是失败，哪怕供应商给了文本。
        assert result == (False, IMAGE_FILE_MISSING_MESSAGE)
        (record,) = tracker.records_snapshot()
        assert record["status"] == "failed"
        assert record["requester"]["umo"] == EVENT.unified_msg_origin
        assert record["error"] == "供应商未返回图片"
        assert record["generated_images"] == 0 and record["images"] == []
    finally:
        await service.close()
        await tracker.close()


@pytest.mark.asyncio
async def test_core_path_request_declares_require_images(tmp_path):
    client = TextOnlyClient()
    service = make_stack(tmp_path, client)
    try:
        await make_generator(client).generate_image_core(
            event=EVENT,
            prompt="draw",
            reference_images=[],
            avatar_reference=[],
            v2_service=service,
        )

        (job,) = await service._scheduler.store.list_jobs()
        assert job.state is JobState.FAILED
        assert job.error_code == "provider_no_image"
        assert job.request.metadata["require_images"] is True
        # 不可重试：旧客户端只被调用一次。
        assert len(client.configs) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_quick_command_path_keeps_text_only_result_as_success(tmp_path):
    client = TextOnlyClient("仅文本")
    service = make_stack(tmp_path, client)
    plugin = _make_plugin()
    plugin._ensure_v2_service = lambda: service
    plugin.api_client = SimpleNamespace(generate_image=client.generate_image)
    plugin._ensure_api_client = lambda: True
    plugin.image_handler.fetch_images_from_event = AsyncMock(return_value=([], []))
    sent = _wire_sender(plugin)
    try:
        outputs = await _run_quick(plugin)

        # 快捷命令的请求不带 require_images：只有文本仍照常发送，不当失败。
        assert len(sent) == 1
        assert sent[0]["image_urls"] == [] and sent[0]["image_paths"] == []
        assert sent[0]["text_content"] == "仅文本"
        assert not any("图像文件未找到" in str(item) for item in outputs)
        (job,) = await service._scheduler.store.list_jobs()
        assert job.state is JobState.SUCCEEDED
        assert "require_images" not in job.request.metadata
    finally:
        await service.close()
