# AstrBot Gemini 图像生成插件 v1.8.3

<div align="center">

![Version](https://img.shields.io/badge/Version-v1.8.3-blue)
![License](https://img.shields.io/badge/License-MIT-orange)

**🎨 强大的 Gemini 图像生成插件，支持智能头像参考和智能表情包切分**

</div>

## ✨ 特性

### 🖼️ **核心功能**
- **生图模式**: 纯文本到图像生成，支持多种风格和参数
- **改图模式**: 基于参考图像进行修改和风格转换，支持配置化头像参考
- **换风格模式**: 专门的风格转换功能，支持配置化头像参考
- **智能头像参考**: 自动获取用户头像和@指定对象头像作为参考，改图和换风格功能支持配置控制
- **多API支持**: 兼容 Google 官方 API、OpenAI 兼容格式 API、Zai API 以及 grok2api
- **多格式支持**: 支持 PNG、JPEG、WEBP、HEIC/HEIF、GIF 等多种图片格式
- **智能表情包切分**: 使用 SmartMemeSplitter 算法自动将生成的表情包网格切割为独立图片，支持合并转发和ZIP打包
- **LLM 工具集成**: 支持 AstrBot LLM 工具调用，通过自然语言智能触发图像生成

### 🛡️ **限制/限流**
- **群限制模式**: 支持不限制/白名单/黑名单三种模式
- **群内限流**: 单群在指定周期内的请求次数限制
- **灵活配置**: 群限制和限流可同时启用，实现精细控制
- **防滥用**: 有效防止API滥用和资源浪费

### 🧠 **智能特性**
- **自然语言触发**: 支持"按照我"、"修改"、"@人"等自然语言触发
- **智能优先级**: @用户 > 发言人 > 群头像（暂未实现）
- **降级处理**: 自动处理参数兼容性问题
- **缓存机制**: 避免重复下载头像，提升性能
- **多文件配置**: 支持不同聊天环境的独立配置
- **智能标记**: 为头像添加了标记用于识别
- **分辨率控制**: 支持 1K、2K、4K 分辨率
- **长宽比调整**: 支持多种常用比例（1:1, 16:9, 4:3, 9:16, 4:5, 5:4, 21:9, 3:4, 2:3等）
- **改图尺寸保持**: 可选开关，改图/换风格时沿用参考图原始尺寸，不强制修改分辨率与比例
- **Google 搜索接地**: 实时数据参考生成（仅限 Gemini 模型）
- **智能重试**: 自动重试机制，提高成功率
- **超时管理**: 适配框架超时控制
- **主题配置**: 可配置的白天/黑夜主题自动切换
- **自动主题切换**: 根据时间自动选择白色/黑色主题
- **自定义时间段**: 可设置白天开始和结束时间（0-23点）
- **手动主题模式**: 可强制使用指定主题（白色/黑色）
- **动态渲染**: 帮助页面会根据时间或配置动态显示对应主题
- **强制分辨率参数**: 支持强制传递分辨率参数，兼容各类非官方模型
- **智能表情包切分**: 使用 SmartMemeSplitter v4 算法，支持颜色边缘突变检测、能量图分析、网格候选微调
- **定时清理**: 自动清理过期临时文件，支持配置缓存保留时间和清理间隔

## 📦 安装

### 前置要求
- AstrBot 4.5.0+
- Python 3.10+
- NapCat（必备目前仅做了napcat平台适配）

### 依赖库
插件会自动安装以下依赖（见 [requirements.txt](requirements.txt)）：
- `opencv-python`：图像处理
- `numpy`：数值计算
- `Pillow`：图像操作

### 安装指南

您可以通过以下两种方式安装 `Gemini 图像生成` 插件：

---

#### 方式一：通过 Git 克隆

1. **进入插件目录**
   打开终端，并使用 `cd` 命令进入 `AstrBot/data/plugins/` 目录。

2. **克隆仓库**
   在终端中执行以下命令，将插件仓库克隆到本地：
   ```bash
   git clone https://github.com/piexian/astrbot_plugin_gemini_image_generation
   ```

---

#### 方式二：通过插件市场

1. **打开插件市场**
   在 AstrBot 的界面中，找到并进入插件市场。

2. **搜索插件**
   在搜索框中输入 `Gemini 图像生成`。

3. **点击安装**
   在搜索结果中找到该插件，并点击"安装"按钮。


## 🔧 配置

### 基础配置

在插件配置中设置以下参数：

- **api_settings.provider_id**: 生图模型提供商（`_special: select_provider`），自动读取模型/密钥/端点；不选将无法调用
- **api_settings.vision_provider_id**: 视觉识别提供商（可选，用于切图前 AI 识别网格行列；留空则不调用）
- **html_render_options.quality**: HTML 帮助页截图质量（1-100，可选）
- **参考图格式校验**: 参考图会在发送前统一检查 MIME，非 Gemini 支持的类型（PNG/JPEG/WEBP/HEIC/HEIF）将自动转为 PNG 再编码。

### 配置项详解

**api_settings**
- `provider_id`：必填，从 AstrBot 提供商中选择生图模型。
- `api_type`：必填，覆盖提供商类型（`google`/`openai`/`zai`/`grok2api`）。
  - `google`：使用 Google/Gemini 官方 API
  - `openai`：使用 OpenAI 兼容格式 API（默认）
  - `zai`：启用 Zai 兼容参数传递（顶层分辨率/比例 + generation_config）
  - `grok2api`：支持相对路径图片与临时缓存图片的自动下载落盘
- `model`：可选，覆盖提供商模型名称。
- `vision_provider_id`：可选，切图前调用视觉模型识别网格行列；留空则跳过 AI 识别。

**image_generation_settings**
- `resolution`：生成图像分辨率，默认 `1K`（可选 1K/2K/4K）。
- `aspect_ratio`：长宽比，默认 `1:1`（可选 1:1, 16:9, 4:3, 3:2, 9:16, 4:5, 5:4, 21:9, 3:4, 2:3）。
- `enable_sticker_split`：表情包切分，默认 true。
- `enable_sticker_zip`：切分后是否打包 ZIP 发送，默认 false。
- `sticker_grid`：表情包提示词网格描述，格式如 `4x4`（建议范围 1-20），默认 `4x4`。
- `preserve_reference_image_size`：改图/换风格时尽量保留参考图尺寸，默认 false。
- `enable_grounding`：Gemini 搜索接地，默认 false（仅 gemini-3-pro-image-preview 支持）。
- `max_reference_images`：参考图最大数量，默认 6（Gemini 3 最多14张，Gemini 2.5 建议不超过4张）。
- `enable_text_response`：是否同时返回文本说明，默认 false（仅 Gemini 3 有效）。
- `force_resolution`：强制传 `image_size` 参数给模型，默认 false。
- `resolution_param_name`：**自定义分辨率参数名**，不同 API 可能使用不同字段名（如 `image_size`、`size`、`resolution`），默认 `image_size`。
- `aspect_ratio_param_name`：**自定义长宽比参数名**，不同 API 可能使用不同字段名（如 `aspect_ratio`、`aspectRatio`、`image_aspect_ratio`），默认 `aspect_ratio`。

**quick_mode_settings**
- 可选：覆盖 `快速` 指令各模式的默认分辨率/长宽比；默认值即内置默认，可直接修改。
- 覆盖项（每个模式下都有 `resolution` / `aspect_ratio` 两个字段）：`avatar`/`poster`/`wallpaper`/`card`/`mobile`/`figure`/`sticker`。

**retry_settings**
- `max_attempts_per_key`：每个密钥的最大重试次数，默认 3。
- `enable_smart_retry`：按错误类型智能重试/切换密钥，默认 true。
- `total_timeout`：单次调用总超时（秒），默认 120。

**service_settings**
- `nap_server_address` / `nap_server_port`：NAP 文件传输地址与端口，默认 localhost:3658。
- `auto_avatar_reference`：自动获取头像作为参考图，默认 false。
- `verbose_logging`：输出详细日志，默认 false。
- `theme_settings.mode`：帮助页主题模式 `cycle`/`single`，默认 cycle。
 - `cycle_config.day_start`/`day_end`：白天时间段（小时），默认 6/18。
 - `cycle_config.day_template`/`night_template`：模板文件名，默认 `help_template_light` / `help_template_dark`。
 - `single_config.template_name`：单一模板文件名，默认 `help_template_light`。

**help_render_mode**
- 帮助页渲染模式，可选 `html`/`local`/`text`，默认 `html`。
  - `html`：使用 t2i 网络服务渲染 HTML 模板（公共接口，可能不稳定可以自建）。
  - `local`：本地 Pillow 渲染 Markdown（无需浏览器，适合资源受限环境）。
  - `text`：纯文本输出（最轻量）。

**html_render_options**（仅 html 模式生效）
- `quality`：截图质量（1-100，留空使用默认值，仅 jpeg 格式生效）。
- `type`：截图格式，`png` 或 `jpeg`，默认 `png`。
- `scale`：截图缩放方式，`device`（更清晰）或 `css`（更小更快），默认 `device`。
- `full_page`：是否截取整页，默认 true。
- `omit_background`：是否去除背景（仅 png 有效，可生成透明背景），默认 false。

**limit_settings**
- `group_limit_mode`：群限制模式 `none`/`whitelist`/`blacklist`，默认 none。
- `group_limit_list`：群号列表（字符串）。
- `enable_rate_limit`：是否开启群内限流，默认 false。
- `rate_limit_period`：限流周期（秒），默认 60。
- `max_requests_per_group`：单群周期内最大请求数，默认 5。

**cache_settings**
- `cache_ttl_minutes`：缓存保留时间（分钟），生成的图片、下载缓存、切图等文件保留多少分钟后自动清理，设为 0 表示不按时间清理，默认 5。
- `cleanup_interval_minutes`：清理间隔（分钟），定时清理任务的执行间隔，设为 0 表示禁用定时清理，默认 30。
- `max_cache_files`：缓存文件数量上限，各缓存目录的文件数量上限，超过时会按时间清理最旧的文件，设为 0 表示不限制数量，默认 100。

### 智能表情包切分
- 切割优先级：手动网格 > AI 行列识别（需配置 vision_provider_id）> 智能网格 > 主体+附件吸附兜底
- 未配置视觉提供商时直接进入智能网格；手动网格存在时不会调用 AI
- SmartMemeSplitter v4 算法特点：
  - **颜色边缘突变分析**：彩色形态学梯度 + OTSU 自适应阈值
  - **能量图分析**：基于 Sobel 算子的多通道能量计算
  - **投影分析**：水平/垂直投影检测网格边界
  - **网格候选微调**：精细调整找到最清晰的分隔线

## 🎯 使用指南

### 命令列表

| 命令 | 说明 | 示例 |
|------|------|------|
| `/生图` | 纯文本生成图像 | `/生图 一只可爱的橙色小猫` |
| `/改图` | 基于参考图修改 | 发送图片 + `/改图 把头发改成红色` |
| `/换风格` | 风格转换 | 发送图片 + `/换风格 水彩` |
| `/快速 头像` | 头像模式（1K, 1:1） | `/快速 头像 商务风格个人头像` |
| `/快速 海报` | 海报模式（2K, 16:9） | `/快速 海报 赛博朋克游戏宣传` |
| `/快速 壁纸` | 壁纸模式（4K, 16:9） | `/快速 壁纸 未来科技城市夜景` |
| `/快速 卡片` | 卡片模式（1K, 3:2） | `/快速 卡片 简约商务风格名片` |
| `/快速 手机` | 手机壁纸模式（2K, 9:16） | `/快速 手机 极简主义手机壁纸` |
| `/快速 手办化` | 手办化效果（2K, 3:2） | `/快速 手办化 [1/2] 动漫角色` |
| `/快速 表情包` | 表情包模式（4K, 16:9） | `/快速 表情包 Q版可爱表情` |
| `/切图` | 切割图片 | `/切图` 或 `/切图 4 4` |
| `/生图帮助` | 查看帮助和配置 | `/生图帮助` |

### 智能头像功能

#### 头像获取优先级
1. **@指定用户** - 最高优先级（获取被@用户的头像）
2. **发言者自己** - 中等优先级（获取发送消息用户的头像）
3. **群头像** - 低优先级（暂未实现）

#### 触发条件

##### 自动获取用户头像
启用 `auto_avatar_reference` 后，以下场景会自动获取头像：

- **个人相关**: "按照我"、"根据我"、"基于我"、"参考我"、"我的头像"、"像我"、"本人"
- **修改相关**: "修改"、"改图"、"重做"、"重新"、"调整"、"优化"、"换风格"
- **@用户**: 任何生图/改图命令 + @某人

#### 使用示例
```bash
# 场景1: 自动获取发言人头像
/生图 按照我生成一个动漫头像
# 结果: 获取发送者头像作为参考

# 场景2: 获取@用户头像
/改图 @小明 把头发改成蓝色
# 结果: 获取小明头像进行修改

# 场景3: 普通生图（不获取头像）
/生图 一只可爱的小猫
# 结果: 纯文本生成，不获取任何头像
```

### 高级功能

#### 快速模式
```bash
# 快速模式预设了最佳分辨率和比例，只需描述想要生成的内容

/快速 头像 商务风格的个人头像        # 配置: 1K分辨率，1:1比例
/快速 海报 赛博朋克游戏宣传          # 配置: 2K分辨率，16:9比例
/快速 壁纸 未来科技城市夜景          # 配置: 4K分辨率，16:9比例
/快速 卡片 简约商务风格名片          # 配置: 1K分辨率，3:2比例
/快速 手机 极简主义手机壁纸          # 配置: 2K分辨率，9:16比例
/快速 手办化 [1/2] 动漫角色         # 配置: 2K分辨率，3:2比例，支持PVC(1)和GK(2)两种风格
/快速 表情包 Q版可爱表情             # 配置: 4K分辨率，16:9比例，LINE风格
/切图                              # 对消息/引用/合并转发/群文件中的图片进行切割
```

**说明：**
- **头像模式**: 自动使用1K分辨率和1:1比例，适合个人头像
- **海报模式**: 自动使用2K分辨率和16:9比例，适合宣传海报
- **壁纸模式**: 自动使用4K分辨率和16:9比例，适合高清壁纸
- **卡片模式**: 自动使用1K分辨率和3:2比例，适合名片卡片
- **手机模式**: 自动使用2K分辨率和9:16比例，适合手机壁纸
- **手办化模式**: 树脂收藏级手办效果，支持通过参数`1`(PVC标准版)或`2`(树脂GK收藏版)选择风格
- **表情包模式**: 自动使用4K分辨率和16:9比例，生成Q版LINE风格表情包
- **切图指令**: 自动从当前消息、引用消息、合并转发节点或群文件中提取图片，默认使用智能网格切分
  - 流程：有手动网格则直接使用；否则若配置了视觉提供商先 AI 识别行列，再走智能网格，最后主体+附件吸附兜底
  - 手动网格：指令后写数字即可，支持“切图 4 4”“切图 44”“切图4x4”等格式，等价于横4列竖4行
  - 主体吸附：指令里包含“吸附”即启用主体+附件吸附分割算法

#### 风格转换
```bash
# 发送图片后使用
/换风格 动漫
```

#### 配置管理
```bash
/生图帮助  # 查看当前配置和参数
```

### LLM 工具集成

本插件集成了 AstrBot 的 LLM 工具功能，允许 LLM 在对话中智能调用图像生成功能。

#### 工具名称
- `gemini_image_generation` - Gemini 图像生成工具

#### 触发场景
当用户通过自然语言请求图像生成、绘画、改图或换风格时，LLM 会自动调用此工具：
- "帮我画一只可爱的小猫"
- "把我的头像改成动漫风格"
- "基于参考图片生成一个海报"
- "帮我生成一个手办化的角色"

#### 参数说明

| 参数 | 类型 | 说明 |
|------|------|------|
| `prompt` | string | 图像生成或修改的详细描述 |
| `use_reference_images` | string | 是否使用上下文中的参考图片（"true"/"false"） |
| `include_user_avatar` | string | 是否包含用户头像作为参考（"true"/"false"） |

#### 使用说明

1. **纯文本生成**：LLM 识别到生成图像的意图时，会自动调用工具并传入描述文字
2. **参考图模式**：当对话上下文包含图片或用户明确要求"修改这张图"时，LLM 会设置 `use_reference_images="true"`
3. **头像参考模式**：当用户说"按照我"、"根据我"或@某人时，LLM 会设置 `include_user_avatar="true"` 以获取对应头像作为参考

此功能使 LLM 能够更智能地处理图像生成请求，无需用户记忆特定命令，通过自然语言即可实现图像生成和修改。

### 智能表情包切分

#### SmartMemeSplitter v4 算法
使用先进的网格切分算法：
- **颜色边缘突变分析**: 彩色形态学梯度 + OTSU 自适应阈值
- **能量图分析**: 基于 Sobel 算子的多通道能量计算
- **投影分析**: 水平/垂直投影检测网格边界
- **网格候选微调**: 精细调整找到最清晰的分隔线

#### 切分效果
- **自动检测**: 智能识别表情包中的独立表情位置
- **精确切割**: 基于检测到的网格线进行精确切割
- **边缘优化**: 添加 padding 避免切割不完整
- **排序输出**: 按从左到右、从上到下的顺序输出表情
- **兜底方案**: 主体+附件吸附分割算法作为备选

## 🎨 图像生成技巧

### 📝 提示词优化

#### 基础结构
```
[主体描述] + [风格描述] + [细节要求] + [质量词汇]
```

#### 示例
```bash
# 好的提示词
/生图 一只白色波斯猫，蓝色大眼睛，坐在花园里，阳光透过树叶洒下，超高清，杰作，细节丰富

# 详细描述
/生图 赛博朋克风格的城市夜景，霓虹灯反射在雨后的街道上，飞行汽车穿梭于摩天大楼之间，电影级画质，写实风格

# 艺术风格
/生文 梵高风格的向日葵田，旋转的画笔，鲜艳的色彩，后印象派，油画质感
```

### 🎭 风格关键词

#### 艺术风格
- `动漫风格`、`漫画风格`、`卡通`
- `写实风格`、`超写实`、`照片级`
- `水彩画`、`油画`、`素描`
- `像素艺术`、`8bit`、`复古游戏`
- `赛博朋克`、`蒸汽朋克`、`科幻`
- `中国风`、`和风`、`欧美风`

#### 质量词汇
- `杰作`、`大师作品`、`精品`
- `超高清`、`4K`、`8K`
- `细节丰富`、`精致`、`精细`
- `专业摄影`、`电影级`、`宣传片`

### 📐 参数设置

#### 分辨率选择
- **1K**: 速度快，适合头像、图标
- **2K**: 平衡速度和质量，适合大多数场景
- **4K**: 最高质量，适合壁纸、海报

#### 长宽比
- **1:1**: 方形图片、头像、图标
- **16:9**: 横向图片、壁纸、海报
- **9:16**: 竖向图片、手机壁纸、封面
- **4:3**: 传统照片比例
- **3:2**: 单反相机比例

### ⚡ 性能优化

1. **选择合适分辨率**
   - 日常使用: 1K 或 2K
   - 高质量需求: 4K

2. **简化提示词**
   - 避免过于复杂的描述
   - 重点突出主体和风格

3. **合理使用参考图片**
   - 限制参考图片数量（默认最多6张）
   - 选择相关性高的参考图

### 💾 缓存管理

- **头像缓存**: 自动缓存用户头像，避免重复下载
- **智能更新**: 头像变化时自动更新缓存
- **定期清理**: 每30分钟自动清理过期临时文件

## 🔍 故障排除

### 常见问题

#### 🔤 字体相关（local 渲染模式）
**问题**: 使用 `local` 渲染模式时中文显示为方块或乱码

**解决方案**:
- 插件会自动下载中文字体（Noto Sans SC），首次使用时需等待下载完成
- 如果自动下载失败，可手动将任意 `.ttf`/`.otf`/`.ttc` 字体文件放入 `tl/` 目录，插件会自动识别并使用
- 系统已安装中文字体（如文泉驿、思源黑体等）时会自动使用系统字体，无需额外下载

#### 🔑 API 相关
**问题**: 生成失败，提示 API 错误
```
❌ 图像生成失败: API只返回了文本响应。请检查模型名称是否正确
```

**解决方案**:
- 检查 API 密钥是否有效
- 确认模型支持图像生成（如: `gemini-3-pro-image-preview`）
- 检查 API 额度是否充足
- 确认 `api_type` 配置正确（google/openai/zai/grok2api）

**问题**: 生成时间过长
```
❌ 图像生成时间过长，超出了框架限制
```

**解决方案**:
- 使用较低的分辨率（1K）
- 简化提示词
- 检查网络连接
- 在 AstrBot 本体配置文件中增加工具超时时间（使用 gemini-3-pro-image-preview 推荐 100s 以上）

#### 🖼️ 图像功能

**问题**: 无法获取头像
```
❌ 无法获取头像
```

**解决方案**:
- 确认使用的是 NapCat 平台（onebotv11/http）
- 检查 NapCat 是否正常运行
- 群头像功能暂未实现，当前仅支持用户头像

**问题**: 切图失败或效果不佳

**解决方案**:
- 尝试手动指定网格：`/切图 4 4`
- 配置 `vision_provider_id` 启用 AI 识别
- 图片应为清晰的网格布局

### 📊 状态检查

```bash
/生图帮助  # 查看当前配置和参数说明
```

#### 检查插件状态
- 确认插件文件夹名正确：`astrbot_plugin_gemini_image_generation`
- 检查配置文件是否存在
- 开启 `verbose_logging` 详细日志开关，查看控制台日志获取详细错误信息

## 🤝 贡献指南

欢迎提交 Issue 和 Pull Request！

### 🐛 报告问题
- 使用 [Issues](https://github.com/piexian/astrbot_plugin_gemini_image_generation/issues) 提交 bug 报告
- 提供详细的错误信息和日志
- 说明复现步骤

### 💡 功能建议
- 在 Issues 中提出功能建议
- 详细描述期望的功能
- 说明使用场景

### 🔧 代码贡献
1. Fork 本仓库
2. 创建功能分支
3. 提交代码更改
4. 发起 Pull Request

### 🧩 新增 API 供应商

开发者请参考：[新增 API 供应商（适配器开发指南）](docs/%E6%96%B0%E5%A2%9EAPI%E4%BE%9B%E5%BA%94%E5%95%86.md)

## 📁 项目结构

```
astrbot_plugin_gemini_image_generation/
├── main.py                 # 插件主入口（业务流程编排）
├── metadata.yaml           # 插件元数据
├── _conf_schema.json       # 配置 Schema
├── requirements.txt        # 依赖列表
├── README.md               # 说明文档
├── LICENSE                 # MIT 许可证
├── docs/
│   └── 新增API供应商.md    # 适配器开发指南
├── templates/              # 帮助页面模板
│   ├── help_template.md
│   ├── help_template_dark.html
│   └── help_template_light.html
└── tl/                     # 核心模块
    ├── __init__.py
    ├── api_types.py        # API 类型定义
    ├── avatar_handler.py   # 头像获取和管理
    ├── enhanced_prompts.py # 提示词增强
    ├── help_renderer.py    # 帮助页渲染
    ├── image_generator.py  # 图像生成核心逻辑
    ├── image_handler.py    # 图像处理、过滤、下载
    ├── image_splitter.py   # 图像切分（SmartMemeSplitter）
    ├── llm_tools.py        # LLM 工具定义
    ├── message_sender.py   # 消息格式化和发送
    ├── plugin_config.py    # 配置加载和管理
    ├── rate_limiter.py     # 限流和群限制
    ├── sticker_cutter.py   # 主体+附件吸附分割
    ├── tl_api.py           # API 客户端
    ├── tl_utils.py         # 工具函数
    ├── vision_handler.py   # 视觉 LLM 操作
    └── api/                # API 供应商适配器
        ├── __init__.py
        ├── base.py         # 基类
        ├── google.py       # Google/Gemini 官方
        ├── grok2api.py     # grok2api 适配
        ├── openai_compat.py # OpenAI 兼容
        ├── registry.py     # 供应商注册表
        └── zai.py          # Zai.is 适配
```

## 📄 许可证

本项目采用 MIT 许可证 - 详见 [LICENSE](./LICENSE) 文件

## 🙏 致谢

- [AstrBot](https://docs.astrbot.app/) - 强大的机器人框架
- [Google Gemini API](https://ai.google.dev/) - 强大的多模态 AI
- [NapCat](https://napneko.github.io/) - OneBot v11 实现

**特别感谢**:

- [@MliKiowa](https://github.com/MliKiowa)：图像切割算法提供者，为插件的智能表情包切分功能提供重要算法支持
- [@exynos967](https://github.com/exynos967)：多个重要功能和修复
  - [PR #1](https://github.com/piexian/astrbot_plugin_gemini_image_generation/pull/1)：限制/限流设置和手办化功能
  - [PR #2](https://github.com/piexian/astrbot_plugin_gemini_image_generation/pull/2)：兼容 OpenAI/Gemini 混合 URL 响应格式
  - [PR #3](https://github.com/piexian/astrbot_plugin_gemini_image_generation/pull/3)：兼容 OpenAI 传入参数
  - [PR #4](https://github.com/piexian/astrbot_plugin_gemini_image_generation/pull/4)：手办化命令使用专用提示词
- [@zouyonghe](https://github.com/zouyonghe)：新增代理支持、可选固定尺寸
  - [PR #5](https://github.com/piexian/astrbot_plugin_gemini_image_generation/pull/5)：为 Gemini API 添加代理支持
  - [PR #6](https://github.com/piexian/astrbot_plugin_gemini_image_generation/pull/6)：增加保留参考图尺寸开关
- [@vmoranv](https://github.com/vmoranv)：优化表情包提示词
  - [PR #11](https://github.com/piexian/astrbot_plugin_gemini_image_generation/pull/11)：优化表情包提示词
- [@itismygo](https://github.com/itismygo)：新增 grok2api 的 OpenAI 兼容适配
  - [PR #32](https://github.com/piexian/astrbot_plugin_gemini_image_generation/pull/32)：支持 grok2api 的 OpenAI 兼容格式
- [@Clhikari](https://github.com/Clhikari)：修复快速生图报错
  - [PR #37](https://github.com/piexian/astrbot_plugin_gemini_image_generation/pull/37)：修复快速生图报错
- [@雪語](https://github.com/YukiRa1n)：修复多个问题，添加 GIF 格式支持
  - [PR #39](https://github.com/piexian/astrbot_plugin_gemini_image_generation/pull/39)：清理无效参数和调试日志
  - [PR #40](https://github.com/piexian/astrbot_plugin_gemini_image_generation/pull/40)：添加 GIF 图片格式支持
  - [PR #41](https://github.com/piexian/astrbot_plugin_gemini_image_generation/pull/41)：修复 base64 图片重复保存问题
  - [PR #45](https://github.com/piexian/astrbot_plugin_gemini_image_generation/pull/45)：改进参考图片处理日志，增加失败检测

## 🔗 相关链接

- **项目地址**: [GitHub Repository](https://github.com/piexian/astrbot_plugin_gemini_image_generation)
- **问题反馈**: [Issues](https://github.com/piexian/astrbot_plugin_gemini_image_generation/issues)
- **AstrBot 文档**: [docs.astrbot.app](https://docs.astrbot.app)
- [grok2api](https://github.com/chenyme/grok2api)
- [zaiis2api](https://github.com/Futureppo/zaiis2api) 
- [zaiis](https://zai.is) 
---

<div align="center">

**如果这个插件对你有帮助，请给个 ⭐ Star 支持一下！**

</div>
