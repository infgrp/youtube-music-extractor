"""
YouTube Music Extractor
-----------------------
키워드로 유튜브 영상을 검색 -> 선택한 영상의 트랙리스트(챕터/설명) 추출 ->
선택한 트랙만 각각 MP3 파일로 저장.

필요:
    pip install yt-dlp
    ffmpeg 실행 파일이 PATH 에 있어야 함 (https://ffmpeg.org/)
"""

from __future__ import annotations

import os
import re
import sys
import math
import shutil
import subprocess
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font as tkfont
from dataclasses import dataclass, field
from typing import Optional

from yt_dlp import YoutubeDL


# ---------- Windows DPI 인식 (흐릿한 폰트 방지) ----------

def _enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    import ctypes
    try:
        # Per-Monitor v2 (Windows 10 1703+)
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
        return
    except Exception:
        pass
    try:
        # Per-Monitor
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ---------- 데이터 구조 ----------

@dataclass
class VideoItem:
    video_id: str
    title: str
    uploader: str
    duration: int  # seconds
    url: str

    def label(self) -> str:
        mm, ss = divmod(self.duration or 0, 60)
        hh, mm = divmod(mm, 60)
        t = f"{hh:02d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
        return f"[{t}] {self.title}  —  {self.uploader}"


@dataclass
class Track:
    video: VideoItem
    index: int
    title: str
    start: float          # seconds
    end: Optional[float]  # seconds, None => 영상 끝까지

    def label(self) -> str:
        def fmt(s):
            s = int(s)
            mm, ss = divmod(s, 60)
            hh, mm = divmod(mm, 60)
            return f"{hh:02d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
        rng = f"{fmt(self.start)}-{fmt(self.end) if self.end else '끝'}"
        return f"[{rng}] {self.title}  ({self.video.title[:40]})"


# ---------- 유튜브 / 트랙리스트 파싱 ----------

def _hms_to_seconds(text: str) -> Optional[int]:
    parts = text.strip().split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == 2:
        m, s = parts
        return m * 60 + s
    if len(parts) == 3:
        h, m, s = parts
        return h * 3600 + m * 60 + s
    return None


TIMESTAMP_RE = re.compile(
    r"""
    (?P<ts>\d{1,2}:\d{2}(?::\d{2})?)    # 00:00 또는 00:00:00
    """,
    re.VERBOSE,
)


def parse_tracklist_from_description(description: str) -> list[tuple[str, int]]:
    """설명에서 (제목, 시작초) 리스트를 추출. 순서는 타임스탬프 오름차순."""
    if not description:
        return []
    results: list[tuple[str, int]] = []
    for line in description.splitlines():
        m = TIMESTAMP_RE.search(line)
        if not m:
            continue
        secs = _hms_to_seconds(m.group("ts"))
        if secs is None:
            continue
        # 타임스탬프 양쪽의 텍스트 중 더 긴 쪽을 제목으로
        before = line[: m.start()].strip(" -–—·.•:·|[](){}")
        after = line[m.end():].strip(" -–—·.•:·|[](){}")
        # 선두의 트랙번호(1., 01), 1)) 제거
        def clean_num(s: str) -> str:
            return re.sub(r"^\s*\d{1,3}\s*[.)\]\-]\s*", "", s).strip()
        title = clean_num(after) if len(after) >= len(before) else clean_num(before)
        if not title:
            continue
        results.append((title, secs))
    # 중복(같은 시작초) 제거 & 정렬
    seen = set()
    out = []
    for t, s in sorted(results, key=lambda x: x[1]):
        if s in seen:
            continue
        seen.add(s)
        out.append((t, s))
    return out


def extract_tracks(video: VideoItem, info: dict) -> list[Track]:
    """info(yt-dlp extract_info 결과)에서 챕터 우선, 없으면 설명에서 트랙 파싱."""
    tracks: list[Track] = []
    chapters = info.get("chapters") or []
    if chapters:
        for i, ch in enumerate(chapters):
            tracks.append(Track(
                video=video,
                index=i + 1,
                title=ch.get("title") or f"Track {i+1}",
                start=float(ch.get("start_time") or 0),
                end=(float(ch["end_time"]) if ch.get("end_time") is not None else None),
            ))
        return tracks

    parsed = parse_tracklist_from_description(info.get("description") or "")
    duration = float(info.get("duration") or 0) or None
    for i, (title, start) in enumerate(parsed):
        end = float(parsed[i + 1][1]) if i + 1 < len(parsed) else duration
        tracks.append(Track(video=video, index=i + 1, title=title,
                            start=float(start), end=end))
    return tracks


# ---------- yt-dlp 래퍼 ----------

class YtWrapper:
    SEARCH_OPTS = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",  # 검색 결과에는 메타데이터만
    }
    INFO_OPTS = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }

    @staticmethod
    def search(keyword: str, n: int = 15) -> list[VideoItem]:
        query = f"ytsearch{n}:{keyword}"
        with YoutubeDL(YtWrapper.SEARCH_OPTS) as ydl:
            info = ydl.extract_info(query, download=False)
        entries = (info or {}).get("entries") or []
        out = []
        for e in entries:
            if not e:
                continue
            vid = e.get("id") or ""
            url = e.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else "")
            out.append(VideoItem(
                video_id=vid,
                title=e.get("title") or "(제목 없음)",
                uploader=e.get("uploader") or e.get("channel") or "",
                duration=int(e.get("duration") or 0),
                url=url,
            ))
        return out

    @staticmethod
    def full_info(video: VideoItem) -> dict:
        with YoutubeDL(YtWrapper.INFO_OPTS) as ydl:
            return ydl.extract_info(video.url, download=False)

    @staticmethod
    def download_audio(video: VideoItem, out_dir: str, progress_hook=None) -> str:
        """영상의 오디오를 m4a/webm 로 다운로드. 반환: 저장된 파일 경로."""
        os.makedirs(out_dir, exist_ok=True)
        outtmpl = os.path.join(out_dir, f"{video.video_id}.%(ext)s")
        opts = {
            "quiet": True,
            "no_warnings": True,
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": outtmpl,
            "noplaylist": True,
        }
        if progress_hook:
            opts["progress_hooks"] = [progress_hook]
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video.url, download=True)
            return ydl.prepare_filename(info)


# ---------- ffmpeg 호출 ----------

def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:150] or "track"


