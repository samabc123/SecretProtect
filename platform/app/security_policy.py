"""Server-side local role policy and security audit helpers.

Authenticated accounts use a stored, server-controlled role.
Client-provided role fields and applicant-portal sessions are never trusted.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException, Request


ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "viewer": frozenset({"read"}),
    "analyst": frozenset({"read", "review", "run", "notify"}),
    "admin": frozenset({"read", "review", "run", "notify", "settings", "rules", "reveal"}),
}


def configured_role() -> str:
    value = os.getenv("APP_OPERATOR_ROLE")
    if value is None:
        try:
            from .config import settings
            from .settings_service import effective_settings

            value = effective_settings(settings).local_operator_role
        except Exception:
            value = "admin"
    value = str(value or "admin").strip().lower()
    return value if value in ROLE_PERMISSIONS else "viewer"


def actor_for_request(request: Request) -> tuple[str, str]:
    host = request.client.host if request.client else ""
    user = getattr(request.state, "user", None)
    if user and host in {"127.0.0.1", "::1", "localhost"}:
        role = user.get("role", "viewer")
        return f"user:{user['username']}", role if role in ROLE_PERMISSIONS else "viewer"
    role = "viewer"
    return f"local-{role}", role


def require_permission(request: Request, permission: str) -> tuple[str, str]:
    actor, role = actor_for_request(request)
    allowed = role == "admin" if permission == "admin" else permission in ROLE_PERMISSIONS[role]
    if not allowed:
        try:
            from .backend_pipeline import backend_store

            store = backend_store()
            store.initialize()
            store.record_security_audit(
                actor=actor,
                role=role,
                action=f"authorize:{permission}",
                resource_type="control-plane",
                decision="denied",
                reason=f"role={role}",
                source_ip=request.client.host if request.client else "",
            )
        except Exception:
            pass
        raise HTTPException(
            status_code=403,
            detail=f"当前服务器角色 {role} 不允许执行 {permission} 操作；未写入。",
        )
    if permission != "read" and request.client and request.client.host not in {"127.0.0.1", "::1", "localhost"}:
        try:
            from .backend_pipeline import backend_store

            store = backend_store()
            store.initialize()
            store.record_security_audit(
                actor=actor,
                role=role,
                action=f"authorize:{permission}",
                resource_type="control-plane",
                decision="denied",
                reason="non_loopback_request",
                source_ip=request.client.host,
            )
        except Exception:
            pass
        raise HTTPException(status_code=403, detail="控制面变更仅允许本机回环访问；未写入。")
    return actor, role


def safe_audit_meta(value: dict[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {}
    allowed = {"status_code", "source_kind", "notification_id", "rule_id", "task_id", "source_id", "reason"}
    result: dict[str, Any] = {}
    for key in allowed:
        if key in value:
            text = str(value[key])
            result[key] = text[:200]
    return result
