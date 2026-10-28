import asyncio
import dataclasses
import math
import time

from operator import attrgetter
from typing import Dict, List, Optional, Union

import click

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia_rs import PrivateKey, G1Element
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint8, uint32, uint64
from chia.util.bech32m import decode_puzzle_hash, encode_puzzle_hash
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import (
    calculate_synthetic_offset,
    DEFAULT_HIDDEN_PUZZLE_HASH,
)

from cic.cli.common import (
    get_additional_data,
    load_db,
    load_pubkeys,
    to_hsm_coin_spend,
    write_unsigned_spend,
)
from cic.cli.record_types import SingletonRecord, ACHRecord, RekeyRecord
from cic.cli.sync_store import SyncStore
from cic.cli.rekey_completion import get_rekey_completion_bundle
from cic.drivers.prefarm import (
    get_withdrawal_spend_info,
    get_ach_clawback_spend_info,
    get_rekey_clawback_spend_info,
    get_ach_clawforward_spend_bundle,
)

from chia_base.bls12_381 import BLSPublicKey, BLSSecretExponent
from hsms.core.signing_hints import SumHint
from hsms.core.unsigned_spend import UnsignedSpend
from hsms.util.qrint_encoding import b2a_qrint


@click.command("payment", short_help="Absorb/Withdraw money into/from the singleton")
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
    help="The outgoing amount (in mojos) to pay",
    default=0,
    show_default=True,
)
@click.option(
    "-t",
    "--recipient-address",
    help="The address that can claim the money after the clawback period is over (must be supplied if amount is > 0)",
    required=True,
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
    recipient_address: str,
    absorb_available_payments: bool,
    maximum_extra_cost: Optional[int],
    amount_threshold: int,
    filename: Optional[str],
):
    # Check to make sure we've been given a correct set of parameters
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
            pubkey_list: List[G1Element] = list(load_pubkeys(pubkeys))
            clawforward_ph: bytes32 = decode_puzzle_hash(recipient_address)
            fee_conditions: List[Program] = [Program.to([60, b""])]

            # Get any p2_singletons to spend
            if absorb_available_payments:
                max_num: Optional[uint32] = (
                    (uint32(0), uint32(math.floor(maximum_extra_cost / 10))) if maximum_extra_cost is not None else None
                )
                p2_singletons: List[Coin] = await sync_store.get_p2_singletons(amount_threshold, start_end=max_num)
                if sum(c.amount for c in p2_singletons) % 2 == 1:
                    smallest_odd_coin: Coin = sorted(
                        [c for c in p2_singletons if c.amount % 2 == 1], key=attrgetter("amount")
                    )[0]
                    p2_singletons = [c for c in p2_singletons if c.name() != smallest_odd_coin.name()]
            else:
                p2_singletons = []

            # Get the spend bundle
            singleton_bundle, data_to_sign = get_withdrawal_spend_info(
                current_singleton.coin,
                pubkey_list,
                derivation,
                current_singleton.lineage_proof,
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
            coin_spends = [to_hsm_coin_spend(cs) for cs in singleton_bundle.coin_spends]
            unsigned_spend = UnsignedSpend(
                coin_spends,
                [SumHint(as_bls_pubkey_list, synth_sk)],
                [],
                get_additional_data(),
            )

            # Print the result
            if filename is not None:
                write_unsigned_spend(filename, unsigned_spend)
                print(f"Successfully wrote spend to {filename}")
            else:
                for chunk in unsigned_spend.chunk(255):
                    print(str(b2a_qrint(chunk)))
        finally:
            await sync_store.db_wrapper.close()

    asyncio.get_event_loop().run_until_complete(do_command())


@click.command("clawback", short_help="Clawback a withdrawal or rekey attempt (will be prompted which one)")
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
            selectable_records: Dict[int, Union[ACHRecord, RekeyRecord]] = {}
            for ach in achs:
                print(f"{index}) PAYMENT to {encode_puzzle_hash(ach.p2_ph, 'xch')} of amount {ach.coin.amount}")
                selectable_records[index] = ach
                index += 1
            for rekey in rekeys:
                print(f"{index}) REKEY from {rekey.from_root} to {rekey.to_root}")
                selectable_records[index] = rekey
                index += 1
            selected_action = int(input("(Enter index of action to cancel): "))
            if selected_action not in range(1, index):
                print("Invalid index specified.")
                return

            # Construct the spend for the selected index
            pubkey_list: List[G1Element] = list(load_pubkeys(pubkeys))
            fee_conditions: List[Program] = [Program.to([60, b""])]
            record = selectable_records[selected_action]
            if isinstance(record, ACHRecord):
                # Validate we have enough keys
                if len(pubkey_list) != derivation.required_pubkeys:
                    print("Incorrect number of keys to claw back selected payment")
                    return

                # Get the spend bundle
                clawback_bundle, data_to_sign = get_ach_clawback_spend_info(
                    record.coin,
                    pubkey_list,
                    derivation,
                    record.p2_ph,
                    fee_conditions,
                )
            else:
                # Validate we have enough keys
                timelock: uint64 = record.timelock
                required_pubkeys: Optional[int] = None
                if timelock == uint8(1):
                    required_pubkeys = derivation.required_pubkeys
                else:
                    for i in range(derivation.minimum_pubkeys, derivation.required_pubkeys):
                        if timelock == uint8(1 + derivation.required_pubkeys - i):
                            required_pubkeys = i
                            break
                if required_pubkeys is None or len(pubkey_list) != required_pubkeys:
                    print("Incorrect number of keys to claw back selected rekey")
                    return

                # Get the spend bundle
                clawback_bundle, data_to_sign = get_rekey_clawback_spend_info(
                    record.coin,
                    pubkey_list,
                    derivation,
                    record.timelock,
                    dataclasses.replace(
                        derivation,
                        prefarm_info=dataclasses.replace(derivation.prefarm_info, puzzle_root=record.to_root),
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
            coin_spends = [to_hsm_coin_spend(cs) for cs in clawback_bundle.coin_spends]
            unsigned_spend = UnsignedSpend(
                coin_spends,
                [SumHint(as_bls_pubkey_list, synth_sk)],
                [],
                get_additional_data(),
            )

            if filename is not None:
                write_unsigned_spend(filename, unsigned_spend)
                print(f"Successfully wrote spend to {filename}")
            else:
                for chunk in unsigned_spend.chunk(255):
                    print(str(b2a_qrint(chunk)))
        finally:
            await sync_store.db_wrapper.close()

    asyncio.get_event_loop().run_until_complete(do_command())


@click.command("complete", short_help="Complete a withdrawal or rekey attempt (will be prompted which one)")
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
            selectable_records: Dict[int, Union[ACHRecord, RekeyRecord]] = {}
            for ach in achs:
                if ach.confirmed_at_time + derivation.prefarm_info.payment_clawback_period < time.time():
                    prefix = f"{index})"
                    selectable_records[index] = ach
                    index += 1
                else:
                    prefix = "-)"
                print(f"{prefix} PAYMENT to {encode_puzzle_hash(ach.p2_ph, 'xch')} of amount {ach.coin.amount}")

            for rekey in rekeys:
                is_lock = rekey.timelock == 0
                if is_lock or rekey.confirmed_at_time + derivation.prefarm_info.rekey_clawback_period < time.time():
                    prefix = f"{index})"
                    selectable_records[index] = rekey
                    index += 1
                else:
                    prefix = "-)"
                if is_lock:
                    print(f"{prefix} LOCK LEVEL INCREASE from {rekey.from_root} (step 2 pending)")
                else:
                    print(f"{prefix} REKEY from {rekey.from_root} to {rekey.to_root}")
            if index == 1:
                print("No actions can be completed at this time.")
                return
            selected_action = int(input("(Enter index of action to complete): "))
            if selected_action not in range(1, index):
                print("Invalid index specified.")
                return

            # Construct the spend for the selected index
            record = selectable_records[selected_action]
            if isinstance(record, ACHRecord):
                # Get the spend bundle
                completion_bundle = get_ach_clawforward_spend_bundle(
                    record.coin,
                    dataclasses.replace(
                        derivation,
                        prefarm_info=dataclasses.replace(derivation.prefarm_info, puzzle_root=record.from_root),
                    ),
                    record.p2_ph,
                )
            else:
                current_singleton: Optional[SingletonRecord] = await sync_store.get_latest_singleton()
                if current_singleton is None:
                    raise RuntimeError("No singleton is found for this configuration.  Try `cic sync` then try again.")
                parent_singleton: Optional[SingletonRecord] = await sync_store.get_singleton_record(
                    record.coin.parent_coin_info
                )
                if parent_singleton is None:
                    raise RuntimeError("Bad sync information. Please try a resync.")

                # Get the spend bundle
                completion_bundle = get_rekey_completion_bundle(
                    current_singleton,
                    parent_singleton,
                    record,
                    derivation,
                )

            if filename is not None:
                with open(filename, "w") as file:
                    file.write(bytes(completion_bundle).hex())
                print(f"Successfully wrote spend to {filename}")
            else:
                print(bytes(completion_bundle).hex())
        finally:
            await sync_store.db_wrapper.close()

    asyncio.get_event_loop().run_until_complete(do_command())
