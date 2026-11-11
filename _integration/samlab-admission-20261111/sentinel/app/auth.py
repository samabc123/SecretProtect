"""Loopback-only browser authentication and CSRF boundary."""

from __future__ import annotations

import hmac
import os
import secrets
import sqlite3
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.templating import Jinja2Templates

from .auth_store import AuthError, AuthStore, SESSION_SECONDS
from .config import ROOT
from .frontend_shell import render_vue_app

SESSION_COOKIE = "sentinel_session"
PREFLIGHT_COOKIE = "sentinel_preflight"
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
VALIDATION_FIELDS = {
    "username": "用户名", "password": "密码", "real_name": "真实姓名",
    "person_no": "学号 / 工号", "email": "联系邮箱", "consent": "个人信息使用授权",
    "reason": "申请理由", "note": "核验 / 审批依据", "decision": "审核结果",
    "expires_at": "有效期", "verified_in_person": "当面身份核验确认",
    "confirmed": "删除确认",
    "token": "一次性激活码", "confirm_password": "确认密码",
    "current_password": "当前密码", "new_password": "新密码",
    "person_id": "人员编号", "entry_id": "白名单编号", "application_id": "申请编号",
    "request_id": "申请编号", "is_active": "账号状态",
    "account_id": "平台账号编号", "policy_version": "准入策略版本",
    "member_ids": "白名单人员列表",
}


def safe_validation_detail(error: RequestValidationError) -> dict:
    # Pydantic errors include submitted values; only return known labels and limits.
    fields = {}
    for item in error.errors():
        location = item.get("loc", ())
        field = location[1] if (
            len(location) == 2 and location[0] in {"body", "path", "query"}
            and location[1] in VALIDATION_FIELDS
        ) else "request"
        label = VALIDATION_FIELDS.get(field, "请求内容")
        kind = item.get("type")
        context = item.get("ctx") or {}
        minimum, maximum = context.get("min_length"), context.get("max_length")
        if kind == "extra_forbidden":
            field = "request"
            message = "请求包含不支持的字段，请刷新页面后重试。"
        elif kind == "json_invalid":
            message = "请求数据格式无效，请刷新页面后重试。"
        elif kind == "missing":
            message = f"请填写{label}。"
        elif kind == "string_too_short" and type(minimum) is int:
            message = f"{label}至少需要 {minimum} 个字符。"
        elif kind == "string_too_long" and type(maximum) is int:
            message = f"{label}最多允许 {maximum} 个字符。"
        elif kind == "string_type":
            message = f"{label}须为文本。"
        elif kind in {"bool_type", "bool_parsing"}:
            message = f"{label}须为是或否。"
        elif kind == "literal_error":
            message = f"{label}无效，请重新选择。"
        else:
            message = f"{label}格式无效，请检查后重试。"
        fields.setdefault(field, message)
    if not fields:
        fields["request"] = "请求内容无效，请检查后重试。"
    return {
        "detail": " ".join(fields.values()),
        "field_errors": [{"field": field, "message": message} for field, message in fields.items()],
    }


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class RegistrationBody(LoginBody):
    confirm_password: str = Field(min_length=6, max_length=128)


class PasswordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)
    confirm_password: str = Field(min_length=6, max_length=128)


class AdminCreation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=6, max_length=128)


class AdminStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_active: StrictBool


def safe_next(value: str) -> str:
    decoded = unquote(unquote(value))
    if (
        not value or not value.startswith("/") or decoded.startswith("//") or "\\" in decoded
        or any(ord(character) < 32 or ord(character) == 127 for character in decoded)
    ):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.path in {"/login", "/register"}:
        return "/"
    return value


