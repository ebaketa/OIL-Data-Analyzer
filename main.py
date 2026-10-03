"""Simple CSV viewer with a tabular display."""

from __future__ import annotations

import csv
import math
import re
import statistics
import tkinter as tk
from bisect import bisect_left
from datetime import datetime, tzinfo
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "OIL Data Analyzer"
ROWS_PER_PAGE = 500
SUPPORTED_ENCODINGS = ("utf-8-sig", "utf-8", "cp1250", "latin-1")
CSV_DELIMITERS = ";,\t|"
DEFAULT_DELIMITER = ";"
PNG_RESOLUTIONS = {
    "SD (720 × 480)": (720, 480),
    "HD (1280 × 720)": (1280, 720),
    "Full HD (1920 × 1080)": (1920, 1080),
    "2K (2560 × 1440)": (2560, 1440),
    "4K (3840 × 2160)": (3840, 2160),
    "8K (7680 × 4320)": (7680, 4320),
}


def parse_number(value: str) -> float:
    """Convert a decimal comma/dot to a finite float or raise ValueError."""
    text = value.strip()
    if not text:
        raise ValueError("Empty numeric value")
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    number = float(text)
    if not math.isfinite(number):
        raise ValueError("Value must be a finite number")
    return number


def parse_iso_time(value: str) -> datetime:
    """Parse ISO time, including Z and long fractional seconds."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        fraction = re.search(r"\.(\d+)", text)
        if fraction:
            normalized = fraction.group(1)[:6].ljust(6, "0")
            text = text[: fraction.start()] + "." + normalized + text[fraction.end() :]
            return datetime.fromisoformat(text)
        raise


_USER_DATETIME_RE = re.compile(
    r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\.?"
    r"(?:\s+(\d{1,2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,6}))?)?)?\s*$"
)


def parse_user_datetime(
    date_value: str,
    time_value: str,
    *,
    end: bool = False,
    timezone: tzinfo | None = None,
) -> datetime:
    """Parse user-entered date/time; the end boundary includes the entire unit."""
    combined = f"{date_value.strip()} {time_value.strip()}".strip()
    match = _USER_DATETIME_RE.match(combined)
    if not match:
        raise ValueError("Invalid date or time format")

    day, month, year, hour, minute, second, fraction = match.groups()
    if hour is None:
        h, m, s, microsecond = (23, 59, 59, 999999) if end else (0, 0, 0, 0)
    elif second is None:
        h, m = int(hour), int(minute)
        s, microsecond = (59, 999999) if end else (0, 0)
    else:
        h, m, s = int(hour), int(minute), int(second)
        if fraction is not None:
            microsecond = int(fraction.ljust(6, "0"))
        else:
            microsecond = 999999 if end else 0

    parsed = datetime(int(year), int(month), int(day), h, m, s, microsecond)
    return parsed.replace(tzinfo=timezone) if timezone is not None else parsed


def detect_delimiter(text: str) -> str:
    """Detect the delimiter while checking CSV row-width consistency."""
    sample = text[:65536]
    lines = sample.splitlines()
    if len(sample) == 65536 and len(lines) > 1:
        lines = lines[:-1]
    lines = [line for line in lines if line.strip()][:50]
    if not lines:
        return DEFAULT_DELIMITER

    def score(delimiter: str) -> int:
        try:
            widths = [len(row) for row in csv.reader(lines, delimiter=delimiter)]
        except csv.Error:
            return 0
        return widths[0] if len(set(widths)) == 1 and widths[0] > 1 else 0

    candidates: list[str] = []
    try:
        candidates.append(
            csv.Sniffer().sniff("\n".join(lines), delimiters=CSV_DELIMITERS).delimiter
        )
    except csv.Error:
        pass
    candidates.extend((DEFAULT_DELIMITER, ",", "\t", "|"))
    return next((delimiter for delimiter in candidates if score(delimiter)), DEFAULT_DELIMITER)


def find_measurement_columns(
    headers: list[str], rows: list[list[str]]
) -> list[tuple[int, str]]:
    """Find numeric measurement columns, excluding ID and time metadata."""
    ignored_headers = {"id", "time", "acquisition time (s)"}
    columns: list[tuple[int, str]] = []
    sample_rows = rows[:500]

    for index, header in enumerate(headers):
        if header.strip().casefold() in ignored_headers:
            continue
        numeric_values = 0
        nonempty_values = 0
        for row in sample_rows:
            if index >= len(row) or not row[index].strip():
                continue
            nonempty_values += 1
            try:
                parse_number(row[index])
                numeric_values += 1
            except ValueError:
                pass
        if numeric_values and numeric_values >= 0.8 * nonempty_values:
            columns.append((index, header))
    return columns


def unit_from_header(header: str) -> str:
    match = re.search(r"\(([^()]*)\)\s*$", header)
    return match.group(1) if match else ""


def short_series_name(header: str) -> str:
    """Return a generic shorter series name, independent of device and channel count."""
    channel = re.search(r"(?:^|\s)CH\d+\s+", header, flags=re.IGNORECASE)
    name = header[channel.end() :] if channel else header
    if unit_from_header(name):
        name = re.sub(r"\s*\([^()]*\)\s*$", "", name)
    return name.strip() or header.strip()


def average_on_common_times(
    series_points: list[tuple[list[datetime], list[float]]],
) -> tuple[list[datetime], list[float]]:
    """Calculate the average only for timestamps present in all series."""
    if not series_points:
        return [], []
    values_by_series = [
        dict(zip(times, values)) for times, values in series_points
    ]
    common_times = set(values_by_series[0])
    for values_by_time in values_by_series[1:]:
        common_times.intersection_update(values_by_time)
    times = sorted(common_times)
    averages = [
        sum(values_by_time[time] for values_by_time in values_by_series)
        / len(values_by_series)
        for time in times
    ]
    return times, averages


def read_csv(path: str | Path) -> tuple[list[str], list[list[str]], str]:
    """Load a CSV and return headers, rows, and the detected delimiter."""
    file_path = Path(path)
    last_error: UnicodeDecodeError | None = None

    for encoding in SUPPORTED_ENCODINGS:
        try:
            with file_path.open("r", encoding=encoding, newline="") as csv_file:
                sample = csv_file.read(65536)
                csv_file.seek(0)
                delimiter = detect_delimiter(sample)

                reader = csv.reader(csv_file, delimiter=delimiter)
                headers = next(reader, [])
                rows = [row for row in reader if row]
                return headers, rows, delimiter
        except UnicodeDecodeError as error:
            last_error = error

    if last_error is not None:
        raise last_error
    return [], [], DEFAULT_DELIMITER


class Series:
    """A numeric measurement series aligned with CSV rows."""

    def __init__(self, name: str, values: list[float | None]) -> None:
        self.name = name
        self.unit = unit_from_header(name)
        self.values = values


class Dataset:
    """CSV data whose time and numeric columns are parsed only once."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.headers, self.rows, self.delimiter = read_csv(self.path)
        self.headers = self._unique_headers(self.headers)
        self.time_index = next(
            (
                index
                for index, header in enumerate(self.headers)
                if header.strip().casefold() == "time"
            ),
            None,
        )
        self.times: list[datetime | None] = [None] * len(self.rows)
        self.timezone: tzinfo | None = None
        self.skipped_times = 0
        self.series: list[Series] = []
        self.series_by_name: dict[str, Series] = {}
        self._points_cache: dict[
            tuple[str, datetime | None, datetime | None, str],
            tuple[list[datetime], list[float]],
        ] = {}
        self._parse_times()
        self._parse_series()

    @staticmethod
    def _unique_headers(headers: list[str]) -> list[str]:
        """Clean headers and make duplicate names unique."""
        seen: dict[str, int] = {}
        unique: list[str] = []
        for raw_header in headers:
            header = raw_header.strip()
            seen[header] = seen.get(header, 0) + 1
            unique.append(
                header if seen[header] == 1 else f"{header} #{seen[header]}"
            )
        return unique

    @property
    def has_time(self) -> bool:
        return self.time_index is not None and any(
            time is not None for time in self.times
        )

    def _parse_times(self) -> None:
        if self.time_index is None:
            for column_index in range(len(self.headers)):
                candidates = [
                    row[column_index]
                    for row in self.rows
                    if column_index < len(row) and row[column_index].strip()
                ][:20]
                if not candidates:
                    continue
                try:
                    for candidate in candidates:
                        parse_iso_time(candidate)
                except (TypeError, ValueError):
                    continue
                self.time_index = column_index
                break
        if self.time_index is None:
            return
        parsed_times: list[datetime | None] = []
        for row in self.rows:
            if self.time_index >= len(row):
                parsed_times.append(None)
                continue
            try:
                parsed_times.append(parse_iso_time(row[self.time_index]))
            except (TypeError, ValueError):
                parsed_times.append(None)

        first_time = next((time for time in parsed_times if time is not None), None)
        if first_time is None:
            self.time_index = None
            return
        self.timezone = first_time.tzinfo
        first_is_aware = first_time.tzinfo is not None
        self.times = [
            time
            if time is not None and (time.tzinfo is not None) == first_is_aware
            else None
            for time in parsed_times
        ]
        self.skipped_times = sum(time is None for time in self.times)

    def _parse_series(self) -> None:
        ignored_headers = {"id", "time", "acquisition time (s)"}
        for column_index, header in enumerate(self.headers):
            if column_index == self.time_index:
                continue
            if header.strip().casefold() in ignored_headers:
                continue
            values: list[float | None] = []
            nonempty_count = 0
            valid_count = 0
            for row in self.rows:
                if column_index >= len(row) or not row[column_index].strip():
                    values.append(None)
                    continue
                nonempty_count += 1
                try:
                    values.append(parse_number(row[column_index]))
                    valid_count += 1
                except (TypeError, ValueError):
                    values.append(None)
            if not valid_count or valid_count < 0.8 * nonempty_count:
                continue
            series = Series(header, values)
            self.series.append(series)
            self.series_by_name[header] = series

    def time_range(self) -> tuple[datetime, datetime] | None:
        valid_times = [time for time in self.times if time is not None]
        return (min(valid_times), max(valid_times)) if valid_times else None

    def points(
        self,
        series_name: str,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
        mode: str = "all",
    ) -> tuple[list[datetime], list[float]]:
        """Return filtered samples or minute/hour averages, using a cache."""
        cache_key = (series_name, time_from, time_to, mode)
        if cache_key in self._points_cache:
            return self._points_cache[cache_key]

        series = self.series_by_name[series_name]
        raw_times: list[datetime] = []
        raw_values: list[float] = []
        for time, value in zip(self.times, series.values):
            if time is None or value is None:
                continue
            if time_from is not None and time < time_from:
                continue
            if time_to is not None and time > time_to:
                continue
            raw_times.append(time)
            raw_values.append(value)

        if any(
            raw_times[index] > raw_times[index + 1]
            for index in range(len(raw_times) - 1)
        ):
            ordered = sorted(zip(raw_times, raw_values), key=lambda item: item[0])
            raw_times = [item[0] for item in ordered]
            raw_values = [item[1] for item in ordered]

        if mode == "all":
            result = (raw_times, raw_values)
        elif mode in {"minute", "hour"}:
            buckets: dict[datetime, list[float]] = {}
            for time, value in zip(raw_times, raw_values):
                bucket = time.replace(second=0, microsecond=0)
                if mode == "hour":
                    bucket = bucket.replace(minute=0)
                total, count = buckets.get(bucket, [0.0, 0])
                buckets[bucket] = [total + value, count + 1]
            result_times = sorted(buckets)
            result = (
                result_times,
                [buckets[time][0] / buckets[time][1] for time in result_times],
            )
        else:
            raise ValueError(f"Unknown aggregation mode: {mode}")

        if len(self._points_cache) >= 128:
            self._points_cache.clear()
        self._points_cache[cache_key] = result
        return result

    def series_data(
        self, selected_series: list[Series] | None = None
    ) -> dict[str, tuple[list[datetime], list[float]]]:
        source = self.series if selected_series is None else selected_series
        result: dict[str, tuple[list[datetime], list[float]]] = {}
        for series in source:
            times, values = self.points(series.name)
            if values:
                result[series.name] = (times, values)
        return result


