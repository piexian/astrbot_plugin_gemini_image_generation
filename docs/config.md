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
| `enabled` | `true` | 条目级开关；设为 `false` 临时停用本条模型配置但不删除，同渠道其他条目不受影响 |
| `priority` | `0` | 同类型多条配置按优先级从高到低尝试；相同优先级按配置表从上到下 |
| `api_keys` | `[]` | API Key 列表，支持多 Key 轮换 |
| `daily_limit_per_key` | `0` | 每个 Key 每日调用上限，`0` 表示不限制 |
| `model_alias` | - | 可选模型别名，供命令和 LLM 工具选择；可跨供应商复用，实际请求仍使用原始模型名 |
| `model` | - | 模型名称；豆包使用 `endpoint_id` |
| `api_base` | - | API 端点地址 |
| `proxy` | - | 独立代理地址，优先级高于全局代理和环境变量 |
| `resolution` | `1K` | 该供应商默认分辨率；快速模式覆盖值优先 |
| `aspect_ratio` | `1:1` | 该供应商默认长宽比；快速模式覆盖值优先 |
| `max_reference_images` | `6` | 该供应商最多使用的参考图数量 |

加载规则：

- `enabled` 为 `false` 的条目直接跳过，不校验模型/密钥、不参与轮询；重新设为 `true` 即可恢复。
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
google / openai / agnes_ai / xai / minimax / stepfun / openai_images / doubao / sensenova / dashscope
```

下方 `doubao_settings`、`openai_images_settings`、`agnes_ai_settings`、`xai_settings`、`minimax_settings`、`stepfun_settings`、`sensenova_settings`、`dashscope_settings` 章节对应这些模板的专用字段。代码中的同名 `*_settings` 字段仅作为兼容旧调用的首个候选投影；多候选场景以 `provider_settings.provider_overrides` 和运行时派生的 `provider_settings_by_type` 为准。

## image_generation_settings

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `enable_sticker_split` | `true` | 表情包自动切割 |
| `enable_sticker_zip` | `false` | 切分后打包 ZIP 发送 |
| `sticker_grid` | `4x4` | 表情包提示词网格描述 |
| `preserve_reference_image_size` | `false` | 改图时保留参考图尺寸 |
| `max_inline_image_size_mb` | `2.0` | 本地图片 base64 编码阈值 |
| `llm_tool_timeout_reserve_percent` | `50` | 为 `tool_call_timeout` 预留的百分比，剩余时间用于前台同步等待 |
| `llm_tool_reference_path_mode` | `whitelist` | LLM 工具 `reference_image_paths` 的本地路径权限模式 |
| `llm_tool_reference_allowed_dirs` | `[]` | `whitelist` 模式下额外允许的参考图目录 |
| `batch_max_images_per_task` | `10` | 单个命名提示词的目标图片数量上限；供应商原生单次上限更小时自动拆分补齐 |
| `batch_max_tasks` | `20` | 一次 `batch_tasks` 允许的命名任务条目上限 |
| `batch_concurrency` | `3` | 后台批量生成并发数；每个条目内部按供应商单次上限顺序补齐 |
| `background_task_retention_hours` | `24` | 后台任务状态记录保留时间；插件重启会把未完成任务标记为 `interrupted` |

分辨率、长宽比、最大参考图数量、Google 文本响应、Google 搜索接地、OpenAI/OpenAI 兼容参数名等均在 `provider_settings.provider_overrides` 的各供应商条目内配置。

### LLM 工具本地路径参考图

`gemini_image_generation` 工具支持 `reference_image_paths` 参数，用于让 LLM 复用上一次工具调用缓存在本地的图片，例如 `data/temp/tool_images/` 下的文件。

路径守卫规则：

- `llm_tool_reference_path_mode=whitelist`：默认模式，只允许默认目录和 `llm_tool_reference_allowed_dirs` 中的文件。
- `llm_tool_reference_path_mode=global`：跳过白名单目录检查，但仍会检查路径穿越、文件存在性和图片完整性。插件会在支持函数工具权限配置的 AstrBot 新版上，尝试将 `gemini_image_generation` 默认权限设为管理员；若管理员已在 WebUI 手动配置过该工具权限，则不会覆盖。
- 默认白名单包含常见 AstrBot 数据目录：`~/.astrbot/data`、`/opt/astrbot/data`、`/AstrBot/data`、`/app/data`，以及 `ASTRBOT_DATA_PATH` 环境变量指向的目录。
- `llm_tool_reference_allowed_dirs` 可追加自定义目录，支持绝对路径或 `~` 开头路径。
- 原始路径中包含 `..` 穿越会被拒绝；符号链接会先 `resolve`，再检查最终路径是否仍位于允许目录内。
- 只有能通过完整性校验的图片文件会被接受；不存在、非文件、非图片或损坏图片都会被拒绝并写入日志。

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
| `image_cache_max_size_mb` | `512.0` | 插件数据目录 `images/` 下生成图与帮助图的容量上限（MB），超限时按最旧文件优先自动清理；`0` 表示不清理 |
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

## doubao_settings（豆包生图专用配置）

配置路径：`provider_settings.provider_overrides` 中选择 `doubao` 模板。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `api_keys` | `[]` | 火山引擎 API Key 列表，支持多 Key 轮换 |
| `daily_limit_per_key` | `0` | 每个 Key 每日调用上限，`0` 表示不限制 |
| `endpoint_id` | `doubao-seedream-5-0-260128` | Ark `model` 字段，可填写模型 ID、版本化模型 ID 或推理点 ID，例如 `doubao-seedream-5-0-pro-260628`、`ep-xxxx` |
| `model_capability` | `auto` | `auto` 仅按 `endpoint_id` 中明确的 Pro 标记识别；不带 Pro 标记的 `seedream-5.0` 不会自动按 Pro 处理。若使用对应 Seedream 5.0 Pro 的 `ep-...` 推理点 ID 或其他无法从名称判断能力的 ID，请设为 `seedream_5_pro`，以启用单图和最多 10 张参考图限制 |
| `endpoint_mode` | `official` | 接入端点：`official` 火山方舟官方图片端点；`agent_plan` Agent Plan 专属图片端点 |
| `api_base` | `https://ark.cn-beijing.volces.com` | API 基础地址；插件按 `endpoint_mode` 自动追加 `/api/v3` 或 `/api/plan/v3`，通常无需修改 |
| `size_mode` | `preset` | 尺寸模式：`preset` 使用 `size`；`custom` 使用 `custom_size` |
| `size` | `2K` | Ark Images API 的 `size` 字段快捷值；5.0 Pro/Lite 支持 `2K` / `3K` / `4K`，4.5 支持 `2K` / `4K`，4.0 支持 `1K` / `2K` / `4K` |
| `custom_size` | `2048x2048` | 自定义宽高像素值，仅 `size_mode=custom` 生效，格式 `WxH`，如 `2304x1728` |
| `watermark` | `false` | 是否添加水印 |
| `output_format` | `jpeg` | 输出图片格式：`jpeg` 或 `png` |
| `optimize_prompt_mode` | `standard` | 提示词优化模式：`standard` / `fast` |
| `sequential_image_generation` | `disabled` | 组图生成模式：`disabled` / `auto`；Seedream 5.0 Pro 不支持组图，设置为 `auto` 时会被 provider 忽略 |
| `sequential_max_images` | `4` | 组图最大数量，范围 `1-15` |

