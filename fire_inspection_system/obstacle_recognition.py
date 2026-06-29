from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from build_region_inspection_inventory import (  # type: ignore
    assign_region,
    load_regions,
    normalize_value,
    row_bbox,
    safe_float,
)

FULL_INVENTORY_FILE = "cad_object_inventory.csv"
GEOMETRY_INVENTORY_FILE = "cad_geometry_inventory.csv"

OBSTACLE_OUTPUT_FIELDS = [
    "obstacle_id",
    "sheet_id",
    "parent_sheet_id",
    "floor_id",
    "floor_name",
    "inspection_region_id",
    "obstacle_type",
    "confidence",
    "reason",
    "geometry_source",
    "object_id",
    "handle",
    "source",
    "entity_type",
    "geometry_kind",
    "is_closed",
    "layer",
    "parent_block_name",
    "block_path",
    "bbox_minx",
    "bbox_miny",
    "bbox_maxx",
    "bbox_maxy",
    "bbox_area",
]

WALL_TERMS = (
    "墙",
    "墙体",
    "剪力墙",
    "结构墙",
    "承重墙",
    "填充墙",
    "砼墙",
    "混凝土墙",
    "隔墙",
    "挡墙",
    "WALL",
    "SHEARWALL",
    "CONC",
)

COLUMN_TERMS = (
    "柱",
    "柱体",
    "结构柱",
    "框架柱",
    "构造柱",
    "COLUMN",
    "PILLAR",
    "COLU",
)

# 已删除“窗户作为障碍物”的识别逻辑：不再定义 WINDOW_TERMS，窗线不会输出为 obstacle_type=window。
# “门窗”层中的门扇圆弧仍仅用于门洞无障碍区，不参与障碍物输出。

STRUCTURE_LAYER_HINTS = (
    "建筑",
    "结构",
    "墙",
    "柱",
    "WALL",
    "COLUMN",
    "CONC",
    "SHEAR",
    "A-WALL",
)

NEGATIVE_LAYER_TERMS = (
    "TEXT",
    "DIMS",
    "DIM",
    "AXIS",
    "ANNO",
    "NOTE",
    "TITLE",
    "GRID",
    "图框",
    "图签",
    "标题",
    "说明",
    "图例",
    "材料表",
    "设备表",
    "参数",
    "LABEL",
    "轴网",
    "轴线",
    "辅助线",
    "尺寸",
    "标注",
    "剖面",
    "详图",
    "大样",
    "索引",
    "编号",
    "不出图",
    "非检查",
    "预制区",
    "非预制",
    "留洞",
    "洞口",
    "预留洞",
    "开洞",
    "孔洞",
    "OPENING",
    "HOLE",
    "PUB_TEXT",
    "TK_LABEL",
    "PMSHEET",
)

PASSAGE_OR_NON_OBSTACLE_TERMS = (
    "门",
    "门洞",
    "DOOR",
    "安全出口",
    "疏散",
    "楼梯",
    "STAIR",
    "电梯",
    "ELEV",
    "LIFT",
    "车位",
    "PARK",
)

DOOR_CLEARANCE_TERMS = (
    "门洞",
    "防火门",
    "门槛",
    "DOOR",
    "FHM",
)

SOLID_OBSTACLE_LAYER_HINTS = (
    "ELEV",
    "WIND",
    "SHAFT",
    "OPEN",
    "井",
    "设备",
    "机房",
)

TEXT_ENTITY_TYPES = {"TEXT", "MTEXT", "ATTRIB"}
LINE_ENTITY_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE"}
AREA_ENTITY_TYPES = {"HATCH", "SOLID", "TRACE", "REGION"}
ROUND_ENTITY_TYPES = {"CIRCLE", "ELLIPSE"}
ARC_ENTITY_TYPES = {"ARC"}
INSERT_ENTITY_TYPES = {"INSERT"}

DEFAULT_WALL_BUFFER = 50.0
DOOR_MASK_PADDING = 150.0
DOOR_MASK_MAX_LONG_SIDE = 5200.0
DOOR_MASK_MAX_REGION_RATIO = 0.035
DOOR_SWING_MIN_ANGLE_DEG = 85.0
DOOR_SWING_MAX_ANGLE_DEG = 95.0
WALL_PAIR_MIN_DIST = 80.0
WALL_PAIR_MAX_DIST = 1200.0
WALL_PAIR_MIN_OVERLAP = 0.35
WALL_PAIR_MAX_ANGLE_DEG = 8.0
MIN_OBSTACLE_WIDTH = 20.0
MIN_OBSTACLE_HEIGHT = 20.0
MAX_SINGLE_OBSTACLE_REGION_RATIO = 0.45
# 项目约定：PUB_HATCH 是墙体填充图层；只要 HATCH 解析出闭合 polygon，就作为墙体面障碍物。
SUPPLEMENT_WALL_HATCH_LAYERS = {"PUB_HATCH"}
WALL_FILL_LAYER_HINTS = (
    "PUB_HATCH",
    "HATCH",
    "WALL",
    "A-WALL",
    "S-WALL",
    "CONC",
    "SHEAR",
)
SUPPLEMENT_HATCH_MIN_AREA = 10000.0
SUPPLEMENT_HATCH_MAX_AREA = 8000000.0
SUPPLEMENT_HATCH_MIN_LONG_SIDE = 800.0
SUPPLEMENT_HATCH_MAX_THICKNESS = 1200.0
SUPPLEMENT_HATCH_MIN_ASPECT_RATIO = 2.0
WALL_POLYGON_MIN_AREA = 8000.0
WALL_POLYGON_MAX_REGION_RATIO = 0.18
WALL_POLYGON_MIN_LONG_SIDE = 700.0
WALL_POLYGON_MAX_THICKNESS = 1300.0
WALL_POLYGON_MIN_ASPECT_RATIO = 1.8
WALL_POLYGON_MIN_RECTANGULARITY = 0.35
DOOR_SWING_MIN_CHORD = 280.0
DOOR_SWING_MAX_CHORD = 3200.0
CLOSED_SOLID_MIN_AREA = 12000.0
CLOSED_SOLID_MAX_REGION_RATIO = 0.06
CLOSED_SOLID_MIN_SIDE = 80.0
CLOSED_SOLID_MAX_LONG_SIDE = 5200.0
CLOSED_SOLID_MAX_ASPECT_RATIO = 6.0
CLOSED_SOLID_MIN_RECTANGULARITY = 0.55


@dataclass(frozen=True)
class ObstacleRecognitionResult:
    result_json: Path
    obstacle_csv: Path
    output_dir: Path
    marked_dxf: Path | None
    obstacle_count: int
    obstacle_type_count: int
    region_count: int
    per_region_geojsons: list[Path]
    union_geojsons: list[Path]


@dataclass
class WallLineContext:
    lines: list[Any]
    tree: Any | None = None

    def query(self, geometry: Any) -> list[Any]:
        if self.tree is None:
            return self.lines
        try:
            result = self.tree.query(geometry)
        except Exception:
            return self.lines
        candidates: list[Any] = []
        for item in result:
            index = None
            try:
                index = int(item.__index__())
            except Exception:
                pass
            if index is not None and 0 <= index < len(self.lines):
                candidates.append(self.lines[index])
            else:
                candidates.append(item)
        return candidates


