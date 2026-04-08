#!/usr/bin/env python3
"""
HiBy M500 Battery Monitor - macOS Menu Bar App + Dashboard Window

メニューバーのアイコンをクリック → ドロップダウンメニューでバッテリー情報を表示
「ダッシュボードを開く」を選択 → アプリウィンドウ（WKWebView）でリッチな
ミクカラーのダッシュボードを表示。ブラウザは使わない。

USB接続を検出すると自動でWiFi ADBに切り替え、ケーブルを外しても監視を継続。

必要条件:
  - Python 3.9+
  - pyobjc-framework-Cocoa, pyobjc-framework-WebKit
  - adb (brew install android-platform-tools)
  - HiBy M500のUSBデバッグが有効であること
"""

import subprocess
import json
import re
import time
import threading
import logging
import signal
from dataclasses import dataclass, field
from http.server import HTTPServer, SimpleHTTPRequestHandler
from typing import Optional
from pathlib import Path

# ── PyObjC ──────────────────────────────────────────────
try:
    import objc
    from AppKit import (
        NSApplication,
        NSStatusBar,
        NSVariableStatusItemLength,
        NSObject,
        NSMenu,
        NSMenuItem,
        NSWindow,
        NSBackingStoreBuffered,
        NSMakeRect,
        NSColor,
        NSScreen,
        NSFont,
        NSMutableAttributedString,
        NSForegroundColorAttributeName,
        NSFontAttributeName,
        NSImage,
        NSAlert,
        NSButton,
    )
    from WebKit import WKWebView, WKWebViewConfiguration
    from Foundation import (
        NSURL, NSURLRequest, NSTimer, NSRunLoop, NSDefaultRunLoopMode,
        NSMakeRange, NSUserDefaults,
    )
except ImportError as _imp_err:
    import traceback
    _err_path = Path("~/Library/Logs/HibyM500Monitor.log").expanduser()
    _err_path.parent.mkdir(parents=True, exist_ok=True)
    with open(_err_path, "a") as _f:
        _f.write(f"ImportError: {_imp_err}\n")
        traceback.print_exc(file=_f)
    print(f"Error: PyObjC が必要です。({_imp_err})")
    print("  pip install pyobjc-framework-Cocoa pyobjc-framework-WebKit")
    raise SystemExit(1)

# ── ログ設定 ─────────────────────────────────────────────
LOG_PATH = Path("~/Library/Logs/HibyM500Monitor.log").expanduser()
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("hiby_monitor")

# ── 設定 ─────────────────────────────────────────────────
POLL_INTERVAL_SEC = 30
LOW_BATTERY_THRESHOLD = 20
CRITICAL_BATTERY_THRESHOLD = 10
ADB_TIMEOUT_SEC = 5
WIFI_ADB_TIMEOUT_SEC = 8
API_PORT = 39539
WIFI_ADB_PORT = 5555

DEVICE_SERIAL: Optional[str] = None

SCRIPT_DIR = Path(__file__).resolve().parent
DASHBOARD_PATH = SCRIPT_DIR / "dashboard.html"
CONFIG_PATH = SCRIPT_DIR / ".hiby_monitor_config.json"

# ダッシュボードウィンドウサイズ
WINDOW_WIDTH = 460
WINDOW_HEIGHT = 780


# ── 接続モード ───────────────────────────────────────────
class ConnectionMode:
    DISCONNECTED = "disconnected"
    USB = "usb"
    WIFI = "wifi"


# ── データクラス ──────────────────────────────────────────
@dataclass
class BatteryInfo:
    level: int = -1
    status: str = "unknown"
    health: str = "unknown"
    temperature: float = 0.0
    voltage: float = 0.0
    technology: str = "unknown"
    plugged: str = "none"
    raw: dict = field(default_factory=dict)

    @property
    def status_jp(self) -> str:
        m = {"charging": "充電中", "discharging": "放電中",
             "full": "満充電", "not charging": "未充電"}
        return m.get(self.status.lower(), self.status)

    @property
    def health_jp(self) -> str:
        m = {"good": "良好", "overheat": "高温", "dead": "寿命",
             "over voltage": "過電圧", "cold": "低温"}
        return m.get(self.health.lower(), self.health)

    @property
    def plugged_jp(self) -> str:
        m = {"none": "なし", "usb": "USB", "ac": "AC", "wireless": "ワイヤレス"}
        return m.get(self.plugged.lower(), self.plugged)

    def to_dict(self) -> dict:
        return {
            "level": self.level, "status": self.status_jp,
            "health": self.health_jp,
            "temperature": f"{self.temperature:.1f}",
            "voltage": f"{self.voltage:.0f}",
            "technology": self.technology, "plugged": self.plugged_jp,
        }


