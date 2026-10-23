locals {
  strategy_runtime_name              = "${var.project_name}-strategy-${coalesce(var.run_id, "manual")}"
  strategy_runtime_config_bucket     = coalesce(var.strategy_runtime_config_bucket, "")
  strategy_runtime_config_key        = coalesce(var.strategy_runtime_config_key, "")
  strategy_runtime_config_object_arn = "arn:aws:s3:::${local.strategy_runtime_config_bucket}/${local.strategy_runtime_config_key}"
  strategy_runtime_tags = {
    Project   = var.project_name
    Component = "strategy-runtime"
    Strategy  = coalesce(var.strategy_name, "unknown")
    RunId     = coalesce(var.run_id, "manual")
    ExpiresAt = var.strategy_runtime_expires_at
  }
}

resource "aws_security_group" "strategy_runtime" {
  count = var.strategy_runtime_enabled ? 1 : 0

  name        = local.strategy_runtime_name
  description = "Strategy runtime egress"
  vpc_id      = data.aws_vpc.default.id

  egress {
    description = "Allow outbound strategy, package, and telemetry traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = local.strategy_runtime_tags
}

resource "aws_iam_role" "strategy_runtime" {
  count = var.strategy_runtime_enabled ? 1 : 0

  name = local.strategy_runtime_name
  tags = local.strategy_runtime_tags

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

resource "aws_iam_role_policy" "strategy_runtime" {
  count = var.strategy_runtime_enabled ? 1 : 0

  name = "strategy-runtime-access"
  role = aws_iam_role.strategy_runtime[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Effect = "Allow"
          Action = [
            "s3:GetObject"
          ]
          Resource = local.strategy_runtime_config_object_arn
        },
        {
          Effect = "Allow"
          Action = [
            "ecr:GetAuthorizationToken",
            "ecr:BatchCheckLayerAvailability",
            "ecr:GetDownloadUrlForLayer",
            "ecr:BatchGetImage"
          ]
          Resource = "*"
        }
      ],
      length(var.strategy_secret_arns) > 0 ? [
        {
          Effect = "Allow"
          Action = [
            "secretsmanager:GetSecretValue"
          ]
          Resource = var.strategy_secret_arns
        }
      ] : []
    )
  })
}

resource "aws_iam_role_policy_attachment" "strategy_runtime_ssm" {
  count = var.strategy_runtime_enabled ? 1 : 0

  role       = aws_iam_role.strategy_runtime[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "strategy_runtime" {
  count = var.strategy_runtime_enabled ? 1 : 0

  name = local.strategy_runtime_name
  role = aws_iam_role.strategy_runtime[0].name
  tags = local.strategy_runtime_tags
}

resource "aws_instance" "strategy_runtime" {
  count = var.strategy_runtime_enabled ? 1 : 0

  ami                         = data.aws_ami.amazon_linux.id
  instance_type               = var.strategy_instance_type
  subnet_id                   = data.aws_subnets.default.ids[0]
  vpc_security_group_ids      = [aws_security_group.strategy_runtime[0].id]
  iam_instance_profile        = aws_iam_instance_profile.strategy_runtime[0].name
  associate_public_ip_address = var.strategy_require_public_ip
  key_name                    = var.ssh_key_name
  user_data = templatefile("${path.module}/strategy_runtime_user_data.sh", {
    aws_region            = var.aws_region
    runtime_config_bucket = local.strategy_runtime_config_bucket
    runtime_config_key    = local.strategy_runtime_config_key
    artifact_type         = var.strategy_artifact_type
    artifact_image        = var.strategy_artifact_image
    startup_command       = var.strategy_startup_command
    execution_command     = var.strategy_execution_command
    runtime_env_secret_id = var.strategy_runtime_env_secret_id
    private_key_secret_id = var.strategy_private_key_secret_id
    execute_scheduled_script_b64 = base64encode(file("${path.module}/../../scripts/nullone-execute-scheduled.sh"))
  })

  tags = merge(local.strategy_runtime_tags, { Name = local.strategy_runtime_name })
}
