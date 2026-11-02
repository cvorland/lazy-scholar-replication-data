"""Offline tests for source validation, indexing, publication, and saved state."""
import csv
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import pipeline as p

COMMIT = "a" * 40


def row(record="FRED-1", **values):
    result = dict.fromkeys(sorted(p.CSV_FIELDS | {"alt_identifier_o", "alt_identifier_r"}), "")
    result.update(flora_id=record, id="FLORA-000001", doi_o="10.1234/original", doi_r="10.1234/report",
                  type="replication", title_o="Original title", title_r="Report title",
                  author_r='[{"given":"Jane","family":"Doe"}]', year_r="2020.0",
                  outcome="Replicated", source="FReD")
    result.update(values)
    return result


def source(rows=None):
    rows = rows if rows is not None else [row()]
    stream = io.StringIO(newline="")
    columns = list(rows[0]) if rows else sorted(p.CSV_FIELDS)
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    body = stream.getvalue().encode()
    manifest = {"schema_version": 1, "filename": "flora.csv", "sha256": p.sha256(body),
                "bytes": len(body), "rows": len(rows), "columns": columns,
                "generated_at": "2026-10-30T03:08:30+00:00", "published": False, "version": None}
    return body, manifest


class ConversionTests(unittest.TestCase):
    def test_doi_normalization_and_preserved_suffix_punctuation(self):
        self.assertEqual(p.normalize_doi(" HTTPS://DX.DOI.ORG/10.1234%2FABC.(2) "), "10.1234/abc.(2)")
        self.assertEqual(p.normalize_doi("DOI: 10.1234/ABC;"), "10.1234/abc;")
        self.assertEqual(p.normalize_doi("10.1234/A%2FB"), "10.1234/a%2fb")
        self.assertEqual(p.normalize_doi("https://doi.org/10.1234/A%252FB"), "10.1234/a%2fb")
        self.assertIsNone(p.normalize_doi("NA"))
        for value in ("10.12/x", "10.1234/x y", "javascript:10.1234/x", "https://evil.test/10.1234/x",
                      "https://doi.org/10.1234/x%ZZ", "https://doi.org/10.1234/x%20"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                p.normalize_doi(value)

    def test_records_roles_dedup_and_reproduction_dimensions(self):
        body, manifest = source([
            row("REPRO-2", doi_o="https://doi.org/10.1234/ORIGINAL", type="reproduction",
                outcome="NA", outcome_computation="Partially reproduced", outcome_computational_quote="A quote",
                out_quote_computational_source="Table 2", outcome_robustness="Not robust"),
            row("FRED-1", doi_r="doi:10.1234/REPORT", outcome_quote='A comma, and a "quote"\nwith a new line'),
            row("REPL-3", doi_o="10.1234/report", doi_r="10.1234/later", title_o=""),
        ])
        snapshot = p.convert(body, manifest, COMMIT)
        self.assertEqual(len(snapshot["papers"]), 3)
        self.assertEqual(snapshot["byDoi"]["10.1234/original"]["asOriginal"], ["FRED-1", "REPRO-2"])
        self.assertEqual(snapshot["byDoi"]["10.1234/report"], {"asOriginal": ["REPL-3"], "asReport": ["FRED-1", "REPRO-2"]})
        rec = snapshot["records"]["REPRO-2"]
        self.assertEqual(rec["type"], "reproduction")
        self.assertIsNone(rec["outcome"]["label"])
        self.assertEqual(rec["computational"], {"label": "Partially reproduced", "quote": "A quote", "quoteSource": "Table 2"})
        self.assertEqual(rec["robustness"]["label"], "Not robust")
        report = snapshot["papers"][rec["report"]]
        self.assertEqual(report["authors"], "Jane Doe")
        self.assertEqual(report["year"], 2020)
        self.assertIn("\n", snapshot["records"]["FRED-1"]["outcome"]["quote"])

    def test_no_doi_report_is_retained_and_not_fabricated(self):
        body, manifest = source([row(doi_r="escholarship.org/uc/item/xyz", url_r="https://escholarship.org/uc/item/xyz")])
        snapshot = p.convert(body, manifest, COMMIT)
        report = snapshot["papers"][snapshot["records"]["FRED-1"]["report"]]
        self.assertIsNone(report["doi"])
        self.assertEqual(report["altId"], "escholarship.org/uc/item/xyz")
        self.assertEqual(list(snapshot["byDoi"]), ["10.1234/original"])

    def test_url_sanitization(self):
        for value in ("javascript:alert(1)", "https://user:pass@example.org", "https://example.org/has space"):
            self.assertIsNone(p.safe_url(value))
        self.assertEqual(p.safe_url("https://example.org/report"), "https://example.org/report")

    def test_deterministic_bytes_and_pinned_provenance(self):
        body, manifest = source()
        first = p.convert(body, manifest, COMMIT)
        self.assertEqual(p.canonical_json(first), p.canonical_json(p.convert(body, manifest, COMMIT)))
        self.assertIn(COMMIT, first["source"]["csvUrl"])
        self.assertEqual(first["source"]["csvSha256"], p.sha256(body))
        self.assertEqual(first["records"]["FRED-1"]["provenance"]["sourceId"], "FLORA-000001")
        self.assertEqual(first["source"]["license"], "CC-BY-4.0")
        changed, changed_manifest = source([row(outcome="Not replicated")])
        self.assertNotEqual(first["datasetVersion"], p.convert(changed, changed_manifest, COMMIT)["datasetVersion"])

    def test_source_hash_size_schema_columns_count_and_record_validation(self):
        body, manifest = source()
        for key, value in (("sha256", "0" * 64), ("bytes", 2), ("schema_version", 2),
                           ("filename", "other.csv"), ("columns", []), ("rows", 999)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                p.convert(body, {**manifest, key: value}, COMMIT)
        for rows in ([], [row(), row()], [row(type="unknown")], [row(record="")]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                p.convert(*source(rows), COMMIT)

    def test_manifest_pins_immutable_asset_and_checks_full_bytes(self):
        snapshot = p.convert(*source(), COMMIT)
        payload = p.canonical_json(snapshot)
        manifest = p.release_manifest(snapshot, payload, "owner/data")
        self.assertEqual(manifest["bytes"], len(payload))
        self.assertEqual(manifest["sha256"], p.sha256(payload))
        self.assertEqual(manifest["recordCount"], 1)
        self.assertIn("/releases/download/dataset-" + snapshot["datasetVersion"] + "/", manifest["url"])
        self.assertNotIn("latest", manifest["url"])

    def test_client_limits_reject_invalid_dates_and_oversized_text(self):
        body, manifest = source()
        for timestamp in ("not-a-date", "2026-10-30", "2026-13-30T00:00:00Z"):
            with self.subTest(timestamp=timestamp), self.assertRaisesRegex(ValueError, "ISO timestamp"):
                p.convert(body, {**manifest, "generated_at": timestamp}, COMMIT)
        for fields in ({"title_r": "a" * 20001}, {"outcome_quote": "😀" * 10001}):
            with self.subTest(fields=list(fields)), self.assertRaisesRegex(ValueError, "20,000"):
                p.convert(*source([row(**fields)]), COMMIT)
        snapshot = p.convert(*source([row(year_r="0000")]), COMMIT)
        self.assertIsNone(snapshot["papers"][snapshot["records"]["FRED-1"]["report"]]["year"])


class FakeGithub:
    def __init__(self, release=None, corrupt=False):
        self.release = release
        self.corrupt = corrupt
        self.assets = {}
        self.events = []

    def find_release(self, tag):
        return self.release

    def asset_bytes(self, asset_id):
        self.events.append(("verify", asset_id))
        return b"corrupt" if self.corrupt else self.assets[asset_id]

    def json(self, path, **kwargs):
        if kwargs.get("method") == "PATCH":
            self.events.append(("publish", path))
            self.release["draft"] = False
            return self.release
        if path == "releases":
            self.events.append(("draft", path))
            self.release = {"id": 1, "draft": True, "assets": []}
            return self.release
        asset_id = len(self.assets) + 1
        self.assets[asset_id] = kwargs["binary"]
        asset = {"id": asset_id, "name": path.split("name=")[1], "size": len(kwargs["binary"])}
        self.release["assets"].append(asset)
        self.events.append(("upload", asset_id))
        return asset


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.snapshot = p.convert(*source(), COMMIT)
        (self.directory / "forrt-snapshot.json").write_bytes(p.canonical_json(self.snapshot))

    def test_verifies_all_three_draft_assets_before_publishing(self):
        github = FakeGithub()
        with patch.object(p, "Github", return_value=github):
            p.publish(self.directory, "owner/data", "test-only")
            self.assertEqual([e[0] for e in github.events], ["draft", "upload", "verify", "upload", "verify", "upload", "verify", "publish"])
            github.events.clear()
            p.publish(self.directory, "owner/data", "test-only")
            self.assertEqual([e[0] for e in github.events], ["verify", "verify", "verify"])

    def test_corrupt_upload_keeps_release_draft(self):
        github = FakeGithub(corrupt=True)
        with patch.object(p, "Github", return_value=github), self.assertRaisesRegex(ValueError, "byte verification"):
            p.publish(self.directory, "owner/data", "test-only")
        self.assertTrue(github.release["draft"])
        self.assertNotIn("publish", [e[0] for e in github.events])

    def test_draft_discovery_paginates_when_published_tag_lookup_returns_404(self):
        github = p.Github("owner/data", "test-only")
        draft = {"tag_name": "dataset-wanted", "draft": True, "id": 42}
        missing = HTTPError("https://api.github.com/example", 404, "Not Found", {}, None)
        with patch.object(github, "json", side_effect=[missing, [{"tag_name": "other"}] * 100, [draft]]) as request:
            self.assertIs(github.find_release("dataset-wanted"), draft)
        self.assertEqual([call.args[0] for call in request.call_args_list], [
            "releases/tags/dataset-wanted", "releases?per_page=100&page=1", "releases?per_page=100&page=2"])

    def test_existing_draft_reuses_verified_assets_and_completes_publication(self):
        payload = (self.directory / "forrt-snapshot.json").read_bytes()
        github = FakeGithub({"id": 1, "draft": True, "assets": [{"id": 1, "name": "forrt-snapshot.json", "size": len(payload)}]})
        github.assets[1] = payload
        with patch.object(p, "Github", return_value=github):
            p.publish(self.directory, "owner/data", "test-only")
        self.assertEqual([e[0] for e in github.events], ["verify", "upload", "verify", "upload", "verify", "publish"])

    def test_tampered_payload_rejected_before_any_github_request(self):
        self.snapshot["records"]["FRED-1"]["outcome"]["label"] = "changed"
        (self.directory / "forrt-snapshot.json").write_bytes(p.canonical_json(self.snapshot))
        with patch.object(p, "Github") as github, self.assertRaisesRegex(ValueError, "dataset version"):
            p.publish(self.directory, "owner/data", "test-only")
        github.assert_not_called()


class StateTests(unittest.TestCase):
    def test_unchanged_build_skips_source_download_and_thirty_day_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state_path = base / "state.json"
            output = base / "dist"
            body, manifest = source()
            with patch.object(p, "download", side_effect=[p.canonical_json(manifest), body]):
                p.build(output, "owner/data", COMMIT, state_path)
            self.assertFalse(state_path.exists(), "Build must not advance publication state")
            now = datetime(2026, 11, 1, tzinfo=timezone.utc)
            self.assertTrue(p.record_success(state_path, output, now))
            original_state = state_path.read_bytes()
            with patch.object(p, "download") as download:
                self.assertIsNone(p.build(output, "owner/data", COMMIT, state_path))
            download.assert_not_called()
            self.assertFalse(p.record_success(state_path, output, now + timedelta(days=29)))
            self.assertEqual(original_state, state_path.read_bytes())
            self.assertTrue(p.record_success(state_path, output, now + timedelta(days=30)))
            self.assertNotEqual(original_state, state_path.read_bytes())

    def test_changed_converter_rebuilds_same_data_version(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state_path = base / "state.json"
            state_path.write_bytes(p.canonical_json({"sourceCommit": COMMIT, "converterSha256": "old", "datasetVersion": "old"}))
            body, manifest = source()
            with patch.object(p, "download", side_effect=[p.canonical_json(manifest), body]) as download:
                result = p.build(base / "dist", "owner/data", COMMIT, state_path)
            self.assertEqual(download.call_count, 2)
            self.assertEqual(result["datasetVersion"], p.convert(body, manifest, COMMIT)["datasetVersion"])


if __name__ == "__main__":
    unittest.main()
