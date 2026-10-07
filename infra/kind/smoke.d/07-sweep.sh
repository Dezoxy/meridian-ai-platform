# shellcheck shell=bash
#   7. sweep:     two lines, read-only (the second is below). The first: the
#                 CronJob meridian-sweep exists, and the
#                 last of its Jobs that the schedule made to finish (not one made
#                 by hand: see below) succeeded; the line prints when it
#                 finished. It fails
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
#                 The Jobs are listed by the sweep's label
#                 (app.kubernetes.io/name=meridian-sweep, which the CronJob's
#                 jobTemplate gives every Job it makes, scheduled or by hand),
#                 not the namespace's: seen on kind on 2026-10-07 (S073, K8),
#                 smoke's own four Jobs a run, kept 15 minutes, made the list
#                 227,658 bytes and jq refused it as one argument (131,072
#                 bytes: "Argument list too long"). The verdict still filters
#                 by owner; a Job the CronJob owns without the label is not
#                 listed (the chart makes none). No cluster or Prometheus
#                 answer is a jq argument anywhere in this script: they go in
#                 on standard input or as --slurpfile of a process substitution
#                 (a test reads the script's text for it).
#                 A Job made by hand (`kubectl create job --from=cronjob/...`)
#                 has the CronJob as its owner too, so by the owner alone a
#                 recent one would read as the schedule's success (S073, K4).
#                 It is told apart by its annotations, seen on kind: it carries
#                 cronjob.kubernetes.io/instantiate=manual, and a scheduled Job
#                 carries batch.kubernetes.io/cronjob-scheduled-timestamp. The
#                 verdict leaves out a Job only when it has the first and not
#                 the second (the presence of the scheduled timestamp is the
#                 positive fact, and wins); a Job with neither is an older
#                 cluster's and counts as before, and the line says that the
#                 annotation was absent. When the newest finished Job of all is
#                 a by-hand one, the line says so, by name and time, and that
#                 a by-hand run is not a run of the schedule: a by-hand success
#                 beside a schedule that stopped is the stopped verdict, and a
#                 by-hand failure beside a healthy schedule is a PASS. Seen on
#                 kind on 2026-10-07: with ONE success kept (the chart now
#                 keeps three), a by-hand success evicted the schedule's own,
#                 and the verdict, resting on the failed scheduled Job of the
#                 evening before, failed a healthy schedule. So when the newest
#                 finished Job the schedule made finished before the CronJob's
#                 lastScheduleTime and a by-hand Job finished after it, that
#                 old Job is not judged and lastScheduleTime alone decides:
#                 older than the bound, the stopped FAIL; within it, a SKIP
#                 that says a Job of the schedule is running, or that the
#                 schedule fired at that time, its Job is no longer in the
#                 history and its outcome was not read (run smoke again after
#                 the next scheduled run; the findings line then skips too, so
#                 a by-hand run turns two PASS into two SKIP until the next
#                 run). What it does not see: a Job made by hand with the
#                 annotation removed, or with the scheduled one added by hand
#                 (a person who edits a Job's annotations can pass for the
#                 schedule), and the alert MeridianSweepStale, which reads the
#                 CronJob's last successful time, which a by-hand success moves
#                 too (seen on kind on 2026-10-07).
#                 The second line (S064, C3) asks Prometheus, through the same
#                 Grafana forward as check 5, whether the pass that line passed
#                 sent its findings: the gauge meridian_sweep_last_pass for job
#                 claims-sweep, each of the six findings (documents-overdue,
#                 triage-not-started, triage-abandoned, runs-ended,
#                 threads-cleaned, failures) with a sample in the last 15
#                 minutes, in one query that takes the newest value of each
#                 over every instance, waited for with `poll` as the cost series
#                 are. PASS names the six values. FAIL tells three causes apart:
#                 Prometheus did not answer with status success, it has no
#                 finding at all, or it has some and the line names the ones
#                 missing (only the six words the script holds are printed,
#                 never a label of the answer: any pod of meridian can push a
#                 series under the sweep's name). SKIP, one line, when the first
#                 line was not a PASS (no pass has finished: so also after `make
#                 up` alone), when the Job that line passed was made without the
#                 collector's address (the first smoke after a deploy that added
#                 it reads a Job made before: wait for a pass), and when Grafana
#                 could not be reached. What it does not prove: that the values
#                 are the pass's own (a pod of meridian can push the same
#                 series) or that the series are one instance's (the query
#                 takes the newest of each).

