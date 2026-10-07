#!/usr/bin/env python3
"""
Proxmox VE Discord Rich Presence (RPC)
Displays live Proxmox server stats directly on your Discord user profile.
Supports multi-screen rotation, guest party badges, and storage monitoring.
"""

import json
import glob
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import socket
import socketserver
import http.server
import urllib.parse
import struct
import threading
import time
import urllib3
import requests
from pypresence import Presence, DiscordNotFound, PipeClosed

# Suppress self-signed certificate warnings from Proxmox
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# If running windowless via pythonw.exe, sys.stdout and sys.stderr are None.
# Redirect them to a local log file so print statements work seamlessly.
LOG_DIR = os.path.dirname(os.path.abspath(__file__))
if "pythonw" in sys.executable.lower() or sys.stdout is None:
    try:
        log_file = open(os.path.join(LOG_DIR, "proxmox_rpc.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = log_file
        sys.stderr = log_file
    except Exception:
        pass
elif sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True, write_through=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True, write_through=True)
    except Exception:
        pass

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        print(f"[ERROR] Config file not found at {CONFIG_PATH}", flush=True)
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


DISCORD_GAMES_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "discord_games_db.json")
_discord_games_db = {}
if os.path.exists(DISCORD_GAMES_DB_PATH):
    try:
        with open(DISCORD_GAMES_DB_PATH, "r", encoding="utf-8") as f:
            _discord_games_db = json.load(f)
    except Exception as e:
        print(f"[WARN] Failed to load discord_games_db.json: {e}", flush=True)


def format_uptime(seconds):
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h"
    elif hours > 0:
        return f"{hours}h {minutes}m"
    elif minutes > 0:
        return f"{minutes}m"
    return "< 1m"


_cached_proxmox_stats = None
_cached_proxmox_time = 0.0
PROXMOX_CACHE_TTL = 18.0  # seconds (one full 3-screen cycle at 6s interval)


_pve_worker_started = False
_pve_lock = threading.Lock()


def _pve_stats_worker():
    while True:
        try:
            cfg = load_config()
            s = fetch_proxmox_stats(cfg)
            with _pve_lock:
                global _cached_proxmox_stats, _cached_proxmox_time
                _cached_proxmox_stats = s
                _cached_proxmox_time = time.time()
        except Exception:
            pass
        time.sleep(10)


def make_default_proxmox_stats(cfg):
    return {
        "node": cfg.get("proxmox_node", "Protutech"),
        "cpu_pct": 0.0,
        "mem_used": 0.0,
        "mem_total": 1.0,
        "mem_pct": 0.0,
        "storage_pool": "local",
        "storage_used_gb": 0.0,
        "storage_total_tb": 1.0,
        "storage_pct": 0.0,
        "running_vms": 0,
        "total_vms": 0,
        "running_lxcs": 0,
        "total_lxcs": 0,
        "total_guests": 0,
        "running_guests": 0,
        "uptime": "Standby",
        "mc_status": "Offline",
        "offline": True
    }


def get_cached_proxmox_stats(cfg, force=False):
    """
    Returns cached Proxmox stats instantly from memory without blocking the rotation loop.
    A dedicated background daemon thread keeps the metrics fresh every 10 seconds.
    """
    global _pve_worker_started, _cached_proxmox_stats
    if not _pve_worker_started:
        _pve_worker_started = True
        t = threading.Thread(target=_pve_stats_worker, daemon=True)
        t.start()
    with _pve_lock:
        if _cached_proxmox_stats is not None:
            return _cached_proxmox_stats
    try:
        s = fetch_proxmox_stats(cfg)
        with _pve_lock:
            _cached_proxmox_stats = s
        return s
    except Exception:
        return make_default_proxmox_stats(cfg)


def fetch_proxmox_stats(cfg):
    host = cfg["proxmox_host"].rstrip("/")
    node = cfg["proxmox_node"]
    headers = {
        "Authorization": f"PVEAPIToken={cfg['proxmox_token_id']}={cfg['proxmox_token_secret']}"
    }

    # 1. Fetch Cluster Node Overview (for smoothed cluster CPU & memory)
    nodes_url = f"{host}/api2/json/nodes"
    nodes_res = requests.get(nodes_url, headers=headers, verify=False, timeout=3.0)
    nodes_res.raise_for_status()
    cluster_nodes = nodes_res.json().get("data", [])
    current_node_summary = next((n for n in cluster_nodes if n.get("node") == node), {})

    # 2. Fetch Detailed Node Status
    status_url = f"{host}/api2/json/nodes/{node}/status"
    node_res = requests.get(status_url, headers=headers, verify=False, timeout=8)
    node_data = node_res.json().get("data", {}) if node_res.status_code == 200 else {}

    # 3. Fetch QEMU Virtual Machines
    qemu_url = f"{host}/api2/json/nodes/{node}/qemu"
    vms_res = requests.get(qemu_url, headers=headers, verify=False, timeout=8)
    vms_data = vms_res.json().get("data", []) if vms_res.status_code == 200 else []

    # 4. Fetch LXC Containers
    lxc_url = f"{host}/api2/json/nodes/{node}/lxc"
    lxc_res = requests.get(lxc_url, headers=headers, verify=False, timeout=8)
    lxc_data = lxc_res.json().get("data", []) if lxc_res.status_code == 200 else []

    # 5. Fetch Storage Pools (find largest storage pool like local-lvm)
    storage_url = f"{host}/api2/json/nodes/{node}/storage"
    storage_res = requests.get(storage_url, headers=headers, verify=False, timeout=8)
    storage_data = storage_res.json().get("data", []) if storage_res.status_code == 200 else []
    active_pools = [s for s in storage_data if s.get("active")]
    primary_pool = max(active_pools, key=lambda s: s.get("total", 0), default={})

    pool_name = primary_pool.get("storage", "local-lvm")
    pool_used_gb = primary_pool.get("used", 0) / (1024 ** 3)
    pool_total_gb = primary_pool.get("total", 1) / (1024 ** 3)
    pool_total_tb = pool_total_gb / 1024
    pool_pct = (pool_used_gb / pool_total_gb) * 100 if pool_total_gb else 0

    # Compute CPU & Memory
    raw_cpu = current_node_summary.get("cpu") if current_node_summary.get("cpu") is not None else node_data.get("cpu", 0)
    cpu_pct = float(raw_cpu or 0) * 100

    mem_used = (current_node_summary.get("mem") or node_data.get("memory", {}).get("used", 0)) / (1024 ** 3)
    mem_total = (current_node_summary.get("maxmem") or node_data.get("memory", {}).get("total", 1)) / (1024 ** 3)
    mem_pct = (mem_used / mem_total) * 100 if mem_total else 0

    running_vms = sum(1 for vm in vms_data if vm.get("status") == "running")
    total_vms = len(vms_data)

    running_lxcs = sum(1 for c in lxc_data if c.get("status") == "running")
    total_lxcs = len(lxc_data)

    uptime_sec = current_node_summary.get("uptime") or node_data.get("uptime", 0)
    uptime_str = format_uptime(uptime_sec)

    # Optional check for Minecraft container (LXC 102 discopanelminecraft)
    mc_container = next((c for c in lxc_data if "minecraft" in c.get("name", "").lower() or c.get("vmid") == 102), None)
    mc_status = "Online" if mc_container and mc_container.get("status") == "running" else "Offline"

    return {
        "node": node,
        "cpu_pct": cpu_pct,
        "mem_used": mem_used,
        "mem_total": mem_total,
        "mem_pct": mem_pct,
        "storage_pool": pool_name,
        "storage_used_gb": pool_used_gb,
        "storage_total_tb": pool_total_tb,
        "storage_pct": pool_pct,
        "running_vms": running_vms,
        "total_vms": total_vms,
        "running_lxcs": running_lxcs,
        "total_lxcs": total_lxcs,
        "total_guests": total_vms + total_lxcs,
        "running_guests": running_vms + running_lxcs,
        "uptime": uptime_str,
        "mc_status": mc_status
    }


COIN_FULL_NAMES = {
    "prl": "Pearl",
    "xel": "Xelis",
    "xmr": "Monero",
    "rvn": "Ravencoin",
    "erg": "Ergo",
    "nexa": "Nexa",
    "iron": "Iron Fish",
    "cfx": "Conflux",
    "zeph": "Zephyr",
    "kls": "Karlsen",
    "pyi": "Pyrin",
    "xna": "Neoxa",
    "clore": "Clore.ai",
    "sal": "Salvia",
    "blocx": "BLOCX",
    "xtm": "Torum",
    "ethw": "Ethereum PoW",
    "qtc": "Quantus",
    "btc": "Bitcoin",
    "etc": "Ethereum Classic",
    "kas": "Kaspa",
    "alph": "Alephium",
    "flux": "Flux",
    "karlsen": "Karlsen",
    "dynex": "Dynex",
    "octa": "OctaSpace"
}


def fetch_kryptex_stats(cfg):
    """
    Reads local Kryptex statistics (mining status, hashrate, hardware temps, and balance)
    directly from the local Kryptex database in read-only mode without blocking or locking.
    """
    db_path = cfg.get("kryptex_db_path")
    if not db_path:
        db_path = os.path.expandvars(r"%APPDATA%\Kryptex\kryptex.db")

    if not os.path.exists(db_path):
        return None

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 1. Account balance
        cur.execute("SELECT total, withdrawable FROM balance LIMIT 1;")
        bal = cur.fetchone()
        balance_usd = bal["total"] if bal else None

        # 2. Latest readings
        cur.execute("SELECT id, timestamp FROM reading ORDER BY id DESC LIMIT 1;")
        latest = cur.fetchone()
        if not latest:
            conn.close()
            return {"mining": False, "balance": balance_usd, "gpu": None, "cpu": None}

        reading_id = latest["id"]
        ts = latest["timestamp"] / 1000.0
        is_active = (time.time() - ts) < 60

        cur.execute("""
            SELECT d.id, d.name, d.type_id, dr.core_temperature, dr.power_usage, dr.fan_speed, dr.core_clock
            FROM device_reading dr
            JOIN device d ON dr.device_id = d.id
            WHERE dr.reading_id = ?
        """, (reading_id,))
        dev_readings = {r["id"]: dict(r) for r in cur.fetchall()}

        cur.execute("""
            SELECT pdr.coin_algorithm_id, pdr.hashrate, c.name as coin, a.name as algo, pd.device_id
            FROM process_device_reading pdr
            JOIN coin_algorithm ca ON pdr.coin_algorithm_id = ca.id
            JOIN coin c ON ca.coin_id = c.id
            JOIN algorithm a ON ca.algorithm_id = a.id
            JOIN process_device pd ON pdr.process_device_id = pd.id
            WHERE pdr.reading_id = ?
        """, (reading_id,))

        gpu_stat = None
        cpu_stat = None
        any_hashrate = False

        def fmt_hr(hr):
            if hr >= 1e12:
                return f"{hr / 1e12:.1f} TH/s"
            elif hr >= 1e9:
                return f"{hr / 1e9:.1f} GH/s"
            elif hr >= 1e6:
                return f"{hr / 1e6:.1f} MH/s"
            elif hr >= 1e3:
                return f"{hr / 1e3:.1f} kH/s"
            return f"{hr:.0f} H/s"

        for p in cur.fetchall():
            dev_id = p["device_id"]
            if dev_id in dev_readings:
                dr = dev_readings[dev_id]
                hr = p["hashrate"] or 0
                if hr > 0:
                    any_hashrate = True
                coin_slug = (p["coin"] or "").lower()
                coin_full = COIN_FULL_NAMES.get(coin_slug, coin_slug.upper())
                info = {
                    "name": dr["name"],
                    "temp": dr["core_temperature"],
                    "power": dr["power_usage"],
                    "coin": p["coin"].upper(),
                    "coin_full": coin_full,
                    "hashrate": fmt_hr(hr)
                }
                if dr["type_id"] == 2:
                    gpu_stat = info
                elif dr["type_id"] == 1:
                    cpu_stat = info

        conn.close()
        return {
            "mining": is_active and any_hashrate,
            "balance": balance_usd,
            "gpu": gpu_stat,
            "cpu": cpu_stat
        }
    except Exception:
        return None


def get_process_command_line(pid):
    """
    Extracts the full command line of any Windows process via fast NT kernel querying.
    Used to inspect running Java/Minecraft instances, modpack paths, and JVM arguments.
    """
    if not pid or sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        ntdll = ctypes.windll.ntdll
        kernel32 = ctypes.windll.kernel32

        class PROCESS_BASIC_INFORMATION(ctypes.Structure):
            _fields_ = [
                ('ExitStatus', wintypes.ULONG),
                ('PebBaseAddress', ctypes.c_void_p),
                ('AffinityMask', ctypes.c_void_p),
                ('BasePriority', wintypes.LONG),
                ('UniqueProcessId', ctypes.c_void_p),
                ('InheritedFromUniqueProcessId', ctypes.c_void_p)
            ]

        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [
                ('Length', wintypes.USHORT),
                ('MaximumLength', wintypes.USHORT),
                ('Buffer', ctypes.c_void_p)
            ]

        h = kernel32.OpenProcess(0x1000 | 0x0010, False, int(pid))
        if not h:
            return ""
        try:
            pbi = PROCESS_BASIC_INFORMATION()
            ret = wintypes.ULONG()
            status = ntdll.NtQueryInformationProcess(h, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), ctypes.byref(ret))
            if status != 0 or not pbi.PebBaseAddress:
                return ""
            user_params = ctypes.c_void_p()
            read = ctypes.c_size_t()
            kernel32.ReadProcessMemory(h, ctypes.c_void_p(pbi.PebBaseAddress + 0x20), ctypes.byref(user_params), 8, ctypes.byref(read))
            if not user_params.value:
                return ""
            cmd_uni = UNICODE_STRING()
            kernel32.ReadProcessMemory(h, ctypes.c_void_p(user_params.value + 0x70), ctypes.byref(cmd_uni), ctypes.sizeof(cmd_uni), ctypes.byref(read))
            if not cmd_uni.Length or not cmd_uni.Buffer:
                return ""
            buf = ctypes.create_unicode_buffer(cmd_uni.Length // 2)
            kernel32.ReadProcessMemory(h, ctypes.c_void_p(cmd_uni.Buffer), buf, cmd_uni.Length, ctypes.byref(read))
            return buf.value
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return ""


def get_process_creation_time(pid):
    """
    Returns the Unix epoch timestamp of when a process was created.
    Ensures Discord's in-game elapsed timer matches the true launch time.
    """
    if not pid or sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32

        class FILETIME(ctypes.Structure):
            _fields_ = [('dwLowDateTime', wintypes.DWORD), ('dwHighDateTime', wintypes.DWORD)]

        h = kernel32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return None
        ct, et, kt, ut = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        kernel32.GetProcessTimes(h, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut))
        kernel32.CloseHandle(h)
        filetime = (ct.dwHighDateTime << 32) + ct.dwLowDateTime
        # Convert Windows 100ns intervals since Jan 1, 1601 to Unix epoch
        return (filetime - 116444736000000000) / 10000000
    except Exception:
        return None


