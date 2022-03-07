import enum

from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint64

from cic.drivers.drop_coins import construct_rekey_puzzle, construct_ach_puzzle
from cic.drivers.merkle_utils import build_merkle_tree
from cic.drivers.prefarm_info import PrefarmInfo
from cic.drivers.singleton import construct_singleton
from cic.drivers.rate_limiting import construct_rate_limiting_puzzle
from cic.load_clvm import load_clvm


PREFARM_INNER = load_clvm("prefarm_inner.clsp", package_or_requirement="cic.clsp.singleton")


class SpendType(int, enum.Enum):
    FINISH_REKEY = 1
    START_REKEY = 2
    HANDLE_PAYMENT = 3

def construct_payment_and_rekey_filter(
    prefarm_info: PrefarmInfo,
    puzzle_hash_list: List[bytes32],
    rekey_timelock: uint64,
) -> Program:
    return P2_MERKLE_MOD.curry(
        FILTER_REKEY_AND_PAYMENT_MOD.curry(
            [
                construct_rekey_puzzle(prefarm_info).get_tree_hash(),
                rekey_timelock,
                construct_ach_puzzle(prefarm_info).get_tree_hash(),
                calculate_ach_clawback_ph(prefarm_info),
            ],
        ),
        build_merkle_tree(puzzle_hash_list)[0],
        [],
    )


def construct_rekey_filter(
    prefarm_info: PrefarmInfo,
    puzzle_hash_list: List[bytes32],
    rekey_timelock: uint64,
) -> Program:
    return P2_MERKLE_MOD.curry(
        FILTER_ONLY_REKEY_MOD.curry(
            [
                construct_rekey_puzzle(prefarm_info).get_tree_hash(),
                rekey_timelock,
            ],
        ),
        build_merkle_tree(puzzle_hash_list)[0],
        [],
    )


def solve_filter_for_payment(
    puzzle_reveal: Program,
    proof_of_inclusion: Program,
    puzzle_solution: Program,
    puzzle_hash_list: List[bytes32],
    p2_ph: bytes32,
):
    return Program.to(
        [
            puzzle_reveal,
            proof_of_inclusion,
            [
                puzzle_solution,
                (build_merkle_tree(puzzle_hash_list)[0], p2_ph),
            ],
        ]
    )


def solve_filter_for_rekey(
    puzzle_reveal: Program,
    proof_of_inclusion: Program,
    puzzle_solution: Program,
    old_puzzle_hash_list: List[bytes32],
    new_puzzle_hash_list: List[bytes32],
    timelock: uint64,
):
    return Program.to(
        [
            puzzle_reveal,
            proof_of_inclusion,
            [
                puzzle_solution,
                [build_merkle_tree(new_puzzle_hash_list)[0], build_merkle_tree(old_puzzle_hash_list)[0], timelock],
            ],
        ]
    )


def construct_prefarm_inner_puzzle(prefarm_info: PrefarmInfo) -> Program:
    return PREFARM_INNER.curry(
        PREFARM_INNER.get_tree_hash(),
        build_merkle_tree(prefarm_info.puzzle_hash_list)[0],
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
