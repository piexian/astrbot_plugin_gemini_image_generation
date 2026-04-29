# tl 模块接口说明

本文档记录 `tl/` 目录内各文件的职责、主要接口和调用边界，供维护者修改插件内部逻辑时快速定位。这里的接口不是对外稳定 API，除非特别说明，默认只服务于本插件内部。

## 总体调用链

```text
main.py
  ├─ ConfigLoader -> PluginConfig
  ├─ GeminiAPIClient -> tl/api/* Provider
  ├─ ImageGenerator -> GeminiAPIClient.generate_image()
  ├─ ImageHandler / AvatarHandler -> 收集参考图
  ├─ MessageSender -> 发送生成结果和 NapCat Stream 兜底
  ├─ VisionHandler / image_splitter / sticker_cutter -> 表情包切分
  ├─ RateLimiter -> 群限制与限流
  ├─ KeyManager -> provider_overrides 多 Key 轮换
  └─ GeminiImageGenerationTool -> AstrBot LLM Tool
```

## 稳定内部入口

| 入口 | 位置 | 作用 |
|------|------|------|
| `PluginConfig` | `plugin_config.py` | 运行时配置数据对象 |
| `ConfigLoader.load()` | `plugin_config.py` | 从 AstrBot 配置加载、迁移、归一化插件配置 |
| `GeminiAPIClient.generate_image()` | `tl_api.py` | 生图 API 请求统一入口 |
| `get_api_client()` / `clear_api_client()` | `tl_api.py` | API 客户端单例管理 |
| `ImageGenerator.generate_image_core()` | `image_generator.py` | 指令与 LLM Tool 共用的核心生图逻辑 |
| `GeminiImageGenerationTool.call()` | `llm_tools.py` | AstrBot FunctionTool 调用入口 |
| `execute_image_generation_tool()` | `llm_tools.py` | 兼容旧调用方式的工具入口 |
| `MessageSender.dispatch_send_results()` | `message_sender.py` | 图片、文本、合并转发发送结果构造入口 |
| `MessageSender.send_results_with_stream_retry()` | `message_sender.py` | 直接发送结果；原始发送失败后按阈值尝试 NapCat Stream API 兜底 |
| `ImageHandler.fetch_images_from_event()` | `image_handler.py` | 从消息事件收集参考图和头像图 |
| `AvatarHandler.get_avatar_reference()` | `avatar_handler.py` | 获取用户头像参考图 |
| `split_image()` | `image_splitter.py` | 表情包切图入口 |
| `create_zip()` | `image_splitter.py` | 切图结果打包 |
| `get_api_provider()` | `api/registry.py` | 根据 `api_type` 获取供应商实现 |

## 数据类型

### `api_types.py`

| 接口 | 类型 | 说明 |
|------|------|------|
| `ApiRequestConfig` | `dataclass` | Provider 请求配置，包含模型、提示词、API 类型、参考图、分辨率、比例、图片输入模式等字段 |
| `APIError` | `Exception` | API 层统一异常，携带 `status_code`、`error_type`、`error_code`、`retryable` |

典型 `ApiRequestConfig` 字段：

```python
ApiRequestConfig(
    model="...",
    prompt="...",
    api_type="openai",
    api_base=None,
    api_key=None,
    resolution="1K",
    aspect_ratio="1:1",
    reference_images=[],
    image_input_mode="force_base64",
)
```

### `api/base.py`

| 接口 | 类型 | 说明 |
|------|------|------|
| `ProviderRequest` | `dataclass(frozen=True)` | Provider 构建出的请求对象：`url`、`headers`、`payload` |
| `ApiProvider` | `Protocol` | 所有 provider 必须实现的协议 |

Provider 协议：

