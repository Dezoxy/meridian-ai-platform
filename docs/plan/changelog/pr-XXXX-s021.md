- **#XXXX, 2026-10-08:** S021 `doing`, the first part of the staff half:
  built, and wired to no route. The step is cut to the staff half (Keycloak
  on kind, four roles, the app layer first behind a switch that is off by
  default, the tenant from the token, the actor on the decision row), with
  the owner's answers and the design's ten decisions in its step file; S093
  (claimants sign in as themselves) and S094 (sign-in at the edge) are rows,
  `todo`; two backlog rows moved to S093 and one is new (TLS to the issuer on
  kind, S094). Built and tested, as modules that no route calls: the token
  check, the key-set client, the session cookie, the route guard and the
  pages' authorization-code flow with PKCE (seven modules of
  `platform/common`), three more import-linter contracts (nine). On kind,
  local only: Keycloak 26.8.0 pinned by digest as an add-on that is off
  unless switched on and is not part of plain `make up` (the owner, "Keep it,
  opt-in only (Recommended)"), a generated realm with a cast of seven
  synthetic test people (six staff in four groups, one role a group, and one
  person in no group; the owner, "Users, one organisation"),
  `identity.sh users`, and one guarded command that prints their disposable
  passwords, `make identity-passwords`. Seen on the cluster in four runs (the
  add-on up, a second run keeping its Secrets, a rotation rolling the pod,
  the cast listed, smoke 62 of 62 with the switch on). Reviews: two security
  reviews and a re-check of the module and a security review of the flow
  ended `Y4 MAY WIRE IT`; two infra reviews of the add-on said approve with
  fixes, and the fixes are in. **Not built:** any route's use of the guard or
  the flow (Y4), the switch `MERIDIAN_SIGNIN` (no code reads it), the actor
  and the tenant (Y6), the scripts' tokens (Y7), the decision record (Y8),
  the Azure half; no Entra tenant has issued a token. **Not seen:** a sign-in
  or a token on the cluster, and a browser. The threat model's rows T-114 to
  T-121 are new and implemented in part, T-05 and T-06 are implemented in
  part, T-32 and T-69 are amended; the data classification has rows amended
  and two new; the register holds 121 threats.
