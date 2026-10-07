# Read from the deployed resources, not from the variables. None of these is a
# secret and none holds the account number: the cluster endpoint and the
# database endpoint are host names without it. The repository's URL begins with
# the account number (<account>.dkr.ecr.<region>.amazonaws.com/<name>), so it
# is not an output: the repository's name is, and the URL is
# `aws ecr describe-repositories` or the console, in the owner's session.

output "region" {
  description = "Region of the cluster, as the provider resolved it. Holds no account identifier."
  value       = aws_eks_cluster.main.region
}

output "cluster_name" {
  description = "Name of the EKS cluster. Holds no account identifier."
  value       = aws_eks_cluster.main.name
}

output "cluster_endpoint" {
  description = "HTTPS endpoint of the cluster's API server (a host name with a random identifier, not the account number). Reachable from the address in api_access_cidr only."
  value       = aws_eks_cluster.main.endpoint
}

output "repository_name" {
  description = "Name of the ECR repository. Its URL begins with the account number and is not printed: build it from the account and the Region, in the owner's session."
  value       = aws_ecr_repository.meridian.name
}

output "database_endpoint" {
  description = "Host name of the PostgreSQL instance (reachable from the nodes only). Holds no account identifier and no secret: the master password is in the secret RDS manages."
  value       = aws_db_instance.main.address
}

output "database_port" {
  description = "Port of the PostgreSQL instance."
  value       = aws_db_instance.main.port
}

output "workload_secret_name" {
  description = "Name of the empty Secrets Manager secret that the workload's service account may read. The name, not the ARN: the ARN holds the account number."
  value       = aws_secretsmanager_secret.workload.name
}
