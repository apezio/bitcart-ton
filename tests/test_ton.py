from decimal import Decimal

import pytest
import ton
import tonaddr
from conftest import (
    PUBKEY,
    SENDER,
    SENDER_OWNER,
    events,
    hex_hash,
    jetton_wallet_of,
    load,
    load_fixture,
    make_daemon,
    native_tx,
    request,
    usdt_tx,
)
from genericprocessor import PR_PAID

USDT_FRIENDLY = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"


def deposit(n, usdt=False):
    return tonaddr.wallet_address(PUBKEY, (tonaddr.USDT_OFFSET if usdt else tonaddr.NATIVE_OFFSET) + n)


# --- keys, wallets ------------------------------------------------------------------


def test_validatekey(daemon):
    assert daemon.validatekey(PUBKEY)
    assert daemon.validatekey(PUBKEY.upper())
    for bad in ("", PUBKEY[:-1], PUBKEY + "0", "zz" * 32, "xpub661MyMwAqRbcF", None):
        assert not daemon.validatekey(bad)


async def test_wallet_address_is_the_fee_wallet(daemon):
    key, wallet = await load(daemon, PUBKEY.upper())
    assert key == PUBKEY
    assert wallet.address == tonaddr.friendly(tonaddr.wallet_address(PUBKEY, tonaddr.FEE_WALLET_ID))
    assert daemon.getaddress(wallet=key) == wallet.address


@pytest.mark.parametrize("contract", [tonaddr.USDT_MASTER, USDT_FRIENDLY, tonaddr.USDT_MASTER.lower()])
async def test_usdt_wallet_contract_normalised(daemon, contract):
    key, wallet = await load(daemon, PUBKEY, contract)
    assert key == f"{PUBKEY}_{tonaddr.USDT_MASTER}"
    assert wallet.contract == tonaddr.USDT_MASTER
    assert wallet.symbol == "USDT" and wallet.divisibility == 6
    assert daemon.normalizeaddress(contract) == tonaddr.USDT_MASTER
    assert daemon.validatecontract(contract)


async def test_other_contract_refused(daemon):
    other = tonaddr.friendly("0:" + "AB" * 32, bounceable=True)
    assert not daemon.validatecontract(other)
    assert not daemon.validatecontract("garbage")
    with pytest.raises(Exception, match="Invalid contract"):
        await daemon.load_wallet(PUBKEY, other)
    with pytest.raises(Exception, match="Invalid contract"):
        await daemon.readcontract(other, "symbol")


async def test_usdt_refused_on_testnet(tmp_path, monkeypatch, rpc):
    d = make_daemon(tmp_path, monkeypatch, rpc, network="testnet")
    assert d.get_tokens() == {}
    assert not d.validatecontract(tonaddr.USDT_MASTER)
    with pytest.raises(Exception, match="Invalid contract"):
        await d.load_wallet(PUBKEY, tonaddr.USDT_MASTER)


async def test_readcontract_and_tokens(daemon, rpc):
    assert daemon.get_tokens() == {"USDT": tonaddr.USDT_MASTER}
    assert await daemon.readcontract(USDT_FRIENDLY, "symbol") == "USDT"
    assert await daemon.readcontract(tonaddr.USDT_MASTER, "decimals") == 6
    owner = "0:" + "11" * 32
    rpc.jetton_balances[jetton_wallet_of(owner)] = 2_500_000
    assert await daemon.readcontract(tonaddr.USDT_MASTER, "balanceOf", owner) == 2_500_000


async def test_balance_is_the_own_address_only(daemon, rpc):
    key, wallet = await load(daemon)
    rpc.balances[tonaddr.parse_address(wallet.address)] = 1_500_000_000
    assert await daemon.getbalance(wallet=key) == {"confirmed": "1.500000000"}
    assert await daemon.listaddresses(balance=True, wallet=key) == [(wallet.address, "1.5")]
    usdt_key, _ = await load(daemon, contract=tonaddr.USDT_MASTER)
    assert await daemon.getbalance(wallet=usdt_key) == {"confirmed": "0.000000"}


def test_unsupported_rpcs(daemon):
    for call in (
        lambda: daemon.make_seed(),
        lambda: daemon.signmessage(),
        lambda: daemon.verifymessage("a", "b", "c"),
        lambda: daemon.get_tx_size({}),
        lambda: daemon._sign_transaction({}, None),
    ):
        with pytest.raises(NotImplementedError, match="not supported"):
            call()


