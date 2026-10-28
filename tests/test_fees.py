from chia_rs import G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend

from cic.cli.fees import bundle_fee

ACS = Program.to(1)
ACS_PH = ACS.get_tree_hash()


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
