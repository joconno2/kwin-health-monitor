# kwin-health-monitor

System tray monitor for KWin Wayland compositor health (memory, FDs, VRAM).

![Python](https://img.shields.io/badge/python-3.10+-blue) ![KDE Plasma](https://img.shields.io/badge/KDE_Plasma-6-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## Why

KWin on Wayland has a [long-standing problem](https://bugs.kde.org/show_bug.cgi?id=498627) where it leaks memory and file descriptors over time, especially with NVIDIA drivers. After running long enough, kwin_wayland bloats to multiple GB and you start getting mouse lag and unresponsive windows. Restarting the compositor fixes it, but there's no built-in way to tell when you need to, so most people just notice when things are already bad and restart by hand.

This puts a tray icon next to your clock that goes from green to yellow to red as KWin's resource usage climbs, so you can restart before the lag hits.

## What it does

Polls kwin_wayland every 10 seconds for RSS memory, file descriptor count/table size, GPU VRAM (NVIDIA or AMD), and thread count. Shows the results as a colored **K** icon in the system tray. Hovering shows current numbers and KWin uptime, clicking opens a history table with the last hour of snapshots, and right-click gives you a menu to restart KWin or quit. You get a desktop notification when things go red.

Daily logs go to `~/logs/kwin-health/`.

## Requirements

- KDE Plasma 6, KWin Wayland
- Python 3.10+
- PyQt6 (`python-pyqt6` on Arch)
- GPU monitoring needs `nvidia-smi` (NVIDIA) or sysfs support (AMD). If neither is available, GPU stats are just skipped.

## Install

### Arch Linux (AUR)

```bash
paru -S kwin-health-monitor-git
```

Then copy the desktop file to autostart:

```bash
cp /usr/share/applications/kwin-health-monitor.desktop ~/.config/autostart/
```

### Manual

```bash
cp kwin-health-monitor.py ~/.local/bin/
chmod +x ~/.local/bin/kwin-health-monitor.py
cp kwin-health-monitor.desktop ~/.config/autostart/
```

### Run

```bash
kwin-health-monitor &
```

## Configuration

Copy `config.toml.example` to `~/.config/kwin-health-monitor/config.toml` if you want to change thresholds:

```bash
mkdir -p ~/.config/kwin-health-monitor
cp config.toml.example ~/.config/kwin-health-monitor/config.toml
```

### Defaults

| Metric | Warning | Critical |
|--------|---------|----------|
| KWin RSS | 600 MB | 1000 MB |
| FD table size | 1024 | 4096 |
| GPU VRAM | 70% | 90% |
| RSS trend (5 min) | +50 MB | -- |

### CLI

```
  -c, --config PATH   config file (default: ~/.config/kwin-health-monitor/config.toml)
  --interval SEC      poll interval in seconds (overrides config)
  --no-log            disable file logging
```

## Notes

Metrics come from `/proc/<pid>/status` (RSS, FD table size, threads) and `nvidia-smi` or `/sys/class/drm/` (VRAM). Nothing invasive, no ptrace, no debug interfaces.

The FD count is tricky: `/proc/<pid>/fd` is often unreadable for KWin because it runs with elevated scheduling priority. In that case the monitor falls back to `FDSize` from `/proc/status`, which is the FD table allocation size (grows in powers of 2). Not exact, but good enough to catch leaks.

"Restart KWin" runs `kwin_wayland --replace`. This restarts the compositor without logging you out, but some apps (Firefox in particular) will die. There's a confirmation dialog.

## License

MIT
