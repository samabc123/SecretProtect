"""规范化后端 REST API。

旧页面接口仍由 app.main 提供；本路由组提供参考项目式的统一来源、任务、
发现项、验证队列、规则、能力矩阵和导出接口。
"""

from __future__ import annotations

import json
import platform
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from .backend_pipeline import backend_store, format_detection_note, run_task
from .backend_scheduler import run_due_once
from .backend_store import BackendStorageError, BackendValidationError
from .time_utils import parse_timestamp
from .config import ROOT
from .detection.registry import get_registry
from .verification_engine import verifier_specs, verify_credential
from .settings_service import effective_settings
from .security_policy import actor_for_request, configured_role, require_permission, safe_audit_meta
from .experiment_runner import run_extensibility_experiments as execute_extensibility_experiments


from .release_policy import visible_sources, visible_tasks

router = APIRouter(prefix="/api")

# These historical test batches remain available to evidence and finding
# queries, but are intentionally omitted from the user-facing run audit.
HIDDEN_AUDIT_RUN_IDS = frozenset({8, 12, 13, 14, 15, 68, 88, 119})

# Keep the coverage matrix in the same business order used by the product:
# Wiki, Weibo, code hosting, knowledge sharing, container images, App, and
# mini programs. The aliases cover older persisted labels without changing
# their stored values.
COVERAGE_CHANNEL_ORDER = (
    "Wiki",
    "微博",
    "代码托管平台",
    "知识共享平台",
    "容器镜像",
    "App",
    "小程序",
)
COVERAGE_CHANNEL_ALIASES = {
    "代码托管": "代码托管平台",
    "知识共享": "知识共享平台",
    "知识分享": "知识共享平台",
    "应用包": "App",
}


def coverage_channel_rank(value: Any) -> tuple[int, str]:
    label = str(value or "").strip()
    canonical = COVERAGE_CHANNEL_ALIASES.get(label, label)
    try:
        rank = COVERAGE_CHANNEL_ORDER.index(canonical)
    except ValueError:
        rank = len(COVERAGE_CHANNEL_ORDER)
    return rank, canonical


def _store():
    store = backend_store()
    store.initialize()
    return store


