from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import ezdxf
from ezdxf.addons.importer import Importer
from ezdxf.math import Matrix44
from shapely.geometry import LineString, box

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from multi_drawing_pipeline.common import Detection, Registration, Sheet, normalize_floor, parse_building_ids, parse_floor_scope
from multi_drawing_pipeline.stages.stage_01_inputs import infer_discipline, infer_role
from multi_drawing_pipeline.stages.stage_02_sheets import build_sheets, classify_title
from multi_drawing_pipeline.stages.stage_02b_building_obstacles import (
    _detect_structural_insert_obstacles,
    _full_layer_decisions,
    _polygonize_column_segments,
    _regions_intersecting_segment,
    classify_obstacle_layer,
    recognize_building_obstacles,
)
from multi_drawing_pipeline.stages.stage_03_recognize import (
    _block_profiles,
    _classify_entity,
    _classify_hvac_structural_symbol,
    _hvac_context_index,
    recognize_drawing,
)
from multi_drawing_pipeline.stages.stage_04_register import (
    _apply_frame_consensus,
    _features,
    _registration_acceptance_level,
    _transform_rank,
    register_sheet,
)
from multi_drawing_pipeline.stages.stage_05_migrate import (
    _import_entity,
    _inside_floor_occupancy,
    _is_continuous_infrastructure,
    _local_hvac_symbol_copy,
    _hvac_spatial_reference_status,
    _load_floor_occupancy_hulls,
    _prepare_direct_open_view,
    _transform_matrix,
)
from multi_drawing_pipeline.stages.fire_route_core import (
    stage_05B_raw_cad_collision,
    stage_10_route_outputs,
)
from multi_drawing_pipeline.stages.stage_05b_route_targets import (
    CONTINUOUS_INFRASTRUCTURE_CATEGORIES,
    build_discrete_route_targets,
    canonical_route_class,
)
from multi_drawing_pipeline.stages.room_topology import (
    RoomAnchor,
    SheetRoomTopology,
    WallIndex,
    guard_room_mapping,
)


class SemanticParsingTests(unittest.TestCase):
    def test_floor_range_and_special_floors(self) -> None:
        self.assertEqual(parse_floor_scope("1#楼七层至十四层平面图"), [f"F{i}" for i in range(7, 15)])
        self.assertEqual(parse_floor_scope("地下二层消防平面图"), ["B2"])
        self.assertEqual(parse_floor_scope("负一层平面图"), ["B1"])
        self.assertEqual(normalize_floor("屋顶给排水平面图"), "ROOF")
        self.assertEqual(normalize_floor("设备层电气平面图"), "EQUIPMENT")

    def test_building_ids_are_not_project_specific(self) -> None:
        self.assertEqual(parse_building_ids("1#、2#、3#楼五层平面图"), ["1", "2", "3"])
        self.assertEqual(parse_building_ids("12、15#楼二层平面图"), ["12", "15"])

    def test_title_classification(self) -> None:
        self.assertEqual(classify_title("二层自动喷淋平面图", "water"), "喷淋")
        self.assertEqual(classify_title("二层自喷给水平面图", "water"), "喷淋")
        self.assertEqual(classify_title("三层火灾自动报警平面图", "electrical"), "自动报警")
        self.assertIsNone(classify_title("火灾自动报警系统图", "electrical"))
        self.assertEqual(classify_title("屋顶防雷平面图", "electrical"), "电气平面")
        self.assertEqual(classify_title("一层平面及照明系统图", "electrical"), "照明")
        self.assertEqual(classify_title("负二层通风平面图", "hvac"), "暖通平面")
        self.assertEqual(classify_title("屋顶层防排烟平面图", "hvac"), "暖通平面")
        self.assertIsNone(classify_title("排烟系统原理图", "hvac"))
        self.assertIsNone(classify_title("具体参数详见机房层给排水消防平面图", "water"))


class RouteTargetSimplificationTests(unittest.TestCase):
    def test_continuous_entities_are_blocked_before_migration(self) -> None:
        pipe = Detection(
            "W-F1-P", "source.dxf", "water", "喷淋", "F1", "管网",
            1.0, 2.0, "LWPOLYLINE", "10", "PIPE", "", "", 0.99, "test",
        )
        wire = Detection(
            "E-F1-W", "source.dxf", "electrical", "报警", "F1", "火灾探测器",
            1.0, 2.0, "LINE", "11", "WIRE", "", "", 0.99, "test",
        )
        device = Detection(
            "E-F1-D", "source.dxf", "electrical", "报警", "F1", "火灾探测器",
            1.0, 2.0, "INSERT", "12", "DEVICE", "探测器", "", 0.99, "test",
        )
        self.assertTrue(_is_continuous_infrastructure(pipe))
        self.assertTrue(_is_continuous_infrastructure(wire))
        self.assertFalse(_is_continuous_infrastructure(device))

    def test_continuous_infrastructure_is_excluded_not_rejected(self) -> None:
        rows = [
            {"detection_id": "S", "migrated": True, "category": "喷头", "target_x": 1.0, "target_y": 2.0},
            {"detection_id": "P", "migrated": True, "category": "管网", "source_entity_type": "LWPOLYLINE", "target_x": 3.0, "target_y": 4.0},
            {"detection_id": "W", "migrated": True, "category": "应急广播及警报装置", "source_entity_type": "LINE", "target_x": 5.0, "target_y": 6.0},
        ]
        targets, excluded = build_discrete_route_targets(rows)
        self.assertEqual([row["detection_id"] for row in targets], ["S"])
        self.assertEqual({row["detection_id"] for row in excluded}, {"P", "W"})
        self.assertIn("管网", CONTINUOUS_INFRASTRUCTURE_CATEGORIES)

    def test_detailed_hvac_classes_use_route_constraint_vocabulary(self) -> None:
        self.assertEqual(canonical_route_class("排烟风机"), "加压风机（管道）/排烟风机（管道）/补风机（管道）")
        self.assertEqual(canonical_route_class("排烟口"), "机械排烟")