def save_figure_png(
    figure: object,
    canvas: object,
    path: str,
    width: int,
    height: int,
) -> None:
    """Save a Matplotlib figure at the exact requested resolution and restore the GUI size."""
    original_size = figure.get_size_inches().copy()
    try:
        figure.set_size_inches(width / 100, height / 100, forward=False)
        figure.savefig(path, format="png", dpi=100, facecolor="white")
    finally:
        figure.set_size_inches(original_size, forward=False)
        canvas.draw_idle()


class ExportControls(ttk.LabelFrame):
    """Shared PNG resolution selection and saving for all charts."""

    def __init__(
        self,
        master: tk.Misc,
        figure: object,
        canvas: object,
        source_file: Path | None,
        file_suffix: str,
        dialog_title: str,
    ) -> None:
        super().__init__(master, text="Chart export", padding=(10, 5, 10, 7))
        self.figure = figure
        self.canvas = canvas
        self.source_file = source_file
        self.file_suffix = file_suffix
        self.dialog_title = dialog_title
        self.resolution = tk.StringVar(value="Full HD (1920 × 1080)")
        ttk.Label(self, text="Resolution:").pack(side=tk.LEFT)
        ttk.Combobox(
            self,
            textvariable=self.resolution,
            values=tuple(PNG_RESOLUTIONS),
            state="readonly",
            width=24,
        ).pack(side=tk.LEFT, padx=(5, 12))
        ttk.Button(self, text="Save PNG…", command=self.save).pack(side=tk.LEFT)

    def save(self) -> None:
        default_name = (
            f"{self.source_file.stem}_{self.file_suffix}.png"
            if self.source_file
            else f"{self.file_suffix}.png"
        )
        selected_path = filedialog.asksaveasfilename(
            parent=self.winfo_toplevel(),
            title=self.dialog_title,
            defaultextension=".png",
            initialfile=default_name,
            filetypes=(("PNG image", "*.png"),),
        )
        if not selected_path:
            return
        width, height = PNG_RESOLUTIONS[self.resolution.get()]
        try:
            save_figure_png(
                self.figure, self.canvas, selected_path, width, height
            )
        except (OSError, ValueError, MemoryError) as error:
            messagebox.showerror(
                "Save error", str(error), parent=self.winfo_toplevel()
            )
            return
        messagebox.showinfo(
            "Chart saved",
            f"PNG was saved at a resolution of {width} × {height}.",
            parent=self.winfo_toplevel(),
        )


class HoverTooltip:
    """Shared appearance and handling of hover annotations on a Matplotlib axis."""

    def __init__(self, axes: object, canvas: object, fontsize: int = 12) -> None:
        self.canvas = canvas
        self.annotation = axes.annotate(
            "",
            xy=(0, 0),
            xytext=(14, 14),
            textcoords="offset points",
            fontsize=fontsize,
            bbox={
                "boxstyle": "round,pad=0.4",
                "facecolor": "white",
                "edgecolor": "#555555",
                "alpha": 0.95,
            },
            arrowprops={"arrowstyle": "->", "color": "#555555"},
            zorder=10,
        )
        self.annotation.set_visible(False)

    def show(self, position: tuple[float, float], text: str) -> None:
        self.annotation.xy = position
        self.annotation.set_text(text)
        self.annotation.set_visible(True)
        self.canvas.draw_idle()

    def hide(self) -> None:
        if self.annotation.get_visible():
            self.annotation.set_visible(False)
            self.canvas.draw_idle()


class TimeRangeControls(ttk.LabelFrame):
    """Shared widget for entering and resetting the time range."""

    def __init__(
        self,
        master: tk.Misc,
        dataset: Dataset,
        command: object,
    ) -> None:
        super().__init__(master, text="Time range", padding=(10, 5, 10, 7))
        time_range = dataset.time_range()
        if time_range is None:
            raise ValueError("Dataset has no valid time")
        self.first_time, self.last_time = time_range
        self.command = command
        self.date_from = tk.StringVar()
        self.time_from = tk.StringVar()
        self.date_to = tk.StringVar()
        self.time_to = tk.StringVar()
        fields = (
            ("Date from:", self.date_from, 12),
            ("Time from:", self.time_from, 10),
            ("Date to:", self.date_to, 12),
            ("Time to:", self.time_to, 10),
        )
        for label, variable, width in fields:
            ttk.Label(self, text=label).pack(side=tk.LEFT)
            entry = ttk.Entry(self, textvariable=variable, width=width)
            entry.pack(side=tk.LEFT, padx=(5, 12))
            entry.bind("<Return>", lambda _event: self.command())
        ttk.Button(self, text="Apply time range", command=self.command).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(self, text="Reset time range", command=self.reset).pack(side=tk.LEFT)
        self.reset(run_command=False)

    def reset(self, run_command: bool = True) -> None:
        self.date_from.set(self.first_time.strftime("%d.%m.%Y."))
        self.time_from.set(self.first_time.strftime("%H:%M:%S"))
        self.date_to.set(self.last_time.strftime("%d.%m.%Y."))
        self.time_to.set(self.last_time.strftime("%H:%M:%S"))
        if run_command:
            self.command()

    def bounds(self) -> tuple[datetime, datetime]:
        time_from = parse_user_datetime(
            self.date_from.get(),
            self.time_from.get(),
            timezone=self.first_time.tzinfo,
        )
        time_to = parse_user_datetime(
            self.date_to.get(),
            self.time_to.get(),
            end=True,
            timezone=self.first_time.tzinfo,
        )
        if time_from >= time_to:
            raise ValueError("Start time must be earlier than end time.")
        return time_from, time_to


