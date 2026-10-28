from typing import Any, Dict, List

import pytest
import pytest_asyncio

from chia_rs import AugSchemeMPL, G1Element, G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend

from cic.cli.two_step_push import TwoStepPushError, push_in_two_steps
from cic.drivers.drop_coins import curry_ach_puzzle
from cic.drivers.prefarm import (
    get_ach_clawforward_spend_bundle,
    get_rekey_spend_info,
    get_withdrawal_spend_info,
)
from cic.drivers.puzzle_root_construction import calculate_puzzle_root
from cic.drivers.two_step_push import needs_two_step_push, split_two_step_push

from tests.test_lifecycle import _setup_info, secret_key_for_index, sign_message_at_index  # noqa: F401

ACS = Program.to(1)
ACS_PH = ACS.get_tree_hash()


@pytest_asyncio.fixture
async def setup_info(_setup_info):  # noqa: F811
    return _setup_info


class _FakeNodeClient:
    def __init__(self, push_results: List[Dict[str, Any]], records: Dict[bytes32, Any]) -> None:
        self._push_results = list(push_results)
        self._records = records
        self.pushed: List[SpendBundle] = []
        self.record_queries: List[bytes32] = []

    async def push_tx(self, spend_bundle: SpendBundle) -> Dict[str, Any]:
        self.pushed.append(spend_bundle)
        return self._push_results.pop(0)

    async def get_coin_record_by_name(self, coin_id: bytes32) -> Any:
        self.record_queries.append(coin_id)
        return self._records.get(coin_id)


def _simple_bundle(seed: int, amount: int = 100) -> SpendBundle:
    coin = Coin(bytes32([seed] * 32), ACS_PH, uint64(amount))
    return SpendBundle([make_spend(coin, ACS, Program.to([]))], G2Element())


def _lock_increase_bundle(setup_info: Any) -> SpendBundle:
    pubkeys = [secret_key_for_index(i).get_g1() for i in range(4)]
    bundle, data_to_sign = get_rekey_spend_info(
        setup_info.singleton,
        pubkeys,
        setup_info.derivation,
        setup_info.first_lineage_proof,
    )
    agg_pk = G1Element()
    for pubkey in pubkeys:
        agg_pk += pubkey
    signature = AugSchemeMPL.aggregate([sign_message_at_index(i, data_to_sign, agg_pk) for i in range(4)])
    return SpendBundle.aggregate([bundle, SpendBundle([], signature)])


@pytest.mark.asyncio
async def test_lock_increase_needs_two_step(setup_info):
    aggregate_bundle = _lock_increase_bundle(setup_info)
    signature = aggregate_bundle.aggregated_signature

    assert needs_two_step_push(aggregate_bundle)

    two_step = split_two_step_push(aggregate_bundle)
    assert len(two_step.first.coin_spends) == 1
    assert two_step.first.aggregated_signature == signature
    assert len(two_step.second.coin_spends) == 2
    assert two_step.second.aggregated_signature == G2Element()

    original_ids = [coin_spend.coin.name() for coin_spend in aggregate_bundle.coin_spends]
    first_ids = [coin_spend.coin.name() for coin_spend in two_step.first.coin_spends]
    second_ids = [coin_spend.coin.name() for coin_spend in two_step.second.coin_spends]
    assert first_ids + second_ids == original_ids
    assert set(first_ids) | set(second_ids) == set(original_ids)


