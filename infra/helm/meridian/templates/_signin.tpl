{{- /*
The staff sign-in of the Claims API (S021, Y4b). Written here and not in
_helpers.tpl, which stays under 800 lines (tests/meridian/test_helm_rate_store_restart.py
holds it there).

`signin.mode` is `off` (the default) or `staff`; anything else fails the render,
naming the value, and so does an empty one. With `off` nothing in this file
renders anything: no variable, no pod annotation, no dnsConfig, so the chart is
what it was before the switch existed. With `staff` the Claims API's container,
and no other workload, is given:
  - MERIDIAN_SIGNIN=staff, the app's own switch;
  - each MERIDIAN_SIGNIN_STAFF_* variable that SigninSettings.from_env
    (platform/common/signin.py) and FlowSettings.from_env (signinflow.py) read,
    from `signin.staff`; tests/meridian/test_helm_signin.py builds the settings
    from the rendered environment with those two functions and
    SessionSettings.from_env (signinsession.py), so a name or a format that
    drifts fails there;
  - MERIDIAN_SESSION_EDGE_PLAIN_HTTP, from `signin.edgePlainHttp`;
  - MERIDIAN_SIGNIN_STAFF_CLIENT_CREDENTIAL and MERIDIAN_SESSION_KEY, from the
    keys `client-credential` and `session-key` of the Secret `signin.secret`,
    through secretKeyRef: the chart never holds either value, and an item of a
    values `env` list that names a MERIDIAN_SIGNIN or MERIDIAN_SESSION variable
    is refused, for every service, the switch on or off (the uploads switch's
    pattern);
and its pod gets a pod annotation, `signin.generation` (so a Secret made anew
rolls the pod: a pod reads its environment at its start), and a dnsConfig.

The dnsConfig is a one-second timeout, two attempts and ndots 3, for a pod whose
sign-in calls the issuer and fetches its keys by name: "a name lookup that hangs
holds the fetching caller for the resolver's time" (the step's re-check). The
names the pod resolves are all in the cluster (agent-runtime.meridian.svc,
keycloak.identity.svc, platform-db-rw.meridian.svc, the collector's full name).
ndots must stay ABOVE the two dots of `<service>.<namespace>.svc`: at ndots 2 or
less such a name is asked as it is first, which leaves the cluster's own zone for
the node's resolver before the search list is tried. At 3 the short names are
still asked inside the cluster first, and the collector's full name (four dots)
is asked once, as it is. tests/meridian/test_helm_signin.py models glibc's rule
and checks every name.
*/ -}}

{{- /*
signinMode: the mode, "off" or "staff"; takes the root. Fails for anything else.
*/ -}}
{{- define "meridian.signinMode" -}}
{{- $mode := toString (get (.Values.signin | default dict) "mode" | default "") -}}
{{- if not (or (eq $mode "off") (eq $mode "staff")) -}}
{{- fail (printf "signin.mode is %q, which is neither off nor staff: off (the default) leaves the Claims API's pages as they are, staff turns the staff sign-in on and needs signin.secret, signin.generation and every signin.staff value (a bare off in a values file is the boolean false in YAML 1.1: write \"off\" in quotes)" $mode) -}}
{{- end -}}
{{- $mode -}}
{{- end -}}

