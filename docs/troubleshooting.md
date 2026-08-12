# Troubleshooting Guide — IPsec Failover Automation

---

## Lambda is not triggered when tunnels fail

**Symptom:** VPN tunnels go down but routes are not updated and no Lambda invocations
appear in CloudWatch Logs.

**Check 1 — TGW is registered with Network Manager:**

```bash
aws networkmanager get-transit-gateway-registrations \
  --global-network-id <global-network-id> \
  --query "TransitGatewayRegistrations[*].{ARN:TransitGatewayArn,State:State.Code}"
```

The TGW must show `State: AVAILABLE`. If it is `PENDING` or absent, register it
and wait before testing again.

**Check 2 — Management stack is deployed in us-east-1:**

```bash
aws cloudformation describe-stacks \
  --stack-name <management-stack-name> \
  --region us-east-1
```

Network Manager events are published to EventBridge in us-east-1 only. A stack in
any other region will never receive these events.

**Check 3 — EventBridge rule is ENABLED:**

```bash
aws events describe-rule \
  --region us-east-1 \
  --name <management-stack-name>-TunnelStateRule \
  --query "{State:State,EventPattern:EventPattern}"
```

Confirm `State: ENABLED`. Also check that the TGW ARN in the event pattern
exactly matches your TGW ARN (including region and account ID).

**Check 4 — Lambda invoke permission exists:**

```bash
aws lambda get-policy \
  --region us-east-1 \
  --function-name <management-stack-name>-FailoverLambda \
  --query "Policy"
```

There should be a statement allowing `events.amazonaws.com` to invoke the function.
If absent, the EventBridge rule target was not created correctly — redeploy the stack.

---

## Lambda runs but routes are not updated

**Symptom:** CloudWatch Logs show Lambda invocations, but route table entries do not
change after a failover event.

**Check 1 — Read the log reason:**

```bash
aws logs filter-log-events \
  --region us-east-1 \
  --log-group-name /aws/lambda/<management-stack-name>-FailoverLambda \
  --filter-pattern "Doing nothing" \
  --start-time $(date -v-30M +%s000 2>/dev/null || date -d '30 minutes ago' +%s000)
```

Common reasons the Lambda takes no action:

| Log message | Cause |
|------------|-------|
| `The tunnel with OutsideIpAddress … is UP` | One tunnel is still active — correct behavior, failover not needed |
| `Lambda function called for the TGW attachment … but supposed to work with` | Event from a different TGW or attachment; wrong parameters |
| `Got VPN-CONNECTION-IPSEC-UP and fallback is not configured` | Recovery event ignored because `Fallback=no` |

**Check 2 — Verify TGWName tag on TGW attachments:**

The Lambda IAM policy uses a tag condition. Without the tag, all `ec2:ReplaceTransitGatewayRoute`
calls are denied silently (they appear as `AccessDenied` in CloudWatch Logs).

```bash
aws ec2 describe-transit-gateway-attachments \
  --region <tgw-region> \
  --transit-gateway-attachment-ids <TGWAttachmentID1> <TGWAttachmentID2> \
  --query "TransitGatewayAttachments[*].{ID:TransitGatewayAttachmentId,Tags:Tags}"
```

Each attachment must have `Key=TGWName, Value=<TGWName>` matching the CFN parameter.

**Check 3 — Verify TGWName tag on TGW route tables:**

```bash
aws ec2 describe-transit-gateway-route-tables \
  --region <tgw-region> \
  --filters "Name=transit-gateway-id,Values=<tgw-id>" \
  --query "TransitGatewayRouteTables[*].{ID:TransitGatewayRouteTableId,Tags:Tags}"
```

All route tables that the Lambda needs to update must have the same `TGWName` tag.

**Check 4 — Healthcheck invocations log `No routes to vpn-XXX found` but routes exist:**

This indicates the Lambda package pre-dates a bug fix in the healthcheck path. The
original code passed the VPN connection ID (`vpn-XXX`) to `update_static_route`
instead of the TGW attachment ID (`tgw-attach-XXX`). The route search filter
`attachment.transit-gateway-attachment-id` requires an attachment ID, so it never
matched and routes were never updated via the healthcheck.

The real-time `VPN-CONNECTION-IPSEC-DOWN` path was not affected and continued
to work correctly.

**Resolution:** Rebuild the Lambda package from the current source and redeploy:

```bash
./build.sh
# upload Lambda/IPsecManagementLambda_v2.zip to S3 then:
aws lambda update-function-code \
  --region us-east-1 \
  --function-name <management-stack-name>-FailoverLambda \
  --s3-bucket <bucket> \
  --s3-key <prefix>/IPsecManagementLambda_v2.zip
```

**Check 5 — Verify static routes exist:**

The Lambda replaces existing static routes — it does not create new ones.

