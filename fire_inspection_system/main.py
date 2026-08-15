"""消防巡检系统主入口：只负责参数解析、十一阶段编排和结果提示。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = MODULE_DIR.parent
for value in (PROJECT_ROOT, MODULE_DIR):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from fire_inspection_system.stages import (
    stage_01_cad_input,
    stage_02_cad_inventory,
    stage_03_floor_preprocess,
    stage_04_inspection_objects,
    stage_05_obstacles,
    stage_05A_obstacles,
    stage_06_navigation_graph,
    stage_07_connector_metric_closure,
    stage_08_semantic_rgcn,
    stage_09_dual_graph_planning,
    stage_10_route_outputs,
    stage_11_acceptance_reports,
)

DEFAULT_DATASET_MANIFEST = stage_08_semantic_rgcn.DEFAULT_DATASET_MANIFEST
DEFAULT_RGCN_CHECKPOINT = stage_08_semantic_rgcn.DEFAULT_RGCN_CHECKPOINT
DEFAULT_ROUTE_HEAD_CHECKPOINT = stage_08_semantic_rgcn.DEFAULT_ROUTE_HEAD_CHECKPOINT


DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "fire_inspection_pipeline"
DEFAULT_LLM_CONFIG = MODULE_DIR / "configs" / "llm_api.json"


def _apply_llm_api_config(args: argparse.Namespace) -> dict[str, str]:
    """Apply CLI/config/environment LLM settings for stages 4 and 5."""
    config_path = Path(args.llm_config).expanduser().resolve()
    payload: dict[str, Any] = {}
    if config_path.is_file():
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"LLM 配置必须是 JSON 对象: {config_path}")
        payload = loaded

    api_key = (
        args.llm_api_key.strip()
        or str(payload.get("api_key") or "").strip()
        or os.getenv("DEEPSEEK_API_KEY", "").strip()
        or os.getenv("OPENAI_API_KEY", "").strip()
    )
    base_url = (
        args.llm_base_url.strip()
        or str(payload.get("base_url") or "").strip()
        or os.getenv("DEEPSEEK_BASE_URL", "").strip()
        or "https://api.deepseek.com"
    )
    model = (
        args.llm_model.strip()
        or str(payload.get("model") or "").strip()
        or os.getenv("DEEPSEEK_MODEL", "").strip()
        or "deepseek-v4-flash"
    )
    if api_key:
        os.environ["DEEPSEEK_API_KEY"] = api_key
    os.environ["DEEPSEEK_BASE_URL"] = base_url
    os.environ["DEEPSEEK_MODEL"] = model
    return {
        "config_path": str(config_path),
        "base_url": base_url,
        "model": model,
        "api_key_configured": str(bool(api_key)),
    }


def _strip_quotes(value: str) -> str:
    return value.strip().strip('"').strip("'")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="消防巡检 CAD 识别、物理导航、R-GCN 双图规划十一阶段主程序"
    )
    parser.add_argument(
        "-i",
        "--input",
        default="",
        help="输入 DWG 或 DXF 路径；留空时弹窗选择",
    )
    parser.add_argument("-o", "--output-dir", default="", help="本次运行结果目录")
    parser.add_argument(
        "--path-planning-only",
        action="store_true",
        help="复用 --output-dir 中的识别/导航结果，只执行阶段7至阶段11",
    )
    parser.add_argument(
        "--no-path-planning",
        action="store_true",
        help="只执行识别和基础物理导航，不生成最终巡检路线",
    )
    parser.add_argument("--force-inventory", action="store_true", help="忽略图元清单缓存")
    parser.add_argument("--max-depth", type=int, default=10, help="INSERT/BLOCK 递归深度")
    parser.add_argument(
        "--expand-all-inserts",
        action="store_true",
        help="展开块内全部虚拟图元",
    )
    parser.add_argument("--grid-size", type=int, default=180, help="图幅/楼层聚类栅格参数")
    parser.add_argument("--no-llm", action="store_true", help="关闭 LLM 兜底识别")
    parser.add_argument(
        "--llm-config",
        default=str(DEFAULT_LLM_CONFIG),
        help="LLM API 配置 JSON（api_key/base_url/model）",
    )
    parser.add_argument("--llm-api-key", default="", help="临时覆盖配置文件中的 API Key")
    parser.add_argument("--llm-base-url", default="", help="临时覆盖 LLM Base URL")
    parser.add_argument("--llm-model", default="", help="临时覆盖模型名称")
    parser.add_argument(
        "--no-floor-overlay",
        action="store_true",
        help="不生成楼层预处理标注 DXF",
    )
    parser.add_argument(
        "--no-review-dxf",
        action="store_true",
        help="不生成巡检对象审核 DXF",
    )
    parser.add_argument(
        "--no-obstacle-dxf",
        action="store_true",
        help="不生成障碍物审核 DXF",
    )
    parser.add_argument(
        "--no-building-vision",
        action="store_true",
        help="Disable conditional Stage 05A multi-building vision recognition.",
    )
    parser.add_argument(
        "--building-vision-required",
        action="store_true",
        help="Fail instead of falling back to floor-level planning when Stage 05A cannot run.",
    )
    parser.add_argument(
        "--ark-model",
        default=stage_05A_obstacles.DEFAULT_ARK_VISION_MODEL,
        help="Volcano Ark vision endpoint/model id used by conditional Stage 05A.",
    )
    parser.add_argument(
        "--ark-api-key-env",
        default="ARK_API_KEY",
        help="Environment variable containing the Volcano Ark API key.",
    )
    parser.add_argument(
        "--ark-vision-force",
        action="store_true",
        help="Ignore cached Stage 05A model responses.",
    )
    parser.add_argument(
        "--no-navigation-graph",
        action="store_true",
        help="跳过物理导航图构建",
    )
    parser.add_argument("--area-graph-pixel-size", type=float, default=0.0)
    parser.add_argument("--area-graph-max-raster-side", type=int, default=1600)
    parser.add_argument("--area-graph-max-raster-pixels", type=int, default=1_500_000)
    parser.add_argument("--area-graph-minimum-bottleneck-score", type=float, default=0.06)
    parser.add_argument("--area-graph-maximum-portals-per-floor", type=int, default=600)
    parser.add_argument(
        "--no-area-anchors",
        action="store_true",
        help="物理图不附加区域锚点",
    )
    parser.add_argument(
        "--no-physical-metric-closure",
        action="store_true",
        help="关闭最终规划时，跳过独立物理 Metric Closure 预计算",
    )
    parser.add_argument("--path-source-run-id", default="", help="R-GCN 运行来源标识")
    parser.add_argument(
        "--path-dmax-ratio",
        type=float,
        default=0.80,
        help="最大连续空走距离/楼层尺度",
    )
    parser.add_argument("--path-transition-top-k", type=int, default=20)
    parser.add_argument(
        "--path-device",
        default="auto",
        help="R-GCN 推理设备：auto/cpu/cuda",
    )
    parser.add_argument(
        "--no-path-dxf",
        action="store_true",
        help="不写最终路线标注 DXF",
    )
    parser.add_argument(
        "--force-navigation-refinement",
        action="store_true",
        help="强制重建 Connector Portal 修正物理图",
    )
    parser.add_argument("--semantic-constraints", default="", help="自定义巡检约束 JSON")
    parser.add_argument(
        "--rgcn-dataset-manifest",
        default=str(DEFAULT_DATASET_MANIFEST),
    )
    parser.add_argument("--rgcn-checkpoint", default=str(DEFAULT_RGCN_CHECKPOINT))
    parser.add_argument(
        "--route-head-checkpoint",
        default=str(DEFAULT_ROUTE_HEAD_CHECKPOINT),
    )
    return parser.parse_args()


def _resolve_resume_input(
    args: argparse.Namespace,
    run_dir: Path,
) -> tuple[Path, dict[str, Any]]:
    summary_path = run_dir / "pipeline_summary.json"
    if not summary_path.is_file():
        raise FileNotFoundError(f"续跑目录缺少 pipeline_summary.json: {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    raw_input = (
        _strip_quotes(args.input)
        if args.input
        else str(summary.get("input_dxf") or "")
    )
    if not raw_input:
        raise ValueError("续跑摘要没有 input_dxf，请显式传入 --input")
    return Path(raw_input).expanduser().resolve(), summary


def _print_final_outputs(
    summary_path: Path,
    path_result: dict[str, Any] | None,
) -> None:
    print(f"\n完成。主流程摘要: {summary_path}")
    if not path_result:
        return
    outputs = path_result.get("outputs") or {}
    print(f"最终路线标注 DXF: {outputs.get('annotated_route_dxf', '')}")
    print(
        "完整访问顺序 CSV: "
        f"{outputs.get('inspection_target_visit_order_csv', '')}"
    )
    print(
        "首次访问顺序 CSV: "
        f"{outputs.get('inspection_target_first_visit_order_csv', '')}"
    )
    print(f"路径规划摘要: {path_result.get('summary_path', '')}")
    print(
        "分楼层验收报告: "
        f"{outputs.get('acceptance_report_index', '')}"
    )


def _run_conditional_building_stage(
    args: argparse.Namespace,
    *,
    sheets_json: Path,
    obstacle_geojsons: list[Path],
    run_dir: Path,
) -> tuple[Path, dict[str, Any]]:
    """Run Stage 05A only for floor plans whose title indicates 2+ buildings."""

    multi_floor_ids = stage_05A_obstacles.find_multi_building_floor_ids(sheets_json)
    summary: dict[str, Any] = {
        "enabled": not args.no_building_vision,
        "multi_building_floor_ids": multi_floor_ids,
        "multi_building_floor_count": len(multi_floor_ids),
        "planning_sheets_json": str(Path(sheets_json).resolve()),
        "fallback_policy": "floor_region_for_single_building_or_unvalidated_detection",
    }
    if not multi_floor_ids:
        summary["status"] = "skipped_no_multi_building_floor"
        return Path(sheets_json).resolve(), summary
    if args.no_building_vision:
        summary["status"] = "disabled_floor_level_fallback"
        return Path(sheets_json).resolve(), summary

    api_key_env = str(args.ark_api_key_env or "ARK_API_KEY").strip()
    api_key_source = (
        f"environment:{api_key_env}"
        if os.getenv(api_key_env, "").strip()
        else "stage_05A_source_default"
    )
    if not (
        os.getenv(api_key_env, "").strip()
        or stage_05A_obstacles.DEFAULT_ARK_API_KEY
    ):
        summary.update(
            {
                "status": "missing_api_key_floor_level_fallback",
                "api_key_env": api_key_env,
            }
        )
        if args.building_vision_required:
            raise RuntimeError(
                f"Multi-building floors require Stage 05A, but {api_key_env} is not set"
            )
        return Path(sheets_json).resolve(), summary

    try:
        render_result = stage_05A_obstacles.run_stage(
            sheets_json,
            obstacle_geojsons,
            run_dir,
            floor_ids=multi_floor_ids,
        )
        vision_result = stage_05A_obstacles.run_building_vision(
            render_result.manifest_json,
            sheets_json,
            run_dir,
            config=stage_05A_obstacles.ArkVisionConfig(
                model=str(args.ark_model),
                api_key_env=api_key_env,
                force=bool(args.ark_vision_force),
            ),
            validation_config=stage_05A_obstacles.BuildingVectorValidationConfig(
                enabled=True
            ),
            obstacle_geojsons=obstacle_geojsons,
            floor_ids=multi_floor_ids,
        )
    except Exception as exc:
        summary.update(
            {
                "status": "stage_05A_error_floor_level_fallback",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        if args.building_vision_required:
            raise
        return Path(sheets_json).resolve(), summary

    effective_sheets = vision_result.sheets_with_buildings_json.resolve()
    envelope_completion = stage_05A_obstacles.write_upper_floor_building_envelope_obstacles(
        effective_sheets,
        run_dir,
    )
    summary.update(
        {
            "status": "completed_with_per_floor_validation_fallback",
            "api_key_env": api_key_env,
            "api_key_source": api_key_source,
            "model": str(args.ark_model),
            "rendered_image_count": render_result.image_count,
            "vision_image_count": vision_result.image_count,
            "vision_success_count": vision_result.success_image_count,
            "vision_error_count": vision_result.error_count,
            "building_region_count": vision_result.building_region_count,
            "needs_review_count": vision_result.needs_review_count,
            "building_regions_json": str(vision_result.building_regions_json),
            "sheets_with_buildings_json": str(effective_sheets),
            "planning_sheets_json": str(effective_sheets),
            "building_envelope_completion": {
                "enabled": True,
                "minimum_floor": 3,
                "obstacle_count": int(envelope_completion["obstacle_count"]),
                "scope_count": int(envelope_completion["scope_count"]),
                "geojson_path": str(envelope_completion["geojson_path"]),
                "audit_path": str(envelope_completion["audit_path"]),
                "annotated_image_count": len(
                    envelope_completion.get("annotated_image_paths") or []
                ),
                "annotated_images": [
                    str(path)
                    for path in envelope_completion.get("annotated_image_paths") or []
                ],
                "overview_image": str(
                    envelope_completion.get("overview_image_path") or ""
                ),
                "review_index": str(
                    envelope_completion.get("review_index_path") or ""
                ),
            },
        }
    )
    return effective_sheets, summary


def _run_path_stages(
    args: argparse.Namespace,
    run_dir: Path,
    input_dxf: Path,
    pipeline_summary: dict[str, Any],
) -> tuple[dict[str, Any], Path]:
    print("\n[7/11] Connector Portal 修正与物理图准备")
    physical = stage_07_connector_metric_closure.run_stage(
        run_dir,
        path_planning_enabled=True,
        force_refinement=args.force_navigation_refinement,
    )
    pipeline_summary["physical_preparation"] = physical.to_summary()

    print("\n[8/11] A/N/C 上下文与冻结 R-GCN 推理")
    semantic = stage_08_semantic_rgcn.run_stage(
        run_dir,
        physical,
        dataset_manifest_path=args.rgcn_dataset_manifest,
        rgcn_checkpoint_path=args.rgcn_checkpoint,
        route_head_checkpoint_path=args.route_head_checkpoint,
        constraints_path=args.semantic_constraints or None,
        source_run_id=args.path_source_run_id,
        transition_top_k=args.path_transition_top_k,
        device_name=args.path_device,
    )
    print(f"  floors: {', '.join(semantic.selected_floor_ids)}")

    print("\n[9/11] 所有识别楼层的双图约束规划")
    planning = stage_09_dual_graph_planning.run_stage(
        run_dir,
        physical,
        semantic,
        dmax_ratio=args.path_dmax_ratio,
    )

    print("\n[10/11] 矢量路径展开、DXF 标注和访问顺序输出")
    path_result, summary_path = stage_10_route_outputs.run_stage(
        run_dir=run_dir,
        input_dxf=input_dxf,
        pipeline_summary=pipeline_summary,
        physical=physical,
        semantic=semantic,
        planning=planning,
        write_dxf=not args.no_path_dxf,
    )
    if path_result is None:
        raise RuntimeError("阶段10未生成路径规划结果")
    print("\n[11/11] 按楼层生成巡检验收报告")
    acceptance_result, summary_path = stage_11_acceptance_reports.run_stage(
        run_dir,
        pipeline_summary=pipeline_summary,
        path_result=path_result,
    )
    print(f"  report_index: {acceptance_result.index_markdown}")
    return path_result, summary_path


def _base_summary(
    cad: stage_01_cad_input.CadInputResult,
    vector_result: Any,
    preprocess_result: Any,
    recognition_result: Any,
    obstacle_result: Any,
    navigation_result: Any,
    physical_result: stage_07_connector_metric_closure.PhysicalPreparationResult,
    *,
    navigation_enabled: bool,
) -> dict[str, Any]:
    return {
        **cad.to_summary(),
        "vector_inventory": {
            "inventory_dir": str(vector_result.inventory_dir),
            "manifest": str(vector_result.manifest_path),
            "cache_hit": vector_result.cache_hit,
            "cache_version": vector_result.cache_version,
            "counts": vector_result.counts,
        },
        "cad_preprocess": {
            "sheets_json": str(preprocess_result.sheets_json),
            "sheets_csv": str(preprocess_result.sheets_csv),
            "overlay_dxf": (
                str(preprocess_result.overlay_dxf)
                if preprocess_result.overlay_dxf
                else ""
            ),
            "sheet_count": preprocess_result.sheet_count,
            "floor_count": preprocess_result.floor_count,
            "usable_region_count": preprocess_result.usable_region_count,
        },
        "inspection_recognition": {
            "result_json": str(recognition_result.result_json),
            "regions_manifest": str(recognition_result.regions_manifest),
            "marked_dxf": (
                str(recognition_result.marked_dxf)
                if recognition_result.marked_dxf
                else ""
            ),
            "marked_report_json": (
                str(recognition_result.marked_report_json)
                if recognition_result.marked_report_json
                else ""
            ),
            "region_count": recognition_result.region_count,
            "inspection_type_count": recognition_result.inspection_type_count,
            "inspection_instance_count": recognition_result.inspection_instance_count,
            "llm_candidate_count": recognition_result.llm_candidate_count,
            "llm_model": recognition_result.llm_model,
        },
        "obstacle_recognition": {
            "result_json": str(obstacle_result.result_json),
            "obstacle_csv": str(obstacle_result.obstacle_csv),
            "marked_dxf": (
                str(obstacle_result.marked_dxf) if obstacle_result.marked_dxf else ""
            ),
            "obstacle_count": obstacle_result.obstacle_count,
            "obstacle_type_count": obstacle_result.obstacle_type_count,
            "region_count": obstacle_result.region_count,
            "per_region_geojsons": [
                str(path) for path in obstacle_result.per_region_geojsons
            ],
            "union_geojsons": [str(path) for path in obstacle_result.union_geojsons],
        },
        "navigation_graph": {
            "enabled": navigation_enabled,
            **(navigation_result.to_dict() if navigation_result else {}),
        },
        "physical_preparation": physical_result.to_summary(),
        "physical_metric_closure": {
            "enabled": bool(physical_result.standalone_metric_closure),
            **(physical_result.standalone_metric_closure or {}),
        },
    }


def main() -> None:
    args = parse_args()
    _apply_llm_api_config(args)
    if args.path_planning_only:
        if not args.output_dir:
            raise ValueError("--path-planning-only 必须同时指定 --output-dir")
        run_dir = Path(args.output_dir).resolve()
        input_dxf, summary = _resolve_resume_input(args, run_dir)
        if not input_dxf.is_file():
            raise FileNotFoundError(input_dxf)
        path_result, summary_path = _run_path_stages(
            args,
            run_dir,
            input_dxf,
            summary,
        )
        _print_final_outputs(summary_path, path_result)
        return

    print("\n[1/11] CAD 输入准备（DWG 转 DXF，DXF 直接使用）")
    cad = stage_01_cad_input.run_stage(
        args.input,
        args.output_dir,
        default_output_root=DEFAULT_OUTPUT_ROOT,
    )
    print(f"  input_cad: {cad.input_cad}")
    print(f"  input_dxf: {cad.input_dxf}")
    print(f"  run_dir: {cad.run_dir}")

    print("\n[2/11] CAD 图元清单")
    vector_result = stage_02_cad_inventory.run_stage(
        cad.input_dxf,
        force=args.force_inventory,
        max_depth=args.max_depth,
        expand_all_inserts=args.expand_all_inserts,
    )
    print(f"  inventory: {vector_result.inventory_dir}")
    print(f"  cache_hit: {vector_result.cache_hit}")

    print("\n[3/11] 图幅与楼层预处理")
    preprocess_result = stage_03_floor_preprocess.run_stage(
        cad.input_dxf,
        vector_result.inventory_dir,
        cad.run_dir,
        grid_size=args.grid_size,
        write_floor_overlay=not args.no_floor_overlay,
    )
    print(f"  sheets_json: {preprocess_result.sheets_json}")

    print("\n[4/11] 巡检对象识别")
    recognition_result = stage_04_inspection_objects.run_stage(
        cad.input_dxf,
        vector_result.inventory_dir,
        preprocess_result.sheets_json,
        cad.run_dir,
        no_llm=args.no_llm,
        write_review_dxf=not args.no_review_dxf,
    )
    print(f"  result_json: {recognition_result.result_json}")

    print("\n[5/11] 障碍物识别")
    obstacle_result = stage_05_obstacles.run_stage(
        cad.input_dxf,
        vector_result.inventory_dir,
        preprocess_result.sheets_json,
        cad.run_dir,
        write_review_dxf=not args.no_obstacle_dxf,
    )
    print(f"  result_json: {obstacle_result.result_json}")

    print("\n[5A/11] Conditional multi-building recognition")
    planning_sheets_json, building_stage_summary = _run_conditional_building_stage(
        args,
        sheets_json=preprocess_result.sheets_json,
        obstacle_geojsons=list(obstacle_result.per_region_geojsons),
        run_dir=cad.run_dir,
    )
    print(f"  status: {building_stage_summary['status']}")
    print(
        "  multi_building_floors: "
        f"{building_stage_summary['multi_building_floor_ids']}"
    )
    print(f"  planning_sheets_json: {planning_sheets_json}")

    navigation_obstacle_geojsons = list(obstacle_result.union_geojsons)
    navigation_expected_obstacle_count = int(obstacle_result.obstacle_count)
    envelope_completion = building_stage_summary.get("building_envelope_completion") or {}
    envelope_geojson = str(envelope_completion.get("geojson_path") or "").strip()
    envelope_obstacle_count = int(envelope_completion.get("obstacle_count") or 0)
    if envelope_geojson and envelope_obstacle_count:
        navigation_obstacle_geojsons.append(Path(envelope_geojson))
        navigation_expected_obstacle_count += envelope_obstacle_count

    navigation_result = None
    if args.no_navigation_graph:
        print("\n[6/11] 已按参数跳过自由空间和物理导航图")
        if not args.no_path_planning:
            raise ValueError("双图路径规划需要物理导航图，不能同时使用 --no-navigation-graph")
    else:
        print("\n[6/11] 自由空间、Portal AreaGraph 和中轴导航图")
        navigation_result = stage_06_navigation_graph.run_stage(
            run_dir=cad.run_dir,
            input_dxf=cad.input_dxf,
            sheets_json=planning_sheets_json,
            obstacle_union_geojsons=navigation_obstacle_geojsons,
            expected_obstacle_count=navigation_expected_obstacle_count,
            expected_target_count=recognition_result.inspection_instance_count,
            area_graph_pixel_size=args.area_graph_pixel_size,
            area_graph_max_raster_side=args.area_graph_max_raster_side,
            area_graph_max_raster_pixels=args.area_graph_max_raster_pixels,
            area_graph_minimum_bottleneck_score=args.area_graph_minimum_bottleneck_score,
            area_graph_maximum_portals_per_floor=(
                args.area_graph_maximum_portals_per_floor
            ),
            include_area_anchors=not args.no_area_anchors,
        )
        print(
            "  rule_graph: "
            f"{navigation_result.area_graph_navigation['outputs']['graph_json']}"
        )

    if args.no_path_planning:
        print("\n[7/11] Connector/Metric Closure 阶段按无最终规划模式执行")
        rule_graph = (
            Path(navigation_result.area_graph_navigation["outputs"]["graph_json"])
            if navigation_result
            else None
        )
        physical = stage_07_connector_metric_closure.run_stage(
            cad.run_dir,
            path_planning_enabled=False,
            force_refinement=False,
            rule_navigation_graph=rule_graph,
            build_standalone_metric_closure=(
                navigation_result is not None
                and not args.no_physical_metric_closure
            ),
        )
        print("\n[8/11] 已按参数跳过 A/N/C 与 R-GCN")
        print("\n[9/11] 已按参数跳过双图约束规划")
        summary = _base_summary(
            cad,
            vector_result,
            preprocess_result,
            recognition_result,
            obstacle_result,
            navigation_result,
            physical,
            navigation_enabled=not args.no_navigation_graph,
        )
        summary["building_region_recognition"] = building_stage_summary
        print("\n[10/11] 写入主流程摘要")
        path_result, summary_path = stage_10_route_outputs.run_stage(
            run_dir=cad.run_dir,
            input_dxf=cad.input_dxf,
            pipeline_summary=summary,
            physical=None,
            semantic=None,
            planning=None,
            write_dxf=False,
        )
        print("\n[11/11] 按楼层生成巡检验收报告")
        acceptance_result, summary_path = stage_11_acceptance_reports.run_stage(
            cad.run_dir,
            pipeline_summary=summary,
            path_result=path_result,
        )
        print(f"  report_index: {acceptance_result.index_markdown}")
        _print_final_outputs(summary_path, path_result)
        return

    placeholder_physical = stage_07_connector_metric_closure.PhysicalPreparationResult(
        enabled=False
    )
    summary = _base_summary(
        cad,
        vector_result,
        preprocess_result,
        recognition_result,
        obstacle_result,
        navigation_result,
        placeholder_physical,
        navigation_enabled=True,
    )
    summary["building_region_recognition"] = building_stage_summary
    path_result, summary_path = _run_path_stages(
        args,
        cad.run_dir,
        cad.input_dxf,
        summary,
    )
    _print_final_outputs(summary_path, path_result)


if __name__ == "__main__":
    main()
