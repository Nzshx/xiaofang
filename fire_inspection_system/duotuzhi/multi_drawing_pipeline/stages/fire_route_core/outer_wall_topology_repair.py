"""Door-aware outer-wall topology repair for Stage 05 obstacle results.

The repair deliberately separates three kinds of evidence:

* a cached vision result supplies building count and label hints;
* local vector validation establishes the building domains without forcing
  incorrect historical candidate ownership onto the vector components;
* the current Stage 05 red obstacle render snaps those domains to current walls;
* current raw-CAD door-swing evidence protects genuine exterior openings.

No vision API is called by this module.  Historical vision data is accepted only
when the source DXF hash and every floor render CAD bounding box match.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw
from shapely.geometry import LineString, box, mapping, shape
from shapely.ops import unary_union

from .stage_05A_obstacles import (
    BuildingVectorValidationConfig,
    MANIFEST_JSON,
    _draw_label,
    _iter_polygon_pixels,
    _load_annotation_font,
    _point_to_pixel,
    _pixel_polygon_to_cad,
    _polygon_from_ring,
    _polygonal_parts,
    _read_json,
    _red_obstacle_mask_from_image,
    _refine_detection_with_vector_mask,
    _snapped_outer_wall_completion,
    _write_json,
    annotate_building_regions,
)


OUTPUT_SUBDIR = Path("obstacles") / "outer_wall_topology_repair"
REPAIRED_OBSTACLES = "door_aware_outer_wall_repairs.geojson"
PROTECTED_PORTALS = "protected_exterior_door_portals.geojson"
REPAIR_AUDIT = "outer_wall_topology_repair_audit.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _input_dxf(run_dir: Path) -> Path:
    summary_path = run_dir / "pipeline_summary.json"
    summary = _read_json(summary_path)
    value = summary.get("input_dxf")
    if not value:
        raise ValueError(f"Missing input_dxf in {summary_path}")
    path = Path(str(value)).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def validate_historical_vision_reuse(
    target_run_dir: Path | str,
    vision_source_run_dir: Path | str,
    *,
    tolerance: float = 1e-6,
) -> dict[str, Any]:
    """Fail closed unless historical vision coordinates exactly match target."""
    target = Path(target_run_dir).resolve()
    source = Path(vision_source_run_dir).resolve()
    target_dxf = _input_dxf(target)
    source_dxf = _input_dxf(source)
    target_hash = _sha256(target_dxf)
    source_hash = _sha256(source_dxf)
    if target_hash != source_hash:
        raise ValueError("Historical vision source DXF hash does not match target DXF")

    target_manifest_path = target / "obstacle_building_region_render" / MANIFEST_JSON
    source_manifest_path = source / "obstacle_building_region_render" / MANIFEST_JSON
    target_manifest = _read_json(target_manifest_path)
    source_manifest = _read_json(source_manifest_path)

    def records(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            str(item.get("floor_id") or ""): item
            for item in payload.get("images", []) or []
            if isinstance(item, dict) and item.get("floor_id")
        }

    target_records = records(target_manifest)
    source_records = records(source_manifest)
    if not target_records or set(target_records) != set(source_records):
        raise ValueError("Historical and target render manifests have different floor sets")
    floor_rows: list[dict[str, Any]] = []
    for floor_id in sorted(target_records):
        target_bbox = [float(value) for value in target_records[floor_id].get("cad_bbox") or []]
        source_bbox = [float(value) for value in source_records[floor_id].get("cad_bbox") or []]
        if len(target_bbox) != 4 or len(source_bbox) != 4:
            raise ValueError(f"Missing CAD bbox for floor {floor_id}")
        if any(abs(left - right) > tolerance for left, right in zip(target_bbox, source_bbox)):
            raise ValueError(f"Historical CAD bbox does not match target for floor {floor_id}")
        current_record, old_record = target_records[floor_id], source_records[floor_id]
        if any(current_record.get(key) != old_record.get(key) for key in ("image_width", "image_height")):
            raise ValueError(f"Historical image dimensions do not match target for floor {floor_id}")
        current_matrix = (current_record.get("transform") or {}).get("cad_to_pixel_matrix")
        old_matrix = (old_record.get("transform") or {}).get("cad_to_pixel_matrix")
        if not current_matrix or not old_matrix or len(current_matrix) != len(old_matrix):
            raise ValueError(f"Missing historical render transform for floor {floor_id}")
        if any(len(left) != len(right) or any(abs(float(a) - float(b)) > tolerance for a, b in zip(left, right))
               for left, right in zip(current_matrix, old_matrix)):
            raise ValueError(f"Historical render transform does not match target for floor {floor_id}")
        floor_rows.append({
            "floor_id": floor_id,
            "cad_bbox": target_bbox,
            "coordinates_match": True,
        })

    sheets_path = (
        source
        / "obstacle_building_region_render"
        / "vision_building_regions"
        / "drawing_sheets_floors_with_building_regions.json"
    )
    sheets = _read_json(sheets_path)
    successful_floor_count = 0
    building_region_count = 0
    for sheet in sheets.get("sheets", []) or []:
        for region in sheet.get("inspection_regions", []) or []:
            detection = region.get("building_region_detection") or {}
            if str(detection.get("status") or "").lower() == "ok":
                successful_floor_count += 1
            building_region_count += len(region.get("building_regions") or [])
    if successful_floor_count < len(target_records) or building_region_count <= 0:
        raise ValueError("Historical vision result is not complete for all target floors")
    return {
        "target_run_dir": str(target),
        "vision_source_run_dir": str(source),
        "target_input_dxf": str(target_dxf),
        "source_input_dxf": str(source_dxf),
        "input_dxf_sha256": target_hash,
        "pixel_transforms_match": True,
        "target_manifest": str(target_manifest_path),
        "source_manifest": str(source_manifest_path),
        "source_sheets_with_buildings": str(sheets_path),
        "floor_count": len(target_records),
        "successful_vision_floor_count": successful_floor_count,
        "building_region_count": building_region_count,
        "floors": floor_rows,
    }


def _door_evidence_paths(run_dir: Path) -> list[Path]:
    return [
        run_dir / "raw_cad_safety" / "dxf_door_swing_evidence.geojson",
        run_dir / "raw_cad_safety" / "raw_cad_door_evidence.geojson",
    ]


def _door_thresholds(geometry: Any, properties: dict[str, Any]) -> list[Any]:
    """Return the two radial leaf positions, not the filled swing sector."""
    if geometry.geom_type != "LineString":
        return []
    points = list(geometry.coords)
    if len(points) < 3:
        return []
    if properties.get("center_x") is not None and properties.get("center_y") is not None:
        center = (float(properties["center_x"]), float(properties["center_y"]))
    else:
        # Circumcenter in local coordinates avoids cancellation at large CAD y.
        ax, ay = points[0][:2]
        bx, by = points[len(points) // 2][0] - ax, points[len(points) // 2][1] - ay
        cx, cy = points[-1][0] - ax, points[-1][1] - ay
        determinant = 2.0 * (bx * cy - by * cx)
        if abs(determinant) < 1e-6:
            return []
        b2, c2 = bx * bx + by * by, cx * cx + cy * cy
        center = (ax + (cy * b2 - by * c2) / determinant,
                  ay + (bx * c2 - cx * b2) / determinant)
    radii = [math.hypot(point[0] - center[0], point[1] - center[1]) for point in points]
    radius = sum(radii) / len(radii)
    if radius <= 0 or max(abs(value - radius) for value in radii) > radius * 0.08:
        return []
    return [LineString([center, points[0][:2]]), LineString([center, points[-1][:2]])]


def load_door_portal_candidates(run_dir: Path | str) -> dict[str, list[dict[str, Any]]]:
    """Load radial door thresholds from current raw-CAD swing evidence."""
    run = Path(run_dir).resolve()
    if not any(path.is_file() for path in _door_evidence_paths(run)):
        raise FileNotFoundError("Current raw-CAD door evidence is required before closing outer walls")
    grouped: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, int, int, int, int]] = set()
    for path in _door_evidence_paths(run):
        if not path.is_file():
            continue
        payload = _read_json(path)
        for feature in payload.get("features", []) or []:
            if not isinstance(feature, dict) or not feature.get("geometry"):
                continue
            properties = feature.get("properties") or {}
            floor_id = str(
                properties.get("source_floor_id") or properties.get("floor_id") or ""
            ).strip()
            if not floor_id:
                continue
            geometry = shape(feature["geometry"])
            if geometry.is_empty:
                continue
            minx, miny, maxx, maxy = geometry.bounds
            key = (
                floor_id,
                round(minx / 50),
                round(miny / 50),
                round(maxx / 50),
                round(maxy / 50),
            )
            if key in seen:
                continue
            seen.add(key)
            thresholds = _door_thresholds(geometry, properties)
            if not thresholds:
                continue
            grouped.setdefault(floor_id, []).append({
                "geometry": geometry,
                "thresholds": thresholds,
                "source_path": str(path),
                "properties": properties,
            })
    return grouped


def subtract_exterior_door_portals(
    envelope: Any,
    candidates: Iterable[dict[str, Any]],
    *,
    wall_width: float = 200.0,
) -> tuple[Any, list[dict[str, Any]]]:
    """Require a radial doorway to follow the facade, not merely be nearby.

    A swing sector or perpendicular leaf can touch an exterior wall without
    being an exterior doorway.  At least 75% of the radial threshold must lie
    in the narrow wall band, and both jambs must be close to that band.
    """
    cuts: list[dict[str, Any]] = []
    tolerance = max(30.0, wall_width * 0.5)
    near_wall = envelope.buffer(tolerance)
    for candidate in candidates:
        choices: list[tuple[float, Any]] = []
        for threshold in candidate.get("thresholds") or []:
            if threshold.length <= 0:
                continue
            fraction = threshold.intersection(near_wall).length / threshold.length
            if fraction < 0.75 or not near_wall.covers(threshold.boundary):
                continue
            choices.append((fraction, threshold))
        if not choices:
            continue
        fraction, threshold = max(choices, key=lambda pair: pair[0])
        clearance = wall_width + tolerance
        # Flat caps keep the opening between the two jambs, without a door-radius
        # sized extra hole. This cut never erases original CAD wall data.
        zone = threshold.buffer(clearance, cap_style=2, join_style=2)
        cut = envelope.intersection(zone)
        if cut.is_empty or cut.area <= 0:
            continue
        cuts.append({**candidate, "zone": zone, "cut": cut, "clearance": clearance,
                     "threshold": threshold, "wall_alignment_fraction": fraction})
    if not cuts:
        return envelope, []
    protected = unary_union([item["zone"] for item in cuts])
    repaired = envelope.difference(protected)
    if not repaired.is_valid:
        repaired = repaired.buffer(0)
    return repaired, cuts


def _building_domains(payload: dict[str, Any]) -> Iterable[tuple[str, str, dict[str, Any], Any]]:
    seen: set[str] = set()
    for sheet in payload.get("sheets", []) or []:
        if not isinstance(sheet, dict) or not sheet.get("path_planning_usable", True):
            continue
        floor_id = str(sheet.get("floor_id") or "").strip()
        for region in sheet.get("inspection_regions", []) or []:
            detection = region.get("building_region_detection") or {}
            if str(detection.get("status") or "").lower() == "error":
                continue
            for building in region.get("building_regions", []) or []:
                scope_id = str(building.get("building_scope_id") or "").strip()
                if not scope_id or scope_id in seen:
                    continue
                raw_parts = building.get("structural_parts") or building.get("parts") or []
                polygons = [
                    polygon
                    for ring in raw_parts
                    if (polygon := _polygon_from_ring(ring)) is not None
                ]
                if not polygons:
                    polygon = _polygon_from_ring(building.get("polygon"))
                    polygons = [polygon] if polygon is not None else []
                if not polygons:
                    continue
                seen.add(scope_id)
                yield floor_id, scope_id, building, unary_union(polygons)


def vector_validated_building_domains(
    target_run_dir: Path | str,
    vision_source_run_dir: Path | str,
    records: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Path]]:
    """Re-run local vector validation from cached vision counts and labels.

    The live model is not called.  Bounded bridge distances are used only to
    connect red components assigned to the same vector-density group. A floor
    that still cannot be
    vector-validated is excluded from wall completion.
    """
    target = Path(target_run_dir).resolve()
    source = Path(vision_source_run_dir).resolve()
    domains: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    review_paths: list[Path] = []
    review_dir = target / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    for floor_id, record in sorted(records.items()):
        image_id = str(record.get("image_id") or "")
        response_path = (
            source
            / "obstacle_building_region_render"
            / "vision_building_regions"
            / "responses"
            / image_id
            / "model_response.json"
        )
        if not response_path.is_file():
            audits.append({
                "floor_id": floor_id,
                "status": "skipped_missing_cached_vision_response",
                "response_path": str(response_path),
            })
            continue
        response = _read_json(response_path)
        candidate = response.get("vision_candidate_detection")
        if not isinstance(candidate, dict):
            audits.append({
                "floor_id": floor_id,
                "status": "skipped_missing_normalized_vision_candidate",
            })
            continue
        image_path = Path(str(record.get("image_path") or ""))
        refined: dict[str, Any] | None = None
        applied_gap = 0
        attempts: list[dict[str, Any]] = []
        for gap in (180, 250):
            config = dataclasses.replace(
                BuildingVectorValidationConfig(),
                max_bridge_gap_pixels=gap,
                merge_gap_pixels=70 if gap == 180 else 250,
                candidate_regions_constrain_vector_groups=False,
            )
            result = _refine_detection_with_vector_mask(
                record,
                candidate,
                image_path,
                floor_geometry=None,
                config=config,
            )
            attempts.append({
                "max_bridge_gap_pixels": gap,
                "candidate_regions_constrain_vector_groups": False,
                "validation_status": result.get("validation_status"),
                "review_reasons": result.get("review_reasons") or [],
            })
            if result.get("validation_status") == "vector_validated":
                refined = result
                applied_gap = gap
                break
        historical_fallback = False
        if refined is None:
            historical = response.get("normalized_detection")
            if (
                isinstance(historical, dict)
                and historical.get("validation_status") == "vector_validated"
            ):
                refined = historical
                historical_fallback = True
        if refined is None:
            audits.append({
                "floor_id": floor_id,
                "status": "skipped_not_vector_validated",
                "reason": "coarse vision rectangles are forbidden as wall-repair boundaries",
                "attempts": attempts,
            })
            continue

        output_path = review_dir / f"{floor_id}_building_regions_vector_validated_reused.png"
        annotate_building_regions(record, refined, output_path)
        review_paths.append(output_path.resolve())
        building_count = 0
        for building in refined.get("buildings") or []:
            if not isinstance(building, dict):
                continue
            scope_id = f"{floor_id}__{str(building.get('building_id') or '')}"
            raw_parts = building.get("structural_parts_cad") or building.get("parts_cad") or []
            polygons = [
                polygon
                for ring in raw_parts
                if (polygon := _polygon_from_ring(ring)) is not None
            ]
            if not polygons:
                polygon = _polygon_from_ring(building.get("polygon_cad"))
                polygons = [polygon] if polygon is not None else []
            if not polygons:
                continue
            domain = unary_union(polygons)
            if not domain.is_valid:
                domain = domain.buffer(0)
            if domain.is_empty:
                continue
            domains.append({
                "floor_id": floor_id,
                "scope_id": scope_id,
                "building": building,
                "domain": domain,
                "vector_validation_max_bridge_gap_pixels": applied_gap,
                "historical_vector_geometry_fallback": historical_fallback,
            })
            building_count += 1
        audits.append({
            "floor_id": floor_id,
            "status": "vector_validated",
            "building_count": building_count,
            "max_bridge_gap_pixels": applied_gap,
            "historical_vector_geometry_fallback": historical_fallback,
            "review_image": str(output_path.resolve()),
            "attempts": attempts,
        })
    return domains, audits, review_paths


def _feature_collection(features: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": features}


def protect_enclosed_connectors(
    record: dict[str, Any], mask: Any, features: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Do not label a two-wall enclosed inter-building corridor as exterior.

    Empty space alone is not evidence. Both parallel side walls must span at
    least 85% of the gap, the middle must be clear, and both corridor ends must
    align with the proposed facade bands. Only new repair geometry is cut.
    """
    import cv2
    import numpy as np

    geometries = [shape(feature["geometry"]) for feature in features]
    bbox = tuple(float(value) for value in record["cad_bbox"])
    transform = record["transform"]
    scale = float(transform["cad_units_per_pixel"])
    pixel_boxes = []
    for geometry in geometries:
        x1, y1, x2, y2 = geometry.bounds
        left, top = _point_to_pixel((x1, y2), bbox, transform)
        right, bottom = _point_to_pixel((x2, y1), bbox, transform)
        pixel_boxes.append([left, top, right, bottom])
    expanded = cv2.dilate(mask, np.ones((3, 3), dtype=np.uint8)) > 0
    connectors = []
    cut_zones: dict[int, list[Any]] = {}
    for vertical in (False, True):
        scan_mask = expanded.T if vertical else expanded
        oriented = [[b[1], b[0], b[3], b[2]] if vertical else b for b in pixel_boxes]
        for first in range(len(features)):
            for second in range(first + 1, len(features)):
                left, right = sorted((first, second), key=lambda index: oriented[index][0])
                a, b = oriented[left], oriented[right]
                x1, x2 = a[2], b[0]
                gap = x2 - x1
                if not 8 < gap <= 650:
                    continue
                y1 = max(0, int(math.ceil(max(a[1], b[1]))))
                y2 = min(scan_mask.shape[0], int(math.floor(min(a[3], b[3]))))
                lo = max(0, int(math.ceil(x1)) + 3)
                hi = min(scan_mask.shape[1], int(math.floor(x2)) - 3)
                if hi <= lo or y2 - y1 < 30:
                    continue
                support = scan_mask[y1:y2, lo:hi].mean(axis=1)
                hit_rows = np.flatnonzero(support >= 0.85) + y1
                if len(hit_rows) < 2:
                    continue
                clusters = np.split(hit_rows, np.flatnonzero(np.diff(hit_rows) > 1) + 1)
                for upper, lower in zip(clusters, clusters[1:]):
                    top, bottom = int(upper[-1]) + 2, int(lower[0]) - 2
                    if not 24 <= bottom - top <= 220:
                        continue
                    if float(scan_mask[top:bottom, lo:hi].mean()) > 0.03:
                        continue
                    guard = max(8.0, max(float(features[index]["properties"].get("wall_width") or 200)
                                          for index in (left, right)) / scale * 2)

                    def cad_point(x: float, y: float) -> list[float]:
                        pixel = [y, x] if vertical else [x, y]
                        return _pixel_polygon_to_cad(record, [pixel])[0]

                    end_lines = [LineString([cad_point(x, top), cad_point(x, bottom)]) for x in (x1, x2)]
                    if any(line.intersection(geometries[index].buffer(scale * 3)).length < line.length * 0.75
                           for index, line in zip((left, right), end_lines)):
                        continue
                    ring = [cad_point(x1 - guard, top), cad_point(x2 + guard, top),
                            cad_point(x2 + guard, bottom), cad_point(x1 - guard, bottom)]
                    zone = _polygon_from_ring(ring)
                    if zone is None:
                        continue
                    cut = unary_union([geometries[left], geometries[right]]).intersection(zone)
                    if cut.is_empty or cut.area <= 0:
                        continue
                    for index in (left, right):
                        cut_zones.setdefault(index, []).append(zone)
                    connectors.append({
                        "type": "Feature", "geometry": mapping(cut),
                        "properties": {
                            "floor_id": str(record.get("floor_id") or ""),
                            "kind": "protected_enclosed_connector",
                            "portal_id": f"{record.get('floor_id')}__CONNECTOR_{len(connectors) + 1:03d}",
                            "building_scope_ids": [features[index]["properties"]["building_scope_id"] for index in (left, right)],
                            "evidence_source": "two_parallel_current_obstacle_walls_and_clear_interior",
                            "connector_region": mapping(zone),
                            "minimum_side_wall_support": 0.85,
                            "interior_red_fraction": float(scan_mask[top:bottom, lo:hi].mean()),
                        },
                    })
    updated = []
    for index, feature in enumerate(features):
        geometry = geometries[index]
        if index in cut_zones:
            geometry = geometry.difference(unary_union(cut_zones[index]))
        updated.append({**feature, "geometry": mapping(geometry)})
    return updated, connectors


