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
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

VERSION = "2.3.0"
MAX_BYTES = 32 * 1024 * 1024
STYLE_FIELDS = "Name Fontname Fontsize PrimaryColour SecondaryColour OutlineColour BackColour Bold Italic Underline StrikeOut ScaleX ScaleY Spacing Angle BorderStyle Outline Shadow Alignment MarginL MarginR MarginV Encoding".split()
EVENT_FIELDS = "Layer Start End Style Name MarginL MarginR MarginV Effect Text".split()
TEXT_CODECS = {"ass", "ssa", "subrip", "srt", "mov_text", "text", "webvtt"}
DM_DEFAULTS = dict(font_size=32, duration=8, area=25, opacity=80, block_scroll=False,
                   block_fixed=True, block_color=False, avoid_subtitles=True, deduplicate=True)


class ToolError(Exception):
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


def render_comments(comments, resolution, offset=0, density=6, duration=8, font_size=32, progress=None,
                    *, area=25, opacity=80, block_scroll=False, block_fixed=True, block_color=False,
                    avoid_subtitles=True, deduplicate=True):
    width, height = resolution
    offset, duration, font_size = finite(offset, "弹幕偏移"), finite(duration, "滚动时长"), finite(font_size, "字号")
    density, area, opacity = finite(density, "同屏条数"), finite(area, "显示区域"), finite(opacity, "不透明度")
    if not density.is_integer() or not 1 <= density <= 30 or not 2 <= duration <= 20 or not 16 <= font_size <= 100:
        raise ToolError("同屏条数范围 1–30，滚动时长 2–20 秒，字号 16–100（以 1080p 为基准）。")
    if not 10 <= area <= 100 or not 10 <= opacity <= 100:
        raise ToolError("显示区域和不透明度范围均为 10–100%。")
    size = font_size * height / 1080
    top, row_height = height * .025, size * 1.45
    bottom = height * min(area / 100, .68 if avoid_subtitles else 1)
    lanes = int((bottom - top) / row_height)
    if lanes < 1:
        raise ToolError("当前显示区域放不下一行弹幕，请缩小字号或增大显示区域。")
    available = [-1.0] * lanes
    doc = Ass(styles={"Scroll": style("Scroll", round(size, 2))})
    doc.info.update(PlayResX=str(width), PlayResY=str(height), WrapStyle="2")
    doc.styles["Scroll"].update(Alignment="7", Outline=str(round(max(1, size / 22), 2)), MarginL="0", MarginR="0", MarginV="0")
    alpha = round(255 * (1 - opacity / 100))
    for key in ("PrimaryColour", "SecondaryColour", "OutlineColour", "BackColour"):
        doc.styles["Scroll"][key] = f"&H{alpha:02X}" + doc.styles["Scroll"][key][-6:]
    seen, omitted = {}, 0
    total = len(comments)
    report(progress, "排列弹幕", 0, total, "条")
    for index, comment in enumerate(sorted(comments, key=lambda x: x.time)):
        # 计数指已检查的条目，包含去重/限流丢弃项。
        if index and index % max(1, total // 100) == 0:
            report(progress, "排列弹幕", index, total, "条")
        at = comment.time + offset
        fixed = comment.mode in (4, 5)
        if (fixed and block_fixed) or (not fixed and block_scroll) or (block_color and comment.color != 0xFFFFFF):
            omitted += 1
            continue
        message = escape_text(comment.text)[:120]
        if at < 0 or (deduplicate and message in seen and at - seen[message] < 15):
            omitted += 1
            continue
        order = range(lanes - 1, -1, -1) if comment.mode == 4 else range(lanes)
        lane = next((i for i in order if available[i] <= at), None)
        if sum(end > at for end in available) >= density:
            lane = None
        if lane is None:
            omitted += 1
            continue
        seen[message] = at
        available[lane] = at + duration
        # 所有模式共用行占用表；固定/反向弹幕也不能与滚动弹幕相撞。
        units = sum(1 if unicodedata.east_asian_width(c) in "WF" else .65 for c in message)
        length = max(size, units * size * 1.2)
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
        doc.events.append(event(round(at * 100), round((at + duration) * 100), tags + message, "Scroll"))
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
          offset=0, subtitle_offset=0, density=6, duration=8, font_size=32):
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
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

