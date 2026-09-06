"""Vertex AI（Agent Platform）Gemini 图像供应商实现。

认证二选一（一个条目只能配置一种、且各一个凭证，加载阶段强校验）：
- ``service_account_files``（AstrBot 配置文件上传字段，存插件根相对路径）：
  服务账号 JSON → RS256 JWT 换取 Bearer token（按凭证缓存，过期前刷新）。
- ``api_keys``：Vertex AI Express 模式 API Key（``x-goog-api-key`` 头）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger

from ..api_types import APIError, ApiRequestConfig
from .base import ProviderRequest
from .google import GoogleProvider

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_NAME = "astrbot_plugin_gemini_image_generation"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_TOKEN_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_TOKEN_REFRESH_MARGIN_SECONDS = 120

_GLOBAL_HOST = "aiplatform.googleapis.com"
_API_VERSION = "v1"

# 官方安全过滤支持代码 → 类别（docs: Gemini image generation and responsible AI）
_RAI_CODE_CATEGORIES = {
    "58061214": "儿童不当内容",
    "17301594": "儿童不当内容",
    "29310472": "名人肖像",
    "15236754": "名人肖像",
    "62263041": "危险内容",
    "57734940": "仇恨内容",
    "22137204": "仇恨内容",
    "74803281": "其他安全问题",
    "29578790": "其他安全问题",
    "42876398": "其他安全问题",
    "39322892": "人物/人脸",
    "92201652": "个人敏感信息",
    "89371032": "违规内容",
    "49114662": "违规内容",
    "72817394": "违规内容",
    "90789179": "性相关内容",
    "63429089": "性相关内容",
    "43188360": "性相关内容",
    "78610348": "毒性内容",
    "61493863": "暴力内容",
    "56562880": "暴力内容",
    "32635315": "低俗内容",
    "64151117": "名人或儿童相关违规",
}
_RAI_CODE_PATTERN = re.compile(r"\d{7,9}")
_BLOCKED_FINISH_REASONS = frozenset(
    {
        "SAFETY",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
        "PROHIBITED_CONTENT",
        "BLOCKLIST",
        "SPII",
    }
)
_SAFETY_ERROR_TYPES = frozenset({"no_image_retry", "invalid_response"})


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _describe_rai_codes(text: str) -> str:
    """把 raiFilteredReason 中的支持代码翻译为类别名。"""
    categories: list[str] = []
    codes: list[str] = []
    for code in _RAI_CODE_PATTERN.findall(text or ""):
        if code not in codes:
            codes.append(code)
        category = _RAI_CODE_CATEGORIES.get(code)
        if category and category not in categories:
            categories.append(category)
    if not codes:
        return ""
    detail = f"代码 {'、'.join(codes)}"
    if categories:
        detail = f"{'、'.join(categories)}，{detail}"
    return detail


class VertexProvider(GoogleProvider):
    name = "vertex"

    def __init__(self) -> None:
        # 凭证指纹 -> (access_token, 过期时间戳)
        self._token_cache: dict[str, tuple[str, float]] = {}
        self._token_lock = asyncio.Lock()
        self._sa_info_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def build_request(
        self,
        *,
        client: Any,
        config: ApiRequestConfig,
        is_retry: bool = False,
    ) -> ProviderRequest:  # noqa: ANN401
        settings = config.provider_settings or {}
        sa_files = self._service_account_files(settings)
        pasted_json = str(settings.get("service_account_json") or "").strip()
        configured_project = str(settings.get("project_id") or "").strip()
        location = str(settings.get("location") or "").strip() or "global"
        headers: dict[str, str] = {"Content-Type": "application/json"}

        credential = pasted_json or (sa_files[0] if sa_files else "")
        project = configured_project
        if credential:
            info, info_project = self._load_service_account(credential)
            token = await self._get_access_token(client, config, info)
            headers["Authorization"] = f"Bearer {token}"
            project = project or info_project
            if not project:
                raise APIError(
                    "Vertex 服务账号凭证未包含 project_id，且配置未填写 project_id，无法构造端点",
                    None,
                    "invalid_request",
                    retryable=False,
                )

        payload = await self._prepare_payload(client=client, config=config)
        url = self._build_url(
            api_base=(config.api_base or "").rstrip("/"),
            model=config.model,
            project=project,
            location=location,
            full_mode=bool(credential),
        )
        if not credential:
            headers["x-goog-api-key"] = config.api_key or ""
        return ProviderRequest(url=url, headers=headers, payload=payload)

    @staticmethod
    def _service_account_files(settings: dict[str, Any]) -> list[str]:
        files = settings.get("service_account_files")
        if not isinstance(files, list):
            return []
        return [str(item).strip() for item in files if str(item).strip()]

    def _load_service_account(self, credential: str) -> tuple[dict[str, Any], str]:
        """读取服务账号凭证，返回 (info, project_id)。

        凭证可以是内联 JSON 文本（``{`` 开头）或插件根相对/绝对文件路径；
        内联按内容指纹缓存，路径按 mtime 缓存。
        """
        stripped = credential.strip()
        if stripped.startswith("{"):
            fingerprint = (
                "inline:" + hashlib.sha256(stripped.encode("utf-8")).hexdigest()
            )
            cached = self._sa_info_cache.get(fingerprint)
            if cached:
                return cached[1], str(cached[1].get("project_id") or "")
            try:
                info = json.loads(stripped)
            except ValueError as exc:
                raise APIError(
                    "Vertex 服务账号凭证不是有效的 JSON",
                    None,
                    "invalid_request",
                    retryable=False,
                ) from exc
            if not isinstance(info, dict) or not info.get("private_key"):
                raise APIError(
                    "Vertex 服务账号凭证缺少 private_key 字段",
                    None,
                    "invalid_request",
                    retryable=False,
                )
            self._sa_info_cache[fingerprint] = (None, info)
            return info, str(info.get("project_id") or "")

        path = self._resolve_credential_path(credential)
        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            tried = "、".join(
                str(base / credential) for base in self._candidate_credential_bases()
            )
            raise APIError(
                f"Vertex 服务账号凭证文件不可读: {credential}（已尝试 {tried}）",
                None,
                "invalid_request",
                retryable=False,
            ) from exc
        cached = self._sa_info_cache.get(credential)
        if cached and cached[0] == mtime:
            return cached[1], str(cached[1].get("project_id") or "")
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise APIError(
                f"Vertex 服务账号凭证不是有效的 JSON: {credential}",
                None,
                "invalid_request",
                retryable=False,
            ) from exc
        if not isinstance(info, dict) or not info.get("private_key"):
            raise APIError(
                f"Vertex 服务账号凭证缺少 private_key 字段: {credential}",
                None,
                "invalid_request",
                retryable=False,
            )
        self._sa_info_cache[credential] = (mtime, info)
        return info, str(info.get("project_id") or "")

    @staticmethod
    def _candidate_credential_bases() -> list[Path]:
        """相对路径凭证的解析基准：官方上传目录优先，其次插件代码目录。"""
        bases: list[Path] = []
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            bases.append(Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME)
        except Exception:  # noqa: BLE001
            pass
        bases.append(PLUGIN_ROOT)
        return bases

    @classmethod
    def _resolve_credential_path(cls, path_value: str) -> Path:
        path = Path(path_value)
        if path.is_absolute():
            return path
        bases = cls._candidate_credential_bases()
        for base in bases:
            candidate = base / path
            if candidate.is_file():
                return candidate
        return bases[0] / path

    @staticmethod
    def _build_url(
        *,
        api_base: str,
        model: str,
        project: str,
        location: str,
        full_mode: bool,
    ) -> str:
        if api_base:
            root = api_base
            if not re.search(r"/v\d+(?:beta\d+)?$", root):
                root = f"{root}/{_API_VERSION}"
            return f"{root}/publishers/google/models/{model}:generateContent"
        if full_mode:
            host = (
                _GLOBAL_HOST
                if location in ("", "global")
                else f"{location}-{_GLOBAL_HOST}"
            )
            return (
                f"https://{host}/{_API_VERSION}/projects/{project}"
                f"/locations/{location}/publishers/google/models/{model}:generateContent"
            )
        return (
            f"https://{_GLOBAL_HOST}/{_API_VERSION}"
            f"/publishers/google/models/{model}:generateContent"
        )

    async def _get_access_token(
        self, client: Any, config: ApiRequestConfig, info: dict[str, Any]
    ) -> str:
        private_key = str(info.get("private_key") or "")
        client_email = str(info.get("client_email") or "")
        fingerprint = hashlib.sha256(
            f"{client_email}:{private_key}".encode()
        ).hexdigest()

        async with self._token_lock:
            cached = self._token_cache.get(fingerprint)
            now = time.time()
            if cached and now < cached[1] - _TOKEN_REFRESH_MARGIN_SECONDS:
                return cached[0]
            token, expires_in = await self._exchange_jwt(
                client, config, private_key, client_email
            )
            self._token_cache[fingerprint] = (
                token,
                now + max(int(expires_in), _TOKEN_REFRESH_MARGIN_SECONDS * 2),
            )
            return token

    async def _exchange_jwt(
        self,
        client: Any,
        config: ApiRequestConfig,
        private_key: str,
        client_email: str,
    ) -> tuple[str, int]:
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError as exc:
            raise APIError(
                "使用服务账号凭证需要安装 cryptography 依赖（pip install cryptography）",
                None,
                "invalid_request",
                retryable=False,
            ) from exc

        now = int(time.time())
        signing_input = ".".join(
            (
                _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode("utf-8")),
                _b64url(
                    json.dumps(
                        {
                            "iss": client_email,
                            "scope": _TOKEN_SCOPE,
                            "aud": _TOKEN_URL,
                            "iat": now,
                            "exp": now + 3600,
                        }
                    ).encode("utf-8")
                ),
            )
        )
        try:
            key = serialization.load_pem_private_key(
                private_key.encode("utf-8"), password=None
            )
            signature = key.sign(
                signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
            )
        except Exception as exc:
            raise APIError(
                f"Vertex 服务账号私钥解析或签名失败: {exc}",
                None,
                "invalid_request",
                retryable=False,
            ) from exc
        assertion = f"{signing_input}.{_b64url(signature)}"

        session = await client._get_session(getattr(config, "proxy", None))
        proxy = client._request_http_proxy(config)
        try:
            async with session.post(
                _TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                body = await response.text()
                if response.status != 200:
                    logger.warning(
                        f"[vertex] 访问令牌获取失败: HTTP {response.status} {body[:300]}"
                    )
                    raise APIError(
                        f"Vertex 访问令牌获取失败（HTTP {response.status}），"
                        "请检查服务账号凭证与网络",
                        response.status,
                        "auth",
                        retryable=True,
                    )
                payload = json.loads(body)
        except APIError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning(f"[vertex] 访问令牌请求异常: {exc}")
            raise APIError(
                f"Vertex 访问令牌请求失败: {exc}",
                None,
                "auth",
                retryable=True,
            ) from None
        token = str(payload.get("access_token") or "")
        if not token:
            raise APIError(
                "Vertex 访问令牌响应缺少 access_token",
                None,
                "auth",
                retryable=True,
            )
        try:
            expires_in = int(payload.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        return token, expires_in

    async def _prepare_payload(
        self, *, client: Any, config: ApiRequestConfig
    ) -> dict[str, Any]:  # noqa: ANN401
        payload = await super()._prepare_payload(client=client, config=config)
        person_generation = str(
            (config.provider_settings or {}).get("person_generation") or ""
        ).strip()
        if person_generation:
            generation_config = payload.setdefault("generationConfig", {})
            image_config = generation_config.setdefault("imageConfig", {})
            image_config["personGeneration"] = person_generation
        return payload

    async def parse_response(
        self,
        *,
        client: Any,
        response_data: dict[str, Any],
        session: aiohttp.ClientSession,
        api_base: str | None = None,
        http_status: int | None = None,
        is_retry: bool = False,
        request_config: ApiRequestConfig | None = None,
    ) -> tuple[list[str], list[str], str | None, str | None]:  # noqa: ANN401
        if not isinstance(response_data, dict):
            raise APIError(
                "Vertex API 返回了非预期格式的响应。", http_status, "invalid_response"
            )
        if http_status is not None and http_status != 200:
            raise self._error_from_http(response_data, http_status)

        blocked = self._detect_safety_block(response_data)
        if blocked and self._prompt_blocked(response_data):
            raise self._safety_error(blocked)

        try:
            return await self._parse_gresponse(
                client=client,
                response_data=response_data,
                session=session,
                request_config=request_config,
            )
        except APIError as exc:
            # 有安全拦截痕迹时，把「只返回文本/格式异常」类失败归因为安全拦截
            if blocked and exc.error_type in _SAFETY_ERROR_TYPES:
                raise self._safety_error(blocked) from exc
            raise

    @staticmethod
    def _prompt_blocked(response_data: dict[str, Any]) -> bool:
        feedback = response_data.get("promptFeedback")
        return isinstance(feedback, dict) and bool(feedback.get("blockReason"))

    def _detect_safety_block(self, response_data: dict[str, Any]) -> str | None:
        feedback = response_data.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            detail = _describe_rai_codes(str(feedback.get("raiFilteredReason") or ""))
            suffix = f"，{detail}" if detail else ""
            return f"提示词被安全过滤拦截（{feedback.get('blockReason')}{suffix}）"

        reasons: list[str] = []
        rai_texts: list[str] = []
        for candidate in response_data.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            finish_reason = str(candidate.get("finishReason") or "")
            if finish_reason in _BLOCKED_FINISH_REASONS:
                reasons.append(finish_reason)
            rai = str(candidate.get("raiFilteredReason") or "")
            if rai:
                rai_texts.append(rai)
        if reasons:
            detail = _describe_rai_codes(" ".join(rai_texts))
            suffix = f"，{detail}" if detail else ""
            unique = "、".join(dict.fromkeys(reasons))
            return f"生成结果被安全过滤拦截（{unique}{suffix}）"
        return None

    def _safety_error(self, description: str) -> APIError:
        logger.warning(f"[vertex] {description}")
        return APIError(
            f"Vertex 内容安全过滤未通过：{description}",
            None,
            "safety",
            retryable=False,
        )

    @staticmethod
    def _collect_rai_reasons(data: Any, found: list[str] | None = None) -> list[str]:
        """只收集 raiFilteredReason 字段：响应体里的项目号/配额号等
        长数字绝不能被当成安全代码。"""
        found = [] if found is None else found
        if isinstance(data, dict):
            rai = data.get("raiFilteredReason")
            if isinstance(rai, str) and rai:
                found.append(rai)
            for value in data.values():
                VertexProvider._collect_rai_reasons(value, found)
        elif isinstance(data, list):
            for item in data:
                VertexProvider._collect_rai_reasons(item, found)
        return found

    def _error_from_http(
        self, response_data: dict[str, Any], http_status: int
    ) -> APIError:
        error = response_data.get("error")
        message = (
            str(error.get("message"))
            if isinstance(error, dict) and error.get("message")
            else f"HTTP {http_status}"
        )
        status_name = str(error.get("status")) if isinstance(error, dict) else ""
        logger.warning(f"[vertex] API 错误: HTTP {http_status} {message[:300]}")

        rai_detail = _describe_rai_codes(
            " ".join(self._collect_rai_reasons(response_data))
        )
        if http_status == 429 or status_name == "RESOURCE_EXHAUSTED":
            return APIError(
                f"Vertex 配额或频率受限: {message}",
                http_status,
                "quota",
                retryable=True,
            )
        if http_status in (401, 403) or status_name in {
            "UNAUTHENTICATED",
            "PERMISSION_DENIED",
        }:
            return APIError(
                f"Vertex 认证或权限失败: {message}",
                http_status,
                "auth",
                retryable=False,
            )
        if http_status == 404 or status_name == "NOT_FOUND":
            # 模型名/区域错误不会自愈，重试无意义
            return APIError(
                f"Vertex 模型或端点不存在: {message}",
                http_status,
                "not_found",
                retryable=False,
            )
        if rai_detail:
            return self._safety_error(f"请求被安全过滤拦截（{rai_detail}）")
        return APIError(f"Vertex API 错误: {message}", http_status)
