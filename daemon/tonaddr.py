"""TON address math, standard library only: shared by the daemon and the sweep tool."""

import base64
import hashlib
import re

NATIVE_OFFSET = 1_000_000_000
USDT_OFFSET = 2_000_000_000
FEE_WALLET_ID = 999_999_999
USDT_MASTER = "0:B113A994B5024A16719F69139328EB759596C38A25F59028B146FECDC3621DFE"

# Wallet v3r2 code cell (one cell, no refs). Checked against toncenter's code_hash of live wallets
# whose interface is wallet_v3r2, and end to end against live wallets (tests/test_tonaddr.py).
V3R2_CODE_HASH = bytes.fromhex("84dafa449f98a6987789ba232358072bc0f76dc4524002a5d0918b9a75d2d599")
V3R2_CODE_DEPTH = 0

BOC_MAGIC = bytes.fromhex("b5ee9c72")
_RAW = re.compile(r"(-?\d+):([0-9a-fA-F]{64})")


def crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def _raw(workchain: int, hash_bytes: bytes) -> str:
    if workchain not in (0, -1):
        raise ValueError("Unsupported workchain")
    return f"{workchain}:{hash_bytes.hex().upper()}"


def parse_address(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("Address must be a string")
    if match := _RAW.fullmatch(text):
        return _raw(int(match[1]), bytes.fromhex(match[2]))
    if len(text) != 48:
        raise ValueError("Invalid address length")
    try:
        data = base64.urlsafe_b64decode(text.replace("+", "-").replace("/", "_"))
    except ValueError:
        raise ValueError("Invalid address encoding") from None
    if len(data) != 36 or data[0] & 0x7F not in (0x11, 0x51):
        raise ValueError("Invalid address flags")
    if int.from_bytes(data[34:], "big") != crc16(data[:34]):
        raise ValueError("Invalid address checksum")
    return _raw(int.from_bytes(data[1:2], "big", signed=True), data[2:34])


def _split(raw: str) -> tuple[int, bytes]:
    workchain, hex_hash = parse_address(raw).split(":")
    return int(workchain), bytes.fromhex(hex_hash)


def friendly(raw: str, bounceable: bool = False, testnet: bool = False) -> str:
    workchain, hash_bytes = _split(raw)
    tag = (0x11 if bounceable else 0x51) | (0x80 if testnet else 0)
    data = bytes([tag]) + workchain.to_bytes(1, "big", signed=True) + hash_bytes
    return base64.urlsafe_b64encode(data + crc16(data).to_bytes(2, "big")).decode()


def _padded(bits: str) -> bytes:
    if len(bits) % 8:
        bits += "1" + "0" * (7 - len(bits) % 8)
    return int(bits, 2).to_bytes(len(bits) // 8, "big") if bits else b""


def _descriptors(bits: str, refs: int) -> bytes:
    return bytes([refs, len(bits) // 8 + (len(bits) + 7) // 8])


def _cell_hash(bits: str, refs: list[tuple[bytes, int]] = ()) -> bytes:
    """Representation hash of an ordinary level-0 cell; refs are (hash, depth) pairs."""
    data = _descriptors(bits, len(refs)) + _padded(bits)
    data += b"".join(depth.to_bytes(2, "big") for _, depth in refs) + b"".join(h for h, _ in refs)
    return hashlib.sha256(data).digest()


def wallet_address(pubkey_hex: str, subwallet_id: int) -> str:
    pubkey = bytes.fromhex(pubkey_hex)
    if len(pubkey) != 32 or not 0 <= subwallet_id < 2**32:
        raise ValueError("Invalid public key or subwallet id")
    data_bits = f"{0:032b}{subwallet_id:032b}{int.from_bytes(pubkey, 'big'):0256b}"
    # StateInit: no split_depth, no special, code and data present, no library
    state_init = _cell_hash("00110", [(V3R2_CODE_HASH, V3R2_CODE_DEPTH), (_cell_hash(data_bits), 0)])
    return _raw(0, state_init)


def _address_bits(raw: str) -> str:
    workchain, hash_bytes = _split(raw)
    return f"100{workchain & 0xFF:08b}{int.from_bytes(hash_bytes, 'big'):0256b}"


def address_cell_boc(raw: str) -> str:
    bits = _address_bits(raw)
    cell = _descriptors(bits, 0) + _padded(bits)
    # flags: no index, no crc, 1-byte cell references; 1-byte offsets; 1 cell, 1 root, 0 absent
    boc = BOC_MAGIC + bytes([0x01, 0x01, 1, 1, 0, len(cell), 0]) + cell
    return base64.b64encode(boc).decode()


def _unpad(data: bytes, d2: int) -> str:
    bits = "".join(f"{byte:08b}" for byte in data)
    if d2 % 2:
        bits = bits.rstrip("0")[:-1]
    return bits


def address_from_boc(b64: str) -> str:
    try:
        boc = base64.b64decode(b64, validate=True)
    except ValueError:
        raise ValueError("Invalid BOC encoding") from None
    if len(boc) < 7 or boc[:4] != BOC_MAGIC:
        raise ValueError("Invalid BOC")
    ref_size, off_size = boc[4] & 0x07, boc[5]
    pos = 6
    cells, roots = (int.from_bytes(boc[pos + i * ref_size : pos + (i + 1) * ref_size], "big") for i in range(2))
    pos += 3 * ref_size + off_size
    if cells != 1 or roots != 1:
        raise ValueError("Expected a single-cell BOC")
    pos += ref_size + (cells * off_size if boc[4] & 0x80 else 0)
    if len(boc) < pos + 2:
        raise ValueError("Truncated BOC")
    d1, d2 = boc[pos], boc[pos + 1]
    data = boc[pos + 2 : pos + 2 + (d2 + 1) // 2]
    if d1 or len(data) != (d2 + 1) // 2:
        raise ValueError("Unexpected cell in BOC")
    bits = _unpad(data, d2)
    if len(bits) != 267 or bits[:3] != "100":
        raise ValueError("Cell does not hold a standard address")
    return _raw(int.from_bytes(bytes([int(bits[3:11], 2)]), "big", signed=True), int(bits[11:], 2).to_bytes(32, "big"))