readonly SWEEP_CRONJOB=meridian-sweep
# The label of the sweep's Jobs: the CronJob's jobTemplate carries it (the chart's
# meridian.labels), so a Job the schedule made and one made by hand from the
# CronJob both have it. Check 7 lists the Jobs by it and not the namespace's,
# which smoke's own Jobs, the migrations and the ingestion fill (S073, K8: a
# list of 227,658 bytes was more than one argument of jq may hold). A Job the
# CronJob owns without the label (none the chart makes) is not listed; the
# verdict still filters by owner.
readonly SWEEP_JOBS_SELECTOR=app.kubernetes.io/name=meridian-sweep
# The CronJob's schedule is every five minutes; a run is overdue after three
# periods (900 s). The period is read from the CronJob's own schedule when it
# is "*/N * * * *"; SWEEP_PERIOD_SECONDS is what is used for any other form.
readonly SWEEP_PERIOD_SECONDS=300
readonly SWEEP_STALE_PERIODS=3
# The clock the sweep check trusts: the database's, as whole seconds.
readonly SWEEP_CLOCK_SQL='SELECT floor(extract(epoch FROM now()))::bigint'
# The sweep's findings line (S064): the gauge the pass sends before it exits, its
# `job` (the sweep's service name), the six findings under `meridian_finding` in
# the order of the pass's summary line, the variable that tells a Job was given
# the collector's address and the window the line looks back over (the rule
# MeridianSweepNotReporting's 15 minutes: three passes).
readonly SWEEP_SERIES=meridian_sweep_last_pass
readonly SWEEP_JOB_LABEL=claims-sweep
readonly SWEEP_FINDINGS=(documents-overdue triage-not-started triage-abandoned runs-ended threads-cleaned failures)
readonly SWEEP_ENDPOINT_ENV=OTEL_EXPORTER_OTLP_ENDPOINT
readonly SWEEP_SERIES_WINDOW=15m
sweep_job_finished="" # set by report_sweep: the Job whose success check 7's first line passed

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

