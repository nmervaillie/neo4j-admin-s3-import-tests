#!/bin/bash
# Installs Java 21 + Neo4j Enterprise, RAID0-mounts NVMe scratch at /scratch.
set -euo pipefail
exec > >(tee /var/log/neo4j-setup.log) 2>&1

NEO4J_VERSION="${neo4j_version}"
NEO4J_HOME=/opt/neo4j

dnf install -y java-21-amazon-corretto-headless tar zstd awscli mdadm xfsprogs

# RAID0 all instance-store NVMe devices into /scratch
DEVICES=$(ls /dev/nvme[0-9]*n1 2>/dev/null || true)
if [[ -n "$DEVICES" ]]; then
  N=$(echo $DEVICES | wc -w)
  mdadm --create /dev/md0 --level=0 --raid-devices="$N" $DEVICES --force
  mkfs.xfs /dev/md0
  mkdir -p /scratch
  echo '/dev/md0 /scratch xfs defaults,nofail 0 2' >> /etc/fstab
  mount /scratch
else
  mkdir -p /scratch # fallback: no NVMe found (wrong instance family?)
fi

mkdir -p /scratch/source /scratch/databases "$NEO4J_HOME"
chown -R ec2-user:ec2-user /scratch "$NEO4J_HOME"

curl -fsSL "https://dist.neo4j.org/neo4j-enterprise-$NEO4J_VERSION-unix.tar.gz" \
  -o /tmp/neo4j.tgz
tar -xzf /tmp/neo4j.tgz -C "$NEO4J_HOME" --strip-components=1
"$NEO4J_HOME/bin/neo4j-admin" server license accept

touch "$NEO4J_HOME/.setup-complete"
echo "setup complete"
