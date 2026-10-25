"""패링 진동 (트레이 상주).

Windows 기본 출력은 이어폰/스피커로 두고 쓴다. 그러면 게임이 패드로 직접 보내는 효과음만 패드에 들어오므로,
패드에 소리가 들어오는 순간 패드 ch3·4에 묵직한 패링 진동을 낸다. 게임 소리와 섞이지 않아 난전 중에도 정확하다.
고른 듣는 장치의 소리 크기에 맞춰 일정한 주파수 진동을 함께 보낸다. 최소 소리 크기를 넘는 소리에만
반응하고, 클수록 세게 한다.
게임(ZenlessZoneZero.exe)이 실행 중이고 DualSense가 연결됐을 때만 동작하고, 게임 창이 앞에 있을 때만 떨린다.
트레이 아이콘 우클릭: 열기 / 닫기. 설정은 parry_haptic.json에 저장한다.
실행: pythonw parry_haptic.py [--show]
"""
import argparse
import ctypes
import json
import os
import sys
import threading
import time
import tkinter as tk
import winreg
from ctypes import wintypes
from tkinter import ttk

import hid
import numpy as np
import psutil
import pyaudiowpatch as pa
import pystray
from PIL import Image, ImageDraw, ImageTk
from scipy.signal import butter, sosfilt

# exe로 묶으면 __file__이 임시 폴더를 가리키니, 설정은 exe 옆에 둔다
HERE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "parry_haptic.json")
GAME_NAME = "ZenlessZoneZero.exe"  # 감지하는 게임 프로그램
GAME = GAME_NAME.lower()
RATE = 48000
CH = 4
BLOCK = RATE // 100  # 10ms

