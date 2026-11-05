terraform {
  required_version = ">= 1.6"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.80" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }

  # Partial config: bucket, key, region and dynamodb_table are passed with
  # -backend-config (see .github/workflows/deploy.yml and README).
  backend "s3" {}
}

provider "aws" {
  region = var.region
  default_tags { tags = { Project = var.name, ManagedBy = "terraform" } }
}
