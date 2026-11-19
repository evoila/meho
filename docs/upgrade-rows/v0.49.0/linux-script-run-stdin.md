## Note
`linux.script.run` sends the script and its values to the host on standard input, also without sudo. The host's process list shows `bash -s` for such a call. A script that reads standard input gets end-of-file.

## Action
None.
