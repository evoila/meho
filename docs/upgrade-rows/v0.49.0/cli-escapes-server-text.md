## Note
In its text output (without `--json`) the CLI shows control characters in text from an API description, a target system or an uploaded session as visible escapes, for example `\x1b` for ESC. This holds for `meho operation search`, `meho operation groups`, `meho operation preview`, the failure text of `meho operation call` and of the vendor commands (not `meho vcf-automation about`), the `meho audit trace` list, `meho targets describe`, and `meho sessions show` and `list`. Other text is printed as it is. The `--json` output is unchanged.

## Action
None for most installs. A script that reads the text output of these commands and needs the exact bytes of a field gets the visible escape for each control character: switch it to `--json` and read the field there.
