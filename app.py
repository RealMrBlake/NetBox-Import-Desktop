import ipaddress
import re
import ssl
import sys
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import pynetbox
import requests
from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim

from profile_store import ProfileStore
from switch_sync import PLATFORMS, discover_switch, sync_snapshot_to_netbox


class SourceDialog(tk.Toplevel):
    """Editor for one VMware or Proxmox connection."""

    def __init__(self, parent, source=None):
        super().__init__(parent)
        self.title("Virtualisierungsquelle")
        self.geometry("520x440")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result = None
        source = source or {}

        self.source_id = source.get("id", uuid.uuid4().hex)
        self.source_type = tk.StringVar(value=source.get("type", "vmware"))
        self.enabled = tk.BooleanVar(value=source.get("enabled", True))
        self.ignore_ssl = tk.BooleanVar(value=source.get("ignore_ssl", False))
        self.include_lxc = tk.BooleanVar(value=source.get("include_lxc", True))

        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="Quellentyp").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=7)
        type_combo = ttk.Combobox(
            body, textvariable=self.source_type, state="readonly",
            values=("vmware", "proxmox"),
        )
        type_combo.grid(row=0, column=1, sticky="ew", pady=7)
        type_combo.bind("<<ComboboxSelected>>", self.update_labels)

        self.name_entry = self.add_entry(body, 1, "Anzeigename", source.get("name", ""))
        self.endpoint_label = ttk.Label(body, text="Host / vCenter")
        self.endpoint_label.grid(row=2, column=0, sticky="w", padx=(0, 12), pady=7)
        self.endpoint_entry = ttk.Entry(body)
        self.endpoint_entry.grid(row=2, column=1, sticky="ew", pady=7)
        self.endpoint_entry.insert(0, source.get("host", source.get("url", "")))

        self.identity_label = ttk.Label(body, text="Benutzer")
        self.identity_label.grid(row=3, column=0, sticky="w", padx=(0, 12), pady=7)
        self.identity_entry = ttk.Entry(body)
        self.identity_entry.grid(row=3, column=1, sticky="ew", pady=7)
        self.identity_entry.insert(0, source.get("username", source.get("token_id", "")))

        self.secret_label = ttk.Label(body, text="Passwort")
        self.secret_label.grid(row=4, column=0, sticky="w", padx=(0, 12), pady=7)
        self.secret_entry = ttk.Entry(body, show="•")
        self.secret_entry.grid(row=4, column=1, sticky="ew", pady=7)
        self.secret_entry.insert(0, source.get("password", source.get("token_secret", "")))

        ttk.Checkbutton(body, text="Quelle aktivieren", variable=self.enabled).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(12, 4)
        )
        ttk.Checkbutton(body, text="TLS-Zertifikatsprüfung deaktivieren", variable=self.ignore_ssl).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=4
        )
        self.lxc_check = ttk.Checkbutton(body, text="LXC-Container mit einlesen", variable=self.include_lxc)
        self.lxc_check.grid(row=7, column=0, columnspan=2, sticky="w", pady=4)
        self.help_text = ttk.Label(body, foreground="#52606d", wraplength=470)
        self.help_text.grid(row=8, column=0, columnspan=2, sticky="w", pady=(12, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=9, column=0, columnspan=2, sticky="e", pady=(20, 0))
        ttk.Button(buttons, text="Abbrechen", command=self.destroy).pack(side="left", padx=4)
        ttk.Button(buttons, text="Übernehmen", command=self.accept, style="Primary.TButton").pack(side="left", padx=4)
        self.update_labels()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_window(self)

    @staticmethod
    def add_entry(parent, row, label, value):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=7)
        entry = ttk.Entry(parent)
        entry.grid(row=row, column=1, sticky="ew", pady=7)
        entry.insert(0, value)
        return entry

    def update_labels(self, _event=None):
        proxmox = self.source_type.get() == "proxmox"
        self.endpoint_label.config(text="Proxmox-URL" if proxmox else "Host / vCenter")
        self.identity_label.config(text="API-Token-ID" if proxmox else "Benutzer")
        self.secret_label.config(text="API-Token-Secret" if proxmox else "Passwort")
        self.help_text.config(text=(
            "Beispiel: https://pve.example.de:8006 · Token-ID: user@pve!netbox"
            if proxmox else "Beispiel: vcenter.example.de oder 192.0.2.10"
        ))
        if proxmox:
            self.lxc_check.state(["!disabled"])
        else:
            self.lxc_check.state(["disabled"])

    def accept(self):
        name = self.name_entry.get().strip()
        endpoint = self.endpoint_entry.get().strip()
        identity = self.identity_entry.get().strip()
        secret = self.secret_entry.get()
        if not all((name, endpoint, identity, secret)):
            messagebox.showwarning("Fehlende Angaben", "Bitte alle Verbindungsfelder ausfüllen.", parent=self)
            return
        source_type = self.source_type.get()
        self.result = {
            "id": self.source_id,
            "type": source_type,
            "name": name,
            "enabled": self.enabled.get(),
            "ignore_ssl": self.ignore_ssl.get(),
        }
        if source_type == "vmware":
            self.result.update({"host": endpoint, "username": identity, "password": secret})
        else:
            if not endpoint.startswith(("http://", "https://")):
                endpoint = f"https://{endpoint}"
            self.result.update({
                "url": endpoint.rstrip("/"), "token_id": identity,
                "token_secret": secret, "include_lxc": self.include_lxc.get(),
            })
        self.destroy()


class SwitchSourceDialog(tk.Toplevel):
    """Editor for one existing NetBox switch and its SSH connection."""

    def __init__(self, parent, devices, switch=None):
        super().__init__(parent)
        self.title("Switch-Zugang")
        self.geometry("620x560")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result = None
        switch = switch or {}
        self.switch_id = switch.get("id", uuid.uuid4().hex)
        self.enabled = tk.BooleanVar(value=switch.get("enabled", True))
        self.platform = tk.StringVar(value=switch.get("platform", "aruba_cx"))
        self.device_map = {}
        self.device_options = []

        selected_device = ""
        selected_tenant = "Alle Mandanten"
        for device in devices:
            site = getattr(getattr(device, "site", None), "name", "")
            tenant = getattr(getattr(device, "tenant", None), "name", "") or "Ohne Mandant"
            parts = [device.name]
            if site:
                parts.append(site)
            parts.append(tenant)
            display = " · ".join(parts)
            option = {
                "display": display,
                "device_id": device.id,
                "device_name": device.name,
                "tenant": tenant,
            }
            self.device_options.append(option)
            self.device_map[display] = (device.id, device.name)
            if device.id == switch.get("device_id"):
                selected_device = display
                selected_tenant = tenant

        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        tenants = ["Alle Mandanten"] + sorted(
            {option["tenant"] for option in self.device_options},
            key=str.casefold,
        )
        ttk.Label(body, text="Mandant").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=7)
        self.tenant_filter = tk.StringVar(value=selected_tenant)
        tenant_combo = ttk.Combobox(body, textvariable=self.tenant_filter, values=tenants, state="readonly")
        tenant_combo.grid(row=0, column=1, sticky="ew", pady=7)
        tenant_combo.bind("<<ComboboxSelected>>", self.on_tenant_changed)

        ttk.Label(body, text="NetBox-Gerät suchen").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=7)
        self.device_selection = tk.StringVar(value=selected_device)
        self.device_combo = ttk.Combobox(
            body,
            textvariable=self.device_selection,
            values=[],
        )
        self.device_combo.grid(row=1, column=1, sticky="ew", pady=7)
        self.device_combo.bind("<KeyRelease>", self.filter_devices)

        ttk.Label(body, text="Plattform").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=7)
        platform_map = {label: key for key, label in PLATFORMS.items()}
        self.platform_map = platform_map
        platform_display = tk.StringVar(value=PLATFORMS[self.platform.get()])
        self.platform_display = platform_display
        ttk.Combobox(
            body,
            textvariable=platform_display,
            values=list(platform_map),
            state="readonly",
        ).grid(row=2, column=1, sticky="ew", pady=7)

        self.host_entry = self.add_entry(body, 3, "Host / IP", switch.get("host", ""))
        self.port_entry = self.add_entry(body, 4, "SSH-Port", str(switch.get("port", 22)))
        self.username_entry = self.add_entry(body, 5, "Benutzer", switch.get("username", ""))
        self.password_entry = self.add_entry(body, 6, "Passwort", switch.get("password", ""), secret=True)
        ttk.Checkbutton(body, text="Switch aktivieren", variable=self.enabled).grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(12, 4)
        )
        ttk.Label(
            body,
            text="Das Passwort wird im Kundenprofil mit Windows DPAPI verschlüsselt gespeichert.",
            foreground="#52606d",
            wraplength=500,
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(12, 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=9, column=0, columnspan=2, sticky="e", pady=(22, 0))
        ttk.Button(buttons, text="Abbrechen", command=self.destroy).pack(side="left", padx=4)
        ttk.Button(buttons, text="Übernehmen", command=self.accept, style="Primary.TButton").pack(side="left", padx=4)
        self.update_device_values()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_window(self)

    def matching_device_options(self, query=""):
        tenant = self.tenant_filter.get()
        query = query.strip().casefold()
        return [
            option for option in self.device_options
            if (tenant == "Alle Mandanten" or option["tenant"] == tenant)
            and (not query or query in option["display"].casefold())
        ]

    def update_device_values(self, query=""):
        self.device_combo["values"] = [option["display"] for option in self.matching_device_options(query)]

    def filter_devices(self, _event=None):
        self.update_device_values(self.device_selection.get())

    def on_tenant_changed(self, _event=None):
        current = self.device_selection.get()
        allowed = {option["display"] for option in self.matching_device_options()}
        if current not in allowed:
            self.device_selection.set("")
        self.update_device_values()

    @staticmethod
    def add_entry(parent, row, label, value, secret=False):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=7)
        entry = ttk.Entry(parent, show="•" if secret else "")
        entry.grid(row=row, column=1, sticky="ew", pady=7)
        entry.insert(0, value)
        return entry

    def accept(self):
        device = self.device_map.get(self.device_selection.get())
        host = self.host_entry.get().strip()
        username = self.username_entry.get().strip()
        password = self.password_entry.get()
        try:
            port = int(self.port_entry.get())
        except ValueError:
            port = 0
        if not device or not host or not username or not password or not 1 <= port <= 65535:
            messagebox.showwarning("Fehlende Angaben", "Bitte alle Switch-Zugangsdaten korrekt ausfüllen.", parent=self)
            return
        device_id, device_name = device
        self.result = {
            "id": self.switch_id,
            "name": device_name,
            "device_id": device_id,
            "platform": self.platform_map[self.platform_display.get()],
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "enabled": self.enabled.get(),
        }
        self.destroy()


class TenantConfirmationDialog(tk.Toplevel):
    """Require explicit tenant confirmation before changing NetBox VMs."""

    def __init__(self, parent, cluster_name, tenants, default_tenant=""):
        super().__init__(parent)
        self.title("Mandant bestätigen")
        self.geometry("500x260")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result = None
        self.tenant_name = tk.StringVar(value=default_tenant)

        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Mandant für die Synchronisierung", font=("Segoe UI Semibold", 14)).pack(anchor="w")
        ttk.Label(
            body,
            text=f"Cluster: {cluster_name}\n\nDer Mandant des Clusters wurde vorausgewählt. Bitte bestätigen oder einen anderen Mandanten auswählen.",
            wraplength=450,
            justify="left",
        ).pack(anchor="w", pady=(8, 14))
        self.combo = ttk.Combobox(body, textvariable=self.tenant_name, values=tenants, state="readonly")
        self.combo.pack(fill="x")

        buttons = ttk.Frame(body)
        buttons.pack(anchor="e", pady=(20, 0))
        ttk.Button(buttons, text="Abbrechen", command=self.destroy).pack(side="left", padx=4)
        ttk.Button(buttons, text="Mandant bestätigen", command=self.accept, style="Primary.TButton").pack(side="left", padx=4)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_window(self)

    def accept(self):
        tenant = self.tenant_name.get().strip()
        if not tenant:
            messagebox.showwarning("Mandant fehlt", "Bitte einen Mandanten auswählen.", parent=self)
            return
        self.result = tenant
        self.destroy()