def install_auth(app: FastAPI, templates: Jinja2Templates) -> None:
    store = AuthStore(Path(os.environ.get("AUTH_DB_PATH", str(ROOT / "data" / "auth.sqlite3"))))
    app.state.auth_store = store
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    @app.on_event("startup")
    def initialize_auth():
        store.initialize()

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, error: AuthError):
        return JSONResponse({"detail": str(error)}, status_code=error.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        if request.url.path.startswith(("/api/auth/", "/api/lab/", "/api/integration/")):
            return JSONResponse(safe_validation_detail(error), status_code=422)
        return await request_validation_exception_handler(request, error)

    @app.middleware("http")
    async def authentication(request: Request, call_next):
        request.state.user = None
        request.state.csrf_token = ""
        path = request.url.path
        if request.client and request.client.host not in LOOPBACK:
            return JSONResponse({"detail": "当前版本仅允许本机访问。"}, status_code=403)
        if path == "/lab" or path.startswith(("/lab/", "/api/lab/portal/", "/api/lab/platform/")):
            return JSONResponse({"detail": "安全平台不接受个人申请，请在 Samlab 平台提交注册请求。"}, status_code=410)
        # Integration routes have separate service signatures, never browser cookies.
        if path.startswith("/api/integration/samlab/"):
            try:
                response = await call_next(request)
            except sqlite3.Error:
                response = JSONResponse({"detail": "准入存储不可用，未放行。"}, status_code=503)
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        public = path in {"/login", "/register", "/api/auth/login", "/api/auth/register"}
        try:
            if not path.startswith("/static/"):
                session = store.session(request.cookies.get(SESSION_COOKIE, ""))
                if session:
                    request.state.user = {key: value for key, value in session.items() if key != "csrf_token"}
                    request.state.csrf_token = session["csrf_token"]
                if not public and not session:
                    if path.startswith("/api/") or path in {"/docs", "/openapi.json", "/redoc"}:
                        return JSONResponse({"detail": "请先登录。"}, status_code=401)
                    return RedirectResponse("/login?next=" + quote(path + ("?" + request.url.query if request.url.query else ""), safe=""), status_code=303)
                if (session and session["needs_password_change"] and path.startswith("/api/")
                        and path not in {"/api/auth/me", "/api/auth/password", "/api/auth/logout", "/api/auth/login"}):
                    return JSONResponse({"detail": "请先修改初始密码。"}, status_code=403)
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    expected = request.cookies.get(PREFLIGHT_COOKIE, "") if public else request.state.csrf_token
                    supplied = request.headers.get("X-CSRF-Token", "")
                    origin = request.headers.get("Origin")
                    if (
                        not expected or not hmac.compare_digest(expected.encode(), supplied.encode())
                        or (origin and origin != str(request.base_url).rstrip("/"))
                        or request.headers.get("Sec-Fetch-Site") == "cross-site"
                    ):
                        return JSONResponse({"detail": "请求校验失败，请刷新页面后重试。"}, status_code=403)
            response = await call_next(request)
        except sqlite3.Error:
            return JSONResponse({"detail": "账号数据库暂不可用，请稍后重试。"}, status_code=503)
        if not path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    def auth_page(request: Request, mode: str, next_path: str):
        if request.state.user:
            return RedirectResponse(safe_next(next_path), status_code=303)
        # Reuse the current preflight token when the browser already has one.
        # This prevents a cached/stale login document from being paired with
        # a freshly rotated HttpOnly cookie during the Vue app transition.
        csrf = request.cookies.get(PREFLIGHT_COOKIE) or secrets.token_urlsafe(32)
        vue = render_vue_app(csrf_token=csrf, mode=mode)
        if vue is not None:
            vue.set_cookie(
                PREFLIGHT_COOKIE, csrf, max_age=1200, httponly=True,
                secure=request.url.scheme == "https", samesite="strict", path="/",
            )
            return vue
        response = templates.TemplateResponse(
            request=request, name="auth.html",
            context={"mode": mode, "csrf_token": csrf, "next_path": safe_next(next_path)},
        )
        response.set_cookie(
            PREFLIGHT_COOKIE, csrf, max_age=1200, httponly=True,
            secure=request.url.scheme == "https", samesite="strict", path="/",
        )
        return response

    @app.get("/login")
    def login_page(request: Request, next: str = "/"):
        return auth_page(request, "login", next)

    @app.get("/register")
    def register_page(request: Request):
        return RedirectResponse("/login", status_code=303)

    def signed_in(request: Request, user: dict, old_token: str = ""):
        if old_token:
            store.logout(old_token)
        token, csrf = store.create_session(user["id"], user["account_version"])
        response = JSONResponse({"user": user, "csrf_token": csrf})
        response.set_cookie(
            SESSION_COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
            secure=request.url.scheme == "https", samesite="strict", path="/",
        )
        response.delete_cookie(PREFLIGHT_COOKIE, path="/")
        return response

    @app.post("/api/auth/login")
    def login(body: LoginBody, request: Request):
        user = store.authenticate(body.username, body.password, request.client.host)
        return signed_in(request, user, request.cookies.get(SESSION_COOKIE, ""))

    @app.post("/api/auth/register", status_code=201)
    def register(body: RegistrationBody, request: Request):
        raise AuthError("安全平台不开放注册；管理员只能由超级管理员创建。", 403)

    @app.get("/api/auth/me")
    def me(request: Request):
        return {"user": request.state.user, "csrf_token": request.state.csrf_token}

    @app.post("/api/auth/logout")
    def logout(request: Request):
        store.logout(request.cookies.get(SESSION_COOKIE, ""))
        response = JSONResponse({"message": "已退出登录。"})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.post("/api/auth/password")
    def change_password(body: PasswordBody, request: Request):
        if body.new_password != body.confirm_password:
            raise AuthError("两次输入的新密码不一致。")
        user = store.change_password(
            request.state.user["id"], body.current_password, body.new_password, request.client.host
        )
        return signed_in(request, user)

    @app.get("/api/auth/admins")
    def list_admins(request: Request):
        return {"items": store.list_admins(request.state.user["id"])}

    @app.post("/api/auth/admins", status_code=201)
    def create_admin(body: AdminCreation, request: Request):
        return store.create_admin(request.state.user["id"], body.username, body.password)

    @app.post("/api/auth/admins/{user_id}/status")
    def admin_status(user_id: int, body: AdminStatus, request: Request):
        store.set_admin_status(request.state.user["id"], user_id, body.is_active)
        return {"message": "管理员状态已更新，原有会话已失效。"}
