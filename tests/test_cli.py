import asyncio
import os

from blspy import BasicSchemeMPL, PrivateKey, G2Element
from click.testing import CliRunner, Result
from pathlib import Path
from typing import List, Optional, Tuple

from chia.clvm.spend_sim import SpendSim, SimClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import CoinSpend
from chia.types.spend_bundle import SpendBundle
from chia.util.bech32m import encode_puzzle_hash
from chia.util.hash import std_hash
from chia.util.ints import uint32, uint64

from cic.cli.main import cli, read_unsigned_spend
from cic.cli.record_types import SingletonRecord, ACHRecord, RekeyRecord
from cic.cli.sync_store import SyncStore
from cic.drivers.prefarm_info import PrefarmInfo
from cic.drivers.puzzle_root_construction import RootDerivation, calculate_puzzle_root
from cic.drivers.singleton import construct_p2_singleton

from hsms.bls12_381 import BLSPublicKey, BLSSecretExponent, BLSSignature
from hsms.cmds.hsmmerge import create_spend_bundle
from hsms.process.sign import sign


# Key functions
def secret_key_for_index(index: int) -> PrivateKey:
    blob = index.to_bytes(32, "big")
    hashed_blob = BasicSchemeMPL.key_gen(std_hash(b"foo" + blob))
    secret_exponent = int.from_bytes(hashed_blob, "big")
    return PrivateKey.from_bytes(secret_exponent.to_bytes(32, "big"))


ACS: Program = Program.to(1)
ACS_PH: bytes32 = ACS.get_tree_hash()


def test_help():
    runner = CliRunner()
    result: Result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Commands to control a prefarm singleton" in result.output


