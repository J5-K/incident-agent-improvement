import json
import unittest
from pathlib import Path
from agent.evidence import action_condition, validate_proposal, render_checked, report_contract

ROOT = Path(__file__).resolve().parents[1]


def evidence(kind="cpu-spike", value=88.4):
    name = "payments-service" if kind == "cpu-spike" else "cache-service"
    return [{"source": "monitoring", "system_name": name, "source_id": "monitoring:" + name,
             "data": {"system_name": name, "status": "degraded", "cpu_percent": value,
                      "memory_percent": value, "alerts": ["CPU 91% (threshold 80%)"]}},
            {"source": "runbook", "system_name": name, "source_id": "runbook:" + name + ":" + kind,
             "incident_type": kind, "data": (ROOT / f"data/runbooks/{kind}.md").read_text()}]


def proposal(items):
    mon, book = items
    step = "3" if book["incident_type"] == "cpu-spike" else "2"
    return {"systems": [{"system_name": mon["system_name"], "monitoring_source": mon["source_id"],
        "cause": "undetermined", "actions": [{"runbook_source": book["source_id"], "step": step,
        "condition_status": action_condition(book, int(step), mon["data"])["status"]}]}]}


class ConditionTests(unittest.TestCase):
    def test_cpu_strict_boundary_and_duration(self):
        for value, status in [(88.4, "not_met"), (90, "not_met"), (90.1, "needs_confirmation")]:
            items = evidence(value=value)
            self.assertEqual(action_condition(items[1], 3, items[0]["data"])["status"], status)

    def test_memory_inclusive_boundary(self):
        for value, status in [(88.8, "not_met"), (89.9, "not_met"), (90, "met"), (90.1, "met")]:
            items = evidence("memory-leak", value)
            self.assertEqual(action_condition(items[1], 2, items[0]["data"])["status"], status)

    def test_missing_invalid_metric(self):
        for value in [None, "91", True, float("nan"), float("inf")]:
            items = evidence(value=value)
            self.assertEqual(action_condition(items[1], 3, items[0]["data"])["status"], "unknown")

    def test_source_change_invalidates_policy(self):
        items = evidence(); items[1]["data"] += "\nchanged"
        self.assertEqual(action_condition(items[1], 3, items[0]["data"])["status"], "unknown")

    def test_unreviewed_step_is_not_automatically_approved(self):
        items = evidence()
        self.assertEqual(action_condition(items[1], 4, items[0]["data"])["status"], "needs_confirmation")


class ProposalTests(unittest.TestCase):
    def test_exact_conditions_accepted(self):
        for kind in ["cpu-spike", "memory-leak"]:
            items = evidence(kind); data = proposal(items)
            parsed, issues = validate_proposal(json.dumps(data), items, [items[0]["system_name"]])
            self.assertEqual(issues, [])
            self.assertIn("수치 조건 미충족", render_checked(parsed, items, True))

    def test_incorrect_claim_is_rejected(self):
        items = evidence(); data = proposal(items)
        data["systems"][0]["actions"][0]["condition_status"] = "met"
        self.assertIn("조건 판단 불일치", " ".join(validate_proposal(json.dumps(data), items, [items[0]["system_name"]])[1]))

    def test_hallucinated_metrics_free_text_and_sources_rejected(self):
        items = evidence(); target = [items[0]["system_name"]]
        for mutation in [lambda r: r.update(cpu_percent=99), lambda r: r.update(cause="CPU 88.4%는 90% 초과"),
                         lambda r: r.update(monitoring_source="monitoring:cache-service"),
                         lambda r: r["actions"][0].update(step="99"),
                         lambda r: r["actions"][0].update(runbook_source="invented")]:
            data = proposal(items); mutation(data["systems"][0])
            self.assertTrue(validate_proposal(json.dumps(data), items, target)[1])

    def test_missing_duplicate_and_unrequested_system(self):
        items = evidence(); data = proposal(items)
        for rows in [[], data["systems"] * 2]:
            self.assertTrue(validate_proposal(json.dumps({"systems": rows}), items, [items[0]["system_name"]])[1])

    def test_fallback_does_not_publish_unvalidated_advice(self):
        items = evidence(); text = render_checked({"systems": []}, items, False)
        self.assertIn("검증 미통과", text)
        self.assertIn("88.4", text)
        self.assertNotIn("부하를 분산", text)
