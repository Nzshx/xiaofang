from __future__ import annotations

import csv
import http.client
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ezdxf
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box, mapping
from shapely.ops import unary_union

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    from mark_inspection_objects_dxf import timestamped_output_path  # type: ignore
except Exception:  # pragma: no cover
    def timestamped_output_path(path: Path) -> Path:
        return path.with_name(f"{path.stem}_{time.strftime('%Y%m%d%H%M%S')}{path.suffix}")


GEOMETRY_FILE = "cad_geometry_inventory.csv"
LAYER_LLM_FILE = "obstacle_layer_llm_decisions.json"
OBSTACLE_CSV_FILE = "floor_obstacles.csv"
RESULT_JSON_FILE = "floor_obstacle_recognition_result.json"
LAYER_LLM_PROMPT_VERSION = "obstacle-layer-v2-strict-wall-column"
LAYER_LLM_BATCH_SIZE = 20
LAYER_LLM_MAX_RETRIES = 3

WALL_LAYER = "CHECK_OBSTACLE_WALL"
COLUMN_LAYER = "CHECK_OBSTACLE_COLUMN"
FILL_LAYER = "CHECK_OBSTACLE_FILL"
TEXT_LAYER = "CHECK_OBSTACLE_TEXT"


@dataclass(frozen=True)
class ObstacleConfig:
    """Centralized geometry thresholds for obstacle validation."""

    min_area: float = 120.0
    max_region_area_ratio: float = 0.45
    max_aspect_ratio: float = 40.0
    parallel_angle_tolerance_deg: float = 5.0
    parallel_spacing_tolerance_ratio: float = 0.35
    min_parallel_lines: int = 3
    max_parallel_lines: int = 4
    min_line_length: float = 120.0
    max_parallel_bundle_width: float = 1800.0
    min_parallel_overlap: float = 250.0
    wall_connect_buffer: float = 300.0
    opening_mask_buffer: float = 80.0
    direct_line_buffer: float = 30.0


@dataclass(frozen=True)
class ObstacleRecognitionResult:
    result_json: Path
    obstacle_csv: Path
    marked_dxf: Path | None
    obstacle_count: int
    obstacle_type_count: int
    region_count: int
    per_region_geojsons: list[Path]
    union_geojsons: list[Path]


def safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except Exception:
        return None
    return number if math.isfinite(number) else None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def clean_layer_name(value: Any) -> str:
    return re.sub(r"[\s_\-()（）\[\]【】/\\]+", "", str(value or "")).upper()


def bbox_from_row(row: dict[str, str]) -> tuple[float, float, float, float] | None:
    minx = safe_float(row.get("bbox_minx"))
    miny = safe_float(row.get("bbox_miny"))
    maxx = safe_float(row.get("bbox_maxx"))
    maxy = safe_float(row.get("bbox_maxy"))
    if None in (minx, miny, maxx, maxy):
        return None
    if maxx <= minx or maxy <= miny:
        return None
    return float(minx), float(miny), float(maxx), float(maxy)


def bbox_center(bounds: tuple[float, float, float, float]) -> tuple[float, float]:
    minx, miny, maxx, maxy = bounds
    return (minx + maxx) / 2.0, (miny + maxy) / 2.0


