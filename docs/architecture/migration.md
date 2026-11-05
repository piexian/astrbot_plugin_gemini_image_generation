# v2 调度器迁移与回滚指南

## 适用范围

v2 已全量覆盖命令、LLM 工具单图/批量、WebUI 创作台和公开 SDK，成为唯一生成调度路径；旧调度执行逻辑及 `retry_settings.scheduler_v2`、`retry_settings.scheduler_v2_sessions` 灰度开关已移除。存量配置文件若保留这两个旧键会被自动忽略，无需手动清理。

## 行为说明

- **统一准入**：额度与生成队列在提交时预留，拒绝发生在任务创建前；已接受任务之后失败或取消不退额度。命令的拒绝提示先于“生成中”等进度提示。
- **错误与完成口径**：生成核心收到无图结果按失败处理，命令快捷路径仍允许纯文本成功。已成功写入的历史不会被后续投递失败改写。
- **LLM 工具**：取消前台调用会同时取消生成；前台结果构建超时直接返回图片 URL/本地路径文本，不再转后台。批量使用父子 Job，子任务独立 deadline 并自动补足张数，最终统一回灌汇总；投递失败保留结果为 `partial`。
- **WebUI**：单图/批量参考图先入库再释放上传租约；生成队列满或服务关闭在提交时返回 429/503，而不是受理后在任务中失败。历史按归档结果判定成功、部分成功或失败。
- **公开 SDK**：任务记录、等待、取消和回调保持兼容；HTTP(S) 参考图受 SSRF 策略限制，本地文件先转临时句柄，队列满稳定抛出 `code="queue_full"`。
- **历史源链接**：`source_urls` 的 query、fragment 和 userinfo 会被移除，旧记录仅在 API 响应中脱敏。失败任务不能再凭带签名参数的源链接手动取回图片，成功任务的 gallery 图片不受影响。
- **供应商与出站安全**：已保存 API Key 只显示数量与掩码；未保存到供应商配置的内网地址默认拒绝，需先保存对应 `api_base`。模型目录与归档下载拒绝 link-local/元数据地址，归档重定向逐跳复检。
- **WebUI 授权**：供应商配置、历史删除、限流、会话、上传等管理操作仅允许面板用户；API Key 可继续使用任务提交与历史读取接口。

## 配置与数据兼容

- v2 任务库位于插件数据目录的 `v2/`，主要文件为 `jobs.sqlite3`，使用 SQLite WAL。
- 生成历史继续使用旧格式与 `partial_success` 等兼容状态；v2 Job ID 比旧记录更长，调用方应将其视为不透明字符串。
- 临时参考图句柄是进程内资源，崩溃后不保证可恢复；恢复出的无运行属主任务按 interrupted/orphaned 处理，不自动重放。
- 旧 `background_tasks.json` 和 `generation_history.json` 的兼容导入与损坏恢复逻辑保留。

## 数据维护

1. 插件关闭或重载时，进行中的 v2 任务会被取消并标记 interrupted；已完成的生成历史和图片保留。
2. 如需重置 v2 任务库，先停止插件，再备份或删除插件数据目录下的 `v2/`；不要在调度器运行时直接删除数据库文件。

## 工具改名

`gemini_image_provider_models` 与 `gemini_image_task_status` 已合并为 `gemini_image_lookup`，用 `kind` 区分 `models`、`templates`、`history` 和 `tasks`。新增 `gemini_image_share`。批量生成从 `gemini_image_generation` 的 `batch_tasks` 拆为独立工具 `gemini_image_batch`。AstrBot 按工具名保存的人格工具列表、工具权限和禁用状态不会自动迁移，升级后需要在 AstrBot 里重新勾选和授权。

单图工具的参数也已更换。批量改由独立工具 `gemini_image_batch` 承载，`gemini_image_generation` 不再接受 `batch_tasks`。

| 旧参数 | 新写法 |
| --- | --- |
| `provider` 与 `model` 分开填写 | `model`：`供应商/模型`、`供应商/`、模型名或别名 |
| `use_reference_images` | `references: ["current"]` |
| `include_user_avatar` | `references: ["avatar:sender"]` 或 `["avatar:mentioned"]` |
| `reference_image_paths` | `references` 里的绝对路径 |
| 顶层 `quality`、`negative_prompt`、`watermark` | `options` 中的同名字段 |
| `preserve_reference_image_size` | 不再由工具传入，改用插件配置 |
| `for_forum` | `deliver: "forum"` |
