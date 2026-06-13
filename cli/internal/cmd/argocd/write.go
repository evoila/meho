// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package argocd

import (
	"encoding/json"
	"fmt"
	"io"
	"os"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// G3.12-T4 (#1405) — the approval-gated write verbs under
// `meho argocd ...`. Every write op registers requires_approval=True on the
// backplane, so a dispatch returns status=awaiting_approval until a human
// approves through the queue (G11.7-T1 #1401); the CLI surfaces that status
// verbatim. The verbs are the governed replacement for the consumer's
// hand-run `kubectl annotate application … argocd.argoproj.io/refresh=hard`
// and ad-hoc `argocd app sync/rollback/delete`.
//
// Verb tree (write half):
//   - argocd app sync     --name N [--revision R] [--prune] [--dry-run]  → argocd.app.sync
//   - argocd app rollback --name N --id I [--prune] [--dry-run]          → argocd.app.rollback
//   - argocd app set      --name N --spec-file F [--no-validate]         → argocd.app.set
//   - argocd app refresh  --name N [--no-hard]                           → argocd.app.refresh
//   - argocd app delete   --name N [--no-cascade] [--propagation-policy P] → argocd.app.delete
//   - argocd appproject create --project-file F [--upsert]               → argocd.appproject.create
//   - argocd appproject update --project-file F                          → argocd.appproject.update
//
// The Application spec / AppProject body is a JSON file (--spec-file /
// --project-file) so an operator can feed the same JSON they'd `kubectl
// apply`.

// loadJSONObject reads a JSON object from the file at path. Returns a
// structured error (mapped to exit code 4 / unexpected) when the file is
// unreadable or not a JSON object.
func loadJSONObject(path string) (map[string]any, *output.StructuredError) {
	raw, err := os.ReadFile(path) // #nosec G304 -- operator-supplied path, operator-only CLI
	if err != nil {
		return nil, output.Unexpected(fmt.Sprintf("read file %q: %v", path, err))
	}
	var obj map[string]any
	if err := json.Unmarshal(raw, &obj); err != nil {
		return nil, output.Unexpected(fmt.Sprintf("parse file %q as JSON object: %v", path, err))
	}
	return obj, nil
}

// writeResultKeyOrder pins a stable render order for the write
// confirmations' scalar fields so the output is diff-stable across ops.
var writeResultKeyOrder = []string{
	"name", "phase", "message", "synced_revision", "rollback_id",
	"refresh", "sync_status", "health_status", "created", "updated",
	"deleted", "cascade", "timed_out",
}

// printWriteResult is the shared pretty-printer for the write confirmations.
// It renders the op header then the flat result object's scalar fields
// (proposed_effect / before-after blocks are nested and only shown under
// --json). The awaiting_approval (parked) status never reaches this
// printer: the shared dispatch.Render intercepts it ahead of the
// pretty-printer and renders the parked hint itself (exit 0).
func printWriteResult(opID string) func(w io.Writer, r *CallResult) {
	return func(w io.Writer, r *CallResult) {
		fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, opID, r.Status, r.DurationMs)
		if r.Status != "ok" {
			printErrorTrailer(w, r)
			return
		}
		obj, err := decodeObject(r.Result)
		if err != nil || obj == nil {
			fallbackResultRender(w, r)
			return
		}
		for _, key := range writeResultKeyOrder {
			if v, ok := obj[key]; ok && v != nil {
				fmt.Fprintf(w, "  %-16s %v\n", key+":", v)
			}
		}
		// Surface the proposed_effect cascade count / before-after presence
		// as a one-liner so the operator knows the reviewer evidence exists
		// (the full block rides under --json).
		if eff, ok := obj["proposed_effect"].(map[string]any); ok {
			if cascade, ok := eff["cascade_resources"].([]any); ok {
				fmt.Fprintf(w, "  %-16s %d resource(s)\n", "cascade:", len(cascade))
			}
			if _, ok := eff["before_spec"]; ok {
				fmt.Fprintf(w, "  %-16s before/after captured (see --json)\n", "proposed_effect:")
			}
		}
	}
}

