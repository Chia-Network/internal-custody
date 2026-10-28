import asyncio
import os

from chia_rs import AugSchemeMPL, G2Element, PrivateKey, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint32, uint64
from click.testing import CliRunner, Result
from pathlib import Path
from typing import List, Optional, Tuple

from chia._tests.util.spend_sim import SpendSim, SimClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import INFINITE_COST, Program
from chia.types.condition_opcodes import ConditionOpcode
from chia.types.coin_spend import make_spend
from chia.util.bech32m import encode_puzzle_hash
from chia.util.hash import std_hash

from cic.cli.fees import find_fee_announcement
from cic.cli.main import cli, read_unsigned_spend
from cic.cli.record_types import SingletonRecord, ACHRecord, RekeyRecord
from cic.cli.sync_store import SyncStore
from cic.drivers.prefarm_info import PrefarmInfo
from cic.drivers.puzzle_root_construction import RootDerivation, calculate_puzzle_root
from cic.drivers.singleton import construct_p2_singleton

from chia_base.bls12_381 import BLSPublicKey, BLSSecretExponent, BLSSignature
from chia_base.cbincode import to_bytes
from hsms.cmds.hsmmerge import create_spend_bundle
from hsms.process.sign import sign


# Key functions
def secret_key_for_index(index: int) -> PrivateKey:
    blob = index.to_bytes(32, "big")
    hashed_blob = AugSchemeMPL.key_gen(std_hash(b"foo" + blob))
    secret_exponent = int.from_bytes(hashed_blob.to_bytes(), "big")
    return PrivateKey.from_bytes(secret_exponent.to_bytes(32, "big"))


ACS: Program = Program.to(1)
ACS_PH: bytes32 = ACS.get_tree_hash()


def test_help():
    runner = CliRunner()
    result: Result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Commands to control a prefarm singleton" in result.output


