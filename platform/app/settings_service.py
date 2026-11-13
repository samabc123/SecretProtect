"""Runtime settings contract, validation, redacted responses and GitHub probes."""

from __future__ import annotations

import re
import os
import urllib.parse
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from .config import Settings, settings as base_settings
from .connectors import list_connector_specs
from .github_connector import GitHubConnectorError, UrllibTransport
from .gitee_connector import GiteeConnectorError, GiteeTransport
from .settings_store import SettingsStorageError, SettingsStore
from .verification_engine import (
    normalize_authorized_endpoint_specs,
    redact_authorized_endpoint_specs,
)


SETTING_SPECS: dict[str, dict[str, Any]] = {
    "DATA_SOURCE": {
        "label": "统一数据源",
        "default": "mock",
        "kind": "select",
        "options": ["mock", "real"],
        "description": "页面级聚合数据源；SQLite事件/任务数据源单独配置。",
    },
    "EVENTS_SOURCE": {
        "label": "事件数据源",
        "default": "sqlite",
        "kind": "select",
        "options": ["sqlite", "mock"],
        "description": "事件列表、详情与报告读取来源。",
    },
    "MONITORING_SOURCE": {
        "label": "监控任务数据源",
        "default": "sqlite",
        "kind": "select",
        "options": ["sqlite", "mock"],
        "description": "监控任务配置与运行状态读取来源。",
    },
    "RESPONSE_SOURCE": {
        "label": "响应数据源",
        "default": "sqlite",
        "kind": "select",
        "options": ["sqlite", "mock"],
        "description": "告警队列、反馈和审计的存储来源。",
    },
    "LOCAL_OPERATOR_ROLE": {
        "label": "本地操作角色",
        "default": "admin",
        "kind": "select",
        "options": ["viewer", "analyst", "admin"],
        "description": "服务端解析的本地角色；请求体不能自行声明角色。viewer只读，analyst可复核/运行，admin可修改设置和规则。",
    },
    "CONNECTOR_NETWORK_ENABLED": {
        "label": "允许授权连接器联网",
        "default": False,
        "kind": "boolean",
        "options": [],
        "description": "关闭时不发起授权连接器网络请求。",
    },
    "GITHUB_API_BASE": {
        "label": "GitHub API 地址",
        "default": "https://api.github.com",
        "kind": "text",
        "options": [],
        "description": "仅允许官方 HTTPS API 地址。",
    },
    "GITHUB_REPOSITORIES": {
        "label": "授权仓库白名单",
        "default": [],
        "kind": "repositories",
        "options": [],
        "description": "每行一个 owner/repository；只扫描明确授权范围。",
    },
    "GITHUB_TOKEN": {
        "label": "GitHub Token",
        "default": "",
        "kind": "secret",
        "options": [],
        "description": "仅用于授权连接器和连接测试，保存后不回显。",
    },
    "GITEE_API_BASE": {
        "label": "Gitee API 地址",
        "default": "https://gitee.com/api/v5",
        "kind": "text",
        "options": [],
        "description": "仅允许官方 HTTPS API 地址。",
    },
    "GITEE_REPOSITORIES": {
        "label": "Gitee 授权仓库白名单",
        "default": [],
        "kind": "repositories",
        "options": [],
        "description": "每行一个 owner/repository；只扫描明确授权范围。",
    },
    "GITEE_TOKEN": {
        "label": "Gitee Token",
        "default": "",
        "kind": "secret",
        "options": [],
        "description": "仅用于授权连接器，保存后不回显。",
    },
    "WEIBO_ACCESS_TOKEN": {
        "label": "微博 OAuth Access Token",
        "default": "",
        "kind": "secret",
        "options": [],
        "description": "仅用于微博官方 OAuth API 连接器；保存后不回显，来源配置不能填写 Token。",
    },
    "CREDENTIAL_HMAC_KEY": {
        "label": "凭据 HMAC 密钥",
        "default": "",
        "kind": "secret",
        "options": [],
        "description": "用于凭据指纹；保存后不回显，清除后新检测将失败。",
    },
    "CONNECTOR_TIMEOUT_SECONDS": {
        "label": "连接超时（秒）",
        "default": 8.0,
        "kind": "number",
        "options": [],
        "description": "单次 GitHub API 请求超时时间。",
    },
    "CONNECTOR_MAX_FILES": {
        "label": "单仓库最大文件数",
        "default": 50,
        "kind": "number",
        "options": [],
        "description": "单次扫描的文件预算，范围 1-500。",
    },
    "CONNECTOR_MAX_FILE_BYTES": {
        "label": "单文件最大字节数",
        "default": 512 * 1024,
        "kind": "number",
        "options": [],
        "description": "超过预算的文件不会被完整读取。",
    },
    "CONNECTOR_MIN_INTERVAL_SECONDS": {
        "label": "请求最小间隔（秒）",
        "default": 1.0,
        "kind": "number",
        "options": [],
        "description": "请求之间的最小等待时间。",
    },
    "CONNECTOR_MAX_RETRIES": {
        "label": "网络重试次数",
        "default": 2,
        "kind": "number",
        "options": [],
        "description": "仅对限流和传输错误进行有限退避重试。",
    },
    "VERIFICATION_NETWORK_ENABLED": {
        "label": "允许授权凭据联网验证",
        "default": False,
        "kind": "boolean",
        "options": [],
        "description": "默认关闭；开启后仍只验证下方 HMAC 指纹白名单中的凭据。",
    },
    "VERIFICATION_ALLOWED_FINGERPRINTS": {
        "label": "授权验证指纹白名单",
        "default": [],
        "kind": "fingerprints",
        "options": [],
        "description": "每行一个完整 HMAC 指纹；只允许验证明确授权的自有或测试凭据。",
    },
    "VERIFICATION_TIMEOUT_SECONDS": {
        "label": "验证请求超时（秒）",
        "default": 8.0,
        "kind": "number",
        "options": [],
        "description": "超时保持无法判断，不判定为凭据失效。",
    },
    "AUTHORIZED_VERIFICATION_ENDPOINTS": {
        "label": "明确授权验证端点",
        "default": [],
        "kind": "authorized_endpoints",
        "options": [],
        "description": "仅允许访问配置中的 HTTPS 或本机端点；成功和失败必须由 JSON 字段条件明确判断。",
    },
    "DOCKERHUB_USERNAME": {
        "label": "Docker Hub 用户名",
        "default": "",
        "kind": "text",
        "options": [],
        "description": "用于 Docker Hub PAT 身份确认；只保存用户名，不保存 Docker Hub Token。",
    },
}

