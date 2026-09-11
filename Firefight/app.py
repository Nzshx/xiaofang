import os
import json
import difflib
import re
from contextvars import ContextVar
from datetime import datetime
from io import BytesIO
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple
from zipfile import ZIP_DEFLATED, ZipFile

import asyncio
import httpx
import requests
import boto3
from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from starlette.middleware.cors import CORSMiddleware
from openpyxl import load_workbook
from starlette.staticfiles import StaticFiles

# ============== 环境 ==============
load_dotenv()

VERBOSE_LOG = os.getenv("VERBOSE_LOG", "0") == "1"

# 全过程诊断日志默认关闭；需要排查时可通过环境变量重新开启。
PIPELINE_TRACE_LOG = os.getenv("PIPELINE_TRACE_LOG", "0") == "1"
PIPELINE_TRACE_CONSOLE = os.getenv("PIPELINE_TRACE_CONSOLE", "0") == "1"
PIPELINE_TRACE_RAW = os.getenv("PIPELINE_TRACE_RAW", "0") == "1"
PIPELINE_TRACE_MAX_CHARS = max(500, int(os.getenv("PIPELINE_TRACE_MAX_CHARS", "6000")))
PIPELINE_TRACE_DIR = os.getenv("PIPELINE_TRACE_DIR", "logs").strip() or "logs"

_TRACE_ID: ContextVar[str] = ContextVar("fire_pipeline_trace_id", default="direct")
_TRACE_IMAGE_KEY: ContextVar[str] = ContextVar("fire_pipeline_image_key", default="")
_TRACE_FILE_LOCK = Lock()

def set_trace_context(trace_id: str = "", image_key: str = ""):
    """为当前任务设置日志关联ID；返回token，结束后交给reset_trace_context。"""
    trace_id = str(trace_id or "direct").strip() or "direct"
    return (
        _TRACE_ID.set(trace_id),
        _TRACE_IMAGE_KEY.set(str(image_key or "")),
    )

def reset_trace_context(tokens) -> None:
    if not tokens:
        return
    try:
        trace_token, image_token = tokens
        _TRACE_ID.reset(trace_token)
        _TRACE_IMAGE_KEY.reset(image_token)
    except Exception:
        pass

def _trace_text(value: Any, max_chars: Optional[int] = None) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        except Exception:
            text = repr(value)
    limit = max_chars or PIPELINE_TRACE_MAX_CHARS
    if len(text) > limit:
        return text[:limit] + f"\n...<已截断，原始长度={len(text)}>"
    return text

def trace_log(
    stage: str,
    message: str,
    data: Any = None,
    *,
    raw: bool = False,
) -> None:
    """打印并落盘每个分析阶段的判断过程，不写入MongoDB。"""
    if not PIPELINE_TRACE_LOG:
        return
    if raw and not PIPELINE_TRACE_RAW:
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    trace_id = _TRACE_ID.get()
    image_key = _TRACE_IMAGE_KEY.get()
    prefix = f"[{timestamp}][TRACE={trace_id}][{stage}]"
    if image_key:
        prefix += f"[image={image_key}]"

    block = f"{prefix} {message}"
    if data is not None:
        block += "\n" + _trace_text(data)

    # 完整过程默认只写入.log；需要临时在终端查看时再开启PIPELINE_TRACE_CONSOLE。
    if PIPELINE_TRACE_CONSOLE:
        print(block, flush=True)

    try:
        os.makedirs(PIPELINE_TRACE_DIR, exist_ok=True)
        safe_id = re.sub(r"[^0-9A-Za-z_.-]+", "_", trace_id)[:120] or "direct"
        log_path = os.path.join(PIPELINE_TRACE_DIR, f"fire_pipeline_{safe_id}.log")
        with _TRACE_FILE_LOCK:
            with open(log_path, "a", encoding="utf-8") as fp:
                fp.write(block + "\n")
    except Exception as exc:
        if VERBOSE_LOG:
            print(f"[TRACE-WARN] 日志写文件失败：{exc}", flush=True)

def debug_log(*args, **kwargs):
    if VERBOSE_LOG:
        print(*args, **kwargs, flush=True)

TOS_ENDPOINT = os.getenv("TOS_ENDPOINT", "tos-cn-beijing.volces.com")
TOS_BUCKET = os.getenv("TOS_BUCKET", "nanjing-fire")
TOS_REGION = os.getenv("TOS_REGION", "cn-beijing")
TOS_ACCESS_KEY = os.getenv("TOS_ACCESS_KEY", "")
TOS_SECRET_KEY = os.getenv("TOS_SECRET_KEY", "")

DOUBAO_API_URL = os.getenv("DOUBAO_API_URL", "https://ark.cn-beijing.volces.com/api/v3/responses")
DOUBAO_API_KEY = os.getenv("DOUBAO_API_KEY", "")
DOUBAO_MODEL = os.getenv("DOUBAO_MODEL", "doubao-seed-2-0-pro-260215")

# StageA 的一级、二级、三级候选白名单及整改建议来自问题分级.xlsx的“问题卡库”。
ISSUE_CATALOG_XLSX = os.getenv("ISSUE_CATALOG_XLSX", "问题分级.xlsx")
ISSUE_CATALOG_SHEET = os.getenv("ISSUE_CATALOG_SHEET", "问题卡库")
ISSUE_CATALOG_HEADER_ROW = int(os.getenv("ISSUE_CATALOG_HEADER_ROW", "1"))
ISSUE_COL_CAT1 = os.getenv("ISSUE_COL_CAT1", "一级分类")
ISSUE_COL_CAT2 = os.getenv("ISSUE_COL_CAT2", "二级分类")
ISSUE_COL_CAT3 = os.getenv("ISSUE_COL_CAT3", "三级问题")
ISSUE_COL_REGULATION = os.getenv("ISSUE_COL_REGULATION", "候选规范依据")
ISSUE_COL_RECTIFICATION = os.getenv("ISSUE_COL_RECTIFICATION", "整改建议")

KB_DOMAIN = os.getenv("KB_DOMAIN", "api-knowledgebase.mlp.cn-beijing.volces.com")
KB_SERVICE_RESOURCE_ID = os.getenv("KB_SERVICE_RESOURCE_ID", "kb-service-58c1523ebd033284")
KB_API_KEY = os.getenv("KB_API_KEY", "")

KB_HTTP_TIMEOUT = int(os.getenv("KB_HTTP_TIMEOUT", "30"))
KB_PER_TYPE_TOPK = int(os.getenv("KB_PER_TYPE_TOPK", "1"))

# 独立规范条文知识库：案例库只负责返回条文号，规范库按 record_id 精确返回原文。
CLAUSE_KB_ENABLED = int(os.getenv("CLAUSE_KB_ENABLED", "1"))
CLAUSE_KB_DOMAIN = os.getenv("CLAUSE_KB_DOMAIN", KB_DOMAIN)
CLAUSE_KB_SERVICE_RESOURCE_ID = os.getenv("CLAUSE_KB_SERVICE_RESOURCE_ID", "")
CLAUSE_KB_API_KEY = os.getenv("CLAUSE_KB_API_KEY", KB_API_KEY)
CLAUSE_KB_CONCURRENCY = max(1, int(os.getenv("CLAUSE_KB_CONCURRENCY", "2")))
CLAUSE_KB_SCAN_LIMIT = max(1, int(os.getenv("CLAUSE_KB_SCAN_LIMIT", "15")))
CLAUSE_KB_MAX_EXACT_MATCHES = max(1, int(os.getenv("CLAUSE_KB_MAX_EXACT_MATCHES", "1")))
CLAUSE_KB_CACHE_ENABLED = int(os.getenv("CLAUSE_KB_CACHE_ENABLED", "1"))

# —— 并发与超时 —— #
FS_STAGEA_ROUNDS = int(os.getenv("FS_STAGEA_ROUNDS", "3"))
FS_FUSION_ROUNDS = int(os.getenv("FS_FUSION_ROUNDS", "3"))
FS_MAX_TYPES     = max(5, int(os.getenv("FS_MAX_TYPES", "5")))
FS_TEMP_LIST     = [float(x.strip()) for x in os.getenv("FS_TEMP_LIST", "0.6,0.8,1.0").split(",") if x.strip()]
FS_MODEL_CONCURRENCY = int(os.getenv("FS_MODEL_CONCURRENCY", "6"))
FS_KB_CONCURRENCY    = int(os.getenv("FS_KB_CONCURRENCY", "5"))
FS_HTTP_TIMEOUT      = int(os.getenv("FS_HTTP_TIMEOUT", "300"))

# —— 正式隐患置信度配置 —— #

# StageA为了提高召回率会补足候选，因此只保留较低权重。
FS_CONFIDENCE_STAGEA_WEIGHT = float(
    os.getenv("FS_CONFIDENCE_STAGEA_WEIGHT", "0.15")
)

# Fusion直接判断现场是否存在候选隐患，是置信度的主要来源。
FS_CONFIDENCE_FUSION_WEIGHT = float(
    os.getenv("FS_CONFIDENCE_FUSION_WEIGHT", "0.75")
)

# 案例知识库检索相关度只作为辅助证据。
FS_CONFIDENCE_KB_WEIGHT = float(
    os.getenv("FS_CONFIDENCE_KB_WEIGHT", "0.10")
)

# 不同Fusion票数对应的置信度上限。
FS_CONFIDENCE_SINGLE_VOTE_CAP = float(
    os.getenv("FS_CONFIDENCE_SINGLE_VOTE_CAP", "0.55")
)
FS_CONFIDENCE_TWO_VOTE_CAP = float(
    os.getenv("FS_CONFIDENCE_TWO_VOTE_CAP", "0.82")
)
FS_CONFIDENCE_MAX = float(
    os.getenv("FS_CONFIDENCE_MAX", "0.95")
)

# Fusion最多运行三轮，采用自适应2+1并使用不同的低温度。
FS_FUSION_TEMP_LIST = [
    float(value.strip())
    for value in os.getenv(
        "FS_FUSION_TEMP_LIST",
        "0.1,0.2,0.3",
    ).split(",")
    if value.strip()
]

# StageA候选证据等级权重
STAGE_A_EVIDENCE_WEIGHTS = {
    "direct": 1.0,       # 图片中存在直接可见证据
    "possible": 0.5,     # 有一定视觉依据，但证据不完整
    "supplement": 0.1,   # 主要为了高召回补足候选
}

# —— Stage0 开关与阈值（新增） —— #
FS_STAGE0_ENABLED   = int(os.getenv("FS_STAGE0_ENABLED", "1"))
FS_STAGE0_THRESHOLD = float(os.getenv("FS_STAGE0_THRESHOLD", "0.6"))

if not all([TOS_BUCKET, TOS_ACCESS_KEY, TOS_SECRET_KEY, DOUBAO_API_KEY]):
    raise RuntimeError("缺少环境变量：TOS_* / DOUBAO_API_KEY / TOS_BUCKET")

# ============== FastAPI ==============
app = FastAPI(title="FireSafety Async Ensemble + Stage0")
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def _index():
    return FileResponse("static/index.html")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

# ============== TOS ==============
tos_client = boto3.client(
    "s3",
    endpoint_url=f"https://{TOS_ENDPOINT}",
    aws_access_key_id=TOS_ACCESS_KEY,
    aws_secret_access_key=TOS_SECRET_KEY,
    region_name=TOS_REGION,
)

def _tos_fixed_host() -> str:
    return f"{TOS_BUCKET}.{TOS_ENDPOINT}"

# 用预签名 PUT URL 上传图片
def upload_image_to_tos(file: UploadFile) -> str:
    ext = os.path.splitext(file.filename or "")[-1].lower()
    if ext not in [".png", ".jpg", ".jpeg", ".bmp", ".webp"]:
        ext = ".jpg" if not ext else ext
    key = f"safe_uploads_stageA/{os.urandom(8).hex()}{ext}"
    try:
        content_type = file.content_type or "application/octet-stream"
        put_url = tos_client.generate_presigned_url(
            ClientMethod="put_object",
            Params={"Bucket": TOS_BUCKET, "Key": key, "ContentType": content_type},
            ExpiresIn=600
        )
        from urllib.parse import urlparse
        parsed = urlparse(put_url)
        put_url = put_url.replace(parsed.netloc, _tos_fixed_host())

        resp = requests.put(put_url, data=file.file.read(), headers={"Content-Type": content_type}, timeout=30)
        if resp.status_code != 200:
            raise HTTPException(status_code=500, detail=f"TOS 上传失败：{resp.status_code}")
        return key
    except HTTPException:
        raise
    except Exception as e:
        print("[ERROR] TOS 上传异常:", e)
        raise HTTPException(status_code=500, detail="TOS 上传失败")


# 为对象生成 GET 临时 URL
def _presigned_get_url(key: str, expires: int = 600) -> str:
    try:
        url = tos_client.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": TOS_BUCKET, "Key": key},
            ExpiresIn=expires
        )
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return url.replace(parsed.netloc, _tos_fixed_host())
    except Exception as e:
        print("[ERROR] 生成 GET 签名失败:", e)
        raise HTTPException(status_code=500, detail="生成签名URL失败")

