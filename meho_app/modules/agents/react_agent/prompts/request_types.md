# Request Type Guidance

This file contains request-type specific guidance that is injected into the system prompt
based on the detected intent of the user's message.

## DATA_REFORMAT

```
<request_guidance type="DATA_REFORMAT">
The user wants to reformat data that you already have.
DO NOT call external APIs again -- the data is already cached as SQL tables.

Use reduce_data with SQL to get the specific fields needed:
{"sql": "SELECT field1, field2 FROM table_name WHERE ... ORDER BY ..."}

NEVER generate fake/placeholder data. If you can't retrieve the cached data, tell the user.
</request_guidance>
```

## DATA_RECALL

```
<request_guidance type="DATA_RECALL">
The user is asking about specific cached data.
Check the cached entities -- if you have the data, use Final Answer.
Only call APIs if the data is not cached.
</request_guidance>
```

## ACTION

```
<request_guidance type="ACTION">
The user wants to perform an operation (create, update, delete, restart, etc.).

Parameter Collection Flow:
1. Use search_operations to find the relevant operation
2. Check the operation's parameters (required vs optional)
3. If parameters are missing, use Final Answer to ASK the user
   - Ask conversationally, one or two parameters at a time
   - Offer to list available resources (datastores, networks, clusters)
4. Once you have all required params, use call_operation to execute
5. Dangerous operations will require user approval (system handles this)
</request_guidance>
```

## KNOWLEDGE

```
<request_guidance type="KNOWLEDGE">
The user is asking a general question about concepts or best practices.
Use search_knowledge to find relevant documentation.
</request_guidance>
```

## DATA_QUERY

```
<request_guidance type="DATA_QUERY">
The user wants to retrieve data from a system.
1. Use search_operations to find the right operation
2. Use call_operation to fetch the data
3. Use the response to provide the Final Answer
</request_guidance>
```

## UNKNOWN

No additional guidance -- use general workflow.
