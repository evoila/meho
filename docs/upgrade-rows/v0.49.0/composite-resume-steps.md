## Note
When a multi-step operation waits for approval at one of its steps and goes on after the approval, MEHO does not send the steps that already ran again. MEHO stores the steps of the run, with their results, on the approval request and replays them. A step whose end MEHO does not know stops the run with `composite_resume_step_outcome_unknown`. A second step that waits in the same run waits for its own approval, with the steps so far stored on its request.

## Action
None for operators. A script that looked for the error code `composite_resume_multi_gate_unsupported` gets a second approval request instead. Connector authors: run each change of a multi-step operation that can go on after an approval through `run_governed_subop`, so that its resume skips the change and uses its stored result.
