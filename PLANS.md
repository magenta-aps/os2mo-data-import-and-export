# Plan: Migrate DIPEX `/service` REST writes to GraphQL mutations

Your goal is to migrate `os2mo-data-import-and-export` to use the `/graphql` API
for writes (create/edit/terminate), dropping the use of the `/service` API for
writes entirely.

**No changes to `os2mo_data_import` (the upstream `MoraHelper` package).** All
GraphQL write logic stays in this repo, following the same pattern used for the
previous `/lora` -> GraphQL migration (see git history, e.g. commit `d4d3a9d5`).

Some items in this plan may be completed already. You can skip those.

## Context

The os2mo-data-import-and-export (DIPEX) repo talks to OS2mo via two APIs:
- **REST `/service/`**: used for writes (create/edit/terminate) in `ad_sync.py` and
  `opus_diff_import.py`, via `MoraHelper._mo_post()` and a few direct `requests` calls.
- **GraphQL `/graphql/v22`**: already used for reads in both files.

`MoraHelper` (from the external `os2mo_data_import` package) is the central REST
wrapper. Its constructor hardcodes `self.host = hostname + "/service/"`. We will
**not** modify it. Instead, we add a local GraphQL writer helper in this repo and
migrate the write call sites to use it. `MoraHelper` remains for reads until reads
are migrated separately (deferred, out of scope for this plan).

The GraphQL schema is defined in the `os2mo` repository
(`mora/graphapi/{mutators.py,inputs.py,models.py}`).

### Precedent: the `/lora` migration

A similar migration was done to replace the deprecated `/lora` API with GraphQL.
That migration constructed `GraphQLClient` instances inline and defined `gql(...)`
query/mutation constants locally in this repo, with no changes to `os2mo_data_import`.
See commit `d4d3a9d5` (`class_tools.find_duplicate_classes`) for the pattern.

## Verified Schema Facts

All mutations confirmed in OS2mo source (`mora/graphapi/mutators.py` + `inputs.py` + `models.py`):

- `*_create`, `*_update`, `*_terminate` mutations exist for: address, engagement, employee,
  org_unit, ituser, manager, class, facet, itsystem
- `*_terminate` takes a dedicated `*TerminateInput` with `uuid` + `validity` (`to` required,
  `from` optional for interval termination)
- `ClassCreateInput` has an `owner: UUID` field
- `OrganisationUnitUpdateInput` has NO `clamp` or `force` field
- Validity format: `{"from": "2020-01-01", "to": None}` (plain date strings, confirmed
  by `exporters/sql_export/tests/integration/conftest.py:23`)
- All mutations return `{"{entity}_create": {"uuid": "..."}}` (or `_update`/`_terminate`)
- GraphQL versions 21-30 available; codebase uses v22
- Field naming: `snake_case` (the schema disables auto-camelCase)

### Payload transformation rules

1. Drop `"type"` discriminator (mutation name is typed)
2. Drop `"data"` wrapper for edits
3. Unwrap `{"uuid": "..."}` dicts to bare UUID strings
4. Employee renames: `givenname` -> `given_name`, `cpr_no` -> `cpr_number`, drop `org` field
5. Termination: use `*_terminate` mutation with `{"uuid": X, "validity": {"to": date, "from": from_date?}}`
6. Manager `responsibility`: `[{"uuid": X}]` -> `[X]` (list of bare UUIDs)

### Reference: working GraphQL mutation examples

`exporters/sql_export/tests/integration/conftest.py` contains working examples of every
create mutation needed (lines 108-608). Copy mutation string shapes from there:
`org_create`, `employee_create`, `facet_create`, `class_create`, `itsystem_create`,
`ituser_create`, `org_unit_create`, `kle_create`, `address_create`, `association_create`,
`engagement_create`, `leave_create`, `manager_create`.

## Checklist

### Phase 0: Inline MoraHelper calls and drop `os2mo_data_import` dependency

