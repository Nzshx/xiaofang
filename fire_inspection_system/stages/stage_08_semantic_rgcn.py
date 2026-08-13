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



# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/semantic/context_features.py
# -----------------------------------------------------------------------------
def _build_s08_context():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/semantic/context_features.py'
    )
    __name__ = 'fire_inspection_system.semantic.context_features'
    __package__ = 'fire_inspection_system.semantic'
    """Compute the three auditable inspection-target context values.

    The value model deliberately contains only:

    ``A``
        Importance of the AreaGraph region containing the target.  The derived
        value uses every effective rule candidate, not only mandatory targets.
    ``N``
        Value of nearby rule-relevant targets within a floor-relative Euclidean
        radius, including a discounted two-hop dependency term.
    ``C``
        Route-distance saving of selecting the instance.  Exact counterfactual
        route solves are preferred; a clearly labelled Metric-Closure proximity
        proxy is used when counterfactuals are not available yet.

    Category base scores, recognition uncertainty and spatial-dispersion rewards
    are intentionally not part of this module.
    """
    import json
    import math
    from collections import defaultdict
    from dataclasses import asdict, dataclass
    from pathlib import Path
    from typing import Any, Iterable, Mapping, Sequence

    @dataclass(frozen=True)
    class ContextFeatureConfig:
        near_radius_factor: float = 0.15
        near_max_neighbors: int = 32
        near_second_order_weight: float = 0.3
        area_constraint_load_weight: float = 0.6
        area_rule_diversity_weight: float = 0.25
        area_portal_context_weight: float = 0.15
        weight_area: float = 1.0 / 3.0
        weight_nearby_risk: float = 1.0 / 3.0
        weight_route_contribution: float = 1.0 / 3.0
        neutral_value: float = 0.5

        def validate(self) -> None:
            if not self.near_radius_factor > 0.0:
                raise ValueError('near_radius_factor must be positive')
            if self.near_max_neighbors < 1:
                raise ValueError('near_max_neighbors must be at least 1')
            if self.near_second_order_weight < 0.0 or not math.isfinite(self.near_second_order_weight):
                raise ValueError('near_second_order_weight must be finite and non-negative')
            area_weights = (self.area_constraint_load_weight, self.area_rule_diversity_weight, self.area_portal_context_weight)
            if any((value < 0.0 or not math.isfinite(value) for value in area_weights)):
                raise ValueError('area-context weights must be finite and non-negative')
            if not math.isclose(sum(area_weights), 1.0, rel_tol=1e-09, abs_tol=1e-09):
                raise ValueError('area-context weights must sum to 1')
            weights = (self.weight_area, self.weight_nearby_risk, self.weight_route_contribution)
            if any((value < 0.0 or not math.isfinite(value) for value in weights)):
                raise ValueError('context-value weights must be finite and non-negative')
            if not math.isclose(sum(weights), 1.0, rel_tol=1e-09, abs_tol=1e-09):
                raise ValueError('context-value weights must sum to 1')

    def _finite_float(value: Any, default: float=0.0) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return default
        return result if math.isfinite(result) else default

    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or '').strip().lower() in {'1', 'true', 'yes', 'all', 'mandatory'}

    def _is_mandatory(target: Mapping[str, Any]) -> bool:
        return _truthy(target.get('mandatory', target.get('is_mandatory', False))) or str(target.get('rule_mode') or '') in {'all', 'mandatory_all'}

    def _positive_int(row: Mapping[str, Any], keys: Sequence[str], default: int) -> int:
        for key in keys:
            try:
                value = int(row.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return default

    def _rule_need_weights(targets: Sequence[Mapping[str, Any]]) -> tuple[dict[str, float], dict[str, str]]:
        """Estimate how strongly each effective candidate participates in a rule.

        This is a constraint-participation share, not a category base value.  The
        shares sum to the required quota within each independent floor/rule/class
        pool (subject to candidate availability), so mandatory and sampled targets
        can be compared without pretending that every quota candidate is required.
        """
        weights: dict[str, float] = {}
        sources: dict[str, str] = {}
        grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for row in targets:
            target_id = str(row.get('target_id') or row.get('node_id') or '')
            if _is_mandatory(row):
                weights[target_id] = 1.0
                sources[target_id] = 'mandatory_required'
                continue
            group_id = str(row.get('quota_group') or row.get('rule_id') or 'unclassified')
            grouped[str(row.get('floor_id') or ''), group_id].append(row)
        for rows in grouped.values():
            mode = str(rows[0].get('rule_mode') or 'quota_per_class')
            if mode == 'distinct_category_group':
                by_class: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
                for row in rows:
                    by_class[str(row.get('target_class') or row.get('category') or 'unknown')].append(row)
                required_classes = _positive_int(rows[0], ('required_distinct_categories', 'distinct_categories', 'sample_size', 'quota'), 2)
                per_class = _positive_int(rows[0], ('instances_per_category', 'per_category', 'per_class'), 1)
                category_share = min(1.0, required_classes / max(len(by_class), 1))
                for class_rows in by_class.values():
                    instance_share = min(1.0, per_class / max(len(class_rows), 1))
                    for row in class_rows:
                        target_id = str(row['target_id'])
                        weights[target_id] = category_share * instance_share
                        sources[target_id] = 'distinct_category_constraint_share'
                continue
            if mode == 'quota_total':
                required = _positive_int(rows[0], ('required_count', 'quota', 'sample_size'), 2)
                share = min(1.0, required / max(len(rows), 1))
                for row in rows:
                    target_id = str(row['target_id'])
                    weights[target_id] = share
                    sources[target_id] = 'group_quota_constraint_share'
                continue
            by_class: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
            for row in rows:
                by_class[str(row.get('target_class') or row.get('category') or 'unknown')].append(row)
            for class_rows in by_class.values():
                default_quota = 4 if str(class_rows[0].get('rule_id') or '') == 'hydrants' else 2
                required = _positive_int(class_rows[0], ('required_count', 'quota', 'sample_size', 'per_class'), default_quota)
                share = min(1.0, required / max(len(class_rows), 1))
                for row in class_rows:
                    target_id = str(row['target_id'])
                    weights[target_id] = share
                    sources[target_id] = 'per_class_quota_constraint_share'
        return (weights, sources)

    def _target_point(target: Mapping[str, Any]) -> tuple[float, float]:
        geometry = target.get('geometry')
        if isinstance(geometry, Mapping):
            coordinates = geometry.get('coordinates')
            if isinstance(coordinates, Sequence) and len(coordinates) >= 2:
                return (_finite_float(coordinates[0]), _finite_float(coordinates[1]))
        x = target.get('raw_x', target.get('x', 0.0))
        y = target.get('raw_y', target.get('y', 0.0))
        return (_finite_float(x), _finite_float(y))

    def _iter_xy(value: Any) -> Iterable[tuple[float, float]]:
        if isinstance(value, (list, tuple)):
            if len(value) >= 2 and isinstance(value[0], (int, float)) and isinstance(value[1], (int, float)):
                x, y = (_finite_float(value[0], math.nan), _finite_float(value[1], math.nan))
                if math.isfinite(x) and math.isfinite(y):
                    yield (x, y)
                return
            for item in value:
                yield from _iter_xy(item)

    def _area_bounds(area: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
        geometry = area.get('geometry')
        coordinates = geometry.get('coordinates') if isinstance(geometry, Mapping) else None
        points = list(_iter_xy(coordinates))
        if not points:
            x = _finite_float(area.get('centroid_x'), math.nan)
            y = _finite_float(area.get('centroid_y'), math.nan)
            if math.isfinite(x) and math.isfinite(y):
                return (x, y, x, y)
            return None
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        return (min(xs), min(ys), max(xs), max(ys))

    def _derive_floor_bounds(targets: Sequence[Mapping[str, Any]], areas: Sequence[Mapping[str, Any]], explicit: Mapping[str, Sequence[float]] | None) -> dict[str, tuple[float, float, float, float]]:
        result: dict[str, tuple[float, float, float, float]] = {}
        if explicit:
            for floor_id, bounds in explicit.items():
                if len(bounds) < 4:
                    raise ValueError(f'floor bounds for {floor_id!r} must contain four values')
                minx, miny, maxx, maxy = (_finite_float(value) for value in bounds[:4])
                result[str(floor_id)] = (minx, miny, maxx, maxy)
        accumulators: dict[str, list[tuple[float, float, float, float]]] = defaultdict(list)
        for area in areas:
            floor_id = str(area.get('floor_id') or '')
            bounds = _area_bounds(area)
            if floor_id and bounds is not None:
                accumulators[floor_id].append(bounds)
        for floor_id, rows in accumulators.items():
            if floor_id in result:
                continue
            result[floor_id] = (min((row[0] for row in rows)), min((row[1] for row in rows)), max((row[2] for row in rows)), max((row[3] for row in rows)))
        target_points: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for target in targets:
            floor_id = str(target.get('floor_id') or '')
            if floor_id:
                target_points[floor_id].append(_target_point(target))
        for floor_id, points in target_points.items():
            if floor_id in result or not points:
                continue
            result[floor_id] = (min((point[0] for point in points)), min((point[1] for point in points)), max((point[0] for point in points)), max((point[1] for point in points)))
        return result

    def _normalize_by_floor(raw_by_id: Mapping[str, float], floor_by_id: Mapping[str, str], neutral: float) -> dict[str, float]:
        grouped: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for item_id, value in raw_by_id.items():
            grouped[floor_by_id.get(item_id, '')].append((item_id, _finite_float(value)))
        normalized: dict[str, float] = {}
        for rows in grouped.values():
            values = [value for _item_id, value in rows]
            low, high = (min(values), max(values))
            if math.isclose(low, high, rel_tol=1e-12, abs_tol=1e-12):
                normalized.update({item_id: neutral for item_id, _value in rows})
                continue
            scale = high - low
            normalized.update({item_id: (value - low) / scale for item_id, value in rows})
        return normalized

    def _extract_distance_pairs(distance_matrix: Mapping[str, Any] | None) -> dict[tuple[str, str], float]:
        """Accept the Euclidean distance-matrix schema and test-friendly variants."""
        result: dict[tuple[str, str], float] = {}
        if not distance_matrix:
            return result
        pair_rows: list[Mapping[str, Any]] = []
        pairs = distance_matrix.get('pairs')
        if isinstance(pairs, list):
            pair_rows.extend((row for row in pairs if isinstance(row, Mapping)))
        floors = distance_matrix.get('floors')
        if isinstance(floors, Mapping):
            for floor_value in floors.values():
                if not isinstance(floor_value, Mapping):
                    continue
                floor_pairs = floor_value.get('pairs')
                if isinstance(floor_pairs, list):
                    pair_rows.extend((row for row in floor_pairs if isinstance(row, Mapping)))
        for row in pair_rows:
            source = str(row.get('target_a') or row.get('source_target_id') or row.get('source') or '')
            target = str(row.get('target_b') or row.get('target_target_id') or row.get('target') or '')
            reachable = row.get('reachable', True)
            distance = _finite_float(row.get('distance', row.get('length')), math.inf)
            if source and target and _truthy(reachable) and math.isfinite(distance):
                result[source, target] = distance
                result[target, source] = distance
        return result

    def _distance_value(distance_matrix: Mapping[str, Any] | None, pair_fallback: Mapping[tuple[str, str], float], floor_id: str, source: str, target: str) -> float | None:
        """Read a distance without copying a production-sized nested matrix."""
        block: Any = distance_matrix
        if isinstance(distance_matrix, Mapping):
            floors = distance_matrix.get('floors')
            if isinstance(floors, Mapping):
                block = floors.get(floor_id)
            if isinstance(block, Mapping):
                distances = block.get('distances', block.get('distance_matrix'))
                if isinstance(distances, Mapping):
                    row = distances.get(source)
                    if isinstance(row, Mapping):
                        value = row.get(target)
                        try:
                            number = float(value)
                        except (TypeError, ValueError):
                            number = math.inf
                        if math.isfinite(number) and number >= 0.0:
                            return number
        return pair_fallback.get((source, target))

    def _counterfactual_saving(target_id: str, floor_scale: float, counterfactuals: Mapping[str, Mapping[str, Any]] | None) -> tuple[float | None, str]:
        row = counterfactuals.get(target_id) if counterfactuals else None
        if not isinstance(row, Mapping):
            return (None, '')
        excluding = row.get('excluding', row.get('forbidden_length', row.get('without')))
        forcing = row.get('forcing', row.get('forced_length', row.get('with')))
        try:
            excluding_value = float(excluding)
            forcing_value = float(forcing)
        except (TypeError, ValueError):
            return (None, '')
        if math.isfinite(forcing_value) and math.isinf(excluding_value):
            return (max(floor_scale, 1.0), 'counterfactual_feasibility_gain')
        if not (math.isfinite(excluding_value) and math.isfinite(forcing_value)):
            return (0.0, 'counterfactual_unavailable')
        source = str(row.get('source') or 'counterfactual_route_saving')
        if bool(row.get('approximate')) and (not source.endswith('_proxy')):
            source = source + '_proxy'
        return (max(0.0, excluding_value - forcing_value), source)

    def _nearby_relations(targets: Sequence[Mapping[str, Any]], floor_scales: Mapping[str, float], base_values: Mapping[str, float], config: ContextFeatureConfig) -> tuple[list[dict[str, Any]], dict[str, float], dict[str, float], dict[str, float]]:
        by_floor: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for target in targets:
            by_floor[str(target.get('floor_id') or '')].append(target)
        relations: list[dict[str, Any]] = []
        first_order: dict[str, float] = {}
        normalized_neighbors: dict[str, list[tuple[str, float, dict[str, Any]]]] = {}
        for floor_id, rows in by_floor.items():
            radius = max(floor_scales.get(floor_id, 1.0) * config.near_radius_factor, 1e-09)
            buckets: dict[tuple[int, int], list[tuple[Mapping[str, Any], float, float]]] = defaultdict(list)
            positioned: list[tuple[Mapping[str, Any], float, float]] = []
            for row in rows:
                x, y = _target_point(row)
                positioned.append((row, x, y))
                buckets[math.floor(x / radius), math.floor(y / radius)].append((row, x, y))
            for row, x, y in positioned:
                target_id = str(row.get('target_id') or row.get('node_id') or '')
                cell_x, cell_y = (math.floor(x / radius), math.floor(y / radius))
                candidates: list[tuple[float, Mapping[str, Any]]] = []
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        for neighbor, nx, ny in buckets.get((cell_x + dx, cell_y + dy), []):
                            neighbor_id = str(neighbor.get('target_id') or neighbor.get('node_id') or '')
                            if not neighbor_id or neighbor_id == target_id:
                                continue
                            distance = math.hypot(nx - x, ny - y)
                            if distance <= radius:
                                candidates.append((distance, neighbor))
                candidates.sort(key=lambda item: (item[0], str(item[1].get('target_id') or '')))
                candidates = candidates[:config.near_max_neighbors]
                relation_rows: list[tuple[str, float, dict[str, Any]]] = []
                decay_sum = sum((math.exp(-distance / radius) for distance, _neighbor in candidates))
                for distance, neighbor in candidates:
                    neighbor_id = str(neighbor.get('target_id') or neighbor.get('node_id') or '')
                    decay = math.exp(-distance / radius)
                    normalized_weight = decay / decay_sum if decay_sum > 0.0 else 0.0
                    neighbor_value = max(0.0, _finite_float(base_values.get(neighbor_id)))
                    relation = {'source_target_id': target_id, 'target_target_id': neighbor_id, 'floor_id': floor_id, 'distance_euclidean': distance, 'radius': radius, 'decay': decay, 'normalized_weight': normalized_weight, 'neighbor_base_value': neighbor_value, 'neighbor_risk_value': neighbor_value, 'first_order_contribution': decay * neighbor_value}
                    relations.append(relation)
                    relation_rows.append((neighbor_id, normalized_weight, relation))
                normalized_neighbors[target_id] = relation_rows
                first_order[target_id] = sum((row[2]['first_order_contribution'] for row in relation_rows))
        second_order: dict[str, float] = {}
        raw_nearby: dict[str, float] = {}
        for target in targets:
            target_id = str(target.get('target_id') or target.get('node_id') or '')
            second = sum((weight * first_order.get(neighbor_id, 0.0) for neighbor_id, weight, _relation in normalized_neighbors.get(target_id, [])))
            second_order[target_id] = second
            raw_nearby[target_id] = first_order.get(target_id, 0.0) + config.near_second_order_weight * second
        return (relations, raw_nearby, first_order, second_order)

    def compute_context_features(targets: Iterable[Mapping[str, Any]], areas: Iterable[Mapping[str, Any]], *, distance_matrix: Mapping[str, Any] | None=None, counterfactuals: Mapping[str, Mapping[str, Any]] | None=None, floor_bounds: Mapping[str, Sequence[float]] | None=None, config: ContextFeatureConfig | None=None) -> dict[str, Any]:
        """Return normalized A/N/C features and semantic NEAR relations.

        ``targets`` are expected to have stable ``target_id`` and ``floor_id``
        fields.  Selection metadata (mandatory/rule/group) is retained in the
        returned rows so the graph builder can join without guessing.
        """
        cfg = config or ContextFeatureConfig()
        cfg.validate()
        target_rows = [dict(row) for row in targets]
        area_rows = [dict(row) for row in areas]
        seen: set[str] = set()
        for row in target_rows:
            target_id = str(row.get('target_id') or row.get('node_id') or '')
            floor_id = str(row.get('floor_id') or '')
            if not target_id or not floor_id:
                raise ValueError('every target requires non-empty target_id and floor_id')
            if target_id in seen:
                raise ValueError(f'duplicate target_id: {target_id}')
            row['target_id'] = target_id
            row['floor_id'] = floor_id
            seen.add(target_id)
        bounds_by_floor = _derive_floor_bounds(target_rows, area_rows, floor_bounds)
        floor_scales = {floor_id: max(maxx - minx, maxy - miny, 1.0) for floor_id, (minx, miny, maxx, maxy) in bounds_by_floor.items()}
        floor_by_target = {row['target_id']: row['floor_id'] for row in target_rows}
        rule_need_by_target, rule_need_source = _rule_need_weights(target_rows)
        rule_need_by_area: dict[str, float] = defaultdict(float)
        rule_groups_by_area: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for row in target_rows:
            area_id = str(row.get('area_id') or '')
            if not area_id:
                continue
            target_id = row['target_id']
            rule_need_by_area[area_id] += rule_need_by_target.get(target_id, 0.0)
            mode = str(row.get('rule_mode') or '')
            group_id = str(row.get('quota_group') or row.get('rule_id') or 'unclassified')
            class_id = str(row.get('target_class') or '') if mode == 'quota_per_class' else ''
            rule_groups_by_area[area_id].add((group_id, class_id))
        areas_by_id = {str(row.get('area_id') or ''): row for row in area_rows if row.get('area_id')}
        floor_area_total: dict[str, float] = defaultdict(float)
        for area in area_rows:
            floor_area_total[str(area.get('floor_id') or '')] += max(0.0, _finite_float(area.get('area')))
        raw_area: dict[str, float] = {}
        area_source: dict[str, str] = {}
        area_constraint_load: dict[str, float] = {}
        area_rule_group_count: dict[str, int] = {}
        for target in target_rows:
            target_id = target['target_id']
            floor_id = target['floor_id']
            area_id = str(target.get('area_id') or '')
            area = areas_by_id.get(area_id, {})
            explicit = target.get('area_importance', area.get('area_importance'))
            if explicit is not None:
                raw_area[target_id] = max(0.0, _finite_float(explicit))
                area_source[target_id] = 'explicit'
                continue
            area_size = max(0.0, _finite_float(area.get('area')))
            floor_total = max(floor_area_total.get(floor_id, 0.0), 1.0)
            area_fraction = max(area_size / floor_total, 1e-09)
            constraint_density = rule_need_by_area.get(area_id, 0.0) / area_fraction
            active_rule_group_count = len(rule_groups_by_area.get(area_id, set()))
            portal_context = math.log1p(max(0.0, _finite_float(area.get('portal_count'))))
            area_constraint_load[target_id] = constraint_density
            area_rule_group_count[target_id] = active_rule_group_count
            raw_area[target_id] = cfg.area_constraint_load_weight * math.log1p(constraint_density) + cfg.area_rule_diversity_weight * math.log1p(active_rule_group_count) + cfg.area_portal_context_weight * portal_context
            area_source[target_id] = 'derived_all_candidate_constraint_load_rule_diversity_and_portal_context' if area else 'default_missing_area'
        normalized_area = _normalize_by_floor(raw_area, floor_by_target, cfg.neutral_value)
        nearby_base: dict[str, float] = {}
        for target in target_rows:
            target_id = target['target_id']
            explicit_risk = target.get('risk_value')
            if explicit_risk is not None:
                nearby_base[target_id] = max(0.0, _finite_float(explicit_risk))
            else:
                nearby_base[target_id] = rule_need_by_target.get(target_id, 0.0) * (0.5 + 0.5 * normalized_area[target_id])
        near_relations, raw_nearby, first_order_nearby, second_order_nearby = _nearby_relations(target_rows, floor_scales, nearby_base, cfg)
        normalized_nearby = _normalize_by_floor(raw_nearby, floor_by_target, cfg.neutral_value)
        distance_pairs = _extract_distance_pairs(distance_matrix)
        mandatory_ids_by_floor: dict[str, list[str]] = defaultdict(list)
        for target in target_rows:
            if _is_mandatory(target):
                mandatory_ids_by_floor[target['floor_id']].append(target['target_id'])
        raw_route: dict[str, float] = {}
        route_source: dict[str, str] = {}
        proxy_cost: dict[str, float] = {}
        for target in target_rows:
            target_id = target['target_id']
            floor_id = target['floor_id']
            saving, source = _counterfactual_saving(target_id, floor_scales.get(floor_id, 1.0), counterfactuals)
            if saving is not None:
                raw_route[target_id] = saving
                route_source[target_id] = source
                continue
            references = [item for item in mandatory_ids_by_floor.get(floor_id, []) if item != target_id]
            if not references:
                references = [row['target_id'] for row in target_rows if row['floor_id'] == floor_id and row['target_id'] != target_id]
            finite = []
            for item in references:
                distance = _distance_value(distance_matrix, distance_pairs, floor_id, target_id, item)
                if distance is not None:
                    finite.append(distance)
            if finite:
                proxy_cost[target_id] = sum(finite) / len(finite)
                route_source[target_id] = 'euclidean_distance_proximity_proxy'
            else:
                raw_route[target_id] = 0.0
                route_source[target_id] = 'unavailable'
        proxy_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        by_id = {row['target_id']: row for row in target_rows}
        for target_id in proxy_cost:
            row = by_id[target_id]
            group = str(row.get('quota_group') or row.get('rule_id') or row.get('target_class') or 'all')
            proxy_groups[row['floor_id'], group].append(target_id)
        for target_ids in proxy_groups.values():
            maximum_cost = max((proxy_cost[target_id] for target_id in target_ids))
            for target_id in target_ids:
                raw_route[target_id] = max(0.0, maximum_cost - proxy_cost[target_id])
        normalized_route = _normalize_by_floor(raw_route, floor_by_target, cfg.neutral_value)
        feature_rows: list[dict[str, Any]] = []
        for target in sorted(target_rows, key=lambda row: (row['floor_id'], row['target_id'])):
            target_id = target['target_id']
            area_value = normalized_area[target_id]
            nearby_value = normalized_nearby[target_id]
            route_value = normalized_route[target_id]
            rule_value = cfg.weight_area * area_value + cfg.weight_nearby_risk * nearby_value + cfg.weight_route_contribution * route_value
            feature_rows.append({'target_id': target_id, 'floor_id': target['floor_id'], 'area_id': str(target.get('area_id') or ''), 'rule_id': str(target.get('rule_id') or ''), 'quota_group': str(target.get('quota_group') or ''), 'mandatory': _is_mandatory(target), 'rule_need_weight': rule_need_by_target.get(target_id, 0.0), 'rule_need_source': rule_need_source.get(target_id, 'unavailable'), 'area_constraint_load': area_constraint_load.get(target_id, 0.0), 'area_rule_group_count': area_rule_group_count.get(target_id, 0), 'A_raw': raw_area[target_id], 'A': area_value, 'A_source': area_source[target_id], 'N_raw': raw_nearby[target_id], 'N_first_order_raw': first_order_nearby.get(target_id, 0.0), 'N_second_order_raw': second_order_nearby.get(target_id, 0.0), 'N': nearby_value, 'N_source': 'all_candidate_rule_need_area_context_with_two_hop_euclidean_dependency', 'C_raw': raw_route[target_id], 'C': route_value, 'C_source': route_source[target_id], 'u_rule': rule_value})
        return {'schema_version': 1, 'feature_type': 'inspection_target_context_value_A_N_C', 'config': asdict(cfg), 'floor_bounds': {key: list(value) for key, value in sorted(bounds_by_floor.items())}, 'floor_scales': dict(sorted(floor_scales.items())), 'targets': feature_rows, 'near_relations': near_relations, 'statistics': {'floor_count': len(floor_scales), 'target_count': len(feature_rows), 'near_relation_count': len(near_relations), 'nonmandatory_context_target_count': sum((not row['mandatory'] and row['rule_need_weight'] > 0.0 for row in feature_rows)), 'second_order_dependency_target_count': sum((row['N_second_order_raw'] > 0.0 for row in feature_rows)), 'counterfactual_target_count': sum((row['C_source'].startswith('counterfactual') or row['C_source'].endswith('_proxy') for row in feature_rows)), 'euclidean_counterfactual_target_count': sum((row['C_source'] == 'euclidean_insertion_coverage_proxy' for row in feature_rows)), 'metric_proxy_target_count': sum((row['C_source'] == 'euclidean_distance_proximity_proxy' for row in feature_rows))}}

    def write_context_features(path: Path | str, payload: Mapping[str, Any]) -> Path:
        output = Path(path).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        return output
    __all__ = ['ContextFeatureConfig', 'compute_context_features', 'write_context_features']
    return dict(locals())

_s08_context = _register_embedded_module(
    'fire_inspection_system.semantic.context_features',
    _build_s08_context(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/semantic/euclidean_closure.py
# -----------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/semantic/route_optimizer.py
# -----------------------------------------------------------------------------
def _build_s08_optimizer():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/semantic/route_optimizer.py'
    )
    __name__ = 'fire_inspection_system.semantic.route_optimizer'
    __package__ = 'fire_inspection_system.semantic'
    """Open inspection-route baseline over a supplied target-distance matrix.

    The matrix may be a physical closure in an external experiment or the current
    same-floor Euclidean approximation.  This optimizer itself makes no wall or
    reachability claim; callers must inspect the matrix metadata.  For small floor
    instances it enumerates rule-feasible selections and solves each open
    Hamiltonian path with Held--Karp dynamic programming.  Larger instances use a
    deterministic greedy selection plus multi-start nearest-neighbour/2-opt route,
    and explicitly mark the result as heuristic.
    """
    import itertools
    import math
    from typing import Any, Iterable, Iterator, Mapping, Sequence
    INF = float('inf')
    EPS = 1e-09

    def _as_id(value: Any) -> str:
        return str(value).strip()

    def _floor_block(distance_matrix: Mapping[str, Any], floor_id: str | None) -> Mapping[str, Any]:
        """Locate one floor's target-distance block."""
        if not isinstance(distance_matrix, Mapping):
            raise TypeError('distance_matrix must be a mapping')
        floors = distance_matrix.get('floors')
        if isinstance(floors, Mapping):
            if floor_id is not None and str(floor_id) in floors:
                return floors[str(floor_id)]
            if floor_id is None and len(floors) == 1:
                return next(iter(floors.values()))
            if floor_id is not None:
                for key, value in floors.items():
                    if str(key) == str(floor_id):
                        return value
            raise KeyError(f'floor {floor_id!r} is absent from distance matrix')
        if floor_id is not None and str(floor_id) in distance_matrix:
            value = distance_matrix[str(floor_id)]
            if isinstance(value, Mapping):
                return value
        return distance_matrix

    def _is_approximate_distance_source(distance_matrix: Mapping[str, Any]) -> bool:
        """Whether the supplied matrix is explicitly non-physical."""
        if not isinstance(distance_matrix, Mapping):
            return False
        if bool(distance_matrix.get('approximate')) or bool(distance_matrix.get('non_wall_safe')):
            return True
        floors = distance_matrix.get('floors')
        if isinstance(floors, Mapping):
            return any((isinstance(block, Mapping) and (bool(block.get('approximate')) or bool(block.get('non_wall_safe'))) for block in floors.values()))
        return False

    def _normalise_matrix(block: Mapping[str, Any]) -> dict[str, dict[str, float | None]]:
        raw = block.get('distances')
        if raw is None:
            raw = block.get('distance_matrix')
        if raw is None:
            raw = block
        if isinstance(raw, Sequence) and (not isinstance(raw, (str, bytes, bytearray))):
            target_ids = block.get('target_ids') or block.get('ids')
            if not isinstance(target_ids, Sequence) or isinstance(target_ids, (str, bytes, bytearray)):
                raise ValueError('matrix-form distance data needs target_ids')
            result: dict[str, dict[str, float | None]] = {}
            for i, source in enumerate(target_ids):
                row: dict[str, float | None] = {}
                if i < len(raw) and isinstance(raw[i], Sequence):
                    for j, target in enumerate(target_ids):
                        value = raw[i][j] if j < len(raw[i]) else None
                        row[_as_id(target)] = _finite_or_none(value)
                result[_as_id(source)] = row
            return result
        if not isinstance(raw, Mapping):
            return {}
        result = {}
        for source, row in raw.items():
            source_id = _as_id(source)
            if isinstance(row, Mapping):
                result[source_id] = {_as_id(target): _finite_or_none(value) for target, value in row.items()}
        return result

    def _finite_or_none(value: Any) -> float | None:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number >= 0 else None

    def _distance(matrix: Mapping[str, Mapping[str, float | None]], source: str, target: str) -> float:
        source, target = (_as_id(source), _as_id(target))
        if source == target:
            return 0.0
        value = matrix.get(source, {}).get(target)
        if value is None:
            value = matrix.get(target, {}).get(source)
        if value is None:
            return INF
        try:
            value = float(value)
        except (TypeError, ValueError):
            return INF
        return value if math.isfinite(value) and value >= 0 else INF

    def _known_ids(block: Mapping[str, Any], matrix: Mapping[str, Mapping[str, float | None]]) -> set[str]:
        target_records = block.get('targets')
        if isinstance(target_records, Sequence) and (not isinstance(target_records, (str, bytes, bytearray))):
            records_with_status = [item for item in target_records if isinstance(item, Mapping) and 'valid_access' in item]
            if records_with_status:
                valid = {_as_id(item.get('target_id')) for item in records_with_status if bool(item.get('valid_access')) and _as_id(item.get('target_id'))}
                if valid:
                    return valid
                return set()
        declared = block.get('target_ids') or block.get('ids')
        if isinstance(declared, Sequence) and (not isinstance(declared, (str, bytes, bytearray))):
            return {_as_id(value) for value in declared}
        known = set(matrix)
        for row in matrix.values():
            known.update(row)
        return known

    def _path_entry(block: Mapping[str, Any], source: str, target: str) -> dict[str, Any] | None:
        paths = block.get('paths')
        if not isinstance(paths, Mapping):
            return None
        row = paths.get(source) or paths.get(str(source))
        if not isinstance(row, Mapping):
            row = next((value for key, value in paths.items() if _as_id(key) == source), None)
        if not isinstance(row, Mapping):
            return None
        value = row.get(target) or row.get(str(target))
        if isinstance(value, Mapping):
            return dict(value)
        return None

    def _candidate_id(candidate: Mapping[str, Any]) -> str:
        return _as_id(candidate.get('target_id') or candidate.get('id') or candidate.get('object_id'))

    def _normalise_floor_targets(target_block: Mapping[str, Any] | Sequence[Mapping[str, Any]], floor_id: str | None) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        if isinstance(target_block, Mapping) and isinstance(target_block.get('floors'), Mapping):
            floors = target_block['floors']
            if floor_id is None:
                if len(floors) != 1:
                    raise ValueError('floor_id is required when target candidates contain multiple floors')
                floor_id = _as_id(next(iter(floors)))
            block = floors.get(floor_id)
            if block is None:
                block = next((value for key, value in floors.items() if _as_id(key) == _as_id(floor_id)), None)
            if not isinstance(block, Mapping):
                raise KeyError(f'floor {floor_id!r} is absent from target candidates')
            target_block = block
        if isinstance(target_block, Mapping):
            current_floor = _as_id(target_block.get('floor_id') or floor_id or 'UNASSIGNED')
            raw_candidates = target_block.get('candidates') or target_block.get('targets') or target_block.get('target_instances') or []
            raw_requirements = target_block.get('requirements') or target_block.get('rule_requirements') or target_block.get('rules') or []
        else:
            current_floor = _as_id(floor_id or 'UNASSIGNED')
            raw_candidates = target_block
            raw_requirements = []
        candidates = [dict(item) for item in raw_candidates if isinstance(item, Mapping)]
        requirements = [dict(item) for item in raw_requirements if isinstance(item, Mapping)]
        for candidate in candidates:
            candidate['target_id'] = _candidate_id(candidate)
            candidate.setdefault('floor_id', current_floor)
        return (current_floor, candidates, requirements)

    def _requirement_type(requirement: Mapping[str, Any]) -> str:
        value = str(requirement.get('type') or requirement.get('mode') or 'quota_per_class').strip()
        if value == 'all':
            return 'mandatory_all'
        if value in {'sample_total', 'max_sample'}:
            return 'quota_per_class'
        return value

    def _fixed_mandatory(requirements: Sequence[Mapping[str, Any]]) -> tuple[set[str], str | None]:
        fixed: set[str] = set()
        for requirement in requirements:
            if _requirement_type(requirement) != 'mandatory_all':
                continue
            ids = requirement.get('candidate_ids') or requirement.get('targets') or []
            if not isinstance(ids, Sequence) or isinstance(ids, (str, bytes, bytearray)):
                return (set(), f"mandatory requirement {requirement.get('requirement_id')} has invalid candidate_ids")
            fixed.update((_as_id(value) for value in ids if _as_id(value)))
        return (fixed, None)

    def _combination_count(requirement: Mapping[str, Any], fixed: set[str]) -> int:
        kind = _requirement_type(requirement)
        if kind == 'mandatory_all':
            return 1
        if kind == 'distinct_category_group':
            categories = requirement.get('categories') or {}
            if not isinstance(categories, Mapping):
                return 0
            required_value = requirement.get('required_distinct_categories')
            required = max(0, int(required_value if required_value is not None else 2))
            available = []
            for category, raw_ids in categories.items():
                ids = [_as_id(value) for value in raw_ids or [] if _as_id(value) and _as_id(value) not in fixed]
                if ids:
                    available.append((str(category), len(ids)))
            if not available:
                return 1
            if len(available) < required:
                return 0
            per_category = int(requirement.get('instances_per_category') or 1)
            if per_category <= 0:
                return 1
            total = 0
            for chosen in itertools.combinations(available, required):
                value = 1
                for _category, count in chosen:
                    value *= math.comb(count, per_category) if count >= per_category else 0
                total += value
            return total
        ids = [_as_id(value) for value in requirement.get('candidate_ids') or [] if _as_id(value) and _as_id(value) not in fixed]
        required = max(0, int(requirement.get('required_count') or requirement.get('quota') or 0))
        return math.comb(len(ids), required) if len(ids) >= required else 0

    def _estimated_selected_target_count(requirements: Sequence[Mapping[str, Any]], fixed: set[str]) -> int:
        """Conservative target-count estimate used only for solver budgeting."""
        estimated = len(fixed)
        for requirement in requirements:
            kind = _requirement_type(requirement)
            if kind == 'mandatory_all':
                continue
            if kind == 'distinct_category_group':
                categories = requirement.get('categories') or {}
                if not isinstance(categories, Mapping):
                    continue
                required_value = requirement.get('required_distinct_categories')
                required_categories = max(0, int(required_value if required_value is not None else 2))
                per_category = max(1, int(requirement.get('instances_per_category') or 1))
                already_satisfied = 0
                for raw_ids in categories.values():
                    ids = {_as_id(value) for value in raw_ids or [] if _as_id(value)}
                    if len(ids & fixed) >= per_category:
                        already_satisfied += 1
                estimated += max(0, required_categories - already_satisfied) * per_category
                continue
            ids = {_as_id(value) for value in requirement.get('candidate_ids') or [] if _as_id(value)}
            required = max(0, int(requirement.get('required_count') or requirement.get('quota') or 0))
            estimated += max(0, required - len(ids & fixed))
        return estimated

    def _iter_options(requirement: Mapping[str, Any], fixed: set[str]) -> Iterator[set[str]]:
        kind = _requirement_type(requirement)
        if kind == 'mandatory_all':
            yield set()
            return
        if kind == 'distinct_category_group':
            categories = requirement.get('categories') or {}
            if not isinstance(categories, Mapping):
                return
            required_value = requirement.get('required_distinct_categories')
            required = max(0, int(required_value if required_value is not None else 2))
            per_category = max(1, int(requirement.get('instances_per_category') or 1))
            available: list[tuple[str, list[str]]] = []
            for category, raw_ids in categories.items():
                ids = sorted({_as_id(value) for value in raw_ids or [] if _as_id(value) and _as_id(value) not in fixed})
                if len(ids) >= per_category:
                    available.append((str(category), ids))
            if not available:
                yield set()
                return
            for chosen in itertools.combinations(available, required):
                option_lists = [list(itertools.combinations(ids, per_category)) for _category, ids in chosen]
                for product in itertools.product(*option_lists):
                    yield {value for group in product for value in group}
            return
        ids = sorted({_as_id(value) for value in requirement.get('candidate_ids') or [] if _as_id(value) and _as_id(value) not in fixed})
        required = max(0, int(requirement.get('required_count') or requirement.get('quota') or 0))
        for option in itertools.combinations(ids, required):
            yield set(option)

    def _held_karp(ids: Sequence[str], matrix: Mapping[str, Mapping[str, float | None]], *, max_segment_distance: float | None=None) -> tuple[float, list[str]] | None:
        """Exact open Hamiltonian path with a free start and free end."""
        ordered_ids = list(dict.fromkeys((_as_id(value) for value in ids)))
        count = len(ordered_ids)
        if count == 0:
            return (0.0, [])
        if count == 1:
            return (0.0, ordered_ids)
        dp: dict[tuple[int, int], float] = {}
        previous: dict[tuple[int, int], tuple[int, int] | None] = {}
        for index in range(count):
            state = (1 << index, index)
            dp[state] = 0.0
            previous[state] = None
        for mask_size in range(1, count):
            for mask in range(1 << count):
                if bin(mask).count('1') != mask_size:
                    continue
                for last in range(count):
                    state = (mask, last)
                    current = dp.get(state)
                    if current is None:
                        continue
                    for nxt in range(count):
                        if mask & 1 << nxt:
                            continue
                        edge = _distance(matrix, ordered_ids[last], ordered_ids[nxt])
                        if not math.isfinite(edge) or (max_segment_distance is not None and edge > max_segment_distance + EPS):
                            continue
                        new_state = (mask | 1 << nxt, nxt)
                        new_value = current + edge
                        old_value = dp.get(new_state, INF)
                        old_prev = previous.get(new_state)
                        if new_value < old_value - EPS or (abs(new_value - old_value) <= EPS and (old_prev is None or last < old_prev[1])):
                            dp[new_state] = new_value
                            previous[new_state] = state
        full = (1 << count) - 1
        finals = [(dp[full, last], last) for last in range(count) if (full, last) in dp]
        if not finals:
            return None
        best_value, last = min(finals, key=lambda item: (item[0], ordered_ids[item[1]]))
        state: tuple[int, int] | None = (full, last)
        reverse_order: list[str] = []
        while state is not None:
            mask, current = state
            reverse_order.append(ordered_ids[current])
            state = previous.get(state)
        reverse_order.reverse()
        return (best_value, reverse_order)

    def _order_length(order: Sequence[str], matrix: Mapping[str, Mapping[str, float | None]]) -> float:
        return sum((_distance(matrix, order[index - 1], order[index]) for index in range(1, len(order))))

    def _greedy_order(ids: Sequence[str], matrix: Mapping[str, Mapping[str, float | None]], *, max_segment_distance: float | None=None, enable_large_two_opt: bool=False) -> tuple[float, list[str]] | None:
        target_ids = list(dict.fromkeys((_as_id(value) for value in ids)))
        if len(target_ids) <= 1:
            return (0.0, target_ids)
        best: tuple[float, list[str]] | None = None
        capped_adjacency: dict[str, set[str]] | None = None
        if max_segment_distance is not None:
            capped_adjacency = {source: {target for target in target_ids if target != source and math.isfinite(_distance(matrix, source, target)) and (_distance(matrix, source, target) <= max_segment_distance + EPS)} for source in target_ids}
        if len(target_ids) <= 32:
            starts = target_ids
        else:
            sample_count = 4 if len(target_ids) > 64 else 8
            stride = max(1, (len(target_ids) - 1) // max(1, sample_count - 1))
            starts = list(dict.fromkeys((target_ids[index] for index in range(0, len(target_ids), stride))))[:sample_count]
            starts.extend([target_ids[0], target_ids[-1]])
            if max_segment_distance is not None:
                degrees = [(len((capped_adjacency or {}).get(source, set())), source) for source in target_ids]
                starts.extend((source for _degree, source in sorted(degrees)[:12]))
            starts = list(dict.fromkeys(starts))
        use_two_opt = len(target_ids) <= 80

        def build_from_start(start: str) -> tuple[float, list[str]] | None:
            unvisited = set(target_ids)
            unvisited.remove(start)
            order = [start]
            while unvisited:
                choices = [(_distance(matrix, order[-1], candidate), candidate) for candidate in unvisited]
                if max_segment_distance is not None:
                    choices = [item for item in choices if item[0] <= max_segment_distance + EPS]
                if not choices:
                    return None
                edge, candidate = min(choices, key=lambda item: (item[0], item[1]))
                if not math.isfinite(edge):
                    return None
                order.append(candidate)
                unvisited.remove(candidate)
            return (_order_length(order, matrix), order)

        def build_cap_aware_from_start(start: str) -> tuple[float, list[str]] | None:
            """Greedy rescue that avoids consuming low-degree endpoints too late."""
            if max_segment_distance is None:
                return None
            unvisited = set(target_ids)
            unvisited.remove(start)
            order = [start]
            while unvisited:
                choices = list(unvisited & (capped_adjacency or {}).get(order[-1], set()))
                if not choices:
                    return None

                def residual_key(candidate: str) -> tuple[int, float, str]:
                    degree = len(unvisited & (capped_adjacency or {}).get(candidate, set()))
                    return (degree, _distance(matrix, order[-1], candidate), candidate)
                candidate = min(choices, key=residual_key)
                order.append(candidate)
                unvisited.remove(candidate)
            return (_order_length(order, matrix), order)
        candidate_orders: list[list[str]] = []
        for start in starts:
            built = build_from_start(start)
            if built is not None:
                candidate_orders.append(built[1])
        if not candidate_orders and max_segment_distance is not None:
            for start in starts:
                built = build_cap_aware_from_start(start)
                if built is not None:
                    candidate_orders.append(built[1])
                    break
        for order in candidate_orders:
            if use_two_opt:
                improved = True
                while improved:
                    improved = False
                    current_length = _order_length(order, matrix)
                    for left in range(0, len(order) - 2):
                        for right in range(left + 2, len(order)):
                            candidate_order = order[:left] + list(reversed(order[left:right + 1])) + order[right + 1:]
                            if max_segment_distance is not None and any((_distance(matrix, candidate_order[index - 1], candidate_order[index]) > max_segment_distance + EPS for index in range(1, len(candidate_order)))):
                                continue
                            candidate_length = _order_length(candidate_order, matrix)
                            if candidate_length < current_length - EPS:
                                order = candidate_order
                                current_length = candidate_length
                                improved = True
            elif enable_large_two_opt:
                max_passes = 8
                for _pass in range(max_passes):
                    current_length = _order_length(order, matrix)
                    best_move: tuple[float, int, int] | None = None
                    for left in range(0, len(order) - 2):
                        for right in range(left + 2, len(order)):
                            old_boundary = 0.0
                            new_boundary = 0.0
                            if left > 0:
                                old_boundary += _distance(matrix, order[left - 1], order[left])
                                new_edge = _distance(matrix, order[left - 1], order[right])
                                if max_segment_distance is not None and new_edge > max_segment_distance + EPS:
                                    continue
                                new_boundary += new_edge
                            if right + 1 < len(order):
                                old_boundary += _distance(matrix, order[right], order[right + 1])
                                new_edge = _distance(matrix, order[left], order[right + 1])
                                if max_segment_distance is not None and new_edge > max_segment_distance + EPS:
                                    continue
                                new_boundary += new_edge
                            delta = new_boundary - old_boundary
                            if delta < -EPS and (best_move is None or delta < best_move[0]):
                                best_move = (delta, left, right)
                    if best_move is None:
                        break
                    _, left, right = best_move
                    proposal = order[:left] + list(reversed(order[left:right + 1])) + order[right + 1:]
                    proposal_length = _order_length(proposal, matrix)
                    if proposal_length < current_length - EPS:
                        order = proposal
                    else:
                        break
            length = _order_length(order, matrix)
            if best is None or (length, tuple(order)) < (best[0], tuple(best[1])):
                best = (length, order)
        return best

    def _insertion_cost(target_id: str, order: Sequence[str], matrix: Mapping[str, Mapping[str, float | None]], *, max_segment_distance: float | None=None) -> float:
        if not order:
            return 0.0
        values = [_distance(matrix, target_id, order[0]), _distance(matrix, order[-1], target_id)]
        if max_segment_distance is not None:
            values = [value for value in values if value <= max_segment_distance + EPS]
        for index in range(1, len(order)):
            left, right = (order[index - 1], order[index])
            a, b, c = (_distance(matrix, left, target_id), _distance(matrix, target_id, right), _distance(matrix, left, right))
            if math.isfinite(a) and math.isfinite(b) and math.isfinite(c) and (max_segment_distance is None or (a <= max_segment_distance + EPS and b <= max_segment_distance + EPS)):
                values.append(a + b - c)
        finite = [value for value in values if math.isfinite(value)]
        return min(finite) if finite else INF

    def _insert_at_best_position(target_id: str, order: Sequence[str], matrix: Mapping[str, Mapping[str, float | None]], *, max_segment_distance: float | None=None) -> list[str] | None:
        """Insert one target into an existing open order with minimum extra cost."""
        current = list(order)
        if not current:
            return [target_id]
        best: tuple[float, int, list[str]] | None = None
        for index in range(len(current) + 1):
            proposal = current[:index] + [target_id] + current[index:]
            if max_segment_distance is not None and any((_distance(matrix, proposal[position - 1], proposal[position]) > max_segment_distance + EPS for position in range(1, len(proposal)))):
                continue
            length = _order_length(proposal, matrix)
            if not math.isfinite(length):
                continue
            candidate = (length, index, proposal)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
        return best[2] if best is not None else None

    def _heuristic_selection(candidates: Mapping[str, Mapping[str, Any]], requirements: Sequence[Mapping[str, Any]], fixed: set[str], matrix: Mapping[str, Mapping[str, float | None]], known_ids: set[str] | None=None, max_segment_distance: float | None=None, max_group_combinations: int=5000, target_values: Mapping[str, float] | None=None, value_bias: float=0.0) -> set[str] | None:
        selected = set(fixed)
        current_route = _greedy_order(sorted(selected), matrix, max_segment_distance=max_segment_distance)
        if selected and current_route is None:
            return None
        order = current_route[1] if current_route else []

        def add_best(ids: Iterable[str], count: int) -> bool:
            nonlocal order
            for _ in range(count):
                choices = [(_insertion_cost(candidate, order, matrix, max_segment_distance=max_segment_distance), candidate) for candidate in ids if candidate not in selected and (known_ids is None or candidate in known_ids)]
                choices = [item for item in choices if math.isfinite(item[0])]
                if not choices:
                    return False
                positive_costs = sorted((cost for cost, _candidate in choices if cost > EPS))
                cost_scale = positive_costs[len(positive_costs) // 2] if positive_costs else 1.0
                _cost, chosen = min(choices, key=lambda item: (item[0] - max(0.0, float(value_bias)) * cost_scale * float((target_values or {}).get(item[1], 0.0)), item[0], item[1]))
                selected.add(chosen)
                inserted = _insert_at_best_position(chosen, order, matrix, max_segment_distance=max_segment_distance)
                if inserted is None:
                    return False
                order = inserted
            return True
        pending = sorted(requirements, key=lambda requirement: (0 if _requirement_type(requirement) == 'distinct_category_group' else 1, _combination_count(requirement, fixed), str(requirement.get('requirement_id') or '')))
        for requirement in pending:
            kind = _requirement_type(requirement)
            if kind == 'mandatory_all':
                continue
            if kind == 'distinct_category_group':
                categories = requirement.get('categories') or {}
                required_value = requirement.get('required_distinct_categories')
                required = max(0, int(required_value if required_value is not None else 2))
                per_category = max(1, int(requirement.get('instances_per_category') or 1))
                category_options = []
                if isinstance(categories, Mapping):
                    for category, raw_ids in categories.items():
                        ids = sorted({_as_id(value) for value in raw_ids or [] if _as_id(value) and _as_id(value) not in selected and (known_ids is None or _as_id(value) in known_ids)})
                        if len(ids) >= per_category:
                            category_options.append((str(category), ids))
                if len(category_options) < required:
                    return None if category_options else selected
                best_pair: tuple[float, float, tuple[str, ...], set[str]] | None = None
                combination_count = 0
                for _chosen_categories in itertools.combinations(category_options, required):
                    count = 1
                    for _name, ids in _chosen_categories:
                        count *= math.comb(len(ids), per_category) if len(ids) >= per_category else 0
                    combination_count += count
                if combination_count > max(1, int(max_group_combinations)):
                    category_combinations = itertools.combinations(category_options, required)
                    for chosen_categories in category_combinations:
                        option: set[str] = set()
                        working_order = list(order)
                        for _name, ids in chosen_categories:
                            available = [value for value in ids if value not in selected and value not in option]
                            chosen_instances: list[str] = []
                            for _ in range(per_category):
                                ranked: list[tuple[float, str]] = []
                                for value in available:
                                    cost = _insertion_cost(value, working_order, matrix, max_segment_distance=max_segment_distance)
                                    if math.isfinite(cost):
                                        ranked.append((cost, value))
                                if not ranked:
                                    break
                                positive = sorted((cost for cost, _value in ranked if cost > EPS))
                                scale = positive[len(positive) // 2] if positive else 1.0
                                chosen_value = min(ranked, key=lambda item: (item[0] - max(0.0, float(value_bias)) * scale * float((target_values or {}).get(item[1], 0.0)), item[0], item[1]))[1]
                                chosen_instances.append(chosen_value)
                                available.remove(chosen_value)
                                inserted = _insert_at_best_position(chosen_value, working_order, matrix, max_segment_distance=max_segment_distance)
                                if inserted is None:
                                    break
                                working_order = inserted
                            if len(chosen_instances) != per_category:
                                option = set()
                                break
                            option.update(chosen_instances)
                        if not option:
                            continue
                        route = (_order_length(working_order, matrix), working_order)
                        value_scale = max(route[0] / max(1, len(selected | option)), 1.0)
                        biased = route[0] - max(0.0, float(value_bias)) * value_scale * sum((float((target_values or {}).get(value, 0.0)) for value in option))
                        key = (biased, route[0], tuple(sorted(option)))
                        if best_pair is None or key < (best_pair[0], best_pair[1], best_pair[2]):
                            best_pair = (biased, route[0], tuple(sorted(option)), option)
                else:
                    for chosen_categories in itertools.combinations(category_options, required):
                        option_lists = [list(itertools.combinations(ids, per_category)) for _name, ids in chosen_categories]
                        for product in itertools.product(*option_lists):
                            option = {value for group in product for value in group}
                            route = _greedy_order(sorted(selected | option), matrix, max_segment_distance=max_segment_distance)
                            if route is None:
                                continue
                            value_scale = max(route[0] / max(1, len(selected | option)), 1.0)
                            biased = route[0] - max(0.0, float(value_bias)) * value_scale * sum((float((target_values or {}).get(value, 0.0)) for value in option))
                            key = (biased, route[0], tuple(sorted(option)))
                            if best_pair is None or key < (best_pair[0], best_pair[1], best_pair[2]):
                                best_pair = (biased, route[0], tuple(sorted(option)), option)
                if best_pair is None:
                    return None
                for chosen in sorted(best_pair[3]):
                    inserted = _insert_at_best_position(chosen, order, matrix, max_segment_distance=max_segment_distance)
                    if inserted is None:
                        return None
                    order = inserted
                    selected.add(chosen)
            else:
                ids = [_as_id(value) for value in requirement.get('candidate_ids') or [] if _as_id(value)]
                needed = max(0, int(requirement.get('required_count') or requirement.get('quota') or 0) - len(set(ids) & fixed))
                if not add_best(ids, needed):
                    return None
        return selected

    def _selected_value(selected: Iterable[str], candidates: Mapping[str, Mapping[str, Any]], values: Mapping[str, float] | None) -> float:
        total = 0.0
        for target_id in selected:
            if values is not None:
                try:
                    total += float(values.get(target_id, 0.0))
                    continue
                except (TypeError, ValueError):
                    pass
            for key in ('value', 'u_rule', 'target_value'):
                try:
                    if key in candidates[target_id]:
                        total += float(candidates[target_id][key])
                        break
                except (TypeError, ValueError):
                    continue
        return total

    def _result_for_order(order: Sequence[str], matrix: Mapping[str, Mapping[str, float | None]], block: Mapping[str, Any], *, max_segment_distance: float | None=None) -> tuple[float, list[dict[str, Any]]] | None:
        length = _order_length(order, matrix)
        if not math.isfinite(length):
            return None
        legs: list[dict[str, Any]] = []
        for index in range(1, len(order)):
            source, target = (order[index - 1], order[index])
            leg_distance = _distance(matrix, source, target)
            if max_segment_distance is not None and leg_distance > max_segment_distance + EPS:
                return None
            entry = _path_entry(block, source, target) or _path_entry(block, target, source)
            leg = {'from_target_id': source, 'to_target_id': target, 'distance': leg_distance, 'reachable': True}
            if entry:
                leg.update({key: value for key, value in entry.items() if key not in {'distance', 'reachable'}})
            legs.append(leg)
        return (length, legs)

    def _gap_statistics(legs: Sequence[Mapping[str, Any]], soft_gap_distance: float | None) -> dict[str, Any]:
        if soft_gap_distance is None:
            return {'soft_gap_distance': None, 'gap_excess': 0.0, 'long_gap_count': 0, 'max_target_gap': max((float(leg.get('distance') or 0.0) for leg in legs), default=0.0)}
        threshold = max(0.0, float(soft_gap_distance))
        distances = [float(leg.get('distance') or 0.0) for leg in legs]
        return {'soft_gap_distance': threshold, 'gap_excess': sum((max(0.0, distance - threshold) for distance in distances)), 'long_gap_count': sum((distance > threshold + EPS for distance in distances)), 'max_target_gap': max(distances, default=0.0)}

    def _audit_requirements(requirements: Sequence[Mapping[str, Any]], selected: set[str]) -> list[dict[str, Any]]:
        audit: list[dict[str, Any]] = []
        for requirement in requirements:
            kind = _requirement_type(requirement)
            if kind == 'distinct_category_group':
                categories = requirement.get('categories') or {}
                selected_categories = []
                available_categories = []
                if isinstance(categories, Mapping):
                    for category, raw_ids in categories.items():
                        ids = {_as_id(value) for value in raw_ids or []}
                        if ids:
                            available_categories.append(str(category))
                        if selected & ids:
                            selected_categories.append(str(category))
                required_value = requirement.get('required_distinct_categories')
                required_categories = int(required_value if required_value is not None else 2)
                not_present = not available_categories
                ok = not_present or len(selected_categories) == required_categories
                audit.append({'requirement_id': requirement.get('requirement_id'), 'rule_id': requirement.get('rule_id'), 'type': kind, 'status': 'not_present' if not_present else 'satisfied' if ok else 'violated', 'selected_count': sum((1 for category in selected_categories if category)), 'selected_categories': selected_categories, 'required_distinct_categories': required_categories})
                continue
            ids = {_as_id(value) for value in requirement.get('candidate_ids') or []}
            selected_count = len(selected & ids)
            required_count = len(ids) if kind == 'mandatory_all' else int(requirement.get('required_count') or requirement.get('quota') or 0)
            ok = selected_count == required_count
            audit.append({'requirement_id': requirement.get('requirement_id'), 'rule_id': requirement.get('rule_id'), 'type': kind, 'class_name': requirement.get('class_name'), 'status': 'not_present' if not ids else 'satisfied' if ok else 'violated', 'selected_count': selected_count, 'required_count': required_count, 'available_count': len(ids)})
        return audit

    def optimize_floor_route(target_candidates: Mapping[str, Any] | Sequence[Mapping[str, Any]], distance_matrix: Mapping[str, Any], floor_id: str | None=None, *, max_exact_targets: int=16, max_selection_combinations: int=50000, max_exact_work_units: int | None=None, distance_tolerance: float=0.0, target_values: Mapping[str, float] | None=None, soft_gap_distance: float | None=None, gap_penalty_weight: float=0.0, max_segment_distance: float | None=None) -> dict[str, Any]:
        """Solve one floor's open constrained route.

        ``distance_tolerance`` is a relative budget used only when values are
        supplied: after finding the shortest feasible route (L^*), a route up to
        ``(1 + distance_tolerance)L^*`` may win if it has greater target value.
        Set it to 0 for the pure shortest-route baseline. ``soft_gap_distance``
        records long target-to-target gaps; ``gap_penalty_weight`` adds their
        excess to route comparison while ``max_segment_distance`` is a hard cap.
        ``max_exact_work_units`` guards exact search using
        ``selection combinations * estimated selected targets``.  Exceeding the
        budget forces the auditable greedy/2-opt path.
        """
        current_floor, candidates_list, requirements = _normalise_floor_targets(target_candidates, floor_id)
        candidates = {_candidate_id(item): item for item in candidates_list if _candidate_id(item)}
        fixed, fixed_error = _fixed_mandatory(requirements)
        audit_base = _audit_requirements(requirements, fixed)
        if fixed_error:
            return {'floor_id': current_floor, 'status': 'infeasible', 'feasible': False, 'reason': fixed_error, 'selected_target_ids': [], 'order': [], 'length': None, 'selection_audit': audit_base, 'solver': 'precheck', 'optimality_proven': False}
        active_requirement = bool(fixed)
        for requirement in requirements:
            kind = _requirement_type(requirement)
            if kind == 'distinct_category_group':
                categories = requirement.get('categories') or {}
                active_requirement = active_requirement or bool(isinstance(categories, Mapping) and any((raw_ids for raw_ids in categories.values())))
            elif kind != 'mandatory_all':
                active_requirement = active_requirement or int(requirement.get('required_count') or requirement.get('quota') or 0) > 0
        if not active_requirement:
            return {'floor_id': current_floor, 'status': 'feasible', 'feasible': True, 'reason': 'no inspection target is present on this floor', 'selected_target_ids': [], 'order': [], 'start_target_id': None, 'end_target_id': None, 'length': 0.0, 'shortest_feasible_length': 0.0, 'distance_budget': 0.0, 'legs': [], 'soft_gap_distance': soft_gap_distance, 'gap_excess': 0.0, 'long_gap_count': 0, 'max_target_gap': 0.0, 'gap_penalty_weight': max(0.0, float(gap_penalty_weight or 0.0)), 'penalized_objective': 0.0, 'max_segment_distance': max_segment_distance, 'selected_targets': [], 'unselected_targets': candidates_list, 'selection_audit': audit_base, 'solver': 'no_targets', 'optimality_proven': True, 'selection_combination_count': 1, 'physical_distance_matrix': not _is_approximate_distance_source(distance_matrix), 'distance_approximate': _is_approximate_distance_source(distance_matrix)}
        try:
            block = _floor_block(distance_matrix, current_floor)
        except KeyError:
            return {'floor_id': current_floor, 'status': 'infeasible', 'feasible': False, 'reason': 'floor absent from distance matrix', 'selected_target_ids': [], 'order': [], 'length': None, 'selection_audit': audit_base, 'solver': 'precheck', 'optimality_proven': False}
        matrix = _normalise_matrix(block)
        known_ids = _known_ids(block, matrix)
        missing_fixed = sorted((target_id for target_id in fixed if target_id not in known_ids))
        if missing_fixed:
            return {'floor_id': current_floor, 'status': 'infeasible', 'feasible': False, 'reason': 'mandatory target absent from distance matrix', 'missing_target_ids': missing_fixed, 'selected_target_ids': [], 'order': [], 'length': None, 'selection_audit': audit_base, 'solver': 'precheck', 'optimality_proven': False}
        option_counts = [_combination_count(requirement, fixed) for requirement in requirements if _requirement_type(requirement) != 'mandatory_all']
        total_combinations = 1
        for count in option_counts:
            total_combinations *= count
            if total_combinations > max_selection_combinations:
                break
        estimated_selected_target_count = _estimated_selected_target_count(requirements, fixed)
        estimated_exact_work_units = total_combinations * max(1, estimated_selected_target_count)
        exact_work_budget = None if max_exact_work_units is None else max(0, int(max_exact_work_units))
        exact_work_budget_exceeded = bool(
            exact_work_budget is not None
            and estimated_exact_work_units > exact_work_budget
        )
        exact_eligible_without_work_budget = bool(
            len(fixed) <= max_exact_targets
            and estimated_selected_target_count <= max_exact_targets
            and total_combinations <= max_selection_combinations
            and all((count > 0 for count in option_counts))
        )
        exact_mode = exact_eligible_without_work_budget and not exact_work_budget_exceeded
        if exact_work_budget_exceeded and exact_eligible_without_work_budget:
            solver_selection_reason = 'exact_work_budget_exceeded'
        elif exact_mode:
            solver_selection_reason = 'within_exact_limits_and_work_budget'
        elif len(fixed) > max_exact_targets:
            solver_selection_reason = 'mandatory_target_count_exceeds_exact_limit'
        elif estimated_selected_target_count > max_exact_targets:
            solver_selection_reason = 'estimated_selected_target_count_exceeds_exact_limit'
        elif total_combinations > max_selection_combinations:
            solver_selection_reason = 'selection_combination_count_exceeds_exact_limit'
        else:
            solver_selection_reason = 'invalid_or_empty_optional_requirement_options'
        solver_budget_audit = {
            'estimated_selected_target_count': estimated_selected_target_count,
            'estimated_exact_work_units': estimated_exact_work_units,
            'max_exact_work_units': exact_work_budget,
            'exact_work_budget_exceeded': exact_work_budget_exceeded,
            'exact_eligible_without_work_budget': exact_eligible_without_work_budget,
            'solver_selection_reason': solver_selection_reason,
        }
        feasible_results: list[tuple[float, list[str], list[dict[str, Any]], set[str]]] = []
        if exact_mode:
            optional_requirements = [requirement for requirement in requirements if _requirement_type(requirement) != 'mandatory_all']
            option_iterators = [_iter_options(requirement, fixed) for requirement in optional_requirements]
            products: Iterable[tuple[set[str], ...]] = itertools.product(*option_iterators) if option_iterators else [tuple()]
            for option_tuple in products:
                selected = set(fixed)
                for option in option_tuple:
                    selected.update(option)
                if any((target_id not in known_ids for target_id in selected)):
                    continue
                if len(selected) > max_exact_targets:
                    continue
                route = _held_karp(sorted(selected), matrix, max_segment_distance=max_segment_distance)
                if route is None:
                    continue
                result = _result_for_order(route[1], matrix, block, max_segment_distance=max_segment_distance)
                if result is None:
                    continue
                feasible_results.append((result[0], route[1], result[1], selected))
        else:
            biases = [0.0]
            if target_values is not None and float(distance_tolerance or 0.0) > EPS:
                biases.extend((0.1, 0.25, 0.5, 1.0, 2.0, 4.0))
            seen_selections: set[frozenset[str]] = set()
            for value_bias in biases:
                selected = _heuristic_selection(candidates, requirements, fixed, matrix, known_ids=known_ids, max_segment_distance=max_segment_distance, max_group_combinations=min(max_selection_combinations, 500), target_values=target_values, value_bias=value_bias)
                frozen = frozenset(selected or set())
                if selected is None or frozen in seen_selections or (not all((target_id in known_ids for target_id in selected))):
                    continue
                seen_selections.add(frozen)
                route = _greedy_order(sorted(selected), matrix, max_segment_distance=max_segment_distance, enable_large_two_opt=True)
                if route is not None:
                    result = _result_for_order(route[1], matrix, block, max_segment_distance=max_segment_distance)
                    if result is not None:
                        feasible_results.append((result[0], route[1], result[1], selected))
        if not feasible_results:
            return {'floor_id': current_floor, 'status': 'infeasible', 'feasible': False, 'reason': 'no rule-feasible connected open route in distance matrix', 'unavailable_target_ids': sorted((target_id for target_id in candidates if target_id not in known_ids)), 'selected_target_ids': [], 'order': [], 'length': None, 'selection_audit': audit_base, 'solver': 'exact_enumeration_held_karp' if exact_mode else 'heuristic_greedy_2opt', 'optimality_proven': bool(exact_mode), 'selection_combination_count': total_combinations, **solver_budget_audit}
        shortest_length = min((item[0] for item in feasible_results))
        tolerance = max(0.0, float(distance_tolerance or 0.0))
        budget = shortest_length * (1.0 + tolerance) + EPS
        eligible = [item for item in feasible_results if item[0] <= budget]
        penalty_weight = max(0.0, float(gap_penalty_weight or 0.0))

        def penalized_cost(item: tuple[float, list[str], list[dict[str, Any]], set[str]]) -> float:
            gap = _gap_statistics(item[2], soft_gap_distance)['gap_excess']
            return item[0] + penalty_weight * float(gap)
        if target_values is not None or any((key in candidate for candidate in candidates.values() for key in ('value', 'u_rule', 'target_value'))):
            chosen = max(eligible, key=lambda item: (_selected_value(item[3], candidates, target_values), -penalized_cost(item), -item[0], tuple(sorted(item[3]))))
        else:
            chosen = min(eligible, key=lambda item: (penalized_cost(item), item[0], tuple(item[1])))
        length, order, legs, selected = chosen
        selection_audit = _audit_requirements(requirements, selected)
        selected_records = [candidates[target_id] for target_id in order if target_id in candidates]
        unselected_records = [candidate for target_id, candidate in candidates.items() if target_id not in selected]
        gap_statistics = _gap_statistics(legs, soft_gap_distance)
        return {'floor_id': current_floor, 'status': 'feasible', 'feasible': True, 'reason': None, 'selected_target_ids': sorted(selected), 'order': list(order), 'start_target_id': order[0] if order else None, 'end_target_id': order[-1] if order else None, 'length': length, 'shortest_feasible_length': shortest_length, 'distance_budget': budget, 'distance_tolerance': tolerance, 'legs': legs, **gap_statistics, 'gap_penalty_weight': penalty_weight, 'penalized_objective': length + penalty_weight * float(gap_statistics['gap_excess']), 'max_segment_distance': max_segment_distance, 'selected_targets': selected_records, 'unselected_targets': unselected_records, 'selection_audit': selection_audit, 'solver': 'exact_enumeration_held_karp' if exact_mode else 'heuristic_greedy_2opt', 'optimality_proven': bool(exact_mode and (not (target_values is not None or any((key in candidate for candidate in candidates.values() for key in ('value', 'u_rule', 'target_value'))))) and (tolerance <= EPS)), 'selection_combination_count': total_combinations, 'heuristic_variant_count': len(feasible_results) if not exact_mode else 0, 'physical_distance_matrix': not _is_approximate_distance_source(distance_matrix), 'distance_approximate': _is_approximate_distance_source(distance_matrix), **solver_budget_audit}
    optimize_route = optimize_floor_route
    solve_constrained_open_route = optimize_floor_route
    __all__ = ['optimize_floor_route', 'optimize_route', 'solve_constrained_open_route']
    return dict(locals())

_s08_optimizer = _register_embedded_module(
    'fire_inspection_system.semantic.route_optimizer',
    _build_s08_optimizer(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/semantic/target_selector.py
# -----------------------------------------------------------------------------
def _build_s08_selector():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/semantic/target_selector.py'
    )
    __name__ = 'fire_inspection_system.semantic.target_selector'
    __package__ = 'fire_inspection_system.semantic'
    """Rule-aware target candidate construction for the semantic inspection graph.

    This module deliberately stops before route optimisation.  It creates one
    candidate record per CAD entity and a set of per-floor hard requirements.  A
    route solver can then choose quota instances using an explicit target-distance
    matrix rather than fixing samples by their drawing order.

    The matching policy is the one used by ``inspection_route_planning``:
    for a given floor and rule class, confirmed CAD/raw-name candidates win; the
    classification name is considered only when that class has no raw-name
    candidate on the floor.  The import is lazy/fallback-safe so this module can be
    used by lightweight graph tests without importing ezdxf.
    """
    import copy
    import hashlib
    import json
    import math
    from collections import defaultdict
    from functools import lru_cache
    from pathlib import Path
    from typing import Any, Iterable
    MODULE_DIR = Path(__file__).resolve().parents[1]
    DEFAULT_CONSTRAINTS_PATH = MODULE_DIR / 'configs' / 'semantic_inspection_constraints.json'

    def _legacy_helpers() -> tuple[Any, Any, Any, Any]:
        """Return the existing selector helpers when the full application is available.

        ``inspection_route_planning`` imports ezdxf at module import time.  Semantic
        unit tests should not fail just because the CAD writer dependency is absent,
        hence the small local fallback below.
        """
        try:
            from inspection_route_planning import annotated_object_name, annotation_sort_key, candidates_for_rule_class, standard_class_name
            return (annotated_object_name, annotation_sort_key, candidates_for_rule_class, standard_class_name)
        except Exception:
            try:
                from fire_inspection_system.inspection_route_planning import annotated_object_name, annotation_sort_key, candidates_for_rule_class, standard_class_name
                return (annotated_object_name, annotation_sort_key, candidates_for_rule_class, standard_class_name)
            except Exception:
                pass
        try:
            try:
                from agents.inspection_object_library import alias_matches_value, cached_library, normalize_value
                from agents.inspection_class_fuzzy_matcher import standard_class_names
            except Exception:
                alias_matches_value = None
                cached_library = lambda _value: {'objects': []}
                normalize_value = lambda value: str(value or '').strip().lower()
                standard_class_names = lambda: []

            def _name(annotation: dict[str, Any]) -> str:
                for key in ('term', 'raw_text', 'norm_text', 'original_object_name', 'cad_object_name', 'raw_name', 'object_name'):
                    value = annotation.get(key)
                    if value is not None and str(value).strip():
                        return str(value).strip()
                return ''

            def _class(annotation: dict[str, Any]) -> str:
                return str(annotation.get('standard_class_name') or annotation.get('class_name') or annotation.get('classification_name') or '').strip()

            def _center(annotation: dict[str, Any]) -> tuple[float, float]:
                bbox = annotation.get('bbox')
                if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
                    try:
                        values = [float(value) for value in bbox[:4]]
                        if all((math.isfinite(value) for value in values)):
                            return ((values[0] + values[2]) / 2.0, (values[1] + values[3]) / 2.0)
                    except (TypeError, ValueError):
                        pass
                point = annotation.get('center') or annotation.get('point')
                if isinstance(point, (list, tuple)) and len(point) >= 2:
                    try:
                        return (float(point[0]), float(point[1]))
                    except (TypeError, ValueError):
                        pass
                return (0.0, 0.0)

            def _sort(annotation: dict[str, Any]) -> tuple[str, str, str, str]:
                x, y = _center(annotation)
                return (_class(annotation), str(annotation.get('object_id') or annotation.get('handle') or ''), f'{x:.6f}', f'{y:.6f}')
            library_objects = list(cached_library('').get('objects', []) or [])
            configured_standard_classes = set(standard_class_names())

            @lru_cache(maxsize=None)
            def _raw_rule_class(value: str) -> str:
                if not value:
                    return ''
                candidates: list[tuple[int, int, str]] = []
                for item in library_objects:
                    canonical = str(item.get('canonical') or '').strip()
                    if canonical not in configured_standard_classes:
                        continue
                    terms = [canonical, *(item.get('aliases', []) or []), *(item.get('abbreviations', []) or []), *(item.get('semantic_examples', []) or [])]
                    matched = []
                    for term in terms:
                        term = str(term).strip()
                        if not term:
                            continue
                        try:
                            ok = bool(alias_matches_value(term, value)) if alias_matches_value else normalize_value(term) in normalize_value(value)
                        except Exception:
                            ok = normalize_value(term) in normalize_value(value)
                        if ok:
                            matched.append(term)
                    if matched:
                        candidates.append((max((len(normalize_value(term)) for term in matched)), len(normalize_value(canonical)), canonical))
                return max(candidates)[2] if candidates else ''

            def _candidates(annotations: list[dict[str, Any]], class_name: str) -> tuple[list[dict[str, Any]], str]:
                original = [item for item in annotations if _raw_rule_class(_name(item)) == class_name]
                if original:
                    return (original, 'original_object_name')
                fallback = [item for item in annotations if not _raw_rule_class(_name(item)) and _class(item) == class_name]
                return (fallback, 'standard_class_name')
            return (_name, _sort, _candidates, _class)
        except Exception as exc:
            raise RuntimeError('unable to initialise inspection target matching helpers') from exc

    def _read_json(path: Path) -> dict[str, Any]:
        with path.open('r', encoding='utf-8') as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError(f'constraint config must be an object: {path}')
        return value

    def load_constraints(path: str | Path | None=None) -> dict[str, Any]:
        """Load and lightly validate the semantic constraint configuration."""
        if path is None:
            candidates = [DEFAULT_CONSTRAINTS_PATH, MODULE_DIR.parent / 'configs' / 'semantic_inspection_constraints.json']
            config_path = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
        else:
            config_path = Path(path)
        payload = _read_json(config_path)
        rules = payload.get('rules')
        if not isinstance(rules, list):
            raise ValueError("semantic constraints require a list named 'rules'")
        for rule in rules:
            if not isinstance(rule, dict):
                raise ValueError('each semantic rule must be an object')
            rule_type = str(rule.get('type') or rule.get('mode') or '').strip()
            if rule_type not in {'mandatory_all', 'quota_per_class', 'quota_total', 'distinct_category_group', 'all', 'sample_total', 'sample_each_subitem'}:
                raise ValueError(f'unsupported semantic rule type: {rule_type}')
            if rule_type == 'sample_each_subitem':
                if not isinstance(rule.get('subitems'), list) or not rule['subitems']:
                    raise ValueError(f"rule {rule.get('id')} has no subitems")
            elif not isinstance(rule.get('classes'), list) or not rule['classes']:
                raise ValueError(f"rule {rule.get('id')} has no classes")
        return payload

    def _value(annotation: dict[str, Any], *keys: str) -> Any:
        properties = annotation.get('properties')
        candidates: list[Any] = []
        if isinstance(properties, dict):
            candidates.extend((properties.get(key) for key in keys))
        candidates.extend((annotation.get(key) for key in keys))
        for candidate in candidates:
            if candidate is not None and str(candidate).strip():
                return candidate
        return None

    def _floor_id(annotation: dict[str, Any]) -> str:
        return str(_value(annotation, 'floor_id', 'floor', 'floor_name', 'sheet_id') or 'UNASSIGNED').strip() or 'UNASSIGNED'

    def _object_id(annotation: dict[str, Any]) -> str:
        return str(_value(annotation, 'object_id', 'handle', 'entity_id', 'cad_object_id', 'id') or '').strip()

    def _target_id(annotation: dict[str, Any], floor_id: str, ordinal: int) -> str:
        value = str(_value(annotation, 'target_id', 'inspection_target_id') or '').strip()
        if value:
            return value
        object_id = _object_id(annotation)
        if object_id:
            return object_id
        digest = hashlib.sha1(f'{floor_id}|{ordinal}|{repr(sorted(annotation.items(), key=lambda item: str(item[0])))}'.encode('utf-8', 'replace')).hexdigest()[:12]
        return f'{floor_id}:anonymous:{digest}'

    def _raw_name(annotation: dict[str, Any], legacy_name: Any) -> str:
        try:
            value = legacy_name(annotation)
        except Exception:
            value = ''
        if value:
            return str(value).strip()
        return str(_value(annotation, 'term', 'raw_text', 'norm_text', 'original_object_name', 'cad_object_name', 'raw_name', 'object_name') or '').strip()

    def _standard_name(annotation: dict[str, Any], legacy_class: Any) -> str:
        try:
            value = legacy_class(annotation)
        except Exception:
            value = ''
        if value:
            return str(value).strip()
        return str(_value(annotation, 'standard_class_name', 'class_name', 'classification_name') or '').strip()

    def _center(annotation: dict[str, Any]) -> tuple[float, float] | None:
        bbox = _value(annotation, 'bbox')
        if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
            try:
                values = [float(value) for value in bbox[:4]]
                if all((math.isfinite(value) for value in values)):
                    return ((values[0] + values[2]) / 2.0, (values[1] + values[3]) / 2.0)
            except (TypeError, ValueError):
                pass
        point = _value(annotation, 'center', 'point', 'centroid')
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            try:
                return (float(point[0]), float(point[1]))
            except (TypeError, ValueError):
                pass
        return None

    def _annotation_sort(annotation: dict[str, Any], legacy_sort: Any) -> tuple[Any, ...]:
        try:
            return tuple(legacy_sort(annotation))
        except Exception:
            point = _center(annotation) or (0.0, 0.0)
            return (_standard_name(annotation, lambda item: ''), _object_id(annotation), f'{point[0]:.6f}', f'{point[1]:.6f}')

    def _deduplicate_floor_annotations(annotations: Iterable[dict[str, Any]], floor_id: str, configured_classes: set[str], legacy_name: Any, legacy_sort: Any, legacy_class: Any) -> list[dict[str, Any]]:
        """Deduplicate CAD entities while retaining deterministic metadata."""
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        anonymous_index = 0
        for annotation in annotations:
            if not isinstance(annotation, dict):
                continue
            if _floor_id(annotation) != floor_id:
                continue
            object_id = _object_id(annotation)
            if not object_id:
                object_id = f'__anonymous__{anonymous_index}'
                anonymous_index += 1
            grouped[object_id].append(annotation)
        result: list[dict[str, Any]] = []
        for object_id, rows in grouped.items():

            def rank(item: dict[str, Any]) -> tuple[int, int, tuple[Any, ...]]:
                raw = _raw_name(item, legacy_name)
                raw_match = 0
                try:
                    try:
                        from inspection_route_planning import original_name_rule_class
                    except Exception:
                        from fire_inspection_system.inspection_route_planning import original_name_rule_class
                    raw_match = int(bool(raw and original_name_rule_class(raw) in configured_classes))
                except Exception:
                    raw_match = int(bool(raw))
                return (-raw_match, -int(bool(_standard_name(item, legacy_class))), _annotation_sort(item, legacy_sort))
            chosen = copy.deepcopy(sorted(rows, key=rank)[0])
            raw_name = _raw_name(chosen, legacy_name)
            if raw_name and (not str(chosen.get('term') or chosen.get('raw_text') or chosen.get('norm_text') or '').strip()):
                chosen['term'] = raw_name
            class_name = _standard_name(chosen, legacy_class)
            if class_name and (not str(chosen.get('standard_class_name') or '').strip()):
                chosen['standard_class_name'] = class_name
            chosen['_semantic_object_id'] = object_id
            result.append(chosen)
        return sorted(result, key=lambda item: (_object_id(item), _annotation_sort(item, legacy_sort)))

    def _normalise_rule(rule: dict[str, Any]) -> dict[str, Any]:
        rule_type = str(rule.get('type') or rule.get('mode') or '').strip()
        if rule_type == 'all':
            rule_type = 'mandatory_all'
        elif rule_type in {'sample_total', 'max_sample'}:
            rule_type = 'quota_per_class'
        classes = [str(value).strip() for value in rule.get('classes', []) if str(value).strip()]
        if rule_type == 'sample_each_subitem':
            for subitem in rule.get('subitems', []) or []:
                if isinstance(subitem, dict):
                    classes.extend((str(value).strip() for value in subitem.get('classes', []) if str(value).strip()))
            rule_type = 'quota_per_class'
        return {**rule, 'id': str(rule.get('id') or rule.get('name') or rule_type), 'name': str(rule.get('name') or rule.get('id') or rule_type), 'type': rule_type, 'classes': classes, 'quota': int(rule.get('quota') or rule.get('sample_size') or 2), 'required_distinct_categories': int(rule.get('required_distinct_categories') or 2), 'instances_per_category': int(rule.get('instances_per_category') or 1)}

    def _flatten_navigation_targets(value: Any) -> list[dict[str, Any]]:
        """Accept GeoJSON, closure target lists, or a plain iterable of records."""
        if value is None:
            return []
        if isinstance(value, (str, Path)):
            return _flatten_navigation_targets(_read_json(Path(value)))
        if isinstance(value, dict):
            if isinstance(value.get('features'), list):
                records: list[dict[str, Any]] = []
                for feature in value['features']:
                    if not isinstance(feature, dict):
                        continue
                    properties = feature.get('properties')
                    merged = dict(properties) if isinstance(properties, dict) else {}
                    if feature.get('id') is not None and (not merged.get('target_id')):
                        merged['target_id'] = feature['id']
                    geometry = feature.get('geometry')
                    if isinstance(geometry, dict) and geometry.get('type') == 'Point':
                        coordinates = geometry.get('coordinates')
                        if isinstance(coordinates, (list, tuple)) and len(coordinates) >= 2:
                            merged.setdefault('point', list(coordinates[:2]))
                    records.append(merged)
                return records
            for key in ('targets', 'inspection_targets', 'instances', 'selected_targets'):
                if isinstance(value.get(key), list):
                    return [dict(item) for item in value[key] if isinstance(item, dict)]
            return [dict(value)]
        return [dict(item) for item in value if isinstance(item, dict)]

    def _merge_navigation_identity(annotations: Iterable[dict[str, Any]], navigation_targets: Any) -> list[dict[str, Any]]:
        """Merge navigation target/access IDs through ``source_object_id``.

        ``collect_region_annotations`` owns the semantic name and CAD object ID,
        while ``navigation_targets.geojson`` owns the stable target ID later used
        by the rule graph and target-distance matrix.  Keeping the join here prevents the
        route layer from guessing IDs.
        """
        navigation = _flatten_navigation_targets(navigation_targets)
        by_floor_object: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        by_object: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for target in navigation:
            source_object_id = str(_value(target, 'source_object_id', 'object_id', 'cad_object_id', 'handle') or '').strip()
            if not source_object_id:
                continue
            target_floor = _floor_id(target)
            by_floor_object[target_floor, source_object_id].append(target)
            by_object[source_object_id].append(target)
        result: list[dict[str, Any]] = []
        for source in annotations:
            annotation = copy.deepcopy(source)
            object_id = _object_id(annotation)
            annotation_floor = _floor_id(annotation)
            matches = by_floor_object.get((annotation_floor, object_id), [])
            if not matches and object_id and (len(by_object.get(object_id, [])) == 1):
                matches = by_object[object_id]
            if matches:

                def nav_rank(item: dict[str, Any]) -> tuple[int, int, str]:
                    return (-int(bool(_value(item, 'access_node_id', 'virtual_access_node_id'))), -int(bool(_value(item, 'target_id', 'inspection_target_id'))), str(_value(item, 'target_id', 'inspection_target_id') or ''))
                target = sorted(matches, key=nav_rank)[0]
                for key in ('target_id', 'inspection_target_id', 'access_node_id', 'virtual_access_node_id', 'virtual_access', 'is_virtual_access', 'access_distance'):
                    value = _value(target, key)
                    if value is not None:
                        annotation[key] = value
                annotation['source_object_id'] = object_id
                annotation['_navigation_target'] = copy.deepcopy(target)
            result.append(annotation)
        return result

    def build_target_candidates(annotations: Iterable[dict[str, Any]], constraints: dict[str, Any] | str | Path | None=None, *, floor_id: str | None=None, navigation_targets: Any=None) -> dict[str, Any]:
        """Build per-floor candidates and hard rule requirements.

        The return value is JSON serialisable and intentionally independent of the
        physical graph.  ``route_optimizer.optimize_floor_route`` consumes one
        floor block together with a target-distance matrix.
        """
        config = load_constraints(constraints) if constraints is None or isinstance(constraints, (str, Path)) else copy.deepcopy(constraints)
        rules = [_normalise_rule(rule) for rule in config.get('rules', [])]
        merged_annotations = _merge_navigation_identity((item for item in annotations if isinstance(item, dict)), navigation_targets)
        if floor_id is not None:
            wanted_floor = str(floor_id)
            all_annotations = [item for item in merged_annotations if _floor_id(item) == wanted_floor]
        else:
            all_annotations = merged_annotations
        floors = sorted({_floor_id(item) for item in all_annotations})
        if floor_id is not None and str(floor_id) not in floors:
            floors.append(str(floor_id))
        legacy_name, legacy_sort, legacy_candidates, legacy_class = _legacy_helpers()
        configured_classes = {class_name for rule in rules for class_name in rule['classes']}
        output_floors: dict[str, Any] = {}
        for current_floor in sorted(floors):
            floor_rows = _deduplicate_floor_annotations(all_annotations, current_floor, configured_classes, legacy_name, legacy_sort, legacy_class)
            candidate_by_object: dict[str, dict[str, Any]] = {}
            candidate_order: list[str] = []
            pool_by_class: dict[str, list[str]] = defaultdict(list)
            pool_basis: dict[str, str] = {}
            object_ordinal = 0

            def add_pool_candidate(annotation: dict[str, Any], class_name: str, basis: str, rule_id: str) -> str:
                nonlocal object_ordinal
                object_key = str(annotation.get('_semantic_object_id') or _object_id(annotation) or '').strip()
                if not object_key:
                    object_key = f'__anonymous__{object_ordinal}'
                object_ordinal += 1
                if object_key in candidate_by_object:
                    record = candidate_by_object[object_key]
                    if rule_id not in record['matched_rule_ids']:
                        record['matched_rule_ids'].append(rule_id)
                    return record['target_id']
                target_id = _target_id(annotation, current_floor, object_ordinal)
                existing_ids = {str(record['target_id']) for record in candidate_by_object.values()}
                if target_id in existing_ids:
                    target_id = f'{target_id}#{object_ordinal}'
                point = _center(annotation)
                record = {'target_id': target_id, 'object_id': _object_id(annotation) or object_key, 'floor_id': current_floor, 'floor_name': str(_value(annotation, 'floor_name') or current_floor), 'class_name': class_name, 'raw_name': _raw_name(annotation, legacy_name), 'standard_class_name': _standard_name(annotation, legacy_class), 'selection_basis': basis, 'matched_rule_ids': [rule_id], 'access_node_id': _value(annotation, 'access_node_id', 'virtual_access_node_id'), 'virtual_access': bool(_value(annotation, 'virtual_access', 'is_virtual_access') or False), 'virtual_access_distance': _value(annotation, 'virtual_access_distance', 'access_distance', 'projection_distance'), 'source_object_id': str(_value(annotation, 'source_object_id') or _object_id(annotation) or object_key), 'point': list(point) if point is not None else None, 'annotation': copy.deepcopy({key: value for key, value in annotation.items() if not str(key).startswith('_')})}
                candidate_by_object[object_key] = record
                candidate_order.append(target_id)
                return target_id
            requirements: list[dict[str, Any]] = []
            audit: list[dict[str, Any]] = []
            for rule in rules:
                rule_id = rule['id']
                rule_type = rule['type']
                if rule_type == 'distinct_category_group':
                    categories: dict[str, list[str]] = {}
                    category_basis: dict[str, str] = {}
                    for class_name in rule['classes']:
                        candidates, basis = legacy_candidates(floor_rows, class_name)
                        ids = [add_pool_candidate(item, class_name, basis, rule_id) for item in candidates]
                        ids = list(dict.fromkeys(ids))
                        categories[class_name] = ids
                        category_basis[class_name] = basis
                        pool_by_class[class_name].extend(ids)
                        pool_basis[class_name] = basis
                    available_categories = [name for name, ids in categories.items() if ids]
                    configured_required_categories = max(0, int(rule['required_distinct_categories']))
                    effective_required_categories = min(configured_required_categories, len(available_categories))
                    unavailable_categories = [name for name in rule['classes'] if name not in available_categories]
                    availability_shortfall = max(0, configured_required_categories - effective_required_categories)
                    requirement = {
                        'requirement_id': rule_id,
                        'rule_id': rule_id,
                        'name': rule['name'],
                        'type': rule_type,
                        'classes': list(rule['classes']),
                        'categories': categories,
                        'required_distinct_categories': effective_required_categories,
                        'configured_required_distinct_categories': configured_required_categories,
                        'instances_per_category': rule['instances_per_category'],
                        'available_count': sum((len(ids) for ids in categories.values())),
                        'available_category_count': len(available_categories),
                        'available_categories': available_categories,
                        'unavailable_categories': unavailable_categories,
                        'availability_shortfall': availability_shortfall,
                    }
                    requirements.append(requirement)
                    status = 'not_present' if not available_categories else 'partially_available' if availability_shortfall else 'ready'
                    audit.append({
                        'requirement_id': rule_id,
                        'type': rule_type,
                        'status': status,
                        'available_categories': available_categories,
                        'unavailable_categories': unavailable_categories,
                        'configured_required_distinct_categories': configured_required_categories,
                        'required_distinct_categories': effective_required_categories,
                        'availability_shortfall': availability_shortfall,
                    })
                    continue
                if rule_type == 'quota_total':
                    aggregate_ids: list[str] = []
                    aggregate_basis: dict[str, str] = {}
                    for class_name in rule['classes']:
                        class_candidates, basis = legacy_candidates(floor_rows, class_name)
                        ids = [add_pool_candidate(item, class_name, basis, rule_id) for item in class_candidates]
                        aggregate_ids.extend(ids)
                        aggregate_basis[class_name] = basis
                        pool_by_class[class_name].extend(ids)
                        pool_basis[class_name] = basis
                    aggregate_ids = list(dict.fromkeys(aggregate_ids))
                    quota = max(0, min(rule['quota'], len(aggregate_ids))) if aggregate_ids else 0
                    requirement = {'requirement_id': rule_id, 'rule_id': rule_id, 'name': rule['name'], 'type': rule_type, 'candidate_ids': aggregate_ids, 'required_count': quota, 'configured_quota': rule['quota'], 'available_count': len(aggregate_ids), 'classes': list(rule['classes'])}
                    requirements.append(requirement)
                    audit.append({'requirement_id': rule_id, 'type': rule_type, 'status': 'not_present' if not aggregate_ids else 'satisfied', 'selection_basis_by_class': aggregate_basis, 'available_count': len(aggregate_ids), 'required_count': quota})
                    continue
                for class_name in rule['classes']:
                    candidates, basis = legacy_candidates(floor_rows, class_name)
                    ids = [add_pool_candidate(item, class_name, basis, rule_id) for item in candidates]
                    ids = list(dict.fromkeys(ids))
                    pool_by_class[class_name].extend(ids)
                    pool_basis[class_name] = basis
                    if rule_type == 'mandatory_all':
                        requirement = {'requirement_id': f'{rule_id}:{class_name}', 'rule_id': rule_id, 'name': rule['name'], 'type': rule_type, 'class_name': class_name, 'candidate_ids': ids, 'required_count': len(ids), 'available_count': len(ids)}
                        status = 'satisfied' if ids else 'not_present'
                    else:
                        quota = max(0, min(rule['quota'], len(ids))) if ids else 0
                        requirement = {'requirement_id': f'{rule_id}:{class_name}', 'rule_id': rule_id, 'name': rule['name'], 'type': rule_type, 'class_name': class_name, 'candidate_ids': ids, 'required_count': quota, 'configured_quota': rule['quota'], 'available_count': len(ids)}
                        status = 'not_present' if not ids else 'satisfied'
                    requirements.append(requirement)
                    audit.append({'requirement_id': requirement['requirement_id'], 'type': rule_type, 'class_name': class_name, 'status': status, 'selection_basis': basis if ids else 'none', 'available_count': len(ids), 'required_count': requirement['required_count']})
            rule_by_id = {rule['id']: rule for rule in rules}
            for record in candidate_by_object.values():
                record['matched_rule_ids'] = sorted(set(record['matched_rule_ids']))
                matched_rules = [rule_by_id[rule_id] for rule_id in record['matched_rule_ids'] if rule_id in rule_by_id]
                primary_rule = matched_rules[0] if matched_rules else {}
                record['target_class'] = record['class_name']
                record['rule_id'] = str(primary_rule.get('id') or '')
                record['rule_mode'] = str(primary_rule.get('type') or '')
                record['mandatory'] = any((rule.get('type') == 'mandatory_all' for rule in matched_rules))
                record['quota_group'] = str(primary_rule.get('id') or '')
            candidates = [candidate_by_object[object_key] for object_key in sorted(candidate_by_object)]
            output_floors[current_floor] = {'floor_id': current_floor, 'candidates': candidates, 'requirements': requirements, 'selection_audit': audit, 'candidate_count': len(candidates), 'deduplicated_annotation_count': len(floor_rows), 'matching_policy': config.get('matching_policy', {})}
        flat_candidates = [candidate for floor_block in output_floors.values() for candidate in floor_block.get('candidates', [])]
        return {'version': int(config.get('version') or 1), 'scope': 'per_floor', 'constraints': config, 'floors': output_floors, 'targets': flat_candidates}
    select_target_candidates = build_target_candidates
    build_semantic_target_candidates = build_target_candidates
    build_rule_candidates = build_target_candidates
    select_targets = build_target_candidates
    __all__ = ['DEFAULT_CONSTRAINTS_PATH', 'build_target_candidates', 'build_semantic_target_candidates', 'build_rule_candidates', 'load_constraints', 'select_targets', 'select_target_candidates']
    return dict(locals())

_s08_selector = _register_embedded_module(
    'fire_inspection_system.semantic.target_selector',
    _build_s08_selector(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/semantic/graph_builder.py
# -----------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/semantic/pipeline.py
# -----------------------------------------------------------------------------
def _build_s08_semantic_pipeline():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/semantic/pipeline.py'
    )
    __name__ = 'fire_inspection_system.semantic.pipeline'
    __package__ = 'fire_inspection_system.semantic'
    """End-to-end semantic inspection planning pipeline.

    This module is intentionally an integration layer.  It does not rebuild the
    obstacle, free-area, portal, or physical rule-navigation graph.  Those files
    remain the geometric source of truth; this pipeline joins their target IDs,
    builds a same-floor Euclidean target-distance approximation, applies the hard
    inspection rules, computes the A/N/C context features, and writes a semantic
    heterograph plus an auditable approximate open-route baseline.

    The command-line entry point is useful for a completed pipeline run::

        python -m semantic.pipeline --run-dir <run-directory>

    No physical shortest path or wall-safe route is computed here.  All distances
    used by this baseline are same-floor target-centre Euclidean distances.
    """
    import argparse
    import copy
    import hashlib
    import json
    import math
    from collections import defaultdict
    from pathlib import Path
    from typing import Any, Mapping, Sequence
    from fire_inspection_system.semantic.context_features import ContextFeatureConfig, compute_context_features, write_context_features
    from fire_inspection_system.semantic.route_optimizer import optimize_floor_route
    from fire_inspection_system.semantic.target_selector import build_target_candidates, load_constraints
    SCHEMA_VERSION = 1
    DEFAULT_VIRTUAL_ACCESS_RADIUS_RATIO = 0.01
    DEFAULT_EMPTY_GAP_RATIO = 0.2
    DEFAULT_HARD_GAP_RATIO = 0.8
    DEFAULT_CONTEXT_EXACT_WORK_BUDGET = 2000
    DEFAULT_DELTAS = (0.0, 0.05, 0.1)




    def _finite(value: Any, default: float=0.0) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if math.isfinite(number) else default

    def _text(value: Any) -> str:
        return '' if value is None else str(value).strip()


    def _normalised_rule_type(rule: Mapping[str, Any]) -> str:
        value = _text(rule.get('type') or rule.get('mode'))
        if value == 'all':
            return 'mandatory_all'
        if value in {'sample_total', 'max_sample'}:
            return 'quota_per_class'
        return value






    def _rule_index(constraints: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        by_id: dict[str, dict[str, Any]] = {}
        by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for raw in constraints.get('rules', []) or []:
            if not isinstance(raw, Mapping):
                continue
            rule = dict(raw)
            rule['id'] = _text(rule.get('id') or rule.get('name'))
            rule['type'] = _normalised_rule_type(rule)
            by_id[rule['id']] = rule
            for class_name in rule.get('classes', []) or []:
                by_class[_text(class_name)].append(rule)
        return (by_id, dict(by_class))

    def _decorate_candidates(bundle: Mapping[str, Any], physical_graph: Mapping[str, Any], constraints: Mapping[str, Any]) -> dict[str, Any]:
        physical_by_target = {_text(node.get('target_id')): dict(node) for node in physical_graph.get('nodes', []) or [] if isinstance(node, Mapping) and node.get('kind') == 'target' and node.get('target_id')}
        rules_by_id, rules_by_class = _rule_index(constraints)
        output = copy.deepcopy(dict(bundle))
        for floor_id, floor in (output.get('floors', {}) or {}).items():
            for candidate in floor.get('candidates', []) or []:
                target_id = _text(candidate.get('target_id'))
                physical = physical_by_target.get(target_id, {})
                for key in ('access_node_id', 'virtual_access', 'virtual_access_distance', 'area_id', 'component_id', 'assignment_status', 'x', 'y', 'raw_x', 'raw_y'):
                    if physical.get(key) is not None:
                        candidate[key] = physical.get(key)
                class_name = _text(candidate.get('class_name') or candidate.get('standard_class_name') or physical.get('target_class'))
                candidate['class_name'] = class_name
                candidate['target_class'] = class_name
                candidate['standard_class_name'] = class_name
                matched = [_text(value) for value in candidate.get('matched_rule_ids', []) or [] if _text(value)]
                applicable = [rules_by_id[value] for value in matched if value in rules_by_id]
                if not applicable:
                    applicable = rules_by_class.get(class_name, [])
                mandatory = any((_normalised_rule_type(rule) == 'mandatory_all' for rule in applicable))
                candidate['mandatory'] = mandatory
                if applicable:
                    rule = applicable[0]
                    candidate['rule_id'] = _text(rule.get('id'))
                    candidate['rule_mode'] = _normalised_rule_type(rule)
                    candidate['quota_group'] = _text(rule.get('id'))
                else:
                    candidate.setdefault('rule_id', '')
                    candidate.setdefault('rule_mode', '')
                    candidate.setdefault('quota_group', class_name)
                candidate['floor_id'] = _text(candidate.get('floor_id') or floor_id)
        return output

    def _flatten_candidates(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for floor in (bundle.get('floors', {}) or {}).values():
            if isinstance(floor, Mapping):
                rows.extend((dict(row) for row in floor.get('candidates', []) or [] if isinstance(row, Mapping)))
        return rows

    def _approximate_distance(distance_matrix: Mapping[str, Any], floor_id: str, source: str, target: str) -> float | None:
        floor = (distance_matrix.get('floors', {}) or {}).get(floor_id, {})
        matrix = floor.get('distances', {}) if isinstance(floor, Mapping) else {}
        row = matrix.get(source, {}) if isinstance(matrix, Mapping) else {}
        value = row.get(target) if isinstance(row, Mapping) else None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number >= 0.0 else None

    def _insertion_counterfactuals(candidates: Sequence[Mapping[str, Any]], routes: Mapping[str, Mapping[str, Any]], distance_matrix: Mapping[str, Any], floor_scales: Mapping[str, float]) -> dict[str, dict[str, float]]:
        """Estimate C by the extra route length needed to insert an instance.

        For a route order ``a, b``, insertion cost is
        ``d(a,i)+d(i,b)-d(a,b)``; endpoint insertion uses one finite leg.  The
        counterfactual is deliberately an auditable approximation of the user's
        “reduces total inspection distance” definition.  It uses the same-floor
        Euclidean approximation only; it is not a physical-path value.
        """
        by_floor: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for candidate in candidates:
            by_floor[_text(candidate.get('floor_id'))].append(candidate)
        result: dict[str, dict[str, float]] = {}
        for floor_id, rows in by_floor.items():
            route = routes.get(floor_id, {})
            order = [_text(value) for value in route.get('order', []) or [] if _text(value)]
            scale = max(_finite(floor_scales.get(floor_id), 1.0), 1.0)
            for candidate in rows:
                target_id = _text(candidate.get('target_id'))
                if not target_id:
                    continue
                if target_id in order:
                    cost = 0.0
                else:
                    costs: list[float] = []
                    if order:
                        for endpoint in (order[0], order[-1]):
                            value = _approximate_distance(distance_matrix, floor_id, target_id, endpoint)
                            if value is not None:
                                costs.append(value)
                    for left, right in zip(order, order[1:]):
                        d_left = _approximate_distance(distance_matrix, floor_id, left, target_id)
                        d_right = _approximate_distance(distance_matrix, floor_id, target_id, right)
                        d_direct = _approximate_distance(distance_matrix, floor_id, left, right)
                        if d_left is not None and d_right is not None and (d_direct is not None):
                            costs.append(max(0.0, d_left + d_right - d_direct))
                    cost = min(costs) if costs else scale
                result[target_id] = {'excluding': scale, 'forcing': min(scale, max(0.0, cost)), 'source': 'euclidean_insertion_coverage_proxy', 'approximate': True}
        return result

    def _route_with_values(bundle: Mapping[str, Any], distance_matrix: Mapping[str, Any], context: Mapping[str, Any], *, delta: float, empty_gap_by_floor: Mapping[str, float], hard_gap_by_floor: Mapping[str, float], max_exact_work_units: int=DEFAULT_CONTEXT_EXACT_WORK_BUDGET) -> dict[str, Any]:
        values = {_text(row.get('target_id')): _finite(row.get('u_rule'), 0.0) for row in context.get('targets', []) or [] if row.get('target_id')}
        floors: dict[str, Any] = {}
        for floor_id, floor in sorted((bundle.get('floors', {}) or {}).items()):
            floors[floor_id] = optimize_floor_route(floor, distance_matrix, floor_id, max_exact_work_units=max_exact_work_units, distance_tolerance=delta, target_values=values, soft_gap_distance=empty_gap_by_floor.get(floor_id), max_segment_distance=hard_gap_by_floor.get(floor_id))
            floors[floor_id]['distance_approximate'] = True
            floors[floor_id]['physical_path_available'] = False
            floors[floor_id]['wall_safe'] = False
            for leg in floors[floor_id].get('legs', []) or []:
                leg['distance_type'] = 'euclidean_target_centres_approximation'
                leg['physical_path_available'] = False
        return {'schema_version': SCHEMA_VERSION, 'route_type': 'same_floor_open_constrained_route', 'distance_metric': 'euclidean_target_centres_approximation', 'physical_path_available': False, 'delta': delta, 'solver_policy': {'purpose': 'approximate_context_coverage_proxy', 'exact_work_unit_formula': 'selection_combination_count * estimated_selected_target_count', 'max_exact_work_units': max_exact_work_units, 'over_budget_solver': 'heuristic_greedy_2opt'}, 'floors': floors}

    __all__ = ['DEFAULT_CONTEXT_EXACT_WORK_BUDGET', 'DEFAULT_EMPTY_GAP_RATIO', 'DEFAULT_HARD_GAP_RATIO', '_decorate_candidates', '_flatten_candidates', '_insertion_counterfactuals', '_route_with_values']
    return dict(locals())

_s08_semantic_pipeline = _register_embedded_module(
    'fire_inspection_system.semantic.pipeline',
    _build_s08_semantic_pipeline(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/semantic/pretraining_dataset.py
# -----------------------------------------------------------------------------
def _build_s08_dataset():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/semantic/pretraining_dataset.py'
    )
    __name__ = 'fire_inspection_system.semantic.pretraining_dataset'
    __package__ = 'fire_inspection_system.semantic'
    """Build floor-isolated tensors for edge-gated semantic-graph pretraining.

    The converter intentionally keeps the physical navigation graph and semantic
    control graph in one disjoint tensor while preserving node and relation types.
    No route or target-selection label is created here.
    """
    import hashlib
    import json
    import math
    from collections import Counter
    from pathlib import Path
    from typing import Any, Iterable, Mapping, Sequence
    import torch
    NODE_FEATURE_NAMES = ('x_normalized', 'y_normalized', 'log_degree_normalized', 'target_recognition_confidence', 'target_virtual_access', 'target_access_distance_normalized', 'area_fraction', 'area_portal_count_normalized', 'portal_width_normalized', 'portal_clearance_normalized', 'portal_bottleneck_score', 'portal_confidence', 'connector_gap_normalized', 'connector_confidence', 'rule_mandatory', 'rule_quota_normalized', 'category_frequency', 'bias')
    EDGE_FEATURE_NAMES = ('distance_or_length_normalized', 'portal_width_normalized', 'portal_clearance_normalized', 'confidence', 'bottleneck_score', 'connector_gap_normalized', 'connector_evidence_distance_normalized', 'near_decay')
    STANDARD_PORTAL_RELATIONS = {'CONNECTED_VIA_PORTAL', 'CONNECTS_AREA'}
    CONNECTOR_RELATIONS = {'CONNECTOR_ACCESS'}
    PHYSICAL_RELATIONS = {'NAVIGABLE', 'TARGET_ACCESS', 'AREA_ANCHOR_ACCESS', 'CONNECTOR_ACCESS', 'VIRTUAL_ACCESS', 'LOCAL_REFINEMENT'}


    def _finite(value: Any, default: float=0.0) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return default
        return result if math.isfinite(result) else default


    def _node_type_for_physical_kind(kind: str) -> str:
        if kind == 'target':
            return 'InspectionTarget'
        if kind == 'portal':
            return 'Portal'
        if kind == 'connector_portal':
            return 'ConnectorPortal'
        return 'NavigationNode'

    def _physical_relation(kind: str) -> str:
        return {'skeleton_edge': 'NAVIGABLE', 'target_access_edge': 'TARGET_ACCESS', 'area_anchor_access_edge': 'AREA_ANCHOR_ACCESS', 'connector_portal_edge': 'CONNECTOR_ACCESS', 'virtual_target_access_edge': 'VIRTUAL_ACCESS', 'local_refinement_edge': 'LOCAL_REFINEMENT'}.get(kind, f"PHYSICAL_{kind or 'UNKNOWN'}")

    class _RawFloorGraph:

        def __init__(self, graph_id: str, source_run_id: str, floor_id: str, reachability: float):
            self.graph_id = graph_id
            self.source_run_id = source_run_id
            self.floor_id = floor_id
            self.reachability = reachability
            self.nodes: list[dict[str, Any]] = []
            self.node_index: dict[str, int] = {}
            self.edges: list[dict[str, Any]] = []
            self._next_group = 0

        def add_node(self, node_id: str, node_type: str, *, x: float=0.0, y: float=0.0, category: str='', attrs: Mapping[str, Any] | None=None) -> int:
            if node_id in self.node_index:
                return self.node_index[node_id]
            index = len(self.nodes)
            self.node_index[node_id] = index
            self.nodes.append({'node_id': node_id, 'node_type': node_type, 'x': _finite(x), 'y': _finite(y), 'category': str(category or ''), 'attrs': dict(attrs or {})})
            return index

        def add_directed(self, source: int, relation: str, target: int, *, features: Mapping[str, Any] | None=None, group_id: int | None=None) -> int:
            if group_id is None:
                group_id = self._next_group
                self._next_group += 1
            self.edges.append({'source': source, 'target': target, 'relation': relation, 'features': dict(features or {}), 'group_id': group_id})
            return group_id

        def add_pair(self, source: int, forward_relation: str, target: int, reverse_relation: str, *, features: Mapping[str, Any] | None=None) -> None:
            group_id = self._next_group
            self._next_group += 1
            self.add_directed(source, forward_relation, target, features=features, group_id=group_id)
            self.add_directed(target, reverse_relation, source, features=features, group_id=group_id)

        def add_undirected(self, source: int, relation: str, target: int, *, features: Mapping[str, Any] | None=None) -> None:
            self.add_pair(source, relation, target, relation, features=features)

    def _floor_scale(nodes: Iterable[Mapping[str, Any]]) -> tuple[float, float, float]:
        positioned = [(_finite(node.get('x')), _finite(node.get('y'))) for node in nodes if math.isfinite(_finite(node.get('x'), math.nan)) and math.isfinite(_finite(node.get('y'), math.nan))]
        if not positioned:
            return (0.0, 0.0, 1.0)
        xs = [point[0] for point in positioned]
        ys = [point[1] for point in positioned]
        return (min(xs), min(ys), max(max(xs) - min(xs), max(ys) - min(ys), 1.0))

    def _build_raw_floor(source_run_id: str, floor_row: Mapping[str, Any], run: Mapping[str, Any], constraints: Mapping[str, Any], *, near_radius_factor: float=0.15, near_max_neighbors: int=32) -> _RawFloorGraph:
        floor_id = str(floor_row['floor_id'])
        reachability = _finite(floor_row.get('reachability'))
        graph = _RawFloorGraph(str(floor_row['floor_sample_id']), source_run_id, floor_id, reachability)
        physical_nodes = [dict(row) for row in run['physical'].get('nodes', []) or [] if str(row.get('floor_id') or '') == floor_id]
        physical_edges = [dict(row) for row in run['physical'].get('edges', []) or [] if str(row.get('floor_id') or '') == floor_id]
        area_nodes = [dict(row) for row in run['areas'].get('nodes', []) or [] if str(row.get('floor_id') or '') == floor_id]
        area_edges = [dict(row) for row in run['areas'].get('edges', []) or [] if str(row.get('floor_id') or '') == floor_id]
        target_props = run['target_props']
        candidate_by_target = run['candidate_by_target']
        portal_props = run['portal_props']
        connector_props = run['connector_props']
        physical_index: dict[str, int] = {}
        portal_index: dict[str, int] = {}
        target_index: dict[str, int] = {}
        area_index: dict[str, int] = {}
        for row in physical_nodes:
            kind = str(row.get('kind') or 'navigation')
            node_id = str(row.get('node_id') or '')
            if not node_id:
                continue
            target_id = str(row.get('target_id') or '')
            target_meta = target_props.get(target_id, {})
            category = str(row.get('target_class') or target_meta.get('target_class') or '')
            attrs = dict(row)
            attrs['recognition_confidence'] = _finite(target_meta.get('confidence'))
            attrs['candidate'] = target_id in candidate_by_target
            if target_id in candidate_by_target:
                attrs['rule_id'] = str(candidate_by_target[target_id].get('rule_id') or '')
            x = row.get('raw_x', row.get('x')) if kind == 'target' else row.get('x')
            y = row.get('raw_y', row.get('y')) if kind == 'target' else row.get('y')
            index = graph.add_node(f'physical::{node_id}', _node_type_for_physical_kind(kind), x=_finite(x), y=_finite(y), category=category, attrs=attrs)
            physical_index[node_id] = index
            if kind == 'portal' and row.get('portal_id'):
                portal_index[str(row['portal_id'])] = index
            if kind == 'target' and target_id:
                target_index[target_id] = index
        for area in area_nodes:
            area_id = str(area.get('area_id') or '')
            if not area_id:
                continue
            area_index[area_id] = graph.add_node(f'area::{area_id}', 'AreaRegion', x=_finite(area.get('centroid_x')), y=_finite(area.get('centroid_y')), attrs=area)
        categories = sorted({str(row.get('target_class') or '') for row in physical_nodes if row.get('kind') == 'target' and row.get('target_class')})
        category_index = {category: graph.add_node(f'category::{floor_id}::{category}', 'InspectionCategory', category=category, attrs={'category_name': category}) for category in categories}
        rules = [dict(row) for row in constraints.get('rules', []) or []]
        rule_index = {str(rule.get('id') or ''): graph.add_node(f"rule::{floor_id}::{rule.get('id')}", 'InspectionRule', attrs=rule) for rule in rules if rule.get('id')}
        floor_node = graph.add_node(f'floor::{source_run_id}::{floor_id}', 'FloorContext', attrs={'reachability': reachability})
        positioned = [graph.nodes[index] for index in [*physical_index.values(), *area_index.values()]]
        min_x, min_y, scale = _floor_scale(positioned)
        for edge in physical_edges:
            source = physical_index.get(str(edge.get('node_a') or ''))
            target = physical_index.get(str(edge.get('node_b') or ''))
            if source is None or target is None:
                continue
            relation = _physical_relation(str(edge.get('kind') or ''))
            features: dict[str, Any] = {'distance': edge.get('length')}
            connector_id = str(edge.get('connector_portal_id') or '')
            connector = connector_props.get(connector_id, {})
            if relation == 'CONNECTOR_ACCESS':
                features.update({'gap': connector.get('gap_distance'), 'evidence': connector.get('evidence_distance'), 'confidence': connector.get('confidence')})
            graph.add_undirected(source, relation, target, features=features)
        for row in physical_nodes:
            node_id = str(row.get('node_id') or '')
            physical = physical_index.get(node_id)
            area = area_index.get(str(row.get('area_id') or ''))
            if physical is None or area is None:
                continue
            if row.get('kind') == 'target':
                graph.add_pair(physical, 'LOCATED_IN_AREA', area, 'CONTAINS_TARGET')
            elif row.get('kind') == 'area_anchor':
                graph.add_pair(physical, 'ANCHORS_AREA', area, 'HAS_ANCHOR')
        for edge in area_edges:
            portal_id = str(edge.get('portal_id') or '')
            portal = portal_index.get(portal_id)
            details = portal_props.get(portal_id, {})
            if portal is None:
                continue
            features = {'width': details.get('width', edge.get('portal_width')), 'clearance': details.get('clearance'), 'confidence': details.get('confidence', edge.get('confidence')), 'bottleneck': details.get('bottleneck_score')}
            for key in ('area_a', 'area_b'):
                area = area_index.get(str(edge.get(key) or ''))
                if area is not None:
                    graph.add_pair(area, 'CONNECTED_VIA_PORTAL', portal, 'CONNECTS_AREA', features=features)
        category_counts = Counter((str(row.get('target_class') or '') for row in physical_nodes if row.get('kind') == 'target' and row.get('target_class')))
        for target_id, target in target_index.items():
            row = next((item for item in physical_nodes if str(item.get('target_id') or '') == target_id), {})
            category_name = str(row.get('target_class') or '')
            category = category_index.get(category_name)
            if category is not None:
                graph.add_pair(target, 'INSTANCE_OF_CATEGORY', category, 'HAS_INSTANCE')
            candidate = candidate_by_target.get(target_id, {})
            rule_id = str(candidate.get('rule_id') or '')
            rule = rule_index.get(rule_id)
            if rule is not None:
                graph.add_pair(target, 'GOVERNED_BY_RULE', rule, 'APPLIES_TO_TARGET')
        for rule in rules:
            rule_node = rule_index.get(str(rule.get('id') or ''))
            if rule_node is None:
                continue
            graph.add_pair(floor_node, 'HAS_RULE', rule_node, 'IN_FLOOR')
            for category_name in rule.get('classes', []) or []:
                category = category_index.get(str(category_name))
                if category is not None:
                    graph.add_pair(rule_node, 'APPLIES_TO_CATEGORY', category, 'GOVERNED_BY_RULE')
        for area in area_index.values():
            graph.add_pair(floor_node, 'CONTAINS_AREA', area, 'IN_FLOOR')
        for category in category_index.values():
            graph.add_pair(floor_node, 'HAS_CATEGORY', category, 'IN_FLOOR')
        target_positions = [(target_id, index, graph.nodes[index]['x'], graph.nodes[index]['y']) for target_id, index in target_index.items()]
        radius = max(scale * near_radius_factor, 1e-09)
        for target_id, source, x, y in target_positions:
            neighbors = []
            for other_id, target, other_x, other_y in target_positions:
                if target_id == other_id:
                    continue
                distance = math.hypot(other_x - x, other_y - y)
                if distance <= radius:
                    neighbors.append((distance, other_id, target))
            neighbors.sort(key=lambda row: (row[0], row[1]))
            for distance, _other_id, target in neighbors[:near_max_neighbors]:
                graph.add_directed(source, 'NEAR_TARGET', target, features={'distance': distance, 'decay': math.exp(-distance / radius)})
        degree = Counter()
        for edge in graph.edges:
            degree[edge['source']] += 1
            degree[edge['target']] += 1
        max_degree = max(degree.values(), default=1)
        floor_area_total = sum((max(0.0, _finite(area.get('area'))) for area in area_nodes)) or 1.0
        max_portal_count = max((_finite(area.get('portal_count')) for area in area_nodes), default=1.0) or 1.0
        target_count = max(len(target_index), 1)
        rule_by_id = {str(rule.get('id') or ''): rule for rule in rules}
        for index, node in enumerate(graph.nodes):
            attrs = node['attrs']
            node_type = node['node_type']
            features = [0.0] * len(NODE_FEATURE_NAMES)
            if node_type not in {'InspectionCategory', 'InspectionRule', 'FloorContext'}:
                features[0] = (node['x'] - min_x) / scale
                features[1] = (node['y'] - min_y) / scale
            features[2] = math.log1p(degree[index]) / max(math.log1p(max_degree), 1.0)
            if node_type == 'InspectionTarget':
                features[3] = _finite(attrs.get('recognition_confidence'))
                features[4] = float(bool(attrs.get('virtual_access')))
                access_distance = attrs.get('virtual_access_distance', attrs.get('projection_distance'))
                features[5] = max(0.0, _finite(access_distance)) / scale
            elif node_type == 'AreaRegion':
                features[6] = max(0.0, _finite(attrs.get('area'))) / floor_area_total
                features[7] = max(0.0, _finite(attrs.get('portal_count'))) / max_portal_count
            elif node_type == 'Portal':
                details = portal_props.get(str(attrs.get('portal_id') or ''), {})
                features[8] = max(0.0, _finite(details.get('width'))) / scale
                features[9] = max(0.0, _finite(details.get('clearance'))) / scale
                features[10] = _finite(details.get('bottleneck_score'))
                features[11] = _finite(details.get('confidence'))
            elif node_type == 'ConnectorPortal':
                details = connector_props.get(str(attrs.get('portal_id') or ''), {})
                features[12] = max(0.0, _finite(details.get('gap_distance'))) / scale
                features[13] = _finite(details.get('confidence', attrs.get('confidence')))
            elif node_type == 'InspectionRule':
                rule = rule_by_id.get(str(attrs.get('id') or ''), attrs)
                mode = str(rule.get('type') or '')
                features[14] = float(mode == 'mandatory_all')
                quota = rule.get('quota', rule.get('required_distinct_categories', 0))
                features[15] = min(1.0, max(0.0, _finite(quota)) / 4.0)
            elif node_type == 'InspectionCategory':
                features[16] = category_counts.get(node['category'], 0) / target_count
            features[17] = 1.0
            node['features'] = features
            node['scale'] = scale
        for edge in graph.edges:
            raw = edge['features']
            values = [0.0] * len(EDGE_FEATURE_NAMES)
            mask = [0.0] * len(EDGE_FEATURE_NAMES)

            def set_value(position: int, value: Any, divisor: float=1.0) -> None:
                if value is None:
                    return
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    return
                if not math.isfinite(number):
                    return
                values[position] = number / divisor
                mask[position] = 1.0
            set_value(0, raw.get('distance'), scale)
            set_value(1, raw.get('width'), scale)
            set_value(2, raw.get('clearance'), scale)
            set_value(3, raw.get('confidence'))
            set_value(4, raw.get('bottleneck'))
            set_value(5, raw.get('gap'), scale)
            set_value(6, raw.get('evidence'), scale)
            set_value(7, raw.get('decay'))
            edge['feature_values'] = values
            edge['feature_mask'] = mask
        return graph

    def _tensorize_graphs(raw_graphs: Sequence[_RawFloorGraph]) -> dict[str, Any]:
        node_types = sorted({node['node_type'] for graph in raw_graphs for node in graph.nodes})
        relations = sorted({edge['relation'] for graph in raw_graphs for edge in graph.edges})
        categories = sorted({node['category'] for graph in raw_graphs for node in graph.nodes if node['category']})
        node_type_to_id = {name: index for index, name in enumerate(node_types)}
        relation_to_id = {name: index for index, name in enumerate(relations)}
        category_to_id = {name: index for index, name in enumerate(categories)}
        node_features: list[list[float]] = []
        node_type_ids: list[int] = []
        node_category_ids: list[int] = []
        graph_ids: list[int] = []
        edge_sources: list[int] = []
        edge_targets: list[int] = []
        edge_type_ids: list[int] = []
        edge_features: list[list[float]] = []
        edge_feature_masks: list[list[float]] = []
        edge_group_ids: list[int] = []
        floor_slices: list[dict[str, Any]] = []
        node_ids: list[str] = []
        node_target_ids: list[str] = []
        node_floor_ids: list[str] = []
        node_source_run_ids: list[str] = []
        node_offset = 0
        edge_offset = 0
        group_offset = 0
        for graph_index, graph in enumerate(raw_graphs):
            start_node = node_offset
            start_edge = edge_offset
            for node in graph.nodes:
                node_ids.append(str(node.get('node_id') or ''))
                attrs = node.get('attrs') or {}
                node_target_ids.append(str(attrs.get('target_id') or ''))
                node_floor_ids.append(graph.floor_id)
                node_source_run_ids.append(graph.source_run_id)
                node_features.append(list(node['features']))
                node_type_ids.append(node_type_to_id[node['node_type']])
                node_category_ids.append(category_to_id.get(node['category'], -1))
                graph_ids.append(graph_index)
            for edge in graph.edges:
                edge_sources.append(node_offset + int(edge['source']))
                edge_targets.append(node_offset + int(edge['target']))
                edge_type_ids.append(relation_to_id[edge['relation']])
                edge_features.append(list(edge['feature_values']))
                edge_feature_masks.append(list(edge['feature_mask']))
                edge_group_ids.append(group_offset + int(edge['group_id']))
            node_offset += len(graph.nodes)
            edge_offset += len(graph.edges)
            group_offset += graph._next_group
            floor_slices.append({'graph_index': graph_index, 'floor_sample_id': graph.graph_id, 'source_run_id': graph.source_run_id, 'floor_id': graph.floor_id, 'reachability': graph.reachability, 'node_start': start_node, 'node_end': node_offset, 'edge_start': start_edge, 'edge_end': edge_offset})
        node_type_tensor = torch.tensor(node_type_ids, dtype=torch.long)
        target_type_id = node_type_to_id['InspectionTarget']
        category_relation_ids = [relation_to_id[name] for name in ('INSTANCE_OF_CATEGORY', 'HAS_INSTANCE') if name in relation_to_id]
        return {'schema_version': 2, 'node_feature_names': list(NODE_FEATURE_NAMES), 'edge_feature_names': list(EDGE_FEATURE_NAMES), 'node_type_names': node_types, 'relation_names': relations, 'category_names': categories, 'node_features': torch.tensor(node_features, dtype=torch.float32), 'node_ids': node_ids, 'node_target_ids': node_target_ids, 'node_floor_ids': node_floor_ids, 'node_source_run_ids': node_source_run_ids, 'node_type': node_type_tensor, 'node_category': torch.tensor(node_category_ids, dtype=torch.long), 'node_graph': torch.tensor(graph_ids, dtype=torch.long), 'edge_index': torch.tensor([edge_sources, edge_targets], dtype=torch.long), 'edge_type': torch.tensor(edge_type_ids, dtype=torch.long), 'edge_features': torch.tensor(edge_features, dtype=torch.float32), 'edge_feature_mask': torch.tensor(edge_feature_masks, dtype=torch.float32), 'edge_group': torch.tensor(edge_group_ids, dtype=torch.long), 'target_node_mask': node_type_tensor == target_type_id, 'category_relation_ids': torch.tensor(category_relation_ids, dtype=torch.long), 'floor_slices': floor_slices}
    __all__ = ['CONNECTOR_RELATIONS', 'EDGE_FEATURE_NAMES', 'NODE_FEATURE_NAMES', 'PHYSICAL_RELATIONS', 'STANDARD_PORTAL_RELATIONS']
    return dict(locals())

_s08_dataset = _register_embedded_module(
    'fire_inspection_system.semantic.pretraining_dataset',
    _build_s08_dataset(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/R-GNN/edge_gated_rgcn_pretraining.py
# -----------------------------------------------------------------------------
def _build_s08_rgcn_model():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/R-GNN/edge_gated_rgcn_pretraining.py'
    )
    __name__ = 'edge_gated_rgcn_pretraining'
    __package__ = ''
    """Pure-PyTorch edge-gated relational pretraining for semantic navigation.

    This model/training module lives in ``fire_inspection_system/R-GNN``; graph
    construction and tensorization remain in ``fire_inspection_system/semantic``.
    """
    import json
    import math
    from dataclasses import asdict, dataclass
    from pathlib import Path
    from typing import Any, Mapping, Sequence
    import torch
    from torch import nn
    from torch.nn import functional as F

    @dataclass(frozen=True)
    class PretrainingConfig:
        hidden_dim: int = 64
        relation_embedding_dim: int = 32
        layer_count: int = 2
        dropout: float = 0.1
        learning_rate: float = 0.002
        weight_decay: float = 0.0001
        epochs: int = 20
        seed: int = 42
        train_type_mask_ratio: float = 0.08
        train_category_mask_ratio: float = 0.15
        train_relation_mask_ratio: float = 0.05
        train_edge_attribute_mask_ratio: float = 0.1
        validation_type_ratio: float = 0.05
        validation_category_ratio: float = 0.1
        validation_relation_group_ratio: float = 0.05
        validation_edge_attribute_group_ratio: float = 0.1
        weight_type_loss: float = 0.5
        weight_category_loss: float = 1.0
        weight_relation_loss: float = 1.0
        weight_edge_attribute_loss: float = 0.5
        gradient_clip_norm: float = 5.0

        def validate(self) -> None:
            if self.hidden_dim < 8 or self.layer_count < 1 or self.epochs < 1:
                raise ValueError('hidden_dim, layer_count and epochs must be positive')
            if not 0.0 <= self.dropout < 1.0:
                raise ValueError('dropout must be in [0, 1)')
            for name, value in asdict(self).items():
                if name.endswith('ratio') and (not 0.0 <= float(value) < 1.0):
                    raise ValueError(f'{name} must be in [0, 1)')

    class EdgeGatedRelationalLayer(nn.Module):

        def __init__(self, hidden_dim: int, relation_count: int, edge_input_dim: int, dropout: float) -> None:
            super().__init__()
            self.hidden_dim = hidden_dim
            self.relation_count = relation_count
            self.relation_weight = nn.Parameter(torch.empty(relation_count, hidden_dim, hidden_dim))
            self.self_linear = nn.Linear(hidden_dim, hidden_dim)
            gate_hidden = max(16, hidden_dim // 2)
            self.gate_mlps = nn.ModuleList([nn.Sequential(nn.Linear(edge_input_dim, gate_hidden), nn.ReLU(), nn.Linear(gate_hidden, 1)) for _ in range(relation_count)])
            self.norm = nn.LayerNorm(hidden_dim)
            self.dropout = nn.Dropout(dropout)
            nn.init.xavier_uniform_(self.relation_weight)

        def forward(self, hidden: torch.Tensor, edge_index: torch.Tensor, edge_type: torch.Tensor, edge_input: torch.Tensor, message_mask: torch.Tensor, relation_names: Sequence[str]) -> tuple[torch.Tensor, dict[str, dict[str, float]]]:
            source_all, target_all = edge_index
            output = hidden.new_zeros(hidden.shape)
            denominator = hidden.new_zeros((hidden.shape[0], 1))
            gate_stats: dict[str, dict[str, float]] = {}
            for relation_id in range(self.relation_count):
                relation_mask = (edge_type == relation_id) & message_mask
                indices = torch.nonzero(relation_mask, as_tuple=False).flatten()
                if indices.numel() == 0:
                    continue
                source = source_all[indices]
                target = target_all[indices]
                gate = torch.sigmoid(self.gate_mlps[relation_id](edge_input[indices]))
                message = hidden[source] @ self.relation_weight[relation_id]
                output.index_add_(0, target, gate * message)
                denominator.index_add_(0, target, gate)
                detached = gate.detach()
                relation_name = relation_names[relation_id]
                gate_stats[relation_name] = {'count': int(detached.numel()), 'mean': float(detached.mean().item()), 'std': float(detached.std(unbiased=False).item()), 'min': float(detached.min().item()), 'max': float(detached.max().item())}
            aggregated = output / denominator.clamp_min(1.0)
            update = F.relu(self.self_linear(hidden) + aggregated)
            return (self.norm(hidden + self.dropout(update)), gate_stats)

    class EdgeGatedRGCNPretrainer(nn.Module):

        def __init__(self, *, node_feature_dim: int, edge_feature_dim: int, node_type_count: int, relation_count: int, category_count: int, relation_names: Sequence[str], config: PretrainingConfig) -> None:
            super().__init__()
            self.config = config
            self.node_type_count = node_type_count
            self.relation_count = relation_count
            self.category_count = category_count
            self.type_mask_id = node_type_count
            self.category_mask_id = category_count + 1
            self.relation_names = list(relation_names)
            hidden_dim = config.hidden_dim
            self.node_type_embedding = nn.Embedding(node_type_count + 1, hidden_dim)
            self.category_embedding = nn.Embedding(category_count + 2, hidden_dim)
            self.continuous_encoder = nn.Sequential(nn.Linear(node_feature_dim, hidden_dim), nn.ReLU(), nn.Linear(hidden_dim, hidden_dim))
            edge_input_dim = edge_feature_dim * 2
            self.layers = nn.ModuleList([EdgeGatedRelationalLayer(hidden_dim, relation_count, edge_input_dim, config.dropout) for _ in range(config.layer_count)])
            self.type_head = nn.Linear(hidden_dim, node_type_count)
            self.category_head = nn.Linear(hidden_dim, category_count)
            self.relation_embedding = nn.Embedding(relation_count, config.relation_embedding_dim)
            pair_dim = hidden_dim * 4
            self.relation_head = nn.Sequential(nn.Linear(pair_dim, hidden_dim), nn.ReLU(), nn.Dropout(config.dropout), nn.Linear(hidden_dim, relation_count))
            self.edge_attribute_head = nn.Sequential(nn.Linear(pair_dim + config.relation_embedding_dim, hidden_dim), nn.ReLU(), nn.Dropout(config.dropout), nn.Linear(hidden_dim, edge_feature_dim))

        def encode(self, node_features: torch.Tensor, node_type_input: torch.Tensor, category_input: torch.Tensor, edge_index: torch.Tensor, edge_type: torch.Tensor, edge_features: torch.Tensor, edge_feature_mask: torch.Tensor, message_mask: torch.Tensor) -> tuple[torch.Tensor, list[dict[str, dict[str, float]]]]:
            hidden = self.node_type_embedding(node_type_input) + self.category_embedding(category_input) + self.continuous_encoder(node_features)
            edge_input = torch.cat((edge_features * edge_feature_mask, edge_feature_mask), dim=1)
            all_gate_stats: list[dict[str, dict[str, float]]] = []
            for layer in self.layers:
                hidden, gate_stats = layer(hidden, edge_index, edge_type, edge_input, message_mask, self.relation_names)
                all_gate_stats.append(gate_stats)
            return (hidden, all_gate_stats)

    __all__ = ['EdgeGatedRGCNPretrainer', 'EdgeGatedRelationalLayer', 'PretrainingConfig']
    return dict(locals())

_s08_rgcn_model = _register_embedded_module(
    'edge_gated_rgcn_pretraining',
    _build_s08_rgcn_model(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/R-GNN/pseudo_label_finetuning.py
# -----------------------------------------------------------------------------
def _build_s08_route_heads():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/R-GNN/pseudo_label_finetuning.py'
    )
    __name__ = 'pseudo_label_finetuning'
    __package__ = ''
    """Fine-tune selection and transition heads from solver pseudo labels."""
    import argparse
    import json
    import math
    import random
    import time
    from collections import defaultdict
    from dataclasses import asdict, dataclass
    from itertools import combinations
    from pathlib import Path
    from typing import Any, Mapping, Sequence
    import torch
    from torch import nn
    from torch.nn import functional as F
    EPS = 1e-09

    def _text(value: Any) -> str:
        return '' if value is None else str(value).strip()

    def _finite(value: Any, default: float=0.0) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if math.isfinite(number) else default



    class SelectionHead(nn.Module):

        def __init__(self, input_dim: int, hidden_dim: int) -> None:
            super().__init__()
            self.network = nn.Sequential(nn.Linear(input_dim, hidden_dim), nn.ReLU(), nn.Dropout(0.1), nn.Linear(hidden_dim, hidden_dim // 2), nn.ReLU(), nn.Linear(hidden_dim // 2, 1))

        def forward(self, value: torch.Tensor) -> torch.Tensor:
            return self.network(value).squeeze(-1)

    class TransitionHead(nn.Module):

        def __init__(self, embedding_dim: int, context_dim: int, hidden_dim: int) -> None:
            super().__init__()
            pair_dim = embedding_dim * 4 + context_dim * 2
            self.network = nn.Sequential(nn.Linear(pair_dim, hidden_dim), nn.ReLU(), nn.Dropout(0.1), nn.Linear(hidden_dim, hidden_dim // 2), nn.ReLU(), nn.Linear(hidden_dim // 2, 1))

        def forward(self, left_embedding: torch.Tensor, right_embedding: torch.Tensor, left_context: torch.Tensor, right_context: torch.Tensor) -> torch.Tensor:
            features = torch.cat((left_embedding, right_embedding, torch.abs(left_embedding - right_embedding), left_embedding * right_embedding, left_context, right_context), dim=-1)
            return self.network(features).squeeze(-1)

    class PairSelectionHead(nn.Module):
        """Score an unordered pair of quota candidates from symmetric features."""

        def __init__(self, input_dim: int, hidden_dim: int) -> None:
            super().__init__()
            self.network = nn.Sequential(nn.Linear(input_dim, hidden_dim), nn.ReLU(), nn.Dropout(0.1), nn.Linear(hidden_dim, hidden_dim // 2), nn.ReLU(), nn.Linear(hidden_dim // 2, 1))

        def forward(self, value: torch.Tensor) -> torch.Tensor:
            return self.network(value).squeeze(-1)
    PAIR_GEOMETRY_FEATURE_NAMES = ('euclidean_distance_over_floor_scale', 'absolute_dx_over_floor_scale', 'absolute_dy_over_floor_scale', 'same_area', 'same_free_component', 'both_direct_access', 'any_virtual_access', 'disconnected_distance_proxy')

    def _embedding_index(graph_data: Mapping[str, Any], embeddings: torch.Tensor) -> dict[tuple[str, str, str], torch.Tensor]:
        grouped: dict[tuple[str, str, str], list[torch.Tensor]] = defaultdict(list)
        target_ids = graph_data.get('node_target_ids') or []
        floors = graph_data.get('node_floor_ids') or []
        sources = graph_data.get('node_source_run_ids') or []
        if not len(target_ids) == len(floors) == len(sources) == embeddings.shape[0]:
            raise ValueError('pretraining graph lacks aligned node target/floor/source identifiers; rebuild schema v2')
        for index, target_id in enumerate(target_ids):
            target_id = _text(target_id)
            if target_id:
                grouped[_text(sources[index]), _text(floors[index]), target_id].append(embeddings[index])
        return {key: torch.stack(values).mean(dim=0) for key, values in grouped.items()}

    def _context_vector(row: Mapping[str, Any]) -> list[float]:
        return [_finite(row.get('A')), _finite(row.get('N')), _finite(row.get('C')), _finite(row.get('u_rule')), 1.0 if bool(row.get('mandatory')) else 0.0, _finite(row.get('rule_need_weight'))]

    def _build_examples(pseudo: Mapping[str, Any], embedding_by_target: Mapping[tuple[str, str, str], torch.Tensor]) -> tuple[list[dict[str, Any]], dict[str, list[int]], dict[str, Any]]:
        examples: list[dict[str, Any]] = []
        floor_indices: dict[str, list[int]] = defaultdict(list)
        floor_payloads: dict[str, Any] = {}
        for floor_sample_id, floor in (pseudo.get('floors', {}) or {}).items():
            source = _text(floor.get('source_run_id'))
            floor_id = _text(floor.get('floor_id'))
            frequencies = (floor.get('soft_labels') or {}).get('selection_frequency', {}) or {}
            for candidate in floor.get('candidates', []) or []:
                target_id = _text(candidate.get('target_id'))
                embedding = embedding_by_target.get((source, floor_id, target_id))
                if embedding is None:
                    continue
                index = len(examples)
                examples.append({'floor_sample_id': floor_sample_id, 'source_run_id': source, 'floor_id': floor_id, 'target_id': target_id, 'embedding': embedding, 'context': torch.tensor(_context_vector(candidate), dtype=torch.float32), 'label': _finite(frequencies.get(target_id), 0.0), 'mandatory': bool(candidate.get('mandatory')), 'component_id': _text(candidate.get('component_id')), 'area_id': _text(candidate.get('area_id')), 'x': _finite(candidate.get('x'), _finite(candidate.get('raw_x'))), 'y': _finite(candidate.get('y'), _finite(candidate.get('raw_y'))), 'virtual_access': bool(candidate.get('virtual_access')), 'floor_scale': max(1.0, _finite(floor.get('floor_scale'), 1.0)), 'u_rule': _finite(candidate.get('u_rule'), 0.0)})
                floor_indices[floor_sample_id].append(index)
            floor_payloads[floor_sample_id] = floor
        return (examples, dict(floor_indices), floor_payloads)

    def _feature_tensor(examples: Sequence[Mapping[str, Any]], indices: Sequence[int]) -> torch.Tensor:
        return torch.stack([torch.cat((examples[index]['embedding'], examples[index]['context'])) for index in indices])


    def _pair_feature(left: Mapping[str, Any], right: Mapping[str, Any]) -> torch.Tensor:
        left_embedding = left['embedding']
        right_embedding = right['embedding']
        left_context = left['context']
        right_context = right['context']
        scale = max(1.0, _finite(left.get('floor_scale'), 1.0))
        dx = abs(_finite(left.get('x')) - _finite(right.get('x')))
        dy = abs(_finite(left.get('y')) - _finite(right.get('y')))
        distance = math.hypot(dx, dy)
        same_area = bool(_text(left.get('area_id'))) and _text(left.get('area_id')) == _text(right.get('area_id'))
        same_component = bool(_text(left.get('component_id'))) and _text(left.get('component_id')) == _text(right.get('component_id'))
        any_virtual = bool(left.get('virtual_access')) or bool(right.get('virtual_access'))
        geometry = torch.tensor([distance / scale, dx / scale, dy / scale, float(same_area), float(same_component), float(not any_virtual), float(any_virtual), distance / scale + (0.0 if same_component else 1.0)], dtype=torch.float32)
        return torch.cat((left_embedding + right_embedding, torch.abs(left_embedding - right_embedding), left_embedding * right_embedding, left_context + right_context, torch.abs(left_context - right_context), left_context * right_context, geometry))

    def _teacher_solution_sets(floor: Mapping[str, Any]) -> list[set[str]]:
        solutions: list[set[str]] = []
        for rows in (floor.get('solutions_by_delta') or {}).values():
            for row in rows or []:
                selected = {_text(value) for value in row.get('selected_target_ids', []) or [] if _text(value)}
                if selected:
                    solutions.append(selected)
        if solutions:
            return solutions
        for row in (floor.get('chosen_by_delta') or {}).values():
            selected = {_text(value) for value in row.get('selected_target_ids', []) or [] if _text(value)}
            if selected:
                solutions.append(selected)
        return solutions

    def _build_pair_rows(examples: Sequence[Mapping[str, Any]], floor_indices: Mapping[str, Sequence[int]], floor_payloads: Mapping[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for floor_sample_id, indices in floor_indices.items():
            by_target = {examples[index]['target_id']: index for index in indices}
            floor = floor_payloads[floor_sample_id]
            teacher_solutions = _teacher_solution_sets(floor)
            for requirement in floor.get('requirements', []) or []:
                kind = _text(requirement.get('type'))
                required = max(0, int(requirement.get('required_count') or requirement.get('quota') or 0))
                if kind in {'mandatory_all', 'distinct_category_group'} or required != 2:
                    continue
                requirement_id = _text(requirement.get('requirement_id'))
                candidate_ids = [target_id for target_id in dict.fromkeys(map(_text, requirement.get('candidate_ids', []) or [])) if target_id in by_target]
                if len(candidate_ids) < 2:
                    continue
                for left_id, right_id in combinations(candidate_ids, 2):
                    left_index, right_index = (by_target[left_id], by_target[right_id])
                    label = sum((left_id in selected and right_id in selected for selected in teacher_solutions)) / len(teacher_solutions) if teacher_solutions else 0.0
                    rows.append({'floor_sample_id': floor_sample_id, 'source_run_id': examples[left_index]['source_run_id'], 'floor_id': examples[left_index]['floor_id'], 'requirement_id': requirement_id, 'left_id': left_id, 'right_id': right_id, 'left_index': left_index, 'right_index': right_index, 'label': float(label), 'feature': _pair_feature(examples[left_index], examples[right_index])})
        return rows


    def _pair_scores(model: PairSelectionHead, mean: torch.Tensor, std: torch.Tensor, pair_rows: Sequence[Mapping[str, Any]], allowed_sources: set[str], device: torch.device) -> dict[int, float]:
        indices = [index for index, row in enumerate(pair_rows) if row['source_run_id'] in allowed_sources]
        if not indices:
            return {}
        features = ((torch.stack([pair_rows[index]['feature'] for index in indices]) - mean) / std).to(device)
        model.eval()
        with torch.no_grad():
            values = torch.sigmoid(model(features)).cpu().tolist()
        return {index: float(value) for index, value in zip(indices, values)}






    def _select_by_scores(floor: Mapping[str, Any], scores: Mapping[str, float]) -> set[str]:
        selected: set[str] = set()
        requirements = floor.get('requirements', []) or []
        for requirement in requirements:
            kind = _text(requirement.get('type'))
            if kind == 'mandatory_all':
                selected.update(map(_text, requirement.get('candidate_ids', []) or []))
        for requirement in requirements:
            kind = _text(requirement.get('type'))
            if kind in {'mandatory_all', 'distinct_category_group'}:
                continue
            ids = list(dict.fromkeys(map(_text, requirement.get('candidate_ids', []) or [])))
            required = max(0, int(requirement.get('required_count') or requirement.get('quota') or 0))
            needed = max(0, required - len(set(ids) & selected))
            ranked = sorted((item for item in ids if item not in selected), key=lambda item: (-scores.get(item, 0.0), item))
            selected.update(ranked[:needed])
        for requirement in requirements:
            if _text(requirement.get('type')) != 'distinct_category_group':
                continue
            categories = requirement.get('categories') or {}
            required_value = requirement.get('required_distinct_categories')
            required = max(0, int(required_value if required_value is not None else 2))
            per_category = max(1, int(requirement.get('instances_per_category') or 1))
            ranked_categories = []
            for category, raw_ids in categories.items():
                ids = sorted(map(_text, raw_ids or []), key=lambda item: (-scores.get(item, 0.0), item))
                if len(ids) >= per_category:
                    chosen = ids[:per_category]
                    ranked_categories.append((sum((scores.get(item, 0.0) for item in chosen)), _text(category), chosen))
            for _score, _category, chosen in sorted(ranked_categories, key=lambda row: (-row[0], row[1]))[:required]:
                selected.update(chosen)
        return selected






    __all__ = ['SelectionHead', 'PairSelectionHead', 'TransitionHead', 'PAIR_GEOMETRY_FEATURE_NAMES', '_build_pair_rows', '_pair_scores']
    return dict(locals())

_s08_route_heads = _register_embedded_module(
    'pseudo_label_finetuning',
    _build_s08_route_heads(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/R-GNN/export_route_recommendations.py
# -----------------------------------------------------------------------------
def _build_s08_route_export():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/R-GNN/export_route_recommendations.py'
    )
    __name__ = 'export_route_recommendations'
    __package__ = ''
    """Export R-GCN head recommendations for the constraint route solver.

    This is an inference bridge: the neural model recommends quota/optional
    instances and local successor candidates.  It does not relax business rules or
    declare a physical route feasible; those decisions remain with the solver.
    """
    import argparse
    import json
    import math
    import sys
    from pathlib import Path
    from typing import Any
    import torch
    ROOT = Path(__file__).resolve().parents[2]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from pseudo_label_finetuning import PairSelectionHead, SelectionHead, TransitionHead, _build_examples, _build_pair_rows, _embedding_index, _feature_tensor, _pair_scores, _select_by_scores

    def _read_json(path: Path) -> Any:
        with path.open('r', encoding='utf-8') as handle:
            return json.load(handle)

    def _write_json(path: Path, payload: Any) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        return path

    def export_route_recommendations(pseudo_label_path: Path | str, graph_tensor_path: Path | str, embedding_path: Path | str, checkpoint_path: Path | str, output_path: Path | str, *, transition_top_k: int=20, device_name: str='auto') -> dict[str, Any]:
        device = torch.device('cuda' if device_name == 'auto' and torch.cuda.is_available() else device_name if device_name != 'auto' else 'cpu')
        pseudo_path = Path(pseudo_label_path).resolve()
        graph_path = Path(graph_tensor_path).resolve()
        embeddings_path = Path(embedding_path).resolve()
        checkpoint_file = Path(checkpoint_path).resolve()
        pseudo = _read_json(pseudo_path)
        graph_data = torch.load(graph_path, map_location='cpu', weights_only=False)
        embedding_data = torch.load(embeddings_path, map_location='cpu', weights_only=False)
        checkpoint = torch.load(checkpoint_file, map_location='cpu', weights_only=False)
        embeddings = embedding_data['node_embeddings'].float()
        embedding_by_target = _embedding_index(graph_data, embeddings)
        examples, floor_indices, floor_payloads = _build_examples(pseudo, embedding_by_target)
        if not examples:
            raise ValueError('no candidates join the pseudo-label rows to R-GCN embeddings')
        embedding_dim = int(checkpoint['embedding_dim'])
        context_dim = len(checkpoint['context_feature_names'])
        hidden_dim = int((checkpoint.get('config') or {}).get('hidden_dim', 64))
        selection = SelectionHead(embedding_dim + context_dim, hidden_dim).to(device)
        transition = TransitionHead(embedding_dim, context_dim, hidden_dim).to(device)
        selection.load_state_dict(checkpoint['selection_head_state_dict'])
        transition.load_state_dict(checkpoint['transition_head_state_dict'])
        selection.eval()
        transition.eval()
        mean = checkpoint['selection_feature_mean'].float()
        std = checkpoint['selection_feature_std'].float().clamp_min(1e-06)
        pair_rows = _build_pair_rows(examples, floor_indices, floor_payloads)
        grouped_pairs: dict[tuple[str, str], list[tuple[int, float]]] = {}
        pair_mlp_weight = float((checkpoint.get('config') or {}).get('pair_mlp_weight', 0.5))
        if pair_rows and 'pair_selection_head_state_dict' in checkpoint:
            pair_mean = checkpoint['pair_feature_mean'].float()
            pair_std = checkpoint['pair_feature_std'].float().clamp_min(1e-06)
            pair_model = PairSelectionHead(int(pair_mean.numel()), hidden_dim).to(device)
            pair_model.load_state_dict(checkpoint['pair_selection_head_state_dict'])
            pair_model.eval()
            sources = {row['source_run_id'] for row in examples}
            all_pair_scores = _pair_scores(pair_model, pair_mean, pair_std, pair_rows, sources, device)
            for index, row in enumerate(pair_rows):
                grouped_pairs.setdefault((row['floor_sample_id'], row['requirement_id']), []).append((index, all_pair_scores.get(index, 0.0)))
        floor_outputs: dict[str, Any] = {}
        with torch.no_grad():
            for floor_sample_id, indices in floor_indices.items():
                if not indices:
                    continue
                features = ((_feature_tensor(examples, indices) - mean) / std).to(device)
                probabilities = torch.sigmoid(selection(features)).cpu().tolist()
                selection_scores = {examples[index]['target_id']: float(score) for index, score in zip(indices, probabilities)}
                recommended = _select_by_scores(floor_payloads[floor_sample_id], selection_scores)
                pair_rankings: dict[str, list[dict[str, Any]]] = {}
                for (pair_floor_id, requirement_id), values in grouped_pairs.items():
                    if pair_floor_id != floor_sample_id:
                        continue
                    blended = []
                    for pair_index, pair_score in values:
                        pair_row = pair_rows[pair_index]
                        unary_score = (selection_scores.get(pair_row['left_id'], 0.0) + selection_scores.get(pair_row['right_id'], 0.0)) / 2.0
                        score = pair_mlp_weight * pair_score + (1.0 - pair_mlp_weight) * unary_score
                        blended.append((pair_index, score, pair_score, unary_score))
                    ranked_pairs = sorted(blended, key=lambda item: (-item[1], pair_rows[item[0]]['left_id'], pair_rows[item[0]]['right_id']))[:max(1, transition_top_k)]
                    pair_rankings[requirement_id] = [{'target_ids': [pair_rows[index]['left_id'], pair_rows[index]['right_id']], 'score': float(score), 'pair_mlp_score': float(pair_score), 'unary_mean_score': float(unary_score)} for index, score, pair_score, unary_score in ranked_pairs]
                successor_top_k: dict[str, list[dict[str, Any]]] = {}
                for source_index in indices:
                    source = examples[source_index]
                    candidates = [index for index in indices if index != source_index and (not source.get('component_id') or not examples[index].get('component_id') or examples[index].get('component_id') == source.get('component_id'))]
                    if not candidates:
                        successor_top_k[source['target_id']] = []
                        continue
                    left_e = source['embedding'].repeat(len(candidates), 1).to(device)
                    right_e = torch.stack([examples[index]['embedding'] for index in candidates]).to(device)
                    left_c = source['context'].repeat(len(candidates), 1).to(device)
                    right_c = torch.stack([examples[index]['context'] for index in candidates]).to(device)
                    scores = torch.sigmoid(transition(left_e, right_e, left_c, right_c)).cpu().tolist()
                    ranked = sorted(zip(candidates, scores), key=lambda item: (-float(item[1]), examples[item[0]]['target_id']))[:max(1, transition_top_k)]
                    successor_top_k[source['target_id']] = [{'target_id': examples[index]['target_id'], 'score': float(score)} for index, score in ranked if math.isfinite(float(score))]
                first = examples[indices[0]]
                floor_outputs[floor_sample_id] = {'source_run_id': first['source_run_id'], 'floor_id': first['floor_id'], 'selection_scores': selection_scores, 'recommended_target_ids': sorted(recommended), 'pair_selection_top_k': pair_rankings, 'successor_top_k': successor_top_k}
        result = {'schema_version': 1, 'artifact_type': 'rgcn_route_solver_recommendations', 'authority': {'selection_and_insertion_ranking': 'R-GCN unary, unordered-pair and transition heads', 'business_constraints_and_physical_feasibility': 'constraint solver'}, 'transition_top_k': max(1, transition_top_k), 'source_files': {'pseudo_labels': str(pseudo_path), 'graph_tensor': str(graph_path), 'embeddings': str(embeddings_path), 'checkpoint': str(checkpoint_file)}, 'floors': floor_outputs}
        output = _write_json(Path(output_path).resolve(), result)
        return {**result, 'output_path': str(output)}
    return dict(locals())

_s08_route_export = _register_embedded_module(
    'export_route_recommendations',
    _build_s08_route_export(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/R-GNN/export_full_floor_recommendations.py
# -----------------------------------------------------------------------------
def _build_s08_full_export():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/R-GNN/export_full_floor_recommendations.py'
    )
    __name__ = 'export_full_floor_recommendations'
    __package__ = ''
    """Run the trained heterogeneous R-GCN and route heads on every source floor.

    The original training manifest intentionally excluded floors below 80 percent
    target reachability.  That is appropriate for training, but it must not cause
    route-time rule-value fallback.  This inference bridge rebuilds floor graphs
    for the requested source, aligns their categorical vocabularies to the frozen
    checkpoint, performs a full-information R-GCN forward pass, and then exports
    selection, pair and transition scores for every candidate with an embedding.
    """
    import argparse
    import copy
    import json
    import math
    import sys
    from pathlib import Path
    from typing import Any, Mapping, Sequence
    import torch
    ROOT = Path(__file__).resolve().parents[2]
    RGNN_DIR = Path(__file__).resolve().parent
    for value in (ROOT, RGNN_DIR):
        if str(value) not in sys.path:
            sys.path.insert(0, str(value))
    from edge_gated_rgcn_pretraining import EdgeGatedRGCNPretrainer, PretrainingConfig
    from export_route_recommendations import export_route_recommendations
    from fire_inspection_system.semantic.pretraining_dataset import _build_raw_floor, _tensorize_graphs
    from fire_inspection_system.semantic.target_selector import load_constraints
    from fire_inspection_system.dual_graph.physical_access import augment_virtual_target_access_nodes

    def _read(path: Path | str) -> Any:
        with Path(path).resolve().open('r', encoding='utf-8') as handle:
            return json.load(handle)

    def _write(path: Path | str, value: Any) -> Path:
        output = Path(path).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        return output

    def _finite(value: Any, default: float=0.0) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return default
        return result if math.isfinite(result) else default

    def _expanded_source_manifest(base: Mapping[str, Any], source_run_id: str, floor_filter: set[str] | None=None, *, runtime_run_dir: Path | str | None=None, available_floor_ids: Sequence[str] | None=None) -> tuple[dict[str, Any], list[str]]:
        manifest = copy.deepcopy(dict(base))
        sources = manifest.get('source_runs', []) or []
        source = next((row for row in sources if str(row.get('source_run_id') or '') == source_run_id), None)
        if source is None:
            if runtime_run_dir is None:
                raise KeyError(f'source_run_id missing from manifest: {source_run_id}')
            source = {'source_run_id': source_run_id, 'run_dir': str(Path(runtime_run_dir).resolve()), 'selected_floor_ids': [], 'excluded_floors': {}, 'runtime_inference_source': True}
        else:
            source = copy.deepcopy(dict(source))
            if runtime_run_dir is not None:
                source['run_dir'] = str(Path(runtime_run_dir).resolve())
                source['runtime_inference_source'] = True
        rows = {str(row.get('floor_id') or ''): copy.deepcopy(dict(row)) for row in manifest.get('floor_samples', []) or [] if str(row.get('source_run_id') or '') == source_run_id}
        for floor_id, info in (source.get('excluded_floors') or {}).items():
            reachability = info.get('reachability') if isinstance(info, Mapping) else None
            if reachability is None or str(floor_id).upper() == 'ROOF':
                continue
            rows.setdefault(str(floor_id), {'floor_sample_id': f'{source_run_id}__{floor_id}', 'source_run_id': source_run_id, 'floor_id': str(floor_id), 'reachability': _finite(reachability)})
        for floor_id in available_floor_ids or []:
            floor_id = str(floor_id)
            if not floor_id:
                continue
            rows.setdefault(floor_id, {'floor_sample_id': f'{source_run_id}__{floor_id}', 'source_run_id': source_run_id, 'floor_id': floor_id, 'reachability': 1.0, 'runtime_inference_floor': True})
        if floor_filter:
            rows = {floor_id: row for floor_id, row in rows.items() if floor_id in floor_filter}
        floor_ids = sorted(rows)
        source_copy = copy.deepcopy(dict(source))
        source_copy['selected_floor_ids'] = floor_ids
        source_copy['excluded_floors'] = {key: value for key, value in (source_copy.get('excluded_floors') or {}).items() if key not in rows}
        manifest['source_runs'] = [source_copy]
        manifest['floor_samples'] = [rows[floor_id] for floor_id in floor_ids]
        manifest['dataset_id'] = f"{base.get('dataset_id', 'semantic')}__{source_run_id}__all_floor_inference"
        floor_selection = dict(manifest.get('floor_selection') or {})
        floor_selection['minimum_inclusive'] = 0.0
        floor_selection['purpose'] = 'frozen_checkpoint_all_floor_inference_only'
        manifest['floor_selection'] = floor_selection
        totals = dict(manifest.get('totals_for_selected_floors') or {})
        totals['floor_count'] = len(floor_ids)
        totals['target_count'] = -1
        manifest['totals_for_selected_floors'] = totals
        return (manifest, floor_ids)

    def _remap_index_tensor(values: torch.Tensor, source_names: Sequence[str], target_names: Sequence[str], *, unknown_value: int | None=None) -> tuple[torch.Tensor, list[str]]:
        target_index = {name: index for index, name in enumerate(target_names)}
        mapping: dict[int, int] = {}
        unknown: list[str] = []
        for index, name in enumerate(source_names):
            if name in target_index:
                mapping[index] = target_index[name]
            elif unknown_value is not None:
                mapping[index] = unknown_value
                unknown.append(name)
            else:
                unknown.append(name)
        if unknown and unknown_value is None:
            raise ValueError(f'checkpoint vocabulary does not contain: {unknown}')
        result = torch.full_like(values, unknown_value if unknown_value is not None else 0)
        for source_index, target_index_value in mapping.items():
            result[values == source_index] = target_index_value
        return (result, unknown)

    def _align_feature_columns(values: torch.Tensor, source_names: Sequence[str], target_names: Sequence[str]) -> torch.Tensor:
        source_index = {name: index for index, name in enumerate(source_names)}
        missing = [name for name in target_names if name not in source_index]
        if missing:
            raise ValueError(f'inference graph lacks checkpoint features: {missing}')
        return values[:, [source_index[name] for name in target_names]]

    def _align_to_checkpoint(graph: Mapping[str, Any], checkpoint: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        aligned = dict(graph)
        node_types, unknown_types = _remap_index_tensor(graph['node_type'], graph['node_type_names'], checkpoint['node_type_names'])
        edge_types, unknown_relations = _remap_index_tensor(graph['edge_type'], graph['relation_names'], checkpoint['relation_names'])
        categories, unknown_categories = _remap_index_tensor(graph['node_category'], graph['category_names'], checkpoint['category_names'], unknown_value=-1)
        aligned.update({'node_type': node_types, 'edge_type': edge_types, 'node_category': categories, 'node_type_names': list(checkpoint['node_type_names']), 'relation_names': list(checkpoint['relation_names']), 'category_names': list(checkpoint['category_names']), 'node_features': _align_feature_columns(graph['node_features'], graph['node_feature_names'], checkpoint['node_feature_names']), 'edge_features': _align_feature_columns(graph['edge_features'], graph['edge_feature_names'], checkpoint['edge_feature_names']), 'edge_feature_mask': _align_feature_columns(graph['edge_feature_mask'], graph['edge_feature_names'], checkpoint['edge_feature_names']), 'node_feature_names': list(checkpoint['node_feature_names']), 'edge_feature_names': list(checkpoint['edge_feature_names'])})
        return (aligned, {'unknown_node_types': unknown_types, 'unknown_relations': unknown_relations, 'unknown_categories_mapped_to_no_category': unknown_categories})

    def _pretraining_config(checkpoint: Mapping[str, Any]) -> PretrainingConfig:
        raw = checkpoint.get('config') or {}
        fields = PretrainingConfig.__dataclass_fields__
        return PretrainingConfig(**{key: raw[key] for key in fields if key in raw})

    @torch.no_grad()
    def _infer_embeddings(graph: Mapping[str, Any], checkpoint: Mapping[str, Any], device: torch.device) -> tuple[torch.Tensor, list[dict[str, dict[str, float]]]]:
        config = _pretraining_config(checkpoint)
        model = EdgeGatedRGCNPretrainer(node_feature_dim=int(graph['node_features'].shape[1]), edge_feature_dim=int(graph['edge_features'].shape[1]), node_type_count=len(checkpoint['node_type_names']), relation_count=len(checkpoint['relation_names']), category_count=len(checkpoint['category_names']), relation_names=checkpoint['relation_names'], config=config).to(device)
        model.load_state_dict(checkpoint['model_state_dict'])
        model.eval()
        node_type = graph['node_type'].to(device)
        category = torch.where(graph['node_category'] >= 0, graph['node_category'] + 1, torch.zeros_like(graph['node_category'])).to(device)
        message_mask = torch.ones(graph['edge_type'].shape[0], dtype=torch.bool, device=device)
        hidden, gates = model.encode(graph['node_features'].to(device), node_type, category, graph['edge_index'].to(device), graph['edge_type'].to(device), graph['edge_features'].to(device), graph['edge_feature_mask'].to(device), message_mask)
        return (hidden.cpu(), gates)

    def _physical_target_index(refined_graph: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
        return {str(row.get('target_id')): row for row in refined_graph.get('nodes', []) or [] if row.get('kind') == 'target' and row.get('target_id')}

    def _feature_properties(payload: Mapping[str, Any], id_key: str) -> dict[str, dict[str, Any]]:
        return {str((feature.get('properties') or {}).get(id_key) or ''): dict(feature.get('properties') or {}) for feature in payload.get('features', []) or [] if isinstance(feature, Mapping) and (feature.get('properties') or {}).get(id_key)}

    def _build_inference_graph_from_existing_candidates(expanded_manifest: Mapping[str, Any], candidate_bundle: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Reuse the research floor-graph builder without re-running target matching."""
        source = (expanded_manifest.get('source_runs') or [])[0]
        run_dir = Path(str(source.get('run_dir') or '')).resolve()
        physical, virtual_access_audit = augment_virtual_target_access_nodes(_read(run_dir / 'area_graph_navigation_refined' / 'refined_navigation_graph.json'), candidate_bundle)
        areas = _read(run_dir / 'area_graph' / 'area_graph.json')
        portals = _read(run_dir / 'area_graph' / 'portal_candidates.geojson')
        connectors = _read(run_dir / 'area_graph_navigation_refined' / 'connector_portals.geojson')
        targets = _read(run_dir / 'navigation_graph' / 'inputs' / 'navigation_targets.geojson')
        candidate_by_target = {str(row.get('target_id') or ''): dict(row) for floor in (candidate_bundle.get('floors') or {}).values() for row in floor.get('candidates', []) or [] if row.get('target_id')}
        run = {'physical': physical, 'areas': areas, 'target_props': _feature_properties(targets, 'target_id'), 'portal_props': _feature_properties(portals, 'portal_id'), 'connector_props': _feature_properties(connectors, 'portal_id'), 'candidate_by_target': candidate_by_target}
        constraints = load_constraints(None)
        raw_graphs = [_build_raw_floor(str(row.get('source_run_id') or ''), row, run, constraints) for row in expanded_manifest.get('floor_samples', []) or []]
        payload = _tensorize_graphs(raw_graphs)
        payload['dataset_id'] = expanded_manifest.get('dataset_id')
        return (payload, {'builder': 'existing_pretraining_dataset._build_raw_floor_and_tensorize_graphs', 'target_matching_recomputed': False, 'candidate_source': 'existing_target_candidates_with_context', 'floor_count': len(raw_graphs), 'node_count': int(payload['node_features'].shape[0]), 'edge_count_directed': int(payload['edge_index'].shape[1]), 'target_node_count': int(payload['target_node_mask'].sum().item()), 'cross_floor_edges_allowed': False, 'virtual_access': virtual_access_audit})

    def _route_inference_payload(candidates: Mapping[str, Any], refined_graph: Mapping[str, Any], source_run_id: str, floor_filter: set[str] | None=None) -> dict[str, Any]:
        physical = _physical_target_index(refined_graph)
        physical_floor_points: dict[str, list[tuple[float, float]]] = {}
        for node in refined_graph.get('nodes', []) or []:
            floor_id = str(node.get('floor_id') or '')
            x, y = (_finite(node.get('x'), math.nan), _finite(node.get('y'), math.nan))
            if floor_id and math.isfinite(x) and math.isfinite(y):
                physical_floor_points.setdefault(floor_id, []).append((x, y))
        floors: dict[str, Any] = {}
        for floor_id, raw_floor in (candidates.get('floors') or {}).items():
            if floor_filter and str(floor_id) not in floor_filter:
                continue
            floor = copy.deepcopy(dict(raw_floor))
            positioned: list[tuple[float, float]] = []
            for candidate in floor.get('candidates', []) or []:
                node = physical.get(str(candidate.get('target_id') or ''), {})
                x = _finite(node.get('x'), _finite(candidate.get('raw_x')))
                y = _finite(node.get('y'), _finite(candidate.get('raw_y')))
                candidate.update({'x': x, 'y': y, 'component_id': str(node.get('graph_component') or node.get('component_id') or ''), 'virtual_access': bool(node.get('virtual_access')), 'access_backend': 'refined_physical_navigation_graph'})
                positioned.append((x, y))
            xs = [row[0] for row in positioned]
            ys = [row[1] for row in positioned]
            floor['floor_sample_id'] = f'{source_run_id}__{floor_id}'
            floor['source_run_id'] = source_run_id
            floor['floor_id'] = str(floor_id)
            scale_points = physical_floor_points.get(str(floor_id), positioned)
            scale_xs = [row[0] for row in scale_points]
            scale_ys = [row[1] for row in scale_points]
            floor['floor_scale'] = max(max(scale_xs) - min(scale_xs) if scale_xs else 0.0, max(scale_ys) - min(scale_ys) if scale_ys else 0.0, 1.0)
            floor['soft_labels'] = {'selection_frequency': {}, 'transition_frequency': []}
            floors[floor['floor_sample_id']] = floor
        return {'schema_version': 1, 'artifact_type': 'all_floor_rgcn_inference_candidates', 'floors': floors}

    def export_full_floor_recommendations(base_manifest_path: Path | str, candidates_path: Path | str, refined_graph_path: Path | str, rgcn_checkpoint_path: Path | str, route_head_checkpoint_path: Path | str, output_dir: Path | str, *, source_run_id: str, floor_ids: Sequence[str] | None=None, transition_top_k: int=20, device_name: str='auto', runtime_run_dir: Path | str | None=None) -> dict[str, Any]:
        output = Path(output_dir).resolve()
        output.mkdir(parents=True, exist_ok=True)
        device = torch.device('cuda' if device_name == 'auto' and torch.cuda.is_available() else device_name if device_name != 'auto' else 'cpu')
        base_manifest = _read(base_manifest_path)
        candidate_bundle = _read(candidates_path)
        floor_filter = {str(value) for value in floor_ids or [] if str(value)} or None
        expanded, requested_floors = _expanded_source_manifest(base_manifest, source_run_id, floor_filter, runtime_run_dir=runtime_run_dir, available_floor_ids=list((candidate_bundle.get('floors') or {}).keys()))
        manifest_path = _write(output / 'all_floor_inference_manifest.json', expanded)
        graph, build_audit = _build_inference_graph_from_existing_candidates(expanded, candidate_bundle)
        graph_path = output / 'all_floor_inference_graphs.pt'
        torch.save(graph, graph_path)
        _write(output / 'all_floor_inference_graph_audit.json', build_audit)
        rgcn_checkpoint = torch.load(Path(rgcn_checkpoint_path).resolve(), map_location='cpu', weights_only=False)
        aligned, alignment_audit = _align_to_checkpoint(graph, rgcn_checkpoint)
        embeddings, gate_statistics = _infer_embeddings(aligned, rgcn_checkpoint, device)
        aligned_graph_path = output / 'all_floor_aligned_graphs.pt'
        embeddings_path = output / 'all_floor_rgcn_embeddings.pt'
        torch.save(aligned, aligned_graph_path)
        torch.save({'node_embeddings': embeddings, 'floor_slices': aligned['floor_slices'], 'gate_statistics_by_layer': gate_statistics}, embeddings_path)
        augmented_refined_graph, virtual_access_audit = augment_virtual_target_access_nodes(_read(refined_graph_path), candidate_bundle)
        inference_payload = _route_inference_payload(candidate_bundle, augmented_refined_graph, source_run_id, floor_filter)
        inference_payload_path = _write(output / 'all_floor_route_head_input.json', inference_payload)
        recommendation_path = output / 'all_floor_route_recommendations.json'
        recommendations = export_route_recommendations(inference_payload_path, aligned_graph_path, embeddings_path, route_head_checkpoint_path, recommendation_path, transition_top_k=transition_top_k, device_name=str(device))
        candidate_counts = {key: len(row.get('candidates', []) or []) for key, row in inference_payload['floors'].items()}
        score_counts = {key: len((recommendations.get('floors', {}).get(key) or {}).get('selection_scores', {})) for key in candidate_counts}
        coverage = {key: {'candidate_count': candidate_counts[key], 'rgcn_scored_candidate_count': score_counts[key], 'complete': score_counts[key] == candidate_counts[key]} for key in candidate_counts}
        result = {'schema_version': 1, 'artifact_type': 'frozen_rgcn_all_floor_inference_summary', 'source_run_id': source_run_id, 'requested_floor_ids': requested_floors, 'device': str(device), 'training_checkpoint_frozen': True, 'rule_value_fallback_allowed': False, 'graph_build_audit': build_audit, 'virtual_access_audit': virtual_access_audit, 'alignment_audit': alignment_audit, 'coverage': coverage, 'outputs': {'expanded_manifest': str(manifest_path), 'aligned_graph': str(aligned_graph_path), 'embeddings': str(embeddings_path), 'route_head_input': str(inference_payload_path), 'recommendations': str(recommendation_path)}}
        summary_path = _write(output / 'full_floor_rgcn_inference_summary.json', result)
        return {**result, 'summary_path': str(summary_path)}
    return dict(locals())

_s08_full_export = _register_embedded_module(
    'export_full_floor_recommendations',
    _build_s08_full_export(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/dual_graph/runtime_pipeline.py
# -----------------------------------------------------------------------------
def _build_s08_runtime():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/dual_graph/runtime_pipeline.py'
    )
    __name__ = 'fire_inspection_system.dual_graph.runtime_pipeline'
    __package__ = 'fire_inspection_system.dual_graph'
    """Runtime integration for the same-floor heterogeneous/physical dual graph."""
    import json
    import math
    import re
    import sys
    import time
    from pathlib import Path
    from typing import Any, Mapping
    SYSTEM_DIR = Path(__file__).resolve().parents[1]
    if str(SYSTEM_DIR) not in sys.path:
        sys.path.insert(0, str(SYSTEM_DIR))
    from fire_inspection_system.dual_graph.physical_access import augment_virtual_target_access_nodes
    from fire_inspection_system.semantic.context_features import ContextFeatureConfig, compute_context_features, write_context_features
    from fire_inspection_system.semantic.physical_metric_closure import ClosureBuildConfig, PhysicalMetricClosure, build_physical_metric_closure
    from fire_inspection_system.semantic.pipeline import DEFAULT_EMPTY_GAP_RATIO, DEFAULT_HARD_GAP_RATIO, _decorate_candidates, _flatten_candidates, _insertion_counterfactuals, _route_with_values
    from fire_inspection_system.semantic.target_selector import build_target_candidates, load_constraints
    PROJECT_ROOT = Path(__file__).resolve().parents[2]
    DEFAULT_DATASET_MANIFEST = PROJECT_ROOT / 'datasets' / 'semantic_navigation_reachability80_v1' / 'dataset_manifest.json'
    DEFAULT_RGCN_CHECKPOINT = PROJECT_ROOT / 'outputs' / 'semantic_pretraining' / 'reachability80_v1_run1' / 'edge_gated_rgcn_pretrained.pt'
    DEFAULT_ROUTE_HEAD_CHECKPOINT = PROJECT_ROOT / 'outputs' / 'semantic_pseudo_labels' / 'reachability80_v1' / 'model' / 'pseudo_label_selection_transition_heads.pt'

    def _read_json(path: Path | str) -> Any:
        return json.loads(Path(path).resolve().read_text(encoding='utf-8'))

    def _write_json(path: Path | str, payload: Any) -> Path:
        output = Path(path).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        return output

    def _require_file(path: Path | str, description: str) -> Path:
        result = Path(path).resolve()
        if not result.is_file() or result.stat().st_size <= 0:
            raise FileNotFoundError(f'{description}不存在或为空: {result}')
        return result

    def _direct_navigation_rows(target_collection: Mapping[str, Any], physical_graph: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Adapt already-produced navigation targets without reopening CAD decisions.

        ``navigation_targets.geojson`` is the authoritative recognition/planning
        hand-off: it already contains the stable target ID, source CAD object ID,
        raw CAD class/name, learned standard class, floor and point.  The refined
        physical graph contributes only access/Area/component metadata.
        """
        physical_by_target = {str(node.get('target_id') or ''): dict(node) for node in physical_graph.get('nodes', []) or [] if isinstance(node, Mapping) and node.get('kind') == 'target' and node.get('target_id')}
        rows: list[dict[str, Any]] = []
        features: list[dict[str, Any]] = []
        for feature in target_collection.get('features', []) or []:
            if not isinstance(feature, Mapping):
                continue
            properties = dict(feature.get('properties') or {})
            geometry = dict(feature.get('geometry') or {})
            coordinates = geometry.get('coordinates') or []
            if geometry.get('type') != 'Point' or len(coordinates) < 2:
                continue
            target_id = str(properties.get('target_id') or '').strip()
            source_object_id = str(properties.get('source_object_id') or '').strip()
            floor_id = str(properties.get('floor_id') or '').strip()
            target_class = str(properties.get('target_class') or '').strip()
            raw_name = str(properties.get('source_class_name') or properties.get('original_object_name') or target_class).strip()
            if not target_id or not source_object_id or (not floor_id):
                continue
            raw_x, raw_y = (float(coordinates[0]), float(coordinates[1]))
            physical = physical_by_target.get(target_id, {})
            row = {**properties, 'target_id': target_id, 'object_id': source_object_id, 'source_object_id': source_object_id, 'floor_id': floor_id, 'class_name': target_class, 'target_class': target_class, 'standard_class_name': target_class, 'original_object_name': raw_name, 'term': raw_name, 'raw_name': raw_name, 'point': [raw_x, raw_y], 'raw_x': raw_x, 'raw_y': raw_y, 'x': float(physical.get('x', raw_x)), 'y': float(physical.get('y', raw_y))}
            for key in ('access_node_id', 'virtual_access', 'virtual_access_distance', 'area_id', 'component_id', 'assignment_status', 'projection_distance'):
                if physical.get(key) is not None:
                    row[key] = physical[key]
            rows.append(row)
            features.append({'type': 'Feature', 'properties': {key: value for key, value in row.items() if key not in {'point'}}, 'geometry': {'type': 'Point', 'coordinates': [raw_x, raw_y]}})
        return (rows, {'type': 'FeatureCollection', 'features': features})


    def _physical_distance_matrix(closure: PhysicalMetricClosure, candidates: Mapping[str, Any]) -> dict[str, Any]:
        floors: dict[str, Any] = {}
        for floor_id, floor in (candidates.get('floors') or {}).items():
            if floor_id not in closure.floor_ids():
                continue
            target_ids = [str(row.get('target_id') or '') for row in floor.get('candidates', []) or [] if row.get('target_id') and closure.has_target(floor_id, str(row.get('target_id')))]
            distances: dict[str, dict[str, float]] = {}
            for left in target_ids:
                row: dict[str, float] = {}
                for right in target_ids:
                    distance = closure.distance(floor_id, left, right)
                    if math.isfinite(distance):
                        row[right] = distance
                distances[left] = row
            floors[floor_id] = {'target_ids': target_ids, 'distances': distances, 'distance_semantics': 'same_floor_physical_navigation_graph_shortest_path'}
        return {'schema_version': 1, 'closure_type': 'runtime_context_physical_metric_matrix', 'floors': floors}

    def build_runtime_candidates_with_context(run_dir: Path | str, refined_graph_path: Path | str, output_dir: Path | str, *, constraints_path: Path | str | None=None, near_radius_factor: float=0.15) -> dict[str, Any]:
        """Reuse the research selector/context code with a physical distance matrix."""
        started = time.perf_counter()
        run = Path(run_dir).resolve()
        output = Path(output_dir).resolve()
        output.mkdir(parents=True, exist_ok=True)
        graph_path = _require_file(refined_graph_path, 'Portal修正物理导航图')
        area_path = _require_file(run / 'area_graph' / 'area_graph.json', 'AreaGraph')
        portal_path = _require_file(run / 'area_graph' / 'portal_candidates.geojson', 'Portal候选GeoJSON')
        target_path = _require_file(run / 'navigation_graph' / 'inputs' / 'navigation_targets.geojson', '巡检目标GeoJSON')
        constraints = load_constraints(Path(constraints_path).resolve() if constraints_path else None)
        physical_graph = _read_json(graph_path)
        area_graph = _read_json(area_path)
        target_collection = _read_json(target_path)
        navigation_rows, enriched_collection = _direct_navigation_rows(target_collection, physical_graph)
        annotations: list[dict[str, Any]] = []
        for row in navigation_rows:
            annotation = dict(row)
            annotation.setdefault('class_name', annotation.get('target_class'))
            annotation.setdefault('floor_name', annotation.get('floor_id'))
            annotations.append(annotation)
        bundle = build_target_candidates(annotations, constraints, navigation_targets=enriched_collection)
        bundle = _decorate_candidates(bundle, physical_graph, constraints)
        selected_floor_ids = sorted((str(floor_id) for floor_id in (bundle.get('floors') or {}).keys() if str(floor_id)))
        augmented_graph, virtual_access = augment_virtual_target_access_nodes(physical_graph, bundle)
        bundle = _decorate_candidates(bundle, augmented_graph, constraints)
        candidate_rows = _flatten_candidates(bundle)
        if not candidate_rows:
            raise RuntimeError(f'识别到的楼层没有生成巡检候选目标: {selected_floor_ids}')
        augmented_path = _write_json(output / 'context_augmented_physical_graph.json', augmented_graph)
        closure_result = build_physical_metric_closure(augmented_path, output / 'physical_context_metric_closure', floors=selected_floor_ids, config=ClosureBuildConfig(include_euclidean_baseline=False))
        closure = PhysicalMetricClosure(closure_result['manifest_path'])
        distance_matrix = _physical_distance_matrix(closure, bundle)
        areas = [dict(row) for row in area_graph.get('nodes', []) or [] if isinstance(row, Mapping) and str(row.get('floor_id') or '') in set(selected_floor_ids)]
        initial_context = compute_context_features(candidate_rows, areas, distance_matrix=distance_matrix, config=ContextFeatureConfig(near_radius_factor=near_radius_factor))
        floor_scales = {str(key): max(float(value), 1.0) for key, value in (initial_context.get('floor_scales') or {}).items()}
        baseline = _route_with_values(bundle, distance_matrix, {'targets': [{'target_id': row.get('target_id'), 'u_rule': 0.0} for row in candidate_rows]}, delta=0.0, empty_gap_by_floor={floor_id: scale * DEFAULT_EMPTY_GAP_RATIO for floor_id, scale in floor_scales.items()}, hard_gap_by_floor={floor_id: scale * DEFAULT_HARD_GAP_RATIO for floor_id, scale in floor_scales.items()})
        counterfactuals = _insertion_counterfactuals(candidate_rows, baseline.get('floors', {}) or {}, distance_matrix, floor_scales)
        for row in counterfactuals.values():
            row['source'] = 'physical_metric_insertion_coverage_proxy'
            row['approximate'] = True
        context = compute_context_features(candidate_rows, areas, distance_matrix=distance_matrix, counterfactuals=counterfactuals, config=ContextFeatureConfig(near_radius_factor=near_radius_factor))
        context_by_target = {str(row.get('target_id') or ''): row for row in context.get('targets', []) or [] if row.get('target_id')}
        context_fields = {'A', 'A_raw', 'A_source', 'N', 'N_raw', 'N_first_order_raw', 'N_second_order_raw', 'N_source', 'C', 'C_raw', 'C_source', 'u_rule', 'rule_need_weight', 'rule_need_source', 'area_constraint_load', 'area_rule_group_count'}
        for floor in (bundle.get('floors') or {}).values():
            for candidate in floor.get('candidates', []) or []:
                values = context_by_target.get(str(candidate.get('target_id') or ''), {})
                candidate.update({key: values[key] for key in context_fields if key in values})
                candidate['context_distance_source'] = 'physical_metric_closure'
                candidate['raster_or_grid_routing_used'] = False
        candidates_path = _write_json(output / 'target_candidates_with_context.json', bundle)
        context_path = write_context_features(output / 'target_context_features.json', context)
        baseline_path = _write_json(output / 'physical_context_baseline.json', baseline)
        summary = {'schema_version': 1, 'stage': 'runtime_candidates_and_three_factor_context', 'selected_floor_ids': selected_floor_ids, 'candidate_count': sum((len(floor.get('candidates', []) or []) for floor in (bundle.get('floors') or {}).values())), 'context_scored_count': len(context_by_target), 'input_reuse': {'navigation_targets': str(target_path), 'area_graph': str(area_path), 'portal_candidates': str(portal_path), 'refined_physical_navigation_graph': str(graph_path), 'cad_or_recognition_detail_reparsed': False, 'source_dxf_reparsed': False}, 'three_context_factors': {'A': '区域约束负载、规则多样性和Portal上下文', 'N': '同层近邻目标的一阶/二阶风险与规则需求传播', 'C': '物理Metric Closure上的插入覆盖贡献代理'}, 'physical_metric_closure': closure_result, 'virtual_access': virtual_access, 'raster_or_grid_routing_used': False, 'elapsed_seconds': time.perf_counter() - started, 'outputs': {'candidates': str(candidates_path), 'context': str(context_path), 'baseline': str(baseline_path), 'augmented_physical_graph': str(augmented_path), 'physical_metric_closure_manifest': closure_result['manifest_path']}}
        summary['baseline_solver_policy'] = {
            **dict(baseline.get('solver_policy') or {}),
            'heuristic_floor_count': sum(
                row.get('solver') == 'heuristic_greedy_2opt'
                for row in (baseline.get('floors') or {}).values()
            ),
            'exact_work_budget_fallback_floor_count': sum(
                row.get('solver_selection_reason') == 'exact_work_budget_exceeded'
                for row in (baseline.get('floors') or {}).values()
            ),
        }
        summary_path = _write_json(output / 'candidate_context_summary.json', summary)
        return {**summary, 'summary_path': str(summary_path)}


    __all__ = ['DEFAULT_DATASET_MANIFEST', 'DEFAULT_RGCN_CHECKPOINT', 'DEFAULT_ROUTE_HEAD_CHECKPOINT', 'build_runtime_candidates_with_context']
    return dict(locals())

_s08_runtime = _register_embedded_module(
    'fire_inspection_system.dual_graph.runtime_pipeline',
    _build_s08_runtime(),
    aliases=(),
)

# === CONSOLIDATED PUBLIC API ===
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_DATASET_MANIFEST = _s08_runtime.DEFAULT_DATASET_MANIFEST
DEFAULT_RGCN_CHECKPOINT = _s08_runtime.DEFAULT_RGCN_CHECKPOINT
DEFAULT_ROUTE_HEAD_CHECKPOINT = _s08_runtime.DEFAULT_ROUTE_HEAD_CHECKPOINT


@dataclass(frozen=True)
class SemanticRgcnResult:
    candidate_summary: dict[str, Any]
    rgcn: dict[str, Any]
    selected_floor_ids: tuple[str, ...]
    candidates_path: Path
    recommendations_path: Path
    source_run_id: str
    elapsed_seconds: float

    def to_summary(self) -> dict[str, Any]:
        return {
            "selected_floor_ids": list(self.selected_floor_ids),
            "source_run_id": self.source_run_id,
            "candidate_context": self.candidate_summary,
            "rgcn": self.rgcn,
            "elapsed_seconds": self.elapsed_seconds,
        }


def _require(path: Path | str, description: str) -> Path:
    result = Path(path).expanduser().resolve()
    if not result.is_file() or result.stat().st_size <= 0:
        raise FileNotFoundError(f"{description}不存在或为空: {result}")
    return result


def _load_exporter() -> Any:
    return _s08_full_export.export_full_floor_recommendations


def run_stage(
    run_dir: Path,
    physical: Any,
    *,
    dataset_manifest_path: Path | str,
    rgcn_checkpoint_path: Path | str,
    route_head_checkpoint_path: Path | str,
    constraints_path: Path | str | None,
    source_run_id: str,
    transition_top_k: int,
    device_name: str,
) -> SemanticRgcnResult:
    started = time.perf_counter()
    if not physical.enabled or physical.refined_graph is None:
        raise ValueError("A/N/C 与 R-GCN 阶段缺少 Connector 修正物理图")
    manifest = _require(dataset_manifest_path, "R-GCN 训练数据 manifest")
    rgcn_checkpoint = _require(rgcn_checkpoint_path, "冻结 R-GCN 权重")
    route_head_checkpoint = _require(route_head_checkpoint_path, "路线头权重")
    run = run_dir.resolve()
    output = run / "path_planning"

    candidate_summary = _s08_runtime.build_runtime_candidates_with_context(
        run,
        physical.refined_graph,
        output / "semantic_value_inputs",
        constraints_path=constraints_path,
    )
    selected_floor_ids = tuple(
        str(value) for value in candidate_summary["selected_floor_ids"]
    )
    if not selected_floor_ids:
        raise RuntimeError("未识别到可供路径规划的楼层")
    candidates_path = _require(
        candidate_summary["outputs"]["candidates"],
        "A/N/C 候选目标文件",
    )
    runtime_source_id = source_run_id.strip() or re.sub(
        r"[^0-9A-Za-z_\-.]+", "_", run.name
    ).strip("_")
    exporter = _load_exporter()
    rgcn = exporter(
        manifest,
        candidates_path,
        physical.refined_graph,
        rgcn_checkpoint,
        route_head_checkpoint,
        output / "rgcn_inference",
        source_run_id=runtime_source_id,
        floor_ids=list(selected_floor_ids),
        transition_top_k=transition_top_k,
        device_name=device_name,
        runtime_run_dir=run,
    )
    incomplete = [
        key
        for key, row in (rgcn.get("coverage") or {}).items()
        if not row.get("complete")
    ]
    if incomplete:
        raise RuntimeError(f"R-GCN 未覆盖全部候选目标: {incomplete}")
    recommendations = _require(
        rgcn["outputs"]["recommendations"],
        "R-GCN 全楼层推荐结果",
    )
    return SemanticRgcnResult(
        candidate_summary=candidate_summary,
        rgcn=rgcn,
        selected_floor_ids=selected_floor_ids,
        candidates_path=candidates_path,
        recommendations_path=recommendations,
        source_run_id=runtime_source_id,
        elapsed_seconds=time.perf_counter() - started,
    )


__all__ = ["SemanticRgcnResult", "run_stage"]
