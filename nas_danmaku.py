#!/usr/bin/env python3
"""字幕弹幕一键合成 v2：单文件，选择影片后自动识别字幕和弹幕，确认后写回原目录。"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import html
from html.parser import HTMLParser
import http.client
import http.cookiejar
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
import zlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace as dataclass_replace

VERSION = "2.13.1"
DEFAULT_DANMAKU_DURATION = 12  # 1× speed reference; the UI preference defaults to 0.5×.
MAX_BYTES = 32 * 1024 * 1024
STYLE_FIELDS = "Name Fontname Fontsize PrimaryColour SecondaryColour OutlineColour BackColour Bold Italic Underline StrikeOut ScaleX ScaleY Spacing Angle BorderStyle Outline Shadow Alignment MarginL MarginR MarginV Encoding".split()
EVENT_FIELDS = "Layer Start End Style Name MarginL MarginR MarginV Effect Text".split()
TEXT_CODECS = {"ass", "ssa", "subrip", "srt", "mov_text", "text", "webvtt"}
DM_DEFAULTS = dict(font_size=50, duration=DEFAULT_DANMAKU_DURATION * 2, area=25, opacity=80, block_scroll=False,
                   block_fixed=True, block_color=False, avoid_subtitles=True, deduplicate=True,
                   block_noise=True, block_keywords="", filter_rules=None)


class ToolError(Exception):
    pass


class TaskCancelled(Exception):
    pass


@dataclass(frozen=True)
class ProgressUpdate:
    message: str
    current: float | None = None
    total: float | None = None
    unit: str = ""
    complete: bool = False

    @property
    def percent(self):
        if self.complete:
            return 100.0
        if self.current is None or self.total is None or self.total <= 0:
            return None
        # 满量后仍可能在等待进程退出/文件关闭；成功返回前不显示完成。
        return max(0.0, min(99.9, self.current / self.total * 100))


def report(progress, message, current=None, total=None, unit="", complete=False):
    if progress is not None:
        progress(ProgressUpdate(message, current, total, unit, complete))


def normalize_path(value):
    path = Path(value).expanduser()
    try:
        return path.resolve()
    except OSError as exc:
        if getattr(exc, "winerror", None) != 1005:
            raise
        # 某些虚拟映射盘支持读写，却不支持 Windows 的最终路径查询。
        # 保留用户选择的盘符路径；后续仍检查文件可读性及大小/修改时间。
        return Path(os.path.abspath(path))


def finite(value, label="数值"):
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise ToolError(f"{label}必须是数字。") from exc
    if not math.isfinite(result):
        raise ToolError(f"{label}不能是无穷大或 NaN。")
    return result


def read_text(path):
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise ToolError("字幕/弹幕文件超过 32 MB，请先精简。")
    raw = path.read_bytes()
    encodings = ("utf-16",) if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except UnicodeError:
            continue
    raise ToolError("无法读取文件编码，请将输入另存为 UTF-8。")


def stamp(value):
    match = re.fullmatch(r"\s*(\d+):(\d{2}):(\d{2})[.,](\d{1,3})\s*", value)
    if not match:
        raise ToolError(f"不支持的时间格式：{value!r}")
    h, m, s, fraction = match.groups()
    if int(m) > 59 or int(s) > 59:
        raise ToolError("字幕时间的分钟/秒超出范围。")
    return round((int(h) * 3600 + int(m) * 60 + int(s) + int(fraction) / 10 ** len(fraction)) * 100)


def format_stamp(value):
    value = max(0, int(value))
    seconds, cs = divmod(value, 100)
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours}:{minute:02}:{sec:02}.{cs:02}"


def style(name="Default", size=48):
    values = [name, "Microsoft YaHei", str(size), "&H00FFFFFF", "&H000000FF", "&H00000000", "&H00000000", "0", "0", "0", "0", "100", "100", "0", "0", "1", "2", "0", "2", "60", "60", "45", "1"]
    return dict(zip(STYLE_FIELDS, values))


def event(start, end, text, style_name="Default", layer=0):
    return dict(zip(EVENT_FIELDS, [str(layer), format_stamp(start), format_stamp(end), style_name, "", "0", "0", "0", "", text]))


@dataclass
class Ass:
    info: dict = field(default_factory=lambda: {"ScriptType": "v4.00+", "PlayResX": "1920", "PlayResY": "1080", "WrapStyle": "0", "ScaledBorderAndShadow": "yes"})
    styles: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    extras: list = field(default_factory=list)

    @property
    def resolution(self):
        try:
            w, h = int(self.info["PlayResX"]), int(self.info["PlayResY"])
        except (KeyError, ValueError) as exc:
            raise ToolError("ASS 缺少有效的 PlayResX/PlayResY，无法安全合并坐标；请先补全画布分辨率。") from exc
        if not (100 <= w <= 16384 and 100 <= h <= 16384):
            raise ToolError("ASS 画布分辨率超出支持范围。")
        return w, h

    def dumps(self):
        lines = ["[Script Info]"] + [f"{key}: {value}" for key, value in self.info.items()]
        lines += ["", "[V4+ Styles]", "Format: " + ", ".join(STYLE_FIELDS)]
        for row in self.styles.values():
            lines.append("Style: " + ",".join(row[k] for k in STYLE_FIELDS))
        lines += ["", "[Events]", "Format: " + ", ".join(EVENT_FIELDS)]
        for row in sorted(self.events, key=lambda x: (stamp(x["Start"]), int(x["Layer"]))):
            lines.append("Dialogue: " + ",".join(row[k] for k in EVENT_FIELDS))
        for name, content in self.extras:
            lines += ["", name] + content
        return "\n".join(lines) + "\n"


def parse_ass(text):
    doc = Ass(info={})
    section = ""
    fields = []
    info_names = {k.lower(): k for k in ("ScriptType", "PlayResX", "PlayResY", "WrapStyle", "ScaledBorderAndShadow")}
    for number, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section, fields = line.lower(), []
            if section == "[v4 styles]":
                raise ToolError("旧版 SSA 请先用 ffmpeg 转换成 ASS。")
            if section not in {"[script info]", "[v4+ styles]", "[events]"}:
                doc.extras.append((line, []))
            continue
        if section not in {"[script info]", "[v4+ styles]", "[events]"}:
            if doc.extras:
                doc.extras[-1][1].append(raw)
            continue
        if not line or line.startswith(";"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.lstrip()
        if section == "[script info]":
            doc.info[info_names.get(key.strip().lower(), key.strip())] = value
        elif key.lower() == "format":
            standard = STYLE_FIELDS if section == "[v4+ styles]" else EVENT_FIELDS
            mapping = {k.lower(): k for k in standard}
            fields = [mapping.get(v.strip().lower(), v.strip()) for v in value.split(",")]
            if not set(standard).issubset(fields) or len(set(fields)) != len(fields):
                raise ToolError(f"ASS 第 {number} 行字段定义不完整或重复。")
            if section == "[events]" and fields[-1] != "Text":
                raise ToolError("ASS 的 Text 字段必须在最后。")
        elif key.lower() in {"style", "dialogue"}:
            if not fields:
                raise ToolError(f"ASS 第 {number} 行之前缺少 Format。")
            values = value.split(",", len(fields) - 1)
            if len(values) != len(fields):
                raise ToolError(f"ASS 第 {number} 行字段数量不正确。")
            row = dict(zip(fields, values))
            if section == "[v4+ styles]" and key.lower() == "style":
                if row["Name"] in doc.styles:
                    raise ToolError("输入 ASS 有重复样式名，请先修复。")
                doc.styles[row["Name"]] = row
            elif section == "[events]" and key.lower() == "dialogue":
                if stamp(row["End"]) <= stamp(row["Start"]):
                    raise ToolError(f"ASS 第 {number} 行结束时间不晚于开始时间。")
                try:
                    int(row["Layer"])
                except ValueError as exc:
                    raise ToolError("ASS Layer 必须是整数。") from exc
                doc.events.append(row)
    doc.resolution
    if not doc.events:
        raise ToolError("ASS 没有可显示的 Dialogue 行。")
    if any(row["Style"] not in doc.styles for row in doc.events):
        raise ToolError("ASS 有未定义的样式，请先修复。")
    return doc


def escape_text(text):
    # 弹幕是文本，绝不允许把其中的 ASS 标签当作代码执行。
    text = " ".join(str(text).split())
    text = "".join(c for c in text if unicodedata.category(c) != "Cc")
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝")


def srt_text(text):
    parts = re.split(r"(<[^>]+>|\r?\n)", text)
    out = []
    for part in parts:
        tag = part.lower()
        if tag in {"<i>", "<b>", "<u>"}:
            out.append("{\\" + tag[1] + "1}")
        elif tag in {"</i>", "</b>", "</u>"}:
            out.append("{\\" + tag[2] + "0}")
        elif tag in {"<br>", "<br/>", "<br />", "\n", "\r\n"}:
            out.append(r"\N")
        elif not tag.startswith("<"):
            out.append(html.unescape(part).replace("\\", "＼").replace("{", "｛").replace("}", "｝"))
    return "".join(out)


def parse_srt(text):
    doc = Ass(styles={"Default": style()})
    for block in re.split(r"\n\s*\n", text.lstrip("\ufeff").replace("\r\n", "\n").strip()):
        lines = block.splitlines()
        if lines and lines[0].strip().isdigit():
            lines.pop(0)
        if not lines:
            continue
        match = re.fullmatch(r"\s*(\d+:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d+:\d{2}:\d{2}[,.]\d{1,3})\s*", lines[0])
        if not match or len(lines) < 2:
            raise ToolError("SRT 存在无法解析的字幕块，请检查时间行或另存为 ASS。")
        start, end = map(stamp, match.groups())
        if end <= start:
            raise ToolError("SRT 结束时间必须晚于开始时间。")
        doc.events.append(event(start, end, srt_text("\n".join(lines[1:]))))
    if not doc.events:
        raise ToolError("SRT 没有字幕内容。")
    return doc


def load_subtitle(path):
    suffix = Path(path).suffix.lower()
    if suffix == ".srt":
        return parse_srt(read_text(path))
    if suffix == ".ass":
        return parse_ass(read_text(path))
    raise ToolError("原字幕请选择 .srt 或 .ass。PGS/SUP 图片字幕需要先 OCR 或找文字字幕。")


def run_media(name, args, timeout=300, progress=None, duration=None):
    executable = shutil.which(name)
    if not executable:
        raise ToolError(f"没有找到 {name}。请安装 FFmpeg 并加入 PATH，或直接使用外挂 SRT/ASS。")
    if name == "ffmpeg" and progress is not None:
        return run_ffmpeg_progress(executable, args, timeout, progress, duration)
    try:
        result = subprocess.run([executable, "-v", "error", "-nostdin"] + args if name == "ffmpeg" else [executable, "-v", "error"] + args,
                                capture_output=True, timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise ToolError("读取影片超时；异地影片建议先取出文字字幕，再导入本工具。") from exc
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace")[-1500:]
        raise ToolError(f"{name} 失败：{message}")
    return result.stdout


def run_ffmpeg_progress(executable, args, timeout, progress, duration):
    total = float(duration) if duration and float(duration) > 0 else None
    message = "提取原台词字幕"
    report(progress, message, 0, total, "秒")
    command = [executable, "-v", "error", "-nostdin", "-nostats", "-stats_period", "0.25",
               "-progress", "pipe:2"] + args
    chunks, errors, reader_errors = [], [], []
    position = 0.0
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
        def read_stdout():
            try:
                while chunk := process.stdout.read(65536):
                    chunks.append(chunk)
            except Exception as exc:
                reader_errors.append(exc)

        def read_stderr():
            nonlocal position
            try:
                for raw in process.stderr:
                    line = raw.decode("utf-8", errors="replace").strip()
                    key, sep, value = line.partition("=")
                    if key == "out_time_us" and sep:
                        try:
                            at = float(value) / 1_000_000
                        except ValueError:
                            continue
                        if math.isfinite(at):
                            position = max(position, at)
                            report(progress, message, position, total, "秒")
                    elif not (sep and re.fullmatch(r"frame|fps|stream_\d+_\d+_q|bitrate|total_size|out_time.*|dup_frames|drop_frames|speed|progress", key)):
                        errors.append(line)
            except Exception as exc:
                reader_errors.append(exc)

        readers = [threading.Thread(target=read_stdout), threading.Thread(target=read_stderr)]
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ToolError("读取影片超时；异地影片建议先取出文字字幕，再导入本工具。") from exc
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            for reader in readers:
                reader.join()
        if process.returncode:
            raise ToolError("ffmpeg 失败：" + "\n".join(errors)[-1500:])
        if reader_errors:
            raise ToolError("读取 FFmpeg 输出失败：" + str(reader_errors[0]))
    report(progress, message, position, total, "秒", complete=True)
    return b"".join(chunks)


def subtitle_tracks(video):
    if not Path(video).is_file():
        raise ToolError("影片路径不存在或当前无法访问。")
    data = json.loads(run_media("ffprobe", ["-show_streams", "-of", "json", str(video)], timeout=60))
    return [s for s in data.get("streams", []) if s.get("codec_type") == "subtitle"]


def extract_subtitle(video, index, progress=None, duration=None):
    report(progress, "检查内封字幕轨道")
    tracks = subtitle_tracks(video)
    if index is None:
        text_tracks = [s for s in tracks if s.get("codec_name") in TEXT_CODECS]
        if len(text_tracks) != 1:
            raise ToolError("影片有多个文字字幕轨或没有文字字幕轨；请先列出字幕轨并指定序号。")
        index = text_tracks[0]["index"]
    selected = next((s for s in tracks if s["index"] == int(index)), None)
    if not selected or selected.get("codec_name") not in TEXT_CODECS:
        raise ToolError("所选轨道不是支持的文字字幕。PGS/SUP/DVD 图片字幕请先 OCR 或找文字字幕。")
    raw = run_media("ffmpeg", ["-i", str(video), "-map", f"0:{index}", "-c:s", "ass", "-f", "ass", "pipe:1"],
                    progress=progress, duration=duration)
    report(progress, "解析原台词字幕")
    return parse_ass(raw.decode("utf-8-sig"))


@dataclass
class Comment:
    time: float
    text: str
    color: int = 16777215
    mode: int = 1


def parse_comments(text):
    comments, skipped = [], 0
    clean = text.lstrip("\ufeff \t\r\n")
    if clean.startswith("<"):
        if "<!DOCTYPE" in clean.upper() or "<!ENTITY" in clean.upper():
            raise ToolError("不支持包含外部实体声明的 XML。")
        try:
            root = ET.fromstring(clean)
        except ET.ParseError as exc:
            raise ToolError("弹幕 XML 格式不正确。") from exc
        rows = [{"p": node.attrib.get("p", ""), "m": "".join(node.itertext()), "xml": True} for node in root.iter("d")]
    else:
        try:
            obj = json.loads(clean)
        except json.JSONDecodeError as exc:
            raise ToolError("弹幕文件不是有效的 XML/JSON。") from exc
        if isinstance(obj, dict) and obj.get("success") is False:
            raise ToolError("弹幕 JSON 是接口错误响应，不含有效弹幕。")
        rows = obj.get("comments", obj.get("data", [])) if isinstance(obj, dict) else obj
        if not isinstance(rows, list):
            raise ToolError("JSON 需要弹弹play comments 数组或 time/text 对象数组。")
    for row in rows:
        try:
            if not isinstance(row, dict):
                raise ValueError()
            if "p" in row:
                p = str(row["p"]).split(",")
                at, mode = finite(p[0]), int(p[1])
                color = int(p[3] if row.get("xml") or len(p) >= 8 else p[2])
                message = row.get("m", row.get("text", ""))
            else:
                at, mode = finite(row["time"]), int(row.get("mode", 1))
                color = int(row.get("color", 16777215))
                message = row.get("text", row.get("m", ""))
            if mode not in (1, 2, 3, 4, 5, 6) or at < 0 or not str(message).strip():
                raise ValueError()
            comments.append(Comment(at, str(message), color & 0xFFFFFF, mode))
        except (ValueError, TypeError, KeyError, IndexError, ToolError):
            skipped += 1
    if not comments:
        raise ToolError("没有读到普通/顶部/底部弹幕，可能源站没有弹幕或格式不受支持。")
    return comments, skipped


_NOISE_FOLD = str.maketrans("觀看簽報時現與幾來還嗎誰號個這後愛帶著們", "观看签报时现与几来还吗谁号个这后爱带着们")
_CN_NUMBER = r"[零〇一二三四五六七八九十两\d]{1,4}"
_DATE_TOKEN = re.compile(
    rf"(?<!\d)(?:(?:19|20)\d{{2}}[./-]\d{{1,2}}[./-]\d{{1,2}}|"
    rf"(?:19|20)\d{{2}}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])|"
    rf"(?:{_CN_NUMBER}年)?{_CN_NUMBER}月(?:{_CN_NUMBER}[日号]?)?)")
_YEAR_TOKEN = re.compile(r"(?<!\d)(?:(?:19|20)\d{2}|[二零〇一三四五六七八九]{4})年?(?!\d)")
_TIME_TOKEN = re.compile(rf"(?<!\d)(?:(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?|"
                         rf"{_CN_NUMBER}(?:点|时)(?:{_CN_NUMBER}分?)?(?:{_CN_NUMBER}秒)?)(?!\d)")
_WATCH_WORDS = re.compile(
    rf"(?:现在|今天|此刻|观看时间|北京时间|时间|凌晨|早上|上午|中午|下午|晚上|半夜|深夜|"
    rf"记录一下|记录|纪念|留念|打卡|签到|签个到|报到|报道|到此一游|留个脚印|路过|"
    rf"刚刚|刚|开始|正在|已经|终于|第一次|第{_CN_NUMBER}次|{_CN_NUMBER}刷|重温|补番|补课|"
    rf"看到这里|看完|观看|看过|看|刷|来过|来了|有人|多少人|一起|一个人|我|本人|于|是|在|还|又|也|都|"
    rf"才|有|人|谁|来|到|这|的|了|吗|呢|啊|呀|哦|喽|啦|没|嘞)")
_PRESENCE = re.compile(
    r"(?:在吗|在不在|有人吗|有人在吗|还有人吗|还有人在吗|还有人在看吗|还有人看吗|有人在看吗|"
    r"有人看吗|有人一起看吗|还有人看的吗|还有人看|谁还在看|谁在看|谁在看呀|有活人吗|活人扣1|"
    r"有人的扣1|在的扣1|在的举个手|举个手|吱一声|吱个声|冒个泡|冒泡|前排|抢沙发|沙发|第一|"
    r"来了|集合|集合啦|占个座|打卡|签到|报到|到此一游|路过|留个脚印){1,8}")
_COMPANION = re.compile(
    rf"(?:今天|这次|现在|正在|准备|第一次|我|本人|想|要|又|也|是|正|刚){{0,8}}"
    r"(?:和|跟|与|陪|陪着|带|带着|带上)[\w]{1,16}"
    r"(?:一起|一块|在|正在|来|陪我){0,3}(?:观看|看过|看完|看|重温|刷)"
    rf"(?:这部电影|这部|电影|影片|片子|的|呢|啦|啊|呀|了|中|一遍|第{_CN_NUMBER}遍){{0,6}}")
_PROMOTION = re.compile(r"(?:加|进)(?:qq|微信|粉丝)群|(?:微信|vx|v信|qq)(?:号|群|同号)?[:：]?[a-z0-9_-]{5,}|"
                        r"扫码(?:领取|领红包|加群)|(?:互粉|刷赞|代刷播放)|关注我(?:领取|领|看全集)|私信我(?:领|获取)")
_REPEATED_ASCII = re.compile(r"([a-z0-9]{1,3})\1{5,}")


def filter_text(text):
    """只规范化用于匹配的副本，不修改显示内容、缓存或原台词。"""
    text = unicodedata.normalize("NFKC", str(text)).casefold().translate(_NOISE_FOLD)
    return "".join(c for c in text if not c.isspace() and unicodedata.category(c) not in {"Cf", "Cc"})


def compile_block_keywords(text):
    if not isinstance(text, str) or len(text) > 10000:
        raise ToolError("自定义屏蔽词最多 10000 个字符，每行一个。")
    words = []
    for line in text.splitlines():
        word = filter_text(line.strip())
        if not word:
            continue
        if len(word) > 80:
            raise ToolError("每个屏蔽词最多 80 个字符。")
        if word not in words:
            words.append(word)
    if len(words) > 100:
        raise ToolError("自定义屏蔽词最多 100 条。")
    return tuple(words)


def watch_sentence(text, words=_WATCH_WORDS):
    # 有人/有+人等存在多种切分；用有界动态规划，避免重复正则回溯拖慢合成。
    reachable = {0}
    for start in range(len(text)):
        if start in reachable:
            for end in range(start + 1, min(len(text), start + 10) + 1):
                if words.fullmatch(text[start:end]):
                    reachable.add(end)
    return len(text) in reachable


RULE_TYPES = {"keyword": "普通关键词（包含就屏蔽）", "full": "正则（整句匹配，去标点）",
              "search": "正则（部分匹配，保留标点）", "date": "组合规则：日期打卡", "watch": "组合规则：观看打卡"}
RULE_FIELDS = {"keyword": ("pattern",), "full": ("pattern",), "search": ("pattern",),
               "date": ("date", "year", "time", "words", "companion"), "watch": ("pattern", "words")}
RULE_FIELD_LABELS = {"pattern": "匹配内容", "date": "日期写法", "year": "年份写法", "time": "时刻写法",
                     "words": "打卡用语（单个词）", "companion": "陪同观看句式"}


def default_filter_rules():
    rows = [
        ("repeat", "重复字母数字", "full", {"pattern": _REPEATED_ASCII.pattern},
         "整句由同一段字母/数字重复至少六遍。", "AAAAAAA", "哈哈哈哈哈哈"),
        ("promotion", "广告引流", "search", {"pattern": _PROMOTION.pattern},
         "句中包含明确的加群、领取资源等广告写法。", "加QQ群123456789", "他用微信联系家人"),
        ("presence", "打卡或找人聊天", "full", {"pattern": _PRESENCE.pattern},
         "整句是前排、签到、在吗等短语；最多连续重复八次。", "在吗？在吗？", "有人看懂这个结尾吗"),
        ("companion", "陪同观看打卡", "full", {"pattern": _COMPANION.pattern},
         "整句在报告和谁看电影，保留带具体剧情内容的句子。", "今天和女朋友一起看", "男主和妻子一起看日落"),
        ("date", "日期或时间打卡", "date",
         {"date": _DATE_TOKEN.pattern, "year": _YEAR_TOKEN.pattern, "time": _TIME_TOKEN.pattern,
          "words": _WATCH_WORDS.pattern, "companion": _COMPANION.pattern},
         "先去除日期/时间；剩下全是打卡用语或陪看句式才屏蔽。单独一个年份保留。下面五项都可查看和修改。",
         "2026年10月7日20:30打卡", "1998年上映的电影"),
        ("watch", "报几刷或观看打卡", "watch",
         {"pattern": r"打卡|签到|报到|第.{1,4}次|.{1,4}刷", "words": _WATCH_WORDS.pattern},
         "命中打卡或几刷，且整句都由打卡用语组成才屏蔽。", "我来二刷了", "二刷才注意到这个伏笔"),
    ]
    return [dict(id=key, name=name, kind=kind, parts=parts, description=description,
                 example=example, keep_example=keep, enabled=True, builtin=True)
            for key, name, kind, parts, description, example, keep in rows]


def compile_filter_rules(rules):
    if not isinstance(rules, list) or len(rules) > 150:
        raise ToolError("屏蔽规则需要是列表，最多 150 条。")
    compiled, ids = [], set()
    for item in rules:
        if not isinstance(item, dict):
            raise ToolError("屏蔽规则格式不正确。")
        key, name, kind = item.get("id"), item.get("name"), item.get("kind")
        if not isinstance(key, str) or not key or len(key) > 80 or key in ids:
            raise ToolError("屏蔽规则编号为空、重复或过长。")
        ids.add(key)
        if (not isinstance(name, str) or not name.strip() or len(name) > 80 or
                not isinstance(kind, str) or kind not in RULE_TYPES):
            raise ToolError("每条规则需要名称（最多 80 字）和有效类型。")
        if not isinstance(item.get("enabled"), bool) or not isinstance(item.get("builtin"), bool):
            raise ToolError(f"规则“{name}”的开关格式不正确。")
        for field_name in ("description", "example", "keep_example"):
            if not isinstance(item.get(field_name, ""), str) or len(item.get(field_name, "")) > 1000:
                raise ToolError(f"规则“{name}”的说明或示例过长。")
        parts = item.get("parts")
        if not isinstance(parts, dict) or set(parts) != set(RULE_FIELDS[kind]):
            raise ToolError(f"规则“{name}”缺少必要的匹配内容。")
        expressions = {}
        for field_name, pattern in parts.items():
            if not isinstance(pattern, str) or not pattern.strip() or len(pattern) > 2000:
                raise ToolError(f"规则“{name}”的{RULE_FIELD_LABELS[field_name]}不能为空，最多 2000 字。")
            if kind == "keyword":
                expressions[field_name] = filter_text(pattern)
                if not expressions[field_name]:
                    raise ToolError("关键词不能只包含空白字符。")
            else:
                try:
                    expressions[field_name] = re.compile(pattern, re.IGNORECASE)
                except (re.error, OverflowError, RecursionError) as exc:
                    raise ToolError(f"规则“{name}”的{RULE_FIELD_LABELS[field_name]}正则写法有误：{exc}") from exc
        compiled.append((item, expressions))
    return compiled


def _rule_matches(normal, plain, item, expressions):
    kind = item["kind"]
    if kind == "keyword":
        return expressions["pattern"] in normal
    # 正则只检查不超过 180 字的短弹幕；关键词不受此限制。
    if len(normal) > 180:
        return False
    if kind == "full":
        return expressions["pattern"].fullmatch(plain) is not None
    if kind == "search":
        return expressions["pattern"].search(normal) is not None
    if kind == "watch":
        return bool(plain and expressions["pattern"].search(plain) and watch_sentence(plain, expressions["words"]))
    has_date = expressions["date"].search(normal) is not None
    has_time = expressions["time"].search(normal) is not None
    has_year = expressions["year"].search(normal) is not None
    remaining = expressions["time"].sub("", expressions["year"].sub("", expressions["date"].sub("", normal)))
    remaining = "".join(c for c in remaining if c.isalnum())
    return ((has_date or has_time or (has_year and remaining)) and watch_sentence(remaining, expressions["words"])) or (
        (has_date or has_time or has_year) and expressions["companion"].fullmatch(remaining) is not None)


def _evaluate_filter_rules(texts, rules, enabled=True, all_matches=False):
    compiled = compile_filter_rules(rules)
    results = []
    for text in texts:
        normal = filter_text(text)
        plain = "".join(c for c in normal if c.isalnum())
        matches = []
        for item, expressions in compiled:
            if item["enabled"] and (enabled or not item["builtin"]) and _rule_matches(normal, plain, item, expressions):
                matches.append(dict(id=item["id"], name=item["name"], builtin=item["builtin"]))
                if not all_matches:
                    break
        results.append(matches)
    return results


def evaluate_filter_rules(texts, rules, enabled=True, all_matches=False, timeout=15):
    compile_filter_rules(rules)
    trusted = {(r["kind"], tuple(sorted(r["parts"].items()))) for r in default_filter_rules()}
    changed_regex = any(r["enabled"] and (enabled or not r["builtin"]) and r["kind"] != "keyword" and
                        (r["kind"], tuple(sorted(r["parts"].items()))) not in trusted for r in rules)
    if not changed_regex:
        return _evaluate_filter_rules(texts, rules, enabled, all_matches)
    # 用户可编辑正则可能回溯很久；放入无窗口子进程，超时终止，不让合成或测试卡死。
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe" and executable.with_name("python.exe").exists():
        executable = executable.with_name("python.exe")
    payload = json.dumps(dict(texts=texts, rules=rules, enabled=enabled, all_matches=all_matches), ensure_ascii=False).encode("utf-8")
    try:
        result = subprocess.run([str(executable), str(Path(__file__).absolute()), "--_filter-rules-worker"],
                                input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise ToolError("屏蔽正则匹配超时，请停用或修改最近编辑的正则；本次未完成过滤。") from exc
    if result.returncode:
        raise ToolError("屏蔽规则执行失败：" + result.stderr.decode("utf-8", errors="replace")[-600:])
    return json.loads(result.stdout.decode("utf-8"))


def rules_with_keywords(rules=None, keywords=()):
    rows = copy.deepcopy(default_filter_rules() if rules is None else rules)
    for index, word in enumerate(keywords):
        rows.insert(index, dict(id=f"legacy-{index}", name="自定义关键词", kind="keyword", parts={"pattern": word},
                                enabled=True, builtin=False, description="包含该词的弹幕会被屏蔽。", example="", keep_example=""))
    return rows


def blocked_comment_reason(text, enabled=True, keywords=(), rules=None):
    matches = evaluate_filter_rules([text], rules_with_keywords(rules, keywords), enabled)[0]
    return matches[0]["name"] if matches else ""


def filter_rules_path():
    folder = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "NasDanmaku"
    return folder / "block-rules.local.json"


def load_filter_rules(path=None):
    path = Path(path) if path is not None else filter_rules_path()
    if not path.exists():
        return default_filter_rules()
    try:
        if path.stat().st_size > 512000:
            raise ToolError("屏蔽规则文件超过大小限制。")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != 1:
            raise ToolError("屏蔽规则文件版本不支持。")
        compile_filter_rules(data["rules"])
        return data["rules"]
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        raise ToolError("读取屏蔽规则失败：" + str(exc)) from exc


def save_filter_rules(rules, path=None, backup_dir=None):
    compile_filter_rules(rules)
    path = Path(path) if path is not None else filter_rules_path()
    content = json.dumps(dict(version=1, rules=rules), ensure_ascii=False, indent=2) + "\n"
    if len(content.encode("utf-8")) > 512000:
        raise ToolError("屏蔽规则文件超过大小限制。")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        if path.exists():
            if path.read_text(encoding="utf-8") == content:
                return
            backups = Path(backup_dir) if backup_dir is not None else local_backup_folder()
            backups.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backups / ("block-rules-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".json"))
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temporary, path)
    except (OSError, UnicodeError) as exc:
        raise ToolError("屏蔽规则未保存（备份或写入失败）：" + str(exc)) from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def local_backup_folder():
    return Path("D:/临时备份/NasDanmaku") if os.name == "nt" else filter_rules_path().parent / "backups"


def save_local_json(name, data):
    """Replace local state atomically, backing up every existing configuration."""
    path = filter_rules_path().with_name(name)
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        if path.exists():
            if path.read_text(encoding="utf-8") == content:
                return
            backups = local_backup_folder()
            backups.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backups / (path.stem + "-" + uuid.uuid4().hex + ".json"))
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary, path)
    except (OSError, UnicodeError) as exc:
        raise ToolError("本机记录未保存（备份或写入失败）：" + str(exc)) from exc
    finally:
        temporary.unlink(missing_ok=True)


def load_local_json(name, default):
    path = filter_rules_path().with_name(name)
    if not path.exists():
        return copy.deepcopy(default)
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("文件过大")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("版本或格式不支持")
        return data
    except (OSError, ValueError) as exc:
        raise ToolError(f"读取 {name} 失败，原文件已保留：{exc}") from exc


def validate_preferences(settings, density):
    result = {key: settings[key] for key in DM_DEFAULTS if key not in ("filter_rules", "block_keywords")}
    for key in ("duration", "font_size", "area", "opacity"):
        result[key] = finite(result[key], key)
    for key in ("block_scroll", "block_fixed", "block_color", "avoid_subtitles", "deduplicate", "block_noise"):
        if not isinstance(result[key], bool):
            raise ToolError("显示设置开关格式错误。")
    if result["block_scroll"] and result["block_fixed"]:
        raise ToolError("滚动和固定不能同时屏蔽。")
    render_comments([Comment(0, "设置校验")], (1920, 1080), density=density,
                    **dict(result, block_noise=False, filter_rules=[]))
    return result


def load_preferences():
    data = load_local_json("preferences.local.json", {"version": 1, "settings": {}, "density": 6, "offsets": {}})
    try:
        settings = dict(DM_DEFAULTS, **data.get("settings", {}))
        validate_preferences(settings, data.get("density", 6))
        offsets = data.get("offsets", {})
        if not isinstance(offsets, dict):
            raise ToolError("影片偏移记录格式错误。")
        settings.update(validate_preferences(settings, data.get("density", 6)))
        return settings, int(data.get("density", 6)), {key: finite(value) for key, value in offsets.items()}
    except (ValueError, TypeError, KeyError) as exc:
        raise ToolError("显示设置格式错误：" + str(exc)) from exc


def video_preference_key(video):
    return os.path.normcase(os.path.abspath(str(video)))


def store_preferences(settings, density, offsets):
    clean = validate_preferences(settings, density)
    save_local_json("preferences.local.json", dict(version=1, settings=clean, density=int(density),
                                                    offsets=dict(list(offsets.items())[-200:])))


def desktop_directory():
    """Respect Windows desktop redirection, including OneDrive and other drives."""
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                folder = Path(os.path.expandvars(winreg.QueryValueEx(key, "Desktop")[0]))
                if folder.is_absolute():
                    return folder
        except (OSError, TypeError, ValueError):
            pass
    return Path.home() / "Desktop"


def load_output_directory():
    data = load_local_json("output.local.json", {"version": 1})
    value = data.get("directory")
    if value is None:
        return desktop_directory()
    if not isinstance(value, str) or not value or "\0" in value or not Path(value).is_absolute():
        raise ToolError("已保存的本机输出目录格式错误，请重新选择保存目录。")
    return Path(value)


def checked_output_directory(value):
    folder = Path(value)
    if not folder.is_absolute() or not folder.is_dir():
        raise ToolError("保存目录不存在或无法访问，请点击“更改目录”重新选择：" + str(folder))
    return folder


def pending_outputs():
    data = load_local_json("pending-outputs.local.json", {"version": 1, "items": []})
    rows = data.get("items")
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not all(key in row for key in
            ("local_output", "video", "signature", "target")) for row in rows):
        raise ToolError("待写回记录格式错误，原文件已保留。")
    for row in rows:
        if any(not isinstance(row[key], str) or not row[key] for key in ("local_output", "video", "target")) or \
                not isinstance(row["signature"], (list, tuple)) or len(row["signature"]) != 2:
            raise ToolError("待写回记录格式错误，原文件已保留。")
    return rows


def remember_output(value, remove=False):
    with LOCAL_STATE_LOCK:
        rows = pending_outputs()
        rows = [row for row in rows if row["local_output"] != value["local_output"]]
        if not remove:
            rows.append(dict(value))
        save_local_json("pending-outputs.local.json", dict(version=1, items=rows))


def render_comments(comments, resolution, offset=0, density=6, duration=DEFAULT_DANMAKU_DURATION, font_size=32, progress=None,
                    *, area=25, opacity=80, block_scroll=False, block_fixed=True, block_color=False,
                    avoid_subtitles=True, deduplicate=True, block_noise=True, block_keywords="", filter_stats=None,
                    filter_rules=None):
    width, height = resolution
    offset, duration, font_size = finite(offset, "弹幕偏移"), finite(duration, "滚动时长"), finite(font_size, "字号")
    density, area, opacity = finite(density, "同屏条数"), finite(area, "显示区域"), finite(opacity, "不透明度")
    if not density.is_integer() or not 1 <= density <= 30 or not 2 <= duration <= 24 or not 16 <= font_size <= 100:
        raise ToolError("同屏条数范围 1–30，滚动时长 2–24 秒，字号 16–100（以 1080p 为基准）。")
    if not 10 <= area <= 100 or not 10 <= opacity <= 100:
        raise ToolError("显示区域和不透明度范围均为 10–100%。")
    keywords = compile_block_keywords(block_keywords)
    if filter_stats is not None:
        filter_stats.update(noise=0, keywords=0, types=0, duplicates=0, density=0, time=0)
    size = font_size * height / 1080
    top, row_height = height * .025, size * 1.45
    bottom = height * min(area / 100, .68 if avoid_subtitles else 1)
    lanes = int((bottom - top) / row_height)
    if lanes < 1:
        raise ToolError("当前显示区域放不下一行弹幕，请缩小字号或增大显示区域。")
    occupants = [[] for _ in range(lanes)]
    last_entry = [-math.inf] * lanes
    next_scroll = -math.inf
    gap = max(size * 1.5, width * .015)
    doc = Ass(styles={"Scroll": style("Scroll", round(size, 2))})
    doc.info.update(PlayResX=str(width), PlayResY=str(height), WrapStyle="2")
    doc.styles["Scroll"].update(Alignment="7", Outline=str(round(max(1, size / 22), 2)), MarginL="0", MarginR="0", MarginV="0")
    alpha = round(255 * (1 - opacity / 100))
    for key in ("PrimaryColour", "SecondaryColour", "OutlineColour", "BackColour"):
        doc.styles["Scroll"][key] = f"&H{alpha:02X}" + doc.styles["Scroll"][key][-6:]
    seen, omitted = {}, 0
    total = len(comments)
    ordered = sorted(comments, key=lambda x: x.time)
    report(progress, "应用弹幕屏蔽规则")
    matches = evaluate_filter_rules([c.text for c in ordered], rules_with_keywords(filter_rules, keywords), block_noise)
    report(progress, "排列弹幕", 0, total, "条")
    for index, comment in enumerate(ordered):
        # 计数指已检查的条目，包含去重/限流丢弃项。
        if index and index % max(1, total // 100) == 0:
            report(progress, "排列弹幕", index, total, "条")
        original_at = comment.time + offset
        # 按 ASS 实际写入的厘秒计算轨迹，避免四舍五入后发生重叠。
        at = round(original_at * 100) / 100
        end = round((original_at + duration) * 100) / 100
        fixed = comment.mode in (4, 5)
        if (fixed and block_fixed) or (not fixed and block_scroll) or (block_color and comment.color != 0xFFFFFF):
            omitted += 1
            if filter_stats is not None:
                filter_stats["types"] += 1
            continue
        if matches[index]:
            omitted += 1
            if filter_stats is not None:
                filter_stats["noise" if matches[index][0]["builtin"] else "keywords"] += 1
            continue
        message = escape_text(comment.text)[:120]
        if original_at < 0 or (deduplicate and message in seen and at - seen[message] < 15):
            omitted += 1
            if filter_stats is not None:
                filter_stats["time" if original_at < 0 else "duplicates"] += 1
            continue
        for active in occupants:
            active[:] = [item for item in active if item[1] > at]
        # 均匀准入，避免开头瞬间填满上限后等待整批离场；不挪动原弹幕时间。
        if sum(map(len, occupants)) >= density or (not fixed and at + 1e-9 < next_scroll):
            omitted += 1
            if filter_stats is not None:
                filter_stats["density"] += 1
            continue
        units = sum(1 if unicodedata.east_asian_width(c) in "WF" else .65 for c in message)
        length = round(max(size, units * size * 1.2), 2)
        speed = (width + 10 + length) / (end - at)
        direction = 0 if fixed else -1 if comment.mode == 6 else 1

        def can_enter(active):
            for previous_at, previous_end, previous_length, previous_speed, previous_direction in active:
                # 固定和反向相遇保守地独占行；同向则检查整段共同显示时间。
                if not direction or previous_direction != direction:
                    return False
                initial_gap = previous_speed * (at - previous_at) - previous_length
                final_gap = initial_gap + (previous_speed - speed) * (min(end, previous_end) - at)
                if min(initial_gap, final_gap) < gap:
                    return False
            return True

        order = range(lanes - 1, -1, -1) if comment.mode == 4 else range(lanes)
        candidates = [i for i in order if can_enter(occupants[i])]
        if not candidates:
            omitted += 1
            if filter_stats is not None:
                filter_stats["density"] += 1
            continue
        lane = candidates[0] if fixed else min(candidates, key=lambda i: (len(occupants[i]), last_entry[i]))
        seen[message] = at
        occupants[lane].append((at, end, length, speed, direction))
        last_entry[lane] = at
        if not fixed:
            next_scroll = at + duration / density
        color = comment.color
        bgr = f"{color & 255:02X}{color >> 8 & 255:02X}{color >> 16 & 255:02X}"
        y = top + lane * row_height
        if fixed:
            position = f"\\an8\\pos({width / 2:.2f},{y:.2f})"
        elif comment.mode == 6:
            position = f"\\an7\\move({-length:.2f},{y:.2f},{width + 10},{y:.2f})"
        else:
            position = f"\\an7\\move({width + 10},{y:.2f},{-length:.2f},{y:.2f})"
        tags = f"{{{position}\\q2\\alpha&H{alpha:02X}&\\c&H{bgr}&}}"
        doc.events.append(event(round(at * 100), round(end * 100), tags + message, "Scroll"))
    if not doc.events:
        raise ToolError("偏移/过滤后没有可显示的弹幕，请调整参数。")
    report(progress, "排列弹幕", total, total, "条", complete=True)
    return doc, omitted


def shift_events(doc, seconds, is_danmaku=False):
    delta = round(finite(seconds, "时间偏移") * 100)
    rows = []
    for row in doc.events:
        start, end = stamp(row["Start"]) + delta, stamp(row["End"]) + delta
        if end <= 0 or (is_danmaku and start < 0):
            continue
        row["Start"], row["End"] = format_stamp(start), format_stamp(end)
        rows.append(row)
    doc.events = rows


def rename_styles(doc, prefix):
    mapping = {name: f"{prefix}{i}" for i, name in enumerate(doc.styles)}
    original_default = mapping.get("Default")
    for row in doc.styles.values():
        row["Name"] = mapping[row["Name"]]
    doc.styles = {row["Name"]: row for row in doc.styles.values()}
    for row in doc.events:
        row["Style"] = mapping[row["Style"]]
        def block(match):
            def reset(found):
                old = found[1]
                if not old:
                    return r"\r"
                if old not in mapping:
                    if original_default is None:
                        raise ToolError(f"ASS 行内样式重置引用了不存在的样式：{old}")
                    return r"\r" + original_default
                return r"\r" + mapping[old]
            return re.sub(r"\\r([^\\}]*)", reset, match[0])
        row["Text"] = re.sub(r"\{[^}]*\}", block, row["Text"])


def merge_ass(subtitles, danmaku):
    base, extra = copy.deepcopy(subtitles), copy.deepcopy(danmaku)
    if base.resolution != extra.resolution:
        raise ToolError(f"两份 ASS 的画布不同：原字幕 {base.resolution}，弹幕 {extra.resolution}。请改用 XML/JSON 弹幕，工具会按原字幕画布生成；不会擅自缩放已有 ASS 特效。")
    if not base.events or not extra.events:
        raise ToolError("原字幕或弹幕在偏移后为空，未生成文件。")
    rename_styles(base, "SUB_")
    rename_styles(extra, "DM_")
    minimum = min(int(row["Layer"]) for row in base.events)
    for row in base.events:
        row["Layer"] = str(int(row["Layer"]) - minimum)
    ceiling = max(int(row["Layer"]) for row in base.events) + 1
    minimum_dm = min(int(row["Layer"]) for row in extra.events)
    for row in extra.events:
        row["Layer"] = str(int(row["Layer"]) - minimum_dm + ceiling)
        # base WrapStyle 属于原字幕；给弹幕独立设置 q，避免自动折行。
        row["Text"] = r"{\q2}" + row["Text"]
    base.styles.update(extra.styles)
    base.events.extend(extra.events)
    base.info["Title"] = "字幕加弹幕 · generated by danmaku_tool " + VERSION
    # 原字幕附件/字体保留；弹幕字体若使用外部字体需在播放设备安装。
    return base


def save_new(path, text, progress=None, message="写入影片原目录"):
    path = Path(path)
    raw = text.encode("utf-8")
    total = len(raw)
    report(progress, message, 0, total, "字节")
    path.parent.mkdir(parents=True, exist_ok=True)
    for version in range(1, 10000):
        candidate = path if version == 1 else path.with_name(f"{path.stem}-v{version}{path.suffix}")
        try:
            stream = candidate.open("xb")
        except FileExistsError:
            continue
        with stream:
            written = 0
            while written < total:
                count = stream.write(raw[written:written + 65536])
                if not count:
                    raise OSError("字幕文件写入中断。")
                written += count
                report(progress, message, written, total, "字节")
        report(progress, message, total, total, "字节", complete=True)
        return candidate
    raise ToolError("同名版本文件过多，请换一个输出目录。")


def safe_name(name):
    name = str(name).strip()
    if not name or name in {".", ".."} or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith((" ", ".")):
        raise ToolError("电影名只填文件名（不含路径），不能包含 / \\ : 等字符。")
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name):
        raise ToolError("电影名不能使用 Windows 保留名称。")
    return name


def build(subtitle=None, video=None, track=None, danmaku=None, out_dir=None, name=None,
          offset=0, subtitle_offset=0, density=6, duration=DEFAULT_DANMAKU_DURATION, font_size=32):
    if bool(subtitle) == bool(video):
        raise ToolError("请选择一份原字幕，或一部用于提取字幕的影片。")
    if not danmaku or not out_dir:
        raise ToolError("请选择弹幕文件和输出目录。")
    name = safe_name(name or Path(video or subtitle).stem)
    base = load_subtitle(subtitle) if subtitle else extract_subtitle(video, track)
    shift_events(base, subtitle_offset)
    warnings = []
    if Path(danmaku).suffix.lower() == ".ass":
        extra = parse_ass(read_text(danmaku))
        count, skipped, filtered = len(extra.events), 0, 0
        shift_events(extra, offset, is_danmaku=True)
        warnings.append("已有 ASS 弹幕保留原字号、密度和位置，仅应用时间偏移；请自行确认不会挡住台词。")
    else:
        comments, skipped = parse_comments(read_text(danmaku))
        count = len(comments)
        extra, filtered = render_comments(comments, base.resolution, offset, density, duration, font_size)
    merged = merge_ass(base, extra)
    path = save_new(Path(out_dir) / f"弹幕版-{name}.ass", merged.dumps())
    return {"output": str(normalize_path(path)), "subtitle_lines": len(base.events), "danmaku_read": count,
            "danmaku_written": len(extra.events), "invalid_or_special": skipped,
            "filtered_or_over_limit": filtered, "warnings": warnings}


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if new.scheme not in {"http", "https"} or (old.scheme == "https" and new.scheme != "https"):
            raise ToolError("接口跳转到不安全协议，已停止。")
        result = super().redirect_request(req, fp, code, msg, headers, newurl)
        if result and (old.scheme, old.netloc) != (new.scheme, new.netloc):
            for key in list(result.headers):
                if key.lower() not in {"user-agent", "accept"}:
                    del result.headers[key]
        return result



# ---- 单片自动识别与公开弹幕源 ----
import concurrent.futures
import difflib
import gzip
import queue
import threading
LOCAL_STATE_LOCK = threading.Lock()
DANDAN_REQUEST_LOCK = threading.Lock()
DANMUBOX_LOCK = threading.Lock()
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

PUBLIC_DANMAKU = "https://dmku.hls.one/"
PUBLIC_DANMAKU_BACKUP = "https://danmu.zxz.ee/"
SHOOTER_API = "https://www.shooter.cn/api/subapi.php"
THUNDER_SUBTITLE_API = "https://api-shoulei-ssl.xunlei.com/oracle/subtitle"
KAN_SEARCH = "https://api.so.360kan.com/index"
PLATFORMS = {"qq": "腾讯视频", "qiyi": "爱奇艺", "bilibili1": "哔哩哔哩", "youku": "优酷", "imgo": "芒果TV"}
PLATFORM_NAMES = dict(PLATFORMS, dandanplay="弹弹play开放弹幕网络", danmubox="弹幕盒子（历史归档）")
HOSTS = ("qq.com", "iqiyi.com", "bilibili.com", "youku.com", "mgtv.com")
HELP = "字幕弹幕一键合成 v" + VERSION + """（单文件）