async def test_unsupported_async_rpcs(daemon):
    for call in (
        daemon.payto("x", 1),
        daemon.broadcast("x"),
        daemon.transfer(),
        daemon.add_peer("x"),
        daemon.get_default_fee("x"),
        daemon.rescan_blocks(1),
        daemon.coin.get_block(1),
        daemon.coin.get_block_txes(1),
    ):
        with pytest.raises(NotImplementedError):
            await call


# --- requests -----------------------------------------------------------------------


async def test_request_export_keys(daemon):
    key, wallet = await load(daemon)
    data, req = await request(daemon, key, "1.5")
    assert data["address"] == tonaddr.friendly(deposit(0))
    assert data["address"].startswith("UQ")  # non-bounceable
    assert data["request_id"] == req.id
    assert data["amount_TON"] == "1.500000000"
    assert data["URI"] == f"ton://transfer/{data['address']}?amount=1500000000"
    assert req.index == 0 and req.watch_address == deposit(0)
    assert wallet.request_addresses[deposit(0)] == req.id


async def test_usdt_request_export_keys(daemon):
    key, _ = await load(daemon, contract=tonaddr.USDT_MASTER)
    data, req = await request(daemon, key, "15")
    assert data["address"] == tonaddr.friendly(deposit(0, usdt=True))
    assert data["amount_USDT"] == "15.000000"
    assert data["URI"] == f"ton://transfer/{data['address']}?amount=15000000&jetton={USDT_FRIENDLY}"
    assert req.watch_address == jetton_wallet_of(deposit(0, usdt=True))


async def test_live_jetton_wallet_reply_is_used(daemon, rpc):
    rpc.overrides["runGetMethod"] = load_fixture("get_wallet_address.json")
    owner = "0:852443F8599FE6A5DA34FE43049AC4E0BEB3071BB2BFB56635EA9421287C283A"
    assert await daemon.coin.get_jetton_wallet(owner) == "0:32081D54752B9340A75C777A94AF0483F07329DD296EB4F915BB45E477B93F8A"
    body = rpc.calls[-1][2]
    assert body["stack"] == [{"type": "slice", "value": tonaddr.address_cell_boc(owner)}]


async def test_testnet_addresses_carry_the_testnet_flag(tmp_path, monkeypatch, rpc):
    d = make_daemon(tmp_path, monkeypatch, rpc, network="testnet")
    key, wallet = await load(d)
    data, _ = await request(d, key)
    assert data["address"].startswith("0Q") and wallet.address.startswith("0Q")
    assert d.getservers() == ["https://toncenter.com"]


async def test_zero_amount_uri_has_no_amount(daemon):
    key, _ = await load(daemon)
    data, _ = await request(daemon, key, "0")
    assert data["URI"] == f"ton://transfer/{data['address']}"


async def test_modifypaymenturl(daemon):
    key, _ = await load(daemon)
    data, _ = await request(daemon, key, "1")
    assert (
        await daemon.modifypaymenturl(data["URI"], Decimal("0.95"), 8) == f"ton://transfer/{data['address']}?amount=950000000"
    )
    usdt_key, _ = await load(daemon, contract=tonaddr.USDT_MASTER)
    usdt, _ = await request(daemon, usdt_key, "10")
    assert (
        await daemon.modifypaymenturl(usdt["URI"], Decimal("9.5"), 6)
        == f"ton://transfer/{usdt['address']}?amount=9500000&jetton={USDT_FRIENDLY}"
    )


async def test_indexes_are_unique_and_increasing(daemon):
    key, wallet = await load(daemon)
    reqs = [(await request(daemon, key))[1] for _ in range(5)]
    assert [r.index for r in reqs] == [0, 1, 2, 3, 4]
    assert len({r.address for r in reqs}) == 5
    assert wallet.next_index == 5
    info = await daemon.getinfo()
    assert info["next_index"] == {key: 5}