# ============== Excel 三级分类白名单 ==============
_ISSUE_ID_RE = re.compile(r"(?P<id>\d+(?:\.\d+)+-\d+)")
_CODE_NAME_RE = re.compile(r"^\s*(?P<code>\d+(?:\.\d+)*)[.、]?\s*(?P<name>.*)$")
_STANDARD_CODE_RE = re.compile(
    r"(?<![A-Z0-9])(?P<code>GB(?:\s*/\s*T)?\s*\d{4,6}(?:\.\d+)?\s*[-—–－−]\s*\d{4})(?![A-Z0-9])",
    re.IGNORECASE,
)
_CLAUSE_NO_RE = re.compile(r"(?<![\d.])(?P<clause>\d+(?:\.\d+){1,3})(?![\d.])")
_CLAUSE_KB_CACHE: Dict[str, List[dict]] = {}
_CLAUSE_KB_CACHE_LOCK = Lock()
_CLAUSE_CACHE_COLLECTION = None
_KB_CATEGORY_MAPPING = None
_KB_ALLOWED_TEXT: Optional[str] = None


def _clause_cache_key(record_id: str) -> str:
    return f"{CLAUSE_KB_SERVICE_RESOURCE_ID}|{record_id}"


def set_clause_cache_collection(collection) -> None:
    global _CLAUSE_CACHE_COLLECTION
    _CLAUSE_CACHE_COLLECTION = collection


async def _get_persistent_clause_cache(
    record_id: str,
) -> Optional[List[dict]]:
    if not CLAUSE_KB_CACHE_ENABLED or _CLAUSE_CACHE_COLLECTION is None:
        return None
    try:
        doc = await asyncio.to_thread(
            _CLAUSE_CACHE_COLLECTION.find_one,
            {"_id": _clause_cache_key(record_id)},
        )
        if not isinstance(doc, dict):
            return None
        payload = doc.get("verified") or []
        if not isinstance(payload, list) or not payload:
            return None
        verified = [dict(item) for item in payload if isinstance(item, dict)]
        if verified:
            with _CLAUSE_KB_CACHE_LOCK:
                _CLAUSE_KB_CACHE[_clause_cache_key(record_id)] = [
                    dict(item) for item in verified
                ]
            trace_log("ClauseKB-Cache", f"mongo_hit record_id={record_id}")
        return verified or None
    except Exception as exc:
        trace_log(
            "ClauseKB-Cache",
            f"mongo_read_error：{type(exc).__name__}: {exc}",
        )
        return None


