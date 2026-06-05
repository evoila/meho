// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package cmd assembles the cobra command tree for the meho CLI.
// The root command exposes global flags consumed by every subcommand
// (--config, -v/--verbose); subcommand-specific behaviour lives in
// sibling files (version.go, login.go, status.go).
package cmd

import (
	"context"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/cmd/admin"
	"github.com/evoila/meho/cli/internal/cmd/agent"
	agentprincipal "github.com/evoila/meho/cli/internal/cmd/agent-principal"
	"github.com/evoila/meho/cli/internal/cmd/approvals"
	"github.com/evoila/meho/cli/internal/cmd/argocd"
	"github.com/evoila/meho/cli/internal/cmd/audit"
	"github.com/evoila/meho/cli/internal/cmd/bind9"
	"github.com/evoila/meho/cli/internal/cmd/broadcast"
	"github.com/evoila/meho/cli/internal/cmd/connector"
	"github.com/evoila/meho/cli/internal/cmd/conventions"
	"github.com/evoila/meho/cli/internal/cmd/docs"
	"github.com/evoila/meho/cli/internal/cmd/gcloud"
	"github.com/evoila/meho/cli/internal/cmd/harbor"
	hetznerrobot "github.com/evoila/meho/cli/internal/cmd/hetzner-robot"
	"github.com/evoila/meho/cli/internal/cmd/holodeck"
	"github.com/evoila/meho/cli/internal/cmd/k8s"
	"github.com/evoila/meho/cli/internal/cmd/kb"
	"github.com/evoila/meho/cli/internal/cmd/keycloak"
	"github.com/evoila/meho/cli/internal/cmd/memory"
	"github.com/evoila/meho/cli/internal/cmd/migrate"
	"github.com/evoila/meho/cli/internal/cmd/nsx"
	"github.com/evoila/meho/cli/internal/cmd/operation"
	"github.com/evoila/meho/cli/internal/cmd/pfsense"
	"github.com/evoila/meho/cli/internal/cmd/retrieval"
	"github.com/evoila/meho/cli/internal/cmd/runbook"
	"github.com/evoila/meho/cli/internal/cmd/scheduler"
	sddcmanager "github.com/evoila/meho/cli/internal/cmd/sddc-manager"
	"github.com/evoila/meho/cli/internal/cmd/targets"
	"github.com/evoila/meho/cli/internal/cmd/topology"
	"github.com/evoila/meho/cli/internal/cmd/vault"
	vcfautomation "github.com/evoila/meho/cli/internal/cmd/vcf-automation"
	vcffleet "github.com/evoila/meho/cli/internal/cmd/vcf-fleet"
	vcflogs "github.com/evoila/meho/cli/internal/cmd/vcf-logs"
	vcfoperations "github.com/evoila/meho/cli/internal/cmd/vcf-operations"
	"github.com/evoila/meho/cli/internal/cmd/vmware"
	"github.com/evoila/meho/cli/internal/discovery"
)

// Execute builds the command tree and runs it, returning any error
// produced by the executed subcommand. The caller is responsible for
// translating that error into a process exit code; cobra has already
// rendered the human-facing error message to stderr because the root
// command is configured with SilenceUsage = true (suppress the usage
// wall on RunE errors).
//
// The returned error may satisfy output.ExitCoder; main inspects it
// to pick the process exit code (auth_expired → 2, unreachable → 3,
// unexpected → 4, generic → 1).
func Execute() error {
	return newRootCmd().Execute()
}

