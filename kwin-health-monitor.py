#!/usr/bin/env python3
"""
KWin compositor health monitor. Sits in the system tray and polls
kwin_wayland for memory, FDs, and VRAM. Green/yellow/red icon.
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHeaderView,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

DEFAULT_CONFIG = {
    "poll_interval_sec": 10,
    "history_minutes": 60,
    "log_dir": str(Path.home() / "logs" / "kwin-health"),
    "rss_warn_mb": 600,
    "rss_crit_mb": 1000,
    "fd_warn": 1024,
    "fd_crit": 4096,
    "vram_warn_pct": 70,
    "vram_crit_pct": 90,
    "rss_trend_warn_mb": 50,
    "rss_trend_window_min": 5,
}

CONFIG_PATH = Path.home() / ".config" / "kwin-health-monitor" / "config.toml"

SEVERITY = {"green": 0, "yellow": 1, "red": 2}


def load_config(path=None):
    """Load config from TOML file, falling back to defaults."""
    cfg = dict(DEFAULT_CONFIG)
    p = Path(path) if path else CONFIG_PATH
    if p.exists() and tomllib is not None:
        with open(p, "rb") as f:
            user = tomllib.load(f)
        cfg.update(user)
    return cfg


def worse(current, new):
    """Return the worse of two severity levels."""
    return current if SEVERITY[current] >= SEVERITY[new] else new


def find_kwin_pid():
    """Find the kwin_wayland process PID."""
    try:
        out = subprocess.check_output(
            ["pidof", "kwin_wayland"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        pids = out.split()
        if len(pids) == 1:
            return int(pids[0])
        # Multiple PIDs: take the one with highest RSS
        best_pid, best_rss = None, 0
        for p in pids:
            try:
                status = Path(f"/proc/{p}/status").read_text()
                for line in status.splitlines():
                    if line.startswith("VmRSS:"):
                        rss = int(line.split()[1])
                        if rss > best_rss:
                            best_pid, best_rss = int(p), rss
                        break
            except (FileNotFoundError, IndexError, ValueError):
                continue
        return best_pid
    except (subprocess.CalledProcessError, ValueError):
        return None


def read_proc_status(pid):
    """Parse VmRSS, FDSize, Threads from /proc/<pid>/status in one read."""
    result = {"rss_kb": None, "fd_size": None, "threads": None}
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except FileNotFoundError:
        return result
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            try:
                result["rss_kb"] = int(line.split()[1])
            except (IndexError, ValueError):
                pass
        elif line.startswith("FDSize:"):
            try:
                result["fd_size"] = int(line.split()[1])
            except (IndexError, ValueError):
                pass
        elif line.startswith("Threads:"):
            try:
                result["threads"] = int(line.split()[1])
            except (IndexError, ValueError):
                pass
    return result


def get_fd_count(pid):
    """Try listing /proc/<pid>/fd; returns (count, "exact") or (None, "fallback")."""
    try:
        return len(os.listdir(f"/proc/{pid}/fd")), "exact"
    except PermissionError:
        return None, "fallback"
    except FileNotFoundError:
        return None, None


def get_gpu_vram():
    """Return (used_mb, total_mb, vendor). Tries nvidia-smi, then AMD sysfs."""
    # NVIDIA
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            text=True, stderr=subprocess.DEVNULL
        ).strip()
        used, total = out.split(",")
        return int(used.strip()), int(total.strip()), "nvidia"
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        pass

    # AMD - read from sysfs
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
        vram_used = card / "device" / "mem_info_vram_used"
        vram_total = card / "device" / "mem_info_vram_total"
        if vram_used.exists() and vram_total.exists():
            try:
                used = int(vram_used.read_text().strip()) // (1024 * 1024)
                total = int(vram_total.read_text().strip()) // (1024 * 1024)
                return used, total, "amd"
            except (ValueError, OSError):
                continue

    # Intel uses shared system memory; no useful VRAM number to report.
    return None, None, None


def get_kwin_uptime(pid):
    """Process uptime in seconds, from /proc/<pid>/stat starttime."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # Field 22 (0-indexed: 21) is starttime in clock ticks
        fields = stat.rsplit(")", 1)[1].split()
        starttime_ticks = int(fields[19])  # index 19 after the comm field
        clk_tck = os.sysconf("SC_CLK_TCK")
        uptime_sec = float(Path("/proc/uptime").read_text().split()[0])
        return uptime_sec - (starttime_ticks / clk_tck)
    except (FileNotFoundError, ValueError, IndexError, OSError):
        return None


