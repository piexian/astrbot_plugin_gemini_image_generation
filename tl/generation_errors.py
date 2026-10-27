"""旧路径 APIError 分支与 v2 失败提示共享的用户文案拼装。"""

from __future__ import annotations


def api_failure_message(
    *,
    message: str,
    status_code: int | None,
    error_type: str | None,
    total_timeout: int,
) -> str:
    """按错误类型/状态码拼装图像生成失败提示（措辞与旧路径 APIError 分支一致）。"""

    status_part = f"（状态码 {status_code}）" if status_code is not None else ""
    error_msg = f"❌ 图像生成失败{status_part}：{message}"
    message_lower = (message or "").lower()
    if error_type in ("timeout", "cancelled"):
        error_msg += (
            f"\n🧐 可能原因：图像生成耗时超出插件超时限制（当前 {total_timeout} 秒）。"
            "\n✅ 建议：在插件配置中增加 total_timeout 与轮询超时，或简化提示词/减少参考图。"
        )
    elif error_type == "network":
        error_msg += "\n🧐 可能原因：网络连接异常或上游服务不可达。\n✅ 建议：检查网络连接和 API 地址配置，稍后重试。"
    elif status_code == 429:
        error_msg += "\n🧐 可能原因：请求过于频繁或额度已用完。\n✅ 建议：稍等片刻再试，或在配置中增加可用额度/开启智能重试。"
    elif status_code == 402:
        error_msg += "\n🧐 可能原因：账户余额不足或套餐到期。\n✅ 建议：充值或更换一组可用的 API 密钥后再试。"
    elif status_code == 403:
        error_msg += "\n🧐 可能原因：API 密钥无效、权限不足或访问受限。\n✅ 建议：核对密钥权限、检查 IP 白名单，必要时重新生成密钥。"
    elif status_code is not None and 500 <= status_code < 600:
        error_msg += "\n🧐 可能原因：上游服务暂时不可用。\n✅ 建议：稍后重试，若频繁出现请联系服务提供方确认故障。"
        # t2i 公共服务繁忙提示
        if "t2i" in message_lower:
            error_msg += (
                "\n⚠️ t2i 公共服务器当前可能繁忙，建议稍后再试；"
                "如需稳定产能可参考 https://docs.astrbot.app/others/self-host-t2i.html 自建。"
            )
    elif error_type == "workspace_unavailable":
        error_msg += "\n🧐 可能原因：无法解析当前会话的工作区目录。\n✅ 建议：查看日志确认框架工作区服务状态后重试。"
    else:
        error_msg += "\n🧐 可能原因：请求参数异常或服务返回未知错误。\n✅ 建议：简化提示词/减少参考图后重试，并查看日志获取更多细节。"
    return error_msg
