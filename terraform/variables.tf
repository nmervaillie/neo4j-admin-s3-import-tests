variable "region" {
  description = "AWS region"
  type        = string
  default     = "eu-west-1"
}

variable "instance_type" {
  description = "EC2 instance type for the import runner"
  type        = string
  default     = "m7gd.4xlarge"
}

variable "neo4j_version" {
  description = "Neo4j Enterprise version (tarball from dist.neo4j.org)"
  type        = string
  default     = "2026.10.1"
}

variable "bucket_name" {
  description = "S3 bucket name prefix; a random hex suffix is appended"
  type        = string
  default     = "neo4j-import-tests"
}

variable "root_volume_size" {
  description = "EBS root volume size in GB (Neo4j install + tooling)"
  type        = number
  default     = 100
}

variable "allowed_ssh_cidr" {
  description = "CIDR allowed to SSH to the instance (e.g. your_ip/32)"
  type        = string
}