class InputDiscoveryTests(unittest.TestCase):
    def test_directory_driven_classification(self) -> None:
        root = Path(r"D:\example_project")
        discipline, confidence, _ = infer_discipline(root / "电气" / "任意名称.dwg", root)
        self.assertEqual(discipline, "electrical")
        self.assertGreater(confidence, 0.9)
        role, selected, _, _ = infer_role(root / "电气" / "火灾报警系统图.dwg", discipline)
        self.assertEqual(role, "professional_content_review")
        self.assertTrue(selected)
        role, selected, _, _ = infer_role(root / "建筑" / "某项目平面剖面图.dwg", "building")
        self.assertEqual(role, "target_building")
        self.assertTrue(selected)
        role, selected, _, _ = infer_role(root / "暖通" / "通风整包图.dwg", "hvac")
        self.assertEqual(role, "professional_plan")
        self.assertTrue(selected)


class SheetModelTests(unittest.TestCase):
    def test_physical_sheet_keeps_buildings_and_floor_range(self) -> None:
        candidate = {
            "drawing": "building.dxf", "discipline": "building", "kind": "建筑平面",
            "floor": "F7", "floors": ["F7", "F8", "F9"], "building_ids": ["1", "2"],
            "title": "1、2#楼七层至九层平面图", "x": 100.0, "y": 20.0, "height": 5.0,
            "min_x": 0.0, "min_y": 0.0, "max_x": 200.0, "max_y": 100.0,
            "detection_method": "test_frame", "confidence": 0.95,
        }
        sheets = build_sheets([candidate])
        self.assertEqual(len(sheets), 1)
        self.assertEqual(sheets[0].building_ids, ["1", "2"])
        self.assertEqual(sheets[0].floors, ["F7", "F8", "F9"])

    def test_outer_multi_floor_container_is_not_a_physical_sheet(self) -> None:
        base = {
            "drawing": "electrical.dxf", "discipline": "electrical", "kind": "动力配电",
            "building_ids": [], "height": 5.0, "detection_method": "test_frame", "confidence": 0.95,
        }
        candidates = [{
            **base, "floor": "F1", "floors": ["F1", "F2", "F3"],
            "title": "一至三层配电图集", "x": 500.0, "y": 20.0,
            "min_x": 0.0, "min_y": 0.0, "max_x": 1000.0, "max_y": 1000.0,
        }]
        for index, floor in enumerate(("F1", "F2", "F3")):
            min_x = 50.0 + index * 300.0
            candidates.append({
                **base, "floor": floor, "floors": [floor],
                "title": f"{index + 1}层配电平面图", "x": min_x + 100.0, "y": 100.0,
                "min_x": min_x, "min_y": 50.0, "max_x": min_x + 200.0, "max_y": 250.0,
            })
        sheets = build_sheets(candidates)
        self.assertEqual(len(sheets), 3)
        self.assertEqual({sheet.floor for sheet in sheets}, {"F1", "F2", "F3"})


