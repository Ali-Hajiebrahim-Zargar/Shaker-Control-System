# Shaker CSV Viewer

**Version 1.3** · Standalone analysis of saved force, oscilloscope and motor-position recordings from this Shaker Control System project.

Browse to a CSV, click **PLOT**, and analyze its raw and filtered signals in an interactive browser plot. The viewer includes applied-frequency guidance for maximum/minimum detection, editable noise and timing tolerances, live metrics for the visible time window, vertical scrolling, and two-point measurements.

The current laboratory setup uses a **SingleTact CSU15-45N calibrated USB force sensor (45 N nominal range)** and a **Tektronix MSO4104B oscilloscope**. Motor-position values come from the FAULHABER/Arduino system described in the [acquisition and hardware guide](../CSU15-45N-USB/README.md).

[راهنمای فارسی و توضیح اصلاح نسخهٔ ۱٫۳](README_FA.md)

## 1. Installation and launch

### Windows

1. Download or clone this repository's **`csu15-45n-usb` branch** and open `CSV-Viewer/`. If you download a ZIP, extract it completely first.
2. Install **Python 3.10 or newer**, including its Tcl/Tk component.
3. Double-click **`INSTALL_REQUIREMENTS.bat`** once. Internet is required to install the Python dependencies.
4. Double-click **`START_VIEWER.bat`**.
5. Select `force_sensor.csv` or `oscilloscope.csv` from a recorded session.
6. Leave **CSV format** on **Auto detect**, choose an output path if needed, and click **PLOT**.

The launchers use `py -3` when the Windows Python launcher is available, otherwise `python`. Installation and launch should use the same Python environment.

### Command line

From this folder:

```bash
python -m pip install -r requirements.txt
python shaker_csv_viewer.py
```

Dependencies are **NumPy, pandas and Plotly**; Tkinter normally comes with the Windows Python installer. On Linux, Tkinter may require your distribution's `python3-tk` package. On macOS, use a Python distribution with working Tcl/Tk support.

No COM connection, Arduino, sensor or VISA driver is required to analyze existing files. After installation, generated HTML plots work offline because Plotly JavaScript and the data are embedded in the file.

### Updating an existing installation

Replace the viewer's source files, restart it, select the CSV and press **PLOT** again. **Previously generated HTML plots contain the older viewer code; opening one does not load new Python changes.** Version 1.3 is shown in the desktop title and plot heading.

## 2. What each file does

| File | Purpose |
|---|---|
| [`shaker_csv_viewer.py`](shaker_csv_viewer.py) | Complete desktop file selector, CSV readers, and embedded interactive HTML/JavaScript viewer |
| [`requirements.txt`](requirements.txt) | Python dependencies |
| [`INSTALL_REQUIREMENTS.bat`](INSTALL_REQUIREMENTS.bat) | Windows dependency installer |
| [`START_VIEWER.bat`](START_VIEWER.bat) | Windows application launcher |
| [`tests/test_csv_loading.py`](tests/test_csv_loading.py) | Synthetic CSV reader and calibration regression tests |
| [`tests/test_interactions.cjs`](tests/test_interactions.cjs) | Numerical and UI-event logic tests using a small Plotly/DOM adapter |
| [`.gitignore`](.gitignore) | Excludes local recordings, generated HTML, Python caches and virtual environments |

The Python file embeds the browser interface; it does not need an HTML template or JavaScript file beside it. Node.js is needed only for the optional JavaScript tests.

## 3. Supported CSV formats

### Current force export

```csv
Time (s),Raw Counts,Force (N),Motor Position (counts),Motor State
```

| Column | Interpretation |
|---|---|
| `Time (s)` | Force recording time in seconds |
| `Raw Counts` | Unconverted SingleTact digital counts |
| `Force (N)` | Force already calibrated by the acquisition application |
| `Motor Position (counts)` | Recorded relative encoder position; no conversion to displacement is assumed |
| `Motor State` | Recorded motor state, shown on waveform hover when available |

Select saved force, raw counts or motor position from the plot's **Signal / capture** selector.

**Saved `Force (N)` values are preserved exactly**, including the acquisition application's calibration and any manual tare. The viewer does not subtract the first second, apply another zero, or multiply saved Newton values by the sensor rating again.

### Current oscilloscope export

