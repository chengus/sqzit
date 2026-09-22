from __future__ import annotations

import os
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Select, Static, Switch
from textual.worker import WorkerState

from .ffmpeg import compression_support_errors
from .media import ToolUnavailable, scan_directory
from .models import MediaItem
from .profiles import IMAGE_FORMATS, PROFILES, VIDEO_CODECS, CompressionProfile, profile_names
from .runner import CompressionRunner, JobUpdate


class HelpScreen(ModalScreen[None]):
    """A keyboard-first help overlay for the current application context."""

    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("q", "close", "Close", show=False),
    ]
    CSS = """
    HelpScreen { align: center middle; background: rgba(0, 0, 0, 0.72); }
    #help-card {
        width: 74;
        max-width: 90%;
        height: auto;
        max-height: 85%;
        padding: 1 2;
        background: $bg-overlay;
        border: round $accent-primary;
    }
    #help-title { color: $fg-emphasis; text-style: bold; margin-bottom: 1; }
    #help-body { color: $fg-default; margin-bottom: 1; }
    #help-close { width: 18; }
    """

    def compose(self) -> ComposeResult:
        body = (
            "NAVIGATION\n"
            "  ↑/↓ or j/k   move through candidates\n"
            "  Tab / Shift+Tab   move between panels\n"
            "  Click / Enter / Space   toggle the highlighted file\n"
            "  Compress N   start compression for selected files\n"
            "  /              filter paths\n"
            "\n"
            "SELECTION\n"
            "  a   select all     n   select none     i   invert\n"
            "\n"
            "JOB CONTROL\n"
            "  p   pause/resume  c   cancel safely\n"
            "  Scan happens before compression. Outputs are kept only when valid\n"
            "  and smaller. Replace mode saves originals under _backup.\n"
            "\n"
            "GENERAL\n"
            "  q   quit          Esc   close this help"
        )
        with Container(id="help-card"):
            yield Static("KEYBOARD GUIDE", id="help-title")
            yield Static(body, id="help-body")
            yield Button("Close", id="help-close", variant="primary")

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "help-close":
            self.action_close()


def _human_size(value: int | None) -> str:
    if value is None:
        return "-"
    size = float(value)
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or suffix == "TiB":
            return f"{size:.1f} {suffix}" if suffix != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


def _human_media_size(value: int) -> str:
    """Format final before/after sizes in decimal MB or GB."""
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f} GB"
    return f"{value / 1_000_000:.1f} MB"


def _format_size_change(original_bytes: int, new_bytes: int) -> str:
    if original_bytes <= 0:
        return f"{_human_media_size(original_bytes)} > {_human_media_size(new_bytes)}"
    change_percent = (new_bytes - original_bytes) / original_bytes * 100
    return (
        f"{_human_media_size(original_bytes)} > {_human_media_size(new_bytes)} "
        f"({change_percent:.1f}%)"
    )


def _format_savings(item: MediaItem) -> str:
    ratio = item.estimated_savings_ratio
    return f"{ratio:.0%}" if ratio is not None else "-"


def _format_duration(value: float | None) -> str:
    if value is None:
        return "-"
    total = int(value)
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def _status_label(status: str) -> str:
    return {
        "ready": "READY",
        "compressed": "DONE",
        "skipped": "SKIP",
        "failed": "FAIL",
        "cancelled": "STOP",
    }.get(status, status.upper())


def _progress_bar(value: float | None, width: int = 24) -> str:
    if value is None:
        return "working"
    completed = max(0, min(width, round(value * width)))
    return f"{('█' * completed) + ('░' * (width - completed))} {value:.0%}"


def _activity_encoder_label(encoder: str | None, compact: bool = False) -> str:
    labels = {
        "h264_videotoolbox": ("VideoToolbox H.264", "VideoToolbox"),
        "hevc_videotoolbox": ("VideoToolbox HEVC", "VideoToolbox"),
        "libx264": ("Software H.264", "CPU H.264"),
        "libx265": ("Software HEVC", "CPU HEVC"),
        "libsvtav1": ("Software AV1", "CPU AV1"),
        "copy": ("Stream copy", "Copy"),
        "libwebp": ("WebP encoder", "WebP"),
        "libaom-av1": ("AV1 encoder", "AV1"),
        "png": ("PNG encoder", "PNG"),
        "mjpeg": ("JPEG encoder", "JPEG"),
    }
    full, short = labels.get(encoder or "", (encoder or "starting", encoder or "starting"))
    return short if compact else full


