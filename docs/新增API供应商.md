# 新增 API 供应商（适配器开发指南）

本插件采用 **供应商适配器** 架构，通过 `api_type` 配置项选择不同的 API 供应商实现。所有供应商代码位于 `tl/api/` 目录下，彼此隔离互不影响。

本文档将指导你如何为插件添加新的 API 供应商支持。

---

## 架构概览

```
tl/api/
├── base.py           # 接口定义（Protocol）
├── registry.py       # 供应商注册表
├── openai_compat.py  # OpenAI 兼容基类（推荐继承）
├── google.py         # Google/Gemini 官方实现
├── zai.py            # Zai 适配（继承 OpenAI 兼容）
└── grok2api.py       # grok2api 适配（继承 OpenAI 兼容）
```

### 核心接口

每个供应商需要实现 `ApiProvider` 协议（定义于 `base.py`）：

```python
class ApiProvider(Protocol):
    name: str

    async def build_request(
        self, *, client: Any, config: ApiRequestConfig
    ) -> ProviderRequest:
        """构建 API 请求（URL、Headers、Payload）"""
        ...

    async def parse_response(
        self,
        *,
        client: Any,
        response_data: dict[str, Any],
        session: aiohttp.ClientSession,
        api_base: str | None = None,
    ) -> tuple[list[str], list[str], str | None, str | None]:
        """解析响应，返回 (image_urls, image_paths, text_content, thought_signature)"""
        ...
```

### 返回值说明

`parse_response()` 返回一个四元组：

| 索引 | 名称 | 类型 | 说明 |
|------|------|------|------|
| 0 | `image_urls` | `list[str]` | 图片 URL 列表（可供用户直接访问） |
| 1 | `image_paths` | `list[str]` | 本地图片路径列表（已下载的图片） |
| 2 | `text_content` | `str \| None` | 模型返回的文本内容 |
| 3 | `thought_signature` | `str \| None` | 思考过程签名（如有） |

---

## 第一步：判断供应商类型

### 类型 A：OpenAI 兼容网关（推荐）

大多数第三方 API 都兼容 OpenAI 格式，区别仅在于：
- 请求参数字段名不同
- 返回的图片链接格式特殊（相对路径、临时缓存等）

**推荐**：继承 `OpenAICompatProvider`，只需覆盖差异部分。

### 类型 B：完全独立协议

请求/响应结构与 OpenAI 差异较大（如 Google 官方 API）。

**推荐**：参考 `google.py` 从头实现。

---

## 第二步：创建 Provider 文件

在 `tl/api/` 下新建文件，例如 `my_provider.py`：

### 示例：继承 OpenAI 兼容基类

```python
"""MyProvider 兼容模式供应商实现。"""

from __future__ import annotations

from typing import Any

import aiohttp

from astrbot.api import logger

from ..api_types import ApiRequestConfig
from .openai_compat import OpenAICompatProvider


class MyProvider(OpenAICompatProvider):
    name = "my_provider"

    # 可选：自定义请求参数
    async def _prepare_payload(
        self, *, client: Any, config: ApiRequestConfig
    ) -> dict[str, Any]:
        payload = await super()._prepare_payload(client=client, config=config)
        
        # 示例：修改参数结构
        # payload["custom_field"] = "value"
        
        return payload

    # 可选：处理特殊图片 URL
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
        # 返回 True 表示已处理该 URL，不再走默认逻辑
        # 返回 False 表示交给基类处理
        return False

    # 可选：从文本中提取额外图片链接
    def _find_additional_image_urls_in_text(self, text: str) -> list[str]:
        return []
```

### OpenAICompatProvider 扩展点

| 方法 | 用途 | 常见场景 |
|------|------|----------|
| `_prepare_payload()` | 自定义请求参数 | 修改字段名、添加额外参数 |
| `_handle_special_candidate_url()` | 处理特殊图片 URL | 相对路径、临时缓存链接 |
| `_find_additional_image_urls_in_text()` | 从文本提取图片链接 | Markdown 中的图片引用 |

---

## 第三步：注册供应商

修改 `tl/api/registry.py`：

```python
# 1. 导入你的 Provider
from .my_provider import MyProvider

# 2. 创建单例
_MY: Final[MyProvider] = MyProvider()

# 3. 在 get_api_provider() 中添加映射
def get_api_provider(api_type: str | None) -> ApiProvider:
    normalized_raw = (api_type or "").strip().lower()
    normalized = normalized_raw.replace("-", "_")

    # 添加你的供应商判断
    if normalized in {"my_provider", "myprovider"}:
        return _MY

    # ... 其他供应商 ...

    return _OPENAI  # 默认回退到 OpenAI 兼容
```

**建议**：做归一化处理，支持 `my-provider`、`my_provider`、`myprovider` 等别名。

---

## 第四步：更新配置文件

### 修改 `_conf_schema.json`

