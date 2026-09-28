"""
接口一（入站）::

    POST /api/cad/receive
    {"id": "后端CAD原始图纸id", "file_url": "https://.../drawing.dxf"}

接口收到并校验请求后立即返回；CAD 下载、十一阶段流水线和接口二回传在
后台任务中执行。

状态查询接口::

    GET /api/cad/status?id=后端CAD原始图纸id

前端每 3～5 秒轮询一次。处理过程对外只展示“图纸巡检对象识别”和
“巡检路径生成”，终态为“解析完成”或“解析失败”；内部失败原因只写本地
状态与日志，不通过状态接口返回。接口二的业务回调结构不受影响。

接口二（出站）一次 POST 包含全部物理楼层的 JSON 数组，并在任务目录保存
内容完全相同的 ``interface2_result.json``。每个数组元素都包含接口一的 cad_id、
楼层结构图及其直属 cad_points、同楼层的路线图。图片字段是原始 PNG 字节的
Base64 字符串（不带 ``data:image/png;base64,`` 前缀）。点位的 x/y 是相对于
整张传输图片左上角的 0..100 百分比坐标。

路线生成接口（同步批量重规划）::

    POST /api/cad/route
    {
      "cad_id": "后端CAD原始图纸id",
      "cad_points": [
        {"point_id": ["point_id", "point_id"], "route_id": "route_id"}
      ]
    }

接口收到请求后，按 route_id 分楼层在原始 CAD 安全物理图上完成重规划，
并在同一个 HTTP 响应中直接返回全部路线图片；不创建后台任务，也不再调用
路线结果回调地址。规划失败统一同步返回 ``result: false``。

本模块只依赖 Python 标准库和 Pillow，不改变十一阶段算法实现。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from PIL import Image, ImageDraw


MODULE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = MODULE_DIR.parent
PIPELINE_MAIN = PROJECT_ROOT / "fire_inspection_system" / "main.py"
DEFAULT_JOB_ROOT = PROJECT_ROOT / "outputs" / "backend_api_jobs"
DEFAULT_RECEIVE_PATH = "/api/cad/receive"
DEFAULT_REPLAN_PATH = "/api/cad/route"
DEFAULT_STATUS_PATH = "/api/cad/status"
DEFAULT_HEALTH_PATH = "/health"
SUPPORTED_CAD_SUFFIXES = {".dwg", ".dxf"}
LOGGER = logging.getLogger("fire_inspection_backend_interface")

PIPELINE_STAGE_PATTERN = re.compile(
    r"^\[(?P<stage>\d+A?)/11\]\s*(?P<name>.+?)\s*$"
)
PIPELINE_STAGE_PROGRESS = {
    "1": 8,
    "2": 15,
    "3": 22,
    "4": 30,
    "5": 38,
    "5A": 42,
    "6": 47,
    "7": 55,
    "8": 63,
    "9": 71,
    "10": 79,
    "11": 86,
}
TERMINAL_JOB_STATUSES = {
    "completed",
    "failed",
    "result_ready_callback_url_missing",
}
SUCCESS_JOB_STATUSES = {"completed", "result_ready_callback_url_missing"}
HEARTBEAT_JOB_STATUSES = {
    "downloading",
    "running_pipeline",
    "building_callback_payloads",
    "sending_callback",
}


def _pipeline_progress_from_line(line: str) -> Optional[Dict[str, Any]]:
    """把主流水线标准输出中的阶段标题转换成状态接口字段。"""

    text = _text(line)
    match = PIPELINE_STAGE_PATTERN.match(text)
    if match:
        stage_code = match.group("stage")
        stage_name = match.group("name")
        return {
            "phase": "pipeline",
            "stage_code": stage_code,
            "stage_index": int(re.match(r"\d+", stage_code).group()),
            "stage_total": 11,
            "stage_name": stage_name,
            "progress_percent": PIPELINE_STAGE_PROGRESS.get(stage_code, 5),
            "message": "正在执行阶段 {}/11：{}".format(stage_code, stage_name),
        }
    if text.startswith("[业务图片]"):
        return {
            "phase": "pipeline_outputs",
            "stage_code": "outputs",
            "stage_index": 11,
            "stage_total": 11,
            "stage_name": "生成业务图片",
            "progress_percent": 88,
            "message": "正在生成业务图片",
        }
    if text.startswith("完成。主流程摘要"):
        return {
            "phase": "pipeline_finalize",
            "stage_code": "pipeline_done",
            "stage_index": 11,
            "stage_total": 11,
            "stage_name": "流水线完成",
            "progress_percent": 90,
            "message": "CAD 识别流水线已完成，正在整理回调数据",
        }
    return None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_cad_file_url(
    file_url: str,
) -> Tuple[str, urllib.parse.ParseResult]:
    """校验 CAD URL，并兼容文件名中未编码的 ``#``。

    按 URL 标准，裸 ``#`` 会把后面的文件名截成 fragment。例如
    ``3#楼平面图.dxf`` 的 path 实际只剩 ``3``，从而无法识别 DXF 后缀。
    只有当原 path 没有 CAD 后缀、且把 ``#`` 编码为 ``%23`` 后能够得到
    DWG/DXF 后缀时，才执行这一兼容转换；正常 URL 和真实 fragment 不变。
    """

    value = _text(file_url)
    if not value:
        raise ValueError("file_url 不能为空")

    parts = urllib.parse.urlparse(value)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise ValueError("file_url 只允许 http/https URL")

    suffix = Path(urllib.parse.unquote(parts.path)).suffix.lower()
    if suffix in SUPPORTED_CAD_SUFFIXES:
        return value, parts

    if parts.fragment and "#" in value:
        candidate = value.replace("#", "%23")
        candidate_parts = urllib.parse.urlparse(candidate)
        candidate_suffix = Path(
            urllib.parse.unquote(candidate_parts.path)
        ).suffix.lower()
        if candidate_suffix in SUPPORTED_CAD_SUFFIXES:
            LOGGER.warning(
                "file_url 的 CAD 文件名包含未编码的 #，已自动转换为 %%23: %s",
                candidate,
            )
            return candidate, candidate_parts

    raise ValueError("file_url 必须指向 DWG 或 DXF 文件")


def _read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON 根节点必须是对象: {}".format(path))
    return value


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def _safe_component(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_.\-一-鿿]+", "_", value).strip("._")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return "{}_{}".format(normalized[:80] or "cad", digest)


def _physical_floor_id(scope_id: Any) -> str:
    value = _text(scope_id)
    return value.split("__", 1)[0] if "__" in value else value


def _floor_resource_id(cad_id: str, floor_id: str, resource: str) -> str:
    """生成可重复且跨 CAD 唯一的楼层资源主键。

    floor_id 本身（例如 F7）只在一张 CAD 内唯一，不能直接作为数据库主键。
    摘要同时包含接口一 cad_id、业务楼层 scope 和资源类型，因此结构图与路线图
    既不会互相重号，同一份 CAD 重试时也不会生成新的主键。
    """

    digest = hashlib.sha256(
        "{}\0{}\0{}".format(cad_id, floor_id, resource).encode("utf-8")
    ).hexdigest()[:20]
    return "{}_{}_{}".format(floor_id, resource, digest)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _authorization_headers(token: str) -> Dict[str, str]:
    return {"Authorization": "Bearer {}".format(token)} if token else {}


def get_file_from_url(
    file_url: str,
    save_dir: Path,
    timeout: int = 60,
    max_bytes: int = 1024 * 1024 * 1024,
    allowed_hosts: Optional[Iterable[str]] = None,
) -> Path:
    """按后端模板的 URL path 编码方式下载 DWG/DXF 到独立任务目录。"""

    file_url, parts = _normalize_cad_file_url(file_url)
    allowed = {_text(item).lower() for item in (allowed_hosts or []) if _text(item)}
    host = (parts.hostname or "").lower()
    if allowed and host not in allowed:
        raise ValueError("文件域名不在允许列表中: {}".format(host))

    filename = Path(urllib.parse.unquote(parts.path)).name
    suffix = Path(filename).suffix.lower()
    filename = _safe_component(Path(filename).stem) + suffix
    save_dir.mkdir(parents=True, exist_ok=True)
    destination = save_dir / filename

    safe_path = urllib.parse.quote(parts.path, safe="/%:@")
    encoded_url = parts._replace(path=safe_path).geturl()
    request = urllib.request.Request(
        encoded_url,
        headers={
            "User-Agent": "Mozilla/5.0 CAD-Inspection-Backend-Bridge/1.0",
            "Referer": "https://www.cscec83.cn/",
        },
    )
    downloaded = 0
    with urllib.request.urlopen(request, timeout=timeout) as response:
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > max_bytes:
            raise ValueError("CAD 文件超过大小限制: {} bytes".format(max_bytes))
        with destination.open("wb") as handle:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                downloaded += len(block)
                if downloaded > max_bytes:
                    raise ValueError("CAD 文件超过大小限制: {} bytes".format(max_bytes))
                handle.write(block)
    if downloaded <= 0:
        raise ValueError("下载到的 CAD 文件为空")
    return destination.resolve()


def post_json(
    url: str,
    payload: Any,
    timeout: int = 120,
    token: str = "",
) -> Dict[str, Any]:
    """使用后端模板相同的 urllib 方式发送 JSON。"""

    if not url:
        raise ValueError("回调 URL 不能为空")
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "User-Agent": "CAD-Inspection-Backend-Bridge/1.0",
        **_authorization_headers(token),
    }
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        status = int(getattr(response, "status", 200))
    if not 200 <= status < 300:
        raise RuntimeError("回调接口返回 HTTP {}".format(status))
    if not raw:
        return {"http_status": status}
    decoded = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(decoded, dict):
        raise ValueError("回调接口返回值必须是 JSON 对象")
    result = decoded.get("result")
    if result is False or _text(result).lower() in {"false", "0", "no"}:
        raise RuntimeError("后端拒绝回调数据: {}".format(decoded))
    return decoded


POINT_TYPE_BY_TARGET_CLASS = {
    # 《消防巡检对象》- 建筑
    "安全出口": "building",
    "消防水泵房/消防泵房": "building",
    "开闭所": "building",
    "用户变": "building",
    "配电房": "building",
    "消防控制室/消控室": "building",
    "风机房": "building",
    "进风机房": "building",
    "补风机房": "building",
    "排烟机房": "building",
    "强电间": "building",
    "弱电间": "building",
    "强弱电间": "building",
    "电井": "building",
    "消防电梯": "building",
    "防火卷帘": "building",
    "避难间": "building",
    # 《消防巡检对象》- 水
    "灭火器": "water",
    "供水装置": "water",
    "管网与喷头": "water",
    "储存装置间/灭火剂储存装置/驱动装置": "water",
    "供水水源/消防水池": "water",
    "消防水泵": "water",
    "报警阀组": "water",
    "喷头": "water",
    "消防水箱": "water",
    "室外（内）消火栓": "water",
    "灭火装置": "water",
    # 《消防巡检对象》- 暖通
    "排烟机": "hvac",
    "机械防烟/机械加压送风": "hvac",
    "自然通风/自然防烟": "hvac",
    "加压风机（管道）/排烟风机（管道）/补风机（管道）": "hvac",
    "机械排烟": "hvac",
    "自然排烟": "hvac",
    "防火阀/排烟防火阀": "hvac",
    # 《消防巡检对象》- 电
    "备用发电机/柴油发电机房": "electric",
    "变配电房": "electric",
    "消防应急照明和疏散指示标志": "electric",
    "火灾探测器": "electric",
    "消防通讯": "electric",
    "布线": "electric",
    "应急广播及警报装置": "electric",
    "区域显示器": "electric",
    "手动报警按钮": "electric",
    "火灾报警控制器": "electric",
    "消防联动控制器及消防控制室图形显示装置": "electric",
}


def _point_type(target_class: Any) -> str:
    name = _text(target_class)
    point_type = POINT_TYPE_BY_TARGET_CLASS.get(name)
    if point_type is None:
        raise ValueError("巡检对象类型未配置四分类映射: {}".format(name or "<空>"))
    return point_type


def _optional_confidence(value: Any) -> Optional[float]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return round(_clamp(float(value), 0.0, 1.0), 6)
    except (TypeError, ValueError):
        return None


def _optional_spec(target: Mapping[str, Any]) -> str:
    for key in ("spec", "model", "model_number", "equipment_model", "型号"):
        value = _text(target.get(key))
        if value:
            return value
    return ""


def image_to_base64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


@dataclass(frozen=True)
class RenderRecord:
    floor_id: str
    image_path: Path
    image_width: int
    image_height: int
    cad_bbox: Tuple[float, float, float, float]
    scale: float
    offset_x: float
    offset_y: float
    route_image_path: Optional[Path] = None

    def cad_to_pixel(self, x: float, y: float) -> Tuple[float, float]:
        minx, _miny, _maxx, maxy = self.cad_bbox
        return (
            self.offset_x + (x - minx) * self.scale,
            self.offset_y + (maxy - y) * self.scale,
        )

    def cad_to_percent(self, x: float, y: float) -> Tuple[float, float]:
        px, py = self.cad_to_pixel(x, y)
        x_percent = px / max(1, self.image_width) * 100.0
        y_percent = py / max(1, self.image_height) * 100.0

        # 不能把图片外的点强制压到 0/100 边界。否则多个无效点会堆叠在
        # 图片边缘，看起来像是前端渲染偏移，实际已经失去原始位置含义。
        tolerance = 1e-7
        if not (
            -tolerance <= x_percent <= 100.0 + tolerance
            and -tolerance <= y_percent <= 100.0 + tolerance
        ):
            raise ValueError(
                "CAD 点不在传输图片范围内: "
                "cad=({:.9f}, {:.9f}), pixel=({:.6f}, {:.6f}), "
                "percent=({:.6f}, {:.6f}), cad_bbox={}".format(
                    x,
                    y,
                    px,
                    py,
                    x_percent,
                    y_percent,
                    self.cad_bbox,
                )
            )
        return (
            round(_clamp(x_percent, 0.0, 100.0), 6),
            round(_clamp(y_percent, 0.0, 100.0), 6),
        )


def _manifest_image_path(manifest_path: Path, value: Any) -> Path:
    path = Path(_text(value))
    candidates = [path]
    if not path.is_absolute():
        candidates.append(manifest_path.parent / path)
    candidates.append(manifest_path.parent / "images" / path.name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return path


def _business_render_records(run_dir: Path) -> Dict[str, RenderRecord]:
    """读取新流水线提供给业务接口的成对缩略图及其坐标变换。"""

    manifest_path = run_dir / "business_outputs" / "business_image_manifest.json"
    if not manifest_path.is_file():
        summary_path = run_dir / "pipeline_summary.json"
        if not summary_path.is_file():
            return {}
        summary = _read_json(summary_path)
        configured = _text((summary.get("business_image_outputs") or {}).get("manifest"))
        if configured:
            manifest_path = Path(configured)
    if not manifest_path.is_file():
        return {}

    manifest = _read_json(manifest_path)
    records: Dict[str, RenderRecord] = {}
    for row in manifest.get("images", []):
        if not isinstance(row, dict):
            continue
        scope_id = _text(row.get("scope_id")) or _text(row.get("floor_id"))
        bbox = row.get("cad_bbox") or []
        transform = row.get("transform") or {}
        obstacle_path = _manifest_image_path(manifest_path, row.get("obstacle_image_path"))
        route_path = _manifest_image_path(manifest_path, row.get("route_image_path"))
        if not scope_id or len(bbox) != 4 or not isinstance(transform, dict):
            continue
        if not obstacle_path.is_file() or not route_path.is_file():
            raise FileNotFoundError(
                "业务图片不完整 scope_id={}: obstacle={}, route={}".format(
                    scope_id, obstacle_path, route_path
                )
            )
        record = RenderRecord(
            floor_id=scope_id,
            image_path=obstacle_path,
            image_width=int(row.get("image_width") or 0),
            image_height=int(row.get("image_height") or 0),
            cad_bbox=tuple(float(item) for item in bbox),
            scale=float(transform.get("scale_pixels_per_cad_unit") or 0.0),
            offset_x=float(transform.get("offset_x_pixels") or 0.0),
            offset_y=float(transform.get("offset_y_pixels") or 0.0),
            route_image_path=route_path,
        )
        if record.image_width > 0 and record.image_height > 0 and record.scale > 0.0:
            records[scope_id] = record
    if not records:
        raise ValueError("business_image_manifest.json 中没有有效的业务图片记录")
    return records


def _manifest_render_records(run_dir: Path) -> Dict[str, RenderRecord]:
    manifest_path = (
        run_dir
        / "obstacle_building_region_render"
        / "obstacle_building_vision_manifest.json"
    )
    if not manifest_path.is_file():
        return {}
    manifest = _read_json(manifest_path)
    annotated_by_floor: Dict[str, Path] = {}
    sheets_path = (
        run_dir
        / "obstacle_building_region_render"
        / "vision_building_regions"
        / "drawing_sheets_floors_with_building_regions.json"
    )
    if sheets_path.is_file():
        sheets = _read_json(sheets_path)
        for sheet in sheets.get("sheets", []):
            if not isinstance(sheet, dict):
                continue
            floor_id = _text(sheet.get("floor_id"))
            detection = sheet.get("building_region_detection")
            if not isinstance(detection, dict):
                regions = sheet.get("inspection_regions") or []
                if regions and isinstance(regions[0], dict):
                    detection = regions[0].get("building_region_detection")
            if isinstance(detection, dict):
                candidate = Path(_text(detection.get("annotated_image_path")))
                if floor_id and candidate.is_file():
                    annotated_by_floor[floor_id] = candidate.resolve()

    records: Dict[str, RenderRecord] = {}
    for row in manifest.get("images", []):
        if not isinstance(row, dict):
            continue
        floor_id = _text(row.get("floor_id"))
        bbox = row.get("cad_bbox") or []
        transform = row.get("transform") or {}
        image_path = annotated_by_floor.get(floor_id)
        if image_path is None:
            image_path = Path(_text(row.get("image_path")))
        if (
            not floor_id
            or not image_path.is_file()
            or len(bbox) != 4
            or not isinstance(transform, dict)
        ):
            continue
        records[floor_id] = RenderRecord(
            floor_id=floor_id,
            image_path=image_path.resolve(),
            image_width=int(row.get("image_width") or 0),
            image_height=int(row.get("image_height") or 0),
            cad_bbox=tuple(float(item) for item in bbox),
            scale=float(transform.get("scale_pixels_per_cad_unit") or 0.0),
            offset_x=float(transform.get("offset_x_pixels") or 0.0),
            offset_y=float(transform.get("offset_y_pixels") or 0.0),
        )
    return {key: value for key, value in records.items() if value.scale > 0.0}


def _iter_geometry_lines(geometry: Mapping[str, Any]) -> Iterable[List[Tuple[float, float]]]:
    kind = _text(geometry.get("type"))
    coordinates = geometry.get("coordinates") or []
    if kind == "LineString":
        yield [(float(x), float(y)) for x, y, *_ in coordinates]
    elif kind == "MultiLineString":
        for line in coordinates:
            yield [(float(x), float(y)) for x, y, *_ in line]
    elif kind == "Polygon":
        for ring in coordinates:
            yield [(float(x), float(y)) for x, y, *_ in ring]
    elif kind == "MultiPolygon":
        for polygon in coordinates:
            for ring in polygon:
                yield [(float(x), float(y)) for x, y, *_ in ring]


def _load_geojson_features(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    payload = _read_json(path)
    return [row for row in payload.get("features", []) if isinstance(row, dict)]


def _fallback_render_records(run_dir: Path) -> Dict[str, RenderRecord]:
    """没有阶段05A图片时，直接用障碍物 GeoJSON 生成楼层底图。"""

    summary = _read_json(run_dir / "pipeline_summary.json")
    sheets_path = Path(_text((summary.get("cad_preprocess") or {}).get("sheets_json")))
    if not sheets_path.is_file():
        return {}
    sheets = _read_json(sheets_path)
    union_paths = [
        Path(_text(item))
        for item in (summary.get("obstacle_recognition") or {}).get("union_geojsons", [])
        if _text(item)
    ]
    obstacle_by_floor: Dict[str, List[Dict[str, Any]]] = {}
    for path in union_paths:
        for feature in _load_geojson_features(path):
            properties = feature.get("properties") or {}
            floor_id = _physical_floor_id(properties.get("floor_id"))
            if floor_id:
                obstacle_by_floor.setdefault(floor_id, []).append(feature)

    output_dir = run_dir / "backend_interface_images" / "base"
    output_dir.mkdir(parents=True, exist_ok=True)
    records: Dict[str, RenderRecord] = {}
    for sheet in sheets.get("sheets", []):
        if not isinstance(sheet, dict) or not sheet.get("path_planning_usable"):
            continue
        floor_id = _text(sheet.get("floor_id"))
        bbox = sheet.get("inspection_region_bbox") or sheet.get("bbox") or []
        if not floor_id or len(bbox) != 4:
            continue
        minx, miny, maxx, maxy = [float(item) for item in bbox]
        width_cad = maxx - minx
        height_cad = maxy - miny
        if width_cad <= 0.0 or height_cad <= 0.0:
            continue
        margin = 30
        max_side = 2400
        scale = min(
            (max_side - 2 * margin) / width_cad,
            (max_side - 2 * margin) / height_cad,
        )
        width = max(256, int(round(width_cad * scale + 2 * margin)))
        height = max(256, int(round(height_cad * scale + 2 * margin)))
        record = RenderRecord(
            floor_id=floor_id,
            image_path=output_dir / "{}_obstacles.png".format(_safe_component(floor_id)),
            image_width=width,
            image_height=height,
            cad_bbox=(minx, miny, maxx, maxy),
            scale=scale,
            offset_x=float(margin),
            offset_y=float(margin),
        )
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        line_width = max(1, int(round(max(width, height) / 1600.0)))
        for feature in obstacle_by_floor.get(floor_id, []):
            geometry = feature.get("geometry") or {}
            for line in _iter_geometry_lines(geometry):
                pixels = [record.cad_to_pixel(x, y) for x, y in line]
                if len(pixels) >= 2:
                    draw.line(pixels, fill="#ff3b30", width=line_width, joint="curve")
        image.save(record.image_path, format="PNG", optimize=True)
        records[floor_id] = record
    return records


def load_render_records(run_dir: Path) -> Dict[str, RenderRecord]:
    records = _business_render_records(run_dir)
    if records:
        return records
    records = _manifest_render_records(run_dir)
    return records if records else _fallback_render_records(run_dir)


def _load_targets(run_dir: Path) -> List[Dict[str, Any]]:
    path = run_dir / "navigation_graph" / "inputs" / "navigation_targets.geojson"
    features = _load_geojson_features(path)
    result: List[Dict[str, Any]] = []
    for feature in features:
        properties = dict(feature.get("properties") or {})
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates") or []
        if geometry.get("type") != "Point" or len(coordinates) < 2:
            continue
        properties["cad_x"] = float(coordinates[0])
        properties["cad_y"] = float(coordinates[1])
        result.append(properties)
    return result


def _mandatory_by_target(run_dir: Path) -> Dict[str, bool]:
    path = (
        run_dir
        / "path_planning"
        / "semantic_value_inputs"
        / "target_candidates_with_context.json"
    )
    if not path.is_file():
        return {}
    payload = _read_json(path)
    return {
        _text(row.get("target_id")): bool(row.get("mandatory"))
        for row in payload.get("targets", [])
        if isinstance(row, dict) and _text(row.get("target_id"))
    }


def _route_features(run_dir: Path) -> List[Dict[str, Any]]:
    path = (
        run_dir
        / "path_planning"
        / "dual_graph"
        / "physical_walk"
        / "forwarding_route.geojson"
    )
    return _load_geojson_features(path)


def _resize_for_transport(image: Image.Image, max_side: int) -> Image.Image:
    if max_side <= 0 or max(image.size) <= max_side:
        return image
    ratio = max_side / float(max(image.size))
    size = (max(1, int(round(image.width * ratio))), max(1, int(round(image.height * ratio))))
    return image.resize(size, Image.Resampling.LANCZOS)


def _image_size(path: Path) -> Tuple[int, int]:
    with Image.open(path) as image:
        return int(image.width), int(image.height)


def _write_coordinate_validation_image(
    image_path: Path,
    points: Sequence[Mapping[str, Any]],
    output_path: Path,
) -> Path:
    """在实际传输 PNG 上按接口 x/y 绘制验收点，仅供本地联调。"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(image_path) as source:
        image = source.convert("RGB")
    draw = ImageDraw.Draw(image)
    marker_radius = max(5, min(16, int(round(max(image.size) / 350.0))))
    line_width = max(2, marker_radius // 3)
    for point in points:
        x = float(point["x"]) / 100.0 * image.width
        y = float(point["y"]) / 100.0 * image.height
        draw.ellipse(
            (
                x - marker_radius,
                y - marker_radius,
                x + marker_radius,
                y + marker_radius,
            ),
            fill="#ffffff",
            outline="#ff1744",
            width=line_width,
        )
        draw.line(
            (x - marker_radius, y, x + marker_radius, y),
            fill="#ff1744",
            width=line_width,
        )
        draw.line(
            (x, y - marker_radius, x, y + marker_radius),
            fill="#ff1744",
            width=line_width,
        )
        label = _text(point.get("id"))
        if label:
            draw.text(
                (x + marker_radius + 2, y - marker_radius),
                label,
                fill="#d50000",
                stroke_width=2,
                stroke_fill="#ffffff",
            )
    image.save(output_path, format="PNG", optimize=True)
    return output_path.resolve()


def _render_transport_images(
    run_dir: Path,
    record: RenderRecord,
    floor_targets: Sequence[Mapping[str, Any]],
    route_features: Sequence[Mapping[str, Any]],
    max_image_side: int,
    output_dir: Optional[Path] = None,
    start_target_id: str = "",
    route_color: str = "#e53935",
) -> Tuple[Path, Path]:
    output_dir = output_dir or run_dir / "backend_interface_images" / "transport"
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_floor = _safe_component(record.floor_id)
    thumbnail_path = output_dir / "{}_thumbnail.png".format(safe_floor)
    route_path = output_dir / "{}_route.png".format(safe_floor)

    with Image.open(record.image_path) as source:
        base = source.convert("RGB")
    route_image = base.copy()
    draw = ImageDraw.Draw(route_image)
    route_width = max(3, int(round(max(route_image.size) / 900.0)))
    point_radius = max(5, int(round(max(route_image.size) / 650.0)))

    ordered_features = sorted(
        route_features,
        key=lambda row: int((row.get("properties") or {}).get("sequence_no") or 0),
    )
    route_palette = (
        route_color,
        "#ef6c00",
        "#7b1fa2",
        "#00897b",
        "#c2185b",
        "#5d4037",
        "#3949ab",
        "#558b2f",
    )

    def group_color(properties: Mapping[str, Any]) -> str:
        try:
            group_index = int(properties.get("interface_route_group_index") or 1)
        except (TypeError, ValueError):
            group_index = 1
        return route_palette[(max(1, group_index) - 1) % len(route_palette)]

    # Stage 10 的主路线只记录认证导航图上的边。巡检对象原始坐标可能位于
    # 墙边或设备图元上，而 selected_inspection_target 的点是已认证的安全
    # 接入坐标。当多个对象共用同一个导航接入节点时，主路线边数量可以为
    # 0；如果不画这段对象接入支线，前端得到的就会是一张只有底图的图片。
    # 单建筑图模式不区分建筑/水/电/暖类别，所有选中点都按同一建筑物理图
    # 的接入坐标绘制安全支线。
    selected_access_points: Dict[str, Tuple[float, float, str, bool]] = {}
    for feature in ordered_features:
        properties = feature.get("properties") or {}
        if properties.get("feature_type") not in {
            "selected_inspection_target",
            "target_visit",
        }:
            continue
        if _physical_floor_id(properties.get("floor_id")) != record.floor_id:
            continue
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates") or []
        target_id = _text(properties.get("target_id"))
        if (
            target_id
            and geometry.get("type") == "Point"
            and isinstance(coordinates, (list, tuple))
            and len(coordinates) >= 2
        ):
            try:
                selected_access_points[target_id] = (
                    float(coordinates[0]),
                    float(coordinates[1]),
                    group_color(properties),
                    bool(properties.get("wall_touch_proxy")),
                )
            except (TypeError, ValueError):
                continue

    access_width = max(3, route_width - 1)
    for target in floor_targets:
        target_id = _text(target.get("target_id"))
        access_point = selected_access_points.get(target_id)
        if access_point is None:
            continue
        raw_point = (float(target["cad_x"]), float(target["cad_y"]))
        raw_pixel = record.cad_to_pixel(*raw_point)
        access_pixel = record.cad_to_pixel(access_point[0], access_point[1])
        if math.dist(raw_pixel, access_pixel) < 0.75:
            continue
        if target.get("interface_wall_touch"):
            # 对象中心可能位于封闭空间内，不绘制穿墙关联线；接触点由下方
            # 的橙色空心标记表示，真实路线终止在该墙体边界点。
            continue
        draw.line(
            (raw_pixel, access_pixel),
            fill=access_point[2],
            width=access_width,
        )

    for feature in ordered_features:
        properties = feature.get("properties") or {}
        if _physical_floor_id(properties.get("floor_id")) != record.floor_id:
            continue
        if properties.get("feature_type") != "route_edge_traversal":
            continue
        for line in _iter_geometry_lines(feature.get("geometry") or {}):
            pixels = [record.cad_to_pixel(x, y) for x, y in line]
            if len(pixels) >= 2:
                draw.line(
                    pixels,
                    fill=group_color(properties),
                    width=route_width,
                    joint="curve",
                )

    for target_id, (x, y, _color, is_wall_touch) in selected_access_points.items():
        if not is_wall_touch:
            continue
        px, py = record.cad_to_pixel(x, y)
        radius = max(7, point_radius)
        draw.ellipse(
            (px - radius, py - radius, px + radius, py + radius),
            outline="#fb8c00",
            width=max(2, route_width // 2),
        )
        draw.line(
            (px - radius, py, px + radius, py),
            fill="#fb8c00",
            width=max(2, route_width // 2),
        )
        draw.line(
            (px, py - radius, px, py + radius),
            fill="#fb8c00",
            width=max(2, route_width // 2),
        )

    for target in floor_targets:
        px, py = record.cad_to_pixel(float(target["cad_x"]), float(target["cad_y"]))
        box = (px - point_radius, py - point_radius, px + point_radius, py + point_radius)
        is_start = bool(target.get("interface_route_group_start")) or (
            _text(target.get("target_id")) == _text(start_target_id)
        )
        outline = group_color(target)
        fill = "#e8f5e9" if is_start else "#ffffff"
        draw.ellipse(box, fill=fill, outline=outline, width=max(2, route_width // 2))
        inner = max(2, point_radius // 3)
        draw.ellipse((px - inner, py - inner, px + inner, py + inner), fill=outline)

    transport_thumbnail = _resize_for_transport(base, max_image_side)
    transport_route = _resize_for_transport(route_image, max_image_side)
    transport_thumbnail.save(thumbnail_path, format="PNG", optimize=True)
    transport_route.save(route_path, format="PNG", optimize=True)
    return thumbnail_path.resolve(), route_path.resolve()


@dataclass(frozen=True)
class CadResultPayload:
    floor_id: str
    payload: Dict[str, Any]
    thumbnail_path: Path
    route_path: Path


@dataclass(frozen=True)
class ReplannedRoutePayload:
    floor_id: str
    payload: Dict[str, Any]
    route_path: Path
    output_dir: Path
    requested_target_ids: Tuple[str, ...]
    planned_target_ids: Tuple[str, ...]
    ordered_target_ids: Tuple[str, ...]
    unreachable_target_reasons: Tuple[Tuple[str, str], ...]
    wall_touch_targets: Tuple[str, ...]
    start_target_id: str
    start_mode: str
    route_group_count: int
    route_validation_path: Path


@dataclass(frozen=True)
class RouteSelectionRequest:
    """Excel 接口第五行中的单个楼层路线选择。"""

    route_id: str
    point_ids: Tuple[str, ...]


class RouteSelectionError(ValueError):
    """接口三中可以安全返回给调用方的选择校验错误。"""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


_POINT_UNREACHABLE_REASON_MESSAGES: Dict[str, Tuple[str, str]] = {
    "not_in_recognition_targets": (
        "point_not_found",
        "点位不存在、已失效或不属于当前 CAD 图纸",
    ),
    "not_in_certified_physical_graph": (
        "not_in_physical_graph",
        "点位已识别，但没有接入认证物理导航图",
    ),
    "no_connected_certified_access_node": (
        "isolated_access_node",
        "点位的导航接入节点为孤立节点，没有可通行连接",
    ),
    "no_safe_wall_touch": (
        "no_safe_wall_touch",
        "点位无法直达，且周围没有可从安全导航区域触碰的墙面点",
    ),
    "invalid_certified_access_coordinate": (
        "invalid_access_coordinate",
        "点位的安全导航接入坐标无效",
    ),
    "floor_missing_from_physical_graph": (
        "floor_navigation_missing",
        "点位所属区域没有可用的安全导航图",
    ),
    "safe_access_node_missing": (
        "safe_access_node_missing",
        "点位没有可用的安全物理图接入节点",
    ),
    "point_on_different_internal_floor": (
        "point_on_different_internal_floor",
        "点位与本次路线锚点不属于同一建筑区域",
    ),
    "physically_disconnected": (
        "physically_disconnected",
        "点位与本次路线锚点不在同一安全连通区域",
    ),
    "unreachable_from_route_start": (
        "unreachable_from_route_start",
        "点位无法从当前路线起点通过安全导航图到达",
    ),
}


def _point_errors_from_unreachable_reasons(
    values: Sequence[Tuple[str, str]],
) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    for point_id, internal_reason in values:
        reason_code, reason = _POINT_UNREACHABLE_REASON_MESSAGES.get(
            _text(internal_reason),
            ("safe_access_unavailable", "点位没有可用的安全导航路径"),
        )
        result.append(
            {
                "point_id": _text(point_id),
                "reason_code": reason_code,
                "reason": reason,
            }
        )
    return result


def _route_failure_response(cad_id: str, exc: BaseException) -> Dict[str, Any]:
    """把已知规划失败转换为稳定、逐点且不泄露内部路径的响应。"""

    cad_id = _text(cad_id)
    code = _text(getattr(exc, "code", "")) or "route_planning_failed"
    raw_details = getattr(exc, "details", None)
    details = dict(raw_details) if isinstance(raw_details, Mapping) else {}
    route_id = _text(getattr(exc, "route_id", "") or details.get("route_id"))
    point_errors: List[Dict[str, str]] = []

    def add_points(values: Any, reason_code: str, reason: str) -> None:
        if not isinstance(values, (list, tuple, set)):
            return
        seen = {row["point_id"] for row in point_errors}
        for value in values:
            point_id = _text(value)
            if point_id and point_id not in seen:
                point_errors.append(
                    {
                        "point_id": point_id,
                        "reason_code": reason_code,
                        "reason": reason,
                    }
                )
                seen.add(point_id)

    message = "路径规划失败"
    suggestion = "请检查所选巡检对象后重新提交"
    public_details: Dict[str, Any] = {}

    if code == "route_id_not_current_cad":
        message = "路线编号不属于当前图纸"
        suggestion = "请使用接口返回的当前图纸 route_id"
    elif code == "selected_points_invalid":
        message = "请求同时包含无效点位和其他楼层点位"
        add_points(
            details.get("missing_target_ids"),
            "point_not_found",
            "点位不存在、已失效或不属于当前 CAD 图纸",
        )
        actual_floors = details.get("actual_floors") or {}
        for point_id in details.get("wrong_floor_target_ids", []) or []:
            point_id = _text(point_id)
            if not point_id:
                continue
            actual_floor = _text(actual_floors.get(point_id))
            point_errors.append(
                {
                    "point_id": point_id,
                    "reason_code": "point_on_other_floor",
                    "reason": "点位属于其他楼层{}".format(
                        "（{}）".format(actual_floor) if actual_floor else ""
                    ),
                }
            )
        public_details["route_ids"] = [
            _text(value) for value in details.get("route_ids", []) or [] if _text(value)
        ]
        public_details["expected_floors"] = dict(details.get("expected_floors") or {})
        public_details["point_route_ids"] = dict(details.get("point_route_ids") or {})
        suggestion = "请刷新当前图纸的巡检对象，并按楼层分别重新选择后提交"
    elif code == "selected_points_unknown":
        message = "部分巡检对象不存在或不属于当前图纸"
        add_points(
            details.get("target_ids"),
            "point_not_found",
            "点位不存在、已失效或不属于当前 CAD 图纸",
        )
        suggestion = "请刷新当前图纸的巡检对象后重新选择"
    elif code == "selected_points_wrong_physical_floor":
        message = "部分巡检对象与所选路线不属于同一楼层"
        actual_floors = details.get("actual_floors") or {}
        for point_id in details.get("target_ids", []) or []:
            point_id = _text(point_id)
            if not point_id:
                continue
            actual_floor = _text(actual_floors.get(point_id))
            point_errors.append(
                {
                    "point_id": point_id,
                    "reason_code": "point_on_other_floor",
                    "reason": "点位属于其他楼层{}".format(
                        "（{}）".format(actual_floor) if actual_floor else ""
                    ),
                }
            )
        public_details["expected_floor_id"] = _text(details.get("expected_floor_id"))
        suggestion = "请按楼层分别选择巡检对象并生成路线"
    elif code == "selected_target_not_user_selectable":
        message = "所选巡检对象均不能参与路线规划"
        reasons = details.get("reasons") or {}
        for value in details.get("target_ids", []) or []:
            point_id = _text(value)
            reason_key = _text(reasons.get(point_id))
            reason_code, reason = _POINT_UNREACHABLE_REASON_MESSAGES.get(
                reason_key,
                ("safe_access_unavailable", "点位没有可用的安全导航接入点"),
            )
            point_errors.append(
                {"point_id": point_id, "reason_code": reason_code, "reason": reason}
            )
        suggestion = "请取消这些失败点，或修复其导航接入后重新解析图纸"
    elif code == "selected_targets_cross_floor":
        message = "所选巡检对象分属不同楼层或建筑区域"
        target_floors = details.get("target_floors") or {}
        for point_id, floor_id in target_floors.items():
            point_errors.append(
                {
                    "point_id": _text(point_id),
                    "reason_code": "point_on_different_internal_floor",
                    "reason": "点位所属内部楼层/建筑区域为 {}".format(_text(floor_id)),
                }
            )
        public_details["floor_ids"] = [
            _text(value) for value in details.get("floor_ids", []) or [] if _text(value)
        ]
        suggestion = "请按内部楼层或建筑区域分别生成路线"
    elif code == "floor_missing_from_physical_graph":
        message = "当前楼层没有可用的安全导航图"
        public_details["floor_id"] = _text(details.get("floor_id"))
        suggestion = "请重新解析图纸或检查该楼层的导航图产物"
    elif code == "selected_target_missing_safe_access":
        message = "部分巡检对象没有安全导航接入点"
        add_points(
            details.get("target_ids"),
            "safe_access_node_missing",
            "点位没有可用的安全物理图接入节点",
        )
        suggestion = "请取消失败点，或修复点位附近的可通行区域后重新解析"
    elif code == "selected_targets_physically_disconnected":
        message = "所选巡检对象不在同一安全连通区域"
        add_points(
            details.get("unreachable_target_ids"),
            "physically_disconnected",
            "点位与参考点之间不存在不穿越障碍物的安全路径",
        )
        public_details.update(
            {
                "floor_id": _text(details.get("floor_id")),
                "reference_point_id": _text(details.get("reachable_from")),
            }
        )
        suggestion = "请按连通区域分别生成路线，或检查门洞、走廊和障碍物识别"
    elif code == "start_cannot_reach_all_selected_targets":
        message = "路线起点无法到达部分巡检对象"
        add_points(
            details.get("unreachable_target_ids"),
            "unreachable_from_route_start",
            "点位无法从当前路线起点通过安全导航图到达",
        )
        public_details["start_point_id"] = _text(details.get("start_target_id"))
        suggestion = "请调整所选点位，或检查起点与目标之间的通道"
    elif code == "selected_target_coverage_failed":
        message = "部分巡检对象未能加入生成路线"
        add_points(
            details.get("missing_target_ids"),
            "not_covered_by_route",
            "点位未被最终路线顺序覆盖",
        )
    elif code == "physical_forwarding_failed":
        message = "安全物理路径展开失败"
        floor_result = details.get("floor_result") or {}
        if isinstance(floor_result, Mapping):
            add_points(
                floor_result.get("unavailable_target_ids"),
                "physical_path_expansion_failed",
                "点位之间无法展开为连续安全物理路径",
            )
        suggestion = "请减少所选点位，或检查导航图的连通性"
    elif code == "missing_shortest_path_predecessor":
        message = "无法还原巡检对象之间的安全路径"
        suggestion = "请检查导航图连通性后重新生成"
    elif code == "route_request_invalid" or isinstance(exc, RouteSelectionError):
        message = str(exc)
        suggestion = "请按接口文档检查 cad_id、route_id 和 point_id"
    elif isinstance(exc, KeyError) and "CAD 任务" in str(exc):
        code = "cad_task_not_found"
        message = "CAD 任务不存在"
        suggestion = "请确认 cad_id 正确"
    elif isinstance(exc, RuntimeError) and "尚未生成" in str(exc):
        code = "cad_task_not_ready"
        message = "图纸尚未解析完成，暂时不能重新规划路线"
        suggestion = "请等待图纸状态为解析完成后再提交"

    if route_id:
        public_details["route_id"] = route_id
    public_details["point_errors"] = point_errors
    public_details["suggestion"] = suggestion
    return {
        "msg": message,
        "result": False,
        "cad_id": cad_id,
        "route": [],
        "error_code": code,
        "error_details": public_details,
    }


def _parse_selected_target_ids(value: Any) -> List[str]:
    if isinstance(value, str):
        values: Iterable[Any] = re.split(r"[,，;；\s]+", value)
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise ValueError("cad_points 必须是逗号分隔字符串或点位 ID 数组")
    result: List[str] = []
    seen = set()
    for item in values:
        target_id = _text(item)
        if target_id and target_id not in seen:
            seen.add(target_id)
            result.append(target_id)
    if not result:
        raise ValueError("cad_points 至少需要一个点位 ID")
    return result


def _parse_route_selection_requests(value: Any) -> List[RouteSelectionRequest]:
    """解析 Excel 约定的 ``cad_points`` 分楼层数组。"""

    if not isinstance(value, list):
        raise RouteSelectionError(
            "cad_points 必须是包含 point_id 和 route_id 的数组",
            code="route_request_invalid",
        )
    requests: List[RouteSelectionRequest] = []
    seen_route_ids = set()
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise RouteSelectionError(
                "cad_points 第 {} 项必须是 JSON 对象".format(index),
                code="route_request_invalid",
            )
        route_id = _text(item.get("route_id"))
        if not route_id:
            raise RouteSelectionError(
                "cad_points 第 {} 项缺少 route_id".format(index),
                code="route_request_invalid",
            )
        if route_id in seen_route_ids:
            raise RouteSelectionError(
                "cad_points 中 route_id 重复: {}".format(route_id),
                code="route_request_invalid",
                details={"route_id": route_id},
            )
        try:
            point_ids = tuple(_parse_selected_target_ids(item.get("point_id")))
        except ValueError as exc:
            raise RouteSelectionError(
                "cad_points 第 {} 项的 point_id 无效：{}".format(index, exc),
                code="route_request_invalid",
                details={"route_id": route_id},
            ) from exc
        seen_route_ids.add(route_id)
        requests.append(RouteSelectionRequest(route_id=route_id, point_ids=point_ids))
    if not requests:
        raise RouteSelectionError(
            "cad_points 至少需要一个楼层路线选择",
            code="route_request_invalid",
        )
    return requests


def build_replanned_route_payload(
    cad_id: str,
    run_dir: Path,
    selected_target_ids: str | Sequence[str],
    max_image_side: int = 2048,
    write_dxf: bool = False,
) -> ReplannedRoutePayload:
    """按最大物理连通组拆分用户选点，并构建一张合并路线图。"""

    from fire_inspection_system.selected_route_replanner import replan_selected_targets

    cad_id = _text(cad_id)
    if not cad_id:
        raise ValueError("id 不能为空")
    requested_ids = _parse_selected_target_ids(selected_target_ids)
    run = Path(run_dir).expanduser().resolve()
    request_digest = hashlib.sha256(
        (cad_id + "\0" + "\0".join(requested_ids) + "\0" + str(time.time_ns())).encode("utf-8")
    ).hexdigest()[:12]
    output_dir = (
        run
        / "path_planning"
        / "user_selected_routes"
        / "{}_{}".format(time.strftime("%Y%m%d_%H%M%S"), request_digest)
    )
    output_dir.mkdir(parents=True, exist_ok=False)

    # 一个前端楼层可能在安全物理图中被划分为多个建筑区域或多个连通
    # 分量。单次规划会返回其中包含选中对象最多的一组；继续对剩余的可达
    # 对象规划，直到所有最大连通组都完成。连通分量之间没有安全边，因此
    # 每个分量至少需要一条路线，这个拆分天然给出覆盖全部可达对象的最少
    # 路线数量。不存在安全接入节点的对象仍作为真正的不可达对象返回。
    retryable_group_reasons = {
        "point_on_different_internal_floor",
        "physically_disconnected",
        "unreachable_from_route_start",
    }
    remaining_ids = list(requested_ids)
    group_results = []
    final_unreachable_reasons: Dict[str, str] = {}
    while remaining_ids:
        group_index = len(group_results) + 1
        try:
            group_result = replan_selected_targets(
                run,
                remaining_ids,
                output_dir=output_dir / "group_{:03d}".format(group_index),
                write_dxf=write_dxf,
            )
        except Exception:
            # 如果尚未生成任何组，保留原异常语义；如果前面已有成功路线，
            # 后续异常也不能被静默吞掉，否则会漏掉用户选择的对象。
            raise

        group_results.append(group_result)
        planned_set = set(group_result.planned_target_ids)
        reasons = dict(group_result.unreachable_target_reasons)
        next_remaining: List[str] = []
        for target_id in remaining_ids:
            if target_id in planned_set:
                continue
            reason = _text(reasons.get(target_id)) or "safe_access_unavailable"
            if reason in retryable_group_reasons:
                next_remaining.append(target_id)
            else:
                final_unreachable_reasons[target_id] = reason
        if not planned_set:
            raise RuntimeError("重规划没有覆盖任何用户选择的巡检对象")
        if next_remaining == remaining_ids:
            raise RuntimeError("重规划分组没有取得进展")
        remaining_ids = next_remaining

    physical_floor_ids = {
        _physical_floor_id(result.floor_id) for result in group_results
    }
    if len(physical_floor_ids) != 1:
        raise RouteSelectionError(
            "同一 route_id 的点位不属于同一物理楼层",
            code="selected_points_wrong_physical_floor",
            details={"physical_floor_ids": sorted(physical_floor_ids)},
        )
    physical_floor_id = next(iter(physical_floor_ids))
    records = load_render_records(run)
    if physical_floor_id not in records:
        raise FileNotFoundError("缺少重规划楼层的底图: {}".format(physical_floor_id))
    target_index = {_text(row.get("target_id")): row for row in _load_targets(run)}
    rendered_ids: List[str] = []
    for result in group_results:
        for target_id in result.planned_target_ids:
            if target_id not in rendered_ids:
                rendered_ids.append(target_id)
        if result.start_target_id not in rendered_ids:
            rendered_ids.append(result.start_target_id)
    group_index_by_target: Dict[str, int] = {}
    wall_touch_target_ids = set()
    group_start_ids = set()
    for group_index, result in enumerate(group_results, start=1):
        group_start_ids.add(result.start_target_id)
        wall_touch_target_ids.update(result.wall_touch_targets)
        for target_id in result.planned_target_ids:
            group_index_by_target[target_id] = group_index
    render_targets: List[Dict[str, Any]] = []
    for target_id in rendered_ids:
        if target_id not in target_index:
            continue
        render_target = dict(target_index[target_id])
        render_target["interface_route_group_index"] = group_index_by_target.get(
            target_id, 1
        )
        render_target["interface_route_group_start"] = target_id in group_start_ids
        render_target["interface_wall_touch"] = target_id in wall_touch_target_ids
        render_targets.append(render_target)
    route_features: List[Dict[str, Any]] = []
    for group_index, result in enumerate(group_results, start=1):
        for feature in _load_geojson_features(result.forwarding_route_geojson):
            tagged_feature = dict(feature)
            tagged_properties = dict(tagged_feature.get("properties") or {})
            tagged_properties["interface_route_group_index"] = group_index
            tagged_feature["properties"] = tagged_properties
            route_features.append(tagged_feature)
    first_result = group_results[0]
    _thumbnail_path, route_path = _render_transport_images(
        run,
        records[physical_floor_id],
        render_targets,
        route_features,
        max_image_side,
        output_dir=output_dir / "transport",
        start_target_id=first_result.start_target_id,
        route_color="#1565c0",
    )
    route_groups_manifest_path = output_dir / "route_groups_manifest.json"
    _atomic_write_json(
        route_groups_manifest_path,
        {
            "route_group_count": len(group_results),
            "groups": [
                {
                    "group_index": index,
                    "floor_id": result.floor_id,
                    "planned_target_ids": list(result.planned_target_ids),
                    "ordered_target_ids": list(result.ordered_target_ids),
                    "start_target_id": result.start_target_id,
                    "start_mode": result.start_mode,
                    "wall_touch_targets": list(result.wall_touch_targets),
                    "route_validation_path": str(result.route_validation_path),
                    "forwarding_route_geojson": str(result.forwarding_route_geojson),
                }
                for index, result in enumerate(group_results, start=1)
            ],
            "unreachable_target_reasons": final_unreachable_reasons,
            "wall_touch_targets": sorted(wall_touch_target_ids),
        },
    )
    planned_target_ids = tuple(
        target_id
        for target_id in requested_ids
        if any(target_id in result.planned_target_ids for result in group_results)
    )
    ordered_target_ids = tuple(
        target_id
        for result in group_results
        for target_id in result.ordered_target_ids
    )
    unreachable_target_reasons = tuple(
        (target_id, final_unreachable_reasons[target_id])
        for target_id in requested_ids
        if target_id in final_unreachable_reasons
    )
    payload = {
        "route_id": physical_floor_id,
        "route_url": image_to_base64(route_path),
        "cad_id": cad_id,
    }
    _atomic_write_json(
        output_dir / "interface3_response_manifest.json",
        {
            "request": {"id": cad_id, "cad_points": requested_ids},
            "response_without_base64": {
                "route_id": physical_floor_id,
                "route_base64_length": len(payload["route_url"]),
                "cad_id": cad_id,
            },
            "requested_target_ids": requested_ids,
            "planned_target_ids": list(planned_target_ids),
            "ordered_target_ids": list(ordered_target_ids),
            "unreachable_target_reasons": {
                target_id: reason
                for target_id, reason in unreachable_target_reasons
            },
            "wall_touch_targets": sorted(wall_touch_target_ids),
            "route_group_count": len(group_results),
            "route_groups_manifest_path": str(route_groups_manifest_path),
            "start_target_id": first_result.start_target_id,
            "start_mode": first_result.start_mode,
            "route_path": str(route_path),
            "route_validation_path": str(route_groups_manifest_path),
        },
    )
    return ReplannedRoutePayload(
        floor_id=physical_floor_id,
        payload=payload,
        route_path=route_path,
        output_dir=output_dir,
        requested_target_ids=tuple(requested_ids),
        planned_target_ids=planned_target_ids,
        ordered_target_ids=ordered_target_ids,
        unreachable_target_reasons=unreachable_target_reasons,
        wall_touch_targets=tuple(
            target_id for target_id in requested_ids if target_id in wall_touch_target_ids
        ),
        start_target_id=first_result.start_target_id,
        start_mode=first_result.start_mode,
        route_group_count=len(group_results),
        route_validation_path=route_groups_manifest_path,
    )


def build_cad_result_payloads(
    cad_id: str,
    run_dir: Path,
    max_image_side: int = 2048,
) -> List[CadResultPayload]:
    """从一次流水线产物构建接口二的全部物理楼层数据。"""

    cad_id = _text(cad_id)
    if not cad_id:
        raise ValueError("cad_id 不能为空")
    run_dir = run_dir.expanduser().resolve()
    if not (run_dir / "pipeline_summary.json").is_file():
        raise FileNotFoundError("缺少 pipeline_summary.json: {}".format(run_dir))

    records = load_render_records(run_dir)
    if not records:
        raise FileNotFoundError("没有可用于接口二的楼层障碍物图片或渲染数据")
    targets = _load_targets(run_dir)
    if not targets:
        raise FileNotFoundError("没有巡检对象点位: navigation_targets.geojson")
    route_features = _route_features(run_dir)
    mandatory = _mandatory_by_target(run_dir)
    coordinate_rejections: List[Dict[str, Any]] = []
    coordinate_stats: Dict[str, Dict[str, Any]] = {}

    targets_by_scope: Dict[str, List[Dict[str, Any]]] = {}
    for target in targets:
        floor_id = _text(target.get("source_floor_id")) or _physical_floor_id(
            target.get("floor_id")
        )
        if not floor_id:
            continue
        scope_id = _text(target.get("building_scope_id"))
        for target_scope in {floor_id, scope_id} - {""}:
            targets_by_scope.setdefault(target_scope, []).append(target)

    results: List[CadResultPayload] = []
    for floor_id in sorted(records):
        floor_targets = targets_by_scope.get(floor_id, [])
        record = records[floor_id]
        structure_id = _floor_resource_id(cad_id, floor_id, "structure")
        route_id = _floor_resource_id(cad_id, floor_id, "route")
        source_image_size = _image_size(record.image_path)
        manifest_size = (record.image_width, record.image_height)
        if source_image_size != manifest_size:
            raise ValueError(
                "坐标变换清单与源图片实际尺寸不一致，不能生成可靠坐标: "
                "scope={}, manifest={}, source_image={}".format(
                    floor_id,
                    manifest_size,
                    source_image_size,
                )
            )
        if record.route_image_path is not None:
            thumbnail_path = record.image_path
            route_path = record.route_image_path
            thumbnail_size = _image_size(thumbnail_path)
            route_size = _image_size(route_path)
            if thumbnail_size != manifest_size or route_size != manifest_size:
                raise ValueError(
                    "业务图片实际尺寸与坐标变换清单不一致，不能生成前端坐标: "
                    "scope={}, manifest={}, thumbnail={}, route={}".format(
                        floor_id,
                        manifest_size,
                        thumbnail_size,
                        route_size,
                    )
                )
        else:
            thumbnail_path, route_path = _render_transport_images(
                run_dir,
                record,
                floor_targets,
                route_features,
                max_image_side,
            )
            thumbnail_size = _image_size(thumbnail_path)
            route_size = _image_size(route_path)
            if thumbnail_size != route_size:
                raise ValueError(
                    "缩略图与路线图尺寸不一致，单组 x/y 无法同时对应两张图片: "
                    "scope={}, thumbnail={}, route={}".format(
                        floor_id,
                        thumbnail_size,
                        route_size,
                    )
                )
        cad_points: List[Dict[str, Any]] = []
        seen_ids = set()
        floor_coordinate_stats = coordinate_stats.setdefault(
            floor_id,
            {
                "source_point_count": 0,
                "transmitted_point_count": 0,
                "rejected_point_count": 0,
                "transmitted_image_width": thumbnail_size[0],
                "transmitted_image_height": thumbnail_size[1],
            },
        )
        for target in sorted(floor_targets, key=lambda row: _text(row.get("target_id"))):
            point_id = _text(target.get("target_id"))
            if not point_id or point_id in seen_ids:
                continue
            seen_ids.add(point_id)
            floor_coordinate_stats["source_point_count"] += 1
            cad_x = float(target["cad_x"])
            cad_y = float(target["cad_y"])
            try:
                x_percent, y_percent = record.cad_to_percent(cad_x, cad_y)
            except ValueError as exc:
                floor_coordinate_stats["rejected_point_count"] += 1
                rejected = {
                    "point_id": point_id,
                    "scope_id": floor_id,
                    "source_floor_id": _text(target.get("source_floor_id")),
                    "building_scope_id": _text(target.get("building_scope_id")),
                    "cad_x": cad_x,
                    "cad_y": cad_y,
                    "cad_bbox": list(record.cad_bbox),
                    "reason": str(exc),
                }
                coordinate_rejections.append(rejected)
                LOGGER.warning(
                    "巡检对象没有对应的图片内显示坐标，已记录并将停止接口二回调: %s",
                    rejected,
                )
                continue
            point = {
                "id": point_id,
                "mainId": structure_id,
                # 接口文档要求 name 为巡检对象分类名称，而不是 CAD 图块编号。
                "name": _text(target.get("target_class")),
                "point_type": _point_type(target.get("target_class")),
                "x": x_percent,
                "y": y_percent,
                "is_must_check": bool(mandatory.get(point_id, False)),
                # 可选业务值缺失时也保留字段，JSON 中发送 null。
                "confidence": _optional_confidence(target.get("confidence")),
                "spec": _optional_spec(target) or None,
            }
            cad_points.append(point)
            floor_coordinate_stats["transmitted_point_count"] += 1

        building_names: List[str] = []
        floor_names: List[str] = []
        for target in floor_targets:
            building_name = _text(target.get("building_name")) or _text(
                target.get("building_id")
            )
            floor_name = _text(target.get("floor_name"))
            if building_name and building_name not in building_names:
                building_names.append(building_name)
            if floor_name and floor_name not in floor_names:
                floor_names.append(floor_name)

        payload = {
            "cad_id": cad_id,
            "thumbnail_url": image_to_base64(thumbnail_path),
            # 对接表沿用 buliding_name 拼写，接口必须与其完全一致。
            "buliding_name": "、".join(building_names),
            "floor": "、".join(floor_names) or _physical_floor_id(floor_id),
            "id": structure_id,
            "cad_points": cad_points,
            "route_id": route_id,
            "route_url": image_to_base64(route_path),
        }
        validation_image_path = _write_coordinate_validation_image(
            thumbnail_path,
            cad_points,
            run_dir
            / "backend_interface_images"
            / "coordinate_validation"
            / "{}_coordinate_check.png".format(_safe_component(floor_id)),
        )
        floor_coordinate_stats["validation_image_path"] = str(
            validation_image_path
        )
        results.append(
            CadResultPayload(
                floor_id=floor_id,
                payload=payload,
                thumbnail_path=thumbnail_path,
                route_path=route_path,
            )
        )

    coordinate_audit_path = (
        run_dir / "backend_interface_images" / "coordinate_validation.json"
    )
    _atomic_write_json(
        coordinate_audit_path,
        {
            "schema_version": 1,
            "coordinate_contract": {
                "fields": ["x", "y"],
                "unit": "percent_0_to_100_of_full_transmitted_image",
                "origin": "top_left",
                "cad_y_axis_inverted_for_image": True,
                "out_of_image_policy": "fail_callback_instead_of_clamp",
            },
            "scope_stats": coordinate_stats,
            "source_point_count": sum(
                row["source_point_count"] for row in coordinate_stats.values()
            ),
            "transmitted_point_count": sum(
                row["transmitted_point_count"] for row in coordinate_stats.values()
            ),
            "rejected_point_count": len(coordinate_rejections),
            "rejected_points": coordinate_rejections,
        },
    )
    if coordinate_rejections:
        raise ValueError(
            "有 {} 个巡检对象不在对应传输图片范围内，已停止接口二回调；"
            "详见 {}".format(len(coordinate_rejections), coordinate_audit_path)
        )
    if not results:
        raise ValueError("渲染楼层与巡检对象楼层无法匹配")
    return results


def _cad_result_array(built: Sequence[CadResultPayload]) -> List[Dict[str, Any]]:
    payload = [item.payload for item in built]
    structure_ids = set()
    route_ids = set()
    for floor in payload:
        required = {
            "cad_id",
            "thumbnail_url",
            "buliding_name",
            "floor",
            "id",
            "cad_points",
            "route_id",
            "route_url",
        }
        missing = required - set(floor)
        if missing:
            raise ValueError("接口二楼层数据缺少字段: {}".format(sorted(missing)))
        structure_id = _text(floor.get("id"))
        route_id = _text(floor.get("route_id"))
        if not structure_id or structure_id in structure_ids:
            raise ValueError("接口二结构图 id 为空或重复: {}".format(structure_id))
        if not route_id or route_id in route_ids:
            raise ValueError("接口二路线图 route_id 为空或重复: {}".format(route_id))
        structure_ids.add(structure_id)
        route_ids.add(route_id)
        points = floor.get("cad_points")
        if not isinstance(points, list):
            raise ValueError("接口二 cad_points 必须直属结构图且为数组")
        for point in points:
            if not isinstance(point, dict) or _text(point.get("mainId")) != structure_id:
                raise ValueError(
                    "巡检对象 mainId 必须等于所属结构图 id: {}".format(
                        _text(point.get("id")) if isinstance(point, dict) else "<非对象>"
                    )
                )
            required_point_fields = {
                "id",
                "mainId",
                "name",
                "point_type",
                "x",
                "y",
                "is_must_check",
                "confidence",
                "spec",
            }
            missing_point_fields = required_point_fields - set(point)
            if missing_point_fields:
                raise ValueError(
                    "接口二巡检对象缺少字段 {}: {}".format(
                        sorted(missing_point_fields), _text(point.get("id"))
                    )
                )
    return payload


def save_cad_results(path: Path, built: Sequence[CadResultPayload]) -> Path:
    """把接口二将要发送的完整数组原样保存到本地。"""

    resolved = path.expanduser().resolve()
    _atomic_write_json(resolved, _cad_result_array(built))
    return resolved


def send_cad_results(
    callback_url: str,
    cad_id: str,
    run_dir: Path,
    token: str = "",
    timeout: int = 120,
    retries: int = 3,
    max_image_side: int = 2048,
    result_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """保存并一次发送接口二完整数组；失败只重试发送，不重跑流水线。"""

    run_dir = run_dir.expanduser().resolve()
    built = build_cad_result_payloads(cad_id, run_dir, max_image_side=max_image_side)
    local_path = save_cad_results(
        result_path or (run_dir.parent / "interface2_result.json"), built
    )
    payload = _cad_result_array(built)
    last_error: Optional[Exception] = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            response = post_json(callback_url, payload, timeout=timeout, token=token)
            return {
                "floor_count": len(payload),
                "point_count": sum(len(row["cad_points"]) for row in payload),
                "attempt": attempt,
                "local_result_path": str(local_path),
                "response": response,
            }
        except Exception as exc:  # 网络异常需记录原始异常类型和消息
            last_error = exc
            if attempt < max(1, retries):
                time.sleep(min(2 ** (attempt - 1), 8))
    raise RuntimeError(
        "接口二完整数组回传失败: {}: {}；本地结果已保留在 {}".format(
            type(last_error).__name__, last_error, local_path
        )
    ) from last_error


@dataclass
class BridgeConfig:
    job_root: Path
    callback_url: str = ""
    route_callback_url: str = ""
    callback_token: str = ""
    inbound_token: str = ""
    allowed_download_hosts: Tuple[str, ...] = ()
    download_timeout: int = 60
    callback_timeout: int = 120
    max_download_bytes: int = 1024 * 1024 * 1024
    max_image_side: int = 2048
    callback_retries: int = 3
    worker_count: int = 1
    heartbeat_interval: float = 5.0


class CadBackendBridge:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.config.job_root.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, config.worker_count),
            thread_name_prefix="cad-backend-job",
        )
        self._lock = threading.Lock()

    def _job_dir(self, cad_id: str) -> Path:
        return (self.config.job_root / _safe_component(cad_id)).resolve()

    def _state_path(self, cad_id: str) -> Path:
        return self._job_dir(cad_id) / "job_state.json"

    def _read_state_raw(self, cad_id: str) -> Dict[str, Any]:
        path = self._state_path(cad_id)
        if not path.is_file():
            return {"id": cad_id, "status": "not_found"}
        return _read_json(path)

    def read_state(self, cad_id: str) -> Dict[str, Any]:
        """返回持久化状态，并补充便于前端判断的动态运行字段。"""

        state = self._read_state_raw(cad_id)
        status = _text(state.get("status"))
        if status == "not_found":
            return state
        now = time.time()
        created_at = _float(state.get("created_at_epoch"), now)
        finished_at = _float(state.get("finished_at_epoch"), 0.0)
        heartbeat_at = _float(
            state.get("heartbeat_at_epoch"),
            _float(state.get("updated_at_epoch"), created_at),
        )
        terminal = status in TERMINAL_JOB_STATUSES
        state["terminal"] = terminal
        state["running"] = not terminal
        state["success"] = status in SUCCESS_JOB_STATUSES if terminal else None
        state["elapsed_seconds"] = round(
            max(0.0, (finished_at or now) - created_at), 3
        )
        state["heartbeat_age_seconds"] = round(max(0.0, now - heartbeat_at), 3)
        stale_after = max(30.0, _float(state.get("stale_after_seconds"), 120.0))
        state["stale_after_seconds"] = stale_after
        state["stale"] = bool(
            status in HEARTBEAT_JOB_STATUSES
            and state["heartbeat_age_seconds"] > stale_after
        )
        return state

    @staticmethod
    def _public_status_name(state: Mapping[str, Any]) -> str:
        """把内部细分阶段折叠成前端约定的两个处理中状态。"""

        status = _text(state.get("status"))
        if status == "failed":
            return "解析失败"
        if status in SUCCESS_JOB_STATUSES:
            return "解析完成"
        if status == "not_found":
            return "任务不存在"
        if status == "running_pipeline":
            stage_index = int(_float(state.get("stage_index"), 0.0))
            if stage_index >= 6:
                return "巡检路径生成"
        if status in {"building_callback_payloads", "sending_callback"}:
            return "巡检路径生成"
        return "图纸巡检对象识别"

    def public_state(self, cad_id: str) -> Dict[str, Any]:
        """构造状态接口响应，不暴露内部阶段、日志路径和失败原因。"""

        state = self.read_state(cad_id)
        if state.get("status") == "not_found":
            return {"id": cad_id, "status": "not_found", "message": "任务不存在"}

        public_status = self._public_status_name(state)
        payload: Dict[str, Any] = {
            "id": cad_id,
            "status": public_status,
            "message": public_status,
            "progress_percent": int(_float(state.get("progress_percent"), 0.0)),
            "running": bool(state.get("running")),
            "terminal": bool(state.get("terminal")),
            "success": state.get("success"),
            "stale": bool(state.get("stale")),
            "created_at_epoch": state.get("created_at_epoch"),
            "updated_at_epoch": state.get("updated_at_epoch"),
            "elapsed_seconds": state.get("elapsed_seconds"),
        }
        return payload

    def _write_state(self, cad_id: str, **values: Any) -> None:
        with self._lock:
            current = self._read_state_raw(cad_id)
            current.update(values)
            current["id"] = cad_id
            current["updated_at_epoch"] = time.time()
            _atomic_write_json(self._state_path(cad_id), current)

    def replan(
        self, cad_id: str, selected_target_ids: str | Sequence[str]
    ) -> ReplannedRoutePayload:
        cad_id = _text(cad_id)
        if not cad_id:
            raise ValueError("id 不能为空")
        state = self.read_state(cad_id)
        if state.get("status") == "not_found":
            raise KeyError("未找到 CAD 任务: {}".format(cad_id))
        run_dir = Path(_text(state.get("run_dir")))
        if not run_dir.is_dir() or not (run_dir / "pipeline_summary.json").is_file():
            raise RuntimeError("CAD 任务尚未生成可重规划的流水线结果")
        return build_replanned_route_payload(
            cad_id,
            run_dir,
            selected_target_ids,
            max_image_side=self.config.max_image_side,
            write_dxf=False,
        )

    def _validate_route_selections(
        self,
        cad_id: str,
        run_dir: Path,
        selections: Sequence[RouteSelectionRequest],
    ) -> None:
        records = load_render_records(run_dir)
        route_floor_by_id = {
            _floor_resource_id(cad_id, floor_id, "route"): _physical_floor_id(floor_id)
            for floor_id in records
        }
        target_floor_by_id: Dict[str, str] = {}
        for target in _load_targets(run_dir):
            target_id = _text(target.get("target_id"))
            floor_id = _text(target.get("source_floor_id")) or _physical_floor_id(
                target.get("floor_id")
            )
            if target_id and floor_id:
                target_floor_by_id[target_id] = _physical_floor_id(floor_id)

        missing_ids: List[str] = []
        wrong_floor_ids: List[str] = []
        expected_floors: Dict[str, str] = {}
        actual_floors: Dict[str, str] = {}
        point_route_ids: Dict[str, List[str]] = {}

        for selection in selections:
            expected_floor = route_floor_by_id.get(selection.route_id)
            if not expected_floor:
                raise RouteSelectionError(
                    "route_id 不属于当前 CAD: {}".format(selection.route_id),
                    code="route_id_not_current_cad",
                    details={"route_id": selection.route_id},
                )
            expected_floors[selection.route_id] = expected_floor
            for point_id in selection.point_ids:
                actual_floor = target_floor_by_id.get(point_id)
                if actual_floor is None:
                    if point_id not in missing_ids:
                        missing_ids.append(point_id)
                elif actual_floor != expected_floor:
                    if point_id not in wrong_floor_ids:
                        wrong_floor_ids.append(point_id)
                    actual_floors[point_id] = actual_floor
                else:
                    continue
                route_ids = point_route_ids.setdefault(point_id, [])
                if selection.route_id not in route_ids:
                    route_ids.append(selection.route_id)

        affected_route_ids = list(
            dict.fromkeys(
                route_id
                for point_id in missing_ids + wrong_floor_ids
                for route_id in point_route_ids.get(point_id, [])
            )
        )
        if missing_ids and wrong_floor_ids:
            raise RouteSelectionError(
                "请求同时包含未知点位和其他楼层点位",
                code="selected_points_invalid",
                details={
                    "route_ids": affected_route_ids,
                    "missing_target_ids": missing_ids,
                    "wrong_floor_target_ids": wrong_floor_ids,
                    "expected_floors": {
                        route_id: expected_floors[route_id]
                        for route_id in affected_route_ids
                    },
                    "actual_floors": actual_floors,
                    "point_route_ids": point_route_ids,
                },
            )
        if missing_ids:
            raise RouteSelectionError(
                "请求包含未知点位: {}".format(", ".join(missing_ids)),
                code="selected_points_unknown",
                details={
                    "route_id": affected_route_ids[0] if len(affected_route_ids) == 1 else "",
                    "target_ids": missing_ids,
                },
            )
        if wrong_floor_ids:
            unique_expected_floors = list(
                dict.fromkeys(expected_floors[route_id] for route_id in affected_route_ids)
            )
            raise RouteSelectionError(
                "请求包含其他楼层点位: {}".format(", ".join(wrong_floor_ids)),
                code="selected_points_wrong_physical_floor",
                details={
                    "route_id": affected_route_ids[0] if len(affected_route_ids) == 1 else "",
                    "target_ids": wrong_floor_ids,
                    "expected_floor_id": (
                        unique_expected_floors[0]
                        if len(unique_expected_floors) == 1
                        else ""
                    ),
                    "actual_floors": actual_floors,
                },
            )

    @staticmethod
    def _write_route_job_state(route_job_dir: Path, **values: Any) -> Path:
        state_path = route_job_dir / "route_job_state.json"
        current = _read_json(state_path) if state_path.is_file() else {}
        current.update(values)
        current["updated_at_epoch"] = time.time()
        _atomic_write_json(state_path, current)
        return state_path

    def generate_routes_sync(
        self,
        cad_id: str,
        selections: Sequence[RouteSelectionRequest],
    ) -> Dict[str, Any]:
        """同步完成用户选点重规划，并直接构造本次 HTTP 响应。"""

        cad_id = _text(cad_id)
        if not cad_id:
            raise RouteSelectionError(
                "cad_id 不能为空",
                code="route_request_invalid",
            )
        if not selections:
            raise RouteSelectionError(
                "cad_points 至少需要一个楼层路线选择",
                code="route_request_invalid",
            )
        state = self.read_state(cad_id)
        if state.get("status") == "not_found":
            raise KeyError("未找到 CAD 任务: {}".format(cad_id))
        run_dir = Path(_text(state.get("run_dir")))
        if not run_dir.is_dir() or not (run_dir / "pipeline_summary.json").is_file():
            raise RuntimeError("CAD 任务尚未生成可重规划的流水线结果")
        self._validate_route_selections(cad_id, run_dir, selections)

        serialized = [
            {"route_id": row.route_id, "point_id": list(row.point_ids)}
            for row in selections
        ]
        request_digest = hashlib.sha256(
            (
                cad_id
                + "\0"
                + json.dumps(serialized, ensure_ascii=False, sort_keys=True)
                + "\0"
                + str(time.time_ns())
            ).encode("utf-8")
        ).hexdigest()[:12]
        request_id = "sync_{}_{}".format(
            time.strftime("%Y%m%d_%H%M%S"), request_digest
        )
        route_job_dir = self._job_dir(cad_id) / "route_jobs" / request_id
        route_job_dir.mkdir(parents=True, exist_ok=False)
        _atomic_write_json(
            route_job_dir / "route_request.json",
            {"cad_id": cad_id, "cad_points": serialized},
        )
        self._write_route_job_state(
            route_job_dir,
            request_id=request_id,
            cad_id=cad_id,
            status="generating_routes",
            mode="synchronous_http_response",
            created_at_epoch=time.time(),
            route_count=len(selections),
        )

        current_route_id = ""
        try:
            route_rows: List[Dict[str, Any]] = []
            route_manifests: List[Dict[str, Any]] = []
            partial_point_errors: List[Dict[str, str]] = []
            first_skipped_exception: Optional[BaseException] = None
            recoverable_route_codes = {
                "selected_target_not_user_selectable",
                "selected_targets_cross_floor",
                "floor_missing_from_physical_graph",
                "selected_target_missing_safe_access",
                "selected_targets_physically_disconnected",
                "start_cannot_reach_all_selected_targets",
                "selected_target_coverage_failed",
                "physical_forwarding_failed",
                "missing_shortest_path_predecessor",
            }
            for index, selection in enumerate(selections, start=1):
                current_route_id = selection.route_id
                try:
                    replanned = build_replanned_route_payload(
                        cad_id,
                        run_dir,
                        selection.point_ids,
                        max_image_side=self.config.max_image_side,
                        write_dxf=False,
                    )
                except Exception as route_exc:
                    route_error_code = _text(getattr(route_exc, "code", ""))
                    if route_error_code not in recoverable_route_codes:
                        raise
                    try:
                        setattr(route_exc, "route_id", selection.route_id)
                    except Exception:
                        pass
                    if first_skipped_exception is None:
                        first_skipped_exception = route_exc
                    failure = _route_failure_response(cad_id, route_exc)
                    failure_details = failure.get("error_details") or {}
                    partial_point_errors.extend(
                        list(failure_details.get("point_errors") or [])
                    )
                    route_manifests.append(
                        {
                            "index": index,
                            "route_id": selection.route_id,
                            "point_ids": list(selection.point_ids),
                            "status": "skipped_no_reachable_route",
                            "error_code": failure.get("error_code"),
                            "error_details": failure_details,
                        }
                    )
                    continue
                route_url = image_to_base64(replanned.route_path)
                route_rows.append(
                    {"route_id": selection.route_id, "route_url": route_url}
                )
                partial_point_errors.extend(
                    _point_errors_from_unreachable_reasons(
                        replanned.unreachable_target_reasons
                    )
                )
                route_manifests.append(
                    {
                        "index": index,
                        "route_id": selection.route_id,
                        "floor_id": replanned.floor_id,
                        "point_ids": list(selection.point_ids),
                        "planned_target_ids": list(replanned.planned_target_ids),
                        "unreachable_target_reasons": {
                            target_id: reason
                            for target_id, reason in replanned.unreachable_target_reasons
                        },
                        "ordered_target_ids": list(replanned.ordered_target_ids),
                        "start_target_id": replanned.start_target_id,
                        "route_group_count": replanned.route_group_count,
                        "route_path": str(replanned.route_path),
                        "route_validation_path": str(
                            replanned.route_validation_path
                        ),
                        "route_base64_length": len(route_url),
                    }
                )

            if not route_rows and first_skipped_exception is not None:
                raise first_skipped_exception

            response_payload: Dict[str, Any] = {
                "msg": (
                    "路径生成完成，部分不可达巡检对象已跳过"
                    if partial_point_errors
                    else "路径生成完成"
                ),
                "result": True,
                "cad_id": cad_id,
                "route": route_rows,
            }
            if partial_point_errors:
                # 复用失败响应中已经存在的字段结构，使前端在继续展示路线图
                # 的同时，可以用同一套 point_errors 组件标注被排除的点。
                response_payload["error_code"] = "partial_route_generated"
                response_payload["error_details"] = {
                    "point_errors": partial_point_errors,
                    "suggestion": "已使用可达点生成路线，请在图上标记并提示不可达点",
                }
            result_path = route_job_dir / "interface3_sync_result.json"
            _atomic_write_json(result_path, response_payload)
            _atomic_write_json(
                route_job_dir / "interface3_sync_manifest.json",
                {
                    "cad_id": cad_id,
                    "request_id": request_id,
                    "mode": "synchronous_http_response",
                    "route_count": len(route_rows),
                    "routes": route_manifests,
                    "local_result_path": str(result_path),
                },
            )
            self._write_route_job_state(
                route_job_dir,
                status="completed",
                finished_at_epoch=time.time(),
                interface3_result_path=str(result_path),
            )
            return response_payload
        except Exception as exc:
            LOGGER.exception("Synchronous route generation failed: %s", cad_id)
            try:
                setattr(exc, "route_id", current_route_id)
            except Exception:
                pass
            error_code = _text(getattr(exc, "code", ""))
            raw_error_details = getattr(exc, "details", None)
            error_details = (
                dict(raw_error_details)
                if isinstance(raw_error_details, Mapping)
                else {}
            )
            self._write_route_job_state(
                route_job_dir,
                status="failed",
                finished_at_epoch=time.time(),
                error_type=type(exc).__name__,
                error=str(exc),
                error_code=error_code,
                error_details=error_details,
                failed_route_id=current_route_id,
            )
            raise

    def submit(self, cad_id: str, file_url: str) -> Tuple[bool, Dict[str, Any]]:
        cad_id = _text(cad_id)
        if not cad_id:
            raise ValueError("id 不能为空")
        file_url, parts = _normalize_cad_file_url(file_url)
        host = (parts.hostname or "").lower()
        if not host:
            raise ValueError("file_url 缺少有效域名")
        allowed = {item.lower() for item in self.config.allowed_download_hosts}
        if allowed and host not in allowed:
            raise ValueError("文件域名不在允许列表中: {}".format(host))
        with self._lock:
            existing = self.read_state(cad_id)
            status = _text(existing.get("status"))
            if status and status != "not_found":
                return False, existing
            job_dir = self._job_dir(cad_id)
            job_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(
                self._state_path(cad_id),
                {
                    "id": cad_id,
                    "file_url": file_url,
                    "status": "accepted",
                    "phase": "accepted",
                    "progress_percent": 0,
                    "message": "任务已接收，等待后台处理",
                    "created_at_epoch": time.time(),
                    "updated_at_epoch": time.time(),
                    "heartbeat_at_epoch": time.time(),
                },
            )
        self._executor.submit(self._run_job, cad_id, file_url)
        return True, self.read_state(cad_id)

    def _run_pipeline_process(
        self,
        cad_id: str,
        input_path: Path,
        run_dir: Path,
        stdout_path: Path,
        stderr_path: Path,
    ) -> Tuple[int, str]:
        """无缓冲运行主流水线，持续写日志、阶段进度和心跳。"""

        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        environment.setdefault("PYTHONUTF8", "1")
        stdout_tail: Any = deque(maxlen=30)
        stderr_tail: Any = deque(maxlen=30)
        tail_lock = threading.Lock()
        heartbeat_interval = max(1.0, float(self.config.heartbeat_interval))

        with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr_handle:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    str(PIPELINE_MAIN),
                    "--input",
                    str(input_path),
                    "--output-dir",
                    str(run_dir),
                ],
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
            )
            self._write_state(
                cad_id,
                pipeline_pid=process.pid,
                pipeline_stdout_log=str(stdout_path),
                pipeline_stderr_log=str(stderr_path),
                heartbeat_at_epoch=time.time(),
            )

            def consume_stream(
                stream: Any,
                log_handle: Any,
                tail: Any,
                parse_progress: bool,
            ) -> None:
                try:
                    for line in iter(stream.readline, ""):
                        log_handle.write(line)
                        log_handle.flush()
                        clean_line = line.rstrip("\r\n")
                        with tail_lock:
                            tail.append(clean_line)
                        if parse_progress:
                            progress = _pipeline_progress_from_line(clean_line)
                            if progress:
                                self._write_state(
                                    cad_id,
                                    heartbeat_at_epoch=time.time(),
                                    last_output=clean_line,
                                    **progress,
                                )
                finally:
                    stream.close()

            stdout_thread = threading.Thread(
                target=consume_stream,
                args=(process.stdout, stdout_handle, stdout_tail, True),
                name="cad-pipeline-stdout-{}".format(cad_id),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=consume_stream,
                args=(process.stderr, stderr_handle, stderr_tail, False),
                name="cad-pipeline-stderr-{}".format(cad_id),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()

            while process.poll() is None:
                with tail_lock:
                    last_stdout = stdout_tail[-1] if stdout_tail else ""
                    last_stderr = stderr_tail[-1] if stderr_tail else ""
                heartbeat_values: Dict[str, Any] = {
                    "heartbeat_at_epoch": time.time(),
                    "pipeline_pid": process.pid,
                }
                if last_stdout:
                    heartbeat_values["last_output"] = last_stdout
                if last_stderr:
                    heartbeat_values["last_stderr_output"] = last_stderr
                self._write_state(cad_id, **heartbeat_values)
                time.sleep(heartbeat_interval)

            return_code = int(process.wait())
            stdout_thread.join(timeout=10.0)
            stderr_thread.join(timeout=10.0)
            with tail_lock:
                last_stdout = stdout_tail[-1] if stdout_tail else ""
                error_tail = "\n".join(line for line in stderr_tail if line).strip()
            final_values: Dict[str, Any] = {
                "pipeline_returncode": return_code,
                "heartbeat_at_epoch": time.time(),
            }
            if last_stdout:
                final_values["last_output"] = last_stdout
            if error_tail:
                final_values["pipeline_error_tail"] = error_tail
            self._write_state(cad_id, **final_values)
            return return_code, error_tail

    def _run_job(self, cad_id: str, file_url: str) -> None:
        job_dir = self._job_dir(cad_id)
        try:
            self._write_state(
                cad_id,
                status="downloading",
                phase="downloading",
                progress_percent=2,
                message="正在下载 CAD 文件",
                heartbeat_at_epoch=time.time(),
            )
            input_path = get_file_from_url(
                file_url,
                job_dir / "input",
                timeout=self.config.download_timeout,
                max_bytes=self.config.max_download_bytes,
                allowed_hosts=self.config.allowed_download_hosts,
            )
            run_dir = (job_dir / "pipeline_run").resolve()
            stdout_path = job_dir / "pipeline.stdout.log"
            stderr_path = job_dir / "pipeline.stderr.log"
            self._write_state(
                cad_id,
                status="running_pipeline",
                phase="pipeline",
                progress_percent=5,
                stage_code="starting",
                stage_index=0,
                stage_total=11,
                stage_name="启动流水线",
                message="CAD 文件下载完成，正在启动识别流水线",
                input_path=str(input_path),
                run_dir=str(run_dir),
                pipeline_stdout_log=str(stdout_path),
                pipeline_stderr_log=str(stderr_path),
                heartbeat_at_epoch=time.time(),
            )
            return_code, error_tail = self._run_pipeline_process(
                cad_id,
                input_path,
                run_dir,
                stdout_path,
                stderr_path,
            )
            if return_code != 0:
                error_summary = error_tail.splitlines()[-1] if error_tail else ""
                raise RuntimeError(
                    "CAD 流水线退出码为 {}{}，详见 {}".format(
                        return_code,
                        "：{}".format(error_summary) if error_summary else "",
                        stderr_path,
                    )
                )

            self._write_state(
                cad_id,
                status="building_callback_payloads",
                phase="building_callback_payloads",
                progress_percent=92,
                message="识别完成，正在生成前端图片和点位数据",
                heartbeat_at_epoch=time.time(),
            )
            built = build_cad_result_payloads(
                cad_id,
                run_dir,
                max_image_side=self.config.max_image_side,
            )
            result_path = save_cad_results(job_dir / "interface2_result.json", built)
            manifest = {
                "cad_id": cad_id,
                "run_dir": str(run_dir),
                "base64_encoding": "raw_png_base64_without_data_uri_prefix",
                "coordinate_unit": "percent_0_to_100_of_full_image_from_top_left",
                "request_body": "single_json_array_containing_all_floors",
                "local_result_path": str(result_path),
                "floors": [
                    {
                        "floor_id": item.floor_id,
                        "structure_id": item.payload["id"],
                        "route_id": item.payload["route_id"],
                        "point_count": len(item.payload["cad_points"]),
                        "thumbnail_path": str(item.thumbnail_path),
                        "route_path": str(item.route_path),
                        "thumbnail_base64_length": len(item.payload["thumbnail_url"]),
                        "route_base64_length": len(item.payload["route_url"]),
                    }
                    for item in built
                ],
            }
            manifest_path = job_dir / "callback_payload_manifest.json"
            _atomic_write_json(manifest_path, manifest)
            if not self.config.callback_url:
                self._write_state(
                    cad_id,
                    status="result_ready_callback_url_missing",
                    phase="result_ready",
                    progress_percent=100,
                    message="处理完成，结果已保存在服务器；未配置结果回调地址",
                    finished_at_epoch=time.time(),
                    heartbeat_at_epoch=time.time(),
                    callback_payload_manifest=str(manifest_path),
                    interface2_result_path=str(result_path),
                )
                return

            self._write_state(
                cad_id,
                status="sending_callback",
                phase="sending_callback",
                progress_percent=96,
                message="处理完成，正在向业务后端回传结果",
                stale_after_seconds=max(
                    120.0, float(self.config.callback_timeout) + 30.0
                ),
                heartbeat_at_epoch=time.time(),
            )
            payload = _cad_result_array(built)
            last_error: Optional[Exception] = None
            acknowledgement: Optional[Dict[str, Any]] = None
            for attempt in range(1, max(1, self.config.callback_retries) + 1):
                self._write_state(
                    cad_id,
                    callback_attempt=attempt,
                    heartbeat_at_epoch=time.time(),
                    message="正在回传处理结果（第 {} 次尝试）".format(attempt),
                )
                try:
                    response = post_json(
                        self.config.callback_url,
                        payload,
                        timeout=self.config.callback_timeout,
                        token=self.config.callback_token,
                    )
                    acknowledgement = {
                        "floor_count": len(payload),
                        "point_count": sum(len(row["cad_points"]) for row in payload),
                        "attempt": attempt,
                        "local_result_path": str(result_path),
                        "response": response,
                    }
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    self._write_state(
                        cad_id,
                        callback_last_error="{}: {}".format(
                            type(exc).__name__, exc
                        ),
                        heartbeat_at_epoch=time.time(),
                    )
                    if attempt < max(1, self.config.callback_retries):
                        time.sleep(min(2 ** (attempt - 1), 8))
            if last_error is not None or acknowledgement is None:
                raise RuntimeError(
                    "接口二完整数组回传失败: {}: {}；本地结果已保留在 {}".format(
                        type(last_error).__name__, last_error, result_path
                    )
                ) from last_error
            self._write_state(
                cad_id,
                status="completed",
                phase="completed",
                progress_percent=100,
                message="CAD 图纸识别及结果回传完成",
                finished_at_epoch=time.time(),
                heartbeat_at_epoch=time.time(),
                callback_payload_manifest=str(manifest_path),
                interface2_result_path=str(result_path),
                callback_acknowledgement=acknowledgement,
            )
        except Exception as exc:
            LOGGER.exception("CAD job failed: %s", cad_id)
            self._write_state(
                cad_id,
                status="failed",
                phase="failed",
                message="CAD 图纸处理失败：{}".format(exc),
                finished_at_epoch=time.time(),
                heartbeat_at_epoch=time.time(),
                error_type=type(exc).__name__,
                error=str(exc),
            )


class CadApiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: Tuple[str, int], bridge: CadBackendBridge):
        super().__init__(server_address, CadRequestHandler)
        self.bridge = bridge


class CadRequestHandler(BaseHTTPRequestHandler):
    server_version = "CadInspectionBridge/1.0"

    @property
    def bridge(self) -> CadBackendBridge:
        return self.server.bridge  # type: ignore[attr-defined]

    def _json_response(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        expected = self.bridge.config.inbound_token
        if not expected:
            return True
        authorization = self.headers.get("Authorization", "")
        api_key = self.headers.get("X-API-Key", "")
        return authorization == "Bearer {}".format(expected) or api_key == expected

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == DEFAULT_HEALTH_PATH:
            self._json_response(HTTPStatus.OK, {"status": "ok"})
            return
        if parsed.path == DEFAULT_STATUS_PATH:
            if not self._authorized():
                self._json_response(HTTPStatus.UNAUTHORIZED, {"msg": "未授权", "result": False})
                return
            cad_id = _text(urllib.parse.parse_qs(parsed.query).get("id", [""])[0])
            if not cad_id:
                self._json_response(
                    HTTPStatus.BAD_REQUEST, {"msg": "id 不能为空", "result": False}
                )
                return
            state = self.bridge.public_state(cad_id)
            self._json_response(
                HTTPStatus.OK if state.get("status") != "not_found" else HTTPStatus.NOT_FOUND,
                state,
            )
            return
        self._json_response(HTTPStatus.NOT_FOUND, {"msg": "接口不存在", "result": False})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urllib.parse.urlparse(self.path)
        request_path = parsed.path.rstrip("/")
        if request_path not in {DEFAULT_RECEIVE_PATH, DEFAULT_REPLAN_PATH}:
            self._json_response(HTTPStatus.NOT_FOUND, {"msg": "接口不存在", "result": False})
            return
        if not self._authorized():
            self._json_response(HTTPStatus.UNAUTHORIZED, {"msg": "未授权", "result": False})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1024 * 1024:
                raise ValueError("请求体大小无效")
            payload = json.loads(self.rfile.read(length).decode("utf-8-sig"))
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象")
            if request_path == DEFAULT_REPLAN_PATH:
                cad_id = _text(payload.get("cad_id"))
                try:
                    selections = _parse_route_selection_requests(
                        payload.get("cad_points")
                    )
                    response_payload = self.bridge.generate_routes_sync(
                        cad_id,
                        selections,
                    )
                except Exception as exc:
                    LOGGER.exception("Synchronous route endpoint failed: %s", cad_id)
                    response_payload = _route_failure_response(cad_id, exc)
                self._json_response(HTTPStatus.OK, response_payload)
                return
            cad_id = _text(payload.get("id"))
            file_url = _text(payload.get("file_url"))
            created, _state = self.bridge.submit(cad_id, file_url)
            message = "接收成功" if created else "任务已接收"
            public_state = self.bridge.public_state(cad_id)
            self._json_response(
                HTTPStatus.OK,
                {
                    "msg": message,
                    "result": True,
                    "id": cad_id,
                    "status": public_state.get("status"),
                },
            )
        except KeyError as exc:
            self._json_response(
                HTTPStatus.NOT_FOUND,
                {"msg": str(exc).strip("'"), "result": False},
            )
        except (ValueError, json.JSONDecodeError) as exc:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"msg": str(exc), "result": False},
            )
        except Exception as exc:
            error_code = _text(getattr(exc, "code", ""))
            if error_code:
                self._json_response(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    {
                        "msg": str(exc),
                        "result": False,
                        "error_code": error_code,
                        "details": getattr(exc, "details", {}),
                    },
                )
                return
            LOGGER.exception("CAD endpoint failed")
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"msg": "处理失败: {}".format(exc), "result": False},
            )

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), fmt % args)


