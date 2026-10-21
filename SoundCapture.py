# -*- coding: utf-8 -*-
"""
内录工作台 —— 电脑内部声音录制 / 音频处理工具

功能：
  录制页  系统内部声音（WASAPI 回环）或麦克风录音、实时双声道电平表、
          削波告警、定时自动停止、自动转 MP3
  文件页  录音库管理：刷新 / 播放 / 重命名 / 删除 / 打开所在文件夹
  工具页  格式转换、片段裁剪、淡入淡出、响度归一化、多文件拼接

不写注册表、不改系统设置，所有产物放在本目录下的「录音输出」。
"""

import ctypes
import datetime
import json
import math
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

try:  # 高分屏不糊
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass


def _dpi_factor():
    """布局缩放系数：96 DPI 记作 1.0，200% 缩放就是 2.0。

    Tk 的字号按 point 计，会跟着系统 DPI 自动放大；但布局里的像素值
    （窗口宽高、卡片宽度、padding）不会。两者不同步，高分屏上文字
    就会挤出卡片被截断。所以像素值必须乘上同一个系数。
    """
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
    except Exception:
        dpi = 96
    s = (dpi or 96) / 96.0
    if s < 1.05:
        return 1.0
    return min(4.0, round(s, 2))


S = _dpi_factor()


def P(x):
    """把按 1 倍屏设计的像素值换算成当前屏幕的像素值。"""
    return int(round(x * S))


import pyaudiowpatch as pyaudio

# ---------------------------------------------------------------- 常量

def _find_root():
    """定位软件根目录。

    直接跑 .py 时是脚本所在目录；打成 exe 后 __file__ 会落在
    dist\\SoundCapture\\ 里，此时向上找到含 SoundCapture.py 的一层，
    保证录音输出不会写进打包目录。
    """
    # 打包后 __file__ 指向 _internal，必须改用 exe 自身位置
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).resolve().parent
    else:
        here = Path(__file__).resolve().parent
    for cand in (here, here.parent, here.parent.parent, here.parent.parent.parent):
        if (cand / "SoundCapture.py").exists():
            return cand
    return here


APP_DIR = _find_root()
OUT_DIR = APP_DIR / "录音输出"
PROC_DIR = OUT_DIR / "处理输出"
OUT_DIR.mkdir(parents=True, exist_ok=True)

FFMPEG = (
    r"C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies"
    r"\python\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe"
)
FFMPEG = FFMPEG if Path(FFMPEG).exists() else "ffmpeg"

AUDIO_EXT = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma"}

BG = "#1b1d21"
PANEL = "#24272d"
CARD = "#2c3037"
FG = "#e6e8ea"
DIM = "#9aa0a6"
ACCENT = "#4ec9b0"
REC = "#e5534b"
LEVEL_OK = "#4ec9b0"
LEVEL_HOT = "#e2b341"
LEVEL_CLIP = "#e5534b"

FONT = ("Microsoft YaHei UI", 9)
FONT_B = ("Microsoft YaHei UI", 9, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 15, "bold")


def fmt_time(sec):
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:05.2f}" if h else f"{m:02d}:{s:05.2f}"


def short_path(p, keep=2):
    """把长路径压成 D:\\…\\父目录\\末级 的形式，避免把界面撑宽。"""
    p = Path(p)
    parts = [x for x in p.parts if x not in ("\\", "/")]
    if len(parts) <= keep + 1:
        return str(p)
    drive = parts[0].rstrip(":\\/")
    tail = parts[-keep:]
    return f"{drive}:\\…\\" + "\\".join(tail)


