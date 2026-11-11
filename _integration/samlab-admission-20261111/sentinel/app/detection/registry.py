"""规则注册表：加载并校验 detection/rules/*.yaml。"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from .synthetic_samples import RuleLoader

RULES_DIR = Path(__file__).resolve().parent / "rules"

REQUIRED_FIELDS = ("id", "title", "type", "vendor", "version", "patterns", "confidence")


@dataclass
class Rule:
    id: str
    title: str
    type: str
    vendor: str
    version: int
    license: str
    confidence: int
    patterns: list[re.Pattern]
    raw_patterns: list[str]
    secret_group: int = 0
    min_length: int = 8
    max_length: int = 4096
    min_entropy: float = 0.0
    required_context: list[re.Pattern] = field(default_factory=list)
    forbidden_context: list[re.Pattern] = field(default_factory=list)
    known_examples: list[str] = field(default_factory=list)
    forbidden_secret_values: list[re.Pattern] = field(default_factory=list)
    pairing: dict | None = None
    verifier: str | None = None
    context_window: int = 3
    capture_block: bool = False
    structured: str | None = None
    tests: dict = field(default_factory=dict)


class RuleRegistry:
    def __init__(self) -> None:
        self.rules: list[Rule] = []
        self.by_id: dict[str, Rule] = {}
        self.file_shas: dict[str, str] = {}

    def load(self, rules_dir: Path | None = None) -> None:
        rules_dir = rules_dir or RULES_DIR
        rules: list[Rule] = []
        shas: dict[str, str] = {}
        for path in sorted(rules_dir.glob("*.yaml")):
            data = yaml.load(path.read_text(encoding="utf-8"), Loader=RuleLoader)
            if isinstance(data, dict):  # 单文件单规则
                data = [data]
            raw_text = path.read_text(encoding="utf-8")
            shas[path.name] = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
            for entry in data:
                rules.append(self._build_rule(entry, path.name))
        self.rules = rules
        self.by_id = {r.id: r for r in rules}
        self.file_shas = shas

    def _build_rule(self, entry: dict, source: str) -> Rule:
        missing = [f for f in REQUIRED_FIELDS if f not in entry]
        if missing:
            raise ValueError(f"规则缺少字段 {missing}：{source} -> {entry.get('id', '?')}")
        compiled = [re.compile(p) for p in entry["patterns"]]
        return Rule(
            id=entry["id"],
            title=entry.get("title", entry["id"]),
            type=entry["type"],
            vendor=entry.get("vendor", ""),
            version=int(entry.get("version", 1)),
            license=entry.get("license", ""),
            confidence=int(entry["confidence"]),
            patterns=compiled,
            raw_patterns=list(entry["patterns"]),
            secret_group=int(entry.get("secret_group", 0)),
            min_length=int(entry.get("min_length", 8)),
            max_length=int(entry.get("max_length", 4096)),
            min_entropy=float(entry.get("min_entropy", 0.0)),
            required_context=[re.compile(p) for p in entry.get("required_context", [])],
            forbidden_context=[re.compile(p) for p in entry.get("forbidden_context", [])],
            known_examples=list(entry.get("known_examples", [])),
            forbidden_secret_values=[re.compile(p) for p in entry.get("forbidden_secret_values", [])],
            pairing=entry.get("pairing"),
            verifier=entry.get("verifier"),
            context_window=int(entry.get("context_window", 3)),
            capture_block=bool(entry.get("capture_block", False)),
            structured=entry.get("structured"),
            tests=entry.get("tests", {}) or {},
        )

    def get(self, rule_id: str) -> Rule | None:
        return self.by_id.get(rule_id)

    def validate_tests(self) -> list[str]:
        """校验每条规则自带正反样例：正例必须命中，反例必须不命中。"""
        from .engine import DetectionEngine  # 局部导入避免循环

        engine = DetectionEngine(self)
        problems: list[str] = []
        for rule in self.rules:
            for sample in rule.tests.get("positives", []):
                if not any(
                    c.rule_id == rule.id and not c.evidence.get("rejected_candidate")
                    for c in engine.scan(sample)
                ):
                    problems.append(f"{rule.id} 正例未命中: {sample[:60]!r}")
            for sample in rule.tests.get("negatives", []):
                hits = [
                    c for c in engine.scan(sample)
                    if c.rule_id == rule.id and not c.evidence.get("rejected_candidate")
                ]
                if hits:
                    problems.append(f"{rule.id} 反例误报: {sample[:60]!r}")
        return problems


_registry: RuleRegistry | None = None


def get_registry() -> RuleRegistry:
    global _registry
    if _registry is None:
        _registry = RuleRegistry()
        _registry.load()
    return _registry
