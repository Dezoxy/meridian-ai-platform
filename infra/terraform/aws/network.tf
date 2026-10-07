# Two zones, public subnets, no NAT gateway and no Elastic IP address. A NAT
# gateway bills by the hour and survives a removal that stops half way (ADR 6,
# "What keeps costing"); public subnets leave nothing like it behind. The
# price is that each node has a public IPv4 address (USD 0.005 an hour each).
# Security groups admit nothing from the internet to a node.
#
# A production environment puts the nodes in private subnets behind a NAT
# gateway (or VPC endpoints) and keeps only load balancers in public ones.

# Zones that need no opt-in: a Local Zone or a Wavelength Zone would not do for
# EKS or RDS.
data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  zones = slice(data.aws_availability_zones.available.names, 0, 2)
}

resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"

  # Both are off by default and both are needed: the private endpoint of the
  # cluster is a private hosted zone and the database endpoint is a name.
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
  count = length(local.zones)

  vpc_id            = aws_vpc.main.id
  availability_zone = local.zones[count.index]
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)

  # Nodes in a public subnet need a public address to reach the API server's
  # public side, the registry and the internet (the sketch's cheaper way out of
  # a NAT gateway, ADR 6).
  map_public_ip_on_launch = true

  tags = {
    Name = "${local.name}-public-${local.zones[count.index]}"
    # What the AWS Load Balancer Controller reads to find the subnets of an
    # internet-facing load balancer. Nothing in this module installs it; the
    # tags cost nothing and the chart's edge would need them.
    "kubernetes.io/role/elb"              = "1"
    "kubernetes.io/cluster/${local.name}" = "shared"
  }
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
  count = length(aws_subnet.public)

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}
