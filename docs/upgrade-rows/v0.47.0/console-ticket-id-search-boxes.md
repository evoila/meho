## Note
The ticket id rule also covers the console: the ticket id search boxes of the agent runs, scheduler, audit and approvals pages, and the approvals "Approve group in order" window. A refused value now gets a 422 answer. The page then lists nothing and shows why the value was refused. A blank box still means no filter.

## Action
None. Operators who search with a ticket id that holds a tab, a line break, a zero-width space or more than 256 characters see the rule text and type the id again.
