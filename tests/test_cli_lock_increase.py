"""CLI tests for lock-level increases through the rekey flow"""

import asyncio

from pathlib import Path
from types import SimpleNamespace
from typing import List, Sequence

from chia_rs import AugSchemeMPL, G1Element, G2Element, SpendBundle
from click.testing import CliRunner, Result

from cic.cli import payment_commands
from cic.cli.main import cli
from cic.cli.record_types import RekeyRecord
from cic.cli.sync_store import SyncStore
from cic.drivers.prefarm import get_rekey_spend_info
from cic.drivers.puzzle_root_construction import RootDerivation

from tests.cli_helpers import LaunchedSingleton, setup_launched_singleton, sign_unsigned_spend
from tests.cli_push_tx_helpers import _record_wallet_calls
from tests.test_cli_compat import _sync
from tests.test_cli_compat_files import (
    _assert_single_push,
    _clawback_file,
    _complete_file,
    _pass_time,
    _sign_and_push,
)
from tests.test_cli_payment_series import _latest_singleton
from tests.test_lifecycle import secret_key_for_index, sign_message_at_index
from tests.timelock_profiles import PROFILES, TimelockProfile, prefarm_info_for


def _increase_file(
    runner: CliRunner,
    launched: LaunchedSingleton,
    filename: str,
    key_indices: Sequence[int],
) -> Result:
    """Invoke increase_security_level with the pubkey files at the given indices."""
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


def _assert_increase_message(
    result: Result,
    required: int,
    maximum: int,
    profile: TimelockProfile,
    filename: str,
) -> None:
    """Assert the success output of increase_security_level."""
    lines = [line for line in result.output.splitlines() if line]
    assert lines[-1] == f"Successfully wrote spend to {filename}"
    assert lines[-2].startswith(
        f"Rekey to lock level {required + 1} of {maximum}. "
        f"It can be pushed once the singleton has not been spent for {profile.rekey_increments} "
    )


def _current_derivation(db_path: Path) -> RootDerivation:
    """Return the private configuration stored in the sync database."""

    async def read() -> RootDerivation:
        sync_store = await SyncStore.create(db_path)
        try:
            derivation = await sync_store.get_configuration(False, block_outdated=False)
            assert isinstance(derivation, RootDerivation)
            return derivation
        finally:
            await sync_store.db_wrapper.close()

    return asyncio.run(read())


def _rekey_records(db_path: Path) -> List[RekeyRecord]:
    """Return all rekey records in the given sync database."""

    async def read() -> List[RekeyRecord]:
        sync_store = await SyncStore.create(db_path)
        try:
            return await sync_store.get_rekey_records(include_completed_coins=True)
        finally:
            await sync_store.db_wrapper.close()

    return asyncio.run(read())


def _sign_lock_increase_bundle(
    bundle: SpendBundle,
    data_to_sign: bytes,
    key_indices: Sequence[int],
) -> SpendBundle:
    """Sign a lock-level increase bundle with the keys at the given indices."""
    pubkeys: List[G1Element] = [secret_key_for_index(index).get_g1() for index in key_indices]
    aggregate_pubkey = G1Element()
    for pubkey in pubkeys:
        aggregate_pubkey += pubkey
    signature = AugSchemeMPL.aggregate(
        [sign_message_at_index(index, data_to_sign, aggregate_pubkey) for index in key_indices]
    )
    return SpendBundle.aggregate([bundle, SpendBundle([], signature)])