def format_duration(seconds):
    """Format seconds as human-readable duration."""
    if seconds is None:
        return "?"
    s = int(seconds)
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    h = s // 3600
    m = (s % 3600) // 60
    if h < 24:
        return f"{h}h {m}m"
    d = h // 24
    h = h % 24
    return f"{d}d {h}h"


def make_icon(color_name):
    """Create a colored circle icon with 'K' label."""
    size = 64
    pix = QPixmap(size, size)
    pix.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    colors = {
        "green": QColor(76, 175, 80),
        "yellow": QColor(255, 193, 7),
        "red": QColor(244, 67, 54),
        "gray": QColor(158, 158, 158),
    }
    c = colors.get(color_name, colors["gray"])
    painter.setBrush(c)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(4, 4, size - 8, size - 8)
    painter.setPen(QColor(255, 255, 255))
    font = painter.font()
    font.setPixelSize(36)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "K")
    painter.end()
    return QIcon(pix)


class HealthSnapshot:
    __slots__ = ("timestamp", "rss_mb", "fd_count", "threads",
                 "vram_used_mb", "vram_total_mb", "uptime_sec", "status")

    def __init__(self, ts, rss, fd, threads, vram_used, vram_total,
                 uptime, status):
        self.timestamp = ts
        self.rss_mb = rss
        self.fd_count = fd
        self.threads = threads
        self.vram_used_mb = vram_used
        self.vram_total_mb = vram_total
        self.uptime_sec = uptime
        self.status = status


class HistoryDialog(QDialog):
    """Shows recent health snapshots in a table."""

    def __init__(self, history, parent=None):
        super().__init__(parent)
        self.setWindowTitle("KWin Health History")
        self.setMinimumSize(750, 400)
        layout = QVBoxLayout(self)

        table = QTableWidget()
        headers = ["Time", "Status", "Uptime", "RSS (MB)", "FDs",
                    "Threads", "VRAM Used", "VRAM %"]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)

        snapshots = list(history)[::-1]
        table.setRowCount(len(snapshots))
        status_colors = {
            "green": QColor(200, 255, 200),
            "yellow": QColor(255, 255, 200),
            "red": QColor(255, 200, 200),
        }
        for i, snap in enumerate(snapshots):
            table.setItem(i, 0, QTableWidgetItem(
                time.strftime("%H:%M:%S", time.localtime(snap.timestamp))))
            item = QTableWidgetItem(snap.status.upper())
            color = status_colors.get(snap.status)
            if color:
                item.setBackground(color)
            table.setItem(i, 1, item)
            table.setItem(i, 2, QTableWidgetItem(
                format_duration(snap.uptime_sec)))
            table.setItem(i, 3, QTableWidgetItem(
                f"{snap.rss_mb:.0f}" if snap.rss_mb is not None else "?"))
            table.setItem(i, 4, QTableWidgetItem(
                str(snap.fd_count) if snap.fd_count is not None else "?"))
            table.setItem(i, 5, QTableWidgetItem(
                str(snap.threads) if snap.threads is not None else "?"))
            table.setItem(i, 6, QTableWidgetItem(
                f"{snap.vram_used_mb}" if snap.vram_used_mb is not None
                else "?"))
            if snap.vram_used_mb is not None and snap.vram_total_mb:
                pct = 100 * snap.vram_used_mb / snap.vram_total_mb
                table.setItem(i, 7, QTableWidgetItem(f"{pct:.0f}%"))
            else:
                table.setItem(i, 7, QTableWidgetItem("?"))

        layout.addWidget(table)


