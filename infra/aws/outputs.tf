output "results_bucket" {
  description = "Persistent S3 bucket for profiler result JSONL."
  value       = local.results_bucket_id
}

output "runner_instance_ids" {
  description = "EC2 runner instance IDs."
  value       = aws_instance.runner[*].id
}

output "runner_public_ips" {
  description = "Public IPs for debugging runner instances."
  value       = aws_instance.runner[*].public_ip
}

output "runner_region" {
  description = "AWS region for this runner deployment."
  value       = var.aws_region
}

output "strategy_runtime_instance_id" {
  description = "EC2 instance ID for the strategy runtime, when enabled."
  value       = try(aws_instance.strategy_runtime[0].id, null)
}

output "strategy_runtime_private_ip" {
  description = "Private IP for the strategy runtime, when enabled."
  value       = try(aws_instance.strategy_runtime[0].private_ip, null)
}

output "strategy_runtime_public_ip" {
  description = "Public IP for the strategy runtime, when enabled."
  value       = try(aws_instance.strategy_runtime[0].public_ip, null)
}
