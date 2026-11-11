from datetime import datetime, timezone
from typing import Any


def build_evidence_package(event: dict[str, Any], alerts: list[dict], feedbacks: list[dict]) -> dict[str, Any]:
    """Build the minimum auditable package without accepting raw secret fields."""
    allowed_event_fields = (
        "event_id",
        "source_kind",
        "channel",
        "platform",
        "credential_type",
        "redacted_value",
        "evidence_level",
        "risk_level",
        "disposition",
        "first_observed_at",
        "first_discovered_at",
        "last_discovered_at",
        "source_locator",
        "detected_at",
        "detection_method",
        "credential_fingerprint",
        "fingerprint_key_version",
        "published_at",
        "published_source",
        "published_confidence",
        "public_time_status",
        "public_time_unknown_reason",
        "published_meaning",
        "published_evidence",
        "discovery_latency_seconds",
        "discovery_latency_status",
        "discovery_latency_reason",
        "discovery_latency_calculation_type",
        "discovery_latency_basis",
        "discovery_latency_end_at",
        "fetched_at",
        "fetched_at_source",
        "first_observed_at",
        "public_confirmed_at",
        "run_id",
        "evidence_status",
    )
    safe_event = {field: event.get(field) for field in allowed_event_fields}
    safe_alerts = [
        {
            "alert_id": item["alert_id"],
            "event_id": item["event_id"],
            "severity": item["severity"],
            "status": item["status"],
            "channel": item["channel"],
            "created_at": item["created_at"],
            "delivered_at": item["delivered_at"],
            "delivery_note": item["delivery_note"],
        }
        for item in alerts
    ]
    safe_feedbacks = [
        {
            "feedback_id": item["feedback_id"],
            "event_id": item["event_id"],
            "action": item["action"],
            "disposition": item["disposition"],
            "created_at": item["created_at"],
            "note": item["note"],
        }
        for item in feedbacks
    ]
    return {
        "schema_version": "1.0",
        "package_type": "脱敏事件证据包",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_boundary": {
            "raw_secret_included": False,
            "source_kind_preserved": True,
            "public_exposure_status": (
                "已确认"
                if safe_event["source_kind"] == "真实检测结果"
                and safe_event["disposition"] == "已确认公开泄露"
                else "待确认"
            ),
            "note": "来源标签和证据等级保留；脱敏导出不证明凭据仍有效。",
        },
        "event": safe_event,
        "alerts": safe_alerts,
        "feedbacks": safe_feedbacks,
    }


def build_run_evidence_package(
    report: dict[str, Any],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Create a portable, masked evidence package from the run evidence report."""
    runs = report.get("runs") if isinstance(report, dict) else []
    checks = report.get("checks") if isinstance(report, dict) else []
    artifacts = report.get("artifacts") if isinstance(report, dict) else []
    safe_runs = []
    for run in runs if isinstance(runs, list) else []:
        metadata = run.get("metadata") or {}
        safe_hits = []
        for hit in metadata.get("credential_hits", [])[:500]:
            safe_hits.append({
                key: hit.get(key)
                for key in (
                    "event_id", "credential_type", "rule_id", "platform", "source_kind",
                    "source_locator", "fingerprint_prefix", "published_at",
                    "published_source", "published_confidence", "public_time_status",
                    "public_time_unknown_reason", "first_observed_at", "fetched_at",
                    "first_discovered_at", "last_discovered_at", "detected_at",
                    "discovery_latency_seconds", "discovery_latency_status",
                    "discovery_latency_reason", "discovery_latency_calculation_type",
                    "discovery_latency_basis", "discovery_latency_end_at",
                )
            })
        safe_runs.append({
            key: run.get(key)
            for key in (
                "run_id", "collector_key", "channel", "platform", "source_name",
                "target_digest", "status", "started_at", "finished_at", "candidates",
                "inserted_count", "duplicate_count", "filtered_count", "new_credentials",
                "retry_count", "rate_limit_count", "checkpoint_status",
                "recovery_status", "idempotency_status", "evidence_status", "error_code",
            )
        } | {
            "authorization_scope": run.get("authorization_scope") or {},
            "credential_type_counts": metadata.get("credential_type_counts") or {},
            "latency": metadata.get("latency") or {},
            "credential_hits": safe_hits,
            "checks": run.get("checks") or [],
        })
    safe_checks = [
        {
            key: check.get(key)
            for key in (
                "run_id", "check_type", "status", "expected", "actual",
                "evidence_ref", "created_at",
            )
        } | {"metadata": check.get("metadata") or {}}
        for check in checks if isinstance(check, dict)
    ]
    safe_artifacts = [
        {
            key: artifact.get(key)
            for key in (
                "artifact_id", "run_id", "artifact_type", "relative_path",
                "sha256", "record_count", "source_kind", "created_at",
            )
        }
        for artifact in artifacts if isinstance(artifact, dict)
    ]
    return {
        "schema_version": "2.0",
        "package_type": "脱敏运行证据包",
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "data_boundary": {
            "raw_secret_included": False,
            "full_credential_value_included": False,
            "hmac_fingerprint_prefix_only": True,
            "controlled_and_synthetic_excluded_from_public_metric": True,
            "note": "该包证明运行链路和脱敏结果，不证明凭据仍有效，也不把采集时间冒充公开时间。",
        },
        "summary": report.get("summary") if isinstance(report, dict) else {},
        "definitions": report.get("definitions") if isinstance(report, dict) else {},
        "runs": safe_runs,
        "checks": safe_checks,
        "artifacts": safe_artifacts,
    }
