# app.py
import os
import re
import time
import unicodedata
import requests
from urllib.parse import urlparse, quote as urlquote, quote
from flask import (
    Flask, request, jsonify, Response, stream_with_context,
    abort, send_from_directory
)

app = Flask(__name__)

# -------- 基础路由（前端静态页） --------
@app.get("/")
def home():
    return send_from_directory("static", "index.html")

# -------- 会话/头/登录态 --------
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
BASE_HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}

SESSION = requests.Session()
SESSION.headers.update(BASE_HEADERS)

# 可选：后端注入登录态（拿更高码率/减少403）
if os.getenv("BILI_SESSDATA"):
    SESSION.cookies.update({"SESSDATA": os.getenv("BILI_SESSDATA")})

# -------- 允许的上游域名（避免被当开放代理）--------
ALLOWED_SUFFIXES = (
    ".bilivideo.com",   # upos-sz-*.bilivideo.com
    ".bilivideo.cn",    # mcdn.bilivideo.cn 等
    ".hdslb.com",
    ".bilibili.com",
)
ALLOWED_EXACT = {"api.bilibili.com", "www.bilibili.com"}

def host_allowed(url: str) -> bool:
    try:
        h = urlparse(url).hostname or ""
        if h in ALLOWED_EXACT:
            return True
        return any(h.endswith(suf) for suf in ALLOWED_SUFFIXES)
    except Exception:
        return False

# -------- 简易缓存（bvid -> 结果，10 分钟）--------
CACHE = {}

# -------- 工具：文件类型与文件名处理 --------
def guess_mime_by_name(name: str):
    if not name:
        return None
    low = name.lower()
    if low.endswith(".m4a"): return "audio/mp4"
    if low.endswith(".webm"): return "audio/webm"
    if low.endswith(".mp3"): return "audio/mpeg"
    if low.endswith(".aac"): return "audio/aac"
    return None

def ascii_fallback(name: str) -> str:
    """把中文/特殊字符名转为安全 ASCII 作为 filename 回退（同时发送 filename* 为 UTF-8 正式名）"""
    if not name:
        return "audio.m4a"
    base, ext = (name.rsplit(".", 1) + [""])[:2]
    norm = unicodedata.normalize("NFKD", base)
    ascii_base = "".join(ch for ch in norm if ord(ch) < 128)
    ascii_base = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_base).strip("_") or "audio"
    ext = ext or "m4a"
    return f"{ascii_base}.{ext}"

# -------- B站 API --------
def extract_bvid(text: str):
    text = (text or "").strip()
    m = re.search(r"(BV[0-9A-Za-z]+)", text)
    return m.group(1) if m else None

def get_cid_by_bvid(bvid: str) -> dict:
    url = "https://api.bilibili.com/x/web-interface/view"
    r = SESSION.get(url, params={"bvid": bvid}, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"view api failed: {data}")
    return data["data"]

def get_playurl(bvid: str, cid: int) -> dict:
    # fnval=16: DASH，audio 列在 dash.audio[]
    url = "https://api.bilibili.com/x/player/playurl"
    params = {"bvid": bvid, "cid": cid, "fnval": 16, "fourk": 1}
    r = SESSION.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"playurl api failed: {data}")
    return data["data"]

# -------- API：解析音频清单 --------
@app.get("/api/parse")
def api_parse():
    raw = request.args.get("url", "")
    bvid = extract_bvid(raw)
    if not bvid:
        return jsonify({"error": "请提供有效的 BV 号或视频链接"}), 400

    now = time.time()
    if bvid in CACHE and now - CACHE[bvid][0] < 600:
        return jsonify(CACHE[bvid][1])

    try:
        view = get_cid_by_bvid(bvid)
        pages = view.get("pages", [])
        if not pages:
            return jsonify({"error": "未找到页面/cid"}), 404

        results = []
        for p in pages:
            cid = p["cid"]
            title = p.get("part") or p.get("title") or ""
            play = get_playurl(bvid, cid)
            dash = play.get("dash", {})
            audios = dash.get("audio", [])  # 每个有 baseUrl/bandwidth/mimeType/codecs/id 等
            items = []
            for a in audios:
                base = a.get("baseUrl", "")
                items.append({
                    "id": a.get("id"),
                    "bandwidth": a.get("bandwidth"),
                    "mimeType": a.get("mimeType"),
                    "codecs": a.get("codecs"),
                    "direct_url": base,
                    "proxy_url": f"/audio?u={quote(base, safe='')}",
                })
            items.sort(key=lambda x: x["bandwidth"] or 0, reverse=True)
            results.append({
                "p": p.get("page", 1),
                "title": title,
                "cid": cid,
                "audios": items
            })

        payload = {"bvid": bvid, "title": view.get("title"), "pages": results}
        CACHE[bvid] = (now, payload)
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# -------- 代理下载：带文件名/类型/Range；支持 norange=1 降级 --------
@app.get("/audio")
def proxy_audio():
    target = request.args.get("u", "")
    name = request.args.get("name")  # 前端给的保存文件名（含后缀）
    no_range = request.args.get("norange") == "1"

    if not target:
        return abort(400, "missing u")
    if not host_allowed(target):
        return abort(400, "upstream host not allowed")

    try:
        # 透传 Range（可用 ?norange=1 关闭，排查某些 CDN 的 206/Content-Range 异常）
        fwd_headers = {}
        if (not no_range) and ("Range" in request.headers):
            fwd_headers["Range"] = request.headers["Range"]

        upstream = SESSION.get(target, stream=True, timeout=20, headers=fwd_headers)
        status = upstream.status_code
        # 调试观测（可注释）
        print("UPSTREAM", status, upstream.headers.get("Content-Type"), target[:110])

        if status >= 400:
            return Response(upstream.content, status=status)

        def generate():
            for chunk in upstream.iter_content(chunk_size=1024 * 256):
                if chunk:
                    yield chunk

        # ---- 构造下游响应头 ----
        resp_headers = {}

        # Content-Type：优先上游，其次按 name 猜
        ct = upstream.headers.get("Content-Type") or guess_mime_by_name(name or "")
        if ct:
            resp_headers["Content-Type"] = ct

        # Content-Disposition：兼容中文名（filename + RFC5987 filename*）
        if name:
            safe_name = re.sub(r'[\\/:*?"<>|]', "_", name)
            ascii_name = ascii_fallback(safe_name)
            disp = f"attachment; filename={ascii_name}; filename*=UTF-8''{urlquote(safe_name)}"
            resp_headers["Content-Disposition"] = disp

        # 分段/范围相关：尽量透传；无则补 Accept-Ranges: bytes
        if upstream.headers.get("Accept-Ranges"):
            resp_headers["Accept-Ranges"] = upstream.headers["Accept-Ranges"]
        else:
            resp_headers["Accept-Ranges"] = "bytes"
        for h in ("Content-Length", "Content-Range", "ETag", "Last-Modified"):
            v = upstream.headers.get(h)
            if v:
                resp_headers[h] = v

        # 某些 CDN 返回 206 但无 Content-Range，降级为 200 以避免浏览器判失败
        resp_status = status
        if status == 206 and not upstream.headers.get("Content-Range"):
            resp_status = 200

        return Response(stream_with_context(generate()), headers=resp_headers, status=resp_status)
    except Exception as e:
        return abort(502, f"upstream error: {e}")

# -------- 入口 --------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5173"))
    app.run(host="0.0.0.0", port=port, debug=True)
