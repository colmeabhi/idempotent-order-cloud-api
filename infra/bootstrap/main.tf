# One-time setup, applied by hand with admin credentials:
#
#   cd infra/bootstrap
#   terraform init
#   terraform apply -var state_bucket_name=<globally-unique-name>
#
# Creates what the CI deploy needs before it can run: Terraform state bucket +
# lock table, the ECR repository, and an IAM role GitHub Actions assumes via
# OIDC (no long-lived AWS keys in GitHub). Its own state stays local; keep
# terraform.tfstate somewhere safe, or migrate it into the bucket afterwards.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.80" }
  }
}

provider "aws" {
  region = var.region
  default_tags { tags = { Project = var.name, ManagedBy = "terraform-bootstrap" } }
}

variable "region" {
  type    = string
  default = "us-east-2"
}

variable "name" {
  type    = string
  default = "order-api"
}

variable "state_bucket_name" {
  type        = string
  description = "Globally unique S3 bucket name for Terraform state."
}

variable "github_repo" {
  type        = string
  description = "owner/repo allowed to assume the deploy role."
  default     = "colmeabhi/idempotent-order-cloud-api"
}

data "aws_caller_identity" "current" {}

# ── Terraform state ──────────────────────────────────────────

resource "aws_s3_bucket" "state" {
  bucket = var.state_bucket_name
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_dynamodb_table" "lock" {
  name         = "${var.name}-terraform-lock"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"
  attribute {
    name = "LockID"
    type = "S"
  }
}

# ── Container registry ───────────────────────────────────────

resource "aws_ecr_repository" "api" {
  name                 = var.name
  image_tag_mutability = "IMMUTABLE" # every deploy is a new git-SHA tag
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 30 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 30 }
      action       = { type = "expire" }
    }]
  })
}

# ── GitHub Actions OIDC deploy role ──────────────────────────

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # AWS validates GitHub's certificate itself; the provider still wants a value.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

data "aws_iam_policy_document" "github_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # Only the deploy job (GitHub environment "production") on this repo.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repo}:environment:production"]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name               = "${var.name}-github-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_trust.json
}

# Terraform manages VPC, ALB, ECS, RDS, ElastiCache, SSM and logs, so the role
# needs broad rights on those services. PowerUserAccess covers everything but
# IAM; IAM is granted below only for this project's own roles.
resource "aws_iam_role_policy_attachment" "deploy_power_user" {
  role       = aws_iam_role.deploy.name
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"
}

data "aws_iam_policy_document" "deploy_iam" {
  statement {
    sid = "ManageProjectRoles"
    actions = [
      "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:TagRole", "iam:UntagRole",
      "iam:UpdateAssumeRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
      "iam:ListInstanceProfilesForRole", "iam:PutRolePolicy", "iam:GetRolePolicy",
      "iam:DeleteRolePolicy", "iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:PassRole",
    ]
    resources = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.name}-*"]
  }
  statement {
    sid       = "ServiceLinkedRoles"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["*"]
    condition {
      test     = "StringLike"
      variable = "iam:AWSServiceName"
      values = [
        "ecs.amazonaws.com", "elasticloadbalancing.amazonaws.com",
        "rds.amazonaws.com", "elasticache.amazonaws.com",
      ]
    }
  }
}

resource "aws_iam_role_policy" "deploy_iam" {
  name   = "manage-project-iam"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy_iam.json
}

# ── Values to copy into GitHub → Settings → Variables ────────

output "AWS_REGION" { value = var.region }
output "AWS_DEPLOY_ROLE_ARN" { value = aws_iam_role.deploy.arn }
output "TF_STATE_BUCKET" { value = aws_s3_bucket.state.bucket }
output "TF_LOCK_TABLE" { value = aws_dynamodb_table.lock.name }
output "ECR_REPOSITORY" { value = aws_ecr_repository.api.name }