class SeriesChecklist(ttk.LabelFrame):
    """Shared checkbox group for selecting measurement series."""

    def __init__(
        self,
        master: tk.Misc,
        title: str,
        variables: dict[str, tk.BooleanVar],
        command: object,
    ) -> None:
        super().__init__(master, text=title, padding=(10, 5, 10, 7))
        self.variables = variables
        for header, variable in variables.items():
            label = short_series_name(header)
            ttk.Checkbutton(
                self, text=label, variable=variable, command=command
            ).pack(side=tk.LEFT, padx=(0, 14))

    def selected(self) -> list[str]:
        return [name for name, variable in self.variables.items() if variable.get()]


class CsvViewer(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1920x1080")
        self.minsize(720, 480)

        self.headers: list[str] = []
        self.rows: list[list[str]] = []
        self.current_page = 0
        self.current_file: Path | None = None
        self.dataset: Dataset | None = None

        self.status_text = tk.StringVar(value="Select a CSV file to display.")
        self.page_text = tk.StringVar(value="Page 0 / 0")
        self._build_ui()

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        style.configure("Csv.Treeview", font=("Segoe UI", 10), rowheight=30)
        style.configure("Csv.Treeview.Heading", font=("Segoe UI", 10, "bold"))

        toolbar = ttk.Frame(self, padding=(10, 10, 10, 6))
        toolbar.pack(fill=tk.X)

        button_panel = ttk.Frame(toolbar)
        button_panel.pack(side=tk.LEFT)
        ttk.Button(button_panel, text="Load CSV…", command=self.choose_file).pack(
            fill=tk.X
        )
        graph_button_panel = ttk.Frame(button_panel)
        graph_button_panel.pack(fill=tk.X, pady=(6, 0))
        self.chart_button = ttk.Button(
            graph_button_panel,
            text="Line chart",
            command=self.show_line_chart,
            state=tk.DISABLED,
        )
        self.chart_button.pack(side=tk.LEFT)
        self.delta_chart_button = ttk.Button(
            graph_button_panel,
            text="Delta (Δ) chart",
            command=self.show_delta_chart,
            state=tk.DISABLED,
        )
        self.delta_chart_button.pack(side=tk.LEFT, padx=(6, 0))
        self.histogram_button = ttk.Button(
            graph_button_panel,
            text="Histogram",
            command=self.show_histogram,
            state=tk.DISABLED,
        )
        self.histogram_button.pack(side=tk.LEFT, padx=(6, 0))
        self.scatter_button = ttk.Button(
            graph_button_panel,
            text="Scatter",
            command=self.show_scatter,
            state=tk.DISABLED,
        )
        self.scatter_button.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(toolbar, textvariable=self.status_text).pack(
            side=tk.LEFT, padx=14, anchor=tk.N
        )

        table_frame = ttk.Frame(self, padding=(10, 0, 10, 6))
        table_frame.pack(fill=tk.BOTH, expand=True)

        self.table = ttk.Treeview(
            table_frame,
            show="headings",
            style="Csv.Treeview",
        )
        vertical_scroll = ttk.Scrollbar(
            table_frame, orient=tk.VERTICAL, command=self.table.yview
        )
        horizontal_scroll = ttk.Scrollbar(
            table_frame, orient=tk.HORIZONTAL, command=self.table.xview
        )
        self.table.configure(
            yscrollcommand=vertical_scroll.set,
            xscrollcommand=horizontal_scroll.set,
        )

        self.table.grid(row=0, column=0, sticky="nsew")
        vertical_scroll.grid(row=0, column=1, sticky="ns")
        horizontal_scroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        footer = ttk.Frame(self, padding=(10, 4, 10, 10))
        footer.pack(fill=tk.X)
        self.previous_button = ttk.Button(
            footer, text="← Previous", command=self.previous_page, state=tk.DISABLED
        )
        self.previous_button.pack(side=tk.LEFT)
        ttk.Label(footer, textvariable=self.page_text).pack(side=tk.LEFT, padx=12)
        self.next_button = ttk.Button(
            footer, text="Next →", command=self.next_page, state=tk.DISABLED
        )
        self.next_button.pack(side=tk.LEFT)

    def choose_file(self) -> None:
        initial_directory = Path(__file__).resolve().parent / "data"
        selected = filedialog.askopenfilename(
            title="Select a CSV file",
            initialdir=initial_directory if initial_directory.exists() else Path.cwd(),
            filetypes=(("CSV files", "*.csv"), ("All files", "*.*")),
        )
        if selected:
            self.load_file(Path(selected))

    def load_file(self, path: Path) -> None:
        try:
            dataset = Dataset(path)
        except (OSError, csv.Error, UnicodeError) as error:
            messagebox.showerror("Load error", str(error))
            return

        if not dataset.headers:
            messagebox.showwarning("Empty file", "CSV file has no header.")
            return

        self.current_file = path
        self.dataset = dataset
        self.headers = dataset.headers
        self.rows = dataset.rows
        self.current_page = 0
        self._configure_columns()
        self._show_page()
        self.status_text.set(
            f"{path.name}  •  {len(dataset.rows):,} rows  "
            f"•  {len(dataset.headers)} columns  •  delimiter: {dataset.delimiter!r}"
        )
        self.chart_button.configure(state=tk.NORMAL)
        self.delta_chart_button.configure(state=tk.NORMAL)
        self.histogram_button.configure(state=tk.NORMAL)
        self.scatter_button.configure(state=tk.NORMAL)
        self.title(f"{APP_TITLE} — {path.name}")

    def show_line_chart(self) -> None:
        dataset = self.dataset
        if dataset is None or not dataset.has_time or not dataset.series:
            messagebox.showwarning(
                "Missing data",
                "CSV must contain a 'Time' column and at least one numeric measurement column.",
            )
            return

        detected_units = {series.unit for series in dataset.series} - {""}
        common_unit = detected_units.pop() if len(detected_units) == 1 else ""
        y_axis_label = f"Value ({common_unit})" if common_unit else "Value"

        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.dates import DateFormatter, date2num
            from matplotlib.figure import Figure
        except ImportError:
            messagebox.showerror(
                "Matplotlib is not installed",
                "Run: python -m pip install -r requirements.txt",
            )
            return

        chart_window = tk.Toplevel(self)
        chart_window.title(
            f"Line chart — {self.current_file.name if self.current_file else ''}"
        )
        chart_window.geometry("1920x1080")
        chart_window.minsize(720, 480)

        figure = Figure(figsize=(12, 7), dpi=100, layout="constrained")
        axes = figure.add_subplot(111)

        series_data = dataset.series_data()
        all_values = [
            value for _times, values in series_data.values() for value in values
        ]

        if not series_data:
            chart_window.destroy()
            messagebox.showwarning(
                "No data", "No valid values were found for plotting."
            )
            return

        canvas = FigureCanvasTkAgg(figure, master=chart_window)
        hover_series: list[
            tuple[str, str, list[float], list[float], list[datetime]]
        ] = []
        hover_annotation = None

        scale_controls = ttk.Frame(chart_window, padding=(10, 10, 10, 4))
        scale_controls.pack(side=tk.TOP, fill=tk.X)
        minimum_value = tk.StringVar(value=f"{min(all_values):.9g}")
        maximum_value = tk.StringVar(value=f"{max(all_values):.9g}")
        all_times = [time for times, _values in series_data.values() for time in times]
        first_time = min(all_times)
        last_time = max(all_times)
        date_from_value = tk.StringVar(value=first_time.strftime("%d.%m.%Y."))
        time_from_value = tk.StringVar(value=first_time.strftime("%H:%M:%S"))
        date_to_value = tk.StringVar(value=last_time.strftime("%d.%m.%Y."))
        time_to_value = tk.StringVar(value=last_time.strftime("%H:%M:%S"))
        current_view: dict[str, str | bool | None] = {
            "period": None,
            "title_suffix": "all samples",
            "average_selected_series": False,
        }
        series_visibility = {
            header: tk.BooleanVar(value=True) for header in series_data
        }

        ttk.Label(scale_controls, text="Min. value:").pack(side=tk.LEFT)
        minimum_entry = ttk.Entry(
            scale_controls, textvariable=minimum_value, width=10
        )
        minimum_entry.pack(side=tk.LEFT, padx=(5, 14))
        ttk.Label(scale_controls, text="Max. value:").pack(side=tk.LEFT)
        maximum_entry = ttk.Entry(
            scale_controls, textvariable=maximum_value, width=10
        )
        maximum_entry.pack(side=tk.LEFT, padx=(5, 14))

        def apply_scale(_event: tk.Event | None = None) -> None:
            try:
                lower_limit = float(minimum_value.get().strip().replace(",", "."))
                upper_limit = float(maximum_value.get().strip().replace(",", "."))
            except ValueError:
                messagebox.showerror(
                    "Invalid value",
                    "Minimum and maximum values must be numbers.",
                    parent=chart_window,
                )
                return

            if lower_limit >= upper_limit:
                messagebox.showerror(
                    "Invalid range",
                    "Minimum value must be less than maximum value.",
                    parent=chart_window,
                )
                return

            axes.set_ylim(lower_limit, upper_limit)
            canvas.draw_idle()

        ttk.Button(
            scale_controls, text="Apply", command=apply_scale
        ).pack(side=tk.LEFT, padx=(0, 18))
        minimum_entry.bind("<Return>", apply_scale)
        maximum_entry.bind("<Return>", apply_scale)

        def show_samples(
            period: str | None,
            title_suffix: str,
            average_selected_series: bool = False,
        ) -> None:
            nonlocal hover_annotation
            try:
                time_from, time_to = time_filter.bounds()
            except ValueError:
                messagebox.showerror(
                    "Invalid date or time",
                    "Enter the date as 27.09.2026. and the time as 00:00.",
                    parent=chart_window,
                )
                return

            if time_from >= time_to:
                messagebox.showerror(
                    "Invalid range",
                    "Start time must be earlier than end time.",
                    parent=chart_window,
                )
                return

            selected_headers = [
                header for header in series_data if series_visibility[header].get()
            ]
            if not selected_headers:
                messagebox.showwarning(
                    "No measurement series selected",
                    "Select at least one measurement series to display.",
                    parent=chart_window,
                )
                return

            selected_points = {
                header: dataset.points(
                    header, time_from, time_to, period or "all"
                )
                for header in selected_headers
            }
            if not any(values for _times, values in selected_points.values()):
                messagebox.showwarning(
                    "No data",
                    "There are no samples in the selected time range.",
                    parent=chart_window,
                )
                return

            selected_average_unit = ""
            if average_selected_series:
                selected_units = {
                    dataset.series_by_name[header].unit
                    for header in selected_headers
                }
                if len(selected_units) != 1:
                    readable_units = ", ".join(
                        sorted(unit or "no unit" for unit in selected_units)
                    )
                    messagebox.showwarning(
                        "Different measurement units",
                        "The average of selected series can only be calculated for series with the same "
                        f"measurement unit. Selected units: {readable_units}.",
                        parent=chart_window,
                    )
                    return
                selected_average_unit = next(iter(selected_units))

            current_view.update(
                period=period,
                title_suffix=title_suffix,
                average_selected_series=average_selected_series,
            )
            axes.clear()
            hover_series.clear()
            displayed_sample_count = 0

            if average_selected_series:
                display_times, display_values = average_on_common_times(
                    list(selected_points.values())
                )
                if not display_times:
                    messagebox.showwarning(
                        "No common samples",
                        "The selected series have no common timestamps in the specified "
                        "time range.",
                        parent=chart_window,
                    )
                    return
                displayed_sample_count = len(display_times)
                axes.plot(
                    display_times,
                    display_values,
                    linewidth=1.5,
                    label="Average of selected series",
                )
                hover_series.append(
                    (
                        "Average of selected series",
                        selected_average_unit,
                        list(date2num(display_times)),
                        display_values,
                        display_times,
                    )
                )
            else:
                for header, (display_times, display_values) in selected_points.items():
                    if displayed_sample_count == 0:
                        displayed_sample_count = len(display_times)
                    axes.plot(
                        display_times,
                        display_values,
                        linewidth=1,
                        label=short_series_name(header),
                    )
                    hover_series.append(
                        (
                            short_series_name(header),
                            unit_from_header(header),
                            list(date2num(display_times)),
                            display_values,
                            display_times,
                        )
                    )

            formatted_count = f"{displayed_sample_count:,}".replace(",", ".")
            axes.set_title(
                f"Measurements over time - {title_suffix} ({formatted_count})",
                fontsize=24,
            )
            axes.set_xlabel("Time", fontsize=20)
            axes.set_ylabel(y_axis_label, fontsize=20)
            axes.tick_params(axis="both", labelsize=14)
            axes.xaxis.set_major_formatter(
                DateFormatter("%H:%M:%S\n%d.%m.%Y.", tz=first_time.tzinfo)
            )
            axes.margins(x=0)
            axes.set_xlim(time_from, time_to)
            axes.grid(True, alpha=0.3)
            axes.legend(loc="upper left", fontsize=18)
            hover_annotation = HoverTooltip(axes, canvas, fontsize=12).annotation
            apply_scale()
            figure.autofmt_xdate()
            canvas.draw_idle()

        def apply_time_filter(_event: tk.Event | None = None) -> None:
            show_samples(
                current_view["period"],
                str(current_view["title_suffix"]),
                bool(current_view["average_selected_series"]),
            )

        time_filter = TimeRangeControls(chart_window, dataset, apply_time_filter)
        time_filter.pack(
            side=tk.TOP,
            fill=tk.X,
            padx=10,
            pady=(0, 5),
        )
        date_from_value = time_filter.date_from
        time_from_value = time_filter.time_from
        date_to_value = time_filter.date_to
        time_to_value = time_filter.time_to

        view_controls = ttk.LabelFrame(
            chart_window, text="View mode", padding=(10, 5, 10, 7)
        )
        view_controls.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 5))
        ttk.Button(
            view_controls,
            text="All samples",
            command=lambda: show_samples(None, "all samples"),
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            view_controls,
            text="1-minute average",
            command=lambda: show_samples("minute", "1-minute average"),
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            view_controls,
            text="1-hour average",
            command=lambda: show_samples("hour", "1-hour average"),
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            view_controls,
            text="Average of selected series",
            command=lambda: show_samples(
                current_view["period"], "average of selected series", True
            ),
        ).pack(side=tk.LEFT)

        def apply_series_filter() -> None:
            show_samples(
                current_view["period"],
                str(current_view["title_suffix"]),
                bool(current_view["average_selected_series"]),
            )

        SeriesChecklist(
            chart_window,
            "Measurement series",
            series_visibility,
            apply_series_filter,
        ).pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 5))

        ExportControls(
            chart_window,
            figure,
            canvas,
            self.current_file,
            "graf",
            "Save chart as PNG",
        ).pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 5))

        def show_hover_value(event: object) -> None:
            if (
                hover_annotation is None
                or event.inaxes is not axes
                or event.xdata is None
                or event.x is None
                or event.y is None
            ):
                if hover_annotation is not None and hover_annotation.get_visible():
                    hover_annotation.set_visible(False)
                    canvas.draw_idle()
                return

            nearest = None
            nearest_distance = 16.0
            for series_name, unit, numeric_times, values, original_times in hover_series:
                position = bisect_left(numeric_times, event.xdata)
                for index in (position - 1, position):
                    if not 0 <= index < len(numeric_times):
                        continue
                    point_x, point_y = axes.transData.transform(
                        (numeric_times[index], values[index])
                    )
                    distance = ((point_x - event.x) ** 2 + (point_y - event.y) ** 2) ** 0.5
                    if distance < nearest_distance:
                        nearest_distance = distance
                        nearest = (
                            series_name,
                            unit,
                            numeric_times[index],
                            values[index],
                            original_times[index],
                        )

            if nearest is None:
                if hover_annotation.get_visible():
                    hover_annotation.set_visible(False)
                    canvas.draw_idle()
                return

            series_name, unit, numeric_time, measured_value, original_time = nearest
            hover_annotation.xy = (numeric_time, measured_value)
            unit_suffix = f" {unit}" if unit else ""
            hover_annotation.set_text(
                f"{series_name}\n"
                f"{original_time.strftime('%d.%m.%Y. %H:%M:%S')}\n"
                f"{measured_value:.9g}{unit_suffix}"
            )
            hover_annotation.set_visible(True)
            canvas.draw_idle()

        canvas.mpl_connect("motion_notify_event", show_hover_value)

        canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        show_samples(None, "all samples")

    def show_delta_chart(self) -> None:
        """Display differences between measurement series of the same unit relative to a reference series."""
        dataset = self.dataset
        if dataset is None or not dataset.has_time or len(dataset.series) < 2:
            messagebox.showwarning(
                "Missing data",
                "Delta chart requires a 'Time' column and at least two numeric series.",
            )
            return

        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.dates import DateFormatter, date2num
            from matplotlib.figure import Figure
        except ImportError:
            messagebox.showerror(
                "Matplotlib is not installed",
                "Run: python -m pip install -r requirements.txt",
            )
            return

        series_data = dataset.series_data()

        if len(series_data) < 2:
            messagebox.showwarning(
                "Missing data",
                "Delta chart requires at least two valid numeric series.",
            )
            return

        unit_counts: dict[str, int] = {}
        for header in series_data:
            unit = unit_from_header(header)
            unit_counts[unit] = unit_counts.get(unit, 0) + 1
        initial_reference = next(
            (
                header
                for header in series_data
                if unit_counts[unit_from_header(header)] >= 2
            ),
            None,
        )
        if initial_reference is None:
            messagebox.showwarning(
                "No compatible series",
                "Delta chart requires at least two numeric series with the same measurement unit.",
            )
            return
        reference_headers = tuple(
            header
            for header in series_data
            if unit_counts[unit_from_header(header)] >= 2
        )

        chart_window = tk.Toplevel(self)
        chart_window.title(
            f"Delta (Δ) — {self.current_file.name if self.current_file else ''}"
        )
        chart_window.geometry("1200x850")
        chart_window.minsize(800, 600)

        figure = Figure(figsize=(12, 7), dpi=100, layout="constrained")
        axes = figure.add_subplot(111)
        canvas = FigureCanvasTkAgg(figure, master=chart_window)

        all_times = [time for times, _values in series_data.values() for time in times]
        first_time = min(all_times)
        last_time = max(all_times)
        reference_value = tk.StringVar(value=initial_reference)
        show_reference_value = tk.BooleanVar(value=True)
        initial_unit = unit_from_header(reference_value.get())
        visibility = {
            header: tk.BooleanVar(value=unit_from_header(header) == initial_unit)
            for header in series_data
        }
        reference_zero_text = tk.StringVar(
            value=f"Show reference series as Δ = 0"
            f"{' ' + initial_unit if initial_unit else ''}"
        )
        current_delta_unit = {"unit": initial_unit}
        date_from_value = tk.StringVar(value=first_time.strftime("%d.%m.%Y."))
        time_from_value = tk.StringVar(value=first_time.strftime("%H:%M:%S"))
        date_to_value = tk.StringVar(value=last_time.strftime("%d.%m.%Y."))
        time_to_value = tk.StringVar(value=last_time.strftime("%H:%M:%S"))
        current_view: dict[str, str | None] = {
            "period": None,
            "title": "all samples",
        }

        def raw_delta_values(reference_header: str) -> list[float]:
            reference_times, reference_values = series_data[reference_header]
            reference_lookup = dict(zip(reference_times, reference_values))
            deltas = [0.0]
            for header, (times, values) in series_data.items():
                if header == reference_header:
                    continue
                if unit_from_header(header) != unit_from_header(reference_header):
                    continue
                deltas.extend(
                    value - reference_lookup[time]
                    for time, value in zip(times, values)
                    if time in reference_lookup
                )
            return deltas

        initial_deltas = raw_delta_values(reference_value.get())
        delta_min = min(initial_deltas)
        delta_max = max(initial_deltas)
        if delta_min == delta_max:
            delta_min -= 1.0
            delta_max += 1.0
        minimum_value = tk.StringVar(value=f"{delta_min:.6g}")
        maximum_value = tk.StringVar(value=f"{delta_max:.6g}")
        hover_series: list[
            tuple[str, list[float], list[float], list[datetime]]
        ] = []
        hover_annotation = None

        reference_controls = ttk.LabelFrame(
            chart_window, text="Reference series", padding=(10, 5, 10, 7)
        )
        reference_controls.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(8, 5))
        ttk.Label(reference_controls, text="Reference series:").pack(side=tk.LEFT)
        reference_picker = ttk.Combobox(
            reference_controls,
            textvariable=reference_value,
            values=reference_headers,
            state="readonly",
            width=48,
        )
        reference_picker.pack(side=tk.LEFT, padx=(5, 16))
        ttk.Checkbutton(
            reference_controls,
            textvariable=reference_zero_text,
            variable=show_reference_value,
        ).pack(side=tk.LEFT)

        scale_controls = ttk.LabelFrame(
            chart_window, text="Y scale", padding=(10, 5, 10, 7)
        )
        scale_controls.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 5))
        ttk.Label(scale_controls, text="Min. Δ:").pack(side=tk.LEFT)
        minimum_entry = ttk.Entry(scale_controls, textvariable=minimum_value, width=10)
        minimum_entry.pack(side=tk.LEFT, padx=(5, 14))
        ttk.Label(scale_controls, text="Max. Δ:").pack(side=tk.LEFT)
        maximum_entry = ttk.Entry(scale_controls, textvariable=maximum_value, width=10)
        maximum_entry.pack(side=tk.LEFT, padx=(5, 14))

        def apply_scale(_event: tk.Event | None = None) -> None:
            try:
                lower = parse_number(minimum_value.get())
                upper = parse_number(maximum_value.get())
            except ValueError:
                messagebox.showerror(
                    "Invalid value",
                    "Minimum and maximum Delta values must be numbers.",
                    parent=chart_window,
                )
                return
            if lower >= upper:
                messagebox.showerror(
                    "Invalid range",
                    "Minimum Delta value must be less than maximum Delta value.",
                    parent=chart_window,
                )
                return
            axes.set_ylim(lower, upper)
            canvas.draw_idle()

        ttk.Button(scale_controls, text="Apply", command=apply_scale).pack(
            side=tk.LEFT
        )
        minimum_entry.bind("<Return>", apply_scale)
        maximum_entry.bind("<Return>", apply_scale)

        def render_delta(period: str | None, title_suffix: str) -> None:
            nonlocal hover_annotation
            try:
                time_from, time_to = time_filter.bounds()
            except ValueError:
                messagebox.showerror(
                    "Invalid date or time",
                    "Enter the date as 27.09.2026. and the time as 00:00.",
                    parent=chart_window,
                )
                return
            if time_from >= time_to:
                messagebox.showerror(
                    "Invalid range",
                    "Start time must be earlier than end time.",
                    parent=chart_window,
                )
                return

            reference_header = reference_value.get()
            reference_unit = unit_from_header(reference_header)
            current_delta_unit["unit"] = reference_unit
            reference_times, reference_values = dataset.points(
                reference_header, time_from, time_to, period or "all"
            )
            reference_lookup = dict(zip(reference_times, reference_values))
            if not reference_lookup:
                messagebox.showwarning(
                    "No data",
                    "The reference series has no samples in the selected range.",
                    parent=chart_window,
                )
                return

            selected_headers = [
                header for header, selected in visibility.items() if selected.get()
            ]
            incompatible_headers = [
                header
                for header in selected_headers
                if unit_from_header(header) != reference_unit
            ]
            if incompatible_headers:
                messagebox.showwarning(
                    "Different measurement units",
                    "Delta can only be calculated between series with the same measurement unit "
                    "as the reference series.",
                    parent=chart_window,
                )
                return
            comparison_headers = [
                header for header in selected_headers if header != reference_header
            ]
            if not comparison_headers and not show_reference_value.get():
                messagebox.showwarning(
                    "No series to display",
                    "Select a measurement series or enable display of the reference series.",
                    parent=chart_window,
                )
                return

            current_view.update(period=period, title=title_suffix)
            axes.clear()
            hover_series.clear()
            displayed_count = 0

            for header in comparison_headers:
                times, values = dataset.points(
                    header, time_from, time_to, period or "all"
                )
                delta_times: list[datetime] = []
                delta_values: list[float] = []
                for time, value in zip(times, values):
                    if time in reference_lookup:
                        delta_times.append(time)
                        delta_values.append(value - reference_lookup[time])
                if not delta_times:
                    continue
                displayed_count = displayed_count or len(delta_times)
                short_name = short_series_name(header)
                axes.plot(delta_times, delta_values, linewidth=1, label=short_name)
                hover_series.append(
                    (short_name, list(date2num(delta_times)), delta_values, delta_times)
                )

            if show_reference_value.get():
                zero_times = sorted(reference_lookup)
                zero_values = [0.0] * len(zero_times)
                displayed_count = displayed_count or len(zero_times)
                reference_name = (
                    short_series_name(reference_header) + " (reference)"
                )
                axes.plot(
                    zero_times,
                    zero_values,
                    linewidth=1.2,
                    linestyle="--",
                    label=reference_name,
                )
                hover_series.append(
                    (reference_name, list(date2num(zero_times)), zero_values, zero_times)
                )

            if not hover_series:
                messagebox.showwarning(
                    "No data",
                    "The selected series have no common time samples with the reference.",
                    parent=chart_window,
                )
                return

            formatted_count = f"{displayed_count:,}".replace(",", ".")
            axes.set_title(
                f"Delta (Δ) - {title_suffix} ({formatted_count})",
                fontsize=24,
            )
            axes.set_xlabel("Time", fontsize=20)
            delta_label = f"Δ ({reference_unit})" if reference_unit else "Δ"
            axes.set_ylabel(delta_label, fontsize=20)
            axes.tick_params(axis="both", labelsize=14)
            axes.xaxis.set_major_formatter(
                DateFormatter("%H:%M:%S\n%d.%m.%Y.", tz=first_time.tzinfo)
            )
            axes.set_xlim(time_from, time_to)
            axes.grid(True, alpha=0.3)
            axes.legend(loc="upper left", fontsize=14)
            hover_annotation = HoverTooltip(axes, canvas, fontsize=12).annotation
            apply_scale()
            figure.autofmt_xdate()
            canvas.draw_idle()

        def rerender(_event: object = None) -> None:
            render_delta(current_view["period"], str(current_view["title"]))

        time_filter = TimeRangeControls(chart_window, dataset, rerender)
        time_filter.pack(
            side=tk.TOP,
            fill=tk.X,
            padx=10,
            pady=(0, 5),
        )

        view_controls = ttk.LabelFrame(
            chart_window, text="View mode", padding=(10, 5, 10, 7)
        )
        view_controls.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 5))
        ttk.Button(
            view_controls,
            text="All samples",
            command=lambda: render_delta(None, "all samples"),
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            view_controls,
            text="1-minute average",
            command=lambda: render_delta("minute", "1-minute average"),
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            view_controls,
            text="1-hour average",
            command=lambda: render_delta("hour", "1-hour average"),
        ).pack(side=tk.LEFT)

        SeriesChecklist(
            chart_window, "Measurement series", visibility, rerender
        ).pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 5))

        def change_reference(_event: object = None) -> None:
            selected_unit = unit_from_header(reference_value.get())
            for header, selected in visibility.items():
                selected.set(unit_from_header(header) == selected_unit)
            reference_zero_text.set(
                f"Show reference series as Δ = 0"
                f"{' ' + selected_unit if selected_unit else ''}"
            )
            rerender()

        reference_picker.bind("<<ComboboxSelected>>", change_reference)
        for child in reference_controls.winfo_children():
            if isinstance(child, ttk.Checkbutton):
                child.configure(command=rerender)

        ExportControls(
            chart_window,
            figure,
            canvas,
            self.current_file,
            "delta_graf",
            "Save Delta chart as PNG",
        ).pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 5))

        def show_hover_value(event: object) -> None:
            if (
                hover_annotation is None
                or event.inaxes is not axes
                or event.xdata is None
                or event.x is None
                or event.y is None
            ):
                if hover_annotation is not None and hover_annotation.get_visible():
                    hover_annotation.set_visible(False)
                    canvas.draw_idle()
                return
            nearest = None
            nearest_distance = 16.0
            for name, numeric_times, values, original_times in hover_series:
                position = bisect_left(numeric_times, event.xdata)
                for index in (position - 1, position):
                    if not 0 <= index < len(numeric_times):
                        continue
                    point_x, point_y = axes.transData.transform(
                        (numeric_times[index], values[index])
                    )
                    distance = ((point_x - event.x) ** 2 + (point_y - event.y) ** 2) ** 0.5
                    if distance < nearest_distance:
                        nearest_distance = distance
                        nearest = (
                            name,
                            numeric_times[index],
                            values[index],
                            original_times[index],
                        )
            if nearest is None:
                if hover_annotation.get_visible():
                    hover_annotation.set_visible(False)
                    canvas.draw_idle()
                return
            name, numeric_time, delta_value, original_time = nearest
            hover_annotation.xy = (numeric_time, delta_value)
            hover_annotation.set_text(
                f"{name}\n{original_time.strftime('%d.%m.%Y. %H:%M:%S')}\n"
                f"Δ = {delta_value:.6g}"
                f"{' ' + current_delta_unit['unit'] if current_delta_unit['unit'] else ''}"
            )
            hover_annotation.set_visible(True)
            canvas.draw_idle()

        canvas.mpl_connect("motion_notify_event", show_hover_value)
        canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        render_delta(None, "all samples")

    def show_histogram(self) -> None:
        """Display a generic histogram of numeric measurement series."""
        dataset = self.dataset
        if dataset is None or not dataset.has_time or not dataset.series:
            messagebox.showwarning(
                "Missing data",
                "CSV must contain a 'Time' column and numeric measurement columns.",
            )
            return

        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure
        except ImportError:
            messagebox.showerror(
                "Matplotlib is not installed",
                "Run: python -m pip install -r requirements.txt",
            )
            return

        series_data = dataset.series_data()

        if not series_data:
            messagebox.showwarning(
                "No data", "No valid numeric values are available for the histogram."
            )
            return

        chart_window = tk.Toplevel(self)
        chart_window.title(
            f"Histogram — {self.current_file.name if self.current_file else ''}"
        )
        chart_window.geometry("1200x900")
        chart_window.minsize(800, 650)

        figure = Figure(figsize=(12, 7), dpi=100, layout="constrained")
        axes = figure.add_subplot(111)
        canvas = FigureCanvasTkAgg(figure, master=chart_window)

        all_times = [time for times, _values in series_data.values() for time in times]
        first_time = min(all_times)
        last_time = max(all_times)
        first_unit = unit_from_header(next(iter(series_data)))
        visibility = {
            header: tk.BooleanVar(value=unit_from_header(header) == first_unit)
            for header in series_data
        }
        date_from_value = tk.StringVar(value=first_time.strftime("%d.%m.%Y."))
        time_from_value = tk.StringVar(value=first_time.strftime("%H:%M:%S"))
        date_to_value = tk.StringVar(value=last_time.strftime("%d.%m.%Y."))
        time_to_value = tk.StringVar(value=last_time.strftime("%H:%M:%S"))
        bins_value = tk.StringVar(value="50")
        auto_bins_value = tk.BooleanVar(value=False)
        hover_bins: list[tuple[object, str, float, float, int]] = []
        hover_annotation = None

        histogram_controls = ttk.LabelFrame(
            chart_window, text="Histogram settings", padding=(10, 5, 10, 7)
        )
        histogram_controls.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 5))
        ttk.Label(histogram_controls, text="Number of bins:").pack(side=tk.LEFT)
        bins_entry = ttk.Entry(histogram_controls, textvariable=bins_value, width=8)
        bins_entry.pack(side=tk.LEFT, padx=(5, 14))

        statistics_frame = ttk.LabelFrame(
            chart_window, text="Statistics", padding=(8, 5, 8, 7)
        )
        statistics_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 5))
        statistics_table = ttk.Treeview(
            statistics_frame,
            columns=("series", "n", "mean", "std", "min", "max"),
            show="headings",
            height=min(5, len(series_data)),
        )
        for column, label, width in (
            ("series", "Series", 330),
            ("n", "N", 90),
            ("mean", "Mean", 130),
            ("std", "Std Dev", 130),
            ("min", "Min", 130),
            ("max", "Max", 130),
        ):
            statistics_table.heading(column, text=label)
            statistics_table.column(column, width=width, anchor=tk.CENTER)
        statistics_table.pack(fill=tk.X)

        def render_histogram(_event: object = None) -> None:
            nonlocal hover_annotation
            selected_headers = [
                header for header, selected in visibility.items() if selected.get()
            ]
            if not selected_headers:
                messagebox.showwarning(
                    "No series selected",
                    "Select at least one measurement series.",
                    parent=chart_window,
                )
                return

            selected_units = {unit_from_header(header) for header in selected_headers}
            if len(selected_units) > 1:
                readable_units = ", ".join(unit or "no unit" for unit in selected_units)
                messagebox.showwarning(
                    "Different measurement units",
                    f"Only series with the same unit can be shown on the same histogram. "
                    f"Selected units: {readable_units}.",
                    parent=chart_window,
                )
                return

            try:
                time_from, time_to = time_filter.bounds()
            except ValueError:
                messagebox.showerror(
                    "Invalid date or time",
                    "Enter the date as 27.09.2026. and the time as 00:00.",
                    parent=chart_window,
                )
                return
            if time_from >= time_to:
                messagebox.showerror(
                    "Invalid range",
                    "Start time must be earlier than end time.",
                    parent=chart_window,
                )
                return

            filtered_data: dict[str, list[float]] = {}
            for header in selected_headers:
                _times, filtered_values = dataset.points(
                    header, time_from, time_to, "all"
                )
                if filtered_values:
                    filtered_data[header] = filtered_values
            if not filtered_data:
                messagebox.showwarning(
                    "No data",
                    "The selected series have no samples in the specified time range.",
                    parent=chart_window,
                )
                return

            total_samples = sum(len(values) for values in filtered_data.values())
            if auto_bins_value.get():
                bin_count = max(1, math.ceil(math.log2(total_samples) + 1))
                bins_value.set(str(bin_count))
            else:
                try:
                    bin_count = int(bins_value.get().strip())
                except ValueError:
                    messagebox.showerror(
                        "Invalid number of bins",
                        "The number of bins must be an integer.",
                        parent=chart_window,
                    )
                    return
                if not 1 <= bin_count <= 10_000:
                    messagebox.showerror(
                        "Invalid number of bins",
                        "The number of bins must be between 1 and 10,000.",
                        parent=chart_window,
                    )
                    return

            all_values = [
                value for values in filtered_data.values() for value in values
            ]
            range_min = min(all_values)
            range_max = max(all_values)
            if range_min == range_max:
                padding = abs(range_min) * 0.01 or 0.5
                range_min -= padding
                range_max += padding

            axes.clear()
            hover_bins.clear()
            statistics_table.delete(*statistics_table.get_children())
            for header, values in filtered_data.items():
                counts, edges, patches = axes.hist(
                    values,
                    bins=bin_count,
                    range=(range_min, range_max),
                    alpha=0.45,
                    label=short_series_name(header),
                    edgecolor="black",
                    linewidth=0.5,
                )
                for count, left, right, patch in zip(
                    counts, edges[:-1], edges[1:], patches
                ):
                    hover_bins.append(
                        (patch, header, float(left), float(right), int(count))
                    )
                std_dev = statistics.stdev(values) if len(values) > 1 else 0.0
                statistics_table.insert(
                    "",
                    tk.END,
                    values=(
                        short_series_name(header),
                        f"{len(values):,}".replace(",", "."),
                        f"{statistics.fmean(values):.9g}",
                        f"{std_dev:.9g}",
                        f"{min(values):.9g}",
                        f"{max(values):.9g}",
                    ),
                )

            unit = next(iter(selected_units))
            x_label = f"Measured value ({unit})" if unit else "Measured value"
            axes.set_title(
                f"Histogram - {bin_count} bins ({total_samples:,} samples)".replace(
                    ",", "."
                ),
                fontsize=20,
            )
            axes.set_xlabel(x_label, fontsize=16)
            axes.set_ylabel("Sample count", fontsize=16)
            axes.tick_params(axis="both", labelsize=12)
            axes.grid(True, axis="y", alpha=0.3)
            axes.legend(loc="upper right", fontsize=12)
            hover_annotation = HoverTooltip(axes, canvas, fontsize=11).annotation
            canvas.draw_idle()

        time_filter = TimeRangeControls(chart_window, dataset, render_histogram)
        time_filter.pack(
            side=tk.TOP,
            fill=tk.X,
            padx=10,
            pady=(0, 5),
            before=histogram_controls,
        )

        def toggle_auto_bins() -> None:
            bins_entry.configure(
                state=tk.DISABLED if auto_bins_value.get() else tk.NORMAL
            )
            render_histogram()

        ttk.Checkbutton(
            histogram_controls,
            text="Auto bins",
            variable=auto_bins_value,
            command=toggle_auto_bins,
        ).pack(side=tk.LEFT, padx=(0, 14))
        ttk.Button(
            histogram_controls, text="Apply", command=render_histogram
        ).pack(side=tk.LEFT)
        bins_entry.bind("<Return>", render_histogram)

        SeriesChecklist(
            chart_window, "Measurement series", visibility, render_histogram
        ).pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 5))

        ExportControls(
            chart_window,
            figure,
            canvas,
            self.current_file,
            "histogram",
            "Save histogram as PNG",
        ).pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 5))

        def show_hover_value(event: object) -> None:
            if hover_annotation is None or event.inaxes is not axes:
                if hover_annotation is not None and hover_annotation.get_visible():
                    hover_annotation.set_visible(False)
                    canvas.draw_idle()
                return
            hovered = None
            for patch, header, left, right, count in reversed(hover_bins):
                contains, _details = patch.contains(event)
                if contains:
                    hovered = (patch, header, left, right, count)
                    break
            if hovered is None:
                if hover_annotation.get_visible():
                    hover_annotation.set_visible(False)
                    canvas.draw_idle()
                return
            patch, header, left, right, count = hovered
            hover_annotation.xy = (
                patch.get_x() + patch.get_width() / 2,
                patch.get_height(),
            )
            unit = unit_from_header(header)
            unit_suffix = f" {unit}" if unit else ""
            hover_annotation.set_text(
                f"{short_series_name(header)}\n"
                f"{left:.9g} – {right:.9g}{unit_suffix}\n"
                f"Sample count: {count}"
            )
            hover_annotation.set_visible(True)
            canvas.draw_idle()

        canvas.mpl_connect("motion_notify_event", show_hover_value)
        canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        render_histogram()

    def show_scatter(self) -> None:
        """Display a generic relationship between two numeric measurement series."""
        dataset = self.dataset
        if dataset is None or not dataset.has_time or len(dataset.series) < 2:
            messagebox.showwarning(
                "Missing data",
                "Scatter plot requires a 'Time' column and at least two numeric series.",
            )
            return

        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure
        except ImportError:
            messagebox.showerror(
                "Matplotlib is not installed",
                "Run: python -m pip install -r requirements.txt",
            )
            return

        series_data = dataset.series_data()

        if len(series_data) < 2:
            messagebox.showwarning(
                "Missing data",
                "Scatter plot requires at least two valid numeric series.",
            )
            return

        chart_window = tk.Toplevel(self)
        chart_window.title(
            f"Scatter — {self.current_file.name if self.current_file else ''}"
        )
        chart_window.geometry("1200x850")
        chart_window.minsize(820, 620)

        figure = Figure(figsize=(12, 7), dpi=100, layout="constrained")
        axes = figure.add_subplot(111)
        canvas = FigureCanvasTkAgg(figure, master=chart_window)

        headers = list(series_data)
        x_series_value = tk.StringVar(value=headers[0])
        y_series_value = tk.StringVar(value=headers[1])
        y_mode_value = tk.StringVar(value="Value")
        all_times = [time for times, _values in series_data.values() for time in times]
        first_time = min(all_times)
        last_time = max(all_times)
        date_from_value = tk.StringVar(value=first_time.strftime("%d.%m.%Y."))
        time_from_value = tk.StringVar(value=first_time.strftime("%H:%M:%S"))
        date_to_value = tk.StringVar(value=last_time.strftime("%d.%m.%Y."))
        time_to_value = tk.StringVar(value=last_time.strftime("%H:%M:%S"))
        x_min_value = tk.StringVar()
        x_max_value = tk.StringVar()
        y_min_value = tk.StringVar()
        y_max_value = tk.StringVar()
        manual_scale = {"enabled": False}
        current_view: dict[str, str | None] = {
            "period": None,
            "title": "all samples",
        }
        scatter_collection = None
        hover_annotation = None
        plotted_x: list[float] = []
        plotted_y: list[float] = []
        plotted_times: list[datetime] = []

        series_controls = ttk.LabelFrame(
            chart_window, text="Series selection", padding=(10, 5, 10, 7)
        )
        series_controls.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(8, 5))
        ttk.Label(series_controls, text="X series:").pack(side=tk.LEFT)
        x_picker = ttk.Combobox(
            series_controls,
            textvariable=x_series_value,
            values=headers,
            state="readonly",
            width=42,
        )
        x_picker.pack(side=tk.LEFT, padx=(5, 16))
        ttk.Label(series_controls, text="Y series:").pack(side=tk.LEFT)
        y_picker = ttk.Combobox(
            series_controls,
            textvariable=y_series_value,
            values=headers,
            state="readonly",
            width=42,
        )
        y_picker.pack(side=tk.LEFT, padx=(5, 16))
        ttk.Label(series_controls, text="Y mode:").pack(side=tk.LEFT)
        mode_picker = ttk.Combobox(
            series_controls,
            textvariable=y_mode_value,
            values=("Value", "Δ to X"),
            state="readonly",
            width=10,
        )
        mode_picker.pack(side=tk.LEFT, padx=(5, 0))

        view_controls = ttk.LabelFrame(
            chart_window, text="View mode", padding=(10, 5, 10, 7)
        )
        view_controls.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 5))

        scale_controls = ttk.LabelFrame(
            chart_window, text="Skala osi", padding=(10, 5, 10, 7)
        )
        scale_controls.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 5))
        ttk.Label(scale_controls, text="X min:").pack(side=tk.LEFT)
        x_min_entry = ttk.Entry(scale_controls, textvariable=x_min_value, width=10)
        x_min_entry.pack(side=tk.LEFT, padx=(5, 10))
        ttk.Label(scale_controls, text="X max:").pack(side=tk.LEFT)
        x_max_entry = ttk.Entry(scale_controls, textvariable=x_max_value, width=10)
        x_max_entry.pack(side=tk.LEFT, padx=(5, 16))
        ttk.Label(scale_controls, text="Y min:").pack(side=tk.LEFT)
        y_min_entry = ttk.Entry(scale_controls, textvariable=y_min_value, width=10)
        y_min_entry.pack(side=tk.LEFT, padx=(5, 10))
        ttk.Label(scale_controls, text="Y max:").pack(side=tk.LEFT)
        y_max_entry = ttk.Entry(scale_controls, textvariable=y_max_value, width=10)
        y_max_entry.pack(side=tk.LEFT, padx=(5, 14))

        def automatic_limits(values: list[float]) -> tuple[float, float]:
            lower = min(values)
            upper = max(values)
            padding = (upper - lower) * 0.05
            if padding == 0:
                padding = abs(lower) * 0.01 or 0.5
            return lower - padding, upper + padding

        def apply_scale(_event: object = None) -> None:
            try:
                x_min = parse_number(x_min_value.get())
                x_max = parse_number(x_max_value.get())
                y_min = parse_number(y_min_value.get())
                y_max = parse_number(y_max_value.get())
            except ValueError:
                messagebox.showerror(
                    "Invalid value",
                    "Sve granice X i Y skale moraju biti brojevi.",
                    parent=chart_window,
                )
                return
            if x_min >= x_max or y_min >= y_max:
                messagebox.showerror(
                    "Invalid range",
                    "Minimalna vrijednost svake osi mora biti manja od maksimalne.",
                    parent=chart_window,
                )
                return
            manual_scale["enabled"] = True
            axes.set_xlim(x_min, x_max)
            axes.set_ylim(y_min, y_max)
            canvas.draw_idle()

        def render_scatter(period: str | None, title_suffix: str) -> None:
            nonlocal scatter_collection, hover_annotation
            nonlocal plotted_x, plotted_y, plotted_times
            x_header = x_series_value.get()
            y_header = y_series_value.get()
            y_mode = y_mode_value.get()
            if x_header == y_header:
                messagebox.showwarning(
                    "Identical series",
                    "Select different X and Y series.",
                    parent=chart_window,
                )
                return
            x_unit = unit_from_header(x_header)
            y_unit = unit_from_header(y_header)
            if y_mode == "Δ to X" and x_unit != y_unit:
                messagebox.showwarning(
                    "Different measurement units",
                    "'Δ to X' mode requires X and Y series to use the same measurement unit.",
                    parent=chart_window,
                )
                return
            try:
                time_from, time_to = time_filter.bounds()
            except ValueError:
                messagebox.showerror(
                    "Invalid date or time",
                    "Enter the date as 27.09.2026. and the time as 00:00.",
                    parent=chart_window,
                )
                return
            if time_from >= time_to:
                messagebox.showerror(
                    "Invalid range",
                    "Start time must be earlier than end time.",
                    parent=chart_window,
                )
                return

            x_times, x_values = dataset.points(
                x_header, time_from, time_to, period or "all"
            )
            y_times, y_values = dataset.points(
                y_header, time_from, time_to, period or "all"
            )
            x_by_time = dict(zip(x_times, x_values))
            y_by_time = dict(zip(y_times, y_values))
            paired_times = sorted(x_by_time.keys() & y_by_time.keys())
            if not paired_times:
                messagebox.showwarning(
                    "No common samples",
                    "The X and Y series have no common time samples in the selected range.",
                    parent=chart_window,
                )
                return

            plotted_times = paired_times
            plotted_x = [x_by_time[time] for time in paired_times]
            if y_mode == "Δ to X":
                plotted_y = [
                    y_by_time[time] - x_by_time[time] for time in paired_times
                ]
            else:
                plotted_y = [y_by_time[time] for time in paired_times]

            current_view.update(period=period, title=title_suffix)
            axes.clear()
            short_x = short_series_name(x_header)
            short_y = short_series_name(y_header)
            if y_mode == "Δ to X":
                y_description = f"{short_y} - {short_x}"
                y_label = f"{y_description} (Δ {y_unit})" if y_unit else y_description
            else:
                y_description = short_y
                y_label = y_header
            scatter_collection = axes.scatter(
                plotted_x,
                plotted_y,
                s=14,
                alpha=0.65,
                label=f"{short_x} → {y_description}",
            )
            axes.set_title(
                f"Scatter - {title_suffix} ({len(paired_times):,} samples)".replace(
                    ",", "."
                ),
                fontsize=22,
            )
            axes.set_xlabel(x_header, fontsize=16)
            axes.set_ylabel(y_label, fontsize=16)
            axes.tick_params(axis="both", labelsize=12)
            axes.grid(True, alpha=0.3)
            axes.legend(loc="best", fontsize=12)
            hover_annotation = HoverTooltip(axes, canvas, fontsize=11).annotation

            if manual_scale["enabled"]:
                axes.set_xlim(
                    parse_number(x_min_value.get()), parse_number(x_max_value.get())
                )
                axes.set_ylim(
                    parse_number(y_min_value.get()), parse_number(y_max_value.get())
                )
            else:
                x_min, x_max = automatic_limits(plotted_x)
                y_min, y_max = automatic_limits(plotted_y)
                axes.set_xlim(x_min, x_max)
                axes.set_ylim(y_min, y_max)
                x_min_value.set(f"{x_min:.9g}")
                x_max_value.set(f"{x_max:.9g}")
                y_min_value.set(f"{y_min:.9g}")
                y_max_value.set(f"{y_max:.9g}")
            canvas.draw_idle()

        def rerender(_event: object = None) -> None:
            render_scatter(current_view["period"], str(current_view["title"]))

        def reset_scale() -> None:
            manual_scale["enabled"] = False
            rerender()

        time_filter = TimeRangeControls(chart_window, dataset, rerender)
        time_filter.pack(
            side=tk.TOP,
            fill=tk.X,
            padx=10,
            pady=(0, 5),
            before=view_controls,
        )

        ttk.Button(
            view_controls,
            text="All samples",
            command=lambda: render_scatter(None, "all samples"),
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            view_controls,
            text="1-minute average",
            command=lambda: render_scatter("minute", "1-minute average"),
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            view_controls,
            text="1-hour average",
            command=lambda: render_scatter("hour", "1-hour average"),
        ).pack(side=tk.LEFT)

        ttk.Button(scale_controls, text="Apply", command=apply_scale).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(
            scale_controls, text="Auto scale", command=reset_scale
        ).pack(side=tk.LEFT)
        for entry in (x_min_entry, x_max_entry, y_min_entry, y_max_entry):
            entry.bind("<Return>", apply_scale)
        for picker in (x_picker, y_picker, mode_picker):
            picker.bind("<<ComboboxSelected>>", rerender)

        ExportControls(
            chart_window,
            figure,
            canvas,
            self.current_file,
            "scatter",
            "Save Scatter plot as PNG",
        ).pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 5))

        def show_hover_value(event: object) -> None:
            if (
                scatter_collection is None
                or hover_annotation is None
                or event.inaxes is not axes
            ):
                if hover_annotation is not None and hover_annotation.get_visible():
                    hover_annotation.set_visible(False)
                    canvas.draw_idle()
                return
            contains, details = scatter_collection.contains(event)
            indices = details.get("ind", []) if contains else []
            if len(indices) == 0:
                if hover_annotation.get_visible():
                    hover_annotation.set_visible(False)
                    canvas.draw_idle()
                return
            index = int(indices[0])
            x_header = x_series_value.get()
            y_header = y_series_value.get()
            x_unit = unit_from_header(x_header)
            y_unit = unit_from_header(y_header)
            y_mode = y_mode_value.get()
            hover_annotation.xy = (plotted_x[index], plotted_y[index])
            if y_mode == "Δ to X":
                y_description = (
                    f"{short_series_name(y_header)} - {short_series_name(x_header)}"
                )
                displayed_y_unit = y_unit
            else:
                y_description = short_series_name(y_header)
                displayed_y_unit = y_unit
            hover_annotation.set_text(
                f"X: {short_series_name(x_header)}\n{plotted_x[index]:.9g}"
                f"{' ' + x_unit if x_unit else ''}\n"
                f"Y: {y_description}\n{plotted_y[index]:.9g}"
                f"{' ' + displayed_y_unit if displayed_y_unit else ''}\n"
                f"Time: {plotted_times[index].strftime('%d.%m.%Y. %H:%M:%S')}"
            )
            hover_annotation.set_visible(True)
            canvas.draw_idle()

        canvas.mpl_connect("motion_notify_event", show_hover_value)
        canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        render_scatter(None, "all samples")

    def _configure_columns(self) -> None:
        column_ids = [f"column_{index}" for index in range(len(self.headers))]
        self.table.configure(columns=column_ids)
        for column_id, header in zip(column_ids, self.headers):
            self.table.heading(column_id, text=header)
            width = min(max(len(header) * 8 + 30, 100), 300)
            self.table.column(column_id, width=width, minwidth=70, stretch=False)

    @property
    def page_count(self) -> int:
        return max(1, (len(self.rows) + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)

    def _show_page(self) -> None:
        self.table.delete(*self.table.get_children())
        start = self.current_page * ROWS_PER_PAGE
        end = min(start + ROWS_PER_PAGE, len(self.rows))

        for row in self.rows[start:end]:
            normalized = (row + [""] * len(self.headers))[: len(self.headers)]
            self.table.insert("", tk.END, values=normalized)

        self.page_text.set(f"Page {self.current_page + 1} / {self.page_count}")
        self.previous_button.configure(
            state=tk.NORMAL if self.current_page > 0 else tk.DISABLED
        )
        self.next_button.configure(
            state=tk.NORMAL if self.current_page + 1 < self.page_count else tk.DISABLED
        )

    def previous_page(self) -> None:
        if self.current_page > 0:
            self.current_page -= 1
            self._show_page()

    def next_page(self) -> None:
        if self.current_page + 1 < self.page_count:
            self.current_page += 1
            self._show_page()


if __name__ == "__main__":
    CsvViewer().mainloop()
