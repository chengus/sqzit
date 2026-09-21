from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable


VIDEO_CODECS = {
    "H.264": "libx264",
    "H.265 / HEVC": "libx265",
    "AV1": "libsvtav1",
    "Copy video stream": "copy",
}

IMAGE_FORMATS = {
    "Preserve image format": "preserve",
    "WebP": "webp",
    "AVIF": "avif",
    "PNG (lossless)": "png",
}


@dataclass(frozen=True)
class CompressionProfile:
    name: str
    video_codec: str = "libx264"
    video_crf: int = 23
    video_preset: str = "medium"
    image_format: str = "preserve"
    image_quality: int = 82
    lossless: bool = False

    def with_overrides(self, **kwargs: object) -> "CompressionProfile":
        return replace(self, **kwargs)


PROFILES: dict[str, CompressionProfile] = {
    "balanced": CompressionProfile(
        name="Balanced",
        video_codec="libx264",
        video_crf=23,
        video_preset="medium",
        image_format="preserve",
        image_quality=82,
    ),
    "smaller": CompressionProfile(
        name="Smaller files",
        video_codec="libx265",
        video_crf=28,
        video_preset="medium",
        image_format="webp",
        image_quality=76,
    ),
    "lossless": CompressionProfile(
        name="Lossless",
        video_codec="libx264",
        video_crf=0,
        video_preset="medium",
        image_format="webp",
        image_quality=100,
        lossless=True,
    ),
}


def get_profile(name: str) -> CompressionProfile:
    try:
        return PROFILES[name]
    except KeyError as exc:
        choices = ", ".join(sorted(PROFILES))
        raise ValueError(f"Unknown profile {name!r}; choose from {choices}") from exc


def profile_names() -> Iterable[tuple[str, str]]:
    for key, profile in PROFILES.items():
        yield profile.name, key


def estimate_factor(kind: str, profile: CompressionProfile, source_codec: str | None) -> float:
    """Return a deliberately conservative scan-time size estimate.

    This is only used for initial selection. The actual encoded size remains
    the source of truth before anything is replaced or copied.
    """

    if profile.lossless:
        return 0.98 if kind == "video" else 1.02
    if kind == "image":
        if profile.image_format == "webp":
            return 0.55 if profile.image_quality >= 80 else 0.38
        if profile.image_format == "avif":
            return 0.45 if profile.image_quality >= 80 else 0.30
        if profile.image_format == "png":
            return 1.02
        return max(0.52, min(0.90, profile.image_quality / 100 + 0.05))
    if profile.video_codec == "copy":
        return 1.0
    if source_codec == profile.video_codec.removeprefix("lib"):
        return 0.82 if profile.video_crf <= 24 else 0.70
    return 0.62 if profile.video_crf <= 24 else 0.48
