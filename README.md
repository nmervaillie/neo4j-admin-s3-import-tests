# neo4j-admin S3 import tests

AWS infrastructure for running `neo4j-admin database import` performance
tests: one EC2 instance, one S3 bucket, synthetic data generation, and a
switchable S3-direct vs local-NVMe import mode.

See `docs/superpowers/specs/2026-11-03-aws-neo4j-import-infra-design.md`
for the design.

## What the tests measure

```
                        ┌──────────────────────────────────────────┐
                        │  GENERATOR (configs/example.json)        │
                        │  same data, seed 42, 4 layouts           │
                        └───────────────┬──────────────────────────┘
                                        │ publish (parallel upload)
                                        ▼
        ┌─────────────────────────── S3 BUCKET ────────────────────────────┐
        │                                                                  │
        │   source-csv-single/      1 file per node/rel type (.csv.gz)     │
        │   source-csv-chunked/   400 files (.csv.gz, 10k rows each)       │
        │   source-parquet-single/ 1 file per type (.parquet)              │
        │   source-parquet-chunked/ 400 files (.parquet)                   │
        │                                                                  │
        └───────┬──────────────────────────────┬───────────────────────────┘
                │                              │
     MODE = s3  │                              │  MODE = local
   (network)    ▼                              ▼  (disk)
   ┌─────────────────────────┐    ┌────────────────────────────────────┐
   │ neo4j-admin import      │    │ aws s3 sync → /scratch (NVMe)      │
   │ reads straight from S3  │    │ then neo4j-admin import reads NVMe │
   └───────────┬─────────────┘    └───────────────┬────────────────────┘
               │                                  │
               └──────────────┬───────────────────┘
                              ▼
              ┌────────────────────────────────┐
              │  pack: tar | zstd → artifact   │
              └───────────────┬────────────────┘
                              │ upload + size verify
                              ▼
        ┌──────────────────────── S3 BUCKET ────────────────────────┐
        │   databases/<db>-<ts>.tar.zst   (imported Neo4j store)   │
        │   timings/<db>-<ts>.json       (sync/import/pack/total)  │
        └───────────────────────────────────────────────────────────┘
```

The experiment: 2 formats (csv.gz / parquet) × 2 granularities
(1 file / many files per node/rel type) × 2 sources (S3-direct /
local NVMe) = 8 imports of the identical logical graph. Everything
else is held constant: same instance, same heap, same seed. The
timings JSON records the axis values (`source_prefix`, `mode`) so
runs compare cleanly.

Note: `total_seconds` in the timings JSON starts at the import, not
the sync — for local-mode end-to-end wall time, add `sync_seconds`.

See `docs/results/2026-11-04-benchmarks.md` for measured results and
findings from the 2026-11 campaigns.

## Requirements

**AWS**
- An AWS account with credentials configured locally
  (`aws sts get-caller-identity` must succeed). Permissions needed:
  EC2 (instances, security groups, key pairs), IAM (role, instance
  profile, policy), S3 (bucket create/delete).
- An EC2 key pair in the target region — the instance is SSH-only,
  no password access.
- Your public IP for `allowed_ssh_cidr` (e.g. `<your_ip>/32`). The
  security group allows SSH from exactly this CIDR; if your IP
  changes between runs, update it or SSH will time out.
- The instance profile the stack creates needs `s3:GetObject`,
  `PutObject`, `ListBucket`, `DeleteObject` on the test bucket —
  terraform sets this up, no manual policy work.

**Tools (operator machine)**
- Terraform >= 1.6 with AWS provider >= 5.0
- [uv](https://docs.astral.sh/uv/) (or Python >= 3.12) — runs the
  generator and the unit tests; dependencies (`boto3`, `pyarrow`)
  install from `pyproject.toml`
- AWS CLI (for `aws s3 ls/cp` when collecting results)

**Neo4j**
- Enterprise tarball is pulled from `dist.neo4j.org` at provision
  time (default `2026.10.1`, see `neo4j_version` variable); the
  license is accepted non-interactively in user_data
  (`--accept-commercial`). Use requires an appropriate Neo4j
  license.

**Cost / scale**
- One m7gd.4xlarge (16 vCPU, 64 GB RAM, ~750 GB NVMe) + 100 GB EBS
  + S3 storage while the stack exists. A full 8-run campaign on the
  1.1M-node dataset is ~35 min of instance time; tear down
  (`terraform destroy`) when done — the bucket has
  `force_destroy = true`, so destroy also deletes all test data.

**Not needed on the EC2 instance**: the generator can run on the
operator machine (it uploads straight to S3); the instance only
needs `run_import.sh`, which relies on tools the user_data script
installs (Java 21, neo4j-admin, awscli v2, zstd, tar).

## Layout

- `terraform/` — root module: S3 bucket, m7gd.4xlarge EC2, IAM, user_data
- `generator/` — synthetic CSV/Parquet data generator
- `scripts/run_import.sh` — import runner (s3 | local mode), artifact upload
- `configs/example.json` — example generator config

## Workflow

```bash
# 0. Authenticate
aws login

# 1. Provision (~3-4 min)
cd terraform
terraform init
terraform apply -var 'allowed_ssh_cidr=<your_ip>/32' -var 'key_name=<your_key_pair>'
# note bucket_name and ssh_command from outputs

# 2. SSH in
ssh ec2-user@<ip>
# wait for setup: until [ -f /opt/neo4j/.setup-complete ]; do sleep 5; done

# 3. Generate source data (on the instance, or anywhere with AWS creds).
#    Four layouts of the same dataset (same --seed): format x file
#    granularity. Keep them side by side under distinct prefixes.
generate --config configs/example.json --dest s3://<bucket>/source-csv-single/ \
  --format csv --rows-per-file 0          # one .csv.gz per node/rel type
generate --config configs/example.json --dest s3://<bucket>/source-csv-chunked/ \
  --format csv --rows-per-file 10000      # thousands of .csv.gz per type
generate --config configs/example.json --dest s3://<bucket>/source-parquet-single/ \
  --format parquet --rows-per-file 0      # one .parquet per type
generate --config configs/example.json --dest s3://<bucket>/source-parquet-chunked/ \
  --format parquet --rows-per-file 10000  # thousands of .parquet per type

# 4. Run imports — 2 modes x 4 layouts:
scripts/run_import.sh --bucket <bucket> --mode s3    --source-prefix source-csv-single
scripts/run_import.sh --bucket <bucket> --mode s3    --source-prefix source-csv-chunked
scripts/run_import.sh --bucket <bucket> --mode s3    --source-prefix source-parquet-single
scripts/run_import.sh --bucket <bucket> --mode s3    --source-prefix source-parquet-chunked
scripts/run_import.sh --bucket <bucket> --mode local --source-prefix source-csv-single
scripts/run_import.sh --bucket <bucket> --mode local --source-prefix source-csv-chunked
scripts/run_import.sh --bucket <bucket> --mode local --source-prefix source-parquet-single
scripts/run_import.sh --bucket <bucket> --mode local --source-prefix source-parquet-chunked

# 5. Results: s3://<bucket>/databases/<db>-<ts>.tar.zst and
#    s3://<bucket>/timings/<db>-<ts>.json

# 6. Tear down between iterations
terraform destroy -var 'allowed_ssh_cidr=<your_ip>/32' -var 'key_name=<your_key_pair>'
```

## Testing

```bash
uv run pytest          # generator + runner unit tests, no AWS needed
cd terraform && terraform validate
```

Note: the bucket has `force_destroy = true` — `terraform destroy` deletes
all objects in it. It holds only generated test data.