SENSITIVE_KEYS = {
    key
    for key, spec in SETTING_SPECS.items()
    if spec["kind"] in {"secret", "authorized_endpoints"}
}
SOURCE_CATALOG = (
    {
        "key": "local_dir",
        "title": "本地授权目录",
        "config_hint": '{"path":"C:/authorized/project-copy"}',
        "network": False,
    },
    {
        "key": "controlled_fixtures",
        "title": "本地受控夹具（仅开发验证）",
        "config_hint": '{"path":"fixtures/controlled"}',
        "network": False,
    },
    {
        "key": "authorized_jsonl",
        "title": "授权 JSONL 导入（离线数据）",
        "config_hint": '{"path":"fixtures/authorized"}',
        "network": False,
    },
    {
        "key": "github",
        "title": "GitHub 授权 API",
        "config_hint": '{"repositories":["owner/repository"],"branch":"main"}',
        "network": True,
    },
    {
        "key": "gitee",
        "title": "Gitee 授权 API",
        "config_hint": '{"repositories":["owner/repository"],"branch":"master"}',
        "network": True,
    },
    {
        "key": "mediawiki",
        "title": "MediaWiki 公开 API",
        "config_hint": '{"api_url":"https://wiki.example.org/w/api.php","max_items":20}',
        "network": True,
    },
    {
        "key": "generic_web",
        "title": "RSS / Atom / Sitemap / URL",
        "config_hint": '{"feed_url":"https://example.org/feed.xml","max_items":20}',
        "network": True,
    },
    {
        "key": "stackoverflow",
        "title": "Stack Overflow 公开 API",
        "config_hint": '{"queries":["AWS_ACCESS_KEY_ID","AWS_SECRET_ACCESS_KEY","API_KEY","API_TOKEN","DATABASE_URL","DB_PASSWORD","PRIVATE KEY","OPENAI_API_KEY"],"site":"stackoverflow","max_items":10,"include_answers":true}',
        "network": True,
    },
    {
        "key": "weibo_authorized_api",
        "title": "微博（官方 OAuth API）",
        "config_hint": '{"uid":"7845697819","post_id":"5355671562486378","include_long_text":true,"max_items":1}',
        "network": True,
    },
    {
        "key": "oci_registry",
        "title": "OCI / Docker 镜像",
        "config_hint": '{"image":"alpine:latest"}',
        "network": True,
    },
    {
        "key": "npm_registry",
        "title": "npm 包仓库",
        "config_hint": '{"package":"package-name","version":"1.0.0"}',
        "network": True,
    },
    {
        "key": "pypi_registry",
        "title": "PyPI 包仓库",
        "config_hint": '{"package":"package-name","version":"1.0.0"}',
        "network": True,
    },
    {
        "key": "huggingface_repo",
        "title": "Hugging Face 公开仓库",
        "config_hint": '{"repository":"owner/model-or-dataset","revision":"main"}',
        "network": True,
    },
    {
        "key": "crates_registry",
        "title": "crates.io 包仓库",
        "config_hint": '{"package":"package-name","version":"1.0.0"}',
        "network": True,
    },
    {
        "key": "nuget_registry",
        "title": "NuGet 包仓库",
        "config_hint": '{"package":"package-name","version":"1.0.0"}',
        "network": True,
    },
    {
        "key": "rubygems_registry",
        "title": "RubyGems 包仓库",
        "config_hint": '{"package":"package-name","version":"1.0.0"}',
        "network": True,
    },
    {
        "key": "apk",
        "title": "Android APK 静态扫描",
        "config_hint": '{"path":"C:/authorized/app.apk"}',
        "network": False,
    },
    {
        "key": "wxapkg",
        "title": "未加密 wxapkg 静态扫描",
        "config_hint": '{"path":"C:/authorized/app.wxapkg"}',
        "network": False,
    },
)
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BASELINE_FIELDS = {
    "data_source": base_settings.data_source,
    "events_source": base_settings.events_source,
    "monitoring_source": base_settings.monitoring_source,
    "response_source": base_settings.response_source,
    "local_operator_role": base_settings.local_operator_role,
    "connector_network_enabled": base_settings.connector_network_enabled,
    "github_api_base": base_settings.github_api_base,
    "github_repositories": base_settings.github_repositories,
    "github_token": base_settings.github_token,
    "credential_hmac_key": base_settings.credential_hmac_key,
    "gitee_api_base": base_settings.gitee_api_base,
    "gitee_repositories": base_settings.gitee_repositories,
    "gitee_token": base_settings.gitee_token,
    "weibo_access_token": base_settings.weibo_access_token,
    "connector_timeout_seconds": base_settings.connector_timeout_seconds,
    "connector_max_files": base_settings.connector_max_files,
    "connector_max_file_bytes": base_settings.connector_max_file_bytes,
    "connector_min_interval_seconds": base_settings.connector_min_interval_seconds,
    "connector_max_retries": base_settings.connector_max_retries,
    "verification_network_enabled": base_settings.verification_network_enabled,
    "verification_allowed_fingerprints": base_settings.verification_allowed_fingerprints,
    "verification_timeout_seconds": base_settings.verification_timeout_seconds,
    "authorized_verification_endpoints": base_settings.authorized_verification_endpoints,
    "dockerhub_username": base_settings.dockerhub_username,
}


