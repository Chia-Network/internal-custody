"""Shared helpers for CLI tests that need a launched singleton."""

import dataclasses
import os
from pathlib import Path
from typing import Iterable, List, Optional, Union

from chia_rs import AugSchemeMPL, G1Element, PrivateKey, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint32, uint64
from click.testing import CliRunner

from chia.util.hash import std_hash

from chia_base.bls12_381 import BLSPublicKey, BLSSecretExponent, BLSSignature
from chia_base.cbincode import to_bytes
from hsms.cmds.hsmmerge import create_spend_bundle
from hsms.process.sign import sign

from cic.cli.main import cli, read_unsigned_spend
from cic.drivers.prefarm_info import PrefarmInfo
from cic.drivers.puzzle_root_construction import RootDerivation, calculate_puzzle_root


def secret_key_for_index(index: int) -> PrivateKey:
    blob = index.to_bytes(32, "big")
    hashed_blob = AugSchemeMPL.key_gen(std_hash(b"foo" + blob))
    secret_exponent = int.from_bytes(hashed_blob.to_bytes(), "big")
    return PrivateKey.from_bytes(secret_exponent.to_bytes(32, "big"))


@dataclasses.dataclass
class LaunchedSingleton:
    prefarm_info: PrefarmInfo
    derivation: RootDerivation
    pubkeys: List[G1Element]
    pubkey_files: List[str]
    config_path: Path
    sync_db_path: Path


def make_prefarm_info() -> PrefarmInfo:
    return PrefarmInfo(
        bytes32([0] * 32),
        bytes32([0] * 32),
        uint64(30),
        uint64(90),
        uint64(30),
        uint64(15),
        uint64(45),
    )


def sign_unsigned_spend(filename: Union[str, Path], key_indices: Iterable[int]) -> SpendBundle:
    unsigned_spend = read_unsigned_spend(str(filename))
    sigs: List[BLSSignature] = [
        si.signature
        for si in sign(unsigned_spend, [BLSSecretExponent(secret_key_for_index(key)) for key in key_indices])
    ]
    hsms_bundle = create_spend_bundle(unsigned_spend, sigs)
    return SpendBundle.from_bytes(to_bytes(hsms_bundle))


def setup_launched_singleton(runner: CliRunner, prefarm_info: Optional[PrefarmInfo] = None) -> LaunchedSingleton:
    """Run the minimum init/derive/launch/sync sequence used by test_init."""
    if prefarm_info is None:
        prefarm_info = make_prefarm_info()
    os.mkdir("infos")
    result = runner.invoke(
        cli,
        [
            "init",
            "--directory",
            "./infos/",
            "--withdrawal-timelock",
            str(prefarm_info.withdrawal_timelock),
            "--payment-clawback",
            str(prefarm_info.payment_clawback_period),
            "--rekey-cancel",
            str(prefarm_info.rekey_clawback_period),
            "--rekey-timelock",
            str(prefarm_info.rekey_increments),
            "--slow-penalty",
            str(prefarm_info.slow_rekey_timelock),
        ],
    )
    assert result.exit_code == 0, result.output

    pubkeys = [secret_key_for_index(index).get_g1() for index in range(5)]
    pubkey_files: List[str] = []
    for index, pubkey in enumerate(pubkeys):
        filename = f"{index}.pk"
        with open(filename, "w") as file:
            file.write(BLSPublicKey(pubkey).as_bech32m())
        pubkey_files.append(filename)

    derivation = calculate_puzzle_root(
        prefarm_info,
        pubkeys,
        uint32(3),
        uint32(5),
        uint32(1),
    )

    result = runner.invoke(
        cli,
        [
            "derive_root",
            "--configuration",
            "./infos/Configuration (needs derivation).txt",
            "--pubkeys",
            ",".join(pubkey_files),
            "--initial-lock-level",
            str(uint32(3)),
        ],
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(
        cli,
        [
            "launch_singleton",
            "--configuration",
            "./infos/Configuration (awaiting launch).txt",
            "--db-path",
            "./infos/",
            "--fee",
            "100",
        ],
    )
    assert result.exit_code == 0, result.output

    config_path = next(Path("./infos/").glob("Configuration (*).txt"))
    with open(config_path, "rb") as file:
        derivation = RootDerivation.from_bytes(file.read())

    result = runner.invoke(
        cli,
        ["sync", "--configuration", str(config_path), "--db-path", "./infos/"],
    )
    assert result.exit_code == 0, result.output

    sync_db_path = next(Path("./infos/").glob("sync (*).sqlite"))
    return LaunchedSingleton(
        prefarm_info=prefarm_info,
        derivation=derivation,
        pubkeys=pubkeys,
        pubkey_files=pubkey_files,
        config_path=config_path,
        sync_db_path=sync_db_path,
    )