```csv
Capture Time (s),Channel,Sample Index,Waveform Time (s),Voltage (V)
```

| Column | Interpretation |
|---|---|
| `Capture Time (s)` | PC-relative waveform retrieval start; identifies the capture |
| `Channel` | Oscilloscope channel, such as `CH1` |
| `Sample Index` | Sample order within a capture |
| `Waveform Time (s)` | Actual time coordinates used for the waveform plot and frequency calculations |
| `Voltage (V)` | Saved voltage samples |

Choose **Channel** and **Signal / capture**, or use **Previous / Next**. Captures are separated by changes in capture time/channel and by sample-index resets. Repeated waveform timestamps in different captures are never merged into one waveform.

**Capture Time is not a hardware trigger timestamp.** It must not be used as the sample time within a capture. The viewer does not infer phase synchronization between scope and force recordings. It reads the samples present in the file; it does not retrieve more samples from the instrument or change acquisition behavior.

The schema above was checked against `CSVWriterWorker` in [`shaker_control_gui_CSU15_45N.py`](../CSU15-45N-USB/GUI/shaker_control_gui_CSU15_45N.py) at acquisition commit [`4bd1638`](https://github.com/Ali-Hajiebrahim-Zargar/Shaker-Control-System/commit/4bd1638ada10a1f6312e6a0403f9e95e18da3590).

### Older files

| Format | Expected content |
|---|---|
| Legacy combined project CSV | `time_s,piezo_voltage_V,force_N,position_counts,motor_state` |
| Count-only force CSV | A time column, plus `Counts` or `Raw Counts` |
| Legacy Tektronix CSV | Time in column 4, voltage in column 5; instrument metadata supports automatic identification |

If a legacy Tektronix file is not detected, explicitly select **Legacy Tektronix CSV**. This positional reader is for that five-column layout, not every possible manufacturer export.

Known named formats accept a UTF-8 BOM, up to 199 preamble lines before the header, comma/semicolon/tab separators, and decimal commas in semicolon-separated files. Explicitly named `Time (ms)` is converted to seconds. Otherwise, supported time columns are interpreted in seconds.

## 4. Count-only force conversion

The desktop's legacy conversion controls apply **only when raw counts are available and no saved `Force (N)` column is present**:

```text
Force_N = (raw_counts - baseline_counts) * sensor_rating_N / 512
```

Defaults:

- **Sensor rating:** 45 N.
- **Zero reference:** factory baseline of 255 counts, as used in the inspected project.
- **Custom baseline:** enter a known baseline in counts.
- **First 1 s median:** explicitly requested legacy zeroing; use only when that initial interval is unloaded.

The rating controls are ignored for saved Newton columns. They cannot correct earlier recordings made with the wrong acquisition calibration. Motor position remains in encoder counts.

## 5. Frequency-guided maximum/minimum detection

This feature works for **both force and oscilloscope waveforms**.

1. Enter the shaker's **Applied frequency (Hz)** in the browser controls.
2. Set **Period tolerance (%)**, initially **10**.
3. Set **Noise threshold (% range)**, initially **5**.
4. Click **Update peaks**.
5. Check **Applied guide: … Hz** beside the controls. **Active mode: AUTOMATIC** means no input frequency is active; **NOT APPLIED** means click Update peaks or press Enter. Then check the text beneath the metrics: **“Guided by … Hz”** means normal cycle matching; **“Sampling-limited observed extrema”** means the saved waveform is too sparse for that mode. Inspect the markers and diagnostics.

Changing a field alone does not apply the numerical settings. The entered frequency controls peak selection; it does not change the recorded waveform or drive the function generator.

### Sparse recordings and the version 1.3 correction

The reported screenshots contain about **2.5 saved samples/s**: **0.4 s between samples**, or only **2.5 samples per expected cycle** with a **1 Hz** guide. Version 1.1 used strict crossing-period matching that could reject every cycle. Version 1.2 retained automatic extrema in this case but disabled the frequency estimate. Its global-mean threshold could still reject all troughs when the capture included a different inactive baseline, while counting small shoulder maxima.

**Version 1.3 fixes those two automatic/sparse-mode problems:**

- A maximum is confirmed after the signal falls by a minimum amplitude; a new maximum requires a new rise of that amplitude. Weak shoulder reversals are therefore suppressed as a pair. Flat maxima use a midpoint of the most recently tracked tied plateau; troughs use the longest lowest plateau between retained maxima.
- The amplitude threshold is `max(noise_fraction * robust_range, SD_factor * population_SD)`. No absolute voltage/force level or `mean - SD` condition rejects a minimum. The robust range is the 2nd-to-98th percentile difference, with full range as a fallback.
- The minimum is the lowest saved value between consecutive retained maxima, provided it lies between them and its depth below both reaches the amplitude threshold. Intervals spanning recording gaps are excluded.
- Frequency is estimated from the **actual complete visible peak-to-peak intervals with resolved troughs**, even when sampling is sparse: `f = number_of_intervals / sum(interval_durations)`. It is labelled **Peak repetition (Hz)**. The input frequency is never copied into the result.

When a positive guide gives fewer than four saved samples per expected cycle, this is explicitly **sampling-limited mode**. It uses the selected noise threshold, a fixed `SD_factor = 0.35`, and no additional minimum spacing. The guide is compared with observed repetition; it does not impose artificial peaks. A full-capture discrepancy larger than the entered period tolerance plus an endpoint timing allowance (`2 * median_dt / interval_count`) is reported beside the controls. The timing allowance is a heuristic, not a statistical confidence interval. The same mode works for raw and filtered force/scope signals.

Four samples/cycle is also a **viewer heuristic**, not a sampling theorem or an amplitude-accuracy guarantee. Sparse amplitudes can miss true extrema, and aliasing or harmonics can affect observed repetition. The rate describes the **saved CSV timestamps**, not necessarily the oscilloscope's internal ADC. Neither a filter nor the guide restores unsaved values. Save a denser waveform for reliable cycle amplitudes.

In the latest screenshot, the visible status says **Automatic detection**, with 91 maxima and zero minima. It does not show an active 1 Hz guide. The large peaks appear roughly two seconds apart; retaining only those would give about **0.5 Hz**, not 1 Hz, if the time coordinates are correct. This is a visual observation, not a numerical diagnosis of the original CSV. The original file is needed to resolve the disagreement with the known excitation setting.

### Expected period and tolerance (normal guided mode)

```text
T_expected = 1 / applied_frequency
accepted duration = T_expected * (1 +/- tolerance_percent / 100)
```

For **2 Hz**, the expected period is **0.5 s**. A **10% period tolerance** accepts durations from **0.45 to 0.55 s**. The tolerance is a percentage of **period**, not a symmetric percentage of frequency. Settings from 0 to 45% are supported; applied frequency must be below the median-based Nyquist limit.

### Noise threshold and complete cycles

The detector finds the 2nd and 98th percentile amplitude levels in the complete selected signal. Their difference is its robust range; if they are equal, it falls back to the full range. A hysteresis band is centered between these levels, with a total width set by **Noise threshold**.

At the 5% default, the band extends 2.5% of the robust range above and below its midpoint. A rising crossing is registered only after the signal has first reached the lower level and then rises through the upper level. This suppresses small reversals around the crossing level.

Only **consecutive** crossings with an acceptable period form a valid cycle. For each accepted complete cycle, the viewer finds:

- **One maximum** from the recorded samples within that cycle.
- **One minimum** from the same cycle.
- A midpoint within the longest tied plateau if the extreme is flat.

The minimum is not assumed to occur half a period after the maximum. This supports asymmetric and quantized waveforms and avoids selecting a small upper-shoulder dip simply because two nearby local maxima were found.

Partial cycles, intervals across recording gaps, and durations outside the selected tolerance are excluded. The algorithm does not skip intermediate crossings merely to manufacture the entered period. Excessive noise, baseline drift, complex multi-lobed waveforms or a wrong applied frequency can therefore result in rejected intervals or no accepted cycles. Increase the noise threshold to suppress small reversals, or adjust the period tolerance for actual cycle variation; inspect the result rather than widening either blindly.

### Input frequency versus measured frequency

```text
Detected frequency = 1 / mean(measured accepted cycle durations)
```

The frequency shown in the metrics is **measured from accepted cycles**, not copied from the input field. For example, a 2 Hz guide with 10% tolerance can accept data measured near 2.08 Hz and still report approximately 2.08 Hz.

This is a conditional estimate from the accepted cycles. A small setting change may accept exactly the same cycles, leaving the markers unchanged. Always review the rejected count as well as the frequency.

Applied frequency, period tolerance and noise threshold persist across signal/capture selection within the HTML page. Filtering and cursor measurements reset when changing signals/captures. Refreshing or reopening the HTML restores initial defaults.

### Automatic mode

Leave applied frequency blank or enter **0**, then click **Update peaks** or press **Enter**. The active-mode label confirms the setting:

- **Auto: min peak spacing (s):** minimum separation of retained maxima; 0.08 s for force/counts and 0.02 s for voltage by default. Within this spacing the larger maximum is kept.
- **Auto: min swing (× SD):** the SD multiplier in the amplitude-reversal threshold; default 0.35. This is no longer an absolute level relative to the mean.
- **Noise threshold (% range):** provides the other amplitude threshold; the larger of the two is used. Raise it to suppress shallow shoulders or lower it if legitimate small swings disappear. The range and SD still describe the full capture, so a much larger transient can require threshold adjustment.

The lowest trough between retained maxima is marked without a global-mean gate. Average extrema remain measured sample values. The frequency estimate describes observed repetition, which can differ from the physical excitation for complex or sparsely sampled waveforms. Constant, monotonic or incomplete waveforms may correctly have N/A frequency.

Minimum spacing must be below the actual period to avoid rejecting real cycles. For example, 0.02 s is too large for a 100 Hz waveform. Detection runs on the complete selected capture and is cached; zoom selects markers and complete intervals without changing detection phase.

## 6. Filters

| Mode | Implementation |
|---|---|
| None | Filtered trace is a copy of the raw samples |
| Low-pass | Causal first-order RC-style IIR |
| High-pass | Causal first-order RC-style IIR |
| Band-pass | High-pass at the lower cutoff, followed by low-pass at the upper cutoff |

Click **Apply filter** after choosing the mode and cutoff values. **Reset** restores None. These preserve the filter family from the original viewer: they are **not Butterworth filters and not zero-phase `filtfilt`**.

For actual sample interval `dt` and cutoff `fc`:

```text
RC = 1 / (2 * pi * fc)

Low-pass:
    alpha = dt / (RC + dt)
    y[i] = y[i-1] + alpha * (x[i] - y[i-1])
    y[0] = x[0]

High-pass:
    alpha = RC / (RC + dt)
    y[i] = alpha * (y[i-1] + x[i] - x[i-1])
    y[0] = 0
```

Cutoffs must be positive and below half the median-based sample rate; the upper band-pass cutoff must exceed the lower cutoff. The cutoff is an RC design parameter, and discrete-time attenuation near Nyquist differs from an ideal continuous-time RC response.

Filters change amplitude and phase and introduce startup transients. They operate on the **whole selected signal/capture before visible-window analysis**, so zooming does not restart filtering at the zoom edge. Time gaps greater than five median sample intervals restart the filters.

## 7. Plot navigation and vertical scrolling

| Control | Action |
|---|---|
| Raw / Filtered checkboxes | Show or hide the corresponding waveform |
| Max / Min marker checkboxes | Show or hide extrema of the filtered waveform |
| Box zoom | Drag a rectangle to change X and Y ranges |
| Pan | Drag the plot to move the view |
| Wheel over plot | Zoom |
| Bottom time slider | Change the visible X interval |
| X start / X end + Apply X | Set a precise time window |
| Full X | Restore the complete selected signal/capture time range |
| Y center / Y span + Apply Y | Set a precise vertical range |
| Right-side Y position slider | Move the Y-axis viewing window while preserving its span |
| Up/down arrows or wheel over Y slider | Pan vertically by 10% of the current Y span per step |
| Fit Y / side Fit | Fit all visible waveform traces within the current X window |
| Reset view | Restore full X and fit Y |
| Plotly camera button | Download an image of the figure |

Vertical movement changes only the viewing range. It does not offset the recorded values, alter calibration, change the X window or recompute which cycles exist.

## 8. Visible-window metrics

Raw and filtered signals each report:

| Metric | Definition |
|---|---|
| Peak repetition (Hz) | Reciprocal mean duration of accepted complete intervals wholly inside the visible X window |
| Avg. peak max | Mean of detected maximum values whose markers lie in the visible X window |
| Avg. trough min | Mean of detected minimum values whose markers lie in the visible X window |
| Absolute max / min | Extrema over every visible sample, including window endpoints |
| RMS | `sqrt(mean(sample_value ** 2))`, including any DC component |

Peak detection is cached for the **complete selected capture**. X zoom/pan changes the included extrema and complete intervals, so zooming does not create new boundary peaks or shift detection phase. Average extrema are computed separately; visible max/min counts can differ near window edges.

`N/A` means insufficient accepted data for that metric. Sampling-limited mode reports an observed repetition estimate when a complete interval is resolved; it does not validate the excitation frequency. All metrics use the visible **X** interval, regardless of whether Y zoom clips part of the waveform. Hidden traces retain their numerical summaries. Red maxima and green minima belong to the **filtered** signal.

## 9. Two-point measurement

Select **Raw** or **Filtered** under **Measure on**, then click two waveform points. The panel shows each point's time/value, absolute time difference, signed value difference, and `1 / delta_t`.

**`1 / delta_t` is the waveform frequency only when the selected points are one full cycle apart.** A third click starts a new pair. Use Clear to remove both points. Changing the filter or selected signal/capture clears the measurement.

## 10. Saving and export

The source CSV is never edited. The desktop normally creates `<CSV name>_viewer.html` beside it, with a numbered suffix if that default already exists. Choose **Save to…** for another destination; the desktop asks before replacing an existing HTML file.

The self-contained HTML includes the selected file's loaded recordings and Plotly JavaScript. It can be reopened offline; interactive settings are session state and return to their defaults on reopening. Treat the HTML as containing your recorded data when sharing it.

**Export visible CSV** downloads every sample in the visible X window with original time, raw and filtered values, selected signal/capture, applied filter, input frequency, period tolerance and noise threshold. Capture/channel/motor-state fields are included when available. It does not export only peaks or silently downsample.

For headless conversion:

```bash
python shaker_csv_viewer.py --csv "C:\data\session\force_sensor.csv" --no-open
python shaker_csv_viewer.py --csv "C:\data\session\oscilloscope.csv" --output "C:\data\scope_viewer.html"
python shaker_csv_viewer.py --help
```

`--format` accepts the same format names as the GUI. `--rating` sets the count-only sensor rating. Explicit command-line `--output` overwrites that HTML target; without it, the numbered default naming avoids collisions.

## 11. Data handling and troubleshooting

| Symptom | What to check |
|---|---|
| Changing frequency appears to do nothing | Click **Update peaks**, inspect the detection status, and ensure the HTML was regenerated with version 1.3. Normal guidance may accept the same cycles after a small change. Sampling-limited mode shows observed extrema without validating their periods. The waveform itself is unchanged. |
| Max/min disappear after entering 1 Hz; saved sample rate is 2.5 Hz | Regenerate the plot with version 1.3. It suppresses weak shoulders, retains resolved troughs, estimates observed repetition and shows a sampling warning. There are only 2.5 saved samples per expected cycle; frequency-validated peaks need denser saved data. |
| Frequency controls affect force but apparently not scope | The same detector is used for both. Select the intended channel/capture, verify its waveform time span and unit, and inspect accepted/rejected intervals. Each capture is analyzed separately. |
| No accepted cycles / N/A frequency | Check the applied frequency, period tolerance, noise threshold and capture duration. Complete cycles are required. Short captures, gaps or complex signals may be unsuitable. |
| Two maxima per main swing / no minima with a different inactive baseline | Regenerate with version 1.3. Automatic detection now confirms amplitude reversals and no longer rejects troughs using the global mean. Inspect the amplitude thresholds. |
| Voltage is constant, for example -0.261 V | Inspect the saved numeric data and acquisition configuration. A viewer cannot reconstruct variations that were never recorded. |
| Old HTML still shows old controls | Regenerate it by selecting the CSV and pressing PLOT in the updated Python application. |
| Missing NumPy/pandas/Plotly | Run `INSTALL_REQUIREMENTS.bat`, using the same Python environment as the launcher. |
| Missing Tkinter / no display | Install Python with Tcl/Tk; use the command-line `--csv ... --no-open` option on a headless machine. |
| File not recognized | Use the documented schemas. Select Legacy Tektronix CSV only for the specified columns 4/5 format. |
| Wrong count-only force scale | Check the sensor rating and baseline. Saved Newton columns are intentionally not rescaled. |
| Saving beside the CSV fails | Use **Save to…** to choose a writable directory. |
| Large file takes time or uses substantial RAM | CSVs and all included captures are loaded into memory, then embedded in HTML. No silent sample reduction is performed. |

Additional handling:

- Rows with non-finite/non-numeric time or amplitude are omitted with a notice.
- Duplicate timestamps within one signal retain the last value, with a notice.
- Backwards time is rejected so restarted recordings are not silently merged.
- Scope rows with invalid capture identity/sample index are rejected. Unusable captures or signals are reported while usable ones remain available.
- Time gaps over five median intervals break plotted lines and exclude intervals spanning the gap from frequency calculations.
- Sampling rate and Nyquist are median-based estimates for irregular timestamps. RMS is sample-weighted, not time-weighted.

## 12. Validation and known limits

Run the included checks from `CSV-Viewer/`:

```bash
python -m unittest discover -s tests -p "test_*.py" -v
node tests/test_interactions.cjs
```

The eleven Python checks cover exact project schemas, preservation of saved Newton values, repeated scope captures/channels, legacy conversions, metadata/BOM/decimal-comma handling, missing data, duplicate/backwards timestamps, empty/partial recordings, and 150,000-sample input.

JavaScript tests cover known-frequency signals, filter/threshold validation, constant signals, recording gaps, false shoulder peaks on an asymmetric waveform, flat minima, independently measured frequency, period-tolerance rejection, incorrect half/double frequency guides, partial cycles, stable markers across zoom, Y controls/slider/arrows/wheel, cursor measurements, channel/capture selection and CSV export event handling. A separate scope-path case uses voltage-scale samples and negative waveform times and checks that applied-frequency changes affect markers and persist across capture/channel changes.

Regression tests include 1 Hz signals sampled at 2.5 samples/s across multiple sample phases and amplitude scales, plus a five-sample repeating spike pattern inspired by the latest screenshot. Tests check actual-sample extrema, measured peak repetition, constant signals, Nyquist rejection, the real **Update peaks** handler, persistent sampling warnings, zoom-stable markers, filter reset, marker visibility and return to automatic mode. The source CSV behind that screenshot was not supplied, so these reproduce the sampling conditions rather than the exact recorded waveform.

These numerical and event-logic checks passed. The JavaScript suite uses a **Plotly/DOM adapter**, not a real browser. Actual desktop/browser rendering was not exercised in the development runtime. Synthetic data matches the inspected export schemas; it is not a substitute for checking markers against your own recordings. The supplied waveform screenshot motivated the shoulder-minimum regression case, but its underlying CSV was not available for exact numerical reproduction.

Version 1.3 additionally reproduces a long inactive baseline followed by repeated large peaks and small shoulders. The previous algorithm returned 200 maxima, zero minima and no frequency; the updated algorithm returns 99 main maxima, 98 intervening minima and the synthetic waveform's measured 0.5 Hz. Checks cover amplitude-offset invariance, pending settings, Enter-to-apply, and a rejected frequency retaining the previous active setting. These are synthetic regression results, not a claim that the screenshot's unprovided CSV has been tested.

## 13. Version history

- **1.3:** amplitude-reversal automatic/sparse detection, removal of the global-mean minimum gate, suppression of weak shoulder maxima, restored sparse peak-repetition estimates, input/observed mismatch diagnostics, explicit active/pending settings, Enter-to-apply, Persian guide and baseline regression tests.
- **1.2:** fixes disappearing markers for sparsely saved waveforms using explicit sampling-limited observed extrema; adds persistent peak diagnostics beside the controls, saved samples per expected cycle, and sparse-data regression checks. Retains strict guidance for normally sampled captures.
- **1.1:** applied-frequency cycle guidance, editable period tolerance/noise threshold, stronger automatic minimum rejection, plateau handling, full-capture detection cached across zoom, vertical Y slider/arrows/wheel, and analysis settings in exported CSV.
- **1.0:** standalone file selector, project/legacy CSV readers, separate scope capture selection, raw/filtered plotting, RC filters, visible metrics, axis controls, two-point measurements and HTML/CSV export.
