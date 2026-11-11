from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from typing import Any

from .alert_store import AlertStore
from .data_access import (
    apply_feedback,
    create_alert,
    batch_feedback,
    export_events,
    get_alerts,
    get_data,
    get_event_detail,
    get_connector_runs,
    export_event_evidence,
    mutate_monitoring_task,
    run_monitoring_task,
)
from .config import ROOT, settings
from .models import (
    AlertResponse,
    AlertsResponse,
    ConnectorRunsResponse,
    EventDetailResponse,
    EventResponse,
    FeedbackRequest,
    FeedbackResponse,
    GithubConnectionRequest,
    MonitoringTaskRequest,
    SettingsUpdateRequest,
)
from .settings_service import (
    effective_settings,
    settings_payload,
    test_github_connection,
    test_gitee_connection,
    test_gitee_token,
    update_settings,
)
from .backend_api import router as backend_router
from .backend_pipeline import backend_store
from .security_policy import require_permission, actor_for_request, safe_audit_meta
from .time_utils import format_timestamp
from .auth import install_auth
from .admission_api import install_admission
from .frontend_shell import render_vue_app


app = FastAPI(title="云上凭据泄露自动化检测系统", version="2.0.0-competition-offline")
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(ROOT / "templates"))
templates.env.filters["format_timestamp"] = format_timestamp
app.include_router(backend_router)
install_auth(app, templates)
install_admission(app)


PAGE_DATA = {
    "overview": "overview",
    "monitoring_tasks": "monitoring_tasks",
    "events": "events",
    "reports": "reports",
    "implementation_status": "implementation_status",
    "connector_runs": "connector_runs",
}


def unified_page_context(request: Request, page_title: str, active_page: str) -> dict[str, Any]:
    return page_context(
        request,
        {
            "page_title": page_title,
            "source": "真实数据：规范化后端 SQLite",
            "implementation_status": "部分实现",
            "error": None,
        },
        active_page,
    )


def page_context(request: Request, data: dict, active_page: str) -> dict:
    return {
        "request": request, "data": data, "active_page": active_page,
        "current_user": getattr(request.state, "user", None),
        "csrf_token": getattr(request.state, "csrf_token", ""),
    }