def fmt_size(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def describe_file(p):
    """按文件名规则说明这个文件录的是什么、属于哪一类。"""
    n = Path(p).name
    ext = Path(p).suffix.lower().lstrip(".")
    if "内部加麦克风" in n:
        src = "内部声音 + 麦克风（合成）"
    elif "麦克风" in n:
        src = "麦克风（外部声音）"
    elif "内部声音" in n:
        src = "电脑内部声音"
    else:
        src = "音频"
    if ext == "wav":
        kind = "无损母版"
    elif ext == "mp3":
        kind = "MP3 压缩副本"
    elif ext == "flac":
        kind = "FLAC 无损压缩"
    else:
        kind = ext.upper() or "文件"
    return f"{src} · {kind}"


def ff(*args):
    """调用 ffmpeg，返回 (returncode, stderr_tail)。"""
    try:
        r = subprocess.run(
            [FFMPEG, "-hide_banner", "-y", *args],
            capture_output=True, text=True, errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode, (r.stderr or "")[-500:]
    except FileNotFoundError:
        return -1, "找不到 ffmpeg"


_DUR_CACHE = {}      # (路径, 大小, mtime) -> 秒数


def probe_duration(path):
    """读取音频时长（秒）。wav 直接解析，其余交给 ffmpeg。

    结果按 (路径, 大小, mtime) 缓存：MP3 每探一次都要起一个 ffmpeg 进程，
    录音库里有几十个文件时能白白卡住界面好几秒。
    """
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return 0.0
    key = (str(p), st.st_size, int(st.st_mtime))
    hit = _DUR_CACHE.get(key)
    if hit is not None:
        return hit
    dur = 0.0
    if p.suffix.lower() == ".wav":
        try:
            with wave.open(str(p), "rb") as w:
                dur = w.getnframes() / float(w.getframerate() or 1)
        except Exception:
            dur = 0.0
    if dur <= 0:
        try:
            r = subprocess.run(
                [FFMPEG, "-hide_banner", "-i", str(p)],
                capture_output=True, text=True, errors="ignore",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", r.stderr or "")
            if m:
                dur = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                       + float(m.group(3)))
        except Exception:
            pass
    _DUR_CACHE[key] = dur
    return dur


def probe_level(path):
    """返回 (mean_volume, max_volume) dB。"""
    try:
        r = subprocess.run(
            [FFMPEG, "-hide_banner", "-i", str(path), "-af", "volumedetect",
             "-f", "null", "-"],
            capture_output=True, text=True, errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = r.stderr or ""
        mean = re.search(r"mean_volume:\s*([-\d.]+)", out)
        mx = re.search(r"max_volume:\s*([-\d.]+)", out)
        return (
            float(mean.group(1)) if mean else None,
            float(mx.group(1)) if mx else None,
        )
    except Exception:
        return None, None


# ---------------------------------------------------------------- 设备

# PortAudio 在进程内被反复 Initialize / Terminate 会直接段错误（实测 SIGSEGV），
# 所以整个进程只持有一个 PyAudio 实例、全程复用，只在需要重扫设备时才重建。
_PA = None
_PA_LOCK = threading.Lock()
# 打开失败过的设备（蓝牙断连 / 被别的程序独占），记住后不再反复试，
# 因为每试一次都要等好几秒，足以让一次短录制彻底落空。
# 按「设备名」记录打不开的设备。不能按编号记——编号每次会话都可能变，
# 会把好设备误判成坏的。蓝牙耳机断连后仍会留在系统列表里，每次都试一遍
# 要白白等十几秒，所以要持久化记住。
DEAD_DEVS = set()
DEAD_FILE = APP_DIR / "_设备黑名单.json"


def load_dead():
    try:
        if DEAD_FILE.exists():
            data = json.loads(DEAD_FILE.read_text(encoding="utf-8"))
            DEAD_DEVS.update(str(x) for x in data.get("names", []))
    except Exception:
        pass


def save_dead():
    try:
        DEAD_FILE.write_text(json.dumps({"names": sorted(DEAD_DEVS)},
                                        ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def force_reset_engine():
    """设备打开时卡死，会连引擎带锁一起霸占住，之后什么都录不了。

    这时只能整体丢弃重建：换一把新锁（旧的那把已经永远等不到），
    并尽力 terminate 掉旧实例（它也可能卡住，所以同样限时）。
    """
    global _PA, _PA_LOCK
    old = _PA
    _PA = None
    _PA_LOCK = threading.Lock()
    if old is not None:
        call_with_timeout(lambda: old.terminate(), 3.0)


def call_with_timeout(fn, timeout=5.0):
    """把可能永久卡住的音频调用放到守护线程里跑，超时就放弃。"""
    box = {}

    def work():
        try:
            box["v"] = fn()
        except Exception as e:
            box["e"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    return box.get("v"), box.get("e")


def get_pyaudio():
    global _PA
    with _PA_LOCK:
        if _PA is None:
            _PA = pyaudio.PyAudio()
        return _PA


def reset_pyaudio():
    """彻底重建音频引擎（用于插拔耳机后重新枚举设备）。只能在空闲时调用。"""
    global _PA
    with _PA_LOCK:
        if _PA is not None:
            try:
                _PA.terminate()
            except Exception:
                pass
            _PA = None


def scan_devices():
    """返回设备信息列表，kind 为 loopback（内部声音）/ mic（麦克风）。"""
    result = []
    p = get_pyaudio()
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out = None
        try:
            default_out = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
        except Exception:
            pass
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["hostApi"] != wasapi["index"] or d["maxInputChannels"] == 0:
                continue
            name = d["name"]
            lb = bool(d.get("isLoopbackDevice"))
            kind = "loopback" if lb else "mic"
            star = ""
            if lb and default_out and default_out["name"] in name:
                star = "  ★当前播放设备"
            result.append({
                "index": int(d["index"]),
                "name": name,
                "kind": kind,
                "label": ("系统内部声音" if lb else "麦克风") + " · " + name + star,
                "channels": min(int(d["maxInputChannels"]), 2),
                "rate": int(d["defaultSampleRate"]),
                "is_default": default_out is not None and lb and default_out["name"] in name,
            })
    except Exception:
        pass
    result.sort(key=lambda x: (x["kind"] != "loopback", not x["is_default"], x["name"]))
    return result


# ---------------------------------------------------------------- 录音线程

class Track:
    """一路采集：可以是电脑内部声音，也可以是麦克风。"""

    def __init__(self, dev):
        self.dev = dev
        self.name = dev["name"]
        self.channels = dev["channels"]
        self.rate = dev["rate"]
        self.frames = []
        self.got = 0
        self.peak = [0, 0]           # 立体声时 [L, R]，单声道时 [峰值, 0]
        self.bpf = max(1, dev["channels"]) * 2


class Recorder(threading.Thread):
    """支持 1~2 路同时采集。

    关键约束：**一个进程只能有一个 PyAudio 实例**，所以多轨共用同一个实例、
    开多条流（实测两个实例会静默崩溃）。
    """

    def __init__(self, devs, q, stop_evt, limit=None, mode="internal",
                 pause_evt=None):
        super().__init__(daemon=True)
        self.devs = list(devs)
        self.q = q
        self.stop_evt = stop_evt
        self.pause_evt = pause_evt if pause_evt is not None else threading.Event()
        self.limit = limit
        # 模式记在自己身上：结束回调可能延迟到达，不能去读 UI 的当前模式
        self.mode = mode
        self.tracks = [Track(d) for d in self.devs]
        self.elapsed = 0.0
        self.clips = 0
        self.error = None
        self.monitor_only = False
        self.dead_note = None      # 被迫换掉的设备名（首选打不开时）
        self.stuck_note = None     # 打开时卡住不返回的设备名
        self.opening_dev = None    # 正在打开哪个设备（看门狗用）
        self.paused = False        # 当前是否处于暂停
        self.pause_count = 0       # 暂停过几次（结束时回显给用户）

    def set_paused(self, flag):
        """暂停 / 继续。暂停期间不写入音频、不计入时长。

        计数放在采集循环里做（那里才真正知道状态翻转的时刻），
        这样不管是谁设置的暂停，统计都不会漏。
        """
        if flag:
            self.pause_evt.set()
        else:
            self.pause_evt.clear()

    @property
    def is_paused(self):
        return self.pause_evt.is_set()

    def _open_stream(self, p, tr):
        """打开一路采集。

        两件事必须处理好：
        1) PyAudio 的 open/read/close 并非线程安全，所有调用都要串行化，
           否则上一次录制的线程还在关流、下一次已经开流，引擎会直接卡死。
        2) 蓝牙耳机这类设备经常「在列表里但打不开」（已断连 / 被别的程序占用），
           所以要按顺序试候选设备，别让用户自己去一个个试。
        """
        cands = [tr.dev] + [d for d in tr.dev.get("candidates", [])
                            if d is not tr.dev and d["name"] not in DEAD_DEVS]
        for dev in cands:
            if dev["name"] in DEAD_DEVS:       # 已知打不开的直接跳过，别再等它
                continue
            self.opening_dev = dev["name"]     # 给看门狗看：现在正卡在哪个设备上
            rates = [dev["rate"]] + ([48000] if dev["rate"] != 48000 else [])
            stuck = False
            for rate in rates:
                st, timed_out = self._open_with_timeout(dev, rate, timeout=5.0)
                if st is not None:
                    tr.dev = dev
                    tr.name = dev["name"]
                    tr.channels = dev["channels"]
                    tr.rate = rate
                    tr.bpf = max(1, dev["channels"]) * 2
                    return st
                stuck = stuck or timed_out
                if timed_out:
                    # 卡死的设备会霸占引擎，只能重建；但重建后设备编号会变，
                    # 必须按设备名把剩下的候选重新定位一遍，否则又会打到空编号上。
                    force_reset_engine()
                    try:
                        fresh = {d["name"]: d for d in scan_devices()}
                        cands = [fresh.get(d["name"], d) for d in cands]
                    except Exception:
                        pass
            if not stuck:                 # 确实打不开才拉黑；卡住只是这次没响应
                DEAD_DEVS.add(dev["name"])
                save_dead()
                self.dead_note = dev["name"]
            else:
                self.stuck_note = dev["name"]
        raise RuntimeError(
            f"无法打开{tr.name}（可能已断开、或被其它程序独占）。\n"
            "请在左侧换一个设备，或关闭正在占用它的软件后重试。")

    @staticmethod
    def _open_with_timeout(dev, rate, timeout=6.0):
        """打开设备，返回 (流, 是否卡住)。

        **必须在当前线程里直接打开。** 实测把它塞进「再开一个线程」里做，
        这个调用会永久卡住不返回——PyAudio 的流是和调用线程绑在一起的。
        坏设备卡住时改由上层看门狗处理。
        """
        try:
            p = get_pyaudio()
            if not _PA_LOCK.acquire(timeout=8):
                return None, True
            try:
                st = p.open(format=pyaudio.paInt16, channels=dev["channels"],
                            rate=rate, input=True,
                            input_device_index=dev["index"],
                            frames_per_buffer=1024)
                return st, False
            finally:
                _PA_LOCK.release()
        except Exception:
            return None, False

    def run(self):
        import array
        streams = []
        # 计时分成两段：base = 之前各段累计的有效时长，seg_start = 本段起点。
        # 暂停时把当前段结算进 base 并停止累加，恢复时重开一段，
        # 这样成片时长里就不会混入暂停的空白。
        base = 0.0
        seg_start = time.time()
        paused = False
        try:
            p = get_pyaudio()          # 复用全局实例，绝不在这里 terminate
            for tr in self.tracks:
                streams.append(self._open_stream(p, tr))
            seg_start = time.time()    # 打开设备的等待时间不算进录音
            while not self.stop_evt.is_set():
                now = time.time()
                if self.pause_evt.is_set():
                    if not paused:
                        paused = True
                        self.paused = True
                        self.pause_count += 1
                        base += now - seg_start
                    # 暂停期间照样要把数据读走丢掉：WASAPI 的缓冲区不会无限积压，
                    # 写满之后会把旧数据往回灌——不读的话，恢复录制时暂停期间的
                    # 声音会被当成新数据录进去。
                    for tr, st in zip(self.tracks, streams):
                        try:
                            with _PA_LOCK:
                                avail = st.get_read_available()
                                if avail and avail > 0:
                                    st.read(avail, exception_on_overflow=False)
                        except Exception:
                            pass
                    self.elapsed = base
                    n_ch = 2 if len(self.tracks) == 1 else len(self.tracks)
                    self.q.put(([0] * n_ch, base))
                    time.sleep(0.02)
                    continue
                if paused:
                    paused = False
                    self.paused = False
                    seg_start = now
                read_any = False
                for tr, st in zip(self.tracks, streams):
                    # WASAPI 回环在「没有任何播放」时 read() 会永久阻塞，
                    # 所以先查可用帧数，有数据才读。
                    try:
                        with _PA_LOCK:
                            avail = st.get_read_available()
                            data = st.read(avail, exception_on_overflow=False) \
                                if (avail and avail > 0) else b""
                    except Exception:
                        avail, data = 0, b""
                    if avail and avail > 0:
                        n = len(data) // tr.bpf
                        tr.got += n
                        read_any = True
                        try:
                            a = array.array("h")
                            a.frombytes(data[:n * tr.bpf])
                            if tr.channels >= 2:
                                l = a[0::2]
                                r = a[1::2]
                                tr.peak = [max(abs(x) for x in l) if l else 0,
                                           max(abs(x) for x in r) if r else 0]
                            else:
                                tr.peak = [max(abs(x) for x in a) if a else 0, 0]
                        except Exception:
                            pass
                        if max(tr.peak) >= 32700:
                            self.clips += 1
                        if not self.monitor_only:
                            tr.frames.append(data)
                    else:
                        tr.peak = [0, 0]
                if not read_any:
                    time.sleep(0.005)

                el = base + (time.time() - seg_start)
                # 时钟对齐：静音期间不产数据，用零样本补齐时间轴，
                # 否则成片时长会短于真实录制时长（多轨各自独立对齐）。
                if not self.monitor_only:
                    for tr in self.tracks:
                        need = int(el * tr.rate) - tr.got
                        if need > 0:
                            tr.frames.append(b"\x00" * (need * tr.bpf))
                            tr.got += need
                self.elapsed = el
                # 上报给 UI：单轨立体声 → [L, R]；多轨 → 每轨一个峰值
                if len(self.tracks) == 1:
                    levels = self.tracks[0].peak
                else:
                    levels = [max(t.peak) for t in self.tracks]
                self.q.put((levels, el))
                if self.limit and el >= self.limit:
                    break
        except Exception as e:
            self.error = str(e)
        finally:
            for st in streams:
                try:
                    with _PA_LOCK:
                        st.stop_stream()
                        st.close()
                except Exception:
                    pass
            self.q.put(("__END__", self.error, self.elapsed, self))

    def save_track(self, idx, path):
        tr = self.tracks[idx]
        with wave.open(str(path), "wb") as w:
            w.setnchannels(tr.channels)
            w.setsampwidth(2)
            w.setframerate(tr.rate)
            w.writeframes(b"".join(tr.frames))
        return path


# ---------------------------------------------------------------- 电平表

class LevelMeter(tk.Canvas):
    """电平表：dB 刻度 + 峰值保持 + 削波指示。通道数可变（1~4）。"""

    def __init__(self, master, **kw):
        super().__init__(master, width=P(150), height=P(180), bg="#15171a",
                         highlightthickness=0, **kw)
        self.labels = ["L", "R"]
        self.values = [0.0, 0.0]
        self.peak_hold = [0.0, 0.0]
        self.hold_time = [0.0, 0.0]
        self.clip_on = False
        self.draw()   # 启动时先画出刻度和空轨道，否则是一块空白

    @staticmethod
    def to_db(v):
        if v <= 0:
            return -60.0
        return max(-60.0, 20 * math.log10(v / 32768.0))

    def set_channels(self, n, labels=None):
        n = max(1, int(n))
        if labels:
            self.labels = [str(x) for x in labels][:n]
        while len(self.labels) < n:
            self.labels.append(f"CH{len(self.labels) + 1}")
        self.labels = self.labels[:n]
        self.values = [0.0] * n
        self.peak_hold = [0.0] * n
        self.hold_time = [0.0] * n
        self.configure(width=P(max(96, 44 + n * 58)))
        self.draw()

    def set_values(self, values, clip=False):
        values = list(values)
        if len(values) != len(self.values):
            self.set_channels(len(values))
        self.values = values
        self.clip_on = clip
        now = time.time()
        for i, v in enumerate(values):
            if v >= self.peak_hold[i] or now - self.hold_time[i] > 1.2:
                self.peak_hold[i] = v
                self.hold_time[i] = now
        self.draw()

    def draw(self):
        self.delete("all")
        w = int(self["width"])
        h = int(self["height"])
        top, bottom = P(16), h - P(22)
        usable = bottom - top
        n = len(self.values)
        bar_w = P(26)
        gap = P(20)
        x0 = (w - (bar_w * n + gap * (n - 1))) // 2

        for db in (0, -6, -12, -24, -36, -48):
            y = top + usable * (db / -60.0)
            self.create_line(2, y, w - P(2), y, fill="#3a3f47")
            self.create_text(w - P(4), y, text=str(db), anchor="e",
                             fill=DIM, font=("Consolas", 7))

        for i in range(n):
            x = x0 + i * (bar_w + gap)
            db = self.to_db(self.values[i])
            frac = max(0.0, min(1.0, (db + 60) / 60))
            fh = usable * frac
            col = LEVEL_OK
            if db > -3:
                col = LEVEL_CLIP
            elif db > -10:
                col = LEVEL_HOT
            self.create_rectangle(x, top, x + bar_w, bottom, fill="#16181c", outline="")
            if fh > 0:
                self.create_rectangle(x, bottom - fh, x + bar_w, bottom,
                                      fill=col, outline="")
            ph = usable * max(0.0, min(1.0, (self.to_db(self.peak_hold[i]) + 60) / 60))
            self.create_line(x, bottom - ph, x + bar_w, bottom - ph,
                             fill="#ffffff", width=P(2))
            self.create_text(x + bar_w / 2, bottom + P(11), text=self.labels[i],
                             fill=DIM, font=FONT)

        if self.clip_on:
            self.create_text(w // 2, 7, text="CLIP", fill=REC,
                             font=("Consolas", 8, "bold"))


# ---------------------------------------------------------------- 主程序

class MiniBar(tk.Toplevel):
    """悬浮控制条：主窗口收起后它始终浮在最上层，可拖动，带状态/计时/迷你电平。

    用 overrideredirect 去掉标题栏和任务栏图标（悬浮条不需要），
    代价是失去系统级的移动/关闭——所以这里自己实现了拖动和三颗按钮。
    """

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("SoundCaptureMini")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg="#15171a", highlightthickness=P(1),
                       highlightbackground="#33383f")
        self.resizable(False, False)
        self._drag = None
        self._build()
        # 高度按实际内容算，别留大白边
        self.update_idletasks()
        w = P(336)
        h = max(P(96), self.winfo_reqheight())
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{sw - w - P(28)}+{sh - h - P(84)}")
        # 只给背景和文字绑拖动，按钮留给点击（否则会拖不动/点不中）
        for wdg in (self, self.bar, self.dot, self.lbl_state,
                    self.lbl_time, self.mini_meter):
            wdg.bind("<Button-1>", self._down)
            wdg.bind("<B1-Motion>", self._move)
            wdg.bind("<ButtonRelease-1>", self._up)

    def _build(self):
        self.bar = tk.Frame(self, bg="#15171a")
        self.bar.pack(fill="x", padx=P(12), pady=(P(10), P(2)))
        self.dot = tk.Label(self.bar, text="●", font=FONT_B, bg="#15171a", fg=DIM)
        self.dot.pack(side="left")
        self.lbl_state = tk.Label(self.bar, text="待机", font=FONT_B,
                                  bg="#15171a", fg=FG)
        self.lbl_state.pack(side="left", padx=(P(5), 0))
        self.lbl_time = tk.Label(self.bar, text="00:00.00",
                                 font=("Consolas", 17, "bold"),
                                 bg="#15171a", fg=FG)
        self.lbl_time.pack(side="right")

        self.mini_meter = tk.Canvas(self, height=P(9), bg="#0f1113",
                                    highlightthickness=0)
        self.mini_meter.pack(fill="x", padx=P(12), pady=(P(4), P(6)))
        self._bar = self.mini_meter.create_rectangle(
            0, 0, 0, P(9), fill=ACCENT, width=0)

        btns = tk.Frame(self, bg="#15171a")
        btns.pack(fill="x", padx=P(12), pady=(0, P(10)))
        # 这一颗是「主操作键」，随状态变脸：待机=开始录制，录制中=暂停，暂停中=继续。
        # 悬浮条宽度有限，分成三颗固定按钮会挤到放不下，所以复用同一个位置。
        self.btn_main = tk.Button(btns, text="▶ 开始", font=FONT, bg=ACCENT,
                                  fg="#0f1113", relief="flat", padx=P(12), pady=P(3),
                                  activebackground="#3fbf7f",
                                  command=self.app.mini_start)
        self.btn_main.pack(side="left")
        self.btn_stop = tk.Button(btns, text="■ 停止", font=FONT, bg=REC,
                                  fg="#ffffff", relief="flat", padx=P(12), pady=P(3),
                                  activebackground="#f0705f",
                                  command=self.app._stop)
        self.btn_stop.pack(side="left", padx=P(6))
        tk.Button(btns, text="⤢ 展开主窗口", font=FONT, bg="#3a4049", fg=FG,
                  relief="flat", padx=P(12), pady=P(3),
                  activebackground="#454b56",
                  command=self.app.restore_main).pack(side="right")

    # ---- 拖动

    def _down(self, e):
        self._drag = (e.x_root, e.y_root, self.winfo_x(), self.winfo_y())

    def _move(self, e):
        if not self._drag:
            return
        x0, y0, wx, wy = self._drag
        self.geometry(f"+{wx + (e.x_root - x0)}+{wy + (e.y_root - y0)}")

    def _up(self, e):
        self._drag = None

    # ---- 状态同步

    def sync(self, state, color, ttext, level, paused, can_pause, can_stop):
        self.dot.config(fg=color)
        self.lbl_state.config(text=state, fg=color)
        self.lbl_time.config(text=ttext)
        w = self.mini_meter.winfo_width()
        if w > 1:
            v = max(0.0, min(1.0, level))
            self.mini_meter.coords(self._bar, 0, 0,
                                   int(w * v) if v > 0.005 else 0, P(9))
        # 禁用态要连底色一起压暗，否则红底/灰底看着仍像能点
        if not can_stop:                      # 没在跑 —— 这颗键就是「开始录制」
            self.btn_main.config(text="▶ 开始", bg=ACCENT, fg="#0f1113",
                                 activebackground="#3fbf7f", state="normal",
                                 command=self.app.mini_start)
        elif can_pause:                       # 录制中 / 暂停中
            self.btn_main.config(text="▶ 继续" if paused else "‖ 暂停",
                                 bg="#c9892f" if paused else "#3a4049",
                                 fg="#ffffff" if paused else FG,
                                 activebackground="#d99a3f" if paused else "#454b56",
                                 state="normal", command=self.app.toggle_pause)
        else:                                 # 正在打开设备 / 纯监听，都不该按
            self.btn_main.config(text="打开设备…" if state.startswith("打开")
                                 else "● 监听中",
                                 bg="#2a2d33", fg="#7a8087", state="disabled",
                                 command=self.app.toggle_pause)
        self.btn_stop.config(
            state="normal" if can_stop else "disabled",
            bg=REC if can_stop else "#5a3a37")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("内录工作台 · SoundCapture")
        # 窗口尺寸按 DPI 放大，同时不超出屏幕可用区域
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(P(980), sw - P(30))
        h = min(P(720), sh - P(80))
        self.geometry(f"{w}x{h}+{P(20)}+{P(10)}")
        self.minsize(min(P(760), w), min(P(520), h))
        self.configure(bg=BG)

        self.devices = []
        self.loop_devs = []
        self.mic_devs = []
        self.rec_mode = "internal"
        self._first_data = False
        self.rec = None
        self.q = queue.Queue()
        self.stop_evt = threading.Event()
        self.pause_evt = threading.Event()
        self.monitor_mode = False
        self.clip_count = 0
        self.last_clip_flash = 0
        self.mini = None                # 悬浮控制条（最小化后的形态）
        self._last_level = 0.0          # 给悬浮条用的归一化电平
        self._done_msg = ""             # 结束后在悬浮条上短暂显示的一句话
        self._done_at = 0.0

        self._style()
        self._build()
        self.after(60, self._pump)

        # 点系统最小化按钮 → 收成悬浮条，而不是缩到任务栏
        self.bind("<Unmap>", self._on_unmap)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        load_dead()
        self.refresh_devices()
        self.refresh_library()

    # ---------------- 样式

    def _style(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure(".", background=BG, foreground=FG, font=FONT,
                    fieldbackground=CARD, borderwidth=0)
        s.configure("TNotebook", background=BG, tabmargins=[0, 4, 0, 0])
        s.configure("TNotebook.Tab", background=PANEL, foreground=DIM,
                    padding=[16, 7], font=FONT_B, borderwidth=0)
        s.map("TNotebook.Tab",
              background=[("selected", CARD)], foreground=[("selected", ACCENT)])
        s.configure("TFrame", background=BG)
        s.configure("Panel.TFrame", background=PANEL)
        s.configure("Card.TFrame", background=CARD)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Panel.TLabel", background=PANEL, foreground=FG)
        s.configure("Dim.TLabel", background=BG, foreground=DIM)
        s.configure("Panel.Dim.TLabel", background=PANEL, foreground=DIM)
        s.configure("TButton", background=CARD, foreground=FG, padding=[12, 6],
                    borderwidth=0, font=FONT)
        s.map("TButton", background=[("active", "#3a4049"), ("disabled", "#2a2d33")])
        s.configure("Accent.TButton", background=ACCENT, foreground="#101214")
        s.map("Accent.TButton", background=[("active", "#63dcc0")])
        s.configure("Rec.TButton", background=REC, foreground="#ffffff")
        s.map("Rec.TButton", background=[("active", "#f0705f")])
        s.configure("TCheckbutton", background=PANEL, foreground=FG)
        s.map("TCheckbutton", background=[("active", PANEL)])
        s.configure("TCombobox", fieldbackground=CARD, background=CARD,
                    foreground=FG, arrowcolor=FG, padding=[6, 4])
        # clam 主题会把 readonly 画成灰底灰字、disabled 反而正常，容易看反。
        # 这里显式指定：只读=深底亮字，禁用=更深底暗字。
        s.map("TCombobox",
              fieldbackground=[("readonly", CARD), ("disabled", "#22252a")],
              foreground=[("readonly", FG), ("disabled", "#5a6068")],
              background=[("readonly", CARD), ("disabled", "#22252a")],
              arrowcolor=[("readonly", FG), ("disabled", "#5a6068")],
              selectbackground=[("readonly", CARD)],
              selectforeground=[("readonly", FG)])
        s.map("TEntry",
              fieldbackground=[("disabled", "#22252a")],
              foreground=[("disabled", "#5a6068")])
        s.configure("Treeview", background=CARD, fieldbackground=CARD,
                    foreground=FG, rowheight=26, borderwidth=0)
        s.configure("Treeview.Heading", background=PANEL, foreground=DIM,
                    font=FONT_B, borderwidth=0)
        s.map("Treeview", background=[("selected", "#334a58")],
              foreground=[("selected", "#ffffff")])
        s.configure("TProgressbar", background=ACCENT, troughcolor=CARD, borderwidth=0)
        s.configure("Horizontal.TScale", background=PANEL, troughcolor=CARD)

    def _build(self):
        head = tk.Frame(self, bg=PANEL, height=P(56))
        head.pack(fill="x", side="top")
        head.pack_propagate(False)
        tk.Label(head, text="内录工作台", font=FONT_TITLE, bg=PANEL, fg=FG).pack(
            side="left", padx=P(20))
        tk.Label(head, text="SoundCapture · 电脑内部声音 / 麦克风录制与音频处理",
                 font=FONT, bg=PANEL, fg=DIM).pack(side="left", pady=(P(8), 0))
        self.status_var = tk.StringVar(value="就绪")
        tk.Label(head, textvariable=self.status_var, font=FONT, bg=PANEL,
                 fg=ACCENT).pack(side="right", padx=(P(4), P(20)))
        tk.Button(head, text="⤡ 悬浮窗", command=self.open_mini, bg="#3a4049",
                  fg=FG, relief="flat", font=FONT, padx=P(12), pady=P(4),
                  activebackground="#454b56").pack(side="right", padx=P(6))
        self.pin_var = tk.BooleanVar(value=False)
        tk.Checkbutton(head, text="⇱ 窗口置顶", variable=self.pin_var,
                       command=self._toggle_pin, bg=PANEL, fg=FG,
                       selectcolor="#2c3138", font=FONT, relief="flat", bd=0,
                       highlightthickness=0, activebackground=PANEL,
                       activeforeground="#101214", indicatoron=0,
                       padx=P(10), pady=P(4)).pack(side="right", padx=P(6))

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=P(12), pady=P(12))
        self._tab_record(nb)
        self._tab_library(nb)
        self._tab_tools(nb)

    # ---------------- 录制页

    def _scroll_left(self, parent, width):
        """左侧控制栏装进可滚动容器：内容再多也不会被窗口高度截掉。"""
        outer = tk.Frame(parent, bg=PANEL, width=width)
        outer.pack(side="left", fill="y")
        outer.pack_propagate(False)
        bar = ttk.Scrollbar(outer, orient="vertical")
        bar.pack(side="right", fill="y")
        canvas = tk.Canvas(outer, bg=PANEL, highlightthickness=0,
                           yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.config(command=canvas.yview)
        inner = tk.Frame(canvas, bg=PANEL)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(win, width=e.width))

        def _wheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        return inner

    def _tab_record(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="  录制  ")

        left = self._scroll_left(tab, P(340))
        right = tk.Frame(tab, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(P(10), 0))

        # 录制模式 —— 三种录法的入口
        box0 = tk.Frame(left, bg=CARD)
        box0.pack(fill="x", padx=P(10), pady=(P(8), P(6)))
        tk.Label(box0, text="录制模式", font=FONT_B, bg=CARD, fg=FG).pack(
            anchor="w", padx=P(12), pady=(P(10), P(4)))
        self.mode = tk.StringVar(value="internal")
        for val, txt in (("internal", "① 仅电脑内部声音"),
                         ("mic", "② 仅麦克风（外部声音）"),
                         ("both", "③ 内部声音 + 麦克风")):
            tk.Radiobutton(box0, text=txt, variable=self.mode, value=val,
                           bg=CARD, fg=FG, selectcolor="#1b1d21",
                           activebackground=CARD, activeforeground=ACCENT,
                           font=FONT, anchor="w", justify="left",
                           command=self._mode_changed).pack(
                fill="x", padx=P(10), pady=P(1))
        self.mode_hint = tk.StringVar(value="")
        tk.Label(box0, textvariable=self.mode_hint, bg=CARD, fg=DIM, font=FONT,
                 wraplength=P(296), justify="left").pack(
            anchor="w", padx=P(12), pady=(P(2), P(10)))

        # 设备
        box = tk.Frame(left, bg=CARD)
        box.pack(fill="x", padx=P(10), pady=P(6))
        tk.Label(box, text="输入设备", font=FONT_B, bg=CARD, fg=FG).pack(
            anchor="w", padx=P(12), pady=(P(10), P(4)))
        tk.Label(box, text="电脑内部声音（回环设备）", bg=CARD, fg=DIM,
                 font=FONT).pack(anchor="w", padx=P(12))
        self.dev_var = tk.StringVar()
        self.dev_combo = ttk.Combobox(box, textvariable=self.dev_var, state="readonly")
        self.dev_combo.pack(fill="x", padx=P(12), pady=(P(2), P(8)))
        self.dev_combo.bind("<<ComboboxSelected>>", lambda e: self._dev_changed())

        tk.Label(box, text="麦克风", bg=CARD, fg=DIM, font=FONT).pack(
            anchor="w", padx=P(12))
        self.mic_var = tk.StringVar()
        self.mic_combo = ttk.Combobox(box, textvariable=self.mic_var, state="readonly")
        self.mic_combo.pack(fill="x", padx=P(12), pady=(P(2), P(8)))
        self.mic_combo.bind("<<ComboboxSelected>>", lambda e: self._dev_changed())

        self.dev_info = tk.StringVar(value="")
        tk.Label(box, textvariable=self.dev_info, font=FONT, bg=CARD, fg=DIM,
                 justify="left", wraplength=P(290)).pack(
            anchor="w", padx=P(12), pady=(0, P(6)))
        tk.Button(box, text="刷新设备列表", command=self.refresh_devices,
                  bg="#3a4049", fg=FG, relief="flat", font=FONT,
                  activebackground="#454b56").pack(fill="x", padx=P(12), pady=(0, P(10)))

        # 选项
        box2 = tk.Frame(left, bg=CARD)
        box2.pack(fill="x", padx=P(10), pady=P(6))
        tk.Label(box2, text="录制选项", font=FONT_B, bg=CARD, fg=FG).pack(
            anchor="w", padx=P(12), pady=(P(10), P(6)))
        row = tk.Frame(box2, bg=CARD)
        row.pack(fill="x", padx=P(12), pady=P(2))
        self.auto_mp3 = tk.BooleanVar(value=True)
        tk.Checkbutton(row, text="生成 MP3 副本", variable=self.auto_mp3,
                       bg=CARD, fg=FG, selectcolor="#1b1d21", font=FONT,
                       activebackground=CARD, activeforeground=ACCENT,
                       relief="flat", bd=0, highlightthickness=0).pack(side="left")
        self.bitrate = tk.StringVar(value="192k")
        ttk.Combobox(row, textvariable=self.bitrate, width=7, state="readonly",
                     values=["128k", "192k", "256k", "320k"]).pack(side="right")
        row2 = tk.Frame(box2, bg=CARD)
        row2.pack(fill="x", padx=P(12), pady=(P(6), P(4)))
        self.use_timer = tk.BooleanVar(value=False)
        tk.Checkbutton(row2, text="定时停止", variable=self.use_timer,
                       bg=CARD, fg=FG, selectcolor="#1b1d21", font=FONT,
                       activebackground=CARD, activeforeground=ACCENT,
                       relief="flat", bd=0, highlightthickness=0).pack(side="left")
        self.timer_var = tk.StringVar(value="60")
        ttk.Entry(row2, textvariable=self.timer_var, width=5).pack(side="left", padx=P(8))
        tk.Label(row2, text="秒", bg=CARD, fg=DIM, font=FONT).pack(side="left")
        row3 = tk.Frame(box2, bg=CARD)
        row3.pack(fill="x", padx=P(12), pady=(0, P(10)))
        self.auto_mini = tk.BooleanVar(value=False)
        tk.Checkbutton(row3, text="开始录制后自动收成悬浮窗", variable=self.auto_mini,
                       bg=CARD, fg=FG, selectcolor="#1b1d21", font=FONT,
                       activebackground=CARD, activeforeground=ACCENT,
                       relief="flat", bd=0, highlightthickness=0).pack(side="left")

        # 输出
        box3 = tk.Frame(left, bg=CARD)
        box3.pack(fill="x", padx=P(10), pady=(P(6), P(10)))
        self.out_dir_real = str(OUT_DIR)
        self.out_var = tk.StringVar(value=OUT_DIR.name)
        head3 = tk.Frame(box3, bg=CARD)
        head3.pack(fill="x", padx=P(12), pady=P(8))
        tk.Label(head3, text="输出目录", font=FONT_B, bg=CARD, fg=FG).pack(side="left")
        tk.Button(head3, text="打开", command=self._open_outdir, bg="#3a4049", fg=FG,
                  relief="flat", font=FONT, padx=P(10)).pack(side="right")
        tk.Button(head3, text="更改", command=self._pick_outdir, bg="#3a4049", fg=FG,
                  relief="flat", font=FONT, padx=P(10)).pack(side="right", padx=P(6))
        tk.Label(box3, textvariable=self.out_var, bg=CARD, fg=DIM,
                 font=FONT, anchor="w").pack(fill="x", padx=P(12), pady=(0, P(8)))

        # 右侧：计时 + 电平
        top = tk.Frame(right, bg=PANEL)
        top.pack(fill="x")
        self.time_var = tk.StringVar(value="00:00.00")
        tk.Label(top, textvariable=self.time_var, font=("Consolas", 40, "bold"),
                 bg=PANEL, fg=FG).pack(side="left", padx=P(24), pady=P(14))
        info = tk.Frame(top, bg=PANEL)
        info.pack(side="left", pady=P(14))
        self.rec_state = tk.StringVar(value="● 待机")
        tk.Label(info, textvariable=self.rec_state, font=FONT_B, bg=PANEL,
                 fg=DIM).pack(anchor="w")
        self.clip_var = tk.StringVar(value="削波 0 次")
        tk.Label(info, textvariable=self.clip_var, font=FONT, bg=PANEL,
                 fg=DIM).pack(anchor="w")

        self.meter = LevelMeter(right)
        self.meter.pack(pady=P(10))
        tk.Label(right, text="电平表仅反映输入信号强度；系统内部声音为数字回环，"
                            "不受外界噪音影响",
                 font=FONT, bg=BG, fg=DIM).pack()

        btns = tk.Frame(right, bg=BG)
        btns.pack(pady=P(18))
        self.btn_rec = tk.Button(btns, text="●  开始录制",
                                 font=("Microsoft YaHei UI", 12, "bold"),
                                 bg=REC, fg="#fff", relief="flat", width=16, height=2,
                                 command=self.toggle_record, activebackground="#f0705f")
        self.btn_rec.pack(side="left", padx=P(8))
        self.btn_pause = tk.Button(btns, text="‖  暂停", font=FONT_B,
                                   bg="#3a4049", fg=FG, relief="flat",
                                   width=9, height=2, state="disabled",
                                   command=self.toggle_pause,
                                   disabledforeground="#5a6068",
                                   activebackground="#454b56")
        self.btn_pause.pack(side="left", padx=P(8))
        self.btn_mon = tk.Button(btns, text="监听电平", font=FONT_B, bg=CARD, fg=FG,
                                 relief="flat", width=12, height=2,
                                 command=self.toggle_monitor, activebackground="#3a4049")
        self.btn_mon.pack(side="left", padx=P(8))

        # 本次输出说明
        out_box = tk.Frame(right, bg=PANEL)
        out_box.pack(fill="both", expand=True, pady=(P(10), 0))
        tk.Label(out_box, text="本次输出的文件（分别是什么）", font=FONT_B,
                 bg=PANEL, fg=FG).pack(anchor="w", padx=P(14), pady=(P(10), P(4)))
        self.out_text = tk.Text(out_box, height=6, bg="#15171a", fg=FG,
                                font=("Consolas", 9), relief="flat",
                                insertbackground=FG, wrap="word")
        self.out_text.pack(fill="both", expand=True, padx=P(14), pady=(0, P(12)))
        self.out_text.insert("1.0", "录制结束后，这里会逐条列出本次产生的每个文件、"
                                    "它录的是什么、以及建议用途。\n")
        self.out_text.config(state="disabled")

    # ---------------- 文件页

    def _tab_library(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="  录音库  ")

        bar = tk.Frame(tab, bg=BG)
        bar.pack(fill="x", pady=(P(10), P(8)))
        self.lib_dir = tk.StringVar(value=str(OUT_DIR))
        tk.Label(bar, textvariable=self.lib_dir, bg=BG, fg=DIM, font=FONT).pack(side="left")
        for txt, cmd in (("刷新", self.refresh_library), ("播放", self.lib_play),
                         ("重命名", self.lib_rename), ("删除", self.lib_delete),
                         ("打开目录", self._open_outdir)):
            tk.Button(bar, text=txt, command=cmd, bg=CARD, fg=FG, relief="flat",
                      font=FONT, padx=P(12), pady=P(4),
                      activebackground="#3a4049").pack(side="right", padx=P(4))

        cols = ("name", "desc", "dur", "size", "mtime")
        wrap = tk.Frame(tab, bg=CARD)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                 selectmode="extended")
        heads = {"name": "文件名", "desc": "这个文件是什么", "dur": "时长",
                 "size": "大小", "mtime": "修改时间"}
        widths = {"name": P(330), "desc": P(240), "dur": P(110), "size": P(100),
                  "mtime": P(170)}
        for c in cols:
            self.tree.heading(c, text=heads[c])
            self.tree.column(c, width=widths[c], anchor="w")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.lib_play())

        self.lib_hint = tk.StringVar(value="双击播放 · 可多选后批量处理（见「工具」页）")
        tk.Label(tab, textvariable=self.lib_hint, bg=BG, fg=DIM, font=FONT).pack(
            anchor="w", pady=P(6))

    # ---------------- 工具页

    def _tab_tools(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="  工具  ")

        # 转换
        c1 = tk.Frame(tab, bg=CARD)
        c1.pack(fill="x", padx=P(2), pady=(P(12), P(8)))
        tk.Label(c1, text="格式转换（对录音库中选中的文件）", font=FONT_B, bg=CARD,
                 fg=FG).pack(anchor="w", padx=P(14), pady=(P(10), P(6)))
        r = tk.Frame(c1, bg=CARD)
        r.pack(fill="x", padx=P(14), pady=(0, P(10)))
        tk.Label(r, text="目标格式", bg=CARD, fg=FG, font=FONT).pack(side="left")
        self.tgt_fmt = tk.StringVar(value="mp3")
        ttk.Combobox(r, textvariable=self.tgt_fmt, width=8, state="readonly",
                     values=["mp3", "wav", "flac", "m4a", "ogg"]).pack(side="left", padx=P(8))
        tk.Label(r, text="比特率", bg=CARD, fg=FG, font=FONT).pack(side="left", padx=(P(16), 0))
        self.tgt_br = tk.StringVar(value="192k")
        ttk.Combobox(r, textvariable=self.tgt_br, width=8, state="readonly",
                     values=["128k", "192k", "256k", "320k"]).pack(side="left", padx=P(8))
        tk.Button(r, text="开始转换", command=self.tool_convert, bg=ACCENT,
                  fg="#101214", relief="flat", font=FONT_B, padx=P(16),
                  activebackground="#63dcc0").pack(side="right")

        # 裁剪 / 淡入淡出
        c2 = tk.Frame(tab, bg=CARD)
        c2.pack(fill="x", padx=P(2), pady=P(8))
        tk.Label(c2, text="片段裁剪与淡入淡出（对选中的第一个文件）", font=FONT_B,
                 bg=CARD, fg=FG).pack(anchor="w", padx=P(14), pady=(P(10), P(6)))
        r2 = tk.Frame(c2, bg=CARD)
        r2.pack(fill="x", padx=P(14))
        tk.Label(r2, text="起点 秒", bg=CARD, fg=FG, font=FONT).pack(side="left")
        self.cut_start = tk.StringVar(value="0")
        ttk.Entry(r2, textvariable=self.cut_start, width=8).pack(side="left", padx=P(6))
        tk.Label(r2, text="终点 秒（0=到结尾）", bg=CARD, fg=FG, font=FONT).pack(
            side="left", padx=(P(14), 0))
        self.cut_end = tk.StringVar(value="0")
        ttk.Entry(r2, textvariable=self.cut_end, width=8).pack(side="left", padx=P(6))
        r3 = tk.Frame(c2, bg=CARD)
        r3.pack(fill="x", padx=P(14), pady=(P(8), P(10)))
        tk.Label(r3, text="淡入 秒", bg=CARD, fg=FG, font=FONT).pack(side="left")
        self.fade_in = tk.StringVar(value="0")
        ttk.Entry(r3, textvariable=self.fade_in, width=6).pack(side="left", padx=P(6))
        tk.Label(r3, text="淡出 秒", bg=CARD, fg=FG, font=FONT).pack(side="left", padx=(P(14), 0))
        self.fade_out = tk.StringVar(value="0")
        ttk.Entry(r3, textvariable=self.fade_out, width=6).pack(side="left", padx=P(6))
        tk.Button(r3, text="应用裁剪/淡变", command=self.tool_cut, bg="#3a4049", fg=FG,
                  relief="flat", font=FONT, padx=P(14),
                  activebackground="#454b56").pack(side="right")

        # 归一化 / 合并 / 信息
        c3 = tk.Frame(tab, bg=CARD)
        c3.pack(fill="x", padx=P(2), pady=P(8))
        tk.Label(c3, text="批量处理", font=FONT_B, bg=CARD, fg=FG).pack(
            anchor="w", padx=P(14), pady=(P(10), P(6)))
        r4 = tk.Frame(c3, bg=CARD)
        r4.pack(fill="x", padx=P(14), pady=(0, P(12)))
        tk.Button(r4, text="响度归一化 (-16 LUFS)", command=self.tool_norm,
                  bg="#3a4049", fg=FG, relief="flat", font=FONT, padx=P(14),
                  activebackground="#454b56").pack(side="left")
        tk.Button(r4, text="按顺序拼接选中文件", command=self.tool_merge,
                  bg="#3a4049", fg=FG, relief="flat", font=FONT, padx=P(14),
                  activebackground="#454b56").pack(side="left", padx=P(8))
        tk.Button(r4, text="查看音频信息", command=self.tool_info,
                  bg="#3a4049", fg=FG, relief="flat", font=FONT, padx=P(14),
                  activebackground="#454b56").pack(side="left", padx=P(8))

        self.proc_log = tk.Text(tab, height=8, bg="#15171a", fg=FG, font=("Consolas", 9),
                                relief="flat", insertbackground=FG)
        self.proc_log.pack(fill="both", expand=True, padx=P(2), pady=(P(8), P(4)))
        self.proc_log.insert("1.0", "处理日志\n" + "-" * 60 + "\n")

    # ================= 录制控制

    def refresh_devices(self):
        if self.rec and self.rec.is_alive():
            messagebox.showinfo("正在录制", "请先停止录制，再刷新设备列表。")
            return
        reset_pyaudio()   # 重建音频引擎，这样刚插上的耳机也能扫到
        get_pyaudio()     # 必须在主线程建好，交给子线程建会导致打开设备卡死
        try:
            self.devices = scan_devices()
        except Exception as e:
            messagebox.showerror("设备枚举失败", str(e))
            return
        if not self.devices:
            messagebox.showwarning("无可用设备", "没有找到可用的 WASAPI 输入设备。")
            return
        self.loop_devs = [d for d in self.devices if d["kind"] == "loopback"]
        self.mic_devs = [d for d in self.devices if d["kind"] == "mic"]
        self._rebuild_candidates()
        self.dev_combo["values"] = [d["label"] for d in self.loop_devs] or ["（无内部声音设备）"]
        self.mic_combo["values"] = [d["label"] for d in self.mic_devs] or ["（无麦克风）"]
        if self.loop_devs:
            li = next((i for i, d in enumerate(self.loop_devs) if d["is_default"]), 0)
            self.dev_combo.current(li)
        if self.mic_devs:
            self.mic_combo.current(0)
        # 清掉选中高亮，否则文字会看起来被“洗白”了
        for cb in (self.dev_combo, self.mic_combo):
            try:
                cb.selection_clear()
            except Exception:
                pass
        self._mode_changed()
        self._dev_changed()

    def _rebuild_candidates(self):
        """同类型、且没被打不开记录排除的设备互为备胎。"""
        for group in (self.loop_devs, self.mic_devs):
            usable = [d for d in group if d["name"] not in DEAD_DEVS]
            for d in group:
                d["candidates"] = [x for x in usable if x is not d]

    def _mode_changed(self):
        m = self.mode.get()
        hints = {
            "internal": "只录电脑播放的声音，无环境噪音与人声",
            "mic": "只录麦克风外部声音，适合配音、口播",
            "both": "两路同录，各存独立音轨并合成成品",
        }
        self.mode_hint.set(hints.get(m, ""))
        self.dev_combo.config(state="readonly" if m in ("internal", "both") else "disabled")
        self.mic_combo.config(state="readonly" if m in ("mic", "both") else "disabled")
        # 电平表跟着模式换通道名：待机时别显示本次根本不会用到的通道
        try:
            labels = self._meter_labels()
            self.meter.set_channels(len(labels), labels)
        except Exception:
            pass

    def _dev_changed(self):
        parts = []
        i = self.dev_combo.current()
        if 0 <= i < len(self.loop_devs):
            d = self.loop_devs[i]
            parts.append(f"内部声音：{d['channels']} 声道 · {d['rate']} Hz")
        j = self.mic_combo.current()
        if 0 <= j < len(self.mic_devs):
            d = self.mic_devs[j]
            parts.append(f"麦克风：{d['channels']} 声道 · {d['rate']} Hz")
        self.dev_info.set(" · ".join(parts))
        self._mode_changed()      # 换设备后单/双声道可能变，电平表要跟着调

    def _cur_sources(self):
        """按当前模式返回要采集的设备列表（both 模式返回两路）。"""
        m = self.mode.get()
        srcs = []
        if m in ("internal", "both"):
            i = self.dev_combo.current()
            if 0 <= i < len(self.loop_devs):
                srcs.append(self.loop_devs[i])
        if m in ("mic", "both"):
            j = self.mic_combo.current()
            if 0 <= j < len(self.mic_devs):
                srcs.append(self.mic_devs[j])
        return srcs

    def _meter_labels(self):
        m = self.mode.get()
        if m == "both":
            return ["内部", "麦克风"]
        if m == "mic":
            return ["麦克风"]
        i = self.dev_combo.current()
        ch = self.loop_devs[i]["channels"] if 0 <= i < len(self.loop_devs) else 2
        return ["L", "R"] if ch >= 2 else ["单声道"]

    def _pick_outdir(self):
        d = filedialog.askdirectory(initialdir=self.out_dir_real)
        if d:
            self.out_dir_real = d
            self.out_var.set(Path(d).name or d)
            self.lib_dir.set(d)          # 录音库要的是真实路径，不能压缩
            self.refresh_library()

    def _open_outdir(self):
        try:
            os.startfile(self.out_dir_real)
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    # ---------------- 窗口形态：完整窗口 ⇄ 置顶悬浮条

    def _toggle_pin(self):
        self.attributes("-topmost", bool(self.pin_var.get()))

    def open_mini(self):
        """把主窗口收成一条始终置顶的悬浮控制条。"""
        if self.mini is not None:
            try:
                self.mini.deiconify()
                self.mini.lift()
                return
            except Exception:
                self.mini = None
        self.withdraw()
        self.mini = MiniBar(self)
        self._sync_status()

    def restore_main(self):
        """从悬浮条回到完整窗口（「展开主窗口」按钮 / 需要改设置时用它）。"""
        if self.mini is not None:
            try:
                self.mini.destroy()
            except Exception:
                pass
            self.mini = None
        try:
            self.deiconify()
        except Exception:
            pass
        self.lift()
        try:
            self.focus_force()
        except Exception:
            pass

    def _on_unmap(self, e):
        # 用户点了系统最小化按钮 —— 这里接管，改成收成悬浮条而不是缩到任务栏
        if e.widget is not self:
            return
        self.after(30, self._take_over_minimize)

    def _take_over_minimize(self):
        try:
            if self.state() == "iconic" and self.mini is None:
                self.deiconify()
                self.open_mini()
        except Exception:
            pass

    def _on_close(self):
        try:
            if (self.rec is not None and self.rec.is_alive()
                    and not getattr(self.rec, "monitor_only", False)):
                if not messagebox.askokcancel(
                        "退出", "正在录制，现在退出会丢掉这段还没保存的录音。\n确定退出吗？"):
                    return
                self.stop_evt.set()
                self.rec.join(timeout=2.5)
        except Exception:
            pass
        if self.mini is not None:
            try:
                self.mini.destroy()
            except Exception:
                pass
        self.destroy()

    def _sync_status(self):
        """每帧把状态同步到主窗口与悬浮条（开销可忽略）。"""
        rec = self.rec
        alive = bool(rec is not None and rec.is_alive())
        mon = getattr(rec, "monitor_only", False) if rec is not None else False
        paused = alive and self.pause_evt.is_set()
        opening = alive and not self._first_data
        if paused:
            state, color = "已暂停", "#e0a33a"
        elif opening:
            state, color = "打开设备…", "#e0a33a"
        elif alive and mon:
            state, color = "监听中（不保存）", ACCENT
        elif alive:
            state, color = "录制中", REC
        elif self._done_msg and time.time() - self._done_at < 5:
            state, color = self._done_msg, ACCENT
        else:
            state, color = "待机", DIM
        self.rec_state.set(("‖ " if paused else "● ") + state)
        self.status_var.set("已暂停" if paused else
                            ("打开设备…" if opening else
                             ("监听中" if (alive and mon) else
                              ("录制中" if alive else "就绪"))))
        can_pause = alive and not mon and not opening
        self.btn_pause.config(
            text="▶  继续" if paused else "‖  暂停",
            bg="#c9892f" if paused else "#3a4049",
            state="normal" if can_pause else "disabled")
        if self.mini is not None:
            try:
                self.mini.sync(state.split("（")[0], color, self.time_var.get(),
                               self._last_level, paused, can_pause, alive)
            except Exception:
                pass

    # ---------------- 录制控制

    def mini_start(self):
        """悬浮条上的「▶ 开始」：按主窗口当前选的模式和设备直接开录。"""
        if self.rec is not None and self.rec.is_alive():
            return                       # 已经在录了，交给暂停键处理
        self._start(monitor=False)

    def _alert(self, title, msg, kind="warning"):
        """主窗口收成悬浮条时它是隐藏的，messagebox 挂在上面会跟着看不见，
        所以弹提示之前先把主窗口放出来。"""
        if self.mini is not None:
            self.restore_main()
        fn = {"warning": messagebox.showwarning,
              "error": messagebox.showerror,
              "info": messagebox.showinfo}[kind]
        fn(title, msg)

    def toggle_pause(self):
        rec = self.rec
        if rec is None or not rec.is_alive():
            return
        if getattr(rec, "monitor_only", False):   # 监听不落地文件，没必要暂停
            return
        rec.set_paused(not self.pause_evt.is_set())
        self._sync_status()

    def toggle_record(self):
        if self.rec and self.rec.is_alive():
            self._stop()
        else:
            self._start(monitor=False)

    def toggle_monitor(self):
        if self.rec and self.rec.is_alive():
            self._stop()
        else:
            self._start(monitor=True)

    def _start(self, monitor):
        srcs = self._cur_sources()
        if not srcs:
            self._alert("无可用设备",
                        "当前录制模式下没有可用设备，请刷新设备列表或换一种模式。")
            return
        # 必须等上一次录制彻底收尾再开新的：否则新设备要排着队等它释放，
        # 打开耗时可能超过整段录制时间，最后得到 0 秒的空文件。
        prev = self.rec
        if prev is not None and prev.is_alive():
            self.stop_evt.set()
            prev.join(timeout=3.0)
            if prev.is_alive():
                self._alert("设备忙",
                            "上一次录制尚未结束，请稍等一两秒再开始。")
                return
        # 这里刻意不去重建音频引擎：重建之后设备编号会变，还拿旧编号去
        # 打开设备就会打到一个不存在的设备上，驱动层直接卡死不返回。
        # 引擎保持全程唯一，编号才始终对得上。
        limit = None
        if self.use_timer.get():
            try:
                limit = float(self.timer_var.get())
            except ValueError:
                self._alert("参数错误", "定时秒数必须是数字。")
                return
        # 注意：队列必须是同一个。每次开录都新建的话，上一次录制的
        # 「结束」消息会投进旧队列，永远没人处理，那次录音就白录了。
        self.stop_evt = threading.Event()
        self.pause_evt = threading.Event()      # set = 暂停（不写盘、不计时长）
        self.time_var.set("00:00.00")
        self._last_level = 0.0
        self._done_msg = ""
        self.clip_count = 0
        self.clip_var.set("削波 0 次")
        self.rec_mode = self.mode.get()
        self.rec = Recorder(srcs, self.q, self.stop_evt, limit,
                            mode=self.rec_mode, pause_evt=self.pause_evt)
        self.rec.monitor_only = monitor
        self.monitor_mode = monitor
        labels = self._meter_labels()
        self.meter.set_channels(len(labels), labels)
        self.rec.start()
        self._rec_started = time.time()
        self._first_data = False
        self.btn_rec.config(text="■  停止" if not monitor else "●  开始录制")
        self.btn_mon.config(text="停止监听" if monitor else "监听电平")
        self.dev_combo.config(state="disabled")
        self.mic_combo.config(state="disabled")
        self._sync_status()
        if monitor is False and self.auto_mini.get():
            # 录起来就自动收成悬浮条：讲课时屏幕上不该占着一大块窗口
            self.after(250, lambda: self.open_mini()
                       if (self.rec is not None and self.rec.is_alive()) else None)

    def _stop(self):
        if self.rec:
            self.stop_evt.set()
        self.btn_rec.config(text="●  开始录制")
        self.btn_mon.config(text="监听电平")
        self._mode_changed()

    def _pump(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "__END__":
                    err = item[1]
                    rec_obj = item[3] if len(item) > 3 else None
                    self._last_level = 0.0
                    self._on_finish(err, rec_obj)
                    self._sync_status()
                    break
                levels, el = item
                top = max(levels) if levels else 0
                if not self._first_data:      # 设备真正开始出数据了
                    self._first_data = True
                # 给悬浮条用的归一化电平（开根号是为了让小声音也看得见）
                self._last_level = min(1.0, (top / 32768.0) ** 0.5) if top else 0.0
                self.meter.set_values(levels, clip=(top >= 32700))
                self.time_var.set(fmt_time(el))
                if top >= 32700:
                    self.clip_count += 1
                    self.clip_var.set(f"削波 {self.clip_count} 次")
        except queue.Empty:
            pass

        # 看门狗：坏设备打开时会永久卡住，这里负责发现并把设备拉黑，
        # 免得用户每次点录制都再卡一次。
        if (self.rec is not None and self.rec.is_alive()
                and not self._first_data and not self.pause_evt.is_set()):
            d = getattr(self.rec, "opening_dev", None)
            if d:
                self.status_var.set(f"正在打开 {d[:22]} …")
            if time.time() - getattr(self, "_rec_started", time.time()) > 15:
                if d:
                    DEAD_DEVS.add(d)
                    save_dead()
                self.rec = None
                self.rec_state.set("● 待机")
                self.status_var.set("设备无响应")
                self.btn_rec.config(text="●  开始录制")
                self._mode_changed()
                self._alert(
                    "设备无响应",
                    f"打开「{d or '所选设备'}」超过 15 秒仍无响应（常见于已断开的蓝牙设备）。\n\n"
                    "已把它记入黑名单，下次自动跳过。请再点一次开始录制；"
                    "若反复出现，请改用其它设备。")
        self._sync_status()
        self.after(50, self._pump)

    def _show_outputs(self, produced, devices=None, mode=None, note=None):
        """在录制页列出本次产生的每个文件、录的是什么、建议用途。"""
        self.out_text.config(state="normal")
        self.out_text.delete("1.0", "end")
        existing = [Path(p) for p, _ in produced if p and Path(p).exists()]
        if existing:
            wav_n = sum(1 for p in existing if p.suffix.lower() == ".wav")
            mp3_n = sum(1 for p in existing if p.suffix.lower() == ".mp3")
            if mode == "both":
                head = (f"本次共输出 {len(existing)} 个文件：\n"
                        f"· {wav_n - 1} 条独立分轨（内部声音、麦克风，各录各的）\n"
                        f"· 1 条合成成品（两条分轨对齐后合在一起，直接听这个）\n")
            else:
                src = "电脑内部声音" if mode == "internal" else "麦克风外部声音"
                head = (f"本次共输出 {len(existing)} 个文件：\n"
                        f"· 1 个 WAV 母版（{src}，未压缩，用来做后期）\n")
            if mp3_n:
                head += (f"· {mp3_n} 个 MP3 副本（内容和对应的 WAV 完全一样，"
                         f"只是体积小、方便发出去）\n")
            if mode == "both":
                head += ("合成成品里已经包含了另外两条分轨的全部声音；"
                         "要单独调音量或降噪时，才回去用分轨。\n\n")
            else:
                head += ("WAV 和 MP3 是同一次录音的两种格式，不是两段不同的内容。\n\n")
            self.out_text.insert("end", head)
        if devices:
            self.out_text.insert("end", "本次实际使用的设备：" + "；".join(devices) + "\n")
        if note:
            self.out_text.insert("end", note + "\n")
        if devices or note:
            self.out_text.insert("end", "\n")
        for p, desc in produced:
            if not p or not Path(p).exists():
                continue
            p = Path(p)
            self.out_text.insert("end", f"● {p.name}\n", "name")
            self.out_text.insert("end", f"    {desc}\n")
            self.out_text.insert(
                "end",
                f"    {fmt_size(p.stat().st_size)} · 时长 {fmt_time(probe_duration(p))}\n"
                f"    {p}\n\n")
        self.out_text.tag_config("name", foreground=ACCENT)
        self.out_text.config(state="disabled")

    def _on_finish(self, err, rec=None):
        self.rec_state.set("● 待机")
        self.status_var.set("就绪")
        if rec is None:
            rec = self.rec
        if rec is self.rec:
            self.rec = None
        if err:
            self._done_msg, self._done_at = "出错", time.time()
            self._alert("录音出错", err, "error")
            return
        if not rec or getattr(rec, "monitor_only", False):
            return
        if not any(t.frames for t in rec.tracks):
            self._done_msg, self._done_at = "没有录到", time.time()
            self._alert(
                "没有录到内容",
                f"这次采集到 0 秒音频（录制时长 {rec.elapsed:.1f} 秒）。\n\n"
                "常见原因：\n"
                "· 刚切换过设备，设备打开耗时超过了录制时间——稍等两秒再录一次即可\n"
                "· 选错了设备（比如选了一个没在用的耳机）\n"
                "· 录内部声音时，电脑当时确实没有播放任何声音（这种情况也会留下等长的静音文件）",
                "info")
            return

        outdir = Path(self.out_dir_real)
        outdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        mode = getattr(rec, "mode", self.rec_mode)
        produced = []
        master = None
        try:
            if mode == "internal":
                master = outdir / f"录音_{stamp}_内部声音.wav"
                rec.save_track(0, master)
                produced.append((master,
                                 "电脑内部声音 · 无损母版（WAV 未压缩）\n"
                                 "    录的是电脑播放的所有声音；留着它做后期，"
                                 "反复编辑也不会像 MP3 那样越转越糊"))
            elif mode == "mic":
                master = outdir / f"录音_{stamp}_麦克风.wav"
                rec.save_track(0, master)
                produced.append((master,
                                 "麦克风外部环境声 · 无损母版（WAV 未压缩）\n"
                                 "    录的是麦克风收到的人声/环境声，不含电脑播放的声音"))
            else:
                w_in = outdir / f"录音_{stamp}_内部声音.wav"
                w_mic = outdir / f"录音_{stamp}_麦克风.wav"
                rec.save_track(0, w_in)
                rec.save_track(1, w_mic)
                r0 = rec.tracks[0].rate
                # 两路采样率常常不同（内部 48k / 麦克风 16k），先统一再合成
                # 注意：重采样只写临时文件，麦克风母版 w_mic 保持原始采样率不动
                mic_for_mix = w_mic
                if rec.tracks[1].rate != r0:
                    rs = outdir / f"_临时_混音用麦克风_{stamp}.wav"
                    ff("-i", str(w_mic), "-ar", str(r0), "-ac", "2", str(rs))
                    if rs.exists():
                        mic_for_mix = rs
                mix = outdir / f"录音_{stamp}_内部加麦克风.wav"
                rc, _ = ff("-i", str(w_in), "-i", str(mic_for_mix), "-filter_complex",
                           "amix=inputs=2:duration=longest:normalize=0,"
                           "alimiter=limit=0.9", "-ar", str(r0), "-ac", "2", str(mix))
                if not mix.exists():  # 老版本 ffmpeg 可能没有 alimiter
                    ff("-i", str(w_in), "-i", str(mic_for_mix), "-filter_complex",
                       "amix=inputs=2:duration=longest:normalize=0",
                       "-ar", str(r0), "-ac", "2", str(mix))
                # 混音用完就删临时文件，别留在录音目录里干扰
                if mic_for_mix is not w_mic:
                    try:
                        mic_for_mix.unlink()
                    except Exception:
                        pass
                produced.append((w_in, f"【分轨 1】电脑内部声音 · 独立音轨（WAV, {r0} Hz）\n"
                                      "    只有电脑播放的声音，没有人声；可单独调音量"))
                produced.append((w_mic, f"【分轨 2】麦克风 · 独立音轨（WAV, {rec.tracks[1].rate} Hz）\n"
                                       "    只有麦克风的人声/环境声，保持麦克风原始采样率；"
                                       "可单独降噪或替换"))
                if mix.exists():
                    produced.append((mix, "【合成】内部声音 + 麦克风 已合在一起（WAV）\n"
                                          "    两轨对齐后的成品，直接听这一版；"
                                          "需要单独调音量时用上面两条分轨"))
                    master = mix
                else:
                    master = w_in
        except Exception as e:
            self._alert("保存失败", str(e), "error")
            return

        if master and master.exists():
            self.status_var.set(f"已保存 {master.name}")
            if self.auto_mp3.get():
                mp3 = master.with_suffix(".mp3")
                mp3 = master.parent / (master.stem + ".mp3")
                ff("-i", str(master), "-codec:a", "libmp3lame",
                   "-b:a", self.bitrate.get(), str(mp3))
                if mp3.exists():
                    produced.append((mp3, f"【压缩副本】上面那一版成片的 MP3（{self.bitrate.get()}）\n"
                                          f"    内容与 {master.name} 完全相同，"
                                          "只是压缩过：体积约为 WAV 的十分之一，"
                                          "方便发微信、上传平台；不再做二次编辑"))
        used = [f"{t.name}（{t.rate} Hz）" for t in rec.tracks]
        chosen = [d["index"] for d in rec.devs]
        switched = [t for t in rec.tracks
                    if t.dev is not None and t.dev["index"] not in chosen]
        if switched:
            used.append("注意：原选设备打不开，已自动改用上面列出的设备")
        note = None
        if getattr(rec, "pause_count", 0):
            note = (f"录制途中暂停过 {rec.pause_count} 次：暂停期间的声音没有录入，"
                    f"时间也没计入，成片是连续的 {fmt_time(rec.elapsed)}，中间没有空档。")
        self._done_msg, self._done_at = "已保存", time.time()
        self._show_outputs(produced, used, mode, note)
        self.refresh_library()

    # ================= 录音库

    def refresh_library(self):
        d = Path(self.lib_dir.get())
        self.tree.delete(*self.tree.get_children())
        if not d.exists():
            return
        files = [p for p in d.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXT]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        # 时长先留空，逐个补：MP3 要起 ffmpeg 才能算出时长，
        # 整目录一次性算完会让「停止录制」后的界面卡住好几秒。
        self._dur_queue = []
        for p in files:
            st = p.stat()
            iid = str(p)
            self.tree.insert("", "end", iid=iid, values=(
                p.name,
                describe_file(p),
                "…",
                fmt_size(st.st_size),
                datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            ))
            self._dur_queue.append((iid, p))
        self.lib_hint.set(f"共 {len(files)} 个音频文件 · 双击播放 · 可多选批量处理")
        self.after(30, self._fill_durations)

    def _fill_durations(self):
        """一次只算一个时长，算完让出主循环，列表始终可点。"""
        q = getattr(self, "_dur_queue", None)
        if not q:
            return
        iid, p = q.pop(0)
        try:
            if self.tree.exists(iid):
                self.tree.set(iid, "dur", fmt_time(probe_duration(p)))
        except Exception:
            pass
        if q:
            self.after(1, self._fill_durations)

    def _selected(self):
        return [Path(i) for i in self.tree.selection()]

    def lib_play(self):
        sel = self._selected()
        if not sel:
            messagebox.showinfo("提示", "请先选中一个文件。")
            return
        try:
            os.startfile(str(sel[0]))
        except Exception as e:
            messagebox.showerror("播放失败", str(e))

    def lib_rename(self):
        sel = self._selected()
        if not sel:
            return
        p = sel[0]
        new = simpledialog.askstring("重命名", "新的文件名（含扩展名）：",
                                     initialvalue=p.name, parent=self)
        if not new:
            return
        try:
            p.rename(p.parent / new)
            self.refresh_library()
        except Exception as e:
            messagebox.showerror("重命名失败", str(e))

    def lib_delete(self):
        sel = self._selected()
        if not sel:
            return
        if not messagebox.askyesno("确认删除",
                                   f"将删除 {len(sel)} 个文件，且不可恢复。继续？"):
            return
        for p in sel:
            try:
                p.unlink()
            except Exception as e:
                messagebox.showerror("删除失败", f"{p.name}: {e}")
        self.refresh_library()

    # ================= 工具

    def _log(self, msg):
        self.proc_log.insert("end", msg + "\n")
        self.proc_log.see("end")

    def _run_async(self, fn):
        def wrap():
            try:
                fn()
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("出错", str(e)))
        threading.Thread(target=wrap, daemon=True).start()

    def tool_convert(self):
        sel = self._selected()
        if not sel:
            messagebox.showinfo("提示", "请先在「录音库」中选择文件。")
            return
        tgt = self.tgt_fmt.get()
        br = self.tgt_br.get()

        def work():
            PROC_DIR.mkdir(parents=True, exist_ok=True)
            for p in sel:
                out = PROC_DIR / f"{p.stem}.{tgt}"
                args = ["-i", str(p)]
                if tgt == "mp3":
                    args += ["-codec:a", "libmp3lame", "-b:a", br]
                elif tgt == "flac":
                    args += ["-codec:a", "flac"]
                elif tgt == "m4a":
                    args += ["-codec:a", "aac", "-b:a", br]
                elif tgt == "ogg":
                    args += ["-codec:a", "libvorbis", "-b:a", br]
                args.append(str(out))
                rc, tail = ff(*args)
                self.after(0, lambda o=out, c=rc, t=tail:
                           self._log(f"{'OK  ' if c == 0 else 'FAIL'} {o.name}"
                                     + ("" if c == 0 else f"  {t[:160]}")))
            self.after(0, self.refresh_library)
        self._run_async(work)

    def tool_cut(self):
        sel = self._selected()
        if not sel:
            messagebox.showinfo("提示", "请先在「录音库」中选择文件。")
            return
        p = sel[0]
        try:
            s = float(self.cut_start.get())
            e = float(self.cut_end.get())
            fi = float(self.fade_in.get())
            fo = float(self.fade_out.get())
        except ValueError:
            messagebox.showwarning("参数错误", "时间参数必须是数字。")
            return
        dur = probe_duration(p)
        end = e if e > 0 else dur
        if end <= s:
            messagebox.showwarning("参数错误", "终点必须晚于起点。")
            return

        def work():
            PROC_DIR.mkdir(parents=True, exist_ok=True)
            out = PROC_DIR / f"{p.stem}_剪辑{p.suffix}"
            filters = []
            if fi > 0:
                filters.append(f"afade=t=in:ss=0:d={fi}")
            if fo > 0:
                filters.append(f"afade=t=out:st={max(0, (end - s) - fo)}:d={fo}")
            args = ["-ss", str(s), "-t", str(end - s), "-i", str(p)]
            if filters:
                args += ["-af", ",".join(filters)]
            args += ["-codec:a", "libmp3lame", "-b:a", "192k"] if p.suffix.lower() == ".mp3" \
                else []
            args.append(str(out))
            rc, tail = ff(*args)
            self.after(0, lambda: self._log(
                f"{'OK  ' if rc == 0 else 'FAIL'} {out.name}  "
                f"裁剪 {s:.2f}s→{end:.2f}s 淡入{fi}s 淡出{fo}s"))
            self.after(0, self.refresh_library)
        self._run_async(work)

    def tool_norm(self):
        sel = self._selected()
        if not sel:
            messagebox.showinfo("提示", "请先在「录音库」中选择文件。")
            return

        def work():
            PROC_DIR.mkdir(parents=True, exist_ok=True)
            for p in sel:
                out = PROC_DIR / f"{p.stem}_归一{p.suffix}"
                rc, _ = ff("-i", str(p), "-af",
                           "loudnorm=I=-16:TP=-1.5:LRA=11",
                           "-ar", "48000", str(out))
                mean, mx = probe_level(out)
                self.after(0, lambda o=out, c=rc, m=mean:
                           self._log(f"{'OK  ' if c == 0 else 'FAIL'} {o.name}"
                                     f"  平均电平 {m if m is not None else '?'} dB"))
            self.after(0, self.refresh_library)
        self._run_async(work)

    def tool_merge(self):
        sel = self._selected()
        if len(sel) < 2:
            messagebox.showinfo("提示", "请至少选择 2 个文件（按住 Ctrl 多选）。")
            return

        def work():
            PROC_DIR.mkdir(parents=True, exist_ok=True)
            lst = PROC_DIR / "_concat.txt"
            lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in sel),
                           encoding="utf-8")
            out = PROC_DIR / f"合并_{datetime.datetime.now():%H%M%S}.wav"
            rc, tail = ff("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
                          str(out))
            self.after(0, lambda: self._log(
                f"{'OK  ' if rc == 0 else 'FAIL'} {out.name}  合并 {len(sel)} 个文件"))
            self.after(0, self.refresh_library)
        self._run_async(work)

    def tool_info(self):
        sel = self._selected()
        if not sel:
            messagebox.showinfo("提示", "请先选中一个文件。")
            return
        p = sel[0]
        dur = probe_duration(p)
        mean, mx = probe_level(p)
        try:
            r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(p)],
                               capture_output=True, text=True, errors="ignore",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            stream = next((l.strip() for l in (r.stderr or "").splitlines()
                           if "Audio:" in l), "")
        except Exception:
            stream = ""
        self._log(f"\n[信息] {p.name}")
        self._log(f"  时长 {fmt_time(dur)} · 大小 {fmt_size(p.stat().st_size)}")
        self._log(f"  平均电平 {mean} dB · 峰值 {mx} dB")
        if stream:
            self._log(f"  {stream[:150]}")
        messagebox.showinfo(
            "音频信息",
            f"{p.name}\n\n时长：{fmt_time(dur)}\n大小：{fmt_size(p.stat().st_size)}\n"
            f"平均电平：{mean} dB\n峰值电平：{mx} dB\n\n{stream[:160]}")


