## Note
`postgres.query` refuses, before it connects, a statement that names a server catalog that can hold a stored password, a connection string with a password, or the SQL text of other sessions: `pg_authid`, `pg_shadow`, `pg_subscription`, `pg_user_mapping`, `pg_user_mappings`, `user_mapping_options`, `pg_stat_activity`, `pg_stat_get_activity`, `pg_stat_get_backend_activity`, `pg_stat_statements` and `pg_stat_statements_info`. It also refuses `table_to_xml`, `schema_to_xml`, `database_to_xml` and their variants. This holds for every login, also on a target with `extras.allow_superuser_query = true`. Plain reads of other tables and the curated `postgres.*` operations run at once.

## Action
Use `postgres.activity` to see the sessions of a server. If a sensor runs `postgres.query` with one of these names, it reads `unknown`: change its statement.
