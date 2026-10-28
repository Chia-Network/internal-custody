"""Construction of the spend bundle that completes a pending rekey or lock."""

import dataclasses

from chia_rs import SpendBundle
from chia.wallet.lineage_proof import LineageProof

from cic.cli.record_types import SingletonRecord, RekeyRecord
from cic.drivers.prefarm import construct_singleton_inner_puzzle, get_rekey_completion_spend
from cic.drivers.puzzle_root_construction import RootDerivation


def get_rekey_completion_bundle(
    current_singleton: SingletonRecord,
    parent_singleton: SingletonRecord,
    record: RekeyRecord,
    derivation: RootDerivation,
) -> SpendBundle:
    """Build the spend bundle that completes the given rekey or lock record."""
    if record.timelock == 0:
        new_derivation = None
        num_pubkeys = derivation.required_pubkeys + 1
    else:
        new_derivation = dataclasses.replace(
            derivation,
            prefarm_info=dataclasses.replace(derivation.prefarm_info, puzzle_root=record.to_root),
        )
        num_pubkeys = derivation.required_pubkeys - (record.timelock - 1)

    completion_bundle = get_rekey_completion_spend(
        current_singleton.coin,
        record.coin,
        derivation.pubkey_list[0:num_pubkeys],
        derivation,
        current_singleton.lineage_proof,
        LineageProof(
            parent_singleton.coin.parent_coin_info,
            construct_singleton_inner_puzzle(derivation.prefarm_info).get_tree_hash(),
            parent_singleton.coin.amount,
        ),
        new_derivation,
    )
    if record.timelock == 0:
        # Put the bundle in the same order as the second push that `cic push_tx`
        # prints: the lock coin spend first, then the singleton FINISH_REKEY
        # spend.  This makes the two bundles byte-identical.
        completion_bundle = SpendBundle(
            sorted(
                completion_bundle.coin_spends,
                key=lambda coin_spend: coin_spend.coin.name() != record.coin.name(),
            ),
            completion_bundle.aggregated_signature,
        )
    return completion_bundle
