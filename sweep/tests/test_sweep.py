import asyncio
import json
import random
from types import SimpleNamespace

import pytest
from conftest import decode_external, needs_tonaddr, raw
from pytoniq_core.crypto.signature import verify_sign
from ton_core import JettonTransferBody, NetworkGlobalID, WalletMessage, WalletV3Config, WalletV3Params
from tonutils.contracts import WalletV3R2

import sweep

DEST = "0:" + "AB" * 32
SUBWALLETS = [1_000_000_000, 1_000_000_007, 2_000_000_000, 2_000_000_123, 999_999_999, 698_983_191]


def wallet(keys, kind="ton", sub=1_000_000_000):
    return sweep.derive(keys.public, kind, sub % 1000, sub)


@needs_tonaddr
@pytest.mark.parametrize("subwallet", SUBWALLETS)
def test_address_matches_tonutils_v3r2(seed_words, keys, subwallet):
    lib, public, _, _ = WalletV3R2.from_mnemonic(None, seed_words, config=WalletV3Config(subwallet_id=subwallet))
    assert public.as_bytes == keys.public
    assert wallet(keys, sub=subwallet).address == raw(lib.address)


@needs_tonaddr
def test_live_mainnet_v3r2_wallet():
    # public key and wallet_id read from toncenter for this active wallet_v3r2 account
    public = bytes.fromhex("6e993592be71829cdbd9bb0a0a2ffc9be8e9f9539740b2ec37e167185a059c25")
    w = sweep.derive(public, "ton", 0, 1475789938)
    assert w.address == "0:0CB0029568C0A5F383DDC37BAB8B52A71950CDDAAACEFF5902B9D6FCF3EEC69F"


@needs_tonaddr
def test_cross_check_passes_for_many_indexes(keys):
    wallets, fee = sweep.derive_all(keys.public, 200, 200)
    assert len({w.address for w in [*wallets, fee]}) == 401


@needs_tonaddr
def test_cross_check_stops_on_mismatch(keys, monkeypatch):
    real = sweep.tonaddr.wallet_address
    monkeypatch.setattr(sweep.tonaddr, "wallet_address", lambda pub, sub: real(pub, sub + (sub == 1_000_000_002)))
    with pytest.raises(sweep.Fail, match="address mismatch for subwallet 1000000002"):
        sweep.derive_all(keys.public, 5, 0)


@needs_tonaddr
@pytest.mark.parametrize("deployed", [False, True])
def test_external_message(keys, deployed):
    w = wallet(keys, sub=1_000_000_042)
    messages = [(160, sweep.internal(DEST, 0, True)), (3, sweep.internal(raw_fee(keys), 5, False))]
    boc, _, valid_until = sweep.external(keys, w, 7 if deployed else 0, deployed, messages, now=1_800_000_000)
    msg, signature, unsigned, subwallet, until, seqno, out = decode_external(boc)
    assert raw(msg.info.dest) == w.address
    assert (msg.init is not None) == (not deployed)
    if not deployed:
        assert msg.init.serialize().hash == w.state_init.serialize().hash
    assert verify_sign(keys.public, unsigned.hash, signature)
    assert not verify_sign(bytes(32 * [1]), unsigned.hash, signature)
    assert (subwallet, until, seqno) == (1_000_000_042, 1_800_000_300, 7 if deployed else 0) and valid_until == until
    assert [(mode, raw(m.info.dest), m.info.bounce, m.info.value.grams) for mode, m in out] == [
        (160, DEST, True, 0),
        (3, raw_fee(keys), False, 5),
    ]


