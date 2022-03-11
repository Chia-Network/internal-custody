import enum

from blspy import G1Element, G2Element
from typing import List, Optional, Tuple

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import CoinSpend
from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.types.spend_bundle import SpendBundle
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzles.p2_conditions import puzzle_for_conditions
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_pk, solution_for_delegated_puzzle
from chia.util.hash import std_hash
from chia.util.ints import uint64

from cic.drivers.drop_coins import (
    construct_rekey_puzzle,
    construct_ach_puzzle,
    curry_ach_puzzle,
    solve_ach_completion,
    solve_ach_clawback,
)
from cic.drivers.filters import construct_payment_and_rekey_filter, construct_rekey_filter, solve_filter_for_payment
from cic.drivers.merkle_utils import simplify_merkle_proof
from cic.drivers.prefarm_info import PrefarmInfo
from cic.drivers.puzzle_root_construction import RootDerivation
from cic.drivers.singleton import construct_singleton, solve_singleton, construct_p2_singleton, solve_p2_singleton
from cic.drivers.rate_limiting import construct_rate_limiting_puzzle, solve_rate_limiting_puzzle
from cic.load_clvm import load_clvm


PREFARM_INNER = load_clvm("prefarm_inner.clsp", package_or_requirement="cic.clsp.singleton")


class SpendType(int, enum.Enum):
    FINISH_REKEY = 1
    START_REKEY = 2
    HANDLE_PAYMENT = 3


def construct_prefarm_inner_puzzle(prefarm_info: PrefarmInfo) -> Program:
    return PREFARM_INNER.curry(
        PREFARM_INNER.get_tree_hash(),
        prefarm_info.puzzle_root,
        [
            construct_rekey_puzzle(prefarm_info).get_tree_hash(),
            construct_ach_puzzle(prefarm_info).get_tree_hash(),
            prefarm_info.withdrawal_timelock,
        ],
    )


def solve_prefarm_inner(spend_type: SpendType, prefarm_amount: uint64, **kwargs) -> Program:
    spend_solution: Program
    if spend_type in [SpendType.START_REKEY, SpendType.FINISH_REKEY]:
        spend_solution = Program.to(
            [
                kwargs["timelock"],
                kwargs["puzzle_root"],
            ]
        )
    elif spend_type == SpendType.HANDLE_PAYMENT:
        spend_solution = Program.to(
            [
                kwargs["out_amount"],
                kwargs["in_amount"],
                kwargs["p2_ph"],
            ]
        )
    else:
        raise ValueError("An invalid spend type was specified")

    return Program.to(
        [
            prefarm_amount,
            spend_type.value,
            spend_solution,
            kwargs.get("puzzle_reveal"),
            kwargs.get("proof_of_inclusion"),
            kwargs.get("puzzle_solution"),
        ]
    )


def construct_singleton_inner_puzzle(prefarm_info: PrefarmInfo) -> Program:
    return construct_rate_limiting_puzzle(
        prefarm_info.start_date,
        prefarm_info.starting_amount,
        prefarm_info.mojos_per_second,
        construct_prefarm_inner_puzzle(prefarm_info),
    )


def construct_full_singleton(
    prefarm_info: PrefarmInfo,
) -> Program:
    return construct_singleton(
        prefarm_info.launcher_id,
        construct_singleton_inner_puzzle(prefarm_info),
    )


