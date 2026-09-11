"""按用户选择的巡检对象重新生成单楼层安全巡检路线。

本模块负责控制面的目标选择与顺序。实际几何路径由阶段 10 在 Stage 06
硬障碍物约束、Stage 09 有效自由空间认证后的物理导航图上展开。
"""

from __future__ import annotations

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
    ordered_target_ids: Tuple[str, ...]
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
    }
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
    candidates, by_floor = _candidate_index(candidate_bundle)
    missing_ids = [target_id for target_id in requested_ids if target_id not in candidates]
    if missing_ids:
        raise KeyError("未找到点位 ID: {}".format(", ".join(missing_ids[:20])))

    selected = [candidates[target_id] for target_id in requested_ids]
    selected_floors = {_text(candidate.get("floor_id")) for candidate in selected}
    if len(selected_floors) != 1:
        raise ValueError(
            "接口三一次只能规划一个楼层，当前点位跨楼层: {}".format(
                ", ".join(sorted(selected_floors))
            )
        )
    floor_id = next(iter(selected_floors))

    node_floor, adjacency_by_floor = _load_graph(paths["physical_graph"])
    graph = adjacency_by_floor.get(floor_id) or {}
    if not graph:
        raise SelectedRoutePlanningError(
            "安全物理导航图中没有该楼层",
            code="floor_missing_from_physical_graph",
            details={"floor_id": floor_id},
        )
    access_nodes = {target_id: _access_node_id(candidates[target_id]) for target_id in requested_ids}
    missing_access = [target_id for target_id, node_id in access_nodes.items() if node_id not in graph]
    if missing_access:
        raise SelectedRoutePlanningError(
            "部分选中点位没有安全物理图接入节点",
            code="selected_target_missing_safe_access",
            details={"target_ids": missing_access},
        )

    first_node = access_nodes[requested_ids[0]]
    component_nodes = _connected_nodes(graph, first_node)
    unreachable = [
        target_id for target_id, node_id in access_nodes.items() if node_id not in component_nodes
    ]
    if unreachable:
        raise SelectedRoutePlanningError(
            "选中点位不在同一安全连通分量，已拒绝生成虚拟跳转路线",
            code="selected_targets_physically_disconnected",
            details={
                "floor_id": floor_id,
                "reachable_from": requested_ids[0],
                "unreachable_target_ids": unreachable,
            },
        )

    selected_start = next((candidate for candidate in selected if _start_kind(candidate)), None)
    if selected_start is not None:
        start = selected_start
        start_mode = "user_selected_start_object"
    else:
        priority = {"safety_exit": 0, "stair": 1, "elevator": 2}
        automatic = [
            candidate
            for candidate in by_floor.get(floor_id, [])
            if _start_kind(candidate)
            and _access_node_id(candidate) in component_nodes
            and _access_node_id(candidate) in graph
        ]
        automatic.sort(
            key=lambda candidate: (
                priority.get(_start_kind(candidate), 99),
                _text(candidate.get("target_id")),
            )
        )
        if automatic:
            start = automatic[0]
            start_mode = "automatic_start_object"
        else:
            start = selected[0]
            start_mode = "automatic_selected_target_fallback"

    start_id = _text(start.get("target_id"))
    start_node = _access_node_id(start)
    distances, previous = _shortest_path_tree(graph, start_node)
    unreachable_from_start = [
        target_id for target_id, node_id in access_nodes.items() if node_id not in distances
    ]
    if unreachable_from_start:
        raise SelectedRoutePlanningError(
            "自动起点无法到达全部选中点位",
            code="start_cannot_reach_all_selected_targets",
            details={"start_target_id": start_id, "unreachable_target_ids": unreachable_from_start},
        )

    route_candidates: List[Mapping[str, Any]] = [start]
    route_candidates.extend(candidate for candidate in selected if _text(candidate.get("target_id")) != start_id)
    order = _open_dfs_target_order(start_node, route_candidates, distances, previous)
    if set(requested_ids) - set(order):
        raise SelectedRoutePlanningError(
            "重规划顺序未覆盖全部选中点位",
            code="selected_target_coverage_failed",
            details={"missing_target_ids": sorted(set(requested_ids) - set(order))},
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
    start_kind = _start_kind(start)
    route_segment = {
        "segment_id": "{}_USER_ROUTE_001".format(floor_id),
        "floor_id": floor_id,
        "physical_floor_id": floor_id.split("__", 1)[0],
        "building_scope_id": floor_id,
        "entry_mode": start_mode,
        "entry_label": "用户选择起点" if start_mode == "user_selected_start_object" else "系统自动起点",
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
        "required_target_ids": requested_ids,
        "support_target_ids": [] if start_id in requested_ids else [start_id],
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
                "route_start_policy": "selected_stair_elevator_or_exit_else_automatic_start",
                "route_start_target_ids": [start_id],
                "route_start_constraint_satisfied": bool(start_kind),
                "virtual_entry_count": 0,
                "virtual_continuation_count": 0,
                "selected_target_ids": requested_ids,
                "required_target_ids": requested_ids,
                "support_target_ids": [] if start_id in requested_ids else [start_id],
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
            "ordered_target_ids": order,
            "start_target_id": start_id,
            "start_mode": start_mode,
            "start_kind": start_kind,
            "physical_graph": str(paths["physical_graph"]),
            "virtual_continuation_allowed": False,
        },
    )

    summary = _stage10._s10_pipeline.build_last_inspection_route(
        run,
        optimized_path,
        paths["candidates"],
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
            "door_portal_refinement_enabled": False,
            "independent_raw_cad_collision_enabled": False,
        },
    )

    final_manifest = _read_json(request_path)
    final_manifest.update(
        {
            "route_summary_path": str(route_summary_path),
            "forwarding_route_geojson": str(geojson_path),
            "route_validation": str(route_validation_path),
            "all_selected_targets_covered": set(requested_ids).issubset(order),
        }
    )
    _write_json(request_path, final_manifest)
    dxf_value = _text(summary.get("outputs", {}).get("annotated_route_dxf"))
    return SelectedRouteResult(
        floor_id=floor_id,
        requested_target_ids=tuple(requested_ids),
        ordered_target_ids=tuple(order),
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