使用：选择影片 → 自动识别片名、字幕及弹幕 → 核对下面的结果 → 确认合成。
也可直接在“电影名称”输入片名（可带年份，如“楚门的世界 1998”），按回车或点“按片名搜索”。
未选影片也能搜索、切换来源及补选字幕/弹幕；取得两者后点“生成到本机”，不需要影片文件。
本机成品默认保存到桌面，可在“调整与生成”中点“更改目录”，下次自动记住。
成品按片名和年份命名，可打开成品文件夹或复制路径；请自行核对电影版本和字幕时间轴。
之后选择影片文件会保留查询结果，仍可按影片文件名合成并写回原目录。
影片路径暂不可访问时，按片名搜索仍可继续；未选文件时跳过外挂、内封和文件指纹查询。
输出：已选影片时写回影片原目录 / 弹幕版-影片完整文件名.ass；未选影片时保存到所选本机目录（默认桌面）。
未选影片时不会猜测片长或按台词结束时间截断弹幕。同名文件自动加 -v2，不覆盖原文件。

只需要这一个 .py；Python 3.10+（含 Tkinter）。读取影片信息和内封文字字幕需要先安装 ffprobe/ffmpeg，并确保可在命令行中运行。
路径必须是 Windows 能读取的本地/映射盘/UNC 路径。极空间 App 里的虚拟路径或分享链接不能直接当文件路径。

自动字幕：同目录外挂中文字幕 → 迅雷 → SubHD → 射手指纹 → SubtitleCat（可能机翻）。
已有可用结果就停止查询；失败自动换源，原因会显示在日志。顶部“网页找字幕”可到 ASSRT 等网站手动补选。
ZIP 字幕包直接读取；7z/RAR 包需要本机已有 7-Zip。只下载现成字幕，不自动发起翻译或绕过网站验证。
若只有 PGS/SUP 图片字幕且在线未找到文字字幕，会明确提示；本工具不做 OCR、不假装已成功。
内封字幕只在手动点“使用内封”后选用；提取可能需扫描影片，异地较慢。
台词和弹幕先缓存到本机，合成后仅将最终 ASS 写回 NAS；不下载整部视频。
NAS 写回失败会保留本机成品和待写回记录，重启后可恢复；“待写回任务”可选择其他任务。
可以打开成品文件夹、复制路径。重试只写回原任务，不使用当前新影片的目录。
下载与合成缓存保存在 %LOCALAPPDATA%/NasDanmaku/cache，本机成品另存到所选目录，界面显示具体路径。

弹幕：按片名通过 360 影视查找电影平台链接，B 站优先直连合并 XML 与分段，其他平台向公开弹幕库按需请求；不需要你填密钥。
选中电影后，自动核实其已找到链接的各个平台，显示来源和实际取得的原始弹幕条数。
电影搜索会排除标题明确标注的鉴赏、解说和预告片，避免把它们当作电影正片弹幕。
“来源”只列出已取得并缓存的弹幕，切换直接用本机缓存。未取得的来源单独标注，详细原因见日志。
弹幕盒子历史归档也会按片名查询，成功后进入“来源”；不同版本分别显示名称和实际条数，不自动混合。
输入简称时，还会用电影搜索识别出的完整片名和年份匹配归档。
目录缓存 24 小时；7z 归档需要本机已有 7-Zip。归档是历史保存量，请核对电影版本和时间轴。
“重查来源”重试未取得、未完成及部分取得的项目，保留完整成功来源；补取失败仍保留原数据。
B 站直连失败后使用第三方缓存，会标注“第三方缓存，可重试”；条数仅代表本次取得量，完整性未验证。
公共源插入的“有多少条弹幕正在赶来”等系统提示不计入评论，也不参与合成。
顶部“弹弹play”按钮会显示已启用、已停用或未配置。已配置后自动查询，日常使用不必打开。
设置窗口直接填入已保存的 AppId 和密钥；密钥默认以圆点显示，点“显示”可查看、点“隐藏”可遮住。
启用后优先匹配文件前 16 MB 的 MD5；匹配失败继续按片名搜索，官方候选进入电影列表。
片名搜索优先查询官方 TMDB 电影目录及详情，按年份和中英文别名核对，未命中再查节目目录。
模糊候选会核对片名；多个官方候选需在列表中选择具体电影或剧集，不会默认使用第一集。
官方弹幕含关联弹幕，已应用服务返回的匹配偏移；下方偏移用于额外调整。
匹配/搜索缓存 2 小时，弹幕缓存 6 小时；可停用官方来源。
公开备用服务： https://dmku.hls.one/ ； https://danmu.zxz.ee/ 。
在线字幕：迅雷、SubHD 按片名查询；射手按视频指纹查询；SubtitleCat 作为可能机翻的末位备用。
这些外部服务可能变更/限流、没有某部电影的数据。失败会显示原因，支持修改片名重新识别或手动补选文件。
只发送查询片名、影片文件名/内容指纹、大小、时长和公开平台链接；不上传电影、原台词内容、NAS 目录或账号。

