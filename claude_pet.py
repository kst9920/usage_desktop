# -*- coding: utf-8 -*-
# v2
"""
ClaudePet - 데스크탑을 돌아다니는 꼬마 양 Claude 사용량 위젯 (Windows)

- 양의 배에 현재 세션 사용량 % + 리셋까지 남은 시간 표시 (목도리 색 = 사용량 단계)
- 작업표시줄 위를 걸어다니고, 창을 만나면 타고 올라가거나 방향 전환, 끝에서 낙하
- 좌클릭: 주간 한도 패널 / 드래그: 이동 / 우클릭: 메뉴

렌더링: Pillow로 4배 크기로 그려 축소(안티앨리어싱) + Win32 레이어드 윈도우(픽셀 알파)
→ 부드러운 테두리. Pillow가 없으면 tkinter 캔버스로 폴백(테두리 거침).

커스텀 스킨: 스크립트 옆 skin 폴더 안에 캐릭터별 하위폴더를 만들고 각 폴더에
walk1.png walk2.png climb1.png climb2.png fall.png idle.png
(정사각 투명 PNG, 오른쪽을 보는 그림) 를 넣으면 캐릭터 세트가 됨.
시작 시 랜덤으로 하나 선택, 더블클릭하면 다른 캐릭터로 교체 (기본 양 포함).

필요: Python 3.9+, pillow 권장 (pip install pillow). 실행: pythonw claude_pet.py
"""

import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import random
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ----------------------------- 설정 -----------------------------
SIZE = 72                      # 캐릭터 크기(px)
SS = 4                         # 슈퍼샘플 배율 (4배로 그려 축소)
POLL_INTERVAL = 180            # 사용량 API 폴링 주기(초)
TICK_MS = 30                   # 물리 틱(ms)
WALK_SPEED = 1.5
CLIMB_SPEED = 1.6              # 평균 등반 속도 (맥동해서 꿈틀꿈틀 올라감)
GRAVITY = 0.9
MAX_FALL = 14.0
CLIMB_PROB = 0.5               # 벽을 만나면 타고 올라갈 확률
IDLE_PROB = 0.0012             # 틱당 잠깐 쉬어갈 확률 (드물게)
TRANSPARENT = "#ff00fe"        # 폴백 모드 투명색
USER_AGENT = "claude-code/2.0.14 (external, cli)"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URLS = ["https://platform.claude.com/v1/oauth/token",
              "https://console.anthropic.com/v1/oauth/token"]  # 신주소 우선, 구주소 폴백

IS_WIN = sys.platform == "win32"

# ------------------------- DPI (tkinter 좌표 = 실제 픽셀) -------------------------
if IS_WIN:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

try:
    from PIL import Image, ImageDraw, ImageFont, ImageChops, ImageOps
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

if IS_WIN:
    import tkinter as tk
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    dwmapi = ctypes.windll.dwmapi

# ----------------------------- 시간 포맷 -----------------------------
KO_WD = ["월", "화", "수", "목", "금", "토", "일"]


def parse_iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def remaining_parts(reset_dt):
    if reset_dt is None:
        return None
    total = int((reset_dt - datetime.now(timezone.utc)).total_seconds())
    if total <= 0:
        return None
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    return d, h, rem // 60


def fmt_compact(reset_dt):
    p = remaining_parts(reset_dt)
    if p is None:
        return "--"
    d, h, m = p
    if d > 0:
        return f"{d}d{h}h"
    if h > 0:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def fmt_korean(reset_dt):
    p = remaining_parts(reset_dt)
    if p is None:
        return "곧 초기화"
    d, h, m = p
    if d > 0:
        return f"{d}일 {h}시간 후"
    if h > 0:
        return f"{h}시간 {m}분 후"
    return f"{m}분 후"


def fmt_local_time(reset_dt):
    if reset_dt is None:
        return ""
    lt = reset_dt.astimezone()
    ampm = "오전" if lt.hour < 12 else "오후"
    h12 = lt.hour % 12 or 12
    return f"({KO_WD[lt.weekday()]}) {ampm} {h12}:{lt.minute:02d}"


# ----------------------------- 자격 증명 / API -----------------------------
def cred_path():
    return os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")


def load_credentials():
    env = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env:
        return {"accessToken": env, "refreshToken": None, "expiresAt": None}
    try:
        with open(cred_path(), "r", encoding="utf-8") as f:
            return json.load(f).get("claudeAiOauth") or {}
    except Exception:
        return {}


def save_credentials(oauth):
    path = cred_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    cur = data.get("claudeAiOauth") or {}
    cur.update(oauth)
    data["claudeAiOauth"] = cur
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def token_expired(cred):
    exp = cred.get("expiresAt")
    return bool(exp) and time.time() * 1000 > exp - 60000


