from __future__ import annotations

import unittest

from fire_inspection_system.stages import stage_09_dual_graph_planning as stage


class _FakeClosure:
    def __init__(
        self,
        target_ids: list[str],
        component_by_target: dict[str, int] | None = None,
    ) -> None:
        self.target_ids = list(target_ids)
        self.component_by_target = component_by_target or {
            target_id: 0 for target_id in target_ids
        }

    def _load(self, floor_id: str) -> dict[str, object]:
        return {
            "target_ids": list(self.target_ids),
            "component_index": [
                self.component_by_target[target_id] for target_id in self.target_ids
            ],
        }

    def distance(self, floor_id: str, left: str, right: str) -> float:
        if left == right:
            return 0.0
        if self.component_by_target[left] != self.component_by_target[right]:
            return float("inf")
        return 10.0


def _candidate(
    target_id: str,
    class_name: str,
    *,
    raw_name: str,
    mandatory: bool = False,
) -> dict[str, object]:
    return {
        "target_id": target_id,
        "class_name": class_name,
        "standard_class_name": class_name,
        "target_class": class_name,
        "raw_name": raw_name,
        "area_id": "F2_AREA_001",
        "point": [0.0 if target_id == "START" else 10.0, 0.0],
        "mandatory": mandatory,
        "u_rule": 0.5,
    }


def _plan(start: dict[str, object]) -> dict[str, object]:
    required = _candidate(
        "REQUIRED",
        "火灾探测器",
        raw_name="感烟探测器",
        mandatory=True,
    )
    candidates = [start, required]
    floor = {
        "floor_id": "F2",
        "floor_scale": 100.0,
        "candidates": candidates,
        "requirements": [
            {
                "requirement_id": "mandatory_all",
                "type": "mandatory_all",
                "candidate_ids": ["REQUIRED"],
                "required_count": 1,
            }
        ],
    }
    recommendation = {
        "selection_scores": {"START": 0.8, "REQUIRED": 0.9},
        "successor_top_k": {},
        "pair_selection_top_k": {},
    }
    plan, _graph = stage._s09_planner._plan_floor(
        "F2",
        floor,
        _FakeClosure(["START", "REQUIRED"]),
        recommendation,
        "RUN__F2",
        stage._s09_planner.DualGraphConfig(
            dmax_ratio=0.8,
            require_complete_rgcn_scores=True,
            expand_physical_walk=False,
            write_dxf=False,
        ),
    )
    return plan


