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
if sys.stdout is None:
    try:
        sys.stdout = open(os.path.join(LOG_DIR, "proxmox_rpc.log"), "a", encoding="utf-8", buffering=1)
        sys.stderr = sys.stdout
    except Exception:
        pass
elif sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True, write_through=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True, write_through=True)
    except Exception:
        pass

# Prevent multiple instances from running concurrently
_app_mutex = None
if sys.platform == "win32":
    import ctypes
    kernel32 = ctypes.windll.kernel32
    _app_mutex = kernel32.CreateMutexW(None, False, "Global\\ProxmoxDiscordRPC_Instance")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        print("[INFO] Proxmox Discord RPC is already running in the background. Exiting.", flush=True)
        sys.exit(0)

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
                info = {
                    "name": dr["name"],
                    "temp": dr["core_temperature"],
                    "power": dr["power_usage"],
                    "coin": p["coin"].upper(),
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


def main():
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

            # Screen 1: Performance & Uptime
            screens.append({
                "name": "Performance",
                "details": f"🟢 {label}: {stats['node']} | Up: {stats['uptime']}",
                "state": f"💻 CPU: {stats['cpu_pct']:.1f}% | 🧠 RAM: {stats['mem_pct']:.0f}% ({stats['mem_used']:.1f}/{stats['mem_total']:.0f}G)"
            })

            # Screen 2: Workloads & Storage
            screens.append({
                "name": "Storage & Workloads",
                "details": f"🖥️ VMs: {stats['running_vms']}/{stats['total_vms']} | 📦 Containers: {stats['running_lxcs']}/{stats['total_lxcs']}",
                "state": f"💾 Storage: {stats['storage_used_gb']:.0f}G / {stats['storage_total_tb']:.1f}TB ({stats['storage_pool']})"
            })

            # Screen 3: Cryptocurrency Mining Status (when enabled)
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
                        if gpu:
                            coin_suffix = f" ({gpu['coin']})" if gpu.get("coin") else ""
                            gpu_name = gpu["name"].replace("NVIDIA GeForce ", "")
                            parts.append(f"🎮 {gpu_name}: {gpu['hashrate']}{coin_suffix}")
                        if cpu:
                            coin_suffix = f" ({cpu['coin']})" if cpu.get("coin") else ""
                            parts.append(f"💻 CPU: {cpu['hashrate']}{coin_suffix}")

                        state = " | ".join(parts) if parts else "Mining active"
                    else:
                        details = f"⛏️ Crypto Mining: Idle{bal_str}"
                        state = "GPU & CPU mining standby"

                    screens.append({
                        "name": "Crypto Miner",
                        "details": details,
                        "state": state
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

            large_img = cfg.get("large_image", "protutech")
            if current_screen["name"] in ("Kryptex Miner", "Crypto Miner"):
                hover_text = "Protutech Cloud | Crypto Mining Rig"
            else:
                hover_text = f"Protutech Cloud | {stats['running_guests']}/{stats['total_guests']} Services Online"

            activity_kwargs = {
                "details": current_screen["details"],
                "state": current_screen["state"],
                "large_image": large_img,
                "large_text": hover_text,
                "start": boot_time
            }

            # Optional Party Badge (shows e.g. "(16 of 16)" guests)
            if cfg.get("show_party_badge", True) and stats["total_guests"] > 0 and current_screen["name"] != "Kryptex Miner":
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
