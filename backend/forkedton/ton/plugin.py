import os
from functools import wraps

import bitcart
from api import constants
from api.ext.blockexplorer import EXPLORERS
from api.ext.exchanges import coingecko, coinrules
from api.ext.moneyformat import currency_table
from api.plugins import BasePlugin
from bitcart import ETH
from universalasync import wrap

# Same variable the daemon gets (components/ton.yml); here it only labels a non-mainnet coin in the admin panel.
TON_NETWORK = os.environ.get("TON_NETWORK", "mainnet")
# CoinGecko lists tether's TON contract in the friendly form; the daemon normalises it to raw.
USDT_RAW = "0:B113A994B5024A16719F69139328EB759596C38A25F59028B146FECDC3621DFE"
USDT_FRIENDLY = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"


class TON(ETH):
    coin_name = "TON"
    friendly_name = "Toncoin"
    xpub_name = "Public key"
    RPC_URL = "http://localhost:5013"


class TONRules:
    coingecko_id = "the-open-network"


# Registration happens at import: CoinService reads bitcart.COINS[...] for every entry of
# BITCART_CRYPTOS as soon as the DI container is built, before any plugin hook runs.
wrap(TON)
bitcart.COINS["TON"] = TON
constants.SUPPORTED_CRYPTOS["ton"] = "Gram (TON)" if TON_NETWORK == "mainnet" else f"Gram (TON, {TON_NETWORK})"
EXPLORERS["ton"] = {
    "mainnet": "https://tonviewer.com/transaction/{}",
    "testnet": "https://testnet.tonviewer.com/transaction/{}",
}
coinrules.TON = TONRules
# Without an entry the USD fallback (2 decimals) would truncate invoice amounts to 0.01 TON.
currency_table.data["TON"] = {"name": "Toncoin", "divisibility": 8, "symbol": None, "crypto": True}

# CoinGecko keys quotes by its own symbol, which for our id changed from "ton" to "gram"; Bitcart asks
# for TON_<fiat>. Keying our id by the coin code keeps both rate paths working whatever the symbol becomes.
# The exchanges run in the worker, which imports this module too. Unwrap first so a reload does not stack.
_find_id = getattr(coingecko.find_id, "__wrapped__", coingecko.find_id)
_find_by_contract = getattr(coingecko.find_by_contract, "__wrapped__", coingecko.find_by_contract)


@wraps(_find_id)
def find_id(all_coins, x):
    return "ton" if x == TONRules.coingecko_id else _find_id(all_coins, x)


@wraps(_find_by_contract)
def find_by_contract(all_coins, contract):
    return _find_by_contract(all_coins, USDT_FRIENDLY if contract.upper() == USDT_RAW else contract)


coingecko.find_id = find_id
coingecko.find_by_contract = find_by_contract


def set_user_address(method):
    # Each invoice has its own deposit address, so there is no sender step; a set user_address
    # tells the operator's checkouts to skip the "enter your sending address" prompt.
    if isinstance(method, dict) and method.get("currency") == "ton":
        method["user_address"] = method["payment_address"]
    return method


class Plugin(BasePlugin):
    name = "forked_ton"

    def setup_app(self, app):
        pass

    async def startup(self):
        # startup() runs in both the API process and the worker, unlike setup_app/worker_setup
        self.context.register_filter("post_create_payment_method", set_user_address)

    async def shutdown(self):
        pass

    async def worker_setup(self):
        pass
