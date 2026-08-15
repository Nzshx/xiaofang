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
from fire_inspection_system.stages import stage_07_connector_metric_closure as _stage07
from fire_inspection_system.stages import stage_08_semantic_rgcn as _stage08
from fire_inspection_system.stages import stage_09_dual_graph_planning as _stage09

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/last/io_utils.py
# -----------------------------------------------------------------------------
def _build_s10_io():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/last/io_utils.py'
    )
    __name__ = 'fire_inspection_system.last.io_utils'
    __package__ = 'fire_inspection_system.last'
    import json
    from pathlib import Path
    from typing import Any

    def read_json(path: Path | str) -> Any:
        resolved = Path(path).resolve()
        with resolved.open('r', encoding='utf-8') as handle:
            return json.load(handle)

    def write_json(path: Path | str, value: Any) -> Path:
        resolved = Path(path).resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with resolved.open('w', encoding='utf-8', newline='\n') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
        return resolved

    def text(value: Any) -> str:
        return '' if value is None else str(value).strip()

    def point_from_mapping(row: dict[str, Any]) -> tuple[float, float]:
        for key in ('access_point', 'physical_anchor_point', 'point'):
            value = row.get(key)
            if isinstance(value, (list, tuple)) and len(value) >= 2:
                return (float(value[0]), float(value[1]))
        if row.get('x') is not None and row.get('y') is not None:
            return (float(row['x']), float(row['y']))
        raise ValueError(f"target has no usable point: {row.get('target_id')}")
    return dict(locals())

