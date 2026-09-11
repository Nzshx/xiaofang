from __future__ import annotations

import json
import math
import re
import csv
import os
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import ezdxf
from ezdxf import bbox as ezbbox
from ezdxf.path import from_hatch
from shapely.geometry import LineString, Point, Polygon, box, mapping, shape
from shapely.ops import polygonize, unary_union

from ..common import Sheet, entity_point, read_json, write_csv, write_json


# Selective deterministic port of the architectural obstacle semantics used by
# the local ``fire_route_core`` modules. Unknown layers are not promoted merely
# because they contain geometry.
WALL_RE = re.compile(
    r"(?:^|[-_$])A[-_$]?WALL|WALL|WINDOW|CURTAIN|GLAZ|GLZ|MASONRY|BRICK|SHEAR|CONC|砌体|剪力墙|幕墙|墙体|结构墙|墙|窗",
    re.I,
)
COLUMN_RE = re.compile(r"(?:^|[-_$])(?:S[-_$]?)?COL(?:U|UMN)?|COLUMN|PILLAR|柱", re.I)
STRUCTURAL_FILL_RE = re.compile(r"STRUCT|CONCRETE|钢筋混凝土|混凝土|结构填充", re.I)
NEGATIVE_RE = re.compile(
    r"DOOR|OPENING|FURN|FURNITURE|EQUIP|SANIT|TITLE|FRAME|BORDER|AXIS|GRID|DIM|"
    r"ANNO|TEXT|NOTE|LEGEND|TABLE|门|洞口|家具|设备|洁具|图框|图签|轴|尺寸|标注|文字|说明|图例",
    re.I,
)
# Openings are uniformly non-walkable by project policy.  An explicit door
# entity with no opening/洞口 wording is recorded only for visual review; this
# adapter does not subtract it from the reference obstacle geometry.
DOOR_RE = re.compile(r"DOOR|门", re.I)
NON_PASSABLE_OPENING_RE = re.compile(r"OPENING|WALL[_ -]?OPEN|洞口|门洞", re.I)
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def classify_obstacle_layer(layer: str, block_name: str = "") -> tuple[str, float, str] | None:
    semantic = f"{layer}|{block_name}"
    if NEGATIVE_RE.search(semantic):
        return None
    if COLUMN_RE.search(semantic):
        return "column", 0.96, "结构柱图层/图块语义"
    if WALL_RE.search(semantic):
        return "wall", 0.95, "墙体图层/图块语义"
    if STRUCTURAL_FILL_RE.search(semantic):
        return "structural_fill", 0.90, "结构填充图层/图块语义"
    return None


def _find_sheet(sheets: list[Sheet], x: float, y: float) -> Sheet | None:
    candidates = [sheet for sheet in sheets if sheet.contains(x, y)]
    return min(
        candidates,
        key=lambda item: (item.max_x - item.min_x) * (item.max_y - item.min_y),
        default=None,
    )


def _entity_center(entity: Any) -> tuple[float, float] | None:
    point = entity_point(entity)
    if point:
        return point
    if entity.dxftype() == "POLYLINE":
        points = _polyline_points(entity)
        if points:
            return (
                sum(item[0] for item in points) / len(points),
                sum(item[1] for item in points) / len(points),
            )
    try:
        bounds = ezbbox.extents([entity], fast=True)
        if bounds.has_data:
            return (
                (float(bounds.extmin.x) + float(bounds.extmax.x)) / 2.0,
                (float(bounds.extmin.y) + float(bounds.extmax.y)) / 2.0,
            )
    except Exception:
        pass
    return None


def _polyline_points(entity: Any) -> list[tuple[float, float]]:
    try:
        if entity.dxftype() == "LWPOLYLINE":
            return [(float(x), float(y)) for x, y, *_ in entity.get_points("xy")]
        if entity.dxftype() == "POLYLINE":
            return [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in entity.vertices]
    except Exception:
        return []
    return []


def _polygon_parts(geometry: Any) -> Iterable[Polygon]:
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    if geometry.geom_type in {"MultiPolygon", "GeometryCollection"}:
        return [part for child in geometry.geoms for part in _polygon_parts(child)]
    return []


def _entity_geometries(entity: Any, obstacle_type: str, wall_half_width: float) -> list[Any]:
    kind = entity.dxftype()
    try:
        if kind == "LINE":
            start, end = entity.dxf.start, entity.dxf.end
            line = LineString([(float(start.x), float(start.y)), (float(end.x), float(end.y))])
            return [line.buffer(wall_half_width, cap_style="flat", join_style="mitre")]
        if kind in {"LWPOLYLINE", "POLYLINE"}:
            points = _polyline_points(entity)
            if len(points) < 2:
                return []
            closed = bool(entity.closed) if kind == "LWPOLYLINE" else bool(entity.is_closed)
            if closed and len(points) >= 3:
                polygon = Polygon(points)
                if not polygon.is_valid:
                    polygon = polygon.buffer(0)
                return list(_polygon_parts(polygon))
            return [LineString(points).buffer(wall_half_width, cap_style="flat", join_style="mitre")]
        if kind == "CIRCLE":
            center = entity.dxf.center
            return [Point(float(center.x), float(center.y)).buffer(float(entity.dxf.radius), resolution=16)]
        if kind == "HATCH":
            result: list[Any] = []
            for path in from_hatch(entity):
                points = [(float(vertex.x), float(vertex.y)) for vertex in path.flattening(max(wall_half_width * 0.25, 1e-6))]
                if len(points) >= 3:
                    polygon = Polygon(points)
                    if not polygon.is_valid:
                        polygon = polygon.buffer(0)
                    result.extend(_polygon_parts(polygon))
            return result
        if kind == "INSERT":
            bounds = ezbbox.extents([entity], fast=True)
            if bounds.has_data:
                return [box(float(bounds.extmin.x), float(bounds.extmin.y), float(bounds.extmax.x), float(bounds.extmax.y))]
    except Exception:
        return []
    return []


