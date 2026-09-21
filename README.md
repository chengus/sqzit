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

In the TUI, use `A` to select all, `N` for none, `I` to invert, Space to
toggle the current row, `P` to pause/resume, and `C` to cancel. The profile
controls can be overridden per run for video codec/CRF, image format/quality,
and lossless mode. sqzit checks that the selected FFmpeg encoders are
available before starting a batch.
