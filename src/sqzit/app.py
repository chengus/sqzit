from __future__ import annotations

import os
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Select, Static, Switch
from textual.worker import WorkerState

from .ffmpeg import compression_support_errors
from .media import ToolUnavailable, scan_directory
from .models import MediaItem
from .profiles import IMAGE_FORMATS, PROFILES, VIDEO_CODECS, CompressionProfile, profile_names
from .runner import CompressionRunner, JobUpdate


def _human_size(value: int | None) -> str:
    if value is None:
        return "-"
    size = float(value)
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or suffix == "TiB":
            return f"{size:.1f} {suffix}" if suffix != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


def _format_savings(item: MediaItem) -> str:
    ratio = item.estimated_savings_ratio
    return f"{ratio:.0%}" if ratio is not None else "-"


class SqzitApp(App):
    TITLE = "sqzit — media compressor"
    CSS = """
    Screen { background: $surface; }
    #toolbar { height: auto; padding: 1 2; background: $panel; }
    #toolbar Horizontal { height: 3; margin-bottom: 1; }
    #toolbar Label { width: 13; padding: 1 0; }
    #directory { width: 1fr; }
    #profile, #mode, #jobs, #video-codec, #image-format { width: 22; }
    #video-crf, #image-quality { width: 12; }
    #actions { height: 3; }
    #actions Button { margin-right: 1; }
    #summary { height: 3; padding: 1 2; }
    #table { height: 1fr; margin: 0 2; }
    #progress { height: 3; padding: 1 2; }
    .muted { color: $text-muted; }
    """
    BINDINGS = [
        Binding("a", "select_all", "Select all"),
        Binding("n", "select_none", "Select none"),
        Binding("i", "invert_selection", "Invert"),
        Binding("space", "toggle_current", "Toggle"),
        Binding("p", "toggle_pause", "Pause"),
        Binding("c", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        directory: str = ".",
        profile: str = "balanced",
        recursive: bool = True,
        mode: str = "replace",
        jobs: int | None = None,
    ) -> None:
        super().__init__()
        self.initial_directory = directory
        self.initial_profile = profile
        self.initial_recursive = recursive
        self.initial_mode = mode
        self.initial_jobs = jobs or max(1, min(4, (os.cpu_count() or 2) - 1))
        self.items: list[MediaItem] = []
        self.runner: CompressionRunner | None = None
        self.scanning = False
        self.running = False
        self.table_row_keys: list[str] = []
        self.setting_profile_controls = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="toolbar"):
            with Horizontal():
                yield Label("Directory")
                yield Input(value=self.initial_directory, placeholder="Folder to scan", id="directory")
                yield Button("Scan", id="scan", variant="primary")
            with Horizontal():
                yield Label("Profile")
                yield Select(list(profile_names()), value=self.initial_profile, id="profile", allow_blank=False)
                yield Label("Mode")
                yield Select(
                    [("Replace + _backup", "replace"), ("Copy with .compressed suffix", "copy")],
                    value=self.initial_mode,
                    id="mode",
                    allow_blank=False,
                )
                yield Label("Jobs")
                yield Input(value=str(self.initial_jobs), id="jobs", type="integer")
                yield Switch(value=self.initial_recursive, id="recursive")
                yield Label("Recursive")
            with Horizontal():
                yield Label("Video codec")
                yield Select(
                    [(label, value) for label, value in VIDEO_CODECS.items()],
                    value=PROFILES[self.initial_profile].video_codec,
                    id="video-codec",
                    allow_blank=False,
                )
                yield Label("Video CRF")
                yield Input(value=str(PROFILES[self.initial_profile].video_crf), id="video-crf", type="integer")
                yield Label("Image format")
                yield Select(
                    [(label, value) for label, value in IMAGE_FORMATS.items()],
                    value=PROFILES[self.initial_profile].image_format,
                    id="image-format",
                    allow_blank=False,
                )
                yield Label("Image quality")
                yield Input(value=str(PROFILES[self.initial_profile].image_quality), id="image-quality", type="integer")
                yield Switch(value=PROFILES[self.initial_profile].lossless, id="lossless")
                yield Label("Lossless")
            with Horizontal(id="actions"):
                yield Button("Start compression", id="start", variant="success", disabled=True)
                yield Button("Pause", id="pause", disabled=True)
                yield Button("Cancel", id="cancel", disabled=True)
        yield Static("Scan a folder to begin.", id="summary", classes="muted")
        table = DataTable(id="table", cursor_type="row", zebra_stripes=True)
        table.add_columns("Sel", "Path", "Type", "Size", "Codec", "Est. savings", "Status")
        yield table
        yield Static("", id="progress", classes="muted")
        yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(self.start_scan)

    def _profile(self) -> CompressionProfile:
        key = str(self.query_one("#profile", Select).value)
        base = PROFILES[key]
        try:
            video_crf = max(0, min(51, int(self.query_one("#video-crf", Input).value)))
        except ValueError:
            video_crf = base.video_crf
        try:
            image_quality = max(1, min(100, int(self.query_one("#image-quality", Input).value)))
        except ValueError:
            image_quality = base.image_quality
        return base.with_overrides(
            video_codec=str(self.query_one("#video-codec", Select).value),
            video_crf=video_crf,
            image_format=str(self.query_one("#image-format", Select).value),
            image_quality=image_quality,
            lossless=self.query_one("#lossless", Switch).value,
        )

    def _apply_profile_controls(self, profile: CompressionProfile) -> None:
        self.setting_profile_controls = True
        try:
            self.query_one("#video-codec", Select).value = profile.video_codec
            self.query_one("#video-crf", Input).value = str(profile.video_crf)
            self.query_one("#image-format", Select).value = profile.image_format
            self.query_one("#image-quality", Input).value = str(profile.image_quality)
            self.query_one("#lossless", Switch).value = profile.lossless
        finally:
            self.setting_profile_controls = False

    def _mode(self) -> str:
        return str(self.query_one("#mode", Select).value)

    def _set_summary(self, message: str) -> None:
        self.query_one("#summary", Static).update(message)

    def start_scan(self) -> None:
        if self.scanning or self.running:
            return
        directory = self.query_one("#directory", Input).value.strip() or "."
        self.scanning = True
        self.query_one("#scan", Button).disabled = True
        self.query_one("#start", Button).disabled = True
        self._set_summary("Scanning with ffprobe…")
        self.run_worker(
            lambda: scan_directory(
                Path(directory),
                self._profile(),
                self.query_one("#recursive", Switch).value,
            ),
            name="scan",
            thread=True,
            exclusive=True,
        )

    def on_worker_state_changed(self, event) -> None:
        if event.worker.state not in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            return
        if event.worker.name == "scan":
            self.scanning = False
            self.query_one("#scan", Button).disabled = False
            if event.worker.error:
                error = event.worker.error
                message = str(error)
                if isinstance(error, ToolUnavailable):
                    message = f"FFmpeg unavailable: {error}"
                self._set_summary(message)
                return
            result = event.worker.result
            self.items = result.items
            self._refresh_table()
            eligible = sum(item.selected for item in self.items)
            self._set_summary(
                f"Found {len(self.items)} media files; {eligible} selected; "
                f"{result.skipped} skipped. Press Enter/Space to toggle, A/N/I to select."
            )
            self.query_one("#start", Button).disabled = not bool(self.items)
        elif event.worker.name == "compression":
            self.running = False
            self.query_one("#pause", Button).disabled = True
            self.query_one("#cancel", Button).disabled = True
            self.query_one("#scan", Button).disabled = False
            if event.worker.error:
                self._set_summary(f"Compression worker failed: {event.worker.error}")
                return
            results = event.worker.result
            counts = {
                status: sum(result.status == status for result in results)
                for status in ("compressed", "skipped", "failed", "cancelled")
            }
            self._set_summary(
                f"Done — compressed {counts['compressed']}, skipped {counts['skipped']}, "
                f"failed {counts['failed']}, cancelled {counts['cancelled']}."
            )

    def _refresh_table(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear(columns=False)
        self.table_row_keys.clear()
        for item in self.items:
            key = str(item.path)
            self.table_row_keys.append(key)
            table.add_row(
                "☑" if item.selected else "☐",
                str(item.path),
                item.kind,
                _human_size(item.size_bytes),
                item.source_codec,
                _format_savings(item),
                item.status,
                key=key,
            )

    def _set_selection(self, value: bool | None) -> None:
        if value is None:
            for item in self.items:
                item.selected = not item.selected
        else:
            for item in self.items:
                item.selected = value
        self._refresh_table()

    def action_select_all(self) -> None:
        self._set_selection(True)

    def action_select_none(self) -> None:
        self._set_selection(False)

    def action_invert_selection(self) -> None:
        self._set_selection(None)

    def action_toggle_current(self) -> None:
        table = self.query_one("#table", DataTable)
        if 0 <= table.cursor_row < len(self.items):
            self.items[table.cursor_row].selected = not self.items[table.cursor_row].selected
            self._refresh_table()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if not self.running:
            self.action_toggle_current()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "scan":
            self.start_scan()
        elif button_id == "start":
            self.start_compression()
        elif button_id == "pause":
            self.action_toggle_pause()
        elif button_id == "cancel":
            self.action_cancel()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "profile" or self.setting_profile_controls:
            return
        profile = PROFILES.get(str(event.value))
        if profile is not None:
            self._apply_profile_controls(profile)
            if self.items and not self.running:
                self.start_scan()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "lossless" and self.items and not self.running and not self.setting_profile_controls:
            self.start_scan()

    def start_compression(self) -> None:
        selected = [item for item in self.items if item.selected]
        if not selected or self.running:
            return
        support_errors = compression_support_errors(selected, self._profile(), self._mode())
        if support_errors:
            details = "; ".join(support_errors[:3])
            suffix = " …" if len(support_errors) > 3 else ""
            self._set_summary(f"Cannot start: {details}{suffix}")
            return
        try:
            jobs = max(1, min(8, int(self.query_one("#jobs", Input).value)))
        except ValueError:
            self._set_summary("Jobs must be a number between 1 and 8.")
            return
        self.runner = CompressionRunner(selected, self._profile(), self._mode(), jobs)
        self.running = True
        self.query_one("#start", Button).disabled = True
        self.query_one("#scan", Button).disabled = True
        self.query_one("#pause", Button).disabled = False
        self.query_one("#cancel", Button).disabled = False
        self._set_summary(f"Compressing {len(selected)} selected files…")
        self.run_worker(self._run_jobs, name="compression", thread=True, exclusive=True)

    def _run_jobs(self):
        assert self.runner is not None
        return self.runner.run(lambda update: self.call_from_thread(self._handle_update, update))

    def _handle_update(self, update: JobUpdate) -> None:
        if update.result is None:
            progress = f"{update.progress:.0%}" if update.progress is not None else "working"
            self.query_one("#progress", Static).update(f"{update.item.filename}: {progress}")
            return
        item = next((candidate for candidate in self.items if candidate.path == update.item.path), None)
        if item is not None:
            item.status = update.result.status
            item.error = update.result.message if update.result.status == "failed" else None
            self._refresh_table()
        self.query_one("#progress", Static).update(
            f"{update.result.status}: {update.item.filename} — {update.result.message}"
        )

    def action_toggle_pause(self) -> None:
        if not self.runner:
            return
        if self.runner.control.paused.is_set():
            self.runner.resume()
            self.query_one("#pause", Button).label = "Pause"
        else:
            self.runner.pause()
            self.query_one("#pause", Button).label = "Resume"

    def action_cancel(self) -> None:
        if self.runner:
            self.runner.cancel()
            self.query_one("#progress", Static).update("Cancelling active jobs safely…")


def run_app(**kwargs: object) -> None:
    SqzitApp(**kwargs).run()
