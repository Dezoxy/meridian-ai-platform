{{- /*
What a Deployment keeps and when its pods restart (S073). The helpers are here
and not in _helpers.tpl, which stays under 800 lines
(tests/meridian/test_helm_rate_store_restart.py holds it there).

The restarts are spread only when the renewal comes before the earliest look at
the file (the last service's, one and five sixths of the margin before the end):
always with the default renewBefore, and with a set one when it is longer than
that. The chart refuses no shorter renewBefore, on purpose: the health rule
reads the file, so a service whose look has passed stays healthy until the file
holds the renewed certificate and then restarts. The services whose look has
passed restart together when the file changes, as all of them did before the
spread. That costs the spread, never availability (pinned by
test_a_renewal_after_a_services_restart_time_costs_the_spread_not_health in
tests/meridian/common/test_certlife.py).
*/ -}}

{{- /*
revisionHistoryLimit: how many old ReplicaSets a Deployment keeps; takes the
root. It is `revisionHistoryLimit` in values.yaml, 2 by default, one value for
every Deployment the chart renders. Refused, with the value's name in the
message, when it is not a whole number of at least 1 (an empty or null value
too): 0 would leave no rollback target, and an unset limit would leave
Kubernetes' ten.
*/ -}}
{{- define "meridian.revisionHistoryLimit" -}}
{{- $limit := toString .Values.revisionHistoryLimit -}}
{{- if not (regexMatch "^[1-9][0-9]*$" $limit) -}}
{{- fail (printf "revisionHistoryLimit is %q, which is not a whole number of at least 1: it is how many old ReplicaSets each Deployment keeps for a rollback" $limit) -}}
{{- end -}}
{{- $limit -}}
{{- end -}}

{{- /*
restartShare: the share of the restart margin a service is given; takes root and
name. Its place in the list of services over their count, as a decimal from 0 up
to, not including, 1: the first service none, the last five sixths of six. The
list is the order Helm ranges the `services` map in, sorted by name
(services.yaml renders in it), so a service added or removed moves the others'
shares at the next deploy. certlife (MERIDIAN_TLS_RESTART_SHARE) restarts the
service that share of a margin earlier than it would have, so the services do
not all restart in the same minute at a renewal. Helm prints the quotient with
sixteen digits, which is exact enough: the error is far below a microsecond of
a day's margin. Two replicas of one service get the same share and still
restart together (they mount one Secret).
*/ -}}
{{- define "meridian.restartShare" -}}
{{- $names := keys .root.Values.services | sortAlpha -}}
{{- $place := 0 -}}
{{- range $index, $name := $names -}}
{{- if eq $name $.name -}}
{{- $place = $index -}}
{{- end -}}
{{- end -}}
{{- divf $place (len $names) -}}
{{- end -}}
