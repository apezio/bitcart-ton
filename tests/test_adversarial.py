from decimal import Decimal

import pytest
import ton
import tonaddr
from aiohttp import web
from conftest import PUBKEY, PUBKEY2, SERVER, events, hex_hash, load, make_daemon, native_tx, request, usdt_tx
from genericprocessor import PR_EXPIRED, PR_PAID, PR_UNPAID

TON = 1_000_000_000


def deposit(n, usdt=False, pubkey=PUBKEY):
    return tonaddr.wallet_address(pubkey, (tonaddr.USDT_OFFSET if usdt else tonaddr.NATIVE_OFFSET) + n)


async def native_request(daemon, amount="1"):
    key, wallet = await load(daemon)
    _, req = await request(daemon, key, amount)
    return key, wallet, req


async def usdt_request(daemon, amount="15"):
    key, wallet = await load(daemon, contract=tonaddr.USDT_MASTER)
    _, req = await request(daemon, key, amount)
    return key, wallet, req


async def test_not_finalized_is_not_counted(daemon, rpc):
    key, wallet, req = await native_request(daemon, "2")
    rpc.add(native_tx(deposit(0), TON, 500), native_tx(deposit(0), TON, 501, finality="confirmed"))
    await daemon.poll_wallet(key)
    assert req.sent_amount == 1 and req.status == PR_UNPAID
    rpc.txs[1]["finality"] = "finalized"
    await daemon.poll_wallet(key)
    assert req.sent_amount == 2 and req.status == PR_PAID and len(req.tx_hashes) == 2  # the pending one is not counted


async def test_emulated_never_credits(daemon, rpc):
    key, wallet, req = await native_request(daemon)
    rpc.add(native_tx(deposit(0), TON, 500, emulated=True))
    await daemon.poll_wallet(key)
    assert req.status == PR_UNPAID and req.sent_amount == 0


async def test_bounced_message_never_credits(daemon, rpc):
    key, _, req = await native_request(daemon)
    rpc.add(native_tx(deposit(0), TON, 500, bounced=True))
    await daemon.poll_wallet(key)
    assert req.sent_amount == 0


async def test_aborted_with_bounce_flag_never_credits(daemon, rpc):
    key, _, req = await native_request(daemon)
    rpc.add(native_tx(deposit(0), TON, 500, aborted=True, bounce=True))
    await daemon.poll_wallet(key)
    assert req.sent_amount == 0


async def test_aborted_without_bounce_flag_credits(daemon, rpc):
    # the normal case for a deposit address: not deployed, non-bounceable transfer, value stays
    key, _, req = await native_request(daemon)
    rpc.add(native_tx(deposit(0), TON, 500, aborted=True, bounce=False))
    await daemon.poll_wallet(key)
    assert req.status == PR_PAID


async def test_external_message_never_credits(daemon, rpc):
    key, _, req = await native_request(daemon)
    rpc.add(native_tx(deposit(0), TON, 500, source=None))
    await daemon.poll_wallet(key)
    assert req.sent_amount == 0


async def test_zero_value_never_credits(daemon, rpc):
    key, _, req = await native_request(daemon, "0")
    rpc.add(native_tx(deposit(0), 0, 500))
    await daemon.poll_wallet(key)
    assert req.status == PR_UNPAID and req.tx_hashes == []


async def test_jetton_notify_to_owner_never_credits(daemon, rpc):
    key, wallet, req = await usdt_request(daemon)
    notify = usdt_tx(deposit(0, usdt=True), 15_000_000, 500, opcode="0x7362d09c")
    notify["in_msg"]["message_content"]["decoded"]["@type"] = "jetton_notify"
    rpc.overrides["transactions"] = {"transactions": [notify]}  # even if the provider serves it
    await daemon.poll_wallet(key)
    assert req.sent_amount == 0
    assert deposit(0, usdt=True) not in wallet.request_addresses
    assert ton.jetton_credit(notify) is None


@pytest.mark.parametrize("fault", [{"aborted": True}, {"exit_code": 37}, {"success": False}])
async def test_failed_internal_transfer_never_credits(daemon, rpc, fault):
    key, _, req = await usdt_request(daemon)
    rpc.add(usdt_tx(req.watch_address, 15_000_000, 500, **fault))
    await daemon.poll_wallet(key)
    assert req.sent_amount == 0


async def test_wrong_opcode_on_jetton_wallet_never_credits(daemon, rpc):
    key, _, req = await usdt_request(daemon)
    rpc.add(usdt_tx(req.watch_address, 15_000_000, 500, opcode="0x0f8a7ea5"))
    await daemon.poll_wallet(key)
    assert req.sent_amount == 0