@needs_tonaddr
def test_signed_body_equals_tonutils(seed_words, keys):
    client = SimpleNamespace(network=NetworkGlobalID.MAINNET)
    lib, *_ = WalletV3R2.from_mnemonic(client, seed_words, config=WalletV3Config(subwallet_id=2_000_000_005))
    transfer = sweep.internal(DEST, 7, True, sweep.jetton_body(5, DEST, DEST))
    messages = [(3, transfer), (160, sweep.internal(raw_fee(keys), 0, False))]
    params = WalletV3Params(seqno=4, valid_until=1_800_000_300)
    expected = asyncio.run(lib._build_signed_msg_cell([WalletMessage(m, i) for m, i in messages], params))
    boc, *_ = sweep.external(keys, wallet(keys, "usdt", 2_000_000_005), 4, True, messages, now=1_800_000_000)
    assert decode_external(boc)[0].body.hash == expected.hash


def raw_fee(keys):
    return sweep.derive_all(keys.public, 0, 0)[1].address


@needs_tonaddr
def test_too_many_messages(keys):
    with pytest.raises(ValueError, match="1 to 4"):
        sweep.external(keys, wallet(keys), 0, False, [(3, sweep.internal(DEST, 1, False))] * 5)


@needs_tonaddr
def test_jetton_transfer_body(keys):
    fee = raw_fee(keys)
    cell = sweep.jetton_body(123_456_789, DEST, fee)
    assert cell.begin_parse().load_uint(32) == 0x0F8A7EA5
    body = JettonTransferBody.deserialize(cell.begin_parse())
    assert body.jetton_amount == 123_456_789
    assert raw(body.destination) == DEST and raw(body.response_address) == fee
    assert body.forward_amount == 1 and body.custom_payload is None


def setup_run(monkeypatch, tmp_path, chain, seed_words, answers=("yes",), config=True):
    if config:
        (tmp_path / "c.json").write_text(json.dumps({"destination": sweep.tonaddr.friendly(DEST, True)}))
    monkeypatch.setattr(sweep, "Api", lambda server, network: chain)
    monkeypatch.setattr(sweep, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(sweep.getpass, "getpass", lambda prompt: " ".join(seed_words))
    replies = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt: next(replies))
    return ["--config", str(tmp_path / "c.json")]


def funded(chain, keys, ton=(), usdt=(), fee_balance=0):
    wallets, fee = sweep.derive_all(keys.public, len(ton), len(usdt))
    for w, amount in zip(wallets, [*ton, *[t for t, _ in usdt]], strict=True):
        if amount:
            chain.add(w.address, amount)
    for w, (_, jettons) in zip(wallets[len(ton) :], usdt, strict=True):
        chain.add_usdt(w.address, jettons)
    if fee_balance:
        chain.add(fee.address, fee_balance)
    return wallets, fee


@needs_tonaddr
def test_dry_run_plans_and_sends_nothing(monkeypatch, tmp_path, chain, seed_words, keys, capsys):
    funded(chain, keys, ton=[2 * 10**9, 5_000_000, 0], usdt=[(0, 50 * 10**6), (0, 50_000)], fee_balance=10**8)
    args = setup_run(monkeypatch, tmp_path, chain, seed_words)
    assert sweep.main(["sweep", "--ton-count", "3", "--usdt-count", "2", *args]) == 0
    out = capsys.readouterr().out
    assert ("POST", "/message") not in chain.calls and not chain.sent
    lines = [line for line in out.splitlines() if line.strip()[:1].isdigit()]
    expected = ["sweep", "skip: below limit", "skip: empty", "sweep", "skip: below limit"]
    assert len(lines) == len(expected) and all(line.endswith(action) for line, action in zip(lines, expected, strict=True))
    assert "Dry run: nothing sent" in out
    assert "enough" in out and "NOT enough" not in out


@needs_tonaddr
def test_fee_wallet_shortfall_reported(monkeypatch, tmp_path, chain, seed_words, keys, capsys):
    funded(chain, keys, usdt=[(0, 5 * 10**6)], fee_balance=10_000_000)
    args = setup_run(monkeypatch, tmp_path, chain, seed_words)
    assert sweep.main(["sweep", "--ton-count", "0", "--usdt-count", "1", *args]) == 0
    assert "NOT enough: top it up" in capsys.readouterr().out