def refresh_token(cred):
    rt = cred.get("refreshToken")
    if not rt:
        return None
    body = json.dumps({"grant_type": "refresh_token",
                       "refresh_token": rt, "client_id": CLIENT_ID}).encode()
    res = None
    for url in TOKEN_URLS:
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                res = json.load(r)
            break
        except Exception:
            continue
    if not res or not res.get("access_token"):
        return None
    new = {"accessToken": res.get("access_token"),
           "expiresAt": int(time.time() * 1000) + int(res.get("expires_in", 3600)) * 1000}
    if res.get("refresh_token"):
        new["refreshToken"] = res["refresh_token"]
    try:
        save_credentials(new)
    except Exception:
        pass
    cred.update(new)
    return cred


def fetch_usage():
    cred = load_credentials()
    if not cred.get("accessToken"):
        return None, "로그인 정보 없음"
    if token_expired(cred):
        cred = refresh_token(cred) or None
        if not cred:
            return None, "토큰 만료 — Claude Code 실행 필요"

    def _call(token):
        req = urllib.request.Request(USAGE_URL, headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)

    try:
        return _call(cred["accessToken"]), None
    except urllib.error.HTTPError as e:
        if e.code == 401:
            cred2 = refresh_token(cred)
            if cred2:
                try:
                    return _call(cred2["accessToken"]), None
                except Exception:
                    pass
            return None, "인증 실패 (401)"
        if e.code == 429:
            return None, "429"
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, f"네트워크 오류: {type(e).__name__}"


# ----------------------------- 폰트 / 스프라이트 -----------------------------
_font_cache = {}


def load_font(px, bold=True, korean=False):
    key = (px, bold, korean)
    if key in _font_cache:
        return _font_cache[key]
    names = []
    if korean:
        names += ["malgunbd.ttf" if bold else "malgun.ttf"]
    names += ["segoeuib.ttf" if bold else "segoeui.ttf",
              "arialbd.ttf" if bold else "arial.ttf",
              "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"]
    f = None
    for n in names:
        try:
            f = ImageFont.truetype(n, px)
            break
        except Exception:
            continue
    if f is None:
        f = ImageFont.load_default()
    _font_cache[key] = f
    return f


def usage_color(pct):
    if pct is None:
        return (154, 160, 166, 255)
    if pct < 50:
        return (47, 111, 237, 255)
    if pct < 80:
        return (232, 147, 12, 255)
    return (224, 62, 62, 255)


def _rounded_leg(d, x, y, dx, dy, w=13, col=(74, 74, 74, 255)):
    """(x,y)에서 (x+dx,y+dy)로 뻗는 둥근 다리"""
    d.line([(x, y), (x + dx, y + dy)], fill=col, width=w)
    r = w // 2
    d.ellipse([x + dx - r, y + dy - r, x + dx + r, y + dy + r], fill=col)


