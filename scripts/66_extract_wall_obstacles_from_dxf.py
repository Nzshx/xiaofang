# -*- coding: utf-8 -*-
"""
66_extract_wall_obstacles_from_dxf.py

功能：
根据 route_layer_config.json 中的 WALL_OBSTACLE 图层，从 DXF 中提取墙体/柱/建筑轮廓障碍。

输入：
1. D:/untitled/only_B1_B2_garage.dxf
2. D:/untitled/outputs_route_layer_scan/route_layer_config.json
3. D:/untitled/outputs_safety_exit_fused/safety_exit_final.csv

输出：
D:/untitled/outputs_wall_obstacles/
├── wall_obstacle_segments_all.csv
├── wall_obstacle_segments_all.geojson
├── wall_obstacle_segments_B1.geojson
├── wall_obstacle_segments_B2.geojson
├── obstacle_union_B1.geojson
├── obstacle_union_B2.geojson
├── floor_bbox.json
├── wall_obstacle_summary.json
└── wall_obstacles_preview.dxf

说明：
1. 本脚本只提取墙体/柱/建筑轮廓障碍，不做路径规划。
2. 后续 67_build_navigation_graph_from_obstacles.py 会基于这些障碍构建导航图。
3. 路径是否穿墙，后续通过 LineString 与 obstacle_union 相交判断。
"""

import os
import csv
import json
import math
from pathlib import Path
from collections import defaultdict, Counter

import ezdxf

try:
    from shapely.geometry import (
        LineString,
        Polygon,
        Point,
        box,
        mapping,
        shape,
        GeometryCollection,
    )
    from shapely.ops import unary_union
except Exception as e:
    raise ImportError(
        "缺少 shapely。请先安装：pip install shapely"
    ) from e

# =========================================================
# 1. 路径配置
# =========================================================
DXF_PATH = r"D:\untitled\only_B1_B2_garage.dxf"
CONFIG_PATH = r"D:\untitled\outputs_route_layer_scan\route_layer_config.json"
INVENTORY_CSV = r"D:\untitled\outputs_route_layer_scan\route_layer_inventory.csv"
SAFETY_EXIT_CSV = r"D:\untitled\outputs_safety_exit_fused\safety_exit_final.csv"
AUTO_FLOOR_REGIONS_JSON = r"D:\untitled\outputs_auto_floor_regions\floor_regions.json"
AUTO_FLOOR_BBOX_JSON = r"D:\untitled\outputs_auto_floor_regions\floor_bbox.json"
MANUAL_BBOX_JSON = r"D:\untitled\outputs_manual_floor_range\manual_floor_bbox.json"
OUT_DIR = Path(r"D:\untitled\outputs_wall_obstacles")
OUT_DIR.mkdir(parents=True, exist_ok=True)

OUT_SEGMENTS_CSV = OUT_DIR / "wall_obstacle_segments_all.csv"
OUT_SEGMENTS_CSV_FALLBACK = OUT_DIR / "wall_obstacle_segments_all_updated.csv"
OUT_SEGMENTS_GEOJSON = OUT_DIR / "wall_obstacle_segments_all.geojson"
OUT_FLOOR_BBOX_JSON = OUT_DIR / "floor_bbox.json"
OUT_SUMMARY_JSON = OUT_DIR / "wall_obstacle_summary.json"
OUT_PREVIEW_DXF = OUT_DIR / "wall_obstacles_preview.dxf"
OUT_PREVIEW_DXF_FALLBACK = OUT_DIR / "wall_obstacles_preview_updated.dxf"


def load_manual_floor_bbox():
    if not os.path.exists(MANUAL_BBOX_JSON):
        return {}

    with open(MANUAL_BBOX_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)

    return data.get("manual_floor_bbox", {})


