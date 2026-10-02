"""The knowledge store, its ingestion, its hybrid search and the tool server that
serves the search: policy wordings as clause chunks with vectors (S012, S046).
"""

# The registry's ID of the tool server, which is also its service name.
SERVICE_NAME = "knowledge-mcp"

# The agent the ingestion runs as; its embedding calls carry this name, so the
# gateway's audit rows say who asked.
INGESTION_AGENT = "knowledge-ingestion"
