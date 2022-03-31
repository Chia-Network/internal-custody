import asyncio
import os
import re

from blspy import BasicSchemeMPL, PrivateKey, G2Element
from click.testing import CliRunner, Result
from pathlib import Path
from typing import List, Optional, Tuple

from chia.clvm.spend_sim import SpendSim, SimClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import CoinSpend
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.types.spend_bundle import SpendBundle
from chia.util.bech32m import encode_puzzle_hash
from chia.util.hash import std_hash
from chia.util.ints import uint32, uint64

from cic.cli.main import cli
from cic.cli.record_types import SingletonRecord, ACHRecord, RekeyRecord
from cic.cli.sync_store import SyncStore
from cic.drivers.prefarm_info import PrefarmInfo
from cic.drivers.puzzle_root_construction import RootDerivation, calculate_puzzle_root
from cic.drivers.singleton import construct_p2_singleton

from hsms.bls12_381 import BLSSecretExponent, BLSSignature
from hsms.cmds.hsmmerge import create_spend_bundle
from hsms.process.sign import sign
from hsms.process.unsigned_spend import UnsignedSpend


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
        uint64(0),
        uint64(1000000000000),
        uint64(1),
        bytes32([0] * 32),
        uint64(30),
        uint64(90),
        uint64(30),
        uint64(45),
        uint64(15),
    )

    with runner.isolated_filesystem():
        os.mkdir("infos")
        result: Result = runner.invoke(cli, ["init"])
        assert result.exit_code != 0

        # Initialize the configuration
        result = runner.invoke(
            cli,
            [
                "init",
                "--filepath",
                "./infos/",
                "--date",
                prefarm_info.start_date,
                "--rate",
                prefarm_info.mojos_per_second,
                "--amount",
                prefarm_info.starting_amount,  # 1 XCH
                "--withdrawal-timelock",
                prefarm_info.withdrawal_timelock,
                "--rekey-timelock",
                prefarm_info.rekey_increments,
                "--slow-penalty",
                prefarm_info.slow_rekey_timelock,
                "--payment-clawback",
                prefarm_info.payment_clawback_period,
                "--rekey-cancel",
                prefarm_info.rekey_clawback_period,
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
        pubkeys_as_hex = [bytes(pk).hex() for pk in pubkeys]
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
                ",".join(pubkeys_as_hex),
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

        result = runner.invoke(
            cli,
            ["sync", "--configuration", config_path, "--db-path", "./infos/"],
        )

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

        # Test our p2_singleton address creator
        result = runner.invoke(
            cli,
            [
                "p2_address",
                "--configuration",
                config_path,
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
                        if cr.coin.amount > prefarm_info.starting_amount
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
                                        [51, P2_SINGLETON.get_tree_hash(), prefarm_info.starting_amount],
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
                "--configuration",
                config_path,
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

        result = runner.invoke(
            cli,
            [
                "payment",
                "--configuration",
                config_path,
                "--db-path",
                sync_db_path,
                "--pubkeys",
                ",".join(pubkeys_as_hex[0:3]),
                "--amount",
                2,
                "--recipient-address",
                encode_puzzle_hash(ACS_PH, prefix="xch"),
                "--absorb-available-payments",
                "--amount-threshold",
                2,
            ],
        )

        # Do a little bit of a signing cermony
        withdrawal_bundle = UnsignedSpend.from_bytes(bytes.fromhex(result.output))
        sigs: List[BLSSignature] = []
        for key in range(0, 3):
            se = BLSSecretExponent(secret_key_for_index(key))
            sigs.extend([si.signature for si in sign(withdrawal_bundle, [se])])
        _hsms_bundle = create_spend_bundle(withdrawal_bundle, sigs)
        signed_withdrawal_bundle = SpendBundle.from_bytes(bytes(_hsms_bundle))  # gotta convert from the hsms here

        # Push the tx
        async def push_withdrawal_spend():
            try:
                sim = await SpendSim.create(db_path="./sim.db")
                sim_client = SimClient(sim)
                result = await sim_client.push_tx(signed_withdrawal_bundle)
                assert result[0] == MempoolInclusionStatus.SUCCESS
                await sim.farm_block()
            finally:
                await sim.close()

        asyncio.get_event_loop().run_until_complete(push_withdrawal_spend())

        # Sync up
        result = runner.invoke(
            cli,
            [
                "sync",
                "--configuration",
                config_path,
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
                assert singleton_record.coin.amount == prefarm_info.starting_amount - 1
                ach_record: ACHRecord = (await sync_store.get_ach_records(include_spent_coins=False))[0]
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
                "--configuration",
                config_path,
                "--db-path",
                sync_db_path,
                "--pubkeys",
                ",".join(pubkeys_as_hex[0:3]),
                "--new-configuration",
                new_derivation_filepath,
            ],
        )

        # Do a little bit of a signing cermony
        rekey_bundle = UnsignedSpend.from_bytes(bytes.fromhex(result.output))
        sigs: List[BLSSignature] = [
            si.signature
            for si in sign(rekey_bundle, [BLSSecretExponent(secret_key_for_index(key)) for key in range(0, 3)])
        ]
        _hsms_bundle = create_spend_bundle(rekey_bundle, sigs)
        signed_rekey_bundle = SpendBundle.from_bytes(bytes(_hsms_bundle))  # gotta convert from the hsms here

        # Push the tx
        async def push_start_rekey_spend():
            try:
                sim = await SpendSim.create(db_path="./sim.db")
                sim_client = SimClient(sim)
                sim.pass_time(prefarm_info.rekey_increments)
                await sim.farm_block()
                result = await sim_client.push_tx(signed_rekey_bundle)
                assert result[0] == MempoolInclusionStatus.SUCCESS
                await sim.farm_block()
            finally:
                await sim.close()

        asyncio.get_event_loop().run_until_complete(push_start_rekey_spend())

        # Sync up
        result = runner.invoke(
            cli,
            [
                "sync",
                "--configuration",
                config_path,
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
                rekey_record: RekeyRecord = (await sync_store.get_rekey_records(include_spent_coins=False))[0]
                return singleton_record, rekey_record
            finally:
                await sync_store.db_connection.close()

        latest_singleton_record, rekey_drop = asyncio.get_event_loop().run_until_complete(
            check_for_singleton_record_and_rekey()
        )

        async def set_rewind_height() -> uint32:
            try:
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
                    "--configuration",
                    config_path,
                    "--db-path",
                    sync_db_path,
                    "--pubkeys",
                    ",".join(pubkeys_as_hex[0:3]),
                ],
                input="1\n",
            )

            end_of_output_hex = re.compile("[a-f0-9]+$")
            spend_hex = re.search(end_of_output_hex, result.output).group(0)
            # Do a little bit of a signing cermony
            clawback_bundle = UnsignedSpend.from_bytes(bytes.fromhex(spend_hex))
            sigs: List[BLSSignature] = [
                si.signature
                for si in sign(clawback_bundle, [BLSSecretExponent(secret_key_for_index(key)) for key in range(0, 3)])
            ]
            _hsms_bundle = create_spend_bundle(clawback_bundle, sigs)
            signed_clawback_bundle = SpendBundle.from_bytes(bytes(_hsms_bundle))  # gotta convert from the hsms here

            # Push the tx
            async def push_clawback_spend():
                try:
                    sim = await SpendSim.create(db_path="./sim.db")
                    sim_client = SimClient(sim)
                    result = await sim_client.push_tx(signed_clawback_bundle)
                    assert result[0] == MempoolInclusionStatus.SUCCESS
                    await sim.farm_block()
                finally:
                    await sim.close()

            asyncio.get_event_loop().run_until_complete(push_clawback_spend())

            # Sync up
            result = runner.invoke(
                cli,
                [
                    "sync",
                    "--configuration",
                    config_path,
                    "--db-path",
                    sync_db_path,
                ],
            )

            async def check_for_spent_record():
                sync_store = await SyncStore.create(sync_db_path)
                try:
                    if i == 0:
                        records = await sync_store.get_ach_records(include_spent_coins=True)
                    else:
                        records = await sync_store.get_rekey_records(include_spent_coins=True)
                    assert len(records) == 1
                    assert not records[0].completed
                finally:
                    await sync_store.db_connection.close()

            asyncio.get_event_loop().run_until_complete(check_for_spent_record())

        async def rewind():
            try:
                sync_store = await SyncStore.create(sync_db_path)
                sim = await SpendSim.create(db_path="./sim.db")
                await sim.rewind(REWIND_HERE)
                sim.pass_time(max(prefarm_info.payment_clawback_period, prefarm_info.rekey_clawback_period))
                await sim.farm_block()
                await sync_store.db_wrapper.begin_transaction()
                await sync_store.add_ach_record(ach_payment)
                await sync_store.add_rekey_record(rekey_drop)
                await sync_store.db_wrapper.commit_transaction()
            finally:
                await sync_store.db_connection.close()
                await sim.close()

        asyncio.get_event_loop().run_until_complete(rewind())

        # Attempt to complete both coins
        for i in range(0, 2):
            result = runner.invoke(
                cli,
                [
                    "complete",
                    "--configuration",
                    config_path,
                    "--db-path",
                    sync_db_path,
                ],
                input="1\n",
            )

            end_of_output_hex = re.compile("[a-f0-9]+$")
            spend_hex = re.search(end_of_output_hex, result.output).group(0)
            completion_bundle = SpendBundle.from_bytes(bytes.fromhex(spend_hex))

            # Push the tx
            async def push_completion_spend():
                try:
                    sim = await SpendSim.create(db_path="./sim.db")
                    sim_client = SimClient(sim)
                    result = await sim_client.push_tx(completion_bundle)
                    assert result[0] == MempoolInclusionStatus.SUCCESS
                    await sim.farm_block()
                finally:
                    await sim.close()

            asyncio.get_event_loop().run_until_complete(push_completion_spend())

            # Sync up
            result = runner.invoke(
                cli,
                [
                    "sync",
                    "--configuration",
                    config_path,
                    "--db-path",
                    sync_db_path,
                ],
            )

            async def check_for_spent_record():
                sync_store = await SyncStore.create(sync_db_path)
                try:
                    if i == 0:
                        records = await sync_store.get_ach_records(include_spent_coins=True)
                    else:
                        records = await sync_store.get_rekey_records(include_spent_coins=True)
                    assert len(records) == 1
                    assert records[0].completed
                finally:
                    await sync_store.db_connection.close()

            asyncio.get_event_loop().run_until_complete(check_for_spent_record())