- [x] 0.1. Inline calls to `MoraHelpers` and other dependencies on `os2mo_data_import`
- [x] 0.2. remove `os2mo_data_import` dependency

### Phase 1: Create local GraphQL writer helper (`integrations/gql_writer.py`)

- [x] 1.1. Create `integrations/gql_writer.py` with a `GraphQLWriter` class that:
      - Accepts a `GraphQLClient` (sync) in its constructor, or auth params to
        construct one (reuse the pattern from `opus_diff_import.py:197-208`
        or `ad_writer.py:290-299`).
      - Has a `dry_run: bool` flag. When True, `create`/`update`/`terminate` log
        the mutation + input and return a fake UUID without executing. This
        replaces the current `MOPostDryRun` monkeypatch pattern
        (`opus_diff_import.py:140-150`).

- [x] 1.2. Add mutation string constants (as `gql(...)` objects) for every
      `*_create`, `*_update`, `*_terminate` mutation needed. Copy shapes from
      `exporters/sql_export/tests/integration/conftest.py`. Needed entities:
      `address`, `engagement`, `employee`, `org_unit`, `ituser`, `manager`, `class`.
      Mutation string template:
      ```python
      ADDRESS_CREATE = gql("""
      mutation CreateAddress($input: AddressCreateInput!) {
          address_create(input: $input) { uuid }
      }
      """)
      ```

- [x] 1.3. Add a `create(self, entity_type: str, input_data: dict) -> str` method
      that dispatches to `{entity_type}_create` mutations via a lookup table
      mapping entity type -> mutation constant. Executes the mutation with
      `variable_values={"input": input_data}` and returns the UUID from the
      response (`resp[f"{entity_type}_create"]["uuid"]`).

- [x] 1.4. Add an `update(self, entity_type: str, input_data: dict) -> str` method
      that dispatches to `{entity_type}_update` mutations. Same pattern as `create`.

- [x] 1.5. Add a `terminate(self, entity_type: str, uuid: str, to_date: str,
      from_date: str | None = None) -> str` method that calls `{entity_type}_terminate`
      with `{"uuid": uuid, "validity": {"to": to_date, "from": from_date}}`.

- [x] 1.6. Add an `ensure_class_in_facet(self, facet: str, bvn: str, title: str,
      scope: str | None = None, owner: UUID | None = None) -> str` method that:
      - Queries existing classes in the facet via GraphQL
        (`query { classes(filter: {facets: [$facet]}) { objects { current { uuid user_key } } } }`)
      - If a matching `user_key` (bvn) is found, return its UUID
      - If not, calls `class_create` mutation with `ClassCreateInput` including
        `owner`, `facet_uuid` (looked up from the facet), `name`, `user_key`,
        `scope`, `published`, `validity`
      - Returns the UUID
      This replaces the current `MoraHelper.ensure_class_in_facet` which does a
      REST read + conditional REST create.

- [x] 1.7. The entity_type -> mutation mapping table should cover at minimum:
      `address`, `engagement`, `employee`, `org_unit`, `ituser`, `manager`, `class`.
      Each maps to three mutation constants (`_CREATE`, `_UPDATE`, `_TERMINATE`).

### Phase 2: Rewrite `integrations/opus/payloads.py`

This file (199 lines) constructs all REST payloads. Rewrite every function to return
a GraphQL input dict.

- [ ] 2.1. `create_user(employee, org_uuid, uuid=None)` -> return dict with:
      `given_name`, `surname`, `cpr_number`, `uuid` (if provided).
      Drop `org` field (derived from auth context).

- [ ] 2.2. `edit_engagement(data, mo_engagement_uuid)` -> return `{"uuid": mo_engagement_uuid, **data}`.
      Drop `type` and `data` wrapper.

- [ ] 2.3. `create_engagement(...)` -> return dict with:
      `org_unit` (bare UUID), `person` (bare UUID), `job_function` (bare UUID),
      `engagement_type` (bare UUID), `user_key`, `validity`.
      Drop `type`.

