import asyncio
import dataclasses
import os

from pathlib import Path
from typing import List, Optional

import click

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia_rs import G1Element, G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint32, uint64
from chia.wallet.conditions import AssertCoinAnnouncement
from chia.wallet.puzzles.singleton_top_layer import SINGLETON_LAUNCHER_HASH
from chia.wallet.util.tx_config import DEFAULT_TX_CONFIG
from chia.wallet.wallet_request_types import Addition, CreateSignedTransaction, SelectCoins

from cic.cli.common import (
    get_node_and_wallet_clients,
    load_db,
    load_prefarm_info,
    load_pubkeys,
    load_root_derivation,
)
from cic.cli.sync_store import SyncStore
from cic.drivers.prefarm import construct_singleton_inner_puzzle
from cic.drivers.prefarm_info import PrefarmInfo
from cic.drivers.puzzle_root_construction import RootDerivation, calculate_puzzle_root
from cic.drivers.singleton import generate_launch_conditions_and_coin_spend


@click.command("init", short_help="Create a configuration file for the prefarm")
@click.option(
    "-d",
    "--directory",
    help="The directory in which to create the configuration file",
    default=".",
    show_default=True,
)
@click.option(
    "-wt",
    "--withdrawal-timelock",
    help="The amount of time where nothing has happened before a withdrawal can be made (in seconds)",
    required=True,
)
@click.option(
    "-pc",
    "--payment-clawback",
    help="The amount of time to clawback a payment before it's completed (in seconds)",
    required=True,
)
@click.option(
    "-rc",
    "--rekey-cancel",
    help="The amount of time to cancel a rekey before it's completed (in seconds)",
    required=True,
)
@click.option(
    "-rt",
    "--rekey-timelock",
    help="The amount of time where nothing has happened before a standard rekey can be initiated (in seconds)",
    required=True,
)
@click.option("-sp", "--slow-penalty", help="The time penalty for performing a slow rekey (in seconds)", required=True)
def init_cmd(
    directory: str,
    withdrawal_timelock: int,
    payment_clawback: int,
    rekey_cancel: int,
    rekey_timelock: int,
    slow_penalty: int,
):
    prefarm_info = PrefarmInfo(
        bytes32([0] * 32),
        bytes32([0] * 32),
        uint64(withdrawal_timelock),
        uint64(payment_clawback),
        uint64(rekey_cancel),
        uint64(rekey_timelock),
        uint64(slow_penalty),
    )

    path = Path(directory).joinpath("Configuration (needs derivation).txt")

    with open(path, "wb") as file:
        file.write(bytes(prefarm_info))

    print(f"Created a configuration file: {path}")


@click.command("derive_root", short_help="Take an existing configuration and pubkey set to derive a puzzle root")
@click.option(
    "-c",
    "--configuration",
    help="The configuration file with which to derive the root (or the filepath to create it at if using --db-path)",
    default="./Configuration (needs derivation).txt",
    show_default=True,
)
@click.option(
    "-db",
    "--db-path",
    help="Optionally specify a DB path to find the configuration from",
    default=None,
)
@click.option(
    "-pks", "--pubkeys", help="A comma separated list of pubkey files that will control this money", required=True
)
@click.option(
    "-m",
    "--initial-lock-level",
    help="The initial number of pubkeys required to do a withdrawal or standard rekey",
    required=True,
)
@click.option(
    "-n",
    "--maximum-lock-level",
    help="The maximum number of pubkeys required to do a withdrawal or standard rekey",
    required=False,
)
@click.option(
    "-min",
    "--minimum-pks",
    help="The minimum number of pubkeys required to initiate a slow rekey",
    default=1,
    show_default=True,
)
@click.option(
    "-va",
    "--validate-against",
    help="Specify a configuration file to check whether it matches the specified parameters",
    default=None,
)
def derive_cmd(
    configuration: str,
    db_path: Optional[str],
    pubkeys: str,
    initial_lock_level: int,
    minimum_pks: int,
    validate_against: Optional[str],
    maximum_lock_level: Optional[int] = None,
):
    if db_path is None:
        with open(Path(configuration), "rb") as file:
            prefarm_info = PrefarmInfo.from_bytes(file.read())
    else:

        async def get_prefarm_info() -> PrefarmInfo:
            assert db_path is not None
            sync_store = await load_db(db_path)
            try:
                prefarm_info = await sync_store.get_configuration(True, block_outdated=False)
                assert isinstance(prefarm_info, PrefarmInfo)
                return prefarm_info
            finally:
                await sync_store.db_wrapper.close()

        prefarm_info = asyncio.get_event_loop().run_until_complete(get_prefarm_info())
    pubkey_list: List[G1Element] = list(load_pubkeys(pubkeys))
    derivation: RootDerivation = calculate_puzzle_root(
        prefarm_info,
        pubkey_list,
        uint32(initial_lock_level),
        uint32(len(pubkey_list) if maximum_lock_level is None else maximum_lock_level),
        uint32(minimum_pks),
    )

    if validate_against is None:
        with open(Path(configuration), "wb") as new_file:
            new_file.write(bytes(derivation))
        if "needs derivation" in configuration:
            os.rename(Path(configuration), Path("awaiting launch".join(configuration.split("needs derivation"))))

        print("Custody rules successfully added to configuration")

    else:
        validation_info = load_prefarm_info(validate_against)
        if validation_info.puzzle_root == derivation.prefarm_info.puzzle_root:
            print("Configuration successfully validated")
        else:
            print("Configuration does not match specified parameters")


