from dataclasses import dataclass

from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint64


@dataclass
class PrefarmInfo:
    launcher_id: bytes32
    start_date: uint64
    starting_amount: uint64
    mojos_per_second: uint64
    puzzle_root: bytes32
    withdrawal_timelock: uint64
    clawback_period: uint64
