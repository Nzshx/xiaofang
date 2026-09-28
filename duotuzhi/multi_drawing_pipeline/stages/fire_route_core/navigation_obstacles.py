"""Approved wall repairs are immutable hard constraints, not portal carve masks.

The same fingerprinted polygon union is used by Stage 06, graph certification,
effective free space and final route geometry review. Raw-CAD evidence remains
an additional, independent check; it cannot exempt these polygons.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from shapely.geometry import GeometryCollection, LineString, Point, mapping, shape
from shapely.ops import unary_union
from shapely.prepared import prep

POLICY = "approved_original_plus_repaired_walls_v1"
RELATIVE_MANIFEST = Path("obstacles/navigation_hard_constraints/manifest.json")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return path.resolve()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def physical_floor(properties: dict) -> str:
    return str(properties.get("source_floor_id") or properties.get("floor_id") or "").split("__", 1)[0]


def activate_approved_repairs(run: Path, originals: list[Path], repairs: Path) -> dict:
    """Only call after explicit approval of this exact repair artifact."""
    run, repairs = run.resolve(), repairs.resolve()
    repair_payload = read_json(repairs)
    repair_rows = repair_payload.get("features")
    if not isinstance(repair_rows, list) or not repair_rows:
        raise ValueError("Approved repair file must contain polygon features")
    audit_path = repairs.parent / "outer_wall_topology_repair_audit.json"
    if audit_path.is_file():
        expected = (read_json(audit_path).get("historical_vision_reuse_validation") or {}).get("input_dxf_sha256")
        source = Path(read_json(run / "pipeline_summary.json")["input_dxf"])
        if expected and sha256(source) != expected:
            raise ValueError("Approved repairs belong to a different source CAD")
    grouped: dict[str, list] = defaultdict(list)
    counts: dict[str, int] = defaultdict(int)
    repair_counts: dict[str, int] = defaultdict(int)
    sources = []
    for path, is_repair in [(Path(p).resolve(), False) for p in originals] + [(repairs, True)]:
        sources.append({"path": str(path), "sha256": sha256(path), "is_repair": is_repair})
        for row in read_json(path).get("features", []):
            props = row.get("properties") or {}
            floor = physical_floor(props)
            if not floor:
                raise ValueError(f"Missing floor in {path}")
            if is_repair and props.get("kind") != "door_aware_outer_wall_topology_repair":
                raise ValueError("Door/connector evidence is not a wall obstacle")
            geometry = shape(row["geometry"]) if row.get("geometry") else GeometryCollection()
            if not geometry.is_empty and (not geometry.is_valid or geometry.geom_type not in {"Polygon", "MultiPolygon"}):
                raise ValueError(f"Invalid polygon obstacle in {path}")
            if is_repair and geometry.is_empty:
                raise ValueError("Empty approved repair")
            grouped[floor].append(geometry)
            counts[floor] += 1 if is_repair else int(props.get("obstacle_count", 1))
            repair_counts[floor] += int(is_repair)
    rows = []
    for floor, parts in sorted(grouped.items()):
        geometry = unary_union(parts)
        if not geometry.is_valid:
            raise ValueError(f"Invalid merged wall geometry on {floor}")
        rows.append({"type": "Feature", "properties": {"floor_id": floor, "obstacle_count": counts[floor],
                    "approved_repair_count": repair_counts[floor], "kind": POLICY},
                    "geometry": mapping(geometry) if not geometry.is_empty else None})
    path = write_json(run / RELATIVE_MANIFEST.parent / "obstacles.geojson", {"type": "FeatureCollection", "features": rows})
    manifest = {"policy": POLICY, "approved": True, "sources": sources, "geojson_path": str(path),
                "sha256": sha256(path), "obstacle_count": sum(counts.values()),
                "repair_count": len(repair_rows), "floor_ids": sorted(grouped),
                "repaired_floor_ids": sorted(f for f, count in repair_counts.items() if count),
                "portal_carving_allowed": False, "clearance_cad_units": 0.0,
                "boundary_touch_allowed": True, "legacy_closed_envelopes_replaced": True}
    write_json(run / RELATIVE_MANIFEST, manifest)
    return manifest


def approved_manifest(run: Path, *, verify: bool = True) -> dict | None:
    path = run / RELATIVE_MANIFEST
    if not path.is_file():
        return None
    manifest = read_json(path)
    if manifest.get("policy") != POLICY or not manifest.get("approved"):
        raise ValueError("Invalid obstacle approval manifest")
    if verify:
        for item in [*manifest["sources"], {"path": manifest["geojson_path"], "sha256": manifest["sha256"]}]:
            if sha256(Path(item["path"])) != item["sha256"]:
                raise ValueError("Obstacle geometry changed after approval; rebuild and recertify navigation")
    return manifest


class HardObstacleIndex:
    def __init__(self, manifest: dict):
        self.manifest = manifest
        self.path = Path(manifest["geojson_path"])
        if sha256(self.path) != manifest["sha256"]:
            raise ValueError("Hard obstacle geometry fingerprint mismatch")
        self.floors = {}
        for row in read_json(self.path)["features"]:
            self.floors[physical_floor(row["properties"])] = shape(row["geometry"]) if row.get("geometry") else GeometryCollection()
        self.prepared = {floor: prep(geom) for floor, geom in self.floors.items()}

    def geometry(self, floor: str):
        key = floor.split("__", 1)[0]
        if key not in self.floors:
            raise ValueError(f"No hard obstacle coverage for floor {floor}")
        return self.floors[key]

    def reason(self, floor: str, geometry: Any) -> str:
        obstacle = self.geometry(floor)
        if geometry is None or geometry.is_empty or not geometry.is_valid:
            return "invalid_route_geometry"
        if not self.prepared[floor.split("__", 1)[0]].intersects(geometry):
            return ""
        # Interior/interior intersection: touching a polygon boundary is allowed,
        # but no pixel buffer, endpoint exception, or portal can erase a wall.
        if geometry.relate_pattern(obstacle, "T********"):
            return "crosses_original_or_approved_repaired_obstacle"
        return ""


def load_index(run: Path) -> HardObstacleIndex | None:
    manifest = approved_manifest(run)
    return HardObstacleIndex(manifest) if manifest else None


def filter_graph_edges(edges: list[dict], index: HardObstacleIndex) -> tuple[list[dict], list[dict]]:
    kept, rejected = [], []
    for edge in edges:
        try:
            line = shape(edge["geometry"])
            if line.geom_type != "LineString":
                raise ValueError("Edge must be a LineString")
            reason = index.reason(str(edge.get("floor_id") or ""), line)
        except (ValueError, KeyError, TypeError):
            reason = "invalid_edge_geometry_or_unknown_floor"
        if reason:
            rejected.append({"edge_id": edge.get("edge_id"), "floor_id": edge.get("floor_id"),
                             "kind": edge.get("kind"), "reason": reason, "geometry": edge.get("geometry")})
        else:
            kept.append({**edge, "hard_obstacle_certified": True, "hard_obstacle_sha256": index.manifest["sha256"]})
    return kept, rejected


def constrain_effective_free_space(path: Path, index: HardObstacleIndex) -> dict:
    payload = read_json(path)
    removed = {}
    for row in payload["features"]:
        floor = str(row["properties"]["floor_id"])
        before = shape(row["geometry"])
        after = before.difference(index.geometry(floor))
        row["geometry"] = mapping(after)
        row["properties"].update(hard_obstacle_sha256=index.manifest["sha256"], free_area_model=POLICY)
        removed[floor] = max(0.0, before.area - after.area)
    write_json(path, payload)
    return {"policy": POLICY, "sha256": index.manifest["sha256"], "portal_area_removed_by_floor": removed}


def verify_navigation_inputs(run: Path, index: HardObstacleIndex) -> None:
    path = run / "navigation_graph/inputs/navigation_inputs_manifest.json"
    sources = read_json(path).get("sources", {}).get("obstacle_union_geojsons", [])
    if not any(row.get("sha256") == index.manifest["sha256"] for row in sources):
        raise ValueError("Stage06 navigation is stale: rebuild with the approved wall union first")


def audit_route_geometry(forwarding: dict, graph: dict, index: HardObstacleIndex) -> dict:
    failures, checked = [], 0
    edges = {str(row.get("edge_id")): row for row in graph.get("edges", [])}
    for floor_id, floor in forwarding.get("floors", {}).items():
        events = floor.get("traversal_events", [])
        event_ids = {row.get("event_id") for row in events}
        for row in events:
            checked += 1
            edge = edges.get(str(row.get("source_edge_id")))
            context = {"floor_id": floor_id, "event_id": row.get("event_id"), "source_edge_id": row.get("source_edge_id")}
            try:
                line = shape(row["geometry"])
                if line.geom_type != "LineString":
                    raise ValueError("Expected LineString")
                reason = index.reason(floor_id, line)
                if reason:
                    failures.append({**context, "reason": reason})
                if edge is None or edge.get("floor_id") != floor_id or not edge.get("hard_obstacle_certified"):
                    failures.append({**context, "reason": "uncertified_or_wrong_floor_edge"})
                else:
                    reference = shape(edge["geometry"])
                    if not line.equals_exact(reference, 1e-7) and not line.equals_exact(LineString(list(reference.coords)[::-1]), 1e-7):
                        failures.append({**context, "reason": "route_geometry_differs_from_certified_edge"})
            except (ValueError, KeyError, TypeError):
                failures.append({**context, "reason": "invalid_route_geometry"})
        for leg in floor.get("legs", []):
            ids = leg.get("traversal_event_ids", [])
            if leg.get("reachable") and (not set(ids).issubset(event_ids) or (float(leg.get("distance") or 0) > 1e-7 and not ids)):
                failures.append({"floor_id": floor_id, "reason": "missing_leg_traversal_geometry", "leg_index": leg.get("leg_index")})
        for visit in floor.get("target_visit_events", []):
            # Confirmed target access positions, not CAD symbol centroids inside walls.
            xy = visit.get("point") or visit.get("access_point")
            if xy and index.reason(floor_id, Point(xy[:2])):
                failures.append({"floor_id": floor_id, "reason": "target_access_inside_wall", "target_id": visit.get("target_id")})
    return {"policy": POLICY, "hard_obstacle_sha256": index.manifest["sha256"],
            "checked_geometry_count": checked, "geometry_failure_count": len(failures),
            "geometry_failures": failures, "geometric_audit_performed": True}
