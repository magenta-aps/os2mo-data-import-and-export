# OS2mo Data Import and Export (DIPEX)

DIPEX is a monorepo for integrations for [OS2mo](https://github.com/magenta-aps/os2mo).

- Integrations here are mostly legacy, they either have modern replacements or are planned
  to be replaced.
- The GraphQL schema is defined in the OS2mo repository
- `integrations` contains integrations
- `exporters` also contains integrations, but these export stuff
- `tools` contains random scripts, they are not necessarily still in use or up to date


## Testing

- There are only unit tests (except for `exporters/sql_export`), and they are not great,
  as they make heavy use of mocking, reference them with caution.
- You should test things manually against a running OS2mo instance, when unit
  testing is not adequate.
- Only write tests that don't require a lot of mocking, everything else should
  be tested manually.
- Each integration has its own tests, rather than a central test directory


## Commits

- Run pre-commit checks before committing (`.pre-commit-config.yaml`) 
- Use conventional commits for your commit messages
- Make as many commits as you deem appropriate
- You are not allowed to push commits, ask the user to do it for you
- The codebase is mostly legacy, so keep changes surgical rather than making large refactors,
  even if it means sometimes writing sub-optimal or non-idiomaitc code.

## Gotchas

- A checked-in virtualenv exists at the repo root. **Ignore `venv/` in all searches**
- `os2mo-admin` is a separate sub-project, treat it as such
- Each file hardcodes a GraphQL version with no explanation of which to use. OS2mo
  supports v21-v30. The schema for each version is at `/graphql/vXX/schema.graphql` on a running
  OS2mo instance. Infer what version to use from context.
- GraphQL `*_delete` mutations do **bitemporal deletion** (removes from all history), NOT
temporal termination. In almost all cases you want `*_terminate` (ends validity at
a date).
- Existing code is tightly coupled across integrations. This is bad, avoid it in new code.
- There are type errors in existing code, always check if an error was already
  present before you made a change
