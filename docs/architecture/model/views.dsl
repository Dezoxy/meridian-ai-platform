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

// The deployment views are of designed environments and say so in their titles.
deployment meridian aws "DeploymentAws" "Where would the platform run on AWS, and which infrastructure would it share?" {
    title "Meridian AI Platform: deployment on AWS in eu-central-1, DESIGNED and not built (what the cloud changes)"
    include *
    autoLayout tb 300 150
}

deployment meridian gcp "DeploymentGcp" "Where would the platform run on Google Cloud, and what does the cloud change?" {
    title "Meridian AI Platform: deployment on Google Cloud in europe-west3, DESIGNED and not built (what the cloud changes)"
    include *
    autoLayout tb 300 150
}

deployment meridian azure "DeploymentAzure" "Where would the platform run on Azure, and what does the cloud change?" {
    title "Meridian AI Platform: deployment on Azure in Sweden Central, DESIGNED and not built (what the cloud changes)"
    include *
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

// Inside one container. `include *` also draws the arrows between the
// neighbours, which the Containers view has; they are excluded so that every
// arrow here starts or ends at a component.
component meridian.runtime "RuntimeComponents" "Which responsibilities sit inside the Agent Runtime, and which of them is the only way out to a model, a tool and the database?" {
    include *
    exclude meridian.registry
    exclude "meridian.claimsApp -> meridian.platformDb"
    exclude "meridian.gateway -> meridian.platformDb"
    exclude "meridian.policyMcp -> meridian.platformDb"
    exclude "meridian.knowledgeMcp -> meridian.platformDb"
    exclude "meridian.claimsMcp -> meridian.platformDb"
    exclude "meridian.knowledgeMcp -> meridian.gateway"
    autoLayout tb 300 150
}

// Inside the database: its schemas, and who reads and writes which. Two views,
// because every service writes the audit trail and those six arrows would
// cross the rest. Each selects its arrows by a tag of data.dsl. The tall
// rank separation keeps a service's arrow from passing through its
// neighbours: at 300 several did.
component meridian.platformDb "DataOwnership" "Which service owns which schema of the Platform Database, and which reads and writes cross into another service's schema?" {
    include *
    exclude meridian.platformDb.auditSchema
    exclude "relationship.tag!=SchemaGrant"
    autoLayout tb 1600 100
}

// Without the gateway schema, whose expiry functions delete old audit events:
// with it the layout put the services beside the database and their arrows
// through one another. The model has that arrow; DataOwnership shows the
// gateway's upkeep job running those functions.
component meridian.platformDb "AuditTrail" "Which services write the audit trail, and what reads it?" {
    include "->meridian.platformDb.auditSchema->"
    exclude meridian.platformDb.gatewaySchema
    exclude "relationship.tag!=AuditTrail"
    autoLayout tb 600 100
}