// newRootCmd constructs a fresh root command. A constructor (rather
// than a package-level var) keeps the command tree free of mutable
// global state, which matters for tests: every test gets its own
// independent tree and can swap stdout/stderr via SetOut / SetErr.
func newRootCmd() *cobra.Command {
	root := &cobra.Command{
		Use:   "meho",
		Short: "Operator CLI for the MEHO governance backplane",
		Long: "meho is the operator-facing CLI for the MEHO governance " +
			"backplane. v0.1 ships login (G2.6-T2), version (G2.6-T1), and " +
			"status (G2.6-T3). Further operations are discovered from the " +
			"backplane at runtime — adding an operation to the backplane " +
			"doesn't require a new CLI binary release (see Goal #11 §5).",
		// SilenceUsage stops cobra from dumping the full usage block
		// when a RunE returns an error — operator-facing tooling
		// should surface a one-line failure, not a wall of help text.
		SilenceUsage: true,
		// SilenceErrors is left false so cobra still writes the error
		// message to stderr for non-status subcommands; status sets
		// SilenceErrors = true on itself to take over both the JSON
		// and human error rendering paths.
		SilenceErrors: false,
	}

	// Global flags. Bound to no destination yet — later tasks read
	// them via cmd.Flags().GetString / GetBool inside their RunE
	// functions, which keeps the root command free of subcommand
	// concerns and avoids global state.
	root.PersistentFlags().String(
		"config",
		"",
		"path to meho config file (default: $XDG_CONFIG_HOME/meho/config.json)",
	)
	root.PersistentFlags().BoolP(
		"verbose",
		"v",
		false,
		"enable verbose output",
	)

	root.AddCommand(newVersionCmd())
	root.AddCommand(newLoginCmd())
	root.AddCommand(newStatusCmd())

	// G4.3-T2 (#441) -- retrieval-quality + migration-decision tooling.
	// `meho retrieval eval` ships first; sibling verbs (usage T5b #464,
	// retire-checklist T6 #445) graft onto the same parent in their own
	// PRs.
	root.AddCommand(retrieval.NewRootCmd())

	// G0.6-T13 (#481) -- operation meta-tool surface for the G0.6
	// dispatcher substrate. `meho operation groups/search/call` wrap
	// the three /api/v1/operations/* routes shipped by G0.6-T8 (#399).
	// Registered before registerDynamicSubcommands so the backplane
	// manifest cannot shadow the built-in verb names.
	root.AddCommand(operation.NewRootCmd())

	// G0.7-T5 (#405) -- spec-ingestion + review workflow surface for
	// the G0.7 pipeline (Initiative #389). `meho connector
	// ingest/list/review/edit-group/edit-op/enable/disable` wrap the
	// seven /api/v1/connectors* routes shipped by G0.7-T6 (#406).
	// Registered before registerDynamicSubcommands so the backplane
	// manifest cannot shadow the built-in verb names.
	root.AddCommand(connector.NewRootCmd())

	// G0.3-T5 (#256) + G0.3-T6 (#257) -- targets registry verbs
	// (list / describe / probe / import) for Initiative #224. Wraps
	// the read + probe + create routes of /api/v1/targets/*.
	// Registered before registerDynamicSubcommands so the backplane
	// manifest cannot shadow the built-in `targets` parent.
	root.AddCommand(targets.NewRootCmd())

	// G8.1-T3 (#467) -- audit-query verbs (query / recent / show /
	// who-touched / my-recent) for Initiative #334. Wraps the four
	// /api/v1/audit/* routes shipped by G8.1-T2 (#466). Registered
	// before registerDynamicSubcommands so the backplane manifest
	// cannot shadow the built-in `audit` parent.
	root.AddCommand(audit.NewRootCmd())

	// G6.3-T4 (#381) -- broadcast-detail override management verbs
	// (overrides list / set / remove) for Initiative #376. Wraps the
	// three /api/v1/broadcast/overrides routes shipped by the same
	// task. tenant_admin-only; non-admin callers see 403. Registered
	// before registerDynamicSubcommands so the backplane manifest
	// cannot shadow the built-in `broadcast` parent.
	root.AddCommand(broadcast.NewRootCmd())

	// G4.1-T4 (#418) -- kb verbs (ingest / search / list / show /
	// add / delete) for Initiative #331. Wraps the five /api/v1/kb*
	// routes shipped by G4.1-T2 (#416) plus the /api/v1/retrieve
	// route for the search verb. Registered before
	// registerDynamicSubcommands so the backplane manifest cannot
	// shadow the built-in `kb` parent.
	root.AddCommand(kb.NewRootCmd())

	// G4.5-T5 (#1524) -- the `meho docs` tree for Initiative #1518
	// (the meho-docs add-on). One verb: `docs search` wraps the
	// /api/v1/search_docs route (T3, #1521) for federated
	// vendor-document retrieval. The tree compiles into every binary
	// but is gated on the tenant-provisioned `meho-docs` capability
	// (T1, #1519): `docs.NewRootCmd` reads the capability from the
	// stored token's JWT claim and, when absent, marks the parent
	// Hidden and makes every verb refuse with a typed
	// `addon_not_provisioned` error — true absence for an
	// unprovisioned tenant. Registered before
	// registerDynamicSubcommands so the backplane manifest cannot
	// shadow the built-in `docs` parent.
	root.AddCommand(docs.NewRootCmd())

	// G12.5-T1 (#1318) -- runbook template authoring verbs
	// (list-templates / show-template / draft-template / edit-template /
	// publish-template / deprecate-template) for Initiative #1200.
	// Wraps the six /api/v1/runbooks/templates routes shipped by
	// G12.2-T3 (#1297). T1 ships the chassis + template verbs; T2
	// (#1319) extends with the five run verbs (start / next / abort /
	// reassign / runs). Read verbs (list-templates) are operator-level;
	// show-template is tenant_admin with the post-completion carve-out
	// (#1309); write verbs (draft/edit/publish/deprecate) require
	// tenant_admin. Registered before registerDynamicSubcommands so
	// the backplane manifest cannot shadow the built-in `runbook`
	// parent.
	root.AddCommand(runbook.NewRootCmd())

	// G7.1-T3 (#315) -- conventions verbs (list / show / create / edit /
	// delete / history) for Initiative #229 (tenant conventions). Wraps
	// the six /api/v1/conventions routes shipped by G7.1-T2 (#314).
	// Read verbs are operator-level; write verbs require tenant_admin.
	// The `edit` verb supports two modes — flag-driven PATCH (scripting)
	// and $EDITOR interactive (operator conversational edit). Registered
	// before registerDynamicSubcommands so the backplane manifest cannot
	// shadow the built-in `conventions` parent.
	root.AddCommand(conventions.NewRootCmd())

	// G5.1-T4 (#424) -- top-level memory verbs (remember / recall /
	// forget / list) for Initiative #332. Each verb is registered
	// onto the root directly (not under a `memory` parent) per the
	// consumer-needs.md §G5 ergonomic shape (`meho remember "..."`
	// rather than `meho memory remember "..."`). Wraps the four
	// /api/v1/memory* routes shipped by G5.1-T2 (#422) plus the
	// /api/v1/retrieve route for the `recall --query` retrieval form.
	// Registered before registerDynamicSubcommands so the backplane
	// manifest cannot shadow the built-in verbs.
	//
	// G5.2-T5 (#627) -- top-level `meho promote` verb for Initiative
	// #374. Wraps POST /api/v1/memory/{scope}/{slug}/promote shipped
	// by G5.2-T4 (#626). Registered alongside the G5.1 verbs because
	// the consumer-needs.md §G5 ergonomic shape applies (`meho promote
	// user/foo --to user-tenant`, not `meho memory promote ...`).
	root.AddCommand(memory.NewRememberCmd())
	root.AddCommand(memory.NewRecallCmd())
	root.AddCommand(memory.NewForgetCmd())
	root.AddCommand(memory.NewListCmd())
	root.AddCommand(memory.NewPromoteCmd())

	// G11.1-T2 (#809) -- agent-definition CRUD verbs (list / show /
	// create / edit / delete) for Initiative #802. Wraps the five
	// /api/v1/agents routes shipped by this Task. Read verbs are
	// operator-level; write verbs require tenant_admin. Registered
	// before registerDynamicSubcommands so the backplane manifest
	// cannot shadow the built-in `agent` parent.
	root.AddCommand(agent.NewRootCmd())

	// G11.2-T1 (#815) -- agent-principal lifecycle verbs (list /
	// register / revoke) for Initiative #803. Wraps the three
	// /api/v1/agent-principals routes. Creates a Keycloak client
	// tagged kind=agent and a DB row on register; revoke disables
	// the Keycloak client (kill switch). Read verbs are
	// operator-level; write verbs require tenant_admin. Registered
	// before registerDynamicSubcommands so the backplane manifest
	// cannot shadow the built-in `agent-principal` parent.
	root.AddCommand(agentprincipal.NewRootCmd())

	// G11.2-T5 (#818) -- approval surfacing channel verbs (list / show /
	// approve / reject) for Initiative #803. Wraps the merged T4/T5 REST
	// surface (/api/v1/approvals routes). Both read and write verbs
	// require the operator role minimum; tenant scoping is enforced
	// server-side. Registered before registerDynamicSubcommands so the
	// backplane manifest cannot shadow the built-in `approvals` parent.
	root.AddCommand(approvals.NewRootCmd())

	// G11.3-T5 (#826) -- scheduled-trigger admin verbs (list / create /
	// cancel) for Initiative #804. Wraps the T5 REST surface
	// (/api/v1/scheduler/triggers routes). list is operator-level; create
	// and cancel require tenant_admin. Tenant scoping is enforced
	// server-side via the JWT; tenant_admin callers may use --tenant to
	// act cross-tenant. Registered before registerDynamicSubcommands so
	// the backplane manifest cannot shadow the built-in `scheduler` parent.
	root.AddCommand(scheduler.NewRootCmd())

	// G3.1-T7 (#511) -- vmware-rest-9.0 operator alias verbs for
	// Initiative #227. The verb tree pre-bakes connector_id=
	// "vmware-rest-9.0" on top of the existing /api/v1/operations/call
	// dispatcher route so operators don't type the connector ID on
	// every invocation. PR-1 ships raw-REST verbs (about, vm list/info,
	// host list, cluster list, datacenter/datastore/network list,
	// operation search/call); composite-backed verbs (vm create, host
	// evacuate, cluster patch) become end-to-end dispatchable once
	// G3.1-T5 (#508) + G3.1-T6 (#509) register the underlying
	// composite ops. Registered before registerDynamicSubcommands so
	// the backplane manifest cannot shadow the built-in `vmware`
	// parent.
	root.AddCommand(vmware.NewRootCmd())

	// G3.5-T3 (#615) -- nsx-rest-4.2 operator alias verbs for
	// Initiative #368. The verb tree pre-bakes connector_id=
	// "nsx-rest-4.2" on top of the existing /api/v1/operations/call
	// dispatcher route. Ships the 9 read-only NSX core verbs (about,
	// node list, cluster status, segment list, transport-zone list,
	// tier0 list, tier1 list, firewall policy list, firewall rule list)
	// plus operation search/call meta-tool wrappers.
	// Registered before registerDynamicSubcommands so the backplane
	// manifest cannot shadow the built-in `nsx` parent.
	root.AddCommand(nsx.NewRootCmd())

	// G3.6-T6 (#838) -- vrli-rest-9.0 operator alias verbs for
	// Initiative #369 (G3.6 tier-3 VCF management plane). The verb tree
	// pre-bakes connector_id="vrli-rest-9.0" on top of the existing
	// /api/v1/operations/call dispatcher route so operators don't type
	// the connector ID on every invocation. Ships the 7 curated read-only
	// vRLI core verbs (about, query, aggregated, field list, host list,
	// content-pack list, alert list) plus operation search/call
	// meta-tool wrappers. `meho vcf-logs query --time-range 1h
	// --target rdc-vrli` replaces the consumer's `./scripts/vcf-logs.sh`
	// wrapper. Registered before registerDynamicSubcommands so the
	// backplane manifest cannot shadow the built-in `vcf-logs` parent.
	root.AddCommand(vcflogs.NewRootCmd())

	// G3.5-T10 (#622) -- harbor-rest-2.x operator alias verbs for
	// Initiative #368. The verb tree pre-bakes connector_id=
	// "harbor-rest-2.x" on top of the existing /api/v1/operations/call
	// dispatcher route. Ships the 9 read-only Harbor core verbs (about,
	// health, project list/info, repository list/info, artifact list/info,
	// robot list) plus robot create/delete typed ops and operation
	// search/call meta-tool wrappers.
	// Registered before registerDynamicSubcommands so the backplane
	// manifest cannot shadow the built-in `harbor` parent.
	root.AddCommand(harbor.NewRootCmd())

	// G3.5-T6 (#618) -- sddc-rest-9.0 operator alias verbs for
	// Initiative #368. The verb tree pre-bakes connector_id=
	// "sddc-rest-9.0" on top of the existing /api/v1/operations/call
	// dispatcher route. Ships the 9 read-only SDDC Manager core verbs
	// (about, manager list, domain list/info, cluster list, host list,
	// network-pool list, bundle list, workflow list) plus operation
	// search/call meta-tool wrappers. Replaces ./scripts/sddc-manager.sh.
	// Registered before registerDynamicSubcommands so the backplane
	// manifest cannot shadow the built-in `sddc-manager` parent.
	root.AddCommand(sddcmanager.NewRootCmd())

	// G3.6-T3 (#837) -- vrops-rest-9.0 operator alias verbs for
	// Initiative #369. The verb tree pre-bakes connector_id=
	// "vrops-rest-9.0" on top of the existing /api/v1/operations/call
	// dispatcher route. Ships the 8 read-only vROps core verbs (about,
	// resource list/get, alert list, alertdefinition list, symptom
	// list, recommendation list, supermetric list) plus operation
	// search/call meta-tool wrappers. Replaces ./scripts/vcf-operations.sh.
	// Registered before registerDynamicSubcommands so the backplane
	// manifest cannot shadow the built-in `vcf-operations` parent.
	root.AddCommand(vcfoperations.NewRootCmd())

	// G3.3-T6 (#550) -- vault-1.x operator alias verbs for Initiative
	// #366. The verb tree pre-bakes connector_id="vault-1.x" on top of
	// the existing /api/v1/operations/call dispatcher route so operators
	// don't type the connector ID on every invocation. Ships the KV-v2
	// (read/list/put/versions/delete), sys (health/seal-status/mounts-
	// list/auth-list), and auth (userpass/approle list+read) verbs over
	// the typed ops registered by G3.3-T1/T2/T3 (#545/#546/#547).
	// `meho vault kv read --target rdc-vault secret <path>` replaces the
	// consumer's `_secret-read.sh` wrapper. Registered before
	// registerDynamicSubcommands so the backplane manifest cannot shadow
	// the built-in `vault` parent.
	root.AddCommand(vault.NewRootCmd())

	// G9.1-T6 (#454) -- topology graph verbs (refresh / dependents /
	// dependencies / path) for Initiative #363. Thin cobra wrappers
	// over the four /api/v1/topology* routes shipped by G9.1-T5
	// (#453); the fifth T6 verb, `meho targets discover`, lives on
	// the targets parent next to the other /api/v1/targets routes.
	// Registered before registerDynamicSubcommands so the backplane
	// manifest cannot shadow the built-in `topology` parent.
	root.AddCommand(topology.NewRootCmd())

	// G3.2-T6 (#326) -- k8s-1.x operator alias verbs for Initiative
	// #320. The verb tree pre-bakes connector_id="k8s-1.x" on top of
	// the existing /api/v1/operations/call dispatcher route so operators
	// don't type the connector ID on every invocation. Ships the 14
	// read-only ops (about/ls + namespace/node list + pod/deployment
	// list+info + service/ingress/configmap list + configmap info +
	// event list + logs) registered by G3.2-T1..T5 (#321/#322/#323/
	// #324/#325). `meho k8s pod list --target rke2-meho --namespace
	// argocd` replaces the consumer's `kubectl-vcf.sh -n argocd get
	// pods` wrapper. Registered before registerDynamicSubcommands so
	// the backplane manifest cannot shadow the built-in `k8s` parent.
	root.AddCommand(k8s.NewRootCmd())

	// G3.6-T9 (#839) -- fleet-rest-9.0 operator alias verbs for Initiative
	// #369. The verb tree pre-bakes connector_id="fleet-rest-9.0" on top of
	// the existing /api/v1/operations/call dispatcher route so operators
	// don't type the connector ID on every invocation. Ships the 8 read-only
	// Fleet core verbs (about, datacenter list, vcenter list, environment
	// list/info, product list, request list/info) plus operation search/call
	// meta-tool wrappers. Replaces ./scripts/vcf-fleet.sh.
	// Registered before registerDynamicSubcommands so the backplane manifest
	// cannot shadow the built-in `vcf-fleet` parent.
	root.AddCommand(vcffleet.NewRootCmd())

	// G3.4-T5 (#591) -- bind9-ssh-9.x operator alias verbs for Initiative
	// #367. The verb tree pre-bakes connector_id="bind9-ssh-9.x" on top
	// of the existing /api/v1/operations/call dispatcher route so
	// operators don't type the connector ID on every invocation. Ships
	// the 11 ops (about, zone list/read, record get/add/remove, config
	// show/apply-views/apply-file/backup/reload) registered by G3.4-
	// T1..T4 (#587/#588/#589/#590). `meho bind9 record add
	// esx-dc6.evba.lab 10.5.50.25 --zone evba.lab --target
	// vcf-router-bind9` replaces the consumer's
	// `bind9-dns.sh --add-a-record` wrapper (the 2026-05-04 / 2026-05-05
	// credential-leak surface — evoila-bosnia/claude-rdc-hetzner-dc#86).
	// Registered before registerDynamicSubcommands so the backplane
	// manifest cannot shadow the built-in `bind9` parent.
	root.AddCommand(bind9.NewRootCmd())

	// G3.7-T3 (#850) -- pfsense-ssh-2.7 operator alias verbs for
	// Initiative #370. The verb tree pre-bakes connector_id=
	// "pfsense-ssh-2.7" on top of the existing /api/v1/operations/call
	// dispatcher route so operators don't type the connector ID on every
	// invocation. Ships 8 read-only ops (about, version, firewall
	// rules/state, nat rules, network interface/gateway, config show)
	// registered by G3.7-T1..T2 (#844/#847). Replaces the consumer's
	// `scripts/pfsense.sh` wrapper (see docs/cross-repo/pfsense-
	// onboarding.md for the migration recipe). Registered before
	// registerDynamicSubcommands so the backplane manifest cannot
	// shadow the built-in `pfsense` parent.
	root.AddCommand(pfsense.NewRootCmd())

	// G3.7-T6 (#851) -- gcloud-rest-1.0 operator alias verbs for
	// Initiative #370. The verb tree pre-bakes connector_id=
	// "gcloud-rest-1.0" on top of the existing /api/v1/operations/call
	// dispatcher route so operators don't type the connector ID on
	// every invocation. Auth uses GCP Application Default Credentials
	// + Service Account Impersonation; SA JSON key material in any
	// target secret_ref is refused by the backend (org policy
	// constraints/iam.disableServiceAccountKeyCreation). Ships the 8
	// read-only gcloud ops (about, project describe, services list, iam
	// sa list, iam policy read, compute instances/networks/subnets
	// list). Replaces ./scripts/gcloud.sh for the read-only surface.
	// Registered before registerDynamicSubcommands so the backplane
	// manifest cannot shadow the built-in `gcloud` parent.
	root.AddCommand(gcloud.NewRootCmd())

	// G3.13-T3 (#1395) -- keycloak-admin-26.x operator alias verbs for
	// Initiative #1388. The verb tree pre-bakes connector_id=
	// "keycloak-admin-26.x" on top of the existing /api/v1/operations/call
	// dispatcher route so operators don't type the connector ID on every
	// invocation. The connector authenticates to the Keycloak Admin REST
	// API with a Vault-sourced admin credential (the admin-vs-operator
	// split — see docs/cross-repo/keycloak-onboarding.md), distinct from
	// the operator's OIDC token. Ships the 6 read-only ops (realm get,
	// client list/get, client-scope list, user list, role-mapping get)
	// registered by G3.13-T1..T2 (#1393/#1394); the write surface is the
	// deferred approval-gated T4 follow-up (#1406). Distinct from the
	// `admin keycloak ...` deployer-onramp subtree (#791). Registered
	// before registerDynamicSubcommands so the backplane manifest cannot
	// shadow the built-in `keycloak` parent.
	root.AddCommand(keycloak.NewRootCmd())

	// G3.12-T3 (#1392) -- argocd-api-3.x operator alias verbs for
	// Initiative #1387. The verb tree pre-bakes connector_id=
	// "argocd-api-3.x" on top of the existing /api/v1/operations/call
	// dispatcher route so operators don't type the connector ID on every
	// invocation. The connector authenticates to the ArgoCD server REST
	// API with a Vault-sourced bearer token (the operator's OIDC token is
	// never forwarded to ArgoCD — see docs/cross-repo/argocd-onboarding.md).
	// Ships the 6 read-only ops (app list/get/diff/resource-tree,
	// appproject list, repo list) registered by G3.12-T2 (#1442); the
	// write surface (sync / refresh) is a deferred approval-gated
	// follow-up. Registered before registerDynamicSubcommands so the
	// backplane manifest cannot shadow the built-in `argocd` parent.
	root.AddCommand(argocd.NewRootCmd())

	// G3.7-T9 (#852) -- hetzner-rest-2026.04 operator alias verbs for
	// Initiative #370. The verb tree pre-bakes connector_id=
	// "hetzner-rest-2026.04" on top of the existing /api/v1/operations/call
	// dispatcher route. Hetzner Robot is a generic-ingested connector with
	// HTTP Basic auth (Webservice user, distinct from Robot portal login).
	// Ships 10 read-only verbs (about, server list/info, ip list, subnet
	// list, vswitch list/info, failover list, rdns list, ssh-key list) plus
	// operation search/call meta-tool wrappers. WARNING: Hetzner Robot blocks
	// the source IP for 10 minutes after 3 consecutive 401 responses — the
	// connector raises auth_failed on the FIRST 401 and never retries.
	// Registered before registerDynamicSubcommands so the backplane manifest
	// cannot shadow the built-in `hetzner-robot` parent.
	root.AddCommand(hetznerrobot.NewRootCmd())

	// G3.8-T3 (#855) -- holodeck-ssh-9.0 operator alias verbs for
	// Initiative #371 (G3.8 Holodeck typed-SSH connector — closes the G3
	// wrapper-retirement story per Goal #214). The verb tree pre-bakes
	// connector_id="holodeck-ssh-9.0" on top of the existing
	// /api/v1/operations/call dispatcher route so operators don't type
	// the connector ID on every invocation. Ships the 8 read-only ops
	// (about, config show, pod list/info, service list, k8s exec
	// (read-only), logs tail, networking show) registered by G3.8-T1/T2
	// (#853/#854). `meho holodeck pod list --target holorouter` replaces
	// the consumer's `holodeck.sh --target holorouter 'pwsh -c
	// "Get-HoloDeckPod | Format-Table"'` invocation 1:1. The sister
	// `clone-holodeck-instance.sh` wrapper (multi-step nested-lab
	// bring-up) stays in the wrapper for v0.2 and surfaces as a Runbook
	// in a future Goal G11 per docs/cross-repo/holodeck-onboarding.md.
	// IMPORTANT: the holodeck.k8s.exec CLI verb forwards the
	// operator-supplied kubectl command verbatim; the read-only safelist
	// + shell-metacharacter guard live on the backend handler
	// (parse_kubectl_command). Duplicating the gate client-side would
	// risk drift with the authoritative backend gate.
	// Registered before registerDynamicSubcommands so the backplane
	// manifest cannot shadow the built-in `holodeck` parent.
	root.AddCommand(holodeck.NewRootCmd())

	// G3.6-T12 (#840) -- vcfa-rest-9.0 operator alias verbs for
	// Initiative #369. The verb tree pre-bakes connector_id=
	// "vcfa-rest-9.0" on top of the existing /api/v1/operations/call
	// dispatcher route. VCFA 9.x is **dual-plane** (provider /cloudapi/*
	// + tenant /iaas/api/*) on one appliance; the persistent --plane
	// flag picks the op namespace and the backend dispatcher routes
	// each call to the correct auth plane via the descriptor's
	// spec_source tag (G3.6-T11 #836). The persistent --fqdn flag is
	// the per-call vhost override the appliance's strict Host: routing
	// requires when reached by IP (without it every path returns 404
	// with empty body). Ships 11 read-only verbs (6 provider + 5
	// tenant) plus operation search/call meta-tool wrappers; replaces
	// ./scripts/vcf-automation.sh for the read-only surface.
	// Registered before registerDynamicSubcommands so the backplane
	// manifest cannot shadow the built-in `vcf-automation` parent.
	root.AddCommand(vcfautomation.NewRootCmd())

	// G5.3-T1 (#608) -- laptop-local memory migration verb tree for
	// Initiative #375. v0.1 ships the skeleton (migrate parent +
	// memory subcommand with flags); flow logic (scanner, huh picker,
	// submission, mark-migrated) lands in T2–T5 (#609–#612). Registered
	// before registerDynamicSubcommands so the backplane manifest cannot
	// shadow the built-in `migrate` parent.
	root.AddCommand(migrate.NewRootCmd())

	// G0.9.1-T11 (#791) -- install-time provisioning verbs for the
	// MEHO deployer onramp. The first verb (`admin keycloak
	// bootstrap-clients`) idempotently creates the public CLI
	// device-code + MCP browser-flow clients, their 5 protocol
	// mappers + 4 default client scopes, the meho-admins group, and
	// an admin user against a Keycloak realm — encoding the 5-step
	// recipe documented in deploy/values-examples/README.md.
	// Confidential-client provisioning is explicitly refused; this
	// verb is for PUBLIC clients only. Registered before
	// registerDynamicSubcommands so the backplane manifest cannot
	// shadow the built-in `admin` parent.
	root.AddCommand(admin.NewRootCmd())

	// Server-driven subcommand discovery (Goal #11 §5). Fetched
	// best-effort on startup so the operator's `meho --help` lists
	// the full set of operations the backplane advertises. v0.1
	// backplanes return an empty manifest — the scaffold runs but
	// produces no extra commands. v0.2+ operations land here
	// without a CLI binary release.
	//
	// The fetch is silent on every failure path: a missing endpoint
	// (404 before G2.2 ships it), an offline operator, or a
	// misconfigured backplane all degrade to "no extra commands"
	// rather than blocking the entire CLI. The configured
	// backplane URL comes from the same config.json `meho login`
	// writes — operators with no login persist no URL and skip the
	// discovery fetch entirely.
	registerDynamicSubcommands(root)

	return root
}

