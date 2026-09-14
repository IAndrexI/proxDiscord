#!/usr/bin/env python3
"""
Proxmox VE Discord Rich Presence (RPC)
Displays live Proxmox server stats directly on your Discord user profile.
Supports multi-screen rotation, guest party badges, and storage monitoring.
"""

import json
import os
import sqlite3
import sys
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


def format_uptime(seconds):
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h"
    elif hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


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
_game_tracker = {"current": None, "start_time": None}


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
            res = {"name": name, "slug": slug, "steam_appid": running_appid}
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
                    return {"name": name, "slug": slug, "steam_appid": c_info.get("steam_appid")}
                name = str(c_info)
                slug = re.sub(r'[^a-z0-9_]', '', name.lower().replace(" ", "_"))
                return {"name": name, "slug": slug, "steam_appid": None}

        # Check known popular games
        for exe_name, g_info in KNOWN_GAMES.items():
            if exe_name in procs:
                if isinstance(g_info, tuple):
                    name, slug = g_info
                else:
                    name = g_info
                    slug = re.sub(r'[^a-z0-9_]', '', name.lower().replace(" ", "_"))
                return {"name": name, "slug": slug, "steam_appid": None}
    except Exception:
        pass

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
    interval = cfg.get("update_interval_seconds", 15)

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

        # 2. Fetch stats and construct screen
        try:
            stats = fetch_proxmox_stats(cfg)
            label = cfg.get("server_label", "Homelabs")

            # Build list of active screens
            screens = []

            # Screen 1: Proxmox Overview (Performance, Workloads & Storage)
            screens.append({
                "name": "Proxmox Overview",
                "details": f"🟢 {label}: {stats['node']} (Up: {stats['uptime']}) | 🖥️ {stats['running_vms']} VMs | 📦 {stats['running_lxcs']} LXCs",
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
                game_info = detect_game_activity(cfg)
                if game_info:
                    game = game_info["name"]
                    details = f"🎮 Playing: {game}"
                    if _game_tracker["current"] != game:
                        _game_tracker["current"] = game
                        _game_tracker["start_time"] = time.time()
                    elapsed = format_uptime(time.time() - _game_tracker["start_time"])
                    state = f"⏱️ Session: {elapsed} | Active on PC"
                else:
                    _game_tracker["current"] = None
                    _game_tracker["start_time"] = None
                    details = "🎮 Gaming: Standby"
                    state = "No game currently running"

                screens.append({
                    "name": "Game Activity",
                    "details": details,
                    "state": state,
                    "game_info": game_info
                })

            # Screen 4: Optional Minecraft Screen (when enabled)
            if cfg.get("enable_minecraft_screen", False):
                screens.append({
                    "name": "Minecraft",
                    "details": f"⛏️ Minecraft Server: {stats['mc_status']}",
                    "state": f"🎮 Server: {cfg.get('minecraft_server_address', 'Online')}"
                })

            # Select current screen and advance
            current_screen = screens[screen_index % len(screens)]
            screen_index = (screen_index + 1) % len(screens)

            default_large = cfg.get("large_image", "protutech")
            game_images = cfg.get("game_images", {})

            large_img = default_large
            large_txt = f"Protutech Cloud | {stats['running_guests']}/{stats['total_guests']} Services Online"
            small_img = None
            small_txt = None

            if current_screen["name"] == "Proxmox Overview":
                large_img = default_large
                large_txt = f"Protutech Cloud | {stats['running_guests']}/{stats['total_guests']} Services Online"
                small_img = None
                small_txt = None

            elif current_screen["name"] in ("Kryptex Miner", "Crypto Miner"):
                k_img = cfg.get("kryptex_image") or game_images.get("kryptex") or game_images.get("mining")
                if k_img:
                    large_img = k_img
                    large_txt = "Kryptex Mining | Protutech Cloud"
                    small_img = default_large
                    small_txt = "Protutech Cloud"
                else:
                    large_img = default_large
                    large_txt = "Protutech Cloud | Crypto Mining Rig"
                    small_img = None
                    small_txt = None

            elif current_screen["name"] == "Game Activity":
                game_info = current_screen.get("game_info")
                if game_info:
                    game_name = game_info["name"]
                    slug = game_info.get("slug", "").lower()
                    appid = game_info.get("steam_appid")

                    # Check config overrides first: by slug, exact name, or steam appid
                    chosen_img = (
                        game_images.get(slug)
                        or game_images.get(game_name)
                        or (game_images.get(str(appid)) if appid else None)
                    )

                    # For Steam games without custom override, auto-fetch official Steam header
                    if not chosen_img and appid:
                        chosen_img = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"

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
                mc_img = game_images.get("minecraft") or cfg.get("minecraft_image")
                if mc_img:
                    large_img = mc_img
                    large_txt = "Minecraft Server | Protutech Cloud"
                    small_img = default_large
                    small_txt = "Protutech Cloud"
                else:
                    large_img = default_large
                    large_txt = "Protutech Cloud | Minecraft"

            activity_kwargs = {
                "details": current_screen["details"],
                "state": current_screen["state"],
                "large_image": large_img,
                "large_text": large_txt,
                "start": boot_time
            }
            if small_img:
                activity_kwargs["small_image"] = small_img
            if small_txt:
                activity_kwargs["small_text"] = small_txt

            # Optional Party Badge (shows e.g. "(16 of 16)" guests)
            if cfg.get("show_party_badge", True) and stats["total_guests"] > 0 and current_screen["name"] not in ("Kryptex Miner", "Crypto Miner", "Game Activity"):
                activity_kwargs["party_size"] = [stats["running_guests"], stats["total_guests"]]
                activity_kwargs["party_id"] = "protutech_guests"

            # Notice: Buttons are removed completely as requested

            rpc.update(**activity_kwargs)
            print(f"[{time.strftime('%X')}] [Screen {screen_index}/{len(screens)} - {current_screen['name']}] {current_screen['details']} | {current_screen['state']}", flush=True)

        except requests.exceptions.RequestException as e:
            print(f"[{time.strftime('%X')}] [WARN] Could not reach Proxmox: {e}", flush=True)
            try:
                rpc.update(
                    details=f"⚠️ {cfg.get('server_label', 'Homelabs')}: Unreachable",
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
