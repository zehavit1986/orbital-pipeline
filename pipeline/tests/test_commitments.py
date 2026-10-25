import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

import openpyxl

from orbital_pipeline.commitments_workbook import (
    ContractTerms, WorkbookLayoutError, parse_amount, parse_date, read_workbook,
    snapshot_date_from_filename,
)
from orbital_pipeline.ingest_commitments import build_contract_history

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "commitments"


def terms(customer="C001", start=date(2026, 1, 1), end=date(2026, 12, 31), amount="1000", status="Active"):
    return ContractTerms(customer, "Name", start, end, Decimal(amount), status)


class ProvidedWorkbooksTest(unittest.TestCase):
    """The three real snapshots, each with a different layout."""

    def read(self, name):
        return {row.contract_id: row for row in read_workbook(DATA_DIR / name)}

    def test_july_layout(self):
        rows = self.read("commitments_2026-07-31.xlsx")
        self.assertEqual(len(rows), 9)  # TOTAL row skipped
        self.assertEqual(rows["K-1001"].terms.commitment_usd, Decimal("1200000"))

    def test_august_text_values(self):
        rows = self.read("commitments_2026-08-31.xlsx")
        self.assertEqual(rows["K-1005"].terms.commitment_usd, Decimal("900000.00"))  # '$900,000.00'
        self.assertEqual(rows["K-1007"].terms.start_date, date(2026, 6, 1))           # '1 Jun 2026'
        self.assertEqual(rows["K-1009"].terms.end_date, date(2027, 1, 31))            # amendment

    def test_september_extra_sheet_padding_and_duplicates(self):
        rows = self.read("commitments_2026-09-28.xlsx")
        self.assertIn("K-1002", rows)            # ' K-1002 ' trimmed
        self.assertNotIn("K-1004", rows)         # removed by Finance
        self.assertEqual(len(rows), 10)          # duplicate K-1008 counted once
        self.assertEqual(rows["K-1011"].terms.customer_id, "C003")

    def test_snapshot_date_from_filename(self):
        self.assertEqual(snapshot_date_from_filename(Path("commitments_2026-09-28.xlsx")), date(2026, 9, 28))
        self.assertIsNone(snapshot_date_from_filename(Path("~$commitments_2026-09-28.xlsx")))


class LayoutChangesTest(unittest.TestCase):
    def write_workbook(self, header, row):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["Some title"])
        sheet.append(header)
        sheet.append(row)
        path = Path(tempfile.mkdtemp()) / "commitments_2026-10-31.xlsx"
        workbook.save(path)
        return path

    def test_reordered_and_renamed_columns(self):
        path = self.write_workbook(
            ["Commitment", "End", "Start", "Cust ID", "Contract No"],
            [500, date(2026, 12, 31), date(2026, 1, 1), "c001", "K-2000"])
        [row] = read_workbook(path)
        self.assertEqual(row.contract_id, "K-2000")
        self.assertEqual(row.terms.customer_id, "C001")
        self.assertEqual(row.terms.commitment_usd, Decimal("500"))

    def test_unknown_column_name_fails_loudly(self):
        path = self.write_workbook(
            ["Contract ID", "Customer ID", "Start Date", "End Date", "Contract Value"],
            ["K-2000", "C001", date(2026, 1, 1), date(2026, 12, 31), 500])
        with self.assertRaises(WorkbookLayoutError) as error:
            read_workbook(path)
        self.assertIn("Contract Value", str(error.exception))

    def test_row_with_dates_but_bad_id_fails(self):
        path = self.write_workbook(
            ["Contract ID", "Customer ID", "Start Date", "End Date", "Commit (USD)"],
            ["K2000", "C001", date(2026, 1, 1), date(2026, 12, 31), 500])
        with self.assertRaises(ValueError):
            read_workbook(path)


class ValueParsingTest(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(parse_date("1 Jun 2026"), date(2026, 6, 1))
        self.assertEqual(parse_date("2026-06-01"), date(2026, 6, 1))
        self.assertEqual(parse_date(46174), date(2026, 6, 1))  # Excel serial number
        with self.assertRaises(ValueError):
            parse_date("next Tuesday")

    def test_amounts(self):
        self.assertEqual(parse_amount("$900,000.00"), Decimal("900000.00"))
        self.assertEqual(parse_amount("900000 USD"), Decimal("900000"))
        with self.assertRaises(ValueError):
            parse_amount(0)


class ContractHistoryTest(unittest.TestCase):
    def test_unchanged_contract_has_one_open_version(self):
        history = build_contract_history({date(2026, 7, 31): {"K-1": terms()},
                                          date(2026, 8, 31): {"K-1": terms()}})
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].valid_from, date(2026, 7, 31))
        self.assertIsNone(history[0].valid_to)

    def test_amendment_closes_old_version(self):
        history = build_contract_history({date(2026, 7, 31): {"K-1": terms(amount="1000")},
                                          date(2026, 8, 31): {"K-1": terms(amount="1500")}})
        old, new = history
        self.assertEqual((old.version, old.valid_to), (1, date(2026, 8, 31)))
        self.assertEqual((new.version, new.valid_to, new.terms.commitment_usd), (2, None, Decimal("1500")))

    def test_removed_contract_is_kept_and_closed(self):
        history = build_contract_history({date(2026, 7, 31): {"K-1": terms(), "K-2": terms("C002")},
                                          date(2026, 8, 31): {"K-1": terms()}})
        removed = [v for v in history if v.contract_id == "K-2"]
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0].valid_to, date(2026, 8, 31))

    def test_contract_that_comes_back_gets_a_new_version(self):
        history = build_contract_history({date(2026, 7, 31): {"K-1": terms()},
                                          date(2026, 8, 31): {},
                                          date(2026, 9, 30): {"K-1": terms()}})
        self.assertEqual([(v.version, v.valid_from, v.valid_to) for v in history],
                         [(1, date(2026, 7, 31), date(2026, 8, 31)), (2, date(2026, 9, 30), None)])


if __name__ == "__main__":
    unittest.main()
