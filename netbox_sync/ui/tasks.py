"""Background tasks and the application-wide activity log."""

import logging
import threading
import time
import traceback
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

SUCCESS = 25
logging.addLevelName(SUCCESS, "SUCCESS")

LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "success": SUCCESS,
          "warning": logging.WARNING, "error": logging.ERROR}
LEVEL_NAMES = {value: key for key, value in LEVELS.items()}

AREAS = {"app": "Allgemein", "netbox": "NetBox", "vm": "VMs", "switch": "Switche", "ipam": "IPAM"}


@dataclass
class LogEntry:
    timestamp: float
    level: str
    area: str
    text: str
    details: str = field(default="")


class LogBus(QObject):
    """Receives log records from any thread and re-emits them in the GUI thread."""

    entry = Signal(object)


class QtLogHandler(logging.Handler):
    def __init__(self, bus):
        super().__init__(level=logging.INFO)
        self.bus = bus

    def emit(self, record):
        if not record.name.startswith("netbox_sync"):
            return
        area = record.name.split(".")[1] if record.name.count(".") >= 1 else "app"
        level = LEVEL_NAMES.get(record.levelno, "info")
        details = ""
        if record.exc_info:
            details = "".join(traceback.format_exception(*record.exc_info))
        self.bus.entry.emit(LogEntry(record.created, level, area if area in AREAS else "app",
                                     record.getMessage(), details))


log_bus = LogBus()


def logger(area):
    return logging.getLogger(f"netbox_sync.{area}")


def area_log(area):
    """Return a ``log(text, level="info")`` callback for the core modules."""
    target = logger(area)

    def write(text, level="info"):
        target.log(LEVELS.get(level, logging.INFO), text)

    return write


class TaskCancelled(Exception):
    pass


class TaskContext:
    def __init__(self, signals, area):
        self._signals = signals
        self._cancel = threading.Event()
        self.area = area
        self.log = area_log(area)

    def progress(self, value, maximum=0, text=""):
        self._signals.progress.emit(int(value), int(maximum), text)

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def check_cancelled(self):
        if self._cancel.is_set():
            raise TaskCancelled()

    def cancel(self):
        self._cancel.set()


class _Signals(QObject):
    progress = Signal(int, int, str)
    succeeded = Signal(object)
    failed = Signal(object)
    finished = Signal()


class Task(QRunnable):
    def __init__(self, function, area, title):
        super().__init__()
        self.setAutoDelete(False)
        self.function = function
        self.title = title
        self.signals = _Signals()
        self.context = TaskContext(self.signals, area)
        self.started_at = None

    def run(self):
        self.started_at = time.monotonic()
        try:
            result = self.function(self.context)
        except TaskCancelled as error:
            self.signals.failed.emit(error)
        except Exception as error:  # noqa: BLE001 - reported to the UI
            logger(self.context.area).debug("Task %s fehlgeschlagen", self.title, exc_info=True)
            error.traceback_text = traceback.format_exc()
            self.signals.failed.emit(error)
        else:
            self.signals.succeeded.emit(result)
        finally:
            self.signals.finished.emit()

    def cancel(self):
        self.context.cancel()


class TaskManager(QObject):
    """Runs one foreground task at a time and exposes busy/progress state."""

    busy_changed = Signal(bool)
    progress = Signal(int, int, str)
    started = Signal(str)

    def __init__(self):
        super().__init__()
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(max(4, self.pool.maxThreadCount()))
        self.current = None
        self._tasks = set()

    @property
    def busy(self):
        return self.current is not None

    def run(self, function, *, title, area="app", on_success=None, on_error=None, on_finished=None,
            on_progress=None, cancellable=True):
        if self.current is not None:
            logger("app").warning("Bitte warten, bis „%s“ abgeschlossen ist.", self.current.title)
            return None
        task = Task(function, area, title)
        task.cancellable = cancellable
        self.current = task
        self._tasks.add(task)

        def finished():
            self._tasks.discard(task)
            if self.current is task:
                self.current = None
                self.busy_changed.emit(False)
            if on_finished:
                on_finished()

        def failed(error):
            if isinstance(error, TaskCancelled):
                logger(area).warning("%s abgebrochen", title)
            elif on_error:
                on_error(error)
            else:
                logger(area).error("%s: %s", title, error)

        task.signals.progress.connect(self.progress)
        if on_progress:
            task.signals.progress.connect(on_progress)
        if on_success:
            task.signals.succeeded.connect(on_success)
        task.signals.failed.connect(failed)
        task.signals.finished.connect(finished)
        self.busy_changed.emit(True)
        self.started.emit(title)
        self.pool.start(task)
        return task

    def cancel(self):
        if self.current is not None:
            self.current.cancel()
