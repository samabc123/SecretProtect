"""Bounded GitHub REST connector with an injectable transport for offline tests."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import urllib.parse
import urllib.error
from urllib.parse import urlparse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Any

from .content_detection import detect_content
from .time_provenance import merge_time_provenance, time_entry
from .time_utils import utc_now_iso


class GitHubConnectorError(ValueError):
    """Safe connector error without token, response body or candidate content."""

    def __init__(self, code: str, retry_after: float | None = None):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


@dataclass(frozen=True)
class TransportResponse:
    payload: Any
    headers: dict[str, str]
    status_code: int = 200


@dataclass(frozen=True)
class GitHubConnectorConfig:
    enabled: bool = False
    token: str = ""
    repositories: tuple[str, ...] = ()
    branch: str | None = None
    api_base: str = "https://api.github.com"
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


class UrllibTransport:
    def __init__(self, timeout: float):
        self.timeout = timeout

    def get_json(self, url: str, headers: dict[str, str]) -> TransportResponse:
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return TransportResponse(
                    json.loads(response.read().decode("utf-8")),
                    {str(key): str(value) for key, value in response.headers.items()},
                    response.status,
                )
        except urllib.error.HTTPError as error:
            retry_after = error.headers.get("Retry-After")
            retry_after_value = _parse_retry_after(retry_after)
            if error.code == 429 or (error.code == 403 and error.headers.get("X-RateLimit-Remaining") == "0"):
                raise GitHubConnectorError("github_rate_limited", retry_after_value) from None
            if error.code == 403:
                raise GitHubConnectorError("github_forbidden") from None
            if error.code == 404:
                raise GitHubConnectorError("github_not_found") from None
            raise GitHubConnectorError("github_http_error") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            raise GitHubConnectorError("github_transport_error") from None


def _safe_repo(repo: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise GitHubConnectorError("invalid_repository_scope")
    return repo


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
        reference = now or datetime.now(timezone.utc)
        return max(0.0, (parsed - reference).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


class GitHubAuthorizedConnector:
    name = "github-authorized-rest"

    def __init__(
        self,
        config: GitHubConnectorConfig,
        transport: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.transport = transport or UrllibTransport(config.timeout_seconds)
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
            raise GitHubConnectorError("network_connector_disabled")
        if not self.config.token or len(self.config.token) < 16:
            raise GitHubConnectorError("github_token_missing")
        if not self.config.repositories:
            raise GitHubConnectorError("repository_allowlist_empty")
        if not self.config.hmac_key:
            raise GitHubConnectorError("credential_hmac_key_missing")
        parsed = urlparse(self.config.api_base)
        if parsed.scheme != "https" or parsed.netloc not in {"api.github.com", "github.com"}:
            raise GitHubConnectorError("github_api_base_not_allowed")
        if self.config.max_files < 1 or self.config.max_files > 500:
            raise GitHubConnectorError("max_files_out_of_range")
        for repo in self.config.repositories:
            _safe_repo(repo)

    def _get_response(self, path_or_url: str) -> TransportResponse:
        url = path_or_url if path_or_url.startswith("https://") else self.config.api_base.rstrip("/") + path_or_url
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.config.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "credential-leak-detector-authorized-connector",
        }
        for attempt in range(self.config.max_retries + 1):
            try:
                result = self.transport.get_json(url, headers)
                if isinstance(result, TransportResponse):
                    response = result
                elif isinstance(result, tuple) and len(result) == 2:
                    response = TransportResponse(result[0], dict(result[1]))
                else:
                    response = TransportResponse(result, {})
                self.sleep(self.config.min_interval_seconds)
                return response
            except GitHubConnectorError as error:
                if error.code not in {"github_rate_limited", "github_transport_error"}:
                    raise
                if attempt >= self.config.max_retries:
                    raise
                self.sleep(min(30.0, error.retry_after if error.retry_after is not None else 2.0 ** attempt))
        raise GitHubConnectorError("github_transport_error")

    def _get(self, path_or_url: str) -> Any:
        return self._get_response(path_or_url).payload

    def _get_paginated(self, path: str, max_pages: int = 20) -> list[Any]:
        items: list[Any] = []
        next_url: str | None = path
        pages = 0
        while next_url:
            pages += 1
            if pages > max_pages:
                raise GitHubConnectorError("github_pagination_limit_exceeded")
            response = self._get_response(next_url)
            payload = response.payload
            if isinstance(payload, list):
                items.extend(payload)
            else:
                items.append(payload)
            next_url = _next_link(response.headers.get("Link"))
        return items

    def collect(self) -> list[dict]:
        self._validate()
        results: list[dict] = []
        for repo in self.config.repositories:
            owner, name = _safe_repo(repo).split("/", 1)
            tree_pages = self._get_paginated(
                f"/repos/{owner}/{name}/git/trees/{self.config.branch or 'HEAD'}?recursive=1"
            )
            tree = {"tree": [], "sha": None, "truncated": False}
            for page in tree_pages:
                if not isinstance(page, dict):
                    raise GitHubConnectorError("github_invalid_response")
                if page.get("truncated") is True:
                    raise GitHubConnectorError("github_tree_truncated")
                tree["tree"].extend(page.get("tree", []))
                tree["sha"] = tree["sha"] or page.get("sha")
            target_key = f"github:{repo}:{self.config.branch or 'HEAD'}"
            tree_sha = tree.get("sha")
            branch_ref = self.config.branch or "HEAD"
            commit_sha = str(tree_sha or "")
            commit_at: str | None = None
            commit_evidence = ""
            try:
                head_commit = self._get(
                    f"/repos/{owner}/{name}/commits/{urllib.parse.quote(branch_ref, safe='')}"
                )
                if isinstance(head_commit, dict):
                    commit_sha = str(head_commit.get("sha") or commit_sha)
                    commit_payload = head_commit.get("commit") or {}
                    committer = commit_payload.get("committer") or {}
                    author = commit_payload.get("author") or {}
                    raw_date = committer.get("date") or author.get("date")
                    if raw_date:
                        parsed = datetime.fromisoformat(
                            str(raw_date).replace("Z", "+00:00")
                        )
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        commit_at = parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
                    if commit_sha:
                        commit_evidence = f"https://github.com/{repo}/commit/{commit_sha}"
            except (GitHubConnectorError, TypeError, ValueError):
                # A tree-only mock or a provider response without commit
                # metadata still yields a safe version identifier; the
                # missing revision time remains explicitly unknown.
                commit_at = None
            if self.config.checkpoint_store and tree_sha:
                if self.config.checkpoint_store.get_checkpoint(target_key) == tree_sha:
                    continue
            entries = [item for item in tree.get("tree", []) if item.get("type") == "blob"]
            if len(entries) > self.config.max_files:
                raise GitHubConnectorError("github_file_budget_exceeded")
            for entry in entries[: self.config.max_files]:
                path = entry.get("path", "")
                file_sha = entry.get("sha")
                if Path(path).suffix.lower() not in {
                    ".env", ".ini", ".cfg", ".conf", ".txt", ".yaml", ".yml", ".json", ".toml", ".pem"
                }:
                    if self.config.checkpoint_store and tree_sha and file_sha:
                        if self.config.defer_checkpoints:
                            self._completed_files.append((target_key, tree_sha, path, file_sha))
                        else:
                            self.config.checkpoint_store.set_file_checkpoint(target_key, tree_sha, path, file_sha, "completed")
                    continue
                if self.config.checkpoint_store and tree_sha and file_sha:
                    checkpoint = self.config.checkpoint_store.get_file_checkpoint(target_key, path)
                    if (
                        checkpoint
                        and checkpoint["tree_sha"] == tree_sha
                        and checkpoint["file_sha"] == file_sha
                        and checkpoint["status"] == "completed"
                    ):
                        continue
                blob = self._get(f"/repos/{owner}/{name}/contents/{urllib.parse.quote(path)}")
                fetched_at = utc_now_iso()
                encoded = blob.get("content", "")
                if len(encoded) > self.config.max_file_bytes * 2:
                    raise GitHubConnectorError("github_file_too_large")
                try:
                    content = base64.b64decode(encoded).decode("utf-8")
                except (ValueError, UnicodeError):
                    if self.config.checkpoint_store and tree_sha and file_sha:
                        if self.config.defer_checkpoints:
                            self._completed_files.append((target_key, tree_sha, path, file_sha))
                        else:
                            self.config.checkpoint_store.set_file_checkpoint(target_key, tree_sha, path, file_sha, "completed")
                    continue
                if len(content.encode("utf-8")) > self.config.max_file_bytes:
                    raise GitHubConnectorError("github_file_too_large")
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                detected_records = detect_content(
                    content,
                    source_kind="真实检测结果",
                    channel="代码托管",
                    platform="GitHub（授权 API）",
                    source_locator=f"github:{repo}:{path}",
                    hmac_key=self.config.hmac_key,
                    fingerprint_key_version=self.config.fingerprint_key_version,
                    include_internal=self.config.include_internal,
                    enabled_rule_ids=self.config.enabled_rule_ids,
                )
                for record in detected_records:
                    record["_version_id"] = commit_sha or tree_sha or file_sha
                    record["_version_kind"] = "commit" if commit_at else "tree"
                    record["_content_hash"] = content_hash
                    record["_content_path"] = path
                    record["_source_updated_at"] = commit_at
                    record["_source_updated_source"] = "github_commit" if commit_at else ""
                    record["_source_updated_confidence"] = "medium" if commit_at else "none"
                    record["_source_updated_meaning"] = (
                        "匹配文件所在仓库版本的提交时间，仅作为内容修订参考；"
                        "不等于凭据首次泄露时间。" if commit_at else ""
                    )
                    record["_source_updated_evidence"] = commit_evidence
                    record["_published_at"] = commit_at
                    record["_published_source"] = "github_commit" if commit_at else ""
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
                    record["published_source"] = "github_commit" if commit_at else ""
                    record["published_confidence"] = "medium" if commit_at else "none"
                    record["published_meaning"] = record["_published_meaning"]
                    record["published_evidence"] = commit_evidence
                    record["public_time_status"] = "reference_only" if commit_at else "unknown"
                    record["public_time_unknown_reason"] = record["_published_unknown_reason"]
                    record["source_updated_at"] = commit_at
                    record["source_updated_source"] = "github_commit" if commit_at else ""
                    record["source_updated_confidence"] = "medium" if commit_at else "none"
                    record["source_updated_meaning"] = record["_source_updated_meaning"]
                    record["source_updated_evidence"] = commit_evidence
                    record["time_provenance"] = merge_time_provenance(
                        record.get("time_provenance"),
                        {
                            "source_updated_at": time_entry(
                                commit_at,
                                source="github_commit" if commit_at else "not_provided",
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
                                source="github_commit" if commit_at else "not_provided",
                                meaning=record["_published_meaning"] or "仓库未提供可验证的版本时间。",
                                confidence=record["_published_confidence"],
                                evidence=commit_evidence,
                                role=record["_published_role"],
                                eligible_for_latency=False,
                                unknown_reason=record["_published_unknown_reason"],
                                evidence_scope=f"github:{repo}:{path}",
                                content_version=str(commit_sha or tree_sha or file_sha),
                            ),
                        },
                    )
                    record["_fetched_at"] = fetched_at
                results.extend(detected_records)
                if self.config.checkpoint_store and tree_sha and file_sha:
                    if self.config.defer_checkpoints:
                        self._completed_files.append((target_key, tree_sha, path, file_sha))
                    else:
                        self.config.checkpoint_store.set_file_checkpoint(target_key, tree_sha, path, file_sha, "completed")
            if self.config.checkpoint_store and tree_sha:
                if self.config.defer_checkpoints:
                    self._completed_trees.append((target_key, tree_sha))
                else:
                    self.config.checkpoint_store.set_checkpoint(target_key, tree_sha)
        return results


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for item in link_header.split(","):
        match = re.match(r'\s*<([^>]+)>\s*;\s*rel="([^"]+)"', item)
        if match and match.group(2) == "next":
            return match.group(1)
    return None
