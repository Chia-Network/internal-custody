import enum

from dataclasses import dataclass
from typing import List, Tuple, Dict

from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint64
from chia.wallet.lineage_proof import LineageProof

from cic.drivers.drop_coins import construct_rekey_puzzle, construct_ach_puzzle
from cic.drivers.merkle_utils import build_merkle_tree
from cic.drivers.prefarm_info import PrefarmInfo
from cic.drivers.singleton import SINGLETON_MOD, SINGLETON_LAUNCHER_HASH, construct_singleton, construct_p2_singleton
from cic.drivers.rate_limiting import construct_rate_limiting_puzzle
from cic.load_clvm import load_clvm


PREFARM_INNER = load_clvm("prefarm_inner.clsp", package_or_requirement="cic.clsp.singleton")


class SpendType(int, enum.Enum):
    FINISH_REKEY = 1
    START_REKEY = 2
    HANDLE_PAYMENT = 3


def construct_prefarm_inner_puzzle(prefarm_info: PrefarmInfo) -> Program:
    return PREFARM_INNER.curry(
        PREFARM_INNER.get_tree_hash(),
        build_merkle_tree(prefarm_info.puzzle_hash_list)[0],
        [
            construct_rekey_puzzle(prefarm_info).get_tree_hash(),
            construct_ach_puzzle(prefarm_info).get_tree_hash(),
        ],
    )


def solve_prefarm_inner(spend_type: SpendType, prefarm_amount: uint64, **kwargs) -> Program:
    spend_solution: Program
    if spend_type in [SpendType.START_REKEY, SpendType.FINISH_REKEY]:
        spend_solution = Program.to(
            [
                kwargs["timelock"],
                build_merkle_tree(kwargs["puzzle_hash_list"])[0],
            ]
        )
    elif spend_type == SpendType.HANDLE_PAYMENT:
        spend_solution = Program.to(
            [
                kwargs["payment_amount"],
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
    launcher_id: bytes32,
    prefarm_info: PrefarmInfo,
) -> Program:
    return construct_singleton(
        launcher_id,
        construct_singleton_inner_puzzle(prefarm_info),
    )
