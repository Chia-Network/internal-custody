import pytest

from chia_rs import G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.wallet.conditions import AssertCoinAnnouncement, AssertConcurrentSpend

from cic.cli.fees import bundle_fee, fee_condition_for_step

ACS = Program.to(1)
ACS_PH = ACS.get_tree_hash()


def _spend(seed: int):
    coin = Coin(bytes32([seed] * 32), ACS_PH, uint64(10))
    return make_spend(coin, ACS, Program.to([]))


def _announcement(coin_spend) -> AssertCoinAnnouncement:
    return AssertCoinAnnouncement(asserted_id=coin_spend.coin.name(), asserted_msg=b"msg")


def test_fee_condition_for_step_uses_announcement_in_announcement_step():
    announcement_spend = _spend(1)
    other_spend = _spend(2)
    step_bundle = SpendBundle([other_spend, announcement_spend], G2Element())
    announcement = _announcement(announcement_spend)

    assert fee_condition_for_step(step_bundle, announcement_spend, announcement) == announcement


def test_fee_condition_for_step_uses_first_spend_in_other_step():
    announcement_spend = _spend(3)
    first_spend = _spend(4)
    second_spend = _spend(5)
    step_bundle = SpendBundle([first_spend, second_spend], G2Element())
    announcement = _announcement(announcement_spend)

    assert fee_condition_for_step(step_bundle, announcement_spend, announcement) == AssertConcurrentSpend(
        first_spend.coin.name()
    )


def test_fee_condition_for_step_rejects_empty_step():
    announcement_spend = _spend(6)
    announcement = _announcement(announcement_spend)

    with pytest.raises(ValueError, match="empty step"):
        fee_condition_for_step(SpendBundle([], G2Element()), announcement_spend, announcement)


def _created_coin_spend(coin: Coin, amount: int, seed: int):
    return make_spend(coin, ACS, Program.to([[51, bytes32([seed] * 32), amount]]))


def test_bundle_fee_is_zero_for_a_fee_less_bundle():
    coin = Coin(bytes32([7] * 32), ACS_PH, uint64(1000))

    assert bundle_fee(SpendBundle([_created_coin_spend(coin, 1000, 1)], G2Element())) == 0


def test_bundle_fee_is_the_difference_for_a_fee_bearing_bundle():
    coin = Coin(bytes32([8] * 32), ACS_PH, uint64(1000))

    assert bundle_fee(SpendBundle([_created_coin_spend(coin, 900, 2)], G2Element())) == 100


def test_bundle_fee_ignores_an_ephemeral_coin_created_and_spent_in_the_bundle():
    coin = Coin(bytes32([9] * 32), ACS_PH, uint64(1000))
    ephemeral_coin = Coin(coin.name(), bytes32([10] * 32), uint64(1000))
    bundle = SpendBundle(
        [
            _created_coin_spend(coin, 1000, 10),
            _created_coin_spend(ephemeral_coin, 900, 11),
        ],
        G2Element(),
    )

    assert bundle_fee(bundle) == 100
