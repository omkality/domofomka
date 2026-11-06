"""Проверки границ CI и отказа активации без доступа к настоящим серверам."""

import hashlib
import io
import json
import runpy
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops.deploy import Deployment, validate_manifest
from ops.receive_release import unpack, verify_source
from tools.ci_image_refs import published_image


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


def test_ci_release_cannot_select_legacy_runtime():
    with pytest.raises(ValueError, match="administrator-frozen"):
        validate_manifest(dict(manifest(), runtime="legacy-v1"), "dev")


def test_legacy_snapshot_preserves_original_code_and_env_and_rejects_drift(tmp_path):
    helpers = runpy.run_path(str(Path(__file__).parents[1] / "ops/bootstrap-ci.py"))
    root, bundle, baseline = tmp_path / "prod", tmp_path / "bundle", tmp_path / "baseline"
    (root / "src").mkdir(parents=True)
    (bundle / "ops").mkdir(parents=True)
    baseline.mkdir()
    source = root / "src/main.py"
    source.write_text("old source")
    (root / ".env").write_text("ORIGINAL_SETTING=preserved\n")
    facts = {
        "revision": "a" * 40,
        "version": "1.5.6",
        "files": {"src/main.py": hashlib.sha256(source.read_bytes()).hexdigest()},
    }
    (bundle / "ops/legacy-v1-baseline.json").write_text(json.dumps(facts))
    (bundle / "ops/compose.legacy-v1.yml").write_text("services: {}\n")
    assert helpers["legacy_manifest"](root, bundle) == facts
    helpers["freeze_legacy"](root, baseline, bundle, facts)
    source.write_text("later source")
    (root / ".env").write_text("NEW_SETTING=changed\n")
    assert (baseline / "legacy-snapshot/src/main.py").read_text() == "old source"
    assert (baseline / "legacy-snapshot/.env").read_text() == "ORIGINAL_SETTING=preserved\n"
    with pytest.raises(ValueError, match="differs from the published"):
        helpers["legacy_manifest"](root, bundle)


def test_legacy_snapshot_rejects_path_escape(tmp_path):
    verify = runpy.run_path(str(Path(__file__).parents[1] / "ops/bootstrap-ci.py"))["legacy_manifest"]
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops/legacy-v1-baseline.json").write_text(
        json.dumps({"revision": "a" * 40, "version": "1.5.6", "files": {"../outside": "b" * 64}})
    )
    with pytest.raises(ValueError, match="path or checksum"):
        verify(tmp_path, tmp_path)


def test_legacy_compose_restores_frozen_inputs_without_restarting_storage(tmp_path, monkeypatch):
    deployment, _, release, _ = prepare(tmp_path)
    (release / "release.json").write_text(json.dumps(dict(manifest(), runtime="legacy-v1")))
    observed = {}
    monkeypatch.setattr("ops.deploy.run", lambda args, **kwargs: observed.update(args=args, **kwargs))
    deployment.compose(release, "up", "-d", "--no-deps", "api", "vkbot")
    assert observed["env"]["DEPLOY_ENV_FILE"] == str(release / "legacy-snapshot/.env")
    assert str(release / "compose.legacy-v1.yml") in observed["args"]
    assert "queue" not in observed["args"]
    assert "redis" not in observed["args"]


def test_legacy_readiness_uses_v1_dependencies_without_starting_another_collector(tmp_path, monkeypatch):
    deployment, _, release, _ = prepare(tmp_path)
    (release / "release.json").write_text(json.dumps(dict(manifest(), runtime="legacy-v1")))
    probes = []
    monkeypatch.setattr(deployment, "compose", lambda release, *args, **kwargs: probes.append(args[-1]))
    deployment.check(release)
    assert "src.api.v2" not in probes[0]
    assert "'1.5.6'" in probes[0]
    assert "src.vkbot.runtime" not in probes[1]
    assert "groups.getLongPollServer" in probes[1]
    assert "await client.ping()" in probes[1]
    assert ".listen(" not in probes[1]
    assert "pg_try_advisory_lock" not in probes[1]


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
    (old / "release.json").write_text(json.dumps(manifest()))
    (new / "release.json").write_text(json.dumps(manifest()))
    state = {"current": str(old), "previous": None}
    (home / "state.json").write_text(json.dumps(state))
    return Deployment(tmp_path, "dev"), old, new, state


@pytest.mark.parametrize("action", ["deploy", "rollback"])
def test_legacy_retry_with_unfinished_v2_work_keeps_current_bot_and_state(tmp_path, monkeypatch, action):
    deployment, old, new, state = prepare(tmp_path)
    (old / "release.json").write_text(json.dumps(dict(manifest(), runtime="legacy-v1")))
    if action == "rollback":
        state["previous"] = str(new)
        deployment.state_path.write_text(json.dumps(state))
    monkeypatch.setattr(deployment, "verify_environment", lambda: None)
    responses = iter(["t", "1"])
    commands = []

    def read_queue(args, **kwargs: object):
        commands.append(args)
        return SimpleNamespace(stdout=next(responses))

    monkeypatch.setattr("ops.deploy.run", read_queue)
    monkeypatch.setattr(deployment, "activate", lambda path: pytest.fail("Current legacy bot must keep running"))
    with pytest.raises(RuntimeError, match="reconcile deliveries"):
        deployment.deploy(new) if action == "deploy" else deployment.rollback()
    assert len(commands) == 2
    assert all("psql" in cmd for cmd in commands)
    assert json.loads(deployment.state_path.read_text()) == state
    assert not (deployment.home / "transaction.json").exists()


