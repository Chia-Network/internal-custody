from typing import Tuple, Dict, List, Any

import hashlib

from chia.types.blockchain_format.sized_bytes import bytes32


# paths here are not quite the same a `NodePath` paths. We don't need the high order bit
# anymore since the proof indicates how big the path is.


def compose_paths(path_1: int, path_2: int, path_2_length: int) -> int:
    return (path_1 << path_2_length) | path_2


def sha256(*args: Tuple[bytes]) -> bytes32:
    return bytes32(hashlib.sha256(b"".join(args)).digest())


def build_merkle_tree_from_2_tuples(tuples) -> Tuple[bytes32, Dict[bytes32, List[bytes32]]]:
    if isinstance(tuples, bytes):
        return tuples, {tuples: (0, [])}

    left, right = tuples
    left_root, left_proofs = build_merkle_tree_from_2_tuples(left)
    right_root, right_proofs = build_merkle_tree_from_2_tuples(right)

    new_root = sha256(left_root, right_root)
    new_proofs = {}
    for name, (path, proof) in left_proofs.items():
        proof.append(right_root)
        new_proofs[name] = (path, proof)
    for name, (path, proof) in right_proofs.items():
        path |= 1 << len(proof)
        proof.append(left_root)
        new_proofs[name] = (path, proof)
    return new_root, new_proofs


def list_to_2_tuples(objects: List[Any]):
    size = len(objects)
    if size == 1:
        return objects[0]
    midpoint = (size + 1) >> 1
    first_half = objects[:midpoint]
    last_half = objects[midpoint:]
    return (list_to_2_tuples(first_half), list_to_2_tuples(last_half))


def build_merkle_tree(objects: List[bytes32]) -> Tuple[bytes32, Dict[bytes32, List[bytes32]]]:
    """
    return (merkle_root, dict_of_proofs)
    """
    objects_2_tuples = list_to_2_tuples(objects)
    return build_merkle_tree_from_2_tuples(objects_2_tuples)


def simplify_merkle_proof(tree_hash: bytes32, proof: Tuple[int, List[bytes32]]) -> bytes32:
    # we return the expected merkle root
    path, nodes = proof
    for node in nodes:
        if path & 1:
            tree_hash = sha256(node, tree_hash)
        else:
            tree_hash = sha256(tree_hash, node)
        path >>= 1
    return tree_hash


def check_merkle_proof(merkle_root: bytes32, tree_hash: bytes32, proof: Tuple[int, List[bytes32]]) -> bool:
    return merkle_root == simplify_merkle_proof(tree_hash, proof)