async def test_undecoded_internal_transfer_never_credits(daemon, rpc):
    key, wallet, req = await usdt_request(daemon)
    tx = usdt_tx(req.watch_address, 15_000_000, 500)
    tx["in_msg"]["message_content"]["decoded"] = {"@type": "unknown"}
    rpc.add(tx)
    await daemon.poll_wallet(key)
    assert req.sent_amount == 0


async def test_transaction_on_another_wallets_address_never_credits(daemon, rpc):
    key, wallet, req = await native_request(daemon)
    key2, wallet2 = await load(daemon, PUBKEY2)
    _, req2 = await request(daemon, key2)
    foreign = native_tx(deposit(0, pubkey=PUBKEY2), TON, 500)
    rpc.overrides["transactions"] = {"transactions": [foreign]}  # served to the first wallet's poll
    await daemon.poll_wallet(key)
    assert req.sent_amount == 0
    assert req2.sent_amount == 0  # only its own poll credits it
    del rpc.overrides["transactions"]
    rpc.add(foreign)
    await daemon.poll_wallet(key2)
    assert req2.status == PR_PAID and req.status == PR_UNPAID


async def test_same_transaction_twice_credits_once(daemon, rpc):
    key, _, req = await native_request(daemon, "2")
    tx = native_tx(deposit(0), TON, 500)
    rpc.add(tx, dict(tx))
    await daemon.poll_wallet(key)
    await daemon.poll_wallet(key)
    assert req.sent_amount == 1 and req.tx_hashes == [hex_hash(500)]


async def test_underpaid_then_topped_up(daemon, rpc):
    key, _, req = await native_request(daemon, "1")
    rpc.add(native_tx(deposit(0), 600_000_000, 500))
    await daemon.poll_wallet(key)
    assert req.status == PR_UNPAID and req.sent_amount == Decimal("0.6")
    rpc.add(native_tx(deposit(0), 399_999_999, 501))
    await daemon.poll_wallet(key)
    assert req.status == PR_UNPAID
    rpc.add(native_tx(deposit(0), 1, 502))
    await daemon.poll_wallet(key)
    assert req.status == PR_PAID and req.sent_amount == 1
    assert [e["status"] for e in events(daemon, key, "new_payment")] == [PR_UNPAID, PR_UNPAID, PR_PAID]


async def test_payment_after_expiry_is_not_credited(daemon, rpc):
    key, wallet, req = await native_request(daemon)
    wallet.set_request_status(req.id, PR_EXPIRED)
    assert deposit(0) not in wallet.request_addresses
    rpc.add(native_tx(deposit(0), TON, 500))
    await daemon.poll_once()
    assert req.status == PR_EXPIRED and req.sent_amount == 0
    assert rpc.polls() == []


async def test_second_payment_after_paid_is_not_credited(daemon, rpc):
    key, wallet, req = await native_request(daemon)
    rpc.add(native_tx(deposit(0), TON, 500))
    await daemon.poll_wallet(key)
    _, later = await request(daemon, key)
    rpc.add(native_tx(deposit(0), TON, 501))
    await daemon.poll_wallet(key)
    assert req.sent_amount == 1 and req.tx_hashes == [hex_hash(500)]
    assert later.sent_amount == 0


async def test_provider_error_mid_batch_then_recovers(daemon, rpc, monkeypatch):
    monkeypatch.setattr(ton.TONDaemon, "ACCOUNTS_PER_REQUEST", 1)
    key, wallet = await load(daemon)
    reqs = [(await request(daemon, key))[1] for _ in range(2)]
    await daemon.poll_wallet(key)
    rpc.add(native_tx(deposit(0), TON, 500), native_tx(deposit(1), TON, 501))
    calls = len(rpc.calls)

    real = rpc.send_request

    async def fail_second(endpoint, params=(), body=None):
        if endpoint == "transactions" and len(rpc.calls) == calls + 1:
            rpc.calls.append((endpoint, list(params), body))
            raise ton.TonTransientError("HTTP 503")
        return await real(endpoint, params, body)

    monkeypatch.setattr(rpc, "send_request", fail_second)
    await daemon.poll_wallet(key)
    assert sum(r.status == PR_PAID for r in reqs) < 2
    monkeypatch.setattr(rpc, "send_request", real)
    await daemon.poll_wallet(key)
    assert all(r.status == PR_PAID and len(r.tx_hashes) == 1 for r in reqs)


