"""Release-only visibility policy for author-owned test configurations."""

import json

from .config import ROOT

POLICY_PATH = ROOT / "release-task-policy.json"


def policy() -> dict:
    # Fail closed if a shipped policy is damaged; do not expose author tasks.
    value = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("release_task_policy_invalid")
    return value


def visible_tasks(items: list[dict]) -> list[dict]:
    hidden = {int(value) for value in policy()["hidden_task_ids"]}
    return [item for item in items if int(item["id"]) not in hidden]


def visible_sources(items: list[dict]) -> list[dict]:
    hidden = {int(value) for value in policy()["hidden_source_ids"]}
    return [item for item in items if int(item["id"]) not in hidden]


def notice() -> str:
    return str(policy()["notice"])


def require_available_task(task_id: int) -> None:
    from .backend_store import BackendValidationError

    if int(task_id) in {int(value) for value in policy()["hidden_task_ids"]}:
        raise BackendValidationError(
            "作者预置测试任务未随包开放运行。请自行新建任务并填写本机路径；"
            "恢复预置任务的方法见 README。"
        )
