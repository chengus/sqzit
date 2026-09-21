from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


MediaKind = Literal["image", "video"]
OutputMode = Literal["replace", "copy"]


@dataclass
class MediaItem:
    """A media file discovered during the scan phase."""

    path: Path
    kind: MediaKind
    size_bytes: int
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    format_name: str | None = None
    estimated_output_bytes: int | None = None
    selected: bool = False
    status: str = "ready"
    error: str | None = None

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def source_codec(self) -> str:
        return self.video_codec or "unknown"

    @property
    def estimated_savings_bytes(self) -> int | None:
        if self.estimated_output_bytes is None:
            return None
        return max(0, self.size_bytes - self.estimated_output_bytes)

    @property
    def estimated_savings_ratio(self) -> float | None:
        if not self.size_bytes or self.estimated_output_bytes is None:
            return None
        return max(0.0, 1 - self.estimated_output_bytes / self.size_bytes)


@dataclass
class CompressionResult:
    source: Path
    output: Path | None
    mode: OutputMode
    status: Literal["compressed", "skipped", "failed", "cancelled"]
    source_size: int
    output_size: int | None = None
    message: str = ""
    duration_seconds: float = 0.0


@dataclass
class ScanResult:
    items: list[MediaItem] = field(default_factory=list)
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
