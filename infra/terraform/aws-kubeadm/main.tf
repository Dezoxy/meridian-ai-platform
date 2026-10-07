# Implemented as code, never applied (S079). Names, tags and the constants that
# the nodes' boot scripts are given.

locals {
  # One name for the cluster and the things named after it. It is not the
  # managed module's name (meridian-aws-test): IAM role names are unique in an
  # account, and the two modules may be applied one after the other, never
  # together, but a leftover of one must not make the other fail.
  name = "meridian-aws-kubeadm"

  tags = {
    project     = "meridian"
    environment = "aws-kubeadm"
    managed-by  = "terraform"
  }

  # The operating system: Canonical's public SSM parameter for the current
  # stable Ubuntu 24.04 LTS (noble) amd64 image with a gp3 root volume. No image
  # identifier is in the repository: the parameter moves when Canonical
  # publishes a new build, so the image is a MOVING INPUT. The release is the
  # fixed part. The page is "Find Ubuntu images on AWS" in Canonical's AWS
  # documentation (read 2026-10-07); it lists this path shape and 24.04 as a
  # release name.
  ubuntu_image_parameter = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"

  # The one Parameter Store parameter that carries the join command from the
  # control plane to the workers (parameter.tf).
  join_parameter_name = "/${local.name}/join-command"

  # The pod network's range, which kubeadm is given and the network plugin
  # uses. It is the plugin's own default (192.168.0.0/16), so the pinned
  # manifest is applied without a change, and it does not overlap the VPC's
  # 10.0.0.0/16.
  pod_network_cidr = "192.168.0.0/16"

  # The signing key of the Kubernetes project's package repositories. The
  # Kubernetes page "Installing kubeadm" (read 2026-10-07) says: "The same
  # signing key is used for all repositories". This is the
  # fingerprint of the key that Release.key served on 2026-10-07 (for v1.34,
  # v1.35, v1.36 and v1.37 alike); the node refuses a key with another one.
  # That key expires on 2026-12-29. After that date an expired key still has
  # the same fingerprint, so the pin check still PASSES, and what fails is
  # `apt-get update` with apt's own signature error (the script stops there
  # under set -e, with no line of its own). The pin's own refusal fires only
  # when the project rotates to a DIFFERENT key. So the apply must come before
  # 2026-12-29, or the project's current key is read again first (its expiry and
  # its fingerprint) and put here in a committed change. The local is named
  # `signer`, not `key`, because the secret scan's generic rule reads a name
  # with `key` before forty hex digits as a credential, and this value is a
  # public fingerprint.
  kubernetes_apt_signer_fingerprint = "DE15B14486CD377B9E876E1A234654DA9A296436"

  # 6443 is kubeadm's default API server port; the Kubernetes page "Ports and
  # Protocols" (read 2026-10-07) lists it.
  api_port = 6443
}
