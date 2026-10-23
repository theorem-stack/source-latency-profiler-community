terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_ami" "amazon_linux" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

data "aws_route_tables" "default" {
  vpc_id = data.aws_vpc.default.id
}

resource "aws_security_group" "runner" {
  count = var.runner_count > 0 ? 1 : 0

  name        = "${var.project_name}-runner-${var.aws_region}"
  description = "Source latency profiler runner egress"
  vpc_id      = data.aws_vpc.default.id

  egress {
    description = "Allow outbound probes and package installation"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Project   = var.project_name
    Component = "runner"
  }
}

resource "aws_iam_role" "runner" {
  count = var.runner_count > 0 ? 1 : 0

  name = "${var.project_name}-runner-${var.aws_region}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy" "runner_results" {
  count = var.runner_count > 0 ? 1 : 0

  name = "write-profiler-results"
  role = aws_iam_role.runner[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:AbortMultipartUpload"
        ]
        Resource = "${local.results_bucket_arn}/runs/*"
      },
      {
        Effect = "Allow"
        Action = [
          "s3:ListBucket"
        ]
        Resource = local.results_bucket_arn
      }
    ]
  })
}

resource "aws_iam_instance_profile" "runner" {
  count = var.runner_count > 0 ? 1 : 0

  name = "${var.project_name}-runner-${var.aws_region}"
  role = aws_iam_role.runner[0].name
}

resource "aws_instance" "runner" {
  count = var.runner_count

  ami                         = data.aws_ami.amazon_linux.id
  instance_type               = var.instance_type
  subnet_id                   = data.aws_subnets.default.ids[count.index % length(data.aws_subnets.default.ids)]
  vpc_security_group_ids      = [aws_security_group.runner[0].id]
  iam_instance_profile        = aws_iam_instance_profile.runner[0].name
  associate_public_ip_address = true
  key_name                    = var.ssh_key_name
  user_data = templatefile("${path.module}/runner_user_data.sh", {
    results_bucket = local.results_bucket_id
    aws_region     = var.aws_region
  })

  tags = {
    Name      = "${var.project_name}-${var.aws_region}-${count.index}"
    Project   = var.project_name
    Component = "runner"
  }
}
