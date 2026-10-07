"""Rendering, stand-in programs and the runner for the twin's boot-script tests.

``test_gcp_kubeadm_bootstrap.py`` runs the two cloud-init scripts of
``infra/terraform/gcp-kubeadm/templates/`` whole, under bash, against stand-in
programs, each of which logs its arguments to a file under the test's own
``tmp_path``. It is the Google Cloud twin of ``awskubeadmsupport`` and takes from
that module what is not about a cloud (the rendering, the stand-ins of the
package tools, of ``kubeadm`` and of ``gpg``, and the fixed values of the join
command), so that the two cannot drift. What is new here is the stand-in for
``curl``, which plays the metadata server and Secret Manager and is as strict
as the one for AWS: a URL, a header or a flag it does not expect is an exit
status of 99 and a line on its error stream.
"""

import os
import stat
import subprocess
from pathlib import Path

from awskubeadmsupport import (
    ADDRESS,
    CONTAINERD_CONFIG,
    CTR_VERSION_ANSWER,
    FINGERPRINT,
    LOG,
    MANIFEST,
    MANIFEST_SHA256,
    MINOR,
    PICK,
    POD_CIDR,
    PORT,
    SECONDS,
    VALID,
    Scratch,
    render,
)
from awskubeadmsupport import CALICO_VERSION as CALICO_VERSION
from awskubeadmsupport import STUBS as AWS_STUBS
from servicesupport import REPO_ROOT

MODULE = REPO_ROOT / "infra" / "terraform" / "gcp-kubeadm"
TEMPLATES = MODULE / "templates"

# ── fixed inputs ─────────────────────────────────────────────────────────────

# The control plane's internal address is ADDRESS (the join command's fixed
# value, a documentation address): the workers' text is rendered for it, and the
# join command names it. The public address is the documentation's second range.
INTERNAL_ADDRESS = ADDRESS
PUBLIC_ADDRESS = "198.51.100.20"
LOCATION = "europe-west3"
STORE_ID = "meridian-gcp-kubeadm-join-command"
PROJECT_ID = "example-project"
# The text after "Bearer " in the stand-in's response: made up, never a real one.
BEARER_VALUE = "stand-in-access-value-0123456789abcdefghijklmnopqrstuvwxyz"
API_HOST = f"secretmanager.{LOCATION}.rep.googleapis.com"
API_PATH = (
    f"https://{API_HOST}/v1/projects/{PROJECT_ID}/locations/{LOCATION}"
    f"/secrets/{STORE_ID}"
)
METADATA = "http://169.254.169.254/computeMetadata/v1"

COMMON_VALUES = {
    "kubernetes_minor": MINOR,
    "kubernetes_apt_signer_fingerprint": FINGERPRINT,
}
CONTROL_PLANE_VALUES = {
    "location": LOCATION,
    "secret_id": STORE_ID,
    "internal_address": INTERNAL_ADDRESS,
    "public_address": PUBLIC_ADDRESS,
    "api_port": PORT,
    "pod_network_cidr": POD_CIDR,
    "calico_version": CALICO_VERSION,
    "calico_manifest_sha256": MANIFEST_SHA256,
}
WORKER_VALUES = {
    "location": LOCATION,
    "secret_id": STORE_ID,
    "control_plane_address": INTERNAL_ADDRESS,
    "api_port": PORT,
}

# ── rendering ────────────────────────────────────────────────────────────────


def template_text(name: str) -> str:
    return (TEMPLATES / f"{name}.sh.tftpl").read_text(encoding="utf-8")


def rendered(role: str) -> str:
    common = render(template_text("node-common"), COMMON_VALUES)
    values = CONTROL_PLANE_VALUES if role == "control-plane" else WORKER_VALUES
    return render(template_text(role), {**values, "common": common})


ROLES = ["control-plane", "worker"]

# Compute Engine's page "Set custom metadata" (read 2026-10-07, updated
# 2026-10-05) says each metadata value is at most 256 KB, and all entries
# together at most 512 KB. The script goes out as the one value `user-data`,
# plain, so the raw size is the one that counts. The other keys the instances set
# are a few hundred bytes. The raw guard is the session's number, against runaway
# growth.
USER_DATA_LIMIT = 256 * 1024
ALL_METADATA_LIMIT = 512 * 1024
RAW_GUARD = 64 * 1024


def user_data_size(role: str) -> int:
    return len(rendered(role).encode("utf-8"))


# ── stand-in programs ────────────────────────────────────────────────────────

