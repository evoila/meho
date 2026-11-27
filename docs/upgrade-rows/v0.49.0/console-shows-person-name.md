## Note
The web console shows a person's name from the login (the `name` claim) in the user menu, on the dashboard and on the account page, instead of the Keycloak user ID. Migration `0192` adds the nullable, encrypted `web_session.display_name` column; sessions that exist stay without a name and show "Your account" until the person signs in again.

## Action
None. People see their name after their next sign-in. A login client that does not send the `name` claim (the `profile` scope) shows "Your account".