@dataclass
class DeviceInfo:
    model: str = "Unknown"
    manufacturer: str = "Unknown"
    brand: str = "Unknown"
    soc_model: str = "Unknown"
    hardware: str = "Unknown"
    cpu_abi: str = "Unknown"
    cpu_cores: int = 0
    cpu_freq_max: str = "Unknown"
    dac_chip: str = "Unknown"
    ram_total: str = "Unknown"
    android_version: str = "Unknown"
    sdk_version: str = "Unknown"
    build_display: str = "Unknown"
    security_patch: str = "Unknown"
    screen_density: str = "Unknown"
    screen_resolution: str = "Unknown"
    wifi_ssid: str = "Unknown"
    bluetooth_name: str = "Unknown"
    serial_number: str = "Unknown"
    kernel_version: str = "Unknown"

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class WirelessInfo:
    """WiFi / Bluetooth の動的ステータス"""
    wifi_on: bool = False
    wifi_ssid: str = ""
    wifi_rssi: int = 0           # dBm
    wifi_link_speed: str = ""    # e.g. "72Mbps"
    wifi_ip: str = ""
    wifi_mac: str = ""
    bt_on: bool = False
    bt_connected_device: str = ""
    bt_codec: str = ""           # LDAC, aptX HD, SBC etc.
    bt_mac: str = ""

    def wifi_signal_label(self) -> str:
        if not self.wifi_on:
            return "OFF"
        if self.wifi_rssi == 0 or not self.wifi_ssid:
            return "未接続"
        if self.wifi_rssi >= -50:
            return "強い"
        if self.wifi_rssi >= -70:
            return "普通"
        return "弱い"

    def bt_status_label(self) -> str:
        if not self.bt_on:
            return "OFF"
        return self.bt_connected_device if self.bt_connected_device else "未接続"

    def to_dict(self) -> dict:
        return {
            "wifi_on": self.wifi_on,
            "wifi_ssid": self.wifi_ssid,
            "wifi_rssi": self.wifi_rssi,
            "wifi_signal": self.wifi_signal_label(),
            "wifi_link_speed": self.wifi_link_speed,
            "wifi_ip": self.wifi_ip,
            "wifi_mac": self.wifi_mac,
            "bt_on": self.bt_on,
            "bt_connected_device": self.bt_connected_device,
            "bt_codec": self.bt_codec,
            "bt_mac": self.bt_mac,
            "bt_status": self.bt_status_label(),
        }


# ── 共有ステート ─────────────────────────────────────────
class SharedState:
    def __init__(self):
        self.connected = False
        self.model = "HiBy M500"
        self.battery: Optional[BatteryInfo] = None
        self.device: Optional[DeviceInfo] = None
        self.wireless: Optional[WirelessInfo] = None
        self.storage: Optional[str] = None
        self.uptime: Optional[str] = None
        self.connection_mode = ConnectionMode.DISCONNECTED
        self.wifi_ip: Optional[str] = None
        self._lock = threading.Lock()

    def update(self, connected, model, battery, storage, uptime,
               connection_mode=ConnectionMode.DISCONNECTED, wifi_ip=None,
               device=None, wireless=None):
        with self._lock:
            self.connected = connected
            self.model = model
            self.battery = battery
            self.storage = storage
            self.uptime = uptime
            self.connection_mode = connection_mode
            self.wifi_ip = wifi_ip
            if device is not None:
                self.device = device
            if wireless is not None:
                self.wireless = wireless

    def to_json(self) -> str:
        with self._lock:
            data = {
                "connected": self.connected,
                "model": self.model,
                "battery": self.battery.to_dict() if self.battery else None,
                "device": self.device.to_dict() if self.device else None,
                "wireless": self.wireless.to_dict() if self.wireless else None,
                "storage": self.storage,
                "uptime": self.uptime,
                "connection_mode": self.connection_mode,
                "wifi_ip": self.wifi_ip,
            }
        return json.dumps(data, ensure_ascii=False)


shared_state = SharedState()


# ── 設定の永続化 ─────────────────────────────────────────
def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    return {}


def save_config(config: dict):
    try:
        CONFIG_PATH.write_text(json.dumps(config, indent=2))
    except Exception as e:
        log.warning("設定保存に失敗: %s", e)


