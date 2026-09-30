# Security policy

## Reporting a vulnerability

**Please open a public issue** unless the problem is one of these:

- a **secret leak** — a password, connection string or secret reference reaching a log, a
  file, the audit log or a crash trace;
- a way to make the app **write when it should refuse** (bypassing read-only, the
  environment gate, the production confirmation, or the keyless-write refusal);
- **SQL injection** through any input the app builds itself;
- a way to get a **different table's rows modified** than the one on screen.

Those four are worth disclosing privately, and you can use GitHub's
"Report a vulnerability" button on the Security tab of the repository. Everything else —
an awkward grid, a confusing dialog, a missing keybinding — is a normal public issue and
will get a faster answer there.

Either way, **please say plainly that it is a security issue.** Naming the class of bug
("this is a write-when-refused bypass") makes it far easier to triage and to prioritise.

## What the app does and does not protect

Being explicit here is more useful than a reassuring summary.

**Protects:**

- Nothing is written to the database until Apply, and Apply runs in a single transaction.
- Read-only per profile, **on by default for production**; `--read-only` locks it for the
  session. Refusals are enforced in `services/safety.py` and covered by unit tests.
- Row identity requires a primary key (or a single-column non-null UNIQUE). Keyless
  UPDATE/DELETE is refused by default; an `UPDATE` is never emitted as `WHERE 1=1`.
- Optimistic concurrency: an `UPDATE`/`DELETE` is scoped by the row's key, and by a
  `rowversion` comparison when the table has one, so a row changed by someone else in the
  meantime is a conflict rather than a silent overwrite. Comparing *all* original column
  values is available via the `compare_original` flag rather than being the default.
- Secrets are stored **by reference** (OS keyring or per-session prompt), never as values
  in a profile file. The audit log has a test asserting no secret reaches it.
- Every Apply — committed, rolled back **or refused** — is recorded in a local audit log.

**Does not protect:**

- **The audit log is append-only by convention.** It is a local file; anyone with write
  access to the config directory can edit or delete it. It is a record of what the tool
  did, not a tamper-evident compliance system.
- **The tool trusts the database's own answers.** CHECK and UNIQUE constraints are
  reported as "will be verified by the database"; the app does not evaluate them. An
  `INSTEAD OF` trigger is surfaced as a warning, not interpreted.
- **The keyring is only as strong as the OS.** In-memory secret storage falls back to
  prompting per session when no keyring is available.
- **`--read-only` is a guardrail, not a sandbox.** It is an application-level refusal; it
  does not revoke database permissions. Enforce real authorization in the database.
- **TLS / certificate policy is the driver's**, configured through the ODBC connection
  string. Review it for your environment.
- The single-file PyInstaller build is not signed.

## Supported versions

The project is at `0.1.0` (beta, pre-1.0). Fixes land on `master`. There are no
long-term-support branches yet.