DEFAULTS = {
    "device": "",  # 듣는 장치(소리 진동 원본) 이름
    "snd_on": True,
    "snd_level": -12,  # 소리 진동 최대 세기 dB
    "snd_min_db": -45,  # 소리 크기 범위(dB). 아래 끝보다 큰 소리부터 진동하고, 위 끝에서 최대 세기로 멈춘다
    "snd_max_db": -33,
    "snd_hz_light": 220,  # 약할 때 진동 주파수(Hz). 높을수록 가볍고 잘게 떨린다
    "snd_hz_heavy": 90,  # 셀 때 진동 주파수(Hz). 낮을수록 묵직하게 떨린다
    "onset_db": -40,  # 패드 소리가 조용하다가 이보다 커지면 패링 진동
    "hz": 70,  # 패링 진동
    "ms": 350,
    "level": 0,
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except OSError:
        pass  # 쓰기 권한이 없는 폴더(예: Program Files)면 저장하지 못하고 이번 실행에서만 설정이 유지된다


# ---- Windows 장치 ----

_RENDER_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"
_P_DESC = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"
LOOPBACK_SUFFIX = " [Loopback]"
# DualSense, DualSense Edge
PAD_IDS = ((0x054C, 0x0CE6), (0x054C, 0x0DF2))


def pad_connected():
    return any(hid.enumerate(vid, pid) for vid, pid in PAD_IDS)


def render_devices():
    """연결된 재생 장치 중 DualSense가 아닌 것의 이름 목록.
    소리를 잡을 때 쓰는 오디오 라이브러리와 같은 출처의 이름이라, Windows 언어와 상관없이 그대로 비교할 수 있다."""
    p = pa.PyAudio()
    try:
        names = {d["name"].removesuffix(LOOPBACK_SUFFIX) for d in p.get_loopback_device_info_generator()}
    finally:
        p.terminate()
    return sorted(n for n in names if "dualsense" not in n.lower())


def default_device_desc():
    """Windows 기본 재생 장치의 장치 이름(예: 'DualSense Wireless Controller')."""
    ole = ctypes.oledll.ole32
    # 오디오 라이브러리가 이 스레드의 COM을 이미 다른 방식으로 초기화했어도 그대로 쓰면 되니 결과를 무시한다
    ctypes.windll.ole32.CoInitializeEx(None, 0)

    def guid(s):
        g = (ctypes.c_byte * 16)()
        ole.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
        return g

    enum = ctypes.c_void_p()
    ole.CoCreateInstance(ctypes.byref(guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")), None, 1,
                         ctypes.byref(guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")), ctypes.byref(enum))
    vt = ctypes.cast(enum, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    dev = ctypes.c_void_p()
    ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                       ctypes.POINTER(ctypes.c_void_p))(vt[4])(enum, 0, 0, ctypes.byref(dev))
    dvt = ctypes.cast(dev, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    pid = wintypes.LPWSTR()
    ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))(dvt[5])(dev, ctypes.byref(pid))
    endpoint = pid.value.split(".")[-1]
    ole.CoTaskMemFree(pid)
    for obj, v in ((dev, dvt), (enum, vt)):
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(v[2])(obj)  # Release
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"{_RENDER_KEY}\{endpoint}\Properties") as k:
        return winreg.QueryValueEx(k, _P_DESC)[0]


_game_pid = [None]


def game_running():
    # 전체 프로세스 훑기는 무거우니, 찾은 PID가 살아 있으면 그것만 본다
    pid = _game_pid[0]
    if pid and psutil.pid_exists(pid):
        return True
    _game_pid[0] = next((p.pid for p in psutil.process_iter(["name"])
                         if (p.info["name"] or "").lower() == GAME), None)
    return _game_pid[0] is not None


_user32 = ctypes.windll.user32


def game_focused():
    """게임 창이 맨 앞(포커스)에 있는지. 게임 PID를 아직 모르면 False."""
    pid = _game_pid[0]
    if not pid:
        return False
    owner = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(_user32.GetForegroundWindow(), ctypes.byref(owner))
    return owner.value == pid


class FocusWatcher:
    """게임 창 포커스를 0.1초마다 확인한다. 소리 콜백에서 매번 Windows에 묻지 않으려고 따로 둔다."""

    def __init__(self):
        self.focused = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            self.focused = game_focused()
            time.sleep(0.1)


# ---- 패링 감지 ----
# 기본 출력이 패드가 아니면 패드에는 패링 등 게임이 직접 보내는 몇 소리만 온다.
# 그래서 조용하다가 소리가 들어오는 순간만 잡으면 된다.

class Onset:
    def __init__(self):
        self.loud = False  # 직전 블록에 소리가 있었는지
        self.quiet_since = 0.0  # 조용해지기 시작한 시각

    def feed(self, x, cfg):
        """패드 루프백 블록을 넣는다. 패링이면 소리 크기(dB), 아니면 None."""
        now = time.monotonic()
        peak_db = 20 * np.log10(float(np.abs(x[:, :2]).max()) / 32768 + 1e-9)
        loud = peak_db >= cfg["onset_db"]
        # 150ms 넘게 조용했다가 소리가 들어온 첫 블록만 잡는다. 소리가 이어지는 동안은 다시 잡지 않는다
        hit = peak_db if loud and not self.loud and now - self.quiet_since >= 0.15 else None
        if not loud and self.loud:
            self.quiet_since = now
        self.loud = loud
        return hit


def pulse_wave(cfg):
    """묵직한 패링 진동: 맞는 순간 가장 세고 바로 서서히 줄어드는 충격 모양.
    줄어드는 동안 주파수도 60%까지 내려가 울림이 가라앉는 느낌을 낸다."""
    n = RATE * int(cfg["ms"]) // 1000
    t = np.arange(n) / RATE
    sec = n / RATE
    freq = cfg["hz"] * 0.6 ** (t / sec)  # 시작 주파수에서 끝날 때 60%까지 부드럽게 내려간다
    w = np.tanh(3 * np.sin(2 * np.pi * np.cumsum(freq) / RATE)) / np.tanh(3)
    # 5ms에 올라가서 곧바로 지수로 줄어든다(끝에서 약 -26dB). 마지막 10ms는 0으로 닫아 딸깍 소리를 막는다
    env = np.minimum(t / 0.005, 1) * np.exp(-t / (sec / 3)) * np.minimum((sec - t) / 0.01, 1)
    return (w * env * 10 ** (cfg["level"] / 20) * 32767).astype(np.float32)


# ---- 소리 진동 ----

DYN_DB = 18  # 소리 크기 범위의 아래 끝에서 진동은 최대 세기보다 이만큼 약하다
# 크기 판단 대역. 녹음을 비교해 보니 전투음과 배경음 차이가 200~1600Hz에서 가장 컸고(+10~15dB),
# 100Hz 아래 베이스는 거의 차이가 없었다(+3dB 안팎)
DET_BAND = (200, 1600)


class SoundShaper:
    """듣는 장치 소리 -> 진동 신호. 10ms 블록 단위로 넣는다.

    소리 크기가 범위(snd_min_db ~ snd_max_db) 아래면 진동하지 않고, 범위 안에서는 클수록 세게,
    위 끝을 넘으면 최대 세기(snd_level)에서 멈춘다.
    """

    def __init__(self, rate, cfg):
        self.cfg = cfg
        self.rate = rate
        self.det_sos = butter(2, [DET_BAND[0], min(DET_BAND[1], rate // 2 - 100)], "bandpass", fs=rate, output="sos")
        self.det_zi = np.zeros((self.det_sos.shape[0], 2))
        self.env_db = None
        self.amp = 0.0
        self.phase = 0.0
        self.freq = float(cfg["snd_hz_light"])
        self.out_db = None  # 마지막 블록의 진동 세기(dB). 꺼졌으면 None

    def process(self, m):
        """소리 블록을 넣으면 같은 길이의 진동 신호(크기 1 기준)를 돌려준다.
        진동은 소리 내용을 그대로 쓰지 않고, 판단한 세기만큼 사인파를 만든다.
        소리를 그대로 쓰면 손이 가장 잘 느끼는 100~300Hz에 실린 배경음악 저음이 같이 떨렸다.
        주파수는 약할 때 snd_hz_light, 셀수록 snd_hz_heavy 쪽으로 옮겨 큰 소리일수록 묵직하게 한다."""
        c = self.cfg
        det, self.det_zi = sosfilt(self.det_sos, m, zi=self.det_zi)
        # 소리 크기(10ms RMS)를 빠르게 올라가고 천천히 내려가게 따라간다
        rms_db = 10 * np.log10(float(np.mean(det.astype(np.float64) ** 2)) + 1e-12)
        if self.env_db is None:
            self.env_db = rms_db
        env = self.env_db + (rms_db - self.env_db) * (0.6 if rms_db > self.env_db else 0.15)
        self.env_db = env
        freq = self.freq
        if env <= c["snd_min_db"]:
            amp, self.out_db = 0.0, None
        else:
            # 0이면 범위 아래 끝, 1이면 위 끝. 위 끝을 넘어도 1에서 멈춘다
            heavy = min((env - c["snd_min_db"]) / max(1.0, c["snd_max_db"] - c["snd_min_db"]), 1.0)
            self.out_db = c["snd_level"] - DYN_DB * (1 - heavy)
            amp = 10 ** (self.out_db / 20)
            freq = c["snd_hz_light"] + (c["snd_hz_heavy"] - c["snd_hz_light"]) * heavy
        # 사인파는 블록을 넘어 위상을 이어 가고, 세기와 주파수는 블록 안에서 부드럽게 바꿔 딸깍 소리를 막는다
        n = len(m)
        ph_ = self.phase + np.cumsum(2 * np.pi * np.linspace(self.freq, freq, n) / self.rate)
        self.phase = float(ph_[-1] % (2 * np.pi))
        y = (np.sin(ph_) * np.linspace(self.amp, amp, n)).astype(np.float32)
        self.amp, self.freq = amp, freq
        return y


# ---- 소리 처리 ----

class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.p = None
        self.streams = []
        self.lock = threading.Lock()
        self.wave = pulse_wave(cfg)
        self.pos = len(self.wave)
        self.hbuf = np.zeros(0, np.float32)  # 소리 진동(48kHz)
        self.snd_name = ""
        self.focus = FocusWatcher()
        self.testing = False

    @property
    def running(self):
        return bool(self.streams)

    def refresh_wave(self):
        self.wave = pulse_wave(self.cfg)

    def test(self):
        if not self.running:
            return False
        self.pos = 0
        self.testing = True  # 테스트는 창을 보고 누르니 게임 포커스와 상관없이 낸다
        return True

    def start(self):
        self.p = pa.PyAudio()
        wasapi = self.p.get_host_api_info_by_type(pa.paWASAPI)["index"]
        loops = list(self.p.get_loopback_device_info_generator())
        pad_in = next((d for d in loops if "dualsense" in d["name"].lower() and d["maxInputChannels"] >= CH), None)
        pad_out = None
        if pad_in:
            name = pad_in["name"].removesuffix(LOOPBACK_SUFFIX)
            pad_out = next((d for d in (self.p.get_device_info_by_index(i) for i in range(self.p.get_device_count()))
                            if d["hostApi"] == wasapi and d["name"] == name and d["maxOutputChannels"] >= CH), None)
        if not pad_in or not pad_out:
            self.p.terminate()
            self.p = None
            return "패드 오디오 장치 없음"
        det = Onset()
        self.hbuf = np.zeros(0, np.float32)

        def on_pad_in(data, frames, ti, status):
            hit = det.feed(np.frombuffer(data, np.int16).reshape(-1, CH), self.cfg)
            if hit is not None:
                self.pos = 0
            return (None, pa.paContinue)

        def on_pad_out(data, frames, ti, status):
            y = np.zeros((frames, CH), np.float32)
            seg = self.wave[self.pos:self.pos + frames]
            self.pos += len(seg)
            with self.lock:
                h = self.hbuf[:frames]
                self.hbuf = self.hbuf[len(h):]
            # 게임 창이 앞에 있을 때만 떨린다(진동 테스트는 예외). 버퍼는 계속 비워서 나중에 몰아 떨지 않게 한다
            if self.focus.focused or self.testing:
                y[:len(seg), 2] = y[:len(seg), 3] = seg
                y[:len(h), 2] += h
                y[:len(h), 3] += h
            if self.pos >= len(self.wave):
                self.testing = False
            return (np.clip(y, -32768, 32767).astype(np.int16).tobytes(), pa.paContinue)

        self.streams = [
            self.p.open(format=pa.paInt16, channels=CH, rate=RATE, input=True, input_device_index=pad_in["index"],
                        frames_per_buffer=BLOCK, stream_callback=on_pad_in),
            self.p.open(format=pa.paInt16, channels=CH, rate=RATE, output=True, output_device_index=pad_out["index"],
                        frames_per_buffer=BLOCK, stream_callback=on_pad_out),
        ]
        self.snd_name = ""
        dev = self.cfg["device"].lower()
        src = next((d for d in loops if dev and d["name"].removesuffix(LOOPBACK_SUFFIX).lower() == dev), None)
        if self.cfg["snd_on"] and src:
            self.streams.append(self.open_sound(src))
            self.snd_name = self.cfg["device"]
        return None

    def open_sound(self, src):
        """듣는 장치 소리를 진동으로 만든다. 배경보다 확 큰 소리에만, 클수록 세게 반응한다."""
        rate, ch = int(src["defaultSampleRate"]), src["maxInputChannels"]
        shaper = SoundShaper(rate, self.cfg)

        def on_sound(data, frames, ti, status):
            x = np.frombuffer(data, np.int16).reshape(-1, ch).astype(np.float32) / 32768
            y = shaper.process(x[:, :2].mean(1) if ch >= 2 else x[:, 0]) * 32767
            if rate != RATE:
                y = np.interp(np.linspace(0, len(y) - 1, round(len(y) * RATE / rate)), np.arange(len(y)), y)
            with self.lock:
                self.hbuf = np.concatenate([self.hbuf, y.astype(np.float32)])[-RATE // 10:]
            return (None, pa.paContinue)

        return self.p.open(format=pa.paInt16, channels=ch, rate=rate, input=True, input_device_index=src["index"],
                           frames_per_buffer=rate // 100, stream_callback=on_sound)

    def stop(self):
        for s in self.streams:
            try:
                s.stop_stream()
                s.close()
            except OSError:
                pass
        self.streams = []
        if self.p:
            self.p.terminate()
            self.p = None

    def alive(self):
        return all(s.is_active() for s in self.streams)


# ---- 트레이와 창 ----

def icon_image(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((6, 6, 58, 58), fill=color)
    return img


def make_icon(size=256):
    """프로그램 아이콘: 오렌지색 둥근 사각형 위의 흰 Z. 글꼴 없이 도형으로 그린다. parry.ico도 이 함수로 만든다."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 5, fill=(242, 130, 30))
    z = [(0.22, 0.22), (0.78, 0.22), (0.78, 0.33), (0.43, 0.67), (0.78, 0.67), (0.78, 0.78),
         (0.22, 0.78), (0.22, 0.67), (0.57, 0.33), (0.22, 0.33)]
    d.polygon([(x * size, y * size) for x, y in z], fill=(255, 255, 255))
    return img


GRAY, ORANGE, GREEN = (128, 128, 128), (230, 150, 30), (40, 190, 80)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="시작할 때 창을 연다")
    a = ap.parse_args()

    cfg = load_config()
    if not cfg["device"]:
        # 처음에는 Windows 기본 출력을 듣는 장치로 고른다
        try:
            desc = default_device_desc()
            cfg["device"] = next((n for n in render_devices() if desc in n), "")
        except OSError:
            pass
    eng = Engine(cfg)
    status = {"text": "시작 중"}
    stop, restart, want_open = threading.Event(), threading.Event(), threading.Event()

    def loop(icon):
        icon.visible = True
        mtime = os.path.getmtime(CONFIG) if os.path.exists(CONFIG) else 0
        while not stop.is_set():
            # 분석기 등에서 설정 파일을 바꾸면 다시 읽는다
            m = os.path.getmtime(CONFIG) if os.path.exists(CONFIG) else 0
            if m != mtime:
                mtime = m
                cfg.update(load_config())
            if restart.is_set():
                restart.clear()
                eng.stop()
            if not game_running():
                eng.stop()
                text, color = "비활성화 (실행 안 됨)", GRAY
            elif not eng.running and not pad_connected():
                text, color = "비활성화 (패드 연결 안 됨)", GRAY
            else:
                if eng.running and not eng.alive():
                    eng.stop()
                err = None if eng.running else eng.start()
                if err:
                    text, color = f"비활성화 ({err})", ORANGE
                elif not eng.focus.focused:
                    text, color = "비활성화 (게임 창이 앞에 없음)", GRAY
                else:
                    text, color = "활성화", GREEN
                    try:
                        if "dualsense" in default_device_desc().lower():
                            text, color = "활성화 (기본 출력이 패드라 게임 소리와 섞여 부정확)", ORANGE
                    except OSError:
                        pass
            text = f"{GAME_NAME}  ·  {text}"
            status["text"] = text
            icon.icon = icon_image(color)
            icon.title = f"패링 진동: {text}"
            restart.wait(0.5)  # 게임 창 포커스가 바뀐 걸 빨리 보여 주려고 자주 확인한다
        eng.stop()

    def on_close(icon, item):
        stop.set()
        restart.set()
        icon.stop()

    icon = pystray.Icon("parry_haptic", icon_image(GRAY), "패링 진동", pystray.Menu(
        pystray.MenuItem("열기", lambda icon, item: want_open.set(), default=True),
        pystray.MenuItem("닫기", on_close)))
    icon.run_detached(setup=loop)

    root = tk.Tk()
    root.title("패링 진동")
    icon_photo = ImageTk.PhotoImage(make_icon(64))  # 창 아이콘. 변수를 들고 있어야 사라지지 않는다
    root.iconphoto(True, icon_photo)
    root.resizable(False, False)
    root.protocol("WM_DELETE_WINDOW", root.withdraw)  # X는 창만 숨긴다
    f = ttk.Frame(root, padding=12)
    f.grid()
    row = [0]

    def r():
        row[0] += 1
        return row[0] - 1

    def save():
        save_config(cfg)

    status_var, test_msg = tk.StringVar(), tk.StringVar()
    i = r()
    ttk.Label(f, text="상태").grid(row=i, column=0, sticky="w")
    ttk.Label(f, textvariable=status_var, width=46).grid(row=i, column=1, columnspan=2, sticky="w")

    def slider(label, key, lo, hi, fmt, step, after=save):
        i = r()
        ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", pady=(4, 0))
        shown = tk.StringVar(value=fmt.format(cfg[key]))

        def move(v):
            v = round(float(v) / step) * step
            cfg[key] = round(v, 3) if isinstance(DEFAULTS[key], float) else int(v)
            shown.set(fmt.format(cfg[key]))
            after()

        ttk.Scale(f, from_=lo, to=hi, value=cfg[key], command=move, length=240).grid(row=i, column=1, sticky="w", pady=(4, 0))
        ttk.Label(f, textvariable=shown, width=8).grid(row=i, column=2, sticky="w", pady=(4, 0))

    def header(text):
        lbl = ttk.Label(f, text=text, font=("", 10, "bold"))
        lbl.grid(row=r(), column=0, columnspan=3, sticky="w")
        return lbl

    def tip(widget, text):
        """마우스를 올리면 설명을 띄운다."""
        box = []

        def show(e):
            w = tk.Toplevel(widget)
            w.wm_overrideredirect(True)
            w.wm_geometry(f"+{e.x_root + 12}+{e.y_root + 12}")
            tk.Label(w, text=text, justify="left", bg="#ffffe0", relief="solid", borderwidth=1,
                     padx=6, pady=4).pack()
            box.append(w)

        def hide(e):
            while box:
                box.pop().destroy()

        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)

    def range_slider(label, key_lo, key_hi, lo, hi, step, unit, width=240, pad=8):
        """손잡이 두 개로 범위를 정하는 슬라이더. 가까운 손잡이를 끌어 옮긴다."""
        i = r()
        ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", pady=(4, 0))
        cv = tk.Canvas(f, width=width, height=22, highlightthickness=0, bg=root.cget("bg"))
        cv.grid(row=i, column=1, sticky="w", pady=(4, 0))
        shown = tk.StringVar()
        ttk.Label(f, textvariable=shown, width=12).grid(row=i, column=2, sticky="w", pady=(4, 0))
        to_x = lambda v: pad + (v - lo) / (hi - lo) * (width - 2 * pad)
        to_v = lambda x: min(hi, max(lo, round((lo + (x - pad) / (width - 2 * pad) * (hi - lo)) / step) * step))

        def redraw():
            cv.delete("all")
            a, b = to_x(cfg[key_lo]), to_x(cfg[key_hi])
            cv.create_line(pad, 11, width - pad, 11, fill="#c8c8c8", width=4)
            cv.create_line(a, 11, b, 11, fill="#0a64c8", width=4)
            for x in (a, b):
                cv.create_rectangle(x - 4, 3, x + 4, 19, fill="#0a64c8", outline="")
            shown.set(f"{cfg[key_lo]:.0f} ~ {cfg[key_hi]:.0f} {unit}")

        grab = [None]

        def press(e):
            grab[0] = key_lo if abs(e.x - to_x(cfg[key_lo])) <= abs(e.x - to_x(cfg[key_hi])) else key_hi
            drag(e)

        def drag(e):
            v = to_v(e.x)
            # 두 손잡이가 엇갈리지 않게 한다
            if grab[0] == key_lo:
                v = min(v, cfg[key_hi])
            else:
                v = max(v, cfg[key_lo])
            cfg[grab[0]] = int(v)
            redraw()
            save()

        cv.bind("<Button-1>", press)
        cv.bind("<B1-Motion>", drag)
        redraw()

    # 소리 진동
    i = r()
    ttk.Separator(f).grid(row=i, column=0, columnspan=3, sticky="ew", pady=(10, 4))
    tip(header("사운드 햅틱 진동"), "듣는 장치에서 소리 크기 범위를 넘는 소리가 나면 진동.\n"
                                  "클수록 세고 낮은 주파수로, 범위 위를 넘으면 최대에서 고정.")
    i = r()
    snd = tk.BooleanVar(value=cfg["snd_on"])

    def on_snd():
        cfg["snd_on"] = snd.get()
        save()
        restart.set()

    ttk.Checkbutton(f, text="사용", variable=snd, command=on_snd).grid(row=i, column=0, columnspan=3, sticky="w")
    i = r()
    ttk.Label(f, text="듣는 장치").grid(row=i, column=0, sticky="w", pady=(4, 0))
    combo = ttk.Combobox(f, state="readonly", width=36)
    combo.grid(row=i, column=1, sticky="w", pady=(4, 0))

    def refresh_devices():
        names = render_devices()
        combo["values"] = names
        if cfg["device"] in names:
            combo.set(cfg["device"])
        else:
            combo.set(f"(연결 안 됨) {cfg['device']}" if cfg["device"] else "(선택 안 됨)")

    def on_pick(e):
        cfg["device"] = combo.get()
        save()
        restart.set()

    combo.bind("<<ComboboxSelected>>", on_pick)
    ttk.Button(f, text="새로고침", command=refresh_devices).grid(row=i, column=2, padx=(6, 0), pady=(4, 0))
    slider("진동 세기", "snd_level", -30, 0, "{:.0f} dB", 1)
    range_slider("소리 크기 범위", "snd_min_db", "snd_max_db", -70, 0, 1, "dB")
    range_slider("진동 주파수 범위", "snd_hz_heavy", "snd_hz_light", 60, 300, 5, "Hz")

    # 패링 진동
    i = r()
    ttk.Separator(f).grid(row=i, column=0, columnspan=3, sticky="ew", pady=(10, 4))
    tip(header("패링 진동"), "게임이 패드로 직접 보내는 패링 소리가 들어오면\n묵직한 진동 한 번.")
    slider("진동 세기", "level", -20, 0, "{:.0f} dB", 1, lambda: (eng.refresh_wave(), save()))
    slider("소리 크기 기준", "onset_db", -60, -10, "{:.0f} dB", 1)
    slider("진동 주파수", "hz", 30, 200, "{:.0f} Hz", 1, lambda: (eng.refresh_wave(), save()))
    slider("진동 길이", "ms", 100, 1000, "{:.0f} ms", 10, lambda: (eng.refresh_wave(), save()))
    i = r()
    bar = ttk.Frame(f)
    bar.grid(row=i, column=0, columnspan=3, sticky="e", pady=(10, 0))
    ttk.Label(bar, textvariable=test_msg).pack(side="left", padx=(0, 8))
    ttk.Button(bar, text="패링 진동 테스트",
               command=lambda: test_msg.set("" if eng.test() else "동작 중일 때만 가능")).pack(side="left")

    root.withdraw()
    if a.show:
        want_open.set()

    def poll():
        if stop.is_set():
            root.destroy()
            return
        status_var.set(status["text"])
        if want_open.is_set():
            want_open.clear()
            refresh_devices()
            root.deiconify()
            root.lift()
            root.focus_force()
        root.after(300, poll)

    poll()
    root.mainloop()


if __name__ == "__main__":
    main()