### Seedream 5.0 Pro 适配说明

当 `endpoint_id` 使用 `doubao-seedream-5-0-pro-260628` 等 Seedream 5.0 Pro 模型 ID 时，provider 会按官方能力自动处理；如果使用对应的 `ep-...` 推理点 ID，请同时将 `model_capability` 设为 `seedream_5_pro`：

- 只发送单图请求；即使配置 `sequential_image_generation=auto`，也不会发送 `sequential_image_generation` 字段。
- 参考图最多发送 10 张；其他支持多图的豆包模型仍按原有上限最多发送 14 张。
- `output_format` 支持 `jpeg`（默认）和 `png`，base64 重试响应会按该格式保存。
- 交互编辑通过提示词中的坐标、框选或箭头等描述完成，插件不会额外注入不受支持的组图、流式字段。

豆包图片接口的请求路径由 `endpoint_mode` 选择：

- `official`：`https://ark.cn-beijing.volces.com/api/v3/images/generations`
- `agent_plan`：`https://ark.cn-beijing.volces.com/api/plan/v3/images/generations`

### Agent Plan 接入

将同一个 `doubao` 模板的 `endpoint_mode` 设置为 `agent_plan` 即可切换，无需改动 `api_base`。Agent Plan 必须使用 Agent Plan 专属 API Key；`endpoint_id` 可填写当前套餐支持的模型（例如 `doubao-seedream-5-0-pro-260628`）或对应的推理点 ID。若填写 `ep-...` 且该推理点对应 Seedream 5.0 Pro，请将 `model_capability` 设为 `seedream_5_pro`。模型和套餐以[支持模型及 Harness](https://docs.volcengine.com/docs/82379/2366394)为准。

当前插件的 `doubao` provider 只封装图片生成接口；Agent Plan 的视频任务接口尚未纳入本插件的图片生成流程。

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

配置路径：`provider_settings.provider_overrides` 中选择 `stepfun` 模板。适配 `step-image-edit-2`（文生图 + 编辑）与 `step-2x-large`（纯文生图）。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `api_keys` | `[]` | StepFun API Key 列表，支持多 Key 轮换 |
| `daily_limit_per_key` | `0` | 每个 Key 每日调用上限，`0` 表示不限制 |
| `model` | `step-image-edit-2` | 图片模型名称；`step-2x-large` 为纯文生图模型，不支持编辑/负向提示词/text_mode |
| `api_base` | `https://api.stepfun.com` | API 端点；同时兼容 `https://api.stepfun.com/step_plan/v1` 写法，自动识别 `/v1` 后缀 |
| `response_format` | `url` | `url` 返回临时签名链接（`res.stepfun.com`），`b64_json` 返回 base64 并由插件落盘 |
| `steps` | `0` | 采样步数，`0` 表示不传（服务端默认 edit-2 `8` / 2x-large `50`）；非零值钳位到 [1, 50] |
| `cfg_scale` | `0` | 提示词引导强度，`0` 表示不传（服务端默认 edit-2 `1.0` / 2x-large `6`）；非零值钳位到 [1.0, 10.0] |
| `negative_prompt` | `""` | 负向提示词，留空不传；仅 `step-image-edit-2` 支持，其他模型自动忽略并记录日志 |
| `text_mode` | `false` | 是否启用 `text_mode`；仅 `step-image-edit-2` 支持，其他模型自动忽略并记录日志 |
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

**`step-2x-large` 文生图（generations）** 支持六档：`256x256` / `512x512` / `768x768` / `1024x1024` / `1280x800`（16:9）/ `800x1280`（9:16），按目标长宽比自动选最近预设。已下线的 `step-1x-medium` 沿用同尺寸表。

**模型级编辑门控**：仅 `step-image-edit` 系列候选参与改图/参考图请求，`step-2x-large` 等纯文生图模型自动跳过。

**图生图（edits）**：

- `step-image-edit-2`：官方仅支持单图输入，传入多图会自动取首张并打 debug 日志；`size` 参数官方明确"该参数不生效"，因此插件不再下发，输出尺寸始终与输入图一致。
- `step-1x-edit`：`size` 仅在 `512x512` / `768x768` / `1024x1024` 三档内透传。

- 两个模型的 prompt 均为 512 字符硬上限，超限请求直接报错（不发起注定失败的服务端调用）。

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

## dashscope_settings（DashScope 阿里云百炼专用配置）

配置路径：`provider_settings.provider_overrides` 中选择 `dashscope` 模板。接入 DashScope **原生** multimodal-generation 同步接口（非 OpenAI 兼容格式），支持通义万相 `wan2.7-image-pro` / `wan2.7-image`（文生图 + 多图编辑）与千问图像 `qwen-image-2.0` 系列。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `api_keys` | `[]` | DashScope API Key 列表（控制台获取的 Bearer Token），支持多 Key 轮换 |
| `daily_limit_per_key` | `0` | 每个 Key 每日调用上限，`0` 表示不限制 |
| `model` | `wan2.7-image-pro` | 推荐 `wan2.7-image-pro` / `wan2.7-image`；千问图像可选 `qwen-image-2.0-pro` / `qwen-image-2.0`（支持负向提示词）。仅支持同步调用模型，wan2.5 及更早的纯异步模型不适用 |
| `endpoint_mode` | `dashscope` | 接入端点：`dashscope` 阿里云百炼官方端点；`token_plan` 千问 AI 平台 Token Plan 套餐端点 |
| `api_base` | - | 可选覆盖；留空按 `endpoint_mode` 自动选择地址，仅自建反代等特殊场景需要填写 |
| `max_reference_images` | `9` | 最大参考图数量（wan2.7 上限 9 张），超过按顺序截取 |
| `size_mode` | `preset` | `preset` 按分辨率+长宽比换算官方推荐像素；`custom` 直接发送 `custom_size` |
| `resolution` | `2K` | 分辨率档位（`1K`/`2K`/`4K`）。4K 仅 `wan2.7-image-pro` 文生图支持；qwen-image-2.0 上限 2K |
| `aspect_ratio` | `1:1` | 默认长宽比，快速模式传入覆盖值时优先 |
| `custom_size` | `2048*2048` | 仅 `size_mode=custom` 生效。格式 `WxH`（x/×/* 均可，发送时统一为 `W*H`），或 wan2.7 简写 `1K`/`2K`/`4K` |
| `n` | `1` | 单次生成图片数量，按成功张数计费，超出范围自动钳制 |
| `watermark` | `false` | 是否添加水印 |
| `negative_prompt` | - | 负向提示词，wan2.7 不支持（自动跳过并记录日志） |
| `prompt_extend` | `false` | 提示词智能改写。服务端默认开启（增加 3-4 秒延迟）；插件提示词已增强，默认关闭并显式发送。wan2.7 不支持 |
| `thinking_mode` | `true` | 增强推理，仅 wan2.7 生效；顺序模式下不可用 |
| `enable_sequential` | `false` | 顺序组图生成，仅 wan2.7 生效；开启后 `n` 上限提高到 12，`thinking_mode` 随之失效 |
| `proxy` | - | 独立代理地址，优先级高于全局代理和环境变量 |

`dashscope` 供应商使用 DashScope 原生同步端点：

- 文生图 / 图像编辑：`POST /api/v1/services/aigc/multimodal-generation/generation`
- 生成图片仅返回 URL 且 **24 小时后过期**，provider 会立即下载落盘；下载失败时兜底返回直链
- 不支持纯异步 legacy 模型（wan2.5 及更早、wanx 系列）；`color_palette`（wan2.7 品牌色）暂不支持配置

### Token Plan（千问 AI 平台套餐）接入

千问 AI 平台 Token Plan 套餐的图像生成接口与 DashScope 原生同步端点结构完全一致（同请求体、同响应格式），切换方式：

- `endpoint_mode` 选 `token_plan`（自动使用 `https://token-plan.cn-beijing.maas.aliyuncs.com`）
- `api_keys` 填套餐专属 Key（以 `sk-sp-` 为前缀）

个人版 Token Plan 图像生成模型仅 `wan2.7-image` / `wan2.7-image-pro`；参数门控与尺寸换算规则与 DashScope 原生端点相同。完整模型列表以千问 AI 平台为准。

参数门控规则（由 provider 按模型自动处理）：

| 参数 | wan2.7 系列 | qwen-image-2.0 系列 | 其他（qwen-image-plus/max 等） |
|------|------------|--------------------|-------------------------------|
| `negative_prompt` | 不支持（跳过） | 支持 | 支持 |
| `prompt_extend` | 不支持（跳过） | 显式发送 | 显式发送 |
| `thinking_mode` | 支持（非顺序模式） | 不发送 | 不发送 |
| `enable_sequential` | 支持 | 不发送 | 不发送 |
| `n` 范围 | 标准 1-4 / 顺序 1-12 | 1-6 | 仅 1 |

preset 模式尺寸换算表（分辨率档位 × 长宽比 → 官方推荐像素）：

| 长宽比 | 4K | 2K | 1K |
|--------|----|----|----|
| 1:1 | `4096*4096` | `2048*2048` | `1280*1280` |
| 16:9 | `4096*2304` | `2688*1536` | `1696*960` |
| 9:16 | `2304*4096` | `1536*2688` | `960*1696` |
| 4:3 | `4096*3072` | `2368*1728` | `1472*1104` |
| 3:4 | `3072*4096` | `1728*2368` | `1104*1472` |

表外比例（`3:2`、`4:5`、`5:4`、`21:9`、`2:3`）按档位总像素预算推算，两边取 16 的倍数并钳位 `[512, 4096]`。

官方文档：

- 文生图：<https://platform.qianwenai.com/docs/developer-guides/image-generation/text-to-image>
- 图像编辑：<https://platform.qianwenai.com/docs/developer-guides/image-generation/wan-image-editing>
- Token Plan 接入：<https://platform.qianwenai.com/docs/token-plan/best-practices/multimodal-generation>
