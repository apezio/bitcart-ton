import base64

import pytest
import tonaddr as t
from conftest import load_fixture

# Live mainnet wallets whose interface toncenter names wallet_v3r2 (code hash 84dafa44…d599). Public key from the
# get-method get_public_key, subwallet id from the data cell (v3r2 has no get_subwallet_id), read 2026-10-05.
V3R2_VECTORS = [
    (
        "b82993f90f94b42371ada6d7ec2ad64e84df8bf7008830161435d26da2203956",
        698983191,
        "0:01D686AACDB7C5FE7A511D7A57693422A5DE73751C6730F95F91C8EFD30382A9",
    ),
    (
        "f8206f1913608391b90547a284012d5ba403514d48e271d45dc17f40867f58f3",
        698983191,
        "0:0379942CBC940A4D0EAA9C2C7782FF11725F005C4CB9E5FDC16FC443A27D3BE6",
    ),
    (
        "03a814675c496e4b4ca76a742c1f60b5077282d8e6e98c418e13974fa0b76ab1",
        698983191,
        "0:0709FC35B02497D5FDB308D51D7BF347DDA80CB0854846B7942428A9E92C387E",
    ),
    (
        "007004af2dbd6b8b2c455a7efc9e253f6a77e19a517a7c64c2fb70d3baadb9c4",
        280436832,
        "0:4DD31E5A3FB6218BAB7040E3D326EBDA0676E088322819217860B76779D44AF3",
    ),
]

OWNER = "0:852443F8599FE6A5DA34FE43049AC4E0BEB3071BB2BFB56635EA9421287C283A"
OWNER_FRIENDLY = "EQCFJEP4WZ_mpdo0_kMEmsTgvrMHG7K_tWY16pQhKHwoOoy2"
OWNER_JETTON_WALLET = "0:32081D54752B9340A75C777A94AF0483F07329DD296EB4F915BB45E477B93F8A"
# the slice toncenter accepted for get_wallet_address(OWNER) on the USDT master
OWNER_SLICE = "te6ccgEBAQEAJAAAQ4AQpIh/CzP81LtGn8hgk1icF9Zg43ZX9qzGvVKEJQ+FB1A="


@pytest.mark.parametrize(("pubkey", "subwallet", "address"), V3R2_VECTORS)
def test_wallet_address_matches_live_v3r2_wallets(pubkey, subwallet, address):
    assert t.wallet_address(pubkey, subwallet) == address
    assert t.wallet_address(pubkey.upper(), subwallet) == address


def test_wallet_address_depends_on_subwallet_and_key():
    pubkey = V3R2_VECTORS[0][0]
    ids = [t.NATIVE_OFFSET, t.NATIVE_OFFSET + 1, t.USDT_OFFSET, t.FEE_WALLET_ID]
    assert len({t.wallet_address(pubkey, i) for i in ids}) == len(ids)
    assert t.wallet_address(pubkey, t.NATIVE_OFFSET) != t.wallet_address(V3R2_VECTORS[1][0], t.NATIVE_OFFSET)


@pytest.mark.parametrize(("pubkey", "subwallet"), [("00" * 31, 1), ("zz" * 32, 1), ("00" * 32, -1), ("00" * 32, 2**32)])
def test_wallet_address_rejects_bad_input(pubkey, subwallet):
    with pytest.raises(ValueError):
        t.wallet_address(pubkey, subwallet)


def test_constants():
    assert t.NATIVE_OFFSET == 1_000_000_000
    assert t.USDT_OFFSET == 2_000_000_000
    assert t.FEE_WALLET_ID == 999_999_999
    assert t.parse_address("EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs") == t.USDT_MASTER
    assert t.USDT_OFFSET + 999_999_999 < 2**32


def test_friendly_and_raw_round_trip():
    assert t.parse_address(OWNER_FRIENDLY) == OWNER
    assert t.parse_address(OWNER.lower()) == OWNER
    assert t.friendly(OWNER, bounceable=True) == OWNER_FRIENDLY
    for bounceable in (False, True):
        for testnet in (False, True):
            text = t.friendly(OWNER, bounceable=bounceable, testnet=testnet)
            assert len(text) == 48
            assert t.parse_address(text) == OWNER
    assert t.friendly(OWNER).startswith("UQ")
    assert t.friendly(OWNER, testnet=True).startswith("0Q")
    assert t.friendly(OWNER, bounceable=True, testnet=True).startswith("kQ")


def test_parse_accepts_standard_base64():
    standard = base64.b64encode(base64.urlsafe_b64decode(OWNER_FRIENDLY)).decode()
    assert standard != OWNER_FRIENDLY
    assert t.parse_address(standard) == OWNER


def test_masterchain_raw_address():
    raw = "-1:" + "AB" * 32
    assert t.parse_address(t.friendly(raw)) == raw


@pytest.mark.parametrize(
    "bad",
    [
        OWNER_FRIENDLY[:-1] + ("A" if OWNER_FRIENDLY[-1] != "A" else "B"),  # CRC16 mismatch
        OWNER_FRIENDLY[:10] + ("x" if OWNER_FRIENDLY[10] != "x" else "y") + OWNER_FRIENDLY[11:],
        OWNER_FRIENDLY[:-2],
        "0:" + "AB" * 31,
        "5:" + "AB" * 32,
        "0:" + "ZZ" * 32,
        "",
        "!" * 48,
        base64.urlsafe_b64encode(b"\x22" + base64.urlsafe_b64decode(OWNER_FRIENDLY)[1:]).decode(),  # unknown tag
    ],
)
def test_parse_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        t.parse_address(bad)


def test_parse_rejects_non_string():
    with pytest.raises(ValueError):
        t.parse_address(None)


def test_crc16_xmodem_check_value():
    assert t.crc16(b"123456789") == 0x31C3


def test_address_cell_boc_matches_accepted_live_slice():
    assert t.address_cell_boc(OWNER) == OWNER_SLICE
    assert t.address_from_boc(OWNER_SLICE) == OWNER


def test_address_from_live_get_wallet_address_reply():
    reply = load_fixture("get_wallet_address.json")
    assert reply["exit_code"] == 0
    assert t.address_from_boc(reply["stack"][0]["value"]) == OWNER_JETTON_WALLET


@pytest.mark.parametrize("raw", [OWNER, OWNER_JETTON_WALLET, t.USDT_MASTER, "-1:" + "00" * 32, "0:" + "FF" * 32])
def test_boc_round_trip(raw):
    assert t.address_from_boc(t.address_cell_boc(raw)) == raw


@pytest.mark.parametrize("bad", ["", "not base64!", base64.b64encode(b"\x00" * 40).decode(), OWNER_SLICE[:-8] + "AAAA"])
def test_address_from_boc_rejects_garbage(bad):
    with pytest.raises(ValueError):
        t.address_from_boc(bad)
