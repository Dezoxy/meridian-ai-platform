# Implemented as code, never applied (S079). Read from the deployed resources.
# None of these is a secret and none holds the account number. There is no
# kubeconfig and no token: the admin kubeconfig stays on the control plane, and
# the join command is in the parameter, which no output reads.

output "region" {
  description = "Region of the cluster, as the provider resolved it. Holds no account identifier."
  value       = aws_vpc.main.region
}

# Not sensitive: the address is the Elastic IP AWS gave this environment, it
# is in every certificate and every join command by design, and the owner needs
# to see it at the apply. Reaching the API server through it needs the one /32
# of api_access_cidr, which is the sensitive value.
output "control_plane_public_address" {
  description = "The control plane's Elastic IP, the address of the API server. Reachable on port 6443 from the address in api_access_cidr and the workers only."
  value       = aws_eip.control_plane.public_ip
}

output "control_plane_instance_id" {
  description = "Instance ID of the control-plane node (open a Session Manager session to it with this)."
  value       = aws_instance.control_plane.id
}

output "worker_instance_ids" {
  description = "Instance IDs of the worker nodes."
  value       = aws_instance.worker[*].id
}

output "join_parameter_name" {
  description = "Name of the Parameter Store parameter that carries the join command. The name, not the ARN: the ARN holds the account number."
  value       = aws_ssm_parameter.join_command.name
}
