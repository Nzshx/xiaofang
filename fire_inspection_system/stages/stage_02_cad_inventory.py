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
# Migrated implementation: agents/cad_vector_inventory_agent.py
# -----------------------------------------------------------------------------
def _build_s02_inventory():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'agents/cad_vector_inventory_agent.py'
    )
    __name__ = 'agents.cad_vector_inventory_agent'
    __package__ = 'agents'
    """
    CAD Vector Inventory Agent
    功能定位：
    1. 读取 DXF 图纸文件；
    2. 扫描 modelspace 或全部 layout 中的 CAD 实体；
    3. 对 INSERT/BLOCK 进行可控递归展开，提取块属性、块内语义实体；
    4. 生成全量图元清单、语义图元清单、对象聚合目录、图层/块/文本汇总；
    5. 生成供 LLM 审查对象类别的压缩 payload。
    """
    import argparse
    import csv
    import hashlib
    import json
    import logging
    import math
    import re
    import sys
    from collections import Counter, defaultdict
    from pathlib import Path
    from typing import Any, Dict, Iterable, List, Optional, Tuple
    import ezdxf
    from ezdxf import bbox
    from ezdxf.entities.copy import CopyStrategy
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')
    logging.getLogger('ezdxf').setLevel(logging.ERROR)
    DEFAULT_INPUT_DXF = Path('D:\\untitled5\\data\\raw\\test.dxf')
    DEFAULT_OUTPUT_DIR = Path('D:\\untitled5\\outputs\\test_single_floor\\inventory')
    DEFAULT_MAX_INSERT_DEPTH = 10
    DEFAULT_TOP_N_LLM = 350
    SEMANTIC_ENTITY_TYPES = {'TEXT', 'MTEXT', 'ATTRIB', 'INSERT'}
    GEOMETRY_ENTITY_TYPES = {'LINE', 'LWPOLYLINE', 'POLYLINE', 'HATCH', 'CIRCLE', 'ARC', 'ELLIPSE', 'SOLID', 'REGION'}
    STRUCTURAL_GEOMETRY_ENTITY_TYPES = {'LINE', 'LWPOLYLINE', 'POLYLINE', 'HATCH', 'CIRCLE', 'ARC', 'ELLIPSE'}
    STRUCTURAL_LAYER_MARKERS = ('墙', 'WALL', 'SHEARWALL', '柱', 'COLUMN', 'COLU', 'PILLAR', '窗', 'WINDOW', 'WIN', '结构', '建筑')
    BUSINESS_ROLE_OPTIONS = [{'role': 'obstacle_object', 'description': 'Non-passable hard obstacle, such as wall, column, closed shaft, or building boundary. It should enter obstacle_union after geometry validation.'}, {'role': 'passable_opening_object', 'description': 'Passable abstract door/opening object, such as door, fire door, passage opening, rolling shutter, or wall opening. It should not be treated as an obstacle.'}, {'role': 'inspection_object', 'description': 'Non-passable fire-inspection context object, such as strong/weak power room, pump room, fan room, shaft, hydrant box, distribution room, or equipment room. It provides inspection value.'}]
    INVENTORY_FIELDS = ['object_id', 'layout', 'source', 'depth', 'insert_depth', 'entity_type', 'handle', 'layer', 'color', 'true_color', 'linetype', 'lineweight', 'parent_block_name', 'block_path', 'insert_path', 'raw_text', 'norm_text', 'geometry_kind', 'is_closed', 'x', 'y', 'bbox_minx', 'bbox_miny', 'bbox_maxx', 'bbox_maxy', 'bbox_area']
    GEOMETRY_INVENTORY_FIELDS = INVENTORY_FIELDS + ['geometry_json', 'geometry_point_count', 'geometry_line_count', 'geometry_polygon_count', 'geometry_length', 'geometry_area']
    CATALOG_FIELDS = ['signature_id', 'count', 'layer', 'entity_type', 'geometry_kind', 'color', 'linetype', 'is_closed', 'parent_block_name', 'block_path_sample', 'norm_text_sample', 'raw_text_sample', 'source_counter', 'depth_min', 'depth_max', 'bbox_minx', 'bbox_miny', 'bbox_maxx', 'bbox_maxy', 'bbox_area', 'sample_object_ids', 'sample_handles', 'llm_review_priority']
    BLOCK_SIGNATURE_FIELDS = ['block_name', 'entity_count', 'entity_type_counts', 'layer_counts', 'text_samples', 'attdef_tags', 'attdef_prompts', 'child_blocks', 'has_direct_semantics', 'has_recursive_semantics', 'has_direct_geometry_semantics', 'has_recursive_geometry_semantics', 'signature_hash']

    def file_sha256(path: Path) -> str:
        """计算输入文件的 SHA256 哈希值。- 唯一标识当前 DXF 文件；
        """
        h = hashlib.sha256()
        with path.open('rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                h.update(chunk)
        return h.hexdigest()

    def safe_str(value: Any) -> str:
        """将任意值安全转换为字符串，主要用于 CSV 写出。
        """
        if value is None:
            return ''
        if isinstance(value, (list, tuple, dict)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    def safe_float(value: Any, default: Optional[float]=None) -> Optional[float]:
        """将任意值安全转换为有限浮点数。
        """
        try:
            f = float(value)
            if math.isfinite(f):
                return f
        except Exception:
            pass
        return default

    def norm_text(value: Any) -> str:
        """规范化 CAD 文本，生成便于检索和聚合的文本键。
        处理内容包括：
        - 去除 DXF 文本格式码，例如 \\P、\\A1;；
        - 删除换行、制表符和多余空白；
        - 删除花括号；
        - 保留中文、数字和普通符号。
        """
        s = str(value or '')
        s = s.replace('\\P', '').replace('\\p', '')
        s = re.sub('\\\\[A-Za-z][^;]*;', '', s)
        s = s.replace('\r', '').replace('\n', '').replace('\t', '')
        s = re.sub('\\s+', '', s)
        s = s.replace('{', '').replace('}', '')
        return s.strip()

    def dxf_attr(entity: Any, name: str, default: Any='') -> Any:
        """安全读取实体的 DXF 属性。
        不同 DXF 实体拥有的 dxf 属性不完全一致。
        本函数用于避免直接 getattr(entity.dxf, name) 时因缺属性导致程序中断。
        """
        try:
            return getattr(entity.dxf, name)
        except Exception:
            return default

    def entity_type(entity: Any) -> str:
        """返回实体类型字符串，例如 LINE、TEXT、MTEXT、INSERT、HATCH。
        当实体异常或缺少 dxftype() 方法时，返回 UNKNOWN。
        """
        try:
            return str(entity.dxftype())
        except Exception:
            return 'UNKNOWN'

    def entity_handle(entity: Any) -> str:
        """返回 CAD 实体 handle。
        handle 是 DXF 内部实体标识，可用于追踪原始对象和人工复核。
        """
        return str(dxf_attr(entity, 'handle', '') or '')

    def entity_layer(entity: Any) -> str:
        """返回 CAD 实体所在图层名称。
        图层名是后续识别墙体、门洞、消防设施、图例和标注的重要语义来源。
        """
        return str(dxf_attr(entity, 'layer', '') or '')

    def entity_text(entity: Any) -> str:
        """提取文本类实体中的原始文字。
        """
        etype = entity_type(entity)
        try:
            if etype in {'TEXT', 'ATTRIB', 'ATTDEF'}:
                return str(entity.dxf.text)
            if etype == 'MTEXT':
                try:
                    return str(entity.plain_text())
                except Exception:
                    return str(entity.text)
        except Exception:
            return ''
        return ''

    def entity_is_closed(entity: Any) -> int:
        """判断实体是否可视为闭合几何。
        主要用于区分闭合多段线、填充区域、圆、椭圆、面域等。
        闭合几何在障碍物、房间区域、边界识别中更有价值。
        """
        etype = entity_type(entity)
        try:
            if etype == 'LWPOLYLINE':
                return 1 if bool(entity.closed) else 0
            if etype == 'POLYLINE':
                return 1 if bool(entity.is_closed) else 0
            if etype in {'HATCH', 'CIRCLE', 'ELLIPSE', 'REGION'}:
                return 1
        except Exception:
            pass
        return 0

    def geometry_kind(entity: Any) -> str:
        """将 DXF 原始实体类型映射为更粗粒度的几何类别。
        """
        etype = entity_type(entity)
        if etype in {'TEXT', 'MTEXT', 'ATTRIB'}:
            return 'text'
        if etype == 'INSERT':
            return 'block_insert'
        if etype in {'LINE', 'XLINE', 'RAY'}:
            return 'line'
        if etype in {'LWPOLYLINE', 'POLYLINE'}:
            return 'polyline_closed' if entity_is_closed(entity) else 'polyline_open'
        if etype in {'ARC', 'CIRCLE', 'ELLIPSE'}:
            return 'curve'
        if etype in {'SPLINE'}:
            return 'spline_curve'
        if etype in {'HATCH'}:
            return 'hatch_area'
        if etype in {'SOLID', 'REGION', 'WIPEOUT'}:
            return 'area_entity'
        if etype in {'DIMENSION', 'LEADER', 'MLEADER'}:
            return 'annotation_dimension'
        if etype in {'POINT'}:
            return 'point'
        return 'other'

    def compact_marker_text(value: Any) -> str:
        """面向图层/块名的轻量归一化，用于判断结构几何语义。"""
        return norm_text(str(value or '')).upper()

    def contains_structural_marker(value: Any) -> bool:
        text = str(value or '')
        if not text:
            return False
        upper = text.upper()
        compact = compact_marker_text(text)
        for marker in STRUCTURAL_LAYER_MARKERS:
            raw = str(marker)
            if raw in text or raw.upper() in upper or compact_marker_text(raw) in compact:
                return True
        return False

    def is_structural_geometry_candidate(entity: Any, block_path: List[str] | None=None) -> bool:
        """判断块内虚拟图元是否值得保留到结构几何清单。

        默认流程不再展开块内所有线段，但墙/柱/窗经常被做成块。这里用实体类型 +
        图层名/块路径语义保留结构相关几何，避免全量展开带来的数量爆炸。
        """
        etype = entity_type(entity)
        if etype not in STRUCTURAL_GEOMETRY_ENTITY_TYPES:
            return False
        if contains_structural_marker(entity_layer(entity)):
            return True
        return any((contains_structural_marker(part) for part in block_path or []))

    def entity_bbox(entity: Any) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        """计算单个实体的二维包围盒。
        """
        try:
            bb = bbox.extents([entity], fast=True)
            if getattr(bb, 'has_data', False):
                return (float(bb.extmin.x), float(bb.extmin.y), float(bb.extmax.x), float(bb.extmax.y))
        except Exception:
            pass
        return (None, None, None, None)

    def as_xy(point: Any) -> Tuple[float, float] | None:
        try:
            return (float(point.x), float(point.y))
        except Exception:
            pass
        try:
            return (float(point[0]), float(point[1]))
        except Exception:
            return None

    def close_ring(points: List[Tuple[float, float]]) -> List[List[float]]:
        coords = [[float(x), float(y)] for x, y in points]
        if coords and coords[0] != coords[-1]:
            coords.append(coords[0])
        return coords

    def line_length_coords(coords: List[List[float]]) -> float:
        total = 0.0
        for index in range(1, len(coords)):
            x1, y1 = coords[index - 1]
            x2, y2 = coords[index]
            total += math.hypot(x2 - x1, y2 - y1)
        return total

    def polygon_area_coords(coords: List[List[float]]) -> float:
        if len(coords) < 4:
            return 0.0
        total = 0.0
        for index in range(1, len(coords)):
            x1, y1 = coords[index - 1]
            x2, y2 = coords[index]
            total += x1 * y2 - x2 * y1
        return abs(total) / 2.0

    def circle_points(center: Tuple[float, float], radius: float, segments: int=48) -> List[List[float]]:
        cx, cy = center
        points = [[cx + radius * math.cos(2 * math.pi * index / segments), cy + radius * math.sin(2 * math.pi * index / segments)] for index in range(segments)]
        if points:
            points.append(points[0])
        return points

    def arc_points(center: Tuple[float, float], radius: float, start_angle: float, end_angle: float, segments: int=24) -> List[List[float]]:
        start = math.radians(float(start_angle))
        end = math.radians(float(end_angle))
        if end < start:
            end += 2 * math.pi
        count = max(segments, 2)
        return [[center[0] + radius * math.cos(start + (end - start) * index / (count - 1)), center[1] + radius * math.sin(start + (end - start) * index / (count - 1))] for index in range(count)]

    def geometry_payload(entity: Any) -> Dict[str, Any] | None:
        """把可用于障碍物识别的 DXF 实体保存为轻量真实几何 JSON。

        输出使用 lines/polygons 两类几何，避免后续障碍物阶段只能依赖 bbox。
        HATCH 的复杂 EdgePath 不强行闭合成 polygon，防止错误面吞噬可通行区域。
        """
        etype = entity_type(entity)
        lines: List[List[List[float]]] = []
        polygons: List[List[List[float]]] = []
        try:
            if etype == 'LINE':
                start = as_xy(entity.dxf.start)
                end = as_xy(entity.dxf.end)
                if start and end:
                    lines.append([[start[0], start[1]], [end[0], end[1]]])
            elif etype == 'LWPOLYLINE':
                points = [(float(p[0]), float(p[1])) for p in entity.get_points()]
                if len(points) >= 2:
                    coords = [[x, y] for x, y in points]
                    if bool(getattr(entity, 'closed', False)):
                        ring = close_ring(points)
                        lines.append(ring)
                        if len(ring) >= 4:
                            polygons.append(ring)
                    else:
                        lines.append(coords)
            elif etype == 'POLYLINE':
                points = []
                for vertex in entity.vertices:
                    loc = as_xy(vertex.dxf.location)
                    if loc:
                        points.append(loc)
                if len(points) >= 2:
                    coords = [[x, y] for x, y in points]
                    if bool(getattr(entity, 'is_closed', False)):
                        ring = close_ring(points)
                        lines.append(ring)
                        if len(ring) >= 4:
                            polygons.append(ring)
                    else:
                        lines.append(coords)
            elif etype == 'CIRCLE':
                center = as_xy(entity.dxf.center)
                radius = safe_float(entity.dxf.radius, 0.0) or 0.0
                if center and radius > 0:
                    polygons.append(circle_points(center, radius))
            elif etype == 'ARC':
                center = as_xy(entity.dxf.center)
                radius = safe_float(entity.dxf.radius, 0.0) or 0.0
                if center and radius > 0:
                    lines.append(arc_points(center, radius, entity.dxf.start_angle, entity.dxf.end_angle))
            elif etype == 'HATCH':
                for path in entity.paths:
                    if hasattr(path, 'vertices'):
                        points = []
                        for vertex in path.vertices:
                            loc = as_xy(vertex)
                            if loc:
                                points.append(loc)
                        if len(points) >= 2:
                            closed = bool(getattr(path, 'is_closed', True))
                            if closed:
                                ring = close_ring(points)
                                lines.append(ring)
                                if len(ring) >= 4:
                                    polygons.append(ring)
                            else:
                                lines.append([[x, y] for x, y in points])
                    elif hasattr(path, 'edges'):
                        for edge in path.edges:
                            edge_type = str(getattr(edge, 'EDGE_TYPE', ''))
                            if edge_type == 'LineEdge':
                                start = as_xy(edge.start)
                                end = as_xy(edge.end)
                                if start and end:
                                    lines.append([[start[0], start[1]], [end[0], end[1]]])
                            elif edge_type == 'ArcEdge':
                                center = as_xy(edge.center)
                                radius = safe_float(edge.radius, 0.0) or 0.0
                                if center and radius > 0:
                                    lines.append(arc_points(center, radius, edge.start_angle, edge.end_angle))
        except Exception:
            return None
        if not lines and (not polygons):
            return None
        point_count = sum((len(line) for line in lines)) + sum((len(poly) for poly in polygons))
        length = sum((line_length_coords(line) for line in lines))
        area = sum((polygon_area_coords(poly) for poly in polygons))
        return {'version': 1, 'entity_type': etype, 'lines': lines, 'polygons': polygons, 'point_count': point_count, 'line_count': len(lines), 'polygon_count': len(polygons), 'length': length, 'area': area}

    def fallback_xy(entity: Any) -> Tuple[Optional[float], Optional[float]]:
        """当 bbox 不可用时，尝试从实体常见锚点属性中提取坐标。
        """
        for attr in ('insert', 'center', 'start', 'location'):
            p = dxf_attr(entity, attr, None)
            if p is not None:
                try:
                    return (float(p.x), float(p.y))
                except Exception:
                    try:
                        return (float(p[0]), float(p[1]))
                    except Exception:
                        pass
        return (None, None)

    def bbox_center_or_fallback(entity: Any, bounds: Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]) -> Tuple[Optional[float], Optional[float]]:
        """返回实体中心点坐标。
        优先使用 bbox 中心；
        如果 bbox 不存在，则调用 fallback_xy() 获取实体锚点坐标。
        """
        minx, miny, maxx, maxy = bounds
        if None not in bounds:
            return ((minx + maxx) / 2.0, (miny + maxy) / 2.0)
        return fallback_xy(entity)

    def get_insert_block_name(entity: Any) -> str:
        """获取 INSERT 实体引用的 block 名称。
        如果当前实体不是 INSERT，或读取失败，则返回空字符串。
        """
        if entity_type(entity) != 'INSERT':
            return ''
        try:
            return str(entity.dxf.name)
        except Exception:
            return ''

    def get_insert_attribs(entity: Any) -> List[Any]:
        """获取 INSERT 实体携带的 ATTRIB 属性列表。
        很多 CAD 块会通过属性记录设备编号、房间名称、图例说明等语义信息。
        """
        try:
            return list(entity.attribs)
        except Exception:
            return []

    def get_virtual_entities(entity: Any) -> List[Any]:
        """展开 INSERT 中的虚拟实体。ezdxf 的 virtual_entities() 可把块参照内部的实体展开为当前坐标系下的临时实体。
        这里不修改原始 DXF，只用于读取和清单生成。
        """
        try:
            CopyStrategy.clear_log_message()
            entities = list(entity.virtual_entities(skipped_entity_callback=lambda *_: None))
            CopyStrategy.clear_log_message()
            return entities
        except Exception:
            return []

    def build_block_signatures(doc: Any) -> Dict[str, Dict[str, Any]]:
        """构建所有 block 定义的摘要签名。
        在选择性展开 INSERT 时，先判断该 block 是否可能包含文本或属性语义，
        避免无差别展开所有块导致对象数量暴涨。
        """
        signatures: Dict[str, Dict[str, Any]] = {}
        for block in doc.blocks:
            block_name = str(getattr(block, 'name', '') or '')
            entities = list(block)
            type_counts = Counter((entity_type(entity) for entity in entities))
            layer_counts = Counter((entity_layer(entity) for entity in entities))
            texts: List[str] = []
            attdef_tags: List[str] = []
            attdef_prompts: List[str] = []
            child_blocks: List[str] = []
            has_direct_geometry_semantics = False
            for entity in entities:
                etype = entity_type(entity)
                text = entity_text(entity).strip()
                if text and text not in texts and (len(texts) < 24):
                    texts.append(text)
                if etype == 'ATTDEF':
                    tag = safe_str(dxf_attr(entity, 'tag', ''))
                    prompt = safe_str(dxf_attr(entity, 'prompt', ''))
                    if tag and tag not in attdef_tags:
                        attdef_tags.append(tag)
                    if prompt and prompt not in attdef_prompts:
                        attdef_prompts.append(prompt)
                elif etype == 'INSERT':
                    child = get_insert_block_name(entity)
                    if child and child not in child_blocks:
                        child_blocks.append(child)
                if is_structural_geometry_candidate(entity, [block_name]):
                    has_direct_geometry_semantics = True
            has_direct_semantics = bool(type_counts.get('TEXT') or type_counts.get('MTEXT') or type_counts.get('ATTDEF'))
            signatures[block_name] = {'block_name': block_name, 'entity_count': len(entities), 'entity_type_counts': dict(type_counts), 'layer_counts': dict(layer_counts), 'text_samples': texts, 'attdef_tags': attdef_tags, 'attdef_prompts': attdef_prompts, 'child_blocks': child_blocks, 'has_direct_semantics': has_direct_semantics, 'has_recursive_semantics': has_direct_semantics, 'has_direct_geometry_semantics': has_direct_geometry_semantics, 'has_recursive_geometry_semantics': has_direct_geometry_semantics}

        def contains_semantics(block_name: str, visiting: set[str]) -> bool:
            """递归判断指定 block 是否直接或间接包含语义实体。
            visiting 用于避免 block 循环引用导致无限递归。
            """
            signature = signatures.get(block_name)
            if not signature:
                return False
            if signature['has_direct_semantics']:
                return True
            if block_name in visiting:
                return False
            next_visiting = {*visiting, block_name}
            return any((contains_semantics(child, next_visiting) for child in signature['child_blocks']))

        def contains_geometry_semantics(block_name: str, visiting: set[str], all_signatures: Dict[str, Dict[str, Any]]) -> bool:
            """递归判断 block 是否直接或间接包含墙/柱/窗相关结构几何。"""
            signature = all_signatures.get(block_name)
            if not signature:
                return False
            if signature.get('has_direct_geometry_semantics'):
                return True
            if block_name in visiting:
                return False
            next_visiting = {*visiting, block_name}
            return any((contains_geometry_semantics(child, next_visiting, all_signatures) for child in signature['child_blocks']))
        for block_name, signature in signatures.items():
            signature['has_recursive_semantics'] = contains_semantics(block_name, set())
            signature['has_recursive_geometry_semantics'] = contains_geometry_semantics(block_name, set(), signatures)
            stable_payload = json.dumps(signature, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            signature['signature_hash'] = hashlib.sha256(stable_payload.encode('utf-8')).hexdigest()
        return signatures

    def block_signature_rows(signatures: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """将 block signature 字典转换为 CSV 可写行。
        Counter、list、dict 等复杂字段会序列化为 JSON 字符串，便于在表格中查看。
        """
        rows: List[Dict[str, Any]] = []
        for block_name in sorted(signatures):
            signature = signatures[block_name]
            rows.append({**signature, 'entity_type_counts': json.dumps(signature['entity_type_counts'], ensure_ascii=False, sort_keys=True), 'layer_counts': json.dumps(signature['layer_counts'], ensure_ascii=False, sort_keys=True), 'text_samples': json.dumps(signature['text_samples'], ensure_ascii=False), 'attdef_tags': json.dumps(signature['attdef_tags'], ensure_ascii=False), 'attdef_prompts': json.dumps(signature['attdef_prompts'], ensure_ascii=False), 'child_blocks': json.dumps(signature['child_blocks'], ensure_ascii=False)})
        return rows

    def walk_entity(entity: Any, layout_name: str, block_path: List[str], insert_path: List[str], depth: int, max_depth: int, *, block_signatures: Dict[str, Dict[str, Any]] | None=None, expand_all_inserts: bool=True) -> Iterable[Dict[str, Any]]:
        """递归遍历 CAD 实体，并按统一结构产出待入库对象。
        处理逻辑：
        1. 普通实体直接产出；
        2. INSERT 先产出块容器本身；
        3. 再产出 INSERT 携带的 ATTRIB；
        4. 在深度限制内，根据 expand_all_inserts 和 block_signatures 决定是否展开块内虚拟实体。
        """
        etype = entity_type(entity)
        if etype != 'INSERT':
            if depth > 0 and (not expand_all_inserts) and (etype not in SEMANTIC_ENTITY_TYPES) and (not is_structural_geometry_candidate(entity, block_path)):
                return
            yield {'entity': entity, 'layout': layout_name, 'source': 'direct_entity' if depth == 0 else 'virtual_entity_in_insert', 'block_path': block_path, 'insert_path': insert_path, 'depth': depth, 'parent_block_name': block_path[-1] if block_path else ''}
            return
        block_name = get_insert_block_name(entity)
        handle = entity_handle(entity)
        new_block_path = block_path + ([block_name] if block_name else [])
        new_insert_path = insert_path + ([handle] if handle else [])
        yield {'entity': entity, 'layout': layout_name, 'source': 'insert_container', 'block_path': new_block_path, 'insert_path': new_insert_path, 'depth': depth, 'parent_block_name': block_name}
        for attrib in get_insert_attribs(entity):
            yield {'entity': attrib, 'layout': layout_name, 'source': 'insert_attrib', 'block_path': new_block_path, 'insert_path': new_insert_path, 'depth': depth + 1, 'parent_block_name': block_name}
        if depth >= max_depth:
            return
        if not expand_all_inserts:
            signature = (block_signatures or {}).get(block_name, {})
            if not (signature.get('has_recursive_semantics', False) or signature.get('has_recursive_geometry_semantics', False)):
                return
        for virtual_entity in get_virtual_entities(entity):
            yield from walk_entity(virtual_entity, layout_name, new_block_path, new_insert_path, depth + 1, max_depth, block_signatures=block_signatures, expand_all_inserts=expand_all_inserts)

    def make_inventory_row(item: Dict[str, Any], index: int) -> Dict[str, Any]:
        """将 walk_entity() 产出的实体包装为标准 inventory 行。
        统一提取：
        - object_id；
        - layout/source/depth；
        - entity_type/handle/layer/color/linetype；
        - block_path/insert_path；
        - raw_text/norm_text；
        - geometry_kind/is_closed；
        - 坐标与 bbox。
        """
        entity = item['entity']
        etype = entity_type(entity)
        raw_text = entity_text(entity)
        bounds = entity_bbox(entity)
        x, y = bbox_center_or_fallback(entity, bounds)
        minx, miny, maxx, maxy = bounds
        bbox_area = ''
        if None not in bounds:
            bbox_area = max((maxx - minx) * (maxy - miny), 0.0)
        true_color = dxf_attr(entity, 'true_color', '')
        return {'object_id': f'CADOBJ_{index:07d}', 'layout': item.get('layout', ''), 'source': item.get('source', ''), 'depth': item.get('depth', 0), 'insert_depth': item.get('depth', 0), 'entity_type': etype, 'handle': entity_handle(entity), 'layer': entity_layer(entity), 'color': dxf_attr(entity, 'color', ''), 'true_color': true_color if true_color is not None else '', 'linetype': dxf_attr(entity, 'linetype', ''), 'lineweight': dxf_attr(entity, 'lineweight', ''), 'parent_block_name': item.get('parent_block_name', ''), 'block_path': item.get('block_path', []), 'insert_path': item.get('insert_path', []), 'raw_text': raw_text, 'norm_text': norm_text(raw_text), 'geometry_kind': geometry_kind(entity), 'is_closed': entity_is_closed(entity), 'x': x if x is not None else '', 'y': y if y is not None else '', 'bbox_minx': minx if minx is not None else '', 'bbox_miny': miny if miny is not None else '', 'bbox_maxx': maxx if maxx is not None else '', 'bbox_maxy': maxy if maxy is not None else '', 'bbox_area': bbox_area}

    def make_geometry_inventory_row(base_row: Dict[str, Any], entity: Any) -> Dict[str, Any] | None:
        """基于同一个实体生成结构几何清单行。

        cad_object_inventory.csv 只保留 bbox；障碍物识别需要真实线/面几何，
        因此这里额外保存 geometry_json，避免后续阶段继续用 bbox 猜墙体。
        """
        etype = str(base_row.get('entity_type', '') or '').upper()
        if etype not in GEOMETRY_ENTITY_TYPES:
            return None
        payload = geometry_payload(entity)
        if not payload:
            return None
        return {**base_row, 'geometry_json': payload, 'geometry_point_count': payload.get('point_count', 0), 'geometry_line_count': payload.get('line_count', 0), 'geometry_polygon_count': payload.get('polygon_count', 0), 'geometry_length': payload.get('length', 0.0), 'geometry_area': payload.get('area', 0.0)}

    def write_csv(path: Path, rows: Iterable[Dict[str, Any]], fields: List[str]) -> None:
        """写出 CSV 文件。
        使用 utf-8-sig 编码，方便 Windows Excel 直接打开并正确显示中文。
        extrasaction='ignore' 用于忽略行字典中多余字段。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            for row in rows:
                writer.writerow({k: safe_str(row.get(k, '')) for k in fields})

    def write_json(path: Path, data: Any) -> None:
        """写出 JSON 文件。
        使用 UTF-8 编码并保留中文字符，便于人工查看和后续程序读取。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def top_counter(values: Iterable[Any], n: int=20) -> str:
        """统计字段值出现频次，并返回 Top N 的 JSON 字符串。
        用于 layer/block/text/catalog 等汇总表中的频次字段。
        """
        counter = Counter((str(v) for v in values if str(v) != ''))
        return json.dumps(dict(counter.most_common(n)), ensure_ascii=False)

    def numeric_extent(rows: List[Dict[str, Any]]) -> Tuple[str, str, str, str, str]:
        """计算一组 inventory 行的整体空间范围。
        返回 minx、miny、maxx、maxy 和整体 bbox 面积。
        如果组内没有有效 bbox，则返回空字符串。
        """
        xs1: List[float] = []
        ys1: List[float] = []
        xs2: List[float] = []
        ys2: List[float] = []
        for row in rows:
            minx = safe_float(row.get('bbox_minx'))
            miny = safe_float(row.get('bbox_miny'))
            maxx = safe_float(row.get('bbox_maxx'))
            maxy = safe_float(row.get('bbox_maxy'))
            if None not in (minx, miny, maxx, maxy):
                xs1.append(minx)
                ys1.append(miny)
                xs2.append(maxx)
                ys2.append(maxy)
        if not xs1:
            return ('', '', '', '', '')
        area = max(max(xs2) - min(xs1), 0.0) * max(max(ys2) - min(ys1), 0.0)
        return (min(xs1), min(ys1), max(xs2), max(ys2), area)

    def sample_values(rows: List[Dict[str, Any]], field: str, n: int=8) -> str:
        """从一组行中提取某个字段的去重样例。
        返回 JSON 字符串，用于在 catalog 和 summary 中展示代表性样本。
        """
        values: List[str] = []
        seen = set()
        for row in rows:
            value = safe_str(row.get(field, ''))
            if value and value not in seen:
                values.append(value)
                seen.add(value)
            if len(values) >= n:
                break
        return json.dumps(values, ensure_ascii=False)

    def make_catalog_key(row: Dict[str, Any]) -> Tuple[str, str, str, str, str, str, str, str]:
        """构建 catalog 聚合键。
        聚合维度包括：图层、实体类型、几何类别、颜色、线型、闭合状态、父 block 名称、文本键。
        文本类实体会把 norm_text 纳入聚合键，避免不同房间名/设施名被错误合并。
        """
        text_key = row.get('norm_text', '') if row.get('geometry_kind') == 'text' else ''
        return (str(row.get('layer', '')), str(row.get('entity_type', '')), str(row.get('geometry_kind', '')), str(row.get('color', '')), str(row.get('linetype', '')), str(row.get('is_closed', '')), str(row.get('parent_block_name', '')), str(text_key))

    def review_priority(rows: List[Dict[str, Any]]) -> int:
        """计算 catalog 分组的 LLM/人工审查优先级。
        该分数不是语义分类置信度，只用于排序。
        文本、非零图层、块来源、闭合几何等因素会提高审查优先级。
        """
        score = 0
        count = len(rows)
        if count >= 100:
            score += 3
        elif count >= 20:
            score += 2
        elif count >= 5:
            score += 1
        if any((r.get('geometry_kind') == 'text' and r.get('norm_text') for r in rows)):
            score += 3
        if any((str(r.get('layer', '')) not in {'', '0'} for r in rows)):
            score += 2
        if any((r.get('parent_block_name') for r in rows)):
            score += 1
        if any((r.get('geometry_kind') in {'hatch_area', 'polyline_closed', 'block_insert'} for r in rows)):
            score += 1
        return score

    def build_catalog(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """将全量 inventory 聚合为对象目录 catalog。
        catalog 用于把十几万级图元压缩为较少的对象签名，
        降低 LLM 审查和人工复核成本。
        """
        buckets: Dict[Tuple[str, str, str, str, str, str, str, str], List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            buckets[make_catalog_key(row)].append(row)
        catalog: List[Dict[str, Any]] = []
        for idx, (key, group) in enumerate(sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True), start=1):
            layer, etype, gkind, color, linetype, is_closed, parent_block_name, text_key = key
            minx, miny, maxx, maxy, area = numeric_extent(group)
            catalog.append({'signature_id': f'CAT_{idx:06d}', 'count': len(group), 'layer': layer, 'entity_type': etype, 'geometry_kind': gkind, 'color': color, 'linetype': linetype, 'is_closed': is_closed, 'parent_block_name': parent_block_name, 'block_path_sample': sample_values(group, 'block_path', 3), 'norm_text_sample': text_key, 'raw_text_sample': sample_values(group, 'raw_text', 5), 'source_counter': top_counter((r.get('source', '') for r in group), 10), 'depth_min': min((int(r.get('depth', 0) or 0) for r in group)), 'depth_max': max((int(r.get('depth', 0) or 0) for r in group)), 'bbox_minx': minx, 'bbox_miny': miny, 'bbox_maxx': maxx, 'bbox_maxy': maxy, 'bbox_area': area, 'sample_object_ids': sample_values(group, 'object_id', 8), 'sample_handles': sample_values(group, 'handle', 8), 'llm_review_priority': review_priority(group)})
        return catalog

    def build_group_summary(rows: List[Dict[str, Any]], group_field: str) -> List[Dict[str, Any]]:
        """按指定字段对 inventory 行进行分组汇总。
        典型用途：
        - 按 layer 生成图层摘要；
        - 按 parent_block_name 生成块摘要。
        """
        buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            buckets[str(row.get(group_field, ''))].append(row)
        result = []
        for key, group in sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True):
            minx, miny, maxx, maxy, area = numeric_extent(group)
            result.append({group_field: key, 'count': len(group), 'entity_type_counter': top_counter((r.get('entity_type', '') for r in group), 20), 'geometry_kind_counter': top_counter((r.get('geometry_kind', '') for r in group), 20), 'layer_counter': top_counter((r.get('layer', '') for r in group), 20), 'block_counter': top_counter((r.get('parent_block_name', '') for r in group), 20), 'color_counter': top_counter((r.get('color', '') for r in group), 20), 'text_samples': sample_values(group, 'norm_text', 10), 'bbox_minx': minx, 'bbox_miny': miny, 'bbox_maxx': maxx, 'bbox_maxy': maxy, 'bbox_area': area})
        return result

    def build_text_summary(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按规范化文本 norm_text 聚合文本实体。
        用于快速查看图纸中出现过哪些房间名、设施名、图例文字或标注文字。
        """
        text_rows = [r for r in rows if r.get('norm_text')]
        buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in text_rows:
            buckets[str(row.get('norm_text', ''))].append(row)
        result = []
        for text, group in sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True):
            minx, miny, maxx, maxy, area = numeric_extent(group)
            result.append({'norm_text': text, 'count': len(group), 'raw_text_samples': sample_values(group, 'raw_text', 5), 'layer_counter': top_counter((r.get('layer', '') for r in group), 20), 'block_counter': top_counter((r.get('parent_block_name', '') for r in group), 20), 'entity_type_counter': top_counter((r.get('entity_type', '') for r in group), 20), 'bbox_minx': minx, 'bbox_miny': miny, 'bbox_maxx': maxx, 'bbox_maxy': maxy, 'bbox_area': area, 'sample_object_ids': sample_values(group, 'object_id', 8)})
        return result

    def build_payload(input_info: Dict[str, Any], catalog: List[Dict[str, Any]], layer_summary: List[Dict[str, Any]], block_summary: List[Dict[str, Any]], text_summary: List[Dict[str, Any]], top_n: int) -> Dict[str, Any]:
        """构造供 LLM 审查对象类别的压缩 payload。
        """
        selected = sorted(catalog, key=lambda r: (int(r.get('llm_review_priority', 0)), int(r.get('count', 0))), reverse=True)[:top_n]
        compact_catalog = [{'signature_id': row['signature_id'], 'count': row['count'], 'layer': row['layer'], 'entity_type': row['entity_type'], 'geometry_kind': row['geometry_kind'], 'color': row['color'], 'linetype': row['linetype'], 'is_closed': row['is_closed'], 'parent_block_name': row['parent_block_name'], 'block_path_sample': row['block_path_sample'], 'norm_text_sample': row['norm_text_sample'], 'raw_text_sample': row['raw_text_sample'], 'source_counter': row['source_counter'], 'bbox_area': row['bbox_area']} for row in selected]
        return {'task': 'cad_object_category_review_for_fire_inspection_route', 'agent_scope': 'CAD Vector Inventory Agent only extracts and aggregates CAD vector information. It does not decide object roles.', 'unique_input_source': input_info, 'business_role_options': BUSINESS_ROLE_OPTIONS, 'non_business_fallback': {'role': 'not_route_related', 'description': 'Use this only when the catalog signature does not belong to any of the three business object categories. This is not a business object category; it is a review fallback to avoid forcing dimensions, axis marks, legends, parking marks, and annotations into the three target classes.'}, 'review_instruction': ['For each catalog signature, first decide whether it is a non-passable obstacle object.', 'If not an obstacle, decide whether it is a passable abstract door/opening object.', 'If not a passable opening, decide whether it is a non-passable fire inspection object.', 'If none of the three business roles applies, mark it as not_route_related.', 'Return only proposed rules. A human reviewer must confirm them before batch classification.'], 'expected_llm_output_schema': {'rules': [{'signature_id': 'CAT_000001 or empty when using a general pattern', 'match_field': 'layer | parent_block_name | norm_text_sample | entity_type | geometry_kind | color | composite', 'pattern': 'string', 'object_role': 'obstacle_object | passable_opening_object | inspection_object | not_route_related', 'semantic_name': 'short human-readable name', 'confidence': '0-1', 'human_review_required': True, 'reason': 'short reason'}]}, 'catalog_samples': compact_catalog, 'top_layer_summary': layer_summary[:80], 'top_block_summary': block_summary[:80], 'top_text_summary': text_summary[:120]}

    def run_inventory(input_dxf: Path, output_dir: Path, scan_modelspace_only: bool, max_depth: int, top_n_llm: int, expand_all_inserts: bool=False) -> Dict[str, Any]:
        """执行 CAD 图元清单生成主流程。
        主要步骤：
        1. 校验输入 DXF；
        2. 读取 DXF 和文件元信息；
        3. 构建 block signatures；
        4. 扫描 layout/modelspace；
        5. 递归遍历实体并生成 inventory；
        6. 构建 semantic inventory、catalog 和各类 summary；
        7. 写出 CSV/JSON 结果；
        8. 写出 manifest。
        """
        if not input_dxf.exists():
            raise FileNotFoundError(str(input_dxf))
        output_dir.mkdir(parents=True, exist_ok=True)
        input_info = {'path': str(input_dxf), 'size_bytes': input_dxf.stat().st_size, 'modified_time': input_dxf.stat().st_mtime, 'sha256': file_sha256(input_dxf)}
        doc = ezdxf.readfile(str(input_dxf))
        try:
            codepage = str(doc.header.get('$DWGCODEPAGE', ''))
        except Exception:
            codepage = ''
        input_info['dxf_codepage'] = codepage
        block_signatures = build_block_signatures(doc)
        layouts = [doc.modelspace()] if scan_modelspace_only else list(doc.layouts)
        rows: List[Dict[str, Any]] = []
        geometry_rows: List[Dict[str, Any]] = []
        index = 1
        print('=' * 100)
        print('CAD Vector Inventory Agent')
        print(f'Input DXF: {input_dxf}')
        print(f"Input SHA256: {input_info['sha256']}")
        print(f'Output dir: {output_dir}')
        print(f'scan_modelspace_only={scan_modelspace_only}, max_depth={max_depth}, expand_all_inserts={expand_all_inserts}')
        for layout in layouts:
            before = len(rows)
            for entity in layout:
                for item in walk_entity(entity, layout.name, [], [], 0, max_depth, block_signatures=block_signatures, expand_all_inserts=expand_all_inserts):
                    row = make_inventory_row(item, index)
                    rows.append(row)
                    geometry_row = make_geometry_inventory_row(row, item['entity'])
                    if geometry_row:
                        geometry_rows.append(geometry_row)
                    index += 1
            print(f'layout={layout.name}, objects={len(rows) - before}')
        semantic_rows = [row for row in rows if str(row.get('entity_type', '')).upper() in SEMANTIC_ENTITY_TYPES]
        catalog = build_catalog(rows)
        layer_summary = build_group_summary(rows, 'layer')
        block_summary = build_group_summary([r for r in rows if r.get('parent_block_name')], 'parent_block_name')
        text_summary = build_text_summary(rows)
        summary_fields = ['count', 'entity_type_counter', 'geometry_kind_counter', 'layer_counter', 'block_counter', 'color_counter', 'text_samples', 'bbox_minx', 'bbox_miny', 'bbox_maxx', 'bbox_maxy', 'bbox_area']
        write_csv(output_dir / 'cad_object_inventory.csv', rows, INVENTORY_FIELDS)
        write_csv(output_dir / 'cad_semantic_inventory.csv', semantic_rows, INVENTORY_FIELDS)
        write_csv(output_dir / 'cad_geometry_inventory.csv', geometry_rows, GEOMETRY_INVENTORY_FIELDS)
        write_csv(output_dir / 'cad_block_signatures.csv', block_signature_rows(block_signatures), BLOCK_SIGNATURE_FIELDS)
        write_json(output_dir / 'cad_block_signatures.json', {'blocks': block_signatures})
        write_csv(output_dir / 'cad_object_catalog.csv', catalog, CATALOG_FIELDS)
        write_csv(output_dir / 'cad_layer_summary.csv', layer_summary, ['layer'] + summary_fields)
        write_csv(output_dir / 'cad_block_summary.csv', block_summary, ['parent_block_name'] + summary_fields)
        write_csv(output_dir / 'cad_text_summary.csv', text_summary, ['norm_text', 'count', 'raw_text_samples', 'layer_counter', 'block_counter', 'entity_type_counter', 'bbox_minx', 'bbox_miny', 'bbox_maxx', 'bbox_maxy', 'bbox_area', 'sample_object_ids'])
        payload = build_payload(input_info, catalog, layer_summary, block_summary, text_summary, top_n_llm)
        write_json(output_dir / 'llm_object_category_review_payload.json', payload)
        manifest = {'agent': 'CAD Vector Inventory Agent', 'scope': 'extract_and_aggregate_only_no_semantic_decision', 'unique_input_source': input_info, 'scan_modelspace_only': scan_modelspace_only, 'max_insert_depth': max_depth, 'insert_expansion_mode': 'full' if expand_all_inserts else 'selective_semantic_and_structural_geometry', 'removed_low_value_outputs': ['cad_color_summary.csv', 'cad_geometry_type_summary.csv'], 'counts': {'inventory_objects': len(rows), 'semantic_inventory_objects': len(semantic_rows), 'geometry_inventory_objects': len(geometry_rows), 'block_signatures': len(block_signatures), 'catalog_signatures': len(catalog), 'layers': len(layer_summary), 'blocks': len(block_summary), 'unique_texts': len(text_summary), 'colors': len(set((str(r.get('color', '')) for r in rows))), 'geometry_types': len(set((str(r.get('geometry_kind', '')) for r in rows)))}, 'entity_type_count': dict(Counter((r.get('entity_type', '') for r in rows)).most_common()), 'source_count': dict(Counter((r.get('source', '') for r in rows)).most_common()), 'geometry_kind_count': dict(Counter((r.get('geometry_kind', '') for r in rows)).most_common()), 'output_files': {'cad_object_inventory': str(output_dir / 'cad_object_inventory.csv'), 'cad_semantic_inventory': str(output_dir / 'cad_semantic_inventory.csv'), 'cad_geometry_inventory': str(output_dir / 'cad_geometry_inventory.csv'), 'cad_block_signatures_csv': str(output_dir / 'cad_block_signatures.csv'), 'cad_block_signatures_json': str(output_dir / 'cad_block_signatures.json'), 'cad_object_catalog': str(output_dir / 'cad_object_catalog.csv'), 'cad_layer_summary': str(output_dir / 'cad_layer_summary.csv'), 'cad_block_summary': str(output_dir / 'cad_block_summary.csv'), 'cad_text_summary': str(output_dir / 'cad_text_summary.csv'), 'llm_object_category_review_payload': str(output_dir / 'llm_object_category_review_payload.json'), 'inventory_manifest': str(output_dir / 'inventory_manifest.json')}, 'encoding_policy': {'csv': 'utf-8-sig', 'json': 'utf-8', 'console': 'utf-8'}}
        write_json(output_dir / 'inventory_manifest.json', manifest)
        print('=' * 100)
        print('Inventory completed')
        print(json.dumps(manifest['counts'], ensure_ascii=False, indent=2))
        print(f"Catalog: {output_dir / 'cad_object_catalog.csv'}")
        print(f"LLM payload: {output_dir / 'llm_object_category_review_payload.json'}")
        return manifest
    return dict(locals())

_s02_inventory = _register_embedded_module(
    'agents.cad_vector_inventory_agent',
    _build_s02_inventory(),
    aliases=('cad_vector_inventory_agent',),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/cad_vector_extraction.py
# -----------------------------------------------------------------------------
def _build_s02_api():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/cad_vector_extraction.py'
    )
    __name__ = 'fire_inspection_system.cad_vector_extraction'
    __package__ = 'fire_inspection_system'
    import hashlib
    import json
    import sys
    from dataclasses import dataclass
    from pathlib import Path
    from typing import Any
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    AGENTS_DIR = PROJECT_ROOT / 'agents'
    if str(AGENTS_DIR) not in sys.path:
        sys.path.insert(0, str(AGENTS_DIR))
    from cad_vector_inventory_agent import DEFAULT_MAX_INSERT_DEPTH, DEFAULT_TOP_N_LLM, file_sha256, run_inventory
    CAD_AGENT_SCRIPT = PROJECT_ROOT / 'agents' / 'cad_vector_inventory_agent.py'
    DEFAULT_CACHE_ROOT = PROJECT_ROOT / '.cache' / 'inventory'
    REQUIRED_INVENTORY_FILES = ('inventory_manifest.json', 'cad_object_inventory.csv', 'cad_semantic_inventory.csv', 'cad_geometry_inventory.csv', 'cad_block_signatures.json', 'cad_object_catalog.csv')

    @dataclass(frozen=True)
    class CadVectorExtractionResult:
        input_dxf: Path
        sha256: str
        inventory_dir: Path
        manifest_path: Path
        cache_hit: bool
        cache_version: str
        counts: dict[str, Any]

    def stage_version(name: str, paths: list[Path], extras: list[str] | None=None) -> str:
        """Make a cache key from the code and settings that affect extraction."""
        digest = hashlib.sha256(name.encode('utf-8'))
        for path in paths:
            digest.update(str(path.relative_to(PROJECT_ROOT)).encode('utf-8', errors='replace'))
            digest.update(file_sha256(path).encode('ascii') if path.exists() else b'missing')
        for value in extras or []:
            digest.update(str(value).encode('utf-8', errors='replace'))
        return digest.hexdigest()[:20]

    def inventory_cache_version(*, max_depth: int, top_n_llm: int, expand_all_inserts: bool, scan_modelspace_only: bool) -> str:
        mode = 'full_insert_expand' if expand_all_inserts else 'selective_semantic_insert_expand'
        return stage_version('fire-inspection-vector-inventory-v1', [CAD_AGENT_SCRIPT], [str(max_depth), str(top_n_llm), mode, str(scan_modelspace_only)])

    def read_json(path: Path) -> dict[str, Any]:
        with path.open('r', encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}

    def write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def inventory_is_complete(inventory_dir: Path) -> bool:
        return all(((inventory_dir / name).exists() for name in REQUIRED_INVENTORY_FILES))

    def extract_cad_vector_information(input_dxf: Path | str, *, cache_root: Path | str=DEFAULT_CACHE_ROOT, force: bool=False, scan_modelspace_only: bool=True, max_depth: int=DEFAULT_MAX_INSERT_DEPTH, top_n_llm: int=DEFAULT_TOP_N_LLM, expand_all_inserts: bool=False) -> CadVectorExtractionResult:
        """Extract reusable CAD vector inventory from a DXF file.

        The default mode records real INSERT instances and only selectively expands
        block internals that carry semantic information. This keeps the inventory
        useful for inspection-object recognition without inflating every block into
        thousands of inner line segments.
        """
        input_path = Path(input_dxf).expanduser().resolve()
        if not input_path.exists():
            raise FileNotFoundError(input_path)
        if input_path.suffix.lower() != '.dxf':
            raise ValueError(f'只支持 DXF 文件: {input_path}')
        sha256 = file_sha256(input_path)
        version = inventory_cache_version(max_depth=max_depth, top_n_llm=top_n_llm, expand_all_inserts=expand_all_inserts, scan_modelspace_only=scan_modelspace_only)
        inventory_dir = Path(cache_root).resolve() / sha256 / version
        manifest_path = inventory_dir / 'inventory_manifest.json'
        if not force and inventory_is_complete(inventory_dir):
            manifest = read_json(manifest_path)
            counts = manifest.get('counts', {}) if isinstance(manifest.get('counts'), dict) else {}
            return CadVectorExtractionResult(input_dxf=input_path, sha256=sha256, inventory_dir=inventory_dir, manifest_path=manifest_path, cache_hit=True, cache_version=version, counts=counts)
        manifest = run_inventory(input_dxf=input_path, output_dir=inventory_dir, scan_modelspace_only=scan_modelspace_only, max_depth=max_depth, top_n_llm=top_n_llm, expand_all_inserts=expand_all_inserts)
        manifest['cache_version'] = version
        manifest['source_sha256'] = sha256
        write_json(manifest_path, manifest)
        counts = manifest.get('counts', {}) if isinstance(manifest.get('counts'), dict) else {}
        return CadVectorExtractionResult(input_dxf=input_path, sha256=sha256, inventory_dir=inventory_dir, manifest_path=manifest_path, cache_hit=False, cache_version=version, counts=counts)

    # The cache fingerprint must survive removal of the legacy agent source file.
    CAD_AGENT_SCRIPT = Path(globals()["__file__"]).resolve()
    return dict(locals())

_s02_api = _register_embedded_module(
    'fire_inspection_system.cad_vector_extraction',
    _build_s02_api(),
    aliases=('cad_vector_extraction',),
)

# === CONSOLIDATED PUBLIC API ===
from pathlib import Path
from typing import Any


def run_stage(
    input_dxf: Path,
    *,
    force: bool,
    max_depth: int,
    expand_all_inserts: bool,
) -> Any:
    return _s02_api.extract_cad_vector_information(
        input_dxf,
        force=force,
        max_depth=max_depth,
        expand_all_inserts=expand_all_inserts,
    )


__all__ = ["run_stage"]