```python
async def build_request(
    self, *, client: Any, config: ApiRequestConfig
) -> ProviderRequest:
    ...

async def parse_response(
    self,
    *,
    client: Any,
    response_data: dict[str, Any],
    session: aiohttp.ClientSession,
    api_base: str | None = None,
    http_status: int | None = None,
) -> tuple[list[str], list[str], str | None, str | None]:
    ...
```

`parse_response()` 返回 `(image_urls, image_paths, text_content, thought_signature)`。

## 配置模块

### `plugin_config.py`

| 接口 | 说明 |
|------|------|
| `PluginConfig` | 插件运行时配置数据类，集中保存 API、图像生成、快速模式、服务、限流、缓存等配置 |
| `ConfigLoader(raw_config, data_dir=None)` | 配置加载器 |
| `ConfigLoader.load()` | 入口方法，返回 `PluginConfig` |
| `_validate_openai_images_settings(settings)` | 校验和归一化 `openai_images` 的 `size_mode/custom_size` |

`ConfigLoader` 还负责：

- v1.9.0 前旧配置迁移。
- 写入 `.migration_v1.9.0.done` 迁移标记。
- 生成 `config_backup_pre_v1.9.0_*.json` 备份。
- 加载 `provider_overrides`、`quick_mode_settings`、`rate_limit_rules`。

### `openai_image_size.py`

| 接口 | 说明 |
|------|------|
| `normalize_size_mode(value)` | 校验并归一化 `preset/custom` |
| `normalize_custom_size_input(value)` | 将 `2048 × 1152`、`2048×1152` 等归一化为 `2048x1152` |
| `validate_custom_size(value)` | 校验 OpenAI Images 自定义尺寸约束 |
| `derive_custom_size_from_preset_params(resolution, aspect_ratio)` | 从旧预设 `1K/2K/4K + 比例` 推导合法 `WxH` |
| `resolve_openai_custom_size(size_candidate, resolution_candidate, aspect_ratio_candidate, settings)` | 按显式 `size`、预设参数、配置 `custom_size` 的优先级解析最终尺寸 |

常量：

| 常量 | 说明 |
|------|------|
| `CUSTOM_SIZE_MAX_EDGE` | 最大边长，当前 `3840` |
| `CUSTOM_SIZE_MIN_PIXELS` / `CUSTOM_SIZE_MAX_PIXELS` | 总像素范围 |
| `PRESET_RESOLUTIONS` | `1K` / `2K` / `4K` |
| `PRESET_ASPECT_RATIOS` | 内部支持的比例枚举 |

## API 客户端与 Provider

### `tl_api.py`

| 接口 | 说明 |
|------|------|
| `GeminiAPIClient(api_keys)` | API 客户端，负责会话、代理、Key 轮换、请求发送、重试和响应解析 |
| `GeminiAPIClient.set_key_manager(key_manager)` | 注入 `KeyManager` |
| `GeminiAPIClient.get_key_for_api_type(api_type)` | 按供应商获取可用 Key |
| `GeminiAPIClient.generate_image(config)` | 生图请求统一入口 |
| `GeminiAPIClient.close()` | 关闭内部 `aiohttp.ClientSession` |
| `get_api_client(api_keys)` | 获取模块级 API 客户端单例 |
| `clear_api_client()` | 清理模块级 API 客户端单例 |

内部关键步骤：

```text
generate_image()
  -> _get_api_url()
  -> get_api_provider(api_type).build_request()
  -> _make_request()
  -> provider.parse_response() 或兼容解析方法
```

`tl_api.py` 同时保留部分兼容解析方法，例如 `_parse_gresponse()`、`_parse_openai_response()`、`_parse_doubao_response()`，新 provider 优先放在 `tl/api/` 中实现。

### `api/registry.py`

| 接口 | 说明 |
|------|------|
| `normalize_api_type(api_type)` | 小写、去空格、`-` 转 `_` |
| `get_api_provider(api_type)` | 返回对应 provider 单例（字典查表，未知值回退 `OpenAICompatProvider`） |

