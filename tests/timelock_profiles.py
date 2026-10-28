"""Timing profiles and helpers for tests that exercise payment series."""

import contextlib

from dataclasses import dataclass
from typing import AsyncIterator, List, Tuple

import pytest

from chia_rs import AugSchemeMPL, G1Element, G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint32, uint64
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzles.singleton_top_layer import SINGLETON_LAUNCHER_HASH

from chia._tests.util.spend_sim import SimClient, SpendSim

from cic.drivers.prefarm import construct_singleton_inner_puzzle
from cic.drivers.prefarm_info import PrefarmInfo
from cic.drivers.puzzle_root_construction import RootDerivation, calculate_puzzle_root
from cic.drivers.singleton import construct_p2_singleton, generate_launch_conditions_and_coin_spend

from tests.test_lifecycle import (
    ACS,
    ACS_PH,
    get_synthetic_pubkey,
    secret_key_for_index,
    sign_message_at_index,
    sign_message_with_offset,
)


@dataclass(frozen=True)
class TimelockProfile:
    """Timing settings used by tests."""

    withdrawal_timelock: int
    payment_clawback_period: int
    rekey_clawback_period: int
    rekey_increments: int
    slow_rekey_timelock: int


# short and long timelock settings for tests
SHORT_TIMELOCKS = TimelockProfile(150, 450, 300, 75, 225)
LONG_TIMELOCKS = TimelockProfile(1000, 3000, 2000, 500, 1500)

PROFILES = pytest.mark.parametrize("profile", [SHORT_TIMELOCKS, LONG_TIMELOCKS], ids=["short", "long"])


def init_args(profile: TimelockProfile) -> List[str]:
    """Return the cic init arguments for a profile."""
    return [
        "init",
        "--directory",
        "./infos/",
        "--withdrawal-timelock",
        str(profile.withdrawal_timelock),
        "--payment-clawback",
        str(profile.payment_clawback_period),
        "--rekey-cancel",
        str(profile.rekey_clawback_period),
        "--rekey-timelock",
        str(profile.rekey_increments),
        "--slow-penalty",
        str(profile.slow_rekey_timelock),
    ]


def prefarm_info_for(profile: TimelockProfile, launcher_id: bytes32 = bytes32([0] * 32)) -> PrefarmInfo:
    """Return prefarm information with the profile's timings."""
    return PrefarmInfo(
        launcher_id,
        bytes32([0] * 32),
        uint64(profile.withdrawal_timelock),
        uint64(profile.payment_clawback_period),
        uint64(profile.rekey_clawback_period),
        uint64(profile.rekey_increments),
        uint64(profile.slow_rekey_timelock),
    )


@dataclass(frozen=True)
class LaunchedWithProfile:
    """A launched singleton and the objects used to drive it."""

    sim: SpendSim
    sim_client: SimClient
    derivation: RootDerivation
    singleton: Coin
    lineage: LineageProof


@contextlib.asynccontextmanager
async def launch_with_profile(profile: TimelockProfile, m: int = 3, n: int = 5) -> AsyncIterator[LaunchedWithProfile]:
    """Launch a singleton whose timings come from the profile."""
    async with SpendSim.managed() as sim:
        sim_client = SimClient(sim)
        await sim.farm_block(ACS_PH)

        pubkeys: List[G1Element] = [secret_key_for_index(i).get_g1() for i in range(n)]
        prefarm_coins = await sim_client.get_coin_records_by_puzzle_hashes([ACS_PH])
        big_coin = next(cr.coin for cr in prefarm_coins if cr.coin.amount == 18375000000000000000)
        small_coin = next(cr.coin for cr in prefarm_coins if cr.coin.amount == 2625000000000000000)

        starting_amount = uint64(18374999999999999999)
        launcher_coin = Coin(big_coin.name(), SINGLETON_LAUNCHER_HASH, starting_amount)
        derivation = calculate_puzzle_root(
            prefarm_info_for(profile, launcher_coin.name()),
            pubkeys,
            uint32(m),
            uint32(n),
            uint32(1),
        )
        conditions, launch_spend = generate_launch_conditions_and_coin_spend(
            big_coin, construct_singleton_inner_puzzle(derivation.prefarm_info), starting_amount
        )
        creation_bundle = SpendBundle(
            [
                make_spend(big_coin, ACS, Program.to(conditions)),
                make_spend(
                    small_coin,
                    ACS,
                    Program.to(
                        [[51, construct_p2_singleton(launch_spend.coin.name()).get_tree_hash(), small_coin.amount]]
                    ),
                ),
                launch_spend,
            ],
            G2Element(),
        )
        await sim_client.push_tx(creation_bundle)
        await sim.farm_block()

        all_coins = await sim.all_non_reward_coins()
        singleton = next(coin for coin in all_coins if coin.amount == starting_amount)
        yield LaunchedWithProfile(
            sim,
            sim_client,
            derivation,
            singleton,
            LineageProof(parent_name=launch_spend.coin.parent_coin_info, amount=launch_spend.coin.amount),
        )


def sign_payment(bundle: SpendBundle, data_to_sign: bytes, key_indices: Tuple[int, ...] = (0, 1, 2)) -> SpendBundle:
    """Sign a payment bundle with the synthetic key and the given keys."""
    pubkeys: List[G1Element] = [secret_key_for_index(index).get_g1() for index in key_indices]
    aggregate_pubkey = G1Element()
    for pubkey in pubkeys:
        aggregate_pubkey += pubkey
    synthetic_pubkey = get_synthetic_pubkey(aggregate_pubkey)
    signature = AugSchemeMPL.aggregate(
        [
            sign_message_with_offset(aggregate_pubkey, data_to_sign, synthetic_pubkey),
            *[sign_message_at_index(index, data_to_sign, synthetic_pubkey) for index in key_indices],
        ]
    )
    return SpendBundle.aggregate([bundle, SpendBundle([], signature)])
