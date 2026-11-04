"""批量生图的结果拼装与通知辅助，供 v2 投递适配器复用。"""

from __future__ import annotations

from typing import Any

from .background_notify import notify_llm_background_result, report_background_failure


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
        "image_urls": result["image_urls"],
        "image_paths": result["image_paths"],
    }


def _batch_summary(task_id: str, results: list[dict[str, Any]]) -> str:
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
    return "\n".join(summary_lines)


async def _send_batch_results(
    plugin: Any,
    event: Any,
    task_id: str,
    results: list[dict[str, Any]],
) -> bool:
    summary = _batch_summary(task_id, results)
    if not any(result["image_urls"] or result["image_paths"] for result in results):
        return await report_background_failure(
            plugin, event, summary, scene=f"批量任务/{task_id}", task_id=task_id
        )
    return await notify_llm_background_result(
        plugin,
        event,
        {"task_id": task_id, "result": summary, "items": results},
        notice=summary,
        scene=f"批量任务/{task_id}",
    )