@pytest.mark.parametrize("responses", [["f"], ["t", "0"]])
def test_initial_empty_queue_allows_first_transition_from_v1(tmp_path, monkeypatch, responses):
    deployment, old, _, _ = prepare(tmp_path)
    (old / "release.json").write_text(json.dumps(dict(manifest(), runtime="legacy-v1")))
    values = iter(responses)
    monkeypatch.setattr("ops.deploy.run", lambda *args, **kwargs: SimpleNamespace(stdout=next(values)))
    deployment.verify_legacy_retry(old)


@pytest.mark.parametrize("target", ["dev", "prod"])
def test_explicit_legacy_rollback_selects_frozen_baseline_only_on_prod(tmp_path, monkeypatch, target):
    deployment, old, current, state = prepare(tmp_path)
    deployment.target = target
    baseline = deployment.home / "releases/baseline"
    baseline.mkdir()
    (baseline / "release.json").write_text(json.dumps(dict(manifest(), environment="prod", runtime="legacy-v1")))
    state.update(current=str(current), previous=str(old))
    deployment.state_path.write_text(json.dumps(state))
    monkeypatch.setattr(deployment, "verify_environment", lambda: None)
    activated = []
    monkeypatch.setattr(deployment, "activate", activated.append)
    if target == "dev":
        with pytest.raises(ValueError, match="only allowed on prod"):
            deployment.rollback(True)
        assert not activated
    else:
        deployment.rollback(True)
        assert activated == [baseline]
        saved = json.loads(deployment.state_path.read_text())
        assert saved["current"] == str(baseline)
        assert saved["previous"] == str(current)


def test_later_v2_deploy_cleanup_keeps_frozen_legacy_inputs(tmp_path):
    deployment, _, _, _ = prepare(tmp_path)
    baseline = deployment.home / "releases/baseline"
    baseline.mkdir()
    (baseline / "release.json").write_text(json.dumps(dict(manifest(), runtime="legacy-v1")))
    (baseline / "saved-config").write_text("preserved")
    deployment.cleanup()
    assert (baseline / "saved-config").read_text() == "preserved"


def test_receiver_scopes_legacy_rollback_to_prod_and_forwards_the_flag(tmp_path, monkeypatch):
    _, _, _, _ = prepare(tmp_path)
    receiver = runpy.run_path(str(Path(__file__).parents[1] / "ops/receive_release.py"))["main"]
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "rollback-legacy")
    monkeypatch.setattr("sys.argv", ["receiver", "--root", str(tmp_path), "--environment", "prod"])
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: calls.append(args))
    receiver()
    assert calls[0][-1] == "--legacy-v1"
    monkeypatch.setattr("sys.argv", ["receiver", "--root", str(tmp_path), "--environment", "dev"])
    with pytest.raises(ValueError, match="only allowed on prod"):
        receiver()
    assert len(calls) == 1


def test_legacy_rollback_sender_needs_no_registry_token_or_release_payload(monkeypatch):
    sender = runpy.run_path(str(Path(__file__).parents[1] / "tools/ci_send.py"))["main"]
    for key, value in {
        "DEPLOY_ACTION": "rollback-legacy",
        "DEPLOY_KEY_FILE": "unused-key",
        "DEPLOY_KNOWN_HOSTS_FILE": "unused-hosts",
        "DEPLOY_HOST": "example.invalid",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GHCR_TOKEN", raising=False)
    observed = {}
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: observed.update(args=args, **kwargs))
    sender()
    assert observed["args"][-1] == "rollback-legacy"
    assert "input" not in observed


def test_failed_activation_restores_old_images_and_keeps_release_state(tmp_path, monkeypatch):
    deployment, old, new, state = prepare(tmp_path)
    monkeypatch.setattr(deployment, "verify_environment", lambda: None)
    monkeypatch.setattr(deployment, "free_space", lambda *a: None)
    monkeypatch.setattr(deployment, "catalog_change", lambda _: None)
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