# `curl`, as the metadata server and Secret Manager. Every call is checked: the
# metadata server needs its header; a call to Secret Manager needs HTTPS only, no
# redirect, TLS 1.2, a cut-off, and the header FILE (`-H @file`) that holds the
# access token, whose text must be exactly the one header line for the token. A
# POST's body is copied (the file given to --data-binary), and so is the header
# file's text as it was at the call. A series <name>.N answers the Nth call (see
# PICK); an empty metadata answer is the server's 404.
CURL = r"""
url=""; out=""; method=GET; data=""; headers=(); header_files=(); flags=" "
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[i]}" in
    --output) out="${args[i + 1]}" ;;
    -X|--request) method="${args[i + 1]}" ;;
    --data-binary) data="${args[i + 1]}" ;;
    -H|--header)
      if [[ ${args[i + 1]} == @* ]]; then header_files+=("${args[i + 1]#@}")
      else headers+=("${args[i + 1]}"); fi ;;
    --proto|--proto-redir) flags+="${args[i]}=${args[i + 1]} " ;;
    --location|-L) flags+="--location " ;;
    --tlsv1.2|--max-time|--fail) flags+="${args[i]} " ;;
    http*) url="${args[i]}" ;;
  esac
done
refuse() { echo "stub curl: $*" >&2; exit 99; }
http_error() {
  echo "curl: (22) The requested URL returned error: $1" >&2; exit 22
}
has_header() {
  local h
  for h in "${headers[@]}"; do [[ $h == "$1" ]] && return 0; done
  return 1
}
metadata_value() {
  local value
  value=$(pick "$1") || http_error 404
  [[ -n $value ]] || http_error 404
  printf '%s' "$value"
}
[[ $flags == *" --fail "* ]] || refuse "no --fail in: $*"
[[ $flags == *" --max-time "* ]] || refuse "no --max-time in: $*"
case "$url" in
  http://169.254.169.254/computeMetadata/v1/*)
    has_header 'Metadata-Flavor: Google' || http_error 403
    [[ $method == GET ]] || refuse "the metadata server is only read: $*"
    key="${url#http://169.254.169.254/computeMetadata/v1/}"
    case "$key" in
      instance/service-accounts/default/token)
        if [[ -e "{scratch}/token-response" ]]; then
          cat "{scratch}/token-response"; exit 0
        fi
        pick token >/dev/null || exit $?
        printf '{"access_token":"%s","expires_in":3599,"token_type":"Bearer"}' \
          "{bearer}" ;;
      project/project-id) metadata_value project ;;
      instance/network-interfaces/0/ip) metadata_value internal ;;
      instance/network-interfaces/0/access-configs/0/external-ip)
        metadata_value external ;;
      *) refuse "unexpected metadata key $key" ;;
    esac ;;
  https://pkgs.k8s.io/*/Release.key) cp "{scratch}/release.key" "$out" ;;
  https://raw.githubusercontent.com/*/manifests/calico.yaml)
    cp "{scratch}/calico.yaml" "$out" ;;
  "{api_path}"/*|"{api_path}":*)
    [[ $flags == *" --proto==https "* ]] || refuse "not https only: $*"
    [[ $flags == *" --proto-redir==https "* ]] || refuse "not https only: $*"
    [[ $flags == *" --tlsv1.2 "* ]] || refuse "no --tlsv1.2 in: $*"
    [[ $flags != *" --location "* ]] || refuse "follows redirects: $*"
    ((${#header_files[@]} == 1)) || refuse "the token must be in a header file: $*"
    for h in "${headers[@]}"; do
      [[ $h == "Content-Type: application/json" ]] || refuse "header: $h"
    done
    [[ "$*" != *"{bearer}"* ]] || refuse "the access token is on the command line"
    n=0; [[ -f "{scratch}/api-calls.count" ]] && n=$(<"{scratch}/api-calls.count")
    echo $((n + 1)) >"{scratch}/api-calls.count"
    cp "${header_files[0]}" "{scratch}/auth.$n"
    stat -c '%a' "${header_files[0]}" >"{scratch}/auth.$n.mode"
    [[ $(<"${header_files[0]}") == "Authorization: Bearer {bearer}" ]] ||
      http_error 401
    case "$url" in
      "{api_path}/versions/latest:access")
        [[ $method == GET ]] || refuse "an access is a GET: $*"
        if [[ -e "{scratch}/raw-response" ]]; then
          cat "{scratch}/raw-response"; exit 0
        fi
        pick secret >"{scratch}/answer" || exit $?
        encoded=$(base64 -w0 <"{scratch}/answer")
        shape='{\n  "name": "projects/1/locations/x/secrets/y/versions/1",\n'
        shape+='  "payload": {\n    "data": "%s",\n    "dataCrc32c": "1"\n  }\n}\n'
        printf "$shape" "$encoded" ;;
      "{api_path}:addVersion")
        [[ $method == POST ]] || refuse "an addVersion is a POST: $*"
        has_header 'Content-Type: application/json' || refuse "no content type"
        [[ $data == @* && -f ${data#@} ]] || refuse "the body must be a file: $*"
        m=0; [[ -f "{scratch}/post.count" ]] && m=$(<"{scratch}/post.count")
        echo $((m + 1)) >"{scratch}/post.count"
        cp "${data#@}" "{scratch}/post.$m.body"
        failures=0
        [[ -f "{scratch}/post-failures" ]] && failures=$(<"{scratch}/post-failures")
        if ((m < failures)); then
          if [[ -e "{scratch}/post-error" ]]; then
            cat "{scratch}/post-error" >&2
          else
            echo "curl: (22) The requested URL returned error: 403" >&2
          fi
          exit 22
        fi
        printf '{"name": "x/versions/1"}' ;;
      *) refuse "unexpected call $url" ;;
    esac ;;
  *) refuse "unexpected $url" ;;
esac
"""

