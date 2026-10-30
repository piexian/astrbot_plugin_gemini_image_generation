# 核心调度器 v2 目标架构

## 目标边界

命令、LLM Tool、WebUI Studio 和 SDK 最终都只负责把入口参数转换为
`GenerationRequest`，调用 `GenerationService.submit()`，再把 Job 结果转换为
各自的兼容响应。Provider 仍是适配器，不负责任务调度、历史、quota、限流或
文件生命周期。旧 `tl_api`、`GenerationTracker` 和 `BackgroundTaskManager`
在迁移期间保留为兼容层，待四个入口都由新调度器覆盖且测试稳定后再删除重复逻辑。

## 核心对象

`tl/core/` 的领域模型定义了以下边界：

- `GenerationRequest`：不可变、可持久化的请求快照；参考图只保存引用标识。
- `Job`：状态、父子关系、deadline、结果和错误的聚合；创建后先持久化，
  再附加 runtime task。
- `GenerationResult`：只包含稳定 artifact 引用、文本和安全统计，不包含密钥
  或原始 provider 响应。
- `ProviderAttempt`：每次尝试独立编号并携带 deadline，供 attempt/body factory
  契约重新构造请求体。
- `Lease`/`LeaseSet`：quota、limiter、reference、artifact、delivery 的统一
  幂等释放边界；取消、异常和关闭都必须经过同一 cleanup 路径。
- `protocols.py`：JobStore、Scheduler、Provider、ReferenceService、quota、
  limiter 和 artifact store 的最小依赖协议。

## Job 状态机

正常路径为：

```text
accepted -> queued -> running -> result_ready -> delivery_pending -> succeeded
                                                        \-> partial
```

任何可运行阶段都可以因调用方取消、关闭或崩溃恢复转入 `cancelled`、
`interrupted` 或 `orphaned`；provider 错误转入 `failed`。`partial` 表示已经
有可交付输出但未满足请求的全部产出。终态不可再次运行。

`accepted` 的持久化时点早于 runtime task 创建。submit 与 close 并发时，无法
附加 runtime task 的 accepted Job 必须转为 `orphaned` 并释放已经获得的 lease。

## 结果投递阶段

入口可以在 Job 运行期间用 `GenerationService.attach_delivery(job_id, delivery)`
（或提交时 `submit(..., delivery=...)`）登记结果投递方（`DeliveryProtocol`：
`deliver(job, result)` 与 `deliver_failure(job)`）。`attach_delivery` 是同步方法，
与调度器的投递判定点（`result_ready` 落库、`job_result` 观察者通知之后）不会交错：
返回 `True` 时，Job 结束会调用 `deliver`（有结果）或 `deliver_failure`（`failed`、
`cancelled`、`interrupted`、`orphaned`，且 `deliver` 尚未接手；早于 `job_finished`
观察者）之一；返回 `False`（Job 未知或已无 runtime task、已登记过、已越过判定点、
调度器已关闭）时结果由入口自行处理。

有投递方时 Job 先转 `delivery_pending`，`deliver` 须在 `delivery_timeout`（默认
600 秒）内完成：正常返回按 `result.partial` 收尾为 `succeeded` / `partial`；抛出普通
异常或超时则收尾为 `partial` 且 `error_code="delivery_failed"`，结果仍保留；
`deliver` 执行期间 Job 被取消则照常以 `cancelled` 收尾。没有投递方时流程不变。
生成历史由 `TrackerProjection` 投影：Job 已有结果且 `job_result` 已把记录收尾时，
投递阶段的取消或失败不再改写该记录。

## 运行时分层

```text
入口适配器
  -> GenerationService (参数/权限/兼容 facade)
  -> JobScheduler (状态、排队、取消、deadline、parent/child)
  -> JobStore (SQLite WAL；jobs/attempts/artifacts/leases/job_events)
  -> ReferenceService / RemoteFetchService / PathPolicy
  -> ProviderRouter (supports/normalize/build_request/send/parse_response)
  -> ArtifactStore + Delivery adapters
```

JobScheduler 是唯一允许为生成 Job 创建 runtime task 的组件。入口不能选择
Provider、重试、直接操作 tracker/quota/临时文件，也不能通过
`asyncio.create_task` 绕过 scheduler。生命周期组件（JobStore writer、SSE
fan-out、配置事务等）若需要后台 task，必须明确 ownership 和关闭路径。

## Provider contract

Router 对每个候选按统一顺序执行 `supports`、`normalize`、
`build_request(attempt)`、`send`、`parse_response`。`build_request` 必须返回可在
当前 attempt 重新生成 body 的 factory；retry 失败时不得静默复用可能已消费或
过期的 payload。总 deadline 包含参考图下载、请求构造、API 请求、输出下载和
backoff；每次 retry 按剩余时间截断。

Provider 只能报告能力、构造请求、发送和解析结果。候选选择、fallback、错误
分类、retry 和 polling 顺序由 Router/Scheduler 统一决定。


## 父子 Job 与生成历史投影

