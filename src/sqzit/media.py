from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .models import MediaItem, ScanResult
from .profiles import CompressionProfile, estimate_factor


IMAGE_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}
VIDEO_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mts",
    ".ts",
    ".webm",
    ".wmv",
}


class ToolUnavailable(RuntimeError):
    pass


def ensure_ffmpeg() -> None:
    for executable in ("ffmpeg", "ffprobe"):
        try:
            subprocess.run(
                [executable, "-version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.SubprocessError) as exc:
            raise ToolUnavailable(
                "sqzit requires ffmpeg and ffprobe on PATH. "
                "Install FFmpeg and try again."
            ) from exc


def _is_hidden_or_backup(path: Path, root: Path) -> bool:
    try:
        relative_parts = path.relative_to(root).parts
    except ValueError:
        relative_parts = path.parts
    return any(part.startswith(".") or part == "_backup" for part in relative_parts)


def discover_paths(root: Path, recursive: bool) -> list[Path]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")

    paths: list[Path] = []
    if recursive:
        for current, dirs, files in os.walk(root):
            current_path = Path(current)
            dirs[:] = [
                name
                for name in dirs
                if not name.startswith(".") and name != "_backup"
            ]
            for name in files:
                path = current_path / name
                if not _is_hidden_or_backup(path, root):
                    paths.append(path)
    else:
        paths = [
            path
            for path in root.iterdir()
            if path.is_file() and not path.name.startswith(".") and path.suffix.lower() != ""
        ]
    return sorted(paths, key=lambda item: str(item).lower())


def probe_file(path: Path, timeout: float = 15) -> dict:
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
        return json.loads(completed.stdout or "{}")
    except FileNotFoundError as exc:
        raise ToolUnavailable("ffprobe was not found on PATH") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise ValueError(f"ffprobe could not read {path.name}") from exc


def _first_stream(data: dict, codec_type: str) -> dict | None:
    return next(
        (stream for stream in data.get("streams", []) if stream.get("codec_type") == codec_type),
        None,
    )


def media_item_from_probe(path: Path, data: dict, profile: CompressionProfile) -> MediaItem | None:
    video = _first_stream(data, "video")
    if video is None:
        return None
    suffix = path.suffix.lower()
    kind = "image" if suffix in IMAGE_EXTENSIONS else "video"
    if suffix not in IMAGE_EXTENSIONS and suffix not in VIDEO_EXTENSIONS:
        kind = "image" if data.get("format", {}).get("format_name", "").startswith("image") else "video"
    size_bytes = path.stat().st_size
    factor = estimate_factor(kind, profile, video.get("codec_name"))
    estimated = max(1, round(size_bytes * factor))
    audio = _first_stream(data, "audio")
    duration_value = video.get("duration") or data.get("format", {}).get("duration")
    try:
        duration = float(duration_value) if duration_value is not None else None
    except (TypeError, ValueError):
        duration = None
    return MediaItem(
        path=path,
        kind=kind,
        size_bytes=size_bytes,
        width=int(video["width"]) if video.get("width") else None,
        height=int(video["height"]) if video.get("height") else None,
        duration=duration,
        video_codec=video.get("codec_name"),
        audio_codec=audio.get("codec_name") if audio else None,
        format_name=data.get("format", {}).get("format_name"),
        estimated_output_bytes=estimated,
        selected=estimated < size_bytes,
    )


def scan_directory(root: Path, profile: CompressionProfile, recursive: bool = True) -> ScanResult:
    ensure_ffmpeg()
    result = ScanResult()
    for path in discover_paths(root, recursive):
        if path.suffix.lower() not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
            result.skipped += 1
            continue
        try:
            data = probe_file(path)
            item = media_item_from_probe(path, data, profile)
            if item is not None:
                result.items.append(item)
            else:
                result.skipped += 1
        except (OSError, ValueError) as exc:
            result.skipped += 1
            result.errors.append(f"{path}: {exc}")
    return result
