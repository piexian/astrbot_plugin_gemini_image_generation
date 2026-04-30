# 逐文件深度优化分析

> 配套文档：[2026-04-30-code-optimization-analysis.md](./2026-04-30-code-optimization-analysis.md)（总览）
> 本文件：按文件给出**精确行号**、**问题分类**、**具体重构建议**

---

## 1. `tl/tl_api.py`（1637 行）— 单文件最复杂

### 1.1 顶层符号
| 符号 | 行号 | 行数 | 备注 |
|---|---:|---:|---|
| `class GeminiAPIClient` | 87–1851 | ~1765 | 巨型类，54+ 方法 |
| `def get_api_client()` | 1853 | 5 | 单例工厂 |
| `def clear_api_client()` | 1861 | 4 | 全局清理 |
| `APIClient` 别名 | 1851 | 1 | 兼容别名 |

### 1.2 超长函数（>100 行）
| 方法 | 行号 | 行数 | 主要问题 |
|---|---:|---:|---|
| `_make_request` | 494–751 | **258** | 嵌套 retry 循环 + Key 轮换 + timeout 计算；3 层闭包 `coerce_int` / `rotate_key_if_possible` / `is_retryable` |
| `_parse_openai_response` | 1151–1420 | **270** | 8 层嵌套 if/else；4 条 fallback 路径（structured → data URI → text → loose regex） |
| `_perform_request` | 829–985 | **157** | provider 分支硬编码；JSON/SSE 解析混在同一处 |
| `_download_image` | 1669–1851 | **183** | HTTP 重试 + 错误诊断 + 参数校验混杂 |

### 1.3 具体优化点（按位置）

#### O1.1 Provider 响应分发硬编码 — L910–950
```python
if api_type == "google": return await self._parse_gresponse(...)
else:
    if normalize_api_type(api_type) == "doubao": return await self._parse_doubao_response(...)
    if normalize_api_type(api_type) in {"openai_images","xai","minimax","stepfun"}:
        provider = get_api_provider(api_type); return await provider.parse_response(...)
    return await self._parse_openai_response(...)
```
**建议**：所有 provider 均通过 `BaseProvider.parse_response()` 走 registry，主类只做 `provider = get_api_provider(api_type); await provider.parse_response(...)`。新增 provider 只改 registry。

#### O1.2 Base64 校验三层 fallback — L207–257
`_validate_and_normalize_b64` 的 padding 计算 + urlsafe + 宽松过滤逻辑应抽到独立模块 `tl/base64_normalizer.py`，提供 `Base64Normalizer.validate(data, context) → str`。

#### O1.3 图像 URL 提取重复 — L1598–1623、L1625–1661、L1245–1320
正则模式（markdown / raw http / data URI）在三处分别定义。
**建议**：`tl/image_url_extractor.py` 统一 `extract_all(text) → (urls, data_uris)`，预编译正则为类常量。

#### O1.4 重试策略硬编码 — L730 / L1687 / L1720
- `delay = min(2 ** (attempt + 2), 10)` 退避参数硬编码
- 下载超时 `30` 秒、`max_retries=1` 直接写死
**建议**：抽 `RetryStrategy` + `tl/constants.py`：
```python
@dataclass(frozen=True)
class ExponentialBackoff:
    base: int = 2; offset: int = 2; cap: float = 10.0
    def delay(self, attempt: int) -> float: return min(self.base ** (attempt + self.offset), self.cap)
```

#### O1.5 函数参数过多
| 函数 | 参数数 |
|---|---:|
| `_make_request` | 9 |
| `_perform_request` | 9 |
| `generate_image` | 5（含 3 个 timeout） |

**建议**：合并 timeout 三个参数为 `TimeoutStrategy(total, per_retry, max_total)`；引入 `RequestContext(config, timeout, retry, session)`。

#### O1.6 异常处理不一致
| 位置 | 模式 |
|---|---|
| L630 | 多层 try/except + raise 新 APIError，丢失链 |
| L1374 | try/except 后 debug log，降级路径不清晰 |
| L1817 | `except Exception: pass`（吞异常） |

**建议**：定义策略——核心路径必须 raise；降级路径 debug log + return None；禁止裸 `pass`。

#### O1.7 嵌套深度 — L1185–L1370 5 层
`_parse_openai_response` 有 5 层嵌套（message → content list/str → image_url dict）。
**建议**：用 dispatcher dict + 每种 part_type 独立函数，扁平化逻辑。