当前映射（与 `_conf_schema.json` 中 `api_settings.api_type.options` 严格一致，不再提供别名）：

| `api_type` | Provider |
|------------|----------|
| `google` | `GoogleProvider` |
| `openai` | `OpenAICompatProvider`（默认兑底） |
| `openai_images` | `OpenAIImagesProvider` |
| `xai` | `XAIProvider` |
| `minimax` | `MiniMaxProvider` |
| `stepfun` | `StepfunProvider` |
| `doubao` | `DoubaoProvider` |
| `zai` | `ZaiProvider` |
| `grok2api` | `Grok2ApiProvider` |

### `api/openai_compat.py`

OpenAI Chat Completions 兼容 provider，默认兜底 provider。

| 接口 | 说明 |
|------|------|
| `OpenAICompatProvider.build_request()` | 构造 `/v1/chat/completions` 请求 |
| `OpenAICompatProvider.parse_response()` | 解析 OpenAI 兼容响应 |
| `_prepare_payload()` | 子类可覆盖的请求体构建 hook |
| `_handle_special_candidate_url()` | 子类可覆盖的特殊图片 URL 处理 hook |
| `_find_additional_image_urls_in_text()` | 子类可覆盖的文本图片链接提取 hook |

### `api/google.py`

Google/Gemini 官方 provider。

| 接口 | 说明 |
|------|------|
| `GoogleProvider.build_request()` | 构造 Google Gemini 请求 |
| `GoogleProvider.parse_response()` | 解析 Gemini 响应 |
| `_prepare_payload()` | 组装 `contents`、参考图和 generation config |
| `_parse_gresponse()` | 从响应中提取图片、文本和 `thought_signature` |

### `api/openai_images.py`

OpenAI Images 原生端点 provider。

| 接口 | 说明 |
|------|------|
| `OpenAIImagesProvider.build_request()` | 根据是否有参考图选择 `generations` 或 `edits` |
| `OpenAIImagesProvider.parse_response()` | 解析 `data[].url` / `data[].b64_json` 与 usage |
| `_prepare_generations_payload()` | 文生图 JSON 请求体 |
| `_prepare_edits_payload()` | 改图 multipart 请求体 |
| `_is_gpt_image_model(model)` | 判断 GPT image 系列 |
| `_get_size_mapping(model)` | 按模型族选择预设尺寸映射 |
| `_resolve_size_value(model, resolution, settings)` | 计算最终 `size` |

### `api/xai.py`

xAI Images 官方 provider。

| 接口 | 说明 |
|------|------|
| `XAIProvider.build_request()` | 构造 `/v1/images/generations` 或 `/v1/images/edits` 请求 |
| `XAIProvider.parse_response()` | 解析 `url` / `b64_json` 图片响应 |
| `_prepare_generations_payload()` | 文生图请求体 |
| `_prepare_edits_payload()` | 改图请求体，参考图转 `data URI` |
| `_to_image_url()` | 统一把本地/URL/base64 输入转换为 xAI 可用图片引用 |

### `api/minimax.py`

MiniMax 图片生成官方 provider。

| 接口 | 说明 |
|------|------|
| `MiniMaxProvider.build_request()` | 构造 `/v1/image_generation` JSON 请求 |
| `MiniMaxProvider.parse_response()` | 解析 `data.image_urls` / `data.image_base64` 响应 |
| `_prepare_payload()` | 组装模型、比例、生成数量、水印、提示词优化和参考图；根据 `resolution` 和 `aspect_ratio` 自动选择 `aspect_ratio` 或显式 `width`/`height` |
| `_map_resolution()` | 全局 `resolution`（1K/2K/4K）映射为目标像素尺寸（4K 降级为 2048） |
| `_compute_dimensions_from_ratio()` | 从 W:H 比例和目标长边计算 `width`/`height`，自动对齐 8 的倍数并钳制 512-2048 |
| `_build_subject_reference()` | 构造 MiniMax `subject_reference` |
| `_to_image_file()` | 参考图按配置透传 URL 或转为 `data URI` |