识别结果需核对片名、年份、时长，平台上架年份有时与上映年不同。
弹幕偏移：正数延后、负数提前。不同剪辑版本可能无法只用一个偏移完全对齐。
默认弹幕只在顶部 1/4 滚动，字号 50（1080p 基准）、不透明度 80%、最多同屏 6 条，屏蔽固定弹幕。
默认速度 0.5×，每条滚动 24 秒；1× 为 12 秒，2× 为 6 秒。“恢复默认”也使用字号50和0.5×。
同向弹幕留够安全间距即可接续进入，按同屏上限分散进入节奏，不再等待整批走完；原时间点不后移。
默认过滤日期/时间打卡、报几刷、陪谁看、在吗/有人吗、重复字母数字及明显广告；“弹幕设置”可关闭。
在“弹幕设置 → 屏蔽规则（内置＋自定义）”查看全部六条内置规则，可修改、停用、删除或恢复默认。
每条规则都有用途、屏蔽/保留示例和实际匹配内容；日期等组合规则可通过下拉列表逐项编辑。
可新增普通关键词（包含就屏蔽），也可新增正则（按写法规律匹配）。不懂正则，直接用关键词即可。
输入一句弹幕点“测试这句弹幕”，能看到是否屏蔽、具体命中了哪些规则；示例文字仅作说明。
规则点击“保存并生效”即可保存在本机；退出未保存编辑时会提示。自定义规则独立于内置总开关。
规则文件：%LOCALAPPDATA%/NasDanmaku/block-rules.local.json；修改已有文件前备份到 D:/临时备份/NasDanmaku。
正则只检查不超过 180 字的弹幕；错误写法会提示，修改后的正则匹配超时会停止，不会卡住一直等待。
过滤发生在合成时，仅影响在线/XML/JSON 弹幕；原始缓存和台词不变，日志显示屏蔽数量。
“弹幕设置”可调显示区域、字号、不透明度、速度及类型过滤；只影响弹幕，不改原台词字幕。
勾选防挡字幕时，即使放大区域也会保留底部 32%；固定弹幕手动开启后同样限制在所选区域内。
显示设置和同屏数量会记住；弹幕偏移按影片完整路径分别保存。换影片默认使用该片上次偏移，没有记录则为零。
调整后点确认合成，无需重新下载；已有 ASS 不会自动改变。
手动导入的 ASS 已有排版，只支持时间偏移；要调整区域和字号请用在线弹幕或 XML/JSON。
字幕与弹幕同属一条 ASS，播放时选择这条字幕即可。想仅看台词，选回原字幕轨。

查询结果陆续显示；已有字幕和弹幕后，可点“使用已取得结果合成”结束等待。
“停止等待”保留已有结果并解除界面等待，尚在连接的请求会在完成或超时后退出；合成与写回不支持中途取消。
进度条表示当前步骤；切换步骤时会归零。没有可用总量时只显示等待/接收量和耗时。
主窗口左侧选择影片、字幕和来源，右侧常驻弹幕状态及运行日志，无需滚动整页找日志。
底部固定显示操作按钮、进度和过滤统计；设置窗口内容仍可滚动，日志有自己的滚动条。
双击 .py 或运行 python nas_danmaku.py；也可把影片路径作为第一个参数传入。
"""


def web_bytes(url, data=None, timeout=25, progress=None, message="下载数据", opener=None, request_headers=None):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ToolError("服务返回了无效的下载链接。")
    headers = {"User-Agent": "Mozilla/5.0 SubtitleDanmaku/2.0", "Accept-Encoding": "identity"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if request_headers:
        headers.update(request_headers)
    req = urllib.request.Request(url, data=data, headers=headers)
    report(progress, message)
    try:
        with (opener or urllib.request.build_opener(SafeRedirect())).open(req, timeout=timeout) as response:
            length = response.headers.get("Content-Length", "")
            total = int(length) if length.isdigit() and not response.headers.get("Transfer-Encoding") else None
            if total is not None and total > MAX_BYTES:
                raise ToolError("服务返回超过 32 MB，已停止。")
            chunks, received = [], 0
            report(progress, message, received, total, "字节")
            read = getattr(response, "read1", response.read)
            while chunk := read(min(65536, MAX_BYTES + 1 - received)):
                chunks.append(chunk)
                received += len(chunk)
                if received > MAX_BYTES:
                    raise ToolError("服务返回超过 32 MB，已停止。")
                report(progress, message, received, total, "字节")
            if total is not None and received != total:
                raise ToolError("下载未完成：实际接收量与服务返回的文件大小不一致，请重试。")
            raw = b"".join(chunks)
    except urllib.error.HTTPError as exc:
        raise ToolError(f"{parsed.hostname} 返回 HTTP {exc.code}，服务暂不可用。") from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        raise ToolError(f"连接 {parsed.hostname} 失败或超时。") from None
    report(progress, message, received, total, "字节", complete=True)
    return raw


def web_json(url, data=None, progress=None, message="下载数据"):
    try:
        raw = web_bytes(url, data, progress=progress, message=message)
        report(progress, "解析下载的数据")
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        raise ToolError("在线服务返回内容不是有效 JSON。") from None


def dandan_protect(value, decrypt=False):
    """Protect this machine's application key with Windows CurrentUser DPAPI."""
    if os.name != "nt":
        raise ToolError("非 Windows 请使用 DANDANPLAY_APP_ID 和 DANDANPLAY_APP_SECRET 环境变量。")
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]
    raw = base64.b64decode(value, validate=True) if decrypt else value.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw)
    incoming = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    outgoing = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    method = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    method.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    method.restype = wintypes.BOOL
    if not method(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ToolError("无法读写本机加密凭证，请用当前 Windows 账号重新配置。")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        result = ctypes.string_at(outgoing.data, outgoing.length)
        return result.decode("utf-8") if decrypt else base64.b64encode(result).decode("ascii")
    finally:
        kernel.LocalFree(outgoing.data)


def dandan_config(*, include_disabled=False):
    data = load_local_json("dandanplay.local.json", dict(version=1, enabled=False, app_id="", protected_secret=""))
    app_id = os.environ.get("DANDANPLAY_APP_ID", "") or data.get("app_id", "")
    secret = os.environ.get("DANDANPLAY_APP_SECRET", "")
    if not secret and data.get("protected_secret") and (data.get("enabled") or include_disabled):
        try:
            secret = dandan_protect(data["protected_secret"], decrypt=True)
        except (ValueError, UnicodeError):
            raise ToolError("弹弹play本机凭证损坏，请重新配置。") from None
    return dict(enabled=bool(data.get("enabled") or os.environ.get("DANDANPLAY_APP_ID")),
                app_id=app_id, secret=secret)


def save_dandan_config(app_id, secret, enabled):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", app_id):
        raise ToolError("请填写有效的 AppId。")
    data = load_local_json("dandanplay.local.json", dict(version=1))
    if secret:
        if len(secret) > 4096 or any(c.isspace() for c in secret):
            raise ToolError("AppSecret 格式不正确，请重新粘贴。")
        data["protected_secret"] = dandan_protect(secret)
    elif data.get("app_id") != app_id:
        data["protected_secret"] = ""
    if enabled and not data.get("protected_secret") and not os.environ.get("DANDANPLAY_APP_SECRET"):
        raise ToolError("启用前请填写 AppSecret。")
    data.update(app_id=app_id, enabled=bool(enabled), version=1)
    save_local_json("dandanplay.local.json", data)


def dandan_setup_state():
    """Status metadata only; opening settings loads credentials separately."""
    try:
        data = load_local_json("dandanplay.local.json", dict(version=1, app_id="", enabled=True))
    except ToolError as exc:
        return dict(label="配置异常", app_id="", enabled=False, configured=False, environment=False, error=str(exc))
    app_id = os.environ.get("DANDANPLAY_APP_ID", "") or data.get("app_id", "")
    configured = bool(app_id and (os.environ.get("DANDANPLAY_APP_SECRET") or data.get("protected_secret")))
    enabled = bool(data.get("enabled") or os.environ.get("DANDANPLAY_APP_ID"))
    return dict(label=("已启用" if enabled else "已停用") if configured else "未配置", app_id=app_id,
                enabled=enabled, configured=configured,
                environment=bool(os.environ.get("DANDANPLAY_APP_ID") or os.environ.get("DANDANPLAY_APP_SECRET")), error="")


def dandan_headers(app_id, secret, path, timestamp=None):
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    path = urllib.parse.urlsplit(path).path
    signature = base64.b64encode(hashlib.sha256((app_id + timestamp + path + secret).encode("utf-8")).digest()).decode("ascii")
    return {"X-AppId": app_id, "X-Timestamp": timestamp, "X-Signature": signature,
            "Content-Type": "application/json", "Accept": "application/json"}


def dandan_request(path, payload=None, progress=None, *, use_cache=True):
    # Only documented public catalog, matching and comment routes are used.
    route = urllib.parse.urlsplit(path).path
    if route not in {"/api/v2/match", "/api/v2/search/episodes", "/api/v2/search/tmdb"} and not re.fullmatch(
            r"/api/v2/(?:comment/[1-9]\d*|bangumi/tmdb-movie-[1-9]\d*)", route):
        raise ToolError("不支持的弹弹play接口。")
    config = dandan_config()
    if not config["enabled"] or not config["app_id"] or not config["secret"]:
        raise ToolError("请先点击顶部“弹弹play”按钮，配置并启用官方来源。")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if payload is not None else None
    cache_key = hashlib.sha256(config["app_id"].encode() + path.encode() + (body or b"")).hexdigest()
    cache = filter_rules_path().parent / "cache" / "dandanplay" / (cache_key + ".json")
    ttl = 6 * 3600 if "/comment/" in route else 2 * 3600
    with DANDAN_REQUEST_LOCK:
        report(progress, "查询弹弹play开放弹幕网络")
        if use_cache and cache.is_file() and 0 <= time.time() - cache.stat().st_mtime < ttl:
            try:
                data = json.loads(read_text(cache))
                if isinstance(data, dict) and not ("/comment/" in route and data.get("comments") == []):
                    report(progress, "使用弹弹play本机缓存")
                    return data
            except (OSError, ValueError, ToolError):
                pass
        headers = dandan_headers(config["app_id"], config["secret"], route)
        try:
            raw = web_bytes("https://api.dandanplay.net" + path, data=body, progress=progress,
                            message="获取弹弹play官方数据", request_headers=headers)
        except ToolError as exc:
            message = str(exc)
            if "HTTP 403" in message or "HTTP 401" in message:
                raise ToolError("弹弹play认证未通过，请核对应用凭证、应用状态及电脑时间。") from None
            if "HTTP 429" in message:
                raise ToolError("弹弹play调用额度或频率受限，请稍后重试；已有缓存仍可使用。") from None
            raise
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeError, ValueError):
            raise ToolError("弹弹play返回的数据格式不正确。") from None
        if not isinstance(data, dict):
            raise ToolError("弹弹play返回的数据格式不正确。")
        if data.get("success") is False:
            # Avoid echoing remote messages or request details containing authentication data.
            raise ToolError("弹弹play未完成本次查询，请检查关键词或稍后重试。")
        if "/comment/" in route and data.get("comments") == []:
            return data  # Let a user's explicit retry query the server again.
        cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, cache)
        finally:
            temporary.unlink(missing_ok=True)
        return data


def dandan_movies(data, search=False):
    rows = []
    if search:
        for anime in data.get("animes") or []:
            for episode in anime.get("episodes") or []:
                rows.append(dict(episode, animeTitle=anime.get("animeTitle", ""), year=anime.get("year", ""),
                                 official_aliases=anime.get("official_aliases", [])))
    else:
        rows = data.get("matches") or []
    movies, seen = [], set()
    for row in rows:
        try:
            episode_id = int(row["episodeId"])
            if episode_id <= 0 or episode_id in seen:
                continue
            title = str(row.get("animeTitle") or "未命名作品")
            episode = str(row.get("episodeTitle") or "")
            seen.add(episode_id)
            movies.append(dict(title=title + (" · " + episode if episode and episode != title else ""),
                               year=str(row.get("year") or ""), duration="",
                               links={"dandanplay": f"https://api.dandanplay.net/api/v2/comment/{episode_id}"},
                               official_title=title, official_shift=finite(row.get("shift", 0)),
                               official_aliases=row.get("official_aliases", []),
                               official_exact=bool(data.get("isMatched")) and not search))
        except (ValueError, TypeError, KeyError, ToolError):
            continue
    return movies


def dandan_match(video, meta, progress=None):
    video = Path(video)
    report(progress, "读取影片前 16 MB 进行弹弹play匹配")
    signature = file_signature(video)
    digest, received, limit = hashlib.md5(), 0, 16 * 1024 * 1024
    with video.open("rb") as stream:
        while received < limit:
            report(progress, "读取影片前 16 MB 进行弹弹play匹配", received, min(signature[0], limit), "字节")
            chunk = stream.read(min(65536, limit - received))
            if not chunk:
                break
            digest.update(chunk)
            received += len(chunk)
    if file_signature(video) != signature:
        raise ToolError("影片读取时发生变化，请重新识别。")
    payload = dict(fileName=video.stem, fileHash=digest.hexdigest(), fileSize=signature[0],
                   videoDuration=int(finite(meta.get("format", {}).get("duration", 0) or 0)), matchMode="hashAndFileName")
    return dandan_movies(dandan_request("/api/v2/match", payload, progress))


def dandan_tmdb_search(title, progress=None, year=""):
    """Movie catalog entries need their detail route to obtain real episode IDs."""
    query = title.strip()
    bilingual = re.match(r"^([\u4e00-\u9fff][^A-Za-z]*?)\s+[A-Za-z]", query)
    if bilingual:
        query = bilingual.group(1).strip()
    data = dandan_request("/api/v2/search/tmdb?" + urllib.parse.urlencode({"keyword": query}), progress=progress)
    animes, failures, attempted, seen = [], [], 0, set()
    for row in data.get("animes") or []:
        bangumi_id = str(row.get("bangumiId") or "")
        if not re.fullmatch(r"tmdb-movie-[1-9]\d*", bangumi_id) or bangumi_id in seen:
            continue
        seen.add(bangumi_id)
        found_year = str(row.get("startDate") or "")[:4]
        if year and found_year and str(year) != found_year:
            continue
        if attempted >= 3:
            report(progress, "电影候选较多，请填写更具体的片名缩小范围")
            break
        attempted += 1
        try:
            response = dandan_request("/api/v2/bangumi/" + bangumi_id, progress=progress)
            detail = response.get("bangumi") or {}
            animes.append(dict(animeTitle=detail.get("animeTitle") or row.get("animeTitle", ""),
                               year=found_year, episodes=detail.get("episodes") or [],
                               official_aliases=[item.get("title", "") for item in detail.get("titles") or []]))
        except (ToolError, OSError) as exc:
            failures.append(str(exc))
            report(progress, "弹弹play电影详情未取得：" + str(exc))
    candidates = dandan_candidates(dandan_movies({"animes": animes}, search=True), title, year)
    if not candidates and failures:
        raise ToolError("弹弹play电影详情查询未完成：" + failures[0])
    return candidates


def dandan_search(title, progress=None, year=""):
    if len(title.strip()) < 2:
        raise ToolError("搜索片名至少需要两个字符。")
    movie_error = None
    try:
        movies = dandan_tmdb_search(title, progress, year)
        if movies:
            return movies
    except (ToolError, OSError) as exc:
        movie_error = exc
        report(progress, "弹弹play电影目录暂不可用，继续节目搜索：" + str(exc))
    data = dandan_request("/api/v2/search/episodes?" + urllib.parse.urlencode({"anime": title.strip(), "v2": "true"}), progress=progress)
    if data.get("hasMore"):
        report(progress, "弹弹play结果较多，请填写更具体的片名或集数缩小范围")
    movies = dandan_candidates(dandan_movies(data, search=True), title, year)
    if not movies and movie_error:
        raise ToolError("弹弹play电影搜索未完成：" + str(movie_error))
    return movies


def normalize_title(text):
    return "".join(c for c in unicodedata.normalize("NFKC", html.unescape(str(text))).casefold() if c.isalnum())


def dandan_candidates(movies, title, year=""):
    """Keep exact file matches and plausible titles; multiple episodes need a choice."""
    queries = [normalize_title(title)]
    bilingual = re.match(r"^([\u4e00-\u9fff][^A-Za-z]*?)\s+[A-Za-z]", title)
    if bilingual:
        queries.append(normalize_title(bilingual.group(1)))
    candidates = []
    for movie in movies:
        if not movie.get("official_exact") and year and movie.get("year") and str(year) != str(movie["year"]):
            continue
        titles = [movie.get("official_title", movie["title"])] + list(movie.get("official_aliases") or [])
        if movie.get("official_exact") or any(query and
                difflib.SequenceMatcher(None, normalize_title(found), query).ratio() >= .72
                for query in queries for found in titles):
            candidates.append(dict(movie))
    for movie in candidates:
        movie["official_confirm"] = not movie.get("official_exact") and len(candidates) > 1
    return candidates


def filename_title(stem):
    # 先切除技术参数，再寻找上映年份；保留 1917、2012 这样的数字片名。
    text = re.sub(r"(?i)[. _\-]+(?:2160p|1080[pi]|720p|480p|4k|8k|blu[ ._-]?ray|bdrip|brrip|web[ ._-]?dl|webrip|hdtv|remux|x26[45]|h[ .]?26[45]|hevc|avc|dvdrip)\b.*", "", stem)
    years = list(re.finditer(r"(?<!\d)((?:18|19|20)\d{2})(?!\d)", text))
    year = ""
    for match in years:
        prefix = text[:match.start()].strip(" ._-([{")
        if prefix:
            year = match[1]
            text = prefix
            break
    text = re.sub(r"\[[^\]]*\]", lambda m: m[0][1:-1] if re.search(r"[\u4e00-\u9fff]", m[0]) else " ", text)
    text = re.sub(r"(?i)\b(?:extended|remastered|unrated|directors?\s*cut|repack|proper)\b", " ", text)
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -_()[]{}")
    return text or stem, year


def identify_movie(video, override=""):
    video = Path(video)
    title, year = filename_title(video.stem)
    source = "文件名"
    nfo = video.with_suffix(".nfo")
    if not nfo.is_file():
        possible = video.parent / "movie.nfo"
        if possible.is_file():
            movies = [p for p in video.parent.iterdir() if p.suffix.lower() in {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts"}]
            if len(movies) == 1:
                nfo = possible
    if nfo.is_file():
        try:
            text = read_text(nfo)
            if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
                raise ValueError()
            root = ET.fromstring(text)
            nfo_title = root.findtext("title")
            if nfo_title:
                title = nfo_title.strip()
                year = (root.findtext("year") or year).strip()
                source = "影片 NFO"
        except (OSError, ValueError, ET.ParseError, ToolError):
            pass
    if override.strip():
        title = override.strip()
        source = "修正片名"
    return {"title": title, "year": year, "source": source}


def inspect_video(video):
    validate_video(video)
    return json.loads(run_media("ffprobe", ["-probesize", "8000000", "-analyzeduration", "5000000",
                      "-show_streams", "-show_format", "-of", "json", str(video)], timeout=20))


def validate_video(video):
    video = Path(video)
    if not video.is_file():
        raise ToolError("影片路径当前不可访问。NAS 盘请先在文件资源管理器里打开并登录，然后重试。")
    if video.suffix.lower() not in {".mkv", ".mp4", ".m4v", ".mov", ".avi", ".ts", ".m2ts", ".wmv", ".webm"}:
        raise ToolError("请选择常见影片文件，例如 MKV/MP4；不支持把网页分享链接当作文件路径。")


@dataclass
class SubtitleChoice:
    label: str
    kind: str
    score: int = 0
    path: str = ""
    index: int | None = None
    doc: Ass | None = None
    delay: float = 0


def local_workspace():
    base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "NasDanmaku" / "cache"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="movie-", dir=base))


def cache_subtitle(choice, folder):
    if choice.doc is None:
        choice.doc = load_subtitle(choice.path)
    # 缓存原始时间轴；服务返回的 delay 只在合成时应用一次。
    path = save_new(Path(folder) / "subtitle.ass", choice.doc.dumps(), message="缓存台词字幕到本机")
    choice.path = str(path)
    return choice


def language_score(text):
    low = text.lower()
    if re.search(r"(?i)(?:\b(?:zh|zho|chi|chs|cht|zh-cn|zh-tw)\b|中文|简体|繁体|中英|双语|chinese)", low):
        score = 100
    elif re.search(r"(?i)(?:\b(?:eng|en|english|jpn|japanese|kor|korean)\b|英文|日语|韩语)", low):
        score = 10
    else:
        score = 50
    if any(word in low for word in ("forced", "强制", "commentary", "评论")):
        score -= 20
    return score


def sidecar_choices(video):
    video = Path(video)
    choices = []
    for path in video.parent.iterdir():
        if path.suffix.lower() not in {".ass", ".srt"} or not path.is_file():
            continue
        if re.search(r"(?i)(字幕加弹幕|弹幕|danmaku|danmu)", path.stem):
            continue
        suffix = path.stem[len(video.stem):] if path.stem.casefold().startswith(video.stem.casefold()) else None
        if suffix is None or (suffix and suffix[0] not in " ._-(["):  # Movie2 不能当 Movie 的字幕
            continue
        score = language_score(suffix) + 15
        choices.append(SubtitleChoice("外挂 · " + path.name, "file", score, str(path)))
    return sorted(choices, key=lambda c: -c.score)


def embedded_choices(meta):
    choices = []
    for stream in meta.get("streams", []):
        if stream.get("codec_type") != "subtitle" or stream.get("codec_name") not in TEXT_CODECS:
            continue
        tags = stream.get("tags", {})
        language = tags.get("language", "未标注语言")
        title = tags.get("title", "")
        score = language_score(language + " " + title)
        if stream.get("disposition", {}).get("forced"):
            score -= 20
        label = f"内封 · #{stream['index']} · {language} · {stream.get('codec_name')} · {title}"
        choices.append(SubtitleChoice(label, "embedded", score, index=int(stream["index"])))
    return sorted(choices, key=lambda c: -c.score)


