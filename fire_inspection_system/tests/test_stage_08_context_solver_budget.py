from __future__ import annotations

import unittest

from fire_inspection_system.stages import stage_08_semantic_rgcn as stage


def _problem() -> tuple[dict[str, object], dict[str, object]]:
    target_ids = ["M0", "M1", "Q0", "Q1", "Q2", "Q3", "Q4"]
    floor = {
        "floor_id": "F2",
        "candidates": [{"target_id": target_id} for target_id in target_ids],
        "requirements": [
            {
                "requirement_id": "mandatory",
                "type": "mandatory_all",
                "candidate_ids": ["M0", "M1"],
            },
            {
                "requirement_id": "quota",
                "type": "quota_per_class",
                "candidate_ids": ["Q0", "Q1", "Q2", "Q3", "Q4"],
                "required_count": 2,
            },
        ],
    }
    distances = {
        left: {right: (0.0 if left == right else 1.0) for right in target_ids}
        for left in target_ids
    }
    matrix = {
        "floors": {
            "F2": {
                "target_ids": target_ids,
                "distances": distances,
                "distance_semantics": "test_complete_graph",
            }
        }
    }
    return floor, matrix


class ContextSolverBudgetTests(unittest.TestCase):
    def test_over_budget_exact_candidate_uses_heuristic(self) -> None:
        floor, matrix = _problem()

        result = stage._s08_optimizer.optimize_floor_route(
            floor,
            matrix,
            "F2",
            max_exact_work_units=20,
        )

        self.assertTrue(result["feasible"])
        self.assertEqual(result["selection_combination_count"], 10)
        self.assertEqual(result["estimated_selected_target_count"], 4)
        self.assertEqual(result["estimated_exact_work_units"], 40)
        self.assertTrue(result["exact_eligible_without_work_budget"])
        self.assertTrue(result["exact_work_budget_exceeded"])
        self.assertEqual(result["solver_selection_reason"], "exact_work_budget_exceeded")
        self.assertEqual(result["solver"], "heuristic_greedy_2opt")

    def test_within_budget_keeps_exact_solver(self) -> None:
        floor, matrix = _problem()

        result = stage._s08_optimizer.optimize_floor_route(
            floor,
            matrix,
            "F2",
            max_exact_work_units=100,
        )

        self.assertTrue(result["feasible"])
        self.assertFalse(result["exact_work_budget_exceeded"])
        self.assertEqual(result["solver_selection_reason"], "within_exact_limits_and_work_budget")
        self.assertEqual(result["solver"], "exact_enumeration_held_karp")


if __name__ == "__main__":
    unittest.main()
