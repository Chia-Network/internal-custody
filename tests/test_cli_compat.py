"""Compatibility tests for CLI output and older unsigned spend formats."""

import asyncio
import re
import sqlite3
import webbrowser

from pathlib import Path
from typing import List, Optional

import pytest

from chia._tests.util.spend_sim import SpendSim, SimClient
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia_rs import G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint32
from chia.util.bech32m import encode_puzzle_hash
from click.testing import CliRunner

from cic.cli.common import load_prefarm_info, load_root_derivation
from cic.cli.main import cli, read_unsigned_spend
from cic.cli.sync_store import SyncStore
from cic.drivers.puzzle_root_construction import calculate_puzzle_root
from cic.drivers.singleton import construct_p2_singleton

from hsms.core.unsigned_spend import UnsignedSpend
from hsms.util.byte_chunks import blob_for_zlib_chunks
from hsms.util.qrint_encoding import a2b_qrint, b2a_qrint

from tests.cli_helpers import LaunchedSingleton, setup_launched_singleton, sign_unsigned_spend
from tests.cli_push_tx_helpers import _record_wallet_calls, _setup_sim_coin
from tests.legacy_formats import encode_unsigned_spend_hsms01

ACS: Program = Program.to(1)
ACS_PH = ACS.get_tree_hash()
PAYMENT_AMOUNT = 2
FUNDING_AMOUNT = 1000
RECIPIENT_ADDRESS = encode_puzzle_hash(bytes32([0] * 32), "xch")

# Flags of the earlier operator tooling, including payment -ps/--previous-spends.
BASELINE_CLI = {
    "audit": ["--db-path", "--diff", "--filepath", "-d", "-db", "-f"],
    "clawback": ["--db-path", "--filename", "--pubkeys", "-db", "-f", "-pks"],
    "complete": ["--db-path", "--filename", "-db", "-f"],
    "derive_root": [
        "--configuration",
        "--db-path",
        "--initial-lock-level",
        "--maximum-lock-level",
        "--minimum-pks",
        "--pubkeys",
        "--validate-against",
        "-c",
        "-db",
        "-m",
        "-min",
        "-n",
        "-pks",
        "-va",
    ],
    "examine_spend": ["--qr-density", "--validate-against", "-va"],
    "export_config": ["--db-path", "--filename", "--public", "-db", "-f", "-p"],
    "increase_security_level": ["--db-path", "--filename", "--pubkeys", "-db", "-f", "-pks"],
    "init": [
        "--directory",
        "--payment-clawback",
        "--rekey-cancel",
        "--rekey-timelock",
        "--slow-penalty",
        "--withdrawal-timelock",
        "-d",
        "-pc",
        "-rc",
        "-rt",
        "-sp",
        "-wt",
    ],
    "launch_singleton": [
        "--configuration",
        "--db-path",
        "--fee",
        "--fingerprint",
        "--node-rpc-port",
        "--wallet-rpc-port",
        "-c",
        "-db",
        "-f",
        "-np",
        "-wp",
    ],
    "p2_address": ["--db-path", "--prefix", "-db", "-p"],
    "payment": [
        "--absorb-available-payments",
        "--amount",
        "--amount-threshold",
        "--db-path",
        "--filename",
        "--maximum-extra-cost",
        "--previous-spends",
        "--pubkeys",
        "--recipient-address",
        "-a",
        "-ap",
        "-at",
        "-db",
        "-f",
        "-mc",
        "-pks",
        "-ps",
        "-t",
    ],
    "push_tx": [
        "--fee",
        "--fingerprint",
        "--node-rpc-port",
        "--spend-bundle",
        "--wallet-rpc-port",
        "-b",
        "-f",
        "-m",
        "-np",
        "-wp",
    ],
    "show": ["--config", "--db-path", "--derivation", "-c", "-d", "-db"],
    "start_rekey": [
        "--db-path",
        "--filename",
        "--new-configuration",
        "--pubkeys",
        "-db",
        "-f",
        "-new",
        "-pks",
    ],
    "sync": ["--configuration", "--db-path", "--node-rpc-port", "--show", "-c", "-db", "-np", "-s"],
    "update_config": ["--configuration", "--db-path", "-c", "-db"],
    "which_pubkeys": ["--no-offset", "--num-pubkeys", "--pubkeys", "-m", "-pks"],
}