### `api/doubao.py`

火山引擎 Ark / 豆包 Seedream provider。

| 接口 | 说明 |
|------|------|
| `DoubaoProvider.build_request()` | 构造 `/api/v3/images/generations` 请求 |
| `DoubaoProvider.parse_response()` | 解析 URL/base64 响应并处理错误码 |
| `_prepare_payload()` | 组装模型、尺寸、水印、组图、参考图等参数 |
| `_map_resolution()` | 将内部 `1K/2K/4K` 或具体尺寸映射为豆包尺寸 |
| `_process_reference_images()` | 处理图生图参考图 |
| `_build_api_error()` | 将豆包错误转为 `APIError` |

### `api/zai.py`

Zai provider，继承 `OpenAICompatProvider`。

| 接口 | 说明 |
|------|------|
| `ZaiProvider._prepare_payload()` | 调整 Zai 所需的 `image_size`、`aspect_ratio`、`generation_config` |

### `api/grok2api.py`

grok2api provider，继承 `OpenAICompatProvider`。

| 接口 | 说明 |
|------|------|
| `_find_additional_image_urls_in_text()` | 从 Markdown 中提取相对图片路径 |
| `_handle_special_candidate_url()` | 处理相对路径和临时缓存图片 |
| `_origin_from_api_base()` | 从 `api_base` 推导 origin |
| `_is_temp_cache_url()` | 判断是否为需立即下载的临时缓存 URL |

## 图像生成与 LLM Tool

### `image_generator.py`

| 接口 | 说明 |
|------|------|
| `ImageGenerator(...)` | 核心图像生成处理器 |
| `update_config(**kwargs)` | 热更新配置字段 |
| `generate_image_core(event, prompt, reference_images, avatar_reference, override_resolution=None, override_aspect_ratio=None, is_tool_call=False)` | 不直接发送消息，只返回成功状态和生成结果 |

返回格式：

```python
(True, (image_urls, image_paths, text_content, thought_signature))
(False, "错误消息")
```

`is_tool_call=True` 时使用 AstrBot 工具调用超时；普通指令使用插件 `total_timeout`。

### `llm_tools.py`

| 接口 | 说明 |
|------|------|
| `GeminiImageGenerationTool` | AstrBot FunctionTool 实现 |
| `GeminiImageGenerationTool.refresh_from_plugin()` | 根据当前插件配置刷新工具描述和参数 schema |
| `GeminiImageGenerationTool.call()` | LLM Tool 调用入口 |
| `execute_image_generation_tool()` | 兼容旧路径的执行入口 |
| `_build_call_tool_result()` | 将图片结果封装为 `CallToolResult`，包含 `ImageContent` / `TextContent` |
| `_background_generate_and_send()` | 后台生成完成后发送结果 |

OpenAI Images 自定义尺寸模式下，工具参数会切换为：

```text
prompt + use_reference_images + include_user_avatar + size + for_forum
```

其他模式下，工具参数为：

```text
prompt + use_reference_images + include_user_avatar + resolution + aspect_ratio + for_forum
```

### `thought_signature.py`

| 接口 | 说明 |
|------|------|
| `log_thought_signature_debug(label, thought_signature)` | 仅输出受限长度的调试信息，避免大字段污染日志或上下文 |

## 图片收集、头像和发送

### `image_handler.py`

| 接口 | 说明 |
|------|------|
| `ImageHandler(api_client=None, max_reference_images=6)` | 图片收集和过滤处理器 |
| `update_config(api_client=None, max_reference_images=None)` | 热更新配置 |
| `is_valid_base64_image_str(value)` | 判断字符串是否为有效图片 base64 |
| `filter_valid_reference_images(images, source)` | 过滤 URL/data URI/base64 形式的参考图 |
| `download_qq_image(url, event=None)` | QQ/nt.qq/qpic 图片特殊下载逻辑 |
| `fetch_images_from_event(event, include_at_avatars=False)` | 从当前消息、引用消息、合并转发、@ 头像中收集图片 |
| `clean_text_content(text)` | 清理消息文本中的图片标记 |

