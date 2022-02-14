from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32

from cic.drivers.singleton import construct_singleton


def construct_inner_puzzle() -> Program:  # Rename this when it actually represents a real layer
    return Program.to(1)


def construct_full_singleton(launcher_id: bytes32) -> Program:
    return construct_singleton(launcher_id, construct_inner_puzzle())
