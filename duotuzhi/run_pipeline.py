from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
WORKSPACE_ROOT = PROJECT_DIR.parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

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
    parser.add_argument("--water", type=Path, action="append", help="水专业 DWG/DXF；可重复任意次数")
    parser.add_argument("--electrical", type=Path, action="append", help="电专业 DWG/DXF；可重复任意次数")
    parser.add_argument("--hvac", type=Path, action="append", help="暖通专业 DWG/DXF；可重复任意次数")
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
        "--skip-obstacle-annotation",
        action="store_true",
        help="跳过建筑障碍物逐对象标注DXF写出（大图可超过100MB；仅省略人审展示文件，障碍数据不受影响）",
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
    project_root, building, water, electrical, hvac = _resolve_inputs(args)

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
        stage2 = stage_02_sheets.run_stage(stage1, run_dir / "02_sheets")
        print(f"      分图: {stage2['sheet_count']} {stage2['counts']}")
    else:
        stage2 = read_json(run_dir / "02_sheets" / "stage_summary.json")

    sheets_path = run_dir / "02_sheets" / "sheets.json"
    obstacle_summary_path = run_dir / "02b_building_obstacles" / "stage_summary.json"
    if args.start_stage <= 2 or not obstacle_summary_path.is_file():
        print("[2B/7] 仅建筑底图的障碍物识别与标注")
        stage2b = stage_02b_building_obstacles.run_stage(
            stage1, sheets_path, run_dir / "02b_building_obstacles",
            write_annotated_dxf=not bool(getattr(args, "skip_obstacle_annotation", False)),
        )
        print(
            f"      障碍物面: {stage2b['obstacle_polygon_count']}, "
            f"来源图元: {stage2b['source_entity_count']}, "
            f"明确门复核: {stage2b['door_opening_mask_count']}, "
            f"不可通行洞口: {stage2b['non_passable_opening_count']} {stage2b['counts']}"
        )
    else:
        stage2b = read_json(obstacle_summary_path)

    building_object_summary_path = run_dir / "02c_building_objects" / "stage_summary.json"
    if args.start_stage <= 3 or not building_object_summary_path.is_file():
        print("[2C/7] 建筑专业专用词库巡检对象识别")
        stage2c = stage_02c_building_objects.run_stage(
            stage1,
            stage2b,
            run_dir / "02c_building_objects",
            no_llm=bool(args.no_llm),
        )
        print(
            f"      建筑对象: {stage2c['building_object_count']}, "
            f"跨专业拒绝: {stage2c['discipline_scope_rejected_count']}"
        )
    else:
        stage2c = read_json(building_object_summary_path)

    if args.start_stage <= 3:
        print("[3/7] 通用水电暖离散对象识别及原图标注")
        stage3 = stage_03_recognize.run_stage(stage1, sheets_path, run_dir / "03_recognition")
        print(
            f"      水: {stage3['recognized_water']}, 电: {stage3['recognized_electrical']}, "
            f"暖通: {stage3['recognized_hvac']}, 识别拒绝: "
            f"{stage3.get('recognition_rejected_count', stage3.get('review_candidate_count', 0))}"
        )
    else:
        stage3 = read_json(run_dir / "03_recognition" / "stage_summary.json")

    if args.start_stage <= 4:
        print("[4/7] 轴网优先的比例/旋转/平移配准")
        stage4 = stage_04_register.run_stage(
            stage1, sheets_path, run_dir / "04_registration",
            registration_policy=args.registration_policy,
            manual_anchors_path=args.manual_anchors,
        )
        print(
            f"      严格通过: {stage4['strict_accepted']}, 近似通过: {stage4['approximate_accepted']}, "
            f"人工锚点: {stage4['manual_accepted']}, "
            f"拒绝: {stage4['rejected']}, 未匹配: {stage4['unmatched']}"
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
        )
        print(
            f"      成功迁移: {stage5['migrated']}, 拒绝: {stage5['rejected']}, "
            f"迁移前排除连续管线: {stage5.get('continuous_infrastructure_excluded', 0)}"
        )
    else:
        stage5 = read_json(run_dir / "05_migration" / "stage_summary.json")

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
            Path(stage5["output_dxf"]),
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
        stage3["source_annotation_files"], stage5["output_dxf"], run_dir / "06_report",
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
        stage7 = stage_07_route_planning.run_stage(
            run_dir=run_dir,
            fused_dxf_path=Path(stage5["output_dxf"]),
            route_targets_path=Path(stage5b["route_targets_json"]),
            building_sheets_path=Path(
                stage2b.get("effective_planning_sheets")
                or stage2b["reference_preprocess_sheets"]
            ),
            building_obstacles_path=run_dir / "02b_building_obstacles" / "building_obstacles.geojson",
            stage_dir=run_dir / "07_route_planning",
            dataset_manifest_path=args.rgcn_dataset_manifest,
            rgcn_checkpoint_path=args.rgcn_checkpoint,
            route_head_checkpoint_path=args.route_head_checkpoint,
            transition_top_k=args.path_transition_top_k,
            device_name=args.path_device,
            dmax_ratio=args.path_dmax_ratio,
            area_graph_pixel_size=args.area_graph_pixel_size,
            write_dxf=not args.no_path_dxf,
        )
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
    print(f"[迁移融合DXF] {stage5['output_dxf']}")
    print(f"[迁移审计报告] {stage6['report']}")
    if stage7:
        if stage7["route_dxf"]:
            print(f"[最终路线DXF] {stage7['route_dxf']}")
        print(f"[巡检验收总报告] {stage7['acceptance_report']}")
        if business_images:
            print(f"[业务图片] {business_images['output_dir']}")
            print(f"[无路线视觉输入图] {business_images['vision_input_output_dir']}")
    print(f"[版本环境快照] {versions['environment']['manifest']}")
    return manifest


def main() -> int:
    run_pipeline(parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
