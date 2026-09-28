"""Safety-preserving display-route beautification.

The original Stage 10 forwarding route is an immutable audit artifact.  This
module creates a separate display geometry by joining contiguous traversal
events and greedily replacing small graph-edge chains with line-of-sight
segments.  Every replacement is certified against the same vector effective
free space used by physical-graph routing; an unsafe replacement is never
published.
"""

from __future__ import annotations

import copy
import json
import math
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from shapely.geometry import LineString, shape
from shapely.ops import unary_union
from shapely.prepared import prep


Point = Tuple[float, float]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _read_json(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON root must be an object: {}".format(path))
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path.resolve()


def _points(geometry: Mapping[str, Any]) -> List[Point]:
    if _text(geometry.get("type")) != "LineString":
        return []
    result: List[Point] = []
    for row in geometry.get("coordinates", []) or []:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            result.append((float(row[0]), float(row[1])))
    return result


def _same_point(left: Point, right: Point, tolerance: float) -> bool:
    return math.dist(left, right) <= tolerance


def _deduplicate(points: Sequence[Point], tolerance: float) -> List[Point]:
    result: List[Point] = []
    for point in points:
        if not result or not _same_point(result[-1], point, tolerance):
            result.append(point)
    return result


def _load_free_spaces(path: Path) -> Dict[str, Any]:
    grouped: Dict[str, List[Any]] = defaultdict(list)
    for feature in _read_json(path).get("features", []) or []:
        if not isinstance(feature, dict) or not feature.get("geometry"):
            continue
        floor_id = _text((feature.get("properties") or {}).get("floor_id"))
        if floor_id:
            grouped[floor_id].append(shape(feature["geometry"]))
    floors = {floor_id: unary_union(values) for floor_id, values in grouped.items()}
    if not floors:
        raise ValueError("有效自由空间中没有楼层几何: {}".format(path))
    return floors


def _floor_scale(geometry: Any) -> float:
    minx, miny, maxx, maxy = geometry.bounds
    return max(maxx - minx, maxy - miny, 1.0)


def _line_is_safe(
    prepared_free_space: Any,
    left: Point,
    right: Point,
    clearance: float,
    prepared_route_corridor: Any | None = None,
) -> Tuple[bool, bool]:
    if _same_point(left, right, 1e-9):
        return True, True
    line = LineString([left, right])
    if not prepared_free_space.covers(line):
        return False, False
    if prepared_route_corridor is not None and not prepared_route_corridor.covers(line):
        return False, False
    if clearance <= 0.0:
        return True, True
    corridor = line.buffer(clearance, cap_style=2, join_style=2)
    return True, bool(prepared_free_space.covers(corridor))


def _shortcut(
    points: Sequence[Point],
    prepared_free_space: Any,
    *,
    duplicate_tolerance: float,
    clearance: float,
    prepared_route_corridor: Any,
    max_lookahead: int,
) -> Tuple[List[Point], Dict[str, int]]:
    source = _deduplicate(points, duplicate_tolerance)
    if len(source) <= 2:
        return source, {
            "shortcut_attempt_count": 0,
            "accepted_shortcut_count": 0,
            "clearance_certified_shortcut_count": 0,
        }
    result = [source[0]]
    cursor = 0
    attempts = 0
    accepted = 0
    clearance_accepted = 0
    while cursor < len(source) - 1:
        upper = min(len(source) - 1, cursor + max(2, max_lookahead))
        chosen = cursor + 1
        chosen_clearance = False
        for candidate in range(upper, cursor + 1, -1):
            attempts += 1
            safe, has_clearance = _line_is_safe(
                prepared_free_space,
                source[cursor],
                source[candidate],
                clearance,
                prepared_route_corridor,
            )
            if safe:
                chosen = candidate
                chosen_clearance = has_clearance
                break
        if chosen > cursor + 1:
            accepted += 1
            if chosen_clearance:
                clearance_accepted += 1
        result.append(source[chosen])
        cursor = chosen
    return result, {
        "shortcut_attempt_count": attempts,
        "accepted_shortcut_count": accepted,
        "clearance_certified_shortcut_count": clearance_accepted,
    }


def _group_key(feature: Mapping[str, Any]) -> Tuple[str, int, str, int, str]:
    properties = feature.get("properties") or {}
    return (
        _text(properties.get("floor_id")),
        int(properties.get("leg_index") or 0),
        _text(properties.get("route_segment_id")),
        int(properties.get("pass_index") or 1),
        _text(properties.get("backend")),
    )


def _contiguous_groups(
    features: Sequence[Mapping[str, Any]],
    join_tolerance_by_floor: Mapping[str, float],
) -> Iterable[List[Mapping[str, Any]]]:
    current: List[Mapping[str, Any]] = []
    current_key: Tuple[str, int, str, int, str] | None = None
    current_end: Point | None = None
    for feature in sorted(
        features,
        key=lambda row: (
            _text((row.get("properties") or {}).get("floor_id")),
            int((row.get("properties") or {}).get("sequence_no") or 0),
        ),
    ):
        points = _points(feature.get("geometry") or {})
        if len(points) < 2:
            continue
        key = _group_key(feature)
        tolerance = join_tolerance_by_floor.get(key[0], 1e-6)
        connected = current_end is not None and _same_point(current_end, points[0], tolerance)
        if current and (key != current_key or not connected):
            yield current
            current = []
        current.append(feature)
        current_key = key
        current_end = points[-1]
    if current:
        yield current


def beautify_route_geojson(
    original_route_geojson: Path | str,
    effective_free_areas: Path | str,
    *,
    output_geojson: Path | str | None = None,
    audit_path: Path | str | None = None,
    max_lookahead: int = 256,
) -> Dict[str, Any]:
    """Create a separate, vector-certified display route GeoJSON."""

    started = time.perf_counter()
    original_path = Path(original_route_geojson).resolve()
    free_path = Path(effective_free_areas).resolve()
    if not original_path.is_file():
        raise FileNotFoundError(original_path)
    if not free_path.is_file():
        raise FileNotFoundError(free_path)
    output_path = (
        Path(output_geojson).resolve()
        if output_geojson
        else original_path.with_name("forwarding_route_beautified.geojson")
    )
    review_path = (
        Path(audit_path).resolve()
        if audit_path
        else original_path.with_name("route_beautification_audit.json")
    )

    payload = _read_json(original_path)
    free_spaces = _load_free_spaces(free_path)
    prepared = {floor_id: prep(value) for floor_id, value in free_spaces.items()}
    floor_scales = {floor_id: _floor_scale(value) for floor_id, value in free_spaces.items()}
    join_tolerance = {
        floor_id: max(scale * 1e-8, 1e-6)
        for floor_id, scale in floor_scales.items()
    }
    clearance = {
        floor_id: max(scale * 5e-5, 1e-4)
        for floor_id, scale in floor_scales.items()
    }
    maximum_deviation = {
        # Roughly 0.25--0.60 m for the current millimetre-based CAD files.
        # The original route corridor prevents attractive-but-unrealistic long
        # diagonal shortcuts across halls or rooms.
        floor_id: max(scale * 1.25e-3, clearance[floor_id] * 4.0)
        for floor_id, scale in floor_scales.items()
    }

    route_features = [
        feature
        for feature in payload.get("features", []) or []
        if isinstance(feature, dict)
        and (feature.get("properties") or {}).get("feature_type")
        == "route_edge_traversal"
    ]
    passthrough_features = [
        copy.deepcopy(feature)
        for feature in payload.get("features", []) or []
        if not (
            isinstance(feature, dict)
            and (feature.get("properties") or {}).get("feature_type")
            == "route_edge_traversal"
        )
    ]
    floor_audit: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "source_event_count": 0,
            "display_run_count": 0,
            "source_point_count": 0,
            "display_point_count": 0,
            "source_length": 0.0,
            "display_length": 0.0,
            "shortcut_attempt_count": 0,
            "accepted_shortcut_count": 0,
            "clearance_certified_shortcut_count": 0,
            "invalid_display_segment_count": 0,
            "omitted_zero_length_event_count": 0,
        }
    )
    display_features: List[Dict[str, Any]] = []
    for run_index, group in enumerate(
        _contiguous_groups(route_features, join_tolerance), 1
    ):
        properties = dict(group[0].get("properties") or {})
        floor_id = _text(properties.get("floor_id"))
        if floor_id not in prepared:
            raise KeyError("路线楼层缺少有效自由空间: {}".format(floor_id))
        source_points: List[Point] = []
        for feature in group:
            points = _points(feature.get("geometry") or {})
            if source_points and points and _same_point(
                source_points[-1], points[0], join_tolerance[floor_id]
            ):
                source_points.extend(points[1:])
            else:
                source_points.extend(points)
        source_points = _deduplicate(source_points, join_tolerance[floor_id])
        if len(source_points) < 2:
            row = floor_audit[floor_id]
            row["source_event_count"] += len(group)
            row["source_point_count"] += len(source_points)
            row["omitted_zero_length_event_count"] += len(group)
            continue
        source_line = LineString(source_points)
        route_corridor = prep(
            source_line.buffer(
                maximum_deviation[floor_id],
                cap_style=2,
                join_style=2,
            )
        )
        display_points, shortcut_audit = _shortcut(
            source_points,
            prepared[floor_id],
            duplicate_tolerance=join_tolerance[floor_id],
            clearance=clearance[floor_id],
            prepared_route_corridor=route_corridor,
            max_lookahead=max_lookahead,
        )
        invalid = 0
        for left, right in zip(display_points, display_points[1:]):
            safe, _has_clearance = _line_is_safe(
                prepared[floor_id], left, right, 0.0
            )
            if not safe:
                invalid += 1
        if invalid:
            raise RuntimeError(
                "美化路线未通过有效自由空间复核: floor={}, invalid={}".format(
                    floor_id, invalid
                )
            )
        display_line = LineString(display_points)
        properties.update(
            {
                "display_geometry": True,
                "display_run_id": "DISPLAY_RUN_{:07d}".format(run_index),
                "source_event_count": len(group),
                "source_point_count": len(source_points),
                "display_point_count": len(display_points),
                "safety_validation": "effective_free_area_covers_every_segment",
            }
        )
        display_features.append(
            {
                "type": "Feature",
                "properties": properties,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[x, y] for x, y in display_points],
                },
            }
        )
        row = floor_audit[floor_id]
        row["source_event_count"] += len(group)
        row["display_run_count"] += 1
        row["source_point_count"] += len(source_points)
        row["display_point_count"] += len(display_points)
        row["source_length"] += float(source_line.length)
        row["display_length"] += float(display_line.length)
        row["invalid_display_segment_count"] += invalid
        for key, value in shortcut_audit.items():
            row[key] += int(value)

    for floor_id, row in floor_audit.items():
        row["point_reduction_count"] = row["source_point_count"] - row["display_point_count"]
        row["point_reduction_percent"] = round(
            row["point_reduction_count"] / max(1, row["source_point_count"]) * 100.0,
            4,
        )
        row["length_reduction"] = row["source_length"] - row["display_length"]
        row["length_reduction_percent"] = round(
            row["length_reduction"] / max(row["source_length"], 1e-9) * 100.0,
            4,
        )
        row["clearance_distance"] = clearance[floor_id]
        row["maximum_deviation_from_original_route"] = maximum_deviation[floor_id]
        row["safe"] = row["invalid_display_segment_count"] == 0

    output_payload = {
        "type": "FeatureCollection",
        "name": "safety_certified_beautified_display_route",
        "properties": {
            "original_route_geojson": str(original_path),
            "effective_free_areas": str(free_path),
            "policy": "contiguous_pass_run_line_of_sight_shortcut",
            "original_route_preserved_for_audit": True,
        },
        "features": display_features + passthrough_features,
    }
    _write_json(output_path, output_payload)
    audit = {
        "schema_version": 1,
        "status": "safe" if all(row["safe"] for row in floor_audit.values()) else "unsafe",
        "policy": "display_only_line_of_sight_shortcut_with_vector_recertification",
        "original_route_preserved_for_audit": True,
        "inputs": {
            "original_route_geojson": str(original_path),
            "effective_free_areas": str(free_path),
        },
        "outputs": {"beautified_route_geojson": str(output_path)},
        "configuration": {
            "max_lookahead_points": int(max_lookahead),
            "join_tolerance_by_floor": join_tolerance,
            "clearance_distance_by_floor": clearance,
            "maximum_deviation_from_original_route_by_floor": maximum_deviation,
            "curve_interpolation_enabled": False,
            "reason_curve_disabled": "unconstrained splines can cut across obstacles",
        },
        "floors": dict(floor_audit),
        "counts": {
            "source_route_event_count": len(route_features),
            "display_route_run_count": len(display_features),
            "source_point_count": sum(row["source_point_count"] for row in floor_audit.values()),
            "display_point_count": sum(row["display_point_count"] for row in floor_audit.values()),
            "invalid_display_segment_count": sum(
                row["invalid_display_segment_count"] for row in floor_audit.values()
            ),
        },
        "elapsed_seconds": time.perf_counter() - started,
    }
    _write_json(review_path, audit)
    audit["outputs"]["audit_json"] = str(review_path)
    return audit


