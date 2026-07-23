import argparse
import ctypes
import datetime as dt
import json
import mmap
import os
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import winreg
from ctypes import wintypes
from pathlib import Path
from typing import Dict, List, Optional, Union

import psutil
import serial
from serial.tools import list_ports


BAUD_RATE = 115200
DEFAULT_INTERVAL = 1.0
APP_NAME = "WindowsSystemMonitorAgent"
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
CONFIG_PATH = APP_DIR / "config.json"
MetricValue = Union[float, int]
SINGLE_INSTANCE_MUTEX = None
TITLE_BITMAP_W = 230
TITLE_BITMAP_H = 24
WEB_UI_URL = "http://127.0.0.1:8765"
LRESULT = getattr(wintypes, "LRESULT", ctypes.c_ssize_t)


DEFAULT_FIELDS = [
    {"metric": "cpu_temp", "label": "CPU Temp", "enabled": True},
    {"metric": "fan_percent", "label": "Fan", "enabled": True},
    {"metric": "cpu_load", "label": "CPU Load", "enabled": True},
    {"metric": "memory", "label": "Memory", "enabled": True},
    {"metric": "system_power", "label": "Est Power", "enabled": True},
    {"metric": "gpu_temp", "label": "GPU Temp", "enabled": True},
    {"metric": "gpu_load", "label": "GPU Load", "enabled": True},
    {"metric": "c_drive", "label": "C Drive", "enabled": True},
]

METRIC_NAMES = {
    "cpu_temp": "CPU temperature",
    "cpu_power": "CPU power",
    "fan_percent": "Fan speed",
    "cpu_load": "CPU load",
    "memory": "Memory used/total",
    "memory_percent": "Memory load",
    "system_power": "Estimated system power",
    "api_week": "OpenAI API weekly spend",
    "api_month": "OpenAI API monthly spend",
    "gpu_temp": "GPU temperature",
    "gpu_load": "GPU load",
    "c_drive": "C drive used/total",
    "c_percent": "C drive load",
    "c_free": "C drive free",
}


def drive_letter(mountpoint: str) -> str:
    if len(mountpoint) >= 2 and mountpoint[1] == ":":
        return mountpoint[0].upper()
    cleaned = mountpoint.strip("\\/").replace("\\", "_").replace("/", "_")
    return cleaned.upper() or "DISK"


def drive_metric_prefix(mountpoint: str) -> str:
    letter = drive_letter(mountpoint).lower()
    safe = "".join(ch if ch.isalnum() else "_" for ch in letter)
    return f"drive_{safe}"


def available_drive_mounts() -> List[str]:
    mounts = []
    seen = set()
    for partition in psutil.disk_partitions(all=False):
        mountpoint = partition.mountpoint
        if not mountpoint or mountpoint in seen:
            continue
        try:
            psutil.disk_usage(mountpoint)
        except Exception:
            continue
        seen.add(mountpoint)
        mounts.append(mountpoint)
    mounts.sort(key=drive_letter)
    return mounts


def drive_metric_names() -> Dict[str, str]:
    names: Dict[str, str] = {}
    for mountpoint in available_drive_mounts():
        letter = drive_letter(mountpoint)
        prefix = drive_metric_prefix(mountpoint)
        names[prefix] = f"{letter}: drive used/total"
        names[f"{prefix}_percent"] = f"{letter}: drive load"
        names[f"{prefix}_free"] = f"{letter}: drive free"
    return names


def available_metric_names(extra_metrics: Optional[List[str]] = None) -> Dict[str, str]:
    names = dict(METRIC_NAMES)
    names.update(drive_metric_names())
    for metric in extra_metrics or []:
        if metric and metric not in names:
            names[metric] = f"{metric} (unavailable)"
    return names


def log(text: str) -> None:
    try:
        print(text, flush=True)
    except Exception:
        pass


def acquire_single_instance() -> bool:
    global SINGLE_INSTANCE_MUTEX
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    mutex_name = f"Local\\{APP_NAME}"
    handle = kernel32.CreateMutexW(None, False, mutex_name)
    if not handle:
        return True
    if ctypes.get_last_error() == 183:
        return False
    SINGLE_INSTANCE_MUTEX = handle
    return True


class HardwareMonitor:
    def __init__(self) -> None:
        self.handles = []
        self.backend = []
        self.available = False
        self._load_librehardwaremonitor()
        self._load_openhardwaremonitor()

    def _load_librehardwaremonitor(self) -> None:
        try:
            import clr  # type: ignore

            dll_dir = BUNDLE_DIR
            sys.path.append(str(dll_dir))
            clr.AddReference(str(dll_dir / "LibreHardwareMonitorLib.dll"))
            from LibreHardwareMonitor import Hardware  # type: ignore

            handle = Hardware.Computer()
            handle.IsMotherboardEnabled = True
            handle.IsCpuEnabled = True
            handle.IsGpuEnabled = True
            handle.Open()
            self.handles.append(("LibreHardwareMonitor", handle))
            self.backend.append("LibreHardwareMonitor")
            self.available = True
        except Exception as exc:
            log(f"LibreHardwareMonitor unavailable: {exc}")

    def _load_openhardwaremonitor(self) -> None:
        try:
            import clr  # type: ignore

            dll_dir = BUNDLE_DIR
            sys.path.append(str(dll_dir))
            clr.AddReference(str(dll_dir / "OpenHardwareMonitorLib.dll"))
            from OpenHardwareMonitor import Hardware  # type: ignore

            handle = Hardware.Computer()
            handle.MainboardEnabled = True
            handle.CPUEnabled = True
            handle.GPUEnabled = True
            handle.Open()
            self.handles.append(("OpenHardwareMonitor", handle))
            self.backend.append("OpenHardwareMonitor")
            self.available = True
        except Exception as exc:
            log(f"OpenHardwareMonitor unavailable: {exc}")

    def read(self) -> Dict[str, MetricValue]:
        data: Dict[str, MetricValue] = {
            "cpu_temp": 0,
            "fan_percent": 0,
            "gpu_temp": 0,
            "gpu_load": 0,
            "cpu_power": 0,
            "gpu_power": 0,
        }
        fan_rpm = 0.0
        fan_control = 0.0

        if not self.available or not self.handles:
            return data

        for _, handle in self.handles:
            for hardware in handle.Hardware:
                hardware.Update()
                self._read_sensors(hardware.Sensors, data)
                for subhardware in hardware.SubHardware:
                    subhardware.Update()
                    self._read_sensors(subhardware.Sensors, data)

        fan_rpm = float(data.pop("_fan_rpm", 0))
        fan_control = float(data.pop("_fan_control", 0))
        if fan_rpm > 0:
            data["fan_percent"] = round(fan_rpm)
        elif fan_control > 0:
            data["fan_percent"] = round(clamp(fan_control, 0, 100))

        core_temp = read_coretemp_shared_memory()
        if core_temp.get("cpu_temp"):
            data["cpu_temp"] = core_temp["cpu_temp"]
        if core_temp.get("cpu_power"):
            data["cpu_power"] = core_temp["cpu_power"]

        return data

    @staticmethod
    def _read_sensors(sensors, data: Dict[str, MetricValue]) -> None:
        for sensor in sensors:
            if sensor.Value is None:
                continue
            sensor_type = sensor.SensorType.ToString()
            name = str(sensor.Name).lower()
            hardware_name = str(sensor.Hardware.Name).lower()
            hardware_type = sensor.Hardware.HardwareType.ToString().lower()
            value = float(sensor.Value)

            if sensor_type == "Temperature":
                if hardware_type == "cpu":
                    if 20 <= value <= 120 and (data.get("cpu_temp", 0) == 0 or "package" in name or "cpu" in name):
                        data["cpu_temp"] = round(value)
                elif hardware_type.startswith("gpu") or "gpu" in hardware_name:
                    if 20 <= value <= 120:
                        data["gpu_temp"] = round(value)
            elif sensor_type == "Load":
                if hardware_type.startswith("gpu") or "gpu" in hardware_name:
                    current = float(data.get("gpu_load", 0))
                    data["gpu_load"] = round(max(current, clamp(value, 0, 100)))
            elif sensor_type == "Power":
                if 0 < value < 1000:
                    if hardware_type == "cpu" or "cpu" in hardware_name:
                        data["cpu_power"] = max(float(data.get("cpu_power", 0)), value)
                    elif hardware_type.startswith("gpu") or "gpu" in hardware_name:
                        data["gpu_power"] = max(float(data.get("gpu_power", 0)), value)
            elif sensor_type == "Fan" and value > 0 and "_fan_rpm" not in data:
                data["_fan_rpm"] = value
            elif sensor_type == "Control" and value > 0 and ("fan" in name or "pwm" in name):
                data["_fan_control"] = value


