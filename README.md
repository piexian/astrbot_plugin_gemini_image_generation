# AstrBot Gemini 图像生成插件

<div align="center">

![Version](https://img.shields.io/badge/Version-v1.9.13-blue)
![License](https://img.shields.io/badge/License-AGPL--3.0-orange)

**🎨 强大的 Gemini 图像生成插件，支持智能头像参考和智能表情包切分**

</div>

> ⚠️ **升级警告**：v1.9.0 以后的配置文件格式不兼容旧版本（v1.8.x 及更早）。升级后如遇配置模板显示错误，请查看 [CHANGELOG.md](./CHANGELOG.md#配置迁移说明) 了解迁移方案。

## ✨ 特性

### 🖼️ 核心功能
- **多模式生图**: 纯文本生成、参考图修改、风格转换
- **快速预设**: 头像/海报/壁纸/卡片/手机/手办化/表情包一键生成
- **智能头像**: 自动获取用户头像和@对象头像作为参考
- **表情包切分**: SmartMemeSplitter v4 算法自动切割表情包网格
- **LLM 工具**: 支持自然语言触发生图，前台返回 `CallToolResult` 结构化图片，超时自动转后台
- **安全回传**: 自动过滤 Gemini `thought_signature`，避免超大签名误注入工具结果导致上下文膨胀
- **多 API 支持**: Google 官方、OpenAI 兼容、OpenAI Images、xAI Images、Zai、grok2api、豆包（Doubao）
- **多格式支持**: PNG、JPEG、WEBP、HEIC/HEIF、GIF

### 🛡️ 限制/限流
- **群限制模式**: 不限制/白名单/黑名单
- **群内限流**: 单群周期内请求次数限制
- **持久化存储**: 限流数据使用 KV 存储，重启不丢失（需 AstrBot >= 4.9.2）

### 🧠 智能特性
- **自然语言触发**: "按照我"、"修改"、"@人"等触发头像获取
- **分辨率控制**: 1K/2K/4K，支持多种长宽比
- **改图尺寸保持**: 可选沿用参考图原始尺寸
- **Google 搜索接地**: 实时数据参考生成（仅 Gemini 模型）
- **智能重试**: 自动重试和密钥轮换
- **主题切换**: 帮助页白天/黑夜主题自动切换
- **定时清理**: 自动清理过期临时文件

## 📦 安装

### 前置要求
- AstrBot 4.10+
- Python 3.10+
- NapCat（目前仅适配 NapCat 平台）

### 依赖库

自动安装（见 [requirements.txt](requirements.txt)）

### 安装方式

**方式一：插件市场**
搜索 `Gemini 图像生成` 并安装

**方式二：链接安装**
 在插件界面右下角点击加号选择从链接安装输入 ` https://github.com/piexian/astrbot_plugin_gemini_image_generation  `
 
## 🔧 配置

### 必填配置

| 配置项 | 说明 |
|--------|------|
| `api_settings.provider_id` | 生图模型提供商（从 AstrBot 提供商列表选择；doubao 无需填写） |
| `api_settings.api_type` | API 类型：`google`/`openai`/`openai_images`/`xai`/`zai`/`grok2api`/`doubao` |

### 配置项详解

**api_settings**
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `provider_id` | - | 必填，生图模型提供商 |
| `api_type` | `openai` | API 类型 |
| `model` | - | 可选，覆盖提供商模型名称 |
| `proxy` | - | 全局代理地址，支持 `http://`、`https://`、`socks5://` 格式；留空则读取环境变量 |
| `vision_provider_id` | - | 可选，切图前 AI 识别网格行列 |

**image_generation_settings**
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `resolution` | `1K` | 分辨率：1K/2K/4K |
| `aspect_ratio` | `1:1` | 长宽比：1:1/16:9/4:3/3:2/9:16/4:5/5:4/21:9/3:4/2:3 |
| `enable_sticker_split` | `true` | 表情包自动切割 |
| `enable_sticker_zip` | `false` | 切分后打包 ZIP 发送 |
| `sticker_grid` | `4x4` | 表情包提示词网格描述 |
| `preserve_reference_image_size` | `false` | 改图时保留参考图尺寸 |
| `enable_grounding` | `false` | Gemini 搜索接地 |
| `max_reference_images` | `6` | 最大参考图数量 |
| `enable_text_response` | `false` | 同时返回文本说明 |
| `force_resolution` | `false` | 强制传分辨率参数 |
| `resolution_param_name` | `image_size` | 自定义分辨率参数名 |
| `aspect_ratio_param_name` | `aspect_ratio` | 自定义长宽比参数名 |
| `max_inline_image_size_mb` | `2.0` | 本地图片 base64 编码阈值 |
| `llm_tool_timeout_reserve_percent` | `50` | 为 `tool_call_timeout` 预留的百分比；剩余时间用于前台同步等待 |

**quick_mode_settings**
- 可覆盖各快速模式的默认分辨率/长宽比
- 支持模式：`avatar`/`poster`/`wallpaper`/`card`/`mobile`/`figure`/`sticker`

**retry_settings**
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `max_attempts_per_key` | `3` | 每个密钥最大重试次数 |
| `enable_smart_retry` | `true` | 按错误类型智能重试 |
| `total_timeout` | `120` | 单次调用总超时（秒） |

**service_settings**
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `nap_server_address` | `localhost` | NAP 文件传输地址 |
| `nap_server_port` | `3658` | NAP 文件传输端口 |
| `auto_avatar_reference` | `false` | 自动获取头像作为参考图 |
| `theme_settings.mode` | `cycle` | 帮助页主题模式 |

**help_render_mode**
- `html`：使用 t2i 网络服务渲染（默认）
- `local`：本地 Pillow 渲染
- `text`：纯文本输出

**limit_settings**
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `group_limit_mode` | `none` | 群限制模式：none/whitelist/blacklist |
| `group_limit_list` | `[]` | 群号列表 |
| `rate_limit_rules` | `[]` | 限流规则列表（template_list），支持多规则配置 |
| `default_rate_limit.enabled` | `false` | 默认限流开关（未匹配规则时使用） |
| `default_rate_limit.period_seconds` | `60` | 默认限流周期（秒） |
| `default_rate_limit.max_requests` | `5` | 默认单群周期内最大请求数 |

**cache_settings**
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `cache_ttl_minutes` | `5` | 缓存保留时间（分钟） |
| `cleanup_interval_minutes` | `30` | 清理间隔（分钟） |
| `max_cache_files` | `100` | 缓存文件数量上限 |

**doubao_settings**（豆包生图专用配置）
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `api_key` | - | 火山引擎 API Key（必填） |
| `endpoint_id` | `doubao-seedream-4.5` | Endpoint/Model ID（ep-xxxx 或 doubao-seedream-4.5/4.0） |
| `api_base` | `https://ark.cn-beijing.volces.com` | API 端点地址 |
| `default_size` | `2K` | 默认尺寸（2K/4K 或具体尺寸） |
| `watermark` | `false` | 是否添加水印 |
| `optimize_prompt_mode` | `standard` | 提示词优化模式（standard/fast） |
| `sequential_image_generation` | `disabled` | 组图生成模式（disabled/auto），[官方文档](https://www.volcengine.com/docs/82379/1824121?lang=zh#fc9f85e4) |
| `sequential_max_images` | `4` | 组图最大数量（2-15） |

**openai_images_settings**（OpenAI Images API 专用配置）
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `api_keys` | `[]` | API Key 列表，支持多 Key 轮换 |
| `model` | `gpt-image-1` | 模型名称（dall-e-2/dall-e-3/gpt-image-1 等） |
| `api_base` | - | API 端点地址，留空使用 OpenAI 官方 |
| `quality` | - | 图像质量（GPT image: auto/high/medium/low；dall-e-3: hd/standard） |
| `response_format` | `b64_json` | 响应格式（b64_json/url） |
| `size_mode` | `preset` | 尺寸模式：`preset` 使用全局分辨率映射，`custom` 使用 `custom_size` 直接传给 OpenAI |
| `custom_size` | `1024x1024` | 自定义尺寸示例值。仅 `size_mode=custom` 生效；该模式下此项必填，格式需为 `WxH` |
| `style` | - | 图像风格，仅 dall-e-3（vivid/natural） |
| `background` | - | 背景透明度，仅 GPT image（auto/transparent/opaque） |
| `output_format` | - | 输出格式，仅 GPT image（png/jpeg/webp） |
| `output_compression` | `0` | 输出压缩率滑动条 0-100，0 表示不传（使用服务端默认），仅 GPT image + jpeg/webp |
| `moderation` | - | 审核模式，仅 GPT image（如 low） |
| `generations_only` | `false` | 开启后强制只用文生图端点，不走 /v1/images/edits |

> `size_mode=custom` 时，插件会在发送请求前校验 `custom_size` 是否满足 OpenAI 官方限制：
> 最大边 `<= 3840`、宽高均为 `16` 的倍数、长短边比 `<= 3:1`、总像素在 `655360-8294400` 之间。
> 官方文档：
> <https://developers.openai.com/api/docs/guides/image-generation>
> <https://developers.openai.com/api/docs/models/gpt-image-2>

> **LLM 工具行为**：当 `api_type=openai_images` 且 `size_mode=custom` 时，LLM 工具参数会从 `resolution`/`aspect_ratio` 切换为仅暴露 `size`。若 LLM 未显式传入 `size`，插件会自动使用配置中的 `custom_size` 值，并在返回结果中提示模型。

**xai_settings**（xAI Images API 专用配置）
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `api_keys` | `[]` | API Key 列表，支持多 Key 轮换 |
| `model` | `grok-imagine-image` | xAI 图像模型名称 |
| `api_base` | `https://api.x.ai` | API 端点地址 |
| `response_format` | `url` | 响应格式（`url`/`b64_json`） |
| `quality` | - | 透传给 xAI 图片接口，留空不传；建议仅在确认接口支持时使用 |
| `n` | `1` | 单次请求生成数量，当前最多 `10` |
| `proxy` | - | 独立代理地址 |

> `xai` 供应商会自动走 xAI 官方 JSON 图像接口：
> - 文生图：`/v1/images/generations`
> - 改图：`/v1/images/edits`
>
> 改图请求会把参考图统一内联为 `data URI`，不使用 `multipart/form-data`。xAI 官方文档当前说明单次编辑最多支持 `5` 张参考图，分辨率支持 `1k/2k`，单图编辑时输出比例默认跟随输入图。

## 🎯 使用指南

### 命令列表

| 命令 | 说明 | 示例 |
|------|------|------|
| `/生图` | 纯文本生成 | `/生图 一只可爱的橙色小猫` |
| `/改图` | 基于参考图修改 | 发送图片 + `/改图 把头发改成红色` |
| `/换风格` | 风格转换 | 发送图片 + `/换风格 水彩` |
| `/快速 头像` | 头像模式 (1K, 1:1) | `/快速 头像 商务风格` |
| `/快速 海报` | 海报模式 (2K, 16:9) | `/快速 海报 赛博朋克` |
| `/快速 壁纸` | 壁纸模式 (4K, 16:9) | `/快速 壁纸 未来城市` |
| `/快速 卡片` | 卡片模式 (1K, 3:2) | `/快速 卡片 商务名片` |
| `/快速 手机` | 手机壁纸 (2K, 9:16) | `/快速 手机 极简风格` |
| `/快速 手办化` | 手办效果 (2K, 3:2) | `/快速 手办化 [1/2] 动漫角色` |
| `/快速 表情包` | 表情包 (4K, 16:9) | `/快速 表情包 Q版可爱` |
| `/切图` | 切割图片 | `/切图` 或 `/切图 4 4` |
| `/生图帮助` | 查看帮助 | `/生图帮助` |

### 智能头像功能

**触发条件**（启用 `auto_avatar_reference` 后）：
- "按照我"、"根据我"、"基于我"、"我的头像" → 自动获取发言人头像
- @某人 + 生图/改图命令 → 获取被@用户头像

**优先级**：@指定用户 > 发言者自己

### 快速模式说明

| 模式 | 分辨率 | 比例 | 说明 |
|------|--------|------|------|
| 头像 | 1K | 1:1 | 个人头像 |
| 海报 | 2K | 16:9 | 宣传海报 |
| 壁纸 | 4K | 16:9 | 高清壁纸 |
| 卡片 | 1K | 3:2 | 名片卡片 |
| 手机 | 2K | 9:16 | 手机壁纸 |
| 手办化 | 2K | 3:2 | 树脂手办效果，参数 1=PVC，2=GK |
| 表情包 | 4K | 16:9 | Q版 LINE 风格表情包 |

### 切图指令

- **智能切分**：`/切图`（自动检测网格）
- **手动网格**：`/切图 4 4`、`/切图 44`、`/切图4x4`
- **主体吸附**：指令包含"吸附"启用主体+附件吸附算法
- **流程**：手动网格 > AI 识别（需配置 vision_provider_id）> 智能网格 > 主体吸附兜底

### LLM 工具集成

通过自然语言触发（无需记忆命令）：
- "帮我画一只可爱的小猫"
- "把我的头像改成动漫风格"
- "基于这张图生成一个海报"

**混合触发模式**：LLM 工具会根据当前 `tool_call_timeout` 和 `llm_tool_timeout_reserve_percent` 自动计算前台等待窗口；如果图片在窗口内生成完成，以 `CallToolResult`（含 `ImageContent`）结构化返回给框架，由模型决定后续操作。若超过等待窗口，则自动切到后台继续生成，完成后通过 `event.send()` 直接发送给用户。代理模式下前台和后台均通过代理下载远程图片，确保全链路可用。

**参数动态切换**：当使用 `openai_images` 供应商且启用 `size_mode=custom` 时，LLM 工具仅接受 `size` 参数（格式 `WxH`），不再接受 `resolution` 和 `aspect_ratio`。其他供应商/模式继续使用 `resolution`/`aspect_ratio`。

**参数校验**：LLM 传入非法的 `resolution`、`aspect_ratio` 或 `size` 时，工具会直接返回错误并要求模型修正后重试，不再静默回退到默认值。

**Gemini 思维签名处理**：插件会把 Gemini 返回的 `thought_signature` 视为仅限协议层使用的 opaque 元数据，不会再把它拼进 Tool 文本结果或用户可见内容。这样可以避免部分 Gemini / NewAPI 网关把超大签名重新塞回上下文，导致 `413` 或“输入 Tokens 数量超过系统限制”。

**论坛发帖模式**：当用户要求将图片发到论坛/AstrBook 时，AI 会设置 `for_forum=true`，此时工具同步等待生成完成并返回图片路径/URL，AI 可自动调用 `upload_image` 上传图床后完成全自动发帖流程。

### 表情包切分算法

SmartMemeSplitter v4 特点：
- **颜色边缘突变分析**：彩色形态学梯度 + OTSU 自适应阈值
- **能量图分析**：基于 Sobel 算子的多通道能量计算
- **投影分析**：水平/垂直投影检测网格边界
- **网格候选微调**：精细调整找到最清晰的分隔线

## 🔍 故障排除

| 问题 | 解决方案 |
|------|----------|
| API 错误 | 检查 API 密钥、模型名称（如 `gemini-3-pro-image-preview`）、api_type 配置 |
| 生成超时 | 降低分辨率、简化提示词、增加工具超时时间（推荐 100s+） |
| LLM 工具生图后报 `413` / 上下文爆炸 | 升级到 `v1.9.12+`；该版本已过滤 `thought_signature`，不再把超大签名回灌到 Tool 结果 |
| 无法获取头像 | 确认使用 NapCat 平台 |
| 切图效果不佳 | 手动指定网格 `/切图 4 4` 或配置视觉提供商 |
| 中文乱码 (local 模式) | 等待字体自动下载或手动放置 `.ttf` 字体到 `tl/` 目录 |
| 网络连接失败 | 在 `api_settings.proxy` 填写代理地址（如 `http://127.0.0.1:7890`），或在各 provider override 中单独配置 |

开启 AstrBot debug 模式查看详细日志。

## 📁 项目结构

```
astrbot_plugin_gemini_image_generation/
├── main.py                 # 插件主入口（业务流程编排）
├── metadata.yaml           # 插件元数据
├── _conf_schema.json       # 配置 Schema
├── requirements.txt        # 依赖列表
├── README.md               # 说明文档
├── CHANGELOG.md            # 更新日志
├── LICENSE                 # AGPL-3.0 许可证
├── docs/
│   └── 新增API供应商.md    # 适配器开发指南
├── templates/              # 帮助页面模板
│   ├── help_template.md
│   ├── help_template_dark.html
│   └── help_template_light.html
└── tl/                     # 核心模块
    ├── __init__.py
    ├── api_types.py        # API 类型定义（ImageGenerationConfig 等）
    ├── avatar_handler.py   # 头像获取和管理（缓存、下载、标记）
    ├── enhanced_prompts.py # 提示词增强（手办化、表情包等预设）
    ├── help_renderer.py    # 帮助页渲染（HTML/Local/Text 三种模式）
    ├── image_generator.py  # 图像生成核心逻辑（参数组装、调用 API）
    ├── image_handler.py    # 图像处理（过滤、下载、格式转换）
    ├── image_splitter.py   # 图像切分（SmartMemeSplitter v4 算法）
    ├── llm_tools.py        # LLM 工具定义（触发器模式实现）
    ├── message_sender.py   # 消息格式化和发送（合并转发、ZIP 打包）
    ├── openai_image_size.py # OpenAI Images 自定义尺寸校验与归一化
    ├── plugin_config.py    # 配置加载和管理（PluginConfig 类）
    ├── rate_limiter.py     # 限流和群限制（KV 持久化）
    ├── sticker_cutter.py   # 主体+附件吸附分割（兜底算法）
    ├── thought_signature.py # Gemini thought signature 安全处理
    ├── tl_api.py           # API 客户端（请求发送、重试、图片下载）
    ├── tl_utils.py         # 工具函数（错误格式化、路径处理）
    ├── vision_handler.py   # 视觉 LLM 操作（网格行列识别）
    └── api/                # API 供应商适配器
        ├── __init__.py
        ├── base.py         # 适配器基类
        ├── doubao.py       # 豆包（Volcengine Ark）适配
        ├── google.py       # Google/Gemini 官方 API
        ├── grok2api.py     # grok2api 适配
        ├── openai_compat.py # OpenAI 兼容格式
        ├── openai_images.py # OpenAI Images 原生端点适配
        ├── registry.py     # 供应商注册表
        ├── xai.py          # xAI Images 原生端点适配
        └── zai.py          # Zai.is 适配
```

## 🤝 贡献

欢迎提交 [Issue](https://github.com/piexian/astrbot_plugin_gemini_image_generation/issues) 和 [Pull Request](https://github.com/piexian/astrbot_plugin_gemini_image_generation/pulls)！

新增 API 供应商请参考：[适配器开发指南](docs/新增API供应商.md)

### 致谢

- [@MliKiowa](https://github.com/MliKiowa) - 图像切割算法
- [@exynos967](https://github.com/exynos967) - 限流设置、手办化功能、OpenAI 兼容
- [@zouyonghe](https://github.com/zouyonghe) - 代理支持、保留参考图尺寸、空格参数支持
- [@vmoranv](https://github.com/vmoranv) - 表情包提示词优化
- [@itismygo](https://github.com/itismygo) - grok2api 适配
- [@Clhikari](https://github.com/Clhikari) - 快速生图修复
- [@YukiRa1n](https://github.com/YukiRa1n) - GIF 支持、多项修复

## 📄 许可证

AGPL-3.0 License - 详见 [LICENSE](./LICENSE)

## 🔗 相关链接

- [项目地址](https://github.com/piexian/astrbot_plugin_gemini_image_generation) | [更新日志](./CHANGELOG.md) | [问题反馈](https://github.com/piexian/astrbot_plugin_gemini_image_generation/issues)
- [AstrBot](https://docs.astrbot.app/) | [Google Gemini API](https://ai.google.dev/) | [NapCat](https://napneko.github.io/)
- [AstrBook 论坛插件](https://github.com/advent259141/astrbot_plugin_astrbook) - 配合 for_forum 模式实现 AI 全自动生图发帖
- [grok2api](https://github.com/chenyme/grok2api) | [zaiis2api](https://github.com/Futureppo/zaiis2api) | [zai.is](https://zai.is)

---

<div align="center">

**如果这个插件对你有帮助，请给个 ⭐ Star 支持一下！**

</div>

## Star History

[![Star History Chart](https://api.star-history.com/image?repos=piexian/astrbot_plugin_gemini_image_generation&type=date&legend=top-left)](https://www.star-history.com/?repos=piexian%2Fastrbot_plugin_gemini_image_generation&type=date&legend=top-left)
