import base64
import unittest
import unittest.mock
from decimal import Decimal

import check
import soroban

# Spiko EUTBL contract instance on Stellar, ledger 65409872 (10 Nov 2026), from Soroban RPC getLedgerEntries
EUTBL = "CBGV2QFQBBGEQRUKUMCPO3SZOHDDYO6SCP5CH6TW7EALKVHCXTMWDDOF"
INSTANCE = "AAAABgAAAAAAAAABTV1AsAhMSEaKowT3bllxxjw70hP6I/p2+QC1VOK82WEAAAAUAAAAAQAAABMAAAAA9I8Q2cfTX8d4O14nV4vw52Tjow/yajvX2OV3WMF6nqgAAAABAAAABQAAAA8AAAAITUVUQURBVEEAAAARAAAAAQAAAAMAAAAPAAAACGRlY2ltYWxzAAAAAwAAAAUAAAAPAAAABG5hbWUAAAAOAAAAIlNwaWtvIEVVIFQtQmlsbHMgTW9uZXkgTWFya2V0IEZ1bmQAAAAAAA8AAAAGc3ltYm9sAAAAAAAOAAAABUVVVEJMAAAAAAAADwAAAARQRVJNAAAAEgAAAAFMdcZX6PuGBe1XMmZSaaHKif8GuZNIs6src/D6zZXWaQAAAA8AAAAGUkVERU1QAAAAAAASAAAAASuVa8SNTKNdcDEcp4IGRmWsKbwMruwNlMb90lfey2RSAAAAEAAAAAEAAAABAAAADwAAAAVPd25lcgAAAAAAABIAAAAAAAAAAHCIXCogfd6QO7TiicvRj01yP9KEQrfEmTtLhU/2CybHAAAAEAAAAAEAAAABAAAADwAAAAtUb3RhbFN1cHBseQAAAAAKAAAAAAAAAAAAABHngtstXw=="


class SorobanTest(unittest.TestCase):
    def test_reads_name_decimals_and_total_supply_from_instance_storage(self):
        def rpc(_url, method, params):
            self.assertEqual((method, params), ("getLedgerEntries", {"keys": [soroban.instance_key(EUTBL)]}))
            return {"entries": [{"xdr": INSTANCE}], "latestLedger": 65409872}
        self.assertEqual(soroban.read_soroban("u", EUTBL, rpc),
                         {"name": "Spiko EU T-Bills Money Market Fund", "supply": Decimal("196860305.19647"), "at": "ledger 65409872"})

    def test_the_entry_belongs_to_the_contract_we_asked_for(self):
        r = soroban.Reader(base64.b64decode(INSTANCE))
        r.int(">i"), r.int(">i")  # entry type, extension
        self.assertEqual(soroban.address(r), "C:" + soroban.contract_id(EUTBL).hex())

    def test_rejects_a_non_contract_address(self):
        with self.assertRaises(ValueError):
            soroban.contract_id("GBHNGLLIE3KWGKCHIKMHJ5HVZHYIK7WTBE4QF5PLAKL4CJGSEU7HZIW5")

    def test_missing_instance_and_truncated_xdr_raise(self):
        with self.assertRaises(ValueError):
            soroban.read_soroban("u", EUTBL, lambda *a: {"entries": [], "latestLedger": 1})
        with self.assertRaises(ValueError):
            soroban.instance_storage(INSTANCE[:200])


class StarknetTest(unittest.TestCase):
    def test_byte_array_name(self):
        felts = ["0x1", "0x5370696b6f20455520542d42696c6c73204d6f6e6579204d61726b65742046", "0x756e64", "0x3"]
        self.assertEqual(check.byte_array(felts), "Spiko EU T-Bills Money Market Fund")

    def test_supply_is_u256_low_high_at_one_block(self):
        blocks = []

        def rpc(_url, method, params):
            if method == "starknet_blockNumber":
                return 17759071
            blocks.append(params["block_id"])
            sel = params["request"]["entry_point_selector"]
            if sel == check.STARKNET_TOTAL_SUPPLY:
                return ["0x1bf3c4759", "0x1"]
            if sel == check.STARKNET_DECIMALS:
                return ["0x5"]
            return ["0x0", "0x4142", "0x2"]
        with unittest.mock.patch.object(check, "jsonrpc", rpc):
            out = check.read_starknet("u", "0xabc")
        self.assertEqual(out, {"name": "AB", "supply": Decimal((1 << 128) + 7503365977).scaleb(-5), "at": "block 17759071"})
        self.assertEqual(set(map(str, blocks)), {"{'block_number': 17759071}"})


if __name__ == "__main__":
    unittest.main()