def test_catalog_is_restored_before_old_app_when_new_health_fails(tmp_path, monkeypatch):
    deployment, old, new, state = prepare(tmp_path)
    monkeypatch.setattr(deployment, "verify_environment", lambda: None)
    monkeypatch.setattr(deployment, "free_space", lambda *a: None)
    monkeypatch.setattr("ops.deploy.run", lambda *a, **k: SimpleNamespace(stdout="a" * 40 + "\n"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(deployment, "catalog_change", lambda _: {"reviewed": True})
    calls = []
    monkeypatch.setattr(
        deployment, "run_catalog", lambda info, reverse=False: calls.append("reverse" if reverse else "forward")
    )

    def activate(path):
        calls.append(path)
        if path == new:
            raise RuntimeError("new health failed")

    monkeypatch.setattr(deployment, "activate", activate)
    with pytest.raises(RuntimeError, match="new health failed"):
        deployment.deploy(new)
    assert calls == ["forward", new, "reverse", old]
    assert json.loads(deployment.state_path.read_text()) == state


def test_bootstrap_cache_cleanup_handles_root_and_ignores_shared_or_foreign_steps(monkeypatch):
    function = runpy.run_path(str(Path(__file__).parents[1] / "ops/bootstrap-ci.py"))["retire_build_cache"]
    records = [
        {"ID": "base", "Parents": None, "Reclaimable": True, "Shared": False, "Description": "python base"},
        {
            "ID": "old",
            "Parents": ["base"],
            "Reclaimable": True,
            "Shared": False,
            "Description": "mount / from exec /bin/sh -c uv sync --frozen --group api",
        },
        {"ID": "vpn", "Parents": None, "Reclaimable": True, "Shared": False, "Description": "amnezia"},
        {"ID": "active", "Parents": ["base"], "Reclaimable": True, "Shared": True, "Description": "COPY src/ ./src/"},
    ]
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: "\n".join(map(json.dumps, records)))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: calls.append(args))
    function()
    assert calls == [["docker", "buildx", "prune", "--filter", "id=old", "--force"]]


def test_ci_uses_registry_digest_and_expanded_filesystem_size(monkeypatch):
    calls = []

    def command(args, **kwargs: object):
        calls.append(args)
        return json.dumps({"digest": "sha256:" + "b" * 64}) if "imagetools" in args else "253618865\t/\n"

    monkeypatch.setattr(subprocess, "check_output", command)
    repository = "ghcr.io/omkality/domofomka-api"
    image, size = published_image(repository, "a" * 40)
    assert image == repository + "@sha256:" + "b" * 64
    assert size == 253618865
    assert calls[0][4] == repository + ":sha-" + "a" * 40
    assert "--network" in calls[1]


@pytest.mark.parametrize("forged", [True, False])
def test_receiver_executes_only_code_from_the_current_published_branch(tmp_path, monkeypatch, forged):
    (tmp_path / "release.json").write_text(json.dumps(manifest()))
    (tmp_path / "compose.deploy.yml").write_bytes(b"published-compose")
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops/deploy.py").write_bytes(b"arbitrary-root-code" if forged else b"published-deployer")

    def read(url):
        if "/git/ref/" in url:
            return json.dumps({"object": {"sha": "a" * 40}}).encode()
        return b"published-compose" if url.endswith(".yml") else b"published-deployer"

    monkeypatch.setattr("ops.receive_release.read_url", read)
    if forged:
        with pytest.raises(ValueError, match="differs from the published commit"):
            verify_source(tmp_path, "dev")
    else:
        verify_source(tmp_path, "dev")


@pytest.mark.parametrize("committed", [False, True])
def test_recovery_respects_state_commit_before_journal_cleanup(tmp_path, monkeypatch, committed):
    deployment, old, new, state = prepare(tmp_path)
    (old / "release.json").write_text(json.dumps(manifest()))
    if committed:
        state["current"] = str(new)
        deployment.state_path.write_text(json.dumps(state))
    journal = deployment.home / "transaction.json"
    journal.write_text(
        json.dumps(
            {
                "previous": str(old),
                "candidate": str(new),
                "catalog": {"reviewed": True},
                "catalog_restore_reverse": True,
            }
        )
    )
    calls = []
    monkeypatch.setattr(deployment, "run_catalog", lambda info, reverse=False: calls.append(("catalog", reverse)))
    monkeypatch.setattr(deployment, "activate", lambda release: calls.append(release))
    deployment.recover_pending()
    assert calls == ([new] if committed else [("catalog", True), old])
    assert not journal.exists()
    assert (tmp_path / ".deployed-revision").read_text().strip() == manifest()["revision"]


@pytest.mark.parametrize("direction", ["forward", "reverse"])
def test_manual_rollback_switches_catalog_and_images_together(tmp_path, monkeypatch, direction):
    deployment, old, new, _ = prepare(tmp_path)
    (old / "release.json").write_text(json.dumps(manifest()))
    state = {
        "current": str(new),
        "previous": str(old),
        "catalog_transition": {"reviewed": True},
        "catalog_direction": direction,
    }
    deployment.state_path.write_text(json.dumps(state))
    monkeypatch.setattr(deployment, "verify_environment", lambda: None)
    calls = []
    monkeypatch.setattr(deployment, "run_catalog", lambda info, reverse=False: calls.append(("catalog", reverse)))
    monkeypatch.setattr(deployment, "activate", lambda release: calls.append(release))
    deployment.rollback()
    assert calls == [("catalog", direction == "forward"), old]
    restored = json.loads(deployment.state_path.read_text())
    assert restored["current"] == str(old)
    assert restored["catalog_direction"] != direction