class RouteStartConstraintTests(unittest.TestCase):
    def test_optional_safety_exit_is_added_and_used_as_first_target(self) -> None:
        plan = _plan(_candidate("START", "安全出口", raw_name="STAIR"))

        self.assertTrue(plan["feasible"])
        self.assertEqual(plan["order"][0], "START")
        self.assertEqual(plan["route_start_target_ids"], ["START"])
        self.assertEqual(plan["route_segments"][0]["entry_target_class_name"], "安全出口")
        self.assertEqual(plan["selection_reason"]["START"], "route_start_anchor")
        self.assertTrue(plan["route_start_constraint_satisfied"])

    def test_fire_elevator_is_a_legal_route_start(self) -> None:
        plan = _plan(_candidate("START", "消防电梯", raw_name="消防电梯"))

        self.assertTrue(plan["feasible"])
        self.assertEqual(plan["order"][0], "START")
        self.assertEqual(plan["route_segments"][0]["entry_target_class_name"], "消防电梯")

    def test_mislabeled_fire_door_cannot_be_a_route_start(self) -> None:
        plan = _plan(_candidate("START", "安全出口", raw_name="FM乙12"))

        self.assertFalse(plan["feasible"])
        self.assertEqual(plan["status"], "infeasible_route_start")
        self.assertEqual(
            plan["reason"],
            "floor_has_no_routable_safety_exit_or_fire_elevator_start",
        )
        self.assertEqual(plan["rejected_fire_door_candidate_ids"], ["START"])
        self.assertEqual(plan["order"], [])

    def test_only_first_segment_requires_a_legal_start(self) -> None:
        candidates = [
            _candidate("START", "安全出口", raw_name="STAIR"),
            _candidate("WITH_ENTRY", "火灾探测器", raw_name="感烟探测器", mandatory=True),
            _candidate("NO_ENTRY", "火灾探测器", raw_name="感烟探测器", mandatory=True),
        ]
        candidates[0]["area_id"] = "F2_AREA_ENTRY"
        candidates[1]["area_id"] = "F2_AREA_ENTRY"
        candidates[2]["area_id"] = "F2_AREA_DISCONNECTED"
        floor = {
            "floor_id": "F2",
            "floor_scale": 100.0,
            "candidates": candidates,
            "requirements": [
                {
                    "requirement_id": "mandatory_all",
                    "type": "mandatory_all",
                    "candidate_ids": ["WITH_ENTRY", "NO_ENTRY"],
                    "required_count": 2,
                }
            ],
        }
        recommendation = {
            "selection_scores": {"START": 0.7, "WITH_ENTRY": 0.8, "NO_ENTRY": 0.9},
            "successor_top_k": {},
            "pair_selection_top_k": {},
        }
        closure = _FakeClosure(
            ["START", "WITH_ENTRY", "NO_ENTRY"],
            {"START": 0, "WITH_ENTRY": 0, "NO_ENTRY": 1},
        )

        plan, _graph = stage._s09_planner._plan_floor(
            "F2",
            floor,
            closure,
            recommendation,
            "RUN__F2",
            stage._s09_planner.DualGraphConfig(
                dmax_ratio=0.8,
                require_complete_rgcn_scores=True,
                expand_physical_walk=False,
                write_dxf=False,
            ),
        )

        self.assertTrue(plan["feasible"])
        self.assertEqual(len(plan["route_segments"]), 2)
        first, continuation = plan["route_segments"]
        self.assertEqual(first["entry_target_id"], "START")
        self.assertTrue(first["route_start_constraint_applies"])
        self.assertTrue(first["route_start_constraint_satisfied"])
        self.assertEqual(first["virtual_entry_id"], "")
        self.assertEqual(continuation["entry_target_id"], "NO_ENTRY")
        self.assertFalse(continuation["route_start_constraint_applies"])
        self.assertIsNone(continuation["route_start_constraint_satisfied"])
        self.assertTrue(continuation["virtual_continuation"])
        self.assertTrue(continuation["virtual_entry_is_not_physical_path"])
        self.assertEqual(continuation["entry_label"], "物理断开后的虚拟续接")
        self.assertEqual(
            continuation["continuation_break_reason"],
            "physical_graph_disconnected_from_floor_route_start",
        )
        self.assertEqual(plan["route_start_target_ids"], ["START"])
        self.assertTrue(plan["route_start_constraint_satisfied"])
        self.assertEqual(plan["virtual_entry_count"], 1)
        self.assertEqual(
            plan["virtual_continuation_segment_ids"],
            [continuation["segment_id"]],
        )

    def test_isolated_legal_entrance_becomes_a_singleton_first_segment(self) -> None:
        candidates = [
            _candidate("START", "消防电梯", raw_name="消防电梯"),
            _candidate("REQUIRED", "火灾探测器", raw_name="感烟探测器", mandatory=True),
        ]
        floor = {
            "floor_id": "F2",
            "floor_scale": 100.0,
            "candidates": candidates,
            "requirements": [
                {
                    "requirement_id": "mandatory_all",
                    "type": "mandatory_all",
                    "candidate_ids": ["REQUIRED"],
                    "required_count": 1,
                }
            ],
        }
        recommendation = {
            "selection_scores": {"START": 0.7, "REQUIRED": 0.9},
            "successor_top_k": {},
            "pair_selection_top_k": {},
        }
        closure = _FakeClosure(
            ["START", "REQUIRED"],
            {"START": 0, "REQUIRED": 1},
        )

        plan, _graph = stage._s09_planner._plan_floor(
            "F2",
            floor,
            closure,
            recommendation,
            "RUN__F2",
            stage._s09_planner.DualGraphConfig(
                dmax_ratio=0.8,
                require_complete_rgcn_scores=True,
                expand_physical_walk=False,
                write_dxf=False,
            ),
        )

        self.assertTrue(plan["feasible"])
        self.assertEqual(plan["route_segments"][0]["ordered_target_ids"], ["START"])
        self.assertEqual(plan["route_segments"][1]["entry_target_id"], "REQUIRED")
        self.assertEqual(plan["virtual_entry_count"], 1)

    def test_secondary_building_without_own_entrance_is_virtual_continuation(self) -> None:
        required = _candidate(
            "REQUIRED",
            "\u706b\u707e\u63a2\u6d4b\u5668",
            raw_name="SMOKE",
            mandatory=True,
        )
        floor = {
            "floor_id": "F1__B02",
            "floor_scale": 100.0,
            "candidates": [required],
            "requirements": [
                {
                    "requirement_id": "mandatory_all",
                    "type": "mandatory_all",
                    "candidate_ids": ["REQUIRED"],
                    "required_count": 1,
                }
            ],
        }
        recommendation = {
            "selection_scores": {"REQUIRED": 0.9},
            "successor_top_k": {},
            "pair_selection_top_k": {},
        }

        plan, _graph = stage._s09_planner._plan_floor(
            "F1__B02",
            floor,
            _FakeClosure(["REQUIRED"]),
            recommendation,
            "RUN__F1__B02",
            stage._s09_planner.DualGraphConfig(
                dmax_ratio=0.8,
                require_complete_rgcn_scores=True,
                expand_physical_walk=False,
                write_dxf=False,
            ),
            physical_floor_id="F1",
            physical_floor_primary_scope_id="F1__B01",
            require_physical_floor_route_start=False,
        )

        self.assertTrue(plan["feasible"])
        self.assertEqual(plan["physical_floor_id"], "F1")
        self.assertEqual(plan["route_start_target_ids"], [])
        self.assertIsNone(plan["route_start_constraint_satisfied"])
        first = plan["route_segments"][0]
        self.assertEqual(first["entry_target_id"], "REQUIRED")
        self.assertTrue(first["virtual_continuation"])
        self.assertFalse(first["route_start_constraint_applies"])
        self.assertEqual(first["entry_label"], "\u7269\u7406\u65ad\u5f00\u540e\u7684\u865a\u62df\u7eed\u63a5")
        self.assertEqual(
            first["continuation_break_reason"],
            "building_scope_disconnected_from_physical_floor_route_start",
        )

    def test_building_scope_id_is_grouped_to_physical_floor(self) -> None:
        self.assertEqual(
            stage._s09_planner._physical_floor_id("F3__B02", {}),
            "F3",
        )


if __name__ == "__main__":
    unittest.main()
