import csv
import tempfile
import unittest
from pathlib import Path

import yaml

from csv_checksum.compare_jobs import load_compare_plan
from csv_checksum.jobs import load_jobs
from csv_checksum.normalizers import NormalizationError, build_normalizer
from csv_checksum.runner import run_compare, run_compare_all, run_generate_all, run_inspect


def norm(rule, value):
    return build_normalizer("c", rule).normalize(value)


class NormalizerTests(unittest.TestCase):
    def test_decimal(self):
        self.assertEqual(norm({"type": "decimal"}, "129690.0"), "129690")
        self.assertEqual(norm({"type": "decimal"}, "1E+2"), "100")
        self.assertEqual(norm({"type": "decimal", "scale": 2}, "0.10000000000000001"), "0.10")
        self.assertEqual(norm({"type": "decimal", "scale": 2}, "-0.001"), "0.00")

    def test_integer(self):
        self.assertEqual(norm({"type": "integer"}, " 42.000 "), "42")
        with self.assertRaises(NormalizationError):
            norm({"type": "integer"}, "4.5")

    def test_boolean_bit_vs_bool(self):
        for raw in ("1", "true", "TRUE", "True"):
            self.assertEqual(norm({"type": "boolean"}, raw), "true")
        for raw in ("0", "false", "FALSE"):
            self.assertEqual(norm({"type": "boolean"}, raw), "false")

    def test_date(self):
        rule = {"type": "date", "input_formats": ["%Y-%m-%d", "%m/%d/%Y"]}
        self.assertEqual(norm(rule, "6/4/2023"), "2023-06-04")
        self.assertEqual(norm(rule, "2023-06-04"), "2023-06-04")
        self.assertEqual(norm(rule, "2023-06-04 00:00:00.000"), "2023-06-04")

    def test_datetime_mssql_vs_bigquery(self):
        rule = {"type": "datetime", "precision": "seconds"}
        for raw in (
            "2023-06-04 10:15:30.123",          # MSSQL DATETIME
            "2023-06-04 10:15:30.1234567",      # MSSQL DATETIME2(7)
            "2023-06-04 10:15:30.123456 UTC",   # BigQuery TIMESTAMP
            "2023-06-04T10:15:30.123456Z",      # ISO
            "2023-06-04 15:45:30 +05:30",       # DATETIMEOFFSET
        ):
            self.assertEqual(norm(rule, raw), "2023-06-04 10:15:30", raw)
        self.assertEqual(norm({"type": "datetime", "precision": "milliseconds"}, "2023-06-04 10:15:30.1239"),
                         "2023-06-04 10:15:30.123")

    def test_string(self):
        self.assertEqual(norm({"type": "string", "case": "upper", "collapse_whitespace": True}, "  a   b "), "A B")


