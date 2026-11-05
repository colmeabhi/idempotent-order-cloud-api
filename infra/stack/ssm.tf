# The DB password never appears in the task definition or in GitHub: ECS reads
# it from this SecureString at task start (see "secrets" in ecs.tf).
#
# It does live in Terraform state, which is why the state bucket is private,
# encrypted and versioned.

resource "random_password" "db" {
  length           = 32
  special          = true
  override_special = "!#$%^&*()-_=+[]{}<>:?" # RDS rejects / @ " and space
}

resource "aws_ssm_parameter" "db_password" {
  name  = "/${var.name}/db-password"
  type  = "SecureString"
  value = random_password.db.result
}