def detect_minecraft_instance(cfg=None):
    """
    Real-time local Minecraft & Modpack Detector:
    1. Scans running processes for javaw.exe, java.exe, Minecraft.Windows.exe.
    2. Inspects JVM arguments (--gameDir, --version, --fml.mcVersion, -Dminecraft.launcher.brand).
    3. Resolves modpack metadata (CurseForge minecraftinstance.json, manifest.json, Prism instance.cfg, Modrinth).
    4. Counts installed mods and parses logs/latest.log for in-game activity (Singleplayer, Multiplayer, Main Menu).
    """
    if sys.platform != "win32":
        return {
            "online": False,
            "name": "Minecraft",
            "modpack_name": None,
            "is_modpack": False,
            "icon_url": None,
            "local_icon_path": None,
            "mods_count": 0,
            "mc_version": "",
            "modloader": "",
            "launcher": "",
            "instance_path": "",
            "pid": None,
            "activity": "Not Playing",
            "details": "Minecraft: Not Playing",
            "state": "No modpack or game running"
        }

    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ('dwSize', wintypes.DWORD),
                ('cntUsage', wintypes.DWORD),
                ('th32ProcessID', wintypes.DWORD),
                ('th32DefaultHeapID', ctypes.POINTER(wintypes.ULONG)),
                ('th32ModuleID', wintypes.DWORD),
                ('cntThreads', wintypes.DWORD),
                ('th32ParentProcessID', wintypes.DWORD),
                ('pcPriClassBase', wintypes.LONG),
                ('dwFlags', wintypes.DWORD),
                ('szExeFile', ctypes.c_char * 260),
            ]

        hSnap = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
        candidates = []
        if kernel32.Process32First(hSnap, ctypes.byref(pe)):
            while True:
                ename = pe.szExeFile.decode('latin1', errors='ignore').lower()
                if ename in ('javaw.exe', 'java.exe', 'minecraft.windows.exe', 'minecraft.exe'):
                    candidates.append((pe.th32ProcessID, ename))
                if not kernel32.Process32Next(hSnap, ctypes.byref(pe)):
                    break
        kernel32.CloseHandle(hSnap)

        for pid, ename in candidates:
            if ename == 'minecraft.windows.exe':
                p_time = get_process_creation_time(pid) or time.time()
                return {
                    "online": True,
                    "name": "Minecraft (Bedrock)",
                    "modpack_name": "Minecraft Bedrock Edition",
                    "is_modpack": False,
                    "icon_url": BUILTIN_GAME_ICONS.get("minecraft"),
                    "local_icon_path": None,
                    "mods_count": 0,
                    "mc_version": "Bedrock",
                    "modloader": "Bedrock",
                    "launcher": "Windows Store",
                    "instance_path": "",
                    "pid": pid,
                    "activity": "In-Game",
                    "start_time": p_time,
                    "details": "Playing Minecraft: Bedrock Edition",
                    "state": "In-Game • Windows"
                }

            cmd = get_process_command_line(pid)
            if not cmd:
                continue
            cmd_lower = cmd.lower()

            # Ignore launcher crash helpers or background updater assistants
            if 'crash_assistant' in cmd_lower and 'net.minecraft' not in cmd_lower and '--gamedir' not in cmd_lower:
                continue

            # Must contain Minecraft launch markers
            if any(k in cmd_lower for k in ('minecraft', '--gamedir', 'net.minecraft', 'cpw.mods', '--fml.', 'fabricmc', 'quiltmc')):
                m_dir = re.search(r'--gameDir\s+(?:"([^"]+)"|([^\s]+))', cmd, re.I)
                game_dir = m_dir.group(1) or m_dir.group(2) if m_dir else ""

                m_ver = re.search(r'--version\s+(?:"([^"]+)"|([^\s]+))', cmd, re.I)
                version_str = m_ver.group(1) or m_ver.group(2) if m_ver else ""

                m_mc = re.search(r'--fml\.mcVersion\s+([^\s]+)', cmd, re.I)
                mc_ver = m_mc.group(1) if m_mc else ""

                m_brand = re.search(r'-Dminecraft\.launcher\.brand=([^\s]+)', cmd, re.I)
                brand = m_brand.group(1) if m_brand else ""

                modpack_name = None
                loader_name = None
                mods_count = 0
                is_modpack = False
                icon_url = None
                local_icon_path = None
                instance_name = os.path.basename(os.path.normpath(game_dir)) if game_dir else ""

                if game_dir and os.path.isdir(game_dir):
                    # 1. CurseForge Instance Manifest
                    mf = os.path.join(game_dir, 'minecraftinstance.json')
                    if os.path.exists(mf):
                        try:
                            with open(mf, 'r', encoding='utf-8', errors='ignore') as f:
                                d = json.load(f)
                                modpack_name = d.get('name')
                                if not mc_ver:
                                    mc_ver = d.get('gameVersion')
                                loader_name = d.get('baseModLoader', {}).get('name')

                                # CurseForge icon extraction (CDN avatar/thumbnail)
                                imp = d.get('installedModpack') or {}
                                if isinstance(imp, dict):
                                    icon_url = imp.get('thumbnailUrl') or imp.get('avatarUrl') or imp.get('logoUrl')
                                if not icon_url:
                                    man_sub = d.get('manifest') or {}
                                    if isinstance(man_sub, dict):
                                        icon_url = man_sub.get('image') or man_sub.get('thumbnailUrl') or man_sub.get('avatarUrl')
                                if not icon_url:
                                    icon_url = d.get('thumbnailUrl') or d.get('avatarUrl')
                                if not icon_url:
                                    p_img = d.get('profileImagePath')
                                    if p_img and isinstance(p_img, str):
                                        if p_img.startswith('http://') or p_img.startswith('https://'):
                                            icon_url = p_img
                                        elif os.path.isfile(p_img):
                                            local_icon_path = p_img
                        except Exception:
                            pass

                    # 2. Modpack manifest.json
                    man = os.path.join(game_dir, 'manifest.json')
                    if os.path.exists(man):
                        try:
                            with open(man, 'r', encoding='utf-8', errors='ignore') as f:
                                d = json.load(f)
                                if not modpack_name:
                                    modpack_name = d.get('name')
                                if not mc_ver:
                                    mc_ver = d.get('minecraft', {}).get('version')
                                if not loader_name:
                                    lds = d.get('minecraft', {}).get('modLoaders', [])
                                    if lds:
                                        loader_name = lds[0].get('id')
                                if not icon_url:
                                    icon_url = d.get('image') or d.get('thumbnailUrl')
                        except Exception:
                            pass

                    # 3. Prism Launcher / MultiMC instance.cfg
                    prism_cfg = os.path.join(game_dir, 'instance.cfg')
                    if os.path.exists(prism_cfg):
                        try:
                            with open(prism_cfg, 'r', encoding='utf-8', errors='ignore') as f:
                                for line in f:
                                    if line.startswith('name=') and not modpack_name:
                                        modpack_name = line.strip().split('=', 1)[1]
                                    elif line.startswith('IntendedVersion=') and not mc_ver:
                                        mc_ver = line.strip().split('=', 1)[1]
                                    elif line.startswith('iconKey=') and not local_icon_path:
                                        icon_k = line.strip().split('=', 1)[1]
                                        c_loc = os.path.join(game_dir, f"{icon_k}.png")
                                        if os.path.isfile(c_loc):
                                            local_icon_path = c_loc
                        except Exception:
                            pass

                    # 4. Modrinth modrinth.index.json
                    mr_file = os.path.join(game_dir, 'modrinth.index.json')
                    if os.path.exists(mr_file):
                        try:
                            with open(mr_file, 'r', encoding='utf-8', errors='ignore') as f:
                                mr_data = json.load(f)
                                if not modpack_name:
                                    modpack_name = mr_data.get('name')
                                if not mc_ver:
                                    mc_ver = mr_data.get('gameVersion')
                                if not icon_url:
                                    icon_url = mr_data.get('icon_url') or mr_data.get('imageUrl')
                        except Exception:
                            pass

                    # 5. Local modpack image files in instance folder
                    if not icon_url and not local_icon_path:
                        for fname in ('icon.png', 'icon.webp', 'icon.jpg', 'instance.png', 'cover.png'):
                            ip = os.path.join(game_dir, fname)
                            if os.path.isfile(ip):
                                local_icon_path = ip
                                break

                    # 6. Count installed mod JARs
                    mods_dir = os.path.join(game_dir, 'mods')
                    if os.path.isdir(mods_dir):
                        mods_count = len(glob.glob(os.path.join(mods_dir, '*.jar')))

                    if mods_count > 0 or loader_name or (modpack_name and modpack_name.lower() != 'vanilla'):
                        is_modpack = True

                    if not modpack_name:
                        if instance_name and instance_name.lower() not in ('.minecraft', 'minecraft'):
                            modpack_name = instance_name
                        elif is_modpack:
                            modpack_name = "Modded Minecraft"
                        else:
                            modpack_name = "Vanilla Minecraft"

                # Version fallback
                if not mc_ver and version_str:
                    clean_ver = re.search(r'1\.\d+(?:\.\d+)?', version_str)
                    mc_ver = clean_ver.group(0) if clean_ver else version_str

                # Loader fallback
                if not loader_name:
                    if 'neoforge' in cmd_lower or (version_str and 'neoforge' in version_str.lower()):
                        loader_name = "NeoForge"
                    elif 'forge' in cmd_lower or (version_str and 'forge' in version_str.lower()):
                        loader_name = "Forge"
                    elif 'fabric' in cmd_lower or (version_str and 'fabric' in version_str.lower()):
                        loader_name = "Fabric"
                    elif 'quilt' in cmd_lower or (version_str and 'quilt' in version_str.lower()):
                        loader_name = "Quilt"

                # Clean loader formatting
                if loader_name:
                    loader_clean = loader_name.replace('-', ' ')
                    if loader_clean.lower().startswith('forge'):
                        loader_clean = 'Forge ' + loader_clean[5:].strip()
                    elif loader_clean.lower().startswith('neoforge'):
                        loader_clean = 'NeoForge ' + loader_clean[8:].strip()
                    elif loader_clean.lower().startswith('fabric'):
                        loader_clean = 'Fabric ' + loader_clean[6:].strip()
                    loader_name = loader_clean

                # 6. Parse latest.log for in-game activity (Singleplayer, Multiplayer, Main Menu)
                activity_state = "In-Game"
                if game_dir:
                    log_file = os.path.join(game_dir, 'logs', 'latest.log')
                    if os.path.isfile(log_file):
                        try:
                            with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                                lines = f.readlines()[-150:]
                            for line in lines:
                                if 'title_screen' in line:
                                    activity_state = "Main Menu"
                                elif 'Starting integrated server' in line or 'Loaded 0 advancements' in line:
                                    activity_state = "Singleplayer"
                                elif 'Connecting to ' in line:
                                    m_srv = re.search(r'Connecting to ([^\s,]+)', line)
                                    activity_state = f"Multiplayer ({m_srv.group(1)})" if m_srv else "Multiplayer"
                                elif 'Saving and stopping server' in line:
                                    activity_state = "Main Menu"
                        except Exception:
                            pass

                # Build details and state
                if is_modpack:
                    details = f"Playing: {modpack_name}"
                    state_parts = []
                    if mods_count > 0:
                        state_parts.append(f"{mods_count} Mods")
                    if loader_name:
                        state_parts.append(loader_name)
                    elif mc_ver:
                        state_parts.append(f"v{mc_ver}")
                    if activity_state:
                        state_parts.append(activity_state)
                    state = " | ".join(state_parts)
                else:
                    details = f"Playing Minecraft v{mc_ver}" if mc_ver else "Playing Minecraft"
                    state = f"Vanilla Minecraft | {activity_state}"

                p_time = get_process_creation_time(pid) or time.time()
                launcher_name = brand or ("CurseForge" if "curseforge" in (game_dir or "").lower() else "")

                return {
                    "online": True,
                    "name": modpack_name or "Minecraft",
                    "modpack_name": modpack_name or "Minecraft",
                    "is_modpack": is_modpack,
                    "icon_url": icon_url,
                    "local_icon_path": local_icon_path,
                    "mods_count": mods_count,
                    "mc_version": mc_ver or "",
                    "modloader": loader_name or "",
                    "launcher": launcher_name,
                    "instance_path": game_dir,
                    "pid": pid,
                    "activity": activity_state,
                    "start_time": p_time,
                    "details": details,
                    "state": state
                }

    except Exception as e:
        print(f"[WARN] Minecraft detection error: {e}", flush=True)

    return {
        "online": False,
        "name": "Minecraft",
        "modpack_name": None,
        "is_modpack": False,
        "icon_url": None,
        "local_icon_path": None,
        "mods_count": 0,
        "mc_version": "",
        "modloader": "",
        "launcher": "",
        "instance_path": "",
        "pid": None,
        "activity": "Not Playing",
        "details": "Minecraft: Not Playing",
        "state": "No modpack or game running"
    }


_cached_mc_status = None
_cached_mc_time = 0.0
_mc_lock = threading.Lock()


def fetch_minecraft_status(cfg=None):
    """
    Returns Minecraft & Modpack status cached for 3 seconds to keep rotations lightning snappy.
    """
    global _cached_mc_status, _cached_mc_time
    now = time.time()
    with _mc_lock:
        if _cached_mc_status is not None and (now - _cached_mc_time) < 3.0:
            return _cached_mc_status
    st = detect_minecraft_instance(cfg)
    with _mc_lock:
        _cached_mc_status = st
        _cached_mc_time = now
    return st


SPEED_CACHE_PATH = os.path.join(LOG_DIR, "speed_cache.json")


def load_speed_cache():
    defaults = {
        "down_mbps": 8120.0,
        "up_mbps": 4420.0,
        "ping_ms": 2.2,
        "last_speed_time": 0.0,
        "last_ping_time": 0.0
    }
    if os.path.isfile(SPEED_CACHE_PATH):
        try:
            with open(SPEED_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in data.items():
                    if v is not None:
                        defaults[k] = v
        except Exception:
            pass
    return defaults


def save_speed_cache(stats):
    try:
        with open(SPEED_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "down_mbps": stats.get("down_mbps"),
                "up_mbps": stats.get("up_mbps"),
                "ping_ms": stats.get("ping_ms")
            }, f, indent=2)
    except Exception:
        pass


_net_stats = load_speed_cache()
_net_worker_started = False
_net_lock = threading.Lock()


def measure_ping(host="1.1.1.1", port=443, count=3):
    """
    Measures low-latency TCP ping to reliable DNS hosts (Cloudflare / Google).
    """
    latencies = []
    for _ in range(count):
        t0 = time.perf_counter()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        try:
            s.connect((host, port))
            lat = (time.perf_counter() - t0) * 1000.0
            latencies.append(lat)
        except Exception:
            pass
        finally:
            s.close()
    return round(sum(latencies) / len(latencies), 1) if latencies else None


def find_speedtest_cli():
    """
    Locates the official Ookla Speedtest CLI executable.
    Supports bundled bin, system PATH, or standard WinGet locations.
    """
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "speedtest.exe"),
        shutil.which("speedtest"),
        shutil.which("speedtest.exe"),
        r"C:\Users\Andre\AppData\Local\Microsoft\WinGet\Packages\Ookla.Speedtest.CLI_Microsoft.Winget.Source_8wekyb3d8bbwe\speedtest.exe"
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def measure_speeds():
    """
    Measures multi-gigabit bandwidth using the official Ookla Speedtest CLI.
    Capable of testing up to 10 Gbps+ lines with real low-latency ping.
    Falls back to parallel multi-connection Cloudflare test if CLI is not present.
    """
    speedtest_bin = find_speedtest_cli()
    if speedtest_bin:
        try:
            cmd = [speedtest_bin, "--accept-license", "--accept-gdpr", "-f", "json"]
            flags = 0x08000000 if sys.platform == "win32" else 0
            proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=flags, timeout=75)
            for line in proc.stdout.splitlines():
                line = line.strip()
                if line.startswith("{") and "bandwidth" in line:
                    data = json.loads(line)
                    # Bandwidth is reported in Bytes/sec; multiply by 8 for bits/sec
                    down_mbps = round((data["download"]["bandwidth"] * 8) / 1e6, 1)
                    up_mbps = round((data["upload"]["bandwidth"] * 8) / 1e6, 1)
                    if "ping" in data and "latency" in data["ping"]:
                        with _net_lock:
                            _net_stats["ping_ms"] = round(data["ping"]["latency"], 1)
                            _net_stats["last_ping_time"] = time.time()
                    return down_mbps, up_mbps
        except Exception:
            pass

    # Fallback: lightweight Cloudflare speed test
    down_mbps = None
    up_mbps = None
    try:
        t0 = time.perf_counter()
        r = requests.get("https://speed.cloudflare.com/__down?bytes=50000000", timeout=10)
        dur = time.perf_counter() - t0
        if r.status_code == 200 and dur > 0:
            down_mbps = round((len(r.content) * 8) / (dur * 1_000_000), 1)
    except Exception:
        pass

    try:
        payload = b"0" * (10 * 1024 * 1024)
        t0 = time.perf_counter()
        r = requests.post("https://speed.cloudflare.com/__up", data=payload, timeout=10)
        dur = time.perf_counter() - t0
        if r.status_code == 200 and dur > 0:
            up_mbps = round((len(payload) * 8) / (dur * 1_000_000), 1)
    except Exception:
        pass

    return down_mbps, up_mbps


def _net_stats_worker():
    """
    Background worker that updates ping every 30s and speed tests every configured interval.
    """
    while True:
        try:
            cfg = load_config()
            if not cfg.get("enable_speed_screen", False):
                time.sleep(5)
                continue

            now = time.time()
            interval_min = cfg.get("speedtest_interval_minutes", 30)
            interval_sec = max(60, int(interval_min * 60))
            ping_host = cfg.get("ping_host", "1.1.1.1")

            # 1. Update Ping every 30 seconds
            if now - _net_stats["last_ping_time"] >= 30.0:
                p = measure_ping(ping_host)
                if p is not None:
                    with _net_lock:
                        _net_stats["ping_ms"] = p
                        _net_stats["last_ping_time"] = now

            # 2. Update Speeds every interval or on initial run
            # SAFETY GUARD: Never run heavy bandwidth speed tests while user is actively playing a game!
            is_gaming = bool(_game_tracker.get("current"))
            if not is_gaming:
                if now - _net_stats["last_speed_time"] >= interval_sec or _net_stats["down_mbps"] is None:
                    d, u = measure_speeds()
                    with _net_lock:
                        if d is not None:
                            _net_stats["down_mbps"] = d
                        if u is not None:
                            _net_stats["up_mbps"] = u
                        _net_stats["last_speed_time"] = now
                        save_speed_cache(_net_stats)

        except Exception:
            pass
        time.sleep(5)


def start_net_worker_if_needed(cfg):
    global _net_worker_started
    if cfg.get("enable_speed_screen", False) and not _net_worker_started:
        _net_worker_started = True
        t = threading.Thread(target=_net_stats_worker, daemon=True)
        t.start()


# Steam Profile Integration & Caching
_steam_worker_started = False
_steam_lock = threading.Lock()
_cached_steam_stats = None
STEAM_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "steam_cache.json")


