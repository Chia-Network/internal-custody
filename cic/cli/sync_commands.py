import asyncio
import dataclasses
import json
import time

from datetime import datetime
from typing import Dict, List, Optional, Union

import click

from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import CoinSpend
from chia_rs import CoinRecord, G1Element
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint32
from chia.util.bech32m import encode_puzzle_hash
from chia.wallet.lineage_proof import LineageProof

from cic.cli.common import get_node_client, load_db, load_prefarm_info, load_root_derivation
from cic.cli.record_types import SingletonRecord, ACHRecord, RekeyRecord
from cic.cli.sync_store import SyncStore
from cic.drivers.prefarm import (
    SpendType,
    construct_full_singleton,
    construct_singleton_inner_puzzle,
    get_puzzle_root_from_puzzle,
    get_new_puzzle_root_from_solution,
    get_spend_type_for_solution,
    get_spending_pubkey_for_solution,
    get_spending_pubkey_for_drop_coin,
    get_spend_params_for_ach_creation,
    get_spend_params_for_rekey_creation,
    was_rekey_completed,
)
from cic.drivers.singleton import construct_p2_singleton

from chia_base.bls12_381 import BLSPublicKey


@click.command("sync", short_help="Sync a singleton from an existing configuration")
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
@click.option(
    "-s",
    "--show",
    help="Show a summary of the singleton after sync is complete",
    is_flag=True,
)
def sync_cmd(
    configuration: Optional[str],
    db_path: str,
    node_rpc_port: Optional[int],
    show: bool,
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
                # await sync_store.db_wrapper.begin_transaction()
                await sync_store.add_configuration(db_config)
            else:
                sync_store: SyncStore = await load_db(db_path)
                prefarm_info = await sync_store.get_configuration(public=True, block_outdated=False)
                # await sync_store.db_wrapper.begin_transaction()

            current_singleton: Optional[SingletonRecord] = await sync_store.get_latest_singleton()
            current_coin_record: Optional[CoinRecord] = None
            if current_singleton is None:
                launcher_coin = await node_client.get_coin_record_by_name(prefarm_info.launcher_id)
                if launcher_coin is None:
                    raise ValueError("The singleton has not been launched yet")
                current_coin_record = (await node_client.get_coin_records_by_parent_ids([prefarm_info.launcher_id]))[0]
                if current_coin_record.spent_block_index == 0:
                    if construct_full_singleton(prefarm_info).get_tree_hash() != current_coin_record.coin.puzzle_hash:
                        raise ValueError("The specified config has the incorrect puzzle root")
                    else:
                        puzzle_root = prefarm_info.puzzle_root
                else:
                    initial_spend = await node_client.get_puzzle_and_solution(
                        current_coin_record.coin.name(), current_coin_record.spent_block_index
                    )
                    puzzle_root = get_puzzle_root_from_puzzle(Program.from_bytes(bytes(initial_spend.puzzle_reveal)))
                current_singleton = SingletonRecord(
                    current_coin_record.coin,
                    puzzle_root,
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
                            print("Configuration is outdated, please update it with command cic update_config")
                    break

                # Fill in all of the information about the spent singleton
                latest_solution: Program = Program.from_bytes(bytes(latest_spend.solution))
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
                    [current_coin_record.coin.name()], include_spent_coins=True
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
                                None,
                            )
                        )
                # Loop with the next coin
                current_coin_record = next_coin_record
                current_singleton = next_singleton
            # Mark any p2_singletons spent
            i: int = 0
            while True:
                unspent_p2_singletons: List[bytes32] = [
                    c.name() for c in (await sync_store.get_p2_singletons(start_end=(i, i + 100)))
                ]
                if unspent_p2_singletons == []:
                    break
                p2_singleton_records = await node_client.get_coin_records_by_names(
                    unspent_p2_singletons,
                    include_spent_coins=True,
                )
                for p2_singleton in p2_singleton_records:
                    if p2_singleton.spent_block_index > 0:
                        await sync_store.set_p2_singleton_spent(p2_singleton.coin.name())
                i += 100
            # Quickly request all of the new p2_singletons
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
                        ach_spend: Optional[CoinSpend] = await node_client.get_puzzle_and_solution(
                            spent_drop_coin.coin.name(), spent_drop_coin.spent_block_index
                        )
                        assert ach_spend is not None
                        spending_pubkey: Optional[G1Element] = get_spending_pubkey_for_drop_coin(
                            Program.from_bytes(bytes(ach_spend.solution))
                        )
                    else:
                        completed = True
                        spending_pubkey = None
                    await sync_store.add_ach_record(
                        dataclasses.replace(
                            current_ach_record,
                            spent_at_height=spent_drop_coin.spent_block_index,
                            completed=completed,
                            clawback_pubkey=spending_pubkey,
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
                    completed = was_rekey_completed(Program.from_bytes(bytes(rekey_spend.solution)))
                    if completed:
                        spending_pubkey = None
                    else:
                        spending_pubkey = get_spending_pubkey_for_drop_coin(
                            Program.from_bytes(bytes(rekey_spend.solution))
                        )
                    await sync_store.add_rekey_record(
                        dataclasses.replace(
                            current_rekey_record,
                            spent_at_height=spent_drop_coin.spent_block_index,
                            completed=completed,
                            clawback_pubkey=spending_pubkey,
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
            await sync_store.db_wrapper.close()
            print(str(e))
            return
        finally:
            node_client.close()
            await node_client.await_closed()

        # await sync_store.db_wrapper.commit_transaction()
        await sync_store.db_wrapper.close()

    asyncio.get_event_loop().run_until_complete(do_sync())

    if show:
        show_cmd(db_path, False, False)


@click.command("p2_address", short_help="Print the address to pay to the singleton")
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
            await sync_store.db_wrapper.close()

    asyncio.get_event_loop().run_until_complete(do_command())


@click.command("show", short_help="Show the status of the singleton, payments, and rekeys")
@click.option(
    "-db",
    "--db-path",
    help="The file path to the sync DB (default: ./sync (******).sqlite)",
    default="./",
    required=True,
)
@click.option(
    "-c",
    "--config",
    help="Display the details of the public config",
    is_flag=True,
)
@click.option(
    "-d",
    "--derivation",
    help="Display the private details of the private config",
    is_flag=True,
)
def _show_cmd(
    db_path: str,
    config: bool,
    derivation: bool,
):
    show_cmd(db_path, config, derivation)


def show_cmd(
    db_path: str,
    config: bool,
    derivation: bool,
):
    async def do_command():
        sync_store: SyncStore = await load_db(db_path)
        try:
            current_time = int(time.time())
            latest_singleton = await sync_store.get_latest_singleton()
            ach_records = await sync_store.get_ach_records()
            rekey_records = await sync_store.get_rekey_records()
            prefarm_info = await sync_store.get_configuration(True, block_outdated=False)

            p2_sum: int = 0
            i: int = 0
            while True:
                p2_singletons = await sync_store.get_p2_singletons(start_end=(uint32(i), uint32(i + 100)))
                if p2_singletons == []:
                    break
                p2_sum += sum(c.amount for c in p2_singletons)
                i += 100

            print()
            print(
                f"Current time: {current_time} ({datetime.fromtimestamp(current_time).strftime('%m/%d/%Y, %H:%M:%S')})"
            )
            print()
            print(f"Config up to date: {not (await sync_store.is_configuration_outdated())}")
            print()
            print("Singleton:")
            print(f"  - launcher ID: {prefarm_info.launcher_id}")
            print(f"  - amount left: {latest_singleton.coin.amount - 1}")
            print(f"  - amount to claim: {p2_sum}")
            print()
            print("Outstanding events:")
            print("  PAYMENTS:")
            for ach in ach_records:
                ach_ready_date: int = ach.confirmed_at_time + prefarm_info.payment_clawback_period
                ach_time_left: int = ach_ready_date - current_time
                if ach_time_left > 0:
                    ready_str = f"(Ready at: {datetime.fromtimestamp(ach_ready_date).strftime('%m/%d/%Y, %H:%M:%S')})"
                else:
                    ready_str = "(Ready)"
                print(f"- PAYMENT to {encode_puzzle_hash(ach.p2_ph, 'xch')} of amount {ach.coin.amount} {ready_str}")
            print("  REKEYS:")
            for rekey in rekey_records:
                rekey_ready_date: int = rekey.confirmed_at_time + prefarm_info.rekey_clawback_period
                rekey_time_left: int = rekey_ready_date - current_time
                if rekey_time_left > 0:
                    ready_str = f"(Ready at: {datetime.fromtimestamp(rekey_ready_date).strftime('%m/%d/%Y, %H:%M:%S')})"
                else:
                    ready_str = "(Ready)"
                print(f"- REKEY from {rekey.from_root} to {rekey.to_root} {ready_str}")

            if config:
                print()
                print("Config:")
                print(f" - current root: {prefarm_info.puzzle_root}")
                print(f" - withdrawal timelock: {prefarm_info.withdrawal_timelock} seconds")
                print(f" - payment clawback period: {prefarm_info.payment_clawback_period} seconds")
                print(f" - rekey cancellation period: {prefarm_info.rekey_clawback_period} seconds")
                print(f" - rekey penalty: {prefarm_info.rekey_increments} seconds")
                print(f" - slow rekey timelock: {prefarm_info.slow_rekey_timelock} seconds")

            if derivation:
                print()
                root_derivation = await sync_store.get_configuration(False, block_outdated=False)
                print("Derivation Info:")
                print(f" - lock level: {root_derivation.required_pubkeys}")
                print(f" - max lock level: {root_derivation.maximum_pubkeys}")
                print(f" - min keys to rekey: {root_derivation.minimum_pubkeys}")
                print(f" - standard rekey timelock: {root_derivation.prefarm_info.rekey_increments} seconds")
                print(f" - slow rekey penalty: {root_derivation.prefarm_info.slow_rekey_timelock} seconds")
                print(" - pubkeys: ")
                for pk in root_derivation.pubkey_list:
                    as_bech32m: str = BLSPublicKey(pk).as_bech32m()
                    print(f"    - {as_bech32m}")

        finally:
            await sync_store.db_wrapper.close()

    asyncio.get_event_loop().run_until_complete(do_command())


@click.command("audit", short_help="Export a history of the singleton to a CSV")
@click.option(
    "-db",
    "--db-path",
    help="The file path to the sync DB (default: ./sync (******).sqlite)",
    default="./",
    required=True,
)
@click.option(
    "-f",
    "--filepath",
    help="The file path the dump the audit log",
    required=False,
)
@click.option(
    "-d",
    "--diff",
    help="A previous audit log to diff against this one",
    required=False,
)
def audit_cmd(
    db_path: str,
    filepath: Optional[str],
    diff: Optional[str],
):
    if diff is not None:
        with open(diff, "r") as file:
            old_dict = json.load(file)

    async def do_command():
        sync_store: SyncStore = await load_db(db_path)
        try:
            singletons: List[SingletonRecord] = await sync_store.get_all_singletons()
            achs: List[ACHRecord] = await sync_store.get_ach_records(include_completed_coins=True)
            rekeys: List[RekeyRecord] = await sync_store.get_rekey_records(include_completed_coins=True)

            # Make dictionaries for easy lookup of coins from the singleton that created them
            singleton_parent_dict: Dict[bytes32, SingletonRecord] = {}
            singleton_dict: Dict[bytes32, SingletonRecord] = {}
            for singleton in singletons:
                singleton_parent_dict[singleton.coin.parent_coin_info] = singleton
                singleton_dict[singleton.coin.name()] = singleton
            ach_dict: Dict[bytes32, ACHRecord] = {}
            for ach in achs:
                ach_dict[ach.coin.parent_coin_info] = ach
            rekey_dict: Dict[bytes32, RekeyRecord] = {}
            for rekey in rekeys:
                rekey_dict[rekey.coin.parent_coin_info] = rekey

            audit_dict: List[Dict[str, Union[str, Dict[str, Union[str, int, bool]]]]] = []
            for singleton in singletons:
                coin_id = singleton.coin.name()
                if singleton.spend_type is None:
                    continue
                if singleton.spend_type == SpendType.HANDLE_PAYMENT:
                    params: Dict[str, Union[str, int, bool]] = {}
                    out_amount, in_amount, p2_ph = get_spend_params_for_ach_creation(
                        Program.from_bytes(bytes(singleton.solution))
                    )
                    if out_amount > 0:
                        params["out_amount"] = out_amount
                        params["recipient_ph"] = p2_ph.hex()
                    if in_amount > 0:
                        params["in_amount"] = in_amount
                    if coin_id in ach_dict:
                        ach_record: ACHRecord = ach_dict[coin_id]
                        if ach_record.completed is not None:
                            params["completed"] = ach_record.completed
                            params["spent_at_height"] = ach_record.spent_at_height
                            if not ach_record.completed and ach_record.clawback_pubkey is not None:
                                params["clawback_pubkey"] = BLSPublicKey(ach_record.clawback_pubkey).as_bech32m()
                elif singleton.spend_type == SpendType.START_REKEY:
                    params = {}
                    timelock, new_root = get_spend_params_for_rekey_creation(
                        Program.from_bytes(bytes(singleton.solution))
                    )
                    rekey_record: RekeyRecord = rekey_dict[coin_id]
                    params["from_root"] = rekey_record.from_root.hex()
                    params["to_root"] = rekey_record.to_root.hex()
                    if rekey_record.completed is not None:
                        params["completed"] = rekey_record.completed
                        params["spent_at_height"] = rekey_record.spent_at_height
                        if not rekey_record.completed and rekey_record.clawback_pubkey is not None:
                            params["clawback_pubkey"] = BLSPublicKey(rekey_record.clawback_pubkey).as_bech32m()
                elif singleton.spend_type == SpendType.FINISH_REKEY:
                    params = {
                        "from_root": singleton_dict[singleton.coin.parent_coin_info].puzzle_root.hex(),
                        "to_root": singleton.puzzle_root.hex(),
                    }

                audit_dict.append(
                    {
                        "time": singleton_parent_dict[coin_id].confirmed_at_time,
                        "action": singleton.spend_type.name,
                        "params": params,
                    }
                )

            sorted_audit_dict = sorted(audit_dict, key=lambda e: e["time"])

            if diff is not None:
                diff_dict = []
                for i in range(0, len(sorted_audit_dict)):
                    if i >= len(old_dict):
                        diff_dict.append({"new": sorted_audit_dict[i]})
                    elif old_dict[i] != sorted_audit_dict[i]:
                        diff_dict.append(
                            {
                                "old": old_dict[i],
                                "new": sorted_audit_dict[i],
                            }
                        )
                final_dict = diff_dict
            else:
                final_dict = sorted_audit_dict

            if filepath is None:
                print(json.dumps(final_dict))
            else:
                with open(filepath, "w") as file:
                    file.write(json.dumps(final_dict))

        finally:
            await sync_store.db_wrapper.close()

    asyncio.get_event_loop().run_until_complete(do_command())
