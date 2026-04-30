# 新增 API 供应商（适配器开发指南）

本插件通过 `api_type` 选择不同的 API 供应商实现。供应商代码集中在 `tl/api/`，对外统一由 `tl/tl_api.py` 调用。

## 架构概览

```text
tl/api/
├── base.py           # ApiProvider / ProviderRequest 接口定义
├── registry.py       # api_type -> Provider 注册表
├── openai_compat.py  # OpenAI Chat Completions 兼容基类
├── openai_images.py  # OpenAI Images 原生端点
├── google.py         # Google/Gemini 官方接口
├── xai.py            # xAI Images 官方接口
├── minimax.py        # MiniMax 图片生成接口
├── doubao.py         # 火山引擎 Ark / 豆包 Seedream
├── zai.py            # Zai 适配
└── grok2api.py       # grok2api 适配
```

当前注册映射（与 `_conf_schema.json` 中 `api_settings.api_type.options` 严格一致，不再提供别名）：

| `api_type` | Provider | 说明 |
|------------|----------|------|
| `google` | `GoogleProvider` | Google/Gemini 官方接口 |
| `openai` | `OpenAICompatProvider` | OpenAI Chat Completions 兼容格式（默认兜底） |
| `openai_images` | `OpenAIImagesProvider` | OpenAI `/v1/images/generations` 与 `/v1/images/edits` |
| `xai` | `XAIProvider` | xAI 官方图像接口 |
| `minimax` | `MiniMaxProvider` | MiniMax `/v1/image_generation` |
| `stepfun` | `StepfunProvider` | StepFun `/v1/images/generations` 与 `/v1/images/edits` |
| `sensenova` | `SenseNovaProvider` | SenseNova（商汤日日新）`/v1/images/generations`（仅文生图，11 种固定尺寸） |
| `zai` | `ZaiProvider` | Zai 兼容接口 |
| `grok2api` | `Grok2ApiProvider` | grok2api 兼容接口 |
| `doubao` | `DoubaoProvider` | 火山引擎 Ark / 豆包 |
| 未知值 | `OpenAICompatProvider` | 默认兜底 |

## 核心接口

每个供应商需要实现 `ApiProvider` 协议：

```python
class ApiProvider(Protocol):
    name: str

    async def build_request(
        self, *, client: Any, config: ApiRequestConfig
    ) -> ProviderRequest:
        ...

    async def parse_response(
        self,
        *,
        client: Any,
        response_data: dict[str, Any],
        session: aiohttp.ClientSession,
        api_base: str | None = None,
        http_status: int | None = None,
    ) -> tuple[list[str], list[str], str | None, str | None]:
        ...
```

`build_request()` 返回 `ProviderRequest(url, headers, payload)`。`parse_response()` 返回：

| 返回值 | 类型 | 说明 |
|--------|------|------|
| `image_urls` | `list[str]` | 可直接发送或展示的图片 URL |
| `image_paths` | `list[str]` | 已下载到本地的图片路径 |
| `text_content` | `str \| None` | 模型返回的文本 |
| `thought_signature` | `str \| None` | 协议层调试元数据，通常不展示给用户 |

## 选择实现方式

### OpenAI Chat Completions 兼容服务

如果接口形态接近 `/v1/chat/completions`，优先继承 `OpenAICompatProvider`。常见只需要覆盖：

| 扩展点 | 用途 |
|--------|------|
| `_prepare_payload()` | 调整请求参数、字段名或额外配置 |
| `_handle_special_candidate_url()` | 处理相对路径、临时缓存 URL、必须立即下载的图片 |
| `_find_additional_image_urls_in_text()` | 从 Markdown 或文本中补充提取图片链接 |

示例：

```python
from __future__ import annotations

from typing import Any

import aiohttp

from ..api_types import ApiRequestConfig
from .openai_compat import OpenAICompatProvider


class MyProvider(OpenAICompatProvider):
    name = "my_provider"

    async def _prepare_payload(
        self, *, client: Any, config: ApiRequestConfig
    ) -> dict[str, Any]:
        payload = await super()._prepare_payload(client=client, config=config)
        payload["custom_field"] = "value"
        return payload

    async def _handle_special_candidate_url(
        self,
        *,
        client: Any,
        session: aiohttp.ClientSession,
        candidate_url: str,
        image_urls: list[str],
        image_paths: list[str],
        api_base: str | None,
        state: dict[str, Any],
    ) -> bool:
        return False
```

