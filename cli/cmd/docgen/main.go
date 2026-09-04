// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Command docgen renders the docs-site CLI reference page
// (docs-site/reference/cli.md) from the meho cobra command tree.
//
// It is NOT part of the shipped meho binary (goreleaser builds only
// ./cmd/meho); it is a build tool driven by `make cli-docs`, and its
// output is committed. The freshness gate in the `cli-api-snapshot-
// freshness` CI job regenerates it and fails on a working-tree diff —
// the same committed-derived-artifact shape as cli/api/openapi.json.
//
// The command tree is built with backplane discovery disabled
// (cmd.NewRootCmdForDocs), so generation is deterministic and offline:
// the page documents the built-in verbs, and a connected backplane may
// additionally surface discovered operations at runtime.
//
// Public-safety: cobra help strings are operator-facing but a few embed
// internal planning references (GitHub issue numbers, Goal/Task IDs,
// design-doc section markers). redact() strips them so none reach the
// published page — the Go twin of _redact in
// backend/scripts/generate_reference_docs.py.
package main

import (
	"fmt"
	"os"
	"regexp"
	"sort"
	"strings"

	"github.com/spf13/cobra"
	"github.com/spf13/pflag"

	"github.com/evoila/meho/cli/internal/cmd"
)

// defaultOutPath is relative to cli/ (where `make cli-docs` runs), so it
// resolves to the repo's docs-site tree.
const defaultOutPath = "../docs-site/reference/cli.md"

// Public-safety redaction patterns. Cobra help strings are operator-facing
// but a few embed internal planning references — GitHub issue numbers
// (#3153), Goal/Task identifiers (G11.2-T6, G0.7), design-doc section
// markers (§5) — or lab-specific example coordinates (a `.lab` hostname,
// an IPv4 literal). None may reach the published page.
var (
	internalRefParen = regexp.MustCompile(`\s*\([^()]*(?:#\d+|G\d[\dA-Za-z.]*(?:-T\d+)?|§\d+)[^()]*\)`)
	internalRefBare  = regexp.MustCompile(`\s*(?:#\d+|G\d[\dA-Za-z.]*(?:-T\d+)?|§\d+)`)
	labHostname      = regexp.MustCompile(`\b[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.lab\b`)
	ipv4Literal      = regexp.MustCompile(`\b(?:\d{1,3}\.){3}\d{1,3}\b`)
	multiSpace       = regexp.MustCompile(`[ \t]{2,}`)
)

// redact strips internal planning references and lab-specific example
// coordinates from operator-facing text.
func redact(text string) string {
	text = internalRefParen.ReplaceAllString(text, "")
	text = internalRefBare.ReplaceAllString(text, "")
	text = labHostname.ReplaceAllString(text, "example.com")
	text = ipv4Literal.ReplaceAllString(text, "<ip>")
	lines := strings.Split(text, "\n")
	for i, ln := range lines {
		lines[i] = strings.TrimRight(multiSpace.ReplaceAllString(ln, " "), " ")
	}
	return strings.TrimSpace(strings.Join(lines, "\n"))
}

// documented reports whether a command belongs in the reference: not
// hidden, and not cobra's auto-added help / completion scaffolding.
func documented(c *cobra.Command) bool {
	if c.Hidden || c.Name() == "help" || c.Name() == "completion" {
		return false
	}
	return c.IsAvailableCommand() || c.HasAvailableSubCommands()
}

func sortedSubcommands(c *cobra.Command) []*cobra.Command {
	subs := make([]*cobra.Command, 0, len(c.Commands()))
	for _, sub := range c.Commands() {
		if documented(sub) {
			subs = append(subs, sub)
		}
	}
	sort.Slice(subs, func(i, j int) bool { return subs[i].Name() < subs[j].Name() })
	return subs
}

// localFlagLines renders a command's own flags (skipping the globals and
// help), one bullet each, sorted by name (pflag VisitAll is sorted).
func localFlagLines(c *cobra.Command) []string {
	var lines []string
	c.LocalFlags().VisitAll(func(f *pflag.Flag) {
		switch f.Name {
		case "help", "config", "verbose":
			return
		}
		name := "`--" + f.Name
		if f.Shorthand != "" {
			name += "`, `-" + f.Shorthand
		}
		name += "`"
		usage := redact(f.Usage)
		lines = append(lines, fmt.Sprintf("- %s — %s", name, usage))
	})
	return lines
}

// headingPrefix caps Markdown heading depth at level 6.
func headingPrefix(depth int) string {
	level := depth + 2 // top-level command == "##"
	if level > 6 {
		level = 6
	}
	return strings.Repeat("#", level)
}

func renderCommand(sb *strings.Builder, c *cobra.Command, depth int) {
	fmt.Fprintf(sb, "%s `%s`\n\n", headingPrefix(depth), c.CommandPath())
	if short := redact(c.Short); short != "" {
		fmt.Fprintf(sb, "%s\n\n", short)
	}
	// The multi-paragraph `Long` help is deliberately omitted: it carries
	// contributor-facing jargon and lab-recipe examples not suited to the
	// public reference, and mechanical ref-redaction of mid-sentence
	// references leaves grammar artifacts. The usage line + flags below
	// document the command shape P-10 needs.
	fmt.Fprintf(sb, "```\n%s\n```\n\n", c.UseLine())
	if flags := localFlagLines(c); len(flags) > 0 {
		fmt.Fprintf(sb, "%s\n\n", strings.Join(flags, "\n"))
	}
	for _, sub := range sortedSubcommands(c) {
		renderCommand(sb, sub, depth+1)
	}
}

func render(root *cobra.Command) string {
	var sb strings.Builder
	sb.WriteString("<!--\n")
	sb.WriteString("  GENERATED FILE — do not edit by hand.\n")
	sb.WriteString("  Regenerate from cli/ with: make cli-docs\n")
	sb.WriteString("  Source of truth: the meho cobra command tree (cli/internal/cmd).\n")
	sb.WriteString("  Freshness is enforced by the cli-api-snapshot-freshness CI job.\n")
	sb.WriteString("-->\n\n")
	sb.WriteString("# CLI reference\n\n")
	sb.WriteString("`meho` is the operator CLI for the MEHO governance backplane. " +
		"It dispatches through the same policy, audit, and approval path as the " +
		"MCP tool surface — the CLI and the agent are dual front-ends on one " +
		"backplane, not wrappers of each other.\n\n")
	sb.WriteString("This page is generated from the built-in command tree. Further " +
		"operations are discovered from a connected backplane at runtime, so a " +
		"logged-in `meho` may list more commands than appear here.\n\n")
	sb.WriteString("Every command accepts the global flags below.\n\n")
	sb.WriteString("## Global flags\n\n")
	sb.WriteString("- `--config` — path to the meho config file " +
		"(default: `$XDG_CONFIG_HOME/meho/config.json`).\n")
	sb.WriteString("- `--verbose`, `-v` — enable verbose output.\n\n")
	for _, sub := range sortedSubcommands(root) {
		renderCommand(&sb, sub, 0)
	}
	out := strings.TrimRight(sb.String(), "\n") + "\n"
	return out
}

func main() {
	outPath := defaultOutPath
	if len(os.Args) > 1 && os.Args[1] != "" {
		outPath = os.Args[1]
	}
	root := cmd.NewRootCmdForDocs()
	if err := os.WriteFile(outPath, []byte(render(root)), 0o644); err != nil {
		fmt.Fprintf(os.Stderr, "docgen: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("wrote %s\n", outPath)
}
