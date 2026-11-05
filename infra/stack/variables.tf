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
