# kwin-health-monitor

System tray widget that monitors KDE KWin Wayland compositor health and flags degradation before it causes input lag.

![Python](https://img.shields.io/badge/python-3.10+-blue) ![KDE Plasma](https://img.shields.io/badge/KDE_Plasma-6-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## What it does

- Polls KWin every 10 seconds for RSS memory, file descriptor table size, thread count, and GPU VRAM (NVIDIA)
- Shows a color-coded "K" icon in the system tray: **green** (healthy), **yellow** (warning), **red** (critical)
- Tracks RSS growth trend and flags rapid increases (>50 MB in 5 minutes)
- Sends a desktop notification on transition to red
- Hover tooltip shows current metrics; click opens a history table (last hour)
- Right-click menu: Show History, Restart KWin, Quit
- Logs to `~/logs/kwin-health/YYYY-MM-DD.log`

## Thresholds

| Metric | Warning | Critical |
|--------|---------|----------|
| KWin RSS | 600 MB | 1000 MB |
| FD table size | 1024 | 4096 |
| GPU VRAM | 70% | 90% |
| RSS trend (5 min) | +50 MB | -- |

## Requirements

- KDE Plasma 6 with KWin Wayland
- Python 3.10+
- PyQt6 (`python-pyqt6` on Arch)
- NVIDIA GPU with `nvidia-smi` (GPU metrics are optional; gracefully skipped if unavailable)

## Install

```bash
cp kwin-health-monitor.py ~/.local/bin/
chmod +x ~/.local/bin/kwin-health-monitor.py
```

### Autostart with Plasma

```bash
cp kwin-health-monitor.desktop ~/.config/autostart/
```

Or create `~/.config/autostart/kwin-health-monitor.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=KWin Health Monitor
Exec=/usr/bin/python3 /path/to/kwin-health-monitor.py
Terminal=false
```

### Run manually

```bash
python3 kwin-health-monitor.py &
```

## Why

KWin on Wayland (especially with NVIDIA) can accumulate GPU buffer state, file descriptors, or memory over long sessions, eventually causing mouse lag and input latency. Restarting the compositor fixes it instantly, but you need to know *when* to restart. This monitor watches for the degradation and alerts you before it gets bad.

## License

MIT
