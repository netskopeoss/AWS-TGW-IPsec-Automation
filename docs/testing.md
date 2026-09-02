# Testing Guide — IPsec Failover Automation

This guide covers validation after a fresh deployment. Run the tests in order —
each builds on the previous.

---

## Before you start

Gather these values from your deployment:

| Value | Where to find it |
|-------|-----------------|
| Management stack name | CloudFormation console (us-east-1) |
| TGW region | Management stack parameter |
| TGW ARN | `arn:aws:ec2:<tgw-region>:<account-id>:transit-gateway/<tgw-id>` |
| TGWAttachmentID1 | Management stack parameter (primary VPN attachment) |
| TGWAttachmentID2 | Management stack parameter (failover VPN attachment) |
| Route table ID(s) | TGW console → Route Tables |
| VPN Connection IDs | AWS console: VPC → Site-to-Site VPN Connections |

The Lambda function name is `<management-stack-name>-FailoverLambda`.

---

## Test 1 — VPN connection state

Confirm that the AWS VPN connections are in the correct state before testing failover.

```bash
aws ec2 describe-vpn-connections \
  --region <tgw-region> \
  --vpn-connection-ids <VPNConnectionID1> <VPNConnectionID2> \
  --query "VpnConnections[*].{ID:VpnConnectionId,State:State,Tunnels:VgwTelemetry[*].{IP:OutsideIpAddress,Status:Status}}"
```

Each VPN connection should have `State: available` and at least one tunnel with `Status: UP`.
If all tunnels show `Status: DOWN`, the VPN connections have not established yet — check the
Netskope admin UI to confirm the tunnel sites were created correctly.

---

## Test 2 — Lambda healthcheck 

Invokes the Lambda with a synthetic healthcheck event. The Lambda reads real VPN tunnel state
but does not modify routes unless tunnels are actually down.

```bash
aws lambda invoke \
  --region us-east-1 \
  --function-name <management-stack-name>-FailoverLambda \
  --cli-binary-format raw-in-base64-out \
  --payload '{
    "detail": {
      "changeType": "VPN-CONNECTION-IPSEC-HEALTHCHECK",
      "transitGatewayArn": "arn:aws:ec2:<tgw-region>:<account-id>:transit-gateway/<tgw-id>"
    }
  }' \
  /tmp/response.json && cat /tmp/response.json
```

**Expected output:** `null` (Lambda returns `None`).

Any error response or exception means there is a configuration problem — check the
CloudWatch Logs immediately.

**Read the logs:**

```bash
aws logs tail \
  --region us-east-1 \
  /aws/lambda/<management-stack-name>-FailoverLambda \
  --since 5m
```

With both tunnels UP you should see lines like:

```
The tunnel with OutsideIpAddress X.X.X.X is UP for the VPN connection vpn-XXXXXXXXX
```

If the Lambda calls `update_static_route` during this test, it means one or both VPN
connections have both tunnels DOWN. Investigate the VPN state before proceeding.

---

## Test 3 — Route table baseline

Confirm all static routes in the TGW route table point to `TGWAttachmentID1` (primary)
before testing failover:

```bash
aws ec2 search-transit-gateway-routes \
  --region <tgw-region> \
  --transit-gateway-route-table-id <route-table-id> \
  --filters "Name=type,Values=static" \
  --query "Routes[*].{CIDR:DestinationCidrBlock,Attachment:TransitGatewayAttachments[0].TransitGatewayAttachmentId,State:State}" \
  --output table
```

All routes should show `TGWAttachmentID1` in the Attachment column.

---

## Test 4 — Failover test

> **This test disrupts active traffic through the primary VPN connection.**
> Run in a maintenance window or non-production environment.

### Step 1 — Bring down the primary tunnel

> **Note on Netskope site disable/delete:** Disabling or deleting a Netskope IPsec
> site via the admin UI or API does **not** immediately terminate the existing IKE SA.
> The AWS VPN connection will continue to show `Status: UP` in `VgwTelemetry` until
> the IKE SA lifetime expires (up to 8 hours) or DPD triggers a teardown. Use the
> PSK mismatch method below for reliable, fast test results.

