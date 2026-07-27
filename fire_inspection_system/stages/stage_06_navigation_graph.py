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
from fire_inspection_system.stages import stage_04_inspection_objects as _stage04
from fire_inspection_system.stages import stage_05_obstacles as _stage05

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/navigation_input_adapter.py
# -----------------------------------------------------------------------------
def _build_s06_inputs():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/navigation_input_adapter.py'
    )
    __name__ = 'fire_inspection_system.navigation_input_adapter'
    __package__ = 'fire_inspection_system'
    import hashlib
    import json
    import math
    from collections import defaultdict
    from dataclasses import dataclass
    from pathlib import Path
    from typing import Any, Iterable, Iterator
    from shapely.geometry import GeometryCollection, Point, Polygon, mapping, shape
    from shapely.ops import unary_union
    from inspection_object_recognition import collect_region_annotations, dedupe_annotations
    from obstacle_recognition_door_mask_fixed import load_floor_regions

    @dataclass(frozen=True)
    class NavigationInputResult:
        free_area_geojson: Path
        obstacle_union_geojson: Path
        targets_geojson: Path
        manifest_json: Path
        floor_count: int
        obstacle_union_count: int
        source_obstacle_feature_count: int
        declared_obstacle_count: int
        source_target_count: int
        deduplicated_source_target_count: int
        dedupe_removed_target_count: int
        target_count: int
        skipped_target_count: int

        def to_dict(self) -> dict[str, Any]:
            return {'free_area_geojson': str(self.free_area_geojson), 'obstacle_union_geojson': str(self.obstacle_union_geojson), 'targets_geojson': str(self.targets_geojson), 'manifest_json': str(self.manifest_json), 'floor_count': self.floor_count, 'obstacle_union_count': self.obstacle_union_count, 'source_obstacle_feature_count': self.source_obstacle_feature_count, 'declared_obstacle_count': self.declared_obstacle_count, 'source_target_count': self.source_target_count, 'deduplicated_source_target_count': self.deduplicated_source_target_count, 'dedupe_removed_target_count': self.dedupe_removed_target_count, 'target_count': self.target_count, 'skipped_target_count': self.skipped_target_count}

    def _read_json(path: Path) -> dict[str, Any]:
        with path.open('r', encoding='utf-8') as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError(f'JSON root must be an object: {path}')
        return payload

    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def _fingerprint(path: Path) -> dict[str, Any]:
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
        stat = path.stat()
        return {'path': str(path.resolve()), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'sha256': digest.hexdigest()}

    def _polygon_parts(geometry: Any) -> Iterator[Polygon]:
        if geometry is None or geometry.is_empty:
            return
        if geometry.geom_type == 'Polygon':
            yield geometry
            return
        for part in getattr(geometry, 'geoms', ()):
            yield from _polygon_parts(part)

    def _strict_polygonal(geometry: Any, context: str, *, allow_empty: bool=False) -> Any:
        """Return repaired polygonal geometry without turning failures into free space."""
        if geometry is None:
            raise ValueError(f'Polygonal geometry is missing: {context}')
        if geometry.is_empty:
            if allow_empty:
                return GeometryCollection()
            raise ValueError(f'Polygonal geometry is empty: {context}')
        candidate = geometry
        if not candidate.is_valid:
            try:
                candidate = candidate.buffer(0)
            except Exception as exc:
                raise ValueError(f'Could not repair polygonal geometry: {context}') from exc
        if candidate.is_empty:
            if allow_empty:
                return GeometryCollection()
            raise ValueError(f'Polygonal geometry became empty after repair: {context}')
        if not candidate.is_valid:
            raise ValueError(f'Polygonal geometry remains invalid after repair: {context}')
        parts = [part for part in _polygon_parts(candidate) if not part.is_empty and part.area > 0]
        if not parts:
            raise ValueError(f'Geometry has no positive-area polygon part: {context}')
        try:
            merged = unary_union(parts)
        except Exception as exc:
            raise ValueError(f'Could not union polygonal geometry: {context}') from exc
        if not merged.is_valid:
            try:
                merged = merged.buffer(0)
            except Exception as exc:
                raise ValueError(f'Could not repair polygonal union: {context}') from exc
        if merged.is_empty or not merged.is_valid:
            raise ValueError(f'Polygonal union is empty or invalid after repair: {context}')
        return merged

    def _source_polygonal(geometry: Any, context: str) -> Any:
        """Repair a source obstacle, but never silently turn a bad wall into free space."""
        try:
            return _strict_polygonal(geometry, context)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f'Could not normalize obstacle geometry: {context}') from exc

    def _floor_domains(sheets_json: Path) -> tuple[dict[str, Any], dict[str, str]]:
        grouped: dict[str, list[Any]] = defaultdict(list)
        names: dict[str, str] = {}
        for region in load_floor_regions(sheets_json):
            floor_id = str(region.get('floor_id') or '').strip()
            if not floor_id:
                raise ValueError(f"Usable inspection region has no floor_id: {region.get('full_region_id', '')}")
            grouped[floor_id].append(region['polygon'])
            names.setdefault(floor_id, str(region.get('floor_name') or floor_id))
        domains: dict[str, Any] = {}
        for floor_id, rows in grouped.items():
            try:
                merged = unary_union(rows)
            except Exception as exc:
                raise RuntimeError(f'Failed to union usable regions for floor {floor_id}') from exc
            domains[floor_id] = _strict_polygonal(merged, f'usable floor domain {floor_id}')
        if not domains:
            raise RuntimeError(f'No usable floor domain was loaded from {sheets_json}')
        return (domains, names)

    def _load_obstacle_unions(paths: list[Path], floor_domains: dict[str, Any]) -> tuple[dict[str, Any], int, int, bool, set[str], dict[str, dict[str, float]]]:
        grouped: dict[str, list[Any]] = defaultdict(list)
        feature_count = 0
        declared_obstacle_count = 0
        declared_counts_complete = True
        covered_floors: set[str] = set()
        explicit_empty_floors: set[str] = set()
        for path in paths:
            payload = _read_json(path)
            features = payload.get('features')
            if not isinstance(features, list):
                raise ValueError(f'Obstacle GeoJSON features must be a list: {path}')
            for index, feature in enumerate(features, start=1):
                if not isinstance(feature, dict):
                    raise ValueError(f'Obstacle feature must be an object: {path} feature {index}')
                properties = feature.get('properties') or {}
                floor_id = str(properties.get('floor_id') or '').strip()
                if not floor_id:
                    raise ValueError(f'Obstacle feature has no floor_id: {path} feature {index}')
                if floor_id not in floor_domains:
                    raise ValueError(f'Obstacle floor_id {floor_id!r} is absent from usable floor regions: {path}')
                raw_declared_count = properties.get('obstacle_count')
                if raw_declared_count is None:
                    declared_counts_complete = False
                    feature_obstacle_count = None
                else:
                    try:
                        feature_obstacle_count = int(raw_declared_count)
                    except Exception as exc:
                        raise ValueError(f'Obstacle feature has invalid obstacle_count: {path} feature {index}') from exc
                    if feature_obstacle_count < 0:
                        raise ValueError(f'Obstacle feature has negative obstacle_count: {path} feature {index}')
                    declared_obstacle_count += feature_obstacle_count
                covered_floors.add(floor_id)
                if not feature.get('geometry'):
                    if feature_obstacle_count is None:
                        raise ValueError(f'Obstacle feature has no geometry or explicit obstacle_count=0: {path} feature {index}')
                    if feature_obstacle_count != 0:
                        raise ValueError(f'Obstacle feature has no geometry but declares obstacles: {path} feature {index}')
                    if grouped.get(floor_id):
                        raise ValueError(f'Floor {floor_id} has both non-empty obstacle geometry and obstacle_count=0')
                    explicit_empty_floors.add(floor_id)
                    feature_count += 1
                    continue
                if floor_id in explicit_empty_floors:
                    raise ValueError(f'Floor {floor_id} has both obstacle_count=0 and non-empty obstacle geometry')
                try:
                    geometry = _source_polygonal(shape(feature['geometry']), f'{path} feature {index}')
                except Exception as exc:
                    raise ValueError(f'Invalid obstacle geometry: {path} feature {index}') from exc
                grouped[floor_id].append(geometry)
                feature_count += 1
        result: dict[str, Any] = {}
        clipping_stats: dict[str, dict[str, float]] = {}
        for floor_id, geometries in grouped.items():
            try:
                merged_raw = unary_union(geometries)
            except Exception as exc:
                raise RuntimeError(f'Failed to merge obstacle union for floor {floor_id}') from exc
            merged = _source_polygonal(merged_raw, f'merged obstacle union for floor {floor_id}')
            try:
                clipped_raw = merged.intersection(floor_domains[floor_id])
            except Exception as exc:
                raise RuntimeError(f'Failed to clip obstacle union for floor {floor_id}') from exc
            if clipped_raw.is_empty:
                raise RuntimeError(f'Obstacle union for floor {floor_id} is completely outside its usable floor domain')
            clipped = _source_polygonal(clipped_raw, f'clipped obstacle union for floor {floor_id}')
            result[floor_id] = clipped
            clipping_stats[floor_id] = {'source_area': float(merged.area), 'clipped_area': float(clipped.area), 'outside_domain_area': float(max(0.0, merged.area - clipped.area))}
        return (result, feature_count, declared_obstacle_count, declared_counts_complete, covered_floors, clipping_stats)

    def _valid_bbox(value: Any) -> tuple[float, float, float, float] | None:
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            return None
        try:
            minx, miny, maxx, maxy = (float(item) for item in value)
        except Exception:
            return None
        if not all((math.isfinite(item) for item in (minx, miny, maxx, maxy))):
            return None
        if maxx <= minx or maxy <= miny:
            return None
        return (minx, miny, maxx, maxy)

    def _target_features(inspection_output_dir: Path, floor_domains: dict[str, Any]) -> tuple[list[dict[str, Any]], int, int, int]:
        result_json = inspection_output_dir / 'region_inspection_results.json'
        if not result_json.exists():
            raise FileNotFoundError(result_json)
        source_annotations, _summary = collect_region_annotations(inspection_output_dir)
        source_count = len(source_annotations)
        invalid_source_count = sum((1 for annotation in source_annotations if str(annotation.get('floor_id') or '').strip() not in floor_domains or _valid_bbox(annotation.get('bbox')) is None))
        if invalid_source_count:
            raise RuntimeError(f'Collected {invalid_source_count} inspection targets with invalid floor_id or bbox')
        annotations = dedupe_annotations(source_annotations)
        deduplicated_count = len(annotations)
        features: list[dict[str, Any]] = []
        skipped = 0
        for annotation in annotations:
            floor_id = str(annotation.get('floor_id') or '').strip()
            bbox = _valid_bbox(annotation.get('bbox'))
            if floor_id not in floor_domains or bbox is None:
                skipped += 1
                continue
            point = Point((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)
            target_index = len(features) + 1
            target_class = str(annotation.get('standard_class_name') or annotation.get('class_name') or annotation.get('original_object_name') or '巡检对象')
            features.append({'type': 'Feature', 'properties': {'target_id': f'{floor_id}_TARGET_{target_index:07d}', 'target_class': target_class, 'floor_id': floor_id, 'floor_name': str(annotation.get('floor_name') or floor_id), 'sheet_id': str(annotation.get('sheet_id') or ''), 'source_object_id': str(annotation.get('object_id') or ''), 'source_class_name': str(annotation.get('original_object_name') or annotation.get('term') or target_class), 'confidence': float(annotation.get('confidence') or 0.0), 'source': 'inspection_object_recognition'}, 'geometry': mapping(point)})
        return (features, skipped, source_count, deduplicated_count)

    def _feature_collection(features: Iterable[dict[str, Any]]) -> dict[str, Any]:
        return {'type': 'FeatureCollection', 'features': list(features)}

    def _geometry_stats(geometry: Any) -> dict[str, Any]:
        polygons = list(_polygon_parts(geometry))
        return {'geometry_type': geometry.geom_type, 'area': float(geometry.area), 'component_count': len(polygons), 'hole_count': sum((len(polygon.interiors) for polygon in polygons))}

    def prepare_navigation_inputs(sheets_json: Path | str, obstacle_union_geojsons: Iterable[Path | str], inspection_output_dir: Path | str, output_dir: Path | str, write_debug_dxf: bool=False, *, expected_obstacle_count: int | None=None, expected_target_count: int | None=None) -> NavigationInputResult:
        """Build the fixed GeoJSON contract consumed by both AreaGraph stages."""
        sheets_path = Path(sheets_json).resolve()
        inspection_dir = Path(inspection_output_dir).resolve()
        out_dir = Path(output_dir).resolve()
        obstacle_paths = [Path(path).resolve() for path in obstacle_union_geojsons]
        if not sheets_path.exists():
            raise FileNotFoundError(sheets_path)
        if expected_obstacle_count is not None and expected_obstacle_count < 0:
            raise ValueError('expected_obstacle_count must be non-negative')
        if expected_target_count is not None and expected_target_count < 0:
            raise ValueError('expected_target_count must be non-negative')
        if not obstacle_paths and expected_obstacle_count != 0:
            raise RuntimeError('Obstacle recognition did not provide any per-floor union GeoJSON; navigation construction is refusing to assume an obstacle-free building')
        for path in obstacle_paths:
            if not path.exists():
                raise FileNotFoundError(path)
        floor_domains, floor_names = _floor_domains(sheets_path)
        obstacles_by_floor, source_obstacle_feature_count, declared_obstacle_count, declared_counts_complete, covered_obstacle_floors, clipping_stats = _load_obstacle_unions(obstacle_paths, floor_domains)
        if expected_obstacle_count is not None:
            if not declared_counts_complete:
                raise RuntimeError('Obstacle union GeoJSON is missing obstacle_count provenance required for reconciliation')
            if declared_obstacle_count != expected_obstacle_count:
                raise RuntimeError(f'Obstacle recognition reported {expected_obstacle_count} obstacles, but per-floor union files declare {declared_obstacle_count}; refusing stale or mismatched union data')
        if expected_obstacle_count and (not obstacles_by_floor):
            raise RuntimeError(f'Obstacle recognition reported {expected_obstacle_count} obstacles, but the union GeoJSON contains no non-empty floor geometry')
        if expected_obstacle_count == 0 and obstacles_by_floor:
            raise RuntimeError('Obstacle recognition reported zero obstacles, but non-empty union geometry was supplied')
        missing_obstacle_floors = sorted(set(floor_domains) - covered_obstacle_floors)
        if missing_obstacle_floors and expected_obstacle_count is None:
            raise RuntimeError('Per-floor obstacle union coverage is incomplete for usable floors: ' + ', '.join(missing_obstacle_floors) + '. Supply expected_obstacle_count so absent floors can only be inferred empty after exact obstacle-count reconciliation.')
        obstacle_features: list[dict[str, Any]] = []
        free_features: list[dict[str, Any]] = []
        floor_stats: dict[str, Any] = {}
        for floor_id, domain in floor_domains.items():
            obstacle = obstacles_by_floor.get(floor_id, GeometryCollection())
            if not obstacle.is_empty:
                obstacle_features.append({'type': 'Feature', 'properties': {'floor_id': floor_id, 'floor_name': floor_names[floor_id], 'kind': 'obstacle_union'}, 'geometry': mapping(obstacle)})
            try:
                free_area_raw = domain.difference(obstacle)
            except Exception as exc:
                raise RuntimeError(f'Failed to subtract obstacles from floor {floor_id}') from exc
            free_area = _strict_polygonal(free_area_raw, f'free area for floor {floor_id}', allow_empty=True)
            if free_area.is_empty:
                floor_stats[floor_id] = {'floor_name': floor_names[floor_id], 'domain': _geometry_stats(domain), 'obstacle': _geometry_stats(obstacle), 'free_area': _geometry_stats(free_area), 'obstacle_clipping': clipping_stats.get(floor_id, {}), 'obstacle_union_provenance': 'source_union_feature' if floor_id in covered_obstacle_floors else 'inferred_zero_after_exact_count_reconciliation', 'status': 'fully_blocked'}
                continue
            free_features.append({'type': 'Feature', 'properties': {'floor_id': floor_id, 'floor_name': floor_names[floor_id], 'kind': 'free_area'}, 'geometry': mapping(free_area)})
            floor_stats[floor_id] = {'floor_name': floor_names[floor_id], 'domain': _geometry_stats(domain), 'obstacle': _geometry_stats(obstacle), 'free_area': _geometry_stats(free_area), 'obstacle_free_overlap_area': float(obstacle.intersection(free_area).area), 'obstacle_clipping': clipping_stats.get(floor_id, {}), 'obstacle_union_provenance': 'source_union_feature' if floor_id in covered_obstacle_floors else 'inferred_zero_after_exact_count_reconciliation', 'status': 'navigable'}
        if not free_features:
            raise RuntimeError('All usable floors are fully blocked after obstacle subtraction')
        targets, skipped_target_count, source_target_count, deduplicated_source_target_count = _target_features(inspection_dir, floor_domains)
        if skipped_target_count:
            raise RuntimeError(f'Skipped {skipped_target_count} inspection targets because floor_id or bbox was invalid')
        if expected_target_count is not None and source_target_count != expected_target_count:
            raise RuntimeError(f'Inspection recognition reported {expected_target_count} targets, but navigation collection found {source_target_count} before deduplication; refusing incomplete or stale inspection data')
        dedupe_removed_target_count = source_target_count - deduplicated_source_target_count
        out_dir.mkdir(parents=True, exist_ok=True)
        obstacle_path = out_dir / 'obstacle_unions.geojson'
        free_path = out_dir / 'free_areas.geojson'
        target_path = out_dir / 'navigation_targets.geojson'
        manifest_path = out_dir / 'navigation_inputs_manifest.json'
        _write_json(obstacle_path, _feature_collection(obstacle_features))
        _write_json(free_path, _feature_collection(free_features))
        _write_json(target_path, _feature_collection(targets))
        inspection_sources = [inspection_dir / 'region_inspection_results.json']
        inspection_sources.extend(sorted(inspection_dir.glob('*/inspection_objects.json')))
        inspection_sources.extend(sorted(inspection_dir.glob('*/cad_semantic_inventory.csv')))
        manifest = {'schema_version': 1, 'pipeline': 'fire_inspection_navigation_input_adapter', 'write_debug_dxf_requested': bool(write_debug_dxf), 'sources': {'sheets_json': _fingerprint(sheets_path), 'obstacle_union_geojsons': [_fingerprint(path) for path in obstacle_paths], 'inspection_outputs': [_fingerprint(path) for path in inspection_sources if path.exists()]}, 'outputs': {'obstacle_unions': _fingerprint(obstacle_path), 'free_areas': _fingerprint(free_path), 'navigation_targets': _fingerprint(target_path)}, 'floor_count': len(free_features), 'obstacle_union_count': len(obstacle_features), 'source_obstacle_feature_count': source_obstacle_feature_count, 'declared_obstacle_count': declared_obstacle_count, 'expected_obstacle_count': expected_obstacle_count, 'inferred_zero_obstacle_floors': missing_obstacle_floors, 'source_target_count': source_target_count, 'deduplicated_source_target_count': deduplicated_source_target_count, 'dedupe_removed_target_count': dedupe_removed_target_count, 'target_count': len(targets), 'skipped_target_count': skipped_target_count, 'expected_target_count': expected_target_count, 'floors': floor_stats}
        _write_json(manifest_path, manifest)
        return NavigationInputResult(free_area_geojson=free_path, obstacle_union_geojson=obstacle_path, targets_geojson=target_path, manifest_json=manifest_path, floor_count=len(free_features), obstacle_union_count=len(obstacle_features), source_obstacle_feature_count=source_obstacle_feature_count, declared_obstacle_count=declared_obstacle_count, source_target_count=source_target_count, deduplicated_source_target_count=deduplicated_source_target_count, dedupe_removed_target_count=dedupe_removed_target_count, target_count=len(targets), skipped_target_count=skipped_target_count)
    return dict(locals())

_s06_inputs = _register_embedded_module(
    'fire_inspection_system.navigation_input_adapter',
    _build_s06_inputs(),
    aliases=('navigation_input_adapter',),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/portal_area_graph.py
# -----------------------------------------------------------------------------
def _build_s06_area_graph():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/portal_area_graph.py'
    )
    __name__ = 'fire_inspection_system.portal_area_graph'
    __package__ = 'fire_inspection_system'
    """Generate wall-gap Portal candidates and an AreaGraph from CAD free space.

    This module is intentionally independent from the previous navigation-graph
    pipeline.  It treats every detected wall gap as a reviewable Portal candidate,
    but only candidates that locally separate free space are used to split AreaGraph
    regions.  The output includes structured JSON/GeoJSON/CSV and a marked DXF.

    Typical usage after the upstream pipeline::

        python portal_area_graph.py --run-dir D:\\untitled5\\outputs\\...\\RUN

    Or with explicit inputs::

        python portal_area_graph.py       --free-area free_areas.geojson       --obstacles obstacle_unions.geojson       --source-dxf drawing.dxf       --output-dir area_graph
    """
    import argparse
    import csv
    import json
    import math
    import time
    from collections import Counter, defaultdict
    from dataclasses import dataclass, field
    from pathlib import Path
    from typing import Any, Iterable, Iterator, Sequence
    import numpy as np
    from scipy.ndimage import convolve, label, minimum_filter
    from shapely.geometry import GeometryCollection, LineString, Point, Polygon, mapping, shape
    from shapely.ops import unary_union
    from shapely.prepared import prep
    from skimage.draw import line as draw_line
    from skimage.draw import polygon as draw_polygon
    from skimage.morphology import medial_axis
    try:
        import ezdxf
    except ImportError:
        ezdxf = None

    @dataclass
    class BuildConfig:
        pixel_size: float = 0.0
        max_raster_side: int = 1600
        max_raster_pixels: int = 1500000
        local_minimum_window: int = 11
        local_context_radius: int = 12
        minimum_bottleneck_score: float = 0.06
        minimum_portal_width_pixels: float = 2.0
        maximum_portal_width_pixels: float = 24.0
        ray_angle_count: int = 24
        max_candidates_per_floor: int = 600
        separator_cut_width_pixels: float = 0.45
        obstacle_contact_tolerance_pixels: float = 2.5
        minimum_component_pixels: float = 4.0
        minimum_region_pixels: float = 4.0

    @dataclass
    class RasterFrame:
        floor_id: str
        component_id: str
        polygon: Polygon
        pixel_size: float
        minx: float
        miny: float
        maxx: float
        maxy: float
        mask: np.ndarray
        skeleton: np.ndarray
        distance: np.ndarray

        def rc_to_xy(self, row: float, col: float) -> tuple[float, float]:
            return (self.minx + (float(col) + 0.5) * self.pixel_size, self.maxy - (float(row) + 0.5) * self.pixel_size)

        def xy_to_rc(self, x: float, y: float) -> tuple[float, float]:
            return ((self.maxy - float(y)) / self.pixel_size - 0.5, (float(x) - self.minx) / self.pixel_size - 0.5)

    @dataclass
    class PortalCandidate:
        portal_id: str
        floor_id: str
        component_id: str
        center: Point
        geometry: LineString
        width: float
        clearance: float
        bottleneck_score: float
        contact_count: int
        separator: bool
        confidence: float
        status: str
        reason: str
        area_a: str = ''
        area_b: str = ''
        properties: dict[str, Any] = field(default_factory=dict)

        def feature(self) -> dict[str, Any]:
            properties = {'portal_id': self.portal_id, 'floor_id': self.floor_id, 'component_id': self.component_id, 'width': self.width, 'clearance': self.clearance, 'bottleneck_score': self.bottleneck_score, 'obstacle_contact_count': self.contact_count, 'local_separator': int(self.separator), 'confidence': self.confidence, 'status': self.status, 'reason': self.reason, 'area_a': self.area_a, 'area_b': self.area_b, 'source': 'medial_axis_wall_gap', **self.properties}
            return {'type': 'Feature', 'properties': properties, 'geometry': mapping(self.geometry)}

    @dataclass
    class AreaRegion:
        area_id: str
        floor_id: str
        component_id: str
        geometry: Polygon
        source_component_area: float
        portal_ids: list[str] = field(default_factory=list)

        def feature(self) -> dict[str, Any]:
            center = self.geometry.representative_point()
            return {'type': 'Feature', 'properties': {'area_id': self.area_id, 'floor_id': self.floor_id, 'component_id': self.component_id, 'kind': 'area_graph_region', 'area': float(self.geometry.area), 'centroid_x': float(center.x), 'centroid_y': float(center.y), 'portal_count': len(self.portal_ids), 'portal_ids': ','.join(self.portal_ids), 'source_component_area': float(self.source_component_area)}, 'geometry': mapping(self.geometry)}

    def _read_json(path: Path) -> Any:
        with Path(path).open('r', encoding='utf-8') as handle:
            return json.load(handle)

    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def _safe_geometry(geometry: Any) -> Any:
        if geometry is None:
            return GeometryCollection()
        if geometry.is_empty:
            return geometry
        if geometry.is_valid:
            return geometry
        try:
            repaired = geometry.buffer(0)
            return repaired if repaired.is_valid else GeometryCollection()
        except Exception:
            return GeometryCollection()

    def _iter_polygons(geometry: Any) -> Iterator[Polygon]:
        geometry = _safe_geometry(geometry)
        if geometry.is_empty:
            return
        if geometry.geom_type == 'Polygon':
            yield geometry
        elif geometry.geom_type in {'MultiPolygon', 'GeometryCollection'}:
            for part in geometry.geoms:
                yield from _iter_polygons(part)

    def _iter_lines(geometry: Any) -> Iterator[LineString]:
        if geometry is None or geometry.is_empty:
            return
        if geometry.geom_type in {'LineString', 'LinearRing'}:
            line = LineString(geometry.coords)
            if line.length > 0:
                yield line
        elif geometry.geom_type in {'MultiLineString', 'GeometryCollection'}:
            for part in geometry.geoms:
                yield from _iter_lines(part)

    def _load_floor_geometries(path: Path) -> dict[str, Any]:
        payload = _read_json(path)
        grouped: dict[str, list[Any]] = defaultdict(list)
        for index, feature in enumerate(payload.get('features', []) or [], start=1):
            geometry_payload = feature.get('geometry')
            if not geometry_payload:
                continue
            properties = feature.get('properties') or {}
            floor_id = str(properties.get('floor_id') or f'FLOOR_{index:03d}')
            geometry = _safe_geometry(shape(geometry_payload))
            if not geometry.is_empty:
                grouped[floor_id].append(geometry)
        return {floor_id: _safe_geometry(unary_union(rows)) for floor_id, rows in grouped.items()}

    def _adaptive_pixel_size(geometry: Any, config: BuildConfig) -> float:
        if config.pixel_size > 0:
            return float(config.pixel_size)
        minx, miny, maxx, maxy = geometry.bounds
        width = max(maxx - minx, 1.0)
        height = max(maxy - miny, 1.0)
        by_side = max(width, height) / max(config.max_raster_side, 256)
        by_pixels = math.sqrt(max(geometry.area, 1.0) / max(config.max_raster_pixels, 65536))
        return float(max(by_side, by_pixels, 1e-06))

    def _rasterize_polygon(floor_id: str, component_id: str, polygon: Polygon, pixel_size: float) -> RasterFrame:
        minx, miny, maxx, maxy = polygon.bounds
        padding = pixel_size * 2.0
        minx -= padding
        miny -= padding
        maxx += padding
        maxy += padding
        columns = max(3, int(math.ceil((maxx - minx) / pixel_size)))
        rows = max(3, int(math.ceil((maxy - miny) / pixel_size)))
        mask = np.zeros((rows, columns), dtype=bool)

        def ring_to_rc(coords: Sequence[tuple[float, float]]) -> tuple[np.ndarray, np.ndarray]:
            xy = np.asarray(coords, dtype=float)
            return ((maxy - xy[:, 1]) / pixel_size, (xy[:, 0] - minx) / pixel_size)
        rr, cc = ring_to_rc(list(polygon.exterior.coords))
        fill_r, fill_c = draw_polygon(rr, cc, shape=mask.shape)
        mask[fill_r, fill_c] = True
        for interior in polygon.interiors:
            rr, cc = ring_to_rc(list(interior.coords))
            hole_r, hole_c = draw_polygon(rr, cc, shape=mask.shape)
            mask[hole_r, hole_c] = False
        skeleton, distance = medial_axis(mask, return_distance=True, rng=0)
        return RasterFrame(floor_id=floor_id, component_id=component_id, polygon=polygon, pixel_size=pixel_size, minx=minx, miny=miny, maxx=maxx, maxy=maxy, mask=mask, skeleton=skeleton.astype(bool), distance=distance.astype(float))

    def _cast_to_blocked(mask: np.ndarray, row: float, col: float, dr: float, dc: float, max_steps: float) -> float | None:
        step = 0.5
        distance = step
        rows, columns = mask.shape
        last_index: tuple[int, int] | None = None
        while distance <= max_steps:
            rr = int(round(row + dr * distance))
            cc = int(round(col + dc * distance))
            if rr < 0 or rr >= rows or cc < 0 or (cc >= columns):
                return distance
            index = (rr, cc)
            if index != last_index:
                if not mask[index]:
                    return distance
                last_index = index
            distance += step
        return None

    def _portal_cross_section(frame: RasterFrame, row: int, col: int, config: BuildConfig) -> tuple[LineString, LineString, Point, float, tuple[float, float], float] | None:
        clearance_pixels = float(frame.distance[row, col])
        max_steps = max(12.0, clearance_pixels * 5.0)
        best: tuple[float, float, float, float] | None = None
        for angle in np.linspace(0.0, math.pi, config.ray_angle_count, endpoint=False):
            dc = math.cos(float(angle))
            dr = -math.sin(float(angle))
            first = _cast_to_blocked(frame.mask, row, col, dr, dc, max_steps)
            second = _cast_to_blocked(frame.mask, row, col, -dr, -dc, max_steps)
            if first is None or second is None:
                continue
            total = first + second
            if best is None or total < best[0]:
                best = (total, first, second, float(angle))
        if best is None:
            return None
        total, first, second, angle = best
        center_xy = frame.rc_to_xy(row, col)
        center = Point(center_xy)
        ux, uy = (math.cos(angle), math.sin(angle))
        extension = frame.pixel_size * 1.5
        start = (center.x - ux * (second * frame.pixel_size + extension), center.y - uy * (second * frame.pixel_size + extension))
        end = (center.x + ux * (first * frame.pixel_size + extension), center.y + uy * (first * frame.pixel_size + extension))
        full_line = LineString([start, end])
        inside_margin = frame.pixel_size * 0.65
        rough_start = (center.x - ux * max(0.0, second * frame.pixel_size - inside_margin), center.y - uy * max(0.0, second * frame.pixel_size - inside_margin))
        rough_end = (center.x + ux * max(0.0, first * frame.pixel_size - inside_margin), center.y + uy * max(0.0, first * frame.pixel_size - inside_margin))
        rough_segment = LineString([rough_start, rough_end])
        if rough_segment.length <= 0:
            return None
        return (rough_segment, full_line, center, float(rough_segment.length), (ux, uy), clearance_pixels)

    def _raster_label_near(labels: np.ndarray, row: float, col: float) -> int:
        center_row, center_col = (int(round(row)), int(round(col)))
        for radius in (0, 1, 2, 3):
            r0, r1 = (max(0, center_row - radius), min(labels.shape[0], center_row + radius + 1))
            c0, c1 = (max(0, center_col - radius), min(labels.shape[1], center_col + radius + 1))
            values = labels[r0:r1, c0:c1]
            nonzero = values[values > 0]
            if nonzero.size:
                counts = np.bincount(nonzero)
                return int(np.argmax(counts))
        return 0

    def _is_raster_separator(frame: RasterFrame, row: int, col: int, segment: LineString, cross_direction: tuple[float, float], width_pixels: float, config: BuildConfig) -> bool:
        radius = max(14, int(math.ceil(width_pixels * 3.0)))
        r0, r1 = (max(0, row - radius), min(frame.mask.shape[0], row + radius + 1))
        c0, c1 = (max(0, col - radius), min(frame.mask.shape[1], col + radius + 1))
        local = frame.mask[r0:r1, c0:c1].copy()
        if local.size == 0:
            return False
        start = frame.xy_to_rc(*segment.coords[0])
        end = frame.xy_to_rc(*segment.coords[-1])
        line_rows, line_cols = draw_line(int(round(start[0] - r0)), int(round(start[1] - c0)), int(round(end[0] - r0)), int(round(end[1] - c0)))
        thickness = max(1, int(math.ceil(config.separator_cut_width_pixels)))
        for line_row, line_col in zip(line_rows, line_cols):
            rr0, rr1 = (max(0, line_row - thickness), min(local.shape[0], line_row + thickness + 1))
            cc0, cc1 = (max(0, line_col - thickness), min(local.shape[1], line_col + thickness + 1))
            local[rr0:rr1, cc0:cc1] = False
        labels, component_count = label(local, structure=np.ones((3, 3), dtype=np.uint8))
        if component_count < 2:
            return False
        ux, uy = cross_direction
        passage_xy = (-uy, ux)
        passage_rc = (-passage_xy[1], passage_xy[0])
        base_distance = max(width_pixels * 0.65, 3.0)
        for factor in (1.0, 1.5, 2.0, 2.75):
            distance = base_distance * factor
            first_label = _raster_label_near(labels, row - r0 + passage_rc[0] * distance, col - c0 + passage_rc[1] * distance)
            second_label = _raster_label_near(labels, row - r0 - passage_rc[0] * distance, col - c0 - passage_rc[1] * distance)
            if first_label > 0 and second_label > 0:
                return first_label != second_label
        return False

    def _detect_component_candidates(frame: RasterFrame, obstacle_union: Any, config: BuildConfig, candidate_limit: int) -> list[PortalCandidate]:
        skeleton = frame.skeleton
        if not np.any(skeleton):
            return []
        neighbor_kernel = np.ones((3, 3), dtype=np.uint8)
        neighbor_kernel[1, 1] = 0
        degree = convolve(skeleton.astype(np.uint8), neighbor_kernel, mode='constant', cval=0)
        distance_for_minimum = np.where(skeleton, frame.distance, np.inf)
        window = max(3, int(config.local_minimum_window) | 1)
        local_minimum = minimum_filter(distance_for_minimum, size=window, mode='constant', cval=np.inf)
        pixels = np.argwhere(skeleton & (degree == 2) & (frame.distance <= local_minimum + 0.35))
        eligible = np.zeros_like(skeleton, dtype=bool)
        score_map = np.zeros_like(frame.distance, dtype=float)
        context = max(4, int(config.local_context_radius))
        for row, col in pixels:
            clearance = float(frame.distance[row, col])
            if clearance < max(1.0, config.minimum_portal_width_pixels * 0.45):
                continue
            r0, r1 = (max(0, row - context), min(skeleton.shape[0], row + context + 1))
            c0, c1 = (max(0, col - context), min(skeleton.shape[1], col + context + 1))
            local_values = frame.distance[r0:r1, c0:c1][skeleton[r0:r1, c0:c1]]
            if local_values.size < 5:
                continue
            reference = float(np.quantile(local_values, 0.7))
            score = max(0.0, min(1.0, (reference - clearance) / max(reference, 1e-09)))
            if score >= config.minimum_bottleneck_score:
                eligible[row, col] = True
                score_map[row, col] = score
        plateau_labels, plateau_count = label(eligible, structure=np.ones((3, 3), dtype=np.uint8))
        raw: list[tuple[float, float, int, int]] = []
        for plateau_index in range(1, plateau_count + 1):
            coordinates = np.argwhere(plateau_labels == plateau_index)
            if coordinates.size == 0:
                continue
            clearances = frame.distance[coordinates[:, 0], coordinates[:, 1]]
            minimum_clearance = float(np.min(clearances))
            central_candidates = coordinates[clearances <= minimum_clearance + 0.35]
            centroid = np.mean(central_candidates, axis=0)
            distances_to_center = np.sum((central_candidates - centroid) ** 2, axis=1)
            chosen = central_candidates[int(np.argmin(distances_to_center))]
            row, col = (int(chosen[0]), int(chosen[1]))
            plateau_score = float(np.max(score_map[coordinates[:, 0], coordinates[:, 1]]))
            raw.append((plateau_score, float(frame.distance[row, col]), row, col))
        raw.sort(key=lambda item: (-item[0], item[1]))
        selected: list[tuple[float, float, int, int]] = []
        for candidate in raw:
            score, clearance, row, col = candidate
            radius = max(5.0, clearance * 1.5)
            if any(((row - other[2]) ** 2 + (col - other[3]) ** 2 <= radius * radius for other in selected)):
                continue
            selected.append(candidate)
            if len(selected) >= candidate_limit:
                break
        output: list[PortalCandidate] = []
        prepared_obstacles = None if obstacle_union.is_empty else prep(obstacle_union)
        for score, _, row, col in selected:
            section = _portal_cross_section(frame, row, col, config)
            if section is None:
                continue
            rough_segment, full_line, center, width, direction, clearance_pixels = section
            width_pixels = width / frame.pixel_size
            separator = _is_raster_separator(frame, row, col, full_line, direction, width_pixels, config)
            segment = full_line if separator else rough_segment
            endpoint_points = [Point(segment.coords[0]), Point(segment.coords[-1])]
            contact_tolerance = frame.pixel_size * config.obstacle_contact_tolerance_pixels
            contact_count = sum((prepared_obstacles.intersects(point.buffer(contact_tolerance, resolution=4)) for point in endpoint_points)) if separator and prepared_obstacles is not None else 0
            balanced = 1.0 - min(1.0, abs(center.distance(endpoint_points[0]) - center.distance(endpoint_points[1])) / max(width, 1e-09))
            confidence = min(1.0, 0.55 * score + 0.25 * int(separator) + 0.1 * (contact_count / 2.0) + 0.1 * balanced)
            reasons: list[str] = []
            if width_pixels < config.minimum_portal_width_pixels:
                reasons.append('below_minimum_width')
            if width_pixels > config.maximum_portal_width_pixels:
                reasons.append('above_maximum_width')
            if contact_count < 2:
                reasons.append('not_two_obstacle_contacts')
            if not separator:
                reasons.append('not_local_separator')
            accepted = not reasons
            output.append(PortalCandidate(portal_id='', floor_id=frame.floor_id, component_id=frame.component_id, center=center, geometry=segment, width=width, clearance=clearance_pixels * frame.pixel_size, bottleneck_score=score, contact_count=contact_count, separator=separator, confidence=confidence, status='accepted_for_segmentation' if accepted else 'candidate_only', reason='accepted_wall_gap_separator' if accepted else ';'.join(reasons), properties={'pixel_size': frame.pixel_size, 'width_pixels': width_pixels}))
        return output

    def _segment_component(polygon: Polygon, portals: list[PortalCandidate], pixel_size: float, config: BuildConfig) -> list[Polygon]:
        accepted = [portal.geometry for portal in portals if portal.status == 'accepted_for_segmentation']
        if not accepted:
            return [polygon]
        cutters = unary_union(accepted)
        cut_width = max(pixel_size * config.separator_cut_width_pixels * 0.45, 1e-08)
        segmented = _safe_geometry(polygon.difference(cutters.buffer(cut_width, cap_style=2, join_style=2)))
        minimum_area = pixel_size * pixel_size * config.minimum_region_pixels
        faces = [part for part in _iter_polygons(segmented) if part.area >= minimum_area]
        return faces or [polygon]

    def _assign_portal_regions(portals: list[PortalCandidate], regions: list[AreaRegion], pixel_size_by_floor: dict[str, float]) -> list[dict[str, Any]]:
        by_component: dict[tuple[str, str], list[AreaRegion]] = defaultdict(list)
        for region in regions:
            by_component[region.floor_id, region.component_id].append(region)
        edges: list[dict[str, Any]] = []
        for portal in portals:
            if portal.status != 'accepted_for_segmentation':
                continue
            tolerance = max(pixel_size_by_floor.get(portal.floor_id, 1.0) * 0.65, 1e-08)
            touched: list[tuple[float, AreaRegion]] = []
            for region in by_component.get((portal.floor_id, portal.component_id), []):
                overlap = float(region.geometry.boundary.intersection(portal.geometry.buffer(tolerance)).length)
                if overlap > tolerance:
                    touched.append((overlap, region))
            touched.sort(key=lambda item: item[0], reverse=True)
            unique: list[AreaRegion] = []
            for _, region in touched:
                if region.area_id not in {item.area_id for item in unique}:
                    unique.append(region)
            if len(unique) < 2:
                portal.status = 'candidate_only'
                portal.reason = 'portal_did_not_split_two_regions'
                continue
            first, second = unique[:2]
            portal.area_a, portal.area_b = (first.area_id, second.area_id)
            first.portal_ids.append(portal.portal_id)
            second.portal_ids.append(portal.portal_id)
            edges.append({'edge_id': f'{portal.floor_id}_EDGE_{len(edges) + 1:06d}', 'floor_id': portal.floor_id, 'area_a': first.area_id, 'area_b': second.area_id, 'portal_id': portal.portal_id, 'portal_width': portal.width, 'confidence': portal.confidence, 'geometry': mapping(portal.geometry)})
        return edges

    def _write_feature_collection(path: Path, features: Iterable[dict[str, Any]]) -> None:
        _write_json(path, {'type': 'FeatureCollection', 'features': list(features)})

    def _write_portal_csv(path: Path, portals: list[PortalCandidate]) -> None:
        fields = ['portal_id', 'floor_id', 'component_id', 'center_x', 'center_y', 'width', 'clearance', 'bottleneck_score', 'obstacle_contact_count', 'local_separator', 'confidence', 'status', 'reason', 'area_a', 'area_b']
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for portal in portals:
                writer.writerow({'portal_id': portal.portal_id, 'floor_id': portal.floor_id, 'component_id': portal.component_id, 'center_x': portal.center.x, 'center_y': portal.center.y, 'width': portal.width, 'clearance': portal.clearance, 'bottleneck_score': portal.bottleneck_score, 'obstacle_contact_count': portal.contact_count, 'local_separator': int(portal.separator), 'confidence': portal.confidence, 'status': portal.status, 'reason': portal.reason, 'area_a': portal.area_a, 'area_b': portal.area_b})

    def _ring_without_closure(ring: Any) -> list[tuple[float, float]]:
        coordinates = [(float(x), float(y)) for x, y in ring.coords]
        return coordinates[:-1] if len(coordinates) > 1 and coordinates[0] == coordinates[-1] else coordinates

    def _write_review_dxf(path: Path, source_dxf: Path | None, regions: list[AreaRegion], portals: list[PortalCandidate]) -> None:
        if ezdxf is None:
            raise RuntimeError('ezdxf is required for DXF review output')
        if source_dxf and source_dxf.exists():
            document = ezdxf.readfile(source_dxf)
        else:
            document = ezdxf.new('R2010')
        modelspace = document.modelspace()
        layer_specs = {'AG_REGION_BOUNDARY': 5, 'AG_REGION_LABEL': 7, 'AG_PORTAL_ACCEPTED': 3, 'AG_PORTAL_CANDIDATE': 2, 'AG_PORTAL_REJECTED': 1, 'AG_PORTAL_LABEL': 7}
        for name, color in layer_specs.items():
            if name not in document.layers:
                document.layers.new(name, dxfattribs={'color': color})
        all_geometries = [region.geometry for region in regions]
        bounds = unary_union(all_geometries).bounds if all_geometries else (0.0, 0.0, 1000.0, 1000.0)
        text_height = max(math.hypot(bounds[2] - bounds[0], bounds[3] - bounds[1]) / 700.0, 1.0)
        for index, region in enumerate(regions, start=1):
            color = 30 + index * 17 % 190
            modelspace.add_lwpolyline(_ring_without_closure(region.geometry.exterior), close=True, dxfattribs={'layer': 'AG_REGION_BOUNDARY', 'color': color})
            center = region.geometry.representative_point()
            label = modelspace.add_text(region.area_id, dxfattribs={'layer': 'AG_REGION_LABEL', 'height': text_height, 'color': color})
            label.dxf.insert = (center.x, center.y)
        for portal in portals:
            layer = 'AG_PORTAL_ACCEPTED' if portal.status == 'accepted_for_segmentation' else 'AG_PORTAL_CANDIDATE'
            color = 3 if portal.status == 'accepted_for_segmentation' else 2
            modelspace.add_lwpolyline([(float(x), float(y)) for x, y in portal.geometry.coords], dxfattribs={'layer': layer, 'color': color, 'lineweight': 70})
            modelspace.add_circle((portal.center.x, portal.center.y), radius=max(text_height * 0.35, portal.width * 0.06), dxfattribs={'layer': layer, 'color': color})
            label = modelspace.add_text(f'{portal.portal_id} W={portal.width:.0f} C={portal.confidence:.2f}', dxfattribs={'layer': 'AG_PORTAL_LABEL', 'height': text_height * 0.65, 'color': color})
            label.dxf.insert = (portal.center.x, portal.center.y)
        path.parent.mkdir(parents=True, exist_ok=True)
        document.saveas(path)

    def build_portal_area_graph(free_area_path: Path, obstacle_path: Path, output_dir: Path, source_dxf: Path | None=None, config: BuildConfig | None=None, write_review_dxf: bool=True) -> dict[str, Any]:
        config = config or BuildConfig()
        started = time.perf_counter()
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        free_by_floor = _load_floor_geometries(Path(free_area_path))
        obstacles_by_floor = _load_floor_geometries(Path(obstacle_path))
        if not free_by_floor:
            raise ValueError('No floor geometry was loaded from free-area GeoJSON')
        portals: list[PortalCandidate] = []
        components_by_floor: dict[str, list[tuple[str, Polygon]]] = defaultdict(list)
        pixel_size_by_floor: dict[str, float] = {}
        raster_stats: list[dict[str, Any]] = []
        for floor_id, floor_geometry in free_by_floor.items():
            print(f'[area-graph] floor={floor_id} raster start', flush=True)
            pixel_size = _adaptive_pixel_size(floor_geometry, config)
            pixel_size_by_floor[floor_id] = pixel_size
            component_index = 0
            floor_portals: list[PortalCandidate] = []
            for polygon in sorted(_iter_polygons(floor_geometry), key=lambda item: item.area, reverse=True):
                component_index += 1
                component_id = f'{floor_id}_FREE_{component_index:04d}'
                components_by_floor[floor_id].append((component_id, polygon))
                if polygon.area < pixel_size * pixel_size * config.minimum_component_pixels:
                    continue
                frame_started = time.perf_counter()
                frame = _rasterize_polygon(floor_id, component_id, polygon, pixel_size)
                remaining_candidates = max(0, config.max_candidates_per_floor - len(floor_portals))
                detected = _detect_component_candidates(frame, obstacles_by_floor.get(floor_id, GeometryCollection()), config, remaining_candidates) if remaining_candidates > 0 else []
                floor_portals.extend(detected)
                raster_stats.append({'floor_id': floor_id, 'component_id': component_id, 'pixel_size': pixel_size, 'rows': int(frame.mask.shape[0]), 'columns': int(frame.mask.shape[1]), 'free_pixels': int(np.count_nonzero(frame.mask)), 'skeleton_pixels': int(np.count_nonzero(frame.skeleton)), 'candidate_count': len(detected), 'elapsed_seconds': round(time.perf_counter() - frame_started, 6)})
            floor_portals.sort(key=lambda item: (-item.confidence, item.width))
            floor_portals = floor_portals[:config.max_candidates_per_floor]
            for index, portal in enumerate(floor_portals, start=1):
                portal.portal_id = f'{floor_id}_PORTAL_{index:05d}'
            portals.extend(floor_portals)
            floor_status = Counter((item.status for item in floor_portals))
            print(f"[area-graph] floor={floor_id} raster done components={component_index} candidates={len(floor_portals)} accepted={floor_status.get('accepted_for_segmentation', 0)} pixel={pixel_size:.3f}", flush=True)
        print('[area-graph] polygon segmentation start', flush=True)
        regions: list[AreaRegion] = []
        portals_by_component: dict[tuple[str, str], list[PortalCandidate]] = defaultdict(list)
        for portal in portals:
            portals_by_component[portal.floor_id, portal.component_id].append(portal)
        for floor_id, components in components_by_floor.items():
            floor_regions: list[tuple[str, Polygon, float]] = []
            for component_id, polygon in components:
                parts = _segment_component(polygon, portals_by_component.get((floor_id, component_id), []), pixel_size_by_floor[floor_id], config)
                for part in parts:
                    floor_regions.append((component_id, part, polygon.area))
            floor_regions.sort(key=lambda item: (item[1].representative_point().x, item[1].representative_point().y))
            for index, (component_id, geometry, source_area) in enumerate(floor_regions, start=1):
                regions.append(AreaRegion(area_id=f'{floor_id}_AREA_{index:05d}', floor_id=floor_id, component_id=component_id, geometry=geometry, source_component_area=source_area))
        edges = _assign_portal_regions(portals, regions, pixel_size_by_floor)
        print(f'[area-graph] polygon segmentation done regions={len(regions)} edges={len(edges)}', flush=True)
        outputs = {'portal_geojson': output_dir / 'portal_candidates.geojson', 'portal_csv': output_dir / 'portal_candidates.csv', 'regions_geojson': output_dir / 'area_graph_regions.geojson', 'area_graph_json': output_dir / 'area_graph.json', 'summary_json': output_dir / 'area_graph_summary.json', 'review_dxf': output_dir / 'area_graph_review.dxf'}
        _write_feature_collection(outputs['portal_geojson'], (portal.feature() for portal in portals))
        _write_portal_csv(outputs['portal_csv'], portals)
        _write_feature_collection(outputs['regions_geojson'], (region.feature() for region in regions))
        graph_payload = {'graph_type': 'portal_area_graph', 'nodes': [region.feature()['properties'] | {'geometry': mapping(region.geometry)} for region in regions], 'edges': edges}
        _write_json(outputs['area_graph_json'], graph_payload)
        if write_review_dxf:
            print('[area-graph] review DXF start', flush=True)
            _write_review_dxf(outputs['review_dxf'], source_dxf, regions, portals)
            print('[area-graph] review DXF done', flush=True)
        status_counts = Counter((portal.status for portal in portals))
        summary = {'free_area_path': str(Path(free_area_path).resolve()), 'obstacle_path': str(Path(obstacle_path).resolve()), 'source_dxf': str(source_dxf.resolve()) if source_dxf else '', 'output_dir': str(output_dir), 'floor_count': len(free_by_floor), 'free_area_component_count': sum((len(rows) for rows in components_by_floor.values())), 'portal_candidate_count': len(portals), 'accepted_portal_count': int(status_counts.get('accepted_for_segmentation', 0)), 'candidate_only_count': int(status_counts.get('candidate_only', 0)), 'area_count': len(regions), 'edge_count': len(edges), 'pixel_size_by_floor': pixel_size_by_floor, 'portal_status_counts': dict(status_counts), 'raster_components': raster_stats, 'elapsed_seconds': round(time.perf_counter() - started, 6), 'outputs': {key: str(value) if key != 'review_dxf' or write_review_dxf else '' for key, value in outputs.items()}}
        _write_json(outputs['summary_json'], summary)
        return summary

    return dict(locals())

_s06_area_graph = _register_embedded_module(
    'fire_inspection_system.portal_area_graph',
    _build_s06_area_graph(),
    aliases=('portal_area_graph',),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/area_graph_navigation.py
# -----------------------------------------------------------------------------
def _build_s06_navigation():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/area_graph_navigation.py'
    )
    __name__ = 'fire_inspection_system.area_graph_navigation'
    __package__ = 'fire_inspection_system'
    """Build executable navigation edges downstream of ``portal_area_graph.py``.

    The AreaGraph is a semantic macro graph.  This module adds a sparse medial-axis
    graph inside each free-space component, preserves accepted Portal positions as
    navigation nodes, attaches AreaGraph anchors and inspection targets by raster
    geodesics, and reports target reachability.

    Typical usage::

        python area_graph_navigation.py --run-dir D:\\untitled5\\outputs\\...\\RUN
    """
    import argparse
    import csv
    import json
    import math
    import time
    from collections import Counter, defaultdict, deque
    from dataclasses import dataclass, replace
    from pathlib import Path
    from typing import Any, Iterable
    import numpy as np
    import shapely
    from scipy.ndimage import distance_transform_edt
    from scipy.spatial import cKDTree
    from shapely.geometry import LineString, Point, Polygon, shape
    from shapely.ops import nearest_points
    from shapely.prepared import prep
    from skimage.graph import MCP_Geometric
    from skimage.morphology import medial_axis
    from portal_area_graph import _iter_polygons, _load_floor_geometries, _rasterize_polygon, _read_json, _write_json
    try:
        import ezdxf
    except ImportError:
        ezdxf = None
    NEIGHBORS_8 = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))

    @dataclass
    class NavConfig:
        simplify_tolerance_pixels: float = 0.35
        minimum_component_pixels: float = 4.0
        include_area_anchors: bool = True

    class GraphBuilder:

        def __init__(self) -> None:
            self.nodes: dict[str, dict[str, Any]] = {}
            self.edges: list[dict[str, Any]] = []
            self.adjacency: dict[str, set[str]] = defaultdict(set)
            self._edge_index = 0

        def add_node(self, node_id: str, **properties: Any) -> str:
            row = {'node_id': node_id, **properties}
            self.nodes[node_id] = row
            self.adjacency.setdefault(node_id, set())
            return node_id

        def add_edge(self, node_a: str, node_b: str, coordinates: Iterable[tuple[float, float]], **properties: Any) -> str:
            coords = [(float(x), float(y)) for x, y in coordinates]
            if not coords:
                return ''
            if len(coords) == 1:
                coords.append(coords[0])
            self._edge_index += 1
            edge_id = f'NAV_EDGE_{self._edge_index:07d}'
            length = sum((math.hypot(x2 - x1, y2 - y1) for (x1, y1), (x2, y2) in zip(coords, coords[1:])))
            row = {'edge_id': edge_id, 'node_a': node_a, 'node_b': node_b, 'length': float(length), **properties, 'geometry': {'type': 'LineString', 'coordinates': coords}}
            self.edges.append(row)
            self.adjacency[node_a].add(node_b)
            self.adjacency[node_b].add(node_a)
            return edge_id

    def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)

    def _component_catalog(free_by_floor: dict[str, Any]) -> dict[str, list[tuple[str, Polygon]]]:
        result: dict[str, list[tuple[str, Polygon]]] = {}
        for floor_id, geometry in free_by_floor.items():
            polygons = sorted(_iter_polygons(geometry), key=lambda item: item.area, reverse=True)
            result[floor_id] = [(f'{floor_id}_FREE_{index:04d}', polygon) for index, polygon in enumerate(polygons, start=1)]
        return result

    def _load_regions(path: Path) -> tuple[list[dict[str, Any]], dict[tuple[str, str], list[dict[str, Any]]]]:
        features = _read_json(path).get('features', []) or []
        rows: list[dict[str, Any]] = []
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for feature in features:
            props = dict(feature.get('properties') or {})
            geometry = shape(feature.get('geometry'))
            row = {**props, 'geometry_object': geometry, 'prepared_geometry': prep(geometry)}
            rows.append(row)
            grouped[str(props.get('floor_id', '')), str(props.get('component_id', ''))].append(row)
        return (rows, grouped)

    def _load_accepted_portals(path: Path) -> dict[tuple[str, str], list[dict[str, Any]]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for feature in _read_json(path).get('features', []) or []:
            props = dict(feature.get('properties') or {})
            if props.get('status') != 'accepted_for_segmentation':
                continue
            geometry = shape(feature.get('geometry'))
            row = {**props, 'geometry_object': geometry, 'center': geometry.interpolate(0.5, normalized=True)}
            grouped[str(props.get('floor_id', '')), str(props.get('component_id', ''))].append(row)
        return grouped

    def _load_targets(path: Path) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, feature in enumerate(_read_json(path).get('features', []) or [], start=1):
            props = dict(feature.get('properties') or {})
            geometry = shape(feature.get('geometry'))
            if geometry.is_empty:
                continue
            point = geometry if geometry.geom_type == 'Point' else geometry.representative_point()
            target_id = str(props.get('target_id') or f'TARGET_{index:07d}')
            rows.append({**props, 'target_id': target_id, 'raw_point': point})
        return rows

    def _point_region(point: Point, regions: list[dict[str, Any]]) -> str:
        if not regions:
            return ''
        for region in regions:
            geometry = region['geometry_object']
            minx, miny, maxx, maxy = geometry.bounds
            if minx <= point.x <= maxx and miny <= point.y <= maxy and region['prepared_geometry'].covers(point):
                return str(region.get('area_id', ''))
        return str(min(regions, key=lambda row: row['geometry_object'].distance(point)).get('area_id', ''))

    def _assign_targets_to_components(targets: list[dict[str, Any]], components: dict[str, list[tuple[str, Polygon]]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        prepared_components = {(floor_id, component_id): prep(polygon) for floor_id, rows in components.items() for component_id, polygon in rows}
        for target in targets:
            floor_id = str(target.get('floor_id', ''))
            candidates = components.get(floor_id, [])
            if not candidates:
                target.update({'assignment_status': 'missing_floor', 'component_id': '', 'access_point': target['raw_point']})
                continue
            raw_point: Point = target['raw_point']
            selected: tuple[str, Polygon] | None = None
            for component_id, polygon in candidates:
                minx, miny, maxx, maxy = polygon.bounds
                if minx <= raw_point.x <= maxx and miny <= raw_point.y <= maxy and prepared_components[floor_id, component_id].covers(raw_point):
                    selected = (component_id, polygon)
                    break
            if selected is None:
                selected = min(candidates, key=lambda row: row[1].distance(raw_point))
                access_point = nearest_points(raw_point, selected[1])[1]
                status = 'projected_to_free_space'
            else:
                access_point = raw_point
                status = 'inside_free_space'
            target.update({'assignment_status': status, 'component_id': selected[0], 'component_polygon': selected[1], 'access_point': access_point, 'projection_distance': float(raw_point.distance(access_point))})
            grouped[floor_id, selected[0]].append(target)
        return grouped

    def _skeleton_neighbors(pixel: tuple[int, int], skeleton: np.ndarray) -> list[tuple[int, int]]:
        row, col = pixel
        rows, cols = skeleton.shape
        result: list[tuple[int, int]] = []
        for dr, dc in NEIGHBORS_8:
            rr, cc = (row + dr, col + dc)
            if 0 <= rr < rows and 0 <= cc < cols and skeleton[rr, cc]:
                result.append((rr, cc))
        return result

    def _edge_key(a: tuple[int, int], b: tuple[int, int]) -> tuple[tuple[int, int], tuple[int, int]]:
        return (a, b) if a <= b else (b, a)

    def _nearest_true_pixel(point: Point, frame: Any, nearest_rows: np.ndarray, nearest_cols: np.ndarray) -> tuple[int, int]:
        row_f, col_f = frame.xy_to_rc(point.x, point.y)
        row = int(np.clip(round(row_f), 0, frame.mask.shape[0] - 1))
        col = int(np.clip(round(col_f), 0, frame.mask.shape[1] - 1))
        if frame.mask[row, col]:
            return (row, col)
        return (int(nearest_rows[row, col]), int(nearest_cols[row, col]))

    def _strict_free_space_frame(frame: Any) -> Any:
        """Keep only raster cells whose centers are strictly inside free space."""
        pixels = np.argwhere(frame.mask)
        strict_mask = np.zeros_like(frame.mask, dtype=bool)
        if len(pixels):
            xs = frame.minx + (pixels[:, 1].astype(float) + 0.5) * frame.pixel_size
            ys = frame.maxy - (pixels[:, 0].astype(float) + 0.5) * frame.pixel_size
            shapely.prepare(frame.polygon)
            inside = shapely.contains_xy(frame.polygon, xs, ys)
            accepted = pixels[inside]
            if len(accepted):
                strict_mask[accepted[:, 0], accepted[:, 1]] = True
        if np.any(strict_mask):
            skeleton, distance = medial_axis(strict_mask, return_distance=True, rng=0)
        else:
            skeleton = np.zeros_like(strict_mask, dtype=bool)
            distance = np.zeros_like(strict_mask, dtype=float)
        return replace(frame, mask=strict_mask, skeleton=skeleton, distance=distance)

    def _simplify_path(coordinates: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
        if len(coordinates) <= 2:
            return coordinates
        simplified = LineString(coordinates).simplify(tolerance, preserve_topology=False)
        if simplified.geom_type != 'LineString' or len(simplified.coords) < 2:
            return [coordinates[0], coordinates[-1]]
        return [(float(x), float(y)) for x, y in simplified.coords]

    def _trace_skeleton(frame: Any, portal_rows: list[dict[str, Any]], regions: list[dict[str, Any]], graph: GraphBuilder, config: NavConfig) -> tuple[dict[tuple[int, int], str], list[tuple[int, int]], dict[str, Any]]:
        skeleton = frame.skeleton.copy()
        if not np.any(skeleton):
            return ({}, [], {'skeleton_pixels': 0, 'skeleton_nodes': 0, 'skeleton_edges': 0, 'portal_node_count': 0, 'portal_snap_max': 0.0})
        prepared_polygon = prep(frame.polygon)
        valid_neighbors: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
        for raw_pixel in np.argwhere(skeleton):
            pixel = tuple(map(int, raw_pixel))
            for neighbor in _skeleton_neighbors(pixel, skeleton):
                if neighbor <= pixel:
                    continue
                segment = LineString([frame.rc_to_xy(*pixel), frame.rc_to_xy(*neighbor)])
                if prepared_polygon.covers(segment):
                    valid_neighbors[pixel].append(neighbor)
                    valid_neighbors[neighbor].append(pixel)
        key_pixels: set[tuple[int, int]] = {pixel for pixel in valid_neighbors if len(valid_neighbors[pixel]) != 2}
        visited_components: set[tuple[int, int]] = set()
        for seed in valid_neighbors:
            if seed in visited_components:
                continue
            stack = [seed]
            component_pixels: list[tuple[int, int]] = []
            visited_components.add(seed)
            while stack:
                current = stack.pop()
                component_pixels.append(current)
                for neighbor in valid_neighbors[current]:
                    if neighbor not in visited_components:
                        visited_components.add(neighbor)
                        stack.append(neighbor)
            if component_pixels and (not any((pixel in key_pixels for pixel in component_pixels))):
                key_pixels.add(min(component_pixels))
        for raw_pixel in np.argwhere(skeleton):
            pixel = tuple(map(int, raw_pixel))
            if pixel not in valid_neighbors:
                key_pixels.add(pixel)
        skeleton_pixels = np.argwhere(skeleton)
        portal_pixel_to_rows: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        if len(skeleton_pixels):
            skeleton_xy = np.asarray([frame.rc_to_xy(row, col) for row, col in skeleton_pixels])
            for portal in portal_rows:
                center: Point = portal['center']
                distances = np.square(skeleton_xy[:, 0] - center.x) + np.square(skeleton_xy[:, 1] - center.y)
                row, col = map(int, skeleton_pixels[int(np.argmin(distances))])
                key_pixels.add((row, col))
                portal_pixel_to_rows[row, col].append(portal)
        node_by_pixel: dict[tuple[int, int], str] = {}
        portal_snap_distances: list[float] = []
        for index, pixel in enumerate(sorted(key_pixels), start=1):
            x, y = frame.rc_to_xy(*pixel)
            point = Point(x, y)
            portal_rows_here = portal_pixel_to_rows.get(pixel, [])
            if portal_rows_here:
                portal = portal_rows_here[0]
                node_id = f"PORTAL::{portal['portal_id']}"
                kind = 'portal'
                portal_id = str(portal['portal_id'])
                portal_snap_distances.append(float(point.distance(portal['center'])))
            else:
                node_id = f'SK::{frame.component_id}::{index:06d}'
                kind = 'skeleton'
                portal_id = ''
            graph.add_node(node_id, kind=kind, floor_id=frame.floor_id, component_id=frame.component_id, area_id=_point_region(point, regions), portal_id=portal_id, x=float(x), y=float(y))
            node_by_pixel[pixel] = node_id
        visited: set[tuple[tuple[int, int], tuple[int, int]]] = set()
        edge_count_before = len(graph.edges)
        prepared_validation_polygon = prepared_polygon
        for start in sorted(key_pixels):
            for neighbor in valid_neighbors.get(start, []):
                first_edge = _edge_key(start, neighbor)
                if first_edge in visited:
                    continue
                visited.add(first_edge)
                path = [start, neighbor]
                previous, current = (start, neighbor)
                guard = 0
                while current not in key_pixels:
                    options = [pixel for pixel in valid_neighbors.get(current, []) if pixel != previous]
                    if len(options) != 1:
                        key_pixels.add(current)
                        if current not in node_by_pixel:
                            x, y = frame.rc_to_xy(*current)
                            node_id = f'SK::{frame.component_id}::X{len(node_by_pixel) + 1:06d}'
                            graph.add_node(node_id, kind='skeleton', floor_id=frame.floor_id, component_id=frame.component_id, area_id=_point_region(Point(x, y), regions), portal_id='', x=float(x), y=float(y))
                            node_by_pixel[current] = node_id
                        break
                    next_pixel = options[0]
                    segment_key = _edge_key(current, next_pixel)
                    if segment_key in visited:
                        break
                    visited.add(segment_key)
                    path.append(next_pixel)
                    previous, current = (current, next_pixel)
                    guard += 1
                    if guard > skeleton.size:
                        break
                if current not in node_by_pixel or len(path) < 2:
                    continue
                raw_coordinates = [frame.rc_to_xy(row, col) for row, col in path]
                coordinates = _simplify_path(raw_coordinates, frame.pixel_size * config.simplify_tolerance_pixels)
                vector_valid = prepared_validation_polygon.covers(LineString(coordinates))
                if not vector_valid:
                    coordinates = raw_coordinates
                    vector_valid = prepared_validation_polygon.covers(LineString(coordinates))
                if not vector_valid:
                    continue
                graph.add_edge(node_by_pixel[start], node_by_pixel[current], coordinates, kind='skeleton_edge', floor_id=frame.floor_id, component_id=frame.component_id, vector_valid_with_raster_tolerance=bool(vector_valid))
        return (node_by_pixel, sorted(key_pixels), {'skeleton_pixels': int(np.count_nonzero(skeleton)), 'skeleton_nodes': len(node_by_pixel), 'skeleton_edges': len(graph.edges) - edge_count_before, 'portal_node_count': len(portal_pixel_to_rows), 'portal_snap_max': max(portal_snap_distances, default=0.0)})

    def _attach_terminals(frame: Any, node_by_pixel: dict[tuple[int, int], str], key_pixels: list[tuple[int, int]], terminals: list[dict[str, Any]], graph: GraphBuilder, config: NavConfig) -> dict[str, Any]:
        if not terminals or not key_pixels:
            return {'terminal_count': len(terminals), 'attached_count': 0, 'failed_count': len(terminals)}
        _, nearest_indices = distance_transform_edt(~frame.mask, return_indices=True)
        nearest_rows, nearest_cols = (nearest_indices[0], nearest_indices[1])
        costs = np.where(frame.mask, 1.0, np.inf)
        mcp = MCP_Geometric(costs, fully_connected=False)
        cumulative_costs, _ = mcp.find_costs(starts=key_pixels)
        prepared_validation_polygon = prep(frame.polygon)
        attached = 0
        failed = 0
        for terminal in terminals:
            raw_point: Point = terminal['point']
            end_pixel = _nearest_true_pixel(raw_point, frame, nearest_rows, nearest_cols)
            x, y = frame.rc_to_xy(*end_pixel)
            access_point = Point(x, y)
            node_id = str(terminal['node_id'])
            graph.add_node(node_id, kind=terminal['kind'], floor_id=frame.floor_id, component_id=frame.component_id, area_id=terminal.get('area_id', ''), target_id=terminal.get('target_id', ''), target_class=terminal.get('target_class', ''), source_object_id=terminal.get('source_object_id', ''), assignment_status=terminal.get('assignment_status', ''), raw_x=float(terminal.get('raw_x', raw_point.x)), raw_y=float(terminal.get('raw_y', raw_point.y)), x=float(access_point.x), y=float(access_point.y), projection_distance=float(terminal.get('projection_distance', 0.0)), raster_snap_distance=float(raw_point.distance(access_point)))
            if not np.isfinite(cumulative_costs[end_pixel]):
                failed += 1
                continue
            try:
                pixel_path = mcp.traceback(end_pixel)
            except ValueError:
                failed += 1
                continue
            if not pixel_path:
                failed += 1
                continue
            start_pixel = tuple(map(int, pixel_path[0]))
            end_from_trace = tuple(map(int, pixel_path[-1]))
            if start_pixel not in node_by_pixel and end_from_trace in node_by_pixel:
                pixel_path = list(reversed(pixel_path))
                start_pixel = tuple(map(int, pixel_path[0]))
            skeleton_node = node_by_pixel.get(start_pixel)
            if not skeleton_node:
                failed += 1
                continue
            coordinates = [frame.rc_to_xy(int(row), int(col)) for row, col in reversed(pixel_path)]
            coordinates[0] = (access_point.x, access_point.y)
            skeleton_row = graph.nodes[skeleton_node]
            coordinates[-1] = (float(skeleton_row['x']), float(skeleton_row['y']))
            if len(coordinates) == 1:
                coordinates.append(coordinates[0])
            simplified = _simplify_path(coordinates, frame.pixel_size * config.simplify_tolerance_pixels)
            vector_valid = prepared_validation_polygon.covers(LineString(simplified))
            if not vector_valid:
                simplified = coordinates
                vector_valid = prepared_validation_polygon.covers(LineString(simplified))
            if not vector_valid:
                failed += 1
                continue
            graph.add_edge(node_id, skeleton_node, simplified, kind=f"{terminal['kind']}_access_edge", floor_id=frame.floor_id, component_id=frame.component_id, vector_valid_with_raster_tolerance=True, validation_method='free_space_raster_geodesic')
            attached += 1
        return {'terminal_count': len(terminals), 'attached_count': attached, 'failed_count': failed}

    def _connected_components(graph: GraphBuilder) -> tuple[dict[str, int], dict[int, list[str]]]:
        component_by_node: dict[str, int] = {}
        members: dict[int, list[str]] = defaultdict(list)
        component_id = 0
        for start in graph.nodes:
            if start in component_by_node:
                continue
            component_id += 1
            queue = deque([start])
            component_by_node[start] = component_id
            while queue:
                current = queue.popleft()
                members[component_id].append(current)
                for neighbor in graph.adjacency.get(current, set()):
                    if neighbor not in component_by_node:
                        component_by_node[neighbor] = component_id
                        queue.append(neighbor)
        return (component_by_node, members)

    def _add_virtual_access_nodes(graph: GraphBuilder, targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates_by_floor: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for node in graph.nodes.values():
            if node.get('kind') not in {'skeleton', 'portal'}:
                continue
            if not graph.adjacency.get(str(node['node_id'])):
                continue
            candidates_by_floor[str(node.get('floor_id', ''))].append(node)
        trees: dict[str, tuple[cKDTree, list[dict[str, Any]]]] = {}
        for floor_id, nodes in candidates_by_floor.items():
            coordinates = np.asarray([(float(node['x']), float(node['y'])) for node in nodes])
            if len(coordinates):
                trees[floor_id] = (cKDTree(coordinates), nodes)
        virtual_rows: list[dict[str, Any]] = []
        for target in targets:
            target_id = str(target['target_id'])
            target_node_id = f'TARGET::{target_id}'
            target_node = graph.nodes.get(target_node_id)
            if target_node is not None and graph.adjacency.get(target_node_id):
                target_node['access_node_id'] = target_node_id
                target_node['virtual_access'] = False
                continue
            floor_id = str(target.get('floor_id', ''))
            tree_row = trees.get(floor_id)
            raw_point: Point = target['raw_point']
            if target_node is None:
                graph.add_node(target_node_id, kind='target', floor_id=floor_id, component_id=str(target.get('component_id', '')), area_id='', target_id=target_id, target_class=target.get('target_class', ''), source_object_id=target.get('source_object_id', ''), assignment_status=target.get('assignment_status', ''), raw_x=float(raw_point.x), raw_y=float(raw_point.y), x=float(raw_point.x), y=float(raw_point.y), projection_distance=float(target.get('projection_distance', 0.0)), raster_snap_distance=0.0)
                target_node = graph.nodes[target_node_id]
            if tree_row is None:
                target_node['access_node_id'] = ''
                target_node['virtual_access'] = False
                continue
            tree, candidates = tree_row
            distance, index = tree.query((raw_point.x, raw_point.y), k=1)
            skeleton_node = candidates[int(index)]
            virtual_node_id = f'VIRTUAL::{target_id}'
            graph.add_node(virtual_node_id, kind='virtual_target_access', floor_id=floor_id, component_id=str(skeleton_node.get('component_id', '')), area_id=str(skeleton_node.get('area_id', '')), target_id=target_id, target_class=target.get('target_class', ''), x=float(skeleton_node['x']), y=float(skeleton_node['y']), raw_x=float(raw_point.x), raw_y=float(raw_point.y), virtual_access_distance=float(distance))
            graph.add_edge(virtual_node_id, str(skeleton_node['node_id']), [(float(skeleton_node['x']), float(skeleton_node['y']))] * 2, kind='virtual_target_access_edge', floor_id=floor_id, component_id=str(skeleton_node.get('component_id', '')), vector_valid_with_raster_tolerance=True, validation_method='coincident_virtual_node_on_navigation_skeleton')
            target_node['access_node_id'] = virtual_node_id
            target_node['virtual_access'] = True
            target_node['virtual_access_distance'] = float(distance)
            virtual_rows.append({'target_id': target_id, 'floor_id': floor_id, 'virtual_node_id': virtual_node_id, 'navigation_node_id': skeleton_node['node_id'], 'distance': float(distance)})
        return virtual_rows

    def _pairwise_rate(counts: Iterable[int]) -> float:
        values = [int(value) for value in counts if value > 0]
        total = sum(values)
        denominator = total * (total - 1) // 2
        if denominator == 0:
            return 1.0 if total else 0.0
        numerator = sum((value * (value - 1) // 2 for value in values))
        return float(numerator / denominator)

    def _analyze_reachability(graph: GraphBuilder, targets: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        component_by_node, members = _connected_components(graph)
        rows: list[dict[str, Any]] = []
        target_counts_by_component: Counter[int] = Counter()
        for target in targets:
            node_id = f"TARGET::{target['target_id']}"
            node = graph.nodes.get(node_id, {})
            access_node_id = str(node.get('access_node_id') or node_id)
            graph_component = component_by_node.get(access_node_id, 0)
            attached = bool(graph.adjacency.get(access_node_id))
            if attached:
                target_counts_by_component[graph_component] += 1
            access_node = graph.nodes.get(access_node_id, {})
            rows.append({'target_id': target['target_id'], 'target_class': target.get('target_class', ''), 'floor_id': target.get('floor_id', ''), 'component_id': access_node.get('component_id', target.get('component_id', '')), 'area_id': access_node.get('area_id', node.get('area_id', '')), 'assignment_status': target.get('assignment_status', ''), 'projection_distance': float(target.get('projection_distance', 0.0)), 'attached': int(attached), 'access_node_id': access_node_id, 'virtual_access': int(bool(node.get('virtual_access'))), 'virtual_access_distance': float(node.get('virtual_access_distance', 0.0)), 'graph_component': graph_component, 'reachable_to_floor_primary': 0, 'reason': 'connected' if attached else 'no_navigation_edge'})
        floors: dict[str, Any] = {}
        for floor_id in sorted({str(row['floor_id']) for row in rows}):
            floor_rows = [row for row in rows if str(row['floor_id']) == floor_id]
            attached_rows = [row for row in floor_rows if row['attached']]
            counts = Counter((int(row['graph_component']) for row in attached_rows))
            primary_component, primary_count = counts.most_common(1)[0] if counts else (0, 0)
            for row in floor_rows:
                row['reachable_to_floor_primary'] = int(bool(row['attached'] and row['graph_component'] == primary_component))
            total = len(floor_rows)
            attached_count = len(attached_rows)
            floors[floor_id] = {'target_count': total, 'attached_target_count': attached_count, 'attachment_rate': attached_count / total if total else 0.0, 'target_graph_component_count': len(counts), 'primary_component_target_count': primary_count, 'primary_component_coverage_rate': primary_count / total if total else 0.0, 'pairwise_target_reachability_rate': _pairwise_rate(counts.values()), 'all_targets_mutually_reachable': bool(total and attached_count == total and (len(counts) == 1))}
        all_attached = [row for row in rows if row['attached']]
        projected = [row for row in rows if row['assignment_status'] == 'projected_to_free_space']
        same_floor_possible_pairs = 0
        same_floor_reachable_pairs = 0
        for floor_row in floors.values():
            total = int(floor_row['target_count'])
            possible = total * (total - 1) // 2
            same_floor_possible_pairs += possible
            same_floor_reachable_pairs += round(possible * floor_row['pairwise_target_reachability_rate'])
        summary = {'target_count': len(rows), 'attached_target_count': len(all_attached), 'unattached_target_count': len(rows) - len(all_attached), 'unattached_targets': [{'target_id': row['target_id'], 'target_class': row['target_class'], 'floor_id': row['floor_id'], 'component_id': row['component_id'], 'projection_distance': row['projection_distance'], 'reason': row['reason']} for row in rows if not row['attached']], 'attachment_rate': len(all_attached) / len(rows) if rows else 0.0, 'same_floor_attachment_rate': len(all_attached) / len(rows) if rows else 0.0, 'same_floor_pairwise_target_reachability_rate': same_floor_reachable_pairs / same_floor_possible_pairs if same_floor_possible_pairs else 0.0, 'virtual_access_target_count': sum((int(row['virtual_access']) for row in rows)), 'virtual_access_targets': [{'target_id': row['target_id'], 'target_class': row['target_class'], 'floor_id': row['floor_id'], 'component_id': row['component_id'], 'virtual_access_distance': row['virtual_access_distance']} for row in rows if row['virtual_access']], 'projected_target_count': len(projected), 'maximum_projection_distance': max((row['projection_distance'] for row in projected), default=0.0), 'graph_component_count': len(members), 'cross_floor_connection_modeled': False, 'floors': floors}
        return (rows, summary)

    def _ensure_layer(document: Any, name: str, color: int, lineweight: int=18) -> None:
        if name not in document.layers:
            document.layers.add(name=name, color=color, lineweight=lineweight)

    def _write_review_dxf(path: Path, source_dxf: Path | None, graph: GraphBuilder, reachability_rows: list[dict[str, Any]]) -> None:
        if ezdxf is None:
            return
        if source_dxf and source_dxf.exists():
            document = ezdxf.readfile(source_dxf)
        else:
            document = ezdxf.new('R2010')
        _ensure_layer(document, 'RG_NAV_SKELETON_EDGE', 3, 18)
        _ensure_layer(document, 'RG_NAV_TARGET_EDGE', 6, 25)
        _ensure_layer(document, 'RG_NAV_AREA_EDGE', 4, 18)
        _ensure_layer(document, 'RG_NAV_NODE', 5, 18)
        _ensure_layer(document, 'RG_NAV_PORTAL_NODE', 2, 35)
        _ensure_layer(document, 'RG_TARGET_REACHABLE', 3, 25)
        _ensure_layer(document, 'RG_TARGET_UNREACHABLE', 1, 35)
        _ensure_layer(document, 'RG_TARGET_VIRTUAL_ACCESS', 2, 35)
        _ensure_layer(document, 'RG_REACHABILITY_LABEL', 7, 18)
        modelspace = document.modelspace()
        lengths = [float(edge.get('length', 0.0)) for edge in graph.edges if edge.get('length', 0.0) > 0]
        radius = max(80.0, min(500.0, (float(np.median(lengths)) if lengths else 500.0) * 0.08))
        for edge in graph.edges:
            kind = str(edge.get('kind', ''))
            if kind == 'virtual_target_access_edge':
                continue
            layer_name = 'RG_NAV_SKELETON_EDGE'
            if kind == 'target_access_edge':
                layer_name = 'RG_NAV_TARGET_EDGE'
            elif kind == 'area_anchor_access_edge':
                layer_name = 'RG_NAV_AREA_EDGE'
            coordinates = edge['geometry']['coordinates']
            if len(coordinates) >= 2:
                modelspace.add_lwpolyline(coordinates, dxfattribs={'layer': layer_name})
        for node in graph.nodes.values():
            if node.get('kind') == 'portal':
                modelspace.add_circle((node['x'], node['y']), radius * 1.4, dxfattribs={'layer': 'RG_NAV_PORTAL_NODE'})
            elif node.get('kind') == 'skeleton':
                modelspace.add_circle((node['x'], node['y']), radius * 0.22, dxfattribs={'layer': 'RG_NAV_NODE'})
            elif node.get('kind') == 'virtual_target_access':
                modelspace.add_circle((node['x'], node['y']), radius * 1.05, dxfattribs={'layer': 'RG_TARGET_VIRTUAL_ACCESS'})
                label_entity = modelspace.add_text(f"VIRTUAL ACCESS {node.get('target_id', '')}", dxfattribs={'layer': 'RG_REACHABILITY_LABEL', 'height': radius})
                label_entity.dxf.insert = (node['x'] + radius, node['y'] + radius)
        reachability_by_target = {row['target_id']: row for row in reachability_rows}
        for node in graph.nodes.values():
            if node.get('kind') != 'target':
                continue
            row = reachability_by_target.get(str(node.get('target_id')), {})
            reachable = bool(row.get('attached'))
            layer_name = 'RG_TARGET_REACHABLE' if reachable else 'RG_TARGET_UNREACHABLE'
            modelspace.add_circle((node['x'], node['y']), radius * 0.75, dxfattribs={'layer': layer_name})
            if not reachable:
                label_entity = modelspace.add_text(f"UNREACHABLE {node.get('target_id', '')}", dxfattribs={'layer': 'RG_REACHABILITY_LABEL', 'height': radius * 1.2})
                label_entity.dxf.insert = (node['x'] + radius, node['y'] + radius)
        path.parent.mkdir(parents=True, exist_ok=True)
        document.saveas(path)

    def _format_rate(value: float) -> str:
        return f'{value * 100:.2f}%'

    def _write_report(path: Path, summary: dict[str, Any], run_stats: dict[str, Any]) -> None:
        reachability = summary['reachability']
        graph_stats = summary['graph']
        floor_lines = []
        for floor_id, row in reachability['floors'].items():
            floor_lines.append(f"| {floor_id} | {row['target_count']} | {row['attached_target_count']} | {_format_rate(row['attachment_rate'])} | {row['target_graph_component_count']} | {_format_rate(row['primary_component_coverage_rate'])} | {_format_rate(row['pairwise_target_reachability_rate'])} |")
        virtual_lines = [f"- `{row['target_id']}`，类别 `{row['target_class']}`，楼层 `{row['floor_id']}`，虚拟访问距离 {row['virtual_access_distance']:.3f} CAD 单位。" for row in reachability.get('virtual_access_targets', [])] or ['- 无，所有目标均直接接入自由空间导航骨架。']
        unattached_lines = [f"- `{row['target_id']}`，类别 `{row['target_class']}`，楼层 `{row['floor_id']}`。" for row in reachability.get('unattached_targets', [])] or ['- 无。']
        report = f"# AreaGraph 规则导航图与同楼层可达性研究报告\n\n## 1. 研究结论\n\n- 本次只评价同楼层可达性，不再把 B1 与 B2 之间没有垂直连接计入可达率。\n- 共接入巡检对象 **{reachability['attached_target_count']} / {reachability['target_count']}**，同楼层接入率为 **{_format_rate(reachability['same_floor_attachment_rate'])}**。\n- 按两个楼层全部目标对加权计算，同楼层两两可达率为 **{_format_rate(reachability['same_floor_pairwise_target_reachability_rate'])}**。\n- 共生成 **{reachability['virtual_access_target_count']}** 个虚拟访问节点。虚拟节点位于同楼层最近的可导航自由空间，不使用穿越障碍物的直线连接原始目标。\n- 有 **{reachability['projected_target_count']}** 个目标原始坐标不在上游自由空间内，最大投影距离为 **{reachability['maximum_projection_distance']:.3f} CAD 单位**，应复核上游定位和障碍物边界。\n\n## 2. 严格防穿障碍物策略\n\n1. 只保留像素中心严格位于 `free_areas.geojson` 内的自由栅格，不使用障碍物容差缓冲。\n2. 中轴相邻像素之间的每条微小线段必须被原始自由空间多边形严格覆盖。\n3. 目标接入采用四邻域多源最短路，禁止从两个障碍物角点之间对角切过。\n4. 折线简化后再次执行严格矢量覆盖检查；不合格则退回未简化路径，仍不合格则不生成该边。\n5. 无法直接接入的目标生成独立虚拟访问节点，虚拟节点与最近的可导航骨架节点重合，不绘制穿障碍物的目标直连线。\n\n## 3. 导航图规模\n\n| 指标 | 数量 |\n|---|---:|\n| AreaGraph 区域锚点 | {graph_stats['area_anchor_count']} |\n| Portal 导航节点 | {graph_stats['portal_node_count']} |\n| 中轴骨架节点 | {graph_stats['skeleton_node_count']} |\n| 巡检对象节点 | {graph_stats['target_node_count']} |\n| 虚拟访问节点 | {graph_stats['virtual_access_node_count']} |\n| 全部导航节点 | {graph_stats['node_count']} |\n| 中轴导航边 | {graph_stats['skeleton_edge_count']} |\n| 目标直接接入边 | {graph_stats['target_access_edge_count']} |\n| 虚拟访问边 | {graph_stats['virtual_access_edge_count']} |\n| 全部导航边 | {graph_stats['edge_count']} |\n\n## 4. 分楼层可达率\n\n| 楼层 | 目标数 | 已接入 | 接入率 | 目标连通分量数 | 主分量覆盖率 | 两两可达率 |\n|---|---:|---:|---:|---:|---:|---:|\n{chr(10).join(floor_lines)}\n\n- **接入率**：目标是否具有同楼层的直接接入节点或虚拟访问节点。\n- **主分量覆盖率**：该楼层最大目标连通分量中的目标比例。\n- **两两可达率**：只在同一楼层内，任意两个目标落在同一导航连通分量的目标对比例。\n\n## 5. 虚拟访问节点\n\n{chr(10).join(virtual_lines)}\n\n虚拟访问距离是原始目标坐标到最近可导航自由空间节点的距离。该线仅用于审核偏移量，不作为可行走导航边输出。\n\n## 6. 仍未接入目标\n\n{chr(10).join(unattached_lines)}\n\n## 7. 结论边界\n\n本报告验证的是点目标在当前 CAD 自由空间模型中的几何拓扑可达性。当前仍未加入机器人半径、转弯半径和门宽余量；加入机器人参数后，应先膨胀障碍物，再重新生成严格自由空间骨架。\n\n## 8. 运行信息\n\n- 自由空间分量处理数：{run_stats['processed_component_count']}\n- 严格自由空间骨架像素：{run_stats['skeleton_pixel_count']}\n- 运行耗时：{summary['elapsed_seconds']:.3f} 秒\n- DXF 重点图层：`RG_NAV_SKELETON_EDGE`、`RG_NAV_TARGET_EDGE`、`RG_TARGET_VIRTUAL_ACCESS`、`RG_TARGET_REACHABLE`、`RG_TARGET_UNREACHABLE`\n"
        path.write_text(report, encoding='utf-8')

    def build_navigation_graph(run_dir: Path, output_dir: Path | None=None, source_dxf: Path | None=None, config: NavConfig | None=None, write_review_dxf: bool=True) -> dict[str, Any]:
        config = config or NavConfig()
        started = time.perf_counter()
        run_dir = Path(run_dir).resolve()
        output_dir = Path(output_dir or run_dir / 'area_graph_navigation').resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        free_path = run_dir / 'navigation_graph' / 'inputs' / 'free_areas.geojson'
        target_path = run_dir / 'navigation_graph' / 'inputs' / 'navigation_targets.geojson'
        area_dir = run_dir / 'area_graph'
        regions_path = area_dir / 'area_graph_regions.geojson'
        portals_path = area_dir / 'portal_candidates.geojson'
        area_summary_path = area_dir / 'area_graph_summary.json'
        for required in (free_path, target_path, regions_path, portals_path, area_summary_path):
            if not required.exists():
                raise FileNotFoundError(f'Required upstream output is missing: {required}')
        area_summary = _read_json(area_summary_path)
        if source_dxf is None:
            raw_source = str(area_summary.get('source_dxf') or '')
            source_dxf = Path(raw_source) if raw_source and Path(raw_source).exists() else None
        pixel_size_by_floor = {str(key): float(value) for key, value in area_summary['pixel_size_by_floor'].items()}
        free_by_floor = _load_floor_geometries(free_path)
        components = _component_catalog(free_by_floor)
        regions, regions_by_component = _load_regions(regions_path)
        portals_by_component = _load_accepted_portals(portals_path)
        targets = _load_targets(target_path)
        targets_by_component = _assign_targets_to_components(targets, components)
        graph = GraphBuilder()
        stats_rows: list[dict[str, Any]] = []
        for floor_id, floor_components in components.items():
            pixel_size = pixel_size_by_floor[floor_id]
            for component_id, polygon in floor_components:
                component_targets = targets_by_component.get((floor_id, component_id), [])
                component_regions = regions_by_component.get((floor_id, component_id), [])
                component_portals = portals_by_component.get((floor_id, component_id), [])
                if polygon.area < pixel_size * pixel_size * config.minimum_component_pixels:
                    if not component_targets and (not component_regions):
                        continue
                print(f'[nav-graph] floor={floor_id} component={component_id} targets={len(component_targets)} regions={len(component_regions)}', flush=True)
                component_started = time.perf_counter()
                frame = _strict_free_space_frame(_rasterize_polygon(floor_id, component_id, polygon, pixel_size))
                node_by_pixel, key_pixels, skeleton_stats = _trace_skeleton(frame, component_portals, component_regions, graph, config)
                target_terminals: list[dict[str, Any]] = []
                for target in component_targets:
                    access_point: Point = target['access_point']
                    target_terminals.append({'node_id': f"TARGET::{target['target_id']}", 'kind': 'target', 'point': access_point, 'target_id': target['target_id'], 'target_class': target.get('target_class', ''), 'source_object_id': target.get('source_object_id', ''), 'area_id': _point_region(access_point, component_regions), 'assignment_status': target.get('assignment_status', ''), 'projection_distance': target.get('projection_distance', 0.0), 'raw_x': target['raw_point'].x, 'raw_y': target['raw_point'].y})
                area_terminals: list[dict[str, Any]] = []
                if config.include_area_anchors:
                    for region in component_regions:
                        point = region['geometry_object'].representative_point()
                        area_terminals.append({'node_id': f"AREA::{region['area_id']}", 'kind': 'area_anchor', 'point': point, 'area_id': region['area_id']})
                _attach_terminals(frame, node_by_pixel, key_pixels, target_terminals + area_terminals, graph, config)
                target_attached = sum((bool(graph.adjacency.get(row['node_id'])) for row in target_terminals))
                area_attached = sum((bool(graph.adjacency.get(row['node_id'])) for row in area_terminals))
                stats_rows.append({'floor_id': floor_id, 'component_id': component_id, 'pixel_size': pixel_size, 'free_pixels': int(np.count_nonzero(frame.mask)), **skeleton_stats, 'target_count': len(target_terminals), 'target_attached': target_attached, 'area_count': len(area_terminals), 'area_attached': area_attached, 'elapsed_seconds': round(time.perf_counter() - component_started, 6)})
        virtual_access_rows = _add_virtual_access_nodes(graph, targets)
        reachability_rows, reachability_summary = _analyze_reachability(graph, targets)
        edge_kind_counts = Counter((str(edge.get('kind', '')) for edge in graph.edges))
        node_kind_counts = Counter((str(node.get('kind', '')) for node in graph.nodes.values()))
        graph_summary = {'node_count': len(graph.nodes), 'edge_count': len(graph.edges), 'skeleton_node_count': node_kind_counts.get('skeleton', 0), 'portal_node_count': node_kind_counts.get('portal', 0), 'target_node_count': node_kind_counts.get('target', 0), 'area_anchor_count': node_kind_counts.get('area_anchor', 0), 'virtual_access_node_count': node_kind_counts.get('virtual_target_access', 0), 'skeleton_edge_count': edge_kind_counts.get('skeleton_edge', 0), 'target_access_edge_count': edge_kind_counts.get('target_access_edge', 0), 'area_access_edge_count': edge_kind_counts.get('area_anchor_access_edge', 0), 'virtual_access_edge_count': edge_kind_counts.get('virtual_target_access_edge', 0), 'node_kind_counts': dict(node_kind_counts), 'edge_kind_counts': dict(edge_kind_counts)}
        outputs = {'graph_json': output_dir / 'rule_navigation_graph.json', 'nodes_csv': output_dir / 'navigation_nodes.csv', 'edges_csv': output_dir / 'navigation_edges.csv', 'reachability_csv': output_dir / 'target_reachability.csv', 'virtual_access_csv': output_dir / 'virtual_access_nodes.csv', 'summary_json': output_dir / 'reachability_summary.json', 'report_md': output_dir / 'reachability_research_report.md', 'review_dxf': output_dir / 'rule_navigation_review.dxf'}
        graph_payload = {'graph_type': 'area_graph_medial_axis_navigation', 'directed': False, 'nodes': list(graph.nodes.values()), 'edges': graph.edges}
        _write_json(outputs['graph_json'], graph_payload)
        _write_csv(outputs['nodes_csv'], list(graph.nodes.values()), ['node_id', 'kind', 'floor_id', 'component_id', 'area_id', 'portal_id', 'target_id', 'target_class', 'access_node_id', 'virtual_access', 'virtual_access_distance', 'x', 'y', 'raw_x', 'raw_y', 'projection_distance', 'raster_snap_distance'])
        edge_csv_rows = [{**edge, 'geometry': json.dumps(edge['geometry'], ensure_ascii=False)} for edge in graph.edges]
        _write_csv(outputs['edges_csv'], edge_csv_rows, ['edge_id', 'node_a', 'node_b', 'kind', 'floor_id', 'component_id', 'length', 'vector_valid_with_raster_tolerance', 'geometry'])
        _write_csv(outputs['reachability_csv'], reachability_rows, ['target_id', 'target_class', 'floor_id', 'component_id', 'area_id', 'assignment_status', 'projection_distance', 'attached', 'access_node_id', 'virtual_access', 'virtual_access_distance', 'graph_component', 'reachable_to_floor_primary', 'reason'])
        _write_csv(outputs['virtual_access_csv'], virtual_access_rows, ['target_id', 'floor_id', 'virtual_node_id', 'navigation_node_id', 'distance'])
        run_stats = {'processed_component_count': len(stats_rows), 'skeleton_pixel_count': sum((row['skeleton_pixels'] for row in stats_rows)), 'components': stats_rows}
        summary = {'run_dir': str(run_dir), 'free_area_path': str(free_path), 'target_path': str(target_path), 'area_graph_path': str(area_dir / 'area_graph.json'), 'source_dxf': str(source_dxf) if source_dxf else '', 'graph': graph_summary, 'reachability': reachability_summary, 'run_stats': run_stats, 'elapsed_seconds': 0.0, 'outputs': {key: str(value) if key != 'review_dxf' or write_review_dxf else '' for key, value in outputs.items()}}
        if write_review_dxf:
            print('[nav-graph] writing review DXF', flush=True)
            _write_review_dxf(outputs['review_dxf'], source_dxf, graph, reachability_rows)
        summary['elapsed_seconds'] = round(time.perf_counter() - started, 6)
        _write_json(outputs['summary_json'], summary)
        _write_report(outputs['report_md'], summary, run_stats)
        return summary
    return dict(locals())

_s06_navigation = _register_embedded_module(
    'fire_inspection_system.area_graph_navigation',
    _build_s06_navigation(),
    aliases=('area_graph_navigation',),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/navigation_pipeline.py
# -----------------------------------------------------------------------------
def _build_s06_pipeline():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/navigation_pipeline.py'
    )
    __name__ = 'fire_inspection_system.navigation_pipeline'
    __package__ = 'fire_inspection_system'
    import hashlib
    from dataclasses import dataclass
    from pathlib import Path
    from typing import Any, Callable, Iterable
    from area_graph_navigation import NavConfig, build_navigation_graph
    from navigation_input_adapter import NavigationInputResult, prepare_navigation_inputs
    from portal_area_graph import BuildConfig as AreaGraphBuildConfig
    from portal_area_graph import build_portal_area_graph

    @dataclass(frozen=True)
    class RuleNavigationPipelineResult:
        inputs: NavigationInputResult
        portal_area_graph: dict[str, Any]
        area_graph_navigation: dict[str, Any]
        final_review_dxf: Path | None

        def to_dict(self) -> dict[str, Any]:
            return {
                'inputs': self.inputs.to_dict(),
                'portal_area_graph': self.portal_area_graph,
                'area_graph_navigation': self.area_graph_navigation,
                'final_review_dxf': (
                    str(self.final_review_dxf) if self.final_review_dxf else ''
                ),
            }

    def _require_nonempty_file(path: Path | str, description: str) -> Path:
        result = Path(path).resolve()
        if not result.is_file() or result.stat().st_size <= 0:
            raise FileNotFoundError(f'{description}不存在或为空: {result}')
        return result

    def build_rule_navigation_pipeline(
        *,
        run_dir: Path | str,
        sheets_json: Path | str,
        obstacle_union_geojsons: Iterable[Path | str],
        inspection_output_dir: Path | str,
        expected_obstacle_count: int,
        expected_target_count: int,
        source_dxf: Path | str | None = None,
        area_graph_config: AreaGraphBuildConfig | None = None,
        navigation_config: NavConfig | None = None,
        progress: Callable[[str], None] | None = print,
    ) -> RuleNavigationPipelineResult:
        root = Path(run_dir).resolve()
        source_path = Path(source_dxf).resolve() if source_dxf else None
        input_dir = root / 'navigation_graph' / 'inputs'

        if progress:
            progress('  [6.1/6.3] 复用楼层、障碍物和巡检对象结果，准备导航输入')
        inputs = prepare_navigation_inputs(
            sheets_json=sheets_json,
            obstacle_union_geojsons=obstacle_union_geojsons,
            inspection_output_dir=inspection_output_dir,
            output_dir=input_dir,
            write_debug_dxf=False,
            expected_obstacle_count=expected_obstacle_count,
            expected_target_count=expected_target_count,
        )
        _require_nonempty_file(inputs.free_area_geojson, '自由空间 GeoJSON')
        _require_nonempty_file(inputs.obstacle_union_geojson, '障碍物并集 GeoJSON')
        _require_nonempty_file(inputs.targets_geojson, '巡检目标 GeoJSON')

        if progress:
            progress('  [6.2/6.3] 构建 Portal AreaGraph（不写中间审阅 DXF）')
        portal_summary = build_portal_area_graph(
            free_area_path=inputs.free_area_geojson,
            obstacle_path=inputs.obstacle_union_geojson,
            output_dir=root / 'area_graph',
            source_dxf=source_path,
            config=area_graph_config or AreaGraphBuildConfig(),
            write_review_dxf=False,
        )
        _require_nonempty_file(
            portal_summary['outputs']['area_graph_json'],
            'Portal AreaGraph',
        )

        if progress:
            progress('  [6.3/6.3] 构建中轴规则导航图（不写中间审阅 DXF）')
        navigation_summary = build_navigation_graph(
            root,
            output_dir=root / 'area_graph_navigation',
            source_dxf=None,
            config=navigation_config or NavConfig(),
            write_review_dxf=False,
        )
        _require_nonempty_file(
            navigation_summary['outputs']['graph_json'],
            '中轴规则导航图',
        )
        return RuleNavigationPipelineResult(
            inputs=inputs,
            portal_area_graph=portal_summary,
            area_graph_navigation=navigation_summary,
            final_review_dxf=None,
        )




    return dict(locals())

_s06_pipeline = _register_embedded_module(
    'fire_inspection_system.navigation_pipeline',
    _build_s06_pipeline(),
    aliases=('navigation_pipeline',),
)

# === CONSOLIDATED PUBLIC API ===
from pathlib import Path
from typing import Any


def run_stage(
    *,
    run_dir: Path,
    input_dxf: Path,
    sheets_json: Path,
    obstacle_union_geojsons: list[Path],
    expected_obstacle_count: int,
    expected_target_count: int,
    area_graph_pixel_size: float,
    area_graph_max_raster_side: int,
    area_graph_max_raster_pixels: int,
    area_graph_minimum_bottleneck_score: float,
    area_graph_maximum_portals_per_floor: int,
    include_area_anchors: bool,
) -> Any:
    return _s06_pipeline.build_rule_navigation_pipeline(
        run_dir=run_dir,
        sheets_json=sheets_json,
        obstacle_union_geojsons=obstacle_union_geojsons,
        inspection_output_dir=run_dir / "inspection_objects",
        expected_obstacle_count=expected_obstacle_count,
        expected_target_count=expected_target_count,
        source_dxf=input_dxf,
        area_graph_config=_s06_area_graph.BuildConfig(
            pixel_size=area_graph_pixel_size,
            max_raster_side=area_graph_max_raster_side,
            max_raster_pixels=area_graph_max_raster_pixels,
            minimum_bottleneck_score=area_graph_minimum_bottleneck_score,
            max_candidates_per_floor=area_graph_maximum_portals_per_floor,
        ),
        navigation_config=_s06_navigation.NavConfig(
            include_area_anchors=include_area_anchors
        ),
    )


__all__ = ["run_stage"]
