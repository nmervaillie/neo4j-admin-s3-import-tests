# Benchmark results — 2026-11-04

Two full campaigns (8 imports each) of `neo4j-admin database import full`
against synthetic datasets of the same shape, differing only in scale.
Raw timing JSONs: `results/2026-11-04-100x/` (4M-row dataset) and
`results/2026-11-04-10x/` (40M-row dataset) — not committed; regenerate
by rerunning the campaign.

## Setup (both runs)

- Instance: m7gd.4xlarge (16 vCPU, 61 GiB RAM, NVMe instance store,
  RAID0-mounted at `/scratch`), eu-west-1
- Neo4j Enterprise 2026.10.1, Java 21 (Corretto), heap 8g, 16 workers
- Dataset: seed 42, `configs/example.json` shape — Person + Company
  nodes, WORKS_FOR + KNOWS relationships
- Layouts: 4 per run — format (csv.gz / parquet) × granularity
  (1 file per node/rel type / 400 files). Identical logical data in
  every layout.
- Modes: `s3` (neo4j-admin reads S3 directly) and `local`
  (`aws s3 sync` to NVMe first, then import)
- csv.gz level 6; parquet snappy (pyarrow default)

## 40M-row dataset (10M Person / 1M Company / 9M WORKS_FOR / 20M KNOWS)

Source sizes: csv ~590 MB, parquet ~1.05 GB per layout
(parquet is larger because the synthetic random strings do not
compress under snappy; gzip handles them better).

| Mode | Layout | Sync | Import | Total | Artifact |
|------|--------|------|--------|-------|----------|
| s3 | csv-single | — | 37s | 52s | 888 MB |
| s3 | parquet-single | — | 74s | 89s | 848 MB |
| s3 | csv-chunked | — | 131s | 146s | 888 MB |
| s3 | parquet-chunked | — | 184s | 199s | 848 MB |
| local | csv-single | 2s | 33s | 49s | 890 MB |
| local | parquet-single | 3s | 37s | 51s | 850 MB |
| local | csv-chunked | 4s | 38s | 54s | 890 MB |
| local | parquet-chunked | 4s | 37s | 52s | 850 MB |

## 4M-row dataset (1M / 100k / 900k / 2M)

Source sizes: csv ~56 MB, parquet ~105 MB per layout. Absolute times
include a ~6s JVM/import floor — ratios are the signal here, not the
absolute values.

| Mode | Layout | Sync | Import | Total |
|------|--------|------|--------|-------|
| s3 | csv-single | — | 12s | 15s |
| s3 | parquet-single | — | 17s | 20s |
| s3 | csv-chunked | — | 79s | 82s |
| s3 | parquet-chunked | — | 147s | 151s |
| local | csv-single | 1s | 10s | 14s |
| local | parquet-single | 1s | 11s | 14s |
| local | csv-chunked | 2s | 12s | 15s |
| local | parquet-chunked | 2s | 11s | 14s |

## Findings

1. **File granularity only matters over S3.** On local NVMe, import
   time is flat across 1-file and 400-file layouts (33–38s at 40M
   rows). Over S3 the 400-file layouts are 2.5–3.5x slower. The
   bottleneck is per-file HTTP request overhead in neo4j-admin's S3
   reader, not the import itself. Prefer few, large source files
   when importing directly from S3.
2. **Transfer volume, not format, drives the s3-single gap.** Over
   S3, parquet-single was 2x slower than csv-single at 40M rows
   (74s vs 37s) — tracking its 1.8x larger byte size. On NVMe the
   same two layouts are within noise. With compressible real-world
   data the parquet/csv size gap would shrink or invert.
3. **Pre-syncing to NVMe is cheap and always competitive.** The
   `aws s3 sync` step cost 2–4s per layout (datacenter-to-datacenter)
   and local mode matched or beat every s3-mode run, including
   s3-single. For repeated imports against the same dataset, syncing
   once and importing locally is strictly better.
4. **Chunked parquet over S3 is the worst combination** (184s at 40M
   rows): large objects times many requests. Avoid it.
5. **Artifacts are not bit-reproducible.** The same logical graph
   yields artifact sizes differing by up to ~5% across runs (see
   `artifact_bytes`). Import is a 16-worker parallel pipeline; store
   page write order and internal ID assignment vary with worker
   timing and input file layout. Differences are byte-layout noise,
   not data differences — every run imported the exact same counts.

## Caveats

- Synthetic data: string values are random and defeat snappy, which
  inflates parquet source size vs csv.gz. Real parquet data usually
  compresses better; finding 2 is about bytes-on-the-wire, not about
  parquet the format.
- `total_seconds` excludes `sync_seconds` (it starts at the import);
  for local-mode end-to-end wall time, add the two.
- Single run per configuration; no repeats, so treat small deltas
  (±10%) as noise.
- At 400M-row scale, chunked layouts need larger `--rows-per-file`
  (~250k): thousands of small files push one comma-joined
  `--nodes`/`--relationships` argument past the Linux 128 KB
  per-argument limit (`E2BIG`).
