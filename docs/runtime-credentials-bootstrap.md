# Preserve Connect runtime credentials before publishing source

Connect historically supplied Django and object storage credentials from source
fallbacks. Changing them would invalidate signatures and storage access. This
migration copies the effective existing bytes, without rotation or log output.

An operator creates the empty `connect-runtime-credentials` Secret and narrowly
scoped RBAC from `k8s/runtime-credentials-bootstrap-rbac.yaml`. Refuse an unexpected
existing target; never apply an empty replacement over stored data. First verify whether the canonical CI identity already has pod execution rights.
The production cutover used its existing reviewed `origin-ci-deploy` role and
added no execution permissions. If no such authority exists, a temporary
Role and RoleBinding named `origin-ci-connect-credentials-exec` grants `create`
on `pods/exec` **only for the exact stable current API, worker and beat pod names**.
Prove the grant permits those names and refuses an unrelated pod before dispatch.
Do not grant namespace-wide execution. Inventory the selected pod UIDs privately.

The source-owned `bootstrap-runtime-credentials.yml` CI workflow reads effective
Django settings from every selected replica into process memory, verifies equal
values across all three roles twice, and rejects deployment/pod identity changes.
It writes through stdin using a Secret resourceVersion compare-and-swap and makes
the target immutable. Source or target verification failures stop the workflow.
No credential values or hashes are reported. The workflow always removes the
temporary execution RoleBinding and Role, including after a failed projection;
the operator verifies removal even if the runner itself is interrupted.

After a successful receipt, wire `SECRET_KEY`, `MINIO_ACCESS_KEY` and
`MINIO_SECRET_KEY` to the named Secret in all three chart workloads, then remove
source defaults. Deploy through normal Origin CI. Verify all consumers reference
the Secret and still perform authenticated requests and storage operations. Then
apply `k8s/runtime-credentials-reader-rbac.yaml` to reduce target access to
named Secret `get` only. Named deletion of the already-absent temporary execution
Role/Binding remains solely for interruption-safe cleanup. A partial attempt preserves the
old workloads and credentials; rerunning cannot overwrite a conflicting target.
Rollback the source/chart together to the prior Origin revision if needed; keep
the immutable Secret and archives, and never rotate to repair a bootstrap error.
