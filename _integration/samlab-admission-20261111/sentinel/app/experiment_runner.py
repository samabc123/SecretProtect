"""可复查的连接器扩展性实验 runner。

实验只使用发布包中的 ``app/`` 代码和隔离 SQLite 临时库，不导入
``tests/``，也不把预设布尔值当成运行结果。Codeberg 案例使用注入式
HTTP 传输夹具执行完整的采集、检测、入库、去重、断点和失败恢复链路；
真实公网平台是否访问过，单独由来源任务的 run_evidence 证明。
"""

from __future__ import annotations

import difflib
import importlib
import json
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlsplit

from .backend_store import BackendStore
from .connector_runs import ConnectorRunStore
from .public_connectors import (
    collect_codeberg_repository,
    collect_weibo_authorized_api,
)
from .public_http import HttpResult, PublicConnectorError


@dataclass(frozen=True)
class ExperimentCase:
    platform_markers: tuple[str, ...]
    source_files: tuple[str, ...]
    config_fields: int
    evidence_ref: str
    functional_test: bool = False


EXPERIMENT_CASES: tuple[ExperimentCase, ...] = (
    ExperimentCase(("本地目录",), ("app/local_scan.py", "app/content_detection.py"), 5, "app/local_scan.py；app/content_detection.py"),
    ExperimentCase(("受控测试载体", "本地授权夹具"), ("app/controlled_detector.py",), 4, "app/controlled_detector.py"),
    ExperimentCase(("通用 JSONL 导出",), ("app/connectors.py",), 4, "app/connectors.py"),
    ExperimentCase(("GitHub（授权 API）",), ("app/github_connector.py", "app/connector_runs.py"), 14, "app/github_connector.py；app/connector_runs.py"),
    ExperimentCase(("Gitee（授权 API）",), ("app/gitee_connector.py", "app/connector_runs.py"), 14, "app/gitee_connector.py；app/connector_runs.py"),
    ExperimentCase(("MediaWiki",), ("app/public_connectors.py", "app/public_http.py"), 6, "app/public_connectors.py::collect_mediawiki"),
    ExperimentCase(("RSS", "Atom", "Sitemap", "URL", "RSS/Atom/Sitemap/URL"), ("app/public_connectors.py", "app/public_http.py"), 8, "app/public_connectors.py::collect_generic_web"),
    ExperimentCase(("Stack Overflow",), ("app/public_connectors.py", "app/public_http.py"), 5, "app/public_connectors.py::collect_stackoverflow"),
    ExperimentCase(("Codeberg",), ("app/public_connectors.py", "app/connectors.py", "app/backend_pipeline.py", "app/backend_store.py"), 3, "app/public_connectors.py::collect_codeberg_repository", True),
    ExperimentCase(("OCI Registry",), ("app/public_connectors.py", "app/public_http.py"), 7, "app/public_connectors.py::collect_oci_registry"),
    ExperimentCase(("npm Registry",), ("app/public_connectors.py", "app/public_http.py"), 4, "app/public_connectors.py::collect_npm_registry"),
    ExperimentCase(("PyPI",), ("app/public_connectors.py", "app/public_http.py"), 4, "app/public_connectors.py::collect_pypi_registry"),
    ExperimentCase(("Hugging Face Hub",), ("app/public_connectors.py", "app/public_http.py"), 4, "app/public_connectors.py::collect_huggingface_repo"),
    ExperimentCase(("crates.io",), ("app/public_connectors.py", "app/public_http.py"), 4, "app/public_connectors.py::collect_crates_registry"),
    ExperimentCase(("NuGet",), ("app/public_connectors.py", "app/public_http.py"), 4, "app/public_connectors.py::collect_nuget_registry"),
    ExperimentCase(("RubyGems",), ("app/public_connectors.py", "app/public_http.py"), 4, "app/public_connectors.py::collect_rubygems_registry"),
    ExperimentCase(("Android APK",), ("app/public_connectors.py",), 4, "app/public_connectors.py::collect_apk"),
    ExperimentCase(("未加密 wxapkg",), ("app/public_connectors.py",), 4, "app/public_connectors.py::collect_wxapkg"),
    ExperimentCase(
        ("微博（官方 OAuth API）",),
        ("app/public_connectors.py",),
        6,
        "app.public_connectors.py::collect_weibo_authorized_api",
        True,
    ),
)


