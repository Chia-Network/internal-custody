"""Split a spend bundle into the two pushes required by chia 2.7.5.

A lock level increase creates a bundle with three spends:

* the START_REKEY singleton spend,
* the rekey_completion spend of the zero amount lock coin created by START_REKEY,
* the FINISH_REKEY singleton spend of the new singleton coin created by START_REKEY.

The last two spend coins that are created by the START_REKEY spend in the same
bundle (ephemeral spends).  ``rekey_completion.clsp`` always emits
``ASSERT_SECONDS_RELATIVE FINISHING_TIMELOCK`` (which is 0 for a lock level
increase).  The chia 2.7.5 mempool rejects any relative height or seconds
condition on an ephemeral spend with ``Err.EPHEMERAL_RELATIVE_CONDITION``, so
the one-shot bundle cannot enter the mempool.

The bundle is therefore split into:

* step 1: all non-ephemeral spends, carrying the bundle's aggregated signature
  (the START_REKEY spend needs it),
* step 2: all ephemeral spends.

The two split bundles themselves need no signature for the ephemeral spends;
step 1 carries the aggregated signature.  ``split_two_step_push`` itself adds no
fee transaction.  Afterwards ``cic push_tx`` attaches a separate fee to each
step: the step that holds the announcement binds its fee to that coin
announcement, and the other step binds its fee with ``ASSERT_CONCURRENT_SPEND``
on its first spend.

The lock coin (0 mojo, created by START_REKEY in step 1) is confirmed on chain
for at least one block before FINISH_REKEY and rekey_completion are spent in
step 2, so between the two steps the new singleton and the lock coin exist on
chain.  If step 2 never lands, the lock coin stays pending on chain and never
expires; the CLI prints the exact step 2 bundle (hex) so it can be pushed later
with ``cic push_tx``.
"""

from dataclasses import dataclass
from typing import Any, FrozenSet, List, Set

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import INFINITE_COST, Program
from chia.types.condition_opcodes import ConditionOpcode
from chia.types.coin_spend import CoinSpend
from chia_rs import G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

# Relative time condition opcodes (the integer values are shown for reference).
ASSERT_SECONDS_RELATIVE = ConditionOpcode.ASSERT_SECONDS_RELATIVE.value  # 80
ASSERT_HEIGHT_RELATIVE = ConditionOpcode.ASSERT_HEIGHT_RELATIVE.value  # 82
ASSERT_BEFORE_SECONDS_RELATIVE = ConditionOpcode.ASSERT_BEFORE_SECONDS_RELATIVE.value  # 84
ASSERT_BEFORE_HEIGHT_RELATIVE = ConditionOpcode.ASSERT_BEFORE_HEIGHT_RELATIVE.value  # 86

# Aggregated signature condition opcodes (the integer values are shown for reference).
AGG_SIG_PARENT = ConditionOpcode.AGG_SIG_PARENT.value  # 43
AGG_SIG_PUZZLE = ConditionOpcode.AGG_SIG_PUZZLE.value  # 44
AGG_SIG_AMOUNT = ConditionOpcode.AGG_SIG_AMOUNT.value  # 45
AGG_SIG_PUZZLE_AMOUNT = ConditionOpcode.AGG_SIG_PUZZLE_AMOUNT.value  # 46
AGG_SIG_PARENT_AMOUNT = ConditionOpcode.AGG_SIG_PARENT_AMOUNT.value  # 47
AGG_SIG_PARENT_PUZZLE = ConditionOpcode.AGG_SIG_PARENT_PUZZLE.value  # 48
AGG_SIG_UNSAFE = ConditionOpcode.AGG_SIG_UNSAFE.value  # 49
AGG_SIG_ME = ConditionOpcode.AGG_SIG_ME.value  # 50

CREATE_COIN = ConditionOpcode.CREATE_COIN.value  # 51

RELATIVE_TIME_CONDITIONS: FrozenSet[bytes] = frozenset(
    (
        ASSERT_SECONDS_RELATIVE,
        ASSERT_HEIGHT_RELATIVE,
        ASSERT_BEFORE_SECONDS_RELATIVE,
        ASSERT_BEFORE_HEIGHT_RELATIVE,
    )
)