def _store(runtime: Settings) -> SettingsStore:
    return SettingsStore(runtime.settings_database_path, runtime.settings_master_key_path)


def _raw_values(runtime: Settings) -> dict[str, Any]:
    return _store(runtime).read_all(SENSITIVE_KEYS)


def _validate_value(key: str, value: Any) -> Any:
    if key not in SETTING_SPECS:
        raise ValueError("unknown_setting")
    spec = SETTING_SPECS[key]
    kind = spec["kind"]
    if kind == "boolean":
        if not isinstance(value, bool):
            raise ValueError("invalid_boolean")
        return value
    if kind == "select":
        if not isinstance(value, str) or value not in spec["options"]:
            raise ValueError("invalid_option")
        return value
    if kind == "secret":
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError("invalid_secret")
        return value
    if kind == "repositories":
        if not isinstance(value, list) or len(value) > 100:
            raise ValueError("invalid_repository_list")
        normalized = []
        for item in value:
            if not isinstance(item, str) or not _REPOSITORY.fullmatch(item.strip()):
                raise ValueError("invalid_repository_scope")
            normalized.append(item.strip())
        return list(dict.fromkeys(normalized))
    if kind == "fingerprints":
        if not isinstance(value, list) or len(value) > 500:
            raise ValueError("invalid_fingerprint_list")
        normalized = []
        for item in value:
            if not isinstance(item, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", item.strip()):
                raise ValueError("invalid_fingerprint")
            normalized.append(item.strip().lower())
        return list(dict.fromkeys(normalized))
    if kind == "authorized_endpoints":
        return [dict(item) for item in normalize_authorized_endpoint_specs(value)]
    if kind == "number":
        if isinstance(value, bool):
            raise ValueError("invalid_number")
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("invalid_number")
            try:
                value = float(value)
            except ValueError:
                raise ValueError("invalid_number") from None
        if not isinstance(value, (int, float)):
            raise ValueError("invalid_number")
        number = float(value)
        if key == "CONNECTOR_TIMEOUT_SECONDS" and not 1 <= number <= 120:
            raise ValueError("timeout_out_of_range")
        if key == "VERIFICATION_TIMEOUT_SECONDS" and not 1 <= number <= 120:
            raise ValueError("verification_timeout_out_of_range")
        if key == "CONNECTOR_MAX_FILES" and (number < 1 or number > 500 or int(number) != number):
            raise ValueError("max_files_out_of_range")
        if key == "CONNECTOR_MAX_FILE_BYTES" and (number < 1024 or number > 10 * 1024 * 1024 or int(number) != number):
            raise ValueError("max_file_bytes_out_of_range")
        if key == "CONNECTOR_MIN_INTERVAL_SECONDS" and not 0 <= number <= 60:
            raise ValueError("interval_out_of_range")
        if key == "CONNECTOR_MAX_RETRIES" and (number < 0 or number > 5 or int(number) != number):
            raise ValueError("retries_out_of_range")
        return int(number) if int(number) == number else number
    if kind == "text" and key not in {"GITHUB_API_BASE", "GITEE_API_BASE"}:
        if not isinstance(value, str) or len(value) > 255:
            raise ValueError("invalid_text")
        return value.strip()
    if key == "GITHUB_API_BASE":
        if not isinstance(value, str):
            raise ValueError("invalid_api_base")
        parsed = urllib.parse.urlparse(value.strip().rstrip("/"))
        if parsed.scheme != "https" or parsed.netloc not in {"api.github.com", "github.com"}:
            raise ValueError("github_api_base_not_allowed")
        return value.strip().rstrip("/")
    if key == "GITEE_API_BASE":
        if not isinstance(value, str):
            raise ValueError("invalid_api_base")
        parsed = urllib.parse.urlparse(value.strip().rstrip("/"))
        if (
            parsed.scheme != "https"
            or parsed.netloc != "gitee.com"
            or parsed.path.rstrip("/") != "/api/v5"
        ):
            raise ValueError("gitee_api_base_not_allowed")
        return value.strip().rstrip("/")
    raise ValueError("unsupported_setting")


def effective_settings(runtime: Settings = base_settings) -> Settings:
    """Merge UI settings only for the process-global configuration object.

    Unit tests and isolated callers can pass a Settings instance to retain their
    explicit values without reading the user's persistent settings database.
    """
    if runtime is not base_settings:
        return runtime
    try:
        values = _raw_values(runtime)
    except SettingsStorageError:
        return runtime
    overrides: dict[str, Any] = {}
    mapping = {
        "DATA_SOURCE": "data_source",
        "EVENTS_SOURCE": "events_source",
        "MONITORING_SOURCE": "monitoring_source",
        "RESPONSE_SOURCE": "response_source",
        "LOCAL_OPERATOR_ROLE": "local_operator_role",
        "CONNECTOR_NETWORK_ENABLED": "connector_network_enabled",
        "GITHUB_API_BASE": "github_api_base",
        "GITHUB_REPOSITORIES": "github_repositories",
        "GITHUB_TOKEN": "github_token",
        "CREDENTIAL_HMAC_KEY": "credential_hmac_key",
        "GITEE_API_BASE": "gitee_api_base",
        "GITEE_REPOSITORIES": "gitee_repositories",
        "GITEE_TOKEN": "gitee_token",
        "WEIBO_ACCESS_TOKEN": "weibo_access_token",
        "CONNECTOR_TIMEOUT_SECONDS": "connector_timeout_seconds",
        "CONNECTOR_MAX_FILES": "connector_max_files",
        "CONNECTOR_MAX_FILE_BYTES": "connector_max_file_bytes",
        "CONNECTOR_MIN_INTERVAL_SECONDS": "connector_min_interval_seconds",
        "CONNECTOR_MAX_RETRIES": "connector_max_retries",
        "VERIFICATION_NETWORK_ENABLED": "verification_network_enabled",
        "VERIFICATION_ALLOWED_FINGERPRINTS": "verification_allowed_fingerprints",
        "VERIFICATION_TIMEOUT_SECONDS": "verification_timeout_seconds",
        "AUTHORIZED_VERIFICATION_ENDPOINTS": "authorized_verification_endpoints",
        "DOCKERHUB_USERNAME": "dockerhub_username",
    }
    environment_keys = {
        "data_source": "DATA_SOURCE",
        "events_source": "EVENTS_SOURCE",
        "monitoring_source": "MONITORING_SOURCE",
        "response_source": "RESPONSE_SOURCE",
        "local_operator_role": "APP_OPERATOR_ROLE",
        "connector_network_enabled": "CONNECTOR_NETWORK_ENABLED",
        "github_api_base": "GITHUB_API_BASE",
        "github_repositories": "GITHUB_REPOSITORIES",
        "github_token": "GITHUB_TOKEN",
        "credential_hmac_key": "CREDENTIAL_HMAC_KEY",
        "gitee_api_base": "GITEE_API_BASE",
        "gitee_repositories": "GITEE_REPOSITORIES",
        "gitee_token": "GITEE_TOKEN",
        "weibo_access_token": "WEIBO_ACCESS_TOKEN",
        "connector_timeout_seconds": "CONNECTOR_TIMEOUT_SECONDS",
        "connector_max_files": "CONNECTOR_MAX_FILES",
        "connector_max_file_bytes": "CONNECTOR_MAX_FILE_BYTES",
        "connector_min_interval_seconds": "CONNECTOR_MIN_INTERVAL_SECONDS",
        "connector_max_retries": "CONNECTOR_MAX_RETRIES",
        "verification_network_enabled": "VERIFICATION_NETWORK_ENABLED",
        "verification_allowed_fingerprints": "VERIFICATION_ALLOWED_FINGERPRINTS",
        "verification_timeout_seconds": "VERIFICATION_TIMEOUT_SECONDS",
        "authorized_verification_endpoints": "AUTHORIZED_VERIFICATION_ENDPOINTS",
        "dockerhub_username": "DOCKERHUB_USERNAME",
    }
    for key, field in mapping.items():
        # Isolated callers and tests may temporarily override the immutable
        # Settings singleton. Persistent UI values must not overwrite that
        # explicit process-local override.
        if runtime is base_settings and getattr(runtime, field) != _BASELINE_FIELDS[field]:
            continue
        # An explicitly supplied process environment is a deliberate startup
        # override (used by run.ps1 and isolated runtime checks). Persistent
        # UI settings remain the default when no environment value is given.
        if environment_keys[field] in os.environ:
            continue
        if key in values:
            value = values[key]
            if key in {"EVENTS_SOURCE", "MONITORING_SOURCE", "RESPONSE_SOURCE", "DATA_SOURCE"}:
                value = str(value).lower()
            if key in {"GITHUB_REPOSITORIES", "GITEE_REPOSITORIES", "VERIFICATION_ALLOWED_FINGERPRINTS"}:
                value = tuple(value)
            if key == "AUTHORIZED_VERIFICATION_ENDPOINTS":
                value = tuple(normalize_authorized_endpoint_specs(value))
            overrides[field] = value
    return replace(runtime, **overrides)


def settings_payload(runtime: Settings = base_settings) -> dict[str, Any]:
    try:
        values = _raw_values(runtime)
    except SettingsStorageError as error:
        return {
            "schema_version": "1.0",
            "page_title": "系统设置",
            "source": "真实数据：SQLite 设置库读取失败",
            "implementation_status": "部分实现",
            "error": str(error),
            "items": [],
            "runtime": {},
        }
    active = effective_settings(runtime)
    fallback_values = {
        "DATA_SOURCE": active.data_source,
        "EVENTS_SOURCE": active.events_source,
        "MONITORING_SOURCE": active.monitoring_source,
        "RESPONSE_SOURCE": active.response_source,
        "LOCAL_OPERATOR_ROLE": active.local_operator_role,
        "CONNECTOR_NETWORK_ENABLED": active.connector_network_enabled,
        "GITHUB_API_BASE": active.github_api_base,
        "GITHUB_REPOSITORIES": list(active.github_repositories),
        "GITHUB_TOKEN": active.github_token,
        "CREDENTIAL_HMAC_KEY": active.credential_hmac_key,
        "GITEE_API_BASE": active.gitee_api_base,
        "GITEE_REPOSITORIES": list(active.gitee_repositories),
        "GITEE_TOKEN": active.gitee_token,
        "WEIBO_ACCESS_TOKEN": active.weibo_access_token,
        "CONNECTOR_TIMEOUT_SECONDS": active.connector_timeout_seconds,
        "CONNECTOR_MAX_FILES": active.connector_max_files,
        "CONNECTOR_MAX_FILE_BYTES": active.connector_max_file_bytes,
        "CONNECTOR_MIN_INTERVAL_SECONDS": active.connector_min_interval_seconds,
        "CONNECTOR_MAX_RETRIES": active.connector_max_retries,
        "VERIFICATION_NETWORK_ENABLED": active.verification_network_enabled,
        "VERIFICATION_ALLOWED_FINGERPRINTS": list(active.verification_allowed_fingerprints),
        "VERIFICATION_TIMEOUT_SECONDS": active.verification_timeout_seconds,
        "AUTHORIZED_VERIFICATION_ENDPOINTS": list(active.authorized_verification_endpoints),
        "DOCKERHUB_USERNAME": active.dockerhub_username,
    }
    items = []
    for key, spec in SETTING_SPECS.items():
        is_sensitive = key in SENSITIVE_KEYS
        value = values.get(key, fallback_values.get(key, spec["default"]))
        configured = (
            (bool(value) if is_sensitive else True)
            if key in values
            else (bool(fallback_values.get(key)) if is_sensitive else True)
        )
        items.append({
            "key": key,
            "label": spec["label"],
            "value": None if is_sensitive else value,
            "configured": configured,
            "sensitive": is_sensitive,
            "editable": True,
            "kind": spec["kind"],
            "options": spec["options"],
            "description": spec["description"],
        })
    ready = bool(
        active.connector_network_enabled
        and active.github_token
        and active.credential_hmac_key
        and active.github_repositories
    )
    gitee_ready = bool(
        active.connector_network_enabled
        and active.gitee_token
        and active.credential_hmac_key
        and active.gitee_repositories
    )
    weibo_ready = bool(
        active.connector_network_enabled
        and active.weibo_access_token
        and active.credential_hmac_key
    )
    return {
        "schema_version": "1.0",
        "page_title": "系统设置",
        "source": "真实数据：SQLite 设置库（敏感项脱敏）",
        "implementation_status": "部分实现",
        "error": None,
        "items": items,
        "source_catalog": [dict(item) for item in SOURCE_CATALOG],
        "collector_specs": list_connector_specs(),
        "runtime": {
            "network_enabled": active.connector_network_enabled,
            "github_ready": ready,
            "gitee_ready": gitee_ready,
            "token_configured": bool(active.github_token),
            "hmac_configured": bool(active.credential_hmac_key),
            "repositories_count": len(active.github_repositories),
            "gitee_token_configured": bool(active.gitee_token),
            "gitee_repositories_count": len(active.gitee_repositories),
            "weibo_token_configured": bool(active.weibo_access_token),
            "weibo_ready": weibo_ready,
            "api_base": active.github_api_base,
            "operator_role": active.local_operator_role,
            "restart_required": False,
            "authorized_verification_endpoints": redact_authorized_endpoint_specs(
                active.authorized_verification_endpoints
            ),
            "authorized_verification_endpoint_count": len(active.authorized_verification_endpoints),
        },
    }


def update_settings(values: dict[str, Any], runtime: Settings = base_settings) -> dict[str, Any]:
    normalized = {}
    for key, value in values.items():
        normalized[key] = _validate_value(key, value)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    try:
        changed = _store(runtime).write_many(normalized, SENSITIVE_KEYS, now)
    except SettingsStorageError as error:
        return {
            "schema_version": "1.0",
            "source": "真实数据：SQLite 设置库写入失败",
            "implementation_status": "部分实现",
            "error": str(error),
            "changed": [],
            "runtime": {},
        }
    payload = settings_payload(runtime)
    payload["changed"] = changed
    return payload


def test_github_connection(repository: str, runtime: Settings = base_settings) -> dict[str, Any]:
    active = effective_settings(runtime)
    repository = repository.strip()
    if not _REPOSITORY.fullmatch(repository):
        return {"ok": False, "error": "仓库格式必须为 owner/repository。", "repository": repository}
    if repository not in active.github_repositories:
        return {"ok": False, "error": "该仓库不在设置页白名单中，未发起请求。", "repository": repository}
    if not active.connector_network_enabled:
        return {"ok": False, "error": "联网开关已关闭，未发起请求。", "repository": repository}
    if not active.github_token:
        return {"ok": False, "error": "GitHub Token 未配置，未发起请求。", "repository": repository}
    parsed = urllib.parse.urlparse(active.github_api_base)
    if parsed.scheme != "https" or parsed.netloc not in {"api.github.com", "github.com"}:
        return {"ok": False, "error": "GitHub API 地址不在允许范围，未发起请求。", "repository": repository}
    owner, name = repository.split("/", 1)
    url = f"{active.github_api_base.rstrip('/')}/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {active.github_token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "credential-leak-detector-authorized-connector",
    }
    try:
        response = UrllibTransport(active.connector_timeout_seconds).get_json(url, headers)
        payload = response.payload if isinstance(response.payload, dict) else {}
        return {
            "ok": True,
            "error": None,
            "repository": repository,
            "message": "仓库可访问；连接测试未扫描内容、未生成事件。",
            "visibility": payload.get("visibility", "unknown"),
            "default_branch": payload.get("default_branch", "unknown"),
        }
    except GitHubConnectorError as error:
        messages = {
            "github_forbidden": "Token 无权访问该仓库或请求被拒绝。",
            "github_not_found": "仓库不存在或当前 Token 无权查看。",
            "github_rate_limited": "GitHub 返回限流；未生成事件。",
            "github_transport_error": "GitHub 请求超时或网络不可达。",
            "github_http_error": "GitHub 返回非预期错误。",
        }
        return {"ok": False, "error": messages.get(error.code, "连接测试失败；未生成事件。"), "repository": repository}