@needs_tonaddr
def test_send_sweeps_ton_and_usdt(monkeypatch, tmp_path, chain, seed_words, keys, capsys):
    wallets, fee = funded(chain, keys, ton=[2 * 10**9], usdt=[(0, 50 * 10**6)], fee_balance=10**9)
    chain.add(DEST, 1, "active")
    args = setup_run(monkeypatch, tmp_path, chain, seed_words)
    assert sweep.main(["sweep", "--ton-count", "1", "--usdt-count", "1", "--send", *args]) == 0
    assert "Summary: swept 2, skipped 0, failed 0" in capsys.readouterr().out
    senders = [s for s, _ in chain.sent]
    assert senders == [wallets[0].address, fee.address, wallets[1].address]
    ton_out = chain.sent[0][1][6]
    assert [(m, raw(i.info.dest), i.info.bounce) for m, i in ton_out] == [(160, DEST, True)]
    fund_out = chain.sent[1][1][6]
    assert [(m, raw(i.info.dest), i.info.bounce, i.info.value.grams) for m, i in fund_out] == [
        (3, wallets[1].address, False, sweep.USDT_GAS_NEED)
    ]
    msg, *_, usdt_out = chain.sent[2][1]
    assert msg.init is not None
    jetton_wallet = chain.jettons[wallets[1].address][0]
    assert [(m, raw(i.info.dest), i.info.value.grams) for m, i in usdt_out] == [
        (3, jetton_wallet, sweep.JETTON_ATTACH),
        (160, fee.address, 0),
    ]
    assert chain.jettons[wallets[1].address][1] == 0
    assert chain.accounts[wallets[0].address].status == "nonexist"
    assert chain.accounts[DEST].balance > 2 * 10**9 - 10**7


@needs_tonaddr
def test_rerun_after_stop_does_remaining_steps_only(monkeypatch, tmp_path, chain, seed_words, keys, capsys):
    wallets, fee = funded(chain, keys, usdt=[(0, 50 * 10**6)], fee_balance=10**9)
    chain.fail_send_at = 1  # stop after the gas funding reached the chain
    args = setup_run(monkeypatch, tmp_path, chain, seed_words, answers=("yes", "yes"))
    assert sweep.main(["sweep", "--ton-count", "0", "--usdt-count", "1", "--send", *args]) == 1
    assert [s for s, _ in chain.sent] == [fee.address]
    chain.fail_send_at = None
    assert sweep.main(["sweep", "--ton-count", "0", "--usdt-count", "1", "--send", *args]) == 0
    assert [s for s, _ in chain.sent] == [fee.address, wallets[0].address]
    assert chain.jettons[wallets[0].address][1] == 0
    assert sweep.main(["sweep", "--ton-count", "0", "--usdt-count", "1", "--send", *args]) == 0
    assert len(chain.sent) == 2
    assert "Nothing to sweep" in capsys.readouterr().out


@needs_tonaddr
def test_not_confirmed_sends_nothing(monkeypatch, tmp_path, chain, seed_words, keys):
    funded(chain, keys, ton=[2 * 10**9])
    args = setup_run(monkeypatch, tmp_path, chain, seed_words, answers=("no",))
    assert sweep.main(["sweep", "--ton-count", "1", "--usdt-count", "0", "--send", *args]) == 1
    assert not chain.sent


@needs_tonaddr
def test_first_run_saves_destination(monkeypatch, tmp_path, chain, seed_words):
    args = setup_run(monkeypatch, tmp_path, chain, seed_words, answers=(DEST, "yes"), config=False)
    assert sweep.main(["sweep", "--ton-count", "1", "--usdt-count", "0", *args]) == 0
    saved = json.loads((tmp_path / "c.json").read_text())
    assert sweep.tonaddr.parse_address(saved["destination"]) == DEST


