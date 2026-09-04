"""Background batch orchestration for named image generation jobs."""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger

from .background_notify import report_background_failure
from .generation_call import invoke_generation_core
from .generation_tracker import tracking_context


def _split_images(images: list[str]) -> tuple[list[str], list[str]]:
    urls: list[str] = []
    paths: list[str] = []
    for image in images:
        if str(image).startswith(("http://", "https://")):
            urls.append(image)
        else:
            paths.append(image)
    return urls, paths


def _batch_item_result(
    item: dict[str, Any],
    *,
    collected: list[str] | None = None,
    text_parts: list[str] | None = None,
    last_stats: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    target = int(item["image_count"])
    images = collected or []
    stats = last_stats or {}
    image_urls, image_paths = _split_images(images)
    return {
        "name": item["name"],
        "requested_images": target,
        "generated_images": len(images),
        "success": len(images) >= target,
        "error": error,
        "image_urls": image_urls,
        "image_paths": image_paths,
        "text_content": "\n".join(text_parts or []) or None,
        "provider": stats.get("successful_provider"),
        "model": stats.get("successful_model"),
        "alias": stats.get("successful_model_alias"),
        "candidate_id": stats.get("successful_candidate_id"),
    }


async def _generate_batch_item(
    plugin: Any,
    event: Any,
    item: dict[str, Any],
) -> dict[str, Any]:
    target = int(item["image_count"])
    collected: list[str] = []
    text_parts: list[str] = []
    last_stats: dict[str, Any] = {}
    error: str | None = None

    try:
        while len(collected) < target:
            remaining = target - len(collected)
            success, result_data = await invoke_generation_core(
                plugin,
                event=event,
                prompt=item["prompt"],
                reference_images=item.get("reference_images") or [],
                avatar_reference=item.get("avatar_reference") or [],
                override_resolution=item.get("resolution"),
                override_aspect_ratio=item.get("aspect_ratio"),
                is_tool_call=True,
                requested_provider=item.get("provider"),
                requested_model=item.get("model"),
                negative_prompt=item.get("negative_prompt"),
                watermark=item.get("watermark"),
                quality=item.get("quality"),
                image_count=remaining,
                suppress_resolution=bool(item.get("suppress_resolution", False)),
            )
            last_stats = plugin.image_generator.get_request_stats()
            if not success or not isinstance(result_data, tuple):
                error = (
                    str(result_data) if isinstance(result_data, str) else "图像生成失败"
                )
                break

            image_urls, image_paths, text_content, _thought_signature = result_data
            images = plugin.message_sender.merge_available_images(
                image_urls,
                image_paths,
            )
            if not images:
                error = "供应商未返回有效图片"
                break
            collected.extend(images[:remaining])
            if text_content:
                text_parts.append(str(text_content))
    except Exception as exc:
        error = f"批量项异常: {exc}"
        logger.error(
            f"[批量任务] 项目 {item['name']} 执行异常: {exc}",
            exc_info=True,
        )

    return _batch_item_result(
        item,
        collected=collected,
        text_parts=text_parts,
        last_stats=last_stats,
        error=error,
    )


def _public_item(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": result["name"],
        "requested_images": result["requested_images"],
        "generated_images": result["generated_images"],
        "success": result["success"],
        "error": result.get("error"),
        "provider": result.get("provider"),
        "model": result.get("model"),
        "alias": result.get("alias"),
        "candidate_id": result.get("candidate_id"),
    }


async def _send_batch_results(
    plugin: Any,
    event: Any,
    task_id: str,
    results: list[dict[str, Any]],
) -> None:
    summary_lines = [f"批量生图任务 {task_id} 已完成："]
    for result in results:
        if result["success"]:
            summary_lines.append(
                f"- {result['name']}: {result['generated_images']}/{result['requested_images']} 张"
            )
        else:
            summary_lines.append(
                f"- {result['name']}: 失败或未完成，"
                f"{result['generated_images']}/{result['requested_images']} 张，"
                f"{result.get('error') or '未知错误'}"
            )
    summary = "\n".join(summary_lines)
    if any(not result["success"] for result in results):
        # 有失败项：聚合走失败通知出口（回灌 LLM 或按配置直发）
        await report_background_failure(
            plugin,
            event,
            summary,
            scene=f"批量任务/{task_id}",
            task_id=task_id,
        )
    else:
        await event.send(event.plain_result(summary))

    for result in results:
        if not result["image_urls"] and not result["image_paths"]:
            continue
        candidate_text = "/".join(
            part
            for part in (
                result.get("provider"),
                result.get("alias") or result.get("model"),
            )
            if part
        )
        label = f"批量任务：{result['name']}"
        if candidate_text:
            label += f"（{candidate_text}）"
        await plugin.message_sender.send_results_with_stream_retry(
            event=event,
            image_urls=result["image_urls"],
            image_paths=result["image_paths"],
            text_content=label,
            thought_signature=None,
            scene=f"批量任务/{result['name']}",
            force_text_response=True,
        )


async def run_batch_job(
    plugin: Any,
    event: Any,
    task_id: str,
    items: list[dict[str, Any]],
) -> None:
    """Generate named items with flat configured concurrency and one final delivery."""
    manager = plugin.background_task_manager
    semaphore = asyncio.Semaphore(max(int(plugin.cfg.batch_concurrency), 1))
    progress_lock = asyncio.Lock()
    results: list[dict[str, Any] | None] = [None] * len(items)

    async def run_one(index: int, item: dict[str, Any]) -> None:
        async with semaphore:
            await manager.update(task_id, current_item=item["name"])
            with tracking_context("llm_batch", task_id, item["name"]):
                result = await _generate_batch_item(plugin, event, item)
        async with progress_lock:
            results[index] = result
            completed = sum(value is not None for value in results)
            succeeded = sum(bool(value and value["success"]) for value in results)
            failed = completed - succeeded
            await manager.update(
                task_id,
                completed_items=completed,
                succeeded_items=succeeded,
                failed_items=failed,
                current_item=None,
                items=[_public_item(value) for value in results if value is not None],
            )

    try:
        outcomes = await asyncio.gather(
            *(run_one(index, item) for index, item in enumerate(items)),
            return_exceptions=True,
        )
        for index, outcome in enumerate(outcomes):
            if not isinstance(outcome, BaseException):
                continue
            item = items[index]
            error = (
                "批量项已取消"
                if isinstance(outcome, asyncio.CancelledError)
                else f"批量项异常: {outcome}"
            )
            logger.error(
                f"[批量任务] 项目 {item['name']} 调度异常: {outcome}",
                exc_info=(type(outcome), outcome, outcome.__traceback__),
            )
            if results[index] is None:
                results[index] = _batch_item_result(item, error=error)

        final_results = [value for value in results if value is not None]
        succeeded = sum(bool(value["success"]) for value in final_results)
        failed = len(final_results) - succeeded
        generated = sum(int(value["generated_images"]) for value in final_results)
        if succeeded == len(items):
            status = "succeeded"
        elif generated > 0:
            status = "partial_success"
        else:
            status = "failed"
        await _send_batch_results(plugin, event, task_id, final_results)
        await manager.update(
            task_id,
            status=status,
            message="批量生成已完成并发送",
            current_item=None,
            completed_items=len(final_results),
            succeeded_items=succeeded,
            failed_items=failed,
            items=[_public_item(value) for value in final_results],
        )
    except asyncio.CancelledError:
        await manager.update(
            task_id,
            status="interrupted",
            message="批量生成已中断",
            current_item=None,
        )
        raise
    except Exception as exc:
        logger.error(f"[批量任务] {task_id} 执行失败: {exc}", exc_info=True)
        final_results = [value for value in results if value is not None]
        succeeded = sum(bool(value["success"]) for value in final_results)
        generated = sum(int(value["generated_images"]) for value in final_results)
        await manager.update(
            task_id,
            status="partial_success" if generated else "failed",
            message=f"批量任务异常: {exc}",
            current_item=None,
            completed_items=len(final_results),
            succeeded_items=succeeded,
            failed_items=len(final_results) - succeeded,
            items=[_public_item(value) for value in final_results],
        )
        try:
            await report_background_failure(
                plugin,
                event,
                f"批量生图任务 {task_id} 失败：{exc}",
                scene=f"批量任务/{task_id}",
                task_id=task_id,
            )
        except Exception:
            pass
