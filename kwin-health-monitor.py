#!/usr/bin/env python3
"""
KWin Compositor Health Monitor - System tray widget.
Tracks KWin memory, file descriptors, GPU VRAM, and flags degradation.
Icon color: green (healthy) / yellow (warning) / red (critical).
Tooltip and click menu show details and history.
"""

import os
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHeaderView,
    QLabel,
    QMenu,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

# --- Configuration ---
POLL_INTERVAL_MS = 10_000  # 10 seconds
HISTORY_LEN = 360          # 1 hour at 10s intervals
LOG_DIR = Path.home() / "logs" / "kwin-health"

# Thresholds
KWIN_RSS_WARN_MB = 600
KWIN_RSS_CRIT_MB = 1000
KWIN_FD_WARN = 1024   # FDSize (table alloc) starts at 512 for KWin, doubles on growth
KWIN_FD_CRIT = 4096
VRAM_WARN_PCT = 70
VRAM_CRIT_PCT = 90
RSS_TREND_WARN_MB = 50  # growth over last 5 min


def find_kwin_pid():
    """Find the kwin_wayland process PID."""
    try:
        out = subprocess.check_output(
            ["pidof", "kwin_wayland"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        # May return multiple PIDs; take the main one (highest RSS)
        pids = out.split()
        if len(pids) == 1:
            return int(pids[0])
        best_pid, best_rss = None, 0
        for p in pids:
            try:
                rss = int(Path(f"/proc/{p}/status").read_text()
                          .split("VmRSS:")[1].split()[0])
                if rss > best_rss:
                    best_pid, best_rss = int(p), rss
            except (FileNotFoundError, IndexError, ValueError):
                continue
        return best_pid
    except (subprocess.CalledProcessError, ValueError):
        return None


def get_kwin_rss_kb(pid):
    """Get KWin RSS in KB from /proc."""
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (FileNotFoundError, ValueError):
        pass
    return None


def get_kwin_fd_count(pid):
    """Get KWin FD table size from /proc/status (FDSize).

    Direct /proc/<pid>/fd listing is often permission-denied for KWin
    due to its elevated scheduling class. FDSize is the allocated fd table
    size (grows in powers of 2), readable without special permissions.
    Not exact count but a useful proxy for leak detection.
    """
    try:
        return len(os.listdir(f"/proc/{pid}/fd"))
    except PermissionError:
        # Fall back to FDSize from /proc/status
        try:
            status = Path(f"/proc/{pid}/status").read_text()
            for line in status.splitlines():
                if line.startswith("FDSize:"):
                    return int(line.split()[1])
        except (FileNotFoundError, ValueError):
            pass
    except FileNotFoundError:
        pass
    return None


def get_kwin_threads(pid):
    """Get KWin thread count."""
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        for line in status.splitlines():
            if line.startswith("Threads:"):
                return int(line.split()[1])
    except (FileNotFoundError, ValueError):
        pass
    return None


def get_gpu_vram():
    """Get GPU VRAM (used_mb, total_mb) via nvidia-smi."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            text=True, stderr=subprocess.DEVNULL
        ).strip()
        used, total = out.split(",")
        return int(used.strip()), int(total.strip())
    except (subprocess.CalledProcessError, ValueError):
        return None, None


def make_icon(color_name):
    """Create a simple colored circle icon."""
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
    # Draw "K" in the center
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
                 "vram_used_mb", "vram_total_mb", "status")

    def __init__(self, ts, rss, fd, threads, vram_used, vram_total, status):
        self.timestamp = ts
        self.rss_mb = rss
        self.fd_count = fd
        self.threads = threads
        self.vram_used_mb = vram_used
        self.vram_total_mb = vram_total
        self.status = status


class HistoryDialog(QDialog):
    """Shows recent health snapshots in a table."""

    def __init__(self, history, parent=None):
        super().__init__(parent)
        self.setWindowTitle("KWin Health History")
        self.setMinimumSize(700, 400)
        layout = QVBoxLayout(self)

        table = QTableWidget()
        headers = ["Time", "Status", "RSS (MB)", "FDs", "Threads",
                    "VRAM Used", "VRAM %"]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)

        # Show most recent first
        snapshots = list(history)[::-1]
        table.setRowCount(len(snapshots))
        for i, snap in enumerate(snapshots):
            table.setItem(i, 0, QTableWidgetItem(
                time.strftime("%H:%M:%S", time.localtime(snap.timestamp))))
            item = QTableWidgetItem(snap.status.upper())
            color = {"green": QColor(200, 255, 200),
                     "yellow": QColor(255, 255, 200),
                     "red": QColor(255, 200, 200)}.get(snap.status)
            if color:
                item.setBackground(color)
            table.setItem(i, 1, item)
            table.setItem(i, 2, QTableWidgetItem(
                f"{snap.rss_mb:.0f}" if snap.rss_mb else "?"))
            table.setItem(i, 3, QTableWidgetItem(
                str(snap.fd_count) if snap.fd_count else "?"))
            table.setItem(i, 4, QTableWidgetItem(
                str(snap.threads) if snap.threads else "?"))
            table.setItem(i, 5, QTableWidgetItem(
                f"{snap.vram_used_mb}" if snap.vram_used_mb else "?"))
            if snap.vram_used_mb and snap.vram_total_mb:
                pct = 100 * snap.vram_used_mb / snap.vram_total_mb
                table.setItem(i, 6, QTableWidgetItem(f"{pct:.0f}%"))
            else:
                table.setItem(i, 6, QTableWidgetItem("?"))

        layout.addWidget(table)


class KWinHealthMonitor:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        self.history = deque(maxlen=HISTORY_LEN)
        self.kwin_pid = find_kwin_pid()
        self.baseline_rss = None
        self.dialog = None

        # Log setup
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        # System tray
        self.tray = QSystemTrayIcon(make_icon("gray"), self.app)
        self.tray.setToolTip("KWin Health: starting...")
        self.tray.activated.connect(self._on_tray_click)

        # Context menu
        menu = QMenu()
        history_action = QAction("Show History", menu)
        history_action.triggered.connect(self._show_history)
        menu.addAction(history_action)

        restart_action = QAction("Restart KWin", menu)
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
        self.timer.start(POLL_INTERVAL_MS)

        # Initial poll
        self._poll()

    def _poll(self):
        # Re-find PID if lost (compositor restarted)
        if self.kwin_pid and not Path(f"/proc/{self.kwin_pid}").exists():
            self.kwin_pid = None
            self.baseline_rss = None

        if not self.kwin_pid:
            self.kwin_pid = find_kwin_pid()
            if not self.kwin_pid:
                self.tray.setIcon(make_icon("gray"))
                self.tray.setToolTip("KWin Health: compositor not found")
                return

        rss_kb = get_kwin_rss_kb(self.kwin_pid)
        rss_mb = rss_kb / 1024 if rss_kb else None
        fd_count = get_kwin_fd_count(self.kwin_pid)
        threads = get_kwin_threads(self.kwin_pid)
        vram_used, vram_total = get_gpu_vram()

        if self.baseline_rss is None and rss_mb is not None:
            self.baseline_rss = rss_mb

        # Evaluate health
        issues = []
        status = "green"

        if rss_mb is not None:
            if rss_mb > KWIN_RSS_CRIT_MB:
                status = "red"
                issues.append(f"RSS critical: {rss_mb:.0f} MB")
            elif rss_mb > KWIN_RSS_WARN_MB:
                status = max(status, "yellow", key=lambda s: ["green", "yellow", "red"].index(s))
                issues.append(f"RSS high: {rss_mb:.0f} MB")

        if fd_count is not None:
            if fd_count > KWIN_FD_CRIT:
                status = "red"
                issues.append(f"FDs critical: {fd_count}")
            elif fd_count > KWIN_FD_WARN:
                status = max(status, "yellow", key=lambda s: ["green", "yellow", "red"].index(s))
                issues.append(f"FDs high: {fd_count}")

        if vram_used is not None and vram_total is not None and vram_total > 0:
            vram_pct = 100 * vram_used / vram_total
            if vram_pct > VRAM_CRIT_PCT:
                status = "red"
                issues.append(f"VRAM critical: {vram_pct:.0f}%")
            elif vram_pct > VRAM_WARN_PCT:
                status = max(status, "yellow", key=lambda s: ["green", "yellow", "red"].index(s))
                issues.append(f"VRAM high: {vram_pct:.0f}%")

        # Trend: check RSS growth over last 5 min (30 samples)
        if len(self.history) >= 30:
            old_rss = self.history[-30].rss_mb
            if old_rss and rss_mb and (rss_mb - old_rss) > RSS_TREND_WARN_MB:
                status = max(status, "yellow", key=lambda s: ["green", "yellow", "red"].index(s))
                issues.append(f"RSS rising: +{rss_mb - old_rss:.0f} MB/5min")

        now = time.time()
        snap = HealthSnapshot(now, rss_mb, fd_count, threads,
                              vram_used, vram_total, status)
        self.history.append(snap)

        # Update tray
        self.tray.setIcon(make_icon(status))

        vram_str = ""
        if vram_used is not None and vram_total is not None:
            vram_str = f"\nVRAM: {vram_used}/{vram_total} MB ({100*vram_used/vram_total:.0f}%)"

        tooltip = (
            f"KWin Health: {status.upper()}\n"
            f"PID: {self.kwin_pid}\n"
            f"RSS: {rss_mb:.0f} MB" + (f" (baseline: {self.baseline_rss:.0f})" if self.baseline_rss else "") + "\n"
            f"FDs: {fd_count or '?'}  Threads: {threads or '?'}"
            f"{vram_str}"
        )
        if issues:
            tooltip += "\n--- Issues ---\n" + "\n".join(issues)

        self.tray.setToolTip(tooltip)

        # Log
        log_file = LOG_DIR / time.strftime("%Y-%m-%d.log")
        with open(log_file, "a") as f:
            parts = [time.strftime("%H:%M:%S"), f"status={status}"]
            if rss_mb is not None:
                parts.append(f"rss={rss_mb:.0f}MB")
            if fd_count is not None:
                parts.append(f"fds={fd_count}")
            if threads is not None:
                parts.append(f"threads={threads}")
            if vram_used is not None:
                parts.append(f"vram={vram_used}/{vram_total}MB")
            if issues:
                parts.append(f"issues=[{'; '.join(issues)}]")
            f.write(" ".join(parts) + "\n")

        # Notify on transition to red
        if status == "red" and len(self.history) >= 2:
            prev = self.history[-2]
            if prev.status != "red":
                self.tray.showMessage(
                    "KWin Health Warning",
                    "\n".join(issues),
                    QSystemTrayIcon.MessageIcon.Warning,
                    5000
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
        """Restart KWin via its DBus replace mechanism."""
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


if __name__ == "__main__":
    monitor = KWinHealthMonitor()
    sys.exit(monitor.run())
