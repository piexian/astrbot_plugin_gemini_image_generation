# H1 — 拆分 main.py(分阶段;本阶段:折叠 30 个样板 property 为 `__getattr__`)

> 状态: 第一阶段实施
> 调整: main.py 1700+ 行,原 plan 设想拆出 `provider_resolver.py` / `command_router.py`,但 `GeminiImageGenerationPlugin` 命令方法用 `@filter.command` 装饰器,必须留在 Star 类内,无法外移。
>
> 改为 **分阶段**,优先消除最大量样板代码:30 个 `@property` 全部形如 `return self.cfg.X`,共 ~120 行纯转发,且经全工作区扫描 **无任何调用方**(内部全部直接用 `self.cfg.X`,外部无 `plugin.X` 访问)。

## 本阶段范围

`main.py` 改动:
- 删除 28 个纯转发 `@property` 定义(约 110 行),范围: line 1586–1697 中所有 `return self.cfg.<同名>`
- 保留 2 个有自定义逻辑的 property:
  - `image_input_mode` -> hardcoded `"force_base64"`
  - `config` -> 返回 `self.raw_config`(属性名重映射)
- 新增 `__getattr__(self, name)` 回退:若 `self.cfg` 有该属性则返回,以保留向后兼容(防御未来未审视到的反射访问)

## 风险

低。
- `__getattr__` 仅在普通属性查找失败后触发,不影响现有显式属性。
- `self.cfg` 是 PluginConfig 对象;访问不存在的属性会按 dataclass 标准抛 AttributeError。
- 经 grep 验证 28 个 property 当前 0 调用方,即使 `__getattr__` 失败也不会破坏任何代码路径。

## 后续阶段(本次不做)

| 阶段 | 目标 |
|---|---|
| H1-2 | 抽取 `_load_provider_from_context` (~160 行)到 `tl/provider_resolver.py` |
| H1-3 | 抽取头像/参考图收集辅助方法到 `tl/event_image_collector.py` |
| H1-4 | 命令体本身留在 main.py,但每个 `@filter.command` body 内核业务逻辑下沉到 helper 模块 |

## 验证

1. ruff format + check
2. compileall
3. grep 确认 `image_input_mode` 与 `config` 属性仍正常定义
