"""Publication-safe fixtures remain deterministic and bounded."""

import string
import unittest

import yaml

from app.detection.synthetic_samples import RuleLoader, expand


class SyntheticSampleTests(unittest.TestCase):
    def test_generation_is_deterministic_with_requested_alphabet_and_length(self):
        template = "{test:hex:40:123456abcdef}"
        value = expand(template)
        self.assertEqual(value, expand(template))
        self.assertEqual(len(value), 40)
        self.assertTrue(set(value) <= set(string.hexdigits))

    def test_tag_preserves_context_and_safe_loader(self):
        loaded = yaml.load('sample: !synthetic "prefix-{test:upper:16:123456abcdef}"', Loader=RuleLoader)
        self.assertTrue(loaded["sample"].startswith("prefix-"))
        self.assertEqual(len(loaded["sample"]), 23)
        with self.assertRaises(yaml.constructor.ConstructorError):
            yaml.load("!!python/object/apply:os.system ['not-executed']", Loader=RuleLoader)

    def test_generation_rejects_unbounded_values(self):
        for length in (0, 513, 999):
            with self.assertRaises(ValueError):
                expand(f"{{test:alnum:{length}:123456abcdef}}")


if __name__ == "__main__":
    unittest.main()
