"""Проверки границ CI и отказа активации без доступа к настоящим серверам."""

import io
import json
import subprocess
import tarfile
from types import SimpleNamespace

import pytest

from ops.deploy import Deployment, validate_manifest
from ops.receive_release import unpack


def manifest():
    return {
        "environment": "dev",
        "revision": "a" * 40,
        "api": "ghcr.io/omkality/domofomka-api@sha256:" + "b" * 64,
        "bot": "ghcr.io/omkality/domofomka-vkbot@sha256:" + "c" * 64,
        "api_bytes": 253618865,
        "bot_bytes": 202648708,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("environment", "prod"),
        ("revision", "dev"),
        ("api", "ghcr.io/omkality/domofomka-api:latest"),
        ("bot", "ghcr.io/other/bot@sha256:" + "c" * 64),
    ],
)
def test_release_rejects_wrong_target_mutable_images_and_foreign_registry(field, value):
    release = manifest()
    release[field] = value
    with pytest.raises(ValueError, match="Invalid|Immutable|Unexpected|Only small"):
        validate_manifest(release, "dev")


@pytest.mark.parametrize("bad", ["../release.json", "/release.json", "symlink"])
def test_receiver_rejects_archive_paths_and_symlinks(tmp_path, bad):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as archive:
        for name in ("compose.deploy.yml", "ops/deploy.py", "release.json"):
            item = tarfile.TarInfo(bad if name == "release.json" and bad != "symlink" else name)
            if name == "release.json" and bad == "symlink":
                item.type, item.linkname = tarfile.SYMTYPE, "/etc/shadow"
            archive.addfile(item)
    with pytest.raises(ValueError, match="Invalid|Immutable|Unexpected|Only small"):
        unpack(buf.getvalue(), tmp_path)
    assert not (tmp_path / "release.json").exists()


def prepare(tmp_path):
    (tmp_path / ".env.dev").write_text("VK_GROUP_ID=223949627\nEXPECTED_VK_GROUP_ID=223949627\n")
    home = tmp_path / ".ci"
    home.mkdir()
    (home / "releases").mkdir()
    old, new = home / "releases/old", home / "releases/new"
    old.mkdir()
    new.mkdir()
    (new / "release.json").write_text(json.dumps(manifest()))
    state = {"current": str(old), "previous": None}
    (home / "state.json").write_text(json.dumps(state))
    return Deployment(tmp_path, "dev"), old, new, state


def test_failed_activation_restores_old_images_and_keeps_release_state(tmp_path, monkeypatch):
    deployment, old, new, state = prepare(tmp_path)
    monkeypatch.setattr(deployment, "verify_environment", lambda: None)
    monkeypatch.setattr(deployment, "free_space", lambda *a: None)
    monkeypatch.setattr("ops.deploy.run", lambda *a, **k: SimpleNamespace(stdout="a" * 40 + "\n"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    calls = []

    def activate(path):
        calls.append(path)
        if path == new:
            raise RuntimeError("health probe failed")

    monkeypatch.setattr(deployment, "activate", activate)
    with pytest.raises(RuntimeError, match="health probe failed"):
        deployment.deploy(new)
    assert calls == [new, old]
    assert json.loads(deployment.state_path.read_text()) == state


def test_disk_preflight_failure_does_not_stop_current_bot(tmp_path, monkeypatch):
    deployment, old, new, state = prepare(tmp_path)
    monkeypatch.setattr(deployment, "verify_environment", lambda: None)
    monkeypatch.setattr("ops.deploy.shutil.disk_usage", lambda _: SimpleNamespace(free=1))
    monkeypatch.setattr(deployment, "activate", lambda _: pytest.fail("Must not stop bot"))
    with pytest.raises(RuntimeError, match="Insufficient free disk"):
        deployment.deploy(new)
    assert json.loads(deployment.state_path.read_text()) == state


def test_compose_uses_pinned_images_and_preserves_host_env(tmp_path, monkeypatch):
    deployment, _, new, _ = prepare(tmp_path)
    observed = {}

    def capture(args, **kwargs: object):
        observed.update(args=args, **kwargs)

    monkeypatch.setattr("ops.deploy.run", capture)
    deployment.compose(new, "up", "-d", "--no-build", "--no-deps", "api")
    assert observed["env"]["API_IMAGE"] == manifest()["api"]
    assert observed["env"]["DEPLOY_GROUP_ID"] == "223949627"
    assert observed["env"]["DEPLOY_ENV_FILE"] == str(tmp_path / ".env.dev")
    assert "down" not in observed["args"]
    assert "queue" not in observed["args"]


def test_failed_manual_rollback_restores_current_release(tmp_path, monkeypatch):
    deployment, old, new, _ = prepare(tmp_path)
    state = {"current": str(new), "previous": str(old)}
    deployment.state_path.write_text(json.dumps(state))
    monkeypatch.setattr(deployment, "verify_environment", lambda: None)
    calls = []

    def activate(path):
        calls.append(path)
        if path == old:
            raise RuntimeError("old version failed")

    monkeypatch.setattr(deployment, "activate", activate)
    with pytest.raises(RuntimeError):
        deployment.rollback()
    assert calls == [old, new]
    assert json.loads(deployment.state_path.read_text()) == state
