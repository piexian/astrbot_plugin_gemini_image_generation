# 配置参考

本文档记录插件的完整配置项。README 只保留最小配置和常用入口。

## 必填配置

| 配置项 | 说明 |
|--------|------|
| `api_settings.provider_id` | 生图模型提供商，从 AstrBot 提供商列表选择；豆包可不填 |
| `api_settings.api_type` | API 类型：`google` / `openai` / `openai_images` / `xai` / `zai` / `grok2api` / `doubao` |

## api_settings

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `provider_id` | - | 生图模型提供商 |
| `api_type` | `openai` | API 类型 |
| `model` | - | 可选，覆盖提供商模型名称 |
| `proxy` | - | 全局代理地址，支持 `http://`、`https://`、`socks5://`；留空读取环境变量 |
| `vision_provider_id` | - | 可选，用于切图前 AI 识别网格行列 |
| `provider_overrides` | `[]` | 可选，按 API 类型覆盖密钥、模型、端点、代理和每日限额 |

## api_settings.provider_overrides

`provider_overrides` 是 `template_list` 配置项。选择对应模板后，该模板内的配置会优先于 AstrBot 提供商配置：

| 通用配置项 | 默认值 | 说明 |
|------------|--------|------|
| `api_keys` | `[]` | API Key 列表，支持多 Key 轮换 |
| `daily_limit_per_key` | `0` | 每个 Key 每日调用上限，`0` 表示不限制 |
| `model` | - | 模型名称；豆包使用 `endpoint_id` |
| `api_base` | - | API 端点地址 |
| `proxy` | - | 独立代理地址，优先级高于全局代理和环境变量 |

支持的模板：

```text
google / openai / zai / grok2api / xai / openai_images / doubao
```

下方 `doubao_settings`、`openai_images_settings`、`xai_settings` 章节对应这些模板的专用字段。旧版顶层配置仅作为兼容读取路径，新配置建议统一放在 `api_settings.provider_overrides` 中。

## image_generation_settings

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `resolution` | `1K` | 分辨率：`1K` / `2K` / `4K` |
| `aspect_ratio` | `1:1` | 长宽比：`1:1` / `16:9` / `4:3` / `3:2` / `9:16` / `4:5` / `5:4` / `21:9` / `3:4` / `2:3` |
| `enable_sticker_split` | `true` | 表情包自动切割 |
| `enable_sticker_zip` | `false` | 切分后打包 ZIP 发送 |
| `sticker_grid` | `4x4` | 表情包提示词网格描述 |
| `preserve_reference_image_size` | `false` | 改图时保留参考图尺寸 |
| `enable_grounding` | `false` | Gemini 搜索接地 |
| `max_reference_images` | `6` | 最大参考图数量 |
| `enable_text_response` | `false` | 同时返回文本说明 |
| `force_resolution` | `false` | 强制传分辨率参数 |
| `resolution_param_name` | `image_size` | 自定义分辨率参数名 |
| `aspect_ratio_param_name` | `aspect_ratio` | 自定义长宽比参数名 |
| `max_inline_image_size_mb` | `2.0` | 本地图片 base64 编码阈值 |
| `llm_tool_timeout_reserve_percent` | `50` | 为 `tool_call_timeout` 预留的百分比，剩余时间用于前台同步等待 |

## quick_mode_settings

可覆盖各快速模式的默认分辨率和长宽比。支持模式：

```text
avatar / poster / wallpaper / card / mobile / figure / sticker
```

## retry_settings

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `max_attempts_per_key` | `3` | 每个密钥最大重试次数 |
| `enable_smart_retry` | `true` | 按错误类型智能重试 |
| `total_timeout` | `120` | 单次调用总超时，单位秒 |

## service_settings

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `nap_server_address` | `localhost` | NAP 文件传输地址 |
| `nap_server_port` | `3658` | NAP 文件传输端口 |
| `auto_avatar_reference` | `false` | 自动获取头像作为参考图 |
| `theme_settings.mode` | `cycle` | 帮助页主题模式 |

## help_render_mode

| 值 | 说明 |
|----|------|
| `html` | 使用 t2i 网络服务渲染，默认 |
| `local` | 本地 Pillow 渲染 |
| `text` | 纯文本输出 |

## limit_settings

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `group_limit_mode` | `none` | 群限制模式：`none` / `whitelist` / `blacklist` |
| `group_limit_list` | `[]` | 群号列表 |
| `rate_limit_rules` | `[]` | 限流规则列表，`template_list` 格式 |
| `default_rate_limit.enabled` | `false` | 默认限流开关，未匹配规则时使用 |
| `default_rate_limit.period_seconds` | `60` | 默认限流周期，单位秒 |
| `default_rate_limit.max_requests` | `5` | 默认单群周期内最大请求数 |

## cache_settings

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `cache_ttl_minutes` | `5` | 缓存保留时间，单位分钟 |
| `cleanup_interval_minutes` | `30` | 清理间隔，单位分钟 |
| `max_cache_files` | `100` | 缓存文件数量上限 |