`submit_batch(parent, children, concurrency=...)` 在返回前持久化父 Job 与全部子 Job。父 Job 不执行 provider，也不占用生成执行槽；子 Job 按并发上限启动，deadline 从各自实际启动时刻计算。父取消会级联取消子 Job，全部子任务失败时父任务以 `batch_all_failed` 收尾。

带 `fill_image_count` 的请求在单次返回不足目标张数时继续生成，保留去重后的已有图片；后续无新增或失败时以部分结果收尾。生成历史由 `TrackerProjection` 根据 `job_accepted`、`job_result`、`child_finished` 和 `job_finished` 写入，批量父记录只汇总子记录状态与张数，图片归档在子记录上。
## 资源与安全边界

每个 Job 都要有明确的：

1. cancellation 传播和调用方 `CancelledError` 处理；
2. quota reservation 与 limiter reservation 释放/退款策略；
3. reference lease、artifact lease、delivery lease 的释放；
4. provider session、临时文件和输出归档 cleanup。

quota 与 limiter 在 `JobScheduler.submit()` 内、落库之前依次预留（先 quota 后
limiter）：组件的 `reserve(request, admission)` 同步或异步均可，返回 `None`（无需
预留）或带幂等 `release()` 的预留对象，可选 `refund()` 与 `runtime_scope()`；
`admission` 是调用方传入、不持久化的准入上下文（命令入口传事件）。任一步拒绝
（`QuotaExceededError`、`QueueFullError`、`ServiceClosedError` 等）都不创建 Job，
异常原样抛给调用方，已拿到的预留按逆序 `refund()`（没有则退化为 `release()`），
落库或建 task 失败同样回滚。Job 被接受后预留在运行时转成持久化的 QUOTA/LIMITER
lease，`runtime_scope()` 在 provider 执行期间生效，终态只 `release()`，不退款。
task 在取走预留之前就结束（提交即取消、close）时由调度器兜底释放，`close()`
结束时登记表必为空。

入口拿到的内联图（data URI / 裸 base64）先经 `ingest_inline` 校验落盘并换成
`ref:<kind>:<id>` 短句柄再放入 `GenerationRequest`；ReferenceService 内部按 kind
分派到 ReferenceStore（`tmp` 为进程内 TTL 存储），执行时把句柄解析回落盘产物，
句柄只允许登记的 requester 解析。
tmp 句柄按 requester 限制容量，活动 reference lease 内不受 TTL/容量淘汰；
容量不足时拒绝新句柄，不跨 requester 驱逐。
入口已按自己的规则校验过本地路径（LLM 工具的 `tool_path_guard`）时，
`prepare_references(..., trusted_local=True)` 会把 `local` / `file:` 形态也读入并换成
句柄：先 `stat` 判大小、读取量以上限封顶，只套用大小、格式与像素边界，PathPolicy
的本地根不因此放宽；默认 `False`，本地路径仍原样保留并在执行阶段按 PathPolicy 校验。

ReferenceService 统一接收本地路径、file URI、HTTP URL、data URI、base64 和
平台图片。PathPolicy/RemoteFetchService 必须阻止路径穿越、符号链接逃逸、
loopback、私网、link-local、云元数据地址和重定向绕过，并限制响应体、像素数、
连接时间和总时间。候选 provider 的 proxy 由 service 传入。source URL 在写入
history、响应和日志前必须脱敏；WebUI 只能看到 masked secret 以及
keep/replace/append/clear 状态。
DNS resolver 对实际连接 IP 再执行同一网络策略，防止校验与连接之间的
DNS rebinding。

## 持久化与恢复

持久化层的 JobStore 使用 sqlite3 + WAL 和版本化 schema migration，包含
`jobs`、`attempts`、`artifacts`、`leases`、`job_events`。所有保存由串行 writer
完成，使用每次唯一的临时文件/事务，不让旧线程覆盖新状态。启动时兼容导入
`background_tasks.json` 和 `generation_history.json`；单个损坏 JSON 只备份并
记录 orphan/interrupted 状态，不能阻止插件启动。

`recover()` 只返回可恢复快照，不自动重放；带 tmp 引用的 Job 以 `has_tmp_references`
标记并排除。无法确认 runtime ownership 或 lease 的记录标记为 `orphaned`，并执行
幂等 cleanup。历史 API 在迁移期间继续输出旧字段和 `partial_success` 等兼容值，
由 facade 做状态映射。

## 接入状态

领域模型、协议、JobStore、JobScheduler、ProviderRouter、ReferenceService 与投递适配器均已落地。命令、LLM 工具单图/批量、WebUI 创作台和公开 SDK 已接入灰度：由默认关闭的 `scheduler_v2` 与 `scheduler_v2_sessions` 控制；WebUI 及无 `umo` 的 SDK 调用仅在名单为空时接入。WebUI 路由守卫、masked secret、出站策略和历史源链接脱敏已统一。

旧路径仍保留为默认执行与回滚路径。后续工作是全量灰度验证、默认值切换评估，以及满足迁移文档条件后删除重复逻辑；每次变更继续运行完整 pytest、ruff 和 compileall。
