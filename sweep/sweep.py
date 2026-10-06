"""Operator sweep tool for the Bitcart TON plugin: moves TON and USDT from deposit wallets to the operator's main wallet."""

import argparse
import base64
import getpass
import importlib.util
import json
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

import requests
from pytoniq_core.crypto.keys import mnemonic_is_valid, mnemonic_to_private_key, words
from pytoniq_core.crypto.signature import sign_message
from ton_core import (
    CONTRACT_CODES,
    Address,
    Cell,
    ContractVersion,
    ExternalMessage,
    InternalMessage,
    JettonTransferBody,
    PublicKey,
    StateInit,
    WalletV3Data,
    begin_cell,
)
from ton_core.contrib.utils import normalize_hash

SERVERS = {"mainnet": "https://toncenter.com/api/v3", "testnet": "https://testnet.toncenter.com/api/v3"}
TON, USDT = 10**9, 10**6
V3R2_CODE = Cell.one_from_boc(CONTRACT_CODES[ContractVersion.WalletV3R2])
V3R2_CODE_HASH = base64.b64encode(V3R2_CODE.hash).decode()
# USDT jetton wallets need about 0.0007 TON per transfer on chain today; 0.05 is the usual wallet value, the excess
# goes back to the fee wallet as response destination.
JETTON_ATTACH = 50_000_000
USDT_GAS_NEED = JETTON_ATTACH + 10_000_000  # plus the deposit wallet's own deploy, compute and forward fees
FUND_FEE = 10_000_000  # kept back in the fee wallet for its own fees per funding transfer
DEFAULT_MIN_TON, DEFAULT_MIN_USDT = "0.01", "0.1"  # about 10 x the chain fee of each sweep type (see README)
SEND_ALL_DESTROY = 128 + 32
PAY_FEES_IGNORE_ERRORS = 1 + 2
VALID_FOR = 300
POLL = 3


class Fail(Exception):
    """A step failed; the message is safe to print (never holds secrets or the API key)."""


def say(*parts):
    print(*parts, flush=True)  # noqa: T201


