"""Pinned compatibility tests for the CLI surface, golden spend and QR output."""

import re
import webbrowser

from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest

from click.testing import CliRunner

from cic.cli.common import read_unsigned_spend, write_unsigned_spend
from cic.cli.main import cli
import cic.cli.examine_commands as examine_commands

from hsms.core.unsigned_spend import UnsignedSpend
from hsms.util.byte_chunks import blob_for_zlib_chunks, chunks_for_zlib_blob
from hsms.util.qrint_encoding import a2b_qrint

from tests.cli_helpers import LaunchedSingleton, setup_launched_singleton
from tests.cli_surface_baseline import BASELINE_PARAMS, UNSET
from tests.golden_spend import build_golden_unsigned_spend
from tests.test_cli_compat import (
    PAYMENT_AMOUNT,
    RECIPIENT_ADDRESS,
    _create_pending_payment,
    _payment_clawback_file,
    _write_new_configuration,
)

GOLDEN_FILE = Path(__file__).parent / "golden" / "unsigned_spend_hsms03.txt"

SPEND_COMMANDS = ["payment", "clawback", "start_rekey", "increase_security_level"]


def _normalized_default(value: object) -> object:
    """Map click's unset sentinel onto the value stored in the baseline table."""
    if type(value).__name__ == "Sentinel":
        return UNSET
    return value


def _command_args(command_name: str, launched: LaunchedSingleton) -> Tuple[List[str], Optional[str]]:
    """Build the arguments and prompt input for one spend command."""
    db_path = str(launched.sync_db_path)
    keys_3 = ",".join(launched.pubkey_files[0:3])
    if command_name == "payment":
        return (
            [
                "payment",
                "--db-path",
                db_path,
                "--pubkeys",
                keys_3,
                "--amount",
                "0",
                "--recipient-address",
                RECIPIENT_ADDRESS,
            ],
            None,
        )
    if command_name == "clawback":
        _create_pending_payment(CliRunner(), launched)
        return ["clawback", "--db-path", db_path, "--pubkeys", keys_3], "1\n"
    if command_name == "start_rekey":
        return (
            [
                "start_rekey",
                "--db-path",
                db_path,
                "--pubkeys",
                keys_3,
                "--new-configuration",
                _write_new_configuration(launched),
            ],
            None,
        )
    return (
        [
            "increase_security_level",
            "--db-path",
            db_path,
            "--pubkeys",
            ",".join(launched.pubkey_files[0:4]),
        ],
        None,
    )


def test_cli_option_defaults_and_types_are_pinned() -> None:
    assert set(BASELINE_PARAMS) <= set(cli.commands)
    for command_name, baseline_params in BASELINE_PARAMS.items():
        command = cli.commands[command_name]
        live_by_name = {parameter.name: parameter for parameter in command.params}
        baseline_names = {entry[0] for entry in baseline_params}
        assert baseline_names <= set(
            live_by_name
        ), f"{command_name} lost parameters: {baseline_names - set(live_by_name)}"
        for entry in baseline_params:
            name, opts, secondary_opts, default, required, is_flag, multiple, type_name, nargs = entry
            parameter = live_by_name[name]
            live = (
                parameter.name,
                tuple(sorted(parameter.opts)),
                tuple(sorted(parameter.secondary_opts)),
                _normalized_default(parameter.default),
                parameter.required,
                getattr(parameter, "is_flag", False),
                getattr(parameter, "multiple", False),
                parameter.type.name,
                parameter.nargs,
            )
            expected = (
                name,
                tuple(opts),
                tuple(secondary_opts),
                default,
                required,
                is_flag,
                multiple,
                type_name,
                nargs,
            )
            assert live == expected, f"{command_name} parameter {name!r} changed: {live!r} != {expected!r}"


def test_golden_unsigned_spend_file_is_read() -> None:
    assert read_unsigned_spend(str(GOLDEN_FILE)) == build_golden_unsigned_spend()


