# AWS module for a self-managed Kubernetes cluster

A second Terraform root module, beside [the managed one](../aws/README.md): a
cluster whose control plane runs on plain virtual machines and is brought up
by kubeadm (one control-plane node, two workers, a network plugin, a join
command passed through one Parameter Store parameter). Status: **implemented
as code**, checked by `terraform validate` and by tests on its text and on its
two boot scripts against stand-in programs; **never planned and never
applied**. No command creates it yet: there is no `make` target and no wrapper
for it, and those come in a later change. The full text (what it creates, the
state, what a check cannot see, the apply and the removal) comes with the
step's documents.
