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
    format_duration,
    get_additional_data,
    open_sync_store,
    load_pubkeys,
    load_root_derivation,
    to_hsm_coin_spend,
    write_unsigned_spend,
)
from cic.cli.record_types import SingletonRecord
from cic.drivers.prefarm import get_rekey_spend_info
from cic.drivers.puzzle_root_construction import RootDerivation, derive_next_lock_level

from chia_base.bls12_381 import BLSPublicKey, BLSSecretExponent
from hsms.core.signing_hints import SumHint
from hsms.core.unsigned_spend import UnsignedSpend
from hsms.util.byte_chunks import chunks_for_zlib_blob
from hsms.util.qrint_encoding import b2a_qrint


def build_rekey_unsigned_spend(
    current_singleton: SingletonRecord,
    pubkey_list: List[G1Element],
    derivation: RootDerivation,
    new_derivation: Optional[RootDerivation],
) -> UnsignedSpend:
    """Build the unsigned rekey spend for the current singleton."""
    fee_conditions: List[Program] = [Program.to([60, b""])]
    singleton_bundle, _ = get_rekey_spend_info(
        current_singleton.coin,
        pubkey_list,
        derivation,
        current_singleton.lineage_proof,
        new_derivation,
        fee_conditions,
    )

    as_bls_pubkey_list = [BLSPublicKey(pk) for pk in pubkey_list]
    aggregate_pubkey = G1Element()
    for pubkey in pubkey_list:
        aggregate_pubkey += pubkey
    synth_sk = BLSSecretExponent(
        PrivateKey.from_bytes(
            calculate_synthetic_offset(aggregate_pubkey, DEFAULT_HIDDEN_PUZZLE_HASH).to_bytes(32, "big")
        )
    )
    coin_spends = [to_hsm_coin_spend(cs) for cs in singleton_bundle.coin_spends]
    return UnsignedSpend(
        coin_spends,
        [SumHint(as_bls_pubkey_list, synth_sk)],
        [],
        get_additional_data(),
    )


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
        async with open_sync_store(db_path) as sync_store:
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

            # Get the spend bundle
            unsigned_spend = build_rekey_unsigned_spend(
                current_singleton,
                pubkey_list,
                derivation,
                new_derivation,
            )

            # Print the result
            if filename is not None:
                write_unsigned_spend(filename, unsigned_spend)
                print(f"Successfully wrote spend to {filename}")
            else:
                for chunk in chunks_for_zlib_blob(bytes(unsigned_spend), 255):
                    print(str(b2a_qrint(chunk)))

    asyncio.run(do_command())


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
        async with open_sync_store(db_path) as sync_store:
            derivation = await sync_store.get_configuration(False, block_outdated=True)

            current_singleton: Optional[SingletonRecord] = await sync_store.get_latest_singleton()
            if current_singleton is None:
                raise RuntimeError("No singleton is found for this configuration.  Try `cic sync` then try again.")
            pubkey_list: List[G1Element] = list(load_pubkeys(pubkeys))

            if derivation.next_root is None:
                raise click.ClickException("already at maximum lock level")

            # Validate we have exactly the current number of required pubkeys
            if len(pubkey_list) != derivation.required_pubkeys:
                raise click.ClickException(
                    f"A lock level increase is signed by exactly {derivation.required_pubkeys} keys "
                    f"(the current lock level); you passed {len(pubkey_list)}."
                )

            try:
                new_derivation = derive_next_lock_level(derivation)
            except ValueError as error:
                raise click.ClickException(str(error)) from error

            # Create the spend
            unsigned_spend = build_rekey_unsigned_spend(
                current_singleton,
                pubkey_list,
                derivation,
                new_derivation,
            )

            rekey_increments = derivation.prefarm_info.rekey_increments
            rekey_clawback_period = derivation.prefarm_info.rekey_clawback_period
            explanation = (
                f"Rekey to lock level {derivation.required_pubkeys + 1} of {derivation.maximum_pubkeys}. "
                f"It can be pushed once the singleton has not been spent for {rekey_increments} "
                f"({format_duration(rekey_increments)}), and completed with cic complete "
                f"{rekey_clawback_period} ({format_duration(rekey_clawback_period)}) after it confirms. "
                "It can be cancelled with cic clawback until it is completed. "
                "cic sync updates the configuration afterwards."
            )

            if filename is not None:
                write_unsigned_spend(filename, unsigned_spend)
                print(explanation)
                print(f"Successfully wrote spend to {filename}")
            else:
                for chunk in chunks_for_zlib_blob(bytes(unsigned_spend), 255):
                    print(str(b2a_qrint(chunk)))
                click.echo(explanation, err=True)

    asyncio.run(do_command())
