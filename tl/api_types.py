"""共享类型。

供 `tl/tl_api.py` 与各供应商实现共用的请求配置/异常类型。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ApiRequestConfig:
    """API 请求配置（基于 Gemini 官方文档）"""

    model: str
    prompt: str
    api_type: str = "openai"
    api_base: str | None = None
    api_key: str | None = None
    resolution: str | None = None
    aspect_ratio: str | None = None
    enable_grounding: bool = False
    response_modalities: str = "TEXT_IMAGE"  # 默认同时返回文本和图像
    max_tokens: int = 1000
    reference_images: list[str] | None = None
    response_text: str | None = None  # 存储文本响应
    enable_smart_retry: bool = True  # 智能重试开关
    enable_text_response: bool = False  # 文本响应开关
    force_resolution: bool = False  # 强制传递分辨率参数
    image_input_mode: str = "force_base64"  # 参考图统一转 base64

    # 官方文档推荐参数
    temperature: float = 0.7  # 控制生成随机性，0.0-1.0
    seed: int | None = None  # 固定种子以确保一致性
    safety_settings: dict | None = None  # 安全设置

    # 自定义 API 参数名（支持不同 API 的字段命名差异）
    resolution_param_name: str = "image_size"  # 分辨率参数名
    aspect_ratio_param_name: str = "aspect_ratio"  # 长宽比参数名


class APIError(Exception):
    """API 错误基类"""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        error_type: str | None = None,
        error_code: str | None = None,
        retryable: bool | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type
        self.error_code = (
            error_code  # 具体错误码，如 "RateLimitExceeded.EndpointRPMExceeded"
        )
        # 三态重试标志：None=使用通用逻辑判断，True/False=供应商显式指定
        self.retryable = retryable