PUBLIC_DANMAKU = "https://dmku.hls.one/"
PUBLIC_DANMAKU_BACKUP = "https://danmu.zxz.ee/"
SHOOTER_API = "https://www.shooter.cn/api/subapi.php"
THUNDER_SUBTITLE_API = "https://api-shoulei-ssl.xunlei.com/oracle/subtitle"
KAN_SEARCH = "https://api.so.360kan.com/index"
PLATFORMS = {"qq": "腾讯视频", "qiyi": "爱奇艺", "bilibili1": "哔哩哔哩", "youku": "优酷", "imgo": "芒果TV"}
HOSTS = ("qq.com", "iqiyi.com", "bilibili.com", "youku.com", "mgtv.com")
HELP = """字幕弹幕一键合成 v2（单文件）

使用：选择影片 → 自动识别片名、字幕及弹幕 → 核对下面的结果 → 确认合成。
输出：影片原目录 / 弹幕版-影片完整文件名.ass。同名则自动加 -v2，不覆盖原文件。

只需要这一个 .py；Python 3.10+（含 Tkinter）。读取影片信息和内封文字字幕需要先安装 ffprobe/ffmpeg，并确保可在命令行中运行。
路径必须是 Windows 能读取的本地/映射盘/UNC 路径。极空间 App 里的虚拟路径或分享链接不能直接当文件路径。

自动字幕：同目录外挂中文字幕 → 迅雷 → SubHD → 射手指纹 → SubtitleCat（可能机翻）。
已有可用结果就停止查询；失败自动换源，原因会显示在日志。顶部“网页找字幕”可到 ASSRT 等网站手动补选。
ZIP 字幕包直接读取；7z/RAR 包需要本机已有 7-Zip。只下载现成字幕，不自动发起翻译或绕过网站验证。
若只有 PGS/SUP 图片字幕且在线未找到文字字幕，会明确提示；本工具不做 OCR、不假装已成功。
内封字幕只在手动点“使用内封”后选用；提取可能需扫描影片，异地较慢。
台词和弹幕先缓存到本机，合成后仅将最终 ASS 写回 NAS；不下载整部视频。
NAS 写回失败会保留本机成品，恢复连接后可点“重试写回 NAS”，不用重新合成。
缓存和成品保存在 %LOCALAPPDATA%/NasDanmaku/cache，界面日志会显示具体目录。

弹幕：按片名通过 360 影视查找电影平台链接，再向公开弹幕库按需请求；不需要你填密钥。
“来源”可选自动或当前电影找到的平台。指定平台失败会提示，可自行换源；不会悄悄换成其他平台。
公开服务： https://dmku.hls.one/ ；备用 https://danmu.zxz.ee/ 。
在线字幕：迅雷、SubHD 按片名查询；射手按视频指纹查询；SubtitleCat 作为可能机翻的末位备用。
这些外部服务可能变更/限流、没有某部电影的数据。失败会显示原因，支持修改片名重新识别或手动补选文件。
只发送查询片名、影片文件名/四段 MD5 和公开平台链接；不上传电影、原台词内容、NAS 目录或账号。

识别结果需核对片名、年份、时长，平台上架年份有时与上映年不同。
弹幕偏移：正数延后、负数提前。不同剪辑版本可能无法只用一个偏移完全对齐。
默认弹幕只在顶部 1/4 滚动，字号 32（1080p 基准）、不透明度 80%、最多同屏 6 条，屏蔽固定弹幕。
“弹幕设置”可调显示区域、字号、不透明度、速度及类型过滤；只影响弹幕，不改原台词字幕。
勾选防挡字幕时，即使放大区域也会保留底部 32%；固定弹幕手动开启后同样限制在所选区域内。
设置保留在当前窗口，重新启动恢复默认。调整后点确认合成，无需重新下载；已有 ASS 不会自动改变。
手动导入的 ASS 已有排版，只支持时间偏移；要调整区域和字号请用在线弹幕或 XML/JSON。
字幕与弹幕同属一条 ASS，播放时选择这条字幕即可。想仅看台词，选回原字幕轨。

进度条表示当前步骤；切换步骤时会归零。没有可用总量时只显示等待/接收量和耗时。
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


def normalize_title(text):
    return "".join(c for c in unicodedata.normalize("NFKC", html.unescape(str(text))).casefold() if c.isalnum())


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
    titles = [identity["title"], filename_title(Path(video).stem)[0]]
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


def subtitle_archive_members(raw):
    """仅返回文字字幕字节；压缩包里的路径不落地。"""
    if raw.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                items = archive.infolist()
                if len(items) > 100 or sum(i.file_size for i in items) > MAX_BYTES:
                    raise ToolError("字幕压缩包解压总量或文件数超限。")
                for item in items:
                    if safe_archive_member(item.filename) and not item.flag_bits & 1 and Path(item.filename).suffix.lower() in {".ass", ".srt"}:
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
            if not safe_archive_member(name) or item.get("Encrypted") == "+" or Path(name).suffix.lower() not in {".srt", ".ass"}:
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
        sidecars = sidecar_choices(video)
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


def canonical_platform_url(url):
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not any(host == h or host.endswith("." + h) for h in HOSTS):
        raise ToolError("未识别的影片平台链接。")
    # 优酷 video?vid= 必须保留 vid；去掉广告跟踪参数。
    query = urllib.parse.urlencode({k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items() if k in {"vid", "id", "cid", "bvid"}})
    return urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path, query, ""))


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
            if re.search(r"有\s*\d+\s*条弹幕列队来袭", message):
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


def fetch_public_danmaku(movie, progress, platform=None):
    errors = []
    if platform is not None and (platform not in PLATFORMS or platform not in movie["links"]):
        raise ToolError("当前电影没有所选平台的链接，请选择列表中的其他来源。")
    # 最多两种平台，失败信息可见；不会对所有来源无限重试。
    keys = [platform] if platform else [k for k in PLATFORMS if k in movie["links"]][:2]
    for key in keys:
        if key not in movie["links"]:
            continue
        url = canonical_platform_url(movie["links"][key])
        try:
            data = web_json(PUBLIC_DANMAKU + "?" + urllib.parse.urlencode({"ac": "dm", "url": url}),
                            progress=progress, message=f"下载{PLATFORMS[key]}弹幕（主源）")
            report(progress, "解析弹幕")
            comments = parse_public_comments(data)
            return comments, PLATFORMS[key] + " · 公益弹幕库", url
        except ToolError as exc:
            errors.append(str(exc))
        try:
            raw = web_bytes(PUBLIC_DANMAKU_BACKUP + "?" + urllib.parse.urlencode({"type": "xml", "id": url}),
                            progress=progress, message=f"下载{PLATFORMS[key]}弹幕（备用源）")
            report(progress, "解析弹幕")
            comments, _ = parse_comments(raw.decode("utf-8-sig"))
            return comments, PLATFORMS[key] + " · 公共弹幕库备用", url
        except (ToolError, UnicodeError) as exc:
            errors.append(str(exc))
    raise ToolError("未能取得这部影片的弹幕。" + ("；".join(dict.fromkeys(errors)) if errors else "没有受支持的平台链接。"))


@dataclass
class ScanResult:
    video: Path
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


def file_signature(video):
    info = Path(video).stat()
    return info.st_size, info.st_mtime_ns


def scan_movie(video, override="", progress=lambda _: None):
    video = normalize_path(video)
    validate_video(video)
    signature = file_signature(video)
    progress("正在读取影片信息…")
    warnings = []
    try:
        meta = inspect_video(video)
    except (ToolError, OSError) as exc:
        meta = {}
        warnings.append("影片信息探测未完成，继续按片名搜索外挂字幕；片长暂未知。" + str(exc))
    try:
        identity = identify_movie(video, override)
    except OSError:
        title, year = filename_title(video.stem)
        identity = {"title": override.strip() or title, "year": year, "source": "修正片名" if override.strip() else "文件名"}
    result = ScanResult(video, identity, meta, signature, warnings=warnings, workspace=local_workspace())
    progress(f"识别片名：{identity['title']} {identity['year']}；正在查找字幕和电影弹幕…")
    report(progress, "查找字幕和电影来源", 0, 2, "项")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        sub_future = pool.submit(discover_subtitles, video, meta, lambda _: None, identity, result.workspace)
        movie_future = pool.submit(search_movies, identity["title"], identity["year"])
        for count, future in enumerate(concurrent.futures.as_completed([sub_future, movie_future]), 1):
            try:
                if future is sub_future:
                    result.subtitles, warnings = future.result()
                    result.warnings.extend(warnings)
                else:
                    result.movies = future.result()
            except (ToolError, OSError) as exc:
                label = "字幕识别失败：" if future is sub_future else "电影弹幕搜索失败："
                result.warnings.append(label + str(exc))
            report(progress, "查找字幕和电影来源", count, 2, "项", complete=count == 2)
    if result.movies:
        chosen = result.movies[0]
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
        try:
            result.comments, result.danmaku_source, result.danmaku_url = fetch_public_danmaku(chosen, progress)
        except ToolError as exc:
            result.warnings.append(str(exc))
    else:
        result.warnings.append("没有找到匹配的电影弹幕来源。可修正片名后重新识别，或补选弹幕文件。")
    cache_danmaku(result, progress)
    return result


def cache_danmaku(result, progress=None):
    if result.workspace is None:
        result.workspace = local_workspace()
    if result.dm_ass is not None:
        save_new(result.workspace / "danmaku.ass", result.dm_ass.dumps(), progress, "缓存弹幕到本机")
    elif result.comments:
        text = json.dumps([{"time": c.time, "text": c.text, "color": c.color, "mode": c.mode}
                           for c in result.comments], ensure_ascii=False)
        save_new(result.workspace / "danmaku.json", text, progress, "缓存弹幕到本机")


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


def synthesize(result, subtitle_index=0, offset=0, density=6, duration=8, font_size=32, progress=lambda _: None,
               *, area=25, opacity=80, block_scroll=False, block_fixed=True, block_color=False,
               avoid_subtitles=True, deduplicate=True):
    if not result.subtitles or not 0 <= subtitle_index < len(result.subtitles):
        raise ToolError("还没有可合成的文字字幕。")
    if not result.comments and result.dm_ass is None:
        raise ToolError("还没有取得弹幕，暂时无法合成。")
    if result.workspace is None:
        result.workspace = local_workspace()
    choice = result.subtitles[subtitle_index]
    if choice.kind == "embedded" and choice.doc is None and file_signature(result.video) != result.signature:
        raise ToolError("影片在识别后发生了变化，请重新选择识别。")
    progress("正在本机准备原台词字幕…" if choice.kind != "embedded" or choice.doc is not None else "手动提取内封字幕（需要读取影片）…")
    length = finite(result.metadata.get("format", {}).get("duration", 0) or 0, "影片时长")
    base = materialize_subtitle(result.subtitles[subtitle_index], result.video, progress=progress, duration=length)
    save_new(result.workspace / "selected-subtitle.ass", base.dumps(), progress, "缓存所选字幕到本机")
    progress("正在排列弹幕并合并台词…")
    if result.dm_ass is not None:
        dm = copy.deepcopy(result.dm_ass)
        shift_events(dm, offset, is_danmaku=True)
        filtered = 0
    else:
        shift = finite(offset, "弹幕偏移")
        comments = [c for c in result.comments if not length or c.time + shift < length]
        dm, filtered = render_comments(comments, base.resolution, offset, density, duration, font_size, progress=progress,
                                       area=area, opacity=opacity, block_scroll=block_scroll, block_fixed=block_fixed,
                                       block_color=block_color, avoid_subtitles=avoid_subtitles, deduplicate=deduplicate)
        filtered += len(result.comments) - len(comments)
    progress("合并台词和弹幕")
    final = merge_ass(base, dm)
    target = result.video.with_name(f"弹幕版-{result.video.stem}.ass")
    output = save_new(result.workspace / target.name, final.dumps(), progress, "在本机保存合成字幕")
    value = {"output": str(output), "local_output": str(output), "video": str(result.video),
             "signature": result.signature, "target": str(target), "subtitle_lines": len(base.events),
             "danmaku_lines": len(dm.events), "filtered": filtered}
    return publish_cached(value, progress)


def publish_cached(value, progress=lambda _: None):
    value = dict(value, saved=False, write_error="")
    value["output"] = value["local_output"]
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
    return value


def copy_to_video_dir(source, target, progress=None):
    total = source.stat().st_size
    message = "写回影片原目录"
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
                        raise OSError("NAS 写入中断。")
                    offset += count
                    copied += count
                    report(progress, message, copied, total, "字节")
            if copied != total:
                raise ToolError("本地合成文件大小发生变化，未发布到 NAS。")
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


class DanmakuSettingsDialog:
    def __init__(self, app):
        self.app = app
        self.window = tk.Toplevel(app.root)
        self.window.title("弹幕设置")
        self.window.resizable(False, False)
        frame = ttk.Frame(self.window, padding=16)
        frame.pack(fill="both", expand=True)
        current = app.render_settings
        self.variables = {key: tk.BooleanVar(value=current[key]) for key in
                          ("block_scroll", "block_fixed", "block_color", "avoid_subtitles", "deduplicate")}
        self.variables.update({key: tk.DoubleVar(value=current[key]) for key in ("area", "opacity", "font_size")})
        self.variables["speed"] = tk.DoubleVar(value=800 / current["duration"])
        self.variables["density"] = tk.StringVar(value=app.density.get())
        self.labels = {}
        ttk.Label(frame, text="弹幕设置", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text="应用后重新合成，电视加载新生成的字幕即可。", foreground="#606975").pack(anchor="w", pady=(2, 10))
        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Label(row, text="屏蔽类型").pack(side="left", padx=(0, 10))
        for key, label in (("block_scroll", "滚动"), ("block_fixed", "固定"), ("block_color", "彩色")):
            ttk.Checkbutton(row, text=label, variable=self.variables[key], command=self.preview).pack(side="left", padx=8)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(6, 10))
        for key, label in (("avoid_subtitles", "防挡字幕（保留底部 32%）"), ("deduplicate", "过滤重复弹幕")):
            ttk.Checkbutton(row, text=label, variable=self.variables[key], command=self.preview).pack(side="left", padx=(0, 12))
        for key, label, low, high in (("area", "显示区域", 10, 100), ("opacity", "不透明度", 10, 100),
                                      ("font_size", "弹幕字号", 16, 64), ("speed", "弹幕速度", 50, 200)):
            row = ttk.Frame(frame)
            row.pack(fill="x", pady=5)
            ttk.Label(row, text=label, width=10).pack(side="left")
            ttk.Scale(row, from_=low, to=high, variable=self.variables[key], command=lambda _: self.preview()).pack(side="left", fill="x", expand=True, padx=10)
            self.labels[key] = tk.StringVar()
            ttk.Label(row, textvariable=self.labels[key], width=17).pack(side="left")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(6, 10))
        ttk.Label(row, text="同屏最多", width=10).pack(side="left")
        ttk.Spinbox(row, from_=1, to=30, width=5, textvariable=self.variables["density"], command=self.preview).pack(side="left", padx=10)
        ttk.Label(row, text="条；区域放不下时自动减少", foreground="#606975").pack(side="left")
        self.canvas = tk.Canvas(frame, width=512, height=288, background="#111c29", highlightthickness=0)
        self.canvas.pack(pady=(0, 6))
        self.note = tk.StringVar()
        ttk.Label(frame, textvariable=self.note, foreground="#606975", wraplength=512).pack(anchor="w")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(12, 0))
        ttk.Button(row, text="恢复默认", command=self.reset).pack(side="left")
        ttk.Button(row, text="应用设置", command=self.apply).pack(side="right")
        ttk.Button(row, text="取消", command=self.window.destroy).pack(side="right", padx=6)
        self.preview()

    def values(self):
        settings = {key: self.variables[key].get() for key in DM_DEFAULTS if key != "duration"}
        for key in ("area", "opacity", "font_size"):
            settings[key] = round(settings[key])
        settings["duration"] = 800 / finite(self.variables["speed"].get(), "速度")
        density = int(self.variables["density"].get())
        return settings, density

    def preview(self):
        if not hasattr(self, "canvas"):
            return
        try:
            settings, density = self.values()
            effective = min(settings["area"], 68 if settings["avoid_subtitles"] else 100)
            self.labels["area"].set(f"顶部 {settings['area']}%")
            self.labels["opacity"].set(f"{settings['opacity']}%")
            self.labels["font_size"].set(f"{settings['font_size']}（1080p）")
            self.labels["speed"].set(f"{8 / settings['duration']:.2g}× / {settings['duration']:.1f} 秒")
            canvas = self.canvas
            canvas.delete("all")
            canvas.create_rectangle(0, 0, 512, 288 * effective / 100, fill="#203e50", outline="")
            canvas.create_line(0, 288 * effective / 100, 512, 288 * effective / 100, fill="#41b6cf", dash=(4, 4))
            canvas.create_text(256, 170, text="电影画面", fill="#59697a", font=("Microsoft YaHei UI", 20))
            canvas.create_text(256, 265, text="原台词字幕 · 样式保持不变", fill="white", font=("Microsoft YaHei UI", -13))
            sample = [Comment(0, "上方滚动弹幕示意"), Comment(0, "弹幕字号与区域比例", 0x7ADDEF),
                      Comment(0, "顶部固定弹幕", mode=5), Comment(0, "底部固定弹幕（仍在显示区域内）", mode=4)]
            doc, _ = render_comments(sample, (1920, 1080), density=density, **settings)
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
            if key != "duration":
                self.variables[key].set(value)
        self.variables["speed"].set(100)
        self.variables["density"].set("6")
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
            render_comments(samples, (1920, 1080), density=density, **settings)
        except (ToolError, ValueError, tk.TclError, ZeroDivisionError) as exc:
            self.note.set("请检查设置：" + str(exc))
            return
        self.app.render_settings = settings
        self.app.density.set(str(density))
        self.app.settings_summary.set(self.app.settings_description())
        self.app.status.set("弹幕设置已应用；点击确认合成后生效，无需重新下载。")
        self.window.destroy()


class App:
    def __init__(self, root, initial=""):
        self.root = root
        root.title("字幕＋弹幕 · 一键合成 v" + VERSION)
        root.geometry("900x700")
        root.minsize(860, 680)
        self.result = None
        self.pending_output = None
        self.busy = False
        self.tasks = queue.Queue()
        self.active_progress = None
        self.task_started = self.step_started = 0.0
        self.path = tk.StringVar(value=initial)
        self.title = tk.StringVar()
        self.identity_text = tk.StringVar(value="选择影片后自动识别")
        self.subtitle = tk.StringVar()
        self.movie = tk.StringVar()
        self.dm_text = tk.StringVar(value="等待选择影片")
        self.status = tk.StringVar(value="选影片 → 自动查找 → 核对字幕与弹幕 → 确认合成")
        self.progress_text = tk.StringVar(value="进度按当前步骤计算；切换步骤时归零")
        self.offset = tk.StringVar(value="0")
        self.density = tk.StringVar(value="6")
        self.render_settings = dict(DM_DEFAULTS)
        self.platform = tk.StringVar(value="自动")
        self.platform_keys = [None]
        self.settings_dialog = None
        self.settings_summary = tk.StringVar(value=self.settings_description())
        self.output_text = tk.StringVar(value="输出到：所选影片的原目录")
        style = ttk.Style(root)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", padding=(8, 4))
        p = ttk.Frame(root, padding=12)
        p.pack(fill="both", expand=True)
        head = ttk.Frame(p)
        head.pack(fill="x")
        ttk.Label(head, text="字幕＋弹幕", style="Title.TLabel").pack(side="left")
        ttk.Button(head, text="使用说明", command=self.help).pack(side="right")
        ttk.Button(head, text="网页找字幕", command=self.subtitle_sites).pack(side="right", padx=6)
        ttk.Label(p, text="选择一部电影，找到台词和弹幕，合成一条字幕放回原目录。", foreground="#606975").pack(anchor="w", pady=(4, 10))
        row = ttk.Frame(p)
        row.pack(fill="x")
        self.path_entry = ttk.Entry(row, textvariable=self.path)
        self.path_entry.pack(side="left", fill="x", expand=True)
        self.path_entry.bind("<Return>", lambda _: self.scan())
        self.browse_button = ttk.Button(row, text="选择影片…", command=self.browse)
        self.browse_button.pack(side="left", padx=(8, 0))
        self.scan_button = ttk.Button(row, text="识别", command=self.scan)
        self.scan_button.pack(side="left", padx=(6, 0))
        row = ttk.Frame(p)
        row.pack(fill="x", pady=10)
        ttk.Label(row, text="识别片名", width=10).pack(side="left")
        self.title_entry = ttk.Entry(row, textvariable=self.title)
        self.title_entry.pack(side="left", fill="x", expand=True)
        self.retry_button = ttk.Button(row, text="按此片名重查", command=lambda: self.scan(override=True))
        self.retry_button.pack(side="left", padx=(8, 0))
        ttk.Label(p, textvariable=self.identity_text, foreground="#606975", wraplength=790).pack(anchor="w", pady=(0, 8))
        box = ttk.LabelFrame(p, text="找到的原台词字幕", padding=10)
        box.pack(fill="x", pady=5)
        self.sub_box = ttk.Combobox(box, state="readonly", textvariable=self.subtitle)
        self.sub_box.pack(side="left", fill="x", expand=True)
        self.manual_sub = ttk.Button(box, text="补选字幕…", command=self.pick_subtitle)
        self.manual_sub.pack(side="left", padx=(8, 0))
        self.embedded_button = ttk.Button(box, text="使用内封", command=self.use_embedded)
        self.embedded_button.pack(side="left", padx=(6, 0))
        box = ttk.LabelFrame(p, text="找到的电影弹幕", padding=10)
        box.pack(fill="x", pady=5)
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.movie_box = ttk.Combobox(row, state="readonly", textvariable=self.movie)
        self.movie_box.pack(side="left", fill="x", expand=True)
        self.movie_box.bind("<<ComboboxSelected>>", self.change_movie)
        ttk.Label(row, text="来源").pack(side="left", padx=(8, 4))
        self.platform_box = ttk.Combobox(row, state="disabled", textvariable=self.platform, values=["自动"], width=10)
        self.platform_box.pack(side="left")
        self.platform_box.bind("<<ComboboxSelected>>", self.change_platform)
        self.manual_dm = ttk.Button(row, text="补选弹幕…", command=self.pick_danmaku)
        self.manual_dm.pack(side="left", padx=(8, 0))
        ttk.Label(box, textvariable=self.dm_text, wraplength=760, foreground="#31566e").pack(anchor="w", pady=(8, 0))
        row = ttk.Frame(p)
        row.pack(fill="x", pady=10)
        ttk.Label(row, text="弹幕偏移（秒）").pack(side="left")
        ttk.Entry(row, textvariable=self.offset, width=8).pack(side="left", padx=6)
        ttk.Label(row, text="正数延后，负数提前", foreground="#606975").pack(side="left")
        ttk.Label(row, text="最多同屏").pack(side="left", padx=(22, 0))
        ttk.Spinbox(row, from_=1, to=30, textvariable=self.density, width=5).pack(side="left", padx=6)
        ttk.Label(row, text="条").pack(side="left")
        self.settings_button = ttk.Button(row, textvariable=self.settings_summary, command=self.open_settings)
        self.settings_button.pack(side="right")
        ttk.Label(p, textvariable=self.output_text, wraplength=790).pack(anchor="w", pady=(0, 8))
        row = ttk.Frame(p)
        row.pack(fill="x")
        self.generate_button = ttk.Button(row, text="确认合成 → 本机合成后写回影片目录", command=self.generate, state="disabled")
        self.generate_button.pack(side="left", fill="x", expand=True)
        self.retry_copy_button = ttk.Button(row, text="重试写回 NAS", command=self.retry_copy, state="disabled")
        self.retry_copy_button.pack(side="left", padx=(6, 0))
        self.progress_bar = ttk.Progressbar(p, mode="determinate", maximum=100)
        self.progress_bar.pack(fill="x", pady=(6, 6))
        ttk.Label(p, textvariable=self.status, wraplength=790).pack(anchor="w")
        ttk.Label(p, textvariable=self.progress_text, foreground="#606975", wraplength=790).pack(anchor="w")
        log_frame = ttk.Frame(p)
        log_frame.pack(fill="both", expand=True, pady=(6, 0))
        self.log_box = tk.Text(log_frame, height=4, wrap="word", state="disabled", font=("Microsoft YaHei UI", 9))
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=scrollbar.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.root.after(100, self.poll)
        if initial:
            self.root.after(200, self.scan)

    def settings_description(self):
        s = self.render_settings
        effective = min(s["area"], 68 if s["avoid_subtitles"] else 100)
        return f"弹幕设置 · 顶部 {effective}% / 字号 {s['font_size']}"

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
        text = tk.Text(win, wrap="word", padx=15, pady=15, font=("Microsoft YaHei UI", 10))
        text.pack(fill="both", expand=True)
        text.insert("1.0", HELP)
        text.configure(state="disabled")

    def subtitle_sites(self):
        title = self.title.get().strip()
        if not title and self.path.get().strip():
            title = filename_title(Path(self.path.get().strip().strip('"')).stem)[0]
        if not title:
            return messagebox.showinfo("先填片名", "请先选择影片，或在识别片名框填写电影名。")
        win = tk.Toplevel(self.root)
        win.title("字幕网站补选")
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)
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
        ready = not self.busy and self.result is not None and bool(self.result.subtitles) and bool(self.result.comments or self.result.dm_ass)
        self.generate_button.configure(state="normal" if ready else "disabled")
        self.retry_copy_button.configure(state="normal" if not self.busy and self.pending_output else "disabled")
        has_platforms = self.result is not None and bool(self.result.movies) and self.movie_box.current() >= 0
        self.platform_box.configure(state="readonly" if not self.busy and has_platforms else "disabled")
        self.settings_button.configure(state="disabled" if self.busy or (self.result and self.result.dm_ass) else "normal")

    def background(self, work, done):
        if self.busy:
            return
        self.set_busy(True)
        def run():
            try:
                self.tasks.put(("done", done, work()))
            except Exception as exc:
                self.tasks.put(("error", str(exc), None))
        threading.Thread(target=run, daemon=True).start()

    def progress(self, message):
        self.tasks.put(("progress", message, None))

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
                kind, first, second = self.tasks.get_nowait()
                if kind == "progress":
                    self.apply_progress(first)
                elif kind == "error":
                    if self.result is None:
                        self.subtitle.set("识别未完成，请检查路径后重试")
                        self.movie.set("识别未完成")
                        self.dm_text.set("尚未获取弹幕")
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
        if not path:
            return self.browse()
        title = self.title.get() if override else ""
        self.result = None
        self.pending_output = None
        self.sub_box.configure(values=[])
        self.movie_box.configure(values=[])
        self.platform_keys = [None]
        self.platform_box.configure(values=["自动"])
        self.platform.set("自动")
        self.subtitle.set("正在识别…")
        self.movie.set("正在查找…")
        self.dm_text.set("正在获取…")
        self.update_ready()
        def done(result):
            self.result = result
            self.path.set(str(result.video))
            self.title.set(result.identity["title"])
            length = float(result.metadata.get("format", {}).get("duration", 0) or 0)
            length_text = f"约 {length / 60:.1f} 分钟" if length else "未知"
            self.identity_text.set(f"{result.identity['source']}识别 · 年份：{result.identity['year'] or '未知'} · 片长：{length_text}")
            self.sub_box.configure(values=[c.label for c in result.subtitles])
            if result.subtitles:
                self.sub_box.current(0)
            else:
                self.subtitle.set("未找到可用文字字幕（见下方提示）")
            self.movie_box.configure(values=[f"{m['title']} · {m['year']} · {m['duration'] or '时长未知'}" for m in result.movies])
            if result.movies:
                self.movie_box.current(0)
            else:
                self.movie.set("未匹配到电影（可以修正片名重查）")
            self.refresh_platforms()
            self.show_danmaku()
            self.output_text.set("输出到：" + str(result.video.with_name(f"弹幕版-{result.video.stem}.ass")))
            for warning in result.warnings:
                self.log("提示：" + warning)
            self.log("本地缓存：" + str(result.workspace))
            self.status.set("已找到字幕和弹幕，请核对后确认合成。" if result.subtitles and (result.comments or result.dm_ass) else "识别结束，仍有缺失项，请查看提示。")
        self.background(lambda: scan_movie(path, title, self.progress), done)

    def show_danmaku(self):
        r = self.result
        if r and r.comments:
            snippets = " / ".join(c.text[:28].replace("\n", " ") for c in r.comments[:2])
            self.dm_text.set(f"已获取 {len(r.comments):,} 条 · {r.danmaku_source}\n预览：{snippets}")
        elif r and r.dm_ass:
            self.dm_text.set(f"已导入 {len(r.dm_ass.events)} 行 ASS 弹幕；保留原字号/密度，画布需与台词相同。")
            self.settings_summary.set("ASS 原有排版（仅偏移）")
        else:
            self.dm_text.set("尚未获取有效弹幕。可切换候选影片，或修正片名重查。")
        if not (r and r.dm_ass):
            self.settings_summary.set(self.settings_description())

    def refresh_platforms(self):
        selected = self.platform.get()
        index = self.movie_box.current()
        movie = self.result.movies[index] if self.result and 0 <= index < len(self.result.movies) else None
        self.platform_keys = [None] + [key for key in PLATFORMS if movie and key in movie["links"]]
        labels = ["自动"] + [PLATFORMS[key] for key in self.platform_keys[1:]]
        self.platform_box.configure(values=labels)
        self.platform_box.current(labels.index(selected) if selected in labels else 0)

    def change_platform(self, _=None):
        self.change_movie()

    def change_movie(self, _=None):
        index = self.movie_box.current()
        if self.busy or not self.result or not 0 <= index < len(self.result.movies):
            return
        self.refresh_platforms()
        platform = self.platform_keys[self.platform_box.current()]
        r = self.result
        r.comments, r.dm_ass = [], None
        r.danmaku_source, r.danmaku_url = "", ""
        self.show_danmaku()
        def done(value):
            r.comments, r.danmaku_source, r.danmaku_url = value
            self.show_danmaku()
            self.status.set("已切换弹幕来源，请核对后确认合成。")
        def work():
            value = fetch_public_danmaku(r.movies[index], self.progress, platform=platform)
            cached = copy.copy(r)
            cached.comments, cached.danmaku_source, cached.danmaku_url = value
            cache_danmaku(cached, self.progress)
            r.workspace = cached.workspace
            return value
        self.background(work, done)

    def use_embedded(self):
        if self.busy or self.result is None:
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
        if not self.result:
            return messagebox.showinfo("先选影片", "先选择影片并完成识别，再补选字幕。")
        path = filedialog.askopenfilename(title="补选原台词字幕", filetypes=[("文字字幕", "*.srt *.ass")])
        if path:
            try:
                if self.result.workspace is None:
                    self.result.workspace = local_workspace()
                choice = SubtitleChoice("手动补选 · " + Path(path).name, "file", 999, path)
                self.result.subtitles.insert(0, cache_subtitle(choice, self.result.workspace))
                self.sub_box.configure(values=[c.label for c in self.result.subtitles])
                self.sub_box.current(0)
                self.update_ready()
            except Exception as exc:
                messagebox.showerror("字幕不可用", str(exc))

    def pick_danmaku(self):
        if not self.result:
            return messagebox.showinfo("先选影片", "先选择影片并完成识别，再补选弹幕。")
        path = filedialog.askopenfilename(title="补选弹幕文件", filetypes=[("弹幕", "*.json *.xml *.ass")])
        if path:
            try:
                if Path(path).suffix.lower() == ".ass":
                    self.result.dm_ass = parse_ass(read_text(path))
                    self.result.comments = []
                else:
                    self.result.comments, _ = parse_comments(read_text(path))
                    self.result.dm_ass = None
                self.result.danmaku_source = "手动补选 · " + Path(path).name
                cache_danmaku(self.result)
                self.platform.set("手动导入")
                self.show_danmaku()
                self.update_ready()
            except Exception as exc:
                messagebox.showerror("弹幕不可用", str(exc))

    def generate(self):
        r = self.result
        if self.busy or r is None:
            return
        try:
            current = os.path.normcase(os.path.abspath(self.path.get().strip().strip('"')))
            if current != os.path.normcase(str(r.video)):
                raise ToolError("路径已经改变，请点击“识别”重新读取后再合成。")
            index = self.sub_box.current()
            offset, density = finite(self.offset.get()), int(self.density.get())
        except (ToolError, ValueError, OSError) as exc:
            return messagebox.showerror("请检查输入", str(exc))
        settings = dict(self.render_settings)
        self.background(lambda: synthesize(r, index, offset, density, progress=self.progress, **settings), self.show_output)

    def show_output(self, value):
        self.log(f"台词 {value['subtitle_lines']} 行，弹幕 {value['danmaku_lines']} 条；过滤/去重/限流 {value['filtered']} 条。")
        if value.get("saved", True):
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

    def retry_copy(self):
        if not self.busy and self.pending_output:
            value = self.pending_output
            self.background(lambda: publish_cached(value, self.progress), self.show_output)


def main(argv=None):
    parser = argparse.ArgumentParser(description="选择影片→自动找字幕和弹幕→确认合成到原目录。单文件，无需 API 密钥。")
    parser.add_argument("video", nargs="?", help="可选：本地/可访问 NAS 影片路径")
    args = parser.parse_args(argv)
    root = tk.Tk()
    App(root, args.video or "")
    root.mainloop()


if __name__ == "__main__":
    main()