class CoreTempSharedData(ctypes.Structure):
    _fields_ = [
        ("uiLoad", ctypes.c_uint * 256),
        ("uiTjMax", ctypes.c_uint * 128),
        ("uiCoreCnt", ctypes.c_uint),
        ("uiCPUCnt", ctypes.c_uint),
        ("fTemp", ctypes.c_float * 256),
        ("fVID", ctypes.c_float),
        ("fCPUSpeed", ctypes.c_float),
        ("fFSBSpeed", ctypes.c_float),
        ("fMultiplier", ctypes.c_float),
        ("sCPUName", ctypes.c_wchar * 100),
        ("ucFahrenheit", ctypes.c_ubyte),
        ("ucDeltaToTjMax", ctypes.c_ubyte),
    ]


class CoreTempSharedDataEx(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("uiLoad", ctypes.c_uint * 256),
        ("uiTjMax", ctypes.c_uint * 128),
        ("uiCoreCnt", ctypes.c_uint),
        ("uiCPUCnt", ctypes.c_uint),
        ("fTemp", ctypes.c_float * 256),
        ("fVID", ctypes.c_float),
        ("fCPUSpeed", ctypes.c_float),
        ("fFSBSpeed", ctypes.c_float),
        ("fMultiplier", ctypes.c_float),
        ("sCPUName", ctypes.c_char * 100),
        ("ucFahrenheit", ctypes.c_ubyte),
        ("ucDeltaToTjMax", ctypes.c_ubyte),
        ("ucTdpSupported", ctypes.c_ubyte),
        ("ucPowerSupported", ctypes.c_ubyte),
        ("uiStructVersion", ctypes.c_uint),
        ("uiTdp", ctypes.c_uint * 128),
        ("fPower", ctypes.c_float * 128),
        ("fMultipliers", ctypes.c_float * 256),
    ]


def read_coretemp_shared_memory() -> Dict[str, MetricValue]:
    try:
        size = ctypes.sizeof(CoreTempSharedDataEx)
        with mmap.mmap(-1, size, tagname="CoreTempMappingObjectEx", access=mmap.ACCESS_READ) as mapped:
            data = CoreTempSharedDataEx.from_buffer_copy(mapped.read(size))
        total = min(int(data.uiCoreCnt) * max(int(data.uiCPUCnt), 1), len(data.fTemp))
        temps = [float(data.fTemp[i]) for i in range(total) if 20 <= float(data.fTemp[i]) <= 120]
        powers = [float(data.fPower[i]) for i in range(len(data.fPower)) if 0 < float(data.fPower[i]) < 1000]
        return {
            "cpu_temp": round(max(temps)) if temps else 0,
            "cpu_power": round(max(powers), 1) if data.ucPowerSupported and powers else 0,
        }
    except Exception:
        try:
            size = ctypes.sizeof(CoreTempSharedData)
            with mmap.mmap(-1, size, tagname="CoreTempMappingObject", access=mmap.ACCESS_READ) as mapped:
                data = CoreTempSharedData.from_buffer_copy(mapped.read(size))
            total = min(int(data.uiCoreCnt) * max(int(data.uiCPUCnt), 1), len(data.fTemp))
            temps = [float(data.fTemp[i]) for i in range(total) if 20 <= float(data.fTemp[i]) <= 120]
            return {"cpu_temp": round(max(temps)) if temps else 0, "cpu_power": 0}
        except Exception:
            return {"cpu_temp": 0, "cpu_power": 0}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def gb(bytes_value: int) -> str:
    return f"{bytes_value / (1024 ** 3):.1f}"


def default_openai_config() -> dict:
    return {
        "admin_key": "",
        "weekly_budget": 0.0,
        "monthly_budget": 0.0,
        "enabled": False,
    }


def default_config() -> dict:
    return {
        "port": "COM3",
        "interval": DEFAULT_INTERVAL,
        "title": "Windows Monitor",
        "fields": [dict(field) for field in DEFAULT_FIELDS],
        "startup": False,
        "openai": default_openai_config(),
    }


def load_config() -> dict:
    config = default_config()
    loaded_has_fields = False
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            loaded_has_fields = "fields" in loaded
            config.update(loaded)
        except Exception as exc:
            log(f"Could not read config: {exc}")
    config["fields"] = normalize_fields(config.get("fields", []), fill_defaults=not loaded_has_fields)
    openai_config = default_openai_config()
    openai_config.update(config.get("openai", {}) or {})
    openai_config["enabled"] = bool(openai_config.get("admin_key"))
    config["openai"] = openai_config
    return config


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def normalize_fields(fields: List[dict], fill_defaults: bool = False) -> List[dict]:
    normalized = []
    valid_metrics = available_metric_names([str(field.get("metric", "")) for field in fields])
    for field in fields:
        metric = field.get("metric")
        if metric in valid_metrics:
            normalized.append({
                "metric": metric,
                "label": field.get("label") or metric,
                "enabled": bool(field.get("enabled", True)),
                "order": int(field.get("order", len(normalized) + 1)),
            })
    if fill_defaults:
        used = {field["metric"] for field in normalized}
        for field in DEFAULT_FIELDS:
            if field["metric"] not in used:
                copied = dict(field)
                copied["order"] = len(normalized) + 1
                normalized.append(copied)
    normalized.sort(key=lambda field: field.get("order", 99))
    for idx, field in enumerate(normalized, start=1):
        field["order"] = idx
    return normalized[:10]


