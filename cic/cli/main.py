import asyncio
import click
import dataclasses
import math
import os
import time

from blspy import PrivateKey, G1Element, G2Element
from clvm.casts import int_to_bytes
from operator import attrgetter
from pathlib import Path
from typing import List, Optional

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program, INFINITE_COST
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.announcement import Announcement
from chia.types.coin_record import CoinRecord
from chia.types.coin_spend import CoinSpend
from chia.types.spend_bundle import SpendBundle
from chia.util.bech32m import decode_puzzle_hash, encode_puzzle_hash
from chia.util.ints import uint32, uint64
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzles.singleton_top_layer import SINGLETON_LAUNCHER_HASH
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import (
    calculate_synthetic_offset,
    DEFAULT_HIDDEN_PUZZLE_HASH,
)

from cic import __version__
from cic.cli.clients import get_wallet_and_node_clients, get_node_client, get_additional_data
from cic.cli.record_types import SingletonRecord, ACHRecord, RekeyRecord
from cic.cli.sync_store import SyncStore
from cic.drivers.prefarm_info import PrefarmInfo
from cic.drivers.prefarm import (
    SpendType,
    construct_full_singleton,
    construct_singleton_inner_puzzle,
    get_new_puzzle_root_from_solution,
    get_withdrawal_spend_info,
    get_rekey_spend_info,
    get_ach_clawback_spend_info,
    get_rekey_clawback_spend_info,
    get_ach_clawforward_spend_bundle,
    get_rekey_completion_spend,
    get_spend_type_for_solution,
    get_spending_pubkey_for_solution,
    get_spend_params_for_ach_creation,
    get_spend_params_for_rekey_creation,
    was_rekey_completed,
)
from cic.drivers.puzzle_root_construction import RootDerivation, calculate_puzzle_root
from cic.drivers.singleton import generate_launch_conditions_and_coin_spend, construct_p2_singleton

from hsms.bls12_381 import BLSPublicKey, BLSSecretExponent
from hsms.process.signing_hints import SumHint
from hsms.process.unsigned_spend import UnsignedSpend
from hsms.streamables.coin_spend import CoinSpend as HSMCoinSpend

CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])


def load_prefarm_info(configuration: Optional[str]) -> PrefarmInfo:
    if configuration is None:
        path: Path = next(Path("./").glob("Configuration (*).txt"))
    else:
        path = Path(configuration)
        if path.is_dir():
            path = next(path.glob("Configuration (*).txt"))
    with open(path, "rb") as file:
        file_bytes = file.read()
        try:
            return PrefarmInfo.from_bytes(file_bytes)
        except AssertionError:
            try:
                return RootDerivation.from_bytes(file_bytes).prefarm_info
            except AssertionError:
                raise ValueError("The configuration specified is not a recognizable format")


def load_root_derivation(configuration: Optional[str]) -> RootDerivation:
    if configuration is None:
        path: Path = next(Path("./").glob("Configuration (*).txt"))
    else:
        path = Path(configuration)
        if path.is_dir():
            path = next(path.glob("Configuration (*).txt"))
    with open(path, "rb") as file:
        file_bytes = file.read()
        try:
            return RootDerivation.from_bytes(file_bytes)
        except AssertionError:
            try:
                PrefarmInfo.from_bytes(file_bytes)
                raise ValueError("The specified configuration file can only perform observer actions")
            except AssertionError:
                raise ValueError("The configuration specified is not a recognizable format")


async def load_db(db_path: str, launcher_id: Optional[bytes32] = None) -> SyncStore:
    path = Path(db_path)
    if path.is_dir():
        existing = list(path.glob("sync (*).sqlite"))
        if len(existing) == 0:
            if launcher_id is None:
                raise ValueError("Insufficient info to initialize DB")
            else:
                path = path.joinpath(f"sync ({launcher_id[0:3].hex()}).sqlite")
        else:
            path = existing[0]
    return await SyncStore.create(path)


@click.group(
    help="\n  Commands to control a prefarm singleton \n",
    context_settings=CONTEXT_SETTINGS,
)
@click.version_option(__version__)
@click.pass_context
def cli(ctx: click.Context) -> None:
    ctx.ensure_object(dict)


