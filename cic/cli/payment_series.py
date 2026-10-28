"""Pure helpers for building and checking series of payment spends."""

import dataclasses

from typing import List, Optional, Set, Tuple

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import CoinSpend, make_spend
from chia_rs import SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64
from chia.wallet.lineage_proof import LineageProof

from cic.drivers.prefarm import (
    SpendType,
    construct_singleton_inner_puzzle,
    get_spend_params_for_ach_creation,
    get_spend_type_for_solution,
)
from cic.drivers.puzzle_root_construction import RootDerivation
from cic.drivers.singleton import SINGLETON_MOD

from hsms.core.unsigned_spend import UnsignedSpend
from hsms.util.qrint_encoding import a2b_qrint

from chia_base.core import CoinSpend as HSMCoinSpend


@dataclasses.dataclass(frozen=True)
class PaymentFileInfo:
    """Read-only description of one file in a payment series."""

    filename: str
    singleton_coin: Optional[Coin]
    links_to_expected: bool
    spend_type: Optional[SpendType]
    is_payment: bool
    out_amount: Optional[int]
    in_amount: Optional[int]
    absorbed: List[bytes32]
    next_coin: Optional[Coin]
    next_lineage: Optional[LineageProof]


def _hsm_coin_spend_to_chia(coin_spend: HSMCoinSpend) -> CoinSpend:
    coin = Coin(
        bytes32(bytes(coin_spend.coin.parent_coin_info)),
        bytes32(bytes(coin_spend.coin.puzzle_hash)),
        uint64(int(coin_spend.coin.amount)),
    )
    return make_spend(
        coin,
        Program.from_bytes(bytes(coin_spend.puzzle_reveal)),
        Program.from_bytes(bytes(coin_spend.solution)),
    )


def load_previous_spend(path: str) -> List[CoinSpend]:
    """Load an unsigned spend file or a signed spend bundle file."""
    with open(path, "r") as file:
        text = file.read().strip()

    try:
        unsigned_spend = UnsignedSpend.from_bytes(a2b_qrint(text))
    except (ValueError, AssertionError, KeyError, IndexError, EOFError, TypeError):
        try:
            signed_bundle = SpendBundle.from_bytes(bytes.fromhex(text))
        except (ValueError, AssertionError, KeyError, EOFError, TypeError) as error:
            raise ValueError(
                "the file is neither an unsigned spend file nor a signed spend bundle file; "
                "check the file name and format"
            ) from error
        return list(signed_bundle.coin_spends)

    return [_hsm_coin_spend_to_chia(coin_spend) for coin_spend in unsigned_spend.coin_spends]


def _find_singleton_spends(coin_spends: List[CoinSpend]) -> List[CoinSpend]:
    singleton_spends: List[CoinSpend] = []
    for coin_spend in coin_spends:
        try:
            puzzle = Program.from_bytes(bytes(coin_spend.puzzle_reveal))
            module, _ = puzzle.uncurry()
        except (ValueError, AssertionError, TypeError):
            continue
        if module == SINGLETON_MOD:
            singleton_spends.append(coin_spend)
    return singleton_spends


def _spend_type_for_coin_spend(coin_spend: CoinSpend) -> SpendType:
    solution = Program.from_bytes(bytes(coin_spend.solution))
    return get_spend_type_for_solution(solution)


def _payment_amounts_for_coin_spend(coin_spend: CoinSpend) -> Tuple[int, int]:
    solution = Program.from_bytes(bytes(coin_spend.solution))
    out_amount, in_amount, _ = get_spend_params_for_ach_creation(solution)
    return int(out_amount), int(in_amount)


def _singleton_inner_puzzle_hash(coin_spend: CoinSpend) -> bytes32:
    puzzle = Program.from_bytes(bytes(coin_spend.puzzle_reveal))
    _, args = puzzle.uncurry()
    inner_puzzle = list(args.as_iter())[-1]
    return inner_puzzle.get_tree_hash()


