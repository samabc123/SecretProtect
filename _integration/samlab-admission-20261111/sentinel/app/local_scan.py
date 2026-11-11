"""Bounded local-directory scanner for authorized/controlled test data."""

from __future__ import annotations

import os
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .content_detection import detect_content
from .time_provenance import merge_time_provenance, time_entry
from .time_utils import utc_now_iso


TEXT_EXTENSIONS = {
    ".env",
    ".ini",
    ".cfg",
    ".conf",
    ".txt",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".pem",
    ".properties",
    ".xml",
}
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "data"}


class LocalScanError(ValueError):
    """Safe local scanner error without file content."""


def scan_local_directory(
    root: Path,
    *,
    hmac_key: str,
    max_file_bytes: int = 512 * 1024,
    max_files: int = 2000,
    include_internal: bool = False,
    enabled_rule_ids: set[str] | frozenset[str] | None = None,
) -> list[dict]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise LocalScanError("authorized local directory does not exist")
    if max_file_bytes < 1 or max_files < 1:
        raise LocalScanError("local scan budget is invalid")
    candidates: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in SKIP_DIRS and not (Path(dirpath) / name).is_symlink()
        ]
        dirnames.sort()
        for filename in sorted(filenames):
            if len(candidates) >= max_files:
                break
            path = Path(dirpath) / filename
            if path.suffix.lower() not in TEXT_EXTENSIONS:
                continue
            try:
                if path.is_symlink():
                    continue
                resolved = path.resolve()
                resolved.relative_to(root)
                if resolved.stat().st_size > max_file_bytes:
                    continue
                relative = resolved.relative_to(root).as_posix()
            except (OSError, UnicodeError, ValueError):
                continue
            candidates.append((relative, resolved))
        if len(candidates) >= max_files:
            break

    def scan_one(item: tuple[str, Path]) -> tuple[str, list[dict[str, Any]]]:
        relative, path = item
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return relative, []
        fetched_at = utc_now_iso()
        observed_at = utc_now_iso()
        events = detect_content(
            content,
            source_kind="受控投放检出",
            channel="本地授权载体",
            platform="本地目录",
            source_locator=f"local:{relative}",
            hmac_key=hmac_key,
            first_observed_at=observed_at,
            include_internal=include_internal,
            enabled_rule_ids=enabled_rule_ids,
        )
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        source_updated_at = None
        try:
            source_updated_at = datetime.fromtimestamp(
                path.stat().st_mtime,
                tz=timezone.utc,
            ).isoformat(timespec="milliseconds")
        except OSError:
            pass
        for event in events:
            event["_fetched_at"] = fetched_at
            event["_version_id"] = content_hash
            event["_version_kind"] = "content_hash"
            event["_content_hash"] = content_hash
            event["_content_path"] = relative
            incoming = {
                "fetched_at": time_entry(
                    fetched_at,
                    source="local_file_read",
                    meaning="本地授权文件读取完成的时间。",
                    confidence="high",
                    evidence=f"local:{relative}",
                    role="acquisition_completion",
                    eligible_for_latency=True,
                    precision="millisecond",
                    evidence_scope=f"local:{relative}",
                    content_version=content_hash,
                ),
            }
            if source_updated_at:
                incoming["source_updated_at"] = time_entry(
                    source_updated_at,
                    source="filesystem_mtime",
                    meaning="本地文件修改时间，仅作为内容修订参考，不是公开泄露时间。",
                    confidence="low",
                    evidence=f"local:{relative}",
                    role="content_revision_reference",
                )
            event["time_provenance"] = merge_time_provenance(
                event.get("time_provenance"),
                incoming,
            )
        return relative, events

    # Reading independent files overlaps I/O while preserving deterministic
    # output order. Small scans stay sequential to avoid thread overhead.
    if len(candidates) < 8:
        scanned_results = [scan_one(item) for item in candidates]
    else:
        workers = min(8, max(2, (os.cpu_count() or 2)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="local-scan") as pool:
            scanned_results = list(pool.map(scan_one, candidates))

    results: dict[str, dict] = {}
    for _, events in scanned_results:
        for event in events:
            results[event["event_id"]] = event
    return list(results.values())
