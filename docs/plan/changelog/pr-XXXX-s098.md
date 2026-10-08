- **#XXXX, 2026-10-08:** S098 Demo claims on kind, written and tested without a
  cluster; **not run on the cluster yet**. `make demo-seed`
  (`infra/kind/demo-seed.sh`) posts the first `COUNT` synthetic claims (40 by
  default, at most 47) through the edge one at a time, `PACE_SECONDS` apart, and
  prints a line a claim and the counts by state, so the adjuster's and the
  claimant's pages show content. Kind only, through the Claims API only (each
  claim has its audit trail and its triage run), the gateway's replay mode is
  simulated so it costs nothing, safe to run twice (an existing claim is
  skipped, one with other content is named, a failed triage is tried again),
  decides nothing. It refuses a Docker engine that is not local, a cluster held
  by another checkout (it only reads the record), under 2,500 MB of free memory
  and a running test database container. A failed triage is counted and shown
  and does not stop the run. From a reading of the sweep's findings line in
  `make smoke`: it asserts that the six series exist and compares no value, so
  seeded claims cannot make it fail. `demo.sh`, the Claims API, the chart, the
  image and the synthetic data are unchanged. Not here: a second insurer, a
  sign-in, claimants' accounts (S093), the test staff (S021).
