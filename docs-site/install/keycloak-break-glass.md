# Keycloak break-glass: get admin access back

Use this page when nobody can log in to the Keycloak admin console of the
built-in Keycloak, and you must change something in it. "Break-glass"
means an emergency way in, used rarely and removed again at once.

The chart removes Keycloak's first-start admin after the first run. A
console admin exists on a new install (see
[The console admin](keycloak-realm.md#the-console-admin)). If its login
is lost, or your install has none, follow this page.

## Who may do this

Only a person who can do both of these in the namespace of the install:

- scale the Keycloak StatefulSet, and
- read the Secrets of the chart.

Treat it like a root login. Tell your team before you start. Keycloak
records admin changes in its admin events (see
[Events Keycloak keeps](#events-keycloak-keeps)).

## What you need

- `kubectl` and `jq` on your workstation, logged in to the cluster.
- Two names. Replace them in every command below:
    - `<namespace>`: the namespace of the install.
    - `<release>`: the Helm release name. The StatefulSet is
      `<release>-keycloak`.

Set them once in your shell:

```bash
NS=<namespace>
KC=<release>-keycloak
```

## The secret never appears on a command line

The new password is read without an echo and goes into a Kubernetes
Secret through a pipe. It is never typed as a command argument and never
printed. The one-off pod reads it from the Secret as an environment
variable.

## Steps

### 1. Stop Keycloak

Keycloak's recovery command needs every Keycloak pod stopped. Run while
Keycloak is up, it can fail or leave a broken state. Login to MEHO stops
working until step 4.

```bash
kubectl -n "$NS" scale statefulset "$KC" --replicas=0
kubectl -n "$NS" wait --for=delete pod "$KC-0" --timeout=120s
```

### 2. Make the temporary admin

Type the new password (it is not shown). Use a long random one.

```bash
read -rs -p "Temporary admin password: " TEMP_PW; echo
printf '%s' "$TEMP_PW" | kubectl -n "$NS" create secret generic keycloak-temp-admin \
  --from-file=password=/dev/stdin
unset TEMP_PW
```

Start a one-off pod. It copies the image, the database settings, the
security settings and the pull secrets from the StatefulSet, so it uses
the same database as Keycloak:

```bash
kubectl -n "$NS" get statefulset "$KC" -o json | jq '
  .spec.template.spec as $s
  | {apiVersion: "v1", kind: "Pod",
     metadata: {name: "keycloak-bootstrap-admin"},
     spec: {
       restartPolicy: "Never",
       automountServiceAccountToken: false,
       imagePullSecrets: ($s.imagePullSecrets // []),
       securityContext: $s.securityContext,
       containers: [ $s.containers[0] | {
         name: "bootstrap-admin", image, imagePullPolicy, resources, securityContext,
         env: (.env + [
           {name: "TEMP_ADMIN_NAME", value: "temp-admin"},
           {name: "TEMP_ADMIN_PASSWORD",
            valueFrom: {secretKeyRef: {name: "keycloak-temp-admin", key: "password"}}}
         ]),
         command: ["/opt/keycloak/bin/kc.sh"],
         args: ["bootstrap-admin", "user",
                "--username:env", "TEMP_ADMIN_NAME",
                "--password:env", "TEMP_ADMIN_PASSWORD", "--no-prompt"]
       } ]
     }}' | kubectl -n "$NS" apply -f -

kubectl -n "$NS" wait --for=jsonpath='{.status.phase}'=Succeeded \
  pod/keycloak-bootstrap-admin --timeout=300s
kubectl -n "$NS" logs keycloak-bootstrap-admin
```

The log should say that the admin was created in realm `master`. Then
remove the pod:

```bash
kubectl -n "$NS" delete pod keycloak-bootstrap-admin
```

Need a service account for scripts, not a person? Use
`bootstrap-admin service` with `--client-id:env` and `--client-secret:env`
in the same pod. The secret must come from an environment variable, from
a Secret, in the same way.

### 3. Start Keycloak again

```bash
kubectl -n "$NS" scale statefulset "$KC" --replicas=1
kubectl -n "$NS" rollout status statefulset "$KC" --timeout=300s
```

Wait until the pod is ready. Keycloak keeps the realm and its database as
they were.

### 4. Make your change

Open the admin console at `https://<host>/auth/admin/master/console/` and
log in as `temp-admin`. The address needs `exposeAdmin: true` in the
Helm values (see [The console admin](keycloak-realm.md#the-console-admin)).

Make the change you came for. For a realm that exists already, the
section [A realm that already runs](#a-realm-that-already-runs) below is
a common one.

### 5. Delete the temporary admin

In the admin console, realm `master`: **Users**, `temp-admin`,
**Delete**. Then delete the Secret and check that the login no longer
works:

```bash
kubectl -n "$NS" delete secret keycloak-temp-admin
```

Open the console login in a private window and try `temp-admin` with its
old password. Keycloak must refuse it. If you made a service account,
delete it too: realm `master`, **Clients**, its client ID, **Delete**.

### 6. If a step stops halfway

- Scale Keycloak back to one replica:
  `kubectl -n "$NS" scale statefulset "$KC" --replicas=1`
- Delete what you made: the pod `keycloak-bootstrap-admin`, the Secret
  `keycloak-temp-admin`, and the temporary admin in realm `master` if it
  was already made.
- Run the steps again from step 1.

## A realm that already runs

Keycloak reads the realm file of the chart only at its first start. So a
realm made before the chart carried these settings does not have them.
The chart does not change a running realm. Set them once, as the
temporary admin, in the admin console, realm `meho`.

| Where in the console | Setting | Value |
|---|---|---|
| **Realm settings → Events → User events settings** | Save events | On |
| | Expiration | 180 days |
| **Realm settings → Events → Admin events settings** | Save events | On |
| | Include representation | Off (the request body can hold a secret) |
| | Expiration | 180 days |
| **Realm settings → Security defenses → Brute force detection** | Mode | Lockout temporarily |
| | Max login failures | 5 |
| | Wait increment | 1 minute |
| | Max wait | 10 minutes |
| | Failure reset time | 12 hours |
| **Realm settings → Tokens** | Access Token Lifespan | 5 minutes |

New installs have all of this from the first start. These are the
values in the realm file, as the Admin REST API names them:

```json
{
  "eventsEnabled": true,
  "eventsExpiration": 15552000,
  "adminEventsEnabled": true,
  "adminEventsDetailsEnabled": false,
  "attributes": {"adminEventsExpiration": "15552000"},
  "bruteForceProtected": true,
  "permanentLockout": false,
  "failureFactor": 5,
  "waitIncrementSeconds": 60,
  "maxFailureWaitSeconds": 600,
  "maxDeltaTimeSeconds": 43200,
  "accessTokenLifespan": 300
}
```

## Events Keycloak keeps

On a new install Keycloak saves login events and admin events for 180
days. It does not save the request body of an admin event, because it
can hold a secret. To see who logged in, or who changed the realm, open
**Events** in the realm `meho` of the admin console.

After 5 wrong passwords an account is locked for 1 minute. Each 5 more
wrong passwords add 1 minute, up to 10 minutes. The lock always ends by
itself.

## References

- Keycloak: [Bootstrap admin recovery](https://www.keycloak.org/server/bootstrap-admin-recovery)
- Keycloak: [Server administration guide](https://www.keycloak.org/docs/latest/server_admin/)
  (events, brute force detection)
