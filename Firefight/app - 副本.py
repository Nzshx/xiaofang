import os
import json
import math
import difflib
from typing import Dict, List, Tuple

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

TOS_ENDPOINT = os.getenv("TOS_ENDPOINT", "tos-cn-beijing.volces.com")
TOS_BUCKET = os.getenv("TOS_BUCKET", "ark-auto-2106575407-cn-beijing-default")
TOS_REGION = os.getenv("TOS_REGION", "cn-beijing")
TOS_ACCESS_KEY = os.getenv("TOS_ACCESS_KEY", "")
TOS_SECRET_KEY = os.getenv("TOS_SECRET_KEY", "")

DOUBAO_API_URL = os.getenv("DOUBAO_API_URL", "https://ark.cn-beijing.volces.com/api/v3/responses")
DOUBAO_API_KEY = os.getenv("DOUBAO_API_KEY", "")
DOUBAO_MODEL = os.getenv("DOUBAO_MODEL", "doubao-seed-2-0-pro-260215")

KB_CATEGORIES_XLSX = os.getenv("KB_CATEGORIES_XLSX", "knowledge_base.xlsx")
KB_SHEET_NAME = os.getenv("KB_SHEET_NAME", "数据集收集")
KB_COL_CAT1 = os.getenv("KB_COL_CAT1", "隐患大类（一级分类）")
KB_COL_CAT2 = os.getenv("KB_COL_CAT2", "隐患小类（二级分类）")

KB_DOMAIN = os.getenv("KB_DOMAIN", "api-knowledgebase.mlp.cn-beijing.volces.com")
KB_SERVICE_RESOURCE_ID = os.getenv("KB_SERVICE_RESOURCE_ID", "kb-service-6bb0cf1193158174")
KB_API_KEY = os.getenv("KB_API_KEY", "")

KB_HTTP_TIMEOUT = int(os.getenv("KB_HTTP_TIMEOUT", "30"))
KB_PER_TYPE_TOPK = int(os.getenv("KB_PER_TYPE_TOPK", "1"))

# —— 并发与超时 —— #
FS_STAGEA_ROUNDS = int(os.getenv("FS_STAGEA_ROUNDS", "3"))
FS_FUSION_ROUNDS = int(os.getenv("FS_FUSION_ROUNDS", "3"))
FS_MAX_TYPES     = int(os.getenv("FS_MAX_TYPES", "5"))
FS_TEMP_LIST     = [float(x.strip()) for x in os.getenv("FS_TEMP_LIST", "0.6,0.8,1.0").split(",") if x.strip()]
FS_MODEL_CONCURRENCY = int(os.getenv("FS_MODEL_CONCURRENCY", "4"))
FS_KB_CONCURRENCY    = int(os.getenv("FS_KB_CONCURRENCY", "5"))
FS_HTTP_TIMEOUT      = int(os.getenv("FS_HTTP_TIMEOUT", "300"))

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

# ============== Excel 分类 ==============
_KB_CATEGORY_MAPPING: Dict[str, List[str]] = None
_KB_ALLOWED_TEXT: str = None

def _load_category_mapping_from_excel(xlsx_path: str, sheet: str) -> Dict[str, List[str]]:
    try:
        wb = load_workbook(xlsx_path, data_only=True)
        if sheet not in wb.sheetnames:
            raise ValueError(f"找不到工作表：{sheet}")
        ws = wb[sheet]

        headers = {}
        for c in range(1, ws.max_column + 1):
            val = ws.cell(row=1, column=c).value
            if isinstance(val, str):
                headers[val.strip()] = c
        if KB_COL_CAT1 not in headers or KB_COL_CAT2 not in headers:
            raise ValueError(f"缺少列：{KB_COL_CAT1} 或 {KB_COL_CAT2}")

        c1_idx, c2_idx = headers[KB_COL_CAT1], headers[KB_COL_CAT2]
        mapping = {}
        for r in range(2, ws.max_row + 1):
            c1 = ws.cell(row=r, column=c1_idx).value
            c2 = ws.cell(row=r, column=c2_idx).value
            if c1 is None:
                continue
            c1 = str(c1).strip()
            if not c1 or c1.lower() == "nan":
                continue
            mapping.setdefault(c1, set())
            if c2 is not None:
                s2 = str(c2).strip()
                if s2 and s2.lower() != "nan":
                    mapping[c1].add(s2)
        return {k: sorted(list(v)) for k, v in sorted(mapping.items(), key=lambda x: x[0])}
    except Exception as e:
        print("[ERROR] 加载Excel分类失败：", e)
        raise HTTPException(status_code=500, detail="服务器未正确加载 Excel 分类清单")