def test_golden_unsigned_spend_file_is_written_identically(tmp_path: Path) -> None:
    rebuilt = build_golden_unsigned_spend()
    written = tmp_path / "unsigned_spend.txt"
    write_unsigned_spend(str(written), rebuilt)
    golden_text = GOLDEN_FILE.read_text()
    assert written.read_text() == golden_text
    assert bytes(read_unsigned_spend(str(GOLDEN_FILE))) == a2b_qrint(golden_text)


@pytest.mark.parametrize("command_name", SPEND_COMMANDS)
def test_qr_output_is_only_decimal_chunk_lines(command_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner)
        base_args, command_input = _command_args(command_name, launched)

        suppressed: List[str] = []
        if command_name == "clawback":
            monkeypatch.setattr("builtins.input", lambda prompt="": "1")
            original_print = print

            def numeric_print(*args: object, **kwargs: Any) -> None:
                text = " ".join(str(arg) for arg in args)
                if re.fullmatch(r"\d+", text):
                    original_print(*args, **kwargs)
                else:
                    suppressed.append(text)

            monkeypatch.setattr("builtins.print", numeric_print)

        qr_result = runner.invoke(cli, base_args, input=command_input)
        assert qr_result.exit_code == 0, qr_result.output
        lines = qr_result.output.splitlines()
        assert len(lines) > 0
        assert all(re.fullmatch(r"\d+", line) is not None for line in lines), qr_result.output

        if command_name == "clawback":
            assert suppressed == [
                "Which actions would you like to cancel?:",
                "",
                f"1) PAYMENT to {RECIPIENT_ADDRESS} of amount {PAYMENT_AMOUNT}",
            ]

        chunks = [a2b_qrint(line) for line in lines]
        assert all(len(chunk) == 255 for chunk in chunks[:-1])
        assert 0 < len(chunks[-1]) <= 255

        file_result = runner.invoke(cli, [*base_args, "--filename", "spend.txt"], input=command_input)
        assert file_result.exit_code == 0, file_result.output
        file_spend = read_unsigned_spend("spend.txt")
        assert UnsignedSpend.from_bytes(blob_for_zlib_chunks(chunks)) == file_spend
        assert chunks == list(chunks_for_zlib_blob(bytes(file_spend), 255))


def test_examine_spend_uses_default_chunk_size(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner)
        spend_file = _payment_clawback_file(runner, launched)

        original_chunks = examine_commands.chunks_for_zlib_blob
        recorded: List[int] = []

        def recording_chunks(blob: bytes, chunk_size: int) -> List[bytes]:
            recorded.append(chunk_size)
            return original_chunks(blob, chunk_size)

        monkeypatch.setattr(examine_commands, "chunks_for_zlib_blob", recording_chunks)
        monkeypatch.setattr(webbrowser, "open", lambda *args, **kwargs: True)

        result = runner.invoke(cli, ["examine_spend", spend_file], input="\n")
        assert result.exit_code == 0, result.output
        assert recorded == [250]

        result = runner.invoke(cli, ["examine_spend", spend_file, "--qr-density", "100"], input="\n")
        assert result.exit_code == 0, result.output
        assert recorded == [250, 100]


@pytest.mark.parametrize("command_name", SPEND_COMMANDS)
def test_spend_commands_print_exact_success_line(command_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if command_name == "clawback":
        monkeypatch.setattr("builtins.input", lambda prompt="": "1")
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner)
        base_args, command_input = _command_args(command_name, launched)
        filename = f"{command_name}_spend.txt"

        result = runner.invoke(cli, [*base_args, "--filename", filename], input=command_input)

        assert result.exit_code == 0, result.output
        success_line = f"Successfully wrote spend to {filename}"
        lines = result.output.splitlines()
        assert lines.count(success_line) == 1, result.output
        non_empty_lines = [line for line in lines if line.strip()]
        assert non_empty_lines[-1] == success_line, result.output
