"""Detect only synthetic credentials in an explicit local controlled fixture root."""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .fingerprint import credential_fingerprint
from .masking import masked_preview
from .time_provenance import time_entry
from .time_utils import utc_now_iso

APPROVED_EXTENSIONS = {".env", ".ini", ".cfg", ".conf", ".txt", ".yaml", ".yml", ".json", ".toml", ".pem"}
MAX_FILE_BYTES = 512 * 1024

RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("AK/SK", re.compile(r"\bAKDEMO[A-Z0-9]{8,32}\b"), "AK/SK demo marker"),
    ("API Key", re.compile(r"\b(?:api|key)_demo_[A-Za-z0-9_-]{4,48}\b"), "API key demo marker"),
    ("API Token", re.compile(r"\btok_demo_[A-Za-z0-9_-]{4,48}\b"), "API token demo marker"),
    ("OAuth Client Secret", re.compile(r"\boauth_demo_[A-Za-z0-9_-]{4,48}\b"), "OAuth demo marker"),
    ("数据库密码", re.compile(r"\bdb_pass_[A-Za-z0-9_-]{4,48}\b"), "database password demo marker"),
    ("SSH 私钥", re.compile(r"-----BEGIN DEMO PRIVATE KEY-----"), "SSH demo marker"),
    ("操作系统账号密码", re.compile(r"\bdemo_pass_[A-Za-z0-9_-]{4,48}\b"), "account password demo marker"),
    ("数据库连接串", re.compile(
        r"(?:postgres|mysql)://demo:[^@\s\"']+@(?:demo-host|localhost)(?::\d+)?/[A-Za-z0-9_-]+"
    ), "database URL demo marker"),
)

CONTROLLED_RULE_IDS = {
    "AK/SK": "controlled-ak-sk",
    "API Key": "controlled-api-key",
    "API Token": "controlled-api-token",
    "OAuth Client Secret": "controlled-oauth-client-secret",
    "数据库密码": "controlled-database-password",
    "SSH 私钥": "controlled-ssh-private-key",
    "操作系统账号密码": "controlled-os-password",
    "数据库连接串": "controlled-database-url",
}


def controlled_rule_specs() -> list[dict[str, object]]:
    return [
        {
            "id": rule_id,
            "title": f"受控夹具：{credential_type}",
            "type": credential_type,
            "vendor": "controlled-fixture",
            "version": 1,
            "license": "internal",
            "verifier_id": None,
        }
        for credential_type, rule_id in CONTROLLED_RULE_IDS.items()
    ]


class ControlledFixtureError(ValueError):
    """A safe, user-facing validation error without file contents."""


def _masked(value: str, credential_type: str) -> str:
    return masked_preview(value, credential_type)


def _event_id(relative_path: str, line_number: int, credential_type: str) -> str:
    token = f"{relative_path}:{line_number}:{credential_type}".encode("utf-8")
    return f"CTL-{hashlib.sha256(token).hexdigest()[:20].upper()}"


def _safe_relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        raise ControlledFixtureError("fixture path escapes the authorized root") from None


def detect_controlled_fixtures(
    root: Path, detected_at: str | None = None, hmac_key: str | None = None,
    fingerprint_key_version: str = "v1", include_internal: bool = False,
    enabled_rule_ids: set[str] | frozenset[str] | None = None,
) -> list[dict]:
    """Return masked EventRecord-shaped dictionaries from a local fixture directory."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ControlledFixtureError("fixture root must be an existing directory")
    if not hmac_key:
        raise ControlledFixtureError("CREDENTIAL_HMAC_KEY is required for controlled detection")
    scan_started = time.perf_counter()
    observed_at = detected_at or utc_now_iso()
    results: list[dict] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in APPROVED_EXTENSIONS:
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ControlledFixtureError("fixture file exceeds the local size limit")
        relative = _safe_relative(path, root)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            matches = []
            for credential_type, pattern, method in RULES:
                rule_id = CONTROLLED_RULE_IDS[credential_type]
                if enabled_rule_ids is not None and rule_id not in enabled_rule_ids:
                    continue
                match = pattern.search(line)
                if match:
                    matches.append((credential_type, pattern, method, match, rule_id))
            # Prefer the connection string as the parent evidence for its password.
            if any(item[0] == "数据库连接串" for item in matches):
                matches = [item for item in matches if item[0] == "数据库连接串"]
            for credential_type, pattern, method, match, rule_id in matches:
                value = match.group(0)
                risk = "critical" if credential_type in {"AK/SK", "数据库连接串", "数据库密码"} else "high"
                event = {
                    "event_id": _event_id(relative, line_number, credential_type),
                    "source_kind": "受控投放检出",
                    "channel": "受控测试载体",
                    "platform": "本地授权夹具",
                    "credential_type": credential_type,
                    "redacted_value": _masked(value, credential_type),
                    "evidence_level": "L1",
                    "risk_level": risk,
                    "disposition": "待确认",
                    "first_observed_at": observed_at,
                    "source_locator": f"fixture:{relative}:line-{line_number}",
                    "detected_at": None,
                    "first_discovered_at": None,
                    "last_discovered_at": None,
                    "detection_method": method,
                    "credential_fingerprint": credential_fingerprint(value, hmac_key),
                    "fingerprint_key_version": fingerprint_key_version,
                }
                if include_internal:
                    event.update({
                        "_secret": value,
                        "_paired": {},
                        "_vendor": "",
                        "_rule_id": rule_id,
                        "_rule_version": 1,
                        "_confidence": 80,
                    })
                results.append(event)
    completed_at = detected_at or utc_now_iso()
    duration_ms = round((time.perf_counter() - scan_started) * 1000, 3)
    for event in results:
        event["detected_at"] = completed_at
        event["first_discovered_at"] = completed_at
        event["last_discovered_at"] = completed_at
        event["time_provenance"] = {
            "first_observed_at": time_entry(
                event.get("first_observed_at"),
                source="controlled_fixture_scan_started",
                meaning="受控夹具扫描开始并读取当前内容位置的时间。",
                confidence="high",
                evidence=event.get("source_locator") or "",
                role="system_observation",
            ),
            "detected_at": time_entry(
                completed_at,
                source="controlled_fixture_scan_completed",
                meaning="受控夹具全部规则扫描完成的时间；处理耗时使用单调时钟测量。",
                confidence="high",
                evidence=f"duration_ms={duration_ms}",
                role="detection_completion",
                eligible_for_latency=True,
            ),
            "first_discovered_at": time_entry(
                completed_at,
                source="controlled_fixture_scan_completed",
                meaning="受控夹具规则扫描实际完成并形成有效结果的时间。",
                confidence="high",
                evidence=f"{event.get('source_locator') or ''};duration_ms={duration_ms}",
                role="first_valid_detection",
                eligible_for_latency=True,
                evidence_scope="controlled_fixture",
            ),
        }
        event["_detection_duration_ms"] = duration_ms
    return results
