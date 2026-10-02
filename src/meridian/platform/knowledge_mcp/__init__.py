"""The knowledge store, its ingestion and its hybrid search: policy wordings as
clause chunks with vectors (S012). The tool server that serves the search comes
later (S046).
"""

# The agent the ingestion runs as; its embedding calls carry this name, so the
# gateway's audit rows say who asked.
INGESTION_AGENT = "knowledge-ingestion"
