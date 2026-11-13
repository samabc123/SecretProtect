"""Rule-based credential detection used by authorized content connectors."""

from .engine import Candidate, DetectionEngine
from .registry import Rule, RuleRegistry, get_registry

__all__ = [
    "Candidate",
    "DetectionEngine",
    "Rule",
    "RuleRegistry",
    "get_registry",
]
