import configparser
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def print_step(title: str) -> None:
    print(f"\n=== {title} ===")


def script_ini_path() -> Path:
    return Path(__file__).with_suffix(".ini")


def load_config() -> dict:
    ini_path = script_ini_path()

    if not ini_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {ini_path}")

    parser = configparser.ConfigParser()
    parser.read(ini_path, encoding="utf-8")

    cfg = {
        "proxmox_host": parser.get("proxmox", "host"),
        "proxmox_port": parser.getint("proxmox", "port", fallback=8006),
        "username": parser.get("auth", "username"),
        "password": parser.get("auth", "password"),
        "node": parser.get("vm", "node", fallback="").strip(),
        "vmid": parser.getint("vm", "vmid"),
        "verify_tls": parser.getboolean("connection", "verify_tls", fallback=False),
        "timeout": parser.getint("connection", "timeout", fallback=10),
        "proxy_scheme": parser.get("spice", "proxy_scheme", fallback="http"),
        "proxy_host": parser.get("spice", "proxy_host"),
        "proxy_port": parser.getint("spice", "proxy_port", fallback=3128),
        "remote_viewer_path": parser.get("viewer", "remote_viewer_path", fallback="").strip(),
        "fullscreen": parser.getboolean("viewer", "fullscreen", fallback=False),
        "output_dir": parser.get("output", "output_dir", fallback=".").strip(),
    }

    cfg["base_url"] = f"https://{cfg['proxmox_host']}:{cfg['proxmox_port']}/api2/json"
    cfg["proxy_url"] = f"{cfg['proxy_scheme']}://{cfg['proxy_host']}:{cfg['proxy_port']}"

    return cfg


def get_remote_viewer_command(configured_path: str) -> str:
    if configured_path:
        expanded = os.path.expandvars(os.path.expanduser(configured_path))
        if os.path.exists(expanded):
            return expanded
        raise FileNotFoundError(
            f"remote-viewer not found at configured path: {expanded}"
        )

    cmd = shutil.which("remote-viewer")
    if cmd:
        return cmd

    raise FileNotFoundError(
        "remote-viewer not found. Set remote_viewer_path in the .ini file "
        "or install remote-viewer in PATH."
    )


def build_vv_exact(data: dict) -> str:
    order = [
        "release-cursor",
        "tls-port",
        "type",
        "password",
        "ca",
        "proxy",
        "secure-attention",
        "delete-this-file",
        "host-subject",
        "toggle-fullscreen",
        "title",
        "host",
    ]

    lines = ["[virt-viewer]"]

    for key in order:
        if key not in data:
            continue

        value = data[key]

        if key == "ca" and isinstance(value, str):
            value = value.replace("\r\n", "\n").replace("\r", "\n")
            value = value.replace("\n", "\\n")

        lines.append(f"{key}={value}")

    return "\n".join(lines) + "\n"


def test_tcp_connectivity(host: str, port: int, timeout: int) -> None:
    with socket.create_connection((host, port), timeout=timeout):
        pass


def authenticate(session: requests.Session, cfg: dict) -> None:
    r = session.post(
        f"{cfg['base_url']}/access/ticket",
        data={
            "username": cfg["username"],
            "password": cfg["password"],
        },
        verify=cfg["verify_tls"],
        timeout=cfg["timeout"],
    )
    print(f"HTTP {r.status_code}")
    r.raise_for_status()

    auth_data = r.json()["data"]
    ticket = auth_data.get("ticket")
    csrf = auth_data.get("CSRFPreventionToken")

    if not ticket:
        raise RuntimeError("Authentication returned no ticket from the API.")

    session.cookies.set("PVEAuthCookie", ticket)
    if csrf:
        session.headers.update({"CSRFPreventionToken": csrf})

    print(f"Authentication OK. User: {auth_data.get('username')}")


def select_assigned_vm(session: requests.Session, cfg: dict) -> None:
    """
    If vmid = 0, selects the first VM visible to the authenticated user.
    The list comes from /cluster/resources?type=vm.
    """
    if cfg["vmid"] != 0:
        if not cfg["node"]:
            raise RuntimeError("The 'node' field must be defined when vmid > 0.")
        return

    r = session.get(
        f"{cfg['base_url']}/cluster/resources",
        params={"type": "vm"},
        verify=cfg["verify_tls"],
        timeout=cfg["timeout"],
    )
    print(f"HTTP {r.status_code}")
    r.raise_for_status()

    resources = r.json().get("data", [])
    if not resources:
        raise RuntimeError("No accessible VMs were found for this user.")

    vms = []
    for item in resources:
        vmid = item.get("vmid")
        node = item.get("node")
        rtype = item.get("type")

        # In Proxmox, QEMU VMs typically appear with type='qemu'.
        # We keep only entries with valid vmid and node.
        if vmid is None or not node:
            continue
        if rtype not in ("qemu", "vm", None):
            continue

        vms.append(
            {
                "vmid": int(vmid),
                "node": str(node),
                "name": item.get("name", ""),
                "status": item.get("status", ""),
            }
        )

    if not vms:
        raise RuntimeError("Resources were returned, but no usable VM was found.")

    vms.sort(key=lambda x: x["vmid"])
    chosen = vms[0]

    cfg["vmid"] = chosen["vmid"]
    cfg["node"] = chosen["node"]

    print(
        f"vmid=0 detected. "
        f"Selected VM: {cfg['vmid']} on node '{cfg['node']}'"
        + (f" ({chosen['name']})" if chosen["name"] else "")
    )


