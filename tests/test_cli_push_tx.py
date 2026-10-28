import asyncio
import re

from chia_rs import G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64
from click.testing import CliRunner
from pathlib import Path
from typing import List

from chia._tests.util.spend_sim import SpendSim, SimClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend

from cic.cli.main import _find_fee_announcement, cli

ACS: Program = Program.to(1)
ACS_PH: bytes32 = ACS.get_tree_hash()


async def _setup_sim_coin() -> Coin:
    async with SpendSim.managed(Path("./sim.db")) as sim:
        await sim.farm_block(ACS_PH)
        sim_client = SimClient(sim)
        coin_records = await sim_client.get_coin_records_by_puzzle_hashes([ACS_PH], include_spent_coins=False)
        return sorted((record.coin for record in coin_records), key=lambda coin: coin.name())[0]


def test_find_fee_announcement_uses_last_match():
    first_coin = Coin(bytes32([1] * 32), ACS_PH, uint64(10))
    second_coin = Coin(bytes32([2] * 32), ACS_PH, uint64(10))
    bundle = SpendBundle(
        [
            make_spend(first_coin, ACS, Program.to([[60, b"first"], [51, ACS_PH, 10]])),
            make_spend(second_coin, ACS, Program.to([[60, b"second"], [51, ACS_PH, 10]])),
        ],
        G2Element(),
    )

    announcement = _find_fee_announcement(bundle)

    assert announcement is not None
    assert announcement.asserted_id == second_coin.name()
    assert announcement.asserted_msg == b"second"


def test_push_tx_two_step_confirmation_timeout(monkeypatch):
    from tests.cli_clients import FullNodeClientMock

    runner = CliRunner()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        with runner.isolated_filesystem():
            coin = loop.run_until_complete(_setup_sim_coin())

            child_coin = Coin(coin.name(), ACS_PH, coin.amount)
            first_spend = make_spend(coin, ACS, Program.to([[51, ACS_PH, coin.amount]]))
            second_spend = make_spend(
                child_coin,
                ACS,
                Program.to([[80, 0], [51, ACS_PH, child_coin.amount]]),
            )
            two_step_bundle = SpendBundle([first_spend, second_spend], G2Element())

            original_get_coin_record_by_name = FullNodeClientMock.get_coin_record_by_name

            async def no_coin_record(self, coin_id):
                return None

            monkeypatch.setattr(FullNodeClientMock, "get_coin_record_by_name", no_coin_record)

            result = runner.invoke(
                cli,
                [
                    "push_tx",
                    "--spend-bundle",
                    bytes(two_step_bundle).hex(),
                    "--confirmation-timeout",
                    "0",
                    "--poll-interval",
                    "0.01",
                ],
            )
            assert result.exit_code != 0
            assert "not seen confirmed" in result.output
            match = re.search(r"Step 2 spend bundle \(hex\): ([0-9a-f]+)", result.output)
            assert match is not None
            second_hex = match.group(1)

            monkeypatch.setattr(FullNodeClientMock, "get_coin_record_by_name", original_get_coin_record_by_name)

            result = runner.invoke(
                cli,
                [
                    "push_tx",
                    "--spend-bundle",
                    second_hex,
                ],
            )
            assert result.exit_code == 0
            assert r"{'success': True, 'status': 'SUCCESS'}" in result.output

            async def check_state() -> None:
                async with SpendSim.managed(Path("./sim.db")) as sim:
                    sim_client = SimClient(sim)
                    child_record = await sim_client.get_coin_record_by_name(child_coin.name())
                    assert child_record is not None
                    assert child_record.spent
                    grandchildren = await sim_client.get_coin_records_by_parent_ids([child_coin.name()])
                    assert len(grandchildren) == 1

            loop.run_until_complete(check_state())
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_push_tx_two_step_no_announcement_does_not_push(monkeypatch):
    from tests.cli_clients import FullNodeClientMock

    runner = CliRunner()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        with runner.isolated_filesystem():
            coin = loop.run_until_complete(_setup_sim_coin())

            child_coin = Coin(coin.name(), ACS_PH, coin.amount)
            first_spend = make_spend(coin, ACS, Program.to([[51, ACS_PH, coin.amount]]))
            second_spend = make_spend(
                child_coin,
                ACS,
                Program.to([[80, 0], [51, ACS_PH, child_coin.amount]]),
            )
            two_step_bundle = SpendBundle([first_spend, second_spend], G2Element())

            pushed_bundles: List[SpendBundle] = []
            original_push_tx = FullNodeClientMock.push_tx

            async def record_push_tx(self, spend_bundle):
                pushed_bundles.append(spend_bundle)
                return await original_push_tx(self, spend_bundle)

            monkeypatch.setattr(FullNodeClientMock, "push_tx", record_push_tx)

            result = runner.invoke(
                cli,
                [
                    "push_tx",
                    "--spend-bundle",
                    bytes(two_step_bundle).hex(),
                    "--fee",
                    100,
                    "--confirmation-timeout",
                    "0",
                    "--poll-interval",
                    "0.01",
                ],
            )

            assert result.exit_code == 0
            assert (
                "Cannot find a way to link fee to this transaction. Please specify 0 fee and try again."
                in result.output
            )
            assert pushed_bundles == []
    finally:
        loop.close()
        asyncio.set_event_loop(None)