SYNC_TABLE_SCHEMA = {
    "singletons": (
        "CREATE TABLE singletons("
        "   coin_id blob PRIMARY KEY,"
        "   parent_id blob,"
        "   puzzle_hash blob,"
        "   amount blob,"
        "   puzzle_root blob,"
        "   lineage_proof blob,"
        "   confirmed_at_time bigint,"
        "   generation bigint,"
        "   puzzle_reveal blob,"
        "   solution blob,"
        "   spend_type int,"
        "   spending_pubkey blob)"
    ),
    "p2_singletons": (
        "CREATE TABLE p2_singletons("
        "   coin_id blob PRIMARY KEY,"
        "   parent_id blob,"
        "   puzzle_hash blob,"
        "   amount blob,"
        "   spent tinyint)"
    ),
    "achs": (
        "CREATE TABLE achs("
        "   coin_id blob PRIMARY KEY,"
        "   parent_id blob,"
        "   puzzle_hash blob,"
        "   amount blob,"
        "   from_root blob,"
        "   p2_ph blob,"
        "   confirmed_at_time bigint,"
        "   spent_at_height bigint,"
        "   completed tinyint,"
        "   spending_pubkey blob)"
    ),
    "rekeys": (
        "CREATE TABLE rekeys("
        "   coin_id blob PRIMARY KEY,"
        "   parent_id blob,"
        "   puzzle_hash blob,"
        "   amount blob,"
        "   from_root blob,"
        "   to_root blob,"
        "   timelock blob,"
        "   confirmed_at_time bigint,"
        "   spent_at_height bigint,"
        "   completed tinyint,"
        "   spending_pubkey blob)"
    ),
    "configuration_info": (
        "CREATE TABLE configuration_info(" "   launcher_id blob PRIMARY KEY," "   info blob," "   outdated tinyint)"
    ),
}

SYNC_MASTER_SCHEMA = (
    ("index", "sqlite_autoindex_achs_1", None),
    ("index", "sqlite_autoindex_configuration_info_1", None),
    ("index", "sqlite_autoindex_p2_singletons_1", None),
    ("index", "sqlite_autoindex_rekeys_1", None),
    ("index", "sqlite_autoindex_singletons_1", None),
    ("table", "achs", SYNC_TABLE_SCHEMA["achs"]),
    ("table", "configuration_info", SYNC_TABLE_SCHEMA["configuration_info"]),
    ("table", "p2_singletons", SYNC_TABLE_SCHEMA["p2_singletons"]),
    ("table", "rekeys", SYNC_TABLE_SCHEMA["rekeys"]),
    ("table", "singletons", SYNC_TABLE_SCHEMA["singletons"]),
)


def _push_file(runner: CliRunner, filename: str, key_indices: List[int]) -> None:
    spend_bundle = sign_unsigned_spend(filename, key_indices)
    result = runner.invoke(cli, ["push_tx", "--spend-bundle", bytes(spend_bundle).hex(), "--fee", "100"])
    assert result.exit_code == 0, result.output
    assert r"{'success': True, 'status': 'SUCCESS'}" in result.output


def _sync(runner: CliRunner, launched: LaunchedSingleton) -> None:
    result = runner.invoke(cli, ["sync", "--db-path", str(launched.sync_db_path)])
    assert result.exit_code == 0, result.output


def _write_new_configuration(launched: LaunchedSingleton) -> str:
    new_derivation = calculate_puzzle_root(
        launched.derivation.prefarm_info,
        launched.pubkeys,
        uint32(2),
        uint32(4),
        uint32(2),
    )
    filename = "new_configuration.txt"
    Path(filename).write_bytes(bytes(new_derivation))
    return filename


