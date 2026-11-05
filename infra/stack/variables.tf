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
