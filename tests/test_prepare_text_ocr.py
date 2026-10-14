import pytest
from scripts.prepare_text_ocr import prepare_data


def test_preview_has_no_download_or_filesystem_side_effect(tmp_path):
    root=tmp_path/'new-data'
    result=prepare_data(root,fetch=lambda _:pytest.fail('unexpected network'))
    assert result['state']=='PREVIEW_ONLY' and not root.exists()


def test_mismatched_existing_or_downloaded_data_is_never_installed(tmp_path):
    root=tmp_path/'existing';target=root/'tooling/local-ocr/tessdata/eng.traineddata'
    target.parent.mkdir(parents=True);target.write_bytes(b'preserve mismatched file')
    with pytest.raises(ValueError):prepare_data(root,download=True)
    assert target.read_bytes()==b'preserve mismatched file'
    new=tmp_path/'new'
    with pytest.raises(ValueError):prepare_data(new,download=True,fetch=lambda _:b'wrong download')
    assert not new.exists()
