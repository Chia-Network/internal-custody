"""Compatibility tests for normal CLI flows that use a single node push."""

import asyncio

from pathlib import Path
from typing import List

from chia._tests.util.spend_sim import SpendSim
from chia_rs import SpendBundle
from chia_rs.sized_ints import uint64
from click.testing import CliRunner

from cic.cli.main import cli

from tests.cli_helpers import LaunchedSingleton, setup_launched_singleton, sign_unsigned_spend
from tests.cli_push_tx_helpers import _record_wallet_calls
from tests.test_cli_compat import (
    PAYMENT_AMOUNT,
    RECIPIENT_ADDRESS,
    _fund_singleton,
    _sync,
    _write_new_configuration,
)


def _pass_time(seconds: int) -> None:
    async def advance() -> None:
        async with SpendSim.managed(Path("./sim.db")) as sim:
            sim.pass_time(uint64(seconds))
            await sim.farm_block()

    asyncio.run(advance())


def _payment_file(runner: CliRunner, launched: LaunchedSingleton, filename: str) -> None:
    result = runner.invoke(
        cli,
        [
            "payment",
            "--filename",
            filename,
            "--db-path",
            str(launched.sync_db_path),
            "--pubkeys",
            ",".join(launched.pubkey_files[0:3]),
            "--amount",
            str(PAYMENT_AMOUNT),
            "--recipient-address",
            RECIPIENT_ADDRESS,
            "--absorb-available-payments",
            "--amount-threshold",
            str(PAYMENT_AMOUNT),
        ],
    )
    assert result.exit_code == 0, result.output


def _start_rekey_file(
    runner: CliRunner,
    launched: LaunchedSingleton,
    filename: str,
    new_configuration: str,
) -> None:
    result = runner.invoke(
        cli,
        [
            "start_rekey",
            "--filename",
            filename,
            "--db-path",
            str(launched.sync_db_path),
            "--pubkeys",
            ",".join(launched.pubkey_files[0:3]),
            "--new-configuration",
            new_configuration,
        ],
    )
    assert result.exit_code == 0, result.output


def _clawback_file(runner: CliRunner, launched: LaunchedSingleton, filename: str, selection: int) -> None:
    result = runner.invoke(
        cli,
        [
            "clawback",
            "--filename",
            filename,
            "--db-path",
            str(launched.sync_db_path),
            "--pubkeys",
            ",".join(launched.pubkey_files[0:3]),
        ],
        input=f"{selection}\n",
    )
    assert result.exit_code == 0, result.output


def _complete_file(runner: CliRunner, launched: LaunchedSingleton, filename: str, selection: int) -> SpendBundle:
    result = runner.invoke(
        cli,
        [
            "complete",
            "--filename",
            filename,
            "--db-path",
            str(launched.sync_db_path),
        ],
        input=f"{selection}\n",
    )
    assert result.exit_code == 0, result.output
    return SpendBundle.from_bytes(bytes.fromhex(Path(filename).read_text()))


def _assert_single_push(runner: CliRunner, pushed_bundles: List[SpendBundle], spend_bundle: SpendBundle) -> None:
    push_count = len(pushed_bundles)
    result = runner.invoke(cli, ["push_tx", "-b", bytes(spend_bundle).hex()])
    assert result.exit_code == 0, result.output
    assert result.output == "{'success': True, 'status': 'SUCCESS'}\n"
    assert len(pushed_bundles) == push_count + 1
    assert pushed_bundles[-1] == spend_bundle


def _sign_and_push(
    runner: CliRunner,
    launched: LaunchedSingleton,
    pushed_bundles: List[SpendBundle],
    filename: str,
) -> None:
    spend_bundle = sign_unsigned_spend(filename, [0, 1, 2])
    _assert_single_push(runner, pushed_bundles, spend_bundle)
    _sync(runner, launched)


def test_normal_flow_bundles_use_single_push(monkeypatch) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner)
        pushed_bundles, _, _ = _record_wallet_calls(monkeypatch)

        _fund_singleton(runner, launched)

        _payment_file(runner, launched, "first_payment.txt")
        _sign_and_push(runner, launched, pushed_bundles, "first_payment.txt")

        _pass_time(int(launched.prefarm_info.withdrawal_timelock))
        _payment_file(runner, launched, "second_payment.txt")
        _sign_and_push(runner, launched, pushed_bundles, "second_payment.txt")

        _clawback_file(runner, launched, "payment_clawback.txt", 1)
        _sign_and_push(runner, launched, pushed_bundles, "payment_clawback.txt")

        _pass_time(int(launched.prefarm_info.payment_clawback_period))
        payment_completion = _complete_file(runner, launched, "payment_completion.txt", 1)
        _assert_single_push(runner, pushed_bundles, payment_completion)
        _sync(runner, launched)

        _pass_time(int(launched.prefarm_info.rekey_increments))
        new_configuration = _write_new_configuration(launched)

        _start_rekey_file(runner, launched, "first_rekey.txt", new_configuration)
        _sign_and_push(runner, launched, pushed_bundles, "first_rekey.txt")

        _clawback_file(runner, launched, "rekey_clawback.txt", 1)
        _sign_and_push(runner, launched, pushed_bundles, "rekey_clawback.txt")

        _pass_time(int(launched.prefarm_info.rekey_increments))
        _start_rekey_file(runner, launched, "second_rekey.txt", new_configuration)
        _sign_and_push(runner, launched, pushed_bundles, "second_rekey.txt")

        _pass_time(int(launched.prefarm_info.rekey_clawback_period))
        rekey_completion = _complete_file(runner, launched, "rekey_completion.txt", 1)
        _assert_single_push(runner, pushed_bundles, rekey_completion)
        _sync(runner, launched)

        assert len(pushed_bundles) == 8
