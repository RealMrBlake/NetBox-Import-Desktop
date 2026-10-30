import sys

import pytest

from netbox_sync.core import updater

RELEASE = {
    "tag_name": "v4.7.0", "name": "Netbox-Import-Desktop v4.7.0", "body": "- neu", "draft": False,
    "prerelease": False, "html_url": "https://example.invalid/releases/tag/v4.7.0",
    "assets": [
        {"name": "NetBox VM Import.exe", "size": 4, "browser_download_url": "https://example.invalid/portable"},
        {"name": "NetBoxSynchronizer-Setup-4.7.0.exe", "size": 4, "browser_download_url": "https://example.invalid/setup"},
    ],
}


class _Response:
    def __init__(self, payload=None, chunks=(), length=None):
        self.payload, self.chunks = payload, chunks
        self.headers = {"Content-Length": str(length)} if length else {}

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload

    def iter_content(self, _size):
        return iter(self.chunks)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_versions_are_compared_numerically():
    assert updater.parse_version("v4.10.0") > updater.parse_version("4.9.3")
    assert updater.parse_version("v5") == (5, 0, 0)


def test_newer_release_is_offered_with_matching_assets(monkeypatch):
    monkeypatch.setattr(updater.requests, "get", lambda *_a, **_k: _Response(RELEASE))
    release = updater.check_for_update("4.6.0")
    assert release["version"] == "4.7.0" and release["notes"] == "- neu"
    assert updater.pick_asset(release, "portable")[0] == "NetBox VM Import.exe"
    assert updater.pick_asset(release, "installer")[0] == "NetBoxSynchronizer-Setup-4.7.0.exe"
    assert updater.check_for_update("4.7.0") is None
    monkeypatch.setattr(updater.requests, "get", lambda *_a, **_k: _Response(dict(RELEASE, prerelease=True)))
    assert updater.check_for_update("4.6.0") is None


def test_incomplete_download_is_discarded(monkeypatch, tmp_path):
    monkeypatch.setattr(updater.requests, "get", lambda *_a, **_k: _Response(chunks=[b"ab"]))
    with pytest.raises(updater.UpdateError):
        updater.download({"url": "x", "size": 4}, tmp_path / "setup.exe")
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setattr(updater.requests, "get", lambda *_a, **_k: _Response(chunks=[b"ab", b"cd"]))
    target = updater.download({"url": "x", "size": 4}, tmp_path / "setup.exe")
    assert target.read_bytes() == b"abcd"


def test_portable_exe_is_swapped_and_restarted(monkeypatch, tmp_path):
    exe = tmp_path / "NetBox VM Import.exe"
    exe.write_bytes(b"old")
    new = tmp_path / "NetBox VM Import.new.exe"
    new.write_bytes(b"new")
    started = []
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(updater.subprocess, "Popen", lambda args, **_kwargs: started.append(args))
    updater.apply_portable(new)
    assert exe.read_bytes() == b"new" and not new.exists()
    assert (tmp_path / "NetBox VM Import.old.exe").read_bytes() == b"old"
    assert started == [[str(exe)]]
