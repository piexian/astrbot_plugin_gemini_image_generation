# Changelog
<details>
<summary>⚠️ 配置迁移说明（v1.9.0）</summary>

**v1.9.0 以后的配置文件格式不兼容旧版本**。升级时插件会自动迁移配置，但如果遇到配置模板显示错误（如字段类型不匹配、选项无法选择等），请按以下步骤处理：

1. **查找备份文件**：旧配置已自动备份到插件数据目录
   - 路径：`AstrBot/data/plugins/astrbot_plugin_gemini_image_generation/config_backup_pre_v1.9.x_<时间戳>.json`
   - 示例：`config_backup_pre_v1.9.0_20260130_143052.json`

2. **删除当前配置**：在根目录中删除本插件的配置文件，`AstrBot\data\config\astrbot_plugin_gemini_image_generation_config.json`

3. **重新配置**：然后webui重载插件，再对照备份文件手动重新配置各项参数

**主要变更**：
- `limit_settings` 中的 `rate_limit_enabled`、`rate_limit_period_seconds`、`rate_limit_max_requests` 已迁移到 `rate_limit_rules`（template_list 格式）
- `quick_mode_settings` 从 object 格式迁移到 template_list 格式

</details>

## [1.9.13] - 2026-04-23

### Added

- `openai_images` 新增 `size_mode` / `custom_size` 配置，支持在 provider override 条目内显式切换预设尺寸模式与自定义尺寸模式
- 新增 `tl/openai_image_size.py`，统一处理 OpenAI 官方自定义尺寸约束校验与归一化

### Changed

- `openai_images` 默认模型调整为 `gpt-image-1`
- LLM 生图工具会根据当前 provider 配置动态切换参数定义
  - `openai_images + size_mode=custom` 时仅暴露 `size`
  - 其他模式继续使用 `resolution` / `aspect_ratio`
- README 补充 OpenAI 官方文档链接，并更新项目结构说明以反映当前仓库模块布局

### Fixed

- 配置保存后插件重载场景下，`openai_images` 自定义尺寸模式现在会随配置动态刷新工具注册信息
- 修复 LLM 工具在非法 `resolution` / `aspect_ratio` / `size` 输入下静默回退默认值的问题，改为直接报错并要求模型修正参数后重试
- 当 `openai_images + size_mode=custom` 且本次未显式传入 `size` 时：
  - 使用配置文件中的 `custom_size`
  - 输出警告级别日志
  - 在工具返回结果中提醒 LLM 本次使用了配置值

## [1.9.12] - 2026-04-21

### Fixed

- 修复 Gemini 图像工具调用路径错误透传 `thought_signature` 的问题
  - 不再将 `thought_signature` 拼入 `CallToolResult` 或 Tool 返回文本，避免 AstrBot 将超大签名重新注入 LLM 上下文
  - 统一将 `thought_signature` 视为仅限协议层/调试使用的 opaque 元数据，默认直接丢弃，不再参与用户可见结果
  - 调试日志改为仅输出受限预览和长度，避免超长签名污染日志或被后续误用
- 修复部分 Gemini / NewAPI 网关下 LLM 工具生图成功后，复核阶段因上下文膨胀触发 `413` / `输入Tokens数量超过系统限制` 的问题

## [1.9.11] - 2026-04-16

### Added

- 新增 `xai` API 类型，原生支持 xAI 官方 `/v1/images/generations` 和 `/v1/images/edits` JSON 图像接口
- 新增 `tl/api/xai.py` 供应商实现，支持将参考图统一转为 `data URI` 内联发送，兼容单图编辑和最多 5 张参考图的多图编辑
- 新增 `provider_overrides[xai]` 配置模板，支持多 Key、每日限额、`response_format`、`n`、代理等参数

### Changed

- `main.py` 增加 `xai_settings` 绑定逻辑，确保 provider override 能注入到 API client
- `tl/tl_api.py` 现在会让 `xai` 走供应商自定义响应解析，正确处理 `data[].url` 和 `data[].b64_json`
- `README.md` 更新多 API 支持列表和 `xai` 配置说明

## [1.9.10] - 2026-04-16

### Fixed

