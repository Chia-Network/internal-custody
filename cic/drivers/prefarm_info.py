from dataclasses import dataclass
from typing import List

from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint64

@dataclass
class PrefarmInfo:
    launcher_id: bytes32
    start_date: uint64
    starting_amount: uint64
    mojos_per_second: uint64
    puzzle_hash_list: List[bytes32]
    clawback_period: uint64