@pytest.mark.parametrize(
    "breakage",
    [
        lambda tx: tx["in_msg"].update(value="abc"),
        lambda tx: tx.pop("description"),
        lambda tx: tx["in_msg"].update(bounced=None),
        lambda tx: tx.update(hash="!!"),
        lambda tx: tx.pop("emulated"),
    ],
)
async def test_malformed_transaction_is_retried_and_blocks_nothing(daemon, rpc, breakage):
    key, wallet, req = await native_request(daemon, "3")
    rpc.add(native_tx(deposit(0), TON, 500), native_tx(deposit(0), TON, 501), native_tx(deposit(0), TON, 502))
    breakage(rpc.txs[1])
    await daemon.poll_wallet(key)
    await daemon.poll_wallet(key)
    assert req.sent_amount == 2 and req.status == PR_UNPAID
    rpc.txs[1] = native_tx(deposit(0), TON, 501)
    await daemon.poll_wallet(key)
    assert req.status == PR_PAID and len(req.tx_hashes) == 3


@pytest.mark.parametrize("reply", [{}, {"transactions": None}, {"transactions": "x"}, {"error": "bad"}])
async def test_malformed_reply_credits_nothing(daemon, rpc, reply):
    key, wallet, req = await native_request(daemon)
    rpc.add(native_tx(deposit(0), TON, 500))
    rpc.overrides["transactions"] = reply
    await daemon.poll_wallet(key)
    assert req.sent_amount == 0
    del rpc.overrides["transactions"]
    await daemon.poll_wallet(key)
    assert req.status == PR_PAID


async def test_restart_with_open_requests(tmp_path, monkeypatch, rpc):
    d = make_daemon(tmp_path, monkeypatch, rpc)
    key, wallet = await load(d)
    _, paid = await request(d, key)
    _, open_req = await request(d, key)
    rpc.add(native_tx(deposit(0), TON, 500))
    await d.poll_wallet(key)
    assert paid.status == PR_PAID
    d2 = make_daemon(tmp_path, monkeypatch, rpc)
    key2, wallet2 = await load(d2)
    assert key2 == key and wallet2 is not wallet
    assert wallet2.request_addresses == {deposit(1): open_req.id}
    rpc.add(native_tx(deposit(1), TON, 600))
    await d2.poll_wallet(key)
    assert wallet2.get_request(open_req.id).status == PR_PAID
    [event] = events(d2, key, "new_payment")
    assert event["address"] == open_req.id


async def test_ton_and_usdt_wallets_of_one_key_stay_apart(daemon, rpc):
    key, wallet, req = await native_request(daemon)
    usdt_key, usdt_wallet, usdt_req = await usdt_request(daemon)
    watched = set(wallet.request_addresses) | {deposit(0)}
    usdt_watched = set(usdt_wallet.request_addresses) | {deposit(0, usdt=True)}
    assert not watched & usdt_watched
    assert req.address != usdt_req.address and req.index == usdt_req.index == 0
    # a USDT-shaped credit on the TON deposit and plain TON on the jetton wallet credit nothing
    rpc.add(usdt_tx(deposit(0), 15_000_000, 500), native_tx(usdt_req.watch_address, TON, 501))
    rpc.txs[0]["in_msg"]["value"] = "0"
    await daemon.poll_once()
    assert req.sent_amount == 0 and usdt_req.sent_amount == 0
    rpc.add(native_tx(deposit(0), TON, 502), usdt_tx(usdt_req.watch_address, 15_000_000, 503))
    await daemon.poll_once()
    assert req.status == PR_PAID and usdt_req.status == PR_PAID
    assert [e["contract"] for e in events(daemon, key, "new_payment")] == [None]
    assert [e["contract"] for e in events(daemon, usdt_key, "new_payment")] == [tonaddr.USDT_MASTER]


async def test_lower_lt_seen_after_higher_lt_is_credited(daemon, rpc):
    # lt order across shards is not the order transactions become final in
    key, wallet, first = await native_request(daemon)
    _, second = await request(daemon, key)
    rpc.add(native_tx(deposit(0), TON, 600))
    await daemon.poll_wallet(key)
    assert first.status == PR_PAID
    rpc.add(native_tx(deposit(1), TON, 550))
    await daemon.poll_wallet(key)
    assert second.status == PR_PAID


async def test_old_history_is_not_scanned(daemon, rpc):
    key, _, req = await native_request(daemon)
    rpc.add(native_tx(deposit(0), TON, 100, now=req.time - ton.UTIME_MARGIN - 1))
    await daemon.poll_wallet(key)
    assert req.sent_amount == 0


# --- provider ------------------------------------------------------------------------


class Recorder:
    def __init__(self):
        self.lines = []

    def __getattr__(self, level):
        return lambda msg, *a, **k: self.lines.append(str(msg))


@pytest.fixture
async def server(unused_tcp_port):
    seen, replies = [], []

    async def handler(request):
        seen.append((request.path, list(request.query.items())))
        status, body = replies.pop(0) if replies else (200, {"last": {"seqno": 7}})
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    yield f"http://127.0.0.1:{unused_tcp_port}/api/v3?api_key=SECRET123", seen, replies
    await runner.cleanup()