def _render_allowed_categories_text(mapping: Dict[str, List[str]]) -> str:
    lines = ["一级分类/小类清单："]
    for c1, subs in mapping.items():
        lines.append(f"- {c1}")
        lines.append("  小类：" + ("｜".join(subs) if subs else "无"))
    return "\n".join(lines)

def _get_kb_categories() -> Tuple[Dict[str, List[str]], str]:
    global _KB_CATEGORY_MAPPING, _KB_ALLOWED_TEXT
    if _KB_CATEGORY_MAPPING is None:
        if not os.path.exists(KB_CATEGORIES_XLSX):
            raise HTTPException(status_code=500, detail=f"KB_CATEGORIES_XLSX 不存在：{KB_CATEGORIES_XLSX}")
        _KB_CATEGORY_MAPPING = _load_category_mapping_from_excel(KB_CATEGORIES_XLSX, KB_SHEET_NAME)
        _KB_ALLOWED_TEXT = _render_allowed_categories_text(_KB_CATEGORY_MAPPING)
        print("[INFO] 已加载分类清单：", len(_KB_CATEGORY_MAPPING), "个一级分类")
    return _KB_CATEGORY_MAPPING, _KB_ALLOWED_TEXT

# ============== 通用工具 ==============
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
    kb_norm = _normalize_ws(kb_text)
    for v in hazard.get("violations", []) or []:
        content = _normalize_ws(v.get("content", ""))
        if not content or content not in kb_norm:
            return False
    return True if hazard.get("violations") else False

