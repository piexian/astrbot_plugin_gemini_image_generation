# 当前生图路由（重构前）

本文记录第一阶段开始时仓库中实际存在的路由和兼容行为。它描述现状，
不是新调度器的实现承诺。旧入口、`GenerationTracker` 和
`BackgroundTaskManager` 在迁移完成前必须继续可用。

## 生命周期和共享对象

插件在 `main.py` 的构造函数中按下面顺序创建主要组件：

1. `ConfigLoader` 读取用户现有配置和 provider candidates。
2. `GenerationScheduler` 提供旧的 FIFO 并发 admission，并注入
   `GeminiAPIClient`。
3. `BackgroundTaskManager` 从插件数据目录读取 `background_tasks.json`。
4. `GenerationTracker` 读取 `generation_history.json`，维护 gallery、历史和
   SSE 订阅。
5. `WebStudioService` 使用 tracker 执行工作台任务；随后初始化
   `RateLimiter`、`KeyManager`、`ImageGenerator`、消息发送器和视觉处理器。

`_load_api_client_from_config()` 将 `provider_candidates`、密钥轮换器、代理和
旧 scheduler 绑定到 `GeminiAPIClient`。`terminate()` 先关闭公开 SDK 服务和
旧 scheduler，再关闭 WebUI、后台任务、tracker、HTTP session 和限流器。
这套顺序是现有插件重载的兼容行为。

## 入口和任务路径

| 入口 | 现有入口函数 | 当前任务/记录路径 | 对外兼容面 |
| --- | --- | --- | --- |
| 命令 | `main.py` 的 `/生图`、`/快速/*`、`/改图`、`/换风格` 等 | 普通快捷生成在 `_quick_generate_image()` 中抓取参考图、检查限流，直接调用 `api_client.generate_image()`；部分改图/切图流程直接调用 `ImageGenerator.generate_image_core()`。成功后归档 gallery 并写 `GenerationTracker`。 | 命令名称、AstrBot 事件回复和图片发送方式保持不变。 |
| LLM Tool | `GeminiImageGenerationTool`（工具名 `gemini_image_generation`） | 单图先解析工具参数、参考图白名单和候选能力；前台短等待，超时后写 `BackgroundTaskManager` 并继续后台投递。批量任务创建后台父记录和 tracker 父记录，再运行 `run_batch_job()`。 | 工具名、参数、前台/后台提示和 `gemini_image_task_status` 查询继续兼容。 |
| WebUI Studio | `WebStudioAPI.generate()` → `WebStudioService.generate()` | API 解析 JSON 后由 service admission、限流、上传文件 lease 和 tracker 记录；单图或 batch 通过 `_attach()` 调用 `asyncio.create_task`，运行 `_execute_generation()`，归档后更新 tracker。 | `/jobs`、`/history`、`/generate`、`/image`、`/providers`、`/providers/models` 等现有 WebUI API 路径保持兼容。 |
| SDK | `ImageGenerationService.submit()` | 公开服务验证参数、选择 candidates、预留旧 scheduler 和限流 token，再调用插件核心生成逻辑；SDK task/callback 状态由 service 维护，并通过 `BackgroundTaskManager` 保存会话记录。 | `get_service(1)`、`submit`、`wait_task`、任务状态和 callback 行为保持兼容。 |

命令和 SDK 当前通常由旧 `GenerationScheduler` admission；WebUI 有自己的
`_api_semaphore`/并发计数；LLM Tool 另外使用 `BackgroundTaskManager`。因此
相同请求从不同入口进入时，排队、记录、取消和资源释放语义并不完全一致。

## 旧后台任务和历史

`BackgroundTaskManager` 使用 `background_tasks.json`，启动时把 `queued`/
`running` 标记为 `interrupted`，并保留 session/plugin ownership 检查。
`attach()` 直接创建 runtime task，完成回调移除 `_runtime_tasks`。保存采用固定
`.json.tmp` 后 `os.replace`。

`GenerationTracker` 使用 `generation_history.json` 和 `gallery/`：

- 启动时损坏 JSON 会重命名为 `generation_history.json.corrupt-*`，插件继续启动；
- 运行中任务会恢复为 `interrupted`；
- `begin()` 当前直接写 `running`，支持 parent/child、参考图 lease 和请求者元数据；
- `complete()` 支持 `succeeded`/`partial_success`，`fail()` 写 `failed`；
- `/jobs`、SSE、history、delete 直接读取 tracker；删除会尊重仍被参考图引用的 gallery 文件。

这些状态与目标状态集合（`accepted`、`result_ready`、`delivery_pending`、
`cancelled`、`orphaned` 等）不同，第一阶段只新增目标领域模型，不改变旧 JSON
格式或旧状态返回。

## Provider 现状

`tl/api/base.py` 目前只有 `ProviderRequest`、`build_request()` 和
`parse_response()` 协议。`registry.py` 按 `api_type` 懒加载以下现有适配器：

`google`、`gemini_interactions`、`vertex`、`openai`、`agnes_ai`、`xai`、
`minimax`、`stepfun`、`openai_images`、`openai_responses`、`doubao`、
`sensenova`、`senseaudio`、`dashscope`、`modelscope`、`siliconflow`。

`GeminiAPIClient.generate_image()` 负责候选选择、key 轮换、重试、fallback、
请求发送和总时间参数；Provider 负责请求构造和响应解析，部分 Provider 在解析
阶段自行 polling 或下载输出。现有 provider 模块和用户配置格式是兼容边界，
第一阶段不迁移它们。

## 已知调度分散点

仓库中除旧 scheduler 外，`BackgroundTaskManager.attach()`、
`WebStudioService._attach()`、LLM Tool 的 `_create_generation_task()` 和
`_schedule_generation_delivery()` 也会直接创建 task。API client 关闭 session、
字体加载、model catalog、rate limiter 持久化、WebUI 配置事务和公开 SDK 的
finish/callback 还有独立的后台 task。`tl_api.invalidate_session()` 还用
`loop.create_task()` 异步关闭旧 session。后续迁移必须逐个收敛这些路径；第一
阶段不删除或改写旧组件。

## WebUI 路由清单

`WebStudioAPI.ROUTES` 当前注册：`/jobs`、`/jobs/stream`、`/history`、
`/history/<job_id>`、`/history/delete`、`/image/<name>`、`/image_b64/<name>`、
`/capabilities`、`/preferences`、`/generate`、`/upload`、`/limits`、
`/providers`、`/vision-providers`、`/providers/models` 和 `/sessions`。
`jobs`/history 直接读 tracker；`generate` 返回 202 和 `job_id`；provider 配置
由 `ProviderConfigService` 处理。当前只有限流/会话管理路径要求 Dashboard
`username`，生成、history 和图片路径尚未统一到同一个 API guard。

## SDK 任务细节

SDK `submit()` 的返回值包含 `task_id`、状态和实例信息，`get_status()` 暴露旧
scheduler 的 active/queued 计数；`wait_task()` 等待 service 的事件，
`cancel_task()` 取消 service 自己保存的 task。
运行任务写入 tracker 后，`_finish()` 归档结果、更新后台记录和 history，并为
`on_complete` 再创建 callback task。这些字段和 callback 时序需要由未来 facade
继续适配，不能直接把新 JobStore 的内部记录暴露给 SDK。
