from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable

from .ffmpeg import ProcessControl, compress_item
from .models import CompressionResult, MediaItem
from .profiles import CompressionProfile


@dataclass
class JobUpdate:
    index: int
    total: int
    item: MediaItem
    result: CompressionResult | None = None
    progress: float | None = None


class CompressionRunner:
    def __init__(self, items: list[MediaItem], profile: CompressionProfile, mode: str, jobs: int):
        self.items = items
        self.profile = profile
        self.mode = mode
        self.jobs = max(1, min(jobs, 8))
        self.control = ProcessControl(threading.Event(), threading.Event())

    def pause(self) -> None:
        self.control.paused.set()

    def resume(self) -> None:
        self.control.paused.clear()

    def cancel(self) -> None:
        self.control.cancelled.set()

    def run(self, on_update: Callable[[JobUpdate], None]) -> list[CompressionResult]:
        total = len(self.items)
        results: list[CompressionResult] = []

        def work(index: int, item: MediaItem) -> JobUpdate:
            if self.control.cancelled.is_set():
                result = CompressionResult(
                    item.path,
                    None,
                    self.mode,
                    "cancelled",
                    item.size_bytes,
                    message="cancelled",
                )
                return JobUpdate(index, total, item, result=result)

            started = time.monotonic()

            def progress(value: float | None, _filename: str) -> None:
                on_update(JobUpdate(index, total, item, progress=value))

            try:
                raw = compress_item(item, self.profile, self.mode, self.control, progress)
                output = raw.get("output")
                result = CompressionResult(
                    source=item.path,
                    output=output,
                    mode=self.mode,
                    status=raw["status"],
                    source_size=item.size_bytes,
                    output_size=output.stat().st_size if output else None,
                    message=raw.get("message", ""),
                    duration_seconds=time.monotonic() - started,
                )
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop a batch
                result = CompressionResult(
                    source=item.path,
                    output=None,
                    mode=self.mode,
                    status="failed",
                    source_size=item.size_bytes,
                    message=str(exc),
                    duration_seconds=time.monotonic() - started,
                )
            return JobUpdate(index, total, item, result=result)

        with ThreadPoolExecutor(max_workers=self.jobs, thread_name_prefix="sqzit") as executor:
            futures: list[Future[JobUpdate]] = [
                executor.submit(work, index, item)
                for index, item in enumerate(self.items)
            ]
            for future in as_completed(futures):
                update = future.result()
                if update.result is not None:
                    results.append(update.result)
                on_update(update)
        return sorted(results, key=lambda result: str(result.source))
