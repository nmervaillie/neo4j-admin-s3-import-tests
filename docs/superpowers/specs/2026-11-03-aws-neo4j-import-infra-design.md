# AWS Infrastructure for neo4j-admin S3 Import Tests — Design

Date: 2026-11-03
Status: Approved (in-chat design review)

## Purpose

Automated AWS provisioning for running `neo4j-admin database import` performance
tests. One EC2 instance and one S3 bucket, reproducible via Terraform, with a
synthetic data generator and a switchable import mode (S3-direct vs
local-NVMe). The imported database artifact is always written back to S3.
Performance testing itself is out of scope for this iteration, but the design
leaves timing data and repeatable provisioning in place for it.

## Decisions

- **IaC tool**: Terraform, single root module. No module split, no remote
  backend, no Packer AMI, no Ansible, no Docker.
- **Region**: eu-west-1.
- **Instance**: m7gd.4xlarge (16 vCPU, 64 GiB RAM, 4x 750 GB NVMe SSD).
- **Neo4j**: 2026.10.1 Enterprise tarball, installed via EC2 user_data.
  Version pinned as a Terraform variable.
- **Data source**: synthetic generator (Python), producing CSV or Parquet,
  either one file per node/relationship label or chunked files.
- **Import modes**: both, switchable per run —
  1. `s3`: `neo4j-admin database import` reads `s3://` URLs directly.
  2. `local`: source files synced to NVMe scratch disk first, then imported
     from local paths.
- **Artifact upload**: mandatory. After import, the resulting database
     directory is uploaded to `s3://bucket/databases/<name>-<timestamp>/`.
     Upload failure is a runner failure.

## Architecture

```
┌───────────────┐  generate    ┌──────────────────────┐
│ generator.py  │ ───────────► │ s3://bucket/source/  │
└───────────────┘              │   nodes/  rels/      │
                               └──────────┬───────────┘
                     s3-direct │          │ sync to NVMe
                               ▼          ▼
                      ┌──────────────────────────┐
                      │ EC2 m7gd.4xlarge         │
                      │ neo4j-admin database     │
                      │ import full              │
                      └───────────┬──────────────┘
                                  │ upload (mandatory)
                                  ▼
                      ┌──────────────────────────┐
                      │ s3://bucket/databases/   │
                      │  <name>-<timestamp>/     │
                      └──────────────────────────┘
```

### Repository layout

```
terraform/               # single root module
  main.tf                # S3 bucket, EC2, IAM instance profile, SG
  variables.tf           # region, instance type, neo4j version, bucket name prefix
  outputs.tf             # instance IP, bucket name, SSH command
  user_data.sh.tpl       # Java + Neo4j tarball install, NVMe mount, fail-fast
generator/               # Python package
  generate.py            # CLI: format, chunking, labels, counts, seed
scripts/
  run_import.sh          # wraps neo4j-admin import; mode = s3 | local; uploads artifact
docs/superpowers/specs/
```

### Components

**Terraform (terraform/)**

- S3 bucket: single bucket, prefixes `source/` and `databases/`.
  `force_destroy = true` (destructive on `terraform destroy` — flagged here
  deliberately: bucket holds only generated test data).
- EC2: m7gd.4xlarge, 100 GB EBS root volume (Neo4j + tooling), NVMe instance
  storage mounted at `/scratch` for local mode and import output.
- IAM instance profile: read/write on the bucket only, no other permissions.
- Security group: SSH (port 22) from operator IP only.
- user_data: `set -euo pipefail`; installs Java and Neo4j Enterprise tarball
  (version from variable); mounts NVme; logs to `/var/log/neo4j-setup.log`;
  writes a status marker file so boot success is checkable.
- Outputs: instance public IP, bucket name, ready-to-use SSH command.

**Generator (generator/generate.py, Python 3.14)**

- Configuration-driven: node labels, relationship types, counts, columns per
  label (`<id>` plus properties of mixed types).
- CLI flags: `--format csv|parquet`, `--rows-per-file N` (0 = one file per
  label), `--seed` for deterministic output, `--dest s3://bucket/source/` or
  local path.
- Dependencies: boto3, pyarrow (parquet), no heavyweight frameworks.
- Partial-write safety: files written under `.tmp` prefix then renamed, so the
  import runner never sees half-written files.

**Import runner (scripts/run_import.sh)**

- `--mode s3`: invokes `neo4j-admin database import full <name>` with `s3://`
  source URLs. Exact S3 configuration for the admin tool (credentials via
  instance profile) is verified during implementation against Neo4j 2026.10
  documentation.
- `--mode local`: `aws s3 sync s3://bucket/source/ /scratch/source/` first,
  then the same import command with local paths.
- Heap size tunable via environment variable (`JAVA_HEAP`, default sized for
  64 GiB RAM).
- Dry-run mode: prints the resolved `neo4j-admin` command without executing.
- After import: uploads the resulting database directory to
  `s3://bucket/databases/<name>-<timestamp>/` as a tar + zstd archive named
  `<name>-<timestamp>.tar.zst`. Upload verified by an
  `aws s3 ls` size check > 0; failure aborts the run as failed.
- Logs timings to stdout and to `s3://bucket/timings/` for later performance
  analysis.

## Data flow

1. `terraform apply` — bucket + instance ready (~3-4 min).
2. SSH to instance.
3. `generate.py` fills `s3://bucket/source/` (or `/scratch/source/` for local
   mode).
4. `run_import.sh --mode s3|local` — import runs, database written to NVMe.
5. Runner uploads artifact to `s3://bucket/databases/<name>-<timestamp>/`.
6. `terraform destroy` tears everything down between test iterations.

## Error handling

- Expired AWS session (`aws login`): apply fails fast with a clear message;
  re-authentication is a manual operator step.
- user_data: fail-fast shell, status marker file, boot log at
  `/var/log/neo4j-setup.log`.
- Generator: validates row counts before upload; aborts on partial write;
  temp-prefix rename strategy above.
- Import runner: fail-fast; neo4j-admin exit code checked; on failure NVMe
  scratch is preserved for post-mortem and the report location is printed.
- Artifact upload: verified with a size check; upload failure = run failure.

## Testing

- Generator: pytest — row counts, chunking boundaries, parquet schema,
  deterministic seed reproduction. Runs locally, no AWS needed.
- Terraform: `terraform validate` and `terraform plan` as cheap checks; one
  real `apply`/`destroy` smoke test as a manual gate (incurs AWS cost).
- Import runner: dry-run mode asserting the resolved command line; smoke
  tested during the manual apply gate.
- Performance testing: out of scope. Hook in place: timings logged to
  `s3://bucket/timings/`.

## Out of scope

- Performance test methodology and analysis (next iteration).
- Multiple environments, remote state backend, module split, Packer AMIs,
  Ansible, Docker.
- Real (non-synthetic) datasets.
