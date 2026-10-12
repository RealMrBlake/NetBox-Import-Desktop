import sys
from pathlib import Path


def resource_path(name):
    """Locate bundled files both from source and inside a PyInstaller build."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    candidate = base / "resources" / name
    if candidate.exists():
        return candidate
    return Path(__file__).resolve().parents[1] / "resources" / name
