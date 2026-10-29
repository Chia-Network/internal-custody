"""Read-only command for checking a list of payment spend files."""

import asyncio

from datetime import datetime, timezone
from typing import List, Optional, Set

import click

from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from chia.types.blockchain_format.coin import Coin

from cic.cli.common import open_sync_store
from cic.cli.payment_series import describe_previous_payment, load_previous_spend
from cic.cli.record_types import SingletonRecord
from cic.drivers.puzzle_root_construction import RootDerivation


@click.command("check_series", short_help="Check a list of payment spends against the synced database")
@click.option(
    "-db",
    "--db-path",
    help="The file path to the sync DB (default: ./sync (******).sqlite)",
    default="./",
    required=True,
)
@click.option(
    "-s",
    "--spends",
    help=(
        "A comma separated list of previously generated spend files (unsigned or signed), in the order they will "
        "be pushed."
    ),
    required=True,
)
def check_series_cmd(db_path: str, spends: str) -> None:
    """Report whether a list of payment files forms a valid series."""
    filenames: List[str] = [name.strip() for name in spends.split(",")]
    for position, name in enumerate(filenames, start=1):
        if name == "":
            raise ValueError(f"Previous spend list entry {position} is empty; remove it and try again")

    async def do_command() -> int:
        async with open_sync_store(db_path) as sync_store:
            derivation = await sync_store.get_configuration(False, block_outdated=True)
            if not isinstance(derivation, RootDerivation):
                raise ValueError("No usable configuration is found for this database.  Try `cic sync` then try again.")
            current_singleton: Optional[SingletonRecord] = await sync_store.get_latest_singleton()
            if current_singleton is None:
                raise RuntimeError("No singleton is found for this configuration.  Try `cic sync` then try again.")
            p2_singletons = await sync_store.get_p2_singletons(uint64(0))
            p2_singleton_ids: Set[bytes32] = {coin.name() for coin in p2_singletons}

            earliest_push_time = int(current_singleton.confirmed_at_time) + int(
                derivation.prefarm_info.withdrawal_timelock
            )
            expected: Optional[Coin] = current_singleton.coin
            seen_absorbed: Set[bytes32] = set()
            first_problem: Optional[str] = None

            for index, filename in enumerate(filenames):
                print(filename)
                if index == 0:
                    timestamp = datetime.fromtimestamp(earliest_push_time, timezone.utc)
                    print(f"Earliest push time: {timestamp.isoformat()} ({earliest_push_time})")

                try:
                    coin_spends = load_previous_spend(filename)
                except (OSError, ValueError) as error:
                    print(f"  PROBLEM: cannot be read: {error}")
                    if first_problem is None:
                        first_problem = filename
                    expected = None
                    continue

                checked_expected = expected
                info = describe_previous_payment(filename, coin_spends, checked_expected or current_singleton.coin)

                if info.singleton_coin is not None:
                    print(f"  Singleton coin: {info.singleton_coin.name().hex()}")
                else:
                    print("  Singleton coin: not a singleton spend")

                if checked_expected is None:
                    print("  not checked because an earlier file is not a valid payment")
                else:
                    print(f"  Spends expected singleton coin: {'yes' if info.links_to_expected else 'no'}")

                spend_type_name = info.spend_type.name if info.spend_type is not None else "not a singleton spend"
                print(f"  Spend type: {spend_type_name}")
                print(f"  Out amount: {info.out_amount if info.out_amount is not None else 'n/a'}")
                print(f"  In amount: {info.in_amount if info.in_amount is not None else 'n/a'}")
                absorbed_text = ", ".join(absorbed.hex() for absorbed in info.absorbed) if info.absorbed else "none"
                print(f"  Absorbed coins: {absorbed_text}")

                if checked_expected is not None:
                    if info.singleton_coin is None:
                        print("  PROBLEM: is not a payment spend")
                        if first_problem is None:
                            first_problem = filename
                    elif not info.links_to_expected:
                        print(f"  PROBLEM: does not spend the expected singleton coin {checked_expected.name().hex()}")
                        if first_problem is None:
                            first_problem = filename
                    elif not info.is_payment:
                        print("  PROBLEM: is not a payment spend")
                        if first_problem is None:
                            first_problem = filename
                    elif info.next_coin is None:
                        assert info.out_amount is not None
                        assert info.in_amount is not None
                        resulting_amount = int(checked_expected.amount) - info.out_amount + info.in_amount
                        print(f"  PROBLEM: the resulting singleton amount {resulting_amount} must be positive and odd")
                        if first_problem is None:
                            first_problem = filename

                for absorbed in info.absorbed:
                    if info.is_payment:
                        if absorbed in seen_absorbed:
                            print(
                                f"  PROBLEM: absorbs coin {absorbed.hex()} that an earlier file in this list "
                                "also absorbs"
                            )
                            if first_problem is None:
                                first_problem = filename
                        if absorbed not in p2_singleton_ids:
                            print(
                                f"  PROBLEM: absorbs coin {absorbed.hex()} "
                                "that is not an unspent p2_singleton in the synced database"
                            )
                            if first_problem is None:
                                first_problem = filename
                    seen_absorbed.add(absorbed)

                if checked_expected is not None and info.next_coin is not None:
                    expected = info.next_coin
                else:
                    expected = None

            if first_problem is not None:
                print(
                    f"File {first_problem} and every file after it must be regenerated with "
                    "cic payment --previous-spends and signed again."
                )
                return 1
            return 0

    if asyncio.run(do_command()) != 0:
        raise SystemExit(1)
