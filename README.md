# neo4j-admin S3 import tests

AWS infrastructure for running `neo4j-admin database import` performance
tests: one EC2 instance, one S3 bucket, synthetic data generation, and a
switchable S3-direct vs local-NVMe import mode.

See `docs/superpowers/specs/2026-11-03-aws-neo4j-import-infra-design.md`
for the design.

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
terraform apply -var 'allowed_ssh_cidr=<your_ip>/32'
# note bucket_name and ssh_command from outputs

# 2. SSH in
ssh ec2-user@<ip>
# wait for setup: until [ -f /opt/neo4j/.setup-complete ]; do sleep 5; done

# 3. Generate source data (on the instance, or anywhere with AWS creds)
generate --config configs/example.json --dest s3://<bucket>/source/ \
  --format csv --rows-per-file 100000

# 4. Run import — S3-direct:
scripts/run_import.sh --bucket <bucket> --mode s3
#    or local-NVMe:
scripts/run_import.sh --bucket <bucket> --mode local

# 5. Results: s3://<bucket>/databases/<db>-<ts>.tar.zst and
#    s3://<bucket>/timings/<db>-<ts>.json

# 6. Tear down between iterations
terraform destroy -var 'allowed_ssh_cidr=<your_ip>/32'
```

## Testing

```bash
uv run pytest          # generator + runner unit tests, no AWS needed
cd terraform && terraform validate
```

Note: the bucket has `force_destroy = true` — `terraform destroy` deletes
all objects in it. It holds only generated test data.
