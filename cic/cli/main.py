import asyncio
import click
import dataclasses
import os

from blspy import G1Element, G2Element
from pathlib import Path
from typing import Optional

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.announcement import Announcement
from chia.types.coin_record import CoinRecord
from chia.types.spend_bundle import SpendBundle
from chia.util.ints import uint32, uint64
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzles.singleton_top_layer import SINGLETON_LAUNCHER_HASH

from cic import __version__
from cic.cli.clients import get_wallet_and_node_clients
from cic.cli.singleton_record import SingletonRecord
from cic.cli.sync_store import SyncStore
from cic.drivers.prefarm_info import PrefarmInfo
from cic.drivers.prefarm import (
    construct_full_singleton,
    construct_singleton_inner_puzzle,
    get_new_puzzle_root_from_solution,
)
from cic.drivers.puzzle_root_construction import RootDerivation, calculate_puzzle_root
from cic.drivers.singleton import generate_launch_conditions_and_coin_spend

CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])


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
    "-f", "--filepath", help="The filepath at which to create the configuration file", default=".", required=True
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
    "-rt",
    "--rekey-timelock",
    help="The amount of time where nothing has happened before a standard rekey can be initiated (in seconds)",
    required=True,
)
@click.option("-sp", "--slow-penalty", help="The time penalty for performing a slow rekey (in seconds)", required=True)
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
    filepath: str,
    date: int,
    rate: int,
    amount: int,
    withdrawal_timelock: int,
    rekey_timelock: int,
    slow_penalty: int,
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
        uint64(slow_penalty),
        uint64(rekey_timelock),
    )

    path = Path(filepath)
    if path.is_dir():
        path = path.joinpath("Configuration (needs derivation).txt")

    with open(path, "wb") as file:
        file.write(bytes(prefarm_info))


