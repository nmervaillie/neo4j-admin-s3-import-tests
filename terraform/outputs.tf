output "bucket_name" {
  description = "S3 bucket for source data, artifacts, and timings"
  value       = aws_s3_bucket.data.bucket
}

output "instance_public_ip" {
  description = "EC2 import runner public IP"
  value       = aws_instance.import_runner.public_ip
}

output "ssh_command" {
  description = "SSH into the import runner"
  value       = "ssh ec2-user@${aws_instance.import_runner.public_ip}"
}