在 `api_settings.api_type.options` 中添加新类型：

```json
{
  "api_type": {
    "description": "API 类型（必选）",
    "type": "string",
    "hint": "...; 选择 my_provider 时启用 XXX 兼容",
    "default": "openai",
    "options": [
      "google",
      "openai",
      "zai",
      "grok2api",
      "my_provider"
    ]
  }
}
```

### 修改 `README.md`

在配置说明部分添加新类型的描述：

```markdown
- `my_provider`：支持 XXX 特殊参数传递
```

---

## 实战参考：grok2api 适配

`grok2api.py` 是一个很好的参考示例，它解决了两个典型问题：

### 问题 1：相对路径图片

API 返回 `/images/xxx` 格式的相对路径，需要拼接 `api_base`：

```python
async def _handle_special_candidate_url(self, ..., api_base, ...):
    is_relative = candidate_url.startswith("/")
    
    if is_relative:
        origin = self._origin_from_api_base(api_base)
        full_url = urllib.parse.urljoin(origin, candidate_url)
        _, image_path = await client._download_image(full_url, session)
        if image_path:
            image_paths.append(image_path)
        return True  # 已处理
    
    return False
```

### 问题 2：临时缓存链接

部分 URL 会过期，需要立即下载：

```python
@staticmethod
def _is_temp_cache_url(url: str) -> bool:
    return "/images/users-" in url or "/temp/image/" in url

async def _handle_special_candidate_url(self, ...):
    if self._is_temp_cache_url(candidate_url):
        _, image_path = await client._download_image(candidate_url, session)
        if image_path:
            image_paths.append(image_path)
        return True
    return False
```

---

## 实战参考：Zai 适配

`zai.py` 展示了如何修改请求参数结构：

```python
class ZaiProvider(OpenAICompatProvider):
    name = "zai"

    async def _prepare_payload(self, *, client, config):
        payload = await super()._prepare_payload(client=client, config=config)
        
        # Zai 需要特殊的参数结构：顶层 + generation_config
        payload.pop("image_config", None)
        
        generation_config = {}
        if config.resolution:
            payload["image_size"] = config.resolution
            generation_config["image_size"] = config.resolution
        if config.aspect_ratio:
            payload["aspect_ratio"] = config.aspect_ratio
            generation_config["aspect_ratio"] = config.aspect_ratio
        
        if generation_config:
            payload["generation_config"] = generation_config
        
        return payload
```

---

## 常见问题

### Q1：如何获取 api_base？

`api_base` 会透传到 `parse_response()` 和 `_handle_special_candidate_url()` 方法中，可直接使用。

### Q2：如何下载图片？

使用 `client._download_image()` 方法：

```python
_, image_path = await client._download_image(url, session, use_cache=False)
```

### Q3：如何避免重复处理同一图片？

使用 `state` 参数维护已处理 URL 集合：

```python
async def _handle_special_candidate_url(self, ..., state, ...):
    seen = state.setdefault("seen_special_urls", set())
    if candidate_url in seen:
        return True  # 已处理，跳过
    seen.add(candidate_url)
    # ... 处理逻辑 ...
```

### Q4：ApiRequestConfig 包含哪些字段？

主要字段（定义于 `tl/api_types.py`）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `prompt` | `str` | 用户提示词 |
| `model` | `str` | 模型名称 |
| `api_key` | `str` | API 密钥 |
| `api_base` | `str \| None` | API 基础 URL |
| `reference_images` | `list[str]` | 参考图片列表 |
| `resolution` | `str \| None` | 分辨率（1K/2K/4K） |
| `aspect_ratio` | `str \| None` | 长宽比（1:1/16:9等） |
| `resolution_param_name` | `str \| None` | 分辨率参数名 |
| `aspect_ratio_param_name` | `str \| None` | 长宽比参数名 |
| `force_resolution` | `bool` | 是否强制传分辨率参数 |
| `enable_grounding` | `bool` | 是否启用搜索接地 |

---

## 开发自检清单

完成开发后，请确保：

- [ ] 代码无语法错误：`python -m compileall -q .`
- [ ] 代码风格检查：`ruff check tl/api/`
- [ ] 已注册到 `registry.py`
- [ ] 已更新 `_conf_schema.json` 配置选项
- [ ] 已更新 `README.md` 文档说明
- [ ] 实际测试生图/改图功能正常

---

## 常见坑位

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| 切图失败 | URL 被当作本地路径 | 先下载图片，返回本地路径 |
| 图片重复 | 同一图片在多处出现 | 使用 `state` 去重 |
| 图片过期 | 临时缓存链接失效 | 强制下载并返回本地路径 |
| 相对路径无法访问 | 缺少 `api_base` | 确保使用 `api_base` 拼接完整 URL |
| 参考图处理失败 | MIME 类型不支持 | 检查支持的格式列表 |