def get_withdrawal_spend_info(
    singleton: Coin,
    pubkeys: List[G1Element],
    derivation: RootDerivation,
    lineage_proof: LineageProof,
    withdrawal_time: uint64,
    amount: uint64,
    clawforward_ph: bytes32,
    p2_singletons_to_claim: List[Coin] = [],
) -> Tuple[SpendBundle, bytes]:
    assert len(pubkeys) > 0
    agg_pk = G1Element()
    for pk in pubkeys:
        agg_pk += pk

    payment_amount: int = amount - sum(c.amount for c in p2_singletons_to_claim)

    # Info to claim the p2_singletons
    singleton_inner_puzhash: bytes32 = construct_singleton_inner_puzzle(derivation.prefarm_info).get_tree_hash()
    p2_singleton_spends: List[CoinSpend] = []
    p2_singleton_claim_conditions: List[Program] = []
    for p2_singleton in p2_singletons_to_claim:
        p2_singleton_spends.append(
            CoinSpend(
                p2_singleton,
                construct_p2_singleton(derivation.prefarm_info.launcher_id),
                solve_p2_singleton(p2_singleton, singleton_inner_puzhash),
            )
        )
        p2_singleton_claim_conditions.append(Program.to([62, p2_singleton.name()]))  # create
        p2_singleton_claim_conditions.append(Program.to([61, std_hash(p2_singleton.name() + b"$")]))  # assert

    # Proofs of inclusion
    filter_proof, leaf_proof = (list(proof.items())[0][1] for proof in derivation.get_proofs_of_inclusion(agg_pk))

    # Construct the puzzle reveals
    inner_puzzle: Program = puzzle_for_pk(agg_pk)
    filter_puzzle: Program = construct_payment_and_rekey_filter(
        derivation.prefarm_info,
        simplify_merkle_proof(inner_puzzle.get_tree_hash(), leaf_proof),
        derivation.prefarm_info.rekey_increments,
    )

    # Construct ACH creation solution
    if payment_amount < 0:
        ach_conditions: List[Program] = []
    else:
        ach_conditions = [
            Program.to([51, curry_ach_puzzle(derivation.prefarm_info, clawforward_ph).get_tree_hash(), amount])
        ]
    delegated_puzzle: Program = puzzle_for_conditions(
        [
            [
                51,
                construct_prefarm_inner_puzzle(derivation.prefarm_info).get_tree_hash(),
                singleton.amount - payment_amount,
            ],
            *ach_conditions,
            *p2_singleton_claim_conditions,
        ]
    )
    inner_solution: Program = solution_for_delegated_puzzle(delegated_puzzle, Program.to([]))

    return (
        SpendBundle(
            [
                CoinSpend(
                    singleton,
                    construct_full_singleton(derivation.prefarm_info),
                    solve_singleton(
                        lineage_proof,
                        singleton.amount,
                        solve_rate_limiting_puzzle(
                            withdrawal_time,
                            solve_prefarm_inner(
                                SpendType.HANDLE_PAYMENT,
                                singleton.amount,
                                out_amount=amount,
                                in_amount=uint64(sum(c.amount for c in p2_singletons_to_claim)),
                                p2_ph=clawforward_ph,
                                puzzle_reveal=filter_puzzle,
                                proof_of_inclusion=filter_proof,
                                puzzle_solution=solve_filter_for_payment(
                                    inner_puzzle,
                                    leaf_proof,
                                    inner_solution,
                                    derivation.prefarm_info.puzzle_root,
                                    clawforward_ph,
                                ),
                            ),
                        ),
                    ),
                ),
                *p2_singleton_spends,
            ],
            G2Element(),
        ),
        (delegated_puzzle.get_tree_hash() + singleton.name() + DEFAULT_CONSTANTS.AGG_SIG_ME_ADDITIONAL_DATA),  # TODO
    )


def get_ach_clawback_spend_info(
    ach_coin: Coin,
    pubkeys: List[G1Element],
    derivation: RootDerivation,
    clawforward_ph: bytes32,
) -> Tuple[SpendBundle, bytes]:
    assert len(pubkeys) > 0
    agg_pk = G1Element()
    for pk in pubkeys:
        agg_pk += pk
    # Proofs of inclusion
    filter_proof, leaf_proof = (list(proof.items())[0][1] for proof in derivation.get_proofs_of_inclusion(agg_pk))

    # Construct the puzzle reveals
    inner_puzzle: Program = puzzle_for_pk(agg_pk)
    filter_puzzle: Program = construct_payment_and_rekey_filter(
        derivation.prefarm_info,
        simplify_merkle_proof(inner_puzzle.get_tree_hash(), leaf_proof),
        derivation.prefarm_info.rekey_increments,
    )

    # Construct inner solution
    delegated_puzzle: Program = puzzle_for_conditions(
        [[51, construct_p2_singleton(derivation.prefarm_info.launcher_id).get_tree_hash(), ach_coin.amount]]
    )
    inner_solution: Program = solution_for_delegated_puzzle(delegated_puzzle, Program.to([]))

    return (
        SpendBundle(
            [
                CoinSpend(
                    ach_coin,
                    curry_ach_puzzle(derivation.prefarm_info, clawforward_ph),
                    solve_ach_clawback(
                        derivation.prefarm_info,
                        ach_coin.amount,
                        filter_puzzle,
                        filter_proof,
                        solve_filter_for_payment(
                            inner_puzzle,
                            leaf_proof,
                            inner_solution,
                            derivation.prefarm_info.puzzle_root,
                            clawforward_ph,
                        ),
                    ),
                ),
            ],
            G2Element(),
        ),
        (delegated_puzzle.get_tree_hash() + ach_coin.name() + DEFAULT_CONSTANTS.AGG_SIG_ME_ADDITIONAL_DATA),  # TODO
    )


def get_ach_clawforward_spend_bundle(
    ach_coin: Coin,
    derivation: RootDerivation,
    clawforward_ph: bytes32,
) -> SpendBundle:
    return SpendBundle(
        [
            CoinSpend(
                ach_coin,
                curry_ach_puzzle(derivation.prefarm_info, clawforward_ph),
                solve_ach_completion(derivation.prefarm_info, ach_coin.amount),
            ),
        ],
        G2Element(),
    )
