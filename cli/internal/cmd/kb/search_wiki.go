// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package kb

import (
	"fmt"
	"io"

	"github.com/evoila/meho/cli/internal/api"
)

// printWikiHits renders the team wiki part of `meho kb search` (#5976).
// The backplane fills `wiki` only for a tenant that keeps its lessons in a
// team wiki; then it holds the wiki's hits, or why the wiki was not
// searched. A nil `wiki` prints nothing, so other tenants see no change.
func printWikiHits(w io.Writer, wiki *api.KbWikiSearch) {
	if wiki == nil {
		return
	}
	if !wiki.Searched {
		if wiki.Message != nil {
			fmt.Fprintln(w, *wiki.Message)
		} else {
			fmt.Fprintf(w, "The team wiki (docs collection `%s`) was not searched.\n", wiki.Collection)
		}
		return
	}
	var hits []api.DocsChunk
	if wiki.Hits != nil {
		hits = *wiki.Hits
	}
	if len(hits) == 0 {
		fmt.Fprintf(w, "no team wiki hits (docs collection %s)\n", wiki.Collection)
	} else {
		fmt.Fprintf(w, "team wiki hits (docs collection %s):\n", wiki.Collection)
		fmt.Fprintf(w, "%-5s %-40s %s\n", "RANK", "TITLE", "SOURCE")
		for i, hit := range hits {
			fmt.Fprintf(w, "%-5d %-40s %s\n", i+1, truncate(wikiTitle(hit), 40), stringOr(hit.SourceUrl, hit.ChunkId))
		}
	}
	if wiki.DuplicateCount != nil && *wiki.DuplicateCount > 0 {
		fmt.Fprintf(w, "%d wiki hits are shown above as their imported copy\n", *wiki.DuplicateCount)
	}
}

func wikiTitle(hit api.DocsChunk) string {
	return stringOr(hit.Title, snippetOf(hit.Content))
}

func stringOr(value *string, fallback string) string {
	if value != nil && *value != "" {
		return *value
	}
	return fallback
}