def require_geometry_libs():
    try:
        from shapely.geometry import LineString, Polygon, box, mapping  # noqa: F401
        from shapely.ops import unary_union  # noqa: F401
    except Exception as exc:  # pragma: no cover - dependency diagnostics
        raise RuntimeError(
            "障碍物识别需要 shapely。请在当前 Python 环境中安装 shapely 后重试。"
        ) from exc


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def compact_text(value: Any) -> str:
    return normalize_value(str(value or ""))


def norm_contains_any(value: Any, terms: Iterable[str]) -> bool:
    text = str(value or "")
    if not text:
        return False
    raw_upper = text.upper()
    compact = compact_text(text)
    for term in terms:
        marker = str(term or "")
        if not marker:
            continue
        if marker in text or marker.upper() in raw_upper or compact_text(marker) in compact:
            return True
    return False


def row_semantic_text(row: dict[str, Any]) -> str:
    parts = [
        row.get("layer", ""),
        row.get("parent_block_name", ""),
        row.get("block_path", ""),
        row.get("insert_path", ""),
        row.get("raw_text", ""),
        row.get("norm_text", ""),
    ]
    return " / ".join(str(part or "") for part in parts if str(part or "").strip())


def row_layer_block_text(row: dict[str, Any]) -> str:
    parts = [
        row.get("layer", ""),
        row.get("parent_block_name", ""),
        row.get("block_path", ""),
    ]
    return " / ".join(str(part or "") for part in parts if str(part or "").strip())


def row_block_key(row: dict[str, Any]) -> str:
    for field in ("insert_path", "block_path", "parent_block_name"):
        value = str(row.get(field, "") or "").strip()
        if value and value not in {"[]", "{}", "None", "null"}:
            return value
    return ""


def is_negative_context(row: dict[str, Any]) -> bool:
    layer_block = row_layer_block_text(row)
    if norm_contains_any(layer_block, NEGATIVE_LAYER_TERMS):
        return True
    # 门、楼梯、电梯、安全出口等通行相关对象不应进入障碍物识别。
    # 旧版本为了识别窗户，允许“门窗/WINDOW”相关对象继续参与；
    # 现在窗户不再作为障碍物，因此不再保留该例外。
    if norm_contains_any(layer_block, PASSAGE_OR_NON_OBSTACLE_TERMS):
        return True
    return False


def is_closed_entity(row: dict[str, Any]) -> bool:
    value = str(row.get("is_closed", "") or "").strip().lower()
    if value in {"1", "true", "yes", "y"}:
        return True
    geometry_kind = str(row.get("geometry_kind", "") or "").lower()
    return "closed" in geometry_kind or "hatch" in geometry_kind or "area" in geometry_kind


def bbox_metrics(bbox: tuple[float, float, float, float]) -> dict[str, float]:
    minx, miny, maxx, maxy = bbox
    width = maxx - minx
    height = maxy - miny
    area = max(width * height, 0.0)
    short_side = min(width, height)
    long_side = max(width, height)
    aspect = long_side / max(short_side, 1e-9)
    return {
        "width": width,
        "height": height,
        "area": area,
        "short_side": short_side,
        "long_side": long_side,
        "aspect": aspect,
    }


def looks_like_wall_by_geometry(
    row: dict[str, Any],
    bbox: tuple[float, float, float, float],
    region: dict[str, Any],
) -> bool:
    entity_type = str(row.get("entity_type", "") or "").upper()
    if entity_type not in LINE_ENTITY_TYPES | AREA_ENTITY_TYPES:
        return False
    metrics = bbox_metrics(bbox)
    if metrics["width"] < MIN_OBSTACLE_WIDTH and metrics["height"] < MIN_OBSTACLE_HEIGHT:
        return False
    if metrics["area"] > float(region.get("_area") or 0.0) * MAX_SINGLE_OBSTACLE_REGION_RATIO:
        return False
    if norm_contains_any(row_layer_block_text(row), NEGATIVE_LAYER_TERMS):
        return False
    if norm_contains_any(row_layer_block_text(row), STRUCTURE_LAYER_HINTS):
        return metrics["aspect"] >= 2.0 or metrics["short_side"] <= 900.0
    # 无语义时只允许非常像墙的细长闭合/开放线状对象作为低置信候选。
    return metrics["aspect"] >= 8.0 and metrics["short_side"] <= 450.0 and metrics["long_side"] >= 1200.0


def looks_like_column_by_geometry(row: dict[str, Any], bbox: tuple[float, float, float, float]) -> bool:
    entity_type = str(row.get("entity_type", "") or "").upper()
    if entity_type not in ROUND_ENTITY_TYPES | AREA_ENTITY_TYPES | LINE_ENTITY_TYPES:
        return False
    if entity_type in LINE_ENTITY_TYPES and not is_closed_entity(row):
        return False
    metrics = bbox_metrics(bbox)
    if metrics["short_side"] < 100.0 or metrics["long_side"] > 2500.0:
        return False
    if not 0.45 <= metrics["width"] / max(metrics["height"], 1e-9) <= 2.2:
        return False
    return norm_contains_any(row_layer_block_text(row), COLUMN_TERMS + STRUCTURE_LAYER_HINTS)


def classify_obstacle(
    row: dict[str, Any],
    bbox: tuple[float, float, float, float],
    region: dict[str, Any],
) -> dict[str, Any] | None:
    entity_type = str(row.get("entity_type", "") or "").upper()
    if entity_type in TEXT_ENTITY_TYPES:
        return None
    if entity_type not in LINE_ENTITY_TYPES | AREA_ENTITY_TYPES | ROUND_ENTITY_TYPES | INSERT_ENTITY_TYPES:
        return None
    if is_negative_context(row):
        return None

    semantic = row_semantic_text(row)
    metrics = bbox_metrics(bbox)
    region_area = float(region.get("_area") or 0.0)
    if region_area > 0 and metrics["area"] > region_area * MAX_SINGLE_OBSTACLE_REGION_RATIO:
        # 图框、填充背景、整层遮罩常常面积巨大，即使图层名称偶然命中也不应作为单个障碍物。
        return None

    if norm_contains_any(semantic, COLUMN_TERMS) or looks_like_column_by_geometry(row, bbox):
        return {
            "obstacle_type": "column",
            "confidence": 0.92 if norm_contains_any(semantic, COLUMN_TERMS) else 0.68,
            "reason": "column_semantic_or_closed_geometry",
        }


    if norm_contains_any(semantic, WALL_TERMS):
        return {
            "obstacle_type": "wall",
            "confidence": 0.9,
            "reason": "wall_layer_or_block_semantic",
        }

    if looks_like_wall_by_geometry(row, bbox, region):
        return {
            "obstacle_type": "wall",
            "confidence": 0.62,
            "reason": "skinny_structural_line_or_closed_polyline",
        }

    return None


def region_box(region: dict[str, Any]):
    from shapely.geometry import box

    minx, miny, maxx, maxy = region["_bbox"]
    return box(minx, miny, maxx, maxy)


def bbox_box(bbox: tuple[float, float, float, float]):
    from shapely.geometry import box

    return box(*bbox)


