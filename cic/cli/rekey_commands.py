import asyncio

from typing import List, Optional

import click

from chia.types.blockchain_format.program import Program
from chia_rs import PrivateKey, G1Element
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import (
    calculate_synthetic_offset,
    DEFAULT_HIDDEN_PUZZLE_HASH,
)

from cic.cli.common import (
    get_additional_data,
    load_db,
    load_pubkeys,
    load_root_derivation,
    to_hsm_coin_spend,
    write_unsigned_spend,
)
from cic.cli.record_types import SingletonRecord
from cic.cli.sync_store import SyncStore
from cic.drivers.prefarm import get_rekey_spend_info
from cic.drivers.puzzle_root_construction import RootDerivation

from chia_base.bls12_381 import BLSPublicKey, BLSSecretExponent
from hsms.core.signing_hints import SumHint
from hsms.core.unsigned_spend import UnsignedSpend
from hsms.util.qrint_encoding import b2a_qrint


@click.command("start_rekey", short_help="Rekey the singleton to a new set of keys/options")
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
            pubkey_list: List[G1Element] = list(load_pubkeys(pubkeys))
            fee_conditions: List[Program] = [Program.to([60, b""])]

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


@click.command(
    "increase_security_level", short_help="Initiate an increase of the number of keys required for withdrawal"
)
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
            pubkey_list: List[G1Element] = list(load_pubkeys(pubkeys))
            fee_conditions: List[Program] = [Program.to([60, b""])]

            # Validate we have enough pubkeys
            if len(pubkey_list) < derivation.required_pubkeys + 1:
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
            coin_spends = [to_hsm_coin_spend(cs) for cs in lock_bundle.coin_spends]
            unsigned_spend = UnsignedSpend(
                coin_spends,
                [SumHint(as_bls_pubkey_list, BLSSecretExponent.zero())],
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
