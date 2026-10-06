import base64
import sys
from pathlib import Path

import pytest
from pytoniq_core.crypto.keys import mnemonic_new
from ton_core import Cell, JettonTransferBody, MessageAny
from ton_core.contrib.utils import normalize_hash

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sweep

needs_tonaddr = pytest.mark.skipif(sweep.tonaddr is None, reason="../daemon/tonaddr.py does not exist yet")


@pytest.fixture(scope="session")
def seed_words():
    return mnemonic_new(24)


@pytest.fixture(scope="session")
def keys(seed_words):
    return sweep.Keys(seed_words)


def decode_external(boc):
    """(message, signature, unsigned body cell, subwallet, valid_until, seqno, [(mode, internal message)])."""
    msg = MessageAny.deserialize(Cell.one_from_boc(base64.b64decode(boc)).begin_parse())
    body = msg.body.begin_parse()
    signature = body.load_bytes(64)
    unsigned = body.to_cell()
    head = unsigned.begin_parse()
    subwallet, valid_until, seqno = head.load_uint(32), head.load_uint(32), head.load_uint(32)
    out = []
    while head.remaining_refs:
        mode = head.load_uint(8)
        out.append((mode, MessageAny.deserialize(head.load_ref().begin_parse())))
    return msg, signature, unsigned, subwallet, valid_until, seqno, out


def raw(address):
    return f"0:{address.hash_part.hex().upper()}"


class FakeChain(sweep.Api):
    """Answers the toncenter calls the tool makes and applies sent messages, roughly like the chain."""

    FEE = 1_000_000

    def __init__(self):
        super().__init__(None, "mainnet")
        self.accounts = {}  # raw -> State
        self.jettons = {}  # owner raw -> [jetton wallet raw, balance]
        self.txs = {}  # raw -> list of transactions
        self.calls = []
        self.sent = []  # (sender raw, decoded external)
        self.fail_send_at = None

    def add(self, address, balance, status="uninit"):
        state = self.accounts.setdefault(address, sweep.State())
        state.balance += balance
        if state.status == "nonexist":
            state.status = status
        if state.status == "active":
            state.code_hash = sweep.V3R2_CODE_HASH

    def add_usdt(self, owner, amount):
        jetton_wallet = "0:" + (owner[2:][::-1])
        self.jettons[owner] = [jetton_wallet, amount]
        self.add(jetton_wallet, 10_000_000, "active")
        self.accounts[jetton_wallet].code_hash = "jetton"

    def call(self, method, path, params=None, body=None):
        self.calls.append((method, path))
        if path == "/walletStates":
            items = [(a, self.accounts.get(a)) for a in params["address"]]
            wallets = [
                {"address": a, "status": s.status, "balance": str(s.balance), "seqno": s.seqno, "code_hash": s.code_hash}
                for a, s in items
                if s and s.status != "nonexist"
            ]
            return {"wallets": wallets}
        if path == "/runGetMethod":
            return self.get_method_call(body)
        if path == "/transactions":
            return {"transactions": list(reversed(self.txs.get(params["account"], [])))}
        if path == "/message":
            return self.receive(body["boc"])
        raise AssertionError(path)

    def get_method_call(self, body):
        if body["method"] == "get_wallet_address":
            owner = sweep.tonaddr.address_from_boc(body["stack"][0]["value"])
            jetton_wallet = self.jettons.get(owner, ["0:" + owner[2:][::-1]])[0]
            return {"exit_code": 0, "stack": [{"type": "cell", "value": sweep.tonaddr.address_cell_boc(jetton_wallet)}]}
        owner, (_, balance) = next((o, j) for o, j in self.jettons.items() if j[0] == body["address"])
        cells = [sweep.tonaddr.address_cell_boc(a) for a in (owner, sweep.tonaddr.USDT_MASTER)]
        return {
            "exit_code": 0,
            "stack": [{"type": "num", "value": hex(balance)}] + [{"type": "cell", "value": c} for c in cells],
        }

    def receive(self, boc):
        if self.fail_send_at is not None and len(self.sent) == self.fail_send_at:
            raise RuntimeError("power cut")
        decoded = decode_external(boc)
        msg, _, _, _, _, seqno, out = decoded
        sender = raw(msg.info.dest)
        self.sent.append((sender, decoded))
        state = self.accounts[sender]
        assert seqno == state.seqno
        state.status, state.seqno, state.code_hash = "active", seqno + 1, sweep.V3R2_CODE_HASH
        state.balance -= self.FEE
        for mode, internal in out:
            value = state.balance if mode & 128 else internal.info.value.grams
            state.balance -= value
            self.deliver(sender, raw(internal.info.dest), value, internal.body)
            if mode & 32 and state.balance == 0:
                self.accounts[sender] = sweep.State()
        tx = {"hash": "tx", "finality": "finalized", "emulated": False, "in_msg": {"hash_norm": self.norm(msg)}}
        tx["description"] = {"aborted": False, "compute_ph": {"success": True}, "action": {"success": True}}
        self.txs.setdefault(sender, []).append(tx)
        return {"message_hash": "x"}

    def deliver(self, sender, dest, value, body):
        owner = next((o for o, j in self.jettons.items() if j[0] == dest), None)
        if owner == sender and body.bits and body.begin_parse().load_uint(32) == 0x0F8A7EA5:
            transfer = JettonTransferBody.deserialize(body.begin_parse())
            self.jettons[owner][1] -= transfer.jetton_amount
            self.add(raw(transfer.response_address), value - self.FEE)
            return
        self.add(dest, value)

    @staticmethod
    def norm(msg):
        return base64.b64encode(bytes.fromhex(normalize_hash(msg))).decode()


@pytest.fixture
def chain():
    return FakeChain()