def _fund_singleton(runner: CliRunner, launched: LaunchedSingleton) -> None:
    async def pay_p2_singleton() -> None:
        async with SpendSim.managed(Path("./sim.db")) as sim:
            sim_client = SimClient(sim)
            acs_coin = next(
                record.coin
                for record in await sim_client.get_coin_records_by_puzzle_hashes([ACS_PH], include_spent_coins=False)
                if record.coin.amount > FUNDING_AMOUNT
            )
            p2_singleton = construct_p2_singleton(launched.derivation.prefarm_info.launcher_id)
            await sim_client.push_tx(
                SpendBundle(
                    [
                        make_spend(
                            acs_coin,
                            ACS,
                            Program.to(
                                [
                                    [51, p2_singleton.get_tree_hash(), FUNDING_AMOUNT],
                                    [51, ACS_PH, acs_coin.amount - FUNDING_AMOUNT],
                                ]
                            ),
                        )
                    ],
                    G2Element(),
                )
            )
            await sim.farm_block()
            sim.pass_time(launched.derivation.prefarm_info.withdrawal_timelock)
            await sim.farm_block()

    asyncio.run(pay_p2_singleton())
    _sync(runner, launched)


def _create_pending_payment(runner: CliRunner, launched: LaunchedSingleton) -> int:
    _fund_singleton(runner, launched)
    filename = "pending_payment.txt"
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
    _push_file(runner, filename, [0, 1, 2])
    _sync(runner, launched)
    return PAYMENT_AMOUNT


def _payment_clawback_file(runner: CliRunner, launched: LaunchedSingleton) -> str:
    _create_pending_payment(runner, launched)
    filename = "payment_clawback.txt"
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
        input="1\n",
    )
    assert result.exit_code == 0, result.output
    return filename


def _unsigned_spend_from_qr_output(output: str) -> UnsignedSpend:
    chunks = []
    for line in output.splitlines():
        match = re.search(r"\d{10,}$", line.strip())
        if match is not None:
            chunks.append(a2b_qrint(match.group(0)))
    assert len(chunks) > 0
    return UnsignedSpend.from_bytes(blob_for_zlib_chunks(chunks))


def test_reads_unsigned_spend_written_by_older_hsms() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner)
        filename = "payment.txt"
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
                "0",
                "--recipient-address",
                RECIPIENT_ADDRESS,
            ],
        )
        assert result.exit_code == 0, result.output
        spend = read_unsigned_spend(filename)

        legacy_filename = "legacy_payment.txt"
        Path(legacy_filename).write_text(b2a_qrint(encode_unsigned_spend_hsms01(spend)))
        restored = read_unsigned_spend(legacy_filename)

        assert restored == spend
        assert [coin_spend.coin.name() for coin_spend in restored.coin_spends] == [
            coin_spend.coin.name() for coin_spend in spend.coin_spends
        ]
        assert [bytes(coin_spend.puzzle_reveal) for coin_spend in restored.coin_spends] == [
            bytes(coin_spend.puzzle_reveal) for coin_spend in spend.coin_spends
        ]
        assert [bytes(coin_spend.solution) for coin_spend in restored.coin_spends] == [
            bytes(coin_spend.solution) for coin_spend in spend.coin_spends
        ]
        assert restored.sum_hints == spend.sum_hints
        assert restored.path_hints == spend.path_hints
        assert restored.agg_sig_me_network_suffix == spend.agg_sig_me_network_suffix


@pytest.mark.parametrize("command_name", ["payment", "clawback", "start_rekey", "increase_security_level"])
def test_qr_output_round_trips(command_name: str) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner)
        db_path = str(launched.sync_db_path)
        keys_3 = ",".join(launched.pubkey_files[0:3])
        spend_file = "spend.txt"
        command_input: Optional[str] = None

        if command_name == "payment":
            base_args = [
                "payment",
                "--db-path",
                db_path,
                "--pubkeys",
                keys_3,
                "--amount",
                "0",
                "--recipient-address",
                RECIPIENT_ADDRESS,
            ]
        elif command_name == "clawback":
            _create_pending_payment(runner, launched)
            base_args = ["clawback", "--db-path", db_path, "--pubkeys", keys_3]
            command_input = "1\n"
        elif command_name == "start_rekey":
            base_args = [
                "start_rekey",
                "--db-path",
                db_path,
                "--pubkeys",
                keys_3,
                "--new-configuration",
                _write_new_configuration(launched),
            ]
        else:
            base_args = [
                "increase_security_level",
                "--db-path",
                db_path,
                "--pubkeys",
                keys_3,
            ]

        qr_result = runner.invoke(cli, base_args, input=command_input)
        assert qr_result.exit_code == 0, qr_result.output
        qr_spend = _unsigned_spend_from_qr_output(qr_result.output)

        file_result = runner.invoke(cli, [*base_args, "--filename", spend_file], input=command_input)
        assert file_result.exit_code == 0, file_result.output
        file_spend = read_unsigned_spend(spend_file)

        assert qr_spend == file_spend


