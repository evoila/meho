---
type: lesson
applies_to: product-and-version
status: confirmed
product: Harbor Backup
version: "5.3"
summary: The nightly backup stops at files that are in use.
---

# Backup job fails on locked files

## Symptom

The nightly backup job ends with the state "failed". The log holds the code HB-0073 and a list of files that were open.

## Cause

Programs keep some files open at night, for example mail archives and database files. The job cannot read an open file in the normal mode.

## Fix

1. Open the settings of the backup job.
2. Switch the read mode from "direct" to "snapshot mode".
3. Add the folder for temporary files to the exclude list.
4. Start the job by hand once.

## Check

The next morning the job must show "completed". A few skipped temporary files are fine.