async def test_index_survives_restart_and_is_never_reused(tmp_path, monkeypatch, rpc):
    d = make_daemon(tmp_path, monkeypatch, rpc)
    key, wallet = await load(d)
    _, first = await request(d, key)
    d.rmrequest(first.id, wallet=key)  # a removed request does not give its index back
    _, second = await request(d, key)
    assert second.index == 1
    d2 = make_daemon(tmp_path, monkeypatch, rpc)
    _, wallet2 = await load(d2)
    assert wallet2.next_index == 2
    _, third = await request(d2, key)
    assert third.index == 2


async def test_usdt_request_fails_cleanly_when_get_method_fails(daemon, rpc):
    key, wallet = await load(daemon, contract=tonaddr.USDT_MASTER)
    rpc.failures["runGetMethod"] = [ton.TonTransientError("HTTP 502")]
    with pytest.raises(ton.TonTransientError):
        await request(daemon, key)
    assert wallet.receive_requests == {} and wallet.request_addresses == {}
    assert wallet.next_index == 1  # consumed: the address may have been derived elsewhere already
    rpc.overrides["runGetMethod"] = {"exit_code": 11, "stack": []}
    with pytest.raises(ton.TonRPCError):
        await request(daemon, key)
    del rpc.overrides["runGetMethod"]
    _, req = await request(daemon, key)
    assert req.index == 2 and list(wallet.receive_requests) == [req.id]


async def test_index_limit(daemon):
    key, wallet = await load(daemon)
    wallet.next_index = ton.MAX_INDEX
    with pytest.raises(Exception, match="used"):
        await request(daemon, key)


async def test_setrequestaddress_is_refused(daemon):
    key, _ = await load(daemon)
    data, req = await request(daemon, key)
    assert daemon.setrequestaddress(req.id, SENDER, wallet=key) is False
    assert req.payment_address is None


# --- credits ------------------------------------------------------------------------


async def test_native_credit(daemon, rpc):
    key, wallet = await load(daemon)
    _, req = await request(daemon, key, "2")
    rpc.add(native_tx(deposit(0), 2_000_000_000, 500))
    await daemon.poll_wallet(key)
    assert req.status == PR_PAID
    assert req.tx_hashes == [hex_hash(500)]
    assert deposit(0) not in wallet.request_addresses
    [event] = events(daemon, key, "new_payment")
    assert event["address"] == req.id and event["contract"] is None and event["sent_amount"] == "2.000000000"


async def test_usdt_credit_event_carries_normalised_contract(daemon, rpc):
    key, wallet = await load(daemon, contract=USDT_FRIENDLY)
    _, req = await request(daemon, key, "15")
    rpc.add(usdt_tx(req.watch_address, 15_000_000, 700))
    await daemon.poll_wallet(key)
    assert req.status == PR_PAID
    [event] = events(daemon, key, "new_payment")
    assert event["address"] == req.id
    assert event["contract"] == daemon.normalizeaddress(USDT_FRIENDLY) == tonaddr.USDT_MASTER
    assert event["sent_amount"] == "15.000000"


async def test_live_native_fixture_credits(daemon, rpc):
    tx = load_fixture("tx_native.json")["transactions"][0]
    assert ton.native_credit(tx) == (2168300000, tx["in_msg"]["source"])
    key, _ = await load(daemon)
    _, req = await request(daemon, key, "2.1683")
    tx["account"], tx["now"] = deposit(0), req.time
    rpc.add(tx)
    await daemon.poll_wallet(key)
    assert req.status == PR_PAID
    assert req.tx_hashes == ["ddb60faf91d8a5ffcc8ac95b997700c152e52c11744d4680fa1a0b810bfcf25c"]


def test_live_payment_to_undeployed_address_credits():
    tx = load_fixture("tx_native_uninit.json")["transactions"][0]
    assert tx["description"]["aborted"] is True and tx["in_msg"]["bounce"] is False
    assert ton.native_credit(tx)[0] == int(tx["in_msg"]["value"])


def test_live_bounceable_payment_to_undeployed_address_does_not_credit():
    tx = load_fixture("tx_native_uninit_bounce.json")["transactions"][0]
    assert tx["description"]["aborted"] is True and tx["in_msg"]["bounce"] is True
    assert ton.native_credit(tx) is None


def test_live_usdt_fixture_credits():
    tx = load_fixture("tx_usdt.json")["transactions"][0]
    assert ton.jetton_credit(tx) == (22_500_000, SENDER_OWNER)