def shooter_hash(video):
    size = Path(video).stat().st_size
    if size < 12288:
        raise ToolError("影片太小，无法计算在线字幕指纹。")
    chunks = []
    with Path(video).open("rb") as source:
        for position in (4096, (size // 3) * 2, size // 3, size - 8192):
            source.seek(position)
            chunks.append(hashlib.md5(source.read(4096)).hexdigest())
    return ";".join(chunks)


def decode_subtitle(raw):
    if raw.startswith(b"\x1f\x8b"):
        import io
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ToolError("在线字幕解压后超过大小限制。")
    encodings = ("utf-16",) if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except UnicodeError:
            pass
    raise ToolError("在线字幕编码无法识别。")


def online_subtitles(video):
    # 仅提交四个块的 MD5 和文件名，不提交 NAS 目录，也不上传原文件。
    payload = urllib.parse.urlencode({"filehash": shooter_hash(video), "pathinfo": Path(video).name, "format": "json", "lang": "Chn"}).encode()
    raw = web_bytes(SHOOTER_API, data=payload)
    if raw.strip() in {b"\xff", b"-1", b"[]", b""}:
        return []
    try:
        rows = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        raise ToolError("在线字幕源返回了无法识别的响应。") from None
    if not isinstance(rows, list):
        raise ToolError("在线字幕源格式发生变化。")
    choices = []
    for row in rows[:3]:
        for file in row.get("Files", []):
            ext = str(file.get("Ext", "")).lower().lstrip(".")
            if ext not in {"srt", "ass"}:
                continue
            try:
                text = decode_subtitle(web_bytes(file["Link"]))
                doc = parse_srt(text) if ext == "srt" else parse_ass(text)
                label = "在线匹配 · 射手 · " + (str(row.get("Desc", ""))[:90] or ext.upper())
                choices.append(SubtitleChoice(label, "online", 95, doc=doc, delay=finite(row.get("Delay", 0), "字幕延迟") / 1000))
            except (ToolError, KeyError, ValueError):
                continue
    return choices


def title_subtitles(video, identity, meta, progress=None):
    title, year = identity["title"], identity.get("year", "")
    bilingual = re.match(r"^([\u4e00-\u9fff][^A-Za-z]*?)\s+[A-Za-z]", title)
    query = bilingual[1].strip() if bilingual else title
    report(progress, "按片名搜索在线中文字幕")
    data = web_json(THUNDER_SUBTITLE_API + "?" +
                    urllib.parse.urlencode({"name": (query + " " + year).strip()}))
    if not isinstance(data, dict) or data.get("code") != 0 or not isinstance(data.get("data"), list):
        raise ToolError("迅雷字幕源未返回有效列表。")
    length = finite(meta.get("format", {}).get("duration", 0) or 0)
    ranked, seen = [], set()
    for row in data["data"]:
        if not isinstance(row, dict):
            continue
        name, simple = str(row.get("name", "")), str(row.get("simple_name", ""))
        ext, url = str(row.get("ext", "")).lower().lstrip("."), row.get("url")
        if ext not in {"srt", "ass"} or not isinstance(url, str) or url in seen:
            continue
        # 标题检索不等于精确版本匹配：拒绝另一部片、冲突年份和分碟字幕。
        if normalize_title(query) not in normalize_title(simple + " " + name):
            continue
        years = re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", simple + " " + name)
        if year and years and year not in years:
            continue
        if re.search(r"(?i)(?:^|[. _\-])(?:cd|disc|disk|d)[ ._\-]*[1-9](?:[. _\-]|$)", name):
            continue
        langs = row.get("languages", [])
        if not isinstance(langs, list):
            continue
        score = language_score(" ".join(str(x) for x in langs) + " " + simple)
        if score < 90:
            continue
        try:
            end = finite(row.get("duration", 0) or 0) / 1000
        except ToolError:
            continue
        # duration 是末句字幕时间，不是影片片长；允许片尾无台词。
        if length and end and not length * .70 <= end <= length * 1.10:
            continue
        if "简体" in langs:
            score += 10
        if "双语" in simple + name or "中英" in simple + name:
            score += 5
        if year and year in years:
            score += 5
        if length and end:
            score += max(0, 10 - abs(length - end) / length * 50)
        seen.add(url)
        ranked.append((score, row))
    choices, errors = [], []
    for _, row in sorted(ranked, key=lambda x: -x[0])[:6]:
        ext = str(row["ext"]).lower().lstrip(".")
        try:
            raw = web_bytes(row["url"], timeout=12, progress=progress, message="下载在线台词字幕到本机")
            text = decode_subtitle(raw)
            doc = parse_srt(text) if ext == "srt" else parse_ass(text)
            if len(doc.events) < 10 or not any(re.search(r"[\u4e00-\u9fff]", e["Text"]) for e in doc.events):
                raise ToolError("下载内容不是完整中文字幕。")
            label = "在线 · 迅雷 · " + str(row.get("name", ""))[:100] + "（需核对版本）"
            choices.append(SubtitleChoice(label, "online", 100, doc=doc))
        except (ToolError, UnicodeError, OSError) as exc:
            errors.append(str(exc))
            if len(errors) >= 2:
                break
            continue
        if len(choices) >= 3:
            break
    if ranked and not choices:
        raise ToolError("迅雷已找到候选字幕，但下载或解析失败：" + "；".join(dict.fromkeys(errors)))
    return choices


class PageLinks(HTMLParser):
    """只读取页面上的链接和文字，不执行脚本。"""
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.links, self.active = [], None
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.active = [dict(attrs), []]

    def handle_data(self, data):
        if self.active is not None:
            self.active[1].append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.active is not None:
            attrs, parts = self.active
            self.links.append((attrs, " ".join("".join(parts).split())))
            self.active = None


class SubtitleSession:
    def __init__(self, label, base, progress, budget=45):
        self.label, self.base, self.progress = label, base, progress
        self.deadline = time.monotonic() + budget
        self.opener = urllib.request.build_opener(SafeRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get(self, url, data=None, referer=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ToolError(self.label + "本次查询超时，切换下一来源。")
        headers = {"Referer": referer or self.base}
        if data is not None:
            data = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        return web_bytes(url, data=data, timeout=min(12, remaining), progress=self.progress,
                         message="查询/下载 " + self.label, opener=self.opener, request_headers=headers)

    def page(self, url):
        return self.get(url).decode("utf-8-sig", errors="replace")

    def post(self, path, data, referer):
        try:
            result = json.loads(self.get(self.base + path, data, referer))
        except (ValueError, UnicodeError):
            raise ToolError(self.label + "返回的不是有效数据，可能需要在网页验证。") from None
        if not isinstance(result, dict) or result.get("success") is not True:
            raise ToolError(self.label + "下载受限或需网页确认：" + str(result.get("msg", "") if isinstance(result, dict) else ""))
        return result


def subtitle_queries(video, identity):
    titles = [identity["title"]]
    if video is not None:
        titles.append(filename_title(Path(video).stem)[0])
    queries = []
    for title in titles:
        bilingual = re.match(r"^([\u4e00-\u9fff][^A-Za-z]*?)\s+([A-Za-z].*)$", title)
        queries.extend([bilingual[1].strip(), bilingual[2].strip()] if bilingual else [title])
    return list(dict.fromkeys(q for q in queries if q.strip()))


def candidate_matches(name, queries, year):
    name = urllib.parse.unquote(name)
    if not any(normalize_title(q) in normalize_title(name) for q in queries):
        return False
    years = re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", name)
    if year and years and year not in years:
        return False
    return not re.search(r"(?i)(?:^|[. _\-])(?:cd|disc|disk|d)[ ._\-]*[1-9](?:[. _\-]|$)", name)


def safe_archive_member(name):
    parts = name.replace("\\", "/").split("/")
    return bool(name) and not name.startswith(("/", "\\", "-", "@")) and ":" not in name and ".." not in parts


def subtitle_archive_members(raw, *, extensions=(".ass", ".srt")):
    """仅返回文字字幕字节；压缩包里的路径不落地。"""
    if raw.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                items = archive.infolist()
                if len(items) > 100 or sum(i.file_size for i in items) > MAX_BYTES:
                    raise ToolError("字幕压缩包解压总量或文件数超限。")
                for item in items:
                    if safe_archive_member(item.filename) and not item.flag_bits & 1 and Path(item.filename).suffix.lower() in extensions:
                        yield item.filename, archive.read(item)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
            raise ToolError("无法读取字幕 ZIP：" + str(exc)) from exc
        return
    if not raw.startswith((b"7z\xbc\xaf\x27\x1c", b"Rar!")):
        text = decode_subtitle(raw)
        yield "subtitle.ass" if "[Events]" in text else "subtitle.srt", raw
        return
    executable = shutil.which("7z") or shutil.which("7zz")
    if not executable:
        installed = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "7-Zip" / "7z.exe"
        executable = str(installed) if installed.is_file() else None
    if not executable:
        raise ToolError("字幕为 7z/RAR 包，需要安装 7-Zip 并加入 PATH；将继续尝试其他来源。")
    with tempfile.TemporaryDirectory(prefix="nas-subtitles-") as temporary:
        archive = Path(temporary) / "subtitle.archive"
        archive.write_bytes(raw)
        def run(args):
            try:
                result = subprocess.run([executable] + args, stdin=subprocess.DEVNULL, capture_output=True,
                                        timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except subprocess.TimeoutExpired as exc:
                raise ToolError("字幕压缩包处理超时。") from exc
            if result.returncode or len(result.stdout) > MAX_BYTES:
                raise ToolError("字幕压缩包损坏、加密或解压大小超限。")
            return result.stdout
        listing = run(["l", "-slt", "-sccUTF-8", str(archive)]).decode("utf-8-sig", errors="replace")
        if "----------" not in listing:
            raise ToolError("无法识别 7-Zip 文件列表。")
        items = [dict(line.split(" = ", 1) for line in block.splitlines() if " = " in line)
                 for block in re.split(r"\r?\n\r?\n", listing.split("----------", 1)[1].strip())]
        if len(items) > 100 or any(not item.get("Size", "0").isdigit() for item in items) or sum(int(i.get("Size", 0)) for i in items) > MAX_BYTES:
            raise ToolError("字幕压缩包解压总量或文件数超限。")
        for item in items:
            name = item.get("Path", "")
            if not safe_archive_member(name) or item.get("Encrypted") == "+" or Path(name).suffix.lower() not in extensions:
                continue
            yield name, run(["x", "-so", "-bd", "-bb0", "-bsp0", "-spd", str(archive), name])


def downloaded_choices(raw, source, title, meta, machine=False):
    choices = []
    length = finite(meta.get("format", {}).get("duration", 0) or 0)
    for name, content in subtitle_archive_members(raw):
        try:
            text = decode_subtitle(content)
            doc = parse_ass(text) if Path(name).suffix.lower() == ".ass" else parse_srt(text)
            chinese = sum(bool(re.search(r"[\u4e00-\u9fff]", e["Text"])) for e in doc.events)
            if len(doc.events) < 10 or chinese < min(5, max(1, len(doc.events) // 20)):
                continue
            if re.search(r"(?i)(?:^|[. _\-])(?:cd|disc|disk|d)[ ._\-]*[1-9](?:[. _\-]|$)", name):
                continue
            end = max(stamp(e["End"]) for e in doc.events) / 100
            if length and not length * .70 <= end <= length * 1.10:
                continue
            score = 100 + (10 if re.search(r"(?i)(简|chs|zh-cn)", name) else 0)
            label = f"在线 · {source}" + (" · 可能机翻" if machine else "") + " · " + title[:70]
            if name not in {"subtitle.ass", "subtitle.srt"}:
                label += " · " + Path(name).name[-55:]
            choices.append(SubtitleChoice(label, "online", score, doc=doc))
        except (ToolError, UnicodeError):
            continue
    return sorted(choices, key=lambda c: -c.score)[:3]


def subhd_subtitles(video, identity, meta, progress=None):
    queries = subtitle_queries(video, identity)
    session = SubtitleSession("SubHD", "https://subhd.tv", progress)
    search = session.base + "/search/" + urllib.parse.quote(queries[0], safe="")
    links = PageLinks(session.page(search)).links
    candidates = {}
    for attrs, title in links:
        href = attrs.get("href", "")
        if re.fullmatch(r"/a/[A-Za-z0-9]+", href):
            candidates[href] = candidates.get(href, "") + " " + title
    candidates = [(href, title.strip()) for href, title in candidates.items()
                  if candidate_matches(title, queries, identity.get("year", ""))]
    candidates.sort(key=lambda item: ("国配" in item[1], "特效" in item[1], -language_score(item[1])))
    errors = []
    for href, title in candidates[:3]:
        detail = session.base + href
        try:
            session.page(detail)
            sid = href.rsplit("/", 1)[1]
            prepared = session.post("/api/sub/prepare-download", {"sid": sid}, detail)
            path = prepared.get("url", "")
            if not isinstance(path, str) or not re.fullmatch(r"/down/[A-Za-z0-9]+", path):
                raise ToolError("SubHD 未提供有效下载页。")
            session.page(session.base + path)
            data = session.post("/api/sub/down", {"sid": sid}, session.base + path)
            if data.get("pass") is not True or not isinstance(data.get("url"), str):
                raise ToolError("SubHD 需要在网页完成验证，已跳过自动下载。")
            raw = session.get(data["url"], referer=session.base + path)
            choices = downloaded_choices(raw, "SubHD", title, meta)
            if choices:
                return choices
            errors.append("候选中未找到完整中文字幕")
        except (ToolError, OSError) as exc:
            errors.append(str(exc))
            if "验证" in str(exc) or "HTTP 403" in str(exc) or "HTTP 429" in str(exc):
                break
    if candidates:
        raise ToolError("SubHD 找到候选但未能取得字幕：" + "；".join(dict.fromkeys(errors)))
    return []


def subtitlecat_subtitles(video, identity, meta, progress=None):
    queries = subtitle_queries(video, identity)
    query = next((q for q in queries if re.search(r"[A-Za-z]", q)), queries[0])
    session = SubtitleSession("SubtitleCat", "https://www.subtitlecat.com", progress)
    links = PageLinks(session.page(session.base + "/index.php?" + urllib.parse.urlencode({"search": query}))).links
    candidates = []
    for attrs, title in links:
        href = attrs.get("href", "")
        if re.fullmatch(r"/?subs/\d+/[^?#]+\.html", href) and candidate_matches(title, queries, identity.get("year", "")):
            candidates.append((urllib.parse.urljoin(session.base + "/", href), title))
    errors = []
    for detail, title in candidates[:3]:
        try:
            links = PageLinks(session.page(detail)).links
            for language in ("download_zh-CN", "download_zh-TW"):
                for attrs, _ in links:
                    href = attrs.get("href", "")
                    if attrs.get("id") != language or not re.fullmatch(r"/subs/\d+/[^?#]+\.srt", href):
                        continue
                    url = session.base + urllib.parse.quote(urllib.parse.unquote(href), safe="/.-_")
                    choices = downloaded_choices(session.get(url), "SubtitleCat", title, meta, machine=True)
                    if choices:
                        return choices
        except (ToolError, OSError) as exc:
            errors.append(str(exc))
    if candidates:
        raise ToolError("SubtitleCat 有候选，但没有可直接下载的完整中文字幕。" + "；".join(dict.fromkeys(errors)))
    return []


def fetch_subtitle_backups(video, identity, meta, progress, warnings):
    sources = [("迅雷", lambda: title_subtitles(video, identity, meta, progress)),
               ("SubHD", lambda: subhd_subtitles(video, identity, meta, progress)),
               ("射手指纹", lambda: online_subtitles(video)),
               ("SubtitleCat（可能机翻）", lambda: subtitlecat_subtitles(video, identity, meta, progress))]
    for label, fetch in sources:
        if video is None and label == "射手指纹":
            continue
        progress("正在查询字幕来源：" + label)
        try:
            choices = fetch()
        except (ToolError, OSError) as exc:
            warnings.append(label + "：" + str(exc))
            continue
        if choices:
            if label.startswith("SubtitleCat"):
                warnings.append("SubtitleCat 是机翻可能性较高的备用字幕，请检查翻译质量与时间轴。")
            return choices
        warnings.append(label + "：本次未找到匹配的可用字幕。")
    return []


def discover_subtitles(video, meta, progress, identity=None, folder=None):
    choices = []
    warnings = []
    folder = folder or local_workspace()
    try:
        sidecars = sidecar_choices(video) if video is not None else []
    except OSError as exc:
        sidecars = []
        warnings.append("影片目录暂不可读取，继续查询在线字幕：" + str(exc))
    for choice in sidecars:
        try:
            choices.append(cache_subtitle(choice, folder))
        except (ToolError, OSError) as exc:
            warnings.append("外挂字幕读取失败：" + str(exc))
    # 不再因为有内封中文而跳过在线查询；默认合成不扫描远程视频。
    if not choices or max(c.score for c in choices) < 90:
        online = fetch_subtitle_backups(video, identity or identify_movie(video), meta, progress, warnings)
        for choice in online:
            try:
                choices.append(cache_subtitle(choice, folder))
            except (ToolError, OSError) as exc:
                warnings.append("本地缓存字幕失败：" + str(exc))
        if online:
            warnings.append("在线字幕已缓存到本机；请核对发行版本和台词时间，按片名命中不保证时间轴一致。")
    choices.sort(key=lambda c: -c.score)
    if not choices:
        warnings.append("没有找到可用外挂/在线文字字幕。可补选 SRT/ASS；如要读取内封文字轨，请手动点“使用内封”。")
    return choices, warnings


def search_movies(title, year=""):
    queries = [title]
    # 发布文件常同时包含中英文片名，完整组合无结果时再查中文标题。
    bilingual = re.match(r"^([\u4e00-\u9fff][^A-Za-z]*?)\s+[A-Za-z]", title)
    if bilingual:
        queries.append(bilingual.group(1).strip(" ._-"))
    # 英文片名先找中文别名，有助于匹配国内平台；失败继续原片名。
    if not re.search(r"[\u4e00-\u9fff]", title):
        try:
            suggestions = web_json("https://movie.douban.com/j/subject_suggest?" + urllib.parse.urlencode({"q": title}))
            if isinstance(suggestions, list):
                match = next((r for r in suggestions if str(r.get("year", "")) == str(year)), suggestions[0] if suggestions else None)
                if match and match.get("title"):
                    queries.insert(0, match["title"])
        except ToolError:
            pass
    results = []
    for query in dict.fromkeys(queries):
        data = web_json(KAN_SEARCH + "?" + urllib.parse.urlencode({"force_v": 1, "kw": query, "pageno": 1, "v_ap": 1, "tab": "all"}))
        body = (data.get("data") or {}) if isinstance(data, dict) else {}
        rows = (body.get("longData") or {}).get("rows") or []
        for row in rows:
            if str(row.get("cat_id")) != "1" or not row.get("playlinks"):
                continue
            found = html.unescape(re.sub(r"<[^>]+>", "", row.get("titleTxt") or row.get("title", "")))
            a, b = normalize_title(found), normalize_title(query)
            # Some providers label reviews/trailers as movies and give them the
            # feature's exact alias. Do not let that alias bypass the title label.
            extra = r"(?:(?:电影|影片|影视)(?:鉴赏|赏析|解说|影评|预告片?)|影评|预告片|幕后花絮|剧情解析)"
            if a != b and (re.match(extra + r"[\s:：·\-《〈【（(]", found) or
                           re.search(r"[\s:：·\-》〉】）)]" + extra + r"$", found)):
                continue
            aliases = [normalize_title(x) for x in re.split(r"[/|;]", row.get("titlealias", "")) if x]
            similarity = max([difflib.SequenceMatcher(None, a, b).ratio()] + [difflib.SequenceMatcher(None, alias, b).ratio() for alias in aliases])
            if similarity < .58:
                continue
            score = similarity * 100
            found_year = str(row.get("year", ""))
            if year and found_year:
                score += 15 if year == found_year else -min(25, abs(int(year) - int(found_year)) * 8) if year.isdigit() and found_year.isdigit() else -15
            links = {k: v for k, v in row["playlinks"].items() if k in PLATFORMS and isinstance(v, str)}
            if links:
                results.append({"title": found, "year": found_year, "links": links, "score": score,
                                "duration": row.get("coverInfo", {}).get("duration", ""), "id": row.get("en_id", "")})
        if results:
            break
    return sorted(results, key=lambda r: -r["score"])[:8]


def decode_danmubox_index(raw):
    """Decode the public site's AES-256-ECB catalog using Windows CNG.

    The public viewer's Utf8.parse takes characters 12:44 of its published key.
    This format key is shared by all visitors; it is not an account credential.
    """
    if os.name != "nt":
        raise ToolError("弹幕盒子目录解码目前需要 Windows。")
    import ctypes
    from ctypes import wintypes
    if len(raw) > 2 * 1024 * 1024:
        raise ToolError("弹幕盒子目录大小超限。")
    try:
        encrypted = base64.b64decode(raw.strip(), validate=True)
    except ValueError:
        raise ToolError("弹幕盒子目录编码无效。") from None
    if not encrypted or len(encrypted) % 16:
        raise ToolError("弹幕盒子目录长度无效。")
    key_bytes = b"jQoRm5OREM7S3qy3ZBgFV8EqPAP6jsZJ"
    cng = ctypes.WinDLL("bcrypt")
    pointer, ulong = ctypes.c_void_p, wintypes.ULONG
    signatures = {
        "BCryptOpenAlgorithmProvider": [ctypes.POINTER(pointer), wintypes.LPCWSTR, wintypes.LPCWSTR, ulong],
        "BCryptSetProperty": [pointer, wintypes.LPCWSTR, pointer, ulong, ulong],
        "BCryptGenerateSymmetricKey": [pointer, ctypes.POINTER(pointer), pointer, ulong, pointer, ulong, ulong],
        "BCryptDecrypt": [pointer, pointer, ulong, pointer, pointer, ulong, pointer, ulong, ctypes.POINTER(ulong), ulong],
        "BCryptDestroyKey": [pointer], "BCryptCloseAlgorithmProvider": [pointer, ulong],
    }
    for name, signature in signatures.items():
        getattr(cng, name).argtypes = signature
        getattr(cng, name).restype = wintypes.LONG
    def check(code):
        if code:
            raise ToolError("弹幕盒子目录解码失败，目录格式可能已更新。")
    algorithm, key = pointer(), pointer()
    try:
        check(cng.BCryptOpenAlgorithmProvider(ctypes.byref(algorithm), "AES", None, 0))
        mode = ctypes.create_unicode_buffer("ChainingModeECB")
        check(cng.BCryptSetProperty(algorithm, "ChainingMode", mode, ctypes.sizeof(mode), 0))
        secret = ctypes.create_string_buffer(key_bytes)
        check(cng.BCryptGenerateSymmetricKey(algorithm, ctypes.byref(key), None, 0, secret, len(key_bytes), 0))
        incoming, outgoing = ctypes.create_string_buffer(encrypted), ctypes.create_string_buffer(len(encrypted))
        length = ulong()
        check(cng.BCryptDecrypt(key, incoming, len(encrypted), None, None, 0, outgoing, len(encrypted), ctypes.byref(length), 1))
        return outgoing.raw[:length.value].decode("utf-8")
    except UnicodeError:
        raise ToolError("弹幕盒子目录文本无效。") from None
    finally:
        if key:
            cng.BCryptDestroyKey(key)
        if algorithm:
            cng.BCryptCloseAlgorithmProvider(algorithm, 0)


def valid_danmubox_entry(entry):
    return (isinstance(entry, dict) and isinstance(entry.get("name"), str) and 0 < len(entry["name"]) <= 500
            and isinstance(entry.get("repo"), str) and re.fullmatch(r"repo\d{3}", entry["repo"])
            and isinstance(entry.get("file"), str) and re.fullmatch(r"\d{2}/[a-f0-9]{32}\.7z", entry["file"])
            and isinstance(entry.get("size"), int) and 0 < entry["size"] <= MAX_BYTES)


def parse_danmubox_index(text, repo):
    entries = []
    for row in text.split(";"):
        fields = row.split(",")
        if len(fields) != 4 or not fields[1].isdigit():
            continue
        entry = dict(name=fields[0], size=int(fields[1]), repo=repo, file=fields[3] + ".7z")
        if valid_danmubox_entry(entry):
            entries.append(entry)
    if not entries:
        raise ToolError("弹幕盒子目录中没有可识别的归档记录。")
    return entries


def danmubox_catalog(progress=None):
    cache = filter_rules_path().parent / "cache" / "danmubox" / "catalog.json"
    with DANMUBOX_LOCK:
        previous = []
        try:
            previous = json.loads(read_text(cache))
            if not isinstance(previous, list) or not previous or not all(valid_danmubox_entry(row) for row in previous):
                previous = []
            if previous and 0 <= time.time() - cache.stat().st_mtime < 24 * 3600:
                return previous
        except (OSError, ToolError, ValueError):
            pass
        def request(name):
            for host in ("https://dmrepository.github.io/list/", "https://dmrepository-list.vercel.app/"):
                try:
                    return web_bytes(host + name, timeout=15)
                except ToolError:
                    continue
            raise ToolError("弹幕盒子目录暂时无法连接。")
        try:
            report(progress, "查询弹幕盒子历史归档目录")
            repositories = request("index").decode("utf-8").strip().split(",")
            if not 1 <= len(repositories) <= 32 or not all(re.fullmatch(r"repo\d{3}", repo) for repo in repositories):
                raise ToolError("弹幕盒子目录列表格式无效。")
            def fetch(repo):
                return parse_danmubox_index(decode_danmubox_index(request(repo)), repo)
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                entries = [entry for batch in pool.map(fetch, repositories) for entry in batch]
        except (ToolError, ValueError) as exc:
            if previous:
                report(progress, "弹幕盒子目录刷新失败，继续使用本机旧目录")
                return previous
            if not isinstance(exc, ToolError):
                raise ToolError("弹幕盒子目录格式无效，暂时无法查询历史归档。") from None
            raise
        cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, cache)
        finally:
            temporary.unlink(missing_ok=True)
        return entries


def danmubox_matches(name, title, year=""):
    # Bracketed labels describe archive editions; a sequel number is not an edition.
    cleaned = re.sub(r"\[[^\]]*\]|【[^】]*】|\([^)]*\)|（[^）]*）", " ", name)
    found = normalize_title(cleaned)
    for query in subtitle_queries(None, {"title": title}):
        query = normalize_title(query)
        if not query:
            continue
        start = found.find(query)
        if start < 0:
            continue
        before, after = found[:start], found[start + len(query):]
        # A Chinese/English alias may border the other script, but not another
        # word in the same script or a sequel number.
        prefix = r"[a-z0-9]$" if re.match(r"[a-z]", query) else r"[\u4e00-\u9fff0-9]$"
        suffix = r"[a-z0-9]" if re.search(r"[a-z]$", query) else r"[\u4e00-\u9fff0-9]"
        if before and re.search(prefix, before):
            continue
        if after and re.match(suffix, after):
            if not (year and after == year):
                continue
        # Exclude the title itself so numeric titles such as 2012 are not years.
        without_title = re.sub(r"[\W_]*".join(map(re.escape, query)), "", name, count=1, flags=re.I)
        years = re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", without_title)
        if year and years and year not in years:
            continue
        return True
    return False


def search_danmubox(title, year="", progress=None):
    rows = [row for row in danmubox_catalog(progress) if danmubox_matches(row["name"], title, year)]
    unique = {(row["repo"], row["file"]): row for row in rows}
    # Keep different archive editions separate; size only orders candidates, not counts.
    return sorted(unique.values(), key=lambda row: (-row["size"], row["name"]))[:8]


def danmubox_source_id(entry):
    return "danmubox:" + entry["repo"] + "/" + entry["file"]


def fetch_danmubox(entry, progress=None):
    if not valid_danmubox_entry(entry):
        raise ToolError("弹幕盒子归档地址无效。")
    relative = entry["repo"] + "/" + entry["file"]
    url = "https://cdn.jsdelivr.net/gh/dmrepository/" + relative
    errors = []
    for target in (url, "https://raw.githubusercontent.com/dmrepository/" + entry["repo"] + "/master/" + entry["file"]):
        try:
            raw = web_bytes(target, timeout=25, progress=progress, message="下载历史归档：" + entry["name"])
            if not raw.startswith((b"7z\xbc\xaf\x27\x1c", b"PK")):
                raise ToolError("弹幕盒子未返回有效压缩包。")
            break
        except ToolError as exc:
            errors.append(str(exc))
    else:
        raise ToolError("弹幕盒子归档下载失败：" + "；".join(errors))
    report(progress, "解包并核对历史弹幕：" + entry["name"])
    try:
        members = list(subtitle_archive_members(raw, extensions=(".xml", ".json")))
    except ToolError as exc:
        raise ToolError(str(exc).replace("字幕", "弹幕")) from None
    if len(members) != 1:
        raise ToolError("此归档含多份文件或没有 XML/JSON，请到弹幕盒子网页下载后分别补选。")
    comments, _ = parse_comments(decode_subtitle(members[0][1]))
    comments = [comment for comment in comments if not public_danmaku_notice(comment.text)]
    if not comments:
        raise ToolError("此归档没有可用弹幕。")
    return comments, "弹幕盒子（历史归档） · " + entry["name"] + " · 请核对版本和时间轴", url


def platform_name(platform):
    return PLATFORM_NAMES.get(platform.split(":", 1)[0], platform)


def movie_source_links(movie):
    links = {key: movie["links"][key] for key in PLATFORM_NAMES if key in movie["links"]}
    links.update({danmubox_source_id(entry): entry["repo"] + "/" + entry["file"]
                  for entry in movie.get("danmubox", []) if valid_danmubox_entry(entry)})
    return links


def canonical_platform_url(url):
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not any(host == h or host.endswith("." + h) for h in HOSTS):
        raise ToolError("未识别的影片平台链接。")
    # 优酷 video?vid= 必须保留 vid；去掉广告跟踪参数。
    query = urllib.parse.urlencode({k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items() if k in {"vid", "id", "cid", "bvid", "p"}})
    return urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path, query, ""))


def public_danmaku_notice(message):
    """Recognize notices inserted by public providers, not viewer comments."""
    return bool(re.search(r"有\s*\d+\s*条弹幕列队来袭", message) or re.fullmatch(
        r"\s*有\s*\d+\s*条弹幕正在赶来[，,]\s*请遵守弹幕礼仪[，,]\s*祝您观影愉快[~～!！。\s]*", message))


def parse_public_comments(data):
    if not isinstance(data, dict) or data.get("code") != 23:
        raise ToolError("公开弹幕源未返回有效弹幕。")
    rows = data.get("danmuku", [])
    comments = []
    for row in rows:
        try:
            if not isinstance(row, list) or len(row) < 5:
                continue
            message = str(row[4])
            if public_danmaku_notice(message):
                continue
            color = str(row[2] or "#ffffff").lstrip("#")
            if len(color) == 3:
                color = "".join(c * 2 for c in color)
            color_int = int(color, 16) if re.fullmatch(r"[0-9a-fA-F]{6}", color) else 16777215
            at = finite(row[0])
            mode = {"right": 1, "scroll": 1, "top": 5, "bottom": 4, "left": 6,
                    "1": 1, "4": 4, "5": 5, "6": 6}.get(str(row[1]).lower())
            if at >= 0 and message.strip() and mode is not None:
                comments.append(Comment(at, message, color_int, mode))
        except (ValueError, TypeError, ToolError):
            continue
    if not comments:
        raise ToolError("该片源暂无可用弹幕。")
    return comments


def bili_fields(raw):
    """Read the small protobuf subset used by Bilibili, rejecting truncated data."""
    offset = 0

    def varint():
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(raw):
                raise ToolError("B 站分段数据不完整。")
            byte = raw[offset]
            offset += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ToolError("B 站分段整数格式错误。")

    while offset < len(raw):
        tag = varint()
        number, wire = tag >> 3, tag & 7
        if not number:
            raise ToolError("B 站分段字段格式错误。")
        if wire == 0:
            value = varint()
        elif wire in (1, 2, 5):
            size = varint() if wire == 2 else (8 if wire == 1 else 4)
            if size > len(raw) - offset:
                raise ToolError("B 站分段数据不完整。")
            value = raw[offset:offset + size]
            offset += size
        else:
            raise ToolError("B 站分段字段类型不支持。")
        yield number, wire, value


def bili_xml_rows(raw):
    # comment.bilibili.com can return raw deflate despite Accept-Encoding: identity.
    if not raw.lstrip().startswith((b"<", b"\xef\xbb\xbf")):
        for window in (-15, 15, 31):
            try:
                decoder = zlib.decompressobj(window)
                decoded = decoder.decompress(raw, MAX_BYTES + 1)
                if len(decoded) > MAX_BYTES or decoder.unconsumed_tail:
                    raise ToolError("B 站 XML 解压超过 32 MB。")
                if decoder.eof:
                    raw = decoded
                    break
            except zlib.error:
                continue
    root = ET.fromstring(raw)
    rows = []
    for node in root.iter("d"):
        parts = node.attrib.get("p", "").split(",")
        try:
            rows.append((parts[7], Comment(finite(parts[0]), "".join(node.itertext()),
                                           int(parts[3]) & 0xFFFFFF, int(parts[1]))))
        except (ValueError, IndexError, ToolError):
            continue
    return rows


def bili_segment_rows(raw):
    rows = []
    for number, wire, value in bili_fields(raw):
        if number != 1 or wire != 2:
            continue
        fields = {n: v for n, w, v in bili_fields(value)}
        identity = fields.get(12, b"").decode("utf-8") or str(fields.get(1, 0))
        rows.append((identity, Comment(fields.get(2, 0) / 1000,
                                      fields.get(7, b"").decode("utf-8"),
                                      fields.get(5, 16777215) & 0xFFFFFF, fields.get(3, 1))))
    return rows


def fetch_bilibili_danmaku(url, progress=None):
    """Merge legacy XML and all six-minute segments by comment ID."""
    try:
        parsed = urllib.parse.urlsplit(url)
        episode = re.search(r"/ep(\d+)", parsed.path)
        video = re.search(r"/(BV[0-9A-Za-z]+|av\d+)", parsed.path)
        if episode:
            ep = int(episode[1])
            data = web_json(f"https://api.bilibili.com/pgc/view/web/season?ep_id={ep}", progress=progress)
            entries = data.get("result", {}).get("episodes", [])
            entry = next((item for item in entries if item.get("id") == ep), None)
            if data.get("code", 0) != 0 or entry is None:
                raise ToolError("B 站未返回所选剧集 CID。")
            cid, aid = int(entry["cid"]), int(entry["aid"])
        elif video:
            identity = video[1]
            query = "bvid=" + identity if identity.startswith("BV") else "aid=" + identity[2:]
            data = web_json("https://api.bilibili.com/x/web-interface/view?" + query, progress=progress)
            if data.get("code", 0) != 0:
                raise ToolError("B 站视频信息查询失败。")
            page = int(urllib.parse.parse_qs(parsed.query).get("p", ["1"])[0])
            entry = data["data"]
            selected = next((item for item in entry["pages"] if item["page"] == page), None)
            if selected is None:
                raise ToolError("B 站没有所选分 P。")
            cid, aid = int(selected["cid"]), int(entry["aid"])
        else:
            raise ToolError("B 站直连暂不支持此链接。")
        if cid <= 0 or aid <= 0:
            raise ToolError("B 站返回了无效 CID。")
        headers = {"Referer": url}
        rows, errors = [], []
        try:
            raw = web_bytes(f"https://comment.bilibili.com/{cid}.xml", request_headers=headers,
                            progress=progress, message="下载 B 站 XML 弹幕")
            rows.extend(bili_xml_rows(raw))
        except (ToolError, ET.ParseError, UnicodeError, ValueError) as exc:
            errors.append("XML：" + str(exc))
        try:
            raw = web_bytes(f"https://api.bilibili.com/x/v2/dm/web/view?type=1&oid={cid}&pid={aid}",
                            request_headers=headers, progress=progress, message="查询 B 站弹幕分段")
            config = next((dict((n, v) for n, w, v in bili_fields(value))
                           for number, wire, value in bili_fields(raw) if number == 4 and wire == 2), {})
            count = config.get(2, 0)
            if not isinstance(count, int) or not 1 <= count <= 100:
                raise ToolError("B 站弹幕分段数量无效。")

            def fetch_segment(index):
                try:
                    report(progress, f"下载 B 站分段 {index}/{count}")
                    raw = web_bytes(f"https://api.bilibili.com/x/v2/dm/web/seg.so?type=1&oid={cid}&segment_index={index}",
                                    request_headers=headers, timeout=20)
                    return bili_segment_rows(raw), ""
                except (ToolError, UnicodeError, ValueError, TypeError, AttributeError) as exc:
                    return [], f"分段 {index}：{exc}"

            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                for index, (items, error) in enumerate(pool.map(fetch_segment, range(1, count + 1)), 1):
                    rows.extend(items)
                    if error:
                        errors.append(error)
                    report(progress, "下载 B 站分段弹幕", index, count, "段")
        except (ToolError, ValueError, TypeError) as exc:
            errors.append("分段：" + str(exc))
        merged = {}
        for identity, comment in rows:
            if comment.mode not in (1, 2, 3, 4, 5, 6) or comment.time < 0 or not comment.text.strip():
                continue
            key = ("id", identity) if identity and identity != "0" else ("content", comment.time, comment.text, comment.color, comment.mode)
            merged[key] = comment
        if not merged:
            raise ToolError("B 站直连没有可用弹幕。" + "；".join(errors))
        source = "哔哩哔哩 · 直连 XML＋分段"
        if errors:
            source += "（部分获取，" + "；".join(errors) + "）"
        return sorted(merged.values(), key=lambda c: c.time), source, url
    except (KeyError, ValueError, TypeError, AttributeError, UnicodeError) as exc:
        raise ToolError("B 站接口数据格式异常：" + str(exc)) from None


def fetch_public_danmaku(movie, progress, platform=None):
    errors = []
    if platform is not None and platform.startswith("danmubox:"):
        entry = next((row for row in movie.get("danmubox", []) if valid_danmubox_entry(row) and danmubox_source_id(row) == platform), None)
        if entry is None:
            raise ToolError("所选历史归档不属于当前电影，请重新查询。")
        return fetch_danmubox(entry, progress)
    if platform is not None and ((platform not in PLATFORMS and platform != "dandanplay") or platform not in movie["links"]):
        raise ToolError("当前电影没有所选平台的链接，请选择列表中的其他来源。")
    if platform == "dandanplay" or (platform is None and "dandanplay" in movie["links"]):
        url = movie["links"]["dandanplay"]
        if not re.fullmatch(r"https://api\.dandanplay\.net/api/v2/comment/[1-9]\d*", url):
            raise ToolError("弹弹play节目编号无效。")
        path = urllib.parse.urlsplit(url).path + "?withRelated=true&chConvert=0"
        data = dandan_request(path, progress=progress)
        if data.get("comments") == []:
            raise ToolError("弹弹play此节目暂时没有弹幕，可切换其他来源。")
        comments, _ = parse_comments(json.dumps(data, ensure_ascii=False))
        shift = finite(movie.get("official_shift", 0))
        if shift:
            comments = [dataclass_replace(comment, time=comment.time + shift) for comment in comments]
        source = "弹弹play开放弹幕网络 · 官方＋关联弹幕"
        if shift:
            source += f" · 已应用匹配偏移 {shift:+g} 秒"
        return comments, source, url
    # 最多两种平台，失败信息可见；不会对所有来源无限重试。
    keys = [platform] if platform else [k for k in PLATFORMS if k in movie["links"]][:2]
    for key in keys:
        if key not in movie["links"]:
            continue
        url = canonical_platform_url(movie["links"][key])
        fallback_note = ""
        if key == "bilibili1":
            try:
                return fetch_bilibili_danmaku(url, progress)
            except ToolError as exc:
                errors.append(str(exc))
                report(progress, f"B 站直连失败，尝试公共弹幕库：{exc}")
                fallback_note = "（部分获取：第三方缓存，完整性未验证）"
        try:
            data = web_json(PUBLIC_DANMAKU + "?" + urllib.parse.urlencode({"ac": "dm", "url": url}),
                            progress=progress, message=f"下载{PLATFORMS[key]}弹幕（主源）")
            report(progress, "解析弹幕")
            comments = parse_public_comments(data)
            return comments, PLATFORMS[key] + " · 公益弹幕库" + fallback_note, url
        except ToolError as exc:
            errors.append(str(exc))
        try:
            raw = web_bytes(PUBLIC_DANMAKU_BACKUP + "?" + urllib.parse.urlencode({"type": "xml", "id": url}),
                            progress=progress, message=f"下载{PLATFORMS[key]}弹幕（备用源）")
            report(progress, "解析弹幕")
            comments, _ = parse_comments(raw.decode("utf-8-sig"))
            comments = [comment for comment in comments if not public_danmaku_notice(comment.text)]
            if not comments:
                raise ToolError("备用弹幕源仅返回了系统提示，没有可用评论。")
            return comments, PLATFORMS[key] + " · 公共弹幕库备用" + fallback_note, url
        except (ToolError, UnicodeError) as exc:
            errors.append(str(exc))
    raise ToolError("未能取得这部影片的弹幕。" + ("；".join(dict.fromkeys(errors)) if errors else "没有受支持的平台链接。"))


@dataclass
class DanmakuSource:
    platform: str
    comments: list = field(default_factory=list)
    source: str = ""
    url: str = ""
    cache_path: str = ""
    error: str = ""
    edition: str = ""

    @property
    def available(self):
        return bool(self.comments) and not self.error

    @property
    def partial(self):
        return self.available and "部分获取" in self.source

    @property
    def label(self):
        if not self.available:
            return f"{platform_name(self.platform)}" + (f" · {self.edition}" if self.edition else "") + " · 未取得"
        suffix = "（第三方缓存，可重试）" if self.partial and "第三方缓存" in self.source else "（部分取得，可重试）" if self.partial else ""
        return f"{platform_name(self.platform)} · {len(self.comments):,} 条" + (f" · {self.edition}" if self.edition else "") + suffix


def movie_source_key(movie):
    # 标题/年份及实际链接一起隔离缓存，避免切换版本时串用弹幕。
    return (movie.get("title", ""), movie.get("year", ""),
            tuple(movie_source_links(movie).items()), movie.get("official_shift", 0))


def source_summary(options):
    if options is None:
        return "来源尚未查询，选中电影后自动核实。"
    labels = [option.label for option in options if not option.platform.startswith("danmubox:")]
    archives = [option for option in options if option.platform.startswith("danmubox:")]
    if archives:
        available = [row for row in archives if row.available]
        labels.append(f"弹幕盒子：{len(available)}/{len(archives)} 个归档可选" +
                      (f"，最多 {max(len(row.comments) for row in available):,} 条" if available else ""))
    return "；".join(labels) or "没有受支持的平台链接。"


def movie_choice_label(movie, catalog):
    options = catalog.get(movie_source_key(movie))
    available = [option.label for option in options or [] if option.available]
    found = " / ".join(available) if available else ("未取得弹幕" if options is not None else "来源待查询")
    return f"{movie['title']} · {movie.get('year', '')} · {movie.get('duration') or '时长未知'} · {found}"


def cached_comment_file(comments, folder, name="danmaku.json", progress=None):
    text = json.dumps([{"time": c.time, "text": c.text, "color": c.color, "mode": c.mode}
                       for c in comments], ensure_ascii=False)
    return save_new(Path(folder) / name, text, progress, "缓存弹幕到本机")


def discover_danmaku_sources(result, movie, progress=None, retry_failed=False, on_update=None):
    key = movie_source_key(movie)
    previous = result.source_catalog.get(key)
    if previous is not None and not retry_failed:
        return previous
    retained = {option.platform: option for option in previous or [] if option.available}
    keys = list(movie_source_links(movie))
    pending = [platform for platform in keys if platform not in retained or retained[platform].partial]
    if result.workspace is None:
        result.workspace = local_workspace()
    result.source_pending = list(pending)
    def fetch(platform):
        edition = next((row["name"] for row in movie.get("danmubox", []) if valid_danmubox_entry(row) and danmubox_source_id(row) == platform), "")
        try:
            def detail(update):
                message = update.message if isinstance(update, ProgressUpdate) else str(update)
                if progress:
                    progress(ProgressUpdate(f"{platform_name(platform)}：{message}"))
            comments, source, url = fetch_public_danmaku(movie, detail, platform=platform)
            if not comments:
                raise ToolError("该来源未返回可用弹幕。")
            cache_name = "danmubox-" + hashlib.sha256(platform.encode()).hexdigest()[:16] if edition else platform
            path = cached_comment_file(comments, result.workspace, f"danmaku-{cache_name}.json")
            return DanmakuSource(platform, comments, source, url, str(path), edition=edition)
        except (ToolError, OSError, ValueError) as exc:
            return DanmakuSource(platform, error=str(exc), edition=edition)
    def publish():
        result.source_catalog[key] = [retained[platform] for platform in keys if platform in retained]
        if on_update:
            on_update(result.source_catalog[key])
    # 同时最多查询三个平台；查询已结束（成功须已缓存）即计数，不混合多路下载字节。
    if pending:
        publish()
        report(progress, "核实各平台弹幕并缓存", 0, len(pending), "个来源")
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, len(pending))) as pool:
            futures = {pool.submit(fetch, platform): platform for platform in pending}
            for count, future in enumerate(concurrent.futures.as_completed(futures), 1):
                platform = futures[future]
                fresh = future.result()
                old = retained.get(platform)
                if old and old.available and (not fresh.available or fresh.partial):
                    detail = fresh.error or "接口仍未全部成功"
                    retained[platform] = dataclass_replace(old, source=old.source.split("；补取未完成", 1)[0] + "；补取未完成，保留原数据：" + detail)
                else:
                    retained[platform] = fresh
                result.source_pending.remove(platform)
                publish()
                report(progress, "核实各平台弹幕并缓存", count, len(pending), "个来源", complete=count == len(pending))
    options = [retained[platform] for platform in keys]
    result.source_catalog[key] = options
    return options


def select_danmaku_source(result, movie, platform=None):
    options = result.source_catalog.get(movie_source_key(movie), [])
    option = next((row for row in options if row.available and (platform is None or row.platform == platform)), None)
    if option is None:
        raise ToolError("这个来源尚未取得可用弹幕，请选择标有条数的来源，或点“重查来源”。")
    result.comments, result.dm_ass = option.comments, None
    result.danmaku_source, result.danmaku_url = option.source, option.url
    result.selected_movie_key, result.selected_platform = movie_source_key(movie), option.platform
    return option


def clear_selected_danmaku(result):
    result.comments, result.dm_ass = [], None
    result.danmaku_source, result.danmaku_url = "", ""
    result.selected_movie_key, result.selected_platform = None, ""


@dataclass
class ScanResult:
    video: Path | None
    identity: dict
    metadata: dict
    signature: tuple
    subtitles: list = field(default_factory=list)
    movies: list = field(default_factory=list)
    comments: list = field(default_factory=list)
    danmaku_source: str = ""
    danmaku_url: str = ""
    warnings: list = field(default_factory=list)
    dm_ass: Ass | None = None
    workspace: Path | None = None
    source_catalog: dict = field(default_factory=dict)
    selected_movie_key: tuple | None = None
    selected_platform: str = ""
    source_pending: list = field(default_factory=list)


def file_signature(video):
    info = Path(video).stat()
    return info.st_size, info.st_mtime_ns


def scan_movie(video, override="", progress=lambda _: None, on_update=None):
    warnings = []
    override = override.strip()
    if video:
        try:
            video = normalize_path(video)
            validate_video(video)
            signature = file_signature(video)
        except (ToolError, OSError) as exc:
            if not override:
                raise
            warnings.append("影片文件暂不可用，已改为只按片名搜索；可生成到本机，需写回时再选择影片。" + str(exc))
            video = None
    else:
        video = None
    if video is None:
        if not override:
            raise ToolError("请输入电影名称，例如：楚门的世界 1998。")
        title, year = filename_title(override)
        identity = {"title": title, "year": year, "source": "手动片名"}
        meta, signature = {}, ()
    else:
        progress("正在读取影片信息…")
        try:
            meta = inspect_video(video)
        except (ToolError, OSError) as exc:
            meta = {}
            warnings.append("影片信息探测未完成，继续按片名搜索外挂字幕；片长暂未知。" + str(exc))
        try:
            identity = identify_movie(video, override)
        except OSError:
            title, year = filename_title(video.stem)
            identity = {"title": override or title, "year": year, "source": "修正片名" if override else "文件名"}
        if override:
            title, year = filename_title(override)
            identity.update(title=title, year=year or identity["year"])
    result = ScanResult(video, identity, meta, signature, warnings=warnings, workspace=local_workspace())
    def publish():
        if on_update:
            on_update(result)
    publish()
    progress(f"识别片名：{identity['title']} {identity['year']}；正在查找字幕和电影弹幕…")
    report(progress, "查找字幕和电影来源", 0, 2, "项")
    def find_movies():
        official, notices = [], []
        try:
            if dandan_config()["enabled"]:
                if video is not None and not override:
                    try:
                        official = dandan_candidates(dandan_match(video, meta, progress), identity["title"], identity["year"])
                    except (ToolError, OSError) as exc:
                        notices.append("弹弹play文件匹配未完成，继续按片名查询：" + str(exc))
                if not official:
                    official = dandan_candidates(dandan_search(identity["title"], progress, identity["year"]),
                                                 identity["title"], identity["year"])
                if not official:
                    notices.append("弹弹play未找到片名相符的节目，已继续查询其他来源。")
                elif any(movie.get("official_confirm") for movie in official):
                    notices.append("弹弹play返回多个候选，请在电影列表中核对并选择具体电影或剧集。")
        except (ToolError, OSError) as exc:
            notices.append("弹弹play官方来源暂不可用：" + str(exc))
        try:
            fallback = search_movies(identity["title"], identity["year"])
        except (ToolError, OSError) as exc:
            fallback = []
            notices.append("其他电影来源未取得：" + str(exc))
        movies = ([movie for movie in official if not movie.get("official_confirm")] + fallback +
                  [movie for movie in official if movie.get("official_confirm")])
        requested = (identity["title"], identity["year"])
        # A short query may resolve to a full movie title on the other services.
        # Search those titles too, then bind each archive to its own movie/year.
        archive_queries = [requested] + [(movie.get("official_title", movie["title"]), movie.get("year", "")) for movie in movies]
        archive_results = {}
        for query in dict.fromkeys(archive_queries):
            try:
                archive_results[query] = search_danmubox(*query, progress=progress)
            except (ToolError, OSError) as exc:
                notices.append("弹幕盒子历史目录未取得：" + str(exc))
                break
        else:
            if not any(archive_results.values()):
                notices.append("弹幕盒子未找到匹配归档，可尝试其他片名，或从网页下载后补选。")
        archives = archive_results.get(requested, [])
        attached = False
        for index, movie in enumerate(movies):
            query = (movie.get("official_title", movie["title"]), movie.get("year", ""))
            candidates = archives + archive_results.get(query, [])
            unique = {(row["repo"], row["file"]): row for row in candidates if danmubox_matches(row["name"], *query)}
            matches = sorted(unique.values(), key=lambda row: (-row["size"], row["name"]))[:8]
            if matches:
                movies[index] = dict(movie, danmubox=matches)
                attached = True
        if archives and not attached:
            movies.append(dict(title=identity["title"], year=identity["year"], duration="", links={}, danmubox=archives))
        return movies, notices
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        sub_future = pool.submit(discover_subtitles, video, meta, lambda _: None, identity, result.workspace)
        movie_future = pool.submit(find_movies)
        for count, future in enumerate(concurrent.futures.as_completed([sub_future, movie_future]), 1):
            try:
                if future is sub_future:
                    result.subtitles, warnings = future.result()
                    result.warnings.extend(warnings)
                else:
                    result.movies, notices = future.result()
                    result.warnings.extend(notices)
            except (ToolError, OSError) as exc:
                label = "字幕识别失败：" if future is sub_future else "电影弹幕搜索失败："
                result.warnings.append(label + str(exc))
            report(progress, "查找字幕和电影来源", count, 2, "项", complete=count == 2)
            publish()
    if result.movies:
        chosen = result.movies[0]
        if chosen.get("official_confirm"):
            return result
        if identity["year"] and chosen["year"] and identity["year"] != chosen["year"]:
            result.warnings.append(f"请核对版本：文件标记 {identity['year']} 年，弹幕平台标记 {chosen['year']} 年，可能是重映/不同剪辑版。")
        try:
            length = finite(meta.get("format", {}).get("duration", 0))
            units = [float(x) for x in chosen["duration"].split(":")]
            online_length = sum(x * 60 ** i for i, x in enumerate(reversed(units)))
            if length and online_length and abs(length - online_length) > max(300, length * .1):
                result.warnings.append(f"片长差异较大：当前文件约 {length / 60:.1f} 分钟，平台标记 {chosen['duration']}，请确认是否同一版本。")
        except (ValueError, ToolError):
            pass
        def source_update(options):
            if any(option.available for option in options):
                select_danmaku_source(result, chosen)
            publish()
        options = discover_danmaku_sources(result, chosen, progress, on_update=source_update)
        result.warnings.extend(f"{platform_name(option.platform)}未取得弹幕：{option.error}" for option in options if not option.available)
        if any(option.available for option in options):
            select_danmaku_source(result, chosen)
    else:
        result.warnings.append("没有找到匹配的电影弹幕来源。可修正片名后重新识别，或补选弹幕文件。")
    return result


def attach_search_video(result, video, progress=lambda _: None):
    """Bind a file after a title search without downloading its cached results again."""
    video = normalize_path(video)
    validate_video(video)
    signature = file_signature(video)
    progress("正在读取所选影片信息，保留已查询的字幕和弹幕…")
    warnings = list(result.warnings)
    try:
        meta = inspect_video(video)
    except (ToolError, OSError) as exc:
        meta = {}
        warnings.append("影片信息探测未完成；请核对版本和时间轴。" + str(exc))
    return dataclass_replace(result, video=video, signature=signature, metadata=meta, warnings=warnings)


def cache_danmaku(result, progress=None):
    if result.workspace is None:
        result.workspace = local_workspace()
    if result.dm_ass is not None:
        save_new(result.workspace / "danmaku.ass", result.dm_ass.dumps(), progress, "缓存弹幕到本机")
    elif result.comments:
        cached_comment_file(result.comments, result.workspace, progress=progress)


def materialize_subtitle(choice, video, progress=None, duration=None):
    if choice.doc is not None:
        doc = copy.deepcopy(choice.doc)
    elif choice.kind == "file":
        doc = load_subtitle(choice.path)
    elif choice.kind == "embedded":
        doc = extract_subtitle(video, choice.index, progress=progress, duration=duration)
        # 用户手动提取一次后，调整弹幕再合成时直接复用。
        choice.doc = copy.deepcopy(doc)
    else:
        raise ToolError("所选字幕不可用。")
    shift_events(doc, choice.delay)
    return doc


def synthesis_filename(result):
    if result.video is not None:
        return f"弹幕版-{result.video.stem}.ass"
    movie = next((movie for movie in result.movies if movie_source_key(movie) == result.selected_movie_key), {})
    title = movie.get("official_title") or movie.get("title") or result.identity.get("title") or "电影"
    year = movie.get("year") or result.identity.get("year") or ""
    name = str(title) + ("." + str(year) if year else "")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:120].strip(" .") or "电影"
    return f"弹幕版-{name}.ass"


def synthesize(result, subtitle_index=0, offset=0, density=6, duration=DEFAULT_DANMAKU_DURATION, font_size=32, progress=lambda _: None,
               *, area=25, opacity=80, block_scroll=False, block_fixed=True, block_color=False,
               avoid_subtitles=True, deduplicate=True, block_noise=True, block_keywords="", filter_rules=None, output_dir=None):
    if not result.subtitles or not 0 <= subtitle_index < len(result.subtitles):
        raise ToolError("还没有可合成的文字字幕。")
    if not result.comments and result.dm_ass is None:
        raise ToolError("还没有取得弹幕，暂时无法合成。")
    destination = result.video.parent if result.video is not None else checked_output_directory(
        output_dir if output_dir is not None else desktop_directory())
    if result.workspace is None:
        result.workspace = local_workspace()
    choice = result.subtitles[subtitle_index]
    if choice.kind == "embedded" and choice.doc is None:
        if result.video is None:
            raise ToolError("内封字幕需要影片文件；可先补选 SRT/ASS，再生成到本机。")
        if file_signature(result.video) != result.signature:
            raise ToolError("影片在识别后发生了变化，请重新选择识别。")
    progress("正在本机准备原台词字幕…" if choice.kind != "embedded" or choice.doc is not None else "手动提取内封字幕（需要读取影片）…")
    length = finite(result.metadata.get("format", {}).get("duration", 0) or 0, "影片时长") if result.video is not None else 0
    base = materialize_subtitle(result.subtitles[subtitle_index], result.video, progress=progress, duration=length)
    save_new(result.workspace / "selected-subtitle.ass", base.dumps(), progress, "缓存所选字幕到本机")
    progress("正在排列弹幕并合并台词…")
    filter_stats = dict(noise=0, keywords=0)
    if result.dm_ass is not None:
        dm = copy.deepcopy(result.dm_ass)
        shift_events(dm, offset, is_danmaku=True)
        filtered = 0
    else:
        shift = finite(offset, "弹幕偏移")
        comments = [c for c in result.comments if not length or c.time + shift < length]
        dm, filtered = render_comments(comments, base.resolution, offset, density, duration, font_size, progress=progress,
                                       area=area, opacity=opacity, block_scroll=block_scroll, block_fixed=block_fixed,
                                       block_color=block_color, avoid_subtitles=avoid_subtitles, deduplicate=deduplicate,
                                       block_noise=block_noise, block_keywords=block_keywords, filter_stats=filter_stats,
                                       filter_rules=filter_rules)
        filtered += len(result.comments) - len(comments)
        filter_stats["time"] += len(result.comments) - len(comments)
    progress("合并台词和弹幕")
    final = merge_ass(base, dm)
    target = destination / synthesis_filename(result)
    output = save_new(result.workspace / target.name, final.dumps(), progress, "在本机保存合成字幕")
    value = {"output": str(output), "local_output": str(output), "video": str(result.video) if result.video is not None else "",
             "signature": result.signature, "target": str(target), "subtitle_lines": len(base.events),
             "danmaku_lines": len(dm.events), "filtered": filtered,
             "noise_filtered": filter_stats["noise"], "keyword_filtered": filter_stats["keywords"]}
    value["filter_stats"] = filter_stats
    value["raw_count"] = len(result.dm_ass.events) if result.dm_ass is not None else len(result.comments)
    if result.video is None:
        try:
            published = copy_to_video_dir(output, target, progress, message="保存到所选本机目录")
        except (OSError, ToolError) as exc:
            raise ToolError("保存到所选目录失败：" + str(exc) + "\n合成缓存仍保留在：" + str(output)) from exc
        value.update(output=str(published), local_output=str(published), saved=True,
                     local_only=True, target=str(published), write_error="")
        progress("本机合成完成；请核对电影版本和字幕时间轴。")
        return value
    return publish_cached(value, progress)


def publish_cached(value, progress=lambda _: None):
    value = dict(value, saved=False, write_error="")
    value["output"] = value["local_output"]
    try:
        remember_output(value)
    except (ToolError, OSError) as exc:
        # Do not risk losing the only recovery path when saving the record failed.
        value["write_error"] = str(exc)
        return value
    try:
        progress("本地合成已完成，检查 NAS 后写回…")
        if file_signature(value["video"]) != tuple(value["signature"]):
            raise ToolError("影片在识别后发生了变化，请重新识别；本地结果已保留，未写回。")
        output = copy_to_video_dir(Path(value["local_output"]), Path(value["target"]), progress)
    except (OSError, ToolError) as exc:
        value["write_error"] = str(exc)
        progress("本地结果已保留，NAS 写回未完成，可重试写回")
        return value
    value.update(output=str(output), saved=True)
    try:
        remember_output(value, remove=True)
    except (ToolError, OSError) as exc:
        value["recovery_warning"] = "文件已写回，清理待写回记录失败：" + str(exc)
    return value


def copy_to_video_dir(source, target, progress=None, *, message="写回影片原目录"):
    total = source.stat().st_size
    report(progress, message, 0, total, "字节")
    # 先上传 .part，完整关闭后才发布为 ASS；断线不会暴露半份字幕。
    temporary = target.with_name(".nas-danmaku-" + uuid.uuid4().hex + ".part")
    created = False
    try:
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            created = True
            copied = 0
            while chunk := incoming.read(65536):
                offset = 0
                while offset < len(chunk):
                    count = outgoing.write(chunk[offset:])
                    if not count:
                        raise OSError("目标目录写入中断。")
                    offset += count
                    copied += count
                    report(progress, message, copied, total, "字节")
            if copied != total:
                raise ToolError("本地合成文件大小发生变化，未发布到目标目录。")
        for version in range(1, 10000):
            candidate = target if version == 1 else target.with_name(f"{target.stem}-v{version}{target.suffix}")
            try:
                if os.name == "nt":
                    temporary.rename(candidate)  # Windows rename 不覆盖已有目标。
                else:
                    os.link(temporary, candidate)  # POSIX 原子发布且拒绝覆盖。
            except FileExistsError:
                continue
            report(progress, message, total, total, "字节", complete=True)
            return candidate
        raise ToolError("同名版本文件过多，请整理影片目录。")
    finally:
        if created:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # 断线残留 .part 不影响本机成果；播放器不会将其当作字幕。

# ---- 一个窗口：选择、展示、确认合成 ----

UI_COLORS = dict(workspace="#f0f3f2", surface="#ffffff", ink="#20332f", muted="#64756f",
                 line="#dce4e0", accent="#17745f", hover="#105c4c", soft="#e7f2ed")


def setup_ui_theme(root):
    """Use one palette for the main form and every native settings window."""
    c = UI_COLORS
    root.configure(background=c["workspace"])
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", font=("Microsoft YaHei UI", 10), background=c["surface"], foreground=c["ink"])
    style.configure("TFrame", background=c["surface"])
    style.configure("Workspace.TFrame", background=c["workspace"])
    style.configure("Panel.TFrame", background=c["surface"], bordercolor=c["line"], borderwidth=1, relief="solid")
    style.configure("TLabel", background=c["surface"], foreground=c["ink"])
    style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
    style.configure("Section.TLabel", font=("Microsoft YaHei UI", 10, "bold"), foreground=c["accent"])
    style.configure("Muted.TLabel", foreground=c["muted"])
    style.configure("Badge.TLabel", background=c["soft"], foreground=c["accent"], padding=(8, 3))
    style.configure("TButton", padding=(9, 3), borderwidth=1, bordercolor=c["line"], relief="flat",
                    background=c["surface"], foreground=c["ink"], focuscolor=c["accent"])
    style.map("TButton", background=[("disabled", "#f3f5f4"), ("active", c["soft"])],
              foreground=[("disabled", "#78847f")], bordercolor=[("focus", c["accent"])])
    style.configure("Accent.TButton", background=c["accent"], foreground="white", bordercolor=c["accent"],
                    font=("Microsoft YaHei UI", 10, "bold"), padding=(14, 4))
    style.map("Accent.TButton", background=[("disabled", "#dce6e1"), ("active", c["hover"])],
              foreground=[("disabled", "#6a7d74"), ("!disabled", "white")],
              bordercolor=[("disabled", "#dce6e1"), ("focus", c["hover"])])
    for name in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(name, padding=3, bordercolor=c["line"], lightcolor=c["surface"], darkcolor=c["line"],
                        fieldbackground=c["surface"], foreground=c["ink"], arrowcolor=c["muted"])
        style.map(name, bordercolor=[("focus", c["accent"])],
                  fieldbackground=[("disabled", "#f1f4f2"), ("readonly", c["surface"])],
                  foreground=[("disabled", "#78847f")], selectbackground=[("!focus", c["soft"])],
                  selectforeground=[("!focus", c["ink"])])
    style.configure("TLabelframe", bordercolor=c["line"], background=c["surface"])
    style.configure("TLabelframe.Label", foreground=c["accent"], font=("Microsoft YaHei UI", 10, "bold"))
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=c["surface"], padding=2, focuscolor=c["accent"])
        style.map(name, background=[("active", c["soft"])], foreground=[("disabled", "#78847f")])
    style.configure("Treeview", rowheight=29, fieldbackground=c["surface"], background=c["surface"], bordercolor=c["line"])
    style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"), background="#edf2ef", padding=6)
    style.map("Treeview", background=[("selected", c["soft"])], foreground=[("selected", c["accent"])])
    style.configure("Horizontal.TProgressbar", background=c["accent"], troughcolor=c["soft"], borderwidth=0, thickness=5)
    style.configure("TScrollbar", background="#c6d4cd", troughcolor=c["workspace"], bordercolor=c["workspace"], arrowsize=13)
    style.configure("TSeparator", background=c["line"])
    style.configure("Horizontal.TScale", background=c["surface"], troughcolor=c["soft"], bordercolor=c["line"])
    for option, value in (("background", c["surface"]), ("foreground", c["ink"]), ("insertBackground", c["ink"]),
                          ("selectBackground", c["soft"]), ("selectForeground", c["ink"]),
                          ("highlightBackground", c["line"]), ("highlightColor", c["accent"]),
                          ("highlightThickness", 1), ("borderWidth", 0)):
        root.option_add("*Text." + option, value)
    root.option_add("*TCombobox*Listbox.font", ("Microsoft YaHei UI", 10))
    root.option_add("*TCombobox*Listbox.selectBackground", c["soft"])
    root.option_add("*TCombobox*Listbox.selectForeground", c["ink"])


