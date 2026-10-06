#!/usr/bin/env bash
# Prove the local platform works end to end: `make smoke`. Changes nothing apart
# from four short-lived Jobs in meridian (unique names; each ends at an
# activeDeadlineSeconds even when its pod never starts, and is removed by
# ttlSecondsAfterFinished once it has finished or failed), two short-lived Pods
# of the network policy check (one
# in meridian, one in default; unique names, each deleted when its line ends and
# again by the EXIT trap), one CertificateRequest in default of
# the certificate policy check (unique name, a request the issuer must refuse,
# deleted as soon as it is read and again by the EXIT trap) and, at most once
# per throttle window per tool server, the refusal's audit row that the tool
# check below causes. The service identity check (9) also leaves two refusal
# rows in the audit table on each run (a 401's and a 403's, which the gateway
# throttles to one a minute per reason) and puts a throwaway key in the probe
# pod's /tmp, which the probe removes when it ends; and the checks that read
# Grafana (4, 5 and 11) read its admin Secret, never printing it.
#   1. edge:      laptop -> 127.0.0.1:8088 -> kind port mapping -> NodePort -> Envoy
#   2. database:  the database's NetworkPolicy names the API server's address
#                 (S063, first line, read from two objects and run even when no
#                 primary is found); pgvector is installed in platform-db, in
#                 the `app` database and
#                 in the `meridian` database; and three lines for the stores of
#                 the `meridian` database, read in the primary's pod: the policy
#                 store holds policies (policy.policies, which the seed Job
#                 fills), the knowledge store holds chunks (knowledge.chunks,
#                 counted with the query deploy.sh counts with), and the
#                 migrations ledger's newest file (public.meridian_migrations)
#                 is the newest file under src/meridian/platform/migrations of
#                 the checkout this script runs from, so a cluster deployed
#                 from another checkout says so. One SKIP line replaces the three
#                 while the migrations ledger table is not there, which is when
#                 nothing was ever migrated (`make up` alone). A ledger beside a
#                 store's missing table (a half-applied or renamed migration) is
#                 that store's FAIL, naming the table; an answer of the probe
#                 that is not in its form is one FAIL; a failed read of the
#                 database keeps its message, cleaned and cut to 160 characters,
#                 in the FAIL line (names of relations and roles, never a row).
#                 What the lines do not prove: a count above zero says
#                 the seed and the ingestion wrote something, not what or how
#                 much (the claims and runs tables are not read: `make demo`
#                 writes them), nor that the chunks are the running image's (the
#                 ingestion Job of the image's tag, kept by deploy.sh, is that
#                 proof); and the ledger shows which migrations were applied,
#                 not that the services run the code that matches them.
#                 The address line (S063) compares the CIDRs of the policy
#                 platform-db's rule for port 6443 with the addresses of the
#                 `kubernetes` EndpointSlice in `default`, the two reads `make
#                 up` and `make deploy` use (common.sh): equal is a PASS; a
#                 difference is a FAIL that says "the API server's address
#                 changed: run make up" (Docker restarted, the node got another
#                 address, and the database loses the API server until then);
#                 a read that failed, a policy that is missing and an endpoint
#                 with no IPv4 address are FAIL lines that say so and never say
#                 "changed". It prints after `make up` alone, as the policy and
#                 the endpoint exist then. What it does not prove: that the path
#                 is closed to every other address, or that the network plugin
#                 enforces an ipBlock rule after the Service's address is
#                 translated to the node's (kindnet; a probe of it is by hand,
#                 see infra/kind/README.md: it needs a listener on 6443 that
#                 smoke does not start); and not that the instance manager is
#                 healthy (the Cluster's status says that).
#   3. tools:    one call per MCP tool server through the runtime's own client,
#                 run in the agent-runtime pod (so with the addresses the runtime
#                 was given), with a run ID that does not exist: each server must
#                 refuse it as `unknown-run`. Skipped, not failed, while the
#                 Meridian services are not deployed (`make deploy`).
#   4. telemetry: six lines (S063). The first two are about TLS and do not need
#                 the Meridian services: the ConfigMap `telemetry-ca` in
#                 `meridian`, which the six services mount to trust the
#                 collector, holds the certificate its authority has now (the
#                 fingerprint of its ca.crt equals that of tls.crt of the Secret
#                 `telemetry-ca` in `observability`; SKIP while the Secret is
#                 not there, FAIL "run make up" when the ConfigMap is missing or
#                 stale: the services read the mounted file at each new
#                 connection and the kubelet refreshes it, so `make up` is the
#                 whole remedy and no restart follows; only the first twelve hex
#                 digits of a fingerprint are
#                 printed, and only that one field of the Secret is read); and a
#                 push in clear text to the collector's port is not accepted: a
#                 Job pod in `meridian` (so one the policies admit, and the
#                 refusal is the TLS listener's, not a NetworkPolicy's) sends
#                 plain HTTP with the database image's bash and the line says
#                 what came back (a 400 from a Go TLS listener, which is what
#                 the collector answered on 2026-10-06, or a connection closed
#                 with no answer, is a PASS; any other status is a FAIL, a 2xx
#                 because the receiver took the push and the rest because
#                 something answered HTTP in clear text, which does not show TLS
#                 (a plain receiver answers 404 or 503 too); a timeout, a
#                 refused connection or silence is a FAIL that proves nothing,
#                 told apart from a refusal by the listener). Then telemetrygen
#                 sends one trace, one log and one metric over OTLP to the
#                 collector, over TLS with the authority's certificate
#                 (--ca-cert; no --otlp-insecure); each is then read back
#                 through Grafana's datasource proxy (Tempo, Loki, Prometheus),
#                 the way an owner would see it. Since S063 these lines prove the TLS path end
#                 to end for telemetrygen (the collector serves TLS only, so a
#                 trace, a log and a metric that arrive came over it), and the
#                 cost series of check 5, which the Model Gateway's own exporter
#                 pushes, proves it for a service (a series that arrives was
#                 sent over TLS and verified against the authority's file: an
#                 exporter that did not trust it would drop its metrics). What
#                 they do not prove: that a service checks the collector's name
#                 and refuses another authority (the exporters' own test,
#                 tests/meridian/common/test_otlp_tls.py, does), that every
#                 service has the file (`make demo`'s trace with a span of each
#                 service does), and that the collector's own hops to Tempo, Loki
#                 and Prometheus are encrypted (they are not). The Jobs run in
#                 `meridian`, not in `observability` (S063), and push over
#                 HTTPS to port 4318, the port and the namespace the six
#                 services push from: the
#                 collector's ingress admits the pods of `meridian` on 4318 and
#                 nothing else (manifests/observability-networkpolicy.yaml), and
#                 smoke's own Jobs get the egress they need from
#                 manifests/smoke-networkpolicy.yaml, which `make up` applies
#                 (the chart's default-deny would cut them off without it). What
#                 each PASS line prints of an answer (the
#                 trace ID, the log line, the series count) goes through
#                 clean_lines and is cut to 120 characters: anyone who can push
#                 a log line to the collector chooses its text.
#   5. cost panel: four lines. Grafana serves the provisioned dashboard
#                 "Meridian: Model Gateway tokens and cost", its queries equal
#                 the file's and every one of them runs in Prometheus (a
#                 dashboard with no query, or a target with no expression, is a
#                 FAIL, and targets of panels nested in rows count); once the
#                 gateway has settled a call since it started (the ledger says
#                 so), Prometheus holds its tokens, cost and calls series (the
#                 series line is skipped while the services are not deployed,
#                 `make deploy`, or the gateway has settled nothing yet, `make
#                 demo`, and fails when the gateway is not available); and
#                 Grafana's service account may not read Secrets in meridian or
#                 observability; and (S063) kube-state-metrics' service account
#                 may not get, list or watch Secrets in meridian or cert-manager
#                 (`kubectl auth can-i --as`, six read-only questions; the line
#                 needs no deployed service and no Grafana forward, so it prints
#                 after `make up` alone). The last line does not prove that the
#                 Prometheus operator's rights are narrow: they are not, the
#                 chart gives it a ClusterRole that reads, creates and changes
#                 Secrets in every namespace and has no value to narrow it
#                 (threat model T-68, still open for the operator); nor that
#                 kube-state-metrics' other rights are small (it still lists
#                 and watches ConfigMaps, Pods and the rest cluster-wide).
#   6. adjuster pages: three lines, through the edge as demo.sh reaches the Claims
#                 API. The queue page answers 200 with the Content-Security-
#                 Policy (frame-ancestors 'none', default-src 'none') and the
#                 synthetic-data line; a decision posted with a foreign Origin is
#                 refused with 403 before any claim is looked up; the claimant's
#                 start page answers 200 with the same policy and the banner's
#                 fictional-data sentence (T-04), and changes no claim. Skipped
#                 while the Meridian services are not deployed (`make deploy`).
#   7. sweep:     one line, read-only. The CronJob meridian-sweep exists, and the
#                 last of its Jobs to finish (the scheduled ones and any made by
#                 hand) succeeded; the line prints when it finished. It fails
#                 when the last one failed (with its reason), when the CronJob
#                 was last scheduled more than three periods (15 minutes) after
#                 that Job finished with nothing running, when that Job finished
#                 more than three periods before now with nothing running (the
#                 schedule stopped after a success), when no Job of it is left
#                 (a finished Job is removed a day after it finished) and it was
#                 last scheduled more than three periods before now with nothing
#                 running, and when it was never scheduled although it was
#                 created more than three periods ago.
#                 The period is read from the CronJob's own .spec.schedule when
#                 that is "*/N * * * *" (N from 1 to 59), else it is
#                 SWEEP_PERIOD_SECONDS. Skipped while the Meridian services are
#                 not deployed (`make deploy`), while no Job of it has finished
#                 yet, while a CronJob that never ran is too young to judge,
#                 while the CronJob was deployed less than one period ago (a
#                 Job of the CronJob it replaced is old, not overdue), and while
#                 it is suspended (.spec.suspend: it makes no runs, so none is
#                 overdue). "Now" is the database's clock: the primary's now(),
#                 read as whole seconds, which this script already trusts for
#                 the audit row of check 9. Not this laptop's `date`: it runs
#                 on a machine whose clock is not the cluster's (see deploy.sh).
#                 Not the controller manager's Lease: one more API object to
#                 trust, for no gain over a clock that is already trusted. The
#                 line fails when that clock cannot be read, rather than judging
#                 with the timestamps alone. What it does not prove: that the
#                 sweep did its work (only that a Job finished), and a database
#                 whose clock is wrong would be believed.
#   8. network policy: five lines (S019, S062, S063). Each opens a TCP connection and
#                 nothing more; a path that no rule allows is a PASS only when it
#                 times out, and a connection refused or a name that does not
#                 resolve is a FAIL, never "blocked". A policy that is missing,
#                 or too wide, makes a denied path answer, so each denied line
#                 can fail. Run in this order, the control first:
#                 - the control: from the Claims API's pod to the Agent Runtime,
#                   which the Claims API's policy and the Agent Runtime's both
#                   name. It must be reached. The probe, the name resolution and
#                   the pod's egress to a named peer work, so a "blocked" below
#                   is not a broken probe. If it fails, the four lines below
#                   would prove nothing and are not printed.
#                 - the Claims API to the Model Gateway, which no rule of the
#                   Claims API's policy names: it must time out.
#                 - the Claims API to the API server's Service address
#                   (kubernetes.default.svc:443), the check's "egress to what
#                   the policy does not list": no service's policy has a rule for
#                   an address or for 443 or 6443, and without a policy the
#                   connection is reached (the database's policy lists 6443,
#                   which is how the instance manager reaches it). It is a
#                   target inside the cluster, so smoke sends nothing off the
#                   machine; an address in a documentation range (192.0.2.1)
#                   was not used because it times out with or without a policy.
#                 - the database's ingress: a probe Pod of the Claims API's own
#                   image and securityContext (nothing is pulled), labelled
#                   app.kubernetes.io/name=meridian-sweep and not
#                   app.kubernetes.io/part-of=meridian. The sweep's policy lets
#                   such a pod reach DNS and the database, so its egress is not
#                   what blocks it (a pod no policy names has none, and its
#                   "blocked" would be default-deny's egress, not the database's
#                   ingress); the database's ingress admits pods by part-of
#                   alone, so the connection to platform-db-rw.meridian.svc:5432
#                   must time out. Then the same pod is given the part-of label
#                   and the same probe must reach the database (a few tries: the
#                   network plugin takes a moment): it is the control, one pod
#                   with and without the label. No Service or Deployment selects
#                   the name label of the sweep, so the pod takes no traffic.
#                 - the collector (S063): a second probe Pod, of the same image
#                   and securityContext, in the namespace `default` (one that
#                   exists on every cluster, outside `meridian` and
#                   `observability`), with no name label of a workload and none
#                   that a policy selects, opens a connection to the collector's
#                   OTLP HTTP port, otel-collector.observability.svc.cluster.
#                   local:4318, the port the six services push to. The
#                   collector's ingress admits the pods of `meridian` alone
#                   (manifests/observability-networkpolicy.yaml, which `make up`
#                   applies), so it must time out. A policy that is missing or
#                   too wide, or a cluster that does not enforce it, makes the
#                   collector answer, and the line fails; a refusal, a name that
#                   does not resolve and a failed exec are failures too, never
#                   "blocked". It is skipped, one line, when the collector's
#                   Deployment is not there. The control above is this line's
#                   control for the probe: the same probe, the same image, the
#                   name resolution and the cluster's enforcement work, and it is
#                   not printed when the control failed. A timeout alone could
#                   also be a collector that is up but hangs, so the line has a
#                   second control, check 4's push: a pod the policies admit (a
#                   telemetrygen Job in `meridian`, over TLS) reached the
#                   collector in this run (check 4 runs first and leaves the
#                   word in ${telemetry_pushed}). When it did not, a timeout
#                   from `default` is a FAIL that proves nothing ("the collector
#                   was not reached from meridian either, so a timeout from
#                   default shows nothing"), not a PASS; an answer from the
#                   collector is the same FAIL as ever, whatever check 4 did.
#                   What the fifth line does not prove:
#                   that a pod of `meridian` can push (check 4 does, with the
#                   telemetrygen Jobs it runs there, and this line depends on
#                   it); that 4317, the collector's gRPC port, is closed to every
#                   pod (the collector's values leave the receiver off, and the
#                   policy admits no one; this line probes 4318 only, the port a
#                   Meridian pod is let in on); or
#                   that a pod of another namespace than `default` is refused
#                   (the rule is "the namespace meridian only", and `default`
#                   stands for all the others). It adds about 10 s: one timeout of 4 s and
#                   the second Pod's start, and at most 60 s more for its start
#                   when something is wrong.
#                 The Pod carries one label of smoke's own as well,
#                 meridian-smoke=network-probe, which no policy, Service or
#                 Deployment selects, so it changes nothing they see. A Pod
#                 that a lost trap left (a run killed with SIGKILL, or a power
#                 cut) stays as a Failed object once its five minutes have
#                 passed, with the labels the policies select on; so the check
#                 starts by listing the Pods with that label, even when it will
#                 skip, and deletes by name those older than 300 s (a younger
#                 one is another run's, which two runs at once leave to each
#                 other; a list that cannot be read is not an error).
#                 The Pod is deleted when the check ends and by the EXIT trap,
#                 which also runs on SIGHUP, SIGINT and SIGTERM (each has a trap
#                 that exits, as the Makefile's pytest-db recipe does: bash does
#                 not reliably run the EXIT trap on a hang-up without it). A
#                 delete that failed is not forgotten: the trap tries again
#                 and says on stderr, with the command to run by hand, when it
#                 fails too. The Pod ends on its own after five minutes, and is
#                 not created when the control failed. The check fails when the
#                 policy `default-deny` does not exist.
#                 Skipped, one line instead of five, while the Claims API is not
#                 deployed (`make deploy`). What it does not prove: that
#                 every other pair of pods is allowed or denied as the chart
#                 says (the chart's tests render and compare the rules); that a
#                 pod of another namespace cannot reach the database, or that an
#                 address outside the machine is unreachable (S019 proved the
#                 database pod's 443 and 80 by hand; it stays by hand, because
#                 smoke sends nothing off the machine); that the API server
#                 answers without a policy (it does for the database's pod,
#                 which S019 measured); and nothing about UDP. Adds about 20 s:
#                 three timeouts of 4 s, the Pod's start and the exec calls
#                 (when something is wrong, at most 60 s for the Pod to be
#                 Ready and 16 s for the label's tries).
#   9. service identity: five lines, from the Agent Runtime's pod to the Model
#                 Gateway (S055, S056; the Claims API's pod cannot reach the
#                 gateway, which check 8 proves, so the runtime's does). The
#                 image has no curl, so Python opens the connection and checks
#                 the gateway's certificate against the CA. `GET /healthz` with
#                 no client certificate answers 200 (the kubelet's probe
#                 presents none); a `POST /v1/chat` with none answers 401 (no
#                 identity); the same with the runtime's own certificate,
#                 naming a tenant the registry's services.yaml does not let it
#                 name (a real tenant, so the gateway's own tenant check would
#                 let it through), answers 403. The fourth line reads the
#                 reason of that 403 from the audit table, in the database's
#                 primary pod: a row of the gateway's refusal for the identity
#                 rule's reason (`caller-name-not-allowed`), naming the
#                 runtime as the calling service, recorded at or after this
#                 run's start, which is the database's clock read just before
#                 the 403's request. The gateway writes it in a worker thread,
#                 at most one per reason, tenant and minute, so the check asks
#                 for a row that exists, never for a count that went up, and
#                 tries for about ten seconds; without it a 403 from the
#                 gateway's own policy would pass for the wrong reason. A
#                 second run inside that minute causes no row of its own: when
#                 the newest row is from the minute before this run started and
#                 none came after it, the line is a SKIP, not a PASS ("the
#                 gateway wrote this minute's refusal row for an earlier run;
#                 run again in a minute"), and with no row at all it is a FAIL.
#                 What the row proves is a refusal by the identity rule, for
#                 that reason, caller and tenant, at or after the mark; not
#                 that it is this run's own 403: two runs that overlap can
#                 share one row (another run's 403, written after this run's
#                 mark while this run's own is throttled, passes this line),
#                 so the PASS says "recorded at or after this run's mark".
#                 The fifth presents a certificate of another
#                 CA: the probe makes a throwaway key and a self-signed
#                 certificate with the runtime's own URI (the right name, the
#                 wrong CA), and the gateway must end the connection before any
#                 answer, with the TLS alert for an unknown CA (`refused`,
#                 ssl.SSLError with reason TLSV1_ALERT_UNKNOWN_CA) or with no
#                 alert at all (`reset`: the connection ended before any
#                 request was sent), worded apart; a status is a FAIL, and so
#                 is any other TLS error, and so is a connection that ended
#                 after the request went out (`closed-after-request`: the
#                 gateway took the request and closed without answering, which
#                 is also what a gateway that accepted the certificate does).
#                 The tools check above is a
#                 further proof: its calls run over TLS with the runtime's
#                 certificate. The two refusals (the 401 and the 403) leave two
#                 refusal rows in the audit table on each run, one per reason
#                 (the gateway throttles its refusal rows to one per reason and
#                 minute). Skipped while the Meridian services are not deployed
#                 (`make deploy`). A traceback is a failure, not a refusal.
#                 What the fifth line does not prove: uvicorn, which the
#                 services run under, ends an unknown CA's connection without
#                 delivering the alert (against the test server with its flags:
#                 a reset under TLS 1.3, an EOF under 1.2), so `reset` is the
#                 answer expected of the gateway, and it is the answer the
#                 cluster gave on every run of 2026-10-06 (`refused`, the
#                 alert, was never seen there; the EOF under 1.2 is the test
#                 server's only). It is wider than a refusal for the unknown
#                 CA: a gateway
#                 that died in that second would end the connection the same
#                 way (the three requests before it were answered by the same
#                 gateway). Only `refused`, the alert, names the unknown CA.
#                 The price of `closed-after-request`: the probe reads for one
#                 second before it sends, so a gateway slower than that to end
#                 the connection turns a refusal into this FAIL; it fails
#                 closed, and a run again tells.
#  10. certificate policy: four lines (S056, S062), run after the first
#                 nine and never skipped: its objects exist after `make up`, so
#                 a missing one is a FAIL. The first three lines read only: the
#                 five CertificateRequestPolicies
#                 (meridian-services, meridian-services-ca,
#                 meridian-deny-unlisted, telemetry-ca, otel-collector; S063)
#                 are Ready; the Deployment
#                 cert-manager-approver-policy in cert-manager has an available
#                 replica; and cert-manager's own approver is off, read two ways
#                 that must agree: the ClusterRole
#                 cert-manager-controller-approve:cert-manager-io, which the
#                 chart renders only with its approver on, does not exist
#                 (kubectl's NotFound is the pass; any other error is a FAIL),
#                 and the controller's arguments hold
#                 --controllers=-certificaterequests-approver. A FAIL says the
#                 issuer may be signing for every request again. Renovate's gate
#                 for the kind platform is `make up` and `make smoke`, and
#                 nothing else makes a certificate request on a cluster that has
#                 its certificates, so a cert-manager or approver-policy update
#                 that turned the approver back on, or left the policies or the
#                 add-on gone, would otherwise pass. The fourth line makes the
#                 one request that the issuer must refuse (S062), the one
#                 change this check makes: a CertificateRequest named
#                 meridian-smoke-refused-<pid>-<random> in the namespace
#                 default, for the issuer meridian-services, with a URI under
#                 the Meridian prefix and a duration that policy allows, so
#                 that only its namespace refuses it: the namespace selector
#                 of meridian-services does not list default, and the policy
#                 meridian-deny-unlisted, which selects the issuer from every
#                 namespace, permits nothing. It passes when the request is
#                 Denied and the approver's whole message, judged before it
#                 is cut, names meridian-deny-unlisted as a policy that
#                 evaluated the request and does not name meridian-services
#                 as one (the line says the reason, cut to 60 characters, and
#                 the message, cut to 120). The form it matches is the one
#                 approver-policy v0.28.0 wrote on the cluster: "No policy
#                 approved this request: [meridian-deny-unlisted:
#                 [spec.allowed.uris: Invalid value: ...", a policy's name
#                 after "[", "]" or "," and before a colon, so that
#                 meridian-services-ca and the issuer's name do not count.
#                 The check depends on
#                 approver-policy's wording at the pinned version. A Denied
#                 request whose message names meridian-services as a policy
#                 is a FAIL (that policy selected a request from another
#                 namespace), and so is one whose message has neither form
#                 (the line says the approver's message is not in the form
#                 this check reads, and prints it cut). It fails when the
#                 request is Approved or carries a certificate (the issuer
#                 signed a request it must refuse; with both conditions true
#                 the request is Approved, whatever their order), and when
#                 neither condition is there
#                 after 30 s (the approver did not answer; a request nobody
#                 approves or denies would also be what a policy that can no
#                 longer be used by the requester leaves). The request is
#                 deleted as soon as it is read, by the EXIT trap when the run
#                 ends first (an error, a FAIL, an interrupt) and, for a run
#                 that was killed, at the start of the next one, which lists
#                 the requests with the label meridian-smoke=refused-request
#                 and deletes by name those older than 300 s (a request lives
#                 two seconds): the age is the creationTimestamp read with jq
#                 against this machine's clock, so a skewed clock only delays
#                 the sweep, and a younger request is another run's, which two
#                 runs at once leave to each other; a list that cannot be read
#                 is not an error. A failed delete, judged by kubectl's exit
#                 status and not by what it wrote, is a FAIL that names the
#                 request. A CertificateRequest makes no
#                 Secret: the certificate an issuer signed would be in the
#                 request's own status, which is deleted with it and never
#                 printed. openssl makes the key and writes it to /dev/null:
#                 it is in no file, no variable and no output. What it does
#                 not prove: the request is made by whoever runs smoke
#                 (kind's cluster-admin, which may use every policy), not by
#                 cert-manager's account, so the check shows what the
#                 namespace selector and the approver do with a request of
#                 another namespace, not the role bindings that let
#                 cert-manager use a policy (the plan's S056 section made
#                 those by hand); and a request for an issuer that is not
#                 Meridian's, which no policy answers, is not made. It adds
#                 a second or two when the approver is up, 30 s when it does
#                 not answer.
#  11. alert rules and health dashboard: four lines, read-only (S062), run
#                 last. Three lines read Prometheus' /api/v1/rules through
#                 Grafana's datasource proxy (the port-forward of check 4) for
#                 the PrometheusRule `meridian` that `make up` applies. The
#                 four groups of infra/kind/alerts/meridian.yaml are loaded and
#                 every rule of the loaded meridian.* groups has health ok (a
#                 FAIL names the rule, its health and Prometheus' lastError,
#                 cut to 120 printable ASCII characters). The loaded group and
#                 rule names equal the file's (the file's names are read with
#                 awk by their indentation, and a test keeps that equal to a
#                 YAML parser's reading; a cluster that runs an older file
#                 says which groups and rules differ). No alert of those
#                 groups is firing (a FAIL names it; a pending alert is not a
#                 failure, and the line names it). The three wait up to 120 s
#                 for the groups to load and be evaluated (a rule not yet
#                 evaluated has health unknown, which is not ok); one FAIL
#                 line replaces them when Prometheus does not answer with
#                 status success, or when the PrometheusRule is not there
#                 (`make up` applies it: the line says to run it), and so does
#                 any other failure to look for it. An answer of status success
#                 without a usable data.groups list fails each of the three
#                 lines (the dashboard line still runs), and with no meridian.*
#                 group loaded the third line fails too: there is nothing to be
#                 firing, so it cannot tell. The fourth line
#                 is check 5's dashboard check for the health dashboard (uid
#                 meridian-platform-health): the provisioned copy has the
#                 queries of dashboards/platform-health.json, and every query
#                 of it runs in Prometheus with a range of an hour. No query
#                 is left out: ${__range_s} becomes 3600, and a query that
#                 finds no series on a quiet cluster still answers with status
#                 success. What it does not prove: that the series a rule or a
#                 panel names exist (an absent series leaves a rule healthy
#                 and quiet, and a panel with no data is a success; that stays
#                 by hand, in docs/operations/README.md), that a threshold is
#                 right, or that anyone would be told (kind has no
#                 Alertmanager). It adds one request for the rules, one for
#                 the dashboard and its nine queries: a few seconds.
# Prints one PASS, FAIL or SKIP line per check and exits non-zero on any FAIL.
set -euo pipefail

# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

readonly EDGE_URL=http://127.0.0.1:8088/
readonly ADJUSTER_QUEUE_URL=http://claims.meridian.localhost:8088/adjuster/claims
readonly ADJUSTER_DECISION_URL=http://claims.meridian.localhost:8088/adjuster/claims/CLM-9999/decision
# The first sentence of the banner every page carries (templates/base.html).
readonly ADJUSTER_BANNER="Synthetic data only."
readonly SWEEP_CRONJOB=meridian-sweep
# The CronJob's schedule is every five minutes; a run is overdue after three
# periods (900 s). The period is read from the CronJob's own schedule when it
# is "*/N * * * *"; SWEEP_PERIOD_SECONDS is what is used for any other form.
readonly SWEEP_PERIOD_SECONDS=300
readonly SWEEP_STALE_PERIODS=3
# The clock the sweep check trusts: the database's, as whole seconds.
readonly SWEEP_CLOCK_SQL='SELECT floor(extract(epoch FROM now()))::bigint'
# The stores check (2): the newest migration file of this checkout, the probe
# for the schemas, the policy count and the ledger's newest name. The chunk
# count is CHUNK_COUNT_SQL of common.sh. COLLATE "C": the ledger's names sort as
# bytes, as the script sorts the files.
readonly MIGRATIONS_DIR="${KIND_DIR}/../../src/meridian/platform/migrations"
readonly STORES_READY_SQL="SELECT concat_ws(',', (to_regclass('public.meridian_migrations') IS NOT NULL)::int, (to_regclass('policy.policies') IS NOT NULL)::int, (to_regclass('knowledge.chunks') IS NOT NULL)::int)"
readonly POLICY_COUNT_SQL='SELECT count(*) FROM policy.policies'
readonly LEDGER_NEWEST_SQL='SELECT name FROM public.meridian_migrations ORDER BY name COLLATE "C" DESC LIMIT 1'
# STORES_READY_SQL answers "L,P,K": 1 or 0 for the ledger table, the policy
# table and the knowledge table. QUERY_ERROR_LENGTH cuts the message of a
# failed read of the database (meridian_query) where a FAIL line carries it.
readonly QUERY_ERROR_LENGTH=160
# Every psql this script runs gets these server settings, through PGOPTIONS in
# the exec (`env`: kubectl exec runs no shell, and a `SET` in the same -c would
# print a line of its own): a statement that runs longer than five seconds, or
# waits longer than three for a lock (a migration that holds one while smoke
# runs), is cancelled, and psql's message ends in the FAIL line of that check
# instead of the line hanging (the pgvector lines of check 2 keep no message:
# they read "not installed").
readonly PSQL_OPTIONS='-c statement_timeout=5s -c lock_timeout=3s'
# The connections the network-policy check (8) tries, as host:port. The Agent
# Runtime is the control: the Claims API's policy and its own name each other. The
# Model Gateway is a Service which only the Agent Runtime, the knowledge tool
# server and the ingestion Job may reach. The API server's Service is one no
# service's policy lists. The database's read-write Service is the one every
# workload's policy lists and only the pods labelled part-of=meridian are admitted
# to (manifests/platform-db-networkpolicy.yaml).
readonly NETWORK_RUNTIME=agent-runtime.meridian.svc:8000
readonly NETWORK_GATEWAY=model-gateway.meridian.svc:8000
readonly NETWORK_API_SERVER=kubernetes.default.svc:443
readonly NETWORK_DATABASE=platform-db-rw.meridian.svc:5432
# The probe Pod: the sweep's name label (its policy reaches DNS and the database
# and no Service or Deployment selects it), a sleep that ends on its own, and how
# long to wait for it and for the network plugin to see the label added to it.
readonly NETWORK_POD_NAME_LABEL=meridian-sweep
# The label of smoke's own that the Pod carries as well (no policy, Service or
# Deployment selects by it, so it changes nothing they see): the next run lists
# the Pods with it and deletes by name those older than NETWORK_LEFTOVER_AGE
# seconds, as it does for check 10's request (REFUSED_LEFTOVER_AGE).
readonly NETWORK_POD_SMOKE_LABEL=meridian-smoke=network-probe
# The collector line (the fifth of check 8) probes from a Pod in this namespace:
# one that exists on every cluster and is neither `meridian` nor `observability`.
# The Pod's name is NETWORK_OUTSIDER_PREFIX and the time.
readonly NETWORK_OUTSIDER_NAMESPACE=default
readonly NETWORK_OUTSIDER_PREFIX=smoke-outsider-
readonly NETWORK_LEFTOVER_AGE=300
readonly NETWORK_POD_LIFETIME=300
readonly NETWORK_POD_READY_TIMEOUT=60s
readonly NETWORK_LABEL_ATTEMPTS=4
readonly NETWORK_LABEL_INTERVAL=1
# The image has no curl, so Python opens the connection; the host and the port are
# its two arguments. The snippet prints "reached" or "blocked" and exits 0 either
# way; anything else (a name that does not resolve, a refused connection) is a
# traceback and a non-zero exit, which the check reports as a failure, not as
# "blocked": only a timeout is what a policy that drops packets looks like.
readonly NETWORK_PROBE_TIMEOUT=4
readonly NETWORK_PROBE='import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout='"${NETWORK_PROBE_TIMEOUT}"').close()
    print("reached")
except TimeoutError:
    print("blocked")'
# The service identity check (9): the Model Gateway's Service from the Agent
# Runtime's pod, with the pod's own certificate files (MERIDIAN_TLS_*). The probe
# prints the HTTP status of one request and nothing else (but for the last mode);
# its argument says which:
#   health          GET /healthz, no client certificate
#   anonymous       POST /v1/chat, no client certificate
#   foreign-tenant  POST /v1/chat with the runtime's certificate, naming a tenant
#                   that is not the runtime's (services.yaml) but is one the
#                   gateway would serve (tenants.yaml: `evaluation` may run the
#                   claims-triage agent), so only the identity rule refuses it;
#                   an unknown tenant would be refused without S055
#   foreign-ca      the same request with a throwaway key and a self-signed
#                   certificate that carries the runtime's own URI (the pod's
#                   MERIDIAN_IDENTITY_PREFIX and the service's ID): the right
#                   name, the wrong CA. The key and certificate are made here,
#                   written to a directory under /tmp (the pod's one writable
#                   path) that is removed when the probe ends, and never printed
#                   or passed as an argument. It reads before it writes (under
#                   TLS 1.3 the alert follows the handshake, and a request sent
#                   first can lose it to a reset; under 1.2 it comes inside the
#                   handshake, so the connection is opened inside the same
#                   try), and prints one of four answers, in this mode only:
#                   "refused", when the server sent the TLS alert for an
#                   unknown CA (ssl.SSLError, reason TLSV1_ALERT_UNKNOWN_CA);
#                   "reset", when the connection ended with no TLS alert
#                   before any request was sent (a reset, a broken pipe, an
#                   abort, or an EOF in violation of protocol:
#                   ssl.SSLEOFError); "closed-after-request", when it ended
#                   the same way after the request went out, which is what a
#                   server that accepted the certificate and then closed
#                   without answering does (http.client.RemoteDisconnected is
#                   a ConnectionResetError), so it is a FAIL, not a refusal;
#                   and the status when an answer came, which is a FAIL. The
#                   price: the one-second read is all the server has to end
#                   the connection, so a gateway slower than that turns a
#                   refusal into `closed-after-request`; it fails closed.
#                   Measured against the test server
#                   that has the services' uvicorn flags, the alert never
#                   arrives: a reset under TLS 1.3, an EOF under 1.2, so `reset`
#                   is the answer expected of the gateway (the cluster gave it
#                   on every run of 2026-10-06; `refused` was never seen
#                   there), and the check passes it, worded apart from
#                   `refused`. Anything else (another alert,
#                   the server's certificate not verifying, a name that does
#                   not resolve, a refused connection, a timeout) stays a
#                   traceback, in every mode.
# The audit line's constants (the row of the 403 above): the gateway's service
# name, the reason the identity rule writes for a name the caller may not use
# (a test keeps it equal to NAME_REFUSAL_REASON), the calling service, which is
# the deployment the probe runs in, the database's clock (read before the 403's
# request: a row recorded at or after it counts for this run), how far before it to
# look for the row of an earlier run (the gateway's REFUSAL_AUDIT_SECONDS: a
# test keeps it equal) and how long to wait for this run's own.
readonly IDENTITY_HOST=model-gateway.meridian.svc
readonly IDENTITY_PORT=8000
readonly IDENTITY_FOREIGN_TENANT=evaluation
readonly IDENTITY_CALLER=agent-runtime
readonly IDENTITY_GATEWAY_SERVICE=model-gateway
readonly IDENTITY_AUDIT_REASON=caller-name-not-allowed
readonly IDENTITY_CLOCK_SQL='SELECT extract(epoch FROM now())'
readonly IDENTITY_AUDIT_THROTTLE=60
readonly IDENTITY_AUDIT_ATTEMPTS=6
readonly IDENTITY_AUDIT_INTERVAL=2
readonly IDENTITY_PROBE='import datetime, http.client, json, os, ssl, sys, tempfile, uuid
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
mode, host, port, tenant = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
context = ssl.create_default_context(cafile=os.environ["MERIDIAN_TLS_CA_FILE"])
def answer():
    connection = http.client.HTTPSConnection(host, port, context=context, timeout=10)
    sent = False
    try:
        connection.connect()
        if mode == "foreign-ca":
            connection.sock.settimeout(1)
            try:
                if not connection.sock.recv(1):
                    return "reset"
            except TimeoutError:
                connection.sock.settimeout(10)
        sent = True
        if mode == "health":
            connection.request("GET", "/healthz")
        else:
            headers = {"Content-Type": "application/json"}
            if mode != "anonymous":
                headers.update({"X-Meridian-Tenant": tenant, "X-Meridian-Agent": "claims-triage", "X-Meridian-Run": str(uuid.uuid4())})
            body = json.dumps({"messages": [{"role": "user", "content": "identity check"}]})
            connection.request("POST", "/v1/chat", body=body, headers=headers)
        return str(connection.getresponse().status)
    except ssl.SSLCertVerificationError:
        raise
    except ssl.SSLError as error:
        if mode == "foreign-ca" and "TLSV1_ALERT_UNKNOWN_CA" in (error.reason or ""):
            return "refused"
        if mode == "foreign-ca" and isinstance(error, ssl.SSLEOFError):
            return "closed-after-request" if sent else "reset"
        raise
    except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
        if mode != "foreign-ca":
            raise
        return "closed-after-request" if sent else "reset"
if mode == "foreign-ca":
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "foreign-ca probe")])
    now = datetime.datetime.now(datetime.timezone.utc)
    uri = os.environ["MERIDIAN_IDENTITY_PREFIX"] + "agent-runtime"
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(minutes=10))
        .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(uri)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    with tempfile.TemporaryDirectory(prefix="foreign-ca-", dir="/tmp") as directory:
        with open(directory + "/tls.crt", "wb") as file:
            file.write(certificate.public_bytes(serialization.Encoding.PEM))
        with open(directory + "/tls.key", "wb") as file:
            file.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        context.load_cert_chain(directory + "/tls.crt", directory + "/tls.key")
        print(answer())
else:
    if mode == "foreign-tenant":
        context.load_cert_chain(os.environ["MERIDIAN_TLS_CERT_FILE"], os.environ["MERIDIAN_TLS_KEY_FILE"])
    print(answer())'

# The certificate policy check (10): what approves the services' certificates
# (S056). The policies are the ones manifests/certificate-policy.yaml applies
# (a test keeps this list equal to deploy.sh's); the add-on and the controller
# are Deployments in cert-manager. With cert-manager's approver off
# (disableAutoApproval in values/cert-manager.yaml) the chart renders neither
# the ClusterRole below nor the controller without the argument that follows.
readonly POLICY_NAMES=(meridian-services meridian-services-ca meridian-deny-unlisted telemetry-ca otel-collector)
readonly POLICY_NAMESPACE=cert-manager
readonly POLICY_ADDON=cert-manager-approver-policy
readonly POLICY_CONTROLLER=cert-manager
readonly POLICY_BUILTIN_ROLE=cert-manager-controller-approve:cert-manager-io
readonly POLICY_BUILTIN_OFF_ARG=--controllers=-certificaterequests-approver
# The request the issuer must refuse (check 10's fourth line): a
# CertificateRequest in REFUSED_NAMESPACE for the issuer REFUSED_ISSUER, with a
# URI that meridian-services allows in `meridian` and a duration it allows, so
# nothing about the request's shape refuses it, only its namespace (a test keeps
# these equal to certificate-policy.yaml's). The name is
# REFUSED_NAME_PREFIX and a suffix, the label is what the next run finds a
# leftover by (only one older than REFUSED_LEFTOVER_AGE seconds is deleted), and
# the answer is read for up to REFUSED_ATTEMPTS tries, REFUSED_INTERVAL seconds
# apart (the approver answers in about a second). The reason is cut to
# REFUSED_REASON_LENGTH characters and the message to REFUSED_MESSAGE_LENGTH for
# the line; a Denied request passes when the whole message names
# REFUSED_DENYING_POLICY and not REFUSED_SELECTING_POLICY as a policy (a test
# keeps both equal to certificate-policy.yaml's names).
readonly REFUSED_NAMESPACE=default
readonly REFUSED_ISSUER=meridian-services
readonly REFUSED_LABEL=meridian-smoke=refused-request
readonly REFUSED_NAME_PREFIX=meridian-smoke-refused-
readonly REFUSED_URI=spiffe://meridian.kind/ns/meridian/sa/meridian-smoke-refused
readonly REFUSED_DURATION=1h0m0s
readonly REFUSED_ATTEMPTS=15
readonly REFUSED_INTERVAL=2
readonly REFUSED_MESSAGE_LENGTH=120
readonly REFUSED_REASON_LENGTH=60
readonly REFUSED_LEFTOVER_AGE=300
readonly REFUSED_SELECTING_POLICY=meridian-services
readonly REFUSED_DENYING_POLICY=meridian-deny-unlisted

