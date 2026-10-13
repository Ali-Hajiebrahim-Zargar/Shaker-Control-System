"""Offline CSV viewer for Shaker Control System / CSU15-45N-USB.

Install: python -m pip install numpy pandas plotly
Run:     python shaker_csv_viewer.py
Headless: python shaker_csv_viewer.py --csv PATH --output plot.html --no-open

Saved Force (N) is used unchanged. Scope captures remain separate; their
Waveform Time is never interpreted as the force sensor's recording clock.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import math
from pathlib import Path
import queue
import re
import threading
import webbrowser

import numpy as np
import pandas as pd

APP_TITLE = "Shaker CSV Viewer 1.2"
FORMATS = ("Auto detect", "Force / combined project CSV", "Scope project CSV", "Legacy force counts", "Legacy Tektronix CSV")


def key(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def finite_positive(value, label):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be a finite positive number.")
    return value


def read_table(path, format_hint="Auto detect"):
    """Locate known named headers, including BOM/preamble and common delimiters."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise ValueError(f"CSV file not found:\n{path}")
    with path.open(encoding="utf-8-sig", newline="") as f:
        lines = [line for _, line in zip(range(200), f)]
    if not lines:
        raise ValueError("The CSV file is empty.")
    for row_index, line in enumerate(lines):
        for delimiter in (",", ";", "\t"):
            row = next(csv.reader([line], delimiter=delimiter))
            names = {key(c) for c in row}
            scope = {"capturetimes", "channel", "sampleindex", "waveformtimes", "voltagev"} <= names
            timed = bool(names & {"time", "times", "timestamp", "timems"})
            values = bool(names & {"forcen", "rawcounts", "counts", "piezovoltagev", "positioncounts", "motorpositioncounts", "voltagev"})
            if scope or (timed and values):
                if format_hint == "Legacy Tektronix CSV":
                    continue
                df = pd.read_csv(path, skiprows=row_index, sep=delimiter, encoding="utf-8-sig")
                df.columns = [str(c).strip() for c in df.columns]
                normalized = [key(c) for c in df.columns]
                if len(set(normalized)) != len(normalized):
                    raise ValueError("Duplicate column names: please use an unmodified acquisition export.")
                if scope:
                    detected = "Scope project CSV"
                elif "forcen" in names or names & {"piezovoltagev", "motorpositioncounts", "positioncounts", "voltagev"}:
                    detected = "Force / combined project CSV"
                else:
                    detected = "Legacy force counts"
                if format_hint not in ("Auto detect", detected):
                    raise ValueError(f"Detected {detected}, but selected {format_hint}. Choose Auto detect.")
                return df, detected
    # The old Tektronix export has metadata in columns 1-3 and samples in 4-5.
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    has_metadata = any(row and key(row[0]) in {"recordlength", "sampleinterval", "source", "triggerpoint"} for row in rows[:200])
    if format_hint == "Legacy Tektronix CSV" or (format_hint == "Auto detect" and has_metadata):
        width = max((len(r) for r in rows), default=0)
        if width < 5:
            raise ValueError("Legacy Tektronix format requires time and voltage in columns 4 and 5.")
        return pd.DataFrame([r + [None] * (width - len(r)) for r in rows]), "Legacy Tektronix CSV"
    raise ValueError("CSV format not recognized. Expected the project's force_sensor.csv or oscilloscope.csv.\n"
                     "For an older Tektronix export with time/voltage in columns 4/5, select Legacy Tektronix CSV.")


def numeric(series):
    # Semicolon-separated European decimal commas are accepted.
    if series.dtype == object:
        series = series.astype(str).str.strip().str.replace(",", ".", regex=False)
    return pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)


def make_record(t, y, label, unit, peak_distance, note="", states=None, capture=None, channel=None):
    t, y = np.asarray(t, float), np.asarray(y, float)
    valid = np.isfinite(t) & np.isfinite(y)
    dropped = int(len(t) - valid.sum())
    states = np.asarray(states, str)[valid] if states is not None else None
    t, y = t[valid], y[valid]
    if len(t) < 10:
        raise ValueError(f"{label}: at least 10 valid samples are required (found {len(t)}).")
    if np.any(np.diff(t) < 0):
        raise ValueError(f"{label}: time runs backwards. Split restarted recordings before plotting; sorting them would mix recordings.")
    keep = np.r_[np.diff(t) != 0, True]  # Last value at each duplicate timestamp.
    duplicate_count = int(len(t) - keep.sum())
    t, y = t[keep], y[keep]
    if states is not None:
        states = states[keep]
    if len(t) < 10:
        raise ValueError(f"{label}: fewer than 10 distinct valid timestamps.")
    dt = np.diff(t)
    median_dt = float(np.median(dt))
    details = [note] if note else []
    if dropped:
        details.append(f"Omitted {dropped} rows with missing/non-numeric time or signal values.")
    if duplicate_count:
        details.append(f"Kept the last value at each duplicate timestamp ({duplicate_count} duplicates).")
    if np.any(dt > 5 * median_dt):
        details.append("Time gaps detected. Lines and filters restart at gaps > 5 median sample intervals; frequency excludes those gaps.")
    if np.quantile(np.abs(dt / median_dt - 1), .95) > .05:
        details.append("Irregular sample timing; sampling rate and Nyquist limit shown are median-based estimates.")
    if np.ptp(y) == 0:
        details.append("This saved signal is constant. Filtering cannot recover changes that were not recorded.")
    return dict(label=label, unit=unit, t=t.tolist(), y=y.tolist(),
                peakDistance=peak_distance, medianDt=median_dt, note=" ".join(details),
                states=states.tolist() if states is not None else None,
                capture=capture, channel=channel)


