# Shared store for the rate limiter, so every task enforces one limit per
# client IP instead of each task keeping its own count.
# The limiter fails open if Redis is unavailable, so a single node is enough.

resource "aws_elasticache_subnet_group" "main" {
  name       = var.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_elasticache_cluster" "rate_limit" {
  cluster_id           = "${var.name}-rl"
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = var.redis_node_type
  num_cache_nodes      = 1
  parameter_group_name = "default.redis7"
  port                 = 6379
  subnet_group_name    = aws_elasticache_subnet_group.main.name
  security_group_ids   = [aws_security_group.redis.id]
}

locals {
  redis_url = "redis://${aws_elasticache_cluster.rate_limit.cache_nodes[0].address}:6379/0"
}
