<!--
Sync Impact Report
------------------
Version change: (template from .specify/templates/constitution-template.md) → 1.0.0

This is the initial ratification of the Kazoo constitution; all values are derived from
the master branch baseline.
Modified principles: none renamed (all five principle slots concretized from template)
Added sections: none beyond template slots; all template placeholders resolved:
  - Core Principles (5 principles)
  - Security & Authentication Requirements (template Section 2)
  - Development Workflow & Quality Gates (template Section 3)
  - Governance
Removed sections: none
Follow-up TODOs: none. RATIFICATION_DATE set to today (2026-08-08) as this is the first
ratification of the constitution document.
-->

# Kazoo Constitution

## Core Principles

### I. Higher-Level API & Recipes
Kazoo is a production-grade, higher-level Python client for Apache ZooKeeper that is
library-first. New capabilities MUST be delivered as standalone, self-contained recipes
(e.g. lock, counter, leader election, cache, lease, queue) that wrap ZooKeeper primitives
in a safe, clear API. Recipes MUST be independently testable, publicly documented, and
MUST have a clear, testable purpose.

### II. Test-First (NON-NEGOTIABLE)
Every change MUST be test-first. Tests MUST be written and approved before implementation,
MUST fail before the implementation exists, and MUST pass once it is added (strict
red-green-refactor cycle). Every patch MUST include tests that cover the new or changed
behavior, and MUST NOT reduce the coverage of the existing test suite. Rationale: Kazoo is
stable production software consumed by a large community; regressions are unacceptable.

### III. Integration Testing Against Real ZooKeeper
Integration tests MUST run against a real ZooKeeper cluster through
`KazooTestHarness`/`KazooTestCase` (`kazoo.testing`), located via the `ZOOKEEPER_PATH`
environment variable and runnable through `ensure-zookeeper-env.sh`. Session lifecycle,
connection loss/recovery, interrupt handling, and recipe behavior MUST be exercised
against the live cluster, not mocks. Cluster logs MUST be surfaced on test failure to aid
diagnosis. Contract changes and shared schemas MUST be covered by integration tests in
addition to unit tests.

### IV. Backward Compatibility & Semantic Versioning
The public API MUST remain backward compatible. Breaking changes MUST only land in a MAJOR
version, MUST be recorded in `CHANGES.md`, and MUST be described under `BREAKING CHANGES`
in the release notes. Supported Python interpreters (per `setup.cfg` classifiers, CPython
and PyPy) and handler backends (`threading`, `gevent`, `eventlet`) MUST NOT be broken by a
change.

### V. Rigorous Quality Gates
Every patch MUST pass peer review, a full test run, flake8/black linting, and mypy strict
type checking before merge. Complexity MUST be justified; the static-analysis and warning
strictures in `pyproject.toml` MUST NOT be relaxed for new code. Commits MUST follow the
Angular convention (`<type>(<scope>): <subject>`) with imperative present-tense subjects.

## Security & Authentication Requirements

Authentication and ACL behavior MUST remain correct: SASL Digest-MD5 via `pure_sasl`
(client and server side), TLS/mTLS via `pyOpenSSL`, and ZooKeeper ACL permissions
(`kazoo.security`). Any change in these areas MUST include tests that exercise the
authentication/authorization path. Credentials, keytabs, and other secrets MUST NOT be
committed to the repository, logged, or exposed in test output; test fixtures use
throwaway values only. All contributed code is licensed under Apache-2.0 and MUST remain
compatible with that license.

## Development Workflow & Quality Gates

- Contributions MUST be prepared via a `master` fork and PR branch based on current
  `master`; PRs MUST NOT introduce merge commits.
- PRs MUST run the full automated test suite, MUST pass linting and typing, and MUST NOT
  decrease code coverage. "Work in progress" PRs are allowed but MUST be labeled and MUST
  NOT be merged until green and reviewed.
- Every patch MUST include adequate tests; it is the author's and reviewer's shared
  responsibility to ensure adequate coverage.
- Recipes MUST declare maintainer(s) in the RST metadata and SHOULD have at least two
  maintainers.
- Commits MUST be signed; commit messages follow the Angular convention with a body that
  references the motivating issue (`closes #`) where applicable.

## Governance

This constitution supersedes undocumented practices and conflicting process guidance.
Amendments MUST be documented, reviewed, and approved before taking effect; an amendment
that changes an API MUST include a migration plan and be recorded here. Versioning policy:
this document follows semantic versioning — PATCH for clarifications and typo fixes, MINOR
for new principles or expanded guidance, MAJOR for incompatible principle removals or
redefinitions. Compliance review expectation: PR reviewers MUST verify conformance with
these principles as part of review.

Use `CONTRIBUTING.md` for runtime development guidance, `.readthedocs.yaml` and `docs/`
for documentation builds, and the "Development Workflow" section above for process gates.

**Version**: 1.0.0 | **Ratified**: 2026-08-08 | **Last Amended**: 2026-08-08