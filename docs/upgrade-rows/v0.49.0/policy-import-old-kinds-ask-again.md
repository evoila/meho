## Note
A policy file import that waits for approval at the upgrade may need to be asked again, when the tenant's stored bindings use an earlier kind name (`SA-Operators`, `Approvers`) and the policy file import is on. Its approval then answers "The policy changed after this import was planned. Run the preview again.", and nothing is stored.

## Action
Run the preview of such an import again and ask for it again. No schema action.
