# Lazy Scholar FORRT data publisher

This directory is a standalone public repository template. It converts the official [FORRT / FLoRA CSV](https://github.com/forrtproject/flora-validation/blob/main/output/flora.csv) into a compact DOI index for Lazy Scholar, checks upstream daily, and publishes versioned GitHub Release assets only when the generated snapshot changes. Python 3.10 or later and its standard library are sufficient; no hosted API, paid service, package installation, or client authentication is required.

Only publish the contents of this directory to the data repository. The surrounding Lazy Scholar extension is a separate project and must not be included. No GitHub repository or release has been created by preparing these files.

## Publish the separate repository

1. Copy this directory to a new location outside the extension. Create an **empty public** GitHub repository, for example `lazy-scholar-replication-data`, in the intended account. Do not initialize the GitHub repository with additional files.
2. From the copied directory, initialize and push it. Replace `OWNER` with the actual account; the workflow uses its own repository name automatically.

   ```sh
   git init
   git branch -M main
   git add .github .gitignore LICENSE README.md pipeline.py test_pipeline.py
   git commit -m "Add FORRT data publisher"
   git remote add origin https://github.com/OWNER/lazy-scholar-replication-data.git
   git push -u origin main
   ```

3. Allow GitHub Actions in the repository settings. The workflow declares `contents: write` to create releases and commit its publication state; repository/organization policy and branch rules must permit those actions. It uses the automatically supplied `GITHUB_TOKEN`; do not create or embed a personal token in the extension.
4. Open **Actions → Publish FORRT data → Run workflow**, selecting the default branch. Verify that the first run succeeds and that its release contains `manifest.json`, `forrt-snapshot.json`, and `forrt-ATTRIBUTION.txt`.
5. From the **Lazy Scholar extension folder**, run its setup helper after the release is public. It verifies the manifest and snapshot before saving the update URL:

   ```sh
   node scripts/configure-forrt.mjs OWNER/lazy-scholar-replication-data
   ```

   Reload the extension, or package the updated extension for distribution. To configure it manually instead, set `FORRT_MANIFEST_URL` in `src/services/replications/config.js` to:

   ```text
   https://github.com/OWNER/lazy-scholar-replication-data/releases/latest/download/manifest.json
   ```

   The manifest points to a particular release's snapshot URL, not to `latest`. The extension checks this small manifest weekly, downloads a new snapshot only for a new version, verifies its byte length/hash/schema, and keeps the previous valid snapshot if an update fails. Its bundled snapshot works before hosting is configured.

## Run locally

Run these commands from this directory. They download public source files and write only to the ignored `dist` directory; they do not publish anything.

```sh
python -m unittest -v
python pipeline.py build --repository OWNER/lazy-scholar-replication-data
```

For a reproducible rebuild, pass `--commit` with a full 40-character upstream commit SHA. To update the extension's bundled seed after inspecting a build, copy only `dist/forrt-snapshot.json` and `dist/forrt-ATTRIBUTION.txt` into its `data` directory. Do not copy the workflow's saved state into the extension.

The `publish` command is used by the workflow after validation. It requires `GITHUB_TOKEN` in the environment and `--repository OWNER/NAME`; it writes real GitHub releases. Never put a token in a command argument, repository file, release asset, or extension.

## Daily publication and failure handling

- The workflow runs at **06:17 UTC daily**, after upstream's usual overnight build, and can also be run manually. It only runs on the default branch and serializes publishing jobs.
- It finds the most recent commit touching `output/flora.csv`, then fetches the CSV and its source manifest at that same full commit. This avoids mixing versions if upstream updates during a run. It verifies the CSV's SHA-256, byte length, exact column list, row count, required fields, stable record IDs, and supported record types before conversion.
- If the CSV commit and converter file have not changed since the last successful publication, it skips downloading the large CSV. Changed source or converter code rebuilds a deterministic snapshot. No local build timestamp enters the data or version hash.
- Publication first creates or resumes an unpublished draft with a content-derived tag. It downloads and verifies all three uploaded assets before making the release public/latest. An already published identical version is verified and reused. Published assets are never overwritten; a conflicting asset fails the job. Interrupted draft uploads can be retried.
- A failed fetch, validation, upload, or hash check stops publication; the previous public release remains usable. GitHub Actions failures appear in the repository's Actions tab and follow the account's notification settings.
- `.pipeline-state.json` is committed only after successful publication (or an unchanged successful check), and at least once every 30 days. This provides repository activity even if FORRT data stays unchanged. GitHub [disables scheduled workflows in inactive public repositories after 60 days](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule), and scheduled runs can be delayed or dropped. Keep workflow write access enabled and inspect failed runs; this is a best-effort daily update, not a timing guarantee.

## Snapshot contract

`schemaVersion` is `1`. Objects are serialized as compact, sorted-key UTF-8 JSON. `datasetVersion` is `v1-` plus the first 24 hex characters of SHA-256 over the canonical snapshot before inserting `datasetVersion`. This includes the source commit, hash, and upstream generation date, but excludes the local build time. A version is therefore stable for identical source inputs and conversion behavior.

| Field | Meaning |
| --- | --- |
| `source` | FORRT / FLoRA name, commit-pinned CSV URL, full commit SHA, CSV SHA-256, upstream `generatedAt`, CC BY 4.0 license URL, and dataset citation URL. |
| `papers` | Paper metadata keyed by `p-` plus a hash of its identity. Each has `doi`, `title`, `authors`, `year`, `journal`, `url`, and optional `altId`; missing values are `null`. Authors are a display string. |
| `records` | Original-to-report relationships keyed by upstream `flora_id` (for example `FRED-1` or `REPRO-1`), linking two paper IDs and a `replication` or `reproduction` type. |
| `outcome`, `computational`, `robustness` | Each record retains these three separate `{label, quote, quoteSource}` objects. Labels and quotations come from upstream and are not reclassified by Lazy Scholar. |
| `provenance` | Each record retains upstream `sourceId` (the separate `FLORA-*` ID), `source`, `sourceDisplayId`, and `sourceRecordId`. |
| `byDoi` | Normalized DOI keys with `asOriginal` and `asReport` arrays of relationship IDs; either role can be present. |

DOIs are trimmed, DOI URL or `doi:` prefixes are removed case-insensitively, URL suffixes are percent-decoded once, and the result is lowercased. Suffix punctuation is preserved. Whitespace or malformed DOI values are never indexed. A malformed upstream DOI is retained as an alternate identifier if one is not already present. Papers without DOIs remain available as linked evidence for a matching original or report; the extension does not guess matches from titles.

Paper identity uses DOI first, then safe source URL, then alternate identifier, then a record-specific fallback. Metadata is deduplicated with a stable first-value choice and enrichment of missing fields. Every relationship ID remains available. UI counts should count distinct linked paper IDs for each type and role, not CSV rows or individual experiments. Reproduction computational and robustness outcomes remain separate from replication outcomes. Conflicting record labels are retained as evidence rather than collapsed into a reliability score.

The release `manifest.json` has exactly these fields:

```json
{
  "schemaVersion": 1,
  "datasetVersion": "v1-CONTENT_HASH",
  "sourceGeneratedAt": "UPSTREAM_ISO_TIMESTAMP",
  "sha256": "SHA256_OF_SNAPSHOT_BYTES",
  "bytes": 4111800,
  "recordCount": 2850,
  "url": "https://github.com/OWNER/REPO/releases/download/dataset-v1-CONTENT_HASH/forrt-snapshot.json"
}
```

The converter enforces a 20 MiB snapshot and 100,000-record ceiling compatible with the extension validator. If those are exceeded, publication stops until both sides are deliberately updated. Changes to the schema require a coordinated extension/converter update.

The upstream manifest currently labels the repository snapshot `published: false` and `version: null`. Those flags are not treated as an official FORRT release or silently substituted with a FORRT version number. Lazy Scholar publishes its own clearly attributed derivative, preserving the actual source commit, hash, and generation date.

## Credit, license, and hosting

The data are from **FORRT / FLoRA**, the FORRT Library of Reproduction and Replication Attempts. See the [official description, complete author list, recommended citation, and CC BY 4.0 statement](https://forrt.org/replication-hub/flora/) and [dataset DOI](https://doi.org/10.17605/OSF.IO/9R62X). Generated data remain under [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/). Each release includes `forrt-ATTRIBUTION.txt` with source provenance and the changes made by Lazy Scholar. The pipeline code is MIT licensed; that license does not replace the data license.

Show visible **FORRT / FLoRA** credit and links alongside replication/reproduction results, and keep attribution available in the extension. The database records reported attempts and their authors' conclusions; a missing match is not evidence that no replication exists. FORRT does not endorse Lazy Scholar. FORRT asks prospective data users to consider contacting its team; this pipeline does not contact anyone or use the hosted replication API.

GitHub's standard hosted Actions runners are [free for public repositories](https://docs.github.com/en/billing/concepts/product-billing/github-actions). [Release assets](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases) have no published total release-size or bandwidth limit, with individual files below 2 GiB and up to 1,000 assets per release. This workflow uses a standard runner and three small assets, without Actions artifacts or caches. These are the current product terms, not a permanent capacity guarantee; normal GitHub terms and availability still apply. No GitHub Pages site is needed.
