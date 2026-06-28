# 新增 API 供应商（适配器开发指南）

本插件通过 `provider_settings.provider_overrides` 中的供应商模板生成候选配置，每个候选携带 canonical `api_type`，再由 `ProviderSpec` 表选择供应商实现。供应商代码集中在 `tl/api/`，对外统一由 `tl/tl_api.py` 调用。

## 架构概览

```text
tl/
├── provider_metadata.py # ProviderSpec 表，唯一代码注册点
├── provider_loader.py   # provider class / hook callable 懒加载
├── provider_hooks.py    # provider 专属配置、能力、候选配置、工具 profile hook
├── provider_settings.py # 共享 provider settings 读取 helper
├── api_normalize.py     # canonical api_type 归一化
└── api/
    ├── base.py             # ApiProvider / ProviderRequest 接口定义
    ├── registry.py         # 按 ProviderSpec 懒加载 provider 实例
    ├── openai_compat.py    # OpenAI Chat Completions 兼容基类
    ├── openai_images.py    # OpenAI Images 原生端点
    ├── google.py           # Google/Gemini 官方接口
    ├── agnes_ai.py         # Agnes AI 图片生成接口
    ├── xai.py              # xAI Images 官方接口
    ├── minimax.py          # MiniMax 图片生成接口
    ├── doubao.py           # 火山引擎 Ark / 豆包 Seedream
    ├── zai.py              # Zai 适配
    ├── grok2api.py         # grok2api 适配
    ├── sensenova.py        # SenseNova(商汤日日新)
    ├── stepfun.py          # StepFun
    ├── provider_limits.py  # 各 provider 参考图上限常量(共享)
    ├── reference_intake.py # 参考图接收阶段日志助手(共享)
    └── data_uri.py         # data URI / base64 助手(共享)
```

当前 `ProviderSpec` 顺序与 `_conf_schema.json` 中 `provider_settings.provider_overrides.templates` 严格一致，不再提供别名：

| `api_type` | Provider | 说明 |
|------------|----------|------|
| `google` | `GoogleProvider` | Google/Gemini 官方接口 |
| `openai` | `OpenAICompatProvider` | OpenAI Chat Completions 兼容格式（默认兜底） |
| `zai` | `ZaiProvider` | Zai 兼容接口 |
| `grok2api` | `Grok2ApiProvider` | grok2api 兼容接口 |
| `agnes_ai` | `AgnesAIProvider` | Agnes AI `/v1/images/generations` |
| `xai` | `XAIProvider` | xAI 官方图像接口 |
| `minimax` | `MiniMaxProvider` | MiniMax `/v1/image_generation` |
| `stepfun` | `StepfunProvider` | StepFun `/v1/images/generations` 与 `/v1/images/edits` |
| `openai_images` | `OpenAIImagesProvider` | OpenAI `/v1/images/generations` 与 `/v1/images/edits` |
| `doubao` | `DoubaoProvider` | 火山引擎 Ark / 豆包 |
| `sensenova` | `SenseNovaProvider` | SenseNova（商汤日日新）`/v1/images/generations`（仅文生图，11 种固定尺寸） |
| 未知值 | - | 配置加载阶段记录错误并跳过，不进入轮询候选 |

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
        request_config: ApiRequestConfig | None = None,
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

## 公共辅助模块（推荐复用）

`tl/api/` 下抽出了若干共享 helper,新供应商应优先使用,避免重复造轮子:

### `provider_limits.py` — 各 provider 参考图上限常量

集中维护 `MAX_REFERENCE_IMAGES_*` 常量,避免魔法数字散落各 provider:

```python
from .provider_limits import (
    MAX_REFERENCE_IMAGES_GOOGLE,        # 14
    MAX_REFERENCE_IMAGES_DOUBAO,        # 14
    MAX_REFERENCE_IMAGES_OPENAI_COMPAT, # 6
    MAX_REFERENCE_IMAGES_MINIMAX,       # 9
)
```

新增 provider 若有专属上限,在此模块新增 `Final[int]` 常量并在 provider 内引用。

### `reference_intake.py` — 参考图接收阶段的统一日志

```python
from .reference_intake import announce_reference_intake

received, accepted = announce_reference_intake(
    references,
    max_count=MAX_REFERENCE_IMAGES_OPENAI_COMPAT,
    log_prefix="[my_provider] ",
)
```

返回 `(收到数量, 实际采用数量)`,并产出统一格式的日志。**不**做截断 — 上限超过时仅打 warning,截断仍由各 provider 自行决定。

### `data_uri.py` — Data URI / Base64 助手

```python
from .data_uri import format_data_uri, strip_data_uri_prefix, looks_like_base64

uri = format_data_uri("image/png", b64_str)   # "data:image/png;base64,..."
pure = strip_data_uri_prefix(maybe_uri)        # 去掉前缀仅留 base64 主体
ok = looks_like_base64(some_str)               # 启发式判断是否疑似 base64
```

`doubao` / `minimax` / `openai_compat` 均已迁移至此模块。新 provider 处理图像 base64 时直接用模块函数即可。

### `tl/api_headers.py`(注:在 `tl/` 下,非 `tl/api/`)

API Key 在请求头中的读写:

```python
from ..api_headers import extract_api_key_from_headers, apply_api_key_to_headers
```

