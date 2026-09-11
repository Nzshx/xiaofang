from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from multi_drawing_pipeline.common import write_json
from multi_drawing_pipeline.stages.stage_02c_building_objects import (
    filter_building_annotations,
    materialize_building_libraries,
)
from multi_drawing_pipeline.stages.stage_05b_route_targets import run_stage as run_target_stage
from multi_drawing_pipeline.stages.stage_06_versioned_environment import run_stage as run_version_stage
from multi_drawing_pipeline.stages.stage_07_route_planning import (
    build_architectural_route_starts,
    build_route_inspection_adapter,
)


class DisciplineIsolationTests(unittest.TestCase):
    def test_building_library_and_output_exclude_professional_classes(self) -> None:
        allowed = {"安全出口", "防火卷帘"}
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw)
            aliases, patterns = materialize_building_libraries(
                allowed_classes=allowed,
                output_dir=output,
            )
            alias_names = {
                row["canonical"] for row in json.loads(aliases.read_text(encoding="utf-8"))["objects"]
            }
            pattern_names = {
                row["canonical"] for row in json.loads(patterns.read_text(encoding="utf-8"))["rules"]
            }
            self.assertLessEqual(alias_names, allowed)
            self.assertLessEqual(pattern_names, allowed)
            accepted, rejected = filter_building_annotations(
                [
                    {
                        "object_id": "A",
                        "standard_class_name": "安全出口",
                        "floor_id": "F1",
                        "bbox": [0, 0, 2, 2],
                    },
                    {
                        "object_id": "B",
                        "standard_class_name": "喷头",
                        "floor_id": "F1",
                        "bbox": [5, 5, 7, 7],
                    },
                ],
                source_drawing=output / "building.dxf",
                allowed_classes=allowed,
            )
            self.assertEqual([row["category"] for row in accepted], ["安全出口"])
            self.assertEqual([row["category"] for row in rejected], ["喷头"])

    def test_building_only_targets_are_valid(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            migration = root / "migration.json"
            building = root / "building.json"
            fused = root / "building.dxf"
            write_json(migration, [])
            write_json(building, [{
                "target_id": "B-1",
                "detection_id": "B-1",
                "discipline": "building",
                "category": "安全出口",
                "target_class": "安全出口",
                "floor_id": "F1",
                "floor": "F1",
                "x": 10.0,
                "y": 20.0,
            }])
            fused.write_text("test", encoding="utf-8")
            result = run_target_stage(
                {}, root / "sheets.json", migration, fused,
                root / "obstacles.geojson", root / "stage",
                building_objects_path=building,
            )
            self.assertEqual(result["building_object_count"], 1)
            self.assertEqual(result["professional_object_count"], 0)
            self.assertEqual(result["route_target_count"], 1)


class EnvironmentVersionTests(unittest.TestCase):
    def test_identical_inputs_reuse_sbm_object_set_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            building = root / "building.dxf"
            floors = root / "floors.json"
            obstacles = root / "obstacles.geojson"
            obstacle_result = root / "obstacle_result.json"
            targets = root / "targets.json"
            registrations = root / "registrations.json"
            scope = root / "scope.json"
            building.write_text("building-vector", encoding="utf-8")
            write_json(floors, {"sheets": []})
            write_json(obstacles, {
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "properties": {"floor": "F1"},
                    "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
                }],
            })
            write_json(obstacle_result, {"obstacle_count": 1})
            write_json(targets, [{
                "target_id": "B-1", "discipline": "building", "target_class": "安全出口",
                "category": "安全出口", "floor_id": "F1", "floor": "F1", "x": 1, "y": 1,
            }])
            write_json(registrations, [])
            write_json(scope, {"building": {"allowed_classes": ["安全出口"]}})
            prepared = {"drawings": [{"discipline": "building", "dxf": str(building)}]}
            obstacle_summary = {
                "backend": "test",
                "effective_planning_sheets": str(floors),
                "obstacles_geojson": str(obstacles),
                "reference_obstacle_result": str(obstacle_result),
                "annotated_dxf": "",
            }
            arguments = dict(
                prepared_payload=prepared,
                obstacle_summary=obstacle_summary,
                route_targets_path=targets,
                registrations_path=registrations,
                scope_policy_path=scope,
                stage_dir=root / "run" / "versions",
                artifact_root=root / "artifacts",
            )
            first = run_version_stage(**arguments)
            second = run_version_stage(**arguments)
            self.assertEqual(first["environment"]["version"], second["environment"]["version"])
            self.assertTrue(second["sbm"]["cache_hit"])
            self.assertTrue(second["object_set"]["cache_hit"])
            self.assertTrue(second["environment"]["cache_hit"])
            connection = sqlite3.connect(second["registry"])
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM sbm_versions").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM object_set_versions").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM environment_versions").fetchone()[0], 1)
            finally:
                connection.close()


