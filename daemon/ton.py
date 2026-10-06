import asyncio
import base64
import contextlib
import json
import os
import re
import secrets
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from aiohttp import ClientError, ClientResponseError, ClientSession, ClientTimeout
from genericprocessor import (
    PR_UNPAID,
    BlockchainFeatures,
    BlockProcessorDaemon,
    Transaction,
    WalletDB,
    daemon_ctx,
    from_wei,
    str_to_bool,
    to_wei,
)
from genericprocessor import Invoice as BaseInvoice
from genericprocessor import KeyStore as BaseKeyStore
from genericprocessor import Wallet as BaseWallet
from logger import get_logger
from storage import Storage, StoredDBProperty
from tonaddr import (
    FEE_WALLET_ID,
    NATIVE_OFFSET,
    USDT_MASTER,
    USDT_OFFSET,
    address_cell_boc,
    address_from_boc,
    friendly,
    parse_address,
    wallet_address,
)
from utils import AbstractRPCProvider, MultipleProviderRPC, modify_payment_url, rpc

logger = get_logger(__name__)

USDT = {"symbol": "USDT", "decimals": 6}
MAX_INDEX = 1_000_000_000
JETTON_INTERNAL_TRANSFER = "0x178d4519"

NOT_SUPPORTED = "Currently not supported"
INVALID_CONTRACT = "Invalid contract address or non-ERC20 token"  # eth.json maps this exact text

# Only finalized transactions are credited: they cannot be rolled back.
FINALIZED_CONFIRMATIONS = 32
RETRY_DELAYS = (1, 2, 4)
UTIME_MARGIN = 600  # seconds before the oldest open request's creation that a poll starts at (clock skew)

GLOBAL_IDS = {"mainnet": -239, "testnet": -3}
SERVERS = {"mainnet": "https://toncenter.com/api/v3", "testnet": "https://testnet.toncenter.com/api/v3"}


def redact_url(url):
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.hostname}"


def is_pubkey(key):
    return isinstance(key, str) and re.fullmatch(r"[0-9a-fA-F]{64}", key) is not None


def is_testnet():
    return daemon_ctx.get().NET == "testnet"


def tx_hash(tx):
    raw = base64.b64decode(tx["hash"], validate=True)
    if len(raw) != 32:
        raise ValueError(f"Unexpected transaction hash {tx['hash']}")
    return raw.hex()


def flag(value):
    if not isinstance(value, bool):
        raise ValueError(f"Expected a boolean, got {value!r}")
    return value


def native_credit(tx):
    msg = tx["in_msg"]
    if msg is None or msg["source"] is None:  # tick-tock or external message: no value comes in
        return None
    value = int(msg["value"])
    if value <= 0 or flag(msg["bounced"]):
        return None
    # An aborted transaction of a bounceable message sends the value back
    if flag(tx["description"]["aborted"]) and flag(msg["bounce"]):
        return None
    return value, parse_address(msg["source"])


def jetton_credit(tx):
    msg = tx["in_msg"]
    if msg is None or msg.get("opcode") != JETTON_INTERNAL_TRANSFER:
        return None
    description = tx["description"]
    compute = description["compute_ph"]
    if flag(description["aborted"]) or compute.get("skipped") or not flag(compute["success"]) or compute["exit_code"] != 0:
        return None
    # The jetton wallet code accepts internal transfers only from sibling wallets, so a successful one is real USDT
    body = msg["message_content"]["decoded"]
    if body["@type"] != "jetton_internal_transfer":
        raise ValueError(f"Undecoded jetton transfer body in {tx['hash']}")
    amount = int(body["amount"]["value"])
    if amount <= 0:
        return None
    sender = body.get("from") or {}  # information only; a mint by the master may carry no address
    return amount, f"{int(sender['workchain_id'])}:{sender['address'].upper()}" if "address" in sender else None


class TonRPCError(Exception):
    pass


class TonTransientError(TonRPCError):
    pass


