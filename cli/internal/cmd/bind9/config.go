// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package bind9

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newConfigCmd returns the `meho bind9 config` parent command and
// assembles its five verbs (show / apply-views / apply-file / backup
// / reload). The parent itself takes no args and prints its own help.
func newConfigCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "config",
		Short:        "bind9 config verbs (show / apply-views / apply-file / backup / reload)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newConfigShowCmd())
	cmd.AddCommand(newConfigApplyViewsCmd())
	cmd.AddCommand(newConfigApplyFileCmd())
	cmd.AddCommand(newConfigBackupCmd())
	cmd.AddCommand(newConfigReloadCmd())
	return cmd
}

// newConfigShowCmd returns `meho bind9 config show <file>`. Maps to
// op_id `bind9.config.show`. <file> may be absolute (lexically under
// the bind config root) or relative (resolved against the same root).
// Traversal / outside-root inputs are rejected pre-stage.
func newConfigShowCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "show <file>",
		Short: "Read a bind9 config file from the target",
		Long: "show dispatches bind9.config.show against the connector_id=\n" +
			"\"bind9-ssh-9.x\" connector. <file> may be absolute (must be\n" +
			"lexically under the bind config root, e.g.\n" +
			"`/etc/bind/named.conf`) or relative (resolved against the\n" +
			"same root, e.g. `named.conf` or `views/external.conf`).\n" +
			"Traversal paths and absolute paths outside the root are\n" +
			"rejected pre-stage with no file content leak.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho bind9 config show named.conf --target vcf-router-bind9\n" +
			"  meho bind9 config show /etc/bind/named.conf.local --target vcf-router-bind9 --json",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runConfigShow(cmd, args[0], targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runConfigShow(cmd *cobra.Command, path, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	params := map[string]any{"path": path}
	r, err := conn.Call(cmd.Context(), backplaneURL, "bind9.config.show", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "bind9.config.show", r, jsonOut, printConfigShow)
}

// printConfigShow renders `{"file": <abs-path>, "content": <text>}`.
// Surfaces the resolved path then the file content verbatim.
func printConfigShow(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s bind9.config.show — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	body, err := decodeFlatResult(r.Result)
	if err != nil || body == nil {
		fallbackResultRender(w, r)
		return
	}
	if file, ok := body["file"].(string); ok && file != "" {
		fmt.Fprintf(w, "  file: %s\n", file)
	}
	content, _ := body["content"].(string)
	if content != "" {
		fmt.Fprintln(w, "  --- content ---")
		fmt.Fprintln(w, content)
	}
}

// newConfigApplyViewsCmd returns `meho bind9 config apply-views
// <local-views.conf> <zones-dir>`. Maps to op_id
// `bind9.config.apply_views`. Stages a multi-file tree:
//   - The named.conf-shaped `<local-views.conf>` lands at
//     `named.conf.local` (the canonical views include).
//   - Every regular file under `<zones-dir>` lands under the same
//     relative subpath in `/etc/bind/` (e.g. a local
//     `<zones-dir>/db.evba.lab` lands at `/etc/bind/db.evba.lab`).
//
// The atomic-apply primitive overlays the live tree; the snapshot-
// rollback contract leaves /etc/bind/ byte-identical on failure.
func newConfigApplyViewsCmd() *cobra.Command {
	var (
		targetName        string
		primaryPath       string
		verifyFQDN        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "apply-views <local-views.conf> <zones-dir>",
		Short: "Apply a views fragment + zonefile tree (atomic; rollback on failure)",
		Long: "apply-views dispatches bind9.config.apply_views against the\n" +
			"connector_id=\"bind9-ssh-9.x\" connector. The CLI reads\n" +
			"<local-views.conf> as the named.conf.local replacement and\n" +
			"walks <zones-dir> (recursively) collecting every regular\n" +
			"file as a zonefile to land under the same relative path in\n" +
			"/etc/bind/. The handler stages the merged tree, runs\n" +
			"`named-checkconf -p`, reloads, and either verifies a sample\n" +
			"FQDN (--verify-fqdn) or just confirms the live config\n" +
			"parses post-reload.\n\n" +
			"--primary-path nominates which staged file's pre/post-op\n" +
			"content the audit row should capture (defaults to the\n" +
			"first key in `files` sorted lexicographically; must\n" +
			"reference one of the staged keys).\n\n" +
			"Atomic-apply contract: an invalid views/zone file or a\n" +
			"failed verify leaves /etc/bind/ byte-identical to before\n" +
			"the call.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho bind9 config apply-views ./views/external.conf ./zones --target vcf-router-bind9\n" +
			"  meho bind9 config apply-views ./views.conf ./zones --verify-fqdn www.evba.lab --target vcf-router-bind9",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runConfigApplyViews(cmd, args[0], args[1], primaryPath, verifyFQDN, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().StringVar(&primaryPath, "primary-path", "",
		"which staged file's content the audit row captures (defaults to first key sorted)")
	cmd.Flags().StringVar(&verifyFQDN, "verify-fqdn", "",
		"sample FQDN to dig-verify post-reload (optional)")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runConfigApplyViews(
	cmd *cobra.Command,
	viewsConfPath, zonesDir, primaryPath, verifyFQDN, targetName string,
	jsonOut bool, backplaneOverride string,
) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	files, err := assembleViewsBundle(viewsConfPath, zonesDir)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
	}
	params := map[string]any{"files": files}
	if primaryPath != "" {
		params["primary_path"] = primaryPath
	}
	if verifyFQDN != "" {
		params["verify_fqdn"] = verifyFQDN
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "bind9.config.apply_views", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "bind9.config.apply_views", r, jsonOut, printWriteResult)
}

// assembleViewsBundle reads the local views.conf and every regular
// file under zonesDir, returning a map keyed by the relative bind-
// root path. Mirrors the handler's documented `files` parameter
// shape: keys are relative paths (no leading slash, no traversal);
// values are UTF-8 content strings.
//
// The views.conf path lands at `named.conf.local` (the bind9
// canonical views include); every file under zonesDir lands at its
// `zonesDir`-relative path verbatim (e.g. `db.evba.lab`, or
// `nested/db.other.zone`). The handler's path-safety filter rejects
// anything that escapes /etc/bind/; we do a defence-in-depth check
// here too (no leading slash; no `..` component) so a typo'd local
// path fails fast before the round-trip.
func assembleViewsBundle(viewsConfPath, zonesDir string) (map[string]string, error) {
	out := map[string]string{}

	// 1. The views.conf -> named.conf.local mapping.
	viewsContent, err := readLocalFile(viewsConfPath)
	if err != nil {
		return nil, err
	}
	out["named.conf.local"] = viewsContent

	// 2. Every regular file under zonesDir, keyed by its relative
	//    path under zonesDir.
	stat, err := os.Stat(zonesDir)
	if err != nil {
		return nil, fmt.Errorf("zones dir %q: %w", zonesDir, err)
	}
	if !stat.IsDir() {
		return nil, fmt.Errorf("zones dir %q is not a directory", zonesDir)
	}
	walkErr := filepath.Walk(zonesDir, func(path string, info os.FileInfo, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if info.IsDir() {
			return nil
		}
		// Reject anything that is not a regular file. Symlinks (could
		// resolve to /etc/passwd or any host path the caller cannot
		// otherwise see), block / char devices, FIFOs, and sockets
		// must not be stageable. The handler's atomic-apply primitive
		// runs with `safety_level=dangerous`; a permissive client-side
		// stager would let a typo'd zones-dir exfil arbitrary files
		// into /etc/bind/. The check uses Mode().IsRegular() (set
		// exactly when the file is a normal on-disk file), not just a
		// symlink-rejection — block / char devices and FIFOs are also
		// not zones-dir entries any operator legitimately wants here.
		if !info.Mode().IsRegular() {
			return fmt.Errorf("zones dir entry %q is not a regular file (mode=%s)", path, info.Mode())
		}
		rel, err := filepath.Rel(zonesDir, path)
		if err != nil {
			return fmt.Errorf("rel %q: %w", path, err)
		}
		// Defence-in-depth: forbid `..` traversal + absolute paths
		// client-side; the handler's path-safety filter is the
		// authoritative gate but failing fast keeps the wire shape
		// clean.
		rel = filepath.ToSlash(rel)
		if rel == "" || strings.HasPrefix(rel, "/") {
			return fmt.Errorf("zones dir entry %q has an unexpected absolute path", path)
		}
		for _, seg := range strings.Split(rel, "/") {
			if seg == ".." {
				return fmt.Errorf("zones dir entry %q contains a `..` segment", path)
			}
		}
		// Skip files that would collide with the explicit views.conf
		// key (operator surprise: a local `named.conf.local` under
		// zonesDir would silently win over the views.conf arg).
		if rel == "named.conf.local" {
			return fmt.Errorf("zones dir contains `named.conf.local` which would collide with the views-conf arg")
		}
		content, err := readLocalFile(path)
		if err != nil {
			return err
		}
		out[rel] = content
		return nil
	})
	if walkErr != nil {
		return nil, walkErr
	}
	return out, nil
}

// newConfigApplyFileCmd returns `meho bind9 config apply-file <name>
// <local-src>`. Maps to op_id `bind9.config.apply_file`. <name> is
// the relative path under the bind config root (e.g.
// `named.conf.options`); <local-src> is the local file whose content
// will replace the remote file's bytes.
func newConfigApplyFileCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "apply-file <name> <local-src>",
		Short: "Replace a bind9 config fragment from a local file (atomic)",
		Long: "apply-file dispatches bind9.config.apply_file against the\n" +
			"connector_id=\"bind9-ssh-9.x\" connector. The CLI reads\n" +
			"<local-src> from the local filesystem and stages its content\n" +
			"as the replacement for <name> on the remote bind config root.\n\n" +
			"<name> may be relative (e.g. `named.conf.options`) or\n" +
			"absolute under the bind config root (e.g.\n" +
			"`/etc/bind/named.conf.options`). Traversal / outside-root\n" +
			"inputs are rejected pre-stage.\n\n" +
			"Atomic-apply contract: an invalid fragment leaves /etc/bind/\n" +
			"byte-identical to before the call.\n\n" +
			"Exit codes mirror meho operation call.",
		Example:       "  meho bind9 config apply-file named.conf.options ./local/named.conf.options --target vcf-router-bind9",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runConfigApplyFile(cmd, args[0], args[1], targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runConfigApplyFile(cmd *cobra.Command, name, localSrc, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	content, err := readLocalFile(localSrc)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
	}
	params := map[string]any{
		"path":    name,
		"content": content,
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "bind9.config.apply_file", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "bind9.config.apply_file", r, jsonOut, printWriteResult)
}

// newConfigBackupCmd returns `meho bind9 config backup [--tag T]`.
// Maps to op_id `bind9.config.backup`. Produces a tar.gz snapshot
// of /etc/bind/ under /var/backups/meho-bind9/ on the target and
// returns a backup_id + listing.
func newConfigBackupCmd() *cobra.Command {
	var (
		targetName        string
		tag               string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "backup",
		Short: "Snapshot /etc/bind/ into /var/backups/meho-bind9/<timestamp>-<tag>.tar.gz",
		Long: "backup dispatches bind9.config.backup against the connector_id=\n" +
			"\"bind9-ssh-9.x\" connector. Creates a UTC-timestamped tar.gz\n" +
			"under /var/backups/meho-bind9/ and returns the backup_id +\n" +
			"the listing of every backup under that directory.\n\n" +
			"--tag is an optional friendly tag embedded in the backup\n" +
			"filename after the timestamp; restricted to\n" +
			"`[A-Za-z0-9._-]{1,64}` so it can't inject shell metacharacters\n" +
			"or path separators.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho bind9 config backup --target vcf-router-bind9\n" +
			"  meho bind9 config backup --tag pre-migration --target vcf-router-bind9",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runConfigBackup(cmd, tag, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().StringVar(&tag, "tag", "",
		"friendly tag embedded in the backup filename ([A-Za-z0-9._-]{1,64})")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runConfigBackup(cmd *cobra.Command, tag, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	params := map[string]any{}
	if tag != "" {
		params["tag"] = tag
	}
	if len(params) == 0 {
		params = nil
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "bind9.config.backup", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "bind9.config.backup", r, jsonOut, printConfigBackup)
}

// printConfigBackup renders the backup result. Surfaces backup_id +
// path + the listing of all backups under the meho-bind9 dir.
func printConfigBackup(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s bind9.config.backup — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	body, err := decodeFlatResult(r.Result)
	if err != nil || body == nil {
		fallbackResultRender(w, r)
		return
	}
	if backupID, ok := body["backup_id"].(string); ok && backupID != "" {
		fmt.Fprintf(w, "  backup_id: %s\n", backupID)
	}
	if path, ok := body["path"].(string); ok && path != "" {
		fmt.Fprintf(w, "  path:      %s\n", path)
	}
	rowsAny, ok := body["rows"].([]any)
	if !ok || len(rowsAny) == 0 {
		return
	}
	fmt.Fprintln(w, "  --- backups under /var/backups/meho-bind9/ ---")
	fmt.Fprintf(w, "%-50s %20s %s\n", "id", "size_bytes", "mtime")
	for _, ra := range rowsAny {
		row, ok := ra.(map[string]any)
		if !ok {
			continue
		}
		fmt.Fprintf(w, "%-50s %20d %s\n",
			truncate(stringField(row, "id"), 50),
			intField(row, "size_bytes"),
			stringField(row, "mtime"),
		)
	}
}

// newConfigReloadCmd returns `meho bind9 config reload`. Maps to
// op_id `bind9.config.reload`. Issues `rndc reload` on the target.
func newConfigReloadCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "reload",
		Short: "rndc reload — re-read the active bind9 configuration",
		Long: "reload dispatches bind9.config.reload against the connector_id=\n" +
			"\"bind9-ssh-9.x\" connector. Runs `rndc reload` on the target\n" +
			"and surfaces the structured success/failure envelope (ok flag,\n" +
			"rndc exit, stderr, rndc-status snapshot pre/post).\n\n" +
			"Use after an `apply-file` / `apply-views` only if you needed\n" +
			"to bypass the primitive's automatic reload (rare); the apply\n" +
			"ops themselves reload at the verify step.\n\n" +
			"Exit codes mirror meho operation call.",
		Example:       "  meho bind9 config reload --target vcf-router-bind9",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runConfigReload(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runConfigReload(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "bind9.config.reload", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "bind9.config.reload", r, jsonOut, printConfigReload)
}

// printConfigReload renders the rndc reload result. Surfaces ok +
// rndc_reload_exit; the rndc-status snapshots before/after are on
// --json.
func printConfigReload(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s bind9.config.reload — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	body, err := decodeFlatResult(r.Result)
	if err != nil || body == nil {
		fallbackResultRender(w, r)
		return
	}
	for _, key := range []string{"ok", "rndc_reload_exit", "stderr"} {
		if v, ok := body[key]; ok && v != nil {
			fmt.Fprintf(w, "  %-18s %v\n", key+":", v)
		}
	}
	fmt.Fprintln(w, "  (see --json for result_state_before / result_state_after rndc-status snapshots)")
}
