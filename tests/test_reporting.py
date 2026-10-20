import json
import unittest
from agent.reporting import build_report, render_report
from agent.evidence import validate_proposal
from test_evidence import evidence, proposal


class ReportingTests(unittest.TestCase):
    def test_facts_and_hypotheses_separate_and_numeric_source_preserved(self):
        items = evidence(); report = build_report(proposal(items), items, True, 1)
        self.assertEqual(report["facts"][0]["cpu_percent"], 88.4)
        self.assertFalse(report["hypotheses"][0]["confirmed"])
        text = render_report(report)
        for heading in ["1) 확인된 사실", "2) 원인 추정", "3) 권장", "4) 참고 출처"]:
            self.assertIn(heading, text)
        self.assertIn("현재 수치와 별개", text)
        self.assertIn("조건 미충족", text)

    def test_fallback_has_no_unverified_causes_or_actions(self):
        items = evidence(); report = build_report({}, items, False, 3, ["조건 불일치"])
        self.assertEqual(report["hypotheses"], [])
        self.assertEqual(report["recommendations"], [])
        self.assertIn("검증 미통과", render_report(report))

    def test_cross_system_source_is_rejected(self):
        items = evidence() + evidence("memory-leak", 88.8)
        data = proposal(items[:2]); data["systems"][0]["actions"][0]["runbook_source"] = items[-1]["source_id"]
        self.assertTrue(validate_proposal(json.dumps(data), items, [items[0]["system_name"]])[1])

    def test_policy_check_not_hidden_by_model_selection(self):
        items = evidence(); data = proposal(items); data["systems"][0]["actions"] = []
        report = build_report(data, items, True, 1)
        self.assertEqual(report["condition_checks"][0]["status"], "not_met")

    def test_all_sources_are_known_and_referenceable(self):
        items = evidence() + evidence("memory-leak", 90)
        data = {"systems": proposal(items[:2])["systems"] + proposal(items[2:])["systems"]}
        report = build_report(data, items, True, 1)
        ids = {s["id"] for s in report["sources"]}
        self.assertEqual(len(ids), 4)
        self.assertTrue(all(x["source_id"] in ids for x in report["facts"] + report["recommendations"]))

    def test_missing_metric_does_not_render_as_none_or_nan(self):
        items = evidence(value=None); text = render_report(build_report(proposal(items), items, True, 1))
        self.assertIn("CPU 미수집", text)
        self.assertNotIn("None%", text)