@needs_tonaddr
@pytest.mark.parametrize("own", ["ton", "fee"])
def test_own_address_refused_as_destination(monkeypatch, tmp_path, chain, seed_words, keys, capsys, own):
    wallets, fee = sweep.derive_all(keys.public, 3, 0)
    target = wallets[2].address if own == "ton" else fee.address
    args = setup_run(monkeypatch, tmp_path, chain, seed_words, answers=(target, "yes"), config=False)
    assert sweep.main(["sweep", "--ton-count", "3", "--usdt-count", "0", *args]) == 1
    assert "one of this seed's own" in capsys.readouterr().out
    assert not (tmp_path / "c.json").exists()


@needs_tonaddr
def test_info(monkeypatch, tmp_path, chain, seed_words, keys, capsys):
    setup_run(monkeypatch, tmp_path, chain, seed_words)
    assert sweep.main(["info"]) == 0
    out = capsys.readouterr().out
    assert keys.public.hex() in out
    assert sweep.tonaddr.friendly(raw_fee(keys)) in out


def leaks(text, seed_words, keys):
    phrase = " ".join(seed_words)
    secret = keys._secret
    pieces = [phrase, secret.hex(), secret[:32].hex(), repr(secret)]
    pieces += [" ".join(seed_words[i : i + 3]) for i in range(22)]
    return [p for p in pieces if p in text]


@needs_tonaddr
@pytest.mark.parametrize(
    "failure, shown", [("chain error", "HTTP 500"), ("unexpected error", "ZeroDivisionError"), ("bad input", "word number")]
)
def test_seed_never_printed(monkeypatch, tmp_path, chain, seed_words, keys, capsys, failure, shown):
    funded(chain, keys, ton=[2 * 10**9], usdt=[(0, 50 * 10**6)], fee_balance=10**9)
    args = setup_run(monkeypatch, tmp_path, chain, seed_words)
    if failure == "chain error":
        monkeypatch.setattr(chain, "receive", lambda boc: (_ for _ in ()).throw(sweep.Fail("HTTP 500")))
    elif failure == "unexpected error":
        monkeypatch.setattr(chain, "receive", lambda boc: 1 / 0)
    else:
        monkeypatch.setattr(sweep.getpass, "getpass", lambda prompt: " ".join(seed_words[:23] + ["notaword"]))
    assert sweep.main(["sweep", "--ton-count", "1", "--usdt-count", "1", "--send", *args]) == 1
    captured = capsys.readouterr()
    assert shown in captured.out
    assert not leaks(captured.out + captured.err, seed_words, keys)
    assert not leaks(repr(keys) + str(keys), seed_words, keys)


def test_keys_from_seed_match_library(seed_words, keys):
    _, public, private, _ = WalletV3R2.from_mnemonic(None, seed_words)
    assert keys.public == public.as_bytes == private.public_key.as_bytes


@pytest.mark.parametrize(
    "seed, error",
    [("one two", "expected 24 words, got 2"), (" ".join(["abandon"] * 23 + ["zzz"]), "word number")],
)
def test_bad_seed_rejected(monkeypatch, seed, error):
    monkeypatch.setattr(sweep, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(sweep.getpass, "getpass", lambda prompt: seed)
    with pytest.raises(sweep.Fail, match=error):
        sweep.read_seed()


def test_bip39_style_seed_rejected(monkeypatch, seed_words):
    words, rng = list(seed_words), random.Random(1)
    while sweep.mnemonic_is_valid(words):
        rng.shuffle(words)
    monkeypatch.setattr(sweep, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(sweep.getpass, "getpass", lambda prompt: " ".join(words))
    with pytest.raises(sweep.Fail, match="not a valid TON seed"):
        sweep.read_seed()


def test_no_terminal_refused(monkeypatch):
    monkeypatch.setattr(sweep, "stdin_is_tty", lambda: False)
    with pytest.raises(sweep.Fail, match="needs a terminal"):
        sweep.read_seed()
