# -*- coding: utf-8 -*-
"""
ClaudeCoin - 데스크탑을 돌아다니는 500원 동전 크기 Claude 사용량 위젯 (Windows)

- 동전: 현재 세션 사용량 % + 리셋까지 남은 시간 표시
- 작업표시줄 위(작업 영역 바닥)를 걸어다니고, 창을 만나면 타고 올라가거나 방향 전환
- 창 끝에서는 아래로 떨어짐 (양 데스크탑 펫 스타일)
- 좌클릭: 주간 한도 패널 열기/닫기 (사용량 %, 리셋까지 남은 일수/시간)
- 드래그: 집어서 이동 (놓으면 떨어짐)
- 우클릭: 메뉴 (걷기 켜기/끄기, 새로고침, 종료)

데이터: Claude Code 로그인 토큰(~/.claude/.credentials.json)으로
https://api.anthropic.com/api/oauth/usage 호출 (3분 간격, 429 시 백오프)

필요: Python 3.9+ (표준 라이브러리만 사용). 실행: pythonw claude_coin.py
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
COIN_SIZE = 64                 # 동전 지름(px). 500원 동전 느낌
POLL_INTERVAL = 180            # 사용량 API 폴링 주기(초)
TICK_MS = 30                   # 물리 틱(ms)
WALK_SPEED = 1.6               # 걷기 속도(px/tick)
CLIMB_SPEED = 2.2              # 벽 타기 속도(px/tick)
GRAVITY = 0.9                  # 낙하 가속
MAX_FALL = 14.0                # 최대 낙하 속도
CLIMB_PROB = 0.5               # 벽을 만났을 때 타고 올라갈 확률
IDLE_PROB = 0.003              # 틱당 잠깐 쉬어갈 확률
TRANSPARENT = "#ff00fe"        # 투명 처리용 색
USER_AGENT = "claude-code/2.0.14 (external, cli)"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code 공개 client_id
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"

# ------------------------- DPI (tkinter 좌표 = 실제 픽셀) -------------------------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import tkinter as tk
import tkinter.font as tkfont

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi

# ----------------------------- 시간 포맷 -----------------------------
KO_WD = ["월", "화", "수", "목", "금", "토", "일"]


def parse_iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def remaining_parts(reset_dt):
    """리셋까지 남은 시간 → (days, hours, minutes). 지났으면 None"""
    if reset_dt is None:
        return None
    delta = reset_dt - datetime.now(timezone.utc)
    total = int(delta.total_seconds())
    if total <= 0:
        return None
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return d, h, m


def fmt_compact(reset_dt):
    """동전 위 표시용: 3d4h / 4h15m / 15m"""
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
    """패널 표시용: 3일 4시간 후 / 4시간 15분 후 / 15분 후"""
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
    """(월) 오후 2:59 형태"""
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
            data = json.load(f)
        return data.get("claudeAiOauth") or {}
    except Exception:
        return {}


def save_credentials(oauth):
    """새로 갱신된 토큰을 .credentials.json에 다시 저장"""
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
    if not exp:
        return False
    return time.time() * 1000 > exp - 60000  # 60초 여유


def refresh_token(cred):
    """refresh_token으로 access token 갱신 후 파일에 저장"""
    rt = cred.get("refreshToken")
    if not rt:
        return None
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.load(r)
    except Exception:
        return None
    new = {
        "accessToken": res.get("access_token"),
        "expiresAt": int(time.time() * 1000) + int(res.get("expires_in", 3600)) * 1000,
    }
    if res.get("refresh_token"):
        new["refreshToken"] = res["refresh_token"]
    try:
        save_credentials(new)
    except Exception:
        pass
    cred.update(new)
    return cred


def fetch_usage():
    """usage API 호출. 반환: (data_dict, error_str)"""
    cred = load_credentials()
    if not cred.get("accessToken"):
        return None, "로그인 정보 없음\nClaude Code에 로그인하세요"
    if token_expired(cred):
        cred = refresh_token(cred)
        if not cred:
            return None, "토큰 만료\nClaude Code를 한 번 실행하세요"

    def _call(token):
        req = urllib.request.Request(USAGE_URL, headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        })
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


# ----------------------------- 창 탐색 (걸어다닐 지형) -----------------------------
GA_ROOT = 2
DWMWA_CLOAKED = 14
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080

EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def get_work_area():
    rect = wt.RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
    return rect.left, rect.top, rect.right, rect.bottom


def enum_desktop_windows(exclude_hwnds):
    """보이는 일반 창들의 (left, top, right, bottom) 목록"""
    rects = []

    def cb(hwnd, _):
        if hwnd in exclude_hwnds:
            return True
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        # 제목 없는 창 제외
        if user32.GetWindowTextLengthW(hwnd) == 0:
            return True
        # 툴윈도우 제외
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if ex & WS_EX_TOOLWINDOW:
            return True
        # UWP 숨김(cloaked) 창 제외
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
class ClaudeCoin:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("ClaudeCoin")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", TRANSPARENT)
        self.root.config(bg=TRANSPARENT)

        s = COIN_SIZE
        self.canvas = tk.Canvas(self.root, width=s, height=s,
                                bg=TRANSPARENT, highlightthickness=0)
        self.canvas.pack()

        # 상태
        self.usage = None          # API 응답
        self.usage_err = None
        self.last_update = None
        self.poll_interval = POLL_INTERVAL
        self.walk_enabled = True
        self.panel = None

        # 물리 상태
        wl, wtop, wr, wb = get_work_area()
        self.x = float(random.randint(wl + 100, max(wl + 101, wr - 200)))
        self.y = float(wb - s)
        self.dir = random.choice([-1, 1])
        self.vy = 0.0
        self.state = "fall"        # walk / fall / climb / idle / held
        self.idle_until = 0.0
        self.climb_target = None   # (wall_x, top_y, side)
        self.step_phase = 0.0
        self.win_rects = []
        self.rects_lock = threading.Lock()

        # 입력
        self.drag_start = None
        self.dragging = False

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Button-3>", self.on_right_click)

        self.root.geometry(f"{s}x{s}+{int(self.x)}+{int(self.y)}")
        self.root.update_idletasks()
        self.own_hwnd = user32.GetAncestor(self.canvas.winfo_id(), GA_ROOT)

        # 스레드: 사용량 폴링 + 창 지형 스캔
        threading.Thread(target=self.poll_loop, daemon=True).start()
        threading.Thread(target=self.scan_loop, daemon=True).start()

        self.draw_coin()
        self.root.after(TICK_MS, self.tick)

    # ------------------- 백그라운드 -------------------
    def poll_loop(self):
        while True:
            data, err = fetch_usage()
            if err == "429":
                self.poll_interval = min(self.poll_interval * 2, 900)
                self.usage_err = f"요청 제한(429) — {self.poll_interval//60}분 후 재시도"
            elif err:
                self.usage_err = err
            else:
                self.usage = data
                self.usage_err = None
                self.last_update = datetime.now()
                self.poll_interval = POLL_INTERVAL
            try:
                self.root.after(0, self.draw_coin)
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
        self.poll_interval = POLL_INTERVAL

        def _go():
            data, err = fetch_usage()
            if not err:
                self.usage, self.usage_err = data, None
                self.last_update = datetime.now()
            else:
                self.usage_err = err if err != "429" else "요청 제한(429)"
            self.root.after(0, self.draw_coin)
            self.root.after(0, self.refresh_panel)
        threading.Thread(target=_go, daemon=True).start()

    # ------------------- 그리기 -------------------
    def session_info(self):
        if not self.usage:
            return None, None
        fh = self.usage.get("five_hour") or {}
        return fh.get("utilization"), parse_iso(fh.get("resets_at", ""))

    @staticmethod
    def usage_color(pct):
        if pct is None:
            return "#9aa0a6"
        if pct < 50:
            return "#2f6fed"
        if pct < 80:
            return "#e8930c"
        return "#e03e3e"

    def draw_coin(self):
        c = self.canvas
        c.delete("all")
        s = COIN_SIZE
        pct, reset = self.session_info()

        # 걷는 다리 (걸을 때만 흔들림)
        if self.state in ("walk",) :
            sway = math.sin(self.step_phase) * 3
        else:
            sway = 0
        foot_y = s - 3
        c.create_oval(s * 0.32 - 3 + sway, foot_y - 3, s * 0.32 + 3 + sway, foot_y + 3,
                      fill="#5f6368", outline="")
        c.create_oval(s * 0.68 - 3 - sway, foot_y - 3, s * 0.68 + 3 - sway, foot_y + 3,
                      fill="#5f6368", outline="")

        # 동전 본체
        m = 3
        body_bottom = s - 6
        c.create_oval(m, m, s - m, body_bottom, fill="#f5f0e6", outline="#c9b98a", width=2)

        # 사용량 링
        if pct is not None:
            col = self.usage_color(pct)
            c.create_arc(m + 2, m + 2, s - m - 2, body_bottom - 2,
                         start=90, extent=-3.6 * min(pct, 100),
                         style="arc", outline=col, width=4)

        cx, cy = s / 2, (m + body_bottom) / 2
        if self.usage_err and pct is None:
            c.create_text(cx, cy - 5, text="!", font=("Segoe UI", 14, "bold"), fill="#e03e3e")
            c.create_text(cx, cy + 9, text="오류", font=("Malgun Gothic", 7), fill="#5f6368")
        elif pct is None:
            c.create_text(cx, cy, text="…", font=("Segoe UI", 12, "bold"), fill="#5f6368")
        else:
            c.create_text(cx, cy - 5, text=f"{pct:.0f}%",
                          font=("Segoe UI", 12, "bold"), fill="#202124")
            c.create_text(cx, cy + 10, text=fmt_compact(reset),
                          font=("Segoe UI", 7), fill="#5f6368")

    # ------------------- 물리 -------------------
    def surfaces(self):
        """서 있을 수 있는 표면 목록: (y, x1, x2)"""
        wl, wtop, wr, wb = get_work_area()
        surf = [(wb, wl, wr)]
        with self.rects_lock:
            rects = list(self.win_rects)
        for (l, t, r, b) in rects:
            if t > wtop + 10:  # 화면 맨 위에 붙은(최대화) 창 위는 제외
                surf.append((t, l + 4, r - 4))
        return surf

    def walls(self):
        with self.rects_lock:
            return list(self.win_rects)

    def find_floor_below(self, x, y_from):
        """(x, y_from) 아래에서 가장 가까운 표면 y"""
        s = COIN_SIZE
        best = None
        cx = x + s / 2
        for (sy, x1, x2) in self.surfaces():
            if x1 <= cx <= x2 and sy >= y_from + s - 1:
                if best is None or sy < best:
                    best = sy
        if best is None:
            best = get_work_area()[3]
        return best

    def current_support(self):
        """지금 딛고 있는 표면 y (없으면 None)"""
        s = COIN_SIZE
        cx = self.x + s / 2
        bottom = self.y + s
        for (sy, x1, x2) in self.surfaces():
            if x1 <= cx <= x2 and abs(sy - bottom) <= 3:
                return sy
        return None

    def tick(self):
        s = COIN_SIZE
        wl, wtop, wr, wb = get_work_area()

        if self.state == "held" or self.panel is not None or not self.walk_enabled:
            pass  # 잡혀있거나 패널이 열려있으면 정지
        elif self.state == "idle":
            if time.time() >= self.idle_until:
                self.state = "walk"
        elif self.state == "fall":
            self.vy = min(self.vy + GRAVITY, MAX_FALL)
            floor = self.find_floor_below(self.x, self.y + self.vy - s)
            ny = self.y + self.vy
            if ny + s >= floor:
                ny = floor - s
                self.state = "walk"
                self.vy = 0.0
            self.y = ny
        elif self.state == "climb":
            wall_x, top_y, side = self.climb_target
            self.y -= CLIMB_SPEED
            if self.y + s <= top_y:
                self.y = top_y - s
                # 창 위로 올라섬
                self.x = wall_x - s if side == "left" else wall_x
                self.dir = 1 if side == "left" else -1
                self.state = "walk"
        elif self.state == "walk":
            self.step_phase += 0.35
            nx = self.x + WALK_SPEED * self.dir
            bottom = self.y + s
            cx_next = nx + s / 2

            # 화면 밖 방지
            if nx < wl:
                nx, self.dir = float(wl), 1
            elif nx + s > wr:
                nx, self.dir = float(wr - s), -1

            # 벽 충돌 검사 (내 바닥보다 높이 솟은 창의 옆면)
            hit = None
            for (l, t, r, b) in self.walls():
                if t < bottom - 12 and b > bottom - 12:
                    if self.dir > 0 and nx + s >= l - 2 and self.x + s < l + 6 and cx_next < l:
                        hit = (l, t, "left"); break
                    if self.dir < 0 and nx <= r + 2 and self.x > r - 6 and cx_next > r:
                        hit = (r, t, "right"); break
            if hit:
                wall_x, top_y, side = hit
                if top_y > wtop + 10 and random.random() < CLIMB_PROB:
                    self.state = "climb"
                    self.climb_target = hit
                    self.x = wall_x - s if side == "left" else float(wall_x)
                else:
                    self.dir *= -1
            else:
                self.x = nx
                # 발밑 확인 (창이 사라졌거나 끝에 도달)
                if self.current_support() is None:
                    self.state = "fall"
                    self.vy = 0.0
                elif random.random() < IDLE_PROB:
                    self.state = "idle"
                    self.idle_until = time.time() + random.uniform(2, 5)

            if int(self.step_phase * 10) % 3 == 0:
                self.draw_coin()

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
            self.root.geometry(f"+{int(self.x)}+{int(self.y)}")

    def on_release(self, e):
        if self.dragging:
            self.state = "fall"
            self.vy = 0.0
        else:
            self.toggle_panel()
        self.drag_start = None
        self.dragging = False

    def on_right_click(self, e):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="지금 새로고침", command=self.force_refresh)
        menu.add_command(
            label="걷기 끄기" if self.walk_enabled else "걷기 켜기",
            command=self.toggle_walk)
        menu.add_separator()
        menu.add_command(label="종료", command=self.root.destroy)
        menu.tk_popup(e.x_root, e.y_root)

    def toggle_walk(self):
        self.walk_enabled = not self.walk_enabled
        if self.walk_enabled and self.state == "held":
            self.state = "fall"

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
        pw = self.panel.winfo_reqwidth()
        ph = self.panel.winfo_reqheight()
        wl, wtop, wr, wb = get_work_area()
        px = int(self.x + COIN_SIZE / 2 - pw / 2)
        px = max(wl + 4, min(px, wr - pw - 4))
        py = int(self.y - ph - 8)
        if py < wtop + 4:
            py = int(self.y + COIN_SIZE + 8)
        self.panel.geometry(f"+{px}+{py}")

    def refresh_panel(self):
        if self.panel is None:
            return
        for w in self.panel.winfo_children():
            w.destroy()
        self.build_panel_content()
        self.place_panel()

    def _row(self, parent, title, block):
        f = tk.Frame(parent, bg="#ffffff")
        f.pack(fill="x", padx=14, pady=(8, 0))
        pct = block.get("utilization") if block else None
        reset = parse_iso(block.get("resets_at", "")) if block else None

        top = tk.Frame(f, bg="#ffffff")
        top.pack(fill="x")
        tk.Label(top, text=title, font=("Malgun Gothic", 9, "bold"),
                 bg="#ffffff", fg="#202124").pack(side="left")
        tk.Label(top, text=("-" if pct is None else f"{pct:.0f}% 사용됨"),
                 font=("Malgun Gothic", 9), bg="#ffffff",
                 fg=self.usage_color(pct)).pack(side="right")

        # 진행 바
        bar = tk.Canvas(f, width=240, height=8, bg="#e9edf2", highlightthickness=0)
        bar.pack(fill="x", pady=(3, 0))
        if pct is not None:
            bar.create_rectangle(0, 0, 240 * min(pct, 100) / 100, 8,
                                 fill=self.usage_color(pct), outline="")

        if reset is not None:
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
            if extra.get("is_enabled"):
                used = extra.get("used_credits")
                limit = extra.get("monthly_limit")
                if used is not None:
                    txt = f"사용 크레딧: {used}" + (f" / {limit}" if limit else "")
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
    if sys.platform != "win32":
        print("이 앱은 Windows 전용입니다.")
        sys.exit(1)
    ClaudeCoin().run()
