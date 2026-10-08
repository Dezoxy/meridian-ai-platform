"""The two words of a run's end that the runtime, the Claims API and the sweep
share (S052, S082). A module of its own that imports nothing: the sweep job
loads it, and so does the Claims API, and neither may load the other's stack."""

# How long a run may stay ``Running`` before a resume may take it over: a leg
# that died, or whose last status write failed twice, leaves the run so. Well
# above the longest a live leg can take (four model calls, each at most the
# 30 s deadline plus one 30 s read timeout, and sixteen tool calls of 10 s: 400 s;
# a test multiplies the constants), so a live leg is not taken over. Not a
# ceiling for one case: model-call response headers that trickle (each wait
# under the read timeout; httpx has no timeout for a whole request). A leg
# that outlives the lease writes nothing over the run (its end matches its own
# claim, ``runs.py``); its tool calls bind until it ends (T-10). The sweep
# ends an unfinished run only after the same time (``runs.py`` reads it from
# ``runtime/sweep.py``, which takes it from here, so the runtime and the sweep
# cannot disagree).
RUNNING_LEASE_SECONDS = 600
# The reason word of the audit event the sweep writes for a run it ends.
ABANDONED_REASON = "abandoned"