@click.command("launch_singleton", short_help="Use 1 mojo to launch the singleton that will control the funds")
@click.option(
    "-c",
    "--configuration",
    help="The configuration file with which to launch the singleton",
    default="./Configuration (awaiting launch).txt",
    required=True,
)
@click.option(
    "-db",
    "--db-path",
    help="The file path to initialize the sync database at",
    default="./",
    required=True,
)
@click.option(
    "-wp",
    "--wallet-rpc-port",
    help="Set the port where the Wallet is hosting the RPC interface. See the rpc_port under wallet in config.yaml",
    type=int,
    default=None,
)
@click.option("-f", "--fingerprint", help="Set the fingerprint to specify which wallet to use", type=int, default=None)
@click.option(
    "-np",
    "--node-rpc-port",
    help="Set the port where the Node is hosting the RPC interface. See the rpc_port under full_node in config.yaml",
    type=int,
    default=None,
)
@click.option("--fee", help="Fee to use for the launch transaction (in mojos)", default=0)
def launch_cmd(
    configuration: str,
    db_path: str,
    wallet_rpc_port: Optional[int],
    fingerprint: Optional[int],
    node_rpc_port: Optional[int],
    fee: int,
):
    with open(Path(configuration), "rb") as file:
        derivation = RootDerivation.from_bytes(file.read())

    async def do_command():
        node_client, wallet_client = await get_node_and_wallet_clients(node_rpc_port, wallet_rpc_port, fingerprint)
        try:
            fund_coins: List[Coin] = (
                await wallet_client.select_coins(
                    SelectCoins(
                        wallet_id=uint32(1),
                        amount=uint64(1 + fee),
                        max_coin_amount=uint64(2**64 - 1),
                    )
                )
            ).coins
            fund_coin: Coin = fund_coins[0]
            launcher_coin = Coin(fund_coin.name(), SINGLETON_LAUNCHER_HASH, 1)
            new_derivation: RootDerivation = calculate_puzzle_root(
                dataclasses.replace(derivation.prefarm_info, launcher_id=launcher_coin.name()),
                derivation.pubkey_list,
                derivation.required_pubkeys,
                derivation.maximum_pubkeys,
                derivation.minimum_pubkeys,
            )
            _, launch_spend = generate_launch_conditions_and_coin_spend(
                fund_coin, construct_singleton_inner_puzzle(new_derivation.prefarm_info), uint64(1)
            )
            creation_bundle = SpendBundle([launch_spend], G2Element())
            announcement = AssertCoinAnnouncement(
                asserted_id=launcher_coin.name(),
                asserted_msg=Program.from_bytes(bytes(launch_spend.solution)).get_tree_hash(),
            )
            fund_response = await wallet_client.create_signed_transactions(
                CreateSignedTransaction(
                    wallet_id=uint32(1),
                    additions=[Addition(puzzle_hash=SINGLETON_LAUNCHER_HASH, amount=uint64(1))],
                    coins=fund_coins,
                    fee=uint64(fee),
                    push=False,
                    sign=True,
                ),
                DEFAULT_TX_CONFIG,
                (announcement,),
            )
            if len(fund_response.signed_txs) == 0:
                raise ValueError("`create_signed_transaction` returned no transactions!")
            fund_bundle: Optional[SpendBundle] = fund_response.signed_txs[0].spend_bundle
            if fund_bundle is None:
                raise ValueError("`create_signed_transaction` returned an unsigned transaction!")
            result = await node_client.push_tx(SpendBundle.aggregate([creation_bundle, fund_bundle]))
            if not result["success"]:
                raise ValueError(result["error"])

            with open(Path(configuration), "wb") as file:
                file.write(bytes(new_derivation))
            if "awaiting launch" in configuration:
                os.rename(
                    Path(configuration),
                    Path(
                        new_derivation.prefarm_info.puzzle_root[0:3].hex().join(configuration.split("awaiting launch"))
                    ),
                )

            print("Singleton successfully launched")
        finally:
            node_client.close()
            wallet_client.close()
            await node_client.await_closed()
            await wallet_client.await_closed()

    asyncio.get_event_loop().run_until_complete(do_command())


