# Demo Troubleshooting — Netskope IPsec Tunnel Provisioning

This guide covers issues specific to `demo/CFN/TGW_IPsec_provisioning.yaml`.
For failover stack issues see `docs/troubleshooting.md` in the repository root.

---

## Stack fails during creation

**Step 1 — Identify the failing resource:**

In the CloudFormation console, go to the stack **Events** tab and find the first
`CREATE_FAILED` event. Note the logical resource ID.

**Step 2 — Check the provisioning Lambda logs:**

```bash
aws logs tail \
  --region <tgw-region> \
  /aws/lambda/<provisioning-stack-name>-ProvisionerLambda \
  --since 30m
```

---

## Netskope API 401 Unauthorized

The API token is invalid or stored incorrectly. Verify the secret:

```bash
aws secretsmanager get-secret-value \
  --region <tgw-region> \
  --secret-id <provisioning-stack-name>-netskope-api-token \
  --query SecretString \
  --output text
```

The value must be a raw token string, not a JSON object. If the token is wrong,
update the secret value directly in Secrets Manager — do not update the stack
parameter, as CloudFormation will not overwrite an already-created secret from
a `NoEcho` parameter.

---

## Netskope API 400 Bad Request

Common causes:

- **Invalid POP names** — verify `PrimaryPOP` and `FailoverPOP` against the list
  at **Settings → Security Cloud Platform → IPSec → Available POPs**, or via the
  API: `GET /api/v1/ipsec/pops?token=<token>`
- **Site name already exists** — a site with the same name exists in your tenant.
  Delete it in the Netskope UI, then delete and redeploy the stack, or use a
  different `SiteName` parameter.
- **SrcIdentity format** — verify the identity string is accepted by your Netskope
  configuration (typically an IP address or FQDN).

---

## TGW attachment lookup timed out

**Symptom:** Stack fails with:
```
TGW attachment for VPN connection vpn-XXXXX not found after 3 minutes
```

**Cause:** The VPN connection's TGW attachment took longer than expected to appear
in the EC2 API. This is usually transient.

**Resolution:** Delete the stack and redeploy.

If the problem recurs, verify the TGW is available:

```bash
aws ec2 describe-transit-gateways \
  --region <tgw-region> \
  --transit-gateway-ids <tgw-id> \
  --query "TransitGateways[0].State"
```

---

## PSK rejected on VPN connection

**Symptom:** Stack fails on `VPNConnection1` or `VPNConnection2` with an invalid
`PreSharedKey` error.

AWS VPN PSK requirements: 8–64 characters, alphanumeric / `.` / `_` only, must
not begin with `0`.

Update the `<stack-name>-ipsec-psk` secret in Secrets Manager with a compliant
value, then delete and redeploy the stack.

---

## Tunnels created but VPN status remains DOWN

**Checks:**

1. **SrcIdentity** — must match what the Netskope POP expects. Verify with your
   Netskope admin.
2. **PSK** — the same Secrets Manager secret is used for both the Netskope API
   call and the AWS VPN tunnel options. A mismatch only occurs if the secret was
   changed between the two operations.
3. **Firewall / security groups** — confirm UDP 500 and UDP 4500 are permitted
   from the AWS VPN endpoint IP to the Netskope POP gateway IP.

---

## Deleting the stack

Deleting the provisioning stack will:

1. Call the Netskope API to delete both tunnel sites
2. Delete AWS VPN connections, Customer Gateways, and the IAM role
3. Schedule both Secrets Manager secrets for deletion (7-day recovery window)

> **Before deleting:** update or delete the management stack first. Deleting the
> VPN connections makes the TGW attachment IDs the management stack holds invalid.

If the Netskope sites were manually deleted before stack deletion, the
`DELETE /api/v1/ipsec/{id}` calls will return 404 and be silently skipped — the
stack will still complete the delete successfully.
