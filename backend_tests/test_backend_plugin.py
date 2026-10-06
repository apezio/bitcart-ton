"""Runs inside bitcart/bitcart:<version> with the plugin mounted exactly as rules/90_ton_modules.py
mounts it (./backend/forkedton -> /app/modules/forkedton:ro), cwd /app. Optionally a second plugin
mounted at /app/modules/forked (the Solana one) for the coexistence test."""

import glob
import importlib
import os
from collections import defaultdict
from decimal import Decimal
from types import SimpleNamespace

import pytest
from api.plugins import PluginContext, load_module, load_plugins
from api.services.plugin_registry import PluginRegistry

PLUGIN_PATH = "modules/forkedton/ton/plugin.py"


@pytest.fixture(scope="module")
def plugin():
    # the stock loader: glob + importlib on the dotted path, next to the image's modules/__init__.py
    assert PLUGIN_PATH in glob.glob("modules/**/**/plugin.py")
    return load_module(PLUGIN_PATH)


def test_registration(plugin):
    import bitcart
    from api import constants
    from api.ext.blockexplorer import EXPLORERS
    from api.ext.exchanges import coinrules
    from api.ext.moneyformat import currency_table

    assert plugin.__name__ == "modules.forkedton.ton.plugin"
    assert bitcart.COINS["TON"] is plugin.TON
    assert plugin.TON.RPC_URL.rsplit(":", 1)[1] == "5013"
    assert plugin.TON.xpub_name == "Public key"
    assert constants.SUPPORTED_CRYPTOS["ton"] == "Gram (TON)"
    assert EXPLORERS["ton"]["mainnet"] == "https://tonviewer.com/transaction/{}"
    assert EXPLORERS["ton"]["testnet"] == "https://testnet.tonviewer.com/transaction/{}"
    assert coinrules.TON.coingecko_id == "the-open-network"
    assert currency_table.get_currency_data("ton")["divisibility"] == 8
    assert currency_table.format_decimal("ton", Decimal("1.23456789")) == "1.23456789"
    assert plugin.Plugin.name == "forked_ton"


def test_network_label(plugin, monkeypatch):
    from api import constants

    monkeypatch.setenv("TON_NETWORK", "testnet")
    try:
        importlib.reload(plugin)
        assert constants.SUPPORTED_CRYPTOS["ton"] == "Gram (TON, testnet)"
        assert plugin.TON.friendly_name == "Toncoin"  # CoinGecko name fallback must not change
    finally:
        monkeypatch.delenv("TON_NETWORK")
        importlib.reload(plugin)
    assert constants.SUPPORTED_CRYPTOS["ton"] == "Gram (TON)"


async def test_filter(plugin):
    # the real dispatcher and the real argument list of invoices.py's post_create_payment_method call
    registry = PluginRegistry.__new__(PluginRegistry)
    registry._callbacks = defaultdict(list)
    obj = plugin.Plugin(os.path.dirname(PLUGIN_PATH))
    obj._set_context(PluginContext(registry, None))
    await obj.startup()
    extra = ("invoice", "wallet", None, "store", [], None)

    ton = {"currency": "ton", "payment_address": "UQabc", "contract": None, "metadata": {}}
    assert await registry.apply_filters("post_create_payment_method", dict(ton), *extra) == {**ton, "user_address": "UQabc"}
    usdt = {**ton, "contract": "EQCxE6"}
    assert (await registry.apply_filters("post_create_payment_method", dict(usdt), *extra))["user_address"] == "UQabc"
    eth = {"currency": "eth", "payment_address": "0xabc", "contract": None, "metadata": {}}
    assert await registry.apply_filters("post_create_payment_method", dict(eth), *extra) == eth
    assert await registry.apply_filters("post_create_payment_method", None, *extra) is None


def test_coexists_with_forked(plugin):
    if not os.path.exists("modules/forked"):
        pytest.skip("no second module mounted at /app/modules/forked")
    found = glob.glob("modules/**/**/plugin.py")
    assert PLUGIN_PATH in found
    assert any(p.startswith("modules/forked/") for p in found)
    classes, _ = load_plugins(SimpleNamespace(is_testing=lambda: False))
    assert "forked_ton" in classes
    others = {name for name, cls in classes.items() if cls.__module__.startswith("modules.forked.")}
    assert others, classes
