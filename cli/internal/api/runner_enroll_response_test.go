// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package api

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
)

// A bad public key on POST /api/v1/runner-principals/enroll answers 422 with
// the list the API file declares. The body below is the one the backplane
// sends (test_a_bad_key_is_refused_and_keeps_the_code).
const runnerBadKeyBody = `{"detail":[{"loc":["body","public_key"],` +
	`"msg":"send the public key only","type":"runner_public_key_invalid"}]}`

func TestParseEnrollRunnerResponseReadsTheBadKeyAnswer(t *testing.T) {
	rsp := &http.Response{
		StatusCode: http.StatusUnprocessableEntity,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(strings.NewReader(runnerBadKeyBody)),
	}

	got, err := ParseEnrollRunnerApiV1RunnerPrincipalsEnrollPostResponse(rsp)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if got.JSON422 == nil || got.JSON422.Detail == nil || len(*got.JSON422.Detail) != 1 {
		t.Fatalf("want one detail entry; got %+v", got.JSON422)
	}
	d := (*got.JSON422.Detail)[0]
	if d.Type != "runner_public_key_invalid" || d.Msg != "send the public key only" || len(d.Loc) != 2 {
		t.Errorf("entry decoded wrong: %+v", d)
	}
}

// The same body also decodes with the plain generated type, without the
// extra reading rules of validation_error.go.
func TestRunnerBadKeyAnswerFitsTheGeneratedType(t *testing.T) {
	type generated HTTPValidationError
	var got generated
	if err := json.Unmarshal([]byte(runnerBadKeyBody), &got); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if got.Detail == nil || len(*got.Detail) != 1 {
		t.Fatalf("want one detail entry; got %+v", got)
	}
}
