"""LegacyClientAdapter：把注入的 legacy 客户端包装成 Router 候选 provider。

客户端以鸭子类型注入（``async generate_image(config, ...)``），本模块不
import 任何旧层实现，供配置层把现有候选逐个桥接到 Provider contract。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from ..generation_scheduler import emit_generation_count
from .errors import LegacyProviderError, redact_text
from .remote_fetch import _remaining
from .requests import GenerationRequest, ProviderAttempt
from .results import GenerationResult

# legacy client 无 deadline 输入时的默认总超时（秒）。
_DEFAULT_TOTAL_TIMEOUT = 120
# legacy client 内部已自重试，桥接层沿用其默认重试次数。
_LEGACY_MAX_RETRIES = 3
# 补足张数时，后续轮次比 Router 的整体 deadline 提前这么久自行收敛为部分结果；
# 否则 Router 的外层超时会连同已累积的产出一起取消。
_FILL_DEADLINE_MARGIN_SECONDS = 1.0
_FILL_ERROR_MAX_CHARS = 200


class LegacyClientAdapter:
    """把 legacy client 适配为 supports/normalize/build/send/parse 契约候选。"""

    def __init__(
        self,
        client: Any,
        *,
        provider: str,
        model: str,
        candidate_id: str | None = None,
        proxy: str | None = None,
        provider_settings: Mapping | None = None,
    ) -> None:
        self._client = client
        self._provider = str(provider)
        self._model = str(model)
        self._candidate_id = candidate_id
        self._proxy = proxy
        self._provider_settings = (
            dict(provider_settings) if provider_settings is not None else None
        )

    @property
    def name(self) -> str:
        return self._provider

    @property
    def route_provider(self) -> str:
        return self._provider

    @property
    def route_model(self) -> str:
        return self._model

    async def supports(self, request: GenerationRequest) -> bool:
        # 候选过滤由配置层完成，适配器一律支持。
        return True

    async def normalize(self, request: GenerationRequest) -> GenerationRequest:
        return request

    async def build_request(
        self, request: GenerationRequest, attempt: ProviderAttempt
    ) -> tuple[str, dict[str, str], Any]:
        # 首选候选沿用入口 requested_provider/model；Router 回退时改用候选自身值，
        # 否则 legacy client 会再次路由到已经失败的首选项。
        metadata = request.metadata or {}
        route_provider = attempt.context.get("candidate_provider")
        route_model = attempt.context.get("candidate_model")
        preferred = bool(attempt.context.get("candidate_preferred", True))
        requested_provider = (
            request.provider if preferred or not route_provider else str(route_provider)
        )
        requested_model = (
            request.model if preferred or not route_model else str(route_model)
        )

        def body_factory() -> ApiRequestConfig:
            # legacy client 自建请求，factory 仅为 contract 形状服务；
            # 每次调用构造新实例，保证重试时幂等重建。
            return ApiRequestConfig(
                model=requested_model or self._model,
                prompt=request.prompt,
                candidate_id=self._candidate_id,
                requested_provider=requested_provider,
                requested_model=requested_model,
                requested_candidate_id=metadata.get("requested_candidate_id"),
                route_all_candidates=bool(metadata.get("route_all_candidates", False)),
                resolution=request.resolution,
                aspect_ratio=request.aspect_ratio,
                negative_prompt=request.negative_prompt,
                quality=request.quality,
                seed=metadata.get("seed"),
                generation_settings=metadata.get("generation_settings") or None,
                watermark=request.watermark,
                reference_images=list(request.reference_images),
                proxy=self._proxy,
                provider_settings=(
                    dict(self._provider_settings)
                    if self._provider_settings is not None
                    else None
                ),
                image_count=request.image_count,
                suppress_resolution=bool(metadata.get("suppress_resolution", False)),
                enable_smart_retry=bool(metadata.get("enable_smart_retry", True)),
            )

        return "", {}, body_factory

    async def send(
        self,
        request: GenerationRequest,
        attempt: ProviderAttempt,
        *,
        url: str,
        headers: Mapping[str, str],
        body_factory: Any,
    ) -> Any:
        metadata = request.metadata or {}
        if metadata.get("fill_image_count") and request.image_count > 1:
            return await self._send_filling(request, attempt, body_factory)
        config = body_factory()
        remaining = _remaining(attempt.deadline_at, None)
        response = await self._generate(request, config, remaining)
        # config 随返回值传出：client 在其上回写 successful_*，供 parse_response 标注。
        return response, config

    async def _generate(
        self, request: GenerationRequest, config: Any, remaining: float | None
    ) -> Any:
        """按剩余 deadline 换算超时后调用 legacy client 一次；APIError 转不可重试错误。"""

        configured_retries = int(
            (request.metadata or {}).get("max_attempts_per_key", _LEGACY_MAX_RETRIES)
        )
        max_retries = max(configured_retries, 1)
        configured_timeout = int(
            (request.metadata or {}).get("total_timeout", _DEFAULT_TOTAL_TIMEOUT)
        )
        configured_timeout = max(configured_timeout, 1)
        if remaining is None:
            total_timeout = configured_timeout
        else:
            # 向下取整且至少 1 秒，已过期的尝试仍给 legacy client 一次机会。
            total_timeout = max(min(int(remaining), configured_timeout), 1)
        logger.debug(
            "legacy 候选 %s/%s 派发（total_timeout=%s）",
            self._provider,
            self._model,
            total_timeout,
        )
        try:
            return await self._client.generate_image(
                config,
                max_retries=max_retries,
                total_timeout=total_timeout,
                per_retry_timeout=configured_timeout,
                max_total_time=(
                    max(int(remaining), 1)
                    if remaining is not None
                    else configured_timeout * 2
                ),
            )
        except APIError as error:
            # legacy client 内部已按密钥重试并做过候选回退，这里转为不可
            # 重试错误：Router 不再重复一轮，直接 fallback 到下一候选。
            raise self._api_error_to_legacy(error) from error

    async def _send_filling(
        self, request: GenerationRequest, attempt: ProviderAttempt, body_factory: Any
    ) -> Any:
        """逐轮请求剩余张数，累积去重后的图片与文本，直到满额、没有新图或出错。

        首轮出错照常抛出；之后的轮次出错、没有新图或来不及（临近 deadline）都
        停下并带着已有产出返回，原因随响应交给 parse_response 记入 stats。
        """

        target = request.image_count
        urls: list[str] = []
        paths: list[str] = []
        texts: list[str] = []
        collected: set[str] = set()
        config: Any = None
        fill_error: str | None = None
        while len(collected) < target:
            first_round = config is None
            round_config = body_factory()
            round_config.image_count = target - len(collected)
            remaining = _remaining(attempt.deadline_at, None)
            if first_round:
                response = await self._generate(request, round_config, remaining)
            else:
                if remaining is not None:
                    remaining -= _FILL_DEADLINE_MARGIN_SECONDS
                    if remaining <= 0:
                        fill_error = "已临近总时限，未能补足张数"
                        break
                try:
                    response = await asyncio.wait_for(
                        self._generate(request, round_config, remaining),
                        timeout=remaining,
                    )
                except Exception as error:
                    fill_error = self._fill_error_text(error)
                    break
            round_urls, round_paths, round_text, _signature = response
            returned = dict.fromkeys(
                str(item)
                for item in (*(round_urls or ()), *(round_paths or ()))
                if item
            )
            fresh = [item for item in returned if item not in collected][
                : target - len(collected)
            ]
            if not fresh:
                if first_round:
                    # 首轮就没有图：交回原始响应，由 parse_response 按单次调用判定。
                    return response, round_config
                fill_error = "供应商未返回新的图片"
                break
            url_items = {str(item) for item in round_urls or ()}
            for item in fresh:
                (urls if item in url_items else paths).append(item)
                collected.add(item)
            if round_text:
                texts.append(str(round_text))
            config = round_config
            await emit_generation_count(
                len(collected), self._progress_stats(round_config)
            )
        if fill_error:
            logger.info(
                "legacy 候选 %s/%s 补足提前结束（%s/%s 张）: %s",
                self._provider,
                self._model,
                len(collected),
                target,
                fill_error,
            )
        return (urls, paths, "\n".join(texts) or None, None), config, fill_error

    def _progress_stats(self, config: Any) -> dict[str, Any]:
        return {
            "provider": getattr(config, "successful_provider", None) or self._provider,
            "model": getattr(config, "successful_model", None) or self._model,
            "alias": getattr(config, "successful_model_alias", None),
            "retry_count": getattr(config, "retry_count", 0),
        }

    @staticmethod
    def _fill_error_text(error: Exception) -> str:
        if isinstance(error, asyncio.TimeoutError):
            return "补足轮次超时"
        detail = getattr(error, "message", None) or str(error) or type(error).__name__
        return redact_text(detail)[:_FILL_ERROR_MAX_CHARS]

    @staticmethod
    def _api_error_to_legacy(error: APIError) -> LegacyProviderError:
        error_type = getattr(error, "error_type", None)
        status_code = getattr(error, "status_code", None)
        if error_type in ("timeout", "cancelled"):
            code = "provider_timeout"
        elif error_type == "network":
            code = "provider_network"
        elif error_type == "workspace_unavailable":
            code = "provider_workspace_unavailable"
        elif isinstance(status_code, int):
            code = f"provider_http_{status_code}"
        else:
            code = "legacy_provider_failed"
        return LegacyProviderError(
            error.message,
            code=code,
            retryable=False,
            details={"status_code": status_code, "error_type": error_type},
        )

    async def parse_response(
        self,
        response: Any,
        *,
        request: GenerationRequest,
        attempt: ProviderAttempt,
    ) -> GenerationResult:
        # 补足张数的响应多带一项：提前停止补足的原因（满额时为 None）。
        legacy_tuple, config, *extra = response
        fill_error = extra[0] if extra else None
        # 第 4 项是思维签名（如 Gemini thoughtSignature），不是错误；
        # 忽略不持久化，避免成功结果被误判失败。
        urls, paths, text, _thought_signature = legacy_tuple
        if (request.metadata or {}).get("require_images") and not any(
            (*(urls or ()), *(paths or ()))
        ):
            # 入口要求必须有图（生成核心路径）：只有文本也算失败，与旧路径一致。
            raise LegacyProviderError(
                "供应商未返回图片", code="provider_no_image", retryable=False
            )
        if not (urls or paths) and not (text and str(text).strip()):
            raise LegacyProviderError(
                "供应商未返回图片或文本内容",
                code="provider_empty_result",
                retryable=False,
            )
        artifacts = tuple(sorted(set(urls or ()) | set(paths or ())))
        # 空 urls/paths 也返回（text-only 输出）；partial 语义由上游判定。
        # client 在本次构造的 config 上回写实际命中候选，标注优先采用回写值。
        provider = getattr(config, "successful_provider", None) or self._provider
        model = getattr(config, "successful_model", None) or self._model
        candidate_id = (
            getattr(config, "successful_candidate_id", None) or self._candidate_id
        )
        # client 只回写本次构造的 config 副本，legacy 统计字段需经 stats 带出，
        # 供 v2_bridge 回写入口 config；默认值省略键，不写 None 占位。
        stats: dict[str, Any] = {}
        retry_count = getattr(config, "retry_count", 0)
        if retry_count:
            stats["retry_count"] = retry_count
        retry_note = getattr(config, "retry_note", None)
        if retry_note:
            stats["retry_note"] = retry_note
        token_usage = getattr(config, "token_usage", None)
        if token_usage:
            stats["token_usage"] = dict(token_usage)
        model_alias = getattr(config, "successful_model_alias", None)
        if model_alias:
            stats["successful_model_alias"] = model_alias
        if fill_error:
            stats["fill_error"] = fill_error
        return GenerationResult(
            artifacts=artifacts,
            text=text,
            provider=provider,
            model=model,
            candidate_id=candidate_id,
            attempt_count=attempt.number,
            partial=bool(fill_error),
            stats=stats,
        )
