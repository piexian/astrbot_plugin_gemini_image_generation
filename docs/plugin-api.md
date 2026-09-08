# 其他插件接入指南（API v1）

本接口供同一 AstrBot 实例内的插件复用已配置的图片供应商。每次提交一个提示词，可生成多张图片；不需要聊天事件，也不自动发送图片或唤醒聊天 Agent。

## 获取服务实例

使用 AstrBot 的插件注册接口获取已启用的插件，再获取公开服务。不要导入 `tl/` 内部模块、读取 API Key 或保存内部客户端。

```python
async def get_image_service(context):
    metadata = context.get_registered_star("astrbot_plugin_gemini_image_generation")
    if metadata is None or metadata.star_cls is None:
        raise RuntimeError("请先安装并启用 Gemini 图像生成插件")
    getter = getattr(metadata.star_cls, "get_service", None)
    if getter is None:
        raise RuntimeError("当前生图插件版本不支持公开接入接口")
    service = getter(api_version=1)
    await service.wait_ready(timeout=10)
    return service
```

`get_service()` 在同次加载期间返回同一个实例；不支持的 API 版本会抛出带 `code="unsupported_version"` 的异常。配置缺失时仍能获取实例并查询状态。

`get_status()` 为同步方法，返回：

| 字段 | 含义 |
| --- | --- |
| `api_version` / `instance_id` | 协议版本和本次服务加载标识 |
| `state` | `initializing`、`ready`、`unavailable`、`closing` 或 `closed` |
| `ready` | 本地服务、供应商配置是否就绪，不代表远端网络或余额正常 |
| `accepting_tasks` | 此刻是否允许提交；队列满时为 false，但仍可 ready |
| `reason` | `not_configured`、`configuration_updating`、`configuration_failed`、`queue_full`、`service_closed` 等原因，正常为 null |
| `active_requests` / `queued_requests` | 全入口生成执行槽占用和等待数量 |

`wait_ready(timeout=None)` 等待就绪；可传秒数限制等待，超时抛出 `TimeoutError`。关闭时返回明确错误。查询状态不初始化客户端、不发起网络请求、不扣额度。状态可能在查询后变化，仍需处理提交失败。

供应商热更新保留服务实例；插件卸载或重载后，旧实例永久关闭。调用方应重新从注册接口获取新实例，不能仅以“持有实例”判断已经就绪。

## 最小提交与等待

```python
async def generate_cover(context, prompt):
    service = await get_image_service(context)
    accepted = await service.submit(
        plugin_id="astrbot_plugin_my_story",
        plugin_name="故事插件",
        prompt=prompt,
    )
    result = await service.wait_task(
        accepted["task_id"], plugin_id="astrbot_plugin_my_story", timeout=180
    )
    if result["status"] not in {"succeeded", "partial_success"}:
        raise RuntimeError(result.get("error") or "图片生成未完成")
    return result["image_urls"], result["image_paths"]
```

`wait_task()` 未传 `timeout` 或传入 `None` 时，使用插件的 `total_timeout`（秒）；显式传入则使用调用方指定的等待时长。超时或调用方取消等待，不会取消后台生成。之后可再次等待，或使用 `await service.get_task(task_id, plugin_id=...)` 查询。

结果收尾时若任务或历史持久化失败，仍保留当前进程内可查询的终态和图片结果，释放等待者并尝试完成回调，同时记录诊断日志。磁盘写入失败时不能保证这些结果在重启后恢复。

## 提交参数

`submit()` 为异步方法，所有参数均按名称传递。

| 参数 | 默认值及约定 |
| --- | --- |
| `plugin_id` | 必填，稳定插件标识；非空、最多 128 字符，不含控制字符 |
| `prompt` | 必填，非空提示词 |
| `plugin_name` | 可选显示名称，最多 200 字符；修改名称不改变任务归属或限流桶 |
| `umo` | 可选完整会话标识：`平台实例ID:消息类型:会话ID` |
| `requester` | 可选对象，支持 `user_id`、`user_name`、`group_id`、`chat_type`；仅用于来源展示 |
| `use_rate_limit` | 默认 false，由调用插件自行限流；true 时接入下文规则 |
| `reference_images` | 可选 HTTP(S) URL 或存在的本地绝对文件路径列表；支持本地 file URI |
| `provider` / `model` | 可选供应商和模型／别名；不指定时沿用已配置的轮询候选 |
| `image_count` | 默认 1，上限取自 `capabilities().max_images_per_task`，不足时按现有逻辑补齐 |
| `resolution` / `aspect_ratio` | 可选尺寸／分辨率及比例，按供应商能力处理 |
| `negative_prompt` / `watermark` / `quality` | 可选公共生成参数，只路由到支持它们的候选 |
| `on_complete` | 可选异步函数，参数为最终任务记录 |

`capabilities()` 同步返回 `api_version`、`max_images_per_task` 和 `candidates`。候选描述复用现有模型能力数据，不包含密钥。不提供临时凭证、覆盖供应商配置或命名批量任务接口。

有 AstrBot 事件时，将 `event.unified_msg_origin` 传入 `umo`；只提供群号或用户 ID 不会推断出完整会话。无会话时只声明插件身份即可调用。插件身份用于同实例协作和追踪，并非针对不可信插件的认证边界。