def _code_lines(root: Path, relative_files: Iterable[str]) -> int:
    total = 0
    for relative in relative_files:
        path = root / relative
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        total += sum(
            1
            for line in lines
            if line.strip() and not line.lstrip().startswith("#")
        )
    return total


def _latest_baseline_file(root: Path, relative: str) -> Path | None:
    candidates = sorted(
        (
            path / "app" / relative.removeprefix("app/")
            for path in (root / "backups").glob("before-evidence-chain*")
        ),
        key=lambda item: item.parent.parent.name,
        reverse=True,
    )
    return next((item for item in candidates if item.is_file()), None)


def _changed_code_lines(root: Path, relative_files: Iterable[str]) -> int | None:
    total = 0
    found_baseline = False
    for relative in relative_files:
        current_path = root / relative
        baseline_path = _latest_baseline_file(root, relative)
        if not current_path.is_file() or baseline_path is None:
            continue
        try:
            current = current_path.read_text(encoding="utf-8").splitlines()
            baseline = baseline_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        found_baseline = True
        diff = difflib.ndiff(baseline, current)
        total += sum(
            1
            for line in diff
            if line.startswith("+ ")
            and line[2:].strip()
            and not line[2:].lstrip().startswith("#")
        )
    return total if found_baseline else None


def _find_case(connector_key: str) -> ExperimentCase | None:
    return next(
        (
            case
            for case in EXPERIMENT_CASES
            if any(marker in connector_key for marker in case.platform_markers)
        ),
        None,
    )


def _builtin_smoke_check(
    case: ExperimentCase,
    *,
    root: Path,
) -> tuple[bool, int, int, str]:
    """Load only release implementation modules; never import test files."""
    started = time.perf_counter()
    missing = [
        relative
        for relative in case.source_files
        if not (root / relative).is_file()
    ]
    if missing:
        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        return False, 0, duration_ms, f"实现文件缺失：{', '.join(missing)}"
    try:
        module_names = {
            "app/local_scan.py": "app.local_scan",
            "app/content_detection.py": "app.content_detection",
            "app/controlled_detector.py": "app.controlled_detector",
            "app/connectors.py": "app.connectors",
            "app/github_connector.py": "app.github_connector",
            "app/gitee_connector.py": "app.gitee_connector",
            "app/public_connectors.py": "app.public_connectors",
            "app/public_http.py": "app.public_http",
            "app/connector_runs.py": "app.connector_runs",
            "app/backend_pipeline.py": "app.backend_pipeline",
            "app/backend_store.py": "app.backend_store",
        }
        for relative in case.source_files:
            module_name = module_names.get(relative)
            if module_name:
                importlib.import_module(module_name)
        if "Codeberg" in case.platform_markers:
            module = importlib.import_module("app.public_connectors")
            if not callable(getattr(module, "collect_codeberg_repository", None)):
                raise AttributeError("缺少 Codeberg 连接器入口")
        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        return True, 1, duration_ms, "发布包实现模块和连接器入口加载通过"
    except (ImportError, AttributeError, OSError) as error:
        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        return False, 0, duration_ms, f"发布包加载失败：{error}"


