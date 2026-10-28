import pytest

from chia_rs.sized_ints import uint32

from cic.cli.common import load_root_derivation
from cic.drivers.puzzle_root_construction import calculate_puzzle_root

from tests.cli_helpers import make_prefarm_info, secret_key_for_index


def test_load_root_derivation_observer_only_config_reports_observer_error(tmp_path):
    path = tmp_path / "Configuration (abc).txt"
    path.write_bytes(bytes(make_prefarm_info()))

    with pytest.raises(ValueError, match="can only perform observer actions"):
        load_root_derivation(str(path))

    with pytest.raises(ValueError, match="can only perform observer actions"):
        load_root_derivation(str(tmp_path))


def test_load_root_derivation_garbage_reports_unrecognizable_format(tmp_path):
    path = tmp_path / "Configuration (abc).txt"
    path.write_bytes(b"not a config")

    with pytest.raises(ValueError, match="not a recognizable format"):
        load_root_derivation(str(path))


def test_load_root_derivation_accepts_full_derivation(tmp_path):
    prefarm_info = make_prefarm_info()
    pubkeys = [secret_key_for_index(index).get_g1() for index in range(5)]
    derivation = calculate_puzzle_root(
        prefarm_info,
        pubkeys,
        uint32(3),
        uint32(5),
        uint32(1),
    )
    path = tmp_path / "Configuration (abc).txt"
    path.write_bytes(bytes(derivation))

    assert load_root_derivation(str(path)) == derivation