def bbox_centerline_buffer(
    bbox: tuple[float, float, float, float],
    *,
    buffer_width: float,
):
    from shapely.geometry import LineString

    minx, miny, maxx, maxy = bbox
    width = maxx - minx
    height = maxy - miny
    if width >= height:
        line = LineString([(minx, (miny + maxy) / 2.0), (maxx, (miny + maxy) / 2.0)])
    else:
        line = LineString([((minx + maxx) / 2.0, miny), ((minx + maxx) / 2.0, maxy)])
    return line.buffer(buffer_width, cap_style=2, join_style=2)


def obstacle_geometry(
    row: dict[str, Any],
    bbox: tuple[float, float, float, float],
    obstacle_type: str,
):
    entity_type = str(row.get("entity_type", "") or "").upper()
    metrics = bbox_metrics(bbox)
    closed = is_closed_entity(row)
    if obstacle_type == "wall" and entity_type in LINE_ENTITY_TYPES and not closed:
        return bbox_centerline_buffer(bbox, buffer_width=DEFAULT_WALL_BUFFER)
    if obstacle_type == "wall" and metrics["aspect"] >= 6.0 and metrics["short_side"] <= 450.0:
        return bbox_centerline_buffer(bbox, buffer_width=max(DEFAULT_WALL_BUFFER, metrics["short_side"] / 2.0))
    return bbox_box(bbox)


def feature_from_geometry(geometry, properties: dict[str, Any]) -> dict[str, Any]:
    from shapely.geometry import mapping

    return {
        "type": "Feature",
        "properties": properties,
        "geometry": mapping(geometry),
    }


def empty_feature_collection() -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": []}


def safe_filename(value: str) -> str:
    name = re.sub(r"[^0-9A-Za-z_\-.\u4e00-\u9fff]+", "_", value).strip("_")
    return name or "UNKNOWN"


def write_feature_collection(path: Path, features: list[dict[str, Any]]) -> None:
    write_json(path, {"type": "FeatureCollection", "features": features})


def write_obstacle_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OBSTACLE_OUTPUT_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in OBSTACLE_OUTPUT_FIELDS})


def read_inventory_rows(inventory_csv: Path) -> Iterable[dict[str, str]]:
    with inventory_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            yield row