# ── ADB ヘルパー ─────────────────────────────────────────
def run_adb(*args: str, serial: Optional[str] = None,
            timeout: int = ADB_TIMEOUT_SEC) -> Optional[str]:
    cmd = ["adb"]
    target = serial or DEVICE_SERIAL
    if target:
        cmd += ["-s", target]
    cmd += list(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return result.stdout.strip()
        log.warning("adb failed (%s): %s", " ".join(cmd), result.stderr.strip())
        return None
    except FileNotFoundError:
        log.error("adb が見つかりません。")
        return None
    except subprocess.TimeoutExpired:
        log.warning("adb タイムアウト (%ds)", timeout)
        return None


def list_connected_devices() -> list[tuple[str, str]]:
    out = run_adb("devices", "-l")
    if out is None:
        return []
    devices = []
    for line in out.strip().split("\n")[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serial = parts[0]
            dtype = "wifi" if re.match(r"\d+\.\d+\.\d+\.\d+:\d+", serial) else "usb"
            devices.append((serial, dtype))
    return devices


def get_device_ip(serial: Optional[str] = None) -> Optional[str]:
    out = run_adb("shell", "ip", "route", serial=serial)
    if out:
        m = re.search(r"src\s+(\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    out2 = run_adb("shell", "ip", "addr", "show", "wlan0", serial=serial)
    if out2:
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out2)
        if m:
            return m.group(1)
    return None


def get_device_model(serial: Optional[str] = None) -> str:
    out = run_adb("shell", "getprop", "ro.product.model", serial=serial)
    return out if out else "Unknown"


# ── WiFi ADB マネージャー ─────────────────────────────────
class WiFiADBManager:
    def __init__(self):
        self.wifi_ip: Optional[str] = None
        self.wifi_serial: Optional[str] = None
        self.usb_serial: Optional[str] = None
        self.mode = ConnectionMode.DISCONNECTED
        self._wifi_setup_done = False
        self._reconnect_attempts = 0
        self._max_reconnect = 5
        config = load_config()
        saved_ip = config.get("wifi_ip")
        if saved_ip:
            self.wifi_ip = saved_ip
            self.wifi_serial = f"{saved_ip}:{WIFI_ADB_PORT}"

    @property
    def active_serial(self) -> Optional[str]:
        if self.mode == ConnectionMode.WIFI:
            return self.wifi_serial
        elif self.mode == ConnectionMode.USB:
            return self.usb_serial
        return None

    def detect_and_manage(self) -> str:
        devices = list_connected_devices()
        usb = [(s, t) for s, t in devices if t == "usb"]
        wifi = [(s, t) for s, t in devices if t == "wifi"]

        if usb:
            self.usb_serial = usb[0][0]
            self._reconnect_attempts = 0
            if not self._wifi_setup_done:
                self._setup_wifi_adb(self.usb_serial)
            self.mode = ConnectionMode.USB
            return self.mode
        if wifi:
            self.wifi_serial = wifi[0][0]
            self.mode = ConnectionMode.WIFI
            self._reconnect_attempts = 0
            return self.mode
        if self.wifi_ip and self._reconnect_attempts < self._max_reconnect:
            if self._try_wifi_connect(self.wifi_ip):
                self.mode = ConnectionMode.WIFI
                self._reconnect_attempts = 0
                return self.mode
            self._reconnect_attempts += 1
        self.mode = ConnectionMode.DISCONNECTED
        return self.mode

    def _setup_wifi_adb(self, usb_serial: str):
        ip = get_device_ip(serial=usb_serial)
        if not ip:
            return
        run_adb("tcpip", str(WIFI_ADB_PORT), serial=usb_serial,
                timeout=WIFI_ADB_TIMEOUT_SEC)
        time.sleep(2)
        if self._try_wifi_connect(ip):
            self.wifi_ip = ip
            self.wifi_serial = f"{ip}:{WIFI_ADB_PORT}"
            self._wifi_setup_done = True
            save_config({"wifi_ip": ip})

    def _try_wifi_connect(self, ip: str) -> bool:
        target = f"{ip}:{WIFI_ADB_PORT}"
        result = run_adb("connect", target, timeout=WIFI_ADB_TIMEOUT_SEC)
        if result and "connected" in result.lower():
            self.wifi_serial = target
            return True
        return False

    def reset(self):
        self._wifi_setup_done = False
        self._reconnect_attempts = 0
        self.mode = ConnectionMode.DISCONNECTED


wifi_manager = WiFiADBManager()


# ── デバイス情報取得 ──────────────────────────────────────
def _adb_timeout():
    return WIFI_ADB_TIMEOUT_SEC if wifi_manager.mode == ConnectionMode.WIFI else ADB_TIMEOUT_SEC


def fetch_battery_info(serial: Optional[str] = None) -> Optional[BatteryInfo]:
    out = run_adb("shell", "dumpsys", "battery", serial=serial, timeout=_adb_timeout())
    if out is None:
        return None
    info = BatteryInfo()
    for line in out.split("\n"):
        line = line.strip()
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip().lower(), val.strip()
        if key == "level":
            info.level = int(val)
        elif key == "status":
            info.status = {"1": "unknown", "2": "charging", "3": "discharging",
                           "4": "not charging", "5": "full"}.get(val, val)
        elif key == "health":
            info.health = {"1": "unknown", "2": "good", "3": "overheat", "4": "dead",
                           "5": "over voltage", "6": "unspecified failure",
                           "7": "cold"}.get(val, val)
        elif key == "temperature":
            info.temperature = int(val) / 10.0
        elif key == "voltage":
            info.voltage = int(val)
        elif key == "technology":
            info.technology = val
        elif key == "plugged":
            info.plugged = {"0": "none", "1": "ac", "2": "usb", "4": "wireless"}.get(val, val)
    return info


def fetch_storage_info(serial: Optional[str] = None) -> Optional[str]:
    out = run_adb("shell", "df", "-h", "/storage/emulated/0",
                  serial=serial, timeout=_adb_timeout())
    if out is None:
        return None
    lines = out.strip().split("\n")
    if len(lines) >= 2:
        parts = lines[1].split()
        if len(parts) >= 5:
            return f"{parts[2]} / {parts[1]} ({parts[4]})"
    return None


def fetch_uptime(serial: Optional[str] = None) -> Optional[str]:
    out = run_adb("shell", "uptime", "-p", serial=serial, timeout=_adb_timeout())
    if not out:
        return None
    return _format_uptime_jp(out)


def _format_uptime_jp(raw: str) -> str:
    """'up 0 weeks, 0 days, 2 hours, 35 minutes, ...' → '2時間35分' に変換。"""
    raw = raw.strip().lstrip("up").strip(" ,")
    weeks = days = hours = mins = 0
    for part in raw.split(","):
        part = part.strip()
        m = re.match(r"(\d+)\s*(week|day|hour|minute|min)", part)
        if m:
            n = int(m.group(1))
            unit = m.group(2)
            if "week" in unit:
                weeks = n
            elif "day" in unit:
                days = n
            elif "hour" in unit:
                hours = n
            elif "min" in unit:
                mins = n
    # 日数に週を加算
    days += weeks * 7
    parts = []
    if days > 0:
        parts.append(f"{days}日")
    if hours > 0:
        parts.append(f"{hours}時間")
    if mins > 0:
        parts.append(f"{mins}分")
    return "".join(parts) if parts else "0分"


def fetch_device_info(serial: Optional[str] = None) -> Optional[DeviceInfo]:
    timeout = _adb_timeout()

    def _prop(key: str) -> str:
        out = run_adb("shell", "getprop", key, serial=serial, timeout=timeout)
        return out if out else "Unknown"

    info = DeviceInfo()
    info.model = _prop("ro.product.model")
    info.manufacturer = _prop("ro.product.manufacturer")
    info.brand = _prop("ro.product.brand")
    info.soc_model = _prop("ro.board.platform")
    info.hardware = _prop("ro.hardware")
    info.cpu_abi = _prop("ro.product.cpu.abi")

    for key, friendly in {"sm6225": "Qualcomm Snapdragon 680 (6nm)",
                          "bengal": "Qualcomm Snapdragon 680 (6nm)"}.items():
        if key in info.soc_model.lower():
            info.soc_model = friendly
            break

    cpuinfo = run_adb("shell", "cat", "/proc/cpuinfo", serial=serial, timeout=timeout)
    if cpuinfo:
        info.cpu_cores = max(cpuinfo.lower().count("processor\t:"),
                             cpuinfo.lower().count("processor  :"), 1)

    freq_out = run_adb("shell", "cat",
                       "/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq",
                       serial=serial, timeout=timeout)
    if freq_out and freq_out.isdigit():
        info.cpu_freq_max = f"{int(freq_out) / 1000:.0f} MHz"

    if "m500" in info.model.lower():
        info.dac_chip = "Dual Cirrus Logic CS43198"

    meminfo = run_adb("shell", "cat", "/proc/meminfo", serial=serial, timeout=timeout)
    if meminfo:
        m = re.search(r"MemTotal:\s+(\d+)\s+kB", meminfo)
        if m:
            gb = int(m.group(1)) / (1024 * 1024)
            info.ram_total = f"{round(gb)} GB ({gb:.1f} GB 実効)"

    info.android_version = _prop("ro.build.version.release")
    info.sdk_version = _prop("ro.build.version.sdk")
    info.build_display = _prop("ro.build.display.id")
    info.security_patch = _prop("ro.build.version.security_patch")
    info.screen_density = _prop("ro.sf.lcd_density")

    wm_size = run_adb("shell", "wm", "size", serial=serial, timeout=timeout)
    if wm_size:
        m = re.search(r"(\d+x\d+)", wm_size)
        if m:
            info.screen_resolution = m.group(1)

    info.bluetooth_name = _prop("ro.bluetooth.name")
    if info.bluetooth_name == "Unknown":
        info.bluetooth_name = _prop("persist.bluetooth.name")
    info.serial_number = _prop("ro.serialno")
    kernel = run_adb("shell", "uname", "-r", serial=serial, timeout=timeout)
    if kernel:
        info.kernel_version = kernel
    return info


def fetch_wireless_info(serial: Optional[str] = None) -> Optional[WirelessInfo]:
    """WiFi / Bluetooth のリアルタイム状態を取得"""
    timeout = _adb_timeout()
    info = WirelessInfo()

    # ── WiFi ──
    wifi_on = run_adb("shell", "settings", "get", "global", "wifi_on",
                       serial=serial, timeout=timeout)
    info.wifi_on = wifi_on and wifi_on.strip() == "1"

    if info.wifi_on:
        wifi_dump = run_adb("shell", "dumpsys", "wifi",
                            serial=serial, timeout=timeout)
        if wifi_dump:
            # mWifiInfo から SSID, RSSI, Link speed を抽出
            winfo_match = re.search(r'mWifiInfo\s+(.+)', wifi_dump)
            if winfo_match:
                wline = winfo_match.group(1)
                ssid_m = re.search(r'SSID:\s*"?([^",]+)"?', wline)
                if ssid_m:
                    info.wifi_ssid = ssid_m.group(1).strip()
                rssi_m = re.search(r'RSSI:\s*(-?\d+)', wline)
                if rssi_m:
                    info.wifi_rssi = int(rssi_m.group(1))
                speed_m = re.search(r'Link speed:\s*(\d+\w+)', wline)
                if speed_m:
                    info.wifi_link_speed = speed_m.group(1)

        # IP / MAC
        ip_out = run_adb("shell", "ip", "addr", "show", "wlan0",
                         serial=serial, timeout=timeout)
        if ip_out:
            ip_m = re.search(r'inet\s+([\d.]+)/', ip_out)
            if ip_m:
                info.wifi_ip = ip_m.group(1)
            mac_m = re.search(r'link/ether\s+([\w:]+)', ip_out)
            if mac_m:
                info.wifi_mac = mac_m.group(1)

    # ── Bluetooth ──
    bt_on = run_adb("shell", "settings", "get", "global", "bluetooth_on",
                     serial=serial, timeout=timeout)
    info.bt_on = bt_on and bt_on.strip() == "1"

    if info.bt_on:
        bt_dump = run_adb("shell", "dumpsys", "bluetooth_manager",
                          serial=serial, timeout=timeout)
        if bt_dump:
            # 接続中デバイス名を探す
            conn_m = re.search(
                r'(?:Connected|Active)\s+(?:device|devices?).*?name[=:]\s*(\S+)',
                bt_dump, re.IGNORECASE)
            if conn_m:
                info.bt_connected_device = conn_m.group(1).strip()

            # A2DP コーデック情報
            codec_m = re.search(r'(?:codec|Codec)[=:\s]+(\w[\w\s]*?)(?:\n|,|\()',
                                bt_dump)
            if codec_m:
                info.bt_codec = codec_m.group(1).strip()

            # BT MAC
            addr_m = re.search(r'address[=:\s]+([\dA-Fa-f:]{17})', bt_dump)
            if addr_m:
                info.bt_mac = addr_m.group(1)

    return info


# ── ヘルパー ─────────────────────────────────────────────
def battery_icon(level: int, charging: bool) -> str:
    if charging:
        return "⚡"
    return "🔋" if level >= 50 else "🪫"


def connection_icon(mode: str) -> str:
    return {"wifi": "📶 ", "usb": "🔌 "}.get(mode, "")


def send_notification(title: str, subtitle: str, message: str):
    try:
        script = (f'display notification "{message}" '
                  f'with title "{title}" subtitle "{subtitle}"')
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log.warning("通知送信に失敗: %s", e)


# ── ローカル API サーバー ─────────────────────────────────
class APIHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SCRIPT_DIR), **kwargs)

    def do_GET(self):
        if self.path == "/api/status":
            body = shared_state.to_json().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/", "/dashboard"):
            self.path = "/dashboard.html"
            super().do_GET()
        else:
            super().do_GET()

    def log_message(self, fmt, *args):
        log.debug("HTTP: " + fmt % args)


