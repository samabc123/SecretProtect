from fastapi import Depends, FastAPI, Request, Path
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from typing import Annotated, Literal

from .auth_store import AuthError
from .frontend_shell import render_vue_app
from .protection import ProtectionStore
from .security_policy import require_permission


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["new", "confirmed", "false_positive", "resolved"]


class Removal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: StrictBool
    note: str = Field(min_length=5, max_length=300)


RecordId = Annotated[int, Path(ge=1, le=9223372036854775807)]


def install_protection(app: FastAPI):
    store = ProtectionStore()
    app.state.protection_store = store

    @app.on_event("startup")
    def initialize():
        store.initialize()

    def admin(request: Request):
        return require_permission(request, "admin")[0]

    @app.get("/attacks")
    @app.get("/decisions")
    @app.get("/engine")
    @app.get("/security-audit")
    def page(request: Request, actor=Depends(admin)):
        response = render_vue_app(csrf_token=request.state.csrf_token)
        if response is None:
            raise AuthError("前端尚未构建。", 503)
        return response

    @app.get("/api/protection/status")
    def status(actor=Depends(admin)):
        return store.status()

    @app.get("/api/protection/events")
    def events(actor=Depends(admin)):
        return {"items": store.events()}

    @app.get("/api/protection/decisions")
    def decisions(actor=Depends(admin)):
        return {"items": store.decisions()}

    @app.get("/api/protection/audit")
    def audit(actor=Depends(admin)):
        return {"items": store.audits()}

    @app.get("/api/protection/notifications")
    def notifications(actor=Depends(admin)):
        return {"items": store.notifications()}

    @app.post("/api/protection/sync")
    def sync(actor=Depends(admin)):
        return store.sync(actor)

    @app.patch("/api/protection/events/{identity}")
    def review(identity: RecordId, body: Review, actor=Depends(admin)):
        store.review(identity, body.status, actor)
        return {"message": "处置状态已更新。"}

    @app.post("/api/protection/decisions/{identity}/delete")
    def remove(identity: RecordId, body: Removal, actor=Depends(admin)):
        if not body.confirmed:
            raise AuthError("请确认解封操作。")
        return store.delete_decision(identity, actor, body.note)
