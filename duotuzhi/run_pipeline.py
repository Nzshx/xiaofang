from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
WORKSPACE_ROOT = PROJECT_DIR.parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from multi_drawing_pipeline.cache_support import (
    clear_document_cache,
    content_cache_key,
    file_sha256,
    json_cache_key,
)
from multi_drawing_pipeline.common import read_json, write_json
from multi_drawing_pipeline.stages import (
    stage_01_inputs,
    stage_02_sheets,
    stage_02b_building_obstacles,
    stage_02c_building_objects,
    stage_03_recognize,
    stage_04_register,
    stage_05_migrate,
    stage_05b_route_targets,
    stage_06_report,
    stage_06_versioned_environment,
    stage_07_route_planning,
)


def choose_project_folder() -> Path:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        selected = filedialog.askdirectory(title="选择包含建筑、水、电、暖通图纸的项目文件夹")
    finally:
        root.destroy()
    if not selected:
        raise RuntimeError("未选择项目文件夹")
    return Path(selected)


def choose_files_manually() -> tuple[list[Path], list[Path], list[Path], list[Path]]:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        building = filedialog.askopenfilename(title="选择唯一建筑目标 DWG/DXF", filetypes=[("CAD", "*.dwg *.dxf")])
        water = filedialog.askopenfilenames(title="选择水专业 DWG/DXF（可多选）", filetypes=[("CAD", "*.dwg *.dxf")])
        electrical = filedialog.askopenfilenames(title="选择电专业 DWG/DXF（可多选）", filetypes=[("CAD", "*.dwg *.dxf")])
        hvac = filedialog.askopenfilenames(title="选择暖通专业 DWG/DXF（可多选）", filetypes=[("CAD", "*.dwg *.dxf")])
    finally:
        root.destroy()
    if not building:
        raise RuntimeError("必须选择1张建筑图；水、电、暖通专业图可不选")
    return (
        [Path(building)], [Path(path) for path in water],
        [Path(path) for path in electrical], [Path(path) for path in hvac],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="通用建筑+水+电+暖通多图纸巡检对象识别和迁移")
    parser.add_argument("--project-root", type=Path, help="项目根目录；自动扫描并分类其中的 DWG/DXF")
    parser.add_argument("--building", type=Path, action="append", help="建筑目标 DWG/DXF；必须且只能1张")
    parser.add_argument(
        "--water", type=Path, nargs="+", action="extend",
        help="水专业 DWG/DXF；一次可选多张，也可重复使用该参数",
    )
    parser.add_argument(
        "--electrical", type=Path, nargs="+", action="extend",
        help="电专业 DWG/DXF；一次可选多张，也可重复使用该参数",
    )
    parser.add_argument(
        "--hvac", type=Path, nargs="+", action="extend",
        help="暖通专业 DWG/DXF；一次可选多张，也可重复使用该参数",
    )
    parser.add_argument("--interactive", action="store_true", help="弹窗选择一个项目文件夹并自动分类")
    parser.add_argument("--manual-files", action="store_true", help="弹窗分别选择建筑、水、电、暖通文件")
    parser.add_argument("--output", type=Path, help="本次运行输出目录；不写入源图目录")
    parser.add_argument("--start-stage", type=int, choices=range(1, 8), default=1, help="从指定阶段续跑")
    parser.add_argument("--scan-only", action="store_true", help="只扫描文件并打印分类，不转换、不执行识别")
    parser.add_argument(
        "--registration-policy", choices=("navigation", "strict"), default="navigation",
        help="配准标准：navigation允许有充分锚点的小偏差近似迁移；strict仅接受高精度配准",
    )
    parser.add_argument(
        "--manual-anchors", type=Path,
        help="可选人工对应点JSON；自动方法失败时用2～3组同名点计算相似变换",
    )
    parser.add_argument(
        "--rebuild-route-targets", action="store_true",
        help="不重跑迁移，仅根据已有融合图重新生成离散巡检目标",
    )
    parser.add_argument("--skip-route-planning", action="store_true", help="只完成融合与审计，不生成巡检路径")
    parser.add_argument("--no-path-dxf", action="store_true", help="生成路径数据和验收报告，但不写路线DXF")
    parser.add_argument("--path-device", default="auto", help="路径模型运行设备：auto/cpu/cuda")
    parser.add_argument("--path-transition-top-k", type=int, default=20, help="语义候选转移边数量")
    parser.add_argument("--path-dmax-ratio", type=float, default=0.8, help="单段最大距离相对楼层尺度")
    parser.add_argument("--area-graph-pixel-size", type=float, default=240.0, help="物理导航栅格尺寸（CAD单位）")
    parser.add_argument("--no-llm", action="store_true", help="关闭建筑专业对象识别的LLM语义兜底")
    parser.add_argument(
        "--force-analysis-cache", action="store_true",
        help="忽略跨运行分析缓存并完整重算；正常使用无需开启",
    )
    parser.add_argument(
        "--skip-obstacle-annotation",
        action="store_true",
        help="不生成过程审阅DXF（兼容旧参数；当前已经是默认行为）",
    )
    parser.add_argument(
        "--write-review-dxf",
        action="store_true",
        help="显式生成障碍物标注和各专业原图识别标注DXF；默认仅保留JSON/CSV",
    )
    parser.add_argument(
        "--write-fused-dxf",
        action="store_true",
        help="显式生成迁移对象融合DXF；输出最终路线DXF时会自动启用",
    )
    parser.add_argument(
        "--artifact-root", type=Path, default=PROJECT_DIR / "artifacts",
        help="SBM、Object Set、环境和路线的内容寻址版本库",
    )
    parser.add_argument("--rgcn-dataset-manifest", type=Path, default=stage_07_route_planning.DEFAULT_DATASET_MANIFEST)
    parser.add_argument("--rgcn-checkpoint", type=Path, default=stage_07_route_planning.DEFAULT_RGCN_CHECKPOINT)
    parser.add_argument("--route-head-checkpoint", type=Path, default=stage_07_route_planning.DEFAULT_ROUTE_HEAD_CHECKPOINT)
    return parser.parse_args()