def load_auto_floor_bbox():
    """
    Prefer automatically detected floor regions from 65b.
    Supported formats:
    - floor_bbox.json: {"B1": [xmin, ymin, xmax, ymax], ...}
    - floor_regions.json: {"floor_bbox": {...}} or
      {"floors": {"B1": {"bbox": [...]}}}
    """
    if os.path.exists(AUTO_FLOOR_BBOX_JSON):
        with open(AUTO_FLOOR_BBOX_JSON, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {
                str(k): v for k, v in data.items()
                if isinstance(v, list) and len(v) == 4
            }

    if os.path.exists(AUTO_FLOOR_REGIONS_JSON):
        with open(AUTO_FLOOR_REGIONS_JSON, "r", encoding="utf-8-sig") as f:
            data = json.load(f)

        if isinstance(data.get("floor_bbox"), dict):
            return {
                str(k): v for k, v in data["floor_bbox"].items()
                if isinstance(v, list) and len(v) == 4
            }

        floors = data.get("floors", {})
        if isinstance(floors, dict):
            out = {}
            for floor, info in floors.items():
                if not isinstance(info, dict):
                    continue
                bbox_values = info.get("bbox")
                if isinstance(bbox_values, list) and len(bbox_values) == 4:
                    out[str(floor)] = bbox_values
            return out

    return {}


# =========================================================
# 2. 楼层范围配置
# =========================================================
# 如果你已经知道完整楼层范围，可以在这里手动填入：
# 格式：[xmin, ymin, xmax, ymax]
MANUAL_FLOOR_BBOX = {
    "B1": None,
    "B2": None,
}

# 如果未手动设置，则根据每层安全出口点自动外扩得到楼层范围
AUTO_BBOX_MARGIN = 60000.0

# =========================================================
# 3. 障碍几何配置
# =========================================================
# 墙线 buffer 宽度，CAD 单位。
# 后续路径边只要与 buffer 后的墙体相交，就认为穿墙。
WALL_LINE_BUFFER = 300.0

# 闭合多段线是否作为面障碍
CLOSED_POLYLINE_AS_POLYGON = True

# 闭合 polygon 额外 buffer。通常保持 0。
POLYGON_EXTRA_BUFFER = 0.0

# PUB_HATCH 是通用填充层，不能整层作为墙体障碍。
# 这里只补充提取“狭长、接近墙厚”的 HATCH 面，避免误吞地面/区域填充。
ENABLE_SUPPLEMENT_PUB_HATCH_WALLS = True
SUPPLEMENT_WALL_HATCH_LAYERS = {"PUB_HATCH"}
SUPPLEMENT_HATCH_MIN_AREA = 10000.0
SUPPLEMENT_HATCH_MAX_AREA = 8000000.0
SUPPLEMENT_HATCH_MIN_LONG_SIDE = 800.0
SUPPLEMENT_HATCH_MAX_THICKNESS = 1200.0
SUPPLEMENT_HATCH_MIN_ASPECT_RATIO = 2.0

# 是否展开 INSERT 块内图元。
# 如果墙体/柱子被做成块，后续可以改 True。
# 第一版建议 False，避免块定义内部不可见图元带来误障碍。
EXPAND_INSERTS = True

# 递归展开嵌套 BLOCK / INSERT。块内图元位于 0 图层时继承父 INSERT 图层。
FLATTEN_BLOCKS_RECURSIVE = True
MAX_BLOCK_RECURSION_DEPTH = 8
BLOCK_LAYER_ZERO_INHERITS_PARENT = True

# 一些块内墙/柱图层未出现在 route_layer_config.json 中，但语义明确。
ENABLE_BLOCK_WALL_LAYER_ALIASES = True
BLOCK_WALL_LAYER_ALIASES = {
    "A-WALL",
    "A-WALL-BLOK",
    "AC-留洞-砼墙",
    "COLU",
    "COLUMN",
    "HS-A-人防-战时墙",
    "HS-A-抗暴挡墙",
    "SHEARWALL",
    "WALL",
}

# 圆、弧离散精度
ARC_SEGMENTS = 24
CIRCLE_SEGMENTS = 48

# 极短线段过滤
MIN_SEGMENT_LENGTH = 10.0

# 输出 preview dxf 是否绘制 buffer 后障碍边界
DRAW_BUFFER_POLYGON_PREVIEW = True

# =========================================================
# 4. DXF preview 图层
# =========================================================
PREVIEW_SEG_LAYER = "CHECK_WALL_OBSTACLE_SEGMENTS"
PREVIEW_POLY_LAYER = "CHECK_WALL_OBSTACLE_POLYGONS"
PREVIEW_BUFFER_LAYER = "CHECK_WALL_OBSTACLE_BUFFER"
PREVIEW_TEXT_LAYER = "CHECK_WALL_OBSTACLE_TEXT"

PREVIEW_SEG_COLOR = 1
PREVIEW_POLY_COLOR = 6
PREVIEW_BUFFER_COLOR = 5
PREVIEW_TEXT_COLOR = 7


# =========================================================
# 5. 基础工具
# =========================================================
def parse_float(v):
    if v is None:
        return None

    s = str(v).strip()
    if s == "" or s.lower() in {"nan", "none", "null"}:
        return None

    try:
        return float(s)
    except Exception:
        return None


def safe_layer(entity):
    try:
        return entity.dxf.layer
    except Exception:
        return ""


def safe_handle(entity):
    try:
        return entity.dxf.handle
    except Exception:
        return ""


def safe_type(entity):
    try:
        return entity.dxftype()
    except Exception:
        return ""


def safe_color(entity):
    try:
        return entity.dxf.color
    except Exception:
        return ""


def safe_lineweight(entity):
    try:
        return entity.dxf.lineweight
    except Exception:
        return ""


def safe_linetype(entity):
    try:
        return entity.dxf.linetype
    except Exception:
        return ""


def safe_insert_name(entity):
    try:
        return entity.dxf.name
    except Exception:
        return ""


def effective_block_layer(child_layer, parent_layer):
    child_layer = str(child_layer or "").strip()
    parent_layer = str(parent_layer or "").strip()
    if BLOCK_LAYER_ZERO_INHERITS_PARENT and child_layer in {"", "0"} and parent_layer:
        return parent_layer
    return child_layer


def is_config_wall_layer(layer, wall_layers):
    return str(layer or "").strip() in wall_layers


def is_block_wall_alias_layer(layer):
    if not ENABLE_BLOCK_WALL_LAYER_ALIASES:
        return False
    return str(layer or "").strip() in BLOCK_WALL_LAYER_ALIASES


def is_wall_obstacle_layer(layer, wall_layers, from_block=False):
    layer = str(layer or "").strip()
    if is_config_wall_layer(layer, wall_layers):
        return True
    if from_block and is_block_wall_alias_layer(layer):
        return True
    return False


def load_layer_roles():
    roles = {}
    if not os.path.exists(INVENTORY_CSV):
        return roles

    with open(INVENTORY_CSV, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            layer = str(row.get("layer", "")).strip()
            role = str(row.get("suggested_role", "")).strip()
            if layer:
                roles[layer] = role

    return roles


def load_route_layer_config(path):
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    with open(path, "r", encoding="utf-8") as f:
        config = json.load(f)

    wall_layers = set(config.get("WALL_OBSTACLE", []))
    layer_roles = load_layer_roles()
    boundary_layers = {
        layer for layer in wall_layers
        if layer_roles.get(layer) == "POSSIBLE_BOUNDARY"
    }
    if boundary_layers:
        print("[过滤] 以下图层用于楼层边界，不作为 WALL_OBSTACLE 障碍:")
        for layer in sorted(boundary_layers):
            print(f"  - {layer}")
        wall_layers -= boundary_layers

    if not wall_layers:
        raise RuntimeError("route_layer_config.json 中 WALL_OBSTACLE 为空。")

    return config, wall_layers


def load_safety_exits(csv_path):
    if not os.path.exists(csv_path):
        raise FileNotFoundError(csv_path)

    rows = []

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        for r in reader:
            x = parse_float(r.get("cad_x"))
            y = parse_float(r.get("cad_y"))

            if x is None:
                x = parse_float(r.get("dedup_center_x"))
            if y is None:
                y = parse_float(r.get("dedup_center_y"))

            if x is None or y is None:
                continue

            floor = str(r.get("floor", "unknown")).strip()
            if not floor:
                floor = "unknown"

            object_id = (
                    str(r.get("final_id", "")).strip()
                    or str(r.get("object_id", "")).strip()
                    or f"SE_{len(rows) + 1:04d}"
            )

            rows.append({
                "object_id": object_id,
                "floor": floor,
                "cad_x": x,
                "cad_y": y,
            })

    return rows


def build_floor_bbox_from_safety_exits(safety_exits):
    auto_bbox = load_auto_floor_bbox()
    manual_bbox = load_manual_floor_bbox()
    groups = defaultdict(list)
    for r in safety_exits:
        groups[r["floor"]].append(r)
    floor_bbox = {}
    for floor, items in groups.items():
        if floor.lower() in {"unknown", "nan", ""}:
            continue
        if floor in auto_bbox:
            floor_bbox[floor] = auto_bbox[floor]
            print(f"[{floor}] 使用自动识别楼层区域: {auto_bbox[floor]}")
            continue
        if floor in manual_bbox:
            floor_bbox[floor] = manual_bbox[floor]
            print(f"[{floor}] 使用人工框选楼层范围: {manual_bbox[floor]}")
            continue
        if MANUAL_FLOOR_BBOX.get(floor) is not None:
            floor_bbox[floor] = MANUAL_FLOOR_BBOX[floor]
            continue
        xs = [i["cad_x"] for i in items]
        ys = [i["cad_y"] for i in items]
        floor_bbox[floor] = [
            min(xs) - AUTO_BBOX_MARGIN,
            min(ys) - AUTO_BBOX_MARGIN,
            max(xs) + AUTO_BBOX_MARGIN,
            max(ys) + AUTO_BBOX_MARGIN,
        ]
    return floor_bbox


def bbox_polygon(b):
    xmin, ymin, xmax, ymax = b
    return box(xmin, ymin, xmax, ymax)


def geometry_to_feature(geom, props):
    return {
        "type": "Feature",
        "properties": props,
        "geometry": mapping(geom),
    }


def save_geojson(path, features):
    data = {
        "type": "FeatureCollection",
        "features": features,
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def geometry_feature_collection(path, geom, props=None):
    if props is None:
        props = {}

    features = []

    if geom is None or geom.is_empty:
        save_geojson(path, features)
        return

    if geom.geom_type == "GeometryCollection":
        for g in geom.geoms:
            if not g.is_empty:
                features.append(geometry_to_feature(g, props))
    elif geom.geom_type.startswith("Multi"):
        for g in geom.geoms:
            if not g.is_empty:
                features.append(geometry_to_feature(g, props))
    else:
        features.append(geometry_to_feature(geom, props))

    save_geojson(path, features)


# =========================================================
# 6. 几何提取：LINE / POLYLINE / HATCH / CIRCLE / ARC
# =========================================================
def line_length(p1, p2):
    return math.hypot(float(p2[0]) - float(p1[0]), float(p2[1]) - float(p1[1]))


def make_linestring(p1, p2):
    if line_length(p1, p2) < MIN_SEGMENT_LENGTH:
        return None
    return LineString([p1, p2])


def extract_line(entity):
    try:
        s = entity.dxf.start
        e = entity.dxf.end
        p1 = (float(s.x), float(s.y))
        p2 = (float(e.x), float(e.y))
        line = make_linestring(p1, p2)
        return [line] if line else [], []
    except Exception:
        return [], []


def extract_lwpolyline(entity):
    lines = []
    polygons = []

    try:
        pts = []
        for p in entity.get_points():
            pts.append((float(p[0]), float(p[1])))

        if len(pts) < 2:
            return [], []

        for i in range(1, len(pts)):
            line = make_linestring(pts[i - 1], pts[i])
            if line:
                lines.append(line)

        is_closed = False
        try:
            is_closed = bool(entity.closed)
        except Exception:
            is_closed = False

        if is_closed and len(pts) >= 3:
            line = make_linestring(pts[-1], pts[0])
            if line:
                lines.append(line)

            if CLOSED_POLYLINE_AS_POLYGON:
                poly = Polygon(pts)
                if not poly.is_valid:
                    poly = poly.buffer(0)

                if not poly.is_empty and poly.area > 0:
                    polygons.append(poly)

    except Exception:
        pass

    return lines, polygons


def extract_polyline(entity):
    lines = []
    polygons = []

    try:
        pts = []

        for v in entity.vertices:
            loc = v.dxf.location
            pts.append((float(loc.x), float(loc.y)))

        if len(pts) < 2:
            return [], []

        for i in range(1, len(pts)):
            line = make_linestring(pts[i - 1], pts[i])
            if line:
                lines.append(line)

        is_closed = False
        try:
            is_closed = bool(entity.is_closed)
        except Exception:
            is_closed = False

        if is_closed and len(pts) >= 3:
            line = make_linestring(pts[-1], pts[0])
            if line:
                lines.append(line)

            if CLOSED_POLYLINE_AS_POLYGON:
                poly = Polygon(pts)
                if not poly.is_valid:
                    poly = poly.buffer(0)

                if not poly.is_empty and poly.area > 0:
                    polygons.append(poly)

    except Exception:
        pass

    return lines, polygons


def extract_circle(entity):
    try:
        c = entity.dxf.center
        r = float(entity.dxf.radius)
        p = Point(float(c.x), float(c.y)).buffer(r, resolution=CIRCLE_SEGMENTS)
        return [], [p]
    except Exception:
        return [], []


def angle_range(start_angle, end_angle, n):
    """
    DXF 角度单位为 degree。
    """
    s = math.radians(float(start_angle))
    e = math.radians(float(end_angle))

    if e < s:
        e += 2 * math.pi

    return [s + (e - s) * i / max(1, n - 1) for i in range(n)]


def extract_arc(entity):
    lines = []

    try:
        c = entity.dxf.center
        r = float(entity.dxf.radius)
        start_angle = float(entity.dxf.start_angle)
        end_angle = float(entity.dxf.end_angle)

        angles = angle_range(start_angle, end_angle, ARC_SEGMENTS)

        pts = [
            (
                float(c.x) + r * math.cos(a),
                float(c.y) + r * math.sin(a),
            )
            for a in angles
        ]

        for i in range(1, len(pts)):
            line = make_linestring(pts[i - 1], pts[i])
            if line:
                lines.append(line)

    except Exception:
        pass

    return lines, []


def extract_hatch(entity):
    """
    尝试从 HATCH 边界中提取 polygon / line。
    HATCH 边界复杂时可能失败，失败则跳过。
    """
    lines = []
    polygons = []

    try:
        for path in entity.paths:
            # PolylinePath
            if hasattr(path, "vertices"):
                pts = []
                for v in path.vertices:
                    try:
                        x = float(v[0])
                        y = float(v[1])
                    except Exception:
                        continue
                    pts.append((x, y))

                if len(pts) >= 2:
                    for i in range(1, len(pts)):
                        line = make_linestring(pts[i - 1], pts[i])
                        if line:
                            lines.append(line)

                    is_closed = False
                    try:
                        is_closed = bool(path.is_closed)
                    except Exception:
                        is_closed = True

                    if is_closed and len(pts) >= 3:
                        line = make_linestring(pts[-1], pts[0])
                        if line:
                            lines.append(line)

                        poly = Polygon(pts)
                        if not poly.is_valid:
                            poly = poly.buffer(0)

                        if not poly.is_empty and poly.area > 0:
                            polygons.append(poly)

            # EdgePath
            elif hasattr(path, "edges"):
                pts = []

                for edge in path.edges:
                    et = edge.EDGE_TYPE

                    if et == "LineEdge":
                        try:
                            p1 = (float(edge.start[0]), float(edge.start[1]))
                            p2 = (float(edge.end[0]), float(edge.end[1]))
                            line = make_linestring(p1, p2)
                            if line:
                                lines.append(line)
                            pts.append(p1)
                            pts.append(p2)
                        except Exception:
                            pass

                    elif et == "ArcEdge":
                        try:
                            cx, cy = float(edge.center[0]), float(edge.center[1])
                            r = float(edge.radius)
                            angles = angle_range(edge.start_angle, edge.end_angle, ARC_SEGMENTS)
                            arc_pts = [
                                (
                                    cx + r * math.cos(a),
                                    cy + r * math.sin(a),
                                )
                                for a in angles
                            ]
                            for i in range(1, len(arc_pts)):
                                line = make_linestring(arc_pts[i - 1], arc_pts[i])
                                if line:
                                    lines.append(line)
                            pts.extend(arc_pts)
                        except Exception:
                            pass

                # EdgePath 是否构成 polygon 这里不强制构造，避免错误闭合

    except Exception:
        pass

    return lines, polygons


def polygon_parts(geom):
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type.startswith("Multi") or geom.geom_type == "GeometryCollection":
        parts = []
        for g in geom.geoms:
            parts.extend(polygon_parts(g))
        return parts
    return []


def min_rect_dims(poly):
    try:
        rect = poly.minimum_rotated_rectangle
        coords = list(rect.exterior.coords)
        lengths = []
        for i in range(1, len(coords)):
            lengths.append(line_length(coords[i - 1], coords[i]))
        lengths = sorted(v for v in lengths if v > 1e-6)
        if len(lengths) < 2:
            return 0.0, 0.0
        return float(lengths[0]), float(lengths[-1])
    except Exception:
        return 0.0, 0.0


def is_supplement_wall_hatch_polygon(poly):
    if poly is None or poly.is_empty:
        return False

    try:
        if not poly.is_valid:
            poly = poly.buffer(0)
    except Exception:
        return False

    if poly.is_empty:
        return False

    area = float(poly.area)
    if area < SUPPLEMENT_HATCH_MIN_AREA or area > SUPPLEMENT_HATCH_MAX_AREA:
        return False

    short_side, long_side = min_rect_dims(poly)
    if short_side <= 0 or long_side <= 0:
        return False
    if short_side > SUPPLEMENT_HATCH_MAX_THICKNESS:
        return False
    if long_side < SUPPLEMENT_HATCH_MIN_LONG_SIDE:
        return False
    if long_side / max(short_side, 1.0) < SUPPLEMENT_HATCH_MIN_ASPECT_RATIO:
        return False

    return True


def extract_geometry_from_entity(entity):
    etype = safe_type(entity)

    if etype == "LINE":
        return extract_line(entity)

    if etype == "LWPOLYLINE":
        return extract_lwpolyline(entity)

    if etype == "POLYLINE":
        return extract_polyline(entity)

    if etype == "CIRCLE":
        return extract_circle(entity)

    if etype == "ARC":
        return extract_arc(entity)

    if etype == "HATCH":
        return extract_hatch(entity)

    return [], []


# =========================================================
# 7. 主提取流程
# =========================================================
def geom_intersects_floor(geom, floor_poly):
    try:
        return geom.intersects(floor_poly)
    except Exception:
        return False


def clip_geom_to_floor(geom, floor_poly):
    try:
        g = geom.intersection(floor_poly)
        if g.is_empty:
            return None
        return g
    except Exception:
        return None


def iter_insert_wall_entities(insert_entity, wall_layers, parent_layer="", block_stack=None, depth=1):
    if not EXPAND_INSERTS:
        return
    if depth > MAX_BLOCK_RECURSION_DEPTH:
        return

    block_stack = list(block_stack or [])
    insert_handle = safe_handle(insert_entity)
    insert_name = safe_insert_name(insert_entity)
    insert_layer = effective_block_layer(safe_layer(insert_entity), parent_layer)

    if insert_handle or insert_name:
        block_stack.append({
            "handle": insert_handle,
            "name": insert_name,
            "layer": insert_layer,
        })

    block_path = " > ".join(
        f"{item.get('name', '') or '<unnamed>'}#{item.get('handle', '')}"
        for item in block_stack
    )
    parent_handle = block_stack[-1].get("handle", "") if block_stack else ""
    parent_name = block_stack[-1].get("name", "") if block_stack else ""

    try:
        virtual_entities = list(insert_entity.virtual_entities())
    except Exception:
        return

    for ve in virtual_entities:
        raw_layer = safe_layer(ve)
        layer = effective_block_layer(raw_layer, insert_layer)
        etype = safe_type(ve)

        if etype == "INSERT" and FLATTEN_BLOCKS_RECURSIVE:
            yield from iter_insert_wall_entities(
                ve,
                wall_layers=wall_layers,
                parent_layer=layer,
                block_stack=block_stack,
                depth=depth + 1,
            )
            continue

        if is_wall_obstacle_layer(layer, wall_layers, from_block=True):
            yield {
                "entity": ve,
                "layer": layer,
                "parent_insert_handle": parent_handle,
                "parent_insert_type": "INSERT",
                "parent_insert_name": parent_name,
                "block_depth": depth,
                "block_path": block_path,
            }


def iter_wall_entities(doc, wall_layers):
    msp = doc.modelspace()

    for entity in msp:
        layer = safe_layer(entity)
        etype = safe_type(entity)

        if etype == "INSERT":
            if EXPAND_INSERTS:
                yield from iter_insert_wall_entities(
                    entity,
                    wall_layers=wall_layers,
                    parent_layer=layer,
                    block_stack=[],
                    depth=1,
                )
            continue

        if is_wall_obstacle_layer(layer, wall_layers, from_block=False):
            yield {
                "entity": entity,
                "layer": layer,
                "parent_insert_handle": "",
                "parent_insert_type": "",
                "parent_insert_name": "",
                "block_depth": 0,
                "block_path": "",
            }


def extract_wall_obstacles():
    print("=" * 100)
    print("[提取墙体/柱/建筑轮廓障碍]")
    print(f"DXF_PATH: {DXF_PATH}")
    print(f"CONFIG_PATH: {CONFIG_PATH}")
    print(f"SAFETY_EXIT_CSV: {SAFETY_EXIT_CSV}")

    if not os.path.exists(DXF_PATH):
        raise FileNotFoundError(DXF_PATH)

    config, wall_layers = load_route_layer_config(CONFIG_PATH)

    print("=" * 100)
    print("[WALL_OBSTACLE 图层]")
    for layer in sorted(wall_layers):
        print(layer)

    safety_exits = load_safety_exits(SAFETY_EXIT_CSV)
    floor_bbox = build_floor_bbox_from_safety_exits(safety_exits)

    if not floor_bbox:
        raise RuntimeError("未能根据 safety_exit_final.csv 构建楼层范围。")

    floor_polys = {
        floor: bbox_polygon(b)
        for floor, b in floor_bbox.items()
    }

    with open(OUT_FLOOR_BBOX_JSON, "w", encoding="utf-8") as f:
        json.dump(floor_bbox, f, ensure_ascii=False, indent=2)

    print("=" * 100)
    print("[楼层范围]")
    for floor, b in floor_bbox.items():
        print(f"{floor}: {b}")

    doc = ezdxf.readfile(DXF_PATH)

    all_segment_rows = []
    all_segment_features = []
    floor_segment_features = defaultdict(list)
    floor_line_geoms = defaultdict(list)
    floor_polygon_geoms = defaultdict(list)

    layer_counter = Counter()
    etype_counter = Counter()
    floor_counter = Counter()

    entity_count = 0
    line_geom_count = 0
    polygon_geom_count = 0
    supplement_hatch_entity_count = 0
    supplement_hatch_polygon_count = 0
    supplement_hatch_rejected_count = 0
    flattened_block_entity_count = 0
    block_depth_counter = Counter()
    block_layer_alias_counter = Counter()

    print("=" * 100)
    print("[开始扫描 WALL_OBSTACLE 图层实体]")

    for item in iter_wall_entities(doc, wall_layers):
        entity = item["entity"]
        layer = item["layer"]
        parent_handle = item["parent_insert_handle"]
        parent_type = item["parent_insert_type"]
        parent_name = item["parent_insert_name"]
        block_depth = item["block_depth"]
        block_path = item["block_path"]

        entity_count += 1
        if block_depth > 0:
            flattened_block_entity_count += 1
            block_depth_counter[block_depth] += 1
            if is_block_wall_alias_layer(layer) and not is_config_wall_layer(layer, wall_layers):
                block_layer_alias_counter[layer] += 1

        etype = safe_type(entity)
        handle = safe_handle(entity)

        layer_counter[layer] += 1
        etype_counter[etype] += 1

        lines, polygons = extract_geometry_from_entity(entity)

        if not lines and not polygons:
            continue

        for line in lines:
            if line is None or line.is_empty:
                continue

            line_geom_count += 1

            for floor, fpoly in floor_polys.items():
                if not geom_intersects_floor(line, fpoly):
                    continue

                clipped = clip_geom_to_floor(line, fpoly)
                if clipped is None or clipped.is_empty:
                    continue

                props = {
                    "floor": floor,
                    "layer": layer,
                    "handle": handle,
                    "entity_type": etype,
                    "geometry_source": "line",
                    "length": float(line.length),
                    "color": safe_color(entity),
                    "lineweight": safe_lineweight(entity),
                    "linetype": safe_linetype(entity),
                    "parent_insert_handle": parent_handle or "",
                    "parent_insert_type": parent_type or "",
                    "parent_insert_name": parent_name or "",
                    "block_depth": block_depth,
                    "block_path": block_path or "",
                }

                floor_counter[floor] += 1
                floor_line_geoms[floor].append(clipped)

                feat = geometry_to_feature(clipped, props)
                floor_segment_features[floor].append(feat)
                all_segment_features.append(feat)

                coords = list(line.coords)
                row = {
                    "floor": floor,
                    "layer": layer,
                    "handle": handle,
                    "entity_type": etype,
                    "geometry_source": "line",
                    "x1": coords[0][0],
                    "y1": coords[0][1],
                    "x2": coords[-1][0],
                    "y2": coords[-1][1],
                    "length": float(line.length),
                    "color": safe_color(entity),
                    "lineweight": safe_lineweight(entity),
                    "linetype": safe_linetype(entity),
                    "parent_insert_handle": parent_handle or "",
                    "parent_insert_type": parent_type or "",
                    "parent_insert_name": parent_name or "",
                    "block_depth": block_depth,
                    "block_path": block_path or "",
                }
                all_segment_rows.append(row)

        for poly in polygons:
            if poly is None or poly.is_empty:
                continue

            if POLYGON_EXTRA_BUFFER and POLYGON_EXTRA_BUFFER > 0:
                poly = poly.buffer(POLYGON_EXTRA_BUFFER)

            if poly.is_empty:
                continue

            polygon_geom_count += 1

            for floor, fpoly in floor_polys.items():
                if not geom_intersects_floor(poly, fpoly):
                    continue

                clipped = clip_geom_to_floor(poly, fpoly)
                if clipped is None or clipped.is_empty:
                    continue

                props = {
                    "floor": floor,
                    "layer": layer,
                    "handle": handle,
                    "entity_type": etype,
                    "geometry_source": "polygon",
                    "area": float(poly.area),
                    "color": safe_color(entity),
                    "lineweight": safe_lineweight(entity),
                    "linetype": safe_linetype(entity),
                    "parent_insert_handle": parent_handle or "",
                    "parent_insert_type": parent_type or "",
                    "parent_insert_name": parent_name or "",
                    "block_depth": block_depth,
                    "block_path": block_path or "",
                }

                floor_counter[floor] += 1
                floor_polygon_geoms[floor].append(clipped)

                feat = geometry_to_feature(clipped, props)
                floor_segment_features[floor].append(feat)
                all_segment_features.append(feat)

                row = {
                    "floor": floor,
                    "layer": layer,
                    "handle": handle,
                    "entity_type": etype,
                    "geometry_source": "polygon",
                    "x1": "",
                    "y1": "",
                    "x2": "",
                    "y2": "",
                    "length": "",
                    "area": float(poly.area),
                    "color": safe_color(entity),
                    "lineweight": safe_lineweight(entity),
                    "linetype": safe_linetype(entity),
                    "parent_insert_handle": parent_handle or "",
                    "parent_insert_type": parent_type or "",
                    "parent_insert_name": parent_name or "",
                    "block_depth": block_depth,
                    "block_path": block_path or "",
                }
                all_segment_rows.append(row)

    if ENABLE_SUPPLEMENT_PUB_HATCH_WALLS:
        print("=" * 100)
        print("[补充扫描 PUB_HATCH 狭长墙体填充]")

        for entity in doc.modelspace():
            layer = safe_layer(entity)
            etype = safe_type(entity)

            if layer not in SUPPLEMENT_WALL_HATCH_LAYERS:
                continue
            if layer in wall_layers:
                continue
            if etype != "HATCH":
                continue

            supplement_hatch_entity_count += 1
            layer_counter[layer] += 1
            etype_counter[etype] += 1

            _, polygons = extract_hatch(entity)
            if not polygons:
                continue

            handle = safe_handle(entity)

            for poly in polygons:
                if poly is None or poly.is_empty:
                    continue

                if not poly.is_valid:
                    try:
                        poly = poly.buffer(0)
                    except Exception:
                        continue

                for floor, fpoly in floor_polys.items():
                    if not geom_intersects_floor(poly, fpoly):
                        continue

                    clipped = clip_geom_to_floor(poly, fpoly)
                    if clipped is None or clipped.is_empty:
                        continue

                    for part in polygon_parts(clipped):
                        if not is_supplement_wall_hatch_polygon(part):
                            supplement_hatch_rejected_count += 1
                            continue

                        supplement_hatch_polygon_count += 1
                        polygon_geom_count += 1

                        props = {
                            "floor": floor,
                            "layer": layer,
                            "handle": handle,
                            "entity_type": etype,
                            "geometry_source": "supplement_pub_hatch_wall_polygon",
                            "area": float(part.area),
                            "color": safe_color(entity),
                            "lineweight": safe_lineweight(entity),
                            "linetype": safe_linetype(entity),
                            "parent_insert_handle": "",
                            "parent_insert_type": "",
                        }

                        floor_counter[floor] += 1
                        floor_polygon_geoms[floor].append(part)

                        feat = geometry_to_feature(part, props)
                        floor_segment_features[floor].append(feat)
                        all_segment_features.append(feat)

                        row = {
                            "floor": floor,
                            "layer": layer,
                            "handle": handle,
                            "entity_type": etype,
                            "geometry_source": "supplement_pub_hatch_wall_polygon",
                            "x1": "",
                            "y1": "",
                            "x2": "",
                            "y2": "",
                            "length": "",
                            "area": float(part.area),
                            "color": safe_color(entity),
                            "lineweight": safe_lineweight(entity),
                            "linetype": safe_linetype(entity),
                            "parent_insert_handle": "",
                            "parent_insert_type": "",
                        }
                        all_segment_rows.append(row)

    # =====================================================
    # 8. 生成每层 obstacle_union
    # =====================================================
    floor_obstacle_union = {}

    for floor in floor_polys.keys():
        line_buffers = []

        for line in floor_line_geoms[floor]:
            try:
                buf = line.buffer(WALL_LINE_BUFFER, cap_style=2, join_style=2)
                if not buf.is_empty:
                    line_buffers.append(buf)
            except Exception:
                pass

        poly_geoms = []
        for poly in floor_polygon_geoms[floor]:
            try:
                if not poly.is_empty:
                    poly_geoms.append(poly)
            except Exception:
                pass

        geoms = line_buffers + poly_geoms

        if geoms:
            union = unary_union(geoms)
            if not union.is_valid:
                union = union.buffer(0)
        else:
            union = GeometryCollection()

        floor_obstacle_union[floor] = union

    # =====================================================
    # 9. 写文件
    # =====================================================
    fields = [
        "floor",
        "layer",
        "handle",
        "entity_type",
        "geometry_source",
        "x1",
        "y1",
        "x2",
        "y2",
        "length",
        "area",
        "color",
        "lineweight",
        "linetype",
        "parent_insert_handle",
        "parent_insert_type",
        "parent_insert_name",
        "block_depth",
        "block_path",
    ]

    actual_segments_csv = OUT_SEGMENTS_CSV
    try:
        f = open(actual_segments_csv, "w", newline="", encoding="utf-8-sig")
    except PermissionError:
        actual_segments_csv = OUT_SEGMENTS_CSV_FALLBACK
        f = open(actual_segments_csv, "w", newline="", encoding="utf-8-sig")

    with f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()

        for row in all_segment_rows:
            writer.writerow({k: row.get(k, "") for k in fields})

    save_geojson(OUT_SEGMENTS_GEOJSON, all_segment_features)

    for floor, features in floor_segment_features.items():
        save_geojson(
            OUT_DIR / f"wall_obstacle_segments_{floor}.geojson",
            features,
        )

    for floor, union in floor_obstacle_union.items():
        geometry_feature_collection(
            OUT_DIR / f"obstacle_union_{floor}.geojson",
            union,
            props={
                "floor": floor,
                "wall_line_buffer": WALL_LINE_BUFFER,
                "source": "WALL_OBSTACLE layers",
            },
        )

    # =====================================================
    # 10. 生成预览 DXF
    # =====================================================
    actual_preview_dxf = write_preview_dxf(
        dxf_path=DXF_PATH,
        floor_line_geoms=floor_line_geoms,
        floor_polygon_geoms=floor_polygon_geoms,
        floor_obstacle_union=floor_obstacle_union,
        out_path=OUT_PREVIEW_DXF,
    )

    summary = {
        "dxf_path": DXF_PATH,
        "config_path": CONFIG_PATH,
        "wall_layers": sorted(wall_layers),
        "floor_bbox": floor_bbox,
        "wall_line_buffer": WALL_LINE_BUFFER,
        "block_flattening": {
            "expand_inserts": EXPAND_INSERTS,
            "recursive": FLATTEN_BLOCKS_RECURSIVE,
            "max_depth": MAX_BLOCK_RECURSION_DEPTH,
            "layer_zero_inherits_parent": BLOCK_LAYER_ZERO_INHERITS_PARENT,
            "flattened_wall_entity_count": flattened_block_entity_count,
            "block_depth_counter": dict(block_depth_counter),
            "enable_block_wall_layer_aliases": ENABLE_BLOCK_WALL_LAYER_ALIASES,
            "block_wall_layer_aliases": sorted(BLOCK_WALL_LAYER_ALIASES),
            "block_layer_alias_counter": dict(block_layer_alias_counter),
        },
        "scanned_wall_entities": entity_count,
        "line_geometry_count": line_geom_count,
        "polygon_geometry_count": polygon_geom_count,
        "supplement_pub_hatch_wall": {
            "enabled": ENABLE_SUPPLEMENT_PUB_HATCH_WALLS,
            "layers": sorted(SUPPLEMENT_WALL_HATCH_LAYERS),
            "entity_count": supplement_hatch_entity_count,
            "accepted_polygon_count": supplement_hatch_polygon_count,
            "rejected_polygon_count": supplement_hatch_rejected_count,
            "min_area": SUPPLEMENT_HATCH_MIN_AREA,
            "max_area": SUPPLEMENT_HATCH_MAX_AREA,
            "max_thickness": SUPPLEMENT_HATCH_MAX_THICKNESS,
            "min_long_side": SUPPLEMENT_HATCH_MIN_LONG_SIDE,
            "min_aspect_ratio": SUPPLEMENT_HATCH_MIN_ASPECT_RATIO,
        },
        "exported_segment_rows": len(all_segment_rows),
        "layer_counter": dict(layer_counter),
        "entity_type_counter": dict(etype_counter),
        "floor_counter": dict(floor_counter),
        "outputs": {
            "segments_csv": str(actual_segments_csv),
            "segments_geojson": str(OUT_SEGMENTS_GEOJSON),
            "floor_bbox_json": str(OUT_FLOOR_BBOX_JSON),
            "preview_dxf": str(actual_preview_dxf),
            "output_dir": str(OUT_DIR),
        }
    }

    with open(OUT_SUMMARY_JSON, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # =====================================================
    # 11. 控制台输出
    # =====================================================
    print("=" * 100)
    print("[提取完成]")
    print(f"扫描 WALL_OBSTACLE 实体数: {entity_count}")
    print(f"线几何数量: {line_geom_count}")
    print(f"面几何数量: {polygon_geom_count}")
    print(f"导出记录数: {len(all_segment_rows)}")

    print("-" * 100)
    print("[按楼层统计]")
    for floor, count in floor_counter.items():
        print(f"{floor}: {count}")

    print("-" * 100)
    print("[按图层 Top 30]")
    for layer, count in layer_counter.most_common(30):
        print(f"{layer}: {count}")

    print("-" * 100)
    print("[按图元类型]")
    for etype, count in etype_counter.items():
        print(f"{etype}: {count}")

    print("=" * 100)
    print("[输出文件]")
    print(f"墙体障碍 CSV: {OUT_SEGMENTS_CSV}")
    print(f"全部墙体障碍 GeoJSON: {OUT_SEGMENTS_GEOJSON}")
    for floor in floor_polys.keys():
        print(f"{floor} 线/面障碍: {OUT_DIR / f'wall_obstacle_segments_{floor}.geojson'}")
        print(f"{floor} union障碍: {OUT_DIR / f'obstacle_union_{floor}.geojson'}")
    print(f"楼层范围: {OUT_FLOOR_BBOX_JSON}")
    print(f"预览 DXF: {OUT_PREVIEW_DXF}")
    print(f"统计 JSON: {OUT_SUMMARY_JSON}")
    print("=" * 100)

    print("[下一步]")
    print("1. 打开 wall_obstacles_preview.dxf，检查提取出的墙体/柱/建筑轮廓是否正确。")
    print("2. 如果误提取了非墙线，回到 route_layer_config.json 从 WALL_OBSTACLE 中移除对应图层。")
    print("3. 如果漏墙，回到 route_layer_config.json 把对应图层加入 WALL_OBSTACLE。")
    print("4. 确认障碍正确后，再写 67_build_navigation_graph_from_obstacles.py 构建导航图。")


# =========================================================
# 12. 预览 DXF
# =========================================================
def ensure_layer(doc, name, color):
    if name not in doc.layers:
        doc.layers.new(name=name, dxfattribs={"color": color})


def add_linestring_to_dxf(msp, geom, layer, color):
    if geom.is_empty:
        return

    if geom.geom_type == "LineString":
        coords = list(geom.coords)
        if len(coords) >= 2:
            msp.add_lwpolyline(
                [(x, y) for x, y in coords],
                dxfattribs={
                    "layer": layer,
                    "color": color,
                    "lineweight": 50,
                },
            )

    elif geom.geom_type == "MultiLineString":
        for g in geom.geoms:
            add_linestring_to_dxf(msp, g, layer, color)

    elif geom.geom_type == "GeometryCollection":
        for g in geom.geoms:
            add_linestring_to_dxf(msp, g, layer, color)


def add_polygon_to_dxf(msp, geom, layer, color):
    if geom.is_empty:
        return

    if geom.geom_type == "Polygon":
        coords = list(geom.exterior.coords)
        if len(coords) >= 3:
            msp.add_lwpolyline(
                [(x, y) for x, y in coords],
                close=True,
                dxfattribs={
                    "layer": layer,
                    "color": color,
                    "lineweight": 30,
                },
            )

    elif geom.geom_type == "MultiPolygon":
        for g in geom.geoms:
            add_polygon_to_dxf(msp, g, layer, color)

    elif geom.geom_type == "GeometryCollection":
        for g in geom.geoms:
            add_polygon_to_dxf(msp, g, layer, color)


def write_preview_dxf(
        dxf_path,
        floor_line_geoms,
        floor_polygon_geoms,
        floor_obstacle_union,
        out_path,
):
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    ensure_layer(doc, PREVIEW_SEG_LAYER, PREVIEW_SEG_COLOR)
    ensure_layer(doc, PREVIEW_POLY_LAYER, PREVIEW_POLY_COLOR)
    ensure_layer(doc, PREVIEW_BUFFER_LAYER, PREVIEW_BUFFER_COLOR)
    ensure_layer(doc, PREVIEW_TEXT_LAYER, PREVIEW_TEXT_COLOR)

    for floor, lines in floor_line_geoms.items():
        for line in lines:
            add_linestring_to_dxf(
                msp,
                line,
                PREVIEW_SEG_LAYER,
                PREVIEW_SEG_COLOR,
            )

    for floor, polys in floor_polygon_geoms.items():
        for poly in polys:
            add_polygon_to_dxf(
                msp,
                poly,
                PREVIEW_POLY_LAYER,
                PREVIEW_POLY_COLOR,
            )

    if DRAW_BUFFER_POLYGON_PREVIEW:
        for floor, union in floor_obstacle_union.items():
            add_polygon_to_dxf(
                msp,
                union,
                PREVIEW_BUFFER_LAYER,
                PREVIEW_BUFFER_COLOR,
            )

    try:
        doc.saveas(str(out_path))
        return out_path
    except PermissionError:
        doc.saveas(str(OUT_PREVIEW_DXF_FALLBACK))
        return OUT_PREVIEW_DXF_FALLBACK


# =========================================================
# 13. 入口
# =========================================================
if __name__ == "__main__":
    extract_wall_obstacles()