provider 通常无需直接调用(由 `tl_api.py` 在重试逻辑中使用),仅在自定义 Key 轮换时可能用到。

## 注册供应商

新增标准供应商时，代码侧只需要新增 provider 实现类，并在 `tl/provider_metadata.py` 的 `_PROVIDER_SPECS` 中增加一条 `ProviderSpec`。`registry.py` 不再手写 provider import，也不再维护 `_PROVIDERS` 字典。

```python
ProviderSpec(
    "my_provider",
    "tl.api.my_provider.MyProvider",
    settings_attr="my_provider_settings",
)
```

`ProviderSpec` 字段说明：

| 字段 | 说明 |
|------|------|
| `api_type` | canonical 供应商 key，必须与 `_conf_schema.json` 模板 key 一致 |
| `provider_path` | provider 类路径，registry 会按需懒加载 |
| `supports_image_edit` | 默认是否支持参考图/改图请求 |
| `settings_attr` | 兼容旧字段投影，例如 `openai_images_settings`；新代码优先读 `provider_settings_by_type` |
| `model_field` | 模型字段名，默认 `model`；豆包使用 `endpoint_id` |
| `settings_validator_path` | 配置校验 hook，例如 OpenAI Images 尺寸校验 |
| `settings_normalizer_path` | 配置归一化 hook，例如豆包默认提示词优化模式 |
| `edit_capability_path` | 候选级改图能力 hook，例如 `openai_images.generations_only` |
| `candidate_config_hook_path` | 候选请求配置 hook，例如 OpenAI Images custom size |
| `tool_profile_path` | LLM Tool 参数行为 hook，例如 OpenAI Images custom size 工具模式 |
| `rebuild_on_retry` | 重试时是否重新调用 provider `build_request()` |
| `retry_error_arg` | 重试 rebuild 时是否传入 `retry_error` |
| `parse_errors_with_provider` | 非 200/配额/权限响应是否交给 provider `parse_response()` |

如果 provider 没有特殊行为，只填写 `api_type`、`provider_path`，按需补 `settings_attr` 即可。新增特殊配置清洗、重试降级、错误解析或工具参数行为时，先在 `tl/provider_hooks.py` 增加 hook，再通过 spec 的 `*_path` 字段声明，不要在 `tl_api.py`、`plugin_config.py`、`main.py` 或 `tl/llm_tools.py` 新增 provider 名称分支。

> **只使用 canonical key**：所有 canonical `api_type` 名称必须与 `_conf_schema.json` 中 `provider_settings.provider_overrides.templates` 严格一致（全小写、下划线分隔）；代码内不再维护其他别名。`normalize_api_type()` 仅做小写、去空格、`-` 转 `_` 三项面向输入的宽容处理。

## 更新配置和文档

新增供应商后至少同步这些文件：

| 文件 | 需要更新的内容 |
|------|----------------|
| `tl/provider_metadata.py` | `_PROVIDER_SPECS` 中新增 `ProviderSpec` |
| `_conf_schema.json` | `provider_settings.provider_overrides.templates` 中新增模板 |
| `tests/test_provider_registry.py` | spec、schema、provider path、hook path 一致性会自动覆盖；必要时补专属行为测试 |
| `docs/config.md` | 配置项说明、默认值、供应商行为差异 |
| `README.md` | 多供应商列表和最小配置说明 |
| `CHANGELOG.md` | 新增供应商、行为变更和兼容性说明 |

旧的 `*_settings` 字段仍作为兼容投影保留，但新运行时代码应优先使用 `provider_settings_by_type` 或 `tl/provider_settings.py` 中的 helper，避免同类型多候选只读到第一条。

`_conf_schema.json` 示例：

```json
{
  "provider_settings": {
    "items": {
      "provider_overrides": {
        "templates": {
          "my_provider": {
            "description": "My Provider 配置",
            "items": {
              "priority": {"type": "int", "default": 0},
              "api_keys": {"type": "list", "default": []},
              "model": {"type": "string", "default": "my-image-model"},
              "resolution": {"type": "string", "default": "1K"},
              "aspect_ratio": {"type": "string", "default": "1:1"},
              "max_reference_images": {"type": "int", "default": 6}
            }
          }
        }
      }
    }
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
- `tests/test_provider_registry.py` 中的 spec/schema/path 一致性测试通过。
- 运行 `ruff format .`、`ruff check .`、`python -m pytest`。

## 常见坑位

| 问题 | 原因 | 处理 |
|------|------|------|
| 新 `api_type` 没生效 | 未注册或 canonical 名称与 `_conf_schema.json` 不一致 | 检查 `tl/provider_metadata.py` 中 `ProviderSpec` 和 `_conf_schema.json` 中 provider 模板名 |
| 图片重复发送 | 响应结构和文本回退都提取到同一张图 | 在 provider 内做去重，或避免无条件文本扫描 |
| URL 过期 | 返回的是临时缓存链接 | 在 provider 内立即下载并返回 `image_paths` |
| 相对路径无法访问 | 响应只给 `/images/...` | 使用 `api_base` 拼接 origin 后下载 |
| 改图失败 | 供应商不支持当前参考图格式 | 在 `build_request()` 中统一转 URL、base64、data URI 或 multipart |
| WebUI 配置缺项 | 只改代码没改 schema | 同步 `_conf_schema.json` 和 `docs/config.md` |
