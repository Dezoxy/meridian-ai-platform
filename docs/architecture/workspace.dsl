// Entry point for the architecture model. Keep this file at docs/architecture/:
// !docs and !adrs paths must be this directory or a subdirectory of it.
// Model fragments live in model/ and are pulled in with !include (order matters).
workspace "Meridian AI Platform" "Architecture model for the Meridian AI Platform: an enterprise platform for building, running and governing LLM agents, with a claims-triage reference workload. Target architecture as of 2026-09-29; nothing is deployed yet." {

    !identifiers hierarchical

    configuration {
        scope softwaresystem
    }

    // Attached to the workspace, not the software system, so both paths resolve
    // unambiguously against this file.
    !docs overview
    !adrs decisions

    properties {
        // Docs and ADRs are attached at workspace level (above), so the
        // per-system documentation/decision inspections do not apply here.
        "structurizr.inspection.model.softwaresystem.documentation" "ignore"
        "structurizr.inspection.model.softwaresystem.decisions" "ignore"
    }

    model {
        !include model/people-systems.dsl
        !include model/containers.dsl
    }

    views {
        !include model/views.dsl
        !include model/styles.dsl

        // Mermaid in the Documentation tab, part 1 of 2. See part 2.
        properties {
            "mermaid.url" "https://mermaid.ink"
            "mermaid.format" "svg"
            "mermaid.compress" "false"
        }
    }

    // Mermaid in the Documentation tab, part 2 of 2: on. The plugin turns
    // every Mermaid fence in the documents and ADRs into an image that the
    // reader's browser fetches from the mermaid.url server, so every page
    // view sends the diagram source there. Acceptable here: the repository
    // is public and synthetic. Some DNS blocklists (HaGeZi Ultimate) block
    // mermaid.ink; allowlist it there. GitHub and the PDF render Mermaid
    // without it. Comment out both parts together to show the source instead.
    !plugin com.structurizr.dsl.plugin.documentation.Mermaid

}