- [ ] 2.4. `create_org_unit(...)` -> return dict with:
      `uuid`, `user_key`, `name`, `parent` (bare UUID), `org_unit_type` (bare UUID), `validity`.

- [ ] 2.5. `edit_org_unit(...)` -> return `{"uuid": unit_uuid, "name": ..., "parent": parent_uuid,
      "org_unit_type": unit_type, "validity": ...}`. Drop `type` and `data` wrapper.

- [ ] 2.6. `terminate_detail(uuid, terminate_date, detail_type, terminate_from=None)` ->
      return `{"uuid": uuid, "validity": {"to": date_str, "from": from_str?}}`.
      The `detail_type` is no longer in the payload - it determines which `*_terminate`
      mutation to call.

- [ ] 2.7. `connect_it_system_to_user(...)` -> return dict with:
      `user_key`, `itsystem` (bare UUID), `person` (bare UUID), `validity`.
      Drop `type`.

- [ ] 2.8. `edit_it_system_username(uuid, username, from_date)` -> return
      `{"uuid": uuid, "user_key": username, "validity": {"from": from_date, "to": None}}`.
      Drop `type` and `data` wrapper.

- [ ] 2.9. `create_address(...)` -> return dict with:
      `value`, `address_type` (bare UUID), `validity`, `visibility` (bare UUID or None),
      `org_unit` or `person` (bare UUID, whichever is set).
      Drop `type`.

- [ ] 2.10. `edit_address(data, mo_address_uuid)` -> return `{"uuid": mo_address_uuid, **data}`.
       Drop `type` and `data` wrapper.

- [ ] 2.11. `create_manager(...)` -> return dict with:
       `user_key`, `org_unit` (bare UUID), `person` (bare UUID), `manager_type` (bare UUID),
       `manager_level` (bare UUID), `responsibility` (list of bare UUIDs), `validity`.
       Drop `type`.

- [ ] 2.12. `edit_manager(...)` -> return `{"uuid": object_uuid, "org_unit": ..., "person": ...,
       "manager_type": ..., "manager_level": ..., "responsibility": [...], "validity": ...}`.
       Drop `type` and `data` wrapper.

### Phase 3: Migrate `integrations/opus/opus_diff_import.py` call sites

- [ ] 3.1. Instantiate `GraphQLWriter` in `__init__`, reusing the existing
      `self.gql_client` (set up at line 177 via `_setup_gql_client()`). Pass it to
      `GraphQLWriter(gql_client=self.gql_client, dry_run=self.dry_run)`.

- [ ] 3.2. Replace `self.helper._mo_post("details/create", payload)` calls with
      `self.gql_writer.create(entity_type, payload)`:
      - Line 343: `_perform_address_update` create -> `create("address", payload)`
      - Line 477: `update_unit` create -> `create("org_unit", payload)`
      - Line 594: `create_engagement` -> `create("engagement", payload)`
      - Line 601: `create_user` -> `create("employee", payload)`
      - Line 645: `connect_it_system` create -> `create("ituser", payload)`
      - Line 764: `update_manager_status` create -> `create("manager", payload)`

- [ ] 3.3. Replace `self.helper._mo_post("details/edit", payload)` calls with
      `self.gql_writer.update(entity_type, payload)`:
      - Line 352: `_perform_address_update` edit -> `update("address", payload)`
      - Line 469: `update_unit` edit -> `update("org_unit", payload)`
      - Line 551: `update_engagement` edit -> `update("engagement", payload)`
      - Line 660: `connect_it_system` edit -> `update("ituser", payload)`
      - Line 757: `update_manager_status` edit -> `update("manager", payload)`

- [ ] 3.4. Replace `self.helper._mo_post("details/terminate", payload)` calls with
      `self.gql_writer.terminate(entity_type, uuid, to_date, from_date)`:
      - Line 858: `terminate_detail` -> `terminate(detail_type, uuid, end_date, terminate_from_date)`