// dispatchWrite is the shared dispatch+render path for the write verbs.
func dispatchWrite(
	cmd *cobra.Command,
	opID, targetName string,
	params map[string]any,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, opID, r, jsonOut, printWriteResult(opID))
}

// writeFlags is the common flag bundle every write verb binds: --target,
// --json, --backplane. The per-verb commands add their own flags.
type writeFlags struct {
	targetName        string
	jsonOut           bool
	backplaneOverride string
}

func (f *writeFlags) bind(cmd *cobra.Command) {
	cmd.Flags().StringVar(&f.targetName, "target", "",
		"target slug to dispatch against (required)")
	cmd.Flags().BoolVar(&f.jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&f.backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from the most recent `meho login`)")
}

// renderLoadError renders a file-load StructuredError through the right
// channel (JSON or text). Pulled out so each verb's RunE stays a one-liner.
func renderLoadError(cmd *cobra.Command, se *output.StructuredError, jsonOut bool) error {
	return output.RenderError(cmd.ErrOrStderr(), se, jsonOut)
}

// --- argocd app sync ---------------------------------------------------

func newAppSyncCmd() *cobra.Command {
	var (
		f        writeFlags
		appName  string
		revision string
		prune    bool
		dryRun   bool
		timeout  int
	)
	cmd := &cobra.Command{
		Use:   "sync",
		Short: "Sync an ArgoCD Application and wait for a terminal phase (approval-gated)",
		Long: "sync dispatches argocd.app.sync (requires_approval=True): it reconciles\n" +
			"the app to its desired Git state and polls operationState until a\n" +
			"terminal phase (Succeeded/Failed/Error). The dispatch parks for human\n" +
			"approval before it touches the cluster.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho argocd app sync --target rdc-argocd --name guestbook --prune",
		Args:    cobra.NoArgs, SilenceUsage: true, SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			params := map[string]any{"name": appName}
			if revision != "" {
				params["revision"] = revision
			}
			if prune {
				params["prune"] = true
			}
			if dryRun {
				params["dry_run"] = true
			}
			if timeout > 0 {
				params["poll_timeout_seconds"] = timeout
			}
			return dispatchWrite(cmd, "argocd.app.sync", f.targetName, params, f.jsonOut, f.backplaneOverride)
		},
	}
	f.bind(cmd)
	cmd.Flags().StringVar(&appName, "name", "", "the Application's metadata.name (required)")
	cmd.Flags().StringVar(&revision, "revision", "", "Git revision to sync to (default: app target revision)")
	cmd.Flags().BoolVar(&prune, "prune", false, "delete resources no longer defined in Git")
	cmd.Flags().BoolVar(&dryRun, "dry-run", false, "render + validate without applying")
	cmd.Flags().IntVar(&timeout, "poll-timeout", 0, "seconds to poll operationState (default 300 backend-side)")
	mustRequire(cmd, "name")
	return cmd
}

// --- argocd app rollback -----------------------------------------------

func newAppRollbackCmd() *cobra.Command {
	var (
		f          writeFlags
		appName    string
		rollbackID int
		prune      bool
		dryRun     bool
		timeout    int
	)
	cmd := &cobra.Command{
		Use:   "rollback",
		Short: "Roll an ArgoCD Application back to a prior deployed revision (approval-gated)",
		Long: "rollback dispatches argocd.app.rollback (requires_approval=True) with the\n" +
			"int64 history id of a prior deployed revision (status.history[].id), then\n" +
			"polls operationState to a terminal phase. Parks for human approval first.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho argocd app rollback --target rdc-argocd --name guestbook --id 7",
		Args:    cobra.NoArgs, SilenceUsage: true, SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			params := map[string]any{"name": appName, "id": rollbackID}
			if prune {
				params["prune"] = true
			}
			if dryRun {
				params["dry_run"] = true
			}
			if timeout > 0 {
				params["poll_timeout_seconds"] = timeout
			}
			return dispatchWrite(cmd, "argocd.app.rollback", f.targetName, params, f.jsonOut, f.backplaneOverride)
		},
	}
	f.bind(cmd)
	cmd.Flags().StringVar(&appName, "name", "", "the Application's metadata.name (required)")
	cmd.Flags().IntVar(&rollbackID, "id", -1, "deployment history id to roll back to (required)")
	cmd.Flags().BoolVar(&prune, "prune", false, "delete resources no longer defined at that revision")
	cmd.Flags().BoolVar(&dryRun, "dry-run", false, "render + validate without applying")
	cmd.Flags().IntVar(&timeout, "poll-timeout", 0, "seconds to poll operationState (default 300 backend-side)")
	mustRequire(cmd, "name")
	mustRequire(cmd, "id")
	return cmd
}

