"""Декларативная политика хранения без подмены VPN и генератора конфигов."""

import json

import pytest

from ops.host_storage import configuration_files
from tests.paths import ROOT


def test_native_configuration_files_exist_and_do_not_modify_vpn_or_ssh():
    entries = configuration_files(ROOT / "ops/config")
    assert len(entries) == 12
    assert all(not any(word in item["target"] for word in ["amnezia", "sshd", "docker/daemon"]) for item in entries)
    logrotate = ROOT / "ops/config/logrotate"
    assert "copytruncate" in (logrotate / "tinyproxy").read_text()
    assert "create 0660 root utmp" in (logrotate / "btmp").read_text()


def test_configuration_manifest_rejects_a_source_outside_ops(tmp_path):
    config = tmp_path / "ops/config"
    config.mkdir(parents=True)
    (config / "manifest.json").write_text(json.dumps([{"source": "../../../outside", "target": "/etc/test"}]))
    with pytest.raises(ValueError, match="(subpath|relative)"):
        configuration_files(config)
