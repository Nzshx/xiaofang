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
# Migrated implementation: fire_inspection_system/cad_local_dwgtodxf.py
# -----------------------------------------------------------------------------
def _build_s01_cad():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/cad_local_dwgtodxf.py'
    )
    __name__ = 'fire_inspection_system.cad_local_dwgtodxf'
    __package__ = 'fire_inspection_system'
    """
    功能：
    1. 弹窗选择一个 DWG 文件；
    2. 使用 ODA File Converter 转换为同目录、同名 DXF；
    3. 例如：
       cheku(all).dwg -> cheku(all).dxf

    不做 DXF 解析。
    不使用 ezdxf。
    """
    import sys
    import shutil
    import tempfile
    import subprocess
    from pathlib import Path
    import tkinter as tk
    from tkinter import filedialog, messagebox
    ODA_EXE_PATH = 'C:\\Program Files\\ODA\\ODAFileConverter 27.1.0\\ODAFileConverter.exe'
    OUTPUT_VERSION = 'ACAD2013'
    OVERWRITE_EXISTING_DXF = True


    def check_oda_exe() -> Path:
        """
        检查 ODAFileConverter.exe 是否存在。
        """
        exe_path = Path(ODA_EXE_PATH)
        if not exe_path.exists():
            raise FileNotFoundError(f'没有找到 ODAFileConverter.exe。\n\n当前配置路径：{exe_path}\n\n请检查 ODA_EXE_PATH 是否写对。')
        if exe_path.name.lower() != 'odafileconverter.exe':
            raise ValueError(f'ODA_EXE_PATH 必须指向 ODAFileConverter.exe。\n\n当前路径：{exe_path}')
        return exe_path

    def convert_dwg_to_same_name_dxf(dwg_path: Path) -> Path:
        """
        使用 ODA File Converter 将 DWG 转换为同目录、同名 DXF。

        示例：
            D:/data/cheku(all).dwg
            ->
            D:/data/cheku(all).dxf
        """
        dwg_path = Path(dwg_path)
        if not dwg_path.exists():
            raise FileNotFoundError(f'DWG 文件不存在：{dwg_path}')
        if dwg_path.suffix.lower() != '.dwg':
            raise ValueError(f'输入文件不是 DWG：{dwg_path}')
        oda_exe = check_oda_exe()
        final_dxf_path = dwg_path.with_suffix('.dxf')
        if final_dxf_path.exists():
            if OVERWRITE_EXISTING_DXF:
                try:
                    final_dxf_path.unlink()
                    print(f'[INFO] 已删除旧 DXF：{final_dxf_path}')
                except PermissionError as e:
                    raise PermissionError(f'无法删除已有 DXF 文件，可能正在被 CAD 软件或其他程序占用。\n\n请关闭该文件后重试：{final_dxf_path}') from e
            else:
                print(f'[INFO] 同名 DXF 已存在，直接返回：{final_dxf_path}')
                return final_dxf_path
        print('=' * 80)
        print('[INFO] 开始 DWG 转 DXF')
        print(f'[INFO] 输入 DWG：{dwg_path}')
        print(f'[INFO] 输出 DXF：{final_dxf_path}')
        print(f'[INFO] ODA 路径：{oda_exe}')
        print(f'[INFO] 输出版本：{OUTPUT_VERSION}')
        print('=' * 80)
        with tempfile.TemporaryDirectory(prefix='oda_convert_') as temp_root_text:
            temp_root = Path(temp_root_text)
            input_dir = temp_root / 'input'
            output_dir = temp_root / 'output'
            input_dir.mkdir(parents=True, exist_ok=True)
            output_dir.mkdir(parents=True, exist_ok=True)
            temp_dwg = input_dir / 'input.dwg'
            shutil.copy2(dwg_path, temp_dwg)
            cmd = [str(oda_exe), str(input_dir), str(output_dir), OUTPUT_VERSION, 'DXF', '0', '1', '*.dwg']
            print('[INFO] 执行 ODA 命令：')
            print(' '.join((f'"{x}"' if ' ' in x else x for x in cmd)))
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='ignore', timeout=600)
            print('[INFO] ODA stdout:')
            print(result.stdout)
            print('[INFO] ODA stderr:')
            print(result.stderr)
            if result.returncode != 0:
                raise RuntimeError(f'ODA File Converter 执行失败。\n\nreturncode: {result.returncode}\n\nstdout:\n{result.stdout}\n\nstderr:\n{result.stderr}')
            dxf_candidates = list(output_dir.rglob('*.dxf'))
            if not dxf_candidates:
                raise RuntimeError(f'ODA 执行完成，但没有生成 DXF 文件。\n\n输出目录：{output_dir}\n\nstdout:\n{result.stdout}\n\nstderr:\n{result.stderr}')
            generated_dxf = dxf_candidates[0]
            if generated_dxf.stat().st_size <= 0:
                raise RuntimeError(f'ODA 生成的 DXF 文件为空：{generated_dxf}')
            shutil.copy2(generated_dxf, final_dxf_path)
        if not final_dxf_path.exists():
            raise RuntimeError(f'转换失败，未生成最终 DXF：{final_dxf_path}')
        if final_dxf_path.stat().st_size <= 0:
            raise RuntimeError(f'转换失败，最终 DXF 文件为空：{final_dxf_path}')
        print('=' * 80)
        print('[OK] DWG 转 DXF 成功')
        print(f'[OK] 输出文件：{final_dxf_path}')
        print('=' * 80)
        return final_dxf_path
    return dict(locals())

