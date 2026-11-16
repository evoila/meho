// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package policy

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// explainOptions are the flags of `meho policy explain`.
type explainOptions struct {
	Me                bool
	Groups            []string
	Principal         string
	Via               string
	Target            string
	File              string
	Scope             string
	Request           string
	JSONOut           bool
	BackplaneOverride string
}

func newExplainCmd() *cobra.Command {
	var opts explainOptions
	cmd := &cobra.Command{
		Use:   "explain [CONNECTOR_ID OP_ID]",
		Short: "Say why a call runs, waits or is denied, and who may approve it: MEHO group kinds (MEHO-<kind>), not the names of the login groups of your install",
		Long: "explain calls POST /api/v1/policy/explain. It says what MEHO does " +
			"with one call (run, wait or deny), why, and who may approve it. " +
			"Nothing runs and nothing is written.\n\n" +
			"Ask for yourself with --me, or for a login with --groups (for " +
			"example --groups Operators) and --principal person, agent or " +
			"service. Such a login has no grants. --via web asks for a person " +
			"in the web interface. --file asks with a policy file that is not " +
			"imported. --request ID shows the rule stored on an approval request, " +
			"and the groups that may approve it.\n\n" +
			"explain asks about a call with no parameters. For an operation " +
			"whose answer depends on its parameters, the answer is the one of " +
			"a call without parameters. It asks the checks after the policy " +
			"rules that the call asks: the announce rule of the tenant, and " +
			"whether the target's connector takes ingested operations. The " +
			"last line names each check after the policy rules that explain " +
			"does not ask and that can still refuse the call.\n\n" +
			"explain works with POLICY_ENGINE_SHADOW off: it then shows today's " +
			"answer only. Today's checks decide while the policy evaluator is off " +
			"or in shadow mode. With POLICY_ENGINE_SHADOW on, the answer also lists " +
			"every rule the evaluator matched, in order, and the floor that decided.\n\n" +
			"Who may approve is a list of MEHO group kinds (MEHO-<kind>), not " +
			"the names of the login groups of your install.\n\n" +
			"Only a tenant admin or a platform admin sees the entries of the " +
			"policy and their places in it. Everyone else gets the answer, the " +
			"reason, the risk level and who may approve.",
		Example: "  meho policy explain <connector-id> <op-id> --me\n" +
			"  meho policy explain <connector-id> <op-id> --groups Operators --target <target-name>\n" +
			"  meho policy explain <connector-id> <op-id> --groups Operators --principal agent\n" +
			"  meho policy explain <connector-id> <op-id> --groups Admins --file policy.yaml\n" +
			"  meho policy explain --request <request-id>",
		Args:          cobra.MaximumNArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runExplain(cmd, opts, args)
		},
	}
	f := cmd.Flags()
	f.BoolVar(&opts.Me, "me", false, "ask for yourself")
	f.StringSliceVar(&opts.Groups, "groups", nil,
		"ask for a login in these groups: Readers, Operators, SA-Operators, Admins, Approvers")
	f.StringVar(&opts.Principal, "principal", "person", "with --groups: person, agent or service")
	f.StringVar(&opts.Via, "via", "",
		"for a person: web or token (default: as you call with --me, token with --groups)")
	f.StringVar(&opts.Target, "target", "", "the target's name")
	f.StringVar(&opts.File, "file", "", "a policy file that is not imported")
	f.StringVar(&opts.Scope, "scope", "tenant", "with --file: tenant or install")
	f.StringVar(&opts.Request, "request", "", "show the rule stored on this approval request")
	f.BoolVar(&opts.JSONOut, "json", false, "print the server's answer as JSON")
	f.StringVar(&opts.BackplaneOverride, "backplane", "",
		"backplane URL to call (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

// explainBody checks the flags and builds the question.
func explainBody(opts explainOptions, args []string) (api.PolicyExplainBody, error) {
	var body api.PolicyExplainBody
	if opts.Request != "" {
		if len(args) > 0 || opts.Me || len(opts.Groups) > 0 || opts.File != "" || opts.Target != "" {
			return body, errors.New("--request takes no operation and no other question")
		}
		id, err := uuid.Parse(opts.Request)
		if err != nil {
			return body, fmt.Errorf("--request must be a request id: %v", err)
		}
		body.RequestId = &id
		return body, nil
	}
	if len(args) != 2 {
		return body, errors.New("name the operation: CONNECTOR_ID OP_ID")
	}
	if opts.Me == (len(opts.Groups) > 0) {
		return body, errors.New("ask for yourself (--me) or for login groups (--groups): one of the two")
	}
	switch opts.Principal {
	case "person", "agent", "service":
	default:
		return body, fmt.Errorf("--principal must be person, agent or service; got %q", opts.Principal)
	}
	if opts.Via != "" && opts.Via != "web" && opts.Via != "token" {
		return body, fmt.Errorf("--via must be web or token; got %q", opts.Via)
	}
	if opts.Scope != "tenant" && opts.Scope != "install" {
		return body, fmt.Errorf("--scope must be tenant or install; got %q", opts.Scope)
	}
	body.ConnectorId, body.OpId = &args[0], &args[1]
	if opts.Me {
		body.Me = &opts.Me
	} else {
		groups := opts.Groups
		principal := api.PolicyExplainBodyPrincipal(opts.Principal)
		body.Groups, body.Principal = &groups, &principal
	}
	if opts.Via != "" {
		via := api.PolicyExplainBodyVia(opts.Via)
		body.Via = &via
	}
	if opts.Target != "" {
		body.Target = &opts.Target
	}
	if opts.File != "" {
		content, err := os.ReadFile(opts.File) // #nosec G304 -- the operator names the file
		if err != nil {
			return body, fmt.Errorf("read %s: %v", opts.File, err)
		}
		file := string(content)
		scope := api.PolicyExplainBodyScope(opts.Scope)
		body.File, body.Scope = &file, &scope
	}
	return body, nil
}

func runExplain(cmd *cobra.Command, opts explainOptions, args []string) error {
	body, err := explainBody(opts, args)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := postExplain(cmd.Context(), backplaneURL, body)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp, opts.JSONOut)
	}
	var answer api.PolicyExplainResponse
	if err := json.Unmarshal(resp.Body, &answer); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("decode the answer: %v", err)), opts.JSONOut)
	}
	if opts.JSONOut {
		if err := output.PrintJSON(cmd.OutOrStdout(), json.RawMessage(resp.Body)); err != nil {
			return err
		}
	} else {
		printExplain(cmd.OutOrStdout(), answer)
	}
	if answer.Status != api.PolicyExplainResponseStatusOk {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(sanitize(deref(answer.Error))), opts.JSONOut)
	}
	return nil
}