- 修复同一张图片因签名 URL 与裸 URL 同时被收集而重复发送的问题
  - 新增 `_normalize_image_ref` 方法：对本地路径取 realpath、对远程 URL 去除鉴权类 query 参数（key/token/signature 等），使签名版与裸版 URL 映射到同一规范化键
  - `merge_available_images` 改用规范化键去重，替代原始字符串比较
  - `openai_compat` / `tl_api` 的文本回退扫描增加守卫条件：仅在未提取到结构化图片时才回退扫描文本 URL，防止同一张图被双重收集

## [1.9.9] - 2026-04-11

### Added

- 新增 `_build_call_tool_result` 方法，将生成结果封装为 AstrBot 官方 `CallToolResult`（含 `ImageContent` / `TextContent`）
- 新增 SHA-256 内容哈希去重，避免同一图片以不同路径/URL 重复返回到模型
- 代理模式下前台路径自动通过代理下载远程 URL 图片并内联为 base64，确保 AstrBot Core 可正常访问
- 代理模式下后台路径在发送前也通过代理下载远程 URL 为本地文件，避免 NapCat 等平台无法直接访问需代理的图片（代理全链路覆盖）
- 当图片仅有远程 URL 且无法转为 base64 时，使用 `TextContent` 返回 URL 并引导模型调用 `send_message_to_user` 发送

### Changed

- **LLM 工具前台返回模式迁移至 `CallToolResult + ImageContent`**：工具生图完成后不再通过 `event.send()` 直接发送图片，而是返回结构化 `CallToolResult`，由 AstrBot 框架统一缓存图片并回传模型；后台超时兜底仍使用 `event.send()` 直接发送
- 默认 `tool_call_timeout` 对齐 AstrBot Core，从 60 秒调整为 120 秒
- 后台生成任务使用插件配置的 `total_timeout` 作为超时上限，不再共享 `tool_call_timeout`，避免超时预算不足

### Fixed

- 修复 API 响应解析中多次图像提取（结构化字段 + 文本正则）产生重复图片的问题，现在仅在结构化字段未提取到图片时才回退到文本提取
- 修复代理模式下远程图片下载可能阻塞整个工具调用超时的问题，新增 30 秒独立超时保护，超时后自动降级到后台发送

## [1.9.8] - 2026-04-11

### Added

- 新增 `openai_images` API 类型，支持 OpenAI `/v1/images/generations`（文生图）和 `/v1/images/edits`（图像编辑）专用端点
- 支持 GPT image 系列模型（gpt-image-1 / gpt-image-1-mini / gpt-image-1.5）和 DALL·E 系列的完整参数配置
- 新增 `output_compression`（输出压缩率，滑动条 0-100）和 `moderation`（审核模式）配置项
- 新增 `generations_only` 配置项，可强制只使用文生图端点
- 支持 multipart/form-data 请求，用于图像编辑端点的二进制图片上传
- GPT image 模型支持多张参考图同时上传
- 解析并记录 API 返回的 Token 用量信息（input/output/total）



## [1.9.7] - 2026-03-30

### Added

- 新增代理（Proxy）支持，可在 `api_settings` 中配置全局代理，也可在各 provider override 中独立配置
- 支持 HTTP、HTTPS、SOCKS5 代理格式（SOCKS5 需将 `aiohttp-socks` 列入依赖，插件会自动安装）
- 代理优先级：provider override > 全局代理 > 环境变量（`HTTPS_PROXY` / `HTTP_PROXY`）
- 代理模式下强制下载图片到本地后再发送，避免外部 URL 因代理无法直接访问

## [1.9.6] - 2026-03-07

### Added

- 新增 `llm_tool_timeout_reserve_percent` 配置项，用于按 `tool_call_timeout` 百分比预留安全时间
- 可配置为 1-100 的整数，默认值为 `50`

### Changed

- LLM 工具触发生图改为“同步优先，超时转后台”的混合模式
- 前台等待时间根据当前 `tool_call_timeout` 与 `llm_tool_timeout_reserve_percent` 动态计算
- 默认 `tool_call_timeout=60` 秒时，预留 `50%` 安全时间，前台最多同步等待约 `30` 秒
- 同步未超安全时间时，工具会直接发送图文结果，减少“图片先到、说明后到”的割裂感
- 超出前台等待窗口后，不取消已提交的生成任务，而是继续在后台执行并在完成后自动发送


