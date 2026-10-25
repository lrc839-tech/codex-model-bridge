"""Small, dependency-free client for OpenAI-compatible Chat Completions.

Only the user-selected URL receives the API key. Redirects are rejected, TLS
verification remains enabled, and upstream error bodies never enter exceptions.
The caller must preserve the complete assistant message when replaying tools;
in particular reasoning_content and encrypted_content can be protocol state.
"""

from __future__ import annotations

import ipaddress
import json
import math
import socket
import ssl
from collections.abc import Mapping
from typing import Any
from urllib import error, parse, request


MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class ProviderError(Exception):
    """Safe public error with a local code and optional upstream HTTP status."""

    def __init__(self, message: str, *, code: str = "provider_error",
                 status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self),
                "status_code": self.status_code, "retryable": self.retryable}


def _is_loopback(host: str) -> bool:
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_base_url(value: str) -> str:
    """Accept a service base or full /chat/completions URL; reject unsafe URLs.

    An omitted scheme means HTTPS. An origin without a path uses /v1. Other
    paths (e.g. /compatible-mode/v1 and /api/paas/v4) are retained verbatim.
    Plain HTTP is limited to explicit loopback hosts for local model servers.
    """
    if not isinstance(value, str) or not value.strip():
        raise ProviderError("请填写 API 链接。", code="invalid_url")
    value = value.strip()
    if any(ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value:
        raise ProviderError("API 链接不能包含空白或控制字符。", code="invalid_url")
    if "://" not in value:
        value = "https://" + value
    try:
        parts = parse.urlsplit(value)
        host, port = parts.hostname, parts.port
    except (ValueError, UnicodeError):
        raise ProviderError("API 链接格式不正确。", code="invalid_url") from None
    if (parts.scheme not in {"https", "http"} or not host or
            parts.username is not None or parts.password is not None or
            parts.query or parts.fragment):
        raise ProviderError("请填写 HTTPS API 链接，不要附带账号、密钥、查询参数或锚点。",
                            code="invalid_url")
    if parts.scheme == "http" and not _is_loopback(host):
        raise ProviderError("远程 API 必须使用 HTTPS；HTTP 仅允许本机回环地址。",
                            code="insecure_url")
    if port is not None and port < 1:
        raise ProviderError("API 端口不正确。", code="invalid_url")
    path = parts.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[:-len("/chat/completions")]
    elif path.endswith("/models"):
        path = path[:-len("/models")]
    if path.endswith("/responses") or path.endswith("/messages"):
        raise ProviderError("请使用 Chat Completions 兼容 API 的基础链接。",
                            code="wrong_protocol")
    path = path or "/v1"
    hostname = "[" + host + "]" if ":" in host else host
    netloc = hostname + (":" + str(port) if port is not None else "")
    return parse.urlunsplit((parts.scheme, netloc, path, "", ""))


def provider_name(base_url: str) -> str:
    """Best-effort recognition for known, official hosts; custom hosts stay generic."""
    host = (parse.urlsplit(base_url).hostname or "").lower()
    if host == "api.deepseek.com":
        return "deepseek"
    if host in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com",
                "dashscope-us.aliyuncs.com", "coding.dashscope.aliyuncs.com"} or host.endswith(".maas.aliyuncs.com"):
        return "qwen"
    if host.endswith(".volces.com") and host.startswith("ark."):
        return "doubao"
    if host in {"api.moonshot.cn", "api.moonshot.ai", "api.kimi.com", "api.kimi.ai"}:
        return "kimi"
    if host in {"open.bigmodel.cn", "api.z.ai"}:
        return "zhipu"
    return "custom"


def execution_parameters(base_url: str, model: str) -> dict[str, Any]:
    """Conservative execution-mode hints for documented, known model families.

    Unknown models retain their service defaults instead of inheriting options
    that could make an otherwise compatible model fail. These are HTTP body
    fields, not the SDK-only `extra_body` wrapper. No temperature is forced.
    """
    provider = provider_name(base_url)
    model = model.lower()
    if (provider == "qwen" and
            model.startswith(("qwen3", "qwen-plus", "qwen-flash")) and
            "-thinking" not in model and "-coder" not in model):
        return {"enable_thinking": False}
    if provider == "deepseek" and model.startswith("deepseek-"):
        return {"thinking": {"type": "disabled"}}
    if provider == "doubao" and model.startswith(("doubao-seed-", "ep-")):
        return {"thinking": {"type": "disabled"}}
    if provider == "kimi" and model.startswith(("kimi-k2.5", "kimi-k2.6")):
        return {"thinking": {"type": "disabled"}}
    if provider == "kimi" and model.startswith("kimi-k3"):
        return {"reasoning_effort": "low"}
    if provider == "zhipu":
        if model.startswith("glm-5.3"):
            return {"reasoning_effort": "low"}
        if model.startswith(("glm-4.5", "glm-4.6", "glm-4.7", "glm-5")):
            return {"thinking": {"type": "disabled"}}
    return {}


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib otherwise forwards Authorization across a redirect. A user
        # must explicitly choose the final endpoint rather than leak a key.
        return None


