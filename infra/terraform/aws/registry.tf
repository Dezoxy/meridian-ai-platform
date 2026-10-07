# One repository for the one image the chart runs (ADR 6: one repository per
# image name; the registry itself is the account and Region pair).
resource "aws_ecr_repository" "meridian" {
  name = "meridian"

  # The chart pulls by digest, so an immutable tag costs nothing and keeps a
  # tag from being moved under a running deployment.
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  # AWS-owned keys. A production registry uses a customer-managed KMS key
  # (encryption_type = "KMS"); a key here would wait at least seven days to be
  # removed.
  encryption_configuration {
    encryption_type = "AES256"
  }

  # The provider's page: force_delete deletes the repository even if it
  # contains images, and defaults to false. The page does not say what the
  # default does to a repository that holds an image; the flag's wording
  # implies a refusal, and the second half pushes an image, so the flag is on.
  # This is a test environment made to be removed. Production leaves it off,
  # so an image cannot be lost to a removal.
  force_delete = true
}
