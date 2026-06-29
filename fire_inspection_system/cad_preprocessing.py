from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from detect_dxf_sheets_floors import (  # type: ignore
    detect_sheets_and_floors,
    unique_output_path,
    write_csv,
    write_overlay_dxf,
)


@dataclass(frozen=True)
class CadPreprocessResult:
    sheets_json: Path
    sheets_csv: Path
    overlay_dxf: Path | None
    sheet_count: int
    floor_count: int
    usable_region_count: int
    floors: list[dict[str, Any]]


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def preprocess_cad_drawing(
    input_dxf: Path | str,
    inventory_dir: Path | str,
    output_dir: Path | str,
    *,
    grid_size: int = 180,
    write_floor_overlay: bool = True,
    overlay_all_sheets: bool = False,
) -> CadPreprocessResult:
    """Detect drawing sheets, floor semantics, and usable floor regions.

    This module is responsible for converting modelspace multi-sheet drawings
    into floor-level regions. The current route fuses explicit frame detection,
    plan-title seed growing, density-cluster fallback, non-plan exclusion, and
    title-based floor parsing.
    """
    input_path = Path(input_dxf).expanduser().resolve()
    inventory_path = Path(inventory_dir).resolve()
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    result = detect_sheets_and_floors(inventory_path, grid_size=grid_size)
    result["input_dxf"] = str(input_path)
    result["output_dir"] = str(out_dir)

    sheets_json = out_dir / "drawing_sheets_floors.json"
    sheets_csv = out_dir / "drawing_sheets_floors.csv"
    write_json(sheets_json, result)
    write_csv(sheets_csv, result["sheets"])

    overlay_path: Path | None = None
    if write_floor_overlay:
        overlay_path = unique_output_path(out_dir / f"{input_path.stem}_sheets_floors_marked.dxf")
        write_overlay_dxf(
            input_path,
            overlay_path,
            result["sheets"],
            include_non_path=overlay_all_sheets,
        )

    usable_floors = [sheet for sheet in result["sheets"] if sheet.get("path_planning_usable")]
    floor_ids = {str(sheet.get("floor_id") or "") for sheet in usable_floors if sheet.get("floor_id")}
    return CadPreprocessResult(
        sheets_json=sheets_json,
        sheets_csv=sheets_csv,
        overlay_dxf=overlay_path,
        sheet_count=int(result.get("sheet_count") or len(result["sheets"])),
        floor_count=len(floor_ids),
        usable_region_count=int(result.get("path_planning_usable_count") or len(usable_floors)),
        floors=usable_floors,
    )

