# -*- coding: utf-8 -*-
"""
DXF 图幅与楼层识别脚本。
功能边界：
1. 输入 cad_vector_inventory_agent.py 生成的 cad_object_inventory.csv，或直接输入 DXF 后自动生成 inventory；
2. 识别图纸中的图幅 / sheet；
3. 根据标题栏、图名文本、楼层关键字推断 floor_id / floor_name；
4. 判断图幅是否适合作为消防巡检路径规划输入；
5. 推断图幅内部可用于巡检对象识别的 inspection_region；
6. 输出 drawing_sheets_floors.json 和 drawing_sheets_floors.csv。
"""
from __future__ import annotations
import argparse
import csv
import json
import math
import re
import subprocess
import sys
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import ezdxf
except Exception:
    ezdxf = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
JOBS_ROOT = PROJECT_ROOT / "web" / "runtime" / "jobs"

SHEET_BOX_LAYER = "SHEET_DETECT_BOX"
SHEET_LABEL_LAYER = "SHEET_DETECT_LABEL"
SHEET_NON_PATH_LAYER = "SHEET_DETECT_NON_PATH"
INSPECTION_REGION_LAYER = "INSPECTION_REGION_BOX"
INSPECTION_REGION_LABEL_LAYER = "INSPECTION_REGION_LABEL"

BBox = tuple[float, float, float, float]

DEFAULT_INPUT_DXF = PROJECT_ROOT / "data" / "raw" / "test.dxf"
DEFAULT_LOCAL_INVENTORY_DIR = PROJECT_ROOT / "outputs" / "test_single_floor" / "inventory"
DEFAULT_LOCAL_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "test_single_floor" / "sheet_floor_review"

AXIS_DIMENSION_TERMS = [
    "轴",
    "轴线",
    "轴网",
    "轴号",
    "定位轴",
    "尺寸",
    "总尺寸",
    "标注",
    "DIM",
    "DIMS",
    "DIMENSION",
    "AXIS",
    "GRID",
    "A-AXIS",
    "ADIM",
]
AXIS_LABEL_CONTEXT_TERMS = [
    "轴",
    "轴文",
    "轴号",
    "AXIS",
    "AXIS_TEXT",
    "_AXISO",
]
NON_AXIS_LABEL_PREFIXES = {
    "DN",
    "DE",
    "EL",
    "FL",
    "WL",
    "HDZ",
    "XF",
    "FH",
    "FM",
    "JS",
    "PS",
    "PY",
    "KT",
    "EQ",
}
INSPECTION_REGION_EXCLUDE_TERMS = [
    "图框",
    "图幅",
    "图签",
    "标题栏",
    "图例",
    "说明",
    "目录",
    "材料表",
    "设备表",
    "剖面",
    "断面",
    "详图",
    "节点",
    "大样",
    "系统图",
    "原理图",
    "示意图",
    "SHEET",
    "FRAME",
    "BORDER",
    "TITLE",
    "TK_LABEL",
    "PMSHEET",
    "LEGEND",
    "NOTE",
    "TABLE",
]

AXIS_DIMENSION_STRICT_TERMS = [
    "\u8f74",
    "\u8f74\u7ebf",
    "\u8f74\u7f51",
    "\u8f74\u53f7",
    "\u8f74\u6587",
    "\u5b9a\u4f4d\u8f74",
    "\u5c3a\u5bf8",
    "\u603b\u5c3a\u5bf8",
    "\u6807\u6ce8",
    "\u5e73\u65f6\u6807\u6ce8",
    "AXIS",
    "AXIS_TEXT",
    "GRID",
    "DIM",
    "DIMS",
    "DIMENSION",
    "DIM_SYMB",
    "DIM_ELEV",
    "DIM_IDEN",
    "PUB_DIM",
]

INSPECTION_REGION_STRICT_EXCLUDE_TERMS = [
    "\u56fe\u6846",
    "\u56fe\u5e45",
    "\u56fe\u7b7e",
    "\u6807\u9898\u680f",
    "\u56fe\u4f8b",
    "\u8bf4\u660e",
    "\u76ee\u5f55",
    "\u6750\u6599\u8868",
    "\u8bbe\u5907\u8868",
    "\u5256\u9762",
    "\u65ad\u9762",
    "\u8be6\u56fe",
    "\u8282\u70b9",
    "\u5927\u6837",
    "\u7cfb\u7edf\u56fe",
    "\u539f\u7406\u56fe",
    "\u793a\u610f\u56fe",
    "SHEET",
    "FRAME",
    "BORDER",
    "TITLE",
    "TK_LABEL",
    "PMSHEET",
    "LEGEND",
    "NOTE",
    "TABLE",
]

# 图框 / 标题栏相关关键词：用于识别 sheet 外边界和标题栏证据。
FRAME_LAYER_TERMS = [
    "图框",
    "图幅",
    "标题栏",
    "图签",
    "BORDER",
    "FRAME",
    "SHEET",
    "TITLE",
    "TK_LABEL",
    "PMSHEET",
]

FRAME_NEGATIVE_LAYER_TERMS = [
    "墙",
    "柱",
    "门",
    "窗",
    "填充",
    "家具",
    "设备",
    "洁具",
    "楼梯",
    "栏杆",
    "车位",
    "道路",
    "红线",
    "防火分区",
    "HATCH",
    "WALL",
    "COLU",
    "DOOR",
    "STAIR",
    "CAR",
]

NON_PLAN_TEXT_TERMS = [
    "详图",
    "大样",
    "图例",
    "说明",
    "目录",
    "材料表",
    "设备表",
    "指标表",
    "剖面",
    "断面",
    "节点",
]

FLOOR_CONTEXT_TERMS = [
    "平面",
    "平面图",
    "防火分区",
    "消防",
    "车库",
    "建筑",
    "地库",
]

# 平面图标题关键词：用于从图名中提取楼层。
PLAN_TITLE_TERMS = [
    "平面图",
    "平面",
    "防火分区图",
    "消防平面",
    "地下室平面",
    "车库平面",
    "建筑平面",
]

TITLEBAR_META_TERMS = [
    "PUB_TITLE",
    "TK_LABEL",
    "PMSHEET",
    "TITLE",
    "图签",
    "标题栏",
    "图名",
    "DRAWING TITLE",
]

# 局部详图标题关键词：这些标题虽然可能含“平面图”，但不能代表整张楼层平面。
LOCAL_DETAIL_PLAN_TITLE_TERMS = [
    "详图",
    "节点",
    "大样",
    "局部",
    "检修口",
    "接头井",
    "风井",
    "排风井",
    "补风井",
    "楼梯",
    "坡道",
    "口部",
    "剖面",
    "断面",
    "立面",
]

TITLE_NEGATIVE_PHRASES = [
    "详见平面图",
    "见平面图",
    "参见平面图",
    "详平面图",
]

# 非路径规划图标题关键词：用于排除剖面、详图、节点、图例、系统图等。
NON_PLAN_TITLE_TERMS = [
    "剖面图",
    "剖面",
    "刨面图",
    "刨面",
    "断面图",
    "断面",
    "立面图",
    "立面",
    "详图",
    "节点图",
    "节点",
    "大样图",
    "大样",
    "图例",
    "系统图",
    "原理图",
    "示意图",
]

CN_NUMBERS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
# 输出 CSV 字段：同时也是 JSON sheets 中主要业务字段的扁平化版本。
OUTPUT_FIELDS = [
    "sheet_id",
    "floor_id",
    "floor_name",
    "sheet_title",
    "floor_source",
    "floor_confidence",
    "sheet_semantic_role",
    "sheet_semantic_name",
    "sheet_semantic_confidence",
    "path_planning_usable",
    "sheet_role",
    "needs_floor_review",
    "method",
    "sheet_confidence",
    "object_count",
    "text_count",
    "seed_object_id",
    "seed_text",
    "seed_floor_id",
    "seed_floor_name",
    "seed_floor_evidence",
    "seed_floor_confidence",
    "growth_rounds",
    "growth_object_count",
    "growth_stop_reason",
    "excluded_non_plan",
    "non_plan_reason",
    "dedup_floor_group_size",
    "dedup_selected_reason",
    "bbox_minx",
    "bbox_miny",
    "bbox_maxx",
    "bbox_maxy",
    "inspection_region_source",
    "inspection_region_confidence",
    "inspection_region_object_count",
    "inspection_region_minx",
    "inspection_region_miny",
    "inspection_region_maxx",
    "inspection_region_maxy",
    "inspection_region_evidence",
    "inspection_regions_json",
    "evidence",
    "semantic_evidence",
]


def safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except Exception:
        return None
    return number if math.isfinite(number) else None


def as_int(value: Any, default: int = 0) -> int:
    """将输入安全转换为整数；失败时返回 default。"""
    try:
        return int(float(value))
    except Exception:
        return default


def latest_job_dir() -> Path:
    """在 web/runtime/jobs 下寻找最近一次包含 inventory 的任务目录。

    注意：本函数只服务 Web job 模式。PyCharm 本地直接运行时，如果没有
    web/runtime/jobs 目录，不应依赖该函数，而应传入 --dxf 或 --inventory-dir。
    """
    if not JOBS_ROOT.exists():
        raise FileNotFoundError(
            f"No jobs directory found: {JOBS_ROOT}. "
            "请使用 --dxf 输入 DXF，或使用 --inventory-dir 指向已有 cad_object_inventory.csv。"
        )
    jobs = [path for path in JOBS_ROOT.iterdir() if path.is_dir() and (path / "inventory").exists()]
    if not jobs:
        raise FileNotFoundError(
            f"No jobs found under {JOBS_ROOT}. "
            "请使用 --dxf 输入 DXF，或使用 --inventory-dir 指向已有 cad_object_inventory.csv。"
        )
    return max(jobs, key=lambda item: item.stat().st_mtime)


def bbox_from_row(row: dict[str, str]) -> tuple[float, float, float, float] | None:
    """从 inventory CSV 行读取 bbox_minx/bbox_miny/bbox_maxx/bbox_maxy，并校验有效性。"""
    minx = safe_float(row.get("bbox_minx"))
    miny = safe_float(row.get("bbox_miny"))
    maxx = safe_float(row.get("bbox_maxx"))
    maxy = safe_float(row.get("bbox_maxy"))
    if None in (minx, miny, maxx, maxy) or maxx <= minx or maxy <= miny:
        return None
    return minx, miny, maxx, maxy


def bbox_area(bbox: tuple[float, float, float, float]) -> float:
    """计算 bbox 面积。"""
    minx, miny, maxx, maxy = bbox
    return max(0.0, maxx - minx) * max(0.0, maxy - miny)


def bbox_center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    """计算 bbox 中心点坐标。"""
    minx, miny, maxx, maxy = bbox
    return (minx + maxx) / 2.0, (miny + maxy) / 2.0