`fetch_images_from_event()` 返回：

```python
(message_images, avatar_images)
```

### `avatar_handler.py`

| 接口 | 说明 |
|------|------|
| `AvatarHandler(auto_avatar_reference=False)` | 头像参考图处理器 |
| `update_config(auto_avatar_reference)` | 热更新自动头像配置 |
| `get_avatar_reference(event)` | 获取发送者或 @ 用户头像，返回 base64/data URL 列表 |
| `should_use_avatar(event)` | 判断是否启用头像参考 |
| `should_use_avatar_for_prompt(event, prompt)` | 结合提示词和 @ 用户判断是否启用头像 |
| `prompt_contains_avatar_keywords(prompt)` | 判断提示词是否包含头像关键词 |
| `parse_mentions(event)` | 从消息组件解析 @ 用户 ID |

### `message_sender.py`

| 接口 | 说明 |
|------|------|
| `MessageSender(enable_text_response=False, max_inline_image_size_mb=2.0, napcat_stream_threshold_mb=2.0, show_duration_stats=True, show_retry_stats=True, show_token_usage_stats=True)` | 消息发送处理器 |
| `update_config(enable_text_response=None, max_inline_image_size_mb=None, napcat_stream_threshold_mb=None, show_duration_stats=None, show_retry_stats=None, show_token_usage_stats=None)` | 热更新发送配置 |
| `is_aioqhttp_event(event)` | 判断是否为 aiocqhttp 平台 |
| `safe_send(event, payload)` | 发送失败时兜底返回错误提示 |
| `send_api_duration(event, api_duration, send_duration=None, retry_count=0, retry_note=None, token_usage=None)` | 发送耗时、重试次数和可用 token 用量统计 |
| `clean_text_content(text)` | 移除 Markdown 图片等不可发送内容 |
| `strip_known_image_refs(text, image_refs)` | 从文本中移除已识别图片引用，避免重复发送 |
| `prepare_text_content(text, image_refs=None)` | 统一清理待发送文本 |
| `merge_available_images(image_urls, image_paths)` | URL 与本地路径合并去重，URL 优先 |
| `build_forward_image_component(image, force_base64=False)` | 构造 AstrBot 图片组件 |
| `dispatch_send_results(...)` | 按单图、多图、合并转发策略发送结果 |
| `send_results_with_stream_retry(...)` | 发送失败时筛选达到阈值的本地图片，调用 `upload_file_stream()` 后重试 |

## 表情包切分与视觉识别

### `image_splitter.py`

| 接口 | 说明 |
|------|------|
| `LegacySmartMemeSplitter` | 旧版边缘/网格检测器 |
| `SmartMemeSplitter` | 当前智能表情包网格检测器 |
| `AIMemeSplitter` | 根据指定行列进行视觉辅助切分 |
| `ai_split_with_rows_cols(image_path, rows, cols, output_dir, file_prefix, base_image)` | 使用行列数执行切分 |
| `resolve_split_source_to_path(source, image_input_mode="force_base64", api_client=None, download_qq_image_fn=None, logger_obj=logger)` | 将切图输入源解析为本地路径 |
| `split_image(image_path, rows=6, cols=4, output_dir=None, bboxes=None, manual_rows=None, manual_cols=None, use_sticker_cutter=False, ai_rows=None, ai_cols=None)` | 切图主入口 |
| `create_zip(files, output_filename=None)` | 打包切图文件 |

`split_image()` 优先级：

```text
bboxes -> manual_rows/manual_cols -> ai_rows/ai_cols -> SmartMemeSplitter -> StickerCutter fallback
```

### `sticker_cutter.py`