async def test_provider_keeps_api_key_and_retries_transient(server, monkeypatch):
    url, seen, replies = server
    log = Recorder()
    monkeypatch.setattr(ton, "logger", log)
    monkeypatch.setattr(ton, "RETRY_DELAYS", (0, 0))
    provider = ton.TonRPCProvider(url)
    replies.extend([(429, {}), (502, {})])
    assert await provider.send_single_request("masterchainInfo") == {"last": {"seqno": 7}}
    assert [path for path, _ in seen] == ["/api/v3/masterchainInfo"] * 3
    assert all(("api_key", "SECRET123") in query for _, query in seen)
    await provider.send_single_request("transactions", [("account", "a"), ("account", "b")])
    assert seen[-1][1] == [("api_key", "SECRET123"), ("account", "a"), ("account", "b")]
    replies.extend([(500, {})] * 3)
    with pytest.raises(ton.TonTransientError) as err:
        await provider.send_single_request("masterchainInfo")
    replies.append((400, {"error": "limit is not allowed"}))
    with pytest.raises(ton.TonRPCError, match="limit is not allowed") as err2:
        await provider.send_single_request("transactions")
    await provider.close()
    assert len(log.lines) == 4
    for text in [*log.lines, str(err.value), str(err2.value)]:
        assert "SECRET123" not in text and "api_key" not in text


async def test_unreachable_provider_error_hides_url(unused_tcp_port, monkeypatch):
    monkeypatch.setattr(ton, "logger", Recorder())
    monkeypatch.setattr(ton, "RETRY_DELAYS", ())
    provider = ton.TonRPCProvider(f"http://127.0.0.1:{unused_tcp_port}/api/v3?api_key=SECRET123")
    with pytest.raises(ton.TonTransientError) as err:
        await provider.send_single_request("masterchainInfo")
    await provider.close()
    assert "SECRET123" not in str(err.value) and "127.0.0.1" not in str(err.value)


async def test_getservers_and_logs_redacted(daemon, rpc, monkeypatch):
    log = Recorder()
    monkeypatch.setattr(ton, "logger", log)
    assert daemon.getservers() == ["https://toncenter.com"]
    assert daemon.coin.current_server() == "https://toncenter.com"
    assert SERVER in daemon.SERVER
    key, _, _ = await native_request(daemon)
    rpc.failures["transactions"] = [ton.TonTransientError("HTTP 503")]
    await daemon.poll_wallet(key)
    info = await daemon.getinfo()
    assert info["server"] == "https://toncenter.com"
    assert log.lines and all("SECRET123" not in line for line in log.lines)


async def test_spam_on_one_address_hides_only_that_address(daemon, rpc, monkeypatch):
    monkeypatch.setattr(ton.TONDaemon, "PAGE_SIZE", 3)
    monkeypatch.setattr(ton.TONDaemon, "MAX_PAGES", 2)
    key, wallet = await load(daemon)
    spammed, victim = [(await request(daemon, key))[1] for _ in range(2)]
    for n in range(10):
        rpc.add(native_tx(deposit(0), 1, 500 + n, n=n))
    rpc.add(native_tx(deposit(1), TON, 600))
    await daemon.poll_wallet(key)
    assert victim.status == PR_PAID and spammed.status == PR_UNPAID


async def test_server_on_another_network_is_refused(daemon, rpc):
    key, wallet, req = await native_request(daemon)
    rpc.global_id = -3
    with pytest.raises(ton.TonRPCError):
        await daemon.coin.get_block_number()
    rpc.add(native_tx(deposit(0), TON, 500))
    with pytest.raises(ton.TonRPCError):  # process_pending logs it; no wallet is polled
        await daemon.poll_once()
    assert req.sent_amount == 0


async def test_closed_wallet_copy_cannot_roll_back_the_counter(tmp_path, monkeypatch, rpc):
    d = make_daemon(tmp_path, monkeypatch, rpc)
    key, old = await load(d)
    _, stale = await request(d, key)
    await d.close_wallet_impl(key)
    _, fresh = await load(d)
    assert fresh is not old
    await request(d, key)
    assert fresh.next_index == 2
    old.set_request_status(stale.id, PR_EXPIRED)  # what the old copy's expiry task does
    d2 = make_daemon(tmp_path, monkeypatch, rpc)
    _, reloaded = await load(d2)
    assert reloaded.next_index == 2 and len(reloaded.receive_requests) == 2


async def test_diskless_load_still_uses_the_wallet_file(tmp_path, monkeypatch, rpc):
    d = make_daemon(tmp_path, monkeypatch, rpc)
    key, _ = await load(d)
    await request(d, key)
    d2 = make_daemon(tmp_path, monkeypatch, rpc)
    wallet = await d2.load_wallet(PUBKEY, None, diskless=True)
    assert wallet.next_index == 1
