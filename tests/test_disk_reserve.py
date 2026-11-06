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