class TonRPCProvider(AbstractRPCProvider):
    def __init__(self, url):
        self.url = url
        self.session = None

    async def send_single_request(self, endpoint, params=(), body=None):
        for attempt, delay in enumerate((*RETRY_DELAYS, None)):
            try:
                return await self._request(endpoint, params, body)
            except TonTransientError as e:
                if delay is None:
                    raise
                logger.warning(f"{redact_url(self.url)} {endpoint}: {e}; retry {attempt + 1} in {delay}s")
                await asyncio.sleep(delay)

    async def _request(self, endpoint, params, body):
        if self.session is None:
            self.session = ClientSession(timeout=ClientTimeout(total=30))
        parsed = urlsplit(self.url)
        url = urlunsplit((parsed.scheme, parsed.netloc, f"{parsed.path.rstrip('/')}/{endpoint}", "", ""))
        # the configured query string carries the api_key
        query = parse_qsl(parsed.query) + [(k, str(v)) for k, v in params]
        # aiohttp exceptions carry the full request URL (API key included); never let them escape
        try:
            request = self.session.get(url, params=query) if body is None else self.session.post(url, params=query, json=body)
            async with request as response:
                if response.status == 429 or response.status >= 500:
                    raise TonTransientError(f"HTTP {response.status}")
                data = await response.json(content_type=None)
        except ClientResponseError as e:
            raise TonTransientError(f"Malformed HTTP response for {endpoint}: {e.message}") from None
        except (ClientError, TimeoutError, OSError, ValueError) as e:
            raise TonTransientError(f"{type(e).__name__} for {endpoint}") from None
        if response.status != 200:
            error = data.get("error") if isinstance(data, dict) else None
            raise TonRPCError(f"HTTP {response.status} for {endpoint}: {str(error)[:200]}")
        if not isinstance(data, dict):
            raise TonTransientError(f"Malformed response for {endpoint}")
        return data

    async def send_ping_request(self):
        return await self.send_single_request("masterchainInfo")

    async def close(self):
        if self.session is not None:
            await self.session.close()


class TONFeatures(BlockchainFeatures):
    async def request(self, endpoint, params=(), body=None):
        return await self.rpc.send_request(endpoint, params, body)

    async def get_block_number(self):
        last = (await self.request("masterchainInfo"))["last"]
        # Deposit addresses are the same on both networks: a testnet server on a mainnet store would take faucet coins
        if last["global_id"] != GLOBAL_IDS[daemon_ctx.get().NET]:
            raise TonRPCError("The server is on another network than TON_NETWORK")
        return int(last["seqno"])

    async def is_connected(self):
        try:
            await self.get_block_number()
            return True
        except Exception:
            return False

    async def get_gas_price(self):
        return 0

    async def get_transaction(self, tx):
        reply = await self.request("transactions", [("hash", tx), ("limit", 1)])
        return reply["transactions"][0] if reply["transactions"] else None

    async def get_tx_receipt(self, tx):
        data = await self.get_transaction(tx)
        if data is None:
            raise Exception("Transaction not found")
        return {key: data[key] for key in ("account", "lt", "now", "mc_block_seqno", "finality")}

    async def get_confirmations(self, tx_hash, data=None) -> int:
        # A hash we recorded on a request was seen finalized; that cannot be undone
        if daemon_ctx.get().is_recorded(tx_hash):
            return FINALIZED_CONFIRMATIONS
        data = await self.get_transaction(tx_hash)
        return FINALIZED_CONFIRMATIONS if data and data["finality"] == "finalized" and not data["emulated"] else 0

    async def get_balance(self, address):
        reply = await self.request("accountStates", [("address", parse_address(address)), ("include_boc", "false")])
        return from_wei(int(reply["accounts"][0]["balance"]) if reply["accounts"] else 0, TONDaemon.DIVISIBILITY)

    async def get_jetton_wallet(self, owner):
        body = {
            "address": USDT_MASTER,
            "method": "get_wallet_address",
            "stack": [{"type": "slice", "value": address_cell_boc(parse_address(owner))}],
        }
        reply = await self.request("runGetMethod", body=body)
        if reply.get("exit_code") != 0 or reply["stack"][0]["type"] not in ("cell", "slice"):
            raise TonRPCError(f"get_wallet_address failed: exit code {reply.get('exit_code')}")
        return address_from_boc(reply["stack"][0]["value"])

    async def get_jetton_balance(self, owner):
        jetton_wallet = await self.get_jetton_wallet(owner)
        reply = await self.request("jetton/wallets", [("address", jetton_wallet), ("limit", 1)])
        wallets = reply["jetton_wallets"]
        return from_wei(int(wallets[0]["balance"]) if wallets else 0, USDT["decimals"])

    async def get_block(self, block, *args, **kwargs):
        raise NotImplementedError(NOT_SUPPORTED)

    async def get_block_txes(self, block):
        raise NotImplementedError(NOT_SUPPORTED)

    def is_address(self, address):
        try:
            parse_address(address)
            return True
        except ValueError:
            return False

    def normalize_address(self, address):
        try:
            return parse_address(address)
        except ValueError:
            raise Exception("Invalid address") from None

    async def get_payment_uri(self, address, amount, divisibility, contract=None):
        params = []
        if amount:
            params.append(f"amount={to_wei(amount, divisibility)}")
        if contract:
            params.append(f"jetton={friendly(contract, bounceable=True)}")
        return f"ton://transfer/{address}" + ("?" + "&".join(params) if params else "")

    async def process_tx_data(self, data):
        raise NotImplementedError(NOT_SUPPORTED)

    def get_tx_hash(self, tx_data):
        return tx_hash(tx_data)

    def get_wallet_key(self, xpub, contract=None, **extra_params):
        xpub = xpub.lower() if isinstance(xpub, str) else xpub
        if not contract:
            return xpub
        with contextlib.suppress(ValueError):  # load_wallet refuses it
            contract = parse_address(contract)
        return f"{xpub}_{contract}"

    def to_dict(self, obj):
        return json.loads(json.dumps(obj, default=str))

    def current_server(self):
        return redact_url(self.rpc.current_rpc.url)