test_github_connection.__test__ = False


def test_gitee_connection(repository: str, runtime: Settings = base_settings) -> dict[str, Any]:
    active = effective_settings(runtime)
    repository = repository.strip()
    if not _REPOSITORY.fullmatch(repository):
        return {"ok": False, "error": "仓库格式必须为 owner/repository。", "repository": repository}
    if repository not in active.gitee_repositories:
        return {"ok": False, "error": "该仓库不在 Gitee 设置页白名单中，未发起请求。", "repository": repository}
    if not active.connector_network_enabled:
        return {"ok": False, "error": "联网开关已关闭，未发起请求。", "repository": repository}
    if not active.gitee_token:
        return {"ok": False, "error": "Gitee Token 未配置，未发起请求。", "repository": repository}
    parsed = urllib.parse.urlparse(active.gitee_api_base.rstrip("/"))
    if (
        parsed.scheme != "https"
        or parsed.netloc != "gitee.com"
        or parsed.path.rstrip("/") != "/api/v5"
    ):
        return {"ok": False, "error": "Gitee API 地址不在允许范围，未发起请求。", "repository": repository}
    owner, name = repository.split("/", 1)
    url = f"{active.gitee_api_base.rstrip('/')}/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {active.gitee_token}",
        "User-Agent": "credential-leak-detector-authorized-connector",
    }
    try:
        response = GiteeTransport(active.connector_timeout_seconds).get_json(url, headers)
        payload = response.payload if isinstance(response.payload, dict) else {}
        return {
            "ok": True,
            "error": None,
            "repository": repository,
            "message": "Gitee 仓库可访问；连接测试未扫描内容、未生成事件。",
            "default_branch": payload.get("default_branch", "unknown"),
        }
    except GiteeConnectorError as error:
        messages = {
            "gitee_unauthorized": "Token 无权访问该仓库或已失效。",
            "gitee_forbidden": "Gitee 拒绝访问，请检查令牌权限和仓库授权范围。",
            "gitee_not_found": "仓库不存在或当前 Token 无权查看。",
            "gitee_rate_limited": "Gitee 返回限流；未生成事件。",
            "gitee_transport_error": "Gitee 请求超时或网络不可达。",
            "gitee_http_error": "Gitee 返回非预期错误。",
        }
        return {"ok": False, "error": messages.get(error.code, "连接测试失败；未生成事件。"), "repository": repository}