{{- /*
signinStaffOn: "true" when the mode is staff, and nothing otherwise; takes the
root. Fails first for a mode that is neither, and for a staff value that is
empty, naming it: the Claims API would not start, or would start without the
check that tells an ID token from an access token.
*/ -}}
{{- define "meridian.signinStaffOn" -}}
{{- if eq (include "meridian.signinMode" .) "staff" -}}
{{- $signin := .Values.signin -}}
{{- range $name := list "secret" "generation" -}}
{{- if not (toString (get $signin $name | default "") | trim) -}}
{{- fail (printf "signin.%s is empty, but signin.mode is staff: %s" $name (ternary "the Secret that holds the pages client's credential and the cookie's key, which deploy.sh reads by name" "the generation annotation of that Secret, which deploy.sh passes with --set-string, so that a Secret made anew rolls the pod" (eq $name "secret"))) -}}
{{- end -}}
{{- end -}}
{{- $staff := get $signin "staff" | default dict -}}
{{- range $name := list "issuer" "audience" "keysUrl" "clientId" "authorizationUrl" "tokenUrl" "endSessionUrl" "appOrigin" "redirectUri" "postLogoutRedirectUri" "requiredTyp" "allowedAzp" -}}
{{- if not (toString (get $staff $name | default "") | trim) -}}
{{- fail (printf "signin.staff.%s is empty, but signin.mode is staff: the Claims API would not start, or would not tell an ID token from an access token (requiredTyp and allowedAzp are set from a live token of the issuer)" $name) -}}
{{- end -}}
{{- end -}}
{{- $plain := toString (get $signin "edgePlainHttp" | default false) -}}
{{- if not (or (eq $plain "true") (eq $plain "false")) -}}
{{- fail (printf "signin.edgePlainHttp is %q, which is neither true nor false" $plain) -}}
{{- end -}}
true
{{- end -}}
{{- end -}}

{{- /*
signinEnvRefusal: fails when the values env item NAME of the service SERVICE is
one of the sign-in or session variables, which come from signin.* and nowhere
else; takes service, name. A name that only starts like one (MERIDIAN_SIGNING)
is none.
*/ -}}
{{- define "meridian.signinEnvRefusal" -}}
{{- if or (eq .name "MERIDIAN_SIGNIN") (hasPrefix "MERIDIAN_SIGNIN_" .name) (eq .name "MERIDIAN_SESSION") (hasPrefix "MERIDIAN_SESSION_" .name) -}}
{{- fail (printf "services.%s.env sets %s: the sign-in and session variables of the Claims API come from signin.mode, signin.staff, signin.secret and signin.edgePlainHttp and from nowhere else (the credential and the cookie's key from a Secret, never a value), so no values env item may set one" .service .name) -}}
{{- end -}}
{{- end -}}

{{- /*
signinEnv: the Claims API's sign-in variables as env items; takes the root. Call
it only when signinStaffOn is "true".
*/ -}}
{{- define "meridian.signinEnv" -}}
{{- $signin := .Values.signin -}}
{{- $staff := $signin.staff -}}
- name: MERIDIAN_SIGNIN
  value: "staff"
{{- range $name, $suffix := dict "issuer" "ISSUER" "audience" "AUDIENCE" "keysUrl" "KEYS_URL" "clientId" "CLIENT_ID" "authorizationUrl" "AUTHORIZATION_URL" "tokenUrl" "TOKEN_URL" "endSessionUrl" "END_SESSION_URL" "appOrigin" "APP_ORIGIN" "redirectUri" "REDIRECT_URI" "postLogoutRedirectUri" "POST_LOGOUT_REDIRECT_URI" "requiredTyp" "REQUIRED_TYP" "allowedAzp" "ALLOWED_AZP" }}
- name: MERIDIAN_SIGNIN_STAFF_{{ $suffix }}
  value: {{ get $staff $name | toString | trim | quote }}
{{- end }}
- name: MERIDIAN_SESSION_EDGE_PLAIN_HTTP
  value: {{ toString $signin.edgePlainHttp | quote }}
- name: MERIDIAN_SIGNIN_STAFF_CLIENT_CREDENTIAL
  valueFrom:
    secretKeyRef:
      name: {{ $signin.secret }}
      key: client-credential
- name: MERIDIAN_SESSION_KEY
  valueFrom:
    secretKeyRef:
      name: {{ $signin.secret }}
      key: session-key
{{- end -}}

{{- /* signinGeneration: the pod annotation's value; takes the root. */ -}}
{{- define "meridian.signinGeneration" -}}
{{- toString .Values.signin.generation | trim -}}
{{- end -}}

{{- /* signinDnsConfig: the Claims API's resolver options; see the top. */ -}}
{{- define "meridian.signinDnsConfig" -}}
dnsConfig:
  options:
    - name: ndots
      value: "3"
    - name: timeout
      value: "1"
    - name: attempts
      value: "2"
{{- end -}}
