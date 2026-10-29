import asyncio
import re

from pathlib import Path

from chia_rs import G2Element, SpendBundle
from click.testing import CliRunner
from typing import List

from chia._tests.util.spend_sim import SpendSim, SimClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend

from cic.cli.main import cli
from cic.drivers.two_step_push import split_two_step_push

from tests import cli_clients
from tests.cli_clients import FullNodeClientMock, WalletClientMock
from tests.cli_push_tx_helpers import (
    ACS,
    ACS_PH,
    LOCK,
    _announcement_bundle,
    _bundle_coin_ids,
    _fee_bearing_bundle,
    _fee_paid,
    _open_sim_without_farming,
    _record_wallet_calls,
    _select_spent_coin_on_second_call,
    _setup_sim_coin,
    _setup_sim_coins,
    _setup_single_wallet_coin,
    _two_step_bundle_with_announcement,
)


def _record_pushes(monkeypatch) -> List[SpendBundle]:
    pushed_bundles: List[SpendBundle] = []
    original_push_tx = FullNodeClientMock.push_tx

    async def record_push_tx(self, spend_bundle):
        pushed_bundles.append(spend_bundle)
        return await original_push_tx(self, spend_bundle)

    monkeypatch.setattr(FullNodeClientMock, "push_tx", record_push_tx)
    return pushed_bundles


def _record_fee_calls(monkeypatch) -> List[object]:
    calls: List[object] = []
    original_create_signed_transactions = WalletClientMock.create_signed_transactions

    async def record_create_signed_transactions(self, request, tx_config, extra_conditions=()):
        calls.append(request)
        return await original_create_signed_transactions(self, request, tx_config, extra_conditions)

    monkeypatch.setattr(WalletClientMock, "create_signed_transactions", record_create_signed_transactions)
    return calls


def _invoke_two_step(runner: CliRunner, bundle: SpendBundle, fee: int = 100):
    return runner.invoke(
        cli,
        [
            "push_tx",
            "--spend-bundle",
            bytes(bundle).hex(),
            "--fee",
            str(fee),
        ],
    )