// registerDynamicSubcommands runs the discovery fetch and grafts
// any returned commands onto rootCmd. Splits out as a named
// function for two reasons:
//
//  1. Tests can swap the function via setDynamicRegistrar (below)
//     to control startup-time behaviour without touching network.
//  2. Errors during dynamic registration (a collision with a
//     built-in subcommand name) print a warning to stderr but
//     never abort startup — the operator still gets to run the
//     local subcommands.
//
// The fetch budget is bounded by discovery.fetchTimeout so a hung
// backplane TCP connection can't block a `meho version` invocation.
func registerDynamicSubcommands(root *cobra.Command) {
	if dynamicRegistrar != nil {
		dynamicRegistrar(root)
		return
	}
	cfg, err := auth.LoadConfig()
	if err != nil || cfg.BackplaneURL == "" {
		// No login yet, or the operator removed the config file —
		// nothing to discover against. Silent: the local-only
		// command set is fully usable.
		return
	}

	// background ctx is fine here: the fetch's own context has the
	// discovery.fetchTimeout cap applied internally. Using
	// cobra.Command.Context() would be nicer but cobra hasn't
	// constructed it yet at command-tree-build time.
	manifest, err := discovery.Fetch(context.Background(), http.DefaultClient, cfg.BackplaneURL)
	if err != nil {
		// Decoding failures (the only error class Fetch returns)
		// are surfaced as a stderr warning but never abort.
		root.PrintErrf("warning: dynamic subcommand discovery failed: %v\n", err)
		return
	}
	if err := discovery.Register(root, manifest); err != nil {
		root.PrintErrf("warning: dynamic subcommand registration: %v\n", err)
	}
}

// dynamicRegistrar overrides registerDynamicSubcommands in tests so
// unit tests can deterministically register synthetic manifests
// without standing up a real backplane HTTP server. nil in
// production.
var dynamicRegistrar func(*cobra.Command)

// setDynamicRegistrar is the test-only seam onto dynamicRegistrar.
// Returns a cleanup function so tests can restore the production
// (nil) value on teardown.
//
// Exposed at package scope (lowercase) so root_test.go can use it
// without exporting the underlying var. Callers must use the
// returned cleanup to avoid contaminating sibling tests.
func setDynamicRegistrar(fn func(*cobra.Command)) func() {
	prev := dynamicRegistrar
	dynamicRegistrar = fn
	return func() { dynamicRegistrar = prev }
}