def _approved_obstacle_paths(run_dir: Path) -> Tuple[List[Path], Path | None]:
    from multi_drawing_pipeline.stages.fire_route_core import navigation_obstacles

    approved = navigation_obstacles.approved_manifest(run_dir)
    if approved:
        originals = [
            Path(row["path"]).resolve()
            for row in approved.get("sources", [])
            if not row.get("is_repair") and Path(row["path"]).is_file()
        ]
        repairs = [
            Path(row["path"]).resolve()
            for row in approved.get("sources", [])
            if row.get("is_repair") and Path(row["path"]).is_file()
        ]
        return originals, repairs[0] if repairs else None
    summary_path = run_dir / "pipeline_summary.json"
    if not summary_path.is_file():
        return [], None
    summary = _read_json(summary_path)
    originals = [
        Path(value).resolve()
        for value in (summary.get("obstacle_recognition") or {}).get("union_geojsons", [])
        if Path(value).is_file()
    ]
    return originals, None


def write_beautified_route_dxf(
    run_dir: Path | str,
    input_dxf: Path | str,
    beautified_route_geojson: Path | str,
) -> Path:
    """Write the one final DXF with beautified, re-certified route geometry."""

    from multi_drawing_pipeline.stages.fire_route_core import stage_10_route_outputs

    run = Path(run_dir).resolve()
    source = Path(input_dxf).resolve()
    walk_dir = run / "path_planning" / "dual_graph" / "physical_walk"
    visit_plan = _read_json(walk_dir / "control_visit_plan.json")
    forwarding = _read_json(walk_dir / "forwarding_route.json")
    beauty = _read_json(Path(beautified_route_geojson).resolve())
    events_by_floor: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for feature in beauty.get("features", []) or []:
        properties = dict(feature.get("properties") or {})
        if properties.get("feature_type") != "route_edge_traversal":
            continue
        floor_id = _text(properties.get("floor_id"))
        if not floor_id:
            continue
        events_by_floor[floor_id].append(
            {**properties, "geometry": copy.deepcopy(feature.get("geometry") or {})}
        )
    for floor_id, floor in (forwarding.get("floors") or {}).items():
        if isinstance(floor, dict):
            floor["traversal_events"] = events_by_floor.get(str(floor_id), [])

    original_obstacles, repair_path = _approved_obstacle_paths(run)
    output_path = walk_dir / "{}_final_safe_route.dxf".format(source.stem)
    try:
        result = stage_10_route_outputs._s10_annotated.write_annotated_route_dxf(
            source,
            output_path,
            visit_plan,
            forwarding,
            original_obstacle_paths=original_obstacles,
            approved_repair_path=repair_path,
        )
    except PermissionError:
        versioned = walk_dir / "{}_final_safe_route_beautified_{}.dxf".format(
            source.stem, time.strftime("%Y%m%d_%H%M%S")
        )
        result = stage_10_route_outputs._s10_annotated.write_annotated_route_dxf(
            source,
            versioned,
            visit_plan,
            forwarding,
            original_obstacle_paths=original_obstacles,
            approved_repair_path=repair_path,
        )
    return Path(result).resolve()