def ui_section(parent, title, *, expand=False):
    panel = ttk.Frame(parent, style="Panel.TFrame", padding=(10, 6))
    panel.pack(fill="both" if expand else "x", expand=expand, pady=(0, 6))
    ttk.Label(panel, text=title, style="Section.TLabel").pack(anchor="w", pady=(0, 4))
    return panel


def scrollable_window(window, width, height):
    """Keep actions outside a scrollable body and fit the current screen."""
    width = min(width, max(320, window.winfo_screenwidth() - 80))
    height = min(height, max(280, window.winfo_screenheight() - 120))
    window.geometry(f"{width}x{height}")
    window.minsize(min(width, 560), min(height, 380))
    footer = ttk.Frame(window, padding=(12, 6))
    footer.pack(side="bottom", fill="x")
    holder = ttk.Frame(window)
    holder.pack(fill="both", expand=True)
    canvas = tk.Canvas(holder, highlightthickness=0, background=UI_COLORS["surface"])
    vertical = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
    horizontal = ttk.Scrollbar(holder, orient="horizontal", command=canvas.xview)
    horizontal.pack(side="bottom", fill="x")
    vertical.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    body = ttk.Frame(canvas, padding=12)
    item = canvas.create_window(0, 0, window=body, anchor="nw")
    body.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda event: canvas.itemconfigure(item, width=max(event.width, body.winfo_reqwidth())))
    def wheel(event):
        if isinstance(event.widget, (tk.Text, ttk.Treeview, ttk.Combobox, ttk.Spinbox)):
            return
        canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
    window.bind("<MouseWheel>", wheel, add="+")
    window.bind("<Next>", lambda _: canvas.yview_scroll(1, "pages"), add="+")
    window.bind("<Prior>", lambda _: canvas.yview_scroll(-1, "pages"), add="+")
    return body, footer