@pytest.mark.asyncio
async def test_non_ephemeral_bundles_do_not_need_two_step(setup_info):
    three_pubkeys = [secret_key_for_index(i).get_g1() for i in range(3)]
    new_pubkeys = [secret_key_for_index(i).get_g1() for i in range(5, 10)]

    re_derivation = calculate_puzzle_root(
        setup_info.prefarm_info,
        new_pubkeys,
        uint64(3),
        uint64(5),
        uint64(1),
    )
    start_bundle, _ = get_rekey_spend_info(
        setup_info.singleton,
        three_pubkeys,
        setup_info.derivation,
        setup_info.first_lineage_proof,
        re_derivation,
    )
    assert not needs_two_step_push(start_bundle)
    with pytest.raises(ValueError):
        split_two_step_push(start_bundle)

    withdrawal_bundle, _ = get_withdrawal_spend_info(
        setup_info.singleton,
        three_pubkeys,
        setup_info.derivation,
        setup_info.first_lineage_proof,
        uint64(10),
        ACS_PH,
    )
    assert not needs_two_step_push(withdrawal_bundle)
    with pytest.raises(ValueError):
        split_two_step_push(withdrawal_bundle)

    ach_coin = Coin(
        bytes32([1] * 32),
        curry_ach_puzzle(setup_info.prefarm_info, ACS_PH).get_tree_hash(),
        uint64(100),
    )
    completion_bundle = get_ach_clawforward_spend_bundle(ach_coin, setup_info.derivation, ACS_PH)
    assert not needs_two_step_push(completion_bundle)
    with pytest.raises(ValueError):
        split_two_step_push(completion_bundle)


def test_ephemeral_spend_with_aggregated_signature_raises():
    coin_a = Coin(bytes32([2] * 32), ACS_PH, uint64(1000))
    coin_b = Coin(coin_a.name(), ACS_PH, uint64(1000))
    spend_a = make_spend(coin_a, ACS, Program.to([[51, ACS_PH, 1000]]))
    spend_b = make_spend(
        coin_b,
        ACS,
        Program.to([[80, 0], [49, bytes(secret_key_for_index(0).get_g1()), b"message"]]),
    )
    bundle = SpendBundle([spend_a, spend_b], G2Element())

    assert needs_two_step_push(bundle)
    with pytest.raises(ValueError, match="aggregated signature"):
        split_two_step_push(bundle)


@pytest.mark.asyncio
async def test_push_in_two_steps_success():
    first = _simple_bundle(1)
    second = _simple_bundle(2)
    records = {coin_spend.coin.name(): object() for coin_spend in second.coin_spends}
    node_client = _FakeNodeClient([{"success": True}, {"success": True}], records)

    first_result, second_result = await push_in_two_steps(node_client, first, second, 0, 0.01)

    assert first_result == {"success": True}
    assert second_result == {"success": True}
    assert node_client.pushed == [first, second]


@pytest.mark.asyncio
async def test_push_in_two_steps_step_one_rejected():
    first = _simple_bundle(3)
    second = _simple_bundle(4)
    node_client = _FakeNodeClient([{"success": False, "error": "rejected"}], {})

    with pytest.raises(TwoStepPushError) as error:
        await push_in_two_steps(node_client, first, second, 0, 0.01)

    assert len(node_client.pushed) == 1
    assert "Step 2 was not attempted" in str(error.value)
    assert error.value.second_bundle == second


@pytest.mark.asyncio
async def test_push_in_two_steps_confirmation_timeout():
    first = _simple_bundle(5)
    second = _simple_bundle(6)
    node_client = _FakeNodeClient([{"success": True}], {})

    with pytest.raises(TwoStepPushError) as error:
        await push_in_two_steps(node_client, first, second, 0, 0.01)

    assert "not seen confirmed" in str(error.value)
    assert error.value.second_bundle == second
    assert SpendBundle.from_bytes(bytes.fromhex(error.value.second_bundle_hex)) == second
    assert len(node_client.pushed) == 1


@pytest.mark.asyncio
async def test_push_in_two_steps_step_two_rejected():
    first = _simple_bundle(7)
    second = _simple_bundle(8)
    records = {coin_spend.coin.name(): object() for coin_spend in second.coin_spends}
    node_client = _FakeNodeClient([{"success": True}, {"success": False, "error": "nope"}], records)

    with pytest.raises(TwoStepPushError) as error:
        await push_in_two_steps(node_client, first, second, 0, 0.01)

    assert "step 2 was rejected" in str(error.value)
    assert error.value.second_bundle == second
    assert SpendBundle.from_bytes(bytes.fromhex(error.value.second_bundle_hex)) == second
    assert len(node_client.pushed) == 2
