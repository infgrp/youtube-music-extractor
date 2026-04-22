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
import customtkinter as ctk
from dataclasses import dataclass
from typing import Optional, Callable

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


# ---------- 내부 예외 ----------

class Cancelled(Exception):
    """사용자가 현재 작업을 취소했음을 알리는 내부 신호.

    워커 스레드와 yt-dlp progress hook 에서 이 예외를 raise 해 중단을
    전파한다. ffmpeg 측은 live Popen 을 직접 terminate 한다.
    """


# ---------- 상수 ----------

# 챕터/설명 타임스탬프가 없을 때 이 길이(초) 이하면 단일곡으로 간주하고
# 영상 전체를 한 트랙으로 추출한다. 이보다 길면 트랙리스트가 없는
# 컴필레이션/믹스로 판단해 검색 결과에서 필터링한다.
SINGLE_TRACK_MAX_SEC = 15 * 60  # 15분


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


def parse_tracklist_from_description(description: str,
                                      duration: Optional[float] = None
                                      ) -> list[tuple[str, int]]:
    """설명에서 (제목, 시작초) 리스트를 추출. 순서는 타임스탬프 오름차순.

    duration 이 주어지면 영상 길이를 초과하는 타임스탬프는 걸러내고,
    결과가 2개 미만이면 빈 리스트를 돌려준다 (트랙리스트라기보단
    공개 시간·업로드 시간 같은 잡음일 가능성이 크기 때문).
    """
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
        # duration 이 알려져 있으면 영상 길이를 넘는 타임스탬프는 잡음.
        if duration is not None and s > duration:
            continue
        seen.add(s)
        out.append((t, s))
    # 타임스탬프 1개만 잡힌 경우는 트랙리스트가 아닌 공개 시각/업로드
    # 시각 같은 잡음일 공산이 크다. 단일곡 폴백에 맡긴다.
    if len(out) < 2:
        return []
    return out


def extract_tracks(video: VideoItem, info: dict,
                    single_track_max_sec: float = SINGLE_TRACK_MAX_SEC
                    ) -> list[Track]:
    """info(yt-dlp extract_info 결과)에서 챕터 → 설명 파싱 → 단일 트랙 순으로 폴백."""
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

    duration = float(info.get("duration") or 0) or None
    parsed = parse_tracklist_from_description(
        info.get("description") or "", duration=duration
    )
    if parsed:
        for i, (title, start) in enumerate(parsed):
            end = float(parsed[i + 1][1]) if i + 1 < len(parsed) else duration
            tracks.append(Track(video=video, index=i + 1, title=title,
                                start=float(start), end=end))
        return tracks

    # 챕터도 설명 타임스탬프도 없음.
    # 길이가 single_track_max_sec 이하면 단일곡으로 간주해 통째로 한 트랙으로 반환.
    # 그보다 길면 트랙 정보가 없는 컴필레이션으로 보고 빈 리스트를 돌려 필터링한다.
    if duration is not None and duration <= single_track_max_sec:
        tracks.append(Track(
            video=video,
            index=1,
            title=video.title,
            start=0.0,
            end=duration,
        ))
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

_SUBPROCESS_FLAGS = (
    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
)


def _ffmpeg_path() -> Optional[str]:
    """번들된 ffmpeg 을 우선 찾고, 없으면 PATH 에서 찾는다.

    PyInstaller onefile 로 빌드된 경우 `sys._MEIPASS` 에,
    onedir/개발 실행 시에는 실행 파일/스크립트와 같은 폴더에 배치된
    `ffmpeg.exe` 를 찾는다. 모두 실패하면 시스템 PATH 에서 검색한다.
    """
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, exe))
    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(os.path.dirname(sys.executable), exe))
    else:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), exe))
    for p in candidates:
        if os.path.isfile(p):
            return p
    return shutil.which("ffmpeg")


def ffmpeg_available() -> bool:
    return _ffmpeg_path() is not None


def _safe_filename(name: str, max_len: int = 150) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_len] or "track"


# Windows 기본 MAX_PATH (NUL 제외). 긴 경로 지원이 꺼진 환경에서
# 파일을 만들 수 있는 상한.
_WIN_MAX_PATH = 259


def split_to_mp3(source_path: str, track: Track, out_dir: str,
                 start_offset: float = 0.0,
                 register_proc: Optional[Callable[[Optional["subprocess.Popen"]], None]] = None
                 ) -> str:
    os.makedirs(out_dir, exist_ok=True)

    prefix = f"{track.index:02d} - "
    suffix = (f" [{track.video.video_id}]" if track.video.video_id else "") + ".mp3"

    # 최종 out_path = out_dir + os.sep + prefix + title + suffix
    # Windows 260자 제한을 넘지 않도록 title 에 쓸 수 있는 최대 길이를
    # out_dir 길이 기준으로 동적으로 계산한다.
    budget = _WIN_MAX_PATH - len(os.path.abspath(out_dir)) - 1 \
              - len(prefix) - len(suffix)
    title_max = max(20, min(150, budget))
    base = f"{prefix}{_safe_filename(track.title, max_len=title_max)}"
    out_path = os.path.join(out_dir, f"{base}{suffix}")

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

    cmd = [_ffmpeg_path() or "ffmpeg", "-y", "-loglevel", "error"]
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
    # --noconsole 로 빌드된 PyInstaller exe 는 stdin/stdout/stderr 가 None 이라
    # 자식 프로세스가 상속받으면 즉시 실패할 수 있다. 명시적으로 redirect 하고
    # Windows 에서는 콘솔 창이 깜빡이지 않도록 CREATE_NO_WINDOW 를 준다.
    # 취소 대응을 위해 Popen 으로 띄워 상위에 핸들을 노출한다. 상위는
    # 필요시 이 핸들로 terminate()/kill() 을 걸어 ffmpeg 을 즉시 중단시킨다.
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=_SUBPROCESS_FLAGS,
    )
    if register_proc:
        register_proc(proc)
    try:
        _, stderr_bytes = proc.communicate()
    finally:
        if register_proc:
            register_proc(None)
    if proc.returncode != 0:
        err = (stderr_bytes or b"").decode("utf-8", "replace").strip()
        # 외부에서 kill 당한 경우 (terminate→SIGTERM/-1 류) 도 returncode ≠ 0
        # 이지만, 취소 흐름은 상위에서 Cancelled 로 감지/변환한다.
        raise subprocess.CalledProcessError(proc.returncode, cmd, stderr=err)
    return out_path


