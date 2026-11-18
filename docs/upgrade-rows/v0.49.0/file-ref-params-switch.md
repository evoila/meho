## Note
New setting `FILE_REF_PARAMS` (chart: `config.fileRefParams`), off by default. An operation input can carry the schema keyword `x-meho-switch: "file_ref_params"`. While the setting is off, MEHO does not show such an input to agents or in the console, and refuses a call that gives it like an unknown input. A connector whose schema has this keyword with an unknown switch name, or not on an input, does not register. No schema migration.

## Action
No action needed. Leave `FILE_REF_PARAMS` off. A later release says when to turn it on.