class RecognitionBoundaryTests(unittest.TestCase):
    def test_recursive_nested_block_semantics_and_geometry(self) -> None:
        doc = ezdxf.new("R2013")
        child = doc.blocks.new("内部设备符号")
        child.add_circle((0, 0), 1)
        child.add_text("感烟探测器")
        parent = doc.blocks.new("匿名外层")
        parent.add_blockref("内部设备符号", (0, 0))
        xref = doc.blocks.new("Xref-整层建筑底图")
        xref.add_blockref("内部设备符号", (0, 0))
        semantics, graphics = _block_profiles(doc)
        self.assertIn("感烟探测器", semantics["匿名外层"])
        self.assertTrue(graphics["匿名外层"])
        self.assertEqual(semantics["Xref-整层建筑底图"], "")

    def test_linework_is_not_misclassified_as_equipment(self) -> None:
        doc = ezdxf.new("R2013")
        msp = doc.modelspace()
        sheet = Sheet(
            "source.dxf", "electrical", "自动报警", "F1", "一层报警平面图",
            0, 0, 50, 50, 0, 0, 100, 100, sheet_id="S", floors=["F1"],
        )
        broadcast_line = msp.add_line((0, 0), (10, 0), dxfattribs={"layer": "消防广播"})
        result = _classify_entity(broadcast_line, "electrical", sheet, "消防广播", {})
        self.assertIsNone(result)

        ordinary_frame = msp.add_lwpolyline([(0, 0), (10, 0)], dxfattribs={"layer": "A-WALL"})
        self.assertIsNone(_classify_entity(ordinary_frame, "electrical", sheet, "A-WALL", {}))

        detector_block = doc.blocks.new("任意项目_烟感")
        detector_block.add_circle((0, 0), 1)
        detector = msp.add_blockref("任意项目_烟感", (5, 5), dxfattribs={"layer": "任意设备层"})
        result = _classify_entity(detector, "electrical", sheet, "任意设备层|任意项目_烟感", {})
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "火灾探测器")

    def test_standalone_text_and_text_only_block_are_not_objects(self) -> None:
        doc = ezdxf.new("R2013")
        msp = doc.modelspace()
        sheet = Sheet(
            "source.dxf", "water", "给排水", "F1", "一层给排水平面图",
            0, 0, 50, 50, 0, 0, 100, 100, sheet_id="S", floors=["F1"],
        )
        label = msp.add_text("灭火器", dxfattribs={"layer": "设备标注"})
        self.assertIsNone(
            _classify_entity(label, "water", sheet, "设备标注|灭火器", {})
        )

        text_block = doc.blocks.new("灭火器说明")
        text_block.add_text("灭火器")
        annotation = msp.add_blockref("灭火器说明", (10, 10))
        self.assertIsNone(
            _classify_entity(
                annotation, "water", sheet, "灭火器说明|灭火器", {},
                {"灭火器说明": False},
            )
        )

    def test_hvac_accepts_equipment_symbols_but_never_duct_lines(self) -> None:
        doc = ezdxf.new("R2013")
        msp = doc.modelspace()
        sheet = Sheet(
            "source.dxf", "hvac", "暖通平面", "B2", "负二层通风平面图",
            0, 0, 50, 50, 0, 0, 100, 100, sheet_id="H", floors=["B2"],
        )
        duct = msp.add_lwpolyline([(0, 0), (20, 0)], dxfattribs={"layer": "ACS_YFG_F2"})
        self.assertIsNone(_classify_entity(duct, "hvac", sheet, "ACS_YFG_F2|排烟风管", {}))

        outlet_block = doc.blocks.new("多叶排烟口")
        outlet_block.add_circle((0, 0), 1)
        outlet = msp.add_blockref("多叶排烟口", (10, 10), dxfattribs={"layer": "ACS_YFG_FRZX"})
        result = _classify_entity(
            outlet, "hvac", sheet, "ACS_YFG_FRZX|多叶排烟口", {},
            {"多叶排烟口": True},
        )
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "排烟口")

    def test_hvac_structural_symbols_use_system_context_without_copying_ducts(self) -> None:
        doc = ezdxf.new("R2013")
        msp = doc.modelspace()
        sheet = Sheet(
            "source.dxf", "hvac", "暖通平面", "B1", "负一层通风平面图",
            0, 0, 50, 50, 0, 0, 100, 100, sheet_id="HVAC", floors=["B1"],
        )
        doc.layers.add("ACS_YFG_FRZX")
        doc.layers.add("ACS_YFG_FM_F")
        msp.add_line((0, 50), (100, 50), dxfattribs={"layer": "ACS_YFG_FRZX"})

        child = doc.blocks.new("ZlFj_YD_PingMian")
        child.add_circle((0, 0), 2)
        fan_block = doc.blocks.new("匿名风机外层")
        fan_block.add_blockref("ZlFj_YD_PingMian", (0, 0))
        fan = msp.add_blockref("匿名风机外层", (50, 51), dxfattribs={"layer": "暖通设备"})

        damper_block = doc.blocks.new("匿名阀门")
        damper_block.add_line((-1, -1), (1, 1))
        damper = msp.add_blockref("匿名阀门", (30, 50), dxfattribs={"layer": "ACS_YFG_FM_F"})

        semantics, graphics = _block_profiles(doc)
        context, limits = _hvac_context_index(doc, [sheet])
        fan_result = _classify_hvac_structural_symbol(
            fan, sheet, f"暖通设备|{semantics['匿名风机外层']}", context, limits, graphics,
        )
        damper_result = _classify_hvac_structural_symbol(
            damper, sheet, "ACS_YFG_FM_F|匿名阀门", context, limits, graphics,
        )
        self.assertEqual(fan_result[0], "排烟风机")
        self.assertEqual(damper_result[0], "排烟防火阀")

        doc.layers.add("ACS_JFG_FRZX")
        msp.add_line((0, 70), (100, 70), dxfattribs={"layer": "ACS_JFG_FRZX"})
        pressure_fan = msp.add_blockref("匿名风机外层", (50, 70.5), dxfattribs={"layer": "暖通设备"})
        context, limits = _hvac_context_index(doc, [sheet])
        pressure_result = _classify_hvac_structural_symbol(
            pressure_fan, sheet, f"暖通设备|{semantics['匿名风机外层']}", context, limits, graphics,
        )
        self.assertEqual(pressure_result[0], "加压风机")

        doc.layers.add("ACS_SFG_FM_F")
        supply_damper = msp.add_blockref("匿名阀门", (35, 50), dxfattribs={"layer": "ACS_SFG_FM_F"})
        supply_damper_result = _classify_hvac_structural_symbol(
            supply_damper, sheet, "ACS_SFG_FM_F|匿名阀门", context, limits, graphics,
        )
        self.assertEqual(supply_damper_result[0], "防火阀")

        make_up_block = doc.blocks.new("消防补风机")
        make_up_block.add_circle((0, 0), 2)
        make_up_fan = msp.add_blockref("消防补风机", (80, 30), dxfattribs={"layer": "暖通设备"})
        graphics["消防补风机"] = True
        make_up_result = _classify_hvac_structural_symbol(
            make_up_fan, sheet, "暖通设备|消防补风机", context, limits, graphics,
        )
        self.assertEqual(make_up_result[0], "补风机")

        doc.layers.add("补风风管")
        msp.add_line((0, 30), (100, 30), dxfattribs={"layer": "补风风管"})
        generic_fan_block = doc.blocks.new("FAN_GENERIC")
        generic_fan_block.add_circle((0, 0), 2)
        generic_make_up_fan = msp.add_blockref("FAN_GENERIC", (20, 30.5), dxfattribs={"layer": "暖通设备"})
        graphics["FAN_GENERIC"] = True
        context, limits = _hvac_context_index(doc, [sheet])
        generic_make_up_result = _classify_hvac_structural_symbol(
            generic_make_up_fan, sheet, "暖通设备|FAN_GENERIC", context, limits, graphics,
        )
        self.assertEqual(generic_make_up_result[0], "补风机")

        system_block = doc.blocks.new("排烟系统说明")
        system_block.add_circle((0, 0), 2)
        system = msp.add_blockref("排烟系统说明", (60, 50))
        self.assertIsNone(_classify_hvac_structural_symbol(
            system, sheet, "排烟系统原理图|排烟口", context, limits,
            {"排烟系统说明": True},
        ))

    def test_room_text_requires_and_binds_real_closed_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "electrical.dxf"
            doc = ezdxf.new("R2013")
            boundary = doc.modelspace().add_lwpolyline(
                [(10, 10), (40, 10), (40, 35), (10, 35)], close=True,
                dxfattribs={"layer": "A-ROOM"},
            )
            doc.modelspace().add_text("柴油发电机房", dxfattribs={"insert": (20, 20), "height": 2})
            doc.modelspace().add_lwpolyline(
                [(60, 10), (90, 10), (90, 35), (60, 35)], close=True,
                dxfattribs={"layer": "A-ROOM"},
            )
            doc.layers.add("A-ANNO-TABL")
            doc.modelspace().add_text(
                "柴油发电机房",
                dxfattribs={"insert": (70, 20), "height": 2, "layer": "A-ANNO-TABL"},
            )
            doc.saveas(source)
            sheet = Sheet(
                str(source), "electrical", "动力配电", "F1", "一层动力平面图",
                0, 0, 50, 50, 0, 0, 100, 100, sheet_id="S", floors=["F1"],
            )
            accepted, _review = recognize_drawing(source, "electrical", [sheet])
            rooms = [item for item in accepted if item["category"] == "备用发电机/柴油发电机房"]
            self.assertEqual(len(rooms), 1)
            self.assertEqual(rooms[0]["handle"], str(boundary.dxf.handle))
            self.assertEqual(rooms[0]["review_status"], "auto_accepted_room_boundary")