AGG_SIG_CONDITIONS: FrozenSet[bytes] = frozenset(
    (
        AGG_SIG_PARENT,
        AGG_SIG_PUZZLE,
        AGG_SIG_AMOUNT,
        AGG_SIG_PUZZLE_AMOUNT,
        AGG_SIG_PARENT_AMOUNT,
        AGG_SIG_PARENT_PUZZLE,
        AGG_SIG_UNSAFE,
        AGG_SIG_ME,
    )
)


@dataclass(frozen=True)
class TwoStepBundles:
    first: SpendBundle
    second: SpendBundle


def _conditions_for_spend(coin_spend: CoinSpend) -> List[Any]:
    puzzle_reveal = Program.from_bytes(bytes(coin_spend.puzzle_reveal))
    solution = Program.from_bytes(bytes(coin_spend.solution))
    _, conditions = puzzle_reveal.run_with_cost(INFINITE_COST, solution)
    return list(conditions.as_python())


def _created_coin_ids(coin_spend: CoinSpend, conditions: List[Any]) -> Set[bytes32]:
    created: Set[bytes32] = set()
    for condition in conditions:
        if condition[0] == CREATE_COIN:
            created.add(
                Coin(
                    coin_spend.coin.name(),
                    bytes32(condition[1]),
                    uint64(int.from_bytes(condition[2], "big")),
                ).name()
            )
    return created


def _spend_conditions(bundle: SpendBundle) -> List[List[Any]]:
    return [_conditions_for_spend(coin_spend) for coin_spend in bundle.coin_spends]


def _ephemeral_coin_ids(bundle: SpendBundle, all_conditions: List[List[Any]]) -> Set[bytes32]:
    spent_coin_ids = {coin_spend.coin.name() for coin_spend in bundle.coin_spends}
    created_coin_ids: Set[bytes32] = set()
    for coin_spend, conditions in zip(bundle.coin_spends, all_conditions):
        created_coin_ids.update(_created_coin_ids(coin_spend, conditions))
    return spent_coin_ids & created_coin_ids


def _has_relative_time_condition(conditions: List[Any]) -> bool:
    return any(condition[0] in RELATIVE_TIME_CONDITIONS for condition in conditions)


def _has_agg_sig_condition(conditions: List[Any]) -> bool:
    return any(condition[0] in AGG_SIG_CONDITIONS for condition in conditions)


def needs_two_step_push(bundle: SpendBundle) -> bool:
    """Return True if an ephemeral spend in the bundle emits a relative time condition."""
    all_conditions = _spend_conditions(bundle)
    ephemeral_coin_ids = _ephemeral_coin_ids(bundle, all_conditions)
    if not ephemeral_coin_ids:
        return False
    for coin_spend, conditions in zip(bundle.coin_spends, all_conditions):
        if coin_spend.coin.name() in ephemeral_coin_ids and _has_relative_time_condition(conditions):
            return True
    return False


def split_two_step_push(bundle: SpendBundle) -> TwoStepBundles:
    """Split a bundle that needs a two-step push into its non-ephemeral and ephemeral spends."""
    if not needs_two_step_push(bundle):
        raise ValueError("This spend bundle does not contain an ephemeral spend with a relative time condition")

    all_conditions = _spend_conditions(bundle)
    ephemeral_coin_ids = _ephemeral_coin_ids(bundle, all_conditions)

    first_spends: List[CoinSpend] = []
    second_spends: List[CoinSpend] = []
    for coin_spend in bundle.coin_spends:
        if coin_spend.coin.name() in ephemeral_coin_ids:
            second_spends.append(coin_spend)
        else:
            first_spends.append(coin_spend)

    second = SpendBundle(second_spends, G2Element())
    for coin_spend, conditions in zip(bundle.coin_spends, all_conditions):
        if coin_spend.coin.name() in ephemeral_coin_ids and _has_agg_sig_condition(conditions):
            raise ValueError(
                "The ephemeral part of this spend bundle emits an aggregated signature condition, "
                "but the second step is pushed without a signature"
            )

    if needs_two_step_push(second):
        raise ValueError("The ephemeral part of this spend bundle is more than one level deep")

    first = SpendBundle(first_spends, bundle.aggregated_signature)
    return TwoStepBundles(first=first, second=second)