def split_to_mp3(source_path: str, track: Track, out_dir: str,
                 start_offset: float = 0.0) -> str:
    os.makedirs(out_dir, exist_ok=True)
    base = f"{track.index:02d} - {_safe_filename(track.title)}"
    suffix = f" [{track.video.video_id}]" if track.video.video_id else ""
    out_path = os.path.join(out_dir, f"{base}{suffix}.mp3")

    # 시작점만 offset 만큼 뒤로 민다 — 앞 곡 꼬리를 스킵하기 위해.
    # 첫 트랙(start=0)은 그대로. 끝은 유튜브가 표시한 경계 그대로 두어
    # 다음 곡 인트로가 섞이지 않게 한다 (이전에 end 에도 offset 을 더해
    # 다음 곡 첫 부분이 현재 트랙 뒤에 붙는 버그가 있었음).
    start = track.start + (start_offset if track.start > 0.0 else 0.0)
    end = track.end

    # 샘플 정확한 컷을 위해 하이브리드 seek 사용:
    #   1) INPUT 쪽 -ss 로 target 약 2초 전까지 빠르게 이동
    #   2) OUTPUT 쪽 -ss 로 남은 거리를 디코더가 샘플 단위로 버림
    # 이렇게 하면 opus/webm 같이 컨테이너 seek 가 거친 포맷에서도
    # 최종 출력이 정확히 start 에서 시작한다.
    PAD = 2.0
    fast_seek = max(0.0, start - PAD)
    fine_seek = start - fast_seek  # 0.0 ~ PAD

    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if fast_seek > 0.0:
        cmd += ["-ss", f"{fast_seek:.3f}"]
    cmd += ["-i", source_path]
    if fine_seek > 0.0:
        cmd += ["-ss", f"{fine_seek:.3f}"]
    if end is not None and end > start:
        cmd += ["-t", f"{(end - start):.3f}"]
    cmd += [
        "-vn",
        "-acodec", "libmp3lame",
        "-b:a", "320k",
        "-ar", "48000",
        "-ac", "2",
        "-id3v2_version", "3",
        "-metadata", f"title={track.title}",
        "-metadata", f"artist={track.video.uploader}",
        "-metadata", f"album={track.video.title}",
        "-metadata", f"track={track.index}",
        out_path,
    ]
    subprocess.run(cmd, check=True)
    return out_path


# ---------- GUI ----------

# ---------- 테마 색상 ----------

COLORS = {
    # --- Backgrounds (딥 차콜) ---
    "bg":          "#0d0e14",
    "surface":     "#151821",
    "surface_alt": "#1b1f2a",
    "surface_hi":  "#22263a",

    # --- Text ---
    "text":        "#f2f4f8",
    "text_sub":    "#c4c9d4",
    "muted":       "#7c8292",
    "dim":         "#565c6a",

    # --- Borders ---
    "border":      "#1e222d",
    "border_str":  "#2a2f3d",

    # --- Primary: 바이닐 라벨 골드 ---
    "primary":     "#d4a853",
    "primary_h":   "#e6bd63",
    "primary_d":   "#a88440",
    "primary_l":   "#2a2218",

    # --- Accent: 딥 틸 (포인트 색) ---
    "accent":      "#3d9aa1",
    "accent_h":    "#4fb0b6",

    # --- Status ---
    "success":     "#6ec175",
    "success_bg":  "#152a18",
    "warn":        "#e0b055",
    "warn_bg":     "#2a2218",
    "danger":      "#d47272",
    "danger_bg":   "#2a1717",
    "info":        "#7a9ed4",
    "info_bg":     "#152036",

    # --- Lists ---
    "zebra":       "#12141b",
    "row_even":    "#15181f",
    "selection":   "#2a2218",

    # --- Player(하단 상태) 바 ---
    "status_bg":   "#07080b",
    "status_bg2":  "#101218",
    "status_fg":   "#edeff4",
    "status_acc":  "#d4a853",

    # --- Vinyl ---
    "vinyl":       "#0a0b0e",
    "vinyl_edge":  "#1a1c22",
    "vinyl_label": "#b8905a",
    "vinyl_label_rim": "#8a6d44",
    "vinyl_label_hi":  "#e6bd63",
    "vinyl_mark":  "#2a1f14",
}


class LPRecord(tk.Canvas):
    """회전하는 LP 바이닐 — 진행 중 인디케이터."""

    def __init__(self, parent, size: int = 150, bg: str = "#07080b"):
        super().__init__(parent, width=size, height=size,
                         bg=bg, highlightthickness=0, bd=0)
        self.size = size
        self.bg = bg
        self.angle = 0.0
        self.spinning = False
        self._marker_item = None
        self._center_item = None
        self._draw_static()
        self._draw_marker()

    def _draw_static(self):
        C = COLORS
        cx = cy = self.size / 2
        r = self.size / 2 - 6

        # 외곽 서브틀 테두리
        self.create_oval(cx - r - 2, cy - r - 2,
                         cx + r + 2, cy + r + 2,
                         outline=C["border_str"], width=1)
        # LP 본체
        self.create_oval(cx - r, cy - r, cx + r, cy + r,
                         fill=C["vinyl"], outline=C["vinyl_edge"], width=1)
        # 그루브 (동심원) — 짝/홀로 음영차
        ring_outer = r - 8
        ring_inner = r * 0.38
        n = max(14, int((ring_outer - ring_inner) / 1.3))
        for i in range(n):
            t = i / max(n - 1, 1)
            gr = ring_outer - (ring_outer - ring_inner) * t
            shade = "#171a23" if i % 2 == 0 else "#0f1219"
            self.create_oval(cx - gr, cy - gr, cx + gr, cy + gr,
                             outline=shade, width=1)
        # 라벨 (가운데 원)
        lr = ring_inner - 4
        self.create_oval(cx - lr, cy - lr, cx + lr, cy + lr,
                         fill=C["vinyl_label"],
                         outline=C["vinyl_label_rim"], width=1)
        # 라벨 상단 하이라이트 호 (윤기)
        self.create_arc(cx - lr + 3, cy - lr + 3,
                        cx + lr - 6, cy + lr - 6,
                        start=50, extent=85, style="arc",
                        outline=C["vinyl_label_hi"], width=2)

    def _draw_marker(self):
        """회전마커 + 중심 홀을 다시 그린다."""
        if self._marker_item is not None:
            self.delete(self._marker_item)
        if self._center_item is not None:
            self.delete(self._center_item)
        cx = cy = self.size / 2
        r = self.size / 2 - 6
        lr = r * 0.38 - 4
        a = math.radians(self.angle)
        mx = cx + lr * 0.55 * math.cos(a)
        my = cy + lr * 0.55 * math.sin(a)
        self._marker_item = self.create_oval(
            mx - 4, my - 4, mx + 4, my + 4,
            fill=COLORS["vinyl_mark"], outline=""
        )
        # 중심 홀
        self._center_item = self.create_oval(
            cx - 3, cy - 3, cx + 3, cy + 3,
            fill=self.bg, outline="#111217"
        )

    def start(self):
        if not self.spinning:
            self.spinning = True
            self._tick()

    def stop(self):
        self.spinning = False

    def _tick(self):
        if not self.spinning:
            return
        self.angle = (self.angle + 5) % 360
        self._draw_marker()
        self.after(40, self._tick)


