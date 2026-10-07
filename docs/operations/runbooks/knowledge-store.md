# Runbook: Knowledge store differs from the manifest

`meridian knowledge verify` printed one or more `DIFFERENCE` lines, or the
ingestion Job on kind failed after its write, or the command ended with an
`ERROR` line. The stored policy clauses, which every wording search returns
and a triage proposal cites, are not what the manifest-verified wordings give.

Status (S067): implemented and tested against PostgreSQL (the command, its
migration `0026` and the Job's command line), labelled **tested without a
cluster**: the Job's command was run in a shell with a stand-in for
`meridian`, and nothing here has run on kind. The next step that holds the
cluster proves the Job there. A Job that verifies without ingesting is not
built.

## What it checks, and what it cannot

The ingestion checks each wording against `manifest.json` before it writes the
clauses (T-57). Nothing read the stored rows back, so a clause rewritten
afterwards by a role that can write the table stayed in the store and in every
search. `verify` reads the wordings the way `ingest` does (the same refusals,
by the same code), cuts them into clauses the same way, reads the stored
clauses and compares them, per product, wording version and clause.

- It finds a change **only when it is run**. A clause rewritten after the last
  run and put back before the next one is not seen, and the search does not
  check anything: the injection suite rewrites stored clauses on purpose, and
  its outcomes must not move. This is a check someone runs, not a control that
  prevents.
- It does not read the vector, the keyword index or the embedding's
  deployment: recomputing a vector is a paid call to the gateway. A changed
  vector with unchanged text is not found.
- It finds a wording changed after it was generated, not a directory forged
  together with its manifest (T-57's residual).

## What you see

One line per difference, a last line with the counts, and an exit status:

```text
DIFFERENCE HOME-STD 2026-01 1.1 body-differs
DIFFERENCE MOTOR-TPL 2026-01 9.9 not-in-manifest
clauses: 85 stored: 86 differences: 2
```

A line names the clause and the kind and never a text: the body is the very
thing that may have been rewritten. A stored name that does not look like a
product code, a version or a clause number is shown as `?`.

| Exit | Meaning |
|---|---|
| 0 | the store is what the manifest says |
| 1 | at least one difference was found (the Job fails) |
| 2 | the check could not be made: the wording is refused as `ingest` would refuse it, a variable is missing or the database cannot be read |

Each run leaves one row in the audit log: service `knowledge-ingestion`, event
`knowledge.verify`, outcome `verified`, `differs` or `refused`, and the counts
in `reason` (`clauses=85 stored=85 differences=0`; for a refusal, the reason
word). The row names the database role `knowledge_ingest`.

## What a difference means

| Kind | The store, compared with the wordings |
|---|---|
| `body-differs`, `title-differs`, `section-differs` | holds other text for a clause: it was rewritten after ingestion, or ingested from other files |
| `source-hash-differs` | holds a clause whose source hash is not the manifest's for that wording |
| `not-in-manifest` | holds a clause the wordings do not give: planted, or left by an ingestion of other files |
| `not-stored` | lacks a clause the wordings give: deleted, or never ingested |

A difference means someone or something with write access to
`knowledge.chunks` (the owner role, a superuser, a restore, an ingestion by
hand from other files) changed it. No service role can update a row. The
ingestion's role can still delete and insert, so a compromised ingestion
process could replace the corpus without an update; that is why the check
exists. Treat it as a change of the evidence behind proposals, not as a
flaw in the wording.

## When to run it

- After anything that could have written the table other than `make deploy`'s
  ingestion: a restore, a PostgreSQL major upgrade (the store must be ingested
  again then anyway), a session with `psql` as the owner, a proposal that
  cites a clause you do not recognise.
- Before an evaluation recording and before a demonstration that quotes
  clauses.
- `make deploy` runs it after each ingestion. It does **not** run when the
  deploy finds this image's ingestion Job already succeeded and the store not
  empty, so a deploy does not re-check a store it did not write. Deleting the
  Job makes the next deploy ingest again, which also replaces the clauses.

The command needs the ingestion role's database URL
(`MERIDIAN_INGEST_DATABASE_URL`), which on kind is in the Secret
`knowledge-ingest-db`: printing a Secret is the owner's act (hard rule 8). A
person who has it runs, from a checkout of the same commit as the image:

```sh
uv run meridian knowledge verify --from data/synthetic
```

## What to do

1. Do not delete the kind cluster or the database. On kind its database is the
   only copy of the audit log, and the rewritten rows are the only copy of what
   was written.
2. Read the lines. Note the clauses and kinds; they name where to look.
3. Before the store is replaced, the owner reads the named rows (`psql` in the
   database's pod, see [Looking into the
   database](../README.md#looking-into-the-database-on-kind)) and keeps what
   they need: the next ingestion deletes every row.
4. Find who wrote: the audit log holds the ingestion's own rows
   (`knowledge.ingest`) and each verification, and no row for a write the
   owner role made by hand (T-25).
5. Ingest again from the generator's files (`make synthetic` reproduces them;
   delete the ingestion Job and run `make deploy`). The replacement is one
   transaction. Run `verify` again: it must print zero differences.

## What not to do

- **Do not edit the manifest to make a difference go away.** The manifest is
  what the check trusts; a hash that is changed to match a rewritten wording
  moves the problem into the files.
- **Do not read a clean run as proof about search.** It says what was stored
  when it ran.
- **Do not run `verify` against files you did not generate.** It will report
  every clause that differs from them, and the report is correct.
