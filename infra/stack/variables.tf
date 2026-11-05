variable "region" {
  type    = string
  default = "us-east-2"
}

variable "name" {
  type    = string
  default = "order-api"
}

variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "az_count" {
  type    = number
  default = 2
}

variable "db_instance_class" {
  type    = string
  default = "db.t3.micro"
}

variable "db_allocated_storage" {
  type    = number
  default = 20
}

variable "db_multi_az" {
  type    = bool
  default = false
}

variable "db_deletion_protection" {
  description = "Set false (and apply) before terraform destroy."
  type        = bool
  default     = true
}

variable "redis_node_type" {
  type    = string
  default = "cache.t4g.micro"
}

variable "certificate_arn" {
  description = "ACM certificate for HTTPS. Empty = HTTP only on port 80."
  type        = string
  default     = ""
}

variable "image_tag" {
  description = "Image tag in the ECR repo to deploy (CI passes the git SHA)."
  type        = string
}

variable "desired_count" {
  type    = number
  default = 2
}

variable "task_cpu" {
  type    = number
  default = 256
}

variable "task_memory" {
  type    = number
  default = 512
}

variable "db_pool_max" {
  description = "Connections per task. Keep desired_count x 2 (deploy overlap) x this under RDS max_connections (~80 on db.t3.micro)."
  type        = number
  default     = 10
}

variable "rate_limit" {
  type    = string
  default = "10/minute"
}

variable "idempotency_ttl_hours" {
  type    = number
  default = 24
}

variable "log_retention_days" {
  type    = number
  default = 30
}
