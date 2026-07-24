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


class NetBoxVMImporter:
    def __init__(self, root):
        self.root = root
        self.root.title("VMware & Proxmox → NetBox Synchronizer")
        self.root.geometry("1160x850")
        self.root.minsize(960, 700)

        self.nb = None
        self.vms = []
        self.profiles = {}
        self.current_sources = []
        self.cluster_map = {}
        self.tenant_map = {}
        self.platforms = []
        self.profile_name = tk.StringVar()
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
        main = ttk.Frame(self.root, padding=18)
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

        self.netbox_url = self.add_field(netbox_card, 0, "URL")
        self.netbox_token = self.add_field(netbox_card, 1, "API-Token", secret=True)
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
        self.tenant_combo = self.add_combo_field(target_card, 1, "Tenant")
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

    @staticmethod
    def add_field(parent, row, label, secret=False):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=5)
        entry = ttk.Entry(parent, show="•" if secret else "")
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
        self.profile_combo["values"] = sorted(self.profiles, key=str.casefold)

    def profile_from_form(self, name):
        return {
            "name": name,
            "netbox_url": self.netbox_url.get().strip(),
            "netbox_token": self.netbox_token.get(),
            "netbox_ignore_ssl": self.netbox_ignore_ssl.get(),
            "sources": [dict(source) for source in self.current_sources],
        }

    def populate_profile(self, name):
        profile = self.profiles.get(name, {})
        self.set_entry(self.netbox_url, profile.get("netbox_url", ""))
        self.set_entry(self.netbox_token, profile.get("netbox_token", ""))
        self.netbox_ignore_ssl.set(bool(profile.get("netbox_ignore_ssl", False)))
        self.current_sources = [dict(source) for source in profile.get("sources", [])]
        self.refresh_source_tree()
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
        self.refresh_source_tree()
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
            self.refresh_source_tree()

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
        self.cluster_map = {item.name: item.id for item in clusters}
        self.tenant_map = {item.name: item.id for item in tenants}
        self.cluster_combo["values"] = sorted(self.cluster_map, key=str.casefold)
        self.tenant_combo["values"] = sorted(self.tenant_map, key=str.casefold)
        self.platforms = list(self.nb.dcim.platforms.all())

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
            ips = [ip for ip in (getattr(net, "ipAddress", None) or []) if self.valid_guest_ip(ip)]
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
        cluster_id = self.cluster_map.get(self.cluster_combo.get())
        tenant_id = self.tenant_map.get(self.tenant_combo.get())
        if not cluster_id:
            messagebox.showerror("Fehler", "Bitte einen NetBox-Cluster auswählen.")
            return
        failures = []
        for item_id in selected:
            vm_data = self.vms[int(item_id)]
            try:
                self.sync_vm(vm_data, cluster_id, tenant_id)
            except Exception as error:
                failures.append(f"{vm_data['name']}: {error}")
                self.log(f"FEHLER {vm_data['name']}: {error}")
        if failures:
            messagebox.showwarning("Import abgeschlossen", "Einige Systeme konnten nicht importiert werden:\n\n" + "\n".join(failures))
        else:
            messagebox.showinfo("Fertig", "VM-Import und Synchronisierung abgeschlossen.")
        self.log("Synchronisierung abgeschlossen")

    def sync_vm(self, source_vm, cluster_id, tenant_id):
        self.log(f"Synchronisiere {source_vm['name']} ({source_vm['source']}) …")
        payload = {
            "name": source_vm["name"], "cluster": cluster_id, "status": source_vm["status"],
            "vcpus": source_vm["vcpus"], "memory": source_vm["memory"], "disk": source_vm["disk"],
        }
        if tenant_id:
            payload["tenant"] = tenant_id
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
                ip_payload = {
                    "address": address, "status": "active",
                    "assigned_object_type": "virtualization.vminterface",
                    "assigned_object_id": netbox_interface.id,
                }
                ip_object = self.nb.ipam.ip_addresses.get(address=address)
                if ip_object:
                    ip_object.update(ip_payload)
                else:
                    ip_object = self.nb.ipam.ip_addresses.create(ip_payload)
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


if __name__ == "__main__":
    root = tk.Tk()
    application = NetBoxVMImporter(root)
    root.mainloop()