# ---------- GUI ----------

# ---------- 테마 색상 ----------

COLORS = {
    # --- Backgrounds — 웜 딥차콜 계열 (살짝 갈색 빛) ---
    "bg":          "#0b0a0e",
    "surface":     "#141318",
    "surface_alt": "#1c1b22",
    "surface_hi":  "#26252e",

    # --- Text — 퓨어 화이트 대신 톤 다운된 아이보리 ---
    "text":        "#ecebf0",
    "text_sub":    "#b8b6c0",
    "muted":       "#7a7883",
    "dim":         "#50505a",

    # --- Borders — 거의 안 보이는 헤어라인 ---
    "border":      "#1b1a20",
    "border_str":  "#2b2a33",

    # --- Primary: 바이닐 라벨 골드 (조금 더 웜·브라이트) ---
    "primary":     "#e0b159",
    "primary_h":   "#f0c478",
    "primary_d":   "#b08535",
    "primary_l":   "#25200f",

    # --- Accent: 골드 동일 계열 카퍼 (틸 제거 → 웜 톤 통일) ---
    "accent":      "#c89363",
    "accent_h":    "#d9a77a",
    "accent_d":    "#966c44",

    # --- Status — 과채도 자제, 모노톤에 가까운 시그널 ---
    "success":     "#7cc187",
    "success_bg":  "#13221a",
    "warn":        "#e8bb62",
    "warn_bg":     "#2a2115",
    "danger":      "#d47278",
    "danger_bg":   "#2a1618",
    "info":        "#8fa8d4",
    "info_bg":     "#141d2e",

    # --- Lists ---
    "zebra":       "#100f14",
    "row_even":    "#141318",
    "selection":   "#2c2415",

    # --- Player(하단 상태) 바 ---
    "status_bg":   "#06050a",
    "status_bg2":  "#0f0e14",
    "status_fg":   "#ecebf0",
    "status_acc":  "#e0b159",

    # --- Vinyl — 더 깊은 블랙 + 리플렉션용 하이라이트 쉐이드 ---
    "vinyl":       "#050407",
    "vinyl_edge":  "#16151b",
    "vinyl_sheen": "#2a2936",      # 그루브 위 옅은 반사
    "vinyl_label": "#c59c5e",
    "vinyl_label_rim": "#8c6c40",
    "vinyl_label_hi":  "#f4d48a",
    "vinyl_label_d":   "#8a6a40",
    "vinyl_mark":  "#2a1d12",
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
        r = self.size / 2 - 5

        # 바깥 글로우 — 골드 빛이 배경으로 스며드는 느낌 (네 겹)
        for i, shade in enumerate(["#0f0c08", "#1a1410", "#2a2015", "#3c2d1a"]):
            ro = r + 4 - i
            self.create_oval(cx - ro, cy - ro, cx + ro, cy + ro,
                             outline=shade, width=1)
        # LP 본체 — 깊은 블랙 + 골드 림
        self.create_oval(cx - r, cy - r, cx + r, cy + r,
                         fill=C["vinyl"], outline=C["primary_d"], width=2)
        # 림 안쪽 섀도우
        r_in = r - 3
        self.create_oval(cx - r_in, cy - r_in, cx + r_in, cy + r_in,
                         outline="#1a1820", width=1)

        # 그루브 — 대비/리듬감 개선. 짝수 번째마다 더 밝은 쉐이드로 라이트 리플렉션.
        ring_outer = r - 8
        ring_inner = r * 0.44
        n = max(18, int((ring_outer - ring_inner) / 1.0))
        for i in range(n):
            t = i / max(n - 1, 1)
            gr = ring_outer - (ring_outer - ring_inner) * t
            # 세 단계 톤: 일반 / 살짝 밝음 / 어두움 — 동심원 리듬 확보
            if i % 5 == 0:
                shade = C["vinyl_sheen"]
            elif i % 2 == 0:
                shade = "#1f1e25"
            else:
                shade = "#0d0c11"
            self.create_oval(cx - gr, cy - gr, cx + gr, cy + gr,
                             outline=shade, width=1)

        # 상단 호 — 전체 디스크 빛 반사 느낌 (북동 45도 방향 얇은 하이라이트)
        hl_r = r - 6
        self.create_arc(cx - hl_r, cy - hl_r, cx + hl_r, cy + hl_r,
                        start=55, extent=42, style="arc",
                        outline="#34323c", width=1)
        self.create_arc(cx - hl_r + 2, cy - hl_r + 2,
                        cx + hl_r - 2, cy + hl_r - 2,
                        start=60, extent=28, style="arc",
                        outline="#3e3c46", width=1)

        # 라벨 (골드 디스크) — 큼직하게, 디테일 추가
        lr = ring_inner - 2
        # 라벨 뒤 섀도우 (아래로 1픽셀 오프셋)
        self.create_oval(cx - lr, cy - lr + 1, cx + lr, cy + lr + 1,
                         fill="#15110a", outline="")
        # 라벨 본체
        self.create_oval(cx - lr, cy - lr, cx + lr, cy + lr,
                         fill=C["vinyl_label"], outline=C["vinyl_label_rim"],
                         width=1)
        # 라벨 하이라이트 (왼쪽 위) — 약한 반사
        self.create_arc(cx - lr + 2, cy - lr + 2,
                        cx + lr - 4, cy + lr - 4,
                        start=55, extent=80, style="arc",
                        outline=C["vinyl_label_hi"], width=2)
        # 라벨 내측 가느다란 이중 링 — 진짜 레이블 느낌
        lr2 = lr * 0.72
        self.create_oval(cx - lr2, cy - lr2, cx + lr2, cy + lr2,
                         outline=C["vinyl_label_rim"], width=1)
        lr3 = lr * 0.42
        self.create_oval(cx - lr3, cy - lr3, cx + lr3, cy + lr3,
                         outline=C["vinyl_label_d"], width=1)

    def _draw_marker(self):
        """회전하는 요소: 방사 스포크 3줄 + 라벨 포인트 + 중심 홀."""
        self.delete("dyn")
        cx = cy = self.size / 2
        r = self.size / 2 - 5
        lr = r * 0.44 - 2
        a = math.radians(self.angle)

        # 라벨-그루브를 가로지르는 가느다란 스포크 3줄 (120도 간격).
        ring_outer = r - 8
        for k in range(3):
            ang = a + k * (2 * math.pi / 3)
            ca, sa = math.cos(ang), math.sin(ang)
            # 그루브 구간만 덮는 짧은 스포크 — 라벨 위는 별도 점으로
            x1 = cx + lr * ca
            y1 = cy + lr * sa
            x2 = cx + ring_outer * ca
            y2 = cy + ring_outer * sa
            self.create_line(x1, y1, x2, y2,
                             fill="#262430", width=1, tags="dyn")

        # 라벨 위 회전 포인트 (타이틀 위치를 상징)
        mr = lr * 0.6
        mx = cx + mr * math.cos(a)
        my = cy + mr * math.sin(a)
        # 포인트 섀도우
        self.create_oval(mx - 5, my - 4, mx + 5, my + 6,
                         fill="#1a1108", outline="", tags="dyn")
        self.create_oval(mx - 4, my - 4, mx + 4, my + 4,
                         fill=COLORS["vinyl_mark"], outline="", tags="dyn")
        # 포인트 위 미니 하이라이트
        self.create_oval(mx - 2, my - 2, mx + 1, my + 1,
                         fill="#4a3a1f", outline="", tags="dyn")

        # 중심 스핀들 홀 + 금속 링
        self.create_oval(cx - 4, cy - 4, cx + 4, cy + 4,
                         fill="#08070c", outline="#3a2e1a", width=1,
                         tags="dyn")
        self.create_oval(cx - 1.5, cy - 1.5, cx + 1.5, cy + 1.5,
                         fill="#1a140a", outline="", tags="dyn")

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


class BusyBar(tk.Canvas):
    """ttk 테마에 의존하지 않는 Canvas 기반 진행바.

    모드:
        idle     — 비어있는 트로프만 표시
        pulse    — 슬라이딩 세그먼트 애니메이션 (indeterminate)
        progress — 왼쪽부터 채워지는 막대 (determinate, 0..100)

    어떤 테마/플랫폼에서도 똑같이 선명하게 그려진다
    (고대비 색상 + 명시적 테두리).
    """

    def __init__(self, parent, *, height: int = 18,
                 fill_color: str = "#f0c478",
                 trough_color: str = "#2a2832",
                 border_color: str = "#4a4755",
                 segment_color: Optional[str] = None,
                 bg: Optional[str] = None):
        if bg is None:
            try:
                bg = parent.cget("bg")
            except tk.TclError:
                bg = "#0b0a0e"
        super().__init__(parent, height=height, bg=bg,
                         highlightthickness=0, bd=0)
        self._h = height
        self._fill = fill_color
        self._trough = trough_color
        self._border = border_color
        self._seg = segment_color or fill_color
        self._mode = "idle"       # "idle" | "pulse" | "progress"
        self._pct = 0.0
        self._pulse_pos = 0.0     # 0..1
        self._pulse_dir = +1
        self._after_id: Optional[str] = None
        self.bind("<Configure>", lambda _e: self._redraw())
        # <Configure> 가 플랫폼/타이밍에 따라 초기 1회를 놓칠 수 있어
        # 안전하게 200ms 뒤 한 번 더 그린다.
        self.after(200, self._redraw)

    # ---- public API ----
    def pulse(self):
        if self._mode == "pulse":
            return
        self._mode = "pulse"
        if self._after_id is None:
            self._after_id = self.after(30, self._tick)
        self._redraw()

    def set_progress(self, pct: float):
        self._mode = "progress"
        self._pct = max(0.0, min(100.0, float(pct)))
        self._cancel_after()
        self._redraw()

    def stop(self):
        self._mode = "idle"
        self._pct = 0.0
        self._cancel_after()
        self._redraw()

    # ---- internals ----
    def _cancel_after(self):
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _tick(self):
        self._after_id = None
        if self._mode != "pulse":
            return
        self._pulse_pos += self._pulse_dir * 0.025
        if self._pulse_pos >= 1.0:
            self._pulse_pos = 1.0
            self._pulse_dir = -1
        elif self._pulse_pos <= 0.0:
            self._pulse_pos = 0.0
            self._pulse_dir = +1
        self._redraw()
        self._after_id = self.after(30, self._tick)

    def _redraw(self):
        self.delete("all")
        w = int(self.winfo_width())
        h = int(self.winfo_height())
        if w <= 2 or h <= 2:
            return
        self.create_rectangle(0, 0, w - 1, h - 1,
                              fill=self._trough, outline=self._border,
                              width=1)
        self.create_line(1, 1, w - 2, 1, fill="#15141a")
        if self._mode == "pulse":
            seg_w = max(60, int(w * 0.28))
            max_x = max(0, w - seg_w - 2)
            x = 1 + int(self._pulse_pos * max_x)
            self.create_rectangle(x, 2, x + seg_w, h - 2,
                                  fill=self._seg, outline="")
            self.create_line(x + seg_w - 1, 2, x + seg_w - 1, h - 2,
                             fill="#ffe6b0")
            self.create_line(x, 2, x, h - 2, fill="#c89c4e")
        elif self._mode == "progress":
            inner_w = w - 2
            fw = int(inner_w * (self._pct / 100.0))
            if fw > 0:
                self.create_rectangle(1, 2, 1 + fw, h - 2,
                                      fill=self._fill, outline="")
                self.create_line(1, 2, 1 + fw, 2, fill="#ffe6b0")


class App(ctk.CTk):
    def __init__(self):
        _enable_dpi_awareness()
        # CustomTkinter 전역 설정 — 다크 모드 + 골드 계열 커스텀 팔레트
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")  # 이후 위젯별로 골드로 오버라이드
        super().__init__(fg_color=COLORS["bg"])
        self.title("YouTube Music Extractor")
        self.configure(bg=COLORS["bg"])

        # DPI 에 맞춘 Tk 스케일링 (72pt = 1") — 흐릿한 폰트 방지
        try:
            dpi = self.winfo_fpixels("1i")
            self.tk.call("tk", "scaling", max(1.0, dpi / 72.0))
        except tk.TclError:
            pass

        # 창 크기를 화면 크기에 맞춰 자동 축소.
        # 이전 버전은 1240x860 고정이었는데, 작은 디스플레이에서
        # 제목표시줄(위) + 플레이어 바(아래)가 잘려 X/중단 버튼/진행바가
        # 전부 화면 밖으로 나가는 문제가 있었다.
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        # 목표 크기는 1240x820. 단 절대 화면을 넘지 않도록 클램프.
        # 작업표시줄·제목표시줄 여유로 가로 -60, 세로 -90 정도를 비움.
        win_w = max(800, min(1240, sw - 60))
        win_h = max(560, min(820, sh - 90))
        # 중앙 정렬하되 y 최소 0 보장 — 제목표시줄이 절대 화면 밖으로 안 나감.
        x = max(0, (sw - win_w) // 2)
        y = max(0, (sh - win_h) // 2 - 10)
        self.geometry(f"{win_w}x{win_h}+{x}+{y}")
        self.minsize(min(800, win_w), min(560, win_h))

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
        self.single_track_max_min_var = tk.IntVar(value=SINGLE_TRACK_MAX_SEC // 60)
        self.msg_queue: "queue.Queue[tuple]" = queue.Queue()

        # 협조적 취소 상태.
        # - _cancel: 워커가 안전 지점마다 체크해 Cancelled 로 탈출.
        # - _current_proc: 현재 실행 중인 ffmpeg Popen 참조. 취소 시 바로 terminate.
        # - _proc_lock: _current_proc 접근 보호.
        # - _worker: 현재 돌고 있는 워커 스레드. 창 닫힘 시 join 대상.
        self._cancel = threading.Event()
        self._current_proc: Optional[subprocess.Popen] = None
        self._proc_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None

        self._apply_styles()
        self._build_ui()
        self.after(100, self._pump_queue)

        # 이전 실행이 비정상 종료돼 남았을 수 있는 _tmp_audio 를 지운다.
        self._cleanup_tmp_audio()
        # 창 닫힘/프로세스 종료 시에도 정리.
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        import atexit
        atexit.register(self._cleanup_tmp_audio)

    def _cleanup_tmp_audio(self):
        try:
            out_dir = self.output_dir.get()
        except Exception:
            return
        tmp = os.path.join(out_dir, "_tmp_audio")
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)

    # ----- 취소 제어 -----

    def _register_proc(self, proc: Optional["subprocess.Popen"]):
        """워커가 현재 실행 중인 ffmpeg Popen 을 등록/해제한다."""
        with self._proc_lock:
            self._current_proc = proc

    def _kill_current_proc(self):
        """등록된 ffmpeg 이 있으면 즉시 terminate → 실패 시 kill."""
        with self._proc_lock:
            proc = self._current_proc
        if not proc:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    proc.kill()
        except Exception:
            pass

    def _make_cancelable_hook(self, inner: Optional[Callable] = None):
        """yt-dlp progress_hook 에 cancel 체크를 얹은 래퍼.

        사용자가 취소를 누르면 hook 안에서 Cancelled 가 raise 되어
        yt-dlp 가 자체적으로 다운로드를 중단·정리한다.
        """
        def hook(d):
            if self._cancel.is_set():
                raise Cancelled()
            if inner:
                inner(d)
        return hook

    def on_cancel(self):
        """UI 중단 버튼 핸들러."""
        if not (self._worker and self._worker.is_alive()):
            return
        self._cancel.set()
        self._kill_current_proc()
        self.status_var.set("중단 요청됨 — 현재 단계를 정리 중...")

    def _on_close(self):
        # 돌고 있는 작업이 있으면 협조적으로 취소하고 잠깐 기다린다.
        if self._worker and self._worker.is_alive():
            self._cancel.set()
            self._kill_current_proc()
            self._worker.join(timeout=2.0)
            # 2초 안에 안 끝나면 어쩔 수 없이 프로세스 종료로 강제 정리.
            self._kill_current_proc()
        self._cleanup_tmp_audio()
        self.destroy()

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
        f_title  = (ui, 24, "bold")
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
                        foreground=C["text_sub"], font=(ui, 12, "bold"))

        # 버튼 — 보조 (다크, 얇은 헤어라인 보더)
        style.configure("TButton",
                        background=C["surface_alt"],
                        foreground=C["text_sub"],
                        bordercolor=C["border_str"],
                        padding=(16, 10),
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

        # 버튼 — Primary (솔리드 골드)
        style.configure("Primary.TButton",
                        background=C["primary"],
                        foreground="#1a1408",
                        bordercolor=C["primary"],
                        padding=(22, 11),
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

        # 버튼 — Accent (골드 동계열 카퍼 · 웜 톤 하모니)
        style.configure("Accent.TButton",
                        background=C["accent"],
                        foreground="#1b120a",
                        bordercolor=C["accent"],
                        padding=(22, 11),
                        borderwidth=0,
                        relief="flat",
                        focusthickness=0,
                        font=f_bold)
        style.map("Accent.TButton",
                  background=[("active", C["accent_h"]),
                              ("pressed", C["accent_d"]),
                              ("disabled", "#4a3624")],
                  foreground=[("active", "#1b120a"),
                              ("disabled", "#7e6a54")])

        # 버튼 — Ghost (아웃라인, Danger 톤)
        style.configure("Ghost.TButton",
                        background=C["surface"],
                        foreground=C["danger"],
                        bordercolor=C["danger"],
                        padding=(16, 10),
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

        # Treeview (다크 리스트) — 고령 사용자 가독성 위해 폰트 확대
        f_list     = (ui, 13)
        f_list_hdr = (ui, 13, "bold")
        style.configure("Treeview",
                        background=C["surface"],
                        fieldbackground=C["surface"],
                        foreground=C["text"],
                        rowheight=44,
                        bordercolor=C["border"],
                        borderwidth=0,
                        relief="flat",
                        font=f_list)
        style.configure("Treeview.Heading",
                        background=C["surface_alt"],
                        foreground=C["muted"],
                        bordercolor=C["border"],
                        font=f_list_hdr,
                        padding=(12, 14),
                        relief="flat")
        style.map("Treeview.Heading",
                  background=[("active", C["surface_hi"])],
                  foreground=[("active", C["primary"])])
        style.map("Treeview",
                  background=[("selected", C["selection"])],
                  foreground=[("selected", C["primary_h"])])

        # (진행바는 ttk.Progressbar 대신 BusyBar 커스텀 Canvas 위젯 사용)

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

        # ─── CustomTkinter 위젯 공통 스타일 ───
        _btn_primary = dict(
            fg_color=C["primary"], hover_color=C["primary_h"],
            text_color="#1a1408", corner_radius=8,
            border_width=0, height=36,
            font=(ui, 11, "bold"))
        _btn_ghost = dict(
            fg_color="transparent", hover_color=C["surface_hi"],
            text_color=C["text_sub"], corner_radius=6,
            border_width=1, border_color=C["border_str"],
            height=32, font=(ui, 10, "bold"))
        _btn_danger = dict(
            fg_color="transparent", hover_color=C["danger_bg"],
            text_color=C["danger"], corner_radius=6,
            border_width=1, border_color=C["danger"],
            height=32, font=(ui, 10, "bold"))
        _entry_kw = dict(
            fg_color=C["surface_alt"], border_color=C["border_str"],
            text_color=C["text"], corner_radius=6,
            border_width=1, height=34)

        # ── 헤더 (미니 LP 로고) — 컴팩트 ──
        header = ttk.Frame(self, padding=(32, 12, 32, 4))
        header.pack(fill="x")
        brand = ttk.Frame(header)
        brand.pack(side="left")

        # 작은 정적 LP — 브랜드 마크
        logo = LPRecord(brand, size=48, bg=C["bg"])
        logo.pack(side="left", padx=(0, 14))

        ttext = ttk.Frame(brand)
        ttext.pack(side="left", anchor="w")
        ttk.Label(ttext, text="YouTube Music Extractor",
                  style="Title.TLabel").pack(anchor="w")
        ttk.Label(ttext,
                  text="키워드로 영상을 찾아 챕터별 트랙을 최고 음질 MP3 로 추출합니다.",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(1, 0))

        # 헤더 하단 헤어라인 (골드 톤)
        sep = tk.Frame(self, bg=C["primary_d"], height=1)
        sep.pack(fill="x", padx=32, pady=(6, 0))

        # ── 검색 카드 (다크) — 컴팩트 ──
        search_card = tk.Frame(self, bg=C["surface"],
                               highlightbackground=C["border"],
                               highlightthickness=1)
        search_card.pack(fill="x", padx=32, pady=(10, 6))

        inner = ttk.Frame(search_card, style="Surface.TFrame", padding=(20, 14))
        inner.pack(fill="x")

        ttk.Label(inner, text="검색어",
                  style="FieldLabel.TLabel").grid(row=0, column=0, sticky="w")
        self.keyword_var = tk.StringVar()
        entry = ctk.CTkEntry(inner, textvariable=self.keyword_var,
                              font=(ui, 11), placeholder_text="예: 7080 music",
                              **_entry_kw)
        entry.grid(row=1, column=0, sticky="we", padx=(0, 10), pady=(4, 0))
        entry.bind("<Return>", lambda _e: self.on_search())

        ttk.Label(inner, text="결과 개수",
                  style="FieldLabel.TLabel").grid(row=0, column=1, sticky="w")
        self.count_var = tk.IntVar(value=15)
        ttk.Spinbox(inner, from_=5, to=50, width=6,
                    textvariable=self.count_var, font=(ui, 11))\
            .grid(row=1, column=1, sticky="w", padx=(0, 10), pady=(4, 0))

        self.btn_search = ctk.CTkButton(inner, text="검색",
                                          command=self.on_search,
                                          **_btn_primary)
        self.btn_search.grid(row=1, column=2, sticky="we", pady=(4, 0))

        # 2행: 저장 폴더, 오프셋
        ttk.Label(inner, text="저장 폴더",
                  style="FieldLabel.TLabel").grid(row=2, column=0, sticky="w",
                                                   pady=(8, 0))
        folder_row = ttk.Frame(inner, style="Surface.TFrame")
        folder_row.grid(row=3, column=0, sticky="we",
                        padx=(0, 10), pady=(4, 0))
        folder_row.columnconfigure(0, weight=1)
        ctk.CTkEntry(folder_row, textvariable=self.output_dir,
                      font=(ui, 10), **_entry_kw)\
            .grid(row=0, column=0, sticky="we")
        ctk.CTkButton(folder_row, text="찾아보기", command=self.on_pick_dir,
                       **_btn_ghost)\
            .grid(row=0, column=1, padx=(8, 0))

        ttk.Label(inner, text="시작 오프셋(초)",
                  style="FieldLabel.TLabel").grid(row=2, column=1, sticky="w",
                                                   pady=(8, 0))
        off_row = ttk.Frame(inner, style="Surface.TFrame")
        off_row.grid(row=3, column=1, sticky="w", padx=(0, 10), pady=(4, 0))
        ttk.Spinbox(off_row, from_=0.0, to=2.0, increment=0.05, width=6,
                    textvariable=self.start_offset_var, format="%.2f",
                    font=(ui, 11))\
            .pack(side="left")
        ttk.Label(off_row, text=" 앞 곡 꼬리 제거용",
                  background=C["surface"], foreground=C["muted"],
                  font=(ui, 9)).pack(side="left", padx=(6, 0))

        ttk.Label(inner, text="단일곡 최대 길이(분)",
                  style="FieldLabel.TLabel").grid(row=2, column=2, sticky="w",
                                                   pady=(8, 0))
        single_row = ttk.Frame(inner, style="Surface.TFrame")
        single_row.grid(row=3, column=2, sticky="w", pady=(4, 0))
        ttk.Spinbox(single_row, from_=1, to=180, width=6,
                    textvariable=self.single_track_max_min_var,
                    font=(ui, 11))\
            .pack(side="left")
        ttk.Label(single_row, text=" 이하는 단일곡 취급",
                  background=C["surface"], foreground=C["muted"],
                  font=(ui, 9)).pack(side="left", padx=(6, 0))

        inner.columnconfigure(0, weight=3)
        inner.columnconfigure(1, weight=1)
        inner.columnconfigure(2, weight=1)

        # ── 플레이어 바 컨테이너 먼저 패킹 (하단 공간 확보) ──
        # body 를 먼저 expand=True 로 패킹하면 Tk 가 수직 공간을 전부 먹어
        # side="bottom" 플레이어가 0 픽셀로 찌부러진다. body 보다 먼저
        # bottom 영역을 확보한 뒤 안쪽 위젯을 뒤에서 채운다.
        self._player = tk.Frame(self, bg=C["status_bg"])
        self._player.pack(fill="x", side="bottom")

        # ── 본문: 좌(영상) / 우(트랙) ── — 최대 공간 확보
        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=32, pady=(4, 4))

        # ─── 좌: 검색 결과 카드 ───
        left = tk.Frame(body, bg=C["surface"],
                        highlightbackground=C["border"], highlightthickness=1)
        body.add(left, weight=1)
        self._build_list_header(left,
            title="검색 결과",
            hint="체크박스로 대상 선택 · 행 클릭 후 Delete 로 제거")

        left_btns = ttk.Frame(left, style="Surface.TFrame")
        left_btns.pack(side="bottom", fill="x", padx=16, pady=(6, 10))
        ctk.CTkButton(left_btns, text="전체 선택", **_btn_ghost,
                       command=lambda: self._toggle_all_videos(True)).pack(side="left", padx=(0, 6))
        ctk.CTkButton(left_btns, text="전체 해제", **_btn_ghost,
                       command=lambda: self._toggle_all_videos(False)).pack(side="left", padx=(0, 6))
        ctk.CTkButton(left_btns, text="목록에서 제거", **_btn_danger,
                       command=self.on_remove_videos).pack(side="left")
        self.btn_extract = ctk.CTkButton(left_btns, text="선택 영상 → 트랙 추출",
                                          command=self.on_extract_tracks,
                                          **_btn_primary)
        self.btn_extract.pack(side="right")

        tv_wrap_v = ttk.Frame(left, style="Surface.TFrame")
        tv_wrap_v.pack(side="top", fill="both", expand=True,
                       padx=18, pady=(0, 10))

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
        right_btns.pack(side="bottom", fill="x", padx=16, pady=(6, 10))
        ctk.CTkButton(right_btns, text="전체 선택", **_btn_ghost,
                       command=lambda: self._toggle_all_tracks(True)).pack(side="left", padx=(0, 6))
        ctk.CTkButton(right_btns, text="전체 해제", **_btn_ghost,
                       command=lambda: self._toggle_all_tracks(False)).pack(side="left", padx=(0, 6))
        ctk.CTkButton(right_btns, text="목록에서 제거", **_btn_danger,
                       command=self.on_remove_tracks).pack(side="left")
        _btn_accent = dict(_btn_primary)
        _btn_accent.update(fg_color=C["accent"], hover_color=C["accent_h"],
                           text_color="#1b120a")
        self.btn_download = ctk.CTkButton(right_btns, text="선택 트랙 MP3 저장",
                                           command=self.on_download,
                                           **_btn_accent)
        self.btn_download.pack(side="right")

        tv_wrap_t = ttk.Frame(right, style="Surface.TFrame")
        tv_wrap_t.pack(side="top", fill="both", expand=True,
                       padx=18, pady=(0, 10))

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
        # 컨테이너는 body 위쪽에서 이미 bottom 에 패킹됨 (self._player).
        # 여기서는 그 안쪽만 채운다.
        player = self._player

        # 상단 얇은 골드 라인 — 카드에서 플레이어로 넘어가는 경계
        tk.Frame(player, bg=C["primary_d"], height=1).pack(fill="x")

        # LP 원판 (좌) + 정보 스택 (우).
        # LP 크기를 84px 로 줄여 player bar 세로를 더 컴팩트하게.
        player_inner = tk.Frame(player, bg=C["status_bg"])
        player_inner.pack(fill="x", padx=28, pady=8)

        self.lp = LPRecord(player_inner, size=84, bg=C["status_bg"])
        self.lp.pack(side="left", padx=(0, 18))

        info = tk.Frame(player_inner, bg=C["status_bg"])
        info.pack(side="left", fill="both", expand=True)

        # 상단 라인: stage (좌) · 중단 버튼 (우)
        line1 = tk.Frame(info, bg=C["status_bg"])
        line1.pack(fill="x")

        self.stage_var = tk.StringVar(value="◯  대기 중")
        tk.Label(line1, textvariable=self.stage_var,
                 bg=C["status_bg"], fg=C["status_acc"],
                 font=(ui, 12, "bold")).pack(side="left")

        self.btn_cancel = ctk.CTkButton(line1, text="중단",
                                         command=self.on_cancel,
                                         state="disabled",
                                         **_btn_danger)
        self.btn_cancel.pack(side="right")

        # 전체 진행 — 선택 트랙 중 완료된 비율 (0..100)
        overall_line = tk.Frame(info, bg=C["status_bg"])
        overall_line.pack(fill="x", pady=(8, 2))
        self.overall_label_var = tk.StringVar(value="전체")
        tk.Label(overall_line, textvariable=self.overall_label_var,
                 bg=C["status_bg"], fg=C["text_sub"],
                 font=(ui, 9, "bold")).pack(side="left")
        self.overall_progress = BusyBar(
            info, height=18, bg=C["status_bg"],
            fill_color=C["primary_h"], segment_color=C["primary_h"],
            trough_color="#2a2832", border_color="#4a4755")
        self.overall_progress.pack(fill="x", pady=(0, 6))

        # 현재 파일 진행 — 다운로드 %, 또는 인코딩 indeterminate
        file_line = tk.Frame(info, bg=C["status_bg"])
        file_line.pack(fill="x", pady=(2, 2))
        self.file_label_var = tk.StringVar(value="")
        tk.Label(file_line, textvariable=self.file_label_var,
                 bg=C["status_bg"], fg=C["muted"],
                 font=(ui, 9)).pack(side="left")
        self.file_rate_var = tk.StringVar(value="")
        tk.Label(file_line, textvariable=self.file_rate_var,
                 bg=C["status_bg"], fg=C["status_fg"],
                 font=(self.F_MONO, 9)).pack(side="right")
        self.file_progress = BusyBar(
            info, height=12, bg=C["status_bg"],
            fill_color=C["accent_h"], segment_color=C["accent_h"],
            trough_color="#242230", border_color="#3f3d4a")
        self.file_progress.pack(fill="x", pady=(0, 6))

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
        hdr.pack(side="top", fill="x", padx=16, pady=(10, 4))
        tk.Label(hdr, text=title, bg=C["surface"], fg=C["text"],
                 font=(ui, 12, "bold")).pack(side="left")
        tk.Label(hdr, text="    " + hint, bg=C["surface"], fg=C["muted"],
                 font=(ui, 9)).pack(side="left")
        # 헤어라인 구분선
        tk.Frame(parent, bg=C["border_str"], height=1)\
            .pack(side="top", fill="x", padx=16, pady=(1, 0))

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
        self.btn_search.configure(state="disabled")

        def work():
            try:
                items = YtWrapper.search(kw, n)
                if self._cancel.is_set():
                    self.msg_queue.put(("cancelled", None))
                    return
                self.msg_queue.put(("search_done", items))
            except Exception as e:
                self.msg_queue.put(("error", f"검색 실패: {e}"))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

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
        self.btn_extract.configure(state="disabled")

        single_max_sec = max(1, int(self.single_track_max_min_var.get() or 15)) * 60

        def work():
            all_tracks: list[Track] = []
            # 챕터/설명 타임스탬프 없고 단일곡도 아닌(너무 긴) 영상
            no_tracks: list[tuple[str, str]] = []  # (video_id, title)
            total = len(chosen)
            # overall 바는 indeterminate 로 돌린 채 두어 "작업 중" 임을 보인다.
            # (세부 카운트는 label 과 status 로만 표현)
            for i, v in enumerate(chosen, 1):
                if self._cancel.is_set():
                    self.msg_queue.put(("cancelled", None))
                    return
                self.msg_queue.put(("stage",
                    f"◉  트랙 정보 수집  ·  {i} / {total}"))
                self.msg_queue.put(("status",
                    f"트랙 추출 중 ({i}/{total}): {v.title}"))
                try:
                    info = YtWrapper.full_info(v)
                    tks = extract_tracks(v, info,
                                          single_track_max_sec=single_max_sec)
                    if not tks:
                        no_tracks.append((v.video_id, v.title))
                        continue
                    all_tracks.extend(tks)
                except Exception as e:
                    self.msg_queue.put(("warn", f"'{v.title}' 추출 실패: {e}"))
            self.msg_queue.put(("tracks_done", (all_tracks, no_tracks)))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

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
        "running": "◉  저장중",
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

        self._start_busy(f"◉  다운로드 & MP3 추출  (0 / {total})",
                          f"{total}개 트랙 저장 시작...",
                          overall_mode="determinate")
        self.msg_queue.put(("overall_progress", (0, total)))
        self.btn_download.configure(state="disabled")
        self.btn_search.configure(state="disabled")
        self.btn_extract.configure(state="disabled")

        def work():
            done = 0
            cancelled = False
            tmp_dir = os.path.join(out_dir, "_tmp_audio")
            os.makedirs(tmp_dir, exist_ok=True)

            def make_dl_hook(title: str):
                # yt-dlp 로부터 들어오는 진행 이벤트 → UI 로 전달.
                # _cancel 이 켜지면 Cancelled 를 raise 해 yt-dlp 자체 정리 경로로 태운다.
                def inner(d):
                    st = d.get("status")
                    if st == "downloading":
                        total_b = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                        got = d.get("downloaded_bytes") or 0
                        pct = (got / total_b * 100.0) if total_b else 0.0
                        speed = d.get("speed") or 0
                        rate = (f"{got/1_000_000:.1f} / {total_b/1_000_000:.1f} MB  ·  "
                                f"{speed/1_000_000:.1f} MB/s") if total_b else \
                               f"{got/1_000_000:.1f} MB"
                        self.msg_queue.put(("file_progress",
                                            (pct, f"다운로드  ·  {title[:50]}", rate)))
                    elif st == "finished":
                        self.msg_queue.put(("file_progress",
                                            (100.0, "다운로드 완료, 처리 중...", "")))
                return self._make_cancelable_hook(inner)

            try:
                for pairs in groups.values():
                    if self._cancel.is_set():
                        cancelled = True
                        break
                    video = pairs[0][1].video
                    self.msg_queue.put(("status", f"원본 오디오 다운로드: {video.title}"))
                    self.msg_queue.put(("stage",
                        f"◉  원본 다운로드 중  ·  {video.title[:60]}"))
                    try:
                        src = YtWrapper.download_audio(
                            video, tmp_dir, progress_hook=make_dl_hook(video.title)
                        )
                    except Cancelled:
                        cancelled = True
                        break
                    except Exception as e:
                        self.msg_queue.put(("warn", f"다운로드 실패 '{video.title}': {e}"))
                        for idx, _ in pairs:
                            self.msg_queue.put(("track_status", (idx, "failed")))
                        continue

                    for idx, t in pairs:
                        if self._cancel.is_set():
                            cancelled = True
                            break
                        done += 1
                        self.msg_queue.put(("track_status", (idx, "running")))
                        self.msg_queue.put(("status",
                            f"MP3 추출 ({done}/{total}): {t.title}"))
                        self.msg_queue.put(("stage",
                            f"◉  MP3 추출 중  ·  {done} / {total}"))
                        self.msg_queue.put(("overall_progress", (done - 1, total)))
                        # 인코딩은 진행률을 모르므로 file bar 는 indeterminate 로 돌린다.
                        self.msg_queue.put(("file_progress",
                                            (None, f"인코딩  ·  {t.title[:50]}", "")))
                        try:
                            split_to_mp3(src, t, out_dir,
                                         start_offset=offset,
                                         register_proc=self._register_proc)
                            self.msg_queue.put(("track_status", (idx, "done")))
                        except subprocess.CalledProcessError as e:
                            if self._cancel.is_set():
                                # 사용자가 취소해 ffmpeg 을 우리가 kill 한 경우.
                                cancelled = True
                                break
                            tail = ""
                            if e.stderr:
                                s = e.stderr if isinstance(e.stderr, str) else \
                                    e.stderr.decode("utf-8", "replace")
                                tail = " — " + s.strip().splitlines()[-1][:200]
                            self.msg_queue.put(("warn",
                                f"MP3 실패 '{t.title}': ffmpeg 종료코드 {e.returncode}{tail}"))
                            self.msg_queue.put(("track_status", (idx, "failed")))
                        except Exception as e:
                            self.msg_queue.put(("warn", f"MP3 실패 '{t.title}': {e}"))
                            self.msg_queue.put(("track_status", (idx, "failed")))
                        self.msg_queue.put(("overall_progress", (done, total)))
                    if cancelled:
                        break
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            if cancelled:
                self.msg_queue.put(("cancelled", None))
            else:
                self.msg_queue.put(("download_done", out_dir))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    # ----- 진행/상태 -----

    def _start_busy(self, stage_text: str, status_text: str = "",
                     cancelable: bool = True,
                     overall_mode: str = "indeterminate"):
        """작업 시작 시 플레이어 바 상태를 세팅.

        overall_mode:
            "indeterminate" — 세부 카운트가 없는 단계(검색/추출). overall
                바를 pulse 애니메이션으로 돌려 "작업 중"임을 확실히 보여준다.
            "determinate"    — done/total 를 overall_progress 메시지로
                업데이트하는 단계(다운로드).
        """
        self.stage_var.set(stage_text)
        if status_text:
            self.status_var.set(status_text)
        self.overall_label_var.set("전체")
        if overall_mode == "indeterminate":
            self.overall_progress.pulse()
        else:
            self.overall_progress.set_progress(0)
        # file 바는 항상 pulse 로 시작. yt-dlp 다운로드 때만 %로 덮어씀.
        self.file_progress.pulse()
        self.file_label_var.set("")
        self.file_rate_var.set("")
        if hasattr(self, "lp"):
            self.lp.start()
        if cancelable and hasattr(self, "btn_cancel"):
            self.btn_cancel.configure(state="normal")
        # 새 작업 시작이면 이전 취소 신호를 리셋.
        self._cancel.clear()
        # 워커가 첫 메시지를 큐에 넣기 전에 이 상태를 화면에 그려 둔다.
        self.update_idletasks()

    def _stop_busy(self, stage_text: str, status_text: str = ""):
        self.overall_progress.stop()
        self.file_progress.stop()
        self.overall_label_var.set("전체")
        self.file_label_var.set("")
        self.file_rate_var.set("")
        self.stage_var.set(stage_text)
        if status_text:
            self.status_var.set(status_text)
        if hasattr(self, "lp"):
            self.lp.stop()
        if hasattr(self, "btn_cancel"):
            self.btn_cancel.configure(state="disabled")

    _TERMINAL_KINDS = frozenset({
        "search_done", "tracks_done", "download_done", "cancelled", "error",
    })

    def _pump_queue(self):
        had_intermediate = False
        deferred: Optional[tuple] = None
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                # 한 tick 안에서 중간 메시지가 먼저 들어왔는데 terminal 이
                # 뒤따라 오면, 중간 상태가 화면에 충분히 그려질 시간을 주기
                # 위해 terminal 을 다음 tick 으로 미룬다.
                if kind in self._TERMINAL_KINDS and had_intermediate:
                    deferred = (kind, payload)
                    break
                if kind not in self._TERMINAL_KINDS:
                    had_intermediate = True
                if kind == "search_done":
                    self._populate_videos(payload)
                    self._stop_busy("◯  대기 중", f"검색 완료: {len(payload)}건")
                    self.btn_search.configure(state="normal")
                elif kind == "tracks_done":
                    tracks, no_tracks = payload
                    # 트랙리스트도 없고 단일곡 길이도 아닌 영상을 결과에서 제거
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
                    self.btn_extract.configure(state="normal")
                    if no_tracks:
                        sample = "\n".join(f"• {t[:60]}"
                                            for _, t in no_tracks[:10])
                        if len(no_tracks) > 10:
                            sample += f"\n... 외 {len(no_tracks) - 10}개"
                        mm = max(1, int(self.single_track_max_min_var.get() or 15))
                        messagebox.showinfo(
                            "처리 불가 영상 제거",
                            f"챕터/설명 타임스탬프가 없고 단일곡({mm}분 이하)도\n"
                            f"아닌 영상 {len(no_tracks)}개를 검색 결과에서 "
                            f"제거했습니다.\n\n{sample}"
                        )
                elif kind == "download_done":
                    self.overall_progress.set_progress(100)
                    self.file_progress.stop()
                    self.file_label_var.set("")
                    self.file_rate_var.set("")
                    self.stage_var.set("✓  완료")
                    self.status_var.set(f"완료. 저장 폴더: {payload}")
                    self.btn_download.configure(state="normal")
                    self.btn_search.configure(state="normal")
                    self.btn_extract.configure(state="normal")
                    self.btn_cancel.configure(state="disabled")
                    self.lp.stop()
                    messagebox.showinfo("완료", f"MP3 저장이 끝났습니다.\n{payload}")
                elif kind == "cancelled":
                    # 사용자가 중단 요청 → 워커가 Cancelled 로 탈출한 직후.
                    self._stop_busy("◯  중단됨", "사용자가 작업을 중단했습니다.")
                    self.btn_search.configure(state="normal")
                    self.btn_extract.configure(state="normal")
                    self.btn_download.configure(state="normal")
                elif kind == "stage":
                    self.stage_var.set(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "warn":
                    self.status_var.set(payload)
                elif kind == "file_progress":
                    # payload = (pct_or_None, label_text, rate_text)
                    #   pct_or_None: 0..100 이면 determinate, None 이면 pulse.
                    pct, label, rate = payload
                    self.file_label_var.set(label or "")
                    self.file_rate_var.set(rate or "")
                    if pct is None:
                        self.file_progress.pulse()
                    else:
                        self.file_progress.set_progress(pct)
                elif kind == "overall_progress":
                    # payload = (done, total)
                    done, total = payload
                    pct = (done / total * 100.0) if total else 0.0
                    self.overall_progress.set_progress(pct)
                    self.overall_label_var.set(f"전체  {done} / {total}")
                elif kind == "track_status":
                    idx, status = payload
                    self._set_track_status(idx, status)
                elif kind == "error":
                    self._stop_busy("✕  오류", payload)
                    self.btn_search.configure(state="normal")
                    self.btn_extract.configure(state="normal")
                    self.btn_download.configure(state="normal")
                    self.lp.stop()
                    messagebox.showerror("오류", payload)
        except queue.Empty:
            pass

        if deferred is not None:
            # 250ms 뒤 다시 큐에 넣으면 그 사이 pump tick 이 한 번 돌며
            # 방금 적용된 intermediate 상태가 화면에 그려진다.
            self.update_idletasks()
            kind, payload = deferred
            self.after(250,
                       lambda k=kind, p=payload: self.msg_queue.put((k, p)))

        self.after(120, self._pump_queue)


if __name__ == "__main__":
    App().mainloop()
