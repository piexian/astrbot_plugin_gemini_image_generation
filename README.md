# AstrBot Gemini 图像生成插件

<div align="center">

**强大的 AstrBot 图像生成插件，支持生图、改图、头像参考、表情包切分和 LLM 工具调用。**

</div>

## 功能概览

- **生图命令**：文字生图、图片改图、换风格，头像／海报／壁纸／手办化等一键预设，表情包生成与切分。详见 [命令列表](docs/usage.md#命令列表) 与 [快速模式](docs/usage.md#快速模式)。
- **关键词触发**：说出特定关键词自动带上参考图和提示词片段，支持命令与自动匹配两种模式。详见 [关键词触发](docs/usage.md#关键词触发)。
- **智能参考图**：自动读取消息图片、引用、转发、群文件和头像；生成图与参考图库图片可按编号引用继续改图。详见 [内部引用](docs/references.md)。
- **多供应商支持**：Google、OpenAI、豆包、MiniMax 等 15 家供应商，可指定模型或按配置顺序自动切换，能力矩阵见 [供应商说明](#供应商说明)。
- **LLM 工具集成**：AI 在对话中自动调用生图，支持单图、批量、查询记录与共享图片，任务完成自动通知。详见 [LLM 工具集成](docs/usage.md#llm-工具集成)。
- **插件接入接口**：其他 AstrBot 插件可调用本插件的生图服务，支持查询、等待与完成回调。详见 [接入指南](docs/plugin-api.md)。
- **限流与额度**：群白名单／黑名单、生成额度与限流，生成图按容量自动清理。详见 [限流控制](docs/usage.md#限流控制)。
- **内置 WebUI 创作台**：Dashboard 里的可视化创作台（需 AstrBot ≥4.29），生图、参考图库、历史画廊与供应商配置。详见 [WebUI 创作台](docs/usage.md#webui-创作台)。
- **提示词模板与广场**：快速模式提示词是可编辑模板，支持模板包导入导出；广场可管理模板并检索、导入、分享在线模板。详见 [模板库](docs/templates.md) 与 [提示词广场](docs/usage.md#提示词广场)。

## 快速安装

### 前置要求

- AstrBot 4.10.4+
- Python 3.12+
- NapCat（目前主要适配 NapCat 平台）
- QQ 官方机器人 WebSocket（`qq_official`，头像参考已通过真实实例验证）
- QQ 官方 V2 适配器（`qq_official_v2` / `qq_official_v2_webhook`）

> [!WARNING]
> 代码同时兼容 QQ 官方机器人 Webhook（`qq_official_webhook`）的头像获取结构，但目前没有真实 Webhook 实例可供验证，不保证一定可用。

### 安装方式

**插件市场**：搜索 `Gemini 图像生成` 并安装。

**链接安装**：在插件界面右下角点击加号，选择从链接安装，输入：

```text
https://github.com/piexian/astrbot_plugin_gemini_image_generation
```

依赖会按 [requirements.txt](https://github.com/piexian/astrbot_plugin_gemini_image_generation/blob/master/requirements.txt) 自动安装。

## 最小配置

插件配置中添加：

- 在 `provider_settings.provider_overrides` 中添加供应商模板（如 `google` / `openai_images` / `agnes_ai`），填入 `api_keys`、`model`、`api_base` 等字段；
- 同类型可添加多条模板，通过 `priority` 控制优先级；相同优先级按配置表顺序尝试；
- 可选配置 `provider_settings.provider_polling`，按列表从上到下自动尝试生成；重复供应商会自动去重，未知供应商会记录错误并跳过；
- 使用 `openai_images`、`openai_responses`、`doubao` 或 `dashscope` 且 `size_mode=custom` 时，配置界面只显示 `custom_size`，避免混用通用分辨率字段。

接入 SenseAudio 时，添加 `senseaudio` 模板并填入 `api_keys` 即可使用默认 Image 2.0 模型；`request_mode` 可选同步或异步。模型尺寸限制和配置示例见 [SenseAudio 配置](docs/config.md#senseaudio图片生成专用配置)。

文档索引：

- [完整配置参考](https://github.com/piexian/astrbot_plugin_gemini_image_generation/blob/master/docs/config.md)
- [使用指南](https://github.com/piexian/astrbot_plugin_gemini_image_generation/blob/master/docs/usage.md)
- [提示词模板库](docs/templates.md)
- [故障排除](https://github.com/piexian/astrbot_plugin_gemini_image_generation/blob/master/docs/troubleshooting.md)
- [新增 API 供应商](https://github.com/piexian/astrbot_plugin_gemini_image_generation/blob/master/docs/新增API供应商.md)
- [其他插件接入指南（API v1）](docs/plugin-api.md)

## 常用命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `/生图` | 纯文本生成 | `/生图 一只可爱的橙色小猫` |
| `/改图` | 基于参考图修改 | 发送图片 + `/改图 把头发改成红色` |
| `/换风格` | 风格转换 | 发送图片 + `/换风格 水彩` |
| `/快速 头像` | 头像模式 | `/快速 头像 商务风格` |
| `/快速 海报` | 海报模式 | `/快速 海报 赛博朋克` |
| `/快速 壁纸` | 壁纸模式 | `/快速 壁纸 未来城市` |
| `/快速 卡片` | 卡片模式 | `/快速 卡片 商务名片` |
| `/快速 手机` | 手机壁纸 | `/快速 手机 极简风格` |
| `/快速 手办化` | 手办效果，附图即可；风格和描述可选 | `/快速 手办化` + 图片，或 `/快速 手办化 2 戴眼镜` + 图片 |
| `/快速 表情包` | 表情包 | `/快速 表情包 Q版可爱` |
| `/切图` | 切割图片 | `/切图` 或 `/切图 4 4` |
| `/生图帮助` | 查看帮助 | `/生图帮助` |

所有快速模式均自带提示词，额外描述可省略，例如发送图片并输入 `/快速 头像`、`/快速 壁纸` 或 `/快速 手办化`。普通 `/生图`、`/改图` 仍需提供描述；`/换风格` 需指定风格，附加描述可选。

更多参数、快速模式说明和 LLM 工具行为见 [使用指南](https://github.com/piexian/astrbot_plugin_gemini_image_generation/blob/master/docs/usage.md)。

## 供应商说明

全部供应商支持文生图，后台任务与批量生成由插件统一调度；图生图支持范围因供应商和模型而异，改图时不支持参考图的候选会自动跳过。各供应商端点、参数与尺寸适配规则见 [完整配置参考](https://github.com/piexian/astrbot_plugin_gemini_image_generation/blob/master/docs/config.md)。

| 供应商 | 文生图 | 图生图 | 后台任务 | 备注 |
|--------|--------|--------|----------|------|
| Google Gemini（`google`） | ✓ | ✓ | ✓ | 可配搜索接地 |
| Gemini Interactions（`gemini_interactions`） | ✓ | ✓ | ✓ | Nano Banana 系列 |
| Vertex AI（`vertex`） | ✓ | ✓ | ✓ | 服务账号 / Express 双认证 |
| OpenAI 兼容（`openai`） | ✓ | ✓ | ✓ | 图生图取决于上游模型 |
| OpenAI Images（`openai_images`） | ✓ | ✓ | ✓ | 支持 GIF/SVG 输出 |
| OpenAI Responses（`openai_responses`） | ✓ | ✓ | ✓ | 支持连续多轮编辑 |
| Agnes AI（`agnes_ai`） | ✓ | ✓ | ✓ | |
| xAI Images（`xai`） | ✓ | ✓ | ✓ | 后台默认 deferred 异步 |
| MiniMax（`minimax`） | ✓ | ✓ | ✓ | H3 原生 4K |
| 豆包（`doubao`） | ✓ | ✓ | ✓ | Seedream 系列 |
| SenseNova（`sensenova`） | ✓ | 部分模型 | ✓ | 仅 u1.5 系列支持图生图 |
| SenseAudio（`senseaudio`） | ✓ | ✓ | ✓ | 同步 / 异步可选 |
| DashScope（`dashscope`） | ✓ | 部分模型 | ✓ | z-image 仅文生图 |
| ModelScope（`modelscope`） | ✓ | 部分模型 | ✓ | 原生异步任务；仅 edit 模型支持图生图 |
| SiliconFlow（`siliconflow`） | ✓ | 部分模型 | ✓ | 仅 Qwen-Image-Edit / Kolors 支持图生图 |

## 项目结构

```text
astrbot_plugin_gemini_image_generation/
├── main.py                 # 插件主入口
├── _conf_schema.json       # 配置 Schema
├── docs/                   # 配置、使用、故障排除和适配器文档
├── views/                  # Dashboard 插件视图（studio 创作台，纯静态，需 AstrBot ≥4.29）
├── .astrbot-plugin/i18n/   # 视图标题与界面文案翻译（跟随 Dashboard 语言）
├── templates/              # 帮助页面模板
└── tl/                     # 核心模块和供应商适配器
```

`tl/` 目录接口索引见：[tl 模块接口说明](https://github.com/piexian/astrbot_plugin_gemini_image_generation/blob/master/tl/README.md)。

## 贡献

欢迎提交 [Issue](https://github.com/piexian/astrbot_plugin_gemini_image_generation/issues) 和 [Pull Request](https://github.com/piexian/astrbot_plugin_gemini_image_generation/pulls)。

开发环境、验证与提交约定见 [开发指南](docs/development.md)；新增 API 供应商见 [适配器开发指南](docs/新增API供应商.md)。

### 致谢

- [@MliKiowa](https://github.com/MliKiowa) - 图像切割算法
- [@exynos967](https://github.com/exynos967) - 限流设置、手办化功能、OpenAI 兼容、Zai 供应商、快速模式配置
- [@zouyonghe](https://github.com/zouyonghe) - 代理支持、保留参考图尺寸、空格参数支持
- [@vmoranv](https://github.com/vmoranv) - 表情包提示词优化、自适应黑描边贴纸切分、线条切图修复
- [@itismygo](https://github.com/itismygo) - grok2api 适配
- [@xunxiing](https://github.com/xunxiing) - OpenAI Images 端点支持、配置提示修复
- [@Clhikari](https://github.com/Clhikari) - 快速生图修复
- [@YukiRa1n](https://github.com/YukiRa1n) - GIF 支持、多项修复
- [@Dashu-Li](https://github.com/Dashu-Li) - 工具参数 null 值覆盖默认值修复、批量并发测试修复

## 许可证

AGPL-3.0 License - 详见 [LICENSE](https://github.com/piexian/astrbot_plugin_gemini_image_generation/blob/master/LICENSE)。

## 相关链接

- [项目地址](https://github.com/piexian/astrbot_plugin_gemini_image_generation)
- [更新日志](https://github.com/piexian/astrbot_plugin_gemini_image_generation/blob/master/CHANGELOG.md)
- [问题反馈](https://github.com/piexian/astrbot_plugin_gemini_image_generation/issues)
- [AstrBot](https://docs.astrbot.app/)
- [Google Gemini API](https://ai.google.dev/)
- [NapCat](https://napneko.github.io/)
- [AstrBook 论坛插件](https://github.com/advent259141/astrbot_plugin_astrbook)

## Star History

<a href="https://www.star-history.com/?repos=piexian%2Fastrbot_plugin_gemini_image_generation&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=piexian/astrbot_plugin_gemini_image_generation&type=date&theme=dark&legend=top-left&sealed_token=Z2XbcOsIu5Vg1iRLrkQbpmCSLkAoEzGqgt9eiqaqMx8gNx7IDJO6nfyApkfrcU0PAQGpcbWkrWlfcUoYmgsOOUozIaQ6p7XOLMOVPcem8xNIigKJe9wFEw" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=piexian/astrbot_plugin_gemini_image_generation&type=date&legend=top-left&sealed_token=Z2XbcOsIu5Vg1iRLrkQbpmCSLkAoEzGqgt9eiqaqMx8gNx7IDJO6nfyApkfrcU0PAQGpcbWkrWlfcUoYmgsOOUozIaQ6p7XOLMOVPcem8xNIigKJe9wFEw" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=piexian/astrbot_plugin_gemini_image_generation&type=date&legend=top-left&sealed_token=Z2XbcOsIu5Vg1iRLrkQbpmCSLkAoEzGqgt9eiqaqMx8gNx7IDJO6nfyApkfrcU0PAQGpcbWkrWlfcUoYmgsOOUozIaQ6p7XOLMOVPcem8xNIigKJe9wFEw" />
 </picture>
</a>
