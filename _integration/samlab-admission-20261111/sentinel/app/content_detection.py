"""Convert scanned text into the project's masked EventRecord contract.

The detector never returns or persists a raw secret. Raw values exist only in
memory long enough to calculate the keyed fingerprint and masked preview.
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone
from typing import Iterable

from .controlled_detector import RULES as LEGACY_RULES
from .detection.engine import Candidate, DetectionEngine
from .detection.registry import get_registry
from .fingerprint import credential_fingerprint
from .masking import masked_preview
from .models import EventRecord
from .time_provenance import time_entry
from .time_utils import timestamp_precision, utc_now_iso


_ENGINE = DetectionEngine(get_registry())
_CRITICAL_TYPES = {
    "aws_access_key_id",
    "aws_secret_access_key",
    "aliyun_access_key_id",
    "aliyun_access_key_secret",
    "tencent_secret_id",
    "tencent_secret_key",
    "private_key_block",
    "gcp_service_account_json",
    "db_connection_url",
    "stripe_live_key",
    "openai_api_key",
    "anthropic_api_key",
}


def _now() -> str:
    return utc_now_iso()


def _mask_value(value: str, credential_type: str) -> str:
    return masked_preview(value, credential_type)


def _risk_level(candidate_type: str, confidence: int) -> str:
    if candidate_type in _CRITICAL_TYPES:
        return "critical"
    if confidence >= 70:
        return "high"
    if confidence >= 45:
        return "medium"
    return "low"


def _event_id(locator: str, rule_id: str, line_start: int, fingerprint: str) -> str:
    material = f"{locator}\x00{rule_id}\x00{line_start}\x00{fingerprint}"
    return f"DET-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:20].upper()}"


def _candidate_event(
    candidate: Candidate,
    *,
    source_kind: str,
    channel: str,
    platform: str,
    source_locator: str,
    first_observed_at: str,
    detected_at: str | None,
    hmac_key: str,
    fingerprint_key_version: str,
    include_internal: bool = False,
) -> dict:
    secret = candidate.secret
    fingerprint = credential_fingerprint(secret, hmac_key)
    evidence = candidate.evidence
    evidence_level = "L2" if any(
        key in evidence for key in ("context", "paired", "structured", "decoded_base64")
    ) else "L1"
    credential_type = candidate.type
    event = {
        "event_id": _event_id(source_locator, candidate.rule_id, candidate.line_start, fingerprint),
        "source_kind": source_kind,
        "channel": channel,
        "platform": platform,
        "credential_type": credential_type,
        "redacted_value": _mask_value(secret, credential_type),
        "evidence_level": evidence_level,
        "risk_level": _risk_level(credential_type, candidate.confidence),
        "disposition": "待确认",
        "first_observed_at": first_observed_at,
        "source_locator": f"{source_locator}:line-{candidate.line_start}",
        "detected_at": detected_at,
        "detection_method": f"规则引擎:{candidate.rule_id}@v{candidate.rule_version}",
        "credential_fingerprint": fingerprint,
        "fingerprint_key_version": fingerprint_key_version,
    }
    validated = EventRecord.model_validate(event).model_dump()
    if include_internal:
        validated.update({
            "_secret": secret,
            "_paired": candidate.evidence.get("paired") or {},
            "_vendor": candidate.vendor,
            "_rule_id": candidate.rule_id,
            "_rule_version": candidate.rule_version,
            "_confidence": candidate.confidence,
        })
    return validated


def _legacy_events(
    content: str,
    *,
    source_kind: str,
    channel: str,
    platform: str,
    source_locator: str,
    first_observed_at: str,
    detected_at: str | None,
    hmac_key: str,
    fingerprint_key_version: str,
    include_internal: bool = False,
) -> Iterable[dict]:
    for line_number, line in enumerate(content.splitlines(), start=1):
        matches = []
        for credential_type, pattern, method in LEGACY_RULES:
            match = pattern.search(line)
            if match:
                matches.append((credential_type, method, match.group(0)))
        if any(item[0] == "数据库连接串" for item in matches):
            matches = [item for item in matches if item[0] == "数据库连接串"]
        for credential_type, method, value in matches:
            fingerprint = credential_fingerprint(value, hmac_key)
            locator = f"{source_locator}:line-{line_number}"
            event = {
                "event_id": _event_id(locator, f"legacy-{credential_type}", line_number, fingerprint),
                "source_kind": source_kind,
                "channel": channel,
                "platform": platform,
                "credential_type": credential_type,
                "redacted_value": _mask_value(value, credential_type),
                "evidence_level": "L1",
                "risk_level": (
                    "critical"
                    if credential_type in {"AK/SK", "数据库连接串", "数据库密码"}
                    else "high"
                ),
                "disposition": "待确认",
                "first_observed_at": first_observed_at,
                "source_locator": locator,
                "detected_at": detected_at,
                "detection_method": method,
                "credential_fingerprint": fingerprint,
                "fingerprint_key_version": fingerprint_key_version,
            }
            validated = EventRecord.model_validate(event).model_dump()
            if include_internal:
                validated.update({
                    "_secret": value,
                    "_paired": {},
                    "_vendor": "",
                    "_rule_id": f"legacy-{credential_type}",
                    "_rule_version": 1,
                    "_confidence": 75,
                })
            yield validated


def detect_content(
    content: str,
    *,
    source_kind: str,
    channel: str,
    platform: str,
    source_locator: str,
    hmac_key: str,
    detected_at: str | None = None,
    first_observed_at: str | None = None,
    fingerprint_key_version: str = "v1",
    include_legacy_markers: bool = True,
    include_internal: bool = False,
    enabled_rule_ids: set[str] | frozenset[str] | None = None,
) -> list[dict]:
    """Scan one authorized text item and return masked event records.

    ``detected_at`` is retained only for deterministic callers that already
    possess a trusted completion timestamp. Normal production callers omit it;
    the function records the wall-clock time after the detector has finished.
    Processing duration is measured separately with ``perf_counter``.
    """
    if not hmac_key:
        raise ValueError("CREDENTIAL_HMAC_KEY is required for content detection")
    detection_started = time.perf_counter()
    observed_at = first_observed_at or _now()
    # Normalize platform line endings before regex and line-offset matching.
    # Connectors may decode Windows-authored files with CRLF endings.
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    # When a backend rule set is supplied, registry rules are authoritative.
    # Legacy markers remain available for direct compatibility callers that do
    # not yet participate in rule management.
    candidates = list(_ENGINE.scan(
        content,
        path=source_locator,
        enabled_rule_ids=enabled_rule_ids,
    ))
    # Detection completion is assigned only after every enabled detector has
    # finished. A caller-supplied value remains available for deterministic
    # tests and trusted upstream timestamps.
    results: list[dict] = []
    if include_legacy_markers and enabled_rule_ids is None:
        results.extend(
            _legacy_events(
                content,
                source_kind=source_kind,
                channel=channel,
                platform=platform,
                source_locator=source_locator,
                first_observed_at=observed_at,
                detected_at=detected_at,
                hmac_key=hmac_key,
                fingerprint_key_version=fingerprint_key_version,
                include_internal=include_internal,
            )
        )
    for candidate in candidates:
        results.append(
            _candidate_event(
                candidate,
                source_kind=source_kind,
                channel=channel,
                platform=platform,
                source_locator=source_locator,
                first_observed_at=observed_at,
                detected_at=detected_at,
                hmac_key=hmac_key,
                fingerprint_key_version=fingerprint_key_version,
                include_internal=include_internal,
            )
        )
    completed_at = detected_at or _now()
    detection_duration_ms = round((time.perf_counter() - detection_started) * 1000, 3)
    observed_source = "caller_supplied" if first_observed_at else "detection_started"
    detected_source = "caller_supplied" if detected_at else "detection_completed"
    observed_confidence = "medium" if first_observed_at else "high"
    detected_confidence = "medium" if detected_at else "high"
    for result in results:
        result["detected_at"] = completed_at
        result["first_discovered_at"] = completed_at
        result["last_discovered_at"] = completed_at
        method = str(result.get("detection_method") or "规则引擎")
        result["time_provenance"] = {
            "first_observed_at": time_entry(
                result.get("first_observed_at"),
                source=observed_source,
                meaning="本系统首次观测到当前内容位置并开始处理的时间。",
                confidence=observed_confidence,
                evidence=source_locator,
                role="system_observation",
                eligible_for_latency=False,
            ),
            "detected_at": time_entry(
                result.get("detected_at"),
                source=detected_source,
                meaning="规则检测实际完成的时间；处理耗时使用单调时钟单独测量。",
                confidence=detected_confidence,
                evidence=f"{method};duration_ms={detection_duration_ms}",
                role="detection_completion",
                eligible_for_latency=True,
            ),
            "first_discovered_at": time_entry(
                result.get("first_discovered_at"),
                source=detected_source,
                meaning=(
                    "规则引擎实际形成有效凭据结果的时间；"
                    "当前检测引擎按内容级完成粒度记录。"
                ),
                confidence=detected_confidence,
                evidence=f"{method};duration_ms={detection_duration_ms}",
                role="first_valid_detection",
                eligible_for_latency=True,
                precision=timestamp_precision(result.get("first_discovered_at")),
                evidence_scope="content_item",
            ),
        }
        result["_detection_duration_ms"] = detection_duration_ms
    unique: dict[str, dict] = {}
    for item in results:
        unique[item["event_id"]] = item
    return list(unique.values())