def get_vm_status(session: requests.Session, cfg: dict) -> str:
    r = session.get(
        f"{cfg['base_url']}/nodes/{cfg['node']}/qemu/{cfg['vmid']}/status/current",
        verify=cfg["verify_tls"],
        timeout=cfg["timeout"],
    )
    print(f"HTTP {r.status_code}")
    r.raise_for_status()

    status_data = r.json().get("data", {})
    status = status_data.get("status")
    if not status:
        raise RuntimeError("The API did not return the current VM status.")

    return status


def ensure_vm_running(session: requests.Session, cfg: dict, wait_seconds: int = 10) -> None:
    status = get_vm_status(session, cfg)
    print(f"Current state of VM {cfg['vmid']}: {status}")

    if status == "running":
        print("The VM is already running.")
        return

    if status != "stopped":
        raise RuntimeError(f"The VM is in an unexpected state: {status}")

    print(f"Starting VM {cfg['vmid']}...")
    r = session.post(
        f"{cfg['base_url']}/nodes/{cfg['node']}/qemu/{cfg['vmid']}/status/start",
        verify=cfg["verify_tls"],
        timeout=cfg["timeout"],
    )
    print(f"HTTP {r.status_code}")
    r.raise_for_status()

    print(f"VM started. Waiting {wait_seconds} seconds before continuing...")
    time.sleep(wait_seconds)

    status = get_vm_status(session, cfg)
    print(f"State after wait: {status}")
    if status != "running":
        raise RuntimeError(f"The VM was not ready after startup. Current state: {status}")


def request_spice(session: requests.Session, cfg: dict) -> dict:
    r = session.post(
        f"{cfg['base_url']}/nodes/{cfg['node']}/qemu/{cfg['vmid']}/spiceproxy",
        verify=cfg["verify_tls"],
        timeout=cfg["timeout"],
    )
    print(f"HTTP {r.status_code}")
    r.raise_for_status()

    spice_data = r.json().get("data", {})
    if not spice_data:
        raise RuntimeError("The API did not return SPICE data.")

    print("host:", spice_data.get("host"))
    print("original proxy:", spice_data.get("proxy"))
    print("tls-port:", spice_data.get("tls-port"))

    # Keeps the previous behavior that already works in your scenario.
    spice_data["proxy"] = cfg["proxy_url"]
    print("final proxy:", spice_data.get("proxy"))

    return spice_data


def write_vv_file(cfg: dict, vv_text: str) -> Path:
    output_dir = Path(os.path.expandvars(os.path.expanduser(cfg["output_dir"]))).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    vv_file = output_dir / f"vm{cfg['vmid']}.vv"
    vv_file.write_text(vv_text, encoding="utf-8", newline="\n")
    return vv_file


def launch_remote_viewer(viewer_cmd: str, vv_file: Path, fullscreen: bool = False) -> None:
    cmd = [viewer_cmd]

    if fullscreen:
        cmd.append("--full-screen")

    cmd.append(str(vv_file))
    subprocess.Popen(cmd)


def main() -> int:
    try:
        cfg = load_config()
    except Exception as e:
        print(f"ERROR loading configuration: {e}")
        return 10

    session = requests.Session()

    print_step("Step 1: TCP reachability to Proxmox")
    try:
        test_tcp_connectivity(cfg["proxmox_host"], cfg["proxmox_port"], cfg["timeout"])
        print(f"TCP OK to {cfg['proxmox_host']}:{cfg['proxmox_port']}")
    except Exception as e:
        print(f"TCP connectivity ERROR: {e}")
        return 1

    print_step("Step 2: authentication")
    try:
        authenticate(session, cfg)
    except Exception as e:
        print(f"Authentication ERROR: {e}")
        return 2

    print_step("Step 3: resolve VM")
    try:
        select_assigned_vm(session, cfg)
        print(f"Final VM to use: vmid={cfg['vmid']} | node={cfg['node']}")
    except Exception as e:
        print(f"ERROR resolving VM: {e}")
        return 3

    print_step("Step 4: check/start VM")
    try:
        ensure_vm_running(session, cfg, wait_seconds=10)
    except Exception as e:
        print(f"ERROR checking or starting VM: {e}")
        return 4

    print_step("Step 5: request SPICE session")
    try:
        spice_data = request_spice(session, cfg)
    except Exception as e:
        print(f"ERROR requesting SPICE session: {e}")
        return 5

    print_step("Step 6: generate .vv file")
    try:
        vv_text = build_vv_exact(spice_data)
        vv_file = write_vv_file(cfg, vv_text)
        print(f".vv file written to: {vv_file}")
        print("\n--- START OF .vv FILE ---")
        print(vv_text)
        print("--- END OF .vv FILE ---")
    except Exception as e:
        print(f"ERROR generating .vv file: {e}")
        return 6

    print_step("Step 7: run remote-viewer")
    try:
        viewer = get_remote_viewer_command(cfg["remote_viewer_path"])
        print(f"Using: {viewer}")
        launch_remote_viewer(viewer, vv_file, cfg["fullscreen"])
        print("remote-viewer launched successfully.")
        return 0
    except Exception as e:
        print(f"ERROR running remote-viewer: {e}")
        return 7


if __name__ == "__main__":
    raise SystemExit(main())