class KeyStore(BaseKeyStore):
    def load_account_from_key(self):
        if not is_pubkey(self.key):
            raise Exception("Error loading wallet: invalid public key")
        self.key = self.public_key = self.key.lower()
        # The fee wallet of the sweep tool: controlled by the key, never an invoice address
        self.address = friendly(wallet_address(self.key, FEE_WALLET_ID), testnet=is_testnet())

    def add_privkey(self, privkey):
        raise Exception(NOT_SUPPORTED)

    @classmethod
    def load(cls, db):
        return cls(key=db.get("key", ""), contract=db.get("contract", None))

    def dump(self):
        return {"key": self.key, "contract": self.contract}


@dataclass
class Invoice(BaseInvoice):
    index: int = None
    watch_address: str = None  # raw; the jetton wallet of the deposit address for USDT


@dataclass
class Wallet(BaseWallet):
    contract: str = None

    next_index = StoredDBProperty("next_index", 0)

    @property
    def contract_addr(self):
        return self.contract

    def stop(self, block_number):
        super().stop(block_number)
        self.closed = True

    def save_db(self):
        # A closed wallet's expiry tasks still run; their stale copy must not overwrite the file (next_index)
        if not getattr(self, "closed", False):
            super().save_db()

    async def balance(self):
        # Own (fee wallet) address only; invoice addresses are summed by the sweep tool
        if self.contract:
            return await self.coin.get_jetton_balance(self.address)
        return await self.coin.get_balance(self.address)

    def parse_credit(self, tx):
        return jetton_credit(tx) if self.contract else native_credit(tx)

    async def create_payment_request_object(self, address, amount, message, expiration, timestamp):
        index = self.next_index
        if index >= MAX_INDEX:
            raise Exception("All deposit addresses of this wallet are used")
        self.next_index = index + 1  # on disk before the address is handed out: an index is never reused
        deposit = wallet_address(self.keystore.key, (USDT_OFFSET if self.contract else NATIVE_OFFSET) + index)
        return Invoice(
            # non-bounceable: the deposit wallet is not deployed, a bounceable transfer would come back
            address=friendly(deposit, testnet=is_testnet()),
            message=message,
            time=timestamp,
            amount=amount,
            sent_amount=Decimal(0),
            exp=expiration,
            id=secrets.token_urlsafe(),
            height=await self.coin.get_block_number(),
            index=index,
            watch_address=await self.coin.get_jetton_wallet(deposit) if self.contract else deposit,
        )

    def add_payment_request(self, req, save_db=True):
        self.receive_requests[req.id] = req
        self.request_addresses[req.watch_address] = req.id
        if save_db:
            self.save_db()
        return req

    def remove_from_detection_dict(self, req):
        if self.request_addresses.get(req.watch_address) == req.id:
            self.request_addresses.pop(req.watch_address, None)


