import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings
from .settings_service import effective_settings
from .alert_store import AlertStore
from .event_store import EventStore, StorageUnavailable
from .connector_runs import ConnectorRunStore
from .controlled_detector import detect_controlled_fixtures
from .connectors import AuthorizedJsonlConnector
from .github_connector import GitHubAuthorizedConnector, GitHubConnectorConfig
from .gitee_connector import GiteeAuthorizedConnector, GiteeConnectorConfig
from .models import (
    AlertsResponse,
    ConnectorRunsResponse,
    EventDetailResponse,
    EventResponse,
    FeedbackRequest,
    FeedbackResponse,
    MonitoringTaskResponse,
    MonitoringTaskRequest,
)
from .monitoring_store import MonitoringStore
from .evidence_export import build_evidence_package
from .connector_scheduler import ConnectorBusyError, ConnectorScheduler, ScheduledConnectorTask
from .connector_runs import ConnectorRunStore
from .local_scan import LocalScanError, scan_local_directory
from .backend_pipeline import backend_store, run_task
from .backend_store import BackendStorageError, BackendValidationError
from .time_utils import parse_timestamp
from .release_policy import visible_tasks, notice


ROOT = Path(__file__).resolve().parent.parent
MOCK_DIR = ROOT / "mock"


def _active_settings():
    return effective_settings(settings)


def _time_source_label(value: str | None) -> str:
    labels = {
        "not_provided": "来源未提供",
        "connector_not_provided": "来源未提供",
        "npm_registry_version_time": "npm 版本发布时间",
        "stackoverflow_creation_date": "来源页面时间",
        "stackoverflow_answer_creation_date": "来源内容时间",
        "rss_published": "RSS/Atom 发布时间",
        "sitemap_lastmod": "Sitemap 最后修改时间（仅参考）",
        "github_commit": "GitHub 匹配版本提交时间",
        "gitee_commit": "Gitee 匹配版本提交时间",
        "pypi_upload_time": "PyPI 分发文件上传时间（仅参考）",
        "weibo_page_metadata": "微博页面元数据时间（仅参考）",
        "image_config_created": "镜像配置创建时间（仅参考）",
        "filesystem_mtime": "本地文件修改时间（仅参考）",
    }
    return labels.get(str(value or "").strip(), str(value or "").strip() or "来源未提供")