def _http_error(status: int) -> ProviderError:
    if status in {301, 302, 303, 307, 308}:
        message = "API 返回重定向；请改填供应商的最终 API 链接。"
    elif status == 400:
        message = "请求参数不被此模型接受，请检查模型名称、工具调用与 JSON 模式支持情况。"
    elif status == 401:
        message = "API Key 无效或已过期，请重新配置。"
    elif status == 402:
        message = "供应商账户余额不足或计费尚未开通。"
    elif status == 403:
        message = "API 访问被拒绝，请检查密钥权限、区域和模型开通状态。"
    elif status == 404:
        message = "API 或模型不存在，请检查基础链接和模型名称。"
    elif status == 429:
        message = "供应商限流或配额不足，请稍后重试并检查账户额度。"
    elif status >= 500:
        message = "供应商服务暂时异常，请稍后重试。"
    else:
        message = "供应商拒绝了 API 请求，请检查配置。"
    return ProviderError("HTTP " + str(status) + "：" + message,
                         code="http_error", status_code=status,
                         retryable=status in {408, 429} or status >= 500)


class ProviderClient:
    """Three-field profile client. No automatic retries or paid fallbacks."""

    def __init__(self, profile: Mapping[str, Any]):
        if not isinstance(profile, Mapping):
            raise ProviderError("模型配置格式不正确。", code="invalid_profile")
        self.base_url = normalize_base_url(profile.get("base_url", ""))
        key, model = profile.get("api_key"), profile.get("model")
        if not isinstance(key, str) or not key.strip():
            raise ProviderError("请填写 API Key。", code="missing_key")
        if any(ord(c) < 33 or ord(c) > 126 for c in key.strip()):
            raise ProviderError("API Key 格式不正确，请重新粘贴。", code="invalid_key")
        if not isinstance(model, str) or not model.strip():
            raise ProviderError("请填写或选择模型名称。", code="missing_model")
        if len(model) > 512 or any(ord(c) < 32 for c in model):
            raise ProviderError("模型名称格式不正确。", code="invalid_model")
        self._api_key = key.strip()
        self.model = model.strip()
        self.provider = provider_name(self.base_url)
        self._opener = request.build_opener(_NoRedirect())

    def __repr__(self) -> str:
        return "ProviderClient(provider=" + repr(self.provider) + ", model=" + repr(self.model) + ")"

    def _request(self, suffix: str, payload: dict[str, Any] | None,
                 timeout: float) -> dict[str, Any]:
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
                not math.isfinite(timeout) or not 0 < timeout <= 3600):
            raise ProviderError("请求超时必须在 0 到 3600 秒之间。", code="invalid_timeout")
        try:
            body = None if payload is None else json.dumps(
                payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise ProviderError("API 请求包含不能序列化的内容。", code="invalid_request") from None
        req = request.Request(
            self.base_url + suffix, data=body,
            headers={"Authorization": "Bearer " + self._api_key,
                     "Accept": "application/json", "Content-Type": "application/json",
                     "User-Agent": "codex-model-bridge/0.1"},
            method="GET" if body is None else "POST")
        try:
            with self._opener.open(req, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except error.HTTPError as exc:
            # Close without reflecting (or persisting) an untrusted error body.
            status = exc.code
            exc.close()
            raise _http_error(status) from None
        except (TimeoutError, socket.timeout):
            raise ProviderError("模型请求超时。未自动重试，以避免重复计费。",
                                code="timeout", retryable=True) from None
        except error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise ProviderError("模型请求超时。未自动重试，以避免重复计费。",
                                    code="timeout", retryable=True) from None
            if isinstance(exc.reason, ssl.SSLError):
                raise ProviderError("API 的 HTTPS 证书验证失败，请检查链接或本机证书。",
                                    code="tls_error") from None
            raise ProviderError("无法连接模型 API，请检查网络、代理和 API 链接。",
                                code="network_error", retryable=True) from None
        except (OSError, ValueError):
            raise ProviderError("模型 API 连接中断，请检查网络和 API 链接。",
                                code="network_error", retryable=True) from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError("模型响应过大，请缩小任务后重试。", code="response_too_large")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ProviderError("API 未返回合法 JSON，请确认填写的是 API 链接。",
                                code="invalid_response") from None
        if not isinstance(result, dict) or "error" in result:
            raise ProviderError("API 返回异常响应，请检查供应商的模型权限与请求配置。",
                                code="invalid_response")
        return result

    def fetch_models(self, timeout: float = 15) -> list[str]:
        """Return IDs; [] means discovery unsupported/empty, manual IDs still work."""
        try:
            result = self._request("/models", None, timeout)
        except ProviderError as exc:
            if exc.status_code in {404, 405, 501}:
                return []
            raise
        data = result.get("data")
        if not isinstance(data, list):
            raise ProviderError("模型列表格式不兼容，请手动填写模型名称。",
                                code="invalid_models_response")
        return sorted({row["id"] for row in data
                       if isinstance(row, dict) and isinstance(row.get("id"), str)
                       and row["id"].strip()})

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
             json_output: bool = False, timeout: float = 120) -> dict[str, Any]:
        if not isinstance(messages, list) or not messages or not all(
                isinstance(message, dict) for message in messages):
            raise ProviderError("对话消息不能为空，且每条消息必须是对象。", code="invalid_messages")
        if tools is not None and not isinstance(tools, list):
            raise ProviderError("工具定义必须是列表。", code="invalid_tools")
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "stream": False}
        payload.update(execution_parameters(self.base_url, self.model))
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if json_output:
            payload["response_format"] = {"type": "json_object"}
        result = self._request("/chat/completions", payload, timeout)
        choices = result.get("choices")
        if (not isinstance(choices, list) or not choices or
                not isinstance(choices[0], dict) or
                not isinstance(choices[0].get("message"), dict)):
            raise ProviderError("模型未返回有效对话结果。", code="invalid_chat_response")
        # Return the original object: tool-call reasoning must round-trip intact.
        return result