```bash
aws ec2 search-transit-gateway-routes \
  --region <tgw-region> \
  --transit-gateway-route-table-id <route-table-id> \
  --filters "Name=type,Values=static" \
  --query "Routes[*].{CIDR:DestinationCidrBlock,Attachment:TransitGatewayAttachments[0].TransitGatewayAttachmentId}"
```

If this returns an empty list, no static routes have been created. Add them following
Step 3 in `docs/deployment.md`.

**Check 6 — Look for AccessDenied errors:**

```bash
aws logs filter-log-events \
  --region us-east-1 \
  --log-group-name /aws/lambda/<management-stack-name>-FailoverLambda \
  --filter-pattern "AccessDenied" \
  --start-time $(date -v-1H +%s000 2>/dev/null || date -d '1 hour ago' +%s000)
```

If found, the IAM role is missing a permission or the tag condition is not met.

---

## DynamoDB lock is stuck

**Symptom:** Consecutive Lambda invocations fail immediately with a `DynamoDBLockError`,
or routes are not updated despite the Lambda being triggered.

**Cause:** A previous Lambda execution crashed (timed out or encountered an unhandled
exception) without releasing the DynamoDB lock. The lock has a 60-second lease with
heartbeat renewal. It expires automatically.

**Resolution — wait:**

Wait 60–120 seconds. The lock will expire on its own and subsequent invocations will
succeed.

**Resolution — force-delete the lock:**

Only do this if you are certain no Lambda execution is currently running.

```bash
aws dynamodb delete-item \
  --region us-east-1 \
  --table-name <management-stack-name>-LockTable \
  --key '{"lock_key": {"S": "my_key"}, "sort_key": {"S": "-"}}'
```

Then trigger the healthcheck (Test 2 in `docs/testing.md`) to verify the Lambda
runs cleanly.

---

## Lambda times out (300-second timeout exceeded)

**Symptom:** CloudWatch Logs end with `Task timed out after 300.00 seconds`.

**Causes and actions:**

1. **Large number of TGW route tables:** The Lambda iterates over every route table
   associated with the TGW. A very high number (>50) could approach the timeout.
   Check how many route tables exist:
   ```bash
   aws ec2 describe-transit-gateway-route-tables \
     --region <tgw-region> \
     --filters "Name=transit-gateway-id,Values=<tgw-id>" \
     --query "length(TransitGatewayRouteTables)"
   ```

2. **API throttling:** Heavy concurrent AWS API usage in the account may throttle
   `SearchTransitGatewayRoutes`. Check CloudWatch Logs for `ThrottlingException`.

3. **Lock contention:** Unlikely with `ReservedConcurrentExecutions=1`, but possible
   during the transition period immediately after deployment. The 10-minute scheduled
   healthcheck will detect and correct any missed route updates on the next cycle.

---

## Failback is not working (routes not reverting to primary)

**Symptom:** The primary VPN recovers but routes remain pointing to the failover attachment.

**Check 1 — Fallback is enabled:**

```bash
aws lambda get-function-configuration \
  --region us-east-1 \
  --function-name <management-stack-name>-FailoverLambda \
  --query "Environment.Variables.FallbackSupport"
```

Must return `"yes"`. If `"no"`, redeploy the management stack with `Fallback=yes`.

**Check 2 — Both tunnels on the primary connection are UP:**

The Lambda only reverts when **both** tunnels are UP (not just one).

```bash
aws ec2 describe-vpn-connections \
  --region <tgw-region> \
  --vpn-connection-ids <VPNConnectionID1> \
  --query "VpnConnections[0].VgwTelemetry[*].{IP:OutsideIpAddress,Status:Status}"
```

Both entries must show `Status: UP`. If one is still DOWN, wait for it to establish.

**Check 3 — Scheduled healthcheck rule is ENABLED:**

```bash
aws events describe-rule \
  --region us-east-1 \
  --name <management-stack-name>-HealthCheckRule \
  --query "State"
```

Must return `"ENABLED"`. If the rule is missing, `Fallback` was set to `no` when
the stack was created — redeploy with `Fallback=yes`.

---

## Stack deployment fails: `CAPABILITY_NAMED_IAM` not acknowledged

```bash
aws cloudformation create-stack ... --capabilities CAPABILITY_NAMED_IAM
```

In the CFN console: check **I acknowledge that AWS CloudFormation might create IAM resources with custom names** before clicking **Create stack**.

---

## Finding the Lambda function name for a v1 deployment

For stacks deployed from the original v1 template, the Lambda name is auto-generated.
Find it with:

```bash
aws cloudformation describe-stack-resource \
  --stack-name <your-stack-name> \
  --logical-resource-id IPsecManagementLambda \
  --query "StackResourceDetail.PhysicalResourceId" \
  --output text
```

See `docs/upgrade-v1-to-v2.md` for the full v1 upgrade procedure.
