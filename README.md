# TON and USDT on TON for Bitcart

A [Bitcart](https://github.com/bitcart/bitcart) **docker plugin**. It adds the coin `ton` (the native coin of TON, 9 decimals; wallets now show it as "GRAM (prev. TON)") and the jetton USDT on TON (master `EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs`, 6 decimals). Each invoice gets its own deposit address. The daemon is watch-only: it never holds a private key. An operator-run sweep tool moves the money out. No Bitcart core, SDK, database or frontend change.

Built and tested against **Bitcart 0.10.3.0** (docker deployment, `bitcart-docker`). Status: the daemon and the sweep tool were tested on mainnet; the plugin has not yet run inside a production Bitcart backend (see "Live test record and what is not tested").

This is an independent plugin. It is not part of the Bitcart project and the Bitcart maintainers do not support it.

Parts (under `compose/plugins/docker/ton/` on the Bitcart host, except `sweep/`):

- `daemon/` — the TON daemon image (`forked/bitcart-ton:0.10.3.0`, built `FROM bitcart/bitcart-xmr:0.10.3.0`, adds `ton.py` and `tonaddr.py` to Bitcart's `daemons/`). Port 5013. Pure Python, no extra packages. `pull_policy: build`, so `./update.sh` skips it and `./start.sh` builds it locally.
- `backend/forkedton/ton/plugin.py` — registers the `TON` coin class in the backend and worker (explorer tonviewer, CoinGecko id `the-open-network`, display divisibility 8). It also adds the `post_create_payment_method` filter (see below). `rules/90_ton_modules.py` appends one read-only mount, `/app/modules/forkedton`, to backend and worker. The package name is `forkedton` because the Solana plugin already uses `/app/modules/forked`.
- `components/ton.yml` — the compose service, `TON_HOST` and `TON_NETWORK` for backend and worker, the `ton_datadir` volume.
- `sweep/` — the sweep tool (`sweep.py`, image from `python:3.12-slim`, pinned and hash-checked `pytoniq-core` and `ton-core`). It is not a compose service. Never install it on the Bitcart host.
- `tests/`, `backend_tests/`, `sweep/tests/` — the three test suites (see "Tests and lint").

## How it works

- Wallet key = the ed25519 public key of the plugin seed, 64 hex characters. The sweep tool prints it (`info`). The daemon never reads the key from the chain.
- Deposit address = a wallet v3r2 address from (public key, subwallet id). Subwallet id = `1000000000 + n` for TON invoices and `2000000000 + n` for USDT invoices. `n` is a per-wallet counter (`next_index` in the wallet file). The daemon writes the next counter value to disk before it gives out the address, so an address is never used twice. One invoice with a TON method and a USDT method has two different addresses.
- Fee wallet = subwallet id `999999999` of the same key. Bitcart shows it as the wallet address. The wallet balance in the admin panel is the balance of the fee wallet only, not the sum of the deposit addresses.
- Addresses are shown in the non-bounceable form (`UQ…` on mainnet, `0Q…` on testnet). A deposit wallet is not deployed. A bounceable transfer to an undeployed account bounces back to the payer.
- USDT: for each invoice the daemon asks the USDT master (get-method `get_wallet_address`) for the jetton wallet of the deposit address. It stores that address on the invoice and watches it. It never takes a jetton wallet address from an index listing.
- Polling: every 5 s the daemon reads `/transactions` for the watched addresses of all open invoices (100 addresses per request, ascending, 1000 per page, at most 20 pages). Each poll starts at the creation time of the oldest open invoice, minus 600 s. There is no cursor. Logical time order across shards is not the order in which transactions become final, so a cursor could pass a payment. Each poll reads the open invoices again. `tx_hashes` stops a second credit. A transaction that cannot be processed is read again on the next poll. If a page is full, the daemon reads each address of that request alone, so transaction spam on one invoice address can hide only that invoice.
- Credit rules, all payments: `finality == "finalized"` and `emulated == false`; the transaction is on a watched address of an open invoice.
  - TON: internal incoming message (has a source), `value > 0`, `bounced == false`, and not (`aborted` and the message `bounce` flag set).
  - USDT: the transaction is on the invoice's jetton wallet; incoming opcode `0x178d4519` (jetton internal transfer); not aborted; compute phase not skipped, success, exit code 0; body decoded as `jetton_internal_transfer`; amount `> 0`. The amount comes from that body. Jetton notify messages to the owner are never used.
- Finality: only finalized transactions are credited. They cannot be rolled back. Each credited transaction reports 32 confirmations, so the invoice completes on detection for all store speed settings. The daemon sends `new_block` when the masterchain seqno changes.
- Partial payments: credits on one open invoice add up. The invoice becomes paid when the sum reaches the amount.
- After an invoice is paid or expired, its address is no longer watched. A later payment is not credited and the daemon writes nothing about it. The sweep tool collects it. Credit it by hand. This also applies to a payment sent in the last seconds before expiry (finality plus one poll, about 15 s), and to a payment made while the daemon was down past the expiry.
- TON sent to a USDT invoice address is not credited. The sweep tool collects it as TON. USDT sent to a TON invoice address is not credited, and the sweep tool does not move it.
- Payment link: `ton://transfer/<address>?amount=<base units>`, plus `&jetton=<USDT master, bounceable form>` for USDT. `modifypaymenturl` rewrites the amount in base units (9 decimals for TON, 6 for USDT).
- No sender-address step: `setrequestaddress` returns `False`. The backend filter sets `user_address = payment_address` on each `ton` payment method. This tells the operator's own checkouts not to ask for a sending address.
- Not supported ("Currently not supported" or NotImplementedError): `payto`, `transfer`, `broadcast`, `signmessage`, `verifymessage`, `make_seed`, `add_peer`, fee estimation, `rescan_blocks`. Do not enable "include network fee" on a store for these wallets.

## Limits

- Only the mainnet USDT master is accepted. On testnet the daemon has no tokens and refuses a USDT wallet.
- An invoice address is watched only while its invoice is open.
- toncenter without an API key allows about 1 request per second. Production needs a key: `TON_SERVER=https://toncenter.com/api/v3?api_key=<key>`. The daemon shows only scheme and host in logs, errors and `getservers`.
- With `TON_DEBUG=false` (default) the Bitcart daemon base disables all logging. `docker logs compose-ton-1` then shows nothing. Set `TON_DEBUG=true` to see `Using server …`, `<symbol> credit <amount> to <address> in <hash>` and errors.

## Environment

- `TON_SERVER` (compose default `https://toncenter.com/api/v3`): toncenter v3 base URL. A comma list = failover (stock `MultipleProviderRPC`). 429 and 5xx replies are tried again after 1, 2 and 4 s.
- `TON_NETWORK` (default `mainnet`): `mainnet` or `testnet`. The same value goes to backend and worker (explorer link; the admin coin list shows `Gram (TON, testnet)`). `components/ton.yml` sets a mainnet default for `TON_SERVER`, so the daemon does not pick the testnet server itself. For testnet, set `TON_SERVER=https://testnet.toncenter.com/api/v3` and `TON_NETWORK=testnet` together.
- `TON_DEBUG` (default `false`).

Where to set them: the same rules as in the [bitcart-solana-usdt](https://github.com/apezio/bitcart-solana-usdt) README apply. `./update.sh` rewrites `/root/bitcart-docker/.env` and keeps only a fixed list plus `*_SERVER` and `*_DEBUG`.

- `TON_SERVER` and `TON_DEBUG` survive in `.env`. Put them in `.env` and also in `/root/bitcart.env.sh`.
- `TON_NETWORK` is dropped from `.env` by the next update. For testnet, add `export TON_NETWORK=testnet` to `/etc/profile.d/bitcart-env.sh`, record it in `/root/bitcart.env.sh`, and add it again after any `setup.sh` run. If it is lost while `TON_SERVER` points to testnet, the daemon refuses the server (it compares the chain id of the server with `TON_NETWORK`) and detects nothing until the two agree.
- After a change to any `TON_*`, run `./start.sh`, so the daemon and backend/worker get the same value.

## First-time setup

1. Build the sweep image on the operator computer, from this plugin directory (docker works the same):

   ```
   podman build -f sweep/Dockerfile -t bitcart-ton-sweep .
   ```

2. Make a NEW seed for this plugin only. The sweep tool does not create seeds. Create a 24-word TON seed with a trusted TON wallet app on a safe device. Keep it in a password manager. It must be a TON seed: the tool refuses a BIP39 seed.
3. Get the public key and the fee wallet address. The tool asks for the seed at a hidden prompt and stores nothing:

   ```
   podman run --rm -it bitcart-ton-sweep info --server 'https://toncenter.com/api/v3?api_key=<key>'
   ```

4. Send some TON to the fee wallet. It pays the gas of USDT sweeps. Per USDT sweep the tool tops up the deposit address to 0.06 TON (0.05 TON attached to the jetton transfer + 0.01 TON) and keeps 0.01 TON back in the fee wallet. The unused TON returns to the fee wallet after each sweep. So 0.07 TON is the minimum; N × 0.07 TON covers N USDT sweeps even if no gas comes back.
5. Copy the plugin to the Bitcart host and enable it:

   ```
   # from this plugin directory on the operator computer
   tar cf - --exclude=.git --exclude=__pycache__ --exclude=sweep . | ssh bitcart \
     'sudo mkdir -p /root/bitcart-docker/compose/plugins/docker/ton && \
      sudo tar xf - --no-same-owner -C /root/bitcart-docker/compose/plugins/docker/ton && \
      sudo chown -R root:root /root/bitcart-docker/compose/plugins/docker/ton && \
      sudo chmod -R go-w /root/bitcart-docker/compose/plugins/docker/ton'
   # on the Bitcart host, as root
   sed -i 's/^BITCART_CRYPTOS=.*/&,ton/' /root/bitcart-docker/.env
   sed -i 's/^export BITCART_CRYPTOS=.*/&,ton/' /root/bitcart.env.sh
   cd /root/bitcart-docker && ./start.sh
   ```

   If another plugin (for example [bitcart-acctpool](https://github.com/apezio/bitcart-acctpool)) made the `compose/plugins/docker` tree root-owned and mounted read-only: Files there must be root-owned and not writable by others. The `chown`/`chmod` lines above do this.
6. In the admin panel: Wallets → create, currency **Gram (TON)**, Public key = the 64-hex key. Create a second wallet with the same key and contract **USDT**. Attach both wallets to the store.

## Sweep procedure

Run the tool on the operator computer or another trusted machine. Never run it on the Bitcart host.

1. Get the two counters. `getinfo` returns `next_index` = {wallet key: counter}. The TON wallet key is the public key. The USDT wallet key is `<public key>_0:B113A994B5024A16719F69139328EB759596C38A25F59028B146FECDC3621DFE`. The daemon RPC is JSON-RPC on port 5013 inside the container, Basic auth `electrum:electrumz` (Bitcart defaults). Passing the key loads that wallet. Example to verify on the host:

   ```
   docker exec compose-ton-1 python -c 'import base64, json, sys, urllib.request as u
   for x in (sys.argv[1], {"xpub": sys.argv[1], "contract": "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"}):
       body = json.dumps({"id": 1, "method": "getinfo", "params": {"xpub": x}}).encode()
       auth = "Basic " + base64.b64encode(b"electrum:electrumz").decode()
       r = u.Request("http://localhost:5013", body, {"Authorization": auth, "Content-Type": "application/json"})
       print(json.load(u.urlopen(r))["result"]["next_index"])' <public key>
   ```

   If toncenter is not reachable, `getinfo` has no `next_index`. A count that is too high is safe: unused addresses show `skip: empty`.
2. Dry run first. It sends nothing. Keep a directory for the config file:

   ```
   mkdir -p ~/ton-sweep
   podman run --rm -it --userns=keep-id -v ~/ton-sweep:/config:Z bitcart-ton-sweep sweep \
     --server 'https://toncenter.com/api/v3?api_key=<key>' --ton-count <N> --usdt-count <M>
   ```

   The container runs as uid 1000 and writes the config in `/config`. With docker, the directory must be writable by uid 1000.
3. First run only: the tool asks for the destination (your main wallet), shows it, and saves it after you type `yes` to `ton-sweep-<network>.json` (`--config` sets another file). Later runs read it. It refuses an address of the other network and an address of this seed's own wallets.
4. Read the table. Columns: `index` (n), `kind` (`ton` or `usdt`), `address` (non-bounceable), `TON` balance, `USDT` balance (USDT rows only), `action`:
   - `sweep` — will be moved.
   - `skip: empty` — no balance.
   - `skip: below limit` — balance under `--min-ton` (default 0.01) or `--min-usdt` (default 0.1).
   - `skip: not a v3r2 wallet` — the address is active with other code. Do not touch it.

   The summary line gives the totals. The fee wallet line says `enough` or `NOT enough: top it up`.
5. Run again with `--send`. The tool shows the destination (bounceable only if it is deployed) and asks for `yes`. For each `sweep` row it reads the balance again before it signs, then waits for a finalized, successful transaction:
   - TON row: the whole balance goes to the destination and the empty account is deleted.
   - USDT row: the fee wallet tops the address up to 0.06 TON. Then one message sends all USDT to the destination and the remaining TON back to the fee wallet.
   - Results: `swept …`, `skipped: balance gone`, or `FAILED …`. Then `Summary: swept X, skipped Y, failed Z`.
6. It is safe to run again after a stop or a failure. Each step is decided from the chain state. A message that did not become final expires after about 5 minutes and cannot apply later.

Exit code: 0 for a dry run, nothing to sweep, or all sweeps done. 1 if a row failed, you did not confirm, an error occurred, or you stopped it.

## Checks after deployment

- `curl https://<your Bitcart host>/api/cryptos` lists `ton`. `/api/cryptos/tokens/ton` → `["USDT"]`.
- `docker logs compose-ton-1` shows the daemon running (needs `TON_DEBUG=true`). There is no `.plugins-failed` in the `bitcart_datadir` volume.
- Create a small real invoice. Pay it in TON from a wallet app. Expect paid; the transaction link points to tonviewer.
- Create a second small invoice. Pay it in USDT. Expect paid.
- Run a dry run, then `--send`, for both addresses. Check that both amounts arrive at the destination.

## Tests and lint

Three suites. All use recorded or mocked toncenter replies; none sends anything to a network. From this plugin directory:

Daemon suite (`tests/`: address math against live v3r2 wallets, credit rules, adversarial cases, recorded mainnet TON and USDT transactions), plus ruff with the settings in `pyproject.toml`:

```
podman run --rm -v "$PWD":/plug:ro,z bitcart/bitcart-xmr:0.10.3.0 sh -c \
  "uv pip install -q --python /home/electrum/.venv/bin/python pytest pytest-asyncio; \
   cp -r /plug /tmp/plug && cd /tmp/plug && python -m pytest -q -p no:cacheprovider tests && \
   uvx -q ruff check . && uvx -q ruff format --check ."
```

Backend suite (`backend_tests/`: the stock loader finds `modules.forkedton.ton.plugin`, registration, network label, filter, rule and component shape), with the production mount layout. Add `-v <solana plugin>/backend/forked:/app/modules/forked:ro,z` to run the coexistence test too:

```
podman run --rm -v "$PWD":/plug:ro,z -v "$PWD/backend/forkedton":/app/modules/forkedton:ro,z -w /app \
  --entrypoint sh bitcart/bitcart:0.10.3.0 -c \
  "uv pip install -q --python /app/.venv/bin/python pytest pytest-asyncio && \
   python -m pytest -q -p no:cacheprovider /plug/backend_tests"
```

Sweep suite (`sweep/tests/`: addresses and signed messages against `tonutils`, the daemon/library cross-check, dry run, send, rerun after a stop, destination handling, seed checks):

```
podman run --rm -v "$PWD":/plug:ro,z python:3.12-slim sh -c \
  "cp -r /plug /work && cd /work && pip install -q --require-hashes -r sweep/requirements.txt && \
   pip install -q -r sweep/requirements-test.txt && python -m pytest -q -p no:cacheprovider sweep/tests"
```

## Live test record and what is not tested

Tested on mainnet on 2026-10-05, with the daemon image on a dev host (not through a Bitcart backend) and the sweep tool:

- A 0.2 TON payment and a 1 USDT payment to two invoice addresses: the daemon marked the two requests paid.
- One sweep run with `--send`: the TON sweep, the gas funding from the fee wallet, and the USDT sweep. The two invoice accounts were deleted, the destination received 0.2 TON (less the fee) and 1 USDT, and the USDT sweep cost the fee wallet approximately 0.0017 TON.

Not tested:

- The plugin inside a running Bitcart backend and worker: invoice creation through the API, the price lookup against the live CoinGecko service, the checkout page. Do the checks of "Checks after deployment" on the first install.
- Testnet. The operator decided to test on mainnet.

## Rollback

Before rollback, record the `next_index` counters (see "Sweep procedure"). They live in the `ton_datadir` volume. Sweep all deposit addresses before you delete it.

```
# on bitcart, as root
sed -i 's/,ton$//' /root/bitcart-docker/.env /root/bitcart.env.sh
rm -rf /root/bitcart-docker/compose/plugins/docker/ton
cd /root/bitcart-docker && ./start.sh
docker volume rm compose_ton_datadir   # optional, after the last sweep
```

Other coins are not changed. Existing TON wallets and invoices in the database stop being served. The money on deposit addresses stays under the seed; the sweep tool works without the daemon.

## Upgrade check (when the pinned Bitcart image changes)

Change the tag in `daemon/Dockerfile` (`FROM`), `components/ton.yml` (`image`) and the test recipes. Run all three suites before `./start.sh`. Then read the new source again:

- Daemon base (`daemons/genericprocessor.py`, `daemons/base.py`, `daemons/utils.py`, `daemons/storage.py`). `ton.py` overrides: `BlockProcessorDaemon.get_default_server_url`, `create_coin`, `shutdown_coin`, `load_wallet`, `process_pending`, `process_transaction`, `getinfo`, `getservers`, `setrequestaddress`, `rescan_blocks`, `modifypaymenturl`, `get_tokens`, `validatecontract`, `readcontract` and the unsupported stubs; `Wallet.create_payment_request_object`, `add_payment_request`, `remove_from_detection_dict`, `stop`, `save_db`, `balance`, `contract_addr`; `KeyStore.load_account_from_key`, `add_privkey`, `load`, `dump`; the `BlockchainFeatures` methods. It relies on: `Wallet.process_new_payment` (`tx_hashes` dedupe, sum of `sent_amount`), `set_request_status` (`PR_PAID` immutable), requests leaving the detection dict on paid/expired, `get_confirmations`, `trigger_event`, `StoredDBProperty`, `MultipleProviderRPC`, `AbstractRPCProvider`, `modify_payment_url`, the `rpc` decorator, `execute_method` loading a wallet from `xpub`, the `{"xpub", "contract"}` param form, `TON_LOGIN`/`TON_PASSWORD` defaults, `configure_logging` with `TON_DEBUG`. The text `Invalid contract address or non-ERC20 token` must still map to its code in `daemons/spec/eth.json`.
- `/app/modules` loading (`api/plugins.py` `load_plugins`): still `glob("modules/**/**/plugin.py")` from `/app`, imported as `modules.forkedton.ton.plugin`. The backend suite checks this.
- Price lookup: `plugin.py` wraps `find_id` and `find_by_contract` of `api/ext/exchanges/coingecko.py`. CoinGecko changed the symbol of id `the-open-network` from `ton` to `gram`, and it lists the USDT contract in the friendly form. Check that `CoingeckoExchange.refresh` still calls these two functions by module lookup and still keys quotes by the result of `find_id`, and that the id `the-open-network` still exists.
- Backend call sites: `CoinService` reads `TON_HOST`/`TON_NETWORK`; `invoices.py` calls `modifypaymenturl` and applies `post_create_payment_method`; `/api/cryptos/tokens` lists `get_tokens` keys. `plugin.py` touches `bitcart.COINS`, the SDK `ETH` class, `api.constants.SUPPORTED_CRYPTOS`, `api.ext.blockexplorer.EXPLORERS`, `api.ext.exchanges.coinrules`, `api.ext.moneyformat.currency_table`.
- bitcart-docker: `bitcart_update_docker_env` variable list, the generator's plugin component and rule loading, `pull_policy: build` skipped by `compose pull`.
- Port 5013 must stay free of upstream daemons.
- toncenter v3 reply fields read by the daemon: `masterchainInfo` `last.seqno`; `transactions` `hash`, `account`, `lt`, `now`, `mc_block_seqno`, `finality`, `emulated`, `total_fees`, `in_msg.source/value/bounced/bounce/opcode`, `in_msg.message_content.decoded` (`@type`, `amount.value`, `from.workchain_id/address`), `description.aborted`, `description.compute_ph.skipped/success/exit_code`; `accountStates` `accounts[].balance`; `runGetMethod` `exit_code`, `stack[0].type/value`; `jetton/wallets` `jetton_wallets[].balance`. By the sweep tool: `walletStates` `wallets[].address/status/balance/seqno/code_hash`, `runGetMethod` (`get_wallet_address`, `get_wallet_data`), `transactions` `in_msg.hash_norm` and `description.action.success`, `POST /message`.

## Separate jobs (not done by this plugin)

- TON entries in your own checkout (coin table, wallet settings), if you do not use the stock Bitcart checkout.