class TONDaemon(BlockProcessorDaemon):
    name = "TON"
    BASE_SPEC_FILE = "daemons/spec/eth.json"
    DEFAULT_PORT = 5013

    DIVISIBILITY = 9
    BLOCK_TIME = 5
    DEFAULT_MAX_SYNC_BLOCKS = 1  # no block scanning: watched addresses are polled
    PAGE_SIZE = 1000  # toncenter maximum
    MAX_PAGES = 20
    ACCOUNTS_PER_REQUEST = 100

    UNIT = "nanoton"

    KEYSTORE_CLASS = KeyStore
    WALLET_CLASS = Wallet
    INVOICE_CLASS = Invoice

    def __init__(self):
        super().__init__()
        if self.NET not in SERVERS:
            raise ValueError(f"Invalid network passed: {self.NET}. Valid choices are {', '.join(SERVERS)}.")

    def get_default_server_url(self):
        return SERVERS.get(self.NET, SERVERS["mainnet"])

    async def create_coin(self, archive=False):
        multi_provider = MultipleProviderRPC([TonRPCProvider(server) for server in self.SERVER])
        await multi_provider.start()
        self.coin = TONFeatures(multi_provider)
        logger.info(f"Using server {self.coin.current_server()}")

    async def shutdown_coin(self, final=False, archive_only=False):
        if not hasattr(self, "coin"):
            return
        await self.coin.rpc.stop()
        for provider in self.coin.rpc.providers:
            await provider.close()

    def is_recorded(self, tx_hash):
        return any(tx_hash in req.tx_hashes for w in self.wallets.values() for req in w.receive_requests.values())

    def normalize_contract(self, contract):
        try:
            contract = parse_address(contract)
        except ValueError:
            raise Exception(INVALID_CONTRACT) from None
        if contract != USDT_MASTER or self.NET != "mainnet":
            raise Exception(INVALID_CONTRACT)
        return contract

    async def load_wallet(self, xpub, contract, diskless=False, extra_params=None):
        if contract:
            contract = self.normalize_contract(contract)
        wallet_key = self.coin.get_wallet_key(xpub, contract)
        if wallet_key in self.wallets:
            return self.wallets[wallet_key]
        if not xpub:
            return None
        xpub = xpub.lower()
        # Never diskless: the wallet holds no secret, and a memory-only one would hand out index 0 again
        wallet_path = os.path.join(self.get_wallet_path(), wallet_key)
        if not os.path.exists(wallet_path):
            self.restore(xpub, wallet_path=wallet_path, contract=contract)
        storage = Storage(wallet_path)
        wallet = self.WALLET_CLASS(self.coin, WalletDB(storage.read()), storage)
        wallet.contract = contract
        if contract:
            wallet.symbol = USDT["symbol"]
            wallet.divisibility = USDT["decimals"]
        self.wallets[wallet_key] = wallet
        self.wallets_updates[wallet_key] = deque(maxlen=self.POLLING_CAP)
        await wallet.start(self.latest_blocks.copy())
        return wallet

    async def process_pending(self):
        while self.running:
            try:
                await self.poll_once()
            except Exception as e:
                logger.error(f"Polling failed: {type(e).__name__}: {e}")
            await asyncio.sleep(self.BLOCK_TIME)

    async def poll_once(self):
        height = await self.coin.get_block_number()
        if height != self.latest_height:
            self.latest_height = height
            # the backend refreshes confirmations only on this event
            await self.trigger_event({"event": "new_block", "height": height}, None)
        for wallet_key in list(self.wallets):
            await self.poll_wallet(wallet_key)
        self.synchronized = True

    async def fetch_transactions(self, accounts, start_utime):
        for i in range(0, len(accounts), self.ACCOUNTS_PER_REQUEST):
            chunk = accounts[i : i + self.ACCOUNTS_PER_REQUEST]
            first = await self.fetch_page(chunk, start_utime, 0)
            # A full page may be spam on one address: read each address alone, so it can hide only itself
            groups = [[a] for a in chunk] if len(first) >= self.PAGE_SIZE and len(chunk) > 1 else [chunk]
            for group in groups:
                for page_no in range(self.MAX_PAGES):
                    page = first if group is chunk and page_no == 0 else await self.fetch_page(group, start_utime, page_no)
                    for tx in page:
                        yield tx
                    if len(page) < self.PAGE_SIZE:
                        break
                else:
                    logger.error(f"More than {self.MAX_PAGES * self.PAGE_SIZE} transactions on {group[0]}; rest not read")

    async def fetch_page(self, accounts, start_utime, page_no):
        params = [("account", a) for a in accounts]
        params += [("start_utime", start_utime), ("sort", "asc"), ("limit", self.PAGE_SIZE)]
        reply = await self.coin.request("transactions", [*params, ("offset", page_no * self.PAGE_SIZE)])
        if not isinstance(reply["transactions"], list):
            raise TonRPCError("Malformed transactions reply")
        return reply["transactions"]

    async def poll_wallet(self, wallet_key):
        wallet = self.wallets.get(wallet_key)
        if wallet is None:
            return
        reqs = [r for r in list(wallet.receive_requests.values()) if wallet.request_addresses.get(r.watch_address) == r.id]
        reqs = [r for r in reqs if r.status == PR_UNPAID]
        if not reqs:
            return
        # No cursor: lt order across shards is not the order in which transactions become final, so a cursor
        # could pass a payment. Open requests are few and short-lived, so every poll reads them again from the
        # oldest one's creation time; tx_hashes stops a second credit. Anything unreadable is read again next poll.
        try:
            accounts = sorted({r.watch_address for r in reqs})
            async for tx in self.fetch_transactions(accounts, min(r.time for r in reqs) - UTIME_MARGIN):
                try:
                    if not flag(tx["emulated"]) and tx["finality"] == "finalized":
                        await self.process_tx(wallet, tx)
                except Exception as e:
                    logger.error(f"Transaction not processed on {wallet_key}, retried next poll: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"Polling {wallet_key} failed: {type(e).__name__}: {e}")

    async def process_tx(self, wallet, tx):
        account = parse_address(tx["account"])
        if account not in wallet.request_addresses:
            return
        credit = wallet.parse_credit(tx)
        if credit is None:
            return
        value, sender = credit
        await self.process_transaction(
            Transaction(tx_hash(tx), sender, account, value, contract=wallet.contract, divisibility=wallet.divisibility)
        )

    async def process_transaction(self, tx):
        # Matches the destination: one address per request
        amount = from_wei(tx.value, tx.divisibility)
        for wallet_key, wallet in list(self.wallets.items()):
            if wallet.contract == tx.contract and tx.to in wallet.request_addresses:
                logger.info(f"{wallet.symbol} credit {amount} to {tx.to} in {tx.hash}")
                await wallet.process_new_payment(tx.to, tx, amount, wallet_key)

    @rpc
    async def getinfo(self, wallet=None):
        info = await super().getinfo(wallet=wallet)
        info["next_index"] = {key: w.next_index for key, w in self.wallets.items()}
        return info

    @rpc
    def getservers(self, wallet=None):
        return [redact_url(server) for server in self.SERVER]

    @rpc(requires_wallet=True)
    def setrequestaddress(self, key, address, wallet):
        return False

    @rpc(requires_network=True)
    async def rescan_blocks(self, start_block, end_block=None, wallet=None):
        raise NotImplementedError("rescan_blocks is not supported for TON: payments are found by polling watched addresses")

    @rpc(requires_network=True)
    async def add_peer(self, url, wallet=None):
        raise NotImplementedError(NOT_SUPPORTED)

    @rpc(requires_network=True)
    async def broadcast(self, tx, wallet=None):
        raise NotImplementedError(NOT_SUPPORTED)

    @rpc(requires_network=True)
    async def get_default_fee(self, tx, wallet=None):
        raise NotImplementedError(NOT_SUPPORTED)

    @rpc
    def get_tx_hash(self, tx_data, wallet=None):
        return self.coin.get_tx_hash(tx_data)

    @rpc
    def get_tx_size(self, tx_data, wallet=None):
        raise NotImplementedError(NOT_SUPPORTED)

    @rpc(requires_network=True)
    async def get_used_fee(self, tx_hash, wallet=None):
        data = await self.coin.get_transaction(tx_hash)
        if data is None:
            raise Exception("Transaction not found")
        return self.coin.to_dict(from_wei(int(data["total_fees"]), self.DIVISIBILITY))

    @rpc(requires_network=True)
    async def gettransaction(self, tx, wallet=None):
        data = await self.coin.get_tx_receipt(tx)
        data["confirmations"] = await self.coin.get_confirmations(tx)
        return data

    @rpc(requires_wallet=True)
    async def listaddresses(self, unused=False, funded=False, balance=False, wallet=None):
        unused, funded, balance = str_to_bool(unused), str_to_bool(funded), str_to_bool(balance)
        wallet_obj = self.wallets[wallet]
        addr_balance = await wallet_obj.balance()
        if (unused and addr_balance > 0) or (funded and addr_balance == 0):
            return []
        if balance:
            return [(wallet_obj.address, self.coin.to_dict(addr_balance))]
        return [wallet_obj.address]

    @rpc
    def make_seed(self, nbits=128, language="english", full_info=False, wallet=None):
        raise NotImplementedError(NOT_SUPPORTED)

    @rpc(requires_wallet=True, requires_network=True)
    async def payto(self, destination, amount, fee=None, feerate=None, gas=None, unsigned=False, wallet=None, *args, **kwargs):
        raise NotImplementedError(NOT_SUPPORTED)

    @rpc(requires_wallet=True)
    def signmessage(self, address=None, message=None, wallet=None):
        raise NotImplementedError(NOT_SUPPORTED)

    def _sign_transaction(self, tx, private_key):
        raise NotImplementedError(NOT_SUPPORTED)

    @rpc
    def verifymessage(self, address, signature, message, wallet=None):
        raise NotImplementedError(NOT_SUPPORTED)

    @rpc
    async def modifypaymenturl(self, url, amount, divisibility=None, wallet=None):
        # the backend passes its display divisibility (8 for TON); the URI amount is in base units
        decimals = USDT["decimals"] if "jetton=" in url else self.DIVISIBILITY
        return modify_payment_url("amount", url, to_wei(Decimal(amount), decimals))

    @rpc
    def get_tokens(self, wallet=None):
        return {USDT["symbol"]: USDT_MASTER} if self.NET == "mainnet" else {}

    @rpc
    def validatecontract(self, address, wallet=None):
        try:
            return bool(self.normalize_contract(address))
        except Exception:
            return False

    @rpc(requires_network=True)
    async def readcontract(self, address, function, *args, wallet=None, **kwargs):
        self.normalize_contract(address)
        if function == "symbol":
            return USDT["symbol"]
        if function == "decimals":
            return USDT["decimals"]
        if function == "balanceOf":
            return to_wei(await self.coin.get_jetton_balance(args[0]), USDT["decimals"])
        raise Exception(f"Unsupported contract function: {function}")

    @rpc(requires_network=True)
    async def transfer(self, *args, wallet=None, **kwargs):
        raise NotImplementedError(NOT_SUPPORTED)


if __name__ == "__main__":
    daemon = TONDaemon()
    daemon.start()
