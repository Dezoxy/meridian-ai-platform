// People and software systems outside the system in scope.
// The system in scope (Meridian AI Platform) is defined in containers.dsl.

claimant = person "Claimant" "Reports a motor or property loss and follows the claim. A synthetic persona in the demo."
adjuster = person "Claims Adjuster" "Reviews the agent's triage proposal and approves it, rejects it or requests documents." "Staff"
agentDeveloper = person "Agent Developer" "Builds a workload on the platform contract: graph, prompts, tools, evaluation cases." "Staff"
platformOperator = person "Platform Operator" "Runs the platform: SLOs, budgets, incidents, upgrades." "Staff"

identityProvider = softwareSystem "Microsoft Entra ID" "Issues tokens for staff and workload identities. A mock OIDC issuer stands in on kind." "External"
azureOpenAI = softwareSystem "Azure OpenAI" "Hosted OpenAI models on EU deployments: Sweden Central primary, West Europe fallback." "External"
mistralFoundry = softwareSystem "Mistral on Azure AI Foundry" "EU-resident Mistral models on the data-zone SKU. Third provider from milestone M2." "External"

adjuster -> identityProvider "Signs in with" "OIDC" "Person"
agentDeveloper -> identityProvider "Signs in with" "OIDC" "Person"