def test_examine_spend_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        launched = setup_launched_singleton(runner)
        spend_file = _payment_clawback_file(runner, launched)

        captured: dict = {}

        def capture_html(url: str, new: int = 0) -> bool:
            captured["url"] = url
            captured["html"] = Path(url.replace("file://", "")).read_text()
            return True

        monkeypatch.setattr(webbrowser, "open", capture_html)
        result = runner.invoke(cli, ["examine_spend", spend_file], input="\n")

        assert result.exit_code == 0, result.output
        assert captured["url"].startswith("file://")
        assert "<svg" in captured["html"]
        assert f"Amount: {PAYMENT_AMOUNT}" in captured["html"]


def test_cli_surface_is_backward_compatible() -> None:
    for command_name, baseline_options in BASELINE_CLI.items():
        assert command_name in cli.commands
        command = cli.commands[command_name]
        accepted_options = set()
        for parameter in command.params:
            accepted_options.update(parameter.opts)
            accepted_options.update(parameter.secondary_opts)
        assert set(baseline_options) <= accepted_options


def test_sync_db_schema_is_unchanged(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.sqlite"

    async def create_store() -> None:
        store = await SyncStore.create(db_path)
        await store.db_wrapper.close()

    asyncio.run(create_store())

    connection = sqlite3.connect(str(db_path))
    try:
        rows = connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall()
        master_rows = connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    finally:
        connection.close()
    schema = {name: sql for name, sql in rows}

    assert set(schema) == set(SYNC_TABLE_SCHEMA)
    for table_name, statement in SYNC_TABLE_SCHEMA.items():
        assert schema[table_name] == statement
    assert master_rows == list(SYNC_MASTER_SCHEMA)


def test_push_tx_reads_signed_bundle_file_with_trailing_newline(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        coin = asyncio.run(_setup_sim_coin())
        spend_bundle = SpendBundle(
            [make_spend(coin, ACS, Program.to([[51, ACS_PH, coin.amount]]))],
            G2Element(),
        )
        filename = "signed_bundle.txt"
        Path(filename).write_text(bytes(spend_bundle).hex() + "\n")
        pushed_bundles, _, _ = _record_wallet_calls(monkeypatch)

        result = runner.invoke(cli, ["push_tx", "-b", filename])

        assert result.exit_code == 0, result.output
        assert result.output == "{'success': True, 'status': 'SUCCESS'}\n"
        assert len(pushed_bundles) == 1
        assert pushed_bundles == [spend_bundle]


PREFARM_INFO_HEX = (
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000001e000000000000005a000000000000001e000000000000000f00000000"
    "0000002d"
)
ROOT_DERIVATION_HEX = (
    "0000000000000000000000000000000000000000000000000000000000000000f33eab1d5d195593ef1d9375eb9a27ac2e19"
    "7c4f43eba7a6d1ea358124a47f42000000000000001e000000000000005a000000000000001e000000000000000f00000000"
    "0000002d000000058f0347a04e231f6e93716eddf281632562af3e947096731532a813da10ce9b4f4a29b36f8a9f102a62d6"
    "1e5dedbd9ce299366bea4a3c8de397218d23a1b24ad94e80b31afc9a26e1c449b7e890b1adc4576d0b70d15d4ac594a62133"
    "dec0d32ea742a47009396860f5feb7a00df0d2ae172145458a0c80be5892dc55ce4e5eae77eef813f43f39683820dc58e692"
    "1153a9c4bf8f10e557e2046db98ea20c8e6dd4026929d1712e7fd667c324122fedf3d672f420dc01cadda3794a4c4a868077"
    "ac2f40f6cb161f872f61910bdacd811534e5b5753242553d9022906cdfa479e172b1eac8e1f38a3743b7897e589424420000"
    "0003000000050000000101d84eafabae9f16c614108d4feebdebd2eb512daf772a64c96ad62592a20993e20000000472b0d8"
    "78fdbbb300205bb09765393c58fb0a2312a30304101f7067567f326ebd0000000000000002182374acea5fb047c7512a8e34"
    "2b93072c3ef4dd49a2667d8cd981a1ad2943fbc084310bce62afb569f137fbb3e0678ae80d951192f99a8b161d9c39d47eb8"
    "d8cc4b0306720bc17735152274879a0b381b57821c03c815f3ef289b000545c54c000000010000000259715b97d887366418"
    "b265e03e74f203fe4248a8318234b5bc56e6ee15a1adaac084310bce62afb569f137fbb3e0678ae80d951192f99a8b161d9c"
    "39d47eb8d8e34d5ad2288dcc091dcd26717c7ac4344664f7221ea63ad302f0a79c78c2e95000000002000000021beab20e05"
    "12afb1d36a7b027c3219fc28feb96cf1d8c038217af444d06f4af9341706ad54e040c846ea3dabd235dac8350455f66ba576"
    "1e76714d9c46a4082bcd451150e83b59d6a3143bd474d74d238bb7b7ceeea309ce85db5efe8c7761740000000300000002b6"
    "c52e60afbcb6745be78e01b2db7b971e111a284ff601d128c23b5aa29b8d1a341706ad54e040c846ea3dabd235dac8350455"
    "f66ba5761e76714d9c46a4082b0000001ef142fcd6589e2be5448e6d4093854e6012ad363ecd4579959903f5513f4b838700"
    "00000000000004581c8459d9d4039bea4fd28ffa194954d7c1567e3dd8da785b792cceee58a0584ff54a483c5455621c29e3"
    "67e5719278bfe45ff8ba7d20aeb1b30fd4dd0ae09684a60536d4ce31f4529742221549e28b2739e57bbc19265118059c71b9"
    "d49a45e5b9b3901ea897fa5e5aaa8774db0bd5c544bb660ce66b9710bea63236398d334cd62359e1302ac4ee2097c4f2ebe3"
    "0e3688051d5e62a5855c71aff8f94c3f350000000100000004307ba059b248e7e734a176a8c7f80197c6333c377021de2608"
    "7294d1ac2b94a44ff54a483c5455621c29e367e5719278bfe45ff8ba7d20aeb1b30fd4dd0ae09684a60536d4ce31f4529742"
    "221549e28b2739e57bbc19265118059c71b9d49a45e5b9b3901ea897fa5e5aaa8774db0bd5c544bb660ce66b9710bea63236"
    "398d3377d4224e30c32535831f31a744d3797b602f838c88e809c5f081f3d75fcadda700000001000000032ff77ead4114ea"
    "cd638669bcfd1570dee27d7066cf8a20832e6890b0a96145c284a60536d4ce31f4529742221549e28b2739e57bbc19265118"
    "059c71b9d49a45e5b9b3901ea897fa5e5aaa8774db0bd5c544bb660ce66b9710bea63236398d33a4e27338df429d13813933"
    "80395e9ade41184a522ca56b0810c6542f59029b6c0000000200000003f70172fae71ac5eff82b162e0fc5c07d57dc56ed3a"
    "1a6375958f75c890303c3d62918c74015f86baa4159a8499d82b3f9ce052caf6ede59c5cea891d934e2e25e5b9b3901ea897"
    "fa5e5aaa8774db0bd5c544bb660ce66b9710bea63236398d33e0f2c85478e0483b0e413e398dcbee39bc52d9a946ca2ed5d5"
    "a793abe5ed898800000003000000038afb41e7a03f0f6b09463b522384e260c2c11ae092b4779ddb1b6d43b29fec8f62918c"
    "74015f86baa4159a8499d82b3f9ce052caf6ede59c5cea891d934e2e25e5b9b3901ea897fa5e5aaa8774db0bd5c544bb660c"
    "e66b9710bea63236398d33ce134dba1a7107c7cfdb80a789c83b412882234a74999283487279a6a7bf2a9d00000008000000"
    "0466cb7f64c8c1ee38639a40f6387456abdabc8699b95ac48d894e32e76649e8fbffb3650f93dad0c098db63501ae08975f5"
    "69c7803710bbafab37449ada9bd2f653306b936ba4ebc60cf5c0812e420587efdf3e2e27fdbdc91dc90becd74b27c6ce62ae"
    "ccb0e01ea9159d82a05e3a522fd077c3443e9696f009cf9fdf58c9f5df0081b57b567e94556883742763ef195a21590778ab"
    "0b3de8800aad2e3b1815a8000000090000000400a6fde28a8ff5553063df3a23e1524c3aee8b7f678f807a9240b5d0e5123a"
    "e0ffb3650f93dad0c098db63501ae08975f569c7803710bbafab37449ada9bd2f653306b936ba4ebc60cf5c0812e420587ef"
    "df3e2e27fdbdc91dc90becd74b27c6ce62aeccb0e01ea9159d82a05e3a522fd077c3443e9696f009cf9fdf58c9f5df744c4b"
    "416343b405b0c83f8ebefcd8e2a145f96aac369f0897a0b399cec22f610000000500000003815a8771f5172680f4843400d6"
    "98b46c26ed85535f8bfe80f031336e87264e9f53306b936ba4ebc60cf5c0812e420587efdf3e2e27fdbdc91dc90becd74b27"
    "c6ce62aeccb0e01ea9159d82a05e3a522fd077c3443e9696f009cf9fdf58c9f5df0ca567c0b2b95ca4485ece73459bf4fe75"
    "a2ecb5a1e8b8fc230896bf62e214aa00000006000000036d7c087654ef1126821090214282877aa9e6d8c2a1514ba0ab4342"
    "c11a35cd66b4339fb693da1dc691f53cccc7cf9f1bb9683633d2482ec55a11e1821f017b6bce62aeccb0e01ea9159d82a05e"
    "3a522fd077c3443e9696f009cf9fdf58c9f5df47a35453e2412d486ccce140a582b8d20ec7079fb3a1d8b496545d98e94778"
    "7c00000007000000035a69d30d663fc4ef3d0800cf2454757fa6cf40b4f1611ec71665005ad22f5757b4339fb693da1dc691"
    "f53cccc7cf9f1bb9683633d2482ec55a11e1821f017b6bce62aeccb0e01ea9159d82a05e3a522fd077c3443e9696f009cf9f"
    "df58c9f5df7cd88f9122fe4e57ba58f33b26813d6dd36b41cec2ccac5816505d1aaaff681600000000000000032295bf1126"
    "2747a637a5afb13a898157025ea7fe7d8d8430dc124618db19a0ee0d29917ec1e7725d19dd83565c590a5c02a44d797f367c"
    "7fcf17a908fbfc483ac6cb6c6df3dbf7b35602027cc885ad6a10d813dd95a8f91eb756e4a94654d6078dd050d6ddab5d00aa"
    "9c03737a8d22adcadc509c618c8a1ac894842ba11e94d000000001000000032f68f5ab99aea91286b8175772dfcb3c81ae4c"
    "a34f18b31953f84c287d43833f0d29917ec1e7725d19dd83565c590a5c02a44d797f367c7fcf17a908fbfc483ac6cb6c6df3"
    "dbf7b35602027cc885ad6a10d813dd95a8f91eb756e4a94654d6078f26833f24932be23fc8e60ed8ce5cf183f31c1fb3be8a"
    "9b8dc694035221303100000001000000022b0b1abf70ccc0d7f2cf96dc8baf1bf86c091cdb8cf9fc057e8162d06392dea6c6"
    "cb6c6df3dbf7b35602027cc885ad6a10d813dd95a8f91eb756e4a94654d607f2d803e63ef9057c3c761edba4fe459baa537f"
    "4a83678c008b13b6b7817d026d0000000200000002dbee71122ef95957cdecbab82666bd9afbdc922ce9bf61293e890d88bf"
    "a6a4e0c2e4a7f16729afb5efe420e723decf678b666e3a420501f15ce779cfa8d17f82f6137813eff991e64c33279951d2f3"
    "c48b481b9e9539b1db962be189658e81a90000000300000002bbda463288f5aedebd718f558e3bf9a32424e0b7238f84e259"
    "c207eca1684f9cc2e4a7f16729afb5efe420e723decf678b666e3a420501f15ce779cfa8d17f825abb5d5568b4a7411dd97b"
    "3356cfedfac09b5fb35621a7fa29ab9b59dc905fb600000000000000037ce66e322c07aba951757e675c64bf94de3ceb7e2d"
    "c3319804efa109e3796195689856f281aa89fc211ccd60ae5f581894e151a401d4fdfbcbb9704ad0deb084e97a8fb6d9ed87"
    "e852db18397c5e2372a115678cd533c07f4d5954bf1461ed020df91132d84844398d71426cb4026ca006af9630de48d4acb2"
    "2df1c04b43be4d00000001000000039e37104cae7e524f49d3de7a6af1f0a2ceb54dbb9d26a871c0e6673477bb0c45689856"
    "f281aa89fc211ccd60ae5f581894e151a401d4fdfbcbb9704ad0deb084e97a8fb6d9ed87e852db18397c5e2372a115678cd5"
    "33c07f4d5954bf1461ed02e6c08e4fb7d8cfd7ea62236549f4efa79becef8342bf00bca4b3c4a0b266a2e000000001000000"
    "02021e27cf7170ed5077514e320eb030ad023e15826eaee192d2df2a7063d01fede97a8fb6d9ed87e852db18397c5e2372a1"
    "15678cd533c07f4d5954bf1461ed026fc6c86b837f6bf75f52e17e63f1fc3667d104366126b33b1c611a8eda8f1f21000000"
    "02000000027b8622d7e1fc9ede1e362d2c026d7542857bd820792445b4d0be1b01459c80861f89f7352bda86d3e84ac912a2"
    "d1162b0ba824c476f18118ee5be779ff8904654eb7420f8651b09124e1d40cdc49eeddacbaa0c25e6ae5a0a482fac8e3b525"
    "9f0000000300000002c704d6d4e49cdaf11ed1c2f52bc6b0a635f329ded5d693b2ec067bb2735fcce91f89f7352bda86d3e8"
    "4ac912a2d1162b0ba824c476f18118ee5be779ff8904650e9d6cb70c2406edf243afa7a9c1109b130b1ca95cca06f8f2ecde"
    "9e397391e900000000000000044a16d174e950f966bd225f208bd64fa0abf9311d96e85c06670fe6f9c1d222552945fc16c0"
    "236a6869a8058e3280dd0e7de29e1c88d9da56233730d45e35387189df430d2976acd9e7b1127633074bf4fb9e45e5281441"
    "2570ee3b8734aa2d30f6f74653b799aca6aaa70b7dd8fc61fa5c57e23193b55328c53e5fc3fa301dd0c1449c437328f963f0"
    "ece716c378f517beacef67235ffae98082ac3046db33e40000000100000004d8d719691fbd8cebb886cf26c96bb3676a3150"
    "f3ae0ef1fba030f589373c62782945fc16c0236a6869a8058e3280dd0e7de29e1c88d9da56233730d45e35387189df430d29"
    "76acd9e7b1127633074bf4fb9e45e52814412570ee3b8734aa2d30f6f74653b799aca6aaa70b7dd8fc61fa5c57e23193b553"
    "28c53e5fc3fa301dd03ea752fe1047fd7aaaf652ba3a2bc4263bc6ed67e2bb97dcb8649ac511cb6b2100000001000000030f"
    "a614a2e86e240d360a0be889d4e8b0ae85d4e4587db5df4ee5e455cad8634a89df430d2976acd9e7b1127633074bf4fb9e45"
    "e52814412570ee3b8734aa2d30f6f74653b799aca6aaa70b7dd8fc61fa5c57e23193b55328c53e5fc3fa301dd006e3479d32"
    "790bee0fe46816b38416fbe82227ad583a480459cc05482dd0f427000000020000000373c645a9bc98aa061efb8a6062263c"
    "ae96a5aefb365c2c8e4ad15978d8bf91e46ae84a47c430c72c10dc5176006c0fde6e34034c3bff204d97c96734d23ffbdbf6"
    "f74653b799aca6aaa70b7dd8fc61fa5c57e23193b55328c53e5fc3fa301dd06eba54710ecd48ee529199006661c769f9db7c"
    "fc16c64f6b611a713d9b8af9ab0000000300000003872dc7fded20668e96a58a8624bf5634474313d5bd0c3cd4d1763fb7f4"
    "87c2116ae84a47c430c72c10dc5176006c0fde6e34034c3bff204d97c96734d23ffbdbf6f74653b799aca6aaa70b7dd8fc61"
    "fa5c57e23193b55328c53e5fc3fa301dd01f3032fb87a2fe036573d5b9bcd2093abf75049da5c4efc6b2c2051555b2464b00"
    "0000080000000452c4ddb58bf7510a20e9f8cf230ff78d32ce0ab4b475302d5ea35d34ccf04112997da7553f374b84459fb4"
    "de9c47a57afd3be2271b271e4f0ead6d6fa2f4b3616a2d4578362ef64fc22558b41f56a87088a4f55ac286a6c79b2336cf5a"
    "30bc6f4a8193052cd4f438dcbfaf157ac0cffdad6a6c3b65183f1606281ff74a948acae0a0610fda975b053ed74208bbaec3"
    "04de0a6af06f3b0bf29ece9a341bfeab8400000009000000046d917a757f0225283df66924c4344bef648998bf0f33a04b58"
    "1a2775b2b9d628997da7553f374b84459fb4de9c47a57afd3be2271b271e4f0ead6d6fa2f4b3616a2d4578362ef64fc22558"
    "b41f56a87088a4f55ac286a6c79b2336cf5a30bc6f4a8193052cd4f438dcbfaf157ac0cffdad6a6c3b65183f1606281ff74a"
    "948aca790957aa8f4f00f29296c395d9335f56df967ba5df1dc37b7eaa779ab19e4637000000050000000378af882027d87c"
    "cc17272916a065652501756dcde14aed3cb52292972fd5e3516a2d4578362ef64fc22558b41f56a87088a4f55ac286a6c79b"
    "2336cf5a30bc6f4a8193052cd4f438dcbfaf157ac0cffdad6a6c3b65183f1606281ff74a948acaf29561de383b5df7c77ca2"
    "55f754ea111e142492d69d05ff7a5155b5133f53870000000600000003eab91563156f463fd9f362f45d036aa6a2b3ec6e54"
    "0fa1da6c76c2ffbd7fa54aff7163b7ebca07819367f3c31e9d54e8039201d423bcb058863dcd958568d84e4a8193052cd4f4"
    "38dcbfaf157ac0cffdad6a6c3b65183f1606281ff74a948aca1c08527f53061f954a696fcd201e22f6678663f846e74c4192"
    "d9f9e0b67dc6e70000000700000003428682572509eb7199efe9b0199b91154e083c3621993341b8b4d648651429fdff7163"
    "b7ebca07819367f3c31e9d54e8039201d423bcb058863dcd958568d84e4a8193052cd4f438dcbfaf157ac0cffdad6a6c3b65"
    "183f1606281ff74a948aca"
)


def test_configuration_files_round_trip(tmp_path: Path) -> None:
    prefarm_path = tmp_path / "prefarm_configuration.txt"
    prefarm_path.write_bytes(bytes.fromhex(PREFARM_INFO_HEX))
    prefarm_info = load_prefarm_info(str(prefarm_path))
    assert bytes(prefarm_info) == bytes.fromhex(PREFARM_INFO_HEX)

    derivation_path = tmp_path / "derivation_configuration.txt"
    derivation_path.write_bytes(bytes.fromhex(ROOT_DERIVATION_HEX))
    derivation = load_root_derivation(str(derivation_path))
    assert bytes(derivation) == bytes.fromhex(ROOT_DERIVATION_HEX)
