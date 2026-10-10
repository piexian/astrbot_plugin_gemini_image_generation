"""LegacyClientAdapter：把注入的 legacy 客户端包装成 Router 候选 provider。

客户端以鸭子类型注入（``async generate_image(config, ...)``），本模块不
import 任何旧层实现，供配置层把现有候选逐个桥接到 Provider contract。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from astrbot.api import logger

from ..api_types import ApiRequestConfig
from .errors import LegacyProviderError
from .remote_fetch import _remaining
from .requests import GenerationRequest, ProviderAttempt
from .results import GenerationResult

# legacy client 无 deadline 输入时的默认总超时（秒）。
_DEFAULT_TOTAL_TIMEOUT = 120
# legacy client 内部已自重试，桥接层沿用其默认重试次数。
_LEGACY_MAX_RETRIES = 3


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

    async def supports(self, request: GenerationRequest) -> bool:
        # 候选过滤由配置层完成，适配器一律支持。
        return True

    async def normalize(self, request: GenerationRequest) -> GenerationRequest:
        return request

    async def build_request(
        self, request: GenerationRequest, attempt: ProviderAttempt
    ) -> tuple[str, dict[str, str], Any]:
        # 路由固定沿用入口的真实 requested_provider/model（None 即全量轮询）；
        # 适配器自身的 provider/model 仅作结果标注，不得写入 requested_*——
        # legacy client 按其过滤候选，哨兵值会导致空路由。
        metadata = request.metadata or {}

        def body_factory() -> ApiRequestConfig:
            # legacy client 自建请求，factory 仅为 contract 形状服务；
            # 每次调用构造新实例，保证重试时幂等重建。
            return ApiRequestConfig(
                model=request.model or self._model,
                prompt=request.prompt,
                candidate_id=self._candidate_id,
                requested_provider=request.provider,
                requested_model=request.model,
                resolution=request.resolution,
                aspect_ratio=request.aspect_ratio,
                negative_prompt=request.negative_prompt,
                quality=request.quality,
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
        config = body_factory()
        remaining = _remaining(attempt.deadline_at, None)
        if remaining is None:
            total_timeout = _DEFAULT_TOTAL_TIMEOUT
        else:
            # 向下取整且至少 1 秒，已过期的尝试仍给 legacy client 一次机会。
            total_timeout = max(int(remaining), 1)
        logger.debug(
            "legacy 候选 %s/%s 派发（total_timeout=%s）",
            self._provider,
            self._model,
            total_timeout,
        )
        response = await self._client.generate_image(
            config,
            max_retries=_LEGACY_MAX_RETRIES,
            total_timeout=total_timeout,
        )
        # config 随返回值传出：client 在其上回写 successful_*，供 parse_response 标注。
        return response, config

    async def parse_response(
        self,
        response: Any,
        *,
        request: GenerationRequest,
        attempt: ProviderAttempt,
    ) -> GenerationResult:
        legacy_tuple, config = response
        urls, paths, text, error = legacy_tuple
        if error:
            # legacy client 内部已自重试，这里标记不可重试：
            # Router 不再重复重试，错误直接 fallback 到下一候选。
            raise LegacyProviderError(
                f"legacy provider {self._provider} 生成失败: {error}",
            )
        artifacts = tuple(sorted(set(urls or ()) | set(paths or ())))
        # 空 urls/paths 也返回（text-only 输出）；partial 语义由上游判定。
        # client 在本次构造的 config 上回写实际命中候选，标注优先采用回写值。
        provider = getattr(config, "successful_provider", None) or self._provider
        model = getattr(config, "successful_model", None) or self._model
        candidate_id = (
            getattr(config, "successful_candidate_id", None) or self._candidate_id
        )
        return GenerationResult(
            artifacts=artifacts,
            text=text,
            provider=provider,
            model=model,
            candidate_id=candidate_id,
            attempt_count=attempt.number,
        )
