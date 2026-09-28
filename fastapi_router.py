"""FastAPI router for mounting the CAD inspection backend on an existing app.

Usage in the server's existing ``main.py``::

    from fire_inspection_system.fastapi_router import router as fire_inspection_router
    app.include_router(fire_inspection_router)

The existing Uvicorn process remains the only listener.  All routes therefore
share the same host and port as ``main:app``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .backend_interface_3 import (
    DEFAULT_RECEIVE_PATH,
    DEFAULT_REPLAN_PATH,
    DEFAULT_STATUS_PATH,
    CadBackendBridge,
    _environment_config,
    _parse_route_selection_requests,
    _route_failure_response,
    _text,
)


LOGGER = logging.getLogger("fire_inspection_fastapi")
PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_MOUNTED_JOB_ROOT = PACKAGE_DIR / "runtime" / "backend_api_jobs"


def _create_bridge() -> CadBackendBridge:
    args = SimpleNamespace(
        job_root=os.getenv("CAD_JOB_ROOT", str(DEFAULT_MOUNTED_JOB_ROOT)),
        callback_url="",
        max_image_side=int(os.getenv("CAD_MAX_IMAGE_SIDE", "2048")),
        workers=int(os.getenv("CAD_WORKERS", "1")),
    )
    return CadBackendBridge(_environment_config(args))


bridge = _create_bridge()
router = APIRouter(tags=["消防巡检 CAD"])


def _authorized(request: Request) -> bool:
    expected = bridge.config.inbound_token
    if not expected:
        return True
    authorization = request.headers.get("Authorization", "")
    api_key = request.headers.get("X-API-Key", "")
    return authorization == f"Bearer {expected}" or api_key == expected


def _error(status_code: int, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"msg": message, "result": False, **extra},
    )


@router.get("/health", summary="消防巡检服务健康检查")
@router.get("/api/cad/health", summary="消防巡检服务健康检查（命名空间路径）")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get(DEFAULT_STATUS_PATH, summary="查询 CAD 处理状态")
def status(request: Request, id: str = "") -> Any:
    if not _authorized(request):
        return _error(401, "未授权")
    cad_id = _text(id)
    if not cad_id:
        return _error(400, "id 不能为空")
    state = bridge.read_state(cad_id)
    return JSONResponse(
        status_code=404 if state.get("status") == "not_found" else 200,
        content=state,
    )


@router.post(DEFAULT_RECEIVE_PATH, summary="接收并异步处理 CAD")
def receive(payload: dict[str, Any], request: Request) -> Any:
    if not _authorized(request):
        return _error(401, "未授权")
    try:
        cad_id = _text(payload.get("id"))
        file_url = _text(payload.get("file_url"))
        created, state = bridge.submit(cad_id, file_url)
        return {
            "msg": "接收成功" if created else "任务已接收",
            "result": True,
            "id": cad_id,
            "status": state.get("status"),
        }
    except ValueError as exc:
        return _error(400, str(exc))
    except Exception as exc:
        LOGGER.exception("CAD receive endpoint failed")
        return _error(500, f"处理失败: {exc}")


@router.post(DEFAULT_REPLAN_PATH, summary="按选定点位重新规划路线")
def replan(payload: dict[str, Any], request: Request) -> Any:
    if not _authorized(request):
        return _error(401, "未授权")
    cad_id = _text(payload.get("cad_id"))
    try:
        selections = _parse_route_selection_requests(payload.get("cad_points"))
        return bridge.generate_routes_sync(cad_id, selections)
    except Exception as exc:
        LOGGER.exception("CAD replan endpoint failed")
        return _route_failure_response(cad_id, exc)
