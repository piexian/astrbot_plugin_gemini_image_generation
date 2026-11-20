# StepFun 图像渠道归档

本分支保留阶跃星辰（StepFun）图像生成渠道的完整代码快照，仅供归档与历史参考。

## 下线事实

阶跃星辰于 **2026-10-10** 整体退出图像生成业务：

- 模型 `step-2x-large`、`step-image-edit-2`、`step-1x-edit`、`step-1x-medium` 全部下线。
- 接口 `/v1/images/generations`、`/v1/images/image2image`、`/v1/images/edits` 及 Step Plan 变体在国内、海外同步停止服务，继续调用无法正常返回结果。
- 当前模型 lineup（step-5-preview、step-3.7/3.5-flash、step-1o-turbo-vision、step-1o-audio）均为文本/推理/视觉理解/音频，无图像生成模型；Responses API 的「多模态图片」是图像理解（input_image → output_text），不是生图。无迁移目标。

官方公告：<https://platform.stepfun.com/docs/zh/announcements/image-model-retirement.md>

## 归档内容

- `tl/api/stepfun.py`：provider 实现（generations JSON + edits multipart）。
- `tl/provider_metadata.py`、`tl/provider_capabilities.py`、`tl/provider_hooks.py`、`tl/plugin_config.py`、`tl/web_studio_service.py` 中的 stepfun 注册与门控。
- `_conf_schema.json` 的 stepfun 模板、`docs/config.md` 的 stepfun_settings 章节。
- `tests/test_stepfun_provider.py` 及相关测试用例。

master 分支已移除该渠道；如需恢复，从本分支取回上述文件并按当时的注册方式接回。