class _CodebergFixtureTransport:
    """Deterministic transport used only by the isolated Codeberg experiment."""

    def __init__(self, *, fail_first_file: bool = False):
        self.fail_first_file = fail_first_file
        self.failed = False
        self.calls: list[str] = []

    def request(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        method: str = "GET",
    ) -> HttpResult:
        del headers, method
        self.calls.append(url)
        path = unquote(urlsplit(url).path)
        if path.endswith("/api/v1/repos/demo-owner/demo-repo/git/trees/main"):
            payload = {
                "sha": "tree-demo-v1",
                "tree": [
                    {"path": "config.env", "type": "blob", "sha": "blob-config-v1"},
                    {"path": "README.md", "type": "blob", "sha": "blob-readme-v1"},
                ],
            }
            return HttpResult(
                200,
                {"content-type": "application/json"},
                json.dumps(payload).encode(),
            )
        if "/raw/main/" in path:
            if self.fail_first_file and not self.failed:
                self.failed = True
                return HttpResult(503, {}, b"")
            name = path.rsplit("/raw/main/", 1)[-1]
            body = (
                b"AWS_ACCESS_KEY_ID=AKIAQ5F2ZK7WLMXEDR9T\n"
                if name == "config.env"
                else b"Documentation file without a credential.\n"
            )
            return HttpResult(200, {"content-type": "text/plain"}, body)
        return HttpResult(404, {}, b"")


class _WeiboAuthorizedFixtureTransport:
    """Deterministic OAuth gateway fixture for the offline Weibo experiment."""

    def __init__(self):
        self.calls: list[dict[str, object]] = []

    def request(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        method: str = "POST",
        body: dict[str, object] | None = None,
    ) -> HttpResult:
        if (
            url != "https://open.weibo.com/cli/api/cli/invoke"
            or method != "POST"
            or not isinstance(body, dict)
        ):
            return HttpResult(400, {}, b"{}")
        args = body.get("args")
        self.calls.append({
            "url": url,
            "method": method,
            "authorization_header_present": bool(
                isinstance(headers, dict) and headers.get("Authorization")
            ),
            "action": body.get("action"),
            "args": args if isinstance(args, dict) else {},
        })
        payload = {
            "result": {
                "statuses": [
                    {
                        "idstr": "9000000000001",
                        "created_at": "Mon Oct 19 10:00:00 +0800 2026",
                        "text": (
                            "微博 OAuth API 离线功能样例\n"
                            "SR_WEIBO_TEST_TOKEN_A1B2C3D4E5F6G7H8J9K0L1M2"
                        ),
                        "user": {"idstr": "7845697819"},
                    }
                ]
            }
        }
        return HttpResult(
            200,
            {"content-type": "application/json"},
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )


class _WorkspaceTemporaryDirectory:
    """Use a normal workspace directory instead of a restricted system temp ACL."""

    def __init__(self, root: Path, prefix: str):
        parent = root / "test_artifacts" / ".experiment-tmp"
        parent.mkdir(parents=True, exist_ok=True)
        self.path = parent / f"{prefix}{uuid.uuid4().hex[:10]}"
        self.path.mkdir(parents=True, exist_ok=False)

    def __enter__(self) -> str:
        return str(self.path)

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _run_weibo_authorized_functional_experiment(root: Path) -> dict:
    started = time.perf_counter()
    config = {
        "uid": "7845697819",
        "include_long_text": True,
        "max_items": 1,
    }
    fixture_token = "offline-fixture-token"
    fixture_hmac_key = "offline-fixture-hmac-key"
    transport = _WeiboAuthorizedFixtureTransport()
    checks: dict[str, dict[str, object]] = {}
    with _WorkspaceTemporaryDirectory(root, "secretradar-weibo-") as temporary:
        temp_root = Path(temporary)
        store = BackendStore(
            temp_root / "backend.sqlite3",
            master_key_path=temp_root / "backend.master.key",
        )
        store.initialize()
        source = store.create_source({
            "name": "微博 OAuth API 离线实验隔离来源",
            "collector_key": "weibo_authorized_api",
            "config": config,
            "mode": "manual",
            "interval_sec": 300,
            "enabled": True,
        })
        task_id = int(source["task"]["id"])
        source_id = int(source["id"])
        run = store.create_run(task_id, "extension_weibo_authorized_offline")
        events = collect_weibo_authorized_api(
            config,
            access_token=fixture_token,
            hmac_key=fixture_hmac_key,
            timeout_seconds=1,
            transport=transport,
        )
        content_items = list(getattr(events, "content_items", []) or [])
        content_counts = store.ingest_content_items(
            source_id,
            run["id"],
            content_items,
        )
        ingest_counts = store.ingest_events(source_id, run["id"], events)
        stats = {
            "items_seen": int(getattr(events, "items_seen", 0)),
            "content_items_seen": content_counts["content_items_seen"],
            "content_items_written": content_counts["content_items_written"],
            "candidates": len(events),
            **ingest_counts,
        }
        store.finish_run(run["id"], "completed", stats)
        store.record_run_evidence(
            run["id"],
            events,
            stats,
            status="completed",
        )
        serialized = json.dumps(
            {"events": list(events), "content_items": content_items},
            ensure_ascii=False,
        )
        request = transport.calls[0] if transport.calls else {}
        checks["request_contract"] = {
            "status": (
                "passed"
                if request.get("action") == "user_timeline/other"
                and request.get("authorization_header_present") is True
                and request.get("args", {}).get("uid") == "7845697819"
                else "failed"
            ),
            "detail": "离线请求使用固定官方网关、POST 和 UID 范围；访问令牌只通过运行时参数传入。",
        }
        checks["collection"] = {
            "status": "passed" if stats["items_seen"] == 1 else "failed",
            "detail": f"离线响应解析出 {stats['items_seen']} 个微博内容项。",
        }
        rule_ids = {
            str(event.get("_rule_id") or "")
            for event in events
            if not event.get("evidence", {}).get("rejected_candidate")
        }
        checks["detection"] = {
            "status": (
                "passed"
                if stats["candidates"] == 1
                and rule_ids == {"weibo-authorized-test-token"}
                else "failed"
            ),
            "detail": (
                f"微博正文形成 {stats['candidates']} 个规则候选，"
                f"命中规则={','.join(sorted(rule_ids)) or '无'}。"
            ),
        }
        checks["content_ingestion"] = {
            "status": (
                "passed"
                if stats["content_items_written"] == 1
                and stats["new_occurrences"] >= 1
                else "failed"
            ),
            "detail": (
                f"隔离 SQLite 写入内容版本 {stats['content_items_written']} 个，"
                f"新增出现位置 {stats['new_occurrences']} 个。"
            ),
        }
        checks["timestamp_and_version"] = {
            "status": (
                "passed"
                if content_items
                and content_items[0].get("version_id") == "weibo:9000000000001"
                and content_items[0].get("published_at")
                else "failed"
            ),
            "detail": "created_at 已解析并与微博状态版本绑定。",
        }
        checks["secret_boundary"] = {
            "status": "passed" if fixture_token not in serialized else "failed",
            "detail": "访问令牌未进入连接器事件、内容版本或实验结果。",
        }
        checks["pipeline"] = {
            "status": (
                "passed"
                if all(item["status"] == "passed" for item in checks.values())
                else "failed"
            ),
            "detail": "离线请求、解析、规则检测、内容入库、出现位置入库和敏感信息边界均已执行。",
        }
        evidence = {
            "schema_version": "1.0",
            "experiment": "微博官方 OAuth API 离线功能实验",
            "baseline": "统一 ConnectorSpec + BackendStore；人工开发工时未知",
            "completed": "当前工作树",
            "real_platform_tested": False,
            "target": "固定官方 OAuth API 网关的本地响应夹具，不访问微博公网",
            "configuration": config,
            "checks": checks,
            "stats": stats,
            "limitations": [
                "本实验只证明连接器在离线响应下可解析、检测和入库。",
                "微博真实授权可用性仍由来源任务的 completed 运行证据单独证明。",
            ],
        }
    artifact_dir = root / "test_artifacts" / "extensibility"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / "weibo-authorized-api-experiment-result.json"
    artifact_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log_path = artifact_dir / "weibo-authorized-api-experiment.log"
    log_lines = [
        "微博官方 OAuth API 离线功能实验",
        f"executed_at={datetime.now(timezone.utc).isoformat()}",
        f"duration_ms={round((time.perf_counter() - started) * 1000, 3)}",
    ]
    for name, check in checks.items():
        log_lines.append(f"{name}: {check['status']} - {check['detail']}")
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    passed = all(item["status"] == "passed" for item in checks.values())
    return {
        "status": "passed" if passed else "failed",
        "checks_run": len(checks),
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "checks": checks,
        "artifact_path": str(artifact_path.relative_to(root)),
        "log_path": str(log_path.relative_to(root)),
        "code_lines": _changed_code_lines(root, ("app/public_connectors.py",)),
    }