def load_tonaddr():
    here = Path(__file__).resolve().parent
    for path in (here / "tonaddr.py", here.parent / "daemon" / "tonaddr.py"):
        if path.exists():
            spec = importlib.util.spec_from_file_location("tonaddr", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    return None


tonaddr = load_tonaddr()


WORDS = frozenset(words)


def stdin_is_tty():
    return sys.stdin.isatty()


class Keys:
    """Ed25519 key pair from the seed; the private half stays in memory only."""

    def __init__(self, seed_words):
        self.public, self._secret = mnemonic_to_private_key(seed_words)

    def __repr__(self):
        return f"Keys(public={self.public.hex()})"

    def sign(self, data):
        return sign_message(data, self._secret)


def read_seed():
    if not stdin_is_tty():
        raise Fail("the seed prompt needs a terminal: run the container with -it")
    seed_words = getpass.getpass("Seed, 24 words (hidden): ").strip().lower().split()
    if len(seed_words) != 24:
        raise Fail(f"expected 24 words, got {len(seed_words)}")
    if bad := [n for n, word in enumerate(seed_words, 1) if word not in WORDS]:
        raise Fail(f"word number(s) {bad} not in the TON word list")
    if not mnemonic_is_valid(seed_words):
        raise Fail("not a valid TON seed (a BIP39 seed is not accepted)")
    return Keys(seed_words)


@dataclass
class Wallet:
    kind: str  # ton, usdt or fee
    index: int | None
    subwallet: int
    address: str
    state_init: StateInit


def derive(public, kind, index, subwallet):
    state_init = StateInit(code=V3R2_CODE, data=WalletV3Data(PublicKey(public), 0, subwallet).serialize())
    address = f"0:{state_init.serialize().hash.hex().upper()}"
    other = tonaddr.wallet_address(public.hex(), subwallet)
    if other != address:
        raise Fail(f"address mismatch for subwallet {subwallet}: library {address}, tonaddr {other}; stopping")
    return Wallet(kind, index, subwallet, address, state_init)


def derive_all(public, ton_count, usdt_count):
    if tonaddr is None:
        raise Fail("tonaddr.py not found next to sweep.py: the address cross-check cannot run")
    wallets = [derive(public, "ton", n, tonaddr.NATIVE_OFFSET + n) for n in range(ton_count)]
    wallets += [derive(public, "usdt", n, tonaddr.USDT_OFFSET + n) for n in range(usdt_count)]
    return wallets, derive(public, "fee", None, tonaddr.FEE_WALLET_ID)


def internal(dest, value, bounce, body=None):
    return InternalMessage(dest=Address(dest), value=value, bounce=bounce, body=body)


def jetton_body(amount, dest, response):
    return JettonTransferBody(Address(dest), amount, Address(response), forward_amount=1).serialize()


def external(keys, wallet, seqno, deployed, messages, now=None):
    """Signed v3r2 external message as (base64 BOC, normalized hash, valid_until)."""
    if not 1 <= len(messages) <= 4:
        raise ValueError("a v3r2 message carries 1 to 4 internal messages")
    valid_until = int(now or time.time()) + VALID_FOR
    body = begin_cell().store_uint(wallet.subwallet, 32).store_uint(valid_until, 32).store_uint(seqno, 32)
    for mode, message in messages:
        body.store_uint(mode, 8).store_ref(message.serialize())
    unsigned = body.end_cell()
    signed = begin_cell().store_bytes(keys.sign(unsigned.hash)).store_cell(unsigned).end_cell()
    message = ExternalMessage(dest=Address(wallet.address), body=signed, state_init=None if deployed else wallet.state_init)
    return base64.b64encode(message.serialize().to_boc()).decode(), normalize_hash(message), valid_until


@dataclass
class State:
    status: str = "nonexist"
    balance: int = 0
    seqno: int = 0
    code_hash: str | None = None

    @property
    def active(self):
        return self.status == "active"


class Api:
    def __init__(self, server, network):
        parts = urlsplit(server or SERVERS[network])
        query = parse_qs(parts.query)
        self.key = query.get("api_key", [None])[0]
        self.base = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
        self.host = parts.netloc
        self.interval = 0.15 if self.key else 1.1  # toncenter allows 1 request/s without a key, 10 with one
        self.last = 0.0
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "bitcart-ton-sweep/1"  # toncenter's CDN rejects the default agent

    def call(self, method, path, params=None, body=None):
        params = dict(params or {}) | ({"api_key": self.key} if self.key else {})
        for attempt in range(4):
            time.sleep(max(0.0, self.last + self.interval - time.monotonic()))
            self.last = time.monotonic()
            try:
                response = self.session.request(method, self.base + path, params=params, json=body, timeout=30)
            except requests.RequestException as exc:
                error = f"{type(exc).__name__}"
            else:
                if response.status_code < 400:
                    return response.json()
                error = f"HTTP {response.status_code} {response.text[:200]}"
                if response.status_code != 429 and response.status_code < 500:
                    break
            time.sleep(2**attempt)
        raise Fail(f"{self.host} {method} {path}: {error}")

    def states(self, addresses):
        result = {}
        for start in range(0, len(addresses), 100):
            data = self.call("GET", "/walletStates", {"address": addresses[start : start + 100]})
            for item in data["wallets"]:
                result[tonaddr.parse_address(item["address"])] = State(
                    item["status"], int(item["balance"]), int(item.get("seqno") or 0), item.get("code_hash")
                )
        return {address: result.get(address, State()) for address in addresses}

    def state(self, address):
        return self.states([address])[address]

    def get_method(self, address, method, stack):
        data = self.call("POST", "/runGetMethod", body={"address": address, "method": method, "stack": stack})
        if data.get("exit_code") != 0:
            raise Fail(f"get-method {method} on {address} failed with exit code {data.get('exit_code')}")
        return data["stack"]

    def jetton_wallet(self, owner):
        arg = base64.b64encode(begin_cell().store_address(Address(owner)).end_cell().to_boc()).decode()
        stack = self.get_method(tonaddr.USDT_MASTER, "get_wallet_address", [{"type": "slice", "value": arg}])
        return tonaddr.address_from_boc(stack[0]["value"])

    def jetton_balance(self, jetton_wallet, owner, state=None):
        if not (state or self.state(jetton_wallet)).active:
            return 0
        stack = self.get_method(jetton_wallet, "get_wallet_data", [])
        found = [tonaddr.address_from_boc(stack[n]["value"]) for n in (1, 2)]
        if found != [owner, tonaddr.USDT_MASTER]:
            raise Fail(f"jetton wallet {jetton_wallet} reports owner/master {found}, expected {owner}")
        return int(stack[0]["value"], 16)

    def find_transaction(self, address, msg_hash):
        data = self.call("GET", "/transactions", {"account": address, "limit": 10, "sort": "desc"})
        wanted = base64.b64encode(bytes.fromhex(msg_hash)).decode()
        for tx in data["transactions"]:
            if (tx.get("in_msg") or {}).get("hash_norm") == wanted:
                return tx
        return None

    def send(self, boc):
        self.call("POST", "/message", body={"boc": boc})


def fmt(amount, decimals):
    return f"{Decimal(amount) / 10**decimals:.{decimals}f}"


def units(text, decimals, name):
    try:
        value = Decimal(text) * 10**decimals
    except InvalidOperation:
        raise Fail(f"{name} is not a number: {text}") from None
    if value < 0 or value != value.to_integral_value():
        raise Fail(f"{name} must be positive with at most {decimals} decimals")
    return int(value)


def show(raw, testnet):
    return tonaddr.friendly(raw, bounceable=False, testnet=testnet)


def check_network_flag(text, testnet):
    if ":" not in text and bool(base64.urlsafe_b64decode(text.replace("+", "-").replace("/", "_"))[0] & 0x80) != testnet:
        raise Fail("the destination address is for the other network")


def load_destination(path, own, testnet):
    if path.exists():
        text = json.loads(path.read_text())["destination"]
    else:
        say(f"No config at {path}: first run.")
        text = input("Destination (your main wallet address): ").strip()
    try:
        dest = tonaddr.parse_address(text)
    except ValueError as exc:
        raise Fail(f"destination address is not valid: {exc}") from None
    check_network_flag(text, testnet)
    if dest in own:
        raise Fail("the destination is one of this seed's own deposit or fee wallets; refusing")
    if not path.exists():
        say(f"Destination: {tonaddr.friendly(dest, bounceable=True, testnet=testnet)}")
        if input("Save this destination? Type yes: ").strip() != "yes":
            raise Fail("destination not confirmed")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"destination": tonaddr.friendly(dest, bounceable=True, testnet=testnet)}) + "\n")
    return dest