class App(tk.Tk):
    def __init__(self):
        _enable_dpi_awareness()
        super().__init__()
        self.title("YouTube Music Extractor")
        self.geometry("1180x820")
        self.minsize(980, 640)
        self.configure(bg=COLORS["bg"])

        # DPI 에 맞춘 Tk 스케일링 (72pt = 1") — 흐릿한 폰트 방지
        try:
            dpi = self.winfo_fpixels("1i")
            self.tk.call("tk", "scaling", max(1.0, dpi / 72.0))
        except tk.TclError:
            pass

        # 기본 폰트 교체 — Windows 에서 Segoe UI Variable (있으면) → Segoe UI → 맑은 고딕
        self._pick_fonts()

        self.videos: list[VideoItem] = []
        self.video_selected: dict[str, bool] = {}
        self.tracks: list[Track] = []
        self.track_selected: dict[int, bool] = {}
        self.output_dir = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "Downloads", "YTMusic")
        )
        self.start_offset_var = tk.DoubleVar(value=0.25)
        self.msg_queue: "queue.Queue[tuple]" = queue.Queue()

        self._apply_styles()
        self._build_ui()
        self.after(100, self._pump_queue)

    # ----- 폰트 -----

    def _pick_fonts(self):
        available = set(tkfont.families(self))

        def pick(*candidates, fallback="TkDefaultFont"):
            for c in candidates:
                if c in available:
                    return c
            return fallback

        # Windows 11: Segoe UI Variable Display/Text, 10: Segoe UI
        self.F_UI = pick("Segoe UI Variable Display", "Segoe UI", "맑은 고딕",
                         "Malgun Gothic", "Noto Sans KR", fallback="TkDefaultFont")
        self.F_MONO = pick("JetBrains Mono", "Cascadia Mono", "Cascadia Code",
                           "Consolas", fallback="TkFixedFont")

        # 기본 tk 폰트들을 일괄 교체 (메시지박스/메뉴 등에도 적용)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                     "TkHeadingFont", "TkCaptionFont", "TkSmallCaptionFont",
                     "TkIconFont", "TkTooltipFont"):
            try:
                f = tkfont.nametofont(name)
                f.configure(family=self.F_UI, size=10)
            except tk.TclError:
                pass

    # ----- 스타일 -----

    def _apply_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        C = COLORS
        ui = self.F_UI

        f_body   = (ui, 10)
        f_bold   = (ui, 10, "bold")
        f_small  = (ui, 9)
        f_hdr    = (ui, 11, "bold")
        f_title  = (ui, 22, "bold")
        f_subtitle = (ui, 10)
        f_badge  = (ui, 9, "bold")

        # 기본
        style.configure(".", background=C["bg"], foreground=C["text"],
                        font=f_body, borderwidth=0,
                        focuscolor=C["primary"])
        style.configure("TFrame", background=C["bg"])
        style.configure("Surface.TFrame", background=C["surface"])
        style.configure("SurfaceAlt.TFrame", background=C["surface_alt"])
        style.configure("StatusBar.TFrame", background=C["status_bg"])

        # 레이블
        style.configure("TLabel", background=C["bg"], foreground=C["text"])
        style.configure("Surface.TLabel",
                        background=C["surface"], foreground=C["text"])
        style.configure("SurfaceAlt.TLabel",
                        background=C["surface_alt"], foreground=C["text"])
        style.configure("Title.TLabel", background=C["bg"],
                        foreground=C["text"], font=f_title)
        style.configure("Subtitle.TLabel", background=C["bg"],
                        foreground=C["muted"], font=f_subtitle)
        style.configure("SectionHdr.TLabel", background=C["surface"],
                        foreground=C["muted"], font=f_badge)
        style.configure("Muted.TLabel", background=C["bg"],
                        foreground=C["muted"], font=f_small)
        style.configure("FieldLabel.TLabel", background=C["surface"],
                        foreground=C["text_sub"], font=f_bold)

        # 버튼 — 보조 (고스트, 다크)
        style.configure("TButton",
                        background=C["surface_alt"],
                        foreground=C["text_sub"],
                        bordercolor=C["border_str"],
                        padding=(14, 9),
                        borderwidth=1,
                        relief="flat",
                        focusthickness=0,
                        font=f_bold)
        style.map("TButton",
                  background=[("active", C["surface_hi"]),
                              ("pressed", C["border_str"]),
                              ("disabled", C["surface_alt"])],
                  foreground=[("active", C["text"]),
                              ("disabled", C["dim"])],
                  bordercolor=[("active", C["primary"]),
                               ("focus", C["primary"])])

        # 버튼 — Primary (바이닐 골드)
        style.configure("Primary.TButton",
                        background=C["primary"],
                        foreground="#1a1408",
                        bordercolor=C["primary"],
                        padding=(18, 10),
                        borderwidth=0,
                        relief="flat",
                        focusthickness=0,
                        font=f_bold)
        style.map("Primary.TButton",
                  background=[("active", C["primary_h"]),
                              ("pressed", C["primary_d"]),
                              ("disabled", "#4a3d20")],
                  foreground=[("active", "#1a1408"),
                              ("disabled", "#8a7850")])

        # 버튼 — Accent (딥 틸)
        style.configure("Accent.TButton",
                        background=C["accent"],
                        foreground="#07161a",
                        bordercolor=C["accent"],
                        padding=(18, 10),
                        borderwidth=0,
                        relief="flat",
                        focusthickness=0,
                        font=f_bold)
        style.map("Accent.TButton",
                  background=[("active", C["accent_h"]),
                              ("pressed", "#2f7e84"),
                              ("disabled", "#1e4a4d")],
                  foreground=[("active", "#07161a"),
                              ("disabled", "#5e8288")])

        # 버튼 — Ghost (Danger 외곽)
        style.configure("Ghost.TButton",
                        background=C["surface"],
                        foreground=C["danger"],
                        bordercolor=C["danger"],
                        padding=(14, 9),
                        borderwidth=1,
                        relief="flat",
                        font=f_bold)
        style.map("Ghost.TButton",
                  background=[("active", C["danger_bg"]),
                              ("pressed", C["danger_bg"])])

        # Entry / Spinbox (다크 필드)
        style.configure("TEntry",
                        fieldbackground=C["surface_alt"],
                        foreground=C["text"],
                        bordercolor=C["border_str"],
                        lightcolor=C["border_str"],
                        darkcolor=C["border_str"],
                        insertcolor=C["primary"],
                        padding=9)
        style.map("TEntry",
                  bordercolor=[("focus", C["primary"])],
                  lightcolor=[("focus", C["primary"])],
                  darkcolor=[("focus", C["primary"])])

        style.configure("TSpinbox",
                        fieldbackground=C["surface_alt"],
                        foreground=C["text"],
                        bordercolor=C["border_str"],
                        lightcolor=C["border_str"],
                        darkcolor=C["border_str"],
                        arrowcolor=C["primary"],
                        padding=7,
                        arrowsize=14)
        style.map("TSpinbox",
                  bordercolor=[("focus", C["primary"])])

        # Treeview (다크 리스트)
        style.configure("Treeview",
                        background=C["surface"],
                        fieldbackground=C["surface"],
                        foreground=C["text"],
                        rowheight=34,
                        bordercolor=C["border"],
                        borderwidth=0,
                        relief="flat",
                        font=f_body)
        style.configure("Treeview.Heading",
                        background=C["surface_alt"],
                        foreground=C["muted"],
                        bordercolor=C["border"],
                        font=f_badge,
                        padding=(10, 10),
                        relief="flat")
        style.map("Treeview.Heading",
                  background=[("active", C["surface_hi"])],
                  foreground=[("active", C["primary"])])
        style.map("Treeview",
                  background=[("selected", C["selection"])],
                  foreground=[("selected", C["primary_h"])])

        # Progressbar (얇은 바, 골드)
        style.configure("Horizontal.TProgressbar",
                        background=C["primary"],
                        troughcolor=C["status_bg2"],
                        bordercolor=C["status_bg2"],
                        lightcolor=C["primary"],
                        darkcolor=C["primary"],
                        thickness=6)

        # Scrollbar (슬림)
        style.configure("Vertical.TScrollbar",
                        background=C["surface_alt"],
                        troughcolor=C["surface"],
                        bordercolor=C["surface"],
                        arrowcolor=C["muted"],
                        lightcolor=C["surface_alt"],
                        darkcolor=C["surface_alt"],
                        gripcount=0,
                        arrowsize=12,
                        width=10)
        style.map("Vertical.TScrollbar",
                  background=[("active", C["border_str"])])

        style.configure("TSeparator", background=C["border"])
        style.configure("TPanedwindow", background=C["bg"])

    # ----- UI 구성 -----

    def _build_ui(self):
        C = COLORS
        ui = self.F_UI

        # ── 헤더 (미니 LP 로고) ──
        header = ttk.Frame(self, padding=(28, 18, 28, 4))
        header.pack(fill="x")
        brand = ttk.Frame(header)
        brand.pack(side="left")

        # 작은 정적 LP — 브랜드 마크
        logo = LPRecord(brand, size=54, bg=C["bg"])
        logo.pack(side="left", padx=(0, 14))

        ttext = ttk.Frame(brand)
        ttext.pack(side="left", anchor="w")
        ttk.Label(ttext, text="YouTube Music Extractor",
                  style="Title.TLabel").pack(anchor="w")
        ttk.Label(ttext,
                  text="키워드로 영상을 찾아 챕터별 트랙을 최고 음질 MP3 로 추출합니다.",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(2, 0))

        # 헤더 하단 구분선 (골드 + 페이드)
        sep = tk.Frame(self, bg=C["primary"], height=1)
        sep.pack(fill="x", padx=28, pady=(6, 0))

        # ── 검색 카드 (다크) ──
        search_card = tk.Frame(self, bg=C["surface"],
                               highlightbackground=C["border"],
                               highlightthickness=1)
        search_card.pack(fill="x", padx=28, pady=(14, 8))

        inner = ttk.Frame(search_card, style="Surface.TFrame", padding=(20, 18))
        inner.pack(fill="x")

        ttk.Label(inner, text="검색어",
                  style="FieldLabel.TLabel").grid(row=0, column=0, sticky="w")
        self.keyword_var = tk.StringVar()
        entry = ttk.Entry(inner, textvariable=self.keyword_var, font=(ui, 11))
        entry.grid(row=1, column=0, sticky="we", padx=(0, 10), pady=(4, 0))
        entry.bind("<Return>", lambda _e: self.on_search())

        ttk.Label(inner, text="결과 개수",
                  style="FieldLabel.TLabel").grid(row=0, column=1, sticky="w")
        self.count_var = tk.IntVar(value=15)
        ttk.Spinbox(inner, from_=5, to=50, width=6,
                    textvariable=self.count_var, font=(ui, 11))\
            .grid(row=1, column=1, sticky="w", padx=(0, 10), pady=(4, 0))

        self.btn_search = ttk.Button(inner, text="검색",
                                     style="Primary.TButton",
                                     command=self.on_search)
        self.btn_search.grid(row=1, column=2, sticky="we", pady=(4, 0))

        # 2행: 저장 폴더, 오프셋
        ttk.Label(inner, text="저장 폴더",
                  style="FieldLabel.TLabel").grid(row=2, column=0, sticky="w",
                                                   pady=(14, 0))
        folder_row = ttk.Frame(inner, style="Surface.TFrame")
        folder_row.grid(row=3, column=0, sticky="we",
                        padx=(0, 10), pady=(4, 0))
        folder_row.columnconfigure(0, weight=1)
        ttk.Entry(folder_row, textvariable=self.output_dir, font=(ui, 10))\
            .grid(row=0, column=0, sticky="we")
        ttk.Button(folder_row, text="찾아보기", command=self.on_pick_dir)\
            .grid(row=0, column=1, padx=(8, 0))

        ttk.Label(inner, text="시작 오프셋(초)",
                  style="FieldLabel.TLabel").grid(row=2, column=1, sticky="w",
                                                   pady=(14, 0))
        off_row = ttk.Frame(inner, style="Surface.TFrame")
        off_row.grid(row=3, column=1, sticky="w", padx=(0, 10), pady=(4, 0))
        ttk.Spinbox(off_row, from_=0.0, to=2.0, increment=0.05, width=6,
                    textvariable=self.start_offset_var, format="%.2f",
                    font=(ui, 11))\
            .pack(side="left")
        ttk.Label(off_row, text=" 앞 곡 꼬리 제거용",
                  background=C["surface"], foreground=C["muted"],
                  font=(ui, 9)).pack(side="left", padx=(6, 0))

        inner.columnconfigure(0, weight=3)
        inner.columnconfigure(1, weight=1)
        inner.columnconfigure(2, weight=0)

        # ── 본문: 좌(영상) / 우(트랙) ──
        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=28, pady=(6, 6))

        # ─── 좌: 검색 결과 카드 ───
        left = tk.Frame(body, bg=C["surface"],
                        highlightbackground=C["border"], highlightthickness=1)
        body.add(left, weight=1)
        self._build_list_header(left,
            title="검색 결과",
            hint="체크박스로 대상 선택 · 행 클릭 후 Delete 로 제거")

        left_btns = ttk.Frame(left, style="Surface.TFrame")
        left_btns.pack(side="bottom", fill="x", padx=14, pady=(6, 14))
        ttk.Button(left_btns, text="전체 선택",
                   command=lambda: self._toggle_all_videos(True)).pack(side="left", padx=(0, 6))
        ttk.Button(left_btns, text="전체 해제",
                   command=lambda: self._toggle_all_videos(False)).pack(side="left", padx=(0, 6))
        ttk.Button(left_btns, text="목록에서 제거",
                   style="Ghost.TButton",
                   command=self.on_remove_videos).pack(side="left")
        self.btn_extract = ttk.Button(left_btns, text="선택 영상 → 트랙 추출",
                                      style="Primary.TButton",
                                      command=self.on_extract_tracks)
        self.btn_extract.pack(side="right")

        tv_wrap_v = ttk.Frame(left, style="Surface.TFrame")
        tv_wrap_v.pack(side="top", fill="both", expand=True,
                       padx=14, pady=(0, 6))

        cols_v = ("sel", "dur", "title", "uploader")
        self.tv_videos = ttk.Treeview(tv_wrap_v, columns=cols_v, show="headings",
                                       selectmode="extended")
        for c, t in zip(cols_v, ("◉", "길이", "제목", "채널")):
            self.tv_videos.heading(c, text=t)
        self.tv_videos.column("sel", width=48, anchor="center", stretch=False)
        self.tv_videos.column("dur", width=82, anchor="center", stretch=False)
        self.tv_videos.column("title", width=380)
        self.tv_videos.column("uploader", width=160)
        vsb = ttk.Scrollbar(tv_wrap_v, orient="vertical",
                            command=self.tv_videos.yview)
        self.tv_videos.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tv_videos.pack(side="left", fill="both", expand=True)
        self.tv_videos.tag_configure("odd", background=C["zebra"],
                                     foreground=C["text"])
        self.tv_videos.tag_configure("even", background=C["surface"],
                                     foreground=C["text"])
        self.tv_videos.tag_configure("picked", background=C["selection"],
                                     foreground=C["primary_h"])
        self.tv_videos.bind("<Button-1>", self._on_video_single_click)
        self.tv_videos.bind("<Double-1>", self._on_video_double_click)
        self.tv_videos.bind("<Delete>", lambda _e: self.on_remove_videos())

        # ─── 우: 트랙 카드 ───
        right = tk.Frame(body, bg=C["surface"],
                         highlightbackground=C["border"], highlightthickness=1)
        body.add(right, weight=1)
        self._build_list_header(right,
            title="트랙",
            hint="상태:  ○ 대기 · ◉ 재생중 · ✓ 완료 · ✕ 실패")

        right_btns = ttk.Frame(right, style="Surface.TFrame")
        right_btns.pack(side="bottom", fill="x", padx=14, pady=(6, 14))
        ttk.Button(right_btns, text="전체 선택",
                   command=lambda: self._toggle_all_tracks(True)).pack(side="left", padx=(0, 6))
        ttk.Button(right_btns, text="전체 해제",
                   command=lambda: self._toggle_all_tracks(False)).pack(side="left", padx=(0, 6))
        ttk.Button(right_btns, text="목록에서 제거",
                   style="Ghost.TButton",
                   command=self.on_remove_tracks).pack(side="left")
        self.btn_download = ttk.Button(right_btns, text="선택 트랙 MP3 저장",
                                       style="Accent.TButton",
                                       command=self.on_download)
        self.btn_download.pack(side="right")

        tv_wrap_t = ttk.Frame(right, style="Surface.TFrame")
        tv_wrap_t.pack(side="top", fill="both", expand=True,
                       padx=14, pady=(0, 6))

        cols_t = ("sel", "status", "range", "title", "source")
        self.tv_tracks = ttk.Treeview(tv_wrap_t, columns=cols_t, show="headings",
                                       selectmode="extended")
        for c, t in zip(cols_t, ("◉", "상태", "구간", "트랙 제목", "원본 영상")):
            self.tv_tracks.heading(c, text=t)
        self.tv_tracks.column("sel", width=48, anchor="center", stretch=False)
        self.tv_tracks.column("status", width=110, anchor="center", stretch=False)
        self.tv_tracks.column("range", width=130, anchor="center", stretch=False)
        self.tv_tracks.column("title", width=260)
        self.tv_tracks.column("source", width=200)
        vsb2 = ttk.Scrollbar(tv_wrap_t, orient="vertical",
                             command=self.tv_tracks.yview)
        self.tv_tracks.configure(yscrollcommand=vsb2.set)
        vsb2.pack(side="right", fill="y")
        self.tv_tracks.pack(side="left", fill="both", expand=True)
        self.tv_tracks.tag_configure("odd",  background=C["zebra"],
                                     foreground=C["text"])
        self.tv_tracks.tag_configure("even", background=C["surface"],
                                     foreground=C["text"])
        self.tv_tracks.tag_configure("pending", background=C["surface_alt"],
                                     foreground=C["muted"])
        self.tv_tracks.tag_configure("running", background=C["primary_l"],
                                     foreground=C["primary_h"])
        self.tv_tracks.tag_configure("done", background=C["success_bg"],
                                     foreground=C["success"])
        self.tv_tracks.tag_configure("failed", background=C["danger_bg"],
                                     foreground=C["danger"])
        self.tv_tracks.bind("<Button-1>", self._on_track_click)
        self.tv_tracks.bind("<Double-1>", self._on_track_double_click)
        self.tv_tracks.bind("<Delete>", lambda _e: self.on_remove_tracks())

        # ── 플레이어 바 (LP + stage/status/progress) ──
        player = tk.Frame(self, bg=C["status_bg"])
        player.pack(fill="x", side="bottom")

        # 상단 가느다란 골드 라인
        tk.Frame(player, bg=C["primary"], height=1).pack(fill="x")

        # LP 원판 (좌) + 정보 스택 (우)
        player_inner = tk.Frame(player, bg=C["status_bg"])
        player_inner.pack(fill="x", padx=28, pady=18)

        self.lp = LPRecord(player_inner, size=120, bg=C["status_bg"])
        self.lp.pack(side="left", padx=(0, 22))

        info = tk.Frame(player_inner, bg=C["status_bg"])
        info.pack(side="left", fill="both", expand=True)

        # 상단 라인: stage (좌) · 진행 수치 (우)
        line1 = tk.Frame(info, bg=C["status_bg"])
        line1.pack(fill="x")

        self.stage_var = tk.StringVar(value="◯  대기 중")
        tk.Label(line1, textvariable=self.stage_var,
                 bg=C["status_bg"], fg=C["status_acc"],
                 font=(ui, 12, "bold")).pack(side="left")

        self.progress_label_var = tk.StringVar(value="")
        tk.Label(line1, textvariable=self.progress_label_var,
                 bg=C["status_bg"], fg=C["status_fg"],
                 font=(self.F_MONO, 9)).pack(side="right")

        # 진행 바 — 얇은 골드 바
        self.progress = ttk.Progressbar(info, mode="determinate",
                                        maximum=100, value=0)
        self.progress.pack(fill="x", pady=(10, 10))

        # 하단 상태 텍스트
        self.status_var = tk.StringVar(
            value="키워드를 입력하고 검색을 시작하세요.")
        tk.Label(info, textvariable=self.status_var,
                 bg=C["status_bg"], fg=C["muted"],
                 anchor="w", font=(ui, 9)).pack(fill="x")

    def _build_list_header(self, parent, title: str, hint: str):
        """카드 상단에 섹션 제목 + 보조 설명."""
        C = COLORS
        ui = self.F_UI
        hdr = ttk.Frame(parent, style="Surface.TFrame")
        hdr.pack(side="top", fill="x", padx=14, pady=(14, 6))
        tk.Label(hdr, text=title, bg=C["surface"], fg=C["text"],
                 font=(ui, 12, "bold")).pack(side="left")
        tk.Label(hdr, text="   " + hint, bg=C["surface"], fg=C["muted"],
                 font=(ui, 9)).pack(side="left")
        # 구분선
        tk.Frame(parent, bg=C["border"], height=1)\
            .pack(side="top", fill="x", padx=14)

    # ----- 이벤트: 검색 -----

    def on_pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.output_dir.get() or ".")
        if d:
            self.output_dir.set(d)

    def on_search(self):
        kw = self.keyword_var.get().strip()
        if not kw:
            messagebox.showwarning("알림", "키워드를 입력하세요.")
            return
        n = int(self.count_var.get())
        self._start_busy("◉  검색 중", f"'{kw}' 검색 중...")
        self.btn_search.config(state="disabled")

        def work():
            try:
                items = YtWrapper.search(kw, n)
                self.msg_queue.put(("search_done", items))
            except Exception as e:
                self.msg_queue.put(("error", f"검색 실패: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _populate_videos(self, items: list[VideoItem]):
        self.tv_videos.delete(*self.tv_videos.get_children())
        self.videos = items
        self.video_selected = {v.video_id: False for v in items}
        for i, v in enumerate(items):
            mm, ss = divmod(v.duration or 0, 60)
            hh, mm = divmod(mm, 60)
            dur = f"{hh:d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
            self.tv_videos.insert("", "end", iid=v.video_id,
                                  values=("○", dur, v.title, v.uploader),
                                  tags=("odd" if i % 2 else "even",))

    def _on_video_single_click(self, event):
        if self.tv_videos.identify("region", event.x, event.y) != "cell":
            return
        if self.tv_videos.identify_column(event.x) != "#1":
            return
        iid = self.tv_videos.identify_row(event.y)
        if iid:
            self._toggle_video(iid)

    def _on_video_double_click(self, event):
        if self.tv_videos.identify("region", event.x, event.y) != "cell":
            return
        iid = self.tv_videos.identify_row(event.y)
        if iid:
            self._toggle_video(iid)

    def _video_row_tag(self, v: VideoItem, idx: int) -> str:
        if self.video_selected.get(v.video_id):
            return "picked"
        return "odd" if idx % 2 else "even"

    def _refresh_video_row(self, v: VideoItem, idx: int):
        picked = self.video_selected.get(v.video_id, False)
        vals = list(self.tv_videos.item(v.video_id, "values"))
        vals[0] = "●" if picked else "○"
        self.tv_videos.item(v.video_id, values=vals,
                            tags=(self._video_row_tag(v, idx),))

    def _toggle_video(self, video_id: str):
        cur = self.video_selected.get(video_id, False)
        self.video_selected[video_id] = not cur
        idx = next((i for i, v in enumerate(self.videos) if v.video_id == video_id), 0)
        self._refresh_video_row(self.videos[idx], idx)

    def _toggle_all_videos(self, on: bool):
        for i, v in enumerate(self.videos):
            self.video_selected[v.video_id] = on
            self._refresh_video_row(v, i)

    def on_remove_videos(self):
        # 1순위: 트리에서 하이라이트된 행, 2순위: 체크된 행
        targets = set(self.tv_videos.selection())
        if not targets:
            targets = {v.video_id for v in self.videos
                       if self.video_selected.get(v.video_id)}
        if not targets:
            messagebox.showwarning(
                "알림",
                "제거할 영상을 행 클릭으로 하이라이트하거나 체크하세요."
            )
            return
        for vid in targets:
            if self.tv_videos.exists(vid):
                self.tv_videos.delete(vid)
            self.video_selected.pop(vid, None)
        self.videos = [v for v in self.videos if v.video_id not in targets]
        # 지브라 패턴 다시 적용
        for i, v in enumerate(self.videos):
            self._refresh_video_row(v, i)
        self.status_var.set(f"영상 {len(targets)}개를 목록에서 제거했습니다.")

    # ----- 이벤트: 트랙 추출 -----

    def on_extract_tracks(self):
        chosen = [v for v in self.videos if self.video_selected.get(v.video_id)]
        if not chosen:
            messagebox.showwarning("알림", "영상을 하나 이상 선택하세요.")
            return
        self._start_busy("◉  트랙 추출 중",
                         f"{len(chosen)}개 영상에서 트랙을 가져오는 중...")
        self.btn_extract.config(state="disabled")

        def work():
            all_tracks: list[Track] = []
            # 트랙리스트/챕터가 전혀 없어 처리 불가한 영상
            no_tracks: list[tuple[str, str]] = []  # (video_id, title)
            for i, v in enumerate(chosen, 1):
                self.msg_queue.put(("stage",
                    f"◉  트랙 정보 수집  ·  {i} / {len(chosen)}"))
                self.msg_queue.put(("status",
                    f"트랙 추출 중 ({i}/{len(chosen)}): {v.title}"))
                try:
                    info = YtWrapper.full_info(v)
                    tks = extract_tracks(v, info)
                    if not tks:
                        no_tracks.append((v.video_id, v.title))
                        continue
                    all_tracks.extend(tks)
                except Exception as e:
                    self.msg_queue.put(("warn", f"'{v.title}' 추출 실패: {e}"))
            self.msg_queue.put(("tracks_done", (all_tracks, no_tracks)))

        threading.Thread(target=work, daemon=True).start()

    def _populate_tracks(self, tracks: list[Track]):
        self.tv_tracks.delete(*self.tv_tracks.get_children())
        self.tracks = tracks
        self.track_selected = {i: False for i in range(len(tracks))}
        self.track_status: dict[int, str] = {i: "" for i in range(len(tracks))}
        for i, t in enumerate(tracks):
            def fmt(s):
                if s is None:
                    return "끝"
                s = int(s)
                mm, ss = divmod(s, 60)
                hh, mm = divmod(mm, 60)
                return f"{hh:02d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
            rng = f"{fmt(t.start)}~{fmt(t.end)}"
            self.tv_tracks.insert("", "end", iid=str(i),
                                  values=("○", "", rng, t.title, t.video.title),
                                  tags=("odd" if i % 2 else "even",))

    def _on_track_click(self, event):
        region = self.tv_tracks.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.tv_tracks.identify_column(event.x)
        if col != "#1":
            return
        iid = self.tv_tracks.identify_row(event.y)
        if not iid:
            return
        self._toggle_track(int(iid))

    def _on_track_double_click(self, event):
        if self.tv_tracks.identify("region", event.x, event.y) != "cell":
            return
        iid = self.tv_tracks.identify_row(event.y)
        if iid:
            self._toggle_track(int(iid))

    _STATUS_LABEL = {
        "": "",
        "pending": "○  대기",
        "running": "◉  재생중",
        "done":    "✓  완료",
        "failed":  "✕  실패",
    }

    def _row_tag(self, idx: int) -> str:
        st = self.track_status.get(idx, "") if hasattr(self, "track_status") else ""
        if st in ("pending", "running", "done", "failed"):
            return st
        return "odd" if idx % 2 else "even"

    def _refresh_track_row(self, idx: int):
        picked = self.track_selected.get(idx, False)
        st = self.track_status.get(idx, "") if hasattr(self, "track_status") else ""
        vals = list(self.tv_tracks.item(str(idx), "values"))
        vals[0] = "●" if picked else "○"
        vals[1] = self._STATUS_LABEL.get(st, "")
        self.tv_tracks.item(str(idx), values=vals, tags=(self._row_tag(idx),))

    def _set_track_status(self, idx: int, status: str):
        if not hasattr(self, "track_status"):
            return
        self.track_status[idx] = status
        self._refresh_track_row(idx)
        # 진행중 트랙은 보이도록 스크롤
        if status == "running":
            try:
                self.tv_tracks.see(str(idx))
            except tk.TclError:
                pass

    def _toggle_track(self, idx: int):
        cur = self.track_selected.get(idx, False)
        self.track_selected[idx] = not cur
        self._refresh_track_row(idx)

    def _toggle_all_tracks(self, on: bool):
        for i in range(len(self.tracks)):
            self.track_selected[i] = on
            self._refresh_track_row(i)

    def on_remove_tracks(self):
        if not self.tracks:
            return
        # 1순위: 트리에서 하이라이트된 행, 2순위: 체크된 행
        try:
            targets = {int(iid) for iid in self.tv_tracks.selection()}
        except ValueError:
            targets = set()
        if not targets:
            targets = {i for i, on in self.track_selected.items() if on}
        if not targets:
            messagebox.showwarning(
                "알림",
                "제거할 트랙을 행 클릭으로 하이라이트하거나 체크하세요."
            )
            return

        # 체크 상태/진행 상태 보존하며 인덱스 재작성
        survivors = [i for i in range(len(self.tracks)) if i not in targets]
        new_tracks = [self.tracks[i] for i in survivors]
        saved_sel = {new_i: self.track_selected.get(old_i, False)
                     for new_i, old_i in enumerate(survivors)}
        saved_st = {new_i: self.track_status.get(old_i, "")
                    for new_i, old_i in enumerate(survivors)}

        removed = len(targets)
        self._populate_tracks(new_tracks)
        self.track_selected = saved_sel
        self.track_status = saved_st
        for i in range(len(new_tracks)):
            self._refresh_track_row(i)
        self.status_var.set(f"트랙 {removed}개를 목록에서 제거했습니다.")

    # ----- 이벤트: 다운로드/추출 -----

    def on_download(self):
        if not ffmpeg_available():
            messagebox.showerror(
                "ffmpeg 없음",
                "ffmpeg 실행 파일을 PATH 에서 찾을 수 없습니다.\n"
                "https://ffmpeg.org/ 에서 설치 후 다시 시도하세요."
            )
            return
        chosen_idx = [i for i, on in self.track_selected.items() if on]
        if not chosen_idx:
            messagebox.showwarning("알림", "트랙을 하나 이상 선택하세요.")
            return
        out_dir = self.output_dir.get().strip()
        if not out_dir:
            messagebox.showwarning("알림", "저장 폴더를 지정하세요.")
            return

        chosen_pairs = [(i, self.tracks[i]) for i in chosen_idx]
        # 모든 선택 트랙을 '대기'로 표시
        for idx, _ in chosen_pairs:
            self._set_track_status(idx, "pending")

        # 영상별로 그룹화 (같은 원본을 한 번만 다운로드)
        groups: dict[str, list[tuple[int, Track]]] = {}
        for idx, t in chosen_pairs:
            groups.setdefault(t.video.video_id, []).append((idx, t))

        total = len(chosen_pairs)
        offset = max(0.0, float(self.start_offset_var.get() or 0.0))

        self.msg_queue.put(("stage", f"◉  다운로드 & MP3 추출  (0 / {total})"))
        self.msg_queue.put(("progress_set", (0.0, "")))
        self.btn_download.config(state="disabled")
        self.btn_search.config(state="disabled")
        self.btn_extract.config(state="disabled")
        self.lp.start()

        def work():
            done = 0
            tmp_dir = os.path.join(out_dir, "_tmp_audio")
            os.makedirs(tmp_dir, exist_ok=True)

            def make_hook():
                def hook(d):
                    st = d.get("status")
                    if st == "downloading":
                        total_b = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                        got = d.get("downloaded_bytes") or 0
                        pct = (got / total_b * 100.0) if total_b else 0.0
                        speed = d.get("speed") or 0
                        txt = f"{got/1_000_000:.1f} / {total_b/1_000_000:.1f} MB  ·  {speed/1_000_000:.1f} MB/s" if total_b else f"{got/1_000_000:.1f} MB"
                        self.msg_queue.put(("download_progress", (pct, txt)))
                    elif st == "finished":
                        self.msg_queue.put(("download_progress", (100.0, "다운로드 완료, 처리 중...")))
                return hook

            try:
                for pairs in groups.values():
                    video = pairs[0][1].video
                    self.msg_queue.put(("status", f"원본 오디오 다운로드: {video.title}"))
                    self.msg_queue.put(("stage",
                        f"◉  원본 다운로드 중  ·  {video.title[:60]}"))
                    try:
                        src = YtWrapper.download_audio(
                            video, tmp_dir, progress_hook=make_hook()
                        )
                    except Exception as e:
                        self.msg_queue.put(("warn", f"다운로드 실패 '{video.title}': {e}"))
                        for idx, _ in pairs:
                            self.msg_queue.put(("track_status", (idx, "failed")))
                        continue

                    for idx, t in pairs:
                        done += 1
                        self.msg_queue.put(("track_status", (idx, "running")))
                        self.msg_queue.put(("status",
                            f"MP3 추출 ({done}/{total}): {t.title}"))
                        self.msg_queue.put(("stage",
                            f"◉  MP3 추출 중  ·  {done} / {total}"))
                        self.msg_queue.put(("overall_progress",
                            ((done - 1) / total * 100.0)))
                        try:
                            split_to_mp3(src, t, out_dir, start_offset=offset)
                            self.msg_queue.put(("track_status", (idx, "done")))
                        except subprocess.CalledProcessError as e:
                            self.msg_queue.put(("warn",
                                f"MP3 실패 '{t.title}': ffmpeg 종료코드 {e.returncode}"))
                            self.msg_queue.put(("track_status", (idx, "failed")))
                        except Exception as e:
                            self.msg_queue.put(("warn", f"MP3 실패 '{t.title}': {e}"))
                            self.msg_queue.put(("track_status", (idx, "failed")))
                        self.msg_queue.put(("overall_progress",
                            (done / total * 100.0)))
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            self.msg_queue.put(("download_done", out_dir))

        threading.Thread(target=work, daemon=True).start()

    # ----- 진행/상태 -----

    def _start_busy(self, stage_text: str, status_text: str = ""):
        self.stage_var.set(stage_text)
        if status_text:
            self.status_var.set(status_text)
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        # LP 가 돌기 시작
        if hasattr(self, "lp"):
            self.lp.start()

    def _stop_busy(self, stage_text: str, status_text: str = ""):
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.progress_label_var.set("")
        self.stage_var.set(stage_text)
        if status_text:
            self.status_var.set(status_text)
        # LP 정지
        if hasattr(self, "lp"):
            self.lp.stop()

    def _pump_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "search_done":
                    self._populate_videos(payload)
                    self._stop_busy("◯  대기 중", f"검색 완료: {len(payload)}건")
                    self.btn_search.config(state="normal")
                elif kind == "tracks_done":
                    tracks, no_tracks = payload
                    # 트랙리스트 없는 영상을 검색 결과에서 제거
                    if no_tracks:
                        bad_ids = {vid for vid, _ in no_tracks}
                        for vid in bad_ids:
                            if self.tv_videos.exists(vid):
                                self.tv_videos.delete(vid)
                            self.video_selected.pop(vid, None)
                        self.videos = [v for v in self.videos
                                        if v.video_id not in bad_ids]
                        for i, v in enumerate(self.videos):
                            self._refresh_video_row(v, i)
                    self._populate_tracks(tracks)
                    msg = f"트랙 추출 완료: {len(tracks)}건"
                    if no_tracks:
                        msg += f"  ·  처리 불가 영상 {len(no_tracks)}개 제거"
                    self._stop_busy("◯  대기 중", msg)
                    self.btn_extract.config(state="normal")
                    if no_tracks:
                        sample = "\n".join(f"• {t[:60]}"
                                            for _, t in no_tracks[:10])
                        if len(no_tracks) > 10:
                            sample += f"\n... 외 {len(no_tracks) - 10}개"
                        messagebox.showinfo(
                            "처리 불가 영상 제거",
                            "챕터 또는 설명 타임스탬프 기반 트랙리스트가 없어\n"
                            f"처리할 수 없는 영상 {len(no_tracks)}개를 "
                            f"검색 결과에서 제거했습니다.\n\n{sample}"
                        )
                elif kind == "download_done":
                    self.progress.configure(mode="determinate", value=100)
                    self.progress_label_var.set("")
                    self.stage_var.set("✓  완료")
                    self.status_var.set(f"완료. 저장 폴더: {payload}")
                    self.btn_download.config(state="normal")
                    self.btn_search.config(state="normal")
                    self.btn_extract.config(state="normal")
                    self.lp.stop()
                    messagebox.showinfo("완료", f"MP3 저장이 끝났습니다.\n{payload}")
                elif kind == "stage":
                    self.stage_var.set(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "warn":
                    self.status_var.set(payload)
                elif kind == "download_progress":
                    pct, txt = payload
                    self.progress.configure(mode="determinate", value=pct)
                    self.progress_label_var.set(txt)
                elif kind == "overall_progress":
                    self.progress.configure(mode="determinate", value=payload)
                elif kind == "progress_set":
                    pct, txt = payload
                    self.progress.configure(mode="determinate", value=pct)
                    self.progress_label_var.set(txt)
                elif kind == "track_status":
                    idx, status = payload
                    self._set_track_status(idx, status)
                elif kind == "error":
                    self._stop_busy("✕  오류", payload)
                    self.btn_search.config(state="normal")
                    self.btn_extract.config(state="normal")
                    self.btn_download.config(state="normal")
                    self.lp.stop()
                    messagebox.showerror("오류", payload)
        except queue.Empty:
            pass
        self.after(120, self._pump_queue)


if __name__ == "__main__":
    App().mainloop()