def describe_previous_payment(filename: str, coin_spends: List[CoinSpend], expected: Coin) -> PaymentFileInfo:
    """Describe one payment file without raising for content that cannot be chained."""
    singleton_spends = _find_singleton_spends(coin_spends)
    if len(singleton_spends) != 1:
        unmatched_absorbed = [coin_spend.coin.name() for coin_spend in coin_spends]
        return PaymentFileInfo(filename, None, False, None, False, None, None, unmatched_absorbed, None, None)

    singleton_spend = singleton_spends[0]
    spent = singleton_spend.coin
    absorbed: List[bytes32] = [
        coin_spend.coin.name() for coin_spend in coin_spends if coin_spend.coin != singleton_spend.coin
    ]
    links_to_expected = spent == expected

    try:
        spend_type: Optional[SpendType] = _spend_type_for_coin_spend(singleton_spend)
    except (ValueError, AssertionError, KeyError, TypeError):
        spend_type = None

    if spend_type != SpendType.HANDLE_PAYMENT:
        return PaymentFileInfo(filename, spent, links_to_expected, spend_type, False, None, None, absorbed, None, None)

    try:
        out_amount, in_amount = _payment_amounts_for_coin_spend(singleton_spend)
        inner_puzzle_hash = _singleton_inner_puzzle_hash(singleton_spend)
    except (ValueError, AssertionError, KeyError, IndexError, TypeError):
        return PaymentFileInfo(filename, spent, links_to_expected, spend_type, False, None, None, absorbed, None, None)

    new_amount = int(spent.amount) - out_amount + in_amount
    if not links_to_expected or new_amount <= 0 or new_amount % 2 == 0:
        return PaymentFileInfo(
            filename, spent, links_to_expected, spend_type, True, out_amount, in_amount, absorbed, None, None
        )

    try:
        next_coin = Coin(spent.name(), spent.puzzle_hash, uint64(new_amount))
    except (ValueError, OverflowError):
        return PaymentFileInfo(
            filename, spent, links_to_expected, spend_type, True, out_amount, in_amount, absorbed, None, None
        )

    return PaymentFileInfo(
        filename,
        spent,
        links_to_expected,
        spend_type,
        True,
        out_amount,
        in_amount,
        absorbed,
        next_coin,
        LineageProof(spent.parent_coin_info, inner_puzzle_hash, spent.amount),
    )


def advance_through_previous_payments(
    current: Coin,
    lineage: LineageProof,
    derivation: RootDerivation,
    previous: List[Tuple[str, List[CoinSpend]]],
) -> Tuple[Coin, LineageProof, Set[bytes32]]:
    """Return the singleton coin and lineage proof that will exist after every previous payment is confirmed in
    order, plus the IDs of all non-singleton coins those payments spend."""
    coin = current
    lineage_proof = lineage
    absorbed: Set[bytes32] = set()

    for filename, coin_spends in previous:
        singleton_spends = _find_singleton_spends(coin_spends)
        if len(singleton_spends) != 1:
            raise ValueError(
                f"{filename}: expected exactly one singleton spend; "
                "only payment spends generated by cic payment can be chained"
            )

        singleton_spend = singleton_spends[0]
        spent = singleton_spend.coin
        if spent != coin:
            raise ValueError(
                f"{filename} does not spend the expected singleton coin {coin.name().hex()}; "
                "list only spends that are not yet confirmed, in push order, and run cic sync first"
            )

        try:
            spend_type = _spend_type_for_coin_spend(singleton_spend)
        except (ValueError, AssertionError, KeyError, TypeError) as error:
            raise ValueError(f"{filename}: only payment spends can be chained") from error
        if spend_type != SpendType.HANDLE_PAYMENT:
            raise ValueError(f"{filename}: only payment spends can be chained")

        try:
            out_amount, in_amount = _payment_amounts_for_coin_spend(singleton_spend)
        except (ValueError, AssertionError, KeyError, TypeError) as error:
            raise ValueError(
                f"{filename}: cannot read the payment amounts; only payment spends can be chained"
            ) from error

        new_amount = int(coin.amount) - out_amount + in_amount
        if new_amount <= 0 or new_amount % 2 == 0:
            raise ValueError(
                f"{filename}: the resulting singleton amount {new_amount} must be positive and odd; "
                "check the payment amount and the absorbed coins, then regenerate this file"
            )

        absorbed.update(coin_spend.coin.name() for coin_spend in coin_spends if coin_spend.coin != singleton_spend.coin)
        lineage_proof = LineageProof(
            spent.parent_coin_info,
            construct_singleton_inner_puzzle(derivation.prefarm_info).get_tree_hash(),
            spent.amount,
        )
        coin = Coin(spent.name(), spent.puzzle_hash, uint64(new_amount))

    return coin, lineage_proof, absorbed