## [1.9.5] - 2026-03-04

### Fixed

- 修复 `_generate_image_core_internal` 兼容层方法缺少 `is_tool_call` 参数导致 LLM 工具调用路径 TypeError 的问题
  - 错误信息：`GeminiImageGenerationPlugin._generate_image_core_internal() got an unexpected keyword argument 'is_tool_call'`
  - 影响范围：所有通过 LLM 工具调用触发的图像生成请求

## [1.9.4] - 2026-03-03

### Fixed

- 修复超时/取消错误（`error_type` 为 timeout/cancelled）落入通用 else 分支导致提示"参数异常"与实际超时原因矛盾的问题
- 修复 `_background_generate_and_send` 中已格式化的错误消息被 `format_error_message` 二次包装导致双层嵌套的问题
- 移除 `__init__` 中过早的 `_load_provider_from_context(quiet=True)` 调用，避免提供商未注册时触发内部警告

### Changed

- `generate_image_core` 新增 `is_tool_call` 参数，区分指令调用与 LLM 工具调用的超时策略
  - 工具调用（`is_tool_call=True`）：使用框架 `tool_call_timeout` 作为总超时
  - 指令调用（默认）：使用插件配置的 `total_timeout` 作为总超时
- 超时错误提示根据调用来源给出精准建议（工具调用提示调整 `tool_call_timeout`，指令调用提示调整 `total_timeout`）
- 新增 `network` 错误类型的独立提示分支
- `tl_api` 中超时消息简化为纯事实描述，具体配置建议由上层按场景生成
- `on_astrbot_loaded` 去除重复的提供商加载调用，简化为单次调用
- 移除所有 logger 调用中的 emoji 前缀（7 个文件共 30 处），保持日志输出简洁规范

## [1.9.3] - 2026-02-21

### Changed

- 移除插件内置的 AstrBot 版本检测代码，改用 `metadata.yaml` 中的 `astrbot_version` 字段声明版本要求（需 AstrBot PR #5235 支持）
- 新增 `support_platforms: [napcat]` 元数据声明支持的平台

## [1.9.2] - 2026-02-05

### Added

- 新增 `for_forum` 论坛发帖模式：LLM 工具调用时设置 `for_forum=true`，工具将同步等待图片生成完成并返回图片路径/URL，便于后续上传到论坛图床

### Fixed

- 修复插件重载时 API 客户端未正确加载的问题（在初始化时调用 `_load_provider_from_context`）

## [1.9.1] - 2026-01-31

### Fixed

- 在 `GeminiImageGenerationTool` 中显式定义 `handler_module_path`，确保 Function Tool 在新版 AstrBot 框架下能被正确解析和加载。

## [1.9.0] - 2026-01-30

### Added

#### 豆包（Volcengine Ark）API 支持

- 新增 `doubao` API 类型，支持字节跳动火山引擎 Ark 平台的豆包生图 API
- 支持 `doubao-seedream-4.5` 和 `doubao-seedream-4.0` 两个全功能模型
- 文生图（t2i）和图生图（i2i）完整支持
- 尺寸映射：支持 1K/2K/4K 和 WxH 格式
- 提示词优化模式：`standard`（标准，质量更高）/ `fast`（快速）
- 组图生成模式：`sequential_image_generation` 参数支持生成一组内容关联的图片
- 水印控制：可选在图片右下角添加"AI生成"水印
- 智能降级：首次使用 URL 格式返回，重试时自动降级为 base64 格式

#### 新增配置项

- `doubao_settings.api_key` - 火山引擎 API Key
- `doubao_settings.endpoint_id` - 模型名称（默认 doubao-seedream-4.5）
- `doubao_settings.api_base` - API 端点地址
- `doubao_settings.default_size` - 默认尺寸（2K/4K 或具体尺寸）
- `doubao_settings.watermark` - 是否添加水印
- `doubao_settings.optimize_prompt_mode` - 提示词优化模式
- `doubao_settings.sequential_image_generation` - 组图生成模式
- `doubao_settings.sequential_max_images` - 组图最大数量

### Technical Details

