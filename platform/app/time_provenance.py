"""时间字段的来源、含义、可信度和补写合并规则。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


REFERENCE_ONLY_PUBLIC_TIME_SOURCES = frozenset(
    {
        "mediawiki_revision_timestamp",
        "github_commit",
        "gitee_commit",
        "stackoverflow_creation_date",
        "stackoverflow_answer_creation_date",
        "sitemap_lastmod",
        "weibo_page_metadata",
        "image_config_created",
        "pypi_upload_time",
    }
)


def time_entry(
    value: str | None,
    *,
    source: str,
    meaning: str,
    confidence: str = "none",
    evidence: str = "",
    role: str = "unknown",
    eligible_for_latency: bool = False,
    unknown_reason: str = "",
    backfill_reason: str = "",
    precision: str = "unknown",
    evidence_scope: str = "",
    content_version: str = "",
) -> dict[str, Any]:
    """Build a secret-free, explainable time record."""
    return {
        "value": value,
        "source": source,
        "meaning": meaning,
        "confidence": confidence,
        "evidence": evidence,
        "role": role,
        "eligible_for_latency": bool(eligible_for_latency),
        "unknown_reason": unknown_reason,
        "backfill_reason": backfill_reason,
        "precision": precision,
        "evidence_scope": evidence_scope,
        "content_version": content_version,
    }


def _confidence_rank(value: Any) -> int:
    return {
        "none": 0,
        "low": 1,
        "medium": 2,
        "high": 3,
    }.get(str(value or "none").strip().lower(), 0)


def _can_upgrade_time_entry(
    current: dict[str, Any],
    candidate: dict[str, Any],
) -> bool:
    """Allow a same-scope legacy/reference value to gain stronger evidence.

    The normal merge rule is intentionally conservative: an existing timestamp
    is never replaced merely because a later scan has another timestamp. A
    stronger, eligible candidate may replace a value that was explicitly stored
    as a reference or historical value, provided both entries identify the same
    content version when that identity is available.
    """
    current_value = current.get("value")
    candidate_value = candidate.get("value")
    if not current_value or not candidate_value:
        return False
    current_version = str(current.get("content_version") or "").strip()
    candidate_version = str(candidate.get("content_version") or "").strip()
    if current_version and candidate_version and current_version != candidate_version:
        return False
    if bool(current.get("eligible_for_latency")):
        return False
    if not bool(candidate.get("eligible_for_latency")):
        return False
    current_role = str(current.get("role") or "unknown")
    if current_role not in {
        "unknown",
        "content_revision_reference",
        "historical_first_valid_detection",
    } and not current.get("backfill_reason"):
        return False
    candidate_role = str(candidate.get("role") or "unknown")
    if candidate_role == "unknown":
        return False
    return (
        _confidence_rank(candidate.get("confidence"))
        >= _confidence_rank(current.get("confidence"))
        or bool(candidate.get("evidence"))
    )


def merge_time_provenance(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
) -> dict[str, Any]:
    """Fill missing time evidence without replacing an existing value.

    A later scan may add provenance for an older row that was initially
    incomplete. It must never replace a value already persisted for that
    content version.
    """
    merged = deepcopy(existing) if isinstance(existing, dict) else {}
    for field, candidate in (incoming or {}).items():
        if not isinstance(candidate, dict):
            continue
        current = merged.get(field)
        if not isinstance(current, dict):
            merged[field] = deepcopy(candidate)
            continue
        current_value = current.get("value")
        candidate_value = candidate.get("value")
        if not current_value and candidate_value:
            merged[field] = deepcopy(candidate)
            continue
        if _can_upgrade_time_entry(current, candidate):
            merged[field] = deepcopy(candidate)
            continue
        for key, value in candidate.items():
            # An unknown record from a later scan must not attach its
            # unknown_reason to an already known value for the same version.
            if current_value and not candidate_value and key == "unknown_reason":
                continue
            # A low-confidence legacy description must not attach an
            # unknown-reason marker to an evidence-grade timestamp. The
            # marker describes a missing or unverifiable value, so keeping it
            # on a valid detection/public-time entry would make provenance
            # internally contradictory.
            if (
                current_value
                and key == "unknown_reason"
                and current.get("eligible_for_latency")
            ):
                continue
            # Re-running the legacy description migration must not attach its
            # backfill marker to a current evidence-grade timestamp.
            if (
                current_value
                and key == "backfill_reason"
                and not current.get("backfill_reason")
                and current.get("eligible_for_latency")
            ):
                continue
            if key not in current or current.get(key) in (None, "", "none"):
                current[key] = value
    return merged


def latency_eligible(provenance: dict[str, Any] | None, field: str = "published_at") -> bool:
    entry = (provenance or {}).get(field)
    if not isinstance(entry, dict) or not entry.get("value"):
        return False
    source = str(entry.get("source") or "")
    return bool(
        entry.get("eligible_for_latency")
        and source not in REFERENCE_ONLY_PUBLIC_TIME_SOURCES
        and not entry.get("backfill_reason")
    )


def reference_time_eligible(
    provenance: dict[str, Any] | None,
    field: str = "published_at",
) -> bool:
    """Return whether a non-public reference timestamp can form a separate interval.

    A reference timestamp is useful for measuring ``reference time -> detection
    completion`` even when it cannot prove the credential was already present
    at that timestamp. It must still have a value and a meaningful provenance
    entry; it is never included in the public-exposure latency population.
    """
    entry = (provenance or {}).get(field)
    if not isinstance(entry, dict) or not entry.get("value"):
        return False
    if entry.get("role") == "unknown" or entry.get("source") in {"", "not_provided"}:
        return False
    return True


def detection_time_eligible(provenance: dict[str, Any] | None) -> bool:
    """Return whether a detection-completion timestamp has process evidence."""
    entry = (provenance or {}).get("detected_at")
    if not isinstance(entry, dict) or not entry.get("value"):
        return False
    if (
        entry.get("role") != "detection_completion"
        or entry.get("source") not in {"detection_completed", "caller_supplied"}
        or not entry.get("eligible_for_latency")
        or entry.get("confidence") not in {"high", "medium"}
    ):
        return False
    # A legacy migration marker is acceptable only when the row still carries
    # the actual detector evidence (for example duration_ms=...).
    return not entry.get("backfill_reason") or bool(entry.get("evidence"))


def first_discovery_eligible(provenance: dict[str, Any] | None) -> bool:
    """Return whether a first-discovery timestamp is evidence-grade."""
    entry = (provenance or {}).get("first_discovered_at")
    if not isinstance(entry, dict) or not entry.get("value"):
        return False
    return bool(
        entry.get("eligible_for_latency")
        and entry.get("role") == "first_valid_detection"
        and entry.get("source") not in {"", "not_provided", "occurrence_storage"}
        and not entry.get("backfill_reason")
    )


def legacy_published_entry(
    value: str | None,
    source: str | None,
    confidence: str | None,
) -> dict[str, Any]:
    """Describe old published_at rows without inventing missing evidence."""
    normalized_source = str(source or "").strip()
    normalized_confidence = str(confidence or "none").strip() or "none"
    if value and normalized_source:
        eligible = (
            normalized_confidence in {"high", "medium"}
            and normalized_source not in REFERENCE_ONLY_PUBLIC_TIME_SOURCES
        )
        return time_entry(
            value,
            source=normalized_source,
            meaning="历史连接器提供的来源公开/内容时间；不等于凭据首次泄露时间。",
            confidence=normalized_confidence,
            role="public_content_time" if eligible else "content_revision_reference",
            eligible_for_latency=eligible,
            backfill_reason="由已有 source_contents.published_at、published_source 和 published_confidence 补写。",
            precision="unknown",
            evidence_scope="legacy_source_contents_row",
        )
    return time_entry(
        value,
        source="not_provided",
        meaning="来源没有提供可验证的公开或内容时间。",
        confidence="none",
        role="unknown",
        eligible_for_latency=False,
        unknown_reason="connector_not_provided",
        backfill_reason="历史记录缺少公开时间来源，保持未知。",
        precision="unknown",
        evidence_scope="legacy_source_contents_row",
    )