### 1.4 拆分目标
```
tl/tl_api.py            (~500 行) - 仅 GeminiAPIClient 主类 + dispatch
tl/api_response_parser.py (~200) - openai/google/doubao 解析合并
tl/image_downloader.py    (~200) - download / cache / mime / retry
tl/api_retry.py           (~150) - RetryStrategy / ExponentialBackoff
tl/base64_normalizer.py   (~80)
tl/image_url_extractor.py (~150)
tl/sse_parser.py          (~60)
```

---

## 2. `main.py`（1502 行）

### 2.1 方法清单（重点）
| 方法 | 行号 | 行数 | 备注 |
|---|---:|---:|---|
| `__init__` | 69–100 | 32 | 模块编排 |
| `_init_modules` | 113–187 | 75 | 7 个模块 new |
| `_update_modules_api_client` | 189–210 | 22 | 5 模块同步更新 |
| `_load_provider_from_context` | 335–527 | **192** | 🔴 配置/绑定/重试全揉 |
| `_quick_generate_image` | 558–678 | 121 | 🔴 限流→引用→提示→生成→发送 |
| `_handle_quick_mode` | 729–779 | 51 | |
| `quick_avatar/poster/wallpaper/card/mobile/figure` | 805–881 | ~76 | 🔴 6 处结构相同 |
| `quick_sticker` | 883–1154 | **271** | 🔴 最长方法 |
| `split_image_command` | 1156–1296 | 141 | |
| `show_help` | 1298–1411 | 113 | 3 种渲染模式 |
| `modify_image` | 1413–1432 | 20 | |
| `change_style` | 1435–1502 | 68 | |

### 2.2 具体优化点

#### O2.1 Provider 设置双重绑定 — L432–440 + L452–480
```python
# L432-440：第一层（写到 cfg）
self.cfg.doubao_settings = override_settings
self.cfg.minimax_settings = override_settings
... (5 次)
# L452-480：第二层（cfg → api_client）
self.api_client.doubao_settings = getattr(self.cfg, "doubao_settings", None)
self.api_client.minimax_settings = ...
... (5 次重复，含 try/except + debug log)
```
**建议**：dict 驱动一次到位
```python
PROVIDER_SETTINGS_ATTR_MAP = {
    "doubao": "doubao_settings", "minimax": "minimax_settings",
    "stepfun": "stepfun_settings", "xai": "xai_settings",
    "openai_images": "openai_images_settings",
}
for api_key, attr in PROVIDER_SETTINGS_ATTR_MAP.items():
    if api_type_norm == api_key:
        setattr(self.cfg, attr, override_settings)
        setattr(self.api_client, attr, override_settings)
```
**收益**：-40~60 行；新增 provider 只改一个 dict。

#### O2.2 快速模式 6 处子命令重复 — L805–881
6 个 `quick_*` 命令仅参数不同，结构 90% 相同。
**建议**：参数表 + 工厂
```python
QUICK_MODES = {
    "avatar":  QuickModeSpec(prompt_fn=get_avatar_prompt,  needs_avatar=True,  ...),
    "poster":  QuickModeSpec(prompt_fn=get_poster_prompt,  ...),
    ...
}
async def _dispatch_quick_mode(self, event, mode: str, text: str): ...
```
减少 ~150 行。

#### O2.3 `quick_sticker` 271 行 — L883–1154
**职责**：参数解析 / 限流 / 生图 / 下载 / AI 网格识别 / 切割 / 打包 / 转发
**建议**：拆 `tl/sticker_workflow.py`
- `prepare_request()` / `generate_base_image()` / `recognize_grid()` / `split_and_pack()` / `send_results()`

#### O2.4 `_load_provider_from_context` 192 行 — L335–527
**建议**：拆为
- `_resolve_provider_config(context) → ProviderConfig`（从 AstrBot Provider 读出）
- `_apply_to_cfg(provider_cfg)`（绑定到 self.cfg）
- `_apply_to_api_client(provider_cfg)`（绑定到 client）
- `_log_provider_status()`
迁到 `tl/provider_resolver.py`。

#### O2.5 `_init_modules` 硬编码 7 模块 — L113–187
**建议**：声明式注册表
```python
self._services: dict[str, Any] = {}
for spec in MODULE_SPECS:
    self._services[spec.name] = spec.factory(self.cfg, ...)
```

