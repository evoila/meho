## Note
A hosted agent with a `connectors` list now gets these answers when it names a target. When every hit of the exact-name step, or every hit of the alias step, is outside the list, the answer is "no target" (`no_target`) with no close names. An answer never names a target outside the list, and never names its product. The close target names of a "no target" answer leave out the targets outside the list, so there can be fewer than five, or none.

## Action
None. If such an agent needs a target that it does not see, add the target's connector to its `connectors` list.