def _error(exc: Exception) -> None:
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, BackendValidationError):
        mapping = {
            "source_not_found": (404, "来源不存在"),
            "task_not_found": (404, "任务不存在"),
            "scan_run_not_found": (404, "扫描批次不存在"),
            "credential_not_found": (404, "发现项不存在"),
            "backend_not_found": (404, "资源不存在"),
            "collector_not_supported": (400, "该微博公开页面连接器已移除，请使用微博（官方 OAuth API）"),
            "task_already_running": (409, "任务正在运行，未重复启动"),
            "source_disabled": (409, "来源已禁用，未启动扫描"),
            "github_repository_outside_allowlist": (403, "GitHub 目标仓库不在授权白名单"),
            "gitee_repository_outside_allowlist": (403, "Gitee 目标仓库不在授权白名单"),
            "credential_value_unavailable": (409, "当前发现项没有可恢复的原文；仅保留脱敏证据"),
            "source_config_must_not_contain_secrets": (400, "来源配置不能包含 Token、密码、密钥等敏感字段；请在系统设置中配置"),
            "credential_hmac_key_required": (400, "尚未配置凭据 HMAC 密钥；请进入“系统设置 → 平台授权”保存密钥后再运行"),
            "network_connector_disabled": (400, "联网连接器已关闭；本地 wxapkg 使用 path 时无需联网，Stack Overflow 等公开 API 需要开启联网"),
            "public_redirect_not_allowed": (400, "公开来源返回了跳转，但当前连接器未允许该跳转；请使用已支持的来源地址或更新对应连接器"),
            "public_redirect_invalid": (400, "公开来源返回了无效跳转，未启动扫描"),
            "public_redirect_limit_exceeded": (400, "公开来源跳转次数超过安全限制，未启动扫描"),
            "robots_access_denied": (403, "目标站点的 robots.txt 禁止当前连接器抓取；不能通过登录、伪装爬虫或绕过访客系统继续访问"),
            "robots_unavailable_or_denied": (403, "目标站点未提供可接受的 robots.txt 访问结果，当前连接器拒绝抓取"),
            "weibo_authorized_token_required": (400, "微博 OAuth Access Token 未配置；请在系统设置中保存，不要写入来源 JSON"),
            "weibo_authorized_uid_invalid": (400, "微博 OAuth 来源的 UID 必须是数字"),
            "weibo_authorized_post_ids_invalid": (400, "微博 OAuth 来源的 post_id 或 post_ids 格式无效"),
            "weibo_authorized_target_required": (400, "微博 OAuth 来源必须提供 uid 或 post_id"),
            "weibo_authorized_include_long_text_invalid": (400, "include_long_text 必须是 JSON 布尔值"),
            "weibo_authorized_api_unauthorized": (401, "微博 OAuth Access Token 无效、过期或已撤销"),
            "weibo_authorized_api_forbidden": (403, "微博 OAuth API 拒绝访问，请检查授权范围"),
            "weibo_authorized_api_not_found": (404, "微博 OAuth API 找不到目标内容或接口"),
            "weibo_authorized_api_rate_limited": (429, "微博 OAuth API 返回限流，请稍后重试"),
            "weibo_authorized_api_request_failed": (502, "微博 OAuth API 返回非预期状态"),
            "weibo_authorized_api_invalid_json": (502, "微博 OAuth API 返回内容不是有效 JSON"),
            "weibo_authorized_api_invalid_response": (502, "微博 OAuth API 返回结构不符合连接器契约"),
            "weibo_authorized_api_response_too_large": (413, "微博 OAuth API 返回内容超过大小预算"),
            "weibo_authorized_api_network_error": (502, "微博 OAuth API 请求失败或网络不可达"),
            "weibo_authorized_api_endpoint_invalid": (400, "微博 OAuth API 地址不是受控官方端点"),
            "weibo_authorized_api_method_invalid": (400, "微博 OAuth API 请求方法不符合连接器契约"),
            "weibo_authorized_api_no_matching_status": (404, "微博 OAuth API 没有返回符合授权范围的微博内容"),
            "wxapkg_path_or_url_required": (400, "wxapkg 配置必须提供 path 或 url，且只能提供其中一个"),
            "wxapkg_file_unavailable": (400, "wxapkg 文件不存在或无法读取；请检查路径和文件权限"),
            "wxapkg_encrypted_or_invalid": (400, "wxapkg 不是可读取的未加密包；请使用自己拥有或明确授权取得的未加密 wxapkg"),
            "wxapkg_header_invalid": (400, "wxapkg 包头无效；请重新导出合法的未加密 wxapkg"),
            "wxapkg_index_truncated": (400, "wxapkg 索引不完整；文件可能损坏，请重新导出"),
            "wxapkg_entry_invalid": (400, "wxapkg 文件索引项无效；请重新导出"),
            "wxapkg_entry_out_of_bounds": (400, "wxapkg 文件索引越界；文件可能损坏，请重新导出"),
            "wxapkg_empty": (400, "wxapkg 中没有可读取的文件项"),
            "notification_id_invalid": (400, "通知记录标识无效"),
            "experiment_identity_required": (400, "实验记录缺少唯一标识、连接器或标题"),
            "experiment_status_invalid": (400, "实验状态不符合契约"),
            "experiment_not_found": (404, "扩展性实验不存在"),
            "event_sort_invalid": (400, "事件排序参数无效"),
            "evidence_check_type_invalid": (400, "证据核验类型不受支持"),
            "evidence_check_status_invalid": (400, "证据核验状态无效"),
            "evidence_check_value_invalid": (400, "证据核验字段过长"),
            "evidence_artifact_invalid": (400, "证据包登记字段无效"),
            "authorized_verification_endpoints_invalid": (400, "授权验证端点必须是最多 100 项的 JSON 数组"),
            "authorized_verification_endpoint_invalid": (400, "授权验证端点配置项格式无效"),
            "authorized_verification_endpoint_id_invalid": (400, "授权验证端点标识无效或重复"),
            "authorized_verification_endpoint_identity_invalid": (400, "授权验证端点缺少系统或平台标识"),
            "authorized_verification_supported_types_invalid": (400, "授权验证端点缺少支持的凭据类型"),
            "authorized_verification_endpoint_url_invalid": (400, "授权验证端点地址无效，不能包含凭据、查询参数或片段"),
            "authorized_verification_endpoint_https_required": (400, "授权验证端点只允许 HTTPS 或本机 HTTP"),
            "authorized_verification_allowed_hosts_invalid": (400, "授权验证端点缺少主机白名单"),
            "authorized_verification_endpoint_host_not_allowlisted": (400, "端点主机必须包含在允许访问的主机白名单中"),
            "authorized_verification_method_invalid": (400, "授权验证端点只支持 GET 或 POST"),
            "authorized_verification_auth_placement_invalid": (400, "授权信息放置方式无效"),
            "authorized_verification_auth_name_invalid": (400, "授权字段名无效"),
            "authorized_verification_auth_scheme_invalid": (400, "授权方式无效"),
            "authorized_verification_success_condition_invalid": (400, "成功响应判断规则无效"),
            "authorized_verification_success_condition_requires_json_path": (400, "成功响应必须包含 JSON 字段判断"),
            "authorized_verification_failure_condition_invalid": (400, "失败响应判断规则无效"),
            "authorized_verification_identity_fields_invalid": (400, "身份字段路径无效"),
            "authorized_verification_timeout_invalid": (400, "授权验证超时时间必须在 1 到 120 秒之间"),
        }
        status, detail = mapping.get(str(exc), (400, str(exc)))
        raise HTTPException(status, detail)
    if isinstance(exc, BackendStorageError):
        raise HTTPException(503, "统一后端存储不可用；未使用桩数据替代")
    raise HTTPException(500, "统一后端执行失败；已停止本次操作")