def load_csv(path, format_hint="Auto detect", sensor_rating=45.0, baseline_mode="Factory baseline (255)", baseline_counts=255.0):
    """Return a JSON-serializable document; no hardware or desktop needed."""
    df, detected = read_table(path, format_hint)
    if df.empty:
        raise ValueError("This CSV contains a header but no recorded samples.")
    records, warnings = [], []
    columns = {key(c): c for c in df.columns}
    if detected == "Scope project CSV":
        capture = numeric(df[columns["capturetimes"]])
        sample = numeric(df[columns["sampleindex"]])
        channels = df[columns["channel"]].fillna("").astype(str).str.strip().str.upper().to_numpy()
        waveform_t = numeric(df[columns["waveformtimes"]])
        voltages = numeric(df[columns["voltagev"]])
        valid_id = np.isfinite(capture) & np.isfinite(sample) & (sample >= 0) & (sample == np.floor(sample)) & (channels != "")
        if not valid_id.all():
            raise ValueError("A scope row has a missing or invalid capture time, channel, or sample index. Repair the export before selecting captures.")
        # Split by file order, capture/channel changes AND index resets. Never
        # group by waveform time, or join captures into a continuous waveform.
        starts = np.r_[0, np.flatnonzero((capture[1:] != capture[:-1]) | (channels[1:] != channels[:-1]) | (sample[1:] <= sample[:-1])) + 1]
        ends = np.r_[starts[1:], len(df)]
        for number, (a, b) in enumerate(zip(starts, ends), 1):
            label = f"{channels[a]} | capture {number} | PC time {capture[a]:.6f} s"
            try:
                records.append(make_record(waveform_t[a:b], voltages[a:b], label, "V", .02,
                    "X is scope Waveform Time (s), relative to the scope time origin. PC Capture Time identifies the retrieval; it is not an acquisition trigger timestamp.",
                    capture=float(capture[a]), channel=str(channels[a])))
            except ValueError as exc:
                warnings.append(str(exc))
        if not records:
            raise ValueError("No usable scope capture. " + "\n".join(warnings[:5]))
        summary = f"{len(records)} separate waveform captures. Select a channel and capture in the plot window."
    elif detected == "Legacy Tektronix CSV":
        channel = "Oscilloscope"
        for _, row in df.iloc[:200].iterrows():
            if key(row.iloc[0]) == "source" and pd.notna(row.iloc[1]):
                channel = str(row.iloc[1]).strip()
                break
        records.append(make_record(numeric(df.iloc[:, 3]), numeric(df.iloc[:, 4]), channel, "V", .02,
                                   "Legacy Tektronix export: time = column 4, voltage = column 5.", channel=channel))
        summary = "One oscilloscope waveform."
    else:
        tkey = next(k for k in ("times", "time", "timems", "timestamp") if k in columns)
        t = numeric(df[columns[tkey]]) * (.001 if tkey == "timems" else 1)
        states = df[columns["motorstate"]].fillna("Unknown").astype(str).to_numpy() if "motorstate" in columns else None
        for col_key, label, unit, spacing in [("forcen", "Force (N) — saved calibration", "N", .08),
                                              ("piezovoltagev", "Piezo voltage (V)", "V", .02),
                                              ("voltagev", "Voltage (V)", "V", .02),
                                              ("motorpositioncounts", "Motor position (counts)", "counts", .08),
                                              ("positioncounts", "Motor position (counts)", "counts", .08)]:
            if col_key in columns:
                note = "Saved Newton values used unchanged; acquisition calibration and any manual tare are preserved." if col_key == "forcen" else "Values as saved in the CSV."
                try:
                    records.append(make_record(t, numeric(df[columns[col_key]]), label, unit, spacing, note, states))
                except ValueError as exc:
                    warnings.append(str(exc))
        counts_key = next((k for k in ("rawcounts", "counts") if k in columns), None)
        if counts_key:
            counts = numeric(df[columns[counts_key]])
            # ONLY count-only legacy files require conversion. Never substitute
            # a reconstructed force for a saved calibrated Force (N) column.
            if "forcen" not in columns:
                rating = finite_positive(sensor_rating, "Sensor rating")
                finite = np.isfinite(t) & np.isfinite(counts)
                if not finite.any():
                    raise ValueError("No numeric raw counts.")
                if baseline_mode == "First 1 s median (unloaded only)":
                    window = finite & (t >= t[finite][0]) & (t <= t[finite][0] + 1)
                    baseline = float(np.median(counts[window]))
                elif baseline_mode == "Custom baseline":
                    baseline = float(baseline_counts)
                    if not math.isfinite(baseline):
                        raise ValueError("Baseline counts must be finite.")
                else:
                    baseline = 255.0
                force = (counts - baseline) * rating / 512
                records.insert(0, make_record(t, force, "Force (N) — converted from counts", "N", .08,
                     f"Count-only conversion: (counts − {baseline:g}) × {rating:g} / 512. {baseline_mode}.", states))
            try:
                records.append(make_record(t, counts, "Raw counts", "counts", .08, "Unconverted sensor counts as recorded.", states))
            except ValueError as exc:
                warnings.append(str(exc))
        if not records:
            raise ValueError("No usable numeric signal. " + "\n".join(warnings))
        summary = "Select Force, raw counts or motor position in the plot window. Saved Force (N) is never re-zeroed." if "forcen" in columns else "Select a recorded signal in the plot window."
    return dict(file=Path(path).name, format=detected, summary=summary, records=records, warnings=warnings,
                scope=detected == "Scope project CSV")


def write_plot(document, output_path, open_browser=True):
    # Import on demand so parsing and desktop launch remain lightweight.
    from plotly.offline import get_plotlyjs
    encoded = json.dumps(document, ensure_ascii=True, allow_nan=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    replacements = {"__TITLE__": html.escape(document["file"]),
                    "__DATA__": encoded, "__PLOTLY__": get_plotlyjs()}
    # One pass: literal placeholder text in a filename/data field must not
    # trigger a second substitution inside the embedded JSON.
    page = re.sub(r"__TITLE__|__DATA__|__PLOTLY__", lambda m: replacements[m.group()], HTML_TEMPLATE)
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(page, encoding="utf-8")
    if open_browser:
        webbrowser.open(destination.as_uri())
    return destination


def default_output(path):
    path = Path(path)
    result = path.with_name(path.stem + "_viewer.html")
    number = 2
    while result.exists():
        result = path.with_name(f"{path.stem}_viewer_{number}.html")
        number += 1
    return result


def launch_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("880x570")
    root.minsize(730, 530)
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    style.configure("TButton", padding=8)
    style.configure("Title.TLabel", font=("Segoe UI", 19, "bold"))
    style.configure("TLabel", font=("Segoe UI", 10))
    frame = ttk.Frame(root, padding=24)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)
    ttk.Label(frame, text="Shaker CSV Viewer", style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
    ttk.Label(frame, text="CSU15-45N force sensor · Tektronix MSO4104B · recorded motor position").grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 20))
    path_var, format_var = tk.StringVar(), tk.StringVar(value=FORMATS[0])
    rating_var, baseline_var = tk.StringVar(value="45"), tk.StringVar(value="255")
    baseline_mode = tk.StringVar(value="Factory baseline (255)")
    status_var = tk.StringVar(value="Choose force_sensor.csv or oscilloscope.csv from a saved session.")
    out_var = tk.StringVar()
    def browse():
        path = filedialog.askopenfilename(title="Select saved CSV", filetypes=[("CSV files", "*.csv *.CSV"), ("All files", "*.*")])
        if path:
            path_var.set(path)
            out_var.set(str(default_output(path)))
    def choose_output():
        path = filedialog.asksaveasfilename(title="Save interactive plot", defaultextension=".html", filetypes=[("HTML plot", "*.html")], initialfile=Path(out_var.get()).name or "shaker_viewer.html")
        if path:
            out_var.set(path)
    for row, text in [(2, "CSV file:"), (3, "CSV format:"), (4, "Output HTML:")]:
        ttk.Label(frame, text=text).grid(row=row, column=0, sticky="w", pady=8, padx=(0, 15))
    ttk.Entry(frame, textvariable=path_var).grid(row=2, column=1, sticky="ew")
    ttk.Button(frame, text="Browse CSV…", command=browse).grid(row=2, column=2, padx=(10, 0))
    format_combo = ttk.Combobox(frame, textvariable=format_var, values=FORMATS, state="readonly", width=35)
    format_combo.grid(row=3, column=1, sticky="w")
    ttk.Entry(frame, textvariable=out_var).grid(row=4, column=1, sticky="ew")
    ttk.Button(frame, text="Save to…", command=choose_output).grid(row=4, column=2, padx=(10, 0))
    legacy = ttk.LabelFrame(frame, text="Count-only legacy files — ignored when Force (N) is present", padding=12)
    legacy.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(18, 12))
    ttk.Label(legacy, text="Rating (N):").grid(row=0, column=0, sticky="w")
    rating_entry = ttk.Entry(legacy, textvariable=rating_var, width=9)
    rating_entry.grid(row=0, column=1, padx=10)
    ttk.Label(legacy, text="Zero reference:").grid(row=0, column=2)
    baseline_combo = ttk.Combobox(legacy, textvariable=baseline_mode, values=["Factory baseline (255)", "Custom baseline", "First 1 s median (unloaded only)"], state="readonly", width=31)
    baseline_combo.grid(row=0, column=3, padx=10)
    ttk.Label(legacy, text="Baseline counts:").grid(row=1, column=0, pady=(10, 0))
    baseline_entry = ttk.Entry(legacy, textvariable=baseline_var, width=9)
    baseline_entry.grid(row=1, column=1, padx=10, pady=(10, 0))
    ttk.Label(legacy, text="45 N default. No automatic tare of saved force.").grid(row=1, column=2, columnspan=2, sticky="w", pady=(10, 0))
    def fields_changed(*_):
        enabled = format_var.get() in ("Auto detect", "Legacy force counts")
        rating_entry.configure(state="normal" if enabled else "disabled")
        baseline_combo.configure(state="readonly" if enabled else "disabled")
        baseline_entry.configure(state="normal" if enabled and baseline_mode.get() == "Custom baseline" else "disabled")
    format_var.trace_add("write", fields_changed)
    baseline_mode.trace_add("write", fields_changed)
    fields_changed()
    ttk.Label(frame, text="The plot opens in your browser and works offline. Scope captures can be selected individually.\nRaw + filtered traces, peak metrics, X/Y zoom, axis controls and two-click time measurement.", wraplength=790, justify="left").grid(row=6, column=0, columnspan=3, sticky="w", pady=10)
    results = queue.Queue()
    def create():
        path = Path(path_var.get().strip()).expanduser()
        if not path.is_file():
            messagebox.showerror(APP_TITLE, "Choose an existing CSV file.", parent=root)
            return
        output = Path(out_var.get().strip()).expanduser() if out_var.get().strip() else default_output(path)
        if output.resolve() == path.resolve() or output.suffix.lower() not in (".html", ".htm"):
            messagebox.showerror(APP_TITLE, "Choose a separate .html output file.", parent=root)
            return
        if output.exists() and not messagebox.askyesno(APP_TITLE, f"Replace existing plot?\n{output}", parent=root):
            return
        # Read Tk variables on the main thread only.
        args = (path, format_var.get(), rating_var.get(), baseline_mode.get(), baseline_var.get())
        plot_button.configure(state="disabled")
        status_var.set("Reading samples and building the interactive plot…")
        def worker():
            try:
                doc = load_csv(*args)
                result = write_plot(doc, output, open_browser=False)
                results.put((True, result, doc))
            except Exception as exc:
                results.put((False, str(exc), None))
        threading.Thread(target=worker, daemon=True).start()
        root.after(100, poll)
    def poll():
        try:
            ok, value, document = results.get_nowait()
        except queue.Empty:
            root.after(100, poll)
            return
        plot_button.configure(state="normal")
        if ok:
            webbrowser.open(value.as_uri())
            status_var.set(f"Created {value.name}\n{document['format']} · {len(document['records'])} selectable signals/captures")
            if document["warnings"]:
                messagebox.showwarning("Some signals/captures could not be loaded", "\n".join(document["warnings"][:8]), parent=root)
        else:
            status_var.set("Could not create plot. Check the message and CSV format.")
            messagebox.showerror(APP_TITLE, value, parent=root)
    plot_button = ttk.Button(frame, text="PLOT", command=create)
    plot_button.grid(row=7, column=0, columnspan=3, pady=12, ipadx=60, ipady=4)
    ttk.Label(frame, textvariable=status_var, wraplength=790, justify="left").grid(row=8, column=0, columnspan=3, sticky="w", pady=8)
    root.mainloop()