readonly CLAIMANT_START_URL=http://claims.meridian.localhost:8088/claimant/claims
# The second sentence of the claimant banner (templates/claimant_base.html).
readonly CLAIMANT_BANNER="Every name, address and description you enter must be fictional: never a real person's."
# The collector's OTLP HTTP port: the one the six services push to
# (telemetry.otlpEndpoint in values/meridian.yaml; a test keeps them equal), the
# one the collector's ingress admits the pods of `meridian` on, and, since S063,
# the one telemetrygen pushes to (--otlp-http). Never 4317, OTLP over gRPC, which
# the collector's ingress admits from no one.
readonly COLLECTOR_ENDPOINT=otel-collector.observability.svc.cluster.local:4318
# The namespace of telemetrygen's Jobs (see check 4 and manifests/smoke-
# networkpolicy.yaml): the collector's ingress admits the pods of this one.
readonly TELEMETRYGEN_NAMESPACE=meridian
# The collector serves TLS with a certificate from an authority of its own
# (manifests/telemetry-ca.yaml; S063). `make up` copies the authority's public
# certificate, `tls.crt` of the Secret TELEMETRY_CA_SECRET in `observability`,
# into the ConfigMap TELEMETRY_CA_CONFIGMAP (key ca.crt) in `meridian` (a test
# keeps both names equal to up.sh's). telemetrygen's Job pod mounts that key at
# TELEMETRYGEN_CA_FILE's directory and pushes with --ca-cert.
readonly TELEMETRY_CA_SECRET=telemetry-ca
readonly TELEMETRY_CA_CONFIGMAP=telemetry-ca
readonly TELEMETRY_CA_NAMESPACE=meridian
# How many hex digits of a certificate's fingerprint a line may print.
readonly TELEMETRY_FINGERPRINT_SHOWN=12
readonly TELEMETRYGEN_CA_DIRECTORY=/etc/telemetry-ca
readonly TELEMETRYGEN_CA_FILE="${TELEMETRYGEN_CA_DIRECTORY}/ca.crt"
# The clear-text probe of check 4: a Job named CLEAR_TEXT_JOB_PREFIX and the
# epoch, in TELEMETRYGEN_NAMESPACE (the pods of `meridian` are the ones the
# collector's ingress admits, and the label of smoke's Jobs gets the egress, so
# the policies let the probe through and a refusal is the TLS listener's). The
# pod runs the platform database's own image, which `make up` has put on the
# node, so no image is pulled and the Meridian services need not be deployed.
# CLEAR_TEXT_PROBE runs under bash in it with the collector's host, port and
# CLEAR_TEXT_TIMEOUT seconds as arguments. It first connects with `timeout`
# (a connection that hangs is "timeout"; any other failure is "error: ..." with
# bash's own message), then sends a POST with an empty body in clear text and
# prints the status line the server answers ("answered HTTP/1.0 400 Bad
# Request"), "closed" when the server ends the connection without a word, or
# "no answer" when it says nothing for CLEAR_TEXT_TIMEOUT seconds.
readonly CLEAR_TEXT_JOB_PREFIX=smoke-cleartext-
readonly CLEAR_TEXT_TIMEOUT=4
# shellcheck disable=SC2016 # the script's own variables, expanded by the pod's bash
readonly CLEAR_TEXT_PROBE='host=$1 port=$2 limit=$3
err="$(timeout "${limit}" bash -c ": </dev/tcp/\$0/\$1" "${host}" "${port}" 2>&1)"
case $? in
  0) ;;
  124) echo timeout; exit 0 ;;
  *) echo "error: ${err}"; exit 0 ;;
esac
exec 3<>"/dev/tcp/${host}/${port}"
printf "POST /v1/traces HTTP/1.1\r\nHost: %s:%s\r\nContent-Type: application/x-protobuf\r\nContent-Length: 0\r\nConnection: close\r\n\r\n" "${host}" "${port}" >&3
IFS= read -r -t "${limit}" line <&3
status=$?
if [ -n "${line}" ]; then
  echo "answered $(printf "%s" "${line}" | tr -d "\r")"
elif [ "${status}" -gt 128 ]; then
  echo "no answer"
else
  echo closed
fi'
# What the telemetry check (4) prints of an answer of Tempo, Loki or Prometheus
# is cut to this many characters (see telemetry_answer).
readonly TELEMETRY_ANSWER_LENGTH=120
readonly GRAFANA_SERVICE=svc/kube-prometheus-stack-grafana
readonly POLL_TIMEOUT=120
readonly POLL_INTERVAL=3
readonly JOB_TIMEOUT=120s
# The dashboard's uid (infra/kind/dashboards/gateway-cost.json) and the series
# the gateway exports for it (their names are in src/meridian/platform/gateway).
readonly DASHBOARD_UID=meridian-gateway-cost
readonly DASHBOARD_FILE="${KIND_DIR}/dashboards/gateway-cost.json"
readonly COST_SERIES=(meridian_gateway_tokens_total meridian_gateway_cost_EUR_total meridian_gateway_calls_total)
# The health dashboard (infra/kind/dashboards/platform-health.json), checked as
# the cost dashboard is, by check_dashboard (check 11).
readonly HEALTH_DASHBOARD_UID=meridian-platform-health
readonly HEALTH_DASHBOARD_FILE="${KIND_DIR}/dashboards/platform-health.json"
# The alert rules check (11): the PrometheusRule `meridian` that up.sh applies
# from ALERT_RULES_FILE; Prometheus names a rule's group by the file's group
# name, and every group of the file starts with ALERT_GROUP_PREFIX (a test keeps
# it so). A rule's lastError is cut to ALERT_ERROR_LENGTH characters.
readonly ALERT_RULES_NAMESPACE=observability
readonly ALERT_RULES_OBJECT=meridian
readonly ALERT_RULES_FILE="${KIND_DIR}/alerts/meridian.yaml"
readonly ALERT_GROUP_PREFIX="meridian."
readonly ALERT_ERROR_LENGTH=120
readonly ALERT_RULES_PATH=/api/datasources/proxy/uid/prometheus/api/v1/rules
# The service account the chart makes for Grafana (release name + "-grafana").
readonly GRAFANA_ACCOUNT=system:serviceaccount:observability:kube-prometheus-stack-grafana
# ... and for kube-state-metrics (release name + "-kube-state-metrics").
readonly KSM_ACCOUNT=system:serviceaccount:observability:kube-prometheus-stack-kube-state-metrics

failures=0
skips=0
grafana_url=""     # set by open_grafana
grafana_failed=0   # open_grafana failed once: later calls fail quietly
identity_answer="" # set by identity_status
identity_primary="" # set by identity_mark_start: the primary pod of platform-db
identity_mark=""    # set by identity_mark_start: the database's clock before the 403
identity_mark_problem="" # set by identity_mark_start: why there is no mark
rules_body=""      # set by fetch_rules
network_answer=""  # set by network_probe
network_pod=""     # the probe Pod of check 8 while it may exist
network_outsider="" # the probe Pod of check 8's collector line, in NETWORK_OUTSIDER_NAMESPACE
telemetry_pushed="" # set to yes by check 4 when telemetrygen's push from meridian passed: check 8's control
refused_request="" # the CertificateRequest of check 10 while it may exist
refused_err_file="" # the messages of check 10's commands, while it runs
refused_state=""   # set by refused_read: "<verdict>|<issued>|<reason>|<message>"
refused_problem="" # set by refused_read: why it could not read the request
refused_verdict="" # set by refused_wait: Approved, Denied or Issued, or empty
refused_reason=""  # set by refused_wait: the verdict's reason, cleaned
refused_message="" # set by refused_wait: the verdict's message, cleaned and cut
refused_message_full="" # set by refused_wait: the same message, cleaned, not cut
poll_error=""      # what the last failed poll attempt saw
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; failures=$((failures + 1)); }
skip() { printf 'SKIP  %s\n' "$*"; skips=$((skips + 1)); }

# clean_lines TEXT: TEXT on one line, its lines joined with ";", without any byte
# that is not printable ASCII (see `clean` in demo.sh). Whatever this script
# prints that came out of a pod goes through it, so a hostile answer cannot
# inject terminal escape sequences or extra lines.
clean_lines() { printf '%s' "$1" | LC_ALL=C tr -cd '[:print:]\n' | paste -sd ';' -; }

need_tools docker kubectl curl jq base64 openssl
require_local_docker
need_cluster

# ── 1. edge ──────────────────────────────────────────────────────────────────
# Envoy sends no identifying header on a 404 with no route, so the request
# counter of the Gateway's listener is the proof that Envoy answered. Listener
# "http-10080" is the Gateway's port 80 (Envoy Gateway adds 10000 to a
# privileged port). The counter is read through the API server's pod proxy.
edge_request_count() {
  local proxy
  proxy="$(kctl -n envoy-gateway-system get pod \
    -l app.kubernetes.io/component=proxy,gateway.envoyproxy.io/owning-gateway-name=edge \
    -o jsonpath='{.items[0].metadata.name}')"
  kctl get --raw "/api/v1/namespaces/envoy-gateway-system/pods/${proxy}:19001/proxy/stats/prometheus" |
    awk '/^envoy_http_downstream_rq_total\{envoy_http_conn_manager_prefix="http-10080"\}/ { print $2 }'
}

check_edge() {
  local before after status
  before="$(edge_request_count 2>/dev/null || true)"
  if ! status="$(curl -q --noproxy '*' -sS -m 10 -o /dev/null -w '%{http_code}' "${EDGE_URL}" 2>&1)"; then
    fail "edge: ${EDGE_URL} did not answer: ${status}"
    return
  fi
  after="$(edge_request_count 2>/dev/null || true)"
  if [[ "${status}" != 404 ]]; then
    fail "edge: expected 404 (no route for this host), got ${status}"
  elif [[ -z "${before}" || -z "${after}" ]]; then
    fail "edge: could not read Envoy's request counter"
  elif ((after > before)); then
    pass "edge: ${EDGE_URL} -> 404, counted by Envoy (${before} -> ${after} requests); no route for this host, as expected"
  else
    fail "edge: got 404 but Envoy did not count the request (${before} -> ${after})"
  fi
}

# ── 2. database ──────────────────────────────────────────────────────────────
# platform_db_primary: the name of the database's primary pod and nothing else
# on stdout; fails when there is none. Shared by the stores check below and the
# sweep check's clock (7).
platform_db_primary() {
  local primary
  primary="$(kctl -n meridian get pod \
    -l cnpg.io/cluster=platform-db,cnpg.io/instanceRole=primary \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)" || return 1
  [[ -n "${primary}" ]] || return 1
  printf '%s' "${primary}"
}

# meridian_query PRIMARY SQL: the answer of `psql -tA` in the `meridian` database
# of that pod. Every SQL passed is a constant of this script. When the read
# fails it returns 1 and prints the reason instead of an answer: what kubectl or
# psql wrote on stderr, cleaned and cut to QUERY_ERROR_LENGTH characters, which
# a caller puts in its FAIL line (`if ! x="$(meridian_query ...)"; then fail
# "...${x}"`). That message names relations, roles and connections, not rows:
# no query of this script casts a column, so a message cannot quote a value.
meridian_query() {
  local answer err_file reason
  err_file="$(mktemp)"
  if answer="$(kctl -n meridian exec "$1" -c postgres -- env "PGOPTIONS=${PSQL_OPTIONS}" psql -d meridian -tAc "$2" 2>"${err_file}")"; then
    rm -f "${err_file}"
    printf '%s' "${answer}"
    return 0
  fi
  reason="$(clean_lines "$(<"${err_file}")")"
  rm -f "${err_file}"
  reason="${reason:-no message}"
  printf '%s' "${reason:0:QUERY_ERROR_LENGTH}"
  return 1
}

# store_count PRIMARY SQL: the count the query gives; fails unless the answer is
# a whole number. On failure it prints the reason instead (see meridian_query).
store_count() {
  local answer
  answer="$(meridian_query "$1" "$2")" || {
    printf '%s' "${answer}"
    return 1
  }
  answer="$(clean_lines "${answer}")"
  [[ "${answer}" =~ ^[0-9]+$ ]] || {
    printf 'the answer was not a whole number'
    return 1
  }
  printf '%s' "${answer}"
}

