# Files by reference: MEHO records, approves and checks files (decision)

**Status:** Proposed, waits for the owner's confirmation. This record becomes the decision only when the owner approves its pull request in their own words. The owner made these decisions on 17 November 2026.
**Date:** 2026-11-17

Nothing in this record is built yet.

## The decision in short

- **Files by reference.** MEHO keeps a record of each file: name, size, SHA-256 checksum (a fingerprint of the bytes), place (where the bytes are stored), who may use it, and when it expires. MEHO does not carry large files through its own API.
- **Approval is for the exact bytes.** A person approves one file with one checksum. The run checks the checksum again before it uses the file.
- **Build in steps.** Proofs in a test setup come first. The build follows, in steps.
- **A store ships with MEHO.** A small S3-type store (a store that speaks the common object-store interface) is a chart option (a setting of the install), off by default, with one key per tenant. The store is part of the first build phase. A customer without a file store can use large files from the start. A laptop uploads straight to the store with short-lived signed addresses.
- **Files that come back from a target hold secrets.** Only the owner of the file and a tenant admin may fetch it. It has a short life (proposed: 7 days by default, 30 days at most).
- **A file approval is not a standing permission.** A file approval is never a standing permission for many targets. Each run on a target is approved, by a person or by one run approval that names the exact operation, target and checksum.
- **Signed addresses are made at run time.** MEHO never stores them and never writes them to a log.
- **A store on a private network is a per-tenant choice.** It is never switched on for the whole install.
- **The approval card shows small text files.** It shows the content of small text files (proposed: up to 64 KiB).
- **MEHO does not scan files.** The approval card, the docs and the help of the `files` command will say so.
- **A dangerous file write on a site behind a satellite runner stays impossible.** There, only a target that pulls the file itself can work. See [satellite-write-path.md](satellite-write-path.md).

## Words used

- **File record:** the entry MEHO keeps for a file. It holds the name, size, checksum, place, allowed users and expiry. It does not hold the bytes.
- **Checksum:** a short value computed from the bytes of a file (SHA-256). If one byte changes, the value changes.
- **Store:** the S3-type service where the bytes live.
- **Signed address:** a web address that allows one upload or one download for a short time, without a login.
- **Satellite runner:** a remote runner for sites that the central MEHO cannot reach directly.

## How a file moves

1. A person or an agent asks to upload a file. MEHO makes a file record and a signed address for the store.
2. The laptop sends the bytes straight to the store. The bytes do not pass the MEHO API.
3. MEHO marks the record ready.
4. An operation that uses the file names the record. The approval is for the record with its checksum. The approval card shows a small text file.
5. When the run uses the file, the checksum is checked again. A different checksum stops the run.
6. A file that comes back from a target is stored the same way, with its owner and an expiry, and is fetched through a signed address made at that moment.

## Why

- Large bytes in the API would use memory and time in every MEHO copy and would not fit the narrow agent surface.
- A reference is small. It can be recorded, approved, audited and checked again.
- An approval is only worth something if it covers what actually runs. The checksum ties the two together.
- Files from targets often hold secrets, such as backups and exports. So access is narrow and life is short.
- A customer should not have to build a file store before the first large file. A store that ships with MEHO removes that step, and stays off for those who do not want it.

## Limits we accept

- MEHO does not scan files. A file can still hold something harmful or a secret. The person who approves must judge the file.
- The approval card shows the content only of small text files (proposed: up to 64 KiB).
- Each target needs its own approval, or a run approval that names each target. This costs time for jobs on many targets.
- A signed address works for anyone who holds it until it expires. So it is short-lived, made at run time, and never stored or logged.
- Sites behind a satellite runner cannot take a dangerous file write.

## What stays open

- **Which store ships.** It is chosen after an upload test. No product is chosen yet.
- **How targets and laptops reach the shipped store.** This is decided after a test on a private network.

## Where it is described

- This record is the first public description. The build steps will add user docs and code docs, and this part will then point to them.
- The satellite write rule: [satellite-write-path.md](satellite-write-path.md).