def repair_outer_wall_topology(
    target_run_dir: Path | str,
    vision_source_run_dir: Path | str,
) -> dict[str, Any]:
    """Create current, door-aware topology repairs for every detected floor."""
    target = Path(target_run_dir).resolve()
    reuse = validate_historical_vision_reuse(target, vision_source_run_dir)
    manifest_path = target / "obstacle_building_region_render" / MANIFEST_JSON
    manifest = _read_json(manifest_path)
    records = {
        str(item.get("floor_id") or ""): item
        for item in manifest.get("images", []) or []
        if isinstance(item, dict) and item.get("floor_id")
    }
    door_candidates = load_door_portal_candidates(target)
    red_masks: dict[str, Any] = {}
    repair_features: list[dict[str, Any]] = []
    portal_features: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    domains, vector_validation_audits, vector_review_paths = vector_validated_building_domains(
        target,
        vision_source_run_dir,
        records,
    )

    for domain_record in domains:
        floor_id = domain_record["floor_id"]
        scope_id = domain_record["scope_id"]
        building = domain_record["building"]
        historical_domain = domain_record["domain"]
        record = records.get(floor_id)
        if record is None:
            audits.append({
                "floor_id": floor_id,
                "building_scope_id": scope_id,
                "status": "skipped_missing_current_render",
            })
            continue
        cad_bbox = [float(value) for value in record.get("cad_bbox") or []]
        axis_scope = box(*cad_bbox)
        structural_domain = historical_domain.intersection(axis_scope)
        if not structural_domain.is_valid:
            structural_domain = structural_domain.buffer(0)
        if structural_domain.is_empty:
            continue
        image_path = Path(str(record.get("image_path") or ""))
        if floor_id not in red_masks:
            red_masks[floor_id] = _red_obstacle_mask_from_image(image_path, record)
        envelope, snap_audit = _snapped_outer_wall_completion(
            record,
            red_masks[floor_id],
            structural_domain,
            inward_search_ratio=0.40,
        )
        if envelope.is_empty:
            minx, miny, maxx, maxy = structural_domain.bounds
            guard = min(maxx - minx, maxy - miny) * 0.006
            envelope = structural_domain.boundary.buffer(guard, cap_style=2, join_style=2)
            snap_audit = {**snap_audit, "fallback": "historical_visual_boundary"}
        repaired, cuts = subtract_exterior_door_portals(
            envelope,
            door_candidates.get(floor_id, []),
            wall_width=float(snap_audit.get("wall_width_cad_units") or 200.0),
        )
        if repaired.is_empty or repaired.area <= 0:
            audits.append({
                "floor_id": floor_id,
                "building_scope_id": scope_id,
                "status": "skipped_empty_after_portal_subtraction",
            })
            continue
        properties = {
            "floor_id": scope_id,
            "source_floor_id": floor_id,
            "building_scope_id": scope_id,
            "building_id": str(building.get("building_id") or ""),
            "kind": "door_aware_outer_wall_topology_repair",
            "source": "historical_vision_domain_current_red_wall_snap_current_raw_cad_doors",
            "axis_grid_scope_clipped": True,
            "building_domain_validation_status": "vector_validated",
            "vector_validation_max_bridge_gap_pixels": int(
                domain_record["vector_validation_max_bridge_gap_pixels"]
            ),
            "historical_vector_geometry_fallback": bool(
                domain_record["historical_vector_geometry_fallback"]
            ),
            "wall_width": float(snap_audit.get("wall_width_cad_units") or 0.0),
            "protected_exterior_door_count": len(cuts),
        }
        repair_features.append({
            "type": "Feature",
            "properties": properties,
            "geometry": mapping(repaired),
        })
        for index, cut in enumerate(cuts, start=1):
            candidate_properties = cut.get("properties") or {}
            portal_features.append({
                "type": "Feature",
                "properties": {
                    "floor_id": floor_id,
                    "building_scope_id": scope_id,
                    "portal_id": f"{scope_id}__P{index:03d}",
                    "kind": "protected_exterior_door_portal",
                    "evidence_source": str(candidate_properties.get("evidence_source") or ""),
                    "source_handle": str(candidate_properties.get("source_handle") or ""),
                    "clearance": float(cut["clearance"]),
                    "wall_alignment_fraction": float(cut["wall_alignment_fraction"]),
                    "threshold": mapping(cut["threshold"]),
                },
                "geometry": mapping(cut["cut"]),
            })
        audits.append({
            **properties,
            "status": "materialized",
            "historical_domain_area": float(historical_domain.area),
            "axis_clipped_domain_area": float(structural_domain.area),
            "proposed_closed_wall_area": float(envelope.area),
            "final_repair_area": float(repaired.area),
            "protected_door_cut_area": float(envelope.area - repaired.area),
            "outer_wall_snap_audit": snap_audit,
        })

    # Enclosed connectors are interior extensions, not holes in the exterior.
    connector_features = []
    updated_repairs = []
    for floor_id, record in records.items():
        floor_features = [feature for feature in repair_features
                          if feature["properties"]["source_floor_id"] == floor_id]
        if not floor_features:
            continue
        revised, connectors = protect_enclosed_connectors(record, red_masks[floor_id], floor_features)
        updated_repairs.extend(revised)
        connector_features.extend(connectors)
    repair_features = updated_repairs
    portal_features.extend(connector_features)
    final_geometries = {feature["properties"]["building_scope_id"]: shape(feature["geometry"])
                        for feature in repair_features}
    for audit in audits:
        geometry = final_geometries.get(audit.get("building_scope_id"))
        if geometry is not None and "final_repair_area" in audit:
            audit["protected_connector_cut_area"] = max(0.0, audit["final_repair_area"] - geometry.area)
            audit["final_repair_area"] = float(geometry.area)
    output_dir = target / OUTPUT_SUBDIR
    output_dir.mkdir(parents=True, exist_ok=True)
    repair_path = output_dir / REPAIRED_OBSTACLES
    portal_path = output_dir / PROTECTED_PORTALS
    audit_path = output_dir / REPAIR_AUDIT
    _write_json(repair_path, _feature_collection(repair_features))
    _write_json(portal_path, _feature_collection(portal_features))
    review = annotate_topology_repairs(
        manifest_path,
        repair_path,
        portal_path,
        target / "review",
        vector_validation_audits=vector_validation_audits,
    )
    _write_json(audit_path, {
        "schema_version": 1,
        "policy": "repair_vector_outer_boundary_preserving_raw_cad_door_thresholds_and_enclosed_connectors",
        "review_required": True,
        "navigation_replanned": False,
        "historical_vision_reuse_validation": reuse,
        "live_vision_api_called": False,
        "axis_grid_scope_constraint": True,
        "door_layer_names_required": False,
        "coarse_vision_candidate_boundaries_used": False,
        "vector_validation": vector_validation_audits,
        "vector_validation_review_images": [str(path) for path in vector_review_paths],
        "repair_feature_count": len(repair_features),
        "protected_exterior_door_portal_count": len(portal_features) - len(connector_features),
        "protected_enclosed_connector_count": len(connector_features),
        "floors_with_repairs": sorted({
            str((feature.get("properties") or {}).get("source_floor_id") or "")
            for feature in repair_features
        }),
        "features": audits,
        "output_geojson": str(repair_path),
        "protected_portals_geojson": str(portal_path),
        "review_overview": str(review.get("overview_path") or ""),
        "review_images": [str(path) for path in review.get("image_paths", [])],
    })
    return {
        "repair_geojson_path": repair_path,
        "portal_geojson_path": portal_path,
        "audit_path": audit_path,
        "repair_count": len(repair_features),
        "portal_count": len(portal_features) - len(connector_features),
        "connector_count": len(connector_features),
        "review_image_paths": review.get("image_paths", []),
        "overview_path": review.get("overview_path"),
        "review_index_path": review.get("index_path"),
    }