_s01_cad = _register_embedded_module(
    'fire_inspection_system.cad_local_dwgtodxf',
    _build_s01_cad(),
    aliases=('cad_local_dwgtodxf',),
)

# === CONSOLIDATED PUBLIC API ===
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class CadInputResult:
    input_cad: Path
    input_dxf: Path
    run_dir: Path
    conversion_performed: bool

    def to_summary(self) -> dict[str, object]:
        return {
            "input_cad": str(self.input_cad),
            "input_dwg": str(self.input_cad) if self.conversion_performed else "",
            "input_dxf": str(self.input_dxf),
            "dwg_to_dxf": {
                "performed": self.conversion_performed,
                "input_dwg": str(self.input_cad) if self.conversion_performed else "",
                "output_dxf": str(self.input_dxf),
            },
            "run_dir": str(self.run_dir),
        }


def _strip_quotes(value: str) -> str:
    return value.strip().strip('"').strip("'")


def _choose_cad_file() -> Path:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        selected = filedialog.askopenfilename(
            title="请选择 DWG 或 DXF 图纸文件",
            filetypes=[
                ("CAD 图纸", "*.dwg *.dxf"),
                ("DWG 文件", "*.dwg"),
                ("DXF 文件", "*.dxf"),
                ("所有文件", "*.*"),
            ],
        )
    finally:
        root.destroy()
    if not selected:
        raise RuntimeError("未选择 DWG 或 DXF 文件，程序已取消。")
    return Path(selected)


def _resolve_input(raw_input: str) -> Path:
    if raw_input:
        return Path(_strip_quotes(raw_input)).expanduser().resolve()
    return _choose_cad_file().expanduser().resolve()


def _default_run_dir(input_dxf: Path, output_root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    safe_stem = (
        re.sub(r"[^0-9A-Za-z_\-.\u4e00-\u9fff]+", "_", input_dxf.stem).strip("_")
        or "drawing"
    )
    return output_root / f"{safe_stem}_{stamp}"


def run_stage(
    raw_input: str,
    output_dir: str,
    *,
    default_output_root: Path,
) -> CadInputResult:
    input_cad = _resolve_input(raw_input)
    if not input_cad.is_file():
        raise FileNotFoundError(input_cad)
    suffix = input_cad.suffix.lower()
    if suffix not in {".dwg", ".dxf"}:
        raise ValueError(f"主流程只支持 DWG 或 DXF 文件: {input_cad}")

    conversion_performed = suffix == ".dwg"
    if conversion_performed:
        input_dxf = _s01_cad.convert_dwg_to_same_name_dxf(input_cad).resolve()
    else:
        input_dxf = input_cad.resolve()
    if not input_dxf.is_file() or input_dxf.suffix.lower() != ".dxf":
        raise RuntimeError(f"未得到有效 DXF 输入文件: {input_dxf}")

    run_dir = (
        Path(output_dir).expanduser().resolve()
        if output_dir
        else _default_run_dir(input_dxf, default_output_root)
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    return CadInputResult(
        input_cad=input_cad,
        input_dxf=input_dxf,
        run_dir=run_dir,
        conversion_performed=conversion_performed,
    )


__all__ = ["CadInputResult", "run_stage"]
