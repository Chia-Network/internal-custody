"""CLI tests for building, continuing and checking payment series."""

import asyncio

from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import pytest

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia_rs import G2Element, SpendBundle
from click.testing import CliRunner, Result

from cic.cli.common import read_unsigned_spend
from cic.cli.main import cli
from cic.cli.payment_series import advance_through_previous_payments, load_previous_spend
from cic.cli.record_types import ACHRecord, SingletonRecord
from cic.cli.sync_store import SyncStore

from hsms.util.qrint_encoding import b2a_qrint

from tests.cli_helpers import LaunchedSingleton, setup_launched_singleton, sign_unsigned_spend
from tests.legacy_formats import encode_unsigned_spend_hsms01
from tests.series_helpers import bundle_to_unsigned_spend_file, make_payment
from tests.test_cli_compat import (
    FUNDING_AMOUNT,
    RECIPIENT_ADDRESS,
    _fund_singleton,
    _push_file,
    _sync,
    _write_new_configuration,
)
from tests.test_cli_compat_files import _pass_time, _start_rekey_file
from tests.timelock_profiles import PROFILES, SHORT_TIMELOCKS, TimelockProfile, prefarm_info_for


def _payment_command(
    runner: CliRunner,
    launched: LaunchedSingleton,
    filename: str,
    amount: int,
    previous: Optional[str] = None,
    absorb: bool = False,
    maximum_extra_cost: Optional[int] = None,
) -> Result:
    """Invoke cic payment for one file of a series."""
    args = [
        "payment",
        "--filename",
        filename,
        "--db-path",
        str(launched.sync_db_path),
        "--pubkeys",
        ",".join(launched.pubkey_files[0:3]),
        "--amount",
        str(amount),
        "--recipient-address",
        RECIPIENT_ADDRESS,
    ]
    if absorb:
        args.extend(["--absorb-available-payments", "--amount-threshold", "0"])
        if maximum_extra_cost is not None:
            args.extend(["--maximum-extra-cost", str(maximum_extra_cost)])
    if previous is not None:
        args.extend(["--previous-spends", previous])
    return runner.invoke(cli, args)


def _check_series(runner: CliRunner, launched: LaunchedSingleton, spends: str) -> Result:
    """Invoke cic check_series for a comma separated list of files."""
    return runner.invoke(
        cli,
        ["check_series", "--db-path", str(launched.sync_db_path), "--spends", spends],
    )


def _series_message(profile: TimelockProfile, previous_count: int) -> str:
    """Return the exact timing message printed for a previous spend list."""
    return (
        f"This spend is only valid after the {previous_count} previous spend(s) are confirmed, in order. "
        f"Each payment can be pushed no sooner than {profile.withdrawal_timelock} seconds after the previous one "
        "confirms. Any other spend of the singleton in between invalidates this file; regenerate it."
    )


def _error_text(result: Result) -> str:
    """Return the text Click reports for a failed invocation."""
    if result.exception is not None:
        return str(result.exception)
    return result.output


def _latest_singleton(db_path: Path) -> Optional[SingletonRecord]:
    """Return the latest singleton record in the given sync database."""

    async def read() -> Optional[SingletonRecord]:
        sync_store = await SyncStore.create(db_path)
        try:
            return await sync_store.get_latest_singleton()
        finally:
            await sync_store.db_wrapper.close()

    return asyncio.run(read())


def _ach_records(db_path: Path) -> List[ACHRecord]:
    """Return all ACH records in the given sync database."""

    async def read() -> List[ACHRecord]:
        sync_store = await SyncStore.create(db_path)
        try:
            return await sync_store.get_ach_records(include_completed_coins=True)
        finally:
            await sync_store.db_wrapper.close()

    return asyncio.run(read())


def _p2_singleton_coins(db_path: Path) -> List[Coin]:
    """Return all unspent p2 singleton coins in the given sync database."""

    async def read() -> List[Coin]:
        sync_store = await SyncStore.create(db_path)
        try:
            return await sync_store.get_p2_singletons()
        finally:
            await sync_store.db_wrapper.close()

    return asyncio.run(read())


