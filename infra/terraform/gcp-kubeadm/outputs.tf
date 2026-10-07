# Implemented as code, never applied (S079). Read from the deployed resources.
# None of these is a secret and none holds the project's ID or number: a
# resource's self link and an instance's ID path begin with the project, so
# neither is an output. There is no kubeconfig and no token: the admin kubeconfig
# stays on the control plane, and the join command is in the secret, which no
# output reads.

output "region" {
  description = "Region of the cluster. Holds no project identifier."
  value       = google_compute_subnetwork.nodes.region
}

# Not sensitive: the address is the one Google Cloud reserved for this
# environment, it is in the API server's certificate by design, and the owner needs
# to see it at the apply. Reaching the API server through it needs the one /32 of
# api_access_cidr, which is the sensitive value.
output "control_plane_public_address" {
  description = "The control plane's reserved public address, a name of the API server's certificate. Reachable on port 6443 from the address in api_access_cidr only."
  value       = google_compute_address.control_plane.address
}

output "control_plane_internal_address" {
  description = "The control plane's internal address, which the workers join at and the nodes' kubeconfigs name."
  value       = local.control_plane_internal_address
}

output "control_plane_instance_name" {
  description = "Name of the control-plane instance (the name a person reaches it by once OS Login or Identity-Aware Proxy is turned on)."
  value       = google_compute_instance.control_plane.name
}

output "worker_instance_names" {
  description = "Names of the worker instances."
  value       = google_compute_instance.worker[*].name
}

output "join_secret_name" {
  description = "Name of the Secret Manager secret that carries the join command. The name, not the resource's path: the path holds the project."
  value       = google_secret_manager_regional_secret.join_command.secret_id
}