def bbox_intersects(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return max(a[0], b[0]) <= min(a[2], b[2]) and max(a[1], b[1]) <= min(a[3], b[3])


def parse_geometry_json(row: dict[str, str]) -> dict[str, Any]:
    raw = row.get("geometry_json") or ""
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def polygon_from_points(points: Any, *, min_area: float) -> Polygon | None:
    if not isinstance(points, list) or len(points) < 4:
        return None
    coords: list[tuple[float, float]] = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        x = safe_float(point[0])
        y = safe_float(point[1])
        if x is not None and y is not None:
            coords.append((x, y))
    if len(coords) < 4:
        return None
    try:
        poly = Polygon(coords)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty or poly.area < min_area:
            return None
        return poly
    except Exception:
        return None


def polygons_from_row(row: dict[str, str], config: ObstacleConfig) -> list[Polygon]:
    payload = parse_geometry_json(row)
    polygons: list[Polygon] = []
    for points in payload.get("polygons", []) or []:
        poly = polygon_from_points(points, min_area=config.min_area)
        if poly is not None:
            polygons.append(poly)
    if polygons:
        return polygons

    entity_type = str(row.get("entity_type") or "").upper()
    bounds = bbox_from_row(row)
    if bounds and entity_type in {"CIRCLE", "REGION", "SOLID"}:
        return [box(*bounds)]
    return []


def lines_from_row(row: dict[str, str], config: ObstacleConfig, *, min_length: float | None = None) -> list[LineString]:
    payload = parse_geometry_json(row)
    lines: list[LineString] = []
    threshold = config.min_line_length if min_length is None else min_length
    for item in payload.get("lines", []) or []:
        if not isinstance(item, list) or len(item) < 2:
            continue
        coords: list[tuple[float, float]] = []
        for point in item:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            x = safe_float(point[0])
            y = safe_float(point[1])
            if x is not None and y is not None:
                coords.append((x, y))
        if len(coords) < 2:
            continue
        try:
            line = LineString(coords)
            if not line.is_empty and line.length > 0 and line.length >= threshold:
                lines.append(line)
        except Exception:
            continue
    return lines


def split_to_segments(line: LineString, config: ObstacleConfig) -> list[LineString]:
    coords = list(line.coords)
    out: list[LineString] = []
    for start, end in zip(coords, coords[1:]):
        segment = LineString([start, end])
        if segment.length >= config.min_line_length:
            out.append(segment)
    return out


def load_geometry_rows(inventory_dir: Path) -> list[dict[str, str]]:
    path = inventory_dir / GEOMETRY_FILE
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def load_floor_regions(sheets_json: Path) -> list[dict[str, Any]]:
    payload = read_json(sheets_json)
    regions: list[dict[str, Any]] = []
    for sheet in payload.get("sheets", []) or []:
        if not isinstance(sheet, dict) or not sheet.get("path_planning_usable"):
            continue
        sheet_id = str(sheet.get("sheet_id") or "")
        floor_id = str(sheet.get("floor_id") or "")
        floor_name = str(sheet.get("floor_name") or floor_id or sheet_id)
        inspection_regions = sheet.get("inspection_regions") or []
        if not inspection_regions and sheet.get("inspection_region_bbox"):
            inspection_regions = [{"region_id": "R01", "bbox": sheet.get("inspection_region_bbox")}]
        if not inspection_regions and sheet.get("bbox"):
            inspection_regions = [{"region_id": "R01", "bbox": sheet.get("bbox")}]
        for index, item in enumerate(inspection_regions, start=1):
            if not isinstance(item, dict):
                continue
            raw_bbox = item.get("bbox")
            if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4:
                continue
            bbox_tuple = tuple(float(value) for value in raw_bbox)
            if bbox_tuple[2] <= bbox_tuple[0] or bbox_tuple[3] <= bbox_tuple[1]:
                continue
            region_id = str(item.get("region_id") or f"R{index:02d}")
            regions.append(
                {
                    "sheet_id": sheet_id,
                    "floor_id": floor_id,
                    "floor_name": floor_name,
                    "region_id": region_id,
                    "full_region_id": f"{sheet_id}:{region_id}",
                    "bbox": bbox_tuple,
                    "polygon": box(*bbox_tuple),
                }
            )
    return regions


def layer_summary(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    for row in rows:
        layer = str(row.get("layer") or "")
        if not layer:
            continue
        item = summaries.setdefault(
            layer,
            {
                "layer": layer,
                "normalized_layer": clean_layer_name(layer),
                "entity_types": Counter(),
                "geometry_kinds": Counter(),
                "sample_blocks": Counter(),
                "sample_texts": Counter(),
            },
        )
        item["entity_types"][str(row.get("entity_type") or "")] += 1
        item["geometry_kinds"][str(row.get("geometry_kind") or "")] += 1
        block = str(row.get("parent_block_name") or "")
        text = str(row.get("norm_text") or row.get("raw_text") or "")
        if block:
            item["sample_blocks"][block[:80]] += 1
        if text:
            item["sample_texts"][text[:80]] += 1

    out: list[dict[str, Any]] = []
    for item in summaries.values():
        out.append(
            {
                "layer": item["layer"],
                "normalized_layer": item["normalized_layer"],
                "entity_types": dict(item["entity_types"].most_common(10)),
                "geometry_kinds": dict(item["geometry_kinds"].most_common(10)),
                "sample_blocks": [key for key, _count in item["sample_blocks"].most_common(8)],
                "sample_texts": [key for key, _count in item["sample_texts"].most_common(8)],
            }
        )
    return sorted(out, key=lambda item: item["layer"])


def parse_llm_json(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?", "", value).strip()
        value = re.sub(r"```$", "", value).strip()
    try:
        payload = json.loads(value)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", value, flags=re.S)
        if not match:
            return {}
        try:
            payload = json.loads(match.group(0))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}


def legacy_llm_script_defaults() -> dict[str, str]:
    """Read the existing inspection LLM script defaults without importing it."""

    script_path = SCRIPTS_DIR / "llm-deepseekv4.py"
    if not script_path.exists():
        return {}
    try:
        text = script_path.read_text(encoding="utf-8")
    except Exception:
        return {}

    defaults: dict[str, str] = {}
    patterns = {
        "api_key": r'--api-key"\s*,\s*default="([^"]*)"',
        "base_url": r'--base-url"\s*,\s*default="([^"]*)"',
        "model": r"DEFAULT_MODEL\s*=\s*\"([^\"]*)\"",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            defaults[key] = match.group(1)
    return defaults


def llm_runtime_config() -> tuple[str, str, str]:
    defaults = legacy_llm_script_defaults()
    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or defaults.get("api_key", "")
    base_url = os.getenv("DEEPSEEK_BASE_URL") or defaults.get("base_url") or "https://api.deepseek.com"
    model = os.getenv("DEEPSEEK_MODEL") or defaults.get("model") or "deepseek-v4-flash"
    if model.strip().lower() == "deepseek-chat":
        model = "deepseek-v4-flash"
    return api_key, base_url, model


def request_layer_llm(batch: list[dict[str, Any]], *, api_key: str, base_url: str, model: str) -> list[dict[str, Any]]:
    prompt = {
        "task": "CAD layer obstacle relevance classification for fire inspection route planning",
        "prompt_version": LAYER_LLM_PROMPT_VERSION,
        "important_policy": [
            "Only classify layer semantic relevance.",
            "Only layers whose layer name, block samples, or text samples clearly mean wall/column/curtain-wall/structural fill may be obstacle_candidate.",
            "Positive obstacle evidence includes: WALL, COL, COLUMN, PILLAR, CONC, CONCRETE, MASONRY, BRICK, SHEARWALL, CURTAIN WALL, GLZ/GLZE when it means curtain wall glazing, 墙, 柱, 幕墙, 砌体, 混凝土, 剪力墙.",
            "Do not infer obstacle_candidate from elevator, stair, shaft, room, equipment, window, door, opening, passage, ramp, furniture, annotation, or symbol semantics unless the same layer also explicitly contains wall/column evidence.",
            "A-EVTR/elevator, stair/staircase/lift, window/door/opening/passage layers are not obstacle layers by themselves.",
            "Dimension, axis, grid, title block, frame, legend, table, and text-only annotation layers are not obstacles.",
        ],
        "roles": {
            "obstacle_candidate": "Layer explicitly means wall, column, curtain wall, masonry/concrete wall, or structural filled obstacle.",
            "passable_opening": "Door/opening/passage/window-door geometry that should be excluded from obstacle output.",
            "not_obstacle": "Annotation, axis, dimension, title, frame, furniture, symbol, or unrelated layer.",
            "unknown": "Insufficient semantic evidence.",
        },
        "candidate_types": ["wall", "column", "filled_obstacle"],
        "layers": batch,
        "output_schema": {
            "decisions": [
                {
                    "layer": "original layer name",
                    "role": "obstacle_candidate|passable_opening|not_obstacle|unknown",
                    "candidate_types": ["wall"],
                    "confidence": 0.0,
                    "reason": "brief semantic reason",
                }
            ]
        },
    }
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are a CAD semantics assistant. Return strict JSON only.",
            },
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        "temperature": 0,
        "enable_thinking": False,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        payload = json.loads(response.read().decode("utf-8"))
    content = payload["choices"][0]["message"]["content"]
    parsed = parse_llm_json(content)
    return [item for item in parsed.get("decisions", []) or [] if isinstance(item, dict)]


LLM_REQUEST_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    http.client.IncompleteRead,
    ConnectionError,
    OSError,
    KeyError,
    ValueError,
    json.JSONDecodeError,
)


def request_layer_llm_with_retry(
    batch: list[dict[str, Any]],
    *,
    api_key: str,
    base_url: str,
    model: str,
    max_retries: int = LAYER_LLM_MAX_RETRIES,
) -> list[dict[str, Any]]:
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return request_layer_llm(batch, api_key=api_key, base_url=base_url, model=model)
        except LLM_REQUEST_ERRORS as exc:
            last_error = exc
            if attempt >= max_retries:
                break
            time.sleep(min(2.0 ** attempt, 8.0))
    assert last_error is not None
    raise last_error


def request_layer_llm_resilient(
    batch: list[dict[str, Any]],
    *,
    api_key: str,
    base_url: str,
    model: str,
) -> list[dict[str, Any]]:
    try:
        return request_layer_llm_with_retry(batch, api_key=api_key, base_url=base_url, model=model)
    except LLM_REQUEST_ERRORS:
        if len(batch) <= 1:
            raise
        midpoint = max(1, len(batch) // 2)
        return (
            request_layer_llm_resilient(batch[:midpoint], api_key=api_key, base_url=base_url, model=model)
            + request_layer_llm_resilient(batch[midpoint:], api_key=api_key, base_url=base_url, model=model)
        )


def load_layer_llm_cache(path: Path, model: str) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if str(payload.get("model") or "") != model:
        return {}
    if str(payload.get("prompt_version") or "") != LAYER_LLM_PROMPT_VERSION:
        return {}
    cached = payload.get("decisions", {})
    if not isinstance(cached, dict):
        return {}
    return {
        str(layer): value
        for layer, value in cached.items()
        if isinstance(value, dict)
        and (
            value.get("llm_returned") is True
            or str(value.get("reason") or "") != "LLM did not return this layer"
        )
    }


def write_layer_llm_output(
    path: Path,
    *,
    model: str,
    base_url: str,
    summaries: list[dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
    complete: bool,
) -> None:
    view = dict(decisions)
    for summary in summaries:
        layer = summary["layer"]
        view.setdefault(
            layer,
            {
                "layer": layer,
                "role": "unknown",
                "candidate_types": [],
                "confidence": 0.0,
                "reason": "LLM did not return this layer",
                "source": "llm_layer_semantic",
                "model": model,
                "llm_returned": False,
            },
        )
    output = {
        "model": model,
        "base_url": base_url,
        "prompt_version": LAYER_LLM_PROMPT_VERSION,
        "strategy": "llm_layer_hit_direct_output_geometry_fallback_for_unhit_layers",
        "complete": complete,
        "layer_count": len(summaries),
        "decided_layer_count": sum(1 for item in view.values() if item.get("llm_returned") is True),
        "role_counts": dict(Counter(item["role"] for item in view.values()).most_common()),
        "candidate_type_counts": dict(
            Counter(kind for item in view.values() for kind in item.get("candidate_types", [])).most_common()
        ),
        "decisions": view,
        "layer_summaries": summaries,
    }
    write_json(path, output)


def classify_layers_by_llm(rows: list[dict[str, str]], output_dir: Path) -> dict[str, dict[str, Any]]:
    api_key, base_url, model = llm_runtime_config()
    if not api_key:
        raise RuntimeError("障碍物图层判别必须调用 LLM，请先设置 DEEPSEEK_API_KEY 或 OPENAI_API_KEY。")

    summaries = layer_summary(rows)
    cache_path = output_dir / LAYER_LLM_FILE
    decisions: dict[str, dict[str, Any]] = load_layer_llm_cache(cache_path, model)
    pending_summaries = [
        summary
        for summary in summaries
        if decisions.get(summary["layer"], {}).get("llm_returned") is not True
    ]
    batch_size = LAYER_LLM_BATCH_SIZE
    for start in range(0, len(pending_summaries), batch_size):
        batch = pending_summaries[start:start + batch_size]
        try:
            raw_decisions = request_layer_llm_resilient(batch, api_key=api_key, base_url=base_url, model=model)
        except LLM_REQUEST_ERRORS as exc:
            write_layer_llm_output(
                cache_path,
                model=model,
                base_url=base_url,
                summaries=summaries,
                decisions=decisions,
                complete=False,
            )
            raise RuntimeError(f"LLM 图层障碍物相关性判别失败，已缓存完成批次: {exc}") from exc
        for item in raw_decisions:
            layer = str(item.get("layer") or "")
            if not layer:
                continue
            role = str(item.get("role") or "unknown")
            candidate_types = [
                str(value)
                for value in item.get("candidate_types", []) or []
                if str(value) in {"wall", "column", "filled_obstacle"}
            ]
            if role != "obstacle_candidate":
                candidate_types = []
            decisions[layer] = {
                "layer": layer,
                "role": role,
                "candidate_types": candidate_types,
                "confidence": float(item.get("confidence") or 0.0),
                "reason": str(item.get("reason") or ""),
                "source": "llm_layer_semantic",
                "model": model,
                "llm_returned": True,
            }
        write_layer_llm_output(
            cache_path,
            model=model,
            base_url=base_url,
            summaries=summaries,
            decisions=decisions,
            complete=False,
        )

    for summary in summaries:
        layer = summary["layer"]
        decisions.setdefault(
            layer,
            {
                "layer": layer,
                "role": "unknown",
                "candidate_types": [],
                "confidence": 0.0,
                "reason": "LLM did not return this layer",
                "source": "llm_layer_semantic",
                "model": model,
                "llm_returned": False,
            },
        )

    output = {
        "model": model,
        "base_url": base_url,
        "prompt_version": LAYER_LLM_PROMPT_VERSION,
        "strategy": "llm_layer_hit_direct_output_geometry_fallback_for_unhit_layers",
        "complete": True,
        "layer_count": len(summaries),
        "decided_layer_count": sum(1 for item in decisions.values() if item.get("llm_returned") is True),
        "role_counts": dict(Counter(item["role"] for item in decisions.values()).most_common()),
        "candidate_type_counts": dict(
            Counter(kind for item in decisions.values() for kind in item.get("candidate_types", [])).most_common()
        ),
        "decisions": decisions,
        "layer_summaries": summaries,
    }
    write_json(output_dir / LAYER_LLM_FILE, output)
    return decisions


def row_candidate_types(row: dict[str, str], decisions: dict[str, dict[str, Any]]) -> list[str]:
    decision = decisions.get(str(row.get("layer") or ""))
    if not decision or decision.get("role") != "obstacle_candidate":
        return []
    return [str(value) for value in decision.get("candidate_types", []) or []]


def is_passable_opening_layer(row: dict[str, str], decisions: dict[str, dict[str, Any]]) -> bool:
    decision = decisions.get(str(row.get("layer") or ""))
    return bool(decision and decision.get("role") == "passable_opening")


def needs_geometry_fallback(row: dict[str, str], decisions: dict[str, dict[str, Any]]) -> bool:
    decision = decisions.get(str(row.get("layer") or ""))
    if not decision:
        return True
    if row_candidate_types(row, decisions) or decision.get("role") in {"passable_opening", "not_obstacle"}:
        return False
    return decision.get("llm_returned") is not True or decision.get("role") == "unknown"


def choose_llm_obstacle_type(candidate_types: list[str], entity_type: str) -> str:
    if "column" in candidate_types and entity_type == "CIRCLE":
        return "column"
    if "wall" in candidate_types:
        return "wall"
    if "column" in candidate_types:
        return "column"
    return "filled_obstacle"


def rows_in_regions(row: dict[str, str], regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bounds = bbox_from_row(row)
    if not bounds:
        return []
    center = Point(*bbox_center(bounds))
    matched = []
    for region in regions:
        if region["polygon"].contains(center) or bbox_intersects(bounds, region["bbox"]):
            matched.append(region)
    return matched


def pseudo_region_for_row(row: dict[str, str]) -> dict[str, Any]:
    bounds = bbox_from_row(row) or (0.0, 0.0, 1.0, 1.0)
    return {
        "sheet_id": "UNSCOPED",
        "floor_id": "UNSCOPED",
        "floor_name": "UNSCOPED",
        "region_id": "ALL",
        "full_region_id": "UNSCOPED:ALL",
        "bbox": bounds,
        "polygon": box(*bounds),
    }


def primary_region_for_row(row: dict[str, str], regions: list[dict[str, Any]]) -> dict[str, Any]:
    bounds = bbox_from_row(row)
    if not bounds:
        return pseudo_region_for_row(row)
    center = Point(*bbox_center(bounds))
    for region in regions:
        if region["polygon"].contains(center):
            return region
    row_box = box(*bounds)
    best_region: dict[str, Any] | None = None
    best_area = 0.0
    for region in regions:
        try:
            area = row_box.intersection(region["polygon"]).area
        except Exception:
            area = 0.0
        if area > best_area:
            best_area = area
            best_region = region
    return best_region if best_region is not None and best_area > 0 else pseudo_region_for_row(row)


def geom_ratio_in_region(geom: Any, region: dict[str, Any]) -> float:
    return float(geom.area) / max(float(region["polygon"].area), 1.0)


def aspect_ratio(bounds: tuple[float, float, float, float]) -> float:
    minx, miny, maxx, maxy = bounds
    width = max(maxx - minx, 1.0)
    height = max(maxy - miny, 1.0)
    return max(width / height, height / width)


def validate_polygon_obstacle(geom: Any, region: dict[str, Any], config: ObstacleConfig) -> bool:
    if geom.is_empty or geom.area < config.min_area:
        return False
    if geom_ratio_in_region(geom, region) > config.max_region_area_ratio:
        return False
    if aspect_ratio(tuple(float(value) for value in geom.bounds)) > config.max_aspect_ratio:
        return False
    return True


def add_obstacle(
    out: list[dict[str, Any]],
    *,
    row: dict[str, str],
    region: dict[str, Any],
    geom: Any,
    obstacle_type: str,
    reason: str,
    confidence: float,
) -> None:
    bounds = tuple(float(value) for value in geom.bounds)
    out.append(
        {
            "obstacle_id": f"OBS_{len(out) + 1:06d}",
            "object_id": row.get("object_id", ""),
            "handle": row.get("handle", ""),
            "source": row.get("source", ""),
            "sheet_id": region["sheet_id"],
            "floor_id": region["floor_id"],
            "floor_name": region["floor_name"],
            "region_id": region["region_id"],
            "full_region_id": region["full_region_id"],
            "obstacle_type": obstacle_type,
            "reason": reason,
            "confidence": confidence,
            "layer": row.get("layer", ""),
            "entity_type": row.get("entity_type", ""),
            "geometry_kind": row.get("geometry_kind", ""),
            "color": row.get("color", ""),
            "linetype": row.get("linetype", ""),
            "parent_block_name": row.get("parent_block_name", ""),
            "bbox_minx": bounds[0],
            "bbox_miny": bounds[1],
            "bbox_maxx": bounds[2],
            "bbox_maxy": bounds[3],
            "area": float(geom.area),
            "geometry": geom,
        }
    )


def llm_direct_geometries_from_row(row: dict[str, str], config: ObstacleConfig) -> list[Any]:
    polygons = polygons_from_row(row, config)
    if polygons:
        return polygons

    line_geoms: list[Any] = []
    for line in lines_from_row(row, config, min_length=0.0):
        try:
            buffered = line.buffer(config.direct_line_buffer, cap_style=2, join_style=2)
        except Exception:
            continue
        if not buffered.is_empty and buffered.area >= config.min_area:
            line_geoms.append(buffered)
    return line_geoms


def detect_llm_hit_obstacles(
    rows: list[dict[str, str]],
    regions: list[dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
    config: ObstacleConfig,
) -> list[dict[str, Any]]:
    """LLM obstacle layers are trusted semantically; geometry is only converted for output."""

    out: list[dict[str, Any]] = []
    for row in rows:
        candidate_types = row_candidate_types(row, decisions)
        if not candidate_types:
            continue
        decision = decisions.get(str(row.get("layer") or ""), {})
        entity_type = str(row.get("entity_type") or "").upper()
        obstacle_type = choose_llm_obstacle_type(candidate_types, entity_type)
        geometries = llm_direct_geometries_from_row(row, config)
        if not geometries:
            continue
        region = primary_region_for_row(row, regions)
        for geom in geometries:
            parts = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
            for part in parts:
                if not isinstance(part, Polygon) or part.is_empty:
                    continue
                add_obstacle(
                    out,
                    row=row,
                    region=region,
                    geom=part,
                    obstacle_type=obstacle_type,
                    reason="llm_layer_obstacle_hit_direct_geometry_unfiltered_review",
                    confidence=float(decision.get("confidence") or 0.9),
                )
    return out


def detect_polygon_and_fill_obstacles(
    rows: list[dict[str, str]],
    regions: list[dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
    config: ObstacleConfig,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        candidate_types = row_candidate_types(row, decisions)
        if not candidate_types:
            continue
        entity_type = str(row.get("entity_type") or "").upper()
        polygons = polygons_from_row(row, config)
        if not polygons:
            continue
        for region in rows_in_regions(row, regions):
            for poly in polygons:
                clipped = poly.intersection(region["polygon"])
                if isinstance(clipped, MultiPolygon):
                    geoms = list(clipped.geoms)
                else:
                    geoms = [clipped]
                for geom in geoms:
                    if not isinstance(geom, Polygon):
                        continue
                    if not validate_polygon_obstacle(geom, region, config):
                        continue
                    if "column" in candidate_types and (entity_type == "CIRCLE" or aspect_ratio(tuple(geom.bounds)) <= 4.0):
                        add_obstacle(
                            out,
                            row=row,
                            region=region,
                            geom=geom,
                            obstacle_type="column",
                            reason="llm_layer_column_candidate_geometry_circle_or_near_rect",
                            confidence=0.86,
                        )
                    elif "wall" in candidate_types:
                        add_obstacle(
                            out,
                            row=row,
                            region=region,
                            geom=geom,
                            obstacle_type="wall",
                            reason="llm_layer_wall_candidate_polygon_or_hatch",
                            confidence=0.82,
                        )
                    elif "filled_obstacle" in candidate_types:
                        add_obstacle(
                            out,
                            row=row,
                            region=region,
                            geom=geom,
                            obstacle_type="filled_obstacle",
                            reason="llm_layer_fill_candidate_polygon_or_hatch",
                            confidence=0.78,
                        )
    return out


def detect_geometry_fallback_polygon_obstacles(
    rows: list[dict[str, str]],
    regions: list[dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
    config: ObstacleConfig,
) -> list[dict[str, Any]]:
    """Geometry-only fallback for layers not semantically hit by the LLM."""

    out: list[dict[str, Any]] = []
    for row in rows:
        if not needs_geometry_fallback(row, decisions):
            continue
        entity_type = str(row.get("entity_type") or "").upper()
        polygons = polygons_from_row(row, config)
        if not polygons:
            continue
        for region in rows_in_regions(row, regions):
            for poly in polygons:
                clipped = poly.intersection(region["polygon"])
                geoms = list(clipped.geoms) if isinstance(clipped, MultiPolygon) else [clipped]
                for geom in geoms:
                    if not isinstance(geom, Polygon):
                        continue
                    if not validate_polygon_obstacle(geom, region, config):
                        continue
                    if entity_type == "CIRCLE" or aspect_ratio(tuple(geom.bounds)) <= 4.0:
                        obstacle_type = "column"
                        reason = "geometry_fallback_closed_or_round_column"
                        confidence = 0.68
                    else:
                        obstacle_type = "filled_obstacle"
                        reason = "geometry_fallback_valid_polygon_or_fill"
                        confidence = 0.62
                    add_obstacle(
                        out,
                        row=row,
                        region=region,
                        geom=geom,
                        obstacle_type=obstacle_type,
                        reason=reason,
                        confidence=confidence,
                    )
    return out


def normalized_angle(line: LineString) -> float:
    (x1, y1), (x2, y2) = line.coords[0], line.coords[-1]
    return math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180.0


def angle_delta(a: float, b: float) -> float:
    value = abs((a - b) % 180.0)
    return min(value, 180.0 - value)


def direction_and_normal(angle: float) -> tuple[tuple[float, float], tuple[float, float]]:
    rad = math.radians(angle)
    direction = (math.cos(rad), math.sin(rad))
    normal = (-math.sin(rad), math.cos(rad))
    return direction, normal


def line_offset(line: LineString, normal: tuple[float, float]) -> float:
    x, y = line.interpolate(0.5, normalized=True).coords[0]
    return x * normal[0] + y * normal[1]


def line_projection(line: LineString, direction: tuple[float, float]) -> tuple[float, float]:
    values = [x * direction[0] + y * direction[1] for x, y in line.coords]
    return min(values), max(values)


def same_line_attr(row: dict[str, str]) -> tuple[str, str, str]:
    return str(row.get("layer") or ""), str(row.get("color") or ""), str(row.get("linetype") or "")


def build_parallel_wall_polygon(lines: list[LineString], config: ObstacleConfig) -> Polygon | None:
    if not (config.min_parallel_lines <= len(lines) <= config.max_parallel_lines):
        return None
    angles = [normalized_angle(line) for line in lines]
    base = angles[0]
    if any(angle_delta(base, angle) > config.parallel_angle_tolerance_deg for angle in angles[1:]):
        return None
    direction, normal = direction_and_normal(base)
    offsets = sorted(line_offset(line, normal) for line in lines)
    width = offsets[-1] - offsets[0]
    if width <= 0 or width > config.max_parallel_bundle_width:
        return None
    if len(offsets) >= 3:
        gaps = [b - a for a, b in zip(offsets, offsets[1:])]
        avg_gap = sum(gaps) / len(gaps)
        if avg_gap <= 0:
            return None
        if max(abs(gap - avg_gap) for gap in gaps) > max(10.0, avg_gap * config.parallel_spacing_tolerance_ratio):
            return None
    intervals = [line_projection(line, direction) for line in lines]
    start = max(item[0] for item in intervals)
    end = min(item[1] for item in intervals)
    if end - start < config.min_parallel_overlap:
        starts = sorted(item[0] for item in intervals)
        ends = sorted(item[1] for item in intervals)
        start = starts[len(starts) // 2]
        end = ends[len(ends) // 2]
    if end <= start or end - start < config.min_parallel_overlap:
        return None

    pad = max(15.0, min(100.0, width * 0.15))
    o1 = offsets[0] - pad
    o2 = offsets[-1] + pad
    coords = [
        (direction[0] * start + normal[0] * o1, direction[1] * start + normal[1] * o1),
        (direction[0] * end + normal[0] * o1, direction[1] * end + normal[1] * o1),
        (direction[0] * end + normal[0] * o2, direction[1] * end + normal[1] * o2),
        (direction[0] * start + normal[0] * o2, direction[1] * start + normal[1] * o2),
    ]
    try:
        poly = Polygon(coords)
        if not poly.is_valid:
            poly = poly.buffer(0)
        return poly if not poly.is_empty and poly.area >= config.min_area else None
    except Exception:
        return None


def known_wall_union(obstacles: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for item in obstacles:
        if item["obstacle_type"] == "wall":
            grouped[item["full_region_id"]].append(item["geometry"])
    return {key: unary_union(values) for key, values in grouped.items() if values}


def connected_to_wall(geom: Any, region_id: str, walls: dict[str, Any], config: ObstacleConfig) -> bool:
    wall = walls.get(region_id)
    if wall is None or wall.is_empty:
        return True
    if geom.intersects(wall):
        return True
    return geom.buffer(config.wall_connect_buffer).intersects(wall)


def detect_parallel_wall_obstacles(
    rows: list[dict[str, str]],
    regions: list[dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
    base_wall_union: dict[str, Any],
    config: ObstacleConfig,
    *,
    fallback_only: bool = False,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, tuple[str, str, str]], list[tuple[dict[str, str], LineString]]] = defaultdict(list)
    for row in rows:
        if fallback_only:
            if not needs_geometry_fallback(row, decisions):
                continue
        elif "wall" not in row_candidate_types(row, decisions):
            continue
        entity_type = str(row.get("entity_type") or "").upper()
        if entity_type not in {"LINE", "LWPOLYLINE", "POLYLINE"}:
            continue
        regions_for_row = rows_in_regions(row, regions)
        if not regions_for_row:
            continue
        for line in lines_from_row(row, config):
            for segment in split_to_segments(line, config):
                for region in regions_for_row:
                    grouped[(region["full_region_id"], same_line_attr(row))].append((row, segment))

    region_by_id = {region["full_region_id"]: region for region in regions}
    out: list[dict[str, Any]] = []
    accepted: list[Any] = []
    for (region_id, _attr), items in grouped.items():
        if len(items) < config.min_parallel_lines:
            continue
        angle_groups: list[list[tuple[dict[str, str], LineString]]] = []
        for row, segment in items:
            angle = normalized_angle(segment)
            for bucket in angle_groups:
                if angle_delta(normalized_angle(bucket[0][1]), angle) <= config.parallel_angle_tolerance_deg:
                    bucket.append((row, segment))
                    break
            else:
                angle_groups.append([(row, segment)])

        region = region_by_id.get(region_id)
        if not region:
            continue
        for bucket in angle_groups:
            base_angle = normalized_angle(bucket[0][1])
            _direction, normal = direction_and_normal(base_angle)
            bucket = sorted(bucket, key=lambda item: line_offset(item[1], normal))
            for size in range(min(config.max_parallel_lines, len(bucket)), config.min_parallel_lines - 1, -1):
                for start in range(0, len(bucket) - size + 1):
                    window = bucket[start:start + size]
                    poly = build_parallel_wall_polygon([item[1] for item in window], config)
                    if poly is None:
                        continue
                    clipped = poly.intersection(region["polygon"])
                    if not validate_polygon_obstacle(clipped, region, config):
                        continue
                    if not connected_to_wall(clipped, region_id, base_wall_union, config):
                        continue
                    if any(clipped.intersection(item).area / max(clipped.area, 1.0) > 0.75 for item in accepted):
                        continue
                    add_obstacle(
                        out,
                        row=window[0][0],
                        region=region,
                        geom=clipped,
                        obstacle_type="wall",
                        reason=(
                            "geometry_fallback_parallel_line_topology"
                            if fallback_only
                            else "llm_layer_wall_candidate_parallel_line_topology"
                        ),
                        confidence=0.66 if fallback_only else 0.74,
                    )
                    accepted.append(clipped)
    return out


def opening_union_by_region(
    rows: list[dict[str, str]],
    regions: list[dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
    config: ObstacleConfig,
) -> dict[str, Any]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        decision = decisions.get(str(row.get("layer") or ""))
        if not decision or decision.get("role") != "passable_opening":
            continue
        bounds = bbox_from_row(row)
        if not bounds:
            continue
        mask = box(*bounds).buffer(config.opening_mask_buffer)
        for region in rows_in_regions(row, regions):
            grouped[region["full_region_id"]].append(mask)
    return {key: unary_union(values) for key, values in grouped.items() if values}


def subtract_openings(obstacles: list[dict[str, Any]], openings: dict[str, Any], config: ObstacleConfig) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for item in obstacles:
        geom = item["geometry"]
        opening = openings.get(item["full_region_id"])
        if opening is not None and not opening.is_empty:
            try:
                geom = geom.difference(opening)
            except Exception:
                pass
        geoms = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
        for part in geoms:
            if not isinstance(part, Polygon) or part.is_empty or part.area < config.min_area:
                continue
            copy = dict(item)
            copy["geometry"] = part
            bounds = tuple(float(value) for value in part.bounds)
            copy.update(
                {
                    "bbox_minx": bounds[0],
                    "bbox_miny": bounds[1],
                    "bbox_maxx": bounds[2],
                    "bbox_maxy": bounds[3],
                    "area": float(part.area),
                }
            )
            cleaned.append(copy)
    return cleaned


def dedupe_obstacles(obstacles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    grid: dict[tuple[str, str, int, int], list[int]] = defaultdict(list)
    cell_size = 2000.0

    def cells_for_bounds(bounds: tuple[float, float, float, float]) -> list[tuple[int, int]]:
        minx, miny, maxx, maxy = bounds
        ix1 = math.floor(minx / cell_size)
        iy1 = math.floor(miny / cell_size)
        ix2 = math.floor(maxx / cell_size)
        iy2 = math.floor(maxy / cell_size)
        return [(ix, iy) for ix in range(ix1, ix2 + 1) for iy in range(iy1, iy2 + 1)]

    for item in sorted(obstacles, key=lambda value: (value["confidence"], value["area"]), reverse=True):
        geom = item["geometry"]
        duplicate = False
        region_id = str(item["full_region_id"])
        obstacle_type = str(item["obstacle_type"])
        candidate_indices: set[int] = set()
        item_cells = cells_for_bounds(tuple(float(value) for value in geom.bounds))
        for ix, iy in item_cells:
            candidate_indices.update(grid.get((region_id, obstacle_type, ix, iy), []))
        for kept_index in candidate_indices:
            existing = kept[kept_index]
            if existing["full_region_id"] != item["full_region_id"]:
                continue
            inter = geom.intersection(existing["geometry"]).area
            if inter / max(min(geom.area, existing["geometry"].area), 1.0) > 0.75:
                duplicate = True
                break
        if duplicate:
            continue
        copy = dict(item)
        copy["obstacle_id"] = f"OBS_{len(kept) + 1:06d}"
        kept.append(copy)
        kept_index = len(kept) - 1
        for ix, iy in item_cells:
            grid[(region_id, obstacle_type, ix, iy)].append(kept_index)
    return kept


def obstacle_csv_row(item: dict[str, Any]) -> dict[str, Any]:
    row = dict(item)
    row.pop("geometry", None)
    return row


def write_obstacle_csv(path: Path, obstacles: list[dict[str, Any]]) -> None:
    fields = [
        "obstacle_id", "object_id", "handle", "source", "sheet_id", "floor_id",
        "floor_name", "region_id", "full_region_id", "obstacle_type", "reason",
        "confidence", "layer", "entity_type", "geometry_kind", "color", "linetype",
        "parent_block_name", "bbox_minx", "bbox_miny", "bbox_maxx", "bbox_maxy",
        "area",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in obstacles:
            row = obstacle_csv_row(item)
            writer.writerow({field: row.get(field, "") for field in fields})


def write_geojson_outputs(output_dir: Path, obstacles: list[dict[str, Any]]) -> tuple[list[Path], list[Path]]:
    per_region_dir = output_dir / "per_region_geojson"
    per_floor_dir = output_dir / "per_floor_union"
    per_region_dir.mkdir(parents=True, exist_ok=True)
    per_floor_dir.mkdir(parents=True, exist_ok=True)

    per_region: dict[str, list[dict[str, Any]]] = defaultdict(list)
    per_floor: dict[str, list[Any]] = defaultdict(list)
    for item in obstacles:
        per_region[item["full_region_id"]].append(item)
        per_floor[str(item.get("floor_id") or item.get("sheet_id") or "UNKNOWN")].append(item["geometry"])

    region_paths: list[Path] = []
    for region_id, items in per_region.items():
        safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", region_id)
        path = per_region_dir / f"{safe}.geojson"
        write_json(
            path,
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": obstacle_csv_row(item),
                        "geometry": mapping(item["geometry"]),
                    }
                    for item in items
                ],
            },
        )
        region_paths.append(path)

    union_paths: list[Path] = []
    for floor_id, geoms in per_floor.items():
        path = per_floor_dir / f"valid_obstacle_union_{floor_id}.geojson"
        write_json(
            path,
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"floor_id": floor_id, "obstacle_count": len(geoms)},
                        "geometry": mapping(unary_union(geoms)),
                    }
                ],
            },
        )
        union_paths.append(path)
    return region_paths, union_paths


def add_layer(doc: Any, name: str, color: int) -> None:
    if name not in doc.layers:
        doc.layers.add(name=name, color=color)
    try:
        doc.layers.get(name).dxf.color = color
    except Exception:
        pass


def add_label(msp: Any, text: str, point: tuple[float, float], height: float, layer: str, color: int) -> None:
    entity = msp.add_text(text, dxfattribs={"layer": layer, "height": height, "color": color})
    try:
        entity.set_placement(point)
    except Exception:
        entity.dxf.insert = point


def write_obstacle_review_dxf(input_dxf: Path, output_dxf: Path, obstacles: list[dict[str, Any]]) -> Path:
    doc = ezdxf.readfile(input_dxf)
    msp = doc.modelspace()
    add_layer(doc, WALL_LAYER, 1)
    add_layer(doc, COLUMN_LAYER, 1)
    add_layer(doc, FILL_LAYER, 1)
    add_layer(doc, TEXT_LAYER, 1)
    layer_by_type = {"wall": WALL_LAYER, "column": COLUMN_LAYER, "filled_obstacle": FILL_LAYER}
    color_by_type = {"wall": 1, "column": 1, "filled_obstacle": 1}

    for index, item in enumerate(obstacles, start=1):
        layer = layer_by_type.get(item["obstacle_type"], WALL_LAYER)
        color = color_by_type.get(item["obstacle_type"], 1)
        geom = item["geometry"]
        polygons = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
        for poly in polygons:
            if not isinstance(poly, Polygon) or poly.is_empty:
                continue
            coords = [(float(x), float(y)) for x, y in poly.exterior.coords]
            entity = msp.add_lwpolyline(coords, close=True, dxfattribs={"layer": layer, "color": color})
            try:
                entity.dxf.const_width = 30
            except Exception:
                pass
        minx, _miny, _maxx, maxy = item["geometry"].bounds
        add_label(
            msp,
            f"{index:03d} {item['obstacle_type']}",
            (float(minx), float(maxy) + 220.0),
            220.0,
            TEXT_LAYER,
            color,
        )

    output_dxf.parent.mkdir(parents=True, exist_ok=True)
    try:
        doc.saveas(output_dxf)
    except PermissionError:
        output_dxf = timestamped_output_path(output_dxf)
        doc.saveas(output_dxf)
    return output_dxf


def recognize_floor_obstacles(
    input_dxf: Path | str,
    inventory_dir: Path | str,
    sheets_json: Path | str,
    output_dir: Path | str,
    *,
    write_review_dxf: bool = True,
    config: ObstacleConfig = ObstacleConfig(),
) -> ObstacleRecognitionResult:
    input_path = Path(input_dxf).expanduser().resolve()
    inventory_path = Path(inventory_dir).resolve()
    sheets_path = Path(sheets_json).resolve()
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = load_geometry_rows(inventory_path)
    regions = load_floor_regions(sheets_path)
    if not regions:
        raise RuntimeError("没有可用于障碍物识别的楼层可巡检区域。")

    layer_decisions = classify_layers_by_llm(rows, out_dir)
    llm_hit_obstacles = detect_llm_hit_obstacles(rows, regions, layer_decisions, config)
    obstacles = llm_hit_obstacles

    obstacle_csv = out_dir / OBSTACLE_CSV_FILE
    write_obstacle_csv(obstacle_csv, obstacles)
    per_region_geojsons, union_geojsons = write_geojson_outputs(out_dir, obstacles)

    marked_dxf: Path | None = None
    if write_review_dxf:
        marked_dxf = write_review_dxf_file = out_dir.parent / "review" / f"{input_path.stem}_obstacles_marked.dxf"
        marked_dxf = write_obstacle_review_dxf(input_path, write_review_dxf_file, obstacles)

    type_counts = Counter(item["obstacle_type"] for item in obstacles)
    region_counts = Counter(item["full_region_id"] for item in obstacles)
    result_json = out_dir / RESULT_JSON_FILE
    write_json(
        result_json,
        {
            "input_dxf": str(input_path),
            "inventory_dir": str(inventory_path),
            "sheets_json": str(sheets_path),
            "output_dir": str(out_dir),
            "strategy": "Unfiltered review: LLM-hit obstacle layers output directly without region clipping, line-length filtering, opening subtraction, or dedupe",
            "layer_llm_decisions": str((out_dir / LAYER_LLM_FILE).resolve()),
            "obstacle_csv": str(obstacle_csv.resolve()),
            "marked_dxf": str(marked_dxf.resolve()) if marked_dxf else "",
            "obstacle_count": len(obstacles),
            "obstacle_type_count": len(type_counts),
            "region_count": len(regions),
            "type_counts": dict(type_counts.most_common()),
            "region_counts": dict(region_counts.most_common()),
            "per_region_geojsons": [str(path.resolve()) for path in per_region_geojsons],
            "union_geojsons": [str(path.resolve()) for path in union_geojsons],
        },
    )
    return ObstacleRecognitionResult(
        result_json=result_json,
        obstacle_csv=obstacle_csv,
        marked_dxf=marked_dxf,
        obstacle_count=len(obstacles),
        obstacle_type_count=len(type_counts),
        region_count=len(regions),
        per_region_geojsons=per_region_geojsons,
        union_geojsons=union_geojsons,
    )
