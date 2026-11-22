---
type: lesson
applies_to: product-and-version
status: confirmed
product: Atlas DNS
version: "3.3"
summary: Add a new DNS record to a zone.
---

# Add a new DNS record

## Goal

A new server needs a name in the company zone.

## Steps

1. Open the zone in Atlas DNS.
2. Choose "Add record" and pick the type, for example A.
3. Enter the name and the address.
4. Set the TTL. 3600 seconds is a good default for a new name.
5. Save the record.

## Check

Ask the resolver for the new name. The answer should come back at once, because no old answer is cached.

## Note

Do not add a name that already exists as another type. The save is refused.
