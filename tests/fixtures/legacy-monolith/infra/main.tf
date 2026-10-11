terraform {
  required_version = ">= 1.7"
}

resource "aws_s3_bucket" "assets" {
  bucket = "legacy-commerce-assets"
}