def test_init():
    runner = CliRunner()

    prefarm_info = PrefarmInfo(
        bytes32([0] * 32),
        bytes32([0] * 32),
        uint64(30),
        uint64(90),
        uint64(30),
        uint64(15),
        uint64(45),
    )

    STARTING_AMOUNT = 1000000000000

    with runner.isolated_filesystem():
        os.mkdir("infos")
        result: Result = runner.invoke(cli, ["init"])
        assert result.exit_code != 0

        # Initialize the configuration
        result = runner.invoke(
            cli,
            [
                "init",
                "--directory",
                "./infos/",
                "--withdrawal-timelock",
                prefarm_info.withdrawal_timelock,
                "--payment-clawback",
                prefarm_info.payment_clawback_period,
                "--rekey-cancel",
                prefarm_info.rekey_clawback_period,
                "--rekey-timelock",
                uint64(15),
                "--slow-penalty",
                uint64(45),
            ],
        )

        with open("./infos/Configuration (needs derivation).txt", "rb") as file:
            assert PrefarmInfo.from_bytes(file.read()) == prefarm_info

        # Derive the private information
        pubkeys = [
            secret_key_for_index(0).get_g1(),
            secret_key_for_index(1).get_g1(),
            secret_key_for_index(2).get_g1(),
            secret_key_for_index(3).get_g1(),
            secret_key_for_index(4).get_g1(),
        ]
        pubkeys_as_blspk = [BLSPublicKey(pk) for pk in pubkeys]
        pubkey_files: List[str] = []
        for i in range(0, len(pubkeys_as_blspk)):
            filename = f"{i}.pk"
            with open(filename, "w") as file:
                file.write(pubkeys_as_blspk[i].as_bech32m())
            pubkey_files.append(filename)
        derivation: RootDerivation = calculate_puzzle_root(
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
                uint32(3),
            ],
        )

        with open("./infos/Configuration (awaiting launch).txt", "rb") as file:
            assert RootDerivation.from_bytes(file.read()) == derivation

        # Launch the singleton using the configuration
        result = runner.invoke(
            cli,
            [
                "launch_singleton",
                "--configuration",
                "./infos/Configuration (awaiting launch).txt",
                "--db-path",
                "./infos/",
                "--fee",
                100,
            ],
        )

        config_path = next(Path("./infos/").glob("Configuration (*).txt"))
        with open(config_path, "rb") as file:
            new_derivation = RootDerivation.from_bytes(file.read())
            assert new_derivation.prefarm_info.launcher_id != bytes32([0] * 32)
            derivation = new_derivation

        # The sim should be initialized now
        async def check_for_launcher():
            try:
                sim = await SpendSim.create(db_path="./sim.db")
                sim_client = SimClient(sim)
                assert (
                    len(await sim_client.get_coin_records_by_parent_ids([new_derivation.prefarm_info.launcher_id])) > 0
                )
            finally:
                await sim.close()

        asyncio.get_event_loop().run_until_complete(check_for_launcher())

        result = runner.invoke(cli, ["sync", "--configuration", config_path, "--db-path", "./infos/"])

        sync_db_path = next(Path("./infos/").glob("sync (*).sqlite"))
        assert sync_db_path.exists()

        latest_singleton_record: SingletonRecord

        async def check_for_singleton_record() -> SingletonRecord:
            sync_store = await SyncStore.create(sync_db_path)
            try:
                singleton_record: Optional[SingletonRecord] = await sync_store.get_latest_singleton()
                assert singleton_record is not None
                return singleton_record
            finally:
                await sync_store.db_connection.close()

        latest_singleton_record = asyncio.get_event_loop().run_until_complete(check_for_singleton_record())

        # Test the other derive_root options
        result = runner.invoke(
            cli,
            [
                "derive_root",
                "--db-path",
                sync_db_path,
                "--pubkeys",
                ",".join(pubkey_files),
                "--initial-lock-level",
                uint32(3),
                "--validate-against",
                config_path,
            ],
        )
        assert "Configuration successfully validated" in result.output

        config_export_path = Path("./export.config")
        result = runner.invoke(
            cli,
            [
                "export_config",
                "--filename",
                config_export_path,
                "--db-path",
                sync_db_path,
            ],
        )
        with open(config_export_path, "rb") as file:
            assert RootDerivation.from_bytes(file.read()) == derivation

        # Test our p2_singleton address creator
        result = runner.invoke(
            cli,
            [
                "p2_address",
                "--db-path",
                sync_db_path,
                "--prefix",
                "test",
            ],
        )

        P2_SINGLETON: Program = construct_p2_singleton(new_derivation.prefarm_info.launcher_id)
        assert encode_puzzle_hash(P2_SINGLETON.get_tree_hash(), "test") in result.output

        # Create and sync some p2_singletons
        async def pay_singleton_and_pass_time():
            try:
                sim = await SpendSim.create(db_path="./sim.db")
                sim_client = SimClient(sim)
                acs_coin: Coin = next(
                    (
                        cr.coin
                        for cr in await sim_client.get_coin_records_by_puzzle_hashes(
                            [ACS_PH], include_spent_coins=False
                        )
                        if cr.coin.amount > STARTING_AMOUNT
                    )
                )
                await sim_client.push_tx(
                    SpendBundle(
                        [
                            CoinSpend(
                                acs_coin,
                                ACS,
                                Program.to(
                                    [
                                        [51, P2_SINGLETON.get_tree_hash(), STARTING_AMOUNT],
                                        [51, P2_SINGLETON.get_tree_hash(), 1],
                                    ]
                                ),
                            )
                        ],
                        G2Element(),
                    )
                )
                await sim.farm_block()
                sim.pass_time(derivation.prefarm_info.withdrawal_timelock)
                await sim.farm_block()
            finally:
                await sim.close()

        asyncio.get_event_loop().run_until_complete(pay_singleton_and_pass_time())

        result = runner.invoke(
            cli,
            [
                "sync",
                "--db-path",
                sync_db_path,
            ],
        )

        async def check_for_singleton_record_and_payments():
            sync_store = await SyncStore.create(sync_db_path)
            try:
                singleton_record: SingletonRecord = await sync_store.get_latest_singleton()
                assert singleton_record == latest_singleton_record
                p2_singletons: List[Coin] = await sync_store.get_p2_singletons()
                assert len(p2_singletons) == 2
            finally:
                await sync_store.db_connection.close()

        asyncio.get_event_loop().run_until_complete(check_for_singleton_record_and_payments())

        spend_bundle_out_path: str = "./unsigned_bundle"
        result = runner.invoke(
            cli,
            [
                "payment",
                "--filename",
                spend_bundle_out_path,
                "--db-path",
                sync_db_path,
                "--pubkeys",
                ",".join(pubkey_files[0:3]),
                "--amount",
                2,
                "--recipient-address",
                encode_puzzle_hash(bytes32([0] * 32), prefix="xch"),
                "--absorb-available-payments",
                "--amount-threshold",
                2,
            ],
        )

        # Do a little bit of a signing cermony
        withdrawal_bundle = read_unsigned_spend(spend_bundle_out_path)
        sigs: List[BLSSignature] = []
        for key in range(0, 3):
            se = BLSSecretExponent(secret_key_for_index(key))
            sigs.extend([si.signature for si in sign(withdrawal_bundle, [se])])
        _hsms_bundle = create_spend_bundle(withdrawal_bundle, sigs)
        signed_withdrawal_bundle = SpendBundle.from_bytes(bytes(_hsms_bundle))  # gotta convert from the hsms here

        # Push the tx
        result = runner.invoke(
            cli,
            [
                "push_tx",
                "--spend-bundle",
                bytes(signed_withdrawal_bundle).hex(),
                "--fee",
                100,
            ],
        )
        assert r"{'success': True, 'status': 'SUCCESS'}" in result.output

        # Sync up
        result = runner.invoke(
            cli,
            [
                "sync",
                "--db-path",
                sync_db_path,
            ],
        )

        ach_payment: ACHRecord

        async def check_for_singleton_record_and_payment() -> Tuple[SingletonRecord, ACHRecord]:
            sync_store = await SyncStore.create(sync_db_path)
            try:
                singleton_record: Optional[SingletonRecord] = await sync_store.get_latest_singleton()
                assert singleton_record is not None
                assert singleton_record != latest_singleton_record
                # +1 from the launch, -2 from the payment
                assert singleton_record.coin.amount == STARTING_AMOUNT - 1

                p2_singletons: List[Coin] = await sync_store.get_p2_singletons()
                assert len(p2_singletons) == 1

                ach_record: ACHRecord = (await sync_store.get_ach_records(include_completed_coins=False))[0]

                return singleton_record, ach_record
            finally:
                await sync_store.db_connection.close()

        latest_singleton_record, ach_payment = asyncio.get_event_loop().run_until_complete(
            check_for_singleton_record_and_payment()
        )

        # Attempt a rekey
        new_derivation: RootDerivation = calculate_puzzle_root(
            derivation.prefarm_info,
            pubkeys,
            # mess with the key quantities
            uint32(2),
            uint32(4),
            uint32(2),
        )
        new_derivation_filepath: str = "./new_derivation.txt"
        with open(new_derivation_filepath, "wb") as file:
            file.write(bytes(new_derivation))

        result = runner.invoke(
            cli,
            [
                "start_rekey",
                "--filename",
                spend_bundle_out_path,
                "--db-path",
                sync_db_path,
                "--pubkeys",
                ",".join(pubkey_files[0:3]),
                "--new-configuration",
                new_derivation_filepath,
            ],
        )

        # Do a little bit of a signing cermony
        rekey_bundle = read_unsigned_spend(spend_bundle_out_path)
        sigs: List[BLSSignature] = [
            si.signature
            for si in sign(rekey_bundle, [BLSSecretExponent(secret_key_for_index(key)) for key in range(0, 3)])
        ]
        _hsms_bundle = create_spend_bundle(rekey_bundle, sigs)
        signed_rekey_bundle = SpendBundle.from_bytes(bytes(_hsms_bundle))  # gotta convert from the hsms here

        # Push the tx
        async def fast_forward():
            try:
                sim = await SpendSim.create(db_path="./sim.db")
                sim.pass_time(derivation.prefarm_info.rekey_increments)
                await sim.farm_block()
            finally:
                await sim.close()

        asyncio.get_event_loop().run_until_complete(fast_forward())

        result = runner.invoke(
            cli,
            [
                "push_tx",
                "--spend-bundle",
                bytes(signed_rekey_bundle).hex(),
                "--fee",
                100,
            ],
        )
        assert r"{'success': True, 'status': 'SUCCESS'}" in result.output

        # Sync up
        result = runner.invoke(
            cli,
            [
                "sync",
                "--db-path",
                sync_db_path,
            ],
        )

        rekey_drop: RekeyRecord

        async def check_for_singleton_record_and_rekey() -> Tuple[SingletonRecord, RekeyRecord]:
            sync_store = await SyncStore.create(sync_db_path)
            try:
                singleton_record: Optional[SingletonRecord] = await sync_store.get_latest_singleton()
                assert singleton_record is not None
                assert singleton_record != latest_singleton_record
                rekey_record: RekeyRecord = (await sync_store.get_rekey_records(include_completed_coins=False))[0]
                return singleton_record, rekey_record
            finally:
                await sync_store.db_connection.close()

        latest_singleton_record, rekey_drop = asyncio.get_event_loop().run_until_complete(
            check_for_singleton_record_and_rekey()
        )

        db_backup_path = "./db_backup.sqlite"

        async def set_rewind_height() -> uint32:
            try:
                with open(db_backup_path, "wb") as backup:
                    with open(sync_db_path, "rb") as file:
                        backup.write(file.read())

                sim = await SpendSim.create(db_path="./sim.db")
                return sim.block_height
            finally:
                await sim.close()

        REWIND_HERE: uint32 = asyncio.get_event_loop().run_until_complete(set_rewind_height())

        # Attempt to claw back both coins
        for i in range(0, 2):
            result = runner.invoke(
                cli,
                [
                    "clawback",
                    "--filename",
                    spend_bundle_out_path,
                    "--db-path",
                    sync_db_path,
                    "--pubkeys",
                    ",".join(pubkey_files[0:3]),
                ],
                input="1\n",
            )

            # Do a little bit of a signing cermony
            clawback_bundle = read_unsigned_spend(spend_bundle_out_path)
            sigs: List[BLSSignature] = [
                si.signature
                for si in sign(clawback_bundle, [BLSSecretExponent(secret_key_for_index(key)) for key in range(0, 3)])
            ]
            _hsms_bundle = create_spend_bundle(clawback_bundle, sigs)
            signed_clawback_bundle = SpendBundle.from_bytes(bytes(_hsms_bundle))  # gotta convert from the hsms here

            # Push the tx
            result = runner.invoke(
                cli,
                [
                    "push_tx",
                    "--spend-bundle",
                    bytes(signed_clawback_bundle).hex(),
                    "--fee",
                    100,
                ],
            )
            assert r"{'success': True, 'status': 'SUCCESS'}" in result.output

            # Sync up
            result = runner.invoke(
                cli,
                [
                    "sync",
                    "--db-path",
                    sync_db_path,
                ],
            )

            async def check_for_spent_clawback():
                sync_store = await SyncStore.create(sync_db_path)
                try:
                    if i == 0:
                        records = await sync_store.get_ach_records(include_completed_coins=True)
                    else:
                        records = await sync_store.get_rekey_records(include_completed_coins=True)
                    assert len(records) == 1
                    assert not records[0].completed
                finally:
                    await sync_store.db_connection.close()

            asyncio.get_event_loop().run_until_complete(check_for_spent_clawback())

        async def rewind():
            try:
                sim = await SpendSim.create(db_path="./sim.db")
                await sim.rewind(REWIND_HERE)
                sim.pass_time(max(prefarm_info.payment_clawback_period, prefarm_info.rekey_clawback_period))
                await sim.farm_block()
                with open(db_backup_path, "rb") as backup:
                    with open(sync_db_path, "wb") as file:
                        file.write(backup.read())
            finally:
                await sim.close()

        asyncio.get_event_loop().run_until_complete(rewind())

        # Attempt to complete both coins
        for i in range(0, 2):
            result = runner.invoke(
                cli,
                [
                    "complete",
                    "--filename",
                    spend_bundle_out_path,
                    "--db-path",
                    sync_db_path,
                ],
                input="1\n",
            )

            with open(spend_bundle_out_path, "r") as file:
                completion_bundle = SpendBundle.from_bytes(bytes.fromhex(file.read()))

            # Push the tx
            result = runner.invoke(
                cli,
                [
                    "push_tx",
                    "--spend-bundle",
                    bytes(completion_bundle).hex(),
                    "--fee",
                    100,
                ],
            )
            assert r"{'success': True, 'status': 'SUCCESS'}" in result.output

            # Sync up
            result = runner.invoke(
                cli,
                [
                    "sync",
                    "--db-path",
                    sync_db_path,
                ],
            )

            async def check_for_spent_completion():
                sync_store = await SyncStore.create(sync_db_path)
                try:
                    if i == 0:
                        records = await sync_store.get_ach_records(include_completed_coins=True)
                    else:
                        records = await sync_store.get_rekey_records(include_completed_coins=True)
                    assert len(records) == 1
                    assert records[0].completed
                finally:
                    await sync_store.db_connection.close()

            asyncio.get_event_loop().run_until_complete(check_for_spent_completion())

        # Check to see that updating the config works
        result = runner.invoke(
            cli,
            [
                "update_config",
                "--configuration",
                new_derivation_filepath,
                "--db-path",
                sync_db_path,
            ],
        )
        assert "Configuration update successful" in result.output

        result = runner.invoke(
            cli,
            [
                "update_config",
                "--configuration",
                new_derivation_filepath,
                "--db-path",
                sync_db_path,
            ],
        )
        assert "The configuration of this sync DB is not outdated" in result.output

        async def rewind_again():
            try:
                sim = await SpendSim.create(db_path="./sim.db")
                await sim.rewind(REWIND_HERE)
                sim.pass_time(prefarm_info.payment_clawback_period)
                with open(db_backup_path, "rb") as backup:
                    with open(sync_db_path, "wb") as file:
                        file.write(backup.read())
            finally:
                await sim.close()

        asyncio.get_event_loop().run_until_complete(rewind_again())

        # Try a lock level increase
        result = runner.invoke(
            cli,
            [
                "increase_security_level",
                "--filename",
                spend_bundle_out_path,
                "--db-path",
                sync_db_path,
                "--pubkeys",
                ",".join(pubkey_files[0:4]),
            ],
        )

        # Do a little bit of a signing cermony
        lock_bundle = read_unsigned_spend(spend_bundle_out_path)
        sigs: List[BLSSignature] = [
            si.signature
            for si in sign(lock_bundle, [BLSSecretExponent(secret_key_for_index(key)) for key in range(0, 4)])
        ]
        _hsms_bundle = create_spend_bundle(lock_bundle, sigs)
        signed_lock_bundle = SpendBundle.from_bytes(bytes(_hsms_bundle))  # gotta convert from the hsms here

        # Push the tx
        result = runner.invoke(
            cli,
            [
                "push_tx",
                "--spend-bundle",
                bytes(signed_lock_bundle).hex(),
                "--fee",
                100,
            ],
        )
        assert r"{'success': True, 'status': 'SUCCESS'}" in result.output

        result = runner.invoke(
            cli,
            [
                "sync",
                "--db-path",
                sync_db_path,
            ],
        )

        # Try completing the payment after a rekey
        result = runner.invoke(
            cli,
            [
                "complete",
                "--filename",
                spend_bundle_out_path,
                "--db-path",
                sync_db_path,
            ],
            input="1\n",
        )

        with open(spend_bundle_out_path, "r") as file:
            completion_bundle = SpendBundle.from_bytes(bytes.fromhex(file.read()))

        # Push the tx
        result = runner.invoke(
            cli,
            [
                "push_tx",
                "--spend-bundle",
                bytes(completion_bundle).hex(),
                "--fee",
                100,
            ],
        )
        assert r"{'success': True, 'status': 'SUCCESS'}" in result.output

        # Sync up
        result = runner.invoke(
            cli,
            [
                "sync",
                "--db-path",
                sync_db_path,
            ],
        )

        async def check_for_spent_completion_again():
            sync_store = await SyncStore.create(sync_db_path)
            try:
                records = await sync_store.get_ach_records(include_completed_coins=True)
                assert len(records) == 1
                assert records[0].completed
            finally:
                await sync_store.db_connection.close()

        asyncio.get_event_loop().run_until_complete(check_for_spent_completion_again())

        # Try a slow rekey
        async def fast_forward_slow_rekey():
            try:
                sim = await SpendSim.create(db_path="./sim.db")
                sim.pass_time(prefarm_info.rekey_increments * 2 + prefarm_info.slow_rekey_timelock)
                await sim.farm_block()
            finally:
                await sim.close()

        asyncio.get_event_loop().run_until_complete(fast_forward_slow_rekey())

        result = runner.invoke(
            cli,
            [
                "start_rekey",
                "--filename",
                spend_bundle_out_path,
                "--db-path",
                sync_db_path,
                "--pubkeys",
                ",".join(pubkey_files[0:3]),
                "--new-configuration",
                new_derivation_filepath,
            ],
        )

        slow_bundle = read_unsigned_spend(spend_bundle_out_path)
        sigs: List[BLSSignature] = [
            si.signature
            for si in sign(slow_bundle, [BLSSecretExponent(secret_key_for_index(key)) for key in range(0, 3)])
        ]
        _hsms_bundle = create_spend_bundle(slow_bundle, sigs)
        signed_slow_bundle = SpendBundle.from_bytes(bytes(_hsms_bundle))  # gotta convert from the hsms here

        result = runner.invoke(
            cli,
            [
                "push_tx",
                "--spend-bundle",
                bytes(signed_slow_bundle).hex(),
                "--fee",
                100,
            ],
        )
        assert r"{'success': True, 'status': 'SUCCESS'}" in result.output

        async def fast_forward_rekey_timelock():
            try:
                sim = await SpendSim.create(db_path="./sim.db")
                sim.pass_time(prefarm_info.rekey_clawback_period)
                await sim.farm_block()
            finally:
                await sim.close()

        asyncio.get_event_loop().run_until_complete(fast_forward_rekey_timelock())

        result = runner.invoke(
            cli,
            [
                "sync",
                "--db-path",
                sync_db_path,
            ],
        )

        result = runner.invoke(
            cli,
            [
                "complete",
                "--filename",
                spend_bundle_out_path,
                "--db-path",
                sync_db_path,
            ],
            input="1\n",
        )

        with open(spend_bundle_out_path, "r") as file:
            completion_bundle = SpendBundle.from_bytes(bytes.fromhex(file.read()))

        # Push the tx
        result = runner.invoke(
            cli,
            [
                "push_tx",
                "--spend-bundle",
                bytes(completion_bundle).hex(),
                "--fee",
                100,
            ],
        )
        assert r"{'success': True, 'status': 'SUCCESS'}" in result.output