#### O2.6 业务流程 stats 重复 — L1413–1502
`modify_image` / `change_style` / `_quick_generate_image` 末尾都有 `生成耗时 + 发送结果` 重复模式。
**建议**：`async def _generate_and_send_with_stats(prompt_builder, *, use_avatar, scene): ...`

#### O2.7 临时文件未清理 — `quick_sticker` 内部 `split_files`（L980+）
切图产物只在异常时清理，正常路径 leak 到清理任务。
**建议**：`async with TempFileTracker() as tmp: ...` 上下文管理器。

#### O2.8 占位空方法 — L221–223
`_apply_openai_custom_size_runtime_defaults` 是空 pass，直接删除。

---

## 3. `tl/tl_utils.py`（1119 行）

### 3.1 顶层符号（按职责分组）
| 类别 | 函数/类 | 行号 |
|---|---|---|
| HTTP 工具 | `_is_qq_host`, `_build_http_headers` | 35, 43 |
| 缓存 | `_LRUCache`, `_check_image_cache`, `_save_to_cache` | 289, 76, 104 |
| Base64 | `_decode_base64_to_temp_file`, `is_valid_base64_image_str`, `_encode_file_to_base64`, `encode_file_to_base64`, `save_base64_image` | 131, 718, 228, 247, 318 |
| 目录 | `get_plugin_data_dir`, `get_temp_dir`, `_build_image_path` | 170, 178, 189 |
| 图像 IO | `save_image_stream`, `save_image_data`, `coerce_supported_image_bytes`, `coerce_supported_image`, `normalize_image_input` | 252, 375, 830, 874, 886 |
| 下载 | `download_qq_avatar`, `download_qq_avatar_legacy`, `_pick_avatar_url` | 549, 1188, 202 |
| 解析 | `collect_image_sources`, `resolve_image_source_to_path` | 762, 1022 |
| 清理 | `cleanup_old_images` | 399 |
| 头像 | `AvatarManager` | 1146 |
| 错误 | `format_error_message` | 1203 |

### 3.2 重点优化点

#### O3.1 `cleanup_old_images` 150 行 — L399–549
对 4 个目录 `images / download_cache / split_output / temp` 执行**完全相同**的过滤+删除流程。
**建议**：
```python
@dataclass
class CleanupRule:
    dir_path: Path; max_age_seconds: int; max_count: int; logger_tag: str

def _cleanup_dir(rule: CleanupRule) -> int: ...
async def cleanup_old_images(...): for rule in rules: _cleanup_dir(rule)
```
预计 -90 行。

#### O3.2 `download_qq_avatar` 169 行 — L549–718
混合 NapCat API 轮询 + HTTP 回退 + 多种错误处理。
**建议**：策略链
```python
class AvatarDownloadStrategy(ABC): async def fetch(self, qq) -> bytes | None: ...
class NapCatStrategy(AvatarDownloadStrategy): ...
class HTTPStrategy(AvatarDownloadStrategy): ...
strategies = [NapCatStrategy(client), HTTPStrategy()]
for s in strategies:
    if data := await s.fetch(qq): return data
```

#### O3.3 `normalize_image_input` 136 行 — L886–1022
7 种输入分支：data URI / file:// / http(s) / base64 / 本地路径 / bytes / PIL Image。
**建议**：dispatcher dict
```python
HANDLERS = {"data_uri": _handle_data_uri, "http": _handle_http, "file": _handle_file, ...}
async def normalize_image_input(src): kind = _classify(src); return await HANDLERS[kind](src)
```

#### O3.4 `resolve_image_source_to_path` 7 参 — L1022
**建议**：参数对象
```python
@dataclass
class ImageSourceResolveContext:
    source: str
    image_input_mode: str = "force_base64"
    api_client: APIClient | None = None
    download_qq_image_fn: Callable | None = None
    event: Any | None = None
```

#### O3.5 缓存 key 重算 — L76 与 L104
两个函数都 `hashlib.sha256(url.encode()).hexdigest()`。
**建议**：`ImageCacheManager.key(url)` 单次计算 + 内部缓存。

#### O3.6 `download_qq_avatar_legacy` `asyncio.run` 反模式 — L1188
在已运行事件循环中调用 `asyncio.run` 会抛异常。
**建议**：删除该 legacy；调用方改为直接 await。

