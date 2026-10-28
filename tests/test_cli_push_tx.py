import asyncio
import re

from chia_rs import G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64
from click.testing import CliRunner
from pathlib import Path
from types import SimpleNamespace
from typing import List

from chia._tests.util.spend_sim import SpendSim, SimClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.condition_opcodes import ConditionOpcode
from chia.types.coin_spend import make_spend
from chia.util.hash import std_hash

from cic.cli.fees import find_fee_announcement
from cic.cli.main import cli
from cic.drivers.two_step_push import split_two_step_push

from tests.cli_push_tx_helpers import (
    ACS,
    ACS_PH,
    _bundle_coin_ids,
    _bundle_conditions,
    _fee_paid,
    _record_wallet_calls,
    _setup_sim_coin,
    _setup_sim_coins,
    _two_step_bundle_with_announcement,
)


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

    announcement = find_fee_announcement(bundle)

    assert announcement is not None
    assert announcement.asserted_id == second_coin.name()
    assert announcement.asserted_msg == b"second"


def test_push_tx_two_step_confirmation_timeout(monkeypatch):
    from tests.cli_clients import FullNodeClientMock

    runner = CliRunner()
    with runner.isolated_filesystem():
        coin = asyncio.run(_setup_sim_coin())

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
        assert "cic sync" in result.output
        assert "may have been dropped" in result.output
        assert "Step 1 was accepted, but" not in result.output
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

        asyncio.run(check_state())


def test_push_tx_two_step_no_announcement_does_not_push(monkeypatch):
    from tests.cli_clients import FullNodeClientMock

    runner = CliRunner()
    with runner.isolated_filesystem():
        coin = asyncio.run(_setup_sim_coin())

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

        assert result.exit_code != 0
        assert "Cannot find a way to link fee to this transaction. Please specify 0 fee and try again." in result.output
        assert pushed_bundles == []


def test_push_tx_two_step_fee_in_both_steps(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        coins = asyncio.run(_setup_sim_coins())
        _, second_spend, two_step_bundle = _two_step_bundle_with_announcement(coins[-1])
        pushed_bundles, fee_bundles, _ = _record_wallet_calls(monkeypatch)

        result = runner.invoke(
            cli,
            [
                "push_tx",
                "--spend-bundle",
                bytes(two_step_bundle).hex(),
                "--fee",
                "100",
            ],
        )

        assert result.exit_code == 0
        assert result.output.count(r"{'success': True, 'status': 'SUCCESS'}") == 2
        assert len(pushed_bundles) == 2
        assert len(fee_bundles) == 2

        fees_for_push = []
        for pushed in pushed_bundles:
            containing = [fee for fee in fee_bundles if _bundle_coin_ids(fee) <= _bundle_coin_ids(pushed)]
            assert len(containing) == 1
            fees_for_push.append(containing[0])

        step_one, step_two = pushed_bundles
        step_one_fee, step_two_fee = fees_for_push

        assert any(
            condition[0] == ConditionOpcode.ASSERT_CONCURRENT_SPEND.value
            and bytes32(condition[1]) == step_one.coin_spends[0].coin.name()
            for condition in _bundle_conditions(step_one_fee)
        )

        expected_announcement_id = std_hash(second_spend.coin.name() + b"msg")
        assert any(
            condition[0] == ConditionOpcode.ASSERT_COIN_ANNOUNCEMENT.value
            and bytes32(condition[1]) == expected_announcement_id
            for condition in _bundle_conditions(step_two_fee)
        )

        assert _fee_paid(step_one_fee) == 100
        assert _fee_paid(step_two_fee) == 100


def test_push_tx_two_step_fee_coins_are_set_aside(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        coins = asyncio.run(_setup_sim_coins())
        _, _, two_step_bundle = _two_step_bundle_with_announcement(coins[-1])
        _, fee_bundles, fee_tx_configs = _record_wallet_calls(monkeypatch)

        result = runner.invoke(
            cli,
            [
                "push_tx",
                "--spend-bundle",
                bytes(two_step_bundle).hex(),
                "--fee",
                "100",
            ],
        )

        assert result.exit_code == 0
        assert len(fee_bundles) == 2
        assert len(fee_tx_configs) == 2

        first_fee_coin_ids = _bundle_coin_ids(fee_bundles[0])
        second_fee_coin_ids = _bundle_coin_ids(fee_bundles[1])
        assert first_fee_coin_ids.isdisjoint(second_fee_coin_ids)
        assert fee_tx_configs[0].excluded_coin_ids == []
        assert set(fee_tx_configs[1].excluded_coin_ids) == first_fee_coin_ids


def test_push_tx_two_step_overlapping_fee_coins_abort(monkeypatch):
    from tests.cli_clients import WalletClientMock

    runner = CliRunner()
    with runner.isolated_filesystem():
        coins = asyncio.run(_setup_sim_coins())
        _, _, two_step_bundle = _two_step_bundle_with_announcement(coins[-1])
        pushed_bundles, _, _ = _record_wallet_calls(monkeypatch)

        async def select_same_coin(self, request, tx_config=None):
            return SimpleNamespace(coins=[coins[0]])

        monkeypatch.setattr(WalletClientMock, "_select_coins", select_same_coin)

        result = runner.invoke(
            cli,
            [
                "push_tx",
                "--spend-bundle",
                bytes(two_step_bundle).hex(),
                "--fee",
                "100",
            ],
        )

        assert result.exit_code != 0
        assert "same coin(s)" in result.output
        assert "Nothing was pushed" not in result.output
        assert len(pushed_bundles) == 1
        match = re.search(r"Step 2 spend bundle \(hex\): ([0-9a-f]+)", result.output)
        assert match is not None
        assert SpendBundle.from_bytes(bytes.fromhex(match.group(1))) == split_two_step_push(two_step_bundle).second


def test_push_tx_two_step_timeout_prints_fee_less_step_2(monkeypatch):
    from tests.cli_clients import FullNodeClientMock

    runner = CliRunner()
    with runner.isolated_filesystem():
        coins = asyncio.run(_setup_sim_coins())
        _, _, two_step_bundle = _two_step_bundle_with_announcement(coins[-1])
        expected_second = split_two_step_push(two_step_bundle).second

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
                "--fee",
                "100",
                "--confirmation-timeout",
                "0",
                "--poll-interval",
                "0.01",
            ],
        )

        assert result.exit_code != 0
        match = re.search(r"Step 2 spend bundle \(hex\): ([0-9a-f]+)", result.output)
        assert match is not None
        fee_less_hex = match.group(1)
        assert SpendBundle.from_bytes(bytes.fromhex(fee_less_hex)) == expected_second
        assert "without the fee" not in result.output

        monkeypatch.setattr(FullNodeClientMock, "get_coin_record_by_name", original_get_coin_record_by_name)

        result = runner.invoke(
            cli,
            [
                "push_tx",
                "--spend-bundle",
                fee_less_hex,
                "--fee",
                "100",
            ],
        )
        assert result.exit_code == 0
        assert r"{'success': True, 'status': 'SUCCESS'}" in result.output