@dataclass
class Row:
    wallet: Wallet
    state: State
    usdt: int = 0
    jetton_wallet: str | None = None
    action: str = ""

    @property
    def sweeps_usdt(self):
        return self.action == "sweep" and self.wallet.kind == "usdt" and self.usdt > 0

    def gas_needed(self):
        return max(0, USDT_GAS_NEED - self.state.balance) if self.sweeps_usdt else 0


def decide(row, min_ton, min_usdt):
    if row.wallet.kind == "usdt" and row.usdt:
        return "sweep" if row.usdt >= min_usdt else "skip: below limit"
    # TON sent to a USDT address by mistake is swept like a TON deposit
    if not row.state.balance:
        return "skip: empty"
    return "sweep" if row.state.balance >= min_ton else "skip: below limit"


def plan(api, wallets, fee, min_ton, min_usdt):
    states = api.states([w.address for w in wallets] + [fee.address])
    rows = [Row(w, states[w.address]) for w in wallets]
    usdt_rows = [r for r in rows if r.wallet.kind == "usdt"]
    # the jetton wallet address comes from the master's get-method, never from an index listing
    for row in usdt_rows:
        row.jetton_wallet = api.jetton_wallet(row.wallet.address)
    jetton_states = api.states([r.jetton_wallet for r in usdt_rows])
    for row in rows:
        if row.jetton_wallet:
            row.usdt = api.jetton_balance(row.jetton_wallet, row.wallet.address, jetton_states[row.jetton_wallet])
        row.action = decide(row, min_ton, min_usdt)
        if row.state.active and row.state.code_hash != V3R2_CODE_HASH:
            row.action = "skip: not a v3r2 wallet"
    return rows, states[fee.address]


