import asyncio
import dataclasses
import os

from blspy import G1Element
from click.testing import CliRunner, Result
from pathlib import Path

from chia.clvm.spend_sim import SpendSim, SimClient
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint32, uint64

from cic.cli.main import cli
from cic.drivers.prefarm_info import PrefarmInfo
from cic.drivers.puzzle_root_construction import RootDerivation, calculate_puzzle_root


def test_help():
    runner = CliRunner()
    result: Result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Commands to control a prefarm singleton" in result.output


def test_init():
    runner = CliRunner()

    prefarm_info = PrefarmInfo(
        bytes32([0] * 32),
        uint64(0),
        uint64(1000000000000),
        uint64(1),
        bytes32([0] * 32),
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

        # Initialize the configuration
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

        with open("./infos/Configuration (needs derivation).txt", "rb") as file:
            assert PrefarmInfo.from_bytes(file.read()) == prefarm_info

        # Derive the private information
        pubkeys = [
            "8b500e920de2f44efbe0ecf3fdf8baa1a980dcf73fd76844d69f300cc843c77340f430b9658c1e8a9f8ca1bb6cf1a31c",
            "b5ae919c39f696cfa34b3fa7bacfc0616a243cf99f9957aa5fcc23a1eebc8572dbad976471fd8f1d4d6eb1eec906261f",
            "ba0015763b9551bcf22f4af4c7769fc059c390336ff9dcbd97cfb5ac6512d692061199942865a481ab0f04acc3db08f9",
            "ad674bf3038a2d71ee3a957e49accfc0992f08734ef1ad2b2ffc1c6f3e34c516a00103b786fec4feb1847773bf3a797e",
            "aa8c0d4d2a47b103de778990735984cea771f34f810d666781c7ddc590eadf355cc986c6a4c089c681fd0d988caf91a3",
        ]
        derivation: RootDerivation = calculate_puzzle_root(
            prefarm_info,
            [G1Element.from_bytes(bytes.fromhex(pk)) for pk in pubkeys],
            uint32(3),
            uint32(5),
            uint32(1),
        )

        result = runner.invoke(
            cli,
            [
                "derive_root",
                "--configuration",
                "./infos/Configuration (needs derivation).txt",
                "--pubkeys",
                ",".join(pubkeys),
                "--initial-lock-level",
                uint32(3),
            ],
        )

        with open("./infos/Configuration (awaiting launch).txt", "rb") as file:
            assert RootDerivation.from_bytes(file.read()) == derivation

        # Launch the singleton using the configuration
        result = runner.invoke(
            cli,
            [
                "launch_singleton",
                "--configuration",
                ".\infos\Configuration (awaiting launch).txt",
                "--fee",
                100,
            ],
        )

        with open(next(Path("./infos/").glob("Configuration (*).txt")), "rb") as file:
            new_derivation = RootDerivation.from_bytes(file.read())
            assert new_derivation.prefarm_info.launcher_id != bytes32([0] * 32)
            assert new_derivation == dataclasses.replace(
                derivation,
                prefarm_info=dataclasses.replace(
                    derivation.prefarm_info, launcher_id=new_derivation.prefarm_info.launcher_id
                ),
            )
            derivation = new_derivation

        # The sim should be initialized now
        async def check_for_launcher():
            try:
                sim = await SpendSim.create(db_path="./sim.db")
                sim_client = SimClient(sim)
                assert (
                    len(await sim_client.get_coin_records_by_parent_ids([new_derivation.prefarm_info.launcher_id])) > 0
                )
            finally:
                await sim.close()

        asyncio.get_event_loop().run_until_complete(check_for_launcher())
