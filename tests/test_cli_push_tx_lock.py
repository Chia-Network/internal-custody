"""push_tx tests for lock-level increase rekey bundles"""

import asyncio

from pathlib import Path
from types import SimpleNamespace
from typing import List, Sequence

from chia._tests.util.spend_sim import SimClient, SpendSim
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.condition_opcodes import ConditionOpcode
from chia.types.coin_spend import make_spend
from chia.util.hash import std_hash
from chia_rs import AugSchemeMPL, G1Element, G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from click.testing import CliRunner, Result

from cic.cli.fees import find_fee_announcement
from cic.cli.main import cli
from cic.drivers.prefarm import get_rekey_spend_info

from tests.cli_clients import WalletClientMock
from tests.cli_helpers import LaunchedSingleton, setup_launched_singleton, sign_unsigned_spend
from tests.cli_push_tx_helpers import ACS, ACS_PH, _bundle_conditions, _fee_paid, _record_wallet_calls, record_pushes
from tests.test_cli_compat import _sync
from tests.test_cli_compat_files import _complete_file, _pass_time
from tests.test_cli_payment_series import _latest_singleton
from tests.test_lifecycle import secret_key_for_index, sign_message_at_index
from tests.timelock_profiles import SHORT_TIMELOCKS, prefarm_info_for


def _increase_file(
    runner: CliRunner,
    launched: LaunchedSingleton,
    filename: str,
    key_indices: Sequence[int],
) -> Result:
    """Run increase_security_level with the pubkey files at the given indices."""
    return runner.invoke(
        cli,
        [
            "increase_security_level",
            "--filename",
            filename,
            "--db-path",
            str(launched.sync_db_path),
            "--pubkeys",
            ",".join(launched.pubkey_files[index] for index in key_indices),
        ],
    )


def _start_bundle(
    runner: CliRunner,
    launched: LaunchedSingleton,
    filename: str,
) -> SpendBundle:
    """Build and sign the START bundle for a lock-level increase."""
    required = int(launched.derivation.required_pubkeys)
    result = _increase_file(runner, launched, filename, range(required))
    assert result.exit_code == 0, result.output
    return sign_unsigned_spend(filename, range(required))


def _push_tx(runner: CliRunner, bundle: SpendBundle, fee: int = 0) -> Result:
    """Push a spend bundle through the push_tx command."""
    return runner.invoke(
        cli,
        ["push_tx", "--spend-bundle", bytes(bundle).hex(), "--fee", str(fee)],
    )


def _assert_fee_asserts_announcement(fee_bundle: SpendBundle, bundle: SpendBundle) -> None:
    """Assert the fee bundle links to the last announcement in the given bundle."""
    announcement = find_fee_announcement(bundle)
    assert announcement is not None
    assert announcement.asserted_id is not None
    assert announcement.asserted_msg is not None
    expected = std_hash(announcement.asserted_id + announcement.asserted_msg)
    assert any(
        condition[0] == ConditionOpcode.ASSERT_COIN_ANNOUNCEMENT.value and bytes32(condition[1]) == expected
        for condition in _bundle_conditions(fee_bundle)
    )


def _coin_spent(coin_id: bytes32) -> bool:
    """Return whether the sim has a spent record for the given coin."""

    async def read() -> bool:
        async with SpendSim.managed(Path("./sim.db")) as sim:
            record = await SimClient(sim).get_coin_record_by_name(coin_id)
            assert record is not None
            return record.spent

    return asyncio.run(read())


def _spend_fee_coin() -> Coin:
    """Spend an ACS coin in the sim and return it."""

    async def spend() -> Coin:
        async with SpendSim.managed(Path("./sim.db")) as sim:
            sim_client = SimClient(sim)
            records = await sim_client.get_coin_records_by_puzzle_hashes([ACS_PH], include_spent_coins=False)
            coin = sorted((record.coin for record in records), key=lambda item: item.name())[0]
            spend_bundle = SpendBundle(
                [make_spend(coin, ACS, Program.to([[51, ACS_PH, coin.amount]]))],
                G2Element(),
            )
            await sim_client.push_tx(spend_bundle)
            await sim.farm_block()
            return coin

    return asyncio.run(spend())