### 原生图像端点或独立协议

如果接口不是 Chat Completions 格式，应独立实现 `build_request()` 和 `parse_response()`。参考：

- `openai_images.py`：multipart/form-data 图像编辑、`b64_json` / `url` 响应解析、自定义尺寸校验。
- `xai.py`：JSON 图像接口、参考图转 `data URI`。
- `minimax.py`：MiniMax `/v1/image_generation`、`subject_reference` 图生图、`image_base64` / `image_urls` 响应解析。
- `doubao.py`：火山 Ark 请求结构、尺寸映射、组图参数。
- `google.py`：Google/Gemini 官方协议。

## 注册供应商

在 `tl/api/registry.py` 中导入并注册单例：

```python
from .my_provider import MyProvider

_MY_PROVIDER: Final[MyProvider] = MyProvider()
```

并在模块顶部的 `_PROVIDERS` 字典中追加映射：

```python
_PROVIDERS: Final[dict[str, ApiProvider]] = {
    # ... 其他 canonical key
    "my_provider": _MY_PROVIDER,
}
```

`get_api_provider()` 本身是一行查表：`_PROVIDERS.get(normalize_api_type(api_type), _OPENAI)`，未知 `api_type` 默认回退到 OpenAI 兼容实现。

> **只使用 canonical key**：所有 canonical `api_type` 名称必须与 `_conf_schema.json` 中 `api_settings.api_type.options` 严格一致（全小写、下划线分隔）；代码内不再维护其他别名。`normalize_api_type()` 仅做小写、去空格、`-` 转 `_` 三项面向输入的宽容处理。

## 更新配置和文档

新增供应商后至少同步这些文件：

| 文件 | 需要更新的内容 |
|------|----------------|
| `_conf_schema.json` | `api_settings.api_type.options` 和 `provider_overrides` 模板 |
| `docs/config.md` | 配置项说明、默认值、供应商行为差异 |
| `README.md` | 多供应商列表和最小配置说明 |
| `CHANGELOG.md` | 新增供应商、行为变更和兼容性说明 |

`_conf_schema.json` 示例：

```json
{
  "api_type": {
    "options": [
      "google",
      "openai",
      "openai_images",
      "xai",
      "zai",
      "grok2api",
      "doubao",
      "my_provider"
    ]
  }
}
```

## 开发自检

完成后至少检查：

- `api_type` 能正确命中 `get_api_provider()`。
- 文生图和改图路径都能构建正确请求。
- 返回 `url`、`b64_json`、本地路径等格式时不会重复发送同一图片。
- 临时 URL、相对 URL、需要代理下载的 URL 已处理。
- 配置模板能在 AstrBot WebUI 正常显示和保存。
- 运行 `uv run ruff check tl/api/`。

## 常见坑位

| 问题 | 原因 | 处理 |
|------|------|------|
| 新 `api_type` 没生效 | 未注册或 canonical 名称与 `_conf_schema.json` 不一致 | 检查 `registry.py` 中 `_PROVIDERS` 字典和 `_conf_schema.json` 中 `api_type.options` |
| 图片重复发送 | 响应结构和文本回退都提取到同一张图 | 在 provider 内做去重，或避免无条件文本扫描 |
| URL 过期 | 返回的是临时缓存链接 | 在 provider 内立即下载并返回 `image_paths` |
| 相对路径无法访问 | 响应只给 `/images/...` | 使用 `api_base` 拼接 origin 后下载 |
| 改图失败 | 供应商不支持当前参考图格式 | 在 `build_request()` 中统一转 URL、base64、data URI 或 multipart |
| WebUI 配置缺项 | 只改代码没改 schema | 同步 `_conf_schema.json` 和 `docs/config.md` |