def selftest(seconds=2.0):
    """隐藏自检：验证设备枚举与录制在打包环境（exe）下同样可用。"""
    root = _find_root()
    log = root / "_selftest.txt"
    try:
        devs = scan_devices()
        loops = [x for x in devs if x["kind"] == "loopback"]
        mics = [x for x in devs if x["kind"] == "mic"]
        # 能跑双轨就跑双轨，顺带验证多流同时采集
        srcs = ([loops[0]] if loops else []) + ([mics[0]] if mics else [])
        q, evt = queue.Queue(), threading.Event()
        r = Recorder(srcs, q, evt, seconds)
        r.start()
        r.join(timeout=40)
        lines = [f"root={root}", f"devices={len(devs)} loopback={len(loops)} mic={len(mics)}",
                 f"sources={len(srcs)}", f"elapsed={r.elapsed:.2f}", f"err={r.error}"]
        for i, tr in enumerate(r.tracks):
            out = root / f"_selftest_tr{i}.wav"
            r.save_track(i, out)
            with wave.open(str(out), "rb") as w:
                dur = w.getnframes() / float(w.getframerate())
            lines.append(f"track{i} {tr.name[:36]} rate={tr.rate} ch={tr.channels} "
                         f"chunks={len(tr.frames)} dur={dur:.2f}")
            out.unlink()
        ok = all(abs(r.elapsed - seconds) < 1.0 for _ in r.tracks) and not r.error
        lines.append("RESULT=" + ("OK" if ok else "CHECK"))
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        log.write_text(f"RESULT=FAIL\n{type(e).__name__}: {e}\n", encoding="utf-8")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sec = 2.0
        for a in sys.argv:
            if a.isdigit():
                sec = float(a)
        selftest(sec)
    else:
        App().mainloop()
