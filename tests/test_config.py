import pytest

from vinyl.config import load_config

MINIMAL = """
[spotify]
client_id = "abc"
[reader]
[web]
[paths]
"""


def test_tap_while_playing_defaults_to_play(tmp_path):
    (tmp_path / "config.toml").write_text(MINIMAL)
    cfg = load_config(tmp_path)
    assert cfg.tap_while_playing == "play"
    assert cfg.db_path == tmp_path / "records.db"


def test_tap_while_playing_queue(tmp_path):
    (tmp_path / "config.toml").write_text(MINIMAL + '[player]\ntap_while_playing = "Queue"\n')
    assert load_config(tmp_path).tap_while_playing == "queue"


def test_tap_while_playing_rejects_nonsense(tmp_path):
    (tmp_path / "config.toml").write_text(MINIMAL + '[player]\ntap_while_playing = "replace"\n')
    with pytest.raises(ValueError):
        load_config(tmp_path)