def bbox_union(boxes: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    """合并多个 bbox，返回能覆盖所有输入框的最小外包矩形。"""
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def bbox_contains_point(bbox: tuple[float, float, float, float], point: tuple[float, float]) -> bool:
    """判断点是否位于 bbox 内部或边界上。"""
    minx, miny, maxx, maxy = bbox
    x, y = point
    return minx <= x <= maxx and miny <= y <= maxy


def bbox_intersects(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    """判断两个 bbox 是否相交或相切。"""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    return ax1 <= bx2 and ax2 >= bx1 and ay1 <= by2 and ay2 >= by1


def bbox_inflate(
        bbox: tuple[float, float, float, float],
        dx: float,
        dy: float | None = None,
) -> tuple[float, float, float, float]:
    """按给定距离向外扩张 bbox；dy 为空时使用 dx。"""
    if dy is None:
        dy = dx
    minx, miny, maxx, maxy = bbox
    return minx - dx, miny - dy, maxx + dx, maxy + dy


def bbox_width(bbox: tuple[float, float, float, float]) -> float:
    """返回 bbox 宽度。"""
    return max(0.0, bbox[2] - bbox[0])


def bbox_height(bbox: tuple[float, float, float, float]) -> float:
    """返回 bbox 高度。"""
    return max(0.0, bbox[3] - bbox[1])


def bbox_point_distance(
        bbox: tuple[float, float, float, float],
        point: tuple[float, float],
) -> float:
    """计算点到 bbox 的最短欧氏距离；点在框内时距离为 0。"""
    minx, miny, maxx, maxy = bbox
    x, y = point
    dx = max(minx - x, 0.0, x - maxx)
    dy = max(miny - y, 0.0, y - maxy)
    return math.hypot(dx, dy)


def bbox_iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """计算两个 bbox 的交并比，用于图幅候选去重。"""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    intersection = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = bbox_area(a) + bbox_area(b) - intersection
    return intersection / union if union > 0 else 0.0


def bbox_intersection_area(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """计算两个 bbox 的交集面积。"""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    return max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)


def bbox_containment(inner: tuple[float, float, float, float], outer: tuple[float, float, float, float]) -> float:
    """计算 inner 被 outer 覆盖的面积比例。"""
    area = bbox_area(inner)
    if area <= 0:
        return 0.0
    return bbox_intersection_area(inner, outer) / area


def text_has_any(value: Any, terms: list[str]) -> bool:
    """大小写不敏感地判断文本是否包含任一关键词。"""
    text = str(value or "").upper()
    return any(str(term).upper() in text for term in terms)


def compact_text(value: Any) -> str:
    """去除文本首尾空白并压缩内部空白，便于楼层和标题正则匹配。"""
    return re.sub(r"\s+", "", str(value or "").strip())


def normalize_axis_label_text(value: Any) -> str:
    """Normalize short CAD axis labels without relying on drawing color."""
    text = compact_text(value).upper()
    text = (
        text.replace("－", "-")
            .replace("–", "-")
            .replace("—", "-")
            .replace("_", "-")
            .replace(" ", "")
    )
    return text.strip("/\\|:：,，;；。.,()（）[]【】")


def bbox_clip(
        bbox: tuple[float, float, float, float],
        outer: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    """将 bbox 裁剪到 outer 范围内；无交集时返回 None。"""
    minx = max(bbox[0], outer[0])
    miny = max(bbox[1], outer[1])
    maxx = min(bbox[2], outer[2])
    maxy = min(bbox[3], outer[3])
    if maxx <= minx or maxy <= miny:
        return None
    return minx, miny, maxx, maxy


def item_semantic_text(item: dict[str, Any]) -> str:
    """拼接图层、块名、文本、实体类型等字段，形成一个可做关键词判断的语义字符串。"""
    return " ".join(
        str(item.get(key, "") or "")
        for key in (
            "layer",
            "parent_block_name",
            "block_path",
            "raw_text",
            "norm_text",
            "entity_type",
            "geometry_kind",
        )
    )


def has_axis_dimension_semantic(item: dict[str, Any]) -> bool:
    """判断 CAD 对象是否具有轴网或尺寸标注相关语义。"""
    meta = item_semantic_text(item)
    return text_has_any(meta, AXIS_DIMENSION_TERMS) or text_has_any(meta, AXIS_DIMENSION_STRICT_TERMS)


def has_inspection_region_exclude_semantic(item: dict[str, Any]) -> bool:
    """判断 CAD 对象是否属于图框、图签、图例、详图、说明等应排除语义。"""
    meta = item_semantic_text(item)
    return text_has_any(meta, INSPECTION_REGION_EXCLUDE_TERMS) or text_has_any(
        meta,
        INSPECTION_REGION_STRICT_EXCLUDE_TERMS,
    )


def is_line_like_item(item: dict[str, Any]) -> bool:
    """判断 inventory 对象是否近似线性实体。"""
    entity = str(item.get("entity_type", "")).upper()
    geometry = str(item.get("geometry_kind", "")).lower()
    return (
            entity in {"LINE", "LWPOLYLINE", "POLYLINE", "XLINE", "RAY", "ARC"}
            or "line" in geometry
            or "polyline" in geometry
            or "curve" in geometry
    )


def is_text_like_item(item: dict[str, Any]) -> bool:
    """判断 inventory 对象是否近似文本实体。"""
    entity = str(item.get("entity_type", "")).upper()
    geometry = str(item.get("geometry_kind", "")).lower()
    return entity in {"TEXT", "MTEXT", "ATTRIB"} or "text" in geometry


def is_insert_like_item(item: dict[str, Any]) -> bool:
    """判断 inventory 对象是否为 INSERT / block_insert。"""
    entity = str(item.get("entity_type", "")).upper()
    geometry = str(item.get("geometry_kind", "")).lower()
    return entity == "INSERT" or geometry == "block_insert"


def axis_label_group(item: dict[str, Any]) -> str:
    meta = item_semantic_text(item)
    has_axis_label_context = text_has_any(meta, AXIS_LABEL_CONTEXT_TERMS)
    if not has_axis_label_context:
        return ""
    text = normalize_axis_label_text(item.get("norm_text") or item.get("raw_text"))
    if not text or len(text) > 12:
        return ""
    match = re.fullmatch(r"([A-Z]{1,4})[- ]?([0-9]{1,3})([A-Z]?)", text)
    if match:
        prefix = match.group(1)
        return "" if prefix in NON_AXIS_LABEL_PREFIXES else prefix
    if re.fullmatch(r"[0-9]{1,3}", text):
        return "NUM"
    return ""


def loose_axis_label_group(item: dict[str, Any]) -> str:
    entity = str(item.get("entity_type", "") or "").upper()
    geometry = str(item.get("geometry_kind", "") or "").lower()
    if entity not in {"TEXT", "MTEXT", "ATTRIB"} and "text" not in geometry:
        return ""
    meta = item_semantic_text(item)
    if text_has_any(meta, INSPECTION_REGION_EXCLUDE_TERMS):
        return ""
    text = normalize_axis_label_text(item.get("norm_text") or item.get("raw_text"))
    match = re.fullmatch(r"([A-Z])[- ]?([0-9]{1,3})([A-Z]?)", text)
    if not match:
        return ""
    prefix = match.group(1)
    return "" if prefix in NON_AXIS_LABEL_PREFIXES else prefix


def axis_label_groups_in_sheet(
        sheet_bbox: tuple[float, float, float, float],
        items: list[dict[str, Any]],
        nearby_candidate_boxes: list[tuple[float, float, float, float]] | None = None,
) -> dict[str, list[tuple[float, float, float, float]]]:
    """在单个图幅内提取轴号标签，并按轴号前缀分组。"""
    groups: dict[str, list[tuple[float, float, float, float]]] = defaultdict(list)
    sw = max(bbox_width(sheet_bbox), 1.0)
    sh = max(bbox_height(sheet_bbox), 1.0)
    loose_distance = max(sw, sh) * 0.018
    for item in items:
        bbox = item["bbox"]
        if not bbox_intersects(bbox, sheet_bbox):
            continue
        clipped = bbox_clip(bbox, sheet_bbox)
        if clipped is None:
            continue
        group = axis_label_group(item)
        if not group and nearby_candidate_boxes:
            group = loose_axis_label_group(item)
            if group:
                center = bbox_center(clipped)
                if not any(bbox_point_distance(candidate_box, center) <= loose_distance for candidate_box in
                           nearby_candidate_boxes):
                    group = ""
        if group:
            groups[group].append(clipped)
    return groups


def axis_dimension_candidate_score(
        item: dict[str, Any],
        sheet_bbox: tuple[float, float, float, float],
) -> tuple[float, list[str]]:
    bbox = item["bbox"]
    clipped = bbox_clip(bbox, sheet_bbox)
    if clipped is None:
        return 0.0, []

    sheet_area = max(bbox_area(sheet_bbox), 1.0)
    item_area = max(bbox_area(clipped), 1.0)
    if item_area / sheet_area > 0.80:
        return 0.0, []

    meta = item_semantic_text(item)
    has_axis_dimension = has_axis_dimension_semantic(item)
    if has_inspection_region_exclude_semantic(item) and not has_axis_dimension:
        return 0.0, []

    sw = max(bbox_width(sheet_bbox), 1.0)
    sh = max(bbox_height(sheet_bbox), 1.0)
    w = bbox_width(clipped)
    h = bbox_height(clipped)
    cx, cy = bbox_center(clipped)
    edge_distance = min(
        abs(cx - sheet_bbox[0]) / sw,
        abs(cx - sheet_bbox[2]) / sw,
        abs(cy - sheet_bbox[1]) / sh,
        abs(cy - sheet_bbox[3]) / sh,
    )
    near_sheet_edge = edge_distance <= 0.14
    long_horizontal = w >= sw * 0.18 and h <= sh * 0.08
    long_vertical = h >= sh * 0.18 and w <= sw * 0.08

    source = str(item.get("source", ""))
    line_like = is_line_like_item(item)
    text_like = is_text_like_item(item)
    insert_like = is_insert_like_item(item)

    score = 0.0
    reasons: list[str] = []
    if has_axis_dimension:
        score += 3.2
        reasons.append("axis_dimension_semantic")
    if line_like:
        score += 0.8
        reasons.append("line_like")
    if long_horizontal:
        score += 1.7
        reasons.append("long_horizontal")
    if long_vertical:
        score += 1.7
        reasons.append("long_vertical")
    if near_sheet_edge:
        score += 0.6
        reasons.append("near_sheet_edge")
    if text_like and has_axis_dimension:
        score += 0.8
        reasons.append("axis_dimension_text")
    if insert_like and has_axis_dimension:
        score += 0.6
        reasons.append("axis_dimension_block")
    if source == "direct_entity":
        score += 0.3

    if not has_axis_dimension:
        return 0.0, []
    return score, reasons


def region_payload_from_boxes(
        *,
        region_id: str,
        boxes: list[tuple[float, float, float, float]],
        sheet_bbox: tuple[float, float, float, float],
        source: str,
        confidence: float,
        evidence: str,
        object_count: int,
) -> dict[str, Any] | None:
    """根据候选 bbox 集合生成 inspection region 结构，并执行面积、宽高比例等合理性检查。"""
    if not boxes:
        return None
    sw = max(bbox_width(sheet_bbox), 1.0)
    sh = max(bbox_height(sheet_bbox), 1.0)
    raw = bbox_union(boxes)
    padded = bbox_inflate(raw, sw * 0.0025, sh * 0.0025)
    region_bbox = bbox_clip(padded, sheet_bbox)
    if region_bbox is None:
        return None
    area_ratio = bbox_area(region_bbox) / max(bbox_area(sheet_bbox), 1.0)
    width_ratio = bbox_width(region_bbox) / sw
    height_ratio = bbox_height(region_bbox) / sh
    if area_ratio < 0.02 or area_ratio > 0.97 or width_ratio < 0.12 or height_ratio < 0.12:
        return None
    return {
        "region_id": region_id,
        "bbox": region_bbox,
        "source": source,
        "confidence": round(confidence, 3),
        "object_count": object_count,
        "evidence": evidence,
    }


def layer_axis_dimension_envelope(
        *,
        sheet_bbox: tuple[float, float, float, float],
        candidates: list[tuple[tuple[float, float, float, float], float, list[str], str, dict[str, Any]]],
) -> dict[str, Any] | None:
    if not candidates:
        return None

    sw = max(bbox_width(sheet_bbox), 1.0)
    sh = max(bbox_height(sheet_bbox), 1.0)
    sheet_area = max(bbox_area(sheet_bbox), 1.0)
    raw_bbox = bbox_union([candidate[0] for candidate in candidates])
    raw_width = max(bbox_width(raw_bbox), 1.0)
    raw_height = max(bbox_height(raw_bbox), 1.0)
    grouped: dict[str, list[tuple[tuple[float, float, float, float], float, list[str], dict[str, Any]]]] = defaultdict(
        list)
    for bbox, score, reasons, layer, item in candidates:
        grouped[layer or "-"].append((bbox, score, reasons, item))

    layer_rows: list[dict[str, Any]] = []
    for layer, entries in grouped.items():
        if len(entries) < 6:
            continue
        layer_text = layer.upper()
        if any(term.upper() in layer_text for term in ["TITLE", "BORDER", "FRAME", "LEGEND", "NOTE", "TABLE"]):
            continue
        if text_has_any(layer, INSPECTION_REGION_STRICT_EXCLUDE_TERMS):
            continue

        boxes = [entry[0] for entry in entries]
        bbox = bbox_union(boxes)
        area_ratio = bbox_area(bbox) / sheet_area
        width_ratio = bbox_width(bbox) / sw
        height_ratio = bbox_height(bbox) / sh
        if not (0.015 <= area_ratio <= 0.92 and width_ratio >= 0.10 and height_ratio >= 0.10):
            continue

        line_count = sum(1 for _bbox, _score, _reasons, item in entries if is_line_like_item(item))
        text_count = sum(1 for _bbox, _score, _reasons, item in entries if is_text_like_item(item))
        insert_count = sum(1 for _bbox, _score, _reasons, item in entries if is_insert_like_item(item))
        horizontal_count = 0
        vertical_count = 0
        for entry_bbox, _score, _reasons, item in entries:
            if not is_line_like_item(item):
                continue
            w = bbox_width(entry_bbox)
            h = bbox_height(entry_bbox)
            if w >= sw * 0.015 and w >= h * 3.0:
                horizontal_count += 1
            if h >= sh * 0.015 and h >= w * 3.0:
                vertical_count += 1

        axis_layer = text_has_any(layer, ["AXIS", "GRID", "\u8f74", "\u8f74\u7f51", "\u8f74\u53f7", "\u8f74\u6587"])
        dim_layer = text_has_any(layer, ["DIM", "\u5c3a\u5bf8", "\u603b\u5c3a\u5bf8", "\u6807\u6ce8"])
        orientation_balance = min(horizontal_count, vertical_count) / max(max(horizontal_count, vertical_count), 1)
        structure_count = line_count + text_count + insert_count
        score = 0.0
        if axis_layer:
            score += 8.0
        if dim_layer:
            score += 5.0
        score += min(len(entries), 220) / 45.0
        score += min(line_count, 140) / 45.0
        score += min(text_count + insert_count, 160) / 55.0
        score += orientation_balance * 2.0
        score += min(width_ratio, 1.0) * 1.4 + min(height_ratio, 1.0) * 1.4
        if area_ratio > 0.88:
            score -= 2.0
        if not axis_layer and not dim_layer:
            score -= 3.0

        layer_rows.append(
            {
                "layer": layer,
                "bbox": bbox,
                "boxes": boxes,
                "count": len(entries),
                "line_count": line_count,
                "text_count": text_count,
                "insert_count": insert_count,
                "horizontal_count": horizontal_count,
                "vertical_count": vertical_count,
                "area_ratio": area_ratio,
                "width_ratio": width_ratio,
                "height_ratio": height_ratio,
                "axis_layer": axis_layer,
                "dim_layer": dim_layer,
                "score": score,
            }
        )

    if not layer_rows:
        return None

    layer_rows.sort(key=lambda row: row["score"], reverse=True)
    best = layer_rows[0]
    selected = [best]
    for row in layer_rows[1:]:
        if len(selected) >= 5:
            break
        if row["score"] < max(4.8, best["score"] * 0.55):
            continue
        width_support = bbox_width(row["bbox"]) / raw_width
        height_support = bbox_height(row["bbox"]) / raw_height
        intersects_best = bbox_intersection_area(row["bbox"], best["bbox"]) > 0
        strong_structural_layer = row["axis_layer"] or row["dim_layer"]
        if strong_structural_layer and (intersects_best or width_support >= 0.45 or height_support >= 0.45):
            selected.append(row)

    selected_bbox = bbox_union([row["bbox"] for row in selected])
    selected_width = bbox_width(selected_bbox)
    selected_height = bbox_height(selected_bbox)
    raw_area = max(bbox_area(raw_bbox), 1.0)
    selected_area = bbox_area(selected_bbox)
    width_support = selected_width / raw_width
    height_support = selected_height / raw_height
    if selected_area >= raw_area * 0.98:
        return None
    if width_support < 0.55 or height_support < 0.45:
        return None

    evidence = " | ".join(
        f"layer={row['layer']}:n={row['count']},score={row['score']:.1f},wr={row['width_ratio']:.2f},hr={row['height_ratio']:.2f}"
        for row in selected
    )
    return region_payload_from_boxes(
        region_id="R01",
        boxes=[selected_bbox],
        sheet_bbox=sheet_bbox,
        source="axis_dimension_layer_envelope",
        confidence=min(0.97, 0.72 + min(best["score"], 18.0) / 18.0 * 0.18),
        evidence=evidence,
        object_count=sum(row["count"] for row in selected),
    )


def line_orientation_flags(
        bbox: tuple[float, float, float, float],
        sheet_bbox: tuple[float, float, float, float],
) -> tuple[bool, bool]:
    """判断线性 bbox 是否近似水平或垂直轴网线。"""
    sw = max(bbox_width(sheet_bbox), 1.0)
    sh = max(bbox_height(sheet_bbox), 1.0)
    w = bbox_width(bbox)
    h = bbox_height(bbox)
    horizontal = w >= sw * 0.015 and w >= h * 3.0 and h <= sh * 0.08
    vertical = h >= sh * 0.015 and h >= w * 3.0 and w <= sw * 0.08
    return horizontal, vertical


def cluster_1d_count(values: list[float], tolerance: float) -> int:
    """按容差聚合一维坐标，估计重复轴线/网格带数量。"""
    if not values:
        return 0
    ordered = sorted(values)
    count = 1
    current = ordered[0]
    for value in ordered[1:]:
        if abs(value - current) > tolerance:
            count += 1
            current = value
        else:
            current = (current + value) / 2.0
    return count


def geometry_axis_dimension_layers(
        sheet_bbox: tuple[float, float, float, float],
        items: list[dict[str, Any]],
) -> set[str]:
    grouped: dict[str, list[tuple[tuple[float, float, float, float], bool, bool]]] = defaultdict(list)
    sw = max(bbox_width(sheet_bbox), 1.0)
    sh = max(bbox_height(sheet_bbox), 1.0)
    sheet_area = max(bbox_area(sheet_bbox), 1.0)

    for item in items:
        if not is_line_like_item(item):
            continue
        if has_inspection_region_exclude_semantic(item):
            continue
        clipped = bbox_clip(item["bbox"], sheet_bbox)
        if clipped is None:
            continue
        horizontal, vertical = line_orientation_flags(clipped, sheet_bbox)
        if not horizontal and not vertical:
            continue
        layer = str(item.get("layer", "") or "-")
        if text_has_any(layer, INSPECTION_REGION_STRICT_EXCLUDE_TERMS):
            continue
        grouped[layer].append((clipped, horizontal, vertical))

    result: set[str] = set()
    for layer, entries in grouped.items():
        if len(entries) < 18:
            continue
        boxes = [entry[0] for entry in entries]
        bbox = bbox_union(boxes)
        area_ratio = bbox_area(bbox) / sheet_area
        width_ratio = bbox_width(bbox) / sw
        height_ratio = bbox_height(bbox) / sh
        if not (0.02 <= area_ratio <= 0.88 and width_ratio >= 0.25 and height_ratio >= 0.18):
            continue
        h_centers = [bbox_center(box)[1] for box, horizontal, _vertical in entries if horizontal]
        v_centers = [bbox_center(box)[0] for box, _horizontal, vertical in entries if vertical]
        h_band_count = cluster_1d_count(h_centers, sh * 0.012)
        v_band_count = cluster_1d_count(v_centers, sw * 0.012)
        h_count = len(h_centers)
        v_count = len(v_centers)
        if h_count >= 6 and v_count >= 6 and h_band_count >= 3 and v_band_count >= 3:
            result.add(layer)
    return result


def split_axis_dimension_regions_by_axis_labels(
        *,
        sheet_bbox: tuple[float, float, float, float],
        items: list[dict[str, Any]],
        candidates: list[tuple[tuple[float, float, float, float], float, list[str], str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """当一个 sheet 内存在多个轴号组时，尝试按轴号组切分多个 inspection region。"""
    candidate_boxes = [candidate[0] for candidate in candidates]
    sw = max(bbox_width(sheet_bbox), 1.0)
    sh = max(bbox_height(sheet_bbox), 1.0)
    strict_label_groups = axis_label_groups_in_sheet(sheet_bbox, items)
    loose_label_groups = axis_label_groups_in_sheet(sheet_bbox, items, candidate_boxes)
    label_groups: dict[str, dict[str, Any]] = {}

    for group, boxes in strict_label_groups.items():
        if group != "NUM" and len(boxes) >= 4:
            label_groups[group] = {"boxes": boxes, "strict": True}
    for group, boxes in loose_label_groups.items():
        if group == "NUM" or group in label_groups or len(boxes) < 5:
            continue
        centers = [bbox_center(box) for box in boxes]
        edge_count = sum(
            1
            for cx, cy in centers
            if (
                    cx <= sheet_bbox[0] + sw * 0.12
                    or cx >= sheet_bbox[2] - sw * 0.12
                    or cy <= sheet_bbox[1] + sh * 0.18
                    or cy >= sheet_bbox[3] - sh * 0.18
            )
        )
        if edge_count / max(len(boxes), 1) < 0.55:
            continue
        label_groups[group] = {"boxes": boxes, "strict": False}

    usable_groups: list[dict[str, Any]] = []
    for group, payload in label_groups.items():
        boxes = payload["boxes"]
        if len(boxes) < 4:
            continue
        group_bbox = bbox_union(boxes)
        if bbox_width(group_bbox) < sw * 0.05 and bbox_height(group_bbox) < sh * 0.05:
            continue
        centers = [bbox_center(box) for box in boxes]
        edge_count = sum(
            1
            for cx, cy in centers
            if (
                    cx <= sheet_bbox[0] + sw * 0.12
                    or cx >= sheet_bbox[2] - sw * 0.12
                    or cy <= sheet_bbox[1] + sh * 0.18
                    or cy >= sheet_bbox[3] - sh * 0.18
            )
        )
        edge_ratio = edge_count / max(len(boxes), 1)
        usable_groups.append(
            {
                "group": group,
                "bbox": group_bbox,
                "count": len(boxes),
                "center": bbox_center(group_bbox),
                "boxes": boxes,
                "strict": bool(payload.get("strict")),
                "edge_ratio": edge_ratio,
            }
        )
    if len(usable_groups) < 2:
        return []
    if len(usable_groups) > 3:
        return []

    x_spread = max(group["center"][0] for group in usable_groups) - min(group["center"][0] for group in usable_groups)
    y_spread = max(group["center"][1] for group in usable_groups) - min(group["center"][1] for group in usable_groups)
    axis = "x" if x_spread / sw >= y_spread / sh else "y"
    coord_index = 0 if axis == "x" else 1
    size = sw if axis == "x" else sh
    low_edge = sheet_bbox[0] if axis == "x" else sheet_bbox[1]
    high_edge = sheet_bbox[2] if axis == "x" else sheet_bbox[3]

    usable_groups.sort(key=lambda group: group["center"][coord_index])
    separated = False
    for left, right in zip(usable_groups, usable_groups[1:]):
        left_max = left["bbox"][2] if axis == "x" else left["bbox"][3]
        right_min = right["bbox"][0] if axis == "x" else right["bbox"][1]
        if right_min - left_max > size * 0.015:
            separated = True
            break
        center_gap = right["center"][coord_index] - left["center"][coord_index]
        if center_gap > size * 0.12:
            separated = True
            break
    if not separated:
        return []

    centers = [group["center"][coord_index] for group in usable_groups]
    boundaries = [low_edge]
    boundaries.extend((centers[idx] + centers[idx + 1]) / 2.0 for idx in range(len(centers) - 1))
    boundaries.append(high_edge)

    regions: list[dict[str, Any]] = []
    for idx, group in enumerate(usable_groups, start=1):
        low = boundaries[idx - 1]
        high = boundaries[idx]
        member_boxes = list(group["boxes"])
        member_count = len(group["boxes"])
        for bbox, _score, _reasons, _layer, _item in candidates:
            cx, cy = bbox_center(bbox)
            coord = cx if axis == "x" else cy
            if low <= coord <= high:
                member_boxes.append(bbox)
                member_count += 1
        payload = region_payload_from_boxes(
            region_id=f"R{idx:02d}_{group['group']}",
            boxes=member_boxes,
            sheet_bbox=sheet_bbox,
            source=f"axis_label_group_{group['group']}",
            confidence=min(0.97, 0.72 + min(member_count, 160) / 160.0 * 0.20),
            evidence=f"axis_group={group['group']} axis={axis} labels={group['count']} candidates={member_count}",
            object_count=member_count,
        )
        if payload:
            regions.append(payload)
    return regions if len(regions) >= 2 else []


def infer_inspection_region(
        sheet: dict[str, Any],
        items: list[dict[str, Any]],
) -> dict[str, Any]:
    sheet_bbox = sheet["bbox"]
    sw = max(bbox_width(sheet_bbox), 1.0)
    sh = max(bbox_height(sheet_bbox), 1.0)
    candidates: list[tuple[tuple[float, float, float, float], float, list[str], str, dict[str, Any]]] = []
    geometry_layers = geometry_axis_dimension_layers(sheet_bbox, items)

    for item in items:
        bbox = item["bbox"]
        if not bbox_intersects(bbox, sheet_bbox):
            continue
        clipped = bbox_clip(bbox, sheet_bbox)
        if clipped is None:
            continue
        score, reasons = axis_dimension_candidate_score(item, sheet_bbox)
        layer = str(item.get("layer", "") or "-")
        if score < 3.0 and layer in geometry_layers and is_line_like_item(item):
            horizontal, vertical = line_orientation_flags(clipped, sheet_bbox)
            if horizontal or vertical:
                score = 3.15
                reasons = ["grid_geometry_layer", "line_like"]
                if horizontal:
                    reasons.append("long_horizontal")
                if vertical:
                    reasons.append("long_vertical")
        if score < 3.0:
            continue
        candidates.append((clipped, score, reasons, layer, item))

    source = "sheet_bbox_fallback"
    confidence = 0.35
    region_bbox = sheet_bbox
    evidence = "fallback: insufficient axis/dimension envelope evidence"
    regions: list[dict[str, Any]] = [
        {
            "region_id": "R01",
            "bbox": sheet_bbox,
            "source": source,
            "confidence": confidence,
            "object_count": 0,
            "evidence": evidence,
        }
    ]

    if candidates:
        split_regions = split_axis_dimension_regions_by_axis_labels(
            sheet_bbox=sheet_bbox,
            items=items,
            candidates=candidates,
        )
        if split_regions:
            regions = split_regions
            region_bbox = bbox_union([region["bbox"] for region in regions])
            source = "axis_label_group_split"
            confidence = round(min(region["confidence"] for region in regions), 3)
            evidence = " | ".join(region["evidence"] for region in regions[:4])
        else:
            layer_payload = layer_axis_dimension_envelope(sheet_bbox=sheet_bbox, candidates=candidates)
            if layer_payload:
                regions = [layer_payload]
                region_bbox = layer_payload["bbox"]
                source = layer_payload["source"]
                confidence = layer_payload["confidence"]
                evidence = layer_payload["evidence"]
            else:
                candidate_boxes = [item[0] for item in candidates]
                raw_region = bbox_union(candidate_boxes)
                clipped_region = bbox_clip(raw_region, sheet_bbox) or raw_region
                area_ratio = bbox_area(clipped_region) / max(bbox_area(sheet_bbox), 1.0)
                width_ratio = bbox_width(clipped_region) / sw
                height_ratio = bbox_height(clipped_region) / sh
                enough_envelope = (
                        len(candidates) >= 8
                        and 0.03 <= area_ratio <= 0.96
                        and width_ratio >= 0.35
                        and height_ratio >= 0.22
                )
                if enough_envelope:
                    reason_counts = Counter(reason for _, _, reasons, _layer, _item in candidates for reason in reasons)
                    layer_counts = Counter(layer for _, _, _, layer, _item in candidates if layer)
                    evidence_parts = [
                        f"{key}:{value}" for key, value in reason_counts.most_common(4)
                    ]
                    evidence_parts.extend(
                        f"layer={key}:{value}" for key, value in layer_counts.most_common(3)
                    )
                    source = "axis_dimension_envelope"
                    confidence = min(
                        0.96,
                        0.58
                        + min(len(candidates), 120) / 120.0 * 0.18
                        + min(width_ratio, 1.0) * 0.10
                        + min(height_ratio, 1.0) * 0.10,
                    )
                    evidence = " | ".join(evidence_parts)
                    payload = region_payload_from_boxes(
                        region_id="R01",
                        boxes=candidate_boxes,
                        sheet_bbox=sheet_bbox,
                        source=source,
                        confidence=confidence,
                        evidence=evidence,
                        object_count=len(candidates),
                    )
                    if payload:
                        regions = [payload]
                        region_bbox = payload["bbox"]
                        confidence = payload["confidence"]
                    else:
                        evidence = "fallback: envelope failed sanity checks"
                else:
                    evidence = (
                        "fallback: weak envelope "
                        f"objects={len(candidates)} area_ratio={area_ratio:.3f} "
                        f"width_ratio={width_ratio:.3f} height_ratio={height_ratio:.3f}"
                    )

    serialized_regions = [
        {
            **region,
            "bbox": [
                region["bbox"][0],
                region["bbox"][1],
                region["bbox"][2],
                region["bbox"][3],
            ],
        }
        for region in regions
    ]
    return {
        "inspection_region_bbox": region_bbox,
        "inspection_regions": serialized_regions,
        "inspection_region_source": source,
        "inspection_region_confidence": round(confidence, 3),
        "inspection_region_object_count": len(candidates),
        "inspection_region_evidence": evidence,
    }


def chinese_number_to_int(value: str) -> int | None:
    """将中文数字或阿拉伯数字文本转换为整数，支持一到九、十、二十等简单楼层表达。"""
    text = compact_text(value)
    if not text:
        return None
    if re.fullmatch(r"\d+", text):
        return int(text)
    if text == "十":
        return 10
    if "十" in text:
        left, _, right = text.partition("十")
        tens = CN_NUMBERS.get(left, 1) if left else 1
        ones = CN_NUMBERS.get(right, 0) if right else 0
        return tens * 10 + ones
    if text in CN_NUMBERS:
        return CN_NUMBERS[text]
    return None


def floor_label(floor_id: str) -> str:
    """将标准 floor_id 转换为中文楼层名称，例如 B2 -> 地下2层。"""
    if floor_id.startswith("B") and floor_id[1:].isdigit():
        n = int(floor_id[1:])
        return f"地下{n}层"
    if floor_id.startswith("F") and floor_id[1:].isdigit():
        n = int(floor_id[1:])
        return "首层" if n == 1 else f"{n}层"
    if floor_id == "ROOF":
        return "屋顶层"
    if floor_id == "STANDARD":
        return "标准层"
    if floor_id == "EQUIPMENT":
        return "设备层"
    if floor_id == "MEZZANINE":
        return "夹层"
    if floor_id == "REFUGE":
        return "避难层"
    return floor_id


def is_plausible_floor_id(floor_id: str) -> bool:
    """过滤不合理楼层编号，避免文本噪声生成异常 floor_id。"""
    if floor_id in {"ROOF", "STANDARD", "EQUIPMENT", "MEZZANINE", "REFUGE"}:
        return True
    if floor_id.startswith("B") and floor_id[1:].isdigit():
        return 1 <= int(floor_id[1:]) <= 8
    if floor_id.startswith("F") and floor_id[1:].isdigit():
        return 1 <= int(floor_id[1:]) <= 80
    return False


def detect_floor_mentions(value: Any) -> list[tuple[str, str]]:
    """从标题或文本中识别楼层信息，返回 floor_id 与证据文本。"""
    text = compact_text(value)
    if not text:
        return []

    matches: list[tuple[str, str]] = []
    if "设备层" in text:
        matches.append(("EQUIPMENT", "设备层"))
    if "避难层" in text:
        matches.append(("REFUGE", "避难层"))
    if "夹层" in text:
        matches.append(("MEZZANINE", "夹层"))
    if "屋顶" in text or "屋面" in text or "顶层" in text:
        matches.append(("ROOF", "屋顶/屋面"))
    if "标准层" in text:
        matches.append(("STANDARD", "标准层"))
    if "首层" in text:
        matches.append(("F1", "首层"))

    for pattern in [r"地下([一二两三四五六七八九十\d]+)层", r"负([一二两三四五六七八九十\d]+)层"]:
        for raw in re.findall(pattern, text):
            number = chinese_number_to_int(raw)
            if number:
                matches.append((f"B{number}", f"地下{raw}层"))

    for raw in re.findall(r"\bB\s*([0-9]+)\s*(?:F|层)?\b", text, flags=re.I):
        number = as_int(raw)
        if number:
            matches.append((f"B{number}", f"B{number}"))

    for raw in re.findall(r"(?:第)?([一二两三四五六七八九十\d]+)层", text):
        if re.search(rf"(地下|负){re.escape(raw)}层", text):
            continue
        number = chinese_number_to_int(raw)
        if number:
            matches.append((f"F{number}", f"{raw}层"))

    for raw in re.findall(r"\b([1-9][0-9]?)\s*F\b", text, flags=re.I):
        number = as_int(raw)
        if number:
            matches.append((f"F{number}", f"{number}F"))

    deduped: list[tuple[str, str]] = []
    seen = set()
    for floor_id, evidence in matches:
        if floor_id not in seen and is_plausible_floor_id(floor_id):
            seen.add(floor_id)
            deduped.append((floor_id, evidence))
    return deduped


def normalize_title_text(value: Any) -> str:
    """清洗图名文本，去除多余空白。"""
    return re.sub(r"\s+", "", str(value or "").strip())


def floor_title_candidate_for_item(item: dict[str, Any]) -> tuple[str, list[tuple[str, str]], float] | None:
    """判断单个文本对象是否可作为楼层图名候选，并给出楼层 mentions 与权重。"""
    title = normalize_title_text(item.get("norm_text") or item.get("raw_text"))
    if not title or "平面图" not in title:
        return None
    if len(title) > 80:
        return None
    if is_negative_plan_title(title):
        return None

    mentions = detect_floor_mentions(title)
    if not mentions:
        return None

    meta = " ".join(
        [
            str(item.get("layer", "")),
            str(item.get("parent_block_name", "")),
            str(item.get("block_path", "")),
        ]
    )
    weight = 36.0
    if text_has_any(meta, FRAME_LAYER_TERMS):
        weight += 14.0
    if any(token in str(meta).upper() for token in ["TK", "TITLE", "PMSHEET"]):
        weight += 8.0
    if len(title) <= 30:
        weight += 4.0
    return title, mentions, weight


def is_local_detail_plan_title(title: Any) -> bool:
    """判断标题是否属于局部详图、节点、大样、风井等局部图名。"""
    text = compact_text(title)
    return bool(text) and text_has_any(text, LOCAL_DETAIL_PLAN_TITLE_TERMS)


def titlebar_meta_text(item: dict[str, Any]) -> str:
    """拼接对象的图层、父块、块路径信息，用于判断是否来自标题栏。"""
    return " ".join(
        [
            str(item.get("layer", "")),
            str(item.get("parent_block_name", "")),
            str(item.get("block_path", "")),
        ]
    )


def item_has_titlebar_meta(item: dict[str, Any]) -> bool:
    """判断对象是否具有标题栏或图签元信息。"""
    meta = titlebar_meta_text(item)
    return text_has_any(meta, FRAME_LAYER_TERMS) or text_has_any(meta, TITLEBAR_META_TERMS)


def is_titlebar_area_item(
        item: dict[str, Any],
        sheet_bbox: tuple[float, float, float, float],
        title: str,
) -> bool:
    """Return True when a title-like text is in the expected title block zone."""
    bbox = item.get("bbox")
    if not bbox:
        return False
    sw = max(bbox_width(sheet_bbox), 1.0)
    sh = max(bbox_height(sheet_bbox), 1.0)
    cx, cy = bbox_center(bbox)
    text_is_short_title = len(compact_text(title)) <= 42
    if not text_is_short_title:
        return False

    right_title_zone = cx >= sheet_bbox[0] + sw * 0.70
    bottom_title_zone = cy <= sheet_bbox[1] + sh * 0.24
    return right_title_zone or bottom_title_zone


def sheet_title_candidate_for_item(
        item: dict[str, Any],
        sheet_bbox: tuple[float, float, float, float],
) -> tuple[str, list[tuple[str, str]], float] | None:
    candidate = floor_title_candidate_for_item(item)
    if not candidate:
        return None

    title, mentions, weight = candidate
    if is_local_detail_plan_title(title):
        return None
    has_meta = item_has_titlebar_meta(item)
    in_titlebar_area = is_titlebar_area_item(item, sheet_bbox, title)
    if not has_meta and not in_titlebar_area:
        return None
    if has_meta:
        weight += 18.0
    if in_titlebar_area:
        weight += 8.0
    return title, mentions, weight


def load_inventory_items(inventory_dir: Path) -> list[dict[str, Any]]:
    """读取 cad_object_inventory.csv，并转换为带 bbox、中心点、面积的内部 item 列表。"""
    path = inventory_dir / "cad_object_inventory.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    items: list[dict[str, Any]] = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            bbox = bbox_from_row(row)
            if not bbox:
                continue
            cx, cy = bbox_center(bbox)
            items.append(
                {
                    "object_id": row.get("object_id", ""),
                    "source": row.get("source", ""),
                    "entity_type": row.get("entity_type", ""),
                    "geometry_kind": row.get("geometry_kind", ""),
                    "is_closed": row.get("is_closed", ""),
                    "layer": row.get("layer", ""),
                    "parent_block_name": row.get("parent_block_name", ""),
                    "block_path": row.get("block_path", ""),
                    "raw_text": row.get("raw_text", ""),
                    "norm_text": row.get("norm_text", ""),
                    "bbox": bbox,
                    "cx": cx,
                    "cy": cy,
                    "area": bbox_area(bbox),
                }
            )
    return items


def frame_candidate_score(item: dict[str, Any], global_bbox: tuple[float, float, float, float]) -> float:
    """对单个 CAD 对象作为图框候选的可能性打分。"""
    bbox = item["bbox"]
    minx, miny, maxx, maxy = bbox
    width = maxx - minx
    height = maxy - miny
    global_width = global_bbox[2] - global_bbox[0]
    global_height = global_bbox[3] - global_bbox[1]
    if width <= 0 or height <= 0 or global_width <= 0 or global_height <= 0:
        return 0.0

    area = bbox_area(bbox)
    global_area = bbox_area(global_bbox)
    area_ratio = area / global_area if global_area > 0 else 0.0
    text = " ".join(
        [
            str(item.get("layer", "")),
            str(item.get("parent_block_name", "")),
            str(item.get("block_path", "")),
        ]
    )
    has_frame_semantic = text_has_any(text, FRAME_LAYER_TERMS)
    if area_ratio < 0.0002 and not has_frame_semantic:
        return 0.0
    if area_ratio > 0.85:
        return 0.0
    if not has_frame_semantic and (width < global_width * 0.08 or height < global_height * 0.03):
        return 0.0

    entity = str(item.get("entity_type", "")).upper()
    geometry = str(item.get("geometry_kind", "")).lower()
    is_closed = str(item.get("is_closed", "")) == "1"

    score = 0.0
    if entity in {"LWPOLYLINE", "POLYLINE"} and is_closed:
        score += 4.0
    if geometry == "polyline_closed":
        score += 2.0
    if entity == "INSERT" or geometry == "block_insert":
        score += 1.0
    if has_frame_semantic:
        score += 5.0
    elif text_has_any(text, FRAME_NEGATIVE_LAYER_TERMS):
        score -= 3.0
    if str(item.get("source", "")) == "direct_entity":
        score += 1.0

    aspect = width / height
    if 0.25 <= aspect <= 8.0:
        score += 1.0
    else:
        score -= 2.0
    if area_ratio >= 0.015:
        score += 1.0
    if has_frame_semantic and area >= 1e9:
        score += 2.0
    if has_frame_semantic and width >= 80000 and height >= 50000:
        score += 1.0

    return score


def collect_frame_title_evidence(
        frame_bbox: tuple[float, float, float, float],
        items: list[dict[str, Any]],
) -> dict[str, Any]:
    """统计图框内部标题栏图名和楼层证据。"""
    title_samples: list[str] = []
    floor_ids: list[str] = []
    title_count = 0
    for item in items:
        if not bbox_contains_point(frame_bbox, (float(item["cx"]), float(item["cy"]))):
            continue
        candidate = sheet_title_candidate_for_item(item, frame_bbox)
        if not candidate:
            continue
        title, mentions, _weight = candidate
        title_count += 1
        if title not in title_samples and len(title_samples) < 8:
            title_samples.append(title)
        for floor_id, _evidence in mentions:
            if floor_id not in floor_ids:
                floor_ids.append(floor_id)
    return {
        "has_plan_title_evidence": title_count > 0,
        "title_evidence_count": title_count,
        "title_evidence_samples": title_samples,
        "title_floor_ids": floor_ids[:8],
    }


def detect_frame_sheets(items: list[dict[str, Any]], global_bbox: tuple[float, float, float, float]) -> list[
    dict[str, Any]]:
    """基于图框语义、闭合多段线、面积比例和标题证据检测 sheet 候选。"""
    candidates: list[dict[str, Any]] = []
    for item in items:
        score = frame_candidate_score(item, global_bbox)
        if score < 5.0:
            continue
        candidates.append(
            {
                "bbox": item["bbox"],
                "method": "frame_candidate",
                "confidence": min(0.95, 0.45 + score * 0.06),
                "score": score,
                "evidence": [
                    f"entity={item.get('entity_type')}",
                    f"geometry={item.get('geometry_kind')}",
                    f"layer={item.get('layer')}",
                    f"block={item.get('parent_block_name')}",
                ],
            }
        )

    if len(candidates) >= 10:
        areas = sorted(bbox_area(row["bbox"]) for row in candidates)
        reference_area = areas[int(len(areas) * 0.75)]
        min_candidate_area = max(reference_area * 0.30, areas[-1] * 0.15)
        candidates = [row for row in candidates if bbox_area(row["bbox"]) >= min_candidate_area]

    candidates.sort(key=lambda row: (row["score"], bbox_area(row["bbox"])), reverse=True)
    kept: list[dict[str, Any]] = []
    for cand in candidates:
        if any(bbox_iou(cand["bbox"], old["bbox"]) >= 0.70 for old in kept):
            continue
        cand_area = bbox_area(cand["bbox"])
        contained_by_existing = False
        for old in kept:
            old_area = bbox_area(old["bbox"])
            if old_area <= cand_area:
                continue
            if bbox_containment(cand["bbox"], old["bbox"]) >= 0.88:
                contained_by_existing = True
                break
        if contained_by_existing:
            continue
        kept = [
            old
            for old in kept
            if not (
                    bbox_area(old["bbox"]) < cand_area
                    and bbox_containment(old["bbox"], cand["bbox"]) >= 0.88
            )
        ]
        kept.append(cand)
        if len(kept) >= 80:
            break
    for cand in kept:
        title_evidence = collect_frame_title_evidence(cand["bbox"], items)
        cand.update(title_evidence)
        if title_evidence["has_plan_title_evidence"]:
            cand["evidence"].append(
                "internal_plan_title="
                + ",".join(title_evidence.get("title_evidence_samples", [])[:3])
            )
            cand["score"] += 4.0
            cand["confidence"] = min(0.98, float(cand.get("confidence", 0.0)) + 0.03)
        else:
            cand["evidence"].append("missing_internal_plan_title")
    return kept


def select_cluster_items(items: list[dict[str, Any]], global_bbox: tuple[float, float, float, float]) -> list[
    dict[str, Any]]:
    """为密度聚类 fallback 筛选合适的图元，排除过大对象和尺寸标注。"""
    global_area = bbox_area(global_bbox)
    global_width = global_bbox[2] - global_bbox[0]
    global_height = global_bbox[3] - global_bbox[1]
    selected: list[dict[str, Any]] = []
    for item in items:
        bbox = item["bbox"]
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        if item["area"] <= 0:
            continue
        if item["area"] > global_area * 0.25:
            continue
        if width > global_width * 0.75 or height > global_height * 0.75:
            continue
        entity = str(item.get("entity_type", "")).upper()
        geometry = str(item.get("geometry_kind", "")).lower()
        if geometry in {"annotation_dimension"} or entity in {"DIMENSION", "LEADER", "MLEADER"}:
            continue
        selected.append(item)
    return selected


def detect_cluster_sheets(
        items: list[dict[str, Any]],
        global_bbox: tuple[float, float, float, float],
        *,
        grid_size: int = 180,
) -> list[dict[str, Any]]:
    """当图框检测失败时，基于网格密度连通域识别图幅候选。"""
    selected = select_cluster_items(items, global_bbox)
    if not selected:
        return []

    minx, miny, maxx, maxy = global_bbox
    width = maxx - minx
    height = maxy - miny
    if width <= 0 or height <= 0:
        return []

    grid_size = max(40, min(grid_size, 320))
    cell_to_indices: dict[tuple[int, int], list[int]] = defaultdict(list)
    occupied: set[tuple[int, int]] = set()
    for idx, item in enumerate(selected):
        gx = int((item["cx"] - minx) / width * (grid_size - 1))
        gy = int((item["cy"] - miny) / height * (grid_size - 1))
        gx = max(0, min(grid_size - 1, gx))
        gy = max(0, min(grid_size - 1, gy))
        cell = (gx, gy)
        cell_to_indices[cell].append(idx)
        occupied.add(cell)

    # Light dilation keeps sparse line drawings connected inside one sheet.
    dilated = set(occupied)
    for gx, gy in list(occupied):
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                nx = gx + dx
                ny = gy + dy
                if 0 <= nx < grid_size and 0 <= ny < grid_size:
                    dilated.add((nx, ny))

    visited: set[tuple[int, int]] = set()
    components: list[set[tuple[int, int]]] = []
    for cell in dilated:
        if cell in visited:
            continue
        queue = deque([cell])
        visited.add(cell)
        comp = set()
        while queue:
            cur = queue.popleft()
            comp.add(cur)
            x, y = cur
            for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                nxt = (x + dx, y + dy)
                if nxt in dilated and nxt not in visited:
                    visited.add(nxt)
                    queue.append(nxt)
        components.append(comp)

    sheets: list[dict[str, Any]] = []
    global_area = bbox_area(global_bbox)
    for comp in components:
        indices: set[int] = set()
        for cell in comp:
            indices.update(cell_to_indices.get(cell, []))
        if len(indices) < 30:
            continue
        boxes = [selected[idx]["bbox"] for idx in indices]
        bbox = bbox_union(boxes)
        area = bbox_area(bbox)
        if area < global_area * 0.002 or area > global_area * 0.90:
            continue
        sheets.append(
            {
                "bbox": bbox,
                "method": "density_cluster",
                "confidence": min(0.88, 0.45 + min(len(indices), 1000) / 2500),
                "score": len(indices),
                "evidence": [f"density_cluster_items={len(indices)}"],
            }
        )

    sheets.sort(key=lambda row: (bbox_center(row["bbox"])[1], bbox_center(row["bbox"])[0]), reverse=True)
    return sheets


class QuadTreeNode:
    """Point quadtree used to avoid scanning every CAD entity during seed growth."""

    def __init__(
            self,
            bounds: tuple[float, float, float, float],
            *,
            depth: int = 0,
            max_depth: int = 9,
            max_points: int = 80,
    ) -> None:
        """初始化四叉树节点，保存边界、深度限制和点容量阈值。"""
        self.bounds = bounds
        self.depth = depth
        self.max_depth = max_depth
        self.max_points = max_points
        self.points: list[tuple[int, tuple[float, float]]] = []
        self.children: list[QuadTreeNode] = []

    def insert(self, idx: int, point: tuple[float, float]) -> bool:
        """向四叉树节点插入点索引；超过容量时自动分裂。"""
        if not bbox_contains_point(self.bounds, point):
            return False
        if self.children:
            for child in self.children:
                if child.insert(idx, point):
                    return True
            return False
        self.points.append((idx, point))
        if len(self.points) > self.max_points and self.depth < self.max_depth:
            self._split()
        return True

    def _split(self) -> None:
        """将当前节点拆分为四个子节点，并重新分配已有点。"""
        minx, miny, maxx, maxy = self.bounds
        midx = (minx + maxx) / 2.0
        midy = (miny + maxy) / 2.0
        if midx <= minx or midx >= maxx or midy <= miny or midy >= maxy:
            return
        self.children = [
            QuadTreeNode((minx, miny, midx, midy), depth=self.depth + 1,
                         max_depth=self.max_depth, max_points=self.max_points),
            QuadTreeNode((midx, miny, maxx, midy), depth=self.depth + 1,
                         max_depth=self.max_depth, max_points=self.max_points),
            QuadTreeNode((minx, midy, midx, maxy), depth=self.depth + 1,
                         max_depth=self.max_depth, max_points=self.max_points),
            QuadTreeNode((midx, midy, maxx, maxy), depth=self.depth + 1,
                         max_depth=self.max_depth, max_points=self.max_points),
        ]
        old_points = self.points
        self.points = []
        for idx, point in old_points:
            inserted = any(child.insert(idx, point) for child in self.children)
            if not inserted:
                self.points.append((idx, point))

    def query(self, bbox: tuple[float, float, float, float], out: set[int]) -> None:
        """查询 bbox 范围内的点索引集合。"""
        if not bbox_intersects(self.bounds, bbox):
            return
        for idx, point in self.points:
            if bbox_contains_point(bbox, point):
                out.add(idx)
        for child in self.children:
            child.query(bbox, out)


class QuadTreeIndex:
    """基于 CAD 对象中心点构建四叉树索引，加速区域生长时的邻近对象查询。"""

    def __init__(
            self,
            items: list[dict[str, Any]],
            bounds: tuple[float, float, float, float],
    ) -> None:
        """根据全局 bbox 初始化四叉树，并插入所有对象中心点。"""
        pad_x = max(1.0, bbox_width(bounds) * 0.001)
        pad_y = max(1.0, bbox_height(bounds) * 0.001)
        self.root = QuadTreeNode(bbox_inflate(bounds, pad_x, pad_y))
        for idx, item in enumerate(items):
            self.root.insert(idx, (float(item["cx"]), float(item["cy"])))

    def query(self, bbox: tuple[float, float, float, float]) -> set[int]:
        """返回查询 bbox 内的对象索引集合。"""
        out: set[int] = set()
        self.root.query(bbox, out)
        return out


def is_negative_plan_title(value: Any) -> bool:
    """判断图名是否属于剖面、详图、系统图等非平面图标题。"""
    text = compact_text(value)
    if not text:
        return False
    return text_has_any(text, NON_PLAN_TITLE_TERMS) or any(phrase in text for phrase in TITLE_NEGATIVE_PHRASES)


def plan_title_seed_for_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """从文本对象中提取可作为区域生长种子的平面图标题。"""
    candidate = floor_title_candidate_for_item(item)
    if not candidate:
        return None
    title, mentions, weight = candidate
    if is_negative_plan_title(title):
        return None
    if not text_has_any(title, PLAN_TITLE_TERMS):
        return None
    floor_id, floor_evidence = mentions[0]
    return {
        "item": item,
        "seed_object_id": item.get("object_id", ""),
        "seed_text": title,
        "floor_id": floor_id,
        "floor_name": floor_label(floor_id),
        "floor_evidence": floor_evidence,
        "weight": weight,
        "center": (float(item["cx"]), float(item["cy"])),
    }


def growth_candidate_item(item: dict[str, Any], global_bbox: tuple[float, float, float, float]) -> bool:
    """判断对象是否可参与基于种子点的 sheet 区域生长。"""
    if item.get("area", 0.0) <= 0:
        return False
    global_area = bbox_area(global_bbox)
    if global_area > 0 and item["area"] > global_area * 0.35:
        return False
    entity = str(item.get("entity_type", "")).upper()
    geometry = str(item.get("geometry_kind", "")).lower()
    if entity in {"DIMENSION", "LEADER", "MLEADER"} or geometry == "annotation_dimension":
        return False
    return True


def nearest_seed_index(point: tuple[float, float], seed_centers: list[tuple[float, float]]) -> int:
    """在多个种子中心中寻找距离指定点最近的种子索引。"""
    best_idx = 0
    best_distance = float("inf")
    px, py = point
    for idx, (sx, sy) in enumerate(seed_centers):
        distance = (px - sx) * (px - sx) + (py - sy) * (py - sy)
        if distance < best_distance:
            best_idx = idx
            best_distance = distance
    return best_idx


def containing_frame_for_seed(
        seed_center: tuple[float, float],
        seed_centers: list[tuple[float, float]],
        frame_sheets: list[dict[str, Any]],
        global_bbox: tuple[float, float, float, float],
) -> dict[str, Any] | None:
    """查找包含指定种子点的已检测图框，用于限制区域生长范围。"""
    global_area = bbox_area(global_bbox)
    matches: list[dict[str, Any]] = []
    for frame in frame_sheets:
        bbox = frame["bbox"]
        if not bbox_contains_point(bbox, seed_center):
            continue
        if global_area > 0 and bbox_area(bbox) > global_area * 0.88:
            continue
        seed_count = sum(1 for center in seed_centers if bbox_contains_point(bbox, center))
        if seed_count > 1:
            continue
        matches.append(frame)
    if not matches:
        return None
    matches.sort(key=lambda row: (bbox_area(row["bbox"]), -float(row.get("confidence", 0.0))))
    return matches[0]


def grow_seed_bbox(
        seed: dict[str, Any],
        seed_index: int,
        seed_centers: list[tuple[float, float]],
        growth_items: list[dict[str, Any]],
        spatial_index: QuadTreeIndex,
        global_bbox: tuple[float, float, float, float],
) -> tuple[tuple[float, float, float, float], int, int, str]:
    """以图名种子为起点，结合四叉树索引向外迭代生长 sheet bbox。"""
    global_width = max(1.0, bbox_width(global_bbox))
    global_height = max(1.0, bbox_height(global_bbox))
    global_area = max(1.0, bbox_area(global_bbox))
    seed_item = seed["item"]
    current = bbox_inflate(seed_item["bbox"], global_width * 0.01, global_height * 0.01)
    assigned: set[int] = set()
    stop_reason = "max_rounds"
    stable_rounds = 0

    for round_idx in range(1, 15):
        pad_x = max(global_width * 0.012, bbox_width(current) * 0.08)
        pad_y = max(global_height * 0.012, bbox_height(current) * 0.08)
        probe = bbox_inflate(current, pad_x, pad_y)
        nearby = spatial_index.query(probe)
        new_indices: set[int] = set()
        for idx in nearby:
            if idx in assigned:
                continue
            item = growth_items[idx]
            center = (float(item["cx"]), float(item["cy"]))
            if nearest_seed_index(center, seed_centers) != seed_index:
                continue
            if bbox_point_distance(current, center) > max(global_width, global_height) * 0.35:
                continue
            new_indices.add(idx)

        if not new_indices:
            stable_rounds += 1
            if stable_rounds >= 2:
                stop_reason = "no_new_objects"
                return current, len(assigned), round_idx, stop_reason
            current = probe
            continue

        old_area = max(1.0, bbox_area(current))
        assigned.update(new_indices)
        boxes = [seed_item["bbox"]] + [growth_items[idx]["bbox"] for idx in assigned]
        candidate = bbox_union(boxes)
        candidate = bbox_inflate(candidate, global_width * 0.006, global_height * 0.006)
        candidate_area = bbox_area(candidate)
        if candidate_area > global_area * 0.70:
            stop_reason = "global_area_limit"
            return current, len(assigned), round_idx, stop_reason
        aspect = bbox_width(candidate) / max(1.0, bbox_height(candidate))
        if aspect > 18.0 or aspect < 0.06:
            stop_reason = "abnormal_aspect"
            return current, len(assigned), round_idx, stop_reason

        growth_ratio = (candidate_area - old_area) / old_area
        current = candidate
        if growth_ratio < 0.02 and len(new_indices) < 6:
            stable_rounds += 1
            if stable_rounds >= 2:
                stop_reason = "density_drop_and_bbox_stable"
                return current, len(assigned), round_idx, stop_reason
        else:
            stable_rounds = 0

    return current, len(assigned), 14, stop_reason


def detect_plan_title_seed_growth_sheets(
        items: list[dict[str, Any]],
        global_bbox: tuple[float, float, float, float],
        frame_sheets: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    frame_sheets = frame_sheets or []
    if frame_sheets:
        return []
    seeds = [seed for item in items if (seed := plan_title_seed_for_item(item))]
    if not seeds:
        return []

    seed_centers = [seed["center"] for seed in seeds]
    growth_items = [item for item in items if growth_candidate_item(item, global_bbox)]
    if not growth_items:
        return []
    spatial_index = QuadTreeIndex(growth_items, global_bbox)
    global_area = max(1.0, bbox_area(global_bbox))
    sheets: list[dict[str, Any]] = []
    for seed_index, seed in enumerate(seeds):
        grown_bbox, object_count, rounds, stop_reason = grow_seed_bbox(
            seed,
            seed_index,
            seed_centers,
            growth_items,
            spatial_index,
            global_bbox,
        )
        final_bbox = grown_bbox
        method = "plan_title_seed_region_growing"
        matched_frame = containing_frame_for_seed(seed["center"], seed_centers, frame_sheets, global_bbox)
        if matched_frame:
            final_bbox = matched_frame["bbox"]
            method = "plan_title_seed_region_growing_frame_fused"

        area = bbox_area(final_bbox)
        if area < global_area * 0.001:
            continue
        if area > global_area * 0.88:
            continue
        if not matched_frame and object_count < 30:
            continue
        confidence = 0.74 + min(0.12, object_count / 3500.0) + min(0.05, float(seed["weight"]) / 120.0)
        if matched_frame:
            confidence = max(confidence, min(0.94, float(matched_frame.get("confidence", 0.0)) + 0.03))
        sheets.append(
            {
                "bbox": final_bbox,
                "method": method,
                "confidence": round(min(0.94, confidence), 3),
                "score": float(seed["weight"]) + min(object_count, 5000) / 100.0,
                "seed_object_id": seed["seed_object_id"],
                "seed_text": seed["seed_text"],
                "seed_cx": seed["center"][0],
                "seed_cy": seed["center"][1],
                "growth_rounds": rounds,
                "growth_object_count": object_count,
                "growth_stop_reason": stop_reason,
                "excluded_non_plan": False,
                "non_plan_reason": "",
                "sheet_title": seed["seed_text"],
                "seed_floor_id": seed["floor_id"],
                "seed_floor_name": seed["floor_name"],
                "seed_floor_evidence": seed["floor_evidence"],
                "seed_floor_confidence": 0.82,
                "evidence": [
                    f"seed_object_id={seed['seed_object_id']}",
                    f"seed_text={seed['seed_text']}",
                    f"seed_floor={seed['floor_id']}",
                    f"growth_object_count={object_count}",
                    f"growth_stop_reason={stop_reason}",
                ],
            }
        )

    sheets.sort(key=lambda row: (row.get("confidence", 0.0), bbox_area(row["bbox"])), reverse=True)
    kept: list[dict[str, Any]] = []
    for cand in sheets:
        if any(bbox_iou(cand["bbox"], old["bbox"]) >= 0.62 for old in kept):
            continue
        kept.append(cand)
    kept.sort(key=lambda row: (-bbox_center(row["bbox"])[1], bbox_center(row["bbox"])[0]))
    return kept


def merge_sheet_candidates(
        frame_sheets: list[dict[str, Any]],
        seed_growth_sheets: list[dict[str, Any]],
        cluster_sheets: list[dict[str, Any]],
) -> list[
    dict[str, Any]]:
    """合并图框、标题种子生长、密度聚类三类 sheet 候选，并执行重叠去重。"""
    if frame_sheets:
        candidates = list(frame_sheets)
    elif seed_growth_sheets:
        candidates = list(seed_growth_sheets)
    else:
        candidates = list(cluster_sheets)

    method_priority = {
        "frame_candidate": 3.0,
        "plan_title_seed_region_growing_frame_fused": 2.8,
        "plan_title_seed_region_growing": 2.5,
        "density_cluster": 1.0,
    }
    candidates.sort(
        key=lambda row: (
            method_priority.get(str(row.get("method", "")), 0.0),
            row.get("confidence", 0),
            bbox_area(row["bbox"]),
        ),
        reverse=True,
    )
    seed_points = [
        (float(row.get("seed_cx")), float(row.get("seed_cy")))
        for row in seed_growth_sheets
        if row.get("seed_cx") not in ("", None) and row.get("seed_cy") not in ("", None)
    ]

    def seed_count_inside(bbox: tuple[float, float, float, float]) -> int:
        """seed_count_inside 的辅助函数。"""
        return sum(1 for point in seed_points if bbox_contains_point(bbox, point))

    kept: list[dict[str, Any]] = []
    for cand in candidates:
        cand_bbox = cand["bbox"]
        cand_method = str(cand.get("method", ""))
        duplicate = False
        for old in kept:
            old_bbox = old["bbox"]
            old_method = str(old.get("method", ""))
            if bbox_iou(cand_bbox, old_bbox) >= 0.55:
                duplicate = True
                break
            if cand_method.startswith("plan_title_seed") and old_method == "frame_candidate":
                cand_center = bbox_center(cand_bbox)
                old_seed_count = seed_count_inside(old_bbox)
                overlap_share = bbox_intersection_area(cand_bbox, old_bbox) / max(1.0, bbox_area(cand_bbox))
                if old_seed_count <= 1 and (
                        bbox_contains_point(old_bbox, cand_center)
                        or bbox_containment(cand_bbox, old_bbox) >= 0.55
                        or overlap_share >= 0.10
                ):
                    duplicate = True
                    break
        if duplicate:
            continue
        kept.append(cand)
    kept.sort(key=lambda row: (-bbox_center(row["bbox"])[1], bbox_center(row["bbox"])[0]))
    return kept


def dedup_sheets_by_floor_id(sheets: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    floor_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    passthrough: list[dict[str, Any]] = []
    for sheet in sheets:
        floor_id = str(sheet.get("floor_id") or "").strip()
        if floor_id and floor_id.upper() != "UNKNOWN":
            floor_groups[floor_id].append(sheet)
        else:
            passthrough.append(sheet)

    method_priority = {
        "frame_candidate": 100.0,
        "plan_title_seed_region_growing_frame_fused": 85.0,
        "plan_title_seed_region_growing": 72.0,
        "density_cluster": 45.0,
    }
    source_priority = {
        "sheet_title": 120.0,
        "plan_title_seed": 110.0,
        "full_sheet_vote": 55.0,
    }

    selected: list[dict[str, Any]] = []
    removed_count = 0
    for floor_id, group in floor_groups.items():
        if len(group) == 1:
            selected.append(group[0])
            continue

        areas = sorted(max(1.0, bbox_area(sheet["bbox"])) for sheet in group)
        median_area = areas[len(areas) // 2]

        def score(sheet: dict[str, Any]) -> tuple[float, float, float, float]:
            """score 的辅助函数。"""
            area = max(1.0, bbox_area(sheet["bbox"]))
            area_ratio = area / max(1.0, median_area)
            area_score = max(0.0, 40.0 - min(40.0, abs(math.log(area_ratio)) * 35.0))
            confidence = float(sheet.get("floor_confidence", 0.0) or 0.0)
            object_count = int(sheet.get("object_count", 0) or 0)
            method = str(sheet.get("method", ""))
            floor_source = str(sheet.get("floor_source", ""))
            usable_score = 1000.0 if bool(sheet.get("path_planning_usable")) else 0.0
            total = (
                    usable_score
                    + source_priority.get(floor_source, 0.0)
                    + method_priority.get(method, 0.0)
                    + min(object_count, 12000) / 120.0
                    + area_score
                    + confidence * 80.0
            )
            return total, confidence, float(object_count), area

        best = max(group, key=score)
        best.setdefault("dedup_floor_group_size", len(group))
        best.setdefault("dedup_selected_reason", "best_floor_sheet_by_semantic_score")
        selected.append(best)
        removed_count += len(group) - 1

    merged = passthrough + selected
    merged.sort(key=lambda row: (-bbox_center(row["bbox"])[1], bbox_center(row["bbox"])[0]))
    return merged, removed_count


def floor_mentions_for_item(item: dict[str, Any]) -> list[tuple[str, str, str, float]]:
    """从单个 item 的文本字段中提取楼层 mentions。"""
    values = [
        ("text", item.get("norm_text") or item.get("raw_text"), 4.0),
        ("parent_block", item.get("parent_block_name"), 3.0),
        ("block_path", item.get("block_path"), 2.0),
        ("layer", item.get("layer"), 1.0),
    ]
    hits: list[tuple[str, str, str, float]] = []
    for source, value, base_weight in values:
        text = str(value or "")
        if not text:
            continue
        if text_has_any(text, NON_PLAN_TEXT_TERMS) and not text_has_any(text, FLOOR_CONTEXT_TERMS):
            base_weight *= 0.45
        if text_has_any(text, FLOOR_CONTEXT_TERMS):
            base_weight += 0.8
        for floor_id, evidence in detect_floor_mentions(text):
            weight = base_weight
            if floor_id == "ROOF" and source == "text" and "平面图" not in compact_text(text):
                weight *= 0.25
            hits.append((floor_id, source, evidence, weight))
    return hits


def infer_sheet_floor(sheet: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    """对单个 sheet 进行楼层推断，优先使用标题栏/图名区域证据。"""
    bbox = sheet["bbox"]
    title_scores: Counter[str] = Counter()
    title_evidence_by_floor: dict[str, list[str]] = defaultdict(list)
    title_text_by_floor: dict[str, tuple[float, str]] = {}
    object_count = 0
    text_count = 0

    for item in items:
        if not bbox_contains_point(bbox, (item["cx"], item["cy"])):
            continue
        object_count += 1
        if item.get("norm_text") or item.get("raw_text"):
            text_count += 1
        title_candidate = sheet_title_candidate_for_item(item, bbox)
        if title_candidate:
            title_text, mentions, title_weight = title_candidate
            for floor_id, evidence in mentions:
                title_scores[floor_id] += title_weight
                sample = f"title:{title_text}"
                if sample not in title_evidence_by_floor[floor_id] and len(title_evidence_by_floor[floor_id]) < 8:
                    title_evidence_by_floor[floor_id].append(sample)
                old = title_text_by_floor.get(floor_id)
                if old is None or title_weight > old[0]:
                    title_text_by_floor[floor_id] = (title_weight, title_text)

    if title_scores:
        total = sum(title_scores.values())
        ranked = title_scores.most_common()
        best_floor, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        confidence = min(
            0.98,
            max(
                0.72,
                (best_score / total) * 0.62
                + min(best_score / 80, 0.25)
                + min((best_score - second_score) / max(best_score, 1), 1.0) * 0.11,
            ),
        )
        sheet_title = title_text_by_floor.get(best_floor, (0.0, ""))[1]
        return {
            "floor_id": best_floor,
            "floor_name": floor_label(best_floor),
            "floor_confidence": round(confidence, 3),
            "floor_source": "sheet_title",
            "sheet_title": sheet_title,
            "floor_candidates": [
                {
                    "floor_id": floor_id,
                    "floor_name": floor_label(floor_id),
                    "score": round(score, 2),
                    "evidence": title_evidence_by_floor.get(floor_id, [])[:6],
                }
                for floor_id, score in ranked[:6]
            ],
            "floor_evidence": title_evidence_by_floor.get(best_floor, [])[:8],
            "object_count": object_count,
            "text_count": text_count,
        }

    return {
        "floor_id": "",
        "floor_name": "",
        "floor_confidence": 0.0,
        "floor_source": "",
        "sheet_title": "",
        "floor_candidates": [],
        "floor_evidence": ["missing_titlebar_plan_title"] if sheet.get("method") == "frame_candidate" else [],
        "object_count": object_count,
        "text_count": text_count,
    }


def semantic_values_for_item(item: dict[str, Any]) -> list[tuple[str, str, float]]:
    """收集 item 的图层、块名、文本等语义字段，用于 sheet 类型判断。"""
    raw_text = item.get("norm_text") or item.get("raw_text")
    return [
        ("text", str(raw_text or ""), 4.0),
        ("parent_block", str(item.get("parent_block_name") or ""), 2.5),
        ("block_path", str(item.get("block_path") or ""), 2.0),
        ("layer", str(item.get("layer") or ""), 1.2),
    ]


def infer_sheet_semantic(sheet: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    bbox = sheet["bbox"]
    plan_score = 0.0
    plan_evidence: list[str] = []
    strong_plan_evidence: list[str] = []
    non_plan_evidence: list[str] = []
    seen_values: set[tuple[str, str]] = set()
    floor_id = sheet.get("floor_id", "")
    floor_confidence = float(sheet.get("floor_confidence", 0.0) or 0.0)
    object_count = int(sheet.get("object_count", 0) or 0)
    text_count = int(sheet.get("text_count", 0) or 0)

    def add_evidence(bucket: list[str], sample: str) -> None:
        """add_evidence 的辅助函数。"""
        if sample not in bucket and len(bucket) < 8:
            bucket.append(sample)

    for item in items:
        if not bbox_contains_point(bbox, (item["cx"], item["cy"])):
            continue
        text_title = normalize_title_text(item.get("norm_text") or item.get("raw_text"))
        if not item_has_titlebar_meta(item) and not (
                text_title and is_titlebar_area_item(item, bbox, text_title)
        ):
            continue
        for source, value, base_weight in semantic_values_for_item(item):
            text = compact_text(value).upper()
            if not text:
                continue
            key = (source, text)
            if key in seen_values:
                continue
            seen_values.add(key)
            weight = base_weight
            if len(text) > 80:
                weight *= 0.35

            for term in PLAN_TITLE_TERMS:
                if str(term).upper() in text:
                    plan_score += weight
                    add_evidence(plan_evidence, f"{source}:{term}")
                    if "平面图" in str(term) or "平面图" in str(value or ""):
                        add_evidence(strong_plan_evidence, f"{source}:{term}")

            if source in {"text", "parent_block", "block_path"} and is_negative_plan_title(value):
                add_evidence(non_plan_evidence, f"{source}:negative_plan_title")

    if floor_confidence >= 0.90:
        return {
            "sheet_semantic_role": "path_plan_floor_plan",
            "sheet_semantic_name": "selected_by_high_floor_confidence",
            "sheet_semantic_confidence": floor_confidence,
            "path_planning_usable": True,
            "sheet_semantic_evidence": plan_evidence[:8] or sheet.get("floor_evidence", [])[:4],
        }

    if floor_confidence <= 0.0 or not floor_id or object_count < 500 or text_count < 20:
        return {
            "sheet_semantic_role": "excluded_zero_confidence",
            "sheet_semantic_name": "excluded_by_zero_or_invalid_floor_confidence",
            "sheet_semantic_confidence": 0.0,
            "path_planning_usable": False,
            "sheet_semantic_evidence": [],
        }

    has_strong_plan_title = bool(strong_plan_evidence)
    has_plan_title = bool(plan_evidence) or plan_score > 0.0
    if has_strong_plan_title or (has_plan_title and not non_plan_evidence):
        return {
            "sheet_semantic_role": "path_plan_floor_plan",
            "sheet_semantic_name": "selected_by_plan_title",
            "sheet_semantic_confidence": round(min(0.89, 0.45 + floor_confidence * 0.25 + min(plan_score / 25.0, 0.18)),
                                               3),
            "path_planning_usable": True,
            "sheet_semantic_evidence": strong_plan_evidence[:8] or plan_evidence[:8],
        }

    return {
        "sheet_semantic_role": "excluded_non_plan_or_no_plan_title",
        "sheet_semantic_name": "excluded_by_non_plan_or_missing_plan_title",
        "sheet_semantic_confidence": 0.0,
        "path_planning_usable": False,
        "sheet_semantic_evidence": non_plan_evidence[:8] or plan_evidence[:8],
    }


def output_rows(sheets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将内部 sheet 结构转换为 CSV/JSON 统一输出行。"""
    rows: list[dict[str, Any]] = []
    for idx, sheet in enumerate(sheets, start=1):
        minx, miny, maxx, maxy = sheet["bbox"]
        raw_region_bbox = sheet.get("inspection_region_bbox") or sheet["bbox"]
        try:
            region_minx, region_miny, region_maxx, region_maxy = (
                float(raw_region_bbox[0]),
                float(raw_region_bbox[1]),
                float(raw_region_bbox[2]),
                float(raw_region_bbox[3]),
            )
        except Exception:
            region_minx, region_miny, region_maxx, region_maxy = minx, miny, maxx, maxy
        floor_id = sheet.get("floor_id", "")
        floor_confidence = float(sheet.get("floor_confidence", 0.0) or 0.0)
        object_count = int(sheet.get("object_count", 0) or 0)
        text_count = int(sheet.get("text_count", 0) or 0)
        path_planning_usable = bool(sheet.get("path_planning_usable", False))
        semantic_role = sheet.get("sheet_semantic_role", "unknown_or_non_floor")
        semantic_name = sheet.get("sheet_semantic_name", "")
        semantic_confidence = float(sheet.get("sheet_semantic_confidence", 0.0) or 0.0)
        rows.append(
            {
                "sheet_id": f"SHEET_{idx:03d}",
                "floor_id": floor_id,
                "floor_name": sheet.get("floor_name", ""),
                "sheet_title": sheet.get("sheet_title", ""),
                "floor_source": sheet.get("floor_source", ""),
                "floor_confidence": floor_confidence,
                "sheet_semantic_role": semantic_role,
                "sheet_semantic_name": semantic_name,
                "sheet_semantic_confidence": semantic_confidence,
                "path_planning_usable": path_planning_usable,
                "sheet_role": "path_planning_floor_plan" if path_planning_usable else semantic_role,
                "needs_floor_review": path_planning_usable and floor_confidence < 0.75,
                "method": sheet.get("method", ""),
                "sheet_confidence": round(float(sheet.get("confidence", 0.0)), 3),
                "object_count": object_count,
                "text_count": text_count,
                "seed_object_id": sheet.get("seed_object_id", ""),
                "seed_text": sheet.get("seed_text", ""),
                "seed_floor_id": sheet.get("seed_floor_id", ""),
                "seed_floor_name": sheet.get("seed_floor_name", ""),
                "seed_floor_evidence": sheet.get("seed_floor_evidence", ""),
                "seed_floor_confidence": sheet.get("seed_floor_confidence", ""),
                "growth_rounds": sheet.get("growth_rounds", ""),
                "growth_object_count": sheet.get("growth_object_count", ""),
                "growth_stop_reason": sheet.get("growth_stop_reason", ""),
                "excluded_non_plan": sheet.get("excluded_non_plan", False),
                "non_plan_reason": sheet.get("non_plan_reason", ""),
                "dedup_floor_group_size": sheet.get("dedup_floor_group_size", ""),
                "dedup_selected_reason": sheet.get("dedup_selected_reason", ""),
                "bbox_minx": minx,
                "bbox_miny": miny,
                "bbox_maxx": maxx,
                "bbox_maxy": maxy,
                "inspection_region_source": sheet.get("inspection_region_source", ""),
                "inspection_region_confidence": sheet.get("inspection_region_confidence", ""),
                "inspection_region_object_count": sheet.get("inspection_region_object_count", ""),
                "inspection_region_minx": region_minx,
                "inspection_region_miny": region_miny,
                "inspection_region_maxx": region_maxx,
                "inspection_region_maxy": region_maxy,
                "inspection_region_evidence": sheet.get("inspection_region_evidence", ""),
                "inspection_regions_json": json.dumps(sheet.get("inspection_regions", []), ensure_ascii=False),
                "evidence": " | ".join(sheet.get("floor_evidence", [])[:6]),
                "semantic_evidence": " | ".join(sheet.get("sheet_semantic_evidence", [])[:6]),
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """按 OUTPUT_FIELDS 写出 drawing_sheets_floors.csv。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in OUTPUT_FIELDS})


def unique_output_path(path: Path) -> Path:
    """Return a non-existing output path by appending a suffix when needed."""
    path = Path(path)
    if not path.exists():
        return path
    for idx in range(2, 100):
        candidate = path.with_name(f"{path.stem}_v{idx}{path.suffix}")
        if not candidate.exists():
            return candidate
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return path.with_name(f"{path.stem}_{stamp}{path.suffix}")


def add_layer(doc: Any, name: str, color: int) -> None:
    """Create a DXF layer if it does not already exist."""
    try:
        if name in doc.layers:
            return
    except Exception:
        pass
    doc.layers.add(name=name, color=color)


def overlay_bbox_from_row(row: dict[str, Any]) -> BBox | None:
    """Read bbox from either JSON-style fields or flattened CSV fields."""
    bbox = row.get("bbox")
    if isinstance(bbox, str):
        try:
            bbox = json.loads(bbox)
        except Exception:
            bbox = None
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        try:
            return (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        except (TypeError, ValueError):
            return None

    keys = ("bbox_minx", "bbox_miny", "bbox_maxx", "bbox_maxy")
    if all(row.get(key) not in ("", None) for key in keys):
        try:
            return tuple(float(row[key]) for key in keys)  # type: ignore[return-value]
        except (TypeError, ValueError):
            return None
    return None


def inspection_region_bboxes_from_row(row: dict[str, Any]) -> list[BBox]:
    """Collect inspection-region boxes from current and legacy row shapes."""
    boxes: list[BBox] = []
    regions = row.get("inspection_regions")
    if isinstance(regions, str):
        try:
            regions = json.loads(regions)
        except Exception:
            regions = None
    if isinstance(regions, list):
        for region in regions:
            if not isinstance(region, dict):
                continue
            box = overlay_bbox_from_row(region)
            if box:
                boxes.append(box)

    direct_bbox = row.get("inspection_region_bbox")
    if isinstance(direct_bbox, str):
        try:
            direct_bbox = json.loads(direct_bbox)
        except Exception:
            direct_bbox = None
    if isinstance(direct_bbox, (list, tuple)) and len(direct_bbox) >= 4:
        try:
            boxes.append(
                (
                    float(direct_bbox[0]),
                    float(direct_bbox[1]),
                    float(direct_bbox[2]),
                    float(direct_bbox[3]),
                )
            )
        except (TypeError, ValueError):
            pass

    keys = (
        "inspection_region_minx",
        "inspection_region_miny",
        "inspection_region_maxx",
        "inspection_region_maxy",
    )
    if all(row.get(key) not in ("", None) for key in keys):
        try:
            boxes.append(tuple(float(row[key]) for key in keys))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            pass

    unique: list[BBox] = []
    seen: set[tuple[int, int, int, int]] = set()
    for box in boxes:
        key = tuple(round(value) for value in box)
        if key not in seen and bbox_area(box) > 0:
            seen.add(key)
            unique.append(box)
    return unique


def add_bbox_polyline(msp: Any, bbox: BBox, layer: str, color: int) -> None:
    minx, miny, maxx, maxy = bbox
    points = [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)]
    msp.add_lwpolyline(points, dxfattribs={"layer": layer, "color": color})


def write_overlay_dxf(
    input_dxf: Path,
    output_dxf: Path,
    rows: list[dict[str, Any]],
    *,
    include_non_path: bool = False,
) -> None:
    """Write a legacy-compatible DXF overlay for detected sheets and regions."""
    if ezdxf is None:
        raise RuntimeError("ezdxf is required to write DXF overlays.")

    doc = ezdxf.readfile(str(input_dxf))
    msp = doc.modelspace()
    add_layer(doc, SHEET_BOX_LAYER, 3)
    add_layer(doc, SHEET_LABEL_LAYER, 1)
    add_layer(doc, SHEET_NON_PATH_LAYER, 8)
    add_layer(doc, INSPECTION_REGION_LAYER, 1)
    add_layer(doc, INSPECTION_REGION_LABEL_LAYER, 1)

    for row in rows:
        bbox = overlay_bbox_from_row(row)
        if not bbox:
            continue
        usable = bool(row.get("path_planning_usable"))
        if not usable and not include_non_path:
            continue

        sheet_layer = SHEET_BOX_LAYER if usable else SHEET_NON_PATH_LAYER
        sheet_color = 3 if usable else 8
        add_bbox_polyline(msp, bbox, sheet_layer, sheet_color)

        minx, miny, maxx, maxy = bbox
        height = max((maxy - miny) * 0.018, 250.0)
        label_parts = [
            str(row.get("sheet_id") or ""),
            str(row.get("floor_name") or row.get("floor_id") or row.get("sheet_title") or "UNKNOWN"),
            str(row.get("sheet_semantic_role") or ""),
        ]
        label = " ".join(part for part in label_parts if part).strip()[:180]
        if label:
            text = msp.add_text(
                label,
                dxfattribs={"layer": SHEET_LABEL_LAYER, "color": sheet_color, "height": height},
            )
            text.dxf.insert = (minx, maxy + height * 0.8, 0)

        if usable:
            for idx, region_bbox in enumerate(inspection_region_bboxes_from_row(row), start=1):
                add_bbox_polyline(msp, region_bbox, INSPECTION_REGION_LAYER, 1)
                rminx, rminy, rmaxx, rmaxy = region_bbox
                region_text = msp.add_text(
                    f"{row.get('sheet_id') or ''} region {idx}",
                    dxfattribs={
                        "layer": INSPECTION_REGION_LABEL_LAYER,
                        "color": 1,
                        "height": max((rmaxy - rminy) * 0.015, 180.0),
                    },
                )
                region_text.dxf.insert = (rminx, rmaxy, 0)

    output_dxf.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(output_dxf))


def find_input_dxf(job_dir: Path) -> Path | None:
    """从 job/upload 目录中寻找原始 DXF 文件。"""
    upload = job_dir / "upload"
    candidates = sorted(upload.glob("*.dxf"))
    return candidates[0] if candidates else None


def strip_path_quotes(value: str) -> str:
    """清理命令行路径两端的引号。"""
    return str(value or "").strip().strip("\"'")


def default_direct_output_dir(input_dxf: Path) -> Path:
    """为直接输入 DXF 模式生成默认输出目录。"""
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    safe_stem = re.sub(r"[^0-9A-Za-z_\-.\u4e00-\u9fff]+", "_", input_dxf.stem).strip("_") or "drawing"
    return PROJECT_ROOT / "outputs" / "sheet_floor_detection" / f"{safe_stem}_{stamp}"


def run_inventory_agent(input_dxf: Path, inventory_dir: Path, *, force: bool = False) -> None:
    """调用 cad_vector_inventory_agent.py 生成 cad_object_inventory.csv；已有文件且未强制刷新时跳过。"""
    inventory_csv = inventory_dir / "cad_object_inventory.csv"
    if inventory_csv.exists() and not force:
        return

    agent_path = PROJECT_ROOT / "agents" / "cad_vector_inventory_agent.py"
    if not agent_path.exists():
        raise FileNotFoundError(agent_path)

    inventory_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(agent_path),
        "--input",
        str(input_dxf),
        "--output",
        str(inventory_dir),
    ]
    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True)


def detect_sheets_and_floors(inventory_dir: Path, *, grid_size: int = 180) -> dict[str, Any]:
    """主检测流程：加载 inventory、检测 sheet、推断楼层、判断语义、推断 inspection region、去重并汇总结果。"""
    # 1. 读取上游 inventory agent 生成的标准图元清单。
    items = load_inventory_items(inventory_dir)
    if not items:
        raise RuntimeError(f"No inventory items found in {inventory_dir}")

    # 2. 用全部有效图元 bbox 计算全局范围，作为图框和聚类检测的参照系。
    global_bbox = bbox_union([item["bbox"] for item in items])

    # 3. 优先基于图框/标题栏检测 sheet；只有图框失败时才启用 fallback。
    frame_sheets = detect_frame_sheets(items, global_bbox)
    if frame_sheets:
        seed_growth_sheets: list[dict[str, Any]] = []
        cluster_sheets: list[dict[str, Any]] = []
    else:
        seed_growth_sheets = detect_plan_title_seed_growth_sheets(items, global_bbox, frame_sheets)
        cluster_sheets = [] if seed_growth_sheets else detect_cluster_sheets(items, global_bbox, grid_size=grid_size)
    # 4. 合并三类 sheet 候选，并执行重叠去重。
    sheets = merge_sheet_candidates(frame_sheets, seed_growth_sheets, cluster_sheets)

    # 5. 对每个 sheet 补充楼层、语义角色和可规划区域。
    enriched: list[dict[str, Any]] = []
    for sheet in sheets:
        floor = infer_sheet_floor(sheet, items)
        sheet.update(floor)
        semantic = infer_sheet_semantic(sheet, items)
        sheet.update(semantic)
        inspection_region = infer_inspection_region(sheet, items)
        sheet.update(inspection_region)
        enriched.append(sheet)

    # 6. 同一楼层只保留一个最适合路径规划的 sheet，避免楼层冗余。
    enriched, floor_dedup_removed_count = dedup_sheets_by_floor_id(enriched)
    rows = output_rows(enriched)
    floor_counts = Counter(row["floor_id"] or "UNKNOWN" for row in rows)
    usable_floor_counts = Counter(row["floor_id"] or "UNKNOWN" for row in rows if row["path_planning_usable"])
    semantic_counts = Counter(row["sheet_semantic_role"] for row in rows)
    review_count = sum(1 for row in rows if row["needs_floor_review"])
    usable_count = sum(1 for row in rows if row["path_planning_usable"])
    return {
        "inventory_dir": str(inventory_dir.resolve()),
        "global_bbox": global_bbox,
        "item_count": len(items),
        "frame_candidate_count": len(frame_sheets),
        "plan_title_seed_candidate_count": len(seed_growth_sheets),
        "cluster_candidate_count": len(cluster_sheets),
        "floor_dedup_removed_count": floor_dedup_removed_count,
        "sheet_count": len(enriched),
        "floor_plan_candidate_count": usable_count,
        "unknown_or_non_floor_count": semantic_counts.get("unknown_or_non_floor", 0),
        "path_planning_usable_count": usable_count,
        "needs_floor_review_count": review_count,
        "floor_counts": dict(floor_counts),
        "path_planning_floor_counts": dict(usable_floor_counts),
        "sheet_semantic_counts": dict(semantic_counts),
        "sheets": [
            {
                **row,
                "bbox": [
                    row["bbox_minx"],
                    row["bbox_miny"],
                    row["bbox_maxx"],
                    row["bbox_maxy"],
                ],
                "inspection_region_bbox": [
                    row["inspection_region_minx"],
                    row["inspection_region_miny"],
                    row["inspection_region_maxx"],
                    row["inspection_region_maxy"],
                ],
                "inspection_regions": enriched[idx].get("inspection_regions", []),
                "floor_candidates": enriched[idx].get("floor_candidates", []),
                "sheet_evidence": enriched[idx].get("evidence", []),
                "sheet_semantic_evidence": enriched[idx].get("sheet_semantic_evidence", []),
            }
            for idx, row in enumerate(rows)
        ],
    }


def resolve_runtime_paths(args: argparse.Namespace) -> tuple[Path, Path, Path | None, str]:
    """解析运行模式，返回 inventory_dir、output_dir、input_dxf 和 mode。

    优先级：
    1. --dxf：直接输入 DXF，必要时先调用 inventory agent；
    2. --inventory-dir：直接复用已有 cad_object_inventory.csv；
    3. --job-id：读取 web/runtime/jobs/<job_id>/inventory；
    4. 无参数：优先使用本地默认 inventory，其次使用默认 DXF，最后才尝试 latest job。
    """
    input_dxf = Path(strip_path_quotes(args.input_dxf)).expanduser().resolve() if strip_path_quotes(args.input_dxf) else None
    inventory_arg = Path(strip_path_quotes(args.inventory_dir)).expanduser().resolve() if strip_path_quotes(args.inventory_dir) else None
    output_arg = Path(strip_path_quotes(args.output_dir)).expanduser().resolve() if strip_path_quotes(args.output_dir) else None

    if input_dxf is not None and not input_dxf.exists():
        raise FileNotFoundError(input_dxf)

    # 模式一：直接输入 DXF。适合 PyCharm 或命令行本地运行。
    if input_dxf is not None and not args.job_id:
        output_dir = output_arg if output_arg else default_direct_output_dir(input_dxf)
        inventory_dir = inventory_arg if inventory_arg else output_dir / "inventory"
        run_inventory_agent(input_dxf, inventory_dir, force=args.force_inventory)
        return inventory_dir, output_dir, input_dxf, "direct_dxf"

    # 模式二：直接指定已有 inventory。适合已经运行过 cad_vector_inventory_agent.py 的情况。
    if inventory_arg is not None and not args.job_id:
        inventory_csv = inventory_arg / "cad_object_inventory.csv"
        if not inventory_csv.exists():
            raise FileNotFoundError(f"缺少 inventory 文件: {inventory_csv}")
        output_dir = output_arg if output_arg else inventory_arg.parent / "sheet_floor_review"
        return inventory_arg, output_dir, input_dxf, "existing_inventory"

    # 模式三：指定 Web job。
    if args.job_id:
        job_dir = JOBS_ROOT / args.job_id
        inventory_dir = inventory_arg if inventory_arg else job_dir / "inventory"
        output_dir = output_arg if output_arg else job_dir / "review"
        if input_dxf is None:
            input_dxf = find_input_dxf(job_dir)
        return inventory_dir, output_dir, input_dxf, "web_job"

    # 模式四：无参数本地默认运行。优先复用常用 inventory，避免误找不存在的 web/runtime/jobs。
    default_inventory_csv = DEFAULT_LOCAL_INVENTORY_DIR / "cad_object_inventory.csv"
    if default_inventory_csv.exists():
        return DEFAULT_LOCAL_INVENTORY_DIR, DEFAULT_LOCAL_OUTPUT_DIR, DEFAULT_INPUT_DXF if DEFAULT_INPUT_DXF.exists() else None, "default_local_inventory"

    # 如果没有默认 inventory，但默认 DXF 存在，则自动生成 inventory。
    if DEFAULT_INPUT_DXF.exists():
        output_dir = output_arg if output_arg else default_direct_output_dir(DEFAULT_INPUT_DXF)
        inventory_dir = output_dir / "inventory"
        run_inventory_agent(DEFAULT_INPUT_DXF, inventory_dir, force=args.force_inventory)
        return inventory_dir, output_dir, DEFAULT_INPUT_DXF, "default_local_dxf"

    # 最后才尝试最新 Web job。
    job_dir = latest_job_dir()
    inventory_dir = job_dir / "inventory"
    output_dir = output_arg if output_arg else job_dir / "review"
    input_dxf = find_input_dxf(job_dir)
    return inventory_dir, output_dir, input_dxf, "latest_web_job"


def main() -> None:
    """命令行入口：解析参数、准备输入输出目录、执行检测并写出 JSON/CSV。"""
    parser = argparse.ArgumentParser(
        description="Detect drawing sheets and floor names from a DXF or CAD inventory outputs."
    )
    parser.add_argument("--job-id", default="", help="web/runtime/jobs job id。")
    parser.add_argument(
        "--inventory-dir",
        default="",
        help="已有 inventory 目录，目录下必须包含 cad_object_inventory.csv。",
    )
    parser.add_argument(
        "--dxf",
        "--input-dxf",
        dest="input_dxf",
        default="",
        help="输入 DXF 路径。传入后会自动调用 cad_vector_inventory_agent.py 生成 inventory。",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="输出目录。默认写到 inventory 同级的 sheet_floor_review，或 outputs/sheet_floor_detection。",
    )
    parser.add_argument("--grid-size", type=int, default=180, help="密度聚类网格大小。")
    parser.add_argument(
        "--force-inventory",
        action="store_true",
        help="即使 cad_object_inventory.csv 已存在，也重新生成 inventory。",
    )
    args = parser.parse_args()

    inventory_dir, output_dir, input_dxf, mode = resolve_runtime_paths(args)
    output_dir.mkdir(parents=True, exist_ok=True)

    result = detect_sheets_and_floors(inventory_dir, grid_size=args.grid_size)
    result["input_dxf"] = str(input_dxf) if input_dxf else ""
    result["output_dir"] = str(output_dir.resolve())
    result["run_mode"] = mode

    json_path = output_dir / "drawing_sheets_floors.json"
    csv_path = output_dir / "drawing_sheets_floors.csv"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(csv_path, result["sheets"])

    print(
        json.dumps(
            {
                "run_mode": mode,
                "inventory_dir": str(inventory_dir.resolve()),
                "output_dir": str(output_dir.resolve()),
                "json": str(json_path.resolve()),
                "csv": str(csv_path.resolve()),
                "sheet_count": result["sheet_count"],
                "floor_plan_candidate_count": result["floor_plan_candidate_count"],
                "path_planning_usable_count": result["path_planning_usable_count"],
                "unknown_or_non_floor_count": result["unknown_or_non_floor_count"],
                "needs_floor_review_count": result["needs_floor_review_count"],
                "floor_counts": result["floor_counts"],
                "path_planning_floor_counts": result["path_planning_floor_counts"],
                "sheet_semantic_counts": result["sheet_semantic_counts"],
                "frame_candidate_count": result["frame_candidate_count"],
                "plan_title_seed_candidate_count": result["plan_title_seed_candidate_count"],
                "cluster_candidate_count": result["cluster_candidate_count"],
                "floor_dedup_removed_count": result["floor_dedup_removed_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
