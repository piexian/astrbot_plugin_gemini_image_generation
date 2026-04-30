# H2 — Provider settings 绑定 dict 化

> 状态: 待实现
> 预估收益: -40 行,新增 provider 只改一个 map

## 问题

`main.py` `_load_provider_from_context` 内部约 L432-L490,对 6 个 provider 的 `*_settings` 绑定到 `self.cfg` 与 `self.api_client` 各重复一遍 if/elif 链,共 ~60 行高度雷同代码。

新增 provider 时需要在两处 if/elif 中各加一个分支。

## 方案

1. 在 `main.py` 顶部(import 区域之后)定义常量:
   ```python
   PROVIDER_SETTINGS_ATTR_MAP: dict[str, str] = {
       "doubao": "doubao_settings",
       "minimax": "minimax_settings",
       "stepfun": "stepfun_settings",
       "sensenova": "sensenova_settings",
       "xai": "xai_settings",
       "openai_images": "openai_images_settings",
   }
   ```
2. 阶段一(cfg 绑定 L430-L445)替换为:
   ```python
   attr = PROVIDER_SETTINGS_ATTR_MAP.get(api_type_norm)
   if attr:
       setattr(self.cfg, attr, override_settings)
   ```
3. 阶段二(api_client 绑定 L451-L490)替换为单循环:
   ```python
   attr = PROVIDER_SETTINGS_ATTR_MAP.get(api_type_norm)
   if attr:
       try:
           setattr(
               self.api_client, attr,
               getattr(self.cfg, attr, None) or {},
           )
       except Exception as e:
           logger.debug(f"绑定 {attr} 到 API client 失败: {e}")
   ```

## 不破坏

- 行为完全一致(同样的 attr 名、同样的 try/except)
- 新增 provider 只需在 map 加一行

## 验证

1. `ruff format .` + `ruff check .`(按 CI 规则)
2. `python -m compileall main.py`
3. 人工 diff 阶段一/二的等价性