- [ ] 3.5. Replace `self.helper.ensure_class_in_facet(...)` (9 call sites at lines
      364, 371, 423, 451, 486, 491, 697, 701, 702) with
      `self.gql_writer.ensure_class_in_facet(...)`. Note the current override at
      line 210-214 passes `owner=opus_helpers.find_opus_root_unit_uuid()` - preserve this.

- [ ] 3.6. Replace `create_user` return value handling: `r.json()` -> UUID string
      returned by `self.gql_writer.create("employee", payload)`.

- [ ] 3.7. Replace `_assert(response)` (line 235-243) with GraphQL error handling.
      The REST 400 "not give raise to a new registration" case needs testing against
      a live OS2mo - GraphQL may return this as a GraphQLError or handle it silently.

- [ ] 3.8. Replace `MOPostDryRun` class (line 140-150) with the `dry_run` flag on
      `GraphQLWriter`. Remove the `self.helper._mo_post = MOPostDryRun` monkeypatch
      (line 178-179). Remove the `MOPostDryRun` class entirely.

- [ ] 3.9. Replace `self.helper._mo_lookup(mo_uuid, "e/{}/details/address")` (line 288)
      with a GraphQL query for employee addresses. (This is a read - if deferring reads,
      leave a TODO and keep the REST call temporarily.)

- [ ] 3.10. Replace `self.helper._mo_lookup(employee_mo_uuid, url)` (line 673, manager query)
       with a GraphQL query. (Read - same as 3.9, can be deferred with a TODO.)

### Phase 4: Migrate `integrations/ad_integration/ad_sync.py` call sites

- [ ] 4.1. Add a `GraphQLWriter` instance to `AdMoSync`. The ad integration already
      constructs a `GraphQLClient` in `ad_writer.py:MOGraphqlSource._get_client()`
      (line 290-299) - reuse this pattern to set up a gql_client, then wrap it in
      `GraphQLWriter`. Alternatively, if `AdMoSync` has access to the existing
      `MOGraphqlSource` client, reuse that.

- [ ] 4.2. Replace `_mo_post("details/create", payload)` calls:
      - Line 316: `_create_address` -> `self.gql_writer.create("address", payload)`
      - Line 483: `_create_it_system` -> `self.gql_writer.create("ituser", payload)`

- [ ] 4.3. Replace `_mo_post("details/edit", payload)` calls:
      - Line 341: `_edit_address` -> `self.gql_writer.update("address", payload)`
      - Line 457: `_edit_engagement_post_to_mo` -> `self.gql_writer.update("engagement", payload)`
      - Line 494: `_update_it_system` -> `self.gql_writer.update("ituser", payload)`

- [ ] 4.4. Replace `_mo_post("details/terminate", payload)` calls:
      - Line 581: `_finalize_it_system` -> `self.gql_writer.terminate("ituser", uuid, today)`
      - Line 616: `_finalize_user_addresses_post_to_mo` -> `self.gql_writer.terminate("address", uuid, today)`

- [ ] 4.5. Replace `self.helper.update_user(employee["uuid"], user_attrs_changed)` (line 633)
      with `self.gql_writer.update("employee", {"uuid": employee["uuid"], **user_attrs_changed})`.

- [ ] 4.6. Replace inline payload construction in ad_sync.py with calls to the rewritten
      `payloads.py` functions where applicable, or construct GraphQL input dicts inline
      (ad_sync.py builds payloads inline, not via `payloads.py`). Apply the same
      transformation rules (drop `type`, unwrap `{"uuid": ...}`, etc.).

### Phase 5: Migrate `tools/move_ou.py`

