"""检测引擎：规则扫描 + 上下文/熵/占位符过滤 + 配对 + 有边界解码 + 结构化解析。

资源约束：单次输入截断到 MAX_INPUT_CHARS；Base64 解码尝试次数与长度受限；
不做递归归档展开（归档由采集器层负责并限额）。
"""
from __future__ import annotations

import base64
import bisect
import json
import re
from dataclasses import dataclass, field

from .registry import Rule, RuleRegistry
from .security import is_placeholder, shannon_entropy

MAX_INPUT_CHARS = 1_000_000
MAX_DECODE_ATTEMPTS = 20
MAX_DECODED_CHARS = 200_000
MAX_BLOCK_CHARS = 20_000

_B64_RUN = re.compile(r"[A-Za-z0-9+/]{48,}={0,2}")
_PRINTABLE = re.compile(r"[\x20-\x7e\r\n\t]")


@dataclass
class Candidate:
    rule_id: str
    rule_version: int
    type: str
    vendor: str
    secret: str
    confidence: int
    line_start: int
    line_end: int
    span: tuple[int, int]
    verifier_id: str | None
    evidence: dict = field(default_factory=dict)


class DetectionEngine:
    def __init__(self, registry: RuleRegistry | None = None) -> None:
        self.registry = registry

    def scan(
        self,
        content: str,
        path: str = "",
        depth: int = 0,
        enabled_rule_ids: set[str] | frozenset[str] | None = None,
    ) -> list[Candidate]:
        if self.registry is None:
            from .registry import get_registry
            self.registry = get_registry()
        if not content:
            return []
        truncated = False
        if len(content) > MAX_INPUT_CHARS:
            content = content[:MAX_INPUT_CHARS]
            truncated = True

        lines = content.split("\n")
        offsets: list[int] = []
        pos = 0
        for line in lines:
            offsets.append(pos)
            pos += len(line) + 1

        candidates: list[Candidate] = []
        for rule in self.registry.rules:
            if enabled_rule_ids is not None and rule.id not in enabled_rule_ids:
                continue
            if rule.structured:
                candidates += self._scan_structured(rule, content)
            elif rule.capture_block:
                candidates += self._scan_block(rule, content, lines, offsets)
            else:
                candidates += self._scan_regex(rule, content, lines, offsets, path)
        if depth < 1:
            candidates += self._scan_base64(content, path, depth, enabled_rule_ids)
        if truncated:
            for c in candidates:
                c.evidence["input_truncated"] = True
        candidates = self._resolve_overlaps(candidates)
        self._apply_pairing(candidates)
        return candidates

    # ---------- 常规正则规则 ----------
    def _scan_regex(self, rule: Rule, content: str, lines: list[str], offsets: list[int], path: str) -> list[Candidate]:
        out: list[Candidate] = []
        for pattern in rule.patterns:
            for m in pattern.finditer(content):
                if "secret" in pattern.groupindex:
                    value = m.group("secret")
                    group_no = pattern.groupindex["secret"]
                else:
                    group_no = rule.secret_group or 0
                    value = m.group(group_no)
                if not value:
                    continue
                v = value.strip()
                evidence: dict = {
                    "pattern": rule.raw_patterns[0] if len(rule.raw_patterns) == 1 else rule.raw_patterns,
                    "match_group": group_no,
                    "path": path,
                }
                if len(rule.raw_patterns) > 1:
                    evidence["pattern"] = pattern.pattern
                if not (rule.min_length <= len(v) <= rule.max_length):
                    continue
                if v in rule.known_examples or is_placeholder(v):
                    evidence["rejected"] = "placeholder_or_example"
                    out.append(self._rejected(rule, m, lines, offsets, evidence))
                    continue
                # forbidden_secret_values 作用于捕获值及全部捕获组（如连接串中的口令组）
                group_values = [g for g in (m.groups() or ()) if isinstance(g, str)]
                if any(rx.search(val) for rx in rule.forbidden_secret_values for val in [v, *group_values]):
                    continue
                if rule.min_entropy and shannon_entropy(v) < rule.min_entropy:
                    evidence["rejected"] = "low_entropy"
                    out.append(self._rejected(rule, m, lines, offsets, evidence))
                    continue
                ls, le = self._line_range(offsets, m.start(), m.end(), len(lines))
                window = "\n".join(lines[max(0, ls - 1 - rule.context_window): min(len(lines), le + rule.context_window)])
                if any(rx.search(window) for rx in rule.forbidden_context):
                    evidence["rejected"] = "forbidden_context"
                    out.append(self._rejected(rule, m, lines, offsets, evidence))
                    continue
                context_hit = None
                if rule.required_context:
                    context_hit = next((rx.pattern for rx in rule.required_context if rx.search(window)), None)
                    if context_hit is None:
                        evidence["rejected"] = "missing_context"
                        out.append(self._rejected(rule, m, lines, offsets, evidence))
                        continue
                if context_hit:
                    evidence["context"] = context_hit
                if pattern.groupindex and pattern.groups >= 2:
                    evidence["context_key"] = m.group(1) if 1 <= pattern.groups else None
                out.append(Candidate(
                    rule_id=rule.id, rule_version=rule.version, type=rule.type, vendor=rule.vendor,
                    secret=v, confidence=rule.confidence,
                    line_start=ls, line_end=le, span=(m.start(), m.end()),
                    verifier_id=rule.verifier, evidence=evidence,
                ))
        return out

    def _rejected(self, rule: Rule, m: re.Match, lines, offsets, evidence: dict) -> Candidate:
        ls, le = self._line_range(offsets, m.start(), m.end(), len(lines))
        return Candidate(
            rule_id=rule.id, rule_version=rule.version, type=rule.type, vendor=rule.vendor,
            secret="", confidence=0, line_start=ls, line_end=le,
            span=(m.start(), m.end()), verifier_id=None, evidence={**evidence, "rejected_candidate": True},
        )

    # ---------- PEM 块规则 ----------
    def _scan_block(self, rule: Rule, content: str, lines: list[str], offsets: list[int]) -> list[Candidate]:
        out: list[Candidate] = []
        for pattern in rule.patterns:
            for m in pattern.finditer(content):
                begin = m.group(0)
                end_match = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----").search(content, m.end())
                if not end_match:
                    continue
                block = content[m.start(): end_match.end()]
                if not (len(block) <= MAX_BLOCK_CHARS):
                    continue
                header_type = (m.group(1) or "").strip() or "GENERIC"
                encrypted = "ENCRYPTED" in begin or "Proc-Type: 4,ENCRYPTED" in block
                if header_type == "OPENSSH":
                    encrypted_state = "unknown"
                else:
                    encrypted_state = bool(encrypted)
                ls, le = self._line_range(offsets, m.start(), end_match.end(), len(lines))
                out.append(Candidate(
                    rule_id=rule.id, rule_version=rule.version, type=rule.type, vendor=rule.vendor,
                    secret=block, confidence=rule.confidence,
                    line_start=ls, line_end=le, span=(m.start(), end_match.end()),
                    verifier_id=rule.verifier,
                    evidence={"key_type": header_type, "encrypted": encrypted_state, "block_chars": len(block)},
                ))
        return out

    # ---------- 结构化 JSON（服务账号等） ----------
    def _scan_structured(self, rule: Rule, content: str) -> list[Candidate]:
        if rule.structured != "gcp_service_account":
            return []
        stripped = content.strip()
        if not stripped.startswith("{"):
            return []
        try:
            obj = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(obj, dict):
            return []
        if obj.get("type") != "service_account":
            return []
        private_key = obj.get("private_key")
        client_email = obj.get("client_email")
        if not (isinstance(private_key, str) and private_key.startswith("-----BEGIN")):
            return []
        if not client_email:
            return []
        paired = {k: str(obj[k]) for k in ("client_email", "project_id", "private_key_id") if obj.get(k)}
        return [Candidate(
            rule_id=rule.id, rule_version=rule.version, type=rule.type, vendor=rule.vendor,
            secret=private_key, confidence=rule.confidence,
            line_start=1, line_end=content.count("\n") + 1, span=(0, min(len(content), 100)),
            verifier_id=rule.verifier,
            evidence={"structured": "gcp_service_account", "paired": {k: v for k, v in paired.items() if k != "private_key"}},
        )]

    # ---------- 有边界 Base64 解码 ----------
    def _scan_base64(
        self,
        content: str,
        path: str,
        depth: int,
        enabled_rule_ids: set[str] | frozenset[str] | None = None,
    ) -> list[Candidate]:
        out: list[Candidate] = []
        attempts = 0
        for m in _B64_RUN.finditer(content):
            if attempts >= MAX_DECODE_ATTEMPTS:
                break
            blob = m.group(0)
            attempts += 1
            padded = blob + "=" * (-len(blob) % 4)
            try:
                decoded = base64.b64decode(padded, validate=True).decode("utf-8")
            except Exception:
                continue
            if not decoded or len(decoded) > MAX_DECODED_CHARS:
                continue
            if len(_PRINTABLE.findall(decoded)) / max(1, len(decoded)) < 0.85:
                continue
            for cand in self.scan(
                decoded,
                path=path,
                depth=depth + 1,
                enabled_rule_ids=enabled_rule_ids,
            ):
                cand.evidence["decoded_base64"] = True
                out.append(cand)
        return out

    # ---------- 去重叠与配对 ----------
    def _resolve_overlaps(self, candidates: list[Candidate]) -> list[Candidate]:
        kept: list[Candidate] = []
        for cand in sorted(candidates, key=lambda c: -c.confidence):
            if cand.evidence.get("rejected_candidate"):
                continue
            dup = any(
                k.secret == cand.secret
                and not (k.span[1] <= cand.span[0] or cand.span[1] <= k.span[0])
                for k in kept
            )
            if not dup:
                kept.append(cand)
        kept.sort(key=lambda c: (c.span[0], c.rule_id))
        return kept

    def _apply_pairing(self, candidates: list[Candidate]) -> None:
        for cand in candidates:
            rule = self.registry.by_id.get(cand.rule_id)
            if rule is None or not rule.pairing:
                continue
            other_id = rule.pairing.get("with", "")
            max_dist = int(rule.pairing.get("max_line_distance", 5))
            boost = int(rule.pairing.get("boost", 0))
            partner = next(
                (c for c in candidates
                 if c.rule_id == other_id and not c.evidence.get("rejected_candidate")
                 and abs(c.line_start - cand.line_start) <= max_dist),
                None,
            )
            if partner is not None:
                cand.evidence["paired_with"] = {"rule": other_id, "line": partner.line_start}
                # 配对字段值同时传递（供 AK/SK 等成对验证器使用；指纹随之区分不同账号）
                cand.evidence["paired"] = {**cand.evidence.get("paired", {}),
                                           partner.type: partner.secret}
                partner.evidence.setdefault("paired_with", {"rule": cand.rule_id, "line": cand.line_start})
                partner.evidence["paired"] = {**partner.evidence.get("paired", {}),
                                              cand.type: cand.secret}
                cand.confidence = min(100, cand.confidence + boost)
                partner.confidence = min(100, partner.confidence + boost)

    @staticmethod
    def _line_range(offsets: list[int], start: int, end: int, n_lines: int) -> tuple[int, int]:
        ls = bisect.bisect_right(offsets, start)
        le = bisect.bisect_right(offsets, max(start, end - 1))
        return min(ls, n_lines), min(le, n_lines)