def test_usdt_credit_without_from_address():
    tx = load_fixture("tx_usdt.json")["transactions"][0]
    tx["in_msg"]["message_content"]["decoded"]["from"] = {"@type": "addr_none"}
    assert ton.jetton_credit(tx) == (22_500_000, None)


async def test_explorer_hash_and_confirmations(daemon, rpc):
    key, _ = await load(daemon)
    data, req = await request(daemon, key)
    rpc.add(native_tx(deposit(0), 1_000_000_000, 500))
    await daemon.poll_wallet(key)
    exported = await daemon.getrequest(req.id, wallet=key)
    assert exported["confirmations"] == ton.FINALIZED_CONFIRMATIONS >= 10
    assert len(req.tx_hashes[0]) == 64 and req.tx_hashes[0] == req.tx_hashes[0].lower()
    # an unknown hash is looked up: finalized gives the constant, unknown or pending gives 0
    rpc.add(native_tx("0:" + "CD" * 32, 1, 900))
    assert await daemon.coin.get_confirmations(hex_hash(900)) == ton.FINALIZED_CONFIRMATIONS
    assert await daemon.coin.get_confirmations(hex_hash(901)) == 0
    rpc.add(native_tx("0:" + "CD" * 32, 1, 902, finality="confirmed"))
    assert await daemon.coin.get_confirmations(hex_hash(902)) == 0


async def test_new_block_emitted_when_seqno_moves(daemon, rpc):
    key, _ = await load(daemon)
    await daemon.poll_once()
    assert daemon.synchronized
    assert events(daemon, key, "new_block") == [{"event": "new_block", "height": 1000}]
    await daemon.poll_once()
    assert len(events(daemon, key, "new_block")) == 1
    rpc.seqno = 1003
    await daemon.poll_once()
    assert events(daemon, key, "new_block")[-1] == {"event": "new_block", "height": 1003}


async def test_no_poll_without_open_requests(daemon, rpc):
    key, _ = await load(daemon)
    await daemon.poll_wallet(key)
    assert rpc.polls() == []


async def test_every_poll_starts_at_oldest_open_request_and_credits_once(daemon, rpc):
    key, wallet = await load(daemon)
    _, req = await request(daemon, key, "5")
    rpc.add(native_tx(deposit(0), 1_000_000_000, 500))
    for _ in range(3):
        await daemon.poll_wallet(key)
        assert ("start_utime", req.time - ton.UTIME_MARGIN) in rpc.polls()[-1][1]
    assert req.sent_amount == 1 and len(req.tx_hashes) == 1
    assert len(events(daemon, key, "new_payment")) == 1


async def test_watch_addresses_batched_and_split(daemon, rpc, monkeypatch):
    monkeypatch.setattr(ton.TONDaemon, "ACCOUNTS_PER_REQUEST", 2)
    key, _ = await load(daemon)
    reqs = [(await request(daemon, key))[1] for _ in range(5)]
    for i in range(len(reqs)):
        rpc.add(native_tx(deposit(i), 1_000_000_000, 500 + i))
    await daemon.poll_wallet(key)
    assert len(rpc.polls()) == 3
    assert sorted(a for c in rpc.polls() for k, a in c[1] if k == "account") == sorted(deposit(i) for i in range(5))
    assert all(r.status == PR_PAID for r in reqs)


async def test_full_pages_are_followed(daemon, rpc, monkeypatch):
    monkeypatch.setattr(ton.TONDaemon, "PAGE_SIZE", 3)
    key, wallet = await load(daemon)
    _, req = await request(daemon, key, "10")
    for n, lt in enumerate([501, 502, 503, 503, 504, 505, 506]):
        rpc.add(native_tx(deposit(0), 1_000_000_000, lt, n=n))
    await daemon.poll_wallet(key)
    assert req.sent_amount == Decimal(7) and len(req.tx_hashes) == 7


async def test_page_cap_reads_what_it_can(daemon, rpc, monkeypatch):
    monkeypatch.setattr(ton.TONDaemon, "PAGE_SIZE", 2)
    monkeypatch.setattr(ton.TONDaemon, "MAX_PAGES", 1)
    key, wallet = await load(daemon)
    _, req = await request(daemon, key, "10")
    for n in range(3):
        rpc.add(native_tx(deposit(0), 1_000_000_000, 501 + n, n=n))
    await daemon.poll_wallet(key)
    assert req.sent_amount == 2
