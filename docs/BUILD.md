# Building the Windows binary

The release artifact is a single `YouTubeMusicExtractor.exe` built with
PyInstaller, with `ffmpeg.exe` bundled inside.

## Prerequisites

- Python 3.11+ on Windows
- `pip install -r requirements.txt`
- `pip install pyinstaller`
- An `ffmpeg.exe` build. The releases are made against the
  [gyan.dev "essentials" build](https://www.gyan.dev/ffmpeg/builds/) — LGPL,
  Windows x64. Any static `ffmpeg.exe` should work.

## Steps

1. Place the FFmpeg binary at `vendor/ffmpeg.exe` (this path is not committed).

2. Build:

   ```bash
   pyinstaller youtube-music-extractor.spec --clean --noconfirm
   ```

3. The artifact is `dist/YouTubeMusicExtractor.exe`. Launch it — there
   should be no console window, and track extraction should work without
   FFmpeg installed on `PATH`.

## Licensing note

The bundled FFmpeg is LGPL. When distributing the `.exe`, include a link
to the FFmpeg source for the exact build used, and keep the FFmpeg license
notice alongside the release. The release page on GitHub is a convenient
place for both.
