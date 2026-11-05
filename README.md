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
- Keyboard shortcuts: `Ctrl+1…8` navigation, `Ctrl+9` settings, `F5` reload/read, `Ctrl+F` search
- Updates from the Gitea releases: checked on start (can be switched off in the
  settings, or run on demand); after confirmation the installed version runs the
  new setup silently and the portable EXE replaces itself, then the application
  restarts. A version can be skipped

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
  platform suggested from model/manufacturer; members of a NetBox virtual
  chassis are added once, through the chassis master
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
- LAG members (Aruba CX `lag N`, ArubaOS-Switch `trunk … trkN`, Comware
  `port link-aggregation group N`) get their LAG in the label, e.g.
  `10,20 (LAG 1)`, and are assigned to the LAG interface in NetBox (field
  *LAG*; only on the same device). Members without own VLAN configuration take
  the VLANs of their LAG. NetBox Utilities 0.25.0 or later ignores the LAG note
  when it evaluates VLAN labels
- Automatic port mapping when NetBox names differ from the switch (e.g. `5` ↔
  `1/1/5`): equal name, then equal port number, then natural order
- **Mapping review** before the preview: non-obvious port mappings and every
  LLDP/CDP neighbour can be corrected; a neighbour can be assigned manually to
  any device of the switch's tenant group. Choices are stored in the profile
- Networks and VLANs in the same review: per network the VRF (global, a VRF of
  the tenant group or a new one) and whether a missing prefix is created. Network
  decisions are stored per profile like the VLAN decisions; networks whose
  prefix exists or that were decided before sit in a collapsed, editable section
- VLAN mapping: switch VLANs are matched to NetBox VLANs of the switch's tenant
  group by VID (own tenant, site and similar names decide between several);
  each VLAN can be mapped manually, skipped or created with a name and a tenant
  of the tenant group of your choice (pre-filled from the switch configuration
  and the switch's tenant). VLANs can also be **ignored** on all switches –
  they are neither created nor assigned and are left out of the labels. VLAN
  decisions are stored per profile (shared by all its switches); only VLANs
  that still need a decision are shown, stored and unambiguous ones are in a
  collapsed section that can be expanded for editing. Mapped VLANs are assigned
  to the ports (802.1Q mode, untagged/tagged)
- Tenant required: a switch without tenant in NetBox is refused; every created
  IP address, prefix, VRF, VLAN and cable gets the switch's tenant
- Stacks are detected (Aruba CX VSF, ArubaOS-Switch stacking/VSF, HPE Comware
  IRF): every member is assigned to its own NetBox device (suggested from the
  NetBox virtual chassis positions, stored per switch) and gets its own ports;
  LAGs and VLAN interfaces stay on the switch's device
- Optional: with the [NetBox Utilities](https://git.mrblake.cc/MrBlake/Netbox-Utilities)
  plugin (0.24.0 or later) a detected stack is also entered as its switch stack
  (the switch's device as stack switch, the other member devices as members);
  members entered by hand are kept. Without the plugin this step is skipped
- Optional configuration backup ("Konfiguration als Anhang sichern"): with the
  [netbox-attachments](https://github.com/Kani999/netbox-attachments) plugin the
  running configuration is attached to the switch's device as a text file.
  Passwords, keys and SNMP communities are removed first; a new attachment is
  only added when the configuration changed (SHA-256 in the description).
  Older plugin versions (parent on the attachment) and newer ones (separate
  assignments) are supported; without the plugin the step is skipped with a note
- Cables from LLDP/CDP, also with FQDN system names or via the management IP.
  Neighbors that report their MAC address as port ID (e.g. access points) are
  matched by MAC, and a device with a single physical port is cabled there
- Existing cables are checked against LLDP/CDP: a different far end is reported
  as a note. Only with the optional "Bestehende Kabel anpassen" (confirmed with
  a warning, NetBox is the source of truth) the cable is moved to the reported
  neighbor; such changes are marked with ⚠ in the preview. Cables to patch
  panels (front/rear ports) and far ends that already have another cable are
  never changed
- The switch configuration is only changed when "Enable LLDP if needed" is
  explicitly switched on
- One-off import without SSH ("Backup importieren …"): configuration backups
  of D-Link DGS-1210 (`config.bin`) and Linksys LGS (`*.cfg` or the contained
  `SNCSR.conf`) switches are read offline. Hostname, location, management IP,
  port descriptions, admin state, untagged/tagged VLANs and VLAN names are
  taken over; the switch is matched to the NetBox device by its hostname and
  then synchronized like an SSH result (no LLDP neighbors, hence no cables)
  The D-Link `config.bin` is attached to the device unchanged (it contains the
  switch's secrets as stored by the switch); the Linksys configuration is
  attached as text with secrets removed

### Sophos access points (CSV from Sophos Central)

- Loads the access point export of Sophos Central (comma or semicolon, UTF-8 or
  Windows encoding) and checks it: missing site, duplicate IP, serial number or
  MAC address
- No tenant to pick up front: existing APs are found in all of NetBox by serial
  number, MAC address and name; Sophos APs in NetBox that are missing in the
  export are listed as "only in NetBox" for the tenants the export concerns
- Sophos sites are mapped to NetBox sites (suggested by name, stored per
  profile); the site is optional – APs without one go to a default site, since
  NetBox requires a site for every device. The tenant is mandatory and chosen
  deliberately per Sophos site – it is never preselected. Sophos sites left without site and tenant are
  ignored (stored per profile) and not transferred
- New APs are created with Sophos device type, role, site, serial number,
  firmware, interface with MAC and IP address (primary IPv4); existing ones get
  missing serial numbers and tenants, optionally the Sophos name. Preview before
  writing

### Network scanner (read only)

- Scans IP ranges, networks and host names (`192.0.2.1-254`, `192.0.2.0/24`,
  up to 4096 addresses) in parallel; "own network" fills in the networks of
  this computer
- Finds hosts by ping (Windows ICMP API, no administrator rights), ARP in the
  local subnet and TCP ports – hosts that block ping are found as well
- Shows name (reverse DNS and NetBIOS, with workgroup/domain), MAC address,
  open services from a configurable port list and response time
- Compares every address with NetBox automatically: NetBox object (device or VM)
  and DNS name next to each IP, with the state "matches", "name differs", "not
  in NetBox" or "in NetBox, not reachable"; nothing is written to NetBox
- Filters for reachable hosts and deviations; double click or context menu opens
  HTTP/HTTPS, Remote Desktop, file shares or the IP address in NetBox

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
