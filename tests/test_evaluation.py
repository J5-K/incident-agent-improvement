import unittest
from evaluation.run import summarize

class EvaluationTests(unittest.TestCase):
    def test_unmeasured_numeric_fields_are_not_zero(self):
        result = summarize([{"completed":True, "route_match":True, "target_match":True, "book_match":None,
            "numeric_match":None, "unsafe_published":None, "expected_failure_observed":None, "elapsed_seconds":0.1}])
        self.assertIsNone(result["structured_numeric_fact_match_rate"])
        self.assertEqual(result["structured_numeric_fact_case_count"], 0)
        self.assertEqual(result["completion_rate"], 1)

    def test_error_publication_rate_uses_injected_cases_only(self):
        result = summarize([{"unsafe_published":v, "elapsed_seconds":0.1} for v in [None, False, True]])
        self.assertEqual(result["unsafe_injected_draft_published_rate"], 0.5)

    def test_handled_failure_is_distinct_from_observed_exception(self):
        row = {"elapsed_seconds":0.1, "failure_report_returned":True, "expected_failure_observed":False}
        result = summarize([row])
        self.assertEqual(result["failure_case_report_returned_rate"], 1)
        self.assertEqual(result["failure_case_error_observed_rate"], 0)
