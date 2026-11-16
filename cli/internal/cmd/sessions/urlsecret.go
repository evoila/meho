// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sessions

import (
	"net/url"
	"regexp"
	"strings"
)

// The secret parts of a URL, found the way the server finds them
// (backend/src/meho_backplane/redaction/url_secret.py): the user part
// before "@", and the value of each query parameter whose name says it
// holds a secret. The name is read after URL decoding, so "%74oken" is
// "token". The host, the path and the other parameters stay readable.

var (
	// A URL: a short scheme, "://", and every byte up to white space, a
	// double quote or an angle bracket. findURLs checks that no scheme
	// byte sits before it.
	urlRe = regexp.MustCompile(`[A-Za-z][A-Za-z0-9+.\-]{0,31}://[^\s"<>]+`)
	// The opening quote of a link in single quotes, inside a URL match.
	quotedLinkStart = regexp.MustCompile(`'[A-Za-z][A-Za-z0-9+.\-]{0,31}://`)
	urlPartMark     = regexp.MustCompile(`[@/?&=#]`)
	nameSplit       = regexp.MustCompile(`[^A-Za-z0-9]+`)

	secretLastWords = map[string]bool{
		"sig": true, "signature": true, "credential": true, "credentials": true, "token": true,
		"secret": true, "password": true, "passwd": true, "pwd": true, "apikey": true,
	}
)

func isSchemeByte(b byte) bool {
	return b >= 'A' && b <= 'Z' || b >= 'a' && b <= 'z' || b >= '0' && b <= '9' ||
		b == '+' || b == '.' || b == '-'
}

// isSecretQueryName splits the name into words at "-", "_", "." and
// camelCase; the last word decides. "api_key" counts too.
func isSecretQueryName(name string) bool {
	var b strings.Builder
	for i := 0; i < len(name); i++ {
		c := name[i]
		if i > 0 && c >= 'A' && c <= 'Z' {
			p := name[i-1]
			if p >= 'a' && p <= 'z' || p >= '0' && p <= '9' {
				b.WriteByte('_')
			}
		}
		b.WriteByte(c)
	}
	var words []string
	for _, w := range nameSplit.Split(strings.ToLower(b.String()), -1) {
		if w != "" {
			words = append(words, w)
		}
	}
	if len(words) == 0 {
		return false
	}
	last := words[len(words)-1]
	return secretLastWords[last] || len(words) >= 2 && words[len(words)-2] == "api" && last == "key"
}

func isMarker(value string) bool {
	if !strings.HasPrefix(value, "[REDACTED:") || !strings.HasSuffix(value, "]") {
		return false
	}
	name := value[len("[REDACTED:") : len(value)-1]
	return name != "" && !strings.ContainsAny(name, "]'\" \t\n\r\f\v")
}

func isSecretValue(value string) bool {
	return value != "" && !isMarker(value)
}

// unquotePlus decodes a query name like Python's unquote_plus: a bad
// escape stays as it is.
func unquotePlus(s string) string {
	if out, err := url.QueryUnescape(s); err == nil {
		return out
	}
	s = strings.ReplaceAll(s, "+", " ")
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if s[i] == '%' && i+2 < len(s) {
			if v, err := url.PathUnescape(s[i : i+3]); err == nil {
				b.WriteString(v)
				i += 2
				continue
			}
		}
		b.WriteByte(s[i])
	}
	return b.String()
}

// urlSecretSpans returns the start and end of every secret part of every
// URL in text, left to right.
func urlSecretSpans(text string) [][2]int {
	var spans [][2]int
	pos := 0
	for pos < len(text) {
		loc := urlRe.FindStringIndex(text[pos:])
		if loc == nil {
			break
		}
		start, end := pos+loc[0], pos+loc[1]
		if start > 0 && isSchemeByte(text[start-1]) {
			// Only a later start after "://" can be a URL of its own.
			pos = start + strings.Index(text[start:], "://") + 1
			continue
		}
		for _, l := range links(text, start, end) {
			spans = append(spans, urlSpans(l.url, l.offset)...)
		}
		pos = end
	}
	return spans
}

type link struct {
	url    string
	offset int
}

// links splits a URL match where the next link in single quotes starts,
// and cuts a link in single quotes at its closing quote.
func links(text string, start, end int) []link {
	quoted := start > 0 && text[start-1] == '\''
	var out []link
	for {
		opening := quotedLinkStart.FindStringIndex(text[start:end])
		stop := end
		if opening != nil {
			stop = start + opening[0]
		}
		out = append(out, link{closeQuote(text[start:stop], quoted), start})
		if opening == nil {
			return out
		}
		start = stop + 1
		quoted = true
	}
}

func closeQuote(u string, quoted bool) string {
	if !quoted {
		return u
	}
	q := strings.LastIndexByte(u, '\'')
	if q == -1 || urlPartMark.MatchString(u[q+1:]) {
		return u
	}
	return u[:q]
}

func urlSpans(u string, offset int) [][2]int {
	var spans [][2]int
	rest := strings.Index(u, "://") + 3
	authorityEnd := len(u)
	if i := strings.IndexAny(u[rest:], "/?#"); i >= 0 {
		authorityEnd = rest + i
	}
	if at := strings.LastIndexByte(u[rest:authorityEnd], '@'); at >= 0 && isSecretValue(u[rest:rest+at]) {
		spans = append(spans, [2]int{offset + rest, offset + rest + at})
	}
	queryEnd := len(u)
	if i := strings.IndexByte(u[authorityEnd:], '#'); i >= 0 {
		queryEnd = authorityEnd + i
	}
	question := strings.IndexByte(u[authorityEnd:queryEnd], '?')
	if question == -1 {
		return spans
	}
	pos := authorityEnd + question + 1
	for pos <= queryEnd {
		pieceEnd := queryEnd
		if i := strings.IndexByte(u[pos:queryEnd], '&'); i >= 0 {
			pieceEnd = pos + i
		}
		if eq := strings.IndexByte(u[pos:pieceEnd], '='); eq >= 0 {
			equals := pos + eq
			// "&amp;" is how HTML and XML text writes "&".
			name := strings.TrimPrefix(unquotePlus(u[pos:equals]), "amp;")
			if isSecretQueryName(name) && isSecretValue(u[equals+1:pieceEnd]) {
				spans = append(spans, [2]int{offset + equals + 1, offset + pieceEnd})
			}
		}
		pos = pieceEnd + 1
	}
	return spans
}
