"""Application state shared by all pages."""

import copy

from PySide6.QtCore import QObject, QTimer, Signal

from netbox_sync.core.config import DEFAULT_SETTINGS, ProfileStore, empty_profile
from netbox_sync.ui.tasks import logger

log = logger("app")


class AppState(QObject):
    profiles_changed = Signal()        # names added/removed/renamed
    profile_switched = Signal()        # another profile is active now
    profile_edited = Signal()          # data inside the active profile changed
    settings_changed = Signal()
    netbox_changed = Signal()          # connection status / lookup data
    vms_changed = Signal()
    switches_changed = Signal()        # switch list or discovery results
    clients_changed = Signal()         # Windows client scan results
    save_failed = Signal(str)

    def __init__(self, store=None):
        super().__init__()
        self.store = store or ProfileStore()
        self.profiles = {}
        self.active = ""
        self.settings = dict(DEFAULT_SETTINGS)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self.save_now)
        self.reset_runtime()

    # ------------------------------------------------------------------
    # Runtime data (never persisted)
    # ------------------------------------------------------------------

    def reset_runtime(self):
        self.nb = None
        self.netbox_status = "unknown"   # unknown | ok | error
        self.netbox_message = "Noch nicht verbunden"
        self.netbox_version = ""
        self.engine = None
        self.vms = []
        self.vms_loaded_at = None
        self.netbox_devices = None
        self.switch_snapshots = {}
        self.switch_status = {}
        # CLI session of failed discoveries (switch id → masked text) for the raw data view.
        self.switch_failed_raw = {}
        self.clients = []
        self.clients_loaded_at = None
        self.client_engine = None
        # Logon from the Windows credential prompt; memory only, per profile.
        self.client_credential = None

    def set_netbox(self, nb=None, status="ok", message="", engine=None):
        if nb is not None:
            self.nb = nb
            self.netbox_version = getattr(nb, "netbox_version", "")
        if engine is not None:
            self.engine = engine
        self.netbox_status = status
        self.netbox_message = message
        if status == "error":
            self.nb = None
            self.engine = None
            self.client_engine = None
        self.netbox_changed.emit()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self):
        try:
            self.profiles, active, self.settings = self.store.load()
        except Exception as error:  # noqa: BLE001
            log.error("Konfiguration konnte nicht geladen werden: %s", error)
            self.profiles, active = {}, ""
        for context in self.store.decrypt_failures:
            log.warning("Gespeichertes Geheimnis nicht lesbar (%s) – bitte neu eingeben.", context)
        if self.profiles:
            self.active = active if active in self.profiles else sorted(self.profiles, key=str.casefold)[0]
        self.profiles_changed.emit()
        self.profile_switched.emit()
        self.settings_changed.emit()

    def schedule_save(self):
        self._save_timer.start()

    def save_now(self):
        self._save_timer.stop()
        try:
            self.store.save(self.profiles, self.active, self.settings)
        except Exception as error:  # noqa: BLE001
            log.error("Speichern fehlgeschlagen: %s", error)
            self.save_failed.emit(str(error))
            return False
        return True

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------

    @property
    def profile(self):
        return self.profiles.get(self.active)

    def profile_names(self):
        return sorted(self.profiles, key=str.casefold)

    def switch_profile(self, name):
        if name == self.active or name not in self.profiles:
            return
        self.save_now()
        self.active = name
        self.reset_runtime()
        self.schedule_save()
        self.profile_switched.emit()
        self.netbox_changed.emit()
        self.vms_changed.emit()
        self.switches_changed.emit()
        self.clients_changed.emit()

    def create_profile(self, name, copy_from=None):
        profile = copy.deepcopy(self.profiles[copy_from]) if copy_from in self.profiles else empty_profile(name)
        profile["name"] = name
        self.profiles[name] = profile
        self.profiles_changed.emit()
        self.active = ""
        self.switch_profile(name)
        self.save_now()

    def rename_profile(self, old, new):
        if old not in self.profiles or new in self.profiles:
            return
        profile = self.profiles.pop(old)
        profile["name"] = new
        self.profiles[new] = profile
        if self.active == old:
            self.active = new
        self.save_now()
        self.profiles_changed.emit()
        self.profile_switched.emit()

    def delete_profile(self, name):
        if name not in self.profiles:
            return
        del self.profiles[name]
        names = self.profile_names()
        self.active = ""
        self.profiles_changed.emit()
        if names:
            self.switch_profile(names[0])
        else:
            self.reset_runtime()
            self.profile_switched.emit()
        self.save_now()

    def edit_profile(self, **fields):
        profile = self.profile
        if profile is None:
            return
        changed = {key: value for key, value in fields.items() if profile.get(key) != value}
        if not changed:
            return
        profile.update(changed)
        if {"netbox_url", "netbox_token", "netbox_ignore_ssl"} & set(changed):
            self.nb = None
            self.engine = None
            self.client_engine = None
            self.netbox_devices = None
            self.netbox_status = "unknown"
            self.netbox_message = "Verbindungsdaten geändert"
            self.netbox_changed.emit()
        self.schedule_save()
        self.profile_edited.emit()

    def touch(self):
        """Call after mutating ``profile['sources']`` or ``profile['switches']`` in place."""
        self.schedule_save()
        self.profile_edited.emit()

    def update_settings(self, **values):
        self.settings.update(values)
        self.schedule_save()
        self.settings_changed.emit()

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @property
    def sources(self):
        return self.profile["sources"] if self.profile else []

    @property
    def switches(self):
        return self.profile["switches"] if self.profile else []

    @property
    def client_options(self):
        return self.profile.setdefault("clients", {}) if self.profile else {}

    def update_client_options(self, **values):
        options = self.client_options
        if self.profile is None or all(options.get(key) == value for key, value in values.items()):
            return
        options.update(values)
        self.schedule_save()

    @property
    def offline(self):
        return bool(self.settings.get("offline_mode"))

    def netbox_configured(self):
        profile = self.profile
        return bool(profile and profile.get("netbox_url") and profile.get("netbox_token"))