func postExplain(ctx context.Context, backplaneURL string, body api.PolicyExplainBody) (*rawResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return doRequest(ctx, authed, func(ctx context.Context) (*http.Response, error) {
		return authed.ExplainApiV1PolicyExplainPost(ctx, &api.ExplainApiV1PolicyExplainPostParams{}, body)
	})
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

func printExplain(w io.Writer, a api.PolicyExplainResponse) {
	if a.Status != api.PolicyExplainResponseStatusOk {
		return // the error goes to stderr
	}
	if a.Request != nil {
		printStoredRequest(w, *a.Request)
		return
	}
	if a.Answer != nil {
		fmt.Fprintf(w, "Answer: %s (today's checks decide)\n", *a.Answer)
	}
	if a.Approvers != nil {
		printApprovers(w, *a.Approvers)
	}
	if a.Today != nil {
		line := a.Today.PolicyVerdict
		if r := deref(a.Today.Reason); r != "" {
			line += ": " + r
		}
		fmt.Fprintf(w, "Today: %s\n", sanitize(line))
	}
	if a.Evaluator == nil {
		fmt.Fprintln(w, "Policy evaluator: off (POLICY_ENGINE_SHADOW is off)")
	} else {
		printEvaluator(w, *a.Evaluator)
	}
	if a.Classification != nil {
		printClassification(w, *a.Classification)
	}
	if a.NotChecked != nil && len(*a.NotChecked) > 0 {
		names := make([]string, len(*a.NotChecked))
		for i, n := range *a.NotChecked {
			names[i] = sanitize(n)
		}
		fmt.Fprintln(w, "Not checked: "+strings.Join(names, ", "))
	}
}

func printApprovers(w io.Writer, a api.ExplainApprovers) {
	groups := "nobody"
	if len(a.Groups) > 0 {
		groups = strings.Join(a.Groups, ", ")
	}
	// Only a tenant admin or a platform admin gets the rule.
	if rule := deref(a.Rule); rule != "" {
		fmt.Fprintf(w, "Who may approve: %s (rule: %s)\n", sanitize(groups), sanitize(rule))
		return
	}
	fmt.Fprintf(w, "Who may approve: %s\n", sanitize(groups))
}

func ruleLine(r api.ExplainRule) string {
	line := fmt.Sprintf("%s %s", r.Source, r.RuleId)
	if r.Verdict != nil {
		line += ": " + string(*r.Verdict)
	}
	if r.Text != "" {
		line += " (" + r.Text + ")"
	}
	if p := deref(r.Path); p != "" {
		line += " at " + p
	}
	return sanitize(line)
}

func printEvaluator(w io.Writer, e api.ExplainEvaluator) {
	same := "same as today"
	if !e.SameAsToday {
		same = "differs from today"
	}
	fmt.Fprintf(w, "Policy evaluator (shadow): %s, %s\n", e.Verdict, same)
	fmt.Fprintf(w, "Decided by: %s\n", ruleLine(e.Rule))
	if e.Floor != nil {
		fmt.Fprintf(w, "Floor: %s\n", ruleLine(*e.Floor))
	}
	fmt.Fprintln(w, "Rules in order:")
	for i, r := range e.Rules {
		fmt.Fprintf(w, "  %d. %s\n", i+1, ruleLine(r))
	}
}

func printClassification(w io.Writer, c api.ExplainClassification) {
	wait := "no"
	if c.RequiresApproval {
		wait = "yes"
	}
	category := deref(c.Category)
	if category == "" {
		category = "-"
	}
	fmt.Fprintf(w, "Risk level: %s, always waits: %s, category: %s\n",
		sanitize(c.RiskLevel), wait, sanitize(category))
	if c.Entries == nil {
		return
	}
	for _, e := range *c.Entries {
		fmt.Fprintf(w, "  matched %s\n", sanitize(e.Path))
	}
}

func printStoredRequest(w io.Writer, r api.ExplainStoredRequest) {
	fmt.Fprintf(w, "Request %s: %s on %s, status %s\n",
		r.RequestId, sanitize(r.OpId), sanitize(r.ConnectorId), sanitize(r.Status))
	if r.PolicyRule == nil {
		fmt.Fprintln(w, "Stored rule: none (the policy evaluator was off when the request was made)")
	} else {
		fmt.Fprintf(w, "Stored rule: %s\n", sanitize(compactJSON(*r.PolicyRule)))
	}
	if r.PolicyVersion != nil {
		fmt.Fprintf(w, "Policy version: %d\n", *r.PolicyVersion)
	}
	printApprovers(w, r.Approvers)
}
