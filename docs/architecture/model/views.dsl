// Each view answers one question for one audience. Split a view before trying
// to style it into readability; budgets are in the architecture-views skill.

systemContext meridian "SystemContext" "What does the platform provide, who uses it, and which external systems does it depend on?" {
    include *
    autoLayout lr
}

// Building blocks of the platform and the workload. Governance-only elements
// (observability, evaluation, registry, Key Vault, the second provider) have
// their own view; left in, their arrows cross every other box.
container meridian "Containers" "What are the building blocks of the platform and the claims-triage workload, and how do they connect?" {
    include *
    exclude meridian.observability meridian.evals meridian.registry meridian.keyVault mistralFoundry agentDeveloper platformOperator
    autoLayout tb 300 150
}

container meridian "Governance" "How are models, policies, budgets and evidence governed?" {
    include meridian.gateway meridian.registry meridian.keyVault meridian.platformDb meridian.evals meridian.observability meridian.runtime
    include agentDeveloper platformOperator azureOpenAI mistralFoundry
    autoLayout tb 300 150
}

dynamic meridian "ClaimsTriage" "What happens between a claim being submitted and a triage proposal waiting for an adjuster?" {
    claimant -> meridian.ingress "Submits a claim"
    meridian.ingress -> meridian.claimsApp "Routes the claim"
    meridian.claimsApp -> meridian.runtime "Starts a triage run"
    meridian.runtime -> meridian.policyMcp "Looks up the policy and its claim history"
    meridian.runtime -> meridian.knowledgeMcp "Retrieves coverage terms with citations"
    meridian.runtime -> meridian.gateway "Asks whether an exclusion applies, when the rules need it; rules then decide the route"
    meridian.gateway -> azureOpenAI "Sends the redacted prompt to an EU deployment (replayed on kind)"
    meridian.runtime -> meridian.platformDb "Checkpoints every step and, for a referred claim, pauses the run for approval"
    autoLayout lr
}

dynamic meridian "ClaimsApproval" "What happens when an adjuster decides on a paused triage proposal?" {
    adjuster -> meridian.ingress "Approves, rejects or asks for documents"
    meridian.ingress -> meridian.claimsApp "Routes the decision"
    meridian.claimsApp -> meridian.platformDb "Records the decision and its audit event and moves the claim"
    meridian.claimsApp -> meridian.runtime "Resumes the paused run"
    meridian.runtime -> meridian.claimsMcp "Reads the recorded decision and adds a note"
    meridian.runtime -> meridian.platformDb "Ends the run, writes its audit event and deletes its checkpoints"
    autoLayout lr
}