# ============== 【新增】Stage0 场景筛查（异步） ==============
def _build_stage0_prompt() -> str:
    # 低温度、严格 JSON。给出正例元素，尽量抑制“万物皆有隐患”的错判。
    return (
        "请判断这张图片是否与【建筑/消防安全巡检】场景相关。若为自然风景、人物自拍、食物、动物、纯图标、文本截图、汽车内饰等非建筑巡检场景，请判为不相关。\n"
        "相关场景通常包含：建筑外部或周边环境/走廊/房间/楼梯/门窗/墙面/天花、管线/风管/电缆桥架、喷淋/消火栓/消防水带/灭火器、泵房/配电柜/控制柜、应急照明/疏散指示等。\n"
        "仅输出严格 JSON（不要代码块、不要解释）：\n"
        "{\n"
        '  "relevant": true,\n'
        '  "confidence": 0.0,\n'
        '  "scene_tags": ["室内","走廊","喷淋","配电柜"],\n'
        '  "reason": "一句话理由"\n'
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
        print(f"[Stage0] 调用失败：HTTP {r.status_code} {r.text[:120]}")
        return {"relevant": True, "confidence": 1.0, "scene_tags": [], "reason": "api_fallback_true"}
    raw = _extract_response_text(r.json())
    out = _parse_stage0_json(raw)
    print(f"[Stage0] 判定：relevant={out['relevant']} conf={out['confidence']:.2f} tags={out['scene_tags'][:4]} reason={out['reason']}")
    return out

# ============== StageA（异步） ==============
def _build_stageA_prompt(allowed_text: str, run_tag: str = "") -> str:
    tag = f"\n【抽样序号】{run_tag}\n" if run_tag else ""
    return (
        "你是一名消防隐患图像分析助手。请基于现场图片，识别【至少5条，最多8条】“疑似或者确定的隐患”。\n"
        "每条隐患必须选择【隐患大类 / 隐患小类】，且只能从下面清单中选择（严禁超出或自造；同一小类不得重复）：\n\n"
        f"{allowed_text}\n\n"
        "仅输出 JSON，不要解释或代码块：\n"
        "{\n"
        '  "hazard_queries": [\n'
        "    {\n"
        '      "category1": "从上面的一级分类中选择",\n'
        '      "category2": "从该一级分类对应的小类中选择（不得与其他条重复）",\n'
        '      "keywords": ["1~4个中文短语"]\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "若图中只明显出现1类隐患，请在同一大类下给出4~5个【最可能的小类】（基于相近特征或常见伴生问题），确保总数≥5。"
        + tag
    )

async def _doubao_chat_image(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    image_url: str,
    prompt_text: str,
    temperature: float = 0.7
) -> str:
    payload = _build_responses_payload(
        prompt_text=prompt_text,
        image_urls=[image_url],
        temperature=temperature
    )
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {DOUBAO_API_KEY}"}
    async with sem:
        r = await client.post(DOUBAO_API_URL, headers=headers, json=payload)
    if r.status_code != 200:
        raise HTTPException(status_code=500, detail=f"StageA 调用失败：{r.text[:200]}")
    return _extract_response_text(r.json())

def _parse_stageA_types(raw_text: str, mapping: Dict[str, List[str]]) -> List[Dict[str, str]]:
    raw = _extract_json(raw_text)
    data = json.loads(raw)
    hqs = data.get("hazard_queries", []) or []
    valid, seen = [], set()
    for it in hqs:
        c1 = str(it.get("category1", "")).strip()
        c2 = str(it.get("category2", "")).strip()
        if not c1 or c1 not in mapping:
            continue
        sub_ok = ""
        if mapping[c1]:
            if c2 in mapping[c1]:
                sub_ok = c2
            else:
                alt = _closest(c2, mapping[c1], cutoff=0.65) if c2 else None
                if alt:
                    sub_ok = alt
                elif len(mapping[c1]) == 1:
                    sub_ok = mapping[c1][0]
                else:
                    continue
        key = f"{c1}||{sub_ok}"
        if key not in seen:
            valid.append({"category1": c1, "category2": sub_ok})
            seen.add(key)
    return valid

async def ensemble_stageA_async(image_url: str, allowed_text: str, mapping: Dict[str, List[str]],
                                rounds: int, temps: List[float],
                                model_sem: asyncio.Semaphore,
                                client: httpx.AsyncClient):
    print(f"[Ensemble-StageA] 并发采样：rounds={rounds}, temps={temps}")
    tasks = []
    for i in range(rounds):
        temp = temps[i % len(temps)]
        tag = f"R{i+1}-T{temp}"
        prompt = _build_stageA_prompt(allowed_text, run_tag=tag)
        tasks.append(_doubao_chat_image(client, model_sem, image_url, prompt, temperature=temp))
    texts = await asyncio.gather(*tasks, return_exceptions=True)

    freq: Dict[str, int] = {}
    per_round: List[List[Dict[str, str]]] = []
    for idx, res in enumerate(texts, 1):
        if isinstance(res, Exception):
            print(f"[StageA] 第{idx}轮失败：{res}")
            per_round.append([])
            continue
        try:
            types_i = _parse_stageA_types(res, mapping)
        except Exception as e:
            print(f"[StageA] 第{idx}轮解析失败：{e}")
            per_round.append([])
            continue
        per_round.append(types_i)
        print(f"[StageA] 第 {idx} 轮识别：")
        for t in types_i:
            key = f"{t['category1']}||{t['category2']}"
            freq[key] = freq.get(key, 0) + 1
            print(f"  - {t['category1']} - {t['category2']}")

    votes = [{"category1": k.split("||")[0], "category2": k.split("||")[1], "count": c}
             for k, c in sorted(freq.items(), key=lambda x: (-x[1], x[0]))]
    print("[StageA] 频次统计：")
    for v in votes:
        print(f"  {v['category1']} - {v['category2']} : {v['count']}")
    selected = [{"category1": v["category1"], "category2": v["category2"]} for v in votes[:FS_MAX_TYPES]]
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

def _build_kb_query(c1: str, c2: str) -> str:
    t = f"隐患类型：{c1}-{c2}；" if c2 else f"隐患类型：{c1}；"
    return (
        t +
        "请返回与该隐患高度相关的：\n"
        "- 隐患描述（专业、简洁）；\n"
        "- 对应规范条文“官方原文”（必须是原文，禁止改写）；\n"
        "- 示例图片（如有）。\n"
        "优先包含《建筑设计防火规范》GB 50016-2014(2018版)及相关国家/地方/专项标准的官方原文。"
    )

async def kb_chat_async(client: httpx.AsyncClient, sem: asyncio.Semaphore, query_text: str) -> dict:
    url = f"http://{KB_DOMAIN}/api/knowledge/service/chat"
    payload = {
        "service_resource_id": KB_SERVICE_RESOURCE_ID,
        "messages": [{"role": "user", "content": query_text}],
        "stream": False
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

async def search_kb_for_types_async(types: List[Dict[str, str]], topk: int,
                                    kb_sem: asyncio.Semaphore, client: httpx.AsyncClient):
    tasks = []
    for t in types:
        q = _build_kb_query(t["category1"], t["category2"])
        tasks.append(kb_chat_async(client, kb_sem, q))

    results = []
    packs = await asyncio.gather(*tasks, return_exceptions=True)
    for idx, p in enumerate(packs):
        c1, c2 = types[idx]["category1"], types[idx]["category2"]
        if isinstance(p, Exception):
            print(f"[KB] {c1}-{c2} 失败：{p}")
            results.append({"category1": c1, "category2": c2, "results": []})
            continue
        rl = (((p or {}).get("data") or {}).get("result_list")) or []
        pack = []
        for entry in rl[:topk]:
            text = entry.get("content", "") or ""
            score = float(entry.get("score", 0.0) or 0.0)
            imgs = []
            for att in entry.get("chunk_attachment", []) or []:
                if att.get("type") == "image" and att.get("link"):
                    imgs.append(att["link"])
            pack.append({"content": text, "images": imgs, "score": score})
        results.append({"category1": c1, "category2": c2, "results": pack})
    return results

# ============== Fusion（异步） ==============
def _build_fusion_prompt(c1: str, c2: str, kb_text: str) -> str:
    c12 = f"{c1}-{c2}" if c2 else c1
    return (
        f"请对比以下两张图片：\n"
        f"1) 第一张是待检测现场图片（Scene）。\n"
        f"2) 第二张是知识库提供的标准参考图片（Standard）。\n\n"
        f"任务：仅就隐患类型「{c12}」进行判断该隐患在现场是否存在。\n"
        f"— 如果不存在：请输出严格 JSON：{{\"hazard_count\":0, \"hazards\":[]}}\n"
        f"— 如果存在：请输出严格 JSON，且仅包含这一条隐患：\n"
        "{\n"
        '  "hazard_count": 1,\n'
        '  "hazards": [\n'
        "    {\n"
        f'      "category1": "{c1}",\n'
        f'      "category2": "{c2}",\n'
        '      "point": "隐患点（简述）",\n'
        '      "description": "隐患说明（结合现场与证据）",\n'
        '      "violations": [\n'
        '        {"standard": "标准名称与条款号", "content": "官方原文"},\n'
        '        {"standard": "其他标准（如有）", "content": "官方原文"}\n'
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "【重要硬性约束】\n"
        "1) 严禁编造或改写条文内容；所有 violations.content 必须逐字复制自下面“官方原文引用区”；\n"
        "2) 若官方原文中没有该条文，请不要输出对应 violations；\n"
        "3) 只输出 JSON，不要包含任何解释或代码块标记。\n\n"
        "【官方原文引用区】（从知识库检索得到，允许多条）\n"
        f"{_clean_kb_text(kb_text)}"
    )

async def _doubao_fusion(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    scene_url: str,
    kb_text: str,
    kb_image_url: str,
    c1: str,
    c2: str
) -> dict:
    image_urls = [scene_url]
    if kb_image_url:
        image_urls.append(kb_image_url)

    payload = _build_responses_payload(
        prompt_text=_build_fusion_prompt(c1, c2, kb_text),
        image_urls=image_urls
    )
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {DOUBAO_API_KEY}"}
    async with sem:
        r = await client.post(DOUBAO_API_URL, headers=headers, json=payload)
    if r.status_code != 200:
        return {"present": False, "hazard": None, "raw": "", "reason": f"http_{r.status_code}"}

    raw = _extract_response_text(r.json())
    try:
        fusion = json.loads(_extract_json(raw))
    except Exception:
        return {"present": False, "hazard": None, "raw": raw, "reason": "parse_error"}

    hazards = (fusion or {}).get("hazards") or []
    if (fusion or {}).get("hazard_count", 0) < 1 or not hazards:
        return {"present": False, "hazard": None, "raw": raw, "reason": "model_no_hazard"}

    hz = hazards[0]
    hz["category1"] = c1
    hz["category2"] = c2

    if not _violations_match_kb(hz, kb_text):
        return {"present": False, "hazard": None, "raw": raw, "reason": "violations_mismatch"}

    return {"present": True, "hazard": hz, "raw": raw}

async def ensemble_fusion_for_type_async(
    scene_url: str, kb_text: str, kb_image_url: str, c1: str, c2: str,
    rounds: int, model_sem: asyncio.Semaphore, client: httpx.AsyncClient
):
    tasks = [
        _doubao_fusion(client, model_sem, scene_url, kb_text, kb_image_url, c1, c2)
        for _ in range(rounds)
    ]
    outs = await asyncio.gather(*tasks, return_exceptions=True)
    votes_present = 0
    accepted: List[dict] = []
    for i, out in enumerate(outs, 1):
        if isinstance(out, Exception):
            print(f"[Fusion] {c1}-{c2} 轮 {i} 异常：{out}")
            continue
        if out.get("present") and out.get("hazard"):
            votes_present += 1
            accepted.append(out["hazard"])
    threshold = math.floor(rounds/2)
    decided = votes_present >= threshold
    best_hz = None
    if decided and accepted:
        best_hz = max(accepted, key=lambda h: (len(h.get("violations", [])), len(h.get("description", ""))))
    return {
        "category1": c1,
        "category2": c2,
        "present_count": votes_present,
        "rounds": rounds,
        "present": decided,
        "hazard": best_hz
    }

# ============== 主接口（异步并发） ==============
@app.post("/analyze/fire-safety/guess-types")
async def guess_hazard_types_and_kb(image: UploadFile = File(...)):
    """
    流水线：
    - Stage0 场景筛查（新增）
    - StageA 多轮并发 -> 取TopN
    - KB 并发检索
    - Fusion 每类多轮并发 + 多数投票
    """
    # 1) 上传图片
    key = upload_image_to_tos(image)
    url = _presigned_get_url(key, expires=600)

    # 2) 加载分类清单
    mapping, allowed_text = _get_kb_categories()

    timeout = httpx.Timeout(FS_HTTP_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        model_sem = asyncio.Semaphore(FS_MODEL_CONCURRENCY)
        kb_sem    = asyncio.Semaphore(FS_KB_CONCURRENCY)

        # ========== 【新增】Stage0：场景筛查 ==========
        stage0_result = {"relevant": True, "confidence": 1.0, "scene_tags": [], "reason": "disabled"}
        if FS_STAGE0_ENABLED:
            stage0_result = await run_stage0_guard_async(client, model_sem, url)

        # 控制台输出 Stage0 结果
        print(f"[Stage0] result = {stage0_result}")

        # 若不相关或置信度过低：直接短路返回空结果（避免非建筑图片误报）
        if FS_STAGE0_ENABLED and (not stage0_result.get("relevant", False) or
                                  float(stage0_result.get("confidence", 0.0)) < FS_STAGE0_THRESHOLD):
            print("[Stage0] 非建筑/消防相关或置信度不足，短路返回空结果。")
            return JSONResponse({
                "image_key": key,
                "image_url": url,
                "stage0": stage0_result,
                "hazard_types": [],
                "kb_results": [],
                "final_result": {"hazard_count": 0, "hazards": []},
                "type_votes": [],
                "fusion_votes": []
            })

        # 3) StageA 并发多轮
        selected_types, type_votes, per_round = await ensemble_stageA_async(
            url, allowed_text, mapping,
            rounds=FS_STAGEA_ROUNDS, temps=FS_TEMP_LIST,
            model_sem=model_sem, client=client
        )

        print("[StageA] 进入后续处理的候选：")
        for i, t in enumerate(selected_types, 1):
            print(f"  {i}. {t['category1']} - {t['category2']}")

        # 4) KB 并发检索
        kb_results = await search_kb_for_types_async(
            selected_types, topk=KB_PER_TYPE_TOPK, kb_sem=kb_sem, client=client
        )
        print("[StageB] KB 命中摘要：")
        for item in kb_results:
            c1, c2 = item["category1"], item["category2"]
            hit = item["results"][0]["content"][:120].replace("\n", " ") + "..." if item.get("results") else "(无结果)"
            print(f"  - {c1} - {c2} => {hit}")

        # 5) Fusion 并发（按类型分组并发，组内多轮并发）
        fusion_tasks = []
        for t in selected_types:
            c1, c2 = t["category1"], t["category2"]
            kb_pack = next((x for x in kb_results if x["category1"] == c1 and x["category2"] == c2), None)
            if not kb_pack or not kb_pack.get("results"):
                print(f"[Fusion] 跳过（无KB证据）：{c1} - {c2}")
                async def _noop(c1=c1, c2=c2):
                    return {"category1": c1, "category2": c2, "present_count": 0, "rounds": FS_FUSION_ROUNDS, "present": False, "hazard": None}
                fusion_tasks.append(_noop())
                continue
            kb_text = kb_pack["results"][0].get("content", "") or ""
            kb_img  = (kb_pack["results"][0].get("images") or [None])[0]
            fusion_tasks.append(
                ensemble_fusion_for_type_async(url, kb_text, kb_img, c1, c2,
                                               rounds=FS_FUSION_ROUNDS, model_sem=model_sem, client=client)
            )

        fusion_res = await asyncio.gather(*fusion_tasks, return_exceptions=True)

    final_hazards = []
    fusion_votes = []
    for r in fusion_res:
        if isinstance(r, Exception):
            print(f"[Fusion] 任务异常：{r}")
            continue
        fusion_votes.append({
            "category1": r["category1"], "category2": r["category2"],
            "present_count": r["present_count"], "rounds": r["rounds"], "present": r["present"]
        })
        status = "存在(多数票)" if r["present"] else "不存在(未过票)"
        print(f"[StageC] {r['category1']} - {r['category2']} => {status} {r['present_count']}/{r['rounds']}")
        if r["present"] and r["hazard"]:
            final_hazards.append(r["hazard"])

    final_result = {"hazard_count": len(final_hazards), "hazards": final_hazards}

    return JSONResponse({
        "image_key": key,
        "image_url": url,
        "stage0": stage0_result,
        "hazard_types": selected_types,
        "kb_results": kb_results,
        "final_result": final_result,
        "type_votes": type_votes,
        "fusion_votes": fusion_votes
    })


# （可选）本地直接运行
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))