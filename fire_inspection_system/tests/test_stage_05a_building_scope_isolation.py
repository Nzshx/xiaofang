from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from shapely.geometry import GeometryCollection, Point, box, shape

from fire_inspection_system.stages import stage_05A_obstacles as stage05a
from fire_inspection_system.stages import stage_05_obstacles as stage05
from fire_inspection_system.stages import stage_06_navigation_graph as stage06


def _building(building_id: str, min_x: float, max_x: float) -> dict[str, object]:
    ring = [
        [min_x, 0.0],
        [max_x, 0.0],
        [max_x, 10.0],
        [min_x, 10.0],
        [min_x, 0.0],
    ]
    return {
        "building_id": building_id,
        "building_index": int(building_id[-2:]),
        "polygon_cad": ring,
        "parts_cad": [ring],
        "structural_parts_cad": [ring],
        "corridor_connection_group": "C001",
        "connected_building_group_id": "C001",
    }


class BuildingScopeIsolationTests(unittest.TestCase):
    def test_generic_facade_number_layer_is_forced_non_obstacle(self) -> None:
        decisions = {
            "facade-4": {
                "layer": "facade-4",
                "role": "obstacle_candidate",
                "candidate_types": ["wall"],
                "confidence": 0.7,
                "reason": "Facade represents exterior wall.",
                "llm_returned": True,
            }
        }

        overridden = stage05._s05_obstacles.apply_deterministic_layer_overrides(decisions)

        self.assertTrue(stage05._s05_obstacles.layer_is_forced_non_obstacle("facade-4"))
        self.assertEqual(overridden["facade-4"]["role"], "not_obstacle")
        self.assertEqual(overridden["facade-4"]["candidate_types"], [])
        self.assertEqual(overridden["facade-4"]["overrode_llm_role"], "obstacle_candidate")
        self.assertEqual(
            stage05._s05_obstacles.row_candidate_types(
                {"layer": "facade-4"},
                decisions,
            ),
            [],
        )

    def test_upper_floor_visual_building_boundary_becomes_closed_obstacle(self) -> None:
        upper = _building("B01", 0.0, 100.0)
        upper.update({"building_scope_id": "F4__B01", "structural_parts": upper["structural_parts_cad"]})
        lower = _building("B01", 200.0, 300.0)
        lower.update({"building_scope_id": "F2__B01", "structural_parts": lower["structural_parts_cad"]})
        sheets = {
            "sheets": [
                {
                    "floor_id": "F4",
                    "inspection_regions": [
                        {
                            "building_region_detection": {"status": "ok", "validation_status": "vector_validated"},
                            "building_regions": [upper],
                        }
                    ],
                },
                {
                    "floor_id": "F2",
                    "inspection_regions": [
                        {
                            "building_region_detection": {"status": "ok", "validation_status": "vector_validated"},
                            "building_regions": [lower],
                        }
                    ],
                },
            ]
        }

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "sheets.json"
            source.write_text(json.dumps(sheets, ensure_ascii=False), encoding="utf-8")
            result = stage05a.write_upper_floor_building_envelope_obstacles(source, tmp)
            payload = json.loads(Path(result["geojson_path"]).read_text(encoding="utf-8"))

        self.assertEqual(result["obstacle_count"], 1)
        self.assertEqual(payload["features"][0]["properties"]["floor_id"], "F4__B01")
        envelope = shape(payload["features"][0]["geometry"])
        self.assertTrue(envelope.covers(Point(0.01, 5.0)))
        self.assertFalse(envelope.covers(Point(50.0, 5.0)))

    def test_visual_outer_region_snaps_to_red_wall_and_closes_wall_gap(self) -> None:
        transform = stage05a._make_transform((0.0, 0.0, 100.0, 100.0), 101, 101, 0)
        record = {
            "cad_bbox": [0.0, 0.0, 100.0, 100.0],
            "transform": transform,
        }
        mask = np.zeros((101, 101), dtype=np.uint8)
        mask[10, 10:91] = 255
        mask[90, 10:91] = 255
        mask[10:41, 10] = 255
        mask[61:91, 10] = 255  # deliberate missing exterior-wall fragment
        mask[10:91, 90] = 255

        completion, audit = stage05a._snapped_outer_wall_completion(
            record,
            mask,
            box(0.0, 0.0, 100.0, 100.0),
        )

        self.assertEqual(audit["snapped_segment_count"], 4)
        self.assertTrue(completion.covers(Point(10.0, 50.0)))
        self.assertFalse(completion.covers(Point(0.0, 50.0)))

    def test_first_floor_does_not_bypass_building_isolation(self) -> None:
        sheets = {
            "sheets": [
                {
                    "sheet_id": "S1",
                    "floor_id": "F1",
                    "path_planning_usable": True,
                    "inspection_regions": [
                        {"region_id": "R01", "bbox": [0.0, 0.0, 20.0, 10.0], "source": "title_block"}
                    ],
                }
            ]
        }
        detections = [
            {
                "full_region_id": "S1:R01",
                "status": "ok",
                "building_count": 2,
                "validation_status": "vector_validated",
                "needs_review": False,
                "buildings": [_building("B01", 0.0, 10.0), _building("B02", 10.0, 20.0)],
                "corridor_connections": [
                    {"corridor_connection_group": "C001", "validation_method": "shared_free_boundary_vector_clearance"}
                ],
            }
        ]

        merged = stage05a._merge_building_regions_into_sheets(sheets, detections)
        buildings = merged["sheets"][0]["building_regions"]

        self.assertEqual(
            [row["building_scope_id"] for row in buildings],
            ["F1__B01", "F1__B02"],
        )
        self.assertTrue(all(not row["corridor_connection_verified"] for row in buildings))
        self.assertTrue(
            all(row["detected_connection_rejected_for_planning"] for row in buildings)
        )

    def test_unvalidated_candidate_regions_still_isolate_buildings(self) -> None:
        buildings = [_building("B01", 0.0, 10.0), _building("B02", 10.0, 20.0)]
        for row in buildings:
            row.update(
                {
                    "parts": row["structural_parts_cad"],
                    "polygon": row["polygon_cad"],
                    "bbox": [0.0, 0.0, 10.0, 10.0]
                    if row["building_id"] == "B01"
                    else [10.0, 0.0, 20.0, 10.0],
                }
            )
        sheets = {
            "sheets": [
                {
                    "sheet_id": "S1",
                    "floor_id": "F6",
                    "floor_name": "6F",
                    "path_planning_usable": True,
                    "inspection_regions": [
                        {
                            "region_id": "R01",
                            "bbox": [0.0, 0.0, 20.0, 10.0],
                            "source": "title_block",
                            "building_regions": buildings,
                            "building_region_detection": {
                                "status": "ok",
                                "validation_status": "vision_candidate_only",
                                "needs_review": True,
                            },
                        }
                    ],
                }
            ]
        }

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sheets.json"
            path.write_text(json.dumps(sheets, ensure_ascii=False), encoding="utf-8")
            regions = stage05._s05_obstacles.load_floor_regions(path)

        self.assertEqual([row["floor_id"] for row in regions], ["F6__B01", "F6__B02"])
        self.assertTrue(all(row["building_region_isolation_only"] for row in regions))

    def test_shared_boundary_without_trusted_connection_does_not_merge(self) -> None:
        domains = {"F2__B01": box(0.0, 0.0, 10.0, 10.0), "F2__B02": box(10.0, 0.0, 20.0, 10.0)}
        members = {
            "F2__B01": [
                {
                    "source_floor_id": "F2",
                    "building_id": "B01",
                    "corridor_connection_group": "C001",
                    "corridor_connection_verified": False,
                    "corridor_connection_verification": "",
                    "polygon": domains["F2__B01"],
                }
            ],
            "F2__B02": [
                {
                    "source_floor_id": "F2",
                    "building_id": "B02",
                    "corridor_connection_group": "C001",
                    "corridor_connection_verified": False,
                    "corridor_connection_verification": "",
                    "polygon": domains["F2__B02"],
                }
            ],
        }

        result = stage06._s06_inputs._merge_vector_verified_corridor_scopes(
            domains,
            {"F2__B01": "2F", "F2__B02": "2F"},
            members,
            {"F2__B01": GeometryCollection(), "F2__B02": GeometryCollection()},
            {"F2__B01", "F2__B02"},
            {},
        )
        merged_domains, _names, _members, _obstacles, _covered, _clipping, connections, remap = result

        self.assertEqual(set(merged_domains), {"F2__B01", "F2__B02"})
        self.assertEqual(connections, [])
        self.assertEqual(remap, {"F2__B01": "F2__B01", "F2__B02": "F2__B02"})


if __name__ == "__main__":
    unittest.main()