#### New Files

- `tl/api/doubao.py` - 豆包 API 适配器实现

#### Modified Files

- `tl/api/registry.py` - 注册 DoubaoProvider
- `_conf_schema.json` - 新增 doubao 到 api_type 选项，新增 doubao_settings 配置节

## [1.8.5] - 2026-01-21

### Added

#### LLM 工具参数增强

- `GeminiImageGenerationTool` 新增 `resolution` (1K/2K/4K) 和 `aspect_ratio` 参数支持
- AI 现在可以根据用户指令精确控制生成图片的分辨率和长宽比
- 支持的比例包括 1:1, 16:9, 4:3, 3:2, 9:16, 4:5, 5:4, 21:9, 3:4, 2:3

### Removed

#### 配置项清理

- 移除 `verbose_logging` 配置项，默认使用标准日志级别

## [1.8.4] - 2026-01-14

### Added

#### LLM 工具触发器模式

- LLM 工具现在采用触发器模式，AI 仅提供提示词和参数选择，图片在后台异步生成
- 生成完成后自动发送结果，避免长生成时间导致的工具超时
- AI 会在工具提示中告诉用户图片正在生成中，需要等待

#### 智能错误消息系统

- 新增 `format_error_message()` 函数，自动识别错误类型并提供针对性建议
- image_config 参数冲突错误：提示管理员修改参数名配置
- API 密钥/模型错误：提示联系管理员检查配置
- 配额/限流错误：提示稍后重试
- 安全过滤错误：提示修改提示词
- 网络连接错误：提示检查网络或配置代理
- **文本回复错误**：模型只返回文字未生成图片时的友好提示
- **空响应错误**：API 返回空响应时的友好提示

#### KV 存储持久化

- 限流器（RateLimiter）现在支持 KV 存储，限流数据在重启后不会丢失
- 使用 AstrBot 内置 KV API（需版本 >= 4.9.2）
- 向后兼容：无 KV API 时自动降级为内存模式

### Changed

#### LLM 工具行为

- 工具调用立即返回确认消息，不再阻塞等待图片生成
- 后台任务独立执行，完成后使用与普通命令相同的方式发送结果
- AI 会用自己的风格告知用户图片正在生成

#### 错误处理

- `main.py` 中的快捷生成错误处理改用 `format_error_message()`
- `tl/llm_tools.py` 中的后台任务和辅助函数错误处理改用 `format_error_message()`
- 所有错误消息现在更加用户友好，针对具体错误类型给出建议

#### 限流器

- 使用 `time.time()` 替代 `time.monotonic()` 以支持跨重启持久化
- `reset()` 方法改为异步，同步清理 KV 存储

### Technical Details

#### Modified Files

- `tl/llm_tools.py` - 重构为触发器模式，后台任务独立执行
- `tl/tl_utils.py` - 新增 `format_error_message()` 智能错误格式化
- `tl/rate_limiter.py` - 新增 KV 存储支持，持久化限流数据
- `main.py` - 使用智能错误消息，传入 KV 回调给限流器
- `metadata.yaml` - 版本号更新至 v1.8.4
- `_conf_schema.json` - 版本号更新
- `README.md` - 版本号更新至 v1.8.4

## [1.8.3] - 2026-01-13

### Added

#### 带空格参数支持

- 支持带空格的提示词（英文提示词）
- 使用 `shlex` 进行智能参数解析
- 引号内的空格正确保留

### Changed

#### 异步调用优化

- 使用 `asyncio.get_running_loop()` 替代 `asyncio.get_event_loop()`
- 改进异步任务的上下文管理

#### 内存优化

- 实现 base64 图像数据的 LRU 缓存
- 避免重复保存相同的图像数据
- 减少不必要的磁盘 I/O 和内存占用

## [1.8.2] - Previous

### Changed

#### 智能内存管理

- 优先使用图片 URL 而非 base64（特别是大图片）
- 新增配置项 `max_inline_image_size_mb` 控制 base64 编码阈值
- 本地图片大于阈值时使用文件系统引用
- 修复发送顺序问题

## Earlier Versions

See [GitHub Releases](https://github.com/piexian/astrbot_plugin_gemini_image_generation/releases) for full changelog history.