// --- argocd app set ----------------------------------------------------

func newAppSetCmd() *cobra.Command {
	var (
		f          writeFlags
		appName    string
		specFile   string
		noValidate bool
	)
	cmd := &cobra.Command{
		Use:   "set",
		Short: "Update an ArgoCD Application's spec / target revision (approval-gated)",
		Long: "set dispatches argocd.app.set (requires_approval=True): it PUTs a new\n" +
			"ApplicationSpec read from --spec-file and captures the spec before/after\n" +
			"into proposed_effect for the reviewer. Parks for human approval first.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho argocd app set --target rdc-argocd --name guestbook --spec-file spec.json",
		Args:    cobra.NoArgs, SilenceUsage: true, SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			spec, se := loadJSONObject(specFile)
			if se != nil {
				return renderLoadError(cmd, se, f.jsonOut)
			}
			params := map[string]any{"name": appName, "spec": spec}
			if noValidate {
				params["validate"] = false
			}
			return dispatchWrite(cmd, "argocd.app.set", f.targetName, params, f.jsonOut, f.backplaneOverride)
		},
	}
	f.bind(cmd)
	cmd.Flags().StringVar(&appName, "name", "", "the Application's metadata.name (required)")
	cmd.Flags().StringVar(&specFile, "spec-file", "", "JSON file with the full ApplicationSpec (required)")
	cmd.Flags().BoolVar(&noValidate, "no-validate", false, "skip server-side spec validation")
	mustRequire(cmd, "name")
	mustRequire(cmd, "spec-file")
	return cmd
}

// --- argocd app refresh ------------------------------------------------

func newAppRefreshCmd() *cobra.Command {
	var (
		f       writeFlags
		appName string
		noHard  bool
	)
	cmd := &cobra.Command{
		Use:   "refresh",
		Short: "Force an immediate reconcile of an ArgoCD Application (approval-gated)",
		Long: "refresh dispatches argocd.app.refresh (requires_approval=True): the\n" +
			"governed replacement for `kubectl annotate application …\n" +
			"argocd.argoproj.io/refresh=hard`. CAUTION: under a selfHeal sync policy\n" +
			"a refresh that reveals drift can trigger an immediate auto-sync. Parks\n" +
			"for human approval first.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho argocd app refresh --target rdc-argocd --name guestbook",
		Args:    cobra.NoArgs, SilenceUsage: true, SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			params := map[string]any{"name": appName}
			if noHard {
				params["hard"] = false
			}
			return dispatchWrite(cmd, "argocd.app.refresh", f.targetName, params, f.jsonOut, f.backplaneOverride)
		},
	}
	f.bind(cmd)
	cmd.Flags().StringVar(&appName, "name", "", "the Application's metadata.name (required)")
	cmd.Flags().BoolVar(&noHard, "no-hard", false, "do a normal refresh instead of a hard refresh")
	mustRequire(cmd, "name")
	return cmd
}

// --- argocd app delete -------------------------------------------------