**Option A — PSK mismatch (recommended for testing):**

Change the Pre-Shared Key on both AWS VPN tunnels to a value that does not match
the Netskope site. AWS will recycle the tunnel endpoint; any IKE renegotiation
attempt will fail, and the tunnel will report DOWN after the DPD timeout (~30 s).

```bash
# Change T1 PSK
aws ec2 modify-vpn-tunnel-options \
  --region <tgw-region> \
  --vpn-connection-id <VPNConnectionID1> \
  --vpn-tunnel-outside-ip-address <T1-outside-ip> \
  --tunnel-options '{"PreSharedKey": "TestFailover.PSK.01"}'

# Wait for VPN to return to "available" before modifying T2
# (modifying T2 while T1 is still being recycled causes UnsupportedOperation)
aws ec2 wait vpn-connection-available \
  --region <tgw-region> \
  --vpn-connection-ids <VPNConnectionID1>

# Change T2 PSK
aws ec2 modify-vpn-tunnel-options \
  --region <tgw-region> \
  --vpn-connection-id <VPNConnectionID1> \
  --vpn-tunnel-outside-ip-address <T2-outside-ip> \
  --tunnel-options '{"PreSharedKey": "TestFailover.PSK.01"}'
```

Restore the original PSK afterward (see Step 6 in the failback procedure).

**Option B — Disable via Netskope admin UI:**

1. Go to **Settings → Security Cloud Platform → IPSec**
2. Find `<site-name>-primary` and disable or delete it

> This method may take the IKE SA lifetime to fully terminate the tunnel on the AWS
> side. It is less reliable for testing than Option A.

**Option C — Disable via Netskope API:**

```bash
curl -X PATCH "https://<tenant>.goskope.com/api/v1/ipsec/<site1-id>?token=<token>" \
  -H "Content-Type: application/json" \
  -d '{"enable": false}'
```

Find the Netskope site ID in the Netskope admin UI under **Settings → Security Cloud Platform → IPSec**, or from the outputs of whichever stack/tool created your tunnel sites.

Same caveat as Option B applies.

### Step 2 — Wait for Network Manager to detect the failure

For organic tunnel failures (DPD timeout), AWS Network Manager emits a
`VPN-CONNECTION-IPSEC-DOWN` event to EventBridge, which triggers the Lambda within
**60–120 seconds** of both tunnels going DOWN.

> **Option A (PSK mismatch) does not trigger real-time events.** A PSK change via
> `modify-vpn-tunnel-options` is an administrative operation — AWS Network Manager
> treats it differently from an organic IKE/DPD failure and does not emit a
> `VPN-CONNECTION-IPSEC-DOWN` event. When using Option A, skip to Step 3 and invoke
> the Lambda manually to trigger failover.

> **If real-time events do not fire for Options B/C either:** Verify TGW registration
> with Network Manager — the TGW must be registered and in `AVAILABLE` state:
> ```bash
> aws networkmanager get-transit-gateway-registrations \
>   --global-network-id <global-network-id> \
>   --query "TransitGatewayRegistrations[*].{ARN:TransitGatewayArn,State:State.Code}"
> ```

### Step 3 — Trigger manually

**Required when using Option A (PSK mismatch).** Optional for Options B/C if you
prefer not to wait for the real-time event.

Invoke the Lambda with the healthcheck event (Test 2). The Lambda will detect that
both tunnels are down and update routes.

### Step 4 — Verify routes switched to failover

```bash
aws ec2 search-transit-gateway-routes \
  --region <tgw-region> \
  --transit-gateway-route-table-id <route-table-id> \
  --filters "Name=type,Values=static" \
  --query "Routes[*].{CIDR:DestinationCidrBlock,Attachment:TransitGatewayAttachments[0].TransitGatewayAttachmentId}" \
  --output table
```

All static routes should now show `TGWAttachmentID2`.