def test_init(monkeypatch):
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
            async with SpendSim.managed(Path("./sim.db")) as sim:
                sim_client = SimClient(sim)
                assert (
                    len(await sim_client.get_coin_records_by_parent_ids([new_derivation.prefarm_info.launcher_id])) > 0
                )

        asyncio.run(check_for_launcher())

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
                await sync_store.db_wrapper.close()

        latest_singleton_record = asyncio.run(check_for_singleton_record())

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
            async with SpendSim.managed(Path("./sim.db")) as sim:
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
                            make_spend(
                                acs_coin,
                                ACS,
                                Program.to(
                                    [
                                        [51, P2_SINGLETON.get_tree_hash(), STARTING_AMOUNT],
                                        [51, P2_SINGLETON.get_tree_hash(), 1],
                                        [51, ACS_PH, acs_coin.amount - STARTING_AMOUNT - 1],
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

        asyncio.run(pay_singleton_and_pass_time())

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
                await sync_store.db_wrapper.close()

        asyncio.run(check_for_singleton_record_and_payments())

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
        signed_withdrawal_bundle = SpendBundle.from_bytes(to_bytes(_hsms_bundle))  # gotta convert from the hsms here

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
                await sync_store.db_wrapper.close()

        latest_singleton_record, ach_payment = asyncio.run(check_for_singleton_record_and_payment())

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
        signed_rekey_bundle = SpendBundle.from_bytes(to_bytes(_hsms_bundle))  # gotta convert from the hsms here

        # Push the tx
        async def fast_forward():
            async with SpendSim.managed(Path("./sim.db")) as sim:
                sim.pass_time(derivation.prefarm_info.rekey_increments)
                await sim.farm_block()

        asyncio.run(fast_forward())

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
                await sync_store.db_wrapper.close()

        latest_singleton_record, rekey_drop = asyncio.run(check_for_singleton_record_and_rekey())

        db_backup_path = "./db_backup.sqlite"

        async def set_rewind_height() -> uint32:
            with open(db_backup_path, "wb") as backup:
                with open(sync_db_path, "rb") as file:
                    backup.write(file.read())

            async with SpendSim.managed(Path("./sim.db")) as sim:
                return sim.block_height

        REWIND_HERE: uint32 = asyncio.run(set_rewind_height())

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
            signed_clawback_bundle = SpendBundle.from_bytes(to_bytes(_hsms_bundle))  # gotta convert from the hsms here

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
                    await sync_store.db_wrapper.close()

            asyncio.run(check_for_spent_clawback())

        async def rewind():
            async with SpendSim.managed(Path("./sim.db")) as sim:
                await sim.rewind(REWIND_HERE)
                sim.pass_time(max(prefarm_info.payment_clawback_period, prefarm_info.rekey_clawback_period))
                await sim.farm_block()
                with open(db_backup_path, "rb") as backup:
                    with open(sync_db_path, "wb") as file:
                        file.write(backup.read())

        asyncio.run(rewind())

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
                    await sync_store.db_wrapper.close()

            asyncio.run(check_for_spent_completion())

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
            async with SpendSim.managed(Path("./sim.db")) as sim:
                await sim.rewind(REWIND_HERE)
                sim.pass_time(prefarm_info.payment_clawback_period)
                with open(db_backup_path, "rb") as backup:
                    with open(sync_db_path, "wb") as file:
                        file.write(backup.read())

        asyncio.run(rewind_again())

        from tests.cli_clients import FullNodeClientMock, WalletClientMock

        pushed_bundles: List[SpendBundle] = []
        fee_bundles: List[SpendBundle] = []
        original_push_tx = FullNodeClientMock.push_tx
        original_create_signed_transactions = WalletClientMock.create_signed_transactions

        async def record_push_tx(self, spend_bundle):
            pushed_bundles.append(spend_bundle)
            return await original_push_tx(self, spend_bundle)

        async def record_create_signed_transactions(self, request, tx_config, extra_conditions=()):
            tx_response = await original_create_signed_transactions(self, request, tx_config, extra_conditions)
            fee_bundles.append(tx_response.signed_txs[0].spend_bundle)
            return tx_response

        monkeypatch.setattr(FullNodeClientMock, "push_tx", record_push_tx)
        monkeypatch.setattr(WalletClientMock, "create_signed_transactions", record_create_signed_transactions)

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
        signed_lock_bundle = SpendBundle.from_bytes(to_bytes(_hsms_bundle))  # gotta convert from the hsms here

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
        assert "This spend bundle must be pushed in two steps." in result.output
        assert "Step 1: non-ephemeral spends" in result.output
        assert "Step 2: ephemeral spends" in result.output
        assert result.output.count(r"{'success': True, 'status': 'SUCCESS'}") == 2

        # Both steps carry a fee.  Step 2's fee is still linked to the lock coin
        # completion announcement; step 1's fee is linked to step 1's first spend.
        assert len(pushed_bundles) == 2
        assert len(fee_bundles) == 2

        def _coin_ids(bundle: SpendBundle) -> set:
            return {coin_spend.coin.name() for coin_spend in bundle.coin_spends}

        def _fee_conditions(bundle: SpendBundle) -> List:
            all_conditions: List = []
            for coin_spend in bundle.coin_spends:
                _, conditions = Program.from_bytes(bytes(coin_spend.puzzle_reveal)).run_with_cost(
                    INFINITE_COST, Program.from_bytes(bytes(coin_spend.solution))
                )
                all_conditions.extend(conditions.as_python())
            return all_conditions

        def _bundle_with_fee(fee_bundle: SpendBundle) -> SpendBundle:
            fee_coin_ids = _coin_ids(fee_bundle)
            matching = [bundle for bundle in pushed_bundles if fee_coin_ids <= _coin_ids(bundle)]
            assert len(matching) == 1
            return matching[0]

        step_one_bundle = _bundle_with_fee(fee_bundles[0])
        step_two_bundle = _bundle_with_fee(fee_bundles[1])
        assert step_one_bundle != step_two_bundle

        announcement = find_fee_announcement(signed_lock_bundle)
        assert announcement is not None
        assert announcement.asserted_id is not None
        assert announcement.asserted_msg is not None
        expected_announcement_id = std_hash(announcement.asserted_id + announcement.asserted_msg)

        assert any(
            condition[0] == ConditionOpcode.ASSERT_COIN_ANNOUNCEMENT.value
            and bytes32(condition[1]) == expected_announcement_id
            for condition in _fee_conditions(fee_bundles[1])
        )
        assert announcement.asserted_id in _coin_ids(step_two_bundle)

        assert any(
            condition[0] == ConditionOpcode.ASSERT_CONCURRENT_SPEND.value
            and bytes32(condition[1]) == step_one_bundle.coin_spends[0].coin.name()
            for condition in _fee_conditions(fee_bundles[0])
        )

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
                await sync_store.db_wrapper.close()

        asyncio.run(check_for_spent_completion_again())

        # Try a slow rekey
        async def fast_forward_slow_rekey():
            async with SpendSim.managed(Path("./sim.db")) as sim:
                sim.pass_time(prefarm_info.rekey_increments * 2 + prefarm_info.slow_rekey_timelock)
                await sim.farm_block()

        asyncio.run(fast_forward_slow_rekey())

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
        signed_slow_bundle = SpendBundle.from_bytes(to_bytes(_hsms_bundle))  # gotta convert from the hsms here

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
            async with SpendSim.managed(Path("./sim.db")) as sim:
                sim.pass_time(prefarm_info.rekey_clawback_period)
                await sim.farm_block()

        asyncio.run(fast_forward_rekey_timelock())

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