# newest_migration: the name of the newest migration file of this checkout, by
# name as bytes; fails when there is none.
newest_migration() {
  local file names=()
  for file in "${MIGRATIONS_DIR}"/[0-9][0-9][0-9][0-9]_*.sql; do
    if [[ -e "${file}" ]]; then
      names+=("${file##*/}")
    fi
  done
  ((${#names[@]} > 0)) || return 1
  printf '%s\n' "${names[@]}" | LC_ALL=C sort | tail -n 1
}

# store_line PRIMARY PRESENT SQL STORE UNIT TABLE REASON: the line of one store,
# which holds records of UNIT in TABLE. PRESENT is 1 or 0, the probe's answer for
# the table: a table that is not there is a FAIL naming it (the ledger says
# migrations ran, so a missing table is a half-applied or renamed migration).
# REASON says what writes the records, for the line about an empty store.
store_line() {
  local primary=$1 present=$2 sql=$3 store=$4 unit=$5 table=$6 reason=$7 count
  if [[ "${present}" != 1 ]]; then
    fail "database: ${table} does not exist in the meridian database in ${primary}, although the migrations ledger does: a migration was half applied or the table was renamed (make deploy)"
  elif ! count="$(store_count "${primary}" "${sql}")"; then
    fail "database: the ${store}'s count could not be read (${table} in ${primary}): ${count}"
  elif ((10#${count} > 0)); then
    pass "database: the ${store} holds ${count} ${unit} (${table})"
  else
    fail "database: the ${store} holds no ${unit} (${table}): ${reason} (make deploy)"
  fi
}

# check_stores PRIMARY: the three lines of the stores, or one SKIP before the
# schemas exist, which is when the migrations ledger table is not there (nothing
# was ever migrated). A store's table that is missing beside a ledger is that
# store's FAIL, and an answer of the probe that is not "L,P,K" (STORES_READY_SQL)
# is one FAIL. Prints counts and a file name; no row's content.
check_stores() {
  local primary=$1 probe ledger tree has_ledger has_policies has_chunks
  if ! probe="$(meridian_query "${primary}" "${STORES_READY_SQL}")"; then
    fail "database: could not read the meridian database in ${primary} to look for its stores (${probe})"
    return
  fi
  probe="$(clean_lines "${probe}")"
  if ! [[ "${probe}" =~ ^([01]),([01]),([01])$ ]]; then
    fail "database: the probe for the stores' tables gave an answer that is not in its form (three of 1 or 0 with commas, as 1,1,1) in ${primary} (answer: ${probe:0:QUERY_ERROR_LENGTH})"
    return
  fi
  has_ledger="${BASH_REMATCH[1]}"
  has_policies="${BASH_REMATCH[2]}"
  has_chunks="${BASH_REMATCH[3]}"
  if [[ "${has_ledger}" != 1 ]]; then
    skip "database: the meridian database holds no migrated schemas yet (make deploy), so its stores are not read"
    return
  fi
  store_line "${primary}" "${has_policies}" "${POLICY_COUNT_SQL}" "policy store" policies policy.policies "the seed Job wrote none"
  store_line "${primary}" "${has_chunks}" "${CHUNK_COUNT_SQL}" "knowledge store" chunks knowledge.chunks "the ingestion Job stored none"
  if ! tree="$(newest_migration)"; then
    fail "database: no migration file under src/meridian/platform/migrations, so the ledger cannot be compared with this checkout"
  elif ! ledger="$(meridian_query "${primary}" "${LEDGER_NEWEST_SQL}")"; then
    fail "database: the migrations ledger could not be read (public.meridian_migrations in ${primary}): ${ledger}"
  elif [[ -z "$(clean_lines "${ledger}")" ]]; then
    fail "database: the migrations ledger is empty (public.meridian_migrations): the migration Job applied nothing"
  elif [[ "$(clean_lines "${ledger}")" == "${tree}" ]]; then
    pass "database: the migrations ledger's newest file is ${tree}, the newest of this checkout"
  else
    fail "database: the migrations ledger's newest file is $(clean_lines "${ledger}"), this checkout's is ${tree}: the cluster was deployed from another checkout (make deploy from this one)"
  fi
}

# The database's policy names the API server's address (S063): the CIDRs of its
# rule for port 6443 are the addresses of the `kubernetes` Service's endpoint.
# A read of two objects, nothing is started and nothing is changed. The reading
# and the comparison are common.sh's, the ones `make up` and `make deploy` use.
# One FAIL line says "run make up" when they differ, and says what could not be
# read when a read failed (it never says "changed" for that). It does not prove
# that the path is closed to every other address (see the header).
check_database_api_server() {
  local shown
  if api_server_matches_policy; then
    shown="$(paste -sd ',' - <<<"${api_server_addresses}")"
    pass "database: the NetworkPolicy platform-db lets its pod reach TCP 6443 at the API server's address alone (${shown}, the endpoint of the kubernetes Service)"
  else
    fail "database: $(clean_lines "${api_server_problem}")"
  fi
}

# One line for the policy's address (check_database_api_server), one per database:
# `app` (the platform's own) and `meridian` (the services'), then the stores of
# `meridian` (check_stores) with the primary found here.
check_database() {
  local primary database version
  check_database_api_server
  primary="$(kctl -n meridian get pod \
    -l cnpg.io/cluster=platform-db,cnpg.io/instanceRole=primary \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [[ -z "${primary}" ]]; then
    fail "database: no primary pod found for platform-db"
    return
  fi
  for database in app meridian; do
    version="$(kctl -n meridian exec "${primary}" -c postgres -- \
      env "PGOPTIONS=${PSQL_OPTIONS}" psql -d "${database}" -tAc "SELECT extversion FROM pg_extension WHERE extname='vector'" \
      2>/dev/null || true)"
    version="$(clean_lines "${version}")"
    if [[ -n "${version}" ]]; then
      pass "database: pgvector ${version} installed in ${primary}, database ${database}"
    else
      fail "database: extension vector is not installed in ${primary}, database ${database}"
    fi
  done
  check_stores "${primary}"
}

# ── 3. tools ─────────────────────────────────────────────────────────────────
# deployed_services: the Meridian Deployments, one name per line (empty when
# there are none). Stdout only: a warning on stderr is not a Deployment; it goes
# to the terminal. Shared with the cost check (5).
deployed_services() {
  kctl -n meridian get deployment \
    -l app.kubernetes.io/part-of=meridian -o name --ignore-not-found
}

# The probe runs in the runtime's own pod, so it uses the addresses the runtime
# was given. Its stdout is one "<server> <tool> <answer>" line per tool server;
# it exits 0 only when every answer is unknown-run (the refusal of a run that
# does not exist, which the servers check before anything else of the caller's).
check_tools() {
  local found out err_file servers
  # Skipped only when no Meridian Deployment exists. When any does, the probe is
  # required: a missing or renamed agent-runtime fails its exec below.
  if ! found="$(deployed_services)"; then
    fail "tools: could not look for the Meridian deployments (kubectl's error is above)"
    return
  fi
  if [[ -z "${found}" ]]; then
    skip "tools: the Meridian services are not deployed (make deploy)"
    return
  fi
  # The names come from stdout alone: a warning on stderr is not a server. A
  # failure shows both.
  err_file="$(mktemp)"
  if out="$(kctl -n meridian exec deploy/agent-runtime -- \
    python -m meridian.runtime.toolprobe 2>"${err_file}")"; then
    servers="$(clean_lines "$(awk '{ print $1 }' <<<"${out}")")"
    pass "tools: each server (${servers//;/, }) answered unknown-run through the runtime's client, over TLS with its certificate"
  else
    fail "tools: the probe in deployment/agent-runtime failed: stdout: $(clean_lines "${out}"); stderr: $(clean_lines "$(<"${err_file}")")"
  fi
  rm -f "${err_file}"
}

# ── 4. telemetry ─────────────────────────────────────────────────────────────
# start_job SIGNAL COUNT_FLAG: one Job that sends one item of SIGNAL (traces,
# logs or metrics) for service ${service}. Named uniquely, so reruns never clash.
# The Job runs in ${TELEMETRYGEN_NAMESPACE} (`meridian`) and pushes OTLP over HTTP
# (--otlp-http, which sends each signal to its default path, /v1/traces and so
# on) and TLS: there is no --otlp-insecure, and --ca-cert names the authority's
# certificate, which the pod reads from the ConfigMap ${TELEMETRY_CA_CONFIGMAP}
# of its own namespace (a Job that cannot find it never starts: its pod stays in
# ContainerCreating, and ttlSecondsAfterFinished counts only from a Job that has
# finished or failed). So the Job carries an activeDeadlineSeconds of 150, 30
# more than the 120 s JOB_TIMEOUT that smoke waits for it (a test keeps the two
# apart): a Job that never started fails at its deadline, and the TTL then
# removes it and its pod. The
# collector's ingress admits the pods of that namespace on 4318. The
# Job's pods carry no part-of label, so the database's ingress does not admit
# them, and manifests/smoke-networkpolicy.yaml selects them by the name label.
start_job() {
  local signal=$1 count_flag=$2
  kctl create -f - >/dev/null <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: smoke-${signal}-${epoch}
  namespace: ${TELEMETRYGEN_NAMESPACE}
  labels:
    app.kubernetes.io/name: meridian-smoke
spec:
  ttlSecondsAfterFinished: 900
  activeDeadlineSeconds: 150
  backoffLimit: 2
  template:
    metadata:
      labels:
        app.kubernetes.io/name: meridian-smoke
    spec:
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: telemetrygen
          image: ${TELEMETRYGEN_IMAGE}
          args:
            - ${signal}
            - --otlp-endpoint
            - ${COLLECTOR_ENDPOINT}
            - --otlp-http
            - --ca-cert
            - ${TELEMETRYGEN_CA_FILE}
            - --service
            - ${service}
            - ${count_flag}
            - "1"
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: [ALL]
          resources:
            requests:
              cpu: 10m
              memory: 32Mi
            limits:
              memory: 64Mi
          volumeMounts:
            - name: telemetry-ca
              mountPath: ${TELEMETRYGEN_CA_DIRECTORY}
              readOnly: true
      volumes:
        - name: telemetry-ca
          configMap:
            name: ${TELEMETRY_CA_CONFIGMAP}
            items:
              - key: ca.crt
                path: ca.crt
EOF
}

# gcurl ARGS...: curl against Grafana with basic auth. The password reaches curl
# on stdin as a config line, so it never appears in a process listing.
gcurl() {
  { set +x; } 2>/dev/null # a `bash -x` run must not trace the password
  printf 'user = "admin:%s"\n' "${password}" | curl -q --noproxy '*' -sS -m 15 -K - "$@"
}

# poll FILTER CURL_ARGS...: run gcurl until jq FILTER prints a non-empty value
# or POLL_TIMEOUT passes. The value is left in ${poll_result}. A failed attempt
# leaves its reason in ${poll_error}: curl's status and stderr, or the start of
# an answer the filter found nothing in; a success clears it.
poll() {
  local filter=$1
  shift
  local deadline=$((SECONDS + POLL_TIMEOUT)) body status err_file
  err_file="$(mktemp)"
  poll_error=""
  while ((SECONDS < deadline)); do
    if body="$(gcurl "$@" 2>"${err_file}")"; then
      if poll_result="$(jq -r "${filter}" <<<"${body}" 2>/dev/null)" &&
        [[ -n "${poll_result}" ]]; then
        poll_error=""
        rm -f "${err_file}"
        return 0
      fi
      poll_error="$(clean_lines "${body:0:160}")"
    else
      status=$?
      poll_error="curl exit ${status}: $(clean_lines "$(<"${err_file}")")"
    fi
    sleep "${POLL_INTERVAL}"
  done
  rm -f "${err_file}"
  poll_result=""
  return 1
}

# network_delete_pod: delete the probe Pod of check 8 when one was named, and
# forget it only when kubectl said it is gone (a pod that never existed is not
# an error), so a delete that failed is tried again by the EXIT trap, as
# refused_delete_request does. Returns 1 when the delete failed. Nothing is
# waited for: the Pod's process is a sleep.
network_delete_pod() {
  [[ -n "${network_pod}" ]] || return 0
  kctl -n meridian delete pod "${network_pod}" --ignore-not-found --wait=false >/dev/null 2>&1 || return 1
  network_pod=""
}

# refused_delete_request [ERR_FILE]: delete the CertificateRequest of check 10
# when one was named, and forget it only when kubectl said it is gone (a
# request that never existed is not an error), so a delete that failed is tried
# again by the EXIT trap. kubectl's stderr goes to ERR_FILE when one is given.
# Nothing is waited for: a request has no finalizer.
refused_delete_request() {
  [[ -n "${refused_request}" ]] || return 0
  kctl -n "${REFUSED_NAMESPACE}" delete certificaterequest "${refused_request}" \
    --ignore-not-found --wait=false >/dev/null 2>"${1:-/dev/null}" || return 1
  refused_request=""
}

cleanup() {
  # On stderr, not stdout: the lines a reader counts do not change.
  network_delete_pod || echo "smoke: could not delete the probe pod ${network_pod} in meridian; delete it by hand: kubectl -n meridian delete pod ${network_pod}" >&2
  network_outsider_delete || echo "smoke: could not delete the probe pod ${network_outsider} in ${NETWORK_OUTSIDER_NAMESPACE}; delete it by hand: kubectl -n ${NETWORK_OUTSIDER_NAMESPACE} delete pod ${network_outsider}" >&2
  refused_delete_request || true
  if [[ -n "${refused_err_file}" ]]; then rm -f "${refused_err_file}"; fi
  if [[ -n "${pf_pid:-}" ]]; then
    kill "${pf_pid}" 2>/dev/null || true
    wait "${pf_pid}" 2>/dev/null || true # kubectl is gone when smoke.sh returns
  fi
  if [[ -n "${pf_log:-}" ]]; then rm -f "${pf_log}"; fi
}

# open_grafana: read the admin password into ${password} (gcurl uses it) and
# forward a local port to Grafana, once per run; the address is left in
# ${grafana_url}. Returns 0 at once when the forward is open and alive. Returns
# 1 after one FAIL line when it cannot open or has died; after that every call
# returns 1 without a line. The password is never an argument and never printed.
open_grafana() {
  { set +x; } 2>/dev/null # a `bash -x` run must not trace the password
  ((grafana_failed == 0)) || return 1
  local port="" waited=0 detail
  if [[ -n "${grafana_url}" ]]; then
    kill -0 "${pf_pid}" 2>/dev/null && return 0
    grafana_failed=1
    fail "grafana: the port-forward to Grafana died (kubectl said: $(clean_lines "$(<"${pf_log}")"))"
    return 1
  fi
  if ! password="$(kctl -n observability get secret grafana-admin \
    -o jsonpath='{.data.admin-password}' 2>/dev/null | base64 -d)" || [[ -z "${password}" ]]; then
    grafana_failed=1
    fail "grafana: could not read the Grafana admin password from Secret grafana-admin"
    return 1
  fi
  pf_log="$(mktemp)"
  # kubectl itself is backgrounded (not the kctl function), so $! is its PID and
  # the EXIT trap can stop it.
  kubectl --kubeconfig "${KUBECONFIG_FILE}" --context "${KUBE_CONTEXT}" \
    -n observability port-forward --address 127.0.0.1 "${GRAFANA_SERVICE}" :80 \
    >"${pf_log}" 2>&1 &
  pf_pid=$!
  while :; do
    port="$(sed -n 's/^Forwarding from 127\.0\.0\.1:\([0-9]*\) ->.*/\1/p' "${pf_log}" | head -n 1)"
    [[ -z "${port}" ]] || break
    kill -0 "${pf_pid}" 2>/dev/null || break # kubectl has exited: no use waiting
    ((waited < 30)) || break
    sleep 1
    waited=$((waited + 1))
  done
  if [[ -z "${port}" ]]; then
    detail="$(clean_lines "$(<"${pf_log}")")"
    grafana_failed=1
    cleanup || true
    pf_pid=""
    pf_log=""
    fail "grafana: could not port-forward to Grafana (kubectl said: ${detail})"
    return 1
  fi
  grafana_url="http://127.0.0.1:${port}"
}

# telemetry_answer TEXT: what a backend returned, as it may be printed: through
# clean_lines, then cut to TELEMETRY_ANSWER_LENGTH characters. Anyone who can
# push a log line to the collector chooses its text, so an escape sequence or a
# line that starts with PASS must not reach the terminal as one.
telemetry_answer() {
  local text
  text="$(clean_lines "$1")"
  printf '%s' "${text:0:TELEMETRY_ANSWER_LENGTH}"
}

# pem_fingerprint PEM: the SHA-256 fingerprint of the first certificate of PEM,
# as upper-case hex without colons; returns 1 when PEM holds no certificate. The
# certificate is public; nothing of PEM but this is kept.
pem_fingerprint() {
  local line
  line="$(openssl x509 -noout -fingerprint -sha256 <<<"$1" 2>/dev/null)" || return 1
  line="${line#*=}"
  printf '%s' "${line//:/}"
}

# check_telemetry_ca: the first line of check 4 (S063). The ConfigMap
# ${TELEMETRY_CA_CONFIGMAP} in `meridian`, which the six services mount to trust
# the collector and telemetrygen's Jobs too, holds the certificate the authority
# has now: the fingerprints of its ca.crt and of tls.crt of the Secret
# ${TELEMETRY_CA_SECRET} in `observability` are equal. Only that one field of
# the Secret is read (a jsonpath, as up.sh does), never the object and never
# tls.key. Prints nothing but the first TELEMETRY_FINGERPRINT_SHOWN hex digits of
# either fingerprint. SKIP while the Secret is not there (nothing of the
# authority exists, `make up`); FAIL when the ConfigMap is missing or differs,
# with the remedy: `make up` publishes the current certificate, and nothing
# follows it. The services mount the ConfigMap as a directory, the kubelet
# refreshes the mounted file, and their exporters read it at each new connection
# (the security review of S063 swapped the file under a live exporter and its
# next new connection used the new one), so they use it within about a minute
# and need no restart.
check_telemetry_ca() {
  local found encoded authority published authority_print published_print
  if ! found="$(kctl -n observability get secret "${TELEMETRY_CA_SECRET}" -o name --ignore-not-found)"; then
    fail "telemetry: could not look for the Secret ${TELEMETRY_CA_SECRET} in observability (kubectl's error is above)"
    return
  fi
  if [[ -z "${found}" ]]; then
    skip "telemetry: the collector's authority is not there (no Secret ${TELEMETRY_CA_SECRET} in observability: run make up), so the ConfigMap ${TELEMETRY_CA_CONFIGMAP} was not compared with it"
    return
  fi
  if ! encoded="$(kctl -n observability get secret "${TELEMETRY_CA_SECRET}" -o 'jsonpath={.data.tls\.crt}')"; then
    fail "telemetry: could not read tls.crt of the Secret ${TELEMETRY_CA_SECRET} in observability (kubectl's error is above)"
    return
  fi
  if ! published="$(kctl -n "${TELEMETRY_CA_NAMESPACE}" get configmap "${TELEMETRY_CA_CONFIGMAP}" -o 'jsonpath={.data.ca\.crt}' --ignore-not-found)"; then
    fail "telemetry: could not read ca.crt of the ConfigMap ${TELEMETRY_CA_CONFIGMAP} in ${TELEMETRY_CA_NAMESPACE} (kubectl's error is above)"
    return
  fi
  if ! authority="$(base64 -d <<<"${encoded}" 2>/dev/null)" ||
    ! authority_print="$(pem_fingerprint "${authority}")"; then
    fail "telemetry: tls.crt of the Secret ${TELEMETRY_CA_SECRET} in observability is not a certificate: read the Certificate (kubectl -n observability get certificate ${TELEMETRY_CA_SECRET})"
    return
  fi
  if [[ -z "${published}" ]]; then
    fail "telemetry: the ConfigMap ${TELEMETRY_CA_CONFIGMAP} in ${TELEMETRY_CA_NAMESPACE} is missing or has no ca.crt, so the services and telemetrygen cannot verify the collector: run make up"
    return
  fi
  if ! published_print="$(pem_fingerprint "${published}")"; then
    fail "telemetry: ca.crt of the ConfigMap ${TELEMETRY_CA_CONFIGMAP} in ${TELEMETRY_CA_NAMESPACE} is not a certificate: run make up"
    return
  fi
  if [[ "${published_print}" == "${authority_print}" ]]; then
    pass "telemetry: the ConfigMap ${TELEMETRY_CA_CONFIGMAP} in ${TELEMETRY_CA_NAMESPACE} holds the certificate of the collector's authority (fingerprints equal, sha256 ${authority_print:0:TELEMETRY_FINGERPRINT_SHOWN}...)"
  else
    fail "telemetry: the ConfigMap ${TELEMETRY_CA_CONFIGMAP} in ${TELEMETRY_CA_NAMESPACE} holds another certificate (sha256 ${published_print:0:TELEMETRY_FINGERPRINT_SHOWN}...) than the collector's authority has now (sha256 ${authority_print:0:TELEMETRY_FINGERPRINT_SHOWN}...): run make up, which publishes the current one (the kubelet refreshes the mounted file and the services use it for their next connection, within about a minute and without a restart)"
  fi
}

# clear_text_job_spec NAME: the probe Job of check_telemetry_clear_text as JSON.
# The pod carries the label of smoke's Jobs (manifests/smoke-networkpolicy.yaml
# gives it DNS and the collector's port and nothing else), the database image's
# bash runs CLEAR_TEXT_PROBE, and the restrictions are telemetrygen's: no root,
# no privilege escalation, a read-only root filesystem, no capability, no
# service-account token. IfNotPresent, not Always: the image is on the node.
clear_text_job_spec() {
  jq -n --arg name "$1" --arg namespace "${TELEMETRYGEN_NAMESPACE}" \
    --arg image "${POSTGRES_IMAGE}" --arg probe "${CLEAR_TEXT_PROBE}" \
    --arg host "${COLLECTOR_ENDPOINT%:*}" --arg port "${COLLECTOR_ENDPOINT##*:}" \
    --arg limit "${CLEAR_TEXT_TIMEOUT}" '
    {"app.kubernetes.io/name": "meridian-smoke"} as $labels | {
      apiVersion: "batch/v1", kind: "Job",
      metadata: {name: $name, namespace: $namespace, labels: $labels},
      spec: {
        ttlSecondsAfterFinished: 900, backoffLimit: 0, activeDeadlineSeconds: 60,
        template: {
          metadata: {labels: $labels},
          spec: {
            restartPolicy: "Never", automountServiceAccountToken: false,
            securityContext: {runAsNonRoot: true, runAsUser: 10001,
              seccompProfile: {type: "RuntimeDefault"}},
            containers: [{
              name: "probe", image: $image, imagePullPolicy: "IfNotPresent",
              command: ["bash", "-c", $probe, "probe", $host, $port, $limit],
              securityContext: {allowPrivilegeEscalation: false,
                readOnlyRootFilesystem: true, capabilities: {drop: ["ALL"]}},
              resources: {requests: {cpu: "10m", memory: "32Mi"},
                limits: {memory: "64Mi"}}
            }]
          }
        }
      }
    }'
}

# check_telemetry_clear_text: the second line of check 4 (S063). A push in clear
# text to the collector's port is not accepted. The probe Job (see
# CLEAR_TEXT_PROBE) runs in a pod the policies admit, so what refuses it is the
# TLS listener and not a NetworkPolicy; it sends plain HTTP to ${COLLECTOR_ENDPOINT}
# and the line reports what came back, told apart from a connection that never
# opened. PASS: a 400 (a Go TLS listener answers 400, "Client sent an HTTP
# request to an HTTPS server": seen on kind on 2026-10-06 as `HTTP/1.0 400 Bad
# Request`) or a connection closed with no answer. FAIL: a 2xx (the receiver
# takes clear text); any other status (a plain HTTP receiver answers 404, 503
# and 301 as well, so such an answer does not show TLS); and every answer that
# says nothing about the listener (a timeout, a refused connection, no answer, a
# Job that did not finish), each naming that the line then proves nothing. SKIP
# while the collector is not deployed (`make up`); the Meridian services are not
# needed. What the answer is is cleaned and cut like every answer from a pod.
check_telemetry_clear_text() {
  local found job answer code detail
  if ! found="$(kctl -n observability get deployment otel-collector -o name --ignore-not-found)"; then
    fail "telemetry: could not look for deployment/otel-collector in observability (kubectl's error is above)"
    return
  fi
  if [[ -z "${found}" ]]; then
    skip "telemetry: the collector is not deployed (make up), so no clear-text push to it was tried"
    return
  fi
  job="${CLEAR_TEXT_JOB_PREFIX}${epoch}"
  if ! detail="$(clear_text_job_spec "${job}" |
    kctl -n "${TELEMETRYGEN_NAMESPACE}" create -f - 2>&1 >/dev/null)"; then
    fail "telemetry: could not start the clear-text probe ${job} in ${TELEMETRYGEN_NAMESPACE} ($(clean_lines "${detail}")), so the line proves nothing"
    return
  fi
  if ! kctl -n "${TELEMETRYGEN_NAMESPACE}" wait --for=condition=complete \
    "job/${job}" --timeout="${JOB_TIMEOUT}" >/dev/null 2>&1; then
    fail "telemetry: the clear-text probe ${job} did not complete (kubectl -n ${TELEMETRYGEN_NAMESPACE} logs job/${job}), so the line proves nothing"
    return
  fi
  if ! answer="$(kctl -n "${TELEMETRYGEN_NAMESPACE}" logs "job/${job}" 2>/dev/null)"; then
    fail "telemetry: could not read the logs of the clear-text probe ${job} (kubectl -n ${TELEMETRYGEN_NAMESPACE} logs job/${job}), so the line proves nothing"
    return
  fi
  answer="$(telemetry_answer "${answer}")"
  read -r _ _ code _ <<<"${answer}"
  if [[ "${answer}" == "answered HTTP/"* && "${code}" =~ ^2[0-9][0-9]$ ]]; then
    fail "telemetry: the collector accepted a push in clear text on ${COLLECTOR_ENDPOINT} (${answer}): its receiver does not serve TLS (make up applies values/otel-collector.yaml)"
  elif [[ "${answer}" == "answered HTTP/"* && "${code}" == 400 ]] || [[ "${answer}" == closed ]]; then
    pass "telemetry: a push in clear text to the collector (${COLLECTOR_ENDPOINT}) is not accepted: a pod the policies admit got \"${answer}\""
  elif [[ "${answer}" == "answered HTTP/"* && "${code}" =~ ^[1-5][0-9][0-9]$ ]]; then
    fail "telemetry: the clear-text push to the collector (${COLLECTOR_ENDPOINT}) was answered with a status other than 400 (${answer}), which does not show TLS: a status other than 400 means something answered HTTP in clear text (make up applies values/otel-collector.yaml)"
  else
    fail "telemetry: the clear-text probe to ${COLLECTOR_ENDPOINT} gave no answer that shows what the listener does (${answer:-nothing}), so the line proves nothing"
  fi
}

check_telemetry() {
  epoch="$(date +%s)"
  service="meridian-smoke-${epoch}"
  check_telemetry_ca
  check_telemetry_clear_text
  log "telemetry: sending one trace, log and metric as service ${service}"
  start_job traces --traces
  start_job logs --logs
  start_job metrics --metrics

  local signal
  for signal in traces logs metrics; do
    if ! kctl -n "${TELEMETRYGEN_NAMESPACE}" wait --for=condition=complete \
      "job/smoke-${signal}-${epoch}" --timeout="${JOB_TIMEOUT}" >/dev/null 2>&1; then
      fail "telemetry: telemetrygen ${signal} job did not complete (kubectl -n ${TELEMETRYGEN_NAMESPACE} logs job/smoke-${signal}-${epoch}; a Job with no egress to the collector, or one that cannot find the ConfigMap ${TELEMETRY_CA_CONFIGMAP}, is the likeliest cause on a cluster that make up has not brought up to date: it applies manifests/smoke-networkpolicy.yaml and manifests/observability-networkpolicy.yaml and makes the ConfigMap)"
      return
    fi
  done
  pass "telemetry: telemetrygen sent trace, log and metric to ${COLLECTOR_ENDPOINT}"
  # The control of check 8's collector line: a pod the policies admit reached the
  # collector over TLS in this run. Set before the read-backs, which prove
  # something else (that the stores answered) and can fail on their own.
  telemetry_pushed=yes

  open_grafana || return 0 # it printed the FAIL line
  local proxy="${grafana_url}/api/datasources/proxy/uid"

  # Traces: THE done-when of S006.
  if poll '.traces[0].traceID // empty' \
    "${proxy}/tempo/api/search?tags=service.name%3D${service}&limit=5"; then
    pass "trace: Tempo has trace $(telemetry_answer "${poll_result}") for ${service}"
  else
    fail "trace: no trace for ${service} in Tempo after ${POLL_TIMEOUT}s"
  fi

  if poll '.data.result[0].values[0][1] // empty' -G \
    "${proxy}/loki/loki/api/v1/query_range" \
    --data-urlencode "query={service_name=\"${service}\"}" --data-urlencode "limit=5"; then
    pass "log: Loki has a line for ${service}: $(telemetry_answer "${poll_result}")"
  else
    fail "log: no line for ${service} in Loki after ${POLL_TIMEOUT}s"
  fi

  if poll '.data.result | select(length > 0) | length | tostring' -G \
    "${proxy}/prometheus/api/v1/query" \
    --data-urlencode "query={__name__=~\"gen.*\",job=\"${service}\"}"; then
    pass "metric: Prometheus has $(telemetry_answer "${poll_result}") series from telemetrygen for ${service}"
  else
    fail "metric: no series for ${service} in Prometheus after ${POLL_TIMEOUT}s"
  fi
}

# ── 5. cost panel ────────────────────────────────────────────────────────────
# run_dashboard_query TITLE EXPR: one instant query through Grafana's datasource
# proxy; Prometheus must answer status success. Counts it in ${queries_run};
# on a refusal returns 1 with "<title>: <error>" in ${query_error}.
run_dashboard_query() {
  local title=$1 expr=$2 body status error
  body="$(gcurl -G "${grafana_url}/api/datasources/proxy/uid/prometheus/api/v1/query" \
    --data-urlencode "query=${expr}" 2>&1)" || true
  queries_run=$((queries_run + 1))
  status="$(jq -r '.status // empty' <<<"${body}" 2>/dev/null || true)"
  [[ "${status}" != success ]] || return 0
  error="$(jq -r '.error // empty' <<<"${body}" 2>/dev/null || true)"
  [[ -n "${error}" ]] || error="${body}"
  query_error="${title}: $(clean_lines "${error:0:200}")"
  return 1
}

# dashboard_targets: the targets of the dashboard JSON on stdin, those of panels
# nested in rows included, as a JSON list of {title, expr} in document order
# (the title is the panel's).
dashboard_targets() {
  jq -c '[.panels[]? | .. | objects | select(has("targets"))
    | .title as $title | .targets[]? | {title: ($title // "(untitled)"), expr: .expr}]'
}

# dashboard_target_problem SERVED: why the served dashboard has nothing to run,
# on stdout, with status 1; nothing and 0 when every target has an expression and
# there is at least one. A dashboard with no target would otherwise "run all 0
# queries" and pass.
dashboard_target_problem() {
  local targets empty
  targets="$(dashboard_targets <<<"$1")"
  if [[ "$(jq 'length' <<<"${targets}")" == 0 ]]; then
    printf 'it has no query to run (no panel, nested in a row or not, has a target), so "all queries ran" would prove nothing'
    return 1
  fi
  empty="$(jq -r 'map(select((.expr // "") == "") | .title) | unique | join(", ")' <<<"${targets}")"
  if [[ -n "${empty}" ]]; then
    printf 'a target of panel(s) %s has no expression' "$(clean_lines "${empty}")"
    return 1
  fi
}

# run_dashboard_queries SERVED: every target of the served dashboard, with the
# range variable set to an hour (3600) and, for a target that names $dimension,
# once per option of that variable. Leaves the count in ${queries_run}; the first
# refusal returns 1 (see run_dashboard_query).
run_dashboard_queries() {
  local served=$1 targets total i title expr options option
  # shellcheck disable=SC2016 # both are Grafana's variable names, written literally
  local range_ref='${__range_s}' dimension_ref='$dimension'
  queries_run=0
  query_error=""
  options="$(jq -r '(.templating.list // [])[] | select(.name == "dimension") | .options[].value' <<<"${served}")"
  targets="$(dashboard_targets <<<"${served}")"
  total="$(jq 'length' <<<"${targets}")"
  for ((i = 0; i < total; i++)); do
    title="$(clean_lines "$(jq -r --argjson i "${i}" '.[$i].title' <<<"${targets}")")"
    expr="$(jq -r --argjson i "${i}" '.[$i].expr' <<<"${targets}")"
    expr="${expr//"${range_ref}"/3600}"
    if [[ "${expr}" != *"${dimension_ref}"* ]]; then
      run_dashboard_query "${title}" "${expr}" || return 1
      continue
    fi
    if [[ -z "${options}" ]]; then
      query_error="${title}: the dashboard has no dimension options"
      return 1
    fi
    while IFS= read -r option; do
      run_dashboard_query "${title}" "${expr//"${dimension_ref}"/${option}}" || return 1
    done <<<"${options}"
  done
}

# check_dashboard UID FILE: the dashboard line, for the cost dashboard (check 5)
# and the health dashboard (check 11). What Grafana serves under UID must be the
# file's: a stale provisioned copy fails. Its queries are then run, so a renamed
# series or a typo shows here. A dashboard with no target, or a target with no
# expression, is a FAIL: "all 0 queries ran" proves nothing. Targets of panels
# nested in rows count (dashboard_targets).
check_dashboard() {
  local uid=$1 file=$2 served title file_exprs served_exprs problem
  if ! poll '(select(.meta.provisioned == true) | .dashboard) // empty | tojson' \
    "${grafana_url}/api/dashboards/uid/${uid}"; then
    fail "dashboard: Grafana has no provisioned dashboard ${uid} after ${POLL_TIMEOUT}s (run make up) (last answer: ${poll_error})"
    return
  fi
  served="${poll_result}"
  title="$(clean_lines "$(jq -r '.title // empty' <<<"${served}" 2>/dev/null)")"
  if ! file_exprs="$(dashboard_targets <"${file}" | jq -c 'map(.expr)')"; then
    fail "dashboard: could not read the queries of ${file}"
    return
  fi
  served_exprs="$(dashboard_targets <<<"${served}" 2>/dev/null | jq -c 'map(.expr)' 2>/dev/null || true)"
  if [[ "${served_exprs}" != "${file_exprs}" ]]; then
    fail "dashboard: Grafana serves \"${title}\" but its queries differ from infra/kind/dashboards/${file##*/} (run make up)"
    return
  fi
  if ! problem="$(dashboard_target_problem "${served}")"; then
    fail "dashboard: Grafana serves \"${title}\" (uid ${uid}) but ${problem} (infra/kind/dashboards/${file##*/})"
    return
  fi
  if run_dashboard_queries "${served}"; then
    pass "dashboard: Grafana serves \"${title}\" (uid ${uid}), provisioned, with the file's queries; all ${queries_run} queries ran in Prometheus (range 3600s)"
  else
    fail "dashboard: a query of \"${title}\" failed in Prometheus: ${query_error}"
  fi
}

# The series line. A gateway process exports once a minute, so the ledger says
# whether a series is due: only attempts settled since the process started can
# be in Prometheus. The start time is checked before it goes into SQL.
check_cost_series() {
  local found available started primary err_file detail answer settled first_settled
  local name got final prom_status missing series_list query last
  series_list="$(printf '%s, ' "${COST_SERIES[@]}")"
  series_list="${series_list%, }"
  local start_pattern='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'
  local ledger_pattern='^([0-9]+)\|([0-9]*)$' # "<count>|<epoch second>"
  local query_url="${grafana_url}/api/datasources/proxy/uid/prometheus/api/v1/query"
  local selector='{__name__=~"meridian_gateway_(tokens|cost_EUR|calls)_total", job="model-gateway"}'
  if ! found="$(deployed_services)"; then
    fail "cost series: could not look for the Meridian deployments (kubectl's error is above)"
    return
  fi
  if [[ -z "${found}" ]]; then
    skip "cost series: the Meridian services are not deployed (make deploy)"
    return
  fi
  # A gateway that is down has settled nothing since its start, and that must not
  # read as one waiting for a claim: it fails here instead of skipping below.
  available="$(kctl -n meridian get deployment model-gateway \
    -o jsonpath='{.status.availableReplicas}' || true)"
  [[ "${available}" =~ ^[0-9]+$ ]] || available=0
  if ((available < 1)); then
    fail "cost series: the gateway is not available (Deployment model-gateway reports ${available} available replicas)"
    return
  fi
  # The newest container start among the gateway's pods (RFC 3339, so it sorts).
  if ! started="$(kctl -n meridian get pod -l app.kubernetes.io/name=model-gateway \
    -o jsonpath='{range .items[*]}{.status.containerStatuses[?(@.name=="model-gateway")].state.running.startedAt}{"\n"}{end}' |
    sort | tail -n 1)"; then
    fail "cost series: could not read the gateway's pods (kubectl's error is above)"
    return
  fi
  if ! [[ "${started}" =~ ${start_pattern} ]]; then
    fail "cost series: could not read when the gateway process started (the newest model-gateway pod has no running start time in the form 2026-01-31T08:00:00Z)"
    return
  fi
  # kubectl's stderr goes to a file (as in check_tools) and into the FAIL line.
  err_file="$(mktemp)"
  if ! primary="$(kctl -n meridian get pod \
    -l cnpg.io/cluster=platform-db,cnpg.io/instanceRole=primary \
    -o jsonpath='{.items[0].metadata.name}' 2>"${err_file}")" || [[ -z "${primary}" ]]; then
    detail="$(clean_lines "$(<"${err_file}")")"
    rm -f "${err_file}"
    fail "cost series: no primary pod found for platform-db${detail:+ (kubectl said: ${detail})}"
    return
  fi
  if ! answer="$(kctl -n meridian exec "${primary}" -c postgres -- \
    env "PGOPTIONS=${PSQL_OPTIONS}" psql -d meridian -tAc "SELECT count(*) || '|' || coalesce(floor(extract(epoch FROM min(closed_at)))::bigint::text, '') FROM gateway.usage WHERE state = 'settled' AND closed_at > '${started}'" \
    2>"${err_file}")"; then
    detail="$(clean_lines "$(<"${err_file}")")"
    rm -f "${err_file}"
    fail "cost series: could not count the settled attempts in gateway.usage of ${primary}${detail:+ (kubectl said: ${detail})}"
    return
  fi
  rm -f "${err_file}"
  answer="$(clean_lines "${answer}")"
  if ! [[ "${answer}" =~ ${ledger_pattern} ]]; then
    fail "cost series: the ledger's answer was not a count and an epoch second"
    return
  fi
  settled="${BASH_REMATCH[1]}"
  first_settled="${BASH_REMATCH[2]}"
  if ((10#${settled} == 0)); then
    skip "cost series: the gateway has settled no call since it started at ${started} (make demo sends a claim)"
    return
  fi
  if [[ -z "${first_settled}" ]]; then
    fail "cost series: the ledger has ${settled} settled attempt(s) since ${started} but no time for the first"
    return
  fi
  # The previous process's series stay visible for five minutes after a restart: keep
  # samples exported since the first settled attempt (`and`: timestamp() drops __name__).
  query="count by (__name__) (${selector} and (timestamp(${selector}) >= ${first_settled}))"
  if poll "[.data.result[].metric.__name__] as \$got
    | if ($(printf '%s\n' "${COST_SERIES[@]}" | jq -R . | jq -sc .) - \$got | length) == 0
      then \$got | sort | join(\", \") else empty end" \
    -G "${query_url}" --data-urlencode "query=${query}"; then
    pass "cost series: ${settled} settled attempt(s) in the ledger since the gateway started at ${started}; Prometheus has ${series_list}"
    return
  fi
  # Which of them did not come, and did Prometheus answer at all: one more look,
  # for the message only. ${poll_error} is what the last attempt saw.
  last="(last answer: ${poll_error})"
  final="$(gcurl -G "${query_url}" --data-urlencode "query=${query}" 2>/dev/null || true)"
  prom_status="$(jq -r '.status // empty' <<<"${final}" 2>/dev/null || true)"
  if [[ "${prom_status}" != success ]]; then
    fail "cost series: Prometheus did not answer the query with status success after ${POLL_TIMEOUT}s ${last}"
    return
  fi
  got="$(clean_lines "$(jq -r '[.data.result[].metric.__name__] | join(" ")' <<<"${final}" 2>/dev/null || true)")"
  missing=""
  for name in "${COST_SERIES[@]}"; do
    [[ " ${got} " == *" ${name} "* ]] || missing="${missing:+${missing}, }${name}"
  done
  if [[ "${missing}" == "${series_list}" ]]; then
    fail "cost series: none of the three series came from the gateway after ${POLL_TIMEOUT}s, though the ledger has ${settled} settled attempt(s) since ${started} (it exports once a minute; is the collector up?) ${last}"
  else
    fail "cost series: missing ${missing} in Prometheus after ${POLL_TIMEOUT}s ${last}"
  fi
}

# Grafana's service account must not read Secrets, in the namespace of the
# database roles' passwords or in its own (the Helm release Secrets, T-68).
# `can-i` prints yes or no on stdout and exits 1 for no; stderr is not an answer.
check_grafana_rights() {
  local namespace answer
  for namespace in meridian observability; do
    answer="$(kctl auth can-i get secrets -n "${namespace}" --as "${GRAFANA_ACCOUNT}" 2>/dev/null || true)"
    if [[ "${answer}" != no ]]; then
      fail "grafana rights: expected \"no\" to reading Secrets in ${namespace} as ${GRAFANA_ACCOUNT}, got \"$(clean_lines "${answer}")\""
      return
    fi
  done
  pass "grafana rights: Grafana's service account may not read Secrets in meridian or observability (T-68)"
}

# kube-state-metrics' service account must not get, list or watch Secrets in the
# namespace of the database roles' passwords or in the one of the CA's key: the
# chart's `secrets` collector is off (values/kube-prometheus-stack.yaml), and the
# ClusterRole it derives from the collectors has no rule for them. Same shape as
# check_grafana_rights: stdout is the answer, stderr is not. A role that came
# back with a rule for Secrets fails here, naming the verb and the namespace.
check_kube_state_metrics_rights() {
  local namespace verb answer
  for namespace in meridian cert-manager; do
    for verb in get list watch; do
      answer="$(kctl auth can-i "${verb}" secrets -n "${namespace}" --as "${KSM_ACCOUNT}" 2>/dev/null || true)"
      if [[ "${answer}" != no ]]; then
        fail "kube-state-metrics rights: expected \"no\" to ${verb} of Secrets in ${namespace} as ${KSM_ACCOUNT}, got \"$(clean_lines "${answer}")\""
        return
      fi
    done
  done
  pass "kube-state-metrics rights: its service account may not get, list or watch Secrets in meridian or cert-manager (T-68)"
}

check_cost_panel() {
  if open_grafana; then # otherwise it printed the one FAIL line
    check_dashboard "${DASHBOARD_UID}" "${DASHBOARD_FILE}"
    check_cost_series
  fi
  check_grafana_rights
  check_kube_state_metrics_rights
}

# ── 6. adjuster pages ────────────────────────────────────────────────────────
# The pages are served by the Claims API (S016), through the edge, in the form
# demo.sh reaches it. No line changes a claim: the second is refused by the
# origin check before any lookup, so CLM-9999 need not exist, and the third is a
# GET of the claimant's start page (S049).
check_adjuster_pages() {
  local found headers_file body_file status csp missing=""
  # Skipped only when no Meridian Deployment exists, as in check_tools.
  if ! found="$(deployed_services)"; then
    fail "adjuster pages: could not look for the Meridian deployments (kubectl's error is above)"
    return
  fi
  if [[ -z "${found}" ]]; then
    skip "adjuster pages: the Meridian services are not deployed (make deploy)"
    return
  fi

  headers_file="$(mktemp)"
  body_file="$(mktemp)"
  if ! status="$(curl -q --noproxy '*' -sS -m 10 -D "${headers_file}" -o "${body_file}" \
    -w '%{http_code}' "${ADJUSTER_QUEUE_URL}" 2>&1)"; then
    fail "adjuster pages: ${ADJUSTER_QUEUE_URL} did not answer: $(clean_lines "${status}")"
  else
    status="$(clean_lines "${status}")"
    # Header names are case-insensitive; the lines end in a carriage return.
    csp="$(grep -i '^content-security-policy:' "${headers_file}" | LC_ALL=C tr -cd '[:print:]' || true)"
    [[ "${status}" == 200 ]] || missing="status 200 (got ${status})"
    [[ "${csp}" == *"frame-ancestors 'none'"* ]] ||
      missing="${missing:+${missing}, }header Content-Security-Policy with frame-ancestors 'none'"
    [[ "${csp}" == *"default-src 'none'"* ]] ||
      missing="${missing:+${missing}, }header Content-Security-Policy with default-src 'none'"
    grep -qF "${ADJUSTER_BANNER}" "${body_file}" ||
      missing="${missing:+${missing}, }text \"${ADJUSTER_BANNER}\""
    if [[ -z "${missing}" ]]; then
      pass "adjuster pages: ${ADJUSTER_QUEUE_URL} -> 200 with a Content-Security-Policy of frame-ancestors 'none' and default-src 'none', and the synthetic-data line"
    else
      fail "adjuster pages: ${ADJUSTER_QUEUE_URL} lacks: ${missing}"
    fi
  fi
  rm -f "${headers_file}" "${body_file}"

  if ! status="$(curl -q --noproxy '*' -sS -m 10 -o /dev/null -w '%{http_code}' \
    -H 'Origin: http://attacker.example' --data 'decision=approve' \
    "${ADJUSTER_DECISION_URL}" 2>&1)"; then
    fail "adjuster pages: ${ADJUSTER_DECISION_URL} did not answer: $(clean_lines "${status}")"
  else
    status="$(clean_lines "${status}")"
    if [[ "${status}" == 403 ]]; then
      pass "adjuster pages: a decision posted with Origin http://attacker.example -> 403, refused before any lookup"
    else
      fail "adjuster pages: a decision posted with Origin http://attacker.example expected 403, got ${status}"
    fi
  fi

  # The claimant's start page (S049), served by the same app with the same
  # headers; its banner asks for fictional data (T-04). A GET, so no claim.
  headers_file="$(mktemp)"
  body_file="$(mktemp)"
  missing=""
  if ! status="$(curl -q --noproxy '*' -sS -m 10 -D "${headers_file}" -o "${body_file}" \
    -w '%{http_code}' "${CLAIMANT_START_URL}" 2>&1)"; then
    fail "adjuster pages: ${CLAIMANT_START_URL} did not answer: $(clean_lines "${status}")"
  else
    status="$(clean_lines "${status}")"
    csp="$(grep -i '^content-security-policy:' "${headers_file}" | LC_ALL=C tr -cd '[:print:]' || true)"
    [[ "${status}" == 200 ]] || missing="status 200 (got ${status})"
    [[ "${csp}" == *"frame-ancestors 'none'"* ]] ||
      missing="${missing:+${missing}, }header Content-Security-Policy with frame-ancestors 'none'"
    [[ "${csp}" == *"default-src 'none'"* ]] ||
      missing="${missing:+${missing}, }header Content-Security-Policy with default-src 'none'"
    grep -qF "${CLAIMANT_BANNER}" "${body_file}" ||
      missing="${missing:+${missing}, }text \"${CLAIMANT_BANNER}\""
    if [[ -z "${missing}" ]]; then
      pass "adjuster pages: ${CLAIMANT_START_URL} -> 200 with a Content-Security-Policy of frame-ancestors 'none' and default-src 'none', and the fictional-data line (T-04)"
    else
      fail "adjuster pages: ${CLAIMANT_START_URL} lacks: ${missing}"
    fi
  fi
  rm -f "${headers_file}" "${body_file}"
}

# ── 7. sweep ─────────────────────────────────────────────────────────────────
# The API server's timestamps (the CronJob's creation and lastScheduleTime, a
# Job's completionTime and conditions) are compared with each other and with
# "now", which is the database's clock (server_epoch): the CronJob has none of
# its own, and this laptop's is not the node's (see deploy.sh).
#
# server_epoch: the database's now() as whole seconds since the epoch, nothing
# else on stdout; fails when the primary or the answer cannot be read, and then
# prints the reason instead (cleaned and cut: see meridian_query).
server_epoch() {
  local primary answer
  primary="$(platform_db_primary)" || {
    printf 'no primary pod of platform-db'
    return 1
  }
  answer="$(meridian_query "${primary}" "${SWEEP_CLOCK_SQL}")" || {
    printf '%s' "${answer}"
    return 1
  }
  answer="$(clean_lines "${answer}")"
  [[ "${answer}" =~ ^[0-9]+$ ]] || {
    printf 'the answer was not a whole number of seconds'
    return 1
  }
  printf '%s' "${answer}"
}

# sweep_period CRONJOB_JSON: the schedule's period in seconds when its
# .spec.schedule is "*/N * * * *" with N from 1 to 59, else
# SWEEP_PERIOD_SECONDS.
sweep_period() {
  local schedule minutes
  schedule="$(jq -r '.spec.schedule // ""' <<<"$1")"
  if [[ "${schedule}" =~ ^\*/([0-9]{1,2})\ \*\ \*\ \*\ \*$ ]]; then
    minutes=$((10#${BASH_REMATCH[1]}))
    if ((minutes >= 1 && minutes <= 59)); then
      printf '%s' "$((minutes * 60))"
      return
    fi
  fi
  printf '%s' "${SWEEP_PERIOD_SECONDS}"
}

# sweep_verdict CRONJOB_JSON JOBS_JSON NOW PERIOD: one line, fields separated by
# "|". NOW is the database's clock in epoch seconds, PERIOD the schedule's in
# seconds; a run is overdue after SWEEP_STALE_PERIODS of them.
#   succeeded|JOB|FINISHED_AT          the newest finished Job of the CronJob
#   failed|JOB|FINISHED_AT|REASON      (scheduled or made by hand with
#                                      `kubectl create job --from=cronjob/...`,
#                                      which has the same owner) and how it ended
#   stale|SCHEDULED_AT|FINISHED_AT     last scheduled more than three periods
#                                      after that Job finished, nothing running:
#                                      the schedule makes no finished runs
#   stopped|JOB|FINISHED_AT|SECONDS    the newest finished Job succeeded more
#                                      than three periods before NOW, nothing
#                                      running: the schedule stopped
#   stopped||SCHEDULED_AT|SECONDS      the same with no Job of the CronJob left
#                                      (the sweep keeps one success and the
#                                      cluster removes a Job a day after it
#                                      finished): last scheduled more than
#                                      three periods before NOW, nothing
#                                      running
#   young|SECONDS|JOB|FINISHED_AT      the same, but the CronJob was created
#                                      less than one period before NOW: that Job
#                                      is an earlier CronJob's, not overdue
#   never|SECONDS                      never scheduled, and the CronJob was
#                                      created more than three periods before NOW
#   unscheduled|SECONDS|CREATED_AT     never scheduled, not yet overdue
#   running                            no Job has finished; one is running
#   none|SCHEDULED_AT                  no Job has finished, none is running
sweep_verdict() {
  jq -nr --arg cronjob "${SWEEP_CRONJOB}" --argjson cj "$1" --argjson jobs "$2" \
    --argjson now "$3" --argjson period "$4" \
    --argjson tolerance "$(($4 * SWEEP_STALE_PERIODS))" '
    def epoch: sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601;
    ($cj.status.lastScheduleTime // null) as $scheduled
    | (($cj.status.active // []) | length) as $active
    | [$jobs.items[]
        | select(any(.metadata.ownerReferences[]?; .kind == "CronJob" and .name == $cronjob))
        | . as $job
        | ([$job.status.conditions[]? | select(.status == "True" and (.type == "Complete" or .type == "Failed"))] | first // empty) as $done
        | {name: $job.metadata.name, type: $done.type, reason: ($done.reason // ""),
           at: ((if $done.type == "Complete" then ($job.status.completionTime // $done.lastTransitionTime) else $done.lastTransitionTime end) // "")}]
    | (sort_by([.at, .name]) | last) as $newest
    | ($now - ($cj.metadata.creationTimestamp | epoch)) as $age
    | if $scheduled == null and $age > $tolerance then "never|\($age)"
      elif $newest == null then
        if $active > 0 then "running"
        elif $scheduled == null then "unscheduled|\($age)|\($cj.metadata.creationTimestamp)"
        elif ($now - ($scheduled | epoch)) > $tolerance then "stopped||\($scheduled)|\($now - ($scheduled | epoch))"
        else "none|\($scheduled)" end
      elif $scheduled != null and $active == 0 and (($scheduled | epoch) - ($newest.at | epoch)) > $tolerance then
        "stale|\($scheduled)|\($newest.at)"
      elif $newest.type == "Complete" and $active == 0 and ($now - ($newest.at | epoch)) > $tolerance then
        if $age < $period then "young|\($age)|\($newest.name)|\($newest.at)"
        else "stopped|\($newest.name)|\($newest.at)|\($now - ($newest.at | epoch))" end
      elif $newest.type == "Complete" then "succeeded|\($newest.name)|\($newest.at)"
      else "failed|\($newest.name)|\($newest.at)|\($newest.reason)" end
  '
}

# report_sweep VERDICT PERIOD: the line for a verdict of sweep_verdict, whose
# fields were cleaned of anything that is not printable ASCII.
report_sweep() {
  local kind first second third bound=$(($2 * SWEEP_STALE_PERIODS))
  IFS='|' read -r kind first second third <<<"$1"
  first="$(clean_lines "${first}")"
  second="$(clean_lines "${second}")"
  third="$(clean_lines "${third}")"
  case "${kind}" in
    succeeded)
      pass "sweep: cronjob/${SWEEP_CRONJOB} is not suspended and its last finished Job, ${first}, succeeded at ${second}"
      ;;
    failed)
      fail "sweep: the last finished Job of cronjob/${SWEEP_CRONJOB}, ${first}, failed at ${second} (${third:-no reason given}); kubectl -n meridian describe job/${first} shows why, and kubectl -n meridian logs job/${first} what its pod printed, if a pod started"
      ;;
    stale)
      fail "sweep: the schedule is not producing finished runs: cronjob/${SWEEP_CRONJOB} was last scheduled at ${first}, more than ${bound} s after its newest finished Job finished at ${second}, and no Job is running"
      ;;
    stopped)
      if [[ -z "${first}" ]]; then
        fail "sweep: the schedule stopped: cronjob/${SWEEP_CRONJOB} was last scheduled at ${second}, ${third} s before the database's clock now, more than the ${bound} s (three periods of ${2} s) allowed; no Job of cronjob/${SWEEP_CRONJOB} is left (finished Jobs are removed a day after they finish) and none is running"
      else
        fail "sweep: the schedule stopped: the newest finished Job of cronjob/${SWEEP_CRONJOB}, ${first}, succeeded at ${second}, ${third} s before the database's clock now, more than the ${bound} s (three periods of ${2} s) allowed, and no Job is running"
      fi
      ;;
    young)
      skip "sweep: cronjob/${SWEEP_CRONJOB} was deployed ${first} s ago by the database's clock, less than one period (${2} s); its newest finished Job, ${second}, finished at ${third}, which is an earlier CronJob's, so it is not judged yet"
      ;;
    never)
      fail "sweep: cronjob/${SWEEP_CRONJOB} has never been scheduled, although it was created ${first} s ago by the database's clock (more than three periods, ${bound} s): the schedule is not producing runs"
      ;;
    unscheduled)
      skip "sweep: cronjob/${SWEEP_CRONJOB} has not been scheduled yet (created ${second}, ${first} s ago by the database's clock), within the ${bound} s it allows"
      ;;
    running)
      skip "sweep: no Job of cronjob/${SWEEP_CRONJOB} has finished yet; the first one is running"
      ;;
    none)
      skip "sweep: no Job of cronjob/${SWEEP_CRONJOB} has finished yet (last scheduled at ${first})"
      ;;
    *)
      fail "sweep: unexpected verdict from the timestamps"
      ;;
  esac
}

# Same skip rule as the tool check: only when no Meridian Deployment exists. Only
# reads (kubectl get, and one SELECT of the database's clock); the Jobs of the
# whole namespace are listed and filtered by owner.
check_sweep() {
  local found cronjob jobs verdict now period
  if ! found="$(deployed_services)"; then
    fail "sweep: could not look for the Meridian deployments (kubectl's error is above)"
    return
  fi
  if [[ -z "${found}" ]]; then
    skip "sweep: the Meridian services are not deployed (make deploy)"
    return
  fi
  if ! cronjob="$(kctl -n meridian get cronjob "${SWEEP_CRONJOB}" -o json --ignore-not-found)"; then
    fail "sweep: could not read cronjob/${SWEEP_CRONJOB} (kubectl's error is above)"
    return
  fi
  if [[ -z "${cronjob}" ]]; then
    fail "sweep: cronjob/${SWEEP_CRONJOB} does not exist (make deploy)"
    return
  fi
  if [[ "$(jq -r '.spec.suspend // false' <<<"${cronjob}")" == true ]]; then
    skip "sweep: cronjob/${SWEEP_CRONJOB} is suspended (spec.suspend), so it makes no runs and none can be overdue"
    return
  fi
  if ! jobs="$(kctl -n meridian get job -o json)"; then
    fail "sweep: could not read the Jobs in meridian (kubectl's error is above)"
    return
  fi
  if ! now="$(server_epoch)"; then
    fail "sweep: could not read the database's clock (now() in the primary pod of platform-db: ${now}), so a schedule that stopped cannot be judged"
    return
  fi
  period="$(sweep_period "${cronjob}")"
  if ! verdict="$(sweep_verdict "${cronjob}" "${jobs}" "${now}" "${period}")"; then
    fail "sweep: could not read the CronJob's and the Jobs' timestamps and conditions"
    return
  fi
  report_sweep "${verdict}" "${period}"
}

# ── 8. network policy ────────────────────────────────────────────────────────
# The tool check above proves the paths the policies allow; this one proves
# that the paths they do not allow are closed, and that its probe can tell. Skipped
# like it, when the Claims API is not deployed.

# network_sweep_leftovers: at the start of check 8, delete by name the Pods with
# NETWORK_POD_SMOKE_LABEL that are older than NETWORK_LEFTOVER_AGE seconds: what
# a run whose trap was lost (SIGKILL, a power cut) left. A Pod that passed its
# activeDeadlineSeconds stays as a Failed object, with the labels the policies
# select on. A younger one is another run's that is under way, and is its own to
# delete. The age is this machine's clock against the Pod's creationTimestamp
# (the API server's), so a skewed clock only delays the sweep. A list that cannot
# be read is not an error: the check goes on, and the leftover waits.
network_sweep_leftovers() {
  local namespace=${1:-meridian} json names leftover
  json="$(kctl -n "${namespace}" get pod -l "${NETWORK_POD_SMOKE_LABEL}" \
    -o json 2>/dev/null)" || return 0
  names="$(jq -r --argjson age "${NETWORK_LEFTOVER_AGE}" '
    .items[] | select(now - (.metadata.creationTimestamp | fromdateiso8601) > $age)
    | .metadata.name' <<<"${json}" 2>/dev/null)" || return 0
  while IFS= read -r leftover; do
    [[ -n "${leftover}" ]] || continue
    kctl -n "${namespace}" delete pod "${leftover}" \
      --ignore-not-found --wait=false >/dev/null 2>&1 || true
  done <<<"${names}"
}

# network_probe WHERE TARGET [NAMESPACE]: the probe from WHERE (deploy/claims-api,
# or the name of the probe Pod) in NAMESPACE (meridian unless given) to TARGET
# (host:port), its answer in ${network_answer}:
# "reached", "blocked", or "error: ..." with what it wrote on stderr when it failed
# (a traceback is never read as an answer).
network_probe() {
  local where=$1 target=$2 namespace=${3:-meridian} err_file
  err_file="$(mktemp)"
  if network_answer="$(kctl -n "${namespace}" exec "${where}" -- \
    python -c "${NETWORK_PROBE}" "${target%:*}" "${target##*:}" 2>"${err_file}")"; then
    network_answer="$(clean_lines "${network_answer}")"
  else
    network_answer="error: $(clean_lines "${network_answer}") $(clean_lines "$(<"${err_file}")")"
  fi
  rm -f "${err_file}"
}

# network_expect EXPECTED WHERE TARGET PASS_TEXT WRONG_TEXT [NAMESPACE
# [UNPROVEN_TEXT]]: a PASS line with PASS_TEXT when the probe's answer is
# EXPECTED (reached or blocked); a FAIL with WRONG_TEXT when it is the other
# word; a FAIL with what it said when it is neither. WHERE is in NAMESPACE
# (meridian unless given). A non-empty UNPROVEN_TEXT turns the PASS into a FAIL
# with that text: the answer is the expected one, but a control the caller
# needs for it to mean anything did not hold. Returns 1 unless it passed.
network_expect() {
  local expected=$1 where=$2 target=$3 pass_text=$4 wrong_text=$5 namespace=${6:-meridian} unproven_text=${7:-}
  network_probe "${where}" "${target}" "${namespace}"
  if [[ "${network_answer}" == "${expected}" && -n "${unproven_text}" ]]; then
    fail "network policy: ${unproven_text}"
  elif [[ "${network_answer}" == "${expected}" ]]; then
    pass "network policy: ${pass_text}"
    return 0
  elif [[ "${network_answer}" == reached || "${network_answer}" == blocked ]]; then
    fail "network policy: ${wrong_text}"
  else
    fail "network policy: the probe in ${where} to ${target} gave no answer of reached or blocked: ${network_answer}"
  fi
  return 1
}

# network_pod_spec NAME NAMESPACE NAME_LABEL: the probe Pod as JSON, from the
# Claims API's Deployment: its image, pull policy and security contexts, so the
# Pod runs what is on the node under the same restrictions, and a sleep that ends
# on its own. Its labels are NAME_LABEL as the name label (the sweep's, and not
# part-of: see the header; empty: no name label at all, for the Pod in `default`)
# and smoke's own, which the next run's sweep finds a leftover by.
network_pod_spec() {
  kctl -n meridian get deployment claims-api -o json | jq --arg name "$1" \
    --arg namespace "$2" --arg label "$3" --argjson lifetime "${NETWORK_POD_LIFETIME}" \
    --arg smoke_key "${NETWORK_POD_SMOKE_LABEL%%=*}" --arg smoke_value "${NETWORK_POD_SMOKE_LABEL#*=}" '
    .spec.template.spec as $pod | $pod.containers[0] as $container | {
      apiVersion: "v1", kind: "Pod",
      metadata: {
        name: $name, namespace: $namespace,
        labels: ({($smoke_key): $smoke_value}
          + (if $label == "" then {} else {"app.kubernetes.io/name": $label} end))
      },
      spec: {
        restartPolicy: "Never", automountServiceAccountToken: false,
        activeDeadlineSeconds: $lifetime, securityContext: $pod.securityContext,
        containers: [{
          name: "probe", image: $container.image, imagePullPolicy: $container.imagePullPolicy,
          command: ["python", "-c", "import time; time.sleep(\($lifetime))"],
          securityContext: $container.securityContext,
          resources: {requests: {cpu: "10m", memory: "32Mi"}, limits: {memory: "128Mi"}}
        }]
      }
    }'
}

# network_start_pod: start the probe Pod and wait until it is Ready. One FAIL
# line (the database line) and 1 when it cannot. ${network_pod} is set before the
# Pod exists, so the trap deletes it whichever step fails.
network_start_pod() {
  local err_file detail
  network_pod="smoke-network-$(date +%s)"
  err_file="$(mktemp)"
  if ! network_pod_spec "${network_pod}" meridian "${NETWORK_POD_NAME_LABEL}" 2>"${err_file}" |
    kctl -n meridian create -f - >/dev/null 2>>"${err_file}"; then
    detail="$(clean_lines "$(<"${err_file}")")"
    rm -f "${err_file}"
    fail "network policy: could not start the probe pod ${network_pod} in meridian (${detail})"
    return 1
  fi
  if ! kctl -n meridian wait --for=condition=Ready "pod/${network_pod}" \
    --timeout="${NETWORK_POD_READY_TIMEOUT}" >/dev/null 2>"${err_file}"; then
    detail="$(clean_lines "$(<"${err_file}")")"
    rm -f "${err_file}"
    fail "network policy: the probe pod ${network_pod} did not become Ready within ${NETWORK_POD_READY_TIMEOUT} (${detail})"
    return 1
  fi
  rm -f "${err_file}"
}

# network_database_lines: the database's line, from the probe Pod. Unlabelled it
# must time out; given the part-of label (the rule the database admits by) the same
# probe must reach, for up to NETWORK_LABEL_ATTEMPTS tries.
network_database_lines() {
  local part_of=app.kubernetes.io/part-of=meridian attempt
  network_probe "${network_pod}" "${NETWORK_DATABASE}"
  if [[ "${network_answer}" == reached ]]; then
    fail "network policy: a pod without the label ${part_of} reached the database (${NETWORK_DATABASE}): the database's ingress is too wide, or the cluster does not enforce it"
    return
  elif [[ "${network_answer}" != blocked ]]; then
    fail "network policy: the probe in ${network_pod} to ${NETWORK_DATABASE} gave no answer of reached or blocked: ${network_answer}"
    return
  fi
  if ! kctl -n meridian label pod "${network_pod}" "${part_of}" >/dev/null 2>&1; then
    fail "network policy: could not add the label ${part_of} to the probe pod ${network_pod}"
    return
  fi
  for ((attempt = 1; attempt <= NETWORK_LABEL_ATTEMPTS; attempt++)); do
    network_probe "${network_pod}" "${NETWORK_DATABASE}"
    [[ "${network_answer}" == blocked ]] || break
    ((attempt == NETWORK_LABEL_ATTEMPTS)) || sleep "${NETWORK_LABEL_INTERVAL}"
  done
  if [[ "${network_answer}" == reached ]]; then
    pass "network policy: a pod without the label ${part_of} cannot reach the database (${NETWORK_DATABASE}), and with that label added the same pod can"
  elif [[ "${network_answer}" == blocked ]]; then
    fail "network policy: the pod still cannot reach the database (${NETWORK_DATABASE}) after the label ${part_of} was added, in ${NETWORK_LABEL_ATTEMPTS} tries: the sweep's egress or the database's ingress is too narrow, and the line above would prove nothing"
  else
    fail "network policy: the probe in ${network_pod} to ${NETWORK_DATABASE} gave no answer of reached or blocked: ${network_answer}"
  fi
}

check_network_database() {
  if network_start_pod; then
    network_database_lines
  fi
  network_delete_pod || true # the Pod stays named: the EXIT trap tries again
}

# network_outsider_delete: delete the probe Pod of the collector line when one was
# named, and forget it only when kubectl said it is gone, as network_delete_pod
# does for the other Pod, so a delete that failed is tried again by the EXIT trap.
# Returns 1 when the delete failed.
network_outsider_delete() {
  [[ -n "${network_outsider}" ]] || return 0
  kctl -n "${NETWORK_OUTSIDER_NAMESPACE}" delete pod "${network_outsider}" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || return 1
  network_outsider=""
}

# network_outsider_start: start the probe Pod of the collector line in
# NETWORK_OUTSIDER_NAMESPACE, with no name label, and wait until it is Ready. One
# FAIL line and 1 when it cannot. ${network_outsider} is set before the Pod
# exists, so the trap deletes it whichever step fails.
network_outsider_start() {
  local err_file detail
  network_outsider="${NETWORK_OUTSIDER_PREFIX}$(date +%s)"
  err_file="$(mktemp)"
  if ! network_pod_spec "${network_outsider}" "${NETWORK_OUTSIDER_NAMESPACE}" "" 2>"${err_file}" |
    kctl -n "${NETWORK_OUTSIDER_NAMESPACE}" create -f - >/dev/null 2>>"${err_file}"; then
    detail="$(clean_lines "$(<"${err_file}")")"
    rm -f "${err_file}"
    fail "network policy: could not start the probe pod ${network_outsider} in ${NETWORK_OUTSIDER_NAMESPACE} (${detail})"
    return 1
  fi
  if ! kctl -n "${NETWORK_OUTSIDER_NAMESPACE}" wait --for=condition=Ready "pod/${network_outsider}" \
    --timeout="${NETWORK_POD_READY_TIMEOUT}" >/dev/null 2>"${err_file}"; then
    detail="$(clean_lines "$(<"${err_file}")")"
    rm -f "${err_file}"
    fail "network policy: the probe pod ${network_outsider} in ${NETWORK_OUTSIDER_NAMESPACE} did not become Ready within ${NETWORK_POD_READY_TIMEOUT} (${detail})"
    return 1
  fi
  rm -f "${err_file}"
}

# check_network_collector: the fifth line of check 8 (S063). A pod outside
# `meridian` and `observability` cannot push to the collector: the probe Pod in
# NETWORK_OUTSIDER_NAMESPACE must time out on the collector's HTTP port, which
# only the pods of `meridian` are admitted to. SKIP when the collector's
# Deployment is not there. The control of the probe is check 8's own (the caller
# returns before this when it failed). A timeout alone cannot tell a policy that
# blocks from a collector that is up but hangs, so the line also needs check 4's
# push from `meridian` (${telemetry_pushed}, set by check_telemetry): when that
# did not pass in this run, a timeout from `default` is a FAIL that proves
# nothing, not a PASS. The probe still runs then, so an answer from the
# collector is told as ever.
check_network_collector() {
  local found unproven=""
  network_sweep_leftovers "${NETWORK_OUTSIDER_NAMESPACE}"
  if ! found="$(kctl -n observability get deployment otel-collector -o name --ignore-not-found)"; then
    fail "network policy: could not look for deployment/otel-collector in observability (kubectl's error is above)"
    return
  fi
  if [[ -z "${found}" ]]; then
    skip "network policy: the collector is not deployed (make up), so no push to it was tried from outside meridian"
    return
  fi
  if [[ "${telemetry_pushed}" != yes ]]; then
    unproven="a probe in ${NETWORK_OUTSIDER_NAMESPACE} timed out on the collector (${COLLECTOR_ENDPOINT}), but check 4's push from meridian did not pass in this run: the collector was not reached from meridian either, so a timeout from ${NETWORK_OUTSIDER_NAMESPACE} shows nothing, and the line proves nothing (a collector that is up but hangs times out for every pod; fix check 4's lines first)"
  fi
  if network_outsider_start; then
    network_expect blocked "${network_outsider}" "${COLLECTOR_ENDPOINT}" \
      "a pod outside meridian and observability (a probe in ${NETWORK_OUTSIDER_NAMESPACE}) cannot push to the collector (${COLLECTOR_ENDPOINT}), which only the pods of meridian may reach, and a pod of meridian did reach it in this run (check 4's push)" \
      "a pod in ${NETWORK_OUTSIDER_NAMESPACE} reached the collector (${COLLECTOR_ENDPOINT}): its ingress admits more than the pods of meridian, or is missing (make up applies manifests/observability-networkpolicy.yaml), or the cluster does not enforce it" \
      "${NETWORK_OUTSIDER_NAMESPACE}" "${unproven}" ||
      true
  fi
  network_outsider_delete || true # the Pod stays named: the EXIT trap tries again
}

check_network_policy() {
  local found policy
  network_sweep_leftovers
  if ! found="$(kctl -n meridian get deployment claims-api -o name --ignore-not-found)"; then
    fail "network policy: could not look for deployment/claims-api (kubectl's error is above)"
    return
  fi
  if [[ -z "${found}" ]]; then
    skip "network policy: the Meridian services are not deployed (make deploy)"
    return
  fi
  if ! policy="$(kctl -n meridian get networkpolicy default-deny -o name --ignore-not-found)"; then
    fail "network policy: could not look for networkpolicy/default-deny (kubectl's error is above)"
    return
  fi
  if [[ -z "${policy}" ]]; then
    fail "network policy: networkpolicy/default-deny does not exist in meridian: the chart was installed with networkPolicy.enabled=false, or not at all (make deploy)"
    return
  fi
  # The control first: when the probe cannot reach what a policy allows, a
  # "blocked" below would mean nothing, so none is printed.
  network_expect reached deploy/claims-api "${NETWORK_RUNTIME}" \
    "the probe reaches what a policy allows: the Claims API to the Agent Runtime (${NETWORK_RUNTIME})" \
    "the Claims API cannot reach the Agent Runtime (${NETWORK_RUNTIME}), which its policy allows: the probe or a policy is broken, so a blocked path would prove nothing" ||
    return 0
  network_expect blocked deploy/claims-api "${NETWORK_GATEWAY}" \
    "the Claims API cannot reach the Model Gateway (${NETWORK_GATEWAY}), which no rule allows" \
    "the Claims API reached the Model Gateway (${NETWORK_GATEWAY}), which no rule allows: the cluster does not enforce NetworkPolicy, or a rule is too wide" ||
    true
  network_expect blocked deploy/claims-api "${NETWORK_API_SERVER}" \
    "the Claims API cannot reach the API server's Service (${NETWORK_API_SERVER}), which no rule of its policy lists" \
    "the Claims API reached the API server's Service (${NETWORK_API_SERVER}), which no rule of its policy lists: the cluster does not enforce egress rules, or one is too wide" ||
    true
  check_network_database
  check_network_collector
}

# ── 9. service identity ──────────────────────────────────────────────────────
# identity_status MODE: the probe's answer for MODE in ${identity_answer}: the
# HTTP status it printed, or "error: ..." with what it wrote on stderr when it
# failed (a name that does not resolve, a certificate that does not verify, a
# refused connection: a traceback is never read as a status).
identity_status() {
  local err_file
  err_file="$(mktemp)"
  if identity_answer="$(kctl -n meridian exec deploy/agent-runtime -- \
    python -c "${IDENTITY_PROBE}" "$1" "${IDENTITY_HOST}" "${IDENTITY_PORT}" "${IDENTITY_FOREIGN_TENANT}" 2>"${err_file}")"; then
    identity_answer="$(clean_lines "${identity_answer}")"
  else
    identity_answer="error: $(clean_lines "$(<"${err_file}")")"
  fi
  rm -f "${err_file}"
}

# expect_identity_status MODE STATUS WHAT: PASS when the probe's answer is STATUS.
expect_identity_status() {
  local mode=$1 expected=$2 what=$3
  identity_status "${mode}"
  if [[ "${identity_answer}" == "${expected}" ]]; then
    pass "service identity: ${what} -> ${expected}"
  else
    fail "service identity: ${what}: expected ${expected}, got ${identity_answer}"
  fi
}

# expect_foreign_ca: the fifth line. The probe's answer for a certificate of
# another CA is "refused" (the TLS alert for an unknown CA) or "reset" (the
# connection ended with no alert before any request was sent, which is how
# uvicorn, the services' server, ends it); both pass, in words that tell them
# apart. "closed-after-request" (the connection ended after the request went
# out), a status or an error is a FAIL.
expect_foreign_ca() {
  local what="POST /v1/chat with a certificate of the Agent Runtime's own name from another CA"
  identity_status foreign-ca
  case "${identity_answer}" in
    refused) pass "service identity: ${what} -> refused (the TLS alert for an unknown CA)" ;;
    reset) pass "service identity: ${what} -> reset (the connection ended with no TLS alert, before any request was sent: uvicorn ends an unknown CA's connection so, and a gateway that died in that second would too)" ;;
    closed-after-request) fail "service identity: ${what}: the gateway took the request of a certificate from another CA and closed without answering: it may have accepted the certificate" ;;
    *) fail "service identity: ${what}: expected refused or reset, got ${identity_answer}" ;;
  esac
}

# identity_mark_start: before the request that causes the audit row below: the
# database's primary pod and its clock, in ${identity_primary} and
# ${identity_mark} (seconds since the epoch, with the fraction). A row recorded
# at or after the mark counts for this run. When either cannot be had, ${identity_mark}
# stays empty and ${identity_mark_problem} says why: the probes still run, and
# check_gateway_refusal_row prints the FAIL. The mark goes into SQL later, so it
# is kept only when it is a number.
identity_mark_start() {
  local err_file answer detail
  identity_primary=""
  identity_mark=""
  identity_mark_problem=""
  err_file="$(mktemp)"
  if ! identity_primary="$(kctl -n meridian get pod \
    -l cnpg.io/cluster=platform-db,cnpg.io/instanceRole=primary \
    -o jsonpath='{.items[0].metadata.name}' 2>"${err_file}")" || [[ -z "${identity_primary}" ]]; then
    detail="$(clean_lines "$(<"${err_file}")")"
    identity_primary=""
    identity_mark_problem="no primary pod found for platform-db to read the audit row of the 403${detail:+ (kubectl said: ${detail})}"
  elif ! answer="$(kctl -n meridian exec "${identity_primary}" -c postgres -- \
    env "PGOPTIONS=${PSQL_OPTIONS}" psql -d meridian -tAc "${IDENTITY_CLOCK_SQL}" 2>"${err_file}")"; then
    detail="$(clean_lines "$(<"${err_file}")")"
    identity_mark_problem="could not read the database's clock in ${identity_primary} before the probe${detail:+ (kubectl said: ${detail})}"
  elif ! [[ "$(clean_lines "${answer}")" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    identity_mark_problem="the database's clock in ${identity_primary} was not a number of seconds"
  else
    identity_mark="$(clean_lines "${answer}")"
  fi
  rm -f "${err_file}"
}

# check_gateway_refusal_row: the audit row of the 403 above, read in the
# database's primary pod the way check_cost_series reads the ledger. The reason
# the identity rule writes for a caller that names a tenant it may not
# (NAME_REFUSAL_REASON), the gateway as the service that wrote it (`service`),
# the calling service in `reference` (refuse_name in the gateway) and the tenant
# the probe named; `recorded_at` is the database's own clock (a timestamptz its
# insert trigger sets), so the age is the database's, not this laptop's. The
# gateway writes in a worker thread and at most one such row per reason, tenant
# and minute, so a second run inside that minute causes no row of its own. The
# query therefore asks for the newest row since ${identity_mark} less one
# throttle window, and says whether it was recorded at or after the mark (PASS,
# in those words: another run's 403 written after the mark is such a row too) or
# before it (the gateway wrote this minute's row for an earlier run: SKIP, not
# PASS); no row at all is a FAIL. It never asks for a count that
# went up, and runs again for about ten seconds while it finds no row of this
# run's, because the gateway writes in a worker thread. Every value in the SQL
# is a constant of this script, or the mark, which identity_mark_start kept
# only because it is a number; none came from a pod unchecked.
check_gateway_refusal_row() {
  local err_file detail answer="" attempt what
  what="a refusal for ${IDENTITY_AUDIT_REASON} by ${IDENTITY_CALLER} (tenant ${IDENTITY_FOREIGN_TENANT})"
  if [[ -z "${identity_mark}" ]]; then
    fail "service identity: ${identity_mark_problem}"
    return
  fi
  err_file="$(mktemp)"
  for ((attempt = 1; attempt <= IDENTITY_AUDIT_ATTEMPTS; attempt++)); do
    if ! answer="$(kctl -n meridian exec "${identity_primary}" -c postgres -- \
      env "PGOPTIONS=${PSQL_OPTIONS}" psql -d meridian -tAc "SELECT floor(extract(epoch FROM now() - recorded_at))::bigint, extract(epoch FROM recorded_at) >= ${identity_mark} FROM audit.events WHERE service = '${IDENTITY_GATEWAY_SERVICE}' AND event = 'model.call' AND outcome = 'refused' AND reason = '${IDENTITY_AUDIT_REASON}' AND reference = '${IDENTITY_CALLER}' AND tenant = '${IDENTITY_FOREIGN_TENANT}' AND recorded_at > to_timestamp(${identity_mark}) - interval '${IDENTITY_AUDIT_THROTTLE} seconds' ORDER BY recorded_at DESC LIMIT 1" \
      2>"${err_file}")"; then
      detail="$(clean_lines "$(<"${err_file}")")"
      rm -f "${err_file}"
      fail "service identity: could not read audit.events in ${identity_primary}${detail:+ (kubectl said: ${detail})}"
      return
    fi
    answer="$(clean_lines "${answer}")"
    if [[ -n "${answer}" && "${answer}" != *"|f" ]]; then break; fi
    ((attempt == IDENTITY_AUDIT_ATTEMPTS)) || sleep "${IDENTITY_AUDIT_INTERVAL}"
  done
  rm -f "${err_file}"
  if [[ -z "${answer}" ]]; then
    fail "service identity: the gateway's audit log has no row for ${what} since this run started or in the ${IDENTITY_AUDIT_THROTTLE} s before it, after ${IDENTITY_AUDIT_ATTEMPTS} tries: the 403 was not recorded, or was not the identity rule's"
  elif [[ "${answer}" =~ ^([0-9]+)\|t$ ]]; then
    pass "service identity: the gateway's audit log has ${what}, ${BASH_REMATCH[1]} s old, recorded at or after this run's mark"
  elif [[ "${answer}" =~ ^([0-9]+)\|f$ ]]; then
    skip "service identity: the gateway wrote this minute's refusal row for an earlier run (the newest row for ${what} is ${BASH_REMATCH[1]} s old, and none came after this run started), so this run's 403 left none of its own; run again in a minute"
  else
    fail "service identity: the audit query's answer was not a number of seconds and a flag"
  fi
}

check_service_identity() {
  local found
  if ! found="$(deployed_services)"; then
    fail "service identity: could not look for the Meridian deployments (kubectl's error is above)"
    return
  fi
  if [[ -z "${found}" ]]; then
    skip "service identity: the Meridian services are not deployed (make deploy)"
    return
  fi
  expect_identity_status health 200 "GET /healthz on the Model Gateway with no certificate (the kubelet's probe sends none)"
  expect_identity_status anonymous 401 "POST /v1/chat on the Model Gateway with no certificate"
  identity_mark_start # the database's clock, before the request that causes the row
  expect_identity_status foreign-tenant 403 "POST /v1/chat with the Agent Runtime's certificate, naming a tenant it may not name"
  check_gateway_refusal_row
  expect_foreign_ca
}

# ── 10. certificate policy ───────────────────────────────────────────────────
# Read-only, and never skipped: `make up` makes every object it reads, with or
# without `make deploy`, so a missing one is a FAIL. Nothing else in `make up`
# or `make smoke` makes a certificate request on a cluster that has its
# certificates, and the issuer is Ready with the policies or the add-on gone, so
# these three lines are what catch a cert-manager or approver-policy update that
# leaves nothing to approve a request, or turns the built-in approver back on.
check_policies_ready() {
  local policy ready wrong=""
  for policy in "${POLICY_NAMES[@]}"; do
    ready="$(kctl get certificaterequestpolicy "${policy}" \
      -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" || ready=""
    [[ "${ready}" == True ]] || wrong+=" ${policy}"
  done
  if [[ -n "${wrong}" ]]; then
    fail "certificate policy: CertificateRequestPolicy${wrong} missing or not Ready: with cert-manager's own approver off, nothing would approve the chart's Certificates (make up)"
    return
  fi
  pass "certificate policy: the CertificateRequestPolicies ${POLICY_NAMES[*]} are Ready"
}

check_approver_addon() {
  local available
  available="$(kctl -n "${POLICY_NAMESPACE}" get deployment "${POLICY_ADDON}" \
    -o jsonpath='{.status.availableReplicas}' 2>/dev/null)" || available=""
  if [[ "${available}" =~ ^[0-9]+$ ]] && ((10#${available} > 0)); then
    pass "certificate policy: deployment/${POLICY_ADDON} in ${POLICY_NAMESPACE} has ${available} available replica(s)"
  else
    fail "certificate policy: deployment/${POLICY_ADDON} in ${POLICY_NAMESPACE} is missing or has no available replica: nothing would approve the chart's Certificates (make up)"
  fi
}

# cert-manager's built-in approver is off, read two ways that must agree: the
# ClusterRole it needs to approve exists only when the chart renders it with the
# approver on (kubectl's NotFound is the pass; any other error is not), and the
# controller's arguments hold the switch that turns the approver controller off,
# as a whole argument.
check_builtin_approver_off() {
  local err_file args problems=""
  err_file="$(mktemp)"
  if kctl get clusterrole "${POLICY_BUILTIN_ROLE}" -o name >/dev/null 2>"${err_file}"; then
    problems+="; the ClusterRole ${POLICY_BUILTIN_ROLE} exists"
  elif ! grep -q '(NotFound)' "${err_file}"; then
    problems+="; could not look for the ClusterRole ${POLICY_BUILTIN_ROLE} (kubectl said: $(clean_lines "$(<"${err_file}")"))"
  fi
  if args="$(kctl -n "${POLICY_NAMESPACE}" get deployment "${POLICY_CONTROLLER}" \
    -o jsonpath='{.spec.template.spec.containers[*].args[*]}' 2>"${err_file}")"; then
    [[ " ${args} " == *" ${POLICY_BUILTIN_OFF_ARG} "* ]] ||
      problems+="; the arguments of deployment/${POLICY_CONTROLLER} do not hold ${POLICY_BUILTIN_OFF_ARG}"
  else
    problems+="; could not read the arguments of deployment/${POLICY_CONTROLLER} (kubectl said: $(clean_lines "$(<"${err_file}")"))"
  fi
  rm -f "${err_file}"
  if [[ -n "${problems}" ]]; then
    fail "certificate policy: cert-manager's own approver may be on, so the issuer may be signing for every request again${problems}"
    return
  fi
  pass "certificate policy: cert-manager's own approver is off (no ClusterRole ${POLICY_BUILTIN_ROLE}; the controller runs with ${POLICY_BUILTIN_OFF_ARG})"
}

# The fourth line, and the one place where smoke changes the cluster on purpose:
# a request that the issuer must refuse. It is a CertificateRequest, not a
# Certificate, so that the key is made here, with openssl, and never leaves this
# machine (a Certificate has cert-manager make it, in a Secret in the cluster).
# The request is for the issuer meridian-services with a URI that issuer signs in
# `meridian`; it is made in REFUSED_NAMESPACE, which no policy but the denying
# one selects, so it must be Denied. Approved, or carrying a certificate, is a
# FAIL: the issuer signed a request it must refuse. The request is deleted
# right after it is read, by the EXIT trap when the run ends first, and, when a
# run was killed, at the start of the next one by name, from a list of the
# requests with its label that are older than REFUSED_LEFTOVER_AGE.

# refused_make_csr: a signing request in PEM on stdout. openssl makes the key
# and writes it to /dev/null: this script never wants the certificate, so the key
# is in no file, no variable and no output.
refused_make_csr() {
  openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -keyout /dev/null -subj / -addext "subjectAltName=URI:${REFUSED_URI}"
}

# refused_manifest NAME CSR: the CertificateRequest as JSON on stdout.
refused_manifest() {
  jq -n --arg name "$1" --arg csr "$2" --arg namespace "${REFUSED_NAMESPACE}" \
    --arg issuer "${REFUSED_ISSUER}" --arg duration "${REFUSED_DURATION}" \
    --arg label_key "${REFUSED_LABEL%%=*}" --arg label_value "${REFUSED_LABEL#*=}" '{
      apiVersion: "cert-manager.io/v1",
      kind: "CertificateRequest",
      metadata: {name: $name, namespace: $namespace, labels: {($label_key): $label_value}},
      spec: {
        request: ($csr + "\n" | @base64),
        duration: $duration,
        usages: ["digital signature", "client auth", "server auth"],
        issuerRef: {name: $issuer, kind: "ClusterIssuer", group: "cert-manager.io"}
      }
    }'
}

# refused_read: the request's verdict in ${refused_state} as
# "<Approved|Denied|empty>|<true|false: it carries a certificate>|<reason>|<message>",
# the message on one line. Returns 1 with ${refused_problem} when it cannot read it.
# The request, and a certificate in it, are read by jq and never printed.
refused_read() {
  local json
  if ! json="$(kctl -n "${REFUSED_NAMESPACE}" get certificaterequest "${refused_request}" \
    -o json 2>"${refused_err_file}")"; then
    refused_problem="could not read the request ${refused_request} in ${REFUSED_NAMESPACE} (kubectl said: $(clean_lines "$(<"${refused_err_file}")"))"
    return 1
  fi
  if ! refused_state="$(jq -r '
    [(.status.conditions // [])[] | select(.status == "True")] as $held
    | (([$held[] | select(.type == "Approved")] + [$held[] | select(.type == "Denied")]) | .[0] // {}) as $verdict
    | [$verdict.type // "", ((.status.certificate // "") != "" | tostring),
       ($verdict.reason // "" | gsub("[|\r\n]"; " ")), ($verdict.message // "" | gsub("[\r\n]+"; " "))]
    | join("|")' <<<"${json}" 2>/dev/null)"; then
    refused_problem="could not read the conditions of the request ${refused_request} in ${REFUSED_NAMESPACE}"
    return 1
  fi
}

# refused_wait: read the request until it holds a verdict, up to REFUSED_ATTEMPTS
# times. Sets ${refused_verdict} (Approved, Denied, or Issued for a request that
# carries a certificate whatever its conditions say), ${refused_reason} and
# ${refused_message} (cleaned and cut). Returns 1 when a read failed and 2 when
# the attempts ran out with no verdict.
refused_wait() {
  local attempt issued
  for ((attempt = 1; attempt <= REFUSED_ATTEMPTS; attempt++)); do
    refused_read || return 1
    IFS='|' read -r refused_verdict issued refused_reason refused_message <<<"${refused_state}"
    refused_reason="$(clean_lines "${refused_reason}")"
    refused_reason="${refused_reason:0:REFUSED_REASON_LENGTH}"
    refused_message_full="$(clean_lines "${refused_message}")"
    refused_message="${refused_message_full:0:REFUSED_MESSAGE_LENGTH}"
    [[ "${issued}" != true ]] || refused_verdict=Issued
    [[ -z "${refused_verdict}" ]] || return 0
    ((attempt == REFUSED_ATTEMPTS)) || sleep "${REFUSED_INTERVAL}"
  done
  return 2
}

# refused_names_policy NAME: succeeds when the approver's whole message
# (${refused_message_full}) names the policy NAME as one that evaluated the
# request. The form it matches is the one approver-policy v0.28.0 wrote on the
# cluster (2026-10-06): "No policy approved this request: [<policy>: [<errors>"
# with the next policy, if any, after a "]" or a ",". A policy's name is the
# word after "[", "]" or "," (and at most one space) and before ":", so that
# meridian-services-ca, the issuer's name after "name: ", a URI that ends in
# the name and a longer name do not count. It depends on that wording at the
# pinned version.
refused_names_policy() {
  local before='(\[|\]|,) ?'
  [[ "${refused_message_full}" =~ ${before}$1: ]]
}

# refused_denial_cause: why the Denied request was denied, from the approver's
# whole message (before it is cut for the line), one word on stdout: "namespace"
# when it names the denying policy and not the one that selects `meridian` (the
# claim of the line: the namespace refuses), "selected" when it names that one,
# and "unreadable" when it names neither as a policy.
refused_denial_cause() {
  if refused_names_policy "${REFUSED_SELECTING_POLICY}"; then
    echo selected
  elif refused_names_policy "${REFUSED_DENYING_POLICY}"; then
    echo namespace
  else
    echo unreadable
  fi
}

# refused_report_denied WHERE LEFT: the line of a Denied request, for the
# request WHERE names; LEFT is what is left of it when it could not be deleted.
refused_report_denied() {
  local where=$1 left=$2 verdict="was Denied (${refused_reason}: ${refused_message})"
  case "$(refused_denial_cause)" in
    selected)
      fail "certificate policy: the policy ${REFUSED_SELECTING_POLICY} selected a request from another namespace: ${where} ${verdict}, and the message names that policy; its namespace selector must not list ${REFUSED_NAMESPACE}${left}" ;;
    unreadable)
      fail "certificate policy: ${where} ${verdict}, but the approver's message is not in the form this check reads (the policy ${REFUSED_DENYING_POLICY}, named as a policy of the request's evaluation); approver-policy's wording may have changed${left}" ;;
    *)
      if [[ -n "${left}" ]]; then
        fail "certificate policy: ${where} ${verdict}${left}"
      else
        pass "certificate policy: a request for the issuer ${REFUSED_ISSUER} from the namespace ${REFUSED_NAMESPACE}, with a URI under the Meridian prefix, ${verdict}, and deleted"
      fi ;;
  esac
}

# refused_report NAME WAIT_STATUS DELETE_PROBLEM: the one line of the fourth
# check, for the request NAME. DELETE_PROBLEM is what kubectl said when the
# request could not be deleted, else empty: the line then says what is left.
refused_report() {
  local name=$1 waited=$2 delete_problem=$3 left="" where
  where="the request ${name} in ${REFUSED_NAMESPACE}"
  [[ -z "${delete_problem}" ]] ||
    left="; it could not be deleted (kubectl said: ${delete_problem}): kubectl -n ${REFUSED_NAMESPACE} delete certificaterequest ${name}"
  case "${waited}:${refused_verdict}" in
    0:Denied) refused_report_denied "${where}" "${left}" ;;
    0:*)
      fail "certificate policy: the issuer signed a request it must refuse: ${where} was ${refused_verdict} (${refused_reason}: ${refused_message})${left:-; smoke deleted it}; check cert-manager's own approver and the selectors of the policies (make up)" ;;
    1:*) fail "certificate policy: ${refused_problem}${left}" ;;
    *) fail "certificate policy: the approver did not answer: ${where} had neither condition Approved nor Denied after $((REFUSED_ATTEMPTS * REFUSED_INTERVAL)) s (is deployment/${POLICY_ADDON} running, and may the user running smoke use the policy meridian-deny-unlisted?)${left}" ;;
  esac
}

# refused_sweep_leftovers: at the start of a run, delete by name the requests
# with REFUSED_LABEL that are older than REFUSED_LEFTOVER_AGE seconds: what a
# killed run left (a request lives two seconds). A younger one is another run's
# that is under way (two sessions on one machine do happen), and is its own to
# delete. The age is this machine's clock against the request's
# creationTimestamp (the API server's), so a skewed clock only delays the
# sweep: the request stays, and a later run deletes it. A list that cannot be
# read is not an error: the check goes on, and the leftover waits.
refused_sweep_leftovers() {
  local json names leftover
  json="$(kctl -n "${REFUSED_NAMESPACE}" get certificaterequest -l "${REFUSED_LABEL}" \
    -o json 2>/dev/null)" || return 0
  names="$(jq -r --argjson age "${REFUSED_LEFTOVER_AGE}" '
    .items[] | select(now - (.metadata.creationTimestamp | fromdateiso8601) > $age)
    | .metadata.name' <<<"${json}" 2>/dev/null)" || return 0
  while IFS= read -r leftover; do
    [[ -n "${leftover}" ]] || continue
    kctl -n "${REFUSED_NAMESPACE}" delete certificaterequest "${leftover}" \
      --ignore-not-found --wait=false >/dev/null 2>&1 || true
  done <<<"${names}"
}

refused_check() {
  local csr manifest detail name waited=0 delete_problem=""
  refused_sweep_leftovers
  if ! csr="$(refused_make_csr 2>"${refused_err_file}")"; then
    fail "certificate policy: openssl could not make the request that the issuer must refuse: $(clean_lines "$(<"${refused_err_file}")")"
    return
  fi
  name="${REFUSED_NAME_PREFIX}$$-${RANDOM}"
  refused_request="${name}"
  if ! manifest="$(refused_manifest "${name}" "${csr}" 2>"${refused_err_file}")" ||
    ! kctl -n "${REFUSED_NAMESPACE}" create -f - <<<"${manifest}" >/dev/null 2>"${refused_err_file}"; then
    detail="$(clean_lines "$(<"${refused_err_file}")")"
    refused_delete_request || true # the create may have been half done
    fail "certificate policy: could not create the request that the issuer must refuse in ${REFUSED_NAMESPACE} (said: ${detail})"
    return
  fi
  refused_wait || waited=$?
  if ! refused_delete_request "${refused_err_file}"; then # the exit status decides
    delete_problem="$(clean_lines "$(<"${refused_err_file}")")"
    delete_problem="${delete_problem:-kubectl exited non-zero with no message}"
  fi
  refused_report "${name}" "${waited}" "${delete_problem}"
}

# The file kubectl's and openssl's messages go to while the request exists; the
# EXIT trap removes it too, so an interrupted run leaves no temporary file.
check_refused_request() {
  refused_err_file="$(mktemp)"
  refused_check
  rm -f "${refused_err_file}"
  refused_err_file=""
}

check_certificate_policy() {
  check_policies_ready
  check_approver_addon
  check_builtin_approver_off
  check_refused_request
}

# ── 11. alert rules and health dashboard ─────────────────────────────────────
# What the file holds, read with awk by the file's indentation (the group
# names at four spaces, the rules at eight; a test compares both with a YAML
# parser's reading): tree_groups prints the group names, tree_rules prints
# "<group><TAB><rule>" per alert and recording rule, both sorted bytewise.
tree_groups() {
  awk '/^    - name: / { print $3 }' "${ALERT_RULES_FILE}" | LC_ALL=C sort
}

tree_rules() {
  awk '/^    - name: / { group = $3 } /^        - (alert|record): / { print group "\t" $3 }' \
    "${ALERT_RULES_FILE}" | LC_ALL=C sort
}

# cluster_groups BODY and cluster_rules BODY: the same two lists for the
# meridian.* groups in Prometheus' /api/v1/rules answer BODY.
cluster_groups() {
  jq -r --arg prefix "${ALERT_GROUP_PREFIX}" \
    '.data.groups[].name | select(startswith($prefix))' <<<"$1" | LC_ALL=C sort
}

cluster_rules() {
  jq -r --arg prefix "${ALERT_GROUP_PREFIX}" \
    '.data.groups[] | select(.name | startswith($prefix)) | .name as $group
      | .rules[] | $group + "\t" + .name' <<<"$1" | LC_ALL=C sort
}

# name_list: the lines of stdin as one line of names ("group/rule" for a tab),
# separated by ", ", without any byte that is not printable ASCII.
name_list() {
  tr '\t' '/' | LC_ALL=C tr -cd '[:print:]\n' | paste -sd ',' - | sed 's/,/, /g'
}

# check_rules_object: 0 when the PrometheusRule is there. Otherwise one FAIL line
# and 1: for kubectl's NotFound (`make up` applies the object, so on any cluster
# this repository makes it is missing for a reason, and the line says what to
# run) and for any other error.
check_rules_object() {
  local err_file
  err_file="$(mktemp)"
  if kctl -n "${ALERT_RULES_NAMESPACE}" get prometheusrule "${ALERT_RULES_OBJECT}" \
    -o name >/dev/null 2>"${err_file}"; then
    rm -f "${err_file}"
    return 0
  fi
  if grep -q '(NotFound)' "${err_file}"; then
    fail "alert rules: the PrometheusRule ${ALERT_RULES_OBJECT} is not in ${ALERT_RULES_NAMESPACE}, and make up applies it from infra/kind/alerts/meridian.yaml: run make up"
  else
    fail "alert rules: could not look for the PrometheusRule ${ALERT_RULES_OBJECT} in ${ALERT_RULES_NAMESPACE} (kubectl said: $(clean_lines "$(<"${err_file}")"))"
  fi
  rm -f "${err_file}"
  return 1
}

# fetch_rules: Prometheus' /api/v1/rules, through Grafana's datasource proxy, left
# in ${rules_body}. It polls (up to POLL_TIMEOUT) until every group of the file
# is loaded and none of the loaded rules is still unevaluated (a rule's health is
# "unknown" until its group's first evaluation), then takes that answer. When that
# does not happen the answer of one more request is judged as it is. Returns 1
# after one FAIL line when Prometheus does not answer with status success. An
# answer of status success that has no list of groups in the form the later
# lines read (data.groups: objects with a string name and a list of rules, which
# are objects) leaves ${rules_body} empty and returns 0: each line that needed
# the groups then prints its own FAIL (rules_missing_groups), and the dashboard
# line after them still runs.
fetch_rules() {
  local wanted status
  wanted="$(tree_groups | jq -R . | jq -sc .)" || wanted="[]"
  if [[ "${wanted}" == "[]" ]]; then
    fail "alert rules: found no group in ${ALERT_RULES_FILE}"
    return 1
  fi
  if poll "select(.status == \"success\")
    | [.data.groups[] | select(.name | startswith(\"${ALERT_GROUP_PREFIX}\"))] as \$loaded
    | select((${wanted} - [\$loaded[].name] | length) == 0
      and ([\$loaded[].rules[].health] | all(. != \"unknown\")))
    | tojson" "${grafana_url}${ALERT_RULES_PATH}"; then
    rules_body="${poll_result}"
    return 0
  fi
  rules_body="$(gcurl "${grafana_url}${ALERT_RULES_PATH}" 2>/dev/null || true)"
  status="$(jq -r '.status // empty' <<<"${rules_body}" 2>/dev/null || true)"
  if [[ "${status}" != success ]]; then
    fail "alert rules: Prometheus did not answer ${ALERT_RULES_PATH} with status success after ${POLL_TIMEOUT}s (last answer: ${poll_error})"
    return 1
  fi
  if ! jq -e '(.data.groups | type == "array")
    and all(.data.groups[]; type == "object" and (.name | type == "string")
      and (.rules | type == "array") and all(.rules[]; type == "object"))' \
    <<<"${rules_body}" >/dev/null 2>&1; then
    rules_body=""
  fi
}

# rules_missing_groups BODY WHAT: 0 after one FAIL line for WHAT when BODY is
# empty (fetch_rules found no usable data.groups in the answer); 1, silently,
# when there is a body to read.
rules_missing_groups() {
  [[ -z "$1" ]] || return 1
  fail "alert rules: ${2} cannot be checked: Prometheus' answer to ${ALERT_RULES_PATH} has no data.groups list of groups with names and rules"
}

# check_rules_loaded BODY: the file's groups are loaded and every rule of the
# loaded meridian.* groups has health ok. A rule that is not is named with its
# health and its lastError, newlines and tabs as spaces and cut to
# ALERT_ERROR_LENGTH characters.
check_rules_loaded() {
  local body=$1 missing unhealthy problems="" total
  if rules_missing_groups "${body}" "whether the groups are loaded and the rules healthy"; then
    return
  fi
  missing="$(LC_ALL=C comm -23 <(tree_groups) <(cluster_groups "${body}") | name_list)"
  unhealthy="$(jq -r --arg prefix "${ALERT_GROUP_PREFIX}" --argjson cut "${ALERT_ERROR_LENGTH}" \
    '.data.groups[] | select(.name | startswith($prefix)) | .rules[] | select(.health != "ok")
      | .name + " (health " + (.health // "unknown")
        + (if (.lastError // "") != "" then ": " + (.lastError | gsub("[\\r\\n\\t]"; " ") | .[0:$cut]) else "" end)
        + ")"' <<<"${body}" | name_list)"
  [[ -z "${missing}" ]] || problems="group(s) ${missing} of infra/kind/alerts/meridian.yaml not loaded in Prometheus (run make up)"
  [[ -z "${unhealthy}" ]] || problems+="${problems:+; }rules not healthy: ${unhealthy}"
  if [[ -n "${problems}" ]]; then
    fail "alert rules: ${problems}"
    return
  fi
  total="$(cluster_rules "${body}" | wc -l)"
  pass "alert rules: the $(tree_groups | wc -l | tr -d ' ') groups of infra/kind/alerts/meridian.yaml are loaded in Prometheus and all ${total//[[:space:]]/} rules in them are healthy"
}

# check_rules_names BODY: the loaded meridian.* groups and their rules are the
# file's, by name, in both directions. A cluster that runs another rule file
# says which groups and rules differ.
check_rules_names() {
  local body=$1 not_loaded not_in_file differences="" total
  if rules_missing_groups "${body}" "whether the loaded rules are the file's"; then
    return
  fi
  not_loaded="$({
    LC_ALL=C comm -23 <(tree_groups) <(cluster_groups "${body}")
    LC_ALL=C comm -23 <(tree_rules) <(cluster_rules "${body}")
  } | name_list)"
  not_in_file="$({
    LC_ALL=C comm -13 <(tree_groups) <(cluster_groups "${body}")
    LC_ALL=C comm -13 <(tree_rules) <(cluster_rules "${body}")
  } | name_list)"
  [[ -z "${not_loaded}" ]] || differences="not loaded: ${not_loaded}"
  [[ -z "${not_in_file}" ]] || differences+="${differences:+; }not in the file: ${not_in_file}"
  if [[ -n "${differences}" ]]; then
    fail "alert rules: the rules Prometheus runs are not infra/kind/alerts/meridian.yaml's (a cluster that runs an older file: run make up): ${differences}"
    return
  fi
  total="$(tree_rules | wc -l)"
  pass "alert rules: the loaded rules are the file's: the same $(tree_groups | wc -l | tr -d ' ') groups and ${total//[[:space:]]/} rule names"
}

# alerts_in_state STATE BODY: the names of the rules of the meridian.* groups
# that have an alert in STATE (firing or pending), sorted, one per line.
alerts_in_state() {
  jq -r --arg prefix "${ALERT_GROUP_PREFIX}" --arg state "$1" \
    '.data.groups[] | select(.name | startswith($prefix)) | .rules[]
      | select(any(.alerts[]?; .state == $state)) | .name' <<<"$2" | LC_ALL=C sort -u
}

# check_rules_firing BODY: no alert of the meridian.* groups is firing. A pending
# alert has not yet lasted its `for`: it is not a failure, and the line names it.
# With no meridian.* group loaded there is nothing to be firing, so the line
# would pass for nothing: it is a FAIL that says it cannot tell.
check_rules_firing() {
  local body=$1 firing pending count
  if rules_missing_groups "${body}" "whether a Meridian alert is firing"; then
    return
  fi
  if [[ -z "$(cluster_groups "${body}")" ]]; then
    fail "alert rules: cannot tell whether a Meridian alert is firing: no group of ${ALERT_GROUP_PREFIX}* is loaded in Prometheus (run make up)"
    return
  fi
  firing="$(alerts_in_state firing "${body}")"
  pending="$(alerts_in_state pending "${body}")"
  if [[ -n "${firing}" ]]; then
    fail "alert rules: Meridian alert(s) firing: $(name_list <<<"${firing}") (the health dashboard's table and Prometheus' Alerts page show them)"
    return
  fi
  if [[ -z "${pending}" ]]; then
    pass "alert rules: no Meridian alert is firing (none pending)"
    return
  fi
  count="$(wc -l <<<"${pending}")"
  pass "alert rules: no Meridian alert is firing (${count//[[:space:]]/} pending, not a failure: $(name_list <<<"${pending}"))"
}

# The rules, then the health dashboard as check 5 reads the cost dashboard.
check_alert_rules() {
  open_grafana || return 0 # otherwise it printed the one FAIL line
  if check_rules_object && fetch_rules; then
    check_rules_loaded "${rules_body}"
    check_rules_names "${rules_body}"
    check_rules_firing "${rules_body}"
  fi
  check_dashboard "${HEALTH_DASHBOARD_UID}" "${HEALTH_DASHBOARD_FILE}"
}

# The EXIT trap deletes what a run made. Bash does not reliably run it when a
# signal ends the script (SIGHUP: an SSH session that drops, a closed terminal),
# so each signal has a trap that exits with 128 plus its number, which runs the
# EXIT trap; the `pytest-db` recipe of the Makefile does the same.
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
check_edge
check_database
check_tools
check_telemetry
check_cost_panel
check_adjuster_pages
check_sweep
check_network_policy
check_service_identity
check_certificate_policy
check_alert_rules

if ((failures > 0)); then
  printf '\n%s check(s) FAILED\n' "${failures}"
  exit 1
fi
if ((skips > 0)); then
  printf '\nAll checks that ran passed; %s skipped.\n' "${skips}"
else
  printf '\nAll checks passed.\n'
fi
printf 'See it yourself: make grafana (user admin; password: make grafana-password), then Explore:\n'
printf '  Tempo       TraceQL    { resource.service.name = "%s" }\n' "${service}"
printf '  Loki        LogQL      {service_name="%s"}\n' "${service}"
printf '  Prometheus  PromQL     {__name__=~"gen.*", job="%s"}\n' "${service}"
printf '  Grafana     Dashboards > Meridian: Model Gateway tokens and cost\n'