@PROFILES
def test_cli_increase_end_to_end(profile: TimelockProfile, monkeypatch) -> None:
    """An increase is built, pushed, listed, completed and reflected by sync."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner, prefarm_info_for(profile))
        pushed_bundles, _, _ = _record_wallet_calls(monkeypatch)
        required = int(launched.derivation.required_pubkeys)
        maximum = int(launched.derivation.maximum_pubkeys)

        result = _increase_file(runner, launched, "increase.txt", range(required))
        assert result.exit_code == 0, result.output
        _assert_increase_message(result, required, maximum, profile, "increase.txt")

        _pass_time(profile.rekey_increments)
        start_bundle = sign_unsigned_spend("increase.txt", range(required))
        _assert_single_push(runner, pushed_bundles, start_bundle)
        assert len(pushed_bundles) == 1
        _sync(runner, launched)

        result = runner.invoke(
            cli,
            [
                "clawback",
                "--filename",
                "unused.txt",
                "--db-path",
                str(launched.sync_db_path),
                "--pubkeys",
                ",".join(launched.pubkey_files[0:required]),
            ],
            input="0\n",
        )
        assert result.exit_code == 0, result.output
        assert f"LOCK LEVEL INCREASE to {required + 1} of {maximum} (REKEY from" in result.output

        _pass_time(profile.rekey_clawback_period)
        _sync(runner, launched)

        result = runner.invoke(
            cli,
            ["complete", "--filename", "listing.txt", "--db-path", str(launched.sync_db_path)],
            input="0\n",
        )
        assert result.exit_code == 0, result.output
        assert f"1) LOCK LEVEL INCREASE to {required + 1} of {maximum} (REKEY from" in result.output
        assert not Path("listing.txt").exists()

        completion_bundle = _complete_file(runner, launched, "completion.txt", 1)
        _assert_single_push(runner, pushed_bundles, completion_bundle)
        _sync(runner, launched)

        result = runner.invoke(cli, ["show", "-d", "--db-path", str(launched.sync_db_path)])
        assert result.exit_code == 0, result.output
        assert f" - lock level: {required + 1}\n" in result.output
        assert "Config up to date: True\n" in result.output


@PROFILES
def test_cli_complete_waits_for_period(profile: TimelockProfile, monkeypatch) -> None:
    """Before the completion period complete marks the increase as not actionable."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner, prefarm_info_for(profile))
        pushed_bundles, _, _ = _record_wallet_calls(monkeypatch)
        required = int(launched.derivation.required_pubkeys)
        maximum = int(launched.derivation.maximum_pubkeys)

        result = _increase_file(runner, launched, "increase.txt", range(required))
        assert result.exit_code == 0, result.output
        _pass_time(profile.rekey_increments)
        start_bundle = sign_unsigned_spend("increase.txt", range(required))
        _assert_single_push(runner, pushed_bundles, start_bundle)
        _sync(runner, launched)

        record = _rekey_records(launched.sync_db_path)[0]
        before_period = int(record.confirmed_at_time) + profile.rekey_clawback_period - 1
        # Needed because complete compares block timestamps with wall-clock time.
        monkeypatch.setattr(payment_commands, "time", SimpleNamespace(time=lambda: before_period))

        result = runner.invoke(
            cli,
            ["complete", "--filename", "unused.txt", "--db-path", str(launched.sync_db_path)],
        )
        assert result.exit_code == 0, result.output
        assert f"-) LOCK LEVEL INCREASE to {required + 1} of {maximum} (REKEY from" in result.output
        assert "No actions can be completed at this time.\n" in result.output
        assert not Path("unused.txt").exists()


