# shellcheck shell=bash
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
#                 A pgvector read that itself fails (the pod, the connection or
#                 the statement's deadline) is a FAIL that says it could not
#                 read pg_extension and gives the first line of what psql or
#                 kubectl wrote, cleaned and cut the same way (S073, K4); it was
#                 an empty answer, "not installed", before.
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
  # shellcheck disable=SC2154  # api_server_addresses and api_server_problem are set by api_server_matches_policy (common.sh)
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
  local primary database version err_file reason
  check_database_api_server
  primary="$(kctl -n meridian get pod \
    -l cnpg.io/cluster=platform-db,cnpg.io/instanceRole=primary \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [[ -z "${primary}" ]]; then
    fail "database: no primary pod found for platform-db"
    return
  fi
  for database in app meridian; do
    err_file="$(mktemp)"
    if version="$(kctl -n meridian exec "${primary}" -c postgres -- \
      env "PGOPTIONS=${PSQL_OPTIONS}" psql -d "${database}" -tAc "SELECT extversion FROM pg_extension WHERE extname='vector'" \
      2>"${err_file}")"; then
      version="$(clean_lines "${version}")"
      if [[ -n "${version}" ]]; then
        pass "database: pgvector ${version} installed in ${primary}, database ${database}"
      else
        fail "database: extension vector is not installed in ${primary}, database ${database}"
      fi
    else
      # The read itself failed (the pod, the connection or the deadline): not
      # an answer, so not "not installed". The first line of what it wrote.
      reason="$(clean_lines "$(head -n 1 "${err_file}")")"
      reason="${reason:-no message}"
      fail "database: could not read pg_extension in ${primary}, database ${database}: ${reason:0:QUERY_ERROR_LENGTH}"
    fi
    rm -f "${err_file}"
  done
  check_stores "${primary}"
}