# sweep_verdict CRONJOB_JSON JOBS_JSON NOW PERIOD: two lines. The first is the
# verdict, fields separated by "|"; the second is the note (see below). NOW is
# the database's clock in epoch seconds, PERIOD the schedule's in seconds; a run
# is overdue after SWEEP_STALE_PERIODS of them.
#   succeeded|JOB|FINISHED_AT          the newest finished Job the schedule made
#   failed|JOB|FINISHED_AT|REASON      and how it ended. A Job made by hand with
#                                      `kubectl create job --from=cronjob/...`
#                                      has the same owner, so it is told apart by
#                                      its annotations: it is left out only when
#                                      it carries cronjob.kubernetes.io/
#                                      instantiate=manual and not the scheduled
#                                      timestamp batch.kubernetes.io/cronjob-
#                                      scheduled-timestamp (the positive fact,
#                                      which wins); a Job with neither is an
#                                      older cluster's and counts as before
#   stale|SCHEDULED_AT|FINISHED_AT     last scheduled more than three periods
#                                      after that Job finished, nothing running:
#                                      the schedule makes no finished runs
#   stopped|JOB|FINISHED_AT|SECONDS    the newest finished Job succeeded more
#                                      than three periods before NOW, nothing
#                                      running: the schedule stopped
#   stopped||SCHEDULED_AT|SECONDS      the same with no Job of the CronJob left
#                                      (the sweep keeps three successes and the
#                                      cluster removes a Job a day after it
#                                      finished): last scheduled more than
#                                      three periods before NOW, nothing
#                                      running
#   The next three are for a history that no longer holds the schedule's
#   newest run: the newest finished Job the schedule made finished BEFORE the
#   CronJob's lastScheduleTime and a Job made by hand finished after it (seen
#   on kind: with one success kept, a by-hand success evicted the schedule's
#   own, and the verdict then rested on an old failed Job). That Job is not
#   judged; lastScheduleTime against the bound decides:
#   stoppedhand|SCHEDULED_AT|SECONDS|JOB  older than three periods, nothing
#                                      running: the schedule stopped
#   busy|SCHEDULED_AT                  within the bound, a Job is running
#   unread|SCHEDULED_AT|JOB            within the bound, nothing running: the
#                                      outcome of the run at SCHEDULED_AT is not
#                                      in the history, so it was not read (JOB
#                                      is the older Job the verdict would have
#                                      rested on)
#   young|SECONDS|JOB|FINISHED_AT      the same, but the CronJob was created
#                                      less than one period before NOW: that Job
#                                      is an earlier CronJob's, not overdue
#   never|SECONDS                      never scheduled, and the CronJob was
#                                      created more than three periods before NOW
#   unscheduled|SECONDS|CREATED_AT     never scheduled, not yet overdue
#   running                            no Job has finished; one is running
#   none|SCHEDULED_AT                  no Job has finished, none is running
# The note is "note:" and then words for report_sweep to end the line with: that
# the newest finished Job of all, by name and time, was made by hand and is not
# judged, and that the Job the verdict rests on carries neither annotation. The
# prefix keeps it from being empty, so that it survives command substitution.
sweep_verdict() {
  # The CronJob and the Jobs are the cluster's answers and can be any size, so
  # they go in as files (process substitutions: nothing on disk), not as
  # arguments, of which one may be 131,072 bytes.
  jq -nr --arg cronjob "${SWEEP_CRONJOB}" \
    --slurpfile cj <(printf '%s' "$1") --slurpfile jobs <(printf '%s' "$2") \
    --argjson now "$3" --argjson period "$4" \
    --argjson tolerance "$(($4 * SWEEP_STALE_PERIODS))" '
    def epoch: sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601;
    $cj[0] as $cj | $jobs[0] as $jobs
    | ($cj.status.lastScheduleTime // null) as $scheduled
    | (($cj.status.active // []) | length) as $active
    | [$jobs.items[]
        | select(any(.metadata.ownerReferences[]?; .kind == "CronJob" and .name == $cronjob))
        | . as $job
        | ([$job.status.conditions[]? | select(.status == "True" and (.type == "Complete" or .type == "Failed"))] | first // empty) as $done
        | ($job.metadata.annotations // {}) as $notes
        | {name: $job.metadata.name, type: $done.type, reason: ($done.reason // ""),
           mark: (if $notes["batch.kubernetes.io/cronjob-scheduled-timestamp"] != null then "scheduled"
                  elif $notes["cronjob.kubernetes.io/instantiate"] == "manual" then "manual"
                  else "unmarked" end),
           at: ((if $done.type == "Complete" then ($job.status.completionTime // $done.lastTransitionTime) else $done.lastTransitionTime end) // "")}]
    | . as $finished
    | (sort_by([.at, .name]) | last) as $latest
    | ([$finished[] | select(.mark != "manual")] | sort_by([.at, .name]) | last) as $newest
    | "note:\(if $latest.mark == "manual" then " [the newest finished Job of all, \($latest.name), finished at \($latest.at) and was made by hand (create job --from=cronjob/\($cronjob)): a by-hand run is not a run of the schedule, so this line does not judge it]" else "" end)\(if $newest.mark == "unmarked" then " [the Job this rests on carries neither batch.kubernetes.io/cronjob-scheduled-timestamp nor cronjob.kubernetes.io/instantiate: the annotation was absent, as on an older cluster, so it is counted as a scheduled Job]" else "" end)" as $note
    | ($now - ($cj.metadata.creationTimestamp | epoch)) as $age
    | ($scheduled != null and $newest != null and ($newest.at | epoch) < ($scheduled | epoch)
        and any($finished[]; .mark == "manual" and .at != "" and (.at | epoch) > ($scheduled | epoch))) as $evicted
    | (if $scheduled == null and $age > $tolerance then "never|\($age)"
      elif $newest == null then
        if $active > 0 then "running"
        elif $scheduled == null then "unscheduled|\($age)|\($cj.metadata.creationTimestamp)"
        elif ($now - ($scheduled | epoch)) > $tolerance then "stopped||\($scheduled)|\($now - ($scheduled | epoch))"
        else "none|\($scheduled)" end
      elif $evicted then
        if $active > 0 then "busy|\($scheduled)"
        elif ($now - ($scheduled | epoch)) > $tolerance then "stoppedhand|\($scheduled)|\($now - ($scheduled | epoch))|\($newest.name)"
        else "unread|\($scheduled)|\($newest.name)" end
      elif $scheduled != null and $active == 0 and (($scheduled | epoch) - ($newest.at | epoch)) > $tolerance then
        "stale|\($scheduled)|\($newest.at)"
      elif $newest.type == "Complete" and $active == 0 and ($now - ($newest.at | epoch)) > $tolerance then
        if $age < $period then "young|\($age)|\($newest.name)|\($newest.at)"
        else "stopped|\($newest.name)|\($newest.at)|\($now - ($newest.at | epoch))" end
      elif $newest.type == "Complete" then "succeeded|\($newest.name)|\($newest.at)"
      else "failed|\($newest.name)|\($newest.at)|\($newest.reason)" end) + "\n" + $note
  '
}

# report_sweep_unjudged KIND FIRST SECOND THIRD PERIOD NOTE: the line for the three
# verdicts of a history that no longer holds the schedule's newest run
# (stoppedhand, busy, unread: see sweep_verdict), from report_sweep's fields.
report_sweep_unjudged() {
  local bound=$(($5 * SWEEP_STALE_PERIODS))
  case "$1" in
    stoppedhand)
      fail "sweep: the schedule stopped: cronjob/${SWEEP_CRONJOB} was last scheduled at ${2}, ${3} s before the database's clock now, more than the ${bound} s (three periods of ${5} s) allowed, and no Job is running; the newest finished Job it made, ${4}, finished before that, and a Job made by hand after that time is not a run of the schedule${6}"
      ;;
    busy)
      skip "sweep: cronjob/${SWEEP_CRONJOB} was last scheduled at ${2} and a Job of it is running; the newest finished Job it made is older than that, so the outcome of this run is not read yet${6}"
      ;;
    unread)
      skip "sweep: cronjob/${SWEEP_CRONJOB} fired at ${2}, within the ${bound} s it allows, and its Job is no longer in the history (a by-hand Job took its place, or the history's limit did): the newest finished Job it made, ${3}, is older than that, so the outcome of that run was not read; run smoke again after the next scheduled run${6}"
      ;;
  esac
}

# report_sweep VERDICT PERIOD [NOTE]: the line for a verdict of sweep_verdict,
# whose fields were cleaned of anything that is not printable ASCII, and the
# verdict's second line, the note, which ends the line (without its "note:"
# prefix; none is also fine).
report_sweep() {
  local kind first second third bound=$(($2 * SWEEP_STALE_PERIODS)) note
  IFS='|' read -r kind first second third <<<"$1"
  first="$(clean_lines "${first}")"
  second="$(clean_lines "${second}")"
  third="$(clean_lines "${third}")"
  note="$(clean_lines "${3:-}")"
  note="${note#note:}"
  case "${kind}" in
    succeeded)
      sweep_job_finished="${first}"
      pass "sweep: cronjob/${SWEEP_CRONJOB} is not suspended and its last finished Job, ${first}, succeeded at ${second}${note}"
      ;;
    failed)
      fail "sweep: the last finished Job of cronjob/${SWEEP_CRONJOB}, ${first}, failed at ${second} (${third:-no reason given}); kubectl -n meridian describe job/${first} shows why, and kubectl -n meridian logs job/${first} what its pod printed, if a pod started${note}"
      ;;
    stale)
      fail "sweep: the schedule is not producing finished runs: cronjob/${SWEEP_CRONJOB} was last scheduled at ${first}, more than ${bound} s after its newest finished Job finished at ${second}, and no Job is running${note}"
      ;;
    stopped)
      if [[ -z "${first}" ]]; then
        fail "sweep: the schedule stopped: cronjob/${SWEEP_CRONJOB} was last scheduled at ${second}, ${third} s before the database's clock now, more than the ${bound} s (three periods of ${2} s) allowed; no Job of cronjob/${SWEEP_CRONJOB} is left (finished Jobs are removed a day after they finish) and none is running${note}"
      else
        fail "sweep: the schedule stopped: the newest finished Job of cronjob/${SWEEP_CRONJOB}, ${first}, succeeded at ${second}, ${third} s before the database's clock now, more than the ${bound} s (three periods of ${2} s) allowed, and no Job is running${note}"
      fi
      ;;
    stoppedhand | busy | unread)
      report_sweep_unjudged "${kind}" "${first}" "${second}" "${third}" "$2" "${note}"
      ;;
    young)
      skip "sweep: cronjob/${SWEEP_CRONJOB} was deployed ${first} s ago by the database's clock, less than one period (${2} s); its newest finished Job, ${second}, finished at ${third}, which is an earlier CronJob's, so it is not judged yet${note}"
      ;;
    never)
      fail "sweep: cronjob/${SWEEP_CRONJOB} has never been scheduled, although it was created ${first} s ago by the database's clock (more than three periods, ${bound} s): the schedule is not producing runs${note}"
      ;;
    unscheduled)
      skip "sweep: cronjob/${SWEEP_CRONJOB} has not been scheduled yet (created ${second}, ${first} s ago by the database's clock), within the ${bound} s it allows${note}"
      ;;
    running)
      skip "sweep: no Job of cronjob/${SWEEP_CRONJOB} has finished yet; the first one is running${note}"
      ;;
    none)
      skip "sweep: no Job of cronjob/${SWEEP_CRONJOB} has finished yet (last scheduled at ${first})${note}"
      ;;
    *)
      fail "sweep: unexpected verdict from the timestamps"
      ;;
  esac
}

