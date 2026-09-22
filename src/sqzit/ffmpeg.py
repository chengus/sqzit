from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

from .media import probe_file
from .models import MediaItem
from .output import copy_output_path, install_copy, replace_with_backup, temporary_path
from .profiles import CompressionProfile


ProgressCallback = Callable[[float | None, str, str], None]


@dataclass
class ProcessControl:
    paused: threading.Event
    cancelled: threading.Event


@lru_cache(maxsize=1)
def is_apple_silicon() -> bool:
    if sys.platform != "darwin":
        return False
    if platform.machine().lower() in {"arm64", "aarch64"}:
        return True
    # Detect Apple Silicon when Python itself is running under Rosetta.
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.optional.arm64"],
            capture_output=True,
            text=True,
            check=True,
            timeout=3,
        )
        return result.stdout.strip() == "1"
    except (OSError, subprocess.SubprocessError):
        return False


@lru_cache(maxsize=1)
def available_encoders() -> frozenset[str]:
    try:
        completed = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    encoders: set[str] = set()
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and "V" in parts[0]:
            encoders.add(parts[1])
    return frozenset(encoders)


def preserved_image_encoder(item: MediaItem) -> str | None:
    return {
        ".jpg": "mjpeg",
        ".jpeg": "mjpeg",
        ".png": "png",
        ".webp": "libwebp",
        ".avif": "libaom-av1",
    }.get(item.path.suffix.lower())


def software_fallback_encoder(profile: CompressionProfile) -> str | None:
    return {
        "auto_h264": "libx264",
        "auto_hevc": "libx265",
    }.get(profile.video_codec)


def resolve_video_encoder(profile: CompressionProfile) -> str:
    if profile.video_codec not in {"auto_h264", "auto_hevc"}:
        return profile.video_codec
    fallback = software_fallback_encoder(profile)
    if profile.lossless or not is_apple_silicon():
        return fallback or "libx264"
    hardware_encoder = {
        "auto_h264": "h264_videotoolbox",
        "auto_hevc": "hevc_videotoolbox",
    }[profile.video_codec]
    if hardware_encoder in available_encoders():
        return hardware_encoder
    return fallback or "libx264"


def required_encoder(item: MediaItem, profile: CompressionProfile, mode: str) -> str | None:
    effective_profile = profile
    if mode == "replace" and item.kind == "image" and profile.image_format != "preserve":
        effective_profile = profile.with_overrides(image_format="preserve")
    if item.kind == "video":
        return None if effective_profile.video_codec == "copy" else resolve_video_encoder(effective_profile)
    if effective_profile.image_format == "webp":
        return "libwebp"
    if effective_profile.image_format == "avif":
        return "libaom-av1"
    if effective_profile.image_format == "png":
        return "png"
    return preserved_image_encoder(item)


def compression_support_errors(
    items: list[MediaItem], profile: CompressionProfile, mode: str
) -> list[str]:
    encoders = available_encoders()
    errors: list[str] = []
    for item in items:
        encoder = required_encoder(item, profile, mode)
        if encoder is None:
            if item.kind == "video" and profile.video_codec == "copy":
                continue
            errors.append(f"{item.filename}: no encoder for {item.path.suffix.lower()}")
        elif encoder not in encoders:
            errors.append(f"{item.filename}: FFmpeg encoder {encoder!r} is unavailable")
    return errors


def output_suffix(item: MediaItem, profile: CompressionProfile) -> str:
    if item.kind == "video" or profile.image_format == "preserve":
        return item.path.suffix
    return "." + profile.image_format


def encoder_for_item(item: MediaItem, profile: CompressionProfile) -> str:
    if item.kind == "video":
        return profile.video_codec
    if profile.image_format == "webp" or item.path.suffix.lower() == ".webp":
        return "libwebp"
    if profile.image_format == "avif":
        return "libaom-av1"
    if profile.image_format == "png":
        return "png"
    return preserved_image_encoder(item) or "unknown encoder"