@PROFILES
def test_cli_series_end_to_end(profile: TimelockProfile) -> None:
    """A series of three payment files is built by the CLI, pushed in order and recorded."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner, prefarm_info_for(profile))
        _fund_singleton(runner, launched)

        before = _latest_singleton(launched.sync_db_path)
        assert before is not None
        p2_coins = _p2_singleton_coins(launched.sync_db_path)
        assert len(p2_coins) == 1
        starting_value = int(before.coin.amount) + int(p2_coins[0].amount)
        amounts = (10, 20, 30)

        result = _payment_command(runner, launched, "p1", amounts[0], absorb=True)
        assert result.exit_code == 0, result.output
        assert result.output == "Successfully wrote spend to p1\n"

        result = _payment_command(runner, launched, "p2", amounts[1], previous="p1")
        assert result.exit_code == 0, result.output
        assert _series_message(profile, 1) in result.output

        result = _payment_command(runner, launched, "p3", amounts[2], previous="p1,p2")
        assert result.exit_code == 0, result.output
        assert _series_message(profile, 2) in result.output

        _pass_time(profile.withdrawal_timelock)
        _push_file(runner, "p1", [0, 1, 2])
        _sync(runner, launched)

        _pass_time(profile.withdrawal_timelock)
        _push_file(runner, "p2", [0, 1, 2])
        _sync(runner, launched)

        _pass_time(profile.withdrawal_timelock)
        _push_file(runner, "p3", [0, 1, 2])
        _sync(runner, launched)

        after = _latest_singleton(launched.sync_db_path)
        assert after is not None
        assert int(after.coin.amount) == starting_value - sum(amounts)
        assert len(_ach_records(launched.sync_db_path)) == 3


@PROFILES
@pytest.mark.parametrize("maximum_extra_cost", [None, 10], ids=["unbounded", "mc10"])
def test_cli_series_absorbs_disjoint_p2_singletons(profile: TimelockProfile, maximum_extra_cost: Optional[int]) -> None:
    """Every file in a CLI series absorbs only p2 singleton coins that earlier files leave unspent."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner, prefarm_info_for(profile))
        _fund_singleton(runner, launched)
        _fund_singleton(runner, launched)

        before = _latest_singleton(launched.sync_db_path)
        assert before is not None
        starting_amount = int(before.coin.amount)
        amounts = (10, 20)

        result = _payment_command(
            runner, launched, "p1", amounts[0], absorb=True, maximum_extra_cost=maximum_extra_cost
        )
        assert result.exit_code == 0, result.output
        coin1, lineage1, absorbed1 = advance_through_previous_payments(
            before.coin,
            before.lineage_proof,
            launched.derivation,
            [("p1", load_previous_spend("p1"))],
        )

        result = _payment_command(
            runner,
            launched,
            "p2",
            amounts[1],
            previous="p1",
            absorb=True,
            maximum_extra_cost=maximum_extra_cost,
        )
        assert result.exit_code == 0, result.output
        _, _, absorbed2 = advance_through_previous_payments(
            coin1,
            lineage1,
            launched.derivation,
            [("p2", load_previous_spend("p2"))],
        )

        expected_sizes = (2, 0) if maximum_extra_cost is None else (1, 1)
        assert (len(absorbed1), len(absorbed2)) == expected_sizes
        assert absorbed1.isdisjoint(absorbed2)

        _pass_time(profile.withdrawal_timelock)
        _push_file(runner, "p1", [0, 1, 2])
        _sync(runner, launched)
        _pass_time(profile.withdrawal_timelock)
        _push_file(runner, "p2", [0, 1, 2])
        _sync(runner, launched)

        after = _latest_singleton(launched.sync_db_path)
        assert after is not None
        assert int(after.coin.amount) == starting_amount + 2 * FUNDING_AMOUNT - sum(amounts)


