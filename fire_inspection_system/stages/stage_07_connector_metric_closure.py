"""Consolidated production stage.

The implementation below is migrated into this file and does not import the
legacy project Python sources at runtime.
"""

from __future__ import annotations

import sys
import types


def _register_embedded_module(name, namespace, *, aliases=()):
    module = types.ModuleType(name)
    module.__dict__.update(namespace)
    module.__name__ = name
    module.__package__ = name.rpartition(".")[0]
    sys.modules[name] = module
    for alias in aliases:
        sys.modules[alias] = module
    if "." in name:
        parent_name, child_name = name.rsplit(".", 1)
        parent = sys.modules.get(parent_name)
        if parent is None:
            parent = types.ModuleType(parent_name)
            parent.__path__ = []
            sys.modules[parent_name] = parent
        setattr(parent, child_name, module)
    return module


def _register_stub_module(name, **symbols):
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    return _register_embedded_module(name, symbols)
from fire_inspection_system.stages import stage_05_obstacles as _stage05
from fire_inspection_system.stages import stage_06_navigation_graph as _stage06

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/connector_portal_refinement.py
# -----------------------------------------------------------------------------
def _build_s07_connector():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/connector_portal_refinement.py'
    )
    __name__ = 'fire_inspection_system.connector_portal_refinement'
    __package__ = 'fire_inspection_system'
    """Refine an AreaGraph navigation graph with connector Portals.

    The upstream Portal implementation is intentionally preserved: its accepted
    Portals split a connected free-space component into semantic areas.  This
    module adds the complementary operation required for navigation:

    * connector Portals bridge two distinct free-space components when supported
      by a CAD door/opening layer or by a very short geometric wall gap;
    * local high-resolution A* reconnects narrow passages that disappear at the
      floor-wide raster resolution;
    * every ordinary/refinement edge remains strictly inside the original free
      space; a connector edge may cross an obstacle only inside its recorded
      door-carve polygon.

    The module produces one final review DXF per run directory, together with the
    modified graph and an acceptance report.
    """
    import argparse
    import csv
    import json
    import math
    import time
    from collections import Counter, defaultdict
    from dataclasses import dataclass
    from pathlib import Path
    from typing import Any, Iterable
    import numpy as np
    import shapely
    from scipy.ndimage import distance_transform_edt
    from scipy.spatial import cKDTree
    from shapely.geometry import GeometryCollection, LineString, Point, Polygon, mapping, shape
    from shapely.ops import nearest_points, unary_union
    from shapely.prepared import prep
    from skimage.graph import MCP_Geometric
    from area_graph_navigation import GraphBuilder, _analyze_reachability, _assign_targets_to_components, _component_catalog, _connected_components, _load_targets
    from obstacle_recognition_door_mask_fixed import ObstacleConfig, bbox_from_row, layer_has_opening_semantics, lines_from_row, load_floor_regions, load_geometry_rows, polygons_from_row
    from portal_area_graph import _load_floor_geometries, _read_json, _write_json
    try:
        import ezdxf
    except ImportError:
        ezdxf = None

    @dataclass
    class RefinementConfig:
        geometric_gap_pixels: float = 1.25
        door_supported_gap_pixels: float = 3.0
        door_evidence_radius_pixels: float = 2.5
        connector_half_width_pixels: float = 0.45
        local_resolution_factor: float = 4.0
        local_max_gap_pixels: float = 10.0
        local_margin_pixels: float = 8.0
        local_max_raster_side: int = 1200
        max_connector_portals_per_floor: int = 256
        max_local_edges_per_floor: int = 256

    class UnionFind:

        def __init__(self, values: Iterable[Any]) -> None:
            self.parent = {value: value for value in values}

        def find(self, value: Any) -> Any:
            parent = self.parent.setdefault(value, value)
            while parent != self.parent[parent]:
                self.parent[parent] = self.parent[self.parent[parent]]
                parent = self.parent[parent]
            while value != parent:
                next_value = self.parent[value]
                self.parent[value] = parent
                value = next_value
            return parent

        def union(self, first: Any, second: Any) -> bool:
            root_a, root_b = (self.find(first), self.find(second))
            if root_a == root_b:
                return False
            self.parent[root_b] = root_a
            return True

    def _load_graph(path: Path) -> GraphBuilder:
        payload = _read_json(path)
        graph = GraphBuilder()
        graph.nodes = {str(row['node_id']): dict(row) for row in payload.get('nodes', [])}
        graph.edges = [dict(row) for row in payload.get('edges', [])]
        graph.adjacency = defaultdict(set)
        for node_id in graph.nodes:
            graph.adjacency[node_id]
        for edge in graph.edges:
            node_a, node_b = (str(edge['node_a']), str(edge['node_b']))
            graph.adjacency[node_a].add(node_b)
            graph.adjacency[node_b].add(node_a)
        graph._edge_index = len(graph.edges)
        return graph

    def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)

    def _iter_coordinates(geometry: Any) -> list[tuple[float, float]]:
        if geometry is None or geometry.is_empty:
            return []
        if geometry.geom_type == 'Point':
            return [(float(geometry.x), float(geometry.y))]
        if geometry.geom_type in {'LineString', 'LinearRing'}:
            return [(float(x), float(y)) for x, y in geometry.coords]
        result: list[tuple[float, float]] = []
        for part in getattr(geometry, 'geoms', []):
            result.extend(_iter_coordinates(part))
        return result

    def _load_opening_evidence(run_dir: Path) -> tuple[dict[str, Any], dict[str, int]]:
        obstacle_summary = _read_json(run_dir / 'obstacles' / 'floor_obstacle_recognition_result.json')
        inventory_dir = Path(obstacle_summary['inventory_dir'])
        sheets_path = Path(obstacle_summary['sheets_json'])
        rows = load_geometry_rows(inventory_dir)
        regions = load_floor_regions(sheets_path)
        config = ObstacleConfig()
        geometries_by_floor: dict[str, list[Any]] = defaultdict(list)
        entity_counts: Counter[str] = Counter()
        for row in rows:
            if not layer_has_opening_semantics(str(row.get('layer') or '')):
                continue
            bounds = bbox_from_row(row)
            if bounds is None:
                continue
            center_x = (bounds[0] + bounds[2]) * 0.5
            center_y = (bounds[1] + bounds[3]) * 0.5
            floor_id = ''
            for region in regions:
                minx, miny, maxx, maxy = region['bbox']
                if minx <= center_x <= maxx and miny <= center_y <= maxy:
                    floor_id = str(region['floor_id'])
                    break
            if not floor_id:
                continue
            geometries = lines_from_row(row, config)
            if not geometries:
                geometries = polygons_from_row(row, config)
            if not geometries:
                geometries = [Point(center_x, center_y)]
            geometries_by_floor[floor_id].extend(geometries)
            entity_counts[floor_id] += 1
        unions = {floor_id: unary_union(geometries) for floor_id, geometries in geometries_by_floor.items() if geometries}
        return (unions, dict(entity_counts))

    def _point_to_rc(point: Point, minx: float, maxy: float, pixel: float, rows: int, cols: int) -> tuple[int, int]:
        row = int(np.clip(round((maxy - point.y) / pixel - 0.5), 0, rows - 1))
        col = int(np.clip(round((point.x - minx) / pixel - 0.5), 0, cols - 1))
        return (row, col)

    def _local_route(polygon: Polygon, start: Point, end: Point, base_pixel: float, config: RefinementConfig) -> list[tuple[float, float]] | None:
        direct = LineString([(start.x, start.y), (end.x, end.y)])
        prepared_polygon = prep(polygon)
        if prepared_polygon.covers(direct):
            return [(start.x, start.y), (end.x, end.y)]
        distance = start.distance(end)
        local_pixel = max(base_pixel / config.local_resolution_factor, 1e-06)
        for multiplier in (1.0, 2.0, 3.0):
            margin = max(config.local_margin_pixels * base_pixel * multiplier, distance * 0.35 * multiplier)
            minx = max(polygon.bounds[0], min(start.x, end.x) - margin)
            miny = max(polygon.bounds[1], min(start.y, end.y) - margin)
            maxx = min(polygon.bounds[2], max(start.x, end.x) + margin)
            maxy = min(polygon.bounds[3], max(start.y, end.y) + margin)
            cols = max(2, int(math.ceil((maxx - minx) / local_pixel)))
            rows = max(2, int(math.ceil((maxy - miny) / local_pixel)))
            longest = max(rows, cols)
            if longest > config.local_max_raster_side:
                scale = longest / config.local_max_raster_side
                pixel = local_pixel * scale
                cols = max(2, int(math.ceil((maxx - minx) / pixel)))
                rows = max(2, int(math.ceil((maxy - miny) / pixel)))
            else:
                pixel = local_pixel
            row_ids, col_ids = np.indices((rows, cols))
            xs = minx + (col_ids.ravel().astype(float) + 0.5) * pixel
            ys = maxy - (row_ids.ravel().astype(float) + 0.5) * pixel
            shapely.prepare(polygon)
            mask = shapely.contains_xy(polygon, xs, ys).reshape((rows, cols))
            if not np.any(mask):
                continue
            _, nearest_indices = distance_transform_edt(~mask, return_indices=True)
            start_rc = _point_to_rc(start, minx, maxy, pixel, rows, cols)
            end_rc = _point_to_rc(end, minx, maxy, pixel, rows, cols)
            if not mask[start_rc]:
                start_rc = (int(nearest_indices[0][start_rc]), int(nearest_indices[1][start_rc]))
            if not mask[end_rc]:
                end_rc = (int(nearest_indices[0][end_rc]), int(nearest_indices[1][end_rc]))
            mcp = MCP_Geometric(np.where(mask, 1.0, np.inf), fully_connected=False)
            costs, _ = mcp.find_costs(starts=[start_rc], ends=[end_rc])
            if not np.isfinite(costs[end_rc]):
                continue
            try:
                path = mcp.traceback(end_rc)
            except ValueError:
                continue
            coordinates = [(minx + (col + 0.5) * pixel, maxy - (row + 0.5) * pixel) for row, col in path]
            if not coordinates:
                continue
            coordinates[0] = (start.x, start.y)
            coordinates[-1] = (end.x, end.y)
            raw_line = LineString(coordinates)
            if not prepared_polygon.covers(raw_line):
                continue
            simplified = raw_line.simplify(pixel * 0.35, preserve_topology=False)
            if simplified.geom_type == 'LineString' and prepared_polygon.covers(simplified):
                return [(float(x), float(y)) for x, y in simplified.coords]
            return [(float(x), float(y)) for x, y in raw_line.coords]
        return None

    def _graph_node_groups(graph: GraphBuilder) -> tuple[dict[str, int], dict[int, list[str]]]:
        return _connected_components(graph)

    def _target_access_nodes(graph: GraphBuilder, targets: list[dict[str, Any]]) -> dict[str, str]:
        result: dict[str, str] = {}
        for target in targets:
            target_id = str(target['target_id'])
            node_id = f'TARGET::{target_id}'
            node = graph.nodes.get(node_id, {})
            access_node_id = str(node.get('access_node_id') or node_id)
            if access_node_id in graph.nodes:
                result[target_id] = access_node_id
        return result

    def _navigable_nodes_by_free_component(graph: GraphBuilder) -> dict[tuple[str, str], list[str]]:
        grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
        preferred = {'skeleton', 'portal', 'virtual_target_access'}
        for node_id, node in graph.nodes.items():
            if node.get('kind') not in preferred:
                continue
            if not graph.adjacency.get(node_id):
                continue
            floor_id = str(node.get('floor_id') or '')
            component_id = str(node.get('component_id') or '')
            if floor_id and component_id:
                grouped[floor_id, component_id].append(node_id)
        return grouped

    def _nearest_node_pairs(graph: GraphBuilder, first_nodes: list[str], second_nodes: list[str], limit: int=24) -> list[tuple[float, str, str]]:
        if not first_nodes or not second_nodes:
            return []
        if len(first_nodes) > len(second_nodes):
            reverse = True
            query_nodes, tree_nodes = (second_nodes, first_nodes)
        else:
            reverse = False
            query_nodes, tree_nodes = (first_nodes, second_nodes)
        tree_coords = np.asarray([(float(graph.nodes[node]['x']), float(graph.nodes[node]['y'])) for node in tree_nodes])
        tree = cKDTree(tree_coords)
        candidates: list[tuple[float, str, str]] = []
        for query_node in query_nodes:
            point = graph.nodes[query_node]
            distance, index = tree.query((float(point['x']), float(point['y'])), k=1)
            tree_node = tree_nodes[int(index)]
            if reverse:
                candidates.append((float(distance), tree_node, query_node))
            else:
                candidates.append((float(distance), query_node, tree_node))
        candidates.sort(key=lambda row: row[0])
        return candidates[:limit]

    def _add_local_refinement_edges(graph: GraphBuilder, targets: list[dict[str, Any]], components: dict[str, list[tuple[str, Polygon]]], pixel_size_by_floor: dict[str, float], config: RefinementConfig) -> list[dict[str, Any]]:
        component_geometry = {(floor_id, component_id): polygon for floor_id, rows in components.items() for component_id, polygon in rows}
        target_access = _target_access_nodes(graph, targets)
        component_by_node, _ = _graph_node_groups(graph)
        target_groups: dict[tuple[str, str], Counter[int]] = defaultdict(Counter)
        for target in targets:
            access_node = target_access.get(str(target['target_id']))
            if not access_node:
                continue
            node = graph.nodes[access_node]
            key = (str(node.get('floor_id') or ''), str(node.get('component_id') or ''))
            target_groups[key][component_by_node.get(access_node, 0)] += 1
        results: list[dict[str, Any]] = []
        result_counts_by_floor: Counter[str] = Counter()
        for (floor_id, free_component_id), graph_counts in sorted(target_groups.items()):
            if len(graph_counts) <= 1:
                continue
            polygon = component_geometry.get((floor_id, free_component_id))
            if polygon is None:
                continue
            max_edges = config.max_local_edges_per_floor
            primary_graph_component = graph_counts.most_common(1)[0][0]
            component_by_node, members = _graph_node_groups(graph)
            primary_nodes = [node_id for node_id in members.get(primary_graph_component, []) if str(graph.nodes[node_id].get('component_id') or '') == free_component_id and graph.nodes[node_id].get('kind') in {'skeleton', 'portal'}]
            for secondary_graph_component, target_count in graph_counts.most_common()[1:]:
                if result_counts_by_floor[floor_id] >= max_edges:
                    break
                component_by_node, members = _graph_node_groups(graph)
                secondary_root = component_by_node.get(next(iter(members.get(secondary_graph_component, [])), ''), secondary_graph_component)
                primary_root = component_by_node.get(primary_nodes[0], primary_graph_component) if primary_nodes else 0
                if secondary_root == primary_root:
                    continue
                secondary_nodes = [node_id for node_id in members.get(secondary_root, []) if str(graph.nodes[node_id].get('component_id') or '') == free_component_id and graph.nodes[node_id].get('kind') in {'skeleton', 'portal'}]
                primary_nodes = [node_id for node_id in members.get(primary_root, []) if str(graph.nodes[node_id].get('component_id') or '') == free_component_id and graph.nodes[node_id].get('kind') in {'skeleton', 'portal'}]
                if not primary_nodes or not secondary_nodes:
                    continue
                accepted = False
                for distance, node_a, node_b in _nearest_node_pairs(graph, primary_nodes, secondary_nodes):
                    if distance > pixel_size_by_floor[floor_id] * config.local_max_gap_pixels:
                        break
                    start = Point(float(graph.nodes[node_a]['x']), float(graph.nodes[node_a]['y']))
                    end = Point(float(graph.nodes[node_b]['x']), float(graph.nodes[node_b]['y']))
                    path = _local_route(polygon, start, end, pixel_size_by_floor[floor_id], config)
                    if path is None:
                        continue
                    edge_id = graph.add_edge(node_a, node_b, path, kind='local_refinement_edge', floor_id=floor_id, component_id=free_component_id, target_count_reconnected=int(target_count), resolution_factor=config.local_resolution_factor, validation_method='strict_local_vector_free_space', vector_valid_with_raster_tolerance=True)
                    results.append({'edge_id': edge_id, 'floor_id': floor_id, 'component_id': free_component_id, 'node_a': node_a, 'node_b': node_b, 'length': float(LineString(path).length), 'target_count_reconnected': int(target_count)})
                    result_counts_by_floor[floor_id] += 1
                    accepted = True
                    break
                if accepted:
                    component_by_node, members = _graph_node_groups(graph)
                    primary_root = component_by_node.get(primary_nodes[0], primary_root)
                    primary_nodes = [node_id for node_id in members.get(primary_root, []) if str(graph.nodes[node_id].get('component_id') or '') == free_component_id and graph.nodes[node_id].get('kind') in {'skeleton', 'portal'}]
        return results

    def _visible_attachment(graph: GraphBuilder, node_ids: list[str], polygon: Polygon, boundary_point: Point) -> tuple[str, list[tuple[float, float]]] | None:
        if not node_ids:
            return None
        coordinates = np.asarray([(float(graph.nodes[node]['x']), float(graph.nodes[node]['y'])) for node in node_ids])
        tree = cKDTree(coordinates)
        count = min(96, len(node_ids))
        distances, indices = tree.query((boundary_point.x, boundary_point.y), k=count)
        indices = np.atleast_1d(indices)
        prepared_polygon = prep(polygon)
        for raw_index in indices:
            node_id = node_ids[int(raw_index)]
            node = graph.nodes[node_id]
            line = LineString([(float(node['x']), float(node['y'])), (boundary_point.x, boundary_point.y)])
            if prepared_polygon.covers(line):
                return (node_id, [(float(node['x']), float(node['y'])), (boundary_point.x, boundary_point.y)])
        return None

    def _connector_candidates(floor_id: str, used_components: Counter[str], geometries: dict[str, Polygon], opening_geometry: Any, pixel_size: float, config: RefinementConfig) -> list[dict[str, Any]]:
        component_ids = [component_id for component_id in used_components if component_id in geometries]
        rows: list[dict[str, Any]] = []
        for index, component_a in enumerate(component_ids):
            geometry_a = geometries[component_a]
            for component_b in component_ids[index + 1:]:
                geometry_b = geometries[component_b]
                distance = float(geometry_a.distance(geometry_b))
                if distance > pixel_size * config.door_supported_gap_pixels:
                    continue
                point_a, point_b = nearest_points(geometry_a, geometry_b)
                bridge_geometry: Any = LineString([(point_a.x, point_a.y), (point_b.x, point_b.y)]) if point_a.distance(point_b) > 1e-09 else point_a
                evidence_distance = math.inf
                if opening_geometry is not None and (not opening_geometry.is_empty):
                    evidence_distance = float(opening_geometry.distance(bridge_geometry))
                door_supported = evidence_distance <= pixel_size * config.door_evidence_radius_pixels
                geometric_supported = distance <= pixel_size * config.geometric_gap_pixels
                if not door_supported and (not geometric_supported):
                    continue
                source = 'door_layer' if door_supported else 'geometric_gap'
                confidence = 0.95 if door_supported else max(0.55, 0.82 - 0.2 * distance / max(pixel_size * config.geometric_gap_pixels, 1e-09))
                rows.append({'floor_id': floor_id, 'component_a': component_a, 'component_b': component_b, 'geometry_a': geometry_a, 'geometry_b': geometry_b, 'point_a': point_a, 'point_b': point_b, 'distance': distance, 'evidence_source': source, 'evidence_distance': evidence_distance if math.isfinite(evidence_distance) else None, 'confidence': confidence, 'target_count_a': int(used_components[component_a]), 'target_count_b': int(used_components[component_b])})
        rows.sort(key=lambda row: (0 if row['evidence_source'] == 'door_layer' else 1, row['distance'], -(row['target_count_a'] + row['target_count_b'])))
        return rows

    def _add_connector_portals(graph: GraphBuilder, targets: list[dict[str, Any]], components: dict[str, list[tuple[str, Polygon]]], pixel_size_by_floor: dict[str, float], opening_by_floor: dict[str, Any], config: RefinementConfig) -> list[dict[str, Any]]:
        target_access = _target_access_nodes(graph, targets)
        used_by_floor: dict[str, Counter[str]] = defaultdict(Counter)
        for target in targets:
            access_node = target_access.get(str(target['target_id']))
            if not access_node:
                continue
            node = graph.nodes[access_node]
            used_by_floor[str(node.get('floor_id') or '')][str(node.get('component_id') or '')] += 1
        node_ids_by_free = _navigable_nodes_by_free_component(graph)
        accepted: list[dict[str, Any]] = []
        sequence_by_floor: Counter[str] = Counter()
        for floor_id, used_components in sorted(used_by_floor.items()):
            geometry_by_id = dict(components.get(floor_id, []))
            candidates = _connector_candidates(floor_id, used_components, geometry_by_id, opening_by_floor.get(floor_id), pixel_size_by_floor[floor_id], config)
            component_by_node, _ = _graph_node_groups(graph)
            graph_roots = set(component_by_node.values())
            union_find = UnionFind(graph_roots)
            for candidate in candidates:
                if sequence_by_floor[floor_id] >= config.max_connector_portals_per_floor:
                    break
                component_a = candidate['component_a']
                component_b = candidate['component_b']
                nodes_a = node_ids_by_free.get((floor_id, component_a), [])
                nodes_b = node_ids_by_free.get((floor_id, component_b), [])
                if not nodes_a or not nodes_b:
                    continue
                roots_a = {union_find.find(component_by_node.get(node, -1)) for node in nodes_a}
                roots_b = {union_find.find(component_by_node.get(node, -1)) for node in nodes_b}
                if roots_a & roots_b:
                    continue
                point_a: Point = candidate['point_a']
                point_b: Point = candidate['point_b']
                attach_a = _visible_attachment(graph, nodes_a, candidate['geometry_a'], point_a)
                attach_b = _visible_attachment(graph, nodes_b, candidate['geometry_b'], point_b)
                if attach_a is None or attach_b is None:
                    continue
                node_a, path_a = attach_a
                node_b, path_b = attach_b
                portal_center = Point((point_a.x + point_b.x) * 0.5, (point_a.y + point_b.y) * 0.5)
                half_width = max(pixel_size_by_floor[floor_id] * config.connector_half_width_pixels, 1e-06)
                if point_a.distance(point_b) > 1e-09:
                    carve = LineString([(point_a.x, point_a.y), (point_b.x, point_b.y)]).buffer(half_width, cap_style=2)
                else:
                    carve = portal_center.buffer(half_width)
                effective_a = prep(unary_union([candidate['geometry_a'], carve]))
                effective_b = prep(unary_union([candidate['geometry_b'], carve]))
                edge_a_coords = path_a + [(portal_center.x, portal_center.y)]
                edge_b_coords = [(portal_center.x, portal_center.y), (point_b.x, point_b.y)] + list(reversed(path_b[:-1]))
                if not effective_a.covers(LineString(edge_a_coords)):
                    continue
                if not effective_b.covers(LineString(edge_b_coords)):
                    continue
                sequence_by_floor[floor_id] += 1
                portal_id = f'{floor_id}_CONNECTOR_PORTAL_{sequence_by_floor[floor_id]:05d}'
                portal_node_id = f'CONNECTOR_PORTAL::{portal_id}'
                graph.add_node(portal_node_id, kind='connector_portal', floor_id=floor_id, component_id=f'{component_a}|{component_b}', component_a=component_a, component_b=component_b, portal_id=portal_id, evidence_source=candidate['evidence_source'], confidence=float(candidate['confidence']), x=float(portal_center.x), y=float(portal_center.y))
                edge_a = graph.add_edge(node_a, portal_node_id, edge_a_coords, kind='connector_portal_edge', floor_id=floor_id, component_id=component_a, connector_portal_id=portal_id, evidence_source=candidate['evidence_source'], validation_method='door_carve_effective_free_space', vector_valid_with_raster_tolerance=True)
                edge_b = graph.add_edge(portal_node_id, node_b, edge_b_coords, kind='connector_portal_edge', floor_id=floor_id, component_id=component_b, connector_portal_id=portal_id, evidence_source=candidate['evidence_source'], validation_method='door_carve_effective_free_space', vector_valid_with_raster_tolerance=True)
                root_a = component_by_node.get(node_a, -1)
                root_b = component_by_node.get(node_b, -1)
                union_find.union(root_a, root_b)
                accepted.append({'portal_id': portal_id, 'floor_id': floor_id, 'component_a': component_a, 'component_b': component_b, 'node_a': node_a, 'node_b': node_b, 'edge_a': edge_a, 'edge_b': edge_b, 'gap_distance': float(candidate['distance']), 'evidence_source': candidate['evidence_source'], 'evidence_distance': candidate['evidence_distance'], 'confidence': float(candidate['confidence']), 'target_count_a': int(candidate['target_count_a']), 'target_count_b': int(candidate['target_count_b']), 'x': float(portal_center.x), 'y': float(portal_center.y), 'geometry': mapping(carve)})
        return accepted

    def _write_review_dxf(path: Path, source_dxf: Path | None, graph: GraphBuilder, reachability_rows: list[dict[str, Any]], connector_portals: list[dict[str, Any]]) -> None:
        if ezdxf is None:
            return
        document = ezdxf.readfile(source_dxf) if source_dxf and source_dxf.exists() else ezdxf.new('R2010')
        layers = {'RG_REFINED_BASE_EDGE': (3, 18), 'RG_CONNECTOR_PORTAL_EDGE': (6, 35), 'RG_LOCAL_REFINEMENT_EDGE': (4, 35), 'RG_CONNECTOR_PORTAL_NODE': (1, 40), 'RG_TARGET_REACHABLE': (3, 25), 'RG_TARGET_UNREACHABLE': (1, 35), 'RG_REFINED_LABEL': (7, 18)}
        for name, (color, lineweight) in layers.items():
            if name not in document.layers:
                document.layers.add(name=name, color=color, lineweight=lineweight)
        modelspace = document.modelspace()
        for edge in graph.edges:
            kind = str(edge.get('kind') or '')
            if kind == 'virtual_target_access_edge':
                continue
            layer = 'RG_REFINED_BASE_EDGE'
            if kind == 'connector_portal_edge':
                layer = 'RG_CONNECTOR_PORTAL_EDGE'
            elif kind == 'local_refinement_edge':
                layer = 'RG_LOCAL_REFINEMENT_EDGE'
            coordinates = edge.get('geometry', {}).get('coordinates', [])
            if len(coordinates) >= 2:
                modelspace.add_lwpolyline(coordinates, dxfattribs={'layer': layer})
        lengths = [float(edge.get('length') or 0.0) for edge in graph.edges if float(edge.get('length') or 0.0) > 0]
        radius = max(60.0, min(500.0, (float(np.median(lengths)) if lengths else 500.0) * 0.08))
        for portal in connector_portals:
            modelspace.add_circle((portal['x'], portal['y']), radius * 1.2, dxfattribs={'layer': 'RG_CONNECTOR_PORTAL_NODE'})
            label = modelspace.add_text(f"{portal['portal_id']} {portal['evidence_source']} gap={portal['gap_distance']:.0f}", dxfattribs={'layer': 'RG_REFINED_LABEL', 'height': radius})
            label.dxf.insert = (portal['x'] + radius, portal['y'] + radius)
        reachability_by_target = {str(row['target_id']): row for row in reachability_rows}
        for node in graph.nodes.values():
            if node.get('kind') != 'target':
                continue
            row = reachability_by_target.get(str(node.get('target_id') or ''), {})
            reachable = bool(row.get('attached'))
            layer = 'RG_TARGET_REACHABLE' if reachable else 'RG_TARGET_UNREACHABLE'
            modelspace.add_circle((node['x'], node['y']), radius * 0.65, dxfattribs={'layer': layer})
        path.parent.mkdir(parents=True, exist_ok=True)
        document.saveas(path)

    def _format_rate(value: float) -> str:
        return f'{value * 100:.2f}%'

    def _write_acceptance_report(path: Path, run_name: str, before: dict[str, Any], after: dict[str, Any], graph_stats: dict[str, Any], connector_portals: list[dict[str, Any]], local_edges: list[dict[str, Any]], opening_counts: dict[str, int], strict_failures: int, elapsed: float) -> None:
        floors = sorted(set(before.get('floors', {})) | set(after.get('floors', {})))
        floor_lines = []
        for floor_id in floors:
            old = before.get('floors', {}).get(floor_id, {})
            new = after.get('floors', {}).get(floor_id, {})
            floor_lines.append(f"| {floor_id} | {old.get('target_count', 0)} | {_format_rate(float(old.get('pairwise_target_reachability_rate', 0.0)))} | {_format_rate(float(new.get('pairwise_target_reachability_rate', 0.0)))} | {_format_rate(float(new.get('attachment_rate', 0.0)))} | {new.get('target_graph_component_count', 0)} |")
        evidence_counts = Counter((row['evidence_source'] for row in connector_portals))
        report = f"# 连接型 Portal 规则导航图验收报告\n\n## 1. 运行对象\n\n- 运行目录：`{run_name}`\n- 保留原有分割型 Portal，并新增跨自由空间分量的连接型 Portal。\n- 每个运行目录只生成一张最终审核 DXF。\n\n## 2. 可达率对比\n\n| 楼层 | 目标数 | 修改前两两可达率 | 修改后两两可达率 | 修改后接入率 | 修改后目标图分量数 |\n|---|---:|---:|---:|---:|---:|\n{chr(10).join(floor_lines)}\n\n- 修改前同楼层加权两两可达率：**{_format_rate(float(before.get('same_floor_pairwise_target_reachability_rate', 0.0)))}**\n- 修改后同楼层加权两两可达率：**{_format_rate(float(after.get('same_floor_pairwise_target_reachability_rate', 0.0)))}**\n- 修改后同楼层接入率：**{_format_rate(float(after.get('same_floor_attachment_rate', after.get('attachment_rate', 0.0))))}**\n\n## 3. 修改内容\n\n- 连接型 Portal：{len(connector_portals)} 个，其中门图层证据 {evidence_counts.get('door_layer', 0)} 个，短几何墙缝证据 {evidence_counts.get('geometric_gap', 0)} 个。\n- 狭窄区域局部加密补边：{len(local_edges)} 条，局部分辨率为基础像素的 `1/4`。\n- 可用门/留洞实体数：{sum(opening_counts.values())}。\n- 修改后节点数：{graph_stats['node_count']}，边数：{graph_stats['edge_count']}。\n\n## 4. 严格验收\n\n- 普通骨架边和局部加密边必须完全位于原始自由空间。\n- 连接型 Portal 边只允许在记录的门洞 carve 多边形内跨越原障碍物。\n- 严格几何校验失败边数：**{strict_failures}**。\n- 验收结论：**{('通过' if strict_failures == 0 else '不通过')}**。\n\n## 5. 解释限制\n\n没有门图层的楼层只能使用不超过约 `1.25` 个基础像素的短墙缝证据，不会跨越楼栋之间的大间距。若同一 `floor_id` 实际包含多个独立楼栋，其跨楼栋目标仍保持不连通，这属于正确拓扑，不应通过虚构导航边提高指标。\n\n## 6. 运行耗时\n\n- {elapsed:.3f} 秒\n"
        path.write_text(report, encoding='utf-8')

    def _validate_added_edges(graph: GraphBuilder, component_geometry: dict[tuple[str, str], Polygon], connector_portals: list[dict[str, Any]]) -> int:
        portal_carves = {str(row['portal_id']): shape(row['geometry']) for row in connector_portals}
        failures = 0
        for edge in graph.edges:
            kind = str(edge.get('kind') or '')
            if kind not in {'local_refinement_edge', 'connector_portal_edge'}:
                continue
            floor_id = str(edge.get('floor_id') or '')
            component_id = str(edge.get('component_id') or '')
            polygon = component_geometry.get((floor_id, component_id))
            if polygon is None:
                failures += 1
                continue
            line = LineString(edge['geometry']['coordinates'])
            if kind == 'local_refinement_edge':
                valid = prep(polygon).covers(line)
            else:
                carve = portal_carves.get(str(edge.get('connector_portal_id') or ''), GeometryCollection())
                valid = prep(unary_union([polygon, carve])).covers(line)
            if not valid:
                failures += 1
        return failures

    def refine_run(run_dir: Path, output_dir: Path | None=None, config: RefinementConfig | None=None) -> dict[str, Any]:
        config = config or RefinementConfig()
        started = time.perf_counter()
        run_dir = Path(run_dir).resolve()
        output_dir = Path(output_dir or run_dir / 'area_graph_navigation_refined').resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        base_dir = run_dir / 'area_graph_navigation'
        base_graph_path = base_dir / 'rule_navigation_graph.json'
        base_summary_path = base_dir / 'reachability_summary.json'
        free_path = run_dir / 'navigation_graph' / 'inputs' / 'free_areas.geojson'
        area_summary_path = run_dir / 'area_graph' / 'area_graph_summary.json'
        for required in (base_graph_path, base_summary_path, free_path, area_summary_path):
            if not required.exists():
                raise FileNotFoundError(required)
        graph = _load_graph(base_graph_path)
        before_summary = _read_json(base_summary_path)['reachability']
        area_summary = _read_json(area_summary_path)
        pixel_size_by_floor = {str(key): float(value) for key, value in area_summary['pixel_size_by_floor'].items()}
        free_by_floor = _load_floor_geometries(free_path)
        components = _component_catalog(free_by_floor)
        component_geometry = {(floor_id, component_id): polygon for floor_id, rows in components.items() for component_id, polygon in rows}
        targets = _load_targets(run_dir / 'navigation_graph' / 'inputs' / 'navigation_targets.geojson')
        _assign_targets_to_components(targets, components)
        opening_by_floor, opening_counts = _load_opening_evidence(run_dir)
        print(f'[refine] {run_dir.name}: local narrow-passage refinement', flush=True)
        local_edges = _add_local_refinement_edges(graph, targets, components, pixel_size_by_floor, config)
        print(f'[refine] local edges={len(local_edges)}', flush=True)
        connector_portals = _add_connector_portals(graph, targets, components, pixel_size_by_floor, opening_by_floor, config)
        print(f'[refine] connector portals={len(connector_portals)}', flush=True)
        reachability_rows, after_summary = _analyze_reachability(graph, targets)
        strict_failures = _validate_added_edges(graph, component_geometry, connector_portals)
        node_kind_counts = Counter((str(row.get('kind') or '') for row in graph.nodes.values()))
        edge_kind_counts = Counter((str(row.get('kind') or '') for row in graph.edges))
        graph_stats = {'node_count': len(graph.nodes), 'edge_count': len(graph.edges), 'node_kind_counts': dict(node_kind_counts), 'edge_kind_counts': dict(edge_kind_counts), 'connector_portal_node_count': node_kind_counts.get('connector_portal', 0), 'connector_portal_edge_count': edge_kind_counts.get('connector_portal_edge', 0), 'local_refinement_edge_count': edge_kind_counts.get('local_refinement_edge', 0)}
        outputs = {'graph_json': output_dir / 'refined_navigation_graph.json', 'nodes_csv': output_dir / 'refined_navigation_nodes.csv', 'edges_csv': output_dir / 'refined_navigation_edges.csv', 'connector_geojson': output_dir / 'connector_portals.geojson', 'connector_csv': output_dir / 'connector_portals.csv', 'reachability_csv': output_dir / 'refined_target_reachability.csv', 'summary_json': output_dir / 'acceptance_summary.json', 'report_md': output_dir / 'acceptance_report.md', 'review_dxf': output_dir / 'refined_navigation_review.dxf'}
        _write_json(outputs['graph_json'], {'graph_type': 'area_graph_with_connector_portals', 'directed': False, 'nodes': list(graph.nodes.values()), 'edges': graph.edges})
        _write_csv(outputs['nodes_csv'], list(graph.nodes.values()), ['node_id', 'kind', 'floor_id', 'component_id', 'area_id', 'portal_id', 'target_id', 'target_class', 'evidence_source', 'confidence', 'x', 'y'])
        edge_rows = [{**row, 'geometry': json.dumps(row.get('geometry', {}), ensure_ascii=False)} for row in graph.edges]
        _write_csv(outputs['edges_csv'], edge_rows, ['edge_id', 'node_a', 'node_b', 'kind', 'floor_id', 'component_id', 'length', 'connector_portal_id', 'evidence_source', 'validation_method', 'geometry'])
        connector_features = [{'type': 'Feature', 'properties': {key: value for key, value in row.items() if key != 'geometry'}, 'geometry': row['geometry']} for row in connector_portals]
        _write_json(outputs['connector_geojson'], {'type': 'FeatureCollection', 'features': connector_features})
        connector_csv_rows = [{key: value for key, value in row.items() if key != 'geometry'} for row in connector_portals]
        _write_csv(outputs['connector_csv'], connector_csv_rows, ['portal_id', 'floor_id', 'component_a', 'component_b', 'node_a', 'node_b', 'gap_distance', 'evidence_source', 'evidence_distance', 'confidence', 'target_count_a', 'target_count_b', 'x', 'y'])
        _write_csv(outputs['reachability_csv'], reachability_rows, ['target_id', 'target_class', 'floor_id', 'component_id', 'area_id', 'attached', 'virtual_access', 'graph_component', 'reachable_to_floor_primary'])
        raw_source = str(area_summary.get('source_dxf') or '')
        source_dxf = Path(raw_source) if raw_source and Path(raw_source).exists() else None
        print('[refine] writing final review DXF', flush=True)
        _write_review_dxf(outputs['review_dxf'], source_dxf, graph, reachability_rows, connector_portals)
        elapsed = time.perf_counter() - started
        acceptance = {'run_dir': str(run_dir), 'before_reachability': before_summary, 'after_reachability': after_summary, 'graph': graph_stats, 'opening_entity_counts_by_floor': opening_counts, 'connector_portal_count': len(connector_portals), 'connector_evidence_counts': dict(Counter((row['evidence_source'] for row in connector_portals))), 'local_refinement_edge_count': len(local_edges), 'strict_validation_failure_count': strict_failures, 'accepted': strict_failures == 0, 'elapsed_seconds': round(elapsed, 6), 'outputs': {key: str(value) for key, value in outputs.items()}}
        _write_json(outputs['summary_json'], acceptance)
        _write_acceptance_report(outputs['report_md'], run_dir.name, before_summary, after_summary, graph_stats, connector_portals, local_edges, opening_counts, strict_failures, elapsed)
        return acceptance
    return dict(locals())

