# 核心调度器 v2 重构说明

> 本目录记录插件生图调度层的目标架构、当前状态、兼容承诺和贡献约定。这里的“v2”只指核心调度器重构，与供应商配置重构及 `rate_limit_buckets_v2` 无关。

## 为什么重构

命令、LLM 工具、WebUI 创作台和公开 SDK 曾分别创建后台任务、记录历史、处理限流与资源释放，导致排队、取消和清理语义不一致（见[重构前路由](current-routing.md)）。

v2 将入口收敛为同一条链路：入口只转换 `GenerationRequest` 和兼容响应，调度器统一负责状态、准入、deadline、取消、投递和资源租约，供应商保持为纯适配器。

## 文档地图

| 文档 | 内容 | 更新方式 |
| --- | --- | --- |
| [scheduler-v2.md](scheduler-v2.md) | 目标架构、状态机、Provider contract、资源与安全边界 | 设计变更时更新 |
| [migration.md](migration.md) | 灰度开启、行为差异、数据与回滚 | 行为或兼容策略变化时更新 |
| [current-routing.md](current-routing.md) | 重构前路由快照 | 不再更新 |
| [开发指南](../development.md) | 环境、验证、提交约定、模块地图 | 工具链或模块边界变化时更新 |
| 本页 | 当前状态、兼容承诺、贡献约定 | 接入状态变化时更新 |

## 当前状态

最后核对：2026-10-28。

| 内容 | 状态 | 用户可见影响 |
| --- | --- | --- |
| 领域模型、SQLite JobStore、JobScheduler、ProviderRouter | 已完成 | 构成统一执行底座 |
| 参考图解析、临时句柄、网络抓取与租约 | 已完成 | 本地与内联参考图按统一边界处理 |
| 命令入口 | 灰度中 | 由 `scheduler_v2` 与会话名单控制 |
| LLM 工具单图与批量 | 灰度中 | 支持统一投递、父子 Job 与补足张数 |
| WebUI 创作台单图与批量 | 灰度中 | 名单为空时随总开关接入 |
| 公开 SDK | 灰度中 | 按 `umo` 命中灰度；无 `umo` 时要求名单为空 |
| WebUI 安全统一 | 已完成 | 统一路由守卫、Key 掩码、出站策略与源链接脱敏 |
| Provider 契约测试、迁移与回滚文档 | 已完成 | 默认收集到 CI 测试套件 |
| 旧调度逻辑删除 | 待办 | 先弃用并稳定运行，再切换默认值后删除 |

v2 已覆盖命令、LLM 工具（单图与批量）、WebUI 创作台和公开 SDK。`scheduler_v2` 默认关闭，旧路径仍是默认执行路径；`scheduler_v2_sessions` 用于按完整 UMO 灰度。

## 兼容承诺

迁移期间保持：

- 命令名称、AstrBot 事件回复与图片发送方式。
- LLM 工具名、前台/后台提示和任务查询方式。
- WebUI 现有 API 路径与请求/响应结构。
- 公开 SDK `get_service(1)`、提交、等待、任务状态与回调语义。
- 现有供应商配置格式；仅新增调度开关与灰度名单配置键。

行为差异和安全策略调整集中记录在[迁移与回滚指南](migration.md)及 `CHANGELOG.md`，不把“接口兼容”理解为所有边界行为完全一致。

## 贡献约定

入口层继续小步修改，避免同时重写多个入口：

- 命令：`main.py`
- LLM 工具：`tl/llm_tools.py`、`tl/batch_generation.py`、`tl/background_tasks.py`
- WebUI：`tl/web_studio_service.py`、`tl/web_api.py`、`tl/generation_tracker.py`
- SDK：`tl/plugin_service.py`
- v2 核心：`tl/core/` 与 `tl/v2_*.py`

供应商适配器（`tl/api/`）和配置格式是兼容边界。新增供应商按[《新增 API 供应商》](../新增API供应商.md)实现，并运行 `tests/test_provider_contracts.py`；无需为 v2 另写调度、限流或历史逻辑。
