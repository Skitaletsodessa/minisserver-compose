"""Run LAST (the name sorts last): every operation of openapi.yaml was exercised through a contract-validated request during this run."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import helpers  # noqa: E402


class Coverage(unittest.TestCase):
    def test_every_operation_of_the_spec_was_exercised_and_validated(self):
        ops = {op["operationId"] for item in helpers.SPEC["paths"].values() for op in item.values() if isinstance(op, dict) and "operationId" in op}
        if len(helpers.SEEN) < 8:
            self.skipTest("run the whole suite in one process (unittest discover) for this check")
        self.assertEqual(sorted(ops - helpers.SEEN), [], "operations never exercised")

    def test_planned_paths_are_marked_planned_and_nothing_else_is(self):
        planned = [p for p, item in helpers.SPEC["paths"].items() for op in item.values() if isinstance(op, dict) and op.get("x-status") == "planned"]
        self.assertEqual(planned, ["/v1/parental/{rest}"])


if __name__ == "__main__":
    unittest.main()
