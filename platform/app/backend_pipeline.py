"""统一扫描流水线：来源/任务 -> 连接器 -> 脱敏事件 -> 规范化后端入库。"""

from __future__ import annotations

from pathlib import Path
import os
from typing import Any, Callable

from .backend_store import BackendStore, BackendValidationError
from .config import ROOT, settings
from .connectors import AuthorizedJsonlConnector
from .controlled_detector import detect_controlled_fixtures
from .github_connector import GitHubAuthorizedConnector, GitHubConnectorConfig
from .gitee_connector import GiteeAuthorizedConnector, GiteeConnectorConfig
from .local_scan import scan_local_directory
from .connector_runs import ConnectorRunStore
from .public_connectors import PublicConnectorError, collect_extended
from .settings_service import effective_settings
from .time_utils import utc_now_iso


def backend_path() -> Path:
    return Path(os.environ.get("BACKEND_DB_PATH", str(ROOT / "data" / "backend.sqlite3")))


def backend_store() -> BackendStore:
    return BackendStore(
        backend_path(),
        master_key_path=backend_path().parent / "backend.master.key",
    )


def _active_settings():
    return effective_settings(settings)


def format_detection_note(stats: dict[str, Any]) -> str:
    """Create a user-facing result summary from the persisted run counters."""
    candidates = max(0, int(stats.get("candidates", 0) or 0))
    inserted = max(0, int(stats.get("new_occurrences", 0) or 0))
    filtered = max(0, int(stats.get("skipped_occurrences", 0) or 0))
    items_seen = max(0, int(stats.get("items_seen", 0) or 0))
    if candidates:
        if filtered:
            return (
                f"本批次检测命中 {candidates} 条候选，其中 {filtered} 条与历史记录重复，"
                f"实际新增 {inserted} 条"
            )
        if inserted == candidates:
            return f"本批次检测命中 {candidates} 条候选，已全部写入"
        return f"本批次检测命中 {candidates} 条候选，实际新增 {inserted} 条"
    if items_seen:
        return f"本批次已扫描 {items_seen} 个内容项，未命中启用规则"
    return "本批次未获取到可扫描内容，未形成候选"