def draw_sheep_base(pose, frame, blink, scarf_col):
    """
    오른쪽을 보는 양 스프라이트 (SS*SIZE 크기, RGBA).
    pose: walk / climb / fall / idle   frame: 0/1
    """
    S = SIZE * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = S / 256.0  # 256 기준 좌표계

    def U(*vals):
        return [v * u for v in vals]

    wool = (250, 249, 246, 255)
    wool_line = (215, 210, 200, 255)
    face_c = (240, 219, 189, 255)
    dark = (74, 74, 74, 255)

    bob = math.sin(frame * math.pi + 0.5) * 3 if pose == "walk" else 0
    if pose == "idle":
        bob = math.sin(time.time() * 2.2) * 2.5

    # ---- 다리 ----
    ly = 214 + bob * 0.3
    if pose == "walk":
        a = 14 if frame == 0 else -14
        _rounded_leg(d, *U(92, ly), *U(-a * 0.6, 30), w=int(13 * u))
        _rounded_leg(d, *U(126, ly), *U(a * 0.6, 30), w=int(13 * u))
        _rounded_leg(d, *U(158, ly), *U(-a * 0.5, 30), w=int(13 * u))
        _rounded_leg(d, *U(186, ly), *U(a * 0.5, 30), w=int(13 * u))
    elif pose == "climb":
        # 벽(오른쪽)에 매달려 발을 번갈아 뻗음
        a = 12 if frame == 0 else -12
        _rounded_leg(d, *U(196, 120), *U(34, -6 + a), w=int(13 * u))
        _rounded_leg(d, *U(198, 160), *U(34, 6 - a), w=int(13 * u))
        _rounded_leg(d, *U(110, 208), *U(-6, 34 + a * 0.5), w=int(13 * u))
        _rounded_leg(d, *U(150, 212), *U(6, 32 - a * 0.5), w=int(13 * u))
    elif pose == "fall":
        _rounded_leg(d, *U(92, 210), *U(-18, 22), w=int(13 * u))
        _rounded_leg(d, *U(126, 214), *U(-6, 30), w=int(13 * u))
        _rounded_leg(d, *U(158, 214), *U(6, 30), w=int(13 * u))
        _rounded_leg(d, *U(186, 210), *U(18, 22), w=int(13 * u))
    else:  # idle
        for x in (92, 126, 158, 186):
            _rounded_leg(d, *U(x, ly), *U(0, 30), w=int(13 * u))

    # ---- 몸통 (구름 뭉치) ----
    cx, cy = 118, 152 + bob
    d.ellipse(U(cx - 72, cy - 58, cx + 72, cy + 58), fill=wool, outline=wool_line, width=int(3 * u))
    for ang in range(0, 360, 36):  # 보글보글 테두리
        bx = cx + math.cos(math.radians(ang)) * 68
        by = cy + math.sin(math.radians(ang)) * 54
        d.ellipse(U(bx - 17, by - 17, bx + 17, by + 17), fill=wool)
    d.ellipse(U(cx - 72, cy - 58, cx + 72, cy + 58), outline=wool_line, width=int(3 * u))

    # ---- 배 (숫자 들어갈 자리) ----
    d.ellipse(U(cx - 52, cy - 38, cx + 52, cy + 44), fill=(255, 255, 255, 255))

    # ---- 머리 (크고 또렷하게, 몸 오른쪽 위로 내밀기) ----
    fx, fy = 200, 88 + bob * 0.6
    if pose == "climb":
        fy -= 8
    # 귀 (뒤로 처진 귀)
    d.ellipse(U(fx - 52, fy + 2, fx - 14, fy + 26), fill=face_c,
              outline=(210, 185, 150, 255), width=int(2 * u))
    # 얼굴
    d.ellipse(U(fx - 30, fy - 18, fx + 34, fy + 40), fill=face_c)
    # 머리털 (얼굴 위 구름)
    d.ellipse(U(fx - 26, fy - 34, fx + 14, fy - 4), fill=wool, outline=wool_line, width=int(2 * u))
    d.ellipse(U(fx - 6, fy - 30, fx + 26, fy - 6), fill=wool)
    # 눈
    ex, ey = fx + 10, fy + 8
    if blink:
        d.line(U(ex - 7, ey, ex + 7, ey), fill=dark, width=int(4 * u))
    else:
        d.ellipse(U(ex - 6, ey - 6, ex + 6, ey + 6), fill=dark)
        d.ellipse(U(ex + 1, ey - 4, ex + 4, ey - 1), fill=(255, 255, 255, 255))
    # 코/입
    d.ellipse(U(fx + 22, fy + 18, fx + 28, fy + 24), fill=(180, 130, 110, 255))
    d.arc(U(fx + 12, fy + 22, fx + 28, fy + 34), start=20, end=160, fill=dark, width=int(3 * u))
    # 볼터치
    d.ellipse(U(fx - 14, fy + 18, fx - 2, fy + 28), fill=(247, 181, 166, 180))

    # ---- 목도리 (사용량 색) ----
    d.line(U(fx - 26, fy + 38, fx + 26, fy + 34), fill=scarf_col, width=int(12 * u))
    d.line(U(fx - 16, fy + 40, fx - 12, fy + 58), fill=scarf_col, width=int(9 * u))

    return img


SKIN_FILES = [("walk0", "walk1.png"), ("walk1", "walk2.png"),
              ("climb0", "climb1.png"), ("climb1", "climb2.png"),
              ("fall0", "fall.png"), ("idle0", "idle.png")]


def load_skin_frames(folder):
    """폴더 하나에서 스킨 프레임 로드 (없으면 None)"""
    frames = {}
    for key, fname in SKIN_FILES:
        p = os.path.join(folder, fname)
        if os.path.isfile(p):
            try:
                frames[key] = Image.open(p).convert("RGBA").resize(
                    (SIZE * SS, SIZE * SS), Image.LANCZOS)
            except Exception:
                pass
    return frames or None


def skin_base_dir():
    """skin 폴더 위치: exe/스크립트 옆 폴더 우선, 없으면 PyInstaller 내장 리소스"""
    ext = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "skin")
    if os.path.isdir(ext):
        return ext
    mp = getattr(sys, "_MEIPASS", None)  # PyInstaller --onefile 임시 추출 경로
    if mp and os.path.isdir(os.path.join(mp, "skin")):
        return os.path.join(mp, "skin")
    return ext


def load_skins():
    """스킨 세트 목록: skin/ 바로 아래 + skin/하위폴더들.
    커스텀 스킨이 하나도 없을 때만 기본 양을 사용."""
    sets = []
    base = skin_base_dir()
    if os.path.isdir(base):
        flat = load_skin_frames(base)
        if flat:
            sets.append(flat)
        for name in sorted(os.listdir(base)):
            sub = os.path.join(base, name)
            if os.path.isdir(sub):
                fr = load_skin_frames(sub)
                if fr:
                    sets.append(fr)
    if not sets:
        sets = [None]  # None = 기본 양
    return sets