def _aggregate_pubkeys(pubkeys: List[G1Element]) -> G1Element:
    """Return the aggregate of the given public keys."""
    aggregate = G1Element()
    for pubkey in pubkeys:
        aggregate += pubkey
    return aggregate


def _sign_lock_bundle(bundle: SpendBundle, data_to_sign: bytes, key_indices: Sequence[int]) -> SpendBundle:
    """Sign a one-bundle lock-level increase with the keys at the given indices."""
    pubkeys = [secret_key_for_index(index).get_g1() for index in key_indices]
    aggregate_pubkey = _aggregate_pubkeys(pubkeys)
    signature = AugSchemeMPL.aggregate(
        [sign_message_at_index(index, data_to_sign, aggregate_pubkey) for index in key_indices]
    )
    return SpendBundle.aggregate([bundle, SpendBundle([], signature)])


def test_push_tx_increase_single_push(monkeypatch) -> None:
    """A signed START bundle is pushed once and printed as a single result."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner, prefarm_info_for(SHORT_TIMELOCKS))
        pushed_bundles = record_pushes(monkeypatch)
        start_bundle = _start_bundle(runner, launched, "increase.txt")

        _pass_time(SHORT_TIMELOCKS.rekey_increments)
        result = _push_tx(runner, start_bundle)

        assert result.exit_code == 0, result.output
        assert result.output == "{'success': True, 'status': 'SUCCESS'}\n"
        assert len(pushed_bundles) == 1
        assert "Step" not in result.output
        assert len(max(result.output.split(), key=len)) < 200


def test_push_tx_increase_rejected_is_reported(monkeypatch) -> None:
    """A START bundle pushed before the rekey_increments wait is rejected once."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner, prefarm_info_for(SHORT_TIMELOCKS))
        pushed_bundles = record_pushes(monkeypatch)
        start_bundle = _start_bundle(runner, launched, "increase.txt")

        result = _push_tx(runner, start_bundle)

        assert result.exit_code == 0, result.output
        assert result.output == "{'success': False, 'error': <Err.ASSERT_SECONDS_RELATIVE_FAILED: 105>}\n"
        assert "ASSERT_SECONDS_RELATIVE_FAILED" in result.output
        assert len(pushed_bundles) == 1


def test_push_tx_increase_fee_linked(monkeypatch) -> None:
    """A START bundle with --fee 100 links the fee to its coin announcement."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner, prefarm_info_for(SHORT_TIMELOCKS))
        pushed_bundles, fee_bundles, fee_tx_configs = _record_wallet_calls(monkeypatch)
        start_bundle = _start_bundle(runner, launched, "increase.txt")

        _pass_time(SHORT_TIMELOCKS.rekey_increments)
        result = _push_tx(runner, start_bundle, 100)

        assert result.exit_code == 0, result.output
        assert result.output == "{'success': True, 'status': 'SUCCESS'}\n"
        assert len(fee_tx_configs) == 1
        assert len(fee_bundles) == 1
        assert len(fee_bundles[0].coin_spends) == 1
        assert len(pushed_bundles) == 1
        _assert_fee_asserts_announcement(fee_bundles[0], start_bundle)
        assert _fee_paid(pushed_bundles[0]) == 100


def test_push_tx_complete_fee_linked(monkeypatch) -> None:
    """A completion bundle with --fee 100 links the fee to the rekey coin announcement."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner, prefarm_info_for(SHORT_TIMELOCKS))
        pushed_bundles, fee_bundles, fee_tx_configs = _record_wallet_calls(monkeypatch)
        start_bundle = _start_bundle(runner, launched, "increase.txt")

        _pass_time(SHORT_TIMELOCKS.rekey_increments)
        result = _push_tx(runner, start_bundle)
        assert result.exit_code == 0, result.output
        assert result.output == "{'success': True, 'status': 'SUCCESS'}\n"
        _sync(runner, launched)

        _pass_time(SHORT_TIMELOCKS.rekey_clawback_period)
        _sync(runner, launched)
        completion_bundle = _complete_file(runner, launched, "completion.txt", 1)

        result = _push_tx(runner, completion_bundle, 100)

        assert result.exit_code == 0, result.output
        assert result.output == "{'success': True, 'status': 'SUCCESS'}\n"
        assert len(fee_tx_configs) == 1
        assert len(fee_bundles) == 1
        rekey_coin = next(
            coin_spend.coin for coin_spend in completion_bundle.coin_spends if coin_spend.coin.amount == 0
        )
        announcement = find_fee_announcement(completion_bundle)
        assert announcement is not None
        assert announcement.asserted_id == rekey_coin.name()
        _assert_fee_asserts_announcement(fee_bundles[0], completion_bundle)
        assert _fee_paid(pushed_bundles[-1]) == 100