async def _set_persistent_clause_cache(
    record_id: str,
    verified: List[dict],
) -> None:
    if (
        not CLAUSE_KB_CACHE_ENABLED
        or _CLAUSE_CACHE_COLLECTION is None
        or not verified
    ):
        return
    cached_verified = [dict(item) for item in verified if isinstance(item, dict)]
    if not cached_verified:
        return
    try:
        cache_key = _clause_cache_key(record_id)

        def _write() -> None:
            _CLAUSE_CACHE_COLLECTION.update_one(
                {"_id": cache_key},
                {
                    "$set": {
                        "service_resource_id": CLAUSE_KB_SERVICE_RESOURCE_ID,
                        "record_id": record_id,
                        "verified": cached_verified,
                        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                },
                upsert=True,
            )

        await asyncio.to_thread(_write)
    except Exception as exc:
        trace_log(
            "ClauseKB-Cache",
            f"mongo_write_error：{type(exc).__name__}: {exc}",
        )


def _cell_text(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _split_code_name(value: str) -> Tuple[str, str]:
    text = _cell_text(value)
    match = _CODE_NAME_RE.match(text)
    if not match:
        return "", text
    return match.group("code"), match.group("name").strip(" .、")


def _load_workbook_compat(xlsx_path: str):
    """读取数据时兼容部分WPS/第三方工具生成的空fill样式节点。"""
    try:
        return load_workbook(xlsx_path, data_only=True, read_only=True)
    except TypeError as exc:
        if "Fill" not in str(exc):
            raise

        # 某些工作簿的styles.xml包含<fill/>，openpyxl会拒绝解析。
        # 这里只在内存中补成合法的none填充，不修改原始Excel文件。
        source = BytesIO()
        with ZipFile(xlsx_path, "r") as source_zip:
            with ZipFile(source, "w", ZIP_DEFLATED) as target_zip:
                for info in source_zip.infolist():
                    content = source_zip.read(info.filename)
                    if info.filename == "xl/styles.xml":
                        content = content.replace(
                            b"<fill/>",
                            b'<fill><patternFill patternType="none"/></fill>',
                        )
                    target_zip.writestr(info, content)

        source.seek(0)
        debug_log("[WARN] Excel样式存在空fill节点，已使用内存兼容模式读取")
        return load_workbook(source, data_only=True, read_only=True)


def _load_category_mapping_from_excel(xlsx_path: str, sheet: str) -> dict:
    """读取问题卡库，形成 issue_id -> 分类、规范依据和整改建议的固定映射。"""
    try:
        wb = _load_workbook_compat(xlsx_path)
        if sheet not in wb.sheetnames:
            raise ValueError(f"找不到工作表：{sheet}")
        ws = wb[sheet]

        headers = {}
        for col in range(1, ws.max_column + 1):
            value = _cell_text(ws.cell(row=ISSUE_CATALOG_HEADER_ROW, column=col).value)
            if value:
                headers[value] = col

        required = [
            ISSUE_COL_CAT1, ISSUE_COL_CAT2, ISSUE_COL_CAT3,
            ISSUE_COL_REGULATION, ISSUE_COL_RECTIFICATION,
        ]
        missing = [name for name in required if name not in headers]
        if missing:
            raise ValueError(f"缺少列：{', '.join(missing)}")

        c1_idx = headers[ISSUE_COL_CAT1]
        c2_idx = headers[ISSUE_COL_CAT2]
        c3_idx = headers[ISSUE_COL_CAT3]
        regulation_idx = headers[ISSUE_COL_REGULATION]
        rectification_idx = headers[ISSUE_COL_RECTIFICATION]

        issues_by_id = {}
        hierarchy = {}
        last_raw1 = ""
        last_raw2 = ""

        for row in range(ISSUE_CATALOG_HEADER_ROW + 1, ws.max_row + 1):
            current_raw1 = _cell_text(ws.cell(row=row, column=c1_idx).value)
            current_raw2 = _cell_text(ws.cell(row=row, column=c2_idx).value)
            raw3 = _cell_text(ws.cell(row=row, column=c3_idx).value)
            regulation = _cell_text(
                ws.cell(row=row, column=regulation_idx).value
            )
            rectification_advice = _cell_text(
                ws.cell(row=row, column=rectification_idx).value
            )

            # 兼容一级、二级分类使用合并单元格的情况。
            if current_raw1:
                if current_raw1 != last_raw1:
                    last_raw2 = ""
                last_raw1 = current_raw1
            if current_raw2:
                last_raw2 = current_raw2

            raw1, raw2 = last_raw1, last_raw2
            if not raw3:
                continue

            match = _ISSUE_ID_RE.search(raw3)
            if not match:
                debug_log(f"[WARN] 第{row}行无法解析三级问题编号：{raw3}")
                continue

            issue_id = match.group("id")
            code1, name1 = _split_code_name(raw1)
            code2, name2 = _split_code_name(raw2)

            name3 = raw3[:match.start()] + raw3[match.end():]
            name3 = re.sub(
                r"^\s*问题\s*Q?\s*[-—:：]?\s*",
                "",
                name3,
                flags=re.IGNORECASE,
            )
            name3 = name3.strip(" .、-—：:")
            if not name3:
                continue

            item = {
                "category1_code": code1,
                "category1": name1 or raw1,
                "category2_code": code2,
                "category2": name2 or raw2,
                "issue_id": issue_id,
                "category3": name3,
                "regulation_index_raw": regulation,
                "rectification_advice": rectification_advice,
            }
            if issue_id in issues_by_id:
                debug_log(f"[WARN] 重复 issue_id，保留首次记录：{issue_id}")
                continue

            issues_by_id[issue_id] = item
            hierarchy.setdefault(code1 or item["category1"], {
                "category1_code": code1,
                "category1": item["category1"],
                "category2": {},
            })
            level2 = hierarchy[code1 or item["category1"]]["category2"].setdefault(
                code2 or item["category2"],
                {
                    "category2_code": code2,
                    "category2": item["category2"],
                    "issues": [],
                },
            )
            level2["issues"].append(item)

        if not issues_by_id:
            raise ValueError("问题卡库中没有可用的三级问题")

        for level1 in hierarchy.values():
            for level2 in level1["category2"].values():
                level2["issues"].sort(key=lambda x: x["issue_id"])

        return {
            "issues_by_id": issues_by_id,
            "hierarchy": hierarchy,
        }
    except HTTPException:
        raise
    except Exception as e:
        print("[ERROR] 加载三级分类白名单失败：", e)
        raise HTTPException(status_code=500, detail=f"服务器未正确加载问题分级表：{e}")


def _render_allowed_categories_text(catalog: dict) -> str:
    lines = ["候选白名单：issue_id|二级分类|三级问题"]
    for issue in catalog["issues_by_id"].values():
        lines.append(
            f"{issue['issue_id']}|{issue['category2']}|{issue['category3']}"
        )
    return "\n".join(lines)


# 保留旧函数名，避免 main.py 或其他模块调用处需要同步修改。
def _get_kb_categories() -> Tuple[dict, str]:
    global _KB_CATEGORY_MAPPING, _KB_ALLOWED_TEXT
    if _KB_CATEGORY_MAPPING is None:
        if not os.path.exists(ISSUE_CATALOG_XLSX):
            raise HTTPException(status_code=500,detail=f"ISSUE_CATALOG_XLSX 不存在：{ISSUE_CATALOG_XLSX}",)
        _KB_CATEGORY_MAPPING = _load_category_mapping_from_excel(ISSUE_CATALOG_XLSX,ISSUE_CATALOG_SHEET,)
        _KB_ALLOWED_TEXT = _render_allowed_categories_text(_KB_CATEGORY_MAPPING)
        debug_log("[INFO] 已加载三级问题白名单：",len(_KB_CATEGORY_MAPPING["issues_by_id"]),"个三级问题",)
    return _KB_CATEGORY_MAPPING, _KB_ALLOWED_TEXT


# ============== 通用工具 ==============
def _clamp01(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return default


def _smoothed_vote_ratio(count: int, rounds: int) -> float:
    """
    使用Jeffreys平滑，避免只有3轮投票时直接出现0或1。

    三轮情况下：
    0票 -> 0.125
    1票 -> 0.375
    2票 -> 0.625
    3票 -> 0.875
    """
    try:
        rounds = max(1, int(rounds or 0))
        count = max(0, min(int(count or 0), rounds))
    except (TypeError, ValueError):
        return 0.0

    return (count + 0.5) / (rounds + 1.0)


def _compute_hazard_confidence(
    stage_a_count: int,
    stage_a_rounds: int,
    fusion_count: int,
    fusion_rounds: int,
    kb_score: float = 0.0,
    stage_a_score: Optional[float] = None
) -> float:
    """
    计算正式隐患的可解释置信度。

    置信度来源：
    - StageA候选稳定性；
    - Fusion现场复核稳定性；
    - 案例知识库相关度。

    该值用于排序和前端提示，不直接等同于统计意义上的真实概率。
    """
    try:
        fusion_count = int(fusion_count or 0)
    except (TypeError, ValueError):
        fusion_count = 0

    # 没有任何Fusion正票，不应产生正式隐患置信度。
    if fusion_count <= 0:
        return 0.0

    if stage_a_score is None:
        # 兼容旧数据或旧调用方式。
        stage_score = _smoothed_vote_ratio(
            stage_a_count,
            stage_a_rounds,
        )
    else:
        # 新流程直接使用证据等级加权后的StageA分数。
        stage_score = _clamp01(stage_a_score)

    fusion_score = _smoothed_vote_ratio(
        fusion_count,
        fusion_rounds,
    )
    kb_score = _clamp01(kb_score)

    stage_weight = max(0.0, FS_CONFIDENCE_STAGEA_WEIGHT)
    fusion_weight = max(0.0, FS_CONFIDENCE_FUSION_WEIGHT)
    kb_weight = max(0.0, FS_CONFIDENCE_KB_WEIGHT)

    weight_sum = stage_weight + fusion_weight + kb_weight
    if weight_sum <= 0:
        stage_weight = 0.15
        fusion_weight = 0.75
        kb_weight = 0.10
        weight_sum = 1.0

    confidence = (
        stage_weight * stage_score
        + fusion_weight * fusion_score
        + kb_weight * kb_score
    ) / weight_sum

    # StageA仅出现一轮，说明候选本身不稳定，再进行轻微降权。
    if int(stage_a_count or 0) <= 1:
        confidence *= 0.90

    # 根据Fusion正票数设置可信度上限。
    if fusion_count == 1:
        confidence = min(
            confidence,
            FS_CONFIDENCE_SINGLE_VOTE_CAP,
        )
    elif fusion_count == 2:
        confidence = min(
            confidence,
            FS_CONFIDENCE_TWO_VOTE_CAP,
        )
    else:
        confidence = min(
            confidence,
            FS_CONFIDENCE_MAX,
        )

    return round(_clamp01(confidence), 4)

def _extract_json(text: str) -> str:
    import re
    m = re.search(r"```json\s*(\{.*\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1].strip()
    return text.strip()

# 构造多模态输入，兼容两种常见返回文本位置
def _build_responses_payload(prompt_text: str, image_urls: List[str] = None, temperature: float = None) -> dict:
    image_urls = image_urls or []
    content = []
    for image_url in image_urls:
        if image_url:
            content.append({"type": "input_image", "image_url": image_url})
    content.append({"type": "input_text", "text": prompt_text})
    payload = {
        "model": DOUBAO_MODEL,
        "input": [
            {
                "role": "user",
                "content": content
            }
        ]
    }
    if temperature is not None:
        payload["temperature"] = float(temperature)
    return payload

def _extract_response_text(data: dict) -> str:
    if not data:
        return ""
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    texts = []
    for item in data.get("output", []) or []:
        for c in item.get("content", []) or []:
            if c.get("type") == "output_text" and c.get("text"):
                texts.append(c["text"])
            elif c.get("type") == "text" and c.get("text"):
                texts.append(c["text"])
    return "\n".join(texts).strip()

def _closest(subcand: str, options: list, cutoff: float = 0.65):
    if not subcand:
        return None
    match = difflib.get_close_matches(subcand.strip(), options or [], n=1, cutoff=cutoff)
    return match[0] if match else None

def _clean_kb_text(t: str) -> str:
    if not t:
        return ""
    return t.replace("<KBImage>", "").replace("</KBImage>", "").strip()

def _normalize_ws(s: str) -> str:
    return " ".join((s or "").split())

def _violations_match_kb(hazard: dict, kb_text: str) -> bool:
    """只校验知识库中已经提供的规范原文，不校验条款索引。"""
    kb_norm = _normalize_ws(kb_text)
    for v in hazard.get("violations", []) or []:
        content = _normalize_ws(v.get("content", ""))
        if not content or content not in kb_norm:
            return False
    return True

def _compact_for_match(value: str) -> str:
    """用于比对规范名称、条款号，忽略空格和中英文标点差异。"""
    return "".join(ch.lower() for ch in (value or "") if ch.isalnum())

def _regulation_ref_is_in_kb(ref: dict, kb_text: str) -> bool:
    """判断条款索引是否来自本次检索结果；不要求知识库已有条文原文。"""
    standard = _compact_for_match(str(ref.get("standard", "")))
    clause = _compact_for_match(str(ref.get("clause", "")))
    kb_compact = _compact_for_match(_clean_kb_text(kb_text))
    return bool(standard and clause and standard in kb_compact and clause in kb_compact)

# 条款索引去重、标记核验状态；并检查规范号和条款号是否在本次 KB 文本内
def _prepare_regulation_refs(hazard: dict, kb_text: str) -> List[dict]:
    """标准化条款索引，并始终标记为待专业核对。"""
    refs = []
    seen = set()
    for raw in hazard.get("regulation_refs", []) or []:
        if not isinstance(raw, dict):
            continue
        standard = str(raw.get("standard", "")).strip()
        clause = str(raw.get("clause", "")).strip()
        if not standard or not clause:
            continue
        key = (_compact_for_match(standard), _compact_for_match(clause))
        if key in seen:
            continue
        seen.add(key)
        refs.append({
            "standard": standard,
            "clause": clause,
            "verification_status": "待专业核对",
            "source": "知识库条款索引" if _regulation_ref_is_in_kb(raw, kb_text) else "模型推断",
        })
    return refs

# 仅保留条文内容能在 KB 返回文本中逐字出现的记录，避免模型生成原文被当成已核验规范
def _verified_violations_from_kb(hazard: dict, kb_text: str) -> List[dict]:
    """仅保留能在本次知识库文本中逐字找到的规范原文。"""
    verified = []
    kb_norm = _normalize_ws(kb_text)
    for raw in hazard.get("violations", []) or []:
        if not isinstance(raw, dict):
            continue
        content = _normalize_ws(str(raw.get("content", "")))
        if not content or content not in kb_norm:
            continue
        verified.append({
            "standard": str(raw.get("standard", "")).strip(),
            "clause": str(raw.get("clause", "")).strip(),
            "content": str(raw.get("content", "")).strip(),
            "verification_status": "已核对原文",
        })
    return verified

# ============== 【新增】Stage0 场景筛查（异步） ==============
def _build_stage0_prompt() -> str:
    # 低温度、严格 JSON。给出正例元素，尽量抑制“万物皆有隐患”的错判。
    return (
        "请判断这张图片是否与【建筑消防验收、消防巡检、建筑施工质量检查或消防设施检查】场景相关。\n"
        "【判断原则】\n"
        "1. 本阶段只判断场景相关性，不判断图片中是否存在消防隐患。\n"
        "2. 不判断图片质量、清晰度、拍摄角度、遮挡情况，也不判断是否能够仅凭图片确认具体问题。\n"
        "3. 图片中不一定会直接出现消防设备。只要属于建筑内部、建筑外部、施工现场、"
        "机电安装区域或消防设施可能设置的位置，均应判为相关。\n"
        "4. 无法确定是否相关时，应优先判为相关，交由后续阶段继续分析，不要轻易拦截。\n\n"
        "【应判为不相关的场景】\n"
        "仅当图片明显属于以下内容且与建筑、施工或消防检查没有关系时，才判为不相关：\n"
        "- 自然风景、人物自拍、食物、动物；\n"
        "- 纯商品图片、纯商标、纯图标、与消防无关的文字截图；\n"
        "- 普通车辆内部、生活用品或其他明显非建筑场景。\n\n"
        " 【相关场景通常包含但不限于】\n"
        "- 建筑室内、走廊、房间、楼梯间、前室、地下室、设备间、管井、吊顶内部；\n"
        "- 建筑外部或周边环境/走廊/房间/楼梯/门窗/墙面/天花、管线/风管/电缆桥架、喷淋/消火栓/消防水带/灭火器、泵房/配电柜/控制柜、应急照明/疏散指示;\n"
        "- 建筑外立面、屋面、消防车道、消防登高操作场地、建筑周边；\n"
        "- 墙体、门窗、楼板、吊顶、洞口、防火分隔和防火封堵位置；\n"
        "- 消防管道、给排水管道、喷淋、消火栓、消防水带、灭火器；\n"
        "- 风管、防烟排烟设施、电缆桥架、线缆、配电柜、控制柜；\n"
        "- 火灾报警设备、应急照明、疏散指示、防火门、消防电梯；\n"
        "- 尚未完工、设备缺失、设施损坏、局部构件或近距离拍摄的建筑施工图片等。\n\n"
        "仅输出严格 JSON（不要代码块、不要解释）：\n"
        "{\n"
        '  "relevant": true,\n'
        '  "confidence": 0.0,\n'
        '  "scene_tags": ["室内","走廊","喷淋","配电柜","管线"],\n'
        '  "reason": "一句话说明判断依据"\n'
        "}\n"
    )

def _parse_stage0_json(raw: str) -> dict:
    try:
        data = json.loads(_extract_json(raw))
    except Exception:
        return {"relevant": True, "confidence": 1.0, "scene_tags": [], "reason": "parse_fallback_true"}
    rel = bool(data.get("relevant", False))
    conf = float(data.get("confidence", 0.0) or 0.0)
    tags = data.get("scene_tags", []) or []
    reason = str(data.get("reason", "") or "")
    # 纠偏：若模型输出 relevant 但信心很低，保持原值；若输出不相关且有明显建筑标签，也不强行改。
    return {"relevant": rel, "confidence": max(0.0, min(conf, 1.0)), "scene_tags": tags, "reason": reason}

# 用豆包判断图片是否与建筑消防巡检有关；返回 relevant、confidence、scene_tags、reason。API/解析失败时默认“放行”。
async def run_stage0_guard_async(client: httpx.AsyncClient, sem: asyncio.Semaphore, image_url: str) -> dict:
    payload = _build_responses_payload(
        prompt_text=_build_stage0_prompt(),
        image_urls=[image_url],
        temperature=0.2
    )
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {DOUBAO_API_KEY}"}
    async with sem:
        r = await client.post(DOUBAO_API_URL, headers=headers, json=payload)
    if r.status_code != 200:
        # 失败时不阻断流程（维持旧行为）：默认放行
        trace_log(
            "Stage0",
            f"调用失败，按兜底规则放行：HTTP {r.status_code}",
            r.text[:1000],
        )
        return {"relevant": True, "confidence": 1.0, "scene_tags": [], "reason": "api_fallback_true"}
    raw = _extract_response_text(r.json())
    trace_log("Stage0-RAW", "模型原始返回", raw, raw=True)
    out = _parse_stage0_json(raw)
    trace_log(
        "Stage0",
        "场景筛查解析结果",
        {
            "relevant": out.get("relevant"),
            "confidence": out.get("confidence"),
            "threshold": FS_STAGE0_THRESHOLD,
            "scene_tags": out.get("scene_tags", []),
            "reason": out.get("reason", ""),
        },
    )
    return out


# ============== StageA 无白名单命中时的开放式兜底 ==============
def _build_catalog_unmatched_prompt() -> str:
    """StageA没有任何有效白名单候选时，开放式判断图片是否确有疑似问题。"""
    return (
        "你是一名消防验收与消防巡检图像分析助手。\n"
        "前一阶段已确认该图片属于建筑、施工或消防检查相关场景，"
        "但系统未能在既有问题分级表中匹配到任何有效三级问题。\n\n"
        "请暂时不要受问题分级表限制，直接根据图片中能够观察到的事实进行开放式复核：\n"
        "1. 判断图片中是否存在明确或较高可能性的消防、建筑防火、消防设施安装或施工质量问题；\n"
        "2. 只能依据图片可见事实，不得把正常构造、正常设备或无法确认的内容强行判为隐患；\n"
        "3. 若没有足够证据，应将 suspected_hazard 设为 false；\n"
        "4. 若存在疑似问题，可给出1～3条模型建议项，但不要编造问题编号；\n"
        "5. 不要填写规范名称、条款号或规范原文，规范依据后续必须由专业人员核验；\n"
        "6. 建议项应说明可见位置、现场现象、判断依据和建议人工检查内容。\n\n"
        "仅输出严格JSON，不要解释、不要代码块：\n"
        "{\n"
        '  "suspected_hazard": true,\n'
        '  "confidence": 0.0,\n'
        '  "summary": "对图片的总体判断",\n'
        '  "suggestions": [\n'
        "    {\n"
        '      "suggested_issue": "模型建议的问题名称，不填写issue_id",\n'
        '      "point": "图片中的具体位置或对象",\n'
        '      "description": "结合可见事实说明疑似问题",\n'
        '      "visual_evidence": ["可见依据1", "可见依据2"],\n'
        '      "recommended_action": "建议人工复核或补拍的内容"\n'
        "    }\n"
        "  ],\n"
        '  "reason": "为什么判断为有问题或无问题"\n'
        "}\n"
    )


def _parse_catalog_unmatched_result(raw_text: str) -> dict:
    """解析开放式兜底结果；解析失败时不误判为无隐患，而是标记需人工复核。"""
    try:
        data = json.loads(_extract_json(raw_text))
    except Exception as exc:
        return {
            "analysis_available": False,
            "suspected_hazard": False,
            "confidence": 0.0,
            "summary": "",
            "suggestions": [],
            "reason": f"fallback_parse_error: {exc}",
        }

    suspected = bool(data.get("suspected_hazard", False))
    try:
        confidence = float(data.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(confidence, 1.0))

    suggestions = []
    if suspected:
        for raw in data.get("suggestions", []) or []:
            if not isinstance(raw, dict):
                continue
            suggested_issue = str(raw.get("suggested_issue", "") or "").strip()
            point = str(raw.get("point", "") or "").strip()
            description = str(raw.get("description", "") or "").strip()
            evidence = [
                str(item).strip()
                for item in (raw.get("visual_evidence", []) or [])
                if str(item).strip()
            ][:5]
            recommended_action = str(raw.get("recommended_action", "") or "").strip()

            # 至少要有问题名称、位置或描述之一，避免保存空建议项。
            if not any([suggested_issue, point, description]):
                continue
            suggestions.append({
                "suggested_issue": suggested_issue or "未分类疑似问题",
                "point": point,
                "description": description,
                "visual_evidence": evidence,
                "recommended_action": recommended_action,
                "catalog_match_status": "unmatched",
                "review_required": True,
            })
            if len(suggestions) >= 3:
                break

    # 模型声称存在问题却没有给出可用建议时，视为结果不完整，需要人工复核。
    analysis_available = not (suspected and not suggestions)
    return {
        "analysis_available": analysis_available,
        "suspected_hazard": suspected,
        "confidence": confidence,
        "summary": str(data.get("summary", "") or "").strip(),
        "suggestions": suggestions,
        "reason": str(data.get("reason", "") or "").strip(),
    }


async def run_catalog_unmatched_fallback_async(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    image_url: str,
) -> dict:
    """当StageA没有白名单候选时，调用模型做一次不受白名单限制的开放式复核。"""
    try:
        raw = await _doubao_chat_image(
            client,
            sem,
            image_url,
            _build_catalog_unmatched_prompt(),
            temperature=0.2,
        )
        result = _parse_catalog_unmatched_result(raw)
    except Exception as exc:
        trace_log(
            "StageA-Fallback",
            f"开放式复核失败：{type(exc).__name__}: {exc}",
        )
        result = {
            "analysis_available": False,
            "suspected_hazard": False,
            "confidence": 0.0,
            "summary": "",
            "suggestions": [],
            "reason": f"fallback_api_error: {exc}",
        }

    debug_log(
        "[StageA-Fallback] "
        f"available={result.get('analysis_available')} "
        f"suspected={result.get('suspected_hazard')} "
        f"suggestions={len(result.get('suggestions', []))}"
    )
    return result


def build_catalog_unmatched_final_result(fallback_result: dict) -> dict:
    """将开放式兜底结果转换为前端和MongoDB统一使用的final_result。"""
    available = bool(fallback_result.get("analysis_available", False))
    suspected = bool(fallback_result.get("suspected_hazard", False))
    suggestions = fallback_result.get("suggestions", []) or []

    if not available:
        return {
            "analysis_status": "catalog_unmatched",
            "catalog_match_status": "unmatched",
            "hazard_count": 0,
            "hazards": [],
            "unmatched_suggestions": [],
            "review_required": True,
            "review_reason": (
                "图片已通过消防巡检场景筛查，但StageA未在问题分级表中匹配到有效问题；"
                "开放式模型复核未能得到完整结果，请人工复核。"
            ),
            "analysis_message": "未在问题分级表里匹配到对应问题。",
            "fallback_summary": fallback_result.get("summary", ""),
            "fallback_reason": fallback_result.get("reason", ""),
            "fallback_confidence": fallback_result.get("confidence", 0.0),
        }

    if suspected:
        return {
            "analysis_status": "catalog_unmatched",
            "catalog_match_status": "unmatched",
            "hazard_count": 0,
            "hazards": [],
            "unmatched_suggestions": suggestions,
            "review_required": True,
            "review_reason": (
                "图片通过消防巡检场景筛查，模型开放式复核认为存在疑似问题，"
                "但未在问题分级表里匹配到对应问题，请人工复核并考虑补充问题分级表。"
            ),
            "analysis_message": "未在问题分级表里匹配到对应问题，以下为模型建议项。",
            "fallback_summary": fallback_result.get("summary", ""),
            "fallback_reason": fallback_result.get("reason", ""),
            "fallback_confidence": fallback_result.get("confidence", 0.0),
        }

    return {
        "analysis_status": "no_hazard",
        "catalog_match_status": "no_stageA_match",
        "hazard_count": 0,
        "hazards": [],
        "unmatched_suggestions": [],
        "review_required": False,
        "review_reason": "",
        "analysis_message": (
            "图片已通过消防巡检场景筛查；StageA未匹配到白名单问题，"
            "开放式复核也未发现有充分图像证据支持的消防隐患。"
        ),
        "fallback_summary": fallback_result.get("summary", ""),
        "fallback_reason": fallback_result.get("reason", ""),
        "fallback_confidence": fallback_result.get("confidence", 0.0),
    }


# ============== StageA（异步）：直接输出一级/二级/三级候选 ==============
def _build_stageA_prompt(
    allowed_text: str,
    run_tag: str = "",
) -> str:
    tag = f"\n【抽样序号】{run_tag}\n" if run_tag else ""

    return (
        "你是消防隐患图像分析助手。\n\n"
        "从下方白名单中选择5~6个最相关的三级问题候选。\n"
        "只能返回白名单中的issue_id，不得自造或重复。\n\n"
        "evidence_level：\n"
        "- direct：图片中有明确直接可见证据；\n"
        "- possible：存在相关视觉现象，但证据不完整；\n"
        "- supplement：证据较弱，仅用于补足召回。\n\n"
        "规则：\n"
        "1. 优先direct，其次possible，最后supplement；\n"
        "2. direct必须能在图片中指出明确对象或异常；\n"
        "3. 不足5个候选时，用supplement补足；\n"
        "4. 不要因出现某设备就把该设备所有常见问题判为direct。\n\n"
        f"【白名单】\n{allowed_text}\n\n"
        "仅输出JSON：\n"
        "{\n"
        '  "hazard_queries": [\n'
        "    {\n"
        '      "issue_id": "1.1.2-7",\n'
        '      "evidence_level": "direct",\n'
        '      "keywords": ["1~3个视觉关键词"]\n'
        "    }\n"
        "  ]\n"
        "}\n"
        + tag
    )


async def _doubao_chat_image(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    image_url: str,
    prompt_text: str,
    temperature: float = 0.7,
) -> str:
    payload = _build_responses_payload(
        prompt_text=prompt_text,
        image_urls=[image_url],
        temperature=temperature,
    )
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {DOUBAO_API_KEY}"}
    async with sem:
        r = await client.post(DOUBAO_API_URL, headers=headers, json=payload)
    if r.status_code != 200:
        raise HTTPException(status_code=500, detail=f"StageA 调用失败：{r.text[:200]}")
    return _extract_response_text(r.json())


def _parse_stageA_types(
    raw_text: str,
    catalog: dict,
) -> List[Dict[str, str]]:
    """
    只接受白名单中的issue_id。

    一级、二级、三级名称从Excel主数据回填；
    evidence_level只作为StageA内部计算依据。
    """
    raw = _extract_json(raw_text)
    data = json.loads(raw)
    hazard_queries = data.get("hazard_queries", []) or []

    valid = []
    seen = set()

    evidence_aliases = {
        "直接证据": "direct",
        "明确证据": "direct",
        "确定": "direct",
        "direct": "direct",

        "可能": "possible",
        "疑似": "possible",
        "间接证据": "possible",
        "possible": "possible",

        "补充": "supplement",
        "补足": "supplement",
        "高召回补充": "supplement",
        "supplement": "supplement",
    }

    for item in hazard_queries:
        if not isinstance(item, dict):
            continue

        raw_issue_id = str(item.get("issue_id", "")).strip()
        match = _ISSUE_ID_RE.search(raw_issue_id)
        issue_id = (
            match.group("id")
            if match
            else raw_issue_id
        )
        master = catalog["issues_by_id"].get(issue_id)
        if not master or issue_id in seen:
            continue

        raw_evidence_level = str(item.get("evidence_level", "")).strip().lower()
        evidence_level = evidence_aliases.get(raw_evidence_level,raw_evidence_level,)

        # 模型未填写或填写异常时，不允许默认成direct。
        if evidence_level not in STAGE_A_EVIDENCE_WEIGHTS:
            evidence_level = "supplement"

        candidate = dict(master)
        candidate["evidence_level"] = evidence_level
        candidate["keywords"] = [
            str(keyword).strip()
            for keyword in (item.get("keywords") or [])
            if str(keyword).strip()
        ][:3]

        valid.append(candidate)
        seen.add(issue_id)

        if len(valid) >= 6:
            break

    return valid


# 三轮并发采样，按 issue_id 出现频次排序，最终保留前 FS_MAX_TYPES（默认5）个三级候选。
async def ensemble_stageA_async(
    image_url: str,
    allowed_text: str,
    mapping: dict,
    rounds: int,
    temps: List[float],
    model_sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
):
    trace_log(
        "StageA",
        "开始多轮候选召回",
        {
            "rounds": rounds,
            "temperatures": temps,
            "max_selected_types": FS_MAX_TYPES,
            "evidence_weights": STAGE_A_EVIDENCE_WEIGHTS,
        },
    )

    tasks = []
    temperatures = temps or [0.7]

    for index in range(rounds):
        temperature = temperatures[
            index % len(temperatures)
        ]
        run_tag = f"R{index + 1}-T{temperature}"

        prompt = _build_stageA_prompt(
            allowed_text,
            run_tag=run_tag,
        )

        tasks.append(
            _doubao_chat_image(
                client,
                model_sem,
                image_url,
                prompt,
                temperature=temperature,
            )
        )

    texts = await asyncio.gather(
        *tasks,
        return_exceptions=True,
    )

    # 原始出现次数
    raw_count: Dict[str, int] = {}

    # 按direct/possible/supplement加权后的累计分数
    weighted_sum: Dict[str, float] = {}

    direct_count: Dict[str, int] = {}
    possible_count: Dict[str, int] = {}
    supplement_count: Dict[str, int] = {}

    candidate_by_id: Dict[str, dict] = {}
    merged_keywords: Dict[str, List[str]] = {}

    per_round: List[List[Dict[str, str]]] = []
    valid_rounds = 0

    for round_index, result in enumerate(texts, 1):
        if isinstance(result, Exception):
            trace_log(
                f"StageA-R{round_index}",
                "模型调用失败",
                {"error": repr(result)},
            )
            per_round.append([])
            continue

        trace_log(
            f"StageA-R{round_index}-RAW",
            "模型原始返回",
            result,
            raw=True,
        )
        try:
            types_in_round = _parse_stageA_types(
                result,
                mapping,
            )
        except Exception as exc:
            trace_log(
                f"StageA-R{round_index}",
                "JSON解析或白名单回填失败",
                {"error": repr(exc)},
            )
            per_round.append([])
            continue

        trace_log(
            f"StageA-R{round_index}",
            "本轮有效候选",
            [
                {
                    "issue_id": item.get("issue_id", ""),
                    "category3": item.get("category3", ""),
                    "evidence_level": item.get("evidence_level", ""),
                    "keywords": item.get("keywords", []),
                }
                for item in types_in_round
            ],
        )

        # API和JSON均正常时才计为有效轮次。
        valid_rounds += 1
        per_round.append(types_in_round)

        debug_log(f"[StageA] 第{round_index}轮识别：" )

        for candidate in types_in_round:
            issue_id = candidate["issue_id"]
            evidence_level = candidate.get(
                "evidence_level",
                "supplement",
            )

            evidence_weight = (
                STAGE_A_EVIDENCE_WEIGHTS.get(
                    evidence_level,
                    STAGE_A_EVIDENCE_WEIGHTS["supplement"],
                )
            )

            raw_count[issue_id] = (
                raw_count.get(issue_id, 0) + 1
            )

            weighted_sum[issue_id] = (
                weighted_sum.get(issue_id, 0.0)
                + evidence_weight
            )

            direct_count.setdefault(issue_id, 0)
            possible_count.setdefault(issue_id, 0)
            supplement_count.setdefault(issue_id, 0)

            if evidence_level == "direct":
                direct_count[issue_id] += 1
            elif evidence_level == "possible":
                possible_count[issue_id] += 1
            else:
                supplement_count[issue_id] += 1

            candidate_by_id.setdefault( issue_id, dict(candidate),)

            # 合并多轮视觉关键词。
            merged_keywords.setdefault(issue_id, [])
            for keyword in candidate.get(
                "keywords",
                [],
            ):
                if (
                    keyword
                    and keyword
                    not in merged_keywords[issue_id]
                ):
                    merged_keywords[issue_id].append(
                        keyword
                    )

            debug_log(
                f"  - {candidate['category1']} - "
                f"{candidate['category2']} - "
                f"{issue_id} "
                f"{candidate['category3']} "
                f"[{evidence_level}, "
                f"weight={evidence_weight}]"
            )

    stage_a_denominator = max(1, valid_rounds)

    # 不再只按出现次数排序：
    # 先按证据加权分数，再看direct次数和原始出现次数。
    ranked_ids = sorted(
        raw_count.keys(),
        key=lambda issue_id: (
            -weighted_sum.get(issue_id, 0.0),
            -direct_count.get(issue_id, 0),
            -possible_count.get(issue_id, 0),
            -raw_count.get(issue_id, 0),
            issue_id,
        ),
    )

    votes = []

    for issue_id in ranked_ids:
        stage_a_score = (
            weighted_sum.get(issue_id, 0.0)
            / stage_a_denominator
        )

        vote = dict(candidate_by_id[issue_id])

        # evidence_level是单轮结果，汇总后不再直接使用。
        vote.pop("evidence_level", None)

        vote["count"] = raw_count.get(issue_id, 0)
        vote["direct_count"] = direct_count.get(issue_id,0, )
        vote["possible_count"] = possible_count.get(issue_id, 0, )
        vote["supplement_count"] = ( supplement_count.get(issue_id, 0))
        vote["stage_a_score"] = round( stage_a_score,4,)
        votes.append(vote)

    debug_log("[StageA] 三级候选证据统计：")

    for vote in votes:
        debug_log(
            f"  {vote['issue_id']} "
            f"{vote['category3']}："
            f"出现{vote['count']}/{stage_a_denominator}，"
            f"direct={vote['direct_count']}，"
            f"possible={vote['possible_count']}，"
            f"supplement={vote['supplement_count']}，"
            f"score={vote['stage_a_score']:.4f}"
        )

    selected = []

    for issue_id in ranked_ids[:FS_MAX_TYPES]:
        candidate = dict(candidate_by_id[issue_id])

        # 删除单轮证据等级，避免误认为这是最终等级。
        candidate.pop("evidence_level", None)
        candidate["keywords"] = merged_keywords.get(
            issue_id,
            [],
        )[:4]

        # 保留原始次数，兼容之前的置信度和日志。
        candidate["stage_a_count"] = int( raw_count.get(issue_id, 0))
        candidate["stage_a_rounds"] = int( stage_a_denominator)

        # 新增内部证据分数。
        candidate["stage_a_score"] = round(
            weighted_sum.get(issue_id, 0.0)
            / stage_a_denominator,
            4,
        )

        candidate["stage_a_direct_count"] = int(direct_count.get(issue_id, 0))
        candidate["stage_a_possible_count"] = int(possible_count.get(issue_id, 0))
        candidate["stage_a_supplement_count"] = int(supplement_count.get(issue_id, 0))
        selected.append(candidate)

    trace_log(
        "StageA",
        "候选投票汇总与最终入选结果",
        {
            "valid_rounds": valid_rounds,
            "all_votes": [
                {
                    "issue_id": vote.get("issue_id", ""),
                    "category3": vote.get("category3", ""),
                    "count": vote.get("count", 0),
                    "direct_count": vote.get("direct_count", 0),
                    "possible_count": vote.get("possible_count", 0),
                    "supplement_count": vote.get("supplement_count", 0),
                    "stage_a_score": vote.get("stage_a_score", 0),
                }
                for vote in votes
            ],
            "selected": [
                {
                    "issue_id": item.get("issue_id", ""),
                    "category3": item.get("category3", ""),
                    "stage_a_count": item.get("stage_a_count", 0),
                    "stage_a_rounds": item.get("stage_a_rounds", 0),
                    "stage_a_score": item.get("stage_a_score", 0),
                    "direct_count": item.get("stage_a_direct_count", 0),
                    "possible_count": item.get("stage_a_possible_count", 0),
                    "supplement_count": item.get("stage_a_supplement_count", 0),
                }
                for item in selected
            ],
        },
    )

    return selected, votes, per_round


# ============== KB（异步） ==============
def _kb_headers():
    if not all([KB_SERVICE_RESOURCE_ID, KB_API_KEY]):
        raise HTTPException(status_code=500, detail="知识库配置缺失（KB_SERVICE_RESOURCE_ID/KB_API_KEY）")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json;charset=UTF-8",
        "Host": KB_DOMAIN,
        "Authorization": f"Bearer {KB_API_KEY}",
    }


def _build_kb_query(candidate: dict) -> str:
    path = " > ".join(
        x for x in [candidate.get("category1", ""), candidate.get("category2", ""), candidate.get("category3", "")]
        if x
    )
    return (
        f"问题编号：{candidate.get('issue_id', '')}\n"
        f"隐患路径：{path}\n"
        f"视觉关键词：{'、'.join(candidate.get('keywords', []) or [])}\n"
        "请返回与该三级问题高度相关的：\n"
        "- 隐患描述（专业、简洁）；\n"
        "- 对应规范的名称和条款号；\n"
        "- 如知识库已收录，再返回可核对的规范原文；\n"
        "- 示例图片（如有）。\n"
        "当前知识库可能只有条款索引；条款索引不是规范原文，不能补写或臆造原文。"
    )


# 向配置的远程知识库 chat 服务请求检索，不读本地向量库。
async def kb_chat_async(client: httpx.AsyncClient, sem: asyncio.Semaphore, query_text: str) -> dict:
    url = f"http://{KB_DOMAIN}/api/knowledge/service/chat"

    payload = {
        "service_resource_id": KB_SERVICE_RESOURCE_ID,
        "messages": [{"role": "user", "content": query_text}],
        "stream": False,
    }
    headers = _kb_headers()
    async with sem:
        r = await client.post(url, headers=headers, json=payload)
    if r.status_code != 200:
        raise HTTPException(status_code=500, detail=f"KB 检索失败：HTTP {r.status_code}")
    data = r.json()
    if data.get("code") != 0:
        raise HTTPException(status_code=500, detail=f"KB 业务错误：{data}")
    return data


# 对每个三级候选并发检索；抽取远端 content、score、图片附件链接。
async def search_kb_for_types_async(
    types: List[Dict[str, str]],
    topk: int,
    kb_sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
):
    trace_log(
        "StageB-KB",
        "开始按候选检索案例知识库",
        [
            {
                "issue_id": candidate.get("issue_id", ""),
                "category3": candidate.get("category3", ""),
                "query": _build_kb_query(candidate),
            }
            for candidate in types
        ],
    )
    tasks = [kb_chat_async(client, kb_sem, _build_kb_query(candidate)) for candidate in types]
    packs = await asyncio.gather(*tasks, return_exceptions=True)
    results = []

    identity_fields = [
        "category1_code", "category1", "category2_code", "category2",
        "issue_id", "category3", "keywords", "regulation_index_raw",
        "rectification_advice", "stage_a_count", "stage_a_rounds",
    ]

    for candidate, pack_result in zip(types, packs):
        base = {key: candidate.get(key, "") for key in identity_fields}
        if isinstance(pack_result, Exception):
            trace_log(
                "StageB-KB",
                f"候选{candidate.get('issue_id', '')}检索失败",
                {"error": repr(pack_result)},
            )
            results.append({**base, "results": []})
            continue

        result_list = (((pack_result or {}).get("data") or {}).get("result_list")) or []
        evidence = []
        for entry in result_list[:topk]:
            text = entry.get("content", "") or ""
            score = float(entry.get("score", 0.0) or 0.0)
            images = []
            for attachment in entry.get("chunk_attachment", []) or []:
                if attachment.get("type") == "image" and attachment.get("link"):
                    images.append(attachment["link"])
            evidence.append({"content": text, "images": images, "score": score})

        results.append({**base, "results": evidence})
        trace_log(
            "StageB-KB",
            f"候选{candidate.get('issue_id', '')}检索结果",
            {
                "category3": candidate.get("category3", ""),
                "hit_count": len(evidence),
                "hits": [
                    {
                        "rank": rank,
                        "score": item.get("score", 0.0),
                        "image_count": len(item.get("images", []) or []),
                        "content": item.get("content", ""),
                    }
                    for rank, item in enumerate(evidence, 1)
                ],
            },
        )

    return results


# ============== 规范条文知识库（按 record_id 精确关联） ==============
def _normalize_standard_code(value: Any) -> str:
    """GB 50016-2014 / GB/T 12345-2020 -> GB50016-2014 / GB/T12345-2020。"""
    text_value = str(value or "").upper()
    text_value = (
        text_value.replace("—", "-")
        .replace("–", "-")
        .replace("－", "-")
        .replace("−", "-")
    )
    match = _STANDARD_CODE_RE.search(text_value)
    if not match:
        return ""
    return re.sub(r"\s+", "", match.group("code"))


def _normalize_clause_no(value: Any) -> str:
    """第6.2.9条 -> 6.2.9。"""
    match = _CLAUSE_NO_RE.search(str(value or ""))
    return match.group("clause") if match else ""


def _normalize_clause_lookup(value: Any) -> Tuple[str, str]:
    """将四级编号按规范库主条文方式关联：6.2.9.3 -> 6.2.9，子项3。"""
    requested = _normalize_clause_no(value)
    if not requested:
        return "", ""
    parts = requested.split(".")
    if len(parts) >= 4:
        return ".".join(parts[:3]), ".".join(parts[3:])
    return requested, ""


def _make_clause_ref(standard: Any, clause: Any, source: str) -> Optional[dict]:
    standard_code = _normalize_standard_code(standard)
    requested_clause_no = _normalize_clause_no(clause)
    clause_no, subitem_no = _normalize_clause_lookup(requested_clause_no)
    if not standard_code or not clause_no:
        return None
    return {
        "standard_code": standard_code,         # 规范编号，如 GB 50016-2014
        "clause_no": clause_no,                 # 实际查询的主条款号
        "requested_clause_no": requested_clause_no, # 原始请求条款号
        "subitem_no": subitem_no,               # 四级条款拆分后的子项号
        "record_id": f"{standard_code}_{clause_no}", # 规范知识库查询 ID，格式一般为“规范号_主条款号”
        "source": source,                       # 条款索引来源，如案例库或问题分级表
    }


def _parse_refs_from_text(value: Any, source: str) -> List[dict]:
    """从问题分级表或案例库条款索引文本中提取规范号和条文号。"""
    text_value = str(value or "")
    code_matches = list(_STANDARD_CODE_RE.finditer(text_value))
    refs: List[dict] = []
    seen = set()

    for index, code_match in enumerate(code_matches):
        segment_end = code_matches[index + 1].start() if index + 1 < len(code_matches) else len(text_value)
        segment = text_value[code_match.end():segment_end]
        for clause_match in _CLAUSE_NO_RE.finditer(segment):
            ref = _make_clause_ref(code_match.group("code"), clause_match.group("clause"), source)
            if not ref or ref["record_id"] in seen:
                continue
            seen.add(ref["record_id"])
            refs.append(ref)
    return refs


def _collect_hazard_regulation_refs(hazard: dict) -> List[dict]:
    """优先使用案例融合提取出的条文号，同时用问题分级表固定依据兜底。"""
    refs: List[dict] = []
    seen = set()

    for raw in hazard.get("regulation_refs", []) or []:
        if not isinstance(raw, dict):
            continue
        standard = raw.get("standard_code") or raw.get("standard") or raw.get("standard_title")
        clause = raw.get("requested_clause_no") or raw.get("clause_no") or raw.get("clause")
        ref = _make_clause_ref(standard, clause, raw.get("source") or "案例知识库条款索引")
        if not ref or ref["record_id"] in seen:
            continue
        seen.add(ref["record_id"])
        refs.append(ref)

    for ref in _parse_refs_from_text(
        hazard.get("regulation_index_raw", ""),
        source="问题分级表候选规范依据",
    ):
        if ref["record_id"] in seen:
            continue
        seen.add(ref["record_id"])
        refs.append(ref)

    return refs


def _knowledge_service_url(domain: str) -> str:
    clean_domain = str(domain or "").strip().rstrip("/")
    if clean_domain.startswith("http://") or clean_domain.startswith("https://"):
        return f"{clean_domain}/api/knowledge/service/chat"
    return f"https://{clean_domain}/api/knowledge/service/chat"


def _clause_kb_headers() -> dict:
    missing = []
    if not CLAUSE_KB_DOMAIN:
        missing.append("CLAUSE_KB_DOMAIN")
    if not CLAUSE_KB_SERVICE_RESOURCE_ID:
        missing.append("CLAUSE_KB_SERVICE_RESOURCE_ID")
    if not CLAUSE_KB_API_KEY:
        missing.append("CLAUSE_KB_API_KEY")
    if missing:
        raise RuntimeError(f"规范知识库缺少配置：{', '.join(missing)}")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json;charset=UTF-8",
        "Authorization": f"Bearer {CLAUSE_KB_API_KEY}",
    }


async def clause_kb_chat_async(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    record_id: str,
) -> dict:
    """使用 record_id 查询规范知识库，并对429自动退避重试。"""
    payload = {
        "service_resource_id": CLAUSE_KB_SERVICE_RESOURCE_ID,
        "messages": [
            {
                "role": "user",
                "content": record_id,
            }
        ],
        "stream": False,
    }

    url = _knowledge_service_url(CLAUSE_KB_DOMAIN)
    headers = _clause_kb_headers()
    response = None

    for attempt in range(5):
        async with sem:
            response = await client.post(
                url,
                headers=headers,
                json=payload,
            )

        if response.status_code != 429:
            break
        retry_after = response.headers.get( "Retry-After", "",).strip()

        try:
            wait_seconds = float(retry_after)
        except (TypeError, ValueError):
            wait_seconds = min(1.5 * (2 ** attempt), 12,)
        wait_seconds = max(wait_seconds, 1.5)
        trace_log(
            "ClauseKB",
            f"{record_id} 触发限流，{wait_seconds:.1f}秒后重试 ({attempt + 1}/5)",
        )
        await asyncio.sleep(wait_seconds)

    if response is None:
        raise RuntimeError("规范知识库未返回响应")
    if response.status_code != 200:
        raise RuntimeError(f"规范知识库调用失败：HTTP " f"{response.status_code}")
    data = response.json()
    if data.get("code") != 0:
        raise RuntimeError( "规范知识库业务错误：" f"{data.get('message', '')}" )
    return data


def _kb_result_entries(pack: dict) -> List[dict]:
    data = (pack or {}).get("data") or {}
    entries = data.get("result_list") if isinstance(data, dict) else None
    return [entry for entry in (entries or []) if isinstance(entry, dict)]


def _entry_searchable_text(entry: dict) -> str:
    parts = []
    for key in ("content", "fields", "metadata", "document_fields"):
        value = entry.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            parts.append(value)
        else:
            try:
                parts.append(json.dumps(value, ensure_ascii=False))
            except Exception:
                parts.append(str(value))
    return "\n".join(parts)


def _extract_entry_identity(entry: dict) -> dict:
    text_value = _entry_searchable_text(entry)

    record_match = re.search(
        r"(?im)^\s*record_id\s*[:：]\s*([^\s,，]+)",
        text_value,
    )
    standard_match = re.search(
        r"(?im)^\s*standard_code\s*[:：]\s*([^\r\n,，]+)",
        text_value,
    )
    clause_match = re.search(
        r"(?im)^\s*(?:clause_no|条文编号)\s*[:：]\s*([^\r\n,，]+)",
        text_value,
    )

    record_id = re.sub(r"\s+", "", record_match.group(1)).upper() if record_match else ""
    standard_code = _normalize_standard_code(standard_match.group(1)) if standard_match else ""
    clause_no = _normalize_clause_no(clause_match.group(1)) if clause_match else ""

    if not standard_code:
        standard_code = _normalize_standard_code(text_value)
    if record_id and "_" in record_id:
        record_standard, record_clause = record_id.rsplit("_", 1)
        standard_code = standard_code or record_standard
        clause_no = clause_no or _normalize_clause_no(record_clause)

    return {
        "record_id": record_id,
        "standard_code": standard_code,
        "clause_no": clause_no,
    }


def _entry_exactly_matches_ref(entry: dict, ref: dict) -> bool:
    identity = _extract_entry_identity(entry)
    target_record_id = ref["record_id"].upper()
    if identity["record_id"]:
        return identity["record_id"] == target_record_id
    return (
        identity["standard_code"] == ref["standard_code"]
        and identity["clause_no"] == ref["clause_no"]
    )


_CLAUSE_METADATA_LINE_RE = re.compile(
    r"(?im)^\s*(?:record_id|standard_code|standard_title|clause_no|source_url)\s*[:：]"
)


def _strip_clause_metadata_suffix(value: str) -> str:
    text_value = str(value or "").strip()
    match = _CLAUSE_METADATA_LINE_RE.search(text_value)
    return text_value[:match.start()].strip() if match else text_value


def _normalize_clause_text_layout(value: Any) -> str:
    """清除规范原文中的多余空行，保留正常的逐条换行。"""
    text = str(value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip()
    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)*", "\n", text)


def _entry_nested_field(entry: dict, field_name: str) -> str:
    direct = entry.get(field_name)
    if direct not in (None, ""):
        return str(direct).strip()
    for container_name in ("fields", "metadata", "document_fields"):
        container = entry.get(container_name)
        if isinstance(container, dict) and container.get(field_name) not in (None, ""):
            return str(container.get(field_name)).strip()
    return ""


def _parse_clause_entry(entry: dict, ref: dict, retrieval_rank: int) -> dict:
    content = str(entry.get("content") or _entry_nested_field(entry, "content") or "")
    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()

    title_match = re.search(
        r"(?im)^\s*(?:content\s*[：:]\s*)?规范名称\s*[：:]\s*(.+)$",
        content,
    )
    clause_match = re.search(r"(?im)^\s*条文编号\s*[：:]\s*(.+)$", content)
    body_match = re.search(r"(?im)^\s*正文\s*[：:]\s*", content)

    clause_text = ""
    explanation_text = ""
    if body_match:
        body = content[body_match.end():]
        explanation_match = re.search(r"(?im)^\s*条文说明\s*[：:]\s*", body)
        if explanation_match:
            clause_text = _strip_clause_metadata_suffix(body[:explanation_match.start()])
            explanation_text = _strip_clause_metadata_suffix(body[explanation_match.end():])
        else:
            clause_text = _strip_clause_metadata_suffix(body)

    standard_title = (
        title_match.group(1).strip()
        if title_match
        else _entry_nested_field(entry, "standard_title")
    )
    standard_code = (
        _normalize_standard_code(_entry_nested_field(entry, "standard_code"))
        or _normalize_standard_code(standard_title)
        or ref["standard_code"]
    )
    clause_no = (
        _normalize_clause_no(_entry_nested_field(entry, "clause_no"))
        or _normalize_clause_no(clause_match.group(1) if clause_match else "")
        or ref["clause_no"]
    )

    clause_text = _normalize_clause_text_layout(clause_text)
    explanation_text = _normalize_clause_text_layout(explanation_text)

    try:
        retrieval_score = float(entry.get("score", 0) or 0)
    except (TypeError, ValueError):
        retrieval_score = 0.0

    return {
        "standard_title": standard_title,
        "standard_code": standard_code,
        "clause_no": clause_no,
        "requested_clause_no": ref.get("requested_clause_no", clause_no),
        "subitem_no": ref.get("subitem_no", ""),
        "clause_text": clause_text,
        "explanation_text": explanation_text,
        "record_id": ref["record_id"],
        "source_url": _entry_nested_field(entry, "source_url"),
        "retrieval_score": retrieval_score,
        "retrieval_rank": retrieval_rank,
        "verification_status": "kb_exact_match",
    }


async def _query_one_clause_ref(
    ref: dict,
    clause_sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
) -> dict:
    record_id = ref["record_id"]
    cache_key = _clause_cache_key(record_id)
    with _CLAUSE_KB_CACHE_LOCK:
        memory_cached = _CLAUSE_KB_CACHE.get(cache_key)
        memory_cached = (
            [dict(item) for item in memory_cached]
            if memory_cached
            else None
        )
    if CLAUSE_KB_CACHE_ENABLED and memory_cached:
        trace_log("ClauseKB-Cache", f"memory_hit record_id={record_id}")
        return {
            "unavailable": False,
            "verified": memory_cached,
        }

    cached = await _get_persistent_clause_cache(record_id)
    if cached:
        return {
            "unavailable": False,
            "verified": [dict(item) for item in cached],
        }

    try:
        trace_log("ClauseKB-Cache", f"miss record_id={record_id}")
        response = await clause_kb_chat_async(client, clause_sem, record_id)
    except Exception as exc:
        trace_log(
            "ClauseKB",
            f"{record_id} 调用失败：{type(exc).__name__}: {exc}",
        )
        return {"unavailable": True, "verified": []}

    entries = _kb_result_entries(response)[:CLAUSE_KB_SCAN_LIMIT]
    verified = []
    for rank, entry in enumerate(entries, 1):
        exact = _entry_exactly_matches_ref(entry, ref)
        identity = _extract_entry_identity(entry)
        debug_log(
            f"[ClauseKB] target={record_id} rank={rank} "
            f"returned={identity.get('record_id') or identity.get('standard_code')} "
            f"clause={identity.get('clause_no')} exact={exact}"
        )
        if not exact:
            continue
        parsed = _parse_clause_entry(entry, ref, rank)
        if not parsed["clause_text"]:
            debug_log(f"[ClauseKB] {record_id} 精确命中但正文为空，跳过")
            continue
        verified.append(parsed)
        if len(verified) >= CLAUSE_KB_MAX_EXACT_MATCHES:
            break

    if verified and CLAUSE_KB_CACHE_ENABLED:
        cached_verified = [dict(item) for item in verified]
        with _CLAUSE_KB_CACHE_LOCK:
            _CLAUSE_KB_CACHE[cache_key] = cached_verified
        await _set_persistent_clause_cache(record_id, cached_verified)
    return {"unavailable": False, "verified": verified}


async def enrich_hazards_with_clause_kb_async(
    hazards: List[dict],
    clause_sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
) -> Tuple[List[dict], List[dict]]:
    """为最终隐患补充规范原文；只查询最终保留结果，不查询全部StageA候选。"""
    refs_by_issue = {
        hazard.get("issue_id", ""): _collect_hazard_regulation_refs(hazard)
        for hazard in hazards
    }

    if not CLAUSE_KB_ENABLED:
        summaries = []
        for hazard in hazards:
            issue_id = hazard.get("issue_id", "")
            refs = refs_by_issue.get(issue_id, [])
            hazard["regulation_refs"] = refs
            hazard["clause_kb_status"] = "disabled" if refs else "missing_reference"
            hazard["regulation_review_status"] = "规范知识库已关闭" if refs else "未提取到规范条款索引"
            summaries.append({
                "issue_id": issue_id,
                "status": hazard["clause_kb_status"],
                "regulation_refs": refs,
                "verified_count": 0,
            })
        return hazards, summaries

    unique_refs: Dict[str, dict] = {}
    for refs in refs_by_issue.values():
        for ref in refs:
            unique_refs.setdefault(ref["record_id"], ref)

    ref_items = list(unique_refs.items())
    outputs = await asyncio.gather(
        *[
            _query_one_clause_ref(ref, clause_sem, client)
            for _, ref in ref_items
        ],
        return_exceptions=True,
    )
    results_by_record_id: Dict[str, dict] = {}
    for (record_id, _), output in zip(ref_items, outputs):
        if isinstance(output, Exception):
            results_by_record_id[record_id] = {
                "unavailable": True,
                "verified": [],
            }
        else:
            results_by_record_id[record_id] = output

    summaries = []
    for hazard in hazards:
        issue_id = hazard.get("issue_id", "")
        refs = refs_by_issue.get(issue_id, [])
        verified = []
        matched_ref_count = 0
        unavailable = False
        seen = set()

        for ref in refs:
            result = results_by_record_id.get(ref["record_id"], {"unavailable": True, "verified": []})
            unavailable = unavailable or bool(result.get("unavailable"))
            items = result.get("verified", [])
            if items:
                matched_ref_count += 1
            for item in items:
                key = item.get("record_id") or ref["record_id"]
                if key in seen:
                    continue
                seen.add(key)
                verified.append(item)

        if not refs:
            status = "missing_reference"
            review_status = "案例库和问题分级表均未提供可解析的规范号、条文号"
        elif unavailable:
            status = "clause_kb_unavailable"
            review_status = "规范知识库调用失败，条文原文尚未核验"
        elif matched_ref_count == len(refs):
            status = "kb_exact_match"
            review_status = "规范原文已精确匹配，具体适用性仍需专业核验"
        elif matched_ref_count:
            status = "partial_match"
            review_status = "部分规范原文已匹配，其余条文未找到"
        else:
            status = "not_found"
            review_status = "已获得条文索引，但规范知识库未返回精确原文"

        hazard["regulation_refs"] = refs
        # 兼容旧前端字段：violations只放经过规范知识库精确核验的原文。
        hazard["violations"] = [
            {
                "standard": item.get("standard_title") or item.get("standard_code", ""),    # 规范名称或规范编号
                "clause": item.get("requested_clause_no") or item.get("clause_no", ""),     # 条款号
                "content": item.get("clause_text", ""),     # 规范知识库精确匹配的条文原文
                "verification_status": "已核对原文",     # 当前一般为“已核对原文”
            }
            for item in verified
            if item.get("clause_text")
        ]
        hazard["clause_kb_status"] = status
        hazard["regulation_review_status"] = review_status
        hazard["regulation_text_verified"] = bool(verified)

        summaries.append({
            "issue_id": issue_id,
            "status": status,
            "regulation_refs": refs,
            "verified_count": len(verified),
        })

    return hazards, summaries


async def collect_fusion_results_and_enrich_clause_kb_async(
    fusion_results: List[Any],
    client: httpx.AsyncClient,
    log_stage_c: Callable[[str], None],
    log_task_error: Callable[[str], None],
) -> Tuple[List[dict], List[dict], List[dict]]:
    """汇总 Fusion 投票，并使用同一 HTTP 客户端补充规范条文原文。"""
    final_hazards: List[dict] = []
    fusion_votes: List[dict] = []

    for result in fusion_results:
        if isinstance(result, Exception):
            log_task_error(f"任务异常：{result}")
            continue
        if not isinstance(result, dict):
            log_task_error(f"任务返回类型异常：{type(result).__name__}")
            continue

        fusion_votes.append({
            "category1": result.get("category1", ""),
            "category2": result.get("category2", ""),
            "issue_id": result.get("issue_id", ""),
            "category3": result.get("category3", ""),
            "rectification_advice": result.get("rectification_advice", ""),
            "confidence": result.get("confidence", 0.0),
            "present_count": result.get("present_count", 0),
            "absent_count": result.get("absent_count", 0),
            "unobservable_count": result.get("unobservable_count", 0),
            "valid_rounds": result.get("valid_rounds", 0),
            "observable_rounds": result.get("observable_rounds", 0),
            "rounds": result.get("rounds", 0),
            "threshold": result.get("threshold", 1),
            "retention_rule": result.get("retention_rule", "rejected"),
            "present": result.get("present", False),
        })

        if result.get("present", False):
            status = (
                "存在(单票低置信度保留)"
                if result.get("present_count", 0) == 1
                else "存在(多票通过)"
            )
        elif result.get("observable_rounds", 0) <= 0:
            status = "当前图片不可判断"
        else:
            status = "未获得present票"

        log_stage_c(
            f"{result.get('issue_id', '')} "
            f"{result.get('category3', '')} => {status}；"
            f"present={result.get('present_count', 0)}/"
            f"{result.get('observable_rounds', 0)}，"
            f"absent={result.get('absent_count', 0)}，"
            f"unobservable={result.get('unobservable_count', 0)}"
        )

        if result.get("present") and result.get("hazard"):
            final_hazards.append(result["hazard"])

    clause_kb_results: List[dict] = []
    if final_hazards:
        clause_sem = asyncio.Semaphore(CLAUSE_KB_CONCURRENCY)
        final_hazards, clause_kb_results = await enrich_hazards_with_clause_kb_async(
            final_hazards,
            clause_sem,
            client,
        )

    return final_hazards, fusion_votes, clause_kb_results


async def build_skipped_fusion_result_async(
    candidate: dict,
    category_formatter: Optional[Callable[[str, str], str]] = None,
) -> dict:
    """为无案例知识库证据的候选生成统一的未执行 Fusion 结果。"""
    category1 = candidate.get("category1", "")
    category2 = candidate.get("category2", "")
    if category_formatter:
        category1 = category_formatter(
            candidate.get("category1_code", ""),
            category1,
        )
        category2 = category_formatter(
            candidate.get("category2_code", ""),
            category2,
        )

    return {
        "category1_code": candidate.get("category1_code", ""),
        "category1": category1,
        "category2_code": candidate.get("category2_code", ""),
        "category2": category2,
        "issue_id": candidate.get("issue_id", ""),
        "category3": candidate.get("category3", ""),
        "rectification_advice": candidate.get("rectification_advice", ""),
        "confidence": 0.0,
        "present_count": 0,
        "absent_count": 0,
        "unobservable_count": 0,
        "valid_rounds": 0,
        "observable_rounds": 0,
        "rounds": 0,
        "threshold": 1,
        "retention_rule": "not_run",
        "present": False,
        "hazard": None,
    }


def build_stage0_rejected_pipeline_payload(
    image_key: str,
    image_url: str,
    stage0_result: dict,
) -> dict:
    """构造 Stage0 未通过时的统一返回结构。"""
    return {
        "image_key": image_key,
        "image_url": image_url,
        "stage0": stage0_result,
        "hazard_types": [],
        "kb_results": [],
        "clause_kb_results": [],
        "final_result": {
            "analysis_status": "stage0_rejected",
            "catalog_match_status": "not_applicable",
            "hazard_count": 0,
            "hazards": [],
            "unmatched_suggestions": [],
            "review_required": False,
            "review_reason": "图片未通过建筑消防巡检场景筛查，未执行后续隐患分析",
            "analysis_message": "图片未通过建筑消防巡检场景筛查。",
        },
        "type_votes": [],
        "fusion_votes": [],
    }


def build_catalog_unmatched_pipeline_payload(
    image_key: str,
    image_url: str,
    stage0_result: dict,
    fallback_result: dict,
    type_votes: List[dict],
) -> dict:
    """构造 StageA 无白名单候选、但已完成开放式兜底复核时的返回结构。"""
    return {
        "image_key": image_key,
        "image_url": image_url,
        "stage0": stage0_result,
        "hazard_types": [],
        "kb_results": [],
        "clause_kb_results": [],
        "stageA_fallback": fallback_result,
        "final_result": build_catalog_unmatched_final_result(fallback_result),
        "type_votes": type_votes,
        "fusion_votes": [],
    }


def build_matched_final_result(
    final_hazards: List[dict],
    catalog_match_status: str = "matched",
) -> dict:
    """构造已完成 StageA/Fusion 流程后的最终隐患结果。"""
    review_required = any(
        hazard.get("clause_kb_status") != "kb_exact_match"
        for hazard in final_hazards
    )
    return {
        "analysis_status": "hazard_detected" if final_hazards else "no_hazard",
        "catalog_match_status": catalog_match_status,
        "hazard_count": len(final_hazards),
        "hazards": final_hazards,
        "unmatched_suggestions": [],
        "review_required": review_required,
        "review_reason": (
            "部分条文原文未精确匹配，需专业人员复核"
            if review_required
            else ""
        ),
        "analysis_message": (
            "已识别到问题分级表内的疑似隐患。"
            if final_hazards
            else "未识别到最终保留隐患。"
        ),
    }


def build_completed_pipeline_payload(
    image_key: str,
    image_url: str,
    stage0_result: dict,
    selected_types: List[dict],
    kb_results: List[dict],
    clause_kb_results: List[dict],
    final_result: dict,
    type_votes: List[dict],
    fusion_votes: List[dict],
) -> dict:
    """构造正常完成流水线后的统一返回结构。"""
    return {
        "image_key": image_key,
        "image_url": image_url,
        "stage0": stage0_result,
        # 直接输出一级、二级、三级候选。
        "hazard_types": selected_types,
        "kb_results": kb_results,
        "clause_kb_results": clause_kb_results,
        "final_result": final_result,
        "type_votes": type_votes,
        "fusion_votes": fusion_votes,
    }


# ============== Fusion（异步） ==============
def _build_fusion_prompt(candidate: dict, kb_text: str) -> str:
    path = " > ".join(
        value
        for value in [
            candidate.get("category1", ""),
            candidate.get("category2", ""),
            candidate.get("category3", ""),
        ]
        if value
    )

    return (
        "请根据现场图片，对指定候选隐患进行复核。\n\n"
        "图片说明：\n"
        "1. 第一张图片是待检测的现场图片；\n"
        "2. 后续图片是知识库参考图片，如没有则只分析现场图片；\n"
        "3. 知识库内容用于帮助理解问题，不代表现场一定存在该问题。\n\n"

        f"本轮仅判断候选："
        f"「{candidate.get('issue_id', '')}｜{path}」。\n\n"

        "请从以下三种结论中选择一种：\n"
        "1. present：现场图片中存在与候选问题相符的可见现象，"
        "可以作为疑似隐患保留；\n"
        "2. absent：现场图片已经清楚展示相关对象，"
        "并且可见状态与该候选问题明显不符；\n"
        "3. unobservable：现场图片中完全没有出现判断该问题所需要的"
        "对象、位置或区域，暂时无法分析。\n\n"

        "【判断原则】\n"
        "1. 本阶段用于发现疑似隐患，不要求仅凭一张图片完成最终规范验收结论；\n"
        "2. 图片中只要存在与候选问题相符的明显现象，即可判为present，"
        "不要求所有规范条件都能同时核验；\n"
        "3. 对遮挡、占用、堵塞、破损、脱落、缺失、未封闭、安装不规范、"
        "位置异常等肉眼可见现象，应优先根据现场实际情况判断；\n"
        "4. 若候选问题包含多种表现形式，只要现场图符合其中一种，"
        "即可判为present，并只描述实际看到的现象；\n"
        "5. 证据不完整但已经存在明显疑似现象时，应判为present，"
        "不要因为无法完成尺寸测量、功能测试或全部规范核验而直接判为unobservable；\n"
        "6. 只有当相关对象或检查区域完全没有出现在图片中时，"
        "才判为unobservable；\n"
        "7. 只有图片明确显示相关对象状态正常、且与候选问题明显不符时，"
        "才判为absent；不能仅因为证据较少就判为absent；\n"
        "8. 看见同类对象不等于一定存在该问题，仍需指出图片中与候选问题相符的现场现象；\n"
        "9. 参考图片和知识库文字只能辅助理解，不得单独作为现场存在隐患的依据；\n"
        "10. 本阶段输出的是疑似隐患，最终是否违反规范仍需结合设计资料和人工复核。\n\n"
        "11. point和description只能描述现场图片中直接可见的构件、部位和异常现象；\n"
        "12. 禁止输出项目名称、楼栋号、单元号、楼层号、房间号、轴线号、"
        "施工区域编号以及其他无法仅凭现场图片确认的具体位置信息；\n"
        "13. 禁止复制知识库案例中的楼栋、楼层、轴线、项目名称和历史整改位置；\n"
        "14. point应使用通用部位描述，例如“幕墙与楼板交接处”“桥架穿墙部位”"
        "“配电箱内部”，不要填写“10-2栋S-4至15轴”等工程定位信息；\n"
        "15. description应直接从可见异常开始描述，不要以“现场某栋某层”开头。\n\n"
        
        "仅输出严格JSON，不要解释、不要代码块：\n"
        "{\n"
        '  "decision": "present、absent或unobservable",\n'
        '  "visual_evidence": ["现场图片中可见的现象"],\n'
        '  "missing_evidence": ["建议人工继续核验的内容"],\n'
        '  "reason": "简要说明判断理由",\n'
        '  "hazard": null\n'
        "}\n\n"

        "当decision为present时，hazard必须填写：\n"
        "{\n"
        '  "category1": "固定分类",\n'
        '  "category2": "固定分类",\n'
        f'  "issue_id": "{candidate.get("issue_id", "")}",\n'
        f'  "category3": "{candidate.get("category3", "")}",\n'
        '  "point": "仅填写通用可见部位或对象，例如幕墙与楼板交接处，不得填写楼栋、楼层、轴线、房间号",\n'
        '  "description": "仅描述现场图片中实际可见的异常，不得引用案例中的项目位置和历史描述",\n'
        '  "regulation_refs": [],\n'
        '  "violations": []\n'
        "}\n\n"

        "当decision为absent或unobservable时，hazard必须为null。\n\n"
        "【知识库证据区】\n"
        "以下内容可能来自其他项目的历史案例，只能用于理解问题类型和异常特征。\n"
        "严禁将其中的项目名称、楼栋号、楼层、房间号、轴线号、整改记录"
        "复制到本次现场图片的point或description中。\n"
        f"{_clean_kb_text(kb_text)}"
    )


async def _doubao_fusion(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    scene_url: str,
    kb_text: str,
    kb_image_url: str,
    candidate: dict,
    temperature: float = 0.2,
) -> dict:
    image_urls = [scene_url]
    if kb_image_url:
        image_urls.append(kb_image_url)

    payload = _build_responses_payload(
        prompt_text=_build_fusion_prompt(candidate, kb_text),
        image_urls=image_urls,
        temperature=temperature,
    )
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {DOUBAO_API_KEY}"}
    async with sem:
        r = await client.post(DOUBAO_API_URL, headers=headers, json=payload)
    issue_id = candidate.get("issue_id", "")
    if r.status_code != 200:
        trace_log(
            "Fusion-Round",
            f"候选{issue_id}调用失败，temperature={temperature}",
            {"http_status": r.status_code, "response": r.text[:1000]},
        )
        return {
            "present": False,
            "valid": False,
            "observable": False,
            "decision": "invalid",
            "hazard": None,
            "reason": f"http_{r.status_code}",
        }

    raw = _extract_response_text(r.json())
    trace_log(
        "Fusion-RAW",
        f"候选{issue_id}模型原始返回，temperature={temperature}",
        raw,
        raw=True,
    )
    try:
        fusion = json.loads(_extract_json(raw))
    except Exception as exc:
        trace_log(
            "Fusion-Round",
            f"候选{issue_id}解析失败，temperature={temperature}",
            {"error": repr(exc), "raw_preview": raw[:1000]},
        )
        return {
            "present": False,
            "valid": False,
            "observable": False,
            "decision": "invalid",
            "hazard": None,
            "reason": "parse_error",
        }

    decision = str(
        (fusion or {}).get("decision", "")
    ).strip().lower()

    trace_log(
        "Fusion-Round",
        f"候选{issue_id}本轮解析结果，temperature={temperature}",
        {
            "decision": decision,
            "reason": (fusion or {}).get("reason", ""),
            "visual_evidence": (fusion or {}).get("visual_evidence", []) or [],
            "missing_evidence": (fusion or {}).get("missing_evidence", []) or [],
            "hazard_present": isinstance((fusion or {}).get("hazard"), dict),
        },
    )

    if decision not in {
        "present",
        "absent",
        "unobservable",
    }:
        return {
            "present": False,
            "valid": False,
            "observable": False,
            "decision": "invalid",
            "hazard": None,
            "reason": "invalid_decision",
        }

    if decision == "unobservable":
        return {
            "present": False,
            "valid": True,
            "observable": False,
            "decision": "unobservable",
            "hazard": None,
            "reason": str(
                fusion.get("reason") or "insufficient_visual_evidence"
            ),
            "visual_evidence": fusion.get(
                "visual_evidence", []
            ) or [],
            "missing_evidence": fusion.get(
                "missing_evidence", []
            ) or [],
        }

    if decision == "absent":
        return {
            "present": False,
            "valid": True,
            "observable": True,
            "decision": "absent",
            "hazard": None,
            "reason": str(
                fusion.get("reason") or "model_no_hazard"
            ),
            "visual_evidence": fusion.get(
                "visual_evidence", []
            ) or [],
            "missing_evidence": fusion.get(
                "missing_evidence", []
            ) or [],
        }

    hazard = fusion.get("hazard")

    if not isinstance(hazard, dict):
        return {
            "present": False,
            "valid": False,
            "observable": False,
            "decision": "invalid",
            "hazard": None,
            "reason": "present_without_hazard",
        }

    hz = hazard

    # 分类字段必须以问题分级表为准，不采用模型自由生成的分类名称。
    for key in [
        "category1_code", "category1", "category2_code", "category2",
        "issue_id", "category3", "regulation_index_raw",
        "rectification_advice",
    ]:
        hz[key] = candidate.get(key, "")

    hz["regulation_refs"] = _prepare_regulation_refs(hz, kb_text)
    hz["violations"] = _verified_violations_from_kb(hz, kb_text)
    hz["review_required"] = bool(hz["regulation_refs"])
    return {
        "present": True,
        "valid": True,
        "observable": True,
        "decision": "present",
        "hazard": hz,
        "reason": str(fusion.get("reason") or ""),
        "visual_evidence": fusion.get(
            "visual_evidence", []
        ) or [],
        "missing_evidence": fusion.get(
            "missing_evidence", []
        ) or [],
    }


# Fusion采用三状态复核：
# - present：现场图存在与候选问题相符的可见或疑似现象；
# - absent：现场图清楚展示相关对象，且状态与候选问题明显不符；
# - unobservable：相关对象或检查区域没有出现在图片中。
# Fusion最多运行3轮，采用自适应2+1：前两轮一致时提前结束，
# 否则执行第三轮；单个present仅在其余结果未形成两票absent时低置信度保留。
async def ensemble_fusion_for_type_async(
    scene_url: str,
    kb_text: str,
    kb_image_url: str,
    candidate: dict,
    rounds: int,
    model_sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
):
    temperatures = FS_FUSION_TEMP_LIST or [0.2]
    max_rounds = max(1, min(3, int(rounds or 0)))

    def temperature_for(index: int) -> float:
        return temperatures[index % len(temperatures)]

    trace_log(
        "Fusion",
        f"开始复核候选{candidate.get('issue_id', '')}",
        {
            "category3": candidate.get("category3", ""),
            "max_rounds": max_rounds,
            "strategy": "adaptive_2_plus_1",
            "temperatures": [temperature_for(index) for index in range(max_rounds)],
            "stage_a_score": candidate.get("stage_a_score"),
            "stage_a_count": candidate.get("stage_a_count", 0),
            "stage_a_rounds": candidate.get("stage_a_rounds", 0),
            "stage_a_direct_count": candidate.get("stage_a_direct_count", 0),
            "kb_score": candidate.get("_kb_score", 0.0),
            "has_kb_reference_image": bool(kb_image_url),
        },
    )

    first_tasks = [
        _doubao_fusion(
            client,
            model_sem,
            scene_url,
            kb_text,
            kb_image_url,
            candidate,
            temperature=temperature_for(index),
        )
        for index in range(min(2, max_rounds))
    ]
    outputs = list(
        await asyncio.gather(*first_tasks, return_exceptions=True)
    )

    def _early_vote_counts(items) -> Tuple[int, int]:
        present = 0
        absent = 0
        for item in items:
            if not isinstance(item, dict) or not item.get("valid", False):
                continue
            decision = str(item.get("decision") or "").strip().lower()
            if decision == "present" and item.get("hazard"):
                present += 1
            elif decision == "absent":
                absent += 1
        return present, absent

    early_present, early_absent = _early_vote_counts(outputs)
    if early_present >= 2:
        early_stop_reason = "two_present"
    elif early_absent >= 2:
        early_stop_reason = "two_absent"
    elif max_rounds >= 3:
        early_stop_reason = "need_third_round"
        try:
            third_output = await _doubao_fusion(
                client,
                model_sem,
                scene_url,
                kb_text,
                kb_image_url,
                candidate,
                temperature=temperature_for(2),
            )
        except Exception as exc:
            third_output = exc
        outputs.append(third_output)
    else:
        early_stop_reason = "max_rounds_reached"

    votes_present = 0
    votes_absent = 0
    votes_unobservable = 0
    valid_rounds = 0
    observable_rounds = 0
    accepted: List[dict] = []

    for index, output in enumerate(outputs, 1):
        if isinstance(output, Exception):
            detail = getattr(output, "detail", None)
            status_code = getattr(output, "status_code", None)
            trace_log(
                "Fusion-Round",
                f"{candidate.get('issue_id', '')} 第{index}轮异常",
                {
                    "type": type(output).__name__,
                    "status_code": status_code,
                    "detail": detail,
                    "repr": repr(output),
                },
            )
            continue

        if not isinstance(output, dict) or not output.get("valid", False):
            reason = (
                str(output.get("reason") or "invalid_output")
                if isinstance(output, dict)
                else "non_dict_output"
            )
            debug_log(
                f"[Fusion] {candidate.get('issue_id', '')} "
                f"第{index}轮无效：{reason}"
            )
            continue

        valid_rounds += 1
        decision = str(output.get("decision") or "").strip().lower()

        if decision == "unobservable":
            votes_unobservable += 1
            debug_log(
                f"[Fusion] {candidate.get('issue_id', '')} "
                f"第{index}轮：unobservable，"
                f"reason={output.get('reason', '')}"
            )
            continue

        # present和absent都表示当前图片具备判断该候选问题的条件。
        observable_rounds += 1

        if decision == "present" and output.get("hazard"):
            votes_present += 1
            accepted.append(output["hazard"])
            debug_log(
                f"[Fusion] {candidate.get('issue_id', '')} "
                f"第{index}轮：present"
            )
        elif decision == "absent":
            votes_absent += 1
            debug_log(
                f"[Fusion] {candidate.get('issue_id', '')} "
                f"第{index}轮：absent，"
                f"reason={output.get('reason', '')}"
            )
        else:
            # 理论上不会进入这里；防止模型返回结构异常却被计入有效反对票。
            valid_rounds -= 1
            observable_rounds -= 1
            debug_log(
                f"[Fusion] {candidate.get('issue_id', '')} "
                f"第{index}轮状态异常：decision={decision}"
            )

    stage_a_count = int(candidate.get("stage_a_count", 0) or 0)
    stage_a_direct_count = int(
        candidate.get("stage_a_direct_count", 0) or 0
    )
    stage_a_rounds = max(
        1,
        int(candidate.get("stage_a_rounds", FS_STAGEA_ROUNDS) or 0),
    )
    kb_score = _clamp01(candidate.get("_kb_score", 0.0))

    raw_stage_a_score = candidate.get("stage_a_score")
    if raw_stage_a_score in (None, ""):
        stage_a_score = None
    else:
        stage_a_score = _clamp01(raw_stage_a_score)

    # 置信度的Fusion分母只使用可判断轮次。
    # unobservable、HTTP失败、解析失败均不应被当成反对票。
    confidence = _compute_hazard_confidence(
        stage_a_count=stage_a_count,
        stage_a_rounds=stage_a_rounds,
        fusion_count=votes_present,
        fusion_rounds=max(1, observable_rounds),
        kb_score=kb_score,
        stage_a_score=stage_a_score,
    )

    # 两票一致优先决策；无法形成两票一致且存在单个present时，
    # 仅按歧义结果低置信度保留。两票absent始终过滤。
    threshold = 2
    if votes_present >= 2 and accepted:
        decided = True
        retention_rule = (
            "two_present_early"
            if early_stop_reason == "two_present"
            else "majority_present"
        )
    elif votes_absent >= 2:
        decided = False
        retention_rule = (
            "two_absent_early"
            if early_stop_reason == "two_absent"
            else "majority_absent"
        )
    elif votes_present >= 1 and accepted:
        decided = True
        retention_rule = "single_present_ambiguous"
        if votes_absent == 1:
            confidence = min(confidence, 0.42)
        else:
            confidence = min(confidence, 0.50)
    else:
        decided = False
        retention_rule = "rejected"

    confidence = round(_clamp01(confidence), 4)

    # 完全没有可判断轮次时才将置信度清零。
    if observable_rounds <= 0:
        confidence = 0.0

    trace_log(
        "Fusion-Summary",
        f"候选{candidate.get('issue_id', '')}投票、置信度与保留结论",
        {
            "category3": candidate.get("category3", ""),
            "stage_a_score": stage_a_score,
            "stage_a_count": f"{stage_a_count}/{stage_a_rounds}",
            "stage_a_direct_count": stage_a_direct_count,
            "kb_score": kb_score,
            "present_count": votes_present,
            "absent_count": votes_absent,
            "unobservable_count": votes_unobservable,
            "executed_rounds": len(outputs),
            "early_stop_reason": early_stop_reason,
            "valid_rounds": valid_rounds,
            "observable_rounds": observable_rounds,
            "retention_threshold": threshold,
            "retention_rule": retention_rule,
            "retained": decided,
            "confidence": confidence,
            "accepted_hazard_count": len(accepted),
        },
    )

    best_hazard = None
    if decided and accepted:
        best_hazard = max(
            accepted,
            key=lambda hazard: (
                len(hazard.get("regulation_refs", [])),
                len(hazard.get("violations", [])),
                len(hazard.get("description", "")),
            ),
        )
        # confidence与point、description等字段处于同一hazard对象层级。
        # 该值由代码根据StageA证据、Fusion可判断投票和KB相关度计算。
        best_hazard["confidence"] = confidence

    return {
        "category1_code": candidate.get("category1_code", ""),
        "category1": candidate.get("category1", ""),
        "category2_code": candidate.get("category2_code", ""),
        "category2": candidate.get("category2", ""),
        "issue_id": candidate.get("issue_id", ""),
        "category3": candidate.get("category3", ""),
        "rectification_advice": candidate.get("rectification_advice", ""),
        "confidence": confidence,
        "present_count": votes_present,
        "absent_count": votes_absent,
        "unobservable_count": votes_unobservable,
        "valid_rounds": valid_rounds,
        "observable_rounds": observable_rounds,
        # rounds作为present_count的展示分母，使用可判断轮次。
        "rounds": observable_rounds,
        "threshold": threshold,
        "retention_rule": retention_rule,
        "present": decided,
        "hazard": best_hazard,
    }


# ============== 主接口（异步并发） ==============
@app.post("/analyze/fire-safety/guess-types")
async def guess_hazard_types_and_kb(image: UploadFile = File(...)):
    """
    简化流程：
    - Stage0：判断是否属于建筑消防巡检场景；
    - StageA：直接从问题分级表输出5个一级/二级/三级候选；
    - KB：按 issue_id 和完整三级路径检索案例；
    - Fusion：每个候选最多3轮，采用自适应2+1；前两轮一致则提前结束，否则执行第三轮；
    - Clause KB：根据案例库条文号生成 record_id，精确查询规范原文。
    """
    key = upload_image_to_tos(image)
    url = _presigned_get_url(key, expires=600)
    trace_tokens = set_trace_context(f"direct-{key[-16:]}", key)
    trace_log("Pipeline", "通过app.py直连接口开始分析")

    catalog, allowed_text = _get_kb_categories()

    timeout = httpx.Timeout(FS_HTTP_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        model_sem = asyncio.Semaphore(FS_MODEL_CONCURRENCY)
        kb_sem = asyncio.Semaphore(FS_KB_CONCURRENCY)

        stage0_result = {"relevant": True,"confidence": 1.0,"scene_tags": [],"reason": "disabled",}
        if FS_STAGE0_ENABLED:
            stage0_result = await run_stage0_guard_async(client, model_sem, url)

        debug_log(f"[Stage0] result = {stage0_result}")

        if FS_STAGE0_ENABLED and (not stage0_result.get("relevant", False)
            or float(stage0_result.get("confidence", 0.0)) < FS_STAGE0_THRESHOLD
        ):
            debug_log("[Stage0] 非建筑/消防相关或置信度不足，短路返回空结果。")
            return JSONResponse(
                build_stage0_rejected_pipeline_payload(key, url, stage0_result)
            )

        selected_types, type_votes, per_round = await ensemble_stageA_async(
            url,allowed_text,catalog,
            rounds=FS_STAGEA_ROUNDS,temps=FS_TEMP_LIST,
            model_sem=model_sem,client=client,
        )

        debug_log("[StageA] 进入后续处理的一级/二级/三级候选：")
        for index, candidate in enumerate(selected_types, 1):
            debug_log(
                f"  {index}. {candidate['category1']} - {candidate['category2']} - "
                f"{candidate['issue_id']} {candidate['category3']}"
            )

        # Stage0已经通过，但StageA三轮均未留下有效白名单候选：
        # 不再直接等同于“无隐患”，而是执行一次开放式模型复核。
        if not selected_types:
            fallback_result = await run_catalog_unmatched_fallback_async(
                client,
                model_sem,
                url,
            )
            return JSONResponse(
                build_catalog_unmatched_pipeline_payload(
                    key,
                    url,
                    stage0_result,
                    fallback_result,
                    type_votes,
                )
            )

        kb_results = await search_kb_for_types_async(
            selected_types,topk=KB_PER_TYPE_TOPK,kb_sem=kb_sem,client=client,
        )

        debug_log("[StageB] KB 命中摘要：")
        for item in kb_results:
            hit = (
                item["results"][0]["content"][:120].replace("\n", " ") + "..."
                if item.get("results") else "(无结果)"
            )
            debug_log(f"  - {item['issue_id']} {item['category3']} => {hit}")

        fusion_tasks = []
        for candidate in selected_types:
            kb_pack = next(
                (item for item in kb_results if item.get("issue_id") == candidate.get("issue_id")),
                None,
            )
            if not kb_pack or not kb_pack.get("results"):
                trace_log(
                    "Fusion-Skip",
                    f"跳过（无KB证据）：{candidate['issue_id']} {candidate['category3']}",
                )

                fusion_tasks.append(build_skipped_fusion_result_async(candidate))
                continue

            kb_item = kb_pack["results"][0]
            kb_text = kb_item.get("content", "") or ""
            kb_image = (kb_item.get("images") or [None])[0]
            kb_score = _clamp01(kb_item.get("score", 0.0))

            # 使用下划线字段，只在内部计算中使用，不进入最终hazard和MongoDB。
            fusion_candidate = dict(candidate)
            fusion_candidate["_kb_score"] = kb_score

            fusion_tasks.append(
                ensemble_fusion_for_type_async(
                    url,
                    kb_text,
                    kb_image,
                    fusion_candidate,
                    rounds=FS_FUSION_ROUNDS,
                    model_sem=model_sem,
                    client=client,
                )
            )

        fusion_results = await asyncio.gather(*fusion_tasks, return_exceptions=True)

        # 在同一客户端内汇总 Fusion，并查询规范条文知识库。
        final_hazards, fusion_votes, clause_kb_results = (
            await collect_fusion_results_and_enrich_clause_kb_async(
                fusion_results,
                client,
                log_stage_c=lambda message: trace_log("StageC", message),
                log_task_error=lambda message: trace_log("Fusion-Task", message),
            )
        )

    final_result = build_matched_final_result(
        final_hazards,
        catalog_match_status="matched" if selected_types else "no_stageA_match",
    )
    return JSONResponse(
        build_completed_pipeline_payload(
            key,
            url,
            stage0_result,
            selected_types,
            kb_results,
            clause_kb_results,
            final_result,
            type_votes,
            fusion_votes,
        )
    )


# （可选）本地直接运行
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
