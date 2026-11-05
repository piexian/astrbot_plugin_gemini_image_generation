# 核心调度器 v2

> “v2”只指核心调度器重构，与供应商配置重构及 `rate_limit_buckets_v2` 无关。

## 为什么重构

命令、LLM 工具、WebUI 创作台和公开 SDK 曾分别创建后台任务、记录历史、处理限流与资源释放，导致排队、取消和清理语义不一致。

v2 把入口收敛为同一条链路：入口只转换 `GenerationRequest` 和兼容响应，调度器统一负责状态、准入、deadline、取消、投递和资源租约，供应商保持为纯适配器。

## 现状

命令、LLM 工具（单图与批量）、WebUI 创作台和公开 SDK 已全部由 v2 执行，旧调度核心与 `scheduler_v2` / `scheduler_v2_sessions` 灰度配置已移除。`tl_api` 仍是供应商客户端的统一入口；`GenerationTracker` 与 `BackgroundTaskManager` 继续承载 `generation_history.json`、`background_tasks.json` 的旧数据格式与损坏恢复，不再各自调度生成。

- 目标架构、状态机、Provider contract、资源与安全边界：[scheduler-v2.md](scheduler-v2.md)
- 升级后的行为差异、数据兼容与维护操作：[migration.md](migration.md)
- 环境、验证步骤与提交约定：[开发指南](../development.md)

## 兼容承诺

保持：

- 命令名称、AstrBot 事件回复与图片发送方式。
- LLM 工具名、前台/后台提示和任务查询方式。
- WebUI 现有 API 路径与请求/响应结构。
- 公开 SDK `get_service(1)`、提交、等待、任务状态与回调语义。
- 现有供应商配置格式。

行为差异和安全策略调整集中记录在[升级与兼容说明](migration.md)及 `CHANGELOG.md`，不把“接口兼容”理解为所有边界行为完全一致。

## 贡献约定

入口层继续小步修改，避免同时重写多个入口：

- 命令：`main.py`
- LLM 工具：`tl/llm_tools.py`、`tl/batch_generation.py`、`tl/background_tasks.py`
- WebUI：`tl/web_studio_service.py`、`tl/web_api.py`、`tl/generation_tracker.py`
- SDK：`tl/plugin_service.py`
- v2 核心：`tl/core/` 与 `tl/v2_*.py`

供应商适配器（`tl/api/`）和配置格式是兼容边界。新增供应商按[《新增 API 供应商》](../新增API供应商.md)实现，并运行 `tests/test_provider_contracts.py`；无需为 v2 另写调度、限流或历史逻辑。
