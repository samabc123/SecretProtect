"""Generate bounded, deterministic rule-test strings, never live credentials."""

import hashlib
import random
import re
import string

import yaml


ALPHABETS = {
    "hex": string.hexdigits[:16],
    "upper": string.ascii_uppercase + string.digits,
    "lower": string.ascii_lowercase + string.digits,
    "alnum": string.ascii_letters + string.digits,
}
MARKER = re.compile(r"\{test:(hex|upper|lower|alnum):([0-9]{1,3}):([a-f0-9]{12})\}")


def expand(template):
    def token(match):
        length = int(match[2])
        if not 1 <= length <= 512:
            raise ValueError("Synthetic test token exceeds bounds")
        generator = random.Random("secretradar-synthetic-only:" + match[3])
        return "".join(generator.choice(ALPHABETS[match[1]]) for _ in range(length))
    return MARKER.sub(token, template)


class RuleLoader(yaml.SafeLoader):
    pass


RuleLoader.add_constructor("!synthetic", lambda loader, node: expand(loader.construct_scalar(node)))


def template_for_sample(sample):
    """Mechanical migration of synthetic examples; not for sanitizing real secrets."""
    def replace(match):
        value = match[0]
        if value.startswith("\\"):
            return value
        # Keep format markers while generating entirely new, unissued test values.
        prefix = next((p for p in ("eyJ1Ijoi", "AKIA", "ASIA", "AKID", "LTAI", "AIza",
                                   "AAAA", "dapi", "pat", "eyJ", "AA", "AC", "00")
                       if value.startswith(p)), "")
        value = value[len(prefix):]
        if "T3BlbkFJ" in value:
            before, after = value.split("T3BlbkFJ", 1)
            return prefix + marker(before) + "T3BlbkFJ" + marker(after)
        return prefix + marker(value)

    def marker(value):
        if not value:
            return ""
        if value.endswith("EXAMPLE") and len(value) > 7:
            return marker(value[:-7]) + "EXAMPLE"
        if len(set(value)) <= 2 or any(word in value.lower() for word in ("example", "placeholder", "yourkey")):
            return value
        alphabet = ("hex" if all(c in string.hexdigits for c in value)
                    else "upper" if value.upper() == value
                    else "lower" if value.lower() == value else "alnum")
        seed = hashlib.sha256(("synthetic-fixture:" + value).encode()).hexdigest()[:12]
        return f"{{test:{alphabet}:{len(value)}:{seed}}}"

    return re.sub(r"\\[nrt]|[A-Za-z0-9]{16,}", replace, sample)
