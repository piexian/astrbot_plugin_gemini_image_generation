"""后台生图结果的分发与单图任务记录收尾（旧后台路径与 v2 投递适配器共用）。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from astrbot.api import logger

from .background_notify import notify_llm_background_result, report_background_failure


async def bump_delivery_count(
    plugin: Any,
    task_id: str,
    *,
    scene: str,
) -> None:
    """累加后台任务的成功交付次数；仅记录，绝不拦截发送。"""
    manager = getattr(plugin, "background_task_manager", None)
    if manager is None or not hasattr(manager, "record_delivery"):
        return
    try:
        await manager.record_delivery(task_id)
    except Exception as exc:
        logger.warning(f"[{scene}] 记录交付次数失败: {exc}")


async def dispatch_generation_result(
    plugin: Any,
    event: Any,
    success: bool,
    result_data: Any,
    *,
    scene: str,
    fallback_text: str | None = None,
    force_text_response: bool = False,
    notify_llm: bool = False,
    task_id: str | None = None,
) -> bool:
    if success and isinstance(result_data, tuple):
        image_urls, image_paths, text_content, thought_signature = result_data

        # 代理全链路：后台发送前，将需要代理的远程 URL 下载为本地文件
        # 避免 NapCat 等平台无法访问代理依赖的 URL
        api_client = getattr(plugin, "api_client", None)
        has_proxy = bool(api_client and getattr(api_client, "proxy", None))
        if has_proxy and image_urls:
            downloaded_paths: list[str] = []
            remaining_urls: list[str] = []
            try:
                session = await api_client._get_session()
                for url in image_urls:
                    if not url.startswith(("http://", "https://")):
                        remaining_urls.append(url)
                        continue
                    try:
                        _, local_path = await api_client._download_image(
                            url, session, use_cache=False
                        )
                        if local_path:
                            downloaded_paths.append(local_path)
                        else:
                            remaining_urls.append(url)
                    except Exception as e:
                        logger.warning(
                            f"[{scene}] 代理下载图片失败，保留原 URL: {url[:80]} {e}"
                        )
                        remaining_urls.append(url)
            except Exception as e:
                logger.warning(f"[{scene}] 获取代理下载会话失败: {e}")
                remaining_urls = list(image_urls)
            image_urls = remaining_urls
            image_paths = list(image_paths or []) + downloaded_paths

        available_images = plugin.message_sender.merge_available_images(
            image_urls,
            image_paths,
        )
        prepared_text = plugin.message_sender.prepare_text_content(
            text_content,
            available_images,
        )
        content_text = prepared_text or fallback_text
        if notify_llm:
            return await notify_llm_background_result(
                plugin,
                event,
                {
                    "task_id": task_id or "",
                    "status": "succeeded",
                    "image_urls": list(image_urls or []),
                    "image_paths": list(image_paths or []),
                    "text_content": content_text,
                },
                notice=f"后台图片生成任务 {task_id or '-'} 已完成，请调用主动发送工具发送图片。",
                scene=scene,
            )
        if text_content and not prepared_text and fallback_text:
            logger.info(
                f"[{scene}] Text content only contained image references; using fallback text."
            )
        try:
            await plugin.message_sender.send_results_with_stream_retry(
                event=event,
                image_urls=image_urls,
                image_paths=image_paths,
                text_content=content_text,
                thought_signature=thought_signature,
                scene=scene,
                force_text_response=force_text_response,
                text_content_prepared=True,
            )
        except Exception as exc:
            logger.warning(f"[{scene}] 发送结果失败: {exc}")
            return False
        if task_id:
            await bump_delivery_count(plugin, task_id, scene=scene)
        return True

    error_msg = result_data if isinstance(result_data, str) else "❌ 图像生成失败"
    if notify_llm:
        return await report_background_failure(
            plugin,
            event,
            error_msg,
            scene=scene,
            task_id=task_id,
        )
    try:
        await event.send(event.plain_result(error_msg))
    except Exception as exc:
        logger.warning(f"[{scene}] 发送错误消息失败: {exc}")
        return False
    return True


def single_task_record_changes(
    *,
    success: bool,
    delivered: bool,
    result_data: Any,
    stats: Mapping[str, Any],
) -> dict[str, Any]:
    """单图后台任务收尾时写入记录的字段；``stats`` 取 successful_* 键。"""
    item = {
        "name": "single",
        "success": bool(success),
        "provider": stats.get("successful_provider"),
        "model": stats.get("successful_model"),
        "alias": stats.get("successful_model_alias"),
        "candidate_id": stats.get("successful_candidate_id"),
        "delivery_success": delivered,
    }
    if success and isinstance(result_data, tuple):
        item["image_urls"] = list(result_data[0] or [])
        item["image_paths"] = list(result_data[1] or [])
    if success and delivered:
        final_status = "succeeded"
        final_message = "图片生成已完成并发送"
    elif success:
        final_status = "partial_success"
        final_message = "图片生成已完成，但结果发送未成功；可查询任务获取图片 URL/路径"
    else:
        final_status = "failed"
        final_message = str(result_data)
    return {
        "status": final_status,
        "message": final_message,
        "completed_items": 1,
        "succeeded_items": 1 if success else 0,
        "failed_items": 0 if success else 1,
        "items": [item],
    }
