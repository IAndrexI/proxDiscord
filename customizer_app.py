#!/usr/bin/env python3
"""
DiscordRPC Studio — Desktop Customizer & Stoat Chat Sync Tool
Allows locally or remotely customizing Discord Rich Presence widgets,
screen rotations, game detection, and self-hosted Stoat Chat synchronization.
"""

import os
import sys
import json
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import urllib.parse
import urllib.request
import requests

APP_TITLE = "DiscordRPC Studio"
DEFAULT_HOST_KEY = "andrex-host-2026"
DEFAULT_LOCAL_URL = "http://localhost:8989"
LOCAL_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# Discord / Obsidian Dark Theme Palette
BG_DARK = "#111214"
BG_CARD = "#1e1f22"
BG_INPUT = "#2b2d31"
TEXT_MAIN = "#f2f3f5"
TEXT_MUTED = "#949ba4"
ACCENT_BLUE = "#5865f2"
ACCENT_GREEN = "#23a55a"
ACCENT_RED = "#f23f43"
ACCENT_YELLOW = "#f0b232"
BORDER_COLOR = "#35373c"


class CustomizerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE} — Widget & Stoat Customizer")
        self.geometry("820x680")
        self.minsize(760, 600)
        self.configure(bg=BG_DARK)

        # Try to set icon
        ico_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "app_icon.ico")
        if os.path.exists(ico_path):
            try:
                self.iconbitmap(ico_path)
            except Exception:
                pass

        self.server_url = tk.StringVar(value=DEFAULT_LOCAL_URL)
        self.host_key = tk.StringVar(value=DEFAULT_HOST_KEY)
        self.connection_status = tk.StringVar(value="Not Connected")
        self.authenticated = tk.BooleanVar(value=False)

        # Widget toggles
        self.enable_proxmox = tk.BooleanVar(value=True)
        self.enable_storage = tk.BooleanVar(value=True)
        self.enable_kryptex = tk.BooleanVar(value=True)
        self.enable_minecraft = tk.BooleanVar(value=True)
        self.enable_mc_server = tk.BooleanVar(value=True)
        self.enable_market = tk.BooleanVar(value=True)
        self.enable_games_hub = tk.BooleanVar(value=True)
        self.enable_game_activity = tk.BooleanVar(value=True)
        self.game_alternate_rotation = tk.BooleanVar(value=True)
        self.enable_speed = tk.BooleanVar(value=True)
        self.enable_steam = tk.BooleanVar(value=True)
        self.enable_github = tk.BooleanVar(value=True)
        self.enable_freegames = tk.BooleanVar(value=True)

        # Stoat Chat variables
        self.enable_stoat = tk.BooleanVar(value=False)
        self.stoat_url = tk.StringVar(value="https://api.stoat.chat")
        self.stoat_token = tk.StringVar(value="")
        self.stoat_auth_mode = tk.StringVar(value="user")
        self.stoat_presence = tk.StringVar(value="Online")
        self.stoat_template = tk.StringVar(value="{details} • {state}")
        self.stoat_test_status = tk.StringVar(value="")

        # Advanced rotation
        self.active_screen = tk.StringVar(value="rotate")
        self.update_interval = tk.StringVar(value="6")
        self.speedtest_interval = tk.StringVar(value="30")
        self.server_label = tk.StringVar(value="Protutech")
        self.mc_server_addr = tk.StringVar(value="minecraft.protutech.vip")
        self.mc_server_port = tk.StringVar(value="25565")
        self.market_cryptos = tk.StringVar(value="BTC, ETH, SOL")
        self.market_stocks = tk.StringVar(value="NVDA, AAPL, MSFT, SPY")

        self.detected_games = []
        self.disabled_games = []

        self._setup_styles()
        self._build_ui()
        self._auto_connect_local()

    def _setup_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure("TNotebook", background=BG_DARK, borderwidth=0)
        style.configure("TNotebook.Tab", background=BG_CARD, foreground=TEXT_MUTED, padding=[16, 8], font=("Segoe UI", 10, "bold"), borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", ACCENT_BLUE)], foreground=[("selected", "#ffffff")])

        style.configure("TFrame", background=BG_DARK)
        style.configure("Card.TFrame", background=BG_CARD, relief="flat")
        style.configure("TLabel", background=BG_CARD, foreground=TEXT_MAIN, font=("Segoe UI", 9))
        style.configure("Header.TLabel", background=BG_DARK, foreground=TEXT_MAIN, font=("Segoe UI", 14, "bold"))
        style.configure("Sub.TLabel", background=BG_DARK, foreground=TEXT_MUTED, font=("Segoe UI", 9))
        style.configure("Section.TLabel", background=BG_CARD, foreground=TEXT_MAIN, font=("Segoe UI", 11, "bold"))
        style.configure("Muted.TLabel", background=BG_CARD, foreground=TEXT_MUTED, font=("Segoe UI", 8))

        style.configure("TCheckbutton", background=BG_CARD, foreground=TEXT_MAIN, font=("Segoe UI", 9))
        style.map("TCheckbutton", background=[("active", BG_CARD)], foreground=[("active", "#ffffff")])

        style.configure("Primary.TButton", background=ACCENT_BLUE, foreground="#ffffff", font=("Segoe UI", 9, "bold"), borderwidth=0, padding=[12, 6])
        style.map("Primary.TButton", background=[("active", "#4752c4")])

        style.configure("Success.TButton", background=ACCENT_GREEN, foreground="#ffffff", font=("Segoe UI", 9, "bold"), borderwidth=0, padding=[12, 6])
        style.map("Success.TButton", background=[("active", "#1f8b4d")])

        style.configure("Secondary.TButton", background=BG_INPUT, foreground=TEXT_MAIN, font=("Segoe UI", 9), borderwidth=0, padding=[10, 5])
        style.map("Secondary.TButton", background=[("active", BORDER_COLOR)])

    def _build_ui(self):
        # Top Header Bar
        hdr = tk.Frame(self, bg=BG_DARK, padx=16, pady=12)
        hdr.pack(fill="x")

        title_lbl = tk.Label(hdr, text="⚡ DiscordRPC Studio", font=("Segoe UI", 15, "bold"), fg="#ffffff", bg=BG_DARK)
        title_lbl.pack(side="left")

        sub_lbl = tk.Label(hdr, text="• Local & Self-Hosted Stoat Chat Customizer", font=("Segoe UI", 10), fg=TEXT_MUTED, bg=BG_DARK)
        sub_lbl.pack(side="left", padx=8, pady=3)

        self.status_pill = tk.Label(hdr, textvariable=self.connection_status, font=("Segoe UI", 9, "bold"), bg=BG_INPUT, fg=TEXT_MUTED, padx=10, pady=4, relief="flat")
        self.status_pill.pack(side="right")

        # Tabbed Notebook
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=16, pady=6)

        tab_conn = ttk.Frame(notebook)
        tab_widgets = ttk.Frame(notebook)
        tab_stoat = ttk.Frame(notebook)
        tab_games = ttk.Frame(notebook)
        tab_settings = ttk.Frame(notebook)

        notebook.add(tab_conn, text="🌐 Connection")
        notebook.add(tab_widgets, text="🧩 Widgets")
        notebook.add(tab_stoat, text="💬 Self-Hosted Stoat")
        notebook.add(tab_games, text="🎮 Games")
        notebook.add(tab_settings, text="⚙️ Rotation & System")

        self._build_tab_connection(tab_conn)
        self._build_tab_widgets(tab_widgets)
        self._build_tab_stoat(tab_stoat)
        self._build_tab_games(tab_games)
        self._build_tab_settings(tab_settings)

        # Bottom Action Bar
        btm = tk.Frame(self, bg=BG_DARK, padx=16, pady=10)
        btm.pack(fill="x")

        save_btn = ttk.Button(btm, text="💾 Save & Apply to Discord", style="Success.TButton", command=self.save_and_apply)
        save_btn.pack(side="right", padx=6)

        refresh_btn = ttk.Button(btm, text="🔄 Reload from Daemon", style="Secondary.TButton", command=self.load_from_server)
        refresh_btn.pack(side="right", padx=6)

        self.footer_msg = tk.Label(btm, text="Ready", font=("Segoe UI", 9), fg=TEXT_MUTED, bg=BG_DARK)
        self.footer_msg.pack(side="left")

    def _build_tab_connection(self, parent):
        card = ttk.Frame(parent, style="Card.TFrame", padding=16)
        card.pack(fill="x", padx=4, pady=8)

        ttk.Label(card, text="DiscordRPC Server Endpoint", style="Section.TLabel").pack(anchor="w")
        ttk.Label(card, text="Connect to your local daemon or remote public DiscordRPC instance", style="Muted.TLabel").pack(anchor="w", pady=(0, 8))

        row1 = tk.Frame(card, bg=BG_CARD)
        row1.pack(fill="x", pady=4)
        ttk.Label(row1, text="Server URL:").pack(side="left", padx=(0, 8))
        e_url = tk.Entry(row1, textvariable=self.server_url, bg=BG_INPUT, fg=TEXT_MAIN, insertbackground=TEXT_MAIN, relief="flat", font=("Segoe UI", 9), width=35)
        e_url.pack(side="left", fill="x", expand=True, padx=4)

        row2 = tk.Frame(card, bg=BG_CARD)
        row2.pack(fill="x", pady=6)
        ttk.Label(row2, text="Host Key:  ").pack(side="left", padx=(0, 8))
        e_key = tk.Entry(row2, textvariable=self.host_key, show="•", bg=BG_INPUT, fg=TEXT_MAIN, insertbackground=TEXT_MAIN, relief="flat", font=("Segoe UI", 9), width=25)
        e_key.pack(side="left", padx=4)

        btn_conn = ttk.Button(row2, text="Connect & Auth", style="Primary.TButton", command=self.connect_server)
        btn_conn.pack(side="left", padx=8)

        # Status & Live Info Card
        info_card = ttk.Frame(parent, style="Card.TFrame", padding=16)
        info_card.pack(fill="both", expand=True, padx=4, pady=8)

        ttk.Label(info_card, text="Live Server Health & Active Screen", style="Section.TLabel").pack(anchor="w", pady=(0, 8))
        self.live_info_text = tk.Text(info_card, bg=BG_INPUT, fg=TEXT_MAIN, relief="flat", font=("Consolas", 9), height=14)
        self.live_info_text.pack(fill="both", expand=True)
        self.live_info_text.insert("1.0", "Click 'Connect & Auth' to fetch active status...")
        self.live_info_text.config(state="disabled")

    def _build_tab_widgets(self, parent):
        scroll_frame = tk.Frame(parent, bg=BG_DARK)
        scroll_frame.pack(fill="both", expand=True)

        card = ttk.Frame(scroll_frame, style="Card.TFrame", padding=16)
        card.pack(fill="both", expand=True, padx=4, pady=8)

        ttk.Label(card, text="Active Screen & Widget Toggles", style="Section.TLabel").pack(anchor="w")
        ttk.Label(card, text="Turn individual screens on or off in the Discord rotation cycle", style="Muted.TLabel").pack(anchor="w", pady=(0, 10))

        widgets_def = [
            ("🖥️ Proxmox VE Overview (CPU, RAM, Workloads)", self.enable_proxmox),
            ("💾 Node Storage Drives (sn770 NVMe SSD + local-lvm)", self.enable_storage),
            ("⛏️ Cryptocurrency Mining (Kryptex GPU/CPU Hashrate)", self.enable_kryptex),
            ("🧱 Minecraft Client Modpack (Star Technology Forge)", self.enable_minecraft),
            ("🌐 Minecraft Dedicated Server (Online/Max Players & MOTD)", self.enable_mc_server),
            ("📈 Recommended Crypto & Stocks (BTC, ETH, SOL, NVDA, AAPL, MSFT, SPY)", self.enable_market),
            ("🎮 Active Games Hub (Multi-Game Session & Party Counter)", self.enable_games_hub),
            ("🕹️ Detect & Show Active PC Games (WAR DOGS, Roblox, CS2, etc.)", self.enable_game_activity),
            ("🔄 Interleave Game Every Other Screen ([Screen] ➔ [Game] ➔ [Screen] ➔ [Game])", self.game_alternate_rotation),
            ("⚡ Network Bandwidth & Latency Speedtest (Cloudflare)", self.enable_speed),
            ("🏆 Steam Profile (Level 100, 3,558 Badges & Playtime)", self.enable_steam),
            ("🐙 GitHub Repositories (Public Projects Counter)", self.enable_github),
            ("🎁 Free Games Claimer (Epic Games & Steam Giveaway Feed)", self.enable_freegames)
        ]

        for label, var in widgets_def:
            row = tk.Frame(card, bg=BG_CARD)
            row.pack(fill="x", pady=4)
            chk = ttk.Checkbutton(row, text=label, variable=var)
            chk.pack(side="left")

    def _build_tab_stoat(self, parent):
        card = ttk.Frame(parent, style="Card.TFrame", padding=16)
        card.pack(fill="both", expand=True, padx=4, pady=8)

        top_row = tk.Frame(card, bg=BG_CARD)
        top_row.pack(fill="x", pady=(0, 8))
        chk_stoat = ttk.Checkbutton(top_row, text="Enable Stoat Chat Synchronization", variable=self.enable_stoat)
        chk_stoat.pack(side="left")

        ttk.Label(card, text="Self-Hosted Stoat Chat Server Configuration", style="Section.TLabel").pack(anchor="w", pady=(6, 2))
        ttk.Label(card, text="Seamlessly sync Discord widgets and rich activity to your own self-hosted Stoat / Revolt server", style="Muted.TLabel").pack(anchor="w", pady=(0, 10))

        # Server URL
        f_url = tk.Frame(card, bg=BG_CARD)
        f_url.pack(fill="x", pady=4)
        ttk.Label(f_url, text="Stoat API URL:").pack(anchor="w")
        e_surl = tk.Entry(f_url, textvariable=self.stoat_url, bg=BG_INPUT, fg=TEXT_MAIN, insertbackground=TEXT_MAIN, relief="flat", font=("Segoe UI", 9))
        e_surl.pack(fill="x", pady=2)
        ttk.Label(f_url, text="E.g. https://stoat.protutech.vip, http://192.168.0.2:8000, or https://api.stoat.chat", style="Muted.TLabel").pack(anchor="w")

        # Token & Auth Mode
        f_auth = tk.Frame(card, bg=BG_CARD)
        f_auth.pack(fill="x", pady=6)

        left_auth = tk.Frame(f_auth, bg=BG_CARD)
        left_auth.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Label(left_auth, text="Authentication Mode:").pack(anchor="w")
        cb_mode = ttk.Combobox(left_auth, textvariable=self.stoat_auth_mode, values=["user", "bot", "bearer", "webhook"], state="readonly")
        cb_mode.pack(fill="x", pady=2)

        right_auth = tk.Frame(f_auth, bg=BG_CARD)
        right_auth.pack(side="left", fill="x", expand=True, padx=(6, 0))
        ttk.Label(right_auth, text="Presence Status:").pack(anchor="w")
        cb_pres = ttk.Combobox(right_auth, textvariable=self.stoat_presence, values=["Online", "Idle", "Focus", "Busy", "Invisible"], state="readonly")
        cb_pres.pack(fill="x", pady=2)

        # Secret Token
        f_tok = tk.Frame(card, bg=BG_CARD)
        f_tok.pack(fill="x", pady=6)
        ttk.Label(f_tok, text="Stoat Secret Token / Webhook URL:").pack(anchor="w")
        e_stok = tk.Entry(f_tok, textvariable=self.stoat_token, show="•", bg=BG_INPUT, fg=TEXT_MAIN, insertbackground=TEXT_MAIN, relief="flat", font=("Segoe UI", 9))
        e_stok.pack(fill="x", pady=2)

        # Custom Status Text Template
        f_tpl = tk.Frame(card, bg=BG_CARD)
        f_tpl.pack(fill="x", pady=6)
        ttk.Label(f_tpl, text="Custom Status Format:").pack(anchor="w")
        e_tpl = tk.Entry(f_tpl, textvariable=self.stoat_template, bg=BG_INPUT, fg=TEXT_MAIN, insertbackground=TEXT_MAIN, relief="flat", font=("Segoe UI", 9))
        e_tpl.pack(fill="x", pady=2)
        ttk.Label(f_tpl, text="Tags: {details}, {state}, {name}, {uptime}", style="Muted.TLabel").pack(anchor="w")

        # Test Connection Box
        f_test = tk.Frame(card, bg=BG_CARD)
        f_test.pack(fill="x", pady=12)
        btn_test = ttk.Button(f_test, text="💬 Test Self-Hosted Stoat Connection", style="Primary.TButton", command=self.test_stoat_connection)
        btn_test.pack(side="left")

        self.lbl_stoat_res = tk.Label(f_test, textvariable=self.stoat_test_status, font=("Segoe UI", 9), fg=TEXT_MUTED, bg=BG_CARD)
        self.lbl_stoat_res.pack(side="left", padx=12)

    def _build_tab_games(self, parent):
        card = ttk.Frame(parent, style="Card.TFrame", padding=16)
        card.pack(fill="both", expand=True, padx=4, pady=8)

        ttk.Label(card, text="Game Detection & Per-Game Muting", style="Section.TLabel").pack(anchor="w")
        ttk.Label(card, text="Control which active games get broadcast to Discord and the Active Games Hub", style="Muted.TLabel").pack(anchor="w", pady=(0, 10))

        self.games_listbox = tk.Listbox(card, bg=BG_INPUT, fg=TEXT_MAIN, selectbackground=ACCENT_BLUE, relief="flat", font=("Segoe UI", 9), height=10)
        self.games_listbox.pack(fill="both", expand=True, pady=4)

        btn_row = tk.Frame(card, bg=BG_CARD)
        btn_row.pack(fill="x", pady=6)
        ttk.Button(btn_row, text="🚫 Toggle Selected Game Mute", style="Secondary.TButton", command=self.toggle_game_mute).pack(side="left", padx=4)
        ttk.Button(btn_row, text="🔄 Refresh Active Games", style="Secondary.TButton", command=self.refresh_games_list).pack(side="left", padx=4)

    def _build_tab_settings(self, parent):
        card = ttk.Frame(parent, style="Card.TFrame", padding=16)
        card.pack(fill="both", expand=True, padx=4, pady=8)

        ttk.Label(card, text="Screen Rotation & Hardware Targets", style="Section.TLabel").pack(anchor="w", pady=(0, 8))

        f_lock = tk.Frame(card, bg=BG_CARD)
        f_lock.pack(fill="x", pady=4)
        ttk.Label(f_lock, text="Active Display Mode:").pack(side="left", padx=(0, 8))
        cb_lock = ttk.Combobox(f_lock, textvariable=self.active_screen, values=[
            "rotate", "proxmox", "storage", "kryptex", "minecraft", "minecraft_server",
            "market", "games_hub", "speed", "steam", "github", "freegames"
        ], state="readonly", width=22)
        cb_lock.pack(side="left")

        f_int = tk.Frame(card, bg=BG_CARD)
        f_int.pack(fill="x", pady=4)
        ttk.Label(f_int, text="Screen Interval (Sec):").pack(side="left", padx=(0, 8))
        tk.Entry(f_int, textvariable=self.update_interval, bg=BG_INPUT, fg=TEXT_MAIN, relief="flat", width=8).pack(side="left")

        ttk.Label(f_int, text="   Speedtest Interval (Min):").pack(side="left", padx=(8, 8))
        tk.Entry(f_int, textvariable=self.speedtest_interval, bg=BG_INPUT, fg=TEXT_MAIN, relief="flat", width=8).pack(side="left")

        f_mc = tk.Frame(card, bg=BG_CARD)
        f_mc.pack(fill="x", pady=6)
        ttk.Label(f_mc, text="Minecraft Server Address:").pack(side="left", padx=(0, 8))
        tk.Entry(f_mc, textvariable=self.mc_server_addr, bg=BG_INPUT, fg=TEXT_MAIN, relief="flat", width=25).pack(side="left", padx=4)
        ttk.Label(f_mc, text="Port:").pack(side="left", padx=(8, 4))
        tk.Entry(f_mc, textvariable=self.mc_server_port, bg=BG_INPUT, fg=TEXT_MAIN, relief="flat", width=8).pack(side="left")

        f_mkt = tk.Frame(card, bg=BG_CARD)
        f_mkt.pack(fill="x", pady=6)
        ttk.Label(f_mkt, text="Tracked Cryptos:").pack(anchor="w")
        tk.Entry(f_mkt, textvariable=self.market_cryptos, bg=BG_INPUT, fg=TEXT_MAIN, relief="flat").pack(fill="x", pady=2)
        ttk.Label(f_mkt, text="Tracked Stocks:").pack(anchor="w", pady=(4, 0))
        tk.Entry(f_mkt, textvariable=self.market_stocks, bg=BG_INPUT, fg=TEXT_MAIN, relief="flat").pack(fill="x", pady=2)

    def _auto_connect_local(self):
        # Auto-connect asynchronously
        threading.Thread(target=self.connect_server, daemon=True).start()

    def connect_server(self):
        url = self.server_url.get().strip().rstrip("/")
        key = self.host_key.get().strip()
        self.footer_msg.config(text=f"Connecting to {url}...", fg=TEXT_MUTED)

        # 1. Try auth login
        try:
            r = requests.post(f"{url}/api/auth/login", json={"key": key}, timeout=4.0)
            if r.status_code == 200:
                self.authenticated.set(True)
                self.connection_status.set("🟢 Host Authenticated")
                self.status_pill.config(bg="#1c3b2b", fg=ACCENT_GREEN)
            else:
                self.authenticated.set(False)
                self.connection_status.set("🟡 Public Viewer")
                self.status_pill.config(bg="#3a301d", fg=ACCENT_YELLOW)
        except Exception:
            self.authenticated.set(False)
            self.connection_status.set("🔴 Offline")
            self.status_pill.config(bg="#3b1d1d", fg=ACCENT_RED)

        # 2. Load config & stats
        self.load_from_server()

    def load_from_server(self):
        url = self.server_url.get().strip().rstrip("/")
        key = self.host_key.get().strip()

        # If local file exists, populate defaults from config.json directly
        if os.path.exists(LOCAL_CONFIG_PATH):
            try:
                with open(LOCAL_CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    self._populate_fields(cfg)
            except Exception:
                pass

        # Also query daemon API
        try:
            headers = {"Authorization": f"Bearer {key}"}
            r_cfg = requests.get(f"{url}/api/config", headers=headers, timeout=4.0)
            if r_cfg.status_code == 200:
                self._populate_fields(r_cfg.json())

            r_stats = requests.get(f"{url}/api/stats", timeout=4.0)
            if r_stats.status_code == 200:
                s_data = r_stats.json()
                self._display_live_info(s_data)

            self.refresh_games_list()
            self.footer_msg.config(text=f"Loaded configuration from {url}", fg=ACCENT_GREEN)
        except Exception as e:
            self.footer_msg.config(text=f"Offline mode (Local config loaded): {e}", fg=ACCENT_YELLOW)

    def _populate_fields(self, cfg):
        self.enable_proxmox.set(cfg.get("enable_proxmox_screen", True))
        self.enable_storage.set(cfg.get("enable_storage_screen", True))
        self.enable_kryptex.set(cfg.get("enable_kryptex_screen", True))
        self.enable_minecraft.set(cfg.get("enable_minecraft_screen", True))
        self.enable_mc_server.set(cfg.get("enable_minecraft_server_screen", True))
        self.enable_market.set(cfg.get("enable_market_screen", True))
        self.enable_games_hub.set(cfg.get("enable_active_games_hub", True))
        self.enable_game_activity.set(cfg.get("enable_game_activity", True))
        self.game_alternate_rotation.set(cfg.get("game_alternate_rotation", True))
        self.enable_speed.set(cfg.get("enable_speed_screen", True))
        self.enable_steam.set(cfg.get("enable_steam_screen", True))
        self.enable_github.set(cfg.get("enable_github_screen", True))
        self.enable_freegames.set(cfg.get("enable_free_games_screen", True))

        # Stoat
        self.enable_stoat.set(cfg.get("enable_stoat_sync", False))
        self.stoat_url.set(cfg.get("stoat_api_url", "https://api.stoat.chat"))
        self.stoat_token.set(cfg.get("stoat_token", ""))
        self.stoat_auth_mode.set(cfg.get("stoat_token_type", "user"))
        self.stoat_presence.set(cfg.get("stoat_presence", "Online"))
        self.stoat_template.set(cfg.get("stoat_template", "{details} • {state}"))

        # Settings
        self.active_screen.set(cfg.get("active_screen", "rotate"))
        self.update_interval.set(str(cfg.get("update_interval_seconds", 6)))
        self.speedtest_interval.set(str(cfg.get("speedtest_interval_minutes", 30)))
        self.server_label.set(cfg.get("server_label", "Protutech"))
        self.mc_server_addr.set(cfg.get("minecraft_server_address", "minecraft.protutech.vip"))
        self.mc_server_port.set(str(cfg.get("minecraft_server_port", 25565)))

        c_list = cfg.get("market_crypto_list", ["BTC", "ETH", "SOL"])
        self.market_cryptos.set(", ".join(c_list) if isinstance(c_list, list) else str(c_list))
        s_list = cfg.get("market_stocks_list", ["NVDA", "AAPL", "MSFT", "SPY"])
        self.market_stocks.set(", ".join(s_list) if isinstance(s_list, list) else str(s_list))

        self.disabled_games = cfg.get("disabled_games", [])

    def _display_live_info(self, data):
        self.live_info_text.config(state="normal")
        self.live_info_text.delete("1.0", "end")

        txt = []
        txt.append(f"CURRENT SCREEN   : {data.get('current_screen_name', 'Unknown')}")
        txt.append(f"TOTAL SCREENS    : {len(data.get('screens', []))} active in rotation")
        txt.append(f"STOAT SYNC       : {'ACTIVE' if data.get('stoat_synced') else 'Inactive'}")

        # Proxmox / Storage info
        pve_screen = next((s for s in data.get("screens", []) if s.get("name") == "Proxmox Overview"), None)
        if pve_screen and pve_screen.get("stats"):
            st = pve_screen["stats"]
            txt.append("")
            txt.append("--- PROXMOX HYPERVISOR STATS ---")
            txt.append(f"Node             : {st.get('node', 'Protutech')} (Uptime: {st.get('uptime', '0m')})")
            txt.append(f"Workloads        : {st.get('running_vms', 0)} VMs, {st.get('running_lxcs', 0)} LXCs")
            txt.append(f"CPU / Memory     : {st.get('cpu_pct', 0):.1f}% CPU | {st.get('mem_pct', 0):.0f}% RAM")
            txt.append(f"Total Storage    : {st.get('storage_used_gb', 0):.0f} GB / {st.get('storage_total_tb', 0):.1f} TB ({st.get('storage_pct', 0):.1f}%)")
            pools = st.get("storage_pools", [])
            for p in pools:
                new_tag = " [NEW DRIVE]" if p.get("is_new") else ""
                txt.append(f"  • Drive {p['name']:10s} ({p['type']:7s}): {p['used_gb']}G / {p['total_tb']}TB ({p['pct']}%) {new_tag}")

        self.live_info_text.insert("1.0", "\n".join(txt))
        self.live_info_text.config(state="disabled")

    def refresh_games_list(self):
        url = self.server_url.get().strip().rstrip("/")
        try:
            r = requests.get(f"{url}/api/games", timeout=3.0)
            if r.status_code == 200:
                g_data = r.json()
                self.detected_games = g_data.get("detected", [])
                self.disabled_games = g_data.get("disabled_games", [])
                self.games_listbox.delete(0, "end")
                for g in self.detected_games:
                    g_name = g.get("name", "Unknown")
                    is_dis = g.get("disabled") or (g_name in self.disabled_games)
                    tag = "[MUTED] " if is_dis else "[ACTIVE] "
                    self.games_listbox.insert("end", f"{tag} {g_name} ({g.get('exe', '')})")
        except Exception:
            pass

    def toggle_game_mute(self):
        sel = self.games_listbox.curselection()
        if not sel:
            messagebox.showinfo("Select Game", "Please select a game from the list to toggle.")
            return

        idx = sel[0]
        if idx < len(self.detected_games):
            g = self.detected_games[idx]
            g_name = g.get("name")
            url = self.server_url.get().strip().rstrip("/")
            key = self.host_key.get().strip()
            try:
                r = requests.post(
                    f"{url}/api/games/toggle",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"name": g_name},
                    timeout=3.0
                )
                if r.status_code == 200:
                    self.refresh_games_list()
                    self.footer_msg.config(text=f"Toggled {g_name}", fg=ACCENT_GREEN)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to toggle game: {e}")

    def test_stoat_connection(self):
        url = self.stoat_url.get().strip().rstrip("/")
        token = self.stoat_token.get().strip()
        mode = self.stoat_auth_mode.get()

        if not url:
            self.lbl_stoat_res.config(text="⚠️ Please enter a Stoat API URL", fg=ACCENT_YELLOW)
            return

        self.lbl_stoat_res.config(text="Connecting to self-hosted Stoat Chat...", fg=TEXT_MUTED)

        def _worker():
            try:
                # 1. Ping base endpoint
                t0 = time.time()
                r_base = requests.get(url, timeout=4.0)
                latency = int((time.time() - t0) * 1000)

                # 2. If token provided, test auth
                headers = {"Content-Type": "application/json"}
                if mode == "bot":
                    headers["x-bot-token"] = token
                elif mode == "bearer":
                    headers["Authorization"] = f"Bearer {token}"
                elif mode == "webhook":
                    headers["Content-Type"] = "application/json"
                else:
                    headers["X-Session-Token"] = token

                auth_info = ""
                if token and mode != "webhook":
                    r_user = requests.get(f"{url}/users/@me", headers=headers, timeout=4.0)
                    if r_user.status_code == 200:
                        u_data = r_user.json()
                        uname = u_data.get("username") or u_data.get("name") or "Authorized User"
                        auth_info = f" • Logged in as: {uname}"
                    elif r_user.status_code in (401, 403):
                        self.lbl_stoat_res.config(text=f"❌ HTTP 401: Invalid Stoat Token ({latency}ms)", fg=ACCENT_RED)
                        return

                self.lbl_stoat_res.config(text=f"🟢 Connected! Self-hosted Stoat Online ({latency}ms){auth_info}", fg=ACCENT_GREEN)
            except Exception as e:
                self.lbl_stoat_res.config(text=f"🔴 Connection Failed: {str(e)[:45]}", fg=ACCENT_RED)

        threading.Thread(target=_worker, daemon=True).start()

    def save_and_apply(self):
        payload = {
            "enable_proxmox_screen": self.enable_proxmox.get(),
            "enable_storage_screen": self.enable_storage.get(),
            "enable_kryptex_screen": self.enable_kryptex.get(),
            "enable_minecraft_screen": self.enable_minecraft.get(),
            "enable_minecraft_server_screen": self.enable_mc_server.get(),
            "enable_market_screen": self.enable_market.get(),
            "enable_active_games_hub": self.enable_games_hub.get(),
            "enable_game_activity": self.enable_game_activity.get(),
            "game_alternate_rotation": self.game_alternate_rotation.get(),
            "enable_speed_screen": self.enable_speed.get(),
            "enable_steam_screen": self.enable_steam.get(),
            "enable_github_screen": self.enable_github.get(),
            "enable_free_games_screen": self.enable_freegames.get(),

            # Stoat Chat
            "enable_stoat_sync": self.enable_stoat.get(),
            "stoat_api_url": self.stoat_url.get().strip().rstrip("/"),
            "stoat_token": self.stoat_token.get().strip(),
            "stoat_token_type": self.stoat_auth_mode.get(),
            "stoat_presence": self.stoat_presence.get(),
            "stoat_template": self.stoat_template.get(),

            # Settings
            "active_screen": self.active_screen.get(),
            "update_interval_seconds": int(self.update_interval.get() or 6),
            "speedtest_interval_minutes": int(self.speedtest_interval.get() or 30),
            "server_label": self.server_label.get().strip(),
            "minecraft_server_address": self.mc_server_addr.get().strip(),
            "minecraft_server_port": int(self.mc_server_port.get() or 25565),
            "market_crypto_list": [x.strip() for x in self.market_cryptos.get().split(",") if x.strip()],
            "market_stocks_list": [x.strip() for x in self.market_stocks.get().split(",") if x.strip()],
            "host_key": self.host_key.get().strip()
        }

        # 1. Update local config.json if available
        if os.path.exists(LOCAL_CONFIG_PATH):
            try:
                with open(LOCAL_CONFIG_PATH, "r", encoding="utf-8") as f:
                    curr = json.load(f)
                curr.update(payload)
                with open(LOCAL_CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(curr, f, indent=4)
            except Exception as e:
                print(f"[WARN] Local config save failed: {e}")

        # 2. Push to daemon API
        url = self.server_url.get().strip().rstrip("/")
        key = self.host_key.get().strip()
        try:
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            r = requests.post(f"{url}/api/config", headers=headers, json=payload, timeout=5.0)
            if r.status_code == 200:
                self.footer_msg.config(text="✅ Configuration applied successfully to Discord!", fg=ACCENT_GREEN)
                messagebox.showinfo("Success", "Settings applied successfully! Discord presence & Stoat Chat sync updated.")
            else:
                self.footer_msg.config(text=f"⚠️ Daemon responded HTTP {r.status_code}", fg=ACCENT_YELLOW)
        except Exception as e:
            self.footer_msg.config(text=f"Saved locally (Daemon offline: {e})", fg=ACCENT_YELLOW)
            messagebox.showinfo("Saved Locally", "Saved to local config.json. Start or reload daemon to apply.")


if __name__ == "__main__":
    app = CustomizerApp()
    app.mainloop()