@router.get("/overview/stats")
def backend_overview_stats():
    try:
        return _store().overview_stats()
    except Exception as exc:
        _error(exc)


@router.get("/sources")
def list_sources():
    try:
        return visible_sources(_store().list_sources())
    except Exception as exc:
        _error(exc)


@router.post("/sources")
def create_source(body: dict[str, Any], request: Request):
    try:
        require_permission(request, "settings")
        result = _store().create_source(body)
        _audit(request, "source.create", "source", str(result["id"]), "allowed")
        return result
    except Exception as exc:
        _error(exc)


@router.patch("/sources/{source_id}")
def update_source(source_id: int, body: dict[str, Any], request: Request):
    try:
        require_permission(request, "settings")
        result = _store().update_source(source_id, body)
        if result is None:
            raise HTTPException(404, "来源不存在")
        _audit(request, "source.update", "source", str(source_id), "allowed", {"source_id": source_id})
        return result
    except HTTPException:
        raise
    except Exception as exc:
        _error(exc)


@router.delete("/sources/{source_id}")
def delete_source(source_id: int, request: Request):
    try:
        require_permission(request, "settings")
        if not _store().delete_source(source_id):
            raise HTTPException(404, "来源不存在")
        _audit(request, "source.delete", "source", str(source_id), "allowed", {"source_id": source_id})
        return {"ok": True, "source_id": source_id}
    except HTTPException:
        raise
    except Exception as exc:
        _error(exc)


@router.post("/sources/{source_id}/run")
def run_source(source_id: int, request: Request):
    try:
        require_permission(request, "run")
        store = _store()
        source = store.get_source(source_id)
        if source is None:
            raise HTTPException(404, "来源不存在")
        task = source.get("task")
        if not task:
            raise HTTPException(409, "来源没有关联任务")
        result = run_task(int(task["id"]), "manual")
        _audit(request, "source.run", "source", str(source_id), "allowed", {"source_id": source_id})
        return result
    except HTTPException:
        raise
    except Exception as exc:
        _error(exc)


@router.get("/tasks")
def list_tasks():
    try:
        return visible_tasks(_store().list_tasks())
    except Exception as exc:
        _error(exc)


def _task_action(task_id: int, action: str, request: Request):
    try:
        require_permission(request, "run")
        result = _store().task_action(task_id, action)
        if result is None:
            raise HTTPException(404, "任务不存在")
        _audit(request, f"task.{action}", "task", str(task_id), "allowed", {"task_id": task_id})
        return result
    except HTTPException:
        raise
    except Exception as exc:
        _error(exc)


@router.post("/tasks/{task_id}/pause")
def pause_task(task_id: int, request: Request):
    return _task_action(task_id, "pause", request)


@router.post("/tasks/{task_id}/resume")
def resume_task(task_id: int, request: Request):
    return _task_action(task_id, "resume", request)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int, request: Request):
    return _task_action(task_id, "cancel", request)


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: int, request: Request):
    return _task_action(task_id, "retry", request)


@router.post("/tasks/{task_id}/run")
def run_task_api(task_id: int, request: Request):
    try:
        require_permission(request, "run")
        result = run_task(task_id, "manual")
        _audit(request, "task.run", "task", str(task_id), "allowed", {"task_id": task_id})
        return result
    except Exception as exc:
        _error(exc)


@router.get("/tasks/{task_id}/runs")
def task_runs(task_id: int):
    try:
        if _store().get_task(task_id) is None:
            raise HTTPException(404, "任务不存在")
        return _store().list_runs(task_id=task_id)
    except HTTPException:
        raise
    except Exception as exc:
        _error(exc)


