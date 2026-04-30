# H3 — 拆分 tl_api.py(分阶段;本阶段:API 头处理工具)

> 状态: 第一阶段实施
> 调整: tl_api.py ~1860 行,核心是单个巨型 `GeminiAPIClient` 类(50+ 方法),方法间通过 `self.session` / `self.key_manager` 等紧耦合。原 plan 设想拆出 `api_response_parser.py` / `image_downloader.py` / `api_retry.py`,但响应解析与下载方法都依赖实例状态,直接拆分会引入 mixin 复杂度。
>
> 改为 **分阶段**,从纯函数 / 已是 `@staticmethod` 的辅助函数开始抽取。本阶段提取 API Key 头处理两个 staticmethod。

## 本阶段范围

新建 `tl/api_headers.py`,导出:
- `extract_api_key_from_headers(headers) -> str | None`
- `apply_api_key_to_headers(headers, api_key) -> None`

`tl/tl_api.py` 改动:
- 删除 `GeminiAPIClient._extract_api_key_from_headers` / `_apply_api_key_to_headers` 两个 staticmethod 定义
- 顶部 import 新模块
- 将两处 `self._extract_api_key_from_headers(` 改为 `extract_api_key_from_headers(`
- 将一处 `self._apply_api_key_to_headers(` 改为 `apply_api_key_to_headers(`

## 风险

零运行时风险:函数纯净,无 `self` 依赖;调用点只有内部 3 处。

## 后续阶段(本次不做)

| 阶段 | 目标 | 阻碍 |
|---|---|---|
| H3-2 | `_ensure_mime_type`、`_first_usage_value`、`_classify_error`、`_is_retryable_error` 抽到 `tl/api_retry_utils.py` | `_classify_error/_is_retryable_error` 形式上是 method,但只用 `self` 名义参数,可改 module 函数 |
| H3-3 | 响应解析方法 (`_parse_gresponse` / `_parse_doubao_response` / `_parse_openai_response` 等) → `api_response_parser.py` | 高耦合 `self.session` / `self.key_manager`,需要保留 method 包装层或用 mixin |
| H3-4 | `_download_image` → `image_downloader.py` | 需要传递 session/proxy 配置 |

## 验证

1. ruff format + check
2. compileall
3. grep 确认无残留 `self._extract_api_key_from_headers` / `self._apply_api_key_to_headers`
