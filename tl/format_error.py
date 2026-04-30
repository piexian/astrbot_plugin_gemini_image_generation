"""错误信息格式化:将异常对象/字符串转换为用户友好的中文提示。

抽取自 ``tl/tl_utils.py``(原 ``format_error_message``)。
"""

from __future__ import annotations


def format_error_message(error: Exception | str) -> str:
    """根据错误类型生成用户友好的错误消息

    Args:
        error: 异常对象或错误字符串

    Returns:
        格式化的错误提示消息
    """
    error_str = str(error).lower()
    original_error = str(error)

    # image_config oneof 冲突错误(参数名配置问题)
    if "image_config" in error_str and "oneof" in error_str:
        if "_image_size" in error_str or "imagesize" in error_str.replace("_", ""):
            return (
                f"❌ 图像生成失败：{original_error}\n"
                "🧐 原因：分辨率参数名配置不正确。\n"
                "✅ 建议：请联系管理员将 resolution_param_name 修改为 imageSize（驼峰式）。"
            )
        if "_aspect_ratio" in error_str or "aspectratio" in error_str.replace("_", ""):
            return (
                f"❌ 图像生成失败：{original_error}\n"
                "🧐 原因：宽高比参数名配置不正确。\n"
                "✅ 建议：请联系管理员将 aspect_ratio_param_name 修改为 aspectRatio（驼峰式）。"
            )
        return (
            f"❌ 图像生成失败：{original_error}\n"
            "🧐 原因：imageConfig 参数配置冲突。\n"
            "✅ 建议：请联系管理员检查 resolution_param_name 和 aspect_ratio_param_name 配置，\n"
            "   Google API 应使用驼峰式命名（imageSize, aspectRatio）。"
        )

    # API 密钥错误
    if "api key" in error_str or "api_key" in error_str or "invalid key" in error_str:
        return (
            f"❌ 图像生成失败：{original_error}\n"
            "🧐 原因：API 密钥无效或已过期。\n"
            "✅ 建议：请联系管理员检查并更新 API 密钥配置。"
        )

    # 模型不存在
    if "model not found" in error_str or "does not exist" in error_str:
        return (
            f"❌ 图像生成失败：{original_error}\n"
            "🧐 原因：指定的模型不存在或不可用。\n"
            "✅ 建议：请联系管理员检查模型名称配置是否正确。"
        )

    # 配额/限流错误
    if "quota" in error_str or "rate limit" in error_str or "429" in error_str:
        return (
            f"❌ 图像生成失败：{original_error}\n"
            "🧐 原因：API 请求配额已用尽或请求过于频繁。\n"
            "✅ 建议：请稍后重试，或联系管理员检查 API 配额。"
        )

    # 内容安全过滤
    if "safety" in error_str or "blocked" in error_str or "content_filter" in error_str:
        return (
            f"❌ 图像生成失败：{original_error}\n"
            "🧐 原因：提示词或图片内容被安全过滤器拦截。\n"
            "✅ 建议：请修改提示词内容后重试。"
        )

    # 超时错误
    if "timeout" in error_str or "timed out" in error_str:
        return (
            f"❌ 图像生成失败：{original_error}\n"
            "🧐 原因：请求超时，服务器响应过慢。\n"
            "✅ 建议：请稍后重试，如持续出现请联系管理员调整超时配置。"
        )

    # 网络连接错误
    if "connection" in error_str or "network" in error_str or "connect" in error_str:
        return (
            f"❌ 图像生成失败：{original_error}\n"
            "🧐 原因：网络连接失败。\n"
            "✅ 建议：请检查网络连接后重试，如需代理请联系管理员配置。"
        )

    # 参考图片问题
    if "reference" in error_str and "image" in error_str:
        return (
            f"❌ 图像生成失败：{original_error}\n"
            "🧐 原因：参考图片处理失败。\n"
            "✅ 建议：请尝试使用其他图片或不使用参考图重试。"
        )

    # 只返回文本，未生成图像（no_image_retry）
    if "no_image_retry" in error_str or "只返回了文本" in error_str:
        return (
            "⚠️ 图像生成未成功：模型只返回了文字描述，未生成图片。\n"
            "🧐 原因：模型可能需要更明确的绘图指令，或当前请求不适合生成图像。\n"
            "✅ 建议：请尝试更明确地描述您想要的图像内容。"
        )

    # 响应格式异常，未找到图像（invalid_response）
    if "invalid_response" in error_str or "未找到有效的图像" in error_str:
        return (
            "⚠️ 图像生成失败：API 响应中未包含图像数据。\n"
            "🧐 原因：服务端返回了空响应或格式异常。\n"
            "✅ 建议：请稍后重试，如持续出现请联系管理员检查 API 配置。"
        )

    # 空响应（无 candidates）
    if "缺少 candidates" in error_str or "no candidates" in error_str:
        return (
            "⚠️ 图像生成失败：API 返回了空响应。\n"
            "🧐 原因：请求可能被过滤或服务端无法处理。\n"
            "✅ 建议：请检查提示词是否合适，如持续出现请联系管理员。"
        )

    # 默认通用错误
    return (
        f"❌ 图像生成失败：{original_error}\n"
        "🧐 可能原因：网络波动、配置缺失或依赖加载失败。\n"
        "✅ 建议：请稍后重试，如持续出现请联系管理员查看日志。"
    )
