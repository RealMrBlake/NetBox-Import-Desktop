import ssl
import atexit
import ipaddress
import json
import os
import sys
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
import pynetbox
from profile_store import ProfileStore


APP_NAME = "NetBox VM Import"


def get_config_path():
    """Return a per-user config path that also works in a packaged EXE."""
    if sys.platform == "win32":
        base_dir = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base_dir / APP_NAME / "config.json"


class ESXiNetBoxImporter:

    def __init__(self, root):
        self.root = root
        self.root.title("VMware → NetBox Synchronizer")
        self.root.geometry("1080x820")
        self.root.minsize(900, 680)

        self.si = None
        self.nb = None
        self.vms = []

        self.site_map = {}
        self.cluster_map = {}
        self.tenant_map = {}
        self.platforms = []

        self.save_credentials = tk.BooleanVar(value=False)
        self.ignore_ssl_errors = tk.BooleanVar(value=False)
        self.config_path = get_config_path()
        self.profile_name = tk.StringVar()
        self.status_text = tk.StringVar(value="Bereit")
        self.store = ProfileStore()
        self.profiles = {}

        self.configure_style()
        self.create_gui()
        self.load_profiles()
        self.set_window_icon()

    # =========================================================
    # GUI
    # =========================================================

    def create_gui(self):

        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        # ============================================
        # ESXi / vCenter
        # ============================================

        ttk.Label(main, text="ESXi / vCenter Host").grid(
            row=0,
            column=0,
            sticky="w"
        )

        self.esxi_host = ttk.Entry(main, width=45)
        self.esxi_host.grid(row=0, column=1, sticky="ew")

        ttk.Label(main, text="Username").grid(
            row=1,
            column=0,
            sticky="w"
        )

        self.esxi_user = ttk.Entry(main, width=45)
        self.esxi_user.grid(row=1, column=1, sticky="ew")

        ttk.Label(main, text="Password").grid(
            row=2,
            column=0,
            sticky="w"
        )

        self.esxi_pass = ttk.Entry(main, width=45, show="*")
        self.esxi_pass.grid(row=2, column=1, sticky="ew")

        # ============================================
        # NetBox
        # ============================================

        ttk.Label(main, text="NetBox URL").grid(
            row=3,
            column=0,
            sticky="w"
        )

        self.nb_url = ttk.Entry(main, width=45)
        self.nb_url.grid(row=3, column=1, sticky="ew")

        ttk.Label(main, text="NetBox Token").grid(
            row=4,
            column=0,
            sticky="w"
        )

        self.nb_token = ttk.Entry(main, width=45, show="*")
        self.nb_token.grid(row=4, column=1, sticky="ew")

        options = ttk.Frame(main)
        options.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))

        ttk.Checkbutton(
            options,
            text="Verbindungsdaten speichern (inkl. Passwort und Token)",
            variable=self.save_credentials
        ).pack(anchor="w")

        ttk.Checkbutton(
            options,
            text="TLS-Zertifikatsprüfung deaktivieren (z. B. selbstsignierte Zertifikate)",
            variable=self.ignore_ssl_errors
        ).pack(anchor="w")

        ttk.Button(
            main,
            text="Verbinden & Daten laden",
            command=self.connect_all
        ).grid(
            row=6,
            column=1,
            sticky="e",
            pady=10
        )

        # ============================================
        # Zielauswahl
        # ============================================

        ttk.Label(main, text="Site").grid(
            row=7,
            column=0,
            sticky="w"
        )

        self.site_combo = ttk.Combobox(main, width=42)
        self.site_combo.grid(row=7, column=1, sticky="ew")

        ttk.Label(main, text="Cluster").grid(
            row=8,
            column=0,
            sticky="w"
        )

        self.cluster_combo = ttk.Combobox(main, width=42)
        self.cluster_combo.grid(row=8, column=1, sticky="ew")

        ttk.Label(main, text="Tenant").grid(
            row=9,
            column=0,
            sticky="w"
        )

        self.tenant_combo = ttk.Combobox(main, width=42)
        self.tenant_combo.grid(row=9, column=1, sticky="ew")

        # ============================================
        # VM Liste
        # ============================================

        ttk.Label(main, text="Gefundene VMs").grid(
            row=10,
            column=0,
            sticky="w",
            pady=(15, 5)
        )

        self.vm_list = tk.Listbox(
            main,
            selectmode=tk.MULTIPLE,
            width=100,
            height=25
        )

        self.vm_list.grid(
            row=11,
            column=0,
            columnspan=2,
            sticky="nsew"
        )

        scrollbar = ttk.Scrollbar(
            main,
            orient="vertical",
            command=self.vm_list.yview
        )

        scrollbar.grid(row=11, column=2, sticky="ns")

        self.vm_list.config(yscrollcommand=scrollbar.set)

        # ============================================
        # Buttons
        # ============================================

        btn_frame = ttk.Frame(main)

        btn_frame.grid(
            row=12,
            column=0,
            columnspan=2,
            pady=10,
            sticky="e"
        )

        ttk.Button(
            btn_frame,
            text="Alle auswählen",
            command=self.select_all
        ).pack(side="left", padx=5)

        ttk.Button(
            btn_frame,
            text="Import / Sync starten",
            command=self.import_vms
        ).pack(side="left", padx=5)

        # ============================================
        # Logfeld
        # ============================================

        ttk.Label(main, text="Log").grid(
            row=13,
            column=0,
            sticky="w"
        )

        self.log_text = tk.Text(main, height=10)

        self.log_text.grid(
            row=14,
            column=0,
            columnspan=2,
            sticky="nsew"
        )

        main.columnconfigure(1, weight=1)
        main.rowconfigure(11, weight=1)

    # =========================================================
    # Einstellungen
    # =========================================================

    def load_settings(self):
        if not self.config_path.exists():
            return

        try:
            with self.config_path.open("r", encoding="utf-8") as config_file:
                settings = json.load(config_file)

            fields = (
                (self.esxi_host, "esxi_host"),
                (self.esxi_user, "esxi_user"),
                (self.esxi_pass, "esxi_password"),
                (self.nb_url, "netbox_url"),
                (self.nb_token, "netbox_token"),
            )
            for widget, key in fields:
                widget.insert(0, settings.get(key, ""))

            self.save_credentials.set(True)
            self.ignore_ssl_errors.set(
                bool(settings.get("ignore_ssl_errors", False))
            )
        except (OSError, ValueError) as error:
            messagebox.showwarning(
                "Konfiguration",
                f"Gespeicherte Einstellungen konnten nicht geladen werden:\n{error}"
            )

    def save_settings(self):
        if not self.save_credentials.get():
            if self.config_path.exists():
                self.config_path.unlink()
            return

        settings = {
            "esxi_host": self.esxi_host.get().strip(),
            "esxi_user": self.esxi_user.get().strip(),
            "esxi_password": self.esxi_pass.get(),
            "netbox_url": self.nb_url.get().strip(),
            "netbox_token": self.nb_token.get(),
            "ignore_ssl_errors": self.ignore_ssl_errors.get(),
        }
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with self.config_path.open("w", encoding="utf-8") as config_file:
            json.dump(settings, config_file, indent=2)

    # The following GUI/profile methods intentionally replace the original
    # single-connection UI while keeping the import logic below unchanged.

    def configure_style(self):
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 20))
        style.configure("Subtitle.TLabel", foreground="#52606d", font=("Segoe UI", 10))
        style.configure("Card.TLabelframe", padding=14)
        style.configure("Card.TLabelframe.Label", font=("Segoe UI Semibold", 11))
        style.configure("Primary.TButton", font=("Segoe UI Semibold", 10), padding=(14, 8))
        style.configure("Status.TLabel", padding=(10, 6), foreground="#334e68")

    def resource_path(self, filename):
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        return base / filename

    def set_window_icon(self):
        icon = self.resource_path("netbox_vm_import.ico")
        if icon.exists():
            try:
                self.root.iconbitmap(str(icon))
            except tk.TclError:
                pass

    def create_gui(self):
        main = ttk.Frame(self.root, padding=18)
        main.pack(fill="both", expand=True)

        header = ttk.Frame(main)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        ttk.Label(header, text="VMware → NetBox", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="Virtuelle Maschinen kundenbezogen synchronisieren", style="Subtitle.TLabel").pack(anchor="w")

        profile_card = ttk.LabelFrame(main, text="Kundenprofil", style="Card.TLabelframe")
        profile_card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.profile_combo = ttk.Combobox(profile_card, textvariable=self.profile_name, state="readonly", width=34)
        self.profile_combo.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.profile_combo.bind("<<ComboboxSelected>>", self.on_profile_selected)
        ttk.Button(profile_card, text="Neu", command=self.new_profile).pack(side="left", padx=3)
        ttk.Button(profile_card, text="Speichern", command=self.save_profile).pack(side="left", padx=3)
        ttk.Button(profile_card, text="Löschen", command=self.delete_profile).pack(side="left", padx=(3, 0))

        connections = ttk.Frame(main)
        connections.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        connections.columnconfigure(0, weight=1, uniform="connections")
        connections.columnconfigure(1, weight=1, uniform="connections")
        vmware = ttk.LabelFrame(connections, text="VMware vSphere / ESXi", style="Card.TLabelframe")
        netbox = ttk.LabelFrame(connections, text="NetBox", style="Card.TLabelframe")
        vmware.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        netbox.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self.esxi_host = self.add_field(vmware, 0, "Host / vCenter")
        self.esxi_user = self.add_field(vmware, 1, "Benutzer")
        self.esxi_pass = self.add_field(vmware, 2, "Passwort", secret=True)
        self.nb_url = self.add_field(netbox, 0, "URL")
        self.nb_token = self.add_field(netbox, 1, "API-Token", secret=True)
        ttk.Checkbutton(netbox, text="Zertifikatsprüfung deaktivieren", variable=self.ignore_ssl_errors).grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))

        action_row = ttk.Frame(main)
        action_row.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(action_row, text="Passwort und Token werden mit Windows DPAPI verschlüsselt.", style="Subtitle.TLabel").pack(side="left")
        ttk.Button(action_row, text="Verbinden & Daten laden", command=self.connect_all, style="Primary.TButton").pack(side="right")

        workspace = ttk.Panedwindow(main, orient="horizontal")
        workspace.grid(row=4, column=0, sticky="nsew")
        targets = ttk.LabelFrame(workspace, text="NetBox-Ziel", style="Card.TLabelframe")
        vm_card = ttk.LabelFrame(workspace, text="Gefundene virtuelle Maschinen", style="Card.TLabelframe")
        workspace.add(targets, weight=1)
        workspace.add(vm_card, weight=3)
        self.site_combo = self.add_combo_field(targets, 0, "Site")
        self.cluster_combo = self.add_combo_field(targets, 1, "Cluster")
        self.tenant_combo = self.add_combo_field(targets, 2, "Tenant")
        targets.columnconfigure(0, weight=1)
        self.vm_list = tk.Listbox(vm_card, selectmode=tk.MULTIPLE, height=14, borderwidth=0, highlightthickness=1, highlightcolor="#2f80ed", font=("Segoe UI", 10))
        vm_scroll = ttk.Scrollbar(vm_card, orient="vertical", command=self.vm_list.yview)
        self.vm_list.configure(yscrollcommand=vm_scroll.set)
        self.vm_list.pack(side="left", fill="both", expand=True)
        vm_scroll.pack(side="right", fill="y")

        buttons = ttk.Frame(main)
        buttons.grid(row=5, column=0, sticky="ew", pady=10)
        ttk.Button(buttons, text="Alle auswählen", command=self.select_all).pack(side="left")
        ttk.Button(buttons, text="Import / Sync starten", command=self.import_vms, style="Primary.TButton").pack(side="right")

        log_card = ttk.LabelFrame(main, text="Aktivitätsprotokoll", style="Card.TLabelframe")
        log_card.grid(row=6, column=0, sticky="nsew")
        self.log_text = tk.Text(log_card, height=7, borderwidth=0, bg="#f7f9fb", foreground="#243b53", font=("Consolas", 9), state="disabled")
        self.log_text.pack(fill="both", expand=True)
        ttk.Label(main, textvariable=self.status_text, style="Status.TLabel", anchor="w").grid(row=7, column=0, sticky="ew", pady=(6, 0))
        main.columnconfigure(0, weight=1)
        main.rowconfigure(4, weight=3)
        main.rowconfigure(6, weight=1)

    def add_field(self, parent, row, label, secret=False):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=5)
        entry = ttk.Entry(parent, show="•" if secret else "")
        entry.grid(row=row, column=1, sticky="ew", pady=5)
        parent.columnconfigure(1, weight=1)
        return entry

    def add_combo_field(self, parent, row, label):
        ttk.Label(parent, text=label).grid(row=row * 2, column=0, sticky="w", pady=(5, 2))
        combo = ttk.Combobox(parent, state="readonly")
        combo.grid(row=row * 2 + 1, column=0, sticky="ew", pady=(0, 8))
        return combo

    def load_profiles(self):
        try:
            self.profiles, active = self.store.load()
            self.refresh_profile_list()
            if self.profiles:
                selected = active if active in self.profiles else next(iter(self.profiles))
                self.profile_name.set(selected)
                self.populate_profile(selected)
        except Exception as error:
            messagebox.showwarning("Konfiguration", f"Profile konnten nicht geladen werden:\n{error}")

    def refresh_profile_list(self):
        self.profile_combo["values"] = sorted(self.profiles, key=str.casefold)

    def profile_from_form(self, name):
        return {
            "name": name, "esxi_host": self.esxi_host.get().strip(),
            "esxi_user": self.esxi_user.get().strip(), "esxi_password": self.esxi_pass.get(),
            "netbox_url": self.nb_url.get().strip(), "netbox_token": self.nb_token.get(),
            "ignore_ssl_errors": self.ignore_ssl_errors.get(),
        }

    def populate_profile(self, name):
        profile = self.profiles.get(name, {})
        for widget, key in ((self.esxi_host, "esxi_host"), (self.esxi_user, "esxi_user"), (self.esxi_pass, "esxi_password"), (self.nb_url, "netbox_url"), (self.nb_token, "netbox_token")):
            widget.delete(0, tk.END)
            widget.insert(0, profile.get(key, ""))
        self.ignore_ssl_errors.set(bool(profile.get("ignore_ssl_errors", False)))
        self.status_text.set(f"Profil „{name}“ geladen")

    def on_profile_selected(self, _event=None):
        self.populate_profile(self.profile_name.get())

    def new_profile(self):
        name = simpledialog.askstring("Neues Kundenprofil", "Kunden- oder Profilname:", parent=self.root)
        if not name or not name.strip():
            return
        name = name.strip()
        if name in self.profiles and not messagebox.askyesno("Profil vorhanden", "Dieses Profil überschreiben?"):
            return
        self.clear_profile_form()
        self.profile_name.set(name)
        self.profiles[name] = self.profile_from_form(name)
        self.refresh_profile_list()
        self.status_text.set(f"Neues Profil „{name}“ – Daten eintragen und speichern")

    def clear_profile_form(self):
        for widget in (self.esxi_host, self.esxi_user, self.esxi_pass, self.nb_url, self.nb_token):
            widget.delete(0, tk.END)
        self.ignore_ssl_errors.set(False)

    def save_profile(self, show_message=True):
        name = self.profile_name.get().strip()
        if not name:
            messagebox.showwarning("Profil", "Bitte zuerst ein Kundenprofil anlegen.")
            return False
        self.profiles[name] = self.profile_from_form(name)
        try:
            self.store.save(self.profiles, name)
            self.refresh_profile_list()
            self.status_text.set(f"Profil „{name}“ verschlüsselt gespeichert")
            if show_message:
                messagebox.showinfo("Gespeichert", "Das Kundenprofil wurde verschlüsselt gespeichert.")
            return True
        except Exception as error:
            messagebox.showerror("Speichern fehlgeschlagen", str(error))
            return False

    def delete_profile(self):
        name = self.profile_name.get()
        if not name or name not in self.profiles:
            return
        if not messagebox.askyesno("Profil löschen", f"Kundenprofil „{name}“ wirklich löschen?"):
            return
        del self.profiles[name]
        next_name = next(iter(sorted(self.profiles, key=str.casefold)), "")
        self.store.save(self.profiles, next_name)
        self.refresh_profile_list()
        self.profile_name.set(next_name)
        if next_name:
            self.populate_profile(next_name)
        else:
            self.clear_profile_form()
        self.status_text.set("Profil gelöscht")

    # =========================================================
    # Logging
    # =========================================================

    def log(self, text):
        self.log_text.config(state="normal")
        self.log_text.insert(tk.END, f"{text}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state="disabled")
        self.status_text.set(text)
        self.root.update()

    # =========================================================
    # Select All
    # =========================================================

    def select_all(self):

        self.vm_list.select_set(0, tk.END)

    # =========================================================
    # Verbindungen
    # =========================================================

    def connect_all(self):

        try:
            if not all((self.esxi_host.get().strip(), self.esxi_user.get().strip(), self.esxi_pass.get(), self.nb_url.get().strip(), self.nb_token.get())):
                raise ValueError("Bitte alle VMware- und NetBox-Verbindungsdaten ausfüllen.")
            if not self.save_profile(show_message=False):
                return

            self.connect_esxi()
            self.connect_netbox()

            self.load_netbox_data()
            self.load_vms()

            messagebox.showinfo(
                "Erfolg",
                "Verbindung erfolgreich hergestellt"
            )

        except Exception as e:

            messagebox.showerror(
                "Fehler",
                str(e)
            )

    def connect_esxi(self):

        self.log("Verbinde zu ESXi/vCenter...")

        context = None
        if self.ignore_ssl_errors.get():
            context = ssl._create_unverified_context()

        self.si = SmartConnect(
            host=self.esxi_host.get(),
            user=self.esxi_user.get(),
            pwd=self.esxi_pass.get(),
            sslContext=context
        )

        atexit.register(Disconnect, self.si)

        self.log("ESXi Verbindung erfolgreich")

    def connect_netbox(self):

        self.log("Verbinde zu NetBox...")

        self.nb = pynetbox.api(
            self.nb_url.get(),
            token=self.nb_token.get()
        )

        self.nb.http_session.verify = not self.ignore_ssl_errors.get()

        self.log("NetBox Verbindung erfolgreich")

    # =========================================================
    # NetBox Daten
    # =========================================================

    def load_netbox_data(self):

        self.log("Lade NetBox Daten...")

        # Sites
        sites = list(self.nb.dcim.sites.all())
        self.site_map = {x.name: x.id for x in sites}
        self.site_combo["values"] = list(self.site_map.keys())

        # Cluster
        clusters = list(self.nb.virtualization.clusters.all())
        self.cluster_map = {x.name: x.id for x in clusters}
        self.cluster_combo["values"] = list(self.cluster_map.keys())

        # Tenant
        tenants = list(self.nb.tenancy.tenants.all())
        self.tenant_map = {x.name: x.id for x in tenants}
        self.tenant_combo["values"] = list(self.tenant_map.keys())

        # Plattformen
        self.platforms = list(self.nb.dcim.platforms.all())

        self.log("NetBox Daten geladen")

    # =========================================================
    # VMware VMs laden
    # =========================================================

    def load_vms(self):

        self.log("Lade VMs aus VMware...")

        content = self.si.RetrieveContent()

        container = content.rootFolder

        view_type = [vim.VirtualMachine]

        recursive = True

        container_view = content.viewManager.CreateContainerView(
            container,
            view_type,
            recursive
        )

        self.vms = container_view.view

        self.vm_list.delete(0, tk.END)

        for vm in self.vms:
            self.vm_list.insert(tk.END, vm.name)

        self.log(f"{len(self.vms)} VMs gefunden")

    # =========================================================
    # Plattform Mapping
    # =========================================================

    def get_platform_id(self, guest_os):

        if not guest_os:
            return None

        guest_os = guest_os.lower()

        for platform in self.platforms:

            if platform.name.lower() in guest_os:
                return platform.id

        return None

    # =========================================================
    # Prefix automatisch ergänzen
    # =========================================================

    def get_ip_with_prefix(self, ip):

        try:

            addr = ipaddress.ip_address(ip)

            if addr.version == 4:
                return f"{ip}/24"

            return f"{ip}/64"

        except:
            return None

    # =========================================================
    # VM Import / Sync
    # =========================================================

    def import_vms(self):

        selected = self.vm_list.curselection()

        if not selected:

            messagebox.showwarning(
                "Hinweis",
                "Keine VMs ausgewählt"
            )

            return

        cluster_name = self.cluster_combo.get()
        tenant_name = self.tenant_combo.get()

        cluster_id = self.cluster_map.get(cluster_name)
        tenant_id = self.tenant_map.get(tenant_name)

        if not cluster_id:

            messagebox.showerror(
                "Fehler",
                "Bitte Cluster auswählen"
            )

            return

        for idx in selected:

            vm = self.vms[idx]

            self.log(f"Synchronisiere VM: {vm.name}")

            # ============================================
            # Hardwaredaten
            # ============================================

            memory = int(vm.config.hardware.memoryMB)

            vcpus = int(vm.config.hardware.numCPU)

            # ============================================
            # Disk in MB (NetBox >= 4.x)
            # ============================================

            disk_size = 0

            for dev in vm.config.hardware.device:

                if isinstance(dev, vim.vm.device.VirtualDisk):

                    # VMware liefert KB
                    # NetBox erwartet MB
                    size_mb = dev.capacityInKB / 1024

                    disk_size += round(size_mb)

            # ============================================
            # Betriebssystem
            # ============================================

            guest_os = ""

            try:
                guest_os = vm.config.guestFullName
            except:
                pass

            platform_id = self.get_platform_id(guest_os)

            # ============================================
            # Status
            # ============================================

            power_state = str(vm.runtime.powerState)

            if "poweredOn" in power_state:
                status = "active"
            else:
                status = "offline"

            # ============================================
            # VM Daten
            # ============================================

            vm_data = {
                "name": vm.name,
                "cluster": cluster_id,
                "status": status,
                "vcpus": vcpus,
                "memory": memory,
                "disk": disk_size,
                "tenant": tenant_id,
                "platform": platform_id
            }

            # ============================================
            # VM erstellen / aktualisieren
            # ============================================

            nb_vm = self.nb.virtualization.virtual_machines.get(
                name=vm.name
            )

            if nb_vm:

                nb_vm.update(vm_data)

                self.log(f"VM aktualisiert: {vm.name}")

            else:

                nb_vm = self.nb.virtualization.virtual_machines.create(
                    vm_data
                )

                self.log(f"VM erstellt: {vm.name}")

            # ============================================
            # Interfaces + IPs
            # ============================================

            primary_ipv4 = None
            primary_ipv6 = None

            if vm.guest and hasattr(vm.guest, "net"):

                for net in vm.guest.net:

                    mac = getattr(net, "macAddress", None)

                    if not mac:
                        continue

                    interface_name = getattr(net, "device", None)

                    if not interface_name:
                        interface_name = f"NIC-{mac[-5:]}"

                    iface_data = {
                        "virtual_machine": nb_vm.id,
                        "name": interface_name,
                        "mac_address": mac,
                        "enabled": True
                    }

                    # Interface zuerst über Namen suchen
                    existing_iface = self.nb.virtualization.interfaces.get(
                        virtual_machine_id=nb_vm.id,
                        name=interface_name
                    )

                    if existing_iface:

                        # Falls MAC geändert wurde -> aktualisieren
                        existing_iface.update(iface_data)

                        vm_iface = existing_iface

                        self.log(
                            f"Interface aktualisiert: {interface_name}"
                        )

                    else:

                        vm_iface = self.nb.virtualization.interfaces.create(
                            iface_data
                        )

                        self.log(
                            f"Interface erstellt: {interface_name}"
                        )

                    self.log(f"Interface synchronisiert: {mac}")

                    # ============================================
                    # IPs
                    # ============================================

                    if hasattr(net, "ipAddress"):

                        for ip in net.ipAddress:

                            if ip.startswith("127."):
                                continue

                            if ip == "::1":
                                continue

                            address = self.get_ip_with_prefix(ip)

                            if not address:
                                continue

                            existing_ip = self.nb.ipam.ip_addresses.get(
                                address=address
                            )

                            ip_data = {
                                "address": address,
                                "status": "active",
                                "assigned_object_type": "virtualization.vminterface",
                                "assigned_object_id": vm_iface.id
                            }

                            if existing_ip:

                                existing_ip.update(ip_data)

                                ip_obj = existing_ip

                                self.log(f"IP aktualisiert: {address}")

                            else:

                                ip_obj = self.nb.ipam.ip_addresses.create(
                                    ip_data
                                )

                                self.log(f"IP erstellt: {address}")

                            # ============================================
                            # Primary IP setzen
                            # ============================================

                            try:

                                parsed_ip = ipaddress.ip_address(ip)

                                if parsed_ip.version == 4:

                                    if not primary_ipv4:
                                        primary_ipv4 = ip_obj.id

                                else:

                                    if not primary_ipv6:
                                        primary_ipv6 = ip_obj.id

                            except:
                                pass

            # ============================================
            # Primäre IPs setzen
            # ============================================

            update_data = {}

            if primary_ipv4:
                update_data["primary_ip4"] = primary_ipv4

            if primary_ipv6:
                update_data["primary_ip6"] = primary_ipv6

            if update_data:

                nb_vm.update(update_data)

                self.log(
                    f"Primary IPs gesetzt für {vm.name}"
                )

        messagebox.showinfo(
            "Fertig",
            "VM Import / Synchronisierung abgeschlossen"
        )

        self.log("Synchronisierung abgeschlossen")


# =========================================================
# Main
# =========================================================

if __name__ == "__main__":

    root = tk.Tk()

    app = ESXiNetBoxImporter(root)

    root.mainloop()
