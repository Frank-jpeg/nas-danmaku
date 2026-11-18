"""离线自检：ID 幂等 / 旧 registry 迁移 / _next_id 兜底。不联网。"""
import json
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 本机这个 Python 没编译 tkinter，nas_danmaku 顶层 import 会炸。
# 这次只测 ID 分配逻辑，不碰 GUI，塞个空壳占位即可。
if "tkinter" not in sys.modules:
    try:
        import tkinter  # noqa: F401
    except ModuleNotFoundError:
        stub = types.ModuleType("tkinter")

        class _Any:
            def __init__(self, *a, **k):
                pass

            def __getattr__(self, name):
                return _Any()

            def __call__(self, *a, **k):
                return _Any()

        def _module_getattr(name):
            return _Any()

        stub.__getattr__ = _module_getattr
        stub.filedialog = stub
        stub.messagebox = stub
        sys.modules["tkinter"] = stub
        sys.modules["tkinter.filedialog"] = stub
        sys.modules["tkinter.messagebox"] = stub
        sys.modules["tkinter.ttk"] = stub
        sys.modules["tkinter.font"] = stub
        print("[stub] 已用空壳替换 tkinter")

TMP = Path(tempfile.mkdtemp(prefix="danmu-idem-"))
os.environ["LOCALAPPDATA"] = str(TMP)

import danmu_server as ds  # noqa: E402

ds.STATE_DIR = TMP / "state"
ds.REGISTRY = ds.STATE_DIR / "registry.json"
ds.STATE_DIR.mkdir(parents=True, exist_ok=True)
ds.comments_path = lambda i: ds.STATE_DIR / f"comments-{i}.json"

fails = []


def check(name, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + f"{name}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        fails.append(name)


print("[1] norm_key 归一化")
check("纯片名+年份", ds.norm_key("让子弹飞", "2010"), "让子弹飞|2010")
check("带括号年份", ds.norm_key("让子弹飞 (2010)", ""), "让子弹飞|")
check("带空格句点", ds.norm_key(" 让子弹飞.", "2010"), "让子弹飞|2010")
check("全角字母", ds.norm_key("Ｌｅｔ　Ｂｕｌｌｅｔｓ　Ｆｌｙ", ""), "letbulletsfly|")
check("数字片名不被剥空", ds.norm_key("2012", ""), "2012|")
check("片名自带年份被剥", ds.norm_key("让子弹飞2010", "2010"), "让子弹飞|2010")

print("[2] allocate 幂等")
m = {"title": "让子弹飞", "year": "2010", "links": {"bilibili1": "u1"}}
a, new_a = ds.allocate(m)
b, new_b = ds.allocate({"title": "让子弹飞 (2010)", "year": "2010", "links": {"bilibili1": "u1"}})
print(f"  第一次 {a} 新建={new_a} / 第二次 {b} 新建={new_b}")
check("ID 相同", (a, b), (a, a))
check("第二次没新建", new_b, False)
check("registry 只有一条", len(ds._registry), 1)

print("[3] 平台链接会更新")
ds.allocate({"title": "让子弹飞", "year": "2010", "links": {"bilibili1": "u1", "qq": "u2"}})
check("links 被覆盖", sorted(ds._registry[str(a)]["links"]), ["bilibili1", "qq"])

print("[4] 旧 registry 迁移：同片多 ID 时选有缓存的")
ds.STATE_DIR = TMP / "state2"
ds.REGISTRY = ds.STATE_DIR / "registry.json"
ds.STATE_DIR.mkdir(parents=True, exist_ok=True)
ds.comments_path = lambda i: ds.STATE_DIR / f"comments-{i}.json"
old = {
    "next": 2000000010,
    "items": {
        "2000000001": {"title": "让子弹飞", "year": "2010", "links": {"bilibili1": "u"}},
        "2000000006": {"title": "让子弹飞", "year": "2010", "links": {"bilibili1": "u"}},
        "2000000007": {"title": "让子弹飞", "year": "2010", "links": {"bilibili1": "u"}},
        "2000000009": {"title": "奥本海默", "year": "2023", "links": {"bilibili1": "u"}},
    },
}
ds.REGISTRY.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
(ds.STATE_DIR / "comments-2000000006.json").write_text('{"comments":[1]}', encoding="utf-8")
(ds.STATE_DIR / "comments-2000000009.json").write_text('{"comments":[1]}', encoding="utf-8")
total, uniq = ds.load_registry()
check("4 条历史压成 2 个片", (total, uniq), (4, 2))
got = ds.allocate({"title": "让子弹飞", "year": "2010", "links": {"bilibili1": "u"}})[0]
check("复用有缓存的那个 ID", got, 2000000006)
check("奥本海默也是幂等", ds.allocate({"title": "奥本海默", "year": "2023", "links": {}})[0], 2000000009)

print("[5] registry 丢失时 _next_id 不撞旧缓存")
ds.STATE_DIR = TMP / "state3"
ds.REGISTRY = ds.STATE_DIR / "registry.json"
ds.STATE_DIR.mkdir(parents=True, exist_ok=True)
ds.comments_path = lambda i: ds.STATE_DIR / f"comments-{i}.json"
(ds.STATE_DIR / "comments-2000000077.json").write_text("{}", encoding="utf-8")
ds.load_registry()
check("next 被推到 78 之后", ds._next_id >= 2000000078, True)

print("[6] do_match 两次同一文件拿到同一 episodeId")
ds.warm = lambda item_id: None          # 不联网预热
ds.nd.search_movies = lambda title, year="": [
    {"title": "让子弹飞", "year": "2010", "links": {"bilibili1": "u"}}
]
r1 = ds.do_match({"fileName": "让子弹飞.2010.BluRay.1080p.mkv"})
r2 = ds.do_match({"fileName": "让子弹飞.2010.2160p.HDR.mkv"})
e1 = r1["matches"][0]["episodeId"] if r1.get("matches") else None
e2 = r2["matches"][0]["episodeId"] if r2.get("matches") else None
print(f"  {r1.get('matches')}")
check("两个不同清晰度同一 ID", (e1, e2), (e1, e1))
check("确实匹配上了", r1.get("isMatched"), True)

print()
if fails:
    print("失败项：" + "，".join(fails))
    sys.exit(1)
print("全部通过")
print("临时目录：" + str(TMP))
