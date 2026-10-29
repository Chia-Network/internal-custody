from chia_rs import G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend

from cic.cli.fees import find_fee_announcement

from tests.cli_push_tx_helpers import ACS, ACS_PH


def test_find_fee_announcement_uses_last_match():
    first_coin = Coin(bytes32([1] * 32), ACS_PH, uint64(10))
    second_coin = Coin(bytes32([2] * 32), ACS_PH, uint64(10))
    bundle = SpendBundle(
        [
            make_spend(first_coin, ACS, Program.to([[60, b"first"], [51, ACS_PH, 10]])),
            make_spend(second_coin, ACS, Program.to([[60, b"second"], [51, ACS_PH, 10]])),
        ],
        G2Element(),
    )

    announcement = find_fee_announcement(bundle)

    assert announcement is not None
    assert announcement.asserted_id == second_coin.name()
    assert announcement.asserted_msg == b"second"