def available_ports() -> List[str]:
    return [port.device for port in list_ports.comports()]


def local_ipv4() -> str:
    candidates = []
    stats = psutil.net_if_stats()
    for name, addrs in psutil.net_if_addrs().items():
        if name in stats and not stats[name].isup:
            continue
        for addr in addrs:
            if addr.family == socket.AF_INET and not addr.address.startswith("127."):
                candidates.append(addr.address)
    return candidates[0] if candidates else "0.0.0.0"


def choose_port(requested_port: Optional[str]) -> str:
    if requested_port:
        return requested_port

    ports = available_ports()
    if not ports:
        raise RuntimeError("No serial ports found. Check the USB cable and driver.")

    log("Available serial ports:")
    for idx, port in enumerate(ports, start=1):
        log(f"  {idx}. {port}")

    if len(ports) == 1:
        log(f"Using {ports[0]}")
        return ports[0]

    selected = input("Select port number: ").strip()
    try:
        return ports[int(selected) - 1]
    except Exception as exc:
        raise RuntimeError("Invalid port selection.") from exc


class OpenAIUsageMonitor:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cache_key = ""
        self.last_fetch = 0.0
        self.values = {"api_week": "N/A", "api_month": "N/A"}

    def read(self, config: dict) -> Dict[str, str]:
        openai_config = config.get("openai", {}) or {}
        admin_key = str(openai_config.get("admin_key", "")).strip()
        if not admin_key:
            return {"api_week": "N/A", "api_month": "N/A"}

        cache_key = json.dumps({
            "key_tail": admin_key[-8:],
            "weekly_budget": openai_config.get("weekly_budget", 0),
            "monthly_budget": openai_config.get("monthly_budget", 0),
        }, sort_keys=True)

        now = time.time()
        with self.lock:
            if self.cache_key == cache_key and now - self.last_fetch < 300:
                return dict(self.values)

        try:
            weekly = self._cost_since(admin_key, dt.datetime.now() - dt.timedelta(days=7))
            month_start = dt.datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            monthly = self._cost_since(admin_key, month_start)
            values = {
                "api_week": self._format_cost(weekly, float(openai_config.get("weekly_budget") or 0)),
                "api_month": self._format_cost(monthly, float(openai_config.get("monthly_budget") or 0)),
            }
        except Exception as exc:
            log(f"OpenAI usage unavailable: {exc}")
            values = {"api_week": "API err", "api_month": "API err"}

        with self.lock:
            self.cache_key = cache_key
            self.last_fetch = now
            self.values = values
            return dict(self.values)

    @staticmethod
    def _cost_since(admin_key: str, start: dt.datetime) -> float:
        start_ts = int(start.timestamp())
        end_ts = int(time.time())
        params = urllib.parse.urlencode({
            "start_time": start_ts,
            "end_time": end_ts,
            "bucket_width": "1d",
            "limit": 180,
        })
        request = urllib.request.Request(
            f"https://api.openai.com/v1/organization/costs?{params}",
            headers={
                "Authorization": f"Bearer {admin_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))

        total = 0.0
        for bucket in payload.get("data", []):
            for result in bucket.get("results", []):
                amount = result.get("amount", {}) or {}
                if amount.get("currency", "usd").lower() == "usd":
                    total += float(amount.get("value") or 0)
        return total

    @staticmethod
    def _format_cost(spend: float, budget: float) -> str:
        if budget > 0:
            return f"${spend:.2f}/${budget:.0f}"
        return f"${spend:.2f}"


OPENAI_USAGE_MONITOR = OpenAIUsageMonitor()


def estimate_system_power(sensors: Dict[str, MetricValue], cpu_load: int) -> int:
    gpu_power = float(sensors.get("gpu_power", 0))
    cpu_power = float(sensors.get("cpu_power", 0))
    if cpu_power <= 0:
        cpu_power = 12.0 + (max(0, min(cpu_load, 100)) / 100.0) * 90.0

    platform_power = 35.0
    return round(cpu_power + gpu_power + platform_power)


def collect_metrics(hw: HardwareMonitor, config: Optional[dict] = None) -> Dict[str, str]:
    sensors = hw.read()
    memory = psutil.virtual_memory()
    c_disk = psutil.disk_usage("C:\\")
    cpu_load = int(psutil.cpu_percent(interval=None))
    system_power = estimate_system_power(sensors, cpu_load)
    openai_usage = OPENAI_USAGE_MONITOR.read(config or {})

    metrics = {
        "cpu_temp": f"{int(sensors['cpu_temp'])} C" if sensors["cpu_temp"] else "N/A",
        "cpu_power": f"{float(sensors['cpu_power']):.0f} W" if sensors["cpu_power"] else "N/A",
        "fan_percent": f"{int(sensors['fan_percent'])}",
        "cpu_load": f"{cpu_load}%",
        "memory": f"{gb(memory.used)}/{gb(memory.total)} GB",
        "memory_percent": f"{int(memory.percent)}%",
        "system_power": f"{system_power} W",
        "gpu_temp": f"{int(sensors['gpu_temp'])} C",
        "gpu_load": f"{int(sensors['gpu_load'])}%",
        "c_drive": f"{gb(c_disk.used)}/{gb(c_disk.total)} GB",
        "c_percent": f"{int(c_disk.percent)}%",
        "c_free": f"{gb(c_disk.free)} GB",
    }
    for mountpoint in available_drive_mounts():
        try:
            disk = psutil.disk_usage(mountpoint)
        except Exception:
            continue
        prefix = drive_metric_prefix(mountpoint)
        metrics[prefix] = f"{gb(disk.used)}/{gb(disk.total)} GB"
        metrics[f"{prefix}_percent"] = f"{int(disk.percent)}%"
        metrics[f"{prefix}_free"] = f"{gb(disk.free)} GB"
    metrics.update(openai_usage)
    return metrics


def display_title(config: dict) -> str:
    return local_ipv4()


def build_packet(hw: HardwareMonitor, config: Optional[dict] = None) -> str:
    config = config or load_config()
    metrics = collect_metrics(hw, config)
    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = display_title(config)
    parts = ["V2", safe_text(title), timestamp]

    for field in normalize_fields(config.get("fields", [])):
        if not field.get("enabled", True):
            continue
        metric = field["metric"]
        label = safe_text(field.get("label") or metric)
        parts.append(f"{label}={safe_text(metrics.get(metric, 'n/a'))}")
        if len(parts) >= 13:
            break

    return "|".join(parts) + "\n"


def build_title_bitmap_packet(text: str) -> Optional[str]:
    if os.name != "nt":
        return None

    width = TITLE_BITMAP_W
    height = TITLE_BITMAP_H
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.CreateFontW.restype = wintypes.HFONT
    gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class RGBQUAD(ctypes.Structure):
        _fields_ = [
            ("rgbBlue", wintypes.BYTE),
            ("rgbGreen", wintypes.BYTE),
            ("rgbRed", wintypes.BYTE),
            ("rgbReserved", wintypes.BYTE),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]

    gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(BITMAPINFO),
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.HANDLE,
        wintypes.DWORD,
    ]
    user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
    user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.RECT), wintypes.UINT]

    BI_RGB = 0
    DIB_RGB_COLORS = 0
    TRANSPARENT = 1
    FW_NORMAL = 400
    DEFAULT_CHARSET = 1
    OUT_DEFAULT_PRECIS = 0
    CLIP_DEFAULT_PRECIS = 0
    CLEARTYPE_QUALITY = 5
    DEFAULT_PITCH = 0
    FF_DONTCARE = 0
    DT_LEFT = 0x00000000
    DT_VCENTER = 0x00000004
    DT_SINGLELINE = 0x00000020
    DT_END_ELLIPSIS = 0x00008000
    SRCCOPY = 0x00CC0020

    hdc = user32.GetDC(None)
    if not hdc:
        return None
    mem_dc = gdi32.CreateCompatibleDC(hdc)
    if not mem_dc:
        user32.ReleaseDC(None, hdc)
        return None

    bits = ctypes.c_void_p()
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB
    bitmap = gdi32.CreateDIBSection(mem_dc, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
    if not bitmap or not bits:
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, hdc)
        return None

    font = gdi32.CreateFontW(
        -18, 0, 0, 0, FW_NORMAL, False, False, False, DEFAULT_CHARSET,
        OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY,
        DEFAULT_PITCH | FF_DONTCARE, "Microsoft YaHei"
    )
    old_bitmap = gdi32.SelectObject(mem_dc, bitmap)
    old_font = gdi32.SelectObject(mem_dc, font)
    try:
        rect = wintypes.RECT(0, 0, width, height)
        gdi32.SetBkMode(mem_dc, TRANSPARENT)
        gdi32.SetTextColor(mem_dc, 0x00FFFFFF)
        brush = gdi32.CreateSolidBrush(0x00000000)
        user32.FillRect(mem_dc, ctypes.byref(rect), brush)
        gdi32.DeleteObject(brush)
        user32.DrawTextW(mem_dc, str(text), -1, ctypes.byref(rect), DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS)

        raw = ctypes.string_at(bits, width * height * 4)
        packed = bytearray((width * height + 7) // 8)
        for y in range(height):
            row = y * width * 4
            for x in range(width):
                offset = row + x * 4
                blue = raw[offset]
                green = raw[offset + 1]
                red = raw[offset + 2]
                if red + green + blue > 90:
                    bit = y * width + x
                    packed[bit // 8] |= 0x80 >> (bit % 8)
        return f"IMG|TITLE|{width}|{height}|{packed.hex()}\n"
    except Exception as exc:
        log(f"Title bitmap unavailable: {exc}")
        return None
    finally:
        if old_font:
            gdi32.SelectObject(mem_dc, old_font)
        if old_bitmap:
            gdi32.SelectObject(mem_dc, old_bitmap)
        if font:
            gdi32.DeleteObject(font)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, hdc)


def safe_text(value: object) -> str:
    text = str(value)
    return text.replace("|", "/").replace("=", ":").replace("\n", " ").strip()


class AgentRunner:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.status = "Stopped"
        self.connection: Optional[serial.Serial] = None
        self.connection_lock = threading.Lock()
        self.reload_after_packet = threading.Event()
        self.last_packet = ""
        self.last_packet_at = 0.0
        self.last_title_bitmap_text = ""

    def start(self, config: dict, on_status=None, force_reload: bool = False) -> None:
        self.stop()
        self.stop_event.clear()
        self.reload_after_packet.clear()
        self.last_title_bitmap_text = ""
        self.thread = threading.Thread(target=self._run, args=(config, on_status), daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        self.thread = None
        self.status = "Stopped"
        with self.connection_lock:
            self.connection = None

    def _set_status(self, text: str, on_status=None) -> None:
        self.status = text
        log(text)
        if on_status:
            on_status(text)

    def _run(self, config: dict, on_status=None) -> None:
        hw = HardwareMonitor()
        psutil.cpu_percent(interval=None)
        port = config["port"]
        interval = max(0.2, float(config.get("interval", DEFAULT_INTERVAL)))

        try:
            with serial.Serial(port, BAUD_RATE, timeout=1) as connection:
                with self.connection_lock:
                    self.connection = connection
                self._set_status(f"Running on {port}, {interval:g}s", on_status)
                while not self.stop_event.is_set():
                    packet = build_packet(hw, config)
                    self.last_packet = packet.strip()
                    self.last_packet_at = time.time()
                    connection.write(packet.encode("utf-8", errors="replace"))
                    if self.reload_after_packet.is_set():
                        connection.write(b"CMD|RELOAD\n")
                        self.reload_after_packet.clear()
                    connection.flush()
                    log(packet.strip())
                    self.stop_event.wait(interval)
        except Exception as exc:
            self._set_status(f"Error: {exc}", on_status)
        finally:
            with self.connection_lock:
                self.connection = None

    def restart(self, config: dict, on_status=None) -> None:
        self.start(config, on_status=on_status, force_reload=True)
        self.status = "Restarted"

    def reload_display(self, port: Optional[str] = None) -> str:
        packet = b"CMD|RELOAD\n"
        with self.connection_lock:
            connection = self.connection
            if connection and connection.is_open:
                connection.write(packet)
                connection.flush()
                self.status = "Reload sent"
                return self.status

        if not port:
            self.status = "Reload failed: serial is not running"
            return self.status

        try:
            with serial.Serial(port, BAUD_RATE, timeout=1) as connection:
                connection.write(packet)
                connection.flush()
            self.status = f"Reload sent to {port}"
            return self.status
        except Exception as exc:
            self.status = f"Reload failed: {exc}"
            return self.status


class TrayIcon:
    WM_TRAYICON = 0x0400 + 20
    WM_COMMAND = 0x0111
    WM_DESTROY = 0x0002
    WM_CLOSE = 0x0010
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    NIM_ADD = 0
    NIM_DELETE = 2
    NIF_MESSAGE = 1
    NIF_ICON = 2
    NIF_TIP = 4
    MF_STRING = 0
    MF_SEPARATOR = 0x0800
    TPM_RIGHTBUTTON = 0x0002
    IDI_APPLICATION = 32512
    MENU_OPEN = 1001
    MENU_RESTART = 1002
    MENU_STOP = 1003
    MENU_QUIT = 1004

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", ctypes.c_void_p),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HANDLE),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HANDLE),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HANDLE),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", wintypes.HANDLE),
        ]

    def __init__(self, tooltip: str, on_open, on_restart, on_stop, on_quit) -> None:
        self.tooltip = tooltip[:127]
        self.on_open = on_open
        self.on_restart = on_restart
        self.on_stop = on_stop
        self.on_quit = on_quit
        self.thread: Optional[threading.Thread] = None
        self.hwnd = None
        self._wndproc = None

    def start(self) -> None:
        if os.name != "nt":
            return
        self.thread = threading.Thread(target=self._run, name="SystemMonitorTray", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.hwnd:
            ctypes.windll.user32.PostMessageW(self.hwnd, self.WM_CLOSE, 0, 0)

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        shell32 = ctypes.windll.shell32
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.LoadIconW.restype = wintypes.HANDLE
        user32.DefWindowProcW.restype = LRESULT
        user32.CreatePopupMenu.restype = wintypes.HMENU
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        self._wndproc = WNDPROC(self._window_proc)

        hinstance = kernel32.GetModuleHandleW(None)
        class_name = f"{APP_NAME}TrayWindow"
        wndclass = self.WNDCLASSW()
        wndclass.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p).value
        wndclass.hInstance = hinstance
        wndclass.lpszClassName = class_name
        atom = user32.RegisterClassW(ctypes.byref(wndclass))
        if not atom and ctypes.get_last_error() != 1410:
            log(f"Tray register failed: {ctypes.get_last_error()}")
            return

        hwnd = user32.CreateWindowExW(0, class_name, APP_NAME, 0, 0, 0, 0, 0, None, None, hinstance, None)
        if not hwnd:
            log(f"Tray window failed: {ctypes.get_last_error()}")
            return
        self.hwnd = hwnd

        icon = user32.LoadIconW(None, self.IDI_APPLICATION)
        nid = self.NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(self.NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
        nid.uCallbackMessage = self.WM_TRAYICON
        nid.hIcon = icon
        nid.szTip = self.tooltip
        if not shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(nid)):
            log("Tray icon add failed")

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _window_proc(self, hwnd, msg, wparam, lparam):
        if msg == self.WM_TRAYICON:
            if lparam in (self.WM_LBUTTONUP, self.WM_LBUTTONDBLCLK):
                self._safe_call(self.on_open)
            elif lparam == self.WM_RBUTTONUP:
                self._show_menu(hwnd)
            return 0
        if msg == self.WM_COMMAND:
            command_id = int(wparam) & 0xFFFF
            if command_id == self.MENU_OPEN:
                self._safe_call(self.on_open)
            elif command_id == self.MENU_RESTART:
                self._safe_call(self.on_restart)
            elif command_id == self.MENU_STOP:
                self._safe_call(self.on_stop)
            elif command_id == self.MENU_QUIT:
                self._safe_call(self.on_quit)
                ctypes.windll.user32.DestroyWindow(hwnd)
            return 0
        if msg == self.WM_CLOSE:
            ctypes.windll.user32.DestroyWindow(hwnd)
            return 0
        if msg == self.WM_DESTROY:
            nid = self.NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(self.NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 1
            ctypes.windll.shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(nid))
            self.hwnd = None
            ctypes.windll.user32.PostQuitMessage(0)
            return 0
        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu(self, hwnd) -> None:
        user32 = ctypes.windll.user32
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, self.MF_STRING, self.MENU_OPEN, "Open WebUI")
        user32.AppendMenuW(menu, self.MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, self.MF_STRING, self.MENU_RESTART, "Restart Agent")
        user32.AppendMenuW(menu, self.MF_STRING, self.MENU_STOP, "Stop Agent")
        user32.AppendMenuW(menu, self.MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, self.MF_STRING, self.MENU_QUIT, "Quit")
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(hwnd)
        user32.TrackPopupMenu(menu, self.TPM_RIGHTBUTTON, point.x, point.y, 0, hwnd, None)
        user32.DestroyMenu(menu)

    def _safe_call(self, callback) -> None:
        try:
            callback()
        except Exception as exc:
            log(f"Tray action failed: {exc}")


