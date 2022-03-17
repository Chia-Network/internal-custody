from blspy import G1Element
from dataclasses import dataclass
from typing import Optional

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import SerializedProgram
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint32
from chia.wallet.lineage_proof import LineageProof

from cic.drivers.prefarm import SpendType


@dataclass(frozen=True)
class SingletonRecord:
    coin: Coin
    puzzle_root: bytes32
    lineage_proof: LineageProof
    generation: uint32
    puzzle_reveal: Optional[SerializedProgram]
    solution: Optional[SerializedProgram]
    spend_type: Optional[SpendType]
    spending_pubkey: Optional[G1Element]
