# JSONFlux result preservation

When a registered operation returns a top-level verdict beside a collection
that can spill, it must declare `llm_instructions.result_scalars` for every
verdict field a caller needs after reduction. If it also needs a bounded
top-level identity object, it may declare `result_objects`; full values remain
available through the JSONFlux handle and explicit `result_object_truncations`
names any bounded inline field.
