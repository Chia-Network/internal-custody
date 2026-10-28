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
from chia.types.blockchain_format.program import INFINITE_COST, Program
from chia.types.condition_opcodes import ConditionOpcode
from chia.types.coin_spend import CoinSpend, make_spend
from chia.util.hash import std_hash

from cic.cli.fees import find_fee_announcement
from cic.cli.main import cli
from cic.drivers.two_step_push import split_two_step_push

ACS: Program = Program.to(1)
ACS_PH: bytes32 = ACS.get_tree_hash()


async def _setup_sim_coin() -> Coin:
    async with SpendSim.managed(Path("./sim.db")) as sim:
        await sim.farm_block(ACS_PH)
        sim_client = SimClient(sim)
        coin_records = await sim_client.get_coin_records_by_puzzle_hashes([ACS_PH], include_spent_coins=False)
        return sorted((record.coin for record in coin_records), key=lambda coin: coin.name())[0]


async def _setup_sim_coins(blocks: int = 2) -> List[Coin]:
    async with SpendSim.managed(Path("./sim.db")) as sim:
        for _ in range(blocks):
            await sim.farm_block(ACS_PH)
        sim_client = SimClient(sim)
        coin_records = await sim_client.get_coin_records_by_puzzle_hashes([ACS_PH], include_spent_coins=False)
        return sorted((record.coin for record in coin_records), key=lambda coin: coin.name())


def _conditions_for_spend(coin_spend: CoinSpend) -> List[List[bytes]]:
    _, conditions = Program.from_bytes(bytes(coin_spend.puzzle_reveal)).run_with_cost(
        INFINITE_COST, Program.from_bytes(bytes(coin_spend.solution))
    )
    return conditions.as_python()


def _bundle_coin_ids(bundle: SpendBundle) -> set:
    return {coin_spend.coin.name() for coin_spend in bundle.coin_spends}


def _bundle_conditions(bundle: SpendBundle) -> List[List[bytes]]:
    return [condition for coin_spend in bundle.coin_spends for condition in _conditions_for_spend(coin_spend)]


def _fee_paid(bundle: SpendBundle) -> int:
    created_amount = sum(
        int.from_bytes(condition[2], "big")
        for condition in _bundle_conditions(bundle)
        if condition[0] == ConditionOpcode.CREATE_COIN.value
    )
    return sum(coin_spend.coin.amount for coin_spend in bundle.coin_spends) - created_amount


def _two_step_bundle_with_announcement(coin: Coin) -> tuple:
    child_coin = Coin(coin.name(), ACS_PH, coin.amount)
    first_spend = make_spend(coin, ACS, Program.to([[51, ACS_PH, coin.amount]]))
    second_spend = make_spend(
        child_coin,
        ACS,
        Program.to([[80, 0], [60, b"msg"], [51, ACS_PH, child_coin.amount]]),
    )
    return first_spend, second_spend, SpendBundle([first_spend, second_spend], G2Element())


def _record_wallet_calls(monkeypatch):
    from tests.cli_clients import FullNodeClientMock, WalletClientMock

    pushed_bundles: List[SpendBundle] = []
    fee_bundles: List[SpendBundle] = []
    fee_tx_configs: List = []
    original_push_tx = FullNodeClientMock.push_tx
    original_create_signed_transactions = WalletClientMock.create_signed_transactions

    async def record_push_tx(self, spend_bundle):
        pushed_bundles.append(spend_bundle)
        return await original_push_tx(self, spend_bundle)

    async def record_create_signed_transactions(self, request, tx_config, extra_conditions=()):
        tx_response = await original_create_signed_transactions(self, request, tx_config, extra_conditions)
        fee_bundles.append(tx_response.signed_txs[0].spend_bundle)
        fee_tx_configs.append(tx_config)
        return tx_response

    monkeypatch.setattr(FullNodeClientMock, "push_tx", record_push_tx)
    monkeypatch.setattr(WalletClientMock, "create_signed_transactions", record_create_signed_transactions)
    return pushed_bundles, fee_bundles, fee_tx_configs


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
        asyncio.set_event_loop(asyncio.new_event_loop())


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
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_push_tx_two_step_fee_in_both_steps(monkeypatch):
    runner = CliRunner()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        with runner.isolated_filesystem():
            coins = loop.run_until_complete(_setup_sim_coins())
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
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_push_tx_two_step_fee_coins_are_set_aside(monkeypatch):
    runner = CliRunner()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        with runner.isolated_filesystem():
            coins = loop.run_until_complete(_setup_sim_coins())
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
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_push_tx_two_step_overlapping_fee_coins_abort(monkeypatch):
    from tests.cli_clients import WalletClientMock

    runner = CliRunner()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        with runner.isolated_filesystem():
            coins = loop.run_until_complete(_setup_sim_coins())
            _, _, two_step_bundle = _two_step_bundle_with_announcement(coins[-1])
            pushed_bundles, _, _ = _record_wallet_calls(monkeypatch)

            async def select_same_coin(self, request, tx_config=None):
                return SimpleNamespace(coins=[coins[0]])

            monkeypatch.setattr(WalletClientMock, "select_coins", select_same_coin)

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
            assert "spend the same coin" in result.output
            assert "Nothing was pushed" in result.output
            assert pushed_bundles == []
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_push_tx_two_step_timeout_prints_fee_less_step_2(monkeypatch):
    from tests.cli_clients import FullNodeClientMock

    runner = CliRunner()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        with runner.isolated_filesystem():
            coins = loop.run_until_complete(_setup_sim_coins())
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
            second_with_fee_hex = match.group(1)

            fee_less_match = re.search(
                r"Step 2 spend bundle without the fee \(hex, retry with "
                r"`cic push_tx --spend-bundle <hex> --fee N` to attach a new fee\): ([0-9a-f]+)",
                result.output,
            )
            assert fee_less_match is not None
            fee_less_hex = fee_less_match.group(1)

            fee_less_bundle = SpendBundle.from_bytes(bytes.fromhex(fee_less_hex))
            assert fee_less_bundle == expected_second
            second_with_fee_bundle = SpendBundle.from_bytes(bytes.fromhex(second_with_fee_hex))
            assert _bundle_coin_ids(expected_second) < _bundle_coin_ids(second_with_fee_bundle)

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
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())