### Step 5 — Check the execution log

```bash
aws logs filter-log-events \
  --region us-east-1 \
  --log-group-name /aws/lambda/<management-stack-name>-FailoverLambda \
  --filter-pattern "Replacing route" \
  --start-time $(date -v-10M +%s000 2>/dev/null || date -d '10 minutes ago' +%s000)
```

You should see one log line per route that was updated, e.g.:

```
Replacing route 10.0.0.0/8 to tgw-attach-<id2> in TGW route table tgw-rtb-<id>
```

---

## Test 5 — Failback test (Fallback=yes only)

If the management stack was deployed with `Fallback=yes`, the Lambda reverts routes
to the primary when both its tunnels recover.

### Step 1 — Restore the primary tunnel

**If you used Option A (PSK mismatch):** Restore the original PSK on both tunnels.

```bash
# Restore T1 PSK
aws ec2 modify-vpn-tunnel-options \
  --region <tgw-region> \
  --vpn-connection-id <VPNConnectionID1> \
  --vpn-tunnel-outside-ip-address <T1-outside-ip> \
  --tunnel-options '{"PreSharedKey": "<original-psk>"}'

aws ec2 wait vpn-connection-available \
  --region <tgw-region> \
  --vpn-connection-ids <VPNConnectionID1>

# Restore T2 PSK
aws ec2 modify-vpn-tunnel-options \
  --region <tgw-region> \
  --vpn-connection-id <VPNConnectionID1> \
  --vpn-tunnel-outside-ip-address <T2-outside-ip> \
  --tunnel-options '{"PreSharedKey": "<original-psk>"}'
```

**If you used Option B or C (Netskope site disable):** Re-enable the Netskope tunnel
site via the admin UI or API.

### Step 2 — Wait for both tunnels to report UP

Both tunnels must be UP (not just one). AWS Network Manager will emit
`VPN-CONNECTION-IPSEC-UP` after detecting recovery.

### Step 3 — Verify routes reverted

```bash
aws ec2 search-transit-gateway-routes \
  --region <tgw-region> \
  --transit-gateway-route-table-id <route-table-id> \
  --filters "Name=type,Values=static" \
  --output table
```

Routes should return to `TGWAttachmentID1`.

---

## CloudWatch Logs reference

**Log group:** `/aws/lambda/<management-stack-name>-FailoverLambda` (us-east-1)

**Follow live:**

```bash
aws logs tail \
  --region us-east-1 \
  /aws/lambda/<management-stack-name>-FailoverLambda \
  --follow
```

**Key messages and their meanings:**

| Log message | Meaning |
|------------|---------|
| `Got event VPN-CONNECTION-IPSEC-DOWN` | Real-time down event received |
| `Got event VPN-CONNECTION-IPSEC-UP` | Real-time recovery event received |
| `Both connections for vpn-XXXXX are down!` | Both tunnels confirmed down — failover proceeding |
| `Replacing route X.X.X.X/X to tgw-attach-…` | A static route is being updated |
| `The tunnel with OutsideIpAddress … is UP` | At least one tunnel still UP — no action |
| `Doing nothing, exiting..` | Normal; Lambda took no action |
| `Got VPN-CONNECTION-IPSEC-UP and fallback is not configured` | Fallback=no; recovery event ignored |
| `Lambda function called for the TGW attachment … but supposed to work with` | Event is for a different attachment — ignored |

---

## Confirming the scheduled healthcheck

If `Fallback=yes`, a scheduled EventBridge rule runs the Lambda every 10 minutes.
Confirm it is working by checking for healthcheck invocations in CloudWatch Logs:

```bash
aws logs filter-log-events \
  --region us-east-1 \
  --log-group-name /aws/lambda/<management-stack-name>-FailoverLambda \
  --filter-pattern "VPN-CONNECTION-IPSEC-HEALTHCHECK" \
  --start-time $(date -v-15M +%s000 2>/dev/null || date -d '15 minutes ago' +%s000)
```

You should see at least one invocation in the last 15 minutes.
