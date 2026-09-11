from __future__ import annotations

from collections import Counter
from pathlib import Path

from ..common import floor_sort_key, read_json, write_csv, write_json


CONTINUOUS_INFRASTRUCTURE_CATEGORIES = frozenset(
    {"管网", "管网与喷头", "布线", "风管", "管道"}
)
CONTINUOUS_ENTITY_TYPES = frozenset({"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "SPLINE"})


def _is_continuous_detection(item: dict[str, object]) -> bool:
    return (
        str(item.get("category") or "").strip() in CONTINUOUS_INFRASTRUCTURE_CATEGORIES
        or str(item.get("entity_type") or "").upper() in CONTINUOUS_ENTITY_TYPES
    )


def run_stage(
    detections_path: Path,
    registrations_path: Path,
    migration_path: Path,
    source_annotations: list[str],
    output_dxf: str,
    stage_dir: Path,
    obstacle_summary: dict[str, object] | None = None,
    simplified_summary: dict[str, object] | None = None,
    building_object_summary: dict[str, object] | None = None,
) -> dict[str, object]:
    stage_dir.mkdir(parents=True, exist_ok=True)
    all_detections = read_json(detections_path)
    detections = [item for item in all_detections if not _is_continuous_detection(item)]
    excluded_continuous_count = len(all_detections) - len(detections)
    registrations = read_json(registrations_path)
    migrations = read_json(migration_path)
    actual_entities = sum(bool(item.get("target_entity_handle")) for item in migrations)
    original_layers = len({item.get("target_layer") for item in migrations if item.get("target_layer")})
    name_labels = sum(bool(item.get("label_handle")) for item in migrations)
    strict_registrations = sum(item.get("acceptance_level", "strict" if item.get("accepted") else "rejected") == "strict" for item in registrations)
    approximate_registrations = sum(item.get("acceptance_level") == "approximate" for item in registrations)
    manual_registrations = sum(item.get("acceptance_level") == "manual" for item in registrations)
    approximate_objects = sum(item.get("migrated") and item.get("registration_acceptance_level") == "approximate" for item in migrations)
    room_guard_counts = Counter(
        str(item.get("room_guard_status", "not_applicable")) for item in migrations
        if item.get("source_entity_type") == "INSERT"
    )
    spatial_reference_counts = Counter(
        str(item.get("spatial_reference_status", "not_applicable"))
        for item in migrations if item.get("discipline") == "hvac"
    )
    recognized_counts: Counter[tuple[str, str, str]] = Counter(
        (item["discipline"], item["floor"], item["category"]) for item in detections
    )
    migrated_counts: Counter[tuple[str, str, str]] = Counter(
        (item["discipline"], item["floor"], item["category"])
        for item in migrations
        if item["migrated"]
    )
    rows = [
        {
            "discipline": key[0],
            "floor": key[1],
            "category": key[2],
            "recognized": value,
            "migrated": migrated_counts.get(key, 0),
            "not_migrated": value - migrated_counts.get(key, 0),
        }
        for key, value in sorted(recognized_counts.items(), key=lambda pair: (pair[0][0], floor_sort_key(pair[0][1]), pair[0][2]))
    ]
    write_csv(stage_dir / "巡检对象数量_按楼层类别.csv", rows)
    totals: Counter[tuple[str, str]] = Counter()
    migrated_totals: Counter[tuple[str, str]] = Counter()
    for item in detections:
        totals[(item["discipline"], item["floor"])] += 1
    for item in migrations:
        if item["migrated"]:
            migrated_totals[(item["discipline"], item["floor"])] += 1
    floor_rows = [
        {
            "discipline": key[0],
            "floor": key[1],
            "recognized": value,
            "migrated": migrated_totals.get(key, 0),
            "not_migrated": value - migrated_totals.get(key, 0),
        }
        for key, value in sorted(totals.items(), key=lambda pair: (pair[0][0], floor_sort_key(pair[0][1])))
    ]
    write_csv(stage_dir / "巡检对象数量_按楼层汇总.csv", floor_rows)

    lines = [
        "# 建筑—水电暖多图纸识别、融合与迁移结果",
        "",
        f"- 建筑专业巡检对象：{int((building_object_summary or {}).get('building_object_count', 0))}",
        f"- 水电暖专业离散巡检对象：{len(detections)}",
        f"- 迁移前排除的连续管网/布线/风管：{excluded_continuous_count}",
        f"- 成功迁移：{sum(item['migrated'] for item in migrations)}",
        f"- 未迁移：{sum(not item['migrated'] for item in migrations)}",
        f"- 严格配准通过：{strict_registrations} 张楼层分图",
        f"- 导航级近似通过：{approximate_registrations} 张楼层分图，涉及 {approximate_objects} 个迁移对象",
        f"- 人工锚点通过：{manual_registrations} 张楼层分图",
        f"- 配准总通过：{sum(item['accepted'] for item in registrations)} / {len(registrations)} 张楼层分图",
        f"- 实际源图元复制：{actual_entities}",
        f"- 保留的源图层种类：{original_layers}",
        f"- 同原图层类别文字：{name_labels}",
        f"- 同名房间直接确认：{room_guard_counts['verified_same_room']}",
        f"- 同名房间内小幅纠偏：{room_guard_counts['adjusted_to_same_room']}",
        f"- 轴网位置原样保留：{sum(value for key, value in room_guard_counts.items() if key.startswith('axis_mapping_kept'))}",
        f"- 房间步骤拒绝：{sum(value for key, value in room_guard_counts.items() if key.startswith('rejected_'))}",
        f"- 暖通建筑空间参考外但已迁移：{spatial_reference_counts['outside_architecture_reference_migrated']}",
        f"- 房间/穿墙逐设备审批表：`{migration_path.parent / 'room_topology_audit.csv'}`",
        f"- 最终建筑实际图元 DXF：`{output_dxf}`",
        f"- 路径规划全专业离散巡检对象：{int((simplified_summary or {}).get('retained_discrete_object_count', 0))}",
        f"- 建筑图跨专业类别拒绝：{int((building_object_summary or {}).get('discipline_scope_rejected_count', 0))}",
        f"- 排除的连续管网/布线/风管：{int((simplified_summary or {}).get('continuous_infrastructure_excluded', 0))}",
        f"- 路径目标清单：`{(simplified_summary or {}).get('route_targets_json', '')}`",
        f"- 建筑障碍物面：{int((obstacle_summary or {}).get('obstacle_polygon_count', 0))}",
        f"- 建筑明确门实体复核：{int((obstacle_summary or {}).get('door_opening_mask_count', 0))}",
        f"- 不可通行洞口：{int((obstacle_summary or {}).get('non_passable_opening_count', 0))}",
        f"- 建筑障碍物标注 DXF：`{(obstacle_summary or {}).get('annotated_dxf', '')}`",
        "",
        "## 按楼层汇总",
        "",
        "| 专业 | 楼层 | 识别 | 已迁移 | 未迁移 |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in floor_rows:
        name = {"building": "建筑", "water": "水", "electrical": "电", "hvac": "暖通"}.get(
            row["discipline"], row["discipline"]
        )
        lines.append(
            f"| {name} | {row['floor']} | {row['recognized']} | {row['migrated']} | {row['not_migrated']} |"
        )
    if any(item["discipline"] == "hvac" for item in detections):
        rule_path = Path(__file__).resolve().parents[2] / "configs" / "inspection_object_rules.json"
        categories = list(dict.fromkeys(item["category"] for item in read_json(rule_path).get("hvac", [])))
        hvac_recognized = Counter(item["category"] for item in detections if item["discipline"] == "hvac")
        hvac_migrated = Counter(item["category"] for item in migrations if item["discipline"] == "hvac" and item["migrated"])
        lines.extend(["", "## 暖通类别覆盖（包含零结果）", "",
                      "识别对象只有已迁移或拒绝两种结果；建筑占用包络和结构凸包仅供后续路径自动筛选。", "",
                      "| 类别 | 识别候选 | 已迁移 | 未迁移 |", "|---|---:|---:|---:|"])
        for category in categories:
            recognized, migrated = hvac_recognized[category], hvac_migrated[category]
            lines.append(f"| {category} | {recognized} | {migrated} | {recognized - migrated} |")
        lines.extend(["", "自然通风、自然排烟等只有说明文字时不强行生成巡检对象；零结果表示未获得足够的具体实体识别证据。"])
    lines.extend(["", "## 原专业图识别标注", ""])
    lines.extend(f"- `{path}`" for path in source_annotations)
    lines.extend(
        [
            "",
            "## 审计说明",
            "",
            "- 数量不执行原抽检配额，当前列出算法识别到的全部可信对象。",
            "- 默认导航级策略：高精度结果标为 strict；有大量公共几何锚点且偏差较小的结果标为 approximate，两者均直接形成迁移或拒绝结果。",
            "- 无轴网或建筑底图图元时，可由同一专业文件至少3张高置信物理图框形成多楼层共识；该结果标为 approximate。",
            "- 自动方法仍失败时，可通过 --manual-anchors 提供2～3组对应点；该结果标为 manual。",
            "- 无公共锚点、残差过大或比例异常仍然拒绝；可用 --registration-policy strict 恢复仅高精度迁移。",
            "- 无可靠楼栋/楼层对应关系或配准证据的分图不会强制迁移，拒绝原因保留在 unmatched_sheets.csv、registrations.csv 和 migration_audit.csv。",
            "- 设备必须是含实际几何的 INSERT；LINE/LWPOLYLINE/ARC 等连续管网、布线和风管不作为巡检对象。独立 TEXT/MTEXT 仅作语义线索。",
            "- 被排除的独立文字保留在 03_recognition/recognition_rejections.csv，只作为自动拒绝证据，不形成第三种状态。",
            "- 可从标题判定的独立系统图、原理图、剖面详图及引用性文字不建立楼层分图。对已进入识别结果且配准成立的对象，建筑占用包络和结构凸包只记录空间证据，不在迁移阶段拒绝；后续路径规划自动筛除真实建筑范围外对象。",
            "- 暖通只迁移风机、排烟口/送风口、自然通风/排烟实体和防火阀等设备/符号块，不迁移风管线。",
            "- 风机块中的远距离附属图元依据设备本体局部几何范围排除，数量记录在 migration_audit.csv 的 excluded_remote_auxiliary_primitives 字段；源CAD不修改。",
            "- 暖通结构识别兼容 ACS 图层角色及部分设备库语义，其余图层/块名依赖类别关键词或图例证据；当前结果不代表对所有CAD制图习惯的完整识别率。",
            "- 轴网配准是设备迁移的主定位。房间步骤只在源/目标存在局部同名房间证据、且纠偏量不超过楼层对角线0.3%或两倍P95配准残差时做小幅纠偏；其余情况原样保留轴网坐标，不因房间文字连线、缺少房名或靠墙而拒绝。",
            "- 柱属于路线障碍物但不作为房间分隔墙；洞口仍按项目规则统一不可通行。连续管网、布线和风管不进入迁移及路径目标。",
            "- 自然通风/自然排烟仅有文字说明而未绑定具体窗/风口实体时不迁移，识别为0不代表现场不存在。",
            "- 障碍物只从建筑底图提取；水、电、暖通专业图元不会参与墙、柱等障碍物判定。",
            "- 各专业使用独立同名库：建筑图不输出水、电、暖通对象，专业图不输出建筑对象；跨专业命中写入拒绝审计。",
            "- 路径规划使用建筑及各专业中具有明确位置的离散对象。",
            "- 追溯信息保存在实际图元的 DUOTUZHI_INSPECTION XDATA 中；设备类别文字使用对象原图层。",
            "- 路径与验收由阶段07生成；每层必须以建筑安全出口或消防电梯为起点。",
        ]
    )
    report = stage_dir / "结果总览.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    payload = {
        "stage": "06_report",
        "source_detection_total": len(all_detections),
        "recognized_total": len(detections),
        "continuous_infrastructure_excluded_before_migration": excluded_continuous_count,
        "migrated_total": sum(item["migrated"] for item in migrations),
        "rejected_total": sum(not item["migrated"] for item in migrations),
        "registration_accepted": sum(item["accepted"] for item in registrations),
        "registration_strict_accepted": strict_registrations,
        "registration_approximate_accepted": approximate_registrations,
        "registration_manual_accepted": manual_registrations,
        "approximate_migrated_objects": approximate_objects,
        "registration_total": len(registrations),
        "actual_source_entities": actual_entities,
        "original_layer_count": original_layers,
        "name_labels_on_original_layers": name_labels,
        "room_guard_counts": dict(room_guard_counts),
        "spatial_reference_counts": dict(spatial_reference_counts),
        "building_obstacle_polygon_count": int((obstacle_summary or {}).get("obstacle_polygon_count", 0)),
        "building_object_count": int((building_object_summary or {}).get("building_object_count", 0)),
        "building_scope_rejected_count": int((building_object_summary or {}).get("discipline_scope_rejected_count", 0)),
        "building_objects_json": str((building_object_summary or {}).get("building_objects_json", "")),
        "building_door_opening_mask_count": int((obstacle_summary or {}).get("door_opening_mask_count", 0)),
        "building_non_passable_opening_count": int((obstacle_summary or {}).get("non_passable_opening_count", 0)),
        "building_obstacle_annotated_dxf": str((obstacle_summary or {}).get("annotated_dxf", "")),
        "simplified_route_target_dxf": str((simplified_summary or {}).get("output_dxf", "")),
        "inspection_center_count": int((simplified_summary or {}).get("inspection_center_count", 0)),
        "route_target_count": int((simplified_summary or {}).get("route_target_count", 0)),
        "inspection_center_members_csv": str((simplified_summary or {}).get("inspection_center_members_csv", "")),
        "report": str(report.resolve()),
        "counts_by_floor_csv": str((stage_dir / "巡检对象数量_按楼层汇总.csv").resolve()),
        "counts_by_floor_category_csv": str((stage_dir / "巡检对象数量_按楼层类别.csv").resolve()),
    }
    write_json(stage_dir / "stage_summary.json", payload)
    return payload