def print_plan(rows, fee, fee_state, testnet):
    say(f"{'index':>5}  {'kind':4}  {'address':48}  {'TON':>14}  {'USDT':>12}  action")
    for r in rows:
        usdt = fmt(r.usdt, 6) if r.wallet.kind == "usdt" else ""
        ton = fmt(r.state.balance, 9)
        say(f"{r.wallet.index:>5}  {r.wallet.kind:4}  {show(r.wallet.address, testnet)}  {ton:>14}  {usdt:>12}  {r.action}")
    todo = [r for r in rows if r.action == "sweep"]
    say(
        f"To sweep: {len(todo)} addresses, {fmt(sum(r.state.balance for r in todo if not r.sweeps_usdt), 9)} TON, "
        f"{fmt(sum(r.usdt for r in todo), 6)} USDT; skipped: {len(rows) - len(todo)}"
    )
    gas = [r.gas_needed() for r in todo if r.gas_needed()]
    # gas returns to the fee wallet after each sweep, so one funding at a time is the real minimum
    need = max(gas, default=0) + FUND_FEE if gas else 0
    enough = "enough" if fee_state.balance >= need else "NOT enough: top it up"
    say(
        f"Fee wallet {show(fee.address, testnet)}: {fmt(fee_state.balance, 9)} TON; USDT sweeps need at least "
        f"{fmt(need, 9)} TON ({fmt(sum(gas) + FUND_FEE * len(gas), 9)} if no gas came back): {enough}"
    )


def wait_final(api, address, msg_hash, valid_until):
    while time.time() < valid_until + 60:
        tx = api.find_transaction(address, msg_hash)
        if tx and tx.get("finality") == "finalized" and not tx.get("emulated"):
            d = tx["description"]
            if d.get("aborted") or not d.get("compute_ph", {}).get("success") or not d.get("action", {}).get("success"):
                raise Fail(f"transaction {tx['hash']} on {address} failed on chain")
            return tx
        time.sleep(POLL)
    raise Fail(
        f"no final transaction on {address} for our message, which has now expired and can no longer apply; "
        "check the explorer, then run again"
    )


def wait_until(check, what, seconds=180):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if check():
            return
        time.sleep(POLL)
    raise Fail(f"timed out waiting for {what}; run again later (each step is decided from chain state)")


def broadcast(api, keys, wallet, state, messages):
    boc, msg_hash, valid_until = external(keys, wallet, state.seqno if state.active else 0, state.active, messages)
    api.send(boc)
    return wait_final(api, wallet.address, msg_hash, valid_until)


def sweep_ton(api, keys, row, dest, dest_bounce, min_ton):
    state = api.state(row.wallet.address)
    if state.balance < min_ton:
        return "skipped: balance gone"
    # whole balance to the destination and delete the empty account; a later payment re-creates it
    broadcast(api, keys, row.wallet, state, [(SEND_ALL_DESTROY, internal(dest, 0, dest_bounce))])
    return f"swept {fmt(state.balance, 9)} TON"


def fund(api, keys, fee, target, amount):
    state = api.state(fee.address)
    if state.balance < amount + FUND_FEE:
        raise Fail(f"fee wallet has {fmt(state.balance, 9)} TON, needs {fmt(amount + FUND_FEE, 9)}; top it up")
    # non-bounceable: the deposit wallet is not deployed yet
    broadcast(api, keys, fee, state, [(PAY_FEES_IGNORE_ERRORS, internal(target, amount, False))])


def sweep_usdt(api, keys, row, fee, dest, min_usdt):
    w = row.wallet
    if api.jetton_balance(row.jetton_wallet, w.address) < min_usdt:
        return "skipped: balance gone"
    if (balance := api.state(w.address).balance) < USDT_GAS_NEED:
        fund(api, keys, fee, w.address, USDT_GAS_NEED - balance)
        wait_until(lambda: api.state(w.address).balance >= USDT_GAS_NEED - 1_000_000, f"gas to arrive on {w.address}")
    state = api.state(w.address)
    amount = api.jetton_balance(row.jetton_wallet, w.address)
    if amount < min_usdt:
        return "skipped: balance gone"
    # Actions run in order: the transfer takes JETTON_ATTACH (+ its forward fee, mode 1), then mode 128 sends what is
    # left to the fee wallet and deletes the account. Mode +2 on the transfer: if it cannot be sent, the TON still
    # returns to the fee wallet and the USDT stays put for the next run.
    transfer = internal(row.jetton_wallet, JETTON_ATTACH, True, jetton_body(amount, dest, fee.address))
    broadcast(api, keys, w, state, [(PAY_FEES_IGNORE_ERRORS, transfer), (SEND_ALL_DESTROY, internal(fee.address, 0, False))])
    wait_until(lambda: api.jetton_balance(row.jetton_wallet, w.address) == 0, f"USDT to leave {w.address}")
    return f"swept {fmt(amount, 6)} USDT"