def _safe_run_name(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", value).strip("_")
    return cleaned[:60] or "cad_project"


def _copy_if_present(source: Path, destination: Path) -> bool:
    if not source.is_file():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return True


def _materialize_building_obstacle_reference(
    summary: dict[str, object],
    stage_dir: Path,
    *,
    cache_key: str,
    cache_hit: bool,
) -> dict[str, object]:
    """Keep small/essential run artifacts while large inventories stay cached."""
    stage_dir.mkdir(parents=True, exist_ok=True)
    cached_root = Path(str(summary["obstacles_geojson"])).resolve().parent
    for name in (
        "building_obstacles.json",
        "building_obstacles.geojson",
        "door_opening_masks.csv",
        "non_passable_openings.csv",
    ):
        _copy_if_present(cached_root / name, stage_dir / name)
    contract_source = (
        cached_root / "local_route_core" / "obstacles"
        / "floor_obstacle_recognition_result.json"
    )
    _copy_if_present(
        contract_source,
        stage_dir / "local_route_core" / "obstacles"
        / "floor_obstacle_recognition_result.json",
    )
    result = dict(summary)
    result.update({
        "obstacles_json": str((stage_dir / "building_obstacles.json").resolve()),
        "obstacles_geojson": str((stage_dir / "building_obstacles.geojson").resolve()),
        "door_opening_mask_csv": str((stage_dir / "door_opening_masks.csv").resolve()),
        "non_passable_opening_csv": str((stage_dir / "non_passable_openings.csv").resolve()),
        "persistent_cache": {
            "key": cache_key,
            "hit": cache_hit,
            "large_inventory_location": str(cached_root.resolve()),
        },
    })
    write_json(stage_dir / "stage_summary.json", result)
    return result


def _materialize_building_object_reference(
    summary: dict[str, object],
    stage_dir: Path,
    *,
    cache_key: str,
    cache_hit: bool,
) -> dict[str, object]:
    stage_dir.mkdir(parents=True, exist_ok=True)
    cached_root = Path(str(summary["building_objects_json"])).resolve().parent
    for name in (
        "building_objects.json",
        "building_objects.csv",
        "discipline_scope_rejections.json",
        "building_object_counts_by_floor.csv",
    ):
        _copy_if_present(cached_root / name, stage_dir / name)
    result = dict(summary)
    result.update({
        "building_objects_json": str((stage_dir / "building_objects.json").resolve()),
        "persistent_cache": {
            "key": cache_key,
            "hit": cache_hit,
            "recognition_runtime_location": str(cached_root.resolve()),
        },
    })
    write_json(stage_dir / "stage_summary.json", result)
    return result


def _resolve_inputs(args: argparse.Namespace) -> tuple[Path | None, list[Path], list[Path], list[Path], list[Path]]:
    if args.manual_files:
        building, water, electrical, hvac = choose_files_manually()
        return None, building, water, electrical, hvac
    project_root = choose_project_folder() if args.interactive or (
        not args.project_root and not args.building and args.start_stage == 1
    ) else args.project_root
    if project_root:
        return project_root.resolve(), [], [], [], []
    return (
        None, list(args.building or []), list(args.water or []),
        list(args.electrical or []), list(args.hvac or []),
    )


def run_pipeline(args: argparse.Namespace) -> dict[str, object]:
    # Programmatic callers may execute several projects in one interpreter.
    # Never carry a parsed document from a previous pipeline invocation.
    clear_document_cache()
    project_root, building, water, electrical, hvac = _resolve_inputs(args)
    force_analysis_cache = bool(getattr(args, "force_analysis_cache", False))
    write_review_dxf = (
        bool(getattr(args, "write_review_dxf", False))
        and not bool(getattr(args, "skip_obstacle_annotation", False))
    )
    # A fused drawing is only needed for CAD display.  JSON-based migration,
    # route planning and acceptance reporting use the building base plus
    # migrated point records.  When a final route DXF is requested, create the
    # fused base automatically so that the published CAD contains the devices.
    write_fused_dxf = (
        bool(getattr(args, "write_fused_dxf", False))
        or not bool(getattr(args, "no_path_dxf", False))
    )

    if args.scan_only:
        if not project_root:
            raise RuntimeError("--scan-only 需要 --project-root 或 --interactive")
        rows = stage_01_inputs.discover_project(project_root)
        for row in rows:
            status = "使用" if row["selected"] else "排除"
            print(f"[{status}] {row['discipline_name']}/{row['role']} {row['relative_path']} :: {row['evidence']}")
        return {"scan_only": True, "project_root": str(project_root), "candidates": rows}

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    project_name = project_root.name if project_root else (building[0].stem if building else "continued_run")
    run_dir = (args.output or (PROJECT_DIR / "runs" / f"{_safe_run_name(project_name)}_{stamp}")).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    cache_root = PROJECT_DIR / "cache" / "converted"
    analysis_cache_root = PROJECT_DIR / "cache" / "analysis_v2"

    if args.start_stage <= 1:
        print("[1/7] 项目扫描、输入分类与隔离转换")
        stage1 = stage_01_inputs.run_stage(
            building, water, electrical, run_dir / "01_inputs", cache_root, project_root=project_root,
            hvac=hvac,
        )
        print(f"      已选: {stage1['counts']['selected_total']}, 排除: {stage1['counts']['excluded']}, 转换: {stage1['counts']['converted']}")
    else:
        stage1 = read_json(run_dir / "01_inputs" / "prepared_drawings.json")

    if args.start_stage <= 2:
        print("[2/7] 自适应图框、楼栋与楼层范围识别")
        stage2 = stage_02_sheets.run_stage(
            stage1,
            run_dir / "02_sheets",
            cache_root=None if force_analysis_cache else analysis_cache_root,
        )
        print(f"      分图: {stage2['sheet_count']} {stage2['counts']}")
        sheet_cache = stage2.get("drawing_parse_cache") or {}
        print(
            f"      分图缓存: 命中 {sheet_cache.get('hits', 0)}, "
            f"首算 {sheet_cache.get('misses', 0)}"
        )
    else:
        stage2 = read_json(run_dir / "02_sheets" / "stage_summary.json")

    sheets_path = run_dir / "02_sheets" / "sheets.json"
    obstacle_summary_path = run_dir / "02b_building_obstacles" / "stage_summary.json"
    if args.start_stage <= 2 or not obstacle_summary_path.is_file():
        print("[2B/7] 仅建筑底图的障碍物识别与标注")
        building_item = next(
            item for item in stage1["drawings"] if item["discipline"] == "building"
        )
        building_dxf = Path(str(building_item["dxf"])).resolve()
        building_sheet_rows = [
            row for row in read_json(sheets_path)
            if row.get("discipline") == "building"
        ]
        route_core = (
            PROJECT_DIR / "multi_drawing_pipeline" / "stages" / "fire_route_core"
        )
        obstacle_rule_files = [
            Path(stage_02b_building_obstacles.__file__),
            route_core / "stage_02_cad_inventory.py",
            route_core / "stage_03_floor_preprocess.py",
            route_core / "stage_05_obstacles.py",
            route_core / "stage_05A_obstacles.py",
            route_core / "outer_wall_topology_repair.py",
        ]
        obstacle_cache_key = content_cache_key(
            "multi_drawing_building_obstacles_v3",
            input_files=[building_dxf],
            rule_files=obstacle_rule_files,
            options={
                "building_sheets": building_sheet_rows,
                "deepseek_base_url": os.getenv("DEEPSEEK_BASE_URL", ""),
                "deepseek_model": os.getenv("DEEPSEEK_MODEL", ""),
            },
        )
        cached_obstacle_dir = (
            analysis_cache_root / "building" / obstacle_cache_key
            / "02b_building_obstacles"
        )
        cached_obstacle_summary = cached_obstacle_dir / "stage_summary.json"
        obstacle_cache_ready = (
            cached_obstacle_summary.is_file()
            and (cached_obstacle_dir / "building_obstacles.geojson").is_file()
            and (
                cached_obstacle_dir / "local_route_core" / "obstacles"
                / "floor_obstacle_recognition_result.json"
            ).is_file()
        )
        use_persistent_obstacle_cache = not force_analysis_cache and not write_review_dxf
        if use_persistent_obstacle_cache and obstacle_cache_ready:
            cached_stage2b = read_json(cached_obstacle_summary)
            obstacle_cache_hit = True
        else:
            obstacle_output = (
                cached_obstacle_dir
                if use_persistent_obstacle_cache
                else run_dir / "02b_building_obstacles"
            )
            cached_stage2b = stage_02b_building_obstacles.run_stage(
                stage1, sheets_path, obstacle_output,
                write_annotated_dxf=write_review_dxf,
            )
            obstacle_cache_hit = False
        if use_persistent_obstacle_cache:
            stage2b = _materialize_building_obstacle_reference(
                cached_stage2b,
                run_dir / "02b_building_obstacles",
                cache_key=obstacle_cache_key,
                cache_hit=obstacle_cache_hit,
            )
        else:
            stage2b = cached_stage2b
            stage2b["persistent_cache"] = {
                "key": obstacle_cache_key,
                "hit": False,
                "disabled": True,
            }
            write_json(obstacle_summary_path, stage2b)
        print(
            f"      障碍物面: {stage2b['obstacle_polygon_count']}, "
            f"来源图元: {stage2b['source_entity_count']}, "
            f"明确门复核: {stage2b['door_opening_mask_count']}, "
            f"不可通行洞口: {stage2b['non_passable_opening_count']} {stage2b['counts']}"
        )
        obstacle_cache = stage2b.get("persistent_cache") or {}
        print(f"      建筑障碍跨运行缓存: {'命中' if obstacle_cache.get('hit') else '首算'}")
    else:
        stage2b = read_json(obstacle_summary_path)

    building_object_summary_path = run_dir / "02c_building_objects" / "stage_summary.json"
    if args.start_stage <= 3 or not building_object_summary_path.is_file():
        print("[2C/7] 建筑专业专用词库巡检对象识别")
        building_dxf = Path(str(stage2b["source_building"])).resolve()
        building_object_rule_files = [
            Path(stage_02c_building_objects.__file__),
            Path(stage_02c_building_objects.SCOPE_CONFIG),
            Path(stage_02c_building_objects.BASE_ALIAS_LIBRARY),
            Path(stage_02c_building_objects.BASE_KEYWORD_LIBRARY),
            (
                PROJECT_DIR / "multi_drawing_pipeline" / "stages"
                / "fire_route_core" / "stage_04_inspection_objects.py"
            ),
        ]
        object_cache_key = content_cache_key(
            "multi_drawing_building_objects_v3",
            input_files=[building_dxf, Path(str(stage2b["effective_planning_sheets"]))],
            rule_files=building_object_rule_files,
            options={
                "no_llm": bool(args.no_llm),
                "deepseek_base_url": os.getenv("DEEPSEEK_BASE_URL", ""),
                "deepseek_model": os.getenv("DEEPSEEK_MODEL", ""),
            },
        )
        cached_object_dir = (
            analysis_cache_root / "building" / object_cache_key
            / "02c_building_objects"
        )
        cached_object_summary = cached_object_dir / "stage_summary.json"
        object_cache_ready = (
            cached_object_summary.is_file()
            and (cached_object_dir / "building_objects.json").is_file()
        )
        use_persistent_object_cache = not force_analysis_cache
        if use_persistent_object_cache and object_cache_ready:
            cached_stage2c = read_json(cached_object_summary)
            object_cache_hit = True
        else:
            object_output = (
                cached_object_dir
                if use_persistent_object_cache
                else run_dir / "02c_building_objects"
            )
            cached_stage2c = stage_02c_building_objects.run_stage(
                stage1,
                stage2b,
                object_output,
                no_llm=bool(args.no_llm),
            )
            object_cache_hit = False
        if use_persistent_object_cache:
            stage2c = _materialize_building_object_reference(
                cached_stage2c,
                run_dir / "02c_building_objects",
                cache_key=object_cache_key,
                cache_hit=object_cache_hit,
            )
        else:
            stage2c = cached_stage2c
            stage2c["persistent_cache"] = {
                "key": object_cache_key,
                "hit": False,
                "disabled": True,
            }
            write_json(building_object_summary_path, stage2c)
        print(
            f"      建筑对象: {stage2c['building_object_count']}, "
            f"跨专业拒绝: {stage2c['discipline_scope_rejected_count']}"
        )
        object_cache = stage2c.get("persistent_cache") or {}
        print(f"      建筑对象跨运行缓存: {'命中' if object_cache.get('hit') else '首算'}")
    else:
        stage2c = read_json(building_object_summary_path)

    if args.start_stage <= 3:
        print("[3/7] 通用水电暖离散对象识别及原图标注")
        stage3 = stage_03_recognize.run_stage(
            stage1,
            sheets_path,
            run_dir / "03_recognition",
            write_annotated_dxf=write_review_dxf,
            cache_root=None if force_analysis_cache else analysis_cache_root,
        )
        print(
            f"      水: {stage3['recognized_water']}, 电: {stage3['recognized_electrical']}, "
            f"暖通: {stage3['recognized_hvac']}, 识别拒绝: "
            f"{stage3.get('recognition_rejected_count', stage3.get('review_candidate_count', 0))}"
        )
        recognition_cache = stage3.get("drawing_parse_cache") or {}
        print(
            f"      专业识别缓存: 命中 {recognition_cache.get('hits', 0)}, "
            f"首算 {recognition_cache.get('misses', 0)}"
        )
    else:
        stage3 = read_json(run_dir / "03_recognition" / "stage_summary.json")

    if args.start_stage <= 4:
        print("[4/7] 轴网优先的比例/旋转/平移配准")
        stage4 = stage_04_register.run_stage(
            stage1, sheets_path, run_dir / "04_registration",
            registration_policy=args.registration_policy,
            manual_anchors_path=args.manual_anchors,
            cache_root=None if force_analysis_cache else analysis_cache_root,
        )
        print(
            f"      严格通过: {stage4['strict_accepted']}, 近似通过: {stage4['approximate_accepted']}, "
            f"人工锚点: {stage4['manual_accepted']}, "
            f"拒绝: {stage4['rejected']}, 未匹配: {stage4['unmatched']}"
        )
        feature_cache = stage4.get("feature_cache") or {}
        print(
            f"      轴网特征缓存: 命中 {feature_cache.get('hits', 0)}, "
            f"首算 {feature_cache.get('misses', 0)}"
        )
    else:
        stage4 = read_json(run_dir / "04_registration" / "stage_summary.json")

    if args.start_stage <= 5:
        print("[5/7] 实际源图元迁移、原图层保留与追溯标注")
        stage5 = stage_05_migrate.run_stage(
            stage1, sheets_path,
            run_dir / "03_recognition" / "recognized_objects.json",
            run_dir / "04_registration" / "registrations.json",
            run_dir / "05_migration",
            write_fused_dxf=write_fused_dxf,
            cache_root=None if force_analysis_cache else analysis_cache_root,
        )
        print(
            f"      成功迁移: {stage5['migrated']}, 拒绝: {stage5['rejected']}, "
            f"迁移前排除连续管线: {stage5.get('continuous_infrastructure_excluded', 0)}"
        )
        room_cache = stage5.get("room_topology_cache") or {}
        print(
            f"      房间拓扑缓存: 命中 {room_cache.get('hits', 0)}, "
            f"首算 {room_cache.get('misses', 0)}"
        )
    else:
        stage5 = read_json(run_dir / "05_migration" / "stage_summary.json")

    route_base_dxf = Path(
        str(stage5.get("output_dxf") or stage5.get("building_base_dxf") or "")
    )
    if not route_base_dxf.is_file():
        building_item = next(
            item for item in stage1["drawings"] if item["discipline"] == "building"
        )
        route_base_dxf = Path(str(building_item["dxf"])).resolve()

    simplified_summary_path = run_dir / "05b_route_targets" / "stage_summary.json"
    existing_stage5b = (
        read_json(simplified_summary_path) if simplified_summary_path.is_file() else {}
    )
    if (
        args.start_stage <= 5
        or args.rebuild_route_targets
        or not simplified_summary_path.is_file()
        or "building_object_count" not in existing_stage5b
    ):
        print("[5B/7] 排除连续管线并生成离散巡检路径目标")
        stage5b = stage_05b_route_targets.run_stage(
            stage1,
            sheets_path,
            run_dir / "05_migration" / "migration_audit.json",
            route_base_dxf,
            run_dir / "02b_building_obstacles" / "building_obstacles.geojson",
            run_dir / "05b_route_targets",
            building_objects_path=Path(stage2c["building_objects_json"]),
        )
        print(
            f"      离散对象: {stage5b['retained_discrete_object_count']}, "
            f"排除连续管线: {stage5b['continuous_infrastructure_excluded']}, "
            f"路径目标: {stage5b['route_target_count']}"
        )
    else:
        stage5b = existing_stage5b

    print("[6/7] 数量、配准与迁移审计报告")
    stage6 = stage_06_report.run_stage(
        run_dir / "03_recognition" / "recognized_objects.json",
        run_dir / "04_registration" / "registrations.json",
        run_dir / "05_migration" / "migration_audit.json",
        stage3.get("source_annotation_files", []),
        str(stage5.get("output_dxf") or ""),
        run_dir / "06_report",
        obstacle_summary=stage2b, simplified_summary=stage5b,
        building_object_summary=stage2c,
    )

    print("[6V/7] 生成带版本的SBM、全专业Object Set和融合环境快照")
    versions = stage_06_versioned_environment.run_stage(
        prepared_payload=stage1,
        obstacle_summary=stage2b,
        route_targets_path=Path(stage5b["route_targets_json"]),
        registrations_path=run_dir / "04_registration" / "registrations.json",
        scope_policy_path=Path(stage2c["scope_policy"]),
        stage_dir=run_dir / "06_versioned_environment",
        artifact_root=Path(args.artifact_root).resolve(),
        recognition_rule_paths=[
            Path(stage2c["alias_library"]),
            Path(stage2c["keyword_pattern_library"]),
            Path(__file__).resolve().parent / "configs" / "inspection_object_rules.json",
        ],
    )
    print(f"      SBM: {versions['sbm']['version']}")
    print(f"      Object Set: {versions['object_set']['version']}")
    print(f"      Environment: {versions['environment']['version']}")
    stage7 = None
    business_images = None
    if not args.skip_route_planning:
        print("[7/7] 分楼层巡检路径生成、路线DXF与验收报告")
        route_targets_path = Path(stage5b["route_targets_json"]).resolve()
        building_sheets_path = Path(
            str(
                stage2b.get("effective_planning_sheets")
                or stage2b["reference_preprocess_sheets"]
            )
        ).resolve()
        building_obstacles_path = (
            run_dir / "02b_building_obstacles" / "building_obstacles.geojson"
        ).resolve()
        route_rule_root = (
            PROJECT_DIR / "multi_drawing_pipeline" / "stages" / "fire_route_core"
        )
        route_cache_key = json_cache_key(
            "multi_drawing_full_route_v2",
            {
                "route_base_dxf_sha256": file_sha256(route_base_dxf),
                "route_targets_sha256": file_sha256(route_targets_path),
                "building_sheets_sha256": file_sha256(building_sheets_path),
                "building_obstacles_sha256": file_sha256(building_obstacles_path),
                "dataset_manifest_sha256": file_sha256(args.rgcn_dataset_manifest),
                "rgcn_checkpoint_sha256": file_sha256(args.rgcn_checkpoint),
                "route_head_checkpoint_sha256": file_sha256(args.route_head_checkpoint),
                "transition_top_k": int(args.path_transition_top_k),
                "device_name": str(args.path_device),
                "dmax_ratio": float(args.path_dmax_ratio),
                "area_graph_pixel_size": float(args.area_graph_pixel_size),
                "write_dxf": not bool(args.no_path_dxf),
                "target_policy": "all_discrete_inspection_objects",
            },
            rule_files=[
                Path(stage_07_route_planning.__file__),
                *sorted(route_rule_root.glob("*.py")),
            ],
        )
        cached_route_dir = analysis_cache_root / "full_route" / route_cache_key / "07_route_planning"
        cached_route_summary = cached_route_dir / "stage_summary.json"
        route_cache_ready = cached_route_summary.is_file()
        use_route_cache = not force_analysis_cache
        if use_route_cache and route_cache_ready:
            stage7 = read_json(cached_route_summary)
            route_cache_hit = True
            print("      命中全量路线跨运行缓存，跳过重复建图与规划")
        else:
            stage7 = stage_07_route_planning.run_stage(
                run_dir=run_dir,
                fused_dxf_path=route_base_dxf,
                route_targets_path=route_targets_path,
                building_sheets_path=building_sheets_path,
                building_obstacles_path=building_obstacles_path,
                stage_dir=(cached_route_dir if use_route_cache else run_dir / "07_route_planning"),
                dataset_manifest_path=args.rgcn_dataset_manifest,
                rgcn_checkpoint_path=args.rgcn_checkpoint,
                route_head_checkpoint_path=args.route_head_checkpoint,
                transition_top_k=args.path_transition_top_k,
                device_name=args.path_device,
                dmax_ratio=args.path_dmax_ratio,
                area_graph_pixel_size=args.area_graph_pixel_size,
                write_dxf=not args.no_path_dxf,
            )
            route_cache_hit = False
        stage7 = dict(stage7)
        stage7["persistent_cache"] = {
            "key": route_cache_key,
            "hit": route_cache_hit,
            "target_policy": "all_discrete_inspection_objects",
            "cache_scope": "identical_building_objects_and_route_configuration",
        }
        current_route_stage = run_dir / "07_route_planning"
        current_route_stage.mkdir(parents=True, exist_ok=True)
        write_json(current_route_stage / "stage_summary.json", stage7)
        print(
            f"      导航目标: {stage7['navigation_target_count']}, "
            f"楼层: {len(stage7['selected_floor_ids'])}, "
            f"导航范围外排除: {stage7['navigation_skipped_target_count']}"
        )
        route_version = stage_06_versioned_environment.finalize_route_version(
            version_summary=versions,
            route_summary=stage7,
            stage_dir=run_dir / "06_versioned_environment",
            artifact_root=Path(args.artifact_root).resolve(),
            dataset_manifest_path=Path(args.rgcn_dataset_manifest),
            rgcn_checkpoint_path=Path(args.rgcn_checkpoint),
            route_head_checkpoint_path=Path(args.route_head_checkpoint),
        )
        print(f"      Route: {route_version['version']}")

        print("[业务图片] 生成障碍物图、路线点位图和无路线视觉输入图")
        from fire_inspection_system.business_image_outputs import (
            generate_business_image_outputs,
        )

        business_images = generate_business_image_outputs(
            Path(stage7["route_runtime_dir"]),
            output_run_dir=run_dir,
        )
        print(f"      图片: {business_images['image_count']}")
        print(f"      manifest: {business_images['manifest']}")

    manifest = {
        "run_dir": str(run_dir),
        "project_root": str(project_root) if project_root else "",
        "scope": {
            "building": True,
            "water": bool(stage1["counts"]["water"]),
            "electrical": bool(stage1["counts"]["electrical"]),
            "hvac": bool(stage1["counts"]["hvac"]),
            "continuous_infrastructure": False,
            "route_target_preparation": True,
            "route_planning": stage7 is not None,
            "acceptance_reporting": stage7 is not None,
        },
        "stage_01": stage1, "stage_02": stage2, "stage_02b": stage2b,
        "stage_02c": stage2c, "stage_03": stage3,
        "stage_04": stage4, "stage_05": stage5, "stage_05b": stage5b, "stage_06": stage6,
        "stage_07": stage7,
        "business_image_outputs": business_images,
        "versions": versions,
    }
    write_json(run_dir / "run_manifest.json", manifest)
    print(f"[完成] {run_dir}")
    if stage5.get("output_dxf"):
        print(f"[迁移融合DXF] {stage5['output_dxf']}")
    else:
        print(f"[迁移点位JSON] {stage5['migration_audit_json']}")
    print(f"[迁移审计报告] {stage6['report']}")
    if stage7:
        if stage7["route_dxf"]:
            print(f"[最终路线DXF] {stage7['route_dxf']}")
        print(f"[巡检验收总报告] {stage7['acceptance_report']}")
        if business_images:
            print(f"[业务图片] {business_images['output_dir']}")
            print(f"[无路线视觉输入图] {business_images['vision_input_output_dir']}")
    print(f"[版本环境快照] {versions['environment']['manifest']}")
    clear_document_cache()
    return manifest


def main() -> int:
    run_pipeline(parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
