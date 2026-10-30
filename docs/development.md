# 开发指南

## 环境

- Python 3.12+，与 CI 基线一致。
- Node.js 22，用于 Studio 前端沙箱与 i18n 测试。
- 在项目虚拟环境中安装依赖，不修改全局环境：

```bash
python -m pip install -r requirements.txt \
  pytest pytest-asyncio aiohttp Pillow "pydantic>=2" \
  jsonschema starlette python-multipart
```

## 验证

完整测试与语法检查：

```bash
pytest -q
python -m compileall -q .
```

契约测试默认包含在完整测试中，也可单独运行：

```bash
pytest -q tests/test_provider_contracts.py
```

Ruff lint 参数与 `.github/workflows/code-quality.yml` 保持一致：

```bash
ruff check . \
  --line-length 88 \
  --target-version py312 \
  --extend-exclude plans,docs,templates \
  --select F,W,E,ASYNC,C4,Q,I,UP \
  --ignore F403,F405,E501,ASYNC230,ASYNC240,UP017,UP041,UP042,UP047,ASYNC109 \
  --per-file-ignores "__init__.py:F401"
```

格式化检查：

```bash
ruff format --check . --line-length 88 --exclude plans,docs,templates
```

只格式化本次修改的 Python 文件；不要为通过检查而重排无关文件。

## 提交约定

- 按功能拆分提交，修复优先合入对应功能提交。
- 使用中文 Conventional Commit 描述：`feat(scope): ...`、`fix(scope): ...`、`chore: ...`。
- 提交前检查暂存范围与差异，不提交测试缓存、运行时数据或 `.ccg/`。
- 配置、接口或行为变化时同步相关文档与测试。

## v2 模块地图

`tl/core/` 是入口无关的调度核心：

- `models.py`、`requests.py`、`states.py`：Job、请求、结果与状态机。
- `job_store.py`、`job_schema.py`：SQLite 持久化、迁移与恢复。
- `scheduler.py`、`service.py`：提交、父子 Job、deadline、取消、投递与关闭。
- `router.py`、`dispatch.py`、`legacy_adapter.py`：Provider contract、旧客户端适配与补足张数。
- `reference_service.py`、`remote_fetch.py`、`path_policy.py`：参考图、临时句柄、下载与路径/网络安全。
- `protocols.py`、`leases.py`、`errors.py`：依赖协议、租约与错误边界。

`tl/v2_*.py` 是兼容适配层：

- `v2_bridge.py`：入口请求与旧结果格式转换。
- `v2_admission.py`：共享额度、FIFO、WebUI 并发与组合准入。
- `v2_tracking.py`：生命周期观察者、生成历史投影与运行时进度。
- `v2_tool_delivery.py`：LLM 工具单图/批量结果回灌。
- `v2_sdk_delivery.py`：公开 SDK 任务记录、等待、回调与取消适配。

架构状态和接入边界见[架构说明](architecture/README.md)，灰度与回滚见[迁移指南](architecture/migration.md)。