| 接口 | 说明 |
|------|------|
| `Region` | 检测区域数据类，包含 `box`、`area`、`center`、`is_main` |
| `StickerCutter` | 主体 + 附件吸附切分算法 |
| `StickerCutter.process_image(img, debug=False)` | 返回裁剪后的透明背景图片列表和调试图 |

`StickerCutter` 内部流程：

```text
_prepare_foreground()
  -> _find_all_regions()
  -> _classify_regions()
  -> _attach_regions()
  -> _suppress_overlapping()
  -> _convert_to_transparent()
```

### `vision_handler.py`

| 接口 | 说明 |
|------|------|
| `VisionHandler(context, api_client=None, vision_provider_id="", vision_model="", enable_llm_crop=True, sticker_bbox_rows=6, sticker_bbox_cols=4)` | 视觉识别处理器 |
| `update_config(...)` | 热更新视觉识别配置 |
| `extract_llm_text(resp)` | 从 AstrBot LLMResponse 中提取文本 |
| `inject_vision_system_prompt(event, req)` | 给视觉裁剪请求注入 system prompt |
| `llm_detect_and_split(image_path)` | 使用视觉 LLM 返回裁剪框后调用 `split_image()` |
| `detect_grid_rows_cols(image_path)` | 使用视觉模型识别网格行列 |

## 提示词和帮助页

### `enhanced_prompts.py`

| 接口 | 说明 |
|------|------|
| `enhance_prompt_for_gemini(prompt)` | 兼容保留，目前直接返回原提示词 |
| `get_avatar_prompt(prompt)` | 头像快速模式提示词 |
| `get_poster_prompt(prompt)` | 海报快速模式提示词 |
| `get_wallpaper_prompt(prompt)` | 壁纸快速模式提示词 |
| `get_card_prompt(prompt)` | 卡片快速模式提示词 |
| `get_mobile_prompt(prompt)` | 手机壁纸快速模式提示词 |
| `get_sticker_prompt(prompt="", rows=4, cols=4)` | 中文表情包提示词 |
| `get_q_version_sticker_prompt(prompt="", rows=4, cols=4)` | 英文 Q 版表情包提示词 |
| `get_figure_prompt(prompt, style_type=1)` | 手办化提示词，`1` 为 PVC，`2` 为树脂 GK |
| `enhance_prompt_for_figure(prompt)` | 兼容旧接口，等价于 `get_figure_prompt(prompt, 1)` |
| `get_generation_prompt(prompt)` | 普通生图提示词 |
| `get_modification_prompt(prompt)` | 改图提示词 |
| `get_auto_modification_prompt(prompt)` | 快捷模式自动改图提示词 |
| `get_style_change_prompt(style, prompt="")` | 风格转换提示词 |
| `get_sticker_bbox_prompt(rows=6, cols=4)` | 视觉裁剪框识别提示词 |
| `get_vision_crop_system_prompt()` | 视觉裁剪 system prompt |
| `get_grid_detect_prompt()` | 网格行列识别提示词 |
| `build_quick_prompt(prompt, skip_figure_enhance=False)` | 快捷模式统一提示词构建，返回 `(enhanced_prompt, is_modification_request)` |

### `help_renderer.py`

| 接口 | 说明 |
|------|------|
| `ensure_font_downloaded()` | local 渲染模式下确保中文字体可用 |
| `get_template_path(templates_dir, theme_settings, extension=".html")` | 按主题配置选择帮助页模板 |
| `render_text(template_data)` | 纯文本帮助页 |
| `render_local_pillow(templates_dir, theme_settings, template_data)` | 使用 Pillow 渲染帮助页图片，返回 bytes |

## 限流、Key 管理和工具函数

### `rate_limiter.py`