BASE_SETTINGS = {
    "primary_keys": ["id", "region"],
    "include_columns": "*",
    "exclude_columns": ["load_ts"],
    "column_rules": {"id": {"type": "integer"}, "active": {"type": "boolean"},
                     "amount": {"type": "decimal", "scale": 2}},
}


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def write(self, name, rows):
        p = self.dir / name
        with open(p, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
        return p

    def write_config(self, files, defaults=None):
        cfg = self.dir / "cfg.yaml"
        data = {"output_dir": "out", "files": files}
        if defaults is not None:
            data["defaults"] = defaults
        cfg.write_text(yaml.safe_dump(data, sort_keys=False))
        return cfg

    def read(self, path):
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def test_full_flow(self):
        self.write("onprem.csv", [
            ["id", "region", "active", "amount", "note", "load_ts"],
            ["1", "EU", "1", "10.5", "x", "2024-01-01"],
            ["2", "EU", "0", "NULL", "y", "2024-01-01"],
            ["3", "US", "1", "1", "z", "2024-01-01"],
            ["4", "US", "1", "1", "only on-prem", "2024-01-01"],
        ])
        # Different column order + header case, BigQuery-style values, one real change.
        self.write("cloud.csv", [
            ["LOAD_TS", "Note", "Amount", "Active", "Region", "ID"],
            ["2099-12-31", "x", "10.50", "true", "EU", "1.0"],
            ["2099-12-31", "y", "", "false", "EU", "2"],
            ["2099-12-31", "CHANGED", "1.00", "true", "US", "3"],
            ["2099-12-31", "only cloud", "1", "true", "US", "5"],
        ])
        cfg = self.write_config([
            {"input": "onprem.csv", **BASE_SETTINGS},
            {"input": "cloud.csv", "output": "custom/cloud_ck.csv", **BASE_SETTINGS},
        ])
        summary = self.dir / "summary.csv"
        self.assertEqual(run_generate_all(cfg, self.dir, summary_file=summary), 0)
        on_ck, cl_ck = self.dir / "out" / "onprem_checksum.csv", self.dir / "custom" / "cloud_ck.csv"
        with open(on_ck, newline="", encoding="utf-8") as fh:
            self.assertEqual(next(csv.reader(fh)), ["id", "region", "checksum"])  # PKs + checksum only
        self.assertEqual(len(self.read(on_ck)), 4)
        self.assertEqual([r["status"] for r in self.read(summary)], ["OK", "OK"])

        out = self.dir / "res.csv"
        self.assertEqual(run_compare(on_ck, cl_ck, out), 1)
        status = {(r["id"], r["region"]): r["status"] for r in self.read(out)}
        self.assertEqual(status, {
            ("1", "EU"): "MATCH",
            ("2", "EU"): "MATCH",
            ("3", "US"): "MISMATCH",
            ("4", "US"): "MISSING_IN_CLOUD",
            ("5", "US"): "MISSING_IN_ONPREM",
        })

        insp = self.dir / "insp.csv"
        self.assertEqual(run_inspect(cfg, self.dir, "onprem", insp, keys_file=out), 0)
        self.assertEqual(sorted(r["id"] for r in self.read(insp)), ["3", "4"])

    def test_failures_do_not_stop_other_files(self):
        self.write("good.csv", [["id", "region", "v"], ["1", "EU", "a"]])
        self.write("no_pk.csv", [["v"], ["a"]])
        cfg = self.write_config([
            {"input": "good.csv", **BASE_SETTINGS},
            {"input": "missing.csv", **BASE_SETTINGS},
            {"input": "no_pk.csv", **BASE_SETTINGS},
            {"input": "bad_rule.csv", **{**BASE_SETTINGS, "column_rules": {"v": {"type": "nope"}}}},
            {"input": "disabled.csv", "enabled": False, **BASE_SETTINGS},
        ])
        summary = self.dir / "summary.csv"
        self.assertEqual(run_generate_all(cfg, self.dir, summary_file=summary), 2)
        self.assertEqual([r["status"] for r in self.read(summary)], ["OK", "FAILED", "FAILED", "FAILED"])
        self.assertTrue((self.dir / "out" / "good_checksum.csv").exists())
        self.assertEqual(list((self.dir / "out").glob("*.tmp")), [])

    def test_only_selected_files(self):
        self.write("a.csv", [["id", "region", "v"], ["1", "EU", "a"]])
        self.write("b.csv", [["id", "region", "v"], ["1", "EU", "a"]])
        cfg = self.write_config([{"input": "a.csv", **BASE_SETTINGS}, {"input": "b.csv", **BASE_SETTINGS}])
        self.assertEqual(run_generate_all(cfg, self.dir, only=["b"]), 0)
        self.assertEqual(sorted(p.name for p in (self.dir / "out").iterdir()), ["b_checksum.csv"])
        ck = self.dir / "out" / "b_checksum.csv"
        self.assertEqual(run_compare(ck, ck, self.dir / "r.csv"), 0)

    def test_defaults_are_merged(self):
        cfg = self.write_config(
            [{"input": "a.csv", "primary_keys": ["id"], "column_rules": {"amount": {"type": "decimal"}},
              "null_handling": {"null_token": "#N"}}],
            defaults={"column_rules": {"id": {"type": "integer"}}, "null_handling": {"null_values": ["NA"]},
                      "hash_algorithm": "md5"},
        )
        job = load_jobs(cfg, self.dir)[0]
        self.assertIsNone(job.error)
        c = job.config
        self.assertEqual(set(c.column_rules), {"id", "amount"})
        self.assertEqual((c.null_token, c.null_values, c.hash_algorithm), ("#N", {"NA"}, "md5"))
        self.assertEqual(job.output, (self.dir / "out" / "a_checksum.csv").resolve())

    def test_duplicate_outputs_rejected(self):
        cfg = self.write_config([
            {"input": "x/a.csv", **BASE_SETTINGS},
            {"input": "y/a.csv", "name": "a2", **BASE_SETTINGS},
        ])
        jobs = load_jobs(cfg, self.dir)
        self.assertIsNone(jobs[0].error)
        self.assertIn("also written by entry #1", jobs[1].error)

    def test_duplicate_keys_collapsed(self):
        self.write("d.csv", [["id", "region", "v"], ["1", "EU", "a"], ["1", "EU", "b"], ["2", "EU", "c"]])
        self.write("s.csv", [["id", "region", "v"], ["1", "EU", "a"], ["2", "EU", "c"]])
        cfg = self.write_config([{"input": "d.csv", **BASE_SETTINGS}, {"input": "s.csv", **BASE_SETTINGS}])
        run_generate_all(cfg, self.dir)
        d_ck, s_ck = self.dir / "out" / "d_checksum.csv", self.dir / "out" / "s_checksum.csv"
        self.assertEqual(len(self.read(d_ck)), 2)
        self.assertEqual(run_compare(d_ck, s_ck, self.dir / "r.csv"), 1)  # duplicate shows as mismatch

    def test_unknown_setting_rejected(self):
        cfg = self.write_config([{"input": "a.csv", "primary_keys": ["id"], "include_colums": ["x"]}])
        self.assertIn("include_colums", load_jobs(cfg, self.dir)[0].error)

    def test_project_config_loads(self):
        root = Path(__file__).resolve().parents[1]
        jobs = load_jobs(root / "config" / "checksum_config.yaml", root)
        self.assertEqual([j.error for j in jobs], [None] * len(jobs))
        self.assertEqual(jobs[0].config.primary_keys, ["Employee_ID"])


class CompareAllTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "op").mkdir()
        (self.dir / "cl").mkdir()

    def checksum(self, folder, name, rows):
        with open(self.dir / folder / name, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows([["id", "checksum"]] + rows)

    def config(self, comparisons, auto=False):
        cfg = self.dir / "cmp.yaml"
        cfg.write_text(yaml.safe_dump({
            "source_dir": "op", "target_dir": "cl", "result_dir": "res",
            "auto_match_same_names": auto, "comparisons": comparisons,
        }))
        return cfg

    def read(self, path):
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def test_explicit_mapping_with_different_names(self):
        self.checksum("op", "A_onprem.csv", [["1", "x"], ["2", "y"]])
        self.checksum("cl", "a_bq.csv", [["1", "x"], ["2", "y"]])
        cfg = self.config([{"name": "a", "source": "A_onprem.csv", "target": "a_bq.csv"}])
        summary = self.dir / "s.csv"
        self.assertEqual(run_compare_all(cfg, self.dir, summary_file=summary), 0)
        self.assertEqual([r["status"] for r in self.read(self.dir / "res" / "a_comparison.csv")], ["MATCH", "MATCH"])
        row = self.read(summary)[0]
        self.assertEqual((row["status"], row["match"], row["mismatch"]), ("ALL_MATCH", "2", "0"))

    def test_auto_match_failures_and_unpaired(self):
        self.checksum("op", "same.csv", [["1", "x"], ["2", "y"]])
        self.checksum("cl", "SAME.csv", [["1", "x"], ["2", "CHANGED"], ["3", "z"]])
        self.checksum("op", "listed.csv", [["1", "x"]])
        self.checksum("cl", "listed_cloud.csv", [["1", "x"]])
        self.checksum("op", "only_onprem.csv", [["1", "x"]])
        self.checksum("cl", "only_cloud.csv", [["1", "x"]])
        cfg = self.config([
            {"name": "listed", "source": "listed.csv", "target": "listed_cloud.csv"},
            {"name": "broken", "source": "does_not_exist.csv", "target": "listed_cloud.csv"},
        ], auto=True)
        summary = self.dir / "s.csv"
        self.assertEqual(run_compare_all(cfg, self.dir, summary_file=summary), 2)  # one pair FAILED
        status = {r["name"]: r["status"] for r in self.read(summary)}
        self.assertEqual(status, {
            "listed": "ALL_MATCH",
            "broken": "FAILED",
            "same": "DIFFERENCES",
            "only_onprem": "NO_CLOUD_FILE",
            "only_cloud": "NO_ONPREM_FILE",
        })
        res = {r["id"]: r["status"] for r in self.read(self.dir / "res" / "same_comparison.csv")}
        self.assertEqual(res, {"1": "MATCH", "2": "MISMATCH", "3": "MISSING_IN_ONPREM"})

    def test_differences_exit_code_and_only(self):
        self.checksum("op", "a.csv", [["1", "x"]])
        self.checksum("cl", "a.csv", [["1", "y"]])
        self.checksum("op", "b.csv", [["1", "x"]])
        self.checksum("cl", "b.csv", [["1", "x"]])
        cfg = self.config([{"source": "a.csv", "target": "a.csv"}, {"source": "b.csv", "target": "b.csv"}])
        self.assertEqual(run_compare_all(cfg, self.dir), 1)
        self.assertEqual(run_compare_all(cfg, self.dir, only=["b"]), 0)

    def test_bad_config(self):
        cfg = self.config([{"source": "a.csv"}, {"source": "b.csv", "target": "b.csv", "colour": "red"}])
        plan = load_compare_plan(cfg, self.dir)
        self.assertIn("'target' is required", plan.pairs[0].error)
        self.assertIn("colour", plan.pairs[1].error)

    def test_project_compare_config_loads(self):
        root = Path(__file__).resolve().parents[1]
        plan = load_compare_plan(root / "config" / "compare_config.yaml", root)
        self.assertEqual(plan.pairs[0].name, "employee_records")
        self.assertIsNone(plan.pairs[0].error)


if __name__ == "__main__":
    unittest.main()
