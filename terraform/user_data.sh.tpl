#!/bin/bash
# Installs Java 21 + Neo4j Enterprise, RAID0-mounts NVMe scratch at /scratch,
# and points Neo4j's data directory at the NVMe volume.
set -euo pipefail
exec > >(tee /var/log/neo4j-setup.log) 2>&1

NEO4J_VERSION="${neo4j_version}"
NEO4J_HOME=/opt/neo4j
NEO4J_DATA_DIR=/scratch/neo4j-data

dnf install -y java-21-amazon-corretto-headless tar zstd mdadm xfsprogs unzip

# AWS CLI v2 (aarch64) — reliable across AL2023 package set changes
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" \
  -o /tmp/awscli.zip
unzip -q /tmp/awscli.zip -d /tmp
/tmp/aws/install

# RAID0 all instance-store NVMe namespace devices into /scratch.
# Namespace-only glob (-ns-*): the controller device must be excluded.
# Model match keeps the EBS root volume (/dev/nvme0n1) out.
DEVICES=$(ls /dev/disk/by-id/nvme-Amazon_EC2_NVMe_Instance_Storage_*-ns-* 2>/dev/null || true)
if [[ -n "$DEVICES" ]]; then
  N=$(echo $DEVICES | wc -w)
  mdadm --create /dev/md0 --level=0 --raid-devices="$N" $DEVICES --force
  mkfs.xfs /dev/md0
  mkdir -p /scratch
  echo '/dev/md0 /scratch xfs defaults,nofail 0 2' >> /etc/fstab
  mount /scratch
else
  mkdir -p /scratch # fallback: no instance-store NVMe found
fi

mkdir -p /scratch/source /scratch/databases "$NEO4J_DATA_DIR" "$NEO4J_HOME"

curl -fsSL "https://dist.neo4j.org/neo4j-enterprise-$NEO4J_VERSION-unix.tar.gz" \
  -o /tmp/neo4j.tgz
tar -xzf /tmp/neo4j.tgz -C "$NEO4J_HOME" --strip-components=1

# Offline import writes here; runner reads NEO4J_DATA_DIR from this conf.
echo "server.directories.data=$NEO4J_DATA_DIR" >> "$NEO4J_HOME/conf/neo4j.conf"

# 2026.10.1: license subcommand takes the flag directly (no 'accept')
"$NEO4J_HOME/bin/neo4j-admin" server license --accept-commercial

# Ownership last: tarball extracted as root, everything below must be
# writable by ec2-user (import runner writes databases/transactions).
chown -R ec2-user:ec2-user /scratch "$NEO4J_HOME"

touch "$NEO4J_HOME/.setup-complete"
echo "setup complete"
