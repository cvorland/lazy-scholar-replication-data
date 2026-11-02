#!/usr/bin/env python3
"""Build and publish Lazy Scholar's FORRT index. Python standard library only."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

UPSTREAM = "forrtproject/flora-validation"
CSV_PATH = "output/flora.csv"
MANIFEST_PATH = "output/flora_release_manifest.json"
SCHEMA_VERSION = 1
MAX_SOURCE_BYTES = 50 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 20 * 1024 * 1024
MAX_RECORDS = 100_000
CSV_FIELDS = {
    "flora_id", "id", "doi_o", "doi_r", "title_o", "title_r", "author_o", "author_r",
    "year_o", "year_r", "journal_o", "journal_r", "url_o", "url_r", "type",
    "outcome", "outcome_quote", "outcome_quote_source", "outcome_computation",
    "outcome_computational_quote", "out_quote_computational_source", "outcome_robustness",
    "outcome_robustness_quote", "out_quote_robust_source", "source", "source_display_id",
    "source_record_id",
}


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def text(value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return None if value in ("", "NA") else value


def normalize_doi(value) -> str | None:
    value = text(value)
    if value is None:
        return None
    url_match = re.match(r"^https?://(?:dx\.)?doi\.org/", value, flags=re.I)
    if url_match:
        value = value[url_match.end():]
        if re.search(r"%(?![0-9a-fA-F]{2})", value):
            raise ValueError("Malformed DOI URL percent escape")
        value = unquote(value, encoding="utf-8", errors="strict")
    value = re.sub(r"^doi:\s*", "", value, flags=re.I).lower()
    if not re.fullmatch(r"10\.\d{4,9}/\S+", value):
        raise ValueError(f"Malformed DOI: {value!r}")
    return value


def safe_url(value) -> str | None:
    value = text(value)
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password:
            if not re.search(r"[\x00-\x20]", value):
                return value
    except ValueError:
        pass
    return None


def authors(value) -> str | None:
    value = text(value)
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return value
    if isinstance(parsed, str):
        return text(parsed)
    if isinstance(parsed, list):
        names = []
        for item in parsed:
            if isinstance(item, str):
                name = text(item)
            elif isinstance(item, dict):
                name = text(" ".join(filter(None, [text(item.get("given")), text(item.get("family"))]))) or text(item.get("name"))
            else:
                name = None
            if name:
                names.append(name)
        return "; ".join(names) or None
    return value


def year(value) -> int | None:
    value = text(value)
    if value and re.fullmatch(r"\d{4}(?:\.0+)?", value):
        parsed = int(float(value))
        return parsed if 0 < parsed < 10000 else None
    return None


def outcome(row, label, quotation, source):
    return {"label": text(row.get(label)), "quote": text(row.get(quotation)), "quoteSource": text(row.get(source))}


def source_url(commit: str, path: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Expected a full upstream Git commit SHA")
    return f"https://raw.githubusercontent.com/{UPSTREAM}/{commit}/{path}"


def verify_source(csv_bytes: bytes, manifest: dict) -> None:
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported upstream manifest schema")
    if manifest.get("filename") != "flora.csv":
        raise ValueError("Unexpected upstream filename")
    if type(manifest.get("bytes")) is not int or manifest["bytes"] != len(csv_bytes):
        raise ValueError("Upstream byte length mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("sha256", ""))) or sha256(csv_bytes) != manifest["sha256"]:
        raise ValueError("Upstream SHA-256 mismatch")
    if not isinstance(manifest.get("generated_at"), str) or not manifest["generated_at"]:
        raise ValueError("Upstream generation date is missing")
    try:
        timestamp = datetime.fromisoformat(manifest["generated_at"].replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("Timezone is missing")
    except ValueError as error:
        raise ValueError("Upstream generation date must be an ISO timestamp with timezone") from error


def convert(csv_bytes: bytes, manifest: dict, commit: str) -> dict:
    verify_source(csv_bytes, manifest)
    pinned_url = source_url(commit, CSV_PATH)
    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"), newline=""))
    header = reader.fieldnames or []
    if len(header) != len(set(header)) or not CSV_FIELDS.issubset(header):
        raise ValueError(f"Invalid CSV columns; missing {sorted(CSV_FIELDS - set(header))}")
    if manifest.get("columns") != header:
        raise ValueError("CSV columns differ from the source manifest")
    rows = list(reader)
    if not rows or len(rows) > MAX_RECORDS or manifest.get("rows") != len(rows):
        raise ValueError("Invalid source record count")
    if any(None in row or any(v is None for v in row.values()) for row in rows):
        raise ValueError("Malformed CSV row width")

    papers, records, by_doi = {}, {}, {}
    for row in sorted(rows, key=lambda r: r["flora_id"]):
        record_id = text(row["flora_id"])
        if not record_id or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,30}-\d{1,12}", record_id):
            raise ValueError(f"Missing or malformed flora_id: {record_id!r}")
        if record_id in records:
            raise ValueError(f"Duplicate flora_id: {record_id}")
        kind = (text(row["type"]) or "").lower()
        if kind not in ("replication", "reproduction"):
            raise ValueError(f"Unknown type for {record_id}: {kind!r}")
        links = {}
        for side, role in (("o", "original"), ("r", "report")):
            raw_doi = text(row[f"doi_{side}"])
            try:
                doi = normalize_doi(raw_doi)
            except (ValueError, UnicodeError):
                # Upstream sometimes puts a repository identifier in a DOI
                # cell. Retain the report, but never index a guessed DOI.
                doi = None
            url = safe_url(row[f"url_{side}"])
            alt_id = text(row.get(f"alt_identifier_{side}")) or (raw_doi if raw_doi and not doi else None)
            identity = f"doi:{doi}" if doi else f"url:{url}" if url else f"alt:{alt_id}" if alt_id else f"record:{record_id}:{role}"
            paper_id = "p-" + sha256(identity.encode("utf-8"))[:24]
            paper = {
                "doi": doi, "title": text(row[f"title_{side}"]), "authors": authors(row[f"author_{side}"]),
                "year": year(row[f"year_{side}"]), "journal": text(row[f"journal_{side}"]), "url": url,
            }
            if alt_id:
                paper["altId"] = alt_id
            if paper_id not in papers:
                papers[paper_id] = paper
            else:
                # Stable record ordering makes metadata choice deterministic;
                # enrich missing fields without overwriting an existing value.
                for field, value in paper.items():
                    if not papers[paper_id].get(field) and value is not None:
                        papers[paper_id][field] = value
            links[role] = paper_id
            if doi:
                by_doi.setdefault(doi, {"asOriginal": [], "asReport": []})["asOriginal" if role == "original" else "asReport"].append(record_id)
        records[record_id] = {
            **links, "type": kind,
            "outcome": outcome(row, "outcome", "outcome_quote", "outcome_quote_source"),
            "computational": outcome(row, "outcome_computation", "outcome_computational_quote", "out_quote_computational_source"),
            "robustness": outcome(row, "outcome_robustness", "outcome_robustness_quote", "out_quote_robust_source"),
            "provenance": {
                "sourceId": text(row["id"]), "source": text(row["source"]),
                "sourceDisplayId": text(row["source_display_id"]), "sourceRecordId": text(row["source_record_id"]),
            },
        }

    snapshot = {
        "schemaVersion": SCHEMA_VERSION,
        "source": {
            "name": "FORRT / FLoRA", "csvUrl": pinned_url, "commit": commit,
            "csvSha256": manifest["sha256"], "generatedAt": manifest["generated_at"],
            "license": "CC-BY-4.0", "licenseUrl": "https://creativecommons.org/licenses/by/4.0/",
            "citationUrl": "https://doi.org/10.17605/OSF.IO/9R62X",
        },
        "papers": papers, "records": records, "byDoi": by_doi,
    }
    # Match the extension's 20,000 UTF-16-code-unit text limit. Fail rather
    # than publish a snapshot the client would reject or silently truncate.
    emitted_text = [paper[field] for paper in papers.values() for field in ("title", "authors", "journal", "url")]
    emitted_text.extend(value for record in records.values() for axis in ("outcome", "computational", "robustness") for value in record[axis].values())
    if any(value is not None and len(value.encode("utf-16-le")) // 2 > 20000 for value in emitted_text):
        raise ValueError("Generated text exceeds the client's 20,000-character limit")
    # No wall-clock build timestamp participates in the version or output.
    snapshot["datasetVersion"] = f"v{SCHEMA_VERSION}-" + sha256(canonical_json(snapshot))[:24]
    if len(canonical_json(snapshot)) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Generated snapshot exceeds the client's 20 MiB limit")
    return snapshot


def attribution(snapshot: dict) -> str:
    source = snapshot["source"]
    return (
        "FORRT / FLoRA replication and reproduction data\n\n"
        "Source: Wallrich, L., Röseler, L., et al. (2026).\n"
        "FORRT Library of Replication Attempts (FLoRA) [Data set].\n"
        "Full citation and authors: https://forrt.org/replication-hub/flora/\n"
        f"Dataset citation: {source['citationUrl']}\n"
        "License: Creative Commons Attribution 4.0 International (CC BY 4.0)\n"
        f"License text: {source['licenseUrl']}\n\n"
        "Changes by Lazy Scholar: selected fields, normalized DOI keys and author\n"
        "display strings, deduplicated paper metadata, and indexed relationship records.\n"
        "Outcome labels and supporting quotations are supplied by FORRT / FLoRA.\n"
        "Inclusion is not a general reliability rating; absence is not evidence that\n"
        "a paper has never been replicated. FORRT does not endorse Lazy Scholar.\n\n"
        f"Source CSV: {source['csvUrl']}\n"
        f"Source commit: {source['commit']}\n"
        f"Source SHA-256: {source['csvSha256']}\n"
        f"Source snapshot generated: {source['generatedAt']}\n"
        f"Lazy Scholar dataset version: {snapshot['datasetVersion']}\n"
    )


def release_manifest(snapshot: dict, payload: bytes, repository: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Repository must be OWNER/NAME")
    tag = "dataset-" + snapshot["datasetVersion"]
    return {
        "schemaVersion": SCHEMA_VERSION, "datasetVersion": snapshot["datasetVersion"],
        "sourceGeneratedAt": snapshot["source"]["generatedAt"],
        "sha256": sha256(payload), "bytes": len(payload), "recordCount": len(snapshot["records"]),
        "url": f"https://github.com/{repository}/releases/download/{tag}/forrt-snapshot.json",
    }


def read_limited(response, limit: int) -> bytes:
    body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError("Download exceeds the configured byte limit")
    return body


def download(url: str, limit: int = MAX_SOURCE_BYTES) -> bytes:
    request = Request(url, headers={"User-Agent": "Lazy-Scholar-FLoRA-data-pipeline", "Accept": "application/json,text/csv,*/*"})
    with urlopen(request, timeout=60) as response:
        return read_limited(response, limit)


def build(output: Path, repository: str | None = None, commit: str | None = None, state_path: Path | None = None) -> dict | None:
    if not commit:
        # A commit touching CSV, rather than unrelated branch-head changes,
        # supplies stable provenance when the upstream data has not changed.
        commits = json.loads(download(f"https://api.github.com/repos/{UPSTREAM}/commits?path={quote(CSV_PATH)}&per_page=1", 1024 * 1024))
        commit = commits[0]["sha"]
    source_url(commit, CSV_PATH)  # validate before using an optional saved state
    converter_hash = sha256(Path(__file__).read_bytes())
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path and state_path.exists() else {}
    output.mkdir(parents=True, exist_ok=True)
    if state.get("sourceCommit") == commit and state.get("converterSha256") == converter_hash and state.get("datasetVersion"):
        (output / "build-status.json").write_bytes(canonical_json({"changed": False}))
        print("Upstream CSV and converter unchanged; no CSV downloaded")
        return None
    manifest = json.loads(download(source_url(commit, MANIFEST_PATH), 64 * 1024))
    csv_bytes = download(source_url(commit, CSV_PATH))
    snapshot = convert(csv_bytes, manifest, commit)
    payload = canonical_json(snapshot)
    (output / "forrt-snapshot.json").write_bytes(payload)
    (output / "forrt-ATTRIBUTION.txt").write_text(attribution(snapshot), encoding="utf-8", newline="\n")
    if repository:
        (output / "manifest.json").write_bytes(canonical_json(release_manifest(snapshot, payload, repository)))
    (output / "build-status.json").write_bytes(canonical_json({"changed": True}))
    (output / "pipeline-state.json").write_bytes(canonical_json({
        "sourceCommit": commit, "converterSha256": converter_hash, "datasetVersion": snapshot["datasetVersion"],
    }))
    print(json.dumps({"datasetVersion": snapshot["datasetVersion"], "records": len(snapshot["records"]), "papers": len(snapshot["papers"]), "dois": len(snapshot["byDoi"]), "bytes": len(payload), "commit": commit}))
    return snapshot


def record_success(state_path: Path, output: Path, now: datetime | None = None) -> bool:
    """Called only after a successful check and any required publication."""
    now = now or datetime.now(timezone.utc)
    previous = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    status = json.loads((output / "build-status.json").read_bytes())
    state = json.loads((output / "pipeline-state.json").read_bytes()) if status.get("changed") else dict(previous)
    if not state.get("sourceCommit") or not state.get("datasetVersion"):
        raise ValueError("No successfully built or previously published state")
    last = previous.get("lastHeartbeatAt")
    try:
        heartbeat_due = not last or now - datetime.fromisoformat(last) >= timedelta(days=30)
    except (ValueError, TypeError):
        heartbeat_due = True
    if status.get("changed") or heartbeat_due:
        state["lastHeartbeatAt"] = now.isoformat(timespec="seconds")
        state_path.write_bytes(canonical_json(state) + b"\n")
        return True
    return False


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Github:
    """Authenticated requests never forward the token across redirects."""
    def __init__(self, repository: str, token: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Repository must be OWNER/NAME")
        self.repository, self.token = repository, token
        self.opener = build_opener(NoRedirect)

    def request(self, path: str, *, method="GET", value=None, binary=None, accept="application/vnd.github+json", host="api.github.com"):
        if host not in ("api.github.com", "uploads.github.com"):
            raise ValueError("Unexpected authenticated request host")
        body = binary if binary is not None else canonical_json(value) if value is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": accept, "User-Agent": "Lazy-Scholar-FLoRA-data-pipeline", "X-GitHub-Api-Version": "2022-11-28"}
        if body is not None:
            headers["Content-Type"] = "application/octet-stream" if binary is not None else "application/json"
        request = Request(f"https://{host}/repos/{self.repository}/{path}", method=method, data=body, headers=headers)
        return self.opener.open(request, timeout=60)

    def json(self, path: str, **kwargs):
        with self.request(path, **kwargs) as response:
            return json.loads(read_limited(response, 4 * 1024 * 1024))

    def find_release(self, tag: str):
        try:
            return self.json(f"releases/tags/{quote(tag, safe='')}")
        except HTTPError as error:
            if error.code != 404:
                raise
        # The by-tag endpoint documents published releases only. The
        # authenticated list endpoint includes drafts for writers, so a
        # partially uploaded draft can be resumed after an interrupted run.
        page = 1
        while True:
            releases = self.json(f"releases?per_page=100&page={page}")
            for release in releases:
                if release.get("tag_name") == tag:
                    return release
            if len(releases) < 100:
                return None
            page += 1

    def asset_bytes(self, asset_id: int) -> bytes:
        try:
            with self.request(f"releases/assets/{asset_id}", accept="application/octet-stream") as response:
                return read_limited(response, MAX_SNAPSHOT_BYTES)
        except HTTPError as error:
            if error.code not in (301, 302, 303, 307, 308):
                raise
            location = error.headers.get("Location", "")
            parsed = urlsplit(location)
            if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".githubusercontent.com"):
                raise ValueError("Unexpected release asset redirect") from error
            # Signed asset URLs are fetched without forwarding GITHUB_TOKEN.
            return download(location, MAX_SNAPSHOT_BYTES)


def publish(input_dir: Path, repository: str, token: str) -> None:
    if not token:
        raise ValueError("GITHUB_TOKEN is required for publishing")
    payload = (input_dir / "forrt-snapshot.json").read_bytes()
    if len(payload) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Snapshot too large")
    snapshot = json.loads(payload)
    content = dict(snapshot)
    version = content.pop("datasetVersion", None)
    expected_version = f"v{SCHEMA_VERSION}-" + sha256(canonical_json(content))[:24]
    if snapshot.get("schemaVersion") != SCHEMA_VERSION or version != expected_version or payload != canonical_json(snapshot):
        raise ValueError("Snapshot is not canonical or its dataset version does not match its content")
    manifest = release_manifest(snapshot, payload, repository)
    manifest_bytes = canonical_json(manifest)
    (input_dir / "manifest.json").write_bytes(manifest_bytes)
    assets = {"forrt-snapshot.json": payload, "manifest.json": manifest_bytes, "forrt-ATTRIBUTION.txt": attribution(snapshot).encode("utf-8")}
    tag = "dataset-" + snapshot["datasetVersion"]
    github = Github(repository, token)
    release = github.find_release(tag)
    if release and not release["draft"]:
        existing = {asset["name"]: asset for asset in release.get("assets", [])}
        for name, body in assets.items():
            if name not in existing or sha256(github.asset_bytes(existing[name]["id"])) != sha256(body):
                raise ValueError(f"Published release differs from expected asset: {name}")
        print(f"Unchanged dataset: {tag}; no release created")
        return
    if release is None:
        release = github.json("releases", method="POST", value={
            "tag_name": tag, "name": f"FLoRA snapshot {snapshot['datasetVersion']}", "draft": True,
            "body": f"{len(snapshot['records'])} FORRT / FLoRA relationship records.\n\nSource: {snapshot['source']['csvUrl']}\n\nData: CC BY 4.0; see forrt-ATTRIBUTION.txt. This is a transformed upstream repository snapshot, not an official FORRT release.",
        })
    existing = {asset["name"]: asset for asset in release.get("assets", [])}
    for name, body in assets.items():
        asset = existing.get(name)
        if asset and github.asset_bytes(asset["id"]) == body:
            continue
        if asset:
            with github.request(f"releases/assets/{asset['id']}", method="DELETE"):
                pass  # only this unpublished draft's known generated asset
        uploaded = github.json(f"releases/{release['id']}/assets?name={quote(name)}", method="POST", binary=body, host="uploads.github.com")
        if uploaded.get("size") != len(body) or github.asset_bytes(uploaded["id"]) != body:
            raise ValueError(f"Uploaded draft asset failed byte verification: {name}")
    # Every draft asset is verified before making the release discoverable.
    github.json(f"releases/{release['id']}", method="PATCH", value={"draft": False, "make_latest": "true"})
    print(f"Published {tag}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--output", type=Path, default=Path("dist"))
    build_parser.add_argument("--repository", help="OWNER/NAME, to generate a release manifest")
    build_parser.add_argument("--commit", help="Full upstream commit SHA; default is latest CSV commit")
    build_parser.add_argument("--state", type=Path, help="Previously published state; skips unchanged CSV downloads")
    publish_parser = sub.add_parser("publish")
    publish_parser.add_argument("--input", type=Path, default=Path("dist"))
    publish_parser.add_argument("--repository", required=True)
    success_parser = sub.add_parser("record-success")
    success_parser.add_argument("--state", type=Path, default=Path(".pipeline-state.json"))
    success_parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    if args.command == "build":
        build(args.output, args.repository, args.commit, args.state)
    elif args.command == "publish":
        publish(args.input, args.repository, os.environ.get("GITHUB_TOKEN", ""))
    else:
        record_success(args.state, args.output)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, HTTPError, OSError, KeyError) as error:
        print(f"Pipeline failed: {error}", file=sys.stderr)
        sys.exit(1)
