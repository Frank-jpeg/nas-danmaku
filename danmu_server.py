#!/usr/bin/env python3
"""把 nas_danmaku 的能力包成弹弹play v2 接口，给 NipaPlay 之类的客户端当「自定义弹幕服务器」。

只需一个 Python 3.11 环境（tkinter 可导入即可，因为要复用 nas_danmaku）。
启动：
    python danmu_server.py                 # 监听 0.0.0.0:9322
    python danmu_server.py --port 9322 --host 0.0.0.0
客户端填：http://<本机局域网IP>:9322

实现的接口（客户端实际会打这几个）：
    POST /api/v2/match              文件名 -> 匹配电影
    GET  /api/v2/comment/<id>       取弹幕
    GET  /api/v2/search/anime       关键词搜片
    GET  /api/v2/search/episodes    按番剧名搜剧集
    GET  /api/v2/bangumi/<id>       剧集包（缓存用）
    GET  /healthz                   存活检查
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import threading
import time
import traceback
import unicodedata
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import nas_danmaku as nd

# 360 影视搜索对海外 IP 偶发 TLS 断连（UNEXPECTED_EOF_WHILE_READING），重试几次基本能过。
_nd_search_movies = nd.search_movies


def search_movies_retry(title, year="", *args, **kwargs):
    last = None
    for attempt in range(3):
        try:
            return _nd_search_movies(title, year, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < 2:
                time.sleep(1.5 + attempt * 1.5)
    raise last


nd.search_movies = search_movies_retry

# 部分国内站（360 影视搜索）对海外 IP 会用怪 TLS 握手，Python 默认上下文偶发
# UNEXPECTED_EOF_WHILE_READING，卡满 10s 才报错。失败后用 TLS1.2 + SECLEVEL=1 再试一次。
_orig_web_bytes = nd.web_bytes
_RELAXED_TLS = ssl.create_default_context()
_RELAXED_TLS.maximum_version = ssl.TLSVersion.TLSv1_2
try:
    _RELAXED_TLS.set_ciphers("DEFAULT@SECLEVEL=1")
except ssl.SSLError:
    pass


def web_bytes_tls_fallback(url, data=None, timeout=25, progress=None, message="下载数据", opener=None, request_headers=None):
    if opener is not None:
        return _orig_web_bytes(url, data=data, timeout=timeout, progress=progress,
                               message=message, opener=opener, request_headers=request_headers)
    try:
        return _orig_web_bytes(url, data=data, timeout=timeout, progress=progress,
                               message=message, request_headers=request_headers)
    except nd.ToolError as exc:
        if "失败或超时" not in str(exc):
            raise
        relaxed = urllib.request.build_opener(
            nd.SafeRedirect(), urllib.request.HTTPSHandler(context=_RELAXED_TLS)
        )
        return _orig_web_bytes(url, data=data, timeout=timeout, progress=progress,
                               message=message, opener=relaxed, request_headers=request_headers)


nd.web_bytes = web_bytes_tls_fallback

STATE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "NasDanmaku" / "danmu-server"
REGISTRY = STATE_DIR / "registry.json"
ID_BASE = 2_000_000_000
# B站弹幕最全，其次腾讯；爱奇艺/优酷/芒果 走备用公共库，能拿到多少算多少。
PLATFORM_ORDER = ("bilibili1", "qq", "qiyi", "youku", "imgo")
WARM_MAX = 3  # 同时进行的后台预抓取上限，再多就会自己撞上 B站 的分段限流

_lock = threading.Lock()
_registry: dict[str, dict] = {}
_by_key: dict[str, int] = {}   # 归一化「片名|年份」-> item_id，保证同一部片永远同一个 ID
_next_id = ID_BASE + 1
_warming: set[int] = set()

_PUNCT = re.compile(r"[\s\-_:：·・,，.。!！?？'\"“”‘’()（）\[\]【】《》<>／/\\|~～]+")


def norm_key(title, year="") -> str:
    """把片名压成稳定的索引键。

    客户端缓存弹幕是按 episodeId 存本地文件的，同一个文件每次匹配都拿新 ID 就永远
    命中不了缓存 —— 断网时自然什么都看不了。所以「同一部片 = 同一个 ID」是硬要求。

    归一化要吃掉全半角、标点、空格、繁简以外的差异不太好办就交给原文，剩下这些足够：
    「让子弹飞」「让子弹飞 (2010)」「 让子弹飞.」都归到同一条。
    """
    text = unicodedata.normalize("NFKC", str(title or "")).lower()
    text = _PUNCT.sub("", text)
    # 片名里自带的年份后缀也去掉，年份统一走第二个参数。
    # 但《2012》这种片名本身就是数字，剥完变成空串会把一堆片子挤到同一个 key 上。
    stripped = re.sub(r"(19|20)\d{2}$", "", text)
    if stripped:
        text = stripped
    if not text:
        text = str(title or "").strip().lower() or "_"
    return f"{text}|{str(year or '').strip()}"


def _scan_max_cache_id() -> int:
    """状态目录里已有 comments-<id>.json 的最大 ID。

    registry.json 万一丢了，_next_id 会退回起点，新分配的 ID 就可能撞上这些旧缓存文件，
    结果就是「匹配的是 A 片，放出来的是 B 片弹幕」。拿它兜个底。
    """
    top = ID_BASE
    try:
        for path in STATE_DIR.glob("comments-*.json"):
            stem = path.stem.split("-", 1)[-1]
            if stem.isdigit():
                top = max(top, int(stem))
    except OSError:
        pass
    return top


# ---------- 状态 ----------

def load_registry():
    global _registry, _by_key, _next_id
    try:
        data = json.loads(REGISTRY.read_text(encoding="utf-8"))
        _registry = {str(k): v for k, v in (data.get("items") or {}).items()}
        _next_id = max(ID_BASE + 1, int(data.get("next") or ID_BASE + 1))
    except (OSError, ValueError):
        _registry, _next_id = {}, ID_BASE + 1

    # 重建索引。老版本给同一部片分配过好几个 ID，这里挑一个当正主：优先有弹幕缓存的，
    # 其次 ID 最小的 —— 这样之前已经抓下来的弹幕不会白费，客户端存的旧 ID 也仍然可用。
    chosen: dict[str, tuple[tuple[int, int], int]] = {}
    for raw_id, row in _registry.items():
        if not raw_id.isdigit() or not isinstance(row, dict):
            continue
        item_id = int(raw_id)
        key = norm_key(row.get("title"), row.get("year"))
        has_cache = 1 if comments_path(item_id).exists() else 0
        rank = (-has_cache, item_id)
        if key not in chosen or rank < chosen[key][0]:
            chosen[key] = (rank, item_id)
    _by_key = {key: item_id for key, (_rank, item_id) in chosen.items()}

    _next_id = max(_next_id, _scan_max_cache_id() + 1)
    return len(_registry), len(_by_key)


def save_registry_locked():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"next": _next_id, "items": _registry}, ensure_ascii=False)
    tmp = REGISTRY.with_suffix("." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, REGISTRY)


def allocate(movie: dict) -> tuple[int, bool]:
    """给影片分配 ID，同一部片幂等。返回 (item_id, 是否新建)。"""
    global _next_id
    key = norm_key(movie.get("title"), movie.get("year"))
    with _lock:
        existing = _by_key.get(key)
        if existing is not None and str(existing) in _registry:
            row = _registry[str(existing)]
            # 平台链接会随时间变化（新上架/下架），顺手用新的覆盖，弹幕源才有得挑
            links = movie.get("links") or {}
            if links and links != row.get("links"):
                row["links"] = links
                save_registry_locked()
            return existing, False

        new_id = _next_id
        _next_id += 1
        _registry[str(new_id)] = {
            "title": movie.get("title", ""),
            "year": str(movie.get("year", "")),
            "links": movie.get("links", {}),
            "key": key,
        }
        _by_key[key] = new_id
        save_registry_locked()
    return new_id, True


def lookup(item_id: int) -> dict | None:
    with _lock:
        row = _registry.get(str(item_id))
    return dict(row) if row else None


def comments_path(item_id: int) -> Path:
    return STATE_DIR / f"comments-{item_id}.json"


def cached_comments(item_id: int):
    try:
        return json.loads(comments_path(item_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def store_comments(item_id: int, payload: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = comments_path(item_id)
    tmp = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# ---------- 弹幕抓取 ----------

def to_dandan(comment, index: int):
    mode = comment.mode if comment.mode in (1, 4, 5) else 1
    return {
        "cid": index,
        "p": f"{comment.time:.3f},{mode},{int(comment.color) & 0xFFFFFF},0",
        "m": comment.text,
    }


def fetch_for(item_id: int) -> dict:
    movie = lookup(item_id)
    if not movie:
        raise KeyError(item_id)
    errors = []
    for platform in PLATFORM_ORDER:
        if platform not in movie["links"]:
            continue
        try:
            comments, source, _url = nd.fetch_public_danmaku(movie, None, platform=platform)
            if not comments:
                raise nd.ToolError("无弹幕")
            payload = {
                "source": source,
                "title": f"{movie['title']} · {nd.PLATFORM_NAMES.get(platform, platform)}",
                "count": len(comments),
                "comments": [to_dandan(c, i) for i, c in enumerate(comments, 1)],
            }
            store_comments(item_id, payload)
            print(f"[ok] {item_id} {movie['title']} <- {source} {len(comments)} 条", flush=True)
            return payload
        except Exception as exc:  # noqa: BLE001 - 单源失败继续下一个
            errors.append(f"{nd.PLATFORM_NAMES.get(platform, platform)}: {exc}")
    raise RuntimeError("；".join(errors) or "该影片没有可用平台链接")


def warm(item_id: int):
    """匹配成功后在后台先抓一遍，客户端来取时通常直接命中缓存。

    并发数封顶：每次匹配都开一个线程，被连点几下就会同时向源站发几十个请求，
    正好撞上它的限流。超过上限就不预抓了，客户端真来取时再同步抓，功能不退化。
    """
    with _lock:
        if item_id in _warming or cached_comments(item_id):
            return
        if len(_warming) >= WARM_MAX:
            return
        _warming.add(item_id)

    def run():
        try:
            fetch_for(item_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[warm-fail] {item_id}: {exc}", flush=True)
        finally:
            with _lock:
                _warming.discard(item_id)

    threading.Thread(target=run, daemon=True).start()


# ---------- 业务 ----------

def do_match(body: dict) -> dict:
    file_name = str(body.get("fileName") or "").strip()
    if not file_name:
        return {"errorCode": 400, "success": False, "errorMessage": "缺少 fileName"}
    title, year = nd.filename_title(file_name)
    if not title:
        return {"isMatched": False, "matches": [], "errorCode": 0, "success": True}
    try:
        movies = nd.search_movies(title, year)
    except Exception as exc:  # noqa: BLE001
        return {"errorCode": 500, "success": False, "errorMessage": f"搜索失败：{exc}"}
    if not movies:
        return {"isMatched": False, "matches": [], "errorCode": 0, "success": True}
    best = movies[0]
    item_id, _created = allocate(best)
    # 已经抓过的片子会直接命中缓存，warm() 内部自己会跳过
    warm(item_id)
    return {
        "errorCode": 0,
        "success": True,
        "isMatched": True,
        "matches": [{
            "animeId": item_id,
            "episodeId": item_id,
            "animeTitle": best["title"],
            "episodeTitle": best["title"],
            "type": "电影",
            "shift": 0,
        }],
    }


def do_search_anime(keyword: str) -> dict:
    if not keyword.strip():
        return {"errorCode": 400, "success": False, "errorMessage": "缺少 keyword"}
    title, year = nd.filename_title(keyword)
    movies = nd.search_movies(title or keyword, year)
    animes = []
    for movie in movies[:8]:
        item_id, _created = allocate(movie)
        animes.append({
            "animeId": item_id,
            "animeTitle": f"{movie['title']} ({movie.get('year') or '----'})",
            "type": "电影",
            "typeDescription": "电影",
            "episodeCount": 1,
        })
    return {"errorCode": 0, "success": True, "animes": animes}


def do_search_episodes(anime: str, episode: str) -> dict:
    result = do_search_anime(anime)
    animes = []
    for row in result.get("animes", []):
        if episode and episode not in row["animeTitle"] and episode not in str(lookup(row["animeId"])):
            continue
        animes.append({
            "animeId": row["animeId"],
            "animeTitle": row["animeTitle"],
            "type": row["type"],
            "episodes": [{
                "episodeId": row["animeId"],
                "episodeTitle": row["animeTitle"],
            }],
        })
    if not animes:
        animes = [{
            "animeId": row["animeId"],
            "animeTitle": row["animeTitle"],
            "type": row["type"],
            "episodes": [{"episodeId": row["animeId"], "episodeTitle": row["animeTitle"]}],
        } for row in result.get("animes", [])]
    return {"errorCode": 0, "success": True, "animes": animes}


def do_comment(item_id: int, refresh: bool = False) -> dict:
    # ?refresh=1 强制重抓。ID 稳定之后缓存会一直被沿用，万一第一次只抓到几条、
    # 或者当时 B站 还没上架这片，得给个手动兜底的口子。
    if not refresh:
        cached = cached_comments(item_id)
        if cached and cached.get("comments"):
            return {"errorCode": 0, "success": True, **cached}
    try:
        payload = fetch_for(item_id)
    except KeyError:
        return {"errorCode": 404, "success": False, "errorMessage": "未知节目 ID，请重新匹配"}
    except Exception as exc:  # noqa: BLE001
        return {"errorCode": 500, "success": False, "errorMessage": str(exc)}
    return {"errorCode": 0, "success": True, **payload}


def do_bangumi(item_id: int) -> dict:
    movie = lookup(item_id)
    if not movie:
        return {"errorCode": 404, "success": False, "errorMessage": "未知番剧 ID"}
    title = f"{movie['title']} ({movie['year']})" if movie.get("year") else movie["title"]
    episode = {"episodeId": item_id, "episodeTitle": title, "episodeNumber": "1"}
    return {
        "errorCode": 0,
        "success": True,
        "bangumi": {
            "animeId": item_id,
            "animeTitle": title,
            "type": "电影",
            "typeDescription": "电影",
            "episodes": [episode],
        },
    }


# ---------- HTTP ----------

TOKEN = ""

# ---------- 限流 ----------
# 目的不是防别人白嫖弹幕，而是防止有人高频刷接口把源站惹毛：B站对同一 IP 的分段接口
# 本来就限流，刷狠了服务器 IP 会被挡，最后是自己看电影时弹幕变 0 条。
RL_WINDOW = 60.0       # 统计窗口（秒）
RL_MAX = 60            # 窗口内允许的请求数
RL_SWEEP_AT = 512      # 记录的 IP 数超过这个值就顺手清理过期条目
MAX_BODY = 64 * 1024   # 请求体上限，防止一次塞几百 MB 进内存

_rl_lock = threading.Lock()
_rl_hits: dict[str, list[float]] = {}
_rl_trusted = {"127.0.0.1", "::1"}


def client_ip(handler) -> str:
    """服务挂在 nginx 后面时 client_address 永远是 127.0.0.1，真实来源得看转发头。

    Cloudflare 会用自己解析出的客户端 IP 覆盖 CF-Connecting-IP，外部伪造不了；
    走 tailnet 直连时就算伪造也只影响他自己，无所谓。
    """
    for header in ("CF-Connecting-IP", "X-Real-IP", "X-Forwarded-For"):
        value = handler.headers.get(header)
        if value:
            head = value.split(",")[0].strip()
            if head:
                return head
    return handler.client_address[0]


def rate_limited(ip: str) -> int:
    """返回 0 表示放行，否则返回建议的重试秒数。"""
    if ip in _rl_trusted:
        return 0
    now = time.monotonic()
    with _rl_lock:
        hits = [t for t in _rl_hits.get(ip, ()) if now - t < RL_WINDOW]
        if len(hits) >= RL_MAX:
            _rl_hits[ip] = hits
            return max(1, int(RL_WINDOW - (now - hits[0])) + 1)
        hits.append(now)
        _rl_hits[ip] = hits
        if len(_rl_hits) > RL_SWEEP_AT:
            cutoff = now - RL_WINDOW
            for key in [k for k, v in _rl_hits.items() if not v or v[-1] < cutoff]:
                _rl_hits.pop(key, None)
    return 0


def strip_token(path: str):
    """有 token 时，要求路径以 /<token> 开头；返回去掉前缀后的路径，不匹配返回 None。"""
    if not TOKEN:
        return path
    prefix = "/" + TOKEN
    if path == prefix:
        return "/"
    if path.startswith(prefix + "/"):
        return path[len(prefix):]
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "nas-danmaku-bridge/0.1"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def _send(self, payload: dict, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self):
        self._send({"errorCode": 0, "success": True})

    def _throttled(self) -> bool:
        ip = client_ip(self)
        wait = rate_limited(ip)
        if not wait:
            return False
        print(f"[throttle] {ip} 触发限流，建议等 {wait}s", flush=True)
        self._send(
            {"errorCode": 429, "success": False, "errorMessage": f"请求过于频繁，请 {wait} 秒后再试"},
            429,
        )
        return True

    def do_GET(self):
        if self._throttled():
            return
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if path == "/healthz":
                return self._send({"ok": True, "service": "nas-danmaku-bridge"})
            routed = strip_token(path)
            if routed is None:
                return self._send({"errorCode": 404, "success": False, "errorMessage": "未知接口"}, 404)
            path = routed or "/"
            if path == "/":
                return self._send({"ok": True, "service": "nas-danmaku-bridge"})
            if path == "/api/v2/search/anime":
                keyword = (query.get("keyword") or [""])[0]
                return self._send(do_search_anime(keyword))
            if path == "/api/v2/search/episodes":
                anime = (query.get("anime") or [""])[0]
                episode = (query.get("episode") or [""])[0]
                return self._send(do_search_episodes(anime, episode))
            if path.startswith("/api/v2/comment/"):
                raw = path.rsplit("/", 1)[-1]
                if not raw.isdigit():
                    return self._send({"errorCode": 400, "success": False, "errorMessage": "节目 ID 无效"}, 400)
                flag = (query.get("refresh") or query.get("force") or ["0"])[0]
                return self._send(do_comment(int(raw), refresh=flag.lower() in ("1", "true", "yes")))
            if path.startswith("/api/v2/bangumi/"):
                raw = path.rsplit("/", 1)[-1]
                if not raw.isdigit():
                    return self._send({"errorCode": 400, "success": False, "errorMessage": "番剧 ID 无效"}, 400)
                return self._send(do_bangumi(int(raw)))
            return self._send({"errorCode": 404, "success": False, "errorMessage": "未知接口"}, 404)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return self._send({"errorCode": 500, "success": False, "errorMessage": "服务器内部错误"}, 500)

    def do_POST(self):
        if self._throttled():
            return
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/")
        routed = strip_token(path)
        if routed is None:
            return self._send({"errorCode": 404, "success": False, "errorMessage": "未知接口"}, 404)
        path = routed or "/"
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self.close_connection = True
            return self._send({"errorCode": 413, "success": False, "errorMessage": "请求体过大"}, 413)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return self._send({"errorCode": 400, "success": False, "errorMessage": "请求体不是合法 JSON"}, 400)
        try:
            if path == "/api/v2/match":
                return self._send(do_match(body if isinstance(body, dict) else {}))
            if path in ("/api/v2/comment/by-url", "/api/v2/comment/by_url"):
                url = str((body or {}).get("videoUrl") or "")
                return self._send(do_match({"fileName": url.rsplit("/", 1)[-1]}))
            return self._send({"errorCode": 404, "success": False, "errorMessage": "未知接口"}, 404)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return self._send({"errorCode": 500, "success": False, "errorMessage": "服务器内部错误"}, 500)


def main():
    parser = argparse.ArgumentParser(description="弹弹play v2 兼容桥，套在 nas_danmaku 上")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9322)
    parser.add_argument("--token", default="", help="URL 前缀令牌；设成 abc123 则客户端填 http://host:port/abc123")
    args = parser.parse_args()
    global TOKEN
    TOKEN = args.token.strip().strip("/")
    total, uniq = load_registry()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"nas-danmaku-bridge 已启动: http://{args.host}:{args.port}  （状态目录 {STATE_DIR}）", flush=True)
    print(f"索引 {uniq} 部影片 / {total} 条历史 ID，下一个 ID = {_next_id}", flush=True)
    print(f"客户端填这个地址：http://<本机局域网IP>:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
