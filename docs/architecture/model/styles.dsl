// This repository's styles. The shared meanings (people, systems, external,
// shapes, security markings, arrows) and the approved palette come from
// styles-shared.dsl, copied unchanged from architecture-base. This file only maps
// this system's layers and groups onto palette families, plus local extras.
// Tag order matters on an element: layer tag first, security marking last.

styles {
    !include styles-shared.dsl

    // Layers: Edge purple, control-plane Services green, Workload teal, Data slate.
    element "Layer Edge" {
        background ${PURPLE_FILL}
        stroke ${PURPLE_STROKE}
    }
    relationship "Layer Edge" {
        color ${PURPLE_STROKE}
    }
    element "Layer Services" {
        background ${GREEN_FILL}
        stroke ${GREEN_STROKE}
    }
    relationship "Layer Services" {
        color ${GREEN_STROKE}
    }
    element "Layer Workload" {
        background ${TEAL_FILL}
        stroke ${TEAL_STROKE}
    }
    relationship "Layer Workload" {
        color ${TEAL_STROKE}
    }
    element "Layer Data" {
        background ${SLATE_FILL}
        stroke ${SLATE_STROKE}
    }
    relationship "Layer Data" {
        color ${SLATE_STROKE}
    }

    // Groups mark planes and the edge; their tint follows the layer they hold.
    element "Group:Edge (internet-facing)" {
        color ${PURPLE_LABEL}
        stroke ${PURPLE_STROKE}
        background ${PURPLE_FRAME}
    }
    element "Group:Control plane" {
        color ${GREEN_LABEL}
        stroke ${GREEN_STROKE}
        background ${GREEN_FRAME}
    }
    element "Group:Workload plane" {
        color ${TEAL_LABEL}
        stroke ${TEAL_STROKE}
        background ${TEAL_FRAME}
    }

    // Local extras.
    element "Staff" {
        background #ede9fe
    }
    element "Vault" {
        shape Folder
    }
}