@cli.command("init", short_help="Create a configuration file for the prefarm")
@click.option(
    "-d", "--directory", help="The directory in which to create the configuration file", default=".", required=True
)
@click.option("-d", "--date", help="Unix time at which withdrawals become possible", required=True)
@click.option("-r", "--rate", help="Mojos that can be withdrawn per second", required=True)
@click.option(
    "-a", "--amount", help="The initial amount that will be locked in this custody program (in mojos)", required=True
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
def init_cmd(
    directory: str,
    date: int,
    rate: int,
    amount: int,
    withdrawal_timelock: int,
    payment_clawback: int,
    rekey_cancel: int,
):
    prefarm_info = PrefarmInfo(
        bytes32([0] * 32),
        uint64(date),
        uint64(amount),
        uint64(rate),
        bytes32([0] * 32),
        uint64(withdrawal_timelock),
        uint64(payment_clawback),
        uint64(rekey_cancel),
    )

    path = Path(directory)
    path_1 = path.joinpath("Configuration (needs derivation).txt")
    path_2 = path.joinpath("Observer Info.txt")

    with open(path_1, "wb") as file:
        file.write(bytes(prefarm_info))
    with open(path_2, "wb") as file:
        file.write(bytes(prefarm_info))


@cli.command("derive_root", short_help="Take an existing configuration and pubkey set to derive a puzzle root")
@click.option(
    "-c",
    "--configuration",
    help="The configuration file with which to derive the root (or the filepath to create it at if using --db-path)",
    default="./Configuration (needs derivation).txt",
    show_default=True,
    required=True,
)
@click.option(
    "-db",
    "--db-path",
    help="Optionally specify a DB path to find the configuration from",
    default=None,
)
@click.option("-pks", "--pubkeys", help="A comma separated list of pubkeys to derive a puzzle for", required=True)
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
    required=True,
)
@click.option(
    "-rt",
    "--rekey-timelock",
    help="The amount of time where nothing has happened before a standard rekey can be initiated (in seconds)",
    required=True,
)
@click.option("-sp", "--slow-penalty", help="The time penalty for performing a slow rekey (in seconds)", required=True)
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
    rekey_timelock: int,
    slow_penalty: int,
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
                await sync_store.db_connection.close()

        prefarm_info = asyncio.get_event_loop().run_until_complete(get_prefarm_info())

    pubkey_list: List[G1Element] = [G1Element.from_bytes(bytes.fromhex(pk)) for pk in pubkeys.split(",")]
    derivation: RootDerivation = calculate_puzzle_root(
        prefarm_info,
        pubkey_list,
        uint32(initial_lock_level),
        uint32(len(pubkey_list) if maximum_lock_level is None else maximum_lock_level),
        uint32(minimum_pks),
        uint64(slow_penalty),
        uint64(rekey_timelock),
    )

    if validate_against is None:
        with open(Path(configuration), "wb") as new_file:
            new_file.write(bytes(derivation))
        if "needs derivation" in configuration:
            os.rename(Path(configuration), Path("awaiting launch".join(configuration.split("needs derivation"))))

    else:
        validation_info = load_prefarm_info(validate_against)
        if validation_info.puzzle_root == derivation.prefarm_info.puzzle_root:
            print("Configuration successfully validated")
        else:
            print("Configuration does not match specified parameters")


@cli.command("launch_singleton", short_help="Use 1 mojo to launch the singleton that will control the funds")
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
        node_client, wallet_client = await get_wallet_and_node_clients(node_rpc_port, wallet_rpc_port, fingerprint)
        try:
            fund_coin: Coin = (await wallet_client.select_coins(amount=1, wallet_id=1))[0]
            launcher_coin = Coin(fund_coin.name(), SINGLETON_LAUNCHER_HASH, 1)
            new_derivation: RootDerivation = calculate_puzzle_root(
                dataclasses.replace(derivation.prefarm_info, launcher_id=launcher_coin.name()),
                derivation.pubkey_list,
                derivation.required_pubkeys,
                derivation.maximum_pubkeys,
                derivation.minimum_pubkeys,
                derivation.slow_rekey_timelock,
                derivation.rekey_increments,
            )
            _, launch_spend = generate_launch_conditions_and_coin_spend(
                fund_coin, construct_singleton_inner_puzzle(new_derivation.prefarm_info), uint64(1)
            )
            creation_bundle = SpendBundle([launch_spend], G2Element())
            announcement = Announcement(launcher_coin.name(), launch_spend.solution.to_program().get_tree_hash())
            fund_bundle: SpendBundle = (
                await wallet_client.create_signed_transaction(
                    [{"puzzle_hash": SINGLETON_LAUNCHER_HASH, "amount": 1}],
                    [fund_coin],
                    fee=uint64(fee),
                    coin_announcements=[announcement],
                )
            ).spend_bundle
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
        finally:
            node_client.close()
            wallet_client.close()
            await node_client.await_closed()
            await wallet_client.await_closed()

    asyncio.get_event_loop().run_until_complete(do_command())


