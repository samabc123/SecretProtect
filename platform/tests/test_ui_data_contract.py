"""Source filters must not silently mix test evidence with real detections."""

import unittest

from app.data_access import _event_query_values, _filter_events


class EventSourceFilterTests(unittest.TestCase):
    def setUp(self):
        self.events = [
            {"event_id": "synthetic-ui", "source_kind": "合成样例"},
            {"event_id": "controlled-ui", "source_kind": "受控投放检出"},
            {"event_id": "real-ui", "source_kind": "真实检测结果"},
        ]

    def test_explicit_all_sources_includes_each_source(self):
        rows = _filter_events(self.events, _event_query_values(source_kind="全部来源"))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["source_kind"], "真实检测结果")

    def test_real_source_does_not_include_test_records(self):
        rows = _filter_events(self.events, _event_query_values(source_kind="真实检测结果"))
        self.assertEqual([row["event_id"] for row in rows], ["real-ui"])

    def test_synthetic_source_does_not_include_real_records(self):
        rows = _filter_events(self.events, _event_query_values(source_kind="合成样例"))
        self.assertEqual([row["event_id"] for row in rows], ["synthetic-ui"])


if __name__ == "__main__":
    unittest.main()