class BuildingObstacleTests(unittest.TestCase):
    def test_structural_column_insert_bbox_becomes_obstacle(self) -> None:
        class FakeApi:
            @staticmethod
            def add_obstacle(out, **kwargs):
                out.append({
                    **kwargs,
                    "geometry": kwargs["geom"],
                    "floor_id": kwargs["region"]["floor_id"],
                })

        rows = [{
            "object_id": "I1", "entity_type": "INSERT", "layer": "_A03-结构柱",
            "parent_block_name": "_FZH", "block_path": '["_FZH"]',
            "bbox_minx": "100", "bbox_miny": "100",
            "bbox_maxx": "300", "bbox_maxy": "300",
        }]
        regions = [{
            "polygon": box(0, 0, 10000, 10000), "full_region_id": "S1:R1",
            "floor_id": "F1", "source_floor_id": "F1", "sheet_id": "S1",
            "floor_name": "一层", "region_id": "R1",
        }]
        obstacles, stats = _detect_structural_insert_obstacles(
            FakeApi, rows, regions, [],
        )
        self.assertEqual(len(obstacles), 1)
        self.assertEqual(obstacles[0]["obstacle_type"], "column")
        self.assertEqual(obstacles[0]["geometry"].area, 40000.0)
        self.assertEqual(stats["accepted_structural_insert_count"], 1)

    def test_only_structural_architectural_layers_become_obstacles(self) -> None:
        doc = ezdxf.new("R2013")
        doc.layers.add("A-WALL")
        doc.layers.add("A-DOOR")
        doc.layers.add("A-OPENING")
        doc.layers.add("A-门洞")
        doc.modelspace().add_lwpolyline(
            [(10, 10), (90, 10), (90, 14), (10, 14)], close=True,
            dxfattribs={"layer": "A-WALL"},
        )
        doc.modelspace().add_lwpolyline(
            [(40, 10), (50, 10), (50, 15), (40, 15)], close=True,
            dxfattribs={"layer": "A-DOOR"},
        )
        doc.modelspace().add_lwpolyline(
            [(60, 10), (70, 10), (70, 15), (60, 15)], close=True,
            dxfattribs={"layer": "A-OPENING"},
        )
        doc.modelspace().add_lwpolyline(
            [(75, 10), (85, 10), (85, 15), (75, 15)], close=True,
            dxfattribs={"layer": "A-门洞"},
        )
        sheet = Sheet(
            "building.dxf", "building", "建筑平面", "F1", "一层平面图",
            0, 0, 50, 50, 0, 0, 100, 100, sheet_id="B", floors=["F1"],
        )
        obstacles, sources, door_masks = recognize_building_obstacles(doc, [sheet])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["layer"], "A-WALL")
        self.assertTrue(obstacles)
        # A door-named rectangle has no qualifying ARC and is not a confirmed door.
        self.assertEqual(len(door_masks), 0)
        self.assertIsNone(classify_obstacle_layer("A-DOOR"))
        self.assertIsNone(classify_obstacle_layer("A-OPENING"))

    def test_bare_chinese_wall_layer_is_an_obstacle(self) -> None:
        classified = classify_obstacle_layer("_A04-墙")
        self.assertIsNotNone(classified)
        self.assertEqual(classified[0], "wall")

    def test_layer_semantics_reuses_exact_layer_cache_when_llm_is_unavailable(self) -> None:
        class FakeApi:
            LAYER_LLM_FILE = "obstacle_layer_llm_decisions.json"

            @staticmethod
            def layer_summary(_rows):
                return [{"layer": "一般图层"}, {"layer": "_A04-墙"}]

            @staticmethod
            def classify_layers_by_llm(_rows, _output_dir):
                raise RuntimeError("HTTP 401")

            @staticmethod
            def apply_deterministic_layer_overrides(decisions):
                return decisions

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            cache = {
                "prompt_version": "legacy-version",
                "decisions": {
                    "一般图层": {
                        "layer": "一般图层", "role": "not_obstacle",
                        "candidate_types": [], "confidence": 0.9,
                        "reason": "cached", "llm_returned": True,
                    },
                },
            }
            (output_dir / FakeApi.LAYER_LLM_FILE).write_text(
                json.dumps(cache, ensure_ascii=False), encoding="utf-8",
            )
            decisions, audit = _full_layer_decisions(FakeApi, [], output_dir)
            self.assertEqual(decisions["一般图层"]["role"], "not_obstacle")
            self.assertEqual(decisions["_A04-墙"]["candidate_types"], ["wall"])
            self.assertEqual(audit["semantic_runtime"], "compatible_layer_cache_after_llm_failure")
            self.assertEqual(audit["reused_cached_llm_decision_count"], 1)

    def test_independent_column_strokes_are_closed_into_a_face(self) -> None:
        segments = [
            LineString([(10, 10), (20, 10)]),
            LineString([(20, 10), (20, 18)]),
            LineString([(20, 18), (10, 18)]),
            LineString([(10, 18), (10, 10)]),
        ]
        faces, reasons = _polygonize_column_segments(segments, box(0, 0, 1000, 1000))
        self.assertEqual(len(faces), 1)
        self.assertAlmostEqual(faces[0].area, 80.0)
        self.assertEqual(reasons["accepted_column_sized_closed_face"], 1)

    def test_open_column_strokes_do_not_create_an_obstacle(self) -> None:
        segments = [
            LineString([(10, 10), (20, 10)]),
            LineString([(20, 10), (20, 18)]),
            LineString([(20, 18), (10, 18)]),
        ]
        faces, _reasons = _polygonize_column_segments(segments, box(0, 0, 1000, 1000))
        self.assertEqual(len(faces), 0)

    def test_horizontal_and_vertical_column_lines_are_assigned_to_floor(self) -> None:
        regions = [{"polygon": box(0, 0, 100, 100), "full_region_id": "F1:R01"}]
        horizontal = LineString([(10, 20), (30, 20)])
        vertical = LineString([(40, 10), (40, 30)])
        self.assertEqual(_regions_intersecting_segment(horizontal, regions), regions)
        self.assertEqual(_regions_intersecting_segment(vertical, regions), regions)


