"""Independent raw-CAD geometry safety layer for physical navigation.

The independent raw-CAD evidence does not consume Stage 05 decisions. It reads
the expanded vector inventory and derives conservative barrier strokes from
geometry/topology only.  The resulting index is used to reject physical graph
edges which cross raw CAD boundaries, except inside a confirmed door Portal.
Approved original+repair polygons are an additional non-exemptible hard check.
"""

from __future__ import annotations

import csv
import hashlib
import heapq
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    Point,
    box,
    mapping,
    shape,
)
from shapely.ops import unary_union
from shapely.prepared import prep
from shapely.strtree import STRtree

try:
    import ezdxf
    from ezdxf import disassemble
    from ezdxf.math import bulge_to_arc
except ImportError:  # pragma: no cover - production requirements include ezdxf
    ezdxf = None
    disassemble = None
    bulge_to_arc = None


@dataclass(frozen=True)
class RawCadSafetyConfig:
    min_segment_pixels: float = 10.0
    connected_segment_pixels: float = 25.0
    endpoint_snap_pixels: float = 0.35
    pair_min_separation_pixels: float = 0.18
    pair_max_separation_pixels: float = 5.5
    parallel_angle_tolerance_degrees: float = 9.0
    minimum_parallel_overlap_ratio: float = 0.42
    door_arc_min_pixels: float = 2.5
    door_arc_max_pixels: float = 24.0
    endpoint_touch_tolerance_pixels: float = 0.22
    target_approach_tolerance_pixels: float = 4.0
    local_detour_margin_pixels: float = 12.0
    local_detour_grid_pixels: float = 1.0
    local_detour_clearance_pixels: float = 0.32
    local_detour_max_cells: int = 60_000
    door_leaf_min_pixels: float = 5.5
    door_leaf_max_pixels: float = 14.0
    door_sweep_min_degrees: float = 68.0
    door_sweep_max_degrees: float = 112.0


@dataclass(frozen=True)
class RawCadSafetyResult:
    safe_graph: Path
    barriers_geojson: Path
    door_evidence_geojson: Path
    rejected_edges_geojson: Path
    audit_json: Path
    audit: dict[str, Any]


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return path.resolve()


def _source_fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    token = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|door_rule2_85_95deg_600_1150mm_v3"
    return {
        "path": str(path.resolve()),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "policy_version": "door_rule2_85_95deg_600_1150mm_v3",
        "cache_key": hashlib.sha256(token.encode("utf-8")).hexdigest(),
    }


def _resolve_source_dxf(
    run_dir: Path,
    source_dxf: Path | str | None = None,
) -> Path:
    """Resolve Stage 05B's real input without depending on a later-stage summary.

    ``pipeline_summary.json`` is a final/checkpoint artifact and is not
    guaranteed to exist when Stage 07 invokes this module during a fresh run.
    The explicit Stage 01 output is authoritative.  Stage 05 and Stage 04
    artifacts are compatibility fallbacks for resuming older interrupted runs.
    """

    if source_dxf is not None and str(source_dxf).strip():
        explicit = Path(source_dxf).expanduser().resolve()
        if not explicit.is_file() or explicit.suffix.lower() != ".dxf":
            raise FileNotFoundError("Stage 05B source DXF does not exist: {}".format(explicit))
        return explicit

    candidates = (
        (run_dir / "obstacles" / "floor_obstacle_recognition_result.json", "input_dxf"),
        (
            run_dir / "inspection_objects" / "region_inspection_instances_report.json",
            "input_dxf",
        ),
        (run_dir / "pipeline_summary.json", "input_dxf"),
    )
    checked: list[str] = []
    for artifact, field in candidates:
        checked.append(str(artifact))
        if not artifact.is_file():
            continue
        try:
            value = str(_read_json(artifact).get(field) or "").strip()
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not value:
            continue
        candidate = Path(value).expanduser().resolve()
        if candidate.is_file() and candidate.suffix.lower() == ".dxf":
            return candidate
    raise FileNotFoundError(
        "Stage 05B cannot resolve source DXF; pass source_dxf explicitly. Checked: {}".format(
            ", ".join(checked)
        )
    )


