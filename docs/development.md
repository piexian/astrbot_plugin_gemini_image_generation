# 开发指南

## 环境

- Python 3.12+，与 CI 基线一致。
- Node.js 24 + Vite+（`vp`），用于 Studio 前端 Vue 工程检查、构建与 i18n 测试。
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


## 分支门禁

`master` 已开启分支保护：合并进 `master` 的 PR 必须先通过 `Code Quality` 的五个检查——`Pytest (Python 3.12)`、`Frontend (Vue & Vite+)`、`Lint & Format`、`Syntax Check`、`Metadata Check`。未要求分支与 `master` 保持同步，也未限制管理员，直接 push `master` 仍然允许，检查只作为合并前置条件。CodeQL 的 `Analyze (actions|javascript-typescript|python)` 由代码扫描默认配置产生，不列为必过项，避免该配置关闭后检查永远停在待运行状态。

工作流每次推送都会触发，先由 `Detect changes` 任务按改动文件判定要跑哪些检查：

- 纯文档改动（`docs/**`、`*.md`、`LICENSE`、`plans/**`）：五个检查全部以跳过状态通过，等同成功。
- Python、`_conf_schema.json`、`templates/**` 或本工作流自身改动：运行 `Pytest`、`Lint & Format`、`Syntax Check`。
- `frontend/**`、`views/**`、`.astrbot-plugin/**` 改动：运行 `Frontend (Vue & Vite+)`；前端产物与 i18n 同时触发 pytest，因为 i18n 守门测试读取这些文件。
- `metadata.yaml`、`_conf_schema.json` 改动：运行 `Metadata Check`。
- `workflow_dispatch` 手动触发：全部运行。

不要用工作流级 `paths:` 过滤来省时间：被路径过滤掉的工作流不产生检查运行，必过检查会一直停在待报告状态并卡死合并；用 `if:` 跳过的任务报成功，不影响合并。同一分支的重复推送由 `concurrency` 取消更早的运行。

代码扫描由仓库内的 `.github/workflows/codeql.yml` 执行（Python、JavaScript/TypeScript、Actions，`codeql-action@v4`），不再使用 GitHub 的默认配置：默认配置无法按路径忽略，纯文档提交也会跑三个 `Analyze` 检查。该工作流的检查不列为必过项，所以用 `paths-ignore` 过滤文档改动不会卡住合并；每周一 03:23 UTC 定时深扫一次，覆盖此前被过滤掉的提交。

## 前端工程

- Studio 前端源码在 `frontend/`（vp + pnpm + Vue 3 + TS）；构建产物提交到 `views/studio/`，由宿主按目录通配伺服，i18n 走桥接上下文。
- 依赖安装、检查、单测与构建：`cd frontend && vp i`、`vp check`（格式化为 `vp fmt`）、`vp lint --deny-warnings`、`pnpm run type-check`、`vp test run`、`vp build`。
- 改动 `frontend/` 源码后必须执行 `vp fmt` 与 `vp build` 并一并提交 `views/studio/`；CI 使用 `voidzero-dev/setup-vp` 执行上述全套校验并比对 `views/studio/` 无差异与无未跟踪产物。
- 同步到实例时排除 `frontend/`，实例只需要 `views/studio/` 构建产物。
- 迁移状态（已完成）：六个视图（工作台/任务进度/画廊/限流/供应商/提示词广场）全部为 Vue SFC，位于 `frontend/src/{workbench,progress,gallery,limits,provider-config,templates}`；`frontend/src/app.js` 仅保留基建（BridgeClient/Store/SSE/SafeDOM/ImageLoader/Lightbox/Toast/Clipboard 等）与 StudioApp 壳（页签路由、refill 编排、上下文同步），经 `frontend/src/main.js` 挂载各视图工厂。
- 静态壳文案（页签/模态框/灯箱框架）仍用 `index.html` 的 `data-i18n-*` + `I18n.applyDom()` 运行时翻译；视图内文案一律 `t()`。语言切换整页 reload 机制不变。

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

架构状态和接入边界见[架构说明](architecture/README.md)，升级后的行为差异与数据兼容见[升级与兼容说明](architecture/migration.md)。
