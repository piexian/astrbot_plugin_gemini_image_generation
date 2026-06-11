# 配置参考

本文档记录插件的完整配置项。README 只保留最小配置和常用入口。


## 必填配置

| 配置项 | 说明 |
|--------|------|
| `provider_settings.provider_overrides` | 生图供应商配置表，至少添加一条有效模板并填写 `api_keys` 和模型字段 |
| `provider_settings.provider_polling` | 可选轮询顺序；留空时按有效配置自动尝试 |

## provider_settings

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `proxy` | - | 全局代理地址，支持 `http://`、`https://`、`socks5://`；留空读取环境变量 |
| `vision_provider_id` | - | 可选，用于切图前 AI 识别网格行列 |
| `vision_model` | - | 可选，视觉识别模型名 |
| `provider_polling` | `[]` | 供应商轮询表，按列表从上到下尝试；重复项自动去重 |
| `provider_overrides` | `[]` | 生图供应商配置表，可添加多个相同类型模板 |

## provider_settings.provider_overrides

`provider_overrides` 是 `template_list` 配置项。每条模板自带供应商类型，插件会从表中读取生图 API 配置，不再使用全局 `api_type`：

| 通用配置项 | 默认值 | 说明 |
|------------|--------|------|
| `priority` | `0` | 同类型多条配置按优先级从高到低尝试；相同优先级按配置表从上到下 |
| `api_keys` | `[]` | API Key 列表，支持多 Key 轮换 |
| `daily_limit_per_key` | `0` | 每个 Key 每日调用上限，`0` 表示不限制 |
| `model` | - | 模型名称；豆包使用 `endpoint_id` |
| `api_base` | - | API 端点地址 |
| `proxy` | - | 独立代理地址，优先级高于全局代理和环境变量 |
| `resolution` | `1K` | 该供应商默认分辨率；快速模式覆盖值优先 |
| `aspect_ratio` | `1:1` | 该供应商默认长宽比；快速模式覆盖值优先 |
| `max_reference_images` | `6` | 该供应商最多使用的参考图数量 |

加载规则：

- 缺少供应商名称、未知模板、缺少模型或缺少 `api_keys` 的条目会记录配置错误并跳过。
- 只要至少有一个有效供应商候选，插件仍可继续使用；如果没有任何有效候选，加载时会记录 `未找到任何有效供应商配置`。
- 同类型多条配置先按 `priority` 从高到低排序；优先级相同时按配置表从上到下排序。
- 改图或参考图请求会跳过不支持参考图的候选，例如 `sensenova` 或开启 `generations_only` 的 `openai_images`。

`provider_polling` 只填写供应商名称，例如：

```text
google / openai_images / minimax
```

列表按从上到下尝试生成，重复名称会自动去重；未知名称会记录配置错误并跳过。留空时按配置表中有效供应商首次出现顺序自动生成轮询列表。

支持的模板：

```text
google / openai / zai / grok2api / agnes_ai / xai / minimax / stepfun / openai_images / doubao / sensenova
```

下方 `doubao_settings`、`openai_images_settings`、`agnes_ai_settings`、`xai_settings`、`minimax_settings`、`stepfun_settings`、`sensenova_settings` 章节对应这些模板的专用字段。代码中的同名 `*_settings` 字段仅作为兼容旧调用的首个候选投影；多候选场景以 `provider_settings.provider_overrides` 和运行时派生的 `provider_settings_by_type` 为准。

## image_generation_settings

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `enable_sticker_split` | `true` | 表情包自动切割 |
| `enable_sticker_zip` | `false` | 切分后打包 ZIP 发送 |
| `sticker_grid` | `4x4` | 表情包提示词网格描述 |
| `preserve_reference_image_size` | `false` | 改图时保留参考图尺寸 |
| `max_inline_image_size_mb` | `2.0` | 本地图片 base64 编码阈值 |
| `llm_tool_timeout_reserve_percent` | `50` | 为 `tool_call_timeout` 预留的百分比，剩余时间用于前台同步等待 |

分辨率、长宽比、最大参考图数量、Google 文本响应、Google 搜索接地、OpenAI/OpenAI 兼容参数名等均在 `provider_settings.provider_overrides` 的各供应商条目内配置。

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
| `napcat_stream_threshold_mb` | `2.0` | 本地图片原始发送失败后，达到该大小才使用 NapCat Stream API 兜底重试；`0` 表示禁用 |
| `auto_avatar_reference` | `false` | 自动获取头像作为参考图 |
| `show_duration_stats` | `true` | 生成完成后是否展示耗时统计 |
| `show_retry_stats` | `true` | 生成完成后是否展示重试次数 |
| `show_token_usage_stats` | `true` | 生成完成后是否展示上游返回的 token 用量 |
| `theme_settings.mode` | `cycle` | 帮助页主题模式 |

