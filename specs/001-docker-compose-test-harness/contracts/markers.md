# Contract: Test Markers

**Feature**: [Docker-Compose Test Harness](../spec.md)

Tests declare their environment requirements with pytest markers; the harness
skips incompatible tests with an explicit reason (FR-008, SC-005).

## Current (already implemented)

### `@pytest.mark.skip_if_zk_version(condition)`

PEP 440 `SpecifierSet` (e.g. `"<3.5"`, `">=3.8, <4.0"`) against the active
ensemble version. Evaluated by the autouse `check_skip_version_marker` fixture.

```python
@pytest.mark.skip_if_zk_version("<3.8")
def test_modern_api(zkclient):
    ...
```

## Target surface (per PYTEST_INTEG.md, to be finalized in tasks)

### `@pytest.mark.zk_version(spec)`

Version gate with a SpecifierSet string.

### `@pytest.mark.zk_auth(*allowed, skip=None)`

Runs only under the listed auth schemes, or skips the listed ones.

### `@pytest.mark.zk_features(require=None, skip=None)`

Runs only when all `require` features are active and none of `skip` are.

```python
@pytest.mark.zk_version(">=3.8")
@pytest.mark.zk_features(require=["ttl"], skip=["readonly"])
def test_create_ttl_znode(zkclient):
    ...
```

## Evaluation rules

| Condition | Action |
|---|---|
| Version not matching `SpecifierSet` | `pytest.skip("Requires ZK ...")` |
| Auth not in allowed list | `pytest.skip("Requires auth in ...")` |
| Auth in skip list | `pytest.skip("Incompatible with auth ...")` |
| Missing required feature | `pytest.skip("Missing required feature(s): ...")` |
| Present incompatible feature | `pytest.skip("Incompatible with active feature(s): ...")` |

## Validation

- Markers are registered (pytest `markers` ini / `pytest_configure`) to avoid
  "unknown marker" warnings.
- Skipping happens at collection time (`pytest_collection_modifyitems`) so
  incompatible tests never spin up clients (PYTEST_INTEG rationale).
- Reason strings are actionable (state what is required vs. what is active).

See also [fixtures.md](./fixtures.md).
