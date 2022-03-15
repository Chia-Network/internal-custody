import os

from click.testing import CliRunner, Result

from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint64

from cic.cli.main import cli
from cic.drivers.prefarm_info import PrefarmInfo


def test_help():
    runner = CliRunner()
    result: Result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Commands to control a prefarm singleton" in result.output

def test_init():
    runner = CliRunner()

    prefarm_info = PrefarmInfo(
        bytes32([0]*32),
        uint64(0),
        uint64(1000000000000),
        uint64(1),
        bytes32([0]*32),
        uint64(30),
        uint64(90),
        uint64(30),
        uint64(45),
        uint64(15),
    )

    with runner.isolated_filesystem():
        os.mkdir("infos")
        result: Result = runner.invoke(cli, ["init"])
        assert result.exit_code != 0
        result = runner.invoke(
            cli,
            [
                "init",
                "--filepath",
                "./infos/",
                "--date",
                prefarm_info.start_date,
                "--rate",
                prefarm_info.mojos_per_second,
                "--amount",
                prefarm_info.starting_amount,  # 1 XCH
                "--withdrawal-timelock",
                prefarm_info.withdrawal_timelock,
                "--rekey-timelock",
                prefarm_info.rekey_increments,
                "--slow-penalty",
                prefarm_info.slow_rekey_timelock,
                "--payment-clawback",
                prefarm_info.payment_clawback_period,
                "--rekey-cancel",
                prefarm_info.rekey_clawback_period,
            ],
        )

        with open("./infos/Public Info (needs derivation).txt", "rb") as file:
            assert PrefarmInfo.from_bytes(file.read()) == prefarm_info
