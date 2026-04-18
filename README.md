# YouTube Music Extractor

A desktop GUI that searches YouTube, detects track boundaries from video
chapters or description timestamps, and exports selected tracks as
high-quality MP3 files.

## Features

- Keyword search (via `yt-dlp`) returning N videos with duration and channel
- Checkbox-based video selection + multi-select
- Track detection from YouTube chapters (preferred) or description timestamps
- Per-track checkbox selection and removal
- One-shot download of source audio per video (reused across tracks)
- MP3 encoding at **320 kbps CBR / 48 kHz / stereo** with ID3v2.3 tags
- Configurable start offset (default 0.25 s) to avoid bleeding the tail of
  the previous track
- Live per-track status (pending / running / done / failed) and a
  determinate progress bar with yt-dlp download stats
- Filters out videos without any tracklist information so they don't appear
  in the results after an extraction attempt

## Requirements

- Python 3.11+
- `yt-dlp` (`pip install -r requirements.txt`)
- `ffmpeg` on `PATH` (https://ffmpeg.org/)

## Run

```bash
pip install -r requirements.txt
python main.py
```

## Workflow

1. Enter a keyword, set result count, click **검색 (Search)**
2. Check the videos you want to process, click **선택 영상 → 트랙 추출**
3. In the tracks panel, check the tracks you want, click **선택 트랙 MP3 저장**

MP3 files are written to the output folder you configured.

## Notes

- Track boundaries come from the source video. If an uploader's timestamps
  are imprecise, the extracted cuts will be equally imprecise. The start
  offset spinner lets you nudge boundaries forward.
- Videos without chapters and without parseable timestamps in the
  description are automatically removed from the results after extraction.

## Disclaimer

This tool is for personal, lawful use only. Users are responsible for
complying with YouTube's Terms of Service and the copyright laws of their
jurisdiction. Do not use this tool to download content you do not have the
right to download.

## License

MIT — see [LICENSE](LICENSE).
