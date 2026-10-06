import asyncio
import base64
import copy
import hashlib
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

DAEMONS_DIR = os.environ.get("BITCART_DAEMONS_DIR", "/home/electrum/site/daemons")
sys.path.insert(0, DAEMONS_DIR)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))

import ton  # noqa: E402
import tonaddr  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

PUBKEY = "b82993f90f94b42371ada6d7ec2ad64e84df8bf7008830161435d26da2203956"
PUBKEY2 = "f8206f1913608391b90547a284012d5ba403514d48e271d45dc17f40867f58f3"
SENDER = "0:6BE97A79B977E9A4F32C669B7781369DE09709BC4E854F4B0E7810F9FF505DE3"
SENDER_OWNER = "0:A5B03F281AEFE1EEA0943B3B2FCCF4956AFF5F9A56D2DB1E681A787068F508A0"
SERVER = "https://toncenter.com/api/v3?api_key=SECRET123"


def load_fixture(name):
    with open(FIXTURES / name) as f:
        return json.load(f)


def fake_hash(n):
    return base64.b64encode(hashlib.sha256(str(n).encode()).digest()).decode()


def hex_hash(n):
    return hashlib.sha256(str(n).encode()).hexdigest()


def native_tx(account, value, lt, n=None, source=SENDER, bounce=False, bounced=False, aborted=False, **extra):
    tx = {
        "account": account,
        "hash": fake_hash(lt if n is None else n),
        "lt": str(lt),
        "now": int(time.time()),
        "mc_block_seqno": 100,
        "finality": "finalized",
        "emulated": False,
        "total_fees": "1000",
        "description": {
            "aborted": aborted,
            "compute_ph": {"skipped": True, "reason": "no_state"},
            "action": None,
        },
        "in_msg": {
            "source": source,
            "destination": account,
            "value": str(value),
            "bounce": bounce,
            "bounced": bounced,
            "opcode": None,
            "message_content": {"body": "te6cckEBAQEAAgAAAEysuc0=", "decoded": None},
        },
        "out_msgs": [],
    }
    tx.update(extra)
    return tx


def usdt_tx(jetton_wallet, amount, lt, n=None, opcode="0x178d4519", aborted=False, success=True, exit_code=0, **extra):
    tx = native_tx(jetton_wallet, 50_000_000, lt, n=n, source="0:" + "AB" * 32, bounce=True, **extra)
    tx["description"] = {
        "aborted": aborted,
        "compute_ph": {"skipped": False, "success": success, "exit_code": exit_code},
        "action": {"success": success},
    }
    tx["in_msg"]["opcode"] = opcode
    tx["in_msg"]["decoded_opcode"] = "jetton_internal_transfer"
    tx["in_msg"]["message_content"]["decoded"] = {
        "@type": "jetton_internal_transfer",
        "amount": {"@type": "var_uint", "value": str(amount)},
        "from": {"@type": "addr_std", "workchain_id": "0", "address": SENDER_OWNER.split(":")[1]},
    }
    return tx


def jetton_wallet_of(owner):
    # stands in for the USDT master's get_wallet_address
    return "0:" + hashlib.sha256(b"jetton" + owner.encode()).hexdigest().upper()


class FakeRPC:
    """Stands in for MultipleProviderRPC: toncenter v3 replies by endpoint, recorded calls."""

    def __init__(self):
        self.seqno = 1000
        self.global_id = -239
        self.txs = []
        self.balances = {}
        self.jetton_balances = {}
        self.failures = {}  # endpoint -> exception, raised once per entry in the list
        self.overrides = {}  # endpoint -> reply returned as is
        self.calls = []
        self.current_rpc = type("P", (), {"url": SERVER})()
        self.providers = []

    async def send_request(self, endpoint, params=(), body=None):
        self.calls.append((endpoint, list(params), body))
        if self.failures.get(endpoint):
            raise self.failures[endpoint].pop(0)
        if endpoint in self.overrides:
            return copy.deepcopy(self.overrides[endpoint])
        query = {}
        for key, value in params:
            query.setdefault(key, []).append(value)
        if endpoint == "masterchainInfo":
            return {"last": {"seqno": self.seqno, "global_id": self.global_id}}
        if endpoint == "transactions":
            return {"transactions": self._transactions(query)}
        if endpoint == "runGetMethod":
            owner = tonaddr.address_from_boc(body["stack"][0]["value"])
            assert body["address"] == tonaddr.USDT_MASTER and body["method"] == "get_wallet_address"
            return {"exit_code": 0, "stack": [{"type": "cell", "value": tonaddr.address_cell_boc(jetton_wallet_of(owner))}]}
        if endpoint == "accountStates":
            address = query["address"][0]
            return {
                "accounts": [{"address": address, "balance": str(self.balances[address])}] if address in self.balances else []
            }
        if endpoint == "jetton/wallets":
            address = query["address"][0]
            found = address in self.jetton_balances
            return {"jetton_wallets": [{"address": address, "balance": str(self.jetton_balances[address])}] if found else []}
        raise AssertionError(f"unexpected endpoint {endpoint}")

    def _transactions(self, query):
        if "hash" in query:
            wanted = query["hash"][0]
            return [copy.deepcopy(t) for t in self.txs if ton.tx_hash(t) == wanted][:1]
        accounts = set(query["account"])
        found = [t for t in self.txs if t["account"] in accounts]
        if "start_utime" in query:
            found = [t for t in found if t["now"] >= int(query["start_utime"][0])]
        found.sort(key=lambda t: int(t["lt"]))
        offset = int(query.get("offset", ["0"])[0])
        return copy.deepcopy(found[offset : offset + int(query["limit"][0])])

    def add(self, *txs):
        self.txs.extend(txs)

    def polls(self):
        return [c for c in self.calls if c[0] == "transactions" and any(k == "account" for k, _ in c[1])]

    async def stop(self):
        pass


def make_daemon(tmp_path, monkeypatch, rpc, network="mainnet"):
    monkeypatch.setenv("TON_DATA_PATH", str(tmp_path))
    monkeypatch.setenv("TON_NETWORK", network)
    rpc.global_id = -3 if network == "testnet" else -239
    monkeypatch.setenv("TON_SERVER", SERVER)
    monkeypatch.setenv("TON_POLLING_CAP", "100")
    monkeypatch.chdir(Path(DAEMONS_DIR).parent)  # spec files are opened relative to the bitcart root
    d = ton.TONDaemon()
    d.coin = ton.TONFeatures(rpc)
    d.loop = asyncio.get_running_loop()
    return d


@pytest.fixture
def rpc():
    return FakeRPC()


@pytest.fixture
async def daemon(tmp_path, monkeypatch, rpc):
    return make_daemon(tmp_path, monkeypatch, rpc)


async def load(daemon, pubkey=PUBKEY, contract=None):
    wallet = await daemon.load_wallet(pubkey, contract)
    return wallet.wallet_key, wallet


async def request(daemon, wallet_key, amount="1"):
    data = await daemon.add_request(Decimal(amount), "order", 3600, force=True, wallet=wallet_key)
    return data, daemon.wallets[wallet_key].get_request(data["request_id"])


def events(daemon, wallet_key, name):
    return [e for e in daemon.wallets_updates[wallet_key] if e["event"] == name]
