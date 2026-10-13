import pytest
from phonelab.backup import preserve, BackupError, flash_capacity


class ReadOnlyROM:
    CHIP_NAME = 'ESP32'
    def __init__(self, mismatch=False, encrypted=False):
        self.reads = 0
        self.mismatch = mismatch
        self.encrypted = encrypted
    def get_flash_encryption_enabled(self): return self.encrypted
    def get_secure_boot_enabled(self): return False
    def get_encrypted_download_disabled(self): return False
    def flash_id(self): return 0x1240EF  # JEDEC capacity 2**18
    def read_flash(self, address, size):
        assert address == 0 and size == 262144
        self.reads += 1
        return bytes([self.reads if self.mismatch else 7]) * size


def test_two_independent_reads_immutable_baseline(tmp_path):
    r = ReadOnlyROM()
    result = preserve(r, tmp_path, {'identity_confirmed': True})
    assert result['state'] == 'DOUBLE_READ_MATCH'
    assert r.reads == 2
    assert (tmp_path / 'read-1.bin').read_bytes() == (tmp_path / 'read-2.bin').read_bytes()
    with pytest.raises(BackupError): preserve(r, tmp_path, {'identity_confirmed': True})
    assert r.reads == 2


def test_mismatch_retains_both_originals_and_records_failure(tmp_path):
    with pytest.raises(BackupError): preserve(ReadOnlyROM(mismatch=True), tmp_path, {'identity_confirmed': True})
    assert (tmp_path / 'read-1.bin').exists()
    assert (tmp_path / 'read-2.bin').exists()
    assert 'MISMATCH' in (tmp_path / 'manifest.json').read_text()


def test_encryption_or_unconfirmed_identity_blocks_reads(tmp_path):
    for r, confirmed in [(ReadOnlyROM(encrypted=True), True), (ReadOnlyROM(), False)]:
        with pytest.raises(BackupError): preserve(r, tmp_path, {'identity_confirmed': confirmed})
        assert r.reads == 0


@pytest.mark.parametrize('jedec', [0, 0xffffff, 0x1040ef, 0x2240ef])
def test_unknown_capacity_is_not_guessed(jedec):
    with pytest.raises(BackupError): flash_capacity(jedec)


def test_short_read_cannot_pass(tmp_path):
    r = ReadOnlyROM()
    r.read_flash = lambda a, n: bytes(n - 1)
    with pytest.raises(BackupError): preserve(r, tmp_path, {'identity_confirmed': True})


def test_restore_preview_rejects_single_read_manifest(tmp_path):
    import json
    from phonelab.backup import restore_preview
    preserve(ReadOnlyROM(), tmp_path, {'identity_confirmed': True})
    p = tmp_path/'manifest.json'
    data = json.loads(p.read_text())
    data['files'] = data['files'][:1]
    p.write_text(json.dumps(data))
    with pytest.raises(BackupError): restore_preview(p)