# Same skip rule as the tool check: only when no Meridian Deployment exists. Only
# reads (kubectl get, and one SELECT of the database's clock); the Jobs of the
# whole namespace are listed and filtered by owner. The first of the check's two
# lines (the second is check_sweep_findings'): it leaves the name of the Job
# whose success it passed in ${sweep_job_finished}, and nothing for any other
# line.
check_sweep_job() {
  local found cronjob jobs verdict note now period
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
  if ! jobs="$(kctl -n meridian get job -l "${SWEEP_JOBS_SELECTOR}" -o json)"; then
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
  note="${verdict#*$'\n'}"
  verdict="${verdict%%$'\n'*}"
  report_sweep "${verdict}" "${period}" "${note}"
}

# sweep_findings_query: the instant query of check 7's second line. `max by`
# takes the last value of each finding whatever its `instance`: a pass sent
# before the CronJob set one instance ID is a series of its own, which
# Prometheus keeps five minutes.
sweep_findings_query() {
  printf 'max by (meridian_finding) (last_over_time(%s{job="%s"}[%s]))' \
    "${SWEEP_SERIES}" "${SWEEP_JOB_LABEL}" "${SWEEP_SERIES_WINDOW}"
}

# sweep_findings_report ANSWER JOB: the FAIL line, once the poll found no answer
# with all six findings; ANSWER is one more look at the query, for the message.
# It tells three causes apart: Prometheus did not answer, it has no finding at
# all, or it has some and not all. Only the six words the script holds are
# printed, never a label from the answer: any pod of meridian can push a series
# under the sweep's name (T-68).
sweep_findings_report() {
  local answer=$1 job=$2 status word found=0 missing=""
  status="$(jq -r '.status // empty' <<<"${answer}" 2>/dev/null || true)"
  if [[ "${status}" != success ]]; then
    fail "sweep findings: Prometheus did not answer the query with status success after ${POLL_TIMEOUT}s (last answer: ${poll_error:-none})"
    return
  fi
  for word in "${SWEEP_FINDINGS[@]}"; do
    if jq -e --arg word "${word}" 'any(.data.result[]?; .metric.meridian_finding == $word)' \
      <<<"${answer}" >/dev/null 2>&1; then
      found=$((found + 1))
    else
      missing="${missing:+${missing}, }${word}"
    fi
  done
  if ((found == 0)); then
    fail "sweep findings: Prometheus has no ${SWEEP_SERIES} series of job ${SWEEP_JOB_LABEL} from the last ${SWEEP_SERIES_WINDOW} after ${POLL_TIMEOUT}s, though job/${job} succeeded with ${SWEEP_ENDPOINT_ENV} set (the pass sends them before it exits; the runbook telemetry-missing says where to look)"
    return
  fi
  fail "sweep findings: Prometheus has ${found} of ${#SWEEP_FINDINGS[@]} findings of ${SWEEP_SERIES} from the last ${SWEEP_SERIES_WINDOW} after ${POLL_TIMEOUT}s, though job/${job} succeeded; missing: ${missing}"
}