_sprite_cache = {}


def make_frame(pose, frame, direction, blink, pct, time_txt, err, skin):
    """최종 SIZE x SIZE RGBA 프레임 (텍스트 포함)"""
    scarf = usage_color(pct)
    key = (pose, frame, direction, blink, scarf)
    base = _sprite_cache.get(key)
    if base is None:
        sk = skin.get(f"{pose}{frame}") if skin else None
        if sk is None and skin:
            sk = skin.get(f"{pose}0")
        src = sk if sk is not None else draw_sheep_base(pose, frame, blink, scarf)
        if pose == "climb" and sk is None:
            src = src.rotate(-14, resample=Image.BICUBIC, center=(SIZE * SS // 2, SIZE * SS // 2))
        if direction < 0:
            src = ImageOps.mirror(src)
        base = src.resize((SIZE, SIZE), Image.LANCZOS)
        if len(_sprite_cache) > 120:
            _sprite_cache.clear()
        _sprite_cache[key] = base

    img = base.copy()
    d = ImageDraw.Draw(img)
    cx = SIZE * 0.5 + (2 if direction < 0 else -2)
    cy = SIZE * 0.56
    if err and pct is None:
        d.text((cx, cy - 3), "!", font=load_font(16), fill=(224, 62, 62, 255),
               anchor="mm", stroke_width=2, stroke_fill=(255, 255, 255, 255))
    elif pct is None:
        d.text((cx, cy), "…", font=load_font(14), fill=(90, 90, 90, 255), anchor="mm")
    else:
        d.text((cx, cy - 4), f"{pct:.0f}%", font=load_font(14),
               fill=(32, 33, 36, 255), anchor="mm",
               stroke_width=2, stroke_fill=(255, 255, 255, 230))
        d.text((cx, cy + 9), time_txt, font=load_font(9, bold=False),
               fill=(95, 99, 104, 255), anchor="mm",
               stroke_width=2, stroke_fill=(255, 255, 255, 230))
    if err and pct is not None:
        # 데이터는 있지만 최신 갱신 실패 → 빨간 점 배지
        d.ellipse([SIZE - 13, 3, SIZE - 3, 13], fill=(224, 62, 62, 255),
                  outline=(255, 255, 255, 255))
    return img


# ----------------------------- Win32 레이어드 윈도우 -----------------------------
if IS_WIN:
    GA_ROOT = 2
    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TOOLWINDOW = 0x00000080
    ULW_ALPHA = 2
    DWMWA_CLOAKED = 14
    HWND_TOPMOST = -1
    SWP_NOSIZE, SWP_NOACTIVATE = 0x0001, 0x0010

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                    ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                    ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                    ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                    ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_byte)]

    class LayeredRenderer:
        """PIL RGBA 이미지를 픽셀 알파 그대로 창에 출력"""

        def __init__(self, hwnd, size):
            self.hwnd, self.size = hwnd, size
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
            self.screen_dc = user32.GetDC(0)
            self.mem_dc = gdi32.CreateCompatibleDC(self.screen_dc)
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = size
            bmi.bmiHeader.biHeight = -size  # top-down
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            self.bits = ctypes.c_void_p()
            self.dib = gdi32.CreateDIBSection(self.mem_dc, ctypes.byref(bmi), 0,
                                              ctypes.byref(self.bits), None, 0)
            gdi32.SelectObject(self.mem_dc, self.dib)

        def render(self, img, x, y):
            # 프리멀티플라이드 알파로 변환
            r, g, b, a = img.split()
            pre = Image.merge("RGBA", (ImageChops.multiply(r, a),
                                       ImageChops.multiply(g, a),
                                       ImageChops.multiply(b, a), a))
            raw = pre.tobytes("raw", "BGRA")
            ctypes.memmove(self.bits, raw, len(raw))
            self._ulw(x, y)

        def move(self, x, y):
            self._ulw(x, y)

        def _ulw(self, x, y):
            sz = wt.SIZE(self.size, self.size)
            src = wt.POINT(0, 0)
            dst = wt.POINT(int(x), int(y))
            bf = BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, alpha 255, AC_SRC_ALPHA
            user32.UpdateLayeredWindow(self.hwnd, self.screen_dc, ctypes.byref(dst),
                                       ctypes.byref(sz), self.mem_dc, ctypes.byref(src),
                                       0, ctypes.byref(bf), ULW_ALPHA)
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, int(x), int(y), 0, 0,
                                SWP_NOSIZE | SWP_NOACTIVATE)

    EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def get_work_area():
        rect = wt.RECT()
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
        return rect.left, rect.top, rect.right, rect.bottom

    def enum_desktop_windows(exclude_hwnds):
        rects = []

        def cb(hwnd, _):
            if hwnd in exclude_hwnds:
                return True
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            if user32.GetWindowTextLengthW(hwnd) == 0:
                return True
            if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                return True
            cloaked = wt.DWORD(0)
            try:
                dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED,
                                             ctypes.byref(cloaked), ctypes.sizeof(cloaked))
                if cloaked.value:
                    return True
            except Exception:
                pass
            r = wt.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
                return True
            if r.right - r.left < 120 or r.bottom - r.top < 80:
                return True
            rects.append((r.left, r.top, r.right, r.bottom))
            return True

        user32.EnumWindows(EnumWindowsProc(cb), 0)
        return rects