| 接口 | 说明 |
|------|------|
| `RateLimiter(config, get_kv=None, put_kv=None)` | 群限制与限流管理器 |
| `get_group_id_from_event(event)` | 从事件中解析群号 |
| `check_and_consume(event)` | 检查黑白名单和限流，并消费一次额度 |
| `reset()` | 清空限流桶 |

`check_and_consume()` 返回：

```python
(True, None)
(False, "提示消息")
```

### `key_manager.py`

| 接口 | 说明 |
|------|------|
| `KeyUsageRecord` | 单个 Key 的使用记录 |
| `ProviderKeyManager` | 单个 provider 的 Key 状态 |
| `KeyManager(config, get_kv=None, put_kv=None)` | 全局 API Key 轮换和每日限额管理 |
| `has_provider(api_type)` | 判断该 provider 是否有独立 Key 配置 |
| `get_available_key(api_type)` | 获取并预扣指定 provider 的可用 Key |
| `rotate_key(api_type)` | 轮换到下一个可用 Key |
| `get_key_status(api_type)` | 返回 Key 数量、每日限额、今日使用量等状态 |

### `tl_utils.py`

该文件是共享工具集合，接口较多，按职责分组如下。

| 分组 | 主要接口 | 说明 |
|------|----------|------|
| 路径 | `get_plugin_data_dir()`、`get_temp_dir()` | 获取插件数据目录和临时目录 |
| Base64 | `encode_file_to_base64()`、`save_base64_image()`、`is_valid_base64_image_str()` | 图片 base64 编码、保存和校验 |
| 图片保存 | `save_image_stream()`、`save_image_data()`、`cleanup_old_images()` | 下载流保存、二进制保存、缓存清理 |
| 图片规范化 | `coerce_supported_image_bytes()`、`coerce_supported_image()`、`normalize_image_input()` | 将输入图片统一为 provider 可用格式 |
| 图片源解析 | `collect_image_sources()`、`resolve_image_source_to_path()` | 从 AstrBot 事件或任意来源解析图片 |
| QQ 头像 | `download_qq_avatar()`、`AvatarManager`、`download_qq_avatar_legacy()` | 头像下载和缓存管理 |
| NapCat Stream | `upload_file_stream()` | 复用当前 NapCat/OneBot 连接上传本地文件并返回可发送路径 |
| 错误展示 | `format_error_message()` | 统一生图错误提示文案 |

注意事项：

- `normalize_image_input()` 是 API provider 处理参考图时的核心工具。
- `resolve_image_source_to_path()` 是 `/切图` 将 URL/base64/data URI 转成本地文件的核心工具。
- `AvatarManager` 管理头像缓存和清理，通常通过 `AvatarHandler` 或 `ImageHandler` 间接使用。
- `upload_file_stream()` 只作为发送失败后的兜底路径使用，常规图片发送仍由 `MessageSender.dispatch_send_results()` 构造。

## `tl/api/__init__.py` 与 `tl/__init__.py`

| 文件 | 作用 |
|------|------|
| `tl/__init__.py` | 包初始化文件，目前不暴露额外接口 |
| `tl/api/__init__.py` | 重新导出 `get_api_provider()`、`normalize_api_type()` |

## 修改建议

- 新增 API 供应商时，优先在 `tl/api/` 新增 provider，并在 `api/registry.py` 注册。
- 只调整请求参数结构时，优先继承 `OpenAICompatProvider` 并覆盖 `_prepare_payload()`。
- 新增配置项时，同步修改 `PluginConfig`、`ConfigLoader.load()`、`_conf_schema.json` 和文档。
- 修改 OpenAI Images 尺寸逻辑时，优先集中在 `openai_image_size.py`，避免 provider、LLM Tool、快速模式各自实现一套校验。
- 修改发送结果格式时，同时检查 `MessageSender.dispatch_send_results()` 和 `llm_tools._build_call_tool_result()`。
- 修改切图逻辑时，优先保持 `split_image()` 的优先级顺序稳定，避免影响 `/快速 表情包` 和 `/切图` 两条路径。