def test_cli_previous_spends_bad_file() -> None:
    """Unreadable, unrecognised and empty previous spend entries are rejected without writing an output file."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner)
        output_file = "series_out.txt"

        result = _payment_command(runner, launched, output_file, 0, previous="missing.txt")
        assert result.exit_code != 0
        assert result.exception is not None
        assert isinstance(result.exception.__cause__, FileNotFoundError)
        assert _error_text(result).count("missing.txt") == 1
        assert not Path(output_file).exists()

        Path("bad.txt").write_text("this is not a spend file")
        result = _payment_command(runner, launched, output_file, 0, previous="bad.txt")
        assert result.exit_code != 0
        assert _error_text(result) == (
            "Cannot read previous spend file bad.txt: the file is neither an unsigned spend file nor a signed "
            "spend bundle file; check the file name and format"
        )
        assert not Path(output_file).exists()

        result = _payment_command(runner, launched, output_file, 0, previous="p1,,p2")
        assert result.exit_code != 0
        assert _error_text(result) == ("Previous spend list entry 2 is empty; remove it and try again")
        assert not Path(output_file).exists()

        result = _payment_command(runner, launched, output_file, 0, previous="p1,")
        assert result.exit_code != 0
        assert _error_text(result) == ("Previous spend list entry 2 is empty; remove it and try again")
        assert not Path(output_file).exists()


@pytest.mark.parametrize("previous", ["", "   "], ids=["empty", "whitespace"])
def test_previous_spends_empty_value_matches_no_flag(previous: str) -> None:
    """An empty previous spend value behaves like omitting the flag."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner)

        result = _payment_command(runner, launched, "without_flag", 0)
        assert result.exit_code == 0, result.output
        result = _payment_command(runner, launched, "with_empty", 0, previous=previous)
        assert result.exit_code == 0, result.output

        assert Path("with_empty").read_bytes() == Path("without_flag").read_bytes()


def test_previous_spends_accepts_older_hsms_unsigned_file() -> None:
    """The older hsms unsigned layout builds the same next file as the current layout."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner)

        result = _payment_command(runner, launched, "p1", 0)
        assert result.exit_code == 0, result.output
        spend = read_unsigned_spend("p1")
        Path("p1_old.txt").write_text(b2a_qrint(encode_unsigned_spend_hsms01(spend)))

        result = _payment_command(runner, launched, "p2_new", 0, previous="p1")
        assert result.exit_code == 0, result.output
        result = _payment_command(runner, launched, "p2_old", 0, previous="p1_old.txt")
        assert result.exit_code == 0, result.output

        assert Path("p2_new").read_bytes() == Path("p2_old").read_bytes()


def test_previous_spends_accepts_signed_bundle_file() -> None:
    """A signed spend bundle builds the same next file as the unsigned spend file it came from."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner)

        result = _payment_command(runner, launched, "p1", 0)
        assert result.exit_code == 0, result.output
        signed_bundle = sign_unsigned_spend("p1", [0, 1, 2])
        Path("p1.signed").write_text(bytes(signed_bundle).hex() + "\n")

        result = _payment_command(runner, launched, "p2_signed", 0, previous="p1.signed")
        assert result.exit_code == 0, result.output
        result = _payment_command(runner, launched, "p2_unsigned", 0, previous="p1")
        assert result.exit_code == 0, result.output

        assert Path("p2_signed").read_bytes() == Path("p2_unsigned").read_bytes()


def test_continue_series_generated_by_older_tool() -> None:
    """A series started with an older tool can be continued after its first file confirms."""
    profile = SHORT_TIMELOCKS
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner, prefarm_info_for(profile))
        _fund_singleton(runner, launched)

        result = _payment_command(runner, launched, "p1", 10, absorb=True)
        assert result.exit_code == 0, result.output
        result = _payment_command(runner, launched, "p2", 20, previous="p1")
        assert result.exit_code == 0, result.output

        p2_spend = read_unsigned_spend("p2")
        Path("p2_old.txt").write_text(b2a_qrint(encode_unsigned_spend_hsms01(p2_spend)))

        _pass_time(profile.withdrawal_timelock)
        _push_file(runner, "p1", [0, 1, 2])
        _sync(runner, launched)

        result = _payment_command(runner, launched, "p3", 30, previous="p2_old.txt")
        assert result.exit_code == 0, result.output
        assert Path("p3").exists()

        _pass_time(profile.withdrawal_timelock)
        _push_file(runner, "p2_old.txt", [0, 1, 2])

        _pass_time(profile.withdrawal_timelock)
        _push_file(runner, "p3", [0, 1, 2])
        _sync(runner, launched)

        assert len(_ach_records(launched.sync_db_path)) == 3


