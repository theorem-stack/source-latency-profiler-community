variable "project_name" {
  description = "Name prefix for AWS resources."
  type        = string
  default     = "source-latency-profiler"
}

variable "aws_region" {
  description = "AWS region for this runner deployment."
  type        = string
  default     = "us-east-1"
}

variable "results_bucket_name" {
  description = "Optional existing or desired S3 bucket name for persistent profiler results."
  type        = string
  default     = null
}

variable "create_results_bucket" {
  description = "Create and manage the persistent S3 results bucket in this state. Set false to reference an existing bucket."
  type        = bool
  default     = true
}

variable "runner_count" {
  description = "Number of identical runner instances to create in this region."
  type        = number
  default     = 1
}

variable "instance_type" {
  description = "EC2 instance type for probe runners."
  type        = string
  default     = "t3.micro"
}

variable "ssh_key_name" {
  description = "Optional EC2 key pair name for debugging access."
  type        = string
  default     = null
}

variable "strategy_runtime_enabled" {
  description = "Create an ephemeral strategy runtime host for NullOne."
  type        = bool
  default     = false
}

variable "run_id" {
  description = "Run identifier used to tag strategy runtime resources."
  type        = string
  default     = null
}

variable "strategy_name" {
  description = "Strategy name used to tag strategy runtime resources."
  type        = string
  default     = null
}

variable "strategy_instance_type" {
  description = "EC2 instance type for the strategy runtime host."
  type        = string
  default     = "c7i.large"
}

variable "strategy_artifact_image" {
  description = "Container image for the strategy runtime."
  type        = string
  default     = ""
}

variable "strategy_artifact_type" {
  description = "Artifact type for the strategy runtime. Currently supports container."
  type        = string
  default     = "container"
}

variable "strategy_runtime_config_bucket" {
  description = "S3 bucket containing the generated NullOne runtime config."
  type        = string
  default     = null
}

variable "strategy_runtime_config_key" {
  description = "S3 key containing the generated NullOne runtime config."
  type        = string
  default     = null
}

variable "strategy_startup_command" {
  description = "Command used to warm the strategy runtime."
  type        = string
  default     = "nullone ready --config /etc/nullone/runtime.json"
}

variable "strategy_execution_command" {
  description = "Command used by operators or scheduler to execute the strategy."
  type        = string
  default     = "nullone execute --config /etc/nullone/runtime.json"
}

variable "strategy_secret_arns" {
  description = "Secrets Manager ARNs the strategy runtime may read."
  type        = list(string)
  default     = []
}

variable "strategy_runtime_env_secret_id" {
  description = "Secrets Manager secret ID containing NullOne runtime env JSON."
  type        = string
  default     = ""
}

variable "strategy_private_key_secret_id" {
  description = "Secrets Manager secret ID containing the Kalshi private key PEM."
  type        = string
  default     = ""
}

variable "strategy_require_public_ip" {
  description = "Whether the strategy runtime should receive a public IP."
  type        = bool
  default     = false
}

variable "strategy_runtime_expires_at" {
  description = "UTC RFC3339 timestamp after which independent janitors may tear down the strategy runtime."
  type        = string
  default     = ""
}

variable "strategy_create_private_endpoints" {
  description = "Create VPC endpoints required for private strategy runtime E2E without a public IP."
  type        = bool
  default     = false
}