def run_main_route_beautification(
    run_dir: Path | str,
    input_dxf: Path | str,
    effective_free_areas: Path | str,
    *,
    write_dxf: bool,
) -> Dict[str, Any]:
    """Main-pipeline adapter run between safety audit and PNG/DXF publishing."""

    run = Path(run_dir).resolve()
    walk_dir = run / "path_planning" / "dual_graph" / "physical_walk"
    original_geojson = walk_dir / "forwarding_route.geojson"
    beauty_geojson = walk_dir / "forwarding_route_beautified.geojson"
    audit_path = walk_dir / "route_beautification_audit.json"
    audit = beautify_route_geojson(
        original_geojson,
        effective_free_areas,
        output_geojson=beauty_geojson,
        audit_path=audit_path,
    )
    dxf_path: Path | None = None
    if write_dxf:
        dxf_path = write_beautified_route_dxf(run, input_dxf, beauty_geojson)
    result = {
        **audit,
        "outputs": {
            **audit.get("outputs", {}),
            "original_route_geojson": str(original_geojson.resolve()),
            "beautified_route_geojson": str(beauty_geojson.resolve()),
            "audit_json": str(audit_path.resolve()),
            "annotated_route_dxf": str(dxf_path) if dxf_path else "",
        },
    }
    _write_json(audit_path, result)
    return result


__all__ = [
    "beautify_route_geojson",
    "run_main_route_beautification",
    "write_beautified_route_dxf",
]