def _audit_legacy_write(
    request: Request,
    action: str,
    resource_type: str,
    resource_id: str = "",
    *,
    decision: str = "allowed",
    reason: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record old-page write outcomes in the same audit table as REST writes."""
    actor, role = actor_for_request(request)
    try:
        store = backend_store()
        store.initialize()
        store.record_security_audit(
            actor=actor,
            role=role,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            decision=decision,
            reason=reason,
            source_ip=request.client.host if request.client else "",
            metadata=safe_audit_meta(metadata),
        )
    except Exception:
        # Audit storage must not turn a completed legacy operation into a false
        # failure. Authorization denials are audited by require_permission.
        return


@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    vue = render_vue_app(csrf_token=getattr(request.state, "csrf_token", ""))
    if vue is not None:
        return vue
    return templates.TemplateResponse(
        "overview.html", page_context(request, get_data("overview"), "overview")
    )


@app.get("/monitoring-tasks", response_class=HTMLResponse)
def monitoring_tasks(request: Request, channel: str = "", status: str = "", collection_method: str = ""):
    vue = render_vue_app(csrf_token=getattr(request.state, "csrf_token", ""))
    if vue is not None:
        return vue
    data = get_data(
        "monitoring_tasks",
        channel=channel,
        status=status,
        collection_method=collection_method,
    )
    return templates.TemplateResponse(
        "monitoring_tasks.html",
        page_context(request, data, "monitoring_tasks"),
        status_code=503 if data["error"] else 200,
    )


@app.get("/events", response_class=HTMLResponse)
def events(
    request: Request,
    channel: str = "",
    credential_type: str = "",
    risk_level: str = "",
    disposition: str = "",
    source_kind: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, enum=[5, 10, 20, 50, 100]),
    sort_by: str = Query("event_id"),
    sort_order: str = Query("asc"),
):
    vue = render_vue_app(csrf_token=getattr(request.state, "csrf_token", ""))
    if vue is not None:
        return vue
    data = get_data(
        "events",
        channel=channel,
        credential_type=credential_type,
        risk_level=risk_level,
        disposition=disposition,
        source_kind=source_kind,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return templates.TemplateResponse(
        "events.html", page_context(request, data, "events"),
        status_code=503 if data["error"] else 200,
    )


@app.get("/events/{event_id}", response_class=HTMLResponse)
def event_detail(request: Request, event_id: str):
    vue = render_vue_app(csrf_token=getattr(request.state, "csrf_token", ""))
    if vue is not None:
        return vue
    data = get_event_detail(event_id)
    return templates.TemplateResponse(
        "event_detail.html",
        page_context(request, data, "events"),
        status_code=404 if data["error"] and "未找到" in data["error"] else 503 if data["error"] else 200,
    )


@app.get("/reports", response_class=HTMLResponse)
def reports(request: Request):
    vue = render_vue_app(csrf_token=getattr(request.state, "csrf_token", ""))
    if vue is not None:
        return vue
    return templates.TemplateResponse(
        "reports.html", page_context(request, get_data("reports"), "reports")
    )


@app.get("/implementation-status", response_class=HTMLResponse)
def implementation_status(request: Request):
    vue = render_vue_app(csrf_token=getattr(request.state, "csrf_token", ""))
    if vue is not None:
        return vue
    return templates.TemplateResponse(
        "implementation_status.html",
        page_context(request, get_data("implementation_status"), "implementation_status"),
    )


@app.get("/findings", response_class=HTMLResponse)
def findings_page(request: Request):
    vue = render_vue_app(csrf_token=getattr(request.state, "csrf_token", ""))
    if vue is not None:
        return vue
    return templates.TemplateResponse(
        "findings.html",
        unified_page_context(request, "统一发现项", "findings"),
    )


@app.get("/verification", response_class=HTMLResponse)
def verification_page(request: Request):
    vue = render_vue_app(csrf_token=getattr(request.state, "csrf_token", ""))
    if vue is not None:
        return vue
    return templates.TemplateResponse(
        "verification.html",
        unified_page_context(request, "验证中心", "verification"),
    )


@app.get("/channels", response_class=HTMLResponse)
def channels_page(request: Request):
    vue = render_vue_app(csrf_token=getattr(request.state, "csrf_token", ""))
    if vue is not None:
        return vue
    return templates.TemplateResponse(
        "channels.html",
        unified_page_context(request, "渠道与规则", "channels"),
    )


@app.get("/connector-runs", response_class=HTMLResponse)
def connector_runs_page(request: Request):
    vue = render_vue_app(csrf_token=getattr(request.state, "csrf_token", ""))
    if vue is not None:
        return vue
    return templates.TemplateResponse(
        "connector_runs.html",
        unified_page_context(request, "连接器运行审计", "connector_runs"),
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    vue = render_vue_app(csrf_token=getattr(request.state, "csrf_token", ""))
    if vue is not None:
        return vue
    data = settings_payload()
    return templates.TemplateResponse(
        "settings.html",
        page_context(request, data, "settings"),
        status_code=503 if data["error"] else 200,
    )


@app.get("/api/events", response_model=EventResponse)
def api_events(
    channel: str = "",
    credential_type: str = "",
    risk_level: str = "",
    disposition: str = "",
    source_kind: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, enum=[5, 10, 20, 50, 100]),
    sort_by: str = Query("event_id"),
    sort_order: str = Query("asc"),
):
    data = get_data(
        "events",
        channel=channel,
        credential_type=credential_type,
        risk_level=risk_level,
        disposition=disposition,
        source_kind=source_kind,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    if data["error"]:
        return JSONResponse(data, status_code=503)
    return data


@app.get("/api/monitoring-tasks")
def api_monitoring_tasks(channel: str = "", status: str = "", collection_method: str = ""):
    data = get_data(
        "monitoring_tasks",
        channel=channel,
        status=status,
        collection_method=collection_method,
    )
    if data["error"]:
        return JSONResponse(data, status_code=503)
    return data


@app.get("/api/settings")
def api_settings():
    data = settings_payload()
    return JSONResponse(data, status_code=503 if data["error"] else 200)


@app.put("/api/settings")
def api_update_settings(settings_request: SettingsUpdateRequest, request: Request):
    require_permission(request, "settings")
    try:
        data = update_settings(settings_request.values)
    except ValueError as error:
        settings_error_messages = {
            "authorized_verification_endpoints_invalid": "明确授权验证端点必须是最多 100 项的 JSON 数组。",
            "authorized_verification_endpoint_invalid": "明确授权验证端点配置项格式无效。",
            "authorized_verification_endpoint_id_invalid": "端点标识无效或重复。",
            "authorized_verification_endpoint_identity_invalid": "端点必须填写系统标识和平台标识。",
            "authorized_verification_supported_types_invalid": "端点必须填写至少一个凭据类型。",
            "authorized_verification_endpoint_url_invalid": "端点地址必须是 HTTPS 或本机地址，且不能包含凭据、查询参数或片段。",
            "authorized_verification_endpoint_https_required": "公网授权验证端点必须使用 HTTPS。",
            "authorized_verification_allowed_hosts_invalid": "端点必须填写允许访问的主机白名单。",
            "authorized_verification_endpoint_host_not_allowlisted": "端点主机必须包含在允许访问的主机白名单中。",
            "authorized_verification_method_invalid": "端点 HTTP 方法只支持 GET 或 POST。",
            "authorized_verification_auth_placement_invalid": "授权信息放置方式无效。",
            "authorized_verification_auth_name_invalid": "授权字段名无效。",
            "authorized_verification_auth_scheme_invalid": "授权方式无效。",
            "authorized_verification_success_condition_invalid": "成功响应判断规则无效。",
            "authorized_verification_success_condition_requires_json_path": "成功响应必须包含 JSON 字段判断，不能只依据 HTTP 200。",
            "authorized_verification_failure_condition_invalid": "失败响应判断规则无效。",
            "authorized_verification_identity_fields_invalid": "身份字段路径无效。",
            "authorized_verification_timeout_invalid": "端点超时时间必须在 1 到 120 秒之间。",
        }
        return JSONResponse(
            {
                "schema_version": "1.0",
                "source": "设置契约校验",
                "implementation_status": "部分实现",
                "error": settings_error_messages.get(str(error), str(error)),
                "changed": [],
                "runtime": {},
            },
            status_code=400,
        )
    if not data["error"]:
        actor, role = actor_for_request(request)
        try:
            backend_store().initialize()
            backend_store().record_security_audit(
                actor=actor,
                role=role,
                action="settings.update",
                resource_type="settings",
                decision="allowed",
                source_ip=request.client.host if request.client else "",
            )
        except Exception:
            pass
    return JSONResponse(data, status_code=503 if data["error"] else 200)


@app.post("/api/settings/test-github")
def api_test_github(connection_request: GithubConnectionRequest, request: Request):
    require_permission(request, "settings")
    return test_github_connection(connection_request.repository)


@app.post("/api/settings/test-gitee")
def api_test_gitee(connection_request: GithubConnectionRequest, request: Request):
    require_permission(request, "settings")
    return test_gitee_connection(connection_request.repository)


@app.post("/api/settings/test-gitee-token")
def api_test_gitee_token(request: Request):
    require_permission(request, "settings")
    result = test_gitee_token()
    _audit_legacy_write(
        request, "settings.test_gitee_token", "settings",
        decision="allowed" if result["ok"] else "failed",
    )
    return result


@app.post("/api/monitoring-tasks")
def api_create_monitoring_task(task_request: MonitoringTaskRequest, request: Request):
    require_permission(request, "settings")
    data = mutate_monitoring_task("create", payload=task_request.model_dump())
    if not data["error"]:
        actor, role = actor_for_request(request)
        try:
            backend_store().initialize()
            backend_store().record_security_audit(
                actor=actor,
                role=role,
                action="legacy.monitoring_task.create",
                resource_type="monitoring_task",
                decision="allowed",
            )
        except Exception:
            pass
    return JSONResponse(data, status_code=400 if data["error"] else 201)


@app.patch("/api/monitoring-tasks/{task_id}")
def api_update_monitoring_task(task_id: str, task_request: MonitoringTaskRequest, request: Request):
    require_permission(request, "settings")
    data = mutate_monitoring_task("update", task_id=task_id, payload=task_request.model_dump())
    if not data["error"]:
        actor, role = actor_for_request(request)
        try:
            backend_store().initialize()
            backend_store().record_security_audit(
                actor=actor,
                role=role,
                action="legacy.monitoring_task.update",
                resource_type="monitoring_task",
                resource_id=task_id,
                decision="allowed",
            )
        except Exception:
            pass
    return JSONResponse(data, status_code=404 if data["error"] and "未找到" in data["error"] else 400 if data["error"] else 200)


@app.post("/api/monitoring-tasks/{task_id}/pause")
def api_pause_monitoring_task(task_id: str, request: Request):
    require_permission(request, "run")
    data = mutate_monitoring_task("pause", task_id=task_id)
    _audit_legacy_write(
        request,
        "legacy.monitoring_task.pause",
        "monitoring_task",
        task_id,
        decision="allowed" if not data["error"] else "failed",
        reason=str(data.get("error") or ""),
    )
    return JSONResponse(data, status_code=404 if data["error"] and "未找到" in data["error"] else 400 if data["error"] else 200)


@app.post("/api/monitoring-tasks/{task_id}/resume")
def api_resume_monitoring_task(task_id: str, request: Request):
    require_permission(request, "run")
    data = mutate_monitoring_task("resume", task_id=task_id)
    _audit_legacy_write(
        request,
        "legacy.monitoring_task.resume",
        "monitoring_task",
        task_id,
        decision="allowed" if not data["error"] else "failed",
        reason=str(data.get("error") or ""),
    )
    return JSONResponse(data, status_code=404 if data["error"] and "未找到" in data["error"] else 400 if data["error"] else 200)


@app.post("/api/monitoring-tasks/{task_id}/run")
def api_run_monitoring_task(task_id: str, request: Request):
    require_permission(request, "run")
    data = run_monitoring_task(task_id)
    _audit_legacy_write(
        request,
        "legacy.monitoring_task.run",
        "monitoring_task",
        task_id,
        decision="allowed" if not data["error"] else "failed",
        reason=str(data.get("error") or ""),
        metadata={"status_code": (data.get("result") or {}).get("status", "")},
    )
    return JSONResponse(data, status_code=404 if data["error"] and "未找到" in data["error"] else 409 if data["error"] and "正在运行" in data["error"] else 400 if data["error"] else 200)


@app.delete("/api/monitoring-tasks/{task_id}")
def api_delete_monitoring_task(task_id: str, request: Request):
    require_permission(request, "settings")
    data = mutate_monitoring_task("delete", task_id=task_id)
    _audit_legacy_write(
        request,
        "legacy.monitoring_task.delete",
        "monitoring_task",
        task_id,
        decision="allowed" if not data["error"] and (data.get("task") or {}).get("deleted", True) else "failed",
        reason=str(data.get("error") or ""),
    )
    if data["error"]:
        return JSONResponse(data, status_code=400)
    if not data["task"]["deleted"]:
        return JSONResponse({"error": "未找到监控任务；未删除。", "implementation_status": "部分实现"}, status_code=404)
    return data


@app.get("/api/connector-runs", response_model=ConnectorRunsResponse)
def api_connector_runs():
    data = get_connector_runs()
    if data["error"]:
        return JSONResponse(data, status_code=503)
    return data


@app.get("/api/{resource}")
def api_resource(resource: str):
    if resource == "alerts":
        data = get_alerts()
        if data["error"]:
            return JSONResponse(data, status_code=503)
        return data
    if resource not in PAGE_DATA:
        return JSONResponse({"detail": "resource not found"}, status_code=404)
    return get_data(PAGE_DATA[resource])


@app.post("/api/events/batch-feedback")
def api_batch_feedback(body: dict[str, Any], request: Request):
    require_permission(request, "review")
    event_ids = body.get("event_ids") or []
    if not isinstance(event_ids, list) or not all(isinstance(item, str) for item in event_ids):
        _audit_legacy_write(
            request,
            "legacy.event.batch_feedback",
            "event",
            "",
            decision="failed",
            reason="event_ids_invalid",
        )
        return JSONResponse({"error": "event_ids 必须是字符串数组。"}, status_code=400)
    data = batch_feedback(event_ids, str(body.get("action") or ""), str(body.get("note") or ""))
    updated = data.get("updated") or []
    failed = data.get("failed") or []
    decision = "allowed" if updated and not failed and not data.get("error") else "partial" if updated else "failed"
    _audit_legacy_write(
        request,
        "legacy.event.batch_feedback",
        "event",
        "",
        decision=decision,
        reason=str(data.get("error") or ""),
        metadata={"reason": f"updated={len(updated)},failed={len(failed)}"},
    )
    return JSONResponse(data, status_code=400 if data["error"] and not data["updated"] else 207 if data["failed"] else 200)


@app.get("/api/events/export")
def api_events_export(event_ids: str = ""):
    selected = [item.strip() for item in event_ids.split(",") if item.strip()]
    data = export_events(selected or None)
    if data["error"]:
        return JSONResponse(data, status_code=501 if data["implementation_status"] == "未实现" else 503)
    return JSONResponse(
        data,
        headers={
            "Content-Disposition": 'attachment; filename="events-masked.json"',
            "X-Data-Source": "sqlite-events",
            "X-Implementation-Status": "partial",
        },
    )


@app.get("/api/reports/export")
def api_reports_export():
    data = get_data("reports")
    if data.get("error"):
        return JSONResponse(data, status_code=503)
    return JSONResponse(
        data,
        headers={
            "Content-Disposition": 'attachment; filename="evaluation-report.json"',
            "X-Data-Source": "reports-json",
            "X-Implementation-Status": "partial",
        },
    )


@app.get("/api/events/{event_id}", response_model=EventDetailResponse)
def api_event_detail(event_id: str):
    data = get_event_detail(event_id)
    if data["error"]:
        status = 404 if "未找到" in data["error"] else 503
        return JSONResponse(data, status_code=status)
    return data


@app.get("/api/events/{event_id}/evidence-export")
def api_event_evidence_export(event_id: str):
    data = export_event_evidence(event_id)
    if data["error"]:
        status = 404 if "未找到" in data["error"] else 503 if "读取失败" in data["error"] else 501
        return JSONResponse(data, status_code=status)
    return JSONResponse(
        data["package"],
        headers={
            "Content-Disposition": f'attachment; filename="event-{event_id}-evidence.json"',
            "X-Data-Source": "sqlite-evidence-export",
            "X-Implementation-Status": "partial",
        },
    )


@app.get("/api/alerts", response_model=AlertsResponse)
def api_alerts():
    data = get_alerts()
    if data["error"]:
        return JSONResponse(data, status_code=503)
    return data


@app.post("/api/events/{event_id}/alert", response_model=AlertResponse)
def api_create_alert(event_id: str, request: Request):
    require_permission(request, "notify")
    data = create_alert(event_id)
    _audit_legacy_write(
        request,
        "legacy.alert.create",
        "event",
        event_id,
        decision="allowed" if not data["error"] else "failed",
        reason=str(data.get("error") or ""),
    )
    if data["error"]:
        status = 404 if "未找到" in data["error"] else 503
        return JSONResponse(data, status_code=status)
    return data


@app.post("/api/alerts/{alert_id}/deliver", response_model=AlertsResponse)
def api_deliver_alert(alert_id: str, request: Request):
    require_permission(request, "notify")
    runtime = effective_settings()
    if (
        runtime.events_source == "sqlite"
        and runtime.database_path.resolve() == (ROOT / "data" / "events.sqlite3").resolve()
        and backend_store().path.exists()
    ):
        item = backend_store().record_notification(alert_id)
        if item is None:
            _audit_legacy_write(request, "legacy.alert.deliver", "alert", alert_id, decision="failed", reason="alert_not_found")
            return JSONResponse({"detail": "alert not found"}, status_code=404)
        _audit_legacy_write(request, "legacy.alert.deliver", "alert", alert_id, metadata={"status_code": item.get("status", "")})
        return get_alerts()
    store = AlertStore(effective_settings().response_database_path)
    item = store.deliver_offline(alert_id)
    if item is None:
        _audit_legacy_write(request, "legacy.alert.deliver", "alert", alert_id, decision="failed", reason="alert_not_found")
        return JSONResponse({"detail": "alert not found"}, status_code=404)
    _audit_legacy_write(request, "legacy.alert.deliver", "alert", alert_id, metadata={"status_code": item.get("status", "")})
    return get_alerts()


@app.post("/api/events/{event_id}/feedback", response_model=FeedbackResponse)
def api_event_feedback(event_id: str, feedback_request: FeedbackRequest, request: Request):
    require_permission(request, "review")
    data = apply_feedback(event_id, feedback_request)
    _audit_legacy_write(
        request,
        "legacy.event.feedback",
        "event",
        event_id,
        decision="allowed" if not data["error"] else "failed",
        reason=str(data.get("error") or ""),
        metadata={"status_code": feedback_request.action},
    )
    if data["error"]:
        status = 404 if "未找到" in data["error"] else 400 if "不符合契约" in data["error"] else 503
        return JSONResponse(data, status_code=status)
    return data


@app.get("/{full_path:path}", include_in_schema=False)
def vue_history_fallback(full_path: str, request: Request):
    """Return the Vue shell for direct navigation to a client-side route.

    API and static misses remain JSON/404 instead of accidentally returning the
    application shell. This also covers newly added frontend routes without
    requiring a matching legacy Jinja endpoint.
    """
    if full_path == "api" or full_path.startswith("api/"):
        return JSONResponse({"detail": "resource not found"}, status_code=404)
    if full_path == "static" or full_path.startswith("static/"):
        return JSONResponse({"detail": "resource not found"}, status_code=404)
    vue = render_vue_app(csrf_token=getattr(request.state, "csrf_token", ""))
    if vue is not None:
        return vue
    return JSONResponse({"detail": "resource not found"}, status_code=404)