func newAppDeleteCmd() *cobra.Command {
	var (
		f           writeFlags
		appName     string
		noCascade   bool
		propagation string
	)
	cmd := &cobra.Command{
		Use:   "delete",
		Short: "Delete an ArgoCD Application with cascade (approval-gated)",
		Long: "delete dispatches argocd.app.delete (requires_approval=True): with cascade\n" +
			"(default) it also removes the app's managed cluster resources. The handler\n" +
			"snapshots that cascade list into proposed_effect for the reviewer. Parks\n" +
			"for human approval first.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho argocd app delete --target rdc-argocd --name guestbook",
		Args:    cobra.NoArgs, SilenceUsage: true, SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			params := map[string]any{"name": appName}
			if noCascade {
				params["cascade"] = false
			}
			if propagation != "" {
				params["propagation_policy"] = propagation
			}
			return dispatchWrite(cmd, "argocd.app.delete", f.targetName, params, f.jsonOut, f.backplaneOverride)
		},
	}
	f.bind(cmd)
	cmd.Flags().StringVar(&appName, "name", "", "the Application's metadata.name (required)")
	cmd.Flags().BoolVar(&noCascade, "no-cascade", false, "leave managed cluster resources orphaned")
	cmd.Flags().StringVar(&propagation, "propagation-policy", "", "deletion propagation policy (foreground|background|orphan)")
	mustRequire(cmd, "name")
	return cmd
}

// --- argocd appproject create ------------------------------------------

func newAppProjectCreateCmd() *cobra.Command {
	var (
		f           writeFlags
		projectFile string
		upsert      bool
	)
	cmd := &cobra.Command{
		Use:   "create",
		Short: "Create an ArgoCD AppProject (approval-gated)",
		Long: "create dispatches argocd.appproject.create (requires_approval=True): it\n" +
			"POSTs an AppProject read from --project-file. An AppProject is the\n" +
			"tenancy/authorization boundary for its Applications. Parks for human\n" +
			"approval first.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho argocd appproject create --target rdc-argocd --project-file proj.json --upsert",
		Args:    cobra.NoArgs, SilenceUsage: true, SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			project, se := loadJSONObject(projectFile)
			if se != nil {
				return renderLoadError(cmd, se, f.jsonOut)
			}
			params := map[string]any{"project": project}
			if upsert {
				params["upsert"] = true
			}
			return dispatchWrite(cmd, "argocd.appproject.create", f.targetName, params, f.jsonOut, f.backplaneOverride)
		},
	}
	f.bind(cmd)
	cmd.Flags().StringVar(&projectFile, "project-file", "", "JSON file with the AppProject object (required)")
	cmd.Flags().BoolVar(&upsert, "upsert", false, "update the project if it already exists")
	mustRequire(cmd, "project-file")
	return cmd
}

// --- argocd appproject update ------------------------------------------

func newAppProjectUpdateCmd() *cobra.Command {
	var (
		f           writeFlags
		projectFile string
	)
	cmd := &cobra.Command{
		Use:   "update",
		Short: "Update an ArgoCD AppProject (approval-gated)",
		Long: "update dispatches argocd.appproject.update (requires_approval=True): it\n" +
			"PUTs an AppProject read from --project-file and captures the project spec\n" +
			"before/after into proposed_effect. Parks for human approval first.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho argocd appproject update --target rdc-argocd --project-file proj.json",
		Args:    cobra.NoArgs, SilenceUsage: true, SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			project, se := loadJSONObject(projectFile)
			if se != nil {
				return renderLoadError(cmd, se, f.jsonOut)
			}
			params := map[string]any{"project": project}
			return dispatchWrite(cmd, "argocd.appproject.update", f.targetName, params, f.jsonOut, f.backplaneOverride)
		},
	}
	f.bind(cmd)
	cmd.Flags().StringVar(&projectFile, "project-file", "", "JSON file with the AppProject object (required)")
	mustRequire(cmd, "project-file")
	return cmd
}

// mustRequire marks a flag required and panics on the programmer error of
// naming a flag that was not defined (mirrors the per-verb inline pattern in
// app.go's read verbs).
func mustRequire(cmd *cobra.Command, name string) {
	if err := cmd.MarkFlagRequired(name); err != nil {
		panic(err)
	}
}
