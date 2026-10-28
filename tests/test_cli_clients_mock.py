import asyncio
import inspect

import pytest
from chia_rs.sized_ints import uint32, uint64

from chia.wallet.util.tx_config import DEFAULT_TX_CONFIG
from chia.wallet.wallet_request_types import SelectCoins
from chia.wallet.wallet_rpc_client import WalletRpcClient

from tests.cli_clients import WalletClientMock


def test_wallet_client_mock_select_coins_signature_matches_real_client():
    mock_parameters = list(inspect.signature(WalletClientMock.select_coins).parameters)
    real_parameters = list(inspect.signature(WalletRpcClient.select_coins).parameters)
    assert mock_parameters == ["self", "request"]
    assert mock_parameters == real_parameters


def test_wallet_client_mock_select_coins_rejects_tx_config():
    request = SelectCoins(wallet_id=uint32(1), amount=uint64(0))
    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(TypeError):
            # The extra argument is intentional: it proves the mock rejects a tx_config like the real client.
            loop.run_until_complete(
                WalletClientMock(None).select_coins(  # pylint: disable=too-many-function-args
                    request, DEFAULT_TX_CONFIG
                )
            )
    finally:
        loop.close()
