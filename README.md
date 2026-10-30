# NetBox Synchronizer

Windows desktop application that documents virtual machines from VMware
vSphere and Proxmox VE, HPE/Aruba switches and Windows clients in NetBox.

| | |
|---|---|
| **Type** | Desktop application (Python, PySide6) |
| **Platform** | Windows |
| **Repository** | <https://git.mrblake.cc/MrBlake/NetBox-VM-Import-Desktop> |

## Features

### General

- Customer profiles with NetBox target, virtualisation sources and switch
  credentials; switch between them in the sidebar
- Automatic saving; passwords, tokens and secrets are encrypted with Windows DPAPI
- All network access runs in the background — the window stays responsive and
  operations can be cancelled
- Light and dark theme (optionally following Windows)
- Log with filter, search and export, plus a rotating log file under
  `%APPDATA%\NetBox VM Import\logs`
- Keyboard shortcuts: `Ctrl+1…8` navigation, `F5` reload/read, `Ctrl+F` search

### Virtual machines

- Multiple VMware and Proxmox sources per profile, read in parallel; connection
  test directly in the dialog
- QEMU/KVM VMs and optionally LXC containers; IP addresses via VMware Tools or
  the QEMU guest agent
- Table with search, filters (source, status, "new"/"already in NetBox") and
  multi-selection
- Automatic cluster mapping: each vCenter/ESXi or Proxmox cluster is mapped to
  the NetBox cluster with the same (or a uniquely similar) name; manual
  mappings are remembered in the profile, and missing clusters can be created in
  NetBox directly
- Four-step import wizard: **Target** (cluster mapping, tenant preselected from
  the cluster, VRF or global) → **Networks** (check missing prefixes with size
  and VRF, create new VRFs directly) → **Review** (summary of all changes) →
  **Import** (progress, live log, cancel)
- VMs are matched uniquely per cluster; existing prefixes are reused
- MAC addresses are set correctly on NetBox ≥ 4.2 (separate MAC model)

### Switches (Aruba CX, ArubaOS-Switch/ProCurve, HPE Comware)

- Add several NetBox devices in one step — host taken from the primary IP,
  platform suggested from model/manufacturer
- Platform "detect automatically" via SSH
- Parallel SSH queries; SSH host keys are stored on first contact and verified
  afterwards
- Interfaces, interface IP addresses and LLDP/CDP neighbours
- **Preview before writing**: all changes are computed as a dry run and only
  sent after confirmation
- Interface types from device type templates are left untouched; Comware short
  names (`GE1/0/1`) are mapped to full NetBox names (`GigabitEthernet1/0/1`)
- VLAN membership per port (untagged and tagged, read from the running
  configuration) is written to the interface **label**, e.g. `10` or `1,20-22`;
  only VLAN-related lines of the configuration are kept in the raw data
- Automatic port mapping when NetBox names differ from the switch (e.g. `5` ↔
  `1/1/5`): equal name, then equal port number, then natural order
- **Mapping review** before the preview: non-obvious port mappings and every
  LLDP/CDP neighbour can be corrected; a neighbour can be assigned manually to
  any device of the switch's tenant group. Choices are stored in the profile
- Networks and VLANs in the same review: per network the VRF (global, a tenant
  VRF or a new one) and whether a missing prefix is created; missing VLANs
  (named from the switch configuration) are only created when ticked. Existing
  VLANs are assigned to the ports (802.1Q mode, untagged/tagged)
- Tenant required: a switch without tenant in NetBox is refused; every created
  IP address, prefix, VRF, VLAN and cable gets the switch's tenant
- Cables from LLDP/CDP, also with FQDN system names or via the management IP
- The switch configuration is only changed when "Enable LLDP if needed" is
  explicitly switched on

### Windows clients (agentless via WMI/CIM over WinRM)

- Targets from Active Directory (optional search OU, enabled accounts only,
  last logon within N days, include/exclude servers) or as a list of host
  names, IP addresses, networks (`192.0.2.0/24`) and ranges (`192.0.2.10-50`)
- Sign-in via the Windows credential dialog (domain account with local admin
  rights, verified against the domain) or with the current Windows session; the
  password is kept in memory only
- Reads name, manufacturer, model (for Lenovo the model name instead of the
  machine type), serial number, form factor, operating system with
  version/build, logged-on user, CPU/RAM and physical network adapters with MAC
  and IPs
- Parallel queries; unreachable clients stay visible with their AD data, errors
  come with a concrete hint
- Import as NetBox devices with site, role and tenant; manufacturers, device
  types and platforms are created when needed, optionally interfaces, MAC and IP
  addresses — with a preview before writing
- Built-in **setup guide**: group policy (WinRM, service, firewall),
  permissions, common errors, check commands and a connection test for single
  clients

### Offline mode (without NetBox)

- "Offline mode (without NetBox)" checkbox in the sidebar — global, independent
  of the customer profile
- VMs and switches are read without a NetBox connection; switches can then be
  added manually by name
- Selected VMs, clients or read switches are exported as a JSON file
  (`*.nbsync.json`) — without passwords, tokens or user names
- On a machine with NetBox access, "Load from file …" (VMs, switches and
  Windows clients pages) loads the file; the import wizard or dry-run preview
  then runs as usual. Switches are matched by NetBox device ID or name

### VRFs & networks

- Create several VRFs or prefixes for one tenant at once

## Installation

Run `NetBoxSynchronizer-Setup-<version>.exe`. The installer

- installs for the current user **without administrator rights** by default
  ("for all users" can be selected in the wizard),
- creates a Start menu entry and optionally a desktop shortcut,
- updates an existing installation in place,
- asks on uninstall whether customer profiles and logs should be removed too.

Customer profiles of the predecessor ("NetBox VM Import") are migrated
automatically.

## Development

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python main.py          # start the application
python -m pytest        # tests
```

Project layout:

```
netbox_sync/
  core/      UI-independent logic: configuration, NetBox, VMware/Proxmox, VM import, switches, Windows clients
  ui/        Qt interface (PySide6): main window, pages, dialogs, import wizard, theme
  resources/ icons
installer/   Inno Setup script
tests/       pytest (parsers, configuration, sync against an in-memory NetBox)
```

## Build & installer

The installer requires [Inno Setup 6](https://jrsoftware.org/isinfo.php)
(`winget install JRSoftware.InnoSetup`).

```powershell
.\build.ps1                 # tests, PyInstaller build and installer
.\build.ps1 -SkipInstaller  # only dist\NetBoxSynchronizer\
```

Output: `dist\installer\NetBoxSynchronizer-Setup-<version>.exe` and the
portable variant `dist\portable\NetBox VM Import.exe` (runs without
installation; `-SkipPortable` omits it). The version is set in
`netbox_sync/__init__.py`.