## 限流与并发

默认不消耗本插件的限流额度，由调用插件自行限制调用频率。所有请求仍受统一生成并发与队列容量约束。

显式传 `use_rate_limit=True` 后：

- 有完整 UMO：匹配现有会话规则，未匹配则使用默认限流，与同 UMO 的其他调用共享计数。
- 无 UMO：使用默认限流，按 `plugin_id` 独立计数；不额外应用会话规则。
- 全局限流开启时再叠加全局检查；全局关闭不影响前两项生效。
- 一次提交按一个逻辑任务计数，多图补齐和重试不重复扣额度。任务已接收后取消不退额度。

例如仅开启“默认每分钟 5 次”：插件 A 和 B 不传 UMO 时各自拥有 5 次额度；传入同一个 UMO 时则共享该会话额度。显示名称变化不会重置额度，计数沿用现有 KV 持久化。

```python
# 自己限流（默认）
await service.submit(plugin_id="astrbot_plugin_a", prompt="画一只猫")

# 无会话：默认规则按插件标识计数
await service.submit(
    plugin_id="astrbot_plugin_a", prompt="画一只猫", use_rate_limit=True
)

# 有会话：会话规则/默认规则按 UMO 计数
await service.submit(
    plugin_id="astrbot_plugin_a", prompt="画一只猫",
    umo=event.unified_msg_origin, use_rate_limit=True,
)
```

指令、LLM、Studio 和外部插件共享 FIFO 调度。默认同时执行 3 个生成调用，最多等待 100 个；对应配置为 `image_generation_settings.generation_max_concurrency` 和 `image_generation_settings.generation_max_queue_size`，重载后生效。原有 Studio、批量及供应商局部并发限制仍生效。

排队不消耗供应商请求超时，LLM 前台等待仍会按实际时间切到后台。多图补齐的下一次调用重新排队。队列满返回繁忙错误，不自动无限重试。

## 回调与插件卸载

```python
class ImageConsumer:
    def __init__(self, context):
        self.context = context
        self.subscriptions = []

    async def start(self, prompt, event=None):
        service = await get_image_service(self.context)
        accepted = await service.submit(
            plugin_id="astrbot_plugin_my_story",
            plugin_name="故事插件",
            prompt=prompt,
            umo=event.unified_msg_origin if event else None,
            requester={"user_id": str(event.get_sender_id())} if event else None,
            on_complete=self.on_image_ready,
        )
        self.subscriptions.append((service, accepted["task_id"]))
        return accepted["task_id"]

    async def on_image_ready(self, result):
        # 在这里交给自己的排版、上传或发送流程；不要再次生成同一张图。
        self.last_result = result

    async def terminate(self):
        for service, task_id in self.subscriptions:
            try:
                await service.remove_callback(
                    task_id, plugin_id="astrbot_plugin_my_story"
                )
            except RuntimeError:
                # 服务可能已经重载，或任务记录已经过期。
                pass
        self.subscriptions.clear()
```

回调在生成结果保存后运行，不占生成执行槽，最多等待 30 秒。同次运行每个任务最多尝试一次，不自动重试。`remove_callback()` 只解除尚未执行的回调，返回是否成功解除；调用插件需自行结束已经开始的业务处理。

`callback_status` 与生成状态独立，可能为 `none`、`pending`、`running`、`removed`、`succeeded`、`failed` 或 `interrupted`。回调异常不将已成功的生图改成失败，查询结果仍可用于补发。等待任务结果不等待回调执行完毕。

## 任务状态、错误与图片保存

生成状态为 `queued`、`running`、`succeeded`、`partial_success`、`failed`、`interrupted`。结果包含 `task_id`、`plugin_id`、`caller`、`requester`、`requested_images`、`generated_images`、`image_urls`、`image_paths`、`text_content`、`stats`、`error`、`callback_status`，启用历史时还有对应的 `job_id`。

查询、等待和回调共用同一份生成结果。归档完整时优先返回归档后的本地路径；否则保留供应商原始地址或生成路径。路径和 URL 沿用现有保留、配额及清理策略，不保证永久有效，长期使用应及时自行复制或上传。

`await service.cancel_task(task_id, plugin_id=...)` 取消本地排队或执行，终态任务不变。不能保证撤回供应商已经受理的请求或费用。服务关闭会中断未完成任务；重启后可以重新获取实例查询保留的任务记录，不恢复生成、不重放回调。

提交和服务错误为 `RuntimeError` 的子类，提供稳定 `code`，如 `invalid_request`、`not_ready`、`not_configured`、`queue_full`、`rate_limited`、`service_closed`、`task_not_found`。版本不匹配为 `unsupported_version`；访问其他插件任务会抛出 `PermissionError`。生成开始后的失败写入任务 `status/error`，不要求解析用户提示文本来判断成功。

## WebUI 来源记录

任务列表、历史画廊及图片详情显示“插件调用 · 插件名称”和稳定插件标识。可按“插件调用”来源、插件标识筛选，或搜索插件名称／标识。仅显示调用方提供的会话和使用者信息；无需填写群号或用户才能记录来源。

关闭生成历史时不新增插件历史和归档记录，任务查询仍可用。回调结果单独展示，不覆盖生成状态。
