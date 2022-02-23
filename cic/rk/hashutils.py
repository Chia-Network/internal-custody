import hashlib

from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from clvm_tools.binutils import assemble


def sha256(*args: bytes) -> bytes32:
    return bytes32(hashlib.sha256(b"".join(args)).digest())


ONE_BLOB = bytes.fromhex("01")


def sha256atom(blob: bytes) -> bytes32:
    return sha256(ONE_BLOB, blob)


TWO_BLOB = bytes.fromhex("02")


def sha256pair(left: bytes32, right: bytes32) -> bytes32:
    return sha256(TWO_BLOB, left, right)


A_BLOB = Program.to(assemble("#a"))
C_BLOB = Program.to(assemble("#c"))
Q_BLOB = Program.to(assemble("#q"))

NULL_TREEHASH = Program.to(0).get_tree_hash()
ONE_TREEHASH = Program.to(1).get_tree_hash()
A_TREEHASH = A_BLOB.get_tree_hash()
C_TREEHASH = C_BLOB.get_tree_hash()
Q_TREEHASH = Q_BLOB.get_tree_hash()
