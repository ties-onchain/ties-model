import io
import json
import tempfile
import unittest
from decimal import Decimal
from contextlib import redirect_stdout
from unittest import mock
from pathlib import Path

import sec

XML = """<edgarSubmission><reportDate>2026-10-31</reportDate><seriesId>S000067043</seriesId>
<netAssetOfSeries>833554415.30</netAssetOfSeries><numberOfSharesOutstanding>833590155.0700</numberOfSharesOutstanding>
<dialyShareholderFlowReported><dailyGrossSubscriptions>89560254.30</dailyGrossSubscriptions>
<dailyGrossRedemptions>7915.22</dailyGrossRedemptions><dailyShareHolderFlowDate>2026-10-29</dailyShareHolderFlowDate></dialyShareholderFlowReported>
<dialyShareholderFlowReported><dailyGrossSubscriptions>70227252.09</dailyGrossSubscriptions>
<dailyGrossRedemptions>144695.30</dailyGrossRedemptions><dailyShareHolderFlowDate>2026-10-01</dailyShareHolderFlowDate></dialyShareholderFlowReported>
</edgarSubmission>"""

OCT = "https://www.sec.gov/Archives/edgar/data/1786958/aoct/primary_doc.xml"

SUBMISSIONS = {"filings": {"recent": {
    "form": ["N-MFP3", "N-MFP3/A", "N-MFP3", "485BPOS"],
    "reportDate": ["2026-10-31", "2026-09-30", "2026-09-30", ""],
    "filingDate": ["2026-11-06", "2026-10-20", "2026-10-06", "2026-10-01"],
    "accessionNumber": ["a-oct", "a-sep-amend", "a-sep", "x"],
    "primaryDocument": ["xslN-MFP3_X01/primary_doc.xml"] * 4}}}


class SecTest(unittest.TestCase):
    def test_parses_shares_net_assets_and_daily_flows_in_date_order(self):
        e = sec.parse_nmfp3(XML, "S000067043")
        self.assertEqual((e["report_date"], e["shares_outstanding"]), ("2026-10-31", "833590155.0700"))
        self.assertEqual([f["date"] for f in e["daily_flows"]], ["2026-10-01", "2026-10-29"])
        self.assertEqual(sec.monthly_flows(e), (Decimal("159787506.39"), Decimal("152610.52")))

    def test_refuses_a_filing_for_another_series(self):
        with self.assertRaises(ValueError):
            sec.parse_nmfp3(XML, "S000000001")

    def test_amendment_wins_and_raw_xml_path_is_used(self):
        self.assertEqual(sec.filings(SUBMISSIONS), [("2026-10-31", "a-oct", "primary_doc.xml"), ("2026-09-30", "a-sep-amend", "primary_doc.xml")])

    def test_update_fetches_only_new_report_dates(self):
        calls = []

        def get(url, _agent):
            calls.append(url)
            return json.dumps(SUBMISSIONS) if "submissions" in url else XML.replace("2026-10-31", "2026-09-30") if "asepamend" in url else XML
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sec, "HERE", Path(tmp)), mock.patch.object(sec.time, "sleep"):
            (Path(tmp) / "official").mkdir()
            (Path(tmp) / "official" / "BENJI.json").write_text(json.dumps([{"report_date": "2026-10-31", "daily_flows": [], "source": OCT}]))
            out = sec.update("BENJI", {"cik": "0001786958", "series_id": "S000067043"}, "ua", get=get)
        self.assertEqual([e["report_date"] for e in out], ["2026-10-31", "2026-09-30"])
        self.assertEqual(len(calls), 2)  # submissions + the one missing filing

    def test_an_amendment_to_a_saved_month_replaces_it_and_says_so(self):
        def get(url, _agent):
            return json.dumps(SUBMISSIONS) if "submissions" in url else XML.replace("2026-10-31", "2026-09-30") if "asepamend" in url else XML
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sec, "HERE", Path(tmp)), mock.patch.object(sec.time, "sleep"):
            (Path(tmp) / "official").mkdir()
            saved = [{"report_date": "2026-10-31", "daily_flows": [], "source": OCT},
                     {"report_date": "2026-09-30", "daily_flows": [], "shares_outstanding": "1", "source": "https://www.sec.gov/old-sep"}]
            (Path(tmp) / "official" / "BENJI.json").write_text(json.dumps(saved))
            with redirect_stdout(io.StringIO()) as out:
                entries = sec.update("BENJI", {"cik": "0001786958", "series_id": "S000067043"}, "ua", get=get)
        sep = next(e for e in entries if e["report_date"] == "2026-09-30")
        self.assertIn("asepamend", sep["source"])
        self.assertNotEqual(sep["shares_outstanding"], "1")
        self.assertIn("sec: BENJI 2026-09-30 amended", out.getvalue())


if __name__ == "__main__":
    unittest.main()