def _sort_monitoring_tasks_by_last_run(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Show the most recently executed task first; never-run tasks stay last."""
    minimum = datetime.min.replace(tzinfo=timezone.utc)

    def sort_key(item: dict[str, Any]) -> tuple[bool, datetime]:
        timestamp = parse_timestamp(item.get("last_run"))
        return (timestamp is not None, timestamp or minimum)

    return sorted(items, key=sort_key, reverse=True)


def _use_unified_backend(runtime) -> bool:
    """Use the normalized store for the default product runtime.

    Isolated tests and explicit legacy database paths retain the old contract,
    which keeps failure-boundary tests meaningful while the running product
    pages share one source of truth.
    """
    try:
        return (
            runtime.events_source == "sqlite"
            and runtime.database_path.resolve() == (ROOT / "data" / "events.sqlite3").resolve()
            and backend_store().path.exists()
        )
    except (OSError, AttributeError):
        return False


def _use_unified_monitoring_backend(runtime) -> bool:
    try:
        return (
            runtime.monitoring_source == "sqlite"
            and runtime.monitoring_database_path.resolve() == (ROOT / "data" / "monitoring.sqlite3").resolve()
            and backend_store().path.exists()
        )
    except (OSError, AttributeError):
        return False


def _enabled_rule_ids() -> frozenset[str]:
    """Read the shared rule switch; runtime failures must fail closed."""
    try:
        store = backend_store()
        store.initialize()
        return frozenset(
            str(rule["id"])
            for rule in store.list_rules()
            if bool(rule.get("enabled"))
        )
    except Exception as error:
        raise RuntimeError("rule_configuration_unavailable") from error


def _load_mock(name: str) -> dict[str, Any]:
    path = MOCK_DIR / f"{name}.json"
    if path.is_file():
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    else:
        # Clean releases may omit mock files. Keep the response contract
        # available without recreating mock or fixture business records.
        payload = {
            "schema_version": "1.0",
            "page_title": name,
            "source": "SQLite 发布快照",
            "implementation_status": "已实现",
            "error": None,
        }
        if name == "overview":
            payload.update(
                metrics=[],
                channel_summary=[],
                credential_summary=[],
                recent_events=[],
                latency_summary=[],
            )
        elif name == "monitoring_tasks":
            payload.update(filters=[], summary=[], items=[], runs=[])
        elif name == "events":
            payload.update(
                filters=[],
                summary={
                    "candidate_count": 0,
                    "classified_count": 0,
                    "public_exposure_confirmed_count": 0,
                    "unique_credential_count": 0,
                    "event_count": 0,
                },
                items=[],
                pagination={
                    "page": 1,
                    "page_size": 20,
                    "total_items": 0,
                    "total_pages": 0,
                    "sort_by": "event_id",
                    "sort_order": "asc",
                },
            )
        elif name == "event_detail":
            payload.update(event=None, timeline=[], evidence=[], rechecks=[], actions=[])
        elif name == "reports":
            payload.update(evaluation_mapping={}, counts={}, timeliness={}, extensibility={}, boundaries={})
        elif name == "implementation_status":
            payload.update(items=[], legend=[])
        elif name == "alerts":
            payload.update(items=[])
    _validate_contract(name, payload)
    return payload


def _validate_contract(name: str, payload: dict[str, Any]) -> None:
    if name == "events":
        EventResponse.model_validate(payload)
        return
    if name == "monitoring_tasks":
        MonitoringTaskResponse.model_validate(payload)
        return
    required = {"page_title", "source", "implementation_status"}
    missing = required.difference(payload)
    if missing:
        raise ValueError(f"{name} mock contract missing fields: {sorted(missing)}")
    expected_collections = {
        "overview": ("metrics", "channel_summary", "credential_summary", "recent_events", "latency_summary"),
        "monitoring_tasks": ("filters", "summary", "items"),
        "events": ("filters", "summary", "items"),
        "event_detail": ("event", "timeline", "evidence", "rechecks", "actions"),
        "reports": ("evaluation_mapping", "counts", "timeliness", "extensibility", "boundaries"),
        "implementation_status": ("items", "legend"),
        "alerts": ("items",),
    }
    for field in expected_collections.get(name, ()):
        if field not in payload:
            raise ValueError(f"{name} mock contract missing field: {field}")
    if name in {"monitoring_tasks", "events", "implementation_status"} and not isinstance(payload["items"], list):
        raise TypeError(f"{name}.items must be a list")
    if name == "events":
        for item in payload["items"]:
            for field in ("event_id", "channel", "platform", "credential_type", "redacted_value", "evidence_level"):
                if field not in item:
                    raise ValueError(f"events item missing field: {field}")


def _real_unimplemented(name: str) -> dict[str, Any]:
    payload = _load_mock(name)
    payload["source"] = "真实数据源未实现"
    payload["implementation_status"] = "未实现"
    payload["notice"] = "DATA_SOURCE=real 已识别，但真实后端能力尚未接入。"
    return payload


def get_data(name: str, **kwargs: Any) -> dict[str, Any]:
    runtime = _active_settings()
    if name == "events":
        return get_events(**kwargs)
    if name == "monitoring_tasks":
        return get_monitoring_tasks(**kwargs)
    if name == "implementation_status":
        return get_implementation_status()
    if name == "reports":
        return get_reports()
    if name == "alerts":
        return get_alerts()
    if name == "overview":
        return get_overview()
    if runtime.data_source == "mock":
        return _load_mock(name)
    return _real_unimplemented(name)


def get_overview() -> dict[str, Any]:
    runtime = _active_settings()
    payload = _load_mock("overview")
    if runtime.events_source != "sqlite" or runtime.monitoring_source != "sqlite":
        return payload
    if _use_unified_backend(runtime):
        try:
            stats = backend_store().overview_stats()
            report = backend_store().evaluation_report()
            payload["source"] = "真实数据：规范化后端 SQLite"
            payload["implementation_status"] = "部分实现"
            payload["error"] = None
            payload["metrics"] = [
                {
                    "label": "真实检测凭据实体",
                    "value": stats["real_credentials_total"],
                    "hint": "真实检测结果中的 HMAC 指纹实体；受控/离线数据单独保留",
                },
                {
                    "label": "真实检测出现位置",
                    "value": stats["real_occurrences_total"],
                    "hint": "仅统计 source_kind=真实检测结果 的 occurrences",
                },
                {"label": "通知待处理", "value": stats["notification_queue"], "hint": "offline-sqlite 队列；不代表外部发送成功"},
                {
                    "label": "公开至检测 P95",
                    "value": (
                        "未知"
                        if report["timeliness"]["published_to_detected"]["p95_seconds"] is None
                        else f'{report["timeliness"]["published_to_detected"]["p95_seconds"]}s'
                    ),
                    "hint": "统计按 UTC；界面时间统一显示北京时间",
                },
            ]
            payload["channel_summary"] = [
                {"channel": channel, "platforms": 0, "objects": count}
                for channel, count in stats.get("by_channel", [])
            ]
            payload["coverage"] = stats.get("coverage", {})
            payload["credential_type_total"] = stats.get("credential_type_count", 0)
            payload["observed_channel_type_count"] = stats.get("coverage", {}).get(
                "observed_channel_type_count", 0
            )
            payload["observed_platform_count"] = stats.get("coverage", {}).get(
                "observed_platform_count", 0
            )
            payload["credential_summary"] = [
                {"type": item[0], "count": item[1]} for item in stats.get("by_type_top", [])
            ]
            records = backend_store().list_event_records(
                source_kind="全部来源",
                page=1,
                page_size=8,
                sort_by="first_observed_at",
                sort_order="desc",
            )
            payload["recent_events"] = [
                {
                    key: item.get(key)
                    for key in (
                        "event_id", "channel", "credential_type", "risk_level",
                        "disposition", "first_observed_at",
                    )
                }
                for item in records["items"]
            ]
            payload["latency_summary"] = [
                {
                    "label": "公开至首次观测",
                    "value": _format_seconds(
                        report["timeliness"]["published_to_observed"]["p50_seconds"]
                    ),
                    "note": "公开时间未知不计入；首次观测来自 occurrence.first_seen_at",
                },
                {
                    "label": "采集至检测",
                    "value": _format_seconds(
                        report["timeliness"]["fetched_to_detected"]["p50_seconds"]
                    ),
                    "note": "仅连接器明确提供 fetched_at 的记录进入",
                },
                {
                    "label": "检测至告警",
                    "value": _format_seconds(
                        report["timeliness"]["detected_to_alert"]["p50_seconds"]
                    ),
                    "note": "本地离线队列入队时间，不等同外部通知送达",
                },
            ]
            return payload
        except (BackendStorageError, BackendValidationError):
            payload["source"] = "真实数据源：规范化后端 SQLite 读取失败"
            payload["implementation_status"] = "部分实现"
            payload["error"] = "总览读取失败；未使用旧库或桩数据替代。"
            return payload
    try:
        events = EventStore(runtime.database_path, runtime.database_timeout, runtime.database_attempts).list_events()
        tasks = MonitoringStore(
            runtime.monitoring_database_path,
            runtime.database_timeout,
            runtime.database_attempts,
        ).list_tasks()
    except StorageUnavailable:
        payload["source"] = "真实数据源：SQLite 读取失败"
        payload["implementation_status"] = "部分实现"
        payload["error"] = "总览聚合读取失败；未使用桩数据替代。"
        return payload
    channels = {}
    credential_counts = {}
    for item in events:
        channels.setdefault(item["channel"], {"platforms": set(), "objects": set()})
        channels[item["channel"]]["platforms"].add(item["platform"])
        channels[item["channel"]]["objects"].add(item.get("source_locator") or item["event_id"])
        credential_counts[item["credential_type"]] = credential_counts.get(item["credential_type"], 0) + 1
    recent = sorted(events, key=lambda item: item.get("first_observed_at") or "", reverse=True)[:8]
    payload["source"] = "真实数据：SQLite 事件库与监控任务库"
    payload["implementation_status"] = "部分实现"
    payload["error"] = None
    payload["metrics"] = [
        {"label": "监控任务", "value": len(tasks), "hint": "SQLite配置记录；不代表调度器常驻运行"},
        {"label": "渠道类型", "value": len(channels), "hint": "按事件记录中的渠道标签统计"},
        {"label": "唯一凭据", "value": len({item.get("credential_fingerprint") for item in events if item.get("credential_fingerprint")}), "hint": "带密钥HMAC指纹去重"},
        {"label": "P50 自动发现", "value": "未知", "hint": "缺少公开时间字段，不生成估计值"},
    ]
    payload["channel_summary"] = [
        {"channel": channel, "platforms": len(value["platforms"]), "objects": len(value["objects"])}
        for channel, value in sorted(channels.items())
    ]
    payload["credential_summary"] = [
        {"type": key, "count": value} for key, value in sorted(credential_counts.items())
    ]
    payload["recent_events"] = [
        {key: item.get(key) for key in ("event_id", "channel", "credential_type", "risk_level", "disposition", "first_observed_at")}
        for item in recent
    ]
    payload["latency_summary"] = [
        {"label": "公开至首次观测", "value": "未知", "note": "事件记录未提供公开时间"},
        {"label": "采集至检测", "value": "未知", "note": "采集时间未单独记录"},
        {"label": "检测至告警", "value": "见响应审计", "note": "不把生成时间当作公开时间"},
    ]
    return payload


def _format_seconds(value: float | None) -> str:
    if value is None:
        return "未知"
    if value < 0:
        return "异常时间顺序"
    rounded = max(0, round(value))
    if rounded < 60:
        return f"{rounded}秒"
    minutes = rounded // 60
    seconds = rounded % 60
    if minutes < 60:
        return f"{minutes}分{seconds}秒" if seconds else f"{minutes}分"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    if hours < 24:
        return f"{hours}小时{remaining_minutes}分" if remaining_minutes else f"{hours}小时"
    days = hours // 24
    remaining_hours = hours % 24
    if days < 365:
        return f"{days}天{remaining_hours}小时" if remaining_hours else f"{days}天"
    years = days // 365
    remaining_days = days % 365
    return f"{years}年{remaining_days}天" if remaining_days else f"{years}年"


def _time_reason_label(value: str | None) -> str:
    labels = {
        "public_time_not_provided": "来源未提供公开时间",
        "connector_not_provided": "来源未提供公开时间",
        "public_time_is_reference_only": "已有时间只能说明内容参考时间",
        "first_discovery_time_not_saved": "检测记录没有保存系统首次发现时间",
        "historical_first_discovery_time_unverified": "只有历史数值，缺少首次检测过程证据",
        "detection_time_not_saved": "检测完成时间未保存",
        "detection_time_evidence_unverified": "检测完成时间缺少过程证据",
        "timestamp_invalid_or_timezone_missing": "时间格式或时区无法核验",
        "discovery_before_public_time": "首次发现早于公开时间，时间证据顺序异常",
        "detection_before_reference_time": "检测时间早于参考时间，时间证据顺序异常",
    }
    raw = str(value or "").strip()
    return labels.get(raw, raw or "缺少可比的时间证据")


def _event_query_values(
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
    allowed_sort = {"event_id", "first_observed_at", "risk_level", "credential_type"}
    if page < 1 or page_size not in {5, 10, 20, 50, 100}:
        raise ValueError("invalid_pagination")
    if sort_by not in allowed_sort or sort_order not in {"asc", "desc"}:
        raise ValueError("invalid_sort")
    return {
        "channel": channel.strip(),
        "credential_type": credential_type.strip(),
        "risk_level": risk_level.strip(),
        "disposition": disposition.strip(),
        "source_kind": source_kind.strip(),
        "page": page,
        "page_size": page_size,
        "sort_by": sort_by,
        "sort_order": sort_order,
    }


def _filter_events(items: list[dict], query: dict[str, Any]) -> list[dict]:
    result = [
        item for item in items
        if (not query["channel"] or item["channel"] == query["channel"])
        and (not query["credential_type"] or item["credential_type"] == query["credential_type"])
        and (not query["risk_level"] or item["risk_level"] == query["risk_level"])
        and (not query["disposition"] or item["disposition"] == query["disposition"])
        and (not query["source_kind"] or item["source_kind"] == query["source_kind"])
    ]
    reverse = query["sort_order"] == "desc"
    if query["sort_by"] == "risk_level":
        risk_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unknown": 4}
        result.sort(key=lambda item: risk_order.get(item.get("risk_level"), 99), reverse=reverse)
    else:
        result.sort(key=lambda item: item.get(query["sort_by"]) or "", reverse=reverse)
    # Apply this as a stable second sort so the priority survives every page.
    result.sort(key=lambda item: 0 if item.get("source_kind") == "真实检测结果" else 1)
    return result


def _event_filters(payload: dict[str, Any], items: list[dict]) -> None:
    option_map = {
        "channel": sorted({item["channel"] for item in items}),
        "credential_type": sorted({item["credential_type"] for item in items}),
        "risk_level": ["critical", "high", "medium", "low", "unknown"],
        "disposition": sorted({item["disposition"] for item in items}),
        "source_kind": sorted({item["source_kind"] for item in items}),
    }
    defaults = {
        "channel": "全部渠道", "credential_type": "全部类型",
        "risk_level": "全部风险", "disposition": "全部状态",
        "source_kind": "全部来源",
    }
    labels = {
        "channel": "渠道类型", "credential_type": "凭据类型",
        "risk_level": "风险档", "disposition": "处置状态",
        "source_kind": "数据来源",
    }
    payload["filters"] = [
        {"key": key, "label": labels[key], "default": defaults[key], "options": option_map[key]}
        for key in labels
    ]


def _apply_event_query(payload: dict[str, Any], all_items: list[dict], query: dict[str, Any]) -> dict[str, Any]:
    filtered = _filter_events(all_items, query)
    total = len(filtered)
    total_pages = (total + query["page_size"] - 1) // query["page_size"] if total else 0
    page = min(query["page"], total_pages) if total_pages else 1
    start = (page - 1) * query["page_size"]
    payload["items"] = filtered[start:start + query["page_size"]]
    payload["pagination"] = {
        "page": page,
        "page_size": query["page_size"],
        "total_items": total,
        "total_pages": total_pages,
        "sort_by": query["sort_by"],
        "sort_order": query["sort_order"],
    }
    payload["summary"]["event_count"] = total
    payload["summary"]["candidate_count"] = total
    payload["summary"]["classified_count"] = sum(
        item["evidence_level"] in {"L1", "L2", "L3", "L4"} for item in filtered
    )
    payload["summary"]["public_exposure_confirmed_count"] = sum(
        item["disposition"] == "已确认公开泄露" for item in filtered
    )
    fingerprints = {
        item["credential_fingerprint"] for item in filtered if item.get("credential_fingerprint")
    }
    payload["summary"]["unique_credential_count"] = len(fingerprints)
    return payload


def get_events(**kwargs: Any) -> dict[str, Any]:
    runtime = _active_settings()
    payload = _load_mock("events")
    try:
        query = _event_query_values(**kwargs)
    except ValueError:
        payload["error"] = "筛选、分页或排序参数无效。"
        payload["items"] = []
        payload["pagination"] = {
            "page": 1, "page_size": 20, "total_items": 0, "total_pages": 0,
            "sort_by": "event_id", "sort_order": "asc",
        }
        return EventResponse.model_validate(payload).model_dump()
    if not query["source_kind"]:
        query["source_kind"] = "真实检测结果"
    if runtime.events_source == "mock":
        _event_filters(payload, payload["items"])
        return EventResponse.model_validate(_apply_event_query(payload, payload["items"], query)).model_dump()
    if _use_unified_backend(runtime):
        try:
            result = backend_store().list_event_records(**query)
            return EventResponse.model_validate(result).model_dump()
        except (BackendStorageError, BackendValidationError):
            payload["source"] = "真实数据源：规范化后端 SQLite 读取失败"
            payload["implementation_status"] = "部分实现"
            payload["error"] = "统一事件存储读取失败；未使用旧库或桩数据替代。"
            payload["items"] = []
            payload["pagination"] = {
                "page": query["page"], "page_size": query["page_size"],
                "total_items": 0, "total_pages": 0,
                "sort_by": query["sort_by"], "sort_order": query["sort_order"],
            }
            payload["summary"] = {
                "candidate_count": None, "classified_count": None,
                "public_exposure_confirmed_count": None,
                "unique_credential_count": None, "event_count": 0,
            }
            return EventResponse.model_validate(payload).model_dump()
    payload["items"] = []
    payload["pagination"] = {
        "page": query["page"], "page_size": query["page_size"], "total_items": 0,
        "total_pages": 0, "sort_by": query["sort_by"], "sort_order": query["sort_order"],
    }
    payload["summary"] = {
        "candidate_count": None,
        "classified_count": None,
        "public_exposure_confirmed_count": None,
        "unique_credential_count": None,
        "event_count": 0,
    }
    payload["source"] = "真实数据源：SQLite（含合成样例与受控投放检出）"
    payload["implementation_status"] = "部分实现"
    if runtime.events_source != "sqlite":
        payload["error"] = "数据源配置无效；请设置 EVENTS_SOURCE=sqlite 或 mock。"
    else:
        store = EventStore(runtime.database_path, runtime.database_timeout, runtime.database_attempts)
        try:
            all_items = store.list_events()
            _event_filters(payload, all_items)
            _apply_event_query(payload, all_items, query)
        except StorageUnavailable:
            payload["source"] = "真实数据源：SQLite 读取失败"
            payload["error"] = "数据库读取失败；未使用桩数据替代。检查数据库路径、初始化与占用情况，或切回 mock。"
    return EventResponse.model_validate(payload).model_dump()


def get_implementation_status() -> dict[str, Any]:
    payload = _load_mock("implementation_status")
    payload["source"] = "真实数据：模块配置 / 计划项"
    payload["implementation_status"] = "部分实现"
    for item in payload["items"]:
        if item["module"] == "事件列表":
            item["status"] = "部分实现：存储、读取、筛选、分页、排序和处置反馈"
            item["evidence"] = "app/data_access.py；app/backend_store.py；docs/stage14-verification.md"
            item["dependencies"] = "SQLite、EVENTS_SOURCE；数据内容保留原来源标签"
        elif item["module"] == "事件详情":
            item["status"] = "部分实现：SQLite查询、告警和反馈审计"
            item["evidence"] = "app/main.py；app/backend_store.py；docs/stage12-verification.md"
            item["dependencies"] = "事件主键、SQLite、EventDetailResponse"
        elif item["module"] == "统一数据访问层":
            item["status"] = "部分实现：事件严格契约、模块级开关和统一后端 REST"
            item["evidence"] = "EventResponse 模型；mock/sqlite 同形响应；/api/sources、/api/tasks、/api/findings"
        elif item["module"] == "监控任务":
            item["status"] = "部分实现：SQLite CRUD、暂停恢复和离线 dry-run"
            item["evidence"] = "app/backend_api.py；app/backend_store.py；docs/stage15-17-verification.md"
            item["dependencies"] = "SQLite、MONITORING_SOURCE、连接器运行审计；常驻调度与在线采集仍需授权"
        elif item["module"] == "受控检测与事件生成":
            item["status"] = "部分实现：本地授权夹具与目录规则检测、脱敏事件导入；公开渠道采集需授权"
            item["evidence"] = "app/controlled_detector.py；app/local_scan.py；app/content_detection.py；docs/stage5-verification.md"
            item["dependencies"] = "显式授权目录、CREDENTIAL_HMAC_KEY、EVENTS_SOURCE、SQLite；不访问外网"
        elif item["module"] == "凭据指纹与唯一关联":
            item["status"] = "部分实现：HMAC指纹和SQLite统计；密钥轮换重算未实现"
            item["evidence"] = "app/fingerprint.py；app/backend_store.py；docs/stage6-contract.md"
            item["dependencies"] = "CREDENTIAL_HMAC_KEY；旧记录无指纹时不纳入唯一数"
        elif item["module"] == "公开渠道连接器与离线授权导入":
            item["status"] = "已实现代码；离线授权导入与多类公开连接器均已接入统一后端"
            item["evidence"] = "app/connectors.py；app/public_connectors.py；app/backend_pipeline.py"
            item["dependencies"] = "CREDENTIAL_HMAC_KEY；网络连接器需显式联网，APK/wxapkg 本地合法载体可离线"
        elif item["module"] == "真实授权平台连接器与调度":
            item["status"] = "部分实现：GitHub/Gitee授权 API + Wiki/RSS/Stack Overflow/OCI公开采集；默认关闭网络"
            item["evidence"] = "app/github_connector.py；app/gitee_connector.py；app/public_connectors.py"
            item["dependencies"] = "GitHub/Gitee需Token和仓库白名单；公开连接器需显式联网和HTTPS公网目标；真实平台实测待授权"
        elif item["module"] == "告警与响应":
            item["status"] = "部分实现：SQLite告警队列、离线通知桩和事件反馈"
            item["evidence"] = "app/alert_store.py；app/backend_api.py；docs/stage12-verification.md"
            item["dependencies"] = "EVENTS_DB_PATH；外部通知通道未接入"
    try:
        unified = backend_store()
        unified.initialize()
        experiments = unified.list_extensibility_experiments()
        experiment_count = len(experiments)
        passed_experiment_count = sum(
            item.get("runtime_check_status") == "已通过" for item in experiments
        )
        matrix_count = len(unified.list_support_matrix())
        additions = [
            {
                "module": "规范化时间模型与指标口径",
                "backend": "公开、首次观测、采集、检测、告警、人工确认分列；未知不估算",
                "status": "部分实现：字段、来源、未知计数和分位数已接入",
                "status_color": "medium",
                "dependencies": "source_contents、occurrences、notification_outbox、review_logs",
                "evidence": "app/backend_store.py；/api/evaluation-report",
                "order": "19",
            },
            {
                "module": "通知适配层与离线重试",
                "backend": "版本化通知事件、幂等 outbox、记录/重试状态",
                "status": "部分实现：仅离线 SQLite，外部发送未接入",
                "status_color": "medium",
                "dependencies": "notification_outbox；告警反馈",
                "evidence": "app/notification_adapters.py；/api/notifications",
                "order": "20",
            },
            {
                "module": "渠道与凭据支持矩阵",
                "backend": "渠道、平台、载体、权限、合规、采集/解析/巡检和凭据规则矩阵",
                "status": f"已实现数据结构：{matrix_count} 行；真实平台实测仍按行记录",
                "status_color": "low",
                "dependencies": "ConnectorSpec、规则注册表、授权证据",
                "evidence": "support_matrix；/api/support-matrix；/api/channels",
                "order": "21",
            },
            {
                "module": "扩展性实验记录",
                "backend": "新增代码量、配置项、工时、测试时长、checkpoint、失败恢复和证据",
                "status": (
                    f"发布包运行时检查：{passed_experiment_count}/{experiment_count} 项通过；"
                    "真实平台实测待授权"
                ),
                "status_color": "low" if experiment_count and passed_experiment_count == experiment_count else "medium",
                "dependencies": "离线插件示例、实验记录接口",
                "evidence": "app/experiment_runner.py；/api/extensibility-experiments；真实平台未访问",
                "order": "22",
            },
            {
                "module": "本地角色与安全审计",
                "backend": "viewer/analyst/admin 服务端权限、回环控制面、拒绝原因和审计",
                "status": "部分实现：本地无登录模式；角色来自服务端配置，不信任请求体",
                "status_color": "medium",
                "dependencies": "APP_OPERATOR_ROLE；security_audit",
                "evidence": "app/security_policy.py；/api/security/audit",
                "order": "23",
            },
        ]
        existing = {item["module"] for item in payload["items"]}
        payload["items"].extend(item for item in additions if item["module"] not in existing)
    except Exception:
        pass
    return payload


def get_monitoring_tasks(
    channel: str = "",
    status: str = "",
    collection_method: str = "",
) -> dict[str, Any]:
    runtime = _active_settings()
    payload = _load_mock("monitoring_tasks")
    query = {
        "channel": channel.strip(),
        "status": status.strip(),
        "collection_method": collection_method.strip(),
    }

    def apply_filters(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            item for item in items
            if (not query["channel"] or item["channel"] == query["channel"])
            and (not query["status"] or item["status"] == query["status"])
            and (not query["collection_method"] or item["collection_method"] == query["collection_method"])
        ]

    if runtime.monitoring_source == "mock":
        payload["items"] = _sort_monitoring_tasks_by_last_run(apply_filters(payload["items"]))
        payload["summary"] = [
            {"label": "任务总数", "value": len(payload["items"]), "hint": "当前筛选结果"},
            {"label": "运行中", "value": sum(item["status"] == "运行中" for item in payload["items"]), "hint": "桩状态"},
            {"label": "待配置", "value": sum(item["status"] == "待配置" for item in payload["items"]), "hint": "桩状态"},
            {"label": "采集失败", "value": sum(item["status"] == "采集失败" for item in payload["items"]), "hint": "桩状态"},
        ]
        payload["runs"] = []
        return MonitoringTaskResponse.model_validate(payload).model_dump()

    if _use_unified_monitoring_backend(runtime):
        try:
            unified_tasks = visible_tasks(backend_store().list_tasks())
            payload["release_notice"] = notice()
            visible_ids = {int(task["id"]) for task in unified_tasks}
            runs = [run for run in backend_store().list_runs(limit=500)
                    if int(run["task_id"]) in visible_ids]
            run_by_task: dict[int, dict[str, Any]] = {}
            for run in runs:
                run_by_task.setdefault(int(run["task_id"]), run)
            items = []
            for task in unified_tasks:
                config = task.get("config") or {}
                if task["collector_key"] in {"github", "gitee"}:
                    monitor_object = (config.get("repositories") or [""])[0]
                else:
                    monitor_object = (
                        config.get("path")
                        or config.get("feed_url")
                        or config.get("sitemap_url")
                        or config.get("url")
                        or config.get("image")
                        or task["source_name"]
                    )
                status_map = {
                    "running": ("运行中", "high"),
                    "paused": ("已暂停", "medium"),
                    "failed": ("采集失败", "high"),
                    "cancelled": ("已暂停", "medium"),
                    "done": ("待配置", "low"),
                    "pending": ("待配置", "medium"),
                }
                current_status, status_color = status_map.get(task["status"], ("待配置", "medium"))
                run = run_by_task.get(int(task["id"]))
                items.append({
                    "task_id": str(task["id"]),
                    "task_name": task["source_name"],
                    "channel": task["category"],
                    "platform": task["platform"],
                    "monitor_object": str(monitor_object),
                    "collection_method": task["collector_key"],
                    "schedule": f'每 {task["interval_sec"]} 秒' if task["mode"] == "continuous" else "手动",
                    "last_run": task.get("last_run_at"),
                    "status": current_status,
                    "status_color": status_color,
                    "last_run_status": (
                        "succeeded" if run and run["status"] == "completed"
                        else "failed" if run and run["status"] == "failed"
                        else "running" if run and run["status"] == "running"
                        else None
                    ),
                    "config": task.get("config") or {},
                    "config_raw": task.get("config_raw"),
                    "config_parse_error": bool(task.get("config_parse_error")),
                    "last_error": task.get("last_error"),
                })
            all_items = items
            items = _sort_monitoring_tasks_by_last_run(apply_filters(all_items))
            counts = {value: 0 for value in ("运行中", "待配置", "采集失败", "已暂停")}
            for item in items:
                counts[item["status"]] += 1
            payload["items"] = items
            payload["filters"] = [
                {
                    "key": "channel",
                    "label": "渠道类型",
                    "default": "全部渠道",
                    "options": sorted({item["channel"] for item in all_items if item["channel"]}),
                },
                {
                    "key": "status",
                    "label": "运行状态",
                    "default": "全部状态",
                    "options": ["运行中", "待配置", "采集失败", "已暂停"],
                },
                {
                    "key": "collection_method",
                    "label": "采集方式",
                    "default": "全部方式",
                    "options": sorted({
                        item["collection_method"]
                        for item in all_items
                        if item["collection_method"]
                    }),
                },
            ]
            payload["summary"] = [
                {"label": "任务总数", "value": len(items), "hint": "统一后端 sources/tasks"},
                {"label": "运行中", "value": counts["运行中"], "hint": "当前任务状态"},
                {"label": "待配置", "value": counts["待配置"], "hint": "手动任务或尚未运行"},
                {"label": "采集失败", "value": counts["采集失败"], "hint": "最近一次运行失败"},
            ]
            payload["source"] = "真实数据：规范化后端 SQLite sources/tasks"
            payload["implementation_status"] = "部分实现"
            payload["error"] = None
            payload["runs"] = [
                {
                    "run_id": f'backend-{run["id"]}',
                    "connector_name": next(
                        (task["collector_key"] for task in unified_tasks if int(task["id"]) == int(run["task_id"])),
                        "unknown",
                    ),
                    "target": next(
                        (task["source_name"] for task in unified_tasks if int(task["id"]) == int(run["task_id"])),
                        f'task-{run["task_id"]}',
                    ),
                    "started_at": run["started_at"],
                    "finished_at": run["finished_at"],
                    "status": (
                        "succeeded" if run["status"] == "completed"
                        else "failed" if run["status"] == "failed"
                        else "running"
                    ),
                    "candidates": int(run.get("stats", {}).get("candidates", 0)),
                    "inserted": int(run.get("stats", {}).get("new_occurrences", 0)),
                    "error_code": run.get("stats", {}).get("error_code"),
                    "task_id": str(run["task_id"]),
                }
                for run in runs[:10]
            ]
            return MonitoringTaskResponse.model_validate(payload).model_dump()
        except (BackendStorageError, BackendValidationError):
            payload["source"] = "真实数据源：规范化后端 SQLite 读取失败"
            payload["implementation_status"] = "部分实现"
            payload["error"] = "统一监控任务读取失败；未使用旧库或桩数据替代。"
            payload["items"] = []
            payload["runs"] = []
            return MonitoringTaskResponse.model_validate(payload).model_dump()

    payload["items"] = []
    payload["summary"] = [
        {"label": "任务总数", "value": 0, "hint": "SQLite配置记录"},
        {"label": "运行中", "value": 0, "hint": "状态字段，不代表调度器已运行"},
        {"label": "待配置", "value": 0, "hint": "SQLite配置记录"},
        {"label": "采集失败", "value": 0, "hint": "状态字段，不代表本次采集失败"},
    ]
    payload["source"] = "真实数据源：SQLite（配置/状态记录）"
    payload["implementation_status"] = "部分实现"
    payload["runs"] = []
    if runtime.monitoring_source != "sqlite":
        payload["error"] = "监控任务数据源配置无效；请设置 MONITORING_SOURCE=sqlite 或 mock。"
    else:
        store = MonitoringStore(
            runtime.monitoring_database_path,
            runtime.database_timeout,
            runtime.database_attempts,
        )
        try:
            payload["items"] = _sort_monitoring_tasks_by_last_run(apply_filters(store.list_tasks()))
            counts = {status: 0 for status in ("运行中", "待配置", "采集失败", "已暂停")}
            for task in payload["items"]:
                counts[task["status"]] += 1
            payload["summary"] = [
                {"label": "任务总数", "value": len(payload["items"]), "hint": "SQLite配置记录"},
                {"label": "运行中", "value": counts["运行中"], "hint": "存储状态，不代表调度器已运行"},
                {"label": "待配置", "value": counts["待配置"], "hint": "存储状态"},
                {"label": "采集失败", "value": counts["采集失败"], "hint": "历史状态样例，不代表本次采集失败"},
            ]
            try:
                payload["runs"] = ConnectorRunStore(runtime.connector_runs_path).list_runs()[:10]
            except (OSError, ValueError):
                payload["runs"] = []
        except StorageUnavailable:
            payload["source"] = "真实数据源：SQLite 读取失败"
            payload["error"] = "监控任务数据库读取失败；未使用桩数据替代。请检查路径或切回 mock。"
    return MonitoringTaskResponse.model_validate(payload).model_dump()


def mutate_monitoring_task(action: str, task_id: str | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime = _active_settings()
    if runtime.monitoring_source != "sqlite":
        return {"error": "当前为桩数据，监控任务写入未实现；请切换 MONITORING_SOURCE=sqlite。", "implementation_status": "未实现"}
    if _use_unified_monitoring_backend(runtime):
        store = backend_store()
        try:
            if action == "create":
                request = MonitoringTaskRequest.model_validate(payload or {}).model_dump()
                platform = request["platform"]
                collector_key = {
                    "GitHub": "github",
                    "GitHub（授权 API）": "github",
                    "Gitee": "gitee",
                    "Gitee（授权 API）": "gitee",
                    "Codeberg": "codeberg",
                    "Hugging Face Hub": "huggingface_repo",
                    "微博（官方 OAuth API）": "weibo_authorized_api",
                    "crates.io": "crates_registry",
                    "NuGet": "nuget_registry",
                    "RubyGems": "rubygems_registry",
                    "本地授权夹具": "controlled_fixtures",
                    "本地目录": "local_dir",
                    "通用 JSONL 导出": "authorized_jsonl",
                }.get(platform)
                if not collector_key:
                    collector_key = request["collection_method"].strip() or "local_dir"
                if collector_key in {"github", "gitee"}:
                    config = {"repositories": [request["monitor_object"]]}
                elif collector_key == "codeberg":
                    config = {"repository": request["monitor_object"]}
                elif collector_key in {
                    "local_dir",
                    "controlled_fixtures",
                    "authorized_jsonl",
                    "apk",
                    "wxapkg",
                }:
                    config = {"path": request["monitor_object"]}
                elif collector_key in {
                    "npm_registry",
                    "pypi_registry",
                    "crates_registry",
                    "nuget_registry",
                    "rubygems_registry",
                }:
                    config = {"package": request["monitor_object"]}
                elif collector_key == "huggingface_repo":
                    config = {"repository": request["monitor_object"]}
                elif collector_key == "weibo_authorized_api":
                    config = {"post_id": request["monitor_object"], "include_long_text": True, "max_items": 1}
                else:
                    config = {"url": request["monitor_object"]}
                schedule = request["schedule"].strip().lower()
                mode = "manual" if schedule in {"", "manual", "手动"} else "continuous"
                interval = 300
                for part in schedule.split():
                    if part.isdigit():
                        interval = max(30, min(86400, int(part)))
                        break
                result = store.create_source({
                    "name": request["task_name"],
                    "collector_key": collector_key,
                    "config": config,
                    "mode": mode,
                    "interval_sec": interval,
                    "enabled": True,
                })
                return {
                    "error": None,
                    "source": "真实数据：规范化后端 SQLite sources/tasks",
                    "implementation_status": "部分实现",
                    "task": {
                        "task_id": str(result["task"]["id"]),
                        "source_id": result["id"],
                        "task_name": result["name"],
                    },
                }
            numeric_id = int(task_id or 0)
            task = store.get_task(numeric_id)
            if task is None:
                return {"error": "未找到统一后端监控任务；未写入。", "implementation_status": "部分实现"}
            if action == "update":
                request = MonitoringTaskRequest.model_validate(payload or {}).model_dump()
                collector_key = str(task.get("collector_key") or request["collection_method"]).strip()
                monitor_object = request["monitor_object"].strip()
                if collector_key in {"github", "gitee"}:
                    config = {"repositories": [monitor_object]}
                elif collector_key == "codeberg":
                    config = {"repository": monitor_object}
                elif collector_key in {
                    "local_dir",
                    "controlled_fixtures",
                    "authorized_jsonl",
                    "apk",
                    "wxapkg",
                }:
                    config = {"path": monitor_object}
                elif collector_key in {
                    "npm_registry",
                    "pypi_registry",
                    "crates_registry",
                    "nuget_registry",
                    "rubygems_registry",
                }:
                    config = {"package": monitor_object}
                elif collector_key == "huggingface_repo":
                    config = {"repository": monitor_object}
                elif collector_key == "weibo_authorized_api":
                    config = {"post_id": monitor_object, "include_long_text": True, "max_items": 1}
                else:
                    config = {"url": monitor_object}
                schedule = request["schedule"].strip().lower()
                mode = "manual" if schedule in {"", "manual", "手动"} else "continuous"
                interval = 300
                for part in schedule.split():
                    if part.isdigit():
                        interval = max(30, min(86400, int(part)))
                        break
                updated = store.update_source(
                    int(task["source_id"]),
                    {
                        "name": request["task_name"],
                        "config": config,
                        "enabled": bool(task.get("source_enabled", True)),
                        "mode": mode,
                        "interval_sec": interval,
                    },
                )
                return {
                    "error": None,
                    "source": "真实数据：规范化后端 SQLite sources/tasks",
                    "implementation_status": "部分实现",
                    "task": {
                        "task_id": str(updated["task"]["id"]),
                        "source_id": updated["id"],
                        "task_name": updated["name"],
                    },
                }
            if action == "delete":
                deleted = store.delete_source(int(task["source_id"]))
                result = {"deleted": deleted}
            elif action in {"pause", "resume", "cancel", "retry"}:
                result = store.task_action(numeric_id, action)
            else:
                return {"error": "不支持的统一后端监控任务操作。", "implementation_status": "未实现"}
            return {
                "error": None,
                "source": "真实数据：规范化后端 SQLite sources/tasks",
                "implementation_status": "部分实现",
                "task": result,
            }
        except (BackendStorageError, BackendValidationError, ValueError):
            return {
                "error": "统一后端监控任务写入失败；未使用旧库或桩数据替代。",
                "source": "真实数据：规范化后端 SQLite",
                "implementation_status": "部分实现",
            }
    store = MonitoringStore(runtime.monitoring_database_path, runtime.database_timeout, runtime.database_attempts)
    try:
        if action == "create":
            result = store.create_task(MonitoringTaskRequest.model_validate(payload or {}).model_dump())
        elif action == "update":
            result = store.update_task(task_id or "", MonitoringTaskRequest.model_validate(payload or {}).model_dump(exclude={"task_id"}))
            if result is None:
                return {"error": "未找到监控任务；未更新。", "implementation_status": "部分实现"}
        elif action == "pause":
            result = store.set_paused(task_id or "", True)
            if result is None:
                return {"error": "未找到监控任务；未更新。", "implementation_status": "部分实现"}
        elif action == "resume":
            result = store.set_paused(task_id or "", False)
            if result is None:
                return {"error": "未找到监控任务；未更新。", "implementation_status": "部分实现"}
        elif action == "delete":
            result = {"deleted": store.delete_task(task_id or "")}
        else:
            return {"error": "不支持的监控任务操作。", "implementation_status": "未实现"}
        return {"error": None, "source": "真实数据：SQLite 监控任务配置", "implementation_status": "部分实现", "task": result}
    except ValueError as error:
        message = "监控任务参数不符合契约；未写入。" if str(error) != "monitoring_task_exists" else "任务 ID 已存在；未写入。"
        return {"error": message, "source": "真实数据：SQLite", "implementation_status": "部分实现"}
    except StorageUnavailable:
        return {"error": "监控任务数据库写入失败；未使用桩数据替代。", "source": "真实数据：SQLite 写入失败", "implementation_status": "部分实现"}


def run_monitoring_task(task_id: str) -> dict[str, Any]:
    runtime = _active_settings()
    if runtime.monitoring_source != "sqlite":
        return {"error": "当前为桩数据，未启动真实任务。", "implementation_status": "未实现"}
    if _use_unified_monitoring_backend(runtime):
        try:
            result = run_task(int(task_id), "manual")
            return {
                "error": None,
                "source": "真实数据：规范化后端 SQLite 统一流水线",
                "implementation_status": "部分实现",
                "result": {
                    "status": "succeeded",
                    "candidates": result["stats"].get("candidates", 0),
                    "inserted": result["stats"].get("new_occurrences", 0),
                    "run_id": result["run"]["id"],
                },
            }
        except (BackendStorageError, BackendValidationError, ValueError):
            return {
                "error": "统一后端任务执行失败；已保留失败审计，未返回候选内容。",
                "source": "真实数据：规范化后端 SQLite",
                "implementation_status": "部分实现",
            }
    task_store = MonitoringStore(runtime.monitoring_database_path, runtime.database_timeout, runtime.database_attempts)
    run_store = ConnectorRunStore(runtime.connector_runs_path)
    enabled_rule_ids = _enabled_rule_ids()
    try:
        task = task_store.get_task(task_id)
        if task is None:
            return {"error": "未找到监控任务；未运行。", "implementation_status": "部分实现"}
        if task["status"] == "已暂停":
            return {"error": "任务已暂停；未运行。", "implementation_status": "部分实现"}
        real_kind = ""
        if task["platform"] == "GitHub（授权 API）" or (
            task["platform"] == "GitHub" and runtime.connector_network_enabled
        ):
            real_kind = "github"
        elif task["platform"] == "Gitee（授权 API）" or (
            task["platform"] == "Gitee" and runtime.connector_network_enabled
        ):
            real_kind = "gitee"
        elif task["platform"] == "本地授权夹具":
            real_kind = "controlled"
        elif task["platform"] in {"本地目录", "本地授权载体"}:
            real_kind = "local-directory"
        elif task["collection_method"] in {"授权离线导入", "Manifest/JSONL"}:
            real_kind = "authorized-jsonl"

        if real_kind == "github" and runtime.connector_network_enabled and task["monitor_object"] not in runtime.github_repositories:
            return {
                "error": "目标仓库不在系统设置白名单中；未发起网络请求。",
                "source": "真实数据：SQLite 调度审计（授权范围校验）",
                "implementation_status": "部分实现",
            }
        if real_kind == "gitee" and runtime.connector_network_enabled and task["monitor_object"] not in runtime.gitee_repositories:
            return {
                "error": "目标 Gitee 仓库不在系统设置白名单中；未发起网络请求。",
                "source": "真实数据：SQLite 调度审计（授权范围校验）",
                "implementation_status": "部分实现",
            }

        def runner() -> tuple[int, int]:
            if real_kind == "controlled":
                records = detect_controlled_fixtures(
                    Path(task["monitor_object"]),
                    hmac_key=runtime.credential_hmac_key,
                    enabled_rule_ids=enabled_rule_ids,
                )
                result = EventStore(runtime.database_path).import_detected(records)
                for record in records:
                    AlertStore(runtime.response_database_path, event_path=runtime.database_path).ensure_alert(record)
                return len(records), result["inserted"]
            if real_kind == "local-directory":
                records = scan_local_directory(
                    Path(task["monitor_object"]),
                    hmac_key=runtime.credential_hmac_key,
                    enabled_rule_ids=enabled_rule_ids,
                )
                result = EventStore(runtime.database_path).import_detected(records)
                for record in records:
                    AlertStore(
                        runtime.response_database_path,
                        event_path=runtime.database_path,
                    ).ensure_alert(record)
                return len(records), result["inserted"]
            if real_kind == "authorized-jsonl":
                records = AuthorizedJsonlConnector(
                    Path(task["monitor_object"]),
                    runtime.credential_hmac_key,
                ).collect()
                result = EventStore(runtime.database_path).import_authorized(records)
                for record in records:
                    AlertStore(runtime.response_database_path, event_path=runtime.database_path).ensure_alert(record)
                return len(records), result["inserted"]
            if real_kind == "github":
                config = GitHubConnectorConfig(
                    enabled=runtime.connector_network_enabled,
                    token=runtime.github_token,
                    repositories=(task["monitor_object"],),
                    api_base=runtime.github_api_base,
                    timeout_seconds=runtime.connector_timeout_seconds,
                    max_files=runtime.connector_max_files,
                    max_file_bytes=runtime.connector_max_file_bytes,
                    min_interval_seconds=runtime.connector_min_interval_seconds,
                    max_retries=runtime.connector_max_retries,
                    hmac_key=runtime.credential_hmac_key,
                    checkpoint_store=run_store,
                    defer_checkpoints=True,
                    enabled_rule_ids=enabled_rule_ids,
                )
                connector = GitHubAuthorizedConnector(config)
                records = connector.collect()
                result = EventStore(runtime.database_path).import_real(records)
                connector.commit_checkpoints()
                for record in records:
                    AlertStore(runtime.response_database_path, event_path=runtime.database_path).ensure_alert(record)
                return len(records), result["inserted"]
            if real_kind == "gitee":
                config = GiteeConnectorConfig(
                    enabled=runtime.connector_network_enabled,
                    token=runtime.gitee_token,
                    repositories=(task["monitor_object"],),
                    api_base=runtime.gitee_api_base,
                    timeout_seconds=runtime.connector_timeout_seconds,
                    max_files=runtime.connector_max_files,
                    max_file_bytes=runtime.connector_max_file_bytes,
                    min_interval_seconds=runtime.connector_min_interval_seconds,
                    max_retries=runtime.connector_max_retries,
                    hmac_key=runtime.credential_hmac_key,
                    checkpoint_store=run_store,
                    defer_checkpoints=True,
                    enabled_rule_ids=enabled_rule_ids,
                )
                connector = GiteeAuthorizedConnector(config)
                records = connector.collect()
                result = EventStore(runtime.database_path).import_real(records)
                connector.commit_checkpoints()
                for record in records:
                    AlertStore(runtime.response_database_path, event_path=runtime.database_path).ensure_alert(record)
                return len(records), result["inserted"]
            return 0, 0

        scheduler = ConnectorScheduler(run_store)
        scheduled = ScheduledConnectorTask(
            task_id=task_id,
            connector_name=f'{task["platform"]}:{task["collection_method"]}',
            target=task["monitor_object"],
            interval_seconds=0,
            enabled=bool(real_kind),
        )
        result = scheduler.run_once(scheduled, runner)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        task_store.update_runtime(task_id, "运行中" if real_kind else "待配置", now, result["status"])
        return {
            "error": None,
            "source": "真实数据：SQLite 调度审计（真实连接器）" if real_kind else "真实数据：SQLite 调度审计（dry-run）",
            "implementation_status": "部分实现",
            "result": result,
        }
    except ConnectorBusyError:
        return {"error": "任务正在运行；本次未重复启动。", "implementation_status": "部分实现"}
    except StorageUnavailable:
        return {"error": "任务或运行审计数据库读取失败；未运行。", "implementation_status": "部分实现"}
    except (LocalScanError, ValueError):
        try:
            task_store.update_runtime(
                task_id,
                "采集失败",
                datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "failed",
            )
        except Exception:
            pass
        return {
            "error": "连接器执行失败；已写入失败审计，未返回候选内容。",
            "source": "真实数据：SQLite 调度审计",
            "implementation_status": "部分实现",
        }
    except Exception:
        try:
            task_store.update_runtime(
                task_id,
                "采集失败",
                datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "failed",
            )
        except Exception:
            pass
        return {
            "error": "连接器执行失败；已写入失败审计，未返回候选内容。",
            "source": "真实数据：SQLite 调度审计",
            "implementation_status": "部分实现",
        }


def get_connector_runs() -> dict[str, Any]:
    runtime = _active_settings()
    try:
        runs = ConnectorRunStore(runtime.connector_runs_path).list_runs()[:50]
        return ConnectorRunsResponse.model_validate({
            "schema_version": "1.0",
            "page_title": "连接器运行记录",
            "source": "真实数据：connector_runs SQLite",
            "implementation_status": "部分实现",
            "error": None,
            "items": runs,
        }).model_dump()
    except (OSError, ValueError):
        return ConnectorRunsResponse.model_validate({
            "schema_version": "1.0",
            "page_title": "连接器运行记录",
            "source": "真实数据：connector_runs SQLite 读取失败",
            "implementation_status": "部分实现",
            "error": "运行记录读取失败；未使用桩数据替代。",
            "items": [],
        }).model_dump()


def get_event_detail(event_id: str) -> dict[str, Any]:
    runtime = _active_settings()
    if runtime.events_source == "mock":
        payload = _load_mock("event_detail")
        payload["schema_version"] = "1.0"
        payload["error"] = None
        return EventDetailResponse.model_validate(payload).model_dump()

    if _use_unified_backend(runtime):
        payload = _load_mock("event_detail")
        payload["schema_version"] = "1.0"
        payload["source"] = "真实数据：规范化后端 SQLite occurrences"
        payload["implementation_status"] = "部分实现"
        try:
            detail = backend_store().event_detail_record(event_id)
        except (BackendStorageError, BackendValidationError):
            payload["error"] = "统一事件详情读取失败；未使用旧库或桩数据替代。"
            payload["event"] = {"event_id": event_id, "summary": [{"label": "查询结果", "value": "读取失败"}]}
            payload["evidence"] = []
            payload["timeline"] = []
            payload["rechecks"] = []
            payload["actions"] = []
            return EventDetailResponse.model_validate(payload).model_dump()
        if detail is None:
            payload["error"] = "未找到该事件；没有使用旧库或桩数据补齐详情。"
            payload["event"] = {"event_id": event_id, "summary": [{"label": "查询结果", "value": "未找到"}]}
            payload["evidence"] = []
            payload["timeline"] = []
            payload["rechecks"] = []
            payload["actions"] = []
            return EventDetailResponse.model_validate(payload).model_dump()
        event = detail["event"]
        payload["error"] = None
        fetched_from_connector = detail.get("fetched_at_source") == "connector"
        payload["event"] = {
            "event_id": event["event_id"],
            "summary": [
                {"label": "凭据类型", "value": event["credential_type"]},
                {"label": "渠道", "value": f'{event["channel"]} / {event["platform"]}'},
                {"label": "风险档", "value": event["risk_level"]},
                {"label": "处置状态", "value": event["disposition"]},
                {"label": "数据来源", "value": event["source_kind"]},
            ],
            "event_record": event,
        }
        payload["evidence"] = [
            {"label": "脱敏候选", "value": event["redacted_value"]},
            {"label": "证据等级", "value": event["evidence_level"]},
            {"label": "公开位置", "value": event.get("source_locator") or "未记录"},
            {"label": "凭据指纹", "value": event.get("credential_fingerprint") or "未生成"},
            {"label": "公开时间", "value": detail.get("published_at") or "未知"},
            {"label": "公开时间状态", "value": {
                "known": "有证据支持",
                "reference_only": "来源时间",
                "unknown": "未知",
            }.get(detail.get("public_time_status"), "未知")},
            {"label": "公开时间来源", "value": _time_source_label(detail.get("published_source"))},
            {"label": "公开时间含义", "value": event.get("published_meaning") or "未记录"},
            {"label": "公开时间证据", "value": event.get("published_evidence") or "未记录"},
            {
                "label": "公开时间未知原因",
                "value": detail.get("public_time_unknown_reason") or "无",
            },
            {
                "label": "内容版本修订时间",
                "value": detail.get("source_updated_at") or "未记录",
            },
            {
                "label": "内容版本时间来源",
                "value": _time_source_label(detail.get("source_updated_source")),
            },
            {
                "label": "采集时间",
                "value": detail.get("fetched_at") if fetched_from_connector else "未知",
            },
            {
                "label": "采集时间来源",
                "value": (
                    "连接器明确记录"
                    if fetched_from_connector
                    else "历史记录推断；不纳入精确时效统计"
                ),
            },
            {
                "label": "系统首次发现时间",
                "value": event.get("first_discovered_at") or "未记录",
            },
            {
                "label": "发现延迟",
                "value": (
                    (
                        (
                            "公开至首次发现："
                            if detail.get("discovery_latency", {}).get("calculation_type")
                            == "public_to_first_discovery"
                            else "内容时间至检出："
                        )
                        + _format_seconds(detail.get("discovery_latency", {}).get("seconds"))
                    )
                    if detail.get("discovery_latency", {}).get("status") == "calculated"
                    else f'不可计算：{_time_reason_label(detail.get("discovery_latency", {}).get("reason"))}'
                ),
            },
            {
                "label": "发现延迟计算依据",
                "value": (
                    detail.get("discovery_latency", {}).get("basis")
                    or "缺失原因待核查"
                ),
            },
            {
                "label": "发现延迟原因",
                "value": (
                    "已按同一来源、同一内容版本、同一出现位置计算"
                    if detail.get("discovery_latency", {}).get("status") == "calculated"
                    else _time_reason_label(detail.get("discovery_latency", {}).get("reason"))
                ),
            },
        ]
        payload["timeline"] = detail["timeline"]
        payload["rechecks"] = [
            {"label": "当前来源可见性", "value": "需按来源再次采集；删除内容不证明副本消失"},
            {"label": "历史副本检查", "value": "未自动检索第三方历史副本"},
            {"label": "凭据撤销/轮换", "value": "未执行；仅自有或明确授权环境允许验证"},
            {"label": "关闭依据", "value": "以复核日志和当前来源复查结果为准"},
        ]
        notification = detail.get("notification")
        payload["alerts"] = []
        if notification:
            payload["alerts"] = [{
                "alert_id": notification["notification_id"],
                "event_id": event["event_id"],
                "severity": notification["severity"],
                "status": notification["status"],
                "channel": "offline-sink",
                "created_at": notification["created_at"],
                "delivered_at": notification.get("delivered_at"),
                "delivery_note": notification["delivery_note"],
            }]
        payload["feedbacks"] = [
            {
                "feedback_id": f'REVIEW-{event["event_id"]}-{index}',
                "event_id": event["event_id"],
                "action": log.get("action") or "",
                "disposition": event["disposition"],
                "created_at": log.get("created_at") or "",
                "note": "",
            }
            for index, log in enumerate(detail.get("review_logs", []), start=1)
        ]
        payload["actions"] = [
            {"label": "确认公开泄露", "status": "可用", "action": "confirm_public"},
            {"label": "标记重复", "status": "可用", "action": "mark_duplicate"},
            {"label": "标记误报", "status": "可用", "action": "mark_false_positive"},
            {"label": "重新检查", "status": "可用", "action": "recheck"},
            {"label": "关闭事件", "status": "可用", "action": "close_event"},
        ]
        return EventDetailResponse.model_validate(payload).model_dump()

    payload = _load_mock("event_detail")
    payload["schema_version"] = "1.0"
    payload["source"] = "真实数据源：SQLite（合成样例记录）"
    payload["implementation_status"] = "部分实现"
    payload["error"] = None
    if runtime.events_source != "sqlite":
        payload["source"] = "真实数据源配置无效"
        payload["error"] = "详情数据源配置无效；请设置 EVENTS_SOURCE=sqlite 或 mock。"
        return EventDetailResponse.model_validate(payload).model_dump()

    store = EventStore(runtime.database_path, runtime.database_timeout, runtime.database_attempts)
    try:
        matched = store.get_event(event_id)
    except StorageUnavailable:
        payload["source"] = "真实数据源：SQLite 读取失败"
        payload["error"] = "事件详情读取失败；未使用桩数据替代。"
        payload["event"] = {
            "event_id": event_id,
            "summary": [
                {"label": "查询结果", "value": "读取失败"},
                {"label": "数据来源", "value": "SQLite"},
            ],
        }
        payload["evidence"] = []
        payload["timeline"] = []
        payload["rechecks"] = []
        payload["actions"] = [
            {"label": "重新检查", "status": "功能未实现"},
            {"label": "返回事件列表", "status": "可用"},
        ]
        return EventDetailResponse.model_validate(payload).model_dump()

    if matched is None:
        payload["source"] = "真实数据源：SQLite"
        payload["error"] = "未找到该事件；没有使用其他事件或桩数据补齐详情。"
        payload["event"] = {
            "event_id": event_id,
            "summary": [
                {"label": "查询结果", "value": "未找到"},
                {"label": "数据来源", "value": "SQLite"},
            ],
        }
        payload["evidence"] = []
        payload["timeline"] = []
        payload["rechecks"] = []
        return EventDetailResponse.model_validate(payload).model_dump()

    if matched["source_kind"] in {"受控投放检出", "离线授权导入"}:
        payload["source"] = f'真实数据源：SQLite（{matched["source_kind"]}）'
    payload["event"] = {
        "event_id": matched["event_id"],
        "summary": [
            {"label": "凭据类型", "value": matched["credential_type"], "hint": "来自 SQLite 事件记录"},
            {"label": "渠道", "value": f'{matched["channel"]} / {matched["platform"]}', "hint": "来自 SQLite 事件记录"},
            {"label": "风险档", "value": matched["risk_level"], "hint": "影响风险尚未独立验证"},
            {"label": "处置状态", "value": matched["disposition"], "hint": "来自 SQLite 事件状态机"},
        ],
        "event_record": matched,
    }
    payload["evidence"] = [
        {"label": "脱敏候选", "value": matched["redacted_value"]},
        {"label": "证据等级", "value": matched["evidence_level"]},
        {"label": "数据来源", "value": matched["source_kind"]},
        {"label": "公开位置", "value": matched.get("source_locator") or "未记录"},
        {"label": "指纹", "value": matched.get("credential_fingerprint") or "未生成（HMAC密钥未配置或旧记录）"},
        {"label": "指纹版本", "value": matched.get("fingerprint_key_version") or "未记录"},
        {"label": "有效性", "value": "未验证"},
    ]
    payload["timeline"] = []
    if matched["first_observed_at"]:
        payload["timeline"].append({
            "time": matched["first_observed_at"],
            "title": "首次观测时间（存储字段）",
            "detail": "仅显示事件记录中的时间；没有推断公开时间、采集时间或检测时间。",
        })
    if matched.get("detected_at"):
        payload["timeline"].append({
            "time": matched["detected_at"],
            "title": "检测时间",
            "detail": f'检测方法：{matched.get("detection_method") or "未记录"}；仅适用于当前受控/存储来源。',
        })
    payload["rechecks"] = [
        {"label": "当前来源可见性", "value": "待按来源复查；受控夹具不等同公开渠道"},
        {"label": "历史副本检查", "value": "未实现（不自动检索或删除第三方副本）"},
        {"label": "凭据撤销/轮换", "value": "未执行，第三方禁止操作"},
        {"label": "关闭依据", "value": "未记录"},
    ]
    alert_store = AlertStore(runtime.response_database_path, event_path=runtime.database_path)
    try:
        alert = alert_store.get_alert_for_event(event_id)
        payload["alerts"] = [alert] if alert else []
        payload["feedbacks"] = alert_store.list_feedback(event_id)
    except StorageUnavailable:
        payload["error"] = "响应审计数据库读取失败；未使用桩数据替代。"
        payload["alerts"] = []
        payload["feedbacks"] = []
    for feedback in payload["feedbacks"]:
        payload["rechecks"][-1]["value"] = feedback["disposition"]
        payload["timeline"].append({
            "time": feedback["created_at"],
            "title": f'反馈：{feedback["action"]}',
            "detail": f'状态更新为“{feedback["disposition"]}”；备注未原样持久化。',
        })
    for alert in payload["alerts"]:
        payload["timeline"].append({
            "time": alert["created_at"],
            "title": "告警审计记录",
            "detail": alert["delivery_note"],
        })
    payload["actions"] = [
        {"label": "确认公开泄露", "status": "可用", "action": "confirm_public"},
        {"label": "标记重复", "status": "可用", "action": "mark_duplicate"},
        {"label": "标记误报", "status": "可用", "action": "mark_false_positive"},
        {"label": "重新检查", "status": "可用", "action": "recheck"},
        {"label": "关闭事件", "status": "可用", "action": "close_event"},
    ]
    return EventDetailResponse.model_validate(payload).model_dump()


def export_event_evidence(event_id: str) -> dict[str, Any]:
    runtime = _active_settings()
    if _use_unified_backend(runtime):
        try:
            detail = backend_store().event_detail_record(event_id)
            if detail is None:
                return {
                    "error": "未找到事件；未生成导出文件。",
                    "source": "真实数据：规范化后端 SQLite",
                    "implementation_status": "部分实现",
                }
            notification = detail.get("notification")
            alert = None
            if notification:
                alert = {
                    "alert_id": notification["notification_id"],
                    "event_id": event_id,
                    "severity": notification["severity"],
                    "status": notification["status"],
                    "channel": "offline-sink",
                    "created_at": notification["created_at"],
                    "delivered_at": notification.get("delivered_at"),
                    "delivery_note": notification["delivery_note"],
                }
            feedbacks = [
                {
                    "feedback_id": f'REVIEW-{event_id}-{index}',
                    "event_id": event_id,
                    "action": log.get("action") or "",
                    "disposition": detail["event"]["disposition"],
                    "created_at": log.get("created_at") or "",
                    "note": "",
                }
                for index, log in enumerate(detail.get("review_logs", []), start=1)
            ]
            export_event = {
                **detail["event"],
                "published_at": detail.get("published_at"),
                "published_source": detail.get("published_source"),
                "published_confidence": detail.get("published_confidence"),
                "published_meaning": detail.get("published_meaning"),
                "published_evidence": detail.get("published_evidence"),
                "public_time_status": detail.get("public_time_status"),
                "public_time_unknown_reason": detail.get("public_time_unknown_reason"),
                "fetched_at": detail.get("fetched_at"),
                "fetched_at_source": detail.get("fetched_at_source"),
                "source_updated_at": detail.get("source_updated_at"),
                "source_updated_source": detail.get("source_updated_source"),
                "source_updated_confidence": detail.get("source_updated_confidence"),
                "source_updated_meaning": detail.get("source_updated_meaning"),
                "source_updated_evidence": detail.get("source_updated_evidence"),
                "time_provenance": detail.get("time_provenance") or {},
                "first_observed_at": (detail.get("time_fields") or {}).get("first_observed_at"),
                "public_confirmed_at": (detail.get("time_fields") or {}).get("public_confirmed_at"),
                "run_id": detail["event"].get("run_id"),
                "evidence_status": "event_recorded",
            }
            return {
                "error": None,
                "source": "真实数据：规范化后端 SQLite 脱敏证据导出",
                "implementation_status": "部分实现",
                "package": build_evidence_package(
                    export_event,
                    [alert] if alert else [],
                    feedbacks,
                ),
            }
        except (BackendStorageError, BackendValidationError):
            return {
                "error": "统一事件证据读取失败；未生成导出文件。",
                "source": "真实数据：规范化后端 SQLite 读取失败",
                "implementation_status": "部分实现",
            }
    if runtime.events_source != "sqlite":
        return {
            "error": "当前为桩数据，未生成导出文件；请切换 EVENTS_SOURCE=sqlite。",
            "source": "桩数据 / 导出未实现",
            "implementation_status": "未实现",
        }
    try:
        event = EventStore(
            runtime.database_path,
            runtime.database_timeout,
            runtime.database_attempts,
        ).get_event(event_id)
        if event is None:
            return {
                "error": "未找到事件；未生成导出文件。",
                "source": "真实数据：SQLite",
                "implementation_status": "部分实现",
            }
        alert_store = AlertStore(runtime.response_database_path, event_path=runtime.database_path)
        return {
            "error": None,
            "source": "真实数据：SQLite 脱敏证据导出",
            "implementation_status": "部分实现",
            "package": build_evidence_package(
                event,
                ([alert] if (alert := alert_store.get_alert_for_event(event_id)) else []),
                alert_store.list_feedback(event_id),
            ),
        }
    except StorageUnavailable:
        return {
            "error": "事件或响应审计数据库读取失败；未生成导出文件。",
            "source": "真实数据：SQLite 读取失败",
            "implementation_status": "部分实现",
        }


def get_alerts() -> dict[str, Any]:
    runtime = _active_settings()
    if runtime.response_source == "mock":
        return AlertsResponse.model_validate(_load_mock("alerts")).model_dump()
    if _use_unified_backend(runtime):
        try:
            items = []
            for notification in backend_store().list_notifications():
                items.append({
                    "alert_id": notification["notification_id"],
                    "event_id": notification["event_id"],
                    "severity": notification["severity"],
                    "status": notification["status"],
                    "channel": "offline-sink",
                    "created_at": notification["created_at"],
                    "delivered_at": notification.get("delivered_at"),
                    "delivery_note": notification["delivery_note"],
                })
            return AlertsResponse.model_validate({
                "schema_version": "1.0",
                "page_title": "告警队列",
                "source": "真实数据：规范化后端 SQLite 通知适配器",
                "implementation_status": "部分实现",
                "error": None,
                "items": items,
            }).model_dump()
        except (BackendStorageError, BackendValidationError):
            return AlertsResponse.model_validate({
                "schema_version": "1.0",
                "page_title": "告警队列",
                "source": "真实数据：规范化后端 SQLite 读取失败",
                "implementation_status": "部分实现",
                "error": "告警队列读取失败；未使用旧库或桩数据替代。",
                "items": [],
            }).model_dump()
    if runtime.response_source != "sqlite":
        return AlertsResponse.model_validate({
            "schema_version": "1.0",
            "page_title": "告警队列",
            "source": "响应数据源配置无效",
            "implementation_status": "部分实现",
            "error": "RESPONSE_SOURCE 必须为 sqlite 或 mock。",
            "items": [],
        }).model_dump()
    try:
        items = AlertStore(runtime.response_database_path, event_path=runtime.database_path).list_alerts()
        return AlertsResponse.model_validate({
            "schema_version": "1.0",
            "page_title": "告警队列",
            "source": "真实数据：SQLite 离线通知队列",
            "implementation_status": "部分实现",
            "error": None,
            "items": items,
        }).model_dump()
    except (OSError, ValueError, StorageUnavailable):
        return AlertsResponse.model_validate({
            "schema_version": "1.0",
            "page_title": "告警队列",
            "source": "真实数据：SQLite 告警队列读取失败",
            "implementation_status": "部分实现",
            "error": "告警队列读取失败；未使用桩数据替代。",
            "items": [],
    }).model_dump()


def create_alert(event_id: str) -> dict[str, Any]:
    runtime = _active_settings()
    if _use_unified_backend(runtime):
        try:
            detail = backend_store().event_detail_record(event_id)
            if detail is None:
                return {
                    "schema_version": "1.0",
                    "source": "真实数据：规范化后端 SQLite",
                    "implementation_status": "部分实现",
                    "error": "未找到事件；未创建告警。",
                    "alert": None,
                }
            notification = detail.get("notification")
            if notification is None:
                return {
                    "schema_version": "1.0",
                    "source": "真实数据：规范化后端 SQLite",
                    "implementation_status": "部分实现",
                    "error": "当前 occurrence 没有通知记录；未伪造告警成功。",
                    "alert": None,
                }
            alert = {
                "alert_id": notification["notification_id"],
                "event_id": event_id,
                "severity": notification["severity"],
                "status": notification["status"],
                "channel": "offline-sink",
                "created_at": notification["created_at"],
                "delivered_at": notification.get("delivered_at"),
                "delivery_note": notification["delivery_note"],
            }
            return {
                "schema_version": "1.0",
                "source": "真实数据：规范化后端 SQLite 离线通知适配器",
                "implementation_status": "部分实现",
                "error": None,
                "alert": alert,
            }
        except (BackendStorageError, BackendValidationError):
            return {
                "schema_version": "1.0",
                "source": "真实数据：规范化后端 SQLite 读取失败",
                "implementation_status": "部分实现",
                "error": "告警读取失败；未发送外部通知。",
                "alert": None,
            }
    if runtime.response_source != "sqlite":
        return {
            "schema_version": "1.0",
            "source": "桩数据 / 离线操作未接入",
            "implementation_status": "未实现",
            "error": "当前响应数据源不是 SQLite，未执行告警写入。",
            "alert": None,
        }
    try:
        event = EventStore(
            runtime.database_path,
            runtime.database_timeout,
            runtime.database_attempts,
        ).get_event(event_id)
        if event is None:
            return {
                "schema_version": "1.0",
                "source": "真实数据：SQLite",
                "implementation_status": "部分实现",
                "error": "未找到事件；未创建告警。",
                "alert": None,
            }
        alert = AlertStore(runtime.response_database_path, event_path=runtime.database_path).ensure_alert(event)
        return {
            "schema_version": "1.0",
            "source": "真实数据：SQLite 离线通知桩",
            "implementation_status": "部分实现",
            "error": None,
            "alert": alert,
        }
    except StorageUnavailable:
        return {
            "schema_version": "1.0",
            "source": "真实数据：SQLite 响应审计写入失败",
            "implementation_status": "部分实现",
            "error": "告警写入失败；未发送外部通知。",
            "alert": None,
        }


def apply_feedback(event_id: str, request: FeedbackRequest) -> dict[str, Any]:
    runtime = _active_settings()
    if _use_unified_backend(runtime):
        action_map = {
            "confirm_public": "confirm",
            "mark_duplicate": "ignore",
            "mark_false_positive": "false_positive",
            "close_event": "ignore",
        }
        try:
            if request.action == "recheck":
                detail = backend_store().event_detail_record(event_id)
                if detail is None:
                    raise BackendValidationError("credential_not_found")
                now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                feedback = {
                    "feedback_id": f"RECHECK-{event_id}-{now}",
                    "event_id": event_id,
                    "action": request.action,
                    "disposition": detail["event"]["disposition"],
                    "created_at": now,
                    "note": "",
                }
            else:
                detail = backend_store().event_detail_record(event_id)
                if detail is None:
                    raise BackendValidationError("credential_not_found")
                # Review is credential-scoped in the normalized model. Use the
                # occurrence's HMAC fingerprint to locate the entity without
                # ever accepting raw secret material from the request.
                fingerprint = detail["event"].get("credential_fingerprint")
                finding = backend_store().find_credential_by_fingerprint(fingerprint)
                if finding is None:
                    raise BackendValidationError("credential_not_found")
                result = backend_store().review(
                    int(finding["id"]),
                    action_map[request.action],
                    request.note,
                )
                feedback = {
                    "feedback_id": f'REVIEW-{event_id}-{result["created_at"]}',
                    "event_id": event_id,
                    "action": request.action,
                    "disposition": detail["event"]["disposition"] if request.action == "mark_duplicate" else (
                        "已确认公开泄露" if request.action == "confirm_public" else
                        "已标记误报" if request.action == "mark_false_positive" else
                        "已忽略"
                    ),
                    "created_at": result["created_at"],
                    "note": "",
                }
            return {
                "schema_version": "1.0",
                "source": "真实数据：规范化后端 SQLite 反馈与安全审计",
                "implementation_status": "部分实现",
                "error": None,
                "feedback": feedback,
            }
        except (BackendStorageError, BackendValidationError):
            return {
                "schema_version": "1.0",
                "source": "真实数据：规范化后端 SQLite 反馈写入失败",
                "implementation_status": "部分实现",
                "error": "统一反馈未写入；请确认事件仍存在且状态允许流转。",
                "feedback": None,
            }
    if runtime.response_source != "sqlite":
        return {
            "schema_version": "1.0",
            "source": "桩数据 / 离线操作未接入",
            "implementation_status": "未实现",
            "error": "当前响应数据源不是 SQLite，未执行状态更新。",
            "feedback": None,
        }
    try:
        feedback = AlertStore(runtime.response_database_path, event_path=runtime.database_path).add_feedback(
            event_id, request.action, request.note
        )
        return {
            "schema_version": "1.0",
            "source": "真实数据：SQLite 事件状态机与反馈审计",
            "implementation_status": "部分实现",
            "error": None,
            "feedback": feedback,
        }
    except StorageUnavailable as error:
        message = "事件反馈写入失败；状态和审计未承诺完成。"
        if str(error) == "event_not_found":
            message = "未找到事件；未更新状态。"
        return {
            "schema_version": "1.0",
            "source": "真实数据：SQLite 响应审计写入失败",
            "implementation_status": "部分实现",
            "error": message,
            "feedback": None,
        }
    except ValueError:
        return {
            "schema_version": "1.0",
            "source": "真实数据：SQLite",
            "implementation_status": "部分实现",
            "error": "反馈动作或备注不符合契约；未更新状态。",
            "feedback": None,
        }


def batch_feedback(event_ids: list[str], action: str, note: str = "") -> dict[str, Any]:
    runtime = _active_settings()
    if _use_unified_backend(runtime):
        if not event_ids or len(event_ids) > 500:
            return {
                "error": "一次最多选择 500 个事件；未写入。",
                "source": "真实数据：规范化后端 SQLite",
                "implementation_status": "部分实现",
                "updated": [],
                "failed": [],
            }
        try:
            request = FeedbackRequest(action=action, note=note)
        except ValueError:
            return {
                "error": "批量反馈动作不符合契约；未写入。",
                "source": "真实数据：规范化后端 SQLite",
                "implementation_status": "部分实现",
                "updated": [],
                "failed": [],
            }
        updated: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        for event_id in dict.fromkeys(event_ids):
            result = apply_feedback(event_id, request)
            if result.get("error"):
                failed.append({"event_id": event_id, "error": result["error"]})
            elif result.get("feedback"):
                updated.append(result["feedback"])
        return {
            "error": None if updated and not failed else "部分事件未更新；请查看 failed 明细。",
            "source": "真实数据：规范化后端 SQLite 批量反馈与审计",
            "implementation_status": "部分实现",
            "updated": updated,
            "failed": failed,
        }
    if runtime.response_source != "sqlite" or runtime.events_source != "sqlite":
        return {
            "error": "批量反馈要求事件和响应数据源均为 SQLite；桩数据未执行写入。",
            "source": "桩数据 / 批量反馈未接入",
            "implementation_status": "未实现",
            "updated": [],
            "failed": [],
        }
    if not event_ids or len(event_ids) > 500:
        return {
            "error": "一次最多选择 500 个事件；未写入。",
            "source": "真实数据：SQLite",
            "implementation_status": "部分实现",
            "updated": [],
            "failed": [],
        }
    if action not in {"confirm_public", "mark_duplicate", "mark_false_positive", "recheck", "close_event"}:
        return {
            "error": "批量反馈动作不符合契约；未写入。",
            "source": "真实数据：SQLite",
            "implementation_status": "部分实现",
            "updated": [],
            "failed": [],
        }
    store = AlertStore(runtime.response_database_path, event_path=runtime.database_path)
    updated: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    for event_id in dict.fromkeys(event_ids):
        try:
            updated.append(store.add_feedback(event_id, action, note))
        except (StorageUnavailable, ValueError) as error:
            failed.append({"event_id": event_id, "error": str(error)})
    return {
        "error": None if updated and not failed else "部分事件未更新；请查看 failed 明细。",
        "source": "真实数据：SQLite 批量反馈与审计",
        "implementation_status": "部分实现",
        "updated": updated,
        "failed": failed,
    }


def export_events(event_ids: list[str] | None = None) -> dict[str, Any]:
    runtime = _active_settings()
    if _use_unified_backend(runtime):
        try:
            rows = backend_store().list_event_records(page=1, page_size=100)
            if event_ids:
                selected = set(event_ids)
                rows["items"] = [item for item in rows["items"] if item["event_id"] in selected]
            return {
                "error": None,
                "source": "真实数据：规范化后端 SQLite 脱敏事件导出",
                "implementation_status": "部分实现",
                "items": rows["items"],
            }
        except (BackendStorageError, BackendValidationError):
            return {
                "error": "统一事件数据库读取失败；未生成导出文件。",
                "source": "真实数据：规范化后端 SQLite 读取失败",
                "implementation_status": "部分实现",
                "items": [],
            }
    if runtime.events_source != "sqlite":
        return {
            "error": "当前为桩数据，未生成导出文件；请切换 EVENTS_SOURCE=sqlite。",
            "source": "桩数据 / 导出未实现",
            "implementation_status": "未实现",
            "items": [],
        }
    try:
        rows = EventStore(
            runtime.database_path,
            runtime.database_timeout,
            runtime.database_attempts,
        ).list_events()
        if event_ids:
            selected = set(event_ids)
            rows = [row for row in rows if row["event_id"] in selected]
        safe_rows = [
            {
                key: value
                for key, value in row.items()
                if key not in {"secret", "raw_value", "_secret"}
            }
            for row in rows
        ]
        return {
            "error": None,
            "source": "真实数据：SQLite 脱敏事件导出",
            "implementation_status": "部分实现",
            "items": safe_rows,
        }
    except StorageUnavailable:
        return {
            "error": "事件数据库读取失败；未生成导出文件。",
            "source": "真实数据：SQLite 读取失败",
            "implementation_status": "部分实现",
            "items": [],
        }


def get_reports() -> dict[str, Any]:
    runtime = _active_settings()
    payload = _load_mock("reports")
    if runtime.events_source != "sqlite":
        return payload
    if _use_unified_backend(runtime):
        try:
            report = backend_store().evaluation_report()
            counts = report["counts"]
            time_report = report["timeliness"]
            experiments = report.get("experiments", [])
            payload["source"] = report["source"]
            payload["implementation_status"] = report["implementation_status"]
            payload["error"] = report["error"]
            payload["counts"] = [
                {
                    "label": "真实候选数",
                    "value": counts.get("real_candidate_count", counts["candidate_count"]),
                    "note": "source_kind=真实检测结果 的出现位置",
                },
                {
                    "label": "真实凭据实体",
                    "value": counts.get("real_credential_count", counts["unique_fingerprint_count"]),
                    "note": "真实检测结果中的凭据实体",
                },
                {
                    "label": "支持检索凭据类型",
                    "value": counts["credential_type_count"],
                    "note": (
                        "按已启用规则记录去重；"
                        f'真实检测命中的类型 {counts.get("real_credential_type_count", 0)} 类'
                    ),
                },
                {
                    "label": "已配置渠道",
                    "value": counts.get("configured_channel_type_count", counts.get("channel_type_count", 0)),
                    "note": "support_matrix 中配置的渠道类型",
                },
                {
                    "label": "已配置平台",
                    "value": counts.get("configured_platform_count", counts.get("platform_count", 0)),
                    "note": "support_matrix 中配置的平台",
                },
                {
                    "label": "确认公开泄露数",
                    "value": counts["confirmed_public_count"],
                    "note": "必须有真实来源 occurrence 的 confirm 反馈",
                },
                {
                    "label": "唯一指纹数",
                    "value": counts["unique_fingerprint_count"],
                    "note": "带密钥 HMAC 指纹去重，可按明细重算",
                },
                {
                    "label": "全量存储实体",
                    "value": counts.get("all_stored_credential_count"),
                    "note": "含受控投放和离线授权记录，单独列示",
                },
            ]
            payload["evaluation_mapping"] = [
                {
                    "evaluation": "①公开渠道类型数量、总数量",
                    "module": "support_matrix / occurrences",
                    "current": (
                        f'{counts.get("configured_channel_type_count", counts.get("channel_type_count", 0))} 类 / '
                        f'{counts.get("configured_platform_count", counts.get("platform_count", 0))} 平台'
                    ),
                    "definition": "按 support_matrix 中已配置的渠道类型和平台去重；真实观测数量单独保存在 observed 字段。",
                    "status": "统一支持矩阵口径",
                },
                {
                    "evaluation": "②支持检索凭据类型数量",
                    "module": "support_matrix / rule_records",
                    "current": f'{counts["credential_type_count"]} 类',
                    "definition": (
                        "按已启用规则记录中的凭据类型去重；"
                        f'真实检测命中的类型为 {counts.get("real_credential_type_count", 0)} 类'
                    ),
                    "status": "已从真实规则记录重算；官方类型口径待确认",
                },
                {
                    "evaluation": "③平台可扩展性",
                    "module": "extensibility_experiments",
                    "current": (
                        f'{sum(item.get("runtime_check_status") == "已通过" for item in experiments)}'
                        f' / {len(experiments)} 项发布包运行时检查通过'
                    ),
                    "definition": (
                        "发布包运行时检查验证实现模块和连接器入口可加载；"
                        "完整功能实验、checkpoint、失败恢复和真实平台实测单独记录。"
                    ),
                    "status": "没有实验数据时保持未开展",
                },
                {
                    "evaluation": "④发现凭据数量",
                    "module": "credentials / occurrences",
                    "current": f'{counts["unique_fingerprint_count"]} 唯一 / {counts["candidate_count"]} 候选 / {counts["confirmed_public_count"]} 已确认公开',
                    "definition": "候选、唯一凭据和确认公开分开统计；官方计数口径待确认",
                    "status": "可重算；不把抽样复核升级为全量确认",
                },
                {
                    "evaluation": "⑤发现时间与凭据公开泄露时间",
                    "module": "source_contents + occurrences + notification_outbox + review_logs",
                    "current": _format_seconds(time_report["published_to_discovered"]["p50_seconds"]),
                    "definition": "公开至系统首次发现单独统计；参考时间只进入内容时间至检出统计；未知字段保留为空",
                    "status": "字段链已实现；样本不足时显示未知",
                },
            ]
            payload["timeliness"] = [
                {
                    "label": label,
                    "value": (
                        f'P50 {_format_seconds(time_report[key]["p50_seconds"])}；'
                        f'P95 {_format_seconds(time_report[key]["p95_seconds"])}；'
                        f'有效样本 {time_report[key]["sample_count"]} 条；'
                        f'未满足条件 {time_report[key].get("unknown_count", 0)} 条'
                    ),
                    "height": 40 + index * 12,
                }
                for index, (label, key) in enumerate((
                    ("公开至首次观测", "published_to_observed"),
                    ("公开至系统首次发现", "published_to_discovered"),
                    ("公开至规则检测完成", "published_to_detected"),
                    ("内容时间至检出", "reference_to_detected"),
                    ("采集至检测", "fetched_to_detected"),
                    ("检测至告警", "detected_to_alert"),
                    ("检测至人工确认", "detected_to_confirmation"),
                ))
            ]
            payload["timeliness_meta"] = {
                "public_time_coverage_percent": time_report.get("public_time_coverage_percent", 0),
                "public_time_eligible_count": time_report.get("public_time_eligible_count", 0),
                "public_time_ineligible_count": time_report.get("public_time_ineligible_count", 0),
                "unknown_public_time_excluded": time_report.get("unknown_public_time_excluded", 0),
                "unknown_time_counts": time_report.get("unknown_time_counts", {}),
                "note": time_report.get("note", ""),
            }
            payload["source_kind_timeliness"] = [
                {
                    "source_kind": kind,
                    "occurrence_count": details["occurrence_count"],
                    "published_to_detected": _format_seconds(
                        details["metrics"]["published_to_detected"]["p50_seconds"]
                    ),
                    "published_to_discovered": _format_seconds(
                        details["metrics"]["published_to_discovered"]["p50_seconds"]
                    ),
                    "reference_to_detected": _format_seconds(
                        details["metrics"]["reference_to_detected"]["p50_seconds"]
                    ),
                    "fetched_to_detected": _format_seconds(
                        details["metrics"]["fetched_to_detected"]["p50_seconds"]
                    ),
                    "detected_to_alert": _format_seconds(
                        details["metrics"]["detected_to_alert"]["p50_seconds"]
                    ),
                    "unknown_publication": details["unknown_time_counts"]["published_at"],
                }
                for kind, details in report.get("timeliness_by_source_kind", {}).items()
            ]
            payload["extensibility"] = [
                {
                    "label": item["title"],
                    "value": (
                        f'{"发布包运行时检查通过" if item.get("runtime_check_status") == "已通过" else "发布包运行时检查未通过"}；'
                        f'完整实验记录={"已通过" if item.get("execution_status") == "passed" else "未完整执行" if item.get("execution_status") == "not_run" else item.get("execution_status") or "未知"}；'
                        f'代码量={item["code_lines"] or "未知"}；'
                        f'工时={item["development_hours"] or "未知"}h'
                    ),
                }
                for item in experiments
            ] or [{"label": "扩展性实验", "value": "尚未建立实验记录"}]
            payload["boundaries"] = report.get("boundaries") or [
                {
                    "label": "来源分层",
                    "value": f'真实检测结果={counts["source_kind_breakdown"].get("真实检测结果", 0)}',
                },
                {
                    "label": "时间字段边界",
                    "value": (
                        f'公开时间未知 {time_report.get("unknown_time_counts", {}).get("published_at", 0)} 条；'
                        "未知时间不使用检测时间代替"
                    ),
                },
            ]
            payload["time_model"] = {
                "timezone": time_report["timezone"],
                "field_sources": time_report["field_sources"],
                "unknown_counts": report["evidence"]["unknown_time_counts"],
                "field_coverage": report.get("time_field_coverage", {}),
                "sample_count": len(report.get("time_samples", [])),
                "note": time_report["note"],
            }
            payload["time_samples"] = report.get("time_samples", [])
            return payload
        except (BackendStorageError, BackendValidationError):
            payload["source"] = "真实数据源：规范化后端 SQLite 读取失败"
            payload["implementation_status"] = "部分实现"
            payload["error"] = "指标报告读取失败；未使用旧库或桩数据替代。"
            return payload
    try:
        events = EventStore(runtime.database_path, runtime.database_timeout, runtime.database_attempts).list_events()
    except StorageUnavailable:
        payload["source"] = "真实数据源：SQLite 读取失败"
        payload["implementation_status"] = "部分实现"
        payload["error"] = "指标报告读取失败；未使用桩数据替代。"
        payload["counts"] = [
            {"label": "候选数", "value": None, "note": "SQLite不可用"},
            {"label": "确认类别数", "value": None, "note": "SQLite不可用"},
            {"label": "唯一凭据数", "value": None, "note": "SQLite不可用"},
            {"label": "泄露事件数", "value": None, "note": "SQLite不可用"},
        ]
        payload["evaluation_mapping"] = [
            {**item, "current": "不可用", "status": "SQLite读取失败"}
            for item in payload["evaluation_mapping"]
        ]
        payload["timeliness"] = [
            {"label": "公开至检测", "value": "不可用", "height": 0},
            {"label": "采集至检测", "value": "不可用", "height": 0},
            {"label": "检测至告警", "value": "不可用", "height": 0},
            {"label": "人工确认", "value": "不可用", "height": 0},
        ]
        payload["extensibility"] = [
            {**item, "value": "不可用"} for item in payload["extensibility"]
        ]
        return payload
    reportable_events = [item for item in events if item["source_kind"] == "真实检测结果"]
    channels = {(item["channel"], item["platform"]) for item in reportable_events}
    types = {item["credential_type"] for item in reportable_events}
    unique = {
        item["credential_fingerprint"]
        for item in reportable_events
        if item.get("credential_fingerprint")
    }
    confirmed = [
        item for item in reportable_events
        if item["disposition"] == "已确认公开泄露"
    ]
    payload["source"] = "真实数据：SQLite 事件记录（无公开时间字段）"
    payload["implementation_status"] = "部分实现"
    payload["error"] = None
    payload["counts"] = [
        {"label": "候选数", "value": len(reportable_events), "note": "仅统计真实检测结果；不等同公开泄露确认"},
        {"label": "确认类别数", "value": len({item["credential_type"] for item in confirmed}), "note": "处置反馈口径"},
        {"label": "唯一凭据数", "value": len(unique), "note": "带密钥 HMAC 指纹去重"},
        {"label": "泄露事件数", "value": len(confirmed), "note": "仅统计已确认公开泄露"},
    ]
    payload["evaluation_mapping"][0]["current"] = f"{len({item['channel'] for item in events})} 类 / {len(channels)} 对象"
    payload["evaluation_mapping"][0]["status"] = "已从 SQLite 重算；不代表公开渠道覆盖"
    payload["evaluation_mapping"][1]["current"] = f"{len(types)} 类"
    payload["evaluation_mapping"][1]["status"] = "已从 SQLite 重算；官方类型口径待确认"
    payload["evaluation_mapping"][3]["current"] = f"{len(unique)} 唯一 / {len(reportable_events)} 候选"
    payload["evaluation_mapping"][3]["status"] = "部分实现；确认数量按反馈记录"
    payload["timeliness"] = [
        {"label": "公开至检测", "value": "未知", "height": 12},
        {"label": "采集至检测", "value": "字段未单独记录", "height": 22},
        {"label": "检测至告警", "value": "SQLite生成时刻记录", "height": 35},
        {"label": "人工确认", "value": "反馈记录可追溯", "height": 50},
    ]
    payload["extensibility"][0]["value"] = "GitHub插件已实现（代码证据）"
    payload["extensibility"][1]["value"] = "白名单、预算、重试、checkpoint"
    payload["extensibility"][2]["value"] = "未实测"
    payload["extensibility"][3]["value"] = "见 stage27_acceptance.ps1 实际输出"
    return payload