def test_push_tx_single_fee_coin_two_step_succeeds(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        monkeypatch.setattr(cli_clients, "_open_sim", _open_sim_without_farming)
        lock_coin = asyncio.run(_setup_single_wallet_coin())
        first_spend, second_spend, two_step_bundle = _two_step_bundle_with_announcement(lock_coin, LOCK)
        pushed_bundles, fee_bundles, _ = _record_wallet_calls(monkeypatch)

        result = _invoke_two_step(runner, two_step_bundle)

        assert result.exit_code == 0, result.output
        assert "Fee: 100 mojos per step, 200 mojos total." in result.output
        assert result.output.count(r"{'success': True, 'status': 'SUCCESS'}") == 2
        assert len(pushed_bundles) == 2
        assert len(fee_bundles) == 2
        assert _fee_paid(fee_bundles[0]) == 100
        assert _fee_paid(fee_bundles[1]) == 100
        assert _bundle_coin_ids(fee_bundles[0]).isdisjoint(_bundle_coin_ids(fee_bundles[1]))

        async def check_state() -> None:
            async with SpendSim.managed(Path("./sim.db")) as sim:
                sim_client = SimClient(sim)
                lock_record = await sim_client.get_coin_record_by_name(lock_coin.name())
                assert lock_record is not None and lock_record.spent
                first_record = await sim_client.get_coin_record_by_name(first_spend.coin.name())
                assert first_record is not None and first_record.spent
                children = await sim_client.get_coin_records_by_parent_ids([second_spend.coin.name()])
                assert len(children) == 1
                assert not children[0].spent

        asyncio.run(check_state())


def test_push_tx_two_step_fee_coin_spent_while_waiting_does_not_matter(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        coins = asyncio.run(_setup_sim_coins())
        first_spend, second_spend, two_step_bundle = _two_step_bundle_with_announcement(coins[-1])
        pushed_bundles, fee_bundles, _ = _record_wallet_calls(monkeypatch)
        spent_elsewhere: List[Coin] = []

        original_get_coin_record_by_name = FullNodeClientMock.get_coin_record_by_name
        first_poll = True

        async def spend_second_fee_coin_on_first_poll(self, coin_id):
            nonlocal first_poll
            if not first_poll:
                return await original_get_coin_record_by_name(self, coin_id)
            first_poll = False
            fee_one_coin_ids = _bundle_coin_ids(fee_bundles[0])
            coin_records = await self.get_coin_records_by_puzzle_hashes([ACS_PH], include_spent_coins=False)
            candidates = sorted(
                (record.coin for record in coin_records if record.coin.name() not in fee_one_coin_ids),
                key=lambda coin: coin.name(),
            )
            spent_coin = candidates[0]
            spent_elsewhere.append(spent_coin)
            spend = make_spend(spent_coin, ACS, Program.to([[51, ACS_PH, spent_coin.amount]]))
            await SimClient.push_tx(self, SpendBundle([spend], G2Element()))
            await self.service.farm_block()
            return None

        monkeypatch.setattr(
            FullNodeClientMock,
            "get_coin_record_by_name",
            spend_second_fee_coin_on_first_poll,
        )

        result = runner.invoke(
            cli,
            [
                "push_tx",
                "--spend-bundle",
                bytes(two_step_bundle).hex(),
                "--fee",
                "100",
                "--poll-interval",
                "0.01",
            ],
        )

        assert result.exit_code == 0, result.output
        assert result.output.count(r"{'success': True, 'status': 'SUCCESS'}") == 2
        assert len(pushed_bundles) == 2
        assert len(fee_bundles) == 2
        assert _fee_paid(fee_bundles[1]) == 100
        assert len(spent_elsewhere) == 1
        assert spent_elsewhere[0].name() not in _bundle_coin_ids(fee_bundles[1])

        async def check_state() -> None:
            async with SpendSim.managed(Path("./sim.db")) as sim:
                sim_client = SimClient(sim)
                lock_record = await sim_client.get_coin_record_by_name(coins[-1].name())
                assert lock_record is not None and lock_record.spent
                first_record = await sim_client.get_coin_record_by_name(first_spend.coin.name())
                assert first_record is not None and first_record.spent
                children = await sim_client.get_coin_records_by_parent_ids([second_spend.coin.name()])
                assert len(children) == 1
                assert not children[0].spent

        asyncio.run(check_state())


def test_push_tx_two_step_second_fee_build_failure(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        coins = asyncio.run(_setup_sim_coins())
        _, _, two_step_bundle = _two_step_bundle_with_announcement(coins[-1])
        expected_second = split_two_step_push(two_step_bundle).second
        pushed_bundles = _record_pushes(monkeypatch)

        original_create = WalletClientMock.create_signed_transactions
        calls: List[object] = []

        async def fail_on_second_call(self, request, tx_config, extra_conditions=()):
            calls.append(request)
            if len(calls) == 2:
                raise ValueError("Can't select amount higher than our spendable balance. Amount: 100, spendable: 0")
            return await original_create(self, request, tx_config, extra_conditions)

        monkeypatch.setattr(WalletClientMock, "create_signed_transactions", fail_on_second_call)

        result = _invoke_two_step(runner, two_step_bundle)

        assert result.exit_code != 0
        assert "Step 1 was accepted and confirmed" in result.output
        assert "could not fund the step 2 fee" in result.output
        assert "Can't select amount higher than our spendable balance" in result.output
        assert "Two fee coins are not needed" in result.output
        assert "use --fee 0" in result.output.lower()
        assert len(pushed_bundles) == 1

        match = re.search(r"Step 2 spend bundle \(hex\): ([0-9a-f]+)", result.output)
        assert match is not None
        assert SpendBundle.from_bytes(bytes.fromhex(match.group(1))) == expected_second


def test_push_tx_two_step_first_fee_build_failure(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        coins = asyncio.run(_setup_sim_coins())
        _, _, two_step_bundle = _two_step_bundle_with_announcement(coins[-1])
        pushed_bundles = _record_pushes(monkeypatch)

        async def fail_create(self, request, tx_config, extra_conditions=()):
            raise ValueError("Can't select amount higher than our spendable balance. Amount: 100, spendable: 0")

        monkeypatch.setattr(WalletClientMock, "create_signed_transactions", fail_create)

        result = _invoke_two_step(runner, two_step_bundle)

        assert result.exit_code != 0
        assert "Could not create the step 1 fee transaction" in result.output
        assert "Can't select amount higher than our spendable balance" in result.output
        assert "Nothing was pushed" in result.output
        assert "use --fee 0" in result.output.lower()
        assert pushed_bundles == []
        assert "Step 2 spend bundle" not in result.output


def test_push_tx_two_step_spent_fee_coin_is_rejected(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        coins = asyncio.run(_setup_sim_coins())
        spent_coin = coins[-1]
        _, _, two_step_bundle = _two_step_bundle_with_announcement(spent_coin)
        expected_second = split_two_step_push(two_step_bundle).second
        pushed_bundles, _, _ = _record_wallet_calls(monkeypatch)
        _select_spent_coin_on_second_call(monkeypatch, spent_coin)

        result = _invoke_two_step(runner, two_step_bundle)

        assert result.exit_code != 0
        assert "DOUBLE_SPEND" in result.output
        assert len(pushed_bundles) == 2

        match = re.search(r"Step 2 spend bundle \(hex\): ([0-9a-f]+)", result.output)
        assert match is not None
        second_with_fee = SpendBundle.from_bytes(bytes.fromhex(match.group(1)))
        assert _bundle_coin_ids(expected_second) < _bundle_coin_ids(second_with_fee)

        fee_less_match = re.search(
            r"Step 2 spend bundle without the fee \([^)]*\): ([0-9a-f]+)",
            result.output,
        )
        assert fee_less_match is not None
        fee_less_hex = fee_less_match.group(1)
        assert SpendBundle.from_bytes(bytes.fromhex(fee_less_hex)) == expected_second

        result = runner.invoke(cli, ["push_tx", "--spend-bundle", fee_less_hex])

        assert result.exit_code == 0, result.output
        assert r"{'success': True, 'status': 'SUCCESS'}" in result.output


def test_push_tx_refuses_to_add_fee_to_fee_bearing_bundle(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        coins = asyncio.run(_setup_sim_coins())
        fee_bearing_bundle = _fee_bearing_bundle(coins[-1], 100)
        pushed_bundles = _record_pushes(monkeypatch)
        fee_calls = _record_fee_calls(monkeypatch)

        result = runner.invoke(
            cli,
            ["push_tx", "--spend-bundle", bytes(fee_bearing_bundle).hex(), "--fee", "100"],
        )

        assert result.exit_code != 0
        assert "already pays a fee of 100 mojos" in result.output
        assert "would pay a second fee" in result.output
        assert "Push it without --fee (or use --fee 0), or use the fee-less hex." in result.output
        assert pushed_bundles == []
        assert fee_calls == []

        result = runner.invoke(cli, ["push_tx", "--spend-bundle", bytes(fee_bearing_bundle).hex()])
        assert result.exit_code == 0, result.output
        assert len(pushed_bundles) == 1
        assert fee_calls == []

        announcement_bundle = _announcement_bundle(coins[-2])
        result = runner.invoke(
            cli,
            ["push_tx", "--spend-bundle", bytes(announcement_bundle).hex(), "--fee", "100"],
        )
        assert result.exit_code == 0, result.output
        assert len(pushed_bundles) == 2
        assert len(fee_calls) == 1


def test_push_tx_two_step_prints_fee_per_step_and_total(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        coins = asyncio.run(_setup_sim_coins())
        _, _, two_step_bundle = _two_step_bundle_with_announcement(coins[-1])

        result = _invoke_two_step(runner, two_step_bundle)

        assert result.exit_code == 0, result.output
        fee_line = "Fee: 100 mojos per step, 200 mojos total."
        assert fee_line in result.output
        assert result.output.index(fee_line) < result.output.index("This spend bundle must be pushed in two steps.")


def test_push_tx_fee_help_text():
    result = CliRunner().invoke(cli, ["push_tx", "--help"])

    assert result.exit_code == 0
    help_text = " ".join(result.output.split())
    assert "Fee in mojos attached to each push." in help_text
    assert "attaches this fee to BOTH steps" in help_text
    assert "2 * N mojos in total" in help_text


def test_push_tx_single_no_announcement_does_not_push(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        coin = asyncio.run(_setup_sim_coin())
        no_announcement_bundle = _fee_bearing_bundle(coin, 0)
        pushed_bundles = _record_pushes(monkeypatch)

        result = runner.invoke(
            cli,
            ["push_tx", "--spend-bundle", bytes(no_announcement_bundle).hex(), "--fee", "100"],
        )

        assert result.exit_code != 0
        assert "Cannot find a way to link fee to this transaction. Please specify 0 fee and try again." in result.output
        assert pushed_bundles == []