class VRFSelectionDialog(tk.Toplevel):
    """Select a tenant VRF or request automatic creation."""

    AUTO_CREATE = "➕ Neue VRF automatisch erstellen"

    def __init__(self, parent, tenant_name, cluster_name, vrfs):
        super().__init__(parent)
        self.title("VRF auswählen")
        self.geometry("540x355")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result = None
        self.vrf_map = {}

        values = []
        for vrf in vrfs:
            rd = getattr(vrf, "rd", None)
            display = f"{vrf.name} (RD: {rd})" if rd else vrf.name
            self.vrf_map[display] = vrf.id
            values.append(display)
        values.append(self.AUTO_CREATE)
        default = values[0] if vrfs else self.AUTO_CREATE
        self.selection = tk.StringVar(value=default)
        self.new_vrf_name = tk.StringVar(value=f"{tenant_name} - {cluster_name}")

        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="VRF für IP-Adressen", font=("Segoe UI Semibold", 14)).pack(anchor="w")
        ttk.Label(
            body,
            text=(
                f"Mandant: {tenant_name}\nCluster: {cluster_name}\n\n"
                "Bitte eine vorhandene VRF dieses Mandanten auswählen oder automatisch eine neue VRF erstellen lassen."
            ),
            wraplength=490,
            justify="left",
        ).pack(anchor="w", pady=(8, 14))
        selection_combo = ttk.Combobox(body, textvariable=self.selection, values=values, state="readonly")
        selection_combo.pack(fill="x")
        selection_combo.bind("<<ComboboxSelected>>", self.update_name_field)
        self.name_frame = ttk.Frame(body)
        ttk.Label(self.name_frame, text="Name der neuen VRF").pack(anchor="w", pady=(14, 4))
        ttk.Entry(self.name_frame, textvariable=self.new_vrf_name).pack(fill="x")
        self.update_name_field()

        buttons = ttk.Frame(body)
        buttons.pack(anchor="e", pady=(22, 0))
        ttk.Button(buttons, text="Abbrechen", command=self.destroy).pack(side="left", padx=4)
        ttk.Button(buttons, text="VRF bestätigen", command=self.accept, style="Primary.TButton").pack(side="left", padx=4)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_window(self)

    def update_name_field(self, _event=None):
        if self.selection.get() == self.AUTO_CREATE:
            self.name_frame.pack(fill="x")
        else:
            self.name_frame.pack_forget()

    def accept(self):
        selected = self.selection.get()
        if selected == self.AUTO_CREATE:
            vrf_name = self.new_vrf_name.get().strip()
            if not vrf_name:
                messagebox.showwarning("VRF-Name fehlt", "Bitte einen Namen für die neue VRF eingeben.", parent=self)
                return
            self.result = {"create": True, "id": None, "name": vrf_name}
        else:
            self.result = {"create": False, "id": self.vrf_map[selected], "name": None}
        self.destroy()


class PrefixCreationDialog(tk.Toplevel):
    """Ask explicitly before creating a missing IPv4 or IPv6 prefix."""

    AUTO_CREATE = "➕ Neue VRF anlegen"

    def __init__(
        self,
        parent,
        address,
        detected_prefix=True,
        vrfs=None,
        default_vrf_id=None,
        tenant_name="Mandant",
    ):
        super().__init__(parent)
        self.title("Subnetz anlegen?")
        self.geometry("560x455")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result = None
        self.vrf_map = {}

        vrf_values = []
        for vrf in vrfs or []:
            rd = getattr(vrf, "rd", None)
            display = f"{vrf.name} (RD: {rd})" if rd else vrf.name
            self.vrf_map[display] = vrf.id
            vrf_values.append(display)
        vrf_values.append(self.AUTO_CREATE)
        default_vrf = next(
            (display for display, vrf_id in self.vrf_map.items() if vrf_id == default_vrf_id),
            self.AUTO_CREATE,
        )
        self.vrf_selection = tk.StringVar(value=default_vrf)
        self.new_vrf_name = tk.StringVar(value=f"{tenant_name} - VRF")

        interface = ipaddress.ip_interface(address)
        self.host_address = interface.ip
        if interface.version == 4:
            self.choices = [f"/{length}" for length in range(8, 25)]
            default_length = max(8, min(interface.network.prefixlen, 24))
        else:
            common = [32, 40, 48, 52, 56, 60, 64, 80, 96, 112, 120, 124, 126, 127, 128]
            if interface.network.prefixlen not in common:
                common.append(interface.network.prefixlen)
            self.choices = [f"/{length}" for length in sorted(set(common))]
            default_length = interface.network.prefixlen
        self.prefix_length = tk.StringVar(value=f"/{default_length}")
        self.preview_text = tk.StringVar()

        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Fehlendes Präfix", font=("Segoe UI Semibold", 14)).pack(anchor="w")
        source_text = "vom Hypervisor erkannt" if detected_prefix else "aus der IP-Adresse geschätzt"
        ttk.Label(
            body,
            text=(
                f"IP-Adresse: {self.host_address}\n"
                f"Präfixlänge: {source_text}\n\n"
                "NetBox enthält noch kein exakt passendes Präfix. Bitte die gewünschte Subnetzgröße auswählen."
            ),
            wraplength=470,
            justify="left",
        ).pack(anchor="w", pady=(8, 14))

        row = ttk.Frame(body)
        row.pack(fill="x")
        ttk.Label(row, text="Subnetzgröße").pack(side="left")
        combo = ttk.Combobox(row, textvariable=self.prefix_length, values=self.choices, state="readonly", width=10)
        combo.pack(side="left", padx=(12, 0))
        combo.bind("<<ComboboxSelected>>", self.update_preview)

        vrf_row = ttk.Frame(body)
        vrf_row.pack(fill="x", pady=(12, 0))
        ttk.Label(vrf_row, text="VRF").pack(side="left")
        vrf_combo = ttk.Combobox(
            vrf_row,
            textvariable=self.vrf_selection,
            values=vrf_values,
            state="readonly",
        )
        vrf_combo.pack(side="left", fill="x", expand=True, padx=(12, 0))
        vrf_combo.bind("<<ComboboxSelected>>", self.update_vrf_fields)

        new_vrf_row = ttk.Frame(body)
        new_vrf_row.pack(fill="x", pady=(10, 0))
        ttk.Label(new_vrf_row, text="Neue VRF").pack(side="left")
        self.new_vrf_entry = ttk.Entry(new_vrf_row, textvariable=self.new_vrf_name)
        self.new_vrf_entry.pack(side="left", fill="x", expand=True, padx=(12, 0))
        self.new_vrf_name.trace_add("write", self.update_preview)
        ttk.Label(body, textvariable=self.preview_text, foreground="#334e68", font=("Segoe UI Semibold", 10)).pack(
            anchor="w", pady=(14, 0)
        )
        if interface.version == 4:
            ttk.Label(
                body,
                text="IPv4-Präfixe werden als kanonische Netzadresse mit .0 am Ende angelegt.",
                foreground="#52606d",
            ).pack(anchor="w", pady=(6, 0))

        buttons = ttk.Frame(body)
        buttons.pack(anchor="e", pady=(22, 0))
        ttk.Button(buttons, text="Nicht anlegen", command=self.destroy).pack(side="left", padx=4)
        ttk.Button(buttons, text="Präfix anlegen", command=self.accept, style="Primary.TButton").pack(side="left", padx=4)
        self.update_vrf_fields()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_window(self)

    def selected_network(self):
        length = int(self.prefix_length.get().lstrip("/"))
        return ipaddress.ip_network(f"{self.host_address}/{length}", strict=False)

    def update_vrf_fields(self, _event=None):
        is_new = self.vrf_selection.get() == self.AUTO_CREATE
        self.new_vrf_entry.configure(state="normal" if is_new else "disabled")
        self.update_preview()

    def update_preview(self, *_args):
        vrf_name = self.vrf_selection.get() or "keine VRF"
        if vrf_name == self.AUTO_CREATE:
            vrf_name = self.new_vrf_name.get().strip() or "neue VRF ohne Namen"
        self.preview_text.set(f"Wird angelegt: {self.selected_network()} · VRF: {vrf_name}")

    def accept(self):
        network = self.selected_network()
        selected_vrf = self.vrf_selection.get()
        if selected_vrf == self.AUTO_CREATE:
            vrf_name = self.new_vrf_name.get().strip()
            if not vrf_name:
                messagebox.showwarning("VRF-Name fehlt", "Bitte einen Namen für die neue VRF eingeben.", parent=self)
                return
            vrf_id = None
            create_vrf = True
        elif selected_vrf in self.vrf_map:
            vrf_name = None
            vrf_id = self.vrf_map[selected_vrf]
            create_vrf = False
        else:
            messagebox.showwarning("VRF fehlt", "Bitte eine VRF für das Präfix auswählen.", parent=self)
            return
        if network.version == 4 and str(network.network_address).split(".")[-1] != "0":
            messagebox.showerror(
                "Ungültige Netzadresse",
                "Das IPv4-Präfix muss mit einer .0-Netzadresse beginnen.",
                parent=self,
            )
            return
        self.result = {
            "network": str(network),
            "vrf_id": vrf_id,
            "create_vrf": create_vrf,
            "vrf_name": vrf_name,
        }
        self.destroy()


