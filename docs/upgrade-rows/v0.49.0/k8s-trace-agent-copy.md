## Note
An agent that reads the trace of a Kubernetes call gets a copy. In it, env values are hidden by the same secret rules as in the direct answer of an operation, and the secret values on container command lines and scripts and in annotations are hidden (the last-applied annotation is cleaned like the object). Plain env values, env names and references, and managedFields stay. A person and a service login read the stored trace as it is.

## Action
None.
