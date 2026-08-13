from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fire_inspection_system.stages import stage_10_route_outputs as stage


class VirtualContinuationOutputTests(unittest.TestCase):
    def test_locked_plan_and_csv_preserve_virtual_continuation_audit(self) -> None:
        optimized = {
            "floors": {
                "F2": {
                    "feasible": True,
                    "status": "feasible",
                    "solver": "test",
                    "length": 1.0,
                    "order": ["START", "NEXT"],
                    "selected_target_ids": ["START", "NEXT"],
                    "route_segments": [
                        {
                            "segment_id": "F2_ROUTE_001",
                            "entry_mode": "floor_route_start_at_inspection_entry_object",
                            "entry_label": "楼层首段：安全出口或消防电梯起点",
                            "virtual_entry_id": "",
                            "virtual_continuation": False,
                            "virtual_entry_is_not_physical_path": False,
                            "continuation_break_reason": "",
                            "physically_reachable_from_floor_route_start": True,
                            "route_start_constraint_applies": True,
                            "route_start_constraint_satisfied": True,
                            "entry_target_id": "START",
                            "entry_target_class_name": "安全出口",
                            "physical_component_id": 0,
                            "ordered_target_ids": ["START"],
                        },
                        {
                            "segment_id": "F2_ROUTE_002",
                            "entry_mode": "virtual_continuation_after_physical_or_dmax_disconnect",
                            "entry_label": "物理断开后的虚拟续接",
                            "virtual_entry_id": "VIRTUAL_ENTRY::F2_ROUTE_002",
                            "virtual_continuation": True,
                            "virtual_entry_is_not_physical_path": True,
                            "continuation_break_reason": "physical_graph_disconnected_from_floor_route_start",
                            "physically_reachable_from_floor_route_start": False,
                            "route_start_constraint_applies": False,
                            "route_start_constraint_satisfied": None,
                            "entry_target_id": "NEXT",
                            "entry_target_class_name": "火灾探测器",
                            "physical_component_id": 1,
                            "ordered_target_ids": ["NEXT"],
                        },
                    ],
                }
            }
        }
        candidates = {
            "floors": {
                "F2": {
                    "candidates": [
                        {"target_id": "START", "class_name": "安全出口", "point": [0.0, 0.0]},
                        {"target_id": "NEXT", "class_name": "火灾探测器", "point": [1.0, 1.0]},
                    ]
                }
            }
        }

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            optimized_path = output / "optimized.json"
            candidates_path = output / "candidates.json"
            optimized_path.write_text(json.dumps(optimized, ensure_ascii=False), encoding="utf-8")
            candidates_path.write_text(json.dumps(candidates, ensure_ascii=False), encoding="utf-8")

            visit_plan = stage._s10_control.build_locked_visit_plan(
                optimized_path,
                candidates_path,
            )
            continuation = visit_plan["floors"]["F2"]["route_segments"][1]
            target = visit_plan["floors"]["F2"]["targets"][1]

            self.assertEqual(continuation["entry_label"], "物理断开后的虚拟续接")
            self.assertTrue(continuation["virtual_entry_is_not_physical_path"])
            self.assertIsNone(continuation["route_start_constraint_satisfied"])
            self.assertTrue(target["is_route_segment_entry"])
            self.assertTrue(target["segment_virtual_continuation"])

            outputs = stage._s10_annotated.write_inspection_visit_order(output, visit_plan)
            header = Path(outputs["inspection_target_visit_order_csv"]).read_text(
                encoding="utf-8-sig"
            ).splitlines()[0]
            self.assertIn("segment_entry_label", header)
            self.assertIn("segment_virtual_entry_is_not_physical_path", header)


if __name__ == "__main__":
    unittest.main()
