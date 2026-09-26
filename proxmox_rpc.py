#!/usr/bin/env python3
"""
Proxmox VE Discord Rich Presence (RPC)
Displays live Proxmox server stats directly on your Discord user profile.
Supports multi-screen rotation, guest party badges, and storage monitoring.
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import socket
import socketserver
import http.server
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


def get_cached_proxmox_stats(cfg, force=False):
    """
    Returns cached Proxmox stats instantly from memory without blocking the rotation loop.
    A dedicated background daemon thread keeps the metrics fresh every 10 seconds.
    """
    global _pve_worker_started
    if not _pve_worker_started:
        _pve_worker_started = True
        t = threading.Thread(target=_pve_stats_worker, daemon=True)
        t.start()
    with _pve_lock:
        if _cached_proxmox_stats is not None:
            return _cached_proxmox_stats
    return fetch_proxmox_stats(cfg)


def fetch_proxmox_stats(cfg):
    host = cfg["proxmox_host"].rstrip("/")
    node = cfg["proxmox_node"]
    headers = {
        "Authorization": f"PVEAPIToken={cfg['proxmox_token_id']}={cfg['proxmox_token_secret']}"
    }

    # 1. Fetch Cluster Node Overview (for smoothed cluster CPU & memory)
    nodes_url = f"{host}/api2/json/nodes"
    nodes_res = requests.get(nodes_url, headers=headers, verify=False, timeout=8)
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


_mc_status_cache = {}
_mc_status_time = {}
MC_CACHE_TTL = 15.0  # Cache Minecraft status for 15s to keep rotations snappy


def _ping_minecraft_slp(host, port=25565, timeout=2.0):
    """
    Pure Python implementation of Minecraft Server List Ping (SLP) protocol.
    Directly handshakes over TCP socket to retrieve live players, MOTD, and version.
    """
    import struct

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))

        host_bytes = host.encode("utf-8")

        def pack_varint(val):
            total = b""
            while True:
                byte = val & 0x7F
                val >>= 7
                if val:
                    total += bytes([byte | 0x80])
                else:
                    total += bytes([byte])
                    break
            return total

        def unpack_varint(sock):
            val = 0
            shift = 0
            while True:
                b = sock.recv(1)
                if not b:
                    return 0
                byte = b[0]
                val |= (byte & 0x7F) << shift
                if not (byte & 0x80):
                    break
                shift += 7
            return val

        # Handshake packet: packet ID 0x00, protocol version 47, host, port, next state 1 (status)
        data = b"\x00" + pack_varint(47) + pack_varint(len(host_bytes)) + host_bytes + struct.pack(">H", port) + pack_varint(1)
        s.sendall(pack_varint(len(data)) + data)

        # Status request packet: packet ID 0x00
        req = b"\x00"
        s.sendall(pack_varint(len(req)) + req)

        # Read response packet length and packet ID
        _ = unpack_varint(s)
        _ = unpack_varint(s)
        str_len = unpack_varint(s)

        resp_data = b""
        while len(resp_data) < str_len:
            chunk = s.recv(min(str_len - len(resp_data), 4096))
            if not chunk:
                break
            resp_data += chunk
        s.close()

        return json.loads(resp_data.decode("utf-8", errors="ignore"))
    except Exception as e:
        return {"error": str(e)}


_mc_worker_started = False
_mc_lock = threading.Lock()
_cached_mc_status = None


def _query_minecraft_status(server_addr, pve_mc_status="Offline", show_address=False):
    """
    Directly queries the Minecraft server (SLP protocol and fallback API).
    """
    clean_addr = (server_addr or "minecraft.protutech.vip").strip()
    host = clean_addr
    port = 25565
    if ":" in host:
        parts = host.split(":", 1)
        host = parts[0]
        try:
            port = int(parts[1])
        except ValueError:
            port = 25565

    endpoints = [(host, port)]
    if not host.replace(".", "").isdigit():
        for lan_ip in ["192.168.0.246", "192.168.0.2"]:
            if (lan_ip, port) not in endpoints:
                endpoints.append((lan_ip, port))

    # 1. Direct Minecraft SLP protocol ping
    for h, p in endpoints:
        slp_data = _ping_minecraft_slp(h, p, timeout=1.0)
        if slp_data and "error" not in slp_data and "players" in slp_data:
            players = slp_data.get("players", {})
            online_p = players.get("online", 0)
            max_p = players.get("max", 0)
            ver_raw = slp_data.get("version", {}).get("name", "")
            ver = ver_raw.replace("Requires MC ", "").split()[0] if ver_raw else ""
            ver_str = f" | v{ver}" if ver else ""

            addr_label = clean_addr if show_address else "Protutech Cloud"
            return {
                "online": True,
                "players_online": online_p,
                "players_max": max_p,
                "version": ver,
                "details": f"Minecraft Server: Online ({online_p}/{max_p})",
                "state": f"{addr_label}{ver_str}"
            }

    # 2. Public API verification (api.mcstatus.io)
    try:
        api_url = f"https://api.mcstatus.io/v2/status/java/{host}:{port}" if port != 25565 else f"https://api.mcstatus.io/v2/status/java/{host}"
        resp = requests.get(api_url, timeout=1.5)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("online"):
                players = data.get("players", {})
                online_p = players.get("online", 0)
                max_p = players.get("max", 0)
                ver_name = data.get("version", {}).get("name_clean", "")
                ver_str = f" | {ver_name}" if ver_name else ""
                addr_label = clean_addr if show_address else "Protutech Cloud"
                return {
                    "online": True,
                    "players_online": online_p,
                    "players_max": max_p,
                    "version": ver_name,
                    "details": f"Minecraft Server: Online ({online_p}/{max_p})",
                    "state": f"{addr_label}{ver_str}"
                }
    except Exception:
        pass

    # 3. Server offline
    stopped_desc = "Server Stopped" if pve_mc_status == "Online" else "Host Offline"
    state_str = f"{clean_addr} | {stopped_desc}" if show_address else f"Protutech Cloud | {stopped_desc}"
    return {
        "online": False,
        "players_online": 0,
        "players_max": 0,
        "version": "",
        "details": "Minecraft Server: Offline",
        "state": state_str
    }


def _mc_status_worker():
    while True:
        try:
            cfg = load_config()
            if cfg.get("enable_minecraft_screen", False):
                mc_addr = cfg.get("minecraft_server_address", "minecraft.protutech.vip")
                show_mc_addr = cfg.get("show_minecraft_address", False)
                res = _query_minecraft_status(mc_addr, show_address=show_mc_addr)
                with _mc_lock:
                    global _cached_mc_status
                    _cached_mc_status = res
        except Exception:
            pass
        time.sleep(15)


def fetch_minecraft_status(server_addr, pve_mc_status="Offline", show_address=False):
    """
    Returns Minecraft status instantly from memory without stalling rotation cycles.
    A dedicated background daemon thread updates the status every 15 seconds.
    """
    global _mc_worker_started
    if not _mc_worker_started:
        _mc_worker_started = True
        t = threading.Thread(target=_mc_status_worker, daemon=True)
        t.start()
    with _mc_lock:
        if _cached_mc_status is not None:
            return _cached_mc_status
    return _query_minecraft_status(server_addr, pve_mc_status=pve_mc_status, show_address=show_address)


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
    Fetch public Steam profile details: avatar, persona name, level, games count, and items count.
    """
    sid = str(steam_id).strip() if steam_id else get_local_steam_id64()
    if not sid:
        return None

    persona = "Steam User"
    avatar_url = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/steam.png"
    level = "0"
    games_count = "0"
    items_count = "0"

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

            a_elem = root.find("avatarFull")
            if a_elem is None:
                a_elem = root.find("avatarMedium")
            if a_elem is None:
                a_elem = root.find("avatarIcon")
            if a_elem is not None and a_elem.text:
                avatar_url = a_elem.text.replace("avatars.fastly.steamstatic.com", "avatars.steamstatic.com")
    except Exception:
        pass

    # 2. HTML endpoint for stats
    try:
        profile_url = f"https://steamcommunity.com/profiles/{sid}/"
        resp = requests.get(profile_url, headers=headers, timeout=5.0)
        if resp.status_code == 200:
            html = resp.text
            lvl_m = re.search(r'friendPlayerLevelNum">(\d+)</span>', html)
            if lvl_m:
                level = lvl_m.group(1)

            gm = re.search(r'href="[^"]*/games[/?][^"]*".*?<span class="profile_count_link_total">\s*([\d,]+)\s*</span>', html, re.DOTALL | re.IGNORECASE)
            if not gm:
                gm = re.search(r'<div class="value">\s*([\d,]+)\s*</div>\s*<div class="label">\s*Games\s*</div>', html, re.DOTALL | re.IGNORECASE)
            if not gm:
                gm = re.search(r'Games.*?<span class="profile_count_link_total">\s*([\d,]+)\s*</span>', html, re.DOTALL | re.IGNORECASE)
            if gm:
                games_count = gm.group(1).strip()

            itm = re.search(r'<div class="value">\s*([\d,]+)\s*</div>\s*<div class="label">\s*Items Owned\s*</div>', html, re.DOTALL | re.IGNORECASE)
            if not itm:
                itm = re.search(r'href="[^"]*/inventory[/?][^"]*".*?<span class="profile_count_link_total">\s*([\d,]+)\s*</span>', html, re.DOTALL | re.IGNORECASE)
            if itm:
                items_count = itm.group(1).strip()
    except Exception:
        pass

    return {
        "steam_id": sid,
        "persona": persona,
        "avatar_url": avatar_url,
        "level": level,
        "games": games_count,
        "items": items_count,
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
    with _steam_lock:
        if _cached_steam_stats is not None:
            return _cached_steam_stats

    cached = load_steam_cache()
    if cached:
        with _steam_lock:
            _cached_steam_stats = cached
        return cached

    sid = cfg.get("steam_id") or None
    fresh = fetch_steam_profile(sid)
    if fresh:
        with _steam_lock:
            _cached_steam_stats = fresh
        save_steam_cache(fresh)
        return fresh
    return None


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
    Fetches active 100% free promotional PC games from:
    1. Epic Games Store Weekly Free Games API
    2. GamerPower PC Giveaways API (Steam & Epic)
    Deduplicates and normalizes game titles.
    """
    games = []
    seen_titles = set()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    # 1. Epic Games Store Official Promotions API
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
                                games.append({"title": clean_t, "platform": "Epic Games"})
    except Exception:
        pass

    # 2. GamerPower Giveaways API (Steam and additional PC promotions)
    try:
        url = "https://www.gamerpower.com/api/giveaways?type=game&platform=pc"
        resp = requests.get(url, headers=headers, timeout=8.0)
        if resp.status_code == 200:
            for g in resp.json():
                platforms = g.get("platforms", "")
                if "Steam" in platforms or "Epic Games" in platforms:
                    raw_title = g.get("title", "")
                    clean_t = raw_title.replace(" Giveaway", "").replace(" (Epic Games)", "").replace(" (Steam)", "").strip()
                    norm = re.sub(r'[^a-z0-9]', '', clean_t.lower())
                    if norm and norm not in seen_titles:
                        seen_titles.add(norm)
                        plat = "Steam" if "Steam" in platforms else "Epic Games"
                        games.append({"title": clean_t, "platform": plat})
    except Exception:
        pass

    return {
        "games": games,
        "count": len(games),
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


# Web Dashboard Server Integration
_dashboard_state = {
    "screens": [],
    "current_screen_name": "",
    "screen_index": 0,
    "last_updated": 0
}
_dashboard_lock = threading.Lock()
_dashboard_server_started = False
DASHBOARD_HTML_PATH = os.path.join(LOG_DIR, "dashboard.html")


class DashboardRequestHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_path = self.path.split("?")[0]
        if parsed_path in ("/", "/index.html"):
            content = b""
            if os.path.exists(DASHBOARD_HTML_PATH):
                try:
                    with open(DASHBOARD_HTML_PATH, "rb") as f:
                        content = f.read()
                except Exception:
                    pass
            if not content:
                content = b"<!DOCTYPE html><html><body><h1>Protutech Cloud Dashboard</h1><p>Dashboard HTML not found.</p></body></html>"

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        elif parsed_path in ("/api/stats", "/api/screens"):
            with _dashboard_lock:
                payload = json.dumps(_dashboard_state).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        else:
            self.send_response(404)
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

    def add_game(name, slug, steam_appid=None, exe_name=None, discord_icon=None, pid=None, pids=None):
        if name and name.lower() not in seen_names:
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

        # Check custom games from config
        custom_games = cfg.get("custom_games", {})
        for exe_name, c_info in custom_games.items():
            ename = exe_name.lower()
            if ename in proc_pids:
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
        if _discord_games_db:
            for ename in proc_pids:
                if ename in _discord_games_db:
                    gpids = proc_pids[ename]
                    all_game_pids.update(gpids)
                    if len(detected) < max_games:
                        g_meta = _discord_games_db[ename]
                        g_name = g_meta.get("name", ename)
                        slug = re.sub(r'[^a-z0-9_]', '', g_name.lower().replace(" ", "_"))
                        add_game(g_name, slug, exe_name=ename, discord_icon=g_meta.get("icon"), pid=gpids[0], pids=gpids)

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
DEFAULT_GITHUB_ICON = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/github.png"
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
    "freegames": DEFAULT_EPIC_GAMES_ICON,
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

    cfg = load_config()
    client_id = cfg.get("discord_client_id", "1548928413337788486")
    interval = cfg.get("update_interval_seconds", 6)

    # Start web dashboard server if enabled
    if cfg.get("enable_dashboard_button", True):
        start_dashboard_server_if_needed(cfg)

    print("=" * 60, flush=True)
    print("  Proxmox VE Discord Rich Presence (RPC) - Rotating Mode", flush=True)
    print(f"  App ID:    {client_id}", flush=True)
    print(f"  Node:      {cfg.get('proxmox_node')}", flush=True)
    print(f"  Target:    {cfg.get('proxmox_host')}", flush=True)
    print(f"  Badges:    {'Enabled' if cfg.get('show_party_badge', True) else 'Disabled'}", flush=True)
    print(f"  Kryptex:   {'Enabled' if cfg.get('enable_kryptex_screen', True) else 'Disabled'}", flush=True)
    print(f"  Gaming:    {'Enabled' if cfg.get('enable_game_activity', True) else 'Disabled'}", flush=True)
    print("=" * 60, flush=True)

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
                rpc = Presence(client_id)
                rpc.connect()
                print("[INFO] Connected to Discord RPC successfully!", flush=True)
                next_tick = time.time()
            except DiscordNotFound:
                print("[WAIT] Discord client is not running. Retrying in 10s...", flush=True)
                time.sleep(10)
                next_tick = time.time()
                continue
            except Exception as e:
                print(f"[WAIT] Could not connect to Discord ({e}). Retrying in 10s...", flush=True)
                time.sleep(10)
                next_tick = time.time()
                continue

        # 2. Reload config and fetch stats
        cycle_start = time.time()
        try:
            cfg = load_config()
            interval = float(cfg.get("update_interval_seconds", 6))

            # Check game activity & sessions
            now = time.time()
            active_games = []
            all_game_pids = set()
            if cfg.get("enable_game_activity", True):
                active_games, all_game_pids = detect_active_games(cfg, max_games=3, return_pids=True)

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

            stats = get_cached_proxmox_stats(cfg)
            label = cfg.get("server_label", "Protutech")

            # Build list of active screens
            screens = []

            # Screen 1: Proxmox Overview (Performance, Workloads & Storage)
            node_name = stats["node"]
            node_tag = f"{label}: {node_name}" if label.lower() != node_name.lower() else label
            screens.append({
                "name": "Proxmox Overview",
                "details": f"{node_tag} (Up: {stats['uptime']}) | {stats['running_vms']} VMs | {stats['running_lxcs']} LXCs",
                "state": f"CPU: {stats['cpu_pct']:.1f}% | RAM: {stats['mem_pct']:.0f}% | Storage: {stats['storage_used_gb']:.0f}G/{stats['storage_total_tb']:.1f}TB"
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

            # Screen 3: Current Game Activity (Supports up to 3 separate screens for active games)
            if cfg.get("enable_game_activity", True):
                if _game_sessions:
                    first_game = list(_game_sessions.values())[0]
                    _game_tracker["current"] = first_game["game_info"]["name"]
                    _game_tracker["start_time"] = first_game["start_time"]

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
                    screens.append({
                        "name": "Game Activity",
                        "screen_type": "game",
                        "details": "Gaming: Standby",
                        "state": "No game currently open",
                        "game_info": None,
                        "start_time": boot_time
                    })

            # Screen 4: Optional Minecraft Screen (when enabled)
            if cfg.get("enable_minecraft_screen", False):
                mc_addr = cfg.get("minecraft_server_address", "minecraft.protutech.vip")
                show_mc_addr = cfg.get("show_minecraft_address", False)
                mc_status = fetch_minecraft_status(mc_addr, pve_mc_status=stats.get("mc_status", "Offline"), show_address=show_mc_addr)
                screens.append({
                    "name": "Minecraft",
                    "details": mc_status["details"],
                    "state": mc_status["state"],
                    "mc_status": mc_status
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

                # Always show past / cached speeds; never show "Testing Bandwidth..."
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

            # Screen 6: Steam Profile (when enabled)
            if cfg.get("enable_steam_screen", True):
                start_steam_worker_if_needed(cfg)
                steam_data = get_cached_steam_stats(cfg)
                if steam_data:
                    screens.append({
                        "name": "Steam Profile",
                        "screen_type": "steam",
                        "details": f"Steam: {steam_data['persona']} | Level {steam_data['level']}",
                        "state": f"{steam_data['games']} Games | {steam_data['items']} Items",
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
                    fg_state = f"{fg_count} Claimable Now | Epic & Steam"
                else:
                    fg_details = "Free PC Games: None Active"
                    fg_state = "Checking Epic & Steam | Protutech Cloud"

                screens.append({
                    "name": "Free Games",
                    "screen_type": "free_games",
                    "details": fg_details,
                    "state": fg_state,
                    "free_games_data": fg_stats
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
                    "games": "Free Games"
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
                                s_icon = DEFAULT_EPIC_GAMES_ICON
                            elif s_name == "Steam Profile":
                                s_icon = s.get("steam_data", {}).get("avatar_url") if s.get("steam_data") else default_large
                            elif s_name == "Minecraft":
                                s_icon = BUILTIN_GAME_ICONS.get("minecraft", DEFAULT_PROXMOX_ICON)
                            elif s_type == "game":
                                s_icon = resolve_game_image(s.get("game_info"), cfg) or default_large
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
                            "stats": stats if s.get("name") == "Proxmox Overview" else None,
                            "k_stats": k_stats if s.get("name") == "Crypto Miner" else None,
                            "net_stats": _net_stats if s.get("name") == "Network Speed" else None,
                            "steam_data": s.get("steam_data"),
                            "github_stats": s.get("github_stats"),
                            "free_games_data": s.get("free_games_data"),
                            "game_info": s.get("game_info")
                        })

                    with _dashboard_lock:
                        _dashboard_state.clear()
                        _dashboard_state.update({
                            "screens": dash_screen_list,
                            "current_screen_name": current_screen.get("name", ""),
                            "screen_index": screen_index,
                            "total_screens": len(screens),
                            "last_updated": time.time()
                        })
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

                mc_addr = cfg.get("minecraft_server_address", "minecraft.protutech.vip")
                show_mc_addr = cfg.get("show_minecraft_address", False)
                mc_info = current_screen.get("mc_status", {})
                suffix = f" | {mc_addr}" if show_mc_addr else " | Protutech Cloud"
                if mc_info.get("online"):
                    large_txt = f"Minecraft: Online{suffix}"
                else:
                    large_txt = f"Minecraft: Offline{suffix}"
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
                level = s_data.get("level", "0")
                large_txt = f"{persona} | Level {level}"
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
                fg_img = cfg.get("free_games_image") or game_images.get("freegames") or game_images.get("epic")
                if fg_img and (fg_img.startswith("http://") or fg_img.startswith("https://")):
                    large_img = fg_img
                elif fg_img in BUILTIN_GAME_ICONS:
                    large_img = BUILTIN_GAME_ICONS[fg_img]
                else:
                    large_img = BUILTIN_GAME_ICONS.get("freegames", DEFAULT_EPIC_GAMES_ICON)

                fg_data = current_screen.get("free_games_data", {})
                fg_list = fg_data.get("games", []) if fg_data else []
                if fg_list:
                    titles_str = ", ".join(g["title"] for g in fg_list[:4])
                    large_txt = f"Free: {titles_str}"
                    if len(large_txt) > 120:
                        large_txt = large_txt[:117] + "..."
                else:
                    large_txt = "Free PC Games | Epic & Steam"

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
                dash_url = cfg.get("dashboard_url", f"http://localhost:{dash_port}")
                activity_kwargs["buttons"] = [
                    {
                        "label": button_label,
                        "url": dash_url
                    }
                ]

            # Optional Party Badge (shows e.g. "(16 of 16)" guests or "(2 of 20)" minecraft players)
            if current_screen["name"] == "Minecraft":
                mc_info = current_screen.get("mc_status", {})
                if mc_info.get("online") and mc_info.get("players_max", 0) > 0:
                    activity_kwargs["party_size"] = [mc_info["players_online"], mc_info["players_max"]]
                    activity_kwargs["party_id"] = "minecraft_players"
            elif (cfg.get("show_party_badge", True) 
                  and stats["total_guests"] > 0 
                  and current_screen.get("screen_type") != "game"
                  and current_screen["name"] not in ("Kryptex Miner", "Crypto Miner", "Game Activity", "Network Speed", "Steam Profile", "GitHub Repositories", "GitHub", "Free Games")):
                activity_kwargs["party_size"] = [stats["running_guests"], stats["total_guests"]]
                activity_kwargs["party_id"] = "protutech_guests"

            # Priority Display Enforcement:
            # Prevent any running game from appearing on top of Protutech!
            # If any game is active, bind Protutech's Rich Presence directly to the active game's PID.
            # This turns the game's Discord presence into Protutech, ensuring Protutech is ALWAYS the main display.
            target_pid = os.getpid()

            # If current screen is a specific game screen, bind to that specific game's PID
            if current_screen.get("screen_type") == "game" and current_screen.get("game_info"):
                g_pid = current_screen["game_info"].get("pid")
                if g_pid and is_pid_alive(g_pid):
                    target_pid = g_pid
            elif active_games:
                # On non-game screens (Proxmox, Crypto, Speed, Steam, GitHub, Free Games),
                # bind to the primary active game PID so Discord displays Protutech as the active game
                for g in active_games:
                    g_pid = g.get("pid")
                    if g_pid and is_pid_alive(g_pid):
                        target_pid = g_pid
                        break

            # Actively suppress and clear all other competing game PIDs
            for p in all_game_pids:
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
            if "pipe" in str(e).lower() or "socket" in str(e).lower():
                rpc = None

        # 4. Exact per-screen timing: sleeps exactly (interval - elapsed) seconds
        elapsed = time.time() - cycle_start
        sleep_dur = max(0.0, interval - elapsed)
        time.sleep(sleep_dur)


if __name__ == "__main__":
    main()
