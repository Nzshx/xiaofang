# -*- coding: utf-8 -*-
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


# 你的 ODA File Converter 实际路径
ODA_EXE_PATH = r"C:\Program Files\ODA\ODAFileConverter 27.1.0\ODAFileConverter.exe"

# 输出 DXF 版本
# 常用：ACAD2010 / ACAD2013 / ACAD2018
OUTPUT_VERSION = "ACAD2013"

# 如果同名 DXF 已存在，是否覆盖
OVERWRITE_EXISTING_DXF = True


def choose_dwg_file() -> Path:
    """
    弹窗选择 DWG 文件。
    """
    root = tk.Tk()
    root.withdraw()

    file_path = filedialog.askopenfilename(
        title="请选择需要转换的 DWG 文件",
        filetypes=[
            ("DWG 文件", "*.dwg"),
            ("所有文件", "*.*"),
        ],
    )

    root.destroy()

    if not file_path:
        raise RuntimeError("未选择 DWG 文件，程序已取消。")

    dwg_path = Path(file_path)

    if not dwg_path.exists():
        raise FileNotFoundError(f"DWG 文件不存在：{dwg_path}")

    if dwg_path.suffix.lower() != ".dwg":
        raise ValueError(f"请选择 .dwg 文件，当前文件为：{dwg_path}")

    return dwg_path


def check_oda_exe() -> Path:
    """
    检查 ODAFileConverter.exe 是否存在。
    """
    exe_path = Path(ODA_EXE_PATH)

    if not exe_path.exists():
        raise FileNotFoundError(
            "没有找到 ODAFileConverter.exe。\n\n"
            f"当前配置路径：{exe_path}\n\n"
            "请检查 ODA_EXE_PATH 是否写对。"
        )

    if exe_path.name.lower() != "odafileconverter.exe":
        raise ValueError(
            "ODA_EXE_PATH 必须指向 ODAFileConverter.exe。\n\n"
            f"当前路径：{exe_path}"
        )

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
        raise FileNotFoundError(f"DWG 文件不存在：{dwg_path}")

    if dwg_path.suffix.lower() != ".dwg":
        raise ValueError(f"输入文件不是 DWG：{dwg_path}")

    oda_exe = check_oda_exe()

    final_dxf_path = dwg_path.with_suffix(".dxf")

    if final_dxf_path.exists():
        if OVERWRITE_EXISTING_DXF:
            try:
                final_dxf_path.unlink()
                print(f"[INFO] 已删除旧 DXF：{final_dxf_path}")
            except PermissionError as e:
                raise PermissionError(
                    "无法删除已有 DXF 文件，可能正在被 CAD 软件或其他程序占用。\n\n"
                    f"请关闭该文件后重试：{final_dxf_path}"
                ) from e
        else:
            print(f"[INFO] 同名 DXF 已存在，直接返回：{final_dxf_path}")
            return final_dxf_path

    print("=" * 80)
    print("[INFO] 开始 DWG 转 DXF")
    print(f"[INFO] 输入 DWG：{dwg_path}")
    print(f"[INFO] 输出 DXF：{final_dxf_path}")
    print(f"[INFO] ODA 路径：{oda_exe}")
    print(f"[INFO] 输出版本：{OUTPUT_VERSION}")
    print("=" * 80)

    # 用临时目录转换，避免中文路径、括号、空格等路径问题影响 ODA
    with tempfile.TemporaryDirectory(prefix="oda_convert_") as temp_root_text:
        temp_root = Path(temp_root_text)

        input_dir = temp_root / "input"
        output_dir = temp_root / "output"

        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        temp_dwg = input_dir / "input.dwg"

        # 复制原始 DWG 到临时目录
        shutil.copy2(dwg_path, temp_dwg)

        # ODA 命令行参数：
        # ODAFileConverter.exe 输入目录 输出目录 输出版本 输出类型 是否递归 是否审计 文件过滤
        cmd = [
            str(oda_exe),
            str(input_dir),
            str(output_dir),
            OUTPUT_VERSION,
            "DXF",
            "0",
            "1",
            "*.dwg",
        ]

        print("[INFO] 执行 ODA 命令：")
        print(" ".join(f'"{x}"' if " " in x else x for x in cmd))

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=600,
        )

        print("[INFO] ODA stdout:")
        print(result.stdout)

        print("[INFO] ODA stderr:")
        print(result.stderr)

        if result.returncode != 0:
            raise RuntimeError(
                "ODA File Converter 执行失败。\n\n"
                f"returncode: {result.returncode}\n\n"
                f"stdout:\n{result.stdout}\n\n"
                f"stderr:\n{result.stderr}"
            )

        dxf_candidates = list(output_dir.rglob("*.dxf"))

        if not dxf_candidates:
            raise RuntimeError(
                "ODA 执行完成，但没有生成 DXF 文件。\n\n"
                f"输出目录：{output_dir}\n\n"
                f"stdout:\n{result.stdout}\n\n"
                f"stderr:\n{result.stderr}"
            )

        generated_dxf = dxf_candidates[0]

        if generated_dxf.stat().st_size <= 0:
            raise RuntimeError(f"ODA 生成的 DXF 文件为空：{generated_dxf}")

        # 复制回原目录，保持原始文件同名
        shutil.copy2(generated_dxf, final_dxf_path)

    if not final_dxf_path.exists():
        raise RuntimeError(f"转换失败，未生成最终 DXF：{final_dxf_path}")

    if final_dxf_path.stat().st_size <= 0:
        raise RuntimeError(f"转换失败，最终 DXF 文件为空：{final_dxf_path}")

    print("=" * 80)
    print("[OK] DWG 转 DXF 成功")
    print(f"[OK] 输出文件：{final_dxf_path}")
    print("=" * 80)

    return final_dxf_path


def main():
    try:
        dwg_path = choose_dwg_file()
        dxf_path = convert_dwg_to_same_name_dxf(dwg_path)

        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            "转换完成",
            f"DWG 转 DXF 成功：\n\n{dxf_path}"
        )
        root.destroy()

    except Exception as e:
        print("=" * 80)
        print("[ERROR] 转换失败")
        print(f"[ERROR] {e}")
        print("=" * 80)

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("转换失败", str(e))
        root.destroy()

        sys.exit(1)


if __name__ == "__main__":
    main()