class KWinHealthMonitor:
    def __init__(self, cfg, enable_logging=True):
        self.cfg = cfg
        self.enable_logging = enable_logging
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        poll_sec = cfg["poll_interval_sec"]
        history_len = (cfg["history_minutes"] * 60) // poll_sec

        self.history = deque(maxlen=history_len)
        self.trend_samples = (cfg["rss_trend_window_min"] * 60) // poll_sec
        self.kwin_pid = find_kwin_pid()
        self.baseline_rss = None
        self.dialog = None
        self.gpu_vendor = None

        # Log setup
        self.log_dir = Path(cfg["log_dir"])
        if self.enable_logging:
            self.log_dir.mkdir(parents=True, exist_ok=True)

        # Pre-render icons
        self.icons = {c: make_icon(c) for c in
                      ("green", "yellow", "red", "gray")}

        # System tray
        self.tray = QSystemTrayIcon(self.icons["gray"], self.app)
        self.tray.setToolTip("KWin Health: starting...")
        self.tray.activated.connect(self._on_tray_click)

        menu = QMenu()
        history_action = QAction("Show History", menu)
        history_action.triggered.connect(self._show_history)
        menu.addAction(history_action)

        restart_action = QAction("Restart KWin...", menu)
        restart_action.triggered.connect(self._restart_kwin)
        menu.addAction(restart_action)

        menu.addSeparator()
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self.app.quit)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.show()

        # Poll timer
        self.timer = QTimer()
        self.timer.timeout.connect(self._poll)
        self.timer.start(poll_sec * 1000)
        self._poll()

    def _poll(self):
        # Re-find PID if lost (compositor restarted)
        if self.kwin_pid and not Path(f"/proc/{self.kwin_pid}").exists():
            self.kwin_pid = None
            self.baseline_rss = None

        if not self.kwin_pid:
            self.kwin_pid = find_kwin_pid()
            if not self.kwin_pid:
                self.tray.setIcon(self.icons["gray"])
                self.tray.setToolTip("KWin Health: compositor not found")
                return

        pid = self.kwin_pid

        # Single /proc/status read for RSS, FDSize, threads
        proc = read_proc_status(pid)
        rss_mb = proc["rss_kb"] / 1024 if proc["rss_kb"] else None

        # Try exact FD count, fall back to FDSize
        fd_exact, fd_mode = get_fd_count(pid)
        fd_count = fd_exact if fd_exact is not None else proc["fd_size"]
        fd_is_exact = fd_exact is not None

        threads = proc["threads"]
        vram_used, vram_total, vendor = get_gpu_vram()
        if vendor:
            self.gpu_vendor = vendor
        uptime = get_kwin_uptime(pid)

        if self.baseline_rss is None and rss_mb is not None:
            self.baseline_rss = rss_mb

        # Evaluate health
        issues = []
        status = "green"

        if rss_mb is not None:
            if rss_mb > self.cfg["rss_crit_mb"]:
                status = "red"
                issues.append(f"RSS critical: {rss_mb:.0f} MB")
            elif rss_mb > self.cfg["rss_warn_mb"]:
                status = worse(status, "yellow")
                issues.append(f"RSS high: {rss_mb:.0f} MB")

        if fd_count is not None:
            if fd_count > self.cfg["fd_crit"]:
                status = worse(status, "red")
                issues.append(f"FDs critical: {fd_count}")
            elif fd_count > self.cfg["fd_warn"]:
                status = worse(status, "yellow")
                issues.append(f"FDs high: {fd_count}")

        if vram_used is not None and vram_total and vram_total > 0:
            vram_pct = 100 * vram_used / vram_total
            if vram_pct > self.cfg["vram_crit_pct"]:
                status = worse(status, "red")
                issues.append(f"VRAM critical: {vram_pct:.0f}%")
            elif vram_pct > self.cfg["vram_warn_pct"]:
                status = worse(status, "yellow")
                issues.append(f"VRAM high: {vram_pct:.0f}%")

        # RSS trend
        if len(self.history) >= self.trend_samples:
            old_rss = self.history[-self.trend_samples].rss_mb
            if old_rss and rss_mb:
                delta = rss_mb - old_rss
                if delta > self.cfg["rss_trend_warn_mb"]:
                    status = worse(status, "yellow")
                    issues.append(
                        f"RSS rising: +{delta:.0f} MB/"
                        f"{self.cfg['rss_trend_window_min']}min")

        now = time.time()
        snap = HealthSnapshot(now, rss_mb, fd_count, threads,
                              vram_used, vram_total, uptime, status)
        self.history.append(snap)

        # Update tray icon
        self.tray.setIcon(self.icons[status])

        # Build tooltip
        lines = [f"KWin Health: {status.upper()}"]
        lines.append(f"PID {pid}  up {format_duration(uptime)}")
        if rss_mb is not None:
            rss_line = f"RSS: {rss_mb:.0f} MB"
            if self.baseline_rss is not None:
                rss_line += f" (baseline: {self.baseline_rss:.0f})"
            lines.append(rss_line)
        fd_label = "FDs" if fd_is_exact else "FDSize"
        lines.append(
            f"{fd_label}: {fd_count or '?'}  "
            f"Threads: {threads or '?'}")
        if vram_used is not None and vram_total:
            pct = 100 * vram_used / vram_total
            lines.append(
                f"VRAM: {vram_used}/{vram_total} MB "
                f"({pct:.0f}%) [{self.gpu_vendor}]")
        if issues:
            lines.append("---")
            lines.extend(issues)
        self.tray.setToolTip("\n".join(lines))

        # Log
        if self.enable_logging:
            log_file = self.log_dir / time.strftime("%Y-%m-%d.log")
            parts = [time.strftime("%H:%M:%S"), f"status={status}"]
            if rss_mb is not None:
                parts.append(f"rss={rss_mb:.0f}MB")
            if fd_count is not None:
                parts.append(f"{'fds' if fd_is_exact else 'fdsize'}={fd_count}")
            if threads is not None:
                parts.append(f"threads={threads}")
            if vram_used is not None:
                parts.append(f"vram={vram_used}/{vram_total}MB")
            if uptime is not None:
                parts.append(f"uptime={format_duration(uptime)}")
            if issues:
                parts.append(f"issues=[{'; '.join(issues)}]")
            with open(log_file, "a") as f:
                f.write(" ".join(parts) + "\n")

        # Desktop notification on transition to red
        if status == "red" and len(self.history) >= 2:
            prev = self.history[-2]
            if prev.status != "red":
                self.tray.showMessage(
                    "KWin Health Warning",
                    "\n".join(issues),
                    QSystemTrayIcon.MessageIcon.Warning,
                    5000,
                )

    def _on_tray_click(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_history()

    def _show_history(self):
        if self.dialog is not None:
            self.dialog.close()
        self.dialog = HistoryDialog(self.history)
        self.dialog.show()

    def _restart_kwin(self):
        """kwin_wayland --replace, with a confirmation dialog first."""
        reply = QMessageBox.question(
            None,
            "Restart KWin?",
            "This will restart the compositor.\n"
            "Some applications (e.g. Firefox) may close.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            subprocess.Popen(
                ["kwin_wayland", "--replace"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def run(self):
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        return self.app.exec()


def main():
    parser = argparse.ArgumentParser(
        description="KWin Wayland compositor health monitor")
    parser.add_argument(
        "-c", "--config", metavar="PATH",
        help=f"config file path (default: {CONFIG_PATH})")
    parser.add_argument(
        "--interval", type=int, metavar="SEC",
        help="poll interval in seconds (overrides config)")
    parser.add_argument(
        "--no-log", action="store_true",
        help="disable file logging")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.interval:
        cfg["poll_interval_sec"] = args.interval

    monitor = KWinHealthMonitor(cfg, enable_logging=not args.no_log)
    sys.exit(monitor.run())


if __name__ == "__main__":
    main()