class ArchitecturalStartTests(unittest.TestCase):
    def test_only_building_objects_can_seed_route_starts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            targets = root / "targets.json"
            sheets = root / "sheets.json"
            output = root / "with_starts.json"
            write_json(targets, [
                {
                    "target_id": "B-EXIT", "object_id": "B-EXIT", "discipline": "building",
                    "target_class": "安全出口", "category": "安全出口", "floor_id": "F1",
                    "x": 10, "y": 10, "source_sheet_id": "S1", "confidence": 1.0,
                },
                {
                    "target_id": "B-SHUTTER", "discipline": "building",
                    "target_class": "防火卷帘", "category": "防火卷帘", "floor_id": "F2",
                    "x": 120, "y": 20, "source_sheet_id": "S2", "confidence": 1.0,
                },
                {
                    "target_id": "E-SIGN", "discipline": "electrical",
                    "target_class": "消防应急照明和疏散指示标志",
                    "category": "消防应急照明和疏散指示标志", "floor_id": "F2",
                    "x": 150, "y": 20, "confidence": 1.0,
                },
            ])
            write_json(sheets, {
                "input_dxf": str(root / "building.dxf"),
                "sheets": [
                    {"sheet_id": "S1", "floor_id": "F1", "floor_name": "1层", "bbox": [0, 0, 100, 100], "path_planning_usable": True},
                    {"sheet_id": "S2", "floor_id": "F2", "floor_name": "2层", "bbox": [100, 0, 200, 100], "path_planning_usable": True},
                ],
            })
            result = build_architectural_route_starts(targets, sheets, output, object())
            self.assertEqual(result["route_anchor_count"], 2)
            self.assertEqual(result["adapter_target_count"], 4)
            self.assertTrue(all(row["discipline"] == "building" for row in result["anchors"]))
            self.assertEqual(
                {row["route_anchor_provenance"] for row in result["anchors"]},
                {"explicit_architectural_symbol", "normalized_architectural_floor_frame_transfer"},
            )

    def test_route_adapter_does_not_label_building_objects_as_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            targets = root / "targets.json"
            sheets = root / "sheets.json"
            write_json(targets, [
                {
                    "target_id": "B-EXIT", "discipline": "building",
                    "target_class": "安全出口", "category": "安全出口",
                    "floor_id": "F1", "x": 10, "y": 10,
                },
                {
                    "target_id": "W-SPRINKLER", "discipline": "water",
                    "target_class": "喷头", "category": "喷头",
                    "floor_id": "F1", "x": 20, "y": 20,
                },
            ])
            write_json(sheets, {
                "sheets": [{
                    "sheet_id": "S1", "floor_id": "F1", "floor_name": "1层",
                    "bbox": [0, 0, 100, 100], "path_planning_usable": True,
                }],
            })
            result = build_route_inspection_adapter(targets, sheets, root / "adapter")
            payload = json.loads(Path(result["result_json"]).read_text(encoding="utf-8"))
            building = next(row for row in payload["catalog_rows"] if row["discipline"] == "building")
            water = next(row for row in payload["catalog_rows"] if row["discipline"] == "water")
            self.assertIn(":BLD_", building["signature_id"])
            self.assertEqual(building["source"], "building_object_identity")
            self.assertEqual(building["layer"], "建筑原生巡检对象")
            self.assertIn(":MIG_", water["signature_id"])
            self.assertEqual(water["source"], "migrated_professional_entity")
            self.assertEqual(water["layer"], "专业迁移巡检对象")


if __name__ == "__main__":
    unittest.main()
