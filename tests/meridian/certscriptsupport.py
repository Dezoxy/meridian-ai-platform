"""Names the tests of ``deploy.sh``, ``smoke.sh`` and its probe share (S056).

``test_certificate_deploy.py`` runs ``deploy.sh`` whole against stub commands;
``test_certificate_smoke.py`` runs the functions of ``smoke.sh``'s checks 9 and 10
in bash against a stub ``kctl``; ``test_certificate_probe.py`` runs the identity
probe for real against a local TLS server. Each needs a few of the same values:
the folder of the kind files, the time a subprocess may take, the calling
service and the three CertificateRequestPolicies with the states a stub can
answer for one.
"""

from servicesupport import REPO_ROOT

KIND_DIR = REPO_ROOT / "infra" / "kind"
CALLER = "agent-runtime"
SECONDS = 60

POLICIES = ("meridian-services", "meridian-services-ca", "meridian-deny-unlisted")
POLICY_STATES = {
    "missing": 'echo "Error from server (NotFound): certificaterequestpolicies.'
    'policy.cert-manager.io \\"x\\" not found" >&2; exit 1',
    "unknown-kind": "echo \"error: the server doesn't have a resource type "
    '\\"certificaterequestpolicy\\"" >&2; exit 1',
    "not-ready": "printf False",
    "no-condition": "exit 0",
    "ready": "printf True",
}