def wrapped_label(parent, **kwargs):
    kwargs.setdefault("wraplength", 560)
    label = ttk.Label(parent, **kwargs)
    label.pack(anchor="w", fill="x")
    parent.bind("<Configure>", lambda event: label.configure(wraplength=max(180, event.width - 40)), add="+")
    return label


def main_window_layout(window):
    """Keep the form, log and actions on screen without an outer page scrollbar."""
    width = min(1280, max(640, window.winfo_screenwidth() - 80))
    height = min(920, max(480, window.winfo_screenheight() - 100))
    window.geometry(f"{width}x{height}")
    window.minsize(min(width, 1100), min(height, 760))
    footer = ttk.Frame(window, padding=(16, 10))
    footer.pack(side="bottom", fill="x")
    content = ttk.Frame(window, style="Workspace.TFrame")
    content.pack(fill="both", expand=True)
    content.columnconfigure(0, weight=7, minsize=640, uniform="main")
    content.columnconfigure(1, weight=3, minsize=300, uniform="main")
    content.rowconfigure(0, weight=1)
    form = ttk.Frame(content, style="Workspace.TFrame", padding=(16, 12, 8, 4))
    form.grid(row=0, column=0, sticky="nsew")
    sidebar = ttk.Frame(content, style="Workspace.TFrame")
    sidebar.grid(row=0, column=1, sticky="nsew", padx=(0, 16), pady=(12, 12))
    return form, footer, sidebar


def elapsed_text(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def progress_detail(update, elapsed):
    def amount(value):
        if update.unit == "字节":
            return f"{value / 1048576:.2f} MB" if value >= 1048576 else f"{value / 1024:.1f} KB"
        if update.unit == "秒":
            return elapsed_text(value)
        return f"{value:,.0f} {update.unit}"

    percent = update.percent
    if update.complete:
        detail = "本步完成 · 100%"
    elif percent is not None:
        detail = f"{percent:.1f}% · {amount(update.current)} / {amount(update.total)}"
        if update.current >= update.total:
            detail += " · 等待收尾确认"
    elif update.current is not None:
        detail = f"已{'提取字幕至' if update.unit == '秒' else '接收'} {amount(update.current)} · 总量未知"
    else:
        detail = "等待结果，暂无法计算百分比"
    if update.unit == "秒" and not update.complete and update.total:
        detail += "（字幕时间 / 片长）"
    return detail + " · 本步已用 " + elapsed_text(elapsed)


class FilterRulesDialog:
    def __init__(self, parent):
        self.parent = parent
        self.rules = rules_with_keywords(parent.rule_rows, compile_block_keywords(parent.variables["block_keywords"].get()))
        self.original_rules = copy.deepcopy(self.rules)
        self.selected = None
        self.current_part = None
        self.window = tk.Toplevel(parent.window)
        self.window.title("弹幕屏蔽规则 · 内置和自定义")
        frame, footer = scrollable_window(self.window, 860, 760)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        ttk.Label(frame, text="屏蔽规则", style="Title.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(frame, text="内置规则也能修改、停用或删除。普通关键词包含就屏蔽；正则用于匹配日期等写法规律。", wraplength=820).pack(anchor="w")
        self.tree = ttk.Treeview(frame, columns=("enabled", "source", "name", "kind"), show="headings", height=6, selectmode="browse")
        for key, label, width in (("enabled", "启用", 55), ("source", "来源", 65), ("name", "规则名称", 210), ("kind", "匹配方式", 330)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=45)
        self.tree.pack(fill="x", pady=8)
        self.tree.bind("<<TreeviewSelect>>", self.select_rule)
        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Button(row, text="新增关键词", command=lambda: self.add_rule("keyword")).pack(side="left")
        ttk.Button(row, text="新增正则", command=lambda: self.add_rule("search")).pack(side="left", padx=6)
        ttk.Button(row, text="删除选中", command=self.delete_rule).pack(side="left")
        ttk.Button(row, text="恢复全部默认", command=self.restore).pack(side="right")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=8)
        self.enabled = tk.BooleanVar()
        self.name = tk.StringVar()
        self.kind = tk.StringVar()
        ttk.Checkbutton(row, text="启用此规则", variable=self.enabled).pack(side="left")
        ttk.Entry(row, textvariable=self.name, width=25).pack(side="left", padx=6)
        self.kind_box = ttk.Combobox(row, textvariable=self.kind, state="readonly", width=31)
        self.kind_box.pack(side="left", fill="x", expand=True)
        self.kind_box.bind("<<ComboboxSelected>>", self.change_kind)
        ttk.Label(frame, text="用途说明（可修改）").pack(anchor="w")
        self.description = tk.Text(frame, height=2, wrap="word", font=("Microsoft YaHei UI", 10))
        self.description.pack(fill="x", pady=(2, 6))
        self.example, self.keep_example = tk.StringVar(), tk.StringVar()
        for label, variable in (("屏蔽示例", self.example), ("保留示例", self.keep_example)):
            row = ttk.Frame(frame)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=label, width=10).pack(side="left")
            ttk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(8, 2))
        ttk.Label(row, text="规则内容").pack(side="left")
        self.part = tk.StringVar()
        self.part_box = ttk.Combobox(row, textvariable=self.part, state="readonly", width=24)
        self.part_box.pack(side="left", padx=8)
        self.part_box.bind("<<ComboboxSelected>>", self.change_part)
        ttk.Label(row, text="组合规则请逐项查看下拉列表", foreground="#606975").pack(side="left")
        self.pattern = tk.Text(frame, height=4, wrap="word", font=("Consolas", 10))
        self.pattern.pack(fill="both", expand=True)
        ttk.Label(frame, text=r"正则小抄：\d 是数字；{4} 是重复四次；A|B 是 A 或 B。写普通词不用这些符号。" +
                  "\n匹配忽略大小写、空格和常见简繁差异；整句正则还会去标点，正则只检查不超过 180 字的弹幕。",
                  wraplength=820, foreground="#606975").pack(anchor="w", pady=6)
        row = ttk.Frame(frame)
        row.pack(fill="x")
        self.sample = tk.StringVar()
        ttk.Entry(row, textvariable=self.sample).pack(side="left", fill="x", expand=True)
        self.test_button = ttk.Button(row, text="测试这句弹幕", command=self.test_sample)
        self.test_button.pack(side="left", padx=(6, 0))
        self.notice = tk.StringVar(value="测试按当前总开关和各条开关执行，显示命中的具体规则。示例只是说明，修改后请测试核对。")
        ttk.Label(frame, textvariable=self.notice, wraplength=820).pack(anchor="w", pady=8)
        row = footer
        ttk.Label(row, text="保存即生效。", foreground="#606975").pack(side="left")
        ttk.Button(row, text="保存并生效", style="Accent.TButton", command=self.save).pack(side="right")
        ttk.Button(row, text="取消", command=self.close).pack(side="right", padx=6)
        self.refresh()

    def flush(self):
        if self.selected is None or self.selected >= len(self.rules):
            return
        item = self.rules[self.selected]
        if self.current_part:
            item["parts"][self.current_part] = self.pattern.get("1.0", "end-1c")
        item.update(name=self.name.get().strip(), enabled=self.enabled.get(), description=self.description.get("1.0", "end-1c"),
                    example=self.example.get(), keep_example=self.keep_example.get())
        if self.tree.exists(str(self.selected)):
            self.tree.item(str(self.selected), values=self.row_values(item))

    @staticmethod
    def row_values(item):
        return ("是" if item["enabled"] else "否", "内置" if item["builtin"] else "自定义", item["name"], RULE_TYPES[item["kind"]])

    def refresh(self, index=0):
        self.selected, self.current_part = None, None
        self.tree.delete(*self.tree.get_children())
        for i, item in enumerate(self.rules):
            self.tree.insert("", "end", iid=str(i), values=self.row_values(item))
        if self.rules:
            self.tree.selection_set(str(min(index, len(self.rules) - 1)))
            self.select_rule()
        else:
            self.name.set("")
            self.description.delete("1.0", "end")
            self.pattern.delete("1.0", "end")
            self.notice.set("列表已空：保存后不会有任何内置屏蔽；可新增规则或恢复默认。")

    def select_rule(self, _=None):
        selection = self.tree.selection()
        if not selection or int(selection[0]) == self.selected:
            return
        self.flush()
        self.selected = int(selection[0])
        item = self.rules[self.selected]
        self.enabled.set(item["enabled"])
        self.name.set(item["name"])
        simple = item["kind"] in ("keyword", "full", "search")
        self.kind_box.configure(values=[RULE_TYPES[k] for k in (("keyword", "full", "search") if simple else (item["kind"],))])
        self.kind.set(RULE_TYPES[item["kind"]])
        self.description.delete("1.0", "end")
        self.description.insert("1.0", item.get("description", ""))
        self.example.set(item.get("example", ""))
        self.keep_example.set(item.get("keep_example", ""))
        self.sample.set(item.get("example", ""))
        self.part_box.configure(values=[RULE_FIELD_LABELS[key] for key in RULE_FIELDS[item["kind"]]])
        self.current_part = None
        self.part_box.current(0)
        self.change_part()

    def change_part(self, _=None):
        if self.selected is None:
            return
        item = self.rules[self.selected]
        if self.current_part:
            item["parts"][self.current_part] = self.pattern.get("1.0", "end-1c")
        self.current_part = RULE_FIELDS[item["kind"]][self.part_box.current()]
        self.pattern.delete("1.0", "end")
        self.pattern.insert("1.0", item["parts"][self.current_part])

    def change_kind(self, _=None):
        if self.selected is None:
            return
        self.flush()
        self.rules[self.selected]["kind"] = next(key for key, label in RULE_TYPES.items() if label == self.kind.get())
        self.tree.item(str(self.selected), values=self.row_values(self.rules[self.selected]))

    def add_rule(self, kind):
        self.flush()
        self.rules.append(dict(id=uuid.uuid4().hex, name="新关键词" if kind == "keyword" else "新正则", kind=kind,
                               enabled=True, builtin=False, parts={"pattern": "示例关键词"},
                               description="请填写要屏蔽的词或匹配规律。", example="示例关键词", keep_example="正常剧情讨论"))
        self.refresh(len(self.rules) - 1)

    def delete_rule(self):
        if self.selected is not None:
            index = self.selected
            self.rules.pop(index)
            self.refresh(index)

    def restore(self):
        self.rules = default_filter_rules()
        self.refresh()

    def test_sample(self):
        self.flush()
        try:
            compile_filter_rules(self.rules)
        except ToolError as exc:
            self.notice.set(str(exc))
            return
        if not self.sample.get().strip():
            self.notice.set("先输入一句要测试的弹幕。")
            return
        rows, sample, enabled = copy.deepcopy(self.rules), self.sample.get(), self.parent.variables["block_noise"].get()
        results = queue.Queue()
        self.test_button.configure(state="disabled")
        self.notice.set("正在测试当前规则…")
        def work():
            try:
                matches = evaluate_filter_rules([sample], rows, enabled, all_matches=True, timeout=3)[0]
                results.put("会屏蔽，命中：" + "、".join(row["name"] for row in matches) if matches else "会保留：当前启用的规则均未命中。")
            except Exception as exc:
                results.put(str(exc))
        def done():
            try:
                self.notice.set(results.get_nowait())
                self.test_button.configure(state="normal")
            except queue.Empty:
                self.window.after(100, done)
        threading.Thread(target=work, daemon=True).start()
        self.window.after(100, done)

    def save(self):
        self.flush()
        try:
            if self.parent.app.busy:
                raise ToolError("当前任务正在处理，请结束后保存规则。")
            compile_filter_rules(self.rules)
            save_filter_rules(self.rules)
        except (ToolError, OSError) as exc:
            self.notice.set(str(exc))
            return
        self.parent.app.render_settings["filter_rules"] = copy.deepcopy(self.rules)
        self.parent.rule_rows = copy.deepcopy(self.rules)
        self.parent.variables["block_keywords"].set("")
        self.parent.keyword_button.configure(text=f"屏蔽规则 · {len(self.rules)} 条")
        self.window.destroy()

    def close(self):
        self.flush()
        if self.rules != self.original_rules:
            answer = messagebox.askyesnocancel("规则尚未保存", "保存修改并生效吗？选“否”丢弃本次修改。", parent=self.window)
            if answer is None:
                return
            if answer:
                return self.save()
        self.window.destroy()


