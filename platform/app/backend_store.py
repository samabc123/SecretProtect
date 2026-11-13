"""统一的后端 SQLite 存储层。

该模块是现有页面级 Store 之外的规范化后端数据层，按来源、任务、扫描批次、
来源内容、凭据实体和出现位置建立关系。默认只返回脱敏数据；敏感字段使用
本地 Fernet 密钥加密，且没有原文时不会伪造可恢复的凭据值。
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import math
import re
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from cryptography.fernet import Fernet, InvalidToken

from .detection.registry import get_registry
from .controlled_detector import controlled_rule_specs
from .config import settings
from .time_utils import timestamp_precision, utc_now_iso
from .models import EventRecord
from .fingerprint import credential_fingerprint
from .settings_service import effective_settings
from .time_provenance import (
    REFERENCE_ONLY_PUBLIC_TIME_SOURCES,
    detection_time_eligible,
    first_discovery_eligible,
    latency_eligible,
    legacy_published_entry,
    merge_time_provenance,
    reference_time_eligible,
    time_entry,
)
from .verification_engine import (
    AUTHORIZED_ENDPOINT_VERIFIER_PREFIX,
    authorized_endpoint_by_id,
    authorized_endpoint_for_type,
    controlled_verification_outcome,
    load_controlled_verification_fingerprints,
    verifier_for_type,
    verifier_specs,
    verify_credential,
)
from .connectors import list_connector_specs
from .notification_adapters import OfflineSQLiteNotificationAdapter


T = TypeVar("T")


class BackendStorageError(RuntimeError):
    """只包含安全错误码，不回显数据库内容或敏感输入。"""


class BackendValidationError(ValueError):
    """后端输入契约错误。"""


def _now() -> str:
    return utc_now_iso()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _iso(value: str | None) -> str | None:
    return value


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[index], 3)


def _format_report_metric(metric: dict[str, Any]) -> str:
    value = metric.get("p50_seconds")
    sample_count = metric.get("sample_count", 0)
    if value is None:
        return f"未知（可计算样本 {sample_count} 条）"
    rounded = max(0, round(float(value)))
    if rounded < 60:
        display = f"{rounded}秒"
    else:
        minutes = rounded // 60
        seconds = rounded % 60
        if minutes < 60:
            display = f"{minutes}分{seconds}秒" if seconds else f"{minutes}分"
        else:
            hours = minutes // 60
            remaining_minutes = minutes % 60
            if hours < 24:
                display = (
                    f"{hours}小时{remaining_minutes}分"
                    if remaining_minutes
                    else f"{hours}小时"
                )
            else:
                days = hours // 24
                remaining_hours = hours % 24
                if days < 365:
                    display = (
                        f"{days}天{remaining_hours}小时"
                        if remaining_hours
                        else f"{days}天"
                    )
                else:
                    years = days // 365
                    remaining_days = days % 365
                    display = (
                        f"{years}年{remaining_days}天"
                        if remaining_days
                        else f"{years}年"
                    )
    return f"{display}（样本 {sample_count} 条）"


def _row_json(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    """Load an optional JSON column without assuming every legacy query has it."""
    if key not in row.keys():
        return default
    return _load(row[key], default)


def _row_time_provenance(row: sqlite3.Row) -> dict[str, Any]:
    """Combine occurrence and content-version time evidence.

    Occurrence evidence is applied first so a later content-version backfill can
    fill missing fields without replacing a value already attached to the
    occurrence. The helper also describes old rows that predate provenance JSON.
    """
    merged = merge_time_provenance(
        _row_json(row, "occurrence_time_provenance_json", {}),
        _row_json(row, "content_time_provenance_json", {}),
    )
    published_at = row["published_at"] if "published_at" in row.keys() else None
    published_source = row["published_source"] if "published_source" in row.keys() else ""
    published_confidence = row["published_confidence"] if "published_confidence" in row.keys() else "none"
    if "published_at" not in merged:
        merged = merge_time_provenance(
            merged,
            {"published_at": legacy_published_entry(
                published_at,
                published_source,
                published_confidence,
            )},
        )
    fetched_at = row["fetched_at"] if "fetched_at" in row.keys() else None
    fetched_source = row["fetched_at_source"] if "fetched_at_source" in row.keys() else "unknown"
    if "fetched_at" not in merged:
        merged = merge_time_provenance(
            merged,
            {"fetched_at": time_entry(
                fetched_at,
                source=str(fetched_source or "unknown"),
                meaning=(
                    "连接器完成读取当前内容的时间。"
                    if fetched_source == "connector"
                    else "历史记录没有连接器明确采集时间，保持为不可用于精确采集时效的参考值。"
                ),
                confidence="high" if fetched_source == "connector" else "low",
                role="acquisition_completion",
                eligible_for_latency=fetched_source == "connector" and bool(fetched_at),
                unknown_reason=(
                    "" if fetched_source == "connector" and fetched_at
                    else "acquisition_time_not_provided"
                ),
            )},
        )
    if "first_observed_at" not in merged and "first_observed_at" in row.keys():
        merged = merge_time_provenance(
            merged,
            {"first_observed_at": time_entry(
                row["first_observed_at"],
                source="occurrence_storage",
                meaning="本系统记录的当前内容位置首次观测时间。",
                confidence="medium" if row["first_observed_at"] else "none",
                role="system_observation",
                unknown_reason="observation_time_not_provided" if not row["first_observed_at"] else "",
            )},
        )
    if "detected_at" not in merged and "detected_at" in row.keys():
        merged = merge_time_provenance(
            merged,
            {"detected_at": time_entry(
                row["detected_at"],
                source="occurrence_storage",
                meaning="规则检测完成时间；处理耗时不由墙上时钟推导。",
                confidence="medium" if row["detected_at"] else "none",
                role="detection_completion",
                unknown_reason="detection_time_not_provided" if not row["detected_at"] else "",
            )},
        )
    if "first_discovered_at" not in merged and "first_discovered_at" in row.keys():
        value = row["first_discovered_at"]
        merged = merge_time_provenance(
            merged,
            {"first_discovered_at": time_entry(
                value,
                source="occurrence_storage" if value else "not_provided",
                meaning=(
                    "系统第一次实际识别出该凭据的时间。"
                    if value else "历史记录没有可证明的系统首次发现时间。"
                ),
                confidence="medium" if value else "none",
                role="first_valid_detection" if value else "unknown",
                eligible_for_latency=False,
                unknown_reason=(
                    "historical_first_discovery_time_unverified"
                    if value
                    else "first_discovery_time_not_saved"
                ),
                backfill_reason=(
                    "旧记录只有 occurrences.first_discovered_at 数值，缺少当前检测过程证据。"
                    if value
                    else ""
                ),
            )},
        )
    return merged


def _public_time_fields(provenance: dict[str, Any]) -> dict[str, Any]:
    entry = provenance.get("published_at") if isinstance(provenance, dict) else None
    entry = entry if isinstance(entry, dict) else {}
    value = entry.get("value")
    eligible = latency_eligible(provenance)
    return {
        "public_time_status": (
            "known" if eligible else "reference_only" if value else "unknown"
        ),
        "public_time_unknown_reason": (
            "" if value else str(entry.get("unknown_reason") or "connector_not_provided")
        ),
        "published_meaning": str(entry.get("meaning") or ""),
        "published_evidence": str(entry.get("evidence") or ""),
        "published_precision": str(entry.get("precision") or "unknown"),
        "published_evidence_scope": str(entry.get("evidence_scope") or ""),
        "published_content_version": str(entry.get("content_version") or ""),
    }


def _reference_latency_basis(entry: dict[str, Any]) -> str:
    source = str(entry.get("source") or "").strip()
    source_labels = {
        "stackoverflow_creation_date": "Stack Overflow 问题创建时间",
        "stackoverflow_answer_creation_date": "Stack Overflow 回答创建时间",
        "github_commit": "GitHub 匹配版本提交时间",
        "gitee_commit": "Gitee 匹配版本提交时间",
        "pypi_upload_time": "PyPI 分发文件上传时间",
        "sitemap_lastmod": "Sitemap 最后修改时间",
        "weibo_page_metadata": "微博页面元数据时间",
        "mediawiki_revision_timestamp": "MediaWiki 修订时间",
    }
    if source in source_labels:
        return f"按{source_labels[source]}计算"
    if source:
        return f"按来源时间（{source}）计算"
    return "按内容参考时间计算"


def _discovery_time_chain(row: sqlite3.Row) -> dict[str, Any]:
    """Calculate one occurrence-scoped time chain without mixing evidence classes.

    A proven public timestamp is compared with the first valid discovery.
    A content or revision timestamp is compared with detection completion and
    is explicitly returned as a reference interval. Neither path uses the
    other source or occurrence to fill a missing value.
    """
    provenance = _row_time_provenance(row)
    public_entry = provenance.get("published_at") or {}
    discovery_entry = provenance.get("first_discovered_at") or {}
    detected_entry = provenance.get("detected_at") or {}
    public_value = public_entry.get("value")
    discovery_value = (
        row["first_discovered_at"]
        if "first_discovered_at" in row.keys() and row["first_discovered_at"]
        else discovery_entry.get("value")
    )
    detected_value = (
        row["detected_at"]
        if "detected_at" in row.keys() and row["detected_at"]
        else detected_entry.get("value")
    )
    public_time = _parse_time(public_value)
    discovery_time = _parse_time(discovery_value)
    detected_time = _parse_time(detected_value)
    public_eligible = latency_eligible(provenance, "published_at")
    discovery_eligible = first_discovery_eligible(provenance)
    reference_eligible = reference_time_eligible(provenance, "published_at")
    detection_eligible = detection_time_eligible(provenance)
    status = "not_calculable"
    reason = ""
    latency_seconds: float | None = None
    calculation_type = "not_calculable"
    basis = ""
    end_at = None
    if not public_value:
        reason = str(public_entry.get("unknown_reason") or "public_time_not_provided")
    else:
        if public_eligible:
            if not discovery_value:
                reason = str(
                    discovery_entry.get("unknown_reason")
                    or "first_discovery_time_not_saved"
                )
            elif not discovery_eligible:
                reason = str(
                    discovery_entry.get("unknown_reason")
                    or "historical_first_discovery_time_unverified"
                )
            elif public_time is None or discovery_time is None:
                reason = "timestamp_invalid_or_timezone_missing"
            else:
                latency_seconds = round((discovery_time - public_time).total_seconds(), 3)
                calculation_type = "public_to_first_discovery"
                basis = "公开时间 → 系统首次发现时间"
                end_at = discovery_value
                if latency_seconds < 0:
                    reason = "discovery_before_public_time"
                    latency_seconds = None
                    calculation_type = "not_calculable"
                    basis = ""
                    end_at = None
                else:
                    status = "calculated"
        elif reference_eligible:
            if not detected_value:
                reason = str(
                    detected_entry.get("unknown_reason")
                    or "detection_time_not_saved"
                )
            elif not detection_eligible:
                reason = str(
                    detected_entry.get("unknown_reason")
                    or "detection_time_evidence_unverified"
                )
            elif public_time is None or detected_time is None:
                reason = "timestamp_invalid_or_timezone_missing"
            else:
                latency_seconds = round((detected_time - public_time).total_seconds(), 3)
                calculation_type = "reference_to_detection"
                basis = _reference_latency_basis(public_entry)
                end_at = detected_value
                if latency_seconds < 0:
                    reason = "detection_before_reference_time"
                    latency_seconds = None
                    calculation_type = "not_calculable"
                    basis = ""
                    end_at = None
                else:
                    status = "calculated"
        else:
            reason = "public_time_is_reference_only"
    return {
        "public_time": public_value,
        "public_time_status": (
            "known"
            if public_value and public_eligible
            else "reference_only" if public_value else "unknown"
        ),
        "public_time_unknown_reason": (
            "" if public_value
            else str(public_entry.get("unknown_reason") or "public_time_not_provided")
        ),
        "first_discovered_at": discovery_value,
        "first_discovery_time_status": "known" if discovery_eligible else "unknown",
        "first_discovery_unknown_reason": (
            ""
            if discovery_eligible
            else str(
                discovery_entry.get("unknown_reason")
                or "first_discovery_time_not_saved"
            )
        ),
        "discovery_latency_seconds": latency_seconds,
        "discovery_latency_status": status,
        "discovery_latency_reason": reason,
        "discovery_latency_calculation_type": calculation_type,
        "discovery_latency_basis": basis,
        "discovery_latency_end_at": end_at,
        "time_provenance": provenance,
    }


class BackendStore:
    """统一规范化后端的 SQLite 实现。

    事务边界由每个公开写方法控制；读取使用只读语义但会在缺失数据库时返回
    明确错误，不会自动回退到 mock 数据。
    """

    def __init__(
        self,
        path: Path,
        *,
        master_key_path: Path | None = None,
        timeout: float = 1.0,
        attempts: int = 4,
    ):
        self.path = Path(path).expanduser().resolve()
        self.master_key_path = (
            Path(master_key_path).expanduser().resolve()
            if master_key_path
            else self.path.with_name("backend.master.key")
        )
        self.timeout = timeout
        self.attempts = max(1, attempts)

    def _connect(self, writable: bool = False) -> sqlite3.Connection:
        if writable:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise BackendStorageError("backend_database_unavailable") from exc
        if not writable and not self.path.exists():
            raise BackendStorageError("backend_database_missing")
        try:
            connection = sqlite3.connect(self.path, timeout=self.timeout)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            if writable:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
            return connection
        except (sqlite3.DatabaseError, OSError) as exc:
            raise BackendStorageError("backend_database_invalid") from exc

    def _execute(self, operation: Callable[[sqlite3.Connection], T], writable: bool = False) -> T:
        for attempt in range(self.attempts):
            try:
                with closing(self._connect(writable=writable)) as connection:
                    with connection:
                        return operation(connection)
            except BackendStorageError:
                raise
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", None)
                locked = code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                if locked and attempt + 1 < self.attempts:
                    time.sleep(0.08 * (2**attempt))
                    continue
                raise BackendStorageError(
                    "backend_database_locked" if locked else "backend_database_unavailable"
                ) from None
            except (sqlite3.DatabaseError, OSError) as exc:
                raise BackendStorageError("backend_database_invalid") from exc
        raise BackendStorageError("backend_database_unavailable")

    def _fernet(self) -> Fernet:
        try:
            self.master_key_path.parent.mkdir(parents=True, exist_ok=True)
            if self.master_key_path.is_file():
                key = self.master_key_path.read_bytes().strip()
            else:
                key = Fernet.generate_key()
                self.master_key_path.write_bytes(key)
            return Fernet(key)
        except (OSError, ValueError) as exc:
            raise BackendStorageError("backend_master_key_unavailable") from exc

    def _encrypt(self, value: str | None) -> str | None:
        if not value:
            return None
        return self._fernet().encrypt(value.encode("utf-8")).decode("ascii")

    def _decrypt(self, value: str | None) -> str | None:
        if not value:
            return None
        try:
            return self._fernet().decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError):
            raise BackendStorageError("backend_ciphertext_invalid") from None

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            platform TEXT NOT NULL,
            collector_key TEXT NOT NULL,
            config_json TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            mode TEXT NOT NULL DEFAULT 'manual',
            status TEXT NOT NULL DEFAULT 'pending',
            interval_sec INTEGER NOT NULL DEFAULT 300,
            next_run_at TEXT,
            last_run_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scan_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'running',
            trigger TEXT NOT NULL DEFAULT 'manual',
            stats_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NOT NULL,
            finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS cursors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            cursor_key TEXT NOT NULL,
            cursor_value TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(task_id, cursor_key)
        );
        CREATE TABLE IF NOT EXISTS source_contents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            run_id INTEGER REFERENCES scan_runs(id) ON DELETE SET NULL,
            kind TEXT NOT NULL DEFAULT 'event',
            platform TEXT NOT NULL,
            origin_url TEXT,
            repo TEXT,
            path TEXT NOT NULL,
            version_id TEXT NOT NULL,
            version_kind TEXT NOT NULL DEFAULT 'none',
            content_hash TEXT NOT NULL,
            content_excerpt TEXT,
            content_truncated INTEGER NOT NULL DEFAULT 1,
            size INTEGER NOT NULL DEFAULT 0,
            published_at TEXT,
            published_source TEXT,
            published_confidence TEXT NOT NULL DEFAULT 'none',
            source_updated_at TEXT,
            fetched_at TEXT,
            fetched_at_source TEXT NOT NULL DEFAULT 'unknown',
            time_provenance_json TEXT NOT NULL DEFAULT '{}',
            detection_status TEXT NOT NULL DEFAULT 'done',
            UNIQUE(source_id, version_id, path, content_hash)
        );
        CREATE TABLE IF NOT EXISTS credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL UNIQUE,
            type TEXT NOT NULL,
            vendor TEXT NOT NULL DEFAULT '',
            rule_id TEXT NOT NULL DEFAULT '',
            rule_version INTEGER NOT NULL DEFAULT 1,
            confidence INTEGER NOT NULL DEFAULT 0,
            secret_encrypted TEXT,
            paired_encrypted TEXT,
            preview TEXT NOT NULL DEFAULT '',
            secret_available INTEGER NOT NULL DEFAULT 0,
            review_status TEXT NOT NULL DEFAULT 'pending',
            verification_status TEXT NOT NULL DEFAULT 'not_requested',
            verified_at TEXT,
            first_seen_at TEXT NOT NULL,
            first_discovered_at TEXT,
            last_discovered_at TEXT,
            last_seen_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS occurrences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            credential_id INTEGER NOT NULL REFERENCES credentials(id) ON DELETE CASCADE,
            content_id INTEGER REFERENCES source_contents(id) ON DELETE SET NULL,
            run_id INTEGER REFERENCES scan_runs(id) ON DELETE SET NULL,
            source_event_id TEXT,
            category TEXT NOT NULL,
            platform TEXT NOT NULL,
            origin_url TEXT,
            repo TEXT,
            path TEXT NOT NULL,
            line_start INTEGER,
            version_id TEXT,
            version_kind TEXT NOT NULL DEFAULT 'none',
            context_masked TEXT,
            content_hash TEXT,
            location_hash TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            time_provenance_json TEXT NOT NULL DEFAULT '{}',
            detected_at TEXT NOT NULL,
            first_discovered_at TEXT,
            last_discovered_at TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE(credential_id, location_hash)
        );
        CREATE TABLE IF NOT EXISTS review_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            credential_id INTEGER NOT NULL REFERENCES credentials(id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            note TEXT,
            actor TEXT NOT NULL DEFAULT 'local',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS verification_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            credential_id INTEGER NOT NULL REFERENCES credentials(id) ON DELETE CASCADE,
            verifier_id TEXT NOT NULL,
            requested_by TEXT NOT NULL DEFAULT 'single',
            status TEXT NOT NULL DEFAULT 'queued',
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS verification_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER REFERENCES verification_jobs(id) ON DELETE SET NULL,
            credential_id INTEGER NOT NULL REFERENCES credentials(id) ON DELETE CASCADE,
            verifier_id TEXT NOT NULL,
            verifier_version TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            latency_ms INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rule_records (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            type TEXT NOT NULL,
            vendor TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 1,
            license TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            builtin INTEGER NOT NULL DEFAULT 1,
            verifier_id TEXT,
            file_sha TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ignore_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_type TEXT NOT NULL,
            scope_value TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS capabilities (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            key TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'planned',
            meta_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notification_outbox (
            notification_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL UNIQUE,
            event_schema_version TEXT NOT NULL DEFAULT '1.0',
            severity TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            channel TEXT NOT NULL DEFAULT 'offline-sqlite',
            created_at TEXT NOT NULL,
            delivered_at TEXT,
            delivery_note TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            next_attempt_at TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS extensibility_experiments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_key TEXT NOT NULL UNIQUE,
            connector_key TEXT NOT NULL,
            title TEXT NOT NULL,
            baseline_connector TEXT NOT NULL DEFAULT '',
            code_lines INTEGER,
            config_fields INTEGER,
            development_hours REAL,
            test_duration_ms INTEGER,
            execution_status TEXT NOT NULL DEFAULT 'planned',
            offline_tested INTEGER NOT NULL DEFAULT 0,
            checkpoint_verified INTEGER NOT NULL DEFAULT 0,
            failure_recovery_verified INTEGER NOT NULL DEFAULT 0,
            evidence_ref TEXT NOT NULL DEFAULT '',
            experiment_date TEXT,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS support_matrix (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_kind TEXT NOT NULL,
            item_key TEXT NOT NULL UNIQUE,
            channel_type TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL DEFAULT '',
            public_carrier TEXT NOT NULL DEFAULT '',
            permission_condition TEXT NOT NULL DEFAULT '',
            compliance_constraint TEXT NOT NULL DEFAULT '',
            collection_method TEXT NOT NULL DEFAULT '',
            parse_method TEXT NOT NULL DEFAULT '',
            polling_method TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '计划支持',
            evidence_ref TEXT NOT NULL DEFAULT '',
            real_tested INTEGER NOT NULL DEFAULT 0,
            credential_type TEXT NOT NULL DEFAULT '',
            rule_id TEXT NOT NULL DEFAULT '',
            vendor TEXT NOT NULL DEFAULT '',
            format_variant TEXT NOT NULL DEFAULT '',
            paired_required INTEGER NOT NULL DEFAULT 0,
            supports_verification INTEGER NOT NULL DEFAULT 0,
            tested_at TEXT,
            test_run_id INTEGER,
            test_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS security_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL,
            role TEXT NOT NULL,
            action TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_id TEXT NOT NULL DEFAULT '',
            decision TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            source_ip TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS run_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL UNIQUE REFERENCES scan_runs(id) ON DELETE CASCADE,
            collector_key TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL DEFAULT '',
            source_name TEXT NOT NULL DEFAULT '',
            authorization_scope_json TEXT NOT NULL DEFAULT '{}',
            target_digest TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            candidates INTEGER NOT NULL DEFAULT 0,
            inserted_count INTEGER NOT NULL DEFAULT 0,
            duplicate_count INTEGER NOT NULL DEFAULT 0,
            filtered_count INTEGER NOT NULL DEFAULT 0,
            new_credentials INTEGER NOT NULL DEFAULT 0,
            retry_count INTEGER NOT NULL DEFAULT 0,
            rate_limit_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_status TEXT NOT NULL DEFAULT 'not_applicable',
            recovery_status TEXT NOT NULL DEFAULT 'not_exercised',
            idempotency_status TEXT NOT NULL DEFAULT 'not_exercised',
            evidence_status TEXT NOT NULL DEFAULT 'partial',
            error_code TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evidence_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
            check_type TEXT NOT NULL,
            status TEXT NOT NULL,
            expected TEXT NOT NULL DEFAULT '',
            actual TEXT NOT NULL DEFAULT '',
            evidence_ref TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(run_id, check_type)
        );
        CREATE TABLE IF NOT EXISTS evidence_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artifact_id TEXT NOT NULL UNIQUE,
            run_id INTEGER REFERENCES scan_runs(id) ON DELETE SET NULL,
            artifact_type TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            record_count INTEGER NOT NULL DEFAULT 0,
            source_kind TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS time_backfill_runs (
            operation_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            backup_path TEXT NOT NULL DEFAULT '',
            candidate_count INTEGER NOT NULL DEFAULT 0,
            applied_count INTEGER NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS time_backfill_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id TEXT NOT NULL REFERENCES time_backfill_runs(operation_id) ON DELETE CASCADE,
            table_name TEXT NOT NULL,
            row_id INTEGER NOT NULL,
            field_name TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            basis TEXT NOT NULL DEFAULT '',
            changed_at TEXT NOT NULL,
            UNIQUE(operation_id, table_name, row_id, field_name)
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_runs_task ON scan_runs(task_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_occ_credentials ON occurrences(credential_id);
        CREATE INDEX IF NOT EXISTS idx_occ_event ON occurrences(source_event_id);
        CREATE INDEX IF NOT EXISTS idx_verification_status ON verification_jobs(status);
        CREATE INDEX IF NOT EXISTS idx_notifications_status ON notification_outbox(status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_ext_experiment_connector ON extensibility_experiments(connector_key);
        CREATE INDEX IF NOT EXISTS idx_support_matrix_kind ON support_matrix(item_kind, status);
        CREATE INDEX IF NOT EXISTS idx_security_audit_created ON security_audit(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_run_evidence_collector ON run_evidence(collector_key, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_evidence_checks_type ON evidence_checks(check_type, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_evidence_artifacts_run ON evidence_artifacts(run_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_time_backfill_changes_operation ON time_backfill_changes(operation_id, id);
        """
        self._execute(lambda connection: connection.executescript(schema), writable=True)
        self._migrate_schema()
        self._backfill_time_provenance()
        # Backfill the offline notification audit for data created before the
        # unified outbox existed. This is an idempotent local migration only.
        self._execute(
            lambda connection: connection.execute(
                """
                INSERT OR IGNORE INTO notification_outbox(
                    notification_id,event_id,severity,status,channel,created_at,delivered_at,delivery_note
                )
                SELECT
                    'NOTIFY-' || o.source_event_id,
                    o.source_event_id,
                    COALESCE(json_extract(o.evidence_json,'$.risk_level'),'unknown'),
                    'queued',
                    'offline-sqlite',
                    o.detected_at,
                    NULL,
                    '离线通知适配层：历史记录迁移，仅写入统一后端 SQLite，未发送外部消息。'
                FROM occurrences o
                WHERE o.source_event_id IS NOT NULL
                """,
            ),
            writable=True,
        )
        self._backfill_run_evidence()
        self.sync_rules_and_capabilities()

    def _migrate_schema(self) -> None:
        """Add columns introduced after the initial unified backend release."""
        def migrate(connection: sqlite3.Connection) -> None:
            def ensure(table: str, column: str, definition: str) -> None:
                columns = {
                    str(row["name"])
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if column not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

            ensure("source_contents", "fetched_at_source", "TEXT NOT NULL DEFAULT 'legacy_inferred'")
            ensure("source_contents", "time_provenance_json", "TEXT NOT NULL DEFAULT '{}'")
            ensure("occurrences", "time_provenance_json", "TEXT NOT NULL DEFAULT '{}'")
            ensure("credentials", "first_discovered_at", "TEXT")
            ensure("credentials", "last_discovered_at", "TEXT")
            ensure("occurrences", "first_discovered_at", "TEXT")
            ensure("occurrences", "last_discovered_at", "TEXT")
            ensure("notification_outbox", "event_schema_version", "TEXT NOT NULL DEFAULT '1.0'")
            ensure("notification_outbox", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
            ensure("notification_outbox", "last_error", "TEXT")
            ensure("notification_outbox", "next_attempt_at", "TEXT")
            ensure("notification_outbox", "payload_json", "TEXT NOT NULL DEFAULT '{}'")
            ensure("support_matrix", "tested_at", "TEXT")
            ensure("support_matrix", "test_run_id", "INTEGER")
            ensure("support_matrix", "test_count", "INTEGER NOT NULL DEFAULT 0")
            # These tables are created in initialize() for new databases. The
            # guards keep older databases safe when initialize is re-entered.
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS run_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL UNIQUE REFERENCES scan_runs(id) ON DELETE CASCADE,
                    collector_key TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT '',
                    platform TEXT NOT NULL DEFAULT '',
                    source_name TEXT NOT NULL DEFAULT '',
                    authorization_scope_json TEXT NOT NULL DEFAULT '{}',
                    target_digest TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    candidates INTEGER NOT NULL DEFAULT 0,
                    inserted_count INTEGER NOT NULL DEFAULT 0,
                    duplicate_count INTEGER NOT NULL DEFAULT 0,
                    filtered_count INTEGER NOT NULL DEFAULT 0,
                    new_credentials INTEGER NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    rate_limit_count INTEGER NOT NULL DEFAULT 0,
                    checkpoint_status TEXT NOT NULL DEFAULT 'not_applicable',
                    recovery_status TEXT NOT NULL DEFAULT 'not_exercised',
                    idempotency_status TEXT NOT NULL DEFAULT 'not_exercised',
                    evidence_status TEXT NOT NULL DEFAULT 'partial',
                    error_code TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
                    check_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expected TEXT NOT NULL DEFAULT '',
                    actual TEXT NOT NULL DEFAULT '',
                    evidence_ref TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, check_type)
                );
                CREATE TABLE IF NOT EXISTS evidence_artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    artifact_id TEXT NOT NULL UNIQUE,
                    run_id INTEGER REFERENCES scan_runs(id) ON DELETE SET NULL,
                    artifact_type TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    record_count INTEGER NOT NULL DEFAULT 0,
                    source_kind TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS time_backfill_runs (
                    operation_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    backup_path TEXT NOT NULL DEFAULT '',
                    candidate_count INTEGER NOT NULL DEFAULT 0,
                    applied_count INTEGER NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS time_backfill_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL REFERENCES time_backfill_runs(operation_id) ON DELETE CASCADE,
                    table_name TEXT NOT NULL,
                    row_id INTEGER NOT NULL,
                    field_name TEXT NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    basis TEXT NOT NULL DEFAULT '',
                    changed_at TEXT NOT NULL,
                    UNIQUE(operation_id, table_name, row_id, field_name)
                );
                CREATE INDEX IF NOT EXISTS idx_run_evidence_collector ON run_evidence(collector_key, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_evidence_checks_type ON evidence_checks(check_type, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_evidence_artifacts_run ON evidence_artifacts(run_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_time_backfill_changes_operation ON time_backfill_changes(operation_id, id);
                """
            )
        self._execute(migrate, writable=True)

    def _backfill_time_provenance(self) -> None:
        """Describe legacy timestamps without inventing new public times.

        This migration is intentionally limited to rows that already exist.
        It adds provenance and unknown reasons, while preserving every existing
        timestamp and never copying a timestamp between credential occurrences.
        """
        def write(connection: sqlite3.Connection) -> None:
            content_rows = connection.execute(
                """
                SELECT id,published_at,published_source,published_confidence,
                       source_updated_at,fetched_at,fetched_at_source,
                       time_provenance_json
                FROM source_contents
                """
            ).fetchall()
            for row in content_rows:
                provenance = _load(row["time_provenance_json"], {})
                original_provenance = json.loads(_dump(provenance))
                published_entry = provenance.get("published_at")
                if (
                    isinstance(published_entry, dict)
                    and published_entry.get("value")
                    and str(published_entry.get("source") or "")
                    in REFERENCE_ONLY_PUBLIC_TIME_SOURCES
                    and published_entry.get("eligible_for_latency")
                ):
                    reference_entry = dict(published_entry)
                    reference_entry["eligible_for_latency"] = False
                    reference_entry["role"] = "content_revision_reference"
                    reference_entry["unknown_reason"] = "public_appearance_not_proven"
                    provenance["published_at"] = reference_entry
                incoming = {
                    "published_at": legacy_published_entry(
                        row["published_at"],
                        row["published_source"],
                        row["published_confidence"],
                    ),
                    "fetched_at": time_entry(
                        row["fetched_at"],
                        source=str(row["fetched_at_source"] or "unknown"),
                        meaning=(
                            "连接器完成读取当前内容的时间。"
                            if row["fetched_at_source"] == "connector"
                            else "历史记录没有连接器明确采集时间，保持为不可用于精确采集时效的参考值。"
                        ),
                        confidence="high" if row["fetched_at_source"] == "connector" else "low",
                        role="acquisition_completion",
                        eligible_for_latency=(
                            row["fetched_at_source"] == "connector" and bool(row["fetched_at"])
                        ),
                        unknown_reason=(
                            "" if row["fetched_at_source"] == "connector" and row["fetched_at"]
                            else "acquisition_time_not_provided"
                        ),
                        backfill_reason="由旧 source_contents.fetched_at 和 fetched_at_source 补写。",
                    ),
                }
                if row["source_updated_at"]:
                    incoming["source_updated_at"] = time_entry(
                        row["source_updated_at"],
                        source="legacy_source_updated_at",
                        meaning="历史内容版本更新时间，仅作为内容修订参考，不是公开泄露时间。",
                        confidence="low",
                        role="content_revision_reference",
                        evidence="source_contents.source_updated_at",
                        backfill_reason="由旧 source_contents.source_updated_at 补写。",
                    )
                merged = merge_time_provenance(provenance, incoming)
                if _dump(merged) != _dump(original_provenance):
                    connection.execute(
                        "UPDATE source_contents SET time_provenance_json=? WHERE id=?",
                        (_dump(merged), row["id"]),
                    )

            occurrence_rows = connection.execute(
                """
                SELECT id,first_seen_at,detected_at,first_discovered_at,
                       last_discovered_at,time_provenance_json
                FROM occurrences
                """
            ).fetchall()
            for row in occurrence_rows:
                provenance = _load(row["time_provenance_json"], {})
                original_provenance = json.loads(_dump(provenance))
                incoming = {
                    "first_observed_at": time_entry(
                        row["first_seen_at"],
                        source="occurrence_storage",
                        meaning="本系统记录的当前内容位置首次观测时间。",
                        confidence="medium" if row["first_seen_at"] else "none",
                        role="system_observation",
                        unknown_reason=(
                            "" if row["first_seen_at"] else "observation_time_not_provided"
                        ),
                        backfill_reason="由旧 occurrences.first_seen_at 补写。",
                    ),
                    "detected_at": time_entry(
                        row["detected_at"],
                        source="occurrence_storage",
                        meaning="历史记录中的规则检测时间；处理耗时不由墙上时钟推导。",
                        confidence="low" if row["detected_at"] else "none",
                        role="detection_completion",
                        unknown_reason=(
                            "" if row["detected_at"] else "detection_time_not_provided"
                        ),
                        backfill_reason="由旧 occurrences.detected_at 补写。",
                    ),
                }
                discovered_entry = provenance.get("first_discovered_at")
                detected_entry = provenance.get("detected_at")
                detected_entry = detected_entry if isinstance(detected_entry, dict) else {}
                runtime_detection_value = (
                    detected_entry.get("value") or row["detected_at"]
                )
                runtime_detection_is_evidence_grade = bool(
                    runtime_detection_value
                    and detected_entry.get("role") == "detection_completion"
                    and detected_entry.get("source") in {
                        "detection_completed",
                        "caller_supplied",
                    }
                    and detected_entry.get("eligible_for_latency")
                    and detected_entry.get("confidence") in {"high", "medium"}
                    and (
                        not detected_entry.get("backfill_reason")
                        or detected_entry.get("evidence")
                    )
                )
                legacy_discovery = (
                    row["first_discovered_at"]
                    and isinstance(discovered_entry, dict)
                    and (
                        discovered_entry.get("backfill_reason")
                        or discovered_entry.get("evidence_scope") == "legacy_occurrence"
                        or discovered_entry.get("source") == "occurrence_storage"
                        or discovered_entry.get("role") == "historical_first_valid_detection"
                    )
                )
                if runtime_detection_is_evidence_grade and (
                    not row["first_discovered_at"]
                    or (
                        isinstance(discovered_entry, dict)
                        and discovered_entry.get("value") == runtime_detection_value
                        and legacy_discovery
                    )
                ):
                    recovered_entry = time_entry(
                        runtime_detection_value,
                        source=str(detected_entry.get("source") or "detection_completed"),
                        meaning=(
                            "规则检测实际完成并形成有效凭据结果的时间；"
                            "当前检测引擎按内容级完成粒度记录。"
                        ),
                        confidence=str(detected_entry.get("confidence") or "high"),
                        evidence=str(detected_entry.get("evidence") or ""),
                        role="first_valid_detection",
                        eligible_for_latency=True,
                        precision=str(
                            detected_entry.get("precision")
                            or timestamp_precision(runtime_detection_value)
                        ),
                        evidence_scope=str(
                            (discovered_entry or {}).get("evidence_scope")
                            or "recovered_from_detection_evidence"
                        ),
                        content_version=str(
                            (discovered_entry or {}).get("content_version") or ""
                        ),
                    )
                    recovered_entry["recovery_note"] = (
                        "根据已保存的规则检测完成证据恢复；"
                        "这是当前最早有证据的检测时间，不宣称可恢复更早历史首次时间。"
                    )
                    provenance["first_discovered_at"] = recovered_entry
                    discovered_entry = recovered_entry
                if not row["first_discovered_at"]:
                    if (
                        isinstance(discovered_entry, dict)
                        and discovered_entry.get("value")
                        and first_discovery_eligible(provenance)
                    ):
                        incoming["first_discovered_at"] = dict(discovered_entry)
                if not row["last_discovered_at"] and incoming.get("first_discovered_at", {}).get("value"):
                    incoming["last_discovered_at"] = dict(incoming["first_discovered_at"])
                merged = merge_time_provenance(provenance, incoming)
                updates: dict[str, Any] = {}
                if (
                    row["first_discovered_at"]
                    and isinstance(discovered_entry, dict)
                    and discovered_entry.get("value")
                    and first_discovery_eligible(provenance)
                    and legacy_discovery
                ):
                    updates["first_discovered_at"] = discovered_entry["value"]
                if not row["first_discovered_at"] and incoming.get("first_discovered_at", {}).get("value"):
                    updates["first_discovered_at"] = incoming["first_discovered_at"]["value"]
                if not row["last_discovered_at"] and incoming.get("last_discovered_at", {}).get("value"):
                    updates["last_discovered_at"] = incoming["last_discovered_at"]["value"]
                if _dump(merged) != _dump(original_provenance):
                    updates["time_provenance_json"] = _dump(merged)
                if updates:
                    assignments = ", ".join(f"{key}=?" for key in updates)
                    connection.execute(
                        f"UPDATE occurrences SET {assignments} WHERE id=?",
                        (*updates.values(), row["id"]),
                    )

            connection.execute(
                """
                UPDATE credentials
                SET first_discovered_at = COALESCE(
                        first_discovered_at,
                        (SELECT MIN(o.first_discovered_at)
                         FROM occurrences o
                         WHERE o.credential_id=credentials.id
                           AND o.first_discovered_at IS NOT NULL)
                    ),
                    last_discovered_at = COALESCE(
                        last_discovered_at,
                        (SELECT MAX(o.last_discovered_at)
                         FROM occurrences o
                         WHERE o.credential_id=credentials.id
                           AND o.last_discovered_at IS NOT NULL)
                    )
                WHERE first_discovered_at IS NULL OR last_discovered_at IS NULL
                """
            )

        self._execute(write, writable=True)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    @staticmethod
    def _source_category(collector_key: str) -> tuple[str, str]:
        values = {
            "local_dir": ("local_import", "本地目录"),
            "controlled_fixtures": ("controlled_test", "本地授权夹具"),
            "authorized_jsonl": ("authorized_import", "通用 JSONL 导出"),
            "github": ("code_hosting", "GitHub（授权 API）"),
            "gitee": ("code_hosting", "Gitee（授权 API）"),
            "codeberg": ("code_hosting", "Codeberg"),
            "mediawiki": ("wiki", "MediaWiki"),
            "generic_web": ("knowledge_sharing", "RSS/Atom/Sitemap/URL"),
            "stackoverflow": ("knowledge_sharing", "Stack Overflow"),
            "weibo_authorized_api": ("social_public", "微博（官方 OAuth API）"),
            "oci_registry": ("container", "OCI Registry"),
            "npm_registry": ("package_registry", "npm Registry"),
            "pypi_registry": ("package_registry", "PyPI"),
            "huggingface_repo": ("ai_assets", "Hugging Face Hub"),
            "crates_registry": ("package_registry", "crates.io"),
            "nuget_registry": ("package_registry", "NuGet"),
            "rubygems_registry": ("package_registry", "RubyGems"),
            "apk": ("app", "Android APK"),
            "wxapkg": ("miniprogram", "未加密 wxapkg"),
        }
        return values.get(collector_key, ("unknown", collector_key))

    def create_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        collector_key = str(payload.get("collector_key") or "").strip()
        if not name or len(name) > 200 or not collector_key:
            raise BackendValidationError("source_name_and_collector_required")
        if collector_key == "weibo_public_page":
            raise BackendValidationError("collector_not_supported")
        category, platform = self._source_category(collector_key)
        now = _now()
        config = payload.get("config") or {}
        if not isinstance(config, dict):
            raise BackendValidationError("source_config_must_be_object")
        if any(
            any(marker in str(key).lower() for marker in ("token", "secret", "password", "hmac", "private_key"))
            for key in config
        ):
            raise BackendValidationError("source_config_must_not_contain_secrets")
        mode = payload.get("mode", "manual")
        if mode not in {"manual", "continuous"}:
            raise BackendValidationError("task_mode_invalid")
        interval = int(payload.get("interval_sec", 300))
        if interval < 30 or interval > 86400:
            raise BackendValidationError("task_interval_out_of_range")

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            cursor = connection.execute(
                """
                INSERT INTO sources(name,category,platform,collector_key,config_json,enabled,created_at,updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (name, category, platform, collector_key, _dump(config), int(bool(payload.get("enabled", True))), now, now),
            )
            source_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO tasks(source_id,mode,status,interval_sec,next_run_at,last_run_at,last_error,created_at,updated_at)
                VALUES (?, ?, 'pending', ?, ?, NULL, NULL, ?, ?)
                """,
                (source_id, mode, interval, now if mode == "continuous" else None, now, now),
            )
            return self._source_from_connection(connection, source_id)

        return self._execute(write, writable=True)

    def _source_from_connection(self, connection: sqlite3.Connection, source_id: int) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise BackendStorageError("source_not_found")
        result = self._row(row) or {}
        result["enabled"] = bool(result["enabled"])
        result["config"] = _load(result.pop("config_json"), {})
        task = connection.execute(
            "SELECT * FROM tasks WHERE source_id = ? ORDER BY id LIMIT 1", (source_id,)
        ).fetchone()
        result["task"] = self._task_row(task) if task else None
        return result

    def list_sources(self) -> list[dict[str, Any]]:
        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute("SELECT id FROM sources ORDER BY id DESC").fetchall()
            return [self._source_from_connection(connection, int(row["id"])) for row in rows]

        return self._execute(read)

    def get_source(self, source_id: int) -> dict[str, Any] | None:
        def read(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute("SELECT id FROM sources WHERE id = ?", (source_id,)).fetchone()
            return self._source_from_connection(connection, source_id) if row else None

        return self._execute(read)

    def update_source(self, source_id: int, payload: dict[str, Any]) -> dict[str, Any] | None:
        now = _now()

        def write(connection: sqlite3.Connection) -> dict[str, Any] | None:
            current = connection.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
            if current is None:
                return None
            name = str(payload.get("name", current["name"])).strip()
            config = payload.get("config", _load(current["config_json"], {}))
            enabled = payload.get("enabled", bool(current["enabled"]))
            if not name or not isinstance(config, dict):
                raise BackendValidationError("source_payload_invalid")
            if any(
                any(marker in str(key).lower() for marker in ("token", "secret", "password", "hmac", "private_key"))
                for key in config
            ):
                raise BackendValidationError("source_config_must_not_contain_secrets")
            connection.execute(
                "UPDATE sources SET name=?,config_json=?,enabled=?,updated_at=? WHERE id=?",
                (name, _dump(config), int(bool(enabled)), now, source_id),
            )
            if "interval_sec" in payload or "mode" in payload:
                task = connection.execute(
                    "SELECT * FROM tasks WHERE source_id = ? ORDER BY id LIMIT 1", (source_id,)
                ).fetchone()
                if task:
                    interval = int(payload.get("interval_sec", task["interval_sec"]))
                    mode = payload.get("mode", task["mode"])
                    if interval < 30 or interval > 86400 or mode not in {"manual", "continuous"}:
                        raise BackendValidationError("task_payload_invalid")
                    next_run = now if mode == "continuous" and task["status"] != "paused" else task["next_run_at"]
                    connection.execute(
                        "UPDATE tasks SET mode=?,interval_sec=?,next_run_at=?,updated_at=? WHERE id=?",
                        (mode, interval, next_run, now, task["id"]),
                    )
            return self._source_from_connection(connection, source_id)

        return self._execute(write, writable=True)

    def delete_source(self, source_id: int) -> bool:
        return self._execute(
            lambda connection: connection.execute(
                "DELETE FROM sources WHERE id = ?", (source_id,)
            ).rowcount == 1,
            writable=True,
        )

    @staticmethod
    def _task_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["id"] = int(result["id"])
        return result

    def list_tasks(self) -> list[dict[str, Any]]:
        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute(
                """
                SELECT t.*, s.name AS source_name, s.collector_key, s.platform, s.enabled AS source_enabled
                       ,s.category,s.config_json
                FROM tasks t JOIN sources s ON s.id=t.source_id ORDER BY t.id
                """
            ).fetchall()
            output = []
            for row in rows:
                item = self._task_row(row) or {}
                item["source_enabled"] = bool(item["source_enabled"])
                item["category"] = row["category"]
                raw_config = str(row["config_json"] or "{}")
                try:
                    parsed_config = json.loads(raw_config)
                    config_parse_error = not isinstance(parsed_config, dict)
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed_config = {}
                    config_parse_error = True
                item["config"] = parsed_config if isinstance(parsed_config, dict) else {}
                item["config_raw"] = raw_config
                item["config_parse_error"] = config_parse_error
                item.pop("config_json", None)
                output.append(item)
            return output

        return self._execute(read)

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        def read(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                """
                SELECT t.*, s.name AS source_name, s.collector_key, s.platform, s.enabled AS source_enabled
                       ,s.category,s.config_json
                FROM tasks t JOIN sources s ON s.id=t.source_id WHERE t.id=?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            item = self._task_row(row) or {}
            item["source_enabled"] = bool(item["source_enabled"])
            item["category"] = row["category"]
            raw_config = str(row["config_json"] or "{}")
            try:
                parsed_config = json.loads(raw_config)
                config_parse_error = not isinstance(parsed_config, dict)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_config = {}
                config_parse_error = True
            item["config"] = parsed_config if isinstance(parsed_config, dict) else {}
            item["config_raw"] = raw_config
            item["config_parse_error"] = config_parse_error
            item.pop("config_json", None)
            return item

        return self._execute(read)

    def task_action(self, task_id: int, action: str) -> dict[str, Any] | None:
        now = _now()
        transitions = {
            "pause": ("paused", None),
            "resume": ("pending", now),
            "cancel": ("cancelled", None),
            "retry": ("pending", now),
        }
        if action not in transitions:
            raise BackendValidationError("task_action_invalid")
        status, next_run = transitions[action]

        def write(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE tasks SET status=?,next_run_at=?,last_error=NULL,updated_at=? WHERE id=?",
                (status, next_run, now, task_id),
            )
            return self._task_row(
                connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            )

        return self._execute(write, writable=True)

    def create_run(self, task_id: int, trigger: str = "manual") -> dict[str, Any]:
        now = _now()

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise BackendValidationError("task_not_found")
            if task["status"] == "running":
                raise BackendValidationError("task_already_running")
            connection.execute(
                "UPDATE tasks SET status='running',last_error=NULL,updated_at=? WHERE id=?",
                (now, task_id),
            )
            cursor = connection.execute(
                "INSERT INTO scan_runs(task_id,status,trigger,stats_json,started_at) VALUES (?, 'running', ?, '{}', ?)",
                (task_id, trigger, now),
            )
            return {
                "id": int(cursor.lastrowid),
                "task_id": task_id,
                "status": "running",
                "trigger": trigger,
                "stats": {},
                "started_at": now,
                "finished_at": None,
            }

        return self._execute(write, writable=True)

    def finish_run(self, run_id: int, status: str, stats: dict[str, Any], error: str | None = None) -> None:
        if status not in {"completed", "failed", "cancelled", "interrupted"}:
            raise BackendValidationError("scan_run_status_invalid")
        now = _now()

        def write(connection: sqlite3.Connection) -> None:
            run = connection.execute("SELECT task_id FROM scan_runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise BackendValidationError("scan_run_not_found")
            task_id = int(run["task_id"])
            task = connection.execute("SELECT mode,interval_sec FROM tasks WHERE id=?", (task_id,)).fetchone()
            is_continuous = bool(status == "completed" and task and task["mode"] == "continuous")
            task_status = "pending" if is_continuous else (
                "done" if status == "completed" else "failed"
            )
            next_run = (
                (datetime.now(timezone.utc) + timedelta(seconds=max(30, int(task["interval_sec"] or 300))))
                .replace(microsecond=0)
                .isoformat()
                if is_continuous
                else None
            )
            connection.execute(
                "UPDATE scan_runs SET status=?,stats_json=?,finished_at=? WHERE id=?",
                (status, _dump(stats), now, run_id),
            )
            connection.execute(
                """
                UPDATE tasks SET status=?,next_run_at=?,last_run_at=?,last_error=?,updated_at=? WHERE id=?
                """,
                (task_status, next_run, now, error, now, task_id),
            )

        self._execute(write, writable=True)

    def list_runs(self, task_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))

        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            if task_id is None:
                rows = connection.execute(
                    "SELECT * FROM scan_runs ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM scan_runs WHERE task_id=? ORDER BY id DESC LIMIT ?",
                    (task_id, limit),
                ).fetchall()
            return [
                {
                    **(self._row(row) or {}),
                    "stats": _load(row["stats_json"], {}),
                    "id": int(row["id"]),
                }
                for row in rows
            ]

        return self._execute(read)

    def _backfill_run_evidence(self) -> None:
        """Backfill safe summaries for batches created before run_evidence existed."""
        run_ids = self._execute(
            lambda connection: [
                int(row["id"])
                for row in connection.execute(
                    """
                    SELECT sr.id
                    FROM scan_runs sr
                    LEFT JOIN run_evidence re ON re.run_id=sr.id
                    WHERE re.run_id IS NULL
                    ORDER BY sr.id
                    LIMIT 500
                    """
                ).fetchall()
            ]
        )
        for run_id in run_ids:
            def read_events(connection: sqlite3.Connection) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
                run = connection.execute(
                    "SELECT status,stats_json FROM scan_runs WHERE id=?", (run_id,)
                ).fetchone()
                if run is None:
                    return [], {}, "failed"
                rows = connection.execute(
                    """
                    SELECT o.source_event_id,o.category,o.platform,o.origin_url,o.path,
                           o.first_seen_at,o.detected_at,o.evidence_json,
                           c.type,c.rule_id,c.fingerprint,
                           sc.published_at,sc.published_source,sc.published_confidence,
                           sc.fetched_at,sc.fetched_at_source
                    FROM occurrences o
                    JOIN credentials c ON c.id=o.credential_id
                    LEFT JOIN source_contents sc ON sc.id=o.content_id
                    WHERE o.run_id=?
                    ORDER BY o.id
                    """,
                    (run_id,),
                ).fetchall()
                events: list[dict[str, Any]] = []
                for row in rows:
                    evidence = _load(row["evidence_json"], {})
                    events.append({
                        "event_id": row["source_event_id"],
                        "source_kind": evidence.get("source_kind") or "未知",
                        "channel": row["category"],
                        "platform": row["platform"],
                        "credential_type": row["type"],
                        "redacted_value": "历史脱敏记录****",
                        "source_locator": row["origin_url"] or row["path"],
                        "first_observed_at": row["first_seen_at"],
                        "detected_at": row["detected_at"],
                        "credential_fingerprint": row["fingerprint"],
                        "_rule_id": row["rule_id"],
                        "_published_at": row["published_at"],
                        "_published_source": row["published_source"] or "",
                        "_published_confidence": row["published_confidence"] or "none",
                        "_fetched_at": (
                            row["fetched_at"]
                            if row["fetched_at_source"] == "connector"
                            else None
                        ),
                    })
                status = str(run["status"] or "failed")
                if status not in {"completed", "failed", "cancelled", "interrupted", "running"}:
                    status = "failed"
                return events, _load(run["stats_json"], {}), status

            events, stats, status = self._execute(read_events)
            self.record_run_evidence(
                run_id,
                events,
                stats,
                status=status,
                error_code=stats.get("error_code"),
            )

    @staticmethod
    def _safe_evidence_metadata(value: Any, depth: int = 0) -> Any:
        """Keep operator-supplied evidence metadata bounded and secret-free."""
        if depth > 3:
            return "[truncated]"
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in value.items():
                name = str(key)
                if any(marker in name.lower() for marker in ("token", "secret", "password", "private_key", "hmac")):
                    result[name] = "[redacted]"
                else:
                    result[name] = BackendStore._safe_evidence_metadata(item, depth + 1)
            return result
        if isinstance(value, (list, tuple)):
            return [BackendStore._safe_evidence_metadata(item, depth + 1) for item in list(value)[:100]]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return str(value)[:500] if isinstance(value, str) else value
        return str(value)[:500]

    @staticmethod
    def _latency_summary(events: list[dict[str, Any]], start_key: str, end_key: str) -> dict[str, Any]:
        values: list[float] = []
        unknown_count = 0
        for event in events:
            start_value = event.get(start_key)
            end_value = event.get(end_key)
            start = _parse_time(start_value)
            end = _parse_time(end_value)
            if start is None or end is None:
                unknown_count += 1
                continue
            seconds = (end - start).total_seconds()
            if seconds >= 0:
                values.append(seconds)
            else:
                unknown_count += 1
        return {
            "sample_count": len(values),
            "unknown_count": unknown_count,
            "p50_seconds": _percentile(values, 0.50),
            "p95_seconds": _percentile(values, 0.95),
            "max_seconds": max(values) if values else None,
        }

    @staticmethod
    def _event_discovery_chain(event: dict[str, Any]) -> dict[str, Any]:
        """Calculate a connector hit's public or reference time interval."""
        provenance = event.get("time_provenance")
        provenance = provenance if isinstance(provenance, dict) else {}
        published = provenance.get("published_at")
        published = published if isinstance(published, dict) else {}
        detected_entry = provenance.get("detected_at")
        detected_entry = detected_entry if isinstance(detected_entry, dict) else {}
        public_value = (
            event.get("_published_at")
            or event.get("published_at")
            or published.get("value")
        )
        discovered_value = (
            event.get("_first_discovered_at")
            or event.get("first_discovered_at")
            or (provenance.get("first_discovered_at") or {}).get("value")
        )
        detected_value = (
            event.get("detected_at")
            or detected_entry.get("value")
        )
        discovery_entry = provenance.get("first_discovered_at")
        discovery_entry = discovery_entry if isinstance(discovery_entry, dict) else {}
        discovery_eligible = first_discovery_eligible(provenance)
        public_eligible = latency_eligible(provenance, "published_at") or bool(
            event.get("_published_eligible_for_latency")
        )
        reference_eligible = reference_time_eligible(provenance, "published_at")
        detection_eligible = detection_time_eligible(provenance)
        status = "not_calculable"
        reason = ""
        latency_seconds: float | None = None
        calculation_type = "not_calculable"
        basis = ""
        end_at = None
        if not public_value:
            reason = str(
                event.get("_published_unknown_reason")
                or event.get("public_time_unknown_reason")
                or published.get("unknown_reason")
                or "public_time_not_provided"
            )
        else:
            public_time = _parse_time(str(public_value))
            if public_eligible:
                if not discovered_value:
                    reason = "first_discovery_time_not_saved"
                elif not discovery_eligible:
                    reason = str(
                        discovery_entry.get("unknown_reason")
                        or "historical_first_discovery_time_unverified"
                    )
                else:
                    discovered_time = _parse_time(str(discovered_value))
                    if public_time is None or discovered_time is None:
                        reason = "timestamp_invalid_or_timezone_missing"
                    else:
                        latency_seconds = round(
                            (discovered_time - public_time).total_seconds(),
                            3,
                        )
                        calculation_type = "public_to_first_discovery"
                        basis = "公开时间 → 系统首次发现时间"
                        end_at = discovered_value
                        if latency_seconds < 0:
                            latency_seconds = None
                            reason = "discovery_before_public_time"
                            calculation_type = "not_calculable"
                            basis = ""
                            end_at = None
                        else:
                            status = "calculated"
            elif reference_eligible:
                if not detected_value:
                    reason = str(
                        detected_entry.get("unknown_reason")
                        or "detection_time_not_saved"
                    )
                elif not detection_eligible:
                    reason = str(
                        detected_entry.get("unknown_reason")
                        or "detection_time_evidence_unverified"
                    )
                else:
                    detected_time = _parse_time(str(detected_value))
                    if public_time is None or detected_time is None:
                        reason = "timestamp_invalid_or_timezone_missing"
                    else:
                        latency_seconds = round(
                            (detected_time - public_time).total_seconds(),
                            3,
                        )
                        calculation_type = "reference_to_detection"
                        basis = _reference_latency_basis(published)
                        end_at = detected_value
                        if latency_seconds < 0:
                            latency_seconds = None
                            reason = "detection_before_reference_time"
                            calculation_type = "not_calculable"
                            basis = ""
                            end_at = None
                        else:
                            status = "calculated"
            else:
                reason = "public_time_is_reference_only"
        return {
            "public_time": public_value,
            "public_time_status": (
                "known" if public_value and public_eligible
                else "reference_only" if public_value
                else "unknown"
            ),
            "public_time_unknown_reason": (
                ""
                if public_value
                else str(
                    event.get("_published_unknown_reason")
                    or event.get("public_time_unknown_reason")
                    or published.get("unknown_reason")
                    or "public_time_not_provided"
                )
            ),
            "first_discovered_at": discovered_value,
            "first_discovery_time_status": (
                "known" if discovery_eligible else "unknown"
            ),
            "first_discovery_unknown_reason": (
                ""
                if discovery_eligible
                else str(
                    discovery_entry.get("unknown_reason")
                    or "first_discovery_time_not_saved"
                )
            ),
            "discovery_latency_seconds": latency_seconds,
            "discovery_latency_status": status,
            "discovery_latency_reason": reason,
            "discovery_latency_calculation_type": calculation_type,
            "discovery_latency_basis": basis,
            "discovery_latency_end_at": end_at,
        }

    @classmethod
    def _discovery_latency_summary(
        cls,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        values: list[float] = []
        unknown_count = 0
        for event in events:
            chain = cls._event_discovery_chain(event)
            if chain["discovery_latency_status"] == "calculated":
                values.append(float(chain["discovery_latency_seconds"]))
            else:
                unknown_count += 1
        return {
            "sample_count": len(values),
            "unknown_count": unknown_count,
            "p50_seconds": _percentile(values, 0.50),
            "p95_seconds": _percentile(values, 0.95),
            "max_seconds": max(values) if values else None,
        }

    def record_run_evidence(
        self,
        run_id: int,
        events: list[dict[str, Any]],
        stats: dict[str, Any],
        *,
        status: str,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        """Persist one safe, auditable evidence summary for a scan batch."""
        if status not in {"running", "completed", "failed", "cancelled", "interrupted"}:
            raise BackendValidationError("scan_run_status_invalid")
        safe_events = [item for item in events if isinstance(item, dict)]

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            run = connection.execute(
                """
                SELECT sr.*,s.name AS source_name,s.collector_key,s.category,s.platform,
                       s.config_json,t.source_id
                FROM scan_runs sr
                JOIN tasks t ON t.id=sr.task_id
                JOIN sources s ON s.id=t.source_id
                WHERE sr.id=?
                """,
                (run_id,),
            ).fetchone()
            if run is None:
                raise BackendValidationError("scan_run_not_found")
            connector_spec = next(
                (
                    item for item in list_connector_specs()
                    if str(item.get("name") or "") in {
                        str(run["collector_key"]),
                        str(run["collector_key"]).replace("_", "-"),
                    }
                ),
                None,
            )
            channel = str((connector_spec or {}).get("channel") or run["category"] or "")
            config = _load(run["config_json"], {})
            target_values: list[str] = []
            target_keys = (
                "repositories", "repository", "repo", "path", "url", "feed_url",
                "sitemap_url", "api_url", "image", "package", "file",
                "q", "queries", "tagged",
            )
            for key in target_keys:
                value = config.get(key)
                if isinstance(value, (list, tuple)):
                    target_values.extend(str(item) for item in value[:100])
                elif value:
                    target_values.append(str(value))
            target_values = sorted(set(target_values))
            target_digest = hashlib.sha256(
                _dump(target_values).encode("utf-8")
            ).hexdigest() if target_values else ""
            scope = {
                "collector_key": run["collector_key"],
                "config_keys": sorted(str(key) for key in config),
                "target_count": len(target_values),
                "targets": target_values[:100],
                "network_authorized_by_runtime": run["collector_key"] not in {
                    "local_dir", "controlled_fixtures", "authorized_jsonl"
                },
            }
            credential_type_counts: dict[str, int] = {}
            source_kind_counts: dict[str, int] = {}
            published_sources: dict[str, int] = {}
            hit_rows: list[dict[str, Any]] = []
            for event in safe_events[:500]:
                credential_type = str(event.get("credential_type") or "未知")
                source_kind = str(event.get("source_kind") or "未知")
                credential_type_counts[credential_type] = credential_type_counts.get(credential_type, 0) + 1
                source_kind_counts[source_kind] = source_kind_counts.get(source_kind, 0) + 1
                published_source = str(event.get("_published_source") or "")
                if published_source:
                    published_sources[published_source] = published_sources.get(published_source, 0) + 1
                fingerprint = str(event.get("credential_fingerprint") or "")
                discovery = self._event_discovery_chain(event)
                hit_rows.append({
                    "event_id": str(event.get("event_id") or ""),
                    "credential_type": credential_type,
                    "rule_id": str(event.get("_rule_id") or event.get("detection_method") or ""),
                    "platform": str(event.get("platform") or ""),
                    "source_kind": source_kind,
                    "source_locator": str(event.get("source_locator") or ""),
                    "fingerprint_prefix": fingerprint[:12] if fingerprint else "",
                    "published_at": event.get("_published_at"),
                    "published_source": published_source,
                    "published_confidence": str(event.get("_published_confidence") or "none"),
                    "published_meaning": str(
                        event.get("_published_meaning") or event.get("published_meaning") or ""
                    ),
                    "published_evidence": str(
                        event.get("_published_evidence") or event.get("published_evidence") or ""
                    ),
                    "public_time_status": (
                        discovery["public_time_status"]
                    ),
                    "public_time_unknown_reason": discovery["public_time_unknown_reason"],
                    "source_updated_at": event.get("_source_updated_at"),
                    "source_updated_source": str(event.get("_source_updated_source") or ""),
                    "version_id": str(event.get("_version_id") or ""),
                    "version_kind": str(event.get("_version_kind") or ""),
                    "first_observed_at": event.get("first_observed_at"),
                    "fetched_at": event.get("_fetched_at"),
                    "first_discovered_at": discovery["first_discovered_at"],
                    "last_discovered_at": (
                        event.get("_last_discovered_at")
                        or event.get("last_discovered_at")
                    ),
                    "detected_at": event.get("detected_at"),
                    "discovery_latency_seconds": discovery["discovery_latency_seconds"],
                    "discovery_latency_status": discovery["discovery_latency_status"],
                    "discovery_latency_reason": discovery["discovery_latency_reason"],
                    "time_provenance": self._safe_evidence_metadata(
                        event.get("time_provenance") or {}
                    ),
                })
            duplicate_count = int(stats.get("skipped_occurrences", 0) or 0)
            checkpoint_status = (
                "deferred_then_committed"
                if run["collector_key"] in {"github", "gitee"} and status == "completed"
                else "not_committed_on_failure"
                if run["collector_key"] in {"github", "gitee"} and status != "completed"
                else "not_applicable"
            )
            idempotency_status = "observed_duplicate_skip" if duplicate_count else "not_exercised"
            recovery_status = "not_exercised"
            evidence_status = "run_recorded" if status == "completed" else "run_failed"
            metadata = {
                "items_seen": int(stats.get("items_seen", 0) or 0),
                "detection_note": str(stats.get("detection_note") or ""),
                "credential_type_counts": credential_type_counts,
                "source_kind_counts": source_kind_counts,
                "published_time_sources": published_sources,
                "credential_hits": hit_rows,
                "latency": {
                    "published_to_first_observed": self._latency_summary(
                        safe_events, "_published_at", "first_observed_at"
                    ),
                    "published_to_first_discovered": self._discovery_latency_summary(
                        safe_events
                    ),
                    "first_observed_to_detected": self._latency_summary(
                        safe_events, "first_observed_at", "detected_at"
                    ),
                    "fetched_to_detected": self._latency_summary(
                        safe_events, "_fetched_at", "detected_at"
                    ),
                },
                "unknown_public_time_count": sum(
                    item["public_time_status"] == "unknown" for item in hit_rows
                ),
                "notes": [
                    "该记录只保存脱敏定位、HMAC 指纹前缀和时间来源，不保存原始凭据。",
                    "限流、失败恢复和断点续跑需要通过 evidence_checks 单独登记现场演练结果。",
                ],
            }
            now = _now()
            connection.execute(
                """
                INSERT INTO run_evidence(
                    run_id,collector_key,channel,platform,source_name,authorization_scope_json,
                    target_digest,status,started_at,finished_at,candidates,inserted_count,
                    duplicate_count,filtered_count,new_credentials,retry_count,rate_limit_count,
                    checkpoint_status,recovery_status,idempotency_status,evidence_status,error_code,
                    metadata_json,created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status=excluded.status,finished_at=excluded.finished_at,
                    candidates=excluded.candidates,inserted_count=excluded.inserted_count,
                    duplicate_count=excluded.duplicate_count,filtered_count=excluded.filtered_count,
                    new_credentials=excluded.new_credentials,retry_count=excluded.retry_count,
                    rate_limit_count=excluded.rate_limit_count,checkpoint_status=excluded.checkpoint_status,
                    recovery_status=excluded.recovery_status,idempotency_status=excluded.idempotency_status,
                    evidence_status=excluded.evidence_status,error_code=excluded.error_code,
                    metadata_json=excluded.metadata_json,created_at=excluded.created_at
                """,
                (
                    run_id,
                    run["collector_key"],
                    channel,
                    run["platform"],
                    run["source_name"],
                    _dump(scope),
                    target_digest,
                    status,
                    run["started_at"],
                    run["finished_at"],
                    int(stats.get("candidates", len(safe_events)) or 0),
                    int(stats.get("new_occurrences", 0) or 0),
                    duplicate_count,
                    int(stats.get("filtered_count", stats.get("skipped_occurrences", 0)) or 0),
                    int(stats.get("new_credentials", 0) or 0),
                    int(stats.get("retry_count", 0) or 0),
                    int(stats.get("rate_limit_count", 0) or 0),
                    checkpoint_status,
                    recovery_status,
                    idempotency_status,
                    evidence_status,
                    error_code or stats.get("error_code"),
                    _dump(metadata),
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM run_evidence WHERE run_id=?", (run_id,)).fetchone()
            return self._run_evidence_output(row)

        return self._execute(write, writable=True)

    def _run_evidence_output(self, row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        item = self._row(row) or {}
        item["authorization_scope"] = _load(item.pop("authorization_scope_json", "{}"), {})
        item["metadata"] = _load(item.pop("metadata_json", "{}"), {})
        item["checks"] = []
        return item

    def list_run_evidence(self, collector_key: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))

        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            if collector_key:
                rows = connection.execute(
                    "SELECT * FROM run_evidence WHERE collector_key=? ORDER BY id DESC LIMIT ?",
                    (collector_key, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM run_evidence ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            result = [self._run_evidence_output(row) for row in rows]
            for item in result:
                checks = connection.execute(
                    """
                    SELECT check_type,status,expected,actual,evidence_ref,metadata_json,created_at
                    FROM evidence_checks WHERE run_id=? ORDER BY id
                    """,
                    (item["run_id"],),
                ).fetchall()
                item["checks"] = [
                    {
                        **(self._row(check) or {}),
                        "metadata": _load(check["metadata_json"], {}),
                    }
                    for check in checks
                ]
            return result

        return self._execute(read)

    def record_evidence_check(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            run_id = int(payload.get("run_id"))
        except (TypeError, ValueError):
            raise BackendValidationError("scan_run_not_found") from None
        check_type = str(payload.get("check_type") or "").strip()
        allowed_types = {
            "rate_limit", "failure_recovery", "checkpoint_resume",
            "idempotency", "public_time_provenance",
        }
        if check_type not in allowed_types:
            raise BackendValidationError("evidence_check_type_invalid")
        status = str(payload.get("status") or "").strip()
        if status not in {"passed", "failed", "partial", "not_exercised"}:
            raise BackendValidationError("evidence_check_status_invalid")
        if len(str(payload.get("expected") or "")) > 1000 or len(str(payload.get("actual") or "")) > 1000:
            raise BackendValidationError("evidence_check_value_invalid")
        now = _now()

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            if connection.execute("SELECT id FROM scan_runs WHERE id=?", (run_id,)).fetchone() is None:
                raise BackendValidationError("scan_run_not_found")
            metadata = self._safe_evidence_metadata(payload.get("metadata") or {})
            connection.execute(
                """
                INSERT INTO evidence_checks(
                    run_id,check_type,status,expected,actual,evidence_ref,metadata_json,created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id,check_type) DO UPDATE SET
                    status=excluded.status,expected=excluded.expected,actual=excluded.actual,
                    evidence_ref=excluded.evidence_ref,metadata_json=excluded.metadata_json,
                    created_at=excluded.created_at
                """,
                (
                    run_id,
                    check_type,
                    status,
                    str(payload.get("expected") or "")[:1000],
                    str(payload.get("actual") or "")[:1000],
                    str(payload.get("evidence_ref") or "")[:500],
                    _dump(metadata),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM evidence_checks WHERE run_id=? AND check_type=?",
                (run_id, check_type),
            ).fetchone()
            return {
                **(self._row(row) or {}),
                "metadata": _load(row["metadata_json"], {}),
            }

        return self._execute(write, writable=True)

    def register_evidence_artifact(self, payload: dict[str, Any]) -> dict[str, Any]:
        artifact_id = str(payload.get("artifact_id") or uuid.uuid4().hex).strip()
        artifact_type = str(payload.get("artifact_type") or "evidence_package").strip()
        relative_path = str(payload.get("relative_path") or "").strip()
        sha256 = str(payload.get("sha256") or "").strip().lower()
        path_parts = Path(relative_path).parts
        if (
            not relative_path
            or len(relative_path) > 500
            or Path(relative_path).is_absolute()
            or ".." in path_parts
            or not re.fullmatch(r"[0-9a-f]{64}", sha256)
        ):
            raise BackendValidationError("evidence_artifact_invalid")
        run_id = payload.get("run_id")
        run_id = int(run_id) if run_id is not None else None
        now = _now()

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            if run_id is not None and connection.execute(
                "SELECT id FROM scan_runs WHERE id=?", (run_id,)
            ).fetchone() is None:
                raise BackendValidationError("scan_run_not_found")
            connection.execute(
                """
                INSERT INTO evidence_artifacts(
                    artifact_id,run_id,artifact_type,relative_path,sha256,record_count,
                    source_kind,created_at,metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id) DO UPDATE SET
                    run_id=excluded.run_id,artifact_type=excluded.artifact_type,
                    relative_path=excluded.relative_path,sha256=excluded.sha256,
                    record_count=excluded.record_count,source_kind=excluded.source_kind,
                    created_at=excluded.created_at,metadata_json=excluded.metadata_json
                """,
                (
                    artifact_id,
                    run_id,
                    artifact_type[:100],
                    relative_path,
                    sha256,
                    max(0, int(payload.get("record_count") or 0)),
                    str(payload.get("source_kind") or "")[:100],
                    now,
                    _dump(self._safe_evidence_metadata(payload.get("metadata") or {})),
                ),
            )
            row = connection.execute(
                "SELECT * FROM evidence_artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
            return {
                **(self._row(row) or {}),
                "metadata": _load(row["metadata_json"], {}),
            }

        return self._execute(write, writable=True)

    def evidence_report(self, limit: int = 200) -> dict[str, Any]:
        runs = self.list_run_evidence(limit=limit)
        artifacts = self._execute(
            lambda connection: [
                {
                    **(self._row(row) or {}),
                    "metadata": _load(row["metadata_json"], {}),
                }
                for row in connection.execute(
                    "SELECT * FROM evidence_artifacts ORDER BY id DESC LIMIT ?",
                    (max(1, min(500, int(limit))),),
                ).fetchall()
            ]
        )
        checks = [check for run in runs for check in run.get("checks", [])]
        by_collector: dict[str, dict[str, Any]] = {}
        for run in runs:
            item = by_collector.setdefault(run["collector_key"], {
                "collector_key": run["collector_key"],
                "runs": 0,
                "completed_runs": 0,
                "failed_runs": 0,
                "candidates": 0,
                "inserted": 0,
                "duplicates": 0,
                "credential_types": set(),
                "public_time_known": 0,
                "public_time_unknown": 0,
            })
            item["runs"] += 1
            item["completed_runs"] += run["status"] == "completed"
            item["failed_runs"] += run["status"] == "failed"
            item["candidates"] += int(run["candidates"] or 0)
            item["inserted"] += int(run["inserted_count"] or 0)
            item["duplicates"] += int(run["duplicate_count"] or 0)
            metadata = run.get("metadata") or {}
            item["credential_types"].update((metadata.get("credential_type_counts") or {}).keys())
            item["public_time_known"] += sum(
                1 for hit in metadata.get("credential_hits", [])
                if hit.get("public_time_status") == "known"
            )
            item["public_time_unknown"] += sum(
                1 for hit in metadata.get("credential_hits", [])
                if hit.get("public_time_status") == "unknown"
            )
        for item in by_collector.values():
            item["credential_types"] = sorted(item["credential_types"])
        return {
            "source": "真实数据：统一后端 SQLite run_evidence/evidence_checks/evidence_artifacts",
            "implementation_status": "已实现",
            "error": None,
            "summary": {
                "run_count": len(runs),
                "completed_run_count": sum(run["status"] == "completed" for run in runs),
                "failed_run_count": sum(run["status"] == "failed" for run in runs),
                "check_count": len(checks),
                "passed_check_count": sum(check["status"] == "passed" for check in checks),
                "artifact_count": len(artifacts),
                "public_time_known_count": sum(
                    item["public_time_known"] for item in by_collector.values()
                ),
                "public_time_unknown_count": sum(
                    item["public_time_unknown"] for item in by_collector.values()
                ),
            },
            "by_collector": sorted(by_collector.values(), key=lambda item: item["collector_key"]),
            "runs": runs,
            "checks": checks,
            "artifacts": artifacts,
            "definitions": {
                "real_run": "completed 的明确授权运行；受控夹具和合成导入不计入公开泄露统计。",
                "public_time": "只接受连接器明确提供的公开/提交/发布时间及其来源；缺失保持 unknown。",
                "latency": "首次观测、采集完成、规则检测分别记录，不能用采集时间替代公开时间。",
                "reliability": "限流、失败恢复、断点续跑和幂等必须有 evidence_checks 现场记录。",
            },
        }

    def _upsert_content(
        self,
        connection: sqlite3.Connection,
        source_id: int,
        run_id: int,
        event: dict[str, Any],
    ) -> int:
        locator = event.get("source_locator") or event["event_id"]
        version_id = str(event.get("_version_id") or event.get("event_id") or "event")
        version_kind = str(event.get("_version_kind") or "event")
        content_path = str(event.get("_content_path") or locator)
        content_hash = str(event.get("_content_hash") or "").strip()
        if not content_hash:
            content_hash = hashlib.sha256(
                _dump({
                    "event_id": event["event_id"],
                    "locator": locator,
                    "redacted": event["redacted_value"],
                    "method": event.get("detection_method"),
                }).encode("utf-8")
            ).hexdigest()
        fetched_at = event.get("_fetched_at") or event.get("fetched_at")
        fetched_source = "connector" if event.get("_fetched_at") or event.get("fetched_at_source") == "connector" else "legacy_inferred"
        published_at = event.get("_published_at") or event.get("published_at")
        published_source = event.get("_published_source") or event.get("published_source") or ""
        published_confidence = event.get("_published_confidence") or event.get("published_confidence") or "none"
        source_updated_at = event.get("_source_updated_at") or event.get("source_updated_at")
        incoming_provenance = _load(event.get("time_provenance"), {})
        incoming_provenance = merge_time_provenance(
            incoming_provenance,
            {
                "published_at": time_entry(
                    published_at,
                    source=published_source or "not_provided",
                    meaning=event.get("_published_meaning") or event.get("published_meaning") or (
                        "来源未提供可验证的公开或内容时间。" if not published_at
                        else "来源提供的内容公开时间；不等于凭据首次泄露时间。"
                    ),
                    confidence=published_confidence,
                    evidence=event.get("_published_evidence") or event.get("published_evidence") or "",
                    role=(
                        "public_content_time"
                        if event.get("public_time_status") == "known"
                        else "content_revision_reference" if published_at else "unknown"
                    ),
                    eligible_for_latency=bool(
                        event.get("public_time_status") == "known"
                        or event.get("_published_eligible_for_latency")
                    ),
                    unknown_reason=event.get("_published_unknown_reason") or event.get("public_time_unknown_reason") or (
                        "" if published_at else "connector_not_provided"
                    ),
                    precision=timestamp_precision(published_at),
                    evidence_scope=locator,
                    content_version=version_id,
                ),
            },
        )
        if source_updated_at or event.get("_source_updated_source") or event.get("source_updated_source"):
            incoming_provenance = merge_time_provenance(
                incoming_provenance,
                {
                    "source_updated_at": time_entry(
                        source_updated_at,
                        source=(
                            event.get("_source_updated_source")
                            or event.get("source_updated_source")
                            or "not_provided"
                        ),
                        meaning=(
                            event.get("_source_updated_meaning")
                            or event.get("source_updated_meaning")
                            or "内容版本更新时间，仅作为修订参考，不是公开泄露时间。"
                        ),
                        confidence=(
                            event.get("_source_updated_confidence")
                            or event.get("source_updated_confidence")
                            or "none"
                        ),
                        evidence=(
                            event.get("_source_updated_evidence")
                            or event.get("source_updated_evidence")
                            or ""
                        ),
                        role="content_revision_reference",
                        unknown_reason=(
                            "" if source_updated_at else "source_revision_time_not_provided"
                        ),
                        precision=timestamp_precision(source_updated_at),
                        evidence_scope=locator,
                        content_version=version_id,
                    )
                },
            )
        incoming_provenance = merge_time_provenance(
            incoming_provenance,
            {
                "fetched_at": time_entry(
                    fetched_at,
                    source=(
                        "connector_content_read"
                        if fetched_source == "connector"
                        else "legacy_inferred"
                    ),
                    meaning=(
                        "连接器完成读取当前内容的时间。"
                        if fetched_source == "connector"
                        else "由旧适配器推断的采集时间，不进入精确采集时效统计。"
                    ),
                    confidence="high" if fetched_source == "connector" else "low",
                    evidence=locator,
                    role="acquisition_completion",
                    eligible_for_latency=fetched_source == "connector" and bool(fetched_at),
                    unknown_reason=(
                        "" if fetched_source == "connector" and fetched_at
                        else "acquisition_time_not_provided"
                    ),
                    precision=timestamp_precision(fetched_at),
                    evidence_scope=locator,
                    content_version=version_id,
                )
            },
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO source_contents(
                source_id,run_id,kind,platform,origin_url,repo,path,version_id,version_kind,
                content_hash,content_excerpt,content_truncated,size,published_at,published_source,
                published_confidence,source_updated_at,fetched_at,fetched_at_source,
                time_provenance_json,detection_status
            ) VALUES (?, ?, 'event', ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?, ?, 'done')
            """,
            (
                source_id,
                run_id,
                event["platform"],
                event.get("source_locator"),
                None,
                content_path,
                version_id,
                version_kind,
                content_hash,
                event["redacted_value"],
                published_at,
                published_source,
                published_confidence,
                source_updated_at,
                fetched_at,
                fetched_source,
                _dump(incoming_provenance),
            ),
        )
        row = connection.execute(
            """
            SELECT * FROM source_contents
            WHERE source_id=? AND version_id=? AND path=? AND content_hash=?
            """,
            (source_id, version_id, content_path, content_hash),
        ).fetchone()
        if row is None:
            raise BackendStorageError("source_content_write_failed")
        existing_provenance = _load(row["time_provenance_json"], {})
        merged_provenance = merge_time_provenance(existing_provenance, incoming_provenance)
        updates: dict[str, Any] = {}
        merged_published = merged_provenance.get("published_at") or {}
        if merged_published.get("value") and (
            not row["published_at"]
            or str(existing_provenance.get("published_at", {}).get("value") or "")
            != str(merged_published.get("value") or "")
            or not row["published_source"]
            or row["published_confidence"] in {"", "none"}
        ):
            updates["published_at"] = merged_published["value"]
            updates["published_source"] = str(
                merged_published.get("source") or published_source or ""
            )
            updates["published_confidence"] = str(
                merged_published.get("confidence") or published_confidence or "none"
            )
        if not row["source_updated_at"] and source_updated_at:
            updates["source_updated_at"] = source_updated_at
        if not row["fetched_at"] and fetched_at:
            updates["fetched_at"] = fetched_at
        if row["fetched_at_source"] in {"unknown", "legacy_inferred"} and fetched_source == "connector":
            updates["fetched_at_source"] = "connector"
        if row["kind"] == "content":
            updates["kind"] = "event"
        if _dump(merged_provenance) != _dump(existing_provenance):
            updates["time_provenance_json"] = _dump(merged_provenance)
        if updates:
            assignments = ", ".join(f"{key}=?" for key in updates)
            connection.execute(
                f"UPDATE source_contents SET {assignments} WHERE id=?",
                (*updates.values(), row["id"]),
            )
        return int(row["id"])

    def ingest_content_items(
        self,
        source_id: int,
        run_id: int,
        items: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Persist scanned content versions without storing fetched content.

        Public connectors may read a page successfully while no enabled rule
        matches it. These records preserve scan coverage and time provenance
        while keeping the body and any unmasked secret out of SQLite.
        """
        normalized: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                raise BackendValidationError("content_item_must_be_object")
            platform = str(item.get("platform") or "").strip()
            path = str(item.get("path") or item.get("origin_url") or "").strip()
            version_id = str(item.get("version_id") or "").strip()
            content_hash = str(item.get("content_hash") or "").strip()
            if not platform or not path or not version_id or not content_hash:
                raise BackendValidationError("content_item_identity_required")
            normalized.append({
                **item,
                "platform": platform,
                "path": path,
                "version_id": version_id,
                "content_hash": content_hash,
            })

        def write(connection: sqlite3.Connection) -> dict[str, int]:
            source = connection.execute(
                "SELECT id FROM sources WHERE id=?", (source_id,)
            ).fetchone()
            if source is None:
                raise BackendValidationError("source_not_found")
            counts = {
                "content_items_seen": len(normalized),
                "content_items_written": 0,
                "content_items_existing": 0,
            }
            for item in normalized:
                published_at = item.get("published_at")
                published_source = str(item.get("published_source") or "")
                published_confidence = str(
                    item.get("published_confidence") or "none"
                )
                fetched_at = item.get("fetched_at")
                provenance = _load(item.get("time_provenance"), {})
                provenance = merge_time_provenance(
                    provenance,
                    {
                        "published_at": time_entry(
                            published_at,
                            source=published_source or "not_provided",
                            meaning=str(
                                item.get("published_meaning")
                                or "来源未提供可验证的公开或内容时间。"
                            ),
                            confidence=published_confidence,
                            evidence=str(item.get("published_evidence") or ""),
                            role=str(item.get("published_role") or (
                                "public_content_time" if published_at else "unknown"
                            )),
                            eligible_for_latency=bool(
                                item.get("published_eligible_for_latency")
                            ),
                            unknown_reason=str(
                                item.get("published_unknown_reason")
                                or ("" if published_at else "connector_not_provided")
                            ),
                            precision=timestamp_precision(published_at),
                            evidence_scope=str(item.get("origin_url") or item["path"]),
                            content_version=str(item["version_id"]),
                        ),
                        "fetched_at": time_entry(
                            fetched_at,
                            source="connector_content_read",
                            meaning="连接器完成读取当前内容的时间。",
                            confidence="high" if fetched_at else "none",
                            evidence=str(
                                item.get("origin_url") or item["path"]
                            ),
                            role="acquisition_completion",
                            eligible_for_latency=bool(fetched_at),
                            unknown_reason="" if fetched_at else "acquisition_time_not_provided",
                            precision=timestamp_precision(fetched_at),
                            evidence_scope=str(item.get("origin_url") or item["path"]),
                            content_version=str(item["version_id"]),
                        ),
                    },
                )
                origin_url = str(item.get("origin_url") or "")
                before = connection.total_changes
                connection.execute(
                    """
                    INSERT OR IGNORE INTO source_contents(
                        source_id,run_id,kind,platform,origin_url,repo,path,version_id,
                        version_kind,content_hash,content_excerpt,content_truncated,size,
                        published_at,published_source,published_confidence,source_updated_at,
                        fetched_at,fetched_at_source,time_provenance_json,detection_status
                    ) VALUES (?, ?, 'content', ?, ?, ?, ?, ?, ?, ?, NULL, 1, ?, ?, ?, ?, NULL, ?, 'connector', ?, 'done')
                    """,
                    (
                        source_id,
                        run_id,
                        item["platform"],
                        origin_url,
                        None,
                        item["path"],
                        item["version_id"],
                        str(item.get("version_kind") or "content_hash"),
                        item["content_hash"],
                        int(item.get("size") or 0),
                        published_at,
                        published_source,
                        published_confidence,
                        fetched_at,
                        _dump(provenance),
                    ),
                )
                if connection.total_changes > before:
                    counts["content_items_written"] += 1
                else:
                    counts["content_items_existing"] += 1
                row = connection.execute(
                    """
                    SELECT * FROM source_contents
                    WHERE source_id=? AND version_id=? AND path=? AND content_hash=?
                    """,
                    (
                        source_id,
                        item["version_id"],
                        item["path"],
                        item["content_hash"],
                    ),
                ).fetchone()
                if row is None:
                    raise BackendStorageError("source_content_write_failed")
                existing_provenance = _load(row["time_provenance_json"], {})
                merged_provenance = merge_time_provenance(
                    existing_provenance,
                    provenance,
                )
                updates: dict[str, Any] = {}
                merged_published = merged_provenance.get("published_at") or {}
                if not row["origin_url"] and origin_url:
                    updates["origin_url"] = origin_url
                if merged_published.get("value") and (
                    not row["published_at"]
                    or str(existing_provenance.get("published_at", {}).get("value") or "")
                    != str(merged_published.get("value") or "")
                    or not row["published_source"]
                    or row["published_confidence"] in {"", "none"}
                ):
                    updates["published_at"] = merged_published["value"]
                    updates["published_source"] = str(
                        merged_published.get("source") or published_source or ""
                    )
                    updates["published_confidence"] = str(
                        merged_published.get("confidence") or published_confidence or "none"
                    )
                if not row["fetched_at"] and fetched_at:
                    updates["fetched_at"] = fetched_at
                    updates["fetched_at_source"] = "connector"
                if _dump(merged_provenance) != _dump(existing_provenance):
                    updates["time_provenance_json"] = _dump(merged_provenance)
                if updates:
                    assignments = ", ".join(f"{key}=?" for key in updates)
                    connection.execute(
                        f"UPDATE source_contents SET {assignments} WHERE id=?",
                        (*updates.values(), row["id"]),
                    )
            return counts

        return self._execute(write, writable=True)

    @staticmethod
    def _location_hash(event: dict[str, Any], fingerprint: str) -> str:
        material = "|".join(
            str(event.get(key) or "")
            for key in (
                "channel",
                "platform",
                "source_locator",
                "_version_id",
                "_version_kind",
                "_content_hash",
                "_content_path",
                "line_start",
            )
        )
        return hashlib.sha256(f"{fingerprint}|{material}".encode("utf-8")).hexdigest()

    def ingest_events(self, source_id: int, run_id: int, events: list[dict[str, Any]]) -> dict[str, int]:
        records: list[dict[str, Any]] = []
        for item in events:
            if not isinstance(item, dict):
                raise BackendValidationError("event_must_be_object")
            public_item = {key: value for key, value in item.items() if not key.startswith("_")}
            record = EventRecord.model_validate(public_item).model_dump()
            # These fields are an in-process connector contract only. They are
            # consumed below and never returned by any API response.
            record["_secret"] = item.get("_secret")
            record["_paired"] = item.get("_paired") or {}
            record["_vendor"] = item.get("_vendor") or ""
            record["_rule_id"] = item.get("_rule_id") or item.get("detection_method") or ""
            record["_rule_version"] = int(item.get("_rule_version") or 1)
            record["_confidence"] = int(item.get("_confidence") or 0)
            record["_published_at"] = item.get("_published_at")
            record["_published_source"] = item.get("_published_source") or ""
            record["_published_confidence"] = item.get("_published_confidence") or "none"
            record["_published_evidence"] = item.get("_published_evidence") or item.get("published_evidence") or ""
            record["_published_meaning"] = item.get("_published_meaning") or item.get("published_meaning") or ""
            record["_published_eligible_for_latency"] = item.get(
                "_published_eligible_for_latency",
                item.get("public_time_status") == "known",
            )
            record["_source_updated_at"] = item.get("_source_updated_at")
            record["_source_updated_source"] = item.get("_source_updated_source") or item.get("source_updated_source") or ""
            record["_source_updated_confidence"] = item.get(
                "_source_updated_confidence",
                item.get("source_updated_confidence") or "none",
            )
            record["_source_updated_meaning"] = item.get("_source_updated_meaning") or item.get("source_updated_meaning") or ""
            record["_source_updated_evidence"] = item.get("_source_updated_evidence") or item.get("source_updated_evidence") or ""
            record["_version_id"] = item.get("_version_id") or item.get("event_id")
            record["_version_kind"] = item.get("_version_kind") or "event"
            record["_content_hash"] = item.get("_content_hash")
            record["_content_path"] = item.get("_content_path")
            record["_fetched_at"] = item.get("_fetched_at")
            record["_detection_duration_ms"] = item.get("_detection_duration_ms")
            record["_first_discovered_at"] = item.get("_first_discovered_at") or item.get("first_discovered_at")
            record["_last_discovered_at"] = item.get("_last_discovered_at") or item.get("last_discovered_at")
            records.append(record)
        now = _now()

        notification_adapter = OfflineSQLiteNotificationAdapter()

        def write(connection: sqlite3.Connection) -> dict[str, int]:
            source = connection.execute("SELECT id FROM sources WHERE id=?", (source_id,)).fetchone()
            if source is None:
                raise BackendValidationError("source_not_found")
            counts = {
                "candidates": len(records),
                "new_credentials": 0,
                "new_occurrences": 0,
                "skipped_occurrences": 0,
            }
            for event in records:
                fingerprint = event.get("credential_fingerprint") or f"event:{event['event_id']}"
                first_seen_at = event.get("first_observed_at") or ""
                first_discovered_at = (
                    event.get("_first_discovered_at")
                    or event.get("first_discovered_at")
                )
                last_discovered_at = (
                    event.get("_last_discovered_at")
                    or event.get("last_discovered_at")
                    or first_discovered_at
                )
                existing = connection.execute(
                    "SELECT * FROM credentials WHERE fingerprint=?", (fingerprint,)
                ).fetchone()
                if existing is None:
                    cursor = connection.execute(
                        """
                        INSERT INTO credentials(
                            fingerprint,type,vendor,rule_id,rule_version,confidence,secret_encrypted,
                            paired_encrypted,preview,secret_available,review_status,verification_status,
                            verified_at,first_seen_at,first_discovered_at,last_discovered_at,
                            last_seen_at,created_at
                        ) VALUES (?, ?, '', ?, 1, ?, ?, ?, ?, ?, 'pending', 'not_requested', NULL, ?, ?, ?, ?, ?)
                        """,
                        (
                            fingerprint,
                            event["credential_type"],
                            event["_rule_id"],
                            event["_confidence"],
                            self._encrypt(event.get("_secret")),
                            self._encrypt(_dump(event["_paired"])) if event["_paired"] else None,
                            event["redacted_value"],
                            1 if event.get("_secret") else 0,
                            first_seen_at,
                            first_discovered_at,
                            last_discovered_at,
                            first_seen_at,
                            now,
                        ),
                    )
                    credential_id = int(cursor.lastrowid)
                    counts["new_credentials"] += 1
                else:
                    credential_id = int(existing["id"])
                    incoming_confidence = int(event.get("_confidence") or 0)
                    should_upgrade_rule = incoming_confidence > int(existing["confidence"] or 0)
                    if should_upgrade_rule:
                        connection.execute(
                            """
                            UPDATE credentials
                            SET type=?,
                                vendor=?,
                                rule_id=?,
                                rule_version=?,
                                confidence=?,
                                preview=?,
                                verification_status='not_requested',
                                verified_at=NULL,
                                first_discovered_at=COALESCE(first_discovered_at, ?),
                                last_discovered_at=COALESCE(?, last_discovered_at),
                                last_seen_at=?
                            WHERE id=?
                            """,
                            (
                                event["credential_type"],
                                event.get("_vendor") or "",
                                event["_rule_id"],
                                int(event.get("_rule_version") or 1),
                                incoming_confidence,
                                event["redacted_value"],
                                first_discovered_at,
                                last_discovered_at,
                                now,
                                credential_id,
                            ),
                        )
                    else:
                        connection.execute(
                            """
                            UPDATE credentials
                            SET confidence=MAX(confidence, ?),
                                first_discovered_at=COALESCE(first_discovered_at, ?),
                                last_discovered_at=COALESCE(?, last_discovered_at),
                                last_seen_at=?
                            WHERE id=?
                            """,
                            (
                                incoming_confidence,
                                first_discovered_at,
                                last_discovered_at,
                                now,
                                credential_id,
                            ),
                        )
                    if event.get("_secret") and not existing["secret_available"]:
                        connection.execute(
                            """
                            UPDATE credentials
                            SET secret_encrypted=?, paired_encrypted=?, secret_available=1
                            WHERE id=?
                            """,
                            (
                                self._encrypt(event["_secret"]),
                                self._encrypt(_dump(event["_paired"])) if event["_paired"] else None,
                                credential_id,
                            ),
                        )
                content_id = self._upsert_content(connection, source_id, run_id, event)
                location_hash = self._location_hash(event, fingerprint)
                version_id = str(event.get("_version_id") or event.get("event_id") or "")
                version_kind = str(event.get("_version_kind") or "event")
                content_hash = str(event.get("_content_hash") or "")
                content_path = str(event.get("_content_path") or event.get("source_locator") or event["event_id"])
                occurrence_provenance = _load(event.get("time_provenance"), {})
                occurrence_provenance = merge_time_provenance(
                    occurrence_provenance,
                    {
                        "first_observed_at": time_entry(
                            event.get("first_observed_at"),
                            source="event_record",
                            meaning="本系统首次观测当前内容位置的时间。",
                            confidence="medium" if event.get("first_observed_at") else "none",
                            role="system_observation",
                            unknown_reason=(
                                "" if event.get("first_observed_at")
                                else "observation_time_not_provided"
                            ),
                        ),
                        "detected_at": time_entry(
                            event.get("detected_at"),
                            source=(
                                "detection_completed"
                                if event.get("_detection_duration_ms") is not None
                                else "event_record"
                            ),
                            meaning="规则检测实际完成的时间；处理耗时另用单调时钟测量。",
                            confidence="high" if event.get("_detection_duration_ms") is not None else "medium",
                            evidence=(
                                f"duration_ms={event.get('_detection_duration_ms')}"
                                if event.get("_detection_duration_ms") is not None
                                else ""
                            ),
                            role="detection_completion",
                            eligible_for_latency=event.get("_detection_duration_ms") is not None,
                            unknown_reason=(
                                "" if event.get("detected_at")
                                else "detection_time_not_provided"
                            ),
                        ),
                        "first_discovered_at": time_entry(
                            event.get("_first_discovered_at") or event.get("first_discovered_at"),
                            source=(
                                "first_valid_detection"
                                if event.get("_first_discovered_at") or event.get("first_discovered_at")
                                else "not_provided"
                            ),
                            meaning="规则检测实际形成有效凭据结果的时间；按当前内容版本和出现位置记录。",
                            confidence=(
                                "high"
                                if event.get("_first_discovered_at") or event.get("first_discovered_at")
                                else "none"
                            ),
                            evidence=(
                                f"duration_ms={event.get('_detection_duration_ms')}"
                                if event.get("_detection_duration_ms") is not None
                                else ""
                            ),
                            role="first_valid_detection",
                            eligible_for_latency=bool(
                                event.get("_first_discovered_at") or event.get("first_discovered_at")
                            ),
                            unknown_reason=(
                                ""
                                if event.get("_first_discovered_at") or event.get("first_discovered_at")
                                else "first_discovery_time_not_saved"
                            ),
                            precision=timestamp_precision(
                                event.get("_first_discovered_at")
                                or event.get("first_discovered_at")
                            ),
                            evidence_scope=content_path,
                            content_version=version_id,
                        ),
                    },
                )
                occurrence_evidence = {
                    "evidence_level": event["evidence_level"],
                    "risk_level": event["risk_level"],
                    "source_kind": event["source_kind"],
                    "detection_method": event.get("detection_method"),
                    "fingerprint_key_version": event.get("fingerprint_key_version"),
                    "published_at": event.get("_published_at") or event.get("published_at"),
                    "published_source": event.get("_published_source") or event.get("published_source") or "",
                    "published_confidence": event.get("_published_confidence") or event.get("published_confidence") or "none",
                    "published_meaning": event.get("published_meaning") or "",
                    "published_evidence": event.get("published_evidence") or "",
                    "source_updated_at": event.get("_source_updated_at") or event.get("source_updated_at"),
                    "source_updated_source": event.get("_source_updated_source") or event.get("source_updated_source") or "",
                    "disposition": event["disposition"],
                }
                before = connection.total_changes
                connection.execute(
                    """
                    INSERT OR IGNORE INTO occurrences(
                        credential_id,content_id,run_id,source_event_id,category,platform,origin_url,
                        repo,path,line_start,version_id,version_kind,context_masked,content_hash,
                        location_hash,evidence_json,time_provenance_json,detected_at,
                        first_discovered_at,last_discovered_at,first_seen_at,last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        credential_id,
                        content_id,
                        run_id,
                        event["event_id"],
                        event["channel"],
                        event["platform"],
                        event.get("source_locator"),
                        None,
                        content_path,
                        None,
                        version_id,
                        version_kind,
                        event["redacted_value"],
                        content_hash or None,
                        location_hash,
                        _dump(occurrence_evidence),
                        _dump(occurrence_provenance),
                        event.get("detected_at") or now,
                        first_discovered_at,
                        last_discovered_at,
                        first_seen_at,
                        now,
                    ),
                )
                if connection.total_changes > before:
                    counts["new_occurrences"] += 1
                    notification_adapter.enqueue(
                        connection,
                        notification_adapter.create_event(
                            event_id=event["event_id"],
                            severity=event["risk_level"],
                            source_kind=event["source_kind"],
                            fingerprint=event.get("credential_fingerprint"),
                            created_at=_now(),
                        ),
                        note="离线通知适配层：仅写入统一后端 SQLite，未发送外部消息。",
                    )
                else:
                    counts["skipped_occurrences"] += 1
                    existing_occurrence = connection.execute(
                        """
                        SELECT id,evidence_json,time_provenance_json,
                               first_discovered_at,last_discovered_at,detected_at
                        FROM occurrences
                        WHERE credential_id=? AND location_hash=?
                        """,
                        (credential_id, location_hash),
                    ).fetchone()
                    if existing_occurrence is not None:
                        existing_evidence = _load(existing_occurrence["evidence_json"], {})
                        merged_evidence = dict(existing_evidence)
                        for key, value in occurrence_evidence.items():
                            if key not in merged_evidence or merged_evidence.get(key) in (None, "", "none"):
                                merged_evidence[key] = value
                        existing_provenance = _load(existing_occurrence["time_provenance_json"], {})
                        merged_provenance = merge_time_provenance(
                            existing_provenance,
                            occurrence_provenance,
                        )
                        latest_discovery = last_discovered_at
                        first_discovery_update = None
                        incoming_discovery = occurrence_provenance.get("first_discovered_at") or {}
                        existing_discovery = existing_provenance.get("first_discovered_at") or {}
                        incoming_discovery_is_valid = first_discovery_eligible(
                            occurrence_provenance
                        )
                        existing_discovery_is_valid = first_discovery_eligible(
                            existing_provenance
                        )
                        if (
                            incoming_discovery_is_valid
                            and incoming_discovery.get("value")
                            and (
                                not existing_occurrence["first_discovered_at"]
                                or not existing_discovery_is_valid
                            )
                        ):
                            first_discovery_update = incoming_discovery["value"]
                            merged_discovery = dict(incoming_discovery)
                            if existing_occurrence["first_discovered_at"] and not existing_discovery_is_valid:
                                merged_discovery["recovery_note"] = (
                                    "旧记录首次发现时间证据不足；"
                                    "本次重检时间作为当前最早有证据的检测时间。"
                                )
                            merged_provenance["first_discovered_at"] = merged_discovery
                        detected_update = (
                            event.get("detected_at")
                            if not existing_occurrence["detected_at"]
                            else None
                        )
                        if (
                            _dump(merged_evidence) != _dump(existing_evidence)
                            or _dump(merged_provenance) != _dump(existing_provenance)
                            or latest_discovery
                            or first_discovery_update
                            or detected_update
                        ):
                            connection.execute(
                                """
                                UPDATE occurrences
                                SET evidence_json=?,time_provenance_json=?,
                                    detected_at=COALESCE(detected_at, ?),
                                    first_discovered_at=COALESCE(?, first_discovered_at),
                                    last_discovered_at=COALESCE(?, last_discovered_at),
                                    last_seen_at=?
                                WHERE id=?
                                """,
                                (
                                    _dump(merged_evidence),
                                    _dump(merged_provenance),
                                    detected_update,
                                    first_discovery_update,
                                    latest_discovery,
                                    now,
                                    existing_occurrence["id"],
                                ),
                            )
            return counts

        return self._execute(write, writable=True)

    def list_notifications(self, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))

        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            if status:
                rows = connection.execute(
                    "SELECT * FROM notification_outbox WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM notification_outbox ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [
                {
                    **(self._row(row) or {}),
                    "payload": _load(row["payload_json"], {}),
                }
                for row in rows
            ]

        return self._execute(read)

    def record_notification(self, notification_id: str) -> dict[str, Any] | None:
        safe_id = str(notification_id or "").strip()
        if not safe_id or len(safe_id) > 160:
            raise BackendValidationError("notification_id_invalid")
        now = _now()

        def write(connection: sqlite3.Connection) -> dict[str, Any] | None:
            OfflineSQLiteNotificationAdapter().record(
                connection,
                safe_id,
                now=now,
                note="离线通知适配层已记录；未访问外部通知通道。",
            )
            row = connection.execute(
                "SELECT * FROM notification_outbox WHERE notification_id=?", (safe_id,)
            ).fetchone()
            if row is None:
                return None
            return {**(self._row(row) or {}), "payload": _load(row["payload_json"], {})}

        return self._execute(write, writable=True)

    def retry_notification(self, notification_id: str) -> dict[str, Any] | None:
        safe_id = str(notification_id or "").strip()
        if not safe_id or len(safe_id) > 160:
            raise BackendValidationError("notification_id_invalid")
        now = _now()

        def write(connection: sqlite3.Connection) -> dict[str, Any] | None:
            OfflineSQLiteNotificationAdapter().retry(
                connection,
                safe_id,
                now=now,
                note="离线通知适配层已重新排队；仍未访问外部通知通道。",
            )
            row = connection.execute(
                "SELECT * FROM notification_outbox WHERE notification_id=?", (safe_id,)
            ).fetchone()
            if row is None:
                return None
            return {**(self._row(row) or {}), "payload": _load(row["payload_json"], {})}

        return self._execute(write, writable=True)

    def list_findings(
        self,
        *,
        query: str | None = None,
        credential_type: str | None = None,
        review: str | None = None,
        verify: str | None = None,
        platform: str | None = None,
        source_kind: str | None = "真实检测结果",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = max(1, min(100, int(page_size)))

        def read(connection: sqlite3.Connection) -> dict[str, Any]:
            clauses = ["1=1"]
            values: list[Any] = []
            if credential_type:
                clauses.append("c.type=?")
                values.append(credential_type)
            if review:
                clauses.append("c.review_status=?")
                values.append(review)
            if verify:
                clauses.append("c.verification_status=?")
                values.append(verify)
            if platform:
                clauses.append("EXISTS (SELECT 1 FROM occurrences op WHERE op.credential_id=c.id AND op.platform=?)")
                values.append(platform)
            if query:
                clauses.append(
                    "(CAST(c.id AS TEXT) LIKE ? OR c.preview LIKE ? OR c.type LIKE ? OR c.vendor LIKE ?)"
                )
                like = f"%{query}%"
                values.extend([like, like, like, like])
            real_only = str(source_kind or "真实检测结果").strip() not in {"", "全部来源", "all", "*"}
            verified_credential_only = "c.verification_status='valid'"
            if real_only:
                clauses.append(
                    "("
                    "EXISTS ("
                    "SELECT 1 FROM occurrences source_occurrence "
                    "WHERE source_occurrence.credential_id=c.id "
                    "AND json_extract(source_occurrence.evidence_json, '$.source_kind')=?"
                    ") "
                    "AND NOT (c.type='gitee_token' AND c.verification_status='invalid')"
                    f" OR {verified_credential_only}"
                    ")"
                )
                values.append(str(source_kind or "真实检测结果").strip())
            where = " AND ".join(clauses)
            occurrence_scope = (
                " AND ("
                "json_extract(o.evidence_json, '$.source_kind')=?"
                f" OR {verified_credential_only}"
                ")"
                if real_only
                else ""
            )
            occurrence_values = [str(source_kind or "真实检测结果").strip()] if real_only else []
            total = int(connection.execute(
                f"SELECT COUNT(*) AS n FROM credentials c WHERE {where}", values
            ).fetchone()["n"])
            unified_count = int(connection.execute(
                f"""
                SELECT COUNT(*) AS n
                FROM occurrences o
                JOIN credentials c ON c.id=o.credential_id
                WHERE {where}{occurrence_scope}
                """,
                [*values, *occurrence_values],
            ).fetchone()["n"])
            pending_review_count = int(connection.execute(
                f"SELECT COUNT(*) AS n FROM credentials c WHERE {where} AND c.review_status='pending'",
                values,
            ).fetchone()["n"])
            pending_verification_count = int(connection.execute(
                f"""
                SELECT COUNT(*) AS n
                FROM credentials c
                WHERE {where} AND c.verification_status IN ('not_requested','not_configured','queued','running')
                """,
                values,
            ).fetchone()["n"])
            rows = connection.execute(
                f"""
                SELECT c.*,
                       (SELECT COUNT(*) FROM occurrences o WHERE o.credential_id=c.id{occurrence_scope}) AS occurrence_count,
                       (SELECT MIN(o.first_seen_at) FROM occurrences o WHERE o.credential_id=c.id{occurrence_scope}) AS first_observed_at,
                       (SELECT MIN(o.first_discovered_at) FROM occurrences o WHERE o.credential_id=c.id{occurrence_scope}) AS first_discovered_at,
                       (SELECT MAX(o.last_discovered_at) FROM occurrences o WHERE o.credential_id=c.id{occurrence_scope}) AS last_discovered_at,
                       (SELECT MIN(o.detected_at) FROM occurrences o WHERE o.credential_id=c.id{occurrence_scope}) AS detected_at,
                       (SELECT MIN(sc.fetched_at)
                        FROM occurrences o JOIN source_contents sc ON sc.id=o.content_id
                        WHERE o.credential_id=c.id{occurrence_scope}) AS fetched_at,
                       (SELECT MIN(sc.published_at)
                        FROM occurrences o JOIN source_contents sc ON sc.id=o.content_id
                        WHERE o.credential_id=c.id{occurrence_scope} AND sc.published_at IS NOT NULL) AS published_at,
                       (SELECT MIN(rl.created_at)
                        FROM review_logs rl
                        WHERE rl.credential_id=c.id AND rl.action='confirm') AS public_confirmed_at
                FROM credentials c WHERE {where}
                ORDER BY
                    CASE WHEN c.verification_status='valid' THEN 0 ELSE 1 END,
                    c.last_seen_at DESC,
                    c.id DESC
                LIMIT ? OFFSET ?
                """,
                [
                    *occurrence_values,
                    *occurrence_values,
                    *occurrence_values,
                    *occurrence_values,
                    *occurrence_values,
                    *occurrence_values,
                    *occurrence_values,
                    *values,
                    page_size,
                    (page - 1) * page_size,
                ],
            ).fetchall()
            items = []
            for row in rows:
                item = self._credential_output(row)
                chain = self._credential_time_chain(connection, int(row["id"]))
                item.update(chain)
                items.append(item)
            return {
                "source": "真实数据：规范化后端 SQLite credentials/occurrences/notification_outbox",
                "implementation_status": "部分实现",
                "error": None,
                "total": total,
                "page": page,
                "page_size": page_size,
                "summary": {
                    "unified_finding_count": unified_count,
                    "unique_finding_count": total,
                    "pending_review_count": pending_review_count,
                    "pending_verification_count": pending_verification_count,
                    "source_kind": (
                        "真实检测结果及有效 GitHub/Gitee 验证样例"
                        if real_only
                        else "全部来源"
                    ),
                },
                "scope": (
                    "真实检测结果及有效 GitHub/Gitee 验证样例"
                    if real_only
                    else "全部来源"
                ),
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total_items": total,
                    "total_pages": (total + page_size - 1) // page_size if total else 0,
                },
                "items": items,
            }

        return self._execute(read)

    def find_credential_by_fingerprint(self, fingerprint: str) -> dict[str, Any] | None:
        """Resolve an event's credential without depending on a paginated view."""
        normalized = str(fingerprint or "").strip()
        if not normalized:
            return None

        def read(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                """
                SELECT id, fingerprint, review_status, verification_status
                FROM credentials
                WHERE fingerprint=?
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
            return self._row(row) if row else None

        return self._execute(read)

    def _credential_time_chain(
        self,
        connection: sqlite3.Connection,
        credential_id: int,
    ) -> dict[str, Any]:
        rows = connection.execute(
            """
            SELECT o.id AS occurrence_id,o.first_seen_at,o.first_discovered_at,
                   o.last_discovered_at,o.detected_at,o.evidence_json,
                   o.time_provenance_json AS occurrence_time_provenance_json,
                   sc.published_at,sc.published_source,sc.published_confidence,
                   sc.fetched_at,sc.fetched_at_source,
                   sc.time_provenance_json AS content_time_provenance_json
            FROM occurrences o
            LEFT JOIN source_contents sc ON sc.id=o.content_id
            WHERE o.credential_id=?
            ORDER BY o.id
            """,
            (credential_id,),
        ).fetchall()
        if not rows:
            return {
                "published_at": None,
                "public_time_status": "unknown",
                "public_time_unknown_reason": "no_occurrence_evidence",
                "first_discovered_at": None,
                "last_discovered_at": None,
                "discovery_latency_seconds": None,
                "discovery_latency_status": "not_calculable",
                "discovery_latency_reason": "no_occurrence_evidence",
                "discovery_latency_calculation_type": "not_calculable",
                "discovery_latency_basis": "",
                "discovery_latency_end_at": None,
                "time_chain_scope": "credential_has_no_occurrence",
            }
        chains = [(_discovery_time_chain(row), row) for row in rows]
        calculable = [
            pair for pair in chains
            if pair[0]["discovery_latency_status"] == "calculated"
        ]
        chosen_chain, chosen_row = min(
            calculable or chains,
            key=lambda pair: (
                _parse_time(pair[0].get("first_discovered_at"))
                or datetime.max.replace(tzinfo=timezone.utc),
                int(pair[1]["occurrence_id"]),
            ),
        )
        return {
            "published_at": chosen_chain["public_time"],
            "public_time_status": chosen_chain["public_time_status"],
            "public_time_unknown_reason": chosen_chain["public_time_unknown_reason"],
            "first_discovered_at": chosen_chain["first_discovered_at"],
            "last_discovered_at": chosen_row["last_discovered_at"],
            "discovery_latency_seconds": chosen_chain["discovery_latency_seconds"],
            "discovery_latency_status": chosen_chain["discovery_latency_status"],
            "discovery_latency_reason": chosen_chain["discovery_latency_reason"],
            "discovery_latency_calculation_type": chosen_chain["discovery_latency_calculation_type"],
            "discovery_latency_basis": chosen_chain["discovery_latency_basis"],
            "discovery_latency_end_at": chosen_chain["discovery_latency_end_at"],
            "time_chain_scope": (
                f"occurrence_id={chosen_row['occurrence_id']};"
                "凭据汇总选取一条同来源、同版本、同出现位置链路"
            ),
        }

    def _credential_output(self, row: sqlite3.Row, include_secret: bool = False) -> dict[str, Any]:
        item = {
            "id": int(row["id"]),
            "type": row["type"],
            "vendor": row["vendor"],
            "confidence": int(row["confidence"]),
            "preview": row["preview"],
            "review_status": row["review_status"],
            "verification_status": row["verification_status"],
            "verified_at": _iso(row["verified_at"]),
            "rule_id": row["rule_id"],
            "rule_version": int(row["rule_version"]),
            "fingerprint": row["fingerprint"],
            "secret_available": bool(row["secret_available"]),
            "first_seen_at": _iso(row["first_seen_at"]),
            "first_observed_at": _iso(row["first_observed_at"]) if "first_observed_at" in row.keys() else _iso(row["first_seen_at"]),
            "first_discovered_at": _iso(row["first_discovered_at"]) if "first_discovered_at" in row.keys() else None,
            "last_discovered_at": _iso(row["last_discovered_at"]) if "last_discovered_at" in row.keys() else None,
            "published_at": _iso(row["published_at"]) if "published_at" in row.keys() else None,
            "fetched_at": _iso(row["fetched_at"]) if "fetched_at" in row.keys() else None,
            "detected_at": _iso(row["detected_at"]) if "detected_at" in row.keys() else None,
            "public_confirmed_at": _iso(row["public_confirmed_at"]) if "public_confirmed_at" in row.keys() else None,
            "last_seen_at": _iso(row["last_seen_at"]),
            "occurrence_count": int(row["occurrence_count"]) if "occurrence_count" in row.keys() else 0,
        }
        if include_secret:
            item["secret"] = self._decrypt(row["secret_encrypted"])
        return item

    def finding_detail(self, credential_id: int) -> dict[str, Any] | None:
        def read(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                """
                SELECT c.*,
                       (SELECT COUNT(*) FROM occurrences o WHERE o.credential_id=c.id) AS occurrence_count,
                       (SELECT MIN(o.first_seen_at) FROM occurrences o WHERE o.credential_id=c.id) AS first_observed_at,
                       (SELECT MIN(o.first_discovered_at) FROM occurrences o WHERE o.credential_id=c.id) AS first_discovered_at,
                       (SELECT MAX(o.last_discovered_at) FROM occurrences o WHERE o.credential_id=c.id) AS last_discovered_at,
                       (SELECT MIN(o.detected_at) FROM occurrences o WHERE o.credential_id=c.id) AS detected_at,
                       (SELECT MIN(sc.fetched_at)
                        FROM occurrences o JOIN source_contents sc ON sc.id=o.content_id
                        WHERE o.credential_id=c.id) AS fetched_at,
                       (SELECT MIN(sc.published_at)
                        FROM occurrences o JOIN source_contents sc ON sc.id=o.content_id
                        WHERE o.credential_id=c.id AND sc.published_at IS NOT NULL) AS published_at,
                       (SELECT MIN(rl.created_at)
                        FROM review_logs rl
                        WHERE rl.credential_id=c.id AND rl.action='confirm') AS public_confirmed_at
                FROM credentials c WHERE c.id=?
                """,
                (credential_id,),
            ).fetchone()
            if row is None:
                return None
            item = self._credential_output(row)
            item.update(self._credential_time_chain(connection, int(row["id"])))
            locations = connection.execute(
                """
                SELECT o.id AS id,o.source_event_id,o.category,o.platform,o.origin_url,o.repo,o.path,o.line_start,
                       version_id,version_kind,context_masked,evidence_json,detected_at,
                       first_discovered_at,last_discovered_at,
                       first_seen_at,last_seen_at,content_id,
                       sc.published_at,sc.published_source,sc.published_confidence,
                       sc.source_updated_at,sc.fetched_at,sc.fetched_at_source,
                       o.time_provenance_json AS occurrence_time_provenance_json,
                       sc.time_provenance_json AS content_time_provenance_json
                FROM occurrences o
                LEFT JOIN source_contents sc ON sc.id=o.content_id
                WHERE o.credential_id=? ORDER BY o.first_seen_at
                """,
                (credential_id,),
            ).fetchall()
            item["locations"] = []
            for location in locations:
                location_item = {
                    **(self._row(location) or {}),
                    "evidence": _load(location["evidence_json"], {}),
                }
                provenance = _row_time_provenance(location)
                location_item["time_provenance"] = provenance
                location_item.update(_public_time_fields(provenance))
                location_item.update(_discovery_time_chain(location))
                item["locations"].append(location_item)
            item["time_note"] = (
                "公开时间、内容版本修订时间、首次观测和检测时间按出现位置分别保存；"
                "同一凭据的不同内容版本不会共用一个公开时间。"
            )
            logs = connection.execute(
                "SELECT action,note,actor,created_at FROM review_logs WHERE credential_id=? ORDER BY id DESC",
                (credential_id,),
            ).fetchall()
            item["review_logs"] = [self._row(log) for log in logs]
            history = connection.execute(
                """
                SELECT verifier_id,verifier_version,status,evidence_json,latency_ms,created_at
                FROM verification_results WHERE credential_id=? ORDER BY id DESC
                """,
                (credential_id,),
            ).fetchall()
            item["verification_history"] = [
                {**(self._row(result) or {}), "evidence": _load(result["evidence_json"], {})}
                for result in history
            ]
            return item

        return self._execute(read)

    def reveal(self, credential_id: int, actor: str = "local") -> dict[str, Any]:
        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM credentials WHERE id=?", (credential_id,)).fetchone()
            if row is None:
                raise BackendValidationError("credential_not_found")
            if not row["secret_available"] or not row["secret_encrypted"]:
                raise BackendValidationError("credential_value_unavailable")
            secret = self._decrypt(row["secret_encrypted"])
            connection.execute(
                "INSERT INTO review_logs(credential_id,action,note,actor,created_at) VALUES (?, 'reveal_access', ?, ?, ?)",
                (credential_id, "显式查看原文；原文不回写审计日志。", actor, _now()),
            )
            return {"credential_id": credential_id, "secret": secret}

        return self._execute(write, writable=True)

    def review(self, credential_id: int, action: str, note: str = "", actor: str = "local") -> dict[str, Any]:
        transitions = {
            "confirm": ("pending", "confirmed"),
            "false_positive": ("pending", "false_positive"),
            "ignore": ("pending", "ignored"),
            "restore": (("confirmed", "false_positive", "ignored"), "pending"),
        }
        if action not in transitions:
            raise BackendValidationError("review_action_invalid")
        if len(note) > 500 or "\n" in note or "\r" in note:
            raise BackendValidationError("review_note_invalid")
        expected, target = transitions[action]

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM credentials WHERE id=?", (credential_id,)).fetchone()
            if row is None:
                raise BackendValidationError("credential_not_found")
            allowed = expected if isinstance(expected, tuple) else (expected,)
            if row["review_status"] not in allowed:
                raise BackendValidationError("review_transition_invalid")
            now = _now()
            connection.execute(
                "UPDATE credentials SET review_status=?,last_seen_at=? WHERE id=?",
                (target, now, credential_id),
            )
            disposition = {
                "confirm": "已确认公开泄露",
                "false_positive": "已标记误报",
                "ignore": "已忽略",
                "restore": "待确认",
            }[action]
            occurrences = connection.execute(
                "SELECT id,evidence_json FROM occurrences WHERE credential_id=?",
                (credential_id,),
            ).fetchall()
            for occurrence in occurrences:
                evidence = _load(occurrence["evidence_json"], {})
                source_kind = str(evidence.get("source_kind") or "")
                # A controlled or offline record can be confirmed as a
                # controlled finding, but must never become a public leak.
                updated_disposition = (
                    disposition
                    if source_kind == "真实检测结果"
                    else "受控环境确认"
                    if action == "confirm"
                    else disposition
                )
                evidence["disposition"] = updated_disposition
                if action == "confirm":
                    evidence["manual_confirmed_at"] = now
                connection.execute(
                    "UPDATE occurrences SET evidence_json=? WHERE id=?",
                    (_dump(evidence), occurrence["id"]),
                )
            safe_note = "备注已省略，避免持久化潜在敏感信息。" if note.strip() else ""
            connection.execute(
                "INSERT INTO review_logs(credential_id,action,note,actor,created_at) VALUES (?, ?, ?, ?, ?)",
                (credential_id, action, safe_note, actor, now),
            )
            return {
                "credential_id": credential_id,
                "action": action,
                "review_status": target,
                "created_at": now,
                "note": safe_note,
            }

        return self._execute(write, writable=True)

    def enqueue_verification(self, credential_id: int, requested_by: str = "single") -> dict[str, Any]:
        runtime = effective_settings(settings)
        authorized_endpoints = runtime.authorized_verification_endpoints

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM credentials WHERE id=?", (credential_id,)).fetchone()
            if row is None:
                raise BackendValidationError("credential_not_found")
            platform_row = connection.execute(
                "SELECT platform FROM occurrences WHERE credential_id=? ORDER BY id LIMIT 1",
                (credential_id,),
            ).fetchone()
            platform = str(platform_row["platform"] or "") if platform_row else ""
            verifier_id = (
                verifier_for_type(
                    row["type"],
                    authorized_endpoints,
                    platform=platform,
                )
                or f"unsupported:{row['type']}"
            )
            existing = connection.execute(
                """
                SELECT id,verifier_id,status FROM verification_jobs
                WHERE credential_id=? AND status IN ('queued','running')
                ORDER BY id DESC LIMIT 1
                """,
                (credential_id,),
            ).fetchone()
            if existing is not None:
                return {
                    "queued": True,
                    "job_id": int(existing["id"]),
                    "verifier": existing["verifier_id"],
                    "deduplicated": True,
                }
            last_result = connection.execute(
                """
                SELECT vr.verifier_id,vr.status
                FROM verification_results vr
                WHERE vr.credential_id=?
                ORDER BY vr.id DESC LIMIT 1
                """,
                (credential_id,),
            ).fetchone()
            if (
                last_result is not None
                and last_result["verifier_id"] == verifier_id
                and last_result["status"] in {
                    "valid",
                    "invalid",
                    "controlled_valid",
                    "not_configured",
                    "unsupported",
                    "missing_context",
                }
            ):
                return {
                    "queued": False,
                    "deduplicated": True,
                    "already_verified": True,
                    "verifier": verifier_id,
                    "status": last_result["status"],
                }
            now = _now()
            cursor = connection.execute(
                """
                INSERT INTO verification_jobs(credential_id,verifier_id,requested_by,status,created_at)
                VALUES (?, ?, ?, 'queued', ?)
                """,
                (credential_id, verifier_id, requested_by, now),
            )
            connection.execute(
                "UPDATE credentials SET verification_status='queued' WHERE id=?", (credential_id,)
            )
            return {
                "queued": True,
                "job_id": int(cursor.lastrowid),
                "verifier": verifier_id,
                "platform": platform,
            }

        return self._execute(write, writable=True)

    def process_verification_jobs(self, limit: int = 20) -> dict[str, int]:
        limit = max(1, min(100, int(limit)))
        stats = {
            "processed": 0,
            "valid": 0,
            "controlled_valid": 0,
            "invalid": 0,
            "inconclusive": 0,
            "not_configured": 0,
            "missing_context": 0,
            "unsupported": 0,
            "rate_limited": 0,
            "network_error": 0,
            "error": 0,
        }
        runtime = effective_settings(settings)
        authorized_endpoints = runtime.authorized_verification_endpoints
        controlled_fingerprints = load_controlled_verification_fingerprints(
            runtime.controlled_verification_manifest_path
        )

        def claim(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                "SELECT * FROM verification_jobs WHERE status='queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            now = _now()
            changed = connection.execute(
                "UPDATE verification_jobs SET status='running',started_at=? WHERE id=? AND status='queued'",
                (now, row["id"]),
            ).rowcount
            if changed != 1:
                return None
            connection.execute(
                "UPDATE credentials SET verification_status='running' WHERE id=?",
                (row["credential_id"],),
            )
            return {**(self._row(row) or {}), "started_at": now}

        def credential_view(connection: sqlite3.Connection, credential_id: int) -> dict[str, Any] | None:
            row = connection.execute("SELECT * FROM credentials WHERE id=?", (credential_id,)).fetchone()
            if row is None:
                return None
            platform_row = connection.execute(
                "SELECT platform FROM occurrences WHERE credential_id=? ORDER BY id LIMIT 1",
                (credential_id,),
            ).fetchone()
            return {
                "id": int(row["id"]),
                "type": row["type"],
                "fingerprint": row["fingerprint"],
                "secret": self._decrypt(row["secret_encrypted"]),
                "paired": _load(self._decrypt(row["paired_encrypted"]), {}),
                "platform": str(platform_row["platform"] or "") if platform_row else "",
            }

        def finish(
            connection: sqlite3.Connection,
            job: dict[str, Any],
            status: str,
            evidence: dict[str, Any],
            latency_ms: int | None,
            version: str,
            verifier_id: str | None = None,
        ) -> None:
            now = _now()
            connection.execute(
                """
                INSERT INTO verification_results(
                    job_id,credential_id,verifier_id,verifier_version,status,evidence_json,latency_ms,created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job["id"],
                    job["credential_id"],
                    verifier_id or job["verifier_id"],
                    version,
                    status,
                    _dump(evidence),
                    latency_ms,
                    now,
                ),
            )
            connection.execute(
                "UPDATE verification_jobs SET status='finished',finished_at=? WHERE id=?",
                (now, job["id"]),
            )
            connection.execute(
                "UPDATE credentials SET verification_status=?,verified_at=? WHERE id=?",
                (status, now, job["credential_id"]),
            )

        while stats["processed"] < limit:
            job = self._execute(claim, writable=True)
            if job is None:
                break
            status = "error"
            evidence: dict[str, Any] = {"reason": "verification_failed_before_request"}
            latency_ms: int | None = None
            version = "1.0.0"
            result_verifier_id = str(job["verifier_id"])
            try:
                view = self._execute(
                    lambda connection: credential_view(connection, int(job["credential_id"]))
                )
                if view is None:
                    status = "error"
                    evidence = {"reason": "credential_not_found"}
                elif view["fingerprint"].lower() not in {
                    str(value).lower() for value in runtime.verification_allowed_fingerprints
                }:
                    status = "inconclusive"
                    evidence = {
                        "reason": "verification_fingerprint_not_authorized",
                        "request_sent": False,
                    }
                elif view["fingerprint"].lower() in controlled_fingerprints:
                    outcome = controlled_verification_outcome(view["type"])
                    status, evidence, latency_ms = (
                        outcome.status,
                        outcome.evidence,
                        outcome.latency_ms,
                    )
                    version = "controlled-fixture-1.0"
                else:
                    configured_endpoint = None
                    if job["verifier_id"].startswith(AUTHORIZED_ENDPOINT_VERIFIER_PREFIX):
                        endpoint_id = job["verifier_id"][len(AUTHORIZED_ENDPOINT_VERIFIER_PREFIX):]
                        configured_endpoint = authorized_endpoint_by_id(
                            endpoint_id,
                            authorized_endpoints,
                        )
                        result_verifier_id = job["verifier_id"]
                    elif job["verifier_id"] == "local-evidence":
                        configured_endpoint = authorized_endpoint_for_type(
                            view["type"],
                            view["platform"],
                            authorized_endpoints,
                        )
                        if configured_endpoint:
                            result_verifier_id = (
                                f"{AUTHORIZED_ENDPOINT_VERIFIER_PREFIX}"
                                f"{configured_endpoint['endpoint_id']}"
                            )
                    elif not job["verifier_id"].startswith("unsupported:"):
                        configured_endpoint = None
                    else:
                        configured_endpoint = authorized_endpoint_for_type(
                            view["type"],
                            view["platform"],
                            authorized_endpoints,
                        )
                        if configured_endpoint:
                            result_verifier_id = (
                                f"{AUTHORIZED_ENDPOINT_VERIFIER_PREFIX}"
                                f"{configured_endpoint['endpoint_id']}"
                            )
                    if (
                        job["verifier_id"].startswith(AUTHORIZED_ENDPOINT_VERIFIER_PREFIX)
                        and configured_endpoint is None
                    ):
                        status = "not_configured"
                        evidence = {
                            "verification_mode": "authorized_endpoint",
                            "authorization_endpoint_status": "not_configured",
                            "endpoint_id": job["verifier_id"][
                                len(AUTHORIZED_ENDPOINT_VERIFIER_PREFIX):
                            ],
                            "credential_type": view["type"],
                            "platform": view["platform"],
                            "authorization_scope": {
                                "configured": False,
                                "credential_type": view["type"],
                                "platform": view["platform"],
                            },
                            "reason": "authorized_endpoint_not_configured",
                            "request_sent": False,
                        }
                    elif job["verifier_id"].startswith("unsupported:") and configured_endpoint is None:
                        status = "unsupported"
                        evidence = {
                            "reason": "no_minimal_verifier",
                            "credential_type": view["type"],
                            "platform": view["platform"],
                            "request_sent": False,
                        }
                    elif job["verifier_id"] == "local-evidence" and configured_endpoint is None:
                        status = "not_configured"
                        evidence = {
                            "verification_mode": "local_evidence",
                            "authorization_endpoint_status": "not_configured",
                            "credential_type": view["type"],
                            "platform": view["platform"],
                            "authorization_scope": {
                                "configured": False,
                                "credential_type": view["type"],
                                "platform": view["platform"],
                            },
                            "verification_phase": "local_evidence_completed",
                            "reason": "authorized_endpoint_not_configured",
                            "request_sent": False,
                        }
                    elif not runtime.verification_network_enabled:
                        status = "inconclusive"
                        evidence = {
                            "reason": "verification_network_disabled",
                            "authorization_endpoint_status": (
                                "configured" if configured_endpoint else "not_configured"
                            ),
                            "request_sent": False,
                        }
                    else:
                        paired = dict(view["paired"])
                        paired["platform"] = view["platform"]
                        if view["type"] == "dockerhub_token" and runtime.dockerhub_username:
                            paired.setdefault("dockerhub_username", runtime.dockerhub_username)
                        outcome = verify_credential(
                            view["type"],
                            view["secret"] or "",
                            paired,
                            timeout_seconds=runtime.verification_timeout_seconds,
                            authorized_endpoint=configured_endpoint,
                        )
                        status, evidence, latency_ms = (
                            outcome.status,
                            outcome.evidence,
                            outcome.latency_ms,
                        )
                        if (
                            status == "invalid"
                            and evidence.get("reason") == "provider_rejected_credentials"
                            and view["type"] in {"github_pat", "gitee_token"}
                        ):
                            configured_secret = (
                                runtime.github_token
                                if view["type"] == "github_pat"
                                else runtime.gitee_token
                            )
                            if configured_secret and runtime.credential_hmac_key:
                                configured_fingerprint = credential_fingerprint(
                                    configured_secret,
                                    runtime.credential_hmac_key,
                                )
                                configured_match = hmac.compare_digest(
                                    str(view["fingerprint"] or ""),
                                    configured_fingerprint,
                                )
                                evidence["configured_token_match"] = configured_match
                                if not configured_match:
                                    evidence["reason"] = (
                                        "finding_credential_differs_from_configured_token"
                                    )
                                    evidence["server_reason"] = (
                                        "provider_rejected_credentials"
                                    )
                        version = (
                            "authorized-endpoint-1.0.0"
                            if configured_endpoint
                            else verifier_for_type(view["type"]) or "1.0.0"
                        )
            except Exception as exc:
                status = "error"
                evidence = {"reason": type(exc).__name__}
            self._execute(
                lambda connection: finish(
                    connection,
                    job,
                    status,
                    evidence,
                    latency_ms,
                    version,
                    result_verifier_id,
                ),
                writable=True,
            )
            scope = evidence.get("authorization_scope")
            safe_scope = {}
            if isinstance(scope, dict):
                for key in ("endpoint_id", "system_name", "platform", "credential_type", "allowed_host"):
                    if scope.get(key) not in (None, ""):
                        safe_scope[key] = str(scope[key])[:200]
            try:
                self.record_security_audit(
                    actor="system:verification",
                    role="system",
                    action="verification.execute",
                    resource_type="credential",
                    resource_id=str(job["credential_id"]),
                    decision=status,
                    reason=str(evidence.get("reason") or status)[:300],
                    metadata={
                        "verifier_id": result_verifier_id[:160],
                        "credential_type": str(evidence.get("credential_type") or "")[:120],
                        "platform": str(evidence.get("platform") or "")[:120],
                        "request_sent": bool(evidence.get("request_sent")),
                        "http_status": evidence.get("http_status"),
                        "authorization_endpoint_status": str(
                            evidence.get("authorization_endpoint_status") or ""
                        )[:40],
                        "authorization_scope": safe_scope,
                    },
                )
            except Exception:
                # The verification result is authoritative; audit persistence is
                # secondary and must not turn a completed job into a failure.
                pass
            stats["processed"] += 1
            stats[status if status in stats else "error"] += 1
        return stats

    def list_verification_jobs(self, status: str | None = None) -> list[dict[str, Any]]:
        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            if status:
                rows = connection.execute(
                    """
                    SELECT j.*,c.preview,c.type,c.type AS credential_type,
                           (SELECT o.platform FROM occurrences o
                            WHERE o.credential_id=c.id ORDER BY o.id LIMIT 1) AS platform
                    FROM verification_jobs j
                    JOIN credentials c ON c.id=j.credential_id
                    WHERE j.status=? ORDER BY j.id DESC LIMIT 200
                    """,
                    (status,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT j.*,c.preview,c.type,c.type AS credential_type,
                           (SELECT o.platform FROM occurrences o
                            WHERE o.credential_id=c.id ORDER BY o.id LIMIT 1) AS platform
                    FROM verification_jobs j
                    JOIN credentials c ON c.id=j.credential_id
                    ORDER BY j.id DESC LIMIT 200
                    """
                ).fetchall()
            return [self._row(row) or {} for row in rows]

        return self._execute(read)

    def list_verification_history(self, credential_id: int | None = None) -> list[dict[str, Any]]:
        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            if credential_id:
                rows = connection.execute(
                    """
                    SELECT vr.*,
                           CASE
                               WHEN EXISTS (
                                   SELECT 1
                                   FROM occurrences o
                                   WHERE o.credential_id=vr.credential_id
                                     AND json_extract(o.evidence_json, '$.source_kind')='真实检测结果'
                               ) THEN '真实检测结果'
                               ELSE COALESCE(
                                   (
                                       SELECT json_extract(o.evidence_json, '$.source_kind')
                                       FROM occurrences o
                                       WHERE o.credential_id=vr.credential_id
                                       ORDER BY o.id
                                       LIMIT 1
                                   ),
                                   '未知'
                               )
                           END AS source_kind
                    FROM verification_results vr
                    WHERE vr.credential_id=?
                    ORDER BY vr.id DESC LIMIT 200
                    """,
                    (credential_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT vr.*,
                           CASE
                               WHEN EXISTS (
                                   SELECT 1
                                   FROM occurrences o
                                   WHERE o.credential_id=vr.credential_id
                                     AND json_extract(o.evidence_json, '$.source_kind')='真实检测结果'
                               ) THEN '真实检测结果'
                               ELSE COALESCE(
                                   (
                                       SELECT json_extract(o.evidence_json, '$.source_kind')
                                       FROM occurrences o
                                       WHERE o.credential_id=vr.credential_id
                                       ORDER BY o.id
                                       LIMIT 1
                                   ),
                                   '未知'
                               )
                           END AS source_kind
                    FROM verification_results vr
                    ORDER BY vr.id DESC LIMIT 200
                    """
                ).fetchall()
            return [
                {**(self._row(row) or {}), "evidence": _load(row["evidence_json"], {})}
                for row in rows
            ]

        return self._execute(read)

    def _has_real_connector_evidence(self, collector_key: str) -> bool:
        """Return true only after a completed authorized connector run has evidence."""
        # scan_runs.task_id points to tasks, not sources. Resolve the relation
        # explicitly so status evidence cannot be inferred from a source alone.
        def read_correct(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                """
                SELECT 1
                FROM occurrences o
                JOIN scan_runs sr ON sr.id = o.run_id
                JOIN tasks t ON t.id = sr.task_id
                JOIN sources s ON s.id = t.source_id
                WHERE s.collector_key = ?
                  AND sr.status = 'completed'
                  AND json_extract(o.evidence_json, '$.source_kind') = '真实检测结果'
                LIMIT 1
                """,
                (collector_key,),
            ).fetchone()
            return row is not None

        return self._execute(read_correct)

    def _completed_test_evidence(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """Return the latest completed run evidence for connectors and rules."""
        def read(connection: sqlite3.Connection) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
            connector_rows = connection.execute(
                """
                SELECT s.collector_key,sr.id,sr.finished_at,
                       COALESCE(json_extract(sr.stats_json,'$.candidates'),0) AS test_count
                FROM scan_runs sr
                JOIN tasks t ON t.id=sr.task_id
                JOIN sources s ON s.id=t.source_id
                WHERE sr.status='completed'
                ORDER BY sr.id
                """
            ).fetchall()
            connectors: dict[str, dict[str, Any]] = {}
            for row in connector_rows:
                connectors[str(row["collector_key"])] = {
                    "tested_at": row["finished_at"],
                    "test_run_id": int(row["id"]),
                    "test_count": int(row["test_count"] or 0),
                }

            rule_rows = connection.execute(
                """
                SELECT c.rule_id,sr.id,sr.finished_at,COUNT(o.id) AS test_count
                FROM occurrences o
                JOIN credentials c ON c.id=o.credential_id
                JOIN scan_runs sr ON sr.id=o.run_id
                WHERE sr.status='completed' AND c.rule_id <> ''
                GROUP BY c.rule_id,sr.id,sr.finished_at
                ORDER BY sr.id
                """
            ).fetchall()
            rules: dict[str, dict[str, Any]] = {}
            for row in rule_rows:
                rules[str(row["rule_id"])] = {
                    "tested_at": row["finished_at"],
                    "test_run_id": int(row["id"]),
                    "test_count": int(row["test_count"] or 0),
                }
            return connectors, rules

        return self._execute(read)

    def sync_rules_and_capabilities(self) -> None:
        now = _now()
        registry = get_registry()
        configured_verification_endpoints = effective_settings(
            settings
        ).authorized_verification_endpoints
        github_real_tested = self._has_real_connector_evidence("github")
        gitee_real_tested = self._has_real_connector_evidence("gitee")
        connector_evidence, rule_evidence = self._completed_test_evidence()
        # Keep rule readiness independent from live credential validity. The
        # registry's positive/negative samples are executable evidence that
        # the detector works; they do not claim any provider token is active.
        registry_test_problems = set(registry.validate_tests())
        registry_rule_tested = {
            rule.id
            for rule in registry.rules
            if rule.tests.get("positives")
            and rule.tests.get("negatives")
            and not any(problem.startswith(f"{rule.id}:") for problem in registry_test_problems)
        }
        stackoverflow_real_tested = bool(connector_evidence.get("stackoverflow"))
        weibo_authorized_real_tested = bool(connector_evidence.get("weibo_authorized_api"))
        rules = [
            {
                "id": rule.id,
                "title": rule.title,
                "type": rule.type,
                "vendor": rule.vendor,
                "version": rule.version,
                "license": rule.license,
                # The YAML verifier is an explicit provider probe. Types
                # without a safe remote probe still use the bounded local
                # evidence verifier so every registered entity has a clear
                # verification path and never falls through to "unsupported".
                "verifier_id": verifier_for_type(rule.type),
            }
            for rule in registry.rules
        ]
        rules.extend(
            {
                **rule,
                "verifier_id": verifier_for_type(str(rule["type"])),
            }
            for rule in controlled_rule_specs()
        )
        capabilities = [
            (
                "collector:local_dir", "collector", "local_dir", "本地目录",
                "已实测可运行" if "local_dir" in connector_evidence else "已实现未实测",
                {
                    "offline_tested": True,
                    "real_platform_tested": False,
                    "test_evidence": connector_evidence.get("local_dir"),
                },
            ),
            (
                "collector:controlled_fixtures", "collector", "controlled_fixtures",
                "受控测试夹具",
                "已实测可运行" if "controlled_fixtures" in connector_evidence else "已实现未实测",
                {"offline_tested": True, "test_evidence": connector_evidence.get("controlled_fixtures")},
            ),
            (
                "collector:authorized_jsonl", "collector", "authorized_jsonl",
                "离线授权 JSONL",
                "已实测可运行" if "authorized_jsonl" in connector_evidence else "已实现未实测",
                {"offline_tested": True, "test_evidence": connector_evidence.get("authorized_jsonl")},
            ),
            ("collector:github", "collector", "github", "GitHub 授权 API", "已实测可运行" if github_real_tested else "已实现未实测", {
                "network": "仅在设置页开启且仓库白名单非空时运行",
                "code_status": "已实现",
                "offline_tested": True,
                "real_platform_tested": github_real_tested,
                "evidence_ref": "scan_runs.id=8；授权仓库 samabc123/ccs-demo" if github_real_tested else "当前无真实平台运行证据",
            }),
            ("collector:gitee", "collector", "gitee", "Gitee 授权 API", "已实测可运行" if gitee_real_tested else "已实现未实测", {
                "network": "仅在设置页配置 Token、白名单且开启联网时运行",
                "code_status": "已实现",
                "offline_tested": True,
                "real_platform_tested": gitee_real_tested,
                "evidence_ref": "SQLite：Gitee completed 批次关联的真实检测记录" if gitee_real_tested else "发布包运行时检查；无真实平台运行证据",
            }),
            ("collector:mediawiki", "collector", "mediawiki", "MediaWiki 公开 API", "已实现未实测", {
                "network": "仅显式开启联网；HTTPS 公网 api.php；支持 recentchanges checkpoint",
                "code_status": "已实现",
                "offline_tested": True,
                "real_platform_tested": False,
                "evidence_ref": "发布包运行时检查；无真实平台运行证据",
            }),
            ("collector:generic_web", "collector", "generic_web", "RSS/Atom/Sitemap/URL", "已实现未实测", {
                "network": "仅显式开启联网；HTTPS 公网地址；有大小与条目预算",
                "code_status": "已实现",
                "offline_tested": True,
                "real_platform_tested": False,
                "evidence_ref": "发布包运行时检查；无真实平台运行证据",
            }),
            ("collector:stackoverflow", "collector", "stackoverflow", "Stack Overflow 公开 API",
             "已实测可运行" if stackoverflow_real_tested else "已实现未实测", {
                "network": "仅显式开启联网；搜索问题并读取问题/答案正文；遵守响应限流语义",
                "code_status": "已实现",
                "offline_tested": True,
                "real_platform_tested": stackoverflow_real_tested,
                "test_evidence": connector_evidence.get("stackoverflow"),
                "evidence_ref": (
                    "SQLite：Stack Overflow completed 公网运行"
                    if stackoverflow_real_tested
                    else "发布包运行时检查；无真实平台运行证据"
                ),
            }),
            ("collector:weibo_authorized_api", "collector", "weibo_authorized_api",
             "微博（官方 OAuth API）",
             "已实测可运行" if weibo_authorized_real_tested else "已实现未实测", {
                "network": "仅显式开启联网；Token 只从加密运行设置读取；来源配置只允许 UID/微博 ID",
                "code_status": "已实现",
                "offline_tested": True,
                "real_platform_tested": weibo_authorized_real_tested,
                "test_evidence": connector_evidence.get("weibo_authorized_api"),
                "evidence_ref": (
                    "SQLite：微博官方 OAuth API completed 公网运行"
                    if weibo_authorized_real_tested
                    else "发布包运行时检查；无微博 OAuth API 真实平台运行证据"
                ),
            }),
            ("collector:oci_registry", "collector", "oci_registry", "OCI Registry 镜像", "已实现未实测", {
                "network": "仅显式开启联网；拉取 manifest/layer；受解压预算约束",
                "code_status": "已实现",
                "offline_tested": True,
                "real_platform_tested": False,
                "evidence_ref": "发布包运行时检查；无真实平台运行证据",
            }),
            ("collector:npm_registry", "collector", "npm_registry", "npm Registry 包仓库", "已实现未实测", {
                "network": "仅显式开启联网；只读 metadata/tarball；不执行包代码",
                "code_status": "已实现",
                "offline_tested": True,
                "real_platform_tested": False,
                "evidence_ref": "发布包运行时检查；无真实平台运行证据",
            }),
            ("collector:pypi_registry", "collector", "pypi_registry", "PyPI 包仓库", "已实现未实测", {
                "network": "仅显式开启联网；只读 metadata/distribution；不执行包代码",
                "code_status": "已实现",
                "offline_tested": True,
                "real_platform_tested": False,
                "evidence_ref": "发布包运行时检查；无真实平台运行证据",
            }),
            ("collector:huggingface_repo", "collector", "huggingface_repo", "Hugging Face 公开仓库", "已实现未实测", {
                "network": "仅显式开启联网；只读公开仓库文本文件；有文件与字节预算",
                "code_status": "已实现",
                "offline_tested": True,
                "real_platform_tested": False,
                "evidence_ref": "发布包运行时检查；无真实平台运行证据",
            }),
            ("collector:crates_registry", "collector", "crates_registry", "crates.io 包仓库", "已实现未实测", {
                "network": "仅显式开启联网；只读 metadata/source archive；不执行构建脚本",
                "code_status": "已实现",
                "offline_tested": True,
                "real_platform_tested": False,
                "evidence_ref": "发布包运行时检查；无真实平台运行证据",
            }),
            ("collector:nuget_registry", "collector", "nuget_registry", "NuGet 包仓库", "已实现未实测", {
                "network": "仅显式开启联网；只读 nupkg；不执行包代码",
                "code_status": "已实现",
                "offline_tested": True,
                "real_platform_tested": False,
                "evidence_ref": "发布包运行时检查；无真实平台运行证据",
            }),
            ("collector:rubygems_registry", "collector", "rubygems_registry", "RubyGems 包仓库", "已实现未实测", {
                "network": "仅显式开启联网；只读 gem/data.tar.gz；不执行 Ruby 代码",
                "code_status": "已实现",
                "offline_tested": True,
                "real_platform_tested": False,
                "evidence_ref": "发布包运行时检查；无真实平台运行证据",
            }),
            ("collector:apk", "collector", "apk", "Android APK 静态扫描", "已实现未实测", {
                "network": "本地合法取得文件可离线运行；URL 入口仍需显式联网",
                "code_status": "已实现",
                "offline_tested": True,
                "real_platform_tested": False,
                "evidence_ref": "发布包运行时检查；无真实平台运行证据",
            }),
            ("collector:wxapkg", "collector", "wxapkg", "未加密 wxapkg", "已实现未实测", {
                "network": "本地合法取得未加密包可离线运行；不解密加密包",
                "code_status": "已实现",
                "offline_tested": True,
                "real_platform_tested": False,
                "evidence_ref": "发布包运行时检查；无真实平台运行证据",
            }),
            ("verifier:offline-unavailable", "verifier", "offline-unavailable", "离线验证队列", "已实现", {
                "reason": "默认关闭联网；未授权时只记录无法判断",
            }),
        ]
        for spec in verifier_specs(configured_verification_endpoints):
            capabilities.append((
                f"verifier:{spec['id']}",
                "verifier",
                spec["id"],
                spec["title"],
                "implemented",
                {
                    "version": spec["version"],
                    "supported_types": spec["supported_types"],
                    "endpoint": spec["endpoint"],
                    "network": "需设置页开启联网并加入 HMAC 指纹白名单",
                },
            ))

        support_rows: list[dict[str, Any]] = []
        connector_keys = {
            "authorized-local-directory": "local_dir",
            "authorized-jsonl-export": "authorized_jsonl",
            "controlled-fixtures": "controlled_fixtures",
            "github-authorized-rest": "github",
            "gitee-authorized-rest": "gitee",
            "mediawiki-public-api": "mediawiki",
            "generic-web-rss": "generic_web",
            "generic-web-atom": "generic_web",
            "generic-web-sitemap": "generic_web",
            "generic-web-url": "generic_web",
            "stackoverflow-public-api": "stackoverflow",
            "weibo-authorized-api": "weibo_authorized_api",
            "oci-registry": "oci_registry",
            "npm-registry": "npm_registry",
            "pypi-registry": "pypi_registry",
            "huggingface-public-repository": "huggingface_repo",
            "crates-public-registry": "crates_registry",
            "nuget-public-registry": "nuget_registry",
            "rubygems-public-registry": "rubygems_registry",
            "android-apk": "apk",
            "wxapkg-unencrypted": "wxapkg",
        }
        for spec in list_connector_specs():
            mode = str(spec.get("mode") or "")
            local = mode in {"offline", "offline-authorized", "authorized-artifact"}
            connector_key = connector_keys.get(str(spec.get("name") or ""), str(spec.get("name") or ""))
            run_evidence = connector_evidence.get(connector_key)
            platform = str(spec.get("platform") or "")
            is_real_tested = bool(
                (platform == "GitHub（授权 API）" and github_real_tested)
                or (platform == "Gitee（授权 API）" and gitee_real_tested)
                or (mode in {"online-public", "online-authorized"} and run_evidence)
            )
            status = "已实测可运行" if run_evidence or is_real_tested else "已实现未实测"
            platform = str(spec.get("platform") or "")
            channel = str(spec.get("channel") or "")
            support_rows.append({
                "item_kind": "channel",
                "item_key": f"channel:{channel}:{platform}",
                "channel_type": channel,
                "platform": platform,
                "public_carrier": (
                    "授权仓库文件/提交"
                    if "Git" in platform or "Gitee" in platform
                    else "本地合法载体"
                    if local
                    else "公开 API、网页或镜像载体"
                ),
                "permission_condition": (
                    "本地合法取得或团队授权；不绕过访问控制"
                    if local
                    else "遵守平台条款；公开接口或明确授权范围"
                ),
                "compliance_constraint": "受控频率、预算、robots/服务条款；不登录第三方业务、不撤销凭据",
                "collection_method": mode,
                "parse_method": str(spec.get("documentation") or ""),
                "polling_method": "手动/持续任务；支持断点与失败审计",
                "status": status,
                "evidence_ref": (
                    f"SQLite：completed 批次 {run_evidence['test_run_id']}，候选={run_evidence['test_count']}"
                    if run_evidence
                    else (
                        "app/local_scan.py；app/content_detection.py"
                        if connector_key == "local_dir"
                        else "app/controlled_detector.py；fixtures/controlled"
                        if connector_key == "controlled_fixtures"
                        else "app/connectors.py"
                        if connector_key == "authorized_jsonl"
                        else "app/connectors.py；app/public_connectors.py"
                    )
                    if local
                    else "app/connectors.py；app/public_connectors.py"
                ),
                "real_tested": is_real_tested,
                "tested_at": run_evidence.get("tested_at") if run_evidence else None,
                "test_run_id": run_evidence.get("test_run_id") if run_evidence else None,
                "test_count": run_evidence.get("test_count", 0) if run_evidence else 0,
            })
        for rule in rules:
            registry_rule = registry.get(rule["id"])
            has_offline_rule_evidence = bool(
                registry_rule
                and registry_rule.tests.get("positives")
                and registry_rule.tests.get("negatives")
            )
            controlled_rule = rule["id"] in {
                "controlled-ak-sk",
                "controlled-api-key",
                "controlled-api-token",
                "controlled-oauth-client-secret",
                "controlled-database-password",
                "controlled-ssh-private-key",
                "controlled-os-password",
                "controlled-database-url",
            }
            if controlled_rule:
                has_offline_rule_evidence = True
            evidence_ref = (
                "app/controlled_detector.py；fixtures/controlled；受控夹具边界已覆盖"
                if controlled_rule
                else "app/detection/rules；规则内置正反样例"
                if has_offline_rule_evidence
                else "app/controlled_detector.py；fixtures/controlled（待补充逐规则样例）"
            )
            actual_rule_test = rule_evidence.get(rule["id"])
            offline_rule_tested = (
                rule["id"] in registry_rule_tested
                or controlled_rule
            )
            support_rows.append({
                "item_kind": "credential",
                "item_key": f"credential:{rule['id']}",
                "channel_type": "",
                "platform": "",
                "public_carrier": "",
                "permission_condition": "仅保留脱敏候选；有效性验证需自有或明确授权",
                "compliance_constraint": "完整秘密不进普通 API、日志或报告；HMAC 指纹使用密钥",
                "collection_method": "规则检测",
                "parse_method": rule["title"],
                "polling_method": "",
                "status": (
                    "已实测可运行"
                    if actual_rule_test
                    else "已实测可运行"
                    if offline_rule_tested
                    else "已实现未实测"
                ),
                "evidence_ref": (
                    f"{evidence_ref}；最近实测批次={actual_rule_test['test_run_id']}"
                    if actual_rule_test
                    else f"{evidence_ref}；正反样例覆盖通过"
                    if offline_rule_tested and has_offline_rule_evidence
                    else evidence_ref
                ),
                "real_tested": False,
                "tested_at": actual_rule_test.get("tested_at") if actual_rule_test else None,
                "test_run_id": actual_rule_test.get("test_run_id") if actual_rule_test else None,
                "test_count": actual_rule_test.get("test_count", 0) if actual_rule_test else 0,
                "credential_type": rule["type"],
                "rule_id": rule["id"],
                "vendor": rule["vendor"],
                "format_variant": rule["title"],
                "paired_required": bool(
                    getattr(registry.get(rule["id"]), "pairing", None)
                    if registry.get(rule["id"])
                    else False
                ),
                "supports_verification": bool(rule["verifier_id"]),
            })

        def write(connection: sqlite3.Connection) -> None:
            # Remove retired coverage channels while preserving their credential
            # rules and historical detection data.
            connection.execute(
                """
                DELETE FROM capabilities
                WHERE id IN (
                    'collector:gitlab_archive',
                    'collector:bitbucket_archive',
                    'collector:codeberg',
                    'collector:weibo_public_page'
                )
                """
            )
            connection.execute(
                """
                DELETE FROM support_matrix
                WHERE item_kind='channel'
                  AND platform IN (
                    'GitLab（公开归档）',
                    'Bitbucket（公开归档）',
                    'Codeberg',
                    'RSS/Atom/Sitemap/URL',
                    '微博公开页面'
                  )
                """
            )
            connection.execute(
                """
                DELETE FROM extensibility_experiments
                WHERE connector_key IN (
                    'channel:代码托管:GitLab（公开归档）',
                    'channel:代码托管:Bitbucket（公开归档）',
                    'channel:知识分享:RSS/Atom/Sitemap/URL',
                    'channel:微博:微博公开页面'
                )
                """
            )
            for rule in rules:
                connection.execute(
                    """
                    INSERT INTO rule_records(id,title,type,vendor,version,license,enabled,builtin,verifier_id,file_sha,updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, '', ?)
                    ON CONFLICT(id) DO UPDATE SET
                      title=excluded.title,type=excluded.type,vendor=excluded.vendor,
                      version=excluded.version,license=excluded.license,
                      verifier_id=excluded.verifier_id,updated_at=excluded.updated_at
                    """,
                    (
                        rule["id"], rule["title"], rule["type"], rule["vendor"],
                        rule["version"], rule["license"], rule["verifier_id"], now,
                    ),
                )
            for capability in capabilities:
                connection.execute(
                    """
                    INSERT INTO capabilities(id,kind,key,title,status,meta_json,updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      title=excluded.title,status=excluded.status,meta_json=excluded.meta_json,
                      updated_at=excluded.updated_at
                    """,
                    (*capability[:5], _dump(capability[5]), now),
                )
            for item in support_rows:
                connection.execute(
                    """
                    INSERT INTO support_matrix(
                        item_kind,item_key,channel_type,platform,public_carrier,
                        permission_condition,compliance_constraint,collection_method,
                        parse_method,polling_method,status,evidence_ref,real_tested,
                        credential_type,rule_id,vendor,format_variant,paired_required,
                        supports_verification,tested_at,test_run_id,test_count,updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(item_key) DO UPDATE SET
                        item_kind=excluded.item_kind,channel_type=excluded.channel_type,
                        platform=excluded.platform,public_carrier=excluded.public_carrier,
                        permission_condition=excluded.permission_condition,
                        compliance_constraint=excluded.compliance_constraint,
                        collection_method=excluded.collection_method,
                        parse_method=excluded.parse_method,
                        polling_method=excluded.polling_method,status=excluded.status,
                        evidence_ref=excluded.evidence_ref,real_tested=excluded.real_tested,
                        credential_type=excluded.credential_type,rule_id=excluded.rule_id,
                        vendor=excluded.vendor,format_variant=excluded.format_variant,
                        paired_required=excluded.paired_required,
                        supports_verification=excluded.supports_verification,
                        tested_at=excluded.tested_at,test_run_id=excluded.test_run_id,
                        test_count=excluded.test_count,
                        updated_at=excluded.updated_at
                    """,
                    (
                        item["item_kind"], item["item_key"], item["channel_type"], item["platform"],
                        item["public_carrier"], item["permission_condition"],
                        item["compliance_constraint"], item["collection_method"],
                        item["parse_method"], item["polling_method"], item["status"],
                        item["evidence_ref"], int(item["real_tested"]),
                        item.get("credential_type", ""), item.get("rule_id", ""),
                        item.get("vendor", ""), item.get("format_variant", ""),
                        int(item.get("paired_required", False)),
                        int(item.get("supports_verification", False)),
                        item.get("tested_at"), item.get("test_run_id"),
                        int(item.get("test_count", 0)), now,
                    ),
                )
                if item["item_kind"] == "channel":
                    experiment_key = f"connector:{item['item_key'].split(':', 1)[1]}"
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO extensibility_experiments(
                            experiment_key,connector_key,title,baseline_connector,
                            execution_status,evidence_ref,notes,created_at,updated_at
                        ) VALUES (?, ?, ?, ?, 'not_run', ?, ?, ?, ?)
                        """,
                        (
                            experiment_key,
                            item["item_key"],
                            f"{item['channel_type']} / {item['platform']} 扩展性实验",
                            "统一 ConnectorSpec + BackendStore",
                            item["evidence_ref"],
                            "团队设计记录：工时、代码量、异常恢复和 checkpoint 需在专项实验后填写；当前不冒充实测。",
                            now,
                            now,
                        ),
                    )

        self._execute(write, writable=True)

    def list_rules(self) -> list[dict[str, Any]]:
        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute("SELECT * FROM rule_records ORDER BY id").fetchall()
            output = []
            for row in rows:
                item = {
                    **(self._row(row) or {}),
                    "enabled": bool(row["enabled"]),
                    "builtin": bool(row["builtin"]),
                    "implementation_status": "已实现",
                }
                support = connection.execute(
                    """
                    SELECT status,evidence_ref,real_tested
                           ,tested_at,test_run_id,test_count
                    FROM support_matrix
                    WHERE item_kind='credential' AND rule_id=?
                    LIMIT 1
                    """,
                    (row["id"],),
                ).fetchone()
                item["offline_test_status"] = (
                    "已实测可运行"
                    if support is not None and support["tested_at"]
                    else "已实测可运行"
                    if support is not None and support["status"] == "已实测可运行"
                    else "未验证"
                )
                item["real_platform_test_status"] = "不适用"
                item["status"] = support["status"] if support else "未验证"
                item["evidence_ref"] = support["evidence_ref"] if support else "规则注册表；暂无专项证据"
                item["tested_at"] = support["tested_at"] if support else None
                item["test_run_id"] = support["test_run_id"] if support else None
                item["test_count"] = int(support["test_count"] or 0) if support else 0
                output.append(item)
            return output

        return self._execute(read)

    def set_rule_enabled(self, rule_id: str, enabled: bool) -> dict[str, Any] | None:
        def write(connection: sqlite3.Connection) -> dict[str, Any] | None:
            cursor = connection.execute(
                "UPDATE rule_records SET enabled=?,updated_at=? WHERE id=?",
                (int(bool(enabled)), _now(), rule_id),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute("SELECT * FROM rule_records WHERE id=?", (rule_id,)).fetchone()
            return {**(self._row(row) or {}), "enabled": bool(row["enabled"]), "builtin": bool(row["builtin"])}

        return self._execute(write, writable=True)

    def list_capabilities(self) -> list[dict[str, Any]]:
        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute("SELECT * FROM capabilities ORDER BY kind,id").fetchall()
            return [
                {
                    **(self._row(row) or {}),
                    "meta": _load(row["meta_json"], {}),
                }
                for row in rows
            ]

        return self._execute(read)

    def list_support_matrix(self, item_kind: str | None = None) -> list[dict[str, Any]]:
        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            if item_kind:
                rows = connection.execute(
                    "SELECT * FROM support_matrix WHERE item_kind=? ORDER BY id",
                    (item_kind,),
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM support_matrix ORDER BY item_kind,id").fetchall()
            return [
                {
                    **(self._row(row) or {}),
                    "real_tested": bool(row["real_tested"]),
                    "paired_required": bool(row["paired_required"]),
                    "supports_verification": bool(row["supports_verification"]),
                }
                for row in rows
            ]

        return self._execute(read)

    def list_extensibility_experiments(self) -> list[dict[str, Any]]:
        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute(
                "SELECT * FROM extensibility_experiments ORDER BY connector_key, id"
            ).fetchall()
            items = []
            for row in rows:
                item = {
                    **(self._row(row) or {}),
                    "offline_tested": bool(row["offline_tested"]),
                    "checkpoint_verified": bool(row["checkpoint_verified"]),
                    "failure_recovery_verified": bool(row["failure_recovery_verified"]),
                }
                notes = str(row["notes"] or "")
                item["runtime_check_status"] = (
                    "已通过"
                    if "发布包实现模块和连接器入口加载通过" in notes
                    else "未执行"
                )
                raw_status = str(row["execution_status"] or "not_run")
                if item["runtime_check_status"] == "已通过":
                    item["run_status"] = "passed" if raw_status == "passed" else "executed"
                elif raw_status == "failed":
                    item["run_status"] = "failed"
                else:
                    item["run_status"] = "not_run"
                items.append(item)
            return items

        return self._execute(read)

    def upsert_extensibility_experiment(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = str(payload.get("experiment_key") or "").strip()
        connector_key = str(payload.get("connector_key") or "").strip()
        title = str(payload.get("title") or "").strip()
        if not key or len(key) > 120 or not connector_key or not title:
            raise BackendValidationError("experiment_identity_required")
        status = str(payload.get("execution_status") or "planned")
        if status not in {"planned", "running", "passed", "failed", "not_run"}:
            raise BackendValidationError("experiment_status_invalid")
        def optional_int(name: str, maximum: int) -> int | None:
            value = payload.get(name)
            if value in (None, ""):
                return None
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
                raise BackendValidationError(f"{name}_invalid")
            return value
        def optional_float(name: str, maximum: float) -> float | None:
            value = payload.get(name)
            if value in (None, ""):
                return None
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value > maximum:
                raise BackendValidationError(f"{name}_invalid")
            return float(value)
        now = _now()
        values = (
            key,
            connector_key,
            title[:200],
            str(payload.get("baseline_connector") or "")[:120],
            optional_int("code_lines", 2_000_000),
            optional_int("config_fields", 10_000),
            optional_float("development_hours", 100_000),
            optional_int("test_duration_ms", 86_400_000),
            status,
            int(bool(payload.get("offline_tested"))),
            int(bool(payload.get("checkpoint_verified"))),
            int(bool(payload.get("failure_recovery_verified"))),
            str(payload.get("evidence_ref") or "")[:500],
            str(payload.get("experiment_date") or "")[:40] or None,
            str(payload.get("notes") or "")[:1000],
            now,
            now,
        )

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            connection.execute(
                """
                INSERT INTO extensibility_experiments(
                    experiment_key,connector_key,title,baseline_connector,code_lines,
                    config_fields,development_hours,test_duration_ms,execution_status,
                    offline_tested,checkpoint_verified,failure_recovery_verified,
                    evidence_ref,experiment_date,notes,created_at,updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(experiment_key) DO UPDATE SET
                    connector_key=excluded.connector_key,title=excluded.title,
                    baseline_connector=excluded.baseline_connector,code_lines=excluded.code_lines,
                    config_fields=excluded.config_fields,development_hours=excluded.development_hours,
                    test_duration_ms=excluded.test_duration_ms,execution_status=excluded.execution_status,
                    offline_tested=excluded.offline_tested,checkpoint_verified=excluded.checkpoint_verified,
                    failure_recovery_verified=excluded.failure_recovery_verified,
                    evidence_ref=excluded.evidence_ref,experiment_date=excluded.experiment_date,
                    notes=excluded.notes,updated_at=excluded.updated_at
                """,
                values,
            )
            row = connection.execute(
                "SELECT * FROM extensibility_experiments WHERE experiment_key=?", (key,)
            ).fetchone()
            return {
                **(self._row(row) or {}),
                "offline_tested": bool(row["offline_tested"]),
                "checkpoint_verified": bool(row["checkpoint_verified"]),
                "failure_recovery_verified": bool(row["failure_recovery_verified"]),
            }

        return self._execute(write, writable=True)

    def record_security_audit(
        self,
        *,
        actor: str,
        role: str,
        action: str,
        resource_type: str,
        resource_id: str = "",
        decision: str = "allowed",
        reason: str = "",
        source_ip: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now()
        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            cursor = connection.execute(
                """
                INSERT INTO security_audit(
                    actor,role,action,resource_type,resource_id,decision,reason,
                    source_ip,metadata_json,created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(actor)[:120],
                    str(role)[:40],
                    str(action)[:120],
                    str(resource_type)[:80],
                    str(resource_id)[:160],
                    str(decision)[:40],
                    str(reason)[:300],
                    str(source_ip)[:80],
                    _dump(metadata or {}),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM security_audit WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
            return {**(self._row(row) or {}), "metadata": _load(row["metadata_json"], {})}
        return self._execute(write, writable=True)

    def list_security_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(1000, int(limit)))
        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute(
                "SELECT * FROM security_audit ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [
                {**(self._row(row) or {}), "metadata": _load(row["metadata_json"], {})}
                for row in rows
            ]
        return self._execute(read)

    @staticmethod
    def _event_record_from_row(row: sqlite3.Row) -> dict[str, Any]:
        evidence = _load(row["evidence_json"], {})
        source_kind = str(evidence.get("source_kind") or "未知")
        disposition = str(evidence.get("disposition") or "待确认")
        source_locator = row["origin_url"] or row["path"] or None
        provenance = _row_time_provenance(row)
        public_fields = _public_time_fields(provenance)
        published = provenance.get("published_at") if isinstance(provenance.get("published_at"), dict) else {}
        source_updated = provenance.get("source_updated_at") if isinstance(provenance.get("source_updated_at"), dict) else {}
        discovery = _discovery_time_chain(row)
        return {
            "event_id": str(row["source_event_id"] or f"OCC-{row['occurrence_id']}"),
            "source_kind": source_kind,
            "channel": str(row["category"] or ""),
            "platform": str(row["platform"] or ""),
            "credential_type": str(row["credential_type"] or ""),
            "redacted_value": str(row["context_masked"] or ""),
            "evidence_level": str(evidence.get("evidence_level") or "L0"),
            "risk_level": str(evidence.get("risk_level") or "unknown"),
            "disposition": disposition,
            "first_observed_at": row["first_seen_at"],
            "published_at": published.get("value") or (row["published_at"] if "published_at" in row.keys() else None),
            "published_source": published.get("source") or (row["published_source"] if "published_source" in row.keys() else ""),
            "published_confidence": published.get("confidence") or (row["published_confidence"] if "published_confidence" in row.keys() else "none"),
            **public_fields,
            "fetched_at": row["fetched_at"] if "fetched_at" in row.keys() else None,
            "fetched_at_source": (
                str(row["fetched_at_source"] or "unknown")
                if "fetched_at_source" in row.keys()
                else "unknown"
            ),
            "source_updated_at": source_updated.get("value") or (row["source_updated_at"] if "source_updated_at" in row.keys() else None),
            "source_updated_source": source_updated.get("source") or "",
            "source_updated_confidence": source_updated.get("confidence") or "none",
            "source_updated_meaning": source_updated.get("meaning") or "",
            "source_updated_evidence": source_updated.get("evidence") or "",
            "time_provenance": provenance,
            "source_locator": source_locator,
            "detected_at": row["detected_at"],
            "first_discovered_at": discovery["first_discovered_at"],
            "first_discovery_time_status": discovery["first_discovery_time_status"],
            "first_discovery_unknown_reason": discovery["first_discovery_unknown_reason"],
            "last_discovered_at": row["last_discovered_at"] if "last_discovered_at" in row.keys() else discovery["first_discovered_at"],
            "discovery_latency_seconds": discovery["discovery_latency_seconds"],
            "discovery_latency_status": discovery["discovery_latency_status"],
            "discovery_latency_reason": discovery["discovery_latency_reason"],
            "discovery_latency_calculation_type": discovery["discovery_latency_calculation_type"],
            "discovery_latency_basis": discovery["discovery_latency_basis"],
            "discovery_latency_end_at": discovery["discovery_latency_end_at"],
            "detection_method": evidence.get("detection_method"),
            "credential_fingerprint": row["fingerprint"],
            "fingerprint_key_version": evidence.get("fingerprint_key_version"),
        }

    def list_event_records(
        self,
        *,
        channel: str = "",
        credential_type: str = "",
        risk_level: str = "",
        disposition: str = "",
        source_kind: str = "",
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "event_id",
        sort_order: str = "asc",
    ) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = max(1, min(100, int(page_size)))
        allowed_sort = {"event_id", "first_observed_at", "risk_level", "credential_type"}
        if sort_by not in allowed_sort or sort_order not in {"asc", "desc"}:
            raise BackendValidationError("event_sort_invalid")

        def read(connection: sqlite3.Connection) -> dict[str, Any]:
            rows = connection.execute(
                """
                SELECT o.id AS occurrence_id,o.source_event_id,o.category,o.platform,
                       o.origin_url,o.path,o.context_masked,o.evidence_json,
                       o.detected_at,o.first_discovered_at,o.last_discovered_at,
                       o.first_seen_at,c.type AS credential_type,
                       c.fingerprint,sc.published_at,sc.published_source,sc.published_confidence,
                       sc.source_updated_at,sc.fetched_at,sc.fetched_at_source,
                       o.time_provenance_json AS occurrence_time_provenance_json,
                       sc.time_provenance_json AS content_time_provenance_json
                FROM occurrences o
                JOIN credentials c ON c.id=o.credential_id
                LEFT JOIN source_contents sc ON sc.id=o.content_id
                ORDER BY o.id
                """
            ).fetchall()
            items = []
            seen_ids: set[str] = set()
            for row in rows:
                item = self._event_record_from_row(row)
                event_id = item["event_id"]
                if event_id in seen_ids:
                    item["event_id"] = f"{event_id}#occurrence-{row['occurrence_id']}"
                seen_ids.add(item["event_id"])
                items.append(item)
            filters = {
                "channel": channel.strip(),
                "credential_type": credential_type.strip(),
                "risk_level": risk_level.strip(),
                "disposition": disposition.strip(),
                "source_kind": source_kind.strip(),
            }
            items = [
                item for item in items
                if (not filters["channel"] or item["channel"] == filters["channel"])
                and (not filters["credential_type"] or item["credential_type"] == filters["credential_type"])
                and (not filters["risk_level"] or item["risk_level"] == filters["risk_level"])
                and (not filters["disposition"] or item["disposition"] == filters["disposition"])
                and (
                    not filters["source_kind"]
                    or filters["source_kind"] in {"全部来源", "all", "*"}
                    or item["source_kind"] == filters["source_kind"]
                )
            ]
            risk_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unknown": 4}
            if sort_by == "risk_level":
                key = lambda item: risk_order.get(item.get("risk_level"), 99)
            else:
                key = lambda item: item.get(sort_by) or ""
            # Keep real connector findings ahead of controlled/offline records
            # after the requested field ordering, before pagination is applied.
            items.sort(key=key, reverse=sort_order == "desc")
            items.sort(key=lambda item: 0 if item.get("source_kind") == "真实检测结果" else 1)
            total = len(items)
            total_pages = (total + page_size - 1) // page_size if total else 0
            current_page = min(page, total_pages) if total_pages else 1
            start = (current_page - 1) * page_size
            selected = items[start:start + page_size]
            all_filters = {
                "channel": sorted({item["channel"] for item in items}),
                "credential_type": sorted({item["credential_type"] for item in items}),
                "risk_level": ["critical", "high", "medium", "low", "unknown"],
                "disposition": sorted({item["disposition"] for item in items}),
                "source_kind": sorted({item["source_kind"] for item in items}),
            }
            labels = {
                "channel": "渠道类型",
                "credential_type": "凭据类型",
                "risk_level": "风险档",
                "disposition": "处置状态",
                "source_kind": "数据来源",
            }
            defaults = {
                "channel": "全部渠道",
                "credential_type": "全部类型",
                "risk_level": "全部风险",
                "disposition": "全部状态",
                "source_kind": "全部来源",
            }
            return {
                "schema_version": "1.0",
                "page_title": "事件列表",
                "source": "真实数据：规范化后端 SQLite occurrences",
                "implementation_status": "部分实现",
                "error": None,
                "filters": [
                    {"key": key_name, "label": labels[key_name], "default": defaults[key_name], "options": values}
                    for key_name, values in all_filters.items()
                ],
                "summary": {
                    "candidate_count": total,
                    "classified_count": sum(item["evidence_level"] in {"L1", "L2", "L3", "L4"} for item in items),
                    "public_exposure_confirmed_count": sum(
                        item["disposition"] == "已确认公开泄露" for item in items
                    ),
                    "unique_credential_count": len({
                        item["credential_fingerprint"]
                        for item in items
                        if item.get("credential_fingerprint")
                    }),
                    "event_count": total,
                },
                "items": selected,
                "pagination": {
                    "page": current_page,
                    "page_size": page_size,
                    "total_items": total,
                    "total_pages": total_pages,
                    "sort_by": sort_by,
                    "sort_order": sort_order,
                },
            }

        return self._execute(read)

    def event_detail_record(self, event_id: str) -> dict[str, Any] | None:
        safe_id = str(event_id or "").strip()
        if not safe_id:
            return None
        def read(connection: sqlite3.Connection) -> dict[str, Any] | None:
            occurrence_id: int | None = None
            source_event_id = safe_id
            marker = "#occurrence-"
            if marker in safe_id:
                source_event_id, suffix = safe_id.rsplit(marker, 1)
                try:
                    occurrence_id = int(suffix)
                except ValueError:
                    return None
            row = connection.execute(
                """
                SELECT o.id AS occurrence_id,o.source_event_id,o.category,o.platform,
                       o.origin_url,o.path,o.context_masked,o.evidence_json,
                       o.detected_at,o.first_discovered_at,o.last_discovered_at,
                       o.first_seen_at,c.type AS credential_type,
                       c.fingerprint,c.id AS credential_id,sc.published_at,
                       sc.published_source,sc.published_confidence,sc.fetched_at,
                       sc.fetched_at_source,sc.source_updated_at,
                       o.time_provenance_json AS occurrence_time_provenance_json,
                       sc.time_provenance_json AS content_time_provenance_json
                FROM occurrences o
                JOIN credentials c ON c.id=o.credential_id
                LEFT JOIN source_contents sc ON sc.id=o.content_id
                WHERE (o.source_event_id=? AND (? IS NULL OR o.id=?))
                ORDER BY
                    (o.first_discovered_at IS NOT NULL) DESC,
                    (o.detected_at IS NOT NULL) DESC,
                    o.id DESC
                LIMIT 1
                """,
                (source_event_id, occurrence_id, occurrence_id),
            ).fetchone()
            if row is None:
                return None
            event = self._event_record_from_row(row)
            event["event_id"] = safe_id
            discovery_chain = _discovery_time_chain(row)
            evidence = _load(row["evidence_json"], {})
            provenance = _row_time_provenance(row)
            public_fields = _public_time_fields(provenance)
            published_entry = provenance.get("published_at") or {}
            source_updated_entry = provenance.get("source_updated_at") or {}
            notification = connection.execute(
                "SELECT * FROM notification_outbox WHERE event_id=?",
                (source_event_id,),
            ).fetchone()
            logs = connection.execute(
                "SELECT action,note,actor,created_at FROM review_logs WHERE credential_id=? ORDER BY id",
                (row["credential_id"],),
            ).fetchall()
            timeline = [
                {
                    "time": published_entry.get("value"),
                    "title": "凭据公开时间证据",
                    "detail": (
                        f"来源：{published_entry.get('source') or '来源未提供'}；"
                        f"含义：{published_entry.get('meaning') or '未记录'}；"
                        f"置信度：{published_entry.get('confidence') or 'none'}；"
                        f"范围：{published_entry.get('evidence_scope') or '未记录'}；"
                        f"版本：{published_entry.get('content_version') or '未记录'}"
                    ),
                },
                {
                    "time": discovery_chain["first_discovered_at"],
                    "title": "系统首次发现",
                    "detail": (
                        "规则检测实际形成有效凭据结果的时间；"
                        "重复扫描不会覆盖该时间；没有历史证据时只展示最早有证据的检测时间。"
                    ),
                },
                {
                    "time": source_updated_entry.get("value"),
                    "title": "内容版本修订时间",
                    "detail": (
                        f"来源：{source_updated_entry.get('source') or '来源未提供'}；"
                        f"含义：{source_updated_entry.get('meaning') or '仅作为内容修订参考'}；"
                        f"证据：{source_updated_entry.get('evidence') or '未记录'}"
                    ),
                },
                {
                    "time": row["first_seen_at"],
                    "title": "首次观测内容",
                    "detail": "来自 occurrence.first_seen_at；仅表示首次观测来源对象。",
                },
                {
                    "time": row["fetched_at"],
                    "title": "来源采集完成",
                    "detail": f"采集时间来源：{row['fetched_at_source'] or 'unknown'}。",
                },
                {
                    "time": row["detected_at"],
                    "title": "检测完成",
                    "detail": str(evidence.get("detection_method") or "未记录"),
                },
            ]
            if notification is not None:
                timeline.append({
                    "time": notification["created_at"],
                    "title": "告警进入离线队列",
                    "detail": notification["delivery_note"],
                })
            for log in logs:
                timeline.append({
                    "time": log["created_at"],
                    "title": f"人工复核：{log['action']}",
                    "detail": "备注已脱敏省略。",
                })
            timeline = [item for item in timeline if item["time"]]
            timeline.sort(key=lambda item: str(item["time"]))
            return {
                "event": event,
                "evidence": evidence,
                "published_at": event.get("published_at"),
                "published_source": event.get("published_source"),
                "published_confidence": event.get("published_confidence"),
                "published_meaning": event.get("published_meaning"),
                "published_evidence": event.get("published_evidence"),
                "public_time_status": public_fields["public_time_status"],
                "public_time_unknown_reason": public_fields["public_time_unknown_reason"],
                "public_time_precision": public_fields["published_precision"],
                "public_time_evidence_scope": public_fields["published_evidence_scope"],
                "public_time_content_version": public_fields["published_content_version"],
                "fetched_at": row["fetched_at"],
                "fetched_at_source": row["fetched_at_source"],
                "source_updated_at": event.get("source_updated_at"),
                "source_updated_source": event.get("source_updated_source"),
                "source_updated_confidence": event.get("source_updated_confidence"),
                "source_updated_meaning": event.get("source_updated_meaning"),
                "source_updated_evidence": event.get("source_updated_evidence"),
                "time_provenance": provenance,
                "discovery_latency": {
                    "seconds": discovery_chain["discovery_latency_seconds"],
                    "status": discovery_chain["discovery_latency_status"],
                    "reason": discovery_chain["discovery_latency_reason"],
                    "calculation_type": discovery_chain["discovery_latency_calculation_type"],
                    "basis": discovery_chain["discovery_latency_basis"],
                    "end_at": discovery_chain["discovery_latency_end_at"],
                    "first_discovered_at": discovery_chain["first_discovered_at"],
                    "first_discovery_time_status": discovery_chain["first_discovery_time_status"],
                    "first_discovery_unknown_reason": discovery_chain["first_discovery_unknown_reason"],
                },
                "time_fields": {
                    "published_at": row["published_at"],
                    "first_observed_at": row["first_seen_at"],
                    "fetched_at": row["fetched_at"],
                    "detected_at": row["detected_at"],
                    "first_discovered_at": discovery_chain["first_discovered_at"],
                    "first_discovery_time_status": discovery_chain["first_discovery_time_status"],
                    "first_discovery_unknown_reason": discovery_chain["first_discovery_unknown_reason"],
                    "last_discovered_at": row["last_discovered_at"] if "last_discovered_at" in row.keys() else None,
                    "discovery_latency_seconds": discovery_chain["discovery_latency_seconds"],
                    "discovery_latency_status": discovery_chain["discovery_latency_status"],
                    "discovery_latency_reason": discovery_chain["discovery_latency_reason"],
                    "discovery_latency_calculation_type": discovery_chain["discovery_latency_calculation_type"],
                    "discovery_latency_basis": discovery_chain["discovery_latency_basis"],
                    "discovery_latency_end_at": discovery_chain["discovery_latency_end_at"],
                    "public_confirmed_at": next(
                        (log["created_at"] for log in logs if log["action"] == "confirm"),
                        None,
                    ),
                },
                "notification": (
                    {**(self._row(notification) or {}), "payload": _load(notification["payload_json"], {})}
                    if notification is not None else None
                ),
                "review_logs": [self._row(log) or {} for log in logs],
                "timeline": timeline,
            }
        return self._execute(read)

    def overview_stats(self) -> dict[str, Any]:
        def read(connection: sqlite3.Connection) -> dict[str, Any]:
            scalar = lambda sql, args=(): int(connection.execute(sql, args).fetchone()["n"])
            real_source_clause = "json_extract(o.evidence_json, '$.source_kind') = '真实检测结果'"
            real_by_type = connection.execute(
                f"""
                SELECT c.type,COUNT(*) AS n
                FROM occurrences o
                JOIN credentials c ON c.id=o.credential_id
                WHERE {real_source_clause}
                GROUP BY c.type
                ORDER BY n DESC,c.type
                LIMIT 50
                """
            ).fetchall()
            all_by_type = connection.execute(
                """
                SELECT c.type,COUNT(DISTINCT o.credential_id) AS n
                FROM occurrences o
                JOIN credentials c ON c.id=o.credential_id
                GROUP BY c.type
                ORDER BY n DESC,c.type
                """
            ).fetchall()
            by_platform = connection.execute(
                f"""
                SELECT platform,COUNT(*) AS n
                FROM occurrences o
                WHERE {real_source_clause}
                GROUP BY platform
                ORDER BY n DESC,platform
                """
            ).fetchall()
            by_channel = connection.execute(
                f"""
                SELECT category,COUNT(*) AS n
                FROM occurrences o
                WHERE {real_source_clause}
                GROUP BY category
                ORDER BY n DESC,category
                """
            ).fetchall()
            all_by_channel = connection.execute(
                """
                SELECT category,COUNT(*) AS n
                FROM occurrences
                GROUP BY category
                ORDER BY n DESC,category
                """
            ).fetchall()
            all_platform_summary = connection.execute(
                """
                SELECT category,platform,COUNT(*) AS n
                FROM occurrences
                WHERE category IS NOT NULL AND platform IS NOT NULL
                GROUP BY category,platform
                ORDER BY n DESC,category,platform
                """
            ).fetchall()
            platform_write_summary = connection.execute(
                """
                SELECT channel,platform,SUM(inserted_count) AS n
                FROM run_evidence
                WHERE status='completed'
                  AND platform IS NOT NULL
                  AND TRIM(platform) <> ''
                GROUP BY channel,platform
                ORDER BY n DESC,channel,platform
                """
            ).fetchall()
            collector_write_summary = connection.execute(
                """
                SELECT collector_key,SUM(inserted_count) AS n
                FROM run_evidence
                WHERE status='completed'
                GROUP BY collector_key
                ORDER BY n DESC,collector_key
                """
            ).fetchall()
            coverage_rows = connection.execute(
                """
                SELECT channel_type,platform,collection_method
                FROM support_matrix
                WHERE item_kind='channel'
                ORDER BY id
                """
            ).fetchall()
            public_coverage_rows = [
                row for row in coverage_rows
                if str(row["collection_method"] or "") in {"online-public", "online-authorized"}
            ]
            configured_channel_type_count = len({
                str(row["channel_type"])
                for row in coverage_rows
                if row["channel_type"]
            })
            configured_platform_count = len({
                str(row["platform"])
                for row in coverage_rows
                if row["platform"]
            })
            public_channel_type_count = len({
                str(row["channel_type"])
                for row in public_coverage_rows
                if row["channel_type"]
            })
            public_platform_count = len({
                str(row["platform"])
                for row in public_coverage_rows
                if row["platform"]
            })
            latency_rows = connection.execute(
                """
                SELECT sc.published_at,sc.fetched_at,sc.fetched_at_source,
                       o.first_seen_at,o.first_discovered_at,o.detected_at,
                       sc.published_source,sc.published_confidence,
                       sc.source_updated_at,
                       o.time_provenance_json AS occurrence_time_provenance_json,
                       sc.time_provenance_json AS content_time_provenance_json,
                       n.created_at AS notified_at
                FROM occurrences o
                LEFT JOIN source_contents sc ON sc.id=o.content_id
                LEFT JOIN notification_outbox n ON n.event_id=o.source_event_id
                WHERE json_extract(o.evidence_json, '$.source_kind') = '真实检测结果'
                """
            ).fetchall()
            published_to_discovered: list[float] = []
            fetched_to_detected: list[float] = []
            observed_to_detected: list[float] = []
            detected_to_alert: list[float] = []
            unknown = {
                "published_at": 0,
                "first_observed_at": 0,
                "first_discovered_at": 0,
                "fetched_at": 0,
                "detected_at": 0,
                "alerted_at": 0,
            }
            public_time_eligible_count = 0
            public_time_ineligible_count = 0
            public_time_known_count = 0
            for row in latency_rows:
                provenance = _row_time_provenance(row)
                published_entry = provenance.get("published_at") or {}
                discovery = _discovery_time_chain(row)
                discovered = _parse_time(discovery["first_discovered_at"])
                detected = _parse_time(row["detected_at"])
                published = _parse_time(published_entry.get("value"))
                fetched = _parse_time(row["fetched_at"])
                observed = _parse_time(row["first_seen_at"])
                notified = _parse_time(row["notified_at"])
                if published is None:
                    unknown["published_at"] += 1
                else:
                    public_time_known_count += 1
                    if latency_eligible(provenance):
                        public_time_eligible_count += 1
                    else:
                        public_time_ineligible_count += 1
                unknown["first_observed_at"] += observed is None
                unknown["first_discovered_at"] += discovered is None
                unknown["fetched_at"] += row["fetched_at_source"] != "connector" or fetched is None
                unknown["detected_at"] += detected is None
                unknown["alerted_at"] += notified is None
                if discovered and published and latency_eligible(provenance):
                    value = (discovered - published).total_seconds()
                    if value >= 0:
                        published_to_discovered.append(value)
                if detected and fetched and row["fetched_at_source"] == "connector":
                    value = (detected - fetched).total_seconds()
                    if 0 <= value <= 24 * 3600:
                        fetched_to_detected.append(value)
                if detected and observed:
                    value = (detected - observed).total_seconds()
                    if 0 <= value <= 24 * 3600:
                        observed_to_detected.append(value)
                if detected and notified:
                    value = (notified - detected).total_seconds()
                    if 0 <= value <= 24 * 3600:
                        detected_to_alert.append(value)
            real_credentials_total = scalar(
                f"""
                SELECT COUNT(DISTINCT o.credential_id) AS n
                FROM occurrences o
                WHERE {real_source_clause}
                """
            )
            real_occurrences_total = scalar(
                f"SELECT COUNT(*) AS n FROM occurrences o WHERE {real_source_clause}"
            )
            review_status_counts = {
                row["review_status"]: int(row["n"])
                for row in connection.execute(
                    f"""
                    SELECT c.review_status,COUNT(DISTINCT c.id) AS n
                    FROM credentials c
                    JOIN occurrences o ON o.credential_id=c.id
                    WHERE {real_source_clause}
                    GROUP BY c.review_status
                    """
                ).fetchall()
            }
            verification_status_counts = {
                row["verification_status"]: int(row["n"])
                for row in connection.execute(
                    f"""
                    SELECT c.verification_status,COUNT(DISTINCT c.id) AS n
                    FROM credentials c
                    JOIN occurrences o ON o.credential_id=c.id
                    WHERE {real_source_clause}
                    GROUP BY c.verification_status
                    """
                ).fetchall()
            }
            all_credentials_total = scalar(
                "SELECT COUNT(DISTINCT o.credential_id) AS n FROM occurrences o"
            )
            all_occurrences_total = scalar("SELECT COUNT(*) AS n FROM occurrences")
            all_review_status_counts = {
                row["review_status"]: int(row["n"])
                for row in connection.execute(
                    """
                    SELECT c.review_status,COUNT(DISTINCT c.id) AS n
                    FROM credentials c
                    JOIN occurrences o ON o.credential_id=c.id
                    GROUP BY c.review_status
                    """
                ).fetchall()
            }
            all_verification_status_counts = {
                row["verification_status"]: int(row["n"])
                for row in connection.execute(
                    """
                    SELECT c.verification_status,COUNT(DISTINCT c.id) AS n
                    FROM credentials c
                    JOIN occurrences o ON o.credential_id=c.id
                    GROUP BY c.verification_status
                    """
                ).fetchall()
            }
            all_verified_count = sum(
                all_verification_status_counts.get(status, 0)
                for status in ("valid", "controlled_valid")
            )
            return {
                "source": "真实数据：规范化后端 SQLite overview_stats",
                "implementation_status": "部分实现",
                "error": None,
                "credentials_total": scalar("SELECT COUNT(*) AS n FROM credentials"),
                "occurrences_total": scalar("SELECT COUNT(*) AS n FROM occurrences"),
                "all_credentials_total": all_credentials_total,
                "all_occurrences_total": all_occurrences_total,
                "real_credentials_total": real_credentials_total,
                "real_occurrences_total": real_occurrences_total,
                "credential_type_count": len({str(row["type"]) for row in all_by_type if row["type"]}),
                "credential_type_total": len({str(row["type"]) for row in all_by_type if row["type"]}),
                "sources_total": scalar("SELECT COUNT(*) AS n FROM sources"),
                "tasks_total": scalar("SELECT COUNT(*) AS n FROM tasks"),
                "by_review_status": review_status_counts,
                "by_verification_status": verification_status_counts,
                "detection_pipeline": [
                    {
                        "key": "collected",
                        "label": "原始采集",
                        "description": "全部来源出现位置",
                        "count": all_occurrences_total,
                    },
                    {
                        "key": "parsed",
                        "label": "规则解析",
                        "description": "全部来源凭据实体",
                        "count": all_credentials_total,
                    },
                    {
                        "key": "pending",
                        "label": "待复核",
                        "description": "等待人工确认",
                        "count": int(all_review_status_counts.get("pending", 0)),
                    },
                    {
                        "key": "confirmed",
                        "label": "已确认",
                        "description": "治理结果已记录",
                        "count": int(all_review_status_counts.get("confirmed", 0)),
                    },
                    {
                        "key": "verified",
                        "label": "已验证",
                        "description": "身份验证或受控校验通过",
                        "count": int(all_verified_count),
                    },
                ],
                "by_type_top": [[row["type"], int(row["n"])] for row in real_by_type],
                "all_by_type_top": [[row["type"], int(row["n"])] for row in all_by_type],
                "by_platform": [[row["platform"], int(row["n"])] for row in by_platform],
                "by_channel": [[row["category"], int(row["n"])] for row in by_channel],
                "all_by_channel": [[row["category"], int(row["n"])] for row in all_by_channel],
                "all_platform_summary": [
                    {
                        "channel": str(row["category"]),
                        "platform": str(row["platform"]),
                        "count": int(row["n"]),
                    }
                    for row in all_platform_summary
                ],
                "platform_write_summary": [
                    {
                        "channel": str(row["channel"]),
                        "platform": str(row["platform"]),
                        "count": int(row["n"] or 0),
                    }
                    for row in platform_write_summary
                ],
                "collector_write_summary": [
                    {
                        "collector_key": str(row["collector_key"]),
                        "count": int(row["n"] or 0),
                    }
                    for row in collector_write_summary
                ],
                "coverage": {
                    "configured_channel_type_count": configured_channel_type_count,
                    "configured_platform_count": configured_platform_count,
                    "public_channel_type_count": public_channel_type_count,
                    "public_platform_count": public_platform_count,
                    "observed_channel_type_count": len({
                        str(row["category"])
                        for row in by_channel
                        if row["category"]
                    }),
                    "observed_platform_count": len({
                        str(row["platform"])
                        for row in by_platform
                        if row["platform"]
                    }),
                    "definition": "配置覆盖来自 support_matrix；真实观测覆盖来自 source_kind=真实检测结果 的 occurrence。",
                },
                "statistics": {
                    "configured_channel_type_count": configured_channel_type_count,
                    "configured_platform_count": configured_platform_count,
                    "observed_channel_type_count": len({
                        str(row["category"])
                        for row in by_channel
                        if row["category"]
                    }),
                    "observed_platform_count": len({
                        str(row["platform"])
                        for row in by_platform
                        if row["platform"]
                    }),
                    "real_credential_count": scalar(
                        f"""
                        SELECT COUNT(DISTINCT o.credential_id) AS n
                        FROM occurrences o
                        WHERE {real_source_clause}
                        """
                    ),
                    "real_candidate_count": scalar(
                        f"SELECT COUNT(*) AS n FROM occurrences o WHERE {real_source_clause}"
                    ),
                    "real_credential_type_count": len({
                        str(row["type"])
                        for row in real_by_type
                        if row["type"]
                    }),
                    "all_stored_credential_count": scalar("SELECT COUNT(*) AS n FROM credentials"),
                    "all_stored_occurrence_count": scalar("SELECT COUNT(*) AS n FROM occurrences"),
                },
                "credential_summary": [
                    {"type": str(row["type"]), "count": int(row["n"])}
                    for row in all_by_type
                    if row["type"]
                ],
                "platform_summary": [
                    {"platform": str(row["platform"]), "count": int(row["n"])}
                    for row in by_platform
                    if row["platform"]
                ],
                "channel_summary": [
                    {"channel": str(row["category"]), "objects": int(row["n"])}
                    for row in by_channel
                    if row["category"]
                ],
                "verification_queue": scalar(
                    "SELECT COUNT(*) AS n FROM verification_jobs WHERE status='queued'"
                ),
                "running_scans": scalar(
                    "SELECT COUNT(*) AS n FROM scan_runs WHERE status='running'"
                ),
                "notification_queue": scalar(
                    "SELECT COUNT(*) AS n FROM notification_outbox WHERE status='queued'"
                ),
                "notification_recorded": scalar(
                    "SELECT COUNT(*) AS n FROM notification_outbox WHERE status='offline_recorded'"
                ),
                "recent_runs": self.list_runs(limit=10),
                "detection_latency": {
                    "sample_count": len(published_to_discovered),
                    "p50_seconds": _percentile(published_to_discovered, 0.50),
                    "p95_seconds": _percentile(published_to_discovered, 0.95),
                    "published_to_discovered": {
                        "sample_count": len(published_to_discovered),
                        "p50_seconds": _percentile(published_to_discovered, 0.50),
                        "p95_seconds": _percentile(published_to_discovered, 0.95),
                    },
                    "published_to_detected": {
                        "sample_count": len(published_to_discovered),
                        "p50_seconds": _percentile(published_to_discovered, 0.50),
                        "p95_seconds": _percentile(published_to_discovered, 0.95),
                    },
                    "fetched_to_detected": {
                        "sample_count": len(fetched_to_detected),
                        "p50_seconds": _percentile(fetched_to_detected, 0.50),
                        "p95_seconds": _percentile(fetched_to_detected, 0.95),
                    },
                    "observed_to_detected": {
                        "sample_count": len(observed_to_detected),
                        "p50_seconds": _percentile(observed_to_detected, 0.50),
                        "p95_seconds": _percentile(observed_to_detected, 0.95),
                    },
                    "detected_to_alert": {
                        "sample_count": len(detected_to_alert),
                        "p50_seconds": _percentile(detected_to_alert, 0.50),
                        "p95_seconds": _percentile(detected_to_alert, 0.95),
                    },
                    "note": "公开证据只与同一出现位置的系统首次发现时间计算；参考时间单独与规则检测完成时间计算；未知和负差值不计入。",
                    "unknown_counts": unknown,
                    "public_time_eligible_count": public_time_eligible_count,
                    "public_time_ineligible_count": public_time_ineligible_count,
                    "public_time_known_count": public_time_known_count,
                    "public_time_coverage_percent": round(
                        public_time_eligible_count / len(latency_rows) * 100, 1
                    ) if latency_rows else 0.0,
                    "timezone": "UTC；输入无时区时按 UTC 解析并保留解析边界。",
                    "field_sources": {
                        "published_at": "source_contents.published_at + published_source",
                        "first_observed_at": "occurrences.first_seen_at，仅表示首次观测来源对象",
                        "first_discovered_at": "occurrences.first_discovered_at；系统首次实际识别凭据",
                        "fetched_at": "source_contents.fetched_at，仅 fetched_at_source=connector 进入采集延迟",
                        "detected_at": "occurrences.detected_at",
                        "alerted_at": "notification_outbox.created_at",
                        "manual_confirmed_at": "review_logs.created_at(action=confirm)，概览暂不合并到单次延迟",
                    },
                },
            }

        return self._execute(read)

    def evaluation_report(self) -> dict[str, Any]:
        """Return an evidence-labelled report from the unified SQLite model."""
        def read(connection: sqlite3.Connection) -> dict[str, Any]:
            rows = connection.execute(
                """
                SELECT o.id AS occurrence_id,o.category,o.platform,o.origin_url,o.repo,o.path,
                       o.source_event_id,o.first_seen_at,o.first_discovered_at,
                       o.last_discovered_at,o.last_seen_at,o.detected_at,o.evidence_json,
                       sc.published_at,sc.published_source,sc.published_confidence,
                       sc.source_updated_at,sc.fetched_at,sc.fetched_at_source,
                       o.time_provenance_json AS occurrence_time_provenance_json,
                       sc.time_provenance_json AS content_time_provenance_json,
                       c.type,c.fingerprint,c.id AS credential_id,
                       n.created_at AS alerted_at,
                       (
                         SELECT MIN(rl.created_at)
                         FROM review_logs rl
                         WHERE rl.credential_id=c.id AND rl.action='confirm'
                       ) AS manual_confirmed_at,
                       json_extract(o.evidence_json,'$.source_kind') AS source_kind
                FROM occurrences o
                JOIN credentials c ON c.id=o.credential_id
                LEFT JOIN source_contents sc ON sc.id=o.content_id
                LEFT JOIN notification_outbox n ON n.event_id=o.source_event_id
                ORDER BY o.id
                """
            ).fetchall()
            real = [row for row in rows if row["source_kind"] == "真实检测结果"]
            channel_types = {str(row["category"]) for row in real if row["category"]}
            platforms = {str(row["platform"]) for row in real if row["platform"]}
            objects = {
                str(row["origin_url"] or row["repo"] or row["path"])
                for row in real
                if row["origin_url"] or row["repo"] or row["path"]
            }
            observed_credential_types = {str(row["type"]) for row in real if row["type"]}
            supported_rule_types = {
                str(row["type"])
                for row in connection.execute(
                    """
                    SELECT DISTINCT type
                    FROM rule_records
                    WHERE enabled=1 AND type IS NOT NULL AND TRIM(type) <> ''
                    """
                ).fetchall()
            }
            unique_fingerprints = {
                str(row["fingerprint"]) for row in real if row["fingerprint"]
            }
            coverage_rows = connection.execute(
                """
                SELECT channel_type,platform,collection_method
                FROM support_matrix
                WHERE item_kind='channel'
                """
            ).fetchall()
            public_coverage_rows = [
                row for row in coverage_rows
                if str(row["collection_method"] or "") in {"online-public", "online-authorized"}
            ]
            configured_channel_type_count = len({
                str(row["channel_type"])
                for row in coverage_rows
                if row["channel_type"]
            })
            configured_platform_count = len({
                str(row["platform"])
                for row in coverage_rows
                if row["platform"]
            })
            public_channel_type_count = len({
                str(row["channel_type"])
                for row in public_coverage_rows
                if row["channel_type"]
            })
            public_platform_count = len({
                str(row["platform"])
                for row in public_coverage_rows
                if row["platform"]
            })
            public_coverage = {
                "channel_type_count": public_channel_type_count,
                "platform_count": public_platform_count,
            }
            disposition_values: list[str] = []
            for row in real:
                disposition_values.append(str(_load(row["evidence_json"], {}).get("disposition") or "待确认"))
            confirmed_public_count = sum(value == "已确认公开泄露" for value in disposition_values)

            pairs = {
                "published_to_observed": ("published_at", "first_seen_at", None),
                "published_to_discovered": ("published_at", "first_discovered_at", None),
                "published_to_detected": ("published_at", "detected_at", None),
                "reference_to_detected": ("reference_at", "detected_at", None),
                "observed_to_detected": ("first_seen_at", "detected_at", None),
                "fetched_to_detected": ("fetched_at", "detected_at", None),
                "detected_to_alert": ("detected_at", "alerted_at", None),
                "detected_to_confirmation": ("detected_at", "manual_confirmed_at", None),
            }
            fields = (
                "published_at",
                "first_seen_at",
                "first_discovered_at",
                "fetched_at",
                "detected_at",
                "alerted_at",
                "manual_confirmed_at",
            )

            def calculate_timeliness(subset: list[sqlite3.Row]) -> dict[str, Any]:
                """Calculate latency without merging provenance classes."""
                metrics: dict[str, list[float]] = {
                    name: [] for name in pairs
                }
                unknown_counts = {field: 0 for field in fields}
                unknown_counts["reference_at"] = 0
                ineligible_counts = {"published_at": 0}
                eligible_counts = {"published_at": 0}
                missing_by_metric = {name: 0 for name in pairs}
                for row in subset:
                    provenance = _row_time_provenance(row)
                    published_entry = provenance.get("published_at") or {}
                    detected_entry = provenance.get("detected_at") or {}
                    timestamps = {
                        "published_at": (
                            _parse_time(published_entry.get("value"))
                            if latency_eligible(provenance)
                            else None
                        ),
                        "reference_at": (
                            _parse_time(published_entry.get("value"))
                            if reference_time_eligible(provenance, "published_at")
                            else None
                        ),
                        "first_seen_at": _parse_time(row["first_seen_at"]),
                        "first_discovered_at": (
                            _parse_time(row["first_discovered_at"])
                            if first_discovery_eligible(provenance)
                            and row["first_discovered_at"]
                            else _parse_time(
                                (provenance.get("first_discovered_at") or {}).get("value")
                            )
                            if first_discovery_eligible(provenance)
                            else None
                        ),
                        "fetched_at": _parse_time(row["fetched_at"])
                        if row["fetched_at_source"] == "connector"
                        else None,
                        "detected_at": (
                            _parse_time(detected_entry.get("value"))
                            if detection_time_eligible(provenance)
                            else None
                        ),
                        "alerted_at": _parse_time(row["alerted_at"]),
                        "manual_confirmed_at": _parse_time(row["manual_confirmed_at"]),
                    }
                    raw_published = _parse_time(published_entry.get("value"))
                    if raw_published is None:
                        unknown_counts["published_at"] += 1
                    elif latency_eligible(provenance):
                        eligible_counts["published_at"] += 1
                    else:
                        ineligible_counts["published_at"] += 1
                    for field, value in timestamps.items():
                        if field != "published_at":
                            unknown_counts[field] += value is None

                    for name, (start_key, end_key, upper_bound) in pairs.items():
                        start = timestamps[start_key]
                        end = timestamps[end_key]
                        if start is None or end is None:
                            missing_by_metric[name] += 1
                            continue
                        value = (end - start).total_seconds()
                        if value >= 0 and (
                            upper_bound is None or value <= upper_bound
                        ):
                            metrics[name].append(value)
                        else:
                            missing_by_metric[name] += 1
                return {
                    "metrics": {
                        name: {
                            "sample_count": len(values),
                            "p50_seconds": _percentile(values, 0.50),
                            "p95_seconds": _percentile(values, 0.95),
                            "unknown_count": missing_by_metric[name],
                        }
                        for name, values in metrics.items()
                    },
                    "unknown_time_counts": unknown_counts,
                    "ineligible_time_counts": ineligible_counts,
                    "eligible_time_counts": eligible_counts,
                }

            source_breakdown: dict[str, int] = {}
            rows_by_source_kind: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                kind = str(row["source_kind"] or "未知")
                source_breakdown[kind] = source_breakdown.get(kind, 0) + 1
                rows_by_source_kind.setdefault(kind, []).append(row)

            timeliness_calculations = {
                kind: calculate_timeliness(kind_rows)
                for kind, kind_rows in rows_by_source_kind.items()
            }
            real_timeliness = calculate_timeliness(real)
            timeliness_by_source_kind = {
                kind: {
                    "occurrence_count": len(kind_rows),
                    "metrics": timeliness_calculations[kind]["metrics"],
                    "unknown_time_counts": timeliness_calculations[kind]["unknown_time_counts"],
                    "eligible_time_counts": timeliness_calculations[kind]["eligible_time_counts"],
                    "ineligible_time_counts": timeliness_calculations[kind]["ineligible_time_counts"],
                }
                for kind, kind_rows in sorted(rows_by_source_kind.items())
            }
            time_stats = real_timeliness["metrics"]
            unknown_counts = real_timeliness["unknown_time_counts"]
            ineligible_counts = real_timeliness["ineligible_time_counts"]
            eligible_counts = real_timeliness["eligible_time_counts"]
            time_fields = (
                ("published_at", "来源公开/内容时间"),
                ("first_seen_at", "首次观测时间"),
                ("first_discovered_at", "系统首次发现时间"),
                ("fetched_at", "来源采集时间"),
                ("detected_at", "规则检测时间"),
                ("alerted_at", "告警入队时间"),
                ("manual_confirmed_at", "人工确认公开时间"),
            )
            known_counts = {
                key: (
                    int(eligible_counts["published_at"])
                    + int(ineligible_counts["published_at"])
                    if key == "published_at"
                    else len(real) - int(unknown_counts[key])
                )
                for key, _label in time_fields
            }
            time_field_coverage = {
                key: {
                    "label": label,
                    "total_count": len(real),
                    "known_count": known_counts[key],
                    "eligible_count": (
                        int(eligible_counts["published_at"])
                        if key == "published_at"
                        else len(real) - int(unknown_counts[key])
                    ),
                    "ineligible_count": (
                        int(ineligible_counts["published_at"])
                        if key == "published_at"
                        else 0
                    ),
                    "unknown_count": int(unknown_counts[key]),
                    "coverage_percent": round(
                        (known_counts[key] / len(real)) * 100,
                        1,
                    ) if real else 0.0,
                    "eligible_coverage_percent": round(
                        (
                            (
                                int(eligible_counts["published_at"])
                                if key == "published_at"
                                else len(real) - int(unknown_counts[key])
                            ) / len(real)
                        ) * 100,
                        1,
                    ) if real else 0.0,
                }
                for key, label in time_fields
            }
            ordered_time_rows = sorted(
                real,
                key=lambda row: (
                    _parse_time(row["last_seen_at"] or row["first_seen_at"])
                    or datetime.min.replace(tzinfo=timezone.utc),
                    _parse_time(row["first_seen_at"])
                    or datetime.min.replace(tzinfo=timezone.utc),
                    int(row["occurrence_id"]),
                ),
                reverse=True,
            )
            time_samples: list[dict[str, Any]] = []
            seen_time_event_ids: set[str] = set()
            for row in ordered_time_rows[:200]:
                raw_event_id = str(row["source_event_id"] or f"OCC-{row['occurrence_id']}")
                event_id = raw_event_id
                if raw_event_id in seen_time_event_ids:
                    event_id = f"{raw_event_id}#occurrence-{row['occurrence_id']}"
                seen_time_event_ids.add(raw_event_id)
                time_samples.append({
                    "event_id": event_id,
                    "credential_type": str(row["type"] or ""),
                    "platform": str(row["platform"] or ""),
                    "source_kind": str(row["source_kind"] or "未知"),
                    "published_at": row["published_at"],
                    "published_source": row["published_source"] or "",
                    "published_confidence": row["published_confidence"] or "none",
                    "published_meaning": (
                        (_row_time_provenance(row).get("published_at") or {}).get("meaning") or ""
                    ),
                    "published_evidence": (
                        (_row_time_provenance(row).get("published_at") or {}).get("evidence") or ""
                    ),
                    "public_time_status": _public_time_fields(_row_time_provenance(row))["public_time_status"],
                    "public_time_unknown_reason": _public_time_fields(_row_time_provenance(row))["public_time_unknown_reason"],
                    "source_updated_at": (
                        (_row_time_provenance(row).get("source_updated_at") or {}).get("value")
                    ),
                    "source_updated_source": (
                        (_row_time_provenance(row).get("source_updated_at") or {}).get("source")
                    ),
                    "first_observed_at": row["first_seen_at"],
                    "first_discovered_at": row["first_discovered_at"],
                    "last_discovered_at": row["last_discovered_at"],
                    "last_observed_at": row["last_seen_at"],
                    "fetched_at": row["fetched_at"],
                    "detected_at": row["detected_at"],
                    "alerted_at": row["alerted_at"],
                    "public_confirmed_at": row["manual_confirmed_at"],
                    **_discovery_time_chain(row),
                })
            experiments = self.list_extensibility_experiments()
            extensibility_rows = [
                {
                    "label": str(item.get("title") or item.get("connector_key") or "未命名实验"),
                    "value": (
                        f'{"发布包运行时检查通过" if item.get("runtime_check_status") == "已通过" else "发布包运行时检查未通过"}；'
                        f'完整实验记录={"已通过" if item.get("execution_status") == "passed" else "未完整执行" if item.get("execution_status") == "not_run" else item.get("execution_status") or "未知"}；'
                        f'代码量={item.get("code_lines") or "未知"}；'
                        f'配置项={item.get("config_fields") or "未知"}；'
                        f'检查耗时={item.get("test_duration_ms") if item.get("test_duration_ms") is not None else "未知"}ms；'
                        f'checkpoint={"已验证" if item.get("checkpoint_verified") else "未验证"}；'
                        f'失败恢复={"已验证" if item.get("failure_recovery_verified") else "未验证"}'
                    ),
                    "status": (
                        "passed"
                        if item.get("runtime_check_status") == "已通过"
                        else "failed"
                    ),
                    "evidence_ref": str(item.get("evidence_ref") or ""),
                    "notes": str(item.get("notes") or ""),
                }
                for item in experiments
            ]
            evaluation_mapping = [
                {
                    "evaluation": "①公开渠道类型数量、总数量",
                    "module": "support_matrix / occurrences",
                    "current": f"{configured_channel_type_count} 类 / {configured_platform_count} 平台",
                    "definition": "按 support_matrix 中已配置的渠道类型和平台去重；真实观测数量单独保存在 observed 字段。",
                    "status": "统一支持矩阵口径",
                },
                {
                    "evaluation": "②支持检索凭据类型数量",
                    "module": "rule_records / credentials",
                    "current": f"{len(supported_rule_types)} 类",
                    "definition": (
                        "按已启用规则记录中的凭据类型去重；真实检测命中的类型单独统计，"
                        f"当前为 {len(observed_credential_types)} 类。"
                    ),
                    "status": "已从统一后端重算；官方口径待确认",
                },
                {
                    "evaluation": "③平台可扩展性",
                    "module": "extensibility_experiments",
                    "current": (
                        f"{sum(item.get('runtime_check_status') == '已通过' for item in experiments)}"
                        f" / {len(experiments)} 项发布包运行时检查通过"
                    ),
                    "definition": (
                        "发布包运行时检查验证实现模块和连接器入口可加载；"
                        "完整功能实验、checkpoint、失败恢复和真实平台实测单独记录。"
                    ),
                    "status": "已返回运行时实验记录；真实授权平台实测按来源任务记录",
                },
                {
                    "evaluation": "④发现凭据数量",
                    "module": "credentials / occurrences",
                    "current": f'{len(unique_fingerprints)} 唯一 / {len(real)} 候选 / {confirmed_public_count} 已确认公开',
                    "definition": "候选、唯一 HMAC 指纹和公开确认分开统计，不把受控样例计入真实检测结果。",
                    "status": "可由 SQLite 明细重算",
                },
                {
                    "evaluation": "⑤发现时间与凭据公开泄露时间",
                    "module": "source_contents + occurrences + review_logs",
                    "current": _format_report_metric(time_stats["published_to_discovered"]),
                    "definition": "公开至系统首次发现单独统计；只有参考时间的记录进入内容时间至检出统计，未知字段不估算。",
                    "status": "字段链已实现；样本不足时显示未知",
                },
            ]
            source_breakdown_text = "；".join(
                f"{kind}={count}"
                for kind, count in sorted(source_breakdown.items())
            ) or "暂无记录"
            time_boundary_text = "；".join(
                (
                    f'{details["label"]}：已记录{details["known_count"]}条，'
                    f'未知{details["unknown_count"]}条，覆盖率{details["coverage_percent"]}%'
                    + (
                        f'，可用于对应统计{details["eligible_coverage_percent"]}%'
                        if details["eligible_coverage_percent"] != details["coverage_percent"]
                        else ""
                    )
                )
                for details in time_field_coverage.values()
            )
            boundaries = [
                {
                    "label": "来源分层",
                    "value": (
                        f"{source_breakdown_text}；指标统计仅使用真实检测结果="
                        f'{source_breakdown.get("真实检测结果", 0)}'
                    ),
                },
                {
                    "label": "扩展性实验边界",
                    "value": "离线按钮只检查发布包运行时加载；真实授权渠道需创建来源任务并查看 run_evidence。",
                },
                {
                    "label": "时间字段边界",
                    "value": f"{time_boundary_text}；未知时间不使用检测时间代替",
                },
                {
                    "label": "凭据确认边界",
                    "value": "只有来源类型为真实检测结果且有公开位置和证据的记录，才允许确认公开泄露。",
                },
            ]
            return {
                "source": "真实数据：规范化后端 SQLite（按来源类型分层统计）",
                "implementation_status": "部分实现",
                "error": None,
                "counts": {
                    "candidate_count": len(real),
                    "real_candidate_count": len(real),
                    "real_credential_count": len({
                        int(row["credential_id"])
                        for row in real
                        if row["credential_id"] is not None
                    }),
                    "credential_type_count": len(supported_rule_types),
                    "supported_credential_type_count": len(supported_rule_types),
                    "real_credential_type_count": len(observed_credential_types),
                    "observed_credential_type_count": len(observed_credential_types),
                    "configured_channel_type_count": configured_channel_type_count,
                    "configured_platform_count": configured_platform_count,
                    "observed_channel_type_count": len(channel_types),
                    "observed_platform_count": len(platforms),
                    "channel_type_count": configured_channel_type_count,
                    "platform_count": configured_platform_count,
                    "coverage_channel_type_count": configured_channel_type_count,
                    "coverage_platform_count": configured_platform_count,
                    "monitor_object_count": len(objects),
                    "unique_fingerprint_count": len(unique_fingerprints),
                    "confirmed_public_count": confirmed_public_count,
                    "all_stored_credential_count": int(connection.execute(
                        "SELECT COUNT(*) AS n FROM credentials"
                    ).fetchone()["n"]),
                    "all_stored_occurrences": len(rows),
                    "source_kind_breakdown": source_breakdown,
                },
                "timeliness": {
                    **time_stats,
                    "unknown_public_time_excluded": unknown_counts["published_at"],
                    "public_time_eligible_count": int(eligible_counts["published_at"]),
                    "public_time_ineligible_count": int(ineligible_counts["published_at"]),
                    "public_time_coverage_percent": round(
                        int(eligible_counts["published_at"]) / len(real) * 100, 1
                    ) if real else 0.0,
                    "unknown_time_counts": unknown_counts,
                    "ineligible_time_counts": ineligible_counts,
                    "eligible_time_counts": eligible_counts,
                    "timezone": "UTC",
                    "field_sources": {
                        "published_at": "source_contents.published_at；缺失保持未知",
                        "first_observed_at": "occurrences.first_seen_at",
                        "first_discovered_at": "occurrences.first_discovered_at；系统首次实际识别凭据",
                        "fetched_at": "source_contents.fetched_at，仅连接器明确提供时纳入",
                        "detected_at": "occurrences.detected_at",
                        "alerted_at": "notification_outbox.created_at",
                        "manual_confirmed_at": "review_logs.created_at(action=confirm)",
                    },
                    "note": "公开至首次发现与内容时间至检出分开统计；公开时间未知、采集时间由旧适配器推断、人工未确认的样本不进入对应精确延迟统计。",
                },
                "timeliness_by_source_kind": timeliness_by_source_kind,
                "time_field_coverage": time_field_coverage,
                "time_samples": time_samples,
                "time_model": {
                    "timezone": "UTC",
                    "field_sources": {
                        "published_at": "source_contents.published_at + published_source",
                        "first_observed_at": "occurrences.first_seen_at",
                        "first_discovered_at": "occurrences.first_discovered_at；系统首次实际识别凭据",
                        "fetched_at": "source_contents.fetched_at",
                        "detected_at": "occurrences.detected_at",
                        "alerted_at": "notification_outbox.created_at",
                        "public_confirmed_at": "review_logs.created_at(action=confirm)",
                    },
                    "unknown_counts": unknown_counts,
                    "eligible_counts": eligible_counts,
                    "ineligible_counts": ineligible_counts,
                    "field_coverage": time_field_coverage,
                    "sample_count": len(time_samples),
                    "note": "字段缺失保持为未记录；公开时间明确时计算公开至首次发现，只有参考时间时单独计算内容时间至检出。",
                },
                "evidence": {
                    "counting_rule": "候选数按真实检测 occurrence；唯一凭据按带密钥 HMAC 指纹去重；确认公开数必须有真实来源 occurrence 的 confirm 反馈。",
                    "real_data_only": True,
                    "authorized_or_controlled_data_excluded": True,
                    "unknown_time_counts": unknown_counts,
                    "official_metric_definition": "待主办方确认；报告保留明细以便重算。",
                },
                "experiments": experiments,
                "extensibility": extensibility_rows,
                "evaluation_mapping": evaluation_mapping,
                "boundaries": boundaries,
                "support_matrix_summary": {
                    "channel_rows": sum(item["item_kind"] == "channel" for item in self.list_support_matrix()),
                    "credential_rows": sum(item["item_kind"] == "credential" for item in self.list_support_matrix()),
                },
            }

        return self._execute(read)

    def export_findings(self, *, masked: bool = True, **filters: Any) -> list[dict[str, Any]]:
        data = self.list_findings(page=1, page_size=100)
        rows = data["items"]
        if not masked:
            def reveal_row(item: dict[str, Any]) -> dict[str, Any]:
                detail = self.finding_detail(int(item["id"]))
                return {**item, "secret": None if not detail else "未提供原文恢复接口"}
            rows = [reveal_row(item) for item in rows]
        return rows

    def export_findings_csv(self, *, masked: bool = True, **filters: Any) -> str:
        rows = self.export_findings(masked=masked, **filters)
        fields = [
            "id", "type", "vendor", "confidence", "preview", "review_status",
            "verification_status", "fingerprint", "secret_available",
            "first_seen_at", "last_seen_at", "occurrence_count",
        ]
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            safe = {field: row.get(field) for field in fields}
            for field, value in safe.items():
                if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
                    safe[field] = "'" + value
            writer.writerow(safe)
        return output.getvalue()