HTML_TEMPLATE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shaker CSV Viewer — __TITLE__</title>
<style>
:root{font-family:Segoe UI,Arial,sans-serif;color:#17263c;background:#f3f6fa;font-size:14px}*{box-sizing:border-box}body{margin:0;padding:22px;max-width:1680px;margin-inline:auto}header{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:16px}h1{font-size:26px;margin:0 0 5px}h2{font-size:14px;margin:0 0 12px;color:#344b67}p{margin:5px 0}.muted{color:#617188;font-size:13px}.badge{background:#e6effc;color:#244f8a;padding:8px 12px;border-radius:20px;white-space:nowrap}.panel{padding:16px;border:1px solid #dce3ed;border-radius:10px;background:white;margin-bottom:12px}.row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.row+.row{margin-top:12px}label{font-weight:600;font-size:13px}select,input,button{font:inherit;border:1px solid #c5cfdf;border-radius:5px;padding:7px 9px;background:white;color:inherit}input[type=number]{width:120px}input[type=checkbox]{accent-color:#2368b0}button{cursor:pointer;background:#f7f9fc;font-weight:600}button:hover{background:#e6effc}button.primary{background:#205fa7;border-color:#205fa7;color:white}button:disabled{opacity:.5;cursor:default}select{max-width:100%}#record{min-width:280px;flex:1}#notice{white-space:pre-line;color:#80551a;background:#fff8ea;padding:11px 14px;border-radius:8px;font-size:13px;margin-bottom:12px}#record-note{margin-top:9px;line-height:1.55}#filter-status{margin-top:9px}.controls{display:grid;grid-template-columns:1.1fr 1fr;gap:12px}.controls>.panel{margin-bottom:12px}#plot{height:540px;width:100%;min-width:0}.plot-panel{padding:5px 10px;display:grid;grid-template-columns:minmax(0,1fr) 58px;gap:4px}.y-scroll-panel{display:flex;align-items:center;flex-direction:column;justify-content:center;gap:8px;padding:12px 0}.y-scroll-panel label{font-size:11px;text-align:center}.y-scroll-panel button{padding:4px 10px}#y-scroll{writing-mode:vertical-lr;direction:rtl;appearance:auto;width:24px;height:330px;padding:0;accent-color:#2368b0;cursor:ns-resize}#y-position{font-size:10px;max-width:56px;overflow-wrap:anywhere;text-align:center}.peak-help{margin-top:8px;line-height:1.5}.stats-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;margin-top:10px;font-variant-numeric:tabular-nums}th,td{text-align:right;border-bottom:1px solid #e6ebf2;padding:10px 9px;white-space:nowrap}th:first-child,td:first-child{text-align:left}th{font-size:12px;color:#586d85;background:#f7f9fc}#measurement{white-space:pre-line;line-height:1.55}.bottom{display:grid;grid-template-columns:2fr 1fr;gap:12px}.explain{line-height:1.6}summary{cursor:pointer;font-weight:600}details p{margin-top:9px}#visible{font-variant-numeric:tabular-nums}@media(max-width:1050px){.controls,.bottom{grid-template-columns:1fr}body{padding:12px}#plot{height:480px}header{align-items:flex-start}.badge{display:none}}:focus-visible{outline:3px solid #95bfff;outline-offset:2px}
</style>
<script>__PLOTLY__</script></head><body>
<header><div><h1>Shaker CSV Viewer <span class="muted">1.2</span></h1><p id="file" class="muted"></p></div><span class="badge">OFFLINE ANALYSIS</span></header>
<div id="notice" role="status" hidden></div>
<section class="panel"><div class="row"><label for="channel" id="channel-label">Channel</label><select id="channel"></select><label for="record" id="record-label">Signal / capture</label><select id="record"></select><button id="previous">← Previous</button><button id="next">Next →</button></div><p id="record-note" class="muted"></p></section>
<div class="controls">
<section class="panel"><h2>FILTER & PEAK DETECTION</h2><div class="row"><label for="filter">Filter</label><select id="filter"><option value="none">None</option><option value="lowpass">Low-pass</option><option value="highpass">High-pass</option><option value="bandpass">Band-pass</option></select><label for="cut1" id="cut-label">Cutoff (Hz)</label><input id="cut1" type="number" value="5" min="0" step="any"><span id="upper-wrap" hidden><label for="cut2">Upper (Hz)</label> <input id="cut2" type="number" value="20" min="0" step="any"></span><button id="apply-filter" class="primary">Apply filter</button><button id="reset-filter">Reset</button></div>
<div class="row"><label for="applied-frequency">Applied frequency (Hz)</label><input id="applied-frequency" type="number" min="0" step="any" placeholder="Auto" aria-describedby="peak-help"><label for="period-tolerance">Period tolerance (%)</label><input id="period-tolerance" type="number" min="0" max="45" step="1" value="10"><label for="noise-threshold">Noise threshold (% range)</label><input id="noise-threshold" type="number" min="0.1" max="50" step="0.5" value="5"><button id="apply-peaks" class="primary">Update peaks</button></div>
<p class="muted peak-help" id="peak-help">Enter the shaker frequency for one max/min pair per accepted complete cycle. With fewer than 4 saved samples per expected cycle, markers show observed extrema only. Blank or 0 = automatic detection.</p>
<p id="peak-status" role="status" class="peak-help" style="color:#80551a;background:#fff8ea;padding:10px;border-radius:6px" hidden></p>
<div class="row"><label for="distance">Auto: min peak spacing (s)</label><input id="distance" type="number" min="0" step="any" value="0.08"><label for="threshold">Auto: level threshold (× SD)</label><input id="threshold" type="number" min="0" step="any" value="0.35"></div>
<div class="row"><label><input id="show-raw" type="checkbox" checked> Raw</label><label><input id="show-filtered" type="checkbox" checked> Filtered</label><label><input id="show-max" type="checkbox" checked> Max markers</label><label><input id="show-min" type="checkbox" checked> Min markers</label></div><p id="filter-status" class="muted"></p></section>
<section class="panel"><h2>VIEW & AXES</h2><div class="row"><label for="x-start">X start (s)</label><input id="x-start" type="number" step="any"><label for="x-end">X end (s)</label><input id="x-end" type="number" step="any"><button id="apply-x">Apply X</button><button id="reset-x">Full X</button></div>
<div class="row"><label for="y-center">Y center</label><input id="y-center" type="number" step="any"><label for="y-span">Y span</label><input id="y-span" type="number" step="any" min="0"><button id="apply-y">Apply Y</button><button id="reset-y">Fit Y</button></div>
<div class="row"><label for="drag">Mouse drag</label><select id="drag"><option value="zoom">Box zoom (X + Y)</option><option value="pan">Pan</option></select><button id="reset-view">Reset view</button><button id="export">Export visible CSV</button></div><p class="muted" style="margin-top:10px">Scroll over the plot to zoom. The right-side slider, arrows or wheel move the Y view without changing its span. Use the bottom slider for time selection. Fit Y includes all visible waveform traces.</p></section>
</div>
<section class="panel plot-panel"><div id="plot" aria-label="Interactive raw and filtered waveform"></div><aside class="y-scroll-panel" aria-label="Vertical plot navigation"><label for="y-scroll">Y position</label><button id="y-up" title="Move view up" aria-label="Move view up">▲</button><input id="y-scroll" type="range" min="0" max="1000" step="any" value="500" aria-label="Vertical view position"><button id="y-down" title="Move view down" aria-label="Move view down">▼</button><output id="y-position"></output><button id="y-fit" title="Fit visible waveforms vertically">Fit</button></aside></section>
<div class="bottom"><section class="panel"><h2>VISIBLE WINDOW METRICS</h2><p id="visible" class="muted"></p><div class="stats-wrap"><table><thead><tr><th>Signal</th><th>Detected freq. (Hz)</th><th>Avg. peak max</th><th>Avg. trough min</th><th>Absolute max</th><th>Absolute min</th><th>RMS</th></tr></thead><tbody id="stats"></tbody></table></div><p id="peak-note" class="muted" style="margin-top:10px"></p></section>
<section class="panel"><h2>TWO-POINT MEASUREMENT</h2><div class="row"><label for="measure-trace">Measure on</label><select id="measure-trace"><option value="1">Filtered</option><option value="0">Raw</option></select><button id="clear-measurement">Clear</button></div><p id="measurement"></p></section></div>
<details class="panel explain"><summary>How values and filters are calculated</summary>
<p>Force (N) columns are plotted exactly as saved, including the acquisition software’s calibration and manual tare. The 45 N count conversion is used only for count-only legacy files. Motor position remains in encoder counts.</p>
<p>Low-pass and high-pass are causal first-order RC-style IIR filters, using the actual interval between samples. Band-pass applies high-pass then low-pass. These match the filter family in your previous viewer; they are not Butterworth or zero-phase filters. They change phase/amplitude and have startup transients. Filtering is applied to the whole selected recording/capture before visible-window analysis. Large gaps restart the filter.</p>
<p>With an applied frequency f, expected period T = 1/f. Rising crossings through a small hysteresis band define candidate cycles. The band is centered between the 2nd and 98th percentile signal levels, and its total width is the Noise threshold percentage of that robust amplitude range. Only consecutive crossings separated by T within the selected period tolerance are accepted. One maximum and one minimum are taken from each complete accepted cycle, with flat extrema placed at a plateau midpoint. No assumption is made that the minimum occurs half a cycle after the maximum. Partial, missing or out-of-tolerance cycles are excluded, and detected frequency is calculated from measured accepted cycle durations, not copied from your input.</p>
<p>Sampling-limited mode: if the applied frequency is below Nyquist but there are fewer than 4 saved samples per expected cycle, strict per-cycle matching is suspended. Observed local maxima above mean + 0.35 × SD and intervening minima below mean − 0.35 × SD are marked, using the selected amplitude noise threshold to reject shallow troughs. No extra spacing rule is imposed. The detected frequency is N/A because these extrema have not been validated as cycles of the entered frequency. Their averages describe saved sample values, not reconstructed true peaks. Period tolerance has no effect in this mode. The 4-sample boundary is a viewer heuristic, not a guarantee of accurate amplitudes above it. Use a denser saved waveform for reliable cycle extrema; filters cannot restore missing samples.</p>
<p>Blank or zero applied frequency uses automatic local-maximum detection with the spacing and standard-deviation controls. Its minima must also lie below the mean − level threshold × SD and have sufficient drop from both adjacent maxima. The amplitude threshold helps reject the upper-shoulder minima produced by small extra peaks. Both methods operate on the complete selected capture; zoom changes which detected extrema and intervals contribute to visible metrics. Absolute extrema and RMS still use all visible samples. A wrong applied frequency may produce no accepted cycles; inspect the markers and the matching-cycle count.</p>
<p>Each scope capture has its own waveform time axis. Capture Time is a PC retrieval timestamp, not a hardware trigger time. Captures are never concatenated; this viewer does not infer force/voltage phase alignment. A constant CSV voltage stays constant: missing waveform information cannot be reconstructed.</p>
<p>Cursor frequency 1/Δt is the waveform frequency only when the two selected points are one full cycle apart. The third click starts a new pair. Switching signal/capture or changing filters clears cursors. Visible CSV export contains every sample in the visible X window, with original time, raw and filtered values.</p>
</details>
<script id="viewer-data" type="application/json">__DATA__</script>
<script>
'use strict';
const DOC = JSON.parse(document.getElementById('viewer-data').textContent);
const $ = id => document.getElementById(id);
const gd = $('plot');
let recordIndex=0, record, x=[], raw=[], filtered=[], x0=0, x1=1, cursors=[], peakDistance=.08, threshold=.35;
let filterDescription='None', busy=false, initialized=false, updateTimer;
let appliedFrequency=0, periodTolerance=.10, noiseThreshold=.05, detectionCache=null;
let yScrollLimits=null, yScrollSpan=null, yPanPending=null, yPanRunning=false;
function mean(a){if(!a.length)return NaN;let s=0;for(const v of a)s+=v;return s/a.length;}
function extent(a){let lo=Infinity,hi=-Infinity;for(const v of a){if(v<lo)lo=v;if(v>hi)hi=v;}return [lo,hi];}
function format(v){return Number.isFinite(v)?Number(v.toPrecision(7)).toString():'N/A';}
function numericInput(id){return $(id).value.trim()===''?NaN:Number($(id).value);}
function report(message=''){const notes=[...DOC.warnings];if(message)notes.push(message);$('notice').textContent=notes.join('\n');$('notice').hidden=!notes.length;}
function gapAt(i){return i>0 && x[i]-x[i-1]>5*record.medianDt;}
function lowPass(signal,time,fc,dtMedian){const out=new Array(signal.length);out[0]=signal[0];const rc=1/(2*Math.PI*fc);for(let i=1;i<signal.length;i++){const dt=time[i]-time[i-1];out[i]=dt>5*dtMedian?signal[i]:out[i-1]+dt/(rc+dt)*(signal[i]-out[i-1]);}return out;}
function highPass(signal,time,fc,dtMedian){const out=new Array(signal.length);out[0]=0;const rc=1/(2*Math.PI*fc);for(let i=1;i<signal.length;i++){const dt=time[i]-time[i-1];out[i]=dt>5*dtMedian?0:rc/(rc+dt)*(out[i-1]+signal[i]-signal[i-1]);}return out;}
function bound(a,value,upper=false){let lo=0,hi=a.length;while(lo<hi){const mid=(lo+hi)>>>1;if(a[mid]<value||(upper&&a[mid]===value))lo=mid+1;else hi=mid;}return lo;}
function visibleBounds(){return [bound(x,Math.min(x0,x1)),bound(x,Math.max(x0,x1),true)];}
// Detection is cached for the whole capture. Zoom never changes its cycle phase.
function robustLevels(yy){
 const sorted=yy.slice().sort((a,b)=>a-b),n=sorted.length;
 if(!n)return {lo:NaN,hi:NaN,span:0,mid:NaN};
 let lo=sorted[Math.floor(.02*(n-1))],hi=sorted[Math.ceil(.98*(n-1))];
 if(hi===lo){lo=sorted[0];hi=sorted[n-1];}
 return {lo,hi,span:hi-lo,mid:(hi+lo)/2};
}
function extremeIndex(yy,a,b,isMax){
 // Longest contiguous plateau wins; never place a marker between disjoint ties.
 let value=isMax?-Infinity:Infinity,start=a,bestStart=a,bestLength=0;
 for(let i=a;i<b;i++){
  const better=isMax?yy[i]>value:yy[i]<value;
  if(better){value=yy[i];start=i;bestStart=i;bestLength=1;}
  else if(yy[i]===value){if(i===a||yy[i-1]!==value)start=i;const length=i-start+1;if(length>bestLength){bestStart=start;bestLength=length;}}
 }
 return bestStart+Math.floor((bestLength-1)/2);
}
function detectPeaks(tx,yy,minSpacing,sdFactor,dtMedian,settings){
 const out={maxIdx:[],minIdx:[],intervals:[],rejected:0,reason:'',guided:settings.frequency>0,samplingLimited:false};
 if(settings.frequency>0){
  if(settings.frequency>=.5/dtMedian){out.reason='Applied frequency is at/above this signal’s median-based Nyquist limit.';return out;}
  const samplesPerCycle=1/(settings.frequency*dtMedian);
  // Sparse samples cannot support the strict crossing-period test reliably.
  // Keep observable extrema, but never label them verified cycles or copy f.
  if(samplesPerCycle<4-1e-9){
   const observed=detectPeaks(tx,yy,0,.35,dtMedian,{...settings,frequency:0});
   return {...observed,guided:true,samplingLimited:true,intervals:[],reason:
    `Sampling limited: ${format(1/dtMedian)} saved samples/s gives only ${format(samplesPerCycle)} samples per expected cycle at ${format(settings.frequency)} Hz. Showing observed extrema; they are not verified cycle peaks. Detected frequency is N/A. Save a denser waveform to resolve cycle peaks. Period tolerance is not used in this mode.`+(observed.reason?' '+observed.reason:'')};
  }
 }
 if(yy.length<3)return out;
 const levels=robustLevels(yy);
 if(!(levels.span>0)){out.reason='Constant signal: no cycles.';return out;}
 const amplitudeThreshold=settings.noise*levels.span;
 if(settings.frequency>0){
  const T=1/settings.frequency,lowerPeriod=T*(1-settings.tolerance),upperPeriod=T*(1+settings.tolerance);
  const low=levels.mid-amplitudeThreshold/2,high=levels.mid+amplitudeThreshold/2;
  const crossings=[];let armed=yy[0]<=low,segment=0;
  for(let i=1;i<yy.length;i++){
   if(tx[i]-tx[i-1]>5*dtMedian){segment++;armed=yy[i]<=low;continue;}
   if(yy[i]<=low)armed=true;
   if(armed&&yy[i]>=high&&yy[i]>yy[i-1]){
    // Interpolate the threshold crossing only; extrema remain actual samples.
    const time=tx[i-1]+(high-yy[i-1])/(yy[i]-yy[i-1])*(tx[i]-tx[i-1]);
    crossings.push({index:i,time,segment});armed=false;
   }
  }
  for(let k=1;k<crossings.length;k++){
   const a=crossings[k-1],b=crossings[k],period=b.time-a.time;
   if(a.segment!==b.segment)continue;
   const eps=1e-10*T;
   if(period<lowerPeriod-eps||period>upperPeriod+eps||b.index-a.index<3){out.rejected++;continue;}
   // Do not skip an intermediate crossing to manufacture the requested period.
   const imax=extremeIndex(yy,a.index,b.index,true),imin=extremeIndex(yy,a.index,b.index,false);
   if(yy[imax]<high||yy[imin]>low||yy[imax]-yy[imin]<amplitudeThreshold){out.rejected++;continue;}
   out.maxIdx.push(imax);out.minIdx.push(imin);out.intervals.push({a:a.time,b:b.time,duration:period});
  }
  if(!out.intervals.length)out.reason=`No complete cycles match the applied frequency and tolerance. Check frequency, threshold or capture duration. Saved samples/cycle: ${format(1/(settings.frequency*dtMedian))}.`;
  return out;
 }
 const average=mean(yy);let variance=0;for(const v of yy)variance+=(v-average)**2;
 const sd=Math.sqrt(variance/yy.length),limit=average+sdFactor*sd,lowLimit=average-sdFactor*sd,peaks=[];
 for(let i=1;i<yy.length-1;i++){
  if(tx[i]-tx[i-1]>5*dtMedian||yy[i]<=yy[i-1]||yy[i]<limit)continue;
  let j=i;while(j+1<yy.length&&yy[j+1]===yy[i]&&tx[j+1]-tx[j]<=5*dtMedian)j++;
  if(j+1<yy.length&&tx[j+1]-tx[j]<=5*dtMedian&&yy[j]>yy[j+1])peaks.push(Math.floor((i+j)/2));i=j;
 }
 const kept=[];
 for(const i of peaks){if(!kept.length||tx[i]-tx[kept[kept.length-1]]>=minSpacing)kept.push(i);else if(yy[i]>yy[kept[kept.length-1]])kept[kept.length-1]=i;}
 out.maxIdx=kept;
 for(let k=1;k<kept.length;k++){
  const a=kept[k-1],b=kept[k];let broken=false;
  for(let j=a+1;j<=b;j++)if(tx[j]-tx[j-1]>5*dtMedian){broken=true;break;}
  if(broken)continue;
  const trough=extremeIndex(yy,a,b+1,false);
  // Reject shoulder dips: require a low level and a drop from BOTH maxima.
  if(trough>a&&trough<b&&yy[trough]<=lowLimit&&Math.min(yy[a],yy[b])-yy[trough]>=amplitudeThreshold){
   out.minIdx.push(trough);out.intervals.push({a:tx[a],b:tx[b],duration:tx[b]-tx[a]});
  }else out.rejected++;
 }
 return out;
}
function peakOptions(){return {frequency:appliedFrequency,tolerance:periodTolerance,noise:noiseThreshold};}
function detections(){
 if(!detectionCache)detectionCache={raw:detectPeaks(x,raw,peakDistance,threshold,record.medianDt,peakOptions()),filtered:detectPeaks(x,filtered,peakDistance,threshold,record.medianDt,peakOptions())};
 return detectionCache;
}
function metrics(tx,yy,minSpacing,sdFactor,dtMedian,detected=null,offset=0){
 const empty={frequency:NaN,avgMax:NaN,avgMin:NaN,max:NaN,min:NaN,rms:NaN,maxIdx:[],minIdx:[],periods:0};
 if(!yy.length)return empty;
 const [lo,hi]=extent(yy);let square=0;for(const v of yy)square+=v*v;
 const out={...empty,min:lo,max:hi,rms:Math.sqrt(square/yy.length)};
 const d=detected||detectPeaks(tx,yy,minSpacing,sdFactor,dtMedian,peakOptions());
 out.maxIdx=d.maxIdx.filter(i=>i>=offset&&i<offset+yy.length).map(i=>i-offset);
 out.minIdx=d.minIdx.filter(i=>i>=offset&&i<offset+yy.length).map(i=>i-offset);
 const durations=d.intervals.filter(p=>p.a>=tx[0]&&p.b<=tx[tx.length-1]).map(p=>p.duration);
 out.avgMax=mean(out.maxIdx.map(i=>yy[i]));out.avgMin=mean(out.minIdx.map(i=>yy[i]));
 out.periods=durations.length;out.frequency=durations.length?1/mean(durations):NaN;return out;
}
function displayData(signal){
 // Preserve every sample and explicitly break lines at recording gaps.
 const tx=[],ty=[],extra=[];
 for(let i=0;i<x.length;i++){if(gapAt(i)){tx.push(null);ty.push(null);extra.push('');}tx.push(x[i]);ty.push(signal[i]);extra.push(record.states?record.states[i]:'');}
 return {x:tx,y:ty,customdata:extra};
}
function updateMetrics(){
 const [a,b]=visibleBounds(),tx=x.slice(a,b),yr=raw.slice(a,b),yf=filtered.slice(a,b);
 const d=detections();
 const rm=metrics(tx,yr,peakDistance,threshold,record.medianDt,d.raw,a),fm=metrics(tx,yf,peakDistance,threshold,record.medianDt,d.filtered,a);
 const rows=[['Raw',rm],['Filtered',fm]];$('stats').replaceChildren();
 for(const [name,m] of rows){const tr=document.createElement('tr');for(const v of [name,format(m.frequency),format(m.avgMax),format(m.avgMin),format(m.max),format(m.min),format(m.rms)]){const cell=document.createElement('td');cell.textContent=v;tr.appendChild(cell);}$('stats').appendChild(tr);}
 $('visible').textContent=`${format(x0)} → ${format(x1)} s · ${b-a} samples · amplitude unit: ${record.unit}`;
 const limited=d.filtered.samplingLimited;
 const guidance=limited?`Sampling-limited observed extrema · requested ${format(appliedFrequency)} Hz. Average peaks/troughs are saved sample values, not verified per-cycle extrema.`:appliedFrequency>0?`Guided by ${format(appliedFrequency)} Hz · expected T=${format(1/appliedFrequency)} s ±${format(100*periodTolerance)}%. Detected frequency uses measured cycle times.`:'Automatic detection · frequency uses intervals between maxima with accepted troughs.';
 $('peak-note').textContent=`${guidance} Visible max/min: raw ${rm.maxIdx.length}/${rm.minIdx.length}, filtered ${fm.maxIdx.length}/${fm.minIdx.length}. ${limited?'No frequency-validated cycles. ':`Complete visible intervals: raw ${rm.periods}, filtered ${fm.periods}. `}${appliedFrequency>0&&!limited?'Filtered: '+d.filtered.intervals.length+' accepted cycles and '+d.filtered.rejected+' rejected intervals in the full capture. ':''}${d.filtered.reason} Markers refer to the filtered signal.`;
 // Persistent diagnostics beside Update peaks, independent of transient errors.
 const messages=d.raw.reason===d.filtered.reason?[d.filtered.reason]:[d.raw.reason?'Raw: '+d.raw.reason:'',d.filtered.reason?'Filtered: '+d.filtered.reason:''];
 $('peak-status').textContent=messages.filter(Boolean).join(' ');$('peak-status').hidden=!$('peak-status').textContent;
 $('filter-status').textContent=`Applied: ${filterDescription}. Saved-data sample rate (median): ${format(1/record.medianDt)} Hz; Nyquist: ${format(.5/record.medianDt)} Hz.`+(appliedFrequency>0?` Saved samples per expected cycle: ${format(1/(record.medianDt*appliedFrequency))}.`:'');
 const maxX=fm.maxIdx.map(i=>tx[i]),maxY=fm.maxIdx.map(i=>yf[i]),minX=fm.minIdx.map(i=>tx[i]),minY=fm.minIdx.map(i=>yf[i]);
 return Plotly.restyle(gd,{x:[maxX,minX],y:[maxY,minY],visible:[$('show-max').checked&&$('show-filtered').checked,$('show-min').checked&&$('show-filtered').checked]},[2,3]);
}
function yRange(){return (gd._fullLayout||gd.layout).yaxis.range.map(Number);}
function syncYScroll(reset=false){
 const yr=yRange(),span=yr[1]-yr[0],center=(yr[0]+yr[1])/2;
 if(!(span>0)||!Number.isFinite(center))return;
 if(reset||!yScrollLimits||!yScrollSpan||Math.abs(span/yScrollSpan-1)>1e-6){
  const [rlo,rhi]=extent(raw),[flo,fhi]=extent(filtered);
  yScrollLimits=[Math.min(rlo,flo,center)-span,Math.max(rhi,fhi,center)+span];yScrollSpan=span;
 }else{
  if(center<yScrollLimits[0])yScrollLimits[0]=center-span;
  if(center>yScrollLimits[1])yScrollLimits[1]=center+span;
 }
 $('y-scroll').value=String(1000*(center-yScrollLimits[0])/(yScrollLimits[1]-yScrollLimits[0]));
 $('y-scroll').setAttribute('aria-valuetext',`Y center ${format(center)} ${record.unit}`);
 $('y-position').textContent=format(center)+' '+record.unit;
 $('y-center').value=format(center);$('y-span').value=format(span);
}
async function panY(center){
 if(!Number.isFinite(center))return;
 yPanPending=center;if(yPanRunning)return;yPanRunning=true;
 try{while(yPanPending!==null){const target=yPanPending;yPanPending=null;const yr=yRange(),span=yr[1]-yr[0];await Plotly.relayout(gd,{'yaxis.range':[target-span/2,target+span/2],'yaxis.autorange':false});syncYScroll();}}
 finally{yPanRunning=false;}
}
function shiftY(direction){const yr=yRange();return panY((yr[0]+yr[1])/2+direction*.1*(yr[1]-yr[0]));}
function syncInputs(){
 const xr=gd.layout.xaxis.range;
 if(xr&&xr.length===2){x0=Number(xr[0]);x1=Number(xr[1]);}
 $('x-start').value=format(x0);$('x-end').value=format(x1);syncYScroll();
}
async function fitY(){
 const [a,b]=visibleBounds();let lo=Infinity,hi=-Infinity;
 for(const signal of [raw,filtered]){if(signal===raw&&!$('show-raw').checked)continue;if(signal===filtered&&!$('show-filtered').checked)continue;for(let i=a;i<b;i++){lo=Math.min(lo,signal[i]);hi=Math.max(hi,signal[i]);}}
 if(!Number.isFinite(lo)){report('No visible waveform samples to fit.');return;}
 const span=hi-lo||Math.max(Math.abs(lo)*.1,1e-6),pad=.05*span;
 await Plotly.relayout(gd,{'yaxis.range':[lo-pad,hi+pad],'yaxis.autorange':false});syncInputs();
}
function filterUI(){const type=$('filter').value;$('upper-wrap').hidden=type!=='bandpass';$('cut-label').textContent=type==='bandpass'?'Lower (Hz)':'Cutoff (Hz)';$('cut1').disabled=type==='none';}
async function clearMeasurement(){cursors=[];$('measurement').textContent='Click two points on the selected waveform. Third click starts a new pair.';return Plotly.restyle(gd,{x:[[],[]],y:[[],[]]},[4,5]);}
async function applyFilter(){
 const type=$('filter').value,c1=numericInput('cut1'),c2=numericInput('cut2'),nyquist=.5/record.medianDt;
 if(type!=='none'&&(!Number.isFinite(c1)||c1<=0||c1>=nyquist))throw Error(`Cutoff must be greater than 0 and below the median-based Nyquist limit (${format(nyquist)} Hz).`);
 if(type==='bandpass'&&(!Number.isFinite(c2)||c2<=c1||c2>=nyquist))throw Error(`Upper cutoff must exceed lower cutoff and be below ${format(nyquist)} Hz.`);
 if(type==='none'){filtered=raw.slice();filterDescription='None';}
 if(type==='lowpass'){filtered=lowPass(raw,x,c1,record.medianDt);filterDescription=`First-order RC low-pass · ${format(c1)} Hz`;}
 if(type==='highpass'){filtered=highPass(raw,x,c1,record.medianDt);filterDescription=`First-order RC high-pass · ${format(c1)} Hz`;}
 if(type==='bandpass'){filtered=lowPass(highPass(raw,x,c1,record.medianDt),x,c2,record.medianDt);filterDescription=`Cascaded RC band-pass · ${format(c1)}–${format(c2)} Hz`;}
 if(!filtered.every(Number.isFinite))throw Error('Filter overflow: inspect signal values and cutoff.');
 const data=displayData(filtered);await Plotly.restyle(gd,{x:[data.x],y:[data.y],customdata:[data.customdata]},[1]);
 detectionCache=null;await clearMeasurement();await updateMetrics();syncYScroll(true);report();
}
function peakUI(){
 const guided=numericInput('applied-frequency')>0;
 $('distance').disabled=guided;$('threshold').disabled=guided;$('period-tolerance').disabled=!guided;
}
async function peakSettings(){
 const text=$('applied-frequency').value.trim(),f=text===''?0:Number(text),t=numericInput('period-tolerance'),n=numericInput('noise-threshold');
 if(!Number.isFinite(f)||f<0)throw Error('Applied frequency must be positive, or blank/0 for automatic detection.');
 if(f>0&&f>=.5/record.medianDt)throw Error('Applied frequency must be below the median-based Nyquist limit.');
 if(!Number.isFinite(t)||t<0||t>45)throw Error('Period tolerance must be between 0 and 45%.');
 if(!Number.isFinite(n)||n<=0||n>50)throw Error('Noise threshold must be greater than 0 and at most 50% of the robust range.');
 if(f===0){const d=numericInput('distance'),k=numericInput('threshold');if(!Number.isFinite(d)||d<0||!Number.isFinite(k)||k<0)throw Error('Peak spacing and threshold must be finite numbers ≥ 0.');peakDistance=d;threshold=k;}
 appliedFrequency=f;periodTolerance=t/100;noiseThreshold=n/100;detectionCache=null;peakUI();await updateMetrics();report();
}
function recordOptions(){
 const channel=$('channel').value;$('record').replaceChildren();
 DOC.records.forEach((r,i)=>{if(!DOC.scope||channel==='All channels'||r.channel===channel){const option=document.createElement('option');option.value=String(i);option.textContent=r.label;$('record').appendChild(option);}});
}
function navState(){const i=$('record').selectedIndex;$('previous').disabled=i<=0;$('next').disabled=i>=$('record').options.length-1;}
async function loadRecord(){
 recordIndex=Number($('record').value);record=DOC.records[recordIndex];x=record.t;raw=record.y;filtered=raw.slice();x0=x[0];x1=x[x.length-1];cursors=[];peakDistance=record.peakDistance;threshold=.35;detectionCache=null;yScrollLimits=null;yScrollSpan=null;
 $('distance').value=String(peakDistance);$('threshold').value='0.35';$('filter').value='none';$('cut1').value=format(Math.min(5,.1/record.medianDt));$('cut2').value=format(Math.min(20,.2/record.medianDt));filterDescription='None';filterUI();peakUI();
 $('record-note').textContent=`${record.note} Samples: ${x.length.toLocaleString()}.`;navState();report();
 const rawData=displayData(raw),filteredData=displayData(filtered);
 const tooltip='Time: %{x:.7g} s<br>Value: %{y:.7g} '+record.unit+(record.states?'<br>Motor: %{customdata}':'')+'<extra>%{fullData.name}</extra>';
 const traces=[
 {...rawData,type:'scatter',mode:'lines',name:'Raw',visible:$('show-raw').checked,line:{color:'#8da6c4',width:1.5},hovertemplate:tooltip,connectgaps:false},
 {...filteredData,type:'scatter',mode:'lines',name:'Filtered',visible:$('show-filtered').checked,line:{color:'#146caa',width:2},hovertemplate:tooltip,connectgaps:false},
 ...[['Max peaks','#dc4c64','circle'],['Min troughs','#209477','triangle-down'],['Cursor 1','#e88c18','x'],['Cursor 2','#8a54b2','x']].map(([name,color,symbol])=>({x:[],y:[],type:'scatter',mode:'markers',name,showlegend:false,hoverinfo:'skip',marker:{color,symbol,size:10}}))];
 const layout={title:{text:record.label,font:{size:16}},paper_bgcolor:'#ffffff',plot_bgcolor:'#ffffff',margin:{l:80,r:30,t:72,b:45},hovermode:'closest',dragmode:$('drag').value,showlegend:true,legend:{orientation:'h',x:0,y:1.11,itemclick:false,itemdoubleclick:false},xaxis:{title:{text:DOC.scope?'Waveform time (s)':'Time (s)'},range:[x0,x1],autorange:false,rangeslider:{visible:true,thickness:.13},fixedrange:false,gridcolor:'#e8edf4'},yaxis:{title:{text:record.unit},fixedrange:false,gridcolor:'#e8edf4'},uirevision:recordIndex};
 busy=true;
 try{
  if(initialized)await Plotly.react(gd,traces,layout,{scrollZoom:true,responsive:true,displaylogo:false});
  else{await Plotly.newPlot(gd,traces,layout,{scrollZoom:true,responsive:true,displaylogo:false});initialized=true;bindPlot();}
  await applyFilter();await fitY();syncInputs();
 }finally{busy=false;}
}
function bindPlot(){
 gd.on('plotly_relayout',event=>{
  if(busy)return;
  const changesX=Object.keys(event).some(k=>k.startsWith('xaxis.range')||k==='xaxis.autorange');
  if(changesX){
   if(event['xaxis.autorange']===true){x0=x[0];x1=x[x.length-1];}
   else if(event['xaxis.range']){[x0,x1]=event['xaxis.range'].map(Number);}
   else{if(event['xaxis.range[0]']!==undefined)x0=Number(event['xaxis.range[0]']);if(event['xaxis.range[1]']!==undefined)x1=Number(event['xaxis.range[1]']);}
   $('x-start').value=format(x0);$('x-end').value=format(x1);
   clearTimeout(updateTimer);updateTimer=setTimeout(()=>updateMetrics().catch(e=>report(e.message)),70);
  }
  if(Object.keys(event).some(k=>k.startsWith('yaxis.')))syncYScroll();
 });
 gd.on('plotly_click',event=>{
  const candidates=(event.points||[]).filter(p=>p.curveNumber===0||p.curveNumber===1);if(!candidates.length)return;
  const target=Number($('measure-trace').value);if(!$(target===0?'show-raw':'show-filtered').checked){report('Show the waveform selected under Measure on before measuring.');return;}
  const p=candidates.find(p=>p.curveNumber===target)||candidates[0],index=bound(x,Number(p.x));if(index>=x.length)return;
  const point={x:x[index],y:(target===0?raw:filtered)[index]};if(cursors.length===2)cursors=[point];else cursors.push(point);
  const p1=cursors[0],p2=cursors[1];Plotly.restyle(gd,{x:[[p1.x],p2?[p2.x]:[]],y:[[p1.y],p2?[p2.y]:[]]},[4,5]);
  $('measurement').textContent=`${target===0?'Raw':'Filtered'} · Point 1: ${format(p1.x)} s, ${format(p1.y)} ${record.unit}`;
  if(p2){const dt=Math.abs(p2.x-p1.x);$('measurement').textContent+=`\nPoint 2: ${format(p2.x)} s, ${format(p2.y)} ${record.unit}\nΔt = ${format(dt)} s · Δy = ${format(p2.y-p1.y)} ${record.unit}\n1/Δt = ${dt>0?format(1/dt)+' Hz':'N/A'} (one full cycle only)`;}else $('measurement').textContent+='\nClick the second point.';
 });
}
function on(id,event,callback){$(id).addEventListener(event,async()=>{if(busy)return;try{await callback();}catch(e){report(e.message);}});}
async function setX(a,b){if(!Number.isFinite(a)||!Number.isFinite(b)||b<=a)throw Error('X end must exceed X start.');x0=a;x1=b;await Plotly.relayout(gd,{'xaxis.range':[a,b],'xaxis.autorange':false});syncInputs();await updateMetrics();}
function exportVisible(){
 const [a,b]=visibleBounds();if(a===b)throw Error('No samples in the visible window.');
 const csvEscape=v=>'"'+String(v??'').replaceAll('"','""')+'"';
 const header=['Time (s)',`Raw (${record.unit})`,`Filtered (${record.unit})`,'Signal / capture','Applied filter','Applied frequency (Hz)','Period tolerance (%)','Noise threshold (% range)'];
 if(record.capture!==null){header.push('Capture Time (s)','Channel');}if(record.states)header.push('Motor State');
 const lines=[header.map(csvEscape).join(',')];for(let i=a;i<b;i++){const row=[x[i],raw[i],filtered[i],record.label,filterDescription,appliedFrequency||'Auto',100*periodTolerance,100*noiseThreshold];if(record.capture!==null)row.push(record.capture,record.channel);if(record.states)row.push(record.states[i]);lines.push(row.map(csvEscape).join(','));}
 const url=URL.createObjectURL(new Blob(['\uFEFF'+lines.join('\r\n')],{type:'text/csv;charset=utf-8;'}));const link=document.createElement('a');link.href=url;link.download=DOC.file.replace(/\.csv$/i,'')+'_visible_'+(recordIndex+1)+'.csv';document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
on('record','change',loadRecord);on('channel','change',async()=>{recordOptions();await loadRecord();});
on('previous','click',async()=>{$('record').selectedIndex--;await loadRecord();});on('next','click',async()=>{$('record').selectedIndex++;await loadRecord();});
on('applied-frequency','input',peakUI);
on('y-scroll','input',()=>{if(!yScrollLimits)return;return panY(yScrollLimits[0]+Number($('y-scroll').value)/1000*(yScrollLimits[1]-yScrollLimits[0]));});
on('y-up','click',()=>shiftY(1));on('y-down','click',()=>shiftY(-1));on('y-fit','click',fitY);
$('y-scroll').addEventListener('wheel',event=>{event.preventDefault();if(!busy&&event.deltaY)shiftY(event.deltaY<0?1:-1).catch(e=>report(e.message));},{passive:false});
on('filter','change',filterUI);on('apply-filter','click',applyFilter);on('reset-filter','click',async()=>{$('filter').value='none';filterUI();await applyFilter();});on('apply-peaks','click',peakSettings);
for(const id of ['show-max','show-min'])on(id,'change',updateMetrics);
for(const [id,index] of [['show-raw',0],['show-filtered',1]])on(id,'change',async()=>{await Plotly.restyle(gd,{visible:$(id).checked},[index]);await updateMetrics();});
on('apply-x','click',async()=>{await setX(numericInput('x-start'),numericInput('x-end'));report();});on('reset-x','click',()=>setX(x[0],x[x.length-1]));
on('apply-y','click',async()=>{const c=numericInput('y-center'),s=numericInput('y-span');if(!Number.isFinite(c)||!Number.isFinite(s)||s<=0)throw Error('Enter a finite Y center and a positive Y span.');await Plotly.relayout(gd,{'yaxis.range':[c-s/2,c+s/2],'yaxis.autorange':false});report();});
on('reset-y','click',fitY);on('reset-view','click',async()=>{await setX(x[0],x[x.length-1]);await fitY();});on('drag','change',()=>Plotly.relayout(gd,{dragmode:$('drag').value}));
on('clear-measurement','click',clearMeasurement);on('measure-trace','change',clearMeasurement);on('export','click',exportVisible);
$('file').textContent=DOC.file+' · '+DOC.format;
if(DOC.scope){for(const name of ['All channels',...new Set(DOC.records.map(r=>r.channel))]){const o=document.createElement('option');o.value=name;o.textContent=name;$('channel').appendChild(o);}}
else{$('channel').hidden=true;$('channel-label').hidden=true;$('previous').hidden=true;$('next').hidden=true;}
recordOptions();
loadRecord().catch(e=>report('Could not render plot: '+e.message));
</script></body></html>
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--format", choices=FORMATS, default="Auto detect")
    parser.add_argument("--rating", type=float, default=45.0)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    if args.csv:
        output = args.output or default_output(args.csv)
        if output.resolve() == args.csv.resolve() or output.suffix.lower() not in (".html", ".htm"):
            parser.error("Output must be a separate HTML file.")
        doc = load_csv(args.csv, args.format, args.rating)
        print(write_plot(doc, output, not args.no_open))
    else:
        launch_gui()


if __name__ == "__main__":
    main()