class DanmakuSettingsDialog:
    def __init__(self, app):
        self.app = app
        self.window = tk.Toplevel(app.root)
        self.window.title("弹幕设置")
        body, footer = scrollable_window(self.window, 1120, 600)
        current = app.render_settings
        self.rule_rows = copy.deepcopy(current["filter_rules"] if current["filter_rules"] is not None else default_filter_rules())
        self.rules_dialog = None
        self.variables = {key: tk.BooleanVar(value=current[key]) for key in
                          ("block_scroll", "block_fixed", "block_color", "avoid_subtitles", "deduplicate", "block_noise")}
        self.variables["block_keywords"] = tk.StringVar(value=current["block_keywords"])
        self.variables.update({key: tk.DoubleVar(value=current[key]) for key in ("area", "opacity", "font_size")})
        self.variables["speed"] = tk.DoubleVar(value=DEFAULT_DANMAKU_DURATION * 100 / current["duration"])
        self.variables["density"] = tk.StringVar(value=app.density.get())
        self.labels = {}
        ttk.Label(body, text="弹幕设置", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="边调边看显示效果。应用后重新合成，电视加载新生成的字幕即可。", style="Muted.TLabel").pack(anchor="w", pady=(2, 16))
        columns = ttk.Frame(body)
        columns.pack(fill="both", expand=True)
        preview_panel = ttk.Frame(columns, padding=(0, 0, 20, 0))
        preview_panel.pack(side="left", fill="y")
        ttk.Label(preview_panel, text="画面预览", style="Section.TLabel").pack(anchor="w", pady=(0, 10))
        frame = ttk.Frame(columns)
        frame.pack(side="left", fill="both", expand=True)
        ttk.Label(frame, text="过滤与显示", style="Section.TLabel").pack(anchor="w", pady=(0, 8))
        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Label(row, text="屏蔽类型").pack(side="left", padx=(0, 10))
        for key, label in (("block_scroll", "滚动"), ("block_fixed", "固定"), ("block_color", "彩色")):
            ttk.Checkbutton(row, text=label, variable=self.variables[key], command=self.preview).pack(side="left", padx=8)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(6, 10))
        for key, label in (("avoid_subtitles", "防挡字幕（保留底部 32%）"), ("deduplicate", "过滤重复弹幕")):
            ttk.Checkbutton(row, text=label, variable=self.variables[key], command=self.preview).pack(side="left", padx=(0, 12))
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(0, 4))
        ttk.Checkbutton(row, text="过滤垃圾弹幕（打卡、报时、闲聊等）", variable=self.variables["block_noise"]).pack(side="left")
        self.keyword_button = ttk.Button(frame, text="屏蔽规则（内置＋自定义）…", command=self.edit_keywords)
        self.keyword_button.pack(anchor="w", pady=(0, 8))
        for key, label, low, high in (("area", "显示区域", 10, 100), ("opacity", "不透明度", 10, 100),
                                      ("font_size", "弹幕字号", 16, 64), ("speed", "弹幕速度", 50, 200)):
            row = ttk.Frame(frame)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=label, width=10).pack(side="left")
            ttk.Scale(row, from_=low, to=high, variable=self.variables[key], command=lambda _: self.preview()).pack(side="left", fill="x", expand=True, padx=10)
            self.labels[key] = tk.StringVar()
            ttk.Label(row, textvariable=self.labels[key], width=15).pack(side="left")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(6, 10))
        ttk.Label(row, text="同屏最多", width=10).pack(side="left")
        ttk.Spinbox(row, from_=1, to=30, width=5, textvariable=self.variables["density"], command=self.preview).pack(side="left", padx=10)
        ttk.Label(row, text="条；区域放不下时自动减少", foreground="#606975").pack(side="left")
        self.canvas = tk.Canvas(preview_panel, width=512, height=288, background="#111c29", highlightthickness=0)
        self.canvas.pack(pady=(0, 6))
        self.note = tk.StringVar()
        ttk.Label(preview_panel, textvariable=self.note, style="Muted.TLabel", wraplength=512).pack(anchor="w")
        row = footer
        ttk.Button(row, text="恢复默认", command=self.reset).pack(side="left")
        ttk.Button(row, text="应用设置", style="Accent.TButton", command=self.apply).pack(side="right")
        ttk.Button(row, text="取消", command=self.window.destroy).pack(side="right", padx=6)
        self.preview()

    def values(self):
        settings = {key: self.variables[key].get() for key in DM_DEFAULTS if key not in ("duration", "filter_rules")}
        settings["filter_rules"] = copy.deepcopy(self.rule_rows)
        for key in ("area", "opacity", "font_size"):
            settings[key] = round(settings[key])
        settings["duration"] = DEFAULT_DANMAKU_DURATION * 100 / finite(self.variables["speed"].get(), "速度")
        density = int(self.variables["density"].get())
        compile_block_keywords(settings["block_keywords"])
        compile_filter_rules(settings["filter_rules"])
        return settings, density

    def edit_keywords(self):
        if self.rules_dialog and self.rules_dialog.window.winfo_exists():
            return
        self.rules_dialog = FilterRulesDialog(self)

    def preview(self):
        if not hasattr(self, "canvas"):
            return
        try:
            settings, density = self.values()
            effective = min(settings["area"], 68 if settings["avoid_subtitles"] else 100)
            self.labels["area"].set(f"顶部 {settings['area']}%")
            self.labels["opacity"].set(f"{settings['opacity']}%")
            self.labels["font_size"].set(f"{settings['font_size']}（1080p）")
            self.labels["speed"].set(f"{DEFAULT_DANMAKU_DURATION / settings['duration']:.2g}× / {settings['duration']:.1f} 秒")
            canvas = self.canvas
            canvas.delete("all")
            canvas.create_rectangle(0, 0, 512, 288 * effective / 100, fill="#203e50", outline="")
            canvas.create_line(0, 288 * effective / 100, 512, 288 * effective / 100, fill="#41b6cf", dash=(4, 4))
            canvas.create_text(256, 170, text="电影画面", fill="#59697a", font=("Microsoft YaHei UI", 20))
            canvas.create_text(256, 265, text="原台词字幕 · 样式保持不变", fill="white", font=("Microsoft YaHei UI", -13))
            sample = [Comment(0, "上方滚动弹幕示意"), Comment(0, "弹幕字号与区域比例", 0x7ADDEF),
                      Comment(0, "顶部固定弹幕", mode=5), Comment(0, "底部固定弹幕（仍在显示区域内）", mode=4)]
            doc, _ = render_comments(sample, (1920, 1080), density=density,
                                     **dict(settings, block_noise=False, block_keywords="", filter_rules=[]))
            for number, row in enumerate(doc.events):
                position = re.search(r"\\(?:move|pos)\([^,]+,([\d.]+)", row["Text"])
                text_value = re.sub(r"\{[^}]*\}", "", row["Text"])
                color = re.search(r"\\c&H([0-9A-F]{6})&", row["Text"])[1]
                rgb = [int(color[i:i+2], 16) for i in (4, 2, 0)]
                bg = (32, 62, 80)
                blended = '#' + ''.join(f'{round(c * settings["opacity"] / 100 + b * (1 - settings["opacity"] / 100)):02x}' for c, b in zip(rgb, bg))
                fixed = r"\pos" in row["Text"]
                canvas.create_text(256 if fixed else 110 + number * 30, float(position[1]) * 288 / 1080,
                                   text=text_value, anchor="n" if fixed else "nw", fill=blended,
                                   font=("Microsoft YaHei UI", -max(1, round(settings["font_size"] * 288 / 1080))))
            extra = "；防挡字幕已将实际范围限制为顶部 68%" if settings["area"] > effective else ""
            self.note.set("16:9 比例示意；字号按画面高度缩放，电视字体可能略有差异" + extra + "。")
        except (ToolError, ValueError, tk.TclError, ZeroDivisionError) as exc:
            self.note.set("请检查设置：" + str(exc))

    def reset(self):
        for key, value in DM_DEFAULTS.items():
            if key not in ("duration", "filter_rules"):
                self.variables[key].set(value)
        self.rule_rows = default_filter_rules()
        self.variables["speed"].set(DEFAULT_DANMAKU_DURATION * 100 / DM_DEFAULTS["duration"])
        self.variables["density"].set("6")
        self.keyword_button.configure(text="屏蔽规则（内置＋自定义）…")
        self.preview()

    def apply(self):
        if self.app.busy:
            self.note.set("当前任务正在处理，请结束后再应用设置。")
            return
        if self.app.result and self.app.result.dm_ass:
            self.note.set("当前已导入排版好的 ASS，只支持偏移；请选择在线弹幕或 XML/JSON 后调整。")
            return
        try:
            settings, density = self.values()
            if settings["block_scroll"] and settings["block_fixed"]:
                raise ToolError("滚动和固定不能同时屏蔽，否则没有弹幕可显示。")
            samples = [Comment(0, "检查滚动"), Comment(0, "检查固定", mode=5)]
            render_comments(samples, (1920, 1080), density=density,
                            **dict(settings, block_noise=False, block_keywords="", filter_rules=[]))
            if settings["filter_rules"] != self.app.render_settings["filter_rules"]:
                save_filter_rules(settings["filter_rules"])
            store_preferences(settings, density, self.app.saved_offsets)
        except (ToolError, ValueError, OSError, tk.TclError, ZeroDivisionError) as exc:
            self.note.set("请检查设置：" + str(exc))
            return
        self.app.render_settings = settings
        self.app.preferences_readable = True
        self.app.density.set(str(density))
        self.app.settings_summary.set(self.app.settings_description())
        self.app.status.set("弹幕设置已应用；点击确认合成后生效，无需重新下载。")
        self.window.destroy()