### 3.3 拆分目标
```
tl/avatar.py            - AvatarManager + 头像下载策略
tl/base64_utils.py      - 编解码 + 校验
tl/image_cache.py       - LRU + 文件缓存
tl/image_downloader.py  - normalize_image_input + resolve_image_source_to_path
tl/image_cleaner.py     - cleanup_old_images
tl/format_error.py      - format_error_message
tl/tl_utils.py          - 仅保留 ~50 行 re-export 兼容入口
```

---

## 4. `tl/api/doubao.py` (618) vs `tl/api/minimax.py` (618)

### 4.1 结构对照
| 功能 | doubao 行号 | minimax 行号 |
|---|---|---|
| 构建请求入口 | 133–192 | 64–100 |
| Payload 准备 | 194–281 | 144–229 |
| 响应解析入口 | 498–637 | 101–142 |
| resolution/int 强制转换 | 282–342 | 510–521, 594–603 |
| 引用图处理 | 379–497 | 317–384 |
| Base64/data URI | 343–378 | 301–316 |
| 错误处理 | 654–718 | ~605+ |

### 4.2 应抽到 `tl/api/base.py` 的方法
```python
class BaseImageProvider:
    # 工具
    @staticmethod
    def coerce_int(v, default=0) -> int: ...
    @staticmethod
    def coerce_optional_int(v) -> int | None: ...
    @staticmethod
    def normalize_string(v) -> str | None: ...
    @staticmethod
    def strip_data_uri_prefix(v: str) -> str: ...
    @staticmethod
    def format_base64_data_uri(b64: str, mime: str | None) -> str: ...

    # HTTP 通用
    async def normalize_api_base(self, value: str | None) -> str: ...
    def build_headers(self, api_key: str) -> dict[str, str]: ...

    # Pipeline
    async def process_reference_images(self, client, config, image_inputs) -> dict | None: ...
    async def save_base64_images(self, image_data, session) -> tuple[list[str], list[str]]: ...
    async def collect_image_urls_and_paths(self, response_data, session): ...

    # 错误
    def classify_error(self, code: str) -> Literal["retry","client","auth","quota"]: ...
    def build_api_error(self, code, msg, http_status, retryable) -> APIError: ...
```

### 4.3 子类保留差异
- **doubao**：4.5 vs 4.0 size 映射、watermark、optimize_prompt、sequential_image_generation、endpoint id 动态、~120 行错误码映射表
- **minimax**：aspect_ratio 标准化与重试、image-01-live 特殊路径、subject_reference 类型系统、安全尺寸校验、~40 行错误码映射

### 4.4 缩减预估
| 文件 | 当前 | 抽走 | 保留 | 缩减 |
|---|---:|---:|---:|---:|
| `doubao.py` | 618 | ~150 | 468 | 24% |
| `minimax.py` | 618 | ~120 | 498 | 19% |
| 其他 7 个 provider | ~2200 | ~300 | ~1900 | 14% |
| **小计** | ~3400 | ~570 | ~2830 | **~17%** |

### 4.5 三阶段策略
1. **阶段 1（低风险）**：纯工具函数（coerce_*、strip/format data URI、normalize_api_base）
2. **阶段 2**：通用 Pipeline（默认 `build_request` / `parse_response`）
3. **阶段 3**：子类只保留 `_prepare_payload` + `_parse_response_specific`

---

## 5. 其余文件（按文件给出 1–3 条具体建议）

### 5.1 `tl/plugin_config.py`（626）
- **L31–35** OpenAI Images size 校验与 `tl/llm_tools.py` L71–73、`tl/openai_image_size.py` 重复 → 集中到 `openai_image_size.py`
- **L183–189** 配置迁移文件 IO 缺重试，可忽略或包 try
- **40+ 字段 PluginConfig** → 拆 `ApiSettings` / `ImageGenerationSettings` / `LimitSettings` / `CacheSettings` 子 dataclass
- `_migrate_config` ~200 行 → 改造为 `MIGRATIONS: list[Callable]` 顺序 apply

### 5.2 `tl/llm_tools.py`（999）
- **L589–628** `_build_tool_parameters` / `_build_tool_description` 用条件分支生成 schema → 改配置模板 dict
- **L834–893** 前台等待+后台转移 9 层缩进 → 抽 `_handle_foreground_timeout()`
- 工具 schema 与 handler 应分离 → `tl/llm_tool_schemas.py`
- **L755–767** 超时计算与 `tl/image_generator.py` 重复

