import hashlib
import io
import json

import pytest

from tools.fetch_dev_backup import receive


def payload(data=b"PGDMP-valid-data", **overrides: object):
    header = {
        "name": "queue-20261106T123804Z-1055854.dump",
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        **overrides,
    }
    return json.dumps(header).encode() + b"\n" + data


def test_verified_backup_is_published_atomically(tmp_path):
    result = receive(io.BytesIO(payload()), tmp_path)
    assert result["verified"]
    assert (tmp_path / result["name"]).read_bytes() == b"PGDMP-valid-data"
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize(
    "data",
    [
        payload(name="../../secret.dump"),
        payload(bytes=999999999),
        payload(sha256="0" * 64),
        payload(b"invalid-dump"),
        payload()[:-3],
    ],
)
def test_corrupt_or_unsafe_backup_is_not_published(tmp_path, data):
    with pytest.raises(ValueError, match="Invalid|integrity|PostgreSQL|Truncated"):
        receive(io.BytesIO(data), tmp_path)
    assert not list(tmp_path.glob("*.dump"))
    assert not list(tmp_path.glob("*.part"))
