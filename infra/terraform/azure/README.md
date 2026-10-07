# Azure platform module

This module is the Azure environment of one demo day, written beside the
persistent foundation: so far it declares a resource group of its own and a
virtual network with three subnets, and the cluster, the registry, the database
and the identities come in later changes. It has never been planned and never
applied, and nothing in it has met Azure. No command plans, applies or removes
it yet, on purpose. Check it by hand in this directory with
`terraform fmt -check`,
`terraform init -backend=false -lockfile=readonly` and
`terraform validate`, none of which needs an Azure sign-in, and delete the
`.terraform` directory afterwards.

## What it declares

Not written yet: a later change of S020 fills this section in.

## Inputs

Not written yet: `variables.tf` holds every input, with its description.

## Outputs

Not written yet: the module declares no output so far.

## The subscription pin

Not written yet: `main.tf` holds the pin and says what it checks.

## The checks that exist

Not written yet: the three commands above are all there is.

## What does not exist, on purpose

Not written yet: no plan, apply or removal command exists for this module.

## What `validate` and the scan cannot see

Not written yet: a later change lists what only an apply would show.

## What a production environment sets differently

Not written yet: a later change of S020 writes it.

## The Region list

Not written yet: `variables.tf` holds the list and where it came from.

## Cost

Not written yet: it is read from Azure's public price list, with its date.