def _collect(
    source: dict[str, Any],
    *,
    checkpoint_store: ConnectorRunStore | None = None,
    finalizers: list[Callable[[], None]] | None = None,
    enabled_rule_ids: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    runtime = _active_settings()
    config = source.get("config") or {}
    hmac_key = runtime.credential_hmac_key
    if not hmac_key:
        raise BackendValidationError("credential_hmac_key_required")
    key = source["collector_key"]
    if key == "local_dir":
        root = Path(str(config.get("path") or "")).expanduser()
        events = scan_local_directory(
            root,
            hmac_key=hmac_key,
            max_file_bytes=int(config.get("max_file_bytes") or runtime.connector_max_file_bytes),
            max_files=int(config.get("max_files") or runtime.connector_max_files),
            include_internal=True,
            enabled_rule_ids=enabled_rule_ids,
        )
        fetched_at = utc_now_iso()
        for event in events:
            event.setdefault("_fetched_at", fetched_at)
        return events
    if key == "controlled_fixtures":
        events = detect_controlled_fixtures(
            Path(str(config.get("path") or "")).expanduser(),
            hmac_key=hmac_key,
            include_internal=True,
            enabled_rule_ids=enabled_rule_ids,
        )
        fetched_at = utc_now_iso()
        for event in events:
            event.setdefault("_fetched_at", fetched_at)
        return events
    if key == "authorized_jsonl":
        return AuthorizedJsonlConnector(
            Path(str(config.get("path") or "")).expanduser(),
            hmac_key=hmac_key,
            include_internal=True,
        ).collect()
    if key == "github":
        repositories = tuple(config.get("repositories") or runtime.github_repositories)
        if set(repositories) - set(runtime.github_repositories):
            raise BackendValidationError("github_repository_outside_allowlist")
        connector = GitHubAuthorizedConnector(
            GitHubConnectorConfig(
                enabled=runtime.connector_network_enabled,
                token=runtime.github_token,
                repositories=repositories,
                branch=config.get("branch"),
                api_base=runtime.github_api_base,
                timeout_seconds=runtime.connector_timeout_seconds,
                max_files=runtime.connector_max_files,
                max_file_bytes=runtime.connector_max_file_bytes,
                min_interval_seconds=runtime.connector_min_interval_seconds,
                max_retries=runtime.connector_max_retries,
                hmac_key=hmac_key,
                include_internal=True,
                checkpoint_store=checkpoint_store,
                defer_checkpoints=True,
                enabled_rule_ids=enabled_rule_ids,
            )
        )
        if finalizers is not None:
            finalizers.append(connector.commit_checkpoints)
        return connector.collect()
    if key == "gitee":
        repositories = tuple(config.get("repositories") or runtime.gitee_repositories)
        if set(repositories) - set(runtime.gitee_repositories):
            raise BackendValidationError("gitee_repository_outside_allowlist")
        connector = GiteeAuthorizedConnector(
            GiteeConnectorConfig(
                enabled=runtime.connector_network_enabled,
                token=runtime.gitee_token,
                repositories=repositories,
                branch=config.get("branch"),
                api_base=runtime.gitee_api_base,
                timeout_seconds=runtime.connector_timeout_seconds,
                max_files=runtime.connector_max_files,
                max_file_bytes=runtime.connector_max_file_bytes,
                min_interval_seconds=runtime.connector_min_interval_seconds,
                max_retries=runtime.connector_max_retries,
                hmac_key=hmac_key,
                include_internal=True,
                checkpoint_store=checkpoint_store,
                defer_checkpoints=True,
                enabled_rule_ids=enabled_rule_ids,
            )
        )
        if finalizers is not None:
            finalizers.append(connector.commit_checkpoints)
        return connector.collect()
    if key in {
        "mediawiki",
        "generic_web",
        "stackoverflow",
        "codeberg",
        "weibo_authorized_api",
        "oci_registry",
        "npm_registry",
        "pypi_registry",
        "huggingface_repo",
        "crates_registry",
        "nuget_registry",
        "rubygems_registry",
        "apk",
        "wxapkg",
    }:
        try:
            pending_checkpoints: list[tuple[str, str]] = []
            if finalizers is not None and checkpoint_store is not None:
                finalizers.append(
                    lambda: [
                        checkpoint_store.set_checkpoint(target, value)
                        for target, value in pending_checkpoints
                    ]
                )
            return collect_extended(
                key,
                config,
                hmac_key=hmac_key,
                access_token=runtime.weibo_access_token,
                network_enabled=runtime.connector_network_enabled,
                timeout_seconds=runtime.connector_timeout_seconds,
                max_file_bytes=runtime.connector_max_file_bytes,
                checkpoint_store=checkpoint_store,
                defer_checkpoints=finalizers is not None,
                pending_checkpoints=pending_checkpoints,
                enabled_rule_ids=enabled_rule_ids,
            )
        except PublicConnectorError as error:
            raise BackendValidationError(str(error)) from None
    raise BackendValidationError("collector_not_supported")


def run_task(task_id: int, trigger: str = "manual") -> dict[str, Any]:
    from .release_policy import require_available_task
    require_available_task(task_id)
    store = backend_store()
    store.initialize()
    run_store = ConnectorRunStore(_active_settings().connector_runs_path)
    task = store.get_task(task_id)
    if task is None:
        raise BackendValidationError("task_not_found")
    source = store.get_source(int(task["source_id"]))
    if source is None or not source["enabled"]:
        raise BackendValidationError("source_disabled")
    run = store.create_run(task_id, trigger)
    stats: dict[str, Any] = {
        "items_seen": 0,
        "content_items_seen": 0,
        "content_items_written": 0,
        "content_items_existing": 0,
        "candidates": 0,
        "new_credentials": 0,
        "new_occurrences": 0,
        "skipped_occurrences": 0,
        "stop_reason": "completed",
    }
    finalizers: list[Callable[[], None]] = []
    try:
        enabled_rule_ids = frozenset(
            str(rule["id"])
            for rule in store.list_rules()
            if bool(rule.get("enabled"))
        )
        events = _collect(
            source,
            checkpoint_store=run_store,
            finalizers=finalizers,
            enabled_rule_ids=enabled_rule_ids,
        )
        stats["items_seen"] = int(getattr(events, "items_seen", len(events)))
        content_items = list(getattr(events, "content_items", []) or [])
        if content_items:
            stats.update(
                store.ingest_content_items(
                    int(source["id"]),
                    int(run["id"]),
                    content_items,
                )
            )
        ingested = store.ingest_events(int(source["id"]), int(run["id"]), events)
        stats.update(ingested)
        stats["detection_note"] = format_detection_note(stats)
        for finalize in finalizers:
            finalize()
        store.finish_run(int(run["id"]), "completed", stats)
        store.record_run_evidence(
            int(run["id"]),
            events,
            stats,
            status="completed",
        )
        # Refresh the persisted channel/rule matrix immediately so a completed
        # local run is visible without restarting the service.
        store.sync_rules_and_capabilities()
        finished_run = next(
            item for item in store.list_runs(task_id=int(task["id"]), limit=10)
            if int(item["id"]) == int(run["id"])
        )
        return {
            "ok": True,
            "run": {
                **run,
                "status": "completed",
                "finished_at": finished_run.get("finished_at"),
                "stats": stats,
            },
            "stats": stats,
            "source_kind_counts": _source_counts(events),
        }
    except Exception as exc:
        stats["stop_reason"] = "failed"
        error_code = _safe_error_code(exc)
        stats["error_code"] = error_code
        store.finish_run(int(run["id"]), "failed", stats, error=error_code)
        store.record_run_evidence(
            int(run["id"]),
            [],
            stats,
            status="failed",
            error_code=error_code,
        )
        raise


def _source_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for event in events:
        key = str(event.get("source_kind") or "未知")
        result[key] = result.get(key, 0) + 1
    return result


def _safe_error_code(error: Exception) -> str:
    """Keep audit output useful without persisting validation input details."""
    message = str(error)
    if "only single-line masked previews are accepted" in message:
        return "event_contract_invalid_masked_preview"
    if "EventRecord" in message:
        return "event_contract_invalid"
    return message[:120] or error.__class__.__name__
