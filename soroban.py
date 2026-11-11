"""Read a custom Soroban token's supply from its contract instance storage, with no SDK.

Spiko's Stellar contracts keep METADATA (name, symbol, decimals) and TotalSupply in instance storage. Soroban RPC
getLedgerEntries returns that entry as XDR; this decodes the subset of XDR it uses.
"""
import base64
import struct
from decimal import Decimal

LEDGER_KEY_CONTRACT_DATA, SC_ADDRESS_CONTRACT, SCV_LEDGER_KEY_CONTRACT_INSTANCE, PERSISTENT = 6, 1, 20, 1


class Reader:
    def __init__(self, data):
        self.data, self.at = data, 0

    def take(self, n):
        chunk = self.data[self.at:self.at + n]
        if len(chunk) < n:
            raise ValueError("XDR ended early")
        self.at += n
        return chunk

    def int(self, fmt):
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def opaque(self):
        n = self.int(">I")
        chunk = self.take(n)
        self.take(-n % 4)
        return chunk


def contract_id(strkey):
    """C... strkey to the 32-byte contract hash."""
    raw = base64.b32decode(strkey)
    if raw[0] != 2 << 3:
        raise ValueError(f"{strkey} is not a contract address")
    return raw[1:33]


def instance_key(strkey):
    """LedgerKey::ContractData(contract, LedgerKeyContractInstance, Persistent), base64 XDR."""
    return base64.b64encode(struct.pack(">ii", LEDGER_KEY_CONTRACT_DATA, SC_ADDRESS_CONTRACT) + contract_id(strkey)
                            + struct.pack(">ii", SCV_LEDGER_KEY_CONTRACT_INSTANCE, PERSISTENT)).decode()


def address(r):
    kind = r.int(">i")
    if kind == 0:
        r.int(">i")  # public key type
        return "G:" + r.take(32).hex()
    if kind == 1:
        return "C:" + r.take(32).hex()
    raise ValueError(f"unsupported ScAddress type {kind}")


def scmap(r):
    return {key(scval(r)): scval(r) for _ in range(r.int(">I"))} if r.int(">I") else None


def key(v):
    """Map keys as plain strings: a symbol stays itself, an enum variant like Vec[Symbol('TotalSupply')] becomes its name."""
    return v[0] if isinstance(v, list) and len(v) == 1 else v


def scval(r):
    kind = r.int(">i")
    if kind == 0:
        return bool(r.int(">I"))
    if kind == 1:
        return None
    if kind in (3, 4):
        return r.int(">I" if kind == 3 else ">i")
    if kind in (5, 7, 8):
        return r.int(">Q")
    if kind == 6:
        return r.int(">q")
    if kind in (9, 10):
        hi, lo = r.int(">Q" if kind == 9 else ">q"), r.int(">Q")
        return (hi << 64) | lo
    if kind == 13:
        return r.opaque().hex()
    if kind in (14, 15):
        return r.opaque().decode()
    if kind == 16:
        return [scval(r) for _ in range(r.int(">I"))] if r.int(">I") else None
    if kind == 17:
        return scmap(r)
    if kind == 18:
        return address(r)
    if kind == 19:
        if r.int(">i") == 0:
            r.take(32)  # wasm hash
        return scmap(r)
    if kind == SCV_LEDGER_KEY_CONTRACT_INSTANCE:
        return "instance"
    raise ValueError(f"unsupported ScVal type {kind}")


def instance_storage(entry_xdr):
    r = Reader(base64.b64decode(entry_xdr))
    if r.int(">i") != LEDGER_KEY_CONTRACT_DATA:
        raise ValueError("not a contract data entry")
    r.int(">i")  # extension point
    address(r)
    scval(r)
    r.int(">i")  # durability
    return scval(r)


def read_instance(url, strkey, rpc):
    """(instance storage, ledger it was read at)."""
    out = rpc(url, "getLedgerEntries", {"keys": [instance_key(strkey)]})
    if not out.get("entries"):
        raise ValueError(f"no contract instance for {strkey}")
    return instance_storage(out["entries"][0]["xdr"]), out["latestLedger"]


def decode(b64):
    return scval(Reader(base64.b64decode(b64)))


def strkey(addr):
    """'G:<hex>' or 'C:<hex>' from address() back to the G... / C... form explorers show."""
    kind, raw = addr.split(":")
    payload = bytes([(6 if kind == "G" else 2) << 3]) + bytes.fromhex(raw)
    crc = 0
    for byte in payload:  # CRC16-XModem, appended little-endian
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
    return base64.b32encode(payload + crc.to_bytes(2, "little")).decode()


def read_soroban(url, strkey, rpc):
    storage, ledger = read_instance(url, strkey, rpc)
    meta = storage["METADATA"]
    supply = Decimal(storage["TotalSupply"]).scaleb(-meta["decimals"])
    return {"name": meta["name"], "supply": supply, "at": f"ledger {ledger}"}