_s07_connector = _register_embedded_module(
    'fire_inspection_system.connector_portal_refinement',
    _build_s07_connector(),
    aliases=('connector_portal_refinement',),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/dual_graph/effective_free_space.py
# -----------------------------------------------------------------------------
def _build_s07_effective_free():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/dual_graph/effective_free_space.py'
    )
    __name__ = 'fire_inspection_system.dual_graph.effective_free_space'
    __package__ = 'fire_inspection_system.dual_graph'
    """Build the effective vector free-space polygons used by the dual graph."""
    import json
    from pathlib import Path
    from typing import Any
    import shapely
    from shapely.geometry import mapping, shape
    from shapely.ops import unary_union

    def _read_json(path: Path) -> Any:
        return json.loads(path.read_text(encoding='utf-8'))

    def _write_json(path: Path, value: Any) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        return path

    def _make_valid(geometry: Any) -> Any:
        if geometry.is_valid:
            return geometry
        make_valid = getattr(shapely, 'make_valid', None)
        return make_valid(geometry) if make_valid else geometry.buffer(0)

    def _polygon_parts(geometry: Any) -> list[Any]:
        geometry = _make_valid(geometry)
        if geometry.is_empty:
            return []
        if geometry.geom_type == 'Polygon':
            return [geometry]
        return [part for part in getattr(geometry, 'geoms', ()) if part.geom_type == 'Polygon' and (not part.is_empty)]

    def _load_free_areas(path: Path) -> dict[str, dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for feature in _read_json(path).get('features') or []:
            properties = dict(feature.get('properties') or {})
            floor_id = str(properties.get('floor_id') or '').strip()
            geometry_payload = feature.get('geometry')
            if not floor_id or not geometry_payload:
                continue
            geometry = shape(geometry_payload)
            parts = _polygon_parts(geometry)
            if not parts:
                continue
            row = grouped.setdefault(floor_id, {'floor_name': str(properties.get('floor_name') or floor_id), 'geometries': []})
            row['geometries'].extend(parts)
        return {floor_id: {'floor_name': row['floor_name'], 'geometry': _make_valid(unary_union(row['geometries']))} for floor_id, row in grouped.items()}

    def _part_count(geometry: Any) -> int:
        return len(_polygon_parts(geometry))

    def build_effective_free_areas(free_area_path: Path | str, connector_portals_path: Path | str | None, output_path: Path | str) -> tuple[Path, dict[str, Any]]:
        """Union accepted Connector Portal polygons with same-floor free space."""
        source = Path(free_area_path).resolve()
        output = Path(output_path).resolve()
        floors = _load_free_areas(source)
        portal_source = Path(connector_portals_path).resolve() if connector_portals_path else None
        portals_by_floor: dict[str, list[tuple[Any, dict[str, Any]]]] = {}
        if portal_source and portal_source.exists():
            for feature in _read_json(portal_source).get('features') or []:
                properties = dict(feature.get('properties') or {})
                floor_id = str(properties.get('floor_id') or '').strip()
                geometry_payload = feature.get('geometry')
                if not floor_id or not geometry_payload:
                    continue
                parts = _polygon_parts(shape(geometry_payload))
                for geometry in parts:
                    portals_by_floor.setdefault(floor_id, []).append((geometry, properties))
        features: list[dict[str, Any]] = []
        floor_audit: dict[str, Any] = {}
        for floor_id, row in sorted(floors.items()):
            raw = row['geometry']
            portal_rows = portals_by_floor.get(floor_id, [])
            effective = _make_valid(unary_union([raw, *(geometry for geometry, _ in portal_rows)]))
            features.append({'type': 'Feature', 'properties': {'floor_id': floor_id, 'floor_name': row['floor_name'], 'free_area_model': 'raw_vector_free_area_plus_accepted_connector_portals', 'accepted_connector_portal_count': len(portal_rows), 'raster_or_grid_routing_used': False}, 'geometry': mapping(effective)})
            floor_audit[floor_id] = {'accepted_connector_portal_count': len(portal_rows), 'portal_ids': [str(properties.get('portal_id') or '') for _, properties in portal_rows], 'raw_polygon_component_count': _part_count(raw), 'effective_polygon_component_count': _part_count(effective), 'raw_free_area': float(raw.area), 'effective_free_area': float(effective.area), 'added_portal_area': float(max(0.0, effective.area - raw.area))}
        _write_json(output, {'type': 'FeatureCollection', 'features': features})
        audit = {'schema_version': 1, 'repair_type': 'accepted_connector_portal_vector_polygon_union', 'raster_or_grid_routing_used': False, 'raw_free_areas': str(source), 'connector_portals': str(portal_source) if portal_source and portal_source.exists() else '', 'effective_free_areas': str(output), 'accepted_connector_portal_count': sum((row['accepted_connector_portal_count'] for row in floor_audit.values())), 'floors': floor_audit}
        return (output, audit)
    __all__ = ['build_effective_free_areas']
    return dict(locals())

_s07_effective_free = _register_embedded_module(
    'fire_inspection_system.dual_graph.effective_free_space',
    _build_s07_effective_free(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/dual_graph/physical_access.py
# -----------------------------------------------------------------------------
def _build_s07_physical_access():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/dual_graph/physical_access.py'
    )
    __name__ = 'fire_inspection_system.dual_graph.physical_access'
    __package__ = 'fire_inspection_system.dual_graph'
    """Pure-vector virtual access adapters for the dual-graph route chain.

    Some semantic inspection targets are intentionally retained even when the
    original navigation extractor did not create a target node for them.  They
    must not be snapped through the retired raster access pipeline.  Instead, the
    target is represented by a zero-length alias at the nearest existing
    same-floor physical-navigation node.  The object location and the access
    distance remain in the audit trail; only the alias participates in routing.
    """
    import copy
    import math
    from collections import defaultdict
    from typing import Any, Mapping

    def _text(value: Any) -> str:
        return '' if value is None else str(value).strip()

    def _finite(value: Any, default: float=math.nan) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return default
        return result if math.isfinite(result) else default

    def _candidate_point(row: Mapping[str, Any]) -> tuple[float, float] | None:
        point = row.get('point')
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            x, y = (_finite(point[0]), _finite(point[1]))
        else:
            x = _finite(row.get('raw_x'), _finite(row.get('x')))
            y = _finite(row.get('raw_y'), _finite(row.get('y')))
        return (x, y) if math.isfinite(x) and math.isfinite(y) else None

    def augment_virtual_target_access_nodes(graph: Mapping[str, Any], candidate_bundle: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Add missing target aliases without adding an off-graph travel edge."""
        result = copy.deepcopy(dict(graph))
        nodes = [dict(row) for row in result.get('nodes', []) or []]
        edges = [dict(row) for row in result.get('edges', []) or []]
        present_targets = {_text(row.get('target_id')) for row in nodes if _text(row.get('target_id'))}
        anchors_by_floor: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for node in nodes:
            floor_id = _text(node.get('floor_id'))
            x, y = (_finite(node.get('x')), _finite(node.get('y')))
            if floor_id and _text(node.get('kind')) != 'target' and math.isfinite(x) and math.isfinite(y):
                anchors_by_floor[floor_id].append(node)
        added: list[dict[str, Any]] = []
        unavailable: list[dict[str, Any]] = []
        for floor_id, floor in sorted((candidate_bundle.get('floors') or {}).items()):
            anchors = anchors_by_floor.get(_text(floor_id), [])
            for candidate in floor.get('candidates', []) or []:
                target_id = _text(candidate.get('target_id'))
                if not target_id or target_id in present_targets:
                    continue
                raw_point = _candidate_point(candidate)
                if raw_point is None or not anchors:
                    unavailable.append({'floor_id': _text(floor_id), 'target_id': target_id, 'reason': 'missing_candidate_point_or_same_floor_physical_anchor'})
                    continue
                anchor = min(anchors, key=lambda row: (math.hypot(_finite(row.get('x')) - raw_point[0], _finite(row.get('y')) - raw_point[1]), _text(row.get('node_id'))))
                anchor_id = _text(anchor.get('node_id'))
                anchor_point = (_finite(anchor.get('x')), _finite(anchor.get('y')))
                target_node_id = f'TARGET::{target_id}'
                access_distance = math.dist(raw_point, anchor_point)
                node = {'node_id': target_node_id, 'kind': 'target', 'floor_id': _text(floor_id), 'component_id': _text(anchor.get('component_id')), 'area_id': _text(anchor.get('area_id')), 'target_id': target_id, 'target_class': _text(candidate.get('class_name') or candidate.get('target_class')), 'source_object_id': _text(candidate.get('object_id') or candidate.get('source_object_id')), 'assignment_status': 'pure_vector_virtual_access_alias', 'raw_x': raw_point[0], 'raw_y': raw_point[1], 'x': anchor_point[0], 'y': anchor_point[1], 'projection_distance': access_distance, 'virtual_access_distance': access_distance, 'access_node_id': target_node_id, 'virtual_access': True, 'virtual_anchor_node_id': anchor_id, 'virtual_access_policy': 'nearest_same_floor_physical_navigation_node', 'raster_or_grid_used': False}
                edge_id = f'VIRTUAL_ACCESS::{target_id}'
                edge = {'edge_id': edge_id, 'node_a': anchor_id, 'node_b': target_node_id, 'length': 1e-06, 'kind': 'virtual_target_access_edge', 'floor_id': _text(floor_id), 'component_id': _text(anchor.get('component_id')), 'geometry': {'type': 'LineString', 'coordinates': [list(anchor_point), list(anchor_point)]}, 'virtual_access': True, 'raster_or_grid_used': False}
                nodes.append(node)
                edges.append(edge)
                present_targets.add(target_id)
                added.append({'floor_id': _text(floor_id), 'target_id': target_id, 'target_node_id': target_node_id, 'anchor_node_id': anchor_id, 'virtual_access_distance': access_distance})
        result['nodes'] = nodes
        result['edges'] = edges
        audit = {'policy': 'nearest_same_floor_physical_navigation_node_zero_length_alias', 'raster_or_grid_used': False, 'added_virtual_target_count': len(added), 'unavailable_target_count': len(unavailable), 'added_virtual_targets': added, 'unavailable_targets': unavailable}
        result['dual_graph_virtual_access'] = audit
        return (result, audit)
    __all__ = ['augment_virtual_target_access_nodes']
    return dict(locals())

_s07_physical_access = _register_embedded_module(
    'fire_inspection_system.dual_graph.physical_access',
    _build_s07_physical_access(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/semantic/physical_metric_closure.py
# -----------------------------------------------------------------------------
def _build_s07_metric_closure():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/semantic/physical_metric_closure.py'
    )
    __name__ = 'fire_inspection_system.semantic.physical_metric_closure'
    __package__ = 'fire_inspection_system.semantic'
    """Precompute and query true target-to-target costs on a physical graph.

    The closure intentionally stores costs, not complete path geometries.  Route
    planning can therefore query all target pairs cheaply and run one final path
    expansion only for consecutive targets in the selected open chain.
    """
    import argparse
    import hashlib
    import json
    import math
    import time
    from collections import defaultdict
    from dataclasses import asdict, dataclass
    from pathlib import Path
    from typing import Any, Mapping, Sequence
    import numpy as np
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import connected_components, dijkstra
    EPS = 1e-09
    SCHEMA_VERSION = 1

    def _text(value: Any) -> str:
        return '' if value is None else str(value).strip()

    def _finite(value: Any, default: float=0.0) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if math.isfinite(number) else default

    def _read_json(path: Path) -> Any:
        with path.open('r', encoding='utf-8') as handle:
            return json.load(handle)

    def _write_json(path: Path, value: Any) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        return path

    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(block)
        return digest.hexdigest()

    def _safe_floor_name(floor_id: str) -> str:
        cleaned = ''.join((character if character.isalnum() or character in '-_.' else '_' for character in floor_id))
        return cleaned or 'UNKNOWN'

    @dataclass(frozen=True)
    class ClosureBuildConfig:
        dtype: str = 'float32'
        resume: bool = True
        include_euclidean_baseline: bool = True
        dijkstra_batch_size: int = 32
        euclidean_block_size: int = 512

    def _group_graph_by_floor(graph: Mapping[str, Any]) -> tuple[dict[str, list[Mapping[str, Any]]], dict[str, list[Mapping[str, Any]]], int]:
        """Scan the graph once and retain references rather than copying rows."""
        nodes_by_floor: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        node_floor: dict[str, str] = {}
        for row in graph.get('nodes', []) or []:
            floor_id, node_id = (_text(row.get('floor_id')), _text(row.get('node_id')))
            if not floor_id or not node_id:
                continue
            nodes_by_floor[floor_id].append(row)
            node_floor[node_id] = floor_id
        edges_by_floor: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        cross_floor_or_invalid = 0
        for row in graph.get('edges', []) or []:
            left_floor = node_floor.get(_text(row.get('node_a')))
            right_floor = node_floor.get(_text(row.get('node_b')))
            if not left_floor or left_floor != right_floor:
                cross_floor_or_invalid += 1
                continue
            edges_by_floor[left_floor].append(row)
        return (dict(nodes_by_floor), dict(edges_by_floor), cross_floor_or_invalid)

    def _compact_undirected_csr(node_ids: Sequence[str], edges: Sequence[Mapping[str, Any]]) -> tuple[csr_matrix, int]:
        """Build a symmetric CSR and retain the minimum of parallel-edge costs."""
        index_by_node = {node_id: index for index, node_id in enumerate(node_ids)}
        capacity = 2 * len(edges)
        rows = np.empty(capacity, dtype=np.int32)
        columns = np.empty(capacity, dtype=np.int32)
        weights = np.empty(capacity, dtype=np.float64)
        position = 0
        usable_edge_count = 0
        for edge in edges:
            left = index_by_node.get(_text(edge.get('node_a')))
            right = index_by_node.get(_text(edge.get('node_b')))
            length = _finite(edge.get('length'), math.nan)
            if left is None or right is None or (not math.isfinite(length)) or (length < 0.0):
                continue
            rows[position:position + 2] = (left, right)
            columns[position:position + 2] = (right, left)
            weights[position:position + 2] = length
            position += 2
            usable_edge_count += 1
        if position == 0:
            return (csr_matrix((len(node_ids), len(node_ids)), dtype=np.float64), usable_edge_count)
        rows, columns, weights = (rows[:position], columns[:position], weights[:position])
        order = np.lexsort((columns, rows))
        rows, columns, weights = (rows[order], columns[order], weights[order])
        starts = np.empty(position, dtype=bool)
        starts[0] = True
        starts[1:] = (rows[1:] != rows[:-1]) | (columns[1:] != columns[:-1])
        start_indices = np.flatnonzero(starts)
        rows = rows[start_indices]
        columns = columns[start_indices]
        weights = np.minimum.reduceat(weights, start_indices)
        matrix = csr_matrix((weights, (rows, columns)), shape=(len(node_ids), len(node_ids)))
        matrix.sort_indices()
        return (matrix, usable_edge_count)

    def _batched_target_dijkstra(graph: csr_matrix, target_node_indices: np.ndarray, *, output_dtype: np.dtype, batch_size: int) -> np.ndarray:
        count = int(target_node_indices.size)
        result = np.full((count, count), np.inf, dtype=output_dtype)
        for start in range(0, count, batch_size):
            stop = min(count, start + batch_size)
            full_rows = dijkstra(graph, directed=True, indices=target_node_indices[start:stop], return_predecessors=False)
            result[start:stop, :] = np.atleast_2d(full_rows)[:, target_node_indices].astype(output_dtype, copy=False)
        for start in range(0, count, batch_size):
            stop = min(count, start + batch_size)
            symmetric = np.minimum(result[start:stop, :], result[:, start:stop].T)
            result[start:stop, :] = symmetric
            result[:, start:stop] = symmetric.T
        np.fill_diagonal(result, 0.0)
        return result

    def _blocked_euclidean(coordinates: np.ndarray, *, output_dtype: np.dtype, block_size: int) -> np.ndarray:
        """Compute T x T distances with B x T temporaries, never T x T x 2."""
        count = int(coordinates.shape[0])
        result = np.empty((count, count), dtype=output_dtype)
        centered = coordinates - coordinates.mean(axis=0, keepdims=True) if count else coordinates
        squared_norm = np.einsum('ij,ij->i', centered, centered)
        for start in range(0, count, block_size):
            stop = min(count, start + block_size)
            squared = squared_norm[start:stop, None] + squared_norm[None, :] - 2.0 * centered[start:stop] @ centered.T
            np.maximum(squared, 0.0, out=squared)
            np.sqrt(squared, out=squared)
            result[start:stop, :] = squared.astype(output_dtype, copy=False)
        np.fill_diagonal(result, 0.0)
        return result

    def _distance_statistics(distance: np.ndarray, block_size: int) -> tuple[int, float]:
        reachable_pairs = 0
        finite_max = 0.0
        count = int(distance.shape[0])
        for start in range(0, count, block_size):
            stop = min(count, start + block_size)
            block = distance[start:stop, :]
            finite = np.isfinite(block)
            reachable_pairs += int(finite.sum())
            if finite.any():
                finite_max = max(finite_max, float(block[finite].max()))
        return (max(0, reachable_pairs - count), finite_max)

    def _build_floor_closure(floor_id: str, nodes: Sequence[Mapping[str, Any]], edges: Sequence[Mapping[str, Any]], *, dtype: str, include_euclidean: bool, dijkstra_batch_size: int, euclidean_block_size: int) -> dict[str, Any]:
        output_dtype = np.dtype(dtype)
        if output_dtype not in (np.dtype('float32'), np.dtype('float64')):
            raise ValueError('closure dtype must be float32 or float64')
        if dijkstra_batch_size <= 0 or euclidean_block_size <= 0:
            raise ValueError('Dijkstra batch size and Euclidean block size must be positive')
        preparation_started = time.perf_counter()
        node_ids = [_text(row.get('node_id')) for row in nodes]
        index_by_node = {node_id: index for index, node_id in enumerate(node_ids)}
        physical_csr, usable_edge_count = _compact_undirected_csr(node_ids, edges)
        component_count, component_by_index = connected_components(physical_csr, directed=False, return_labels=True)
        preparation_seconds = time.perf_counter() - preparation_started
        targets: list[tuple[str, int, float, float, str]] = []
        seen_targets: set[str] = set()
        for node in nodes:
            target_id = _text(node.get('target_id'))
            node_id = _text(node.get('node_id'))
            if not target_id or target_id in seen_targets or node.get('kind') not in {'target', 'virtual_target_access'}:
                continue
            seen_targets.add(target_id)
            targets.append((target_id, index_by_node[node_id], _finite(node.get('x'), math.nan), _finite(node.get('y'), math.nan), node_id))
        targets.sort(key=lambda row: row[0])
        target_ids = [row[0] for row in targets]
        target_node_indices = np.asarray([row[1] for row in targets], dtype=np.int32)
        target_node_ids = [row[4] for row in targets]
        count = len(targets)
        dijkstra_started = time.perf_counter()
        distance = _batched_target_dijkstra(physical_csr, target_node_indices, output_dtype=output_dtype, batch_size=dijkstra_batch_size)
        dijkstra_seconds = time.perf_counter() - dijkstra_started
        euclidean = np.empty((0, 0), dtype=np.float32)
        euclidean_seconds = 0.0
        if include_euclidean:
            euclidean_started = time.perf_counter()
            coordinates = np.asarray([[row[2], row[3]] for row in targets], dtype=np.float64)
            euclidean = _blocked_euclidean(coordinates, output_dtype=output_dtype, block_size=euclidean_block_size)
            euclidean_seconds = time.perf_counter() - euclidean_started
        target_components = np.asarray([component_by_index[index] for index in target_node_indices], dtype=np.int32)
        considered = count * max(0, count - 1)
        reachable_pairs, finite_distance_max = _distance_statistics(distance, max(dijkstra_batch_size, euclidean_block_size))
        return {'target_ids': np.asarray(target_ids, dtype=np.str_), 'target_node_ids': np.asarray(target_node_ids, dtype=np.str_), 'component_index': target_components, 'distance': distance.astype(dtype), 'euclidean': euclidean, 'stats': {'floor_id': floor_id, 'node_count': len(nodes), 'edge_count': len(edges), 'usable_edge_count': usable_edge_count, 'target_count': count, 'physical_component_count': int(component_count), 'csr_directed_entry_count': int(physical_csr.nnz), 'ordered_off_diagonal_pair_count': considered, 'reachable_ordered_pair_count': reachable_pairs, 'reachable_pair_rate': reachable_pairs / considered if considered else 1.0, 'finite_distance_max': finite_distance_max, 'backend': 'scipy.sparse.csgraph.dijkstra_on_symmetric_csr', 'dijkstra_batch_size': dijkstra_batch_size, 'euclidean_block_size': euclidean_block_size, 'timing': {'csr_and_components_seconds': preparation_seconds, 'batched_dijkstra_seconds': dijkstra_seconds, 'blocked_euclidean_seconds': euclidean_seconds}}}

    def build_physical_metric_closure(graph_path: Path | str, output_dir: Path | str, *, floors: Sequence[str] | None=None, config: ClosureBuildConfig | None=None) -> dict[str, Any]:
        """Build floor-isolated target Metric Closures and a provenance manifest."""
        cfg = config or ClosureBuildConfig()
        source = Path(graph_path).resolve()
        output = Path(output_dir).resolve()
        output.mkdir(parents=True, exist_ok=True)
        graph = _read_json(source)
        grouping_started = time.perf_counter()
        nodes_by_floor, edges_by_floor, cross_floor_or_invalid_edges = _group_graph_by_floor(graph)
        grouping_seconds = time.perf_counter() - grouping_started
        available_floors = sorted(nodes_by_floor)
        floor_filter = set(map(_text, floors or []))
        selected_floors = [floor for floor in available_floors if not floor_filter or floor in floor_filter]
        source_hash = _sha256(source)
        started = time.perf_counter()
        existing_manifest_path = output / 'manifest.json'
        floor_rows: dict[str, Any] = {}
        if cfg.resume and existing_manifest_path.exists():
            existing_manifest = _read_json(existing_manifest_path)
            if existing_manifest.get('source_graph_sha256') == source_hash:
                floor_rows.update(existing_manifest.get('floors') or {})
        for floor_id in selected_floors:
            floor_started = time.perf_counter()
            filename = f'floor_{_safe_floor_name(floor_id)}.npz'
            floor_path = output / filename
            metadata_path = output / f'floor_{_safe_floor_name(floor_id)}.json'
            if cfg.resume and floor_path.exists() and metadata_path.exists():
                existing = _read_json(metadata_path)
                if existing.get('source_graph_sha256') == source_hash and existing.get('config') == asdict(cfg):
                    floor_rows[floor_id] = {**existing, 'cache_hit': True}
                    continue
            nodes = nodes_by_floor.get(floor_id, [])
            edges = edges_by_floor.get(floor_id, [])
            closure = _build_floor_closure(floor_id, nodes, edges, dtype=cfg.dtype, include_euclidean=cfg.include_euclidean_baseline, dijkstra_batch_size=cfg.dijkstra_batch_size, euclidean_block_size=cfg.euclidean_block_size)
            np.savez_compressed(floor_path, target_ids=closure['target_ids'], target_node_ids=closure['target_node_ids'], component_index=closure['component_index'], distance=closure['distance'], euclidean=closure['euclidean'])
            metadata = {**closure['stats'], 'file': filename, 'source_graph': str(source), 'source_graph_sha256': source_hash, 'config': asdict(cfg), 'distance_semantics': 'undirected shortest-path sum of physical edge length', 'path_geometry_stored': False, 'elapsed_seconds': time.perf_counter() - floor_started, 'cache_hit': False}
            _write_json(metadata_path, metadata)
            floor_rows[floor_id] = metadata
        manifest = {'schema_version': SCHEMA_VERSION, 'closure_type': 'same_floor_true_physical_target_metric_closure', 'source_graph': str(source), 'source_graph_sha256': source_hash, 'config': asdict(cfg), 'floor_count': len(floor_rows), 'requested_floor_ids': selected_floors, 'graph_grouping': {'strategy': 'single_pass_nodes_then_single_pass_edges', 'elapsed_seconds': grouping_seconds, 'cross_floor_or_invalid_edge_count': cross_floor_or_invalid_edges}, 'floors': floor_rows, 'path_geometry_policy': 'costs only; rerun shortest path for final consecutive target pairs', 'elapsed_seconds': time.perf_counter() - started}
        manifest_path = _write_json(output / 'manifest.json', manifest)
        return {**manifest, 'manifest_path': str(manifest_path), 'output_dir': str(output)}

    class PhysicalMetricClosure:
        """Lazy floor-by-floor reader used by selection and route solvers."""

        def __init__(self, manifest_path: Path | str) -> None:
            self.manifest_path = Path(manifest_path).resolve()
            self.root = self.manifest_path.parent
            self.manifest = _read_json(self.manifest_path)
            self._floors: dict[str, dict[str, Any]] = {}

        def floor_ids(self) -> list[str]:
            return sorted((self.manifest.get('floors') or {}).keys())

        def _load(self, floor_id: str) -> dict[str, Any]:
            if floor_id in self._floors:
                return self._floors[floor_id]
            metadata = (self.manifest.get('floors') or {}).get(floor_id)
            if not metadata:
                raise KeyError(f'floor not present in physical closure: {floor_id}')
            with np.load(self.root / metadata['file'], allow_pickle=False) as payload:
                target_ids = payload['target_ids'].astype(str).tolist()
                data = {'target_ids': target_ids, 'target_node_ids': payload['target_node_ids'].astype(str).tolist(), 'component_index': payload['component_index'].astype(np.int32), 'distance': payload['distance'], 'euclidean': payload['euclidean'], 'index': {target_id: index for index, target_id in enumerate(target_ids)}}
            self._floors[floor_id] = data
            return data

        def has_target(self, floor_id: str, target_id: str) -> bool:
            return target_id in self._load(floor_id)['index']

        def target_node_id(self, floor_id: str, target_id: str) -> str:
            """Return the authoritative lower-graph node used by the closure.

            Keeping this mapping public prevents route expansion from silently
            switching back to a raster/grid access anchor after planning costs
            were computed on the physical navigation graph.
            """
            data = self._load(floor_id)
            index = data['index'].get(target_id)
            if index is None:
                return ''
            return str(data['target_node_ids'][index])

        def distance(self, floor_id: str, left: str, right: str) -> float:
            data = self._load(floor_id)
            left_index, right_index = (data['index'].get(left), data['index'].get(right))
            if left_index is None or right_index is None:
                return math.inf
            return float(data['distance'][left_index, right_index])

        def same_component(self, floor_id: str, left: str, right: str) -> bool:
            data = self._load(floor_id)
            left_index, right_index = (data['index'].get(left), data['index'].get(right))
            return left_index is not None and right_index is not None and (int(data['component_index'][left_index]) == int(data['component_index'][right_index]))
    __all__ = ['ClosureBuildConfig', 'PhysicalMetricClosure', 'build_physical_metric_closure']
    return dict(locals())

_s07_metric_closure = _register_embedded_module(
    'fire_inspection_system.semantic.physical_metric_closure',
    _build_s07_metric_closure(),
    aliases=(),
)

# === CONSOLIDATED PUBLIC API ===
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PhysicalPreparationResult:
    enabled: bool
    refined_graph: Path | None = None
    connector_portals: Path | None = None
    effective_free_areas: Path | None = None
    refinement: dict[str, Any] | None = None
    free_space_audit: dict[str, Any] | None = None
    standalone_metric_closure: dict[str, Any] | None = None
    elapsed_seconds: float = 0.0

    def to_summary(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "refined_graph": str(self.refined_graph) if self.refined_graph else "",
            "connector_portals": str(self.connector_portals) if self.connector_portals else "",
            "effective_free_areas": (
                str(self.effective_free_areas) if self.effective_free_areas else ""
            ),
            "refinement": self.refinement or {},
            "effective_free_space": self.free_space_audit or {},
            "standalone_metric_closure": self.standalone_metric_closure or {},
            "elapsed_seconds": self.elapsed_seconds,
        }


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _require(path: Path, description: str) -> Path:
    result = path.resolve()
    if not result.is_file() or result.stat().st_size <= 0:
        raise FileNotFoundError(f"{description}不存在或为空: {result}")
    return result


def run_stage(
    run_dir: Path,
    *,
    path_planning_enabled: bool,
    force_refinement: bool,
    rule_navigation_graph: Path | None = None,
    build_standalone_metric_closure: bool = False,
) -> PhysicalPreparationResult:
    started = time.perf_counter()
    run = run_dir.resolve()

    if not path_planning_enabled:
        closure: dict[str, Any] | None = None
        if build_standalone_metric_closure:
            if rule_navigation_graph is None:
                raise ValueError("预计算物理 Metric Closure 时缺少规则导航图")
            closure = _s07_metric_closure.build_physical_metric_closure(
                rule_navigation_graph,
                run / "physical_metric_closure",
            )
        return PhysicalPreparationResult(
            enabled=bool(closure),
            standalone_metric_closure=closure,
            elapsed_seconds=time.perf_counter() - started,
        )

    output = run / "area_graph_navigation_refined"
    graph_path = output / "refined_navigation_graph.json"
    connector_path = output / "connector_portals.geojson"
    summary_path = output / "acceptance_summary.json"
    if not force_refinement and graph_path.is_file() and connector_path.is_file():
        refinement = (
            _read_json(summary_path)
            if summary_path.is_file()
            else {
                "accepted": True,
                "reused": True,
                "outputs": {
                    "graph_json": str(graph_path),
                    "connector_geojson": str(connector_path),
                },
            }
        )
    else:
        refinement = _s07_connector.refine_run(run, output_dir=output)
        if not bool(refinement.get("accepted")):
            raise RuntimeError("Connector Portal 修正物理导航图未通过严格边验证")

    graph_path = _require(graph_path, "Connector Portal 修正物理导航图")
    connector_path = _require(connector_path, "Connector Portal 矢量文件")
    effective_free, free_space_audit = _s07_effective_free.build_effective_free_areas(
        run / "navigation_graph" / "inputs" / "free_areas.geojson",
        connector_path,
        run / "path_planning" / "precomputed" / "effective_free_areas.geojson",
    )
    return PhysicalPreparationResult(
        enabled=True,
        refined_graph=graph_path,
        connector_portals=connector_path,
        effective_free_areas=Path(effective_free).resolve(),
        refinement=refinement,
        free_space_audit=free_space_audit,
        elapsed_seconds=time.perf_counter() - started,
    )


__all__ = ["PhysicalPreparationResult", "run_stage"]