def build_ffmpeg_command(
    item: MediaItem,
    profile: CompressionProfile,
    output: Path,
) -> list[str]:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(item.path),
        "-map_metadata",
        "0",
        "-map",
        "0:v:0",
    ]
    if item.kind == "video":
        command += ["-map", "0:a?", "-c:a", "copy"]
        if profile.video_codec == "copy":
            command += ["-c:v", "copy"]
        elif profile.video_codec in {"h264_videotoolbox", "hevc_videotoolbox"}:
            quality = max(1, min(100, round((51 - profile.video_crf) * 100 / 51)))
            command += ["-c:v", profile.video_codec, "-allow_sw", "0", "-q:v", str(quality)]
        else:
            command += ["-c:v", profile.video_codec]
            if profile.lossless:
                command += ["-qp", "0"]
            else:
                command += ["-crf", str(profile.video_crf), "-preset", profile.video_preset]
        if output.suffix.lower() in {".mp4", ".m4v", ".mov"}:
            command += ["-movflags", "+faststart"]
    else:
        command += ["-frames:v", "1"]
        image_format = profile.image_format
        if image_format == "png":
            command += ["-c:v", "png", "-compression_level", "9"]
        elif image_format == "avif":
            command += ["-c:v", "libaom-av1", "-still-picture", "1", "-crf", str(max(0, 63 - profile.image_quality // 2))]
        elif image_format == "webp" or item.path.suffix.lower() == ".webp":
            command += ["-c:v", "libwebp"]
            if profile.lossless:
                command += ["-lossless", "1", "-compression_level", "6"]
            else:
                command += ["-q:v", str(profile.image_quality)]
        else:
            encoder = preserved_image_encoder(item)
            if encoder is None:
                raise ValueError(f"cannot preserve image format {item.path.suffix.lower()}")
            if encoder == "mjpeg":
                command += ["-c:v", encoder, "-q:v", str(max(2, min(31, 31 - profile.image_quality // 4)))]
            elif encoder == "png":
                command += ["-c:v", encoder, "-compression_level", "9"]
            elif encoder == "libaom-av1":
                command += ["-c:v", encoder, "-still-picture", "1", "-crf", str(max(0, 63 - profile.image_quality // 2))]
            else:
                command += ["-c:v", encoder, "-q:v", str(profile.image_quality)]
    command += ["-progress", "pipe:1", "-nostats", str(output)]
    return command


def _duration(item: MediaItem) -> float | None:
    return item.duration if item.duration and item.duration > 0 else None


def run_ffmpeg(
    item: MediaItem,
    profile: CompressionProfile,
    output: Path,
    control: ProcessControl,
    progress: ProgressCallback | None = None,
) -> None:
    command = build_ffmpeg_command(item, profile, output)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert process.stdout is not None
        if progress:
            progress(None, item.filename, encoder_for_item(item, profile))
        for line in process.stdout:
            if control.cancelled.is_set():
                os.killpg(process.pid, signal.SIGCONT)
                os.killpg(process.pid, signal.SIGTERM)
                break
            while control.paused.is_set() and not control.cancelled.is_set():
                os.killpg(process.pid, signal.SIGSTOP)
                time.sleep(0.25)
            if control.cancelled.is_set():
                os.killpg(process.pid, signal.SIGCONT)
                os.killpg(process.pid, signal.SIGTERM)
                break
            if not control.paused.is_set():
                os.killpg(process.pid, signal.SIGCONT)
            if progress and line.startswith("out_time_ms="):
                try:
                    seconds = int(line.split("=", 1)[1]) / 1_000_000
                    total = _duration(item)
                    progress(
                        min(1.0, seconds / total) if total else None,
                        item.filename,
                        encoder_for_item(item, profile),
                    )
                except (TypeError, ValueError):
                    pass
        return_code = process.wait()
        if control.cancelled.is_set():
            raise InterruptedError("cancelled")
        if return_code != 0:
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError(stderr.strip() or f"ffmpeg exited with code {return_code}")
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def validate_output(output: Path, item: MediaItem) -> None:
    if not output.exists() or output.stat().st_size == 0:
        raise ValueError("ffmpeg produced no output")
    data = probe_file(output)
    if not any(stream.get("codec_type") == "video" for stream in data.get("streams", [])):
        raise ValueError("ffmpeg output has no readable video stream")


def compress_item(
    item: MediaItem,
    profile: CompressionProfile,
    mode: str,
    control: ProcessControl,
    progress: ProgressCallback | None = None,
):
    # Replace mode keeps the original filename and extension. A format change
    # such as JPEG -> WebP is therefore only applied in copy mode; otherwise
    # the result would contain WebP bytes under a misleading original name.
    effective_profile = profile
    if mode == "replace" and item.kind == "image" and profile.image_format != "preserve":
        effective_profile = profile.with_overrides(image_format="preserve")
    source_size = item.path.stat().st_size
    encoder = required_encoder(item, effective_profile, mode)
    if encoder is None and not (item.kind == "video" and effective_profile.video_codec == "copy"):
        raise ValueError(f"cannot encode {item.path.suffix.lower()} with the selected profile")
    if encoder and encoder not in available_encoders():
        raise RuntimeError(f"FFmpeg encoder {encoder!r} is unavailable")
    suffix = output_suffix(item, effective_profile)
    destination = copy_output_path(item.path, suffix) if mode == "copy" else item.path
    encoded = temporary_path(item.path.parent, suffix)
    encode_profile = effective_profile
    if item.kind == "video" and effective_profile.video_codec != "copy":
        encode_profile = effective_profile.with_overrides(video_codec=encoder)
    try:
        try:
            run_ffmpeg(item, encode_profile, encoded, control, progress)
        except (OSError, RuntimeError):
            fallback = software_fallback_encoder(effective_profile)
            if (
                item.kind != "video"
                or fallback is None
                or encoder == fallback
                or fallback not in available_encoders()
            ):
                raise
            encoded.unlink(missing_ok=True)
            encoder = fallback
            encode_profile = effective_profile.with_overrides(video_codec=fallback)
            if progress:
                progress(0.0, item.filename, fallback)
            run_ffmpeg(item, encode_profile, encoded, control, progress)
        validate_output(encoded, item)
        if encoded.stat().st_size >= source_size:
            return {
                "status": "skipped",
                "output": None,
                "message": f"{encoder}; encoded file was not smaller",
            }
        if mode == "replace":
            backup = replace_with_backup(item.path, encoded)
            return {
                "status": "compressed",
                "output": item.path,
                "message": f"{encoder}; backup: {backup}",
            }
        output = install_copy(item.path, encoded, destination)
        return {"status": "compressed", "output": output, "message": f"{encoder}; {output}"}
    except InterruptedError:
        return {"status": "cancelled", "output": None, "message": "cancelled"}
    finally:
        encoded.unlink(missing_ok=True)
