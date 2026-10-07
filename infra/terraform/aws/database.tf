# RDS for PostgreSQL in the VPC, reachable from the nodes alone. The chart on
# kind runs CloudNativePG, which makes the eleven roles, their Secrets and the
# vector extension; none of that is made here. This module makes the instance.
# CREATE EXTENSION vector needs a database connection, so it cannot be done in
# Terraform: the second half records whether it works on this instance.

resource "aws_db_subnet_group" "main" {
  name        = local.name
  description = "Both public subnets of the test VPC; the instance itself is not publicly accessible."
  subnet_ids  = aws_subnet.public[*].id
}

# Managed node groups put their nodes in the cluster security group that EKS
# makes (the EKS page "Amazon EKS security group requirements": the network
# interfaces of the nodes of any managed node group are in it). That group is
# the node group's security group here, and it is the only source this one
# admits. It has no rule from the internet.
resource "aws_security_group" "database" {
  name        = "${local.name}-database"
  description = "PostgreSQL from the EKS nodes only."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-database" }
}

resource "aws_vpc_security_group_ingress_rule" "database_from_nodes" {
  security_group_id            = aws_security_group.database.id
  referenced_security_group_id = aws_eks_cluster.main.vpc_config[0].cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "PostgreSQL from the EKS cluster security group, which the nodes carry."
}

resource "aws_db_instance" "main" {
  identifier     = local.name
  engine         = "postgres"
  engine_version = var.database_engine_version
  instance_class = var.database_instance_class

  # An initial database named as the chart's connection strings expect. The
  # roles the chart's services connect as are not made here.
  db_name  = "meridian"
  username = "meridian_master"

  # RDS makes and owns the master password in Secrets Manager: no password is
  # in a variable, in the repository or chosen by this module. The RDS page
  # "Password management with Amazon RDS and AWS Secrets Manager" says: "If you
  # delete a DB instance that manages a secret in Secrets Manager, the secret
  # and its associated metadata are also deleted." So the secret goes with the
  # instance; whether a recovery window applies to that deletion is not stated
  # there. RDS also rotates it, every seven days by default.
  manage_master_user_password = true

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true # the AWS-managed key; production names a customer-managed one

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.database.id]
  publicly_accessible    = false

  # Single zone. Production sets multi_az = true.
  multi_az = false

  auto_minor_version_upgrade = true
  apply_immediately          = true

  # PostgreSQL 17 is in standard support. This keeps the instance from being
  # moved into paid extended support when its version passes the end of
  # standard support (the provider's page: the default is extended support
  # enabled).
  engine_lifecycle_support = "open-source-rds-extended-support-disabled"

  # One day of automated backups, and they are removed with the instance.
  # Retained backups and snapshots bill until they are deleted (ADR 6).
  # Production keeps seven to thirty-five days and retains the backups.
  backup_retention_period  = 1
  delete_automated_backups = true
  copy_tags_to_snapshot    = true

  # This environment is made to be removed: no deletion protection, no final
  # snapshot. Production sets deletion_protection = true and
  # skip_final_snapshot = false with a final_snapshot_identifier, and then
  # removing the database is a deliberate two-step act.
  deletion_protection = false
  skip_final_snapshot = true
}