def _environment_config(args: argparse.Namespace) -> BridgeConfig:
    allowed = tuple(
        item.strip()
        for item in os.getenv("CAD_DOWNLOAD_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    )
    return BridgeConfig(
        job_root=Path(args.job_root).expanduser().resolve(),
        callback_url=_text(args.callback_url or os.getenv("CAD_RESULT_CALLBACK_URL")),
        route_callback_url=_text(
            getattr(args, "route_callback_url", "")
            or os.getenv("CAD_ROUTE_CALLBACK_URL")
        ),
        callback_token=_text(os.getenv("CAD_RESULT_CALLBACK_TOKEN")),
        inbound_token=_text(os.getenv("CAD_RECEIVE_TOKEN")),
        allowed_download_hosts=allowed,
        download_timeout=int(os.getenv("CAD_DOWNLOAD_TIMEOUT", "60")),
        callback_timeout=int(os.getenv("CAD_CALLBACK_TIMEOUT", "120")),
        max_download_bytes=int(os.getenv("CAD_MAX_DOWNLOAD_BYTES", str(1024 ** 3))),
        max_image_side=int(args.max_image_side),
        callback_retries=int(os.getenv("CAD_CALLBACK_RETRIES", "3")),
        worker_count=int(args.workers),
        heartbeat_interval=float(os.getenv("CAD_HEARTBEAT_INTERVAL", "5")),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CAD 识别、结果回调与选点路线桥接服务")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="启动接口一 HTTP 服务")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8766)
    serve.add_argument("--job-root", default=str(DEFAULT_JOB_ROOT))
    serve.add_argument("--callback-url", default="")
    serve.add_argument(
        "--route-callback-url",
        default="",
        help="兼容旧启动参数；同步路线接口不会使用该回调地址",
    )
    serve.add_argument("--max-image-side", type=int, default=2048)
    serve.add_argument("--workers", type=int, default=1)

    send = subparsers.add_parser("send", help="对已有运行目录执行接口二回传")
    send.add_argument("--cad-id", required=True)
    send.add_argument("--run-dir", required=True)
    send.add_argument("--callback-url", default="")
    send.add_argument("--token", default="")
    send.add_argument("--timeout", type=int, default=120)
    send.add_argument("--retries", type=int, default=3)
    send.add_argument("--max-image-side", type=int, default=2048)

    inspect = subparsers.add_parser("inspect", help="构建并保存接口二完整数组，不发送")
    inspect.add_argument("--cad-id", required=True)
    inspect.add_argument("--run-dir", required=True)
    inspect.add_argument("--result-path", default="")
    inspect.add_argument("--max-image-side", type=int, default=1024)
    return parser


def main() -> None:
    logging.basicConfig(
        level=os.getenv("CAD_API_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _build_parser().parse_args()
    if args.command == "serve":
        config = _environment_config(args)
        bridge = CadBackendBridge(config)
        server = CadApiServer((args.host, args.port), bridge)
        LOGGER.info("接口一已启动: http://%s:%s%s", args.host, args.port, DEFAULT_RECEIVE_PATH)
        LOGGER.info("接口五已启动: http://%s:%s%s", args.host, args.port, DEFAULT_REPLAN_PATH)
        LOGGER.info("接口二回调 URL: %s", config.callback_url or "未配置（结果将保留在任务目录）")
        LOGGER.info("路线生成接口采用同步 HTTP 响应，不发送异步路线回调")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            LOGGER.info("收到停止信号")
        finally:
            server.server_close()
        return
    if args.command == "send":
        callback_url = _text(args.callback_url or os.getenv("CAD_RESULT_CALLBACK_URL"))
        token = _text(args.token or os.getenv("CAD_RESULT_CALLBACK_TOKEN"))
        responses = send_cad_results(
            callback_url,
            args.cad_id,
            Path(args.run_dir),
            token=token,
            timeout=args.timeout,
            retries=args.retries,
            max_image_side=args.max_image_side,
        )
        print(json.dumps(responses, ensure_ascii=False, indent=2))
        return
    built = build_cad_result_payloads(
        args.cad_id,
        Path(args.run_dir),
        max_image_side=args.max_image_side,
    )
    result_path = save_cad_results(
        Path(args.result_path)
        if _text(args.result_path)
        else Path(args.run_dir).expanduser().resolve().parent / "interface2_result.json",
        built,
    )
    summary = [
        {
            "floor_id": item.floor_id,
            "point_count": len(item.payload["cad_points"]),
            "thumbnail_path": str(item.thumbnail_path),
            "route_path": str(item.route_path),
            "thumbnail_base64_length": len(item.payload["thumbnail_url"]),
            "route_base64_length": len(item.payload["route_url"]),
            "first_point": item.payload["cad_points"][0] if item.payload["cad_points"] else None,
        }
        for item in built
    ]
    print(
        json.dumps(
            {"local_result_path": str(result_path), "floors": summary},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
