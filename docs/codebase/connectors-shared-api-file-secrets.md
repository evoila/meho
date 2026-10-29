# Shared API file secret check

## Overview

`backend/src/meho_backplane/connectors/_shared/api_file_secrets.py` checks
every API description file before a read returns it. An API description
file is a file an appliance serves to describe its own API: a Swagger or
OpenAPI file (JSON or YAML), a WSDL or XSD file (XML) or a documentation
page (HTML).

The helper reads the file with a real parser for its format and replaces
every value under a secret name with the placeholder
`[REDACTED:param_name]`. It then writes the file back from the parsed
copy. So a secret is the value under a secret-named key, wherever it
hides and however it is written. A text scan was tried first (#4690,
#4731) and lost a race against every new trick: a value on the next line,
a YAML alias, an escaped key, a quoted `>` inside an HTML attribute. The
parser sees the structure, so those tricks do not exist for it.

**The check fails closed.** What it cannot read whole, it does not
return. It withholds that part instead:

- The part becomes the placeholder `[REDACTED:param_name]`.
- The count `secrets_replaced` includes it.
- The result names the reason (`CheckedApiFile.withheld`), never the
  text of the part.

Only a part that the parser read fully, and that passed the secret-name
rules key by key, comes back. It comes back redacted. Before this rule,
each code review found one more form that a parser could not read and
the check let through. Now every such form is withheld.

Every API file read uses this helper (#4780): `fleet.api_file.get`,
`hcx.api_file.get` and `vcenter.sso_admin.api_file.get` on main, and the
reads of #4690 (VCF Operations) and #4731 (Aria Automation 8) after it.
New API file reads use it from the start; `srm.api_file.get`,
`vr.api_file.get` and `vrli.api_file.get` (Logs 8.18 and 9.0) do. These
reads also give the helper MEHO's general redaction rules as a text rule
(`text_redactor` in `connectors/_shared/api_file_read.py`), so text they
match comes back as a placeholder instead of stopping the read. The rule
runs on each parsed text value before the copy is written back, so the
copy keeps its format and every value keeps its type.

## Key types

- `check_api_file(text, *, content_type, path, text_rule=None) ->
  CheckedApiFile` — the entry point. Returns the checked copy (`text`),
  the number of placeholders in it (`secrets_replaced`, withheld parts
  included) and one reason for each withheld part (`withheld`, in
  order). Raises `ApiFileFormatError` when the format is unknown, the
  file does not parse, or the copy cannot be written as UTF-8. An empty
  file comes back empty. `text_rule` is an optional
  function for one text value: it returns the changed value and how many
  places changed. It runs on every text value that is not replaced
  whole, in every format, and its count joins `secrets_replaced`.
- `detect_format(content_type, path)` — `json`, `yaml`, `xml`, `html` or
  `None`. The content type decides. When it is `text/plain` or missing,
  the file ending decides (`.json`, `.yaml`, `.yml`, `.xml`, `.wsdl`,
  `.xsd`, `.html`, `.htm`). YAML is never tried on other text: almost any
  text is valid YAML.
- `scrub_query_string(path) -> (path, count)` — the same check for the
  query of a redirect path. The names are decoded first, so
  `license%4Bey` reads as `licenseKey`.
- `REDACTED` — the placeholder, the same one the governed Vault read
  returns for a secret-named value.
- `ApiFileFormatError` (a `ValueError`) — the message names the reason
  (format, line, column), never the text.
- `WithheldError` (in `api_file_values.py`) — raised inside the check
  when one part cannot be read whole. It carries the reason; the check
  catches it and withholds the part. The reasons are fixed texts, for
  example "holds more than one document" or "names a key twice".

The three sibling modules hold the parts: `api_file_values.py` has the
`Scrubber` walk over parsed values, the placeholder, the withheld
reasons and the search for JSON documents inside a text value;
`api_file_yaml.py` has the YAML check on the node graph, for a YAML file
and for a YAML example in an HTML page; `api_file_markup.py` has the XML
and HTML parsers and writers, and the check of an example block. The
fail-closed rules for text values are on only for the API file checks
(`Scrubber(strict=True)`). The Salt connector uses the same walk without
them, so its results do not change.

## What counts as a secret name

MEHO's one rule for secret-named fields,
`is_secret_param_name` in `broadcast/events.py`: `password`, `passwd`,
`pwd`, `passphrase`, `apikey`, `kubeconfig` anywhere in the name;
`secret`, `secrets`, `token`, `credential`, `credentials` at the end of
the name; and the pairs `api_key`, `access_key`, `private_key`,
`encryption_key`, `license_key`, `licence_key`, `secret_id`,
`secret_value`, `secret_data`. Names are read in snake, camel and kebab
case. #4780 added the licence key.

The helper adds one rule of its own (`is_secret_name` in
`api_file_values.py`): a name that ends in a session id (`sessionId`,
`session_id`, `X-Session-Id`, `JSESSIONID`) is a secret name too. Some
appliances use the session id as the login token; the Logs session API
returns its token as `sessionId`. The rule stays in the helper, not in
MEHO's general rule, because elsewhere a `session_id` is often MEHO's own
agent session, which is no secret. A name that only starts with
`session` (`sessionTimeout`) is not a secret name.

There is no exception for schema words. The value under a secret name
is replaced even when it is the description of a field (`"password":
{"type": "string"}`) or a path item (`"/reset-password": {...}`). This
loses a little detail; `secrets_replaced` tells the reader how much. In
return, no trick can use an exception: the exception for schema words
is how `{"credentials": {"default": "..."}}` got through a text scan.

## Control flow

1. Pick the parser from the content type, or from the file ending.
2. Parse. JSON with `json`; YAML with PyYAML's safe loader; XML with
   `defusedxml` (a file that declares entities is refused); HTML with
   `html.parser`. A parse error names the kind of error and the line
   and column, never the text there (a YAML error message would quote
   an unknown tag). A YAML value that does not fit its tag (`!!int
   made-up`) fails outside PyYAML's own errors, in the stdlib call that
   reads it, and that error quotes the value; the file is refused with
   the error class only.
3. Replace:
   - JSON and YAML: the whole value of every secret-named key, at any
     depth, also inside lists, whatever it is (text, number, list,
     object, `null`). A YAML alias (`*name`) is the same node as its
     anchor, so the secret is marked on the node graph before the values
     are built, and every place that points at it (anchor, alias, a
     mapping merged with `<<`) gets the placeholder.
   - XML and HTML: the whole content of a secret-named element, and the
     value of a secret-named attribute. An element whose `name`, `id`,
     `key` or `type` attribute is a secret name (`<input type="password"
     value="...">`) loses its content and every attribute value except
     the label that marks it: `<input name="password" id="abc">` comes
     back with `id="[REDACTED:param_name]"`, and `<xsd:element
     name="password" type="xsd:string"/>` loses its `type`. Every
     attribute the parser saw is checked, so a repeated attribute name
     (`name="password" name="normal"`) cannot hide the first one. The
     HTML parser lowercases element and attribute names, so `licenseKey`
     arrives as `licensekey`; HTML names go through
     `is_secret_param_name_lowercased`, which asks whether any casing of
     the name is a secret name. It matches a few more names than the
     exact rule (`passwordless`), which only replaces more.
   - HTML `<script>` blocks: JSON content is checked like a JSON file.
     Any other script content becomes the placeholder; the helper cannot
     parse JavaScript, so it does not return it. `<style>` content is
     free text.
   - An HTML `<pre>` or `<code>` block is an example. Its check fails
     closed. The check joins the text of the block over its child
     elements, as the page wrote it (a page often colours an example
     with `<span>` elements). Then it reads that text in one of three
     ways:
     1. One JSON document, maybe after one request line
        (`POST /api/v2/sessions`). JSON is read whole and strict.
     2. One YAML document (a mapping or a list), maybe after one
        request line.
     3. Plain text: one YAML scalar, such as a shell command, a path or
        a word.

     A JSON or YAML document comes back written from the parsed copy,
     every secret value replaced. The child elements do not come back;
     the request line does. Plain text comes back as written, after
     the checks for any text value (next point). When YAML reads a
     quoted scalar as other words than the text shows (an escape such
     as `\x77`), that value is checked too.
   - A block is withheld whole when:
     - it does not parse as one JSON or YAML document, and is not plain
       text either;
     - it holds more than one document (`---` in YAML, two JSON
       objects);
     - a mapping at any depth names a key twice (the parser would keep
       only the last value);
     - a child element is secret (by its name, a naming attribute or a
       secret-named attribute) or is a `<script>`;
     - it is plain text that names a secret next to a value.
   - Every text value, in every format, is free text that can hold an
     example. Every JSON object in it (or list that holds one) is
     parsed and checked like a JSON file, and written back from the
     parsed copy. A request line before the body does not hide the
     body. The text around the documents goes through the general
     rules. A text value is withheld whole when:
     - its JSON names a key twice in one object, at any depth;
     - a part that starts like a JSON object (`{"`, `[{`) does not
       parse;
     - it names a secret next to a value outside its JSON documents:
       a secret name followed by `:` or `=` (`sessionId: ...`,
       `"licenseKey": ...`, `password=...`). Plain text has no
       structure, so the check cannot tell where such a value ends;
     - it is not valid Unicode (JSON can spell a lone surrogate,
       `\ud800`);
     - the parser cannot finish it within a bounded amount of work.

     A secret name without a value (`the sessionId field`) stays.
   - Any text value that holds a private key in PEM form
     (`-----BEGIN ... PRIVATE KEY-----`), under any name.
4. Write the copy in the same format from the parsed file. JSON is
   compact (no spaces), close to the minified form appliances serve.
   YAML comes from the safe dumper, keys in order. XML keeps every
   namespace declaration on the element that declared it, with the
   prefixes as declared, because WSDL and XSD files use them inside
   attribute values (`type="xsd:string"`) and a child can bind a prefix
   to another namespace; comments, the XML declaration and CDATA markers
   do not come back. HTML comes back without comments.
5. Count every placeholder written.

## What the reads do with it

Each read runs the helper on the whole file before it cuts the text into
parts, so a part boundary cannot hide a secret. The answer carries
`secrets_replaced`; `size` and `sha256` describe the checked copy, and
joining the parts gives the copy. A redirect path goes through
`scrub_query_string`, and its count is added. The Logs read
(`vrli.api_file.get`) also returns `withheld` (how many parts were
withheld) and `withheld_reasons` (each reason with its count).

The size limit stays as before: a file too big for one stored result is
refused with its size, before parsing. The last check stays too: the
read stops when MEHO's general redaction rules (`redaction/patterns.py`)
would still change the copy, for example an `Authorization` header line
in free text. A read that gives those rules as `text_rule` has already
replaced such text, value by value, so this check passes. The `api_key` rule skips a value that already is a
complete `[REDACTED:...]` placeholder, so a replaced value does not set
it off.

## What the reader loses

- The parts give the checked copy, not the bytes the appliance sent:
  the same content, without comments, with secret values replaced.
- A value under a secret name is replaced even when it is a schema or a
  path item. In XML and HTML, an element that a `name`, `id`, `key` or
  `type` attribute marks as secret keeps only that label; its other
  attribute values go too, also a schema `type`.
- Inline JavaScript does not come back. JavaScript, CSS and plain text
  files are refused.
- Free text that is not JSON, such as "the password is abc", cannot be
  parsed; MEHO's general redaction rules check it, as before. Free text
  that gives a secret name a value (`password: abc`) is withheld whole.
- An example block comes back without its child elements (the colour
  markup) and written from the parsed copy, like a file: JSON compact,
  YAML from the safe dumper (comments gone, keys in order, flow lists as
  block lists, the YAML 1.1 reading of a value: `yes` is `true`). A
  YAML flow mapping such as `{id}` comes back as `id: null`.
- A withheld block or text value is gone from the copy. The reasons say
  how many there were and why.

## Dependencies

`json`, `html.parser` and `urllib.parse` from the standard library;
PyYAML (`yaml`, the C loader and dumper when libyaml is present);
`defusedxml`; `is_secret_param_name` from `broadcast/events.py`.

## Known issues

- A YAML file with several documents (`---`) does not parse as one
  document and is refused. OpenAPI files are single documents. A YAML
  example with several documents inside a page is withheld.
- The check reads a secret by its name. Free text that holds a secret
  without a name and without a form the general rules know (`the
  password is abc`) still comes back. The general rules run on it.
- The HTML parser does not close elements the way a browser does
  (`<p>` and `<li>` are not closed by the next one). The written copy
  keeps everything in order, so this changes nesting only.

## References

- Tests: `backend/tests/test_connectors_shared_api_file_secrets.py` (one
  test per trick from the code reviews), and the tests of the three reads.
- The reads: `connectors-vcf-fleet.md`, `connectors-hcx.md`,
  `connectors-vmware-rest.md` (the SSO Admin read), `connectors-srm.md`,
  `connectors-vr.md`.
- The general redaction rules: `redaction.md`.