# ----------------------------- 메인 앱 -----------------------------
class ClaudePet:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("ClaudePet")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)

        self.layered = HAS_PIL
        if self.layered:
            self.root.config(bg="black")
            self.canvas = None
        else:
            self.root.attributes("-transparentcolor", TRANSPARENT)
            self.root.config(bg=TRANSPARENT)
            self.canvas = tk.Canvas(self.root, width=SIZE, height=SIZE,
                                    bg=TRANSPARENT, highlightthickness=0)
            self.canvas.pack()

        # 상태
        self.usage = None
        self.usage_err = None
        self.last_update = None
        self.poll_interval = POLL_INTERVAL
        self.walk_enabled = True
        self.panel = None
        self.skins = load_skins() if HAS_PIL else [None]
        self.skin_idx = random.randrange(len(self.skins))
        self.skin = self.skins[self.skin_idx]

        # 물리
        wl, wtop, wr, wb = get_work_area()
        self.x = float(random.randint(wl + 100, max(wl + 101, wr - 200)))
        self.y = float(wb - SIZE)
        self.dir = random.choice([-1, 1])
        self.vy = 0.0
        self.state = "fall"
        self.idle_until = 0.0
        self.no_wall_until = 0.0
        self.climb_target = None
        self.climb_check = 0
        self.step_phase = 0.0
        self.anim_frame = 0
        self.blink_until = 0.0
        self.next_blink = time.time() + random.uniform(2, 5)
        self.win_rects = []
        self.rects_lock = threading.Lock()
        self._tick_n = 0
        self._last_kick = 0.0

        # 입력
        self.drag_start = None
        self.dragging = False
        self._click_job = None
        self._suppress_click_until = 0.0
        target = self.canvas if self.canvas else self.root
        target.bind("<ButtonPress-1>", self.on_press)
        target.bind("<B1-Motion>", self.on_motion)
        target.bind("<ButtonRelease-1>", self.on_release)
        target.bind("<Double-Button-1>", self.on_double_click)
        target.bind("<Button-3>", self.on_right_click)

        self.root.geometry(f"{SIZE}x{SIZE}+{int(self.x)}+{int(self.y)}")
        self.root.update_idletasks()
        hwnd_src = self.canvas.winfo_id() if self.canvas else self.root.winfo_id()
        self.own_hwnd = user32.GetAncestor(hwnd_src, GA_ROOT)

        self.renderer = LayeredRenderer(self.own_hwnd, SIZE) if self.layered else None

        threading.Thread(target=self.poll_loop, daemon=True).start()
        threading.Thread(target=self.scan_loop, daemon=True).start()

        self.render_frame(force=True)
        self.root.after(TICK_MS, self.tick)

    # ------------------- 백그라운드 -------------------
    def poll_loop(self):
        while True:
            data, err = fetch_usage()
            if err == "429":
                self.poll_interval = min(self.poll_interval * 2, 900)
                self.usage_err = f"요청 제한(429) — {self.poll_interval // 60}분 후 재시도"
            elif err:
                self.usage_err = err
            else:
                self.usage, self.usage_err = data, None
                self.last_update = datetime.now()
                self.poll_interval = POLL_INTERVAL
            try:
                self.root.after(0, lambda: self.render_frame(force=True))
                self.root.after(0, self.refresh_panel)
            except Exception:
                return
            time.sleep(self.poll_interval)

    def scan_loop(self):
        excl = {self.own_hwnd}
        while True:
            try:
                rects = enum_desktop_windows(excl)
            except Exception:
                rects = []
            with self.rects_lock:
                self.win_rects = rects
            time.sleep(0.5)

    def force_refresh(self):
        def _go():
            data, err = fetch_usage()
            if not err:
                self.usage, self.usage_err = data, None
                self.last_update = datetime.now()
                self.poll_interval = POLL_INTERVAL
            else:
                self.usage_err = err if err != "429" else "요청 제한(429)"
            self.root.after(0, lambda: self.render_frame(force=True))
            self.root.after(0, self.refresh_panel)
        threading.Thread(target=_go, daemon=True).start()

    # ------------------- 렌더링 -------------------
    def session_info(self):
        if not self.usage:
            return None, None
        fh = self.usage.get("five_hour") or {}
        pct = fh.get("utilization")
        reset = parse_iso(fh.get("resets_at", ""))
        # 리셋 시각이 지났는데 서버가 아직 옛 값을 주면 0%로 표시 (새 세션 시작 전)
        if reset is not None and remaining_parts(reset) is None:
            pct, reset = 0.0, None
        return pct, reset

    def render_frame(self, force=False):
        pct, reset = self.session_info()
        now = time.time()
        blink = now < self.blink_until
        if now >= self.next_blink:
            self.blink_until = now + 0.15
            self.next_blink = now + random.uniform(2.5, 6)

        pose = {"walk": "walk", "climb": "climb", "fall": "fall",
                "idle": "idle", "held": "fall"}.get(self.state, "idle")
        if not self.walk_enabled and self.state not in ("held",):
            pose = "idle"

        if self.layered:
            img = make_frame(pose, self.anim_frame % 2, self.dir, blink,
                             pct, fmt_compact(reset), self.usage_err, self.skin)
            self.renderer.render(img, self.x, self.y)
        else:
            self._draw_fallback(pct, reset)

    def _draw_fallback(self, pct, reset):
        c = self.canvas
        c.delete("all")
        s = SIZE
        col = "#9aa0a6" if pct is None else (
            "#2f6fed" if pct < 50 else "#e8930c" if pct < 80 else "#e03e3e")
        c.create_oval(3, 3, s - 3, s - 3, fill="#f5f0e6", outline="#c9b98a", width=2)
        if pct is not None:
            c.create_arc(5, 5, s - 5, s - 5, start=90, extent=-3.6 * min(pct, 100),
                         style="arc", outline=col, width=4)
        cx = s / 2
        if pct is None:
            c.create_text(cx, cx, text="…", font=("Segoe UI", 12, "bold"))
        else:
            c.create_text(cx, cx - 5, text=f"{pct:.0f}%", font=("Segoe UI", 12, "bold"))
            c.create_text(cx, cx + 10, text=fmt_compact(reset), font=("Segoe UI", 7),
                          fill="#5f6368")

    # ------------------- 지형 -------------------
    def surfaces(self):
        wl, wtop, wr, wb = get_work_area()
        surf = [(wb, wl, wr)]
        with self.rects_lock:
            rects = list(self.win_rects)
        for (l, t, r, b) in rects:
            if t > wtop + 10:
                surf.append((t, l + 4, r - 4))
        return surf

    def walls(self):
        with self.rects_lock:
            return list(self.win_rects)

    def find_floor_below(self, x, y_from):
        s = SIZE
        best = None
        cx = x + s / 2
        for (sy, x1, x2) in self.surfaces():
            if x1 <= cx <= x2 and sy >= y_from + s - 1:
                if best is None or sy < best:
                    best = sy
        return best if best is not None else get_work_area()[3]

    def current_support(self):
        s = SIZE
        cx = self.x + s / 2
        bottom = self.y + s
        for (sy, x1, x2) in self.surfaces():
            if x1 <= cx <= x2 and abs(sy - bottom) <= 3:
                return sy
        return None

    # ------------------- 물리 -------------------
    def tick(self):
        s = SIZE
        wl, wtop, wr, wb = get_work_area()
        self._tick_n += 1
        moved = False
        anim_every = 5  # 프레임 전환 주기

        # 리셋 시각이 지난 옛 데이터가 계속 오면 1~2분 간격으로 재조회
        if self._tick_n % 2000 == 0 and self.usage:
            fh = self.usage.get("five_hour") or {}
            r = parse_iso(fh.get("resets_at", ""))
            if r is not None and remaining_parts(r) is None \
                    and time.time() - self._last_kick > 90:
                self._last_kick = time.time()
                self.force_refresh()

        if self.state == "held" or self.panel is not None or not self.walk_enabled:
            pass
        elif self.state == "idle":
            if time.time() >= self.idle_until:
                self.state = "walk"
            elif self._tick_n % 6 == 0:
                self.render_frame()  # 숨쉬기/깜빡임
        elif self.state == "fall":
            self.vy = min(self.vy + GRAVITY, MAX_FALL)
            floor = self.find_floor_below(self.x, self.y + self.vy - s)
            ny = self.y + self.vy
            if ny + s >= floor:
                ny, self.state, self.vy = floor - s, "walk", 0.0
            self.y = ny
            moved = True
        elif self.state == "climb":
            wall_x, top_y, side = self.climb_target
            # 꿈틀꿈틀: 맥동하는 속도로 상승
            pulse = 0.35 + 0.9 * abs(math.sin(self.step_phase))
            self.step_phase += 0.22
            self.y -= CLIMB_SPEED * pulse
            moved = True
            # 벽이 아직 있는지 가끔 확인
            self.climb_check += 1
            if self.climb_check % 10 == 0:
                alive = any(abs((l if side == "left" else r) - wall_x) < 16 and t < self.y + s
                            for (l, t, r, b) in self.walls())
                if not alive:
                    self.state, self.vy = "fall", 0.0
            if self.y + s <= top_y:
                self.y = top_y - s
                self.x = wall_x - s if side == "left" else float(wall_x)
                self.dir = 1 if side == "left" else -1
                self.state = "walk"
        elif self.state == "walk":
            self.step_phase += 0.3
            nx = self.x + WALK_SPEED * self.dir
            bottom = self.y + s
            moved = True

            if nx < wl:
                nx, self.dir = float(wl), 1
                self.no_wall_until = time.time() + 0.4
            elif nx + s > wr:
                nx, self.dir = float(wr - s), -1
                self.no_wall_until = time.time() + 0.4

            hit = None
            if time.time() >= self.no_wall_until:
                for (l, t, r, b) in self.walls():
                    # 창의 옆면이 몸통 높이에 걸쳐 있으면 벽
                    if t < bottom - 12 and b > self.y + 10:
                        if self.dir > 0 and self.x + s <= l + 4 and nx + s >= l - 2:
                            hit = (l, t, "left")
                            break
                        if self.dir < 0 and self.x >= r - 4 and nx <= r + 2:
                            hit = (r, t, "right")
                            break
            if hit:
                wall_x, top_y, side = hit
                if top_y > wtop + 10 and random.random() < CLIMB_PROB:
                    self.state = "climb"
                    self.climb_target = hit
                    self.climb_check = 0
                    self.x = wall_x - s if side == "left" else float(wall_x)
                    self.dir = 1 if side == "left" else -1
                else:
                    self.dir *= -1
                    self.no_wall_until = time.time() + 0.4
            else:
                self.x = nx
                if self.current_support() is None:
                    self.state, self.vy = "fall", 0.0
                elif random.random() < IDLE_PROB:
                    self.state = "idle"
                    self.idle_until = time.time() + random.uniform(1.5, 3.5)

        if moved:
            if self._tick_n % anim_every == 0:
                self.anim_frame += 1
                self.render_frame()
            elif self.layered:
                self.renderer.move(self.x, self.y)
            else:
                self.root.geometry(f"+{int(self.x)}+{int(self.y)}")

        self.root.after(TICK_MS, self.tick)

    # ------------------- 입력 -------------------
    def on_press(self, e):
        self.drag_start = (e.x_root, e.y_root, self.x, self.y)
        self.dragging = False

    def on_motion(self, e):
        if not self.drag_start:
            return
        dx = e.x_root - self.drag_start[0]
        dy = e.y_root - self.drag_start[1]
        if abs(dx) > 5 or abs(dy) > 5:
            self.dragging = True
            self.state = "held"
            self.close_panel()
        if self.dragging:
            self.x = self.drag_start[2] + dx
            self.y = self.drag_start[3] + dy
            if self.layered:
                self.renderer.move(self.x, self.y)
            else:
                self.root.geometry(f"+{int(self.x)}+{int(self.y)}")

    def on_release(self, e):
        if self.dragging:
            self.state, self.vy = "fall", 0.0
        elif time.time() >= self._suppress_click_until:
            # 더블클릭과 구분하기 위해 잠시 기다렸다가 패널 토글
            # (더블클릭 직후의 두 번째 release는 무시)
            if self._click_job:
                self.root.after_cancel(self._click_job)
            self._click_job = self.root.after(280, self._single_click)
        self.drag_start = None
        self.dragging = False

    def _single_click(self):
        self._click_job = None
        self.toggle_panel()

    def on_double_click(self, e):
        # 대기 중인 단일클릭(패널 토글) 취소 + 이어지는 release도 무시
        if self._click_job:
            self.root.after_cancel(self._click_job)
            self._click_job = None
        self._suppress_click_until = time.time() + 0.4
        self.close_panel()
        self.switch_skin()

    def switch_skin(self):
        """캐릭터 교체 — skin 폴더의 다른 세트로 랜덤 변경 (기본 양 포함)"""
        if len(self.skins) < 2:
            return
        choices = [i for i in range(len(self.skins)) if i != self.skin_idx]
        self.skin_idx = random.choice(choices)
        self.skin = self.skins[self.skin_idx]
        _sprite_cache.clear()
        self.render_frame(force=True)

    def on_right_click(self, e):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="지금 새로고침", command=self.force_refresh)
        menu.add_command(label="캐릭터 바꾸기", command=self.switch_skin)
        menu.add_command(label="걷기 끄기" if self.walk_enabled else "걷기 켜기",
                         command=self.toggle_walk)
        menu.add_separator()
        menu.add_command(label="종료", command=self.root.destroy)
        menu.tk_popup(e.x_root, e.y_root)

    def toggle_walk(self):
        self.walk_enabled = not self.walk_enabled
        if self.walk_enabled and self.state == "held":
            self.state = "fall"
        self.render_frame(force=True)

    # ------------------- 주간 한도 패널 -------------------
    def toggle_panel(self):
        if self.panel is not None:
            self.close_panel()
        else:
            self.open_panel()

    def close_panel(self):
        if self.panel is not None:
            try:
                self.panel.destroy()
            except Exception:
                pass
            self.panel = None

    def open_panel(self):
        self.panel = tk.Toplevel(self.root)
        self.panel.overrideredirect(True)
        self.panel.attributes("-topmost", True)
        self.panel.config(bg="#ffffff", highlightbackground="#d0d5dc",
                          highlightthickness=1)
        self.build_panel_content()
        self.place_panel()
        self.panel.bind("<Button-1>", lambda e: self.close_panel())

    def place_panel(self):
        self.panel.update_idletasks()
        pw, ph = self.panel.winfo_reqwidth(), self.panel.winfo_reqheight()
        wl, wtop, wr, wb = get_work_area()
        px = max(wl + 4, min(int(self.x + SIZE / 2 - pw / 2), wr - pw - 4))
        py = int(self.y - ph - 8)
        if py < wtop + 4:
            py = int(self.y + SIZE + 8)
        self.panel.geometry(f"+{px}+{py}")

    def refresh_panel(self):
        if self.panel is None:
            return
        for w in self.panel.winfo_children():
            w.destroy()
        self.build_panel_content()
        self.place_panel()

    @staticmethod
    def _hex(pct):
        if pct is None:
            return "#9aa0a6"
        if pct < 50:
            return "#2f6fed"
        if pct < 80:
            return "#e8930c"
        return "#e03e3e"

    def _row(self, parent, title, block):
        f = tk.Frame(parent, bg="#ffffff")
        f.pack(fill="x", padx=14, pady=(8, 0))
        pct = block.get("utilization") if block else None
        reset = parse_iso(block.get("resets_at", "")) if block else None
        stale = reset is not None and remaining_parts(reset) is None
        if stale:
            pct, reset = 0.0, None

        top = tk.Frame(f, bg="#ffffff")
        top.pack(fill="x")
        tk.Label(top, text=title, font=("Malgun Gothic", 9, "bold"),
                 bg="#ffffff", fg="#202124").pack(side="left")
        tk.Label(top, text=("-" if pct is None else f"{pct:.0f}% 사용됨"),
                 font=("Malgun Gothic", 9), bg="#ffffff",
                 fg=self._hex(pct)).pack(side="right")

        bar = tk.Canvas(f, width=240, height=8, bg="#e9edf2", highlightthickness=0)
        bar.pack(fill="x", pady=(3, 0))
        if pct is not None:
            bar.create_rectangle(0, 0, 240 * min(pct, 100) / 100, 8,
                                 fill=self._hex(pct), outline="")
        if stale:
            tk.Label(f, text="초기화 시각 지남 — 새 세션 시작 전",
                     font=("Malgun Gothic", 8), bg="#ffffff", fg="#9aa0a6",
                     anchor="w").pack(fill="x", pady=(2, 0))
        elif reset is not None:
            tk.Label(f, text=f"초기화: {fmt_korean(reset)}  {fmt_local_time(reset)}",
                     font=("Malgun Gothic", 8), bg="#ffffff", fg="#5f6368",
                     anchor="w").pack(fill="x", pady=(2, 0))

    def build_panel_content(self):
        p = self.panel
        tk.Label(p, text="Claude 사용량", font=("Malgun Gothic", 10, "bold"),
                 bg="#ffffff", fg="#202124").pack(anchor="w", padx=14, pady=(10, 0))
        if self.usage:
            u = self.usage
            self._row(p, "현재 세션 (5시간)", u.get("five_hour"))
            self._row(p, "주간 · 모든 모델", u.get("seven_day"))
            if u.get("seven_day_opus"):
                self._row(p, "주간 · Opus", u.get("seven_day_opus"))
            if u.get("seven_day_sonnet"):
                self._row(p, "주간 · Sonnet", u.get("seven_day_sonnet"))
            extra = u.get("extra_usage") or {}
            if extra.get("is_enabled") and extra.get("used_credits") is not None:
                limit = extra.get("monthly_limit")
                txt = f"사용 크레딧: {extra['used_credits']}" + (f" / {limit}" if limit else "")
                tk.Label(p, text=txt, font=("Malgun Gothic", 8), bg="#ffffff",
                         fg="#5f6368").pack(anchor="w", padx=14, pady=(6, 0))
        status = self.usage_err or (
            f"마지막 업데이트: {self.last_update:%H:%M:%S}" if self.last_update else "불러오는 중…")
        tk.Label(p, text=status, font=("Malgun Gothic", 8), bg="#ffffff",
                 fg="#e03e3e" if self.usage_err else "#9aa0a6",
                 justify="left").pack(anchor="w", padx=14, pady=(8, 10))

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    if not IS_WIN:
        print("이 앱은 Windows 전용입니다.")
        sys.exit(1)
    ClaudePet().run()
