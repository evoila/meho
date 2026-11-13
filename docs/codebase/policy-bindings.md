# Bindings: which login group is which team

The `bindings` section of a tenant's policy file
([policy.md](policy.md)) says which login group is which team. A binding
gives a group one of today's five group kinds (`Readers`, `Operators`,
`SA-Operators`, `Admins`, `Approvers`) and a set of categories
([policy-classification.md](policy-classification.md)). There are no new
roles.

Everything here is behind the switch `POLICY_APPROVER_ROUTING` (off by
default; Helm `config.policy.approverRouting`). With the switch off, MEHO
reads no binding and the login works as before. The file still accepts and
checks a `bindings` section with the switch off.

The bindings come in with the policy file, and every policy command
(`validate`, `diff`, `import`, `export`) needs the switch
`POLICY_CLASSIFICATION` too. So to use bindings, turn on both switches.

Code: `backend/src/meho_backplane/policy/bindings.py`.

## Overview

```yaml
bindings:
  - match: {group: /MEHO-Operators/Network}   # the group's full path
    kind: SA-Operators
    categories: [network]
  - match: {attribute: tenant-a-storage}         # a value of the group attribute
    kind: Operators
    categories: [storage]
  - match: {group: /MEHO-Bots}
    kind: Readers
    categories: ["*"]                         # every category
    principal: agent                          # default: person
```

A binding matches one group exactly, never by its short name. A short name
can match more than one group: a group with the same name in another
branch, a group synced from a company directory, or the same name in
another case.

- `group` is the full path. It must start with `/`. It matches an entry of
  the token's `groups` claim with exactly the same text, upper and lower
  case included. The Keycloak group-membership mapper must send full paths
  (`Full group path` on). A short name in the token never matches.
- `attribute` is a value of the Keycloak group attribute `meho_group`. A
  User Attribute mapper (Multivalued and Aggregate on) copies the values of
  all the person's groups into the token claim `meho_groups`. The match is
  exact.
- The `groups` claim must be a list of texts. A `groups` claim of another
  shape (one text, a map, a list with a number in it), or a `meho_groups`
  claim that is not a text or a list of texts, matches no binding at all,
  the same rule as for self-approval today: anything unclear gives no
  kind. MEHO logs `policy_bindings_malformed_groups_claim`.
- `kind` is one of the five group kinds.
- `categories` is a list of categories of the taxonomy, or `["*"]` for
  every category.
- `principal` is `person` (the default), `agent`, `service` or `runner`.
  A binding matches only a login of that kind.

## Key types

- `BindingEntry`: one binding, with `ref` (for example
  `tenant:bindings[2]` or `shipped:bindings[0]`), `kind`, `categories`,
  `group` or `attribute`, and `principal`.
- `BoundKindConflictError`: the bindings that match a login give two kinds.
- `Operator.bound_kind` and `Operator.bound_categories`
  (`auth/operator.py`): what the bindings gave the login in the current
  tenant. `None` and empty when the switch is off or no binding matched.

## Control flow

1. The token parser (`_operator_from_claims` in `auth/jwt.py`) reads no
   database. It builds the Operator as before.
2. `select_current_tenant` (`auth/current_tenant.py`) picks the current
   tenant.
3. Then `verify_jwt_for_audience` calls `bind_current_tenant`. REST
   (`verify_jwt`, `middleware.py`), MCP and the web session
   (`ui/auth/refresh.py`) all go through this one function.
4. `bind_current_tenant` does nothing while the switch is off or when no
   tenant is chosen (a status answer). Otherwise it reads the tenant's
   stored policy (`tenant_policy`, one row). The bindings of that file
   count. When the file has no bindings, or the tenant has no stored
   policy, the shipped bindings count, but only for a login without a
   tenant list (`meho_tenants`). The `groups` claim is not per tenant: it
   lists the person's groups of every tenant. So for a login with a
   tenant list, the list entry of the current tenant keeps deciding the
   kind, exactly as with the switch off.
5. `bind` matches the login:
   - No binding matches: the login keeps the kind MEHO works out today
     (from the token's `tenant_role`, `approver` and the self-approval
     group).
   - The bindings that match give two kinds (for example `Operators` and
     `SA-Operators`, or `Operators` and `Readers`): the login is refused
     with `401 multiple_bound_kinds`. A login gets one kind per tenant, as
     today. This check counts every binding that matches, also one of
     another base level. So a person in groups of two base levels (for
     example two subgroups, `/MEHO-Operators/Network` and
     `/MEHO-Readers/Storage`, both bound) is refused, whatever role the
     token carries.
   - One base level per login stays. Vault reads the `tenant_role` claim of
     the token, so a binding never changes the role. When the one kind
     that matches has another role than the token (for example `Admins`
     for an `operator` token), it is left out and logged as
     `policy_binding_other_base_level`. The login keeps today's kind.
   - Else the login gets the `approver` and `self_approver` flags of that
     kind (`group_grant` in `auth/operator.py`), `bound_kind` and the
     categories of all these bindings together. `"*"` means every
     category. A machine login never gets either flag. A token with a bad
     `meho_tenants` claim never gets `self_approver`.
   - The one exception to the two-kinds rule: when only shipped bindings
     match, and they give `Operators` and `SA-Operators` (a person in an
     operator group and in a renamed self-approval group), the login gets
     `SA-Operators`. Today such a person is an operator who may approve
     their own changes.