class RegistrationTests(unittest.TestCase):
    def test_multi_floor_frame_consensus_recovers_no_anchor_series(self) -> None:
        sheets: list[Sheet] = []
        registrations: list[Registration] = []
        candidate_counts: dict[str, int] = {}
        for index in range(3):
            source = Sheet(
                "power.dxf", "electrical", "动力配电", f"F{index + 1}", f"{index + 1}层动力图",
                0, 0, 1100, index * 2000 + 500, 1000, index * 2000, 1200, index * 2000 + 1000,
                sheet_id=f"S{index}", floors=[f"F{index + 1}"], detection_method="insert_frame", confidence=0.95,
            )
            target = Sheet(
                "building.dxf", "building", "建筑平面", f"F{index + 1}", f"{index + 1}层建筑图",
                0, 0, 100, index * 3000 + 500, 0, index * 3000, 200, index * 3000 + 1000,
                sheet_id=f"T{index}", floors=[f"F{index + 1}"], detection_method="insert_frame", confidence=0.95,
            )
            sheets.extend([source, target])
            candidate_counts[source.sheet_id] = 1
            registrations.append(Registration(
                source.drawing, source.kind, source.floor, target.drawing,
                "common_geometry_similarity", False, 1, 1, 0, 0, 0, 0,
                0, 0, 0, math.inf, math.inf, "no anchors",
                source_sheet_id=source.sheet_id, target_sheet_id=target.sheet_id,
                source_floors=[source.floor], target_floors=[target.floor], fit_tolerance=10,
            ))
        applied = _apply_frame_consensus(registrations, sheets, candidate_counts, "navigation")
        self.assertEqual(applied, 3)
        self.assertTrue(all(item.accepted for item in registrations))
        self.assertTrue(all(item.method == "multi_floor_frame_consensus" for item in registrations))
        self.assertTrue(all(item.acceptance_level == "approximate" for item in registrations))
        self.assertAlmostEqual(registrations[0].translate_x, -1000.0)

    def test_axis_line_geometry_is_available_when_axis_text_is_missing(self) -> None:
        doc = ezdxf.new("R2013")
        doc.layers.add("C-GRID")
        doc.modelspace().add_line((50.0, 0.0), (50.0, 100.0), dxfattribs={"layer": "C-GRID"})
        sheet = Sheet(
            "grid.dxf", "building", "建筑平面", "F1", "一层平面图",
            0, 0, 50, 50, 0, 0, 100, 100, sheet_id="GRID", floors=["F1"],
        )
        features = _features(doc, [sheet])[sheet.sheet_id]
        self.assertTrue(any(key.startswith("AXISLINE:") and family == "axis" for key, _, _, family in features))

    def test_nested_axis_features_are_resolved_to_world_coordinates(self) -> None:
        doc = ezdxf.new("R2013")
        doc.layers.add("A-GRID")
        inner = doc.blocks.new("内部轴网")
        inner.add_line((0, -20), (0, 20), dxfattribs={"layer": "A-GRID"})
        inner.add_text("1", dxfattribs={"layer": "A-GRID", "insert": (0, 0), "height": 2})
        outer = doc.blocks.new("楼层底图")
        outer.add_blockref("内部轴网", (10, 20))
        wrapper = doc.blocks.new("外部参照容器")
        wrapper.add_blockref(
            "楼层底图", (100, 200),
            dxfattribs={"xscale": 2.0, "yscale": 2.0, "rotation": 90.0},
        )
        # The wrapper itself sits at origin, outside the physical sheet; only
        # its transformed children are inside, matching common xref practice.
        doc.modelspace().add_blockref("外部参照容器", (0, 0))
        sheet = Sheet(
            "nested_grid.dxf", "building", "建筑平面", "F1", "一层平面图",
            0, 0, 100, 200, 0, 100, 200, 300, sheet_id="NESTED_GRID", floors=["F1"],
        )
        features = _features(doc, [sheet])[sheet.sheet_id]
        named = [(x, y) for key, x, y, family in features if key == "AXIS:1" and family == "axis"]
        self.assertEqual(len(named), 1)
        self.assertAlmostEqual(named[0][0], 60.0)
        self.assertAlmostEqual(named[0][1], 220.0)
        self.assertTrue(any(key.startswith("AXISLINE:") for key, _, _, _ in features))

    def test_transform_rank_prefers_many_exact_matches_over_a_few_extra_loose_matches(self) -> None:
        tolerance = 100.0
        exact = (997, 0, 997, 0.0, 0.0, [0.0] * 997, [])
        loose = (1004, 0, 1004, 76.7, 76.7, [76.7] * 1004, [])
        self.assertGreater(_transform_rank(exact, tolerance), _transform_rank(loose, tolerance))

    def test_navigation_policy_accepts_only_small_well_supported_offset(self) -> None:
        self.assertEqual(
            _registration_acceptance_level(1206, 8, 666.5, 666.5, 1422.03, 1.0, "navigation"),
            "approximate",
        )
        self.assertEqual(
            _registration_acceptance_level(1206, 8, 666.5, 666.5, 1422.03, 1.0, "strict"),
            "rejected",
        )
        self.assertEqual(
            _registration_acceptance_level(0, 8, math.inf, math.inf, 1422.03, 1.0, "navigation"),
            "rejected",
        )

    def test_similarity_registration_recovers_scale_rotation_translation(self) -> None:
        source = Sheet(
            "source.dxf", "water", "喷淋", "F5", "五层喷淋平面图",
            0, 0, 0, 0, -100, -100, 100, 100, sheet_id="SRC", floors=["F5"],
        )
        target = Sheet(
            "target.dxf", "building", "建筑平面", "F5", "五层平面图",
            1000, 2000, 1000, 2000, 500, 1000, 2500, 3000, sheet_id="DST", floors=["F5"],
        )
        source_features = [(f"K:{index}", float(index * 100), float((index % 4) * 130), "common") for index in range(12)]
        angle = math.radians(30.0)
        scale, dx, dy = 2.0, 1000.0, 2000.0
        target_features = []
        for key, x, y, family in source_features:
            tx = scale * (math.cos(angle) * x - math.sin(angle) * y) + dx
            ty = scale * (math.sin(angle) * x + math.cos(angle) * y) + dy
            target_features.append((key, tx, ty, family))
        result = register_sheet(source, target, source_features, target_features)
        self.assertTrue(result.accepted)
        self.assertAlmostEqual(result.scale_x, scale, places=5)
        self.assertAlmostEqual(result.rotation_deg, 30.0, places=4)
        self.assertAlmostEqual(result.translate_x, dx, places=4)
        self.assertAlmostEqual(result.translate_y, dy, places=4)

    def test_named_axes_take_priority_over_conflicting_common_geometry(self) -> None:
        source = Sheet(
            "source.dxf", "water", "喷淋", "F2", "二层喷淋平面图",
            0, 0, 0, 0, -100, -100, 100, 100, sheet_id="AXIS_SRC", floors=["F2"],
        )
        target = Sheet(
            "target.dxf", "building", "建筑平面", "F2", "二层建筑平面图",
            1000, 2000, 1000, 2000, 800, 1800, 1200, 2200, sheet_id="AXIS_DST", floors=["F2"],
        )
        source_features = [
            ("AXIS:1", -50.0, -50.0, "axis"),
            ("AXIS:2", 50.0, -50.0, "axis"),
            ("AXIS:A", -50.0, 50.0, "axis"),
        ] + [(f"K:{index}", float(index), float(index * 2), "common") for index in range(20)]
        target_features = [
            ("AXIS:1", 950.0, 1950.0, "axis"),
            ("AXIS:2", 1050.0, 1950.0, "axis"),
            ("AXIS:A", 950.0, 2050.0, "axis"),
        ] + [(f"K:{index}", float(index * 2 + 3000), float(index * 4 - 500), "common") for index in range(20)]
        result = register_sheet(source, target, source_features, target_features)
        self.assertTrue(result.accepted)
        self.assertEqual(result.method, "named_axis_similarity")
        self.assertAlmostEqual(result.scale_x, 1.0)
        self.assertAlmostEqual(result.rotation_deg, 0.0)
        self.assertAlmostEqual(result.translate_x, 1000.0)
        self.assertAlmostEqual(result.translate_y, 2000.0)


