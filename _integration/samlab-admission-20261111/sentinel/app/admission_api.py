"""Administrator-only admission UI and signed Samlab service contract."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError
from starlette.requests import ClientDisconnect

from .auth import safe_validation_detail
from .auth_store import AuthError
from .config import ROOT
from .frontend_shell import render_vue_app
from .samlab_admission import MAX_BULK_MEMBERS, SamlabAdmissionStore
from .security_policy import require_permission
from . import roster_import


PREFIX = "/api/integration/samlab"


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Application(Body):
    request_id: str = Field(min_length=1, max_length=100)
    username: str = Field(min_length=3, max_length=32)
    person_no: str | None = Field(default=None, max_length=64)
    email: str = Field(min_length=3, max_length=180)


class Review(Body):
    decision: Literal["approved", "rejected"]


class AdminProvision(Body):
    request_id: str = Field(min_length=1, max_length=100)
    username: str = Field(min_length=3, max_length=32)
    person_no: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=3, max_length=255)
    account_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    actor_id: int = Field(strict=True, ge=1)
    source: Literal["admin_direct", "admin_invite"]
    display_username: str = Field(max_length=100)


class Removal(Body):
    note: str = Field(min_length=5, max_length=500)
    confirmed: StrictBool


class BatchRemoval(Removal):
    member_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        min_length=1, max_length=MAX_BULK_MEMBERS)


class ImportCommit(Body):
    batch_id: str = Field(pattern=r"^[A-Za-z0-9_-]{32}$")


class Acknowledgement(Body):
    account_id: str = Field(min_length=1, max_length=128)
    policy_version: int = Field(strict=True, ge=1)
    state: Literal["active", "disabled"]


def integration_ready():
    return len(os.environ.get("SAMLAB_INTEGRATION_SECRET", "").encode()) >= 32


def signing_message(timestamp: str, nonce: str, method: str, path: str, body: bytes) -> bytes:
    return "\n".join((timestamp, nonce, method, path, hashlib.sha256(body).hexdigest())).encode()


def install_admission(app: FastAPI):
    store = SamlabAdmissionStore(Path(os.environ.get("ADMISSION_DB_PATH", str(ROOT / "data" / "admission.sqlite3"))))
    app.state.admission_store = store

    @app.on_event("startup")
    def initialize():
        store.initialize()

    def admin(request: Request):
        actor, _ = require_permission(request, "admin")
        if request.state.user["needs_password_change"]:
            raise AuthError("请先修改初始密码。", 403)
        return actor

    async def service(request: Request):
        if not integration_ready():
            raise AuthError("Samlab 服务签名尚未配置，真实接入未启用。", 503)
        timestamp = request.headers.get("X-Samlab-Timestamp", "")
        nonce = request.headers.get("X-Samlab-Nonce", "")
        signature = request.headers.get("X-Samlab-Signature", "")
        if (not re.fullmatch(r"[0-9]{10}", timestamp)
                or abs(time.time() - int(timestamp)) > 300
                or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", nonce)
                or not re.fullmatch(r"[a-f0-9]{64}", signature) or request.url.query):
            raise AuthError("服务签名无效或已过期。", 401)
        body = bytearray()
        try:
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 16384:
                    raise AuthError("请求内容超过允许大小。", 413)
        except ClientDisconnect:
            raise AuthError("请求内容未完整接收，未执行准入操作。", 400) from None
        expected = hmac.new(
            os.environ["SAMLAB_INTEGRATION_SECRET"].encode(),
            signing_message(timestamp, nonce, request.method, request.url.path, bytes(body)), hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise AuthError("服务签名无效或已过期。", 401)
        store.consume_nonce(nonce)
        return bytes(body)

    def parse(model, raw: bytes):
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise AuthError("请求须为有效 JSON 对象。") from None
        try:
            return model.model_validate(value)
        except ValidationError as error:
            from fastapi.exceptions import RequestValidationError
            errors = [{**entry, "loc": ("body", *entry["loc"])} for entry in error.errors()]
            return JSONResponse(safe_validation_detail(RequestValidationError(errors)), status_code=422)

    @app.middleware("http")
    async def admission_errors(request: Request, call_next):
        if not request.url.path.startswith(("/api/lab/admin/", PREFIX + "/")):
            return await call_next(request)
        try:
            return await call_next(request)
        except sqlite3.Error:
            return JSONResponse({"detail": "准入数据存储暂不可用，未放行。"}, status_code=503)

    @app.get("/admission")
    @app.get("/administrators")
    def page(request: Request, actor: str = Depends(admin)):
        response = render_vue_app(csrf_token=request.state.csrf_token)
        if response is None:
            raise AuthError("请先构建前端。", 503)
        return response

    @app.get("/api/lab/admin/dashboard")
    def dashboard(actor: str = Depends(admin)):
        health = store.health()
        return {**store.dashboard(), "integration": {
            **health, "signature_configured": integration_ready(),
            "platform_connected": False, "mode": "isolated_candidate",
        }}

    @app.get("/api/lab/admin/roster/template")
    def roster_template(actor: str = Depends(admin)):
        return Response("\ufeff用户名,邮箱,ID\r\n", media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="samlab-roster.csv"',
                                 "Cache-Control": "no-store"})

    @app.post("/api/lab/admin/roster/preview")
    async def roster_preview(request: Request, actor: str = Depends(admin)):
        if request.headers.get("content-type", "").split(";")[0].lower() not in {"text/csv", "application/octet-stream"}:
            raise AuthError("请上传 CSV 名单。", 415)
        raw = bytearray()
        try:
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > roster_import.MAX_BYTES:
                    raise AuthError("名单文件不能超过 1 MiB。", 413)
        except ClientDisconnect:
            raise AuthError("文件未完整接收，未执行导入。") from None
        return roster_import.preview(store, bytes(raw), actor)

    @app.post("/api/lab/admin/roster/commit")
    def roster_commit(body: ImportCommit, actor: str = Depends(admin)):
        return roster_import.commit(store, body.batch_id, actor)

    @app.post("/api/lab/admin/requests/{request_id}/review")
    def review(request_id: str, body: Review, actor: str = Depends(admin)):
        return store.review(request_id, body.decision, actor)

    @app.post("/api/lab/admin/members/batch-delete")
    def batch_delete(body: BatchRemoval, actor: str = Depends(admin)):
        if not body.confirmed:
            raise AuthError("请确认删除所选白名单人员。")
        return store.delete_members(body.member_ids, actor, body.note)

    @app.post("/api/lab/admin/members/{member_id}/delete")
    def delete(member_id: str, body: Removal, actor: str = Depends(admin)):
        if not body.confirmed:
            raise AuthError("请确认删除该白名单人员。")
        return store.delete_members([member_id], actor, body.note)

    @app.post(PREFIX + "/requests")
    async def receive(raw: bytes = Depends(service)):
        body = parse(Application, raw)
        if isinstance(body, JSONResponse):
            return body
        return store.submit(**body.model_dump())

    @app.post(PREFIX + "/admin-provision")
    async def admin_provision(raw: bytes = Depends(service)):
        body = parse(AdminProvision, raw)
        if isinstance(body, JSONResponse):
            return body
        return store.admin_provision(**body.model_dump())

    @app.get(PREFIX + "/requests/{request_id}")
    def status(request_id: str, raw: bytes = Depends(service)):
        return store.status(request_id)

    @app.post(PREFIX + "/requests/{request_id}/acknowledgement")
    async def acknowledge(request_id: str, raw: bytes = Depends(service)):
        body = parse(Acknowledgement, raw)
        if isinstance(body, JSONResponse):
            return body
        return store.acknowledge(request_id, **body.model_dump())