def annotate_topology_repairs(
    manifest_json: Path | str,
    repair_geojson: Path | str,
    portal_geojson: Path | str,
    review_dir: Path | str,
    *,
    vector_validation_audits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    manifest = _read_json(Path(manifest_json))
    repairs = _read_json(Path(repair_geojson)).get("features", []) or []
    portals = _read_json(Path(portal_geojson)).get("features", []) or []
    output = Path(review_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    records = {
        str(item.get("floor_id") or ""): item
        for item in manifest.get("images", []) or []
        if isinstance(item, dict) and item.get("floor_id")
    }
    repair_by_floor: dict[str, list[dict[str, Any]]] = {}
    portal_by_floor: dict[str, list[dict[str, Any]]] = {}
    validation_by_floor = {
        str(item.get("floor_id") or ""): item
        for item in (vector_validation_audits or [])
        if isinstance(item, dict) and item.get("floor_id")
    }
    for feature in repairs:
        floor_id = str((feature.get("properties") or {}).get("source_floor_id") or "")
        repair_by_floor.setdefault(floor_id, []).append(feature)
    for feature in portals:
        floor_id = str((feature.get("properties") or {}).get("floor_id") or "")
        portal_by_floor.setdefault(floor_id, []).append(feature)

    image_paths: list[Path] = []
    rows: list[dict[str, Any]] = []
    for floor_id in sorted(records):
        record = records[floor_id]
        source_path = Path(str(record.get("image_path") or ""))
        with Image.open(source_path) as source:
            image = source.convert("RGBA")
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        cad_bbox = tuple(float(value) for value in record["cad_bbox"])
        transform = record["transform"]
        line_width = max(5, min(image.size) // 650)
        font = _load_annotation_font(max(18, min(image.size) // 105))
        banner_font = _load_annotation_font(max(20, min(image.size) // 92))
        floor_repairs = repair_by_floor.get(floor_id, [])
        for feature in floor_repairs:
            geometry = shape(feature["geometry"])
            for exterior, holes in _iter_polygon_pixels(geometry, cad_bbox, transform):
                draw.polygon(exterior, fill=(0, 190, 255, 105))
                draw.line(exterior + [exterior[0]], fill=(0, 75, 255, 255), width=line_width)
                for hole in holes:
                    if len(hole) >= 3:
                        draw.polygon(hole, fill=(0, 0, 0, 0))
                        draw.line(hole + [hole[0]], fill=(0, 75, 255, 255), width=line_width)
            point = geometry.representative_point()
            label_point = _point_to_pixel((point.x, point.y), cad_bbox, transform)
            props = feature.get("properties") or {}
            label = f"WALL REPAIR {props.get('building_scope_id', '')}"
            label_width = draw.textbbox((0, 0), label, font=font)[2]
            _draw_label(
                draw,
                (max(6, min(label_point[0], image.width - label_width - 16)), max(60, label_point[1])),
                label,
                font,
                (0, 75, 255, 255),
            )
        for feature in portal_by_floor.get(floor_id, []):
            geometry = shape(feature["geometry"])
            connector = (feature.get("properties") or {}).get("kind") == "protected_enclosed_connector"
            fill = (255, 195, 0, 210) if connector else (20, 255, 80, 210)
            outline = (200, 110, 0, 255) if connector else (0, 125, 35, 255)
            for exterior, holes in _iter_polygon_pixels(geometry, cad_bbox, transform):
                draw.polygon(exterior, fill=fill)
                draw.line(exterior + [exterior[0]], fill=outline, width=line_width + 2)
        validation = validation_by_floor.get(floor_id) or {}
        validation_status = str(validation.get("status") or "unknown")
        door_count = sum(feature["properties"].get("kind") == "protected_exterior_door_portal"
                         for feature in portal_by_floor.get(floor_id, []))
        connector_count = len(portal_by_floor.get(floor_id, [])) - door_count
        banner = (
            f"{floor_id} | RED existing | BLUE repair | GREEN door evidence | AMBER enclosed connector | "
            f"{validation_status} | buildings={len(floor_repairs)} door-arcs={door_count} connectors={connector_count}"
        )
        bbox = draw.textbbox((12, 12), banner, font=banner_font)
        draw.rectangle((0, 0, image.width, bbox[3] + 24), fill=(0, 0, 0, 200))
        draw.text((12, 12), banner, fill=(255, 255, 255, 255), font=banner_font)
        output_path = output / f"{floor_id}_obstacles_topology_repaired.png"
        Image.alpha_composite(image, overlay).convert("RGB").save(
            output_path,
            format="PNG",
            optimize=True,
        )
        image_paths.append(output_path.resolve())
        rows.append({
            "floor_id": floor_id,
            "vector_validation_status": validation_status,
            "repair_count": len(floor_repairs),
            "protected_door_count": door_count,
            "protected_connector_count": connector_count,
            "image_path": str(output_path.resolve()),
        })

    overview_path: Path | None = None
    if image_paths:
        thumbnails: list[Image.Image] = []
        target_width = 1100
        for path in image_paths:
            with Image.open(path) as source:
                thumb = source.convert("RGB")
            scale = min(1.0, target_width / max(1, thumb.width))
            thumbnails.append(thumb.resize(
                (max(1, int(thumb.width * scale)), max(1, int(thumb.height * scale))),
                Image.Resampling.LANCZOS,
            ))
        columns = 2
        gap = 20
        row_heights = [
            max(thumb.height for thumb in thumbnails[index:index + columns])
            for index in range(0, len(thumbnails), columns)
        ]
        canvas = Image.new(
            "RGB",
            (target_width * columns + gap * (columns + 1), sum(row_heights) + gap * (len(row_heights) + 1)),
            "white",
        )
        y = gap
        for row_index, row_height in enumerate(row_heights):
            x = gap
            for thumb in thumbnails[row_index * columns:(row_index + 1) * columns]:
                canvas.paste(thumb, (x, y))
                x += target_width + gap
            y += row_height + gap
        overview_path = output / "obstacles_topology_repair_9_floors_overview.png"
        canvas.save(overview_path, format="PNG", optimize=True)
    index_path = output / "obstacles_topology_repair_review_index.json"
    _write_json(index_path, {
        "schema_version": 1,
        "legend": {
            "red": "current Stage 05 recognized obstacles",
            "blue": "new outer-wall topology repair obstacle",
            "green": "current raw-CAD exterior door evidence preserved as an opening",
            "amber": "existing enclosed connector is not an exterior wall",
        },
        "floors": rows,
        "overview_path": str(overview_path.resolve()) if overview_path else "",
    })
    return {
        "image_paths": image_paths,
        "overview_path": overview_path.resolve() if overview_path else None,
        "index_path": index_path.resolve(),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--vision-source-run", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    result = repair_outer_wall_topology(args.run_dir, args.vision_source_run)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