class RoomTopologyGuardTests(unittest.TestCase):
    @staticmethod
    def registration() -> Registration:
        return Registration(
            "source.dxf", "暖通平面", "F1", "building.dxf", "named_axis_similarity", True,
            1.0, 1.0, 1010.0, 0.0, 0.0, 0.0, 10, 0, 10, 0.0, 10.0, "test",
            source_sheet_id="S", target_sheet_id="T", fit_tolerance=10.0,
            confidence=0.99, acceptance_level="strict",
        )

    @staticmethod
    def topology(sheet: Sheet, anchor_x: float, wall_x: float) -> SheetRoomTopology:
        walls = [((wall_x, sheet.min_y), (wall_x, sheet.max_y))]
        return SheetRoomTopology(
            sheet, (RoomAnchor("设备间", (anchor_x, sheet.center_y)),), WallIndex.build(walls, 20.0),
        )

    def test_cross_wall_mapping_is_corrected_inside_same_named_room(self) -> None:
        source_sheet = Sheet(
            "source.dxf", "hvac", "暖通平面", "F1", "一层暖通",
            0, 0, 500, 500, 0, 0, 1000, 1000, sheet_id="S", floors=["F1"],
        )
        target_sheet = Sheet(
            "building.dxf", "building", "建筑平面", "F1", "一层建筑",
            0, 0, 1500, 500, 1000, 0, 2000, 1000, sheet_id="T", floors=["F1"],
        )
        result = guard_room_mapping(
            (400.0, 500.0), (1410.0, 500.0),
            self.topology(source_sheet, 390.0, 500.0),
            self.topology(target_sheet, 1390.0, 1405.0),
            self.registration(), source_bbox=[395.0, 495.0, 400.0, 505.0],
        )
        self.assertEqual(result["status"], "adjusted_to_same_room")
        self.assertAlmostEqual(result["adjust_x"], -10.0)
        self.assertTrue(result["wall_crossing_detected"])

    def test_axis_mapping_is_kept_if_room_correction_is_not_reliable(self) -> None:
        source_sheet = Sheet(
            "source.dxf", "hvac", "暖通平面", "F1", "一层暖通",
            0, 0, 500, 500, 0, 0, 1000, 1000, sheet_id="S", floors=["F1"],
        )
        target_sheet = Sheet(
            "building.dxf", "building", "建筑平面", "F1", "一层建筑",
            0, 0, 1500, 500, 1000, 0, 2000, 1000, sheet_id="T", floors=["F1"],
        )
        result = guard_room_mapping(
            (400.0, 500.0), (1410.0, 500.0),
            self.topology(source_sheet, 390.0, 500.0),
            self.topology(target_sheet, 1370.0, 1375.0),
            self.registration(), source_bbox=[390.0, 490.0, 410.0, 510.0],
        )
        self.assertEqual(result["status"], "axis_mapping_kept_room_conflict")
        self.assertEqual(result["adjust_x"], 0.0)
        self.assertEqual(result["adjust_y"], 0.0)

    def test_missing_room_text_uses_architectural_wall_clearance(self) -> None:
        source_sheet = Sheet(
            "source.dxf", "hvac", "暖通平面", "F1", "一层暖通",
            0, 0, 50, 50, 0, 0, 100, 100, sheet_id="S", floors=["F1"],
        )
        target_sheet = Sheet(
            "building.dxf", "building", "建筑平面", "F1", "一层建筑",
            0, 0, 50, 50, 0, 0, 100, 100, sheet_id="T", floors=["F1"],
        )
        source = SheetRoomTopology(source_sheet, tuple(), WallIndex.build([], 10.0))
        target = SheetRoomTopology(
            target_sheet,
            tuple(),
            WallIndex.build([((50.0, 0.0), (50.0, 100.0))] * 4, 10.0),
        )
        registration = self.registration()
        registration.translate_x = 0.0
        result = guard_room_mapping((20.0, 20.0), (20.0, 20.0), source, target, registration)
        self.assertEqual(result["status"], "axis_mapping_kept")

    def test_unlabelled_device_near_wall_keeps_axis_mapping_without_correction(self) -> None:
        source_sheet = Sheet(
            "source.dxf", "hvac", "暖通平面", "F1", "一层暖通",
            0, 0, 50, 50, 0, 0, 100, 100, sheet_id="S", floors=["F1"],
        )
        target_sheet = Sheet(
            "building.dxf", "building", "建筑平面", "F1", "一层建筑",
            0, 0, 50, 50, 0, 0, 100, 100, sheet_id="T", floors=["F1"],
        )
        source = SheetRoomTopology(source_sheet, tuple(), WallIndex.build([], 10.0))
        target = SheetRoomTopology(
            target_sheet,
            tuple(),
            WallIndex.build([((50.0, 0.0), (50.0, 100.0))] * 4, 10.0),
        )
        registration = self.registration()
        registration.translate_x = 0.0
        result = guard_room_mapping((49.5, 20.0), (49.5, 20.0), source, target, registration)
        self.assertEqual(result["status"], "axis_mapping_kept_near_wall")
        self.assertEqual(result["adjust_x"], 0.0)
        self.assertEqual(result["adjust_y"], 0.0)


