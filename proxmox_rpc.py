#!/usr/bin/env python3
"""
Proxmox VE Discord Rich Presence (RPC)
Displays live Proxmox server stats directly on your Discord user profile.
Supports multi-screen rotation, guest party badges, and storage monitoring.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import socket
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


def get_cached_proxmox_stats(cfg, force=False):
    """
    Returns cached Proxmox stats to eliminate network lag on non-Proxmox screens.
    Fetches fresh stats every PROXMOX_CACHE_TTL seconds or when force=True.
    """
    global _cached_proxmox_stats, _cached_proxmox_time
    now = time.time()
    if force or _cached_proxmox_stats is None or (now - _cached_proxmox_time) >= PROXMOX_CACHE_TTL:
        try:
            _cached_proxmox_stats = fetch_proxmox_stats(cfg)
            _cached_proxmox_time = now
        except Exception:
            if _cached_proxmox_stats is not None:
                return _cached_proxmox_stats
            raise
    return _cached_proxmox_stats


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


def fetch_minecraft_status(server_addr, pve_mc_status="Offline", show_address=False):
    """
    Strictly verifies if the actual Minecraft server is running and accepting connections.
    1. Direct SLP ping to configured server address (over TCP)
    2. Fallback to public status API (api.mcstatus.io)
    3. Fallback to local container/host LAN endpoints (192.168.0.246 / 192.168.0.2)
    4. Only returns online=True if a live Minecraft instance answers with valid status.
    5. Hides server IP/domain when show_address is False for privacy.
    """
    now = time.time()
    clean_addr = (server_addr or "minecraft.protutech.vip").strip()
    cache_key = f"{clean_addr}_{show_address}"
    if cache_key in _mc_status_cache and (now - _mc_status_time.get(cache_key, 0)) < MC_CACHE_TTL:
        return _mc_status_cache[cache_key]

    host = clean_addr
    port = 25565
    if ":" in host:
        parts = host.split(":", 1)
        host = parts[0]
        try:
            port = int(parts[1])
        except ValueError:
            port = 25565

    # Target endpoints to probe for live Minecraft SLP ping
    endpoints = [(host, port)]
    # If host is a domain, also probe local container/host endpoints as LAN backup
    if not host.replace(".", "").isdigit():
        for lan_ip in ["192.168.0.246", "192.168.0.2"]:
            if (lan_ip, port) not in endpoints:
                endpoints.append((lan_ip, port))

    # 1. Direct Minecraft SLP protocol ping
    for h, p in endpoints:
        slp_data = _ping_minecraft_slp(h, p, timeout=1.2)
        if slp_data and "error" not in slp_data and "players" in slp_data:
            players = slp_data.get("players", {})
            online_p = players.get("online", 0)
            max_p = players.get("max", 0)
            ver_raw = slp_data.get("version", {}).get("name", "")
            ver = ver_raw.replace("Requires MC ", "").split()[0] if ver_raw else ""
            ver_str = f" | v{ver}" if ver else ""

            addr_label = f"🌐 {clean_addr}" if show_address else "🎮 Protutech Cloud"
            res = {
                "online": True,
                "players_online": online_p,
                "players_max": max_p,
                "version": ver,
                "details": f"⛏️ Minecraft: Online ({online_p}/{max_p} Online)",
                "state": f"{addr_label}{ver_str}"
            }
            _mc_status_cache[cache_key] = res
            _mc_status_time[cache_key] = now
            return res

    # 2. Public API verification (api.mcstatus.io)
    try:
        api_url = f"https://api.mcstatus.io/v2/status/java/{host}:{port}" if port != 25565 else f"https://api.mcstatus.io/v2/status/java/{host}"
        resp = requests.get(api_url, timeout=2.0)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("online"):
                players = data.get("players", {})
                online_p = players.get("online", 0)
                max_p = players.get("max", 0)
                ver_name = data.get("version", {}).get("name_clean", "")
                ver_str = f" | {ver_name}" if ver_name else ""
                addr_label = f"🌐 {clean_addr}" if show_address else "🎮 Protutech Cloud"
                res = {
                    "online": True,
                    "players_online": online_p,
                    "players_max": max_p,
                    "version": ver_name,
                    "details": f"⛏️ Minecraft: Online ({online_p}/{max_p} Online)",
                    "state": f"{addr_label}{ver_str}"
                }
                _mc_status_cache[cache_key] = res
                _mc_status_time[cache_key] = now
                return res
    except Exception:
        pass

    # 3. Server is truly offline (no Minecraft process responding)
    stopped_desc = "Server Stopped" if pve_mc_status == "Online" else "Host Offline"
    state_str = f"🌐 {clean_addr} | {stopped_desc}" if show_address else f"Protutech Cloud | {stopped_desc}"
    res = {
        "online": False,
        "players_online": 0,
        "players_max": 0,
        "version": "",
        "details": "⛏️ Minecraft Server: Offline",
        "state": state_str
    }

    _mc_status_cache[cache_key] = res
    _mc_status_time[cache_key] = now
    return res


_net_stats = {
    "down_mbps": None,
    "up_mbps": None,
    "ping_ms": None,
    "last_speed_time": 0.0,
    "last_ping_time": 0.0
}
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
            if now - _net_stats["last_speed_time"] >= interval_sec or _net_stats["down_mbps"] is None:
                d, u = measure_speeds()
                with _net_lock:
                    if d is not None:
                        _net_stats["down_mbps"] = d
                    if u is not None:
                        _net_stats["up_mbps"] = u
                    _net_stats["last_speed_time"] = now

        except Exception:
            pass
        time.sleep(5)


def start_net_worker_if_needed(cfg):
    global _net_worker_started
    if cfg.get("enable_speed_screen", False) and not _net_worker_started:
        _net_worker_started = True
        t = threading.Thread(target=_net_stats_worker, daemon=True)
        t.start()


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
_game_tracker = {
    "current": None,
    "start_time": None,
    "last_seen": 0.0,
    "last_game_info": None
}


def detect_game_activity(cfg):
    """
    Detects the active game on the PC via Steam RunningAppID and running processes snapshot.
    Returns a dict with 'name', 'slug', 'steam_appid' or None.
    """
    import re

    # 1. Check Steam RunningAppID
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            running_appid, _ = winreg.QueryValueEx(key, "RunningAppID")
            steam_path, _ = winreg.QueryValueEx(key, "SteamPath")

        if running_appid and running_appid > 0:
            if running_appid in _steam_app_cache:
                return _steam_app_cache[running_appid]

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
            res = {"name": name, "slug": slug, "steam_appid": running_appid, "exe_name": None}
            _steam_app_cache[running_appid] = res
            return res
    except Exception:
        pass

    # 2. Check running processes snapshot
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
        procs = set()
        if kernel32.Process32First(hSnap, ctypes.byref(pe)):
            while True:
                procs.add(pe.szExeFile.decode("latin1", errors="ignore").lower())
                if not kernel32.Process32Next(hSnap, ctypes.byref(pe)):
                    break
        kernel32.CloseHandle(hSnap)

        # Check custom games from config
        custom_games = cfg.get("custom_games", {})
        for exe_name, c_info in custom_games.items():
            if exe_name.lower() in procs:
                if isinstance(c_info, dict):
                    name = c_info.get("name", exe_name)
                    slug = c_info.get("slug") or re.sub(r'[^a-z0-9_]', '', name.lower().replace(" ", "_"))
                    return {"name": name, "slug": slug, "steam_appid": c_info.get("steam_appid"), "exe_name": exe_name}
                name = str(c_info)
                slug = re.sub(r'[^a-z0-9_]', '', name.lower().replace(" ", "_"))
                return {"name": name, "slug": slug, "steam_appid": None, "exe_name": exe_name}

        # Check known popular games
        for exe_name, g_info in KNOWN_GAMES.items():
            if exe_name in procs:
                if isinstance(g_info, tuple):
                    name, slug = g_info
                else:
                    name = g_info
                    slug = re.sub(r'[^a-z0-9_]', '', name.lower().replace(" ", "_"))
                return {"name": name, "slug": slug, "steam_appid": None, "exe_name": exe_name}

        # Check against comprehensive Discord games database (10,000+ PC games)
        if _discord_games_db:
            for exe in procs:
                if exe in _discord_games_db:
                    g_meta = _discord_games_db[exe]
                    g_name = g_meta.get("name", exe)
                    slug = re.sub(r'[^a-z0-9_]', '', g_name.lower().replace(" ", "_"))
                    return {
                        "name": g_name,
                        "slug": slug,
                        "steam_appid": None,
                        "exe_name": exe,
                        "discord_icon": g_meta.get("icon")
                    }
    except Exception:
        pass

    return None


# Official Brand Logo CDNs
DEFAULT_PROXMOX_ICON = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/proxmox.png"
DEFAULT_KRYPTEX_ICON = "https://www.kryptex.com/static/v2/favicons/android-chrome-512x512.aba2291aca42.png"
DEFAULT_CLOUDFLARE_ICON = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/cloudflare.png"
DEFAULT_SPEED_ICON = DEFAULT_CLOUDFLARE_ICON

# Built-in official Discord CDN application icons for instant zero-latency image matching
BUILTIN_GAME_ICONS = {
    "proxmox": DEFAULT_PROXMOX_ICON,
    "kryptex": DEFAULT_KRYPTEX_ICON,
    "cloudflare": DEFAULT_CLOUDFLARE_ICON,
    "speed": DEFAULT_SPEED_ICON,
    "speedtest": DEFAULT_SPEED_ICON,
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
    global _app_mutex
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

    while True:
        # 1. Ensure Discord RPC connection
        if rpc is None:
            try:
                rpc = Presence(client_id)
                rpc.connect()
                print("[INFO] Connected to Discord RPC successfully!", flush=True)
            except DiscordNotFound:
                print("[WAIT] Discord client is not running. Retrying in 10s...", flush=True)
                time.sleep(10)
                continue
            except Exception as e:
                print(f"[WAIT] Could not connect to Discord ({e}). Retrying in 10s...", flush=True)
                time.sleep(10)
                continue

        # 2. Reload config and fetch stats
        try:
            cfg = load_config()
            is_screen_one = (screen_index % last_screen_count == 0)
            stats = get_cached_proxmox_stats(cfg, force=is_screen_one)
            label = cfg.get("server_label", "Protutech")

            # Build list of active screens
            screens = []

            # Screen 1: Proxmox Overview (Performance, Workloads & Storage)
            node_name = stats["node"]
            node_tag = f"{label}: {node_name}" if label.lower() != node_name.lower() else label
            screens.append({
                "name": "Proxmox Overview",
                "details": f"🟢 {node_tag} (Up: {stats['uptime']}) | 🖥️ {stats['running_vms']} VMs | 📦 {stats['running_lxcs']} LXCs",
                "state": f"💻 CPU: {stats['cpu_pct']:.1f}% | 🧠 RAM: {stats['mem_pct']:.0f}% | 💾 Storage: {stats['storage_used_gb']:.0f}G/{stats['storage_total_tb']:.1f}TB"
            })

            # Screen 2: Cryptocurrency Mining Status (when enabled)
            if cfg.get("enable_kryptex_screen", True):
                k_stats = fetch_kryptex_stats(cfg)
                if k_stats:
                    show_bal = cfg.get("show_kryptex_balance", False)
                    show_gpu = cfg.get("show_kryptex_gpu", True)
                    show_cpu = cfg.get("show_kryptex_cpu", True)

                    bal_str = f" | 💰 ${k_stats['balance']:.2f}" if (show_bal and k_stats.get("balance") is not None) else ""
                    if k_stats["mining"]:
                        gpu = k_stats.get("gpu") if show_gpu else None
                        cpu = k_stats.get("cpu") if show_cpu else None

                        details = f"⛏️ Crypto Mining{bal_str}"
                        parts = []
                        if gpu and gpu.get("coin_full"):
                            parts.append(f"🎮 GPU: {gpu['coin_full']}")
                        if cpu and cpu.get("coin_full"):
                            parts.append(f"💻 CPU: {cpu['coin_full']}")

                        state = " | ".join(parts) if parts else "Mining active"
                    else:
                        details = f"⛏️ Crypto Mining: Idle{bal_str}"
                        state = "GPU & CPU mining standby"

                    screens.append({
                        "name": "Crypto Miner",
                        "details": details,
                        "state": state
                    })

            # Screen 3: Current Game Activity (when enabled)
            if cfg.get("enable_game_activity", True):
                now = time.time()
                game_info = detect_game_activity(cfg)
                if game_info:
                    game = game_info["name"]
                    if _game_tracker["current"] != game:
                        _game_tracker["current"] = game
                        _game_tracker["start_time"] = now
                    _game_tracker["last_seen"] = now
                    _game_tracker["last_game_info"] = game_info
                    active_game = game_info
                elif _game_tracker["current"] and (now - _game_tracker["last_seen"] < GAME_DEBOUNCE_SECONDS):
                    # Grace period: keep game active during loading screens / server transitions
                    active_game = _game_tracker["last_game_info"]
                else:
                    _game_tracker["current"] = None
                    _game_tracker["start_time"] = None
                    _game_tracker["last_game_info"] = None
                    active_game = None

                if active_game:
                    game = active_game["name"]
                    elapsed = format_uptime(now - _game_tracker["start_time"])
                    details = f"🎮 Playing: {game}"
                    state = f"⏱️ Session: {elapsed} | Active on PC"
                else:
                    details = "🎮 Gaming: Standby"
                    state = "No game currently running"

                screens.append({
                    "name": "Game Activity",
                    "details": details,
                    "state": state,
                    "game_info": active_game
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
                        return None
                    if mbps >= 1000:
                        return f"{mbps / 1000:.2f} Gbps"
                    return f"{mbps:.0f} Mbps"

                if d_val is not None and u_val is not None:
                    speed_details = f"🚀 Internet: {format_net_speed(d_val)} ↓ | {format_net_speed(u_val)} ↑"
                elif d_val is not None:
                    speed_details = f"🚀 Internet: {format_net_speed(d_val)} ↓"
                else:
                    speed_details = "🚀 Internet: Testing Bandwidth..."

                if p_val is not None:
                    p_formatted = f"{p_val:.1f}ms" if p_val < 10 else f"{p_val:.0f}ms"
                    speed_state = f"⚡ Ping: {p_formatted} | Protutech Cloud"
                else:
                    speed_state = "⚡ Latency: Measuring | Protutech Cloud"

                screens.append({
                    "name": "Network Speed",
                    "details": speed_details,
                    "state": speed_state
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
                    "ping": "Network Speed"
                }
                target_name = alias_map.get(active_mode, active_mode)
                for s in screens:
                    if s["name"].lower() == target_name.lower() or target_name.lower() in s["name"].lower():
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

            elif current_screen["name"] == "Game Activity":
                game_info = current_screen.get("game_info")
                if game_info:
                    game_name = game_info["name"]
                    chosen_img = resolve_game_image(game_info, cfg)

                    if chosen_img:
                        large_img = chosen_img
                        large_txt = f"Playing {game_name}"
                        small_img = default_large
                        small_txt = "Protutech Cloud"
                    else:
                        large_img = default_large
                        large_txt = f"Playing {game_name} | Protutech Cloud"
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

                large_txt = "Internet Speed & Ping | Protutech Cloud"
                small_img = default_large
                small_txt = "Protutech Cloud"

            game_start = int(_game_tracker["start_time"]) if (current_screen["name"] == "Game Activity" and _game_tracker.get("start_time")) else boot_time
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

            # Optional Party Badge (shows e.g. "(16 of 16)" guests or "(2 of 20)" minecraft players)
            if current_screen["name"] == "Minecraft":
                mc_info = current_screen.get("mc_status", {})
                if mc_info.get("online") and mc_info.get("players_max", 0) > 0:
                    activity_kwargs["party_size"] = [mc_info["players_online"], mc_info["players_max"]]
                    activity_kwargs["party_id"] = "minecraft_players"
            elif cfg.get("show_party_badge", True) and stats["total_guests"] > 0 and current_screen["name"] not in ("Kryptex Miner", "Crypto Miner", "Game Activity", "Network Speed"):
                activity_kwargs["party_size"] = [stats["running_guests"], stats["total_guests"]]
                activity_kwargs["party_id"] = "protutech_guests"

            # Notice: Buttons are removed completely as requested

            rpc.update(**activity_kwargs)
            print(f"[{time.strftime('%X')}] [Screen {screen_index}/{len(screens)} - {current_screen['name']}] {current_screen['details']} | {current_screen['state']}", flush=True)

        except requests.exceptions.RequestException as e:
            print(f"[{time.strftime('%X')}] [WARN] Could not reach Proxmox: {e}", flush=True)
            try:
                rpc.update(
                    details=f"⚠️ {cfg.get('server_label', 'Protutech')}: Unreachable",
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

        time.sleep(interval)


if __name__ == "__main__":
    main()