def start_api_server():
    HTTPServer(("127.0.0.1", API_PORT), APIHandler).serve_forever()


# ══════════════════════════════════════════════════════════
#  macOS アプリ本体: NSMenu + NSWindow(WKWebView)
# ══════════════════════════════════════════════════════════

WELCOME_PREF_KEY = "HiByM500_HideWelcome"


class AppDelegate(NSObject):
    """メニューバー常駐 + ダッシュボードウィンドウ管理。"""

    # ── 初期化 ────────────────────────────────────────────
    def applicationDidFinishLaunching_(self, notification):
        self._show_welcome_if_needed()
        self._low_notified = False
        self._critical_notified = False
        self._wifi_notified = False
        self._device_info_fetched = False
        self.model_name = "HiBy M500"
        self._dashboard_window = None

        # ── ステータスアイテム ──
        self.statusItem = NSStatusBar.systemStatusBar() \
            .statusItemWithLength_(NSVariableStatusItemLength)
        self.statusItem.setTitle_("🎵 --")
        self.statusItem.setHighlightMode_(True)

        # ── ドロップダウンメニュー（ダッシュボード風テーマ） ──
        self._init_colors()
        self.menu = NSMenu.alloc().init()
        self.menu.setAutoenablesItems_(False)
        # メニュー最小幅を確保
        self.menu.setMinimumWidth_(260)

        # ── ヘッダー: デバイス名 ──
        self.mi_device = self._add_item("")
        self.mi_device.setAttributedTitle_(
            self._styled("♪", "HiBy M500 ミク Edition", self._white, 10, 13))
        self.mi_connection = self._add_item("")
        self.menu.addItem_(NSMenuItem.separatorItem())

        # ── セクション: バッテリー ──
        self.menu.addItem_(self._section_header("── BATTERY ──"))
        self.mi_battery = self._add_item("")
        self.mi_status = self._add_item("")
        self.mi_health = self._add_item("")
        self.menu.addItem_(NSMenuItem.separatorItem())

        # ── セクション: ステータス ──
        self.menu.addItem_(self._section_header("── STATUS ──"))
        self.mi_temp = self._add_item("")
        self.mi_voltage = self._add_item("")
        self.mi_storage = self._add_item("")
        self.mi_uptime = self._add_item("")
        self.menu.addItem_(NSMenuItem.separatorItem())

        # ── セクション: ワイヤレス ──
        self.menu.addItem_(self._section_header("── WIRELESS ──"))
        self.mi_wifi = self._add_item("")
        self.mi_bt = self._add_item("")
        self.menu.addItem_(NSMenuItem.separatorItem())

        # ── アクション ──
        self._add_action_item("🖥  ダッシュボードを開く", "openDashboard:")
        self._add_action_item("🔄 今すぐ更新", "refreshNow:")
        self._add_action_item("🔁 再接続", "reconnect:")
        self._add_action_item("❓ セットアップ手順", "showWelcome:")
        self.menu.addItem_(NSMenuItem.separatorItem())

        # 終了は NSApp に直接送る
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "終了", "terminate:", "q"
        )
        quit_item.setTarget_(NSApplication.sharedApplication())
        self.menu.addItem_(quit_item)

        # 初期テーマ適用
        self._apply_disconnected_style()

        self.statusItem.setMenu_(self.menu)

        # ── ポーリングタイマー ──
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            POLL_INTERVAL_SEC, self, b"pollTick:", None, True,
        )
        NSRunLoop.currentRunLoop().addTimer_forMode_(
            self._timer, NSDefaultRunLoopMode
        )

        # 初回ポーリング
        self._poll()

    # ── ウェルカムダイアログ ──────────────────────────────
    @objc.python_method
    def _show_welcome_if_needed(self):
        """初回起動時にセットアップ手順を表示。チェックで次回以降非表示。"""
        defaults = NSUserDefaults.standardUserDefaults()
        if defaults.boolForKey_(WELCOME_PREF_KEY):
            return

        # Accessory ポリシーだとアラートが裏に隠れるので一時的に前面化
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(0)  # NSApplicationActivationPolicyRegular
        app.activateIgnoringOtherApps_(True)

        alert = NSAlert.alloc().init()
        alert.setMessageText_("HiBy M500 Monitor へようこそ")
        alert.setInformativeText_(
            "このアプリは HiBy M500 に ADB (Android Debug Bridge) で"
            "接続し、バッテリーやデバイス情報をメニューバーに表示します。\n\n"
            "━━ 初回セットアップ手順 ━━\n\n"
            "① M500 で USBデバッグ を有効にする\n"
            "   設定 → デバイスについて → ビルド番号を7回タップ\n"
            "   → 開発者向けオプション → USBデバッグ ON\n\n"
            "② M500 を USB ケーブルで Mac に接続\n\n"
            "③ M500 の画面に「USBデバッグを許可しますか？」\n"
            "   と表示されたら「OK」をタップ\n"
            "   ※「このパソコンからのUSBデバッグを常に\n"
            "     許可する」にチェック推奨\n\n"
            "━━ WiFi ADB 自動切替 ━━\n\n"
            "USB 接続で認識後、自動的に WiFi ADB に移行します。\n"
            "ケーブルを外しても WiFi 経由で監視を継続できます。"
        )
        alert.setAlertStyle_(0)  # NSInformationalAlertStyle
        alert.addButtonWithTitle_("OK、始める")

        # ── 「次回から表示しない」チェックボックス ──
        checkbox = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 250, 20))
        checkbox.setButtonType_(3)  # NSSwitchButton
        checkbox.setTitle_("次回から表示しない")
        checkbox.setState_(0)
        alert.setAccessoryView_(checkbox)

        alert.runModal()

        # メニューバー常駐モードに戻す
        app.setActivationPolicy_(1)  # NSApplicationActivationPolicyAccessory

        # チェックされていたら次回非表示
        if checkbox.state() == 1:
            defaults.setBool_forKey_(True, WELCOME_PREF_KEY)
            defaults.synchronize()
            log.info("ウェルカムダイアログ: 次回から非表示に設定")
        else:
            log.info("ウェルカムダイアログ: 表示")

    # ── テーマカラー ─────────────────────────────────────
    _teal = None
    _pink = None
    _gray = None
    _white = None
    _dim = None

    @classmethod
    @objc.python_method
    def _init_colors(cls):
        if cls._teal is None:
            cls._teal = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.224, 0.773, 0.733, 1.0)       # #39C5BB
            cls._pink = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.910, 0.640, 0.710, 1.0)       # #E8A3B5
            cls._gray = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.545, 0.580, 0.620, 1.0)       # #8B949E
            cls._white = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.902, 0.929, 0.953, 1.0)       # #E6EDF3
            cls._dim = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.42, 0.45, 0.48, 1.0)          # #6B7279

    @objc.python_method
    def _styled(self, label: str, value: str, color=None,
                label_size: float = 11, value_size: float = 12) -> NSMutableAttributedString:
        """ラベル(グレー) + 値(カラー) の Attributed String を返す"""
        self._init_colors()
        lbl_font = NSFont.systemFontOfSize_weight_(label_size, 0.0)   # Regular
        val_font = NSFont.monospacedDigitSystemFontOfSize_weight_(value_size, 0.3)
        c = color or self._teal
        text = f"{label}  {value}"
        attr = NSMutableAttributedString.alloc().initWithString_(text)
        # NSAttributedString は UTF-16 単位で範囲を計算するため、
        # 絵文字（サロゲートペア）を含む場合 Python len() ではズレる
        lbl_u16 = len(label.encode("utf-16-le")) // 2
        val_u16 = len(value.encode("utf-16-le")) // 2
        label_len = lbl_u16 + 2  # +2 はセパレータの半角スペース
        attr.addAttribute_value_range_(
            NSForegroundColorAttributeName, self._dim,
            NSMakeRange(0, label_len))
        attr.addAttribute_value_range_(
            NSFontAttributeName, lbl_font,
            NSMakeRange(0, label_len))
        attr.addAttribute_value_range_(
            NSForegroundColorAttributeName, c,
            NSMakeRange(label_len, val_u16))
        attr.addAttribute_value_range_(
            NSFontAttributeName, val_font,
            NSMakeRange(label_len, val_u16))
        return attr

    @objc.python_method
    def _section_header(self, text: str) -> NSMenuItem:
        """セクションヘッダー（ミクティール、小さめ太字）"""
        self._init_colors()
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", None, "")
        item.setEnabled_(False)
        attr = NSMutableAttributedString.alloc().initWithString_(text)
        hdr_font = NSFont.systemFontOfSize_weight_(10, 0.5)
        attr.addAttribute_value_range_(
            NSForegroundColorAttributeName, self._teal,
            NSMakeRange(0, len(text.encode("utf-16-le")) // 2))
        attr.addAttribute_value_range_(
            NSFontAttributeName, hdr_font,
            NSMakeRange(0, len(text.encode("utf-16-le")) // 2))
        item.setAttributedTitle_(attr)
        return item

    # ── メニュー項目ヘルパー ──────────────────────────────
    @objc.python_method
    def _add_item(self, title: str) -> NSMenuItem:
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, None, ""
        )
        item.setEnabled_(False)
        self.menu.addItem_(item)
        return item

    @objc.python_method
    def _add_action_item(self, title: str, action: str):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, action, ""
        )
        item.setTarget_(self)
        self.menu.addItem_(item)

    # ── メニューアクション ────────────────────────────────
    @objc.IBAction
    def openDashboard_(self, sender):
        """ダッシュボードウィンドウを表示。"""
        log.info("openDashboard_ called")
        try:
            if self._dashboard_window is not None:
                if self._dashboard_window.isVisible():
                    self._dashboard_window.makeKeyAndOrderFront_(None)
                    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
                    return
                else:
                    # ウィンドウは閉じられている → 再作成
                    self._dashboard_window = None

            self._create_dashboard_window()
        except Exception as e:
            log.error("ダッシュボードウィンドウ作成に失敗: %s", e)
            # フォールバック: ブラウザで開く
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{API_PORT}/")

    @objc.python_method
    def _create_dashboard_window(self):
        """NSWindow + WKWebView でダッシュボードを作成。"""
        # ウィンドウスタイル（整数直指定で互換性を確保）
        #   Titled=1, Closable=2, Miniaturizable=4, Resizable=8,
        #   FullSizeContentView=32768
        style = 1 | 2 | 4 | 8 | (1 << 15)

        # 画面中央に配置
        screen = NSScreen.mainScreen().frame()
        x = (screen.size.width - WINDOW_WIDTH) / 2
        y = (screen.size.height - WINDOW_HEIGHT) / 2
        frame = NSMakeRect(x, y, WINDOW_WIDTH, WINDOW_HEIGHT)

        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False
        )
        window.setTitle_("HiBy M500 Monitor")
        window.setMinSize_((360, 500))
        window.setReleasedWhenClosed_(False)

        # タイトルバー透過（ダッシュボードの背景色とシームレスに）
        try:
            window.setTitlebarAppearsTransparent_(True)
            window.setTitleVisibility_(1)  # NSWindowTitleHidden
        except Exception:
            pass  # 古い macOS では無視

        # 背景色（#0d1117）
        try:
            bg = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                13 / 255, 17 / 255, 23 / 255, 1.0
            )
            window.setBackgroundColor_(bg)
        except Exception:
            pass

        # ── WKWebView ──
        wk_config = WKWebViewConfiguration.alloc().init()
        webview = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, WINDOW_WIDTH, WINDOW_HEIGHT), wk_config
        )
        webview.setAutoresizingMask_(0x02 | 0x10)  # flexible width + height

        # HTTP URL でロード（キャッシュ無効 + API サーバー経由）
        url = NSURL.URLWithString_(f"http://127.0.0.1:{API_PORT}/")
        # NSURLRequestReloadIgnoringLocalCacheData = 1
        request = NSURLRequest.requestWithURL_cachePolicy_timeoutInterval_(
            url, 1, 30
        )
        webview.loadRequest_(request)

        # 背景透過
        try:
            webview.setValue_forKey_(False, "drawsBackground")
        except Exception:
            pass

        window.setContentView_(webview)
        window.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

        self._dashboard_window = window
        self._webview = webview
        log.info("ダッシュボードウィンドウを作成しました")

    @objc.IBAction
    def showWelcome_(self, sender):
        """メニューからセットアップ手順を再表示（常に表示、チェック状態もリセット）。"""
        defaults = NSUserDefaults.standardUserDefaults()
        defaults.setBool_forKey_(False, WELCOME_PREF_KEY)
        defaults.synchronize()
        self._show_welcome_if_needed()

    @objc.IBAction
    def refreshNow_(self, sender):
        self._poll()

    @objc.IBAction
    def reconnect_(self, sender):
        wifi_manager.reset()
        send_notification("HiBy M500 Monitor", "再接続中...",
                          "USB接続してください。WiFi ADB を再セットアップします。")
        self._poll()

    # ── ポーリング ────────────────────────────────────────
    def pollTick_(self, timer):
        self._poll()

    @objc.python_method
    def _poll(self):
        threading.Thread(target=self._fetch_and_update, daemon=True).start()

    @objc.python_method
    def _fetch_and_update(self):
        mode = wifi_manager.detect_and_manage()
        serial = wifi_manager.active_serial

        if mode == ConnectionMode.DISCONNECTED:
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                b"updateMenuDisconnected:", None, False
            )
            shared_state.update(False, self.model_name, None, None, None,
                                ConnectionMode.DISCONNECTED)
            self._low_notified = False
            self._critical_notified = False
            self._wifi_notified = False
            return

        if self.model_name == "HiBy M500":
            m = get_device_model(serial=serial)
            if m and m != "Unknown":
                self.model_name = m

        if not self._device_info_fetched:
            di = fetch_device_info(serial=serial)
            if di:
                self._device_info_fetched = True
                shared_state.update(True, self.model_name, None, None, None,
                                    mode, wifi_manager.wifi_ip, device=di)

        battery = fetch_battery_info(serial=serial)
        storage = fetch_storage_info(serial=serial)
        uptime = fetch_uptime(serial=serial)
        wireless = fetch_wireless_info(serial=serial)

        if battery:
            shared_state.update(True, self.model_name, battery, storage, uptime,
                                mode, wifi_manager.wifi_ip, wireless=wireless)
            # メインスレッドで UI 更新（辞書を渡す）
            payload = {
                "battery": battery, "storage": storage,
                "uptime": uptime, "mode": mode,
                "wireless": wireless,
            }
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                b"updateMenuConnected:", payload, False
            )
            self._check_low_battery(battery)
            if mode == ConnectionMode.WIFI and not self._wifi_notified:
                send_notification("HiBy M500 Monitor",
                                  "WiFi ADB に切り替えました",
                                  f"IP: {wifi_manager.wifi_ip}")
                self._wifi_notified = True
        else:
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                b"updateMenuDisconnected:", None, False
            )
            shared_state.update(False, self.model_name, None, None, None,
                                ConnectionMode.DISCONNECTED)

    # ── メインスレッド UI 更新 ────────────────────────────
    def updateMenuConnected_(self, payload):
        b = payload["battery"]
        storage = payload["storage"]
        uptime = payload["uptime"]
        mode = payload["mode"]

        charging = b.status.lower() == "charging"
        icon = battery_icon(b.level, charging)
        conn = connection_icon(mode)
        self.statusItem.setTitle_(f"{conn}{icon} {b.level}%")

        # デバイス & 接続
        self.mi_device.setAttributedTitle_(
            self._styled("♪", f"{self.model_name}", self._white, 10, 13))
        if mode == ConnectionMode.WIFI:
            self.mi_connection.setAttributedTitle_(
                self._styled("  接続", f"WiFi ({wifi_manager.wifi_ip})", self._teal))
        else:
            self.mi_connection.setAttributedTitle_(
                self._styled("  接続", "USB", self._teal))

        # バッテリー（レベルに応じて色変更）
        batt_color = self._teal
        if b.level <= CRITICAL_BATTERY_THRESHOLD:
            batt_color = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.9, 0.3, 0.3, 1.0)  # 赤
        elif b.level <= LOW_BATTERY_THRESHOLD:
            batt_color = self._pink
        batt_val = f"{icon} {b.level}%"
        if charging:
            batt_val += " ⚡充電中"
        self.mi_battery.setAttributedTitle_(
            self._styled("  バッテリー", batt_val, batt_color))
        self.mi_status.setAttributedTitle_(
            self._styled("  ステータス", b.status_jp, self._white))
        self.mi_health.setAttributedTitle_(
            self._styled("  ヘルス", b.health_jp, self._teal))

        # ステータス
        self.mi_temp.setAttributedTitle_(
            self._styled("  🌡 温度", f"{b.temperature:.1f}°C", self._white))
        self.mi_voltage.setAttributedTitle_(
            self._styled("  ⚡ 電圧", f"{b.voltage:.0f}mV", self._white))
        self.mi_storage.setAttributedTitle_(
            self._styled("  💾 ストレージ", storage or "取得失敗", self._white))
        self.mi_uptime.setAttributedTitle_(
            self._styled("  ⏱ 稼働時間", uptime or "取得失敗", self._white))

        # ワイヤレス
        w = payload.get("wireless")
        if w:
            if w.wifi_on and w.wifi_ssid:
                self.mi_wifi.setAttributedTitle_(self._styled(
                    "  📶 WiFi",
                    f"{w.wifi_ssid}  {w.wifi_signal_label()}",
                    self._teal))
            elif w.wifi_on:
                self.mi_wifi.setAttributedTitle_(
                    self._styled("  📶 WiFi", "ON (未接続)", self._dim))
            else:
                self.mi_wifi.setAttributedTitle_(
                    self._styled("  📶 WiFi", "OFF", self._dim))

            if w.bt_on and w.bt_connected_device:
                bt_val = w.bt_connected_device
                if w.bt_codec:
                    bt_val += f"  [{w.bt_codec}]"
                self.mi_bt.setAttributedTitle_(
                    self._styled("  🎧 BT", bt_val, self._pink))
            elif w.bt_on:
                self.mi_bt.setAttributedTitle_(
                    self._styled("  🎧 BT", "ON (未接続)", self._dim))
            else:
                self.mi_bt.setAttributedTitle_(
                    self._styled("  🎧 BT", "OFF", self._dim))

    @objc.python_method
    def _apply_disconnected_style(self):
        """未接続時のスタイル適用"""
        self.mi_device.setAttributedTitle_(
            self._styled("♪", "HiBy M500 — 未接続", self._dim, 10, 13))
        self.mi_connection.setAttributedTitle_(
            self._styled("  接続", "--", self._dim))
        self.mi_battery.setAttributedTitle_(
            self._styled("  バッテリー", "--", self._dim))
        self.mi_status.setAttributedTitle_(
            self._styled("  ステータス", "--", self._dim))
        self.mi_health.setAttributedTitle_(
            self._styled("  ヘルス", "--", self._dim))
        self.mi_temp.setAttributedTitle_(
            self._styled("  🌡 温度", "--", self._dim))
        self.mi_voltage.setAttributedTitle_(
            self._styled("  ⚡ 電圧", "--", self._dim))
        self.mi_storage.setAttributedTitle_(
            self._styled("  💾 ストレージ", "--", self._dim))
        self.mi_uptime.setAttributedTitle_(
            self._styled("  ⏱ 稼働時間", "--", self._dim))
        self.mi_wifi.setAttributedTitle_(
            self._styled("  📶 WiFi", "--", self._dim))
        self.mi_bt.setAttributedTitle_(
            self._styled("  🎧 BT", "--", self._dim))

    def updateMenuDisconnected_(self, _):
        self.statusItem.setTitle_("🎵 --")
        self._apply_disconnected_style()

    # ── バッテリー警告 ────────────────────────────────────
    @objc.python_method
    def _check_low_battery(self, b: BatteryInfo):
        if b.status.lower() == "charging":
            self._low_notified = False
            self._critical_notified = False
            return
        if b.level <= CRITICAL_BATTERY_THRESHOLD and not self._critical_notified:
            send_notification("⚠️ HiBy M500 バッテリー危険",
                              f"残量 {b.level}%", "すぐに充電してください！")
            self._critical_notified = True
            self._low_notified = True
        elif b.level <= LOW_BATTERY_THRESHOLD and not self._low_notified:
            send_notification("🔋 HiBy M500 バッテリー低下",
                              f"残量 {b.level}%", "充電を推奨します。")
            self._low_notified = True