@router.get("/backend-runs")
def backend_runs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    include_failed: bool = False,
):
    try:
        store = _store()
        tasks = {int(item["id"]): item for item in store.list_tasks()}
        all_items = []
        for run in store.list_runs(limit=500):
            if int(run["id"]) in HIDDEN_AUDIT_RUN_IDS:
                continue
            task = tasks.get(int(run["task_id"]))
            started = parse_timestamp(run["started_at"])
            finished = parse_timestamp(run["finished_at"])
            duration_seconds = (
                round((finished - started).total_seconds(), 3)
                if started and finished and finished >= started
                else None
            )
            stats = run.get("stats", {})
            candidates = int(stats.get("candidates", 0))
            inserted = int(stats.get("new_occurrences", 0))
            filtered = int(stats.get("skipped_occurrences", max(0, candidates - inserted)))
            is_failure = run["status"] == "failed" or bool(stats.get("error_code"))
            all_items.append({
                "run_id": f"backend-{run['id']}",
                "connector_name": task["collector_key"] if task else "unknown",
                "target": task["source_name"] if task else f"task-{run['task_id']}",
                "started_at": run["started_at"],
                "finished_at": run["finished_at"],
                "duration_seconds": duration_seconds,
                "status": (
                    "succeeded" if run["status"] == "completed"
                    else "running" if run["status"] == "running"
                    else "failed"
                ),
                "candidates": candidates,
                "inserted": inserted,
                "filtered": filtered,
                "items_seen": int(stats.get("items_seen", 0) or 0),
                "content_items_written": int(stats.get("content_items_written", 0) or 0),
                "content_items_existing": int(stats.get("content_items_existing", 0) or 0),
                "detection_note": format_detection_note(stats),
                "filtered_reason": (
                    format_detection_note(stats)
                    if filtered
                    else None
                ),
                "new_credentials": int(stats.get("new_credentials", 0)),
                "task_id": str(run["task_id"]),
                "_is_failure": is_failure,
            })
        failed = [item for item in all_items if item["_is_failure"]]
        for item in all_items:
            item.pop("_is_failure", None)
        visible = all_items if include_failed else [
            item for item in all_items
            if item["status"] != "failed"
        ]
        total = len(visible)
        total_pages = (total + page_size - 1) // page_size if total else 0
        current_page = min(page, total_pages) if total_pages else 1
        start = (current_page - 1) * page_size
        return {
            "source": "真实数据：规范化后端 SQLite scan_runs",
            "implementation_status": "部分实现",
            "error": None,
            "items": visible[start:start + page_size],
            "summary": {
                "total_runs": len(all_items),
                "visible_runs": total,
                "failed_runs": len(failed),
                "hidden_failed_runs": 0 if include_failed else len(failed),
                "success_with_filtering": sum(
                    1 for item in all_items
                    if item["status"] == "succeeded" and item["filtered"] > 0
                ),
                "failure_time_range": {
                    "oldest": min(
                        (item["started_at"] for item in failed),
                        default=None,
                    ),
                    "newest": max(
                        (item["started_at"] for item in failed),
                        default=None,
                    ),
                },
            },
            "pagination": {
                "page": current_page,
                "page_size": page_size,
                "total_items": total,
                "total_pages": total_pages,
            },
        }
    except Exception as exc:
        _error(exc)


