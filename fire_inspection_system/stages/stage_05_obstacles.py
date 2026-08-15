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

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/obstacle_recognition_door_mask_fixed.py
# -----------------------------------------------------------------------------
def _build_s05_obstacles():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/obstacle_recognition_door_mask_fixed.py'
    )
    __name__ = 'fire_inspection_system.obstacle_recognition_door_mask_fixed'
    __package__ = 'fire_inspection_system'
    import csv
    import hashlib
    import http.client
    import json
    import math
    import os
    import re
    import sys
    import time
    import urllib.error
    import urllib.request
    import warnings
    from collections import Counter, defaultdict
    from dataclasses import dataclass
    from numbers import Integral
    from pathlib import Path
    from statistics import median
    from typing import Any
    import ezdxf
    from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Point, Polygon, box, mapping
    from shapely.ops import polygonize, unary_union
    from shapely.strtree import STRtree
    try:
        from shapely.validation import make_valid as shapely_make_valid
    except Exception:
        shapely_make_valid = None
    try:
        from scipy.spatial import cKDTree
    except Exception:
        cKDTree = None
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    SCRIPTS_DIR = PROJECT_ROOT / 'scripts'
    LLM_CONFIG_PATH = PROJECT_ROOT / 'fire_inspection_system' / 'configs' / 'llm_api.json'
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        from mark_inspection_objects_dxf import timestamped_output_path
    except Exception:

        def timestamped_output_path(path: Path) -> Path:
            return path.with_name(f"{path.stem}_{time.strftime('%Y%m%d%H%M%S')}{path.suffix}")
    GEOMETRY_FILE = 'cad_geometry_inventory.csv'
    LAYER_LLM_FILE = 'obstacle_layer_llm_decisions.json'
    OBSTACLE_CSV_FILE = 'floor_obstacles.csv'
    RESULT_JSON_FILE = 'floor_obstacle_recognition_result.json'
    LAYER_LLM_PROMPT_VERSION = 'obstacle-layer-v6-facade-drawing-negative'
    LAYER_LLM_BATCH_SIZE = 20
    LAYER_LLM_MAX_RETRIES = 3
    WALL_LAYER = 'CHECK_OBSTACLE_WALL'
    COLUMN_LAYER = 'CHECK_OBSTACLE_COLUMN'
    FILL_LAYER = 'CHECK_OBSTACLE_FILL'
    TEXT_LAYER = 'CHECK_OBSTACLE_TEXT'
    TOPOLOGY_LAYER = 'CHECK_OBSTACLE_TOPOLOGY'

    @dataclass(frozen=True)
    class ObstacleConfig:
        """Scale-invariant controls for obstacle review geometry."""
        min_area: float = 0.0
        parallel_angle_tolerance_deg: float = 5.0
        min_projection_overlap_ratio: float = 0.05
        max_wall_width_to_overlap_ratio: float = 0.6
        wall_width_model_direction_tolerance_deg: float = 10.0
        wall_width_model_min_samples: int = 3
        wall_width_model_max_neighbors: int = 24
        wall_width_model_local_radius_ratio: float = 8.0
        wall_width_model_outlier_ratio: float = 3.0
        wall_width_model_sparse_outlier_ratio: float = 4.0
        wall_width_model_mad_multiplier: float = 6.0
        wall_width_model_similar_ratio: float = 1.5
        wall_width_model_similar_min_support: int = 3
        wall_width_model_similar_min_fraction: float = 0.25
        wall_width_model_direction_bucket_deg: float = 5.0
        wall_width_model_use_spatial_index: bool = True
        wall_width_model_shadow_compare: bool = False
        periodic_parallel_rejection_enabled: bool = True
        periodic_parallel_min_line_count: int = 5
        periodic_parallel_angle_tolerance_deg: float = 1.0
        periodic_parallel_pitch_relative_tolerance: float = 0.05
        periodic_parallel_min_projection_overlap_ratio: float = 0.8
        regional_wall_processing_enabled: bool = True
        regional_candidate_halo_multiplier: float = 1.25
        regional_topology_halo_width_ratio: float = 3.0
        topology_completion_enabled: bool = True
        topology_polygon_min_core_overlap_ratio: float = 0.5
        topology_perpendicular_tolerance_deg: float = 10.0
        topology_endpoint_support_width_ratio: float = 1.1
        topology_cap_search_tolerance_width_ratio: float = 0.02
        topology_cap_min_crossing_ratio: float = 0.75
        topology_max_cap_tail_width_ratio: float = 1.5
        topology_line_snap_max_tolerance: float = 0.001

    @dataclass(frozen=True)
    class ObstacleRecognitionResult:
        result_json: Path
        obstacle_csv: Path
        marked_dxf: Path | None
        obstacle_count: int
        obstacle_type_count: int
        region_count: int
        per_region_geojsons: list[Path]
        union_geojsons: list[Path]

    def safe_float(value: Any) -> float | None:
        try:
            number = float(value)
        except Exception:
            return None
        return number if math.isfinite(number) else None

    def write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def read_json(path: Path) -> dict[str, Any]:
        with path.open('r', encoding='utf-8') as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}

    def clean_layer_name(value: Any) -> str:
        return re.sub('[\\s_\\-()（）\\[\\]【】/\\\\]+', '', str(value or '')).upper()

    def layer_is_forced_non_obstacle(layer: Any) -> bool:
        """Reject generic facade drawing layers from physical wall geometry."""
        normalized = clean_layer_name(layer)
        return bool(re.fullmatch(r'FACADE\d*', normalized))

    def apply_deterministic_layer_overrides(
        decisions: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        for layer, raw_decision in list(decisions.items()):
            if not layer_is_forced_non_obstacle(layer):
                continue
            original = dict(raw_decision or {})
            decisions[layer] = {
                'layer': layer,
                'role': 'not_obstacle',
                'candidate_types': [],
                'confidence': 1.0,
                'reason': 'generic facade drawing layer is not floor-plan wall geometry',
                'source': 'deterministic_negative_layer_rule',
                'model': str(original.get('model') or ''),
                'llm_returned': bool(original.get('llm_returned')),
                'overrode_llm_role': str(original.get('role') or ''),
                'overrode_llm_candidate_types': list(original.get('candidate_types') or []),
                'overrode_llm_reason': str(original.get('reason') or ''),
            }
        return decisions

    def bbox_from_row(row: dict[str, str]) -> tuple[float, float, float, float] | None:
        minx = safe_float(row.get('bbox_minx'))
        miny = safe_float(row.get('bbox_miny'))
        maxx = safe_float(row.get('bbox_maxx'))
        maxy = safe_float(row.get('bbox_maxy'))
        if None in (minx, miny, maxx, maxy):
            return None
        if maxx <= minx or maxy <= miny:
            return None
        return (float(minx), float(miny), float(maxx), float(maxy))

    def bbox_center(bounds: tuple[float, float, float, float]) -> tuple[float, float]:
        minx, miny, maxx, maxy = bounds
        return ((minx + maxx) / 2.0, (miny + maxy) / 2.0)

    def bbox_intersects(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
        return max(a[0], b[0]) <= min(a[2], b[2]) and max(a[1], b[1]) <= min(a[3], b[3])

    def parse_geometry_json(row: dict[str, str]) -> dict[str, Any]:
        raw = row.get('geometry_json') or ''
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def polygon_from_points(points: Any, *, min_area: float) -> Polygon | None:
        if not isinstance(points, list) or len(points) < 4:
            return None
        coords: list[tuple[float, float]] = []
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            x = safe_float(point[0])
            y = safe_float(point[1])
            if x is not None and y is not None:
                coords.append((x, y))
        if len(coords) < 4:
            return None
        try:
            poly = Polygon(coords)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty or poly.area <= min_area:
                return None
            return poly
        except Exception:
            return None

    def polygons_from_row(row: dict[str, str], config: ObstacleConfig) -> list[Polygon]:
        payload = parse_geometry_json(row)
        polygons: list[Polygon] = []
        for points in payload.get('polygons', []) or []:
            poly = polygon_from_points(points, min_area=config.min_area)
            if poly is not None:
                polygons.append(poly)
        if polygons:
            return polygons
        entity_type = str(row.get('entity_type') or '').upper()
        bounds = bbox_from_row(row)
        if bounds and entity_type in {'CIRCLE', 'REGION', 'SOLID'}:
            return [box(*bounds)]
        return []

    def lines_from_row(row: dict[str, str], config: ObstacleConfig, *, min_length: float | None=None) -> list[LineString]:
        payload = parse_geometry_json(row)
        lines: list[LineString] = []
        threshold = 0.0 if min_length is None else min_length
        for item in payload.get('lines', []) or []:
            if not isinstance(item, list) or len(item) < 2:
                continue
            coords: list[tuple[float, float]] = []
            for point in item:
                if not isinstance(point, (list, tuple)) or len(point) < 2:
                    continue
                x = safe_float(point[0])
                y = safe_float(point[1])
                if x is not None and y is not None:
                    coords.append((x, y))
            if len(coords) < 2:
                continue
            try:
                line = LineString(coords)
                if not line.is_empty and line.length > 0 and (line.length >= threshold):
                    lines.append(line)
            except Exception:
                continue
        return lines

    def load_geometry_rows(inventory_dir: Path) -> list[dict[str, str]]:
        path = inventory_dir / GEOMETRY_FILE
        if not path.exists():
            raise FileNotFoundError(path)
        with path.open('r', encoding='utf-8-sig', newline='') as handle:
            return [dict(row) for row in csv.DictReader(handle)]

    def _scope_token(value: Any, fallback: str) -> str:
        token = re.sub('[^0-9A-Za-z_.-]+', '_', str(value or '').strip()).strip('_')
        return token or fallback

    def _building_scope_id(source_floor_id: str, building: dict[str, Any]) -> str:
        connection_group = (
            building.get('connected_building_group_id')
            or building.get('building_connection_group')
            or building.get('corridor_connection_group')
        )
        verification = str(
            building.get('corridor_connection_verification')
            or building.get('connection_verification_status')
            or ''
        ).strip().lower()
        trusted_connection = bool(
            connection_group
            and building.get('corridor_connection_verified') is True
            and verification in {'manual_verified', 'cad_portal_verified', 'door_portal_verified'}
        )
        if trusted_connection:
            return f"{source_floor_id}__CONN_{_scope_token(connection_group, 'GROUP')}"
        return f"{source_floor_id}__{_scope_token(building.get('building_id'), 'B00')}"

    def _building_polygon(building: dict[str, Any], inspection_polygon: Any) -> Any | None:
        polygons: list[Any] = []
        raw_parts = building.get('structural_parts') or building.get('parts')
        if isinstance(raw_parts, list):
            for raw_ring in raw_parts:
                if not isinstance(raw_ring, list) or len(raw_ring) < 3:
                    continue
                try:
                    points = [(float(point[0]), float(point[1])) for point in raw_ring if isinstance(point, (list, tuple)) and len(point) >= 2]
                    candidate = Polygon(points) if len(points) >= 3 else None
                    if candidate is not None and not candidate.is_valid:
                        candidate = candidate.buffer(0)
                    if candidate is not None and not candidate.is_empty and candidate.area > 0:
                        polygons.append(candidate)
                except Exception:
                    continue
        raw_polygon = building.get('polygon')
        if not polygons and isinstance(raw_polygon, list) and len(raw_polygon) >= 3:
            try:
                points = [(float(point[0]), float(point[1])) for point in raw_polygon if isinstance(point, (list, tuple)) and len(point) >= 2]
                candidate = Polygon(points) if len(points) >= 3 else None
                if candidate is not None and not candidate.is_valid:
                    candidate = candidate.buffer(0)
                if candidate is not None and not candidate.is_empty and candidate.area > 0:
                    polygons.append(candidate)
            except Exception:
                pass
        raw_bbox = building.get('bbox')
        if not polygons and isinstance(raw_bbox, (list, tuple)) and len(raw_bbox) == 4:
            try:
                candidate = box(*(float(value) for value in raw_bbox))
                if candidate.area > 0:
                    polygons.append(candidate)
            except Exception:
                pass
        if not polygons:
            return None
        try:
            geometry = unary_union(polygons).intersection(inspection_polygon)
            if not geometry.is_valid:
                geometry = geometry.buffer(0)
            return geometry if not geometry.is_empty and geometry.area > 0 else None
        except Exception:
            return None

    def load_floor_regions(sheets_json: Path) -> list[dict[str, Any]]:
        """Load CAD analysis domains, split by building whenever validation exists."""
        payload = read_json(sheets_json)
        regions: list[dict[str, Any]] = []
        for sheet in payload.get('sheets', []) or []:
            if not isinstance(sheet, dict) or not sheet.get('path_planning_usable'):
                continue
            sheet_id = str(sheet.get('sheet_id') or '')
            source_floor_id = str(sheet.get('floor_id') or '')
            floor_name = str(sheet.get('floor_name') or source_floor_id or sheet_id)
            inspection_regions = sheet.get('inspection_regions') or []
            if not inspection_regions and sheet.get('inspection_region_bbox'):
                inspection_regions = [{'region_id': 'R01', 'bbox': sheet.get('inspection_region_bbox')}]
            for index, item in enumerate(inspection_regions, start=1):
                if not isinstance(item, dict):
                    continue
                source = str(item.get('source') or sheet.get('inspection_region_source') or '')
                if source in {'sheet_bbox', 'sheet_bbox_fallback'} or source.startswith('sheet_bbox'):
                    continue
                raw_bbox = item.get('bbox')
                if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4:
                    continue
                bbox_tuple = tuple(float(value) for value in raw_bbox)
                if bbox_tuple[2] <= bbox_tuple[0] or bbox_tuple[3] <= bbox_tuple[1]:
                    continue
                region_id = str(item.get('region_id') or f'R{index:02d}')
                confidence = safe_float(item.get('confidence'))
                if confidence is None:
                    confidence = safe_float(sheet.get('inspection_region_confidence')) or 0.0
                inspection_polygon = box(*bbox_tuple)
                raw_buildings = item.get('building_regions')
                if not isinstance(raw_buildings, list) and len(inspection_regions) == 1:
                    raw_buildings = sheet.get('building_regions')
                detection_meta = item.get('building_region_detection')
                if isinstance(raw_buildings, list) and raw_buildings and isinstance(detection_meta, dict):
                    detection_status = str(detection_meta.get('status') or '').strip().lower()
                    if detection_status == 'error':
                        raw_buildings = []
                building_regions: list[dict[str, Any]] = []
                if isinstance(raw_buildings, list):
                    for building_index, building in enumerate(raw_buildings, start=1):
                        if not isinstance(building, dict):
                            continue
                        polygon = _building_polygon(building, inspection_polygon)
                        if polygon is None:
                            continue
                        building_id = _scope_token(building.get('building_id'), f'B{building_index:02d}')
                        planning_scope_id = _building_scope_id(source_floor_id, building)
                        child_region_id = f'{region_id}__{building_id}'
                        building_regions.append({
                            'sheet_id': sheet_id,
                            'floor_id': planning_scope_id,
                            'source_floor_id': source_floor_id,
                            'building_scope_id': planning_scope_id,
                            'building_id': building_id,
                            'building_index': int(building.get('building_index') or building_index),
                            'building_name': str(building.get('name_text') or building.get('label_hint') or building_id),
                            'floor_name': floor_name,
                            'region_id': child_region_id,
                            'parent_region_id': region_id,
                            'full_region_id': f'{sheet_id}:{child_region_id}',
                            'bbox': tuple(float(value) for value in polygon.bounds),
                            'polygon': polygon,
                            'region_source': str(building.get('source') or 'building_region'),
                            'region_confidence': safe_float(building.get('confidence')) or confidence,
                            'region_evidence': f"building_region={building_id}; validation={building.get('validation_method') or 'unknown'}",
                            'building_region_needs_review': bool(
                                building.get('needs_review')
                                or (detection_meta or {}).get('needs_review')
                            ),
                            'building_region_isolation_only': str(
                                (detection_meta or {}).get('validation_status') or ''
                            ).strip() not in {'vector_validated', 'manual_verified'},
                            'corridor_connection_group': str(
                                building.get('corridor_connection_group') or ''
                            ),
                            'corridor_connection_verified': bool(
                                building.get('corridor_connection_verified')
                            ),
                            'corridor_connection_verification': str(
                                building.get('corridor_connection_verification') or ''
                            ),
                        })
                if building_regions:
                    regions.extend(building_regions)
                else:
                    regions.append({'sheet_id': sheet_id, 'floor_id': source_floor_id, 'source_floor_id': source_floor_id, 'building_scope_id': source_floor_id, 'building_id': '', 'building_index': 0, 'building_name': '', 'floor_name': floor_name, 'region_id': region_id, 'parent_region_id': region_id, 'full_region_id': f'{sheet_id}:{region_id}', 'bbox': bbox_tuple, 'polygon': inspection_polygon, 'region_source': source, 'region_confidence': confidence, 'region_evidence': str(item.get('evidence') or sheet.get('inspection_region_evidence') or ''), 'building_region_needs_review': False})
        return regions

    def layer_summary(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
        summaries: dict[str, dict[str, Any]] = {}
        for row in rows:
            layer = str(row.get('layer') or '')
            if not layer:
                continue
            item = summaries.setdefault(layer, {'layer': layer, 'normalized_layer': clean_layer_name(layer), 'entity_types': Counter(), 'geometry_kinds': Counter(), 'sample_blocks': Counter(), 'sample_texts': Counter()})
            item['entity_types'][str(row.get('entity_type') or '')] += 1
            item['geometry_kinds'][str(row.get('geometry_kind') or '')] += 1
            block = str(row.get('parent_block_name') or '')
            text = str(row.get('norm_text') or row.get('raw_text') or '')
            if block:
                item['sample_blocks'][block[:80]] += 1
            if text:
                item['sample_texts'][text[:80]] += 1
        out: list[dict[str, Any]] = []
        for item in summaries.values():
            out.append({'layer': item['layer'], 'normalized_layer': item['normalized_layer'], 'entity_types': dict(item['entity_types'].most_common(10)), 'geometry_kinds': dict(item['geometry_kinds'].most_common(10)), 'sample_blocks': [key for key, _count in item['sample_blocks'].most_common(8)], 'sample_texts': [key for key, _count in item['sample_texts'].most_common(8)]})
        return sorted(out, key=lambda item: item['layer'])

    def parse_llm_json(text: str) -> dict[str, Any]:
        value = text.strip()
        if value.startswith('```'):
            value = re.sub('^```(?:json)?', '', value).strip()
            value = re.sub('```$', '', value).strip()
        try:
            payload = json.loads(value)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            match = re.search('\\{.*\\}', value, flags=re.S)
            if not match:
                return {}
            try:
                payload = json.loads(match.group(0))
                return payload if isinstance(payload, dict) else {}
            except Exception:
                return {}

    def llm_api_config_defaults() -> dict[str, str]:
        """Read the shared API Key/Base URL/model configuration."""
        if not LLM_CONFIG_PATH.is_file():
            return {}
        try:
            payload = json.loads(LLM_CONFIG_PATH.read_text(encoding='utf-8'))
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            key: str(payload.get(key) or '').strip()
            for key in ('api_key', 'base_url', 'model')
        }

    def llm_runtime_config() -> tuple[str, str, str]:
        defaults = llm_api_config_defaults()
        api_key = os.getenv('DEEPSEEK_API_KEY') or os.getenv('OPENAI_API_KEY') or defaults.get('api_key', '')
        base_url = os.getenv('DEEPSEEK_BASE_URL') or defaults.get('base_url') or 'https://api.deepseek.com'
        model = os.getenv('DEEPSEEK_MODEL') or defaults.get('model') or 'deepseek-v4-flash'
        if model.strip().lower() == 'deepseek-chat':
            model = 'deepseek-v4-flash'
        return (api_key, base_url, model)

    def request_layer_llm(batch: list[dict[str, Any]], *, api_key: str, base_url: str, model: str) -> list[dict[str, Any]]:
        prompt = {'task': 'CAD layer obstacle relevance classification for fire inspection route planning', 'prompt_version': LAYER_LLM_PROMPT_VERSION, 'important_policy': ['Only classify layer semantic relevance.', 'Return obstacle_candidate only when the layer name, block samples, or text samples clearly mean wall, column, curtain wall, structural wall, masonry/concrete wall, or structural filled obstacle.', 'Positive obstacle evidence includes: WALL, COL, COLUMN, PILLAR, CONC, CONCRETE, MASONRY, BRICK, SHEARWALL, CURTAIN WALL,WINDOW, GLZ/GLZE when it means curtain wall glazing, 墙, 柱, 幕墙, 砌体, 混凝土, 剪力墙.', 'Do not infer obstacle_candidate from elevator, stair, shaft, room, equipment,  door, opening, passage, ramp, furniture, annotation, or symbol semantics unless the same layer also explicitly contains wall/column evidence.', 'Dimension, axis, grid, title block, frame, legend, table, and text-only annotation layers are not obstacles.'], 'roles': {'obstacle_candidate': 'Layer explicitly means wall, column, curtain wall, masonry/concrete wall, or structural filled obstacle.', 'not_obstacle': 'Annotation, axis, dimension, title, frame, furniture, symbol, or unrelated layer.', 'unknown': 'Insufficient semantic evidence.'}, 'candidate_types': ['wall', 'column', 'filled_obstacle'], 'layers': batch, 'output_schema': {'decisions': [{'layer': 'original layer name', 'role': 'obstacle_candidate|not_obstacle|unknown', 'candidate_types': ['wall'], 'confidence': 0.0, 'reason': 'brief semantic reason'}]}}
        prompt['important_policy'].append(
            'Generic drawing layers named FACADE, FACADE-2, FACADE-4, or FACADE followed only by a number are facade representation layers and must be not_obstacle. Do not reinterpret them as exterior walls.'
        )
        body = {'model': model, 'messages': [{'role': 'system', 'content': 'You are a CAD semantics assistant. Return strict JSON only.'}, {'role': 'user', 'content': json.dumps(prompt, ensure_ascii=False)}], 'temperature': 0, 'enable_thinking': False, 'response_format': {'type': 'json_object'}}
        request = urllib.request.Request(base_url.rstrip('/') + '/chat/completions', data=json.dumps(body, ensure_ascii=False).encode('utf-8'), headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.loads(response.read().decode('utf-8'))
        content = payload['choices'][0]['message']['content']
        parsed = parse_llm_json(content)
        return [item for item in parsed.get('decisions', []) or [] if isinstance(item, dict)]
    LLM_REQUEST_ERRORS = (urllib.error.URLError, TimeoutError, http.client.IncompleteRead, ConnectionError, OSError, KeyError, ValueError, json.JSONDecodeError)

    def request_layer_llm_with_retry(batch: list[dict[str, Any]], *, api_key: str, base_url: str, model: str, max_retries: int=LAYER_LLM_MAX_RETRIES) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                return request_layer_llm(batch, api_key=api_key, base_url=base_url, model=model)
            except LLM_REQUEST_ERRORS as exc:
                last_error = exc
                if attempt >= max_retries:
                    break
                time.sleep(min(2.0 ** attempt, 8.0))
        assert last_error is not None
        raise last_error

    def request_layer_llm_resilient(batch: list[dict[str, Any]], *, api_key: str, base_url: str, model: str) -> list[dict[str, Any]]:
        try:
            return request_layer_llm_with_retry(batch, api_key=api_key, base_url=base_url, model=model)
        except LLM_REQUEST_ERRORS:
            if len(batch) <= 1:
                raise
            midpoint = max(1, len(batch) // 2)
            return request_layer_llm_resilient(batch[:midpoint], api_key=api_key, base_url=base_url, model=model) + request_layer_llm_resilient(batch[midpoint:], api_key=api_key, base_url=base_url, model=model)

    def load_layer_llm_cache(path: Path, model: str) -> dict[str, dict[str, Any]]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return {}
        if str(payload.get('model') or '') != model:
            return {}
        if str(payload.get('prompt_version') or '') != LAYER_LLM_PROMPT_VERSION:
            return {}
        cached = payload.get('decisions', {})
        if not isinstance(cached, dict):
            return {}
        return {str(layer): value for layer, value in cached.items() if isinstance(value, dict) and (value.get('llm_returned') is True or str(value.get('reason') or '') != 'LLM did not return this layer')}

    def write_layer_llm_output(path: Path, *, model: str, base_url: str, summaries: list[dict[str, Any]], decisions: dict[str, dict[str, Any]], complete: bool) -> None:
        view = dict(decisions)
        for summary in summaries:
            layer = summary['layer']
            view.setdefault(layer, {'layer': layer, 'role': 'unknown', 'candidate_types': [], 'confidence': 0.0, 'reason': 'LLM did not return this layer', 'source': 'llm_layer_semantic', 'model': model, 'llm_returned': False})
        output = {'model': model, 'base_url': base_url, 'prompt_version': LAYER_LLM_PROMPT_VERSION, 'strategy': 'llm_layer_area_geometry_plus_parallel_wall_line_bundles', 'complete': complete, 'layer_count': len(summaries), 'decided_layer_count': sum((1 for item in view.values() if item.get('llm_returned') is True)), 'role_counts': dict(Counter((item['role'] for item in view.values())).most_common()), 'candidate_type_counts': dict(Counter((kind for item in view.values() for kind in item.get('candidate_types', []))).most_common()), 'decisions': view, 'layer_summaries': summaries}
        write_json(path, output)

    def classify_layers_by_llm(rows: list[dict[str, str]], output_dir: Path) -> dict[str, dict[str, Any]]:
        api_key, base_url, model = llm_runtime_config()
        if not api_key:
            raise RuntimeError(
                f'障碍物图层判别必须调用 LLM，请在 {LLM_CONFIG_PATH} 中设置 api_key，'
                '或通过 main.py 的 --llm-api-key 参数临时传入。'
            )
        summaries = layer_summary(rows)
        cache_path = output_dir / LAYER_LLM_FILE
        decisions: dict[str, dict[str, Any]] = load_layer_llm_cache(cache_path, model)
        pending_summaries = [summary for summary in summaries if decisions.get(summary['layer'], {}).get('llm_returned') is not True]
        batch_size = LAYER_LLM_BATCH_SIZE
        for start in range(0, len(pending_summaries), batch_size):
            batch = pending_summaries[start:start + batch_size]
            try:
                raw_decisions = request_layer_llm_resilient(batch, api_key=api_key, base_url=base_url, model=model)
            except LLM_REQUEST_ERRORS as exc:
                write_layer_llm_output(cache_path, model=model, base_url=base_url, summaries=summaries, decisions=decisions, complete=False)
                raise RuntimeError(f'LLM 图层障碍物相关性判别失败，已缓存完成批次: {exc}') from exc
            for item in raw_decisions:
                layer = str(item.get('layer') or '')
                if not layer:
                    continue
                role = str(item.get('role') or 'unknown')
                candidate_types = [str(value) for value in item.get('candidate_types', []) or [] if str(value) in {'wall', 'column', 'filled_obstacle'}]
                if role != 'obstacle_candidate':
                    candidate_types = []
                decisions[layer] = {'layer': layer, 'role': role, 'candidate_types': candidate_types, 'confidence': float(item.get('confidence') or 0.0), 'reason': str(item.get('reason') or ''), 'source': 'llm_layer_semantic', 'model': model, 'llm_returned': True}
            write_layer_llm_output(cache_path, model=model, base_url=base_url, summaries=summaries, decisions=decisions, complete=False)
        for summary in summaries:
            layer = summary['layer']
            decisions.setdefault(layer, {'layer': layer, 'role': 'unknown', 'candidate_types': [], 'confidence': 0.0, 'reason': 'LLM did not return this layer', 'source': 'llm_layer_semantic', 'model': model, 'llm_returned': False})
        decisions = apply_deterministic_layer_overrides(decisions)
        output = {'model': model, 'base_url': base_url, 'prompt_version': LAYER_LLM_PROMPT_VERSION, 'strategy': 'llm_layer_area_geometry_plus_parallel_wall_line_bundles_with_deterministic_negative_layer_rules', 'complete': True, 'layer_count': len(summaries), 'decided_layer_count': sum((1 for item in decisions.values() if item.get('llm_returned') is True)), 'role_counts': dict(Counter((item['role'] for item in decisions.values())).most_common()), 'candidate_type_counts': dict(Counter((kind for item in decisions.values() for kind in item.get('candidate_types', []))).most_common()), 'decisions': decisions, 'layer_summaries': summaries}
        write_json(output_dir / LAYER_LLM_FILE, output)
        return decisions

    def row_candidate_types(row: dict[str, str], decisions: dict[str, dict[str, Any]]) -> list[str]:
        if layer_is_forced_non_obstacle(row.get('layer')):
            return []
        decision = decisions.get(str(row.get('layer') or ''))
        if not decision or decision.get('role') != 'obstacle_candidate':
            return []
        return [str(value).strip().lower() for value in decision.get('candidate_types', []) or [] if str(value).strip()]

    def row_has_wall_semantic(row: dict[str, str], decisions: dict[str, dict[str, Any]]) -> bool:
        candidate_types = row_candidate_types(row, decisions)
        if 'wall' in candidate_types:
            return True
        decision = decisions.get(str(row.get('layer') or ''))
        if not decision or decision.get('role') != 'obstacle_candidate':
            return False
        semantic_text = ' '.join([str(row.get('layer') or ''), str(decision.get('reason') or ''), ' '.join((str(value) for value in decision.get('candidate_types', []) or []))]).upper()
        return any((term in semantic_text for term in ('WALL', 'CURTAIN', 'GLZ', 'GLZE', 'GLAZ', '墙', '幕墙')))

    def choose_llm_obstacle_type(candidate_types: list[str], entity_type: str) -> str:
        if 'column' in candidate_types and entity_type == 'CIRCLE':
            return 'column'
        if 'wall' in candidate_types:
            return 'wall'
        if 'column' in candidate_types:
            return 'column'
        return 'filled_obstacle'

    def rows_in_regions(row: dict[str, str], regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        bounds = bbox_from_row(row)
        if not bounds:
            return []
        center = Point(*bbox_center(bounds))
        row_geometries: list[Any] | None = None
        matched = []
        for region in regions:
            region_polygon = region['polygon']
            if region_polygon.covers(center):
                matched.append(region)
                continue
            if not bbox_intersects(bounds, region['bbox']):
                continue
            if row_geometries is None:
                row_geometries = []
                row_geometries.extend(polygons_from_row(row, ObstacleConfig()))
                row_geometries.extend(lines_from_row(row, ObstacleConfig(), min_length=0.0))
            for geom in row_geometries:
                try:
                    if geom.intersects(region_polygon):
                        matched.append(region)
                        break
                except Exception:
                    continue
        return matched

    def add_obstacle(out: list[dict[str, Any]], *, row: dict[str, str], region: dict[str, Any], geom: Any, obstacle_type: str, reason: str, confidence: float, semantic_evidence: str='', geometry_evidence: str='', topology_evidence: str='', negative_evidence: str='', fusion_decision: str='', review_topology_geometry: Any | None=None) -> None:
        bounds = tuple((float(value) for value in geom.bounds))
        out.append({'obstacle_id': f'OBS_{len(out) + 1:06d}', 'object_id': row.get('object_id', ''), 'handle': row.get('handle', ''), 'source': row.get('source', ''), 'sheet_id': region['sheet_id'], 'floor_id': region['floor_id'], 'source_floor_id': region.get('source_floor_id', region['floor_id']), 'building_scope_id': region.get('building_scope_id', region['floor_id']), 'building_id': region.get('building_id', ''), 'building_name': region.get('building_name', ''), 'floor_name': region['floor_name'], 'region_id': region['region_id'], 'parent_region_id': region.get('parent_region_id', region['region_id']), 'full_region_id': region['full_region_id'], 'obstacle_type': obstacle_type, 'reason': reason, 'confidence': confidence, 'layer': row.get('layer', ''), 'entity_type': row.get('entity_type', ''), 'geometry_kind': row.get('geometry_kind', ''), 'color': row.get('color', ''), 'linetype': row.get('linetype', ''), 'parent_block_name': row.get('parent_block_name', ''), 'semantic_evidence': semantic_evidence, 'geometry_evidence': geometry_evidence, 'topology_evidence': topology_evidence, 'negative_evidence': negative_evidence, 'fusion_decision': fusion_decision, 'bbox_minx': bounds[0], 'bbox_miny': bounds[1], 'bbox_maxx': bounds[2], 'bbox_maxy': bounds[3], 'area': float(geom.area), 'geometry': geom, 'review_topology_geometry': review_topology_geometry})

    def semantic_evidence_for_row(row: dict[str, str], decisions: dict[str, dict[str, Any]]) -> str:
        decision = decisions.get(str(row.get('layer') or ''), {})
        role = str(decision.get('role') or 'unknown')
        types = ','.join((str(value) for value in decision.get('candidate_types', []) or []))
        confidence = decision.get('confidence', '')
        reason = str(decision.get('reason') or '')
        return f"layer={row.get('layer', '')}; role={role}; types={types}; confidence={confidence}; reason={reason}"

    def is_dashed_linetype(row: dict[str, str]) -> bool:
        text = f"{row.get('linetype', '')} {row.get('geometry_kind', '')}".upper()
        return any((token in text for token in ('DASH', 'HIDDEN', 'PHANTOM', 'CENTER', 'DOT', '虚线')))

    def open_line_segments_from_row(row: dict[str, str], config: ObstacleConfig) -> list[LineString]:
        entity_type = str(row.get('entity_type') or '').upper()
        if entity_type not in {'LINE', 'LWPOLYLINE', 'POLYLINE'}:
            return []
        if str(row.get('is_closed') or '').lower() in {'1', 'true'}:
            return []
        if is_dashed_linetype(row):
            return []
        segments: list[LineString] = []
        for line in lines_from_row(row, config, min_length=0.0):
            coords = list(line.coords)
            for start, end in zip(coords, coords[1:]):
                try:
                    segment = LineString([start, end])
                except Exception:
                    continue
                if not segment.is_empty and segment.length > 0:
                    segments.append(segment)
        return segments

    def line_unit_vectors(line: LineString) -> tuple[tuple[float, float], tuple[float, float]] | None:
        start, end = line_endpoint_pair(line)
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length = math.hypot(dx, dy)
        if length <= 0:
            return None
        ux, uy = (dx / length, dy / length)
        if ux < 0 or (abs(ux) < 1e-09 and uy < 0):
            ux, uy = (-ux, -uy)
        return ((ux, uy), (-uy, ux))

    def line_angle_deg_mod_180(line: LineString) -> float | None:
        vectors = line_unit_vectors(line)
        if vectors is None:
            return None
        (ux, uy), _normal = vectors
        angle = math.degrees(math.atan2(uy, ux))
        if angle < 0:
            angle += 180.0
        if angle >= 180.0:
            angle -= 180.0
        return angle

    def angle_distance_mod_180(a: float, b: float) -> float:
        diff = abs(a - b) % 180.0
        return min(diff, 180.0 - diff)

    def project_point(point: tuple[float, float], axis: tuple[float, float]) -> float:
        return point[0] * axis[0] + point[1] * axis[1]

    def projection_interval(line: LineString, axis: tuple[float, float]) -> tuple[float, float]:
        values = [project_point((float(x), float(y)), axis) for x, y in line.coords]
        return (min(values), max(values))

    def average_projection(line: LineString, axis: tuple[float, float]) -> float:
        values = [project_point((float(x), float(y)), axis) for x, y in line.coords]
        return sum(values) / len(values)

    def overlap_interval(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float] | None:
        start = max(a[0], b[0])
        end = min(a[1], b[1])
        if end <= start:
            return None
        return (start, end)

    def point_from_axes(along: float, across: float, direction: tuple[float, float], normal: tuple[float, float]) -> tuple[float, float]:
        return (along * direction[0] + across * normal[0], along * direction[1] + across * normal[1])

    def wall_band_from_interval(interval: tuple[float, float], low: float, high: float, direction: tuple[float, float], normal: tuple[float, float], config: ObstacleConfig) -> Polygon | None:
        if interval[1] <= interval[0] or high <= low:
            return None
        try:
            polygon = Polygon([point_from_axes(interval[0], low, direction, normal), point_from_axes(interval[1], low, direction, normal), point_from_axes(interval[1], high, direction, normal), point_from_axes(interval[0], high, direction, normal)])
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
            if polygon.is_empty or polygon.area <= config.min_area:
                return None
            return polygon
        except Exception:
            return None

    def parallel_wall_pair_geometry(line_a: LineString, line_b: LineString, config: ObstacleConfig) -> dict[str, Any] | None:
        """Return the projection-overlap core and topology completion candidates.

        The overlap band remains the only unconditional wall geometry.  The union
        span and endpoint support band are candidates consumed later by the wall
        topology stage; they are never accepted without cap, L/T, or polygonized
        wall-face evidence.
        """
        angle_a = line_angle_deg_mod_180(line_a)
        angle_b = line_angle_deg_mod_180(line_b)
        if angle_a is None or angle_b is None:
            return None
        if angle_distance_mod_180(angle_a, angle_b) > config.parallel_angle_tolerance_deg:
            return None
        vectors = line_unit_vectors(line_a)
        if vectors is None:
            return None
        direction, normal = vectors
        interval_a = projection_interval(line_a, direction)
        interval_b = projection_interval(line_b, direction)
        core_interval = overlap_interval(interval_a, interval_b)
        if core_interval is None:
            return None
        overlap = core_interval[1] - core_interval[0]
        min_length = min(line_a.length, line_b.length)
        if overlap <= min_length * config.min_projection_overlap_ratio:
            return None
        across_a = average_projection(line_a, normal)
        across_b = average_projection(line_b, normal)
        width = abs(across_a - across_b)
        if width <= 0 or width > overlap * config.max_wall_width_to_overlap_ratio:
            return None
        low = min(across_a, across_b)
        high = max(across_a, across_b)
        core_polygon = wall_band_from_interval(core_interval, low, high, direction, normal, config)
        if core_polygon is None:
            return None
        union_interval = (min(interval_a[0], interval_b[0]), max(interval_a[1], interval_b[1]))
        full_polygon = wall_band_from_interval(union_interval, low, high, direction, normal, config)
        support_amount = width * config.topology_endpoint_support_width_ratio
        endpoint_support_interval = (core_interval[0] - support_amount, core_interval[1] + support_amount)
        endpoint_support_polygon = wall_band_from_interval(endpoint_support_interval, low, high, direction, normal, config)
        core_center = core_polygon.centroid
        center = (float(core_center.x), float(core_center.y))
        return {'angle': angle_a, 'direction': direction, 'normal': normal, 'interval_a': interval_a, 'interval_b': interval_b, 'core_interval': core_interval, 'union_interval': union_interval, 'low': low, 'high': high, 'width': width, 'overlap': overlap, 'center': center, 'core_polygon': core_polygon, 'full_polygon': full_polygon or core_polygon, 'endpoint_support_polygon': endpoint_support_polygon or core_polygon}

    def pair_record_segment_key(record: dict[str, Any]) -> tuple[Any, Any]:
        """Return concrete segment identities, preferring stable inventory UIDs."""
        uid_a = record.get('segment_a_uid')
        uid_b = record.get('segment_b_uid')
        if uid_a is not None and uid_b is not None:
            return tuple(sorted((str(uid_a), str(uid_b))))
        return tuple(sorted((int(record['segment_a_index']), int(record['segment_b_index']))))

    def pair_candidate_sort_key(record: dict[str, Any]) -> tuple[float, float, float, int, int, str]:
        segment_a_index = int(record['segment_a_index'])
        segment_b_index = int(record['segment_b_index'])
        segment_a_index, segment_b_index = sorted((segment_a_index, segment_b_index))
        return (float(record['distance']), float(record.get('angle_difference') or 0.0), -float(record['overlap']), segment_a_index, segment_b_index, str(record.get('pair_uid') or record.get('pair_key') or ''))

    def select_mutual_best_one_to_one_pairs(candidate_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Choose deterministic minimum-distance pairs without reusing a segment.

        Edges are consumed by global minimum-distance priority.  At the moment an
        edge is accepted, every closer edge incident to either endpoint has already
        been consumed or made unavailable, so the chosen endpoints are mutual best
        among the remaining segments.  Each concrete segment is used at most once.
        """
        used_segments: set[Any] = set()
        selected: list[dict[str, Any]] = []
        for record in sorted(candidate_records, key=pair_candidate_sort_key):
            segment_a_index, segment_b_index = pair_record_segment_key(record)
            if segment_a_index in used_segments or segment_b_index in used_segments:
                continue
            record['pair_selection_evidence'] = 'mutual_best_available_one_to_one_min_distance'
            selected.append(record)
            used_segments.add(segment_a_index)
            used_segments.add(segment_b_index)
        return selected

    def select_core_owner_then_topology_only_pairs(candidate_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Reserve segments for in-region cores before routing tail-only pairs.

        A pair whose projection core is outside every inspection region may still
        have a source-span tail entering a region and later prove that tail through
        an L/T junction or end cap.  Such topology-only evidence must not steal a
        concrete segment from a pair whose high-confidence core is already inside
        the inspected floor area.
        """
        core_candidates = [record for record in candidate_records if str(record.get('pair_routing') or 'core') == 'core']
        selected_core = select_mutual_best_one_to_one_pairs(core_candidates)
        occupied_segments = {segment_uid for record in selected_core for segment_uid in pair_record_segment_key(record)}
        topology_candidates = [record for record in candidate_records if str(record.get('pair_routing') or '') == 'topology_only' and (not occupied_segments.intersection(pair_record_segment_key(record)))]
        selected_topology = select_mutual_best_one_to_one_pairs(topology_candidates)
        for record in selected_core:
            record['pair_selection_evidence'] += ';core_owner_priority'
        for record in selected_topology:
            record['pair_selection_evidence'] += ';topology_only_after_core_occupancy'
        return selected_core + selected_topology

    def pair_record_center(record: dict[str, Any]) -> tuple[float, float]:
        cached = record.get('center')
        if isinstance(cached, (list, tuple)) and len(cached) >= 2:
            return (float(cached[0]), float(cached[1]))
        center = record['core_polygon'].centroid
        result = (float(center.x), float(center.y))
        record['center'] = result
        return result

    @dataclass
    class WallWidthReferenceIndex:
        """Directional buckets with an optional spatial index for wall-pair peers.

        An instance is deliberately scoped by its caller (later this is one
        ``floor_id + layer`` group).  Keeping scope out of the index key makes it
        impossible for a query to accidentally see a reference from another
        floor, while this helper remains useful for whole-drawing shadow tests.
        """
        reference_pairs: list[dict[str, Any]]
        bucket_width_deg: float
        bucket_count: int
        bucket_members: dict[int, list[int]]
        bucket_trees: dict[int, Any]
        scipy_enabled: bool

    def wall_width_direction_bucket(angle: float, bucket_width_deg: float=5.0) -> int:
        width = max(0.1, min(180.0, float(bucket_width_deg)))
        count = max(1, int(math.ceil(180.0 / width)))
        return int(math.floor(float(angle) % 180.0 / width)) % count

    def build_wall_width_reference_index(reference_pairs: list[dict[str, Any]], config: ObstacleConfig) -> WallWidthReferenceIndex:
        """Precompute pair centres and build deterministic direction/KD indexes."""
        bucket_width = max(0.1, min(180.0, float(config.wall_width_model_direction_bucket_deg)))
        bucket_count = max(1, int(math.ceil(180.0 / bucket_width)))
        bucket_members: dict[int, list[int]] = defaultdict(list)
        for index, record in enumerate(reference_pairs):
            pair_record_center(record)
            bucket_id = wall_width_direction_bucket(float(record['angle']), bucket_width)
            bucket_members[bucket_id].append(index)
        scipy_enabled = bool(config.wall_width_model_use_spatial_index and cKDTree is not None)
        bucket_trees: dict[int, Any] = {}
        if scipy_enabled:
            for bucket_id, members in bucket_members.items():
                centres = [pair_record_center(reference_pairs[index]) for index in members]
                if centres:
                    bucket_trees[bucket_id] = cKDTree(centres)
        return WallWidthReferenceIndex(reference_pairs=reference_pairs, bucket_width_deg=bucket_width, bucket_count=bucket_count, bucket_members=dict(bucket_members), bucket_trees=bucket_trees, scipy_enabled=scipy_enabled)

    def _wall_width_compatible_bucket_ids(angle: float, index: WallWidthReferenceIndex, tolerance_deg: float) -> list[int]:
        centre_bucket = wall_width_direction_bucket(angle, index.bucket_width_deg)
        span = int(math.ceil(max(0.0, tolerance_deg) / index.bucket_width_deg)) + 1
        return sorted({(centre_bucket + offset) % index.bucket_count for offset in range(-span, span + 1)})

    def _wall_width_peer_value(record: dict[str, Any], peer: dict[str, Any], config: ObstacleConfig) -> tuple[float, float] | None:
        if set(pair_record_segment_key(record)).intersection(pair_record_segment_key(peer)):
            return None
        if angle_distance_mod_180(float(record['angle']), float(peer['angle'])) > config.wall_width_model_direction_tolerance_deg:
            return None
        peer_width = float(peer['width'])
        if peer_width <= 0:
            return None
        center_x, center_y = pair_record_center(record)
        peer_x, peer_y = pair_record_center(peer)
        return (math.hypot(peer_x - center_x, peer_y - center_y), peer_width)

    def _wall_width_directional_peers_bruteforce(record: dict[str, Any], reference_pairs: list[dict[str, Any]], config: ObstacleConfig) -> list[tuple[float, float]]:
        peers: list[tuple[float, float]] = []
        for peer in reference_pairs:
            value = _wall_width_peer_value(record, peer, config)
            if value is not None:
                peers.append(value)
        return peers

    def _wall_width_directional_peers_indexed(record: dict[str, Any], index: WallWidthReferenceIndex, config: ObstacleConfig) -> list[tuple[float, float]]:
        """Return the exact peer sample the legacy gate would retain.

        Radius queries cover the local branch.  When it is sparse, adaptive KNN
        queries obtain enough exact-angle peers for the legacy nearest-neighbour
        fallback.  Direction and shared-segment checks remain exact post-filters.
        """
        center = pair_record_center(record)
        width = float(record['width'])
        local_radius = max(float(record['overlap']), width) * config.wall_width_model_local_radius_ratio
        minimum_samples = max(1, int(config.wall_width_model_min_samples))
        maximum_neighbors = max(minimum_samples, int(config.wall_width_model_max_neighbors))
        bucket_ids = _wall_width_compatible_bucket_ids(float(record['angle']), index, config.wall_width_model_direction_tolerance_deg)
        if not index.scipy_enabled:
            candidate_indices = {reference_index for bucket_id in bucket_ids for reference_index in index.bucket_members.get(bucket_id, [])}
            peers = [value for reference_index in sorted(candidate_indices) if (value := _wall_width_peer_value(record, index.reference_pairs[reference_index], config)) is not None]
            return peers
        local_reference_indices: set[int] = set()
        for bucket_id in bucket_ids:
            members = index.bucket_members.get(bucket_id, [])
            tree = index.bucket_trees.get(bucket_id)
            if not members or tree is None:
                continue
            for local_index in tree.query_ball_point(center, r=math.nextafter(local_radius, math.inf)):
                local_reference_indices.add(members[int(local_index)])
        local_peers = [value for reference_index in sorted(local_reference_indices) if (value := _wall_width_peer_value(record, index.reference_pairs[reference_index], config)) is not None]
        exact_local_count = sum((1 for spatial_distance, _peer_width in local_peers if spatial_distance <= local_radius))
        if exact_local_count >= minimum_samples:
            return local_peers
        global_reference_indices: set[int] = set()
        for bucket_id in bucket_ids:
            members = index.bucket_members.get(bucket_id, [])
            tree = index.bucket_trees.get(bucket_id)
            if not members or tree is None:
                continue
            requested = min(len(members), max(1, maximum_neighbors + 2))
            valid_in_bucket: list[tuple[float, int]] = []
            while True:
                distances, positions = tree.query(center, k=requested)
                if requested == 1:
                    distance_values = [float(distances)]
                    position_values = [int(positions)]
                else:
                    distance_values = [float(value) for value in distances]
                    position_values = [int(value) for value in positions]
                valid_in_bucket = []
                for spatial_distance, local_index in zip(distance_values, position_values):
                    if local_index < 0 or local_index >= len(members) or (not math.isfinite(spatial_distance)):
                        continue
                    reference_index = members[local_index]
                    if _wall_width_peer_value(record, index.reference_pairs[reference_index], config) is not None:
                        valid_in_bucket.append((spatial_distance, reference_index))
                if len(valid_in_bucket) >= maximum_neighbors or requested >= len(members):
                    break
                requested = min(len(members), max(requested + 1, requested * 2))
            if valid_in_bucket:
                valid_in_bucket.sort(key=lambda item: (item[0], item[1]))
                cutoff = valid_in_bucket[min(maximum_neighbors, len(valid_in_bucket)) - 1][0]
                for local_index in tree.query_ball_point(center, r=math.nextafter(cutoff, math.inf)):
                    global_reference_indices.add(members[int(local_index)])
        return [value for reference_index in sorted(global_reference_indices) if (value := _wall_width_peer_value(record, index.reference_pairs[reference_index], config)) is not None]

    def _wall_width_model_decision(record: dict[str, Any], directional_peers: list[tuple[float, float]], config: ObstacleConfig) -> tuple[bool, str]:
        """Apply the unchanged robust one-sided wall-width decision formula."""
        width = float(record['width'])
        if width <= 0:
            return (False, 'rejected_non_positive_width')
        minimum_samples = max(1, int(config.wall_width_model_min_samples))
        if len(directional_peers) < minimum_samples:
            if directional_peers:
                sparse_widths = [peer_width for _distance, peer_width in directional_peers]
                sparse_median = float(median(sparse_widths))
                sparse_upper_bound = sparse_median * config.wall_width_model_sparse_outlier_ratio
                if width > sparse_upper_bound + 1e-09:
                    return (False, f'rejected_sparse_overwide_outlier;peer_count={len(sparse_widths)};width={width:.6f};median={sparse_median:.6f};upper_bound={sparse_upper_bound:.6f}')
            return (True, f'passed_sparse_directional_model;peer_count={len(directional_peers)}')
        directional_peers.sort(key=lambda item: (item[0], item[1]))
        local_radius = max(float(record['overlap']), width) * config.wall_width_model_local_radius_ratio
        local_peers = [item for item in directional_peers if item[0] <= local_radius]
        scope = 'local_directional'
        if len(local_peers) < minimum_samples:
            local_peers = directional_peers
            scope = 'directional_knn_fallback'
        local_peers = local_peers[:max(minimum_samples, int(config.wall_width_model_max_neighbors))]
        peer_widths = [item[1] for item in local_peers]
        if len(peer_widths) < minimum_samples:
            return (True, f'passed_sparse_{scope};peer_count={len(peer_widths)}')
        similar_peer_count = sum((1 for peer_width in peer_widths if max(width, peer_width) / max(min(width, peer_width), 1e-09) <= config.wall_width_model_similar_ratio))
        required_similar_support = max(1, int(config.wall_width_model_similar_min_support), int(math.ceil(len(peer_widths) * config.wall_width_model_similar_min_fraction)))
        if similar_peer_count >= required_similar_support:
            return (True, f'passed_supported_width_mode;scope={scope};peer_count={len(peer_widths)};similar_peer_count={similar_peer_count};required_support={required_similar_support}')
        peer_median = float(median(peer_widths))
        peer_mad = float(median([abs(value - peer_median) for value in peer_widths]))
        upper_bound = max(peer_median * config.wall_width_model_outlier_ratio, peer_median + peer_mad * config.wall_width_model_mad_multiplier)
        passed = width <= upper_bound + 1e-09
        decision = 'passed_width_upper_bound' if passed else 'rejected_overwide_outlier'
        return (passed, f'{decision};scope={scope};peer_count={len(peer_widths)};width={width:.6f};median={peer_median:.6f};mad={peer_mad:.6f};upper_bound={upper_bound:.6f}')

    def wall_width_model_gate(record: dict[str, Any], reference_pairs: list[dict[str, Any]], config: ObstacleConfig, *, reference_index: WallWidthReferenceIndex | None=None, shadow_compare: bool | None=None) -> tuple[bool, str]:
        """Reject only clearly over-wide pairs using local directional peers.

        The model is intentionally one-sided: unusually narrow pairs are retained,
        while a thick local mode is retained when at least a small peer cluster has
        comparable width.  Sparse directions remain unchanged instead of guessing
        a scale from one or two samples.
        """
        if reference_index is None:
            peers = _wall_width_directional_peers_bruteforce(record, reference_pairs, config)
            return _wall_width_model_decision(record, peers, config)
        indexed_peers = _wall_width_directional_peers_indexed(record, reference_index, config)
        indexed_decision = _wall_width_model_decision(record, indexed_peers, config)
        compare = config.wall_width_model_shadow_compare if shadow_compare is None else bool(shadow_compare)
        if compare:
            brute_peers = _wall_width_directional_peers_bruteforce(record, reference_pairs, config)
            brute_decision = _wall_width_model_decision(record, brute_peers, config)
            if indexed_decision != brute_decision:
                raise AssertionError(f"wall width spatial index changed the legacy decision: pair={record.get('pair_uid') or record.get('pair_key')}; indexed={indexed_decision}; brute_force={brute_decision}")
        return indexed_decision

    def llm_area_geometries_from_row(row: dict[str, str], config: ObstacleConfig) -> list[Any]:
        return polygons_from_row(row, config)

    def detect_llm_hit_obstacles(rows: list[dict[str, str]], regions: list[dict[str, Any]], decisions: dict[str, dict[str, Any]], config: ObstacleConfig) -> list[dict[str, Any]]:
        """Output closed/area geometry from LLM-hit obstacle layers."""
        out: list[dict[str, Any]] = []
        for row in rows:
            candidate_types = row_candidate_types(row, decisions)
            if not candidate_types:
                continue
            decision = decisions.get(str(row.get('layer') or ''), {})
            entity_type = str(row.get('entity_type') or '').upper()
            obstacle_type = choose_llm_obstacle_type(candidate_types, entity_type)
            geometries = llm_area_geometries_from_row(row, config)
            if not geometries:
                continue
            matched_regions = rows_in_regions(row, regions)
            if not matched_regions:
                continue
            for geom in geometries:
                for region in matched_regions:
                    try:
                        clipped = geom.intersection(region['polygon'])
                    except Exception:
                        continue
                    parts = list(clipped.geoms) if isinstance(clipped, MultiPolygon) else [clipped]
                    for part in parts:
                        if not isinstance(part, Polygon) or part.is_empty:
                            continue
                        add_obstacle(out, row=row, region=region, geom=part, obstacle_type=obstacle_type, reason='llm_layer_obstacle_hit_direct_geometry_clipped_to_inspection_region', confidence=float(decision.get('confidence') or 0.9), semantic_evidence=semantic_evidence_for_row(row, decisions), geometry_evidence='llm_hit_layer_area_geometry_clipped_to_inspection_region', fusion_decision='accepted_by_llm_layer_area_geometry_then_region_clip')
        return out

    def polygon_parts(geom: Any) -> list[Polygon]:
        if geom is None:
            return []
        try:
            if geom.is_empty:
                return []
        except Exception:
            return []
        if isinstance(geom, Polygon):
            return [geom]
        parts: list[Polygon] = []
        for part in getattr(geom, 'geoms', []) or []:
            parts.extend(polygon_parts(part))
        return parts

    def safe_polygonal_difference(left: Any, right: Any) -> Any:
        """Run GEOS difference while containing benign degenerate-edge warnings."""
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', RuntimeWarning)
                result = left.difference(right)
            if not result.is_empty and (not result.is_valid):
                result = result.buffer(0)
            return result
        except Exception:
            return GeometryCollection()

    def layer_has_opening_semantics(layer: str) -> bool:
        """Keep topology completion away from explicit door/opening layers."""
        text = str(layer or '').upper()
        return any((token in text for token in ('DOOR', 'OPENING', '留洞', '门窗洞', '洞口')))

    def perpendicular_angle_match(angle_a: float, angle_b: float, tolerance_deg: float) -> bool:
        return abs(angle_distance_mod_180(angle_a, angle_b) - 90.0) <= tolerance_deg

    def strtree_query_indices(tree: STRtree, query_geom: Any, geometry_id_to_index: dict[int, int]) -> list[int]:
        """Normalize Shapely 1.x geometry and 2.x integer STRtree results."""
        indices: list[int] = []
        for value in tree.query(query_geom):
            if isinstance(value, Integral) or not hasattr(value, 'geom_type'):
                try:
                    indices.append(int(value))
                except Exception:
                    continue
            else:
                index = geometry_id_to_index.get(id(value))
                if index is not None:
                    indices.append(index)
        return indices

    def polygonized_wall_faces(segments: list[dict[str, Any]], core_union: Any, config: ObstacleConfig) -> list[Polygon]:
        """Polygonize trusted wall linework and retain faces supported by cores."""
        if not segments or core_union is None or getattr(core_union, 'is_empty', True):
            return []
        try:
            noded_linework = unary_union([item['line'] for item in segments])
            candidates = polygonize(noded_linework)
        except Exception:
            return []
        selected: list[Polygon] = []
        for candidate in candidates:
            if candidate.is_empty or candidate.area <= config.min_area:
                continue
            try:
                core_overlap = candidate.intersection(core_union).area
            except Exception:
                continue
            if core_overlap <= 0:
                continue
            if core_overlap / candidate.area < config.topology_polygon_min_core_overlap_ratio:
                continue
            selected.append(candidate)
        return selected

    def polygonized_wall_face_union(segments: list[dict[str, Any]], core_union: Any, config: ObstacleConfig) -> tuple[Any, int]:
        """Return the union of the individually core-qualified wall faces."""
        selected = polygonized_wall_faces(segments, core_union, config)
        if not selected:
            return (GeometryCollection(), 0)
        try:
            return (unary_union(selected), len(selected))
        except Exception:
            return (GeometryCollection(), 0)

    def complete_layer_wall_pair_topology(pair_records: list[dict[str, Any]], segments: list[dict[str, Any]], config: ObstacleConfig, *, polygonized_faces_override: list[Polygon] | None=None) -> dict[str, Any]:
        """Complete pair tails and L/T/cap junctions without bridging openings.

        Every pair keeps its projection-overlap core.  Additional geometry must be
        inside a polygonized wall face, intersect a perpendicular wall-bundle
        support band, or be bounded by a short perpendicular cap line.
        """
        for record in pair_records:
            record['enhanced_polygon'] = record['core_polygon']
            record['topology_added_polygon'] = GeometryCollection()
            record['topology_evidence_kinds'] = []
        if not config.topology_completion_enabled or not pair_records or layer_has_opening_semantics(str(pair_records[0].get('layer') or '')):
            return {'polygonized_union': GeometryCollection(), 'polygonized_face_count': 0, 'leftover_parts': []}
        try:
            core_union = unary_union([record['core_polygon'] for record in pair_records])
        except Exception:
            core_union = GeometryCollection()
        if polygonized_faces_override is None:
            polygonized_union, polygonized_face_count = polygonized_wall_face_union(segments, core_union, config)
        else:
            polygonized_face_count = len(polygonized_faces_override)
            try:
                polygonized_union = unary_union(polygonized_faces_override) if polygonized_faces_override else GeometryCollection()
            except Exception:
                polygonized_union = GeometryCollection()
                polygonized_face_count = 0
        support_geometries = [record['endpoint_support_polygon'] for record in pair_records]
        support_tree = STRtree(support_geometries)
        support_index = {id(geom): index for index, geom in enumerate(support_geometries)}
        line_geometries = [item['line'] for item in segments]
        line_tree = STRtree(line_geometries)
        line_index = {id(geom): index for index, geom in enumerate(line_geometries)}
        for record_index, record in enumerate(pair_records):
            core = record['core_polygon']
            full = record['full_polygon']
            try:
                candidate_extra = safe_polygonal_difference(full, core)
            except Exception:
                continue
            if candidate_extra.is_empty:
                continue
            supported_parts: list[Any] = []
            cap_supported_parts: list[Any] = []
            evidence_kinds: set[str] = set()
            if not polygonized_union.is_empty:
                try:
                    supported = candidate_extra.intersection(polygonized_union)
                    if not supported.is_empty and supported.area > config.min_area:
                        supported_parts.append(supported)
                        evidence_kinds.add('polygonized_wall_face')
                except Exception:
                    pass
            for other_index in strtree_query_indices(support_tree, candidate_extra, support_index):
                if other_index == record_index:
                    continue
                other = pair_records[other_index]
                if not perpendicular_angle_match(float(record['angle']), float(other['angle']), config.topology_perpendicular_tolerance_deg):
                    continue
                try:
                    supported = candidate_extra.intersection(support_geometries[other_index])
                except Exception:
                    continue
                if not supported.is_empty and supported.area > config.min_area:
                    supported_parts.append(supported)
                    evidence_kinds.add('perpendicular_wall_bundle')
            width = float(record['width'])
            search_tolerance = max(1e-06, width * config.topology_cap_search_tolerance_width_ratio)
            candidate_extra_parts = polygon_parts(candidate_extra)
            for extra_part in candidate_extra_parts:
                approximate_tail_length = extra_part.area / max(width, 1e-09)
                if approximate_tail_length > width * config.topology_max_cap_tail_width_ratio:
                    continue
                minx, miny, maxx, maxy = extra_part.bounds
                search_geom = box(minx - search_tolerance, miny - search_tolerance, maxx + search_tolerance, maxy + search_tolerance)
                for segment_index in strtree_query_indices(line_tree, search_geom, line_index):
                    segment = segments[segment_index]
                    if not perpendicular_angle_match(float(record['angle']), float(segment['angle']), config.topology_perpendicular_tolerance_deg):
                        continue
                    try:
                        crossing_length = segment['line'].intersection(search_geom).length
                    except Exception:
                        continue
                    if crossing_length >= width * config.topology_cap_min_crossing_ratio:
                        supported_parts.append(extra_part)
                        cap_supported_parts.append(extra_part)
                        evidence_kinds.add('perpendicular_end_cap')
                        break
            if not supported_parts:
                continue
            try:
                use_numerically_stable_full_span = candidate_extra_parts and len(cap_supported_parts) == len(candidate_extra_parts) and (candidate_extra.area <= max(float(config.min_area), float(full.area) * 1e-09))
                if use_numerically_stable_full_span:
                    enhanced = full
                    added = candidate_extra
                else:
                    supported_union = unary_union(supported_parts).intersection(candidate_extra)
                    enhanced = core.union(supported_union)
                    if not enhanced.is_valid:
                        enhanced = enhanced.buffer(0)
                    added = safe_polygonal_difference(enhanced, core)
            except Exception:
                continue
            if added.is_empty or added.area <= config.min_area:
                continue
            record['enhanced_polygon'] = enhanced
            record['topology_added_polygon'] = added
            record['topology_evidence_kinds'] = sorted(evidence_kinds)
        try:
            enhanced_union = unary_union([record['enhanced_polygon'] for record in pair_records])
            leftover = safe_polygonal_difference(polygonized_union, enhanced_union)
        except Exception:
            leftover = GeometryCollection()
        return {'polygonized_union': polygonized_union, 'polygonized_face_count': polygonized_face_count, 'leftover_parts': [part for part in polygon_parts(leftover) if part.area > config.min_area]}

    def stable_wall_segment_uid(row: dict[str, str], line: LineString, *, row_index: int, segment_index: int) -> str:
        """Build a stable identity for one concrete straight source segment."""
        start, end = line_endpoint_pair(line)
        endpoints = sorted((start, end))

        def coordinate(value: float) -> str:
            rounded = round(float(value), 9)
            if rounded == 0:
                rounded = 0.0
            return f'{rounded:.9f}'
        geometry_key = ':'.join((coordinate(value) for point in endpoints for value in point))
        source_key = '/'.join((str(row.get(field) or '') for field in ('source', 'object_id', 'handle', 'parent_block_name')))
        return f'{source_key}|row={row_index}|part={segment_index}|xy={geometry_key}'

    def stable_wall_pair_uid(floor_id: str, layer: str, segment_a_uid: str, segment_b_uid: str) -> str:
        first, second = sorted((str(segment_a_uid), str(segment_b_uid)))
        return f'floor={floor_id}|layer={layer}|{first}<->{second}'

    def stable_wall_face_uid(floor_id: str, layer: str, face: Polygon) -> str:
        """Hash a polygon independently of ring start point and orientation."""

        def canonical_coordinate(value: float) -> float:
            rounded = round(float(value), 9)
            return 0.0 if rounded == 0 else rounded

        def canonical_ring(ring: Any) -> tuple[tuple[float, float], ...]:
            coordinates = [(canonical_coordinate(point[0]), canonical_coordinate(point[1])) for point in ring.coords]
            if len(coordinates) > 1 and coordinates[0] == coordinates[-1]:
                coordinates.pop()
            if not coordinates:
                return ()

            def minimum_rotation(values: list[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
                minimum_point = min(values)
                return min((tuple(values[index:] + values[:index]) for index, point in enumerate(values) if point == minimum_point))
            forward = minimum_rotation(coordinates)
            reverse = minimum_rotation(list(reversed(coordinates)))
            return min(forward, reverse)
        signature = (canonical_ring(face.exterior), tuple(sorted((canonical_ring(ring) for ring in face.interiors))))
        digest = hashlib.sha1(repr(signature).encode('utf-8')).hexdigest()[:20]
        return f'{floor_id}|{layer}|TOPOFACE_{digest}'

    def _floor_region_contexts(regions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for region in regions:
            floor_scope_id = str(region.get('floor_id') or '').strip()
            if not floor_scope_id:
                sheet_id = str(region.get('sheet_id') or '').strip()
                full_region_id = str(region.get('full_region_id') or '').strip()
                floor_scope_id = f'SHEET:{sheet_id}' if sheet_id else f'REGION:{full_region_id}'
            grouped[floor_scope_id].append(region)
        contexts: dict[str, dict[str, Any]] = {}
        for floor_id, floor_regions in grouped.items():
            ordered = sorted(floor_regions, key=lambda item: str(item.get('full_region_id') or ''))
            try:
                polygon = unary_union([region['polygon'] for region in ordered])
            except Exception:
                polygon = GeometryCollection()
            contexts[floor_id] = {'floor_id': floor_id, 'regions': ordered, 'polygon': polygon}
        return contexts

    def _segment_floor_owner(row: dict[str, str], line: LineString, floor_contexts: dict[str, dict[str, Any]], config: ObstacleConfig) -> str | None:
        """Assign a concrete segment to exactly one floor (never duplicate it)."""
        declared_floor = str(row.get('floor_id') or '')
        if declared_floor and declared_floor in floor_contexts:
            return declared_floor
        direct_scores: list[tuple[float, int, float, str]] = []
        midpoint = line.interpolate(0.5, normalized=True)
        for floor_id, context in floor_contexts.items():
            polygon = context['polygon']
            try:
                intersection_length = float(line.intersection(polygon).length)
                midpoint_covered = int(bool(polygon.covers(midpoint)))
            except Exception:
                continue
            if intersection_length <= 1e-09 and (not midpoint_covered):
                continue
            confidence = max((float(region.get('region_confidence') or 0.0) for region in context['regions']), default=0.0)
            direct_scores.append((intersection_length, midpoint_covered, confidence, floor_id))
        if direct_scores:
            direct_scores.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
            return direct_scores[0][3]
        if not config.regional_wall_processing_enabled:
            return None
        candidate_distance_ratio = config.max_wall_width_to_overlap_ratio * max(1.0, config.regional_candidate_halo_multiplier)
        cap_distance_ratio = config.topology_max_cap_tail_width_ratio + config.topology_cap_search_tolerance_width_ratio
        maximum_distance = float(line.length) * max(candidate_distance_ratio, cap_distance_ratio)
        nearby: list[tuple[float, str]] = []
        for floor_id, context in floor_contexts.items():
            try:
                distance = float(line.distance(context['polygon']))
            except Exception:
                continue
            if distance <= maximum_distance + 1e-09:
                nearby.append((distance, floor_id))
        if not nearby:
            return None
        nearby.sort(key=lambda item: (item[0], item[1]))
        return nearby[0][1]

    def _segment_intersects_region_halo(segment: dict[str, Any], region: dict[str, Any], config: ObstacleConfig) -> bool:
        line = segment['line']
        halo = float(line.length) * config.max_wall_width_to_overlap_ratio * max(1.0, config.regional_candidate_halo_multiplier)
        minx, miny, maxx, maxy = region.get('bbox') or region['polygon'].bounds
        expanded_bounds = (minx - halo, miny - halo, maxx + halo, maxy + halo)
        if not bbox_intersects(tuple((float(value) for value in line.bounds)), expanded_bounds):
            return False
        try:
            return float(line.distance(region['polygon'])) <= halo + 1e-09
        except Exception:
            return False

    def build_periodic_segment_direction_buckets(segments: list[dict[str, Any]], config: ObstacleConfig) -> tuple[float, int, dict[int, dict[str, Any]]]:
        """Build direction buckets with a spatial tree for periodic-line queries."""
        bucket_width = max(0.1, min(180.0, float(config.wall_width_model_direction_bucket_deg)))
        bucket_count = max(1, int(math.ceil(180.0 / bucket_width)))
        members: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for segment in segments:
            bucket_id = wall_width_direction_bucket(float(segment['angle']), bucket_width)
            members[bucket_id].append(segment)
        indexed_members: dict[int, dict[str, Any]] = {}
        for bucket_id, bucket_segments in members.items():
            geometries = [segment['line'] for segment in bucket_segments]
            indexed_members[bucket_id] = {'segments': bucket_segments, 'tree': STRtree(geometries), 'geometry_index': {id(geometry): index for index, geometry in enumerate(geometries)}}
        return (bucket_width, bucket_count, indexed_members)

    def periodic_parallel_pair_pattern(record: dict[str, Any], periodic_index: tuple[float, int, dict[int, dict[str, Any]]], config: ObstacleConfig) -> tuple[bool, str]:
        """Reject a candidate whose width is the pitch of a repeated line array.

        Stair treads, façade grids, and hatch/detail line arrays form many parallel,
        projection-overlapping lines at a nearly constant pitch.  Treating adjacent
        members as two faces of a wall makes that pitch look like a well-supported
        local wall-thickness mode.  A real repeated double-line wall instead has an
        alternating small wall thickness and much larger bay spacing, so it does not
        form a run of five or more equal candidate-width gaps.
        """
        if not config.periodic_parallel_rejection_enabled:
            return (False, 'periodic_parallel_model_disabled')
        width = float(record.get('width') or 0.0)
        if width <= 0:
            return (False, 'periodic_parallel_not_applicable_non_positive_width')
        bucket_width, bucket_count, bucket_members = periodic_index
        centre_bucket = wall_width_direction_bucket(float(record['angle']), bucket_width)
        span = int(math.ceil(max(0.0, config.periodic_parallel_angle_tolerance_deg) / bucket_width)) + 1
        bucket_ids = {(centre_bucket + offset) % bucket_count for offset in range(-span, span + 1)}
        direction = record['direction']
        normal = record['normal']
        core_interval = record['core_interval']
        overlap_length = max(float(record['overlap']), 1e-09)
        minimum_line_count = max(2, int(config.periodic_parallel_min_line_count))
        compatible_line_count = sum((len(bucket_members.get(bucket_id, {}).get('segments', [])) for bucket_id in bucket_ids))
        if compatible_line_count < minimum_line_count:
            return (False, f'not_periodic_parallel_pattern;line_count={compatible_line_count}')
        try:
            minx, miny, maxx, maxy = record['full_polygon'].bounds
        except Exception:
            minx, miny, maxx, maxy = record['core_polygon'].bounds
        search_margin = width * minimum_line_count
        search_geometry = box(minx - search_margin, miny - search_margin, maxx + search_margin, maxy + search_margin)
        offset_samples: list[float] = []
        for bucket_id in sorted(bucket_ids):
            bucket = bucket_members.get(bucket_id)
            if not bucket:
                continue
            candidate_indices = strtree_query_indices(bucket['tree'], search_geometry, bucket['geometry_index'])
            for segment_index in candidate_indices:
                segment = bucket['segments'][segment_index]
                if angle_distance_mod_180(float(record['angle']), float(segment['angle'])) > config.periodic_parallel_angle_tolerance_deg:
                    continue
                interval = projection_interval(segment['line'], direction)
                common = overlap_interval(interval, core_interval)
                if common is None:
                    continue
                common_length = float(common[1] - common[0])
                if common_length / overlap_length < config.periodic_parallel_min_projection_overlap_ratio:
                    continue
                offset_samples.append(float(average_projection(segment['line'], normal)))
        if len(offset_samples) < minimum_line_count:
            return (False, f'not_periodic_parallel_pattern;line_count={len(offset_samples)}')
        offset_samples.sort()
        duplicate_tolerance = max(1e-06, width * 0.001)
        unique_offsets: list[float] = []
        for offset in offset_samples:
            if not unique_offsets or abs(offset - unique_offsets[-1]) > duplicate_tolerance:
                unique_offsets.append(offset)
            else:
                unique_offsets[-1] = (unique_offsets[-1] + offset) / 2.0
        if len(unique_offsets) < minimum_line_count:
            return (False, f'not_periodic_parallel_pattern;unique_line_count={len(unique_offsets)}')
        low = float(record['low'])
        high = float(record['high'])
        low_index = min(range(len(unique_offsets)), key=lambda index: abs(unique_offsets[index] - low))
        high_index = min(range(len(unique_offsets)), key=lambda index: abs(unique_offsets[index] - high))
        if high_index < low_index:
            low_index, high_index = (high_index, low_index)
        relative_tolerance = max(0.0, config.periodic_parallel_pitch_relative_tolerance)
        endpoint_tolerance = max(1e-06, width * relative_tolerance)
        if high_index <= low_index or abs(unique_offsets[low_index] - low) > endpoint_tolerance or abs(unique_offsets[high_index] - high) > endpoint_tolerance or (abs(unique_offsets[high_index] - unique_offsets[low_index] - width) > endpoint_tolerance):
            return (False, f'not_periodic_parallel_pattern;unique_line_count={len(unique_offsets)}')
        all_pitches = [right - left for left, right in zip(unique_offsets, unique_offsets[1:])]
        best_run_line_count = 0
        best_run_cv = math.inf
        for seed_index, seed_pitch in enumerate(all_pitches):
            if seed_pitch <= 0:
                continue
            pitch_tolerance = max(1e-06, seed_pitch * relative_tolerance)
            run_start = seed_index
            while run_start > 0 and abs(all_pitches[run_start - 1] - seed_pitch) <= pitch_tolerance:
                run_start -= 1
            run_end = seed_index
            while run_end + 1 < len(all_pitches) and abs(all_pitches[run_end + 1] - seed_pitch) <= pitch_tolerance:
                run_end += 1
            run_line_count = run_end - run_start + 2
            best_run_line_count = max(best_run_line_count, run_line_count)
            if run_line_count < minimum_line_count:
                continue
            if not run_start <= low_index < high_index <= run_end + 1:
                continue
            run_pitches = all_pitches[run_start:run_end + 1]
            pitch_mean = sum(run_pitches) / len(run_pitches)
            pitch_variance = sum(((value - pitch_mean) ** 2 for value in run_pitches)) / len(run_pitches)
            pitch_cv = math.sqrt(pitch_variance) / max(abs(pitch_mean), 1e-09)
            best_run_cv = min(best_run_cv, pitch_cv)
            if pitch_cv > relative_tolerance:
                continue
            pitch_multiple = max(1, int(round(width / pitch_mean)))
            multiple_tolerance = max(1e-06, pitch_mean * relative_tolerance * pitch_multiple)
            if abs(width - pitch_mean * pitch_multiple) > multiple_tolerance:
                continue
            return (True, f'rejected_periodic_parallel_pattern;run_line_count={run_line_count};pitch={pitch_mean:.6f};pitch_cv={pitch_cv:.6f};candidate_width={width:.6f};pitch_multiple={pitch_multiple}')
        evidence = f'not_periodic_parallel_pattern;run_line_count={best_run_line_count}'
        if math.isfinite(best_run_cv):
            evidence += f';pitch_cv={best_run_cv:.6f}'
        return (False, evidence)

    def _enumerate_wall_pair_candidates(segments: list[dict[str, Any]], *, floor_id: str, layer: str, candidate_region_id: str, config: ObstacleConfig) -> list[dict[str, Any]]:
        """Enumerate pairs from a region+halo workset using complete source lines."""
        if len(segments) < 2:
            return []
        segment_geometries = [item['line'] for item in segments]
        segment_tree = STRtree(segment_geometries)
        segment_index = {id(geom): index for index, geom in enumerate(segment_geometries)}
        candidate_records: list[dict[str, Any]] = []
        for local_index, first in enumerate(segments):
            line_a = first['line']
            direction = first['direction']
            normal = first['normal']
            interval_a = projection_interval(line_a, direction)
            across_a = average_projection(line_a, normal)
            max_pair_offset = line_a.length * config.max_wall_width_to_overlap_ratio
            minx, miny, maxx, maxy = line_a.bounds
            pair_search_box = box(minx - max_pair_offset, miny - max_pair_offset, maxx + max_pair_offset, maxy + max_pair_offset)
            candidate_indices = sorted((other_index for other_index in strtree_query_indices(segment_tree, pair_search_box, segment_index) if other_index > local_index))
            for other_local_index in candidate_indices:
                second = segments[other_local_index]
                if angle_distance_mod_180(first['angle'], second['angle']) > config.parallel_angle_tolerance_deg:
                    continue
                line_b = second['line']
                interval = overlap_interval(interval_a, projection_interval(line_b, direction))
                if interval is None:
                    continue
                overlap = interval[1] - interval[0]
                if overlap <= min(line_a.length, line_b.length) * config.min_projection_overlap_ratio:
                    continue
                distance = abs(across_a - average_projection(line_b, normal))
                if distance <= 0 or distance > overlap * config.max_wall_width_to_overlap_ratio:
                    continue
                pair_geometry = parallel_wall_pair_geometry(line_a, line_b, config)
                if pair_geometry is None:
                    continue
                row_a = first['row']
                row_b = second['row']
                segment_a_uid = str(first['segment_uid'])
                segment_b_uid = str(second['segment_uid'])
                pair_uid = stable_wall_pair_uid(floor_id, layer, segment_a_uid, segment_b_uid)
                pair_record = dict(pair_geometry)
                pair_record.update({'floor_id': floor_id, 'layer': layer, 'row_a': row_a, 'row_b': row_b, 'line_a': line_a, 'line_b': line_b, 'pair_uid': pair_uid, 'pair_key': tuple(sorted((segment_a_uid, segment_b_uid))), 'segment_a_uid': segment_a_uid, 'segment_b_uid': segment_b_uid, 'segment_a_index': int(first['segment_index']), 'segment_b_index': int(second['segment_index']), 'distance': distance, 'angle_difference': angle_distance_mod_180(first['angle'], second['angle']), 'candidate_region_ids': {candidate_region_id}})
                candidate_records.append(pair_record)
        return candidate_records

    def _assign_pair_region_owner(record: dict[str, Any], regions: list[dict[str, Any]], config: ObstacleConfig) -> None:

        def geometry_matches(geometry: Any, center: Point) -> list[tuple[int, float, float, str]]:
            matches: list[tuple[int, float, float, str]] = []
            for region in regions:
                try:
                    overlap_area = float(geometry.intersection(region['polygon']).area)
                    covers_center = int(bool(region['polygon'].covers(center)))
                except Exception:
                    continue
                if overlap_area <= config.min_area:
                    continue
                matches.append((covers_center, overlap_area, float(region.get('region_confidence') or 0.0), str(region.get('full_region_id') or '')))
            matches.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
            return matches
        core = record['core_polygon']
        full = record['full_polygon']
        core_matches = geometry_matches(core, Point(*pair_record_center(record)))
        try:
            full_center = full.centroid
        except Exception:
            full_center = Point(*pair_record_center(record))
        full_matches = geometry_matches(full, full_center)
        core_owner = core_matches[0][3] if core_matches else ''
        topology_owner = full_matches[0][3] if full_matches else ''
        if core_owner:
            pair_routing = 'core'
            owner_region_id = core_owner
            output_region_ids = tuple(sorted((item[3] for item in core_matches)))
        elif topology_owner:
            pair_routing = 'topology_only'
            owner_region_id = topology_owner
            output_region_ids = tuple(sorted((item[3] for item in full_matches)))
        else:
            pair_routing = 'halo_ghost'
            owner_region_id = ''
            output_region_ids = ()
        record['core_output_region_ids'] = tuple(sorted((item[3] for item in core_matches)))
        record['topology_candidate_region_ids'] = tuple(sorted((item[3] for item in full_matches)))
        record['core_owner_region_id'] = core_owner
        record['topology_candidate_owner_region_id'] = topology_owner
        record['pair_routing'] = pair_routing
        record['output_region_ids'] = output_region_ids
        record['owner_region_id'] = owner_region_id

    def validate_wall_pair_invariants(pair_records: list[dict[str, Any]], *, floor_id: str | None=None, layer: str | None=None) -> dict[str, int]:
        """Fail fast on duplicate pairs, cross-floor records, or segment reuse."""
        seen_pairs: set[str] = set()
        used_segments: set[Any] = set()
        for record in pair_records:
            record_floor = str(record.get('floor_id') or '')
            record_layer = str(record.get('layer') or '')
            if floor_id is not None and record_floor != str(floor_id):
                raise AssertionError(f'cross-floor wall pair: expected={floor_id!r}; actual={record_floor!r}')
            if layer is not None and record_layer != str(layer):
                raise AssertionError(f'cross-layer wall pair: expected={layer!r}; actual={record_layer!r}')
            pair_uid = str(record.get('pair_uid') or record.get('pair_key') or '')
            if pair_uid in seen_pairs:
                raise AssertionError(f'duplicate wall pair_uid: {pair_uid}')
            seen_pairs.add(pair_uid)
            for segment_uid in pair_record_segment_key(record):
                if segment_uid in used_segments:
                    raise AssertionError(f'concrete wall segment paired more than once: {segment_uid}')
                used_segments.add(segment_uid)
        return {'pair_count': len(seen_pairs), 'occupied_segment_count': len(used_segments)}

    def wall_line_snap_tolerance(line_geometries: list[LineString], config: ObstacleConfig) -> float:
        maximum_abs_coordinate = max((abs(float(value)) for line in line_geometries for value in line.bounds if math.isfinite(float(value))), default=0.0)
        return min(max(1e-06, maximum_abs_coordinate * 1e-12), max(1e-06, float(config.topology_line_snap_max_tolerance)))

    def build_wall_line_connectivity_index(segments: list[dict[str, Any]], config: ObstacleConfig) -> dict[str, Any]:
        """Precompute complete raw-line components for one floor+layer scope.

        Region halos select seed lines, then expand through this index so an outer
        edge of a large but connected closed wall face is not lost merely because
        it lies farther away than a fixed multiple of wall thickness.
        """
        line_geometries = [segment['line'] for segment in segments]
        line_tree = STRtree(line_geometries) if line_geometries else None
        line_index = {id(geometry): index for index, geometry in enumerate(line_geometries)}
        line_snap_tolerance = wall_line_snap_tolerance(line_geometries, config)
        parent = list(range(len(segments)))
        rank = [0] * len(segments)

        def find(node: int) -> int:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root == right_root:
                return
            if rank[left_root] < rank[right_root]:
                left_root, right_root = (right_root, left_root)
            parent[right_root] = left_root
            if rank[left_root] == rank[right_root]:
                rank[left_root] += 1
        if line_tree is not None:
            for index, line in enumerate(line_geometries):
                minx, miny, maxx, maxy = line.bounds
                search_box = box(minx - line_snap_tolerance, miny - line_snap_tolerance, maxx + line_snap_tolerance, maxy + line_snap_tolerance)
                for other_index in strtree_query_indices(line_tree, search_box, line_index):
                    if other_index <= index:
                        continue
                    try:
                        connected = bool(line.distance(line_geometries[other_index]) <= line_snap_tolerance)
                    except Exception:
                        connected = False
                    if connected:
                        union(index, other_index)
        component_indices_by_root: dict[int, list[int]] = defaultdict(list)
        root_by_index: list[int] = []
        for index in range(len(segments)):
            root = find(index)
            root_by_index.append(root)
            component_indices_by_root[root].append(index)
        return {'line_geometries': line_geometries, 'line_tree': line_tree, 'line_index': line_index, 'line_snap_tolerance': line_snap_tolerance, 'root_by_index': root_by_index, 'component_indices_by_root': dict(component_indices_by_root), 'segment_uid_to_index': {str(segment.get('segment_uid') or ''): index for index, segment in enumerate(segments)}}

    def assign_floor_pair_logical_component_uids(pair_records: list[dict[str, Any]], line_connectivity_index: dict[str, Any], *, floor_id: str, layer: str, config: ObstacleConfig) -> int:
        """Assign region-independent logical component IDs to final wall pairs."""
        if not pair_records:
            return 0
        parent = list(range(len(pair_records)))
        rank = [0] * len(pair_records)

        def find(node: int) -> int:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root == right_root:
                return
            if rank[left_root] < rank[right_root]:
                left_root, right_root = (right_root, left_root)
            parent[right_root] = left_root
            if rank[left_root] == rank[right_root]:
                rank[left_root] += 1
        line_tree = line_connectivity_index['line_tree']
        line_geometries = line_connectivity_index['line_geometries']
        line_index = line_connectivity_index['line_index']
        raw_roots_to_pairs: dict[int, list[int]] = defaultdict(list)
        support_geometries: list[Any] = []
        support_tolerances: list[float] = []
        for pair_index, record in enumerate(pair_records):
            try:
                support = unary_union([record['core_polygon'], record['full_polygon'], record['endpoint_support_polygon']])
            except Exception:
                support = record['endpoint_support_polygon']

            def usable_support(candidate: Any) -> bool:
                try:
                    bounds = tuple((float(value) for value in candidate.bounds))
                except Exception:
                    return False
                return not bool(getattr(candidate, 'is_empty', True)) and len(bounds) == 4 and all((math.isfinite(value) for value in bounds))
            if not usable_support(support):
                support = next((candidate for candidate in (record.get('full_polygon'), record.get('endpoint_support_polygon'), record.get('core_polygon'), record.get('line_a'), record.get('line_b')) if candidate is not None and usable_support(candidate)), GeometryCollection())
            support_geometries.append(support)
            tolerance = max(1e-06, float(record['width']) * config.topology_cap_search_tolerance_width_ratio)
            support_tolerances.append(tolerance)
            attached_raw_roots: set[int] = set()
            for segment_uid in pair_record_segment_key(record):
                segment_index = line_connectivity_index['segment_uid_to_index'].get(str(segment_uid))
                if segment_index is not None:
                    attached_raw_roots.add(int(line_connectivity_index['root_by_index'][int(segment_index)]))
            if line_tree is not None and (not support.is_empty):
                minx, miny, maxx, maxy = support.bounds
                search_box = box(minx - tolerance, miny - tolerance, maxx + tolerance, maxy + tolerance)
                for segment_index in strtree_query_indices(line_tree, search_box, line_index):
                    try:
                        if line_geometries[segment_index].distance(support) <= tolerance + 1e-09:
                            attached_raw_roots.add(int(line_connectivity_index['root_by_index'][segment_index]))
                    except Exception:
                        continue
            for raw_root in attached_raw_roots:
                raw_roots_to_pairs[raw_root].append(pair_index)
        for pair_indices in raw_roots_to_pairs.values():
            if len(pair_indices) < 2:
                continue
            anchor = pair_indices[0]
            for pair_index in pair_indices[1:]:
                union(anchor, pair_index)
        support_tree = STRtree(support_geometries)
        support_index = {id(geometry): index for index, geometry in enumerate(support_geometries)}
        maximum_tolerance = max(support_tolerances, default=1e-06)
        for pair_index, support in enumerate(support_geometries):
            if support.is_empty:
                continue
            minx, miny, maxx, maxy = support.bounds
            search_box = box(minx - maximum_tolerance, miny - maximum_tolerance, maxx + maximum_tolerance, maxy + maximum_tolerance)
            for other_index in strtree_query_indices(support_tree, search_box, support_index):
                if other_index <= pair_index:
                    continue
                tolerance = max(support_tolerances[pair_index], support_tolerances[other_index])
                try:
                    if support.distance(support_geometries[other_index]) <= tolerance + 1e-09:
                        union(pair_index, other_index)
                except Exception:
                    continue
        pairs_by_root: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for pair_index, record in enumerate(pair_records):
            pairs_by_root[find(pair_index)].append(record)
        for component_pairs in pairs_by_root.values():
            pair_uids = sorted((str(record.get('pair_uid') or record.get('pair_key') or '') for record in component_pairs))
            digest = hashlib.sha1('\n'.join(pair_uids).encode('utf-8')).hexdigest()[:16]
            logical_uid = f'{floor_id}|{layer}|TOPOPAIR_{digest}'
            for record in component_pairs:
                record['logical_component_uid'] = logical_uid
        return len(pairs_by_root)

    def build_regional_topology_components(pair_records: list[dict[str, Any]], segments: list[dict[str, Any]], *, floor_id: str, layer: str, region_id: str, config: ObstacleConfig) -> list[dict[str, Any]]:
        """Split a region+halo topology workset into deterministic components.

        Final pairs are component anchors.  Their two source segments are strongly
        connected; raw line intersections, full/support wall bands, and the same
        small cap tolerance used by topology attach L/T and end-cap evidence.  Raw
        components with no final-pair anchor are intentionally discarded so remote
        linework cannot participate in polygonization.
        """
        if not pair_records:
            return []
        segment_count = len(segments)
        pair_count = len(pair_records)
        node_count = segment_count + pair_count
        parent = list(range(node_count))
        rank = [0] * node_count

        def find(node: int) -> int:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root == right_root:
                return
            if rank[left_root] < rank[right_root]:
                left_root, right_root = (right_root, left_root)
            parent[right_root] = left_root
            if rank[left_root] == rank[right_root]:
                rank[left_root] += 1
        segment_uid_to_index = {str(segment.get('segment_uid') or ''): index for index, segment in enumerate(segments)}
        line_geometries = [segment['line'] for segment in segments]
        line_tree = STRtree(line_geometries) if line_geometries else None
        line_index = {id(geom): index for index, geom in enumerate(line_geometries)}
        line_snap_tolerance = wall_line_snap_tolerance(line_geometries, config)
        if line_tree is not None:
            for index, line in enumerate(line_geometries):
                minx, miny, maxx, maxy = line.bounds
                search_box = box(minx - line_snap_tolerance, miny - line_snap_tolerance, maxx + line_snap_tolerance, maxy + line_snap_tolerance)
                for other_index in strtree_query_indices(line_tree, search_box, line_index):
                    if other_index <= index:
                        continue
                    try:
                        connected = bool(line.distance(line_geometries[other_index]) <= line_snap_tolerance)
                    except Exception:
                        connected = False
                    if connected:
                        union(index, other_index)
        support_geometries: list[Any] = []
        support_tolerances: list[float] = []

        def has_finite_nonempty_bounds(geometry: Any) -> bool:
            """GEOS can collapse sub-micron wall bands at large CAD coordinates."""
            try:
                bounds = tuple((float(value) for value in geometry.bounds))
            except Exception:
                return False
            return not bool(getattr(geometry, 'is_empty', True)) and len(bounds) == 4 and all((math.isfinite(value) for value in bounds))
        for pair_index, record in enumerate(pair_records):
            pair_node = segment_count + pair_index
            for segment_uid in pair_record_segment_key(record):
                source_index = segment_uid_to_index.get(str(segment_uid))
                if source_index is not None:
                    union(pair_node, source_index)
            try:
                support = unary_union([record['core_polygon'], record['full_polygon'], record['endpoint_support_polygon']])
            except Exception:
                support = record['endpoint_support_polygon']
            if not has_finite_nonempty_bounds(support):
                support = next((candidate for candidate in (record['full_polygon'], record['endpoint_support_polygon'], record['core_polygon'], record.get('line_a'), record.get('line_b')) if candidate is not None and has_finite_nonempty_bounds(candidate)), GeometryCollection())
                record['topology_component_support_fallback'] = True
            support_geometries.append(support)
            tolerance = max(1e-06, float(record['width']) * config.topology_cap_search_tolerance_width_ratio)
            support_tolerances.append(tolerance)
            if line_tree is None or not has_finite_nonempty_bounds(support):
                continue
            minx, miny, maxx, maxy = support.bounds
            search_box = box(minx - tolerance, miny - tolerance, maxx + tolerance, maxy + tolerance)
            for raw_index in strtree_query_indices(line_tree, search_box, line_index):
                try:
                    distance = float(line_geometries[raw_index].distance(support))
                except Exception:
                    continue
                if distance <= tolerance + 1e-09:
                    union(pair_node, raw_index)
        support_tree = STRtree(support_geometries)
        support_index = {id(geom): index for index, geom in enumerate(support_geometries)}
        maximum_support_tolerance = max(support_tolerances, default=1e-06)
        for pair_index, support in enumerate(support_geometries):
            if not has_finite_nonempty_bounds(support):
                continue
            minx, miny, maxx, maxy = support.bounds
            search_box = box(minx - maximum_support_tolerance, miny - maximum_support_tolerance, maxx + maximum_support_tolerance, maxy + maximum_support_tolerance)
            for other_pair_index in strtree_query_indices(support_tree, search_box, support_index):
                if other_pair_index <= pair_index:
                    continue
                tolerance = max(support_tolerances[pair_index], support_tolerances[other_pair_index])
                try:
                    distance = float(support.distance(support_geometries[other_pair_index]))
                except Exception:
                    continue
                if distance <= tolerance + 1e-09:
                    union(segment_count + pair_index, segment_count + other_pair_index)
        try:
            core_union = unary_union([record['core_polygon'] for record in pair_records])
        except Exception:
            core_union = GeometryCollection()
        qualified_faces = polygonized_wall_faces(segments, core_union, config)
        pair_core_geometries = [record['core_polygon'] for record in pair_records]
        pair_core_tree = STRtree(pair_core_geometries)
        pair_core_index = {id(geometry): index for index, geometry in enumerate(pair_core_geometries)}
        qualified_face_associations: list[tuple[Polygon, int]] = []
        for face in qualified_faces:
            supporting_pair_indices_by_root: dict[int, list[int]] = defaultdict(list)
            for pair_index in strtree_query_indices(pair_core_tree, face, pair_core_index):
                try:
                    overlaps_core = face.intersection(pair_core_geometries[pair_index]).area > 0
                except Exception:
                    overlaps_core = False
                if overlaps_core:
                    preliminary_root = find(segment_count + pair_index)
                    supporting_pair_indices_by_root[preliminary_root].append(pair_index)
            if not supporting_pair_indices_by_root:
                continue
            eligible_roots: list[tuple[float, str, int, list[int]]] = []
            for preliminary_root, root_pair_indices in supporting_pair_indices_by_root.items():
                try:
                    root_core_union = unary_union([pair_core_geometries[index] for index in root_pair_indices])
                    root_core_overlap = float(face.intersection(root_core_union).area)
                except Exception:
                    continue
                if root_core_overlap <= 0:
                    continue
                if root_core_overlap / float(face.area) < config.topology_polygon_min_core_overlap_ratio:
                    continue
                stable_root_key = min((str(pair_records[index].get('pair_uid') or pair_records[index].get('pair_key') or '') for index in root_pair_indices))
                eligible_roots.append((root_core_overlap, stable_root_key, preliminary_root, root_pair_indices))
            if not eligible_roots:
                continue
            eligible_roots.sort(key=lambda item: (-item[0], item[1], item[2]))
            supporting_pair_indices = eligible_roots[0][3]
            face_segment_indices: list[int] = []
            if line_tree is not None:
                try:
                    face_boundary = face.boundary
                    boundary_band = face_boundary.buffer(line_snap_tolerance)
                except Exception:
                    face_boundary = GeometryCollection()
                    boundary_band = GeometryCollection()
                for raw_index in strtree_query_indices(line_tree, boundary_band.envelope, line_index):
                    try:
                        boundary_distance = float(line_geometries[raw_index].distance(face_boundary))
                        boundary_overlap_length = float(line_geometries[raw_index].intersection(boundary_band).length)
                        supports_face = boundary_distance <= line_snap_tolerance and boundary_overlap_length > line_snap_tolerance
                    except Exception:
                        supports_face = False
                    if supports_face:
                        face_segment_indices.append(raw_index)
            if not face_segment_indices:
                continue
            anchor_node = segment_count + supporting_pair_indices[0]
            for pair_index in supporting_pair_indices[1:]:
                union(anchor_node, segment_count + pair_index)
            for raw_index in face_segment_indices:
                union(anchor_node, raw_index)
            qualified_face_associations.append((face, supporting_pair_indices[0]))
        pairs_by_root: dict[int, list[dict[str, Any]]] = defaultdict(list)
        segments_by_root: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for pair_index, record in enumerate(pair_records):
            pairs_by_root[find(segment_count + pair_index)].append(record)
        for segment_index, segment in enumerate(segments):
            root = find(segment_index)
            if root in pairs_by_root:
                segments_by_root[root].append(segment)
        qualified_faces_by_root: dict[int, list[Polygon]] = defaultdict(list)
        for face, supporting_pair_index in qualified_face_associations:
            qualified_faces_by_root[find(segment_count + supporting_pair_index)].append(face)
        components: list[dict[str, Any]] = []
        seen_pair_uids: set[str] = set()
        for root, component_pairs in pairs_by_root.items():
            component_segments = list(segments_by_root.get(root, []))
            component_qualified_faces = qualified_faces_by_root.get(root, [])
            pair_uids = sorted((str(record.get('pair_uid') or record.get('pair_key') or '') for record in component_pairs))
            for pair_uid in pair_uids:
                if pair_uid in seen_pair_uids:
                    raise AssertionError(f'pair_uid assigned to multiple topology components: {pair_uid}')
                seen_pair_uids.add(pair_uid)
            identity_tokens = pair_uids + sorted((str(segment.get('segment_uid') or '') for segment in component_segments))
            digest = hashlib.sha1('\n'.join(identity_tokens).encode('utf-8')).hexdigest()[:16]
            inherited_logical_uids = sorted({str(record.get('logical_component_uid') or '') for record in component_pairs if str(record.get('logical_component_uid') or '')})
            if len(inherited_logical_uids) > 1:
                raise AssertionError(f'regional topology merged independently assigned floor-level logical components: {inherited_logical_uids}')
            if inherited_logical_uids:
                logical_component_uid = inherited_logical_uids[0]
            else:
                logical_digest = hashlib.sha1('\n'.join(pair_uids).encode('utf-8')).hexdigest()[:16]
                logical_component_uid = f'{floor_id}|{layer}|TOPOPAIR_{logical_digest}'
            component_uid = f'{floor_id}|{layer}|{region_id}|TOPO_{digest}'
            components.append({'component_uid': component_uid, 'logical_component_uid': logical_component_uid, 'pair_records': component_pairs, 'segments': component_segments, 'qualified_faces': component_qualified_faces, 'pair_count': len(component_pairs), 'segment_count': len(component_segments), 'support_fallback_count': sum((1 for record in component_pairs if record.get('topology_component_support_fallback') is True)), 'line_snap_tolerance': line_snap_tolerance, 'qualified_face_count': len(component_qualified_faces), 'qualified_face_anchor_count': len(component_qualified_faces)})
        expected_pair_uids = {str(record.get('pair_uid') or record.get('pair_key') or '') for record in pair_records}
        if seen_pair_uids != expected_pair_uids:
            raise AssertionError('topology component partition lost or duplicated final pairs')
        components.sort(key=lambda component: (str(component['component_uid']),))
        return components

    def complete_regional_wall_topology_components(pair_records: list[dict[str, Any]], segments: list[dict[str, Any]], *, floor_id: str, layer: str, region_id: str, config: ObstacleConfig) -> dict[str, Any]:
        """Run topology independently for every anchored regional component."""
        components = build_regional_topology_components(pair_records, segments, floor_id=floor_id, layer=layer, region_id=region_id, config=config)
        completed_pairs: list[dict[str, Any]] = []
        leftover_parts: list[Polygon] = []
        leftover_component_uid_by_geometry_id: dict[int, str] = {}
        component_diagnostics: list[dict[str, Any]] = []
        total_face_count = 0
        total_support_fallback_count = 0
        evidence_kind_counts: Counter[str] = Counter()
        component_face_unions: list[tuple[str, Any]] = []
        for component in components:
            component_uid = str(component['component_uid'])
            component_pairs = component['pair_records']
            component_segments = component['segments']
            logical_component_uid = str(component.get('logical_component_uid') or '')
            if not logical_component_uid:
                logical_pair_uids = sorted((str(record.get('pair_uid') or record.get('pair_key') or '') for record in component_pairs))
                logical_digest = hashlib.sha1('\n'.join(logical_pair_uids).encode('utf-8')).hexdigest()[:16]
                logical_component_uid = f'{floor_id}|{layer}|TOPOPAIR_{logical_digest}'
            result = complete_layer_wall_pair_topology(component_pairs, component_segments, config, polygonized_faces_override=component['qualified_faces'])
            total_face_count += int(result['polygonized_face_count'])
            total_support_fallback_count += int(component['support_fallback_count'])
            for record in component_pairs:
                record['topology_component_uid'] = component_uid
                evidence_kind_counts.update(record.get('topology_evidence_kinds') or [])
                completed_pairs.append(record)
            try:
                component_face_union = unary_union(component['qualified_faces']) if component['qualified_faces'] else GeometryCollection()
            except Exception:
                component_face_union = GeometryCollection()
            if not component_face_union.is_empty:
                component_face_unions.append((component_uid, component_face_union))
            polygonized_face_uids = sorted({stable_wall_face_uid(floor_id, layer, face) for face in component['qualified_faces']})
            if len(polygonized_face_uids) != int(result['polygonized_face_count']):
                raise AssertionError('qualified topology faces did not receive unique stable identities')
            component_diagnostics.append({'component_uid': component_uid, 'logical_component_uid': logical_component_uid, 'pair_count': int(component['pair_count']), 'segment_count': int(component['segment_count']), 'support_fallback_count': int(component['support_fallback_count']), 'line_snap_tolerance': float(component['line_snap_tolerance']), 'qualified_face_count': int(component['qualified_face_count']), 'qualified_face_anchor_count': int(component['qualified_face_anchor_count']), 'polygonized_face_count': int(result['polygonized_face_count']), 'polygonized_face_uids': polygonized_face_uids, 'leftover_part_count': 0, 'topology_evidence_kind_counts': dict(Counter((kind for record in component_pairs for kind in record.get('topology_evidence_kinds') or [])))})
        try:
            qualified_face_union = unary_union([geometry for _uid, geometry in component_face_unions])
            enhanced_pair_union = unary_union([record['enhanced_polygon'] for record in completed_pairs])
            regional_leftover = safe_polygonal_difference(qualified_face_union, enhanced_pair_union)
        except Exception:
            regional_leftover = GeometryCollection()
        claimed_leftover_union: Any = GeometryCollection()
        leftover_counts: Counter[str] = Counter()
        for component_uid, component_face_union in component_face_unions:
            try:
                component_leftover = regional_leftover.intersection(component_face_union)
            except Exception:
                continue
            if not claimed_leftover_union.is_empty:
                component_leftover = safe_polygonal_difference(component_leftover, claimed_leftover_union)
            for part in polygon_parts(component_leftover):
                if part.area <= config.min_area:
                    continue
                leftover_parts.append(part)
                leftover_component_uid_by_geometry_id[id(part)] = component_uid
                leftover_counts[component_uid] += 1
            try:
                claimed_leftover_union = component_leftover if claimed_leftover_union.is_empty else unary_union([claimed_leftover_union, component_leftover])
            except Exception:
                pass
        for item in component_diagnostics:
            item['leftover_part_count'] = int(leftover_counts[str(item['component_uid'])])
        completed_pair_uids = [str(record.get('pair_uid') or record.get('pair_key') or '') for record in completed_pairs]
        if len(completed_pair_uids) != len(set(completed_pair_uids)):
            raise AssertionError('regional topology emitted a duplicate pair_uid')
        return {'pair_records': completed_pairs, 'leftover_parts': leftover_parts, 'leftover_component_uid_by_geometry_id': leftover_component_uid_by_geometry_id, 'polygonized_face_count': total_face_count, 'support_fallback_count': total_support_fallback_count, 'component_count': len(components), 'component_diagnostics': component_diagnostics, 'topology_evidence_kind_counts': dict(evidence_kind_counts)}

    def detect_parallel_wall_line_obstacles(rows: list[dict[str, str]], regions: list[dict[str, Any]], decisions: dict[str, dict[str, Any]], config: ObstacleConfig, diagnostics: dict[str, Any] | None=None) -> list[dict[str, Any]]:
        """Build floor-isolated walls through region+halo worksets.

        Pair ownership and segment occupancy are resolved once per floor/layer.
        Region worksets only reduce candidate/topology inputs and determine final
        clipping; they can never create a second pairing of the same source line.
        """
        stats = diagnostics if diagnostics is not None else {}
        stats.clear()
        stats.update({'extracted_segment_count': 0, 'assigned_segment_count': 0, 'unassigned_segment_count': 0, 'raw_candidate_count': 0, 'deduplicated_candidate_count': 0, 'candidate_duplicate_occurrence_count': 0, 'halo_ghost_candidate_count': 0, 'topology_only_candidate_count': 0, 'discarded_halo_ghost_candidate_count': 0, 'provisional_pair_count': 0, 'topology_only_provisional_pair_count': 0, 'final_pair_count': 0, 'topology_only_final_pair_count': 0, 'max_segment_degree': 0, 'cross_floor_pair_count': 0, 'cross_layer_pair_count': 0, 'duplicate_pair_uid_count': 0, 'invalid_output_geometry_skipped': 0, 'topology_component_count': 0, 'topology_component_instance_count': 0, 'polygonized_face_count': 0, 'polygonized_face_instance_count': 0, 'topology_count_semantics': 'unique_pair_anchored_components; regional instance counts are reported separately', 'topology_support_fallback_count': 0, 'topology_evidence_kind_counts': {}, 'sparse_rejected_pairs': [], 'periodic_candidate_rejected_count': 0, 'periodic_rejected_pairs': [], 'width_gate_counts': {'evaluated': 0, 'passed': 0, 'rejected': 0, 'sparse': 0, 'sparse_rejected': 0, 'local': 0, 'fallback': 0}, 'floor_layer_scopes': {}})
        floor_contexts = _floor_region_contexts(regions)
        wall_segments_by_scope: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row_index, row in enumerate(rows):
            if not row_has_wall_semantic(row, decisions):
                continue
            layer = str(row.get('layer') or '')
            if layer_has_opening_semantics(layer):
                continue
            for local_segment_index, segment in enumerate(open_line_segments_from_row(row, config)):
                stats['extracted_segment_count'] += 1
                angle = line_angle_deg_mod_180(segment)
                vectors = line_unit_vectors(segment)
                if angle is None or vectors is None:
                    stats['unassigned_segment_count'] += 1
                    continue
                floor_id = _segment_floor_owner(row, segment, floor_contexts, config)
                if floor_id is None:
                    stats['unassigned_segment_count'] += 1
                    continue
                stats['assigned_segment_count'] += 1
                wall_segments_by_scope[floor_id, layer].append({'floor_id': floor_id, 'layer': layer, 'row': row, 'line': segment, 'angle': angle, 'direction': vectors[0], 'normal': vectors[1], 'segment_uid': stable_wall_segment_uid(row, segment, row_index=row_index, segment_index=local_segment_index)})
        out: list[dict[str, Any]] = []
        global_logical_component_face_counts: dict[str, int] = {}
        global_polygonized_face_uids: set[str] = set()
        for (floor_id, layer), segments in sorted(wall_segments_by_scope.items()):
            scope_key = f'{floor_id}::{layer}'
            scope_stats: dict[str, Any] = {'floor_id': floor_id, 'layer': layer, 'segment_count': len(segments), 'raw_candidate_count': 0, 'deduplicated_candidate_count': 0, 'halo_ghost_candidate_count': 0, 'topology_only_candidate_count': 0, 'discarded_halo_ghost_candidate_count': 0, 'provisional_pair_count': 0, 'topology_only_provisional_pair_count': 0, 'final_pair_count': 0, 'topology_only_final_pair_count': 0, 'max_segment_degree': 0, 'cross_floor_pair_count': 0, 'cross_layer_pair_count': 0, 'duplicate_pair_uid_count': 0, 'width_gate_counts': {'evaluated': 0, 'passed': 0, 'rejected': 0, 'sparse': 0, 'sparse_rejected': 0, 'local': 0, 'fallback': 0}, 'topology_halo': 0.0, 'topology_component_count': 0, 'topology_component_instance_count': 0, 'polygonized_face_count': 0, 'polygonized_face_instance_count': 0, 'topology_support_fallback_count': 0, 'topology_evidence_kind_counts': {}, 'sparse_rejected_pairs': [], 'periodic_candidate_rejected_count': 0, 'periodic_rejected_pairs': [], 'invalid_output_geometry_skipped': 0, 'regions': {}}
            stats['floor_layer_scopes'][scope_key] = scope_stats
            scope_logical_component_face_counts: dict[str, int] = {}
            scope_polygonized_face_uids: set[str] = set()
            if len(segments) < 2:
                continue
            segments = sorted(segments, key=lambda item: (round(item['angle'] / config.parallel_angle_tolerance_deg), item['line'].bounds, item['segment_uid']))
            for index, segment in enumerate(segments):
                segment['segment_index'] = index
            periodic_segment_index = build_periodic_segment_direction_buckets(segments, config)
            floor_regions = floor_contexts[floor_id]['regions']
            candidate_by_uid: dict[str, dict[str, Any]] = {}
            if config.regional_wall_processing_enabled:
                candidate_worksets = [(str(region['full_region_id']), [segment for segment in segments if _segment_intersects_region_halo(segment, region, config)]) for region in floor_regions]
            else:
                candidate_worksets = [(f'{floor_id}:FULL_FLOOR', segments)]
            for candidate_region_id, workset_segments in candidate_worksets:
                records = _enumerate_wall_pair_candidates(workset_segments, floor_id=floor_id, layer=layer, candidate_region_id=candidate_region_id, config=config)
                stats['raw_candidate_count'] += len(records)
                scope_stats['raw_candidate_count'] += len(records)
                for record in records:
                    pair_uid = str(record['pair_uid'])
                    existing = candidate_by_uid.get(pair_uid)
                    if existing is None:
                        candidate_by_uid[pair_uid] = record
                    else:
                        existing['candidate_region_ids'].update(record['candidate_region_ids'])
            candidate_records = list(candidate_by_uid.values())
            stats['deduplicated_candidate_count'] += len(candidate_records)
            scope_stats['deduplicated_candidate_count'] = len(candidate_records)
            duplicate_occurrences = max(0, int(scope_stats['raw_candidate_count']) - len(candidate_records))
            stats['candidate_duplicate_occurrence_count'] += duplicate_occurrences
            if not candidate_records:
                continue
            for record in candidate_records:
                _assign_pair_region_owner(record, floor_regions, config)
            halo_ghost_candidate_count = sum((1 for record in candidate_records if not str(record.get('core_owner_region_id') or '')))
            topology_only_candidate_count = sum((1 for record in candidate_records if record.get('pair_routing') == 'topology_only'))
            discarded_halo_ghost_candidate_count = sum((1 for record in candidate_records if record.get('pair_routing') == 'halo_ghost'))
            stats['halo_ghost_candidate_count'] += halo_ghost_candidate_count
            scope_stats['halo_ghost_candidate_count'] = halo_ghost_candidate_count
            stats['topology_only_candidate_count'] += topology_only_candidate_count
            scope_stats['topology_only_candidate_count'] = topology_only_candidate_count
            stats['discarded_halo_ghost_candidate_count'] += discarded_halo_ghost_candidate_count
            scope_stats['discarded_halo_ghost_candidate_count'] = discarded_halo_ghost_candidate_count
            candidate_records = [record for record in candidate_records if record.get('pair_routing') != 'halo_ghost']
            if not candidate_records:
                continue
            non_periodic_candidates: list[dict[str, Any]] = []
            for record in candidate_records:
                periodic_rejected, periodic_evidence = periodic_parallel_pair_pattern(record, periodic_segment_index, config)
                record['periodic_parallel_model_evidence'] = periodic_evidence
                if periodic_rejected:
                    rejected_entry = {'pair_uid': str(record.get('pair_uid') or ''), 'floor_id': floor_id, 'layer': layer, 'owner_region_id': str(record.get('owner_region_id') or ''), 'width': float(record['width']), 'evidence': periodic_evidence}
                    stats['periodic_candidate_rejected_count'] += 1
                    scope_stats['periodic_candidate_rejected_count'] += 1
                    stats['periodic_rejected_pairs'].append(rejected_entry)
                    scope_stats['periodic_rejected_pairs'].append(rejected_entry)
                    continue
                non_periodic_candidates.append(record)
            candidate_records = non_periodic_candidates
            if not candidate_records:
                continue
            provisional_pairs = select_core_owner_then_topology_only_pairs(candidate_records)
            validate_wall_pair_invariants(provisional_pairs, floor_id=floor_id, layer=layer)
            stats['provisional_pair_count'] += len(provisional_pairs)
            scope_stats['provisional_pair_count'] = len(provisional_pairs)
            topology_only_provisional_pair_count = sum((1 for record in provisional_pairs if record.get('pair_routing') == 'topology_only'))
            stats['topology_only_provisional_pair_count'] += topology_only_provisional_pair_count
            scope_stats['topology_only_provisional_pair_count'] = topology_only_provisional_pair_count
            core_reference_pairs = [record for record in provisional_pairs if record.get('pair_routing') == 'core']
            width_reference_pairs = core_reference_pairs or provisional_pairs
            width_reference_index = build_wall_width_reference_index(width_reference_pairs, config)
            gated_candidates: list[dict[str, Any]] = []
            for record in candidate_records:
                width_passed, width_evidence = wall_width_model_gate(record, width_reference_pairs, config, reference_index=width_reference_index)
                record['wall_width_model_evidence'] = width_evidence
                gate_counts = stats['width_gate_counts']
                scope_gate_counts = scope_stats['width_gate_counts']
                gate_counts['evaluated'] += 1
                scope_gate_counts['evaluated'] += 1
                gate_counts['passed' if width_passed else 'rejected'] += 1
                scope_gate_counts['passed' if width_passed else 'rejected'] += 1
                if 'sparse' in width_evidence:
                    gate_counts['sparse'] += 1
                    scope_gate_counts['sparse'] += 1
                if width_evidence.startswith('rejected_sparse_overwide_outlier'):
                    gate_counts['sparse_rejected'] += 1
                    scope_gate_counts['sparse_rejected'] += 1
                    rejected_entry = {'pair_uid': str(record.get('pair_uid') or ''), 'floor_id': floor_id, 'layer': layer, 'owner_region_id': str(record.get('owner_region_id') or ''), 'width': float(record['width']), 'evidence': width_evidence}
                    stats['sparse_rejected_pairs'].append(rejected_entry)
                    scope_stats['sparse_rejected_pairs'].append(rejected_entry)
                if 'scope=local_directional' in width_evidence:
                    gate_counts['local'] += 1
                    scope_gate_counts['local'] += 1
                elif 'scope=directional_knn_fallback' in width_evidence:
                    gate_counts['fallback'] += 1
                    scope_gate_counts['fallback'] += 1
                if width_passed:
                    gated_candidates.append(record)
            pair_records = select_core_owner_then_topology_only_pairs(gated_candidates)
            if not pair_records:
                continue
            pair_uid_counts = Counter((str(record.get('pair_uid') or record.get('pair_key') or '') for record in pair_records))
            duplicate_pair_uid_count = sum((count - 1 for count in pair_uid_counts.values() if count > 1))
            cross_floor_pair_count = sum((1 for record in pair_records if str(record.get('floor_id') or '') != str(floor_id)))
            cross_layer_pair_count = sum((1 for record in pair_records if str(record.get('layer') or '') != str(layer)))
            scope_stats['duplicate_pair_uid_count'] = duplicate_pair_uid_count
            scope_stats['cross_floor_pair_count'] = cross_floor_pair_count
            scope_stats['cross_layer_pair_count'] = cross_layer_pair_count
            stats['duplicate_pair_uid_count'] += duplicate_pair_uid_count
            stats['cross_floor_pair_count'] += cross_floor_pair_count
            stats['cross_layer_pair_count'] += cross_layer_pair_count
            validate_wall_pair_invariants(pair_records, floor_id=floor_id, layer=layer)
            stats['final_pair_count'] += len(pair_records)
            scope_stats['final_pair_count'] = len(pair_records)
            topology_only_final_pair_count = sum((1 for record in pair_records if record.get('pair_routing') == 'topology_only'))
            stats['topology_only_final_pair_count'] += topology_only_final_pair_count
            scope_stats['topology_only_final_pair_count'] = topology_only_final_pair_count
            segment_degrees = Counter((segment_uid for record in pair_records for segment_uid in pair_record_segment_key(record)))
            maximum_degree = max(segment_degrees.values(), default=0)
            scope_stats['max_segment_degree'] = maximum_degree
            stats['max_segment_degree'] = max(int(stats['max_segment_degree']), maximum_degree)
            decision = decisions.get(layer, {})
            line_connectivity_index = build_wall_line_connectivity_index(segments, config)
            scope_stats['raw_line_component_count'] = len(line_connectivity_index['component_indices_by_root'])
            scope_stats['line_snap_tolerance'] = float(line_connectivity_index['line_snap_tolerance'])
            scope_stats['floor_logical_component_count'] = assign_floor_pair_logical_component_uids(pair_records, line_connectivity_index, floor_id=floor_id, layer=layer, config=config)
            maximum_width = max((float(record['width']) for record in pair_records))
            topology_halo_multiplier = max(1.0, config.regional_topology_halo_width_ratio)
            scope_stats['topology_halo'] = 0.0
            scope_stats['maximum_final_wall_width'] = maximum_width
            for region in floor_regions:
                region_id = str(region.get('full_region_id') or '')
                relevant_pair_records: list[dict[str, Any]] = []
                relevant_pair_halos: list[float] = []
                for record in pair_records:
                    pair_halo = float(record['width']) * topology_halo_multiplier
                    try:
                        distance_to_region = min(float(record['full_polygon'].distance(region['polygon'])), float(record['endpoint_support_polygon'].distance(region['polygon'])))
                    except Exception:
                        continue
                    if distance_to_region <= pair_halo + 1e-09:
                        relevant_pair_records.append(record)
                        relevant_pair_halos.append(pair_halo)
                topology_halo = max(relevant_pair_halos, default=0.0)
                scope_stats['topology_halo'] = max(float(scope_stats['topology_halo']), topology_halo)
                try:
                    topology_scope = region['polygon'].buffer(topology_halo)
                except Exception:
                    topology_scope = region['polygon']
                local_pair_records = [dict(record) for record in relevant_pair_records]
                if not local_pair_records:
                    scope_stats['regions'][region_id] = {'topology_halo': topology_halo, 'pair_workset_count': 0, 'segment_seed_count': 0, 'connected_line_component_seed_count': 0, 'segment_workset_count': 0, 'component_count': 0, 'polygonized_face_count': 0, 'topology_evidence_kind_counts': {}, 'components': [], 'invalid_output_geometry_skipped': 0}
                    continue
                line_tree = line_connectivity_index['line_tree']
                line_geometries = line_connectivity_index['line_geometries']
                line_snap_tolerance = float(line_connectivity_index['line_snap_tolerance'])
                try:
                    seed_query_geometry = topology_scope.buffer(line_snap_tolerance)
                except Exception:
                    seed_query_geometry = topology_scope
                seed_segment_indices: set[int] = set()
                if line_tree is not None:
                    for segment_index in strtree_query_indices(line_tree, seed_query_geometry, line_connectivity_index['line_index']):
                        try:
                            if line_geometries[segment_index].distance(topology_scope) <= line_snap_tolerance:
                                seed_segment_indices.add(segment_index)
                        except Exception:
                            continue
                for record in relevant_pair_records:
                    for segment_uid in pair_record_segment_key(record):
                        segment_index = line_connectivity_index['segment_uid_to_index'].get(str(segment_uid))
                        if segment_index is not None:
                            seed_segment_indices.add(int(segment_index))
                seed_component_roots = {line_connectivity_index['root_by_index'][segment_index] for segment_index in seed_segment_indices}
                expanded_segment_indices = sorted({segment_index for component_root in seed_component_roots for segment_index in line_connectivity_index['component_indices_by_root'].get(component_root, [])})
                local_segments = [segments[index] for index in expanded_segment_indices]
                region_stats = {'topology_halo': topology_halo, 'pair_workset_count': len(local_pair_records), 'segment_seed_count': len(seed_segment_indices), 'connected_line_component_seed_count': len(seed_component_roots), 'segment_workset_count': len(local_segments), 'component_count': 0, 'polygonized_face_count': 0, 'support_fallback_count': 0, 'topology_evidence_kind_counts': {}, 'components': [], 'invalid_output_geometry_skipped': 0}
                scope_stats['regions'][region_id] = region_stats
                topology_result = complete_regional_wall_topology_components(local_pair_records, local_segments, floor_id=floor_id, layer=layer, region_id=region_id, config=config)
                local_pair_records = topology_result['pair_records']
                region_stats['component_count'] = int(topology_result['component_count'])
                region_stats['polygonized_face_count'] = int(topology_result['polygonized_face_count'])
                region_stats['support_fallback_count'] = int(topology_result['support_fallback_count'])
                region_stats['topology_evidence_kind_counts'] = topology_result['topology_evidence_kind_counts']
                region_stats['components'] = topology_result['component_diagnostics']
                stats['topology_component_instance_count'] += int(topology_result['component_count'])
                stats['polygonized_face_instance_count'] += int(topology_result['polygonized_face_count'])
                stats['topology_support_fallback_count'] += int(topology_result['support_fallback_count'])
                scope_stats['topology_component_instance_count'] += int(topology_result['component_count'])
                scope_stats['polygonized_face_instance_count'] += int(topology_result['polygonized_face_count'])
                scope_stats['topology_support_fallback_count'] += int(topology_result['support_fallback_count'])
                for component_item in topology_result['component_diagnostics']:
                    logical_uid = str(component_item.get('logical_component_uid') or '')
                    if not logical_uid:
                        continue
                    face_count = int(component_item.get('polygonized_face_count') or 0)
                    scope_logical_component_face_counts[logical_uid] = max(scope_logical_component_face_counts.get(logical_uid, 0), face_count)
                    global_logical_component_face_counts[logical_uid] = max(global_logical_component_face_counts.get(logical_uid, 0), face_count)
                    face_uids = {str(face_uid) for face_uid in component_item.get('polygonized_face_uids') or [] if str(face_uid)}
                    if len(face_uids) != face_count:
                        raise AssertionError('topology component face count does not match stable face identities')
                    scope_polygonized_face_uids.update(face_uids)
                    global_polygonized_face_uids.update(face_uids)
                global_evidence_counts = Counter(stats['topology_evidence_kind_counts'])
                global_evidence_counts.update(topology_result['topology_evidence_kind_counts'])
                stats['topology_evidence_kind_counts'] = dict(global_evidence_counts)
                scope_evidence_counts = Counter(scope_stats['topology_evidence_kind_counts'])
                scope_evidence_counts.update(topology_result['topology_evidence_kind_counts'])
                scope_stats['topology_evidence_kind_counts'] = dict(scope_evidence_counts)
                for record in local_pair_records:
                    row_a = record['row_a']
                    row_b = record['row_b']
                    polygon = record['enhanced_polygon']
                    topology_added = record['topology_added_polygon']
                    evidence_kinds = list(record.get('topology_evidence_kinds') or [])
                    was_completed = not topology_added.is_empty and topology_added.area > config.min_area
                    try:
                        if not polygon.intersects(region['polygon']):
                            continue
                        clipped = polygon.intersection(region['polygon'])
                    except Exception:
                        stats['invalid_output_geometry_skipped'] += 1
                        scope_stats['invalid_output_geometry_skipped'] += 1
                        region_stats['invalid_output_geometry_skipped'] += 1
                        continue
                    for part in polygon_parts(clipped):
                        if part.area <= config.min_area or part.is_empty or (not part.is_valid):
                            stats['invalid_output_geometry_skipped'] += 1
                            scope_stats['invalid_output_geometry_skipped'] += 1
                            region_stats['invalid_output_geometry_skipped'] += 1
                            continue
                        review_topology_geometry = None
                        if was_completed:
                            try:
                                review_topology_geometry = topology_added.intersection(part.buffer(1e-06))
                                if review_topology_geometry.is_empty:
                                    review_topology_geometry = None
                            except Exception:
                                review_topology_geometry = None
                        reason = 'llm_wall_layer_parallel_line_bundle_clipped_to_inspection_region'
                        fusion_decision = 'accepted_by_llm_wall_layer_and_parallel_line_bundle'
                        if was_completed:
                            reason = 'llm_wall_layer_parallel_line_bundle_topology_completed_clipped_to_inspection_region'
                            fusion_decision = 'accepted_by_parallel_core_and_wall_topology_completion'
                        add_obstacle(out, row=row_a, region=region, geom=part, obstacle_type='wall', reason=reason, confidence=float(decision.get('confidence') or 0.9), semantic_evidence=semantic_evidence_for_row(row_a, decisions), geometry_evidence=f"parallel_wall_line_bundle;paired_object_id={row_b.get('object_id', '')};paired_layer={row_b.get('layer', '')};floor_scope={floor_id};segment_uid={record.get('segment_a_uid', '')};paired_segment_uid={record.get('segment_b_uid', '')};pair_uid={record.get('pair_uid', '')};topology_component_uid={record.get('topology_component_uid', '')};owner_region={record.get('owner_region_id', '')};pair_routing={record.get('pair_routing', '')};core_outside_halo_candidates={halo_ghost_candidate_count};topology_only_candidates={topology_only_candidate_count};discarded_halo_ghost_candidates={discarded_halo_ghost_candidate_count};wall_width={float(record['width']):.6f};projection_core_length={float(record['overlap']):.6f};source_union_length={float(record['union_interval'][1] - record['union_interval'][0]):.6f};topology_added_area={float(topology_added.area):.6f};pair_selection={record.get('pair_selection_evidence', '')};wall_width_model={record.get('wall_width_model_evidence', '')};periodic_parallel_model={record.get('periodic_parallel_model_evidence', '')};non_dashed_open_straight_lines", topology_evidence='floor_layer_hard_isolation;region_halo_topology;connected_topology_component;direction_parallel;projection_overlap_core;trusted_wall_layer;mutual_best_one_to_one;local_directional_wall_width_model' + (';' + ';'.join(evidence_kinds) if evidence_kinds else ''), fusion_decision=fusion_decision, review_topology_geometry=review_topology_geometry)
                representative_row = local_pair_records[0]['row_a']
                for topology_part in topology_result['leftover_parts']:
                    component_uid = str(topology_result['leftover_component_uid_by_geometry_id'].get(id(topology_part), ''))
                    try:
                        if not topology_part.intersects(region['polygon']):
                            continue
                        clipped = topology_part.intersection(region['polygon'])
                    except Exception:
                        stats['invalid_output_geometry_skipped'] += 1
                        scope_stats['invalid_output_geometry_skipped'] += 1
                        region_stats['invalid_output_geometry_skipped'] += 1
                        continue
                    for part in polygon_parts(clipped):
                        if part.area <= config.min_area or part.is_empty or (not part.is_valid):
                            stats['invalid_output_geometry_skipped'] += 1
                            scope_stats['invalid_output_geometry_skipped'] += 1
                            region_stats['invalid_output_geometry_skipped'] += 1
                            continue
                        add_obstacle(out, row=representative_row, region=region, geom=part, obstacle_type='wall', reason='llm_wall_layer_topology_polygon_completion_clipped_to_inspection_region', confidence=float(decision.get('confidence') or 0.9), semantic_evidence=semantic_evidence_for_row(representative_row, decisions), geometry_evidence=f"same_floor_layer_region_halo_noded_wall_linework;floor_scope={floor_id};topology_component_uid={component_uid};polygonized_face_count={int(topology_result['polygonized_face_count'])};minimum_core_overlap_ratio={config.topology_polygon_min_core_overlap_ratio:.3f}", topology_evidence='floor_layer_hard_isolation;region_halo_topology;connected_topology_component;wall_line_connectivity;closed_boundary;L_T_or_end_cap_completion', fusion_decision='accepted_by_polygonized_wall_face_overlapping_parallel_cores', review_topology_geometry=part)
            scope_stats['topology_component_count'] = len(scope_logical_component_face_counts)
            scope_stats['polygonized_face_count'] = len(scope_polygonized_face_uids)
        stats['topology_component_count'] = len(global_logical_component_face_counts)
        stats['polygonized_face_count'] = len(global_polygonized_face_uids)
        return out

    def line_endpoint_pair(line: LineString) -> tuple[tuple[float, float], tuple[float, float]]:
        coords = list(line.coords)
        return ((float(coords[0][0]), float(coords[0][1])), (float(coords[-1][0]), float(coords[-1][1])))

    def obstacle_csv_row(item: dict[str, Any]) -> dict[str, Any]:
        row = dict(item)
        row.pop('geometry', None)
        row.pop('review_topology_geometry', None)
        return row

    def write_obstacle_csv(path: Path, obstacles: list[dict[str, Any]]) -> None:
        fields = ['obstacle_id', 'object_id', 'handle', 'source', 'sheet_id', 'floor_id', 'source_floor_id', 'building_scope_id', 'building_id', 'building_name', 'floor_name', 'region_id', 'parent_region_id', 'full_region_id', 'obstacle_type', 'reason', 'confidence', 'layer', 'entity_type', 'geometry_kind', 'color', 'linetype', 'parent_block_name', 'semantic_evidence', 'geometry_evidence', 'topology_evidence', 'negative_evidence', 'fusion_decision', 'bbox_minx', 'bbox_miny', 'bbox_maxx', 'bbox_maxy', 'area']
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for item in obstacles:
                row = obstacle_csv_row(item)
                writer.writerow({field: row.get(field, '') for field in fields})

    def normalize_valid_polygonal_geometry(geometry: Any, *, context: str='polygonal_geometry') -> Any:
        """Return a valid, nonempty, positive-area polygonal geometry or fail."""
        if geometry is None or getattr(geometry, 'is_empty', True):
            raise ValueError(f'{context}: polygonal geometry is empty')
        candidate = geometry
        if not bool(getattr(candidate, 'is_valid', False)):
            try:
                candidate = shapely_make_valid(candidate) if shapely_make_valid is not None else candidate.buffer(0)
            except Exception as exc:
                raise ValueError(f'{context}: geometry repair failed: {exc}') from exc
        cleaned_parts: list[Polygon] = []
        for part in polygon_parts(candidate):
            repaired = part
            if repaired.is_empty or repaired.area <= 0:
                continue
            if not repaired.is_valid:
                try:
                    repaired = shapely_make_valid(repaired) if shapely_make_valid is not None else repaired.buffer(0)
                except Exception as exc:
                    raise ValueError(f'{context}: polygon part repair failed: {exc}') from exc
            for polygon in polygon_parts(repaired):
                if not polygon.is_empty and polygon.area > 0:
                    cleaned_parts.append(polygon)
        if not cleaned_parts:
            raise ValueError(f'{context}: repair produced no polygonal area')
        try:
            normalized = unary_union(cleaned_parts)
        except Exception as exc:
            raise ValueError(f'{context}: polygonal normalization union failed: {exc}') from exc
        if not normalized.is_valid:
            try:
                normalized = shapely_make_valid(normalized) if shapely_make_valid is not None else normalized.buffer(0)
                normalized_parts = [part for part in polygon_parts(normalized) if not part.is_empty and part.area > 0]
                normalized = unary_union(normalized_parts) if normalized_parts else GeometryCollection()
                if not normalized.is_valid:
                    normalized = normalized.buffer(0)
            except Exception as exc:
                raise ValueError(f'{context}: final validity repair failed: {exc}') from exc
        if normalized.is_empty or not normalized.is_valid or float(getattr(normalized, 'area', 0.0) or 0.0) <= 0 or (str(getattr(normalized, 'geom_type', '')) not in {'Polygon', 'MultiPolygon'}):
            raise ValueError(f"{context}: normalized union is not valid positive polygonal geometry; type={getattr(normalized, 'geom_type', '')}; valid={getattr(normalized, 'is_valid', False)}; area={float(getattr(normalized, 'area', 0.0) or 0.0):.6f}")
        return normalized

    def valid_polygonal_union(geometries: list[Any], *, context: str='polygonal_union') -> Any:
        """Normalize inputs and their union; never silently replace failure by empty."""
        if not geometries:
            raise ValueError(f'{context}: no input geometries')
        normalized_inputs = [normalize_valid_polygonal_geometry(geometry, context=f'{context}:input[{index}]') for index, geometry in enumerate(geometries)]
        try:
            merged = unary_union(normalized_inputs)
        except Exception as exc:
            raise ValueError(f'{context}: unary_union failed: {exc}') from exc
        return normalize_valid_polygonal_geometry(merged, context=f'{context}:result')

    def write_geojson_outputs(output_dir: Path, obstacles: list[dict[str, Any]]) -> tuple[list[Path], list[Path]]:
        per_region_dir = output_dir / 'per_region_geojson'
        per_floor_dir = output_dir / 'per_floor_union'
        per_region_dir.mkdir(parents=True, exist_ok=True)
        per_floor_dir.mkdir(parents=True, exist_ok=True)
        per_region: dict[str, list[dict[str, Any]]] = defaultdict(list)
        per_floor: dict[str, list[Any]] = defaultdict(list)
        for item in obstacles:
            per_region[item['full_region_id']].append(item)
            per_floor[str(item.get('floor_id') or item.get('sheet_id') or 'UNKNOWN')].append(item['geometry'])
        region_paths: list[Path] = []
        for region_id, items in per_region.items():
            safe = re.sub('[^0-9A-Za-z_.-]+', '_', region_id)
            path = per_region_dir / f'{safe}.geojson'
            write_json(path, {'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'properties': obstacle_csv_row(item), 'geometry': mapping(item['geometry'])} for item in items]})
            region_paths.append(path)
        union_paths: list[Path] = []
        for floor_id, geoms in per_floor.items():
            path = per_floor_dir / f'valid_obstacle_union_{floor_id}.geojson'
            floor_union = valid_polygonal_union(geoms, context=f'floor_union:{floor_id}')
            write_json(path, {'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'properties': {'floor_id': floor_id, 'obstacle_count': len(geoms)}, 'geometry': mapping(floor_union)}]})
            union_paths.append(path)
        return (region_paths, union_paths)

    def add_layer(doc: Any, name: str, color: int) -> None:
        if name not in doc.layers:
            doc.layers.add(name=name, color=color)
        try:
            doc.layers.get(name).dxf.color = color
        except Exception:
            pass

    def add_label(msp: Any, text: str, point: tuple[float, float], height: float, layer: str, color: int) -> None:
        entity = msp.add_text(text, dxfattribs={'layer': layer, 'height': height, 'color': color})
        try:
            entity.set_placement(point)
        except Exception:
            entity.dxf.insert = point

    def paired_object_ids_from_evidence(value: Any) -> list[str]:
        return [item.strip() for item in re.findall('paired_object_id=([^;]+)', str(value or '')) if item.strip()]

    def flatten_review_geometries(geom: Any, geom_types: set[str]) -> list[Any]:
        if geom is None:
            return []
        try:
            if geom.is_empty:
                return []
        except Exception:
            return []
        geom_type = str(getattr(geom, 'geom_type', ''))
        if geom_type in geom_types:
            return [geom]
        parts: list[Any] = []
        for part in getattr(geom, 'geoms', []) or []:
            parts.extend(flatten_review_geometries(part, geom_types))
        return parts

    def clip_review_geometry(geom: Any, clip_geom: Any) -> Any:
        try:
            return geom.intersection(clip_geom.buffer(1e-06))
        except Exception:
            return geom

    def draw_review_line(msp: Any, line: LineString, layer: str, color: int) -> bool:
        coords = [(float(x), float(y)) for x, y in line.coords]
        if len(coords) < 2:
            return False
        entity = msp.add_lwpolyline(coords, close=False, dxfattribs={'layer': layer, 'color': color})
        try:
            entity.dxf.const_width = 30
        except Exception:
            pass
        return True

    def draw_review_polygon(msp: Any, poly: Polygon, layer: str, color: int) -> bool:
        if poly.is_empty:
            return False
        coords = [(float(x), float(y)) for x, y in poly.exterior.coords]
        if len(coords) < 4:
            return False
        entity = msp.add_lwpolyline(coords, close=True, dxfattribs={'layer': layer, 'color': color})
        try:
            entity.dxf.const_width = 30
        except Exception:
            pass
        return True

    def draw_review_source_row(msp: Any, row: dict[str, str], clip_geom: Any, layer: str, color: int) -> int:
        drawn = 0
        for line in lines_from_row(row, ObstacleConfig(), min_length=0.0):
            clipped = clip_review_geometry(line, clip_geom)
            for part in flatten_review_geometries(clipped, {'LineString'}):
                if getattr(part, 'length', 0.0) > 0 and draw_review_line(msp, part, layer, color):
                    drawn += 1
        if drawn:
            return drawn
        for poly in polygons_from_row(row, ObstacleConfig()):
            clipped = clip_review_geometry(poly, clip_geom)
            for part in flatten_review_geometries(clipped, {'Polygon'}):
                if draw_review_polygon(msp, part, layer, color):
                    drawn += 1
        return drawn

    def write_obstacle_review_dxf(input_dxf: Path, output_dxf: Path, obstacles: list[dict[str, Any]], source_rows: list[dict[str, str]] | None=None) -> Path:
        doc = ezdxf.readfile(input_dxf)
        msp = doc.modelspace()
        add_layer(doc, WALL_LAYER, 1)
        add_layer(doc, COLUMN_LAYER, 5)
        add_layer(doc, FILL_LAYER, 3)
        add_layer(doc, TEXT_LAYER, 1)
        add_layer(doc, TOPOLOGY_LAYER, 2)
        layer_by_type = {'wall': WALL_LAYER, 'column': COLUMN_LAYER, 'filled_obstacle': FILL_LAYER}
        color_by_type = {'wall': 1, 'column': 5, 'filled_obstacle': 3}
        source_by_id = {str(row.get('object_id') or ''): row for row in source_rows or [] if row.get('object_id')}
        for index, item in enumerate(obstacles, start=1):
            layer = layer_by_type.get(item['obstacle_type'], WALL_LAYER)
            color = color_by_type.get(item['obstacle_type'], 1)
            geom = item['geometry']
            drawn = 0
            source_ids = [str(item.get('object_id') or '')]
            if str(item.get('reason') or '').startswith('llm_wall_layer_parallel_line_bundle'):
                source_ids.extend(paired_object_ids_from_evidence(item.get('geometry_evidence')))
            seen_source_ids: set[str] = set()
            for source_id in source_ids:
                if not source_id or source_id in seen_source_ids:
                    continue
                seen_source_ids.add(source_id)
                row = source_by_id.get(source_id)
                if row is not None:
                    drawn += draw_review_source_row(msp, row, geom, layer, color)
            if not drawn:
                for poly in polygon_parts(geom):
                    draw_review_polygon(msp, poly, layer, color)
            topology_geom = item.get('review_topology_geometry')
            for poly in polygon_parts(topology_geom):
                draw_review_polygon(msp, poly, TOPOLOGY_LAYER, 2)
            minx, _miny, _maxx, maxy = item['geometry'].bounds
            topology_suffix = ' topo' if 'topology' in str(item.get('reason') or '') else ''
            add_label(msp, f"{index:03d} {item['obstacle_type']}{topology_suffix}", (float(minx), float(maxy) + 220.0), 220.0, TEXT_LAYER, color)
        output_dxf.parent.mkdir(parents=True, exist_ok=True)
        try:
            doc.saveas(output_dxf)
        except PermissionError:
            output_dxf = timestamped_output_path(output_dxf)
            doc.saveas(output_dxf)
        return output_dxf

    def recognize_floor_obstacles(input_dxf: Path | str, inventory_dir: Path | str, sheets_json: Path | str, output_dir: Path | str, *, write_review_dxf: bool=True, config: ObstacleConfig=ObstacleConfig()) -> ObstacleRecognitionResult:
        input_path = Path(input_dxf).expanduser().resolve()
        inventory_path = Path(inventory_dir).resolve()
        sheets_path = Path(sheets_json).resolve()
        out_dir = Path(output_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        rows = load_geometry_rows(inventory_path)
        regions = load_floor_regions(sheets_path)
        if not regions:
            raise RuntimeError('没有可用于障碍物识别的楼层可巡检区域。')
        layer_decisions = classify_layers_by_llm(rows, out_dir)
        area_obstacles = detect_llm_hit_obstacles(rows, regions, layer_decisions, config)
        wall_processing_stats: dict[str, Any] = {}
        parallel_wall_obstacles = detect_parallel_wall_line_obstacles(rows, regions, layer_decisions, config, diagnostics=wall_processing_stats)
        obstacles = area_obstacles + parallel_wall_obstacles
        obstacle_csv = out_dir / OBSTACLE_CSV_FILE
        write_obstacle_csv(obstacle_csv, obstacles)
        per_region_geojsons, union_geojsons = write_geojson_outputs(out_dir, obstacles)
        marked_dxf: Path | None = None
        if write_review_dxf:
            marked_dxf = write_review_dxf_file = out_dir.parent / 'review' / f'{input_path.stem}_obstacles_marked.dxf'
            marked_dxf = write_obstacle_review_dxf(input_path, write_review_dxf_file, obstacles, rows)
        type_counts = Counter((item['obstacle_type'] for item in obstacles))
        region_counts = Counter((item['full_region_id'] for item in obstacles))
        topology_completed_pair_count = sum((1 for item in parallel_wall_obstacles if str(item.get('reason') or '').startswith('llm_wall_layer_parallel_line_bundle_topology_completed')))
        topology_polygon_completion_count = sum((1 for item in parallel_wall_obstacles if str(item.get('reason') or '').startswith('llm_wall_layer_topology_polygon_completion')))
        topology_added_area = sum((float(getattr(item.get('review_topology_geometry'), 'area', 0.0) or 0.0) for item in parallel_wall_obstacles))
        result_json = out_dir / RESULT_JSON_FILE
        write_json(result_json, {'input_dxf': str(input_path), 'inventory_dir': str(inventory_path), 'sheets_json': str(sheets_path), 'output_dir': str(out_dir), 'strategy': 'LLM classifies trusted obstacle layers; closed/area geometry is clipped to inspection regions; open wall lines use projection-overlap cores plus topology-supported source-span, L/T junction, and end-cap completion.', 'layer_llm_decisions': str((out_dir / LAYER_LLM_FILE).resolve()), 'obstacle_csv': str(obstacle_csv.resolve()), 'marked_dxf': str(marked_dxf.resolve()) if marked_dxf else '', 'obstacle_count': len(obstacles), 'obstacle_type_count': len(type_counts), 'region_count': len(regions), 'stage_counts': {'area_obstacles': len(area_obstacles), 'parallel_wall_line_bundle_obstacles': len(parallel_wall_obstacles), 'topology_completed_pair_obstacles': topology_completed_pair_count, 'topology_polygon_completion_obstacles': topology_polygon_completion_count, 'final_obstacles': len(obstacles)}, 'wall_processing_stats': wall_processing_stats, 'topology_added_area': topology_added_area, 'type_counts': dict(type_counts.most_common()), 'region_counts': dict(region_counts.most_common()), 'per_region_geojsons': [str(path.resolve()) for path in per_region_geojsons], 'union_geojsons': [str(path.resolve()) for path in union_geojsons]})
        return ObstacleRecognitionResult(result_json=result_json, obstacle_csv=obstacle_csv, marked_dxf=marked_dxf, obstacle_count=len(obstacles), obstacle_type_count=len(type_counts), region_count=len(regions), per_region_geojsons=per_region_geojsons, union_geojsons=union_geojsons)
    return dict(locals())

_s05_obstacles = _register_embedded_module(
    'fire_inspection_system.obstacle_recognition_door_mask_fixed',
    _build_s05_obstacles(),
    aliases=('obstacle_recognition_door_mask_fixed',),
)

# === CONSOLIDATED PUBLIC API ===
from pathlib import Path
from typing import Any


def run_stage(
    input_dxf: Path,
    inventory_dir: Path,
    sheets_json: Path,
    run_dir: Path,
    *,
    write_review_dxf: bool,
) -> Any:
    return _s05_obstacles.recognize_floor_obstacles(
        input_dxf,
        inventory_dir,
        sheets_json,
        run_dir / "obstacles",
        write_review_dxf=write_review_dxf,
    )


__all__ = ["run_stage"]
