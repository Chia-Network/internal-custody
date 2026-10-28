"""Tests for explicit and configured RPC ports in the CLI clients."""

import asyncio

from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from chia_rs.sized_ints import uint16

from cic.cli import clients
from cic.cli.sync_store import SyncStore

CONFIG: Dict[str, Any] = {
    "self_hostname": "localhost-from-config",
    "full_node": {"rpc_port": 1111},
    "wallet": {"rpc_port": 2222},
}


def _patch_load_config(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[Any, str]]:
    """Replace load_config and record its arguments."""
    calls: List[Tuple[Any, str]] = []

    def fake_load_config(root_path: Any, filename: str) -> Dict[str, Any]:
        calls.append((root_path, filename))
        return CONFIG

    monkeypatch.setattr(clients, "load_config", fake_load_config)
    return calls


def _patch_create(monkeypatch: pytest.MonkeyPatch, client_name: str) -> Tuple[List[Dict[str, Any]], object]:
    """Replace a client's create method with an async recorder."""
    calls: List[Dict[str, Any]] = []
    sentinel = object()

    async def fake_create(self_hostname: str, port: uint16, root_path: Any, config: Dict[str, Any]) -> object:
        calls.append({"self_hostname": self_hostname, "port": port, "root_path": root_path, "config": config})
        return sentinel

    monkeypatch.setattr(getattr(clients, client_name), "create", fake_create)
    return calls, sentinel


def test_node_client_explicit_port_is_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit node port reaches the client unchanged."""
    config_calls = _patch_load_config(monkeypatch)
    create_calls, sentinel = _patch_create(monkeypatch, "FullNodeRpcClient")

    result = asyncio.run(clients.get_node_client(8555))

    assert result is sentinel
    assert config_calls == [(clients.DEFAULT_ROOT_PATH, "config.yaml")]
    assert len(create_calls) == 1
    call = create_calls[0]
    assert call["self_hostname"] == "localhost-from-config"
    assert call["port"] == uint16(8555)
    assert call["port"] == 8555
    assert call["root_path"] == clients.DEFAULT_ROOT_PATH
    assert call["config"] == CONFIG


def test_node_client_none_port_uses_config_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing node port falls back to the configured port."""
    config_calls = _patch_load_config(monkeypatch)
    create_calls, sentinel = _patch_create(monkeypatch, "FullNodeRpcClient")

    result = asyncio.run(clients.get_node_client(None))

    assert result is sentinel
    assert config_calls == [(clients.DEFAULT_ROOT_PATH, "config.yaml")]
    assert len(create_calls) == 1
    call = create_calls[0]
    assert call["self_hostname"] == "localhost-from-config"
    assert call["port"] == uint16(1111)
    assert call["port"] == 1111
    assert call["root_path"] == clients.DEFAULT_ROOT_PATH
    assert call["config"] == CONFIG


def test_wallet_client_explicit_port_is_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit wallet port reaches the client unchanged."""
    config_calls = _patch_load_config(monkeypatch)
    create_calls, sentinel = _patch_create(monkeypatch, "WalletRpcClient")

    result = asyncio.run(clients.get_wallet_client(9256))

    assert result is sentinel
    assert config_calls == [(clients.DEFAULT_ROOT_PATH, "config.yaml")]
    assert len(create_calls) == 1
    call = create_calls[0]
    assert call["self_hostname"] == "localhost-from-config"
    assert call["port"] == uint16(9256)
    assert call["port"] == 9256
    assert call["root_path"] == clients.DEFAULT_ROOT_PATH
    assert call["config"] == CONFIG


def test_wallet_client_none_port_uses_config_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing wallet port falls back to the configured port."""
    config_calls = _patch_load_config(monkeypatch)
    create_calls, sentinel = _patch_create(monkeypatch, "WalletRpcClient")

    result = asyncio.run(clients.get_wallet_client(None))

    assert result is sentinel
    assert config_calls == [(clients.DEFAULT_ROOT_PATH, "config.yaml")]
    assert len(create_calls) == 1
    call = create_calls[0]
    assert call["self_hostname"] == "localhost-from-config"
    assert call["port"] == uint16(2222)
    assert call["port"] == 2222
    assert call["root_path"] == clients.DEFAULT_ROOT_PATH
    assert call["config"] == CONFIG


def test_add_configuration_rejects_unknown_type(tmp_path: Path) -> None:
    """add_configuration raises TypeError for an unsupported configuration type."""

    async def reject() -> None:
        store = await SyncStore.create(tmp_path / "sync.sqlite")
        try:
            with pytest.raises(TypeError, match="PrefarmInfo or a RootDerivation"):
                await store.add_configuration("not a configuration")  # type: ignore[arg-type]
            assert await store.get_configuration(True) is None
        finally:
            await store.db_wrapper.close()

    asyncio.run(reject())
