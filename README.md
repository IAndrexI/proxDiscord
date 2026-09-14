# proxDiscord

Display live Proxmox VE server stats directly on your Discord profile using Discord Rich Presence.

<p align="center">
  <img src="protutech_cloud.jpg" alt="Protutech Cloud Logo" width="220">
</p>

## Features
- **Auto-Rotating Screens**: Cycles between CPU/RAM/Uptime and Storage/VMs/LXCs every 15 seconds.
- **Active Guests Badge**: Shows a live `(16 of 16)` active guest count.
- **Background Mode & Auto-Start**: Runs silently and starts with Windows on boot.
- **Self-Healing**: Automatically reconnects if Discord or network restarts.

---

## Quick Setup

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure
Copy `config.example.json` to `config.json`:
```bash
copy config.example.json config.json
```
Fill in your Proxmox connection details and Discord Application ID in `config.json`:
```json
{
  "discord_client_id": "YOUR_DISCORD_APPLICATION_ID",
  "server_label": "Homelabs",
  "proxmox_host": "https://192.168.0.2:8006",
  "proxmox_node": "Protutech",
  "proxmox_token_id": "root@pam!discord-rpc",
  "proxmox_token_secret": "YOUR_TOKEN_SECRET_HERE",
  "update_interval_seconds": 15,
  "large_image": "protutech",
  "show_party_badge": true
}
```

### 3. Run
- **Interactive**: Double-click `run.bat`
- **Background (Silent)**: Double-click `run-background.vbs`
- **Start with Windows**: Double-click `install_startup.bat`