class App:
    def __init__(self, root, initial=""):
        self.root = root
        root.title("字幕＋弹幕 · 一键合成 v" + VERSION)
        self.result = None
        self.pending_output = None
        self.last_output = None
        self.task_id = 0
        self.cancel_event = threading.Event()
        self.task_local = threading.local()
        self.cancellable = False
        self.busy = False
        self.tasks = queue.Queue()
        self.active_progress = None
        self.task_started = self.step_started = 0.0
        self.path = tk.StringVar(value=initial)
        self.title = tk.StringVar()
        self.identity_text = tk.StringVar(value="可直接输入片名搜索，例如：楚门的世界 1998")
        self.subtitle = tk.StringVar()
        self.movie = tk.StringVar()
        self.dm_text = tk.StringVar(value="等待选择影片或按片名搜索")
        self.status = tk.StringVar(value="选影片自动识别，或输入电影名称后按回车搜索")
        self.progress_text = tk.StringVar(value="进度按当前步骤计算；切换步骤时归零")
        self.offset = tk.StringVar(value="0")
        self.density = tk.StringVar(value="6")
        self.render_settings = dict(DM_DEFAULTS)
        self.saved_offsets = {}
        self.preferences_readable = True
        rule_warning = ""
        try:
            self.render_settings, density, self.saved_offsets = load_preferences()
            self.density.set(str(density))
        except (ToolError, OSError) as exc:
            rule_warning = str(exc)
            self.preferences_readable = False
        self.output_directory = desktop_directory()
        self.output_directory_readable = True
        try:
            self.output_directory = load_output_directory()
        except (ToolError, OSError) as exc:
            rule_warning += "\n" + str(exc)
            self.output_directory_readable = False
        try:
            recovered = pending_outputs()
            self.pending_output = recovered[-1] if recovered else None
            self.last_output = self.pending_output
        except (ToolError, OSError) as exc:
            rule_warning += "\n" + str(exc)
        try:
            self.render_settings["filter_rules"] = load_filter_rules()
        except (ToolError, OSError) as exc:
            self.render_settings["filter_rules"] = default_filter_rules()
            rule_warning += f"\n读取已保存的屏蔽规则失败，暂用默认规则；原文件未改动。{exc}"
        self.platform = tk.StringVar(value="来源待查询")
        self.platform_keys = []
        self.source_status = tk.StringVar(value="选中电影后自动查询各平台，只列出已取得弹幕的可选来源。")
        self.settings_dialog = None
        self.dandan_dialog = None
        self.dandan_summary = tk.StringVar()
        self.refresh_dandan_status()
        self.settings_summary = tk.StringVar(value=self.settings_description())
        self.output_text = tk.StringVar(value="本机保存目录：" + str(self.output_directory))
        self.result_summary = tk.StringVar()
        setup_ui_theme(root)
        head = ttk.Frame(root, padding=(16, 8))
        head.pack(side="top", fill="x")
        brand = ttk.Frame(head)
        brand.pack(side="left")
        ttk.Label(brand, text="字幕＋弹幕", style="Title.TLabel").pack(side="left")
        ttk.Label(brand, text="合成工作台", style="Muted.TLabel").pack(side="left", padx=(14, 0))
        ttk.Button(head, text="使用说明", command=self.help).pack(side="right")
        ttk.Button(head, text="网页找字幕", command=self.subtitle_sites).pack(side="right", padx=6)
        ttk.Button(head, textvariable=self.dandan_summary, command=self.open_dandan_settings).pack(side="right")
        ttk.Separator(root).pack(fill="x")
        p, footer, sidebar = main_window_layout(root)

        find = ui_section(p, "01  /  查找电影")
        row = ttk.Frame(find)
        row.pack(fill="x")
        ttk.Label(row, text="电影名称", width=9).pack(side="left")
        self.title_entry = ttk.Entry(row, textvariable=self.title)
        self.title_entry.pack(side="left", fill="x", expand=True)
        self.title_entry.bind("<Return>", lambda _: self.scan(override=True))
        self.retry_button = ttk.Button(row, text="按片名搜索", style="Accent.TButton", command=lambda: self.scan(override=True))
        self.retry_button.pack(side="left", padx=(8, 0))
        row = ttk.Frame(find)
        row.pack(fill="x", pady=(7, 5))
        ttk.Label(row, text="影片文件", width=9, style="Muted.TLabel").pack(side="left")
        self.path_entry = ttk.Entry(row, textvariable=self.path)
        self.path_entry.pack(side="left", fill="x", expand=True)
        self.path_entry.bind("<Return>", lambda _: self.scan())
        self.browse_button = ttk.Button(row, text="选择影片…", command=self.browse)
        self.browse_button.pack(side="left", padx=(8, 0))
        self.scan_button = ttk.Button(row, text="识别", command=self.scan)
        self.scan_button.pack(side="left", padx=(6, 0))
        wrapped_label(find, textvariable=self.identity_text, style="Muted.TLabel")

        resources = ui_section(p, "02  /  选择字幕与弹幕")
        row = ttk.Frame(resources)
        row.pack(fill="x")
        ttk.Label(row, text="台词字幕", width=9).pack(side="left")
        self.sub_box = ttk.Combobox(row, state="readonly", textvariable=self.subtitle)
        self.sub_box.pack(side="left", fill="x", expand=True)
        self.manual_sub = ttk.Button(row, text="补选字幕…", command=self.pick_subtitle)
        self.manual_sub.pack(side="left", padx=(8, 0))
        self.embedded_button = ttk.Button(row, text="使用内封", command=self.use_embedded)
        self.embedded_button.pack(side="left", padx=(6, 0))
        row = ttk.Frame(resources)
        row.pack(fill="x", pady=(7, 0))
        ttk.Label(row, text="电影匹配", width=9).pack(side="left")
        self.movie_box = ttk.Combobox(row, state="readonly", textvariable=self.movie)
        self.movie_box.pack(side="left", fill="x", expand=True)
        self.movie_box.bind("<<ComboboxSelected>>", self.change_movie)
        self.manual_dm = ttk.Button(row, text="补选弹幕…", command=self.pick_danmaku)
        self.manual_dm.pack(side="left", padx=(8, 0))
        row = ttk.Frame(resources)
        row.pack(fill="x", pady=(7, 0))
        ttk.Label(row, text="弹幕来源", width=9).pack(side="left")
        self.platform_box = ttk.Combobox(row, state="disabled", textvariable=self.platform, width=28)
        self.platform_box.pack(side="left", fill="x", expand=True)
        self.platform_box.bind("<<ComboboxSelected>>", self.change_platform)
        self.retry_sources_button = ttk.Button(row, text="重查来源", command=self.retry_sources)
        self.retry_sources_button.pack(side="left", padx=(8, 0))

        settings = ui_section(p, "03  /  调整与生成")
        row = ttk.Frame(settings)
        row.pack(fill="x")
        ttk.Label(row, text="弹幕偏移").pack(side="left")
        ttk.Entry(row, textvariable=self.offset, width=6).pack(side="left", padx=6)
        ttk.Label(row, text="秒（＋延后 / −提前）", style="Muted.TLabel").pack(side="left")
        ttk.Label(row, text="同屏最多").pack(side="left", padx=(16, 0))
        ttk.Spinbox(row, from_=1, to=30, textvariable=self.density, width=4).pack(side="left", padx=6)
        ttk.Label(row, text="条", style="Muted.TLabel").pack(side="left")
        self.settings_button = ttk.Button(row, text="弹幕设置…", command=self.open_settings)
        self.settings_button.pack(side="right")
        ttk.Label(settings, textvariable=self.settings_summary, style="Muted.TLabel").pack(anchor="w", pady=(6, 0))
        row = ttk.Frame(settings)
        row.pack(fill="x", pady=(6, 0))
        self.output_entry = ttk.Entry(row, textvariable=self.output_text, state="readonly")
        self.output_entry.pack(side="left", fill="x", expand=True)
        self.output_directory_button = ttk.Button(row, text="更改目录…", command=self.choose_output_directory)
        self.output_directory_button.pack(side="right", padx=(8, 0))

        details = ui_section(sidebar, "当前弹幕")
        wrapped_label(details, textvariable=self.dm_text, foreground=UI_COLORS["accent"])
        ttk.Separator(details).pack(fill="x", pady=8)
        wrapped_label(details, textvariable=self.source_status, style="Muted.TLabel")
        log_frame = ui_section(sidebar, "运行日志", expand=True)
        self.log_box = tk.Text(log_frame, width=32, height=6, wrap="word", state="disabled",
                               font=("Microsoft YaHei UI", 9), padx=6, pady=6)
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=scrollbar.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        row = ttk.Frame(footer)
        row.pack(fill="x")
        self.generate_button = ttk.Button(row, text="生成到本机", style="Accent.TButton", command=self.generate, state="disabled")
        self.generate_button.pack(side="right")
        self.cancel_button = ttk.Button(row, text="停止等待", command=self.cancel_task, state="disabled")
        self.cancel_button.pack(side="right", padx=8)
        self.open_output_button = ttk.Button(row, text="打开成品文件夹", command=self.open_output, state="disabled")
        self.open_output_button.pack(side="left")
        self.copy_path_button = ttk.Button(row, text="复制成品路径", command=self.copy_output_path, state="disabled")
        self.copy_path_button.pack(side="left", padx=6)
        self.retry_copy_button = ttk.Button(row, text="重试写回 NAS", command=self.retry_copy, state="disabled")
        self.retry_copy_button.pack(side="left", padx=(6, 0))
        ttk.Button(row, text="待写回任务…", command=self.choose_pending).pack(side="left", padx=6)
        self.progress_bar = ttk.Progressbar(footer, mode="determinate", maximum=100)
        self.progress_bar.pack(fill="x", pady=(7, 5))
        wrapped_label(footer, textvariable=self.result_summary)
        wrapped_label(footer, textvariable=self.status)
        wrapped_label(footer, textvariable=self.progress_text, style="Muted.TLabel")
        self.root.after(100, self.poll)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if self.pending_output:
            self.output_text.set("已恢复待写回成品：" + self.pending_output["local_output"])
            self.status.set("上次的本机成品已保留，可重试写回，或在“待写回任务”中选择其他成品。")
        self.update_ready()
        if rule_warning:
            self.status.set(rule_warning)
            self.log(rule_warning)
        if initial:
            self.root.after(200, self.scan)

    def settings_description(self):
        s = self.render_settings
        effective = min(s["area"], 68 if s["avoid_subtitles"] else 100)
        return f"弹幕设置 · 顶部 {effective}% / 字号 {s['font_size']}"

    def refresh_dandan_status(self):
        state = dandan_setup_state()
        self.dandan_summary.set("弹弹play · " + state["label"])
        return state

    def open_dandan_settings(self):
        if self.busy:
            return
        if self.dandan_dialog and self.dandan_dialog.winfo_exists():
            return
        win = self.dandan_dialog = tk.Toplevel(self.root)
        win.title("弹弹play设置")
        body, footer = scrollable_window(win, 680, 600)
        ttk.Label(body, text="弹弹play接入", style="Title.TLabel").pack(anchor="w", pady=(0, 10))
        current = self.refresh_dandan_status()
        summary = tk.StringVar()
        def show_summary(state):
            if state["configured"]:
                saved = "凭证由环境变量提供，环境变量优先生效。" if state["environment"] else "凭证已保存在本机，无需重复填写。"
                summary.set(f"弹弹play官方来源 · {state['label']}\n{saved}\nAppId：{state['app_id']}")
            else:
                summary.set("弹弹play官方来源 · " + state["label"] + "\n" +
                            (state["error"] or "首次使用请填写凭证；保存后会自动记住。"))
        show_summary(current)
        wrapped_label(body, textvariable=summary)
        wrapped_label(body, text="启用后会自动查询官方弹幕，日常使用无需打开此窗口。可在这里停用来源、检查连接或修改凭证。")
        credential_error = ""
        try:
            original = dandan_config(include_disabled=True)
        except (ToolError, OSError) as exc:
            original = dict(app_id=current["app_id"], secret="")
            credential_error = "已保存凭证读取失败：" + str(exc)
        app_id = tk.StringVar(value=original["app_id"])
        secret = tk.StringVar(value=original["secret"])
        enabled = tk.BooleanVar(value=current["enabled"])
        enabled_box = ttk.Checkbutton(body, text="启用官方弹幕来源", variable=enabled)
        enabled_box.pack(anchor="w", pady=8)
        editor = ttk.Frame(body)
        editor.pack(fill="x")
        ttk.Label(editor, text="AppId").pack(anchor="w", pady=(10, 2))
        app_entry = ttk.Entry(editor, textvariable=app_id)
        app_entry.pack(fill="x")
        ttk.Label(editor, text="AppSecret").pack(anchor="w", pady=(10, 2))
        secret_row = ttk.Frame(editor)
        secret_row.pack(fill="x")
        secret_entry = ttk.Entry(secret_row, textvariable=secret, show="●")
        secret_entry.pack(side="left", fill="x", expand=True)
        def toggle_secret():
            masked = bool(secret_entry.cget("show"))
            secret_entry.configure(show="" if masked else "●")
            show_button.configure(text="隐藏" if masked else "显示")
        show_button = ttk.Button(secret_row, text="显示", width=6, command=toggle_secret)
        show_button.pack(side="right", padx=(6, 0))
        wrapped_label(editor, text="已保存的凭证会自动填入，无需重复输入。密钥默认遮住，可点“显示”查看；更换 AppId 时请同时填写对应的新密钥。")
        ttk.Button(editor, text="打开开发者中心", command=lambda: webbrowser.open("https://dev.dandanplay.com/Center")).pack(anchor="w", pady=6)
        note = tk.StringVar(value=credential_error or ("已填入保存的凭证，可直接关闭并识别影片。" if current["configured"]
                            else "填写凭证后点击“保存并验证”。"))
        wrapped_label(body, textvariable=note)
        def save():
            if self.busy:
                return
            new_id, new_secret = app_id.get().strip(), secret.get().strip()
            if new_id != original["app_id"] and original["secret"] and new_secret == original["secret"]:
                note.set("更换 AppId 后，请同时填写对应的新 AppSecret。")
                return
            # An unchanged prefilled key retains the original encrypted value and avoids needless backups.
            values = (new_id, "" if new_id == original["app_id"] and new_secret == original["secret"] else new_secret, enabled.get())
            secret_entry.configure(show="●")
            show_button.configure(text="显示")
            for widget in (app_entry, secret_entry, enabled_box, show_button):
                widget.configure(state="disabled")
            title = self.title.get().strip() or "楚门的世界"
            save_button.configure(state="disabled")
            note.set("正在保存并验证连接…")
            def work():
                save_dandan_config(*values)
                if values[2]:
                    response = dandan_request("/api/v2/search/tmdb?" + urllib.parse.urlencode({"keyword": title}),
                                              progress=self.progress, use_cache=False)
                    return len(response.get("animes") or [])
                return None
            def done(count):
                if win.winfo_exists():
                    note.set("官方来源已停用。" if count is None else f"官方接口验证成功，返回 {count} 部作品。重新识别影片即可使用。")
                    try:
                        original.update(dandan_config(include_disabled=True))
                        app_id.set(original["app_id"])
                        secret.set(original["secret"])
                    except (ToolError, OSError) as exc:
                        note.set("配置已保存，读取凭证失败：" + str(exc))
                self.status.set("弹弹play官方接口已验证，可重新识别影片。" if count is not None else "弹弹play官方来源已停用。")
            def safe_work():
                try:
                    return (True, work())
                except (ToolError, OSError, ValueError) as exc:
                    return (False, str(exc))
            def show(result):
                state = self.refresh_dandan_status()
                if win.winfo_exists():
                    show_summary(state)
                    for widget in (app_entry, secret_entry, enabled_box, show_button, save_button):
                        widget.configure(state="normal")
                if result[0]:
                    done(result[1])
                elif win.winfo_exists():
                    note.set("尚未验证成功：" + result[1])
                    save_button.configure(state="normal")
                    self.status.set("弹弹play配置已检查，但连接尚未验证成功。")
            self.background(safe_work, show)
        save_button = ttk.Button(footer, text="保存并验证", style="Accent.TButton", command=save)
        save_button.pack(side="right")
        ttk.Button(footer, text="关闭", command=win.destroy).pack(side="right", padx=6)

    def open_settings(self):
        if self.busy or (self.result and self.result.dm_ass):
            return
        if self.settings_dialog and self.settings_dialog.window.winfo_exists():
            return
        self.settings_dialog = DanmakuSettingsDialog(self)

    def help(self):
        win = tk.Toplevel(self.root)
        win.title("使用说明")
        win.geometry("740x610")
        header = ttk.Frame(win, padding=(16, 12))
        header.pack(fill="x")
        ttk.Label(header, text="使用说明", style="Title.TLabel").pack(side="left")
        ttk.Button(header, text="关闭", command=win.destroy).pack(side="right")
        body = ttk.Frame(win)
        body.pack(fill="both", expand=True)
        text = tk.Text(body, wrap="word", padx=16, pady=12, font=("Microsoft YaHei UI", 10))
        scrollbar = ttk.Scrollbar(body, command=text.yview)
        scrollbar.pack(side="right", fill="y")
        text.configure(yscrollcommand=scrollbar.set)
        text.pack(fill="both", expand=True)
        text.insert("1.0", HELP)
        text.configure(state="disabled")

    def subtitle_sites(self):
        title = self.title.get().strip()
        if not title and self.path.get().strip():
            title = filename_title(Path(self.path.get().strip().strip('"')).stem)[0]
        if not title:
            return messagebox.showinfo("先填片名", "请先选择影片，或在电影名称框填写片名。")
        win = tk.Toplevel(self.root)
        win.title("字幕网站补选")
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="网页找字幕", style="Title.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Label(frame, text=title, style="Badge.TLabel", wraplength=500).pack(anchor="w", pady=(0, 12))
        ttk.Label(frame, text="自动来源没有合适版本时，可在网站搜索后下载 SRT/ASS，再点“补选字幕”。", wraplength=500).pack(anchor="w", pady=(0, 10))
        sites = [("ASSRT（伪射手）", "https://assrt.net/sub/?" + urllib.parse.urlencode({"searchword": title})),
                 ("SubHD", "https://subhd.tv/search/" + urllib.parse.quote(title, safe="")),
                 ("SubtitleCat（可能机翻）", "https://www.subtitlecat.com/index.php?" + urllib.parse.urlencode({"search": title}))]
        for label, url in sites:
            ttk.Button(frame, text=label, command=lambda u=url: webbrowser.open_new_tab(u)).pack(fill="x", pady=4)
        ttk.Label(frame, text="需要验证码、登录或付费时请在网页自行处理；工具不会绕过这些限制。", wraplength=500).pack(anchor="w", pady=(10, 0))

    def log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def browse(self):
        file = filedialog.askopenfilename(title="选择影片（本地文件或可访问的 NAS 盘）", filetypes=[("影片", "*.mkv *.mp4 *.m4v *.avi *.mov *.ts *.m2ts *.wmv *.webm"), ("所有文件", "*")])
        if file:
            self.path.set(file)
            self.scan()

    def set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for widget in (self.path_entry, self.title_entry, self.browse_button, self.scan_button, self.retry_button, self.manual_sub, self.manual_dm, self.embedded_button):
            widget.configure(state=state)
        self.sub_box.configure(state="disabled" if busy else "readonly")
        self.movie_box.configure(state="disabled" if busy else "readonly")
        if busy:
            self.task_started = time.monotonic()
            self.active_progress = None
            self.apply_progress(ProgressUpdate("准备处理"))
        self.update_ready()

    def update_ready(self):
        has_video = self.result is not None and self.result.video is not None
        ready = (not self.busy or self.cancellable) and self.result is not None and bool(self.result.subtitles) and bool(self.result.comments or self.result.dm_ass)
        self.generate_button.configure(state="normal" if ready else "disabled")
        self.generate_button.configure(text="使用已取得结果合成" if self.busy and self.cancellable and ready else
                                       "确认合成并写回" if has_video else "生成到本机")
        self.cancel_button.configure(state="normal" if self.busy and self.cancellable else "disabled")
        self.open_output_button.configure(state="normal" if self.last_output else "disabled")
        self.copy_path_button.configure(state="normal" if self.last_output else "disabled")
        self.retry_copy_button.configure(state="normal" if not self.busy and self.pending_output else "disabled")
        has_platforms = self.result is not None and bool(self.result.movies) and self.movie_box.current() >= 0
        self.platform_box.configure(state="readonly" if not self.busy and has_platforms and self.platform_keys else "disabled")
        self.retry_sources_button.configure(state="normal" if not self.busy and has_platforms else "disabled")
        self.settings_button.configure(state="disabled" if self.busy or (self.result and self.result.dm_ass) else "normal")
        self.embedded_button.configure(state="normal" if has_video and not self.busy else "disabled")
        self.output_directory_button.configure(state="disabled" if self.busy or has_video else "normal")

    def choose_output_directory(self):
        if self.busy or (self.result and self.result.video is not None):
            return
        selected = filedialog.askdirectory(parent=self.root, title="选择本机成品保存目录", mustexist=True,
                                           initialdir=str(self.output_directory))
        if not selected:
            return
        try:
            folder = checked_output_directory(selected)
            save_local_json("output.local.json", dict(version=1, directory=str(folder)))
        except (ToolError, OSError, ValueError) as exc:
            return messagebox.showerror("保存目录未更改", str(exc), parent=self.root)
        self.output_directory = folder
        self.output_directory_readable = True
        self.output_text.set("本机保存目录：" + str(folder))
        self.status.set("保存目录已记住，下次生成到这里；已有成品保留在原位置。")

    def background(self, work, done, *, cancellable=False):
        if self.busy:
            return
        self.task_id += 1
        ticket = self.task_id
        event = self.cancel_event = threading.Event()
        self.cancellable = cancellable
        self.set_busy(True)
        def run():
            self.task_local.context = (ticket, event)
            try:
                value = work()
                if not event.is_set():
                    self.tasks.put(("done", done, value, ticket))
            except TaskCancelled:
                pass
            except Exception as exc:
                if not event.is_set():
                    self.tasks.put(("error", str(exc), None, ticket))
        threading.Thread(target=run, daemon=True).start()

    def progress(self, message):
        self.progress_callback()(message)

    def progress_callback(self):
        ticket, event = getattr(self.task_local, "context", (self.task_id, self.cancel_event))
        def callback(message):
            if event.is_set():
                raise TaskCancelled()
            self.tasks.put(("progress", message, None, ticket))
        return callback

    def snapshot_callback(self, done):
        ticket, event = self.task_local.context
        def callback(result):
            if event.is_set():
                raise TaskCancelled()
            self.tasks.put(("snapshot", done, copy.deepcopy(result), ticket))
        return callback

    def cancel_task(self):
        if not self.busy or not self.cancellable:
            return
        self.cancel_event.set()
        self.task_id += 1
        self.cancellable = False
        self.set_busy(False)
        self.progress_bar["value"] = 0
        self.progress_text.set("已停止等待；已取得的结果保留，尚在连接的请求将在超时后结束。")
        self.status.set("可以使用已有结果、补选文件，或重试未完成的来源。")
        self.refresh_platforms()

    def save_current_preferences(self):
        if not self.preferences_readable:
            raise ToolError("原显示设置读取失败，未覆盖原文件；请在弹幕设置中核对并应用后再保存。")
        offsets = dict(self.saved_offsets)
        if self.result and self.result.video is not None:
            offsets[video_preference_key(self.result.video)] = finite(self.offset.get(), "弹幕偏移")
        store_preferences(self.render_settings, int(self.density.get()), offsets)
        self.saved_offsets = offsets

    def close(self):
        if self.busy and not self.cancellable:
            messagebox.showinfo("正在处理", "请等待本次合成或写回完成后关闭，避免中断成品保存。", parent=self.root)
            return
        try:
            self.save_current_preferences()
        except (ToolError, ValueError, OSError) as exc:
            if not messagebox.askyesno("设置未保存", str(exc) + "\n仍然关闭吗？", parent=self.root):
                return
        self.cancel_event.set()
        self.root.destroy()

    def apply_progress(self, update):
        if isinstance(update, str):
            update = ProgressUpdate(update)
        if self.active_progress is None or update.message != self.active_progress.message:
            self.step_started = time.monotonic()
            self.log(update.message)
        self.active_progress = update
        self.status.set("当前步骤：" + update.message)
        self.progress_bar["value"] = update.percent if update.percent is not None else 0
        self.refresh_progress_time()

    def refresh_progress_time(self):
        if self.busy and self.active_progress is not None:
            self.progress_text.set(progress_detail(self.active_progress, time.monotonic() - self.step_started))

    def poll(self):
        try:
            while True:
                item = self.tasks.get_nowait()
                kind, first, second = item[:3]
                if len(item) > 3 and item[3] != self.task_id:
                    continue
                if kind == "progress":
                    self.apply_progress(first)
                elif kind == "snapshot":
                    first(second)
                    self.update_ready()
                elif kind == "error":
                    if self.result is None:
                        self.subtitle.set("查询未完成，请检查输入后重试")
                        self.movie.set("识别未完成")
                        self.dm_text.set("尚未获取弹幕")
                        self.source_status.set("来源查询未完成，请查看下方错误原因。")
                    self.set_busy(False)
                    self.progress_bar["value"] = 0
                    self.progress_text.set("已停止 · 用时 " + elapsed_text(time.monotonic() - self.task_started))
                    self.status.set("未完成：" + first)
                    self.log(first)
                    messagebox.showerror("未完成", first)
                else:
                    self.set_busy(False)
                    self.progress_bar["value"] = 100
                    self.progress_text.set("本次处理结束 · 用时 " + elapsed_text(time.monotonic() - self.task_started))
                    first(second)
                    self.update_ready()
        except queue.Empty:
            pass
        self.refresh_progress_time()
        self.root.after(100, self.poll)

    def scan(self, override=False):
        if self.busy:
            return
        path = self.path.get().strip().strip('"')
        title = self.title.get().strip() if override or not path else ""
        if override and not title:
            self.status.set("请输入电影名称，例如：楚门的世界 1998。")
            return
        if not path and not title:
            return self.browse()
        if path and not override and self.result is not None and self.result.video is None:
            return self.attach_video(path)
        try:
            self.save_current_preferences()
        except (ToolError, ValueError, OSError) as exc:
            self.log("设置未保存：" + str(exc))
        self.result = None
        self.last_output = None
        self.result_summary.set("")
        self.offset.set(str(self.saved_offsets.get(video_preference_key(path), 0)) if path else "0")
        self.sub_box.configure(values=[])
        self.movie_box.configure(values=[])
        self.platform_keys = []
        self.platform_box.configure(values=[])
        self.platform.set("来源待查询")
        self.source_status.set("正在核实各平台弹幕，取得后显示来源与条数。")
        self.subtitle.set("正在识别…")
        self.movie.set("正在查找…")
        self.dm_text.set("正在获取…")
        self.update_ready()
        def done(result, final=True):
            self.result = result
            self.path.set(str(result.video) if result.video is not None else "")
            if result.video is None:
                self.offset.set("0")
            self.title.set(result.identity["title"])
            self.show_video_info()
            self.sub_box.configure(values=[c.label for c in result.subtitles])
            if result.subtitles:
                self.sub_box.current(0)
            else:
                self.subtitle.set("未找到可用文字字幕（见下方提示）")
            self.movie_box.configure(values=[movie_choice_label(m, result.source_catalog) for m in result.movies])
            if result.movies and result.movies[0].get("official_confirm"):
                self.movie.set("请展开列表选择具体电影或剧集")
            elif result.movies:
                self.movie_box.current(0)
            else:
                self.movie.set("未匹配到电影（可以修正片名重查）")
            self.refresh_platforms()
            self.show_danmaku()
            if final:
                for warning in result.warnings:
                    self.log("提示：" + warning)
                self.log("本地缓存：" + str(result.workspace))
                if result.video is None:
                    self.status.set("片名搜索完成；可生成到本机，也可选择影片后写回原目录。" if result.subtitles and (result.comments or result.dm_ass)
                                    else "片名搜索完成，仍有缺失项；请查看提示或补选字幕/弹幕。")
                else:
                    self.status.set("已找到字幕和弹幕，请核对后确认合成。" if result.subtitles and (result.comments or result.dm_ass) else "识别结束，仍有缺失项，请查看提示。")
            else:
                self.status.set("已取得可用结果，可直接合成或继续等待其他来源。" if result.video is not None and result.subtitles and result.comments else "正在查询；已取得的结果会陆续显示。")
        def work():
            return scan_movie(path or None, title, self.progress_callback(),
                              on_update=self.snapshot_callback(lambda result: done(result, False)))
        self.background(work, done, cancellable=True)

    def show_video_info(self):
        result = self.result
        year = result.identity.get("year") or "未知"
        if result.video is None:
            self.identity_text.set(f"按片名搜索 · 年份：{year} · 尚未选择影片文件")
            self.output_text.set("本机保存目录：" + str(self.output_directory))
        else:
            length = float(result.metadata.get("format", {}).get("duration", 0) or 0)
            length_text = f"约 {length / 60:.1f} 分钟" if length else "未知"
            self.identity_text.set(f"{result.identity['source']}识别 · 年份：{year} · 片长：{length_text}")
            self.output_text.set("输出到：" + str(result.video.with_name(f"弹幕版-{result.video.stem}.ass")))

    def attach_video(self, path):
        result = self.result
        def done(updated):
            self.result = updated
            self.path.set(str(updated.video))
            self.offset.set(str(self.saved_offsets.get(video_preference_key(updated.video), 0)))
            self.show_video_info()
            for warning in updated.warnings[len(result.warnings):]:
                self.log(warning)
            self.status.set("影片已选择，已保留查询结果；请核对电影、字幕版本和弹幕时间后合成。")
            self.log("已关联影片：" + str(updated.video))
        self.background(lambda: attach_search_video(result, path, self.progress), done, cancellable=True)

    def show_danmaku(self):
        r = self.result
        if r and r.comments:
            snippets = " / ".join(c.text[:28].replace("\n", " ") for c in r.comments[:2])
            self.dm_text.set(f"已获取 {len(r.comments):,} 条 · {r.danmaku_source}\n原始预览（合成时过滤）：{snippets}")
        elif r and r.dm_ass:
            self.dm_text.set(f"已导入 {len(r.dm_ass.events)} 行 ASS 弹幕；保留原字号/密度，画布需与台词相同。")
            self.settings_summary.set("ASS 原有排版（仅偏移）")
        else:
            self.dm_text.set("尚未获取有效弹幕。可切换候选影片，或修正片名重查。")
        if not (r and r.dm_ass):
            self.settings_summary.set(self.settings_description())

    def refresh_platforms(self):
        index = self.movie_box.current()
        r = self.result
        movie = r.movies[index] if r and 0 <= index < len(r.movies) else None
        options = r.source_catalog.get(movie_source_key(movie)) if movie else None
        available = [option for option in options or [] if option.available]
        self.platform_keys = [option.platform for option in available]
        labels = [option.label for option in available]
        self.platform_box.configure(values=labels)
        if movie and r.selected_movie_key == movie_source_key(movie) and r.selected_platform in self.platform_keys:
            self.platform_box.current(self.platform_keys.index(r.selected_platform))
        elif r and r.danmaku_source.startswith("手动补选"):
            self.platform.set("手动导入")
        else:
            self.platform.set("请选择已取得的来源" if available else "未取得可用来源" if options is not None else "来源待查询")
        summary = source_summary(options) if movie else ("请先在电影列表选择具体电影或剧集，再获取弹幕。"
            if r and r.movies else "未匹配到电影来源，可修正片名或手动补选弹幕。")
        if r and r.source_pending:
            summary += ("；查询中：" if self.busy else "；待重试：") + "、".join(dict.fromkeys(platform_name(key) for key in r.source_pending))
        self.source_status.set(summary + ("；未取得的原因见下方日志。" if any(not option.available for option in options or []) else ""))

    def change_platform(self, _=None):
        index, source_index = self.movie_box.current(), self.platform_box.current()
        if self.busy or not self.result or not 0 <= index < len(self.result.movies) or not 0 <= source_index < len(self.platform_keys):
            return
        try:
            select_danmaku_source(self.result, self.result.movies[index], self.platform_keys[source_index])
        except ToolError as exc:
            self.status.set(str(exc))
            self.refresh_platforms()
            return
        self.show_danmaku()
        self.status.set("已切换到本机缓存的弹幕，无需重新下载；请核对后确认合成。")
        self.update_ready()

    def change_movie(self, _=None, *, retry_failed=False):
        index = self.movie_box.current()
        if self.busy or not self.result or not 0 <= index < len(self.result.movies):
            return
        r = self.result
        movie = r.movies[index]
        key = movie_source_key(movie)
        previous_platform = r.selected_platform if r.selected_movie_key == key else ""
        keep_manual = retry_failed and r.danmaku_source.startswith("手动补选") and bool(r.comments or r.dm_ass)
        if not retry_failed:
            clear_selected_danmaku(r)
            r.source_pending = [platform for platform in movie_source_links(movie) if
                                not any(row.platform == platform for row in r.source_catalog.get(key, []))]
        self.refresh_platforms()
        self.show_danmaku()
        self.update_ready()
        def done(updated, final=True):
            r = updated
            self.result = r
            options = r.source_catalog.get(key, [])
            if any(option.available for option in options) and not keep_manual:
                preferred = previous_platform if any(option.available and option.platform == previous_platform for option in options) else None
                select_danmaku_source(r, movie, preferred)
            elif not retry_failed:
                clear_selected_danmaku(r)
            self.movie_box.configure(values=[movie_choice_label(m, r.source_catalog) for m in r.movies])
            self.movie_box.current(index)
            self.refresh_platforms()
            self.show_danmaku()
            for option in options:
                if final and not option.available:
                    self.log(f"{platform_name(option.platform)}未取得弹幕：{option.error}")
            self.status.set("已列出弹幕来源与条数，切换直接使用本机缓存。" if any(option.available for option in options)
                            else "当前候选未取得在线弹幕，可重查来源、换候选或补选文件。")
            self.update_ready()
        if key in r.source_catalog and not retry_failed:
            done(r)
        else:
            working = copy.deepcopy(r)
            def work():
                notify = self.snapshot_callback(lambda result: done(result, False))
                discover_danmaku_sources(working, movie, self.progress_callback(), retry_failed=retry_failed,
                                         on_update=lambda _: notify(working))
                return working
            self.background(work, done, cancellable=True)

    def retry_sources(self):
        self.change_movie(retry_failed=True)

    def use_embedded(self):
        if self.busy or self.result is None or self.result.video is None:
            return
        choices = embedded_choices(self.result.metadata)
        if not choices:
            return messagebox.showinfo("无可用内封文字字幕", "未发现可用内封文字轨，请补选 SRT/ASS。PGS/SUP 图片字幕需要先 OCR。")
        for choice in choices:
            choice.label += "（需读取影片，异地较慢）"
        self.result.subtitles = choices + [c for c in self.result.subtitles if c.kind != "embedded"]
        self.sub_box.configure(values=[c.label for c in self.result.subtitles])
        self.sub_box.current(0)
        self.status.set("已手动选择内封字幕；确认合成时需要扫描影片，异地可能较慢。")
        self.update_ready()

    def pick_subtitle(self):
        if self.busy:
            return
        if not self.result:
            return messagebox.showinfo("先查电影", "先选择影片或输入片名搜索，再补选字幕。")
        path = filedialog.askopenfilename(title="补选原台词字幕", filetypes=[("文字字幕", "*.srt *.ass")])
        if path:
            r = self.result
            def work():
                self.progress("正在后台读取并缓存字幕…")
                folder = r.workspace or local_workspace()
                choice = SubtitleChoice("手动补选 · " + Path(path).name, "file", 999, path)
                return cache_subtitle(choice, folder), folder
            def done(value):
                choice, r.workspace = value
                r.subtitles.insert(0, choice)
                self.sub_box.configure(values=[c.label for c in r.subtitles])
                self.sub_box.current(0)
                self.status.set("字幕已导入并缓存。" + ("可生成到本机。" if r.video is None else "可以合成。"))
                self.update_ready()
            self.background(work, done)

    def pick_danmaku(self):
        if self.busy:
            return
        if not self.result:
            return messagebox.showinfo("先查电影", "先选择影片或输入片名搜索，再补选弹幕。")
        path = filedialog.askopenfilename(title="补选弹幕文件", filetypes=[("弹幕", "*.json *.xml *.ass")])
        if path:
            staged = dataclass_replace(self.result)
            def work():
                self.progress("正在后台读取并缓存弹幕…")
                if Path(path).suffix.lower() == ".ass":
                    staged.dm_ass = parse_ass(read_text(path))
                    staged.comments = []
                else:
                    staged.comments, _ = parse_comments(read_text(path))
                    staged.dm_ass = None
                staged.danmaku_source = "手动补选 · " + Path(path).name
                staged.danmaku_url = ""
                staged.selected_movie_key, staged.selected_platform = None, ""
                cache_danmaku(staged)
                return staged
            def done(result):
                self.result = result
                self.platform.set("手动导入")
                self.show_danmaku()
                self.status.set("弹幕已导入并缓存。" + ("可生成到本机。" if result.video is None else "可以合成。"))
                self.update_ready()
            self.background(work, done)

    def generate(self):
        if self.busy and self.cancellable and self.result and self.result.subtitles and (self.result.comments or self.result.dm_ass):
            self.cancel_task()
        r = self.result
        if self.busy or r is None:
            return
        try:
            if r.video is not None:
                current = os.path.normcase(os.path.abspath(self.path.get().strip().strip('"')))
                if current != os.path.normcase(str(r.video)):
                    raise ToolError("路径已经改变，请点击“识别”重新读取后再合成。")
            index = self.sub_box.current()
            offset, density = finite(self.offset.get()), int(self.density.get())
            output_dir = None
            if r.video is None:
                if not self.output_directory_readable:
                    raise ToolError("原保存目录设置读取失败，请点击“更改目录”核对并重新选择。")
                output_dir = checked_output_directory(self.output_directory)
            self.save_current_preferences()
        except (ToolError, ValueError, OSError) as exc:
            return messagebox.showerror("请检查输入", str(exc))
        settings = dict(self.render_settings)
        self.background(lambda: synthesize(r, index, offset, density, progress=self.progress,
                                          output_dir=output_dir, **settings), self.show_output)

    def show_output(self, value):
        self.last_output = value
        self.log(f"台词 {value['subtitle_lines']} 行，弹幕 {value['danmaku_lines']} 条；过滤/去重/限流 {value['filtered']} 条。")
        self.log(f"其中内置规则屏蔽 {value.get('noise_filtered', 0)} 条，自定义规则屏蔽 {value.get('keyword_filtered', 0)} 条。")
        stats = value.get("filter_stats", {})
        summary = (f"原始 {value.get('raw_count', value['danmaku_lines'] + value['filtered']):,} 条 → "
                   f"最终 {value['danmaku_lines']:,} 条；类型屏蔽 {stats.get('types', 0):,}、"
                   f"规则屏蔽 {stats.get('noise', 0) + stats.get('keywords', 0):,}、"
                   f"重复 {stats.get('duplicates', 0):,}、密度/空间限制 {stats.get('density', 0):,}、"
                   f"时间范围外 {stats.get('time', 0):,}。")
        self.result_summary.set(summary)
        self.log(summary)
        if value.get("recovery_warning"):
            self.log(value["recovery_warning"])
        if value.get("local_only"):
            self.status.set("合成完成，已保存到本机；可打开成品文件夹，核对版本和时间轴后使用。")
            self.output_text.set("本机成品：" + value["output"])
            messagebox.showinfo("本机合成完成", value["output"] + "\n\n可用“打开成品文件夹”找到文件。\n尚未核验影片版本和片长，请检查字幕时间轴后使用。")
        elif value.get("saved", True):
            self.pending_output = None
            self.status.set("合成完成，已保存到影片原目录。")
            self.output_text.set("已保存：" + value["output"])
            messagebox.showinfo("合成完成", value["output"] + "\n\n在极影视中选择这条以“弹幕版-”开头的字幕即可。")
        else:
            self.pending_output = value
            self.status.set("本机合成成功，NAS 写回未完成。恢复连接后点“重试写回 NAS”。")
            self.output_text.set("本地成品：" + value["local_output"])
            self.progress_bar["value"] = 0
            self.progress_text.set("本地成品已保留 · 等待写回 NAS")
            self.log("写回原因：" + value["write_error"])
            messagebox.showwarning("本地完成，等待写回", "合成字幕已保存在电脑：\n" + value["local_output"] +
                                   "\n\n" + value["write_error"] + "\n\n恢复 NAS 连接后点“重试写回 NAS”，无需重新下载或合成。")
        self.update_ready()

    def output_path(self):
        if not self.last_output:
            return ""
        return self.last_output.get("output", self.last_output["local_output"]) if self.last_output.get("saved") else self.last_output["local_output"]

    def copy_output_path(self):
        path = self.output_path()
        if path:
            self.root.clipboard_clear()
            self.root.clipboard_append(path)
            self.status.set("成品路径已复制。")

    def open_output(self):
        path = self.output_path()
        if path:
            try:
                if os.name == "nt":
                    subprocess.Popen(["explorer.exe", "/select,", path])
                else:
                    webbrowser.open(Path(path).parent.as_uri())
            except OSError as exc:
                self.status.set("打开文件夹失败，可复制路径手动打开：" + str(exc))

    def choose_pending(self):
        if self.busy:
            return
        try:
            rows = pending_outputs()
        except (ToolError, OSError) as exc:
            return messagebox.showerror("无法读取待写回任务", str(exc), parent=self.root)
        if not rows:
            return messagebox.showinfo("待写回任务", "没有尚未写回的成品。", parent=self.root)
        win = tk.Toplevel(self.root)
        win.title("恢复待写回成品")
        body, footer = scrollable_window(win, 700, 360)
        ttk.Label(body, text="待写回成品", style="Title.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Label(body, text="选择已有成品后重试写回，不重新下载或合成。", wraplength=620).pack(anchor="w")
        choices = ttk.Combobox(body, state="readonly", values=[Path(row["local_output"]).name + " · " + str(i + 1) for i, row in enumerate(rows)])
        choices.pack(fill="x", pady=8)
        detail = tk.StringVar()
        ttk.Label(body, textvariable=detail, wraplength=620).pack(anchor="w")
        def describe(_=None):
            row = rows[choices.current()]
            detail.set("本机：" + row["local_output"] + "\n目标：" + row["target"] + "\n" + row.get("write_error", ""))
        def select():
            if self.busy:
                return
            self.pending_output = self.last_output = rows[choices.current()]
            self.output_text.set("待写回成品：" + self.pending_output["local_output"])
            self.status.set("已恢复成品，点“重试写回 NAS”即可。")
            self.update_ready()
            win.destroy()
        choices.bind("<<ComboboxSelected>>", describe)
        choices.current(len(rows) - 1)
        describe()
        ttk.Button(footer, text="恢复这个任务", style="Accent.TButton", command=select).pack(side="right")
        ttk.Button(footer, text="关闭", command=win.destroy).pack(side="right", padx=6)

    def retry_copy(self):
        if not self.busy and self.pending_output:
            value = self.pending_output
            self.background(lambda: publish_cached(value, self.progress), self.show_output)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--_filter-rules-worker"]:
        try:
            payload = json.loads(sys.stdin.buffer.read())
            result = _evaluate_filter_rules(payload["texts"], payload["rules"], payload["enabled"], payload["all_matches"])
            sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        except Exception as exc:
            sys.stderr.buffer.write(str(exc).encode("utf-8"))
            return 1
        return 0
    parser = argparse.ArgumentParser(description="选择影片→自动找字幕和弹幕→确认合成到原目录。单文件，无需 API 密钥。")
    parser.add_argument("video", nargs="?", help="可选：本地/可访问 NAS 影片路径")
    args = parser.parse_args(argv)
    root = tk.Tk()
    App(root, args.video or "")
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
