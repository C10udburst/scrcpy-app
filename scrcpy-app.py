import sys
import os
import re
import json
import subprocess
import hashlib
import shutil
import argparse
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QListView,
    QVBoxLayout,
    QWidget,
    QLineEdit,
    QLabel,
    QStackedWidget,
    QPushButton,
)
from PyQt6.QtWidgets import QHBoxLayout, QMessageBox
from PyQt6.QtCore import QSize, Qt, QThread, pyqtSignal, QSortFilterProxyModel
from PyQt6.QtGui import (
    QStandardItemModel,
    QStandardItem,
    QIcon,
    QPainter,
    QColor,
    QPixmap,
)

# Cache directory
CACHE_DIR = "/tmp/scrcpy-app-cache"
PY_SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.argv[0] if sys.argv[0] else __file__))
ICON_EXTRACTOR_JAR = os.path.join(PY_SCRIPT_DIR, "icon-extractor.jar")
REMOTE_TMP_DIR = "/data/local/tmp"

class IconFetcherThread(QThread):
    icon_fetched = pyqtSignal(str, str) # package_name, icon_path

    def __init__(self, serial, device_cache_dir, allowed_packages=None):
        super().__init__()
        self.serial = serial
        self.device_cache_dir = device_cache_dir
        self.running = True
        # allowed_packages: set or None
        self.allowed_packages = set(allowed_packages) if allowed_packages else None

    def run(self):
        # 1. Push jar
        subprocess.run(["adb", "-s", self.serial, "push", ICON_EXTRACTOR_JAR, REMOTE_TMP_DIR])

        # 2. Run app and read stdout
        cmd = ["adb", "-s", self.serial, "shell", f"CLASSPATH={REMOTE_TMP_DIR}/icon-extractor.jar app_process {REMOTE_TMP_DIR} Main"]
        print(f"Running {' '.join(cmd)}")

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
        for line in proc.stdout:
            if not self.running:
                break
            if "Extracted:" in line:
                pkg = line.split("Extracted:")[1].strip()
                # honor allowed_packages if provided
                if self.allowed_packages is not None and pkg not in self.allowed_packages:
                    continue

                # local cache path
                local_icon = os.path.join(self.device_cache_dir, f"{pkg}.png")

                # if cached, emit without re-pulling
                if os.path.exists(local_icon) and os.path.getsize(local_icon) > 0:
                    self.icon_fetched.emit(pkg, local_icon)
                    continue

                # pull from device (Main.java writes to /data/local/tmp/ExtractedIcons)
                subprocess.run(["adb", "-s", self.serial, "pull", f"/data/local/tmp/ExtractedIcons/{pkg}.png", local_icon])
                if os.path.exists(local_icon):
                    self.icon_fetched.emit(pkg, local_icon)


def get_launcher_icon(icon_path, label):
    if icon_path and os.path.exists(icon_path):
        return QIcon(icon_path)

    # Fallback: create a colorful square with the first letter of the label
    h = hashlib.md5(label.encode("utf-8")).hexdigest()
    color_hex = f"#{h[:6]}"

    icon_size = 64
    pixmap = QPixmap(icon_size, icon_size)
    pixmap.fill(QColor(color_hex))

    painter = QPainter(pixmap)
    painter.setPen(QColor("#2c3e50"))
    painter.drawRect(0, 0, icon_size - 1, icon_size - 1)

    painter.setPen(QColor("#ffffff"))
    font = painter.font()
    font.setPointSize(24)
    font.setBold(True)
    painter.setFont(font)

    letter = label[0].upper() if label else "?"
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, letter)
    painter.end()

    icon = QIcon()
    icon.addPixmap(pixmap)
    return icon