def _run_codeberg_functional_experiment(root: Path) -> dict:
    started = time.perf_counter()
    config = {
        "repository": "demo-owner/demo-repo",
        "revision": "main",
        "max_items": 2,
    }
    checks: dict[str, dict[str, object]] = {}
    run_log: list[dict[str, object]] = []
    with _WorkspaceTemporaryDirectory(root, "secretradar-codeberg-") as temporary:
        temp_root = Path(temporary)
        store = BackendStore(
            temp_root / "backend.sqlite3",
            master_key_path=temp_root / "backend.master.key",
        )
        store.initialize()
        source = store.create_source({
            "name": "Codeberg 扩展实验隔离来源",
            "collector_key": "codeberg",
            "config": config,
            "mode": "manual",
            "interval_sec": 300,
            "enabled": True,
        })
        task_id = int(source["task"]["id"])
        source_id = int(source["id"])
        checkpoint_store = ConnectorRunStore(temp_root / "connector_runs.sqlite3")
        checkpoint_store.get_checkpoint("codeberg:demo-owner/demo-repo:main")

        failed_run = store.create_run(task_id, "extension_failure_recovery")
        failing_transport = _CodebergFixtureTransport(fail_first_file=True)
        failure_message = ""
        try:
            collect_codeberg_repository(
                config,
                hmac_key="extension-fixture-hmac-key",
                timeout_seconds=1,
                max_file_bytes=512 * 1024,
                transport=failing_transport,
                checkpoint_store=checkpoint_store,
                defer_checkpoints=True,
                pending_checkpoints=[],
            )
        except PublicConnectorError as error:
            failure_message = str(error)
            store.finish_run(
                failed_run["id"],
                "failed",
                {"error_code": failure_message},
                failure_message,
            )
            store.record_run_evidence(
                failed_run["id"],
                [],
                {"error_code": failure_message},
                status="failed",
                error_code=failure_message,
            )
        else:
            raise AssertionError("failure injection did not fail")
        checks["failure_recovery"] = {
            "status": (
                "passed"
                if failure_message
                and not checkpoint_store.get_checkpoint(
                    "codeberg:demo-owner/demo-repo:main"
                )
                else "failed"
            ),
            "detail": "首个文件读取失败后未提交断点；失败批次已入库审计。",
        }

        successful_run = store.create_run(task_id, "extension_success")
        pending: list[tuple[str, str]] = []
        events = collect_codeberg_repository(
            config,
            hmac_key="extension-fixture-hmac-key",
            timeout_seconds=1,
            max_file_bytes=512 * 1024,
            transport=_CodebergFixtureTransport(),
            checkpoint_store=checkpoint_store,
            defer_checkpoints=True,
            pending_checkpoints=pending,
        )
        content_items = list(getattr(events, "content_items", []) or [])
        content_counts = store.ingest_content_items(
            source_id,
            successful_run["id"],
            content_items,
        )
        ingest_counts = store.ingest_events(source_id, successful_run["id"], events)
        success_stats = {
            "items_seen": int(getattr(events, "items_seen", len(content_items))),
            "content_items_seen": content_counts["content_items_seen"],
            "content_items_written": content_counts["content_items_written"],
            "candidates": len(events),
            **ingest_counts,
        }
        store.finish_run(successful_run["id"], "completed", success_stats)
        store.record_run_evidence(
            successful_run["id"],
            events,
            success_stats,
            status="completed",
        )
        for target, value in pending:
            checkpoint_store.set_checkpoint(target, value)
        run_log.append({
            "run_id": successful_run["id"],
            "kind": "success",
            "stats": success_stats,
        })
        checks["collection"] = {
            "status": "passed" if success_stats["content_items_seen"] == 2 else "failed",
            "detail": f"实际读取并登记 {success_stats['content_items_seen']} 个内容版本。",
        }
        checks["detection"] = {
            "status": "passed" if success_stats["candidates"] >= 1 else "failed",
            "detail": f"规则引擎形成 {success_stats['candidates']} 个候选。",
        }
        checks["ingestion"] = {
            "status": "passed" if success_stats["new_occurrences"] >= 1 else "failed",
            "detail": f"隔离 SQLite 新增 {success_stats['new_occurrences']} 个出现位置。",
        }

        resumed = collect_codeberg_repository(
            config,
            hmac_key="extension-fixture-hmac-key",
            timeout_seconds=1,
            max_file_bytes=512 * 1024,
            transport=_CodebergFixtureTransport(),
            checkpoint_store=checkpoint_store,
            defer_checkpoints=True,
            pending_checkpoints=[],
        )
        checks["checkpoint_resume"] = {
            "status": (
                "passed"
                if len(resumed) == 0
                and getattr(resumed, "checkpoint_status", "") == "unchanged"
                else "failed"
            ),
            "detail": "相同树版本命中已提交 checkpoint，未重复采集内容。",
        }

        duplicate_run = store.create_run(task_id, "extension_idempotency")
        duplicate_content_counts = store.ingest_content_items(
            source_id,
            duplicate_run["id"],
            content_items,
        )
        duplicate_ingest = store.ingest_events(source_id, duplicate_run["id"], events)
        duplicate_stats = {
            "items_seen": int(getattr(events, "items_seen", len(content_items))),
            "content_items_seen": duplicate_content_counts["content_items_seen"],
            "content_items_existing": duplicate_content_counts["content_items_existing"],
            "candidates": len(events),
            **duplicate_ingest,
        }
        store.finish_run(duplicate_run["id"], "completed", duplicate_stats)
        store.record_run_evidence(
            duplicate_run["id"],
            events,
            duplicate_stats,
            status="completed",
        )
        run_log.append({
            "run_id": duplicate_run["id"],
            "kind": "idempotency",
            "stats": duplicate_stats,
        })
        checks["idempotency"] = {
            "status": (
                "passed"
                if duplicate_stats["new_occurrences"] == 0
                and duplicate_stats["skipped_occurrences"] >= 1
                else "failed"
            ),
            "detail": f"重复运行跳过 {duplicate_stats['skipped_occurrences']} 个出现位置。",
        }
        checks["pipeline"] = {
            "status": (
                "passed"
                if all(item["status"] == "passed" for item in checks.values())
                else "failed"
            ),
            "detail": "采集、检测、入库、重复过滤、断点和失败恢复均有隔离运行记录。",
        }
        evidence = {
            "schema_version": "1.0",
            "experiment": "Codeberg 新监控渠道扩展实验",
            "baseline": "本轮备份目录中的修改前文件；人工开发工时未知",
            "completed": "当前工作树",
            "real_platform_tested": False,
            "target": "隔离 Codeberg API/原始文件传输夹具，不访问第三方平台",
            "configuration": config,
            "checks": checks,
            "runs": run_log + store.list_run_evidence(limit=20),
            "limitations": [
                "本实验证明新增适配器的离线可执行链路，不证明 Codeberg 真实平台已访问。",
                "公开时间未由树 API 提供，因此保持未知，不计算发现延迟。",
                "checkpoint 以仓库树 SHA 为边界；真实平台端到端 checkpoint 仍需在明确授权来源任务中验证。",
            ],
        }
    artifact_dir = root / "test_artifacts" / "extensibility"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / "codeberg-experiment-result.json"
    artifact_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log_path = artifact_dir / "codeberg-experiment.log"
    log_lines = [
        "Codeberg 新监控渠道扩展实验",
        f"executed_at={datetime.now(timezone.utc).isoformat()}",
        f"duration_ms={round((time.perf_counter() - started) * 1000, 3)}",
    ]
    for name, check in checks.items():
        log_lines.append(f"{name}: {check['status']} - {check['detail']}")
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    passed = all(item["status"] == "passed" for item in checks.values())
    return {
        "status": "passed" if passed else "failed",
        "checks_run": len(checks),
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "checks": checks,
        "artifact_path": str(artifact_path.relative_to(root)),
        "log_path": str(log_path.relative_to(root)),
        "code_lines": _changed_code_lines(
            root,
            (
                "app/public_connectors.py",
                "app/connectors.py",
                "app/backend_pipeline.py",
                "app/backend_store.py",
            ),
        ),
    }


