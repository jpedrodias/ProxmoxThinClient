# ProxmoxThinClient
Script to connect to a Proxmox VM via SPICE


# Setup on Linux
**System requirements:**
```bash
sudo apt install python3-pip virt-viewer git
```

**Install App and requirements:**
```bash
git clone https://github.com/jpedrodias/ProxmoxThinClient.git
cd ProxmoxThinClient

python3 -m venv venv

source venv/bin/activate

pip install proxmoxer requests
```

**Change settings:**
```bash
nano vdiclient.ini
```
ps: if vmid = 0, then the first vm in the list will be used.



**Run app:**
```bash
python vdiclient.py
```
---


# Setup on Windows
