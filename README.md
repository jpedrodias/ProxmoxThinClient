# ProxmoxThinClient
Script to connect to a Proxmox VM via SPICE


# Setup on Linux
```bash
sudo apt install python3-pip virt-viewer git


git clone https://github.com/jpedrodias/ProxmoxThinClient.git
cd ProxmoxThinClient

nano vdiclient.ini

python3 -m venv venv

source venv/bin/activate

pip install proxmoxer requests
```

```bash
python vdiclient.py
```