@click.command("update_config", short_help="Update an outdated config in a sync DB with a new config")
@click.option(
    "-c",
    "--configuration",
    help="The configuration file update the sync database with (default: ./Configuration (******).txt)",
    default=None,
)
@click.option(
    "-db",
    "--db-path",
    help="The file path to find the sync database at (default: ./sync (******).sqlite)",
    default="./",
    required=True,
)
def update_cmd(
    configuration: Optional[str],
    db_path: str,
):
    async def do_command():
        sync_store: SyncStore = await load_db(db_path)
        try:
            if not await sync_store.is_configuration_outdated():
                print("The configuration of this sync DB is not outdated")
            else:
                try:
                    db_config = load_root_derivation(configuration)
                    puzzle_root = db_config.prefarm_info.puzzle_root
                except ValueError:
                    db_config = load_prefarm_info(configuration)
                    puzzle_root = db_config.puzzle_root
                latest_singleton = await sync_store.get_latest_singleton()
                if latest_singleton.puzzle_root != puzzle_root:
                    print("Completing update, but configuration is still outdated")
                    outdated = True
                else:
                    outdated = False
                # await sync_store.db_wrapper.begin_transaction()
                await sync_store.add_configuration(db_config, outdated)
                # await sync_store.db_wrapper.commit_transaction()
                print("Configuration update successful")
        finally:
            await sync_store.db_wrapper.close()

    asyncio.get_event_loop().run_until_complete(do_command())


@click.command("export_config", short_help="Export a copy of the current DB's config")
@click.option(
    "-f",
    "--filename",
    help="The file path to export the config to (default: ./Configuration Export (******).sqlite)",
    default=None,
)
@click.option(
    "-db",
    "--db-path",
    help="The file path to initialize/find the sync database at (default: ./sync (******).sqlite)",
    default="./",
    required=True,
)
@click.option(
    "-p",
    "--public",
    help="Export the public information only",
    is_flag=True,
)
def export_cmd(
    filename: Optional[str],
    db_path: str,
    public: bool,
):
    async def do_command():
        sync_store: SyncStore = await load_db(db_path)
        try:
            if not public:
                try:
                    configuration = await sync_store.get_configuration(False, block_outdated=False)
                    puzzle_root = configuration.prefarm_info.puzzle_root
                except ValueError:
                    configuration = await sync_store.get_configuration(True, block_outdated=False)
                    puzzle_root = configuration.puzzle_root
            else:
                configuration = await sync_store.get_configuration(True, block_outdated=False)
                puzzle_root = configuration.puzzle_root
            if filename is None:
                _filename = f"Configuration Export ({puzzle_root[0:3].hex()}).txt"
            else:
                _filename = filename
            with open(Path(_filename), "wb") as file:
                file.write(bytes(configuration))
            print(f"Config successfully exported to {_filename}")
        finally:
            await sync_store.db_wrapper.close()

    asyncio.get_event_loop().run_until_complete(do_command())
