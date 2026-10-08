// People and software systems outside the system in scope.
// The system in scope (Meridian AI Platform) is defined in containers.dsl.

claimant = person "Claimant" "Reports a motor or property loss and follows the claim. A synthetic persona in the demo."
adjuster = person "Claims Adjuster" "Reviews the agent's triage proposal and approves it, rejects it or requests documents." "Staff"
agentDeveloper = person "Agent Developer" "Builds a workload on the platform contract: graph, prompts, tools, evaluation cases." "Staff"
platformOperator = person "Platform Operator" "Runs the platform: budgets, cost and traces today; SLOs, incidents and upgrades as designed." "Staff"

identityProvider = softwareSystem "Microsoft Entra ID" "Issues tokens for staff and workload identities. Sign-in is designed: no route checks a token, a mock issuer runs on kind only as an opt-in add-on, and the gateway's live calls use the developer's own login." "External"
azureOpenAI = softwareSystem "Azure OpenAI" "Hosted OpenAI models on EU deployments in Sweden Central, called from a laptop and replayed on kind; West Europe is the designed fallback region." "External"
mistralFoundry = softwareSystem "Mistral on Azure AI Foundry" "EU-resident Mistral models on the data-zone SKU. A second provider, designed for milestone M2." "External,Designed"

adjuster -> identityProvider "Signs in with" "OIDC" "Person,Designed"
agentDeveloper -> identityProvider "Signs in with" "OIDC" "Person,Designed"