6. A stored `bindings` section that cannot be read (changed outside MEHO)
   gives the login the kind with the fewest rights of its base level
   (`Readers`, `Operators` or `Admins`), no category and no flag. MEHO logs
   `policy_bindings_unreadable`.

### The shipped bindings

`shipped_binding_documents` makes them from the two login settings:

- each group of `JWT_ROLE_GROUPS` gets the kind its name **ends with**
  (`MEHO-Operators` and `Corp-SA-Operators` give `Operators` and
  `SA-Operators`; upper and lower case do not count). A group whose name
  ends with no kind gets no shipped binding;
- the group of `JWT_SELF_APPROVER_GROUP` gets `SA-Operators`;
- each is matched by its top-level full path (`/MEHO-Operators`) and gets
  every category.

A site that renamed its groups must check that each name in
`JWT_ROLE_GROUPS` ends with the kind the group gives (for example
`Corp-Readers`, not `Corp-Read-Only`). Run `meho policy export` with both
switches on and read its `bindings`: each group must have the kind you
expect. A group with a name like `Corp-Read-Only` is missing there; write
its binding in the file yourself.

They count only for a login without a tenant list (step 4 above). The
shipped realm sends a tenant list, so there the shipped bindings change
nothing; they are for a site with the older mappers.

`meho policy export` writes them into a tenant's file when the tenant has
no stored bindings and the switch is on. After an import, the file holds
the bindings.

The import diff (and `meho policy diff`) compares the file with the
**stored** bindings, not with the shipped ones. So the first import of an
exported file lists the shipped bindings as `added`, although they were
already in effect. A file without `bindings` removes every stored binding,
and the shipped bindings are back in effect.

## The import checks

`binding_mistakes`, called from `check_document` (`policy/validate.py`):

- `bindings` in the install's file: "`bindings` belongs to a tenant's
  policy file. Each tenant binds its own login groups."
- An agent or a service with a kind that may approve (every kind but
  `Readers`): "`bindings[N]` gives an agent or a service a group kind that
  may approve. Only a person approves, with a click in the web interface or
  a fresh sign-in."
- A runner: "`bindings[N]`: a runner cannot get a role. A runner only runs
  what MEHO has already decided."
- A category that is not in the taxonomy.

The parser (`policy/document.py`) refuses a short name in `group`, a match
with both or neither of `group` and `attribute`, an unknown kind or login
kind, `"*"` next to other categories, and the same match twice.

A binding edit is a change of rights: bindings change only through the
policy import, and the import carries the rights-change mark, so another
admin must approve it ([policy.md](policy.md)). So two admins of the
tenant decide the stored bindings: one imports, a second one approves. The
import diff has a `bindings` section with `added`, `changed` and
`removed`.

A stored binding matches a group path or attribute value of the whole
Keycloak realm, not only the groups of its tenant. Who is in a group is
decided in Keycloak, by whoever manages that group. When a binding names a
group of another tenant (for example `/MEHO-lab-b-Operators/Network`),
the people in that group get the kind in this tenant too, if they work in
this tenant at all and have the base level of the kind here. The other
tenant's admins then decide, with their group changes, who gets the kind
here. So bind only groups that your tenant manages, and use paths and
attribute values with the tenant's name in them.

## Other places that read the kind

- A delegated agent run (`delegated_operator` in `auth/delegation.py`)
  clears `bound_kind` and `bound_categories`, next to the `approver` and
  self-approval flags.
- The audit rows of an approval (`_write_audit_row` in
  `operations/approval_queue.py`) carry `bound_kind` in their payload when
  the bindings gave the person who acted on the row a kind. On a decision
  row this says that the approver's right came from a binding, the way
  `tenant_role_source` says it for a platform admin.

## Dependencies

- `auth/operator.py`: `RoleGroup`, `group_grant`, `is_human_principal`.
- `policy/document.py`: the file models, `GROUP_KINDS`, `ALL_CATEGORIES`.
- `db/models.py`: `TenantPolicy`.

## Known issues

- With the switch on, every request reads one `tenant_policy` row after
  the tenant is chosen. There is no cache.
- No code reads `bound_categories` yet. Approver routing by category uses
  it in a later change.
- The shipped realm sends short group names (`Full group path` off). Then
  only `attribute` bindings match until a site turns full paths on.

## References

- `docs/decisions/2026-11-09-configurable-policy.md`, decision 2.
- `docs/decisions/login-role-groups.md`: the five groups and the mappers.
- Login setup: `docs-site/install/keycloak-realm.md` and
  `docs/cross-repo/keycloak-tenant-claims.md`.
- Tests: `backend/tests/test_policy_bindings.py`,
  `backend/tests/test_policy_bindings_parity.py` (the switch on without
  stored bindings changes no login) and
  `backend/tests/integration/test_keycloak_policy_bindings_e2e.py` (the
  claims of a real Keycloak with full paths and the `meho-groups` mapper).
