from __future__ import annotations

from pathlib import Path

from jvvv import utils


def test_windows_drive_root_accepts_drive_letter_paths():
    assert utils._windows_drive_root("E:\\Archive\\Folder") == "E:\\"
    assert utils._windows_drive_root(Path("D:/Media")) == "D:\\"


def test_windows_drive_root_rejects_missing_or_unc_paths():
    assert utils._windows_drive_root(None) is None
    assert utils._windows_drive_root("") is None
    assert utils._windows_drive_root("\\\\server\\share\\Archive") is None


def test_eject_volume_supported_requires_windows(monkeypatch):
    monkeypatch.setattr(utils.platform, "system", lambda: "Linux")

    assert not utils.eject_volume_supported("E:\\Archive")


def test_eject_volume_supported_rejects_system_drive(monkeypatch):
    monkeypatch.setattr(utils.platform, "system", lambda: "Windows")
    monkeypatch.setenv("SystemDrive", "C:")

    assert not utils.eject_volume_supported("C:\\Users")
    assert utils.eject_volume_supported("E:\\Archive")