def parse_geometry_json(row: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    """从 cad_geometry_inventory.csv 的 geometry_json 恢复真实线/面几何。"""
    from shapely.geometry import LineString, Polygon

    raw = row.get("geometry_json")
    if not raw:
        return [], []
    try:
        payload = json.loads(str(raw))
    except Exception:
        return [], []
    if not isinstance(payload, dict):
        return [], []

    lines: list[Any] = []
    polygons: list[Any] = []
    for coords in payload.get("lines", []) or []:
        if not isinstance(coords, list) or len(coords) < 2:
            continue
        try:
            line = LineString([(float(x), float(y)) for x, y in coords])
        except Exception:
            continue
        if not line.is_empty and line.length >= 10.0:
            lines.append(line)

    for coords in payload.get("polygons", []) or []:
        if not isinstance(coords, list) or len(coords) < 4:
            continue
        try:
            polygon = Polygon([(float(x), float(y)) for x, y in coords])
        except Exception:
            continue
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if not polygon.is_empty and polygon.area > 0:
            polygons.append(polygon)
    return lines, polygons


def row_has_wall_semantic(row: dict[str, Any]) -> bool:
    return norm_contains_any(row_semantic_text(row), WALL_TERMS)


def row_has_column_semantic(row: dict[str, Any]) -> bool:
    return norm_contains_any(row_semantic_text(row), COLUMN_TERMS)



def row_has_solid_obstacle_semantic(row: dict[str, Any]) -> bool:
    semantic = row_layer_block_text(row)
    return norm_contains_any(
        semantic,
        WALL_TERMS + COLUMN_TERMS + STRUCTURE_LAYER_HINTS + SOLID_OBSTACLE_LAYER_HINTS,
    )


def row_has_door_semantic(row: dict[str, Any]) -> bool:
    """识别门洞/门扇几何，用于从墙体障碍中扣除通行净空。"""
    semantic = row_semantic_text(row)
    if not semantic:
        return False
    compact = compact_text(semantic)
    upper = semantic.upper()
    if "阀门" in semantic or "闸门" in semantic:
        return False
    if norm_contains_any(semantic, DOOR_CLEARANCE_TERMS):
        return True
    if re.search(r"(?<![A-Z0-9])FM[甲乙丙]?[A-Z0-9-]*", upper):
        return True
    if "门" in compact and "门窗" not in compact:
        return True
    # “门窗”层里窗线很多，只有门扇圆弧才稳定代表门洞净空。
    return "门窗" in compact and str(row.get("entity_type", "") or "").upper() == "ARC"


def min_rect_dims(poly: Any) -> tuple[float, float]:
    try:
        rect = poly.minimum_rotated_rectangle
        coords = list(rect.exterior.coords)
        lengths = []
        for index in range(1, len(coords)):
            x1, y1 = coords[index - 1]
            x2, y2 = coords[index]
            lengths.append(((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5)
        lengths = sorted(value for value in lengths if value > 1e-6)
        if len(lengths) < 2:
            return 0.0, 0.0
        return float(lengths[0]), float(lengths[-1])
    except Exception:
        return 0.0, 0.0


def is_supplement_wall_hatch_polygon(poly: Any) -> bool:
    if poly is None or poly.is_empty:
        return False
    if not poly.is_valid:
        poly = poly.buffer(0)
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
    return long_side / max(short_side, 1.0) >= SUPPLEMENT_HATCH_MIN_ASPECT_RATIO


def is_wall_like_polygon(poly: Any, region: dict[str, Any] | None = None) -> bool:
    """Accept only polygonal surfaces that look like real wall bodies."""
    if poly is None or poly.is_empty:
        return False
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty:
        return False

    area = float(poly.area)
    if area < WALL_POLYGON_MIN_AREA:
        return False

    region_area = float((region or {}).get("_area") or 0.0)
    if region_area > 0 and area > region_area * WALL_POLYGON_MAX_REGION_RATIO:
        return False

    short_side, long_side = min_rect_dims(poly)
    if short_side <= 0 or long_side <= 0:
        return False
    if short_side > WALL_POLYGON_MAX_THICKNESS:
        return False
    if long_side < WALL_POLYGON_MIN_LONG_SIDE:
        return False
    if long_side / max(short_side, 1.0) < WALL_POLYGON_MIN_ASPECT_RATIO:
        return False

    rect_area = max(short_side * long_side, 1.0)
    if area / rect_area < WALL_POLYGON_MIN_RECTANGULARITY:
        return False
    return True


def row_has_wall_fill_semantic(row: dict[str, Any]) -> bool:
    """Return True for filled/closed entities that are likely real wall surfaces."""
    semantic = row_layer_block_text(row)
    if norm_contains_any(semantic, WALL_FILL_LAYER_HINTS):
        return True
    return row_has_wall_semantic(row)


def is_wall_surface_polygon(
    poly: Any,
    row: dict[str, Any],
    region: dict[str, Any] | None = None,
) -> bool:
    """Accept closed wall fill surfaces, including curved or irregular wall bodies.

    This intentionally does not require a skinny rectangle. Wall fills in CAD can
    be L-shaped, bent, or arc-like, so rectangle/aspect filters are too strict.
    """
    if poly is None or poly.is_empty:
        return False
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty:
        return False
    area = float(poly.area)
    if area < WALL_POLYGON_MIN_AREA:
        return False
    region_area = float((region or {}).get("_area") or 0.0)
    if region_area > 0 and area > region_area * MAX_SINGLE_OBSTACLE_REGION_RATIO:
        return False
    if not row_has_wall_fill_semantic(row):
        return False
    return True


def arc_sweep_degrees(line: Any) -> float | None:
    try:
        coords = list(line.coords)
    except Exception:
        return None
    if len(coords) < 5:
        return None
    x1, y1 = coords[0]
    x2, y2 = coords[-1]
    chord = ((float(x2) - float(x1)) ** 2 + (float(y2) - float(y1)) ** 2) ** 0.5
    if chord < DOOR_SWING_MIN_CHORD or chord > DOOR_SWING_MAX_CHORD:
        return None
    ratio = float(line.length) / max(chord, 1.0)
    if ratio <= 1.0:
        return None
    low = 1e-6
    high = math.pi
    for _ in range(40):
        mid = (low + high) / 2.0
        mid_ratio = mid / max(2.0 * math.sin(mid / 2.0), 1e-9)
        if mid_ratio < ratio:
            low = mid
        else:
            high = mid
    return math.degrees((low + high) / 2.0)


def is_door_swing_arc_line(line: Any) -> bool:
    sweep = arc_sweep_degrees(line)
    if sweep is None:
        return False
    return DOOR_SWING_MIN_ANGLE_DEG <= sweep <= DOOR_SWING_MAX_ANGLE_DEG


def has_door_swing_geometry(
    row: dict[str, Any],
    bbox: tuple[float, float, float, float],
    lines: list[Any],
) -> bool:
    if not lines:
        return False
    metrics = bbox_metrics(bbox)
    if metrics["long_side"] > 6500.0 or metrics["short_side"] > 4200.0:
        return False
    return any(is_door_swing_arc_line(line) for line in lines)


def is_closed_solid_obstacle_polygon(poly: Any, region: dict[str, Any] | None = None) -> bool:
    if poly is None or poly.is_empty:
        return False
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty:
        return False

    area = float(poly.area)
    if area < CLOSED_SOLID_MIN_AREA:
        return False
    region_area = float((region or {}).get("_area") or 0.0)
    if region_area > 0 and area > region_area * CLOSED_SOLID_MAX_REGION_RATIO:
        return False

    short_side, long_side = min_rect_dims(poly)
    if short_side < CLOSED_SOLID_MIN_SIDE or long_side <= 0:
        return False
    if long_side > CLOSED_SOLID_MAX_LONG_SIDE:
        return False
    if long_side / max(short_side, 1.0) > CLOSED_SOLID_MAX_ASPECT_RATIO:
        return False

    rect_area = max(short_side * long_side, 1.0)
    return area / rect_area >= CLOSED_SOLID_MIN_RECTANGULARITY


def is_column_obstacle_geometry(
    row: dict[str, Any],
    region: dict[str, Any],
    lines: list[Any],
    polygons: list[Any],
) -> bool:
    entity_type = str(row.get("entity_type", "") or "").upper()
    if entity_type in ARC_ENTITY_TYPES:
        return False
    if any(is_door_swing_arc_line(line) for line in lines):
        return False
    if entity_type in {"CIRCLE", "ELLIPSE"}:
        return True
    return any(is_closed_solid_obstacle_polygon(poly, region) for poly in polygons)


def geometry_parts(geometry: Any) -> list[Any]:
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type in {"Polygon", "LineString"}:
        return [geometry]
    if geometry.geom_type.startswith("Multi") or geometry.geom_type == "GeometryCollection":
        parts: list[Any] = []
        for item in geometry.geoms:
            parts.extend(geometry_parts(item))
        return parts
    return []


def expand_bbox(
    bbox: tuple[float, float, float, float],
    padding: float,
) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = bbox
    return minx - padding, miny - padding, maxx + padding, maxy + padding


def is_door_clearance_candidate(
    row: dict[str, Any],
    bbox: tuple[float, float, float, float],
    region: dict[str, Any],
    lines: list[Any],
    *,
    door_arc_block_keys: set[str] | None = None,
) -> bool:
    """判断当前几何是否应作为门洞无障碍区依据。

    处理目的：
    1. 恢复“门 / 门洞 / 防火门 / DOOR / FHM”等门语义直接生成 door mask；
    2. 允许同一门块内的直线、门框线、门槛线参与门洞净空保护；
    3. 用尺寸和区域比例限制 mask 范围，避免把大面积墙体或整块图纸误当门区。
    """
    block_key = row_block_key(row)
    in_door_arc_block = bool(
        door_arc_block_keys and block_key and block_key in door_arc_block_keys
    )
    has_door_semantic = row_has_door_semantic(row)
    has_swing_arc = has_door_swing_geometry(row, bbox, lines)
    if not has_door_semantic and not has_swing_arc and not in_door_arc_block:
        return False

    metrics = bbox_metrics(bbox)
    if metrics["long_side"] > DOOR_MASK_MAX_LONG_SIDE:
        return False

    region_area = float(region.get("_area") or 0.0)
    if region_area > 0 and metrics["area"] > region_area * DOOR_MASK_MAX_REGION_RATIO:
        return False

    return True


def door_clearance_mask(
    row: dict[str, Any],
    bbox: tuple[float, float, float, float],
    region: dict[str, Any],
    lines: list[Any],
    polygons: list[Any],
    *,
    door_arc_block_keys: set[str] | None = None,
) -> Any | None:
    """生成门洞无障碍 mask。

    mask 采用“门对象 bbox + 150 图纸单位外扩”的保守矩形，
    用于从墙、柱等障碍物中扣除门口前后的小范围通行净空。
    """
    if not is_door_clearance_candidate(
        row,
        bbox,
        region,
        lines,
        door_arc_block_keys=door_arc_block_keys,
    ):
        return None
    mask = bbox_box(expand_bbox(bbox, DOOR_MASK_PADDING))
    return mask.intersection(region_box(region))


def line_endpoints(line: Any) -> tuple[tuple[float, float], tuple[float, float]] | None:
    try:
        coords = list(line.coords)
    except Exception:
        return None
    if len(coords) < 2:
        return None
    start = (float(coords[0][0]), float(coords[0][1]))
    end = (float(coords[-1][0]), float(coords[-1][1]))
    return start, end


def line_angle(line: Any) -> float | None:
    endpoints = line_endpoints(line)
    if not endpoints:
        return None
    (x1, y1), (x2, y2) = endpoints
    if abs(x2 - x1) < 1e-9 and abs(y2 - y1) < 1e-9:
        return None
    return math.atan2(y2 - y1, x2 - x1)


def angle_delta_degrees(a: float, b: float) -> float:
    delta = abs((a - b + math.pi / 2.0) % math.pi - math.pi / 2.0)
    return math.degrees(delta)


def projection_interval(line: Any, angle: float) -> tuple[float, float] | None:
    try:
        coords = list(line.coords)
    except Exception:
        return None
    if len(coords) < 2:
        return None
    ux, uy = math.cos(angle), math.sin(angle)
    values = [float(x) * ux + float(y) * uy for x, y in coords]
    return min(values), max(values)


def parallel_overlap_ratio(line: Any, other: Any, angle: float) -> float:
    interval_a = projection_interval(line, angle)
    interval_b = projection_interval(other, angle)
    if not interval_a or not interval_b:
        return 0.0
    a1, a2 = interval_a
    b1, b2 = interval_b
    overlap = max(0.0, min(a2, b2) - max(a1, b1))
    base = max(min(a2 - a1, b2 - b1), 1e-9)
    return overlap / base


def is_parallel_wall_pair(line: Any, other: Any) -> bool:
    if line is other:
        return False
    angle = line_angle(line)
    other_angle = line_angle(other)
    if angle is None or other_angle is None:
        return False
    if angle_delta_degrees(angle, other_angle) > WALL_PAIR_MAX_ANGLE_DEG:
        return False
    distance = float(line.distance(other))
    if distance < WALL_PAIR_MIN_DIST or distance > WALL_PAIR_MAX_DIST:
        return False
    return parallel_overlap_ratio(line, other, angle) >= WALL_PAIR_MIN_OVERLAP


def build_wall_line_context(lines: list[Any]) -> WallLineContext:
    try:
        from shapely.strtree import STRtree

        return WallLineContext(lines=lines, tree=STRtree(lines) if lines else None)
    except Exception:
        return WallLineContext(lines=lines, tree=None)


def is_supported_wall_line(line: Any, context: WallLineContext | list[Any]) -> bool:
    if line.length < 10.0:
        return False
    if isinstance(context, WallLineContext):
        candidates = context.query(line.buffer(WALL_PAIR_MAX_DIST))
    else:
        candidates = context
    # Wall layer semantics only provide candidate status. A LINE wall is
    # accepted only when it has a parallel mate, preventing lone annotation,
    # opening, dimension, or helper lines from becoming wall obstacles.
    return any(is_parallel_wall_pair(line, other) for other in candidates)


def classify_geometry_obstacle(
    row: dict[str, Any],
    bbox: tuple[float, float, float, float],
    region: dict[str, Any],
    lines: list[Any],
    polygons: list[Any],
    *,
    door_arc_block_keys: set[str] | None = None,
) -> dict[str, Any] | None:
    entity_type = str(row.get("entity_type", "") or "").upper()
    if entity_type in ARC_ENTITY_TYPES:
        return None
    if entity_type in TEXT_ENTITY_TYPES or entity_type not in LINE_ENTITY_TYPES | AREA_ENTITY_TYPES | ROUND_ENTITY_TYPES:
        return None
    if is_negative_context(row):
        return None

    block_key = row_block_key(row)
    in_door_arc_block = bool(
        door_arc_block_keys and block_key and block_key in door_arc_block_keys
    )

    # 门对象及同一门块内的小范围几何不再进入障碍物分类。
    # 这一步在 wall/column 判断之前执行，避免门框线、门槛线、
    # 门块中的直线被识别为 FIRE_OBS_WALL 后堵住通行。
    if is_door_clearance_candidate(
        row,
        bbox,
        region,
        lines,
        door_arc_block_keys=door_arc_block_keys,
    ):
        return None

    layer = str(row.get("layer", "") or "").strip()

    # Filled or closed wall surfaces are preferred over edge-line walls. They
    # preserve real wall area and support bent / irregular wall geometry.
    if polygons and any(is_wall_surface_polygon(poly, row, region) for poly in polygons):
        return {
            "obstacle_type": "wall",
            "confidence": 0.94,
            "reason": "closed_wall_surface_polygon",
        }

    # 按当前项目约定：PUB_HATCH 中解析出的闭合填充面就是墙体。
    # 这里不再使用面积、长宽比、厚度、矩形度等几何阈值过滤。
    # 只要求 cad_geometry_inventory.csv 中确实解析出了 polygon，后续仍会按 inspection_region 裁剪。
    if entity_type == "HATCH" and layer in SUPPLEMENT_WALL_HATCH_LAYERS and polygons:
        return {
            "obstacle_type": "wall",
            "confidence": 0.91,
            "reason": "pub_hatch_closed_area_wall",
        }

    metrics = bbox_metrics(bbox)
    region_area = float(region.get("_area") or 0.0)
    if region_area > 0 and metrics["area"] > region_area * MAX_SINGLE_OBSTACLE_REGION_RATIO:
        return None

    if row_has_column_semantic(row):
        if in_door_arc_block or not is_column_obstacle_geometry(row, region, lines, polygons):
            return None
        return {
            "obstacle_type": "column",
            "confidence": 0.94,
            "reason": "column_layer_or_block_semantic_real_geometry",
        }


    if row_has_wall_semantic(row):
        return {
            "obstacle_type": "wall",
            "confidence": 0.93,
            "reason": "wall_semantic_with_geometry_support",
        }

    if (
        (entity_type in AREA_ENTITY_TYPES or is_closed_entity(row))
        and row_has_solid_obstacle_semantic(row)
        and not in_door_arc_block
        and any(is_closed_solid_obstacle_polygon(poly, region) for poly in polygons)
    ):
        return {
            "obstacle_type": "column",
            "confidence": 0.72,
            "reason": "closed_filled_polygon_geometry",
        }

    # 无墙/柱语义时不再用“细长 bbox”猜墙，避免把轴线、尺寸线、设备线误当障碍。
    return None


def obstacle_geometries_from_real_geometry(
    decision: dict[str, Any],
    lines: list[Any],
    polygons: list[Any],
    *,
    wall_context: WallLineContext | list[Any] | None = None,
    region: dict[str, Any] | None = None,
) -> list[Any]:
    obstacle_type = str(decision.get("obstacle_type") or "")
    geoms: list[Any] = []
    if obstacle_type == "wall":
        context = wall_context or lines
        for line in lines:
            if is_supported_wall_line(line, context):
                geoms.append(line.buffer(DEFAULT_WALL_BUFFER, cap_style=2, join_style=2))
        for poly in polygons:
            if str(decision.get("reason")) == "closed_wall_surface_polygon":
                # Keep validated wall fill polygons as surfaces. Do not apply
                # straight-wall aspect/thickness/rectangularity filters here.
                geoms.append(poly)
            elif str(decision.get("reason")) == "pub_hatch_closed_area_wall":
                # PUB_HATCH 已在分类阶段被认定为闭合墙体填充面，直接保留 polygon。
                # 不再套用 is_supplement_wall_hatch_polygon() 或 is_wall_like_polygon() 的面积/长宽比限制。
                geoms.append(poly)
            elif str(decision.get("reason")) == "supplement_skinny_pub_hatch_real_polygon":
                if is_supplement_wall_hatch_polygon(poly):
                    geoms.append(poly)
            else:
                if is_wall_like_polygon(poly, region):
                    geoms.append(poly)
    elif obstacle_type == "column":
        if str(decision.get("reason")) == "closed_filled_polygon_geometry":
            geoms.extend(poly for poly in polygons if is_closed_solid_obstacle_polygon(poly, region))
        else:
            geoms.extend(polygons)
    return [geom for geom in geoms if geom is not None and not geom.is_empty]


def recognize_obstacle_geometry_rows(
    geometry_csv: Path,
    regions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    require_geometry_libs()
    features_by_region: dict[str, list[dict[str, Any]]] = defaultdict(list)
    geometries_by_region_type: dict[tuple[str, str], list[Any]] = defaultdict(list)
    obstacle_rows: list[dict[str, Any]] = []
    scanned_count = 0
    region_hit_count = 0
    rejected_count = Counter()
    door_blocked_obstacle_clear_count = 0
    entries: list[dict[str, Any]] = []
    door_masks_by_region: dict[str, list[Any]] = defaultdict(list)
    wall_context_lines_by_region: dict[str, list[Any]] = defaultdict(list)
    door_arc_block_keys_by_region: dict[str, set[str]] = defaultdict(set)

    for row in read_inventory_rows(geometry_csv):
        scanned_count += 1
        bbox = row_bbox(row)
        if not bbox:
            rejected_count["invalid_bbox"] += 1
            continue
        region = assign_region(bbox, regions)
        if not region:
            rejected_count["outside_regions"] += 1
            continue
        region_hit_count += 1

        lines, polygons = parse_geometry_json(row)
        if not lines and not polygons:
            rejected_count["invalid_geometry_json"] += 1
            continue
        sheet_id = str(region.get("sheet_id", ""))
        entry = {
            "row": row,
            "bbox": bbox,
            "region": region,
            "sheet_id": sheet_id,
            "lines": lines,
            "polygons": polygons,
        }
        entries.append(entry)
        block_key = row_block_key(row)
        entity_type = str(row.get("entity_type", "") or "").upper()
        if block_key and any(is_door_swing_arc_line(line) for line in lines):
            door_arc_block_keys_by_region[sheet_id].add(block_key)


    for entry in entries:
        sheet_id = entry["sheet_id"]
        mask = door_clearance_mask(
            entry["row"],
            entry["bbox"],
            entry["region"],
            entry["lines"],
            entry["polygons"],
            door_arc_block_keys=door_arc_block_keys_by_region.get(sheet_id),
        )
        if mask is not None and not mask.is_empty:
            door_masks_by_region[sheet_id].append(mask)

        # 墙线平行配对上下文也要排除门对象和门块内小几何。
        # 否则门框线虽然不直接输出为障碍物，却可能作为“平行伴随线”支持旁边短线成为墙。
        if (
            row_has_wall_semantic(entry["row"])
            and not is_negative_context(entry["row"])
            and not is_door_clearance_candidate(
                entry["row"],
                entry["bbox"],
                entry["region"],
                entry["lines"],
                door_arc_block_keys=door_arc_block_keys_by_region.get(sheet_id),
            )
        ):
            wall_context_lines_by_region[sheet_id].extend(entry["lines"])

    door_mask_union_by_region: dict[str, Any] = {}
    for sheet_id, masks in door_masks_by_region.items():
        union = union_geometries(masks)
        if union is not None and not union.is_empty:
            door_mask_union_by_region[sheet_id] = union
    wall_context_by_region = {
        sheet_id: build_wall_line_context(lines)
        for sheet_id, lines in wall_context_lines_by_region.items()
    }

    for entry in entries:
        row = entry["row"]
        bbox = entry["bbox"]
        region = entry["region"]
        sheet_id = entry["sheet_id"]
        lines = entry["lines"]
        polygons = entry["polygons"]
        decision = classify_geometry_obstacle(
            row,
            bbox,
            region,
            lines,
            polygons,
            door_arc_block_keys=door_arc_block_keys_by_region.get(sheet_id),
        )
        if not decision:
            rejected_count["not_obstacle"] += 1
            continue

        raw_geoms = obstacle_geometries_from_real_geometry(
            decision,
            lines,
            polygons,
            wall_context=wall_context_by_region.get(sheet_id, []),
            region=region,
        )
        if not raw_geoms:
            rejected_count["no_usable_obstacle_geometry"] += 1
            continue

        for raw_geom in raw_geoms:
            clipped = raw_geom.intersection(region_box(region))
            door_mask = door_mask_union_by_region.get(sheet_id)
            if door_mask is not None and not clipped.is_empty and clipped.intersects(door_mask):
                before_area = float(getattr(clipped, "area", 0.0) or 0.0)
                clipped = clipped.difference(door_mask)
                after_area = float(getattr(clipped, "area", 0.0) or 0.0)
                if after_area < before_area:
                    door_blocked_obstacle_clear_count += 1
            if clipped.is_empty:
                rejected_count["empty_after_region_clip"] += 1
                continue
            for clipped_part in geometry_parts(clipped):
                if clipped_part.is_empty or getattr(clipped_part, "area", 0.0) <= 0.0:
                    rejected_count["zero_area_geometry"] += 1
                    continue
                obstacle_id = f"OBS_{len(obstacle_rows) + 1:07d}"
                record = {
                    "obstacle_id": obstacle_id,
                    "sheet_id": region.get("sheet_id", ""),
                    "parent_sheet_id": region.get("parent_sheet_id", ""),
                    "floor_id": region.get("floor_id", ""),
                    "floor_name": region.get("floor_name", ""),
                    "inspection_region_id": region.get("inspection_region_id", ""),
                    "obstacle_type": decision["obstacle_type"],
                    "confidence": round(float(decision["confidence"]), 3),
                    "reason": decision["reason"],
                    "geometry_source": "cad_geometry_inventory_real_geometry",
                    "object_id": row.get("object_id", ""),
                    "handle": row.get("handle", ""),
                    "source": row.get("source", ""),
                    "entity_type": row.get("entity_type", ""),
                    "geometry_kind": row.get("geometry_kind", ""),
                    "is_closed": row.get("is_closed", ""),
                    "layer": row.get("layer", ""),
                    "parent_block_name": row.get("parent_block_name", ""),
                    "block_path": row.get("block_path", ""),
                    "bbox_minx": row.get("bbox_minx", ""),
                    "bbox_miny": row.get("bbox_miny", ""),
                    "bbox_maxx": row.get("bbox_maxx", ""),
                    "bbox_maxy": row.get("bbox_maxy", ""),
                    "bbox_area": row.get("bbox_area", ""),
                }
                obstacle_rows.append(record)
                feature = feature_from_geometry(clipped_part, record)
                obstacle_type = str(decision["obstacle_type"])
                features_by_region[sheet_id].append(feature)
                geometries_by_region_type[(sheet_id, obstacle_type)].append(clipped_part)

    summary = {
        "geometry_csv": str(geometry_csv),
        "scanned_geometry_objects": scanned_count,
        "geometry_objects_in_usable_regions": region_hit_count,
        "door_clearance_mask_count": sum(len(value) for value in door_masks_by_region.values()),
        "door_clearance_region_count": len(door_mask_union_by_region),
        "door_blocked_obstacle_clear_count": door_blocked_obstacle_clear_count,
        "door_arc_block_key_count": sum(len(value) for value in door_arc_block_keys_by_region.values()),
        "wall_context_line_count": sum(len(value) for value in wall_context_lines_by_region.values()),
        "obstacle_count": len(obstacle_rows),
        "rejected_counts": dict(rejected_count),
        "features_by_region": {key: len(value) for key, value in features_by_region.items()},
    }
    return obstacle_rows, {
        "summary": summary,
        "features_by_region": features_by_region,
        "geometries_by_region_type": geometries_by_region_type,
    }


def recognize_obstacle_rows(
    inventory_csv: Path,
    regions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    require_geometry_libs()
    features_by_region: dict[str, list[dict[str, Any]]] = defaultdict(list)
    geometries_by_region_type: dict[tuple[str, str], list[Any]] = defaultdict(list)
    obstacle_rows: list[dict[str, Any]] = []
    scanned_count = 0
    region_hit_count = 0
    rejected_count = Counter()

    for row in read_inventory_rows(inventory_csv):
        scanned_count += 1
        bbox = row_bbox(row)
        if not bbox:
            rejected_count["invalid_bbox"] += 1
            continue
        region = assign_region(bbox, regions)
        if not region:
            rejected_count["outside_regions"] += 1
            continue
        region_hit_count += 1

        decision = classify_obstacle(row, bbox, region)
        if not decision:
            rejected_count["not_obstacle"] += 1
            continue

        geom = obstacle_geometry(row, bbox, str(decision["obstacle_type"]))
        clipped = geom.intersection(region_box(region))
        if clipped.is_empty:
            rejected_count["empty_after_region_clip"] += 1
            continue
        if clipped.area <= 0.0:
            rejected_count["zero_area_geometry"] += 1
            continue

        obstacle_id = f"OBS_{len(obstacle_rows) + 1:07d}"
        metrics = bbox_metrics(bbox)
        record = {
            "obstacle_id": obstacle_id,
            "sheet_id": region.get("sheet_id", ""),
            "parent_sheet_id": region.get("parent_sheet_id", ""),
            "floor_id": region.get("floor_id", ""),
            "floor_name": region.get("floor_name", ""),
            "inspection_region_id": region.get("inspection_region_id", ""),
            "obstacle_type": decision["obstacle_type"],
            "confidence": round(float(decision["confidence"]), 3),
            "reason": decision["reason"],
            "geometry_source": "inventory_bbox_buffer_or_polygon",
            "object_id": row.get("object_id", ""),
            "handle": row.get("handle", ""),
            "source": row.get("source", ""),
            "entity_type": row.get("entity_type", ""),
            "geometry_kind": row.get("geometry_kind", ""),
            "is_closed": row.get("is_closed", ""),
            "layer": row.get("layer", ""),
            "parent_block_name": row.get("parent_block_name", ""),
            "block_path": row.get("block_path", ""),
            "bbox_minx": bbox[0],
            "bbox_miny": bbox[1],
            "bbox_maxx": bbox[2],
            "bbox_maxy": bbox[3],
            "bbox_area": metrics["area"],
        }
        obstacle_rows.append(record)
        feature = feature_from_geometry(clipped, record)
        features_by_region[str(region.get("sheet_id", ""))].append(feature)
        geometries_by_region_type[(str(region.get("sheet_id", "")), str(decision["obstacle_type"]))].append(clipped)

    summary = {
        "inventory_csv": str(inventory_csv),
        "scanned_objects": scanned_count,
        "objects_in_usable_regions": region_hit_count,
        "obstacle_count": len(obstacle_rows),
        "rejected_counts": dict(rejected_count),
        "features_by_region": {key: len(value) for key, value in features_by_region.items()},
    }
    return obstacle_rows, {
        "summary": summary,
        "features_by_region": features_by_region,
        "geometries_by_region_type": geometries_by_region_type,
    }


def union_geometries(geometries: list[Any]):
    from shapely.ops import unary_union

    if not geometries:
        return None
    union = unary_union(geometries)
    return union if not union.is_empty else None


def write_geojson_outputs(
    output_dir: Path,
    rows: list[dict[str, Any]],
    features_by_region: dict[str, list[dict[str, Any]]],
    geometries_by_region_type: dict[tuple[str, str], list[Any]],
) -> tuple[list[Path], list[Path], dict[str, Any]]:
    per_region_dir = output_dir / "per_region"
    union_dir = output_dir / "union"
    per_region_dir.mkdir(parents=True, exist_ok=True)
    union_dir.mkdir(parents=True, exist_ok=True)

    per_region_paths: list[Path] = []
    for sheet_id, features in sorted(features_by_region.items()):
        path = per_region_dir / f"obstacles_{safe_filename(sheet_id)}.geojson"
        write_feature_collection(path, features)
        per_region_paths.append(path)

    union_paths: list[Path] = []
    union_summary: dict[str, Any] = {}
    for (sheet_id, obstacle_type), geometries in sorted(geometries_by_region_type.items()):
        union = union_geometries(geometries)
        if union is None:
            continue
        props = {
            "sheet_id": sheet_id,
            "obstacle_type": obstacle_type,
            "source_feature_count": len(geometries),
            "union_area": union.area,
        }
        path = union_dir / f"obstacle_union_{safe_filename(sheet_id)}_{safe_filename(obstacle_type)}.geojson"
        write_feature_collection(path, [feature_from_geometry(union, props)])
        union_paths.append(path)
        union_summary[f"{sheet_id}:{obstacle_type}"] = props

    write_feature_collection(output_dir / "obstacles_all.geojson", [
        feature
        for features in features_by_region.values()
        for feature in features
    ])
    return per_region_paths, union_paths, union_summary


def ensure_dxf_layer(doc: Any, name: str, color: int) -> None:
    try:
        doc.layers.get(name)
    except Exception:
        doc.layers.add(name, color=color)


def polygon_exteriors(geometry: Any) -> Iterable[list[tuple[float, float]]]:
    geom_type = getattr(geometry, "geom_type", "")
    if geom_type == "Polygon":
        coords = [(float(x), float(y)) for x, y in list(geometry.exterior.coords)]
        if len(coords) >= 3:
            yield coords
    elif geom_type == "MultiPolygon":
        for polygon in geometry.geoms:
            yield from polygon_exteriors(polygon)
    elif geom_type == "GeometryCollection":
        for part in geometry.geoms:
            yield from polygon_exteriors(part)


def write_obstacle_overlay_dxf(
    input_dxf: Path,
    output_dxf: Path,
    geometries_by_region_type: dict[tuple[str, str], list[Any]],
    *,
    max_union_parts: int = 5000,
) -> Path:
    require_geometry_libs()
    try:
        import ezdxf
    except Exception as exc:  # pragma: no cover - dependency diagnostics
        raise RuntimeError("障碍物标注 DXF 需要 ezdxf。") from exc

    doc = ezdxf.readfile(input_dxf)
    msp = doc.modelspace()
    layer_by_type = {
        "wall": ("FIRE_OBS_WALL", 1),
        "column": ("FIRE_OBS_COLUMN", 5),
    }
    label_layer = "FIRE_OBS_LABEL"
    ensure_dxf_layer(doc, label_layer, 1)
    for name, color in layer_by_type.values():
        ensure_dxf_layer(doc, name, color)

    drawn_parts = 0
    for (sheet_id, obstacle_type), geometries in sorted(geometries_by_region_type.items()):
        union = union_geometries(geometries)
        if union is None:
            continue
        layer_name, _color = layer_by_type.get(obstacle_type, ("FIRE_OBS_OTHER", 2))
        ensure_dxf_layer(doc, layer_name, 2)
        minx, miny, maxx, maxy = union.bounds
        label = f"{sheet_id} {obstacle_type} x{len(geometries)}"
        msp.add_text(
            label,
            dxfattribs={"layer": label_layer, "height": max((maxx - minx) / 80.0, 200.0)},
        ).set_placement((minx, maxy))
        for coords in polygon_exteriors(union):
            if drawn_parts >= max_union_parts:
                break
            msp.add_lwpolyline(coords, close=True, dxfattribs={"layer": layer_name})
            drawn_parts += 1

    output_dxf.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(output_dxf)
    return output_dxf


def recognize_floor_obstacles(
    input_dxf: Path | str,
    inventory_dir: Path | str,
    sheets_json: Path | str,
    output_dir: Path | str,
    *,
    write_review_dxf: bool = True,
) -> ObstacleRecognitionResult:
    input_path = Path(input_dxf).expanduser().resolve()
    inventory_path = Path(inventory_dir).resolve() / FULL_INVENTORY_FILE
    geometry_path = Path(inventory_dir).resolve() / GEOMETRY_INVENTORY_FILE
    sheets_path = Path(sheets_json).resolve()
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not inventory_path.exists():
        raise FileNotFoundError(f"缺少全量 CAD 图元清单: {inventory_path}")
    if not geometry_path.exists():
        raise FileNotFoundError(
            f"缺少结构几何清单: {geometry_path}。请重新运行主程序或使用 --force-inventory 重新生成 inventory。"
        )
    if not sheets_path.exists():
        raise FileNotFoundError(f"缺少图幅/楼层预处理结果: {sheets_path}")

    regions = load_regions(sheets_path)
    if not regions:
        raise RuntimeError("没有可用于路径规划的楼层区域，无法进行障碍物识别。")

    obstacle_rows, payload = recognize_obstacle_geometry_rows(geometry_path, regions)
    features_by_region = payload["features_by_region"]
    geometries_by_region_type = payload["geometries_by_region_type"]
    obstacle_csv = out_dir / "obstacle_segments_all.csv"
    write_obstacle_csv(obstacle_csv, obstacle_rows)

    per_region_geojsons, union_geojsons, union_summary = write_geojson_outputs(
        out_dir,
        obstacle_rows,
        features_by_region,
        geometries_by_region_type,
    )

    marked_dxf: Path | None = None
    if write_review_dxf:
        marked_dxf = write_obstacle_overlay_dxf(
            input_path,
            out_dir / f"{input_path.stem}_obstacles_marked.dxf",
            geometries_by_region_type,
        )

    type_counts = Counter(str(row.get("obstacle_type") or "") for row in obstacle_rows)
    region_counts = Counter(str(row.get("sheet_id") or "") for row in obstacle_rows)
    result_payload = {
        "input_dxf": str(input_path),
        "inventory_dir": str(Path(inventory_dir).resolve()),
        "geometry_inventory": str(geometry_path),
        "sheets_json": str(sheets_path),
        "output_dir": str(out_dir),
        "recognition_mode": "real_geometry_inventory_with_floor_region_constraint",
        "note": (
            "障碍物识别复用上游 cad_geometry_inventory.csv 中的真实 LINE/LWPOLYLINE/HATCH/CIRCLE 几何；"
            "不再使用 inventory bbox 猜测墙体；门语义、门扇圆弧及同一门块会生成 150 单位门洞无障碍区，避免门框线/门槛线/门块内线条堵门。若缺少块内墙体，请重新生成 inventory 以触发结构几何块选择性展开。"
        ),
        "region_count": len(regions),
        "obstacle_count": len(obstacle_rows),
        "obstacle_type_counts": dict(type_counts.most_common()),
        "region_counts": dict(region_counts.most_common()),
        "scan_summary": payload["summary"],
        "union_summary": union_summary,
        "artifacts": {
            "obstacle_csv": str(obstacle_csv),
            "obstacles_all_geojson": str(out_dir / "obstacles_all.geojson"),
            "per_region_geojsons": [str(path) for path in per_region_geojsons],
            "union_geojsons": [str(path) for path in union_geojsons],
            "marked_dxf": str(marked_dxf) if marked_dxf else "",
        },
        "sample_obstacles": obstacle_rows[:30],
    }
    result_json = out_dir / "obstacle_recognition_results.json"
    write_json(result_json, result_payload)

    return ObstacleRecognitionResult(
        result_json=result_json,
        obstacle_csv=obstacle_csv,
        output_dir=out_dir,
        marked_dxf=marked_dxf,
        obstacle_count=len(obstacle_rows),
        obstacle_type_count=len(type_counts),
        region_count=len(regions),
        per_region_geojsons=per_region_geojsons,
        union_geojsons=union_geojsons,
    )


def strip_path_quotes(value: str) -> str:
    return value.strip().strip('"').strip("'")


def prompt_path(label: str) -> Path:
    value = strip_path_quotes(input(f"{label}: "))
    if not value:
        raise FileNotFoundError(f"未输入{label}")
    return Path(value).expanduser().resolve()


def infer_inventory_dir_from_sheets(sheets_json: Path) -> Path | None:
    payload = read_json(sheets_json)
    value = payload.get("inventory_dir")
    if value:
        path = Path(str(value)).expanduser().resolve()
        return path if path.exists() else None
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按楼层可巡检区域识别墙体、柱体等障碍物，并生成审查标注 DXF。")
    parser.add_argument("-i", "--input-dxf", default="", help="原始 DXF 文件路径。")
    parser.add_argument("--inventory-dir", default="", help="包含 cad_object_inventory.csv 的上游 inventory 目录。")
    parser.add_argument("--sheets-json", default="", help="图幅/楼层预处理 drawing_sheets_floors.json。")
    parser.add_argument("-o", "--output-dir", default="", help="障碍物识别输出目录。")
    parser.add_argument("--no-review-dxf", action="store_true", help="不生成障碍物标注 DXF。")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_dxf = Path(strip_path_quotes(args.input_dxf)).expanduser().resolve() if args.input_dxf else prompt_path("请输入 DXF 文件路径")
    sheets_json = Path(strip_path_quotes(args.sheets_json)).expanduser().resolve() if args.sheets_json else prompt_path(
        "请输入 drawing_sheets_floors.json 路径"
    )
    if args.inventory_dir:
        inventory_dir = Path(strip_path_quotes(args.inventory_dir)).expanduser().resolve()
    else:
        inventory_dir = infer_inventory_dir_from_sheets(sheets_json) or prompt_path("请输入 inventory 目录路径")
    output_dir = (
        Path(strip_path_quotes(args.output_dir)).expanduser().resolve()
        if args.output_dir
        else sheets_json.parent.parent / "obstacles"
    )

    result = recognize_floor_obstacles(
        input_dxf,
        inventory_dir,
        sheets_json,
        output_dir,
        write_review_dxf=not args.no_review_dxf,
    )
    print(json.dumps(
        {
            "result_json": str(result.result_json),
            "obstacle_csv": str(result.obstacle_csv),
            "marked_dxf": str(result.marked_dxf) if result.marked_dxf else "",
            "obstacle_count": result.obstacle_count,
            "obstacle_type_count": result.obstacle_type_count,
            "region_count": result.region_count,
        },
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