def _door_opening_masks(doc: ezdxf.document.Drawing, sheets: list[Sheet], component_audit: dict[str, Any] | None = None) -> tuple[dict[str, list[Any]], list[dict[str, Any]]]:
    """Only Rule-2-confirmed ARC/INSERT objects receive green door-review boxes."""
    if component_audit is None:
        from .fire_route_core import stage_05_obstacles
        api = stage_05_obstacles._s05_obstacles
        _, component_audit = api.review_layer_components(doc, [], {}, api, api.ObstacleConfig())
    masks: dict[str, list[Any]] = defaultdict(list)
    rows: list[dict[str, Any]] = []
    for component in component_audit['confirmed_doors']:
        bounds = component.get('bbox')
        if not bounds:
            continue
        layer = component['layer']
        block_name = component['block_name']
        center = ((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)
        sheet = _find_sheet(sheets, *center) if center else None
        if not sheet:
            continue
        diagonal = math.hypot(sheet.max_x - sheet.min_x, sheet.max_y - sheet.min_y)
        padding = max(diagonal * 0.001, 1e-6)
        try:
            mask = box(*bounds).buffer(padding, cap_style="square", join_style="mitre")
            mask = mask.intersection(box(sheet.min_x, sheet.min_y, sheet.max_x, sheet.max_y))
        except Exception:
            continue
        if mask.is_empty:
            continue
        masks[sheet.sheet_id].append(mask)
        rows.append({
            "sheet_id": sheet.sheet_id, "floor": sheet.floor,
            "handle": component['handle'], "layer": layer,
            "block_name": block_name, "entity_type": component['entity_type'],
            "evidence": "门规则2确认：门/DOOR图层、ARC或含ARC块、85~95度、600~1150mm；复核框不裁剪墙体",
            "door_arc_evidence": component['door_arc_evidence'],
            "bbox": [float(value) for value in mask.bounds],
        })
    return masks, rows


def _non_passable_opening_masks(
    doc: ezdxf.document.Drawing,
    sheets: list[Sheet],
    confirmed_door_handles: set[str] | None = None,
) -> tuple[dict[str, list[Any]], list[dict[str, Any]]]:
    masks: dict[str, list[Any]] = defaultdict(list)
    rows: list[dict[str, Any]] = []
    for entity in doc.modelspace():
        if str(entity.dxf.get('handle', '')) in (confirmed_door_handles or set()):
            continue
        if entity.dxftype() not in {"LINE", "ARC", "LWPOLYLINE", "POLYLINE", "CIRCLE", "INSERT"}:
            continue
        layer = str(entity.dxf.get("layer", ""))
        block_name = str(entity.dxf.get("name", "")) if entity.dxftype() == "INSERT" else ""
        semantic = f"{layer}|{block_name}"
        if not NON_PASSABLE_OPENING_RE.search(semantic):
            continue
        center = _entity_center(entity)
        sheet = _find_sheet(sheets, *center) if center else None
        if not sheet:
            continue
        diagonal = math.hypot(sheet.max_x - sheet.min_x, sheet.max_y - sheet.min_y)
        padding = max(diagonal * 0.0008, 1e-6)
        try:
            bounds = ezbbox.extents([entity], fast=True)
            if not bounds.has_data:
                continue
            geometry = box(
                float(bounds.extmin.x), float(bounds.extmin.y),
                float(bounds.extmax.x), float(bounds.extmax.y),
            ).buffer(padding, cap_style="square", join_style="mitre")
            geometry = geometry.intersection(box(sheet.min_x, sheet.min_y, sheet.max_x, sheet.max_y))
        except Exception:
            continue
        if geometry.is_empty:
            continue
        masks[sheet.floor].append(geometry)
        rows.append({
            "sheet_id": sheet.sheet_id, "floor": sheet.floor,
            "handle": str(entity.dxf.get("handle", "")), "layer": layer,
            "block_name": block_name, "entity_type": entity.dxftype(),
            "evidence": "项目规则：洞口/门洞/OPENING统一不可通行",
            "bbox": [float(value) for value in geometry.bounds],
        })
    return masks, rows


def _load_local_obstacle_modules() -> tuple[Any, Any, Any, Any]:
    """Load the self-contained obstacle algorithms shipped with this pipeline."""
    from .fire_route_core import (
        stage_02_cad_inventory,
        stage_03_floor_preprocess,
        stage_05_obstacles,
        stage_05A_obstacles,
    )

    return (
        stage_02_cad_inventory,
        stage_03_floor_preprocess,
        stage_05_obstacles,
        stage_05A_obstacles,
    )


def _run_conditional_multi_building_review(
    stage05a: Any,
    sheets_path: Path,
    obstacle_geojsons: list[Path],
    run_dir: Path,
) -> tuple[Path, list[Path], dict[str, Any]]:
    """Mirror the original Stage 05A gate without silently calling an API."""
    multi_floor_ids = stage05a.find_multi_building_floor_ids(sheets_path)
    summary: dict[str, Any] = {
        "module": "duotuzhi_local_fire_route_core.stage_05A_obstacles",
        "multi_building_floor_ids": multi_floor_ids,
        "external_vision_opt_in_env": "DUOTUZHI_ENABLE_BUILDING_VISION",
    }
    if not multi_floor_ids:
        summary["status"] = "skipped_no_multi_building_floor"
        return sheets_path.resolve(), [], summary
    try:
        render = stage05a.run_stage(
            sheets_path,
            obstacle_geojsons,
            run_dir,
            floor_ids=multi_floor_ids,
        )
    except Exception as exc:
        summary.update({
            "status": "stage_05A_render_error_floor_level_fallback",
            "error_type": type(exc).__name__,
            "error": str(exc),
        })
        return sheets_path.resolve(), [], summary
    summary.update({
        "render_manifest": str(render.manifest_json),
        "rendered_image_count": int(render.image_count),
    })
    opted_in = os.getenv("DUOTUZHI_ENABLE_BUILDING_VISION", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if not opted_in:
        summary["status"] = "rendered_review_external_vision_not_opted_in"
        return sheets_path.resolve(), [], summary
    api_key_env = "ARK_API_KEY"
    if not os.getenv(api_key_env, "").strip():
        summary.update({
            "status": "rendered_review_missing_ark_api_key_floor_level_fallback",
            "api_key_env": api_key_env,
        })
        return sheets_path.resolve(), [], summary
    try:
        vision = stage05a.run_building_vision(
            render.manifest_json,
            sheets_path,
            run_dir,
            config=stage05a.ArkVisionConfig(api_key_env=api_key_env),
            validation_config=stage05a.BuildingVectorValidationConfig(enabled=True),
            obstacle_geojsons=obstacle_geojsons,
            floor_ids=multi_floor_ids,
        )
    except Exception as exc:
        summary.update({
            "status": "stage_05A_vision_error_floor_level_fallback",
            "error_type": type(exc).__name__,
            "error": str(exc),
        })
        return sheets_path.resolve(), [], summary
    effective_sheets = vision.sheets_with_buildings_json.resolve()
    envelope = stage05a.write_upper_floor_building_envelope_obstacles(
        effective_sheets,
        run_dir,
    )
    envelope_paths = []
    value = envelope.get("geojson_path")
    if value and Path(value).is_file() and int(envelope.get("obstacle_count") or 0) > 0:
        envelope_paths.append(Path(value).resolve())
    summary.update({
        "status": "completed_vector_validated_building_vision",
        "planning_sheets_json": str(effective_sheets),
        "building_region_count": int(vision.building_region_count),
        "needs_review_count": int(vision.needs_review_count),
        "building_envelope_completion": {
            "obstacle_count": int(envelope.get("obstacle_count") or 0),
            "geojson_path": str(value or ""),
            "audit_path": str(envelope.get("audit_path") or ""),
        },
    })
    return effective_sheets, envelope_paths, summary


# Cross-run shared cache for obstacle layer-LLM decisions.  Entries are keyed
# by exact layer name and only accepted when the prompt version matches, so a
# second run over the same (or similarly layered) drawing skips the network
# round-trips entirely.  Stored under duotuzhi/cache, next to the converted
# DWG cache, never inside a per-run output directory.
SHARED_LAYER_LLM_CACHE_DIR = Path(__file__).resolve().parents[2] / "cache" / "obstacle_layer_llm"


def _shared_layer_cache_path(api: Any) -> Path:
    return SHARED_LAYER_LLM_CACHE_DIR / api.LAYER_LLM_FILE


def _read_shared_layer_payload(api: Any) -> dict[str, Any]:
    """Load the cross-run shared layer cache when it matches the live prompt."""
    expected_prompt = str(getattr(api, "LAYER_LLM_PROMPT_VERSION", "") or "")
    if not expected_prompt:
        # Test doubles and partial APIs cannot version-stamp decisions; the
        # shared cache stays unused so behaviour matches the pre-cache code.
        return {}
    path = _shared_layer_cache_path(api)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if str(payload.get("prompt_version") or "") != expected_prompt:
        return {}
    if not isinstance(payload.get("decisions"), dict):
        return {}
    return payload


def _seed_run_cache_from_shared(cache_path: Path, shared_payload: dict[str, Any]) -> None:
    """Merge shared decisions into the run-local cache so the classifier skips them.

    Run-local entries always win.  Seeding is skipped when the run-local cache
    already carries a different model, so decisions from different models are
    never mixed under a single model label.
    """
    shared_decisions = shared_payload.get("decisions") or {}
    if not shared_decisions:
        return
    try:
        existing = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        if not isinstance(existing, dict):
            existing = {}
    except (OSError, ValueError, TypeError):
        existing = {}
    existing_model = str(existing.get("model") or "")
    shared_model = str(shared_payload.get("model") or "")
    if existing_model and shared_model and existing_model != shared_model:
        return
    existing_decisions = existing.get("decisions")
    if not isinstance(existing_decisions, dict):
        existing_decisions = {}
    merged = dict(shared_decisions)
    merged.update(existing_decisions)
    seeded = dict(shared_payload)
    seeded.update({
        "model": existing_model or shared_model,
        "prompt_version": str(shared_payload.get("prompt_version") or ""),
        "complete": False,
        "decisions": merged,
        "seeded_from_shared_cache": True,
    })
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = cache_path.with_name(cache_path.name + ".tmp")
        temp_path.write_text(json.dumps(seeded, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, cache_path)
    except OSError:
        pass


def _publish_shared_layer_cache(api: Any, cache_path: Path) -> None:
    """Promote freshly decided layers into the cross-run shared cache.

    Only entries the LLM actually returned are published; deterministic
    overrides and fallbacks are recomputed locally on every run.  Failures
    here must never break the pipeline.
    """
    if not str(getattr(api, "LAYER_LLM_PROMPT_VERSION", "") or ""):
        # Without a prompt version the api cannot version-stamp decisions
        # (e.g. test doubles); never let such callers touch the shared cache.
        return
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    if not isinstance(payload, dict):
        return
    decisions = payload.get("decisions")
    if not isinstance(decisions, dict):
        return
    fresh = {
        str(layer): value
        for layer, value in decisions.items()
        if isinstance(value, dict) and value.get("llm_returned") is True
    }
    if not fresh:
        return
    shared_path = _shared_layer_cache_path(api)
    existing = _read_shared_layer_payload(api)
    if existing and str(existing.get("model") or "") == str(payload.get("model") or ""):
        merged = dict(existing.get("decisions") or {})
    else:
        merged = {}
    merged.update(fresh)
    output = {
        "model": str(payload.get("model") or ""),
        "base_url": str(payload.get("base_url") or ""),
        "prompt_version": str(payload.get("prompt_version") or ""),
        "strategy": "shared_cross_run_obstacle_layer_llm_cache",
        "complete": True,
        "layer_count": len(merged),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "decisions": merged,
    }
    try:
        shared_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = shared_path.with_name(shared_path.name + ".tmp")
        temp_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, shared_path)
    except OSError:
        pass


def _full_layer_decisions(
    api: Any,
    rows: list[dict[str, str]],
    output_dir: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Run the migrated full layer-semantic classifier used by the fire pipeline.

    The LLM classifier remains inside ``duotuzhi`` and reads the copied local
    configuration.  Explicit wall/column layer names are then promoted as a
    deterministic safety net, so an otherwise valid response cannot demote a
    layer such as ``_A04-墙`` or ``COLUMN``.
    """
    summaries = api.layer_summary(rows)
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = output_dir / api.LAYER_LLM_FILE
    reusable_cache: dict[str, dict[str, Any]] = {}
    cache_prompt_version = ""
    if cache_path.exists():
        try:
            cached_payload = json.loads(cache_path.read_text(encoding="utf-8"))
            cache_prompt_version = str(cached_payload.get("prompt_version") or "")
            current_layers = {str(item.get("layer") or "") for item in summaries}
            for layer, item in (cached_payload.get("decisions") or {}).items():
                layer = str(layer)
                if layer not in current_layers or not isinstance(item, dict):
                    continue
                if item.get("llm_returned") is not True:
                    continue
                role = str(item.get("role") or "unknown")
                if role not in {"obstacle_candidate", "not_obstacle", "unknown"}:
                    continue
                reusable_cache[layer] = dict(item)
        except (OSError, ValueError, TypeError):
            reusable_cache = {}

    # Cross-run shared cache fills whatever the run-local cache does not cover
    # (run-local entries keep precedence).
    shared_payload = _read_shared_layer_payload(api)
    shared_cache_layer_count = 0
    if shared_payload:
        layers_now = {str(item.get("layer") or "") for item in summaries}
        for layer, item in (shared_payload.get("decisions") or {}).items():
            layer = str(layer)
            if layer in reusable_cache or layer not in layers_now:
                continue
            if not isinstance(item, dict) or item.get("llm_returned") is not True:
                continue
            role = str(item.get("role") or "unknown")
            if role not in {"obstacle_candidate", "not_obstacle", "unknown"}:
                continue
            reusable_cache[layer] = dict(item)
            shared_cache_layer_count += 1

    current_layers = {str(item.get("layer") or "") for item in summaries}
    cache_covers_current_layers = bool(current_layers) and current_layers.issubset(reusable_cache)
    semantic_runtime = "llm_completed"
    semantic_error = ""
    if cache_covers_current_layers:
        decisions = dict(reusable_cache)
        semantic_runtime = "compatible_complete_layer_cache"
    else:
        # Let the classifier reuse shared decisions via its own on-disk cache,
        # so only layers nobody has ever decided hit the network.
        if shared_payload:
            _seed_run_cache_from_shared(cache_path, shared_payload)
        try:
            decisions = api.classify_layers_by_llm(rows, output_dir)
        except RuntimeError as exc:
            # A stale/invalid credential or temporarily unavailable endpoint must
            # not make geometry processing impossible.  A cache is safe to reuse
            # only by exact layer name; remaining layers stay unknown and can only
            # be promoted by the explicit structural rules below.
            semantic_error = str(exc)
            if reusable_cache:
                decisions = dict(reusable_cache)
                semantic_runtime = "compatible_layer_cache_after_llm_failure"
            else:
                decisions = {}
                semantic_runtime = "deterministic_structural_fallback_after_llm_failure"
            for summary in summaries:
                layer = str(summary.get("layer") or "")
                decisions.setdefault(layer, {
                    "layer": layer,
                    "role": "unknown",
                    "candidate_types": [],
                    "confidence": 0.0,
                    "reason": "LLM unavailable; retained as unknown before structural safety rules",
                    "source": "llm_unavailable_fallback",
                    "model": "",
                    "llm_returned": False,
                })
    # Whatever path produced the decisions, share the LLM-returned entries so
    # later runs (any output directory) can reuse them without the network.
    _publish_shared_layer_cache(api, cache_path)
    explicit_override_count = 0
    for summary in summaries:
        layer = str(summary.get("layer", ""))
        classified = classify_obstacle_layer(layer)
        if not classified:
            continue
        obstacle_type, confidence, evidence = classified
        mapped = (
            "column" if obstacle_type == "column"
            else "wall" if obstacle_type == "wall"
            else "filled_obstacle"
        )
        current = decisions.get(layer) or {}
        current_types = {
            str(value).strip().lower()
            for value in current.get("candidate_types", []) or []
        }
        if current.get("role") == "obstacle_candidate" and mapped in current_types:
            continue
        decisions[layer] = {
            "layer": layer,
            "role": "obstacle_candidate",
            "candidate_types": [mapped],
            "confidence": max(float(current.get("confidence") or 0.0), confidence),
            "reason": f"{evidence}；明确结构图层安全兜底",
            "source": "deterministic_explicit_structural_layer_override_after_llm",
            "model": str(current.get("model") or ""),
            "llm_returned": bool(current.get("llm_returned")),
            "overrode_role": str(current.get("role") or ""),
            "overrode_candidate_types": list(current.get("candidate_types") or []),
            "overrode_reason": str(current.get("reason") or ""),
        }
        explicit_override_count += 1
    decisions = api.apply_deterministic_layer_overrides(decisions)
    effective_path = output_dir / "obstacle_layer_decisions_effective.json"
    audit = {
        "strategy": "full_migrated_llm_layer_semantics_plus_explicit_structural_safety_overrides",
        "semantic_classifier": "duotuzhi_local_fire_route_core.stage_05_obstacles.classify_layers_by_llm",
        "runtime_imports_fire_inspection_system": False,
        "semantic_runtime": semantic_runtime,
        "semantic_error": semantic_error,
        "reused_cached_llm_decision_count": len(reusable_cache) if semantic_runtime.startswith("compatible_") else 0,
        "reused_cache_prompt_version": cache_prompt_version if semantic_runtime.startswith("compatible_") else "",
        "shared_layer_cache": str(_shared_layer_cache_path(api).resolve()),
        "reused_shared_cache_layer_count": shared_cache_layer_count,
        "explicit_structural_override_count": explicit_override_count,
        "decision_count": len(decisions),
        "source_llm_decisions": str((output_dir / api.LAYER_LLM_FILE).resolve()),
        "effective_decisions": str(effective_path.resolve()),
        "decisions": decisions,
        "layer_summaries": summaries,
    }
    write_json(effective_path, audit)
    return decisions, audit


def _plausible_column_face(face: Polygon, region_polygon: Polygon) -> tuple[bool, str]:
    """Reject floor outlines and annotation boxes while retaining column faces."""
    if face.is_empty or face.area <= 0:
        return False, "empty_or_zero_area"
    min_x, min_y, max_x, max_y = face.bounds
    width, height = max_x - min_x, max_y - min_y
    if width <= 0 or height <= 0:
        return False, "degenerate_bbox"
    region_min_x, region_min_y, region_max_x, region_max_y = region_polygon.bounds
    diagonal = math.hypot(region_max_x - region_min_x, region_max_y - region_min_y)
    minimum_dimension = max(diagonal * 0.0001, 1e-6)
    maximum_dimension = max(diagonal * 0.05, minimum_dimension)
    if min(width, height) < minimum_dimension:
        return False, "too_thin_for_column"
    if max(width, height) > maximum_dimension:
        return False, "too_large_for_column"
    if max(width, height) / min(width, height) > 8.0:
        return False, "too_elongated_for_column"
    if face.area > region_polygon.area * 0.01:
        return False, "too_large_relative_to_floor"
    rectangular_fill = face.area / (width * height)
    if rectangular_fill < 0.2:
        return False, "insufficient_compactness"
    return True, "accepted_column_sized_closed_face"


def _polygonize_column_segments(
    segments: list[LineString],
    region_polygon: Polygon,
) -> tuple[list[Polygon], Counter[str]]:
    """Close independent column strokes into compact polygonal obstacles."""
    reasons: Counter[str] = Counter()
    if len(segments) < 3:
        reasons["insufficient_segments"] += 1
        return [], reasons
    try:
        merged = unary_union(segments)
        faces = list(polygonize(merged))
    except Exception:
        reasons["polygonize_error"] += 1
        return [], reasons
    accepted: list[Polygon] = []
    for face in faces:
        try:
            clipped = face.intersection(region_polygon)
        except Exception:
            reasons["region_clip_error"] += 1
            continue
        for part in _polygon_parts(clipped):
            valid, reason = _plausible_column_face(part, region_polygon)
            reasons[reason] += 1
            if valid:
                accepted.append(part)
    return accepted, reasons


def _regions_intersecting_segment(
    segment: LineString,
    regions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Assign zero-width/zero-height LINE entities by real geometry."""
    segment_bounds = segment.bounds
    matched: list[dict[str, Any]] = []
    for region in regions:
        polygon = region.get("polygon")
        if polygon is None or polygon.is_empty:
            continue
        region_bounds = polygon.bounds
        if (
            segment_bounds[2] < region_bounds[0]
            or segment_bounds[0] > region_bounds[2]
            or segment_bounds[3] < region_bounds[1]
            or segment_bounds[1] > region_bounds[3]
        ):
            continue
        try:
            if segment.intersects(polygon):
                matched.append(region)
        except Exception:
            continue
    return matched


def _detect_column_linework_obstacles(
    api: Any,
    rows: list[dict[str, str]],
    regions: list[dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
    config: Any,
    existing_obstacles: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build obstacles for columns drawn only as several independent strokes."""
    grouped: dict[tuple[str, str], list[tuple[LineString, dict[str, str]]]] = defaultdict(list)
    region_by_id = {str(region["full_region_id"]): region for region in regions}
    extracted_segment_count = 0
    for row in rows:
        candidate_types = set(api.row_candidate_types(row, decisions))
        if "column" not in candidate_types:
            continue
        segments = api.open_line_segments_from_row(row, config)
        if not segments:
            continue
        for segment in segments:
            matched_regions = _regions_intersecting_segment(segment, regions)
            for region in matched_regions:
                try:
                    clipped = segment.intersection(region["polygon"])
                except Exception:
                    continue
                line_parts = (
                    [clipped] if isinstance(clipped, LineString)
                    else [part for part in getattr(clipped, "geoms", []) if isinstance(part, LineString)]
                )
                for part in line_parts:
                    if part.is_empty or part.length <= 0:
                        continue
                    key = (str(region["full_region_id"]), str(row.get("layer") or ""))
                    grouped[key].append((part, row))
                    extracted_segment_count += 1

    existing_by_floor: dict[str, list[Any]] = defaultdict(list)
    for obstacle in existing_obstacles:
        floor_id = str(obstacle.get("floor_id") or obstacle.get("source_floor_id") or "")
        geometry = obstacle.get("geometry")
        if floor_id and geometry is not None and not geometry.is_empty:
            existing_by_floor[floor_id].append(geometry)
    existing_union = {
        floor_id: api.valid_polygonal_union(geometries, context=f"column_linework_existing:{floor_id}")
        for floor_id, geometries in existing_by_floor.items()
    }

    output: list[dict[str, Any]] = []
    rejection_reasons: Counter[str] = Counter()
    polygonized_face_count = 0
    duplicate_face_count = 0
    for (region_id, layer), values in sorted(grouped.items()):
        region = region_by_id[region_id]
        faces, reasons = _polygonize_column_segments(
            [segment for segment, _row in values], region["polygon"],
        )
        rejection_reasons.update(reasons)
        polygonized_face_count += sum(reasons.values()) - reasons.get("insufficient_segments", 0)
        representative = values[0][1]
        decision = decisions.get(layer) or {}
        floor_id = str(region.get("floor_id") or region.get("source_floor_id") or "")
        known = existing_union.get(floor_id)
        for face in faces:
            if known is not None and not known.is_empty:
                try:
                    overlap_ratio = face.intersection(known).area / max(face.area, 1e-12)
                except Exception:
                    overlap_ratio = 0.0
                if overlap_ratio >= 0.95:
                    duplicate_face_count += 1
                    continue
            api.add_obstacle(
                output,
                row=representative,
                region=region,
                geom=face,
                obstacle_type="column",
                reason="column_independent_linework_polygonized_closed_face",
                confidence=max(float(decision.get("confidence") or 0.0), 0.96),
                semantic_evidence=api.semantic_evidence_for_row(representative, decisions),
                geometry_evidence="independent_column_strokes_polygonized_to_compact_closed_face",
                topology_evidence=f"region={region_id}; layer={layer}; source_segment_count={len(values)}",
                fusion_decision="accepted_by_column_semantics_and_closed_linework_topology",
            )
    diagnostics = {
        "column_linework_scope_count": len(grouped),
        "extracted_segment_count": extracted_segment_count,
        "polygonized_face_count": polygonized_face_count,
        "accepted_column_face_count": len(output),
        "duplicate_existing_face_count": duplicate_face_count,
        "face_decision_counts": dict(rejection_reasons),
    }
    return output, diagnostics


def _load_recursive_insert_rows(inventory_dir: Path) -> list[dict[str, str]]:
    """Load INSERT containers omitted from the geometry-only Stage 05 input.

    The recursive inventory deliberately separates block containers from their
    exploded child geometry.  That is useful for semantic inspection-object
    recognition, but a structural column drawn as an INSERT can otherwise lose
    the decisive outer-layer semantics (for example ``_A03-结构柱``).  Keep the
    container row so its real transformed bbox can become the column footprint.
    """
    path = inventory_dir / "cad_object_inventory.csv"
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            dict(row)
            for row in csv.DictReader(handle)
            if str(row.get("entity_type") or "").upper() == "INSERT"
        ]


def _detect_structural_insert_obstacles(
    api: Any,
    insert_rows: list[dict[str, str]],
    regions: list[dict[str, Any]],
    existing_obstacles: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Materialize recursively inventoried structural INSERTs as obstacles.

    This is generic: acceptance requires explicit column semantics on the
    insertion layer/block path *and* a compact, column-sized transformed bbox.
    A block name, handle, coordinate or drawing-specific exception is never
    hard-coded.
    """
    existing_by_floor: dict[str, list[Any]] = defaultdict(list)
    for obstacle in existing_obstacles:
        floor_id = str(obstacle.get("floor_id") or obstacle.get("source_floor_id") or "")
        geometry = obstacle.get("geometry")
        if floor_id and geometry is not None and not geometry.is_empty:
            existing_by_floor[floor_id].append(geometry)
    existing_union = {
        floor_id: api.valid_polygonal_union(geometries, context=f"structural_insert_existing:{floor_id}")
        for floor_id, geometries in existing_by_floor.items()
    }

    output: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    candidate_count = 0
    for row in insert_rows:
        layer = str(row.get("layer") or "")
        block_name = str(row.get("parent_block_name") or "")
        block_path = str(row.get("block_path") or "")
        classification = classify_obstacle_layer(layer, f"{block_name}|{block_path}")
        if not classification or classification[0] != "column":
            continue
        candidate_count += 1
        try:
            footprint = box(
                float(row["bbox_minx"]), float(row["bbox_miny"]),
                float(row["bbox_maxx"]), float(row["bbox_maxy"]),
            )
        except (KeyError, TypeError, ValueError):
            reasons["invalid_insert_bbox"] += 1
            continue
        if footprint.is_empty or footprint.area <= 0:
            reasons["empty_insert_bbox"] += 1
            continue
        matched = [
            region for region in regions
            if footprint.intersects(region["polygon"])
        ]
        if not matched:
            reasons["outside_floor_regions"] += 1
            continue
        for region in matched:
            clipped = footprint.intersection(region["polygon"])
            for part in _polygon_parts(clipped):
                accepted, reason = _plausible_column_face(part, region["polygon"])
                reasons[reason] += 1
                if not accepted:
                    continue
                floor_id = str(region.get("floor_id") or region.get("source_floor_id") or "")
                known = existing_union.get(floor_id)
                if known is not None and not known.is_empty:
                    overlap = part.intersection(known).area / max(part.area, 1e-12)
                    if overlap >= 0.95:
                        reasons["duplicate_existing_obstacle"] += 1
                        continue
                api.add_obstacle(
                    output,
                    row=row,
                    region=region,
                    geom=part,
                    obstacle_type="column",
                    reason="recursive_structural_insert_bbox_materialized",
                    confidence=max(float(classification[1]), 0.98),
                    semantic_evidence=(
                        f"layer={layer}; parent_block={block_name}; block_path={block_path}; "
                        "explicit structural-column semantics"
                    ),
                    geometry_evidence="recursive INSERT transformed bbox is compact column footprint",
                    topology_evidence=f"region={region['full_region_id']}; insert_depth={row.get('insert_depth', '')}",
                    fusion_decision="accepted_by_structural_insert_semantics_and_compact_bbox",
                )
    return output, {
        "recursive_insert_count": len(insert_rows),
        "structural_column_insert_candidate_count": candidate_count,
        "accepted_structural_insert_count": len(output),
        "decision_counts": dict(reasons),
    }


def recognize_building_obstacles(
    doc: ezdxf.document.Drawing,
    sheets: list[Sheet],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    geometries_by_sheet: dict[str, list[tuple[Any, str]]] = defaultdict(list)
    from .fire_route_core import stage_05_obstacles
    api = stage_05_obstacles._s05_obstacles
    decisions = {str(layer.dxf.name): {'role': 'not_obstacle', 'candidate_types': []}
                 for layer in doc.layers if api.layer_has_door_attribute(str(layer.dxf.name))}
    regions = [{'sheet_id': sheet.sheet_id, 'floor_id': sheet.floor, 'floor_name': sheet.floor,
                'region_id': 'R01', 'full_region_id': sheet.sheet_id + ':R01',
                'polygon': box(sheet.min_x, sheet.min_y, sheet.max_x, sheet.max_y)} for sheet in sheets]
    component_obstacles, component_audit = api.review_layer_components(doc, regions, decisions, api, api.ObstacleConfig())
    door_masks, door_rows = _door_opening_masks(doc, sheets, component_audit)
    excluded_handles = {r['handle'] for r in component_audit['owned_components'] if r['handle']}
    for item in component_obstacles:
        geometries_by_sheet[item['sheet_id']].append((item['geometry'], item['obstacle_type']))
        candidates.append({k: item.get(k) for k in ('sheet_id', 'layer', 'handle', 'entity_type', 'obstacle_type')})
    for entity in doc.modelspace():
        if str(entity.dxf.get('handle', '')) in excluded_handles or str(entity.dxf.layer) in component_audit['review_layers']:
            continue
        if entity.dxftype() not in {"LINE", "LWPOLYLINE", "POLYLINE", "CIRCLE", "HATCH", "INSERT"}:
            continue
        layer = str(entity.dxf.get("layer", ""))
        block_name = str(entity.dxf.get("name", "")) if entity.dxftype() == "INSERT" else ""
        classification = classify_obstacle_layer(layer, block_name)
        if not classification:
            continue
        obstacle_type, confidence, evidence = classification
        point = _entity_center(entity)
        if not point:
            continue
        sheet = _find_sheet(sheets, point[0], point[1])
        if not sheet:
            continue
        diagonal = math.hypot(sheet.max_x - sheet.min_x, sheet.max_y - sheet.min_y)
        wall_half_width = max(diagonal * 0.0008, 1e-6)
        sheet_polygon = box(sheet.min_x, sheet.min_y, sheet.max_x, sheet.max_y)
        min_area = max(sheet_polygon.area * 1e-10, 1e-12)
        accepted_geometries = []
        for geometry in _entity_geometries(entity, obstacle_type, wall_half_width):
            clipped = geometry.intersection(sheet_polygon)
            accepted_geometries.extend(part for part in _polygon_parts(clipped) if part.area >= min_area)
        if not accepted_geometries:
            continue
        source_row = {
            "sheet_id": sheet.sheet_id, "floor": sheet.floor,
            "handle": str(entity.dxf.get("handle", "")), "layer": layer,
            "block_name": block_name, "entity_type": entity.dxftype(),
            "obstacle_type": obstacle_type, "confidence": confidence,
            "evidence": evidence, "geometry_count": len(accepted_geometries),
        }
        candidates.append(source_row)
        geometries_by_sheet[sheet.sheet_id].extend((geometry, obstacle_type) for geometry in accepted_geometries)

    obstacles: list[dict[str, Any]] = []
    sheet_map = {sheet.sheet_id: sheet for sheet in sheets}
    for sheet_id, values in geometries_by_sheet.items():
        sheet = sheet_map[sheet_id]
        # Dissolve overlaps per type.  This produces route-ready polygonal
        # barriers instead of thousands of duplicate wall strokes.
        for obstacle_type in sorted({kind for _geometry, kind in values}):
            merged = unary_union([geometry for geometry, kind in values if kind == obstacle_type])
            # A review bbox is not an opening polygon: never carve walls with it.
            for polygon in _polygon_parts(merged):
                coordinates = [[float(x), float(y)] for x, y in polygon.exterior.coords]
                obstacles.append({
                    "obstacle_id": f"OBS-{len(obstacles) + 1:06d}",
                    "sheet_id": sheet_id, "floor": sheet.floor,
                    "building_ids": list(sheet.building_ids), "obstacle_type": obstacle_type,
                    "area": float(polygon.area), "bbox": [float(value) for value in polygon.bounds],
                    "coordinates": coordinates,
                })
    return obstacles, candidates, door_rows


def _annotate(
    doc: ezdxf.document.Drawing,
    obstacles: list[dict[str, Any]],
    reference_obstacles: list[dict[str, Any]],
    column_linework_obstacles: list[dict[str, Any]],
    structural_insert_obstacles: list[dict[str, Any]],
    door_masks: list[dict[str, Any]],
    non_passable_openings: list[dict[str, Any]],
    output: Path,
) -> Path:
    layers = {"wall": 1, "column": 6, "structural_fill": 30, "building_obstacle_union": 1}
    for kind, color in layers.items():
        name = f"通用融合_建筑障碍物_{kind}"
        if name not in doc.layers:
            doc.layers.add(name, color=color, lineweight=35)
    column_review_layer = "通用融合_柱障碍_散线闭合"
    if column_review_layer not in doc.layers:
        doc.layers.add(column_review_layer, color=6, lineweight=70)
    raw_review_layers = {
        "wall": ("通用融合_障碍物逐对象_墙体", 1),
        "column": ("通用融合_障碍物逐对象_柱体", 2),
        "filled_obstacle": ("通用融合_障碍物逐对象_填充实体", 30),
    }
    for layer_name, color in raw_review_layers.values():
        if layer_name not in doc.layers:
            doc.layers.add(layer_name, color=color, lineweight=50)
    insert_review_layer = "通用融合_柱障碍_结构图块"
    if insert_review_layer not in doc.layers:
        doc.layers.add(insert_review_layer, color=2, lineweight=70)
    if "通用融合_明确门图元_仅复核" not in doc.layers:
        doc.layers.add("通用融合_明确门图元_仅复核", color=3, lineweight=25)
    if "通用融合_不可通行洞口" not in doc.layers:
        doc.layers.add("通用融合_不可通行洞口", color=30, lineweight=35)
    msp = doc.modelspace()
    for item in obstacles:
        msp.add_lwpolyline(
            item["coordinates"], close=True,
            dxfattribs={"layer": f"通用融合_建筑障碍物_{item['obstacle_type']}"},
        )
    # Preserve the raw Stage 05 provenance for review.  Navigation consumes the
    # dissolved floor union above, while reviewers need to see each actual wall,
    # column or filled entity instead of one anonymous merged outline.
    for item in reference_obstacles:
        geometry = item.get("geometry")
        obstacle_type = str(item.get("obstacle_type") or "")
        layer_name, color = raw_review_layers.get(
            obstacle_type, ("通用融合_障碍物逐对象_填充实体", 30)
        )
        for polygon in _polygon_parts(geometry):
            msp.add_lwpolyline(
                [(float(x), float(y)) for x, y in polygon.exterior.coords],
                close=True,
                dxfattribs={"layer": layer_name, "color": color},
            )

    for item in [*column_linework_obstacles, *structural_insert_obstacles]:
        geometry = item.get("geometry")
        if geometry is None:
            continue
        review_layer = (
            insert_review_layer
            if str(item.get("reason") or "") == "recursive_structural_insert_bbox_materialized"
            else column_review_layer
        )
        review_color = 2 if review_layer == insert_review_layer else 6
        for polygon in _polygon_parts(geometry):
            min_x, min_y, max_x, max_y = polygon.bounds
            marker_offset = max(min(max_x - min_x, max_y - min_y) * 0.08, 1e-6)
            marker = polygon.buffer(marker_offset, join_style=2)
            msp.add_lwpolyline(
                [(float(x), float(y)) for x, y in marker.exterior.coords],
                close=True,
                dxfattribs={"layer": review_layer, "color": review_color},
            )
            msp.add_line(
                (min_x - marker_offset, min_y - marker_offset),
                (max_x + marker_offset, max_y + marker_offset),
                dxfattribs={"layer": review_layer, "color": review_color},
            )
            msp.add_line(
                (min_x - marker_offset, max_y + marker_offset),
                (max_x + marker_offset, min_y - marker_offset),
                dxfattribs={"layer": review_layer, "color": review_color},
            )
    for item in door_masks:
        min_x, min_y, max_x, max_y = item["bbox"]
        msp.add_lwpolyline(
            [(min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y)], close=True,
            dxfattribs={"layer": "通用融合_明确门图元_仅复核"},
        )
    for item in non_passable_openings:
        min_x, min_y, max_x, max_y = item["bbox"]
        msp.add_lwpolyline(
            [(min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y)], close=True,
            dxfattribs={"layer": "通用融合_不可通行洞口"},
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(output)
    return output


def run_stage(
    prepared_payload: dict[str, object],
    sheets_path: Path,
    stage_dir: Path,
    *,
    write_annotated_dxf: bool = True,
) -> dict[str, object]:
    stage_dir.mkdir(parents=True, exist_ok=True)
    building = next(item for item in prepared_payload["drawings"] if item["discipline"] == "building")
    source = Path(str(building["dxf"])).resolve()
    sheets = [
        Sheet(**item) for item in read_json(sheets_path)
        if item["discipline"] == "building" and Path(str(item["drawing"])).resolve() == source
    ]
    doc = ezdxf.readfile(source)
    stage02, stage03, stage05, stage05a = _load_local_obstacle_modules()
    reference_root = stage_dir / "local_route_core"
    inventory_dir = reference_root / "inventory"
    required_inventory = (
        "inventory_manifest.json", "cad_object_inventory.csv", "cad_semantic_inventory.csv",
        "cad_geometry_inventory.csv", "cad_block_signatures.json", "cad_object_catalog.csv",
    )
    cached_manifest = (
        read_json(inventory_dir / "inventory_manifest.json")
        if all((inventory_dir / name).is_file() for name in required_inventory)
        else {}
    )
    cached_sha = str((cached_manifest.get("unique_input_source") or {}).get("sha256", ""))
    source_sha = stage02._s02_inventory.file_sha256(source)
    if cached_manifest and cached_sha == source_sha:
        inventory_manifest = cached_manifest
    else:
        inventory_manifest = stage02._s02_inventory.run_inventory(
            source, inventory_dir, True, 10, 350, False,
        )
    preprocess_dir = reference_root / "preprocess"
    preprocess_sheets_path = preprocess_dir / "drawing_sheets_floors.json"
    if not preprocess_sheets_path.is_file():
        preprocess = stage03._s03_api.preprocess_cad_drawing(
            source, inventory_dir, preprocess_dir,
        )
        preprocess_sheets_path = preprocess.sheets_json
    api = stage05._s05_obstacles
    geometry_rows = api.load_geometry_rows(inventory_dir)
    regions = api.load_floor_regions(preprocess_sheets_path)
    if not regions:
        raise RuntimeError("本地路线核心的楼层预处理未产生可用于障碍物识别的建筑区域")
    recursive_insert_rows = _load_recursive_insert_rows(inventory_dir)
    decisions, semantic_audit = _full_layer_decisions(
        api, geometry_rows + recursive_insert_rows, reference_root,
    )
    config = api.ObstacleConfig()
    component_obstacles, component_audit = api.review_layer_components(doc, regions, decisions, api, config)
    component_audit_path = reference_root / 'obstacles' / 'mixed_layer_component_audit.json'
    write_json(component_audit_path, component_audit)
    geometry_rows = api.exclude_reviewed_component_rows(geometry_rows, component_audit)
    recursive_insert_rows = api.exclude_reviewed_component_rows(recursive_insert_rows, component_audit)
    print(f"      [构件复核] 待复核图层: {len(component_audit['review_layers'])}, "
          f"规则2确认门: {component_audit['confirmed_door_count']}, "
          f"补充障碍面: {len(component_obstacles)}", flush=True)
    area_obstacles = api.detect_llm_hit_obstacles(geometry_rows, regions, decisions, config)
    wall_stats: dict[str, Any] = {}
    parallel_obstacles = api.detect_parallel_wall_line_obstacles(
        geometry_rows, regions, decisions, config, diagnostics=wall_stats,
    )
    column_linework_obstacles, column_linework_stats = _detect_column_linework_obstacles(
        api,
        geometry_rows,
        regions,
        decisions,
        config,
        area_obstacles + parallel_obstacles,
    )
    structural_insert_obstacles, structural_insert_stats = _detect_structural_insert_obstacles(
        api,
        recursive_insert_rows,
        regions,
        area_obstacles + parallel_obstacles + column_linework_obstacles,
    )
    # Reuse the block rule embedded in this pipeline's existing Stage 05.
    print("      [块障碍] 按候选图层块实例检测跨内部图层平行线组", flush=True)
    block_parallel_obstacles, block_parallel_stats = api.detect_candidate_block_obstacles(
        source, regions, decisions, api, config, doc=doc, component_audit=component_audit,
        existing_obstacles=(area_obstacles + parallel_obstacles + column_linework_obstacles
                            + structural_insert_obstacles),
    )
    block_parallel_audit = reference_root / "obstacles" / "block_parallel_obstacle_audit.json"
    write_json(block_parallel_audit, block_parallel_stats)
    print(f"      [块障碍] 候选块: {block_parallel_stats.get('candidate_instance_count', 0)}, "
          f"多组平行线块: {block_parallel_stats.get('qualified_instance_count', 0)}, "
          f"含弧线排除: {block_parallel_stats.get('arc_rejected_instance_count', 0)}, "
          f"展开不完整: {block_parallel_stats.get('incomplete_rejected_instance_count', 0)}, "
          f"新增障碍面: {len(block_parallel_obstacles)}", flush=True)
    reference_obstacles = (
        area_obstacles
        + parallel_obstacles
        + column_linework_obstacles
        + structural_insert_obstacles
        + block_parallel_obstacles
        + component_obstacles
    )
    for index, obstacle in enumerate(reference_obstacles, start=1):
        obstacle["obstacle_id"] = f"OBS_{index:06d}"
    original_output = reference_root / "obstacles"
    original_output.mkdir(parents=True, exist_ok=True)
    original_csv = original_output / "floor_obstacles.csv"
    api.write_obstacle_csv(original_csv, reference_obstacles)
    region_geojsons, union_geojsons = api.write_geojson_outputs(original_output, reference_obstacles)
    effective_preprocess_sheets, envelope_paths, building_scope_summary = (
        _run_conditional_multi_building_review(
            stage05a,
            preprocess_sheets_path,
            union_geojsons,
            reference_root,
        )
    )
    # Maintain the original single-drawing Stage 05 runtime contract.  The raw
    # CAD collision stage and downstream audit tools consume this manifest.
    reference_result = original_output / "floor_obstacle_recognition_result.json"
    write_json(reference_result, {
        "input_dxf": str(source),
        "inventory_dir": str(inventory_dir.resolve()),
        "sheets_json": str(effective_preprocess_sheets.resolve()),
        "output_dir": str(original_output.resolve()),
        "strategy": "full local Stage 05 obstacle chain plus additive recursive structural INSERT columns",
        "layer_llm_decisions": semantic_audit["source_llm_decisions"],
        "obstacle_csv": str(original_csv.resolve()),
        "obstacle_count": len(reference_obstacles),
        "obstacle_type_count": len({str(item.get('obstacle_type') or '') for item in reference_obstacles}),
        "region_count": len(regions),
        "stage_counts": {
            "area_obstacles": len(area_obstacles),
            "parallel_wall_line_bundle_obstacles": len(parallel_obstacles),
            "column_linework_obstacles": len(column_linework_obstacles),
            "structural_insert_column_obstacles": len(structural_insert_obstacles),
            "candidate_block_parallel_obstacles": len(block_parallel_obstacles),
            "final_obstacles": len(reference_obstacles),
        },
        "wall_processing_stats": wall_stats,
        "column_linework_stats": column_linework_stats,
        "structural_insert_stats": structural_insert_stats,
        "block_parallel_audit": str(block_parallel_audit.resolve()),
        "mixed_layer_component_audit": str(component_audit_path.resolve()),
        "mixed_layer_component_obstacle_count": len(component_obstacles),
        "confirmed_door_count": component_audit['confirmed_door_count'],
        "per_region_geojsons": [str(path.resolve()) for path in region_geojsons],
        "union_geojsons": [str(path.resolve()) for path in union_geojsons],
    })

    door_mask_geometries, door_masks = _door_opening_masks(doc, sheets, component_audit)
    opening_geometries, non_passable_openings = _non_passable_opening_masks(
        doc, sheets, {item['handle'] for item in component_audit['confirmed_doors'] if item['handle']})
    geometries_by_floor: dict[str, list[Any]] = defaultdict(list)
    for item in reference_obstacles:
        floor = str(item.get("floor_id") or item.get("source_floor_id") or item.get("sheet_id") or "UNKNOWN")
        geometries_by_floor[floor].append(item["geometry"])
    for envelope_path in envelope_paths:
        envelope_payload = json.loads(envelope_path.read_text(encoding="utf-8"))
        for feature in envelope_payload.get("features", []):
            properties = feature.get("properties") or {}
            floor = str(
                properties.get("source_floor_id")
                or properties.get("floor_id")
                or "UNKNOWN"
            )
            try:
                geometry = shape(feature["geometry"])
            except Exception:
                continue
            if not geometry.is_empty:
                geometries_by_floor[floor].append(geometry)
    for floor, geometries in opening_geometries.items():
        geometries_by_floor[floor].extend(geometries)

    obstacles: list[dict[str, Any]] = []
    minimum_area_by_floor = {
        sheet.floor: max((sheet.max_x - sheet.min_x) * (sheet.max_y - sheet.min_y) * 1e-10, 1e-9)
        for sheet in sheets
    }
    for floor, geometries in sorted(geometries_by_floor.items()):
        merged = api.valid_polygonal_union(geometries, context=f"duotuzhi_final_union:{floor}")
        for polygon in _polygon_parts(merged):
            if polygon.area < minimum_area_by_floor.get(floor, 1e-9):
                continue
            obstacles.append({
                "obstacle_id": f"OBS-{len(obstacles) + 1:06d}", "sheet_id": "",
                "floor": floor, "building_ids": [], "obstacle_type": "building_obstacle_union",
                "area": float(polygon.area), "bbox": [float(value) for value in polygon.bounds],
                "coordinates": [[float(x), float(y)] for x, y in polygon.exterior.coords],
                "interiors": [
                    [[float(x), float(y)] for x, y in ring.coords]
                    for ring in polygon.interiors
                ],
            })
    write_json(stage_dir / "building_obstacles.json", obstacles)
    write_csv(stage_dir / "door_opening_masks.csv", door_masks)
    write_csv(stage_dir / "non_passable_openings.csv", non_passable_openings)

    features = []
    for item in obstacles:
        polygon = Polygon(item["coordinates"], item.get("interiors") or [])
        features.append({
            "type": "Feature",
            "properties": {
                key: value
                for key, value in item.items()
                if key not in {"coordinates", "interiors"}
            },
            "geometry": mapping(polygon),
        })
    geojson_path = stage_dir / "building_obstacles.geojson"
    geojson_path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # The per-object annotated DXF is a human-review rendering only.  It can
    # cost over a hundred MB on large drawings, so callers may opt out; every
    # machine-consumed obstacle artifact above is produced regardless.
    if write_annotated_dxf:
        annotated = _annotate(
            doc,
            obstacles,
            reference_obstacles,
            column_linework_obstacles,
            structural_insert_obstacles,
            door_masks,
            non_passable_openings,
            stage_dir / "建筑底图_障碍物识别标注_逐对象含柱体高亮.dxf",
        )
        annotated_dxf_value = str(annotated.resolve())
    else:
        annotated_dxf_value = ""
    counts = dict(Counter(item["obstacle_type"] for item in obstacles))
    payload = {
        "stage": "02b_building_obstacles", "source_building": str(source),
        "backend": "duotuzhi_local_fire_route_core",
        "building_sheet_count": len(sheets),
        "source_entity_count": int(inventory_manifest.get("counts", {}).get("geometry_inventory_objects", 0)),
        "door_opening_mask_count": len(door_masks),
        "non_passable_opening_count": len(non_passable_openings),
        "obstacle_polygon_count": len(obstacles), "counts": counts,
        "reference_area_obstacle_count": len(area_obstacles),
        "reference_parallel_wall_obstacle_count": len(parallel_obstacles),
        "reference_column_linework_obstacle_count": len(column_linework_obstacles),
        "reference_structural_insert_obstacle_count": len(structural_insert_obstacles),
        "reference_block_parallel_obstacle_count": len(block_parallel_obstacles),
        "mixed_layer_component_obstacle_count": len(component_obstacles),
        "mixed_layer_component_audit": str(component_audit_path.resolve()),
        "confirmed_door_rule": component_audit['door_rule'],
        "block_parallel_audit": str(block_parallel_audit.resolve()),
        "block_parallel_rule_version": block_parallel_stats["rule_version"],
        "reference_region_count": len(regions),
        "reference_wall_processing_stats": wall_stats,
        "reference_column_linework_stats": column_linework_stats,
        "reference_structural_insert_stats": structural_insert_stats,
        "layer_semantic_strategy": semantic_audit["strategy"],
        "layer_semantic_decisions": semantic_audit["effective_decisions"],
        "layer_llm_decisions": semantic_audit["source_llm_decisions"],
        "runtime_imports_fire_inspection_system": False,
        "shared_block_obstacle_module": stage05.__name__,
        "reference_inventory": str(inventory_dir.resolve()),
        "reference_preprocess_sheets": str(preprocess_sheets_path.resolve()),
        "effective_planning_sheets": str(effective_preprocess_sheets.resolve()),
        "conditional_building_scope_recognition": building_scope_summary,
        "reference_obstacle_csv": str(original_csv.resolve()),
        "reference_obstacle_result": str(reference_result.resolve()),
        "reference_region_geojsons": [str(path.resolve()) for path in region_geojsons],
        "reference_union_geojsons": [str(path.resolve()) for path in union_geojsons],
        "obstacles_json": str((stage_dir / "building_obstacles.json").resolve()),
        "obstacles_geojson": str(geojson_path.resolve()),
        "source_entity_csv": str(original_csv.resolve()),
        "door_opening_mask_csv": str((stage_dir / "door_opening_masks.csv").resolve()),
        "non_passable_opening_csv": str((stage_dir / "non_passable_openings.csv").resolve()),
        "annotated_dxf": annotated_dxf_value,
        "annotated_dxf_skipped": not write_annotated_dxf,
        "scope": "building_drawing_only",
    }
    write_json(stage_dir / "stage_summary.json", payload)
    return payload
