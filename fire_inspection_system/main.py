from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from cad_preprocessing import preprocess_cad_drawing
from cad_vector_extraction import extract_cad_vector_information
from inspection_object_recognition import recognize_inspection_objects
from obstacle_recognition import recognize_floor_obstacles

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "fire_inspection_pipeline"


def strip_path_quotes(value: str) -> str:
    return value.strip().strip('"').strip("'")


def choose_dxf_with_dialog() -> Path | None:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        selected = filedialog.askopenfilename(
            title="选择 DXF 文件",
            filetypes=[("DXF 文件", "*.dxf"), ("所有文件", "*.*")],
        )
        root.destroy()
        return Path(selected) if selected else None
    except Exception:
        return None


def prompt_input_dxf() -> Path:
    entered = strip_path_quotes(input("请输入 DXF 文件路径；直接回车可打开文件选择窗口："))
    if entered:
        return Path(entered).expanduser().resolve()
    selected = choose_dxf_with_dialog()
    if selected:
        return selected.expanduser().resolve()
    raise FileNotFoundError("未选择 DXF 文件。")


def default_run_dir(input_dxf: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    safe_stem = re.sub(r"[^0-9A-Za-z_\-.\u4e00-\u9fff]+", "_", input_dxf.stem).strip("_") or "drawing"
    return DEFAULT_OUTPUT_ROOT / f"{safe_stem}_{stamp}"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="消防巡检路径推荐前置识别主程序。")
    parser.add_argument("-i", "--input", default="", help="输入 DXF 文件路径；留空则运行后交互选择。")
    parser.add_argument("-o", "--output-dir", default="", help="本次运行结果目录。")
    parser.add_argument("--force-inventory", action="store_true", help="忽略 inventory 缓存，重新解析 DXF。")
    parser.add_argument("--max-depth", type=int, default=10, help="INSERT/BLOCK 递归扫描深度。")
    parser.add_argument("--expand-all-inserts", action="store_true", help="审计模式：展开块内全部虚拟图元。默认关闭。")
    parser.add_argument("--grid-size", type=int, default=180, help="楼层/图幅密度聚类兜底网格大小。")
    parser.add_argument("--no-llm", action="store_true", help="关闭 LLM 兜底，仅使用本地规则识别。")
    parser.add_argument("--no-floor-overlay", action="store_true", help="不生成图幅/楼层预处理标注 DXF。")
    parser.add_argument("--no-review-dxf", action="store_true", help="不生成巡检对象审查标注 DXF。")
    parser.add_argument("--no-obstacle-dxf", action="store_true", help="不生成障碍物审查标注 DXF。")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_dxf = Path(strip_path_quotes(args.input)).expanduser().resolve() if args.input else prompt_input_dxf()
    if not input_dxf.exists():
        raise FileNotFoundError(input_dxf)
    if input_dxf.suffix.lower() != ".dxf":
        raise ValueError(f"只支持 DXF 文件: {input_dxf}")

    run_dir = Path(args.output_dir).resolve() if args.output_dir else default_run_dir(input_dxf)
    run_dir.mkdir(parents=True, exist_ok=True)

    print("\n[1/5] CAD 图纸矢量信息提取")
    vector_result = extract_cad_vector_information(
        input_dxf,
        force=args.force_inventory,
        max_depth=args.max_depth,
        expand_all_inserts=args.expand_all_inserts,
    )
    print(f"  inventory: {vector_result.inventory_dir}")
    print(f"  cache_hit: {vector_result.cache_hit}")

    print("\n[2/5] CAD 图纸预处理：图幅、楼层、可用区域")
    preprocess_result = preprocess_cad_drawing(
        input_dxf,
        vector_result.inventory_dir,
        run_dir / "preprocess",
        grid_size=args.grid_size,
        write_floor_overlay=not args.no_floor_overlay,
    )
    print(f"  sheets_json: {preprocess_result.sheets_json}")
    print(f"  usable floors: {preprocess_result.usable_region_count}")

    print("\n[3/5] 楼层区域内巡检对象识别")
    recognition_result = recognize_inspection_objects(
        input_dxf,
        vector_result.inventory_dir,
        preprocess_result.sheets_json,
        run_dir / "inspection_objects",
        no_llm=args.no_llm,
        write_review_dxf=not args.no_review_dxf,
    )
    print(f"  result_json: {recognition_result.result_json}")
    if recognition_result.marked_dxf:
        print(f"  review_dxf: {recognition_result.marked_dxf}")

    print("\n[4/5] 楼层区域内障碍物识别")
    obstacle_result = recognize_floor_obstacles(
        input_dxf,
        vector_result.inventory_dir,
        preprocess_result.sheets_json,
        run_dir / "obstacles",
        write_review_dxf=not args.no_obstacle_dxf,
    )
    print(f"  result_json: {obstacle_result.result_json}")
    if obstacle_result.marked_dxf:
        print(f"  obstacle_dxf: {obstacle_result.marked_dxf}")

    print("\n[5/5] 写入本次运行摘要")
    summary = {
        "input_dxf": str(input_dxf),
        "run_dir": str(run_dir),
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
            "overlay_dxf": str(preprocess_result.overlay_dxf) if preprocess_result.overlay_dxf else "",
            "sheet_count": preprocess_result.sheet_count,
            "floor_count": preprocess_result.floor_count,
            "usable_region_count": preprocess_result.usable_region_count,
        },
        "inspection_recognition": {
            "result_json": str(recognition_result.result_json),
            "regions_manifest": str(recognition_result.regions_manifest),
            "marked_dxf": str(recognition_result.marked_dxf) if recognition_result.marked_dxf else "",
            "marked_report_json": str(recognition_result.marked_report_json) if recognition_result.marked_report_json else "",
            "region_count": recognition_result.region_count,
            "inspection_type_count": recognition_result.inspection_type_count,
            "inspection_instance_count": recognition_result.inspection_instance_count,
            "llm_candidate_count": recognition_result.llm_candidate_count,
            "llm_model": recognition_result.llm_model,
        },
        "obstacle_recognition": {
            "result_json": str(obstacle_result.result_json),
            "obstacle_csv": str(obstacle_result.obstacle_csv),
            "marked_dxf": str(obstacle_result.marked_dxf) if obstacle_result.marked_dxf else "",
            "obstacle_count": obstacle_result.obstacle_count,
            "obstacle_type_count": obstacle_result.obstacle_type_count,
            "region_count": obstacle_result.region_count,
            "per_region_geojsons": [str(path) for path in obstacle_result.per_region_geojsons],
            "union_geojsons": [str(path) for path in obstacle_result.union_geojsons],
        },
    }
    summary_path = run_dir / "pipeline_summary.json"
    write_json(summary_path, summary)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n完成。摘要文件: {summary_path}")


if __name__ == "__main__":
    main()
