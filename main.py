"""Start script for development and PyInstaller: ``python main.py``."""

import sys

from netbox_sync.ui.app import main

if __name__ == "__main__":
    sys.exit(main())