@cli.command("update_config", short_help="Update an outdated config in a sync DB with a new config")
@click.option(
    "-c",
    "--configuration",
    help="The configuration file with which to initialize a sync database (default: ./Configuration (******).txt)",
    default=None,
)
@click.option(
    "-db",
    "--db-path",
    help="The file path to initialize/find the sync database at (default: ./sync (******).sqlite)",
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
                await sync_store.db_wrapper.begin_transaction()
                await sync_store.add_configuration(db_config, outdated)
                await sync_store.db_wrapper.commit_transaction()
                print("Configuration update successful")
        finally:
            await sync_store.db_connection.close()

    asyncio.get_event_loop().run_until_complete(do_command())


@cli.command("export_config", short_help="Export a copy of the current DB's config")
@click.option(
    "-f",
    "--filename",
    help="The file path to export the config to (default: ./Configuration (******).sqlite)",
    default=None,
)
@click.option(
    "-db",
    "--db-path",
    help="The file path to initialize/find the sync database at (default: ./sync (******).sqlite)",
    default="./",
    required=True,
)
def export_cmd(
    filename: Optional[str],
    db_path: str,
):
    async def do_command():
        sync_store: SyncStore = await load_db(db_path)
        try:
            try:
                configuration = await sync_store.get_configuration(False, block_outdated=False)
                puzzle_root = configuration.prefarm_info.puzzle_root
            except ValueError:
                configuration = await sync_store.get_configuration(True, block_outdated=False)
                puzzle_root = configuration.puzzle_root
            if filename is None:
                _filename = f"Configuration ({puzzle_root[0:3].hex()}).txt"
            else:
                _filename = filename
            with open(Path(_filename), "wb") as file:
                file.write(bytes(configuration))
            print(f"Config successfully exported to {_filename}")
        finally:
            await sync_store.db_connection.close()

    asyncio.get_event_loop().run_until_complete(do_command())


@cli.command("sync", short_help="Sync a singleton from an existing configuration")
@click.option(
    "-c",
    "--configuration",
    help="The configuration file with which to initialize a sync database (default: ./Configuration (******).txt)",
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
    "-np",
    "--node-rpc-port",
    help="Set the port where the Node is hosting the RPC interface. See the rpc_port under full_node in config.yaml",
    type=int,
    default=None,
)
def sync_cmd(
    configuration: Optional[str],
    db_path: str,
    node_rpc_port: Optional[int],
):
    # Start sync
    async def do_sync():
        try:
            node_client = await get_node_client(node_rpc_port)

            if configuration is not None:
                try:
                    db_config = load_root_derivation(configuration)
                    prefarm_info = db_config.prefarm_info
                except ValueError:
                    db_config = load_prefarm_info(configuration)
                    prefarm_info = db_config
                sync_store: SyncStore = await load_db(db_path, prefarm_info.launcher_id)
                await sync_store.db_wrapper.begin_transaction()
                await sync_store.add_configuration(db_config)
            else:
                sync_store: SyncStore = await load_db(db_path)
                prefarm_info = await sync_store.get_configuration(public=True, block_outdated=False)
                await sync_store.db_wrapper.begin_transaction()

            current_singleton: Optional[SingletonRecord] = await sync_store.get_latest_singleton()
            current_coin_record: Optional[CoinRecord] = None
            if current_singleton is None:
                launcher_coin = await node_client.get_coin_record_by_name(prefarm_info.launcher_id)
                current_coin_record = (await node_client.get_coin_records_by_parent_ids([prefarm_info.launcher_id]))[0]
                if construct_full_singleton(prefarm_info).get_tree_hash() != current_coin_record.coin.puzzle_hash:
                    raise ValueError("The specified config has the incorrect puzzle root")
                current_singleton = SingletonRecord(
                    current_coin_record.coin,
                    prefarm_info.puzzle_root,
                    LineageProof(parent_name=launcher_coin.coin.parent_coin_info, amount=launcher_coin.coin.amount),
                    current_coin_record.timestamp,
                    uint32(0),
                    None,
                    None,
                    None,
                    None,
                )
                await sync_store.add_singleton_record(current_singleton)
            if current_coin_record is None:
                current_coin_record = await node_client.get_coin_record_by_name(current_singleton.coin.name())

            p2_singleton_begin_sync: uint32 = current_coin_record.confirmed_block_index
            # Begin loop
            while True:
                latest_spend: Optional[CoinSpend] = await node_client.get_puzzle_and_solution(
                    current_coin_record.coin.name(), current_coin_record.spent_block_index
                )
                if latest_spend is None:
                    if current_singleton.puzzle_root != prefarm_info.puzzle_root:
                        outdated: bool = await sync_store.update_config_puzzle_root(current_singleton.puzzle_root)
                        if outdated:
                            print("Configuration is outdated, please update it with command <TODO>")
                    break

                # Fill in all of the information about the spent singleton
                latest_solution: Program = latest_spend.solution.to_program()
                spend_type: SpendType = get_spend_type_for_solution(latest_solution)
                await sync_store.add_singleton_record(
                    dataclasses.replace(
                        current_singleton,
                        puzzle_reveal=latest_spend.puzzle_reveal,
                        solution=latest_spend.solution,
                        spend_type=spend_type,
                        spending_pubkey=get_spending_pubkey_for_solution(latest_solution),
                    )
                )

                # Create the new singleton's record
                all_children: List[CoinRecord] = await node_client.get_coin_records_by_parent_ids(
                    [current_coin_record.coin.name()]
                )
                drop_coin: Optional[CoinRecord] = None
                potential_drop_coins = [cr for cr in all_children if cr.coin.amount % 2 == 0]
                if len(potential_drop_coins) > 0:
                    drop_coin = potential_drop_coins[0]
                next_coin_record = [cr for cr in all_children if cr.coin.amount % 2 == 1][0]
                if next_coin_record.coin.puzzle_hash == current_coin_record.coin.puzzle_hash:
                    next_puzzle_root: bytes32 = current_singleton.puzzle_root
                else:
                    next_puzzle_root = get_new_puzzle_root_from_solution(latest_solution)
                next_singleton = SingletonRecord(
                    next_coin_record.coin,
                    next_puzzle_root,
                    LineageProof(
                        current_coin_record.coin.parent_coin_info,
                        construct_singleton_inner_puzzle(
                            dataclasses.replace(prefarm_info, puzzle_root=current_singleton.puzzle_root)
                        ).get_tree_hash(),
                        current_coin_record.coin.amount,
                    ),
                    next_coin_record.timestamp,
                    uint32(current_singleton.generation + 1),
                    None,
                    None,
                    None,
                    None,
                )
                await sync_store.add_singleton_record(next_singleton)
                # Detect any drop coins and add records for them
                if drop_coin is not None:
                    if spend_type == SpendType.HANDLE_PAYMENT:
                        _, _, p2_ph = get_spend_params_for_ach_creation(latest_solution)
                        await sync_store.add_ach_record(
                            ACHRecord(
                                drop_coin.coin,
                                current_singleton.puzzle_root,
                                p2_ph,
                                drop_coin.timestamp,
                                None,
                                None,
                            )
                        )
                    elif spend_type == SpendType.START_REKEY:
                        timelock, new_root = get_spend_params_for_rekey_creation(latest_solution)
                        await sync_store.add_rekey_record(
                            RekeyRecord(
                                drop_coin.coin,
                                current_singleton.puzzle_root,
                                new_root,
                                timelock,
                                drop_coin.timestamp,
                                None,
                                None,
                            )
                        )
                # Loop with the next coin
                current_coin_record = next_coin_record
                current_singleton = next_singleton
            # Quickly request all of the p2_singletons
            p2_singleton_ph: bytes32 = construct_p2_singleton(prefarm_info.launcher_id).get_tree_hash()
            await sync_store.add_p2_singletons(
                [
                    cr.coin
                    for cr in (
                        await node_client.get_coin_records_by_puzzle_hashes(
                            [p2_singleton_ph],
                            include_spent_coins=False,
                            start_height=p2_singleton_begin_sync,
                        )
                    )
                ]
            )
            # Check the status of any drop coins
            ach_coins: List[ACHRecord] = await sync_store.get_ach_records(include_completed_coins=False)
            rekey_coins: List[ACHRecord] = await sync_store.get_rekey_records(include_completed_coins=False)
            ach_ids: List[bytes32] = [ach.coin.name() for ach in ach_coins]
            rekey_ids: List[bytes32] = [rekey.coin.name() for rekey in rekey_coins]
            all_drop_coin_records: List[CoinRecord] = await node_client.get_coin_records_by_names(
                [*ach_ids, *rekey_ids], include_spent_coins=True
            )
            all_spent_drop_coins: List[CoinRecord] = [cr for cr in all_drop_coin_records if cr.spent_block_index > 0]
            all_unspent_drop_coins: List[CoinRecord] = [cr for cr in all_drop_coin_records if cr.spent_block_index == 0]
            for spent_drop_coin in all_spent_drop_coins:
                if spent_drop_coin.coin.name() in ach_ids:
                    current_ach_record: ACHRecord = [
                        r for r in ach_coins if r.coin.name() == spent_drop_coin.coin.name()
                    ][0]
                    drop_coin_child: CoinRecord = (
                        await node_client.get_coin_records_by_parent_ids([spent_drop_coin.coin.name()])
                    )[0]
                    if (
                        drop_coin_child.coin.puzzle_hash
                        == construct_p2_singleton(prefarm_info.launcher_id).get_tree_hash()
                    ):
                        completed = False
                    else:
                        completed = True
                    await sync_store.add_ach_record(
                        dataclasses.replace(
                            current_ach_record,
                            spent_at_height=spent_drop_coin.spent_block_index,
                            completed=completed,
                        )
                    )
                else:
                    current_rekey_record: ACHRecord = [
                        r for r in rekey_coins if r.coin.name() == spent_drop_coin.coin.name()
                    ][0]
                    rekey_spend: Optional[CoinSpend] = await node_client.get_puzzle_and_solution(
                        spent_drop_coin.coin.name(), spent_drop_coin.spent_block_index
                    )
                    assert rekey_spend is not None
                    completed = was_rekey_completed(rekey_spend.solution.to_program())
                    await sync_store.add_rekey_record(
                        dataclasses.replace(
                            current_rekey_record,
                            spent_at_height=spent_drop_coin.spent_block_index,
                            completed=completed,
                        )
                    )
            for outdated_rekey in [
                r
                for r in rekey_coins
                if r.coin.name() in (cr.coin.name() for cr in all_unspent_drop_coins)
                and r.from_root != current_singleton.puzzle_root
            ]:
                await sync_store.add_rekey_record(dataclasses.replace(outdated_rekey, completed=False))
        except Exception as e:
            await sync_store.db_connection.close()
            raise e
        finally:
            node_client.close()
            await node_client.await_closed()

        await sync_store.db_wrapper.commit_transaction()
        await sync_store.db_connection.close()

    asyncio.get_event_loop().run_until_complete(do_sync())


@cli.command("p2_address", short_help="Print the address to pay to the singleton")
@click.option(
    "-db",
    "--db-path",
    help="The file path to the sync DB (default: ./sync (******).sqlite)",
    default="./",
    required=True,
)
@click.option(
    "-p",
    "--prefix",
    help="The prefix to use when encoding the address",
    default="xch",
    show_default=True,
)
def address_cmd(db_path: str, prefix: str):
    async def do_command():
        sync_store: SyncStore = await load_db(db_path)
        try:
            prefarm_info = await sync_store.get_configuration(True, block_outdated=False)
            print(encode_puzzle_hash(construct_p2_singleton(prefarm_info.launcher_id).get_tree_hash(), prefix))
        finally:
            await sync_store.db_connection.close()

    asyncio.get_event_loop().run_until_complete(do_command())


@cli.command("push_tx", short_help="Push a signed spend bundle to the network")
@click.option(
    "-b",
    "--spend-bundle",
    help="The signed spend bundle",
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
@click.option(
    "-m",
    "--fee",
    help="The fee to attach to this spend (in mojos)",
    type=int,
    default=0,
)
def push_cmd(
    spend_bundle: str,
    wallet_rpc_port: Optional[int],
    fingerprint: Optional[int],
    node_rpc_port: Optional[int],
    fee: int,
):
    async def do_command():
        try:
            node_client, wallet_client = await get_wallet_and_node_clients(node_rpc_port, wallet_rpc_port, fingerprint)

            try:
                push_bundle = SpendBundle.from_bytes(bytes.fromhex(spend_bundle))
            except Exception:
                print("Spend bundle cannot be recognized.  Please make sure this spend bundle is signed and try again.")
                return

            spends: List[SpendBundle] = [push_bundle]

            if fee > 0:
                fee_announcement: Optional[Announcement] = None
                for coin_spend in push_bundle.coin_spends:
                    _, conditions = coin_spend.puzzle_reveal.run_with_cost(INFINITE_COST, coin_spend.solution)
                    for condition in conditions.as_python():
                        if condition[0] == int_to_bytes(60):  # CREATE_COIN_ANNOUNCEMENT
                            fee_announcement = Announcement(coin_spend.coin.name(), condition[1])
                            break
                if fee_announcement is None:
                    print("Cannot find a way to link fee to this transaction. Please specify 0 fee and try again.")
                    return
                else:
                    spends.append(
                        (
                            await wallet_client.create_signed_transaction(
                                [
                                    {"puzzle_hash": bytes32([0] * 32), "amount": 0}
                                ],  # This is dust but the RPC requires it
                                fee=uint64(fee),
                                coin_announcements=[fee_announcement],
                            )
                        ).spend_bundle
                    )

            result = await node_client.push_tx(SpendBundle.aggregate(spends))
            print(result)

        finally:
            node_client.close()
            wallet_client.close()
            await node_client.await_closed()
            await wallet_client.await_closed()

    asyncio.get_event_loop().run_until_complete(do_command())


@cli.command("payment", short_help="Absorb/Withdraw money into/from the singleton")
@click.option(
    "-db",
    "--db-path",
    help="The file path to the sync DB (default: ./sync (******).sqlite)",
    default="./",
    required=True,
)
@click.option(
    "-f",
    "--filename",
    help="The filepath to dump the spend bundle into",
    default=None,
)
@click.option(
    "-pks",
    "--pubkeys",
    help="A comma separated list of pubkeys that will be signing this spend.",
    required=True,
)
@click.option(
    "-a",
    "--amount",
    help="The outgoing amount (in mojos) to pay.  Can be zero to make no payment.",
    default=0,
    show_default=True,
)
@click.option(
    "-t",
    "--recipient-address",
    help="The address that can claim the money after the clawback period is over (must be supplied if amount is > 0)",
)
@click.option(
    "-ap",
    "--absorb-available-payments",
    help="Look for any outstanding payments to the singleton and claim them while doing this spend (adds tx cost)",
    is_flag=True,
)
@click.option(
    "-mc",
    "--maximum-extra-cost",
    help="The maximum extra tx cost to be taken on while absorbing payments (as an estimated percentage)",
    default=50,
    show_default=True,
)
@click.option(
    "-at",
    "--amount-threshold",
    help="The minimum amount required of a payment in order for it to be absorbed",
    default=1000000000000,
    show_default=True,
)
def payments_cmd(
    db_path: str,
    pubkeys: str,
    amount: int,
    recipient_address: Optional[str],
    absorb_available_payments: bool,
    maximum_extra_cost: Optional[int],
    amount_threshold: int,
    filename: Optional[str],
):
    # Check to make sure we've been given a correct set of parameters
    if amount > 0 and recipient_address is None:
        raise ValueError("You must specify a recipient address for outgoing payments")
    if amount % 2 == 1:
        raise ValueError("You can not make payments of an odd amount")

    async def do_command():
        sync_store: SyncStore = await load_db(db_path)
        try:
            derivation = await sync_store.get_configuration(False, block_outdated=True)
            # Collect some relevant information
            current_singleton: Optional[SingletonRecord] = await sync_store.get_latest_singleton()
            if current_singleton is None:
                raise RuntimeError("No singleton is found for this configuration.  Try `cic sync` then try again.")
            pubkey_list: List[G1Element] = [G1Element.from_bytes(bytes.fromhex(pk)) for pk in pubkeys.split(",")]
            clawforward_ph: bytes32 = decode_puzzle_hash(recipient_address)
            fee_conditions: List[Program] = [Program.to([60, b"$"])]

            # Get any p2_singletons to spend
            max_num: Optional[uint32] = (
                uint32(math.floor(maximum_extra_cost / 10)) if maximum_extra_cost is not None else None
            )
            p2_singletons: List[Coin] = await sync_store.get_p2_singletons(amount_threshold, max_num)
            if sum(c.amount for c in p2_singletons) % 2 == 1:
                smallest_coin: Coin = sorted(p2_singletons, key=attrgetter("amount"))[0]
                p2_singletons = [c for c in p2_singletons if c.name() != smallest_coin.name()]

            # Check that this payment will be legal
            withdrawn_amount: int = derivation.prefarm_info.starting_amount - (
                current_singleton.coin.amount - (amount - sum(c.amount for c in p2_singletons))
            )
            time_to_use: int = math.ceil(withdrawn_amount / derivation.prefarm_info.mojos_per_second)
            if time_to_use > int(time.time() - 600):  # subtract 10 minutes to allow for weird block timestamps
                raise ValueError("That much cannot be withdrawn at this time.")

            # Get the spend bundle
            singleton_bundle, data_to_sign = get_withdrawal_spend_info(
                current_singleton.coin,
                pubkey_list,
                derivation,
                current_singleton.lineage_proof,
                time_to_use,
                amount,
                clawforward_ph,
                p2_singletons_to_claim=p2_singletons,
                additional_conditions=fee_conditions,
            )

            # Cast everything into HSM types
            as_bls_pubkey_list = [BLSPublicKey(pk) for pk in pubkey_list]
            agg_pk = sum(as_bls_pubkey_list, start=BLSPublicKey.zero())
            synth_sk = BLSSecretExponent(
                PrivateKey.from_bytes(
                    calculate_synthetic_offset(agg_pk, DEFAULT_HIDDEN_PUZZLE_HASH).to_bytes(32, "big")
                )
            )
            coin_spends = [
                HSMCoinSpend(cs.coin, cs.puzzle_reveal.to_program(), cs.solution.to_program())
                for cs in singleton_bundle.coin_spends
            ]
            unsigned_spend = UnsignedSpend(
                coin_spends,
                [SumHint(as_bls_pubkey_list, synth_sk)],
                [],
                get_additional_data(),
            )

            # Print the result
            if filename is not None:
                with open(filename, "w") as file:
                    file.write(bytes(unsigned_spend).hex())
            else:
                print(bytes(unsigned_spend).hex())
        finally:
            await sync_store.db_connection.close()

    asyncio.get_event_loop().run_until_complete(do_command())


@cli.command("start_rekey", short_help="Rekey the singleton to a new set of keys/options")
@click.option(
    "-db",
    "--db-path",
    help="The file path to the sync DB (default: ./sync (******).sqlite)",
    default="./",
    required=True,
)
@click.option(
    "-f",
    "--filename",
    help="The filepath to dump the spend bundle into",
    default=None,
)
@click.option(
    "-pks",
    "--pubkeys",
    help="A comma separated list of pubkeys that will be signing this spend.",
    required=True,
)
@click.option(
    "-new",
    "--new-configuration",
    help="The configuration you would like to rekey the singleton to",
    required=True,
)
def start_rekey_cmd(
    db_path: str,
    pubkeys: str,
    new_configuration: str,
    filename: Optional[str],
):
    async def do_command():
        sync_store: SyncStore = await load_db(db_path)

        try:
            derivation = await sync_store.get_configuration(False, block_outdated=True)
            new_derivation: RootDerivation = load_root_derivation(new_configuration)

            # Quick sanity check that everything except the puzzle root is the same
            if not derivation.prefarm_info.is_valid_update(new_derivation.prefarm_info):
                raise ValueError(
                    "This configuration has more changed than the keys."
                    "Please derive a configuration with the same values for everything except key-related info."
                )

            # Collect some relevant information
            current_singleton: Optional[SingletonRecord] = await sync_store.get_latest_singleton()
            if current_singleton is None:
                raise RuntimeError("No singleton is found for this configuration.  Try `cic sync` then try again.")
            pubkey_list: List[G1Element] = [G1Element.from_bytes(bytes.fromhex(pk)) for pk in pubkeys.split(",")]
            fee_conditions: List[Program] = [Program.to([60, b"$"])]

            # Get the spend bundle
            singleton_bundle, data_to_sign = get_rekey_spend_info(
                current_singleton.coin,
                pubkey_list,
                derivation,
                current_singleton.lineage_proof,
                new_derivation,
                fee_conditions,
            )

            # Cast everything into HSM types
            as_bls_pubkey_list = [BLSPublicKey(pk) for pk in pubkey_list]
            agg_pk = sum(as_bls_pubkey_list, start=BLSPublicKey.zero())
            synth_sk = BLSSecretExponent(
                PrivateKey.from_bytes(
                    calculate_synthetic_offset(agg_pk, DEFAULT_HIDDEN_PUZZLE_HASH).to_bytes(32, "big")
                )
            )
            coin_spends = [
                HSMCoinSpend(cs.coin, cs.puzzle_reveal.to_program(), cs.solution.to_program())
                for cs in singleton_bundle.coin_spends
            ]
            unsigned_spend = UnsignedSpend(
                coin_spends,
                [SumHint(as_bls_pubkey_list, synth_sk)],
                [],
                get_additional_data(),
            )

            # Print the result
            if filename is not None:
                with open(filename, "w") as file:
                    file.write(bytes(unsigned_spend).hex())
            print(bytes(unsigned_spend).hex())
        finally:
            await sync_store.db_connection.close()

    asyncio.get_event_loop().run_until_complete(do_command())


@cli.command("clawback", short_help="Clawback a withdrawal or rekey attempt (will be prompted which one)")
@click.option(
    "-db",
    "--db-path",
    help="The file path to the sync DB (default: ./sync (******).sqlite)",
    default="./",
    required=True,
)
@click.option(
    "-f",
    "--filename",
    help="The filepath to dump the spend bundle into",
    default=None,
)
@click.option(
    "-pks",
    "--pubkeys",
    help="A comma separated list of pubkeys that will be signing this spend.",
    required=True,
)
def clawback_cmd(
    db_path: str,
    pubkeys: str,
    filename: Optional[str],
):
    async def do_command():
        sync_store: SyncStore = await load_db(db_path)

        try:
            derivation = await sync_store.get_configuration(False, block_outdated=True)

            achs: List[ACHRecord] = await sync_store.get_ach_records(include_completed_coins=False)
            rekeys: List[RekeyRecord] = await sync_store.get_rekey_records(include_completed_coins=False)

            # Prompt the user for the action to cancel
            if len(achs) == 0 and len(rekeys) == 0:
                print("No actions outstanding")
                return
            print("Which actions would you like to cancel?:")
            print()
            index: int = 1
            for ach in achs:
                print(f"{index}) PAYMENT to {encode_puzzle_hash(ach.p2_ph, 'xch')} of amount {ach.coin.amount}")
                index += 1
            for rekey in rekeys:
                print(f"{index}) REKEY from {rekey.from_root} to {rekey.to_root}")
                index += 1
            selected_action = int(input("(Enter index of action to cancel): "))
            if selected_action not in range(1, index):
                print("Invalid index specified.")
                return

            # Construct the spend for the selected index
            pubkey_list: List[G1Element] = [G1Element.from_bytes(bytes.fromhex(pk)) for pk in pubkeys.split(",")]
            fee_conditions: List[Program] = [Program.to([60, b"$"])]
            if selected_action <= len(achs):
                ach_record: ACHRecord = achs[selected_action - 1]

                # Validate we have enough keys
                if len(pubkey_list) != derivation.required_pubkeys:
                    print("Incorrect number of keys to claw back selected payment")
                    return

                # Get the spend bundle
                clawback_bundle, data_to_sign = get_ach_clawback_spend_info(
                    ach_record.coin,
                    pubkey_list,
                    derivation,
                    ach_record.p2_ph,
                    fee_conditions,
                )
            else:
                rekey_record: RekeyRecord = rekeys[selected_action - len(achs) - 1]

                # Validate we have enough keys
                timelock: uint64 = rekey_record.timelock
                required_pubkeys: Optional[int] = None
                if timelock == derivation.rekey_increments:
                    required_pubkeys = derivation.required_pubkeys
                else:
                    for i in range(derivation.minimum_pubkeys, derivation.required_pubkeys):
                        if timelock == derivation.slow_rekey_timelock + (
                            derivation.rekey_increments * (derivation.required_pubkeys - i)
                        ):
                            required_pubkeys = i
                            break
                if required_pubkeys is None or len(pubkey_list) != required_pubkeys:
                    print("Incorrect number of keys to claw back selected rekey")
                    return

                # Get the spend bundle
                clawback_bundle, data_to_sign = get_rekey_clawback_spend_info(
                    rekey_record.coin,
                    pubkey_list,
                    derivation,
                    rekey_record.timelock,
                    dataclasses.replace(
                        derivation,
                        prefarm_info=dataclasses.replace(derivation.prefarm_info, puzzle_root=rekey_record.to_root),
                    ),
                    fee_conditions,
                )

            as_bls_pubkey_list = [BLSPublicKey(pk) for pk in pubkey_list]
            agg_pk = sum(as_bls_pubkey_list, start=BLSPublicKey.zero())
            synth_sk = BLSSecretExponent(
                PrivateKey.from_bytes(
                    calculate_synthetic_offset(agg_pk, DEFAULT_HIDDEN_PUZZLE_HASH).to_bytes(32, "big")
                )
            )
            coin_spends = [
                HSMCoinSpend(cs.coin, cs.puzzle_reveal.to_program(), cs.solution.to_program())
                for cs in clawback_bundle.coin_spends
            ]
            unsigned_spend = UnsignedSpend(
                coin_spends,
                [SumHint(as_bls_pubkey_list, synth_sk)],
                [],
                get_additional_data(),
            )
            if filename is not None:
                with open(filename, "w") as file:
                    file.write(bytes(unsigned_spend).hex())
            print(bytes(unsigned_spend).hex())
        finally:
            await sync_store.db_connection.close()

    asyncio.get_event_loop().run_until_complete(do_command())


@cli.command("complete", short_help="Complete a withdrawal or rekey attempt (will be prompted which one)")
@click.option(
    "-db",
    "--db-path",
    help="The file path to the sync DB (default: ./sync (******).sqlite)",
    default="./",
    required=True,
)
@click.option(
    "-f",
    "--filename",
    help="The filepath to dump the spend bundle into",
    default=None,
)
def complete_cmd(
    db_path: str,
    filename: Optional[str],
):
    async def do_command():
        sync_store: SyncStore = await load_db(db_path)

        try:
            derivation = await sync_store.get_configuration(False, block_outdated=True)

            achs: List[ACHRecord] = await sync_store.get_ach_records(include_completed_coins=False)
            rekeys: List[RekeyRecord] = await sync_store.get_rekey_records(include_completed_coins=False)

            # Prompt the user for the action to complete
            if len(achs) == 0 and len(rekeys) == 0:
                print("No actions outstanding")
                return
            print("Which actions would you like to complete?:")
            print()
            index: int = 1
            for ach in achs:
                if ach.confirmed_at_time + derivation.prefarm_info.payment_clawback_period < time.time():
                    prefix = f"{index})"
                    index += 1
                else:
                    prefix = "-)"
                print(f"{prefix} PAYMENT to {encode_puzzle_hash(ach.p2_ph, 'xch')} of amount {ach.coin.amount}")

            for rekey in rekeys:
                if rekey.confirmed_at_time + derivation.prefarm_info.rekey_clawback_period < time.time():
                    prefix = f"{index})"
                    index += 1
                else:
                    prefix = "-)"
                print(f"{prefix} REKEY from {rekey.from_root} to {rekey.to_root}")
            if index == 1:
                print("No actions can be completed at this time.")
                return
            selected_action = int(input("(Enter index of action to complete): "))
            if selected_action not in range(1, index):
                print("Invalid index specified.")
                return

            # Construct the spend for the selected index
            if selected_action <= len(achs):
                ach_record: ACHRecord = achs[selected_action - 1]
                # Get the spend bundle
                completion_bundle = get_ach_clawforward_spend_bundle(
                    ach_record.coin,
                    derivation,
                    ach_record.p2_ph,
                )
            else:
                rekey_record: RekeyRecord = rekeys[selected_action - len(achs) - 1]
                current_singleton: Optional[SingletonRecord] = await sync_store.get_latest_singleton()
                if current_singleton is None:
                    raise RuntimeError("No singleton is found for this configuration.  Try `cic sync` then try again.")
                parent_singleton: Optional[SingletonRecord] = await sync_store.get_singleton_record(
                    rekey_record.coin.parent_coin_info
                )
                if parent_singleton is None:
                    raise RuntimeError("Bad sync information. Please try a resync.")

                # Get the spend bundle
                completion_bundle = get_rekey_completion_spend(
                    current_singleton.coin,
                    rekey_record.coin,
                    derivation.pubkey_list[0 : derivation.required_pubkeys],
                    derivation,
                    current_singleton.lineage_proof,
                    LineageProof(
                        parent_singleton.coin.parent_coin_info,
                        construct_singleton_inner_puzzle(derivation.prefarm_info).get_tree_hash(),
                        parent_singleton.coin.amount,
                    ),
                    dataclasses.replace(
                        derivation,
                        prefarm_info=dataclasses.replace(derivation.prefarm_info, puzzle_root=rekey_record.to_root),
                    ),
                )

            if filename is not None:
                with open(filename, "w") as file:
                    file.write(bytes(completion_bundle).hex())
            print(bytes(completion_bundle).hex())
        finally:
            await sync_store.db_connection.close()

    asyncio.get_event_loop().run_until_complete(do_command())


@cli.command("increase_security_level", short_help="Initiate an increase of the number of keys required for withdrawal")
@click.option(
    "-db",
    "--db-path",
    help="The file path to the sync DB (default: ./sync (******).sqlite)",
    default="./",
    required=True,
)
@click.option(
    "-pks",
    "--pubkeys",
    help="A comma separated list of pubkeys that will be signing this spend.",
    required=True,
)
@click.option(
    "-f",
    "--filename",
    help="The filepath to dump the spend bundle into",
    default=None,
)
def increase_cmd(
    db_path: str,
    pubkeys: str,
    filename: Optional[str],
):
    async def do_command():
        sync_store: SyncStore = await load_db(db_path)
        try:
            derivation = await sync_store.get_configuration(False, block_outdated=True)

            current_singleton: Optional[SingletonRecord] = await sync_store.get_latest_singleton()
            if current_singleton is None:
                raise RuntimeError("No singleton is found for this configuration.  Try `cic sync` then try again.")
            pubkey_list: List[G1Element] = [G1Element.from_bytes(bytes.fromhex(pk)) for pk in pubkeys.split(",")]
            fee_conditions: List[Program] = [Program.to([60, b"$"])]

            # Validate we have enough pubkeys
            if len(pubkey_list) < derivation.required_pubkeys:
                print("Not enough keys to increase the security level")
                return

            # Create the spend
            lock_bundle, data_to_sign = get_rekey_spend_info(
                current_singleton.coin,
                pubkey_list,
                derivation,
                current_singleton.lineage_proof,
                additional_conditions=fee_conditions,
            )

            as_bls_pubkey_list = [BLSPublicKey(pk) for pk in pubkey_list]
            coin_spends = [
                HSMCoinSpend(cs.coin, cs.puzzle_reveal.to_program(), cs.solution.to_program())
                for cs in lock_bundle.coin_spends
            ]
            unsigned_spend = UnsignedSpend(
                coin_spends,
                [SumHint(as_bls_pubkey_list, BLSSecretExponent.zero())],
                [],
                get_additional_data(),
            )

            if filename is not None:
                with open(filename, "w") as file:
                    file.write(bytes(unsigned_spend).hex())
            print(bytes(unsigned_spend).hex())
        finally:
            await sync_store.db_connection.close()

    asyncio.get_event_loop().run_until_complete(do_command())


def main() -> None:
    cli()  # pylint: disable=no-value-for-parameter


if __name__ == "__main__":
    main()
