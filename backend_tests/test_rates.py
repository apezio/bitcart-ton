"""TON and USDT-on-TON rates through the real WalletDataService.get_rate, ExchangeRateService and
CoingeckoExchange; only CoinGecko HTTP (fetch_delayed), the coin object and the store are faked."""

import asyncio
from collections import defaultdict
from decimal import Decimal
from types import SimpleNamespace

import pytest
from api.ext.exchanges import coingecko
from api.plugins import load_module
from api.services.exchange_rate import ExchangeRateService
from api.services.plugin_registry import PluginRegistry
from api.services.wallet_data import WalletDataService

TON_PRICE = 1.53
USDT_PRICE = 1.02  # not 1, so a fallback rate of 1 cannot pass for it
ETH_USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"


def coins_list(ton_symbol):
    # shaped like /coins/list?include_platform=true, as of 2026-10-05
    return [
        {"id": "the-open-network", "symbol": ton_symbol, "name": "Gram (prev. Toncoin)", "platforms": {}},
        {
            "id": "tether",
            "symbol": "usdt",
            "name": "Tether",
            "platforms": {"ethereum": ETH_USDT, "the-open-network": "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"},
        },
        {"id": "toncoin-fake", "symbol": "ton", "name": "Toncoin", "platforms": {}},
    ]


@pytest.fixture
def plugin():
    return load_module("modules/forkedton/ton/plugin.py")


@pytest.fixture
def make_service(plugin, monkeypatch):
    def make(ton_symbol="gram"):
        prices = {"the-open-network": TON_PRICE, "tether": USDT_PRICE, "toncoin-fake": 99}

        async def fetch_delayed(method, url, **kwargs):
            if url.endswith("/simple/supported_vs_currencies"):
                return ["usd"]
            if "/coins/list" in url:
                return coins_list(ton_symbol)
            ids = url.split("ids=", 1)[1].split("&", 1)[0].split(",")
            return {i: {"usd": prices[i]} for i in ids if i}

        monkeypatch.setattr(coingecko, "fetch_delayed", fetch_delayed)
        settings = SimpleNamespace(
            IS_WORKER=True, is_testing=lambda: False, coingecko_api_url="http://cg", coingecko_headers={}
        )
        ton = SimpleNamespace(coin_name="TON", friendly_name=plugin.TON.friendly_name)
        service = ExchangeRateService(None, settings, SimpleNamespace(cryptos={"ton": ton}), None, None)
        service.lock = asyncio.Lock()
        service.contracts = {"ton": []}  # TON is the only coin; no other contract pulls tether in
        service.exchanges["coingecko"] = coingecko.CoingeckoExchange(settings, service, [ton], service.contracts)
        registry = PluginRegistry.__new__(PluginRegistry)
        registry._callbacks = defaultdict(list)
        return WalletDataService(None, service, registry)

    return make


def coin(contract=None):
    async def readcontract(address, name):
        assert (address, name) == (contract, "symbol")
        return "USDT"

    return SimpleNamespace(xpub={"xpub": "key", "contract": contract}, server=SimpleNamespace(readcontract=readcontract))


WALLET = SimpleNamespace(currency="ton")
STORE = SimpleNamespace(checkout_settings=SimpleNamespace(rate_rules=""))


@pytest.mark.parametrize("store", [STORE, None], ids=["store", "no-store"])
@pytest.mark.parametrize("ton_symbol", ["gram", "ton"])
async def test_ton_rate(make_service, store, ton_symbol):
    service = make_service(ton_symbol)
    assert await service.get_rate(WALLET, "USD", coin(), store=store) == Decimal("1.53")


@pytest.mark.parametrize("store", [STORE, None], ids=["store", "no-store"])
async def test_usdt_rate_from_ton_contract(plugin, make_service, store):
    service = make_service()
    assert await service.get_rate(WALLET, "USD", coin(plugin.USDT_RAW), store=store) == Decimal("1.02")


async def test_bug_without_patch(plugin, make_service, monkeypatch):
    # proves the tests above detect the bug: stock lookups give the silent fallback rate 1
    monkeypatch.setattr(coingecko, "find_id", plugin._find_id)
    monkeypatch.setattr(coingecko, "find_by_contract", plugin._find_by_contract)
    assert await make_service().get_rate(WALLET, "USD", coin(), store=STORE) == Decimal(1)
    assert await make_service().get_rate(WALLET, "USD", coin(plugin.USDT_RAW)) == Decimal(1)


def test_other_lookups_unchanged(plugin):
    cache = coins_list("gram")
    assert coingecko.find_id(cache, "tether") == "usdt"
    assert coingecko.find_by_contract(cache, ETH_USDT.lower())["id"] == "tether"
    assert coingecko.find_by_contract(cache, "0:" + "0" * 64) is None