class DetectedNetworksDialog(tk.Toplevel):
    """Review all newly detected networks and their VRFs in one import plan."""

    AUTO_CREATE = "➕ Neue VRF anlegen"

    def __init__(self, parent, networks, vrfs, default_vrf_id, tenant_name):
        super().__init__(parent)
        self.title("Neue Netzwerke prüfen")
        self.geometry("1060x620")
        self.minsize(900, 480)
        self.transient(parent)
        self.grab_set()
        self.result = None
        self.rows = []
        self.vrf_map = {}

        vrf_values = []
        for vrf in vrfs:
            rd = getattr(vrf, "rd", None)
            display = f"{vrf.name} (RD: {rd})" if rd else vrf.name
            self.vrf_map[display] = vrf.id
            vrf_values.append(display)
        vrf_values.append(self.AUTO_CREATE)
        default_vrf = next(
            (display for display, vrf_id in self.vrf_map.items() if vrf_id == default_vrf_id),
            vrf_values[0],
        )

        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Neue Netzwerke erkannt", font=("Segoe UI Semibold", 16)).pack(anchor="w")
        ttk.Label(
            body,
            text=(
                "Nur noch nicht vorhandene Netze werden angezeigt. Subnetzgröße und VRF können pro Netz geändert "
                "werden. Bei ‚Neue VRF anlegen‘ erscheint ein Namensfeld in derselben Zeile."
            ),
            wraplength=980,
            foreground="#52606d",
        ).pack(anchor="w", pady=(5, 14))

        header = ttk.Frame(body)
        header.pack(fill="x", padx=(2, 20))
        ttk.Label(header, text="Anlegen", width=8).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="Erkannte IP", width=21).grid(row=0, column=1, sticky="w")
        ttk.Label(header, text="Größe", width=9).grid(row=0, column=2, sticky="w")
        ttk.Label(header, text="Netzwerk", width=25).grid(row=0, column=3, sticky="w")
        ttk.Label(header, text="VRF", width=34).grid(row=0, column=4, sticky="w")

        container = ttk.Frame(body)
        container.pack(fill="both", expand=True, pady=(4, 10))
        canvas = tk.Canvas(container, borderwidth=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        self.rows_frame = ttk.Frame(canvas)
        self.rows_frame.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        window_id = canvas.create_window((0, 0), window=self.rows_frame, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        for index, item in enumerate(networks):
            self.add_network_row(index, item, vrf_values, default_vrf, tenant_name)

        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Import abbrechen", command=self.destroy).pack(side="left")
        ttk.Button(buttons, text="Ohne neue Netze fortfahren", command=self.skip_all).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="Auswahl übernehmen", command=self.accept, style="Primary.TButton").pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_window(self)

    def add_network_row(self, index, item, vrf_values, default_vrf, tenant_name):
        row_frame = ttk.Frame(self.rows_frame, padding=(4, 7))
        row_frame.pack(fill="x")
        enabled = tk.BooleanVar(value=True)
        prefix_length = tk.StringVar(value=f"/{item['suggested_prefixlen']}")
        network_text = tk.StringVar()
        vrf_selection = tk.StringVar(value=default_vrf)
        new_vrf_name = tk.StringVar(value=f"{tenant_name} - {item['suggested_network'].network_address}")

        ttk.Checkbutton(row_frame, variable=enabled).grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Label(row_frame, text=str(item["host_address"]), width=21).grid(row=0, column=1, sticky="w")
        choices = self.prefix_choices(item["host_address"], item["detected_prefixlen"])
        size_combo = ttk.Combobox(row_frame, textvariable=prefix_length, values=choices, state="readonly", width=7)
        size_combo.grid(row=0, column=2, sticky="w", padx=(0, 12))
        ttk.Label(row_frame, textvariable=network_text, width=25).grid(row=0, column=3, sticky="w")
        vrf_combo = ttk.Combobox(
            row_frame,
            textvariable=vrf_selection,
            values=vrf_values,
            state="readonly",
            width=34,
        )
        vrf_combo.grid(row=0, column=4, sticky="ew")
        row_frame.columnconfigure(4, weight=1)

        name_frame = ttk.Frame(row_frame)
        ttk.Label(name_frame, text="Name der neuen VRF:").pack(side="left")
        ttk.Entry(name_frame, textvariable=new_vrf_name).pack(side="left", fill="x", expand=True, padx=(8, 0))

        row = {
            "frame": row_frame,
            "enabled": enabled,
            "host_address": item["host_address"],
            "prefix_length": prefix_length,
            "network_text": network_text,
            "vrf_selection": vrf_selection,
            "new_vrf_name": new_vrf_name,
            "name_frame": name_frame,
        }
        self.rows.append(row)

        size_combo.bind("<<ComboboxSelected>>", lambda _event, current=row: self.update_row(current))
        vrf_combo.bind("<<ComboboxSelected>>", lambda _event, current=row: self.update_row(current))
        self.update_row(row)

    @staticmethod
    def prefix_choices(host_address, detected_prefixlen):
        host = ipaddress.ip_address(host_address)
        if host.version == 4:
            return [f"/{length}" for length in range(8, 25)]
        common = {32, 40, 48, 52, 56, 60, 64, 80, 96, 112, 120, 124, 126, 127, 128, detected_prefixlen}
        return [f"/{length}" for length in sorted(common)]

    def update_row(self, row):
        length = int(row["prefix_length"].get().lstrip("/"))
        network = ipaddress.ip_network(f"{row['host_address']}/{length}", strict=False)
        row["network_text"].set(str(network))
        if row["vrf_selection"].get() == self.AUTO_CREATE:
            row["name_frame"].grid(row=1, column=4, sticky="ew", pady=(7, 0))
        else:
            row["name_frame"].grid_remove()

    def skip_all(self):
        self.result = []
        self.destroy()

    def accept(self):
        decisions = {}
        for row in self.rows:
            if not row["enabled"].get():
                continue
            network = row["network_text"].get()
            selected_vrf = row["vrf_selection"].get()
            if selected_vrf == self.AUTO_CREATE:
                vrf_name = row["new_vrf_name"].get().strip()
                if not vrf_name:
                    messagebox.showwarning("VRF-Name fehlt", f"Bitte für {network} einen VRF-Namen eingeben.", parent=self)
                    return
                decision = {"network": network, "vrf_id": None, "create_vrf": True, "vrf_name": vrf_name}
            else:
                decision = {
                    "network": network,
                    "vrf_id": self.vrf_map[selected_vrf],
                    "create_vrf": False,
                    "vrf_name": None,
                }
            existing = decisions.get(network)
            if existing and existing != decision:
                messagebox.showwarning(
                    "Netzwerk doppelt",
                    f"{network} wurde mit unterschiedlichen VRFs ausgewählt.",
                    parent=self,
                )
                return
            decisions[network] = decision
        self.result = list(decisions.values())
        self.destroy()


class NetworkManagementDialog(tk.Toplevel):
    """Create multiple tenant VRFs and prefixes in one dedicated window."""

    def __init__(self, parent, importer, default_tenant=""):
        super().__init__(parent)
        self.importer = importer
        self.title("VRFs & Netzwerke")
        self.geometry("720x650")
        self.minsize(650, 590)
        self.transient(parent)
        self.grab_set()
        self.tenant_name = tk.StringVar(value=default_tenant)
        self.vrf_selection = tk.StringVar()
        self.vrf_map = {}
        self.status_text = tk.StringVar(value="Bereit")

        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="VRFs & Netzwerke", font=("Segoe UI Semibold", 16)).pack(anchor="w")
        ttk.Label(
            body,
            text="Mehrere Einträge jeweils zeilenweise erfassen und gesammelt in NetBox anlegen.",
            foreground="#52606d",
        ).pack(anchor="w", pady=(4, 16))

        tenant_row = ttk.Frame(body)
        tenant_row.pack(fill="x")
        ttk.Label(tenant_row, text="Mandant", width=14).pack(side="left")
        tenant_combo = ttk.Combobox(
            tenant_row,
            textvariable=self.tenant_name,
            values=sorted(importer.tenant_map, key=str.casefold),
            state="readonly",
        )
        tenant_combo.pack(side="left", fill="x", expand=True)
        tenant_combo.bind("<<ComboboxSelected>>", self.refresh_vrfs)

        vrf_box = ttk.LabelFrame(body, text="Mehrere VRFs anlegen", padding=12)
        vrf_box.pack(fill="both", expand=True, pady=(16, 10))
        ttk.Label(vrf_box, text="Ein VRF-Name pro Zeile:").pack(anchor="w")
        self.vrf_names = tk.Text(vrf_box, height=6, font=("Segoe UI", 10))
        self.vrf_names.pack(fill="both", expand=True, pady=(6, 10))
        ttk.Button(vrf_box, text="Alle VRFs anlegen", command=self.create_vrfs, style="Primary.TButton").pack(
            anchor="e"
        )

        prefix_box = ttk.LabelFrame(body, text="Mehrere Netzwerke anlegen", padding=12)
        prefix_box.pack(fill="both", expand=True, pady=(0, 10))
        vrf_row = ttk.Frame(prefix_box)
        vrf_row.pack(fill="x")
        ttk.Label(vrf_row, text="Ziel-VRF", width=14).pack(side="left")
        self.vrf_combo = ttk.Combobox(vrf_row, textvariable=self.vrf_selection, state="readonly")
        self.vrf_combo.pack(side="left", fill="x", expand=True)
        ttk.Label(prefix_box, text="Ein Präfix pro Zeile, z. B. 10.10.100.0/24:").pack(anchor="w", pady=(12, 0))
        self.prefixes = tk.Text(prefix_box, height=6, font=("Consolas", 10))
        self.prefixes.pack(fill="both", expand=True, pady=(6, 10))
        ttk.Button(
            prefix_box,
            text="Alle Netzwerke anlegen",
            command=self.create_prefixes,
            style="Primary.TButton",
        ).pack(anchor="e")

        footer = ttk.Frame(body)
        footer.pack(fill="x", pady=(4, 0))
        ttk.Label(footer, textvariable=self.status_text, foreground="#334e68").pack(side="left")
        ttk.Button(footer, text="Schließen", command=self.destroy).pack(side="right")

        if not self.tenant_name.get() and importer.tenant_map:
            self.tenant_name.set(sorted(importer.tenant_map, key=str.casefold)[0])
        self.refresh_vrfs()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_window(self)

    @staticmethod
    def text_lines(widget):
        return list(dict.fromkeys(line.strip() for line in widget.get("1.0", "end").splitlines() if line.strip()))

    def refresh_vrfs(self, _event=None):
        tenant_name = self.tenant_name.get()
        tenant_id = self.importer.tenant_map.get(tenant_name)
        vrfs = self.importer.tenant_vrfs(tenant_id) if tenant_id else []
        self.vrf_map = {}
        values = []
        for vrf in vrfs:
            rd = getattr(vrf, "rd", None)
            display = f"{vrf.name} (RD: {rd})" if rd else vrf.name
            self.vrf_map[display] = vrf.id
            values.append(display)
        self.vrf_combo["values"] = values
        if self.vrf_selection.get() not in self.vrf_map:
            self.vrf_selection.set(values[0] if values else "")

    def create_vrfs(self):
        names = self.text_lines(self.vrf_names)
        tenant_name = self.tenant_name.get()
        if not tenant_name or not names:
            messagebox.showwarning("Angaben fehlen", "Bitte Mandant und mindestens einen VRF-Namen angeben.", parent=self)
            return
        created, reused, errors = self.importer.create_tenant_vrfs_batch(tenant_name, names)
        self.status_text.set(f"VRFs: {created} angelegt, {reused} bereits vorhanden")
        self.refresh_vrfs()
        if not errors:
            self.vrf_names.delete("1.0", "end")
        else:
            messagebox.showwarning("VRFs teilweise angelegt", "\n".join(errors), parent=self)

    def create_prefixes(self):
        networks = self.text_lines(self.prefixes)
        tenant_name = self.tenant_name.get()
        vrf_id = self.vrf_map.get(self.vrf_selection.get())
        if not tenant_name or not vrf_id or not networks:
            messagebox.showwarning(
                "Angaben fehlen",
                "Bitte Mandant, Ziel-VRF und mindestens ein Netzwerk angeben.",
                parent=self,
            )
            return
        created, reused, canonical, errors = self.importer.create_prefixes_batch(
            tenant_name,
            vrf_id,
            networks,
        )
        self.status_text.set(f"Netzwerke: {created} angelegt, {reused} bereits vorhanden")
        if canonical:
            self.prefixes.delete("1.0", "end")
            self.prefixes.insert("1.0", "\n".join(canonical))
        if errors:
            messagebox.showwarning("Netzwerke teilweise angelegt", "\n".join(errors), parent=self)
        else:
            messagebox.showinfo(
                "Netzwerke verarbeitet",
                f"{created} Netzwerk(e) angelegt, {reused} bereits vorhanden.",
                parent=self,
            )