def load_steam_cache():
    if os.path.exists(STEAM_CACHE_FILE):
        try:
            with open(STEAM_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_steam_cache(data):
    try:
        with open(STEAM_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def get_local_steam_id64():
    """
    Auto-detect active Steam user SteamID64 from Windows registry or config files.
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess") as key:
            active_user, _ = winreg.QueryValueEx(key, "ActiveUser")
            if active_user and active_user > 0:
                return str(76561197960265728 + active_user)
    except Exception:
        pass

    for steam_dir in [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]:
        vdf_path = os.path.join(steam_dir, "config", "loginusers.vdf")
        if os.path.exists(vdf_path):
            try:
                with open(vdf_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                auto_m = re.search(r'"(\d{17})"\s*\{[^}]*"AutoLogin"\s*"1"', content, re.DOTALL)
                if auto_m:
                    return auto_m.group(1)
                all_ids = re.findall(r'"(7656\d{13})"', content)
                if all_ids:
                    return all_ids[0]
            except Exception:
                pass
    return None


def fetch_steam_profile(steam_id=None):
    """
    Fetch public Steam profile details: avatar, persona name, level, games count, items count,
    badges count, achievements count, perfect games count, hours played, and online status.
    """
    sid = str(steam_id).strip() if steam_id else get_local_steam_id64()
    if not sid:
        return None

    persona = "Steam User"
    avatar_url = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/steam.png"
    level = "0"
    games_count = "0"
    items_count = "0"
    badges_count = "0"
    achievements_count = "0"
    perfect_games_count = "0"
    hours_count = "0"
    online_status = "Online"

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9'
    }

    # 1. XML endpoint
    try:
        xml_url = f"https://steamcommunity.com/profiles/{sid}/?xml=1"
        resp = requests.get(xml_url, headers=headers, timeout=5.0)
        if resp.status_code == 200:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(resp.content)
            p_elem = root.find("steamID")
            if p_elem is not None and p_elem.text:
                persona = p_elem.text

            a_elem = root.find("avatarFull") or root.find("avatarMedium") or root.find("avatarIcon")
            if a_elem is not None and a_elem.text:
                avatar_url = a_elem.text.replace("avatars.fastly.steamstatic.com", "avatars.steamstatic.com")

            state_elem = root.find("onlineState")
            if state_elem is not None and state_elem.text:
                st_val = state_elem.text.strip().lower()
                if st_val == "in-game":
                    online_status = "In-Game"
                elif st_val == "online":
                    online_status = "Online"
                elif st_val == "offline":
                    online_status = "Offline"
    except Exception:
        pass

    # 2. HTML endpoint for stats
    try:
        profile_url = f"https://steamcommunity.com/profiles/{sid}/"
        resp = requests.get(profile_url, headers=headers, timeout=5.0)
        if resp.status_code == 200:
            html = resp.text

            def parse_max_numeric(matches, fallback="0"):
                if not matches: return fallback
                cleaned = [m.replace(',', '').strip() for m in matches if m.replace(',', '').strip().isdigit()]
                if not cleaned: return matches[0].strip()
                return f"{max([int(x) for x in cleaned]):,}"

            if persona == "Steam User":
                p_m = re.search(r'<span class="actual_persona_name">([^<]+)</span>', html)
                if p_m:
                    persona = p_m.group(1).strip()

            lvl_m = re.search(r'friendPlayerLevelNum">(\d+)</span>', html)
            if lvl_m:
                level = lvl_m.group(1).strip()

            gm = re.findall(r'count_link_label">Games</span>(?:\s*&nbsp;)?\s*<span class="profile_count_link_total">\s*([\d,]+)\s*</span>', html, re.IGNORECASE)
            if not gm:
                gm = re.findall(r'href="[^"]*/games[/?][^"]*".*?<span class="profile_count_link_total">\s*([\d,]+)\s*</span>', html, re.DOTALL | re.IGNORECASE)
            games_count = parse_max_numeric(gm, games_count)

            bdg = re.findall(r'<div class="value">\s*([\d,]+)\s*</div>\s*<div class="label">\s*Total Badges Earned\s*</div>', html, re.IGNORECASE)
            if not bdg:
                bdg = re.findall(r'count_link_label">Badges</span>(?:\s*&nbsp;)?\s*<span class="profile_count_link_total">\s*([\d,]+)\s*</span>', html, re.IGNORECASE)
            badges_count = parse_max_numeric(bdg, badges_count)

            itm = re.findall(r'<div class="value">\s*([\d,]+)\s*</div>\s*<div class="label">\s*Items Owned\s*</div>', html, re.IGNORECASE)
            if not itm:
                itm = re.findall(r'href="[^"]*/inventory[/?][^"]*".*?<span class="profile_count_link_total">\s*([\d,]+)\s*</span>', html, re.DOTALL | re.IGNORECASE)
            items_count = parse_max_numeric(itm, items_count)

            ach = re.findall(r'<div class="value">\s*([\d,]+)\s*</div>\s*<div class="label">\s*Achievements\s*</div>', html, re.IGNORECASE)
            if not ach:
                ach = re.findall(r'([\d,]+)\s*</div>\s*<div class="label">\s*Achievements', html, re.IGNORECASE)
            achievements_count = parse_max_numeric(ach, achievements_count)

            pfg = re.findall(r'<div class="value">\s*([\d,]+)\s*</div>\s*<div class="label">\s*Perfect Games\s*</div>', html, re.IGNORECASE)
            perfect_games_count = parse_max_numeric(pfg, perfect_games_count)

            hrs = re.findall(r'([\d,]+(?:\.\d+)?)\s*hrs?\s*on\s*record', html, re.IGNORECASE)
            hours_count = parse_max_numeric(hrs, hours_count)

            hdr_m = re.search(r'<div class="profile_in_game_header">([^<]+)</div>', html)
            if hdr_m:
                h_txt = hdr_m.group(1).strip()
                if "In-Game" in h_txt:
                    game_m = re.search(r'<div class="profile_in_game_name">([^<]+)</div>', html)
                    online_status = f"Playing {game_m.group(1).strip()}" if game_m else "In-Game"
                elif "Online" in h_txt:
                    online_status = "Online"
                elif "Offline" in h_txt:
                    online_status = "Offline"
    except Exception:
        pass

    return {
        "steam_id": sid,
        "persona": persona,
        "avatar_url": avatar_url,
        "level": level,
        "games": games_count,
        "items": items_count,
        "badges": badges_count,
        "achievements": achievements_count,
        "perfect_games": perfect_games_count,
        "hours": hours_count,
        "status": online_status,
        "last_updated": time.time()
    }


def _steam_stats_worker():
    while True:
        try:
            cfg = load_config()
            if cfg.get("enable_steam_screen", True):
                interval_min = float(cfg.get("steam_cache_minutes", 15))
                sid = cfg.get("steam_id") or None
                res = fetch_steam_profile(sid)
                if res:
                    with _steam_lock:
                        global _cached_steam_stats
                        _cached_steam_stats = res
                    save_steam_cache(res)
                time.sleep(interval_min * 60)
            else:
                time.sleep(30)
        except Exception:
            time.sleep(60)


def start_steam_worker_if_needed(cfg):
    global _steam_worker_started
    if cfg.get("enable_steam_screen", True) and not _steam_worker_started:
        _steam_worker_started = True
        t = threading.Thread(target=_steam_stats_worker, daemon=True)
        t.start()


def get_cached_steam_stats(cfg):
    global _cached_steam_stats
    interval_min = float(cfg.get("steam_cache_minutes", 15))

    with _steam_lock:
        if _cached_steam_stats is not None and str(_cached_steam_stats.get("level", "0")) not in ("0", "") and (time.time() - _cached_steam_stats.get("last_updated", 0)) < (interval_min * 60):
            return _cached_steam_stats

    cached = load_steam_cache()
    if cached and str(cached.get("level", "0")) not in ("0", "") and (time.time() - cached.get("last_updated", 0)) < (interval_min * 60):
        with _steam_lock:
            _cached_steam_stats = cached
        return cached

    sid = cfg.get("steam_id") or None
    fresh = fetch_steam_profile(sid)
    if fresh and str(fresh.get("level", "0")) not in ("0", ""):
        with _steam_lock:
            _cached_steam_stats = fresh
        save_steam_cache(fresh)
        return fresh
    return cached or fresh or None


# GitHub Stats Integration & Caching
_github_worker_started = False
_github_lock = threading.Lock()
_cached_github_stats = None
GITHUB_CACHE_FILE = os.path.join(LOG_DIR, "github_cache.json")


def load_github_cache():
    if os.path.exists(GITHUB_CACHE_FILE):
        try:
            with open(GITHUB_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_github_cache(data):
    try:
        with open(GITHUB_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def fetch_github_stats(username, token=None):
    """
    Fetches the total repository count created by the user.
    Strict Privacy Rule:
    The returned data only contains aggregate metrics (total repository count).
    No repository URLs, repository names, or user profile links are exposed.
    """
    total_repos = None
    headers = {
        "User-Agent": "Protutech-Discord-RPC",
        "Accept": "application/vnd.github.v3+json"
    }

    # If personal access token is provided, fetch authenticated user profile (includes private repos)
    if token:
        try:
            auth_headers = dict(headers)
            auth_headers["Authorization"] = f"Bearer {str(token).strip()}"
            resp = requests.get("https://api.github.com/user", headers=auth_headers, timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                pub = data.get("public_repos", 0)
                priv = data.get("total_private_repos") or data.get("owned_private_repos") or 0
                total_repos = pub + priv
        except Exception:
            pass

    # Fallback to public profile if no token or token query failed
    if total_repos is None and username:
        try:
            user_url = f"https://api.github.com/users/{str(username).strip()}"
            resp = requests.get(user_url, headers=headers, timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                total_repos = data.get("public_repos", 0)
        except Exception:
            pass

    if total_repos is not None:
        return {
            "total_repos": total_repos,
            "last_updated": time.time()
        }
    return None


def _github_stats_worker():
    while True:
        try:
            cfg = load_config()
            if cfg.get("enable_github_screen", True):
                interval_min = float(cfg.get("github_cache_minutes", 30))
                uname = cfg.get("github_username", "IAndrexI")
                tok = cfg.get("github_token") or None
                res = fetch_github_stats(uname, tok)
                if res:
                    with _github_lock:
                        global _cached_github_stats
                        _cached_github_stats = res
                    save_github_cache(res)
                time.sleep(interval_min * 60)
            else:
                time.sleep(30)
        except Exception:
            time.sleep(60)


def start_github_worker_if_needed(cfg):
    global _github_worker_started
    if cfg.get("enable_github_screen", True) and not _github_worker_started:
        _github_worker_started = True
        t = threading.Thread(target=_github_stats_worker, daemon=True)
        t.start()


def get_cached_github_stats(cfg):
    global _cached_github_stats
    with _github_lock:
        if _cached_github_stats is not None:
            return _cached_github_stats

    cached = load_github_cache()
    if cached:
        with _github_lock:
            _cached_github_stats = cached
        return cached

    uname = cfg.get("github_username", "IAndrexI")
    tok = cfg.get("github_token") or None
    fresh = fetch_github_stats(uname, tok)
    if fresh:
        with _github_lock:
            _cached_github_stats = fresh
        save_github_cache(fresh)
        return fresh

    return {"total_repos": 10, "last_updated": time.time()}


# Free Games (Promotional Giveaways) Integration & Caching
_free_games_worker_started = False
_free_games_lock = threading.Lock()
_cached_free_games = None
FREE_GAMES_CACHE_FILE = os.path.join(LOG_DIR, "free_games_cache.json")


def load_free_games_cache():
    if os.path.exists(FREE_GAMES_CACHE_FILE):
        try:
            with open(FREE_GAMES_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_free_games_cache(data):
    try:
        with open(FREE_GAMES_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def fetch_free_games():
    """
    Fetches active 100% free promotional PC games using GamerPower (the #1 free games giveaway tracker)
    and Epic Games Store promotions API, with direct claim links, platform info, and worth metadata.
    """
    games = []
    seen_titles = set()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    # 1. GamerPower Giveaways API (Primary tracker for Steam, Epic, GOG freebies)
    try:
        url = "https://www.gamerpower.com/api/giveaways?type=game&platform=pc"
        resp = requests.get(url, headers=headers, timeout=8.0)
        if resp.status_code == 200:
            for g in resp.json():
                platforms = g.get("platforms", "")
                if any(p in platforms for p in ("Steam", "Epic Games", "GOG")):
                    raw_title = g.get("title", "")
                    clean_t = re.sub(r'\bGiveaway\b', '', raw_title, flags=re.I)
                    clean_t = re.sub(r'\s*\([^)]*\)\s*', ' ', clean_t)
                    clean_t = re.sub(r'\s+', ' ', clean_t).strip()
                    norm = re.sub(r'[^a-z0-9]', '', clean_t.lower())
                    if norm and norm not in seen_titles:
                        seen_titles.add(norm)
                        plat = "Steam" if "Steam" in platforms else ("Epic Games" if "Epic Games" in platforms else "GOG")
                        claim_url = g.get("open_giveaway_url") or g.get("gamerpower_url") or "https://www.gamerpower.com"
                        games.append({
                            "title": clean_t,
                            "platform": plat,
                            "worth": g.get("worth"),
                            "url": claim_url,
                            "thumbnail": g.get("thumbnail"),
                            "gamerpower_url": g.get("gamerpower_url") or "https://www.gamerpower.com"
                        })
    except Exception:
        pass

    # 2. Epic Games Store Official Promotions API (Direct weekly verification)
    try:
        url = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions?locale=en-US&country=US&allowCountries=US"
        resp = requests.get(url, headers=headers, timeout=8.0)
        if resp.status_code == 200:
            elements = resp.json().get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", [])
            for el in elements:
                promos = el.get("promotions")
                if not promos:
                    continue
                offers = promos.get("promotionalOffers")
                if offers and len(offers) > 0:
                    for offer in offers[0].get("promotionalOffers", []):
                        discount = offer.get("discountSetting", {}).get("discountPercentage")
                        if discount == 0:
                            raw_t = el.get("title", "").strip()
                            clean_t = raw_t.replace(" Giveaway", "").strip()
                            norm = re.sub(r'[^a-z0-9]', '', clean_t.lower())
                            if norm and norm not in seen_titles:
                                seen_titles.add(norm)
                                product_slug = el.get("productSlug") or (el.get("catalogNs", {}).get("mappings", [{}])[0].get("pageSlug") if el.get("catalogNs", {}).get("mappings") else None)
                                page_url = f"https://store.epicgames.com/p/{product_slug}" if product_slug else "https://store.epicgames.com/free-games"
                                games.append({
                                    "title": clean_t,
                                    "platform": "Epic Games",
                                    "worth": "Free",
                                    "url": page_url,
                                    "thumbnail": None,
                                    "gamerpower_url": "https://www.gamerpower.com"
                                })
    except Exception:
        pass

    return {
        "games": games,
        "count": len(games),
        "source": "GamerPower",
        "source_url": "https://www.gamerpower.com",
        "last_updated": time.time()
    }


def _free_games_worker():
    while True:
        try:
            cfg = load_config()
            if cfg.get("enable_free_games_screen", True):
                interval_min = float(cfg.get("free_games_cache_minutes", 60))
                res = fetch_free_games()
                if res and res.get("games"):
                    with _free_games_lock:
                        global _cached_free_games
                        _cached_free_games = res
                    save_free_games_cache(res)
                time.sleep(interval_min * 60)
            else:
                time.sleep(30)
        except Exception:
            time.sleep(60)


def start_free_games_worker_if_needed(cfg):
    global _free_games_worker_started
    if cfg.get("enable_free_games_screen", True) and not _free_games_worker_started:
        _free_games_worker_started = True
        t = threading.Thread(target=_free_games_worker, daemon=True)
        t.start()


def get_cached_free_games(cfg):
    global _cached_free_games
    with _free_games_lock:
        if _cached_free_games is not None:
            return _cached_free_games

    cached = load_free_games_cache()
    if cached:
        with _free_games_lock:
            _cached_free_games = cached
        return cached

    fresh = fetch_free_games()
    if fresh and fresh.get("games"):
        with _free_games_lock:
            _cached_free_games = fresh
        save_free_games_cache(fresh)
        return fresh

    return {"games": [], "count": 0, "last_updated": time.time()}


# Market Watch Integration (Recommended Crypto & Stocks)
_market_worker_started = False
_market_lock = threading.Lock()
_cached_market_stats = None
MARKET_CACHE_FILE = os.path.join(LOG_DIR, "market_cache.json")


def load_market_cache():
    if os.path.exists(MARKET_CACHE_FILE):
        try:
            with open(MARKET_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_market_cache(data):
    try:
        with open(MARKET_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def fetch_market_data(cfg=None):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    crypto_symbols = (cfg.get("market_crypto_list") if cfg else None) or ["BTC", "ETH", "SOL"]
    stocks_symbols = (cfg.get("market_stocks_list") if cfg else None) or ["NVDA", "AAPL", "MSFT", "SPY"]

    tickers = []
    for c in crypto_symbols:
        c_clean = str(c).strip().upper()
        if not c_clean.endswith("-USD"):
            tickers.append((c_clean, f"{c_clean}-USD", "crypto"))
        else:
            tickers.append((c_clean.replace("-USD", ""), c_clean, "crypto"))
    for s in stocks_symbols:
        s_clean = str(s).strip().upper()
        tickers.append((s_clean, s_clean, "stock"))

    results = {
        "crypto": {},
        "stocks": {},
        "items": [],
        "last_updated": time.time()
    }

    for label, y_ticker, category in tickers:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{y_ticker}?interval=1d"
            resp = requests.get(url, headers=headers, timeout=4.0)
            if resp.status_code == 200:
                chart_res = resp.json().get("chart", {}).get("result", [])
                if chart_res:
                    meta = chart_res[0].get("meta", {})
                    price = meta.get("regularMarketPrice")
                    prev_close = meta.get("previousClose") or meta.get("chartPreviousClose") or price
                    if price is not None:
                        change_pct = ((price - prev_close) / prev_close) * 100.0 if prev_close else 0.0
                        item_info = {
                            "symbol": label,
                            "ticker": y_ticker,
                            "price": float(price),
                            "change_pct": round(float(change_pct), 2),
                            "category": category
                        }
                        if category == "crypto":
                            results["crypto"][label] = item_info
                        else:
                            results["stocks"][label] = item_info
                        results["items"].append(item_info)
        except Exception:
            pass

    return {
        "crypto": list(results["crypto"].values()),
        "stocks": list(results["stocks"].values()),
        "crypto_dict": results["crypto"],
        "stocks_dict": results["stocks"],
        "items": results["items"],
        "last_updated": time.time()
    }


def _market_stats_worker():
    while True:
        try:
            cfg = load_config()
            if cfg.get("enable_market_screen", True):
                interval_min = float(cfg.get("market_cache_minutes", 5))
                res = fetch_market_data(cfg)
                if res and res.get("items"):
                    with _market_lock:
                        global _cached_market_stats
                        _cached_market_stats = res
                    save_market_cache(res)
                time.sleep(interval_min * 60)
            else:
                time.sleep(30)
        except Exception:
            time.sleep(60)


def start_market_worker_if_needed(cfg):
    global _market_worker_started
    if cfg.get("enable_market_screen", True) and not _market_worker_started:
        _market_worker_started = True
        t = threading.Thread(target=_market_stats_worker, daemon=True)
        t.start()


def get_cached_market_stats(cfg):
    global _cached_market_stats
    with _market_lock:
        if _cached_market_stats is not None:
            return _cached_market_stats

    cached = load_market_cache()
    if cached:
        with _market_lock:
            _cached_market_stats = cached
        return cached

    fresh = fetch_market_data(cfg)
    if fresh and fresh.get("items"):
        with _market_lock:
            _cached_market_stats = fresh
        save_market_cache(fresh)
        return fresh

    return {"crypto": {}, "stocks": {}, "items": [], "last_updated": time.time()}


# Minecraft Self-Hosted Server Status Integration
_mc_server_worker_started = False
_mc_server_lock = threading.Lock()
_cached_mc_server_status = None
MC_SERVER_CACHE_FILE = os.path.join(LOG_DIR, "mc_server_cache.json")


def load_mc_server_cache():
    if os.path.exists(MC_SERVER_CACHE_FILE):
        try:
            with open(MC_SERVER_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_mc_server_cache(data):
    try:
        with open(MC_SERVER_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def fetch_minecraft_server_status(cfg=None):
    if cfg is None:
        cfg = load_config()
    server_addr = str(cfg.get("minecraft_server_address", "minecraft.protutech.vip")).strip()
    server_port = int(cfg.get("minecraft_server_port", 25565))
    server_label = str(cfg.get("minecraft_server_label", "Protutech Server")).strip()

    result = {
        "online": False,
        "hostname": server_addr,
        "port": server_port,
        "label": server_label,
        "players_online": 0,
        "players_max": 20,
        "version": "1.20+",
        "motd": f"{server_label} • Offline",
        "icon": None,
        "last_checked": time.time()
    }

    # 1. Query mcsrvstat.us
    try:
        api_url = f"https://api.mcsrvstat.us/3/{server_addr}:{server_port}"
        resp = requests.get(api_url, timeout=4.0)
        if resp.status_code == 200:
            data = resp.json()
            is_online = bool(data.get("online", False))
            result["online"] = is_online
            if is_online:
                p_info = data.get("players", {})
                result["players_online"] = int(p_info.get("online", 0))
                result["players_max"] = int(p_info.get("max", 20))
                result["version"] = str(data.get("version", "1.20+"))
                motd_obj = data.get("motd", {})
                if isinstance(motd_obj, dict):
                    clean_motd = motd_obj.get("clean", [])
                    result["motd"] = " ".join(clean_motd).strip() if clean_motd else f"{server_label} SMP"
                elif isinstance(motd_obj, list):
                    result["motd"] = " ".join(motd_obj).strip()
                result["icon"] = data.get("icon")
                return result
    except Exception:
        pass

    # 2. Fallback direct socket ping
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect((server_addr, server_port))
        s.close()
        result["online"] = True
        result["motd"] = f"{server_label} Online"
    except Exception:
        result["online"] = False

    return result


def _mc_server_stats_worker():
    while True:
        try:
            cfg = load_config()
            if cfg.get("enable_minecraft_server_screen", True):
                res = fetch_minecraft_server_status(cfg)
                if res:
                    with _mc_server_lock:
                        global _cached_mc_server_status
                        _cached_mc_server_status = res
                    save_mc_server_cache(res)
                time.sleep(60)
            else:
                time.sleep(30)
        except Exception:
            time.sleep(60)


def start_mc_server_worker_if_needed(cfg):
    global _mc_server_worker_started
    if cfg.get("enable_minecraft_server_screen", True) and not _mc_server_worker_started:
        _mc_server_worker_started = True
        t = threading.Thread(target=_mc_server_stats_worker, daemon=True)
        t.start()


def get_cached_mc_server_status(cfg):
    global _cached_mc_server_status
    with _mc_server_lock:
        if _cached_mc_server_status is not None:
            return _cached_mc_server_status

    cached = load_mc_server_cache()
    if cached:
        with _mc_server_lock:
            _cached_mc_server_status = cached
        return cached

    fresh = fetch_minecraft_server_status(cfg)
    if fresh:
        with _mc_server_lock:
            _cached_mc_server_status = fresh
        save_mc_server_cache(fresh)
        return fresh

    return {"online": False, "hostname": "minecraft.protutech.vip", "players_online": 0, "players_max": 20, "last_checked": time.time()}


# Stoat Chat Presence Sync Helper
def sync_stoat_status(current_screen, cfg):
    """
    Syncs current active Discord RPC screen details and state to Stoat / Revolt Chat.
    Uses PATCH https://api.stoat.chat/users/@me (or configured stoat_api_url)
    """
    if not cfg.get("enable_stoat_sync", False):
        return
    token = str(cfg.get("stoat_token", "")).strip()
    if not token:
        return

    api_url = str(cfg.get("stoat_api_url", "https://api.stoat.chat")).rstrip("/")
    token_type = str(cfg.get("stoat_token_type", "user")).lower()
    headers = {
        "Content-Type": "application/json"
    }
    if token_type == "bot":
        headers["x-bot-token"] = token
    else:
        headers["X-Session-Token"] = token

    status_text = f"{current_screen.get('details', '')} | {current_screen.get('state', '')}"
    if len(status_text) > 120:
        status_text = status_text[:117] + "..."

    payload = {
        "status": {
            "text": status_text,
            "presence": cfg.get("stoat_presence", "Online")
        }
    }

    def _do_patch():
        try:
            requests.patch(f"{api_url}/users/@me", headers=headers, json=payload, timeout=4.0)
        except Exception:
            pass

    threading.Thread(target=_do_patch, daemon=True).start()



# Web Dashboard Server Integration
_dashboard_state = {
    "user_id": "andrex",
    "user_name": "Andrex",
    "user_avatar": "",
    "screens": [],
    "current_screen_name": "",
    "screen_index": 0,
    "last_updated": 0
}
_users_store = {}
_dashboard_lock = threading.Lock()
_dashboard_server_started = False
_custom_trackers_cache = {}
DASHBOARD_HTML_PATH = os.path.join(LOG_DIR, "dashboard.html")


class DashboardRequestHandler(http.server.BaseHTTPRequestHandler):
    def is_request_host(self):
        try:
            cfg = load_config()
            configured_key = str(cfg.get("host_key", "andrex-host-2026")).strip()

            req_key = self.headers.get("X-Host-Key", "").strip()
            if not req_key:
                auth_hdr = self.headers.get("Authorization", "").strip()
                if auth_hdr.startswith("Bearer "):
                    req_key = auth_hdr[7:].strip()

            url_parts = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(url_parts.query)
            if not req_key:
                req_key = q.get("key", [""])[0] or q.get("host_key", [""])[0]

            if req_key and configured_key and req_key == configured_key:
                return True
        except Exception:
            pass
        return False

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Host-Key")
        self.end_headers()

    def do_GET(self):
        url_parts = urllib.parse.urlparse(self.path)
        parsed_path = url_parts.path
        query_params = urllib.parse.parse_qs(url_parts.query)

        if parsed_path in ("/", "/index.html", "/dashboard.html", "/discordrpc", "/discordrpc.html"):
            content = b""
            if os.path.exists(DASHBOARD_HTML_PATH):
                try:
                    with open(DASHBOARD_HTML_PATH, "rb") as f:
                        content = f.read()
                except Exception:
                    pass
            if not content:
                content = b"<!DOCTYPE html><html><body><h1>DiscordRPC</h1><p>DiscordRPC HTML not found.</p></body></html>"

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        elif parsed_path == "/api/auth/verify":
            is_host = self.is_request_host()
            payload = json.dumps({"is_host": is_host}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        elif parsed_path == "/api/config":
            if not self.is_request_host():
                self.send_response(403)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b'{"error": "Forbidden: Main host access required"}')
                return

            cfg = load_config()
            safe_cfg = {
                "update_interval_seconds": cfg.get("update_interval_seconds", 6),
                "active_screen": cfg.get("active_screen", "rotate"),
                "enable_proxmox_screen": cfg.get("enable_proxmox_screen", True),
                "enable_kryptex_screen": cfg.get("enable_kryptex_screen", True),
                "enable_minecraft_screen": cfg.get("enable_minecraft_screen", True),
                "minecraft_only_when_playing": cfg.get("minecraft_only_when_playing", False),
                "enable_minecraft_server_screen": cfg.get("enable_minecraft_server_screen", True),
                "minecraft_server_address": cfg.get("minecraft_server_address", "minecraft.protutech.vip"),
                "minecraft_server_port": cfg.get("minecraft_server_port", 25565),
                "minecraft_server_label": cfg.get("minecraft_server_label", "Protutech Server"),
                "enable_market_screen": cfg.get("enable_market_screen", True),
                "market_crypto_list": cfg.get("market_crypto_list", ["BTC", "ETH", "SOL"]),
                "market_stocks_list": cfg.get("market_stocks_list", ["NVDA", "AAPL", "MSFT", "SPY"]),
                "market_cache_minutes": cfg.get("market_cache_minutes", 5),
                "enable_stoat_sync": cfg.get("enable_stoat_sync", False),
                "stoat_api_url": cfg.get("stoat_api_url", "https://api.stoat.chat"),
                "stoat_token": cfg.get("stoat_token", ""),
                "stoat_token_type": cfg.get("stoat_token_type", "user"),
                "stoat_presence": cfg.get("stoat_presence", "Online"),
                "enable_speed_screen": cfg.get("enable_speed_screen", True),
                "enable_steam_screen": cfg.get("enable_steam_screen", True),
                "enable_github_screen": cfg.get("enable_github_screen", True),
                "enable_free_games_screen": cfg.get("enable_free_games_screen", True),
                "enable_game_activity": cfg.get("enable_game_activity", False),
                "enable_active_games_hub": cfg.get("enable_active_games_hub", True),
                "disabled_games": cfg.get("disabled_games", []),
                "custom_games": cfg.get("custom_games", {}),
                "speedtest_interval_minutes": cfg.get("speedtest_interval_minutes", 30),
                "steam_cache_minutes": cfg.get("steam_cache_minutes", 15),
                "github_cache_minutes": cfg.get("github_cache_minutes", 30),
                "free_games_cache_minutes": cfg.get("free_games_cache_minutes", 60),
                "hidden_screens": cfg.get("hidden_screens", []),
                "custom_trackers": cfg.get("custom_trackers", []),
                "host_key": cfg.get("host_key", "andrex-host-2026"),
                "is_host": True
            }
            payload = json.dumps(safe_cfg).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        elif parsed_path in ("/api/users", "/api/members"):
            with _dashboard_lock:
                users_list = []
                now = time.time()
                local_uid = _dashboard_state.get("user_id", "andrex")
                if local_uid and local_uid not in _users_store and _dashboard_state.get("screens"):
                    _users_store[local_uid] = dict(_dashboard_state)

                USERS_FILE = os.path.join(LOG_DIR, "users.json")
                if os.path.exists(USERS_FILE):
                    try:
                        with open(USERS_FILE, "r", encoding="utf-8") as f:
                            extra_users = json.load(f)
                            for e_uid, e_val in extra_users.items():
                                if e_uid not in _users_store:
                                    _users_store[e_uid] = e_val
                    except Exception:
                        pass

                for uid, udata in _users_store.items():
                    last_seen = udata.get("last_updated", 0)
                    is_online = (now - last_seen) < 120
                    users_list.append({
                        "id": uid,
                        "name": udata.get("user_name", uid),
                        "avatar": udata.get("user_avatar") or DEFAULT_PROXMOX_ICON,
                        "online": is_online,
                        "current_screen": udata.get("current_screen_name", ""),
                        "total_screens": len(udata.get("screens", [])),
                        "last_updated": last_seen
                    })
                payload = json.dumps({"users": users_list, "default_user": local_uid}).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        elif parsed_path in ("/api/stats", "/api/screens"):
            requested_user = query_params.get("user", [None])[0]
            is_host = self.is_request_host()
            with _dashboard_lock:
                if requested_user and requested_user.lower() not in _users_store:
                    USERS_FILE = os.path.join(LOG_DIR, "users.json")
                    if os.path.exists(USERS_FILE):
                        try:
                            with open(USERS_FILE, "r", encoding="utf-8") as f:
                                extra_users = json.load(f)
                                for e_uid, e_val in extra_users.items():
                                    _users_store[e_uid.lower()] = e_val
                        except Exception:
                            pass

                if requested_user and requested_user.lower() in _users_store:
                    target_data = dict(_users_store[requested_user.lower()])
                else:
                    target_data = dict(_dashboard_state)

                if "screens" in target_data:
                    cfg_now = load_config()
                    hidden_set = set(cfg_now.get("hidden_screens", []))
                    if not is_host:
                        target_data["screens"] = [s for s in target_data["screens"] if s.get("name") not in hidden_set]

                target_data["is_host"] = is_host
                payload = json.dumps(target_data).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        elif parsed_path == "/api/minecraft/icon":
            mc_status = fetch_minecraft_status()
            loc_path = mc_status.get("local_icon_path") if mc_status else None
            if loc_path and os.path.isfile(loc_path):
                try:
                    ctype = "image/png"
                    if loc_path.lower().endswith(".webp"):
                        ctype = "image/webp"
                    elif loc_path.lower().endswith((".jpg", ".jpeg")):
                        ctype = "image/jpeg"
                    elif loc_path.lower().endswith(".gif"):
                        ctype = "image/gif"
                    with open(loc_path, "rb") as f:
                        img_data = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(img_data)))
                    self.send_header("Cache-Control", "public, max-age=300")
                    self.end_headers()
                    self.wfile.write(img_data)
                    return
                except Exception:
                    pass
            self.send_response(404)
            self.end_headers()

        elif parsed_path == "/api/games":
            cfg = load_config()
            detected, _ = detect_active_games(cfg, max_games=10, return_pids=True)
            disabled_games = cfg.get("disabled_games", [])
            custom_games = cfg.get("custom_games", {})

            known_list = []
            for exe_name, g_info in KNOWN_GAMES.items():
                if isinstance(g_info, tuple):
                    name, slug = g_info
                else:
                    name = g_info
                    slug = re.sub(r'[^a-z0-9_]', '', name.lower().replace(" ", "_"))

                is_running = any(d.get("slug") == slug or d.get("name") == name for d in detected)
                is_disabled = (slug.lower() in [x.lower() for x in disabled_games]) or (name.lower() in [x.lower() for x in disabled_games])
                known_list.append({
                    "name": name,
                    "slug": slug,
                    "exe": exe_name,
                    "running": is_running,
                    "disabled": is_disabled
                })

            for c_exe, c_val in custom_games.items():
                c_name = c_val.get("name", c_exe) if isinstance(c_val, dict) else str(c_val)
                c_slug = c_val.get("slug") if isinstance(c_val, dict) else re.sub(r'[^a-z0-9_]', '', c_name.lower().replace(" ", "_"))
                is_running = any(d.get("slug") == c_slug or d.get("name") == c_name for d in detected)
                is_disabled = (c_slug.lower() in [x.lower() for x in disabled_games]) or (c_name.lower() in [x.lower() for x in disabled_games])
                known_list.append({
                    "name": c_name,
                    "slug": c_slug,
                    "exe": c_exe,
                    "running": is_running,
                    "disabled": is_disabled,
                    "custom": True
                })

            payload = json.dumps({
                "detected": detected,
                "known": known_list,
                "disabled_games": disabled_games
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        elif parsed_path == "/api/market":
            cfg = load_config()
            market_data = get_cached_market_stats(cfg)
            payload = json.dumps(market_data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        elif parsed_path == "/api/minecraft/server":
            cfg = load_config()
            mc_srv = get_cached_mc_server_status(cfg)
            payload = json.dumps(mc_srv).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed_path = self.path.split("?")[0]
        if parsed_path == "/api/auth/login":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body.decode("utf-8")) if body else {}
                key = str(data.get("key") or data.get("host_key") or "").strip()
                cfg = load_config()
                configured_key = str(cfg.get("host_key", "andrex-host-2026")).strip()

                if key and configured_key and key == configured_key:
                    payload = json.dumps({"is_host": True, "token": configured_key, "message": "Host login successful"}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                else:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(b'{"error": "Invalid host key", "is_host": false}')
                    return
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                return

        elif parsed_path == "/api/config":
            if not self.is_request_host():
                self.send_response(403)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b'{"error": "Forbidden: Main host access required"}')
                return

            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                payload = json.loads(body.decode("utf-8"))

                cfg = load_config()
                allowed_keys = [
                    "update_interval_seconds", "active_screen",
                    "enable_proxmox_screen", "enable_kryptex_screen",
                    "enable_minecraft_screen", "minecraft_only_when_playing",
                    "enable_minecraft_server_screen", "minecraft_server_address",
                    "minecraft_server_port", "minecraft_server_label",
                    "enable_market_screen", "market_crypto_list", "market_stocks_list", "market_cache_minutes",
                    "enable_stoat_sync", "stoat_api_url", "stoat_token", "stoat_token_type", "stoat_presence",
                    "enable_speed_screen", "enable_steam_screen", "enable_github_screen",
                    "enable_free_games_screen", "enable_game_activity", "enable_active_games_hub",
                    "speedtest_interval_minutes", "steam_cache_minutes",
                    "github_cache_minutes", "free_games_cache_minutes",
                    "hidden_screens", "custom_trackers", "disabled_games", "custom_games", "host_key"
                ]

                for k in allowed_keys:
                    if k in payload:
                        cfg[k] = payload[k]

                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok", "message": "Configuration saved", "config": cfg}).encode("utf-8"))
                return
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                return

        elif parsed_path == "/api/games/toggle":
            if not self.is_request_host():
                self.send_response(403)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b'{"error": "Forbidden: Main host access required"}')
                return

            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body.decode("utf-8"))
                slug = str(data.get("slug") or data.get("name") or "").strip().lower()
                disabled = bool(data.get("disabled", True))

                cfg = load_config()
                disabled_list = list(cfg.get("disabled_games", []))

                if disabled:
                    if slug and slug not in [x.lower() for x in disabled_list]:
                        disabled_list.append(slug)
                else:
                    disabled_list = [x for x in disabled_list if x.lower() != slug]

                cfg["disabled_games"] = disabled_list
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok", "disabled_games": disabled_list}).encode("utf-8"))
                return
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                return

        elif parsed_path == "/api/games/add":
            if not self.is_request_host():
                self.send_response(403)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b'{"error": "Forbidden: Main host access required"}')
                return

            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body.decode("utf-8"))
                name = str(data.get("name", "")).strip()
                exe = str(data.get("exe", "")).strip().lower()
                if not exe.endswith(".exe"):
                    exe += ".exe"

                cfg = load_config()
                custom_games = cfg.get("custom_games", {})
                custom_games[exe] = {
                    "name": name or exe.replace(".exe", "").title(),
                    "slug": re.sub(r'[^a-z0-9_]', '', (name or exe).lower().replace(" ", "_"))
                }
                cfg["custom_games"] = custom_games
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok", "custom_games": custom_games}).encode("utf-8"))
                return
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                return

        elif parsed_path == "/api/trackers/add":
            if not self.is_request_host():
                self.send_response(403)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b'{"error": "Forbidden: Main host access required"}')
                return

            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                new_tracker = json.loads(body.decode("utf-8"))
                cfg = load_config()
                current_trackers = list(cfg.get("custom_trackers", []))
                current_trackers.append(new_tracker)
                cfg["custom_trackers"] = current_trackers
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok", "custom_trackers": current_trackers}).encode("utf-8"))
                return
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                return

        elif parsed_path == "/api/trackers/delete":
            if not self.is_request_host():
                self.send_response(403)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b'{"error": "Forbidden: Main host access required"}')
                return

            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body.decode("utf-8"))
                tracker_id = str(data.get("id") or data.get("name") or "").strip()

                cfg = load_config()
                current_trackers = [ct for ct in cfg.get("custom_trackers", []) if ct.get("id") != tracker_id and ct.get("name") != tracker_id]
                cfg["custom_trackers"] = current_trackers
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok", "custom_trackers": current_trackers}).encode("utf-8"))
                return
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                return

        elif parsed_path == "/api/push":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body.decode("utf-8"))
                uid = str(data.get("user_id", "")).strip().lower()
                if uid:
                    with _dashboard_lock:
                        _users_store[uid] = {
                            "user_id": uid,
                            "user_name": data.get("user_name", uid),
                            "user_avatar": data.get("user_avatar") or DEFAULT_PROXMOX_ICON,
                            "screens": data.get("screens", []),
                            "current_screen_name": data.get("current_screen_name", ""),
                            "screen_index": data.get("screen_index", 0),
                            "total_screens": len(data.get("screens", [])),
                            "last_updated": time.time()
                        }
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(b'{"status": "ok"}')
                    return
            except Exception:
                pass
            self.send_response(400)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress noisy HTTP access logs
        pass


def _dashboard_server_worker(port=8989):
    try:
        socketserver.TCPServer.allow_reuse_address = True
        server = socketserver.TCPServer(("0.0.0.0", port), DashboardRequestHandler)
        server.serve_forever()
    except Exception as e:
        print(f"[WARN] Dashboard server error on port {port}: {e}", flush=True)


def start_dashboard_server_if_needed(cfg):
    global _dashboard_server_started
    if not _dashboard_server_started:
        port = int(cfg.get("dashboard_port", 8989))
        _dashboard_server_started = True
        t = threading.Thread(target=_dashboard_server_worker, args=(port,), daemon=True)
        t.start()
        print(f"[INFO] Web Dashboard server running at http://localhost:{port}", flush=True)


CLOUDFLARED_BIN = os.path.join(LOG_DIR, "bin", "cloudflared.exe")
_cloudflared_proc = None
_cloudflared_started = False

def start_cloudflare_tunnel_if_needed(cfg):
    global _cloudflared_proc, _cloudflared_started
    if not cfg.get("enable_cloudflare_tunnel", False):
        return

    if _cloudflared_started:
        return

    token = cfg.get("cloudflare_tunnel_token", "").strip()
    port = int(cfg.get("dashboard_port", 8989))

    bin_path = CLOUDFLARED_BIN if os.path.exists(CLOUDFLARED_BIN) else (shutil.which("cloudflared") or CLOUDFLARED_BIN)
    if not os.path.exists(bin_path):
        print(f"[WARN] Cloudflare tunnel binary not found at {bin_path}", flush=True)
        return

    try:
        domain = cfg.get("dashboard_domain", "custom domain")
        if token:
            cmd = [bin_path, "tunnel", "run", "--token", token]
            print(f"[INFO] Connecting Cloudflare Zero Trust Tunnel for {domain}...", flush=True)
        else:
            cmd = [bin_path, "tunnel", "--url", f"http://127.0.0.1:{port}"]
            print(f"[INFO] Starting Cloudflare Quick Tunnel on port {port}...", flush=True)

        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW

        _cloudflared_started = True
        _cloudflared_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags
        )
        print(f"[INFO] Cloudflare Tunnel running in background (PID: {_cloudflared_proc.pid})", flush=True)
    except Exception as e:
        print(f"[WARN] Failed to start Cloudflare Tunnel: {e}", flush=True)


def _cleanup_cloudflared():
    global _cloudflared_proc
    if _cloudflared_proc and _cloudflared_proc.poll() is None:
        try:
            _cloudflared_proc.terminate()
        except Exception:
            pass

import atexit
atexit.register(_cleanup_cloudflared)


def patch_discord_game_utils():
    """
    Ensures Discord's native game identification module (discord_game_utils)
    does not report launcher/background apps like CurseForge or Overwolf as games,
    persisting across Discord auto-updates.
    """
    try:
        local_app = os.environ.get("LOCALAPPDATA", "")
        if not local_app:
            return
        discord_dir = os.path.join(local_app, "Discord")
        if not os.path.exists(discord_dir):
            return
        import glob
        pattern = os.path.join(discord_dir, "app-*", "modules", "discord_game_utils-*", "discord_game_utils", "index.js")
        patch_code = '''"use strict";
const native = require('./discord_game_utils.node');

const BLOCKED_NAMES = [
  'curseforge',
  'curse.agent.host',
  'overwolf'
];

const originalIdentifyGame = native.identifyGame;
if (typeof originalIdentifyGame === 'function') {
  native.identifyGame = function(pid, callback) {
    return originalIdentifyGame.call(native, pid, (err, res) => {
      try {
        if (!err && res) {
          const name = (res.name || '').toLowerCase();
          const exe = (res.executableName || '').toLowerCase();
          const pub = (res.publisher || '').toLowerCase();
          if (BLOCKED_NAMES.some(b => name.includes(b) || exe.includes(b) || pub.includes(b))) {
            return callback(3, {
              name: '',
              executableName: res.executableName || '',
              distributor: '',
              sku: '',
              publisher: '',
              iconHash: '',
              icon: ''
            });
          }
        }
      } catch (e) {}
      return callback(err, res);
    });
  };
}

module.exports = native;
'''
        for idx_file in glob.glob(pattern):
            try:
                with open(idx_file, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                if "BLOCKED_NAMES" not in content:
                    bak_file = idx_file + ".bak"
                    if not os.path.exists(bak_file):
                        try:
                            with open(bak_file, "w", encoding="utf-8") as f:
                                f.write(content)
                        except Exception:
                            pass
                    with open(idx_file, "w", encoding="utf-8") as f:
                        f.write(patch_code)
                    print(f"[INFO] Auto-patched Discord game detector: {idx_file}", flush=True)

                # Ensure package.json points to index.js so the wrapper is loaded
                pkg_file = os.path.join(os.path.dirname(idx_file), "package.json")
                if os.path.exists(pkg_file):
                    try:
                        with open(pkg_file, "r", encoding="utf-8") as f:
                            pkg = json.load(f)
                        if pkg.get("main") != "index.js":
                            pkg["main"] = "index.js"
                            with open(pkg_file, "w", encoding="utf-8") as f:
                                json.dump(pkg, f, indent=2)
                    except Exception:
                        pass
            except Exception:
                pass

        # Also patch discord_utils index.js candidate games & game detection callbacks
        pattern_du = os.path.join(discord_dir, "app-*", "modules", "discord_utils-*", "discord_utils", "index.js")
        patch_du_code = (
            "\nconst BLOCKED_GAME_NAMES = ['curseforge', 'curse.agent.host', 'overwolf', 'curse'];\n"
            "const isBlockedGame = (g) => {\n"
            "    if (!g) return false;\n"
            "    const s = `${g.name || ''} ${g.exePath || ''} ${g.processName || ''} ${g.executableName || ''} ${g.cmdLine || ''}`.toLowerCase();\n"
            "    return BLOCKED_GAME_NAMES.some(b => s.includes(b));\n"
            "};\n"
            "for (const fn of ['setCandidateGamesCallback', 'setGameDetectionCallback']) {\n"
            "    const orig = nativeUtils[fn];\n"
            "    if (typeof orig === 'function') {\n"
            "        nativeUtils[fn] = function(cb) {\n"
            "            if (typeof cb !== 'function') return orig.apply(nativeUtils, arguments);\n"
            "            return orig.call(nativeUtils, function(games) {\n"
            "                try {\n"
            "                    if (Array.isArray(games)) {\n"
            "                        games = games.filter(g => !isBlockedGame(g));\n"
            "                    }\n"
            "                } catch(e) {}\n"
            "                return cb(games);\n"
            "            });\n"
            "        };\n"
            "    }\n"
            "}\n"
        )
        for du_file in glob.glob(pattern_du):
            try:
                with open(du_file, "r", encoding="utf-8", errors="ignore") as f:
                    du_content = f.read()
                if "BLOCKED_GAME_NAMES" not in du_content:
                    needle = "nativeUtils.clearCandidateGamesCallback = nativeUtils.setCandidateGamesCallback;"
                    if needle in du_content:
                        du_content = du_content.replace(needle, needle + patch_du_code, 1)
                        with open(du_file, "w", encoding="utf-8") as f:
                            f.write(du_content)
                        print(f"[INFO] Auto-patched discord_utils detector: {du_file}", flush=True)
            except Exception:
                pass
    except Exception:
        pass

    # 1. Maintain Discord app.asar native hook & localStorage sanitizer
    try:
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            disc_dir = os.path.join(local_app, "Discord")
            if os.path.isdir(disc_dir):
                for app_entry in glob.glob(os.path.join(disc_dir, "app-*")):
                    asar_idx = os.path.join(app_entry, "resources", "app.asar", "index.js")
                    if os.path.isfile(asar_idx):
                        try:
                            with open(asar_idx, "r", encoding="utf-8", errors="ignore") as f:
                                idx_content = f.read()
                            if "AntiCurse" not in idx_content:
                                # Strip any previous partial hook
                                if "originalDlopen" in idx_content and "require(" in idx_content:
                                    tail_idx = idx_content.rfind("require(")
                                    if tail_idx != -1:
                                        idx_content = idx_content[tail_idx:]

                                hook_code = (
                                    "// Suppress CurseForge and Overwolf from Discord native game identification & local storage\n"
                                    "try {\n"
                                    "  const originalDlopen = process.dlopen;\n"
                                    "  const BLOCKED = ['curseforge', 'curse.agent.host', 'overwolf'];\n"
                                    "  const isBlocked = (s) => s && BLOCKED.some(b => String(s).toLowerCase().includes(b));\n"
                                    "  process.dlopen = function(mod, filename, flags) {\n"
                                    "    const res = originalDlopen.apply(this, arguments);\n"
                                    "    try {\n"
                                    "      if (filename) {\n"
                                    "        if (filename.includes('discord_game_utils')) {\n"
                                    "          const origIdentify = mod.exports.identifyGame;\n"
                                    "          if (typeof origIdentify === 'function') {\n"
                                    "            mod.exports.identifyGame = function(pid, callback) {\n"
                                    "              return origIdentify.call(mod.exports, pid, (err, data) => {\n"
                                    "                try {\n"
                                    "                  if (!err && data) {\n"
                                    "                    if (isBlocked(data.name) || isBlocked(data.executableName) || isBlocked(data.publisher)) {\n"
                                    "                      return callback(3, { name: '', executableName: data.executableName || '' });\n"
                                    "                    }\n"
                                    "                  }\n"
                                    "                } catch(e) {}\n"
                                    "                return callback(err, data);\n"
                                    "              });\n"
                                    "            };\n"
                                    "          }\n"
                                    "        }\n"
                                    "        if (filename.includes('discord_utils')) {\n"
                                    "          for (const fn of ['setCandidateGamesCallback', 'setGameDetectionCallback']) {\n"
                                    "            const origFn = mod.exports[fn];\n"
                                    "            if (typeof origFn === 'function') {\n"
                                    "              mod.exports[fn] = function(cb) {\n"
                                    "                if (typeof cb !== 'function') return origFn.apply(mod.exports, arguments);\n"
                                    "                const wrappedCb = function(games) {\n"
                                    "                  try {\n"
                                    "                    if (Array.isArray(games)) {\n"
                                    "                      games = games.filter(g => !isBlocked(g && (g.name || g.exePath || g.processName || g.executableName)));\n"
                                    "                    }\n"
                                    "                  } catch(e) {}\n"
                                    "                  return cb(games);\n"
                                    "                };\n"
                                    "                return origFn.call(mod.exports, wrappedCb);\n"
                                    "              };\n"
                                    "            }\n"
                                    "          }\n"
                                    "        }\n"
                                    "      }\n"
                                    "    } catch(e) {}\n"
                                    "    return res;\n"
                                    "  };\n"
                                    "  // AntiCurse: Renderer-level localStorage cleanup of CurseForge overrides\n"
                                    "  const electron = require('electron');\n"
                                    "  const app = electron && electron.app;\n"
                                    "  if (app) {\n"
                                    "    const cleanScript = `\n"
                                    "      try {\n"
                                    "        const raw = localStorage.getItem('RunningGameStore');\n"
                                    "        if (raw) {\n"
                                    "          const s = JSON.parse(raw);\n"
                                    "          let chg = false;\n"
                                    "          const isB = (x) => x && (String(x).toLowerCase().includes('curse') || String(x).toLowerCase().includes('overwolf'));\n"
                                    "          for (const target of [s, s._state]) {\n"
                                    "            if (!target) continue;\n"
                                    "            for (const mapName of ['gameOverrides', 'enableDetection', 'enableOverlay', 'enableOverlayV3']) {\n"
                                    "              if (target[mapName]) {\n"
                                    "                for (const k of Object.keys(target[mapName])) {\n"
                                    "                  if (isB(k)) { delete target[mapName][k]; chg = true; }\n"
                                    "                }\n"
                                    "              }\n"
                                    "            }\n"
                                    "            if (Array.isArray(target.gamesSeen)) {\n"
                                    "              const origLen = target.gamesSeen.length;\n"
                                    "              target.gamesSeen = target.gamesSeen.filter(g => !isB(g && (g.name || g.exePath)));\n"
                                    "              if (target.gamesSeen.length !== origLen) chg = true;\n"
                                    "            }\n"
                                    "          }\n"
                                    "          if (chg) { localStorage.setItem('RunningGameStore', JSON.stringify(s)); }\n"
                                    "        }\n"
                                    "      } catch(e) {}\n"
                                    "    `;\n"
                                    "    app.on('browser-window-created', (evt, win) => {\n"
                                    "      try {\n"
                                    "        if (win && win.webContents) {\n"
                                    "          win.webContents.on('dom-ready', () => {\n"
                                    "            try { win.webContents.executeJavaScript(cleanScript).catch(() => {}); } catch(e) {}\n"
                                    "          });\n"
                                    "        }\n"
                                    "      } catch(e) {}\n"
                                    "    });\n"
                                    "  }\n"
                                    "} catch(e) {}\n\n"
                                )
                                with open(asar_idx, "w", encoding="utf-8") as f:
                                    f.write(hook_code + idx_content)
                                print(f"[INFO] Installed native CurseForge suppression hook in {asar_idx}", flush=True)
                        except Exception:
                            pass
    except Exception:
        pass

    # 2. Ensure Equicord IgnoreActivities plugin has CurseForge blocked in Blacklist mode
    try:
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            equi_settings = os.path.join(appdata, "Equicord", "settings", "settings.json")
            if os.path.exists(equi_settings):
                with open(equi_settings, "r", encoding="utf-8") as f:
                    eq_data = json.load(f)
                if "plugins" not in eq_data:
                    eq_data["plugins"] = {}
                curse_entries = [
                    {"id": "1272208350544924835", "name": "Star Technology", "type": 0},
                    {"id": "1402418491272986635", "name": "Minecraft", "type": 0},
                    {"id": "c:/users/andre/curseforge/minecraft/install/java/java-runtime-delta/bin/javaw.exe", "name": "Minecraft", "type": 0},
                    {"id": "c:/users/andre/curseforge/minecraft/install/java/java-runtime-delta/bin/javaw.exe:Minecraft", "name": "Minecraft", "type": 0},
                    {"id": "javaw.exe", "name": "Minecraft", "type": 0},
                    {"id": "Minecraft", "name": "Minecraft", "type": 0},
                    {"id": "Star Technology", "name": "Star Technology", "type": 0},
                    {"id": "c:/users/andre/appdata/local/programs/curseforge windows/curseforge.exe", "name": "CurseForge", "type": 0},
                    {"id": "c:/users/andre/appdata/local/programs/curseforge windows/curseforge.exe:CurseForge", "name": "CurseForge", "type": 0},
                    {"id": "CurseForge", "name": "CurseForge", "type": 0},
                    {"id": "curseforge", "name": "CurseForge", "type": 0},
                    {"id": "curseforge.exe", "name": "CurseForge", "type": 0},
                    {"id": "CurseForge 1.321.2-40115", "name": "CurseForge", "type": 0},
                    {"id": "curse.agent.host.exe", "name": "Curse.Agent.Host", "type": 0},
                    {"id": "overwolf.exe", "name": "Overwolf", "type": 0},
                    {"id": "overwolf", "name": "Overwolf", "type": 0}
                ]
                ia = eq_data["plugins"].get("IgnoreActivities", {})
                needs_update = False
                if not ia.get("enabled"):
                    ia["enabled"] = True
                    needs_update = True
                if ia.get("listMode") != 1:
                    ia["listMode"] = 1  # Blacklist filter mode
                    needs_update = True
                
                existing_ids = {e.get("id") for e in ia.get("ignoredActivities", [])}
                updated_list = list(ia.get("ignoredActivities", []))
                for c_entry in curse_entries:
                    if c_entry["id"] not in existing_ids:
                        updated_list.append(c_entry)
                        existing_ids.add(c_entry["id"])
                        needs_update = True
                
                if needs_update:
                    ia["ignoredActivities"] = updated_list
                    eq_data["plugins"]["IgnoreActivities"] = ia
                    with open(equi_settings, "w", encoding="utf-8") as f:
                        json.dump(eq_data, f, indent=2)
                    print("[INFO] Configured Equicord IgnoreActivities in Blacklist mode for CurseForge suppression", flush=True)
    except Exception:
        pass

    # 3. Permanently enforce disabled Discord Rich Presence in CurseForge configuration
    try:
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            cf_storage = os.path.join(appdata, "CurseForge", "storage.json")
            if os.path.exists(cf_storage):
                with open(cf_storage, "r", encoding="utf-8") as f:
                    cf_data = json.load(f)
                priv_str = cf_data.get("privacy-settings")
                needs_cf_update = False
                if not priv_str:
                    needs_cf_update = True
                else:
                    try:
                        p_obj = json.loads(priv_str)
                        if p_obj.get("enableDiscordRichPresence") is not False:
                            needs_cf_update = True
                    except Exception:
                        needs_cf_update = True
                if needs_cf_update:
                    cf_data["privacy-settings"] = json.dumps({
                        "isPrivacyOptimizePerformance": True,
                        "isPrivacyCustomize": False,
                        "enableDiscordRichPresence": False,
                        "enableCRN": False
                    })
                    with open(cf_storage, "w", encoding="utf-8") as f:
                        json.dump(cf_data, f, indent=4)
                    print("[INFO] Enforced enableDiscordRichPresence=False in CurseForge storage.json", flush=True)

        # Terminate any lingering Curse.Agent.Host.exe process that may hold a stale Discord RPC pipe
        subprocess.run(["taskkill", "/F", "/IM", "Curse.Agent.Host.exe"], capture_output=True, creationflags=0x08000000)
    except Exception:
        pass


KNOWN_GAMES = {
    "robloxplayerbeta.exe": ("Roblox", "roblox"),
    "robloxplayer.exe": ("Roblox", "roblox"),
    "javaw.exe": ("Minecraft", "minecraft"),
    "minecraft.exe": ("Minecraft", "minecraft"),
    "minecraftbedrock.exe": ("Minecraft (Bedrock)", "minecraft"),
    "valorant.exe": ("Valorant", "valorant"),
    "valorant-win64-shipping.exe": ("Valorant", "valorant"),
    "league of legends.exe": ("League of Legends", "league_of_legends"),
    "leagueclient.exe": ("League of Legends", "league_of_legends"),
    "fortniteclient-win64-shipping.exe": ("Fortnite", "fortnite"),
    "genshinimpact.exe": ("Genshin Impact", "genshin_impact"),
    "honkaistarrail.exe": ("Honkai: Star Rail", "honkai_star_rail"),
    "zenlesszonezero.exe": ("Zenless Zone Zero", "zenless_zone_zero"),
    "overwatch.exe": ("Overwatch 2", "overwatch"),
    "r5apex.exe": ("Apex Legends", "apex_legends"),
    "gta5.exe": ("Grand Theft Auto V", "gta5"),
    "gtav.exe": ("Grand Theft Auto V", "gta5"),
    "osu!.exe": ("osu!", "osu"),
    "osu.exe": ("osu!", "osu"),
    "rocketleague.exe": ("Rocket League", "rocket_league"),
    "destiny2.exe": ("Destiny 2", "destiny2"),
    "cyberpunk2077.exe": ("Cyberpunk 2077", "cyberpunk2077"),
    "eldenring.exe": ("Elden Ring", "eldenring"),
    "helldivers2.exe": ("Helldivers 2", "helldivers2"),
    "palworld-win64-shipping.exe": ("Palworld", "palworld"),
    "terraria.exe": ("Terraria", "terraria"),
    "tmodloader.exe": ("tModLoader", "tmodloader"),
    "escapefromtarkov.exe": ("Escape From Tarkov", "tarkov"),
    "warframe.x64.exe": ("Warframe", "warframe"),
    "rainbowsix.exe": ("Rainbow Six Siege", "rainbowsix"),
    "rustclient.exe": ("Rust", "rust"),
    "deadbydaylight-win64-shipping.exe": ("Dead by Daylight", "deadbydaylight"),
    "wow.exe": ("World of Warcraft", "wow"),
    "wowclassic.exe": ("World of Warcraft Classic", "wowclassic"),
    "diablo iv.exe": ("Diablo IV", "diablo4"),
    "starcraft.exe": ("StarCraft", "starcraft"),
    "sc2_x64.exe": ("StarCraft II", "sc2"),
    "heroes of the storm_x64.exe": ("Heroes of the Storm", "hots"),
    "fallout4.exe": ("Fallout 4", "fallout4"),
    "skyrimse.exe": ("Skyrim", "skyrim"),
    "baldursgate3.exe": ("Baldur's Gate 3", "bg3"),
    "bg3_dx11.exe": ("Baldur's Gate 3", "bg3"),
    "bg3.exe": ("Baldur's Gate 3", "bg3"),
    "subnautica.exe": ("Subnautica", "subnautica"),
    "sekiro.exe": ("Sekiro: Shadows Die Twice", "sekiro"),
    "darksoulsiii.exe": ("Dark Souls III", "darksouls3"),
    "armoredcore6.exe": ("Armored Core VI", "armoredcore6"),
    "monsterhunterrise.exe": ("Monster Hunter Rise", "mhrise"),
    "monsterhunterworld.exe": ("Monster Hunter: World", "mhworld"),
    "blackmythwukong.exe": ("Black Myth: Wukong", "blackmythwukong"),
    "b1-win64-shipping.exe": ("Black Myth: Wukong", "blackmythwukong"),
    "among us.exe": ("Among Us", "amongus"),
    "lethal company.exe": ("Lethal Company", "lethalcompany"),
    "marvelrivals.exe": ("Marvel Rivals", "marvelrivals"),
    "marvelrivals-win64-shipping.exe": ("Marvel Rivals", "marvelrivals"),
}

_steam_app_cache = {}
GAME_DEBOUNCE_SECONDS = 25  # Grace period for game transitions, server changes, and loading screens
_game_sessions = {}  # {game_name: {"start_time": float, "last_seen": float, "game_info": dict}}
_game_tracker = {
    "current": None,
    "start_time": None,
    "last_seen": 0.0,
    "last_game_info": None
}


def is_pid_alive(pid):
    if not pid:
        return False
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        h = kernel32.OpenProcess(0x1000, False, int(pid))
        if h:
            kernel32.CloseHandle(h)
            return True
        return False
    except Exception:
        return False


def detect_active_games(cfg, max_games=3, return_pids=False):
    """
    Scans running games across:
    1. Steam RunningAppID
    2. Custom games in config.json
    3. Known popular games
    4. Discord detectable games database (10,000+ PC games)
    Attaches process IDs (PIDs) to each detected game and collects all active game PIDs.
    """
    import re
    detected = []
    seen_names = set()
    all_game_pids = set()

    disabled_set = set(str(x).lower().strip() for x in cfg.get("disabled_games", []))

    def add_game(name, slug, steam_appid=None, exe_name=None, discord_icon=None, pid=None, pids=None):
        if not name:
            return
        if (slug and slug.lower() in disabled_set) or (name and name.lower() in disabled_set) or (exe_name and exe_name.lower() in disabled_set):
            return
        if "spiral" in name.lower() or "spiral" in (slug or "").lower():
            return
        if name.lower() not in seen_names:
            seen_names.add(name.lower())
            detected.append({
                "name": name,
                "slug": slug,
                "steam_appid": steam_appid,
                "exe_name": exe_name,
                "discord_icon": discord_icon,
                "pid": pid,
                "pids": pids or []
            })

    # 1. Check Steam RunningAppID
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            running_appid, _ = winreg.QueryValueEx(key, "RunningAppID")
            steam_path, _ = winreg.QueryValueEx(key, "SteamPath")

        if running_appid and running_appid > 0:
            if running_appid in _steam_app_cache:
                add_game(**_steam_app_cache[running_appid])
            else:
                name = None
                search_dirs = [os.path.join(steam_path, "steamapps")]
                vdf_path = os.path.join(steam_path, "steamapps", "libraryfolders.vdf")
                if os.path.exists(vdf_path):
                    try:
                        with open(vdf_path, "r", encoding="utf-8", errors="ignore") as f:
                            for match in re.finditer(r'"path"\s+"([^"]+)"', f.read()):
                                lib_dir = os.path.join(match.group(1).replace("\\\\", "\\"), "steamapps")
                                if os.path.exists(lib_dir) and lib_dir not in search_dirs:
                                    search_dirs.append(lib_dir)
                    except Exception:
                        pass

                for sdir in search_dirs:
                    mfile = os.path.join(sdir, f"appmanifest_{running_appid}.acf")
                    if os.path.exists(mfile):
                        try:
                            with open(mfile, "r", encoding="utf-8", errors="ignore") as f:
                                m = re.search(r'"name"\s+"([^"]+)"', f.read())
                                if m:
                                    name = m.group(1)
                                    break
                        except Exception:
                            pass

                if not name:
                    try:
                        resp = requests.get(f"https://store.steampowered.com/api/appdetails?appids={running_appid}", timeout=2.0)
                        if resp.status_code == 200:
                            data = resp.json().get(str(running_appid), {})
                            if data.get("success") and "data" in data:
                                name = data["data"].get("name")
                    except Exception:
                        pass

                if not name:
                    name = f"Steam Game ({running_appid})"

                slug = re.sub(r'[^a-z0-9_]', '', name.lower().replace(" ", "_"))
                g_dict = {"name": name, "slug": slug, "steam_appid": running_appid, "exe_name": None}
                _steam_app_cache[running_appid] = g_dict
                add_game(**g_dict)
    except Exception:
        pass

    # 2. Check running processes snapshot & collect process IDs
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        hSnap = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
        proc_pids = {}
        if kernel32.Process32First(hSnap, ctypes.byref(pe)):
            while True:
                ename = pe.szExeFile.decode("latin1", errors="ignore").lower()
                proc_pids.setdefault(ename, []).append(pe.th32ProcessID)
                if not kernel32.Process32Next(hSnap, ctypes.byref(pe)):
                    break
        kernel32.CloseHandle(hSnap)

        claimed_exes = set()

        # Check custom games from config
        custom_games = cfg.get("custom_games", {})
        for exe_name, c_info in custom_games.items():
            ename = exe_name.lower()
            if ename in proc_pids:
                claimed_exes.add(ename)
                gpids = proc_pids[ename]
                all_game_pids.update(gpids)
                if len(detected) < max_games:
                    if isinstance(c_info, dict):
                        name = c_info.get("name", exe_name)
                        slug = c_info.get("slug") or re.sub(r'[^a-z0-9_]', '', name.lower().replace(" ", "_"))
                        add_game(name, slug, steam_appid=c_info.get("steam_appid"), exe_name=exe_name, pid=gpids[0], pids=gpids)
                    else:
                        name = str(c_info)
                        slug = re.sub(r'[^a-z0-9_]', '', name.lower().replace(" ", "_"))
                        add_game(name, slug, exe_name=exe_name, pid=gpids[0], pids=gpids)

        # Check known popular games
        for exe_name, g_info in KNOWN_GAMES.items():
            ename = exe_name.lower()
            if ename in proc_pids:
                claimed_exes.add(ename)
                gpids = proc_pids[ename]
                all_game_pids.update(gpids)
                if len(detected) < max_games:
                    if isinstance(g_info, tuple):
                        name, slug = g_info
                    else:
                        name = g_info
                        slug = re.sub(r'[^a-z0-9_]', '', name.lower().replace(" ", "_"))
                    add_game(name, slug, exe_name=exe_name, pid=gpids[0], pids=gpids)

        # Check against comprehensive Discord games database (10,000+ PC games)
        # Skip generic runtime and interpreter executables that are handled specially
        GENERIC_RUNTIMES = {"javaw.exe", "java.exe", "python.exe", "pythonw.exe", "cmd.exe", "powershell.exe", "explorer.exe"}
        if _discord_games_db:
            for ename in proc_pids:
                if ename in claimed_exes or ename in GENERIC_RUNTIMES:
                    continue
                if ename in _discord_games_db:
                    claimed_exes.add(ename)
                    gpids = proc_pids[ename]
                    all_game_pids.update(gpids)
                    if len(detected) < max_games:
                        g_meta = _discord_games_db[ename]
                        g_name = g_meta.get("name", ename)
                        slug = re.sub(r'[^a-z0-9_]', '', g_name.lower().replace(" ", "_"))
                        add_game(g_name, slug, exe_name=ename, discord_icon=g_meta.get("icon"), pid=gpids[0], pids=gpids)

        # Collect all CurseForge, Overwolf & Minecraft Java helper PIDs for active RPC clearing and suppression
        BLOCKED_LAUNCHER_EXES = (
            "curseforge.exe", "curseforgewindows.exe", "curse.agent.host.exe",
            "overwolf.exe", "overwolflauncher.exe", "overwolfbrowser.exe",
            "javaw.exe", "java.exe", "minecraft.exe", "minecraft.windows.exe"
        )
        for b_exe in BLOCKED_LAUNCHER_EXES:
            if b_exe in proc_pids:
                all_game_pids.update(proc_pids[b_exe])

    except Exception:
        pass

    if return_pids:
        return detected[:max_games], all_game_pids
    return detected[:max_games]


def detect_game_activity(cfg):
    """
    Backwards-compatible helper returning the primary detected game.
    """
    games = detect_active_games(cfg, max_games=1)
    return games[0] if games else None


# Official Brand Logo CDNs
DEFAULT_PROXMOX_ICON = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/proxmox.png"
DEFAULT_KRYPTEX_ICON = "https://www.kryptex.com/static/v2/favicons/android-chrome-512x512.aba2291aca42.png"
DEFAULT_CLOUDFLARE_ICON = "https://cdn.jsdelivr.net/gh/IAndrexI/proxDiscord@main/assets/cloudflare.png"
DEFAULT_SPEED_ICON = DEFAULT_CLOUDFLARE_ICON
DEFAULT_STEAM_ICON = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/steam.png"
DEFAULT_GITHUB_ICON = "https://cdn.jsdelivr.net/gh/IAndrexI/proxDiscord@main/assets/github.png"
DEFAULT_DVD_ICON = "https://cdn.jsdelivr.net/gh/IAndrexI/proxDiscord@main/assets/dvd.png"
DEFAULT_FREE_GAMES_ICON = DEFAULT_DVD_ICON
DEFAULT_EPIC_GAMES_ICON = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/epic-games.png"

# Built-in official Discord CDN application icons for instant zero-latency image matching
BUILTIN_GAME_ICONS = {
    "proxmox": DEFAULT_PROXMOX_ICON,
    "kryptex": DEFAULT_KRYPTEX_ICON,
    "cloudflare": DEFAULT_CLOUDFLARE_ICON,
    "speed": DEFAULT_SPEED_ICON,
    "speedtest": DEFAULT_SPEED_ICON,
    "steam": DEFAULT_STEAM_ICON,
    "github": DEFAULT_GITHUB_ICON,
    "epic": DEFAULT_EPIC_GAMES_ICON,
    "epic_games": DEFAULT_EPIC_GAMES_ICON,
    "dvd": DEFAULT_DVD_ICON,
    "disc": DEFAULT_DVD_ICON,
    "freegames": DEFAULT_FREE_GAMES_ICON,
    "roblox": "https://cdn.discordapp.com/app-icons/363445589247131668/f2b60e350a2097289b3b0b877495e55f.png",
    "minecraft": "https://cdn.discordapp.com/app-icons/1402418491272986635/166fbad351ecdd02d11a3b464748f66b.png",
    "valorant": "https://cdn.discordapp.com/app-icons/700136079562375258/11f81959f4fdd76ca6c39c59eac256c1.png",
    "fortnite": "https://cdn.discordapp.com/app-icons/1402418703554842694/c1864b38910c209afd5bf6423b672022.png",
    "league_of_legends": "https://cdn.discordapp.com/app-icons/1402418765274026024/76118d09f6d4d76f8271e847cbbfe7b2.png",
    "genshin_impact": "https://cdn.discordapp.com/app-icons/762434991303950386/0a7cc00267310bf4afdd78b175a7aeea.png",
    "honkai_star_rail": "https://cdn.discordapp.com/app-icons/1121201675240210523/444d067889922e42b0af99b13e5d5c72.png",
    "zenless_zone_zero": "https://cdn.discordapp.com/app-icons/1257819671114289184/fb528a20677f93d8f365fac88e3f0713.png",
    "overwatch": "https://cdn.discordapp.com/app-icons/356875221078245376/a60bb76ba4d4acafbd4cb9aad6e61739.png",
    "apex_legends": "https://cdn.discordapp.com/app-icons/542075586886107149/91fac0600c5b6527c1aea95a93c6a8e0.png",
    "gta5": "https://cdn.discordapp.com/app-icons/1402418714716143646/b77111108195cd5e4dd2011dd39bf67d.png",
    "osu": "https://cdn.discordapp.com/app-icons/1402418239342120960/ea86f6c52576847a7cb81f1c1faa18a3.png",
    "rocket_league": "https://cdn.discordapp.com/app-icons/356877880938070016/a74899a5190c48a3e6ce9f8d2eaff348.png",
    "destiny2": "https://cdn.discordapp.com/app-icons/372438022647578634/876323877dd2f3e3fdfc1637a30eb356.png",
    "cyberpunk2077": "https://cdn.discordapp.com/app-icons/787443973538971748/023b9ec72cd60e31e25bca878c77984a.png",
    "eldenring": "https://cdn.discordapp.com/app-icons/1377783621775130694/be5767e5d6fc2f9ab982f461cff7a528.png",
    "helldivers2": "https://cdn.discordapp.com/app-icons/1205090671527071784/6d49be66d5f2b88bdfc8cc7095abdbda.png",
    "palworld": "https://cdn.discordapp.com/app-icons/1197827812623650866/f2039761488809de552d44ebc6739ffe.png",
    "terraria": "https://cdn.discordapp.com/app-icons/1402418344912752671/4c3c185abc0dfb4cb1ec5612de4d7366.png",
    "tarkov": "https://cdn.discordapp.com/app-icons/406637848297472017/1e5e0defac5328c442fbd425f2079b69.png",
    "warframe": "https://cdn.discordapp.com/app-icons/1402416961962381402/17bae2c6f31fcebbbac09b7a569fc0b9.png",
    "rainbowsix": "https://cdn.discordapp.com/app-icons/356876590342340608/01125e693db476e6f83f7d9769080fd0.png",
    "rust": "https://cdn.discordapp.com/app-icons/1402418594532298837/9ab7e18473429b016307b867e6c924a4.png",
    "deadbydaylight": "https://cdn.discordapp.com/app-icons/357607133254254632/64e7623c9af49f2e7dc7048df16b1013.png",
    "wow": "https://cdn.discordapp.com/app-icons/356875762940379136/fc92f820c44e72085dc6205e5e746850.png",
    "diablo4": "https://cdn.discordapp.com/app-icons/1113966530531704943/ced913ddd2b497545cd3e2931b1310ab.png",
    "starcraft": "https://cdn.discordapp.com/app-icons/358425800766128128/ba12a43bee663d2a3b06a583bc80f4bf.png",
    "sc2": "https://cdn.discordapp.com/app-icons/358425800766128128/ba12a43bee663d2a3b06a583bc80f4bf.png",
    "hots": "https://cdn.discordapp.com/app-icons/356878860190613504/7b3bc9037909ab14917accca8c6fb8c1.png",
    "fallout4": "https://cdn.discordapp.com/app-icons/359509759642042378/6c903026d4fc97559ba48ecf9cc4dc04.png",
    "skyrim": "https://cdn.discordapp.com/app-icons/359507724196773888/05e8f8b49eb61bb6f4a97c77c1d7fbdb.png",
    "darksouls3": "https://cdn.discordapp.com/app-icons/359509500199436288/4ae400e61d27c2b0b86fd1c15f4eb8d7.png",
    "armoredcore6": "https://cdn.discordapp.com/app-icons/1146138865673982022/4827b30d4aa166729ecc2ed21c43a465.png",
    "mhrise": "https://cdn.discordapp.com/app-icons/1022248949865791588/93ca098b4b56d0e9df5bfe49e990fe4d.png",
    "mhworld": "https://cdn.discordapp.com/app-icons/477152881196269569/8fd08a2e0440f80334ea403d2300a828.png",
    "blackmythwukong": "https://cdn.discordapp.com/app-icons/1272842103910699040/908f3f31004652e1971c6e8a4b9d7c30.png",
    "lethalcompany": "https://cdn.discordapp.com/app-icons/1167674267748540516/4f1ee29121b9a0f4dfcc4ce6bb9bd5af.png",
    "marvelrivals": "https://cdn.discordapp.com/app-icons/1314395942253756416/2dd7882b887306ab5afad03452869ad8.png",
    "sekiro": "https://cdn.discordapp.com/app-icons/1402416796874834143/d21ee8b4a8c5836b60bc2b673544a634.png",
    "subnautica": "https://cdn.discordapp.com/app-icons/1402416999887278220/3ada80e2f8d7d86ec75587d8ba783756.png"
}

_game_icon_cache = {}


def resolve_game_image(game_info, cfg):
    """
    Automatically resolves the best high-res game image:
    1. Custom user override in config.json ('game_images') if it's a URL or custom asset
    2. Steam Game: official Steam CDN header
    3. Built-in Popular Games list (official Discord CDN verified icons)
    4. Discord Detectable Applications API (covers 24,000+ PC games on Discord)
    5. Config override fallback or None
    """
    if not game_info:
        return None

    game_name = game_info.get("name", "")
    slug = game_info.get("slug", "").lower()
    appid = game_info.get("steam_appid")
    exe_name = game_info.get("exe_name") or ""
    if exe_name:
        exe_name = exe_name.lower()

    game_images = cfg.get("game_images", {})
    override = (
        game_images.get(slug)
        or game_images.get(game_name)
        or (game_images.get(str(appid)) if appid else None)
    )

    # If user provided a specific direct URL or distinct custom asset
    if override and (override.startswith("http://") or override.startswith("https://")):
        return override

    cache_key = str(appid) if appid else (exe_name or slug or game_name)
    if cache_key in _game_icon_cache:
        return _game_icon_cache[cache_key]

    # Steam Game: auto Steam CDN banner
    if appid:
        url = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"
        _game_icon_cache[cache_key] = url
        return url

    # Built-in Popular Games list (official Discord verified icons)
    if slug in BUILTIN_GAME_ICONS:
        url = BUILTIN_GAME_ICONS[slug]
        _game_icon_cache[cache_key] = url
        return url

    # Official Discord icon from database
    if game_info.get("discord_icon"):
        _game_icon_cache[cache_key] = game_info["discord_icon"]
        return game_info["discord_icon"]

    # Check indexed local Discord games DB
    if exe_name and _discord_games_db and exe_name in _discord_games_db:
        icon_url = _discord_games_db[exe_name].get("icon")
        if icon_url:
            _game_icon_cache[cache_key] = icon_url
            return icon_url

    # Dynamic Discord Detectable API lookup (covers 24,000+ games)
    try:
        resp = requests.get("https://discord.com/api/v9/applications/detectable", timeout=3.0)
        if resp.status_code == 200:
            for item in resp.json():
                for exe in item.get("executables", []):
                    ename = exe.get("name", "").lower()
                    if ename and (ename == exe_name or ename.endswith("/" + exe_name) or ename.endswith("\\" + exe_name)):
                        icon = item.get("icon_hash") or item.get("cover_image_hash")
                        if icon:
                            url = f"https://cdn.discordapp.com/app-icons/{item['id']}/{icon}.png"
                            _game_icon_cache[cache_key] = url
                            return url
                if item.get("name", "").lower() == game_name.lower():
                    icon = item.get("icon_hash") or item.get("cover_image_hash")
                    if icon:
                        url = f"https://cdn.discordapp.com/app-icons/{item['id']}/{icon}.png"
                        _game_icon_cache[cache_key] = url
                        return url
    except Exception:
        pass

    # If user provided an asset key in config, return it
    if override:
        return override

    return None


_app_mutex = None

def main():
    global _app_mutex, _dashboard_state
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        _app_mutex = kernel32.CreateMutexW(None, False, "Global\\ProxmoxDiscordRPC_Instance")
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            print("[INFO] Proxmox Discord RPC is already running in the background. Exiting.", flush=True)
            sys.exit(0)

        # Ensure Registry Run Key exists for seamless background auto-start on boot
        try:
            import winreg
            run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
            pyw_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "Scripts", "pythonw.exe")
            script_path = os.path.abspath(__file__)
            if os.path.isfile(pyw_path) and os.path.isfile(script_path):
                cmd_str = f'"{pyw_path}" "{script_path}"'
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.SetValueEx(key, "ProxmoxDiscordRPC", 0, winreg.REG_SZ, cmd_str)
        except Exception:
            pass

    cfg = load_config()
    client_id = cfg.get("discord_client_id", "1548928413337788486")
    interval = cfg.get("update_interval_seconds", 6)

    # Start web dashboard server if enabled
    if cfg.get("enable_dashboard_button", True):
        start_dashboard_server_if_needed(cfg)
        start_cloudflare_tunnel_if_needed(cfg)

    print("=" * 60, flush=True)
    print("  Proxmox VE Discord Rich Presence (RPC) - Rotating Mode", flush=True)
    print(f"  App ID:    {client_id}", flush=True)
    print(f"  Node:      {cfg.get('proxmox_node')}", flush=True)
    print(f"  Target:    {cfg.get('proxmox_host')}", flush=True)
    print(f"  Badges:    {'Enabled' if cfg.get('show_party_badge', True) else 'Disabled'}", flush=True)
    print(f"  Kryptex:   {'Enabled' if cfg.get('enable_kryptex_screen', True) else 'Disabled'}", flush=True)
    print(f"  Gaming:    {'Enabled' if cfg.get('enable_game_activity', True) else 'Disabled'}", flush=True)
    patch_discord_game_utils()

    rpc = None
    boot_time = int(time.time())
    screen_index = 0
    last_screen_count = 4 if cfg.get("enable_minecraft_screen", False) else 3
    next_tick = time.time()
    _previously_active_pids = set()

    while True:
        # 1. Ensure Discord RPC connection
        if rpc is None:
            try:
                _new_rpc = Presence(client_id)
                _new_rpc.connect()
                rpc = _new_rpc
                print("[INFO] Connected to Discord RPC successfully!", flush=True)
                next_tick = time.time()
            except DiscordNotFound:
                rpc = None
                print("[WAIT] Discord client is not running. Retrying in 10s...", flush=True)
                time.sleep(10)
                next_tick = time.time()
                continue
            except Exception as e:
                rpc = None
                print(f"[WAIT] Could not connect to Discord ({e}). Retrying in 10s...", flush=True)
                time.sleep(10)
                next_tick = time.time()
                continue

        # 2. Reload config and fetch stats
        cycle_start = time.time()
        try:
            cfg = load_config()
            interval = float(cfg.get("update_interval_seconds", 6))

            # Check game activity & sessions (always detect for suppression & priority enforcement)
            now = time.time()
            detected_games, all_game_pids = detect_active_games(cfg, max_games=5, return_pids=True)
            active_games = []
            if cfg.get("enable_game_activity", True) or cfg.get("enable_active_games_hub", True):
                active_games = detected_games
                # Update multi-game tracking sessions
                for g_info in active_games:
                    g_name = g_info["name"]
                    if g_name not in _game_sessions:
                        _game_sessions[g_name] = {
                            "start_time": now,
                            "last_seen": now,
                            "game_info": g_info
                        }
                    else:
                        _game_sessions[g_name]["last_seen"] = now
                        _game_sessions[g_name]["game_info"] = g_info

                # Debounce / cleanup closed games
                expired_games = [name for name, session in _game_sessions.items() if (now - session["last_seen"]) >= GAME_DEBOUNCE_SECONDS]
                for name in expired_games:
                    del _game_sessions[name]
            else:
                _game_sessions.clear()

            try:
                stats = get_cached_proxmox_stats(cfg)
            except Exception:
                stats = make_default_proxmox_stats(cfg)
            label = cfg.get("server_label", "Protutech")

            # Build list of active screens
            screens = []

            # Screen 1: Proxmox Overview (Performance, Workloads & Storage)
            if cfg.get("enable_proxmox_screen", True):
                node_name = stats.get("node", "Protutech")
                node_tag = f"{label}: {node_name}" if label.lower() != str(node_name).lower() else label
                if stats.get("offline"):
                    pve_details = f"{node_tag} | Hypervisor Standby"
                    pve_state = "Protutech Cloud Services"
                else:
                    pve_details = f"{node_tag} (Up: {stats.get('uptime', '0m')}) | {stats.get('running_vms', 0)} VMs | {stats.get('running_lxcs', 0)} LXCs"
                    pve_state = f"CPU: {stats.get('cpu_pct', 0.0):.1f}% | RAM: {stats.get('mem_pct', 0.0):.0f}% | Storage: {stats.get('storage_used_gb', 0.0):.0f}G/{stats.get('storage_total_tb', 1.0):.1f}TB"
                screens.append({
                    "name": "Proxmox Overview",
                    "details": pve_details,
                    "state": pve_state
                })

            # Screen 2: Cryptocurrency Mining Status (when enabled)
            k_stats = None
            if cfg.get("enable_kryptex_screen", True):
                k_stats = fetch_kryptex_stats(cfg)
                if k_stats:
                    show_bal = cfg.get("show_kryptex_balance", False)
                    show_gpu = cfg.get("show_kryptex_gpu", True)
                    show_cpu = cfg.get("show_kryptex_cpu", True)

                    bal_str = f" | ${k_stats['balance']:.2f}" if (show_bal and k_stats.get("balance") is not None) else ""
                    if k_stats["mining"]:
                        gpu = k_stats.get("gpu") if show_gpu else None
                        cpu = k_stats.get("cpu") if show_cpu else None

                        details = f"Crypto Mining{bal_str}"
                        parts = []
                        if gpu and gpu.get("coin_full"):
                            parts.append(f"GPU: {gpu['coin_full']}")
                        if cpu and cpu.get("coin_full"):
                            parts.append(f"CPU: {cpu['coin_full']}")

                        state = " | ".join(parts) if parts else "Mining Active"
                    else:
                        details = f"Crypto Mining: Idle{bal_str}"
                        state = "GPU & CPU Standby"

                    screens.append({
                        "name": "Crypto Miner",
                        "details": details,
                        "state": state
                    })

            # Screen 3: Game Activity & Active Games Hub (when enabled)
            if cfg.get("enable_game_activity", True) or cfg.get("enable_active_games_hub", True):
                if active_games:
                    first_game = active_games[0]
                    _game_tracker["current"] = first_game["name"]
                    _game_tracker["start_time"] = _game_sessions.get(first_game["name"], {}).get("start_time", now)

                    # 3a. Combined Active Games Hub Screen (Dynamic party count based on games)
                    if cfg.get("enable_active_games_hub", True):
                        g_names = [g["name"] for g in active_games]
                        display_titles = " • ".join(g_names[:3])
                        if len(g_names) > 3:
                            display_titles += f" (+{len(g_names) - 3} more)"
                        cnt = len(active_games)
                        hub_start = min([_game_sessions[g["name"]]["start_time"] for g in active_games if g["name"] in _game_sessions] or [boot_time])
                        screens.append({
                            "name": "Active Games Hub",
                            "screen_type": "games_hub",
                            "details": f"Active Games: {display_titles}",
                            "state": f"{cnt} Running • Gaming Hub",
                            "active_games": active_games,
                            "party_size": [cnt, max(cnt, 4)],
                            "party_id": "active_games_hub",
                            "start_time": hub_start
                        })

                    # 3b. Separate screens for each running game (up to 3)
                    for game_name, session in list(_game_sessions.items())[:3]:
                        elapsed = format_uptime(now - session["start_time"])
                        screens.append({
                            "name": f"Game: {game_name}",
                            "screen_type": "game",
                            "details": f"{game_name}",
                            "state": f"Time Opened: {elapsed}",
                            "game_info": session["game_info"],
                            "start_time": session["start_time"]
                        })
                else:
                    _game_tracker["current"] = None
                    _game_tracker["start_time"] = None
                    if cfg.get("enable_active_games_hub", True):
                        screens.append({
                            "name": "Active Games Hub",
                            "screen_type": "games_hub",
                            "details": "Gaming: Standby",
                            "state": "No games currently open",
                            "active_games": [],
                            "start_time": boot_time
                        })
                    else:
                        screens.append({
                            "name": "Game Activity",
                            "screen_type": "game",
                            "details": "Gaming: Standby",
                            "state": "No game currently open",
                            "game_info": None,
                            "start_time": boot_time
                        })

            # Screen 4: Local Minecraft Client & Modpack Screen (when enabled)
            if cfg.get("enable_minecraft_screen", True):
                mc_status = fetch_minecraft_status(cfg)
                if not (cfg.get("minecraft_only_when_playing", False) and not mc_status.get("online")):
                    screens.append({
                        "name": "Minecraft",
                        "screen_type": "minecraft",
                        "details": mc_status["details"],
                        "state": mc_status["state"],
                        "mc_status": mc_status,
                        "start_time": mc_status.get("start_time", boot_time) if mc_status.get("online") else boot_time
                    })

            # Screen 4b: Minecraft Self-Hosted Server Screen (when enabled)
            if cfg.get("enable_minecraft_server_screen", True):
                start_mc_server_worker_if_needed(cfg)
                mc_srv = get_cached_mc_server_status(cfg)
                if mc_srv:
                    srv_label = mc_srv.get("label") or "Protutech Server"
                    if mc_srv.get("online"):
                        p_on = mc_srv.get("players_online", 0)
                        p_max = mc_srv.get("players_max", 20)
                        mc_srv_details = f"MC Server: {p_on}/{p_max} Players Online"
                        mc_srv_state = f"{srv_label} • v{mc_srv.get('version', '1.20+')}"
                    else:
                        mc_srv_details = "MC Server: Offline • Standby"
                        mc_srv_state = f"{mc_srv.get('hostname', 'minecraft.protutech.vip')} • Standby"

                    screens.append({
                        "name": "Minecraft Server",
                        "screen_type": "minecraft_server",
                        "details": mc_srv_details,
                        "state": mc_srv_state,
                        "mc_server_data": mc_srv
                    })

            # Screen 5: Optional Network Speed & Latency (when enabled)
            if cfg.get("enable_speed_screen", False):
                start_net_worker_if_needed(cfg)
                with _net_lock:
                    d_val = _net_stats.get("down_mbps")
                    u_val = _net_stats.get("up_mbps")
                    p_val = _net_stats.get("ping_ms")

                def format_net_speed(mbps):
                    if mbps is None:
                        return "8.12 Gbps"
                    if mbps >= 1000:
                        return f"{mbps / 1000:.2f} Gbps"
                    return f"{mbps:.0f} Mbps"

                d_str = format_net_speed(d_val) if d_val is not None else "8.12 Gbps"
                u_str = format_net_speed(u_val) if u_val is not None else "4.42 Gbps"
                speed_details = f"Internet: {d_str} Down | {u_str} Up"

                p_formatted = f"{p_val:.1f}ms" if (p_val is not None and p_val < 10) else (f"{p_val:.0f}ms" if p_val is not None else "2.2ms")
                speed_state = f"Ping: {p_formatted} | Protutech Cloud"

                screens.append({
                    "name": "Network Speed",
                    "details": speed_details,
                    "state": speed_state
                })

            # Screen 6: Steam Profile with Full Stats (when enabled)
            if cfg.get("enable_steam_screen", True):
                start_steam_worker_if_needed(cfg)
                steam_data = get_cached_steam_stats(cfg)
                if steam_data:
                    lvl = steam_data.get("level", "0")
                    achs = steam_data.get("achievements") or "0"
                    bdgs = steam_data.get("badges") or "0"
                    pfg = steam_data.get("perfect_games") or "0"
                    st_status = steam_data.get("status") or "Online"
                    screens.append({
                        "name": "Steam Profile",
                        "screen_type": "steam",
                        "details": f"Steam: {steam_data['persona']} (Lvl {lvl}) • {st_status}",
                        "state": f"{achs} Achs | {bdgs} Badges | {pfg} Perfect",
                        "steam_data": steam_data
                    })

            # Screen 7: GitHub Repositories (when enabled)
            if cfg.get("enable_github_screen", True):
                start_github_worker_if_needed(cfg)
                gh_stats = get_cached_github_stats(cfg)
                if gh_stats:
                    total_r = gh_stats.get("total_repos", 0)
                    proj_label = "Project Created" if total_r == 1 else "Projects Created"
                    screens.append({
                        "name": "GitHub Repositories",
                        "screen_type": "github",
                        "details": f"GitHub Repositories: {total_r}",
                        "state": f"{total_r} {proj_label} | Protutech Cloud",
                        "github_stats": gh_stats
                    })

            # Screen 8: Free PC Games Out Now (when enabled)
            if cfg.get("enable_free_games_screen", True):
                start_free_games_worker_if_needed(cfg)
                fg_stats = get_cached_free_games(cfg)
                games_list = fg_stats.get("games", []) if fg_stats else []
                fg_count = len(games_list)

                if fg_count == 1:
                    fg_details = f"Free Game: {games_list[0]['title']}"
                    fg_state = f"Claimable Now | {games_list[0]['platform']}"
                elif fg_count > 1:
                    short_titles = []
                    for g in games_list:
                        t = g["title"]
                        if len(t) > 24 and ":" in t:
                            t = t.split(":")[0].strip()
                        short_titles.append(t)

                    display_titles = " • ".join(short_titles[:2])
                    if len(short_titles) > 2:
                        display_titles += f" +{len(short_titles) - 2} more"

                    fg_details = f"Free Games: {display_titles}"
                    fg_state = f"{fg_count} Claimable Now | GamerPower & Epic"
                else:
                    fg_details = "Free PC Games: None Active"
                    fg_state = "GamerPower Tracker | Protutech Cloud"

                screens.append({
                    "name": "Free Games",
                    "screen_type": "free_games",
                    "details": fg_details,
                    "state": fg_state,
                    "free_games_data": fg_stats
                })

            # Screen 8b: Recommended Crypto & Stocks Market Watch (when enabled)
            if cfg.get("enable_market_screen", True):
                start_market_worker_if_needed(cfg)
                m_data = get_cached_market_stats(cfg)
                if m_data and m_data.get("items"):
                    c_dict = m_data.get("crypto_dict") or {x["symbol"]: x for x in m_data.get("crypto", []) if isinstance(x, dict)}
                    s_dict = m_data.get("stocks_dict") or {x["symbol"]: x for x in m_data.get("stocks", []) if isinstance(x, dict)}

                    c_parts = []
                    for c_sym in ("BTC", "ETH", "SOL"):
                        if c_sym in c_dict:
                            it = c_dict[c_sym]
                            pr = it["price"]
                            pr_str = f"${pr/1000:.1f}K" if pr >= 1000 else f"${pr:.2f}"
                            sgn = "+" if it["change_pct"] >= 0 else ""
                            c_parts.append(f"{c_sym} {pr_str} ({sgn}{it['change_pct']}%)")
                    m_details = " • ".join(c_parts[:2]) if c_parts else "Crypto Market Active"

                    s_parts = []
                    for s_sym in ("NVDA", "AAPL", "MSFT", "SPY"):
                        if s_sym in s_dict:
                            it = s_dict[s_sym]
                            sgn = "+" if it["change_pct"] >= 0 else ""
                            s_parts.append(f"{s_sym} ${it['price']:.0f} ({sgn}{it['change_pct']}%)")
                    m_state = " • ".join(s_parts[:2]) if s_parts else "Stocks Market Active"

                    screens.append({
                        "name": "Crypto & Stocks",
                        "screen_type": "market",
                        "details": m_details,
                        "state": m_state,
                        "market_data": m_data
                    })

            # Screen 9+: Custom Trackers (when configured)
            for ct in cfg.get("custom_trackers", []):
                if ct.get("enabled", True):
                    ct_name = ct.get("name", "Custom Tracker")
                    ct_details = ct.get("details", "")
                    ct_state = ct.get("state", "Protutech Cloud")
                    ct_icon = ct.get("icon_url") or DEFAULT_DVD_ICON
                    
                    check_url = ct.get("check_url")
                    if check_url:
                        c_id = ct.get("id") or ct_name
                        cached_c = _custom_trackers_cache.get(c_id)
                        c_interval = float(ct.get("interval_minutes", 5)) * 60
                        if not cached_c or (now - cached_c.get("last_checked", 0)) > c_interval:
                            try:
                                c_resp = requests.get(check_url, timeout=3.0)
                                _custom_trackers_cache[c_id] = {
                                    "online": c_resp.status_code < 400,
                                    "status_code": c_resp.status_code,
                                    "last_checked": now
                                }
                            except Exception:
                                _custom_trackers_cache[c_id] = {
                                    "online": False,
                                    "status_code": 0,
                                    "last_checked": now
                                }
                        c_info = _custom_trackers_cache.get(c_id, {})
                        c_stat = "● Online" if c_info.get("online") else "○ Offline"
                        if "{status}" in ct_details:
                            ct_details = ct_details.replace("{status}", c_stat)
                        elif not ct_details:
                            ct_details = f"{ct_name}: {c_stat}"

                    screens.append({
                        "name": ct_name,
                        "screen_type": "custom",
                        "custom_id": ct.get("id") or ct_name,
                        "details": ct_details,
                        "state": ct_state,
                        "large_image": ct_icon,
                        "custom_data": ct
                    })

            if not screens:
                screens.append({
                    "name": "Protutech Cloud",
                    "screen_type": "system",
                    "details": "Protutech Cloud Services",
                    "state": "All trackers standby",
                    "large_image": default_large
                })

            # Screen Selection: Manual lock or timed rotation
            active_mode = str(cfg.get("active_screen", "rotate")).strip().lower()
            selected_screen = None

            if active_mode not in ("rotate", "all", "timer", "timed", "cycle"):
                alias_map = {
                    "proxmox": "Proxmox Overview",
                    "pve": "Proxmox Overview",
                    "server": "Proxmox Overview",
                    "mining": "Crypto Miner",
                    "kryptex": "Crypto Miner",
                    "miner": "Crypto Miner",
                    "game": "Game Activity",
                    "gaming": "Game Activity",
                    "minecraft": "Minecraft",
                    "mc": "Minecraft",
                    "speed": "Network Speed",
                    "network": "Network Speed",
                    "internet": "Network Speed",
                    "ping": "Network Speed",
                    "steam": "Steam Profile",
                    "steamprofile": "Steam Profile",
                    "github": "GitHub Repositories",
                    "git": "GitHub Repositories",
                    "repos": "GitHub Repositories",
                    "freegames": "Free Games",
                    "freegame": "Free Games",
                    "games": "Free Games",
                    "minecraftserver": "Minecraft Server",
                    "mcserver": "Minecraft Server",
                    "market": "Crypto & Stocks",
                    "crypto": "Crypto & Stocks",
                    "stocks": "Crypto & Stocks",
                    "activegames": "Active Games",
                    "hub": "Active Games"
                }
                target_name = alias_map.get(active_mode, active_mode)
                for s in screens:
                    if (s["name"].lower() == target_name.lower() or 
                        target_name.lower() in s["name"].lower() or 
                        (target_name.lower() in ("game", "gaming") and s.get("screen_type") == "game")):
                        selected_screen = s
                        break

            if selected_screen:
                current_screen = selected_screen
            else:
                current_screen = screens[screen_index % len(screens)]
                last_screen_count = max(1, len(screens))
                screen_index = (screen_index + 1) % len(screens)

            default_large = cfg.get("large_image", "protutech")
            game_images = cfg.get("game_images", {})

            # Update web dashboard live state
            if cfg.get("enable_dashboard_button", True):
                try:
                    start_dashboard_server_if_needed(cfg)
                    start_cloudflare_tunnel_if_needed(cfg)
                    dash_screen_list = []
                    for s in screens:
                        try:
                            s_name = s.get("name", "")
                            s_type = s.get("screen_type", "System Screen")
                            if s_name == "Proxmox Overview":
                                s_icon = DEFAULT_PROXMOX_ICON
                            elif s_name == "Crypto Miner":
                                s_icon = DEFAULT_KRYPTEX_ICON
                            elif s_name == "Network Speed":
                                s_icon = DEFAULT_SPEED_ICON
                            elif s_name in ("GitHub Repositories", "GitHub"):
                                s_icon = DEFAULT_GITHUB_ICON
                            elif s_name == "Free Games":
                                s_icon = DEFAULT_FREE_GAMES_ICON
                            elif s_name == "Steam Profile":
                                s_icon = s.get("steam_data", {}).get("avatar_url") if s.get("steam_data") else default_large
                            elif s_name == "Minecraft":
                                mc_icon = s.get("mc_status", {}).get("icon_url")
                                if not mc_icon and s.get("mc_status", {}).get("local_icon_path"):
                                    mc_icon = "/api/minecraft/icon"
                                s_icon = mc_icon or BUILTIN_GAME_ICONS.get("minecraft", DEFAULT_PROXMOX_ICON)
                            elif s_name == "Minecraft Server":
                                s_icon = BUILTIN_GAME_ICONS.get("minecraft", DEFAULT_PROXMOX_ICON)
                            elif s_name == "Crypto & Stocks":
                                s_icon = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/tradingview.png"
                            elif s_name in ("Active Games", "Active Games Hub"):
                                s_icon = BUILTIN_GAME_ICONS.get("roblox", DEFAULT_STEAM_ICON)
                            elif s_type == "game":
                                s_icon = resolve_game_image(s.get("game_info"), cfg) or default_large
                            elif s_type == "custom":
                                s_icon = s.get("large_image") or DEFAULT_DVD_ICON
                            else:
                                s_icon = default_large
                        except Exception:
                            s_icon = default_large

                        dash_screen_list.append({
                            "name": s.get("name", "Screen"),
                            "screen_type": s.get("screen_type", "System Screen"),
                            "details": s.get("details", ""),
                            "state": s.get("state", ""),
                            "large_image": s_icon or default_large,
                            "hidden": s.get("name") in cfg.get("hidden_screens", []),
                            "custom_id": s.get("custom_id"),
                            "custom_data": s.get("custom_data"),
                            "stats": stats if s.get("name") == "Proxmox Overview" else None,
                            "k_stats": k_stats if s.get("name") == "Crypto Miner" else None,
                            "net_stats": _net_stats if s.get("name") == "Network Speed" else None,
                            "steam_data": s.get("steam_data"),
                            "github_stats": s.get("github_stats"),
                            "free_games_data": s.get("free_games_data"),
                            "game_info": s.get("game_info"),
                            "mc_status": s.get("mc_status"),
                            "server_data": s.get("mc_server_data"),
                            "mc_server_data": s.get("mc_server_data"),
                            "market_data": s.get("market_data"),
                            "games_hub": {
                                "active_count": len(s.get("active_games", [])),
                                "games": s.get("active_games", [])
                            } if s.get("name") in ("Active Games", "Active Games Hub") else None,
                            "active_games": s.get("active_games")
                        })

                    user_id = str(cfg.get("user_id", "andrex")).strip().lower()
                    user_name = str(cfg.get("user_display_name", "Andrex")).strip()
                    user_avatar = cfg.get("user_avatar_url") or ""
                    if not user_avatar:
                        for item in dash_screen_list:
                            if item.get("name") == "Steam Profile" and item.get("steam_data"):
                                user_avatar = item["steam_data"].get("avatar_url")
                                break
                    if not user_avatar:
                        user_avatar = DEFAULT_PROXMOX_ICON

                    with _dashboard_lock:
                        _dashboard_state.clear()
                        _dashboard_state.update({
                            "user_id": user_id,
                            "user_name": user_name,
                            "user_avatar": user_avatar,
                            "screens": dash_screen_list,
                            "current_screen_name": current_screen.get("name", ""),
                            "screen_index": screen_index,
                            "total_screens": len(screens),
                            "last_updated": time.time()
                        })
                        _users_store[user_id] = dict(_dashboard_state)

                    # Optional remote Cloudflare hub push
                    remote_hub = cfg.get("remote_hub_url")
                    if remote_hub:
                        try:
                            push_payload = {
                                "user_id": user_id,
                                "user_name": user_name,
                                "user_avatar": user_avatar,
                                "screens": dash_screen_list,
                                "current_screen_name": current_screen.get("name", ""),
                                "screen_index": screen_index,
                                "token": cfg.get("remote_hub_token", "")
                            }
                            threading.Thread(
                                target=lambda: requests.post(f"{remote_hub.rstrip('/')}/api/push", json=push_payload, timeout=5),
                                daemon=True
                            ).start()
                        except Exception:
                            pass
                except Exception as d_err:
                    print(f"[WARN] Dashboard state update error: {d_err}", flush=True)

            large_img = default_large
            large_txt = f"Protutech Cloud | {stats['running_guests']}/{stats['total_guests']} Services Online"
            small_img = None
            small_txt = None

            if current_screen["name"] == "Proxmox Overview":
                pve_img = cfg.get("proxmox_image") or game_images.get("proxmox") or DEFAULT_PROXMOX_ICON
                large_img = pve_img
                large_txt = f"Proxmox VE | {stats['running_guests']}/{stats['total_guests']} Services Online"
                small_img = default_large
                small_txt = "Protutech Cloud"

            elif current_screen["name"] in ("Kryptex Miner", "Crypto Miner"):
                k_custom = cfg.get("kryptex_image") or game_images.get("kryptex") or game_images.get("mining")
                if k_custom and (k_custom.startswith("http://") or k_custom.startswith("https://") or k_custom != "kryptex"):
                    large_img = k_custom
                else:
                    large_img = DEFAULT_KRYPTEX_ICON

                large_txt = "Kryptex Mining Rig | Protutech Cloud"
                small_img = default_large
                small_txt = "Protutech Cloud"

            elif current_screen.get("screen_type") == "game" or current_screen["name"].startswith("Game"):
                game_info = current_screen.get("game_info")
                if game_info:
                    game_name = game_info["name"]
                    chosen_img = resolve_game_image(game_info, cfg)

                    if chosen_img:
                        large_img = chosen_img
                        large_txt = f"{game_name} | Opened"
                        small_img = default_large
                        small_txt = "Protutech Cloud"
                    else:
                        large_img = default_large
                        large_txt = f"{game_name} (Opened) | Protutech Cloud"
                        small_img = None
                        small_txt = None
                else:
                    large_img = default_large
                    large_txt = "Gaming Activity | Protutech Cloud"
                    small_img = None
                    small_txt = None

            elif current_screen["name"] == "Minecraft":
                mc_img = cfg.get("minecraft_image") or game_images.get("minecraft")
                if mc_img and (mc_img.startswith("http://") or mc_img.startswith("https://")):
                    large_img = mc_img
                elif mc_img in BUILTIN_GAME_ICONS:
                    large_img = BUILTIN_GAME_ICONS[mc_img]
                else:
                    large_img = BUILTIN_GAME_ICONS.get("minecraft", default_large)

                mc_info = current_screen.get("mc_status", {})
                if mc_info.get("online"):
                    mp_name = mc_info.get("modpack_name") or "Minecraft"
                    m_loader = mc_info.get("modloader") or ""
                    m_mods = mc_info.get("mods_count", 0)
                    m_ver = mc_info.get("mc_version") or ""
                    m_act = mc_info.get("activity") or "In-Game"
                    m_launcher = mc_info.get("launcher") or "Minecraft"

                    # If modpack has an official icon/image URL, use it as the main large image!
                    pack_icon = mc_info.get("icon_url")
                    if pack_icon and (pack_icon.startswith("http://") or pack_icon.startswith("https://")):
                        large_img = pack_icon

                    if mc_info.get("is_modpack"):
                        parts = [mp_name]
                        if m_loader:
                            parts.append(m_loader)
                        elif m_ver:
                            parts.append(f"v{m_ver}")
                        if m_mods > 0:
                            parts.append(f"({m_mods} Mods)")
                        large_txt = " | ".join(parts)
                    else:
                        large_txt = f"Minecraft v{m_ver} | {m_act}" if m_ver else f"Minecraft | {m_act}"

                    if len(large_txt) > 120:
                        large_txt = large_txt[:117] + "..."

                    # When using modpack icon as large image, badge with Minecraft grass block icon!
                    if pack_icon and (pack_icon.startswith("http://") or pack_icon.startswith("https://")):
                        small_img = BUILTIN_GAME_ICONS.get("minecraft", default_large)
                    else:
                        small_img = default_large
                    small_txt = f"{m_launcher} • {m_act}"[:120]
                else:
                    large_txt = "Minecraft: Standby | Protutech Cloud"
                    small_img = default_large
                    small_txt = "Protutech Cloud"

            elif current_screen["name"] == "Network Speed":
                speed_img = cfg.get("speed_image") or game_images.get("speed") or game_images.get("cloudflare") or game_images.get("speedtest")
                if speed_img and (speed_img.startswith("http://") or speed_img.startswith("https://")):
                    large_img = speed_img
                elif speed_img in BUILTIN_GAME_ICONS:
                    large_img = BUILTIN_GAME_ICONS[speed_img]
                else:
                    large_img = BUILTIN_GAME_ICONS.get("speed", DEFAULT_SPEED_ICON)

                large_txt = "Cloudflare Speed & Latency | Protutech Cloud"
                small_img = default_large
                small_txt = "Protutech Cloud"

            elif current_screen["name"] == "Minecraft Server":
                mc_srv = current_screen.get("mc_server_data", {})
                srv_icon = mc_srv.get("icon")
                if srv_icon and (srv_icon.startswith("http://") or srv_icon.startswith("https://")):
                    large_img = srv_icon
                elif cfg.get("minecraft_server_image"):
                    large_img = cfg["minecraft_server_image"]
                else:
                    large_img = BUILTIN_GAME_ICONS.get("minecraft", DEFAULT_PROXMOX_ICON)

                if mc_srv.get("online"):
                    large_txt = f"{mc_srv.get('label', 'Minecraft Server')} | {mc_srv.get('players_online', 0)}/{mc_srv.get('players_max', 20)} Online"
                else:
                    large_txt = f"{mc_srv.get('label', 'Minecraft Server')} | Standby"
                if len(large_txt) > 120:
                    large_txt = large_txt[:117] + "..."
                small_img = default_large
                small_txt = "Protutech Cloud"

            elif current_screen["name"] == "Crypto & Stocks":
                market_icon = cfg.get("market_image") or "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/tradingview.png"
                large_img = market_icon
                large_txt = "Market Watch | BTC, ETH, SOL, NVDA, AAPL, MSFT, SPY"
                if len(large_txt) > 120:
                    large_txt = large_txt[:117] + "..."
                small_img = default_large
                small_txt = "Protutech Cloud"

            elif current_screen["name"] == "Active Games":
                active_g = current_screen.get("active_games", [])
                if active_g:
                    first_g = active_g[0]
                    chosen_g_img = resolve_game_image(first_g, cfg)
                    large_img = chosen_g_img or BUILTIN_GAME_ICONS.get("roblox", DEFAULT_STEAM_ICON)
                    g_names_all = ", ".join(g["name"] for g in active_g)
                    large_txt = f"Active: {g_names_all} | Gaming Hub"
                else:
                    large_img = default_large
                    large_txt = "Gaming Hub | Standby"
                if len(large_txt) > 120:
                    large_txt = large_txt[:117] + "..."
                small_img = default_large
                small_txt = "Protutech Cloud"

            elif current_screen["name"] == "Steam Profile":
                s_data = current_screen.get("steam_data", {})
                s_avatar = cfg.get("steam_image") or s_data.get("avatar_url")
                if s_avatar and (s_avatar.startswith("http://") or s_avatar.startswith("https://")):
                    large_img = s_avatar.replace("avatars.fastly.steamstatic.com", "avatars.steamstatic.com")
                elif s_avatar in BUILTIN_GAME_ICONS:
                    large_img = BUILTIN_GAME_ICONS[s_avatar]
                else:
                    large_img = BUILTIN_GAME_ICONS.get("steam", default_large)

                persona = s_data.get("persona", "Steam User")
                level = str(s_data.get("level", "0"))
                achs = s_data.get("achievements") or "0"
                bdgs = s_data.get("badges") or "0"
                pfg = s_data.get("perfect_games") or "0"
                hrs = s_data.get("hours") or "0"
                large_txt = f"{persona} | Lvl {level} • {s_data.get('games', '0')} Games • {achs} Achs • {bdgs} Badges • {hrs}h"
                if len(large_txt) > 120:
                    large_txt = large_txt[:117] + "..."
                small_img = default_large
                small_txt = "Protutech Cloud"

            elif current_screen["name"] in ("GitHub Repositories", "GitHub"):
                gh_img = cfg.get("github_image") or game_images.get("github")
                if gh_img and (gh_img.startswith("http://") or gh_img.startswith("https://")):
                    large_img = gh_img
                elif gh_img in BUILTIN_GAME_ICONS:
                    large_img = BUILTIN_GAME_ICONS[gh_img]
                else:
                    large_img = BUILTIN_GAME_ICONS.get("github", DEFAULT_GITHUB_ICON)

                large_txt = "GitHub Repositories | Protutech Cloud"
                small_img = default_large
                small_txt = "Protutech Cloud"

            elif current_screen["name"] == "Free Games":
                fg_img = cfg.get("free_games_image") or game_images.get("freegames") or game_images.get("dvd") or game_images.get("disc")
                if fg_img and (fg_img.startswith("http://") or fg_img.startswith("https://")):
                    large_img = fg_img
                elif fg_img in BUILTIN_GAME_ICONS:
                    large_img = BUILTIN_GAME_ICONS[fg_img]
                else:
                    large_img = BUILTIN_GAME_ICONS.get("freegames", DEFAULT_FREE_GAMES_ICON)

                fg_data = current_screen.get("free_games_data", {})
                fg_list = fg_data.get("games", []) if fg_data else []
                if fg_list:
                    titles_str = ", ".join(g["title"] for g in fg_list[:4])
                    large_txt = f"Free: {titles_str}"
                    if len(large_txt) > 120:
                        large_txt = large_txt[:117] + "..."
                else:
                    large_txt = "Free Games Tracker | GamerPower"

                small_img = default_large
                small_txt = "Protutech Cloud"

            elif current_screen.get("screen_type") == "custom":
                large_img = current_screen.get("large_image") or default_large
                large_txt = f"{current_screen['name']} | Protutech Cloud"
                small_img = default_large
                small_txt = "Protutech Cloud"

            game_start = int(current_screen.get("start_time", boot_time))
            activity_kwargs = {
                "details": current_screen["details"],
                "state": current_screen["state"],
                "large_image": large_img,
                "large_text": large_txt,
                "start": game_start
            }
            if small_img:
                activity_kwargs["small_image"] = small_img
            if small_txt:
                activity_kwargs["small_text"] = small_txt

            # Optional Dashboard Button (displays interactive button on Discord profile)
            if cfg.get("enable_dashboard_button", True):
                button_label = str(cfg.get("dashboard_button_label", "View All Screens"))[:32]
                dash_port = int(cfg.get("dashboard_port", 8989))
                user_id = str(cfg.get("user_id", "andrex")).strip().lower()
                custom_domain = cfg.get("dashboard_domain", "").strip()

                if custom_domain:
                    dash_url = f"https://{custom_domain}/?user={user_id}"
                elif cfg.get("dashboard_url"):
                    dash_url = cfg.get("dashboard_url")
                else:
                    dash_url = f"http://localhost:{dash_port}/?user={user_id}"

                activity_kwargs["buttons"] = [
                    {
                        "label": button_label,
                        "url": dash_url
                    }
                ]

            # Optional Party Badge (shows e.g. "(172 of 172)" mods or "(16 of 16)" guests)
            if current_screen["name"] == "Minecraft":
                mc_info = current_screen.get("mc_status", {})
                if mc_info.get("online") and mc_info.get("is_modpack") and mc_info.get("mods_count", 0) > 0:
                    mods_cnt = mc_info["mods_count"]
                    activity_kwargs["party_size"] = [mods_cnt, mods_cnt]
                    activity_kwargs["party_id"] = "minecraft_mods"
            elif current_screen["name"] == "Minecraft Server":
                mc_srv = current_screen.get("mc_server_data", {})
                if mc_srv.get("online"):
                    activity_kwargs["party_size"] = [mc_srv.get("players_online", 0), mc_srv.get("players_max", 20)]
                    activity_kwargs["party_id"] = "mc_server_players"
            elif current_screen["name"] == "Active Games":
                if current_screen.get("party_size"):
                    activity_kwargs["party_size"] = current_screen["party_size"]
                    activity_kwargs["party_id"] = "active_games_hub"
            elif current_screen["name"] == "Steam Profile":
                st_data = current_screen.get("steam_data", {})
                st_lvl = str(st_data.get("level", "0")).replace(',', '').strip()
                if st_lvl.isdigit() and int(st_lvl) > 0:
                    activity_kwargs["party_size"] = [int(st_lvl), int(st_lvl)]
                    activity_kwargs["party_id"] = "steam_level"
            elif (cfg.get("show_party_badge", True) 
                  and stats["total_guests"] > 0 
                  and current_screen.get("screen_type") != "game"
                  and current_screen["name"] not in ("Kryptex Miner", "Crypto Miner", "Game Activity", "Network Speed", "Steam Profile", "GitHub Repositories", "GitHub", "Free Games", "Minecraft", "Minecraft Server", "Crypto & Stocks", "Active Games")):
                activity_kwargs["party_size"] = [stats["running_guests"], stats["total_guests"]]
                activity_kwargs["party_id"] = "protutech_guests"

            # Priority Display Enforcement & Game Suppression:
            target_pid = os.getpid()

            if not cfg.get("enable_game_activity", True):
                # When gaming activity is disabled, run purely on Python's PID and suppress all games
                target_pid = os.getpid()
            else:
                # If current screen is a specific game screen, bind to that specific game's PID
                if current_screen.get("screen_type") == "game" and current_screen.get("game_info"):
                    g_pid = current_screen["game_info"].get("pid")
                    if g_pid and is_pid_alive(g_pid):
                        target_pid = g_pid
                elif detected_games:
                    chosen_pid = None
                    for target_slug in ("roblox",):
                        for g in detected_games:
                            if g.get("slug") == target_slug:
                                for p in g.get("pids", [g.get("pid")]):
                                    if is_pid_alive(p):
                                        chosen_pid = p
                                        break
                            if chosen_pid:
                                break
                        if chosen_pid:
                            break
                    if not chosen_pid:
                        for g in detected_games:
                            p = g.get("pid")
                            if p and is_pid_alive(p):
                                chosen_pid = p
                                break
                    if chosen_pid:
                        target_pid = chosen_pid

            # Actively suppress and clear all other competing game PIDs
            for p in (all_game_pids | _previously_active_pids):
                if p != target_pid and is_pid_alive(p):
                    try:
                        rpc.clear(pid=p)
                    except Exception:
                        pass

            # If target_pid is a game process, clear Python's own PID to prevent duplicate ghost activities
            if target_pid != os.getpid():
                try:
                    rpc.clear(pid=os.getpid())
                except Exception:
                    pass

            # Clear any previously tracked game PIDs that have now closed
            for p in list(_previously_active_pids):
                if p not in all_game_pids and p != target_pid:
                    try:
                        rpc.clear(pid=p)
                    except Exception:
                        pass
                    _previously_active_pids.discard(p)

            _previously_active_pids.update(all_game_pids)

            # Update Discord Rich Presence on the chosen priority PID
            rpc.update(pid=target_pid, **activity_kwargs)
            sync_stoat_status(current_screen, cfg)
            print(f"[{time.strftime('%X')}] [Screen {screen_index}/{len(screens)} - {current_screen['name']}] [PID: {target_pid}] {current_screen['details']} | {current_screen['state']}", flush=True)

        except requests.exceptions.RequestException as e:
            print(f"[{time.strftime('%X')}] [WARN] Could not reach Proxmox: {e}", flush=True)
            try:
                rpc.update(
                    details=f"{cfg.get('server_label', 'Protutech')}: Unreachable",
                    state="Retrying Proxmox VE connection...",
                    large_image=cfg.get("large_image", "protutech"),
                    large_text="Connection error",
                    start=boot_time
                )
            except Exception:
                rpc = None
        except (PipeClosed, BrokenPipeError, ConnectionResetError, OSError) as e:
            print(f"[WARN] Discord connection lost ({e}). Reconnecting...", flush=True)
            try:
                rpc.close()
            except Exception:
                pass
            rpc = None
        except Exception as e:
            print(f"[{time.strftime('%X')}] [ERROR] Unexpected: {e}", flush=True)
            err_str = str(e).lower()
            if any(k in err_str for k in ("pipe", "socket", "connect", "client", "closed", "reset", "event", "broken")):
                try:
                    if rpc:
                        rpc.close()
                except Exception:
                    pass
                rpc = None

        # 4. Exact per-screen timing: sleeps exactly (interval - elapsed) seconds
        elapsed = time.time() - cycle_start
        sleep_dur = max(0.0, interval - elapsed)
        time.sleep(sleep_dur)


if __name__ == "__main__":
    main()