def _feature_collection(features: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": list(features)}


def _polygonal_parts(geometry: Any) -> list[Any]:
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    result: list[Any] = []
    for part in getattr(geometry, "geoms", []):
        result.extend(_polygonal_parts(part))
    return result


def _linear_parts(geometry: Any) -> list[LineString]:
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type in {"LineString", "LinearRing"}:
        line = LineString(geometry.coords)
        return [line] if len(line.coords) >= 2 and line.length > 0 else []
    result: list[LineString] = []
    for part in getattr(geometry, "geoms", []):
        result.extend(_linear_parts(part))
    return result


def _load_floor_domains(free_areas_path: Path) -> dict[str, Any]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for feature in _read_json(free_areas_path).get("features", []) or []:
        floor_id = str((feature.get("properties") or {}).get("floor_id") or "").strip()
        geometry_payload = feature.get("geometry")
        if floor_id and geometry_payload:
            grouped[floor_id].append(shape(geometry_payload))
    return {
        floor_id: unary_union(geometries)
        for floor_id, geometries in grouped.items()
        if geometries
    }


def _load_pixel_sizes(run_dir: Path, floors: Iterable[str]) -> dict[str, float]:
    summary_path = run_dir / "area_graph" / "area_graph_summary.json"
    payload = _read_json(summary_path) if summary_path.is_file() else {}
    source = payload.get("pixel_size_by_floor") or {}
    values = [float(value) for value in source.values() if float(value) > 0]
    fallback = sorted(values)[len(values) // 2] if values else 100.0
    result: dict[str, float] = {}
    for floor_id in floors:
        physical_floor = str(floor_id).split("__", 1)[0]
        result[str(floor_id)] = float(source.get(floor_id) or source.get(physical_floor) or fallback)
    return result


def _door_arc_layer_excluded(layer: str) -> bool:
    text = str(layer or "").upper()
    return any(
        marker in text
        for marker in (
            "GRID", "AXIS", "DIM", "ANNO", "TEXT", "LEAD", "HATCH",
            "FURN", "FIXT", "TOLT", "STAIR", "STRS", "PIPE", "DUCT",
            "轴网", "轴线", "标注", "尺寸", "家具", "洁具", "楼梯", "管线",
        )
    )


def _expanded_insert_entities(entities: Iterable[Any]) -> Iterable[Any]:
    """Expand INSERTs only; keep dimensions/other annotation containers intact."""
    for entity in entities:
        if entity.dxftype() != "INSERT":
            yield entity
            continue
        try:
            inserts = list(entity.multi_insert()) if entity.mcount > 1 else [entity]
        except Exception:
            inserts = [entity]
        for insert in inserts:
            try:
                children = insert.virtual_entities()
                yield from _expanded_insert_entities(children)
            except Exception:
                continue


def _floor_for_center(
    x: float,
    y: float,
    bounds_by_floor: Mapping[str, tuple[float, float, float, float]],
) -> str:
    for floor_id, (minx, miny, maxx, maxy) in bounds_by_floor.items():
        if minx <= x <= maxx and miny <= y <= maxy:
            return floor_id
    return ""


def _arc_line(
    center_x: float,
    center_y: float,
    radius: float,
    start_radians: float,
    end_radians: float,
    *,
    point_count: int = 17,
) -> tuple[LineString, float]:
    sweep = (end_radians - start_radians) % (2.0 * math.pi)
    coordinates = [
        (
            center_x + radius * math.cos(start_radians + sweep * index / (point_count - 1)),
            center_y + radius * math.sin(start_radians + sweep * index / (point_count - 1)),
        )
        for index in range(point_count)
    ]
    return LineString(coordinates), math.degrees(sweep)


def extract_dxf_door_swing_evidence(
    run_dir: Path | str,
    *,
    source_dxf: Path | str | None = None,
    config: RawCadSafetyConfig | None = None,
) -> Path:
    """Recursively expand source DXF blocks and cache geometric door-swing arcs."""
    if ezdxf is None:
        raise RuntimeError("ezdxf is required for direct DXF door-swing extraction")
    config = config or RawCadSafetyConfig()
    run = Path(run_dir).resolve()
    output = run / "raw_cad_safety" / "dxf_door_swing_evidence.geojson"
    resolved_source_dxf = _resolve_source_dxf(run, source_dxf)
    fingerprint = _source_fingerprint(resolved_source_dxf)
    if output.is_file():
        cached = _read_json(output)
        if (cached.get("source_fingerprint") or {}).get("cache_key") == fingerprint["cache_key"]:
            return output.resolve()

    domains = _load_floor_domains(run / "navigation_graph" / "inputs" / "free_areas.geojson")
    bounds_by_floor = {floor_id: tuple(domain.bounds) for floor_id, domain in domains.items()}
    document = ezdxf.readfile(resolved_source_dxf)
    from multi_drawing_pipeline.stages.fire_route_core import stage_05_obstacles
    api = stage_05_obstacles._s05_obstacles
    _, audit = api.review_layer_components(document, [], {}, api, api.ObstacleConfig())
    features, seen = [], set()
    for component in audit['confirmed_doors']:
        for evidence in component['door_arc_evidence']:
            cx, cy = evidence['center']
            floor_id = _floor_for_center(cx, cy, bounds_by_floor)
            if not floor_id:
                continue
            radius = evidence['radius_drawing_units']
            start_angle, end_angle = evidence['start_angle'], evidence['end_angle']
            key = (floor_id, round(cx, 4), round(cy, 4), round(radius, 4),
                   round(start_angle % 360, 5), round(end_angle % 360, 5))
            if key in seen:
                continue
            seen.add(key)
            arc, sweep = _arc_line(cx, cy, radius, math.radians(start_angle), math.radians(end_angle))
            features.append({'type': 'Feature', 'properties': {
                'floor_id': floor_id, 'kind': 'dxf_door_swing_arc',
                'evidence_source': 'dxf_recursive_door_swing_arc',
                'door_rule_version': audit['rule_version'],
                'layer': component['layer'], 'entity_type': component['entity_type'],
                'source_handle': component['handle'], 'instance_path': component['instance_path'],
                'center_x': cx, 'center_y': cy, 'radius': radius,
                'radius_mm': evidence['radius_mm'], 'sweep_degrees': sweep, 'confidence': 0.93,
            }, 'geometry': mapping(arc)})
    payload = _feature_collection(features)
    payload['schema_version'] = 2
    payload['source_fingerprint'] = fingerprint
    payload['policy'] = audit['door_rule']
    payload['mm_per_drawing_unit'] = audit['mm_per_drawing_unit']
    payload['counts'] = {'door_swing_arc_count': len(features),
                         'by_floor': dict(Counter(f['properties']['floor_id'] for f in features))}
    return _write_json(output, payload)


def _inventory_path(run_dir: Path) -> Path:
    summary = _read_json(run_dir / "obstacles" / "floor_obstacle_recognition_result.json")
    inventory_dir = Path(str(summary["inventory_dir"])).resolve()
    path = inventory_dir / "cad_geometry_inventory.csv"
    if not path.is_file():
        raise FileNotFoundError(f"CAD geometry inventory is missing: {path}")
    return path


def _door_semantic_evidence(row: Mapping[str, Any]) -> bool:
    text = " ".join(
        str(row.get(key) or "")
        for key in ("layer", "parent_block_name", "block_path")
    ).upper()
    window_markers = ("WIND", "WINDOW", "GLZ", "GLASS", "CURTAIN", "窗", "玻璃", "幕墙")
    door_markers = ("DOOR", "门", "OPENING", "洞口")
    return any(token in text for token in door_markers) and not any(
        token in text for token in window_markers
    )


def _geometry_rows(run_dir: Path) -> Iterable[dict[str, Any]]:
    with _inventory_path(run_dir).open("r", encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def _candidate_floors(
    bounds: tuple[float, float, float, float],
    domain_bounds: Mapping[str, tuple[float, float, float, float]],
) -> list[str]:
    minx, miny, maxx, maxy = bounds
    return [
        floor_id
        for floor_id, (floor_minx, floor_miny, floor_maxx, floor_maxy) in domain_bounds.items()
        if not (
            maxx < floor_minx
            or minx > floor_maxx
            or maxy < floor_miny
            or miny > floor_maxy
        )
    ]


def _canonical_line_key(line: LineString, precision: int = 4) -> tuple[Any, ...]:
    coords = tuple((round(float(x), precision), round(float(y), precision)) for x, y in line.coords)
    reversed_coords = tuple(reversed(coords))
    return min(coords, reversed_coords)


def _extract_raw_linework(
    run_dir: Path,
    domains: Mapping[str, Any],
    pixel_by_floor: Mapping[str, float],
    config: RawCadSafetyConfig,
    source_dxf: Path | str | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], dict[str, int]]:
    lines_by_floor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    doors_by_floor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: dict[str, set[tuple[Any, ...]]] = defaultdict(set)
    counts: Counter[str] = Counter()
    domain_bounds = {floor_id: tuple(domain.bounds) for floor_id, domain in domains.items()}
    for row in _geometry_rows(run_dir):
        try:
            payload = json.loads(str(row.get("geometry_json") or "{}"))
        except json.JSONDecodeError:
            counts["invalid_geometry_json"] += 1
            continue
        raw_lines = payload.get("lines") or []
        raw_polygons = payload.get("polygons") or []
        if not raw_lines and not raw_polygons:
            continue
        try:
            bounds = (
                float(row["bbox_minx"]),
                float(row["bbox_miny"]),
                float(row["bbox_maxx"]),
                float(row["bbox_maxy"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        floor_ids = _candidate_floors(bounds, domain_bounds)
        if not floor_ids:
            continue
        is_closed = str(row.get("is_closed") or "0") in {"1", "true", "True"}
        entity_type = str(row.get("entity_type") or "").upper()
        source_id = str(row.get("object_id") or row.get("handle") or "")
        source_lines: list[LineString] = []
        for coordinates in raw_lines:
            try:
                line = LineString([(float(point[0]), float(point[1])) for point in coordinates])
            except (TypeError, ValueError, IndexError):
                continue
            source_lines.extend(_linear_parts(line))
        for coordinates in raw_polygons:
            try:
                polygon = shape({"type": "Polygon", "coordinates": coordinates})
            except Exception:
                continue
            source_lines.extend(_linear_parts(polygon.boundary))
            is_closed = True
        for floor_id in floor_ids:
            pixel = pixel_by_floor[floor_id]
            for line in source_lines:
                floor_minx, floor_miny, floor_maxx, floor_maxy = domain_bounds[floor_id]
                line_minx, line_miny, line_maxx, line_maxy = line.bounds
                if (
                    line_maxx < floor_minx - pixel
                    or line_minx > floor_maxx + pixel
                    or line_maxy < floor_miny - pixel
                    or line_miny > floor_maxy + pixel
                ):
                    continue
                for part in (line,):
                    key = _canonical_line_key(part)
                    if key in seen[floor_id]:
                        continue
                    seen[floor_id].add(key)
                    record = {
                        "geometry": part,
                        "source_object_id": source_id,
                        "source_handle": str(row.get("handle") or ""),
                        "source_entity_type": entity_type,
                        "source_layer": str(row.get("layer") or ""),
                        "source_parent_block": str(row.get("parent_block_name") or ""),
                        "closed_source": bool(is_closed),
                    }
                    lines_by_floor[floor_id].append(record)
                    counts["raw_line_part_count"] += 1
    # Door semantics or arbitrary curved geometry alone cannot grant passage.
    evidence_path = extract_dxf_door_swing_evidence(run_dir, source_dxf=source_dxf)
    for feature in _read_json(evidence_path).get('features', []):
        properties = feature['properties']
        floor_id = properties['floor_id']
        if floor_id in domains:
            doors_by_floor[floor_id].append({
                'geometry': shape(feature['geometry']),
                'evidence_source': 'door_rule_2_confirmed',
                'source_object_id': properties.get('instance_path', ''),
                'source_handle': properties.get('source_handle', ''),
            })
    counts["floor_count"] = len(lines_by_floor)
    counts["door_evidence_part_count"] = sum(len(rows) for rows in doors_by_floor.values())
    return dict(lines_by_floor), dict(doors_by_floor), dict(counts)


def _segment_vector(line: LineString) -> tuple[float, float, float]:
    x1, y1 = line.coords[0]
    x2, y2 = line.coords[-1]
    dx, dy = float(x2 - x1), float(y2 - y1)
    length = math.hypot(dx, dy)
    return (dx / length, dy / length, length) if length > 0 else (0.0, 0.0, 0.0)


def _parallel_support(
    first: LineString,
    second: LineString,
    pixel: float,
    config: RawCadSafetyConfig,
) -> bool:
    ux, uy, length_a = _segment_vector(first)
    vx, vy, length_b = _segment_vector(second)
    minimum_length = config.min_segment_pixels * pixel
    if min(length_a, length_b) < minimum_length:
        return False
    if abs(ux * vx + uy * vy) < math.cos(math.radians(config.parallel_angle_tolerance_degrees)):
        return False
    separation = float(first.distance(second))
    if not (
        config.pair_min_separation_pixels * pixel
        <= separation
        <= config.pair_max_separation_pixels * pixel
    ):
        return False
    origin_x, origin_y = first.coords[0]
    interval_a = (0.0, length_a)
    projections = [
        (float(x) - origin_x) * ux + (float(y) - origin_y) * uy
        for x, y in (second.coords[0], second.coords[-1])
    ]
    interval_b = (min(projections), max(projections))
    overlap = max(0.0, min(interval_a[1], interval_b[1]) - max(interval_a[0], interval_b[0]))
    return overlap >= config.minimum_parallel_overlap_ratio * min(length_a, length_b)


def _endpoint_key(point: tuple[float, float], tolerance: float) -> tuple[int, int]:
    return (int(round(float(point[0]) / tolerance)), int(round(float(point[1]) / tolerance)))


def _clearly_nonstructural(row: Mapping[str, Any]) -> bool:
    """Suppress drafting/service linework without requiring positive wall names."""
    text = f"{row.get('source_layer', '')} {row.get('source_parent_block', '')}".upper()
    markers = (
        "GRID", "AXIS", "轴网", "轴线", "DIM", "ANNO", "TEXT", "LEAD",
        "标注", "尺寸", "编号", "HATCH", "填充", "PIPE", "DUCT", "风管",
        "管线", "ABOV", "STAIR", "STRS",
    )
    return any(marker in text for marker in markers)


def _select_barriers(
    rows_by_floor: Mapping[str, list[dict[str, Any]]],
    pixel_by_floor: Mapping[str, float],
    domains: Mapping[str, Any],
    config: RawCadSafetyConfig,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    selected_by_floor: dict[str, list[dict[str, Any]]] = {}
    audit: dict[str, Any] = {}
    for floor_id, rows in rows_by_floor.items():
        pixel = pixel_by_floor[floor_id]
        geometries = [row["geometry"] for row in rows]
        tree = STRtree(geometries) if geometries else None
        floor_bounds = domains[floor_id].bounds
        floor_diagonal = math.hypot(
            floor_bounds[2] - floor_bounds[0], floor_bounds[3] - floor_bounds[1]
        )
        maximum_safety_segment_length = max(12.0 * pixel, 0.35 * floor_diagonal)
        selected: set[int] = set()
        closed_supported: set[int] = set()
        for index, row in enumerate(rows):
            if not row["closed_source"] or _clearly_nonstructural(row):
                continue
            line = row["geometry"]
            width = line.bounds[2] - line.bounds[0]
            height = line.bounds[3] - line.bounds[1]
            short_side = min(width, height)
            long_side = max(width, height)
            aspect_ratio = long_side / max(short_side, 0.25 * pixel)
            thin_boundary = (
                0.18 * pixel <= short_side <= 6.0 * pixel
                and long_side >= 5.0 * pixel
                and aspect_ratio >= 3.0
            )
            if thin_boundary:
                closed_supported.add(index)
        selected.update(closed_supported)
        pair_supported: set[int] = set()
        maximum_separation = config.pair_max_separation_pixels * pixel
        if tree is not None:
            for index, line in enumerate(geometries):
                if (
                    rows[index]["closed_source"]
                    or
                    line.length < config.min_segment_pixels * pixel
                    or line.length > maximum_safety_segment_length
                    or _clearly_nonstructural(rows[index])
                ):
                    continue
                query = box(
                    line.bounds[0] - maximum_separation,
                    line.bounds[1] - maximum_separation,
                    line.bounds[2] + maximum_separation,
                    line.bounds[3] + maximum_separation,
                )
                for raw_other in tree.query(query):
                    other = int(raw_other)
                    if other <= index:
                        continue
                    if rows[other]["closed_source"] or _clearly_nonstructural(rows[other]):
                        continue
                    if _parallel_support(line, geometries[other], pixel, config):
                        pair_supported.update((index, other))
        selected.update(pair_supported)
        tolerance = max(config.endpoint_snap_pixels * pixel, 1e-6)
        endpoint_members: dict[tuple[int, int], list[int]] = defaultdict(list)
        for index, line in enumerate(geometries):
            if rows[index]["closed_source"]:
                continue
            endpoint_members[_endpoint_key(line.coords[0], tolerance)].append(index)
            endpoint_members[_endpoint_key(line.coords[-1], tolerance)].append(index)
        connected: set[int] = set()
        for index, line in enumerate(geometries):
            if (
                rows[index]["closed_source"]
                or
                line.length < config.connected_segment_pixels * pixel
                or line.length > maximum_safety_segment_length
                or _clearly_nonstructural(rows[index])
            ):
                continue
            first_degree = len(endpoint_members[_endpoint_key(line.coords[0], tolerance)])
            last_degree = len(endpoint_members[_endpoint_key(line.coords[-1], tolerance)])
            if first_degree >= 2 and last_degree >= 2:
                connected.add(index)
        selected.update(connected)
        selected_rows: list[dict[str, Any]] = []
        for index in sorted(selected):
            reasons: list[str] = []
            if index in closed_supported:
                reasons.append("closed_geometry_boundary")
            if index in pair_supported:
                reasons.append("parallel_boundary_pair")
            if index in connected:
                reasons.append("continuous_endpoint_topology")
            hard_barrier = (
                index in closed_supported
                or (index in pair_supported and index in connected)
            )
            selected_rows.append(
                {
                    **rows[index],
                    "barrier_reasons": reasons,
                    "barrier_strength": "hard" if hard_barrier else "audit_only_candidate",
                }
            )
        selected_by_floor[floor_id] = selected_rows
        audit[floor_id] = {
            "raw_line_count": len(rows),
            "barrier_line_count": len(selected_rows),
            "hard_barrier_line_count": sum(
                row["barrier_strength"] == "hard" for row in selected_rows
            ),
            "closed_boundary_count": len(closed_supported),
            "parallel_pair_supported_count": len(pair_supported),
            "continuous_endpoint_supported_count": len(connected),
        }
    return selected_by_floor, audit


def _portal_index(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.is_file():
        return {}
    result: dict[str, dict[str, Any]] = {}
    for feature in _read_json(path).get("features", []) or []:
        properties = dict(feature.get("properties") or {})
        portal_id = str(properties.get("portal_id") or "")
        evidence = str(properties.get("evidence_source") or "")
        if portal_id and evidence in {
            "door_layer",
            "door_geometry",
            "door_swing_arc",
            "door_semantics",
            "inventory_door_semantics",
            "inventory_door_arc",
            "dxf_recursive_door_swing_arc",
        }:
            result[portal_id] = {"geometry": shape(feature["geometry"]), "properties": properties}
    return result


def _door_passages(
    run_dir: Path,
    pixel_by_floor: Mapping[str, float],
    source_dxf: Path | str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    evidence_path = extract_dxf_door_swing_evidence(
        run_dir,
        source_dxf=source_dxf,
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, feature in enumerate(_read_json(evidence_path).get("features", []) or [], 1):
        properties = dict(feature.get("properties") or {})
        floor_id = str(properties.get("floor_id") or "")
        if not floor_id or floor_id not in pixel_by_floor:
            continue
        arc = shape(feature["geometry"])
        center = (
            float(properties.get("center_x")),
            float(properties.get("center_y")),
        )
        try:
            sector = shape(
                {
                    "type": "Polygon",
                    "coordinates": [[center, *list(arc.coords), center]],
                }
            ).buffer(pixel_by_floor[floor_id])
        except Exception:
            continue
        if sector.is_empty:
            continue
        grouped[floor_id].append(
            {
                "passage_id": f"{floor_id}_DOOR_SWING_{index:05d}",
                "geometry": sector,
                "properties": properties,
            }
        )
    return dict(grouped)


def _significant_collision(
    edge_line: LineString,
    barrier: LineString,
    endpoint_tolerance: float,
    allowed_portal: Any | None,
    target_approach_zone: Any | None = None,
) -> Any | None:
    collision = edge_line.intersection(barrier)
    if collision.is_empty:
        return None
    if allowed_portal is not None:
        collision = collision.difference(allowed_portal.buffer(endpoint_tolerance))
        if collision.is_empty:
            return None
    endpoint_zone = unary_union(
        [Point(edge_line.coords[0]).buffer(endpoint_tolerance), Point(edge_line.coords[-1]).buffer(endpoint_tolerance)]
    )
    collision = collision.difference(endpoint_zone)
    if target_approach_zone is not None and not collision.is_empty:
        collision = collision.difference(target_approach_zone)
    return None if collision.is_empty else collision


def _routing_multiplier(kind: str) -> float:
    return {
        "skeleton_edge": 1.0,
        "portal_edge": 1.0,
        "connector_portal_edge": 1.03,
        "area_anchor_access_edge": 1.08,
        "local_refinement_edge": 1.18,
        "target_access_edge": 1.3,
        "virtual_target_access_edge": 1.6,
    }.get(kind, 1.2)


def _local_safe_detour(
    edge_line: LineString,
    floor_domain: Any,
    blocked_surface: Any,
    pixel: float,
    config: RawCadSafetyConfig,
) -> LineString | None:
    """Find a local raster detour instead of merely deleting a cut graph edge."""
    start = Point(edge_line.coords[0])
    end = Point(edge_line.coords[-1])
    dx = float(end.x - start.x)
    dy = float(end.y - start.y)
    span = math.hypot(dx, dy)
    if span <= 1e-9:
        return None
    ux, uy = dx / span, dy / span
    vx, vy = -uy, ux
    margin = config.local_detour_margin_pixels * pixel
    step = config.local_detour_grid_pixels * pixel
    columns = int(math.ceil((span + 2.0 * margin) / step)) + 1
    rows = int(math.ceil((2.0 * margin) / step)) + 1
    if columns * rows > config.local_detour_max_cells:
        return None

    envelope = edge_line.buffer(margin, cap_style=3, join_style=2)
    local_blocked = blocked_surface.intersection(envelope)
    endpoint_carve = unary_union(
        [
            start.buffer(0.55 * pixel),
            end.buffer(0.55 * pixel),
        ]
    )
    local_blocked = local_blocked.difference(endpoint_carve)
    prepared_domain = prep(floor_domain.buffer(0.05 * pixel))
    prepared_blocked = prep(local_blocked) if not local_blocked.is_empty else None

    def coordinate(cell: tuple[int, int]) -> tuple[float, float]:
        column, row = cell
        along = -margin + column * step
        across = -margin + row * step
        return (
            float(start.x + ux * along + vx * across),
            float(start.y + uy * along + vy * across),
        )

    start_cell = (
        int(round(margin / step)),
        int(round(margin / step)),
    )
    end_cell = (
        int(round((margin + span) / step)),
        int(round(margin / step)),
    )
    valid_cache: dict[tuple[int, int], bool] = {}

    def valid(cell: tuple[int, int]) -> bool:
        if cell in {start_cell, end_cell}:
            return True
        if cell in valid_cache:
            return valid_cache[cell]
        column, row = cell
        if not (0 <= column < columns and 0 <= row < rows):
            return False
        point = Point(coordinate(cell))
        accepted = prepared_domain.covers(point) and (
            prepared_blocked is None or not prepared_blocked.covers(point)
        )
        valid_cache[cell] = accepted
        return accepted

    queue: list[tuple[float, float, tuple[int, int]]] = [(span, 0.0, start_cell)]
    costs: dict[tuple[int, int], float] = {start_cell: 0.0}
    previous: dict[tuple[int, int], tuple[int, int]] = {}
    moves = (
        (-1, -1, math.sqrt(2.0)), (0, -1, 1.0), (1, -1, math.sqrt(2.0)),
        (-1, 0, 1.0),                              (1, 0, 1.0),
        (-1, 1, math.sqrt(2.0)),  (0, 1, 1.0),  (1, 1, math.sqrt(2.0)),
    )
    while queue:
        _, current_cost, current = heapq.heappop(queue)
        if current == end_cell:
            break
        if current_cost > costs.get(current, math.inf) + 1e-9:
            continue
        for delta_column, delta_row, factor in moves:
            candidate = (current[0] + delta_column, current[1] + delta_row)
            if not valid(candidate):
                continue
            candidate_cost = current_cost + factor * step
            if candidate_cost + 1e-9 >= costs.get(candidate, math.inf):
                continue
            costs[candidate] = candidate_cost
            previous[candidate] = current
            heuristic = math.hypot(
                candidate[0] - end_cell[0], candidate[1] - end_cell[1]
            ) * step
            heapq.heappush(queue, (candidate_cost + heuristic, candidate_cost, candidate))
    if end_cell not in costs:
        return None

    cells = [end_cell]
    while cells[-1] != start_cell:
        cells.append(previous[cells[-1]])
    cells.reverse()
    coordinates = [tuple(edge_line.coords[0])]
    coordinates.extend(coordinate(cell) for cell in cells[1:-1])
    coordinates.append(tuple(edge_line.coords[-1]))
    route = LineString(coordinates).simplify(0.18 * pixel, preserve_topology=True)
    if not prepared_domain.covers(route):
        return None
    if not local_blocked.is_empty and not route.intersection(local_blocked).is_empty:
        return None
    return route


def _add_post_collision_door_portals(
    nodes: list[dict[str, Any]],
    accepted_edges: list[dict[str, Any]],
    passages_by_floor: Mapping[str, list[dict[str, Any]]],
    barrier_trees: Mapping[str, STRtree],
    barrier_rows: Mapping[str, list[dict[str, Any]]],
    domains: Mapping[str, Any],
    pixel_by_floor: Mapping[str, float],
    config: RawCadSafetyConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Counter[str]]:
    """Reconnect components split by safety cuts only at verified door swings."""
    parent = {str(node["node_id"]): str(node["node_id"]) for node in nodes}

    def find(node_id: str) -> str:
        while parent[node_id] != node_id:
            parent[node_id] = parent[parent[node_id]]
            node_id = parent[node_id]
        return node_id

    def union(node_a: str, node_b: str) -> None:
        root_a, root_b = find(node_a), find(node_b)
        if root_a != root_b:
            parent[root_b] = root_a

    for edge in accepted_edges:
        node_a = str(edge.get("node_a") or "")
        node_b = str(edge.get("node_b") or "")
        if node_a in parent and node_b in parent:
            union(node_a, node_b)

    floor_nodes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        if str(node.get("kind") or "") == "skeleton":
            floor_nodes[str(node.get("floor_id") or "")].append(node)
    point_trees = {
        floor_id: STRtree([Point(float(node["x"]), float(node["y"])) for node in rows])
        for floor_id, rows in floor_nodes.items()
        if rows
    }

    added_nodes: list[dict[str, Any]] = []
    added_edges: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    serial = 0
    for floor_id, passages in passages_by_floor.items():
        if floor_id not in point_trees or floor_id not in barrier_trees:
            continue
        pixel = pixel_by_floor[floor_id]
        candidates_source = floor_nodes[floor_id]
        point_tree = point_trees[floor_id]
        for passage in passages:
            passage_geometry = passage["geometry"]
            validation_zone = passage_geometry.buffer(2.5 * pixel)
            properties = passage.get("properties") or {}
            radius = float(properties.get("radius") or 8.0 * pixel)
            context = passage_geometry.buffer(8.0 * pixel)
            raw_indices = point_tree.query(context)
            candidates = [candidates_source[int(index)] for index in raw_indices]
            candidates.sort(
                key=lambda node: Point(float(node["x"]), float(node["y"])).distance(
                    passage_geometry
                )
            )
            candidates = candidates[:20]
            best: tuple[float, dict[str, Any], dict[str, Any], LineString] | None = None
            maximum_span = max(28.0 * pixel, 2.8 * radius + 10.0 * pixel)
            for first_index, node_a in enumerate(candidates):
                node_a_id = str(node_a["node_id"])
                for node_b in candidates[first_index + 1 :]:
                    node_b_id = str(node_b["node_id"])
                    if find(node_a_id) == find(node_b_id):
                        continue
                    line = LineString(
                        [
                            (float(node_a["x"]), float(node_a["y"])),
                            (float(node_b["x"]), float(node_b["y"])),
                        ]
                    )
                    if line.length < 1.0 * pixel or line.length > maximum_span:
                        continue
                    if not domains[floor_id].buffer(0.05 * pixel).covers(line):
                        continue
                    local_walk = line.intersection(validation_zone)
                    if local_walk.is_empty or float(local_walk.length) < 0.5 * pixel:
                        continue
                    collision_count = 0
                    valid = True
                    for raw_barrier_index in barrier_trees[floor_id].query(line):
                        row = barrier_rows[floor_id][int(raw_barrier_index)]
                        collision = _significant_collision(
                            line,
                            row["geometry"],
                            config.endpoint_touch_tolerance_pixels * pixel,
                            None,
                        )
                        if collision is None:
                            continue
                        collision_count += 1
                        if not validation_zone.covers(collision):
                            valid = False
                            break
                    if not valid or collision_count == 0:
                        continue
                    score = float(line.length)
                    if best is None or score < best[0]:
                        best = (score, node_a, node_b, line)
            if best is None:
                continue
            _, node_a, node_b, line = best
            crossing = line.intersection(validation_zone)
            crossing_parts = _linear_parts(crossing)
            if not crossing_parts:
                continue
            crossing_part = max(crossing_parts, key=lambda geometry: geometry.length)
            portal_point = crossing_part.interpolate(0.5, normalized=True)
            serial += 1
            portal_id = f"RAW_DOOR_PORTAL::{floor_id}::{serial:05d}"
            node_a_id = str(node_a["node_id"])
            node_b_id = str(node_b["node_id"])
            added_nodes.append(
                {
                    "node_id": portal_id,
                    "kind": "connector_portal",
                    "floor_id": floor_id,
                    "component_id": "RAW_CAD_POST_COLLISION_DOOR",
                    "area_id": "",
                    "portal_id": portal_id,
                    "x": float(portal_point.x),
                    "y": float(portal_point.y),
                    "evidence_source": "dxf_recursive_door_swing_arc",
                    "validation_method": "post_collision_two_reachable_spaces_and_door_sector",
                    "source_handle": str(properties.get("source_handle") or ""),
                }
            )
            for suffix, endpoint_node, coordinates in (
                ("A", node_a_id, [tuple(line.coords[0]), (portal_point.x, portal_point.y)]),
                ("B", node_b_id, [(portal_point.x, portal_point.y), tuple(line.coords[-1])]),
            ):
                geometry = LineString(coordinates)
                added_edges.append(
                    {
                        "edge_id": f"RAW_DOOR_EDGE::{floor_id}::{serial:05d}::{suffix}",
                        "node_a": endpoint_node if suffix == "A" else portal_id,
                        "node_b": portal_id if suffix == "A" else endpoint_node,
                        "length": float(geometry.length),
                        "kind": "connector_portal_edge",
                        "floor_id": floor_id,
                        "component_id": "RAW_CAD_POST_COLLISION_DOOR",
                        "connector_portal_id": portal_id,
                        "evidence_source": "dxf_recursive_door_swing_arc",
                        "validation_method": "post_collision_two_reachable_spaces_and_door_sector",
                        "geometry": mapping(geometry),
                        "routing_cost_multiplier": 1.03,
                        "routing_cost": float(geometry.length) * 1.03,
                        "corridor_backbone_preference": "preferred",
                        "raw_cad_collision_certified": True,
                        "confirmed_door_crossing": True,
                        "confirmed_door_passage_ids": [str(passage["passage_id"])],
                    }
                )
            parent[portal_id] = portal_id
            union(node_a_id, portal_id)
            union(portal_id, node_b_id)
            counts[floor_id] += 1
    return added_nodes, added_edges, counts


def build_and_certify_physical_graph(
    run_dir: Path | str,
    graph_path: Path | str,
    connector_portals_path: Path | str | None,
    *,
    output_dir: Path | str | None = None,
    source_dxf: Path | str | None = None,
    config: RawCadSafetyConfig | None = None,
) -> RawCadSafetyResult:
    config = config or RawCadSafetyConfig()
    run = Path(run_dir).resolve()
    output = Path(output_dir or run / "raw_cad_safety").resolve()
    output.mkdir(parents=True, exist_ok=True)
    from multi_drawing_pipeline.stages.fire_route_core import navigation_obstacles as hard_walls
    hard_index = hard_walls.load_index(run)
    if hard_index:
        hard_walls.verify_navigation_inputs(run, hard_index)
    free_areas_path = run / "navigation_graph" / "inputs" / "free_areas.geojson"
    domains = _load_floor_domains(free_areas_path)
    pixel_by_floor = _load_pixel_sizes(run, domains)
    raw_rows, door_rows, extraction_counts = _extract_raw_linework(
        run, domains, pixel_by_floor, config, source_dxf=source_dxf
    )
    barriers_by_floor, floor_extraction_audit = _select_barriers(
        raw_rows, pixel_by_floor, domains, config
    )

    barrier_features: list[dict[str, Any]] = []
    for floor_id, rows in sorted(barriers_by_floor.items()):
        geometry = MultiLineString([list(row["geometry"].coords) for row in rows]) if rows else GeometryCollection()
        barrier_features.append(
            {
                "type": "Feature",
                "properties": {
                    "floor_id": floor_id,
                    "kind": "independent_raw_cad_barrier_strokes",
                    "barrier_line_count": len(rows),
                },
                "geometry": mapping(geometry),
            }
        )
    barriers_path = _write_json(output / "raw_cad_barriers.geojson", _feature_collection(barrier_features))

    door_features: list[dict[str, Any]] = []
    for floor_id, rows in sorted(door_rows.items()):
        for row in rows:
            door_features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "floor_id": floor_id,
                        "kind": "independent_door_evidence",
                        "evidence_source": row["evidence_source"],
                        "source_object_id": row["source_object_id"],
                        "source_handle": row["source_handle"],
                    },
                    "geometry": mapping(row["geometry"]),
                }
            )
    doors_path = _write_json(output / "raw_cad_door_evidence.geojson", _feature_collection(door_features))

    graph_source = Path(graph_path).resolve()
    graph = _read_json(graph_source)
    nodes_by_id = {
        str(node.get("node_id") or ""): node
        for node in graph.get("nodes", []) or []
        if str(node.get("node_id") or "")
    }
    portals = _portal_index(Path(connector_portals_path).resolve() if connector_portals_path else None)
    passages_by_floor = _door_passages(
        run,
        pixel_by_floor,
        source_dxf=source_dxf,
    )
    passage_trees = {
        floor_id: STRtree([row["geometry"] for row in rows])
        for floor_id, rows in passages_by_floor.items()
        if rows
    }
    trees: dict[str, STRtree] = {}
    strokes: dict[str, list[dict[str, Any]]] = {}
    for floor_id, rows in barriers_by_floor.items():
        hard_rows = [row for row in rows if row["barrier_strength"] == "hard"]
        strokes[floor_id] = hard_rows
        if hard_rows:
            trees[floor_id] = STRtree([row["geometry"] for row in hard_rows])
    blocked_surfaces = {
        floor_id: unary_union(
            [
                row["geometry"].buffer(
                    config.local_detour_clearance_pixels * pixel_by_floor[floor_id],
                    cap_style=2,
                    join_style=2,
                )
                for row in rows
            ]
        )
        for floor_id, rows in strokes.items()
        if rows
    }

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    rejected_features: list[dict[str, Any]] = []
    rejected_by_kind: Counter[str] = Counter()
    door_exempted_by_floor: Counter[str] = Counter()
    local_detour_by_floor: Counter[str] = Counter()
    for index, raw_edge in enumerate(graph.get("edges", []) or [], 1):
        edge = dict(raw_edge)
        floor_id = str(edge.get("floor_id") or "")
        coordinates = (edge.get("geometry") or {}).get("coordinates") or []
        if len(coordinates) < 2 or floor_id not in trees:
            multiplier = _routing_multiplier(str(edge.get("kind") or ""))
            edge["routing_cost_multiplier"] = multiplier
            edge["routing_cost"] = float(edge.get("length") or 0.0) * multiplier
            edge["raw_cad_collision_certified"] = True
            accepted.append(edge)
            continue
        line = LineString([(float(point[0]), float(point[1])) for point in coordinates])
        if line.length <= 0:
            edge["routing_cost_multiplier"] = 1.0
            edge["routing_cost"] = 0.0
            edge["raw_cad_collision_certified"] = True
            accepted.append(edge)
            continue
        portal_id = str(edge.get("connector_portal_id") or "")
        portal = portals.get(portal_id)
        allowed_portal = portal["geometry"] if portal else None
        tolerance = config.endpoint_touch_tolerance_pixels * pixel_by_floor[floor_id]
        target_approach_zone = None
        if str(edge.get("kind") or "") in {"target_access_edge", "virtual_target_access_edge"}:
            for node_id in (str(edge.get("node_a") or ""), str(edge.get("node_b") or "")):
                node = nodes_by_id.get(node_id) or {}
                if str(node.get("kind") or "") in {"target", "virtual_target_access"}:
                    target_approach_zone = Point(
                        float(node.get("x")), float(node.get("y"))
                    ).buffer(config.target_approach_tolerance_pixels * pixel_by_floor[floor_id])
                    break
        collisions: list[tuple[dict[str, Any], Any]] = []
        for raw_barrier_index in trees[floor_id].query(line):
            barrier_row = strokes[floor_id][int(raw_barrier_index)]
            collision = _significant_collision(
                line,
                barrier_row["geometry"],
                tolerance,
                allowed_portal,
                target_approach_zone,
            )
            if collision is not None:
                collisions.append((barrier_row, collision))
        if collisions:
            confirmed_passage_ids: set[str] = set()
            uncovered_collisions: list[tuple[dict[str, Any], Any]] = []
            passage_tree = passage_trees.get(floor_id)
            passage_rows = passages_by_floor.get(floor_id, [])
            free_space_valid = bool(domains[floor_id].covers(line))
            for barrier_row, collision in collisions:
                covered_by_passage = False
                if passage_tree is not None and free_space_valid:
                    for raw_passage_index in passage_tree.query(collision):
                        passage = passage_rows[int(raw_passage_index)]
                        passage_geometry = passage["geometry"]
                        if not passage_geometry.covers(collision):
                            continue
                        local_walk = line.intersection(passage_geometry)
                        if local_walk.is_empty or float(local_walk.length) < 0.5 * pixel_by_floor[floor_id]:
                            continue
                        confirmed_passage_ids.add(str(passage["passage_id"]))
                        covered_by_passage = True
                        break
                if not covered_by_passage:
                    uncovered_collisions.append((barrier_row, collision))
            if not uncovered_collisions and confirmed_passage_ids:
                multiplier = _routing_multiplier(str(edge.get("kind") or ""))
                edge["routing_cost_multiplier"] = multiplier
                edge["routing_cost"] = float(edge.get("length") or line.length) * multiplier
                edge["corridor_backbone_preference"] = "preferred" if multiplier <= 1.05 else "penalized_access_or_refinement"
                edge["raw_cad_collision_certified"] = True
                edge["confirmed_door_crossing"] = True
                edge["confirmed_door_passage_ids"] = sorted(confirmed_passage_ids)
                edge["door_side_free_space_validation"] = "entire_edge_covered_by_stage06_vector_free_space"
                accepted.append(edge)
                door_exempted_by_floor[floor_id] += 1
                continue
            collisions = uncovered_collisions or collisions
            edge_id = str(edge.get("edge_id") or f"EDGE_{index:08d}")
            kind = str(edge.get("kind") or "")
            if kind == "skeleton_edge" and floor_id in blocked_surfaces:
                detour = _local_safe_detour(
                    line,
                    domains[floor_id],
                    blocked_surfaces[floor_id],
                    pixel_by_floor[floor_id],
                    config,
                )
                if detour is not None:
                    original_length = float(edge.get("length") or line.length)
                    multiplier = _routing_multiplier(kind)
                    edge["geometry"] = mapping(detour)
                    edge["length"] = float(detour.length)
                    edge["routing_cost_multiplier"] = multiplier
                    edge["routing_cost"] = float(detour.length) * multiplier
                    edge["corridor_backbone_preference"] = "preferred"
                    edge["raw_cad_collision_certified"] = True
                    edge["raw_cad_local_detour"] = True
                    edge["raw_cad_original_straight_length"] = original_length
                    edge["raw_cad_detour_ratio"] = float(detour.length) / max(original_length, 1e-9)
                    accepted.append(edge)
                    local_detour_by_floor[floor_id] += 1
                    continue
            rejected_by_kind[kind] += 1
            rejected.append(
                {
                    "edge_id": edge_id,
                    "floor_id": floor_id,
                    "kind": kind,
                    "reason": "crosses_independent_raw_cad_barrier_outside_confirmed_door_portal",
                    "collision_count": len(collisions),
                    "cad_entities": [
                        {
                            "source_object_id": row["source_object_id"],
                            "source_handle": row["source_handle"],
                            "source_layer": row["source_layer"],
                            "source_entity_type": row["source_entity_type"],
                            "closed_source": bool(row["closed_source"]),
                            "geometry_length_pixels": round(
                                float(row["geometry"].length) / pixel_by_floor[floor_id], 3
                            ),
                            "geometry_width_pixels": round(
                                float(row["geometry"].bounds[2] - row["geometry"].bounds[0])
                                / pixel_by_floor[floor_id],
                                3,
                            ),
                            "geometry_height_pixels": round(
                                float(row["geometry"].bounds[3] - row["geometry"].bounds[1])
                                / pixel_by_floor[floor_id],
                                3,
                            ),
                            "barrier_reasons": row["barrier_reasons"],
                        }
                        for row, _ in collisions[:20]
                    ],
                }
            )
            rejected_features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "edge_id": edge_id,
                        "floor_id": floor_id,
                        "edge_kind": kind,
                        "collision_count": len(collisions),
                    },
                    "geometry": mapping(unary_union([collision for _, collision in collisions])),
                }
            )
            continue
        multiplier = _routing_multiplier(str(edge.get("kind") or ""))
        edge["routing_cost_multiplier"] = multiplier
        edge["routing_cost"] = float(edge.get("length") or line.length) * multiplier
        edge["corridor_backbone_preference"] = "preferred" if multiplier <= 1.05 else "penalized_access_or_refinement"
        edge["raw_cad_collision_certified"] = True
        accepted.append(edge)

    post_door_nodes, post_door_edges, post_door_by_floor = _add_post_collision_door_portals(
        list(graph.get("nodes", []) or []),
        accepted,
        passages_by_floor,
        trees,
        strokes,
        domains,
        pixel_by_floor,
        config,
    )
    accepted.extend(post_door_edges)
    hard_rejected = []
    if hard_index:
        # Check ALL outputs, including generated detours, target approaches and
        # post-collision door edges. No portal/endpoint exemption is permitted.
        accepted, hard_rejected = hard_walls.filter_graph_edges(accepted, hard_index)
        for row in hard_rejected:
            rejected.append({key: value for key, value in row.items() if key != "geometry"})
            rejected_by_kind[str(row.get("kind") or "")] += 1
            rejected_features.append({"type": "Feature", "properties": {
                key: value for key, value in row.items() if key != "geometry"
            }, "geometry": row.get("geometry")})
    safe_graph_payload = {
        **graph,
        "graph_type": "independent_raw_cad_collision_certified_physical_navigation_graph",
        "nodes": [*(graph.get("nodes", []) or []), *post_door_nodes],
        "edges": accepted,
    }
    if hard_index:
        safe_graph_payload["hard_obstacle_certification"] = {
            **hard_index.manifest, "run_dir": str(run), "rejected_edge_count": len(hard_rejected),
        }
    safe_graph_path = _write_json(output / "safe_refined_navigation_graph.json", safe_graph_payload)
    rejected_path = _write_json(output / "rejected_physical_edges.geojson", _feature_collection(rejected_features))
    audit = {
        "schema_version": 1,
        "stage": "independent_raw_cad_geometry_collision_certification",
        "policy": {
            "uses_stage_05_obstacle_decisions": False,
            "barrier_evidence": [
                "closed_geometry_boundary",
                "parallel_boundary_pair",
                "continuous_endpoint_topology",
            ],
            "cross_boundary_only_at_confirmed_door_portal": True,
            "geometric_gap_without_door_evidence_allowed": False,
            "corridor_policy": "prefer_skeleton_and_confirmed_portal_backbone; penalize target access and local refinement",
            "raw_collision_recovery": "locally replan around finite CAD barriers; delete edge only when no collision-free detour exists",
            "candidate_strength_policy": "single geometry evidence is audit-only; hard cuts require measurable thickness or combined parallel-and-continuous topology",
        },
        "sources": {
            "graph": str(graph_source),
            "geometry_inventory": str(_inventory_path(run)),
            "free_areas": str(free_areas_path.resolve()),
            "connector_portals": str(Path(connector_portals_path).resolve()) if connector_portals_path else "",
        },
        "outputs": {
            "safe_graph": str(safe_graph_path),
            "barriers_geojson": str(barriers_path),
            "door_evidence_geojson": str(doors_path),
            "rejected_edges_geojson": str(rejected_path),
        },
        "counts": {
            **extraction_counts,
            "barrier_line_count": sum(len(rows) for rows in barriers_by_floor.values()),
            "hard_barrier_line_count": sum(len(rows) for rows in strokes.values()),
            "input_edge_count": len(graph.get("edges", []) or []),
            "accepted_edge_count": len(accepted),
            "rejected_edge_count": len(rejected),
            "hard_obstacle_rejected_edge_count": len(hard_rejected),
            "confirmed_connector_portal_count": len(portals),
            "confirmed_door_passage_count": sum(len(rows) for rows in passages_by_floor.values()),
            "door_exempted_edge_count": sum(door_exempted_by_floor.values()),
            "local_detour_edge_count": sum(local_detour_by_floor.values()),
            "post_collision_door_portal_count": sum(post_door_by_floor.values()),
            "post_collision_door_portal_edge_count": len(post_door_edges),
        },
        "door_exempted_edge_count_by_floor": dict(door_exempted_by_floor),
        "local_detour_edge_count_by_floor": dict(local_detour_by_floor),
        "post_collision_door_portal_count_by_floor": dict(post_door_by_floor),
        "rejected_edge_kind_counts": dict(rejected_by_kind),
        "floors": floor_extraction_audit,
        "rejected_edges": rejected,
        "hard_obstacle_certification": safe_graph_payload.get("hard_obstacle_certification"),
    }
    audit_path = _write_json(output / "raw_cad_collision_audit.json", audit)
    safe_graph_payload["raw_cad_collision_certification"] = audit
    _write_json(safe_graph_path, safe_graph_payload)
    return RawCadSafetyResult(
        safe_graph=safe_graph_path,
        barriers_geojson=barriers_path,
        door_evidence_geojson=doors_path,
        rejected_edges_geojson=rejected_path,
        audit_json=audit_path,
        audit=audit,
    )


def audit_forwarding_route(
    forwarding_route_path: Path | str,
    safe_graph_path: Path | str,
    output_path: Path | str,
) -> dict[str, Any]:
    """Check safe-edge membership AND actual geometry against approved walls."""
    forwarding = _read_json(Path(forwarding_route_path).resolve())
    graph = _read_json(Path(safe_graph_path).resolve())
    safe_edge_ids = {str(edge.get("edge_id") or "") for edge in graph.get("edges", []) or []}
    failures: list[dict[str, Any]] = []
    traversal_count = 0
    for floor_id, floor in (forwarding.get("floors") or {}).items():
        for event in floor.get("traversal_events", []) or []:
            traversal_count += 1
            source_edge_id = str(event.get("source_edge_id") or "")
            if source_edge_id not in safe_edge_ids:
                failures.append(
                    {
                        "floor_id": floor_id,
                        "event_id": event.get("event_id"),
                        "source_edge_id": source_edge_id,
                        "reason": "route_traversal_edge_missing_from_raw_cad_safe_graph",
                    }
                )
    geometry_audit = {}
    certification = graph.get("hard_obstacle_certification")
    # If this run opted in, a legacy/unfingerprinted graph must fail closed.
    from multi_drawing_pipeline.stages.fire_route_core import navigation_obstacles as hard_walls
    inferred_run = Path(safe_graph_path).resolve().parent.parent
    current = hard_walls.approved_manifest(inferred_run)
    if current and not certification:
        failures.append({"reason": "safe_graph_missing_approved_wall_certification"})
    if certification:
        current = hard_walls.approved_manifest(Path(certification["run_dir"]))
        if not current or current["sha256"] != certification["sha256"]:
            failures.append({"reason": "safe_graph_uses_stale_obstacles"})
        else:
            geometry_audit = hard_walls.audit_route_geometry(forwarding, graph, hard_walls.HardObstacleIndex(current))
            failures.extend(geometry_audit["geometry_failures"])
    payload = {
        "schema_version": 1,
        "stage": "final_route_independent_raw_cad_safety_audit",
        "forwarding_route": str(Path(forwarding_route_path).resolve()),
        "safe_graph": str(Path(safe_graph_path).resolve()),
        "traversal_count": traversal_count,
        "failure_count": len(failures),
        "accepted": not failures,
        "failures": failures,
        **geometry_audit,
    }
    _write_json(Path(output_path), payload)
    return payload


def audit_beautified_route_against_existing_raw_cad(
    run_dir: Path | str,
    original_route_geojson: Path | str,
    beautified_route_geojson: Path | str,
    barriers_geojson: Path | str,
    output_path: Path | str,
    *,
    config: RawCadSafetyConfig | None = None,
) -> dict[str, Any]:
    """Fail closed if display simplification creates a new raw-CAD crossing.

    The original forwarding route has already been certified edge by edge.  Its
    exact intersections with raw-CAD barrier strokes therefore define the only
    crossing zones the display route may reuse (normally confirmed doors and
    legal endpoints).  A beautified segment is accepted only when every raw-CAD
    intersection stays inside one of those existing certified zones.
    """
    active_config = config or RawCadSafetyConfig()
    run = Path(run_dir).resolve()
    free_areas_path = run / "navigation_graph" / "inputs" / "free_areas.geojson"
    domains = _load_floor_domains(free_areas_path)
    pixel_by_floor = _load_pixel_sizes(run, domains)

    def route_lines(path: Path | str) -> dict[str, list[LineString]]:
        grouped: dict[str, list[LineString]] = defaultdict(list)
        for feature in _read_json(Path(path).resolve()).get("features", []) or []:
            properties = feature.get("properties") or {}
            if properties.get("feature_type") != "route_edge_traversal" or not feature.get("geometry"):
                continue
            floor_id = str(properties.get("floor_id") or "")
            geometry = shape(feature["geometry"])
            if floor_id and not geometry.is_empty:
                if geometry.geom_type == "LineString":
                    grouped[floor_id].append(geometry)
                else:
                    grouped[floor_id].extend(
                        part for part in getattr(geometry, "geoms", [])
                        if part.geom_type == "LineString" and not part.is_empty
                    )
        return dict(grouped)

    barriers: dict[str, Any] = {}
    for feature in _read_json(Path(barriers_geojson).resolve()).get("features", []) or []:
        properties = feature.get("properties") or {}
        floor_id = str(properties.get("floor_id") or "")
        if not floor_id or not feature.get("geometry"):
            continue
        geometry = shape(feature["geometry"])
        barriers[floor_id] = unary_union([barriers.get(floor_id, GeometryCollection()), geometry])

    original_by_floor = route_lines(original_route_geojson)
    display_by_floor = route_lines(beautified_route_geojson)
    failures: list[dict[str, Any]] = []
    segment_count = 0
    allowed_zones: dict[str, Any] = {}
    for floor_id, barrier in barriers.items():
        tolerance = max(
            active_config.endpoint_touch_tolerance_pixels * pixel_by_floor.get(floor_id, 1.0),
            1e-6,
        )
        original = unary_union(original_by_floor.get(floor_id, []))
        certified_crossings = (
            barrier.intersection(original)
            if not original.is_empty
            else GeometryCollection()
        )
        allowed_zones[floor_id] = certified_crossings.buffer(tolerance)

    for floor_id, lines in display_by_floor.items():
        barrier = barriers.get(floor_id)
        if barrier is None or barrier.is_empty:
            segment_count += len(lines)
            continue
        allowed = allowed_zones.get(floor_id, GeometryCollection())
        for index, line in enumerate(lines, start=1):
            segment_count += 1
            collision = line.intersection(barrier)
            if not collision.is_empty and not allowed.is_empty:
                collision = collision.difference(allowed)
            if collision.is_empty:
                continue
            failures.append({
                "floor_id": floor_id,
                "display_segment_index": index,
                "reason": "beautification_created_new_raw_cad_barrier_crossing",
                "collision_geometry": mapping(collision),
            })

    payload = {
        "schema_version": 1,
        "stage": "beautified_route_existing_raw_cad_recertification",
        "original_route_geojson": str(Path(original_route_geojson).resolve()),
        "beautified_route_geojson": str(Path(beautified_route_geojson).resolve()),
        "barriers_geojson": str(Path(barriers_geojson).resolve()),
        "display_segment_count": segment_count,
        "failure_count": len(failures),
        "accepted": not failures,
        "failures": failures,
    }
    _write_json(Path(output_path), payload)
    return payload


__all__ = [
    "RawCadSafetyConfig",
    "RawCadSafetyResult",
    "audit_beautified_route_against_existing_raw_cad",
    "audit_forwarding_route",
    "build_and_certify_physical_graph",
]
