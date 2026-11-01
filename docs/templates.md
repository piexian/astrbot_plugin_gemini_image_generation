# 提示词模板库

`/快速` 的提示词来自模板库。内置模板可以覆盖后恢复，本地模板存在插件数据目录的 `prompt_templates.json`。

## 字段

| 字段 | 规则 |
| --- | --- |
| `id` | 1–48 位小写字母、数字、下划线或连字符 |
| `name` | 1–40 字，单行 |
| `ai_description` | 最多 80 字，单行，可空 |
| `content` | 1–8000 字；控制字符只允许换行、回车和制表符 |
| `defaults` | 只允许 `resolution`（如 `2K`）和 `aspect_ratio`（如 `16:9`） |
| `reference` | `none`、`optional` 或 `required` |
| `tags` | 最多 10 个，每个 1–20 字，去重保序 |
| `author` | 可选，最多 32 字 |
| `source_url` | 可选，https 域名链接，最多 300 字，不带账号口令 |
| `ai_enabled` | 是否允许后续给模型使用，默认开启 |
| `origin` | `builtin`、`local` 或 `remote` |

`nsfw` 为真的条目会被拒绝。名称、说明、作者和标签会去掉控制字符。

占位符只有 `{{input}}`、`{{rows}}` 和 `{{cols}}`。正文里没有 `{{input}}` 时，非空输入追加在末尾，前缀为“用户需求：”；输入里的占位符不会再展开。`rows` 和 `cols` 取表情包网格配置。

## 内置模板

| id | 名称 | 默认分辨率 | 默认比例 | 参考图 |
| --- | --- | --- | --- | --- |
| `avatar` | 头像 | 1K | 1:1 | 可选 |
| `poster` | 海报 | 2K | 16:9 | 可选 |
| `wallpaper` | 壁纸 | 4K | 16:9 | 可选 |
| `card` | 卡片 | 1K | 3:2 | 可选 |
| `mobile` | 手机壁纸 | 2K | 9:16 | 可选 |
| `figure-pvc` | 手办化（PVC） | 2K | 3:2 | 可选 |
| `figure-gk` | 手办化（树脂 GK） | 2K | 3:2 | 可选 |
| `sticker` | 表情包（中文 Q 版） | 4K | 16:9 | 必需 |
| `sticker-simple` | 表情包（英文 Q 版） | 4K | 16:9 | 必需 |

`/快速` 的尺寸优先级是：该内置模板的覆盖、`quick_mode_settings`、上表默认值。表情包输入以“简单”开头时使用 `sticker-simple`。

## 存储

文件为 `{"schema_version": 1, "templates": [], "builtin_overrides": {}}`。内置正文留在代码里，文件只保存覆盖；恢复默认就是删掉对应覆盖。本地模板最多 1000 条，文件最大 8 MB。

文件损坏时会改名为 `prompt_templates.json.corrupt-<时间戳>`，并继续使用内置模板。版本比当前程序新时不改文件，只读使用内置模板。单条坏记录会被跳过。

## JSON 包

```json
{
  "format": "gemini-image-templates",
  "version": 1,
  "templates": [
    {
      "id": "poster-cat",
      "name": "猫咪海报",
      "content": "一张猫咪海报。\n用户需求：{{input}}",
      "defaults": {"resolution": "2K", "aspect_ratio": "16:9"},
      "reference": "none",
      "created_at": "2026-01-01T00:00:00Z",
      "updated_at": "2026-01-01T00:00:00Z"
    }
  ]
}
```

包最多 500 条。未知字段以及 `preview`、`ai_enabled`、`origin` 会被忽略。

## WebUI 广场

Studio 的「提示词广场」页签管理本地模板，接口沿用工作台的鉴权与 CSRF 守卫：

| 接口 | 说明 |
| --- | --- |
| `GET /templates` | 列出内置与本地模板 |
| `POST /templates` | `create` / `update` / `delete` / `restore` / `set_ai_enabled` |
| `POST /templates/import` | 导入包，默认重新分配本地 id（`keep_ids` 保留包内 id） |
| `GET /templates/export` | 导出包 |
| `POST /templates/asset` | 把画廊图片复制为模板预览图 |
| `GET /template_asset/<name>` | 读取预览图 |

字段校验与本地存储共用同一套规则，非法输入按字段返回中文原因。预览图存插件数据目录 `template_assets/`，不参与画廊容量清理；删除模板时一并清理。