NapCat v4.8.115+ 支持 Stream API。插件默认仍先按 `max_inline_image_size_mb` 规则发送本地图片；只有原始发送失败且文件大小达到 `napcat_stream_threshold_mb` 时，才会复用当前 NapCat/OneBot 连接调用 `upload_file_stream` 并重试一次。Docker / docker compose 部署仍建议共享 `AstrBot/data` 目录，以兼容普通本地文件发送路径。

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

配置路径：`provider_settings.provider_overrides` 中选择 `doubao` 模板。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `api_keys` | `[]` | 火山引擎 API Key 列表，支持多 Key 轮换 |
| `daily_limit_per_key` | `0` | 每个 Key 每日调用上限，`0` 表示不限制 |
| `endpoint_id` | `doubao-seedream-5-0-260128` | Endpoint/Model ID，例如 `ep-xxxx` 或 `doubao-seedream-5-0-260128` |
| `api_base` | `https://ark.cn-beijing.volces.com` | API 端点地址 |
| `size_mode` | `preset` | 尺寸模式：`preset` 使用 `size`；`custom` 使用 `custom_size` |
| `size` | `2K` | Ark Images API 的 `size` 字段快捷值；5.0 lite 支持 `2K` / `3K` / `4K`，4.5 支持 `2K` / `4K`，4.0 支持 `1K` / `2K` / `4K` |
| `custom_size` | `2048x2048` | 自定义宽高像素值，仅 `size_mode=custom` 生效，格式 `WxH`，如 `2304x1728` |
| `watermark` | `false` | 是否添加水印 |
| `optimize_prompt_mode` | `standard` | 提示词优化模式：`standard` / `fast` |
| `sequential_image_generation` | `disabled` | 组图生成模式：`disabled` / `auto` |
| `sequential_max_images` | `4` | 组图最大数量，范围 `1-15` |

豆包组图官方文档：<https://www.volcengine.com/docs/82379/1824121?lang=zh#fc9f85e4>

## openai_images_settings（OpenAI Images API 专用配置）

配置路径：`provider_settings.provider_overrides` 中选择 `openai_images` 模板。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `api_keys` | `[]` | API Key 列表，支持多 Key 轮换 |
| `daily_limit_per_key` | `0` | 每个 Key 每日调用上限，`0` 表示不限制 |
| `model` | `gpt-image-1` | 模型名称，例如 `dall-e-2` / `dall-e-3` / `gpt-image-1` / `gpt-image-2` |
| `api_base` | - | API 端点地址，留空使用 OpenAI 官方 |
| `quality` | - | 图像质量。GPT image：`auto` / `high` / `medium` / `low`；dall-e-3：`hd` / `standard` |
| `response_format` | `b64_json` | 响应格式：`b64_json` / `url` |
| `size_mode` | `preset` | 尺寸模式：`preset` 使用供应商分辨率映射；`custom` 使用 `custom_size` |
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
| LLM 工具调用 | 固定接收 `resolution` / `aspect_ratio`，未传入时使用配置中的 `custom_size` |

WebUI 中切换为 `size_mode=custom` 后，`resolution` 和 `aspect_ratio` 会自动隐藏，仅保留 `custom_size`；切回 `preset` 后再显示预设分辨率和长宽比。

`size_mode=custom` 时，LLM 工具不会动态切换参数 schema；传入非法 `resolution` / `aspect_ratio` 时会记录警告并回退为默认配置。

## xai_settings（xAI Images API 专用配置）

配置路径：`provider_settings.provider_overrides` 中选择 `xai` 模板。

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

## agnes_ai_settings（Agnes AI 图片生成 API 专用配置）

配置路径：`provider_settings.provider_overrides` 中选择 `agnes_ai` 模板。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `api_keys` | `[]` | Agnes AI API Key 列表，支持多 Key 轮换 |
| `daily_limit_per_key` | `0` | 每个 Key 每日调用上限，`0` 表示不限制 |
| `model` | `agnes-image-2.1-flash` | Agnes AI 图片模型，可填写 `agnes-image-2.0-flash` / `agnes-image-2.1-flash` |
| `api_base` | `https://apihub.agnes-ai.com` | API 端点地址，插件会统一调用 `/v1/images/generations` |
| `response_format` | `url` | 响应格式：`url` / `b64_json` |
| `reference_image_mode` | `base64` | 参考图传递方式：`base64` / `auto` / `url` |
| `proxy` | - | 独立代理地址 |