# ── エントリーポイント ────────────────────────────────────
def main():
    log.info("HiBy M500 Monitor 起動")
    print("=" * 50)
    print("  HiBy M500 Monitor")
    print("=" * 50)
    print()
    print(f"  ログ        : {LOG_PATH}")
    print(f"  ポーリング  : {POLL_INTERVAL_SEC}秒")
    print(f"  API         : http://127.0.0.1:{API_PORT}/api/status")
    print()

    if run_adb("version") is None:
        print("  ❌ adb が見つかりません。")
        print("     brew install android-platform-tools")
        raise SystemExit(1)

    config = load_config()
    if config.get("wifi_ip"):
        print(f"  前回のWiFi IP: {config['wifi_ip']}")

    devices = list_connected_devices()
    if devices:
        for s, t in devices:
            print(f"  ✅ {get_device_model(serial=s)} ({t}: {s})")
    else:
        print("  ⚠️  デバイスが見つかりません（接続を待機）")
    print()

    # M500 製品画像をダウンロード（白背景の透過処理はダッシュボード側のCanvasで実行）
    m500_jpg = SCRIPT_DIR / "m500_device.jpg"
    if not m500_jpg.exists():
        try:
            import urllib.request
            url = ("https://www.mixwave.co.jp/wp-content/uploads/2025/12/"
                   "hibydigital_m5oo_miku_wh_product_pt02-1200x651.jpg")
            print("  📷 M500 画像をダウンロード中...")
            urllib.request.urlretrieve(url, str(m500_jpg))
            print("  ✅ 画像保存完了")
        except Exception as e:
            log.warning("M500 画像ダウンロード失敗: %s (ダッシュボードで外部URLを使用)", e)
            print(f"  ⚠️  画像ダウンロード失敗（ダッシュボードで外部URLを使用）")

    # API サーバー（バックグラウンド）
    threading.Thread(target=start_api_server, daemon=True).start()
    print(f"  API: http://127.0.0.1:{API_PORT}/")
    print()

    # macOS アプリ起動
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)  # NSApplicationActivationPolicyAccessory

    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)

    print("  メニューバーに 🎵 アイコンが表示されます。")
    print("  「ダッシュボードを開く」でウィンドウ表示。")
    print("  Ctrl+C で終了")
    print()

    signal.signal(signal.SIGINT, lambda *_: app.terminate_(None))
    app.run()


if __name__ == "__main__":
    main()