class CleanRouteOutputTests(unittest.TestCase):
    def test_delivery_dxf_contains_route_without_review_annotations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "fused.dxf"
            output = root / "clean_route.dxf"
            ezdxf.new("R2018").saveas(source)
            visit_plan = {
                "floors": {
                    "F1": {
                        "targets": [{
                            "target_id": "T1",
                            "access_point": [10.0, 10.0],
                            "raw_point": [10.0, 10.0],
                            "object_bbox": [9.0, 9.0, 11.0, 11.0],
                            "visit_order": 1,
                            "is_route_segment_entry": True,
                            "segment_virtual_continuation": True,
                        }]
                    }
                }
            }
            forwarding = {
                "floors": {
                    "F1": {
                        "traversal_events": [{
                            "pass_index": 1,
                            "backend": "refined_physical_graph",
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[0.0, 0.0], [20.0, 0.0]],
                            },
                        }]
                    }
                }
            }
            stage_10_route_outputs._s10_annotated.write_annotated_route_dxf(
                source,
                output,
                visit_plan,
                forwarding,
                include_review_annotations=False,
            )
            doc = ezdxf.readfile(output)
            layers = [str(entity.dxf.get("layer", "")) for entity in doc.modelspace()]
            self.assertTrue(any(layer.startswith("LAST_ROUTE_") for layer in layers))
            self.assertFalse(any(layer == "LAST_TARGET_ORDER" for layer in layers))
            self.assertFalse(any(layer == "LAST_VIRTUAL_CONTINUATION" for layer in layers))
            self.assertFalse(any(layer == "LAST_TARGET_FRAME" for layer in layers))
            self.assertFalse(any(layer == "AI_RECOGNIZED_OBSTACLE" for layer in layers))

    def test_beautified_route_must_not_create_new_raw_cad_crossing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inputs = root / "navigation_graph" / "inputs"
            inputs.mkdir(parents=True)
            area_graph = root / "area_graph"
            area_graph.mkdir()
            (area_graph / "area_graph_summary.json").write_text(
                json.dumps({"pixel_size_by_floor": {"F1": 1.0}}),
                encoding="utf-8",
            )
            (inputs / "free_areas.geojson").write_text(json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "properties": {"floor_id": "F1"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
                    },
                }],
            }), encoding="utf-8")
            barriers = root / "barriers.geojson"
            barriers.write_text(json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "properties": {"floor_id": "F1"},
                    "geometry": {"type": "LineString", "coordinates": [[5, 0], [5, 10]]},
                }],
            }), encoding="utf-8")

            def write_route(path: Path, coordinates: list[list[float]]) -> None:
                path.write_text(json.dumps({
                    "type": "FeatureCollection",
                    "features": [{
                        "type": "Feature",
                        "properties": {"feature_type": "route_edge_traversal", "floor_id": "F1"},
                        "geometry": {"type": "LineString", "coordinates": coordinates},
                    }],
                }), encoding="utf-8")

            original = root / "original.geojson"
            unsafe_display = root / "unsafe.geojson"
            write_route(original, [[4, 1], [6, 1]])
            write_route(unsafe_display, [[4, 5], [6, 5]])
            audit = stage_05B_raw_cad_collision.audit_beautified_route_against_existing_raw_cad(
                root,
                original,
                unsafe_display,
                barriers,
                root / "audit.json",
            )
            self.assertFalse(audit["accepted"])
            self.assertEqual(audit["failure_count"], 1)