`agnes_ai` 供应商统一使用 JSON 请求：

- 文生图：`POST /v1/images/generations`
- 图生图：同一端点，通过 `extra_body.image` 传入参考图 URL 或 data URI

当 `response_format=b64_json` 且没有参考图时，插件会按 Agnes AI 文生图格式发送 `return_base64=true`；带参考图时会在 `extra_body.response_format` 中请求 `b64_json`。默认 `reference_image_mode=base64` 会把本地图片和 URL 参考图统一转成 data URI；如确认参考图是公开可访问 URL，可改为 `auto` 或 `url`。

## minimax_settings（MiniMax 图片生成 API 专用配置）

配置路径：`provider_settings.provider_overrides` 中选择 `minimax` 模板。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `api_keys` | `[]` | MiniMax API Key 列表，支持多 Key 轮换 |
| `daily_limit_per_key` | `0` | 每个 Key 每日调用上限，`0` 表示不限制 |
| `model` | `image-01` | MiniMax 图片模型，官方支持 `image-01` / `image-01-live` |
| `api_base` | `https://api.minimaxi.com` | API 端点地址，插件会统一调用 `/v1/image_generation` |
| `response_format` | `base64` | 响应格式：`base64` / `url`。官方 URL 有效期为 24 小时 |
| `n` | `1` | 单次请求生成图片数量，官方范围 `1-9` |
| `prompt_optimizer` | `false` | 是否开启 MiniMax 提示词自动优化 |
| `aigc_watermark` | `false` | 是否添加 AIGC 水印 |
| `reference_image_mode` | `auto` | 参考图传递方式：`auto` / `base64` / `url` |
| `subject_reference_type` | `character` | `subject_reference.type`，默认用于人物主体一致性 |
| `width` / `height` | `0` | 未传 `aspect_ratio` 时可同时设置，范围 `512-2048` 且为 `8` 的倍数；`0` 表示不传 |
| `seed` | `0` | 固定随机种子，`0` 表示不传 |
| `proxy` | - | 独立代理地址 |

`minimax` 供应商使用 MiniMax 官方单一图像端点：

- 文生图：`POST /v1/image_generation`
- 图生图：同一端点，通过 `subject_reference[].image_file` 传入参考图

供应商条目的 `resolution` 和 `aspect_ratio` 的适配规则：

| 场景 | resolution | aspect_ratio | 实际行为 |
|------|-----------|-------------|---------|
| 1K + 支持比例 | 1K | `1:1`/`16:9`/... | 透传 `aspect_ratio`（MiniMax 原生） |
| 2K/4K + 支持比例 | 2K/4K | `1:1`/`16:9`/... | 计算显式 `width`/`height`（4K 降级为 2048） |
| 不支持比例 | 任意 | `4:5`/`5:4` | 计算显式 `width`/`height` |
| 无比例 + 无 w/h 设置 | 2K/4K | — | 使用 `resolution` 对应的正方形尺寸 |
| `image-01-live` 模型 | 任意 | 任意 | 仅使用 `aspect_ratio`，不发送 `width`/`height` |

支持的长宽比枚举：`1:1` / `16:9` / `4:3` / `3:2` / `2:3` / `3:4` / `9:16` / `21:9`。`image-01-live` 不支持 `21:9`，插件会自动忽略。

官方文档：

- <https://platform.minimaxi.com/docs/guides/image-generation>
- <https://platform.minimaxi.com/docs/api-reference/image-generation-t2i>
- <https://platform.minimaxi.com/docs/api-reference/image-generation-i2i>

## stepfun_settings（StepFun 图片生成 API 专用配置）

