import hashlib

import pytest

from tools.archive_catalog_backup import archive


def test_catalog_backup_checks_content_and_retains_two_versions(tmp_path):
    source = tmp_path / "input.dump"
    output = tmp_path / "backups"
    for index in range(3):
        data = b"PGDMP" + bytes([index]) * 100
        source.write_bytes(data)
        archive(source, output, hashlib.sha256(data).hexdigest())
    assert len(list(output.glob("*.dump"))) == 2


def test_catalog_backup_does_not_accept_corrupted_existing_copy(tmp_path):
    source = tmp_path / "input.dump"
    source.write_bytes(b"PGDMP-verified")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / "backups"
    archive(source, output, digest)
    (output / ("catalog-" + digest + ".dump")).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="corrupt"):
        archive(source, output, digest)
