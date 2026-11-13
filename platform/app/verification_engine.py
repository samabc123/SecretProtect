"""最小授权凭据验证引擎。

验证与检测分离，只在后端显式开启联网并把候选指纹加入授权白名单后运行。
请求只访问固定的身份确认端点，不跟随重定向、不保存响应体、不执行资源枚举。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SUPPORTED_VERIFIERS: dict[str, str] = {
    "github_pat": "github-pat",
    "gitee_token": "gitee-token",
    "gitlab_pat": "gitlab-pat",
    "dockerhub_token": "dockerhub-pat",
    "slack_token": "slack-auth-test",
    "telegram_bot_token": "telegram-getme",
    "npm_token": "npm-whoami",
    "huggingface_token": "huggingface-whoami",
    "aws_access_key_id": "aws-sts-getcalleridentity",
    "aws_secret_access_key": "aws-sts-getcalleridentity",
    "twilio_account_sid": "twilio-pair",
    "twilio_auth_token": "twilio-pair",
    "openai_api_key": "openai-models",
    "anthropic_api_key": "anthropic-models",
    "stripe_live_key": "stripe-account",
    "stripe_test_key": "stripe-account",
    "digitalocean_token": "digitalocean-account",
    "notion_api_key": "notion-user",
    "linear_api_key": "linear-viewer",
    "pulumi_access_token": "pulumi-user",
    "postman_api_key": "postman-user",
    "datadog_api_key": "datadog-validate",
    "cloudflare_api_token": "cloudflare-token-verify",
    "vercel_token": "vercel-user",
    "replicate_api_token": "replicate-account",
    "groq_api_key": "groq-models",
    "mistral_api_key": "mistral-models",
    "cohere_api_key": "cohere-check",
}

CONTROLLED_VERIFIABLE_TYPES = {
    "AK/SK",
    "API Key",
    "API Token",
    "OAuth Client Secret",
    "数据库密码",
    "SSH 私钥",
    "操作系统账号密码",
    "数据库连接串",
}

AUTHORIZED_ENDPOINT_VERIFIER_PREFIX = "authorized-endpoint:"
_ENDPOINT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_JSON_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_AUTH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_AUTH_PLACEMENTS = {"header", "query", "json_body"}
_AUTH_SCHEMES = {"raw", "bearer", "basic"}


def _normalized_strings(value: Any, *, field: str, limit: int = 100) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > limit:
        raise ValueError(f"{field}_invalid")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field}_invalid")
        normalized = item.strip()
        if not normalized or len(normalized) > 160:
            raise ValueError(f"{field}_invalid")
        if normalized not in result:
            result.append(normalized)
    if not result:
        raise ValueError(f"{field}_invalid")
    return result


def _normalized_json_path(value: Any, *, field: str, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _JSON_PATH_RE.fullmatch(value.strip()):
        raise ValueError(f"{field}_invalid")
    return value.strip()


def _normalized_http_statuses(value: Any, *, field: str) -> list[int] | None:
    if value is None:
        return None
    values = value if isinstance(value, list) else [value]
    if not values or len(values) > 20:
        raise ValueError(f"{field}_invalid")
    result: list[int] = []
    for item in values:
        if isinstance(item, bool) or not isinstance(item, int) or not 100 <= item <= 599:
            raise ValueError(f"{field}_invalid")
        if item not in result:
            result.append(item)
    return result


def _normalize_condition(value: Any, *, field: str, require_json: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field}_invalid")
    allowed = {"http_status", "status_codes", "json_path", "equals", "exists"}
    if set(value) - allowed:
        raise ValueError(f"{field}_invalid")
    statuses = _normalized_http_statuses(
        value.get("http_status", value.get("status_codes")),
        field=field,
    )
    json_path = _normalized_json_path(value.get("json_path"), field=field)
    has_equals = "equals" in value
    has_exists = "exists" in value
    if has_equals and has_exists:
        raise ValueError(f"{field}_invalid")
    if has_exists and not isinstance(value["exists"], bool):
        raise ValueError(f"{field}_invalid")
    if json_path and not (has_equals or has_exists):
        raise ValueError(f"{field}_invalid")
    if require_json and not json_path:
        raise ValueError(f"{field}_requires_json_path")
    if not statuses and not json_path:
        raise ValueError(f"{field}_invalid")
    normalized: dict[str, Any] = {}
    if statuses:
        normalized["http_status"] = statuses
    if json_path:
        normalized["json_path"] = json_path
    if has_equals:
        equals = value["equals"]
        if isinstance(equals, (dict, list)):
            raise ValueError(f"{field}_invalid")
        normalized["equals"] = equals
    if has_exists:
        normalized["exists"] = value["exists"]
    return normalized


def normalize_authorized_endpoint_specs(value: Any) -> tuple[dict[str, Any], ...]:
    """Validate explicit generic identity endpoint configuration."""
    if value in (None, ""):
        return ()
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("authorized_verification_endpoints_invalid")
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("authorized_verification_endpoint_invalid")
        endpoint_id = str(raw.get("endpoint_id") or "").strip()
        if not _ENDPOINT_ID_RE.fullmatch(endpoint_id) or endpoint_id in seen_ids:
            raise ValueError("authorized_verification_endpoint_id_invalid")
        system_name = str(raw.get("system_name") or "").strip()
        platform = str(raw.get("platform") or "").strip()
        if not system_name or len(system_name) > 120 or not platform or len(platform) > 120:
            raise ValueError("authorized_verification_endpoint_identity_invalid")
        supported_types = _normalized_strings(
            raw.get("supported_types"),
            field="authorized_verification_supported_types",
        )
        endpoint_url = str(raw.get("endpoint_url") or "").strip()
        parsed = urllib.parse.urlparse(endpoint_url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme not in {"https", "http"}
            or not host
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.query
            or len(endpoint_url) > 2048
        ):
            raise ValueError("authorized_verification_endpoint_url_invalid")
        if parsed.scheme == "http" and host not in _LOCAL_HOSTS:
            raise ValueError("authorized_verification_endpoint_https_required")
        allowed_hosts = _normalized_strings(
            raw.get("allowed_hosts"),
            field="authorized_verification_allowed_hosts",
        )
        allowed_hosts = [item.lower() for item in allowed_hosts]
        if host not in allowed_hosts:
            raise ValueError("authorized_verification_endpoint_host_not_allowlisted")
        method = str(raw.get("method") or "GET").strip().upper()
        if method not in {"GET", "POST"}:
            raise ValueError("authorized_verification_method_invalid")
        auth_placement = str(raw.get("auth_placement") or "header").strip().lower()
        if auth_placement not in _AUTH_PLACEMENTS:
            raise ValueError("authorized_verification_auth_placement_invalid")
        auth_name = str(raw.get("auth_name") or "Authorization").strip()
        if not _AUTH_NAME_RE.fullmatch(auth_name):
            raise ValueError("authorized_verification_auth_name_invalid")
        auth_scheme = str(raw.get("auth_scheme") or "bearer").strip().lower()
        if auth_scheme not in _AUTH_SCHEMES:
            raise ValueError("authorized_verification_auth_scheme_invalid")
        if auth_placement != "header" and auth_scheme == "basic":
            raise ValueError("authorized_verification_auth_scheme_invalid")
        success_condition = _normalize_condition(
            raw.get("success_condition"),
            field="authorized_verification_success_condition",
            require_json=True,
        )
        failure_condition = _normalize_condition(
            raw.get("failure_condition"),
            field="authorized_verification_failure_condition",
        )
        identity_fields = raw.get("identity_fields", [])
        if identity_fields in (None, ""):
            identity_fields = []
        elif not isinstance(identity_fields, list) or len(identity_fields) > 20:
            raise ValueError("authorized_verification_identity_fields_invalid")
        else:
            identity_fields = [
                _normalized_json_path(item, field="authorized_verification_identity_fields", required=True)
                for item in identity_fields
            ]
        timeout_value = raw.get("timeout_seconds", 8)
        if isinstance(timeout_value, bool) or not isinstance(timeout_value, (int, float)):
            raise ValueError("authorized_verification_timeout_invalid")
        timeout_seconds = float(timeout_value)
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("authorized_verification_timeout_invalid")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("authorized_verification_enabled_invalid")
        result.append({
            "endpoint_id": endpoint_id,
            "system_name": system_name,
            "platform": platform,
            "supported_types": supported_types,
            "endpoint_url": endpoint_url,
            "method": method,
            "auth_placement": auth_placement,
            "auth_name": auth_name,
            "auth_scheme": auth_scheme,
            "success_condition": success_condition,
            "failure_condition": failure_condition,
            "identity_fields": identity_fields,
            "timeout_seconds": int(timeout_seconds) if timeout_seconds.is_integer() else timeout_seconds,
            "allowed_hosts": allowed_hosts,
            "enabled": enabled,
        })
        seen_ids.add(endpoint_id)
    return tuple(result)


def redact_authorized_endpoint_specs(value: Any) -> list[dict[str, Any]]:
    try:
        specs = normalize_authorized_endpoint_specs(list(value or []))
    except (TypeError, ValueError):
        return []
    return [dict(spec) for spec in specs]


def authorized_endpoint_for_type(
    credential_type: str,
    platform: str | None,
    authorized_endpoints: Any = (),
) -> dict[str, Any] | None:
    try:
        specs = normalize_authorized_endpoint_specs(list(authorized_endpoints or []))
    except (TypeError, ValueError):
        return None
    normalized_platform = str(platform or "").strip().lower()
    candidates = [
        spec for spec in specs
        if spec["enabled"] and credential_type in spec["supported_types"]
        and (
            spec["platform"] == "*"
            or not normalized_platform
            or spec["platform"].lower() == normalized_platform
        )
    ]
    exact = [
        spec for spec in candidates
        if spec["platform"] != "*" and normalized_platform and spec["platform"].lower() == normalized_platform
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    wildcard = [spec for spec in candidates if spec["platform"] == "*"]
    return wildcard[0] if len(wildcard) == 1 else None


def authorized_endpoint_by_id(endpoint_id: str, authorized_endpoints: Any = ()) -> dict[str, Any] | None:
    try:
        specs = normalize_authorized_endpoint_specs(list(authorized_endpoints or []))
    except (TypeError, ValueError):
        return None
    return next(
        (spec for spec in specs if spec["endpoint_id"] == str(endpoint_id) and spec["enabled"]),
        None,
    )


def load_controlled_verification_fingerprints(path: str | Path | None) -> frozenset[str]:
    """Load fingerprints for an explicit local verification fixture."""
    if not path:
        return frozenset()
    manifest_path = Path(path).expanduser()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(payload, dict) or payload.get("verification_mode") != "controlled_fixture":
        return frozenset()
    values = payload.get("fingerprints")
    if not isinstance(values, list):
        return frozenset()
    return frozenset(
        str(value).strip().lower()
        for value in values
        if isinstance(value, str) and value.strip()
    )


def controlled_verification_outcome(
    credential_type: str,
    *,
    manifest_label: str = "本地受控样例清单",
) -> VerificationOutcome:
    return VerificationOutcome(
        "controlled_valid",
        {
            "verification_mode": "controlled_fixture",
            "request_sent": False,
            "credential_type": credential_type,
            "reason": "controlled_fixture_manifest_match",
            "manifest": manifest_label,
        },
        0,
    )


@dataclass(frozen=True)
class VerificationOutcome:
    status: str
    evidence: dict[str, Any]
    latency_ms: int | None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def verifier_for_type(
    credential_type: str,
    authorized_endpoints: Any = (),
    *,
    platform: str | None = None,
) -> str | None:
    direct = SUPPORTED_VERIFIERS.get(credential_type)
    if direct:
        return direct
    endpoint = authorized_endpoint_for_type(credential_type, platform, authorized_endpoints)
    if endpoint:
        return f"{AUTHORIZED_ENDPOINT_VERIFIER_PREFIX}{endpoint['endpoint_id']}"
    if credential_type in CONTROLLED_VERIFIABLE_TYPES:
        return "local-evidence"
    # Detection rules are the source of truth for credential entities. Types
    # without a safe, provider-neutral identity endpoint still get a bounded
    # local verification attempt instead of being mislabeled unsupported.
    try:
        from .detection.registry import get_registry

        if any(rule.type == credential_type for rule in get_registry().rules):
            return "local-evidence"
    except Exception:
        return None
    return None


def verifier_specs(authorized_endpoints: Any = ()) -> list[dict[str, Any]]:
    specs = [
        {"id": "github-pat", "title": "GitHub PAT（GET /user 身份确认）", "version": "1.0.0", "supported_types": ["github_pat"], "endpoint": "https://api.github.com/user"},
        {"id": "gitee-token", "title": "Gitee Token（GET /api/v5/user 身份确认）", "version": "1.0.0", "supported_types": ["gitee_token"], "endpoint": "https://gitee.com/api/v5/user"},
        {"id": "gitlab-pat", "title": "GitLab PAT（GET /user 身份确认）", "version": "1.0.0", "supported_types": ["gitlab_pat"], "endpoint": "https://gitlab.com/api/v4/user"},
        {"id": "dockerhub-pat", "title": "Docker Hub PAT（账号身份确认）", "version": "1.0.0", "supported_types": ["dockerhub_token"], "endpoint": "https://hub.docker.com/v2/auth/token"},
        {"id": "slack-auth-test", "title": "Slack Token（auth.test 身份确认）", "version": "1.0.0", "supported_types": ["slack_token"], "endpoint": "https://slack.com/api/auth.test"},
        {"id": "telegram-getme", "title": "Telegram Bot Token（getMe 身份确认）", "version": "1.0.0", "supported_types": ["telegram_bot_token"], "endpoint": "https://api.telegram.org/bot<token>/getMe"},
        {"id": "npm-whoami", "title": "npm Token（whoami 身份确认）", "version": "1.0.0", "supported_types": ["npm_token"], "endpoint": "https://registry.npmjs.org/-/whoami"},
        {"id": "huggingface-whoami", "title": "Hugging Face Token（whoami-v2 身份确认）", "version": "1.0.0", "supported_types": ["huggingface_token"], "endpoint": "https://huggingface.co/api/whoami-v2"},
        {"id": "aws-sts-getcalleridentity", "title": "AWS AK/SK（STS GetCallerIdentity 身份确认）", "version": "1.0.0", "supported_types": ["aws_access_key_id", "aws_secret_access_key"], "endpoint": "https://sts.amazonaws.com/?Action=GetCallerIdentity"},
        {"id": "twilio-pair", "title": "Twilio SID/Auth Token（账户身份确认）", "version": "1.0.0", "supported_types": ["twilio_account_sid", "twilio_auth_token"], "endpoint": "https://api.twilio.com/2010-04-01/Accounts/<sid>.json"},
        {"id": "openai-models", "title": "OpenAI API Key（GET /v1/models）", "version": "1.0.0", "supported_types": ["openai_api_key"], "endpoint": "https://api.openai.com/v1/models"},
        {"id": "anthropic-models", "title": "Anthropic API Key（GET /v1/models）", "version": "1.0.0", "supported_types": ["anthropic_api_key"], "endpoint": "https://api.anthropic.com/v1/models"},
        {"id": "stripe-account", "title": "Stripe Secret Key（GET /v1/account）", "version": "1.0.0", "supported_types": ["stripe_live_key", "stripe_test_key"], "endpoint": "https://api.stripe.com/v1/account"},
        {"id": "digitalocean-account", "title": "DigitalOcean Token（GET /v2/account）", "version": "1.0.0", "supported_types": ["digitalocean_token"], "endpoint": "https://api.digitalocean.com/v2/account"},
        {"id": "notion-user", "title": "Notion Integration Token（GET /v1/users/me）", "version": "1.0.0", "supported_types": ["notion_api_key"], "endpoint": "https://api.notion.com/v1/users/me"},
        {"id": "linear-viewer", "title": "Linear API Key（viewer 查询）", "version": "1.0.0", "supported_types": ["linear_api_key"], "endpoint": "https://api.linear.app/graphql"},
        {"id": "pulumi-user", "title": "Pulumi Access Token（GET /api/user）", "version": "1.0.0", "supported_types": ["pulumi_access_token"], "endpoint": "https://api.pulumi.com/api/user"},
        {"id": "postman-user", "title": "Postman API Key（GET /me）", "version": "1.0.0", "supported_types": ["postman_api_key"], "endpoint": "https://api.getpostman.com/me"},
        {"id": "datadog-validate", "title": "Datadog API Key（validate）", "version": "1.0.0", "supported_types": ["datadog_api_key"], "endpoint": "https://api.datadoghq.com/api/v1/validate"},
        {"id": "cloudflare-token-verify", "title": "Cloudflare API Token（verify）", "version": "1.0.0", "supported_types": ["cloudflare_api_token"], "endpoint": "https://api.cloudflare.com/client/v4/user/tokens/verify"},
        {"id": "vercel-user", "title": "Vercel Token（GET /v2/user）", "version": "1.0.0", "supported_types": ["vercel_token"], "endpoint": "https://api.vercel.com/v2/user"},
        {"id": "replicate-account", "title": "Replicate Token（GET /v1/account）", "version": "1.0.0", "supported_types": ["replicate_api_token"], "endpoint": "https://api.replicate.com/v1/account"},
        {"id": "groq-models", "title": "Groq API Key（GET /openai/v1/models）", "version": "1.0.0", "supported_types": ["groq_api_key"], "endpoint": "https://api.groq.com/openai/v1/models"},
        {"id": "mistral-models", "title": "Mistral API Key（GET /v1/models）", "version": "1.0.0", "supported_types": ["mistral_api_key"], "endpoint": "https://api.mistral.ai/v1/models"},
        {"id": "cohere-check", "title": "Cohere API Key（check-api-key）", "version": "1.0.0", "supported_types": ["cohere_api_key"], "endpoint": "https://api.cohere.com/v1/check-api-key"},
    ]
    try:
        configured_endpoints = normalize_authorized_endpoint_specs(list(authorized_endpoints or []))
    except (TypeError, ValueError):
        configured_endpoints = ()
    for endpoint in configured_endpoints:
        specs.append({
            "id": f"{AUTHORIZED_ENDPOINT_VERIFIER_PREFIX}{endpoint['endpoint_id']}",
            "title": f"{endpoint['system_name']} 授权身份端点",
            "version": "authorized-endpoint-1.0.0",
            "supported_types": list(endpoint["supported_types"]),
            "endpoint": endpoint["endpoint_url"],
            "platform": endpoint["platform"],
            "enabled": endpoint["enabled"],
            "auth_placement": endpoint["auth_placement"],
            "success_condition": dict(endpoint["success_condition"]),
            "failure_condition": dict(endpoint["failure_condition"]),
        })
    live_types = {
        item
        for spec in specs
        if not spec["id"].startswith(AUTHORIZED_ENDPOINT_VERIFIER_PREFIX)
        for item in spec["supported_types"]
    }
    from .detection.registry import get_registry

    local_types: list[str] = sorted(
        {*CONTROLLED_VERIFIABLE_TYPES, *(rule.type for rule in get_registry().rules)}
        - live_types
    )
    if local_types:
        specs.append({
            "id": "local-evidence",
            "title": "本地证据校验（格式/配对/授权端点准备度）",
            "version": "1.0.0",
            "supported_types": local_types,
            "endpoint": "不联网；未配置授权端点时只完成本地证据校验",
        })
    return specs


def _latency(start: float) -> int:
    return max(0, int((time.monotonic() - start) * 1000))


def _evidence(endpoint: str, status_code: int | None = None, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"endpoint": endpoint}
    if status_code is not None:
        result["http_status"] = int(status_code)
    result.update({key: value for key, value in extra.items() if value is not None})
    return result


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    timeout_seconds: float,
    body: bytes | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, dict[str, str], bytes, str | None]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    active_opener = opener or urllib.request.build_opener(_NoRedirect())
    try:
        with active_opener.open(request, timeout=timeout_seconds) as response:
            return int(response.status), {str(k).lower(): str(v) for k, v in response.headers.items()}, response.read(128 * 1024), None
    except urllib.error.HTTPError as error:
        try:
            response_body = error.read(128 * 1024)
        except OSError:
            response_body = b""
        return int(error.code), {str(k).lower(): str(v) for k, v in error.headers.items()}, response_body, None
    except (socket.timeout, TimeoutError):
        return 0, {}, b"", "timeout"
    except urllib.error.URLError as error:
        if isinstance(error.reason, (socket.timeout, TimeoutError)):
            return 0, {}, b"", "timeout"
        return 0, {}, b"", "network_error"
    except OSError:
        return 0, {}, b"", "network_error"


def _redirect_outcome(status_code: int, headers: dict[str, str], endpoint: str, allowed_host: str) -> VerificationOutcome | None:
    if not 300 <= status_code < 400:
        return None
    location = headers.get("location", "")
    host = urllib.parse.urlparse(location).hostname
    reason = "redirect_not_followed"
    if host and host != allowed_host:
        reason = "redirect_to_unexpected_host"
    return VerificationOutcome("inconclusive", _evidence(endpoint, status_code, reason=reason), None)


def _json_body(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8", errors="replace"))
    except (UnicodeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_value(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except (UnicodeError, ValueError, json.JSONDecodeError):
        return None


def _json_path_value(payload: Any, path: str) -> tuple[bool, Any]:
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _condition_matches(condition: dict[str, Any], status_code: int, payload: Any) -> bool:
    statuses = condition.get("http_status")
    if statuses and status_code not in statuses:
        return False
    path = condition.get("json_path")
    if not path:
        return bool(statuses)
    exists, value = _json_path_value(payload, path)
    if "exists" in condition:
        return exists is condition["exists"]
    if not exists:
        return False
    return value == condition.get("equals")


def _provider_success(credential_type: str, body: bytes) -> bool:
    payload = _json_value(body)
    if credential_type == "npm_token":
        return isinstance(payload, str) and bool(payload.strip())
    if not isinstance(payload, dict):
        return False
    if credential_type in {"github_pat", "gitee_token", "gitlab_pat"}:
        return bool(payload.get("id")) and bool(
            payload.get("login") or payload.get("username") or payload.get("name")
        )
    if credential_type == "huggingface_token":
        return bool(payload.get("name") or payload.get("id"))
    if credential_type in {"openai_api_key", "anthropic_api_key", "groq_api_key", "mistral_api_key"}:
        return isinstance(payload.get("data"), list)
    if credential_type in {"stripe_live_key", "stripe_test_key"}:
        return bool(payload.get("id"))
    if credential_type == "digitalocean_token":
        account = payload.get("account")
        return isinstance(account, dict) and bool(account.get("uuid") or account.get("email"))
    if credential_type == "notion_api_key":
        return bool(payload.get("id"))
    if credential_type == "linear_api_key":
        viewer = (payload.get("data") or {}).get("viewer") if isinstance(payload.get("data"), dict) else None
        return isinstance(viewer, dict) and bool(viewer.get("id"))
    if credential_type == "pulumi_access_token":
        return bool(payload.get("id") or payload.get("username") or payload.get("githubLogin"))
    if credential_type == "postman_api_key":
        return bool(payload.get("id") or payload.get("username") or payload.get("email"))
    if credential_type == "datadog_api_key":
        return payload.get("valid") is True
    if credential_type == "cloudflare_api_token":
        return payload.get("success") is True and isinstance(payload.get("result"), dict)
    if credential_type == "vercel_token":
        return bool(payload.get("id") or payload.get("username"))
    if credential_type == "replicate_api_token":
        return bool(payload.get("name") or payload.get("username"))
    if credential_type == "cohere_api_key":
        return payload.get("valid") is True
    return False


def _http_verifier(credential_type: str, secret: str, *, timeout_seconds: float, opener) -> VerificationOutcome:
    config = {
        "github_pat": ("https://api.github.com/user", "api.github.com", {"Authorization": f"Bearer {secret}", "Accept": "application/vnd.github+json"}),
        "gitee_token": ("https://gitee.com/api/v5/user", "gitee.com", {"Authorization": f"Bearer {secret}"}),
        "gitlab_pat": ("https://gitlab.com/api/v4/user", "gitlab.com", {"PRIVATE-TOKEN": secret}),
        "npm_token": ("https://registry.npmjs.org/-/whoami", "registry.npmjs.org", {"Authorization": f"Bearer {secret}", "Accept": "application/json"}),
        "huggingface_token": ("https://huggingface.co/api/whoami-v2", "huggingface.co", {"Authorization": f"Bearer {secret}"}),
        "openai_api_key": ("https://api.openai.com/v1/models", "api.openai.com", {"Authorization": f"Bearer {secret}"}),
        "anthropic_api_key": ("https://api.anthropic.com/v1/models", "api.anthropic.com", {"x-api-key": secret, "anthropic-version": "2023-06-01"}),
        "stripe_live_key": ("https://api.stripe.com/v1/account", "api.stripe.com", {"Authorization": f"Basic {b64encode(f'{secret}:'.encode()).decode()}"}),
        "stripe_test_key": ("https://api.stripe.com/v1/account", "api.stripe.com", {"Authorization": f"Basic {b64encode(f'{secret}:'.encode()).decode()}"}),
        "digitalocean_token": ("https://api.digitalocean.com/v2/account", "api.digitalocean.com", {"Authorization": f"Bearer {secret}"}),
        "notion_api_key": ("https://api.notion.com/v1/users/me", "api.notion.com", {"Authorization": f"Bearer {secret}", "Notion-Version": "2022-06-28"}),
        "linear_api_key": ("https://api.linear.app/graphql", "api.linear.app", {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}),
        "pulumi_access_token": ("https://api.pulumi.com/api/user", "api.pulumi.com", {"Authorization": f"token {secret}"}),
        "postman_api_key": ("https://api.getpostman.com/me", "api.getpostman.com", {"X-Api-Key": secret}),
        "datadog_api_key": ("https://api.datadoghq.com/api/v1/validate", "api.datadoghq.com", {"DD-API-KEY": secret}),
        "cloudflare_api_token": ("https://api.cloudflare.com/client/v4/user/tokens/verify", "api.cloudflare.com", {"Authorization": f"Bearer {secret}"}),
        "vercel_token": ("https://api.vercel.com/v2/user", "api.vercel.com", {"Authorization": f"Bearer {secret}"}),
        "replicate_api_token": ("https://api.replicate.com/v1/account", "api.replicate.com", {"Authorization": f"Bearer {secret}"}),
        "groq_api_key": ("https://api.groq.com/openai/v1/models", "api.groq.com", {"Authorization": f"Bearer {secret}"}),
        "mistral_api_key": ("https://api.mistral.ai/v1/models", "api.mistral.ai", {"Authorization": f"Bearer {secret}"}),
        "cohere_api_key": ("https://api.cohere.com/v1/check-api-key", "api.cohere.com", {"Authorization": f"Bearer {secret}"}),
    }
    provider_names = {
        "github_pat": "GitHub",
        "gitee_token": "Gitee",
        "gitlab_pat": "GitLab",
        "npm_token": "npm",
        "huggingface_token": "Hugging Face",
        "openai_api_key": "OpenAI",
        "anthropic_api_key": "Anthropic",
        "stripe_live_key": "Stripe",
        "stripe_test_key": "Stripe",
        "digitalocean_token": "DigitalOcean",
        "notion_api_key": "Notion",
        "linear_api_key": "Linear",
        "pulumi_access_token": "Pulumi",
        "postman_api_key": "Postman",
        "datadog_api_key": "Datadog",
        "cloudflare_api_token": "Cloudflare",
        "vercel_token": "Vercel",
        "replicate_api_token": "Replicate",
        "groq_api_key": "Groq",
        "mistral_api_key": "Mistral",
        "cohere_api_key": "Cohere",
    }
    endpoint, allowed_host, auth_headers = config[credential_type]
    request_url = endpoint
    if credential_type == "gitee_token":
        # Gitee declares access_token in the query; evidence keeps the safe endpoint.
        request_url = endpoint + "?" + urllib.parse.urlencode({"access_token": secret})
        auth_headers = {"Accept": "application/json"}
    start = time.monotonic()
    status_code, headers, body, error = _request(
        "GET", request_url,
        headers={"User-Agent": "ccs-authorized-verifier/1.0", **auth_headers},
        timeout_seconds=timeout_seconds, opener=opener,
    )
    if error:
        return VerificationOutcome(
            "network_error",
            _evidence(
                endpoint,
                reason="transport_timeout" if error == "timeout" else "transport_error",
                provider=provider_names[credential_type],
            ),
            _latency(start),
        )
    redirect = _redirect_outcome(status_code, headers, endpoint, allowed_host)
    if redirect:
        return VerificationOutcome(redirect.status, redirect.evidence, _latency(start))
    evidence = _evidence(endpoint, status_code, provider=provider_names[credential_type])
    if 200 <= status_code < 300:
        if _provider_success(credential_type, body):
            status = "valid"
        else:
            status = "inconclusive"
            evidence["reason"] = "provider_response_missing_identity"
    elif status_code == 401:
        status = "invalid"
        evidence["reason"] = "provider_rejected_credentials"
    elif status_code == 429:
        status = "rate_limited"
        evidence["reason"] = "provider_rate_limited"
    elif status_code == 403:
        if headers.get("x-ratelimit-remaining") == "0":
            status = "rate_limited"
            evidence["reason"] = "provider_rate_limited"
        else:
            status = "inconclusive"
            evidence["reason"] = "provider_forbidden"
    elif status_code == 404 and credential_type == "npm_token":
        status = "inconclusive"
        evidence["reason"] = "npm_whoami_endpoint_not_found"
    elif status_code >= 500:
        status = "inconclusive"
        evidence["reason"] = "provider_unavailable"
    else:
        status = "inconclusive"
        evidence["reason"] = "provider_rejected_request"
    return VerificationOutcome(status, evidence, _latency(start))


def _twilio_verifier(
    secret: str,
    credential_type: str,
    paired: dict[str, Any],
    *,
    timeout_seconds: float,
    opener,
) -> VerificationOutcome:
    endpoint = "https://api.twilio.com/2010-04-01/Accounts/<sid>.json"
    account_sid = secret if credential_type == "twilio_account_sid" else paired.get("twilio_account_sid")
    auth_token = secret if credential_type == "twilio_auth_token" else paired.get("twilio_auth_token")
    if not account_sid or not auth_token:
        return VerificationOutcome(
            "missing_context",
            _evidence(endpoint, reason="missing_twilio_key_pair", request_sent=False),
            None,
        )
    start = time.monotonic()
    status_code, headers, response_body, error = _request(
        "GET",
        f"https://api.twilio.com/2010-04-01/Accounts/{urllib.parse.quote(str(account_sid), safe='')}.json",
        headers={
            "User-Agent": "ccs-authorized-verifier/1.0",
            "Authorization": "Basic " + b64encode(f"{account_sid}:{auth_token}".encode()).decode(),
        },
        timeout_seconds=timeout_seconds,
        opener=opener,
    )
    if error:
        return VerificationOutcome(
            "network_error",
            _evidence(
                endpoint,
                reason="transport_timeout" if error == "timeout" else "transport_error",
                provider="Twilio",
            ),
            _latency(start),
        )
    redirect = _redirect_outcome(status_code, headers, endpoint, "api.twilio.com")
    if redirect:
        return VerificationOutcome(redirect.status, redirect.evidence, _latency(start))
    evidence = _evidence(endpoint, status_code, provider="Twilio")
    if 200 <= status_code < 300:
        payload = _json_body(response_body)
        if payload.get("sid") or payload.get("account_sid"):
            status = "valid"
        else:
            status = "inconclusive"
            evidence["reason"] = "provider_response_missing_identity"
    elif status_code in {401, 403}:
        status = "invalid" if status_code == 401 else "inconclusive"
        evidence["reason"] = "provider_rejected_credentials" if status == "invalid" else "provider_forbidden"
    elif status_code == 429:
        status = "rate_limited"
        evidence["reason"] = "provider_rate_limited"
    elif status_code >= 500:
        status = "inconclusive"
        evidence["reason"] = "provider_unavailable"
    else:
        status = "inconclusive"
        evidence["reason"] = "provider_rejected_request"
    return VerificationOutcome(status, evidence, _latency(start))


def _local_evidence_verifier(credential_type: str, secret: str, paired: dict[str, Any]) -> VerificationOutcome:
    """Validate locally without claiming the remote credential is active.

    Many secrets are passwords, private keys, database URLs, or write-capable
    webhooks. There is no safe provider-neutral identity call for these values.
    They still receive a recorded verification result so the UI can explain the
    exact missing authorization boundary instead of showing unsupported.
    """
    if not secret.strip():
        return VerificationOutcome("missing_context", {"reason": "secret_unavailable"}, None)
    evidence: dict[str, Any] = {
        "verification_mode": "local_evidence",
        "credential_type": credential_type,
        "platform": str(paired.get("platform") or ""),
        "request_sent": False,
        "paired_context": bool(paired),
        "verification_phase": "local_evidence_completed",
        "authorization_endpoint_status": "not_configured",
        "authorization_scope": {
            "configured": False,
            "platform": str(paired.get("platform") or ""),
            "credential_type": credential_type,
        },
        "reason": "authorized_endpoint_not_configured",
    }
    return VerificationOutcome("not_configured", evidence, 0)


def _authorized_endpoint_verifier(
    credential_type: str,
    secret: str,
    endpoint: dict[str, Any],
    *,
    timeout_seconds: float,
    opener,
) -> VerificationOutcome:
    endpoint_url = endpoint["endpoint_url"]
    parsed = urllib.parse.urlparse(endpoint_url)
    host = (parsed.hostname or "").lower()
    if host not in {str(item).lower() for item in endpoint["allowed_hosts"]}:
        return VerificationOutcome(
            "inconclusive",
            _evidence(
                endpoint_url,
                reason="authorized_endpoint_host_not_allowlisted",
                request_sent=False,
                verification_mode="authorized_endpoint",
                endpoint_id=endpoint["endpoint_id"],
            ),
            None,
        )
    auth_value = secret
    if endpoint["auth_scheme"] == "bearer":
        auth_value = f"Bearer {secret}"
    elif endpoint["auth_scheme"] == "basic":
        auth_value = "Basic " + b64encode(secret.encode("utf-8")).decode("ascii")
    request_url = endpoint_url
    headers = {
        "User-Agent": "ccs-authorized-verifier/1.0",
        "Accept": "application/json",
    }
    body: bytes | None = None
    if endpoint["auth_placement"] == "header":
        headers[endpoint["auth_name"]] = auth_value
    elif endpoint["auth_placement"] == "query":
        request_url = endpoint_url + "?" + urllib.parse.urlencode({endpoint["auth_name"]: auth_value})
    else:
        headers["Content-Type"] = "application/json"
        body = json.dumps({endpoint["auth_name"]: auth_value}, separators=(",", ":")).encode("utf-8")
    start = time.monotonic()
    status_code, response_headers, response_body, error = _request(
        endpoint["method"],
        request_url,
        headers=headers,
        timeout_seconds=min(timeout_seconds, float(endpoint["timeout_seconds"])),
        body=body,
        opener=opener,
    )
    base_evidence = {
        "verification_mode": "authorized_endpoint",
        "endpoint_id": endpoint["endpoint_id"],
        "system_name": endpoint["system_name"],
        "platform": endpoint["platform"],
        "credential_type": credential_type,
        "authorization_scope": {
            "endpoint_id": endpoint["endpoint_id"],
            "system_name": endpoint["system_name"],
            "platform": endpoint["platform"],
            "credential_type": credential_type,
            "allowed_host": host,
        },
        "authorization_endpoint_status": "configured",
        "request_sent": True,
    }
    if error:
        return VerificationOutcome(
            "inconclusive",
            _evidence(
                endpoint_url,
                reason="authorized_endpoint_timeout" if error == "timeout" else "authorized_endpoint_unavailable",
                error_type=error,
                **base_evidence,
            ),
            _latency(start),
        )
    redirect = _redirect_outcome(status_code, response_headers, endpoint_url, host)
    if redirect:
        return VerificationOutcome(
            "inconclusive",
            {
                **redirect.evidence,
                **base_evidence,
                "reason": "authorized_endpoint_redirect_not_allowed",
            },
            _latency(start),
        )
    payload = _json_value(response_body)
    evidence = _evidence(endpoint_url, status_code, **base_evidence)
    if _condition_matches(endpoint["success_condition"], status_code, payload):
        missing_identity = []
        for identity_path in endpoint["identity_fields"]:
            exists, value = _json_path_value(payload, identity_path)
            if not exists or value in (None, "", [], {}):
                missing_identity.append(identity_path)
        if missing_identity:
            evidence.update({
                "reason": "authorized_endpoint_identity_fields_missing",
                "identity_fields_checked": list(endpoint["identity_fields"]),
                "identity_fields_missing": missing_identity,
            })
            return VerificationOutcome("inconclusive", evidence, _latency(start))
        evidence["reason"] = "authorized_endpoint_success_condition_matched"
        evidence["identity_fields_checked"] = list(endpoint["identity_fields"])
        return VerificationOutcome("valid", evidence, _latency(start))
    if _condition_matches(endpoint["failure_condition"], status_code, payload):
        evidence["reason"] = "authorized_endpoint_failure_condition_matched"
        return VerificationOutcome("invalid", evidence, _latency(start))
    evidence["reason"] = (
        "authorized_endpoint_response_unexpected"
        if payload is None
        else "authorized_endpoint_conditions_not_matched"
    )
    return VerificationOutcome("inconclusive", evidence, _latency(start))


def _dockerhub_verifier(
    secret: str,
    paired: dict[str, Any],
    *,
    timeout_seconds: float,
    opener,
) -> VerificationOutcome:
    """Confirm a Docker Hub PAT with the account identity endpoint.

    Docker Hub requires the account identifier together with the PAT. The
    response body is intentionally inspected only for the presence of an
    access token and is never persisted in evidence.
    """
    endpoint = "https://hub.docker.com/v2/auth/token"
    username = str(paired.get("dockerhub_username") or "").strip()
    if not username:
        return VerificationOutcome(
            "missing_context",
            _evidence(
                endpoint,
                request_sent=False,
                reason="missing_dockerhub_username",
                provider="Docker Hub",
            ),
            None,
        )

    start = time.monotonic()
    body = json.dumps(
        {"identifier": username, "secret": secret},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    status_code, headers, response_body, error = _request(
        "POST",
        endpoint,
        headers={
            "User-Agent": "ccs-authorized-verifier/1.0",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout_seconds=timeout_seconds,
        body=body,
        opener=opener,
    )
    if error:
        return VerificationOutcome(
            "network_error",
            _evidence(
                endpoint,
                reason="transport_error",
                provider="Docker Hub",
                username=username,
                request_sent=True,
            ),
            _latency(start),
        )

    redirect = _redirect_outcome(status_code, headers, endpoint, "hub.docker.com")
    if redirect:
        return VerificationOutcome(redirect.status, redirect.evidence, _latency(start))

    response = _json_body(response_body)
    evidence = _evidence(
        endpoint,
        status_code,
        provider="Docker Hub",
        username=username,
        request_sent=True,
    )
    if 200 <= status_code < 300:
        if response.get("access_token"):
            return VerificationOutcome("valid", evidence, _latency(start))
        evidence["reason"] = "provider_response_missing_access_token"
        return VerificationOutcome("inconclusive", evidence, _latency(start))
    if status_code == 401:
        evidence["reason"] = "provider_rejected_credentials"
        return VerificationOutcome("invalid", evidence, _latency(start))
    if status_code == 429:
        evidence["reason"] = "provider_rate_limited"
        return VerificationOutcome("rate_limited", evidence, _latency(start))
    if status_code == 403:
        evidence["reason"] = "provider_forbidden"
        return VerificationOutcome("inconclusive", evidence, _latency(start))
    if status_code >= 500:
        evidence["reason"] = "provider_unavailable"
        return VerificationOutcome("inconclusive", evidence, _latency(start))
    evidence["reason"] = "provider_rejected_request"
    return VerificationOutcome("inconclusive", evidence, _latency(start))


def _slack_verifier(secret: str, *, timeout_seconds: float, opener) -> VerificationOutcome:
    endpoint = "https://slack.com/api/auth.test"
    start = time.monotonic()
    status_code, headers, body, error = _request(
        "POST", endpoint,
        headers={"User-Agent": "ccs-authorized-verifier/1.0", "Authorization": f"Bearer {secret}"},
        timeout_seconds=timeout_seconds, opener=opener,
    )
    if error:
        return VerificationOutcome(
            "network_error",
            _evidence(endpoint, reason="transport_timeout" if error == "timeout" else "transport_error"),
            _latency(start),
        )
    redirect = _redirect_outcome(status_code, headers, endpoint, "slack.com")
    if redirect:
        return VerificationOutcome(redirect.status, redirect.evidence, _latency(start))
    data = _json_body(body)
    error_code = str(data.get("error") or "")
    evidence = _evidence(endpoint, status_code, ok=bool(data.get("ok")))
    if data.get("ok") is True:
        status = "valid"
    elif error_code in {"invalid_auth", "account_inactive", "token_revoked", "token_expired"}:
        status = "invalid"
        evidence["error"] = error_code
    elif error_code == "ratelimited" or status_code == 429:
        status = "rate_limited"
    else:
        status = "inconclusive"
    return VerificationOutcome(status, evidence, _latency(start))


def _telegram_verifier(secret: str, *, timeout_seconds: float, opener) -> VerificationOutcome:
    endpoint = "https://api.telegram.org/bot<token>/getMe"
    url = f"https://api.telegram.org/bot{urllib.parse.quote(secret, safe=':_-')}/getMe"
    start = time.monotonic()
    status_code, headers, body, error = _request(
        "GET", url, headers={"User-Agent": "ccs-authorized-verifier/1.0"},
        timeout_seconds=timeout_seconds, opener=opener,
    )
    if error:
        return VerificationOutcome("network_error", _evidence(endpoint, reason="transport_error"), _latency(start))
    redirect = _redirect_outcome(status_code, headers, endpoint, "api.telegram.org")
    if redirect:
        return VerificationOutcome(redirect.status, redirect.evidence, _latency(start))
    data = _json_body(body)
    evidence = _evidence(endpoint, status_code, ok=bool(data.get("ok")))
    if data.get("ok") is True:
        status = "valid"
    elif status_code == 401:
        status = "invalid"
    elif status_code == 429:
        status = "rate_limited"
    else:
        status = "inconclusive"
    return VerificationOutcome(status, evidence, _latency(start))


def _sigv4_headers(access_key: str, secret_key: str) -> dict[str, str]:
    host, region, service = "sts.amazonaws.com", "us-east-1", "sts"
    query = "Action=GetCallerIdentity&Version=2011-06-15"
    now = datetime.now(timezone.utc)
    amzdate, datestamp = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical_headers = f"host:{host}\nx-amz-date:{amzdate}\n"
    signed_headers = "host;x-amz-date"
    canonical_request = f"GET\n/\n{query}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = f"AWS4-HMAC-SHA256\n{amzdate}\n{scope}\n{hashlib.sha256(canonical_request.encode()).hexdigest()}"

    def sign(key: bytes, value: str) -> bytes:
        return hmac.new(key, value.encode(), hashlib.sha256).digest()

    key = sign(sign(sign(("AWS4" + secret_key).encode(), datestamp), region), service)
    signature = hmac.new(sign(key, "aws4_request"), string_to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "Authorization": f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}",
        "x-amz-date": amzdate,
    }


def _aws_verifier(secret: str, credential_type: str, paired: dict[str, Any], *, timeout_seconds: float, opener) -> VerificationOutcome:
    endpoint = "https://sts.amazonaws.com/?Action=GetCallerIdentity"
    access_key = secret if credential_type == "aws_access_key_id" else paired.get("aws_access_key_id")
    secret_key = secret if credential_type == "aws_secret_access_key" else paired.get("aws_secret_access_key")
    if not access_key or not secret_key:
        return VerificationOutcome(
            "missing_context",
            _evidence(endpoint, reason="missing_aws_key_pair", request_sent=False),
            None,
        )
    start = time.monotonic()
    status_code, headers, body, error = _request(
        "GET", "https://sts.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
        headers={"User-Agent": "ccs-authorized-verifier/1.0", **_sigv4_headers(str(access_key), str(secret_key))},
        timeout_seconds=timeout_seconds, opener=opener,
    )
    if error:
        return VerificationOutcome("network_error", _evidence(endpoint, reason="transport_error"), _latency(start))
    redirect = _redirect_outcome(status_code, headers, endpoint, "sts.amazonaws.com")
    if redirect:
        return VerificationOutcome(redirect.status, redirect.evidence, _latency(start))
    text = body.decode("utf-8", errors="replace").lower()
    evidence = _evidence(endpoint, status_code)
    if status_code == 200:
        status = "valid" if "<arn>" in text or "<userid>" in text else "inconclusive"
        if status == "inconclusive":
            evidence["reason"] = "provider_response_missing_identity"
    elif status_code == 403:
        status = "invalid"
    elif status_code == 429 or "throttl" in text:
        status = "rate_limited"
    else:
        status = "inconclusive"
    return VerificationOutcome(status, evidence, _latency(start))


def verify_credential(
    credential_type: str,
    secret: str,
    paired: dict[str, Any] | None = None,
    *,
    timeout_seconds: float = 8.0,
    opener=None,
    authorized_endpoint: dict[str, Any] | None = None,
) -> VerificationOutcome:
    """对一条已获授权的凭据执行最小认证验证。"""
    paired = paired or {}
    if not secret:
        return VerificationOutcome("missing_context", {"reason": "secret_unavailable"}, None)
    if credential_type in SUPPORTED_VERIFIERS:
        pass
    elif authorized_endpoint is not None:
        return _authorized_endpoint_verifier(
            credential_type,
            secret,
            authorized_endpoint,
            timeout_seconds=timeout_seconds,
            opener=opener,
        )
    elif verifier_for_type(credential_type) == "local-evidence":
        return _local_evidence_verifier(credential_type, secret, paired)
    else:
        return VerificationOutcome("unsupported", {"reason": f"no_minimal_verifier:{credential_type}"}, None)
    if credential_type == "dockerhub_token":
        return _dockerhub_verifier(
            secret,
            paired,
            timeout_seconds=timeout_seconds,
            opener=opener,
        )
    if credential_type in {"aws_access_key_id", "aws_secret_access_key"}:
        return _aws_verifier(secret, credential_type, paired, timeout_seconds=timeout_seconds, opener=opener)
    if credential_type in {"twilio_account_sid", "twilio_auth_token"}:
        return _twilio_verifier(
            secret,
            credential_type,
            paired,
            timeout_seconds=timeout_seconds,
            opener=opener,
        )
    if credential_type == "slack_token":
        return _slack_verifier(secret, timeout_seconds=timeout_seconds, opener=opener)
    if credential_type == "telegram_bot_token":
        return _telegram_verifier(secret, timeout_seconds=timeout_seconds, opener=opener)
    return _http_verifier(credential_type, secret, timeout_seconds=timeout_seconds, opener=opener)
