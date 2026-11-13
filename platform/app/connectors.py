"""Connector registry and offline import adapter.

Network-capable implementations live in :mod:`app.public_connectors` and are
still disabled by default. This module remains the stable capability listing
used by the API and UI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .fingerprint import credential_fingerprint
from .models import AuthorizedManifest, EventRecord
from .masking import masked_preview
from .time_provenance import time_entry
from .time_utils import utc_now_iso
from .public_connectors import EXTENDED_CONNECTOR_SPECS


class ConnectorInputError(ValueError):
    """Safe connector error without candidate contents."""


class Connector(Protocol):
    name: str

    def collect(self) -> list[dict]:
        ...


@dataclass(frozen=True)
class ConnectorSpec:
    name: str
    channel: str
    platform: str
    mode: str
    network_enabled: bool
    status: str
    documentation: str


CONNECTOR_SPECS = (
    ConnectorSpec(
        name="authorized-local-directory",
        channel="本地授权载体",
        platform="本地目录",
        mode="offline-authorized",
        network_enabled=False,
        status="已实现",
        documentation="显式目录 + 文件大小/扩展名预算 + YAML规则检测",
    ),
    ConnectorSpec(
        name="authorized-jsonl-export",
        channel="授权离线导入",
        platform="通用 JSONL 导出",
        mode="offline",
        network_enabled=False,
        status="已实现",
        documentation="manifest.json + events.jsonl",
    ),
    ConnectorSpec(
        name="controlled-fixtures",
        channel="受控测试载体",
        platform="本地授权夹具",
        mode="offline-authorized",
        network_enabled=False,
        status="已实现",
        documentation="仅使用仓库内合成样例；不访问网络、不验证凭据有效性",
    ),
    ConnectorSpec(
        name="github-authorized-rest",
        channel="代码托管",
        platform="GitHub（授权 API）",
        mode="online-authorized",
        network_enabled=False,
        status="已实现骨架；默认关闭",
        documentation="GITHUB_TOKEN + 仓库白名单 + 显式网络开关",
    ),
    ConnectorSpec(
        name="gitee-authorized-rest",
        channel="代码托管",
        platform="Gitee（授权 API）",
        mode="online-authorized",
        network_enabled=False,
        status="已实现骨架；默认关闭",
        documentation="GITEE_TOKEN + 仓库白名单 + 显式网络开关",
    ),
)


def list_connector_specs() -> list[dict]:
    return [
        *[spec.__dict__ for spec in CONNECTOR_SPECS],
        *[dict(spec) for spec in EXTENDED_CONNECTOR_SPECS],
    ]


def _mask_value(value: str, credential_type: str) -> str:
    return masked_preview(value, credential_type)


def _safe_export(root: Path, export_file: str) -> Path:
    candidate = (root / export_file).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        raise ConnectorInputError("authorized export escapes manifest root") from None
    if candidate.suffix.lower() != ".jsonl":
        raise ConnectorInputError("authorized export must be JSONL")
    return candidate


class AuthorizedJsonlConnector:
    name = "authorized-jsonl-export"

    def __init__(
        self,
        root: Path,
        hmac_key: str,
        fingerprint_key_version: str = "v1",
        include_internal: bool = False,
    ):
        self.root = root.expanduser().resolve()
        self.hmac_key = hmac_key
        self.fingerprint_key_version = fingerprint_key_version
        self.include_internal = include_internal

    def collect(self) -> list[dict]:
        if not self.hmac_key:
            raise ConnectorInputError("CREDENTIAL_HMAC_KEY is required for offline import")
        manifest_path = self.root / "manifest.json"
        try:
            manifest = AuthorizedManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            raise ConnectorInputError("authorized manifest is missing or invalid") from None
        if manifest.synthetic_only is not True:
            raise ConnectorInputError("stage7 sample connector requires synthetic_only=true")
        if manifest.expires_at < datetime.now(timezone.utc).date().isoformat():
            raise ConnectorInputError("authorization manifest is expired")
        export_path = _safe_export(self.root, manifest.export_file)
        if not export_path.is_file():
            raise ConnectorInputError("authorized export file is missing")
        results: list[dict] = []
        try:
            lines = export_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            raise ConnectorInputError("authorized export cannot be read") from None
        fetched_at = utc_now_iso()
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                value = row["value"]
                credential_type = row["credential_type"]
                locator = row["locator"]
            except (TypeError, KeyError, json.JSONDecodeError):
                raise ConnectorInputError("authorized export row contract is invalid") from None
            if not isinstance(value, str) or not isinstance(credential_type, str) or not isinstance(locator, str):
                raise ConnectorInputError("authorized export row types are invalid")
            event_id = f"AUTH-{manifest.authorization_id}-{line_number}"
            observed_at = utc_now_iso()
            detected_at = utc_now_iso()
            event = {
                "event_id": event_id,
                "source_kind": "离线授权导入",
                "channel": manifest.channel,
                "platform": manifest.platform,
                "credential_type": credential_type,
                "redacted_value": _mask_value(value, credential_type),
                "evidence_level": "L2",
                "risk_level": "high",
                "disposition": "待确认",
                "first_observed_at": observed_at,
                "source_locator": f"authorized:{manifest.authorization_id}:{locator}",
                "detected_at": detected_at,
                "first_discovered_at": detected_at,
                "last_discovered_at": detected_at,
                "detection_method": "离线授权导入",
                "credential_fingerprint": credential_fingerprint(value, self.hmac_key),
                "fingerprint_key_version": self.fingerprint_key_version,
                "time_provenance": {
                    "first_observed_at": time_entry(
                        observed_at,
                        source="authorized_jsonl_read",
                        meaning="授权 JSONL 行读取并完成字段解析的时间。",
                        confidence="high",
                        evidence=f"authorized:{manifest.authorization_id}:{locator}",
                        role="system_observation",
                    ),
                    "detected_at": time_entry(
                        detected_at,
                        source="authorized_jsonl_import_completed",
                        meaning="授权 JSONL 行导入记录完成的时间。",
                        confidence="high",
                        evidence="authorized-jsonl",
                        role="detection_completion",
                        eligible_for_latency=True,
                    ),
                    "first_discovered_at": time_entry(
                        detected_at,
                        source="authorized_jsonl_import_completed",
                        meaning="离线授权 JSONL 行完成解析并形成导入结果的时间。",
                        confidence="high",
                        evidence="authorized-jsonl",
                        role="first_valid_detection",
                        eligible_for_latency=True,
                        evidence_scope="authorized_jsonl",
                    ),
                    "published_at": time_entry(
                        None,
                        source="not_provided",
                        meaning="离线授权导入没有公开渠道发布时间，保持未知。",
                        confidence="none",
                        role="unknown",
                        unknown_reason="connector_not_provided",
                    ),
                },
            }
            validated = EventRecord.model_validate(event).model_dump()
            if self.include_internal:
                validated.update({
                    "_secret": value,
                    "_paired": {},
                    "_vendor": "",
                    "_rule_id": "authorized-jsonl",
                    "_rule_version": 1,
                    "_confidence": 80,
                })
            validated["_fetched_at"] = fetched_at
            results.append(validated)
        return results
