import asyncio
from pathlib import Path

from chia_rs import SpendBundle
from chia_rs.sized_ints import uint32
from click.testing import CliRunner

from chia._tests.util.spend_sim import SpendSim

from cic.cli.main import cli
from cic.cli.sync_store import SyncStore
from cic.drivers.puzzle_root_construction import calculate_puzzle_root
from cic.drivers.two_step_push import split_two_step_push

from tests.cli_helpers import LaunchedSingleton, setup_launched_singleton, sign_unsigned_spend


def _push_lock_step_one(runner: CliRunner, setup: LaunchedSingleton) -> SpendBundle:
    bundle_path = "./lock_bundle"
    result = runner.invoke(
        cli,
        [
            "increase_security_level",
            "--filename",
            bundle_path,
            "--db-path",
            str(setup.sync_db_path),
            "--pubkeys",
            ",".join(setup.pubkey_files[0:4]),
        ],
    )
    assert result.exit_code == 0, result.output

    signed_lock_bundle = sign_unsigned_spend(bundle_path, range(4))
    first_bundle = split_two_step_push(signed_lock_bundle).first
    result = runner.invoke(cli, ["push_tx", "--spend-bundle", bytes(first_bundle).hex()])
    assert result.exit_code == 0, result.output
    assert r"{'success': True, 'status': 'SUCCESS'}" in result.output

    result = runner.invoke(cli, ["sync", "--db-path", str(setup.sync_db_path)])
    assert result.exit_code == 0, result.output
    return signed_lock_bundle


def test_complete_pending_lock_after_step_1_only():
    runner = CliRunner()
    with runner.isolated_filesystem():
        setup = setup_launched_singleton(runner)
        db_path = str(setup.sync_db_path)
        signed_lock_bundle = _push_lock_step_one(runner, setup)

        completion_path = "./completion_bundle"
        result = runner.invoke(
            cli,
            ["complete", "--filename", completion_path, "--db-path", db_path],
            input="1\n",
        )
        assert result.exit_code == 0, result.output
        old_root = setup.derivation.prefarm_info.puzzle_root
        assert f"LOCK LEVEL INCREASE from {old_root} (step 2 pending)" in result.output
        assert "REKEY from" not in result.output

        with open(completion_path, "r") as file:
            completion_bundle = SpendBundle.from_bytes(bytes.fromhex(file.read()))
        assert bytes(completion_bundle) == bytes(split_two_step_push(signed_lock_bundle).second)

        result = runner.invoke(cli, ["push_tx", "--spend-bundle", bytes(completion_bundle).hex()])
        assert result.exit_code == 0, result.output
        assert r"{'success': True, 'status': 'SUCCESS'}" in result.output

        result = runner.invoke(cli, ["sync", "--db-path", db_path])
        assert result.exit_code == 0, result.output

        async def check_completed() -> None:
            sync_store = await SyncStore.create(setup.sync_db_path)
            try:
                latest_singleton = await sync_store.get_latest_singleton()
                assert latest_singleton is not None
                assert setup.derivation.next_root is not None
                assert latest_singleton.puzzle_root == setup.derivation.next_root
                assert await sync_store.get_rekey_records(include_completed_coins=False) == []
            finally:
                await sync_store.db_wrapper.close()

        asyncio.get_event_loop().run_until_complete(check_completed())


def test_complete_selects_lock_before_clawback_period(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        setup = setup_launched_singleton(runner)
        _push_lock_step_one(runner, setup)

        monkeypatch.setattr("cic.cli.payment_commands.time.time", lambda: 0)
        result = runner.invoke(
            cli,
            ["complete", "--filename", "./completion_bundle", "--db-path", str(setup.sync_db_path)],
            input="1\n",
        )
        assert result.exit_code == 0, result.output
        assert f"LOCK LEVEL INCREASE from {setup.derivation.prefarm_info.puzzle_root} (step 2 pending)" in result.output
        assert "No actions can be completed at this time." not in result.output


def test_complete_still_lists_normal_rekey_with_clawback_wait(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        setup = setup_launched_singleton(runner)
        db_path = str(setup.sync_db_path)

        new_derivation = calculate_puzzle_root(
            setup.derivation.prefarm_info,
            setup.pubkeys,
            uint32(2),
            uint32(4),
            uint32(2),
        )
        new_configuration = "./new_derivation.txt"
        with open(new_configuration, "wb") as file:
            file.write(bytes(new_derivation))

        async def fast_forward() -> None:
            async with SpendSim.managed(Path("./sim.db")) as sim:
                sim.pass_time(setup.prefarm_info.rekey_increments * 2 + setup.prefarm_info.slow_rekey_timelock)
                await sim.farm_block()

        asyncio.get_event_loop().run_until_complete(fast_forward())

        bundle_path = "./rekey_bundle"
        result = runner.invoke(
            cli,
            [
                "start_rekey",
                "--filename",
                bundle_path,
                "--db-path",
                db_path,
                "--pubkeys",
                ",".join(setup.pubkey_files[0:3]),
                "--new-configuration",
                new_configuration,
            ],
        )
        assert result.exit_code == 0, result.output

        signed_rekey_bundle = sign_unsigned_spend(bundle_path, range(3))
        result = runner.invoke(cli, ["push_tx", "--spend-bundle", bytes(signed_rekey_bundle).hex()])
        assert result.exit_code == 0, result.output
        assert r"{'success': True, 'status': 'SUCCESS'}" in result.output

        result = runner.invoke(cli, ["sync", "--db-path", db_path])
        assert result.exit_code == 0, result.output

        monkeypatch.setattr("cic.cli.payment_commands.time.time", lambda: 0)
        result = runner.invoke(
            cli,
            ["complete", "--filename", "./unused_bundle", "--db-path", db_path],
            input="1\n",
        )
        assert result.exit_code == 0, result.output
        assert (
            f"REKEY from {setup.derivation.prefarm_info.puzzle_root} " f"to {new_derivation.prefarm_info.puzzle_root}"
        ) in result.output
        assert "LOCK LEVEL INCREASE" not in result.output
        assert "No actions can be completed at this time." in result.output