_s10_io = _register_embedded_module(
    'fire_inspection_system.last.io_utils',
    _build_s10_io(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/last/vector_free_space.py
# -----------------------------------------------------------------------------
def _build_s10_vector_free():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/last/vector_free_space.py'
    )
    __name__ = 'fire_inspection_system.last.vector_free_space'
    __package__ = 'fire_inspection_system.last'
    """Exact vector free-space index used by physical-graph routing."""
    from pathlib import Path
    from typing import Any
    from shapely.geometry import shape
    from shapely.ops import unary_union
    from fire_inspection_system.last.io_utils import read_json, text

    def _polygonal(value: Any) -> Any:
        if value is None or value.is_empty:
            return value
        if not value.is_valid:
            value = value.buffer(0)
        polygons = []
        if value.geom_type == 'Polygon':
            polygons.append(value)
        elif value.geom_type == 'MultiPolygon':
            polygons.extend(value.geoms)
        elif hasattr(value, 'geoms'):
            for part in value.geoms:
                if part.geom_type == 'Polygon':
                    polygons.append(part)
                elif part.geom_type == 'MultiPolygon':
                    polygons.extend(part.geoms)
        return unary_union(polygons) if polygons else value

    class VectorFreeSpaceIndex:
        """Load CAD-derived polygons without constructing cells or raster masks."""

        def __init__(self, path: Path | str):
            self.path = Path(path).resolve()
            payload = read_json(self.path)
            grouped: dict[str, list[Any]] = {}
            for feature in payload.get('features', []) or []:
                floor_id = text((feature.get('properties') or {}).get('floor_id'))
                geometry = feature.get('geometry')
                if floor_id and geometry:
                    grouped.setdefault(floor_id, []).append(shape(geometry))
            self.floors = {floor_id: _polygonal(unary_union(values)) for floor_id, values in grouped.items()}
            if not self.floors:
                raise ValueError(f'no floor free-space geometry in {self.path}')

        def floor(self, floor_id: str) -> Any:
            if floor_id not in self.floors:
                raise KeyError(f'floor missing from vector free areas: {floor_id}')
            return self.floors[floor_id]
    __all__ = ['VectorFreeSpaceIndex']
    return dict(locals())

_s10_vector_free = _register_embedded_module(
    'fire_inspection_system.last.vector_free_space',
    _build_s10_vector_free(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/last/physical_graph.py
# -----------------------------------------------------------------------------
def _build_s10_physical_graph():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/last/physical_graph.py'
    )
    __name__ = 'fire_inspection_system.last.physical_graph'
    __package__ = 'fire_inspection_system.last'
    import heapq
    import math
    from dataclasses import dataclass
    from pathlib import Path
    from fire_inspection_system.last.io_utils import read_json, text

    @dataclass(frozen=True)
    class PhysicalNode:
        node_id: str
        floor_id: str
        x: float
        y: float
        kind: str

    @dataclass(frozen=True)
    class PhysicalEdge:
        edge_id: str
        floor_id: str
        node_a: str
        node_b: str
        length: float
        coordinates: tuple[tuple[float, float], ...]

    @dataclass(frozen=True)
    class OrientedEdge:
        edge: PhysicalEdge
        source: str
        target: str
        coordinates: tuple[tuple[float, float], ...]

    @dataclass(frozen=True)
    class PhysicalPath:
        node_ids: tuple[str, ...]
        edges: tuple[OrientedEdge, ...]
        distance: float

    class FloorPhysicalGraph:

        def __init__(self, floor_id: str, nodes: dict[str, PhysicalNode], edges: list[PhysicalEdge]):
            self.floor_id = floor_id
            self.nodes = nodes
            self.edges = {edge.edge_id: edge for edge in edges}
            self.adjacency: dict[str, list[tuple[str, float, str]]] = {node_id: [] for node_id in nodes}
            for edge in edges:
                if edge.node_a not in nodes or edge.node_b not in nodes:
                    continue
                self.adjacency[edge.node_a].append((edge.node_b, edge.length, edge.edge_id))
                self.adjacency[edge.node_b].append((edge.node_a, edge.length, edge.edge_id))

        def _oriented(self, edge_id: str, source: str, target: str) -> OrientedEdge:
            edge = self.edges[edge_id]
            coordinates = edge.coordinates
            if edge.node_a == target and edge.node_b == source:
                coordinates = tuple(reversed(coordinates))
            elif edge.node_a != source or edge.node_b != target:
                source_xy = (self.nodes[source].x, self.nodes[source].y)
                if coordinates and math.dist(coordinates[-1], source_xy) < math.dist(coordinates[0], source_xy):
                    coordinates = tuple(reversed(coordinates))
            return OrientedEdge(edge=edge, source=source, target=target, coordinates=coordinates)

        def shortest_path(self, source: str, target: str, *, banned_edge_ids: set[str] | None=None) -> PhysicalPath | None:
            if source not in self.nodes or target not in self.nodes:
                return None
            if source == target:
                return PhysicalPath((source,), (), 0.0)
            distances = {source: 0.0}
            previous: dict[str, tuple[str, str]] = {}
            queue: list[tuple[float, str]] = [(0.0, source)]
            settled: set[str] = set()
            while queue:
                distance, node_id = heapq.heappop(queue)
                if node_id in settled:
                    continue
                settled.add(node_id)
                if node_id == target:
                    break
                for neighbour, weight, edge_id in self.adjacency.get(node_id, []):
                    if banned_edge_ids and edge_id in banned_edge_ids:
                        continue
                    candidate = distance + weight
                    if candidate + 1e-09 < distances.get(neighbour, math.inf):
                        distances[neighbour] = candidate
                        previous[neighbour] = (node_id, edge_id)
                        heapq.heappush(queue, (candidate, neighbour))
            if target not in distances:
                return None
            reversed_steps: list[tuple[str, str, str]] = []
            cursor = target
            while cursor != source:
                parent, edge_id = previous[cursor]
                reversed_steps.append((parent, cursor, edge_id))
                cursor = parent
            steps = list(reversed(reversed_steps))
            nodes = [source]
            oriented = []
            for left, right, edge_id in steps:
                nodes.append(right)
                oriented.append(self._oriented(edge_id, left, right))
            return PhysicalPath(tuple(nodes), tuple(oriented), float(distances[target]))

    class RefinedPhysicalGraph:

        def __init__(self, path: Path | str):
            self.path = Path(path).resolve()
            payload = read_json(self.path)
            nodes_by_floor: dict[str, dict[str, PhysicalNode]] = {}
            for row in payload.get('nodes', []) or []:
                floor_id = text(row.get('floor_id'))
                node_id = text(row.get('node_id'))
                if not floor_id or not node_id:
                    continue
                nodes_by_floor.setdefault(floor_id, {})[node_id] = PhysicalNode(node_id=node_id, floor_id=floor_id, x=float(row.get('x') or 0.0), y=float(row.get('y') or 0.0), kind=text(row.get('kind')))
            edges_by_floor: dict[str, list[PhysicalEdge]] = {}
            for index, row in enumerate(payload.get('edges', []) or [], 1):
                floor_id = text(row.get('floor_id'))
                node_a = text(row.get('node_a'))
                node_b = text(row.get('node_b'))
                if not floor_id or not node_a or (not node_b):
                    continue
                geometry = row.get('geometry') or {}
                coordinates = tuple(((float(value[0]), float(value[1])) for value in geometry.get('coordinates', []) or [] if isinstance(value, (list, tuple)) and len(value) >= 2))
                floor_nodes = nodes_by_floor.get(floor_id, {})
                if len(coordinates) < 2 and node_a in floor_nodes and (node_b in floor_nodes):
                    coordinates = ((floor_nodes[node_a].x, floor_nodes[node_a].y), (floor_nodes[node_b].x, floor_nodes[node_b].y))
                length = float(row.get('length') or 0.0)
                if length <= 0.0 and len(coordinates) >= 2:
                    length = sum((math.dist(a, b) for a, b in zip(coordinates, coordinates[1:])))
                edges_by_floor.setdefault(floor_id, []).append(PhysicalEdge(edge_id=text(row.get('edge_id')) or f'EDGE_{index:08d}', floor_id=floor_id, node_a=node_a, node_b=node_b, length=length, coordinates=coordinates))
            self.floors = {floor_id: FloorPhysicalGraph(floor_id, nodes, edges_by_floor.get(floor_id, [])) for floor_id, nodes in nodes_by_floor.items()}

        def floor(self, floor_id: str) -> FloorPhysicalGraph:
            if floor_id not in self.floors:
                raise KeyError(f'floor missing from refined physical graph: {floor_id}')
            return self.floors[floor_id]
    __all__ = ['FloorPhysicalGraph', 'OrientedEdge', 'PhysicalEdge', 'PhysicalNode', 'PhysicalPath', 'RefinedPhysicalGraph']
    return dict(locals())

_s10_physical_graph = _register_embedded_module(
    'fire_inspection_system.last.physical_graph',
    _build_s10_physical_graph(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/last/control_plane.py
# -----------------------------------------------------------------------------
def _build_s10_control():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/last/control_plane.py'
    )
    __name__ = 'fire_inspection_system.last.control_plane'
    __package__ = 'fire_inspection_system.last'
    from pathlib import Path
    from typing import Any, Iterable
    from fire_inspection_system.last.io_utils import point_from_mapping, read_json, text, write_json

    def _candidate_index(bundle: dict[str, Any], floor_id: str) -> dict[str, dict[str, Any]]:
        floor = (bundle.get('floors') or {}).get(floor_id) or {}
        return {text(row.get('target_id')): dict(row) for row in floor.get('candidates', []) or [] if text(row.get('target_id'))}

    def _access_node_id(candidate: dict[str, Any]) -> str:
        target_id = text(candidate.get('target_id'))
        return text(candidate.get('source_access_node_id')) or text(candidate.get('access_node_id')) or f'TARGET::{target_id}'

    def build_locked_visit_plan(optimized_order_path: Path | str, candidates_path: Path | str, output_path: Path | str | None=None, floors: Iterable[str] | None=None) -> dict[str, Any]:
        """Canonicalize an existing control decision without changing it.

        Target selection, target instances, access anchors, visit order and the
        control-plane cost are copied verbatim into a locked VisitPlan.  The
        forwarding plane is not allowed to mutate any of them.
        """
        optimized_path = Path(optimized_order_path).resolve()
        candidate_file = Path(candidates_path).resolve()
        optimized = read_json(optimized_path)
        candidates = read_json(candidate_file)
        requested = {text(value) for value in floors or [] if text(value)}
        result_floors: dict[str, Any] = {}
        for floor_id, route in sorted((optimized.get('floors') or {}).items()):
            floor_id = text(floor_id)
            physical_floor_id = text(route.get('physical_floor_id')) or floor_id.split('__', 1)[0]
            primary_scope_id = text(route.get('physical_floor_primary_scope_id')) or floor_id
            if requested and floor_id not in requested:
                continue
            if not bool(route.get('feasible')):
                continue
            route_segments = []
            segmented_order: list[str] = []
            visit_segment_ids: list[str] = []
            for segment_index, raw_segment in enumerate(route.get('route_segments', []) or [], 1):
                segment_id = text(raw_segment.get('segment_id')) or f'{floor_id}_ROUTE_{segment_index:03d}'
                segment_order = [text(value) for value in raw_segment.get('ordered_target_ids', []) or [] if text(value)]
                if not segment_order:
                    continue
                entry_target_id = text(raw_segment.get('entry_target_id')) or segment_order[0]
                if entry_target_id != segment_order[0]:
                    raise ValueError(
                        f'route segment entry target is not the first control target on {floor_id}: '
                        f'{entry_target_id} != {segment_order[0]}'
                    )
                route_start_constraint_applies = bool(raw_segment.get('route_start_constraint_applies'))
                route_segments.append({'segment_id': segment_id, 'segment_index': len(route_segments) + 1, 'floor_id': floor_id, 'physical_floor_id': physical_floor_id, 'building_scope_id': floor_id, 'physical_floor_primary_scope_id': primary_scope_id, 'physical_floor_primary_route': bool(route.get('physical_floor_primary_route')), 'entry_mode': text(raw_segment.get('entry_mode')), 'entry_label': text(raw_segment.get('entry_label')), 'virtual_entry_id': text(raw_segment.get('virtual_entry_id')), 'virtual_continuation': bool(raw_segment.get('virtual_continuation')), 'virtual_entry_is_not_physical_path': bool(raw_segment.get('virtual_entry_is_not_physical_path')), 'continuation_break_reason': text(raw_segment.get('continuation_break_reason')), 'physically_reachable_from_floor_route_start': raw_segment.get('physically_reachable_from_floor_route_start'), 'route_start_constraint_applies': route_start_constraint_applies, 'entry_target_id': entry_target_id, 'entry_target_class_name': text(raw_segment.get('entry_target_class_name')), 'route_start_constraint_satisfied': bool(raw_segment.get('route_start_constraint_satisfied')) if route_start_constraint_applies else None, 'physical_component_id': raw_segment.get('physical_component_id'), 'ordered_target_ids': segment_order})
                segmented_order.extend(segment_order)
                visit_segment_ids.extend([segment_id] * len(segment_order))
            order = [text(value) for value in route.get('order', []) or [] if text(value)]
            if segmented_order:
                if order and order != segmented_order:
                    raise ValueError(f'route segments do not match the control order on {floor_id}')
                order = segmented_order
            if not order:
                order = [text(value) for value in route.get('selected_target_ids', []) or [] if text(value)]
            if not order:
                continue
            if not route_segments:
                segment_id = f'{floor_id}_ROUTE_001'
                route_segments = [{'segment_id': segment_id, 'segment_index': 1, 'entry_mode': 'legacy_single_open_route', 'entry_label': '旧版单段路线（未提供入口约束审计）', 'virtual_entry_id': '', 'virtual_continuation': False, 'virtual_entry_is_not_physical_path': False, 'continuation_break_reason': '', 'physically_reachable_from_floor_route_start': None, 'route_start_constraint_applies': False, 'entry_target_id': order[0], 'entry_target_class_name': '', 'route_start_constraint_satisfied': None, 'physical_component_id': route.get('component_id'), 'ordered_target_ids': list(order)}]
                visit_segment_ids = [segment_id] * len(order)
            by_id = _candidate_index(candidates, floor_id)
            segment_by_id = {row['segment_id']: row for row in route_segments}
            missing = [target_id for target_id in order if target_id not in by_id]
            if missing:
                raise KeyError(f'control targets missing from candidate bundle on {floor_id}: {missing[:10]}')
            locked_targets = []
            segment_visit_ordinals: dict[str, int] = {}
            for visit_order, (target_id, segment_id) in enumerate(zip(order, visit_segment_ids), 1):
                candidate = by_id[target_id]
                segment = segment_by_id[segment_id]
                segment_visit_ordinals[segment_id] = segment_visit_ordinals.get(segment_id, 0) + 1
                x, y = point_from_mapping(candidate)
                annotation = candidate.get('annotation') or {}
                raw_bbox = annotation.get('bbox') if isinstance(annotation, dict) else None
                bbox = [float(value) for value in raw_bbox[:4]] if isinstance(raw_bbox, (list, tuple)) and len(raw_bbox) >= 4 else []
                graph_access_node_id = _access_node_id(candidate)
                virtual_access = bool(candidate.get('virtual_access'))
                locked_targets.append({'target_id': target_id, 'visit_order': visit_order, 'route_segment_id': segment_id, 'segment_visit_ordinal': segment_visit_ordinals[segment_id], 'is_route_segment_entry': segment_visit_ordinals[segment_id] == 1, 'segment_entry_mode': segment.get('entry_mode'), 'segment_entry_label': segment.get('entry_label'), 'segment_virtual_continuation': bool(segment.get('virtual_continuation')), 'segment_virtual_entry_is_not_physical_path': bool(segment.get('virtual_entry_is_not_physical_path')), 'segment_continuation_break_reason': segment.get('continuation_break_reason'), 'floor_id': floor_id, 'physical_floor_id': physical_floor_id, 'building_scope_id': floor_id, 'physical_floor_primary_scope_id': primary_scope_id, 'access_node_id': graph_access_node_id, 'graph_access_node_id': graph_access_node_id, 'access_point': [x, y], 'raw_point': list(candidate.get('point') or [x, y]), 'object_bbox': bbox, 'object_id': text(candidate.get('object_id') or candidate.get('source_object_id')), 'class_name': text(candidate.get('class_name') or candidate.get('target_class')), 'raw_name': text(candidate.get('raw_name') or (candidate.get('annotation') or {}).get('original_object_name')), 'mandatory': bool(candidate.get('mandatory')), 'virtual_access': virtual_access, 'virtual_access_distance': float(candidate.get('virtual_access_distance') or 0.0), 'selection_basis': text(candidate.get('selection_basis')), 'matched_rule_ids': list(candidate.get('matched_rule_ids') or [])})
            unique_selected = [target_id for index, target_id in enumerate(order) if target_id not in order[:index]]
            result_floors[floor_id] = {'floor_id': floor_id, 'physical_floor_id': physical_floor_id, 'building_scope_id': floor_id, 'physical_floor_primary_scope_id': primary_scope_id, 'physical_floor_primary_route': bool(route.get('physical_floor_primary_route')), 'order_locked': True, 'selected_target_ids': unique_selected, 'ordered_target_ids': list(order), 'selected_target_count': len(unique_selected), 'target_visit_count': len(order), 'repeated_target_visit_count': len(order) - len(unique_selected), 'route_segment_count': len(route_segments), 'route_segments': route_segments, 'control_status': text(route.get('status')), 'control_feasible': bool(route.get('feasible')), 'control_solver': text(route.get('solver')), 'control_planned_cost': float(route.get('length') or 0.0), 'targets': locked_targets}
        if requested - set(result_floors):
            raise KeyError(f'requested floors missing from control order: {sorted(requested - set(result_floors))}')
        payload = {'schema_version': 1, 'artifact_type': 'locked_control_visit_plan', 'order_locked': True, 'cost_recalculation_allowed': False, 'building_scopes_planned_independently': True, 'cross_building_physical_edges_allowed': False, 'physical_floor_primary_scopes': dict(optimized.get('physical_floor_primary_scopes') or {}), 'source_optimized_order': str(optimized_path), 'source_candidates': str(candidate_file), 'floors': result_floors}
        if output_path is not None:
            write_json(output_path, payload)
        return payload
    __all__ = ['build_locked_visit_plan']
    return dict(locals())

_s10_control = _register_embedded_module(
    'fire_inspection_system.last.control_plane',
    _build_s10_control(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/last/forwarding_plane.py
# -----------------------------------------------------------------------------
def _build_s10_forwarding():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/last/forwarding_plane.py'
    )
    __name__ = 'fire_inspection_system.last.forwarding_plane'
    __package__ = 'fire_inspection_system.last'
    from collections import Counter, defaultdict
    from dataclasses import dataclass
    from typing import Any, Iterable
    from shapely.geometry import LineString
    from shapely.prepared import prep
    from fire_inspection_system.last.io_utils import text
    from fire_inspection_system.last.physical_graph import FloorPhysicalGraph, PhysicalPath, RefinedPhysicalGraph
    from fire_inspection_system.last.vector_free_space import VectorFreeSpaceIndex

    @dataclass(frozen=True)
    class CandidateTraversal:
        edge_key: str
        source_node: str
        target_node: str
        coordinates: tuple[tuple[float, float], ...]
        backend: str
        source_edge_id: str

        @property
        def length(self) -> float:
            return float(LineString(self.coordinates).length) if len(self.coordinates) >= 2 else 0.0

    class SupportTopology:
        """Audit the unique support graph of a temporal physical-graph walk.

        A route is a walk, not a simple path or a bounded-degree tree.  Junctions
        and repeated traversals are therefore recorded but never rejected merely
        because their accumulated support graph has a high degree.
        """

        def __init__(self) -> None:
            self.edge_endpoints: dict[str, tuple[str, str]] = {}
            self.neighbours: dict[str, set[str]] = defaultdict(set)
            self.predecessors: dict[str, set[str]] = defaultdict(set)
            self.successors: dict[str, set[str]] = defaultdict(set)
            self.node_coordinates: dict[str, tuple[float, float]] = {}

        def _simulated(self, traversals: Iterable[CandidateTraversal]) -> tuple[dict[str, tuple[str, str]], dict[str, set[str]], dict[str, set[str]], dict[str, set[str]]]:
            edges = dict(self.edge_endpoints)
            neighbours = {key: set(value) for key, value in self.neighbours.items()}
            predecessors = {key: set(value) for key, value in self.predecessors.items()}
            successors = {key: set(value) for key, value in self.successors.items()}
            for traversal in traversals:
                if traversal.source_node == traversal.target_node or traversal.edge_key in edges:
                    continue
                left, right = (traversal.source_node, traversal.target_node)
                edges[traversal.edge_key] = (left, right)
                neighbours.setdefault(left, set()).add(right)
                neighbours.setdefault(right, set()).add(left)
                successors.setdefault(left, set()).add(right)
                predecessors.setdefault(right, set()).add(left)
            return (edges, neighbours, predecessors, successors)

        @staticmethod
        def _audit(neighbours: dict[str, set[str]], predecessors: dict[str, set[str]], successors: dict[str, set[str]]) -> dict[str, Any]:
            nodes = set(neighbours) | set(predecessors) | set(successors)
            return {'node_count': len(nodes), 'edge_count': sum((len(value) for value in neighbours.values())) // 2, 'max_support_degree': max((len(neighbours.get(node, set())) for node in nodes), default=0), 'max_structural_in_degree': max((len(predecessors.get(node, set())) for node in nodes), default=0), 'max_structural_out_degree': max((len(successors.get(node, set())) for node in nodes), default=0), 'degree_constraints_enabled': False, 'support_degree_over_3_nodes': [], 'structural_in_degree_over_1_nodes': [], 'structural_out_degree_over_2_nodes': [], 'valid': True}

        def commit(self, traversals: Iterable[CandidateTraversal]) -> dict[str, Any]:
            traversals = list(traversals)
            edges, neighbours, predecessors, successors = self._simulated(traversals)
            audit = self._audit(neighbours, predecessors, successors)
            self.edge_endpoints = edges
            self.neighbours = defaultdict(set, neighbours)
            self.predecessors = defaultdict(set, predecessors)
            self.successors = defaultdict(set, successors)
            for traversal in traversals:
                if traversal.coordinates:
                    self.node_coordinates.setdefault(traversal.source_node, traversal.coordinates[0])
                    self.node_coordinates.setdefault(traversal.target_node, traversal.coordinates[-1])
            return audit

        def audit(self) -> dict[str, Any]:
            return self._audit(self.neighbours, self.predecessors, self.successors)

    def _graph_traversals(path: PhysicalPath) -> list[CandidateTraversal]:
        return [CandidateTraversal(edge_key=f'GRAPH_EDGE::{oriented.edge.edge_id}', source_node=oriented.source, target_node=oriented.target, coordinates=oriented.coordinates, backend='refined_physical_graph', source_edge_id=oriented.edge.edge_id) for oriented in path.edges]

    def _path_vector_valid(traversals: Iterable[CandidateTraversal], polygon: Any) -> bool:
        prepared = prep(polygon)
        return all((len(row.coordinates) >= 2 and prepared.covers(LineString(row.coordinates)) for row in traversals))

    def _target_map(floor_plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {text(row.get('target_id')): row for row in floor_plan.get('targets', []) or [] if text(row.get('target_id'))}

    def _topology_aware_graph_path(graph: FloorPhysicalGraph, source_graph_node: str, target_graph_node: str, source_support_node: str, target_support_node: str, source_point: tuple[float, float], target_point: tuple[float, float], polygon: Any) -> tuple[list[CandidateTraversal], str, dict[str, Any]]:
        """Return a vector-certified physical-graph path between locked targets.

        Invalid physical edges are lazily excluded and Dijkstra is retried.  No
        raster or off-graph replacement is permitted.
        """
        banned: set[str] = set()
        attempts: list[dict[str, Any]] = []
        if source_graph_node not in graph.nodes or target_graph_node not in graph.nodes:
            return ([], 'physical_graph_disconnected_or_missing_access', {'attempts': attempts, 'banned_edge_ids': []})
        while True:
            path = graph.shortest_path(source_graph_node, target_graph_node, banned_edge_ids=banned)
            if path is None:
                return ([], 'physical_graph_disconnected_or_missing_access', {'attempts': attempts, 'banned_edge_ids': sorted(banned)})
            traversals = _graph_traversals(path)
            source_graph_point = (graph.nodes[source_graph_node].x, graph.nodes[source_graph_node].y)
            target_graph_point = (graph.nodes[target_graph_node].x, graph.nodes[target_graph_node].y)
            if source_support_node != source_graph_node:
                traversals.insert(0, CandidateTraversal(edge_key=f'ACCESS_EDGE::{source_support_node}::{source_graph_node}', source_node=source_support_node, target_node=source_graph_node, coordinates=(source_point, source_graph_point), backend='refined_physical_graph_access', source_edge_id=''))
            if target_support_node != target_graph_node:
                traversals.append(CandidateTraversal(edge_key=f'ACCESS_EDGE::{target_graph_node}::{target_support_node}', source_node=target_graph_node, target_node=target_support_node, coordinates=(target_graph_point, target_point), backend='refined_physical_graph_access', source_edge_id=''))
            if not traversals and source_support_node != target_support_node:
                return ([], 'physical_graph_path_has_no_edges', {'attempts': attempts, 'banned_edge_ids': sorted(banned)})
            if not _path_vector_valid(traversals, polygon):
                invalid = next((row for row in traversals if not prep(polygon).covers(LineString(row.coordinates))), None)
                if invalid is None or not invalid.source_edge_id:
                    return ([], 'physical_graph_geometry_failed_vector_review', {'attempts': attempts, 'banned_edge_ids': sorted(banned)})
                banned.add(invalid.source_edge_id)
                attempts.append({'reason': 'vector_invalid', 'banned_edge_id': invalid.source_edge_id})
                continue
            return (traversals, '', {'attempts': attempts, 'banned_edge_ids': sorted(banned), 'routing_policy': 'physical_graph_only_vector_certified_walk'})

    def forward_locked_floor_plan(floor_plan: dict[str, Any], physical_graph: FloorPhysicalGraph, free_spaces: VectorFreeSpaceIndex, cached_routes: dict[str, Any] | None=None) -> dict[str, Any]:
        floor_id = text(floor_plan.get('floor_id'))
        target_by_id = _target_map(floor_plan)
        order = [text(value) for value in floor_plan.get('ordered_target_ids', []) or [] if text(value)]
        if order != [text(row.get('target_id')) for row in floor_plan.get('targets', []) or []]:
            raise ValueError(f'locked target rows do not match the control order on {floor_id}')
        route_segments = list(floor_plan.get('route_segments', []) or [])
        if not route_segments:
            route_segments = [{'segment_id': f'{floor_id}_ROUTE_001', 'ordered_target_ids': list(order)}]
        segmented_order = [text(target_id) for segment in route_segments for target_id in segment.get('ordered_target_ids', []) or [] if text(target_id)]
        if segmented_order != order:
            raise ValueError(f'locked route segments do not match the control order on {floor_id}')
        polygon = free_spaces.floor(floor_id)
        topology = SupportTopology()
        edge_pass_counts: Counter[str] = Counter()
        node_visit_counts: Counter[str] = Counter()
        traversal_events: list[dict[str, Any]] = []
        node_visit_events: list[dict[str, Any]] = []
        target_visit_events: list[dict[str, Any]] = []
        legs: list[dict[str, Any]] = []
        actual_length = 0.0
        failure_count = 0
        graph_leg_count = local_leg_count = repeated_edge_traversal_count = 0

        def add_node_event(node_id: str, point: tuple[float, float], leg_index: int) -> str:
            node_visit_counts[node_id] += 1
            event_id = f'{floor_id}_NODE_EVENT_{len(node_visit_events) + 1:07d}'
            node_visit_events.append({'event_id': event_id, 'sequence_no': len(node_visit_events) + 1, 'physical_node_id': node_id, 'visit_ordinal_for_node': node_visit_counts[node_id], 'leg_index': leg_index, 'point': [float(point[0]), float(point[1])], 'is_repeated_visit': node_visit_counts[node_id] > 1})
            return event_id
        leg_work: list[tuple[str, str, str, int, int, bool]] = []
        singleton_starts: list[tuple[str, str, int]] = []
        offset = 0
        for segment in route_segments:
            segment_id = text(segment.get('segment_id'))
            segment_order = [text(value) for value in segment.get('ordered_target_ids', []) or []]
            if len(segment_order) == 1:
                singleton_starts.append((segment_id, segment_order[0], offset + 1))
            for local_index, (left_id, right_id) in enumerate(zip(segment_order, segment_order[1:]), 1):
                leg_work.append((segment_id, left_id, right_id, offset + local_index + 1, offset + 1, local_index == 1))
            offset += len(segment_order)
        for leg_index, (route_segment_id, left_id, right_id, right_visit_order, segment_start_visit_order, is_segment_first_leg) in enumerate(leg_work, 1):
            left = target_by_id[left_id]
            right = target_by_id[right_id]
            if is_segment_first_leg:
                first_point = tuple(map(float, left['access_point'][:2]))
                first_node = text(left.get('access_node_id'))
                first_event = add_node_event(first_node, first_point, leg_index - 1)
                target_visit_events.append({'target_id': left_id, 'visit_order': segment_start_visit_order, 'route_segment_id': route_segment_id, 'node_event_id': first_event, 'access_node_id': first_node, 'access_point': list(first_point)})
            source_node = text(left.get('access_node_id'))
            target_node = text(right.get('access_node_id'))
            source_graph_node = text(left.get('graph_access_node_id') or source_node)
            target_graph_node = text(right.get('graph_access_node_id') or target_node)
            source_point = tuple(map(float, left['access_point'][:2]))
            target_point = tuple(map(float, right['access_point'][:2]))
            if source_graph_node in physical_graph.nodes:
                graph_point = (physical_graph.nodes[source_graph_node].x, physical_graph.nodes[source_graph_node].y)
                if source_node == source_graph_node and source_point != graph_point:
                    source_node = f'ACCESS::{left_id}'
            if target_graph_node in physical_graph.nodes:
                graph_point = (physical_graph.nodes[target_graph_node].x, physical_graph.nodes[target_graph_node].y)
                if target_node == target_graph_node and target_point != graph_point:
                    target_node = f'ACCESS::{right_id}'
            fallback_reason = ''
            backend = ''
            selected: list[CandidateTraversal] = []
            topology_before = topology.audit()
            graph_candidate, fallback_reason, graph_search_audit = _topology_aware_graph_path(physical_graph, source_graph_node, target_graph_node, source_node, target_node, source_point, target_point, polygon)
            if graph_candidate:
                selected = graph_candidate
                backend = 'refined_physical_graph'
            local_search_audit: dict[str, Any] = {'enabled': False, 'reason': 'off_graph_and_raster_fallbacks_are_forbidden'}
            if not selected:
                failure_count += 1
                legs.append({'leg_index': leg_index, 'route_segment_id': route_segment_id, 'from_target_id': left_id, 'to_target_id': right_id, 'reachable': False, 'backend': backend or 'none', 'fallback_reason': fallback_reason, 'graph_search_audit': graph_search_audit, 'local_search_audit': local_search_audit, 'control_order_changed': False, 'control_cost_recalculated': False})
                break
            topology_after = topology.commit(selected)
            if backend == 'refined_physical_graph':
                graph_leg_count += 1
            else:
                local_leg_count += 1
            leg_length = 0.0
            leg_event_ids = []
            for traversal in selected:
                edge_pass_counts[traversal.edge_key] += 1
                pass_index = edge_pass_counts[traversal.edge_key]
                repeated = pass_index > 1
                repeated_edge_traversal_count += int(repeated)
                event_id = f'{floor_id}_EDGE_EVENT_{len(traversal_events) + 1:07d}'
                length = traversal.length
                leg_length += length
                traversal_events.append({'event_id': event_id, 'sequence_no': len(traversal_events) + 1, 'leg_index': leg_index, 'route_segment_id': route_segment_id, 'from_target_id': left_id, 'to_target_id': right_id, 'physical_edge_key': traversal.edge_key, 'source_edge_id': traversal.source_edge_id, 'source_node_id': traversal.source_node, 'target_node_id': traversal.target_node, 'pass_index': pass_index, 'is_repeated_traversal': repeated, 'direction': f'{traversal.source_node}->{traversal.target_node}', 'backend': traversal.backend, 'distance': length, 'geometry': {'type': 'LineString', 'coordinates': [list(point) for point in traversal.coordinates]}})
                leg_event_ids.append(event_id)
                point = traversal.coordinates[-1]
                add_node_event(traversal.target_node, point, leg_index)
            actual_length += leg_length
            right_event = node_visit_events[-1]['event_id']
            target_visit_events.append({'target_id': right_id, 'visit_order': right_visit_order, 'route_segment_id': route_segment_id, 'node_event_id': right_event, 'access_node_id': target_node, 'access_point': list(target_point)})
            legs.append({'leg_index': leg_index, 'route_segment_id': route_segment_id, 'from_target_id': left_id, 'to_target_id': right_id, 'reachable': True, 'backend': backend, 'fallback_reason': fallback_reason, 'distance': leg_length, 'traversal_event_ids': leg_event_ids, 'local_search_audit': local_search_audit, 'graph_search_audit': graph_search_audit, 'topology_before': topology_before, 'topology_after': topology_after, 'control_order_changed': False, 'control_cost_recalculated': False})
        for route_segment_id, target_id, visit_order in singleton_starts:
            target = target_by_id[target_id]
            point = tuple(map(float, target['access_point'][:2]))
            node_id = text(target.get('access_node_id'))
            event_id = add_node_event(node_id, point, 0)
            target_visit_events.append({'target_id': target_id, 'visit_order': visit_order, 'route_segment_id': route_segment_id, 'node_event_id': event_id, 'access_node_id': node_id, 'access_point': list(point)})
        target_visit_events.sort(key=lambda row: int(row.get('visit_order') or 0))
        final_audit = topology.audit()
        repeated_nodes = sorted(({'physical_node_id': node_id, 'visit_count': count, 'event_sequence': [row['event_id'] for row in node_visit_events if row['physical_node_id'] == node_id]} for node_id, count in node_visit_counts.items() if count > 1), key=lambda row: row['physical_node_id'])
        repeated_edges = sorted(({'physical_edge_key': edge_key, 'traversal_count': count, 'event_sequence': [row['event_id'] for row in traversal_events if row['physical_edge_key'] == edge_key]} for edge_key, count in edge_pass_counts.items() if count > 1), key=lambda row: row['physical_edge_key'])
        expected_leg_count = len(leg_work)
        feasible = bool(floor_plan.get('control_feasible', True)) and failure_count == 0 and (len(legs) == expected_leg_count) and final_audit['valid']
        virtual_continuation_count = sum(bool(row.get('virtual_entry_id')) for row in route_segments)
        return {'floor_id': floor_id, 'status': 'feasible' if feasible else 'infeasible_forwarding', 'feasible': feasible, 'order_locked': True, 'control_order_changed': False, 'control_cost_recalculated': False, 'ordered_target_ids': order, 'route_segments': route_segments, 'control_planned_cost': float(floor_plan.get('control_planned_cost') or 0.0), 'forwarding_actual_geometry_length': actual_length, 'target_visit_events': target_visit_events, 'legs': legs, 'traversal_events': traversal_events, 'node_visit_events': node_visit_events, 'repeated_nodes': repeated_nodes, 'repeated_edges': repeated_edges, 'support_topology': final_audit, 'counts': {'target_count': len(set(order)), 'target_visit_count': len(order), 'repeated_target_visit_count': len(order) - len(set(order)), 'route_segment_count': len(route_segments), 'virtual_entry_count': virtual_continuation_count, 'virtual_continuation_count': virtual_continuation_count, 'expected_leg_count': expected_leg_count, 'completed_leg_count': sum((bool(row.get('reachable')) for row in legs)), 'failed_leg_count': failure_count, 'graph_leg_count': graph_leg_count, 'local_raster_leg_count': local_leg_count, 'node_visit_event_count': len(node_visit_events), 'repeated_physical_node_count': len(repeated_nodes), 'edge_traversal_event_count': len(traversal_events), 'repeated_physical_edge_count': len(repeated_edges), 'repeated_edge_traversal_count': repeated_edge_traversal_count}}

    def forward_locked_visit_plan(visit_plan: dict[str, Any], refined_graph: RefinedPhysicalGraph, free_spaces: VectorFreeSpaceIndex, cached_routes: dict[str, Any] | None=None) -> dict[str, Any]:
        floors = {floor_id: forward_locked_floor_plan(floor_plan, refined_graph.floor(floor_id), free_spaces, cached_routes=cached_routes) for floor_id, floor_plan in sorted((visit_plan.get('floors') or {}).items())}
        return {'schema_version': 1, 'artifact_type': 'locked_order_temporal_forwarding_route', 'policy': {'primary_forwarding_plane': 'refined_physical_navigation_graph', 'fallback': 'none', 'route_type': 'physical_graph_walk', 'target_order_locked': True, 'control_cost_recalculated': False, 'physical_floor_first_route_segment_requires_legal_inspection_entry': True, 'every_building_requires_own_legal_entry': False, 'building_scopes_planned_independently': True, 'later_disconnected_building_scopes_and_segments_use_virtual_continuation': True, 'virtual_continuations_are_not_physical_paths': True, 'cross_building_physical_edges_allowed': False, 'cross_segment_physical_jumps_allowed': False, 'support_degree_limit': None, 'structural_in_degree_limit': None, 'structural_out_degree_limit': None, 'repeated_physical_nodes_and_edges_allowed': True, 'repeated_traversals_have_temporal_event_order': True}, 'floors': floors, 'counts': {'floor_count': len(floors), 'feasible_floor_count': sum((bool(row.get('feasible')) for row in floors.values())), 'target_count': sum((int(row['counts']['target_count']) for row in floors.values())), 'target_visit_count': sum((int(row['counts']['target_visit_count']) for row in floors.values())), 'repeated_target_visit_count': sum((int(row['counts']['repeated_target_visit_count']) for row in floors.values())), 'route_segment_count': sum((int(row['counts']['route_segment_count']) for row in floors.values())), 'virtual_entry_count': sum((int(row['counts']['virtual_entry_count']) for row in floors.values())), 'virtual_continuation_count': sum((int(row['counts']['virtual_continuation_count']) for row in floors.values())), 'graph_leg_count': sum((int(row['counts']['graph_leg_count']) for row in floors.values())), 'local_raster_leg_count': sum((int(row['counts']['local_raster_leg_count']) for row in floors.values())), 'repeated_physical_node_count': sum((int(row['counts']['repeated_physical_node_count']) for row in floors.values())), 'repeated_physical_edge_count': sum((int(row['counts']['repeated_physical_edge_count']) for row in floors.values()))}}
    __all__ = ['CandidateTraversal', 'SupportTopology', 'forward_locked_floor_plan', 'forward_locked_visit_plan']
    return dict(locals())

_s10_forwarding = _register_embedded_module(
    'fire_inspection_system.last.forwarding_plane',
    _build_s10_forwarding(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/last/acceptance_report.py
# -----------------------------------------------------------------------------
def _build_s10_acceptance():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/last/acceptance_report.py'
    )
    __name__ = 'fire_inspection_system.last.acceptance_report'
    __package__ = 'fire_inspection_system.last'
    from pathlib import Path
    from typing import Any
    from fire_inspection_system.last.io_utils import read_json, text, write_json

    def _failure_explanation(reason: str) -> str:
        if 'disconnected_or_missing_access' in reason:
            return '精细物理图断开或访问锚点缺失；纯物理图策略禁止生成图外补路'
        if 'geometry_failed_vector_review' in reason:
            return '物理图候选路径未通过有效自由空间矢量复核；为避免穿墙已拒绝该路段'
        if 'path_has_no_edges' in reason:
            return '两个访问锚点之间没有可展开的物理图边'
        return '转发层未找到同时满足物理图可达性和矢量不穿墙要求的路径'

    def write_acceptance_reports(output_dir: Path | str, visit_plan: dict[str, Any], forwarding: dict[str, Any]) -> dict[str, str]:
        output = Path(output_dir).resolve()
        floor_reports: dict[str, Any] = {}
        for floor_id, route in sorted((forwarding.get('floors') or {}).items()):
            plan = (visit_plan.get('floors') or {}).get(floor_id) or {}
            by_id = {text(row.get('target_id')): row for row in plan.get('targets', []) or [] if text(row.get('target_id'))}
            failures = []
            for leg in route.get('legs', []) or []:
                if leg.get('reachable') is not False:
                    continue
                left_id = text(leg.get('from_target_id'))
                right_id = text(leg.get('to_target_id'))
                left, right = (by_id.get(left_id, {}), by_id.get(right_id, {}))
                reason = text(leg.get('fallback_reason')) or 'unreachable'
                failures.append({'leg_index': int(leg.get('leg_index') or 0), 'from_target_id': left_id, 'from_visit_order': int(left.get('visit_order') or 0), 'from_class_name': text(left.get('class_name')), 'from_raw_name': text(left.get('raw_name')), 'from_access_point': list(left.get('access_point') or []), 'to_target_id': right_id, 'to_visit_order': int(right.get('visit_order') or 0), 'to_class_name': text(right.get('class_name')), 'to_raw_name': text(right.get('raw_name')), 'to_access_point': list(right.get('access_point') or []), 'reason_code': reason, 'explanation': _failure_explanation(reason), 'control_order_changed': bool(leg.get('control_order_changed')), 'control_cost_recalculated': bool(leg.get('control_cost_recalculated'))})
            counts = route.get('counts') or {}
            topology = route.get('support_topology') or {}
            route_segments = list(route.get('route_segments', []) or [])
            virtual_continuations = [row for row in route_segments if row.get('virtual_continuation')]
            floor_reports[floor_id] = {'acceptance': 'passed' if route.get('feasible') else 'failed_with_diagnostics', 'status': route.get('status'), 'target_count': int(counts.get('target_count') or 0), 'expected_leg_count': int(counts.get('expected_leg_count') or 0), 'completed_leg_count': int(counts.get('completed_leg_count') or 0), 'failed_leg_count': int(counts.get('failed_leg_count') or 0), 'graph_leg_count': int(counts.get('graph_leg_count') or 0), 'local_raster_leg_count': int(counts.get('local_raster_leg_count') or 0), 'virtual_continuation_count': len(virtual_continuations), 'virtual_continuations': virtual_continuations, 'virtual_continuations_are_not_physical_paths': True, 'repeated_physical_node_count': int(counts.get('repeated_physical_node_count') or 0), 'repeated_physical_edge_count': int(counts.get('repeated_physical_edge_count') or 0), 'support_topology': topology, 'failed_legs': failures}
        optimized_path = text(visit_plan.get('source_optimized_order'))
        if optimized_path and Path(optimized_path).exists():
            optimized = read_json(optimized_path)
            for floor_id, route in sorted((optimized.get('floors') or {}).items()):
                if floor_id in floor_reports:
                    continue
                floor_reports[floor_id] = {'acceptance': 'control_failed', 'status': route.get('status'), 'target_count': 0, 'expected_leg_count': 0, 'completed_leg_count': 0, 'failed_leg_count': 0, 'graph_leg_count': 0, 'local_raster_leg_count': 0, 'repeated_physical_node_count': 0, 'repeated_physical_edge_count': 0, 'support_topology': {}, 'control_failure_reason': text(route.get('reason')) or 'control order unavailable', 'failed_legs': []}
        passed = [key for key, row in floor_reports.items() if row['acceptance'] == 'passed']
        failed = [key for key, row in floor_reports.items() if row['acceptance'] != 'passed']
        payload = {'schema_version': 1, 'artifact_type': 'last_route_acceptance_report', 'overall_acceptance': 'passed' if not failed else 'partial_acceptance_with_failed_floor_reports', 'control_visit_order_preserved': True, 'control_cost_recalculated': False, 'routing_backend': 'refined_physical_navigation_graph_only', 'raster_routing_allowed': False, 'repeated_nodes_and_edges_allowed': True, 'passed_floors': passed, 'failed_floors': failed, 'floor_reports': floor_reports}
        json_path = write_json(output / 'route_acceptance_report.json', payload)
        lines = ['# 巡检路线验收报告', '', f"- 总体结果：{payload['overall_acceptance']}", f"- 通过楼层：{(', '.join(passed) if passed else '无')}", f"- 未通过楼层：{(', '.join(failed) if failed else '无')}", '- 控制层对象及访问顺序：保持不变', '- 转发层重新计算控制成本：否', '- 路线策略：仅使用精细物理导航图；允许重复节点和重复边；不启用局部栅格。', '- 每条输出路线均通过有效自由空间矢量复核；失败路段不会生成跨墙替代线。', '- DXF 仅包含实际生成的路线、巡检目标框和访问序号；失败原因只记录在本报告中。', '']
        for floor_id, row in floor_reports.items():
            lines.extend([f"## {floor_id} — {row['acceptance']}", '', f"对象 {row['target_count']}；路段完成 {row['completed_leg_count']}/{row['expected_leg_count']}；物理图 {row['graph_leg_count']}；图外/局部栅格 {row['local_raster_leg_count']}。", ''])
            for failure in row['failed_legs']:
                lines.extend([f"- 失败路段 {failure['leg_index']}：访问 {failure['from_visit_order']} `{failure['from_target_id']}` → 访问 {failure['to_visit_order']} `{failure['to_target_id']}`", f"  - 原因：{failure['explanation']}（`{failure['reason_code']}`）"])
            if row['failed_legs']:
                lines.append('')
            if row.get('acceptance') == 'control_failed':
                lines.extend([f"- 控制层失败：{row.get('control_failure_reason')}", ''])
        markdown_path = output / 'route_acceptance_report.md'
        markdown_path.write_text('\n'.join(lines), encoding='utf-8')
        return {'acceptance_report_json': str(json_path), 'acceptance_report_markdown': str(markdown_path)}
    __all__ = ['write_acceptance_reports']
    return dict(locals())

_s10_acceptance = _register_embedded_module(
    'fire_inspection_system.last.acceptance_report',
    _build_s10_acceptance(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/last/annotated_output.py
# -----------------------------------------------------------------------------
def _build_s10_annotated():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/last/annotated_output.py'
    )
    __name__ = 'fire_inspection_system.last.annotated_output'
    __package__ = 'fire_inspection_system.last'
    import csv
    import math
    from collections import Counter
    from pathlib import Path
    from typing import Any, Iterable
    import ezdxf
    from ezdxf.enums import TextEntityAlignment
    from fire_inspection_system.last.io_utils import write_json
    PASS_COLORS = (3, 1, 6, 5, 2, 4, 30, 140, 200)

    def _layer(doc: Any, name: str, color: int, lineweight: int=25) -> None:
        if name not in doc.layers:
            doc.layers.add(name, color=color, lineweight=lineweight)

    def _pass_layer(pass_index: int, backend: str) -> tuple[str, int]:
        repeated = pass_index > 1
        prefix = 'LAST_ROUTE_REPEAT' if repeated else 'LAST_ROUTE_PRIMARY'
        suffix = 'LOCAL' if 'raster' in backend or 'cache' in backend else 'GRAPH'
        color = PASS_COLORS[min(max(pass_index - 1, 0), len(PASS_COLORS) - 1)]
        return (f'{prefix}_P{pass_index:02d}_{suffix}', color)

    def _floor_scale(targets: list[dict[str, Any]], events: list[dict[str, Any]]) -> float:
        points = []
        for row in targets:
            point = row.get('access_point') or row.get('raw_point')
            if isinstance(point, list) and len(point) >= 2:
                points.append((float(point[0]), float(point[1])))
        for row in events:
            geometry = row.get('geometry') or {}
            for point in geometry.get('coordinates', []) or []:
                if isinstance(point, list) and len(point) >= 2:
                    points.append((float(point[0]), float(point[1])))
        if len(points) < 2:
            return 1.0
        min_x = min((point[0] for point in points))
        max_x = max((point[0] for point in points))
        min_y = min((point[1] for point in points))
        max_y = max((point[1] for point in points))
        return max(math.hypot(max_x - min_x, max_y - min_y), 1.0)

    def write_annotated_route_dxf(source_dxf: Path | str | None, output_dxf: Path | str, visit_plan: dict[str, Any], forwarding: dict[str, Any]) -> Path:
        source = Path(source_dxf).resolve() if source_dxf else None
        doc = ezdxf.readfile(source) if source and source.exists() else ezdxf.new('R2018')
        msp = doc.modelspace()
        _layer(doc, 'LAST_TARGET_FRAME', 2, 35)
        _layer(doc, 'LAST_TARGET_ORDER', 7, 13)
        _layer(doc, 'LAST_VIRTUAL_CONTINUATION', 1, 35)
        for floor_id, floor_route in sorted((forwarding.get('floors') or {}).items()):
            floor_plan = (visit_plan.get('floors') or {}).get(floor_id) or {}
            targets = list(floor_plan.get('targets', []) or [])
            traversal_events = list(floor_route.get('traversal_events', []) or [])
            scale = _floor_scale(targets, traversal_events)
            text_height = scale / max(260.0, math.sqrt(max(len(targets), 1)) * 24.0)
            marker_radius = text_height * 0.55
            for event in traversal_events:
                geometry = event.get('geometry') or {}
                coordinates = geometry.get('coordinates') or []
                if len(coordinates) < 2:
                    continue
                pass_index = int(event.get('pass_index') or 1)
                backend = str(event.get('backend') or '')
                layer_name, color = _pass_layer(pass_index, backend)
                _layer(doc, layer_name, color, 35 if pass_index > 1 else 25)
                polyline = msp.add_lwpolyline([(float(point[0]), float(point[1])) for point in coordinates], dxfattribs={'layer': layer_name, 'color': color})
                if 'raster' in backend or 'cache' in backend:
                    polyline.dxf.lineweight = 35
            target_groups: dict[str, list[dict[str, Any]]] = {}
            for target in targets:
                target_groups.setdefault(str(target.get('target_id') or ''), []).append(target)
            for _target_id, target_visits in target_groups.items():
                target = target_visits[0]
                access = target.get('access_point') or [0.0, 0.0]
                raw = target.get('raw_point') or access
                access_xy = (float(access[0]), float(access[1]))
                raw_xy = (float(raw[0]), float(raw[1]))
                bbox = target.get('object_bbox') or []
                if isinstance(bbox, list) and len(bbox) >= 4:
                    min_x, min_y, max_x, max_y = map(float, bbox[:4])
                else:
                    min_x, min_y = (raw_xy[0] - marker_radius, raw_xy[1] - marker_radius)
                    max_x, max_y = (raw_xy[0] + marker_radius, raw_xy[1] + marker_radius)
                msp.add_lwpolyline([(min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y)], close=True, dxfattribs={'layer': 'LAST_TARGET_FRAME', 'color': 2, 'lineweight': 35})
                visit_orders = sorted((int(row.get('visit_order') or 0) for row in target_visits))
                label = msp.add_text('/'.join((f'{visit_order:03d}' for visit_order in visit_orders)), height=text_height, dxfattribs={'layer': 'LAST_TARGET_ORDER', 'color': 7})
                label.set_placement((min_x, max_y + text_height * 0.2), align=TextEntityAlignment.LEFT)
                virtual_entry = next((row for row in target_visits if row.get('is_route_segment_entry') and row.get('segment_virtual_continuation')), None)
                if virtual_entry:
                    continuation = msp.add_text('VIRTUAL CONTINUATION', height=text_height, dxfattribs={'layer': 'LAST_VIRTUAL_CONTINUATION', 'color': 1})
                    continuation.set_placement((min_x, min_y - text_height * 1.2), align=TextEntityAlignment.LEFT)
        output = Path(output_dxf).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        doc.saveas(output)
        ezdxf.readfile(output)
        return output

    def forwarding_to_geojson(forwarding: dict[str, Any], visit_plan: dict[str, Any]) -> dict[str, Any]:
        features = []
        for floor_id, floor in sorted((forwarding.get('floors') or {}).items()):
            edge_totals = Counter((row.get('physical_edge_key') for row in floor.get('traversal_events', []) or []))
            for event in floor.get('traversal_events', []) or []:
                features.append({'type': 'Feature', 'properties': {'feature_type': 'route_edge_traversal', 'floor_id': floor_id, 'event_id': event.get('event_id'), 'sequence_no': event.get('sequence_no'), 'leg_index': event.get('leg_index'), 'route_segment_id': event.get('route_segment_id'), 'from_target_id': event.get('from_target_id'), 'to_target_id': event.get('to_target_id'), 'physical_edge_key': event.get('physical_edge_key'), 'pass_index': event.get('pass_index'), 'traversal_count': edge_totals[event.get('physical_edge_key')], 'is_repeated_traversal': event.get('is_repeated_traversal'), 'backend': event.get('backend')}, 'geometry': event.get('geometry')})
            floor_plan = (visit_plan.get('floors') or {}).get(floor_id) or {}
            for target in floor_plan.get('targets', []) or []:
                point = target.get('access_point') or [0.0, 0.0]
                features.append({'type': 'Feature', 'properties': {'feature_type': 'selected_inspection_target', 'floor_id': floor_id, 'target_id': target.get('target_id'), 'visit_order': target.get('visit_order'), 'route_segment_id': target.get('route_segment_id'), 'is_route_segment_entry': target.get('is_route_segment_entry'), 'segment_entry_label': target.get('segment_entry_label'), 'segment_virtual_continuation': target.get('segment_virtual_continuation'), 'segment_virtual_entry_is_not_physical_path': target.get('segment_virtual_entry_is_not_physical_path'), 'segment_continuation_break_reason': target.get('segment_continuation_break_reason'), 'class_name': target.get('class_name'), 'raw_name': target.get('raw_name'), 'access_node_id': target.get('access_node_id'), 'virtual_access': target.get('virtual_access')}, 'geometry': {'type': 'Point', 'coordinates': list(point[:2])}})
        return {'type': 'FeatureCollection', 'features': features}

    def write_event_csvs(output_dir: Path | str, forwarding: dict[str, Any]) -> dict[str, str]:
        output = Path(output_dir).resolve()
        output.mkdir(parents=True, exist_ok=True)
        node_path = output / 'node_visit_events.csv'
        edge_path = output / 'edge_traversal_events.csv'
        target_path = output / 'target_visit_events.csv'

        def write_rows(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
            with path.open('w', encoding='utf-8-sig', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(rows)
        node_rows = []
        edge_rows = []
        target_rows = []
        for floor_id, floor in sorted((forwarding.get('floors') or {}).items()):
            for row in floor.get('node_visit_events', []) or []:
                point = row.get('point') or [None, None]
                node_rows.append({'floor_id': floor_id, **row, 'x': point[0], 'y': point[1]})
            for row in floor.get('traversal_events', []) or []:
                edge_rows.append({'floor_id': floor_id, **row})
            for row in floor.get('target_visit_events', []) or []:
                point = row.get('access_point') or [None, None]
                target_rows.append({'floor_id': floor_id, **row, 'x': point[0], 'y': point[1]})
        write_rows(node_path, node_rows, ['floor_id', 'event_id', 'sequence_no', 'physical_node_id', 'visit_ordinal_for_node', 'leg_index', 'x', 'y', 'is_repeated_visit'])
        write_rows(edge_path, edge_rows, ['floor_id', 'event_id', 'sequence_no', 'leg_index', 'route_segment_id', 'from_target_id', 'to_target_id', 'physical_edge_key', 'source_edge_id', 'source_node_id', 'target_node_id', 'pass_index', 'is_repeated_traversal', 'direction', 'backend', 'distance'])
        write_rows(target_path, target_rows, ['floor_id', 'route_segment_id', 'target_id', 'visit_order', 'node_event_id', 'access_node_id', 'x', 'y'])
        return {'node_visit_events_csv': str(node_path), 'edge_traversal_events_csv': str(edge_path), 'target_visit_events_csv': str(target_path)}

    def write_inspection_visit_order(output_dir: Path | str, visit_plan: dict[str, Any]) -> dict[str, str]:
        """Write a human-reviewable first/repeated inspection visit sequence."""
        output = Path(output_dir).resolve()
        rows: list[dict[str, Any]] = []
        floors_payload: dict[str, Any] = {}
        for floor_id, floor in sorted((visit_plan.get('floors') or {}).items()):
            ordinal: Counter[str] = Counter()
            first_order: dict[str, int] = {}
            floor_rows = []
            for target in floor.get('targets', []) or []:
                target_id = str(target.get('target_id') or '')
                route_order = int(target.get('visit_order') or 0)
                ordinal[target_id] += 1
                first_order.setdefault(target_id, route_order)
                point = target.get('access_point') or [None, None]
                row = {'floor_id': floor_id, 'route_segment_id': target.get('route_segment_id'), 'segment_visit_ordinal': target.get('segment_visit_ordinal'), 'is_route_segment_entry': target.get('is_route_segment_entry'), 'segment_entry_mode': target.get('segment_entry_mode'), 'segment_entry_label': target.get('segment_entry_label'), 'segment_virtual_continuation': target.get('segment_virtual_continuation'), 'segment_virtual_entry_is_not_physical_path': target.get('segment_virtual_entry_is_not_physical_path'), 'segment_continuation_break_reason': target.get('segment_continuation_break_reason'), 'route_visit_order': route_order, 'first_visit_order': first_order[target_id], 'visit_ordinal_for_target': ordinal[target_id], 'is_repeated_target_visit': ordinal[target_id] > 1, 'target_id': target_id, 'class_name': target.get('class_name'), 'raw_name': target.get('raw_name'), 'mandatory': bool(target.get('mandatory')), 'access_node_id': target.get('graph_access_node_id'), 'x': point[0], 'y': point[1]}
                rows.append(row)
                floor_rows.append(row)
            floors_payload[floor_id] = {'unique_target_count': len(first_order), 'route_visit_count': len(floor_rows), 'repeated_target_visit_count': len(floor_rows) - len(first_order), 'route_segment_count': len({row.get('route_segment_id') for row in floor_rows}), 'visits': floor_rows}
        csv_path = output / 'inspection_target_visit_order.csv'
        with csv_path.open('w', encoding='utf-8-sig', newline='') as handle:
            fields = ['floor_id', 'route_segment_id', 'segment_visit_ordinal', 'is_route_segment_entry', 'segment_entry_mode', 'segment_entry_label', 'segment_virtual_continuation', 'segment_virtual_entry_is_not_physical_path', 'segment_continuation_break_reason', 'route_visit_order', 'first_visit_order', 'visit_ordinal_for_target', 'is_repeated_target_visit', 'target_id', 'class_name', 'raw_name', 'mandatory', 'access_node_id', 'x', 'y']
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)
        first_rows = [row for row in rows if int(row['visit_ordinal_for_target']) == 1]
        first_csv_path = output / 'inspection_target_first_visit_order.csv'
        with first_csv_path.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(first_rows)
        json_path = write_json(output / 'inspection_target_visit_order.json', {'schema_version': 1, 'artifact_type': 'same_floor_inspection_target_visit_order', 'cross_floor_route_allowed': False, 'floors': floors_payload})
        first_json_path = write_json(output / 'inspection_target_first_visit_order.json', {'schema_version': 1, 'artifact_type': 'same_floor_inspection_target_first_visit_order', 'cross_floor_route_allowed': False, 'floors': {floor_id: {'unique_target_count': payload['unique_target_count'], 'route_segment_count': payload['route_segment_count'], 'first_visits': [row for row in payload['visits'] if int(row['visit_ordinal_for_target']) == 1]} for floor_id, payload in floors_payload.items()}})
        return {'inspection_target_visit_order_csv': str(csv_path), 'inspection_target_visit_order_json': str(json_path), 'inspection_target_first_visit_order_csv': str(first_csv_path), 'inspection_target_first_visit_order_json': str(first_json_path)}
    __all__ = ['forwarding_to_geojson', 'write_annotated_route_dxf', 'write_event_csvs', 'write_inspection_visit_order']
    return dict(locals())

_s10_annotated = _register_embedded_module(
    'fire_inspection_system.last.annotated_output',
    _build_s10_annotated(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/last/pipeline.py
# -----------------------------------------------------------------------------
def _build_s10_pipeline():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/last/pipeline.py'
    )
    __name__ = 'fire_inspection_system.last.pipeline'
    __package__ = 'fire_inspection_system.last'
    import argparse
    import time
    from pathlib import Path
    from typing import Any, Iterable
    from fire_inspection_system.last.acceptance_report import write_acceptance_reports
    from fire_inspection_system.last.annotated_output import forwarding_to_geojson, write_annotated_route_dxf, write_event_csvs, write_inspection_visit_order
    from fire_inspection_system.last.control_plane import build_locked_visit_plan
    from fire_inspection_system.last.forwarding_plane import forward_locked_visit_plan
    from fire_inspection_system.last.io_utils import read_json, text, write_json
    from fire_inspection_system.last.physical_graph import RefinedPhysicalGraph
    from fire_inspection_system.last.vector_free_space import VectorFreeSpaceIndex

    def _source_dxf(run_dir: Path, override: Path | str | None) -> Path | None:
        if override:
            path = Path(override).resolve()
            return path if path.exists() else None
        summary_path = run_dir / 'pipeline_summary.json'
        if not summary_path.exists():
            return None
        value = text(read_json(summary_path).get('input_dxf'))
        path = Path(value).resolve() if value else None
        return path if path and path.exists() else None

    def build_last_inspection_route(run_dir: Path | str, optimized_order_path: Path | str, candidates_path: Path | str, effective_free_areas_path: Path | str, *, output_dir: Path | str | None=None, refined_graph_path: Path | str | None=None, source_dxf: Path | str | None=None, floors: Iterable[str] | None=None, route_cache_path: Path | str | None=None, max_grid_cells: int=1500000, write_dxf: bool=True) -> dict[str, Any]:
        started = time.perf_counter()
        run = Path(run_dir).resolve()
        output = Path(output_dir).resolve() if output_dir else run / 'last_route_planning'
        output.mkdir(parents=True, exist_ok=True)
        refined_path = Path(refined_graph_path).resolve() if refined_graph_path else run / 'area_graph_navigation_refined' / 'refined_navigation_graph.json'
        if not refined_path.exists():
            raise FileNotFoundError(refined_path)
        control_plan_path = output / 'control_visit_plan.json'
        visit_plan = build_locked_visit_plan(optimized_order_path, candidates_path, output_path=control_plan_path, floors=floors)
        refined_graph = RefinedPhysicalGraph(refined_path)
        free_spaces = VectorFreeSpaceIndex(effective_free_areas_path)
        forwarding = forward_locked_visit_plan(visit_plan, refined_graph, free_spaces)
        forwarding_path = write_json(output / 'forwarding_route.json', forwarding)
        geojson_path = write_json(output / 'forwarding_route.geojson', forwarding_to_geojson(forwarding, visit_plan))
        csv_outputs = write_event_csvs(output, forwarding)
        visit_order_outputs = write_inspection_visit_order(output, visit_plan)
        acceptance_outputs = write_acceptance_reports(output, visit_plan, forwarding)
        source = _source_dxf(run, source_dxf)
        dxf_path: Path | None = None
        dxf_error = ''
        if write_dxf:
            try:
                stem = source.stem if source else 'inspection'
                dxf_path = write_annotated_route_dxf(source, output / f'{stem}_last_annotated_routes.dxf', visit_plan, forwarding)
            except Exception as exc:
                dxf_error = f'{type(exc).__name__}: {exc}'
        summary = {'schema_version': 1, 'pipeline_type': 'locked_control_physical_graph_walk_with_temporal_repeats', 'run_dir': str(run), 'output_dir': str(output), 'requirements': {'control_target_instances_locked': True, 'control_access_points_locked': True, 'control_visit_order_locked': True, 'control_cost_recalculated_after_forwarding': False, 'primary_forwarding_plane': 'refined_physical_navigation_graph', 'off_graph_fallback_allowed': False, 'raster_routing_allowed': False, 'every_route_segment_vector_certified': True, 'repeated_visits_allowed': True, 'temporal_order_recorded_for_every_node_and_edge_traversal': True, 'support_degree_limit': None, 'structural_in_degree_limit': None, 'structural_out_degree_limit': None, 'repeated_route_coloring': 'DXF pass-index layers'}, 'inputs': {'optimized_order': str(Path(optimized_order_path).resolve()), 'candidates': str(Path(candidates_path).resolve()), 'refined_physical_graph': str(refined_path), 'effective_free_areas': str(Path(effective_free_areas_path).resolve()), 'source_dxf': str(source) if source else '', 'ignored_route_cache': str(Path(route_cache_path).resolve()) if route_cache_path else ''}, 'computational_config': {'routing_backend': 'refined_physical_navigation_graph_only', 'deprecated_max_grid_cells_ignored': int(max_grid_cells), 'note': 'no route raster is constructed or searched'}, 'counts': forwarding.get('counts', {}), 'floor_results': {floor_id: {'status': row.get('status'), 'feasible': row.get('feasible'), 'control_planned_cost': row.get('control_planned_cost'), 'forwarding_actual_geometry_length': row.get('forwarding_actual_geometry_length'), **(row.get('counts') or {}), **{key: (row.get('support_topology') or {}).get(key) for key in ('max_support_degree', 'max_structural_in_degree', 'max_structural_out_degree', 'valid')}} for floor_id, row in (forwarding.get('floors') or {}).items()}, 'outputs': {'control_visit_plan': str(control_plan_path), 'forwarding_route': str(forwarding_path), 'forwarding_route_geojson': str(geojson_path), 'annotated_route_dxf': str(dxf_path) if dxf_path else '', 'annotated_route_dxf_error': dxf_error, **csv_outputs, **visit_order_outputs, **acceptance_outputs}, 'elapsed_seconds': time.perf_counter() - started}
        summary_path = write_json(output / 'last_route_summary.json', summary)
        summary['summary_path'] = str(summary_path)
        return summary
    __all__ = ['build_last_inspection_route', 'main']
    return dict(locals())

_s10_pipeline = _register_embedded_module(
    'fire_inspection_system.last.pipeline',
    _build_s10_pipeline(),
    aliases=(),
)

# === CONSOLIDATED PUBLIC API ===
import copy
import json
import time
from pathlib import Path
from typing import Any


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return path


def _require(path: Path | str, description: str) -> Path:
    result = Path(path).resolve()
    if not result.is_file() or result.stat().st_size <= 0:
        raise FileNotFoundError(f"{description}不存在或为空: {result}")
    return result


def run_stage(
    *,
    run_dir: Path,
    input_dxf: Path,
    pipeline_summary: dict[str, Any],
    physical: Any | None,
    semantic: Any | None,
    planning: Any | None,
    write_dxf: bool,
) -> tuple[dict[str, Any] | None, Path]:
    path_result: dict[str, Any] | None = None
    if physical is not None and semantic is not None and planning is not None:
        started = time.perf_counter()
        dual = copy.deepcopy(planning.dual_graph)
        dual_output = (run_dir / "path_planning" / "dual_graph").resolve()
        certified_graph = _require(
            dual["precomputed_inputs"]["certified_physical_graph"],
            "认证物理导航图",
        )
        route_summary = _s10_pipeline.build_last_inspection_route(
            run_dir,
            dual["outputs"]["optimized_target_order"],
            dual["outputs"]["physical_access_candidates"],
            _require(physical.effective_free_areas, "有效自由空间"),
            output_dir=dual_output / "physical_walk",
            refined_graph_path=certified_graph,
            source_dxf=input_dxf,
            write_dxf=write_dxf,
        )
        forwarding_seconds = time.perf_counter() - started
        dual["config"]["write_dxf"] = write_dxf
        dual["config"]["expand_physical_walk"] = True
        dual["timing"]["physical_walk_expansion_and_artifact_seconds"] = (
            forwarding_seconds
        )
        dual["timing"]["total_pipeline_seconds"] = (
            float(dual["timing"].get("recognition_precompute_seconds_excluded_from_planning") or 0.0)
            + float(dual["timing"].get("dual_graph_planning_seconds") or 0.0)
            + forwarding_seconds
        )
        dual["outputs"].update(
            {
                "physical_walk_summary": route_summary["summary_path"],
                "annotated_route_dxf": route_summary.get("outputs", {}).get(
                    "annotated_route_dxf", ""
                ),
                "acceptance_report": route_summary.get("outputs", {}).get(
                    "acceptance_report_markdown", ""
                ),
                "inspection_target_visit_order_csv": route_summary.get(
                    "outputs", {}
                ).get("inspection_target_visit_order_csv", ""),
                "inspection_target_visit_order_json": route_summary.get(
                    "outputs", {}
                ).get("inspection_target_visit_order_json", ""),
                "inspection_target_first_visit_order_csv": route_summary.get(
                    "outputs", {}
                ).get("inspection_target_first_visit_order_csv", ""),
                "inspection_target_first_visit_order_json": route_summary.get(
                    "outputs", {}
                ).get("inspection_target_first_visit_order_json", ""),
            }
        )
        dual_summary_path = _write_json(dual_output / "dual_graph_summary.json", dual)
        dual["summary_path"] = str(dual_summary_path)
        annotated_dxf = str(dual["outputs"].get("annotated_route_dxf") or "")
        if write_dxf:
            _require(annotated_dxf, "最终路线标注 DXF")

        path_result = {
            "schema_version": 1,
            "pipeline_type": "main_integrated_same_floor_dual_graph_path_planning",
            "run_dir": str(run_dir.resolve()),
            "output_dir": str((run_dir / "path_planning").resolve()),
            "source_dxf": str(input_dxf.resolve()),
            "source_run_id": semantic.source_run_id,
            "selected_floor_ids": list(semantic.selected_floor_ids),
            "architecture": dual["architecture"],
            "refinement": physical.refinement or {},
            "effective_free_space": physical.free_space_audit or {},
            "candidate_context": semantic.candidate_summary,
            "rgcn": semantic.rgcn,
            "dual_graph": dual,
            "counts": dual["counts"],
            "elapsed_seconds": (
                physical.elapsed_seconds
                + semantic.elapsed_seconds
                + planning.elapsed_seconds
                + forwarding_seconds
            ),
            "outputs": {
                "annotated_route_dxf": annotated_dxf,
                "inspection_target_visit_order_csv": dual["outputs"][
                    "inspection_target_visit_order_csv"
                ],
                "inspection_target_visit_order_json": dual["outputs"][
                    "inspection_target_visit_order_json"
                ],
                "inspection_target_first_visit_order_csv": dual["outputs"][
                    "inspection_target_first_visit_order_csv"
                ],
                "inspection_target_first_visit_order_json": dual["outputs"][
                    "inspection_target_first_visit_order_json"
                ],
                "dual_graph_summary": str(dual_summary_path),
                "rgcn_summary": semantic.rgcn["summary_path"],
                "candidate_context_summary": semantic.candidate_summary[
                    "summary_path"
                ],
                "effective_free_areas": str(physical.effective_free_areas),
            },
        }
        path_summary_path = _write_json(
            run_dir / "path_planning" / "path_planning_summary.json",
            path_result,
        )
        path_result["summary_path"] = str(path_summary_path)

    pipeline_summary["path_planning"] = path_result or {"enabled": False}
    summary_path = _write_json(run_dir / "pipeline_summary.json", pipeline_summary)
    return path_result, summary_path


__all__ = ["run_stage"]
