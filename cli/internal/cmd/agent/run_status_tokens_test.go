// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"bytes"
	"strings"
	"testing"

	"github.com/google/uuid"

	"github.com/evoila/meho/cli/internal/api"
)

func TestRunStatusPrintsTokenCounts(t *testing.T) {
	input, output, cacheRead, cacheWrite := 1000, 300, 200, 0
	var buf bytes.Buffer
	printRunStatus(&buf, &api.AgentRunStatusResponse{
		RunId:            uuid.MustParse("11111111-1111-1111-1111-111111111111"),
		Status:           api.AgentRunStatusSucceeded,
		Turns:            1,
		InputTokens:      &input,
		OutputTokens:     &output,
		CacheReadTokens:  &cacheRead,
		CacheWriteTokens: &cacheWrite,
	})
	for _, want := range []string{
		"input_tokens:    1000", "output_tokens:   300", "cache_read:      200", "cache_write:     0",
	} {
		if !strings.Contains(buf.String(), want) {
			t.Errorf("status missing %q in %q", want, buf.String())
		}
	}
}

func TestRunStatusLeavesOutTokensOfAnUnfinishedRun(t *testing.T) {
	var buf bytes.Buffer
	printRunStatus(&buf, &api.AgentRunStatusResponse{
		RunId:  uuid.MustParse("11111111-1111-1111-1111-111111111111"),
		Status: api.AgentRunStatusRunning,
	})
	if strings.Contains(buf.String(), "tokens") || strings.Contains(buf.String(), "cache_") {
		t.Errorf("unfinished run shows token lines: %q", buf.String())
	}
}
