"""受控 Gitee API 连接器。

只支持明确授权的仓库读取，不执行搜索、登录、资源枚举或写操作。
连接器只在内存中短暂持有原文，随后交给统一检测引擎生成脱敏事件。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .content_detection import detect_content
from .time_provenance import merge_time_provenance, time_entry
from .time_utils import utc_now_iso


class GiteeConnectorError(ValueError):
    """不回显 Token、响应体或候选原文的安全连接器错误。"""

    def __init__(self, code: str, retry_after: float | None = None):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


@dataclass(frozen=True)
class GiteeConnectorConfig:
    enabled: bool = False
    token: str = ""
    repositories: tuple[str, ...] = ()
    branch: str | None = None
    api_base: str = "https://gitee.com/api/v5"
    timeout_seconds: float = 8.0
    max_files: int = 50
    max_file_bytes: int = 512 * 1024
    min_interval_seconds: float = 1.0
    max_retries: int = 2
    hmac_key: str = ""
    fingerprint_key_version: str = "v1"
    checkpoint_store: Any | None = None
    defer_checkpoints: bool = False
    include_internal: bool = False
    enabled_rule_ids: frozenset[str] | None = None


@dataclass(frozen=True)
class GiteeTransportResponse:
    payload: Any
    headers: dict[str, str]
    status_code: int = 200


def _parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - (now or datetime.now(timezone.utc))).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_repo(repo: str) -> str:
    value = (repo or "").strip().rstrip("/")
    if value.startswith(("http://", "https://")):
        parts = value.split("/")
        value = "/".join(parts[-2:])
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise GiteeConnectorError("invalid_repository_scope")
    return value


class GiteeTransport:
    def __init__(self, timeout: float):
        self.timeout = timeout

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        self._opener = urllib.request.build_opener(_NoRedirect())

    def get_json(self, url: str, headers: dict[str, str]) -> GiteeTransportResponse:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https" or parsed.netloc != "gitee.com"
            or not parsed.path.startswith("/api/v5/")
        ):
            raise GiteeConnectorError("gitee_api_base_not_allowed")
        # Gitee Open API v5 declares access_token as a query parameter.
        # Keep it inside transport: callers, audit records and errors use safe URLs.
        authorization = headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ").strip()
        query = [(key, value) for key, value in urllib.parse.parse_qsl(parsed.query) if key != "access_token"]
        if authorization.startswith("Bearer ") and token:
            query.append(("access_token", token))
        request_url = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))
        public_headers = {key: value for key, value in headers.items() if key.lower() != "authorization"}
        request = urllib.request.Request(request_url, headers=public_headers, method="GET")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return GiteeTransportResponse(
                    json.loads(response.read().decode("utf-8")),
                    {str(key): str(value) for key, value in response.headers.items()},
                    response.status,
                )
        except urllib.error.HTTPError as error:
            retry_after = _parse_retry_after(error.headers.get("Retry-After"))
            if error.code == 429 or (error.code == 403 and (
                retry_after is not None or error.headers.get("X-RateLimit-Remaining") == "0"
            )):
                raise GiteeConnectorError("gitee_rate_limited", retry_after) from None
            if error.code == 403:
                raise GiteeConnectorError("gitee_forbidden") from None
            if error.code == 401:
                raise GiteeConnectorError("gitee_unauthorized") from None
            if error.code == 404:
                raise GiteeConnectorError("gitee_not_found") from None
            raise GiteeConnectorError("gitee_http_error") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            raise GiteeConnectorError("gitee_transport_error") from None


class GiteeAuthorizedConnector:
    name = "gitee-authorized-rest"

    def __init__(
        self,
        config: GiteeConnectorConfig,
        transport: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.transport = transport or GiteeTransport(config.timeout_seconds)
        self.sleep = sleep
        self._completed_files: list[tuple[str, str, str, str]] = []
        self._completed_trees: list[tuple[str, str]] = []

    def commit_checkpoints(self) -> None:
        if not self.config.checkpoint_store:
            return
        for target, tree_sha, path, file_sha in self._completed_files:
            self.config.checkpoint_store.set_file_checkpoint(target, tree_sha, path, file_sha, "completed")
        for target, tree_sha in self._completed_trees:
            self.config.checkpoint_store.set_checkpoint(target, tree_sha)
        self._completed_files.clear()
        self._completed_trees.clear()

    def _validate(self) -> None:
        if not self.config.enabled:
            raise GiteeConnectorError("network_connector_disabled")
        if not self.config.token or len(self.config.token) < 16:
            raise GiteeConnectorError("gitee_token_missing")
        if not self.config.repositories:
            raise GiteeConnectorError("repository_allowlist_empty")
        if not self.config.hmac_key:
            raise GiteeConnectorError("credential_hmac_key_missing")
        parsed = urlparse(self.config.api_base.rstrip("/"))
        if (
            parsed.scheme != "https"
            or parsed.netloc != "gitee.com"
            or parsed.path.rstrip("/") != "/api/v5"
        ):
            raise GiteeConnectorError("gitee_api_base_not_allowed")
        if self.config.max_files < 1 or self.config.max_files > 500:
            raise GiteeConnectorError("max_files_out_of_range")
        if self.config.max_file_bytes < 1024:
            raise GiteeConnectorError("max_file_bytes_out_of_range")
        for repo in self.config.repositories:
            _safe_repo(repo)

    def _get_response(self, path: str) -> GiteeTransportResponse:
        url = path if path.startswith("https://") else self.config.api_base.rstrip("/") + path
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.config.token}",
            "User-Agent": "credential-leak-detector-authorized-connector",
        }
        for attempt in range(self.config.max_retries + 1):
            try:
                result = self.transport.get_json(url, headers)
                response = result if isinstance(result, GiteeTransportResponse) else GiteeTransportResponse(result, {})
                self.sleep(self.config.min_interval_seconds)
                return response
            except GiteeConnectorError as error:
                if error.code not in {"gitee_rate_limited", "gitee_transport_error"}:
                    raise
                if attempt >= self.config.max_retries:
                    raise
                self.sleep(min(30.0, error.retry_after if error.retry_after is not None else 2.0 ** attempt))
        raise GiteeConnectorError("gitee_transport_error")

    def _get(self, path: str) -> Any:
        return self._get_response(path).payload

    def collect(self) -> list[dict]:
        self._validate()
        results: list[dict] = []
        allowed_suffixes = {
            ".env", ".ini", ".cfg", ".conf", ".txt", ".yaml", ".yml",
            ".json", ".toml", ".pem", ".properties", ".xml",
        }
        for repo in self.config.repositories:
            owner, name = _safe_repo(repo).split("/", 1)
            encoded_repo = f"{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}"
            info = self._get(f"/repos/{encoded_repo}")
            if not isinstance(info, dict):
                raise GiteeConnectorError("gitee_invalid_response")
            branch = self.config.branch or info.get("default_branch") or "master"
            branch_path = urllib.parse.quote(str(branch), safe="")
            commit_sha = ""
            commit_at: str | None = None
            commit_evidence = ""
            try:
                head_commit = self._get(
                    f"/repos/{encoded_repo}/commits/{branch_path}"
                )
                if isinstance(head_commit, dict):
                    commit_sha = str(head_commit.get("sha") or head_commit.get("id") or "")
                    commit_payload = head_commit.get("commit") or head_commit
                    committer = commit_payload.get("committer") or {}
                    author = commit_payload.get("author") or {}
                    raw_date = (
                        committer.get("date")
                        or author.get("date")
                        or head_commit.get("committed_date")
                        or head_commit.get("created_at")
                    )
                    if raw_date:
                        parsed = datetime.fromisoformat(
                            str(raw_date).replace("Z", "+00:00")
                        )
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        commit_at = parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
                    if commit_sha:
                        commit_evidence = f"https://gitee.com/{repo}/commit/{commit_sha}"
            except (GiteeConnectorError, TypeError, ValueError):
                commit_at = None
            tree = self._get(f"/repos/{encoded_repo}/git/trees/{branch_path}?recursive=1")
            if not isinstance(tree, dict) or not isinstance(tree.get("tree"), list):
                raise GiteeConnectorError("gitee_invalid_response")
            if tree.get("truncated") is True:
                raise GiteeConnectorError("gitee_tree_truncated")
            tree_sha = str(tree.get("sha") or "")
            target_key = f"gitee:{repo}:{branch}"
            if self.config.checkpoint_store and tree_sha:
                if self.config.checkpoint_store.get_checkpoint(target_key) == tree_sha:
                    continue
            entries = [
                item for item in tree["tree"]
                if item.get("type") == "blob"
                and Path(str(item.get("path") or "")).suffix.lower() in allowed_suffixes
            ]
            if len(entries) > self.config.max_files:
                raise GiteeConnectorError("gitee_file_budget_exceeded")
            for entry in entries:
                path = str(entry.get("path") or "")
                file_sha = str(entry.get("sha") or "")
                if self.config.checkpoint_store and tree_sha and file_sha:
                    checkpoint = self.config.checkpoint_store.get_file_checkpoint(target_key, path)
                    if (
                        checkpoint
                        and checkpoint["tree_sha"] == tree_sha
                        and checkpoint["file_sha"] == file_sha
                        and checkpoint["status"] == "completed"
                    ):
                        continue
                blob = self._get(f"/repos/{encoded_repo}/git/blobs/{urllib.parse.quote(file_sha, safe='')}")
                fetched_at = utc_now_iso()
                if not isinstance(blob, dict):
                    raise GiteeConnectorError("gitee_invalid_response")
                encoded = str(blob.get("content") or "")
                try:
                    data = base64.b64decode(encoded)
                    content = data.decode("utf-8")
                except (ValueError, UnicodeError):
                    self._defer_file_checkpoint(target_key, tree_sha, path, file_sha)
                    continue
                if len(data) > self.config.max_file_bytes:
                    raise GiteeConnectorError("gitee_file_too_large")
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                records = detect_content(
                    content,
                    source_kind="真实检测结果",
                    channel="代码托管",
                    platform="Gitee（授权 API）",
                    source_locator=f"gitee:{repo}:{path}",
                    hmac_key=self.config.hmac_key,
                    fingerprint_key_version=self.config.fingerprint_key_version,
                    include_internal=self.config.include_internal,
                    enabled_rule_ids=self.config.enabled_rule_ids,
                )
                for record in records:
                    record["_version_id"] = commit_sha or tree_sha or file_sha
                    record["_version_kind"] = "commit" if commit_at else "tree"
                    record["_content_hash"] = content_hash
                    record["_content_path"] = path
                    record["_source_updated_at"] = commit_at
                    record["_source_updated_source"] = "gitee_commit" if commit_at else ""
                    record["_source_updated_confidence"] = "medium" if commit_at else "none"
                    record["_source_updated_meaning"] = (
                        "匹配文件所在仓库版本的提交时间，仅作为内容修订参考；"
                        "不等于凭据首次泄露时间。" if commit_at else ""
                    )
                    record["_source_updated_evidence"] = commit_evidence
                    record["_published_at"] = commit_at
                    record["_published_source"] = "gitee_commit" if commit_at else ""
                    record["_published_confidence"] = "medium" if commit_at else "none"
                    record["_published_evidence"] = commit_evidence
                    record["_published_meaning"] = (
                        "匹配文件所在仓库版本的提交时间，只能作为内容版本公开参考；"
                        "不能证明凭据在该提交首次出现。" if commit_at else ""
                    )
                    record["_published_role"] = "content_revision_reference" if commit_at else "unknown"
                    record["_published_eligible_for_latency"] = False
                    record["_published_unknown_reason"] = (
                        "public_appearance_not_proven" if commit_at else "commit_metadata_not_provided"
                    )
                    record["published_at"] = commit_at
                    record["published_source"] = "gitee_commit" if commit_at else ""
                    record["published_confidence"] = "medium" if commit_at else "none"
                    record["published_meaning"] = record["_published_meaning"]
                    record["published_evidence"] = commit_evidence
                    record["public_time_status"] = "reference_only" if commit_at else "unknown"
                    record["public_time_unknown_reason"] = record["_published_unknown_reason"]
                    record["source_updated_at"] = commit_at
                    record["source_updated_source"] = "gitee_commit" if commit_at else ""
                    record["source_updated_confidence"] = "medium" if commit_at else "none"
                    record["source_updated_meaning"] = record["_source_updated_meaning"]
                    record["source_updated_evidence"] = commit_evidence
                    record["time_provenance"] = merge_time_provenance(
                        record.get("time_provenance"),
                        {
                            "source_updated_at": time_entry(
                                commit_at,
                                source="gitee_commit" if commit_at else "not_provided",
                                meaning=record["_source_updated_meaning"] or "仓库未提供可验证的提交时间。",
                                confidence=record["_source_updated_confidence"],
                                evidence=commit_evidence,
                                role="content_revision_reference",
                                unknown_reason=(
                                    "" if commit_at else "commit_metadata_not_provided"
                                ),
                            ),
                            "published_at": time_entry(
                                commit_at,
                                source="gitee_commit" if commit_at else "not_provided",
                                meaning=record["_published_meaning"] or "仓库未提供可验证的版本时间。",
                                confidence=record["_published_confidence"],
                                evidence=commit_evidence,
                                role=record["_published_role"],
                                eligible_for_latency=False,
                                unknown_reason=record["_published_unknown_reason"],
                                evidence_scope=f"gitee:{repo}:{path}",
                                content_version=str(commit_sha or tree_sha or file_sha),
                            ),
                        },
                    )
                    record["_fetched_at"] = fetched_at
                results.extend(records)
                self._defer_file_checkpoint(target_key, tree_sha, path, file_sha)
            if self.config.checkpoint_store and tree_sha:
                if self.config.defer_checkpoints:
                    self._completed_trees.append((target_key, tree_sha))
                else:
                    self.config.checkpoint_store.set_checkpoint(target_key, tree_sha)
        return results

    def _defer_file_checkpoint(self, target: str, tree_sha: str, path: str, file_sha: str) -> None:
        if not self.config.checkpoint_store or not tree_sha or not file_sha:
            return
        if self.config.defer_checkpoints:
            self._completed_files.append((target, tree_sha, path, file_sha))
        else:
            self.config.checkpoint_store.set_file_checkpoint(target, tree_sha, path, file_sha, "completed")
