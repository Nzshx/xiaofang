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

def _deferred_last_route_call(*args, **kwargs):
    raise RuntimeError("Physical route expansion belongs to stage 10")

_register_stub_module(
    "fire_inspection_system.last.pipeline",
    build_last_inspection_route=_deferred_last_route_call,
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/last/io_utils.py
# -----------------------------------------------------------------------------
def _build_s09_io():
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

_s09_io = _register_embedded_module(
    'fire_inspection_system.last.io_utils',
    _build_s09_io(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/last/vector_free_space.py
# -----------------------------------------------------------------------------
def _build_s09_vector_free():
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

_s09_vector_free = _register_embedded_module(
    'fire_inspection_system.last.vector_free_space',
    _build_s09_vector_free(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/semantic/pseudo_label_route_solver.py
# -----------------------------------------------------------------------------
def _build_s09_pseudo_solver():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/semantic/pseudo_label_route_solver.py'
    )
    __name__ = 'fire_inspection_system.semantic.pseudo_label_route_solver'
    __package__ = 'fire_inspection_system.semantic'
    """Constraint-teacher solution pools and on-demand physical route expansion.

    The semantic teacher uses Euclidean target distances only to create inexpensive
    selection/order proposals.  It never builds an all-pairs physical Metric
    Closure.  Once a proposal exists, only consecutive selected target pairs are
    expanded on the refined navigation graph with cached, early-stop Dijkstra.
    Disconnected physical components become separate open route segments; no
    straight-line jump is inserted between them.
    """
    import argparse
    import hashlib
    import heapq
    import itertools
    import json
    import math
    import time
    from collections import Counter, defaultdict
    from dataclasses import dataclass
    from pathlib import Path
    from typing import Any, Iterable, Mapping, Sequence
    EPS = 1e-09
    DEFAULT_DELTAS = (0.0, 0.05, 0.1)



    def _text(value: Any) -> str:
        return '' if value is None else str(value).strip()

    def _finite(value: Any, default: float=0.0) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if math.isfinite(number) else default










    class _UnionFind:

        def __init__(self, values: Iterable[str]) -> None:
            self.parent = {value: value for value in values}

        def find(self, value: str) -> str:
            parent = self.parent[value]
            if parent != value:
                self.parent[value] = self.find(parent)
            return self.parent[value]

        def union(self, left: str, right: str) -> None:
            a, b = (self.find(left), self.find(right))
            if a != b:
                self.parent[b] = a

    @dataclass
    class _PathResult:
        distance: float
        node_ids: list[str]
        edge_indices: list[int]

    class PhysicalFloorGraph:

        def __init__(self, floor_id: str, nodes: Sequence[Mapping[str, Any]], edges: Sequence[Mapping[str, Any]]) -> None:
            self.floor_id = floor_id
            self.nodes = {_text(row.get('node_id')): dict(row) for row in nodes if _text(row.get('node_id'))}
            self.edges = [dict(row) for row in edges]
            self.adjacency: dict[str, list[tuple[str, float, int]]] = defaultdict(list)
            union = _UnionFind(self.nodes)
            for index, edge in enumerate(self.edges):
                left, right = (_text(edge.get('node_a')), _text(edge.get('node_b')))
                if left not in self.nodes or right not in self.nodes:
                    continue
                length = max(0.0, _finite(edge.get('length'), 0.0))
                self.adjacency[left].append((right, length, index))
                self.adjacency[right].append((left, length, index))
                union.union(left, right)
            self.component = {node_id: union.find(node_id) for node_id in self.nodes}
            self.target_nodes = {_text(row.get('target_id')): node_id for node_id, row in self.nodes.items() if row.get('kind') in {'target', 'virtual_target_access'} and _text(row.get('target_id'))}
            self.cache: dict[tuple[str, str], _PathResult | None] = {}

        def _populate_paths(self, source: str, targets: set[str]) -> None:
            pending = {target for target in targets if (source, target) not in self.cache and source in self.nodes and (target in self.nodes) and (self.component[source] == self.component[target])}
            for target in targets:
                if source not in self.nodes or target not in self.nodes or self.component.get(source) != self.component.get(target):
                    self.cache[source, target] = None
            if not pending:
                return
            distances = {source: 0.0}
            previous: dict[str, tuple[str, int]] = {}
            queue: list[tuple[float, str]] = [(0.0, source)]
            found: set[str] = set()
            while queue and len(found) < len(pending):
                distance, node = heapq.heappop(queue)
                if distance > distances.get(node, math.inf) + EPS:
                    continue
                if node in pending:
                    found.add(node)
                for neighbor, weight, edge_index in self.adjacency.get(node, []):
                    proposal = distance + weight
                    if proposal + EPS < distances.get(neighbor, math.inf):
                        distances[neighbor] = proposal
                        previous[neighbor] = (node, edge_index)
                        heapq.heappush(queue, (proposal, neighbor))
            for target in pending:
                if target not in distances:
                    self.cache[source, target] = None
                    continue
                nodes = [target]
                edge_indices: list[int] = []
                current = target
                while current != source:
                    parent, edge_index = previous[current]
                    nodes.append(parent)
                    edge_indices.append(edge_index)
                    current = parent
                nodes.reverse()
                edge_indices.reverse()
                result = _PathResult(distances[target], nodes, edge_indices)
                self.cache[source, target] = result
                self.cache[target, source] = _PathResult(result.distance, list(reversed(result.node_ids)), list(reversed(result.edge_indices)))

        def precompute_target_orders(self, orders: Iterable[Sequence[str]]) -> None:
            requested: dict[str, set[str]] = defaultdict(set)
            for order in orders:
                for segment in self.segment_orders(order):
                    for left_target, right_target in zip(segment, segment[1:]):
                        left = self.target_nodes.get(left_target)
                        right = self.target_nodes.get(right_target)
                        if left and right:
                            requested[left].add(right)
            for source, targets in requested.items():
                self._populate_paths(source, targets)

        def shortest_path(self, source: str, target: str) -> _PathResult | None:
            key = (source, target)
            if key in self.cache:
                return self.cache[key]
            if source == target:
                result = _PathResult(0.0, [source], [])
                self.cache[key] = result
                return result
            self._populate_paths(source, {target})
            return self.cache.get(key)

        def segment_orders(self, order: Sequence[str]) -> list[list[str]]:
            groups: dict[str, list[str]] = defaultdict(list)
            group_order: list[str] = []
            for target_id in order:
                node_id = self.target_nodes.get(target_id)
                if not node_id:
                    group = f'MISSING::{target_id}'
                else:
                    group = self.component[node_id]
                if group not in groups:
                    group_order.append(group)
                groups[group].append(target_id)
            return [groups[group] for group in group_order]

        def expand(self, order: Sequence[str], *, include_details: bool=False) -> dict[str, Any]:
            segments = self.segment_orders(order)
            output_segments: list[dict[str, Any]] = []
            wall_crossing_edges = 0
            unvalidated_edges = 0
            unavailable_targets: list[str] = []
            total_length = 0.0
            all_leg_distances: list[float] = []
            incident_edges: dict[str, set[int]] = defaultdict(set)
            edge_use_count: Counter[int] = Counter()
            for segment_index, target_order in enumerate(segments):
                if target_order and target_order[0] not in self.target_nodes:
                    unavailable_targets.extend(target_order)
                    continue
                legs: list[dict[str, Any]] = []
                for source_target, target_target in zip(target_order, target_order[1:]):
                    source_node = self.target_nodes.get(source_target)
                    target_node = self.target_nodes.get(target_target)
                    if not source_node or not target_node:
                        unavailable_targets.extend((value for value, node in ((source_target, source_node), (target_target, target_node)) if not node))
                        continue
                    path = self.shortest_path(source_node, target_node)
                    if path is None:
                        legs.append({'from_target_id': source_target, 'to_target_id': target_target, 'reachable': False})
                        continue
                    invalid = 0
                    missing = 0
                    for edge_index in path.edge_indices:
                        edge = self.edges[edge_index]
                        status = edge.get('vector_valid_with_raster_tolerance')
                        invalid += status is False
                        missing += status is None
                        node_a = _text(edge.get('node_a'))
                        node_b = _text(edge.get('node_b'))
                        if node_a and node_b:
                            incident_edges[node_a].add(edge_index)
                            incident_edges[node_b].add(edge_index)
                            edge_use_count[edge_index] += 1
                    wall_crossing_edges += invalid
                    unvalidated_edges += missing
                    total_length += path.distance
                    all_leg_distances.append(path.distance)
                    leg = {'from_target_id': source_target, 'to_target_id': target_target, 'reachable': True, 'distance': path.distance, 'wall_crossing_edge_count': invalid, 'unvalidated_edge_count': missing, 'path_edge_count': len(path.edge_indices)}
                    if include_details:
                        leg['path_node_ids'] = path.node_ids
                        leg['path_edge_ids'] = [self.edges[index].get('edge_id') for index in path.edge_indices]
                        leg['geometry'] = self._path_geometry(path)
                    legs.append(leg)
                output_segments.append({'segment_index': segment_index, 'target_order': list(target_order), 'start_target_id': target_order[0] if target_order else None, 'end_target_id': target_order[-1] if target_order else None, 'legs': legs, 'length': sum((_finite(row.get('distance'), 0.0) for row in legs))})
            branch_node_ids = sorted((node_id for node_id, edge_ids in incident_edges.items() if len(edge_ids) > 2))
            endpoint_node_ids = sorted((node_id for node_id, edge_ids in incident_edges.items() if len(edge_ids) == 1))
            return {'floor_id': self.floor_id, 'route_segment_count': len(output_segments), 'segments': output_segments, 'physical_route_length': total_length, 'wall_crossing_edge_count': wall_crossing_edges, 'unvalidated_edge_count': unvalidated_edges, 'unavailable_target_ids': sorted(set(unavailable_targets)), 'all_legs_reachable': all((leg.get('reachable') for segment in output_segments for leg in segment['legs'])), 'maximum_target_gap': max(all_leg_distances, default=0.0), 'mean_target_gap': sum(all_leg_distances) / len(all_leg_distances) if all_leg_distances else 0.0, 'leg_count': len(all_leg_distances), 'branch_node_count': len(branch_node_ids), 'branch_node_ids': branch_node_ids if include_details else [], 'repeated_edge_traversal_count': sum((max(0, count - 1) for count in edge_use_count.values())), 'route_union_endpoint_count': len(endpoint_node_ids), 'branch_free': len(branch_node_ids) == 0}

        def _path_geometry(self, path: _PathResult) -> list[list[float]]:
            coordinates: list[list[float]] = []
            for position, edge_index in enumerate(path.edge_indices):
                edge = self.edges[edge_index]
                current_node = path.node_ids[position]
                raw = (edge.get('geometry') or {}).get('coordinates') or []
                points = [list(map(float, point[:2])) for point in raw if isinstance(point, Sequence) and len(point) >= 2]
                if not points:
                    left = self.nodes[path.node_ids[position]]
                    right = self.nodes[path.node_ids[position + 1]]
                    points = [[_finite(left.get('x')), _finite(left.get('y'))], [_finite(right.get('x')), _finite(right.get('y'))]]
                if _text(edge.get('node_a')) != current_node:
                    points.reverse()
                if coordinates and points and (coordinates[-1] == points[0]):
                    coordinates.extend(points[1:])
                else:
                    coordinates.extend(points)
            return coordinates


















    __all__ = ['PhysicalFloorGraph']
    return dict(locals())

_s09_pseudo_solver = _register_embedded_module(
    'fire_inspection_system.semantic.pseudo_label_route_solver',
    _build_s09_pseudo_solver(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/semantic/chain_route_planner.py
# -----------------------------------------------------------------------------
def _build_s09_chain():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/semantic/chain_route_planner.py'
    )
    __name__ = 'fire_inspection_system.semantic.chain_route_planner'
    __package__ = 'fire_inspection_system.semantic'
    """Mandatory-backbone open-route planner with branch-free chain constraints."""
    import argparse
    import json
    import math
    import time
    from dataclasses import asdict, dataclass
    from pathlib import Path
    from typing import Any, Mapping, Sequence
    from fire_inspection_system.semantic.physical_metric_closure import PhysicalMetricClosure
    from fire_inspection_system.semantic.pseudo_label_route_solver import PhysicalFloorGraph
    EPS = 1e-09

    def _text(value: Any) -> str:
        return '' if value is None else str(value).strip()

    def _finite(value: Any, default: float=0.0) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return default
        return result if math.isfinite(result) else default



    def _requirement_type(row: Mapping[str, Any]) -> str:
        value = _text(row.get('type') or row.get('mode'))
        return 'mandatory_all' if value == 'all' else value

    class RecommendationView:

        def __init__(self, payload: Mapping[str, Any] | None=None) -> None:
            row = dict(payload or {})
            self.selection_scores = {_text(key): _finite(value) for key, value in (row.get('selection_scores') or {}).items() if _text(key)}
            self.successors: dict[str, dict[str, float]] = {}
            for source, values in (row.get('successor_top_k') or {}).items():
                self.successors[_text(source)] = {_text(item.get('target_id')): _finite(item.get('score')) for item in values or [] if _text(item.get('target_id'))}
            self.pair_rankings: dict[str, list[tuple[tuple[str, str], float]]] = {}
            for requirement_id, values in (row.get('pair_selection_top_k') or {}).items():
                ranked: list[tuple[tuple[str, str], float]] = []
                for item in values or []:
                    target_ids = sorted({_text(value) for value in item.get('target_ids', []) or [] if _text(value)})
                    if len(target_ids) == 2:
                        ranked.append(((target_ids[0], target_ids[1]), _finite(item.get('score'))))
                self.pair_rankings[_text(requirement_id)] = sorted(ranked, key=lambda value: (-value[1], value[0]))

        def selection(self, target_id: str, fallback: float=0.0) -> float:
            return self.selection_scores.get(target_id, fallback)

        def transition(self, left: str | None, right: str | None) -> float:
            if not left or not right:
                return 0.0
            return self.successors.get(left, {}).get(right, 0.0)

        def insertion(self, left: str | None, target: str, right: str | None) -> float:
            score = self.transition(left, target) + self.transition(target, right)
            if left and right:
                score -= self.transition(left, right)
            return score

        def pairs(self, requirement_id: str) -> list[tuple[tuple[str, str], float]]:
            return self.pair_rankings.get(requirement_id, [])


    def _mandatory_targets(floor: Mapping[str, Any]) -> set[str]:
        selected: set[str] = set()
        for requirement in floor.get('requirements', []) or []:
            if _requirement_type(requirement) == 'mandatory_all':
                selected.update(map(_text, requirement.get('candidate_ids', []) or []))
        return {target_id for target_id in selected if target_id}

    def select_exact_rule_targets(floor: Mapping[str, Any], candidates: Mapping[str, Mapping[str, Any]], recommendations: RecommendationView) -> tuple[set[str], dict[str, str]]:
        """Apply hard counts; learned scores only rank otherwise legal choices."""
        selected = _mandatory_targets(floor)
        reason = {target_id: 'mandatory_all' for target_id in selected}

        def rank(ids: Sequence[str]) -> list[str]:
            valid = [item for item in dict.fromkeys(map(_text, ids)) if item in candidates]
            return sorted(valid, key=lambda target_id: (-recommendations.selection(target_id, _finite(candidates[target_id].get('u_rule'), 0.0)), target_id))
        for requirement in floor.get('requirements', []) or []:
            kind = _requirement_type(requirement)
            if kind in {'mandatory_all', 'distinct_category_group'}:
                continue
            ids = rank(requirement.get('candidate_ids', []) or [])
            required = max(0, int(requirement.get('required_count') or requirement.get('quota') or 0))
            already = len(set(ids) & selected)
            needed = max(0, required - already)
            requirement_id = _text(requirement.get('requirement_id'))
            available = {item for item in ids if item not in selected}
            if needed == 2:
                for (left_id, right_id), _score in recommendations.pairs(requirement_id):
                    if left_id in available and right_id in available:
                        for target_id in (left_id, right_id):
                            selected.add(target_id)
                            reason[target_id] = requirement_id or kind
                        needed = 0
                        break
            for target_id in [item for item in ids if item not in selected][:needed]:
                selected.add(target_id)
                reason[target_id] = requirement_id or kind
        for requirement in floor.get('requirements', []) or []:
            if _requirement_type(requirement) != 'distinct_category_group':
                continue
            categories = requirement.get('categories') or {}
            required_value = requirement.get('required_distinct_categories')
            required_categories = max(0, int(required_value if required_value is not None else 2))
            instances_per_category = max(1, int(requirement.get('instances_per_category') or 1))
            ranked_categories: list[tuple[float, str, list[str]]] = []
            if isinstance(categories, Mapping):
                for category, raw_ids in categories.items():
                    ids = rank(raw_ids or [])
                    if len(ids) < instances_per_category:
                        continue
                    chosen = ids[:instances_per_category]
                    score = sum((recommendations.selection(target_id, _finite(candidates[target_id].get('u_rule'), 0.0)) for target_id in chosen))
                    ranked_categories.append((score, _text(category), chosen))
            for _score, category, chosen in sorted(ranked_categories, key=lambda row: (-row[0], row[1]))[:required_categories]:
                for target_id in chosen:
                    selected.add(target_id)
                    reason[target_id] = f"{_text(requirement.get('requirement_id'))}:{category}"
        return (selected, reason)










    __all__ = ['RecommendationView', 'select_exact_rule_targets']
    return dict(locals())

_s09_chain = _register_embedded_module(
    'fire_inspection_system.semantic.chain_route_planner',
    _build_s09_chain(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/dual_graph/planner.py
# -----------------------------------------------------------------------------
def _build_s09_planner():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/dual_graph/planner.py'
    )
    __name__ = 'fire_inspection_system.dual_graph.planner'
    __package__ = 'fire_inspection_system.dual_graph'
    """Hierarchical dual-graph inspection route planning.

    The upper graph contains Area and Target nodes.  R-GCN recommendations rank
    otherwise legal target choices and high-value Area nodes.  The lower graph is
    the certified physical navigation graph.  A precomputed physical Metric
    Closure is consumed by the solver; its construction is explicitly reported
    outside planning time.

    The resulting route is an Area-tree-supported physical walk.  It is neither a
    simple target path nor a bounded-degree support graph: shortest physical paths
    may revisit nodes and edges while the selected targets remain unique.
    """
    import argparse
    import copy
    import heapq
    import json
    import math
    import re
    import time
    from collections import defaultdict
    from dataclasses import asdict, dataclass
    from pathlib import Path
    from typing import Any, Mapping, Sequence
    from shapely.geometry import LineString, Point
    from shapely.prepared import prep
    from fire_inspection_system.last.pipeline import build_last_inspection_route
    from fire_inspection_system.last.vector_free_space import VectorFreeSpaceIndex
    from fire_inspection_system.dual_graph.physical_access import augment_virtual_target_access_nodes
    from fire_inspection_system.semantic.chain_route_planner import RecommendationView, select_exact_rule_targets
    from fire_inspection_system.semantic.physical_metric_closure import ClosureBuildConfig, PhysicalMetricClosure, build_physical_metric_closure
    EPS = 1e-09

    def _text(value: Any) -> str:
        return '' if value is None else str(value).strip()

    def _finite(value: Any, default: float=0.0) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if math.isfinite(number) else default

    def _read(path: Path | str) -> Any:
        return json.loads(Path(path).resolve().read_text(encoding='utf-8'))

    def _write(path: Path | str, value: Any) -> Path:
        output = Path(path).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        return output

    def materialize_physical_access_candidates(candidates_bundle: Mapping[str, Any], certified_graph: Mapping[str, Any], closure: PhysicalMetricClosure) -> tuple[dict[str, Any], dict[str, Any]]:
        """Lock every routable target to the exact node used by the metric closure.

        The selector bundle may contain a grid-centre ``access_point`` inherited
        from an older raster attachment stage.  Those coordinates must not be
        mixed with distances computed on the physical graph.  This function makes
        the lower graph authoritative for both planning and final expansion.
        """
        result = copy.deepcopy(dict(candidates_bundle))
        graph_nodes = {_text(row.get('node_id')): row for row in certified_graph.get('nodes', []) or [] if _text(row.get('node_id'))}
        floor_audit: dict[str, Any] = {}
        for floor_id, floor in (result.get('floors') or {}).items():
            materialized = 0
            missing_closure = 0
            missing_graph_node = 0
            for candidate in floor.get('candidates', []) or []:
                target_id = _text(candidate.get('target_id'))
                if not target_id or floor_id not in closure.floor_ids() or (not closure.has_target(floor_id, target_id)):
                    missing_closure += 1
                    continue
                node_id = closure.target_node_id(floor_id, target_id)
                node = graph_nodes.get(node_id)
                if node is None:
                    missing_graph_node += 1
                    continue
                x, y = (_finite(node.get('x'), math.nan), _finite(node.get('y'), math.nan))
                if not math.isfinite(x) or not math.isfinite(y):
                    missing_graph_node += 1
                    continue
                candidate.update({'source_access_node_id': node_id, 'access_node_id': node_id, 'access_point': [x, y], 'physical_anchor_point': [x, y], 'virtual_access': bool(node.get('virtual_access')), 'virtual_access_distance': _finite(node.get('projection_distance'), _finite(candidate.get('virtual_access_distance'), 0.0)), 'virtual_anchor_node_id': _text(node.get('virtual_anchor_node_id')), 'access_backend': 'certified_physical_navigation_graph', 'physical_graph_access_materialized': True})
                materialized += 1
            floor_audit[str(floor_id)] = {'candidate_count': len(floor.get('candidates', []) or []), 'materialized_physical_access_count': materialized, 'missing_from_physical_closure_count': missing_closure, 'missing_physical_node_count': missing_graph_node}
        return (result, {'policy': 'metric_closure_target_node_is_authoritative', 'raster_or_grid_access_used_for_forwarding': False, 'floors': floor_audit})

    def _requirement_type(row: Mapping[str, Any]) -> str:
        value = _text(row.get('type') or row.get('mode'))
        return 'mandatory_all' if value == 'all' else value

    @dataclass(frozen=True)
    class DualGraphConfig:
        area_value_bias: float = 0.35
        transition_score_weight: float = 0.03
        local_two_opt_rounds: int = 4
        local_two_opt_span: int = 24
        mandatory_value_bonus: float = 1.0
        dmax_ratio: float = 0.8
        require_complete_rgcn_scores: bool = True
        floor_ids: tuple[str, ...] = ()
        write_dxf: bool = True
        expand_physical_walk: bool = True

    def certify_physical_graph(graph_path: Path | str, effective_free_areas_path: Path | str, output_path: Path | str, candidate_bundle: Mapping[str, Any] | None=None) -> tuple[Path, dict[str, Any]]:
        """Remove every physical edge not covered by effective vector free space."""
        graph_source = Path(graph_path).resolve()
        payload = _read(graph_source)
        virtual_access_audit: dict[str, Any] = {'policy': 'disabled', 'raster_or_grid_used': False, 'added_virtual_target_count': 0, 'unavailable_target_count': 0}
        if candidate_bundle is not None:
            payload, virtual_access_audit = augment_virtual_target_access_nodes(payload, candidate_bundle)
        free_spaces = VectorFreeSpaceIndex(effective_free_areas_path)
        prepared = {floor_id: prep(geometry) for floor_id, geometry in free_spaces.floors.items()}
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for index, raw in enumerate(payload.get('edges', []) or [], 1):
            edge = dict(raw)
            floor_id = _text(edge.get('floor_id'))
            coordinates = (edge.get('geometry') or {}).get('coordinates') or []
            valid = False
            if floor_id in prepared and len(coordinates) >= 2:
                points = [(float(row[0]), float(row[1])) for row in coordinates]
                if all((math.dist(points[0], point) <= EPS for point in points[1:])):
                    valid = prepared[floor_id].covers(Point(points[0]))
                else:
                    valid = prepared[floor_id].covers(LineString(points))
            if valid:
                edge['dual_graph_vector_certified'] = True
                accepted.append(edge)
            else:
                rejected.append({'edge_id': _text(edge.get('edge_id')) or f'EDGE_{index:08d}', 'floor_id': floor_id, 'reason': 'outside_effective_free_area_or_missing_geometry'})
        certified = {**payload, 'graph_type': 'vector_certified_refined_physical_navigation_graph', 'edges': accepted, 'dual_graph_certification': {'source_graph': str(graph_source), 'effective_free_areas': str(Path(effective_free_areas_path).resolve()), 'input_edge_count': len(payload.get('edges', []) or []), 'accepted_edge_count': len(accepted), 'rejected_edge_count': len(rejected), 'rejected_edges': rejected, 'virtual_access': virtual_access_audit}}
        output = _write(output_path, certified)
        return (output, certified['dual_graph_certification'])

    def _recommendation_payload(recommendations: Mapping[str, Any], source_run_id: str, floor_id: str) -> tuple[dict[str, Any], str]:
        floors = recommendations.get('floors', {}) or {}
        direct_keys = (f'{source_run_id}__{floor_id}', floor_id)
        for key in direct_keys:
            if isinstance(floors.get(key), Mapping):
                return (dict(floors[key]), key)
        matches = [(str(key), dict(row)) for key, row in floors.items() if isinstance(row, Mapping) and _text(row.get('source_run_id')) == source_run_id and (_text(row.get('floor_id')) == floor_id)]
        if matches:
            return (matches[0][1], matches[0][0])
        return ({}, '')

    def _component_map(closure: PhysicalMetricClosure, floor_id: str) -> dict[str, int]:
        data = closure._load(floor_id)
        return {target_id: int(data['component_index'][index]) for index, target_id in enumerate(data['target_ids'])}


    def _candidate_score(candidate: Mapping[str, Any], view: RecommendationView) -> tuple[float, str]:
        target_id = _text(candidate.get('target_id'))
        if target_id in view.selection_scores:
            return (view.selection(target_id), 'rgcn_selection_head')
        return (_finite(candidate.get('u_rule'), 0.0), 'explainable_rule_value_fallback')

    ROUTE_START_CLASS_NAMES = frozenset({'安全出口', '消防电梯'})
    FIRE_DOOR_CONTEXT_MARKERS = ('防火门', 'DOOR_FIRE', 'FIRE_DOOR')
    FIRE_DOOR_CODE_PATTERN = re.compile(r'^(?:FM|FGM|FJM)(?:[甲乙丙丁]?\d{0,8}(?:[-_]\d+)?)?$')

    def _candidate_class_name(candidate: Mapping[str, Any]) -> str:
        return _text(
            candidate.get('standard_class_name')
            or candidate.get('class_name')
            or candidate.get('target_class')
        )

    def _is_fire_door_candidate(candidate: Mapping[str, Any]) -> bool:
        """Defensively reject stale fire-door candidates mislabeled as exits."""
        annotation = candidate.get('annotation') or {}
        values = [
            candidate.get('raw_name'),
            candidate.get('original_object_name'),
            candidate.get('term'),
            candidate.get('layer'),
        ]
        if isinstance(annotation, Mapping):
            values.extend(
                annotation.get(key)
                for key in (
                    'raw_name',
                    'original_object_name',
                    'term',
                    'source_class_name',
                    'layer',
                )
            )
        for value in values:
            normalized = _text(value).strip().upper().replace(' ', '')
            if not normalized:
                continue
            if any(marker in normalized for marker in FIRE_DOOR_CONTEXT_MARKERS):
                return True
            if FIRE_DOOR_CODE_PATTERN.fullmatch(normalized):
                return True
        return False

    def _is_route_start_candidate(candidate: Mapping[str, Any]) -> bool:
        return (
            _candidate_class_name(candidate) in ROUTE_START_CLASS_NAMES
            and not _is_fire_door_candidate(candidate)
        )

    def _physical_floor_id(scope_id: str, floor: Mapping[str, Any]) -> str:
        """Return the CAD physical floor behind a building-isolated scope.

        Building isolation names scopes as ``F1__B01``.  Older candidate
        bundles do not carry ``source_floor_id``, so the suffix is also decoded
        here instead of treating every building as a separate physical floor.
        """
        explicit = _text(
            floor.get('source_floor_id')
            or floor.get('physical_floor_id')
            or floor.get('parent_floor_id')
        )
        return explicit or scope_id.split('__', 1)[0]

    def _scope_has_routable_route_start(
        floor_id: str,
        floor: Mapping[str, Any],
        closure: PhysicalMetricClosure,
        recommendation: Mapping[str, Any],
        config: DualGraphConfig,
    ) -> bool:
        candidates = {
            _text(row.get('target_id')): row
            for row in floor.get('candidates', []) or []
            if _text(row.get('target_id'))
        }
        routable_ids = set(candidates) & set(_component_map(closure, floor_id))
        if config.require_complete_rgcn_scores:
            scored = set(RecommendationView(recommendation).selection_scores)
            routable_ids &= scored
        return any(_is_route_start_candidate(candidates[target_id]) for target_id in routable_ids)

    def _select_route_start_target(
        cluster: set[str],
        required: set[str],
        candidates: Mapping[str, Mapping[str, Any]],
        view: RecommendationView,
        floor_id: str,
        closure: PhysicalMetricClosure,
    ) -> str:
        """Choose a legal inspection-object route start inside one Dmax component.

        Existing required targets are preferred so an optional object is not added
        unnecessarily.  Physical distance to the required set is the next-order
        criterion; the R-GCN value only breaks otherwise legal choices.
        """
        eligible = [
            target_id
            for target_id in sorted(cluster)
            if target_id in candidates and _is_route_start_candidate(candidates[target_id])
        ]
        if not eligible:
            return ''

        def rank(target_id: str) -> tuple[int, float, float, str]:
            distances = [
                _distance(closure, floor_id, target_id, other)
                for other in sorted(required)
                if other != target_id
            ]
            finite_distances = [value for value in distances if math.isfinite(value)]
            service_distance = sum(finite_distances) if finite_distances else 0.0
            score = _candidate_score(candidates[target_id], view)[0]
            return (0 if target_id in required else 1, service_distance, -score, target_id)

        return min(eligible, key=rank)

    def _constraint_audit(original_floor: Mapping[str, Any], selected: set[str]) -> tuple[bool, list[dict[str, Any]]]:
        rows: list[dict[str, Any]] = []
        feasible = True
        for requirement in original_floor.get('requirements', []) or []:
            kind = _requirement_type(requirement)
            requirement_id = _text(requirement.get('requirement_id'))
            if kind == 'mandatory_all':
                required_ids = {_text(value) for value in requirement.get('candidate_ids', []) or []}
                missing = sorted(required_ids - selected)
                ok = not missing
                configured_required = int(requirement.get('configured_required_count') or len(required_ids))
                selected_count = len(required_ids & selected)
                row = {
                    'requirement_id': requirement_id,
                    'type': kind,
                    'satisfied': ok,
                    'configured_satisfied': selected_count >= configured_required,
                    'missing': missing,
                    'unavailable_candidate_ids': list(requirement.get('unavailable_candidate_ids') or []),
                    'required_count': len(required_ids),
                    'configured_required_count': configured_required,
                    'selected_count': selected_count,
                    'configured_shortfall': max(0, configured_required - selected_count),
                }
            elif kind == 'distinct_category_group':
                categories = requirement.get('categories') or {}
                per_category = int(requirement.get('instances_per_category') or 1)
                required_value = requirement.get('required_distinct_categories')
                required_categories = int(required_value if required_value is not None else 2)
                configured_required_categories = int(requirement.get('configured_required_distinct_categories') or required_categories)
                satisfied_categories = [category for category, ids in categories.items() if len(selected & {_text(value) for value in ids or []}) >= per_category] if isinstance(categories, Mapping) else []
                ok = len(satisfied_categories) >= required_categories
                configured_ok = len(satisfied_categories) >= configured_required_categories
                row = {
                    'requirement_id': requirement_id,
                    'type': kind,
                    'satisfied': ok,
                    'configured_satisfied': configured_ok,
                    'required_distinct_categories': required_categories,
                    'configured_required_distinct_categories': configured_required_categories,
                    'satisfied_categories': satisfied_categories,
                    'configured_shortfall': max(0, configured_required_categories - len(satisfied_categories)),
                    'unavailable_categories': list(requirement.get('unavailable_categories') or []),
                }
            else:
                candidate_ids = {_text(value) for value in requirement.get('candidate_ids', []) or []}
                required = int(requirement.get('required_count') or requirement.get('quota') or 0)
                configured_required = int(requirement.get('configured_quota') or requirement.get('configured_required_count') or required)
                actual = len(selected & candidate_ids)
                ok = actual >= required
                row = {
                    'requirement_id': requirement_id,
                    'type': kind,
                    'satisfied': ok,
                    'configured_satisfied': actual >= configured_required,
                    'required_count': required,
                    'configured_required_count': configured_required,
                    'selected_count': actual,
                    'configured_shortfall': max(0, configured_required - actual),
                }
            feasible = feasible and ok
            rows.append(row)
        return (feasible, rows)

    def _reportable_constraint_shortfalls(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Keep only active, partially satisfiable requirements for later reporting."""
        result: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            if row.get('configured_satisfied') is not False and row.get('satisfied', False):
                continue
            kind = _text(row.get('type'))
            if kind == 'distinct_category_group':
                active = bool(row.get('satisfied_categories')) or int(row.get('required_distinct_categories') or 0) > 0
            else:
                active = int(row.get('required_count') or 0) > 0 or int(row.get('selected_count') or 0) > 0
            if active:
                result.append(row)
        return result

    def _distance(closure: PhysicalMetricClosure, floor_id: str, left: str, right: str) -> float:
        return closure.distance(floor_id, left, right)

    def _sequence_length(order: Sequence[str], floor_id: str, closure: PhysicalMetricClosure) -> float:
        total = 0.0
        for left, right in zip(order, order[1:]):
            value = _distance(closure, floor_id, left, right)
            if not math.isfinite(value):
                return math.inf
            total += value
        return total

    def _two_opt_block(order: Sequence[str], floor_id: str, closure: PhysicalMetricClosure, previous_target: str | None, config: DualGraphConfig) -> list[str]:
        result = list(order)
        if len(result) < 3:
            return result

        def objective(values: Sequence[str]) -> float:
            prefix = [previous_target] if previous_target else []
            return _sequence_length(prefix + list(values), floor_id, closure)
        current = objective(result)
        for _ in range(max(0, config.local_two_opt_rounds)):
            improved = False
            for left in range(len(result) - 1):
                stop = min(len(result), left + max(3, config.local_two_opt_span))
                for right in range(left + 2, stop + 1):
                    proposal = result[:left] + list(reversed(result[left:right])) + result[right:]
                    value = objective(proposal)
                    if value + EPS < current:
                        result, current, improved = (proposal, value, True)
            if not improved:
                break
        return result

    def _area_graph(selected: set[str], candidates: Mapping[str, Mapping[str, Any]], view: RecommendationView, floor_id: str, closure: PhysicalMetricClosure, config: DualGraphConfig) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, float]]:
        targets_by_area: dict[str, list[str]] = defaultdict(list)
        area_values: dict[str, float] = defaultdict(float)
        target_rows: list[dict[str, Any]] = []
        for target_id in sorted(selected):
            candidate = candidates[target_id]
            area_id = _text(candidate.get('area_id')) or f'UNASSIGNED::{target_id}'
            targets_by_area[area_id].append(target_id)
            score, source = _candidate_score(candidate, view)
            value = score + (config.mandatory_value_bonus if bool(candidate.get('mandatory')) else 0.0)
            area_values[area_id] += value
            target_rows.append({'node_id': f'TARGET::{target_id}', 'node_type': 'target', 'target_id': target_id, 'area_id': area_id, 'class_name': _text(candidate.get('class_name')), 'mandatory': bool(candidate.get('mandatory')), 'selection_value': score, 'value_source': source, 'selected': True})
        area_nodes = [{'node_id': f'AREA::{area_id}', 'node_type': 'area', 'area_id': area_id, 'target_count': len(targets_by_area[area_id]), 'aggregate_value': area_values[area_id]} for area_id in sorted(targets_by_area)]
        membership_edges = [{'edge_type': 'target_in_area', 'source': f'TARGET::{target_id}', 'target': f'AREA::{area_id}'} for area_id, target_ids in sorted(targets_by_area.items()) for target_id in target_ids]
        proximity_edges: list[dict[str, Any]] = []
        areas = sorted(targets_by_area)
        for left_index, left_area in enumerate(areas):
            for right_area in areas[left_index + 1:]:
                best: tuple[float, str, str] | None = None
                for left in targets_by_area[left_area]:
                    for right in targets_by_area[right_area]:
                        value = _distance(closure, floor_id, left, right)
                        if math.isfinite(value) and (best is None or (value, left, right) < best):
                            best = (value, left, right)
                if best is not None:
                    proximity_edges.append({'edge_type': 'inter_area_physical_proximity', 'source': f'AREA::{left_area}', 'target': f'AREA::{right_area}', 'distance': best[0], 'left_target_id': best[1], 'right_target_id': best[2]})
        return ({'graph_type': 'rgcn_value_area_target_graph', 'floor_id': floor_id, 'nodes': area_nodes + target_rows, 'edges': membership_edges + proximity_edges}, dict(targets_by_area), dict(area_values))

    def _minimum_area_tree(value_graph: Mapping[str, Any], area_values: Mapping[str, float], config: DualGraphConfig, *, root_area_id: str='') -> tuple[str, list[dict[str, Any]], list[str]]:
        areas = sorted(area_values)
        if not areas:
            return ('', [], [])
        root = root_area_id if root_area_id in area_values else max(areas, key=lambda area: (area_values[area], area))
        edge_rows = [row for row in value_graph.get('edges', []) or [] if row.get('edge_type') == 'inter_area_physical_proximity']
        adjacency: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in edge_rows:
            left = _text(row.get('source')).removeprefix('AREA::')
            right = _text(row.get('target')).removeprefix('AREA::')
            adjacency[left].append({**row, 'other': right})
            adjacency[right].append({**row, 'other': left})
        max_value = max(area_values.values()) if area_values else 1.0
        visited = {root}
        tree: list[dict[str, Any]] = []
        while len(visited) < len(areas):
            choices: list[tuple[float, float, str, str, dict[str, Any]]] = []
            for parent in sorted(visited):
                for edge in adjacency.get(parent, []):
                    child = edge['other']
                    if child in visited:
                        continue
                    normalized_value = area_values[child] / max(max_value, EPS)
                    adjusted = float(edge['distance']) / (1.0 + config.area_value_bias * normalized_value)
                    choices.append((adjusted, float(edge['distance']), parent, child, edge))
            if not choices:
                return (root, tree, sorted(set(areas) - visited))
            _adjusted, distance, parent, child, source = min(choices)
            tree.append({'parent_area_id': parent, 'child_area_id': child, 'physical_distance': distance, 'parent_value': area_values[parent], 'child_value': area_values[child], 'bridge_target_ids': [_text(source.get('left_target_id')), _text(source.get('right_target_id'))]})
            visited.add(child)
        return (root, tree, [])

    def _area_preorder(root: str, tree: Sequence[Mapping[str, Any]], area_values: Mapping[str, float]) -> list[str]:
        children: dict[str, list[tuple[float, str]]] = defaultdict(list)
        for edge in tree:
            parent = _text(edge.get('parent_area_id'))
            child = _text(edge.get('child_area_id'))
            distance = _finite(edge.get('physical_distance'), math.inf)
            priority = distance / (1.0 + max(0.0, area_values.get(child, 0.0)))
            children[parent].append((priority, child))
        order: list[str] = []

        def visit(area_id: str) -> None:
            order.append(area_id)
            for _priority, child in sorted(children.get(area_id, []), key=lambda row: (row[0], -area_values.get(row[1], 0.0), row[1])):
                visit(child)
        if root:
            visit(root)
        return order

    def _local_area_order(target_ids: Sequence[str], previous_target: str | None, floor_id: str, closure: PhysicalMetricClosure, view: RecommendationView, candidates: Mapping[str, Mapping[str, Any]], config: DualGraphConfig) -> list[str]:
        remaining = set(target_ids)
        if not remaining:
            return []
        if previous_target:
            current = min(remaining, key=lambda target_id: (_distance(closure, floor_id, previous_target, target_id), -_candidate_score(candidates[target_id], view)[0], target_id))
        else:
            current = max(remaining, key=lambda target_id: (_candidate_score(candidates[target_id], view)[0], target_id))
        order = [current]
        remaining.remove(current)
        while remaining:
            left = order[-1]
            distances = {target_id: _distance(closure, floor_id, left, target_id) for target_id in remaining}
            finite_values = [value for value in distances.values() if math.isfinite(value)]
            scale = max(finite_values) if finite_values else 1.0
            current = min(remaining, key=lambda target_id: (distances[target_id] - config.transition_score_weight * scale * view.transition(left, target_id), distances[target_id], target_id))
            order.append(current)
            remaining.remove(current)
        return _two_opt_block(order, floor_id, closure, previous_target, config)

    def _filter_floor_to_target_ids(floor: Mapping[str, Any], allowed: set[str]) -> dict[str, Any]:
        filtered = copy.deepcopy(dict(floor))
        filtered['candidates'] = [dict(row) for row in floor.get('candidates', []) or [] if _text(row.get('target_id')) in allowed]
        requirements = []
        for raw in floor.get('requirements', []) or []:
            row = copy.deepcopy(dict(raw))
            original_candidate_ids = list(row.get('candidate_ids', []) or [])
            row['candidate_ids'] = [value for value in original_candidate_ids if _text(value) in allowed]
            if isinstance(row.get('categories'), Mapping):
                row['categories'] = {category: [value for value in ids or [] if _text(value) in allowed] for category, ids in row['categories'].items()}
            kind = _requirement_type(row)
            if kind == 'distinct_category_group':
                per_category = max(1, int(row.get('instances_per_category') or 1))
                configured_required = max(
                    0,
                    int(
                        row.get('configured_required_distinct_categories')
                        or row.get('required_distinct_categories')
                        or 0
                    ),
                )
                available_categories = [
                    str(category)
                    for category, ids in (row.get('categories') or {}).items()
                    if len(ids or []) >= per_category
                ]
                row['configured_required_distinct_categories'] = configured_required
                row['required_distinct_categories'] = min(configured_required, len(available_categories))
                row['available_category_count'] = len(available_categories)
                row['available_categories'] = available_categories
                row['unavailable_categories'] = [
                    str(category)
                    for category in (row.get('categories') or {})
                    if str(category) not in set(available_categories)
                ]
                row['availability_shortfall'] = max(0, configured_required - len(available_categories))
            elif kind == 'mandatory_all':
                row['configured_required_count'] = int(
                    row.get('configured_required_count')
                    or row.get('required_count')
                    or len(original_candidate_ids)
                )
                row['required_count'] = len(row['candidate_ids'])
                row['available_count'] = len(row['candidate_ids'])
                row['unavailable_candidate_ids'] = [
                    value for value in original_candidate_ids if _text(value) not in allowed
                ]
                row['availability_shortfall'] = max(
                    0,
                    row['configured_required_count'] - len(row['candidate_ids']),
                )
            else:
                configured_required = max(
                    0,
                    int(
                        row.get('configured_quota')
                        or row.get('configured_required_count')
                        or row.get('required_count')
                        or row.get('quota')
                        or 0
                    ),
                )
                row['configured_required_count'] = configured_required
                row['required_count'] = min(
                    max(0, int(row.get('required_count') or row.get('quota') or 0)),
                    len(row['candidate_ids']),
                )
                row['available_count'] = len(row['candidate_ids'])
                row['availability_shortfall'] = max(0, configured_required - len(row['candidate_ids']))
            requirements.append(row)
        filtered['requirements'] = requirements
        return filtered

    def _floor_scale(candidates: Mapping[str, Mapping[str, Any]]) -> float:
        points: list[tuple[float, float]] = []
        for row in candidates.values():
            value = row.get('point') or row.get('physical_anchor_point') or row.get('access_point')
            if isinstance(value, (list, tuple)) and len(value) >= 2:
                x, y = (_finite(value[0], math.nan), _finite(value[1], math.nan))
                if math.isfinite(x) and math.isfinite(y):
                    points.append((x, y))
        if not points:
            return 1.0
        xs, ys = ([row[0] for row in points], [row[1] for row in points])
        return max(max(xs) - min(xs), max(ys) - min(ys), 1.0)

    def _physical_floor_scales(graph: Mapping[str, Any]) -> dict[str, float]:
        positioned: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for row in graph.get('nodes', []) or []:
            floor_id = _text(row.get('floor_id'))
            x, y = (_finite(row.get('x'), math.nan), _finite(row.get('y'), math.nan))
            if floor_id and math.isfinite(x) and math.isfinite(y):
                positioned[floor_id].append((x, y))
        result = {}
        for floor_id, points in positioned.items():
            xs, ys = ([row[0] for row in points], [row[1] for row in points])
            result[floor_id] = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
        return result

    def _dmax_adjacency(target_ids: Sequence[str], floor_id: str, closure: PhysicalMetricClosure, dmax: float) -> dict[str, list[tuple[str, float]]]:
        ids = sorted(set(target_ids))
        adjacency: dict[str, list[tuple[str, float]]] = {target_id: [] for target_id in ids}
        for index, left in enumerate(ids):
            for right in ids[index + 1:]:
                distance = _distance(closure, floor_id, left, right)
                if math.isfinite(distance) and distance <= dmax + EPS:
                    adjacency[left].append((right, distance))
                    adjacency[right].append((left, distance))
        return adjacency

    def _connect_required_targets_with_dmax_relays(required: set[str], eligible: set[str], root: str, floor_id: str, closure: PhysicalMetricClosure, view: RecommendationView, candidates: Mapping[str, Mapping[str, Any]], dmax: float) -> tuple[set[str], list[dict[str, Any]], dict[str, Any]]:
        """Build a target-level tree whose every edge is at most Dmax.

        Optional high-value inspection targets may be inserted as relays.  This is
        a tree-connection heuristic on the exact physical Metric Closure; no
        Euclidean, raster or off-graph distance is consulted.
        """
        if not required:
            return (set(), [], {'feasible': False, 'reason': 'no_required_targets'})
        adjacency = _dmax_adjacency(sorted(eligible), floor_id, closure, dmax)
        connected = {root}
        remaining = set(required) - connected
        tree_pairs: list[tuple[str, str, float]] = []
        while remaining:
            distances = {target_id: 0.0 for target_id in connected}
            previous: dict[str, tuple[str, float]] = {}
            queue = [(0.0, target_id) for target_id in sorted(connected)]
            heapq.heapify(queue)
            reached = ''
            while queue:
                cost, current = heapq.heappop(queue)
                if cost > distances.get(current, math.inf) + EPS:
                    continue
                if current in remaining:
                    reached = current
                    break
                for neighbour, physical_distance in adjacency.get(current, []):
                    neighbour_value = max(0.0, min(1.0, _candidate_score(candidates[neighbour], view)[0]))
                    transition = max(0.0, min(1.0, view.transition(current, neighbour)))
                    value_discount = 1.0 - 0.08 * neighbour_value - 0.04 * transition
                    proposal = cost + physical_distance * max(0.8, value_discount)
                    if proposal + EPS < distances.get(neighbour, math.inf):
                        distances[neighbour] = proposal
                        previous[neighbour] = (current, physical_distance)
                        heapq.heappush(queue, (proposal, neighbour))
            if not reached:
                return (connected, [{'source_target_id': left, 'target_target_id': right, 'distance': distance} for left, right, distance in tree_pairs], {'feasible': False, 'reason': 'selected_targets_disconnected_under_dmax_even_with_rgcn_scored_relays', 'unconnected_required_target_ids': sorted(remaining), 'dmax': dmax})
            reverse_path = [reached]
            while reverse_path[-1] not in connected:
                parent, _distance_value = previous[reverse_path[-1]]
                reverse_path.append(parent)
            path = list(reversed(reverse_path))
            for left, right in zip(path, path[1:]):
                distance = next((value for neighbour, value in adjacency[left] if neighbour == right))
                tree_pairs.append((left, right, distance))
            connected.update(path)
            remaining -= connected
        tree_rows = [{'source_target_id': left, 'target_target_id': right, 'distance': distance, 'within_dmax': distance <= dmax + EPS} for left, right, distance in tree_pairs]
        relays = connected - required
        return (connected, tree_rows, {'feasible': True, 'dmax': dmax, 'required_target_count': len(required), 'support_target_count': len(connected), 'relay_target_ids': sorted(relays), 'relay_target_count': len(relays), 'maximum_tree_edge_distance': max((row[2] for row in tree_pairs), default=0.0)})

    def _open_tree_walk(root: str, tree_edges: Sequence[Mapping[str, Any]], preferred_order: Sequence[str], view: RecommendationView) -> tuple[list[str], str]:
        adjacency: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for row in tree_edges:
            left = _text(row.get('source_target_id'))
            right = _text(row.get('target_target_id'))
            distance = _finite(row.get('distance'), math.inf)
            adjacency[left].append((right, distance))
            adjacency[right].append((left, distance))
        if not adjacency:
            return ([root] if root else [], root)
        parent = {root: ''}
        parent_edge: dict[str, float] = {}
        stack = [root]
        traversal = []
        while stack:
            current = stack.pop()
            traversal.append(current)
            for neighbour, distance in adjacency[current]:
                if neighbour in parent:
                    continue
                parent[neighbour] = current
                parent_edge[neighbour] = distance
                stack.append(neighbour)
        root_distance = {root: 0.0}
        for node in traversal[1:]:
            root_distance[node] = root_distance[parent[node]] + parent_edge[node]
        leaves = [node for node in parent if node != root and len(adjacency[node]) == 1]
        endpoint = max(leaves or [root], key=lambda node: (root_distance[node], node))
        endpoint_path = set()
        current = endpoint
        while current:
            endpoint_path.add(current)
            current = parent.get(current, '')
        preferred_rank = {target_id: index for index, target_id in enumerate(preferred_order)}
        walk = [root]

        def visit(node: str) -> None:
            children = [row for row in adjacency[node] if parent.get(row[0]) == node]
            final_children = [row for row in children if row[0] in endpoint_path]
            ordinary = [row for row in children if row[0] not in endpoint_path]
            ordinary.sort(key=lambda row: (preferred_rank.get(row[0], len(preferred_rank) + 1), row[1] - row[1] * 0.03 * view.transition(node, row[0]), row[0]))
            for child, _distance_value in ordinary:
                walk.append(child)
                visit(child)
                walk.append(node)
            for child, _distance_value in final_children:
                walk.append(child)
                visit(child)
        visit(root)
        return (walk, endpoint)

    def _first_visits(order: Sequence[str]) -> list[str]:
        seen: set[str] = set()
        result = []
        for target_id in order:
            if target_id not in seen:
                seen.add(target_id)
                result.append(target_id)
        return result

    def _dmax_target_clusters(target_ids: Sequence[str], floor_id: str, closure: PhysicalMetricClosure, dmax: float) -> list[set[str]]:
        """Connected components of the exact physical Metric Closure at Dmax."""
        adjacency = _dmax_adjacency(target_ids, floor_id, closure, dmax)
        remaining = set(adjacency)
        clusters: list[set[str]] = []
        while remaining:
            seed = min(remaining)
            component = {seed}
            stack = [seed]
            remaining.remove(seed)
            while stack:
                current = stack.pop()
                for neighbour, _distance_value in adjacency.get(current, []):
                    if neighbour in remaining:
                        remaining.remove(neighbour)
                        component.add(neighbour)
                        stack.append(neighbour)
            clusters.append(component)
        return clusters

    def _plan_floor(
        floor_id: str,
        floor: Mapping[str, Any],
        closure: PhysicalMetricClosure,
        recommendation: Mapping[str, Any],
        recommendation_key: str,
        config: DualGraphConfig,
        *,
        physical_floor_id: str = '',
        physical_floor_primary_scope_id: str = '',
        require_physical_floor_route_start: bool = True,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        physical_floor_id = physical_floor_id or _physical_floor_id(floor_id, floor)
        physical_floor_primary_scope_id = physical_floor_primary_scope_id or floor_id
        candidates = {_text(row.get('target_id')): dict(row) for row in floor.get('candidates', []) or [] if _text(row.get('target_id'))}
        view = RecommendationView(recommendation)
        components = _component_map(closure, floor_id)
        routable_ids = set(candidates) & set(components)
        rgcn_coverage = {target_id for target_id in routable_ids if target_id in view.selection_scores}
        missing_rgcn_scores = sorted(routable_ids - rgcn_coverage)
        if config.require_complete_rgcn_scores:
            filtered_floor = _filter_floor_to_target_ids(floor, rgcn_coverage)
            filtered_candidates = {_text(row.get('target_id')): dict(row) for row in filtered_floor.get('candidates', []) or []}
            if not recommendation_key:
                return ({'floor_id': floor_id, 'status': 'infeasible_rgcn_coverage', 'feasible': False, 'reason': 'same_floor_rgcn_recommendation_missing', 'rgcn_recommendation_key': recommendation_key, 'rgcn_scored_candidate_count': len(rgcn_coverage), 'candidate_count': len(routable_ids), 'missing_rgcn_score_target_ids': missing_rgcn_scores, 'order': [], 'selected_target_ids': [], 'solver': 'dual_graph_rgcn_area_tree_dmax_walk'}, {'graph_type': 'rgcn_value_area_target_graph', 'floor_id': floor_id, 'nodes': [], 'edges': []})
        else:
            filtered_floor = _filter_floor_to_target_ids(floor, routable_ids)
            filtered_candidates = {_text(row.get('target_id')): dict(row) for row in filtered_floor.get('candidates', []) or []}
        selected, selection_reason = select_exact_rule_targets(filtered_floor, filtered_candidates, view)
        constraints_ok, constraint_rows = _constraint_audit(filtered_floor, selected)
        constraint_shortfalls = _reportable_constraint_shortfalls(constraint_rows)
        configured_constraints_ok = all(
            row.get('configured_satisfied', row.get('satisfied', False))
            for row in constraint_rows
        )
        if not selected:
            return ({'floor_id': floor_id, 'status': 'infeasible_no_routable_inspection_targets', 'feasible': False, 'reason': 'no_routable_rgcn_scored_inspection_target_is_available', 'hard_constraint_audit': constraint_rows, 'inspection_constraint_shortfalls': constraint_shortfalls, 'effective_inspection_constraints_satisfied': constraints_ok, 'configured_inspection_constraints_satisfied': configured_constraints_ok, 'rgcn_scored_candidate_count': len(rgcn_coverage), 'candidate_count': len(routable_ids), 'missing_rgcn_score_target_ids': missing_rgcn_scores, 'order': [], 'selected_target_ids': [], 'solver': 'dual_graph_rgcn_area_tree_dmax_walk'}, {'graph_type': 'rgcn_value_area_target_graph', 'floor_id': floor_id, 'nodes': [], 'edges': []})
        dmax = config.dmax_ratio * max(_finite(floor.get('floor_scale'), 1.0), _floor_scale(filtered_candidates))
        clusters = _dmax_target_clusters(sorted(filtered_candidates), floor_id, closure, dmax)
        selected_clusters = [cluster for cluster in clusters if cluster & selected]
        selected_clusters.sort(key=lambda cluster: (-sum((_candidate_score(filtered_candidates[target_id], view)[0] for target_id in cluster & selected)), min(cluster & selected)))
        floor_route_start_target = ''
        floor_route_start_cluster_index = -1
        for cluster_index, cluster in enumerate(selected_clusters):
            candidate = _select_route_start_target(
                cluster,
                set(selected) & cluster,
                filtered_candidates,
                view,
                floor_id,
                closure,
            )
            if candidate:
                floor_route_start_target = candidate
                floor_route_start_cluster_index = cluster_index
                break
        if not floor_route_start_target:
            # A legal floor entrance may be its own Dmax cluster and therefore
            # contain no rule-selected target.  It must still form the first
            # route segment instead of making the whole floor look entrance-less.
            for cluster in clusters:
                if cluster in selected_clusters:
                    continue
                candidate = _select_route_start_target(
                    cluster,
                    set(),
                    filtered_candidates,
                    view,
                    floor_id,
                    closure,
                )
                if candidate:
                    selected_clusters.append(cluster)
                    floor_route_start_target = candidate
                    floor_route_start_cluster_index = len(selected_clusters) - 1
                    break
        if not floor_route_start_target and not require_physical_floor_route_start:
            # A secondary building scope is a deliberately disconnected route.
            # Its first required object is the audited virtual continuation
            # anchor when that building does not independently contain an exit.
            start_cluster = selected_clusters[0]
            local_required = sorted(set(selected) & start_cluster)
            floor_route_start_target = max(
                local_required,
                key=lambda target_id: (_candidate_score(filtered_candidates[target_id], view)[0], target_id),
            )
            floor_route_start_cluster_index = 0
        route_start_policy = {
            'mode': 'hard_constraint_for_first_route_segment_per_physical_floor',
            'allowed_class_names': sorted(ROUTE_START_CLASS_NAMES),
            'fire_door_candidates_forbidden': True,
            'ordinary_target_fallback_allowed_for_physical_floor_start': False,
            'secondary_building_scope_ordinary_virtual_anchor_allowed': True,
            'later_disconnected_segments_use_virtual_continuation': True,
            'virtual_continuation_is_physical_path': False,
        }
        value_graph = {'graph_type': 'rgcn_value_area_target_graph', 'structure': 'same_floor_area_target_forest', 'floor_id': floor_id, 'nodes': [], 'edges': [], 'segments': []}
        if not floor_route_start_target:
            selected_cluster_target_ids = set(filtered_candidates)
            rejected_fire_door_ids = sorted(
                target_id
                for target_id in selected_cluster_target_ids
                if target_id in filtered_candidates
                and _candidate_class_name(filtered_candidates[target_id]) in ROUTE_START_CLASS_NAMES
                and _is_fire_door_candidate(filtered_candidates[target_id])
            )
            return ({
                'floor_id': floor_id,
                'status': 'infeasible_route_start',
                'feasible': False,
                'reason': 'floor_has_no_routable_safety_exit_or_fire_elevator_start',
                'physical_floor_id': physical_floor_id,
                'building_scope_id': floor_id,
                'physical_floor_primary_scope_id': physical_floor_primary_scope_id,
                'required_target_ids': sorted(selected),
                'selected_cluster_candidate_ids': sorted(selected_cluster_target_ids),
                'rejected_fire_door_candidate_ids': rejected_fire_door_ids,
                'route_start_policy': route_start_policy,
                'order': [],
                'selected_target_ids': sorted(selected),
                'solver': 'dual_graph_rgcn_area_forest_dmax_walk',
            }, value_graph)
        if floor_route_start_cluster_index > 0:
            start_cluster = selected_clusters.pop(floor_route_start_cluster_index)
            selected_clusters.insert(0, start_cluster)
        floor_route_start_component_id = components.get(floor_route_start_target)
        graph_nodes: dict[str, dict[str, Any]] = {}
        graph_edges: dict[str, dict[str, Any]] = {}
        route_segments: list[dict[str, Any]] = []
        support_selected = set(selected)
        preferred_unique_order: list[str] = []
        area_order: list[str] = []
        area_blocks: list[dict[str, Any]] = []
        area_trees: list[dict[str, Any]] = []
        target_trees: list[dict[str, Any]] = []
        root_areas: list[str] = []
        dmax_segment_audits: list[dict[str, Any]] = []
        for segment_index, cluster in enumerate(selected_clusters, 1):
            segment_id = f'{floor_id}_ROUTE_{segment_index:03d}'
            required = set(selected) & cluster
            building_scope_entry_applies = segment_index == 1
            route_start_constraint_applies = (
                building_scope_entry_applies and require_physical_floor_route_start
            )
            root_target = floor_route_start_target if building_scope_entry_applies else ''
            planning_targets = required | ({root_target} if root_target else set())
            root_area_id = (
                (_text(filtered_candidates[root_target].get('area_id')) or f'UNASSIGNED::{root_target}')
                if root_target
                else ''
            )
            segment_graph, targets_by_area, area_values = _area_graph(planning_targets, filtered_candidates, view, floor_id, closure, config)
            root, area_tree, unreachable_areas = _minimum_area_tree(segment_graph, area_values, config, root_area_id=root_area_id)
            if unreachable_areas:
                return ({'floor_id': floor_id, 'status': 'infeasible_area_value_graph', 'feasible': False, 'reason': 'selected_areas_are_disconnected_in_physical_metric_closure', 'unreachable_area_ids': unreachable_areas, 'order': [], 'selected_target_ids': sorted(selected), 'solver': 'dual_graph_rgcn_area_forest_dmax_walk'}, segment_graph)
            local_area_order = _area_preorder(root, area_tree, area_values)
            local_preferred: list[str] = []
            local_blocks: list[dict[str, Any]] = []
            for area_id in local_area_order:
                if building_scope_entry_applies and area_id == root and not local_preferred:
                    remaining_area_targets = [
                        target_id for target_id in targets_by_area[area_id]
                        if target_id != root_target
                    ]
                    local = [root_target] + _local_area_order(
                        remaining_area_targets,
                        root_target,
                        floor_id,
                        closure,
                        view,
                        filtered_candidates,
                        config,
                    )
                else:
                    local = _local_area_order(targets_by_area[area_id], local_preferred[-1] if local_preferred else None, floor_id, closure, view, filtered_candidates, config)
                block = {'segment_id': segment_id, 'area_id': area_id, 'aggregate_value': area_values[area_id], 'target_ids': local, 'entry_target_id': local[0] if local else '', 'exit_target_id': local[-1] if local else ''}
                local_blocks.append(block)
                local_preferred.extend(local)
            if not building_scope_entry_applies:
                root_target = local_preferred[0] if local_preferred else min(required)
            connection_required = required | ({root_target} if building_scope_entry_applies else set())
            support_targets, target_tree, segment_dmax_audit = _connect_required_targets_with_dmax_relays(connection_required, cluster, root_target, floor_id, closure, view, filtered_candidates, dmax)
            if not segment_dmax_audit.get('feasible'):
                return ({'floor_id': floor_id, 'status': 'infeasible_dmax', 'feasible': False, 'reason': segment_dmax_audit.get('reason'), 'required_target_ids': sorted(selected), 'selected_target_ids': sorted(support_selected), 'preferred_unique_order': preferred_unique_order, 'dmax_audit': segment_dmax_audit, 'order': [], 'solver': 'dual_graph_rgcn_area_forest_dmax_walk'}, value_graph)
            if route_start_constraint_applies and root_target not in required:
                selection_reason[root_target] = 'route_start_anchor'
            route_start_anchor_ids = {root_target} if building_scope_entry_applies else set()
            relay_target_ids = support_targets - required - route_start_anchor_ids
            for relay in sorted(relay_target_ids):
                selection_reason.setdefault(relay, 'dmax_rgcn_scored_relay')
            support_selected.update(support_targets)
            local_order, endpoint = _open_tree_walk(root_target, target_tree, local_preferred, view)
            local_distances = [_distance(closure, floor_id, left, right) for left, right in zip(local_order, local_order[1:])]
            physical_component_id = components.get(root_target)
            physically_reachable_from_floor_start = (
                require_physical_floor_route_start
                and physical_component_id == floor_route_start_component_id
            )
            if route_start_constraint_applies:
                continuation_break_reason = ''
            elif building_scope_entry_applies and not require_physical_floor_route_start:
                continuation_break_reason = 'building_scope_disconnected_from_physical_floor_route_start'
            else:
                continuation_break_reason = (
                    'dmax_disconnected_from_floor_route_start'
                    if physically_reachable_from_floor_start
                    else 'physical_graph_disconnected_from_floor_route_start'
                )
            route_segments.append({'segment_id': segment_id, 'floor_id': floor_id, 'physical_floor_id': physical_floor_id, 'building_scope_id': floor_id, 'physical_floor_primary_scope_id': physical_floor_primary_scope_id, 'physical_floor_primary_route': require_physical_floor_route_start, 'physical_component_id': physical_component_id, 'entry_mode': 'floor_route_start_at_inspection_entry_object' if route_start_constraint_applies else 'virtual_continuation_after_physical_or_dmax_disconnect', 'entry_label': '楼层首段：安全出口或消防电梯起点' if route_start_constraint_applies else '物理断开后的虚拟续接', 'virtual_entry_id': '' if route_start_constraint_applies else f'VIRTUAL_ENTRY::{segment_id}', 'virtual_continuation': not route_start_constraint_applies, 'virtual_entry_is_not_physical_path': not route_start_constraint_applies, 'continuation_break_reason': continuation_break_reason, 'physically_reachable_from_floor_route_start': physically_reachable_from_floor_start, 'route_start_constraint_applies': route_start_constraint_applies, 'entry_target_id': root_target, 'entry_target_class_name': _candidate_class_name(filtered_candidates[root_target]), 'entry_target_selected_by_inspection_rules': root_target in required, 'route_start_constraint_satisfied': _is_route_start_candidate(filtered_candidates[root_target]) if route_start_constraint_applies else None, 'ordered_target_ids': local_order, 'first_visit_order': _first_visits(local_order), 'required_target_ids': sorted(required), 'support_target_ids': sorted(support_targets), 'relay_target_ids': sorted(relay_target_ids), 'open_endpoint_target_id': endpoint, 'length': sum(local_distances), 'maximum_continuous_uninspected_distance': max(local_distances, default=0.0), 'dmax_constraint_satisfied': all((value <= dmax + EPS for value in local_distances)), 'route_start_area_id': root, 'root_high_value_area_id': root, 'area_visit_order': local_area_order, 'area_backbone_tree': area_tree, 'target_backbone_tree': target_tree})
            preferred_unique_order.extend(local_preferred)
            area_order.extend(local_area_order)
            area_blocks.extend(local_blocks)
            root_areas.append(root)
            area_trees.extend(({**row, 'segment_id': segment_id} for row in area_tree))
            target_trees.extend(({**row, 'segment_id': segment_id} for row in target_tree))
            dmax_segment_audits.append({**segment_dmax_audit, 'segment_id': segment_id})
            value_graph['segments'].append({'segment_id': segment_id, **segment_graph})
            for row in segment_graph.get('nodes', []) or []:
                graph_nodes.setdefault(_text(row.get('node_id')), dict(row))
            for row in segment_graph.get('edges', []) or []:
                graph_edges.setdefault(_text(row.get('edge_id')), dict(row))
        value_graph['nodes'] = list(graph_nodes.values())
        value_graph['edges'] = list(graph_edges.values())
        selected = support_selected
        constraints_ok, constraint_rows = _constraint_audit(filtered_floor, selected)
        constraint_shortfalls = _reportable_constraint_shortfalls(constraint_rows)
        configured_constraints_ok = all(
            row.get('configured_satisfied', row.get('satisfied', False))
            for row in constraint_rows
        )
        order = [target_id for segment in route_segments for target_id in segment['ordered_target_ids']]
        first_visit_order = _first_visits(order)
        leg_distances = [_distance(closure, floor_id, left, right) for segment in route_segments for left, right in zip(segment['ordered_target_ids'], segment['ordered_target_ids'][1:])]
        maximum_empty_distance = max(leg_distances, default=0.0)
        dmax_satisfied = all((value <= dmax + EPS for value in leg_distances))
        rgcn_selected = sum((target_id in view.selection_scores for target_id in selected))
        score_sources = {target_id: _candidate_score(candidates[target_id], view)[1] for target_id in selected}
        virtual_continuation_segment_ids = [
            row['segment_id'] for row in route_segments if row.get('virtual_continuation')
        ]
        return ({'floor_id': floor_id, 'physical_floor_id': physical_floor_id, 'building_scope_id': floor_id, 'physical_floor_primary_scope_id': physical_floor_primary_scope_id, 'physical_floor_primary_route': require_physical_floor_route_start, 'status': 'feasible', 'feasible': True, 'solver': 'dual_graph_rgcn_area_forest_dmax_walk', 'route_type': 'area_value_backbone_forest_plus_open_dmax_target_tree_physical_walks', 'physical_component_ids': sorted({components[target_id] for target_id in selected}), 'route_segments': route_segments, 'route_segment_count': len(route_segments), 'route_start_policy': route_start_policy, 'route_start_target_ids': [route_segments[0]['entry_target_id']] if route_segments and require_physical_floor_route_start else [], 'route_start_constraint_satisfied': bool(route_segments and route_segments[0].get('route_start_constraint_satisfied')) if require_physical_floor_route_start else None, 'virtual_entry_count': len(virtual_continuation_segment_ids), 'virtual_continuation_count': len(virtual_continuation_segment_ids), 'virtual_continuation_segment_ids': virtual_continuation_segment_ids, 'cross_segment_physical_jump_count': 0, 'selected_target_ids': sorted(selected), 'required_target_ids': sorted(set().union(*(set(row['required_target_ids']) for row in route_segments))), 'order': order, 'first_visit_order': first_visit_order, 'preferred_unique_order': preferred_unique_order, 'length': sum((_finite(row.get('length')) for row in route_segments)), 'selection_reason': selection_reason, 'score_sources': score_sources, 'rgcn_recommendation_key': recommendation_key, 'rgcn_scored_selected_target_count': rgcn_selected, 'rule_value_fallback_selected_target_count': len(selected) - rgcn_selected, 'rgcn_scored_candidate_count': len(rgcn_coverage), 'missing_rgcn_score_target_ids': missing_rgcn_scores, 'effective_inspection_constraints_satisfied': constraints_ok, 'configured_inspection_constraints_satisfied': configured_constraints_ok, 'inspection_constraint_shortfalls': constraint_shortfalls, 'root_high_value_area_ids': root_areas, 'area_visit_order': area_order, 'area_blocks': area_blocks, 'area_backbone_tree': area_trees, 'tree_edge_count': len(area_trees), 'dmax': dmax, 'dmax_ratio': config.dmax_ratio, 'maximum_continuous_uninspected_distance': maximum_empty_distance, 'dmax_constraint_satisfied': dmax_satisfied, 'dmax_audit': {'feasible': dmax_satisfied, 'dmax': dmax, 'segment_count': len(route_segments), 'segments': dmax_segment_audits, 'virtual_continuations_are_not_physical_paths': True}, 'target_backbone_tree': target_trees, 'target_backbone_tree_edge_count': len(target_trees), 'hard_constraint_audit': constraint_rows, 'component_audit': {'policy': 'physical_floor_first_segment_has_legal_start_each_building_scope_isolated_and_later_scopes_use_audited_virtual_continuation', 'physical_component_count': len({components[target_id] for target_id in selected}), 'cross_component_edges_added': 0, 'cross_building_edges_added': 0, 'virtual_continuation_count': len(virtual_continuation_segment_ids)}, 'target_order_unique': len(order) == len(set(order)), 'repeated_target_visit_count': len(order) - len(first_visit_order), 'physical_walk_repeated_edges_allowed': True}, value_graph)

    def build_dual_graph_inspection_route(run_dir: Path | str, candidates_path: Path | str, effective_free_areas_path: Path | str, refined_graph_path: Path | str, output_dir: Path | str, *, recommendations_path: Path | str | None=None, source_run_id: str='', source_dxf: Path | str | None=None, config: DualGraphConfig | None=None) -> dict[str, Any]:
        cfg = config or DualGraphConfig()
        total_started = time.perf_counter()
        output = Path(output_dir).resolve()
        output.mkdir(parents=True, exist_ok=True)
        candidates_bundle = _read(candidates_path)
        precompute_started = time.perf_counter()
        certified_graph_path, certification = certify_physical_graph(refined_graph_path, effective_free_areas_path, output / 'precomputed' / 'certified_physical_graph.json', candidates_bundle)
        closure_result = build_physical_metric_closure(certified_graph_path, output / 'precomputed' / 'physical_metric_closure', config=ClosureBuildConfig(include_euclidean_baseline=False))
        precompute_seconds = time.perf_counter() - precompute_started
        recommendations = _read(recommendations_path) if recommendations_path else {}
        closure = PhysicalMetricClosure(closure_result['manifest_path'])
        certified_graph_payload = _read(certified_graph_path)
        physical_floor_scales = _physical_floor_scales(certified_graph_payload)
        for floor_id, floor in (candidates_bundle.get('floors') or {}).items():
            if floor_id in physical_floor_scales:
                floor['floor_scale'] = physical_floor_scales[floor_id]
        physical_access_candidates, access_materialization_audit = materialize_physical_access_candidates(candidates_bundle, certified_graph_payload, closure)
        physical_access_candidates_path = _write(output / 'physical_access_candidates.json', physical_access_candidates)
        planning_started = time.perf_counter()
        optimized_floors: dict[str, Any] = {}
        value_graph_floors: dict[str, Any] = {}
        planning_inputs: dict[str, dict[str, Any]] = {}
        physical_floor_scopes: dict[str, list[str]] = {}
        for floor_id, floor in sorted((candidates_bundle.get('floors') or {}).items()):
            if cfg.floor_ids and floor_id not in set(cfg.floor_ids):
                continue
            physical_floor_id = _physical_floor_id(floor_id, floor)
            recommendation, recommendation_key = _recommendation_payload(recommendations, source_run_id, floor_id)
            planning_inputs[floor_id] = {
                'floor': floor,
                'physical_floor_id': physical_floor_id,
                'recommendation': recommendation,
                'recommendation_key': recommendation_key,
            }
            physical_floor_scopes.setdefault(physical_floor_id, []).append(floor_id)

        physical_floor_primary_scopes: dict[str, str] = {}
        for physical_floor_id, scope_ids in sorted(physical_floor_scopes.items()):
            legal_scope_ids = [
                scope_id
                for scope_id in sorted(scope_ids)
                if scope_id in closure.floor_ids()
                and _scope_has_routable_route_start(
                    scope_id,
                    planning_inputs[scope_id]['floor'],
                    closure,
                    planning_inputs[scope_id]['recommendation'],
                    cfg,
                )
            ]
            # If no legal entrance exists anywhere on the physical floor, keep
            # one hard-failing primary scope.  This preserves the business
            # requirement without incorrectly requiring every building to fail.
            physical_floor_primary_scopes[physical_floor_id] = (
                legal_scope_ids[0] if legal_scope_ids else sorted(scope_ids)[0]
            )

        for floor_id, row in sorted(planning_inputs.items()):
            floor = row['floor']
            physical_floor_id = row['physical_floor_id']
            primary_scope_id = physical_floor_primary_scopes[physical_floor_id]
            if floor_id not in closure.floor_ids():
                optimized_floors[floor_id] = {'floor_id': floor_id, 'physical_floor_id': physical_floor_id, 'building_scope_id': floor_id, 'physical_floor_primary_scope_id': primary_scope_id, 'status': 'infeasible_missing_physical_closure', 'feasible': False, 'reason': 'floor_missing_from_precomputed_physical_distance_matrix', 'order': [], 'selected_target_ids': [], 'solver': 'dual_graph_rgcn_area_tree_tsp'}
                continue
            plan, graph = _plan_floor(
                floor_id,
                floor,
                closure,
                row['recommendation'],
                row['recommendation_key'],
                cfg,
                physical_floor_id=physical_floor_id,
                physical_floor_primary_scope_id=primary_scope_id,
                require_physical_floor_route_start=floor_id == primary_scope_id,
            )
            optimized_floors[floor_id] = plan
            value_graph_floors[floor_id] = graph
        planning_seconds = time.perf_counter() - planning_started
        optimized_path = _write(output / 'optimized_target_order.json', {'schema_version': 1, 'architecture': 'rgcn_value_graph_plus_certified_physical_graph', 'route_type': 'area_forest_supported_open_physical_walks_with_dmax', 'same_floor_only': True, 'physical_floor_first_route_segment_requires_legal_inspection_entry': True, 'building_scopes_planned_independently': True, 'every_building_requires_own_legal_entry': False, 'physical_floor_primary_scopes': physical_floor_primary_scopes, 'later_disconnected_building_scopes_and_segments_use_virtual_continuation': True, 'virtual_continuations_are_not_physical_paths': True, 'cross_building_physical_edges_allowed': False, 'cross_segment_physical_jumps_allowed': False, 'floors': optimized_floors})
        value_graph_path = _write(output / 'value_graph.json', {'schema_version': 1, 'graph_type': 'rgcn_area_target_value_graph', 'source_run_id': source_run_id, 'floors': value_graph_floors})
        route_summary: dict[str, Any] = {}
        forwarding_seconds = 0.0
        if cfg.expand_physical_walk:
            forwarding_started = time.perf_counter()
            route_summary = build_last_inspection_route(run_dir, optimized_path, physical_access_candidates_path, effective_free_areas_path, output_dir=output / 'physical_walk', refined_graph_path=certified_graph_path, source_dxf=source_dxf, write_dxf=cfg.write_dxf)
            forwarding_seconds = time.perf_counter() - forwarding_started
        result = {'schema_version': 1, 'pipeline_type': 'same_floor_heterogeneous_rgcn_value_graph_plus_physical_metric_forest', 'architecture': {'upper_graph': 'existing same-floor Area/Portal/Anchor/Target/Rule heterogeneous graph with frozen R-GCN value and route heads', 'lower_graph': 'vector-certified refined physical navigation graph', 'solver': 'mandatory/quota exact selection + high-value area backbone forest + Dmax target trees on exact physical Metric Closure', 'route': 'independent building-scope open physical walks grouped by physical floor; non-simple target, node and edge revisits allowed', 'cross_floor_edges': False, 'cross_building_physical_edges': False, 'raster_or_grid_routing': False}, 'physical_floor_primary_scopes': physical_floor_primary_scopes, 'physical_floor_scopes': physical_floor_scopes, 'config': asdict(cfg), 'timing': {'recognition_precompute_seconds_excluded_from_planning': precompute_seconds, 'dual_graph_planning_seconds': planning_seconds, 'physical_walk_expansion_and_artifact_seconds': forwarding_seconds, 'total_pipeline_seconds': time.perf_counter() - total_started, 'planning_time_excludes_physical_distance_matrix_construction': True}, 'precomputed_inputs': {'certified_physical_graph': str(certified_graph_path), 'physical_metric_closure_manifest': closure_result['manifest_path'], 'certification': certification, 'physical_access_candidates': str(physical_access_candidates_path), 'access_materialization': access_materialization_audit, 'physical_floor_scales': physical_floor_scales}, 'rgcn': {'recommendations': str(Path(recommendations_path).resolve()) if recommendations_path else '', 'source_run_id': source_run_id, 'floors_with_rgcn_scores': [floor_id for floor_id in sorted(optimized_floors) if _recommendation_payload(recommendations, source_run_id, floor_id)[1]], 'missing_scores_fallback': 'forbidden; every eligible selected/relay target must have a same-floor R-GCN score' if cfg.require_complete_rgcn_scores else 'candidate u_rule; explicitly audited per target'}, 'counts': {'floor_count': len(optimized_floors), 'building_scope_count': len(optimized_floors), 'physical_floor_count': len(physical_floor_scopes), 'dual_graph_feasible_floor_count': sum((bool(row.get('feasible')) for row in optimized_floors.values())), 'selected_target_count': sum((len(row.get('selected_target_ids', []) or []) for row in optimized_floors.values() if row.get('feasible'))), 'planned_target_count': sum((len(row.get('order', []) or []) for row in optimized_floors.values())), 'planned_target_visit_count': sum((len(row.get('order', []) or []) for row in optimized_floors.values())), 'repeated_target_visit_count': sum((int(row.get('repeated_target_visit_count') or 0) for row in optimized_floors.values())), 'open_route_segment_count': sum((int(row.get('route_segment_count') or 0) for row in optimized_floors.values())), 'virtual_entry_count': sum((int(row.get('virtual_entry_count') or 0) for row in optimized_floors.values())), 'area_backbone_tree_edge_count': sum((int(row.get('tree_edge_count') or 0) for row in optimized_floors.values())), 'rgcn_scored_selected_target_count': sum((int(row.get('rgcn_scored_selected_target_count') or 0) for row in optimized_floors.values())), 'rule_value_fallback_selected_target_count': sum((int(row.get('rule_value_fallback_selected_target_count') or 0) for row in optimized_floors.values()))}, 'floors': optimized_floors, 'outputs': {'optimized_target_order': str(optimized_path), 'value_graph': str(value_graph_path), 'physical_access_candidates': str(physical_access_candidates_path), 'physical_walk_summary': route_summary.get('summary_path', ''), 'annotated_route_dxf': route_summary.get('outputs', {}).get('annotated_route_dxf', ''), 'acceptance_report': route_summary.get('outputs', {}).get('acceptance_report_markdown', ''), 'inspection_target_visit_order_csv': route_summary.get('outputs', {}).get('inspection_target_visit_order_csv', ''), 'inspection_target_visit_order_json': route_summary.get('outputs', {}).get('inspection_target_visit_order_json', ''), 'inspection_target_first_visit_order_csv': route_summary.get('outputs', {}).get('inspection_target_first_visit_order_csv', ''), 'inspection_target_first_visit_order_json': route_summary.get('outputs', {}).get('inspection_target_first_visit_order_json', '')}}
        result['counts']['floor_with_inspection_constraint_shortfall_count'] = sum(
            bool(row.get('inspection_constraint_shortfalls'))
            for row in optimized_floors.values()
        )
        result['counts']['inspection_constraint_shortfall_count'] = sum(
            len(row.get('inspection_constraint_shortfalls', []) or [])
            for row in optimized_floors.values()
        )
        result['architecture']['route_start'] = (
            'hard constraint: the first route segment on every physical floor starts at a '
            'safety exit or fire elevator inspection object; fire doors forbidden; '
            'building scopes are planned independently and later disconnected building scopes or '
            'segments use audited virtual continuations that are not physical paths'
        )
        result['counts']['legal_route_start_count'] = sum(
            len(row.get('route_start_target_ids', []) or [])
            for row in optimized_floors.values()
            if row.get('feasible')
        )
        result['counts']['infeasible_route_start_floor_count'] = sum(
            row.get('status') == 'infeasible_route_start'
            for row in optimized_floors.values()
        )
        result['counts']['virtual_continuation_count'] = sum(
            int(row.get('virtual_continuation_count') or 0)
            for row in optimized_floors.values()
        )
        summary_path = _write(output / 'dual_graph_summary.json', result)
        return {**result, 'summary_path': str(summary_path)}
    __all__ = ['DualGraphConfig', 'build_dual_graph_inspection_route', 'certify_physical_graph', 'main']
    return dict(locals())

_s09_planner = _register_embedded_module(
    'fire_inspection_system.dual_graph.planner',
    _build_s09_planner(),
    aliases=(),
)

# === CONSOLIDATED PUBLIC API ===
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DualGraphPlanningResult:
    dual_graph: dict[str, Any]
    elapsed_seconds: float

    def to_summary(self) -> dict[str, Any]:
        return self.dual_graph


def run_stage(
    run_dir: Path,
    physical: Any,
    semantic: Any,
    *,
    dmax_ratio: float,
) -> DualGraphPlanningResult:
    started = time.perf_counter()
    if physical.refined_graph is None or physical.effective_free_areas is None:
        raise ValueError("双图规划阶段缺少物理导航图或有效自由空间")
    dual = _s09_planner.build_dual_graph_inspection_route(
        run_dir,
        semantic.candidates_path,
        physical.effective_free_areas,
        physical.refined_graph,
        run_dir / "path_planning" / "dual_graph",
        recommendations_path=semantic.recommendations_path,
        source_run_id=semantic.source_run_id,
        source_dxf=None,
        config=_s09_planner.DualGraphConfig(
            dmax_ratio=dmax_ratio,
            require_complete_rgcn_scores=True,
            floor_ids=semantic.selected_floor_ids,
            write_dxf=False,
            expand_physical_walk=False,
        ),
    )
    expected = len(semantic.selected_floor_ids)
    actual = int(dual["counts"]["dual_graph_feasible_floor_count"])
    if actual != expected:
        raise RuntimeError(
            f"至少一个识别楼层未生成可行双图路径: 可行 {actual}/{expected}"
        )
    return DualGraphPlanningResult(
        dual_graph=dual,
        elapsed_seconds=time.perf_counter() - started,
    )


__all__ = ["DualGraphPlanningResult", "run_stage"]