def startup_command() -> str:
    exe = Path(sys.argv[0]).resolve()
    if exe.suffix.lower() == ".exe":
        return f'"{exe}"'
    return f'"{sys.executable}" "{Path(__file__).resolve()}" --web'


def set_startup(enabled: bool) -> None:
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def is_startup_enabled() -> bool:
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except FileNotFoundError:
        return False


def run(port: str, interval: float, config: Optional[dict] = None) -> None:
    config = dict(config or load_config())
    config["port"] = port
    config["interval"] = interval
    runner = AgentRunner()
    runner.start(config)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        runner.stop()
        log("\nStopped.")


def run_gui(minimized: bool = False) -> None:
    import tkinter as tk
    from tkinter import messagebox, ttk

    config = load_config()
    config["startup"] = is_startup_enabled()
    runner = AgentRunner()

    root = tk.Tk()
    root.title("System Monitor Agent")
    root.geometry("720x520")
    root.minsize(640, 460)

    status_var = tk.StringVar(value="Stopped")
    port_var = tk.StringVar(value=config.get("port", "COM3"))
    interval_var = tk.DoubleVar(value=float(config.get("interval", DEFAULT_INTERVAL)))
    title_var = tk.StringVar(value=config.get("title", "Windows Monitor"))
    startup_var = tk.BooleanVar(value=config["startup"])

    fields = normalize_fields(config.get("fields", []))
    field_vars = []

    def refresh_ports() -> None:
        values = available_ports()
        port_combo["values"] = values
        if values and port_var.get() not in values:
            port_var.set(values[0])

    def current_config() -> dict:
        return {
            "port": port_var.get().strip(),
            "interval": max(0.2, float(interval_var.get())),
            "title": title_var.get().strip() or "Windows Monitor",
            "startup": startup_var.get(),
            "fields": [
                {"metric": metric_var.get(), "label": label_var.get().strip(), "enabled": enabled_var.get(), "order": idx + 1}
                for idx, (enabled_var, label_var, metric_var) in enumerate(field_vars)
            ],
        }

    def save() -> None:
        cfg = current_config()
        save_config(cfg)
        set_startup(cfg["startup"])
        status_var.set("Saved")

    def start() -> None:
        cfg = current_config()
        save_config(cfg)
        set_startup(cfg["startup"])
        runner.start(cfg, lambda text: root.after(0, status_var.set, text))

    def restart() -> None:
        cfg = current_config()
        save_config(cfg)
        set_startup(cfg["startup"])
        runner.restart(cfg, lambda text: root.after(0, status_var.set, text))
        status_var.set("Restarted")

    def stop() -> None:
        runner.stop()
        status_var.set("Stopped")

    def reload_display() -> None:
        cfg = current_config()
        save_config(cfg)
        status_var.set(runner.reload_display(cfg.get("port")))

    def move_row(index: int, delta: int) -> None:
        target = index + delta
        if target < 0 or target >= len(field_vars):
            return
        values = [(v[0].get(), v[1].get(), v[2].get()) for v in field_vars]
        values[index], values[target] = values[target], values[index]
        for vars_, row_values in zip(field_vars, values):
            vars_[0].set(row_values[0])
            vars_[1].set(row_values[1])
            vars_[2].set(row_values[2])

    top = ttk.Frame(root, padding=12)
    top.pack(fill="x")
    ttk.Label(top, text="Port").grid(row=0, column=0, sticky="w")
    port_combo = ttk.Combobox(top, textvariable=port_var, width=16)
    port_combo.grid(row=0, column=1, sticky="w", padx=(6, 18))
    ttk.Button(top, text="Refresh", command=refresh_ports).grid(row=0, column=2, sticky="w")
    ttk.Label(top, text="Interval").grid(row=0, column=3, sticky="w", padx=(18, 6))
    ttk.Spinbox(top, from_=0.2, to=60, increment=0.2, textvariable=interval_var, width=8).grid(row=0, column=4, sticky="w")
    ttk.Label(top, text="seconds").grid(row=0, column=5, sticky="w", padx=(6, 0))

    ttk.Label(top, text="Title").grid(row=1, column=0, sticky="w", pady=(10, 0))
    ttk.Entry(top, textvariable=title_var).grid(row=1, column=1, columnspan=4, sticky="ew", padx=(6, 0), pady=(10, 0))
    ttk.Checkbutton(top, text="Start with Windows", variable=startup_var).grid(row=1, column=5, sticky="w", padx=(12, 0), pady=(10, 0))
    top.columnconfigure(4, weight=1)

    editor = ttk.LabelFrame(root, text="Display UI", padding=10)
    editor.pack(fill="both", expand=True, padx=12, pady=8)
    ttk.Label(editor, text="Show").grid(row=0, column=0)
    ttk.Label(editor, text="Label").grid(row=0, column=1, sticky="w")
    ttk.Label(editor, text="Metric").grid(row=0, column=2, sticky="w")

    for i, field in enumerate(fields):
        enabled_var = tk.BooleanVar(value=field["enabled"])
        label_var = tk.StringVar(value=field["label"])
        metric_var = tk.StringVar(value=field["metric"])
        field_vars.append((enabled_var, label_var, metric_var))

        ttk.Checkbutton(editor, variable=enabled_var).grid(row=i + 1, column=0)
        ttk.Entry(editor, textvariable=label_var, width=24).grid(row=i + 1, column=1, sticky="ew", padx=6, pady=3)
        ttk.Combobox(editor, textvariable=metric_var, values=list(METRIC_NAMES.keys()), width=18, state="readonly").grid(row=i + 1, column=2, sticky="w", pady=3)
        ttk.Button(editor, text="Up", command=lambda idx=i: move_row(idx, -1)).grid(row=i + 1, column=3, padx=2)
        ttk.Button(editor, text="Down", command=lambda idx=i: move_row(idx, 1)).grid(row=i + 1, column=4, padx=2)
    editor.columnconfigure(1, weight=1)

    bottom = ttk.Frame(root, padding=12)
    bottom.pack(fill="x")
    ttk.Button(bottom, text="Restart", command=restart).pack(side="left")
    ttk.Button(bottom, text="Stop", command=stop).pack(side="left", padx=8)
    ttk.Label(bottom, textvariable=status_var).pack(side="right")

    refresh_ports()
    if minimized:
        root.withdraw()
        start()

    def on_close() -> None:
        if runner.thread and runner.thread.is_alive():
            if not messagebox.askyesno("Quit", "Stop the agent and quit?"):
                return
        runner.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


