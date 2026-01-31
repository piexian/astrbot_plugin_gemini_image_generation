# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

### ⚠️ 配置迁移说明

**v1.9.0 以后的配置文件格式不兼容旧版本**。升级时插件会自动迁移配置，但如果遇到配置模板显示错误（如字段类型不匹配、选项无法选择等），请按以下步骤处理：

1. **查找备份文件**：旧配置已自动备份到插件数据目录
   - 路径：`AstrBot/data/plugins/astrbot_plugin_gemini_image_generation/config_backup_pre_v1.9.x_<时间戳>.json`
   - 示例：`config_backup_pre_v1.9.0_20260130_143052.json`

2. **删除当前配置**：在根目录中删除本插件的配置文件，`AstrBot\data\config\astrbot_plugin_gemini_image_generation_config.json`

3. **重新配置**：然后webui重载插件，再对照备份文件手动重新配置各项参数

**主要变更**：
- `limit_settings` 中的 `rate_limit_enabled`、`rate_limit_period_seconds`、`rate_limit_max_requests` 已迁移到 `rate_limit_rules`（template_list 格式）
- `quick_mode_settings` 从 object 格式迁移到 template_list 格式

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