def execute(api, keys, rows, fee, dest, dest_bounce, min_ton, min_usdt):
    swept = failed = 0
    skipped = sum(r.action != "sweep" for r in rows)
    for row in (r for r in rows if r.action == "sweep"):
        label = f"{row.wallet.kind} {row.wallet.index} {row.wallet.address}"
        try:
            if row.sweeps_usdt:
                result = sweep_usdt(api, keys, row, fee, dest, min_usdt)
            else:
                result = sweep_ton(api, keys, row, dest, dest_bounce, min_ton)
        except Fail as exc:
            failed += 1
            say(f"FAILED {label}: {exc}")
            continue
        say(f"{label}: {result}")
        if result.startswith("swept"):
            swept += 1
        else:
            skipped += 1
    return swept, skipped, failed


def cmd_info(args, api):
    keys = read_seed()
    _, fee = derive_all(keys.public, 0, 0)
    testnet = args.network == "testnet"
    say(f"Network: {args.network}")
    say(f"Public key (enter in Bitcart as the wallet key): {keys.public.hex()}")
    say(f"Fee wallet: {show(fee.address, testnet)}  balance {fmt(api.state(fee.address).balance, 9)} TON")
    return 0


def cmd_sweep(args, api):
    testnet = args.network == "testnet"
    if testnet and args.usdt_count:
        raise Fail("USDT exists on mainnet only: use --usdt-count 0 on testnet")
    min_ton, min_usdt = units(args.min_ton, 9, "--min-ton"), units(args.min_usdt, 6, "--min-usdt")
    keys = read_seed()
    wallets, fee = derive_all(keys.public, args.ton_count, args.usdt_count)
    own = {w.address for w in [*wallets, fee]}
    dest = load_destination(Path(args.config or f"ton-sweep-{args.network}.json"), own, testnet)
    rows, fee_state = plan(api, wallets, fee, min_ton, min_usdt)
    print_plan(rows, fee, fee_state, testnet)
    if not args.send:
        say("Dry run: nothing sent. Add --send to sweep.")
        return 0
    if not any(r.action == "sweep" for r in rows):
        say("Nothing to sweep.")
        return 0
    # bounceable is safe only to a deployed wallet; otherwise the value would come back
    dest_bounce = api.state(dest).active
    say(
        f"Destination: {tonaddr.friendly(dest, bounceable=dest_bounce, testnet=testnet)} "
        f"({'deployed, bounceable' if dest_bounce else 'NOT deployed, non-bounceable'})"
    )
    if input("Send everything marked sweep to this destination? Type yes: ").strip() != "yes":
        say("Not confirmed: nothing sent.")
        return 1
    swept, skipped, failed = execute(api, keys, rows, fee, dest, dest_bounce, min_ton, min_usdt)
    say(f"Summary: swept {swept}, skipped {skipped}, failed {failed}")
    return 1 if failed else 0


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("info", "sweep"):
        p = sub.add_parser(name)
        p.add_argument("--network", choices=SERVERS, default="mainnet")
        p.add_argument("--server", help="toncenter v3 base URL; an API key goes in ?api_key=")
    p.add_argument("--ton-count", type=int, required=True, help="TON addresses 0..N-1 (daemon getinfo counter)")
    p.add_argument("--usdt-count", type=int, required=True, help="USDT addresses 0..M-1 (daemon getinfo counter)")
    p.add_argument("--send", action="store_true", help="execute; without it the run changes nothing")
    p.add_argument("--min-ton", default=DEFAULT_MIN_TON)
    p.add_argument("--min-usdt", default=DEFAULT_MIN_USDT)
    p.add_argument("--config", help="config file (default ton-sweep-<network>.json in the current directory)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        api = Api(args.server, args.network)
        return (cmd_info if args.command == "info" else cmd_sweep)(args, api)
    except Fail as exc:
        say(f"error: {exc}")
    except KeyboardInterrupt:
        say("stopped")
    except Exception as exc:  # one line and no traceback, so no frame with key material is shown
        say(f"error: unexpected {type(exc).__name__}: {str(exc)[:200]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
