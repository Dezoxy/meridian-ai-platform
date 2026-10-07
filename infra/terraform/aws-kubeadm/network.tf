# Implemented as code, never applied (S079). One VPC, ONE public subnet, an
# internet gateway and a route table. No NAT gateway (it bills by the hour and
# survives a removal that stops half way, ADR 6, "What keeps costing"), so every
# node has a public IPv4 address, which bills by the hour. The two security
# groups (security.tf, 12 ingress rule resources and 2 egress rules) admit
# nothing from the internet but the API server's port: from the one /32 the
# owner names and from the nodes' own public addresses, never from 0.0.0.0/0.
# Everything else is between the two groups. Nothing is made for a second zone:
# no EKS and no RDS here asks for one.
#
# A production cluster puts the nodes in private subnets behind a NAT gateway or
# VPC endpoints and the control plane behind a load balancer.

# A zone that needs no opt-in: a Local Zone or a Wavelength Zone would not do.
data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"

  # Both on: the nodes resolve each other's private host names (the kubelet
  # registers the node under its host name), and Session Manager's agent
  # resolves the service names.
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = local.name }
}

# The VPC's default security group admits traffic from itself and sends
# anywhere. Adopting it with no rules empties it, so nothing can use it by
# mistake.
resource "aws_default_security_group" "main" {
  vpc_id = aws_vpc.main.id

  tags = { Name = "${local.name}-default-unused" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = { Name = local.name }
}

resource "aws_subnet" "public" {
  vpc_id            = aws_vpc.main.id
  availability_zone = data.aws_availability_zones.available.names[0]
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, 0)

  # The nodes need a public address to reach the package repositories, the
  # plugin's manifest, the registries and the Systems Manager endpoints (the
  # sketch's cheaper way out of a NAT gateway, ADR 6).
  map_public_ip_on_launch = true

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}
