terraform {
  required_version = ">= 1.9"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.70" }
    archive = { source = "hashicorp/archive", version = "~> 2.6" }
  }
}

# Region and, for the local emulator, the endpoint, credentials and CA bundle
# all come from the environment (AWS_REGION, AWS_ENDPOINT_URL, ...).
provider "aws" {}
