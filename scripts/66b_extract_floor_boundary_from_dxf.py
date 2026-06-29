# -*- coding: utf-8 -*-
"""
66b_extract_floor_boundary_from_dxf.py

功能：
从 DXF 中提取每层楼层可规划边界 FLOOR_BOUNDARY。

当前规则：
1. 默认从 HS-A-建筑轮廓 图层提取建筑轮廓；
2. 支持闭合 LWPOLYLINE / POLYLINE 直接转 Polygon；
3. 支持 LINE / ARC / 非闭合多段线 polygonize 成 Polygon；
4. 每层根据 safety_exit_final.csv 中的安全出口位置选择最合适的边界 polygon；
5. 输出 floor_boundary_B1.geojson / floor_boundary_B2.geojson；
6. 输出 floor_boundary_preview.dxf 供人工检查。

为什么需要本脚本：
- floor_bbox 只是矩形范围，会导致导航图生成到建筑外面；
- floor_boundary 是真实建筑轮廓，用来限制导航节点和导航边只能在建筑内部生成。
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
        Point,
        LineString,
        Polygon,
        MultiPolygon,
        GeometryCollection,
        box,
        mapping,
    )
    from shapely.ops import unary_union, polygonize
except Exception as e:
    raise ImportError("缺少 shapely，请先安装：pip install shapely") from e

# =========================================================
# 1. 路径配置
# =========================================================
DXF_PATH = r"D:\untitled\only_B1_B2_garage.dxf"
SAFETY_EXIT_CSV = r"D:\untitled\outputs_safety_exit_fused\safety_exit_final.csv"
AUTO_FLOOR_BBOX_JSON = r"D:\untitled\outputs_auto_floor_regions\floor_bbox.json"
MANUAL_BBOX_JSON = r"D:\untitled\outputs_manual_floor_range\manual_floor_bbox.json"
# 66 脚本已经生成的楼层 bbox，优先读取
FLOOR_BBOX_JSON = r"D:\untitled\outputs_wall_obstacles\floor_bbox.json"
OUT_DIR = Path(r"D:\untitled\outputs_wall_obstacles")
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_ALL_BOUNDARY_GEOJSON = OUT_DIR / "floor_boundary_all_candidates.geojson"
OUT_BOUNDARY_SUMMARY_JSON = OUT_DIR / "floor_boundary_summary.json"
OUT_PREVIEW_DXF = OUT_DIR / "floor_boundary_preview.dxf"


def load_manual_floor_bbox():
    if not os.path.exists(MANUAL_BBOX_JSON):
        return {}

    with open(MANUAL_BBOX_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)

    return data.get("manual_floor_bbox", {})


# =========================================================
# 2. 边界图层配置
# =========================================================
# 你已经确认 HS-A-建筑轮廓 是墙体 / 建筑外轮廓
FLOOR_BOUNDARY_LAYERS = {
    "HS-A-建筑轮廓",
}

# 如果某些图纸还有其他建筑轮廓图层，后续可以加进来：
# FLOOR_BOUNDARY_LAYERS.add("xxx")


# =========================================================
# 3. 楼层范围配置
# =========================================================
# 如果你知道精确 B1/B2 范围，可手动填写
# 格式：[xmin, ymin, xmax, ymax]
MANUAL_FLOOR_BBOX = {
    "B1": None,
    "B2": None,
}

# 如果没有手动范围，则根据 safety_exit_final.csv 自动外扩
AUTO_BBOX_MARGIN = 60000.0

# =========================================================
# 4. 几何参数
# =========================================================
# polygon 面积过小的候选边界直接剔除
MIN_BOUNDARY_POLYGON_AREA = 1_000_000.0

# 判断安全出口是否属于 polygon 时允许的容差
# 因为安全出口文字点可能贴近边界或略偏外
SAFETY_EXIT_CONTAIN_TOLERANCE = 5000.0

# 输出边界是否轻微收缩。
# 一般保持 0。如果后续导航点贴在轮廓线上，可以设 -300 或 -500。
BOUNDARY_OUTPUT_BUFFER = 0.0

# ARC / CIRCLE 离散精度
ARC_SEGMENTS = 32
CIRCLE_SEGMENTS = 64

# 极短线过滤
MIN_SEGMENT_LENGTH = 10.0

# =========================================================
# 5. DXF 预览样式
# =========================================================
BOUNDARY_LAYER_PREFIX = "CHECK_FLOOR_BOUNDARY_"
CANDIDATE_LAYER = "CHECK_FLOOR_BOUNDARY_CANDIDATE"
TEXT_LAYER = "CHECK_FLOOR_BOUNDARY_TEXT"

BOUNDARY_COLOR = 1  # 红色
CANDIDATE_COLOR = 8  # 灰色
TEXT_COLOR = 7

TEXT_HEIGHT = 1000.0


# =========================================================
# 6. 基础工具
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


def line_len(p1, p2):
    return math.hypot(float(p2[0]) - float(p1[0]), float(p2[1]) - float(p1[1]))


def make_line(p1, p2):
    if line_len(p1, p2) < MIN_SEGMENT_LENGTH:
        return None
    return LineString([p1, p2])


def clean_polygon(poly):
    if poly is None or poly.is_empty:
        return None

    try:
        if not poly.is_valid:
            poly = poly.buffer(0)
    except Exception:
        return None

    if poly.is_empty:
        return None

    if poly.geom_type == "Polygon":
        if poly.area >= MIN_BOUNDARY_POLYGON_AREA:
            return poly
        return None

    if poly.geom_type == "MultiPolygon":
        polys = [p for p in poly.geoms if p.area >= MIN_BOUNDARY_POLYGON_AREA]
        if not polys:
            return None
        return unary_union(polys)

    return None


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


def save_single_geometry_geojson(path, geom, props=None):
    if props is None:
        props = {}

    features = []

    if geom is not None and not geom.is_empty:
        if geom.geom_type == "Polygon":
            features.append(geometry_to_feature(geom, props))

        elif geom.geom_type == "MultiPolygon":
            for g in geom.geoms:
                features.append(geometry_to_feature(g, props))

        elif geom.geom_type == "GeometryCollection":
            for g in geom.geoms:
                if g.geom_type in {"Polygon", "MultiPolygon"} and not g.is_empty:
                    features.append(geometry_to_feature(g, props))

    save_geojson(path, features)


# =========================================================
# 7. 读取安全出口与楼层 bbox
# =========================================================
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
                "x": float(x),
                "y": float(y),
            })

    return rows


def group_by_floor(items):
    g = defaultdict(list)
    for item in items:
        g[item["floor"]].append(item)
    return dict(g)


def load_or_build_floor_bbox(safety_exits):
    out = {}

    manual_bbox = load_manual_floor_bbox()

    for floor, b in manual_bbox.items():
        out[floor] = b
        print(f"[{floor}] 66b 使用人工框选楼层范围: {b}")

    # 2. 读取 66 生成的 floor_bbox.json
    if os.path.exists(FLOOR_BBOX_JSON):
        with open(FLOOR_BBOX_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)

        for floor, b in data.items():
            out[floor] = b
            print(f"[{floor}] 66b 使用 66 输出楼层范围覆盖旧范围: {b}")

    if os.path.exists(AUTO_FLOOR_BBOX_JSON):
        with open(AUTO_FLOOR_BBOX_JSON, "r", encoding="utf-8-sig") as f:
            data = json.load(f)

        for floor, b in data.items():
            if floor not in out:
                out[floor] = b
                print(f"[{floor}] 66b 使用自动识别楼层区域: {b}")

    # 3. 如果仍然没有，则从安全出口自动生成
    safety_by_floor = group_by_floor(safety_exits)

    for floor, items in safety_by_floor.items():
        if floor in out:
            continue

        if floor.lower() in {"unknown", "nan", ""}:
            continue

        xs = [i["x"] for i in items]
        ys = [i["y"] for i in items]

        out[floor] = [
            min(xs) - AUTO_BBOX_MARGIN,
            min(ys) - AUTO_BBOX_MARGIN,
            max(xs) + AUTO_BBOX_MARGIN,
            max(ys) + AUTO_BBOX_MARGIN,
        ]

    return out


# =========================================================
# 8. DXF 几何提取
# =========================================================
def angle_range(start_angle, end_angle, n):
    s = math.radians(float(start_angle))
    e = math.radians(float(end_angle))

    if e < s:
        e += 2 * math.pi

    return [s + (e - s) * i / max(1, n - 1) for i in range(n)]


def extract_line(entity):
    lines = []

    try:
        s = entity.dxf.start
        e = entity.dxf.end

        p1 = (float(s.x), float(s.y))
        p2 = (float(e.x), float(e.y))

        line = make_line(p1, p2)
        if line:
            lines.append(line)

    except Exception:
        pass

    return lines, []


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
            line = make_line(pts[i - 1], pts[i])
            if line:
                lines.append(line)

        is_closed = False
        try:
            is_closed = bool(entity.closed)
        except Exception:
            is_closed = False

        if is_closed and len(pts) >= 3:
            line = make_line(pts[-1], pts[0])
            if line:
                lines.append(line)

            poly = clean_polygon(Polygon(pts))
            if poly is not None:
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
            line = make_line(pts[i - 1], pts[i])
            if line:
                lines.append(line)

        is_closed = False
        try:
            is_closed = bool(entity.is_closed)
        except Exception:
            is_closed = False

        if is_closed and len(pts) >= 3:
            line = make_line(pts[-1], pts[0])
            if line:
                lines.append(line)

            poly = clean_polygon(Polygon(pts))
            if poly is not None:
                polygons.append(poly)

    except Exception:
        pass

    return lines, polygons


def extract_circle(entity):
    try:
        c = entity.dxf.center
        r = float(entity.dxf.radius)

        poly = Point(float(c.x), float(c.y)).buffer(r, resolution=CIRCLE_SEGMENTS)
        poly = clean_polygon(poly)

        if poly is not None:
            return [], [poly]

    except Exception:
        pass

    return [], []


def extract_arc(entity):
    lines = []

    try:
        c = entity.dxf.center
        r = float(entity.dxf.radius)

        angles = angle_range(
            float(entity.dxf.start_angle),
            float(entity.dxf.end_angle),
            ARC_SEGMENTS,
        )

        pts = [
            (
                float(c.x) + r * math.cos(a),
                float(c.y) + r * math.sin(a),
            )
            for a in angles
        ]

        for i in range(1, len(pts)):
            line = make_line(pts[i - 1], pts[i])
            if line:
                lines.append(line)

    except Exception:
        pass

    return lines, []


def extract_hatch(entity):
    """
    尝试从 HATCH 边界提取 polygon。
    建筑轮廓通常不是 HATCH，但这里保留兼容。
    """
    lines = []
    polygons = []

    try:
        for path in entity.paths:
            if hasattr(path, "vertices"):
                pts = []

                for v in path.vertices:
                    try:
                        pts.append((float(v[0]), float(v[1])))
                    except Exception:
                        pass

                if len(pts) >= 2:
                    for i in range(1, len(pts)):
                        line = make_line(pts[i - 1], pts[i])
                        if line:
                            lines.append(line)

                    is_closed = True
                    try:
                        is_closed = bool(path.is_closed)
                    except Exception:
                        pass

                    if is_closed and len(pts) >= 3:
                        line = make_line(pts[-1], pts[0])
                        if line:
                            lines.append(line)

                        poly = clean_polygon(Polygon(pts))
                        if poly is not None:
                            polygons.append(poly)

    except Exception:
        pass

    return lines, polygons


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


def extract_boundary_candidates(doc):
    msp = doc.modelspace()

    all_lines = []
    direct_polygons = []
    source_counter = Counter()
    layer_counter = Counter()
    entity_counter = Counter()

    for entity in msp:
        layer = safe_layer(entity)

        if layer not in FLOOR_BOUNDARY_LAYERS:
            continue

        etype = safe_type(entity)
        handle = safe_handle(entity)

        lines, polygons = extract_geometry_from_entity(entity)

        if not lines and not polygons:
            continue

        layer_counter[layer] += 1
        entity_counter[etype] += 1

        for line in lines:
            if line is not None and not line.is_empty:
                all_lines.append({
                    "geom": line,
                    "layer": layer,
                    "handle": handle,
                    "entity_type": etype,
                    "source": "linework",
                })
                source_counter["line"] += 1

        for poly in polygons:
            if poly is not None and not poly.is_empty:
                direct_polygons.append({
                    "geom": poly,
                    "layer": layer,
                    "handle": handle,
                    "entity_type": etype,
                    "source": "closed_entity",
                })
                source_counter["closed_polygon"] += 1

    # 非闭合线段 polygonize
    polygonized = []

    try:
        line_geoms = [x["geom"] for x in all_lines]

        if line_geoms:
            merged_lines = unary_union(line_geoms)
            polys = list(polygonize(merged_lines))

            for idx, p in enumerate(polys):
                p = clean_polygon(p)
                if p is not None:
                    polygonized.append({
                        "geom": p,
                        "layer": "polygonized_boundary_lines",
                        "handle": f"polygonized_{idx}",
                        "entity_type": "POLYGONIZED",
                        "source": "polygonize",
                    })
                    source_counter["polygonize"] += 1

    except Exception as e:
        print(f"[警告] polygonize 失败: {e}")

    candidates = direct_polygons + polygonized

    return candidates, all_lines, {
        "source_counter": dict(source_counter),
        "layer_counter": dict(layer_counter),
        "entity_counter": dict(entity_counter),
    }


# =========================================================
# 9. 每层选择边界 polygon
# =========================================================
def count_safety_exits_in_polygon(poly, safety_items):
    if poly is None or poly.is_empty:
        return 0

    count = 0
    poly_check = poly.buffer(SAFETY_EXIT_CONTAIN_TOLERANCE)

    for se in safety_items:
        p = Point(se["x"], se["y"])
        if poly_check.contains(p) or poly_check.touches(p):
            count += 1

    return count


def select_boundary_for_floor(floor, floor_bbox, safety_items, candidates):
    """
    根据楼层 bbox 和安全出口位置，从候选 polygon 中选择该层边界。
    选择逻辑：
    1. 与楼层 bbox 有交集；
    2. 优先包含安全出口数量最多；
    3. 若安全出口数量相同，优先 intersection area 最大；
    4. 若仍相同，优先 polygon 面积最大。
    """
    floor_poly = box(*floor_bbox)

    scored = []

    for idx, cand in enumerate(candidates):
        poly = cand["geom"]

        if poly is None or poly.is_empty:
            continue

        try:
            if not poly.intersects(floor_poly):
                continue

            inter_area = poly.intersection(floor_poly).area
            if inter_area <= 0:
                continue

            se_count = count_safety_exits_in_polygon(poly, safety_items)

            score = (
                se_count,
                inter_area,
                poly.area,
            )

            scored.append({
                "idx": idx,
                "candidate": cand,
                "score": score,
                "safety_exit_count": se_count,
                "intersection_area": inter_area,
                "area": poly.area,
            })

        except Exception:
            continue

    if not scored:
        # 兜底：如果无法提取边界，使用 bbox。
        # 注意这只是兜底，不建议作为最终方案。
        print(f"[警告] {floor} 未找到合适建筑轮廓 polygon，临时使用 bbox 作为 boundary。")
        return floor_poly, {
            "source": "fallback_bbox",
            "safety_exit_count": 0,
            "intersection_area": floor_poly.area,
            "area": floor_poly.area,
            "candidate_idx": -1,
        }

    scored.sort(
        key=lambda r: (
            r["score"][0],
            r["score"][1],
            r["score"][2],
        ),
        reverse=True,
    )

    best = scored[0]
    boundary = best["candidate"]["geom"]

    if BOUNDARY_OUTPUT_BUFFER != 0:
        try:
            boundary = boundary.buffer(BOUNDARY_OUTPUT_BUFFER)
            boundary = clean_polygon(boundary)
        except Exception:
            pass

    return boundary, {
        "source": best["candidate"]["source"],
        "layer": best["candidate"]["layer"],
        "handle": best["candidate"]["handle"],
        "entity_type": best["candidate"]["entity_type"],
        "safety_exit_count": best["safety_exit_count"],
        "intersection_area": best["intersection_area"],
        "area": best["area"],
        "candidate_idx": best["idx"],
    }


# =========================================================
# 10. DXF 预览
# =========================================================
def ensure_layer(doc, name, color):
    if name not in doc.layers:
        doc.layers.new(name=name, dxfattribs={"color": color})


def add_polygon_to_dxf(msp, geom, layer, color, lineweight=80):
    if geom is None or geom.is_empty:
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
                    "lineweight": lineweight,
                },
            )

    elif geom.geom_type == "MultiPolygon":
        for g in geom.geoms:
            add_polygon_to_dxf(msp, g, layer, color, lineweight)

    elif geom.geom_type == "GeometryCollection":
        for g in geom.geoms:
            add_polygon_to_dxf(msp, g, layer, color, lineweight)


def write_preview_dxf(selected_boundaries, candidate_polygons):
    doc = ezdxf.readfile(DXF_PATH)
    msp = doc.modelspace()

    ensure_layer(doc, CANDIDATE_LAYER, CANDIDATE_COLOR)
    ensure_layer(doc, TEXT_LAYER, TEXT_COLOR)

    # 候选边界灰色预览，只画部分，避免太乱
    for idx, cand in enumerate(candidate_polygons[:300]):
        add_polygon_to_dxf(
            msp,
            cand["geom"],
            CANDIDATE_LAYER,
            CANDIDATE_COLOR,
            lineweight=15,
        )

    for floor, data in selected_boundaries.items():
        layer = f"{BOUNDARY_LAYER_PREFIX}{floor}"
        ensure_layer(doc, layer, BOUNDARY_COLOR)

        geom = data["geometry"]
        add_polygon_to_dxf(
            msp,
            geom,
            layer,
            BOUNDARY_COLOR,
            lineweight=100,
        )

        try:
            c = geom.centroid
            label = (
                f"{floor} FLOOR_BOUNDARY "
                f"source={data['meta'].get('source')} "
                f"SE={data['meta'].get('safety_exit_count')}"
            )

            msp.add_text(
                label,
                dxfattribs={
                    "layer": TEXT_LAYER,
                    "color": TEXT_COLOR,
                    "height": TEXT_HEIGHT,
                    "insert": (float(c.x), float(c.y)),
                },
            )
        except Exception:
            pass

    doc.saveas(str(OUT_PREVIEW_DXF))


# =========================================================
# 11. 主程序
# =========================================================
def main():
    print("=" * 100)
    print("[提取楼层 FLOOR_BOUNDARY]")
    print(f"DXF_PATH: {DXF_PATH}")
    print(f"FLOOR_BOUNDARY_LAYERS: {sorted(FLOOR_BOUNDARY_LAYERS)}")

    if not os.path.exists(DXF_PATH):
        raise FileNotFoundError(DXF_PATH)

    safety_exits = load_safety_exits(SAFETY_EXIT_CSV)
    safety_by_floor = group_by_floor(safety_exits)
    floor_bbox = load_or_build_floor_bbox(safety_exits)

    print("=" * 100)
    print("[安全出口统计]")
    for floor, items in safety_by_floor.items():
        print(f"{floor}: {len(items)}")

    print("=" * 100)
    print("[楼层 bbox]")
    for floor, b in floor_bbox.items():
        print(f"{floor}: {b}")

    doc = ezdxf.readfile(DXF_PATH)

    candidates, line_items, scan_stats = extract_boundary_candidates(doc)

    print("=" * 100)
    print("[候选边界统计]")
    print(f"linework 数量: {len(line_items)}")
    print(f"candidate polygon 数量: {len(candidates)}")
    print(f"source_counter: {scan_stats['source_counter']}")
    print(f"layer_counter: {scan_stats['layer_counter']}")
    print(f"entity_counter: {scan_stats['entity_counter']}")

    if not candidates:
        print("[警告] 未提取到任何建筑轮廓 polygon。后续会使用 bbox 兜底，但导航图仍可能覆盖到外部。")

    # 保存全部候选 polygon
    all_candidate_features = []

    for idx, cand in enumerate(candidates):
        all_candidate_features.append(
            geometry_to_feature(
                cand["geom"],
                {
                    "candidate_idx": idx,
                    "source": cand["source"],
                    "layer": cand["layer"],
                    "handle": cand["handle"],
                    "entity_type": cand["entity_type"],
                    "area": float(cand["geom"].area),
                },
            )
        )

    save_geojson(OUT_ALL_BOUNDARY_GEOJSON, all_candidate_features)

    selected_boundaries = {}
    summary = {
        "dxf_path": DXF_PATH,
        "floor_boundary_layers": sorted(FLOOR_BOUNDARY_LAYERS),
        "candidate_polygon_count": len(candidates),
        "scan_stats": scan_stats,
        "floors": {},
        "outputs": {},
    }

    for floor, b in floor_bbox.items():
        if floor.lower() in {"unknown", "nan", ""}:
            continue

        safety_items = safety_by_floor.get(floor, [])

        boundary, meta = select_boundary_for_floor(
            floor=floor,
            floor_bbox=b,
            safety_items=safety_items,
            candidates=candidates,
        )

        selected_boundaries[floor] = {
            "geometry": boundary,
            "meta": meta,
        }

        out_geojson = OUT_DIR / f"floor_boundary_{floor}.geojson"

        save_single_geometry_geojson(
            out_geojson,
            boundary,
            props={
                "floor": floor,
                **meta,
            },
        )

        summary["floors"][floor] = {
            "floor_bbox": b,
            "safety_exit_count": len(safety_items),
            "selected_boundary": meta,
            "boundary_area": float(boundary.area) if boundary is not None and not boundary.is_empty else 0,
            "output_geojson": str(out_geojson),
        }

        print("-" * 100)
        print(f"[{floor}] 选中边界")
        print(f"source: {meta.get('source')}")
        print(f"layer: {meta.get('layer')}")
        print(f"handle: {meta.get('handle')}")
        print(f"safety_exit_count_inside: {meta.get('safety_exit_count')}")
        print(f"area: {meta.get('area')}")
        print(f"输出: {out_geojson}")

    write_preview_dxf(selected_boundaries, candidates)

    summary["outputs"] = {
        "all_boundary_candidates": str(OUT_ALL_BOUNDARY_GEOJSON),
        "preview_dxf": str(OUT_PREVIEW_DXF),
        "summary_json": str(OUT_BOUNDARY_SUMMARY_JSON),
    }

    with open(OUT_BOUNDARY_SUMMARY_JSON, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("=" * 100)
    print("[输出文件]")
    print(f"全部候选边界 GeoJSON: {OUT_ALL_BOUNDARY_GEOJSON}")

    for floor in selected_boundaries:
        print(f"{floor} 边界 GeoJSON: {OUT_DIR / f'floor_boundary_{floor}.geojson'}")

    print(f"边界预览 DXF: {OUT_PREVIEW_DXF}")
    print(f"汇总 JSON: {OUT_BOUNDARY_SUMMARY_JSON}")
    print("=" * 100)

    print("[检查方法]")
    print("1. 打开 floor_boundary_preview.dxf。")
    print("2. 查看 CHECK_FLOOR_BOUNDARY_B1 / CHECK_FLOOR_BOUNDARY_B2。")
    print("3. 红色边界应包住对应楼层的真实建筑范围。")
    print("4. 如果边界选错，检查 floor_boundary_summary.json 中 selected_boundary。")
    print("5. 如果生成了 fallback_bbox，说明 HS-A-建筑轮廓 没能形成闭合 polygon，需要进一步用 polygonize 或手动边界。")
    print("6. 下一步修改 67_build_navigation_graph_from_obstacles.py，使其读取 floor_boundary_B1/B2.geojson 替代矩形 bbox。")


if __name__ == "__main__":
    main()
