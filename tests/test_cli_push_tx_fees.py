import asyncio

from click.testing import CliRunner
from cic.cli.main import cli

from tests.cli_push_tx_helpers import (
    _announcement_bundle,
    _fee_bearing_bundle,
    _setup_sim_coin,
    _setup_sim_coins,
    record_fee_calls,
    record_pushes,
)


def test_push_tx_refuses_to_add_fee_to_fee_bearing_bundle(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        coins = asyncio.run(_setup_sim_coins())
        fee_bearing_bundle = _fee_bearing_bundle(coins[-1], 100)
        pushed_bundles = record_pushes(monkeypatch)
        fee_calls = record_fee_calls(monkeypatch)

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


def test_push_tx_fee_help_text():
    result = CliRunner().invoke(cli, ["push_tx", "--help"])

    assert result.exit_code == 0
    help_text = " ".join(result.output.split())
    assert "Fee in mojos attached to the push." in help_text
    assert "2 * N" not in help_text


def test_push_tx_single_no_announcement_does_not_push(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        coin = asyncio.run(_setup_sim_coin())
        no_announcement_bundle = _fee_bearing_bundle(coin, 0)
        pushed_bundles = record_pushes(monkeypatch)

        result = runner.invoke(
            cli,
            ["push_tx", "--spend-bundle", bytes(no_announcement_bundle).hex(), "--fee", "100"],
        )

        assert result.exit_code != 0
        assert "Cannot find a way to link fee to this transaction. Please specify 0 fee and try again." in result.output
        assert pushed_bundles == []
