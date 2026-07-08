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
LAYER_LLM_PROMPT_VERSION = "obstacle-layer-v5-parallel-wall-lines"
LAYER_LLM_BATCH_SIZE = 20
LAYER_LLM_MAX_RETRIES = 3

WALL_LAYER = "CHECK_OBSTACLE_WALL"
COLUMN_LAYER = "CHECK_OBSTACLE_COLUMN"
FILL_LAYER = "CHECK_OBSTACLE_FILL"
TEXT_LAYER = "CHECK_OBSTACLE_TEXT"


@dataclass(frozen=True)
class ObstacleConfig:
    """Scale-invariant controls for obstacle review geometry."""

    min_area: float = 0.0
    parallel_angle_tolerance_deg: float = 5.0
    min_projection_overlap_ratio: float = 0.05
    max_wall_width_to_overlap_ratio: float = 0.60


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
        if poly.is_empty or poly.area <= min_area:
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
    threshold = 0.0 if min_length is None else min_length
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


def joint_angle_deg(prev_point: tuple[float, float], joint: tuple[float, float], next_point: tuple[float, float]) -> float | None:
    ax = prev_point[0] - joint[0]
    ay = prev_point[1] - joint[1]
    bx = next_point[0] - joint[0]
    by = next_point[1] - joint[1]
    len_a = math.hypot(ax, ay)
    len_b = math.hypot(bx, by)
    if len_a <= 0 or len_b <= 0:
        return None
    dot = ax * bx + ay * by
    cosine = max(-1.0, min(1.0, dot / (len_a * len_b)))
    return math.degrees(math.acos(cosine))


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
        for index, item in enumerate(inspection_regions, start=1):
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or sheet.get("inspection_region_source") or "")
            if source in {"sheet_bbox", "sheet_bbox_fallback"} or source.startswith("sheet_bbox"):
                continue
            raw_bbox = item.get("bbox")
            if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4:
                continue
            bbox_tuple = tuple(float(value) for value in raw_bbox)
            if bbox_tuple[2] <= bbox_tuple[0] or bbox_tuple[3] <= bbox_tuple[1]:
                continue
            region_id = str(item.get("region_id") or f"R{index:02d}")
            confidence = safe_float(item.get("confidence"))
            if confidence is None:
                confidence = safe_float(sheet.get("inspection_region_confidence")) or 0.0
            regions.append(
                {
                    "sheet_id": sheet_id,
                    "floor_id": floor_id,
                    "floor_name": floor_name,
                    "region_id": region_id,
                    "full_region_id": f"{sheet_id}:{region_id}",
                    "bbox": bbox_tuple,
                    "polygon": box(*bbox_tuple),
                    "region_source": source,
                    "region_confidence": confidence,
                    "region_evidence": str(item.get("evidence") or sheet.get("inspection_region_evidence") or ""),
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
            "Return obstacle_candidate only when the layer name, block samples, or text samples clearly mean wall, column, curtain wall, structural wall, masonry/concrete wall, or structural filled obstacle.",
            "Positive obstacle evidence includes: WALL, COL, COLUMN, PILLAR, CONC, CONCRETE, MASONRY, BRICK, SHEARWALL, CURTAIN WALL, GLZ/GLZE when it means curtain wall glazing, 墙, 柱, 幕墙, 砌体, 混凝土, 剪力墙.",
            "Do not infer obstacle_candidate from elevator, stair, shaft, room, equipment, window, door, opening, passage, ramp, furniture, annotation, or symbol semantics unless the same layer also explicitly contains wall/column evidence.",
            "Dimension, axis, grid, title block, frame, legend, table, and text-only annotation layers are not obstacles.",
        ],
        "roles": {
            "obstacle_candidate": "Layer explicitly means wall, column, curtain wall, masonry/concrete wall, or structural filled obstacle.",
            "not_obstacle": "Annotation, axis, dimension, title, frame, furniture, symbol, or unrelated layer.",
            "unknown": "Insufficient semantic evidence.",
        },
        "candidate_types": ["wall", "column", "filled_obstacle"],
        "layers": batch,
        "output_schema": {
            "decisions": [
                {
                    "layer": "original layer name",
                    "role": "obstacle_candidate|not_obstacle|unknown",
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
        "strategy": "llm_layer_area_geometry_plus_parallel_wall_line_bundles",
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
        "strategy": "llm_layer_area_geometry_plus_parallel_wall_line_bundles",
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
    return [str(value).strip().lower() for value in decision.get("candidate_types", []) or [] if str(value).strip()]


def row_has_wall_semantic(row: dict[str, str], decisions: dict[str, dict[str, Any]]) -> bool:
    candidate_types = row_candidate_types(row, decisions)
    if "wall" in candidate_types:
        return True
    decision = decisions.get(str(row.get("layer") or ""))
    if not decision or decision.get("role") != "obstacle_candidate":
        return False
    semantic_text = " ".join(
        [
            str(row.get("layer") or ""),
            str(decision.get("reason") or ""),
            " ".join(str(value) for value in decision.get("candidate_types", []) or []),
        ]
    ).upper()
    return any(term in semantic_text for term in ("WALL", "CURTAIN", "GLZ", "GLZE", "GLAZ", "墙", "幕墙"))


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
    row_geometries: list[Any] | None = None
    matched = []
    for region in regions:
        region_polygon = region["polygon"]
        if region_polygon.covers(center):
            matched.append(region)
            continue
        if not bbox_intersects(bounds, region["bbox"]):
            continue
        if row_geometries is None:
            row_geometries = []
            row_geometries.extend(polygons_from_row(row, ObstacleConfig()))
            row_geometries.extend(lines_from_row(row, ObstacleConfig(), min_length=0.0))
        for geom in row_geometries:
            try:
                if geom.intersects(region_polygon):
                    matched.append(region)
                    break
            except Exception:
                continue
    return matched


def add_obstacle(
    out: list[dict[str, Any]],
    *,
    row: dict[str, str],
    region: dict[str, Any],
    geom: Any,
    obstacle_type: str,
    reason: str,
    confidence: float,
    semantic_evidence: str = "",
    geometry_evidence: str = "",
    topology_evidence: str = "",
    negative_evidence: str = "",
    fusion_decision: str = "",
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
            "semantic_evidence": semantic_evidence,
            "geometry_evidence": geometry_evidence,
            "topology_evidence": topology_evidence,
            "negative_evidence": negative_evidence,
            "fusion_decision": fusion_decision,
            "bbox_minx": bounds[0],
            "bbox_miny": bounds[1],
            "bbox_maxx": bounds[2],
            "bbox_maxy": bounds[3],
            "area": float(geom.area),
            "geometry": geom,
        }
    )


def semantic_evidence_for_row(row: dict[str, str], decisions: dict[str, dict[str, Any]]) -> str:
    decision = decisions.get(str(row.get("layer") or ""), {})
    role = str(decision.get("role") or "unknown")
    types = ",".join(str(value) for value in decision.get("candidate_types", []) or [])
    confidence = decision.get("confidence", "")
    reason = str(decision.get("reason") or "")
    return f"layer={row.get('layer','')}; role={role}; types={types}; confidence={confidence}; reason={reason}"


def is_dashed_linetype(row: dict[str, str]) -> bool:
    text = f"{row.get('linetype', '')} {row.get('geometry_kind', '')}".upper()
    return any(token in text for token in ("DASH", "HIDDEN", "PHANTOM", "CENTER", "DOT", "虚线"))


def open_line_segments_from_row(row: dict[str, str], config: ObstacleConfig) -> list[LineString]:
    entity_type = str(row.get("entity_type") or "").upper()
    if entity_type not in {"LINE", "LWPOLYLINE", "POLYLINE"}:
        return []
    if str(row.get("is_closed") or "").lower() in {"1", "true"}:
        return []
    if is_dashed_linetype(row):
        return []

    segments: list[LineString] = []
    for line in lines_from_row(row, config, min_length=0.0):
        coords = list(line.coords)
        for start, end in zip(coords, coords[1:]):
            try:
                segment = LineString([start, end])
            except Exception:
                continue
            if not segment.is_empty and segment.length > 0:
                segments.append(segment)
    return segments


def line_unit_vectors(line: LineString) -> tuple[tuple[float, float], tuple[float, float]] | None:
    start, end = line_endpoint_pair(line)
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy)
    if length <= 0:
        return None
    ux, uy = dx / length, dy / length
    if ux < 0 or (abs(ux) < 1e-9 and uy < 0):
        ux, uy = -ux, -uy
    return (ux, uy), (-uy, ux)


def line_angle_deg_mod_180(line: LineString) -> float | None:
    vectors = line_unit_vectors(line)
    if vectors is None:
        return None
    (ux, uy), _normal = vectors
    angle = math.degrees(math.atan2(uy, ux))
    if angle < 0:
        angle += 180.0
    if angle >= 180.0:
        angle -= 180.0
    return angle


def angle_distance_mod_180(a: float, b: float) -> float:
    diff = abs(a - b) % 180.0
    return min(diff, 180.0 - diff)


def project_point(point: tuple[float, float], axis: tuple[float, float]) -> float:
    return point[0] * axis[0] + point[1] * axis[1]


def projection_interval(line: LineString, axis: tuple[float, float]) -> tuple[float, float]:
    values = [project_point((float(x), float(y)), axis) for x, y in line.coords]
    return min(values), max(values)


def average_projection(line: LineString, axis: tuple[float, float]) -> float:
    values = [project_point((float(x), float(y)), axis) for x, y in line.coords]
    return sum(values) / len(values)


def overlap_interval(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float] | None:
    start = max(a[0], b[0])
    end = min(a[1], b[1])
    if end <= start:
        return None
    return start, end


def point_from_axes(
    along: float,
    across: float,
    direction: tuple[float, float],
    normal: tuple[float, float],
) -> tuple[float, float]:
    return (
        along * direction[0] + across * normal[0],
        along * direction[1] + across * normal[1],
    )


def parallel_wall_band_from_pair(
    line_a: LineString,
    line_b: LineString,
    config: ObstacleConfig,
) -> Polygon | None:
    angle_a = line_angle_deg_mod_180(line_a)
    angle_b = line_angle_deg_mod_180(line_b)
    if angle_a is None or angle_b is None:
        return None
    if angle_distance_mod_180(angle_a, angle_b) > config.parallel_angle_tolerance_deg:
        return None

    vectors = line_unit_vectors(line_a)
    if vectors is None:
        return None
    direction, normal = vectors
    interval = overlap_interval(projection_interval(line_a, direction), projection_interval(line_b, direction))
    if interval is None:
        return None
    overlap = interval[1] - interval[0]
    min_length = min(line_a.length, line_b.length)
    if overlap <= min_length * config.min_projection_overlap_ratio:
        return None

    across_a = average_projection(line_a, normal)
    across_b = average_projection(line_b, normal)
    width = abs(across_a - across_b)
    if width <= 0 or width > overlap * config.max_wall_width_to_overlap_ratio:
        return None

    low = min(across_a, across_b)
    high = max(across_a, across_b)
    try:
        polygon = Polygon(
            [
                point_from_axes(interval[0], low, direction, normal),
                point_from_axes(interval[1], low, direction, normal),
                point_from_axes(interval[1], high, direction, normal),
                point_from_axes(interval[0], high, direction, normal),
            ]
        )
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.is_empty or polygon.area <= config.min_area:
            return None
        return polygon
    except Exception:
        return None


def llm_area_geometries_from_row(row: dict[str, str], config: ObstacleConfig) -> list[Any]:
    return polygons_from_row(row, config)


def detect_llm_hit_obstacles(
    rows: list[dict[str, str]],
    regions: list[dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
    config: ObstacleConfig,
) -> list[dict[str, Any]]:
    """Output closed/area geometry from LLM-hit obstacle layers."""

    out: list[dict[str, Any]] = []
    for row in rows:
        candidate_types = row_candidate_types(row, decisions)
        if not candidate_types:
            continue
        decision = decisions.get(str(row.get("layer") or ""), {})
        entity_type = str(row.get("entity_type") or "").upper()
        obstacle_type = choose_llm_obstacle_type(candidate_types, entity_type)
        geometries = llm_area_geometries_from_row(row, config)
        if not geometries:
            continue
        matched_regions = rows_in_regions(row, regions)
        if not matched_regions:
            continue
        for geom in geometries:
            for region in matched_regions:
                try:
                    clipped = geom.intersection(region["polygon"])
                except Exception:
                    continue
                parts = list(clipped.geoms) if isinstance(clipped, MultiPolygon) else [clipped]
                for part in parts:
                    if not isinstance(part, Polygon) or part.is_empty:
                        continue
                    add_obstacle(
                        out,
                        row=row,
                        region=region,
                        geom=part,
                        obstacle_type=obstacle_type,
                        reason="llm_layer_obstacle_hit_direct_geometry_clipped_to_inspection_region",
                        confidence=float(decision.get("confidence") or 0.9),
                        semantic_evidence=semantic_evidence_for_row(row, decisions),
                        geometry_evidence="llm_hit_layer_area_geometry_clipped_to_inspection_region",
                        fusion_decision="accepted_by_llm_layer_area_geometry_then_region_clip",
                    )
    return out


def detect_parallel_wall_line_obstacles(
    rows: list[dict[str, str]],
    regions: list[dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
    config: ObstacleConfig,
) -> list[dict[str, Any]]:
    """Build wall obstacle polygons only from trusted-layer parallel line bundles."""

    wall_segments_by_layer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not row_has_wall_semantic(row, decisions):
            continue
        for segment in open_line_segments_from_row(row, config):
            angle = line_angle_deg_mod_180(segment)
            vectors = line_unit_vectors(segment)
            if angle is None or vectors is None:
                continue
            wall_segments_by_layer[str(row.get("layer") or "")].append(
                {
                    "row": row,
                    "line": segment,
                    "angle": angle,
                    "direction": vectors[0],
                    "normal": vectors[1],
                }
            )

    out: list[dict[str, Any]] = []
    used_pairs: set[tuple[str, str]] = set()
    for layer, segments in wall_segments_by_layer.items():
        if len(segments) < 2:
            continue
        segments = sorted(segments, key=lambda item: (round(item["angle"] / config.parallel_angle_tolerance_deg), item["line"].bounds))
        for index, first in enumerate(segments):
            best: tuple[float, int, Polygon] | None = None
            line_a = first["line"]
            direction = first["direction"]
            normal = first["normal"]
            interval_a = projection_interval(line_a, direction)
            across_a = average_projection(line_a, normal)
            for other_index in range(index + 1, len(segments)):
                second = segments[other_index]
                if angle_distance_mod_180(first["angle"], second["angle"]) > config.parallel_angle_tolerance_deg:
                    continue
                line_b = second["line"]
                interval = overlap_interval(interval_a, projection_interval(line_b, direction))
                if interval is None:
                    continue
                overlap = interval[1] - interval[0]
                if overlap <= min(line_a.length, line_b.length) * config.min_projection_overlap_ratio:
                    continue
                distance = abs(across_a - average_projection(line_b, normal))
                if distance <= 0 or distance > overlap * config.max_wall_width_to_overlap_ratio:
                    continue
                polygon = parallel_wall_band_from_pair(line_a, line_b, config)
                if polygon is None:
                    continue
                if best is None or distance < best[0]:
                    best = (distance, other_index, polygon)
            if best is None:
                continue

            other_index = best[1]
            second = segments[other_index]
            row_a = first["row"]
            row_b = second["row"]
            pair_key = tuple(
                sorted(
                    (
                        f"{row_a.get('object_id') or ''}:{index}:{tuple(round(v, 3) for v in line_a.bounds)}",
                        f"{row_b.get('object_id') or ''}:{other_index}:{tuple(round(v, 3) for v in second['line'].bounds)}",
                    )
                )
            )
            if pair_key in used_pairs:
                continue
            used_pairs.add(pair_key)

            decision = decisions.get(layer, {})
            polygon = best[2]
            for region in regions:
                try:
                    if not polygon.intersects(region["polygon"]):
                        continue
                    clipped = polygon.intersection(region["polygon"])
                except Exception:
                    continue
                parts = list(clipped.geoms) if isinstance(clipped, MultiPolygon) else [clipped]
                for part in parts:
                    if not isinstance(part, Polygon) or part.is_empty or part.area <= config.min_area:
                        continue
                    add_obstacle(
                        out,
                        row=row_a,
                        region=region,
                        geom=part,
                        obstacle_type="wall",
                        reason="llm_wall_layer_parallel_line_bundle_clipped_to_inspection_region",
                        confidence=float(decision.get("confidence") or 0.9),
                        semantic_evidence=semantic_evidence_for_row(row_a, decisions),
                        geometry_evidence=(
                            "parallel_wall_line_bundle;"
                            f"paired_object_id={row_b.get('object_id','')};"
                            f"paired_layer={row_b.get('layer','')};"
                            "non_dashed_open_straight_lines"
                        ),
                        topology_evidence="direction_parallel;local_adjacent;projection_overlap;trusted_wall_layer",
                        fusion_decision="accepted_by_llm_wall_layer_and_parallel_line_bundle",
                    )
    return out


def line_endpoint_pair(line: LineString) -> tuple[tuple[float, float], tuple[float, float]]:
    coords = list(line.coords)
    return (float(coords[0][0]), float(coords[0][1])), (float(coords[-1][0]), float(coords[-1][1]))


def point_distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def obstacle_csv_row(item: dict[str, Any]) -> dict[str, Any]:
    row = dict(item)
    row.pop("geometry", None)
    return row


def write_obstacle_csv(path: Path, obstacles: list[dict[str, Any]]) -> None:
    fields = [
        "obstacle_id", "object_id", "handle", "source", "sheet_id", "floor_id",
        "floor_name", "region_id", "full_region_id", "obstacle_type", "reason",
        "confidence", "layer", "entity_type", "geometry_kind", "color", "linetype",
        "parent_block_name", "semantic_evidence", "geometry_evidence", "topology_evidence",
        "negative_evidence", "fusion_decision", "bbox_minx", "bbox_miny", "bbox_maxx", "bbox_maxy",
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


def paired_object_ids_from_evidence(value: Any) -> list[str]:
    return [item.strip() for item in re.findall(r"paired_object_id=([^;]+)", str(value or "")) if item.strip()]


def flatten_review_geometries(geom: Any, geom_types: set[str]) -> list[Any]:
    if geom is None:
        return []
    try:
        if geom.is_empty:
            return []
    except Exception:
        return []
    geom_type = str(getattr(geom, "geom_type", ""))
    if geom_type in geom_types:
        return [geom]
    parts: list[Any] = []
    for part in getattr(geom, "geoms", []) or []:
        parts.extend(flatten_review_geometries(part, geom_types))
    return parts


def clip_review_geometry(geom: Any, clip_geom: Any) -> Any:
    try:
        # The generated obstacle geometry is already clipped to the inspection
        # region.  A tiny buffer keeps original CAD lines that lie exactly on the
        # generated polygon boundary from disappearing during intersection.
        return geom.intersection(clip_geom.buffer(1e-6))
    except Exception:
        return geom


def draw_review_line(msp: Any, line: LineString, layer: str, color: int) -> bool:
    coords = [(float(x), float(y)) for x, y in line.coords]
    if len(coords) < 2:
        return False
    entity = msp.add_lwpolyline(coords, close=False, dxfattribs={"layer": layer, "color": color})
    try:
        entity.dxf.const_width = 30
    except Exception:
        pass
    return True


def draw_review_polygon(msp: Any, poly: Polygon, layer: str, color: int) -> bool:
    if poly.is_empty:
        return False
    coords = [(float(x), float(y)) for x, y in poly.exterior.coords]
    if len(coords) < 4:
        return False
    entity = msp.add_lwpolyline(coords, close=True, dxfattribs={"layer": layer, "color": color})
    try:
        entity.dxf.const_width = 30
    except Exception:
        pass
    return True


def draw_review_source_row(msp: Any, row: dict[str, str], clip_geom: Any, layer: str, color: int) -> int:
    drawn = 0
    for line in lines_from_row(row, ObstacleConfig(), min_length=0.0):
        clipped = clip_review_geometry(line, clip_geom)
        for part in flatten_review_geometries(clipped, {"LineString"}):
            if getattr(part, "length", 0.0) > 0 and draw_review_line(msp, part, layer, color):
                drawn += 1
    if drawn:
        return drawn

    for poly in polygons_from_row(row, ObstacleConfig()):
        clipped = clip_review_geometry(poly, clip_geom)
        for part in flatten_review_geometries(clipped, {"Polygon"}):
            if draw_review_polygon(msp, part, layer, color):
                drawn += 1
    return drawn


def write_obstacle_review_dxf(
    input_dxf: Path,
    output_dxf: Path,
    obstacles: list[dict[str, Any]],
    source_rows: list[dict[str, str]] | None = None,
) -> Path:
    doc = ezdxf.readfile(input_dxf)
    msp = doc.modelspace()
    add_layer(doc, WALL_LAYER, 1)
    add_layer(doc, COLUMN_LAYER, 5)
    add_layer(doc, FILL_LAYER, 3)
    add_layer(doc, TEXT_LAYER, 1)
    layer_by_type = {"wall": WALL_LAYER, "column": COLUMN_LAYER, "filled_obstacle": FILL_LAYER}
    color_by_type = {"wall": 1, "column": 5, "filled_obstacle": 3}
    source_by_id = {str(row.get("object_id") or ""): row for row in source_rows or [] if row.get("object_id")}

    for index, item in enumerate(obstacles, start=1):
        layer = layer_by_type.get(item["obstacle_type"], WALL_LAYER)
        color = color_by_type.get(item["obstacle_type"], 1)
        geom = item["geometry"]
        drawn = 0
        source_ids = [str(item.get("object_id") or "")]
        if str(item.get("reason") or "").startswith("llm_wall_layer_parallel_line_bundle"):
            source_ids.extend(paired_object_ids_from_evidence(item.get("geometry_evidence")))

        seen_source_ids: set[str] = set()
        for source_id in source_ids:
            if not source_id or source_id in seen_source_ids:
                continue
            seen_source_ids.add(source_id)
            row = source_by_id.get(source_id)
            if row is not None:
                drawn += draw_review_source_row(msp, row, geom, layer, color)

        if not drawn:
            polygons = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
            for poly in polygons:
                if isinstance(poly, Polygon):
                    draw_review_polygon(msp, poly, layer, color)

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

    # LLM only decides trusted obstacle layers. Open wall lines must form
    # non-dashed parallel bundles before they become obstacle polygons.
    area_obstacles = detect_llm_hit_obstacles(rows, regions, layer_decisions, config)
    parallel_wall_obstacles = detect_parallel_wall_line_obstacles(rows, regions, layer_decisions, config)
    obstacles = area_obstacles + parallel_wall_obstacles

    obstacle_csv = out_dir / OBSTACLE_CSV_FILE
    write_obstacle_csv(obstacle_csv, obstacles)
    per_region_geojsons, union_geojsons = write_geojson_outputs(out_dir, obstacles)

    marked_dxf: Path | None = None
    if write_review_dxf:
        marked_dxf = write_review_dxf_file = out_dir.parent / "review" / f"{input_path.stem}_obstacles_marked.dxf"
        marked_dxf = write_obstacle_review_dxf(input_path, write_review_dxf_file, obstacles, rows)

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
            "strategy": "LLM classifies trusted obstacle layers; closed/area geometry is clipped to inspection regions; open wall lines are accepted only when they form non-dashed parallel wall bundles with projection overlap.",
            "layer_llm_decisions": str((out_dir / LAYER_LLM_FILE).resolve()),
            "obstacle_csv": str(obstacle_csv.resolve()),
            "marked_dxf": str(marked_dxf.resolve()) if marked_dxf else "",
            "obstacle_count": len(obstacles),
            "obstacle_type_count": len(type_counts),
            "region_count": len(regions),
            "stage_counts": {
                "area_obstacles": len(area_obstacles),
                "parallel_wall_line_bundle_obstacles": len(parallel_wall_obstacles),
                "final_obstacles": len(obstacles),
            },
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