class SqzitApp(App):
    TITLE = "sqzit"
    SUB_TITLE = "media compression workspace"
    CSS = """
    $bg-base: #10131a;
    $bg-surface: #1b202b;
    $bg-overlay: #2a3140;
    $fg-default: #d7dce8;
    $fg-muted: #7d8799;
    $fg-emphasis: #f2f5fb;
    $border: #3a4558;
    $accent-primary: #7aa2f7;
    $accent-secondary: #bb9af7;
    $status-success: #9ece6a;
    $status-warning: #e0af68;
    $status-error: #f7768e;
    $status-info: #7dcfff;

    Screen {
        background: $bg-base;
        color: $fg-default;
    }
    Header {
        background: $bg-surface;
        color: $fg-emphasis;
    }
    Footer {
        background: $bg-surface;
        color: $fg-muted;
    }
    #main-layout {
        height: 1fr;
        padding: 1;
    }
    #sidebar {
        width: 34;
        min-width: 25;
        height: 1fr;
        padding: 1 2;
        background: $bg-surface;
        border: round $border;
    }
    #workspace {
        width: 1fr;
        height: 1fr;
        margin-left: 1;
    }
    #brand {
        color: $fg-emphasis;
        text-style: bold;
        margin-bottom: 1;
    }
    .section-title {
        color: $accent-secondary;
        text-style: bold;
        margin-top: 1;
        margin-bottom: 1;
    }
    .field-label {
        color: $fg-muted;
        margin-top: 1;
    }
    .inline-field {
        height: 3;
        align: left middle;
    }
    .inline-field Switch { margin-right: 1; }
    .inline-field Label { color: $fg-default; }
    Input, Select {
        background: $bg-base;
        border: round $border;
    }
    Input:focus, Select:focus {
        border: round $accent-primary;
    }
    #sidebar Input, #sidebar Select { width: 1fr; }
    #sidebar .short-field { width: 12; }
    #selection-toolbar {
        height: 4;
        align: left middle;
        padding: 0 1;
        background: $bg-surface;
    }
    #selection-toolbar Button {
        margin-right: 1;
        min-width: 7;
        color: $fg-emphasis !important;
        text-opacity: 1;
        text-style: bold;
    }
    #selection-toolbar Button:disabled { color: $fg-muted !important; text-opacity: 1; }
    #compress-selected {
        min-width: 17;
        color: $bg-base !important;
        background: $status-success;
        text-opacity: 1;
        text-style: bold;
    }
    #scan { min-width: 7; color: $fg-emphasis !important; background: $accent-primary; }
    #pause, #cancel { display: none; }
    #session-summary {
        height: auto;
        min-height: 5;
        padding: 1;
        color: $fg-muted;
        background: $bg-base;
        border: round $border;
    }
    #workspace-header {
        height: auto;
        padding: 0 1;
        background: $bg-surface;
        border: round $border;
    }
    #workspace-topline { height: 3; }
    #workspace-title { color: $fg-emphasis; text-style: bold; }
    #selection-count { color: $fg-muted; }
    #scan-state { width: 1fr; text-align: right; color: $status-info; }
    #search { display: none; width: 34; }
    #table {
        height: 1fr;
        margin-top: 1;
        border: round $border;
    }
    #table > .datatable--header {
        background: $bg-overlay;
        color: $fg-emphasis;
        text-style: bold;
    }
    #table > .datatable--cursor {
        background: $accent-primary 30%;
        color: $fg-emphasis;
        text-style: bold;
    }
    #detail-row {
        height: 8;
        margin-top: 1;
    }
    .detail-card {
        width: 1fr;
        padding: 0 1;
        background: $bg-surface;
        border: round $border;
    }
    .detail-title { color: $accent-secondary; text-style: bold; }
    .detail-value { color: $fg-default; }
    #activity-card { margin-left: 1; }
    #progress { color: $status-info; }
    #too-small {
        display: none;
        width: 1fr;
        height: 1fr;
        content-align: center middle;
        text-align: center;
        color: $fg-emphasis;
    }
    Screen.too-small #main-layout { display: none; }
    Screen.too-small #too-small { display: block; }

    """
    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("?", "help", "Help", show=True),
        Binding("/", "search", "Filter", show=True),
        Binding("space", "toggle_current", "Toggle", show=True),
        Binding("p", "toggle_pause", "Pause", show=True),
        Binding("a", "select_all", "All", show=False),
        Binding("n", "select_none", "None", show=False),
        Binding("i", "invert_selection", "Invert", show=False),
        Binding("c", "cancel", "Cancel", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
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
        self.job_states: dict[Path, tuple[MediaItem, str, float | None, str | None]] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            "Terminal is too small for the workspace. Resize to at least 80×24.",
            id="too-small",
        )
        with Horizontal(id="main-layout"):
            with VerticalScroll(id="sidebar"):
                yield Static("SQZIT  /  MEDIA COMPRESSION", id="brand")
                yield Static("SOURCE", classes="section-title")
                yield Label("Folder", classes="field-label")
                yield Input(value=self.initial_directory, placeholder="Folder to scan", id="directory")
                with Horizontal(classes="inline-field"):
                    yield Switch(value=self.initial_recursive, id="recursive")
                    yield Label("Recursive scan")

                yield Static("ENCODE PROFILE", classes="section-title")
                yield Label("Preset", classes="field-label")
                yield Select(list(profile_names()), value=self.initial_profile, id="profile", allow_blank=False)
                yield Label("Video codec", classes="field-label")
                yield Select(
                    [(label, value) for label, value in VIDEO_CODECS.items()],
                    value=PROFILES[self.initial_profile].video_codec,
                    id="video-codec",
                    allow_blank=False,
                )
                with Horizontal(classes="inline-field"):
                    yield Label("CRF", classes="field-label")
                    yield Input(
                        value=str(PROFILES[self.initial_profile].video_crf),
                        id="video-crf",
                        classes="short-field",
                        type="integer",
                    )
                    yield Label("Lossless")
                    yield Switch(value=PROFILES[self.initial_profile].lossless, id="lossless")
                yield Label("Image format", classes="field-label")
                yield Select(
                    [(label, value) for label, value in IMAGE_FORMATS.items()],
                    value=PROFILES[self.initial_profile].image_format,
                    id="image-format",
                    allow_blank=False,
                )
                with Horizontal(classes="inline-field"):
                    yield Label("Image quality", classes="field-label")
                    yield Input(
                        value=str(PROFILES[self.initial_profile].image_quality),
                        id="image-quality",
                        classes="short-field",
                        type="integer",
                    )

                yield Static("OUTPUT", classes="section-title")
                yield Select(
                    [("Replace + keep _backup", "replace"), ("Copy as .compressed", "copy")],
                    value=self.initial_mode,
                    id="mode",
                    allow_blank=False,
                )
                with Horizontal(classes="inline-field"):
                    yield Label("Parallel jobs")
                    yield Input(value=str(self.initial_jobs), id="jobs", classes="short-field", type="integer")

                yield Static("Scan a folder to begin.", id="session-summary")

            with Vertical(id="workspace"):
                with Vertical(id="workspace-header"):
                    with Horizontal(id="workspace-topline"):
                        with Vertical():
                            yield Static("MEDIA CANDIDATES", id="workspace-title")
                            yield Static("Space or Enter toggles the focused row", id="selection-count")
                        yield Input(placeholder="Filter paths…", id="search")
                        yield Static("idle", id="scan-state")
                    with Horizontal(id="selection-toolbar"):
                        yield Button("Select all", id="select-all")
                        yield Button("Clear", id="select-none")
                        yield Button("Scan", id="scan", variant="primary")
                        yield Button("Compress 0", id="compress-selected", variant="success", disabled=True)
                        yield Button("Pause", id="pause", disabled=True)
                        yield Button("Cancel", id="cancel", disabled=True)
                table = DataTable(id="table", cursor_type="row", zebra_stripes=True)
                table.add_columns("Sel", "Path", "Kind", "Size", "Codec", "Save", "Status")
                yield table
                with Horizontal(id="detail-row"):
                    with Vertical(classes="detail-card"):
                        yield Static("SELECTED FILE", classes="detail-title")
                        yield Static("No file selected", id="detail", classes="detail-value")
                    with Vertical(classes="detail-card", id="activity-card"):
                        yield Static("ACTIVITY", classes="detail-title")
                        yield Static("Waiting for a scan", id="progress", classes="detail-value")
                        yield Static("", id="activity-message", classes="detail-value")
        yield Footer()

    def on_mount(self) -> None:
        self._check_size()
        self.call_after_refresh(self.start_scan)

    def on_resize(self, _event: Resize) -> None:
        self._check_size()

    def _check_size(self) -> None:
        too_small = self.size.width < 80 or self.size.height < 24
        if too_small:
            self.add_class("too-small")
        else:
            self.remove_class("too-small")
        if self.is_mounted:
            sidebar = self.query_one("#sidebar", VerticalScroll)
            detail_row = self.query_one("#detail-row", Horizontal)
            sidebar.styles.width = 28 if self.size.width < 105 else 34
            detail_row.styles.height = 8
            self.query_one("#select-all", Button).label = "All" if self.size.width < 105 else "Select all"
            self.query_one("#compress-selected", Button).styles.min_width = (
                14 if self.size.width < 105 else 17
            )

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

    def _mode(self) -> str:
        return str(self.query_one("#mode", Select).value)

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

    def _set_summary(self, message: str) -> None:
        self.query_one("#session-summary", Static).update(message)

    def _update_selection_count(self) -> None:
        selected = sum(item.selected for item in self.items)
        self.query_one("#selection-count", Static).update(
            f"{selected} of {len(self.items)} selected  ·  Space/Enter toggles row"
        )
        compress_button = self.query_one("#compress-selected", Button)
        compress_button.label = f"Compress {selected}"
        compress_button.disabled = selected == 0 or self.running

    def start_scan(self) -> None:
        if self.scanning or self.running or self.has_class("too-small"):
            return
        directory = self.query_one("#directory", Input).value.strip() or "."
        self.scanning = True
        self.query_one("#scan", Button).disabled = True
        self.query_one("#select-all", Button).disabled = True
        self.query_one("#select-none", Button).disabled = True
        self.query_one("#compress-selected", Button).disabled = True
        self.query_one("#scan-state", Static).update("scanning…")
        self.query_one("#progress", Static).update("Working · probing media")
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
            self.query_one("#select-all", Button).disabled = False
            self.query_one("#select-none", Button).disabled = False
            if event.worker.error:
                error = event.worker.error
                message = str(error)
                if isinstance(error, ToolUnavailable):
                    message = f"FFmpeg unavailable: {error}"
                self.query_one("#scan-state", Static).update("error")
                self.query_one("#progress", Static).update("FAIL · scan failed")
                self._set_summary(message)
                return
            result = event.worker.result
            self.items = result.items
            self._refresh_table()
            eligible = sum(item.selected for item in self.items)
            self.query_one("#scan-state", Static).update("ready")
            self.query_one("#progress", Static).update("Scan complete")
            self.query_one("#table", DataTable).focus()
            self._set_summary(
                f"{len(self.items)} media found · {eligible} preselected · {result.skipped} skipped"
            )
            self._update_selection_count()
        elif event.worker.name == "compression":
            self.running = False
            self.job_states.clear()
            self.query_one("#progress", Static).update("Compression finished")
            self.query_one("#activity-message", Static).display = True
            self.query_one("#pause", Button).disabled = True
            self.query_one("#cancel", Button).disabled = True
            self.query_one("#pause", Button).display = False
            self.query_one("#cancel", Button).display = False
            self.query_one("#scan", Button).disabled = False
            self.query_one("#select-all", Button).disabled = False
            self.query_one("#select-none", Button).disabled = False
            self.query_one("#scan-state", Static).update("complete")
            self._update_selection_count()
            if event.worker.error:
                self._set_summary(f"Compression worker failed: {event.worker.error}")
                return
            results = event.worker.result
            counts = {
                status: sum(result.status == status for result in results)
                for status in ("compressed", "skipped", "failed", "cancelled")
            }
            self._set_summary(
                f"DONE {counts['compressed']} compressed · SKIP {counts['skipped']} skipped · "
                f"FAIL {counts['failed']} failed · STOP {counts['cancelled']} cancelled"
            )
            compressed = [result for result in results if result.status == "compressed" and result.output_size]
            if compressed:
                original_total = sum(result.source_size for result in compressed)
                output_total = sum(result.output_size or 0 for result in compressed)
                self.query_one("#activity-message", Static).update(
                    "Batch total\n" + _format_size_change(original_total, output_total)
                )
            else:
                self.query_one("#activity-message", Static).update("No files were reduced.")

    def _filtered_items(self) -> list[MediaItem]:
        query = self.query_one("#search", Input).value.strip().lower()
        if not query:
            return self.items
        return [item for item in self.items if query in str(item.path).lower()]

    def _refresh_table(self) -> None:
        table = self.query_one("#table", DataTable)
        cursor_row = max(table.cursor_row, 0)
        table.clear(columns=False)
        self.table_row_keys.clear()
        for item in self._filtered_items():
            key = str(item.path)
            self.table_row_keys.append(key)
            table.add_row(
                "✓" if item.selected else "○",
                str(item.path),
                item.kind,
                _human_size(item.size_bytes),
                item.source_codec,
                _format_savings(item),
                _status_label(item.status),
                key=key,
            )
        if table.row_count:
            table.move_cursor(row=min(cursor_row, table.row_count - 1), animate=False)
        self._update_selection_count()
        self._update_detail()

    def _update_detail(self) -> None:
        table = self.query_one("#table", DataTable)
        visible = self._filtered_items()
        if not visible or table.cursor_row < 0 or table.cursor_row >= len(visible):
            self.query_one("#detail", Static).update("No file selected")
            return
        item = visible[table.cursor_row]
        size_line = f"{item.kind} · {_human_size(item.size_bytes)} · {_status_label(item.status)}"
        media_line = f"{item.width or '?'}×{item.height or '?'} · {_format_duration(item.duration)} · {item.source_codec}"
        self.query_one("#detail", Static).update(f"{item.filename}\n{size_line}\n{media_line}")

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
        visible = self._filtered_items()
        if 0 <= table.cursor_row < len(visible):
            visible[table.cursor_row].selected = not visible[table.cursor_row].selected
            self._refresh_table()

    def action_cursor_down(self) -> None:
        table = self.query_one("#table", DataTable)
        if table.row_count:
            table.move_cursor(row=min(table.row_count - 1, table.cursor_row + 1), animate=False)
            self._update_detail()

    def action_cursor_up(self) -> None:
        table = self.query_one("#table", DataTable)
        if table.row_count:
            table.move_cursor(row=max(0, table.cursor_row - 1), animate=False)
            self._update_detail()

    def action_search(self) -> None:
        search = self.query_one("#search", Input)
        search.display = True
        search.styles.width = min(34, max(18, self.size.width - 56))
        search.focus()
        self.add_class("search-open")

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def on_key(self, event) -> None:
        if event.key == "escape":
            search = self.query_one("#search", Input)
            if search.display:
                search.value = ""
                search.display = False
                self.remove_class("search-open")
                self.query_one("#table", DataTable).focus()

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        if not self.running:
            self.action_toggle_current()

    def on_data_table_row_highlighted(self, _event: DataTable.RowHighlighted) -> None:
        self._update_detail()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self._refresh_table()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "scan":
            self.start_scan()
        elif button_id == "compress-selected":
            self.start_compression()
        elif button_id == "select-all":
            self.action_select_all()
        elif button_id == "select-none":
            self.action_select_none()
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
            self.query_one("#scan-state", Static).update("blocked")
            return
        try:
            jobs = max(1, min(8, int(self.query_one("#jobs", Input).value)))
        except ValueError:
            self._set_summary("Parallel jobs must be a number between 1 and 8.")
            return
        self.runner = CompressionRunner(selected, self._profile(), self._mode(), jobs)
        self.running = True
        self.query_one("#activity-message", Static).update("")
        self.query_one("#activity-message", Static).display = False
        self.query_one("#compress-selected", Button).disabled = True
        self.query_one("#scan", Button).disabled = True
        self.query_one("#select-all", Button).disabled = True
        self.query_one("#select-none", Button).disabled = True
        self.query_one("#pause", Button).display = True
        self.query_one("#cancel", Button).display = True
        self.query_one("#pause", Button).disabled = False
        self.query_one("#cancel", Button).disabled = False
        self.query_one("#scan-state", Static).update("running")
        self.job_states = {
            item.path: (item, "queued", None, None)
            for item in selected
        }
        self._render_activity()
        self._set_summary(f"Compressing {len(selected)} selected files…")
        self.run_worker(self._run_jobs, name="compression", thread=True, exclusive=True)

    def _run_jobs(self):
        assert self.runner is not None
        return self.runner.run(lambda update: self.call_from_thread(self._handle_update, update))

    def _handle_update(self, update: JobUpdate) -> None:
        if update.result is None:
            self.job_states[update.item.path] = (
                update.item,
                "working",
                update.progress,
                update.encoder,
            )
            self._render_activity()
            return
        self.job_states.pop(update.item.path, None)
        if self.job_states:
            self._render_activity()
        item = next((candidate for candidate in self.items if candidate.path == update.item.path), None)
        if item is not None:
            item.status = update.result.status
            item.error = update.result.message if update.result.status == "failed" else None
            self._refresh_table()
        self.query_one("#progress", Static).update(
            f"{_status_label(update.result.status)} {update.item.filename}"
        )
        if update.result.status == "compressed" and update.result.output_size is not None:
            self.query_one("#activity-message", Static).display = True
            encoder_name = update.result.message.split(";", 1)[0]
            self.query_one("#activity-message", Static).update(
                f"{update.item.filename} · {encoder_name}\n"
                f"{_format_size_change(update.result.source_size, update.result.output_size)}"
            )
        else:
            self.query_one("#activity-message", Static).display = True
            self.query_one("#activity-message", Static).update(
                f"{_status_label(update.result.status)} · {update.item.filename}\n"
                f"{update.result.message}"
            )

    def _render_activity(self) -> None:
        """Show all active jobs and the next queued files in a stable list."""
        entries = list(self.job_states.values())
        active = [entry for entry in entries if entry[1] == "working"]
        queued = [entry for entry in entries if entry[1] == "queued"]
        lines = [f"{len(active)} active  ·  {len(queued)} queued"]
        visible_entries = active + queued
        card_width = max(20, self.query_one("#activity-card", Vertical).content_size.width)
        compact = card_width < 34
        for item, state, progress, encoder in visible_entries[:2]:
            filename = item.filename
            if state == "queued":
                detail = "queued"
                name_width = max(5, card_width - len(detail) - 2)
                if len(filename) > name_width:
                    filename = filename[: max(1, name_width - 1)] + "…"
                lines.append(f"{filename:<{name_width}} {detail}")
            else:
                encoder_label = _activity_encoder_label(encoder, compact=compact)
                if compact:
                    progress_label = f"{progress:.0%}" if progress is not None else "0%"
                    name_width = max(4, card_width - len(progress_label) - len(encoder_label) - 2)
                    detail = f"{progress_label} {encoder_label}"
                else:
                    detail = f"{_progress_bar(progress, width=4)} {encoder_label}"
                    name_width = max(6, card_width - len(detail) - 2)
                if len(filename) > name_width:
                    filename = filename[: max(1, name_width - 1)] + "…"
                lines.append(f"{filename:<{name_width}} {detail}")
        remaining = len(visible_entries) - 2
        if remaining > 0:
            lines.append(f"… and {remaining} more")
        self.query_one("#progress", Static).update("\n".join(lines))

    def action_toggle_pause(self) -> None:
        if not self.runner:
            return
        if self.runner.control.paused.is_set():
            self.runner.resume()
            self.query_one("#pause", Button).label = "Pause"
            self.query_one("#scan-state", Static).update("running")
        else:
            self.runner.pause()
            self.query_one("#pause", Button).label = "Resume"
            self.query_one("#scan-state", Static).update("paused")

    def action_cancel(self) -> None:
        if self.runner:
            self.runner.cancel()
            self.query_one("#progress", Static).update("STOP · cancelling active jobs safely…")


def run_app(**kwargs: object) -> None:
    SqzitApp(**kwargs).run()
