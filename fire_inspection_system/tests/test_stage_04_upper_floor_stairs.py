from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from fire_inspection_system.stages import stage_04_inspection_objects as stage


class UpperFloorStairRecognitionTests(unittest.TestCase):
    def test_floor_gate(self) -> None:
        for value in ("F2", "2F", "2层", "二楼", "F7__B01", "ROOF", "屋顶层", "EQUIPMENT"):
            with self.subTest(value=value):
                self.assertTrue(stage._s04_library.is_second_floor_or_above(value))
        for value in ("F1", "1F", "首层", "B1", "地下二层", "UNKNOWN", ""):
            with self.subTest(value=value):
                self.assertFalse(stage._s04_library.is_second_floor_or_above(value))

    def test_alias_library_maps_only_upper_floor_stairs(self) -> None:
        upper = {"term": "楼梯间", "source_type": "text", "source_floor_id": "F2", "floor_name": "2层"}
        first = {"term": "楼梯间", "source_type": "text", "source_floor_id": "F1", "floor_name": "首层"}

        upper_decision = stage._s04_llm.local_library_decision(upper)

        self.assertEqual(upper_decision["standard_class_name"], "安全出口")
        self.assertEqual(upper_decision["reason"], "inspection_upper_floor_alias")
        for layer_name in ("A-STRS", "HS-A-楼梯", "STAIR"):
            with self.subTest(layer_name=layer_name):
                layer = {"term": layer_name, "source_type": "layer", "layer": layer_name, "source_floor_id": "F3", "floor_name": "3层"}
                layer_decision = stage._s04_llm.local_library_decision(layer)
                self.assertEqual(layer_decision["standard_class_name"], "安全出口")
        self.assertIsNone(stage._s04_llm.local_library_decision(first))

    def test_model_fallback_cannot_promote_first_floor_stair(self) -> None:
        raw = {"role": "inspection_object", "standard_class_name": "安全出口", "confidence": 0.95}
        upper = {"term": "专业疏散楼梯间", "source_type": "text", "source_floor_id": "F3", "floor_name": "3层"}
        first = {"term": "专业疏散楼梯间", "source_type": "text", "source_floor_id": "F1", "floor_name": "首层"}

        self.assertEqual(stage._s04_llm.normalize_llm_decision(raw, upper)["standard_class_name"], "安全出口")
        first_decision = stage._s04_llm.normalize_llm_decision(raw, first)
        self.assertEqual(first_decision["role"], "IGNORE")
        self.assertEqual(first_decision["reason"], "stair_below_second_floor_not_safety_exit")

    def test_upper_floor_fire_door_is_still_not_a_safety_exit(self) -> None:
        raw = {"role": "inspection_object", "standard_class_name": "安全出口", "confidence": 0.95}
        fire_door = {"term": "FM乙12", "source_type": "text", "layer": "DOOR_FIRE_TEXT", "source_floor_id": "F3", "floor_name": "3层"}

        decision = stage._s04_llm.normalize_llm_decision(raw, fire_door)

        self.assertEqual(decision["role"], "IGNORE")
        self.assertEqual(decision["reason"], "fire_door_not_safety_exit")

    def test_stair_layer_lines_are_clustered_only_on_upper_floor(self) -> None:
        fieldnames = ["object_id", "entity_type", "layer", "geometry_kind", "bbox_minx", "bbox_miny", "bbox_maxx", "bbox_maxy"]
        with tempfile.TemporaryDirectory() as temp_dir:
            inventory_dir = Path(temp_dir)
            inventory_path = inventory_dir / stage._s04_region.FULL_INVENTORY_FILE
            with inventory_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                for floor_x, prefix in ((1000, "F2"), (21000, "F1")):
                    for index in range(6):
                        y = 1000 + index * 200
                        writer.writerow({
                            "object_id": f"{prefix}_{index}",
                            "entity_type": "LINE",
                            "layer": "STAIR",
                            "geometry_kind": "line",
                            "bbox_minx": floor_x,
                            "bbox_miny": y,
                            "bbox_maxx": floor_x + 3000,
                            "bbox_maxy": y,
                        })
            regions = [
                {"sheet_id": "SHEET_F2", "source_floor_id": "F2", "floor_id": "F2", "floor_name": "2层", "_bbox": (0, 0, 10000, 10000), "_area": 100_000_000},
                {"sheet_id": "SHEET_F1", "source_floor_id": "F1", "floor_id": "F1", "floor_name": "首层", "_bbox": (20000, 0, 30000, 10000), "_area": 100_000_000},
            ]

            clusters = stage._s04_region.build_upper_floor_stair_cluster_rows(inventory_dir, regions)

        self.assertEqual(len(clusters), 1)
        region, row = clusters[0]
        self.assertEqual(region["source_floor_id"], "F2")
        self.assertEqual(row["norm_text"], "楼梯")
        self.assertEqual(row["layer"], "STAIR")
        self.assertEqual(row["geometry_kind"], "stair_layer_cluster")

    def test_region_pipeline_outputs_only_upper_floor_stair_exit(self) -> None:
        inventory_fields = [
            "object_id", "layout", "source", "depth", "insert_depth", "entity_type", "handle", "layer", "color",
            "true_color", "linetype", "lineweight", "parent_block_name", "block_path", "insert_path", "raw_text",
            "norm_text", "geometry_kind", "is_closed", "x", "y", "bbox_minx", "bbox_miny", "bbox_maxx",
            "bbox_maxy", "bbox_area",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inventory_dir = root / "inventory"
            output_dir = root / "output"
            inventory_dir.mkdir()
            with (inventory_dir / stage._s04_region.SEMANTIC_INVENTORY_FILE).open("w", encoding="utf-8-sig", newline="") as handle:
                csv.DictWriter(handle, fieldnames=inventory_fields).writeheader()
            with (inventory_dir / stage._s04_region.FULL_INVENTORY_FILE).open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=inventory_fields)
                writer.writeheader()
                for floor_x, prefix in ((1000, "F2"), (21000, "F1")):
                    for index in range(6):
                        y = 1000 + index * 200
                        writer.writerow({
                            "object_id": f"{prefix}_{index}", "layout": "Model", "source": "modelspace", "depth": 0,
                            "insert_depth": 0, "entity_type": "LINE", "layer": "A-STRS", "geometry_kind": "line",
                            "bbox_minx": floor_x, "bbox_miny": y, "bbox_maxx": floor_x + 3000, "bbox_maxy": y,
                        })
            sheets_path = root / "sheets.json"
            sheets_path.write_text(json.dumps({"sheets": [
                {"sheet_id": "SHEET_F2", "floor_id": "F2", "floor_name": "2层", "floor_confidence": 1.0, "path_planning_usable": True, "bbox": [0, 0, 10000, 10000]},
                {"sheet_id": "SHEET_F1", "floor_id": "F1", "floor_name": "首层", "floor_confidence": 1.0, "path_planning_usable": True, "bbox": [20000, 0, 30000, 10000]},
            ]}, ensure_ascii=False), encoding="utf-8")

            result = stage._s04_region.run_pipeline(inventory_dir, sheets_path, output_dir, root / "unused.py", no_llm=True)

        self.assertEqual(result["upper_floor_stair_cluster_count"], 1)
        floors = {floor["source_floor_id"]: floor for floor in result["floors"]}
        upper_stairs = [row for row in floors["F2"]["catalog_rows"] if row["semantic_name"] == "楼梯" and row["standard_class_name"] == "安全出口"]
        first_stairs = [row for row in floors["F1"]["catalog_rows"] if row["semantic_name"] == "楼梯" and row["standard_class_name"] == "安全出口"]
        self.assertEqual(sum(row["count"] for row in upper_stairs), 1)
        self.assertEqual(first_stairs, [])


if __name__ == "__main__":
    unittest.main()