## doubao_settings（豆包生图专用配置）

配置路径：`api_settings.provider_overrides` 中选择 `doubao` 模板。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `api_keys` | `[]` | 火山引擎 API Key 列表，支持多 Key 轮换 |
| `daily_limit_per_key` | `0` | 每个 Key 每日调用上限，`0` 表示不限制 |
| `endpoint_id` | `doubao-seedream-4-5-251128` | Endpoint/Model ID，例如 `ep-xxxx` 或 `doubao-seedream-4-5-251128` |
| `api_base` | `https://ark.cn-beijing.volces.com` | API 端点地址 |
| `default_size` | `2K` | 默认尺寸，支持 `2K` / `4K` 或具体尺寸 |
| `watermark` | `false` | 是否添加水印 |
| `optimize_prompt_mode` | `standard` | 提示词优化模式：`standard` / `fast` |
| `sequential_image_generation` | `disabled` | 组图生成模式：`disabled` / `auto` |
| `sequential_max_images` | `4` | 组图最大数量，范围 `2-15` |

豆包组图官方文档：<https://www.volcengine.com/docs/82379/1824121?lang=zh#fc9f85e4>

## openai_images_settings（OpenAI Images API 专用配置）

配置路径：`api_settings.provider_overrides` 中选择 `openai_images` 模板。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `api_keys` | `[]` | API Key 列表，支持多 Key 轮换 |
| `daily_limit_per_key` | `0` | 每个 Key 每日调用上限，`0` 表示不限制 |
| `model` | `gpt-image-1` | 模型名称，例如 `dall-e-2` / `dall-e-3` / `gpt-image-1` / `gpt-image-2` |
| `api_base` | - | API 端点地址，留空使用 OpenAI 官方 |
| `quality` | - | 图像质量。GPT image：`auto` / `high` / `medium` / `low`；dall-e-3：`hd` / `standard` |
| `response_format` | `b64_json` | 响应格式：`b64_json` / `url` |
| `size_mode` | `preset` | 尺寸模式：`preset` 使用全局分辨率映射；`custom` 使用 `custom_size` |
| `custom_size` | `1024x1024` | 自定义尺寸，仅 `size_mode=custom` 生效。格式 `WxH`，支持 `x` 或 `×` |
| `style` | - | 图像风格，仅 dall-e-3：`vivid` / `natural` |
| `background` | - | 背景透明度，仅 GPT image：`auto` / `transparent` / `opaque` |
| `output_format` | - | 输出格式，仅 GPT image：`png` / `jpeg` / `webp` |
| `output_compression` | `0` | 输出压缩率 `0-100`，`0` 表示不传，仅 GPT image + jpeg/webp |
| `moderation` | - | 审核模式，仅 GPT image，例如 `low` |
| `generations_only` | `false` | 开启后强制只用 `/v1/images/generations`，不走 `/v1/images/edits` |

### OpenAI Images 自定义尺寸

`size_mode=custom` 时，插件会在发送请求前校验 `custom_size`：

- 最大边 `<= 3840`
- 宽高均为 `16` 的倍数
- 长短边比 `<= 3:1`
- 总像素在 `655360-8294400` 之间

官方文档：

- <https://developers.openai.com/api/docs/guides/image-generation>
- <https://developers.openai.com/api/docs/models/gpt-image-2>

`size_mode=custom` 各调用路径行为：

| 调用路径 | `size` 取值 |
|----------|-------------|
| 普通生图/改图 | 直接使用配置中的 `custom_size` |
| 快速模式 | 根据模式预设的 `resolution + aspect_ratio` 自动换算，例如 `2K + 16:9 -> 2048x1152` |
| LLM 工具调用 | LLM 显式传入 `size` 时以该值为准，否则使用配置中的 `custom_size` |

`size_mode=custom` 时，LLM 工具仅暴露 `size` 参数，不再接受 `resolution` / `aspect_ratio`。传入非法值时会直接返回校验错误。

## xai_settings（xAI Images API 专用配置）

配置路径：`api_settings.provider_overrides` 中选择 `xai` 模板。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `api_keys` | `[]` | API Key 列表，支持多 Key 轮换 |
| `daily_limit_per_key` | `0` | 每个 Key 每日调用上限，`0` 表示不限制 |
| `model` | `grok-imagine-image` | xAI 图像模型名称 |
| `api_base` | `https://api.x.ai` | API 端点地址 |
| `response_format` | `url` | 响应格式：`url` / `b64_json` |
| `quality` | - | 透传给 xAI 图片接口，留空不传 |
| `n` | `1` | 单次请求生成数量，当前最多 `10` |
| `proxy` | - | 独立代理地址 |

`xai` 供应商会自动走 xAI 官方 JSON 图像接口：

- 文生图：`/v1/images/generations`
- 改图：`/v1/images/edits`

改图请求会把参考图统一内联为 `data URI`，不使用 `multipart/form-data`。xAI 官方文档当前说明单次编辑最多支持 `5` 张参考图，分辨率支持 `1k/2k`，单图编辑时输出比例默认跟随输入图。
