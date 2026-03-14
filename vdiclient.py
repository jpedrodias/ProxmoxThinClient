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
        raise FileNotFoundError(f"Ficheiro de configuração não encontrado: {ini_path}")

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
            f"remote-viewer não encontrado no caminho configurado: {expanded}"
        )

    cmd = shutil.which("remote-viewer")
    if cmd:
        return cmd

    raise FileNotFoundError(
        "remote-viewer não encontrado. Define remote_viewer_path no ficheiro .ini "
        "ou instala remote-viewer no PATH."
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
        raise RuntimeError("Autenticação sem ticket devolvido pela API.")

    session.cookies.set("PVEAuthCookie", ticket)
    if csrf:
        session.headers.update({"CSRFPreventionToken": csrf})

    print(f"Autenticação OK. Utilizador: {auth_data.get('username')}")


def select_assigned_vm(session: requests.Session, cfg: dict) -> None:
    """
    Se vmid = 0, escolhe a primeira VM visível ao utilizador autenticado.
    A lista vem de /cluster/resources?type=vm.
    """
    if cfg["vmid"] != 0:
        if not cfg["node"]:
            raise RuntimeError("O campo 'node' tem de estar definido quando vmid > 0.")
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
        raise RuntimeError("Não foram encontradas VMs acessíveis para este utilizador.")

    vms = []
    for item in resources:
        vmid = item.get("vmid")
        node = item.get("node")
        rtype = item.get("type")

        # Em Proxmox, VMs QEMU aparecem tipicamente com type='qemu'.
        # Mantemos apenas entradas com vmid e node válidos.
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
        raise RuntimeError("Foram devolvidos recursos, mas nenhuma VM utilizável foi encontrada.")

    vms.sort(key=lambda x: x["vmid"])
    chosen = vms[0]

    cfg["vmid"] = chosen["vmid"]
    cfg["node"] = chosen["node"]

    print(
        f"vmid=0 detetado. "
        f"VM escolhida: {cfg['vmid']} no node '{cfg['node']}'"
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
        raise RuntimeError("A API não devolveu o estado atual da VM.")

    return status


def ensure_vm_running(session: requests.Session, cfg: dict, wait_seconds: int = 10) -> None:
    status = get_vm_status(session, cfg)
    print(f"Estado atual da VM {cfg['vmid']}: {status}")

    if status == "running":
        print("A VM já está ligada.")
        return

    if status != "stopped":
        raise RuntimeError(f"A VM está num estado inesperado: {status}")

    print(f"A iniciar VM {cfg['vmid']}...")
    r = session.post(
        f"{cfg['base_url']}/nodes/{cfg['node']}/qemu/{cfg['vmid']}/status/start",
        verify=cfg["verify_tls"],
        timeout=cfg["timeout"],
    )
    print(f"HTTP {r.status_code}")
    r.raise_for_status()

    print(f"VM iniciada. A aguardar {wait_seconds} segundos antes de continuar...")
    time.sleep(wait_seconds)

    status = get_vm_status(session, cfg)
    print(f"Estado após espera: {status}")
    if status != "running":
        raise RuntimeError(f"A VM não ficou pronta após arranque. Estado atual: {status}")


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
        raise RuntimeError("A API não devolveu dados SPICE.")

    print("host:", spice_data.get("host"))
    print("proxy original:", spice_data.get("proxy"))
    print("tls-port:", spice_data.get("tls-port"))

    # Mantém o comportamento antigo que já funciona no teu cenário.
    spice_data["proxy"] = cfg["proxy_url"]
    print("proxy final:", spice_data.get("proxy"))

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
        print(f"ERRO a carregar configuração: {e}")
        return 10

    session = requests.Session()

    print_step("Passo 1: reachability TCP ao Proxmox")
    try:
        test_tcp_connectivity(cfg["proxmox_host"], cfg["proxmox_port"], cfg["timeout"])
        print(f"TCP OK para {cfg['proxmox_host']}:{cfg['proxmox_port']}")
    except Exception as e:
        print(f"ERRO de conectividade TCP: {e}")
        return 1

    print_step("Passo 2: autenticação")
    try:
        authenticate(session, cfg)
    except Exception as e:
        print(f"ERRO na autenticação: {e}")
        return 2

    print_step("Passo 3: resolver VM")
    try:
        select_assigned_vm(session, cfg)
        print(f"VM final a usar: vmid={cfg['vmid']} | node={cfg['node']}")
    except Exception as e:
        print(f"ERRO ao resolver a VM: {e}")
        return 3

    print_step("Passo 4: verificar/ligar VM")
    try:
        ensure_vm_running(session, cfg, wait_seconds=10)
    except Exception as e:
        print(f"ERRO ao verificar ou ligar a VM: {e}")
        return 4

    print_step("Passo 5: pedido de sessão SPICE")
    try:
        spice_data = request_spice(session, cfg)
    except Exception as e:
        print(f"ERRO ao pedir sessão SPICE: {e}")
        return 5

    print_step("Passo 6: gerar ficheiro .vv")
    try:
        vv_text = build_vv_exact(spice_data)
        vv_file = write_vv_file(cfg, vv_text)
        print(f"Ficheiro .vv gravado em: {vv_file}")
        print("\n--- INÍCIO DO FICHEIRO .vv ---")
        print(vv_text)
        print("--- FIM DO FICHEIRO .vv ---")
    except Exception as e:
        print(f"ERRO a gerar ficheiro .vv: {e}")
        return 6

    print_step("Passo 7: executar remote-viewer")
    try:
        viewer = get_remote_viewer_command(cfg["remote_viewer_path"])
        print(f"A usar: {viewer}")
        launch_remote_viewer(viewer, vv_file, cfg["fullscreen"])
        print("remote-viewer lançado com sucesso.")
        return 0
    except Exception as e:
        print(f"ERRO ao executar remote-viewer: {e}")
        return 7


if __name__ == "__main__":
    raise SystemExit(main())