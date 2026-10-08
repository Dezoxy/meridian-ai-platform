// The components of one container: the Agent Runtime (S090). A component is
// a responsibility of the running service, not a file: the file names are in
// the technology field so a reader can find the code. Three modules of the
// package are not components and the view register in ../README.md says why:
// sweep.py (its statements run in the Claims Triage App's scheduled sweep),
// toolprobe.py (a command) and the meters, failure words and settings.
//
// Every relationship from a component to another container sits under a
// container-level relationship in containers.dsl, so no other view changes.

!element meridian.runtime {
    runApi = component "Run API" "Starts, resumes and reads runs; refuses a calling service, tenant or agent the registry does not allow; picks each agent's host and loads the workload's graph or workflow factory from its entry point; writes the run's row before a host runs anything." "Python, FastAPI (app.py, host_wiring.py, graphs.py, settling.py)" "Layer Services"
    runRecords = component "Run Records" "The run's row, its lease and its resume-once claim, with an audit event in the same transaction as each change." "Python, psycopg (runs.py)" "Layer Services"
    langgraphHost = component "LangGraph Host" "Runs a workload's graph behind the host protocol: start, pause for approval, resume; checkpoints every step; one span per node." "Python, LangGraph (langgraph_host.py, checkpoints.py, tracing.py)" "Layer Services"
    agentFrameworkHost = component "Agent Framework Host" "Runs a workload's workflow behind the same host protocol, with the runtime's own checkpoint store, which writes JSON and nothing else." "Python, Microsoft Agent Framework (agent_framework_host.py, workflow_checkpoints.py)" "Layer Services"
    modelClient = component "Model Client" "A graph's or workflow's only way to a model: sets the tenant, agent and run headers, forwards the trace and stops at the run's call limit." "Python, httpx (model_client.py)" "Layer Services"
    toolClient = component "Tool Client" "A graph's or workflow's only way to a tool: refuses a tool outside the agent's allowlist, stops at the run's call limit and keeps one connection per tool server." "Python, MCP SDK (tool_client.py, tool_transport.py)" "Layer Services"
}

// Into the runtime
meridian.claimsApp -> meridian.runtime.runApi "Starts, resumes and reads runs through" "HTTP/JSON over mutual TLS" "Layer Workload"

// Inside the runtime
meridian.runtime.runApi -> meridian.runtime.runRecords "Records each leg's start, pause and end through" "Python call" "Layer Services"
meridian.runtime.runApi -> meridian.runtime.langgraphHost "Starts and resumes a graph agent's leg on" "Host protocol" "Layer Services"
meridian.runtime.runApi -> meridian.runtime.agentFrameworkHost "Starts and resumes a workflow agent's leg on" "Host protocol" "Layer Services"
meridian.runtime.langgraphHost -> meridian.runtime.modelClient "Lets the graph call a model only through" "Python call" "Layer Services"
meridian.runtime.langgraphHost -> meridian.runtime.toolClient "Lets the graph call a tool only through" "Python call" "Layer Services"
meridian.runtime.agentFrameworkHost -> meridian.runtime.modelClient "Lets the workflow call a model only through" "Python call" "Layer Services"
meridian.runtime.agentFrameworkHost -> meridian.runtime.toolClient "Lets the workflow call a tool only through" "Python call" "Layer Services"

// Out of the runtime
meridian.runtime.runApi -> meridian.registry "Loads agents, their hosts and tool allowlists from" "File read at startup" "Layer Services"
meridian.runtime.modelClient -> meridian.gateway "Requests completions through" "HTTP/JSON over mutual TLS, tenant, agent and run headers" "Layer Services"
meridian.runtime.toolClient -> meridian.policyMcp "Calls policy tools on" "MCP, Streamable HTTP over mutual TLS" "Layer Services"
meridian.runtime.toolClient -> meridian.knowledgeMcp "Calls retrieval tools on" "MCP, Streamable HTTP over mutual TLS" "Layer Services"
meridian.runtime.toolClient -> meridian.claimsMcp "Calls claim tools on" "MCP, Streamable HTTP over mutual TLS" "Layer Services"
meridian.runtime.runRecords -> meridian.platformDb "Writes run rows and their audit events to" "PostgreSQL" "Layer Services"
meridian.runtime.langgraphHost -> meridian.platformDb "Checkpoints graph state in" "PostgreSQL" "Layer Services"
meridian.runtime.agentFrameworkHost -> meridian.platformDb "Checkpoints workflow state as JSON in" "PostgreSQL" "Layer Services"
