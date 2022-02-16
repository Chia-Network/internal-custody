from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint64

from cic.drivers.singleton import construct_singleton
from cic.drivers.rate_limiting import construct_rate_limiting_puzzle


def construct_singleton_inner_puzzle(
    initial_drain_date: uint64,
    drain_rate: uint64,
    inner_puzzle: Program,
) -> Program:
    return construct_rate_limiting_puzzle(
        initial_drain_date,
        drain_rate,
        inner_puzzle,
    )


def construct_full_singleton(
    launcher_id: bytes32,
    initial_drain_date: uint64,
    drain_rate: uint64,
    inner_puzzle: Program,
) -> Program:
    return construct_singleton(
        launcher_id,
        construct_singleton_inner_puzzle(
            initial_drain_date,
            drain_rate,
            inner_puzzle,
        ),
    )
