output "alb_url" {
  value = "${local.https ? "https" : "http"}://${aws_lb.main.dns_name}"
}

output "ecs_cluster" {
  value = aws_ecs_cluster.main.name
}

output "ecs_service" {
  value = aws_ecs_service.api.name
}

output "db_endpoint" {
  value = aws_db_instance.main.address
}

output "redis_url" {
  value = local.redis_url
}

output "image" {
  value = "${data.aws_ecr_repository.api.repository_url}:${var.image_tag}"
}