def run_web_gui(auto_start: bool = False, open_browser_on_start: bool = True) -> None:
    import html
    import urllib.parse
    import webbrowser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    url = WEB_UI_URL
    runner = AgentRunner()

    def render_page(status: str = "") -> bytes:
        config = load_config()
        config["startup"] = is_startup_enabled()
        openai_config = config.get("openai", {}) or {}
        ports = available_ports()
        fields = normalize_fields(config.get("fields", []))
        metric_names = available_metric_names([field.get("metric", "") for field in fields])
        drive_summary = ", ".join(f"{drive_letter(mount)}:" for mount in available_drive_mounts()) or "None"
        selected_port = config.get("port", "COM3")
        if selected_port not in ports:
            ports.insert(0, selected_port)

        port_options = "".join(
            f'<option value="{html.escape(port)}" {"selected" if port == selected_port else ""}>{html.escape(port)}</option>'
            for port in ports
        )
        rows = []
        for idx, field in enumerate(fields):
            metric_options = "".join(
                f'<option value="{html.escape(key)}" {"selected" if key == field["metric"] else ""}>{html.escape(name)}</option>'
                for key, name in metric_names.items()
            )
            rows.append(
                f"""
                <section class="slot">
                  <input type="hidden" name="order_{idx}" value="{field.get("order", idx + 1)}">
                  <div class="slot-head">
                    <div class="slot-title">
                      <button class="drag-handle" type="button" title="Drag to reorder" aria-label="Drag to reorder">
                        <span></span><span></span><span></span>
                      </button>
                      <span class="slot-index">{idx + 1}</span>
                    </div>
                    <label><input type="checkbox" name="enabled_{idx}" {"checked" if field["enabled"] else ""}> Show</label>
                    <button class="delete-slot" type="button" title="Delete element" aria-label="Delete element">Delete</button>
                  </div>
                  <input class="slot-label" name="label_{idx}" value="{html.escape(field["label"])}">
                  <select name="metric_{idx}">{metric_options}</select>
                  <div class="slot-value" data-slot-value="{idx}">--</div>
                </section>
                """
            )
        default_metric_options = "".join(
            f'<option value="{html.escape(key)}">{html.escape(name)}</option>'
            for key, name in metric_names.items()
        )

        page = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>System Monitor Agent</title>
  <style>
    body {{ font-family: Segoe UI, Arial, sans-serif; margin: 24px; background: #101318; color: #e8edf2; }}
    main {{ max-width: 980px; margin: auto; }}
    input, select, button {{ font: inherit; padding: 8px; border-radius: 4px; border: 1px solid #4a5568; background: #171c23; color: #e8edf2; }}
    button {{ cursor: pointer; margin-right: 8px; background: #2b6cb0; border-color: #2b6cb0; }}
    .row {{ display: flex; gap: 12px; align-items: center; margin: 12px 0; flex-wrap: wrap; }}
    .status {{ color: #90cdf4; margin: 12px 0; }}
    .screen {{ margin: 18px 0; border: 1px solid #3b4452; background: #050607; border-radius: 4px; padding: 10px; max-width: 760px; }}
    .screen-top {{ display: flex; justify-content: center; align-items: center; border-bottom: 1px solid #38404a; padding: 0 4px 8px; color: #90cdf4; font-size: 18px; font-weight: 600; }}
    .screen-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1px 10px; padding-top: 10px; }}
    .screen-bottom {{ border-top: 1px solid #38404a; margin-top: 10px; padding-top: 8px; text-align: center; color: #e8edf2; font-size: 14px; }}
    .slot {{ min-height: 74px; border-bottom: 1px solid #2d3748; padding: 8px 4px 10px; }}
    .slot:nth-child(odd) {{ border-right: 1px solid #2d3748; padding-right: 12px; }}
    .slot.dragging {{ opacity: .45; outline: 1px dashed #90cdf4; pointer-events: none; }}
    .slot.drop-target {{ outline: 1px solid #90cdf4; }}
    .slot-head {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; color: #90cdf4; font-size: 12px; margin-bottom: 6px; }}
    .slot-title {{ display: flex; align-items: center; gap: 8px; }}
    .drag-handle {{ width: 26px; height: 26px; padding: 0; margin: 0; display: inline-flex; flex-direction: column; justify-content: center; align-items: center; gap: 3px; background: #1f2937; border-color: #4a5568; cursor: grab; }}
    .drag-handle:active {{ cursor: grabbing; }}
    .drag-handle span {{ width: 12px; height: 2px; border-radius: 2px; background: #90cdf4; pointer-events: none; }}
    .delete-slot {{ padding: 5px 8px; margin: 0; background: #4a1f26; border-color: #74404a; color: #ffd6dc; font-size: 12px; }}
    .screen-actions {{ margin: 10px 0 2px; }}
    .muted {{ color: #a0aec0; font-size: 12px; }}
    .slot-index {{ color: #a0aec0; }}
    .slot input.slot-label {{ width: calc(100% - 18px); margin-bottom: 6px; color: #e8edf2; }}
    .slot select {{ width: 100%; }}
    .slot-value {{ min-height: 18px; margin-top: 6px; color: #ffffff; font-size: 13px; }}
    .settings {{ border-top: 1px solid #2d3748; border-bottom: 1px solid #2d3748; padding: 12px 0; margin: 12px 0; }}
    .settings input[type="password"] {{ min-width: 320px; }}
  </style>
  <script>
    let dragged = null;
    let dropTarget = null;
    let dragStartIndex = -1;
    let grid = null;
    function renumberSlots() {{
      document.querySelectorAll('.slot').forEach((slot, index) => {{
        slot.querySelector('input[type="hidden"]').value = index + 1;
        slot.querySelector('input[type="hidden"]').name = 'order_' + index;
        slot.querySelector('.slot-label').name = 'label_' + index;
        slot.querySelector('select').name = 'metric_' + index;
        slot.querySelector('input[type="checkbox"]').name = 'enabled_' + index;
        slot.querySelector('.slot-value').dataset.slotValue = index;
        slot.querySelector('.slot-index').textContent = index + 1;
      }});
      document.getElementById('fieldCount').value = document.querySelectorAll('.slot').length;
      updateAddButton();
    }}
    function updateAddButton() {{
      const addButton = document.getElementById('addSlot');
      if (!addButton) return;
      addButton.disabled = document.querySelectorAll('.slot').length >= 10;
    }}
    function bindSlot(slot) {{
      if (slot.dataset.bound === '1') return;
      slot.dataset.bound = '1';
      const handle = slot.querySelector('.drag-handle');
      handle.addEventListener('pointerdown', event => {{
        if (event.button !== 0) return;
        dragged = slot;
        dragStartIndex = [...grid.querySelectorAll('.slot')].indexOf(slot);
        slot.classList.add('dragging');
        event.preventDefault();
      }});
      slot.querySelector('.delete-slot').addEventListener('click', () => {{
        slot.remove();
        renumberSlots();
        refreshState();
      }});
    }}
    function addSlot() {{
      if (document.querySelectorAll('.slot').length >= 10) return;
      const template = document.getElementById('slotTemplate');
      const slot = template.content.firstElementChild.cloneNode(true);
      const select = slot.querySelector('select');
      if ([...select.options].some(option => option.value === 'cpu_load')) {{
        select.value = 'cpu_load';
      }}
      slot.querySelector('.slot-label').value = select.options[select.selectedIndex]?.text.replace('CPU load', 'CPU Load') || 'New';
      grid.appendChild(slot);
      bindSlot(slot);
      renumberSlots();
    }}
    function slotAtPoint(container, x, y) {{
      const element = document.elementFromPoint(x, y);
      const item = element ? element.closest('.slot') : null;
      if (item && container.contains(item) && item !== dragged) {{
        return item;
      }}
      return null;
    }}
    function setDropTarget(target) {{
      if (dropTarget && dropTarget !== target) {{
        dropTarget.classList.remove('drop-target');
      }}
      dropTarget = target;
      if (dropTarget) dropTarget.classList.add('drop-target');
    }}
    function moveDragged(event) {{
      if (!dragged) return;
      event.preventDefault();
      setDropTarget(slotAtPoint(grid, event.clientX, event.clientY));
    }}
    function finishDrag(event) {{
      if (!dragged) return;
      const target = event ? slotAtPoint(grid, event.clientX, event.clientY) || dropTarget : dropTarget;
      if (target && target !== dragged) {{
        const targetIndex = [...grid.querySelectorAll('.slot')].indexOf(target);
        if (targetIndex > dragStartIndex) {{
          grid.insertBefore(dragged, target.nextSibling);
        }} else {{
          grid.insertBefore(dragged, target);
        }}
      }}
      dragged.classList.remove('dragging');
      setDropTarget(null);
      dragged = null;
      dragStartIndex = -1;
      renumberSlots();
    }}
    window.addEventListener('DOMContentLoaded', () => {{
      grid = document.querySelector('.screen-grid');
      document.querySelectorAll('.slot').forEach(bindSlot);
      document.getElementById('addSlot').addEventListener('click', addSlot);
      document.addEventListener('pointermove', moveDragged);
      document.addEventListener('pointerup', finishDrag);
      document.addEventListener('pointercancel', finishDrag);
      renumberSlots();
      refreshState();
      setInterval(refreshState, 1000);
    }});
    async function refreshState() {{
      try {{
        const response = await fetch('/state', {{ cache: 'no-store' }});
        if (!response.ok) return;
        const state = await response.json();
        document.getElementById('statusText').textContent = 'Status: ' + state.status;
        if (state.timestamp) document.getElementById('screenTime').textContent = state.timestamp;
        if (state.title) document.getElementById('screenIp').textContent = state.title;
        document.querySelectorAll('.slot-value').forEach((node, index) => {{
          const field = state.fields[index];
          node.textContent = field ? field.value : '--';
        }});
      }} catch (error) {{
      }}
    }}
  </script>
</head>
<body>
<main>
  <h1>System Monitor Agent</h1>
  <form method="post" action="/restart">
    <input type="hidden" id="fieldCount" name="field_count" value="{len(fields)}">
    <div class="row">
      <label>Port <select name="port">{port_options}</select></label>
      <label>Interval <input type="number" step="0.2" min="0.2" name="interval" value="{config.get("interval", 1)}"> seconds</label>
      <label><input type="checkbox" name="startup" {"checked" if config["startup"] else ""}> Start with Windows</label>
    </div>
    <div class="settings">
      <div class="row">
        <label>OpenAI Admin Key <input type="password" name="openai_admin_key" placeholder="{"saved" if openai_config.get("admin_key") else "sk-admin..."}"></label>
        <label>Weekly Limit (manual) <input type="number" step="0.01" min="0" name="openai_weekly_budget" value="{openai_config.get("weekly_budget", 0)}"></label>
        <label>Monthly Limit (manual) <input type="number" step="0.01" min="0" name="openai_monthly_budget" value="{openai_config.get("monthly_budget", 0)}"></label>
      </div>
      <div class="muted">Detected drives: {html.escape(drive_summary)}</div>
    </div>
    <div class="screen">
      <div class="screen-top">
        <span id="screenTime">{dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</span>
      </div>
      <div class="screen-grid">{"".join(rows)}</div>
      <div class="screen-bottom" id="screenIp">{html.escape(local_ipv4())}</div>
      <div class="screen-actions">
        <button id="addSlot" type="button">Add element</button>
        <span class="muted">Maximum 10 elements on the display</span>
      </div>
    </div>
    <template id="slotTemplate">
      <section class="slot">
        <input type="hidden" name="order_new" value="1">
        <div class="slot-head">
          <div class="slot-title">
            <button class="drag-handle" type="button" title="Drag to reorder" aria-label="Drag to reorder">
              <span></span><span></span><span></span>
            </button>
            <span class="slot-index">1</span>
          </div>
          <label><input type="checkbox" name="enabled_new" checked> Show</label>
          <button class="delete-slot" type="button" title="Delete element" aria-label="Delete element">Delete</button>
        </div>
        <input class="slot-label" name="label_new" value="CPU Load">
        <select name="metric_new">{default_metric_options}</select>
        <div class="slot-value" data-slot-value="0">--</div>
      </section>
    </template>
    <button name="action" value="restart">Restart</button>
    <button name="action" value="stop">Stop</button>
    <button name="action" value="quit">Quit</button>
  </form>
  <div class="status" id="statusText">Status: {html.escape(status or runner.status)}</div>
</main>
</body>
</html>"""
        return page.encode("utf-8")

    def config_from_post(body: str) -> dict:
        form = urllib.parse.parse_qs(body)
        previous = load_config()
        valid_metrics = available_metric_names([
            form.get(f"metric_{idx}", [""])[0]
            for idx in range(10)
        ])
        fields = []
        try:
            field_count = min(10, max(0, int(form.get("field_count", ["10"])[0])))
        except ValueError:
            field_count = 10
        for idx in range(field_count):
            metric = form.get(f"metric_{idx}", [""])[0]
            if metric not in valid_metrics:
                continue
            try:
                order = int(form.get(f"order_{idx}", [idx + 1])[0])
            except ValueError:
                order = idx + 1
            fields.append({
                "metric": metric,
                "label": form.get(f"label_{idx}", [metric])[0],
                "enabled": f"enabled_{idx}" in form,
                "order": order,
            })
        admin_key = form.get("openai_admin_key", [""])[0].strip()
        if not admin_key:
            admin_key = (previous.get("openai", {}) or {}).get("admin_key", "")
        try:
            weekly_budget = float(form.get("openai_weekly_budget", ["0"])[0] or 0)
        except ValueError:
            weekly_budget = 0.0
        try:
            monthly_budget = float(form.get("openai_monthly_budget", ["0"])[0] or 0)
        except ValueError:
            monthly_budget = 0.0
        return {
            "port": form.get("port", ["COM3"])[0],
            "interval": float(form.get("interval", ["1"])[0]),
            "title": previous.get("title", "Windows Monitor"),
            "startup": "startup" in form,
            "fields": fields,
            "openai": {
                "admin_key": admin_key,
                "weekly_budget": weekly_budget,
                "monthly_budget": monthly_budget,
                "enabled": bool(admin_key),
            },
        }

    def current_state() -> dict:
        packet = runner.last_packet
        state = {
            "status": runner.status,
            "title": local_ipv4(),
            "timestamp": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "fields": [],
            "last_packet_age": round(time.time() - runner.last_packet_at, 1) if runner.last_packet_at else None,
        }
        if not packet.startswith("V2|"):
            return state

        parts = packet.split("|")
        if len(parts) >= 3:
            state["title"] = parts[1]
            state["timestamp"] = parts[2]
        for part in parts[3:13]:
            label, sep, value = part.partition("=")
            state["fields"].append({
                "label": label,
                "value": value if sep else part,
            })
        return state

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] == "/state":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(json.dumps(current_state()).encode("utf-8"))
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_page())

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            form = urllib.parse.parse_qs(body)
            action = form.get("action", ["restart"])[0]
            config = config_from_post(body)
            save_config(config)
            set_startup(config.get("startup", False))
            status = "Restarted"
            if action == "restart":
                runner.restart(config)
            elif action == "stop":
                runner.stop()
                status = "Stopped"
            elif action == "quit":
                runner.stop()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                status = "Quitting"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_page(status))

        def log_message(self, format: str, *args) -> None:
            return

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    except OSError:
        webbrowser.open(url)
        return

    tray: Optional[TrayIcon] = None

    def open_webui() -> None:
        webbrowser.open(url)

    def restart_from_tray() -> None:
        config = load_config()
        config["startup"] = is_startup_enabled()
        runner.restart(config)

    def stop_from_tray() -> None:
        runner.stop()

    def quit_from_tray() -> None:
        runner.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    tray = TrayIcon(
        "System Monitor Agent",
        open_webui,
        restart_from_tray,
        stop_from_tray,
        quit_from_tray,
    )
    tray.start()

    if auto_start or getattr(sys, "frozen", False):
        config = load_config()
        config["startup"] = is_startup_enabled()
        runner.start(config, force_reload=True)

    if open_browser_on_start:
        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        if tray:
            tray.stop()
        runner.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Windows USB serial agent for the ESP32 system monitor.")
    parser.add_argument("--port", help="Serial port, for example COM3. If omitted, the agent lists detected ports.")
    parser.add_argument("--interval", type=float, default=None, help="Refresh interval in seconds.")
    parser.add_argument("--gui", action="store_true", help="Open the configuration GUI.")
    parser.add_argument("--web", action="store_true", help="Open the browser-based configuration UI.")
    parser.add_argument("--minimized", action="store_true", help="Start GUI hidden and begin sending with saved settings.")
    parser.add_argument("--background", action="store_true", help="Start sending with saved settings without opening a UI.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.web or (getattr(sys, "frozen", False) and len(sys.argv) == 1):
        if not acquire_single_instance():
            import webbrowser

            webbrowser.open(WEB_UI_URL)
            return 0
        frozen_auto = getattr(sys, "frozen", False) and len(sys.argv) == 1
        run_web_gui(auto_start=frozen_auto, open_browser_on_start=args.web)
        return 0
    if args.gui:
        run_gui(minimized=args.minimized)
        return 0
    if args.background or args.minimized:
        config = load_config()
        run(config.get("port", "COM3"), float(config.get("interval", DEFAULT_INTERVAL)), config)
        return 0

    config = load_config()
    port = choose_port(args.port or config.get("port"))
    interval = args.interval if args.interval is not None else float(config.get("interval", DEFAULT_INTERVAL))
    try:
        run(port, interval, config)
        return 0
    except Exception as exc:
        log(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
