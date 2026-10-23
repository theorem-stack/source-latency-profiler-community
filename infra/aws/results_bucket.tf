data "aws_caller_identity" "current" {}

locals {
  effective_results_bucket_name = coalesce(
    var.results_bucket_name,
    "${var.project_name}-results-${data.aws_caller_identity.current.account_id}"
  )
  results_bucket_id  = var.create_results_bucket ? aws_s3_bucket.results[0].id : data.aws_s3_bucket.results[0].id
  results_bucket_arn = var.create_results_bucket ? aws_s3_bucket.results[0].arn : data.aws_s3_bucket.results[0].arn
}

data "aws_s3_bucket" "results" {
  count  = var.create_results_bucket ? 0 : 1
  bucket = local.effective_results_bucket_name
}

resource "aws_s3_bucket" "results" {
  count  = var.create_results_bucket ? 1 : 0
  bucket = local.effective_results_bucket_name

  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Project   = var.project_name
    Component = "results"
    Retention = "persistent"
  }
}

resource "aws_s3_bucket_versioning" "results" {
  count  = var.create_results_bucket ? 1 : 0
  bucket = local.results_bucket_id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "results" {
  count  = var.create_results_bucket ? 1 : 0
  bucket = local.results_bucket_id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "results" {
  count  = var.create_results_bucket ? 1 : 0
  bucket = local.results_bucket_id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
