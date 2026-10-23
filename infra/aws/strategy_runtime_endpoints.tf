locals {
  strategy_interface_endpoint_services = toset([
    "ecr.api",
    "ecr.dkr",
    "ssm",
    "ssmmessages",
    "ec2messages",
    "secretsmanager",
    "logs",
  ])
}

resource "aws_security_group" "strategy_vpc_endpoints" {
  count = var.strategy_runtime_enabled && var.strategy_create_private_endpoints ? 1 : 0

  name        = "${local.strategy_runtime_name}-vpce"
  description = "Private API endpoints for strategy runtime"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description     = "HTTPS from strategy runtime"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [aws_security_group.strategy_runtime[0].id]
  }

  egress {
    description = "Endpoint response traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.strategy_runtime_tags, { Name = "${local.strategy_runtime_name}-vpce" })
}

resource "aws_vpc_endpoint" "strategy_s3" {
  count = var.strategy_runtime_enabled && var.strategy_create_private_endpoints ? 1 : 0

  vpc_id            = data.aws_vpc.default.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = data.aws_route_tables.default.ids

  tags = merge(local.strategy_runtime_tags, { Name = "${local.strategy_runtime_name}-s3" })
}

resource "aws_vpc_endpoint" "strategy_interface" {
  for_each = var.strategy_runtime_enabled && var.strategy_create_private_endpoints ? local.strategy_interface_endpoint_services : toset([])

  vpc_id              = data.aws_vpc.default.id
  service_name        = "com.amazonaws.${var.aws_region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = data.aws_subnets.default.ids
  security_group_ids  = [aws_security_group.strategy_vpc_endpoints[0].id]
  private_dns_enabled = true

  tags = merge(local.strategy_runtime_tags, { Name = "${local.strategy_runtime_name}-${each.key}" })
}
