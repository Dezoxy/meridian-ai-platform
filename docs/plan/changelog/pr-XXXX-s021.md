- **#XXXX, 2026-10-08:** S021 `doing`, built in part and wired to
  nothing: the step is cut to the staff half (Keycloak on kind, four roles,
  the app layer first behind a switch that is off by default, the tenant from
  the token, the actor on the decision row), with the owner's five answers and
  the design's ten decisions in its Part C section; S093 (claimants sign in as
  themselves) and S094 (sign-in at the edge) are new rows, `todo`; two backlog
  rows moved to S093. Built and tested, as modules that no route calls: the
  token check, the key-set client, the session cookie and the route guard (four
  modules of `platform/common`, ten test files, 1,543 tests passing under
  `tests/meridian/common/` and the import-contract tests), three more
  import-linter contracts (nine), the Keycloak 26.8.0 pin, a realm generator
  and an opt-in rig that ran against the pinned image (6 passed). Two security
  reviews and a re-check ended `Y4 MAY WIRE IT`. Keycloak is the local
  stand-in, an add-on that is off unless switched on and not part of plain
  `make up` (the owner, "Keep it, opt-in only (Recommended)"). Not built: any
  route's use of the guard (Y4), the page flow (Y3), Keycloak on kind (Y2b),
  the switch `MERIDIAN_SIGNIN` (no code reads it); no Entra tenant has issued a
  token. The threat model's rows T-114 to T-121 are new and implemented in
  part (T-112 is S071's), T-05 is implemented in part, and T-06, T-32 and T-69
  are amended; the data classification has rows amended and two new; the
  register holds 120 threats. The whole suite: FINAL-SUITE-RESULT.
