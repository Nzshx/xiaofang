"""按用户选择的巡检对象重新生成单楼层安全巡检路线。

本模块负责控制面的目标选择与顺序。实际几何路径由阶段 10 在 Stage 06
硬障碍物约束、Stage 09 有效自由空间认证后的物理导航图上展开。
"""

from __future__ import annotations

import copy
import heapq
import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Sequence, Tuple

from fire_inspection_system.stages import stage_10_route_outputs as _stage10


def _text(value: Any) -> str:
    return str(value or "").strip()


def _read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON 根节点必须是对象: {}".format(path))
    return value


def _write_json(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path.resolve()


class SelectedRoutePlanningError(RuntimeError):
    """用户选择无法安全组成一条物理路线。"""

    def __init__(self, message: str, *, code: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


@dataclass(frozen=True)
class SelectedRouteResult:
    floor_id: str
    requested_target_ids: Tuple[str, ...]
    planned_target_ids: Tuple[str, ...]
    ordered_target_ids: Tuple[str, ...]
    unreachable_target_reasons: Tuple[Tuple[str, str], ...]
    wall_touch_targets: Tuple[str, ...]
    start_target_id: str
    start_mode: str
    output_dir: Path
    route_summary_path: Path
    forwarding_route_geojson: Path
    route_validation_path: Path
    annotated_route_dxf: Path | None


def normalize_target_ids(values: str | Iterable[Any]) -> List[str]:
    """兼容接口文档的逗号字符串，也接受 JSON 字符串数组。"""

    if isinstance(values, str):
        raw_values: Iterable[Any] = re.split(r"[,，;；\s]+", values)
    else:
        raw_values = values
    result: List[str] = []
    seen = set()
    for value in raw_values:
        target_id = _text(value)
        if target_id and target_id not in seen:
            seen.add(target_id)
            result.append(target_id)
    return result


def _candidate_index(bundle: Mapping[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    by_floor: Dict[str, List[Dict[str, Any]]] = {}
    for floor_id, floor in (bundle.get("floors") or {}).items():
        floor_key = _text(floor_id)
        for raw in (floor or {}).get("candidates", []) or []:
            if not isinstance(raw, dict):
                continue
            candidate = dict(raw)
            target_id = _text(candidate.get("target_id"))
            if not target_id:
                continue
            candidate.setdefault("floor_id", floor_key)
            by_id[target_id] = candidate
            by_floor.setdefault(floor_key, []).append(candidate)
    return by_id, by_floor


def _navigation_target_index(path: Path) -> Dict[str, Dict[str, Any]]:
    """读取识别结果，并保留构造人工选点候选所需的原始属性。"""

    payload = _read_json(path)
    result: Dict[str, Dict[str, Any]] = {}
    for feature in payload.get("features", []) or []:
        if not isinstance(feature, Mapping):
            continue
        properties = dict(feature.get("properties") or {})
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates") or []
        target_id = _text(properties.get("target_id"))
        if (
            not target_id
            or geometry.get("type") != "Point"
            or not isinstance(coordinates, (list, tuple))
            or len(coordinates) < 2
        ):
            continue
        try:
            point = [float(coordinates[0]), float(coordinates[1])]
        except (TypeError, ValueError):
            continue
        result[target_id] = {
            "properties": properties,
            "point": point,
        }
    return result


def _has_positive_graph_connection(
    adjacency_by_floor: Mapping[str, Mapping[str, Sequence[Tuple[str, float]]]],
    floor_id: str,
    node_id: str,
) -> bool:
    return any(
        neighbour != node_id
        for neighbour, _weight in (adjacency_by_floor.get(floor_id) or {}).get(
            node_id, ()
        )
    )


def _nearest_certified_wall_touch(
    *,
    run_dir: Path,
    physical_graph_payload: Mapping[str, Any],
    adjacency_by_floor: Mapping[str, Mapping[str, Sequence[Tuple[str, float]]]],
    point: Sequence[float],
    floor_id: str,
) -> Dict[str, Any] | None:
    """Project an enclosed target to the first safe wall contact from a walkable component.

    The route stays on the certified graph. Its terminal access connector ends
    at the first obstacle boundary between the reachable graph node and target;
    it never continues through the wall to the target center.
    """

    try:
        from shapely.geometry import LineString, Point, shape
        from shapely.ops import nearest_points
    except ImportError:
        return None

    physical_floor = _text(floor_id).split("__", 1)[0]
    obstacle_path = (
        run_dir
        / "obstacles"
        / "per_floor_union"
        / "valid_obstacle_union_{}.geojson".format(physical_floor)
    )
    free_areas_path = (
        run_dir / "path_planning" / "precomputed" / "effective_free_areas.geojson"
    )
    if not obstacle_path.is_file() or not free_areas_path.is_file():
        return None
    try:
        obstacle_features = _read_json(obstacle_path).get("features", []) or []
        obstacle_geometry = shape(obstacle_features[0]["geometry"])
        free_features = _read_json(free_areas_path).get("features", []) or []
        free_geometry = next(
            (
                shape(feature["geometry"])
                for feature in free_features
                if _text((feature.get("properties") or {}).get("floor_id"))
                == floor_id
            ),
            None,
        )
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    if obstacle_geometry.is_empty or free_geometry is None or free_geometry.is_empty:
        return None

    floor_graph = adjacency_by_floor.get(floor_id) or {}
    graph_nodes = {
        _text(row.get("node_id")): dict(row)
        for row in physical_graph_payload.get("nodes", []) or []
        if isinstance(row, Mapping)
        and _text(row.get("floor_id")) == floor_id
        and _text(row.get("node_id")) in floor_graph
    }
    if not graph_nodes:
        return None

    target_point = Point(float(point[0]), float(point[1]))
    nearest_wall_distance = float(obstacle_geometry.distance(target_point))
    # The target-side nearest boundary can be the *inside* face of an enclosed
    # room. A walkable graph node may only see the opposite face, so allow for
    # wall thickness and the short gap to the target, while still rejecting a
    # remote wall that merely happens to block the ray.
    contact_limit = max(1000.0, min(2000.0, nearest_wall_distance * 8.0 + 250.0))

    # Cache connected graph components locally for this target. Only components
    # containing an accepted portal are candidates for approach from walkable
    # circulation space.
    visited: set[str] = set()
    best: Tuple[float, float, str, Dict[str, Any]] | None = None
    for seed, seed_row in graph_nodes.items():
        if seed in visited or _text(seed_row.get("kind")) != "portal":
            continue
        component: set[str] = set()
        stack = [seed]
        visited.add(seed)
        while stack:
            node_id = stack.pop()
            component.add(node_id)
            for neighbour, _weight in floor_graph.get(node_id, ()):
                if neighbour in graph_nodes and neighbour not in visited:
                    visited.add(neighbour)
                    stack.append(neighbour)

        approachable = []
        for node_id in component:
            row = graph_nodes[node_id]
            if _text(row.get("kind")) not in {"portal", "skeleton", "area_anchor"}:
                continue
            try:
                x, y = float(row["x"]), float(row["y"])
            except (KeyError, TypeError, ValueError):
                continue
            distance = math.hypot(x - target_point.x, y - target_point.y)
            approachable.append((distance, node_id, x, y))
        for _distance, node_id, x, y in sorted(approachable)[:160]:
            line = LineString([(x, y), (target_point.x, target_point.y)])
            intersection = line.intersection(obstacle_geometry)
            if intersection.is_empty:
                continue
            contact = nearest_points(Point(x, y), intersection)[1]
            wall_distance = float(contact.distance(target_point))
            if wall_distance > contact_limit:
                continue
            connector = LineString([(x, y), (contact.x, contact.y)])
            if not free_geometry.covers(connector):
                continue
            # The connector may touch the wall at its endpoint, but cannot run
            # along or through the hard obstacle before reaching that endpoint.
            obstacle_overlap = connector.intersection(obstacle_geometry).length
            if obstacle_overlap > 1e-5:
                continue
            access = {
                "floor_id": floor_id,
                "door_id": "",
                "access_node_id": node_id,
                "access_point": [float(contact.x), float(contact.y)],
                "area_ids": {_text(graph_nodes[node_id].get("area_id"))} - {""},
                "evidence": "first_contact_on_certified_obstacle_boundary",
                "confidence": 1.0,
                "wall_distance": wall_distance,
                "wall_touch": True,
            }
            score = (wall_distance, float(connector.length), node_id, access)
            if best is None or score[:3] < best[:3]:
                best = score
    return best[3] if best is not None else None


def _merge_user_selectable_physical_targets(
    candidate_bundle: Mapping[str, Any],
    *,
    run_dir: Path,
    requested_target_ids: Sequence[str],
    navigation_targets_path: Path,
    physical_graph_path: Path,
    adjacency_by_floor: Mapping[str, Mapping[str, Sequence[Tuple[str, float]]]],
) -> Tuple[Dict[str, Any], List[str], Dict[str, str]]:
    """补入用户选中对象，并为不可直达对象建立安全的代访问点。

    自动规划候选仍由业务规则控制。这里只为接口三中用户明确选择的点位
    建立候选记录。对象自身接入节点不可达时，只使用认证自由空间内可触碰
    的障碍墙面；找不到安全墙面接触点时将对象标记为不可达，不会改用门口
    代理，也不会新增穿墙边或把对象中心伪装成可达点。
    """

    merged = copy.deepcopy(dict(candidate_bundle))
    existing, _by_floor = _candidate_index(merged)
    missing_requested = [
        target_id for target_id in requested_target_ids if target_id not in existing
    ]
    recognition = _navigation_target_index(navigation_targets_path)
    graph_payload = _read_json(physical_graph_path)
    graph_nodes_by_id: Dict[str, Dict[str, Any]] = {}
    target_nodes: Dict[str, List[Dict[str, Any]]] = {}
    for raw in graph_payload.get("nodes", []) or []:
        if not isinstance(raw, Mapping):
            continue
        node = dict(raw)
        node_id = _text(node.get("node_id"))
        if node_id:
            graph_nodes_by_id[node_id] = node
        target_id = _text(node.get("target_id"))
        if target_id:
            target_nodes.setdefault(target_id, []).append(node)

    portal_nodes_by_floor: Dict[str, set[str]] = {}
    for raw in graph_payload.get("nodes", []) or []:
        if not isinstance(raw, Mapping) or _text(raw.get("kind")) != "portal":
            continue
        floor_id = _text(raw.get("floor_id"))
        node_id = _text(raw.get("node_id"))
        if floor_id and node_id and _has_positive_graph_connection(
            adjacency_by_floor, floor_id, node_id
        ):
            portal_nodes_by_floor.setdefault(floor_id, set()).add(node_id)
    component_portal_cache: Dict[Tuple[str, str], bool] = {}

    def direct_access_reaches_portal_network(floor_id: str, node_id: str) -> bool:
        if not _has_positive_graph_connection(
            adjacency_by_floor, floor_id, node_id
        ):
            return False
        floor_portals = portal_nodes_by_floor.get(floor_id) or set()
        # 没有入口节点的旧运行无法判定分量是否连接公共路网，保留原直接接入行为。
        if not floor_portals:
            return True
        key = (floor_id, node_id)
        if key not in component_portal_cache:
            component = _connected_nodes(adjacency_by_floor[floor_id], node_id)
            component_portal_cache[key] = bool(component & floor_portals)
        return component_portal_cache[key]
    added: List[str] = []
    wall_touched: List[str] = []
    unavailable: Dict[str, str] = {}
    floors = merged.setdefault("floors", {})
    if not isinstance(floors, dict):
        floors = {}
        merged["floors"] = floors

    def choose_wall_touch(
        target_id: str,
        candidate: Mapping[str, Any] | None,
        recognized: Mapping[str, Any] | None,
    ) -> Dict[str, Any] | None:
        nodes = target_nodes.get(target_id, [])
        properties = dict((recognized or {}).get("properties") or {})
        point = (candidate or {}).get("point") or (recognized or {}).get("point")
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            for node in nodes:
                try:
                    point = [float(node.get("raw_x")), float(node.get("raw_y"))]
                    break
                except (TypeError, ValueError):
                    continue
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            return None
        candidate_floors = [
            (candidate or {}).get("floor_id"),
            properties.get("building_scope_id"),
            properties.get("floor_id"),
        ]
        candidate_floors.extend(node.get("floor_id") for node in nodes)
        for candidate_floor in dict.fromkeys(
            _text(value) for value in candidate_floors if _text(value)
        ):
            wall_touch = _nearest_certified_wall_touch(
                run_dir=run_dir,
                physical_graph_payload=graph_payload,
                adjacency_by_floor=adjacency_by_floor,
                point=point,
                floor_id=candidate_floor,
            )
            if wall_touch is not None:
                return wall_touch
        return None

    def apply_wall_touch(
        candidate: MutableMapping[str, Any], wall_touch: Mapping[str, Any]
    ) -> None:
        candidate.update(
            {
                "floor_id": _text(wall_touch.get("floor_id")),
                "access_point": list(wall_touch.get("access_point") or ()),
                "physical_anchor_point": list(wall_touch.get("access_point") or ()),
                "source_access_node_id": _text(wall_touch.get("access_node_id")),
                "access_node_id": _text(wall_touch.get("access_node_id")),
                "virtual_access": False,
                "virtual_access_distance": 0.0,
                "visit_mode": "wall_touch_proxy",
                "wall_touch_proxy": True,
                "wall_touch_evidence": _text(wall_touch.get("evidence")),
                "selection_basis": "user_selected_certified_wall_touch",
                "access_backend": "certified_physical_navigation_graph_wall_touch",
                "manual_selection_eligible": True,
                "physical_graph_access_materialized": True,
            }
        )

    # 已经是业务候选的对象也可能只有孤立 TARGET 节点。以前这类点会形成
    # 单点分组并得到 0 条边；不可达对象现在只尝试安全墙面接触点。
    requested_set = set(requested_target_ids)
    for floor in floors.values():
        if not isinstance(floor, MutableMapping):
            continue
        for raw_candidate in floor.get("candidates", []) or []:
            if not isinstance(raw_candidate, MutableMapping):
                continue
            target_id = _text(raw_candidate.get("target_id"))
            if target_id not in requested_set:
                continue
            floor_id = _text(raw_candidate.get("floor_id"))
            access_id = _access_node_id(raw_candidate)
            if direct_access_reaches_portal_network(floor_id, access_id):
                continue
            wall_touch = choose_wall_touch(
                target_id, raw_candidate, recognition.get(target_id)
            )
            if wall_touch is None:
                unavailable[target_id] = "no_safe_wall_touch"
                continue
            apply_wall_touch(raw_candidate, wall_touch)
            wall_touched.append(target_id)

    for target_id in missing_requested:
        recognized = recognition.get(target_id)
        if recognized is None:
            unavailable[target_id] = "not_in_recognition_targets"
            continue
        nodes = target_nodes.get(target_id, [])
        usable: List[Tuple[str, str, Dict[str, Any], Dict[str, Any]]] = []
        for node in nodes:
            node_id = _text(node.get("node_id"))
            floor_id = _text(node.get("floor_id"))
            floor_graph = adjacency_by_floor.get(floor_id) or {}
            preferred_access_id = _text(node.get("access_node_id")) or node_id
            access_id = ""
            for candidate_access_id in (preferred_access_id, node_id):
                neighbours = floor_graph.get(candidate_access_id) or ()
                if candidate_access_id and neighbours and direct_access_reaches_portal_network(
                    floor_id, candidate_access_id
                ):
                    access_id = candidate_access_id
                    break
            if not access_id:
                continue
            access_node = graph_nodes_by_id.get(access_id) or node
            usable.append((floor_id, access_id, node, access_node))

        properties = dict(recognized["properties"])
        wall_touch = None
        if usable:
            floor_id, access_id, target_node, access_node = sorted(
                usable,
                key=lambda item: (item[0], item[1], _text(item[2].get("node_id"))),
            )[0]
            try:
                access_point = [float(access_node.get("x")), float(access_node.get("y"))]
                if not all(math.isfinite(value) for value in access_point):
                    raise ValueError
            except (TypeError, ValueError):
                unavailable[target_id] = "invalid_certified_access_coordinate"
                continue
        else:
            wall_touch = choose_wall_touch(target_id, None, recognized)
            if wall_touch is None:
                unavailable[target_id] = (
                    "not_in_certified_physical_graph"
                    if not nodes
                    else "no_safe_wall_touch"
                )
                continue
            floor_id = _text(wall_touch.get("floor_id"))
            access_id = _text(wall_touch.get("access_node_id"))
            access_point = list(wall_touch.get("access_point") or ())
            target_node = nodes[0] if nodes else {}
        target_class = _text(
            properties.get("target_class")
            or properties.get("standard_class_name")
            or properties.get("source_class_name")
            or target_node.get("target_class")
        )
        raw_name = _text(
            properties.get("source_class_name")
            or properties.get("original_object_name")
            or properties.get("raw_name")
            or target_class
        )
        source_object_id = _text(
            properties.get("source_object_id")
            or properties.get("object_id")
            or target_node.get("source_object_id")
        )
        candidate = {
            "target_id": target_id,
            "object_id": source_object_id,
            "source_object_id": source_object_id,
            "floor_id": floor_id,
            "floor_name": _text(properties.get("floor_name") or floor_id),
            "class_name": target_class,
            "target_class": target_class,
            "standard_class_name": target_class,
            "raw_name": raw_name,
            "point": list(recognized["point"]),
            "access_point": access_point,
            "physical_anchor_point": access_point,
            "source_access_node_id": access_id,
            "access_node_id": access_id,
            "virtual_access": bool(target_node.get("virtual_access")),
            "virtual_access_distance": float(
                target_node.get("virtual_access_distance")
                or target_node.get("projection_distance")
                or 0.0
            ),
            "selection_basis": "user_selected_recognized_physical_graph_target",
            "matched_rule_ids": [],
            "mandatory": False,
            "manual_selection_eligible": True,
            "physical_graph_access_materialized": True,
            "access_backend": "certified_physical_navigation_graph",
            "annotation": properties,
        }
        if wall_touch is not None:
            apply_wall_touch(candidate, wall_touch)
            wall_touched.append(target_id)
        floor = floors.setdefault(
            floor_id,
            {"floor_id": floor_id, "candidates": [], "requirements": []},
        )
        if not isinstance(floor, dict):
            floor = {"floor_id": floor_id, "candidates": [], "requirements": []}
            floors[floor_id] = floor
        floor_candidates = floor.get("candidates")
        if not isinstance(floor_candidates, list):
            floor_candidates = []
            floor["candidates"] = floor_candidates
        floor_candidates.append(candidate)
        floor["candidate_count"] = len(floor_candidates)
        flat_targets = merged.get("targets")
        if isinstance(flat_targets, list):
            flat_targets.append(copy.deepcopy(candidate))
        added.append(target_id)

    merged["manual_selection_supplement"] = {
        "policy": "direct_physical_access_else_nearest_certified_wall_touch",
        "nearest_node_snap_allowed": False,
        "candidate_only_wall_gap_allowed": False,
        "requested_missing_business_candidate_ids": missing_requested,
        "supplemented_target_ids": added,
        "wall_touch_target_ids": sorted(set(wall_touched)),
        "unavailable_target_reasons": unavailable,
    }
    return merged, added, unavailable


def _access_node_id(candidate: Mapping[str, Any]) -> str:
    target_id = _text(candidate.get("target_id"))
    return (
        _text(candidate.get("source_access_node_id"))
        or _text(candidate.get("access_node_id"))
        or "TARGET::{}".format(target_id)
    )


def _candidate_words(candidate: Mapping[str, Any]) -> str:
    annotation = candidate.get("annotation") or {}
    values = [
        candidate.get("standard_class_name"),
        candidate.get("class_name"),
        candidate.get("target_class"),
        candidate.get("raw_name"),
        candidate.get("original_object_name"),
        candidate.get("term"),
    ]
    if isinstance(annotation, Mapping):
        values.extend(
            annotation.get(key)
            for key in (
                "target_class",
                "source_class_name",
                "class_name",
                "standard_class_name",
                "raw_name",
                "original_object_name",
                "term",
            )
        )
    return "|".join(_text(value).upper().replace(" ", "") for value in values if _text(value))


def _start_kind(candidate: Mapping[str, Any]) -> str:
    words = _candidate_words(candidate)
    if not words:
        return ""
    if any(marker in words for marker in ("防火门", "DOOR_FIRE", "FIRE_DOOR")):
        return ""
    fire_door_code = re.compile(r"^(?:FM|FGM|FJM)(?:[甲乙丙丁]?\d{0,8}(?:[-_]\d+)?)?$")
    if any(fire_door_code.fullmatch(token) for token in words.split("|") if token):
        return ""
    if any(marker in words for marker in ("安全出口", "疏散出口", "EMERGENCYEXIT", "SAFETYEXIT")):
        return "safety_exit"
    if any(marker in words for marker in ("楼梯", "梯间", "STAIR")):
        return "stair"
    if any(marker in words for marker in ("消防电梯", "电梯", "ELEVATOR", "LIFT")):
        return "elevator"
    return ""


def _load_graph(path: Path) -> Tuple[
    Dict[str, str],
    Dict[str, Dict[str, List[Tuple[str, float]]]],
]:
    payload = _read_json(path)
    node_floor: Dict[str, str] = {}
    adjacency: Dict[str, Dict[str, List[Tuple[str, float]]]] = {}
    for row in payload.get("nodes", []) or []:
        if not isinstance(row, dict):
            continue
        node_id = _text(row.get("node_id"))
        floor_id = _text(row.get("floor_id"))
        if node_id and floor_id:
            node_floor[node_id] = floor_id
            adjacency.setdefault(floor_id, {}).setdefault(node_id, [])
    for row in payload.get("edges", []) or []:
        if not isinstance(row, dict):
            continue
        left = _text(row.get("node_a"))
        right = _text(row.get("node_b"))
        floor_id = _text(row.get("floor_id")) or node_floor.get(left, "")
        if not left or not right or not floor_id:
            continue
        if node_floor.get(left) != floor_id or node_floor.get(right) != floor_id:
            continue
        weight = float(row.get("length") or row.get("routing_cost") or 0.0)
        if weight <= 0.0:
            weight = 1e-9
        adjacency.setdefault(floor_id, {}).setdefault(left, []).append((right, weight))
        adjacency.setdefault(floor_id, {}).setdefault(right, []).append((left, weight))
    return node_floor, adjacency


def _connected_nodes(graph: Mapping[str, Sequence[Tuple[str, float]]], source: str) -> set[str]:
    if source not in graph:
        return set()
    visited = {source}
    stack = [source]
    while stack:
        node = stack.pop()
        for neighbour, _weight in graph.get(node, ()):
            if neighbour not in visited:
                visited.add(neighbour)
                stack.append(neighbour)
    return visited


def _shortest_path_tree(
    graph: Mapping[str, Sequence[Tuple[str, float]]], source: str
) -> Tuple[Dict[str, float], Dict[str, str]]:
    distances: Dict[str, float] = {source: 0.0}
    previous: Dict[str, str] = {}
    queue: List[Tuple[float, str]] = [(0.0, source)]
    settled = set()
    while queue:
        distance, node = heapq.heappop(queue)
        if node in settled:
            continue
        settled.add(node)
        for neighbour, weight in graph.get(node, ()):
            candidate = distance + weight
            old = distances.get(neighbour, math.inf)
            if candidate + 1e-9 < old or (
                abs(candidate - old) <= 1e-9 and node < previous.get(neighbour, "\uffff")
            ):
                distances[neighbour] = candidate
                previous[neighbour] = node
                heapq.heappush(queue, (candidate, neighbour))
    return distances, previous


def _open_dfs_target_order(
    root_node: str,
    route_candidates: Sequence[Mapping[str, Any]],
    distances: Mapping[str, float],
    previous: Mapping[str, str],
) -> List[str]:
    """在根最短路树的必要子树上做开放 DFS，最深分支最后访问。"""

    target_ids_by_node: Dict[str, List[str]] = {}
    input_order: Dict[str, int] = {}
    required_nodes = set()
    for index, candidate in enumerate(route_candidates):
        target_id = _text(candidate.get("target_id"))
        node_id = _access_node_id(candidate)
        input_order[target_id] = index
        target_ids_by_node.setdefault(node_id, []).append(target_id)
        required_nodes.add(node_id)

    children: Dict[str, set[str]] = {}
    union_nodes = {root_node}
    for node in required_nodes:
        cursor = node
        if cursor not in distances:
            continue
        union_nodes.add(cursor)
        while cursor != root_node:
            parent = previous.get(cursor)
            if not parent:
                raise SelectedRoutePlanningError(
                    "无法还原选中目标到起点的安全物理路径",
                    code="missing_shortest_path_predecessor",
                    details={"target_node": node, "cursor": cursor},
                )
            children.setdefault(parent, set()).add(cursor)
            union_nodes.add(parent)
            cursor = parent

    max_distance_cache: Dict[str, float] = {}

    def subtree_max_distance(node: str) -> float:
        if node not in max_distance_cache:
            values = [float(distances.get(node, 0.0))]
            values.extend(subtree_max_distance(child) for child in children.get(node, ()))
            max_distance_cache[node] = max(values)
        return max_distance_cache[node]

    order: List[str] = []

    def visit(node: str) -> None:
        for target_id in sorted(target_ids_by_node.get(node, ()), key=input_order.__getitem__):
            if target_id not in order:
                order.append(target_id)
        ordered_children = sorted(
            children.get(node, ()),
            key=lambda child: (subtree_max_distance(child), child),
        )
        for child in ordered_children:
            visit(child)

    visit(root_node)
    return order


def _resolve_input_paths(run_dir: Path) -> Dict[str, Path]:
    paths = {
        "candidates": run_dir
        / "path_planning"
        / "dual_graph"
        / "physical_access_candidates.json",
        "free_areas": run_dir
        / "path_planning"
        / "precomputed"
        / "effective_free_areas.geojson",
        "physical_graph": run_dir
        / "path_planning"
        / "precomputed"
        / "certified_physical_graph.json",
        "navigation_targets": run_dir
        / "navigation_graph"
        / "inputs"
        / "navigation_targets.geojson",
    }
    summary_path = run_dir / "path_planning" / "path_planning_summary.json"
    if summary_path.is_file():
        summary = _read_json(summary_path)
        if summary.get("planning_mode") == "coverage":
            outputs = summary.get("outputs") or {}
            for key, name in (("candidates", "physical_access_candidates"),
                              ("free_areas", "effective_free_areas"),
                              ("physical_graph", "certified_physical_graph")):
                if outputs.get(name):
                    paths[key] = Path(outputs[name])
    if not paths["physical_graph"].is_file():
        paths["physical_graph"] = (
            run_dir / "area_graph_navigation_refined" / "refined_navigation_graph.json"
        )
    if not paths["candidates"].is_file():
        paths["candidates"] = (
            run_dir
            / "path_planning"
            / "semantic_value_inputs"
            / "target_candidates_with_context.json"
        )
    for label, path in paths.items():
        if not path.is_file():
            raise FileNotFoundError("重规划缺少 {}: {}".format(label, path))
    return {key: value.resolve() for key, value in paths.items()}


def replan_selected_targets(
    run_dir: Path | str,
    selected_target_ids: str | Iterable[Any],
    *,
    output_dir: Path | str | None = None,
    write_dxf: bool = False,
) -> SelectedRouteResult:
    """为一张物理楼层上的用户选择目标生成一条无虚拟跳转路线。"""

    run = Path(run_dir).expanduser().resolve()
    if not (run / "pipeline_summary.json").is_file():
        raise FileNotFoundError("缺少 pipeline_summary.json: {}".format(run))
    requested_ids = normalize_target_ids(selected_target_ids)
    if not requested_ids:
        raise ValueError("cad_points 至少需要一个点位 ID")

    paths = _resolve_input_paths(run)
    candidate_bundle = _read_json(paths["candidates"])
    _node_floor, adjacency_by_floor = _load_graph(paths["physical_graph"])
    candidate_bundle, supplemented_target_ids, unavailable_target_reasons = (
        _merge_user_selectable_physical_targets(
            candidate_bundle,
            run_dir=run,
            requested_target_ids=requested_ids,
            navigation_targets_path=paths["navigation_targets"],
            physical_graph_path=paths["physical_graph"],
            adjacency_by_floor=adjacency_by_floor,
        )
    )
    candidates, _by_floor = _candidate_index(candidate_bundle)
    unreachable_reasons: Dict[str, str] = {}
    eligible: List[Tuple[str, str, str]] = []
    for target_id in requested_ids:
        candidate = candidates.get(target_id)
        if candidate is None:
            unreachable_reasons[target_id] = unavailable_target_reasons.get(
                target_id, "not_in_certified_physical_graph"
            )
            continue
        candidate_floor = _text(candidate.get("floor_id"))
        candidate_graph = adjacency_by_floor.get(candidate_floor) or {}
        if not candidate_graph:
            unreachable_reasons[target_id] = "floor_missing_from_physical_graph"
            continue
        access_node_id = _access_node_id(candidate)
        if access_node_id not in candidate_graph:
            unreachable_reasons[target_id] = "safe_access_node_missing"
            continue
        if not _has_positive_graph_connection(
            adjacency_by_floor, candidate_floor, access_node_id
        ):
            unreachable_reasons[target_id] = unavailable_target_reasons.get(
                target_id, "no_connected_certified_access_node"
            )
            continue
        eligible.append((target_id, candidate_floor, access_node_id))

    if not eligible:
        raise SelectedRoutePlanningError(
            "全部选中点位均没有可用的安全物理图接入节点",
            code="selected_target_not_user_selectable",
            details={
                "target_ids": requested_ids[:20],
                "reasons": {
                    target_id: unreachable_reasons.get(target_id, "unknown")
                    for target_id in requested_ids[:20]
                },
            },
        )

    # 接口没有单独传递建筑/连通分量。若用户先点中了一个孤立区域，直接
    # 使用第一个点作为锚点会把后面真正能互相到达的一组点全部排除，最终
    # 只能得到一个点、没有可见路线。这里选择包含用户已选点数量最多的
    # “内部楼层 + 物理连通分量”；数量相同时仍按用户选择顺序决定。
    best_anchor = eligible[0]
    best_member_count = 0
    for candidate_anchor in eligible:
        _, candidate_floor, candidate_node = candidate_anchor
        candidate_graph = adjacency_by_floor[candidate_floor]
        candidate_component = _connected_nodes(candidate_graph, candidate_node)
        candidate_member_count = sum(
            1
            for _, member_floor, member_node in eligible
            if member_floor == candidate_floor and member_node in candidate_component
        )
        if candidate_member_count > best_member_count:
            best_anchor = candidate_anchor
            best_member_count = candidate_member_count

    anchor_target_id, floor_id, first_node = best_anchor
    graph = adjacency_by_floor[floor_id]
    component_nodes = _connected_nodes(graph, first_node)
    planned_target_ids: List[str] = []
    access_nodes: Dict[str, str] = {}
    for target_id, candidate_floor, access_node_id in eligible:
        if candidate_floor != floor_id:
            unreachable_reasons[target_id] = "point_on_different_internal_floor"
            continue
        if access_node_id not in component_nodes:
            unreachable_reasons[target_id] = "physically_disconnected"
            continue
        planned_target_ids.append(target_id)
        access_nodes[target_id] = access_node_id

    selected = [candidates[target_id] for target_id in planned_target_ids]
    wall_touch_targets = tuple(
        target_id
        for target_id in planned_target_ids
        if candidates[target_id].get("wall_touch_proxy")
    )

    selected_start = next((candidate for candidate in selected if _start_kind(candidate)), None)
    if selected_start is not None:
        start = selected_start
        start_mode = "user_selected_start_object"
    else:
        # 用户重规划只允许使用本次明确选择的点。未选择安全出口、楼梯或
        # 电梯时，直接以请求顺序中的第一个可达点为起点，绝不从楼层候选
        # 中自动补入安全出口，否则图片会出现用户没有选择的长接入路线。
        start = selected[0]
        start_mode = "user_selected_first_target"

    start_id = _text(start.get("target_id"))
    start_node = _access_node_id(start)
    distances, previous = _shortest_path_tree(graph, start_node)
    unreachable_from_start = [
        target_id for target_id, node_id in access_nodes.items() if node_id not in distances
    ]
    if unreachable_from_start:
        for target_id in unreachable_from_start:
            unreachable_reasons[target_id] = "unreachable_from_route_start"
            access_nodes.pop(target_id, None)
        planned_target_ids = [
            target_id
            for target_id in planned_target_ids
            if target_id not in unreachable_from_start
        ]
        selected = [candidates[target_id] for target_id in planned_target_ids]

    route_candidates: List[Mapping[str, Any]] = [start]
    route_candidates.extend(candidate for candidate in selected if _text(candidate.get("target_id")) != start_id)
    order = _open_dfs_target_order(start_node, route_candidates, distances, previous)
    if set(planned_target_ids) - set(order):
        raise SelectedRoutePlanningError(
            "重规划顺序未覆盖全部选中点位",
            code="selected_target_coverage_failed",
            details={
                "missing_target_ids": sorted(set(planned_target_ids) - set(order))
            },
        )
    if not order or order[0] != start_id:
        order = [start_id] + [target_id for target_id in order if target_id != start_id]

    output = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else run
        / "path_planning"
        / "user_selected_routes"
        / time.strftime("%Y%m%d_%H%M%S")
    )
    output.mkdir(parents=True, exist_ok=True)
    merged_candidates_path = _write_json(
        output / "user_selected_physical_access_candidates.json",
        candidate_bundle,
    )
    start_kind = _start_kind(start)
    route_segment = {
        "segment_id": "{}_USER_ROUTE_001".format(floor_id),
        "floor_id": floor_id,
        "physical_floor_id": floor_id.split("__", 1)[0],
        "building_scope_id": floor_id,
        "entry_mode": start_mode,
        "entry_label": "用户选择起点",
        "virtual_entry_id": "",
        "virtual_continuation": False,
        "virtual_entry_is_not_physical_path": False,
        "continuation_break_reason": "",
        "physically_reachable_from_floor_route_start": True,
        "route_start_constraint_applies": bool(start_kind),
        "entry_target_id": start_id,
        "entry_target_class_name": _text(
            start.get("standard_class_name") or start.get("class_name") or start.get("target_class")
        ),
        "route_start_constraint_satisfied": bool(start_kind),
        "physical_component_id": "{}_SAFE_COMPONENT".format(floor_id),
        "ordered_target_ids": order,
        "first_visit_order": order,
        "required_target_ids": planned_target_ids,
        "support_target_ids": [],
        "relay_target_ids": [],
    }
    optimized = {
        "schema_version": 1,
        "pipeline_type": "user_selected_targets_safe_physical_open_dfs",
        "physical_floor_primary_scopes": {floor_id.split("__", 1)[0]: floor_id},
        "floors": {
            floor_id: {
                "floor_id": floor_id,
                "physical_floor_id": floor_id.split("__", 1)[0],
                "building_scope_id": floor_id,
                "physical_floor_primary_scope_id": floor_id,
                "physical_floor_primary_route": True,
                "status": "feasible",
                "feasible": True,
                "solver": "user_selected_rooted_shortest_path_tree_open_dfs",
                "route_type": "single_safe_physical_walk_without_virtual_continuation",
                "route_segments": [route_segment],
                "route_segment_count": 1,
                "route_start_policy": "selected_start_object_else_first_user_selected_target",
                "route_start_target_ids": [start_id],
                "route_start_constraint_satisfied": bool(start_kind),
                "virtual_entry_count": 0,
                "virtual_continuation_count": 0,
                "selected_target_ids": planned_target_ids,
                "required_target_ids": planned_target_ids,
                "support_target_ids": [],
                "order": order,
                "first_visit_order": order,
                "length": 0.0,
                "selection_reason": "all_user_selected_targets_are_hard_required",
                "target_order_unique": True,
                "repeated_target_visit_count": 0,
            }
        },
    }
    optimized_path = _write_json(output / "user_selected_optimized_target_order.json", optimized)
    request_path = _write_json(
        output / "user_selected_route_request.json",
        {
            "cad_run_dir": str(run),
            "floor_id": floor_id,
            "requested_target_ids": requested_ids,
            "planned_target_ids": planned_target_ids,
            "unreachable_target_reasons": unreachable_reasons,
            "anchor_target_id": anchor_target_id,
            "ordered_target_ids": order,
            "start_target_id": start_id,
            "start_mode": start_mode,
            "start_kind": start_kind,
            "physical_graph": str(paths["physical_graph"]),
            "source_business_candidates": str(paths["candidates"]),
            "effective_user_selected_candidates": str(merged_candidates_path),
            "supplemented_recognized_target_ids": supplemented_target_ids,
            "wall_touch_targets": list(wall_touch_targets),
            "virtual_continuation_allowed": False,
        },
    )

    summary = _stage10._s10_pipeline.build_last_inspection_route(
        run,
        optimized_path,
        merged_candidates_path,
        paths["free_areas"],
        output_dir=output / "physical_walk",
        refined_graph_path=paths["physical_graph"],
        floors=[floor_id],
        write_dxf=write_dxf,
    )
    route_summary_path = Path(summary["summary_path"]).resolve()
    forwarding_path = Path(summary["outputs"]["forwarding_route"]).resolve()
    geojson_path = Path(summary["outputs"]["forwarding_route_geojson"]).resolve()
    floor_result = (summary.get("floor_results") or {}).get(floor_id) or {}
    if not bool(floor_result.get("feasible")):
        raise SelectedRoutePlanningError(
            "安全物理转发层无法完成全部选中点位",
            code="physical_forwarding_failed",
            details={"floor_result": floor_result, "summary_path": str(route_summary_path)},
        )
    route_validation_path = _write_json(
        output / "route_validation.json",
        {
            "accepted": True,
            "floor_id": floor_id,
            "physical_graph": str(paths["physical_graph"]),
            "forwarding_route": str(forwarding_path),
            "stage06_hard_obstacles_and_topology_repairs": True,
            "stage09_effective_free_space_graph_certification": True,
            "wall_touch_targets": list(wall_touch_targets),
            "independent_raw_cad_collision_enabled": False,
        },
    )

    final_manifest = _read_json(request_path)
    final_manifest.update(
        {
            "route_summary_path": str(route_summary_path),
            "forwarding_route_geojson": str(geojson_path),
            "route_validation": str(route_validation_path),
            "all_planned_targets_covered": set(planned_target_ids).issubset(order),
            "all_selected_targets_covered": not unreachable_reasons
            and set(requested_ids).issubset(order),
        }
    )
    _write_json(request_path, final_manifest)
    dxf_value = _text(summary.get("outputs", {}).get("annotated_route_dxf"))
    return SelectedRouteResult(
        floor_id=floor_id,
        requested_target_ids=tuple(requested_ids),
        planned_target_ids=tuple(planned_target_ids),
        ordered_target_ids=tuple(order),
        unreachable_target_reasons=tuple(
            (target_id, unreachable_reasons[target_id])
            for target_id in requested_ids
            if target_id in unreachable_reasons
        ),
        wall_touch_targets=wall_touch_targets,
        start_target_id=start_id,
        start_mode=start_mode,
        output_dir=output,
        route_summary_path=route_summary_path,
        forwarding_route_geojson=geojson_path,
        route_validation_path=route_validation_path,
        annotated_route_dxf=Path(dxf_value).resolve() if dxf_value else None,
    )


__all__ = [
    "SelectedRoutePlanningError",
    "SelectedRouteResult",
    "normalize_target_ids",
    "replan_selected_targets",
]