class NetBoxVMImporter:
    def __init__(self, root):
        self.root = root
        self.root.title("NetBox Synchronizer")
        self.root.geometry("1160x850")
        self.root.minsize(960, 700)

        self.nb = None
        self.vms = []
        self.profiles = {}
        self.current_sources = []
        self.current_switches = []
        self.switch_snapshots = {}
        self.netbox_devices = []
        self.cluster_map = {}
        self.cluster_tenants = {}
        self.tenant_map = {}
        self.vrfs = []
        self.platforms = []
        self.profile_name = tk.StringVar()
        self.netbox_url_var = tk.StringVar()
        self.netbox_token_var = tk.StringVar()
        self.netbox_ignore_ssl = tk.BooleanVar(value=False)
        self.status_text = tk.StringVar(value="Bereit")
        self.store = ProfileStore()

        self.configure_style()
        self.create_gui()
        self.load_profiles()
        self.set_window_icon()

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
        style.configure("Treeview", rowheight=27, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9))

    def resource_path(self, filename):
        return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / filename

    def set_window_icon(self):
        icon = self.resource_path("netbox_vm_import.ico")
        if icon.exists():
            try:
                self.root.iconbitmap(str(icon))
            except tk.TclError:
                pass

    def create_gui(self):
        self.main_tabs = ttk.Notebook(self.root)
        self.main_tabs.pack(fill="both", expand=True)
        virtualization_tab = ttk.Frame(self.main_tabs)
        switches_tab = ttk.Frame(self.main_tabs)
        self.main_tabs.add(virtualization_tab, text="Virtualisierung")
        self.main_tabs.add(switches_tab, text="Switche")

        main = ttk.Frame(virtualization_tab, padding=18)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(4, weight=3)
        main.rowconfigure(6, weight=1)

        header = ttk.Frame(main)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(header, text="VMware & Proxmox → NetBox", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="Mehrere Virtualisierungsquellen kundenbezogen synchronisieren", style="Subtitle.TLabel").pack(anchor="w")

        profile_card = ttk.LabelFrame(main, text="Kundenprofil", style="Card.TLabelframe")
        profile_card.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self.profile_combo = ttk.Combobox(profile_card, textvariable=self.profile_name, state="readonly")
        self.profile_combo.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.profile_combo.bind("<<ComboboxSelected>>", self.on_profile_selected)
        ttk.Button(profile_card, text="Neu", command=self.new_profile).pack(side="left", padx=3)
        ttk.Button(profile_card, text="Speichern", command=self.save_profile).pack(side="left", padx=3)
        ttk.Button(profile_card, text="Löschen", command=self.delete_profile).pack(side="left", padx=3)

        connections = ttk.Panedwindow(main, orient="horizontal")
        connections.grid(row=2, column=0, sticky="nsew", pady=(0, 10))
        source_card = ttk.LabelFrame(connections, text="Virtualisierungsquellen", style="Card.TLabelframe")
        netbox_card = ttk.LabelFrame(connections, text="NetBox-Ziel", style="Card.TLabelframe")
        connections.add(source_card, weight=3)
        connections.add(netbox_card, weight=2)

        self.source_tree = ttk.Treeview(source_card, columns=("type", "endpoint", "active"), show="headings", height=5)
        self.source_tree.heading("type", text="Typ")
        self.source_tree.heading("endpoint", text="Quelle")
        self.source_tree.heading("active", text="Aktiv")
        self.source_tree.column("type", width=90, stretch=False)
        self.source_tree.column("endpoint", width=320)
        self.source_tree.column("active", width=60, stretch=False, anchor="center")
        self.source_tree.pack(fill="both", expand=True)
        self.source_tree.bind("<Double-1>", lambda _event: self.edit_source())
        source_buttons = ttk.Frame(source_card)
        source_buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(source_buttons, text="Quelle hinzufügen", command=self.add_source).pack(side="left")
        ttk.Button(source_buttons, text="Bearbeiten", command=self.edit_source).pack(side="left", padx=5)
        ttk.Button(source_buttons, text="Entfernen", command=self.remove_source).pack(side="left")

        self.netbox_url = self.add_field(netbox_card, 0, "URL", variable=self.netbox_url_var)
        self.netbox_token = self.add_field(netbox_card, 1, "API-Token", secret=True, variable=self.netbox_token_var)
        ttk.Checkbutton(netbox_card, text="Zertifikatsprüfung deaktivieren", variable=self.netbox_ignore_ssl).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )
        ttk.Label(netbox_card, text="Secrets werden mit Windows DPAPI verschlüsselt.", style="Subtitle.TLabel").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(12, 0)
        )

        action_row = ttk.Frame(main)
        action_row.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        ttk.Button(action_row, text="Verbinden & VMs laden", command=self.connect_all, style="Primary.TButton").pack(side="right")

        workspace = ttk.Panedwindow(main, orient="horizontal")
        workspace.grid(row=4, column=0, sticky="nsew")
        target_card = ttk.LabelFrame(workspace, text="Importziel", style="Card.TLabelframe")
        vm_card = ttk.LabelFrame(workspace, text="Gefundene virtuelle Maschinen", style="Card.TLabelframe")
        workspace.add(target_card, weight=1)
        workspace.add(vm_card, weight=4)
        self.cluster_combo = self.add_combo_field(target_card, 0, "Cluster")
        self.tenant_combo = self.add_combo_field(target_card, 1, "Mandant")
        self.cluster_combo.bind("<<ComboboxSelected>>", self.on_cluster_selected)
        target_card.columnconfigure(0, weight=1)

        vm_columns = ("name", "source", "kind", "status", "cpu", "memory")
        self.vm_tree = ttk.Treeview(vm_card, columns=vm_columns, show="headings", selectmode="extended")
        headings = {"name": "Name", "source": "Quelle", "kind": "Typ", "status": "Status", "cpu": "vCPU", "memory": "RAM (MB)"}
        widths = {"name": 210, "source": 160, "kind": 80, "status": 80, "cpu": 55, "memory": 80}
        for column in vm_columns:
            self.vm_tree.heading(column, text=headings[column])
            self.vm_tree.column(column, width=widths[column], anchor="center" if column in ("kind", "status", "cpu", "memory") else "w")
        vm_scroll = ttk.Scrollbar(vm_card, orient="vertical", command=self.vm_tree.yview)
        self.vm_tree.configure(yscrollcommand=vm_scroll.set)
        self.vm_tree.pack(side="left", fill="both", expand=True)
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
        self.create_switch_gui(switches_tab)

    def create_switch_gui(self, parent):
        main = ttk.Frame(parent, padding=18)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)

        header = ttk.Frame(main)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(header, text="HPE & Aruba Switche", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Bestehende NetBox-Geräte per SSH auslesen und dokumentieren",
            style="Subtitle.TLabel",
        ).pack(anchor="w")

        settings = ttk.Panedwindow(main, orient="horizontal")
        settings.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        profile_card = ttk.LabelFrame(settings, text="Kundenprofil", style="Card.TLabelframe")
        netbox_card = ttk.LabelFrame(settings, text="NetBox-Ziel", style="Card.TLabelframe")
        settings.add(profile_card, weight=3)
        settings.add(netbox_card, weight=2)

        self.switch_profile_combo = ttk.Combobox(profile_card, textvariable=self.profile_name, state="readonly")
        self.switch_profile_combo.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.switch_profile_combo.bind("<<ComboboxSelected>>", self.on_profile_selected)
        ttk.Button(profile_card, text="Neu", command=self.new_profile).pack(side="left", padx=3)
        ttk.Button(profile_card, text="Speichern", command=self.save_profile).pack(side="left", padx=3)
        ttk.Button(profile_card, text="Löschen", command=self.delete_profile).pack(side="left", padx=3)

        self.switch_netbox_url = self.add_field(netbox_card, 0, "URL", variable=self.netbox_url_var)
        self.switch_netbox_token = self.add_field(
            netbox_card,
            1,
            "API-Token",
            secret=True,
            variable=self.netbox_token_var,
        )
        ttk.Checkbutton(
            netbox_card,
            text="Zertifikatsprüfung deaktivieren",
            variable=self.netbox_ignore_ssl,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        source_card = ttk.LabelFrame(main, text="Switch-Zugänge", style="Card.TLabelframe")
        source_card.grid(row=2, column=0, sticky="nsew", pady=(0, 10))
        switch_columns = ("device", "platform", "host", "active")
        self.switch_tree = ttk.Treeview(source_card, columns=switch_columns, show="headings", height=6)
        for column, title, width in (
            ("device", "NetBox-Gerät", 230),
            ("platform", "Plattform", 210),
            ("host", "SSH-Host", 260),
            ("active", "Aktiv", 60),
        ):
            self.switch_tree.heading(column, text=title)
            self.switch_tree.column(column, width=width, anchor="center" if column == "active" else "w")
        self.switch_tree.pack(fill="both", expand=True)
        self.switch_tree.bind("<Double-1>", lambda _event: self.edit_switch())
        source_buttons = ttk.Frame(source_card)
        source_buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(source_buttons, text="NetBox-Geräte laden", command=self.load_switch_devices).pack(side="left")
        ttk.Button(source_buttons, text="Switch hinzufügen", command=self.add_switch).pack(side="left", padx=(8, 4))
        ttk.Button(source_buttons, text="Bearbeiten", command=self.edit_switch).pack(side="left", padx=4)
        ttk.Button(source_buttons, text="Entfernen", command=self.remove_switch).pack(side="left", padx=4)
        ttk.Button(
            source_buttons,
            text="Ausgewählte auslesen",
            command=self.discover_selected_switches,
            style="Primary.TButton",
        ).pack(side="right")

        switch_workspace = ttk.Panedwindow(main, orient="vertical")
        switch_workspace.grid(row=3, column=0, sticky="nsew", pady=(0, 10))
        result_card = ttk.LabelFrame(switch_workspace, text="Schnittstellen & LLDP/CDP-Nachbarn", style="Card.TLabelframe")
        result_columns = ("switch", "interface", "state", "speed", "ips", "neighbor", "remote_port")
        self.switch_result_tree = ttk.Treeview(result_card, columns=result_columns, show="headings")
        result_headings = {
            "switch": "Switch", "interface": "Schnittstelle", "state": "Status",
            "speed": "Geschwindigkeit", "ips": "IP-Adressen",
            "neighbor": "LLDP/CDP-Nachbar", "remote_port": "Nachbar-Port",
        }
        result_widths = {
            "switch": 150, "interface": 140, "state": 70, "speed": 100,
            "ips": 210, "neighbor": 170, "remote_port": 140,
        }
        for column in result_columns:
            self.switch_result_tree.heading(column, text=result_headings[column])
            self.switch_result_tree.column(column, width=result_widths[column])
        result_scroll = ttk.Scrollbar(result_card, orient="vertical", command=self.switch_result_tree.yview)
        self.switch_result_tree.configure(yscrollcommand=result_scroll.set)
        self.switch_result_tree.pack(side="left", fill="both", expand=True)
        result_scroll.pack(side="right", fill="y")

        log_card = ttk.LabelFrame(switch_workspace, text="Switch-Protokoll", style="Card.TLabelframe")
        self.switch_log_text = tk.Text(
            log_card, height=6, borderwidth=0, bg="#f7f9fb", foreground="#243b53", font=("Consolas", 9), state="disabled"
        )
        log_scroll = ttk.Scrollbar(log_card, orient="vertical", command=self.switch_log_text.yview)
        self.switch_log_text.configure(yscrollcommand=log_scroll.set)
        self.switch_log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")
        switch_workspace.add(result_card, weight=4)
        switch_workspace.add(log_card, weight=1)

        actions = ttk.Frame(main)
        actions.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        self.create_neighbor_cables = tk.BooleanVar(value=True)
        ttk.Checkbutton(actions, text="LLDP/CDP-Verbindungen als Kabel anlegen", variable=self.create_neighbor_cables).pack(side="left")
        ttk.Button(actions, text="Nach NetBox synchronisieren", command=self.sync_switches, style="Primary.TButton").pack(side="right")
        ttk.Button(actions, text="SSH-Rohdaten", command=self.open_switch_raw_window).pack(side="right", padx=(0, 8))
        ttk.Button(actions, text="Protokoll öffnen", command=self.open_switch_log_window).pack(side="right", padx=(0, 8))

    @staticmethod
    def add_field(parent, row, label, secret=False, variable=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=5)
        entry = ttk.Entry(parent, show="•" if secret else "", textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=5)
        parent.columnconfigure(1, weight=1)
        return entry

    @staticmethod
    def add_combo_field(parent, row, label):
        ttk.Label(parent, text=label).grid(row=row * 2, column=0, sticky="w", pady=(5, 2))
        combo = ttk.Combobox(parent, state="readonly")
        combo.grid(row=row * 2 + 1, column=0, sticky="ew", pady=(0, 8))
        return combo

    def log(self, text):
        self.log_text.config(state="normal")
        self.log_text.insert(tk.END, f"{text}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state="disabled")
        self.status_text.set(text)
        self.root.update()

    # Profiles and sources

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
        values = sorted(self.profiles, key=str.casefold)
        self.profile_combo["values"] = values
        if hasattr(self, "switch_profile_combo"):
            self.switch_profile_combo["values"] = values

    def profile_from_form(self, name):
        return {
            "name": name,
            "netbox_url": self.netbox_url.get().strip(),
            "netbox_token": self.netbox_token.get(),
            "netbox_ignore_ssl": self.netbox_ignore_ssl.get(),
            "sources": [dict(source) for source in self.current_sources],
            "switches": [dict(switch) for switch in self.current_switches],
        }

    def populate_profile(self, name):
        profile = self.profiles.get(name, {})
        self.set_entry(self.netbox_url, profile.get("netbox_url", ""))
        self.set_entry(self.netbox_token, profile.get("netbox_token", ""))
        self.netbox_ignore_ssl.set(bool(profile.get("netbox_ignore_ssl", False)))
        self.current_sources = [dict(source) for source in profile.get("sources", [])]
        self.current_switches = [dict(switch) for switch in profile.get("switches", [])]
        self.switch_snapshots = {}
        self.refresh_source_tree()
        self.refresh_switch_tree()
        if hasattr(self, "switch_result_tree"):
            self.refresh_switch_results()
        self.clear_loaded_data()
        self.status_text.set(f"Profil „{name}“ geladen")

    @staticmethod
    def set_entry(widget, value):
        widget.delete(0, tk.END)
        widget.insert(0, value)

    def on_profile_selected(self, _event=None):
        self.populate_profile(self.profile_name.get())

    def new_profile(self):
        name = simpledialog.askstring("Neues Kundenprofil", "Kunden- oder Profilname:", parent=self.root)
        if not name or not name.strip():
            return
        name = name.strip()
        if name in self.profiles and not messagebox.askyesno("Profil vorhanden", "Dieses Profil überschreiben?"):
            return
        self.profile_name.set(name)
        self.set_entry(self.netbox_url, "")
        self.set_entry(self.netbox_token, "")
        self.netbox_ignore_ssl.set(False)
        self.current_sources = []
        self.current_switches = []
        self.refresh_source_tree()
        self.refresh_switch_tree()
        self.profiles[name] = self.profile_from_form(name)
        self.refresh_profile_list()

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
            self.set_entry(self.netbox_url, "")
            self.set_entry(self.netbox_token, "")
            self.current_sources = []
            self.current_switches = []
            self.refresh_source_tree()
            self.refresh_switch_tree()

    def refresh_source_tree(self):
        self.source_tree.delete(*self.source_tree.get_children())
        for source in self.current_sources:
            endpoint = source.get("host", source.get("url", ""))
            self.source_tree.insert("", "end", iid=source["id"], values=(
                source.get("type", "").upper(), f"{source.get('name', '')} · {endpoint}",
                "Ja" if source.get("enabled", True) else "Nein",
            ))

    def add_source(self):
        dialog = SourceDialog(self.root)
        if dialog.result:
            self.current_sources.append(dialog.result)
            self.refresh_source_tree()

    def selected_source(self):
        selected = self.source_tree.selection()
        if not selected:
            return None
        return next((source for source in self.current_sources if source["id"] == selected[0]), None)

    def edit_source(self):
        source = self.selected_source()
        if not source:
            messagebox.showinfo("Quelle", "Bitte eine Quelle auswählen.")
            return
        dialog = SourceDialog(self.root, source)
        if dialog.result:
            index = self.current_sources.index(source)
            self.current_sources[index] = dialog.result
            self.refresh_source_tree()
            self.source_tree.selection_set(dialog.result["id"])

    def remove_source(self):
        source = self.selected_source()
        if source and messagebox.askyesno("Quelle entfernen", f"Quelle „{source['name']}“ entfernen?"):
            self.current_sources.remove(source)
            self.refresh_source_tree()

    def switch_log(self, text):
        self.switch_log_text.configure(state="normal")
        self.switch_log_text.insert(tk.END, f"{text}\n")
        self.switch_log_text.see(tk.END)
        self.switch_log_text.configure(state="disabled")
        self.status_text.set(text)
        self.root.update()

    def _open_switch_text_window(self, title, content):
        window = tk.Toplevel(self.root)
        window.title(title)
        window.geometry("1000x650")
        window.minsize(700, 400)
        icon = self.resource_path("netbox_vm_import.ico")
        if icon.exists():
            try:
                window.iconbitmap(str(icon))
            except tk.TclError:
                pass
        frame = ttk.Frame(window, padding=10)
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        text_widget = tk.Text(frame, wrap="none", font=("Consolas", 9), bg="#f7f9fb", foreground="#243b53")
        vertical = ttk.Scrollbar(frame, orient="vertical", command=text_widget.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=text_widget.xview)
        text_widget.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        text_widget.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        text_widget.insert("1.0", content)
        text_widget.configure(state="disabled")
        return window

    def open_switch_log_window(self):
        content = self.switch_log_text.get("1.0", "end-1c") or "Noch keine Protokolleinträge vorhanden."
        self._open_switch_text_window("Switch-Protokoll", content)

    def open_switch_raw_window(self):
        selected_ids = set(self.switch_tree.selection())
        snapshots = [
            (switch, self.switch_snapshots.get(switch["id"]))
            for switch in self.current_switches
            if (not selected_ids or switch["id"] in selected_ids) and self.switch_snapshots.get(switch["id"])
        ]
        if not snapshots:
            messagebox.showinfo("SSH-Rohdaten", "Bitte zuerst mindestens einen Switch auslesen.", parent=self.root)
            return
        sections = []
        labels = {
            "interfaces": "SCHNITTSTELLEN",
            "ipv4_interfaces": "IPV4-SCHNITTSTELLEN",
            "ipv6_interfaces": "IPV6-SCHNITTSTELLEN",
            "neighbors": "LLDP-NACHBARN",
            "cdp_neighbors": "CDP-NACHBARN",
        }
        for switch, snapshot in snapshots:
            sections.append(f"{'=' * 20} {switch['name']} ({switch['host']}) {'=' * 20}")
            raw = snapshot.get("raw", {})
            for key, label in labels.items():
                sections.extend((f"\n--- {label} ---", str(raw.get(key, "") or "(keine Ausgabe)")))
        self._open_switch_text_window("SSH-Rohdaten", "\n".join(sections))

    def refresh_switch_tree(self):
        if not hasattr(self, "switch_tree"):
            return
        self.switch_tree.delete(*self.switch_tree.get_children())
        for switch in self.current_switches:
            self.switch_tree.insert("", "end", iid=switch["id"], values=(
                switch.get("name", ""),
                PLATFORMS.get(switch.get("platform"), switch.get("platform", "")),
                f"{switch.get('host', '')}:{switch.get('port', 22)}",
                "Ja" if switch.get("enabled", True) else "Nein",
            ))

    def load_switch_devices(self):
        try:
            if not self.netbox_url.get().strip() or not self.netbox_token.get():
                raise ValueError("Bitte NetBox-URL und API-Token im Tab Virtualisierung eintragen.")
            if not self.save_profile(show_message=False):
                return False
            self.connect_netbox()
            self.switch_log("Lade vorhandene NetBox-Geräte …")
            devices = list(self.nb.dcim.devices.all())
            self.netbox_devices = sorted(
                (device for device in devices if getattr(device, "name", None)),
                key=lambda item: str(item.name).casefold(),
            )
            ignored = len(devices) - len(self.netbox_devices)
            text = f"{len(self.netbox_devices)} benannte NetBox-Geräte geladen"
            if ignored:
                text += f" ({ignored} Gerät(e) ohne Namen übersprungen)"
            self.switch_log(text)
            return True
        except Exception as error:
            messagebox.showerror("NetBox-Verbindung", str(error), parent=self.root)
            return False

    def add_switch(self):
        if not self.netbox_devices and not self.load_switch_devices():
            return
        dialog = SwitchSourceDialog(self.root, self.netbox_devices)
        if dialog.result:
            self.current_switches.append(dialog.result)
            self.refresh_switch_tree()

    def selected_switches(self):
        selected_ids = set(self.switch_tree.selection())
        return [switch for switch in self.current_switches if switch["id"] in selected_ids]

    def edit_switch(self):
        selected = self.selected_switches()
        if len(selected) != 1:
            messagebox.showinfo("Switch", "Bitte genau einen Switch auswählen.", parent=self.root)
            return
        if not self.netbox_devices and not self.load_switch_devices():
            return
        current = selected[0]
        dialog = SwitchSourceDialog(self.root, self.netbox_devices, current)
        if dialog.result:
            index = self.current_switches.index(current)
            self.current_switches[index] = dialog.result
            self.refresh_switch_tree()
            self.switch_tree.selection_set(dialog.result["id"])

    def remove_switch(self):
        selected = self.selected_switches()
        if not selected:
            return
        if not messagebox.askyesno(
            "Switch entfernen",
            f"{len(selected)} Switch-Zugang/Zugänge aus dem Profil entfernen?",
            parent=self.root,
        ):
            return
        selected_ids = {switch["id"] for switch in selected}
        self.current_switches = [switch for switch in self.current_switches if switch["id"] not in selected_ids]
        for switch_id in selected_ids:
            self.switch_snapshots.pop(switch_id, None)
        self.refresh_switch_tree()
        self.refresh_switch_results()

    def discover_selected_switches(self):
        switches = self.selected_switches()
        if not switches:
            messagebox.showinfo("Switche", "Bitte mindestens einen Switch auswählen.", parent=self.root)
            return
        if not self.save_profile(show_message=False):
            return
        failures = []
        for switch in switches:
            if not switch.get("enabled", True):
                continue
            try:
                self.switch_log(f"Lese {switch['name']} ({switch['host']}) aus …")
                snapshot = discover_switch(switch)
                self.switch_snapshots[switch["id"]] = snapshot
                if snapshot.get("lldp_enabled_automatically"):
                    self.switch_log(f"{switch['name']}: LLDP war deaktiviert und wurde automatisch aktiviert")
                self.switch_log(
                    f"{switch['name']}: {len(snapshot['interfaces'])} Schnittstellen, "
                    f"{len(snapshot.get('ip_addresses', []))} IP-Adressen, "
                    f"{len(snapshot.get('lldp_neighbors', []))} LLDP- und "
                    f"{len(snapshot.get('cdp_neighbors', []))} CDP-Nachbarn"
                )
                if not snapshot["interfaces"]:
                    self.switch_log(f"WARNUNG {switch['name']}: Keine Schnittstellen erkannt – bitte SSH-Rohdaten prüfen")
                if not snapshot["neighbors"]:
                    self.switch_log(f"WARNUNG {switch['name']}: Keine LLDP/CDP-Nachbarn erkannt – bitte SSH-Rohdaten prüfen")
            except Exception as error:
                failures.append(f"{switch['name']}: {error}")
                self.switch_log(f"FEHLER {switch['name']}: {error}")
        self.refresh_switch_results()
        if failures:
            messagebox.showwarning("Teilweise ausgelesen", "\n".join(failures), parent=self.root)

    def refresh_switch_results(self):
        self.switch_result_tree.delete(*self.switch_result_tree.get_children())
        switch_map = {switch["id"]: switch for switch in self.current_switches}
        for switch_id, snapshot in self.switch_snapshots.items():
            switch = switch_map.get(switch_id)
            if not switch:
                continue
            for interface in snapshot["interfaces"]:
                neighbor = interface.get("neighbor") or {}
                speed = interface.get("speed_mbps")
                speed_text = f"{speed / 1000:g} Gbit/s" if speed and speed >= 1000 else (f"{speed} Mbit/s" if speed else "")
                self.switch_result_tree.insert("", "end", values=(
                    switch["name"],
                    interface["name"],
                    "Up" if interface.get("connected") else "Down",
                    speed_text,
                    ", ".join(interface.get("ip_addresses", [])),
                    neighbor.get("system_name", ""),
                    neighbor.get("remote_port", ""),
                ))

    def sync_switches(self):
        if not self.switch_snapshots:
            messagebox.showinfo("Switche", "Bitte zuerst mindestens einen Switch auslesen.", parent=self.root)
            return
        if self.nb is None and not self.load_switch_devices():
            return
        switch_map = {switch["id"]: switch for switch in self.current_switches}
        totals = {"created": 0, "updated": 0, "ip_addresses": 0, "cables": 0}
        errors = []
        for switch_id, snapshot in self.switch_snapshots.items():
            switch = switch_map.get(switch_id)
            if not switch:
                continue
            self.switch_log(f"Synchronisiere {switch['name']} nach NetBox …")
            result = sync_snapshot_to_netbox(
                self.nb,
                switch,
                snapshot,
                create_cables=self.create_neighbor_cables.get(),
            )
            for key in totals:
                totals[key] += result[key]
            errors.extend(f"{switch['name']}: {error}" for error in result["errors"])
        text = (
            f"{totals['created']} Schnittstellen erstellt, {totals['updated']} aktualisiert, "
            f"{totals['ip_addresses']} IP-Adressen synchronisiert, {totals['cables']} LLDP-Kabel angelegt."
        )
        self.switch_log(text)
        if errors:
            messagebox.showwarning("Synchronisierung teilweise abgeschlossen", text + "\n\n" + "\n".join(errors), parent=self.root)
        else:
            messagebox.showinfo("Switch-Synchronisierung abgeschlossen", text, parent=self.root)

    def clear_loaded_data(self):
        self.vms = []
        if hasattr(self, "vm_tree"):
            self.vm_tree.delete(*self.vm_tree.get_children())
        for combo in (getattr(self, "cluster_combo", None), getattr(self, "tenant_combo", None)):
            if combo:
                combo.set("")
                combo["values"] = ()

    # Connections and loading

    def connect_all(self):
        try:
            if not self.netbox_url.get().strip() or not self.netbox_token.get():
                raise ValueError("Bitte NetBox-URL und API-Token ausfüllen.")
            active_sources = [source for source in self.current_sources if source.get("enabled", True)]
            if not active_sources:
                raise ValueError("Bitte mindestens eine aktive Virtualisierungsquelle anlegen.")
            if not self.save_profile(show_message=False):
                return

            self.connect_netbox()
            self.load_netbox_data()
            self.vms = []
            failed = []
            for source in active_sources:
                try:
                    if source["type"] == "vmware":
                        loaded = self.load_vmware_source(source)
                    else:
                        loaded = self.load_proxmox_source(source)
                    self.vms.extend(loaded)
                    self.log(f"{source['name']}: {len(loaded)} Systeme geladen")
                except Exception as error:
                    failed.append(f"{source['name']}: {error}")
                    self.log(f"FEHLER bei {source['name']}: {error}")
            self.refresh_vm_tree()
            if not self.vms:
                raise RuntimeError("Keine VMs geladen. " + (" | ".join(failed) if failed else ""))
            text = f"{len(self.vms)} Systeme aus {len(active_sources) - len(failed)} Quellen geladen."
            if failed:
                text += "\n\nNicht erreichbar:\n" + "\n".join(failed)
                messagebox.showwarning("Teilweise geladen", text)
            else:
                messagebox.showinfo("Erfolg", text)
        except Exception as error:
            messagebox.showerror("Verbindungsfehler", str(error))

    def connect_netbox(self):
        self.log("Verbinde zu NetBox …")
        self.nb = pynetbox.api(self.netbox_url.get().strip(), token=self.netbox_token.get())
        self.nb.http_session.verify = not self.netbox_ignore_ssl.get()

    def load_netbox_data(self):
        self.log("Lade NetBox-Zieldaten …")
        clusters = list(self.nb.virtualization.clusters.all())
        tenants = list(self.nb.tenancy.tenants.all())
        self.vrfs = list(self.nb.ipam.vrfs.all())
        self.cluster_map = {item.name: item.id for item in clusters}
        self.tenant_map = {item.name: item.id for item in tenants}
        self.cluster_tenants = {}
        for cluster in clusters:
            tenant = getattr(cluster, "tenant", None)
            if tenant:
                tenant_name = getattr(tenant, "name", None)
                tenant_id = getattr(tenant, "id", None)
                if isinstance(tenant, dict):
                    tenant_name = tenant.get("name")
                    tenant_id = tenant.get("id")
                if tenant_name and tenant_id:
                    self.cluster_tenants[cluster.name] = {
                        "name": tenant_name,
                        "id": tenant_id,
                    }
                    self.tenant_map.setdefault(tenant_name, tenant_id)
        self.cluster_combo["values"] = sorted(self.cluster_map, key=str.casefold)
        self.tenant_combo["values"] = sorted(self.tenant_map, key=str.casefold)
        self.platforms = list(self.nb.dcim.platforms.all())

    def on_cluster_selected(self, _event=None):
        cluster_name = self.cluster_combo.get()
        cluster_tenant = self.cluster_tenants.get(cluster_name)
        if cluster_tenant:
            self.tenant_combo.set(cluster_tenant["name"])
            self.status_text.set(
                f"Mandant „{cluster_tenant['name']}“ vom Cluster „{cluster_name}“ übernommen"
            )
        else:
            self.tenant_combo.set("")
            self.status_text.set(
                f"Cluster „{cluster_name}“ hat keinen Mandanten – Auswahl vor dem Sync erforderlich"
            )

    def load_vmware_source(self, source):
        self.log(f"Verbinde zu VMware: {source['name']} …")
        context = ssl._create_unverified_context() if source.get("ignore_ssl") else None
        service_instance = SmartConnect(
            host=source["host"], user=source["username"], pwd=source["password"], sslContext=context
        )
        try:
            content = service_instance.RetrieveContent()
            view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
            try:
                # Fetch every required VM property in one PropertyCollector call.
                # Accessing properties on each ManagedObject separately is extremely
                # slow on vCenter because every access may cause another SOAP request.
                traversal = vim.PropertyCollector.TraversalSpec(
                    name="vmViewTraversal", type=vim.view.ContainerView,
                    path="view", skip=False,
                )
                object_spec = vim.PropertyCollector.ObjectSpec(
                    obj=view, skip=True, selectSet=[traversal]
                )
                property_spec = vim.PropertyCollector.PropertySpec(
                    type=vim.VirtualMachine, all=False, pathSet=[
                        "name", "config.hardware.memoryMB", "config.hardware.numCPU",
                        "config.hardware.device", "config.guestFullName",
                        "runtime.powerState", "guest.net",
                    ],
                )
                filter_spec = vim.PropertyCollector.FilterSpec(
                    objectSet=[object_spec], propSet=[property_spec]
                )
                options = vim.PropertyCollector.RetrieveOptions()
                result = content.propertyCollector.RetrievePropertiesEx([filter_spec], options)
                objects = list(result.objects or [])
                while result.token:
                    result = content.propertyCollector.ContinueRetrievePropertiesEx(result.token)
                    objects.extend(result.objects or [])
                normalized = []
                for item in objects:
                    properties = {prop.name: prop.val for prop in (item.propSet or [])}
                    if properties.get("name"):
                        normalized.append(self.normalize_vmware_vm(properties, source))
                return normalized
            finally:
                view.Destroy()
        finally:
            Disconnect(service_instance)

    def normalize_vmware_vm(self, properties, source):
        disks = sum(
            round(device.capacityInKB / 1024)
            for device in properties.get("config.hardware.device", [])
            if isinstance(device, vim.vm.device.VirtualDisk)
        )
        interfaces = []
        for index, net in enumerate(properties.get("guest.net", []) or []):
            mac = getattr(net, "macAddress", None)
            if not mac:
                continue
            ips = self.vmware_guest_ips(net)
            interfaces.append({"name": getattr(net, "device", None) or f"NIC-{index}", "mac": mac, "ips": ips})
        return {
            "name": properties["name"],
            "source": source["name"],
            "source_type": "VMware",
            "kind": "VM",
            "status": "active" if "poweredOn" in str(properties.get("runtime.powerState", "")) else "offline",
            "vcpus": int(properties.get("config.hardware.numCPU", 0)),
            "memory": int(properties.get("config.hardware.memoryMB", 0)),
            "disk": disks,
            "guest_os": properties.get("config.guestFullName", "") or "",
            "interfaces": interfaces,
        }

    def vmware_guest_ips(self, net):
        """Prefer VMware-reported prefix lengths and retain legacy IP fallback."""
        ips = []
        ip_config = getattr(net, "ipConfig", None)
        for entry in (getattr(ip_config, "ipAddress", None) or []):
            value = getattr(entry, "ipAddress", None)
            prefix = getattr(entry, "prefixLength", None)
            if value and self.valid_guest_ip(value):
                ips.append(f"{value}/{prefix}" if prefix is not None else value)

        known_addresses = {
            str(ipaddress.ip_interface(value).ip if "/" in value else ipaddress.ip_address(value))
            for value in ips
        }
        for value in (getattr(net, "ipAddress", None) or []):
            if not self.valid_guest_ip(value):
                continue
            normalized = str(ipaddress.ip_address(value))
            if normalized not in known_addresses:
                ips.append(value)
                known_addresses.add(normalized)
        return ips

    def proxmox_session(self, source):
        session = requests.Session()
        session.verify = not source.get("ignore_ssl")
        session.headers["Authorization"] = f"PVEAPIToken={source['token_id']}={source['token_secret']}"
        return session

    @staticmethod
    def proxmox_get(session, base_url, path):
        response = session.get(f"{base_url}/api2/json{path}", timeout=30)
        response.raise_for_status()
        return response.json().get("data")

    def load_proxmox_source(self, source):
        self.log(f"Verbinde zu Proxmox: {source['name']} …")
        session = self.proxmox_session(source)
        resources = self.proxmox_get(session, source["url"], "/cluster/resources?type=vm") or []
        self.log(f"{source['name']}: Cluster-API meldet {len(resources)} Ressourcen")

        # Some restricted API tokens may not see the cluster-wide resource
        # endpoint. Try the per-node endpoints as a compatible fallback.
        if not resources:
            resources = self.load_proxmox_resources_by_node(session, source)
            self.log(f"{source['name']}: Node-Abfrage meldet {len(resources)} Ressourcen")

        result = []
        for resource in resources:
            kind = resource.get("type")
            if kind not in ("qemu", "lxc") or self.proxmox_is_template(resource.get("template")):
                continue
            if kind == "lxc" and not source.get("include_lxc", True):
                continue
            try:
                result.append(self.normalize_proxmox_guest(session, source, resource))
            except Exception as error:
                self.log(f"WARNUNG {source['name']} / VMID {resource.get('vmid')}: {error}")
        if not result:
            raise RuntimeError(
                "API erreichbar, aber keine sichtbaren VMs/LXCs gefunden. "
                "Dem Benutzer und dem API-Token mindestens PVEAuditor auf /vms zuweisen."
            )
        return result

    def load_proxmox_resources_by_node(self, session, source):
        resources = []
        nodes = self.proxmox_get(session, source["url"], "/nodes") or []
        kinds = ["qemu"]
        if source.get("include_lxc", True):
            kinds.append("lxc")
        for node_data in nodes:
            node = node_data.get("node")
            if not node:
                continue
            for kind in kinds:
                guests = self.proxmox_get(session, source["url"], f"/nodes/{node}/{kind}") or []
                for guest in guests:
                    resource = dict(guest)
                    resource["node"] = node
                    resource["type"] = kind
                    resources.append(resource)
        return resources

    @staticmethod
    def proxmox_is_template(value):
        return str(value).strip().lower() in ("1", "true", "yes")

    def normalize_proxmox_guest(self, session, source, resource):
        kind = resource["type"]
        node = resource["node"]
        vmid = resource["vmid"]
        config = self.proxmox_get(session, source["url"], f"/nodes/{node}/{kind}/{vmid}/config") or {}
        interfaces = self.proxmox_interfaces(config, kind)
        if kind == "qemu":
            self.add_qemu_agent_ips(session, source, node, vmid, interfaces)
        guest_os = self.proxmox_os_name(config.get("ostype", ""), kind)
        return {
            "name": resource.get("name") or f"{kind}-{vmid}",
            "source": source["name"],
            "source_type": "Proxmox",
            "kind": "LXC" if kind == "lxc" else "VM",
            "status": "active" if resource.get("status") == "running" else "offline",
            "vcpus": int(resource.get("maxcpu") or config.get("cores") or 0),
            "memory": round(int(resource.get("maxmem") or 0) / 1024 / 1024) or int(config.get("memory") or 0),
            "disk": round(int(resource.get("maxdisk") or 0) / 1024 / 1024),
            "guest_os": guest_os,
            "interfaces": interfaces,
        }

    def proxmox_interfaces(self, config, kind):
        interfaces = []
        for key in sorted(config):
            if not re.fullmatch(r"net\d+", key):
                continue
            values = self.parse_proxmox_options(str(config[key]))
            if kind == "qemu":
                mac = next((value for name, value in values.items() if name in ("virtio", "e1000", "e1000e", "vmxnet3", "rtl8139")), None)
                name = key
                ips = []
            else:
                mac = values.get("hwaddr")
                name = values.get("name", key)
                ip_value = values.get("ip", "")
                ips = [ip_value] if ip_value and ip_value not in ("dhcp", "manual") else []
            if mac:
                interfaces.append({"name": name, "mac": mac, "ips": ips})
        return interfaces

    @staticmethod
    def parse_proxmox_options(value):
        result = {}
        for part in value.split(","):
            if "=" in part:
                key, item = part.split("=", 1)
                result[key.strip().lower()] = item.strip()
        return result

    def add_qemu_agent_ips(self, session, source, node, vmid, interfaces):
        try:
            payload = self.proxmox_get(session, source["url"], f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces") or {}
            agent_interfaces = payload.get("result", payload if isinstance(payload, list) else [])
            by_mac = {interface["mac"].lower(): interface for interface in interfaces}
            for agent_interface in agent_interfaces:
                mac = (agent_interface.get("hardware-address") or "").lower()
                target = by_mac.get(mac)
                if not target:
                    continue
                for address in agent_interface.get("ip-addresses", []):
                    ip = address.get("ip-address")
                    prefix = address.get("prefix")
                    if ip and self.valid_guest_ip(ip):
                        target["ips"].append(f"{ip}/{prefix}" if prefix is not None else ip)
        except (requests.RequestException, ValueError, KeyError):
            # The guest agent is optional; configured MAC addresses are still useful.
            return

    @staticmethod
    def proxmox_os_name(ostype, kind):
        names = {
            "win11": "Microsoft Windows 11", "win10": "Microsoft Windows 10/2016/2019",
            "win8": "Microsoft Windows 8/2012", "win7": "Microsoft Windows 7/2008 R2",
            "winxp": "Microsoft Windows XP", "l26": "Linux 2.6+", "l24": "Linux 2.4",
            "solaris": "Solaris", "other": "Other",
        }
        return names.get(ostype, "Linux Container" if kind == "lxc" else ostype)

    def refresh_vm_tree(self):
        self.vm_tree.delete(*self.vm_tree.get_children())
        for index, vm_data in enumerate(self.vms):
            self.vm_tree.insert("", "end", iid=str(index), values=(
                vm_data["name"], vm_data["source"], vm_data["kind"], vm_data["status"],
                vm_data["vcpus"], vm_data["memory"],
            ))

    def select_all(self):
        self.vm_tree.selection_set(self.vm_tree.get_children())

    # NetBox import

    @staticmethod
    def valid_guest_ip(value):
        try:
            address = ipaddress.ip_interface(value).ip if "/" in value else ipaddress.ip_address(value)
            return not (address.is_loopback or address.is_link_local or address.is_unspecified)
        except ValueError:
            return False

    @staticmethod
    def ip_with_prefix(value):
        try:
            if "/" in value:
                return str(ipaddress.ip_interface(value))
            address = ipaddress.ip_address(value)
            return f"{address}/24" if address.version == 4 else f"{address}/64"
        except ValueError:
            return None

    def get_platform_id(self, guest_os):
        guest_os = (guest_os or "").lower()
        if not guest_os:
            return None
        for platform in self.platforms:
            if platform.name.lower() in guest_os or guest_os in platform.name.lower():
                return platform.id
        return None

    def import_vms(self):
        selected = self.vm_tree.selection()
        if not selected:
            messagebox.showwarning("Hinweis", "Keine VMs ausgewählt.")
            return
        cluster_name = self.cluster_combo.get()
        cluster_id = self.cluster_map.get(cluster_name)
        if not cluster_id:
            messagebox.showerror("Fehler", "Bitte einen NetBox-Cluster auswählen.")
            return
        inherited_tenant = self.cluster_tenants.get(cluster_name, {}).get("name", "")
        default_tenant = self.tenant_combo.get() or inherited_tenant
        confirmation = TenantConfirmationDialog(
            self.root,
            cluster_name,
            sorted(self.tenant_map, key=str.casefold),
            default_tenant,
        )
        if confirmation.result is None:
            self.log("Synchronisierung durch Benutzer abgebrochen")
            return
        tenant_name = confirmation.result
        tenant_id = self.tenant_map[tenant_name]
        self.tenant_combo.set(tenant_name)
        self.log(f"Mandant bestätigt: {tenant_name}")
        vrf_id = self.select_or_create_vrf(tenant_name, tenant_id, cluster_name)
        if vrf_id is None:
            self.log("Synchronisierung ohne VRF-Auswahl abgebrochen")
            return
        self.synced_prefixes = set()
        prefix_plans = self.prepare_network_import(selected, tenant_name, tenant_id, vrf_id)
        if prefix_plans is None:
            self.log("Synchronisierung in der Netzwerkprüfung abgebrochen")
            return
        self.prefix_plans = prefix_plans
        failures = []
        for item_id in selected:
            vm_data = self.vms[int(item_id)]
            try:
                self.sync_vm(vm_data, cluster_id, tenant_id, vrf_id)
            except Exception as error:
                failures.append(f"{vm_data['name']}: {error}")
                self.log(f"FEHLER {vm_data['name']}: {error}")
        if failures:
            messagebox.showwarning("Import abgeschlossen", "Einige Systeme konnten nicht importiert werden:\n\n" + "\n".join(failures))
        else:
            messagebox.showinfo("Fertig", "VM-Import und Synchronisierung abgeschlossen.")
        self.log("Synchronisierung abgeschlossen")

    def select_or_create_vrf(self, tenant_name, tenant_id, cluster_name):
        tenant_vrfs = self.tenant_vrfs(tenant_id)
        dialog = VRFSelectionDialog(
            self.root,
            tenant_name,
            cluster_name,
            sorted(tenant_vrfs, key=lambda item: item.name.casefold()),
        )
        if dialog.result is None:
            return None
        if not dialog.result["create"]:
            vrf_id = dialog.result["id"]
            selected = next(vrf for vrf in tenant_vrfs if vrf.id == vrf_id)
            self.log(f"Vorhandene VRF ausgewählt: {selected.name}")
            return vrf_id

        vrf_name = dialog.result["name"]
        try:
            return self.get_or_create_tenant_vrf(vrf_name, tenant_id)
        except Exception as error:
            messagebox.showerror("VRF konnte nicht erstellt werden", str(error))
            return None

    def tenant_vrfs(self, tenant_id):
        return sorted(
            (
                vrf for vrf in self.vrfs
                if getattr(getattr(vrf, "tenant", None), "id", None) == tenant_id
            ),
            key=lambda item: item.name.casefold(),
        )

    def prepare_network_import(self, selected_items, tenant_name, tenant_id, default_vrf_id):
        detected = {}
        for item_id in selected_items:
            vm_data = self.vms[int(item_id)]
            for interface in vm_data.get("interfaces", []):
                for ip_value in dict.fromkeys(interface.get("ips", [])):
                    address = self.ip_with_prefix(ip_value)
                    if not address:
                        continue
                    parsed = ipaddress.ip_interface(address)
                    if parsed.version == 4:
                        suggested_prefixlen = 24
                    else:
                        suggested_prefixlen = parsed.network.prefixlen
                    suggested_network = ipaddress.ip_network(
                        f"{parsed.ip}/{suggested_prefixlen}",
                        strict=False,
                    )
                    key = str(suggested_network)
                    detected.setdefault(
                        key,
                        {
                            "host_address": parsed.ip,
                            "host_addresses": set(),
                            "detected_prefixlen": parsed.network.prefixlen,
                            "suggested_prefixlen": suggested_prefixlen,
                            "suggested_network": suggested_network,
                        },
                    )["host_addresses"].add(parsed.ip)

        tenant_vrf_ids = {vrf.id for vrf in self.tenant_vrfs(tenant_id)}
        self.preexisting_prefixes = []
        missing = []
        for item in detected.values():
            group_is_missing = False
            for host in item["host_addresses"]:
                covering = self.find_covering_prefixes(host, tenant_vrf_ids)
                default_matches = [match for match in covering if match[3] == default_vrf_id]
                matching_vrfs = {match[3] for match in covering}
                reusable = None
                if default_matches:
                    reusable = max(default_matches, key=lambda match: match[0])
                elif len(matching_vrfs) == 1:
                    reusable = max(covering, key=lambda match: match[0])
                if reusable:
                    _, prefix, existing_network, existing_vrf_id = reusable
                    cached = (prefix, existing_network, existing_vrf_id)
                    if cached not in self.preexisting_prefixes:
                        self.preexisting_prefixes.append(cached)
                else:
                    group_is_missing = True
            if group_is_missing:
                missing.append(item)

        if not missing:
            self.log("Keine neuen Netzwerke erkannt")
            return []

        dialog = DetectedNetworksDialog(
            self.root,
            missing,
            self.tenant_vrfs(tenant_id),
            default_vrf_id,
            tenant_name,
        )
        if dialog.result is None:
            return None

        for decision in dialog.result:
            if decision.get("create_vrf"):
                decision["vrf_id"] = self.get_or_create_tenant_vrf(decision["vrf_name"], tenant_id)
                decision["create_vrf"] = False
        self.log(f"Netzwerkplan bestätigt: {len(dialog.result)} neue(s) Netzwerk(e)")
        return dialog.result

    def open_network_manager(self):
        if self.nb is None or not self.tenant_map:
            messagebox.showwarning(
                "Keine NetBox-Verbindung",
                "Bitte zuerst mit NetBox verbinden und die Daten laden.",
                parent=self.root,
            )
            return
        NetworkManagementDialog(self.root, self, self.tenant_combo.get())

    def create_tenant_vrfs_batch(self, tenant_name, names):
        tenant_id = self.tenant_map[tenant_name]
        created = 0
        reused = 0
        errors = []
        for name in names:
            exists = any(vrf.name == name for vrf in self.tenant_vrfs(tenant_id))
            try:
                self.get_or_create_tenant_vrf(name, tenant_id)
                if exists:
                    reused += 1
                else:
                    created += 1
            except Exception as error:
                errors.append(f"{name}: {error}")
                self.log(f"FEHLER VRF {name}: {error}")
        return created, reused, errors

    def find_prefixes_in_vrf(self, network, vrf_id):
        return [
            prefix for prefix in self.nb.ipam.prefixes.filter(prefix=str(network))
            if getattr(getattr(prefix, "vrf", None), "id", None) == vrf_id
        ]

    def create_prefixes_batch(self, tenant_name, vrf_id, values):
        tenant_id = self.tenant_map[tenant_name]
        created = 0
        reused = 0
        canonical = []
        errors = []
        for value in values:
            try:
                network = ipaddress.ip_network(value, strict=False)
                if network.version == 4 and str(network.network_address).split(".")[-1] != "0":
                    raise ValueError("IPv4-Netzadresse muss mit .0 enden")
                network_text = str(network)
                candidates = self.find_prefixes_in_vrf(network_text, vrf_id)
                if len(candidates) > 1:
                    raise ValueError("mehrere identische Präfixe in dieser VRF gefunden")
                if candidates:
                    candidates[0].update({"tenant": tenant_id})
                    reused += 1
                else:
                    self.nb.ipam.prefixes.create({
                        "prefix": network_text,
                        "status": "active",
                        "tenant": tenant_id,
                        "vrf": vrf_id,
                    })
                    created += 1
                canonical.append(network_text)
                self.log(f"Netzwerk verarbeitet: {network_text}")
            except Exception as error:
                errors.append(f"{value}: {error}")
                self.log(f"FEHLER Netzwerk {value}: {error}")
        return created, reused, canonical, errors

    def get_or_create_tenant_vrf(self, vrf_name, tenant_id):
        existing = next(
            (
                vrf for vrf in self.vrfs
                if vrf.name == vrf_name
                and getattr(getattr(vrf, "tenant", None), "id", None) == tenant_id
            ),
            None,
        )
        if existing:
            self.log(f"Vorhandene VRF verwendet: {vrf_name}")
            return existing.id

        vrf = self.nb.ipam.vrfs.create({
            "name": vrf_name,
            "tenant": tenant_id,
            "enforce_unique": True,
            "description": "Automatisch erstellt durch NetBox VM Import",
        })
        self.vrfs.append(vrf)
        self.log(f"VRF erstellt: {vrf_name}")
        return vrf.id

    def sync_vm(self, source_vm, cluster_id, tenant_id, vrf_id):
        self.log(f"Synchronisiere {source_vm['name']} ({source_vm['source']}) …")
        payload = {
            "name": source_vm["name"], "cluster": cluster_id, "status": source_vm["status"],
            "vcpus": source_vm["vcpus"], "memory": source_vm["memory"], "disk": source_vm["disk"],
            "tenant": tenant_id,
        }
        platform_id = self.get_platform_id(source_vm.get("guest_os"))
        if platform_id:
            payload["platform"] = platform_id

        netbox_vm = self.nb.virtualization.virtual_machines.get(name=source_vm["name"])
        if netbox_vm:
            netbox_vm.update(payload)
            self.log(f"VM aktualisiert: {source_vm['name']}")
        else:
            netbox_vm = self.nb.virtualization.virtual_machines.create(payload)
            self.log(f"VM erstellt: {source_vm['name']}")

        primary_ipv4 = None
        primary_ipv6 = None
        for interface in source_vm.get("interfaces", []):
            interface_payload = {
                "virtual_machine": netbox_vm.id, "name": interface["name"],
                "mac_address": interface["mac"], "enabled": True,
            }
            netbox_interface = self.nb.virtualization.interfaces.get(
                virtual_machine_id=netbox_vm.id, name=interface["name"]
            )
            if netbox_interface:
                netbox_interface.update(interface_payload)
            else:
                netbox_interface = self.nb.virtualization.interfaces.create(interface_payload)

            for ip_value in dict.fromkeys(interface.get("ips", [])):
                address = self.ip_with_prefix(ip_value)
                if not address:
                    continue
                effective_vrf_id = self.sync_prefix_tenant(
                    address,
                    tenant_id,
                    prefix_is_detected="/" in ip_value,
                    default_vrf_id=vrf_id,
                )
                ip_payload = {
                    "address": address, "status": "active",
                    "tenant": tenant_id,
                    "vrf": effective_vrf_id,
                    "assigned_object_type": "virtualization.vminterface",
                    "assigned_object_id": netbox_interface.id,
                }
                try:
                    host_address = str(ipaddress.ip_interface(address).ip)
                    ip_object = self.nb.ipam.ip_addresses.get(
                        address=host_address,
                        vrf_id=effective_vrf_id,
                    )
                    if ip_object:
                        ip_object.update(ip_payload)
                    else:
                        ip_object = self.nb.ipam.ip_addresses.create(ip_payload)
                except Exception as error:
                    raise RuntimeError(f"IP-Adresse {address}: {error}") from error
                version = ipaddress.ip_interface(address).version
                if version == 4 and not primary_ipv4:
                    primary_ipv4 = ip_object.id
                elif version == 6 and not primary_ipv6:
                    primary_ipv6 = ip_object.id

        primary = {}
        if primary_ipv4:
            primary["primary_ip4"] = primary_ipv4
        if primary_ipv6:
            primary["primary_ip6"] = primary_ipv6
        if primary:
            netbox_vm.update(primary)

    def find_covering_prefixes(self, host_address, allowed_vrf_ids):
        host = ipaddress.ip_address(host_address)
        host_prefix = f"{host}/{host.max_prefixlen}"
        try:
            candidates = list(self.nb.ipam.prefixes.filter(contains_or_equals=host_prefix))
        except Exception as error:
            self.log(f"Hinweis: Präfixsuche für {host} nicht möglich: {error}")
            return []
        matches = []
        for prefix in candidates:
            prefix_vrf_id = getattr(getattr(prefix, "vrf", None), "id", None)
            if prefix_vrf_id not in allowed_vrf_ids:
                continue
            try:
                parsed = ipaddress.ip_network(str(prefix.prefix))
            except ValueError:
                continue
            if host in parsed:
                matches.append((parsed.prefixlen, prefix, str(parsed), prefix_vrf_id))
        return matches

    def sync_prefix_tenant(self, address, tenant_id, prefix_is_detected, default_vrf_id):
        """Assign a tenant and apply the previously confirmed network import plan."""
        if not hasattr(self, "synced_prefixes"):
            self.synced_prefixes = set()
        if not hasattr(self, "prefix_plans"):
            self.prefix_plans = []
        parsed_interface = ipaddress.ip_interface(address)
        host_address = parsed_interface.ip
        network = str(parsed_interface.network)
        vrf_id = default_vrf_id

        for existing_prefix, existing_network, existing_vrf_id in getattr(self, "preexisting_prefixes", []):
            if host_address in ipaddress.ip_network(existing_network):
                existing_prefix.update({"tenant": tenant_id})
                self.synced_prefixes.add((existing_network, existing_vrf_id))
                self.log(f"Vorhandenes Netzwerk wiederverwendet: {existing_network}")
                return existing_vrf_id

        for cached_network, cached_vrf in self.synced_prefixes:
            if host_address in ipaddress.ip_network(cached_network):
                return cached_vrf

        cache_key = (network, vrf_id)
        candidates = self.find_prefixes_in_vrf(network, vrf_id)

        if len(candidates) > 1:
            self.log(
                f"Präfix {network} nicht geändert: mehrere passende VRFs gefunden"
            )
            return vrf_id

        try:
            if candidates:
                candidates[0].update({"tenant": tenant_id})
                self.log(f"Mandant am Präfix aktualisiert: {network}")
            else:
                tenant_vrf_ids = {vrf.id for vrf in self.tenant_vrfs(tenant_id)}
                covering = self.find_covering_prefixes(host_address, tenant_vrf_ids)
                default_matches = [item for item in covering if item[3] == vrf_id]
                matching_vrfs = {item[3] for item in covering}
                reusable = None
                if default_matches:
                    reusable = max(default_matches, key=lambda item: item[0])
                elif len(matching_vrfs) == 1:
                    reusable = max(covering, key=lambda item: item[0])
                if reusable:
                    _, existing_prefix, existing_network, existing_vrf_id = reusable
                    existing_prefix.update({"tenant": tenant_id})
                    self.synced_prefixes.add((existing_network, existing_vrf_id))
                    self.log(f"Vorhandenes Netzwerk wiederverwendet: {existing_network}")
                    return existing_vrf_id
                matching_plans = [
                    decision for decision in self.prefix_plans
                    if host_address in ipaddress.ip_network(decision["network"])
                ]
                if not matching_plans:
                    self.log(f"Präfix für {host_address} nicht angelegt (Netzwerkplan)")
                    return vrf_id
                decision = max(
                    matching_plans,
                    key=lambda item: ipaddress.ip_network(item["network"]).prefixlen,
                )
                network = decision["network"]
                vrf_id = decision["vrf_id"]
                cache_key = (network, vrf_id)
                selected_candidates = self.find_prefixes_in_vrf(network, vrf_id)
                if len(selected_candidates) > 1:
                    self.log(f"Präfix {network} nicht geändert: mehrere passende VRFs gefunden")
                    return vrf_id
                if selected_candidates:
                    selected_candidates[0].update({"tenant": tenant_id})
                    self.log(f"Mandant am Präfix aktualisiert: {network}")
                else:
                    prefix_payload = {"prefix": network, "status": "active", "tenant": tenant_id}
                    if vrf_id is not None:
                        prefix_payload["vrf"] = vrf_id
                    self.nb.ipam.prefixes.create(prefix_payload)
                    self.log(f"Präfix nach Bestätigung erstellt: {network}")
        except Exception as error:
            raise RuntimeError(f"Präfix {network}: {error}") from error

        self.synced_prefixes.add(cache_key)
        return vrf_id


if __name__ == "__main__":
    root = tk.Tk()
    application = NetBoxVMImporter(root)
    root.mainloop()