def test_push_tx_fee_build_failure_pushes_nothing(monkeypatch) -> None:
    """A wallet failure while building the fee leaves the node without a push."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner, prefarm_info_for(SHORT_TIMELOCKS))
        pushed_bundles = record_pushes(monkeypatch)
        start_bundle = _start_bundle(runner, launched, "increase.txt")
        _pass_time(SHORT_TIMELOCKS.rekey_increments)

        async def fail_create(self, request, tx_config, extra_conditions=()):
            """Raise for every attempt to build a fee transaction."""
            raise ValueError("wallet cannot build the transaction")

        monkeypatch.setattr(WalletClientMock, "create_signed_transactions", fail_create)

        result = _push_tx(runner, start_bundle, 100)

        assert result.exit_code != 0
        assert "Could not create the fee transaction" in result.output
        assert pushed_bundles == []


def test_push_tx_spent_fee_coin_rejected(monkeypatch) -> None:
    """A spent fee coin makes the aggregate fail and leaves the START coin unspent."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner, prefarm_info_for(SHORT_TIMELOCKS))
        pushed_bundles = record_pushes(monkeypatch)
        start_bundle = _start_bundle(runner, launched, "increase.txt")
        _pass_time(SHORT_TIMELOCKS.rekey_increments)
        spent_coin = _spend_fee_coin()

        async def select_spent_coin(self, request, tx_config=None):
            """Return the already spent coin for the fee transaction."""
            return SimpleNamespace(coins=[spent_coin])

        monkeypatch.setattr(WalletClientMock, "_select_coins", select_spent_coin)

        result = _push_tx(runner, start_bundle, 100)

        assert result.exit_code == 0, result.output
        assert result.output == "{'success': False, 'error': <Err.DOUBLE_SPEND: 5>}\n"
        assert len(pushed_bundles) == 1
        assert not _coin_spent(start_bundle.coin_spends[0].coin.name())


def test_push_tx_one_shot_lock_bundle_rejected(monkeypatch) -> None:
    """A driver-level one-bundle lock increase is rejected with the ephemeral error."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner, prefarm_info_for(SHORT_TIMELOCKS))
        pushed_bundles = record_pushes(monkeypatch)
        latest = _latest_singleton(launched.sync_db_path)
        assert latest is not None
        four_pubkeys = [secret_key_for_index(index).get_g1() for index in range(4)]

        lock_bundle, data_to_sign = get_rekey_spend_info(
            latest.coin,
            four_pubkeys,
            launched.derivation,
            latest.lineage_proof,
        )
        signed_bundle = _sign_lock_bundle(lock_bundle, data_to_sign, range(4))

        result = _push_tx(runner, signed_bundle)

        assert result.exit_code == 0, result.output
        assert result.output == "{'success': False, 'error': <Err.EPHEMERAL_RELATIVE_CONDITION: 141>}\n"
        assert "EPHEMERAL_RELATIVE_CONDITION" in result.output
        assert len(pushed_bundles) == 1
        assert len(max(result.output.split(), key=len)) < 200


def test_push_tx_removed_options() -> None:
    """The removed two-step options are no longer accepted by push_tx."""
    for option, value in (("--confirmation-timeout", "5"), ("--poll-interval", "1")):
        result = CliRunner().invoke(cli, ["push_tx", "--spend-bundle", "00", option, value])

        assert result.exit_code == 2
        assert "No such option" in result.output
