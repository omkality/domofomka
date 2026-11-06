from types import SimpleNamespace

import pytest

from ops.disk_reserve import CRITICAL, LOW, REARM, reserve_action, stop_application


@pytest.mark.parametrize(
    ("free", "exists", "expected"),
    [
        (LOW - 1, True, "release_and_clean"),
        (CRITICAL - 1, False, "release_and_clean"),
        (LOW, True, "keep"),
        (REARM - 1, False, "keep"),
        (REARM, False, "rearm"),
        (REARM, True, "keep"),
    ],
)
def test_reserve_uses_hysteresis_and_never_reallocates_under_pressure(free, exists, expected):
    assert reserve_action(free, exists) == expected


def test_emergency_stop_never_targets_vpn_or_unknown_containers(monkeypatch):
    monkeypatch.setattr("ops.disk_reserve.shutil.which", lambda _: "/usr/bin/docker")
    names = "amnezia-awg2\namnezia-socks5proxy\nother-important-service\ndomofomka-dev-queue-1\ndomofomka-vkbot-1\n"
    monkeypatch.setattr("ops.disk_reserve.subprocess.check_output", lambda *a, **k: names)
    called = []
    monkeypatch.setattr(
        "ops.disk_reserve.subprocess.run", lambda args, **kwargs: called.append(args) or SimpleNamespace(returncode=0)
    )
    assert set(stop_application()) == {"domofomka-dev-queue-1", "domofomka-vkbot-1"}
    assert {row[-1] for row in called} == {"domofomka-dev-queue-1", "domofomka-vkbot-1"}


@pytest.mark.skipif(__import__("os").name != "posix", reason="Host guard requires Linux flock")
def test_pressure_releases_reserve_before_stopping_only_application(tmp_path, monkeypatch):
    from ops import disk_reserve

    reserve = tmp_path / "emergency.reserve"
    reserve.write_bytes(b"reserved")
    monkeypatch.setattr(disk_reserve, "HOME", tmp_path)
    samples = iter([SimpleNamespace(free=LOW - 1), SimpleNamespace(free=CRITICAL - 1)])
    monkeypatch.setattr(disk_reserve.shutil, "disk_usage", lambda _: next(samples))
    calls = []
    monkeypatch.setattr(disk_reserve, "safe_cleanup", lambda: calls.append("cleanup"))
    monkeypatch.setattr(disk_reserve, "stop_application", lambda: calls.append("stop_application") or [])
    monkeypatch.setattr(disk_reserve.os, "umask", lambda _: 0)
    disk_reserve.main()
    assert not reserve.exists()
    assert calls == ["cleanup", "stop_application"]