@cli.command("derive_root", short_help="Take an existing configuration and pubkey set to derive a puzzle root")
@click.option(
    "-c",
    "--configuration",
    help="The configuration file with which to derive the root",
    default="./Configuration (needs derivation).txt",
    required=True,
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
def derive_cmd(
    configuration: str,
    pubkeys: str,
    initial_lock_level: int,
    minimum_pks: int,
    maximum_lock_level: Optional[int] = None,
):
    with open(Path(configuration), "rb") as file:
        prefarm_info = PrefarmInfo.from_bytes(file.read())
    pubkeys: List[G1Element] = [G1Element.from_bytes(bytes.fromhex(pk)) for pk in pubkeys.split(",")]

    derivation: RootDerivation = calculate_puzzle_root(
        prefarm_info,
        pubkeys,
        uint32(initial_lock_level),
        uint32(len(pubkeys) if maximum_lock_level is None else maximum_lock_level),
        uint32(minimum_pks),
    )

    with open(Path(configuration), "wb") as file:
        file.write(bytes(derivation))
    if "needs derivation" in configuration:
        os.rename(Path(configuration), Path("awaiting launch".join(configuration.split("needs derivation"))))


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
@click.option("-m", "--fee", help="Fee to use for the launch transaction (in mojos)", default=0)
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
            new_derivation: RootDerivation = dataclasses.replace(
                derivation, prefarm_info=dataclasses.replace(derivation.prefarm_info, launcher_id=launcher_coin.name())
            )
            _, launch_spend = generate_launch_conditions_and_coin_spend(
                fund_coin, construct_singleton_inner_puzzle(new_derivation.prefarm_info), uint64(1)
            )
            creation_bundle = SpendBundle([launch_spend], G2Element())
            announcement = Announcement(launcher_coin.name(), launch_spend.solution.to_program().get_tree_hash())
            # TODO: A malicious farmer can steal our mojo here if they want (needs support for assertions in RPC)
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

            path = Path(db_path)
            if path.is_dir():
                path = path.joinpath(f"sync ({launcher_coin.name()[0:3].hex()}).sqlite")
            sync_store = await SyncStore.create(path)
            try:
                await sync_store.add_singleton_record(
                    SingletonRecord(
                        Coin(
                            launcher_coin.name(),
                            construct_full_singleton(new_derivation.prefarm_info).get_tree_hash(),
                            uint64(1),
                        ),
                        new_derivation.prefarm_info.puzzle_root,
                        LineageProof(parent_name=launcher_coin.parent_coin_info, amount=uint64(1)),
                        uint32(0),
                        None,
                        None,
                        None,
                        None,
                    )
                )
                await sync_store.db_connection.commit()
            finally:
                await sync_store.db_connection.close()

            with open(Path(configuration), "wb") as file:
                file.write(bytes(new_derivation))
            if "awaiting launch" in configuration:
                os.rename(
                    Path(configuration),
                    Path(launcher_coin.name()[0:3].hex().join(configuration.split("awaiting launch"))),
                )
        finally:
            node_client.close()
            await node_client.await_closed()

    asyncio.get_event_loop().run_until_complete(do_command())


@cli.command("sync", short_help="Sync a singleton from an existing configuration")
@click.option(
    "-c",
    "--configuration",
    help="The configuration file for the singleton to sync (default: ./Configuration (******).txt)",
    default=None,
    required=True,
)
@click.option(
    "-db",
    "--db-path",
    help="The file path to initialize the sync database at (default: ./sync (******).sqlite)",
    default="./",
    required=True,
)
@click.option(
    "-u",
    "--auto-update-config",
    help="Automatically update the synced config with the synced information (only works for observers)",
    is_flag=True,
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
    auto_update_config: bool,
    node_rpc_port: Optional[int],
):
    # Load the configuration (can be either public or private config)
    if configuration is None:
        path: Path = next(Path("./").glob("Configuration (*).txt"))
    else:
        path = Path(configuration)
    with open(Path(configuration), "rb") as file:
        file_bytes = file.read()
        try:
            prefarm_info = PrefarmInfo.from_bytes(file_bytes)
        except AssertionError:
            try:
                prefarm_info = RootDerivation.from_bytes(file_bytes).prefarm_info
                if auto_update_config:
                    raise ValueError("Cannot automatically update the config for non-observers")
            except AssertionError:
                raise ValueError("The configuration specified is not a recognizable format")

    # Start sync
    async def do_sync():
        node_client, wallet_client = await get_wallet_and_node_clients(node_rpc_port, None, None)

        path = Path(db_path)
        if path.is_dir():
            path = path.joinpath(f"sync ({prefarm_info.launcher_id[0:3].hex()}).sqlite")
        sync_store = await SyncStore.create(path)
        await sync_store.db_wrapper.begin_transaction()

        try:
            current_singleton: Optional[SingletonRecord] = await sync_store.get_latest_singleton()
            current_coin_record: Optional[CoinRecord] = None
            if current_singleton is None:
                current_coin_record = await node_client.get_coin_record_by_name(prefarm_info.launcher_id)
                if construct_full_singleton(prefarm_info).get_tree_hash() != current_coin_record.coin.puzzle_hash:
                    raise ValueError("The specified config has the incorrect puzzle root")
                current_singleton = SingletonRecord(
                    current_coin_record.coin,
                    prefarm_info.puzzle_root,
                    LineageProof(
                        parent_name=current_coin_record.coin.parent_coin_info, amount=current_coin_record.coin.amount
                    ),
                    uint32(0),
                    None,
                    None,
                    None,
                    None,
                )
                await sync_store.add_singleton_record(current_singleton)
            if current_coin_record is None:
                current_coin_record = await node_client.get_coin_record_by_name(current_singleton.coin.name())

            # Begin loop
            while True:
                latest_spend: Optional[CoinSpend] = await node_client.get_puzzle_and_solution(
                    current_coin_record.coin.name(), current_coin_record.spent_block_index
                )
                if latest_spend is None:
                    break
                latest_solution: Program = latest_spend.solution.to_program()
                await sync_store.add_singleton_record(dataclasses.replace(current_singleton,
                    puzzle_reveal=latest_spend.puzzle_reveal,
                    solution=latest_spend.solution,
                    spend_type=get_spend_type_for_solution(latest_solution),
                    spending_pubkey=get_spending_pubkey_for_solution(latest_solution),
                ))
                next_coin_record = [
                    cr
                    for cr in (await node_client.get_coin_records_by_parent_ids([current_coin_record.coin.name()]))
                    if cr.coin.amount % 2 == 1
                ][0]
                if next_coin_record.coin.puzzle_hash == current_coin_record.coin.puzzle_hash:
                    next_puzzle_root: bytes32 = current_singleton.puzzle_root
                else:
                    next_puzzle_root = get_new_puzzle_root_from_solution(latest_solution)
                next_singleton = SingletonRecord(
                    next_coin_record.coin,
                    next_puzzle_root,
                    LineageProof(
                        current_coin_record.coin.parent_coin_info,
                        construct_prefarm_inner_puzzle(current_singleton.puzzle_root).get_tree_hash(),
                        current_coin_record.coin.amount,
                    ),
                    uint32(current_singleton.generation + 1),
                    None,
                    None,
                    None,
                    None,
                )
                await sync_store.add_singleton_record(next_singleton)
                current_coin_record = next_coin_record
                current_singleton = next_singleton
        except Exception as e:
            await sync_store.db_connection.close()
            raise e
        finally:
            node_client.close()
            await node_client.await_closed()

        await sync_store.db_wrapper.commit_transaction()
        await sync_store.db_connection.close()

    asyncio.get_event_loop().run_until_complete(do_sync())


def main() -> None:
    cli()  # pylint: disable=no-value-for-parameter


if __name__ == "__main__":
    main()
