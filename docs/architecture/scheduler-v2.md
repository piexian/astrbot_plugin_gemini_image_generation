# 核心调度器 v2 目标架构

## 目标边界

命令、LLM Tool、WebUI Studio 和 SDK 最终都只负责把入口参数转换为
`GenerationRequest`，调用 `GenerationService.submit()`，再把 Job 结果转换为
各自的兼容响应。Provider 仍是适配器，不负责任务调度、历史、quota、限流或
文件生命周期。旧 `tl_api`、`GenerationTracker` 和 `BackgroundTaskManager`
在迁移期间保留为兼容层，待四个入口都由新调度器覆盖且测试稳定后再删除重复逻辑。

## 核心对象

`tl/core/` 的第一阶段领域模型定义了以下边界：

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

## 资源与安全边界

每个 Job 都要有明确的：

1. cancellation 传播和调用方 `CancelledError` 处理；
2. quota reservation 与 limiter reservation 释放/退款策略；
3. reference lease、artifact lease、delivery lease 的释放；
4. provider session、临时文件和输出归档 cleanup。

ReferenceService 统一接收本地路径、file URI、HTTP URL、data URI、base64 和
平台图片。PathPolicy/RemoteFetchService 必须阻止路径穿越、符号链接逃逸、
loopback、私网、link-local、云元数据地址和重定向绕过，并限制响应体、像素数、
连接时间和总时间。候选 provider 的 proxy 由 service 传入。source URL 在写入
history、响应和日志前必须脱敏；WebUI 只能看到 masked secret 以及 keep/replace/
clear 状态。

## 持久化与恢复

第二阶段的 JobStore 使用 sqlite3 + WAL 和版本化 schema migration，至少包含
`jobs`、`attempts`、`artifacts`、`leases`、`job_events`。所有保存由串行 writer
完成，使用每次唯一的临时文件/事务，不让旧线程覆盖新状态。启动时兼容导入
`background_tasks.json` 和 `generation_history.json`；单个损坏 JSON 只备份并
记录 orphan/interrupted 状态，不能阻止插件启动。

恢复只会重新排队可安全恢复的 accepted/queued Job；无法确认 runtime ownership
或 lease 的记录标记为 `orphaned`，并执行一次幂等 cleanup。历史 API 在迁移期间
继续输出旧字段和 `partial_success` 等兼容值，由 facade 做状态映射。

## 分阶段接入顺序

第一阶段（当前）只提交领域模型、协议、lease 生命周期测试和本文档。后续按
JobStore → JobScheduler → ProviderRouter → ReferenceService → 命令 → LLM Tool →
WebUI Studio → SDK 的顺序迁移；每次迁移都运行项目测试、ruff、compileall，
并停在可审查提交，不自动跨入下一高风险阶段。
