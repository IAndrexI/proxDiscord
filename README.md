# Proxmox VE Discord Rich Presence (RPC)

Display real-time Proxmox VE server metrics directly on your Discord profile using Discord Rich Presence.

![Protutech Cloud Logo](protutech_cloud.jpg)

## Features

- 🔄 **Multi-Screen Rotation**: Automatically cycles between status screens every 15 seconds:
  - **Screen 1 (Host Health & Performance)**: Live CPU %, RAM % (Used/Total), Uptime.
  - **Screen 2 (Storage & Workloads)**: VM count, Container count, primary Storage Pool usage.
  - **Screen 3 (Minecraft Server - Optional)**: Server status and address.
- 👥 **Active Guests Counter Badge**: Displays a `(16 of 16)` badge next to your status showing active VMs and LXCs.
- 🛡️ **Privacy First**: No public web links or exposed credentials; queries Proxmox locally over HTTPS.
- 🚀 **Windows Auto-Start**: Includes 1-click startup scripts to run silently in the background on system boot.
- 🔁 **Self-Healing Connection**: Automatically handles Discord restarts, PC wake/sleep, and network drops.

---

## Getting Started

### 1. Requirements
- Python 3.10+
- Proxmox VE (7.x, 8.x, or 9.x)
- Discord Desktop Client running on the machine

### 2. Installation
1. Clone this repository:
   ```bash
   git clone https://github.com/YOUR_USERNAME/proxmox-discord-rpc.git
   cd proxmox-discord-rpc
   ```
2. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   .\.venv\Scripts\activate   # On Windows
   # source .venv/bin/activate # On Linux/macOS
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

### 3. Create a Proxmox API Token
1. Open your Proxmox Web GUI (`https://<proxmox-ip>:8006`).
2. Navigate to **Datacenter** -> **Permissions** -> **API Tokens** -> click **Add**.
3. Fill in:
   - **User**: `root@pam` (or your monitoring user)
   - **Token ID**: `discord-rpc`
   - **Privilege Separation**: Unchecked (or ensure `PVEAuditor` permission).
4. Save the generated **Token Secret** key.

### 4. Configuration
Copy `config.example.json` to `config.json`:
```bash
copy config.example.json config.json
```
Edit `config.json` with your details:
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
  "show_party_badge": true,
  "enable_minecraft_screen": false,
  "minecraft_server_address": "play.yourdomain.com:25565"
}
```

### 5. Discord Developer Portal Setup
1. Go to [Discord Developer Portal](https://discord.com/developers/applications).
2. Create an Application and copy the **Application ID** into `config.json`.
3. Set the application name to whatever title you want displayed (e.g. `Homelabs` or `Proxmox VE`).
4. In **Rich Presence** -> **Art Assets**, upload your logo with the asset key `protutech`.

---

## Running the Application

- **Interactive Mode**: Double-click `run.bat` or run:
  ```powershell
  .\run.bat
  ```
- **Silent Background Mode**: Double-click `run-background.vbs`.
- **Run Automatically at Windows Boot**: Double-click `install_startup.bat`.
- **Remove from Windows Boot**: Double-click `uninstall_startup.bat`.

---

## License
MIT License