# check_sweep_findings: the second line of check 7. The pass sends its six
# findings (meridian_sweep_last_pass) to the collector before it exits, and this
# asks Prometheus, through Grafana's datasource proxy as check 5 does, whether
# each of the six has a sample in the last 15 minutes: one query, waited for with
# `poll` as the cost series are. It looks for what the Job whose success the line
# above passed should have sent, so the Job's own spec says whether it was given
# the collector's address (the first smoke after a deploy that added it reads a
# Job made before: a SKIP, not a FAIL). A SKIP in every case where no pass has
# finished (the line above was a SKIP or a FAIL), the Job has no address or
# Grafana's forward is not open. Only reads.
check_sweep_findings() {
  local job=${sweep_job_finished} spec addressed query query_url final
  if [[ -z "${job}" ]]; then
    skip "sweep findings: no pass of the sweep has finished (see the line above), so there is nothing to look for"
    return
  fi
  if ! spec="$(kctl -n meridian get job "${job}" -o json)"; then
    fail "sweep findings: could not read job/${job} (kubectl's error is above)"
    return
  fi
  addressed="$(jq -r --arg name "${SWEEP_ENDPOINT_ENV}" \
    '[.spec.template.spec.containers[]?.env[]? | select(.name == $name)] | length' \
    <<<"${spec}" 2>/dev/null || true)"
  if ! [[ "${addressed}" =~ ^[0-9]+$ ]]; then
    fail "sweep findings: could not read the environment of job/${job}"
    return
  fi
  if ((addressed == 0)); then
    skip "sweep findings: job/${job} was made without ${SWEEP_ENDPOINT_ENV}, so it sent nothing (the chart gives the CronJob the collector's address: a pass made after make deploy has it)"
    return
  fi
  if ! open_grafana; then # the grafana line above said why
    skip "sweep findings: not looked for, because Grafana could not be reached"
    return
  fi
  # shellcheck disable=SC2154  # grafana_url is set by open_grafana (shared.sh)
  query_url="${grafana_url}/api/datasources/proxy/uid/prometheus/api/v1/query"
  query="$(sweep_findings_query)"
  if poll "([.data.result[]? | {(.metric.meridian_finding // \"\"): (.value[1] | tostring)}] | add // {}) as \$got
    | $(printf '%s\n' "${SWEEP_FINDINGS[@]}" | jq -R . | jq -sc .) as \$words
    | if all(\$words[]; \$got[.] != null)
      then [\$words[] | \"\(.)=\(\$got[.])\"] | join(\", \") else empty end" \
    -G "${query_url}" --data-urlencode "query=${query}"; then
    # shellcheck disable=SC2154  # poll_result is set by poll (shared.sh)
    pass "sweep findings: job/${job} succeeded and Prometheus has all ${#SWEEP_FINDINGS[@]} findings of ${SWEEP_SERIES} (job ${SWEEP_JOB_LABEL}) from the last ${SWEEP_SERIES_WINDOW}: $(clean_lines "${poll_result}")"
    return
  fi
  # One more look, for the message only; ${poll_error} is what the last attempt saw.
  final="$(gcurl -G "${query_url}" --data-urlencode "query=${query}" 2>/dev/null || true)"
  sweep_findings_report "${final}" "${job}"
}

# Check 7: the sweep's two lines, the Job's and its findings'.
check_sweep() {
  sweep_job_finished=""
  check_sweep_job
  check_sweep_findings
}