def run_extensibility_experiments(
    store: BackendStore,
    *,
    project_root: Path | None = None,
    experiment_key: str | None = None,
) -> list[dict]:
    """Run release-only checks, optionally for one selected experiment."""
    root = (project_root or Path(__file__).resolve().parents[1]).resolve()
    store.initialize()
    results: list[dict] = []
    current_items = store.list_extensibility_experiments()
    if experiment_key:
        current_items = [
            item for item in current_items
            if str(item.get("experiment_key") or "") == str(experiment_key)
        ]
        if not current_items:
            raise ValueError("experiment_not_found")
    for current in current_items:
        case = _find_case(str(current.get("connector_key") or ""))
        if case is None:
            continue
        smoke_passed, smoke_checks, smoke_duration, smoke_note = _builtin_smoke_check(
            case,
            root=root,
        )
        functional = (
            _run_codeberg_functional_experiment(root)
            if case.functional_test and smoke_passed and "Codeberg" in case.platform_markers
            else (
                _run_weibo_authorized_functional_experiment(root)
                if case.functional_test and smoke_passed and "微博（官方 OAuth API）" in case.platform_markers
                else None
            )
        )
        if functional is not None:
            status = str(functional["status"])
            checks_run = int(functional["checks_run"]) + smoke_checks
            duration_ms = int(functional["duration_ms"]) + smoke_duration
            offline_tested = status == "passed"
            checkpoint_verified = bool(
                functional["checks"].get("checkpoint_resume", {}).get("status")
                == "passed"
            )
            failure_recovery_verified = bool(
                functional["checks"].get("failure_recovery", {}).get("status")
                == "passed"
            )
            evidence_ref = f"{case.evidence_ref}；{functional['artifact_path']}"
            notes = (
                f"{smoke_note}；功能实验状态={status}；真实平台实测=False；"
                f"结果={functional['artifact_path']}；日志={functional['log_path']}。"
            )
            code_lines = functional["code_lines"]
        else:
            status = "failed" if not smoke_passed else "not_run"
            checks_run = smoke_checks
            duration_ms = smoke_duration
            offline_tested = False
            checkpoint_verified = False
            failure_recovery_verified = False
            evidence_ref = case.evidence_ref
            notes = (
                f"{smoke_note}；该连接器没有本轮可复查的端到端实验夹具，"
                "不能把入口加载检查标为功能通过；真实平台实测未执行。"
            )
            code_lines = _changed_code_lines(root, case.source_files)
        payload = {
            "experiment_key": current["experiment_key"],
            "connector_key": current["connector_key"],
            "title": current["title"],
            "baseline_connector": current.get("baseline_connector") or "统一 ConnectorSpec + BackendStore",
            "code_lines": code_lines,
            "config_fields": case.config_fields,
            "development_hours": None,
            "test_duration_ms": duration_ms,
            "execution_status": status,
            "offline_tested": offline_tested,
            "checkpoint_verified": checkpoint_verified,
            "failure_recovery_verified": failure_recovery_verified,
            "evidence_ref": evidence_ref,
            "experiment_date": datetime.now(timezone.utc).date().isoformat(),
            "notes": (
                f"{notes} checks={checks_run}；真实授权平台测试必须通过来源任务单独记录。"
            ),
        }
        results.append(store.upsert_extensibility_experiment(payload))
    return results