### 5.3 `tl/message_sender.py`（628）
- **L253–315** 5 层 if-elif 发送模式判断 → 策略类 `SendStrategy`
- **L110–130** Stream fallback 路径与 `image_handler.py` 文件检查重复
- **L369–422** base64/URL → component 转换重复 → `_resolve_image_to_component()`

### 5.4 `tl/image_handler.py`（438）
- **L153–220** `download_qq_image()` 三层重试嵌套 → `_HttpDownloadStrategy` 策略链
- **L113–130 / L355–370 / L412–440** 内部嵌套 async 函数 → 提为类私有方法
- **L232–270** `_is_auto_at()` 多属性兼容 → 装饰器或 `getattr_first(obj, names)`

### 5.5 `tl/image_generator.py`（285）
- **L67–91** 15 参初始化 → `dataclass` 字段工厂
- **L113–130** 参考图过滤与 `image_handler.py` 重复 → 抽 `_filter_valid_references()`
- **L178–210** 错误类型判断长 if 链 → `match` 或 `ErrorClassifier`

### 5.6 `tl/image_splitter.py`（1069）
- **L200–350** `analyze_grid_variations` 双层循环 O(n²) → 早期剪枝 + DP 缓存
- **L430–520** `refine_grid_candidate` 4 层嵌套 → 预计算投影数据，循环 -60%
- **L800–900** 手动网格 vs 自动检测的切割点微调 → 抽 `_optimize_cuts_by_energy()`
- 与 `sticker_cutter.py` 公共形态学操作 → `tl/image_grid.py`

### 5.7 `tl/sticker_cutter.py`（401）
- **L47–95** 4 种前景提取方式合并 → `cv2.findContours` 参数优化
- **L167–220** 区域特征反复计算 → `_compute_region_features()` 缓存
- **L300–350** NMS IoU 重叠重算 → `IntersectionComputer` 复用

### 5.8 `tl/enhanced_prompts.py`（190）
- **8 处** `if prompt: return base + user_prompt else return base` → `@combine_with_user_prompt` 装饰器
- **L86–115** PVC vs 树脂双风格硬编码 → 资源/常量字典
- **L32–50** 质量描述常量化 → `QUALITY_PROMPTS = {"sticker": ..., "figure": ...}`

### 5.9 `tl/help_renderer.py`（317）
- **L30–75** 字体查找散落 3 函数 → `FontManager`
- **L240–280** Pillow 排版与 HTML 模板独立 → 共享排版引擎
- **L156–180** 模板 cycle/single 判断与 `plugin_config.py` 模式判断相似

### 5.10 `tl/vision_handler.py`（303）
- **L145–180** JSON 解析 + 坐标还原 → `_parse_json_bbox_response()` / `_rescale_boxes_to_original()`
- **L102–140** 压缩副本生命周期 → `@asynccontextmanager` 包裹
- **L80–100** system_prompt + 调用参数 → `VisionRequest` dataclass

### 5.11 `tl/avatar_handler.py`（181）
- **L95–130** 并发下载异常 4 层嵌套 → `_gather_with_timeout()`
- **L43–65** 关键词匹配纯字符串 → 预编译正则
- **L138–155** 头像参考决策树 → 策略模式或配置驱动

### 5.12 `tl/key_manager.py`（240）
- **L87–135** 日期重置字符串比较 → `datetime.date` 缓存
- **L152–185** key 选择 while 循环可能死循环 → 显式 break + 计数器
- KV 序列化重复 → 与 `rate_limiter.py` 抽 `tl/kv_store.py`

### 5.13 `tl/rate_limiter.py`（175）
- **L81–110** 规则线性查询 → 哈希预索引
- **L125–155** 滑动窗口 list → `collections.deque`
- KV 部分与 `key_manager.py` 合并

### 5.14 `tl/openai_image_size.py`（223）
- **L52–90** 校验逻辑分散 → `@dataclass(frozen=True) class Size`
- **L106–160** 12 种常见组合预计算 + LRU
- **L165–223** while 缩放迭代 → 直接公式

### 5.15 `tl/napcat_stream.py`（145）
- **L45–80** chunk + sha256 → `FileStreamProcessor`
- **L110–145** 多字段名响应提取 → `extract_first(d, names)`
- **L65–75** `hasattr/getattr` 能力检查缓存

### 5.16 `tl/thought_signature.py`（24）
- **L15–23** 调用前加 `if logger.isEnabledFor(logging.DEBUG)` 守卫
- 文件过小，可与其他 debug 工具合并