- [ ] 5.1. **BLOCKED**: `OrganisationUnitUpdateInput` has no `clamp` or `force` field.
      `move_ou.py` uses `clamp: True` (clamp validity to today) and `?force=True`
      (override validation). These have no GraphQL equivalents.
      **Recommendation**: Leave `move_ou.py` on REST. Add a comment explaining why.
      File a feature request against OS2mo if GraphQL `clamp` support is needed.

### Phase 6: Update existing unit tests

No new tests. Update existing tests to assert on the new `GraphQLWriter` boundary.

- [ ] 6.1. `integrations/opus/tests/test_opus_diff_import.py`:
      - `OpusDiffImportTestbase` (line 27-44): replace `self.morahelper_mock._mo_post.return_value.status_code = 201`
        with mocking `self.gql_writer` (a `MagicMock`). `gql_writer.create.return_value`
        should return a UUID string, `gql_writer.terminate.return_value` returns None.
      - Remove `MOPostDryRun` import (line 20).
      - Update assertions: `diff.helper._mo_post.assert_called_with("details/create", {payload})`
        -> `diff.gql_writer.create.assert_called_with("address", {input})` (lines 173, 205, etc.)
      - `test_update_username` parameterized test (line 217-222): update `change_type` values
        from `"details/create"` / `"details/edit"` / `"details/terminate"` to
        `"create"` / `"update"` / `"terminate"` method names on `gql_writer`.

- [ ] 6.2. `integrations/ad_integration/tests/mocks.py`:
      - Replace or supplement `MockMoraHelper` (line 449) with a mock `GraphQLWriter`
        that records `create`/`update`/`terminate` calls.
      - Update mock `update_user` (line 428) to mock `gql_writer.update("employee", ...)`.

- [ ] 6.3. `integrations/ad_integration/tests/test_utils.py`:
      - Update mock `_mo_post` (line 417) to mock `gql_writer`.
      - Update mock `update_user` (line 428) to mock `gql_writer.update("employee", ...)`.

- [ ] 6.4. `integrations/ad_integration/tests/test_ad_sync.py`:
      - Update `MockMoraHelper` subclass (line 77) to record `gql_writer` calls instead
        of `update_user` calls.

- [ ] 6.5. Run all affected tests and verify they pass:
      ```bash
      pytest integrations/opus/tests/
      pytest integrations/ad_integration/tests/
      ```

- [ ] 6.6. Run lint and typecheck:
      ```bash
      ruff check . && ruff format --check .
      mypy . --ignore-missing-imports --strict-optional --explicit-package-bases --namespace-packages
      ```

### Phase 7: Manual verification

- [ ] 7.1. Test the opus import against a test OS2mo instance - verify that creates,
      edits, and terminates produce the same data as before.
- [ ] 7.2. Test the AD sync against a test OS2mo instance - same verification.
- [ ] 7.3. Test the `_assert()` 400 "not give raise to a new registration" case -
      verify how GraphQL handles a no-op edit (does it return an error or succeed silently?).
- [ ] 7.4. Test dry-run mode for both opus and ad integrations.

## Files changed summary

| File | Change |
|---|---|
| `integrations/gql_writer.py` | **NEW** - local GraphQL writer helper (~100-120 lines) |
| `integrations/opus/payloads.py` | Full rewrite (REST payloads -> GraphQL input dicts) |
| `integrations/opus/opus_diff_import.py` | ~15 call sites migrated to `GraphQLWriter` |
| `integrations/ad_integration/ad_sync.py` | ~8 call sites migrated to `GraphQLWriter` |
| `tools/move_ou.py` | No change (stays on REST, add comment) |
| `integrations/opus/tests/test_opus_diff_import.py` | Update mock boundary |
| `integrations/ad_integration/tests/mocks.py` | Update mock boundary |
| `integrations/ad_integration/tests/test_utils.py` | Update mock boundary |
| `integrations/ad_integration/tests/test_ad_sync.py` | Update mock boundary |

No changes to `os2mo_data_import` (external `MoraHelper` package).
No dependency bumps in `pyproject.toml`.
