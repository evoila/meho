// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"bytes"
	"encoding/json"
	"io"
	"strings"
	"testing"
)

// managedRealmAnswers holds one small answer per human printer, as the JSON
// text after its opening brace.
var managedRealmAnswers = map[string]struct {
	print  func(w io.Writer, r *CallResult)
	answer string
}{
	"user list":         {printUserList, `"rows":[{"username":"a"}],"total":1}`},
	"empty user list":   {printUserList, `"rows":[],"total":0}`},
	"client list":       {printClientList, `"rows":[{"clientId":"a"}],"total":1}`},
	"client get":        {printClientGet, `"client":{"clientId":"a"}}`},
	"client-scope list": {printClientScopeList, `"rows":[{"name":"a"}],"total":1}`},
	"group list":        {printGroupList, `"rows":[{"name":"a"}],"total":1}`},
	"group member list": {printGroupMemberList, `"rows":[{"username":"a"}],"total":1}`},
	"realm get":         {printRealmGet, `"realm":{"realm":"a"}}`},
	"role-mapping get":  {printRoleMappingGet, `"role_mappings":{"realmMappings":[]}}`},
	"a write":           {printWriteResult("keycloak.user.create"), `"id":"a","created":true}`},
}

func printAnswer(print func(w io.Writer, r *CallResult), answer string) string {
	var b bytes.Buffer
	print(&b, &CallResult{Status: "ok", Result: json.RawMessage(answer)})
	return b.String()
}

// TestPrintersShowTheManagedRealmWhenTheServerSuppliesIt — with the field,
// each printer adds one line with it; without the field, the output is the
// same but for that line.
func TestPrintersShowTheManagedRealmWhenTheServerSuppliesIt(t *testing.T) {
	for name, tc := range managedRealmAnswers {
		t.Run(name, func(t *testing.T) {
			const line = "  managed realm: example-realm\n"
			with := printAnswer(tc.print, `{"managed_realm":"example-realm",`+tc.answer)
			without := printAnswer(tc.print, `{`+tc.answer)
			if strings.Count(with, line) != 1 {
				t.Errorf("want one %q line in:\n%s", line, with)
			}
			if strings.Contains(without, "managed realm") {
				t.Errorf("unexpected realm line in:\n%s", without)
			}
			if got := strings.Replace(with, line, "", 1); got != without {
				t.Errorf("the rest of the output differs:\n%q\n%q", got, without)
			}
		})
	}
}

// TestManagedRealmShowsControlCharactersAsEscapes — a realm name from the
// server sends no raw control byte to the terminal.
func TestManagedRealmShowsControlCharactersAsEscapes(t *testing.T) {
	out := printAnswer(printUserList, `{"managed_realm":"a\u001b[2Kb\u0007","rows":[],"total":0}`)
	if !strings.Contains(out, `a\x1b[2Kb\x07`) || strings.ContainsAny(out, "\x1b\a") {
		t.Errorf("want visible escapes, got %q", out)
	}
}

// TestManagedRealmOfAnotherTypePrintsNothing — a null, an empty text or a
// number is no realm name.
func TestManagedRealmOfAnotherTypePrintsNothing(t *testing.T) {
	for _, value := range []string{"null", `""`, "7"} {
		out := printAnswer(printUserList, `{"managed_realm":`+value+`,"rows":[],"total":0}`)
		if strings.Contains(out, "managed realm") {
			t.Errorf("unexpected realm line for %s: %q", value, out)
		}
	}
}