配置路径：`provider_settings.provider_overrides` 中选择 `stepfun` 模板。仅完全适配 `step-image-edit-2` 模型参数，其他模型名可填写但参数会按 step-image-edit-2 的格式透传。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `api_keys` | `[]` | StepFun API Key 列表，支持多 Key 轮换 |
| `daily_limit_per_key` | `0` | 每个 Key 每日调用上限，`0` 表示不限制 |
| `model` | `step-image-edit-2` | 图片模型名称，可改为其它 StepFun 图片模型 |
| `api_base` | `https://api.stepfun.com` | API 端点；同时兼容 `https://api.stepfun.com/step_plan/v1` 写法，自动识别 `/v1` 后缀 |
| `response_format` | `url` | `url` 返回临时签名链接（`res.stepfun.com`），`b64_json` 返回 base64 并由插件落盘 |
| `steps` | `0` | 采样步数，`0` 表示不传（服务端默认 `8`） |
| `cfg_scale` | `0` | 提示词引导强度，`0` 表示不传（服务端默认 `1.0`） |
| `negative_prompt` | `""` | 负向提示词，留空不传 |
| `text_mode` | `false` | 是否启用 step-image-edit-2 的 `text_mode`，适用于含文字生成场景 |
| `seed` | `0` | 固定随机种子，`0` 表示不传 |
| `proxy` | - | 独立代理地址，优先级高于全局代理和环境变量 |

`stepfun` 供应商使用阶跃星辰官方图片端点：

- 文生图：`POST /v1/images/generations`（JSON 请求体）
- 图生图：`POST /v1/images/edits`（`multipart/form-data`）

尺寸适配规则（按模型自动选择官方支持的预设集合）：

**`step-image-edit-2` 文生图（generations）** 支持五档尺寸：

| 通用尺寸/比例 | 实际下发 |
|--------------|---------|
| 正方形 | `1024x1024` |
| 竖图 9:16 / 3:4 | `768x1360` |
| 竖图 接近 4:5 | `896x1184` |
| 横图 16:9 / 4:3 | `1360x768` |
| 横图 接近 5:4 | `1184x896` |

**`step-1x-medium` 文生图（generations）** 支持六档：`256x256` / `512x512` / `768x768` / `1024x1024` / `1280x800` / `800x1280`，按目标长宽比自动选最近预设。

**图生图（edits）**：

- `step-image-edit-2`：官方仅支持单图输入，传入多图会自动取首张并打 debug 日志；`size` 参数官方明确"该参数不生效"，因此插件不再下发，输出尺寸始终与输入图一致。
- `step-1x-edit`：`size` 仅在 `512x512` / `768x768` / `1024x1024` 三档内透传。

其他注意事项：

- 请求被安全审核拦截时，StepFun 会返回 `HTTP 451`，插件统一识别为安全类错误（`category="safety"`，不重试）。
- `b64_json` 模式下生成的图片会保存为本地文件，避免 URL 过期问题。

官方文档：

- <https://platform.stepfun.com/docs/llm/image-edit>
- <https://platform.stepfun.com/docs/api-reference/image-edit>

## sensenova_settings（SenseNova（商汤日日新）专用配置）

配置路径：`provider_settings.provider_overrides` 中选择 `sensenova` 模板。仅支持文生图，尺寸限定为 11 种官方预设。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `api_keys` | `[]` | SenseNova API Key 列表，控制台获取的 Bearer Token，支持多 Key 轮换 |
| `daily_limit_per_key` | `0` | 每个 Key 每日调用上限，`0` 表示不限制 |
| `model` | `sensenova-u1-fast` | SenseNova 图像生成模型，目前仅支持 `sensenova-u1-fast` |
| `api_base` | `https://token.sensenova.cn` | API 端点地址 |
| `default_size` | `2752x1536` | 未推导出合法比例时的兜底尺寸，必须为下表 11 种官方尺寸之一 |
| `proxy` | - | 独立代理地址，优先级高于全局代理和环境变量 |

`sensenova` 供应商使用 SenseNova 官方图像端点：

- 文生图：`POST /v1/images/generations`
- 不支持图生图（由 provider 在 `build_request()` 阶段报错）

官方支持的 11 种固定尺寸（供应商条目的 `aspect_ratio` 会被映射到最接近的预设）：

| 尺寸 | 近似比例 |
|------|---------|
| `2048x2048` | 1:1 |
| `2752x1536` | 16:9 |
| `1536x2752` | 9:16 |
| `2368x1760` | 4:3 |
| `1760x2368` | 3:4 |
| `2496x1664` | 3:2 |
| `1664x2496` | 2:3 |
| `1824x2272` | 4:5 |
| `2272x1824` | 5:4 |
| `3072x1376` | 21:9 |
| `1344x3136` | 超竖长 |

官方文档：

- <https://platform.sensenova.cn/doc?path=/chat/ImageGeneration/ImageGeneration.md>