@PROFILES
def test_cli_clawback_cancels_increase(profile: TimelockProfile, monkeypatch) -> None:
    """A pending increase can be clawed back before completion."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner, prefarm_info_for(profile))
        pushed_bundles, _, _ = _record_wallet_calls(monkeypatch)
        required = int(launched.derivation.required_pubkeys)

        result = _increase_file(runner, launched, "increase.txt", range(required))
        assert result.exit_code == 0, result.output
        _pass_time(profile.rekey_increments)
        start_bundle = sign_unsigned_spend("increase.txt", range(required))
        _assert_single_push(runner, pushed_bundles, start_bundle)
        _sync(runner, launched)

        _clawback_file(runner, launched, "clawback.txt", 1)
        _sign_and_push(runner, launched, pushed_bundles, "clawback.txt")

        result = runner.invoke(cli, ["complete", "--db-path", str(launched.sync_db_path)])
        assert result.exit_code == 0, result.output
        assert "No actions outstanding\n" in result.output

        result = runner.invoke(cli, ["show", "-d", "--db-path", str(launched.sync_db_path)])
        assert result.exit_code == 0, result.output
        assert f" - lock level: {required}\n" in result.output


@PROFILES
def test_cli_increase_guards(profile: TimelockProfile, monkeypatch) -> None:
    """Wrong key counts are rejected and the maximum level is enforced."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner, prefarm_info_for(profile))
        pushed_bundles, _, _ = _record_wallet_calls(monkeypatch)
        required = int(launched.derivation.required_pubkeys)
        maximum = int(launched.derivation.maximum_pubkeys)

        result = _increase_file(runner, launched, "too_many.txt", range(required + 1))
        assert result.exit_code != 0
        assert result.output == (
            f"Error: A lock level increase is signed by exactly {required} keys "
            f"(the current lock level); you passed {required + 1}.\n"
        )
        assert not Path("too_many.txt").exists()

        result = _increase_file(runner, launched, "too_few.txt", range(required - 1))
        assert result.exit_code != 0
        assert result.output == (
            f"Error: A lock level increase is signed by exactly {required} keys "
            f"(the current lock level); you passed {required - 1}.\n"
        )
        assert not Path("too_few.txt").exists()

        for key_count, filename in ((required, "first.txt"), (required + 1, "second.txt")):
            result = _increase_file(runner, launched, filename, range(key_count))
            assert result.exit_code == 0, result.output
            _pass_time(profile.rekey_increments)
            start_bundle = sign_unsigned_spend(filename, range(key_count))
            _assert_single_push(runner, pushed_bundles, start_bundle)
            _sync(runner, launched)

            _pass_time(profile.rekey_clawback_period)
            _sync(runner, launched)
            completion_bundle = _complete_file(runner, launched, f"complete_{key_count}.txt", 1)
            _assert_single_push(runner, pushed_bundles, completion_bundle)
            _sync(runner, launched)

        derivation = _current_derivation(launched.sync_db_path)
        assert derivation.required_pubkeys == maximum
        assert derivation.next_root is None

        for key_count, filename in ((maximum, "at_maximum.txt"), (1, "at_maximum_one.txt")):
            result = _increase_file(runner, launched, filename, range(key_count))
            assert result.exit_code != 0
            assert result.output == "Error: already at maximum lock level\n"
            assert not Path(filename).exists()


@PROFILES
def test_cli_lists_timelock0_coin_as_unsupported(profile: TimelockProfile, monkeypatch) -> None:
    """A timelock 0 rekey record is reported as unsupported by complete and clawback."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner, prefarm_info_for(profile))
        pushed_bundles, _, _ = _record_wallet_calls(monkeypatch)
        latest = _latest_singleton(launched.sync_db_path)
        assert latest is not None
        derivation = _current_derivation(launched.sync_db_path)
        four_pubkeys = [secret_key_for_index(index).get_g1() for index in range(4)]

        lock_bundle, data_to_sign = get_rekey_spend_info(
            latest.coin,
            four_pubkeys,
            derivation,
            latest.lineage_proof,
        )
        start_spend = next(spend for spend in lock_bundle.coin_spends if spend.coin == latest.coin)
        signed_bundle = _sign_lock_increase_bundle(
            SpendBundle([start_spend], G2Element()),
            data_to_sign,
            range(4),
        )

        _pass_time(profile.rekey_increments)
        _assert_single_push(runner, pushed_bundles, signed_bundle)
        _sync(runner, launched)

        records = _rekey_records(launched.sync_db_path)
        assert len(records) == 1
        record = records[0]
        assert record.timelock == 0
        skip_line = (
            f"Skipping lock level increase coin (timelock 0) from {record.from_root}: "
            "not supported by this version of cic."
        )

        result = runner.invoke(cli, ["complete", "--db-path", str(launched.sync_db_path)])
        assert result.exit_code == 0, result.output
        assert result.output == f"{skip_line}\nNo actions outstanding\n"

        result = runner.invoke(
            cli,
            [
                "clawback",
                "--db-path",
                str(launched.sync_db_path),
                "--pubkeys",
                ",".join(launched.pubkey_files[0:3]),
            ],
        )
        assert result.exit_code == 0, result.output
        assert result.output == f"{skip_line}\nNo actions outstanding\n"