### 5.17 `tl/__init__.py`（21）
- 添加 `__all__`、`__version__`
- 维持 re-export 以保证拆分后向后兼容

### 5.18 `tl/api_types.py`（53）
- **L8–32** `ApiRequestConfig` 17 字段 → 拆 core / optional 两个 dataclass
- **L38–53** 异常 `__init__` 参数多 → `@dataclass` 简化
- 字段加 `field(validator=...)` 校验

---

## 6. 跨文件重复矩阵（高复用价值）

| 重复主题 | 涉及文件 | 建议落地 |
|---|---|---|
| QQ 图片 / 头像下载 | tl_utils, image_handler, avatar_handler | `tl/avatar.py` + `tl/image_downloader.py` |
| Base64 校验 | tl_api, tl_utils, image_handler | `tl/base64_normalizer.py` |
| 图像格式转换 (PNG/JPEG/WebP) | tl_utils, vision_handler, image_handler | `tl/image_converter.py` |
| URL 提取（markdown/raw/data URI） | tl_api（3 处） | `tl/image_url_extractor.py` |
| 重试 + 退避 | tl_api, image_handler, llm_tools | `tl/api_retry.py` |
| 参考图处理 | google/openai_compat/openai_images/doubao/minimax | `tl/api/reference_image_builder.py` |
| OpenAI size 校验 | plugin_config, llm_tools, openai_image_size | 集中到 `openai_image_size.py` |
| KV 持久化 | key_manager, rate_limiter | `tl/kv_store.py` |
| 群 ID 提取 | rate_limiter, message_sender, main | `tl/event_utils.py` |
| 形态学操作 | image_splitter, sticker_cutter | `tl/image_grid.py` |

---

## 7. 推荐落地顺序（按 ROI + 风险）

| 阶段 | 任务 | 工时 | 风险 |
|---|---|---:|---|
| **P0 quick wins** | O2.1 provider 双重绑定 dict 化 | 1h | 低 |
| | O2.8 删除空 `_apply_openai_custom_size_runtime_defaults` | 5min | 极低 |
| | L3 thought_signature 加 DEBUG 守卫 | 10min | 低 |
| | O1.4 抽 `tl/constants.py` 集中魔法数 | 1h | 低 |
| **P1 抽公共工具** | `tl/base64_normalizer.py` | 2h | 低 |
| | `tl/image_url_extractor.py` | 2h | 低 |
| | `tl/api_retry.py` (RetryStrategy + ExponentialBackoff) | 3h | 低 |
| | `tl/kv_store.py`（合并 key_manager + rate_limiter） | 3h | 中 |
| **P2 拆分大文件** | `tl_utils.py` → 5 个新模块（保留 re-export） | 6h | 中 |
| | `tl_api.py` → 主类 + parser + downloader | 8h | 中 |
| | `main.py` → provider_resolver + sticker_workflow + command_router | 8h | 中 |
| **P3 Provider 抽象** | `BaseImageProvider` 阶段 1（工具方法） | 2h | 低 |
| | 阶段 2（Pipeline 默认实现） | 4h | 中 |
| | 阶段 3（子类只留差异） | 6h | 中 |
| **P4 算法优化** | image_splitter 早期剪枝 + DP | 4h | 中 |
| | sticker_cutter 区域特征缓存 | 2h | 低 |
| **P5 配置/类型** | plugin_config 拆子 dataclass + MIGRATIONS list | 4h | 中 |
| | api_types core/optional 拆分 | 1h | 低 |
| | 类型注解 + ruff/mypy CI | 3h | 低 |

总工时估算：**60–80 小时**（不含测试）；建议每个 PR 单独落地一项，配套冒烟测试（生图/改图/头像参考/限流/切图）。

---

## 8. 风险与边界

1. **向后兼容**：`tl/__init__.py` 与 `tl_utils.py` 必须保留所有现有导出符号；新模块只做 re-export 入口
2. **配置迁移**：`_migrate_config` 历史分支须保留并补 unit test
3. **Provider 注册**：抽 BaseImageProvider 时确保 registry 行为不变（所有现有 api_type 仍可解析）
4. **AstrBot API 升级**：集中化的 `provider_resolver` 反而有利于以后适配
5. **算法优化**：image_splitter / sticker_cutter 的剪枝改动需对比改动前后的切图准确度（建议保留对比测试用例集）