STUBS = {
    **{
        name: body
        for name, body in AWS_STUBS.items()
        if name not in {"aws", "snap", "curl"}
    },
    "curl": CURL,
}


def stub_text(name: str, scratch: Path) -> str:
    text = "#!/usr/bin/env bash\n"
    text += PICK.format(scratch=scratch)
    text += LOG.format(name=name, scratch=scratch) + "\n"
    body = STUBS[name]
    for key, value in {
        "{scratch}": str(scratch),
        "{bearer}": BEARER_VALUE,
        "{api_path}": API_PATH,
    }.items():
        body = body.replace(key, value)
    return text + body


def make_scratch(tmp_path: Path) -> Scratch:
    """A scratch directory with every stand-in program written, and the answers
    of a node that boots well: the metadata server reports the two addresses, the
    project and a token; the key has the pinned fingerprint, the manifest is the
    pinned one, kubeadm prints a join command, and the secret's newest version
    holds one."""
    scratch = Scratch(tmp_path)
    for directory in ("bin", "root", "tmp"):
        (tmp_path / directory).mkdir(exist_ok=True)
    (tmp_path / "calls").touch()
    for name in STUBS:
        path = tmp_path / "bin" / name
        path.write_text(stub_text(name, tmp_path), encoding="utf-8")
        path.chmod(0o755)
    (tmp_path / "release.key").write_text("stand-in key\n", encoding="utf-8")
    (tmp_path / "calico.yaml").write_text(MANIFEST, encoding="utf-8")
    scratch.listing((FINGERPRINT, ""))
    (tmp_path / "join-line").write_text(VALID + "\n", encoding="utf-8")
    (tmp_path / "containerd.toml").write_text(CONTAINERD_CONFIG, encoding="utf-8")
    scratch.series("internal", [INTERNAL_ADDRESS])
    scratch.series("external", [PUBLIC_ADDRESS])
    scratch.series("project", [PROJECT_ID])
    scratch.series("token", [""])
    scratch.series("secret", [VALID + "\n"])
    scratch.series("ctr", [CTR_VERSION_ANSWER])
    os.mknod(tmp_path / "containerd.sock", stat.S_IFSOCK | 0o600)
    return scratch


def posted_bodies(scratch: Scratch) -> list[str]:
    """The request bodies that were sent to Secret Manager, in order."""
    return [
        path.read_text(encoding="utf-8")
        for path in sorted(scratch.path.glob("post.*.body"))
    ]


def run_script(
    scratch: Scratch, role: str, **settings: str
) -> subprocess.CompletedProcess[str]:
    """The rendered script of ``role`` under bash, with the stand-ins first on
    PATH, its root prefix in the scratch directory and its waits at zero
    seconds. ``settings`` override the scripts' own variables (and add to the
    environment: ``LC_ALL`` is one)."""
    script = scratch.path / f"{role}.sh"
    script.write_text(rendered(role), encoding="utf-8")
    env = {
        "PATH": f"{scratch.path / 'bin'}:/usr/bin:/bin",
        "HOME": str(scratch.path),
        "TMPDIR": str(scratch.path / "tmp"),
        "BOOT_ROOT": str(scratch.path / "root"),
        "POLL_SECONDS": "0",
        "ADDRESS_ATTEMPTS": "3",
        "PUBLISH_ATTEMPTS": "3",
        "JOIN_ATTEMPTS": "3",
        "CONTAINERD_ATTEMPTS": "3",
        "APT_ATTEMPTS": "3",
        **settings,
    }
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env=env,
        cwd=scratch.path,
        check=False,
        timeout=SECONDS,
    )
