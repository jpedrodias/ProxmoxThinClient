# ProxmoxThinClient
Script to connect to a Proxmox VM using SPICE


# Setup — Linux
**System requirements:**

- A recent distribution with `python3` (3.8+ recommended).
- `virt-viewer` (provides the `virt-viewer`/`remote-viewer` client used to open SPICE sessions).
- `git` (optional — only needed to clone the repo).

Install packages (Ubuntu/Debian example):
```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip virt-viewer git
```

**Install the app and dependencies:**

1. Clone the repository (or extract the ZIP if you downloaded it):
```bash
git clone https://github.com/jpedrodias/ProxmoxThinClient.git
cd ProxmoxThinClient
```

2. Create and activate a virtual environment — this keeps dependencies isolated from your system Python:
```bash
python3 -m venv venv
source venv/bin/activate
```

3. Install the Python dependencies required by the script:
```bash
pip install proxmoxer requests
```

**Edit configuration:**
Open `vdiclient.ini` and configure your Proxmox host, credentials and preferences. For example, edit with `nano`:
```bash
nano vdiclient.ini
```
Note: if `vmid = 0`, the script will select the first VM in the list returned by the API.

**Run the app:**

With the virtual environment active run:
```bash
python vdiclient.py
```

If you prefer not to activate the venv, you can run the Python executable inside the venv directly:
```bash
venv/bin/python vdiclient.py
```
---

# Setup — Windows
**System requirements:**

1. Install [Python 3](https://www.python.org/downloads/) (make sure to add Python to PATH during installation).
2. Install [Virt-Viewer](https://virt-manager.org/download/).
3. (Optional) Install [Git](https://git-scm.com/download/win) to clone the repository.

**Install the app and dependencies:**

If using Git:
```bat
git clone https://github.com/jpedrodias/ProxmoxThinClient.git
cd ProxmoxThinClient
```

If you don't have Git, download the repository ZIP and extract it.

Create the virtual environment and install dependencies:
```bat
python -m venv venv
call venv\Scripts\activate.bat
pip install -r requirements.bat
```
Or, manually:
```bat
python -m venv venv
call venv\Scripts\activate.bat
pip install proxmoxer requests pywin32
```

**Change settings:**
Edit the `vdiclient.ini` file to set connection options:
```bat
notepad vdiclient.ini
```
Note: if `vmid = 0`, the first VM in the list will be used.

**Run the app:**
```bat
call run.bat
```
or
```bat
call venv\Scripts\activate.bat
python vdiclient.py
call venv\Scripts\deactivate.bat
```

---

# Setup Thin Client Debian XFCE as Kiosk mode

This section shows a simple approach to run the thin client automatically in a locked-down XFCE session (kiosk-style). Adjust paths and usernames to fit your environment.

1) Install required packages (Debian/Ubuntu):

```bash
sudo apt update
sudo apt install xorg xfce4 lightdm xinit
```

2) Create a dedicated kiosk user (optional but recommended):

```bash
sudo adduser --disabled-password --gecos "" kiosk
```

3) Configure LightDM autologin for the kiosk user: create `/etc/lightdm/lightdm.conf.d/50-autologin.conf` with:

```ini
[Seat:*]
autologin-user=kiosk
autologin-user-timeout=0
user-session=xfce
```

4) Install or copy the application into the kiosk user's home (example: `/home/kiosk/ProxmoxThinClient`) and create a small launcher script `start-vdiclient.sh` inside that folder:

```bash
# as root or a user with permissions
cp -r /path/to/ProxmoxThinClient /home/kiosk/ProxmoxThinClient
chown -R kiosk:kiosk /home/kiosk/ProxmoxThinClient

cat > /home/kiosk/ProxmoxThinClient/start-vdiclient.sh <<'EOF'
#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate
exec python vdiclient.py
EOF

chmod +x /home/kiosk/ProxmoxThinClient/start-vdiclient.sh
chown kiosk:kiosk /home/kiosk/ProxmoxThinClient/start-vdiclient.sh
```

5) Create an XFCE autostart desktop entry so the session launches the script on login:

```bash
sudo -u kiosk mkdir -p /home/kiosk/.config/autostart
cat > /home/kiosk/.config/autostart/vdiclient.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Proxmox Thin Client
Exec=/home/kiosk/ProxmoxThinClient/start-vdiclient.sh
StartupNotify=false
Terminal=false
EOF

chown kiosk:kiosk /home/kiosk/.config/autostart/vdiclient.desktop
```

6) Optional: disable screen locking and automatic suspend for the kiosk session (you can use the XFCE GUI or run xfconf commands):

```bash
# Example: disable lock/screen saver settings via xfconf (run as kiosk user or via sudo -u)
sudo -u kiosk xfconf-query -c xfce4-session -p /general/LockCommand --create --type string -s ""
sudo -u kiosk xfconf-query -c xfce4-power-manager -p /xfce4-power-manager/blank-on-ac --create --type int -s 0
```

7) Reboot and verify the kiosk user logs in automatically and `vdiclient.py` starts. If anything fails, check `~/.xsession-errors`, LightDM logs in `/var/log/lightdm/`, and the script log/output.

Notes:
- If you prefer not to use LightDM, you can configure auto-login via other display managers or run X from a systemd user service.
- Keep the system updated and restrict SSH/console access for kiosk users as required by your security policy.