test_gitee_connection.__test__ = False


def test_gitee_token(runtime: Settings = base_settings) -> dict[str, Any]:
    """Probe the configured Gitee token against the fixed identity endpoint."""
    active = effective_settings(runtime)
    if not active.connector_network_enabled:
        return {"ok": False, "error": "联网开关已关闭，未发起请求。"}
    if not active.gitee_token:
        return {"ok": False, "error": "Gitee Token 未配置，未发起请求。"}
    parsed = urllib.parse.urlparse(active.gitee_api_base.rstrip("/"))
    if (
        parsed.scheme != "https"
        or parsed.netloc != "gitee.com"
        or parsed.path.rstrip("/") != "/api/v5"
    ):
        return {"ok": False, "error": "Gitee API 地址不在允许范围，未发起请求。"}
    url = f"{active.gitee_api_base.rstrip('/')}/user"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {active.gitee_token}",
        "User-Agent": "credential-leak-detector-authorized-connector",
    }
    try:
        response = GiteeTransport(active.connector_timeout_seconds).get_json(url, headers)
        payload = response.payload if isinstance(response.payload, dict) else {}
        login = payload.get("login")
        user_id = payload.get("id")
        if not isinstance(login, str) or not login or type(user_id) is not int or user_id <= 0:
            return {"ok": False, "error": "Gitee 未返回有效账号身份，无法确认 Token；未生成事件。"}
        return {
            "ok": True,
            "error": None,
            "message": "Gitee Token 身份验证通过；未读取仓库内容、未生成事件。",
            "login": login,
        }
    except GiteeConnectorError as error:
        messages = {
            "gitee_unauthorized": "Gitee Token 无效、已过期或已撤销。",
            "gitee_forbidden": "Gitee 拒绝 Token 身份验证，请检查权限范围。",
            "gitee_rate_limited": "Gitee 返回限流；未生成事件。",
            "gitee_transport_error": "Gitee 请求超时或网络不可达。",
            "gitee_http_error": "Gitee 返回非预期错误。",
        }
        return {"ok": False, "error": messages.get(error.code, "Token 身份验证失败；未生成事件。")}


test_gitee_token.__test__ = False