@router.get("/findings")
def list_findings(
    q: str | None = None,
    type: str | None = None,
    review: str | None = None,
    verify: str | None = None,
    platform: str | None = None,
    source_kind: str | None = Query("真实检测结果"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    try:
        return _store().list_findings(
            query=q,
            credential_type=type,
            review=review,
            verify=verify,
            platform=platform,
            source_kind=source_kind,
            page=page,
            page_size=page_size,
        )
    except Exception as exc:
        _error(exc)


@router.get("/findings/{credential_id}")
def finding_detail(credential_id: int):
    try:
        result = _store().finding_detail(credential_id)
        if result is None:
            raise HTTPException(404, "发现项不存在")
        return result
    except HTTPException:
        raise
    except Exception as exc:
        _error(exc)


@router.post("/findings/{credential_id}/reveal")
def reveal_finding(credential_id: int, request: Request):
    try:
        require_permission(request, "reveal")
        client_host = request.client.host if request.client else ""
        if client_host not in {"127.0.0.1", "::1", "localhost"}:
            raise HTTPException(403, "原文揭示仅允许本机回环访问")
        result = _store().reveal(credential_id)
        _audit(request, "finding.reveal", "credential", str(credential_id), "allowed")
        return result
    except HTTPException:
        raise
    except Exception as exc:
        _error(exc)


@router.post("/findings/{credential_id}/review")
def review_finding(credential_id: int, body: dict[str, Any], request: Request):
    try:
        require_permission(request, "review")
        result = _store().review(
            credential_id,
            str(body.get("action") or ""),
            str(body.get("note") or ""),
        )
        _audit(request, "finding.review", "credential", str(credential_id), "allowed")
        return result
    except Exception as exc:
        _error(exc)


@router.post("/findings/batch-review")
def batch_review(body: dict[str, Any], request: Request):
    require_permission(request, "review")
    raw_ids = body.get("ids") or []
    if not isinstance(raw_ids, list) or len(raw_ids) > 500:
        raise HTTPException(400, "ids 必须是最多 500 个整数")
    ids: list[int] = []
    for value in raw_ids:
        if isinstance(value, bool):
            raise HTTPException(400, "ids 必须是最多 500 个整数")
        try:
            credential_id = int(value)
        except (TypeError, ValueError):
            raise HTTPException(400, "ids 必须是最多 500 个整数") from None
        if credential_id <= 0:
            raise HTTPException(400, "ids 必须是正整数")
        if credential_id not in ids:
            ids.append(credential_id)
    ok: list[int] = []
    failed: list[dict[str, Any]] = []
    store = _store()
    for credential_id in ids:
        try:
            result = store.review(
                credential_id,
                str(body.get("action") or ""),
                str(body.get("note") or ""),
            )
            ok.append(credential_id)
        except Exception as exc:
            failed.append({"id": credential_id, "error": str(exc)})
    _audit(request, "finding.batch_review", "credential", "", "allowed", {"reason": f"count={len(ok)}"})
    return {"ok": ok, "failed": failed}


@router.post("/findings/{credential_id}/verify")
def verify_finding(credential_id: int, request: Request):
    try:
        require_permission(request, "run")
        result = _store().enqueue_verification(credential_id, "single")
        _audit(request, "finding.verify_enqueue", "credential", str(credential_id), "allowed")
        return result
    except Exception as exc:
        _error(exc)


@router.post("/findings/batch-verify")
def batch_verify(body: dict[str, Any], request: Request):
    require_permission(request, "run")
    raw_ids = body.get("ids") or []
    if not isinstance(raw_ids, list) or len(raw_ids) > 500:
        raise HTTPException(400, "ids 必须是最多 500 个整数")
    ids: list[int] = []
    for value in raw_ids:
        if isinstance(value, bool):
            raise HTTPException(400, "ids 必须是最多 500 个整数")
        try:
            credential_id = int(value)
        except (TypeError, ValueError):
            raise HTTPException(400, "ids 必须是最多 500 个整数") from None
        if credential_id <= 0:
            raise HTTPException(400, "ids 必须是正整数")
        if credential_id not in ids:
            ids.append(credential_id)
    queued: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    store = _store()
    for credential_id in ids:
        try:
            queued.append(store.enqueue_verification(credential_id, "batch"))
        except Exception as exc:
            failed.append({"id": credential_id, "error": str(exc)})
    _audit(request, "finding.batch_verify_enqueue", "credential", "", "allowed", {"reason": f"count={len(queued)}"})
    return {"queued": queued, "failed": failed}


@router.get("/verification/jobs")
def verification_jobs(status: str | None = None):
    try:
        return {
            "source": "真实数据：规范化后端 SQLite verification_jobs",
            "implementation_status": "部分实现",
            "error": None,
            "items": _store().list_verification_jobs(status),
        }
    except Exception as exc:
        _error(exc)


@router.get("/verification/history")
def verification_history(credential_id: int | None = None):
    try:
        return {
            "source": "真实数据：规范化后端 SQLite verification_results",
            "implementation_status": "部分实现",
            "error": None,
            "items": _store().list_verification_history(credential_id),
        }
    except Exception as exc:
        _error(exc)


@router.get("/notifications")
def notifications(status: str | None = None):
    try:
        store = _store()
        return {
            "source": "真实数据：规范化后端 SQLite 离线通知队列",
            "implementation_status": "部分实现",
            "error": None,
            "items": store.list_notifications(status=status),
        }
    except Exception as exc:
        _error(exc)


@router.post("/notifications/{notification_id}/record")
def record_notification(notification_id: str, request: Request):
    try:
        require_permission(request, "notify")
        result = _store().record_notification(notification_id)
        if result is None:
            raise HTTPException(404, "通知记录不存在")
        response = {
            "source": "真实数据：规范化后端 SQLite 离线通知队列",
            "implementation_status": "部分实现",
            "error": None,
            "item": result,
        }
        _audit(request, "notification.record", "notification", notification_id, "allowed", {"notification_id": notification_id})
        return response
    except HTTPException:
        raise
    except Exception as exc:
        _error(exc)


@router.post("/notifications/{notification_id}/retry")
def retry_notification(notification_id: str, request: Request):
    try:
        require_permission(request, "notify")
        result = _store().retry_notification(notification_id)
        if result is None:
            raise HTTPException(404, "通知记录不存在")
        _audit(request, "notification.retry", "notification", notification_id, "allowed", {"notification_id": notification_id})
        return {
            "source": "真实数据：规范化后端 SQLite 离线通知队列",
            "implementation_status": "部分实现",
            "error": None,
            "item": result,
        }
    except HTTPException:
        raise
    except Exception as exc:
        _error(exc)


@router.get("/evaluation-report")
def evaluation_report():
    try:
        return _store().evaluation_report()
    except Exception as exc:
        _error(exc)


@router.get("/evidence/report")
def evidence_report(limit: int = Query(200, ge=1, le=500)):
    try:
        return _store().evidence_report(limit=limit)
    except Exception as exc:
        _error(exc)


@router.get("/evidence/runs")
def evidence_runs(
    collector_key: str | None = None,
    limit: int = Query(200, ge=1, le=500),
):
    try:
        return {
            "source": "真实数据：统一后端 SQLite run_evidence",
            "implementation_status": "已实现",
            "error": None,
            "items": _store().list_run_evidence(collector_key=collector_key, limit=limit),
        }
    except Exception as exc:
        _error(exc)


@router.post("/evidence/checks")
def create_evidence_check(body: dict[str, Any], request: Request):
    try:
        require_permission(request, "run")
        result = _store().record_evidence_check(body)
        _audit(
            request,
            "evidence.check",
            "scan_run",
            str(body.get("run_id") or ""),
            "allowed",
            {"check_type": body.get("check_type"), "status": body.get("status")},
        )
        return result
    except Exception as exc:
        _error(exc)


@router.post("/evidence/artifacts")
def create_evidence_artifact(body: dict[str, Any], request: Request):
    try:
        require_permission(request, "run")
        result = _store().register_evidence_artifact(body)
        _audit(
            request,
            "evidence.artifact",
            "scan_run",
            str(body.get("run_id") or ""),
            "allowed",
            {"artifact_id": result.get("artifact_id")},
        )
        return result
    except Exception as exc:
        _error(exc)


@router.post("/verification/process")
def process_verification(request: Request):
    try:
        require_permission(request, "run")
        result = _store().process_verification_jobs()
        _audit(request, "verification.process", "verification", "", "allowed")
        return result
    except Exception as exc:
        _error(exc)


@router.post("/verification/test-configured-token")
def test_configured_token(request: Request, body: dict[str, Any] | None = None):
    """Verify the current locally configured GitHub/Gitee token transiently.

    This is intentionally separate from a discovered finding: the configured
    platform token is an explicit operator-owned setting and must not create a
    finding, verification job, history row, or public-leak statistic.
    """
    try:
        require_permission(request, "run")
        provider = str((body or {}).get("provider") or "").strip().lower()
        type_by_provider = {"github": "github_pat", "gitee": "gitee_token"}
        credential_type = type_by_provider.get(provider)
        if not credential_type:
            raise HTTPException(400, "provider 只能是 github 或 gitee")
        runtime = effective_settings()
        secret = runtime.github_token if provider == "github" else runtime.gitee_token
        if not secret:
            raise HTTPException(409, f"{provider} 当前未配置 Token")
        result = verify_credential(
            credential_type,
            secret,
            timeout_seconds=runtime.verification_timeout_seconds,
        )
        outcome = {
            "status": result.status,
            "evidence": {
                **result.evidence,
                "configured_token": True,
                "verification_mode": "configured_platform_token",
                "request_sent": True,
            },
            "latency_ms": result.latency_ms,
        }
        _audit(
            request,
            "verification.test_configured_token",
            "platform_token",
            provider,
            "allowed",
            {"provider": provider, "status": outcome["status"]},
        )
        return {
            "source": "当前平台授权设置（瞬时验证，不写入发现项历史）",
            "implementation_status": "已实现",
            "provider": provider,
            "credential_type": credential_type,
            "queue_written": False,
            "history_written": False,
            "next_action": "如需写入验证队列和历史，请在统一发现项中点击对应记录的“验证”，再在验证中心处理排队任务。",
            **outcome,
        }
    except HTTPException:
        raise
    except Exception as exc:
        _error(exc)


@router.get("/channels")
def channels(
    q: str | None = None,
    channel: str | None = None,
    platform: str | None = None,
    status: str | None = None,
):
    try:
        store = _store()
        from .connectors import list_connector_specs

        support_rows = store.list_support_matrix("channel")
        collector_write_summary = store.overview_stats().get("collector_write_summary", [])
        cumulative_writes = {
            str(item.get("collector_key") or "").strip().replace("-", "_"): int(item.get("count") or 0)
            for item in collector_write_summary
            if str(item.get("collector_key") or "").strip()
        }
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
        support_by_pair = {
            (str(item.get("channel_type") or ""), str(item.get("platform") or "")): item
            for item in support_rows
        }
        offline_tested_connectors = {
            "authorized-local-directory",
            "authorized-jsonl-export",
            "controlled-fixtures",
            "github-authorized-rest",
            "gitee-authorized-rest",
            "mediawiki-public-api",
            "generic-web-rss",
            "generic-web-atom",
            "generic-web-sitemap",
            "generic-web-url",
            "stackoverflow-public-api",
            "weibo-authorized-api",
            "oci-registry",
            "npm-registry",
            "pypi-registry",
            "huggingface-public-repository",
            "crates-public-registry",
            "nuget-public-registry",
            "rubygems-public-registry",
            "android-apk",
            "wxapkg-unencrypted",
        }
        collectors = []
        for raw in list_connector_specs():
            item = dict(raw)
            pair = (
                str(item.get("channel") or ""),
                str(item.get("platform") or ""),
            )
            support = support_by_pair.get(pair, {})
            connector_name = str(item.get("name") or "")
            offline_tested = connector_name in offline_tested_connectors
            has_completed_run = bool(
                support.get("tested_at") or support.get("test_run_id")
            )
            real_platform_tested = bool(support.get("real_tested"))
            item["implementation_status"] = (
                "已实现"
                if "实现" in str(item.get("status") or "") or offline_tested
                else "计划支持"
            )
            item["offline_test_status"] = "已验证" if offline_tested else "未验证"
            item["real_platform_test_status"] = (
                "已实测可运行"
                if real_platform_tested or has_completed_run
                else "未实测"
            )
            item["status"] = (
                "已实测可运行"
                if real_platform_tested or has_completed_run
                else "已实现未实测"
            )
            collector_key = connector_keys.get(connector_name, connector_name.replace("-", "_"))
            aggregate_suffix = "（RSS/Atom/Sitemap/URL 合计）" if collector_key == "generic_web" else ""
            item["evidence_ref"] = (
                f"实测完成，累计写入 {cumulative_writes.get(collector_key, 0)} 条{aggregate_suffix}"
                if has_completed_run
                else "已完成真实平台实测"
                if real_platform_tested
                else "代码测试通过，尚未运行实际任务"
                if offline_tested
                else "已实现，尚未运行实际任务"
            )
            collectors.append(item)

        filter_options = {
            "channel": sorted({
                str(item.get("channel") or "")
                for item in collectors
                if item.get("channel")
            }, key=lambda channel: (
                coverage_channel_rank(channel)[0],
                coverage_channel_rank(channel)[1],
                channel,
            )),
            "platform": sorted({
                str(item.get("platform") or "")
                for item in collectors
                if item.get("platform")
            }),
            "status": sorted({
                str(item.get("status") or "")
                for item in collectors
                if item.get("status")
            }),
        }
        normalized_query = str(q or "").strip().casefold()
        collectors = [
            item for item in collectors
            if (
                not normalized_query
                or normalized_query in " ".join(
                    str(item.get(key) or "")
                    for key in ("name", "channel", "platform", "status", "evidence_ref")
                ).casefold()
            )
            and (not channel or item.get("channel") == channel)
            and (not platform or item.get("platform") == platform)
            and (not status or item.get("status") == status)
        ]
        collectors.sort(
            key=lambda item: (
                coverage_channel_rank(item.get("channel"))[0],
                coverage_channel_rank(item.get("channel"))[1],
                str(item.get("platform") or ""),
                str(item.get("name") or ""),
            )
        )
        public_support_rows = [
            item for item in support_rows
            if str(item.get("collection_method") or "")
            in {"online-public", "online-authorized"}
        ]
        coverage = {
            "configured_channel_type_count": len({
                str(item.get("channel_type") or "")
                for item in support_rows
                if item.get("channel_type")
            }),
            "configured_platform_count": len({
                str(item.get("platform") or "")
                for item in support_rows
                if item.get("platform")
            }),
            "public_channel_type_count": len({
                str(item.get("channel_type") or "")
                for item in public_support_rows
                if item.get("channel_type")
            }),
            "public_platform_count": len({
                str(item.get("platform") or "")
                for item in public_support_rows
                if item.get("platform")
            }),
            "observed_channel_type_count": None,
            "observed_platform_count": None,
            "definition": "支持矩阵统计配置覆盖；真实发现统计由总览/指标报告单独返回。",
        }

        return {
            "source": "真实数据：规范化后端 SQLite capabilities/support_matrix",
            "implementation_status": "部分实现",
            "error": None,
            "capabilities": store.list_capabilities(),
            "collectors": collectors,
            "coverage": coverage,
            "filters": filter_options,
            "support_matrix": store.list_support_matrix(),
            "extensibility_experiments": store.list_extensibility_experiments(),
            "verifiers": [
                *verifier_specs(effective_settings().authorized_verification_endpoints),
                {
                    "id": "offline-unavailable",
                    "title": "离线验证队列",
                    "version": "1.0",
                    "supported_types": [],
                    "endpoint": "不访问第三方业务接口",
                },
            ],
        }
    except Exception as exc:
        _error(exc)


@router.get("/rules")
def rules():
    try:
        return _store().list_rules()
    except Exception as exc:
        _error(exc)


@router.patch("/rules/{rule_id}")
def patch_rule(rule_id: str, body: dict[str, Any], request: Request):
    try:
        require_permission(request, "rules")
        enabled = body.get("enabled")
        if type(enabled) is not bool:
            raise HTTPException(400, "enabled 必须是 JSON 布尔值")
        result = _store().set_rule_enabled(rule_id, enabled)
        if result is None:
            raise HTTPException(404, "规则不存在")
        _audit(request, "rule.update", "rule", rule_id, "allowed", {"rule_id": rule_id})
        return result
    except HTTPException:
        raise
    except Exception as exc:
        _error(exc)


@router.get("/export/findings.json")
def export_findings_json(masked: bool = True):
    try:
        if not masked:
            raise HTTPException(400, "统一后端只允许脱敏导出；masked=false 被拒绝")
        return {
            "source": "规范化后端 SQLite",
            "implementation_status": "已实现",
            "masked": True,
            "items": _store().export_findings(masked=True),
        }
    except HTTPException:
        raise
    except Exception as exc:
        _error(exc)


@router.get("/export/findings.csv")
def export_findings_csv(masked: bool = True):
    try:
        if not masked:
            raise HTTPException(400, "统一后端只允许脱敏导出；masked=false 被拒绝")
        return PlainTextResponse(
            _store().export_findings_csv(masked=True),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="findings.csv"'},
        )
    except HTTPException:
        raise
    except Exception as exc:
        _error(exc)


@router.get("/export/evidence-report.json")
def export_evidence_report(limit: int = Query(500, ge=1, le=500)):
    try:
        return _store().evidence_report(limit=limit)
    except Exception as exc:
        _error(exc)


@router.get("/system/info")
def system_info():
    try:
        store = _store()
        return {
            "app": "云上凭据泄露自动化检测系统",
            "backend_version": "2.0-unified-sqlite",
            "python": platform.python_version(),
            "database": str(backend_store().path),
            "database_exists": backend_store().path.exists(),
            "rules_loaded": len(get_registry().rules),
            "capabilities": len(store.list_capabilities()),
            "collectors": [
                "local_dir",
                "controlled_fixtures",
                "authorized_jsonl",
                "github",
                "gitee",
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
            ],
            "verifiers": [
                item["id"]
                for item in verifier_specs(effective_settings().authorized_verification_endpoints)
            ],
            "network_policy": "采集和验证均默认关闭联网；必须在设置页显式开启并限制授权范围",
            "secret_policy": "默认仅脱敏；统一后端不保存采集器返回的原文",
            "operator_role": configured_role(),
            "permissions": "viewer只读；analyst可复核/运行/记录离线通知；admin可修改设置/规则/实验记录/原文揭示（仍限本机）",
        }
    except Exception as exc:
        _error(exc)


@router.post("/backend/initialize")
def initialize_backend(request: Request):
    try:
        require_permission(request, "settings")
        store = _store()
        _audit(request, "backend.initialize", "backend", "", "allowed")
        return {
            "ok": True,
            "database": str(store.path),
            "rules": len(store.list_rules()),
            "capabilities": len(store.list_capabilities()),
        }
    except Exception as exc:
        _error(exc)


@router.post("/backend/run-due")
def run_due_backend_tasks(request: Request):
    try:
        require_permission(request, "run")
        result = run_due_once()
        _audit(request, "scheduler.run_due", "scheduler", "", "allowed")
        return {
            "ok": True,
            "source": "真实数据：统一后端调度器",
            "implementation_status": "部分实现",
            "result": result,
        }
    except Exception as exc:
        _error(exc)


@router.get("/support-matrix")
def support_matrix(item_kind: str | None = None):
    try:
        return {
            "source": "真实数据：规范化后端 SQLite support_matrix",
            "implementation_status": "部分实现",
            "error": None,
            "items": _store().list_support_matrix(item_kind),
        }
    except Exception as exc:
        _error(exc)


@router.get("/extensibility-experiments")
def extensibility_experiments():
    try:
        return {
            "source": "真实数据：规范化后端 SQLite extensibility_experiments",
            "implementation_status": "部分实现",
            "error": None,
            "items": _store().list_extensibility_experiments(),
        }
    except Exception as exc:
        _error(exc)


@router.post("/extensibility-experiments")
def create_extensibility_experiment(body: dict[str, Any], request: Request):
    try:
        require_permission(request, "admin")
        item = _store().upsert_extensibility_experiment(body)
        _audit(request, "experiment.upsert", "experiment", str(item["experiment_key"]), "allowed")
        return {
            "source": "真实数据：规范化后端 SQLite extensibility_experiments",
            "implementation_status": "部分实现",
            "error": None,
            "item": item,
        }
    except Exception as exc:
        _error(exc)


@router.post("/extensibility-experiments/run")
def run_extensibility_experiments_api(request: Request):
    try:
        require_permission(request, "admin")
        items = execute_extensibility_experiments(_store())
        _audit(request, "experiment.run_offline", "experiment", "", "allowed", {"reason": f"count={len(items)}"})
        return {
            "source": "真实数据：发布包运行时 smoke check 写入 SQLite 实验表",
            "implementation_status": "部分实现",
            "error": None,
            "real_platform_tested": False,
            "items": items,
        }
    except Exception as exc:
        _error(exc)


@router.post("/extensibility-experiments/{experiment_key:path}/run")
def run_one_extensibility_experiment(experiment_key: str, request: Request):
    try:
        require_permission(request, "admin")
        items = execute_extensibility_experiments(
            _store(),
            experiment_key=experiment_key,
        )
        _audit(
            request,
            "experiment.run_offline_one",
            "experiment",
            experiment_key,
            "allowed",
        )
        return {
            "source": "真实数据：单项发布包运行时检查写入 SQLite 实验表",
            "implementation_status": "部分实现",
            "error": None,
            "real_platform_tested": False,
            "items": items,
        }
    except ValueError as exc:
        if str(exc) == "experiment_not_found":
            raise HTTPException(404, "扩展性实验不存在")
        _error(exc)
    except Exception as exc:
        _error(exc)


@router.get("/security/audit")
def security_audit(limit: int = Query(200, ge=1, le=1000)):
    try:
        return {
            "source": "真实数据：规范化后端 SQLite security_audit",
            "implementation_status": "部分实现",
            "error": None,
            "items": _store().list_security_audit(limit),
        }
    except Exception as exc:
        _error(exc)


def _audit(
    request: Request,
    action: str,
    resource_type: str,
    resource_id: str,
    decision: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    actor, role = actor_for_request(request)
    try:
        _store().record_security_audit(
            actor=actor,
            role=role,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            decision=decision,
            source_ip=request.client.host if request.client else "",
            metadata=safe_audit_meta(metadata),
        )
    except Exception:
        # A secondary audit failure must not expose database details or turn a
        # completed primary operation into a false failure response.
        return