def test_check_series_reports() -> None:
    """check_series accepts a valid list and reports each kind of problem with an exact line."""
    profile = SHORT_TIMELOCKS
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner, prefarm_info_for(profile))
        current = _latest_singleton(launched.sync_db_path)
        assert current is not None

        result = _payment_command(runner, launched, "p1", 0)
        assert result.exit_code == 0, result.output
        result = _payment_command(runner, launched, "p2", 0, previous="p1")
        assert result.exit_code == 0, result.output

        valid = _check_series(runner, launched, "p1,p2")
        assert valid.exit_code == 0, valid.output
        earliest = int(current.confirmed_at_time) + profile.withdrawal_timelock
        earliest_time = datetime.fromtimestamp(earliest, timezone.utc).isoformat()
        assert f"Earliest push time: {earliest_time} ({earliest})\n" in valid.output
        assert "PROBLEM" not in valid.output

        even_bundle, _ = make_payment(current.coin, current.lineage_proof, launched.derivation, 10)
        even_spend = even_bundle.coin_spends[0]
        solution = Program.from_bytes(bytes(even_spend.solution)).as_python()
        solution[2][2][0] = 1
        modified_spend = even_spend.replace(solution=Program.to(solution).to_serialized())
        bundle_to_unsigned_spend_file("p1_even", SpendBundle([modified_spend], G2Element()))
        even_amount = int(current.coin.amount) - 1
        even = _check_series(runner, launched, "p1_even")
        assert even.exit_code == 1
        assert f"  PROBLEM: the resulting singleton amount {even_amount} must be positive and odd\n" in even.output

        wrong_order = _check_series(runner, launched, "p2,p1")
        assert wrong_order.exit_code == 1
        wrong_coin_line = f"  PROBLEM: does not spend the expected singleton coin {current.coin.name().hex()}"
        assert f"{wrong_coin_line}\n" in wrong_order.output
        assert wrong_order.output.endswith(
            "File p2 and every file after it must be regenerated with "
            "cic payment --previous-spends and signed again.\n"
        )

        new_configuration = _write_new_configuration(launched)
        _start_rekey_file(runner, launched, "start_rekey.txt", new_configuration)
        non_payment = _check_series(runner, launched, "start_rekey.txt")
        assert non_payment.exit_code == 1
        assert "  Spend type: START_REKEY\n" in non_payment.output
        assert "  PROBLEM: is not a payment spend\n" in non_payment.output

        _fund_singleton(runner, launched)
        p2_coins = _p2_singleton_coins(launched.sync_db_path)
        assert len(p2_coins) == 1
        absorbed = p2_coins[0]

        result = _payment_command(runner, launched, "p1_absorb", 0, absorb=True)
        assert result.exit_code == 0, result.output
        funded = _latest_singleton(launched.sync_db_path)
        assert funded is not None
        p1_spends = load_previous_spend("p1_absorb")
        coin1, lineage1, absorbed_ids = advance_through_previous_payments(
            funded.coin,
            funded.lineage_proof,
            launched.derivation,
            [("p1_absorb", p1_spends)],
        )
        assert absorbed_ids == {absorbed.name()}

        p2_bundle, _ = make_payment(coin1, lineage1, launched.derivation, 0, p2_singletons=[absorbed])
        bundle_to_unsigned_spend_file("p2_absorb", p2_bundle)

        duplicate = _check_series(runner, launched, "p1_absorb,p2_absorb")
        assert duplicate.exit_code == 1
        assert (
            f"  PROBLEM: absorbs coin {absorbed.name().hex()} that an earlier file in this list also absorbs\n"
            in duplicate.output
        )

        _pass_time(profile.withdrawal_timelock)
        _push_file(runner, "p1_absorb", [0, 1, 2])
        _sync(runner, launched)
        spent = _latest_singleton(launched.sync_db_path)
        assert spent is not None
        assert spent.coin == coin1

        spent_bundle, _ = make_payment(
            spent.coin, spent.lineage_proof, launched.derivation, 0, p2_singletons=[absorbed]
        )
        bundle_to_unsigned_spend_file("spent_absorb", spent_bundle)

        spent_check = _check_series(runner, launched, "spent_absorb")
        assert spent_check.exit_code == 1
        assert (
            f"  PROBLEM: absorbs coin {absorbed.name().hex()} "
            "that is not an unspent p2_singleton in the synced database\n" in spent_check.output
        )
