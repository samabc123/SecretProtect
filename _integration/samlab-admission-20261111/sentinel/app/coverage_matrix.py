"""Run the local credential-rule coverage and latency matrix.

This command only consumes synthetic rule samples from the repository. It
never contacts a platform and never prints sample secret values.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any

from .detection.engine import DetectionEngine
from .detection.registry import get_registry
from .local_scan import scan_local_directory


def _benchmark_local_scan(file_count: int = 32) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ccs-scan-benchmark-") as directory:
        root = Path(directory)
        for index in range(file_count):
            (root / f"fixture-{index}.env").write_text(
                f"API_KEY=J8kQ2wL5vB8nM4cR6tZ0aS3dF7yG{index:02d}\n",
                encoding="utf-8",
            )
        started = time.perf_counter()
        records = scan_local_directory(
            root,
            hmac_key="coverage-matrix-local-key",
            max_files=file_count,
        )
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
    return {
        "files": file_count,
        "candidates": len(records),
        "duration_ms": duration_ms,
        "mode": "bounded parallel local scan",
    }


def build_coverage_report() -> dict[str, Any]:
    registry = get_registry()
    engine = DetectionEngine(registry)
    started = time.perf_counter()
    rule_rows: list[dict[str, Any]] = []
    failures: list[str] = []
    positive_count = 0
    negative_count = 0
    for rule in registry.rules:
        positives = list(rule.tests.get("positives", []))
        negatives = list(rule.tests.get("negatives", []))
        positive_count += len(positives)
        negative_count += len(negatives)
        positive_hits = 0
        negative_hits = 0
        for sample in positives:
            hits = [
                item for item in engine.scan(sample)
                if item.rule_id == rule.id and not item.evidence.get("rejected_candidate")
            ]
            positive_hits += bool(hits)
        for sample in negatives:
            hits = [
                item for item in engine.scan(sample)
                if item.rule_id == rule.id and not item.evidence.get("rejected_candidate")
            ]
            negative_hits += bool(hits)
        if positive_hits != len(positives):
            failures.append(f"{rule.id}:positive")
        if negative_hits:
            failures.append(f"{rule.id}:negative")
        rule_rows.append({
            "id": rule.id,
            "type": rule.type,
            "vendor": rule.vendor,
            "positive_samples": len(positives),
            "positive_hits": positive_hits,
            "negative_samples": len(negatives),
            "negative_hits": negative_hits,
        })
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    return {
        "rules": len(registry.rules),
        "positive_samples": positive_count,
        "negative_samples": negative_count,
        "passed": not failures,
        "failures": failures,
        "duration_ms": duration_ms,
        "local_scan_benchmark": _benchmark_local_scan(),
        "rule_rows": rule_rows,
        "source": "repository synthetic rule samples",
    }


def main() -> int:
    report = build_coverage_report()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
