import configparser
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, scrolledtext

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def script_ini_path() -> Path:
    script_path = Path(__file__).resolve()
    script_stem = script_path.stem
    script_dir = script_path.parent

    candidates = [
        script_dir / f"{script_stem}.ini",
    ]

    base_stem = script_stem
    for suffix in ("_gui", "_cli", "_clt"):
        if script_stem.endswith(suffix):
            base_stem = script_stem[: -len(suffix)]
            break

    extra_names = [
        f"{base_stem}_gui.ini",
        f"{base_stem}_cli.ini",
        f"{base_stem}_clt.ini",
        f"{base_stem}.ini",
    ]

    for name in extra_names:
        path = script_dir / name
        if path not in candidates:
            candidates.append(path)

    for path in candidates:
        if path.exists():
            return path

    searched = "\n - ".join(str(p) for p in candidates)
    raise FileNotFoundError(
        "Nenhum ficheiro de configuração foi encontrado. "
        f"Foram procurados:\n - {searched}"
    )


def load_config() -> dict:
    ini_path = script_ini_path()

    if not ini_path.exists():
        raise FileNotFoundError(f"Ficheiro de configuração não encontrado: {ini_path}")

    parser = configparser.ConfigParser()
    parser.read(ini_path, encoding="utf-8")

    cfg = {
        "config_file": str(ini_path),
        "proxmox_host": parser.get("proxmox", "host"),
        "proxmox_port": parser.getint("proxmox", "port", fallback=8006),
        "username": parser.get("auth", "username"),
        "password": parser.get("auth", "password"),
        "verify_tls": parser.getboolean("connection", "verify_tls", fallback=False),
        "timeout": parser.getint("connection", "timeout", fallback=10),
        "proxy_scheme": parser.get("spice", "proxy_scheme", fallback="http"),
        "proxy_host": parser.get("spice", "proxy_host"),
        "proxy_port": parser.getint("spice", "proxy_port", fallback=3128),
        "remote_viewer_path": parser.get("viewer", "remote_viewer_path", fallback="").strip(),
        "fullscreen": parser.getboolean("viewer", "fullscreen", fallback=False),
        "output_dir": parser.get("output", "output_dir", fallback=".").strip(),
        "start_wait_seconds": parser.getint("vm", "start_wait_seconds", fallback=10),
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
        "remote-viewer não encontrado. Define remote_viewer_path no .ini "
        "ou instala o remote-viewer no PATH."
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


def authenticate(session: requests.Session, cfg: dict, log) -> None:
    log("Autenticação no Proxmox...")
    r = session.post(
        f"{cfg['base_url']}/access/ticket",
        data={
            "username": cfg["username"],
            "password": cfg["password"],
        },
        verify=cfg["verify_tls"],
        timeout=cfg["timeout"],
    )
    log(f"HTTP {r.status_code}")
    r.raise_for_status()

    auth_data = r.json()["data"]
    ticket = auth_data.get("ticket")
    csrf = auth_data.get("CSRFPreventionToken")

    if not ticket:
        raise RuntimeError("A autenticação não devolveu ticket.")

    session.cookies.set("PVEAuthCookie", ticket)
    if csrf:
        session.headers.update({"CSRFPreventionToken": csrf})

    log(f"Autenticação OK. Utilizador: {auth_data.get('username')}")


def list_accessible_vms(session: requests.Session, cfg: dict, log) -> list:
    log("A obter lista de VMs acessíveis...")
    r = session.get(
        f"{cfg['base_url']}/cluster/resources",
        params={"type": "vm"},
        verify=cfg["verify_tls"],
        timeout=cfg["timeout"],
    )
    log(f"HTTP {r.status_code}")
    r.raise_for_status()

    resources = r.json().get("data", [])
    vms = []

    for item in resources:
        vmid = item.get("vmid")
        node = item.get("node")
        rtype = item.get("type")

        if vmid is None or not node:
            continue
        if rtype not in ("qemu", "vm", None):
            continue

        vms.append(
            {
                "vmid": int(vmid),
                "node": str(node),
                "name": item.get("name", f"VM {vmid}"),
                "status": item.get("status", ""),
                "maxcpu": item.get("maxcpu"),
                "maxmem": item.get("maxmem"),
            }
        )

    vms.sort(key=lambda x: (x["name"].lower(), x["vmid"]))

    if not vms:
        raise RuntimeError("Não foram encontradas VMs acessíveis para este utilizador.")

    log(f"Foram encontradas {len(vms)} VM(s).")
    return vms


def get_vm_status(session: requests.Session, cfg: dict, node: str, vmid: int) -> str:
    r = session.get(
        f"{cfg['base_url']}/nodes/{node}/qemu/{vmid}/status/current",
        verify=cfg["verify_tls"],
        timeout=cfg["timeout"],
    )
    r.raise_for_status()

    status_data = r.json().get("data", {})
    status = status_data.get("status")
    if not status:
        raise RuntimeError("A API não devolveu o estado atual da VM.")

    return status


def ensure_vm_running(session: requests.Session, cfg: dict, node: str, vmid: int, log) -> None:
    status = get_vm_status(session, cfg, node, vmid)
    log(f"Estado atual da VM {vmid}: {status}")

    if status == "running":
        log("A VM já está ligada.")
        return

    if status != "stopped":
        raise RuntimeError(f"A VM está num estado inesperado: {status}")

    log(f"A iniciar VM {vmid}...")
    r = session.post(
        f"{cfg['base_url']}/nodes/{node}/qemu/{vmid}/status/start",
        verify=cfg["verify_tls"],
        timeout=cfg["timeout"],
    )
    log(f"HTTP {r.status_code}")
    r.raise_for_status()

    wait_seconds = cfg.get("start_wait_seconds", 10)
    log(f"VM iniciada. A aguardar {wait_seconds} segundos...")
    time.sleep(wait_seconds)

    status = get_vm_status(session, cfg, node, vmid)
    log(f"Estado após espera: {status}")

    if status != "running":
        raise RuntimeError(f"A VM não ficou pronta. Estado atual: {status}")


def request_spice(session: requests.Session, cfg: dict, node: str, vmid: int, log) -> dict:
    log(f"A pedir sessão SPICE para VM {vmid}...")
    r = session.post(
        f"{cfg['base_url']}/nodes/{node}/qemu/{vmid}/spiceproxy",
        verify=cfg["verify_tls"],
        timeout=cfg["timeout"],
    )
    log(f"HTTP {r.status_code}")
    r.raise_for_status()

    spice_data = r.json().get("data", {})
    if not spice_data:
        raise RuntimeError("A API não devolveu dados SPICE.")

    log(f"SPICE host: {spice_data.get('host')}")
    log(f"SPICE proxy original: {spice_data.get('proxy')}")
    log(f"SPICE tls-port: {spice_data.get('tls-port')}")

    spice_data["proxy"] = cfg["proxy_url"]
    log(f"SPICE proxy final: {spice_data.get('proxy')}")

    return spice_data


def write_vv_file(cfg: dict, vmid: int, vv_text: str) -> Path:
    output_dir = Path(os.path.expandvars(os.path.expanduser(cfg["output_dir"]))).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    vv_file = output_dir / f"vm{vmid}.vv"
    vv_file.write_text(vv_text, encoding="utf-8", newline="\n")
    return vv_file


def launch_remote_viewer(viewer_cmd: str, vv_file: Path, fullscreen: bool = False) -> None:
    cmd = [viewer_cmd]

    if fullscreen:
        cmd.append("--full-screen")

    cmd.append(str(vv_file))
    subprocess.Popen(cmd)


   
    
class ProxmoxTkApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Proxmox VM Launcher")
        self.root.geometry("900x650")
        self.center_window(900, 650)

        self.cfg = None
        self.session = requests.Session()
        self.vm_buttons = []

        self.build_ui()
        self.start_initial_load()

    def center_window(self, width: int = 900, height: int = 650):
        self.root.update_idletasks()

        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()

        x = (screen_width // 2) - (width // 2)
        y = (screen_height // 2) - (height // 2)

        self.root.geometry(f"{width}x{height}+{x}+{y}")
        
    def build_ui(self):
        top = tk.Frame(self.root, padx=10, pady=10)
        top.pack(fill="x")

        self.status_var = tk.StringVar(value="A iniciar...")
        status_label = tk.Label(
            top,
            textvariable=self.status_var,
            anchor="w",
            font=("Segoe UI", 10, "bold"),
        )
        status_label.pack(fill="x")

        mid = tk.PanedWindow(self.root, orient="horizontal", sashrelief="raised")
        mid.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        left_frame = tk.Frame(mid, bd=1, relief="solid")
        right_frame = tk.Frame(mid, bd=1, relief="solid")
        mid.add(left_frame, stretch="always")
        mid.add(right_frame, stretch="always")

        left_title = tk.Label(
            left_frame,
            text="Máquinas virtuais disponíveis",
            font=("Segoe UI", 11, "bold"),
        )
        left_title.pack(anchor="w", padx=10, pady=(10, 5))

        list_frame = tk.Frame(left_frame)
        list_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.vm_canvas = tk.Canvas(list_frame, highlightthickness=0)
        self.vm_scrollbar = tk.Scrollbar(list_frame, orient="vertical", command=self.vm_canvas.yview)
        self.vm_canvas.configure(yscrollcommand=self.vm_scrollbar.set)

        self.vm_scrollbar.pack(side="right", fill="y")
        self.vm_canvas.pack(side="left", fill="both", expand=True)

        self.vm_container = tk.Frame(self.vm_canvas)
        self.vm_canvas_window = self.vm_canvas.create_window(
            (0, 0), window=self.vm_container, anchor="nw"
        )

        self.vm_container.bind("<Configure>", self._on_vm_frame_configure)
        self.vm_canvas.bind("<Configure>", self._on_vm_canvas_configure)
        self.vm_canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        right_title = tk.Label(
            right_frame,
            text="Registo",
            font=("Segoe UI", 11, "bold"),
        )
        right_title.pack(anchor="w", padx=10, pady=(10, 5))

        self.log_box = scrolledtext.ScrolledText(
            right_frame,
            wrap="word",
            height=20,
            state="disabled",
        )
        self.log_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        bottom = tk.Frame(self.root, padx=10, pady=10)
        bottom.pack(fill="x", pady=(0, 10))

        self.refresh_btn = tk.Button(
            bottom,
            text="Atualizar lista",
            command=self.refresh_vms,
            state="disabled",
        )
        self.refresh_btn.pack(side="left")

        self.exit_btn = tk.Button(bottom, text="Fechar", command=self.root.destroy)
        self.exit_btn.pack(side="right")

    def _on_vm_frame_configure(self, event=None):
        self.vm_canvas.configure(scrollregion=self.vm_canvas.bbox("all"))

    def _on_vm_canvas_configure(self, event):
        self.vm_canvas.itemconfigure(self.vm_canvas_window, width=event.width)

    def _on_mousewheel(self, event):
        try:
            self.vm_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except Exception:
            pass

    def log(self, message: str):
        self.root.after(0, self._append_log, message)

    def _append_log(self, message: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def set_status(self, text: str):
        self.root.after(0, lambda: self.status_var.set(text))

    def clear_vm_buttons(self):
        for widget in self.vm_container.winfo_children():
            widget.destroy()
        self.vm_buttons.clear()
        self.root.after(10, self._on_vm_frame_configure)

    def add_vm_button(self, vm: dict):
        display_name = vm["name"] or f"VM {vm['vmid']}"
        status = vm.get("status", "desconhecido")
        button_text = f"{display_name}\nVMID: {vm['vmid']} | Node: {vm['node']} | Estado: {status}"

        btn = tk.Button(
            self.vm_container,
            text=button_text,
            justify="left",
            anchor="w",
            padx=10,
            pady=10,
            command=lambda v=vm: self.connect_to_vm(v),
        )
        btn.pack(fill="x", pady=4)
        self.vm_buttons.append(btn)

    def set_vm_buttons_state(self, state: str):
        for btn in self.vm_buttons:
            btn.configure(state=state)
        self.refresh_btn.configure(state=state)

    def start_initial_load(self):
        threading.Thread(target=self._initial_load_worker, daemon=True).start()

    def _initial_load_worker(self):
        self.set_status("A carregar configuração...")
        try:
            self.cfg = load_config()
            self.log(f"Configuração carregada de: {self.cfg['config_file']}")

            self.set_status("A testar ligação TCP...")
            test_tcp_connectivity(
                self.cfg["proxmox_host"],
                self.cfg["proxmox_port"],
                self.cfg["timeout"],
            )
            self.log(f"TCP OK para {self.cfg['proxmox_host']}:{self.cfg['proxmox_port']}")

            self.set_status("A autenticar...")
            authenticate(self.session, self.cfg, self.log)

            self.load_vms()

        except Exception as e:
            self.set_status("Erro na inicialização")
            self.log(f"ERRO: {e}")
            self.root.after(0, lambda: messagebox.showerror("Erro", str(e)))

    def load_vms(self):
        self.set_status("A obter lista de VMs...")
        try:
            vms = list_accessible_vms(self.session, self.cfg, self.log)
            self.root.after(0, lambda: self.populate_vm_list(vms))
            self.set_status(f"{len(vms)} VM(s) disponíveis")
        except Exception as e:
            self.set_status("Erro ao obter VMs")
            self.log(f"ERRO: {e}")
            self.root.after(0, lambda: messagebox.showerror("Erro", str(e)))

    def populate_vm_list(self, vms: list):
        self.clear_vm_buttons()
        for vm in vms:
            self.add_vm_button(vm)
        self.refresh_btn.configure(state="normal")
        self.root.after(10, self._on_vm_frame_configure)

    def refresh_vms(self):
        self.clear_vm_buttons()
        self.set_status("A atualizar lista...")
        threading.Thread(target=self.load_vms, daemon=True).start()

    def connect_to_vm(self, vm: dict):
        threading.Thread(target=self._connect_to_vm_worker, args=(vm,), daemon=True).start()

    def _connect_to_vm_worker(self, vm: dict):
        try:
            self.root.after(0, lambda: self.set_vm_buttons_state("disabled"))

            name = vm["name"] or f"VM {vm['vmid']}"
            node = vm["node"]
            vmid = vm["vmid"]

            self.set_status(f"A ligar a {name}...")
            self.log("")
            self.log("=" * 60)
            self.log(f"Ligação solicitada para: {name} (VMID {vmid}, node {node})")

            ensure_vm_running(self.session, self.cfg, node, vmid, self.log)

            spice_data = request_spice(self.session, self.cfg, node, vmid, self.log)

            vv_text = build_vv_exact(spice_data)
            vv_file = write_vv_file(self.cfg, vmid, vv_text)
            self.log(f"Ficheiro .vv gravado em: {vv_file}")

            viewer = get_remote_viewer_command(self.cfg["remote_viewer_path"])
            self.log(f"A usar viewer: {viewer}")

            launch_remote_viewer(viewer, vv_file, self.cfg["fullscreen"])

            self.set_status(f"Ligação iniciada para {name}")
            self.log("remote-viewer iniciado com sucesso.")

        except Exception as e:
            self.set_status("Erro ao ligar à VM")
            self.log(f"ERRO: {e}")
            self.root.after(0, lambda: messagebox.showerror("Erro", str(e)))
        finally:
            self.root.after(0, lambda: self.set_vm_buttons_state("normal"))


def main() -> int:
    root = tk.Tk()
    root.withdraw()
    app = ProxmoxTkApp(root)
    root.deiconify()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
