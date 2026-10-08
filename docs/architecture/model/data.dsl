// The schemas of the Platform Database as its components (S091), and who
// reads and writes which. One PostgreSQL database, `meridian`, holds six
// schemas; a role per service and per job holds the grants. An arrow is drawn
// from the container whose service or job holds the grant, and its technology
// field names the role. Read from the GRANT and REVOKE statements of the
// migration files, not from a database's catalog.
//
// Every arrow here lies under a container's arrow to the database in
// containers.dsl, so no other view changes. The tags SchemaGrant and
// AuditTrail carry no style: the two views select their arrows by them.

!element meridian.platformDb {
    claimsSchema = component "claims" "Claims, decisions, triage proposals, briefs, reported documents and uploaded files, notes and approval requests; two views of open and decided claims." "PostgreSQL schema" "Layer Data"
    runtimeSchema = component "runtime" "Runs, and the checkpoints of the two hosts." "PostgreSQL schema" "Layer Data"
    gatewaySchema = component "gateway" "The usage ledger, budget counters and credits; the functions that expire ledger rows and audit events." "PostgreSQL schema" "Layer Data"
    policySchema = component "policy" "Policies and their claim history." "PostgreSQL schema" "Layer Data"
    knowledgeSchema = component "knowledge" "Policy wording chunks with their embeddings." "PostgreSQL schema, pgvector" "Layer Data"
    auditSchema = component "audit" "The audit events of every service, insert-only, and the claim trail view." "PostgreSQL schema" "Layer Data"
}

// A service and the schema that is its own
meridian.claimsApp -> meridian.platformDb.claimsSchema "Reads and writes claims, decisions, proposals, briefs, documents and files in; its sweep moves overdue claims in" "PostgreSQL, roles claims_api and claims_sweep" "Layer Workload,SchemaGrant"
meridian.claimsMcp -> meridian.platformDb.claimsSchema "Writes notes and approval requests to; reads claims and decisions from" "PostgreSQL, role claims_mcp" "Layer Workload,SchemaGrant"
meridian.runtime -> meridian.platformDb.runtimeSchema "Reads and writes runs and checkpoints in" "PostgreSQL, role agent_runtime" "Layer Services,SchemaGrant"
meridian.gateway -> meridian.platformDb.gatewaySchema "Writes usage and budget counters to; its upkeep job runs the expiry functions of" "PostgreSQL, roles model_gateway and gateway_upkeep" "Layer Services,SchemaGrant"
meridian.policyMcp -> meridian.platformDb.policySchema "Reads policies and claim history from" "PostgreSQL, role policy_mcp" "Layer Services,SchemaGrant"
meridian.knowledgeMcp -> meridian.platformDb.knowledgeSchema "Searches wording chunks in; its ingestion job writes them to" "PostgreSQL, roles knowledge_mcp and knowledge_ingest" "Layer Services,SchemaGrant"

// A service and another service's schema
meridian.claimsApp -> meridian.platformDb.runtimeSchema "Its sweep ends stranded runs and deletes their checkpoints in" "PostgreSQL, role claims_sweep" "Layer Workload,SchemaGrant,Crosses a schema"
meridian.claimsMcp -> meridian.platformDb.runtimeSchema "Reads the run of each call from" "PostgreSQL, role claims_mcp" "Layer Workload,SchemaGrant,Crosses a schema"
meridian.policyMcp -> meridian.platformDb.claimsSchema "Reads the claim of each call and the open and decided claims from" "PostgreSQL, role policy_mcp" "Layer Services,SchemaGrant,Crosses a schema"
meridian.policyMcp -> meridian.platformDb.runtimeSchema "Reads the run of each call from" "PostgreSQL, role policy_mcp" "Layer Services,SchemaGrant,Crosses a schema"
meridian.knowledgeMcp -> meridian.platformDb.policySchema "Reads the policy of each call from" "PostgreSQL, role knowledge_mcp" "Layer Services,SchemaGrant,Crosses a schema"
meridian.knowledgeMcp -> meridian.platformDb.claimsSchema "Reads the claim of each call from" "PostgreSQL, role knowledge_mcp" "Layer Services,SchemaGrant,Crosses a schema"
meridian.knowledgeMcp -> meridian.platformDb.runtimeSchema "Reads the run of each call from" "PostgreSQL, role knowledge_mcp" "Layer Services,SchemaGrant,Crosses a schema"

// The audit trail
meridian.claimsApp -> meridian.platformDb.auditSchema "Inserts audit events into; reads a claim's trail from" "PostgreSQL, roles claims_api and claims_sweep" "Layer Workload,AuditTrail"
meridian.claimsMcp -> meridian.platformDb.auditSchema "Inserts audit events into" "PostgreSQL, role claims_mcp" "Layer Workload,AuditTrail"
meridian.runtime -> meridian.platformDb.auditSchema "Inserts audit events into" "PostgreSQL, role agent_runtime" "Layer Services,AuditTrail"
meridian.gateway -> meridian.platformDb.auditSchema "Inserts audit events into, on a connection of its own" "PostgreSQL, role model_gateway" "Layer Services,AuditTrail"
meridian.policyMcp -> meridian.platformDb.auditSchema "Inserts audit events into" "PostgreSQL, role policy_mcp" "Layer Services,AuditTrail"
meridian.knowledgeMcp -> meridian.platformDb.auditSchema "Inserts audit events into" "PostgreSQL, roles knowledge_mcp and knowledge_ingest" "Layer Services,AuditTrail"
meridian.platformDb.auditSchema -> meridian.platformDb.claimsSchema "Its claim trail view joins claims from" "SQL view" "Layer Data,AuditTrail"
meridian.platformDb.auditSchema -> meridian.platformDb.runtimeSchema "Its claim trail view joins runs from" "SQL view" "Layer Data,AuditTrail"
meridian.platformDb.gatewaySchema -> meridian.platformDb.auditSchema "Its expiry functions delete old audit events from" "SQL functions" "Layer Data,AuditTrail"
