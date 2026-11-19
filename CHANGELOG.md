# Changelog

> 更早版本（v1–v3）的更新日志已归档：[v1](docs/changelog/v1.md) · [v2](docs/changelog/v2.md) · [v3](docs/changelog/v3.md)。


## [Unreleased]

### Fixed

- **Google/Vertex 原生出站字段对齐官方 camelCase**：`imageConfig` 默认发送 `imageSize`/`aspectRatio`，搜索接地工具键改为 `googleSearch`；此前默认的 snake_case 字段会被官方端点拒绝。配置项「分辨率参数名／长宽比参数名」留空即用新默认；旧配置中已保存的值不会被自动改写，如遇 `image_config oneof` 报错请清空这两项。
- **Vertex `person_generation` 出站改用官方大写枚举**（`ALLOW_ALL`/`ALLOW_ADULT`/`ALLOW_NONE`），配置界面仍是小写选项，无需改动。

### Changed

- 配置提示明确协议边界：Google 模板端点说明反代（含 Antigravity `/antigravity/v1beta` 入口）填根地址会自动补 `/v1beta`；Gemini Interactions 模板端点标明为 Interactions 协议，与原生 `generateContent` 不通用。OpenAI 兼容渠道继续使用 snake_case 字段（由反代转换层处理）。


## [4.0.0] - 2026-11-05

> **升级提示**：本版有不兼容改动。AI 生图工具的名称和参数经过重构，升级后请在 AstrBot 插件设置里重新勾选并授权工具；配置中残留的旧调度开关会被自动忽略，无需手动清理。

### Added

- **提示词模板**：快速模式（头像、海报、壁纸、手办化、表情包等）的提示词改为可编辑模板，支持内置模板、自建模板和 JSON 模板包导入导出，生成历史会记下用了哪个模板。
- **提示词广场**：WebUI 新增页签，可按关键词和标签搜索模板，新建／编辑／复制／删除、恢复内置默认、单独控制某个模板是否允许 AI 使用、把模板一键填入创作台；模板预览图不会被容量清理删除。
- **在线模板库**：广场里可以直接搜索查看别人分享的模板、导入成本地副本、同步更新、上传自己的模板、举报以及修改删除自己的模板，并支持密钥的申请、校验、回填和吊销。联网方式在配置项「在线模板使用方式」里选择：仅本地缓存（默认）、实时检索、完全不联网。
- **三个新的 AI 工具**：`gemini_image_lookup` 查询可用模型、模板、可引用的历史生成记录和后台任务；`gemini_image_share` 由本人共享或取消共享生成记录；`gemini_image_batch` 一次提交多张批量生成，公共参数写在顶层、单张可覆盖。
- **生成结果可引用**：生成的图会带引用号，可直接要求 AI 拿某张已生成的图继续改图；默认只允许引用本会话的记录，跨会话引用需打开配置项「允许跨会话引用生成图」。
- **WebUI 界面重写**：六个页签全部改为组件化实现，切换与加载更顺畅，前端改动纳入自动化检查；同时补全了从收到消息到发回结果的全过程日志，排查问题开 DEBUG 即可追踪单个任务。

### Changed

- 命令、AI 工具（单图与批量）、WebUI 创作台和其他插件调用全部改由统一的生成任务系统处理，旧的生成流程已删除；使用方式不变。
- 额度和队列改为提交时一次性判定：额度不足或队列已满时任务不会创建，也不再出现“先提示生成中、随后又报错”；不在允许名单的群仍然静默不回复。任务一旦开始，之后失败或取消不退还额度（与旧版一致）。
- AI 工具更可靠：前台等待时取消请求会同时取消生成；下载慢导致前台超时时不再默默转后台，而是直接返回图片地址让 AI 发送；转后台的任务完成后会把图片发回原会话，发送失败也会保留已经生成的图片。
- AI 工具说明改为面向实际用法的描述，去掉内部术语；明确指定分辨率和比例时优先于默认尺寸；任务完成后会自动通知，无需反复查询。
- WebUI 不再明文显示供应商密钥，只显示数量和掩码预览，密钥的保存与读取都以配置文件为准；所有插件页面都需要登录或 API Key 授权，并校验请求来源。
- 网络图片改为先读文件头判断是否为合法图片，不合格立即停止下载，超大非图片不再整份下载完才报错；保留重定向次数与 20MB 体积上限，并在保存前清理无用元数据减小体积。
- 生成历史里的来源链接不再保留签名、Token 等参数（旧记录仅在展示时脱敏），失败任务不能再用这类链接手动取回图片；已成功的图片不受影响。
- 其他插件调用公开接口时任务记录、回调、等待与取消保持兼容，队列已满会明确告知原因。
- 运行环境统一为 Python 3.12。

### Removed

- 移除 AI 工具 `gemini_image_provider_models` 与 `gemini_image_task_status`，功能并入 `gemini_image_lookup`；升级后需重新勾选授权。
- 单图工具不再接受 `batch_tasks`（改用 `gemini_image_batch`）和遗留的 `size`（改用 `resolution` 与 `aspect_ratio`），以及 `provider`、`use_reference_images`、`include_user_avatar`、`reference_image_paths`、`preserve_reference_image_size`、`negative_prompt`、`watermark`、`quality`、`for_forum`；传入这些旧键会被忽略而不报错。
- 移除旧调度核心与 `retry_settings` 下的 `scheduler_v2`、`scheduler_v2_sessions` 两个灰度开关。

### Fixed

- 修复中文环境下 WebUI 界面整页变成英文。
- 修复供应商返回思维内容标记时，图片其实已经生成成功却被判为失败。
- 修复只返回文字、没有图片的结果被当成成功：表情包、换风格和 AI 工具单图现在按失败提示“图像文件未找到”并记入历史（`/生图` 等快捷命令仍允许只回文字）。
- 修复已经成功的生成记录会被之后的发送失败或取消改写成“中断／失败”。
- 修复失败提示里出现内部调试文字，现在和旧版一样给出可读原因（网络、超时、额度等）。
- 修复在线模板库密钥错误时会把 Dashboard 踢回登录页，现在只提示密钥无效。
- 修复其他插件传入的本地参考图被静默丢弃，本地图片现在能正常作为参考。

## Earlier Versions

See [GitHub Releases](https://github.com/piexian/astrbot_plugin_gemini_image_generation/releases) for full changelog history.
