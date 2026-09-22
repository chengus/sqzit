# sqzit

`sqzit` is a terminal UI for scanning and compressing image/video folders with
FFmpeg. It scans first, lets you choose files, and only installs an encoded
file when it is valid and smaller than the source.

## Requirements

- Python 3.12+
- FFmpeg and FFprobe on `PATH`
- macOS (initial target)

## Run from a checkout

```bash
uv run sqzit /path/to/media
```

Useful options:

```text
--profile balanced|smaller|lossless
--mode replace|copy
--no-recursive
--jobs 1-8
```

Replace mode stores the old file in a sibling `_backup` folder before the new
file is installed. Copy mode writes `name.compressed.ext` beside the source
and never overwrites an existing output.

After scanning, the candidate list receives keyboard focus. Click a row or
move with `j`/`k` or the arrow keys; press `Space` or `Enter` to select or
deselect it. The toolbar shows the current selection count: choose **Select
all** or **Clear**, then press **Compress N** to start the job. Use `?` for the
keyboard guide, `/` to filter paths, `I` to invert selection, `P` to
pause/resume, and `C` to cancel. The profile controls can be overridden per
run for video codec/CRF, image format/quality, and lossless mode. sqzit checks
that the selected FFmpeg encoders are available before starting a batch.

Balanced and Smaller profiles automatically prefer Apple VideoToolbox H.264
and HEVC hardware encoders on Apple Silicon, with software encoder fallback
when the hardware encoder is unavailable or cannot start. After compression,
the activity panel shows each completed file's original size, new size, and
percentage change, followed by the combined totals for successfully compressed
files.
