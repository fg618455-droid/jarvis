"""Invalid acceptance inputs must fail before any application state is written."""
import pytest

from scripts.live_tool_matrix import validate_inputs


@pytest.mark.parametrize('content', [b'\xef\xbb\xbf{}', b'{broken', b'[]', b'\xff\xfe{}'])
def test_invalid_config_does_not_write_anything(tmp_path, content):
    config = tmp_path / 'config.json'
    config.write_bytes(content)
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(ValueError):
        validate_inputs(str(config), str(tmp_path))
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_missing_explicit_config_cannot_fall_back(tmp_path):
    with pytest.raises(ValueError, match='explicit'):
        validate_inputs('config.json', str(tmp_path))
    assert list(tmp_path.iterdir()) == []