class DeviceMonitorThread(QThread):
    device_status_changed = pyqtSignal(
        bool, str
    )  # (is_connected, device_serial)
    apps_loaded = pyqtSignal(
        list
    )  # [{'package': ..., 'label': ..., 'icon_path': ...}]
    app_resolved = pyqtSignal(
        dict
    )  # {'package': ..., 'label': ..., 'icon_path': ...}
    packages_listed = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self.running = True
        self.connected = False

    def run(self):
        while self.running:
            try:
                res = subprocess.run(
                    ["adb", "get-state"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                state = res.stdout.strip()
                connected = state == "device"
            except Exception:
                connected = False

            if connected != self.connected:
                self.connected = connected
                serial = ""
                if connected:
                    try:
                        serial = subprocess.run(
                            ["adb", "get-serialno"],
                            capture_output=True,
                            text=True,
                        ).stdout.strip()
                    except Exception:
                        pass
                self.device_status_changed.emit(connected, serial)

                if connected:
                    self.load_apps(serial)

            self.msleep(2000)

    def load_apps(self, serial):
        try:
            # Query launchable activities
            cmd = [
                "adb",
                "shell",
                "cmd",
                "package",
                "query-activities",
                "-a",
                "android.intent.action.MAIN",
                "-c",
                "android.intent.category.LAUNCHER",
            ]
            res = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5
            )
            output = res.stdout
        except Exception as e:
            print(f"Error querying activities: {e}")
            return

        packages = []
        for block in output.split("Activity #")[1:]:
            pkg_match = re.search(r"packageName=([a-zA-Z0-9_.]+)", block)
            if not pkg_match:
                pkg_match = re.search(
                    r"packageName:\s*([a-zA-Z0-9_.]+)", block
                )
            if pkg_match:
                packages.append(pkg_match.group(1))

        packages = list(dict.fromkeys(packages))
        if not packages:
            return

        # Scope cache per device
        safe_serial = "".join(
            c for c in serial if c.isalnum() or c in ("-", "_")
        )
        if not safe_serial:
            safe_serial = "default"

        device_cache_dir = os.path.join(CACHE_DIR, safe_serial)
        os.makedirs(device_cache_dir, exist_ok=True)
        cache_file = os.path.join(device_cache_dir, "apps.json")

        cache = {}
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r") as f:
                    cache = json.load(f)
            except Exception:
                pass

        new_cache = {}
        cached_active_apps = []
        packages_to_fetch = []

        for pkg in packages:
            if pkg in cache:
                cached_info = cache[pkg]
                icon_path = cached_info.get("icon")
                # only treat as cached if an icon path exists and the file is present
                if icon_path and os.path.exists(icon_path):
                    cached_active_apps.append(
                        {
                            "package": pkg,
                            "label": cached_info.get("label", pkg),
                            "icon_path": icon_path,
                        }
                    )
                    new_cache[pkg] = cached_info
                    continue
            packages_to_fetch.append(pkg)

        if cached_active_apps:
            self.apps_loaded.emit(cached_active_apps)

        # Notify about the full set of launchable packages so the UI can start extractor limited to these
        try:
            self.packages_listed.emit(packages)
        except Exception:
            pass

        # For remaining packages, fetch dynamically
        for pkg in packages_to_fetch:
            if not self.connected or not self.running:
                break

            info = self.fetch_app_details(pkg, device_cache_dir)
            if info:
                new_cache[pkg] = info
                self.app_resolved.emit(
                    {
                        "package": pkg,
                        "label": info["label"],
                        "icon_path": info["icon"],
                    }
                )
            else:
                # Fallback: deduce name from package
                label_parts = pkg.split(".")
                label = label_parts[-1].capitalize()
                is_generic = label.lower() in (
                    "android",
                    "google",
                    "app",
                    "application",
                )
                if is_generic and len(label_parts) > 1:
                    label = label_parts[-2].capitalize()

                fallback_info = {
                    "label": label,
                    "icon": "",
                    "apk_path": "",
                }
                new_cache[pkg] = fallback_info
                self.app_resolved.emit(
                    {
                        "package": pkg,
                        "label": fallback_info["label"],
                        "icon_path": "",
                    }
                )

        # Write updated cache
        try:
            with open(cache_file, "w") as f:
                json.dump(new_cache, f, indent=2)
        except Exception as e:
            print(f"Error saving cache: {e}")

    def fetch_app_details(self, package_name, device_cache_dir):
        # Simplified: rely on the on-device Java extractor to provide icons.
        # Return a lightweight label fallback and empty icon/apk path.
        try:
            label_parts = package_name.split('.')
            label = label_parts[-1].capitalize() if label_parts else package_name
            is_generic = label.lower() in ("android", "google", "app", "application")
            if is_generic and len(label_parts) > 1:
                label = label_parts[-2].capitalize()

            return {
                "label": label,
                "icon": "",
                "apk_path": "",
            }
        except Exception as e:
            print(f"Error fetching app details for {package_name}: {e}")
            return None


class LauncherApp(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Scrcpy App Launcher")
        self.resize(720, 520)
        self.setMinimumWidth(650)

        self.stacked_widget = QStackedWidget()
        self.setCentralWidget(self.stacked_widget)

        # 1. Connection Waiting Screen
        self.conn_widget = QWidget()
        conn_layout = QVBoxLayout(self.conn_widget)
        conn_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.conn_title = QLabel("🔌 Waiting for Android Device...")
        self.conn_title.setStyleSheet(
            "font-size: 20px; font-weight: bold;"
        )
        self.conn_title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.conn_subtitle = QLabel(
            "Connect your device via USB or network, and make sure "
            "USB debugging is enabled."
        )
        self.conn_subtitle.setStyleSheet(
            "font-size: 13px; margin-top: 10px;"
        )
        self.conn_subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)

        conn_layout.addWidget(self.conn_title)
        conn_layout.addWidget(self.conn_subtitle)
        self.stacked_widget.addWidget(self.conn_widget)

        # 2. Main Launcher Screen
        self.launcher_widget = QWidget()
        launcher_layout = QVBoxLayout(self.launcher_widget)
        launcher_layout.setContentsMargins(15, 15, 15, 15)
        launcher_layout.setSpacing(10)

        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText(
            "🔍 Search apps by name or package..."
        )
        self.search_bar.setStyleSheet(
            """
            QLineEdit {
                border-radius: 6px;
                padding: 8px 14px;
                font-size: 14px;
            }
        """
        )
        # search bar with clear-cache icon to the right
        search_hbox = QHBoxLayout()
        search_hbox.addWidget(self.search_bar)
        self.clear_cache_btn = QPushButton()
        clear_icon = QIcon.fromTheme("edit-clear")
        if clear_icon.isNull():
            self.clear_cache_btn.setText("🗑")
        else:
            self.clear_cache_btn.setIcon(clear_icon)
        self.clear_cache_btn.setToolTip("Clear device icon cache")
        self.clear_cache_btn.setFixedSize(28, 28)
        self.clear_cache_btn.clicked.connect(self.on_clear_cache_clicked)
        search_hbox.addWidget(self.clear_cache_btn)
        launcher_layout.addLayout(search_hbox)

        self.list_view = QListView()
        self.list_view.setViewMode(QListView.ViewMode.IconMode)
        self.list_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.list_view.setMovement(QListView.Movement.Static)
        self.list_view.setSpacing(15)
        self.list_view.setIconSize(QSize(64, 64))
        self.list_view.setGridSize(QSize(100, 110))
        launcher_layout.addWidget(self.list_view)

        self.status_bar = QLabel("Disconnected")
        self.status_bar.setStyleSheet(
            "color: #7f8c8d; font-size: 11px; padding: 4px;"
        )
        launcher_layout.addWidget(self.status_bar)

        # clear cache button moved into search bar

        self.stacked_widget.addWidget(self.launcher_widget)

        # Data Model and Proxy Model
        self.model = QStandardItemModel()
        self.proxy_model = QSortFilterProxyModel()
        self.proxy_model.setSourceModel(self.model)
        self.proxy_model.setSortCaseSensitivity(
            Qt.CaseSensitivity.CaseInsensitive
        )
        self.proxy_model.setFilterCaseSensitivity(
            Qt.CaseSensitivity.CaseInsensitive
        )
        self.proxy_model.setFilterKeyColumn(0)

        # Search filter mapping
        self.search_bar.textChanged.connect(
            self.proxy_model.setFilterFixedString
        )

        self.list_view.setModel(self.proxy_model)
        self.list_view.clicked.connect(self.on_item_clicked)

        # Track existing packages in UI
        self.displayed_packages = {}
        self.current_serial = None
        self.icon_fetcher = None

        # Start background monitor thread
        self.monitor_thread = DeviceMonitorThread()
        self.monitor_thread.device_status_changed.connect(
            self.on_device_status_changed
        )
        self.monitor_thread.apps_loaded.connect(self.on_apps_loaded)
        self.monitor_thread.app_resolved.connect(self.on_app_resolved)
        self.monitor_thread.packages_listed.connect(self.on_packages_listed)
        self.monitor_thread.start()

    def on_device_status_changed(self, connected, serial):
        if connected:
            # store current serial
            self.current_serial = serial
            self.stacked_widget.setCurrentIndex(1)
            self.status_bar.setText(
                f"Connected: Device ({serial})"
                if serial
                else "Connected: Device"
            )
        else:
            self.stacked_widget.setCurrentIndex(0)
            self.status_bar.setText("Disconnected")
            self.model.clear()
            self.displayed_packages.clear()
            # stop icon fetcher
            try:
                if self.icon_fetcher and self.icon_fetcher.isRunning():
                    self.icon_fetcher.running = False
                    self.icon_fetcher.wait(1000)
            except Exception:
                pass
            self.icon_fetcher = None
            self.current_serial = None

    def on_packages_listed(self, packages):
        # Start the icon extractor but restrict to the packages discovered by the launcher
        if not self.current_serial:
            return

        safe_serial = "".join(c for c in self.current_serial if c.isalnum() or c in ("-", "_")) or "default"
        device_cache_dir = os.path.join(CACHE_DIR, safe_serial)
        os.makedirs(device_cache_dir, exist_ok=True)

        # stop existing fetcher if any
        try:
            if self.icon_fetcher and self.icon_fetcher.isRunning():
                self.icon_fetcher.running = False
                self.icon_fetcher.wait(1000)
        except Exception:
            pass

        self.icon_fetcher = IconFetcherThread(self.current_serial, device_cache_dir, allowed_packages=packages)
        self.icon_fetcher.icon_fetched.connect(self.on_icon_fetched)
        self.icon_fetcher.start()

    def on_clear_cache_clicked(self):
        # Confirm with the user before clearing cache
        reply = QMessageBox.warning(self, "Clear Cache", "Clear icon cache for connected device?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.clear_cache()

    def on_icon_fetched(self, package, local_icon_path):
        # Update cache file and UI when an icon is streamed from device
        safe_serial = "default"
        try:
            if self.current_serial:
                safe_serial = "".join(c for c in self.current_serial if c.isalnum() or c in ("-", "_")) or "default"
        except Exception:
            pass
        device_cache_dir = os.path.join(CACHE_DIR, safe_serial)
        cache_file = os.path.join(device_cache_dir, "apps.json")

        cache = {}
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r") as f:
                    cache = json.load(f)
            except Exception:
                cache = {}

        entry = cache.get(package, {})
        entry["icon"] = local_icon_path
        if not entry.get("label"):
            entry["label"] = package.split(".")[-1].capitalize()
        cache[package] = entry

        try:
            with open(cache_file, "w") as f:
                json.dump(cache, f, indent=2)
        except Exception:
            pass

        # Update UI
        self.add_or_update_app_item(package, entry.get("label", package), local_icon_path)

    def clear_cache(self):
        # Clear the cache for the currently connected device
        if not self.current_serial:
            # fall back: try to detect device
            try:
                res = subprocess.run(["adb", "get-serialno"], capture_output=True, text=True, timeout=2)
                serial = res.stdout.strip()
            except Exception:
                serial = None
        else:
            serial = self.current_serial

        if not serial:
            print("No device serial available; cannot clear device cache.")
            return

        safe_serial = "".join(c for c in serial if c.isalnum() or c in ("-", "_")) or "default"
        device_dir = os.path.join(CACHE_DIR, safe_serial)
        if os.path.exists(device_dir):
            try:
                shutil.rmtree(device_dir)
                print(f"Cleared cache directory: {device_dir}")
            except Exception as e:
                print(f"Failed to clear cache: {e}")
        else:
            print(f"No cache found for device: {serial}")

    def add_or_update_app_item(self, package, label, icon_path):
        icon = get_launcher_icon(icon_path, label)

        if package in self.displayed_packages:
            # Update existing item
            item = self.displayed_packages[package]
            item.setText(label)
            item.setIcon(icon)
        else:
            # Create new item
            item = QStandardItem(icon, label)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            )
            item.setData(package, Qt.ItemDataRole.UserRole)
            self.model.appendRow(item)
            self.displayed_packages[package] = item

        # Keep sorted alphabetically
        self.proxy_model.sort(0, Qt.SortOrder.AscendingOrder)

    def on_apps_loaded(self, apps_list):
        for app in apps_list:
            self.add_or_update_app_item(
                app["package"], app["label"], app["icon_path"]
            )

    def on_app_resolved(self, app):
        self.add_or_update_app_item(
            app["package"], app["label"], app["icon_path"]
        )

    def on_item_clicked(self, index):
        source_index = self.proxy_model.mapToSource(index)
        item = self.model.itemFromIndex(source_index)
        package_name = item.data(Qt.ItemDataRole.UserRole)

        print(f"Launching {package_name} via scrcpy...")
        cmd = [
            "scrcpy",
            "--new-display",
            "--flex-display",
            "--no-vd-system-decorations",
            f"--start-app={package_name}",
            "--keep-active",
        ]
        try:
            subprocess.Popen(cmd)
        except Exception as e:
            print(f"Error launching scrcpy: {e}")

    def closeEvent(self, event):
        self.monitor_thread.running = False
        self.monitor_thread.wait()
        super().closeEvent(event)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrcpy App Launcher")
    parser.add_argument(
        "--clean-cache",
        "-c",
        action="store_true",
        help="Clean the cache for the connected device",
    )
    cli_args, _ = parser.parse_known_args()

    if cli_args.clean_cache:
        try:
            res = subprocess.run(
                ["adb", "get-state"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if res.stdout.strip() == "device":
                serial = subprocess.run(
                    ["adb", "get-serialno"],
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                safe_serial = "".join(
                    c for c in serial if c.isalnum() or c in ("-", "_")
                )
                if safe_serial:
                    device_dir = os.path.join(CACHE_DIR, safe_serial)
                    if os.path.exists(device_dir):
                        shutil.rmtree(device_dir)
                        print(f"Cleared cache directory: {device_dir}")
                    else:
                        print(f"No cache found for device: {serial}")
            else:
                print("No active ADB device detected. Cannot clear cache.")
        except Exception as e:
            print(f"Error cleaning cache: {e}")

    app = QApplication(sys.argv)

    if "breeze" in QIcon.themeSearchPaths():
        QIcon.setThemeName("breeze")

    window = LauncherApp()
    window.show()
    sys.exit(app.exec())