class ActualEntityMigrationTests(unittest.TestCase):
    def test_shape_reference_detects_empty_sheet_corner_inside_bounding_rectangle(self) -> None:
        import json
        from shapely.geometry import Point
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            obstacle_dir = root / "02b_building_obstacles"
            obstacle_dir.mkdir()
            payload = {"type": "FeatureCollection", "features": [{
                "type": "Feature", "properties": {"floor": "ROOF"},
                "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [100, 0], [100, 100], [0, 0]]]},
            }]}
            (obstacle_dir / "building_obstacles.geojson").write_text(json.dumps(payload), encoding="utf-8")
            hull = _load_floor_occupancy_hulls(root / "05_migration")["ROOF"]
            self.assertTrue(hull.covers(Point(75, 25)))
            self.assertFalse(hull.covers(Point(10, 90)))

    def test_fan_migration_keeps_local_symbol_and_excludes_remote_auxiliary(self) -> None:
        from ezdxf import bbox
        source = ezdxf.new("R2013")
        block = source.blocks.new("arbitrary_equipment")
        for a, b in [((0, 0), (10, 0)), ((10, 0), (10, 10)),
                     ((10, 10), (0, 10)), ((0, 10), (0, 0))]:
            block.add_line(a, b)
        motor = source.blocks.new("arbitrary_motor")
        motor.add_circle((0, 0), 2)
        block.add_blockref(motor.name, (5, 5))
        distant = source.blocks.new("auxiliary")
        distant.add_line((0, 0), (10, 0))
        block.add_blockref(distant.name, (5000, 0))
        ref = source.modelspace().add_blockref(block.name, (100, 200),
            dxfattribs={"layer": "original_equipment", "xscale": 2, "yscale": 2})
        trimmed, removed = _local_hvac_symbol_copy(source, ref)
        self.assertEqual(removed, 1)
        self.assertEqual(ref.dxf.name, "arbitrary_equipment")
        self.assertEqual(len(block), 6)
        bounds = bbox.extents([trimmed], fast=True)
        self.assertAlmostEqual(bounds.size.x, 20)
        self.assertAlmostEqual(bounds.size.y, 20)
        self.assertEqual(trimmed.dxf.layer, "original_equipment")
        source.layers.add("original_equipment", color=3)
        target = ezdxf.new("R2013")
        target.appids.add("DUOTUZHI_INSPECTION")
        importer = Importer(source, target)
        item = Detection("H-F1-1", "source.dxf", "hvac", "暖通平面", "F1", "排烟风机",
                         100, 200, "INSERT", str(ref.dxf.handle), "original_equipment",
                         ref.dxf.name, "", 0.94, "test")
        imported = _import_entity(importer, target.modelspace(), trimmed,
                                  Matrix44.translate(1000, 2000, 0), item)
        importer.finalize()
        bounds = bbox.extents([imported], fast=True)
        self.assertAlmostEqual(bounds.extmin.x, 1100)
        self.assertAlmostEqual(bounds.extmin.y, 2200)
        self.assertAlmostEqual(bounds.size.x, 20)
        self.assertEqual(imported.dxf.layer, "original_equipment")

    def test_hvac_architectural_occupancy_is_diagnostic_only(self) -> None:
        envelopes = {"F3": (100.0, 100.0, 300.0, 200.0)}
        self.assertTrue(_inside_floor_occupancy("F3", 95.0, 150.0, envelopes))
        self.assertFalse(_inside_floor_occupancy("F3", 200.0, 260.0, envelopes))
        status, inside_envelope, inside_hull = _hvac_spatial_reference_status(
            "F3", 200.0, 260.0, envelopes, {},
        )
        self.assertEqual(status, "outside_architecture_reference_migrated")
        self.assertFalse(inside_envelope)
        self.assertIsNone(inside_hull)
        # Missing architectural obstacle evidence keeps the established sheet
        # frame fallback instead of rejecting every object.
        self.assertTrue(_inside_floor_occupancy("ROOF", 999.0, 999.0, envelopes))

    def test_import_preserves_original_layer_without_migration_box(self) -> None:
        source = ezdxf.new("R2013")
        source.layers.add("任意项目_喷头层", color=5)
        entity = source.modelspace().add_circle((10.0, 20.0), 3.0, dxfattribs={"layer": "任意项目_喷头层"})
        target = ezdxf.new("R2013")
        target.appids.add("DUOTUZHI_INSPECTION")
        importer = Importer(source, target)
        item = Detection(
            "W-F5-00001", "source.dxf", "water", "喷淋", "F5", "喷头",
            10.0, 20.0, "CIRCLE", str(entity.dxf.handle), "任意项目_喷头层", "", "", 0.99, "test",
        )
        imported = _import_entity(
            importer, target.modelspace(), entity,
            Matrix44.chain(Matrix44.scale(2.0), Matrix44.z_rotate(math.radians(90)), Matrix44.translate(100, 200, 0)),
            item,
        )
        importer.finalize()
        self.assertIsNotNone(imported)
        self.assertEqual(imported.dxf.layer, "任意项目_喷头层")
        self.assertAlmostEqual(imported.dxf.center.x, 60.0)
        self.assertAlmostEqual(imported.dxf.center.y, 220.0)
        self.assertFalse(any(layer.dxf.name.startswith("多图纸_迁移_") for layer in target.layers))

    def test_direct_open_view_points_to_building_sheet(self) -> None:
        doc = ezdxf.new("R2018")
        sheet = Sheet(
            "building.dxf", "building", "建筑平面", "F5", "五层平面图",
            0, 0, 6_500_000, 200_000, 6_300_000, 100_000, 6_700_000, 300_000,
            sheet_id="TARGET", floors=["F5"],
        )
        result = _prepare_direct_open_view(doc, {sheet.sheet_id: sheet})
        active = doc.viewports.get("*Active")[0]
        self.assertAlmostEqual(active.dxf.center.x, 6_500_000)
        self.assertAlmostEqual(active.dxf.center.y, 200_000)
        self.assertGreater(active.dxf.height, 200_000)
        self.assertEqual(doc.header["$TILEMODE"], 1)
        self.assertEqual(result["audit_errors"], 0)


if __name__ == "__main__":
    unittest.main()
