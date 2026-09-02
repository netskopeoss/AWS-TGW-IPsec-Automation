# DevOps Reference — AWS TGW to Netskope IPsec Automation

This document explains how the solution works internally. It is intended for
engineers responsible for operating, monitoring, or extending the system.

For deployment procedures see `docs/deployment.md`. For testing see `docs/testing.md`.

---

## System overview

The solution keeps TGW static routes pointing at whichever Netskope POP currently
has working IPsec tunnels. It monitors tunnel state via AWS Network Manager events
and updates routes within seconds of detecting a dual-tunnel failure.

One long-running CloudFormation stack:

| Stack | Template | Lifecycle |
|-------|----------|-----------|
| Management | `CFN/TGW_IPsec_management_v2.yaml` | Long-running; failover Lambda active continuously |

The management stack assumes that two AWS Site-to-Site VPN connections to Netskope
POPs already exist and are attached to the TGW. The `demo/` directory contains a
separate provisioning stack (`demo/CFN/TGW_IPsec_provisioning.yaml`) for creating
those VPN connections via the Netskope API — see `demo/README.md`.

---

## Failover Lambda — `Lambda/lambda_function.py`

### Module-level initialisation

```python
TGWRegion     = os.environ["TGWRegion"]
TGWID         = os.environ["TGWID"]
TGWAttachmentID1 = os.environ["TGWAttachmentID1"]   # primary
TGWAttachmentID2 = os.environ["TGWAttachmentID2"]   # failover
DynamoDBLockTable = os.environ["DynamoDBLockTable"]
FallbackSupport   = os.environ["FallbackSupport"]    # "yes" | "no"

ec2              = boto3.client("ec2", region_name=TGWRegion)
dynamodb_resource = boto3.resource("dynamodb")
```

The boto3 clients and environment variables are initialised once per Lambda
container, not per invocation. This avoids re-importing boto3 on every call and
is intentional for performance.

Note that `ec2` is initialised in the Lambda's deployment region (`us-east-1`)
but with `region_name=TGWRegion`. All EC2 API calls are routed to the TGW's
region from a Lambda running in us-east-1.

### Event types

The Lambda handles three `changeType` values, all delivered via EventBridge:

| `changeType` | Source | Trigger condition |
|-------------|--------|-------------------|
| `VPN-CONNECTION-IPSEC-DOWN` | Network Manager | One or both tunnels in a VPN connection reported DOWN |
| `VPN-CONNECTION-IPSEC-UP` | Network Manager | A tunnel in a VPN connection came back UP |
| `VPN-CONNECTION-IPSEC-HEALTHCHECK` | Scheduled EventBridge rule | Every 10 minutes (fallback mode only) |

### Guard clauses

Before taking any action the handler verifies:

1. **TGW ID match** — the event's `transitGatewayArn` must contain the configured
   `TGWID`. Events for other TGWs are ignored (relevant if multiple TGWs share the
   same Network Manager global network).

2. **Attachment ID match** (non-healthcheck events only) — the event's
   `transitGatewayAttachmentArn` must be either `TGWAttachmentID1` or
   `TGWAttachmentID2`. Events for other attachments (VPC attachments, etc.) are
   ignored.

### DOWN handler — failover path

```
Event received: VPN-CONNECTION-IPSEC-DOWN
  │
  ├─ ec2:DescribeVpnConnections(VpnConnectionId)
  │    ├─ Tunnel 1 UP?  → log "one tunnel still UP", return (no action)
  │    └─ Tunnel 2 UP?  → log "one tunnel still UP", return (no action)
  │
  └─ Both DOWN → update_static_route(
         TGWAttachmentID_Current = event attachment,
         TGWAttachmentID_NEW     = the other attachment
     )
```

The DOWN event fires when any tunnel changes state. The Lambda re-checks the live
tunnel state via `DescribeVpnConnections` before acting — a single-tunnel failure
does not trigger a failover because the VPN connection remains functional with one
tunnel active.

### UP handler — fallback path (FallbackSupport=yes only)

```
Event received: VPN-CONNECTION-IPSEC-UP
  │
  ├─ FallbackSupport == "no"? → log, return (no action)
  │
  ├─ ec2:DescribeVpnConnections(VpnConnectionId)
  │    ├─ Tunnel 1 DOWN? → log "still one tunnel down", return (no action)
  │    └─ Tunnel 2 DOWN? → log "still one tunnel down", return (no action)
  │
  └─ Both UP → update_static_route(
         TGWAttachmentID_Current = the other attachment (currently active),
         TGWAttachmentID_NEW     = event attachment (primary recovering)
     )
```

The fallback restores routes to the primary attachment only when **both** its
tunnels are confirmed UP. A single-tunnel recovery is not sufficient.

### HEALTHCHECK handler — sweep path

```
Event received: VPN-CONNECTION-IPSEC-HEALTHCHECK
  │
  ├─ ec2:DescribeTransitGatewayAttachments([TGWAttachmentID1, TGWAttachmentID2])
  │
  └─ For each attachment:
       │   ResourceId              = attachment["ResourceId"]               ← VPN connection ID (vpn-XXX)
       │   current_attachment      = attachment["TransitGatewayAttachmentId"] ← TGW attachment ID (tgw-attach-XXX)
       │
       └─ ec2:DescribeVpnConnections(ResourceId)
            ├─ Either tunnel UP? → continue (attachment healthy)
            └─ Both DOWN?
                 └─ update_static_route(current=current_attachment, new=other)
                    return  ← only one failover per execution
```

> **Important:** `update_static_route` searches TGW routes by
> `attachment.transit-gateway-attachment-id` — it requires a `tgw-attach-XXX` ID.
> `ResourceId` is a VPN connection ID (`vpn-XXX`) and must **not** be passed to
> `update_static_route` directly.

The healthcheck is a safety net for the case where an event-driven invocation
timed out or was otherwise missed. It runs every 10 minutes when `Fallback=yes`.
The handler returns after the first failing attachment to avoid race conditions.

---

## Route update function — `update_static_route`

```python
def update_static_route(TGWID, TGWAttachmentID_Current, TGWAttachmentID_NEW):
```

This is the only function that modifies AWS state. Execution flow:

```
1. Acquire DynamoDB distributed lock (lease_duration=60s)
   │
2. ec2:DescribeTransitGatewayRouteTables(filter: transit-gateway-id=TGWID)
   │  Returns all route tables associated with the TGW
   │
3. For each route table:
   │  ec2:SearchTransitGatewayRoutes(
   │      filter: attachment.transit-gateway-attachment-id=TGWAttachmentID_Current,
   │              type=static
   │  )
   │  For each matching route:
   │      ec2:ReplaceTransitGatewayRoute(
   │          DestinationCidrBlock, RouteTableId,
   │          NewAttachmentId=TGWAttachmentID_NEW
   │      )
   │
4. Release lock
```

**Key behaviour:** The function iterates over every route table belonging to the
TGW. In deployments with multiple TGW route tables (e.g. one per VPC), all tables
are updated atomically within the lock window.

**It does not create routes** — only existing static routes pointing to
`TGWAttachmentID_Current` are replaced. Routes must exist before the first failover.

---

## DynamoDB distributed lock

### Purpose

`ReservedConcurrentExecutions: 1` on the Lambda ensures at most one execution runs
at a time. However, a queued invocation could start immediately after the previous
one completes — the DynamoDB lock is a belt-and-suspenders defence ensuring
inconsistent route states cannot result from rapid back-to-back invocations.

### Lock parameters

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `lock_key` | `"my_key"` | Partition key — single global lock per stack |
| `sort_key` | `"-"` | Range key — library default |
| `lease_duration` | 60 seconds | If the Lambda crashes without releasing, the lock expires after 60 s |
| `expiry_period` | 1200 seconds | DynamoDB TTL — the item is auto-deleted 20 minutes after creation |
| Heartbeat period | 5 seconds (library default) | Periodic DynamoDB write to refresh the lease while execution is live |

### Lock lifecycle

1. `acquire_lock` issues a conditional `PutItem` — fails if an unexpired lock exists
2. A background thread sends heartbeat `UpdateItem` calls every 5 seconds
3. On normal completion, `lock.release()` deletes the item
4. If the Lambda crashes (timeout, OOM, etc.), the heartbeat thread stops; the lock
   expires automatically after `lease_duration` (60 s)

### Manually clearing a stuck lock

If the Lambda consistently fails to acquire the lock after more than 2 minutes,
the lock may be stuck. Force-delete it:

```bash
aws dynamodb delete-item \
  --region us-east-1 \
  --table-name <management-stack-name>-LockTable \
  --key '{"lock_key": {"S": "my_key"}, "sort_key": {"S": "-"}}'
```

Only do this when you are certain no Lambda execution is currently running.

---

## Provisioning Lambda — `demo/Lambda/provisioning_function.py`

The provisioning Lambda is part of the `demo/` stack and is separate from the
failover Lambda described above. See `demo/README.md` and `demo/docs/deployment.md`
for full documentation.

In brief: it is a CloudFormation Custom Resource Lambda that creates and deletes
Netskope IPsec tunnel sites via the Netskope REST API, and polls
`ec2:DescribeTransitGatewayAttachments` to return TGW attachment IDs once VPN
connections have been associated with the TGW.

---

## EventBridge integration

### Real-time rule — `<stack-name>-TunnelStateRule`

```yaml
EventPattern:
  source: [aws.networkmanager]
  detail-type: ["Network Manager Status Update"]
  detail:
    transitGatewayArn:
      - arn:aws:ec2:<TGWRegion>:<AccountId>:transit-gateway/<TGWID>
```

Matches any Network Manager status update for the specific TGW. The `changeType`
field (DOWN / UP) is discriminated inside the Lambda, not in the event pattern —
this avoids needing two separate rules.

AWS Network Manager publishes these events to EventBridge in **us-east-1**,
which is why the management stack must be deployed there regardless of the TGW's
region.

### Scheduled rule — `<stack-name>-HealthCheckRule`

Present only when `Fallback=yes`. Fires every 10 minutes with a hard-coded payload:

```json
{
  "detail": {
    "changeType": "VPN-CONNECTION-IPSEC-HEALTHCHECK",
    "transitGatewayArn": "arn:aws:ec2:<TGWRegion>:<AccountId>:transit-gateway/<TGWID>"
  }
}
```

This is the safety net for missed or dropped events. The most likely scenario it
guards against: a DOWN event triggers the Lambda, the Lambda times out mid-execution
(routes partially updated or not updated), and the UP event arrives while the
previous execution is still marked as running. The healthcheck will detect the
incorrect route state on the next cycle.

---

## IAM design

### Management stack Lambda role — least privilege

```
ec2:SearchTransitGatewayRoutes   ]  on transit-gateway-route-table/*
ec2:ReplaceTransitGatewayRoute   ]  on transit-gateway-attachment/*
                                     CONDITION: ec2:ResourceTag/TGWName == <TGWName>

ec2:DescribeTransitGatewayRouteTables  ]
ec2:DescribeVpnConnections             ]  on *  (describe-only, no Condition required)
ec2:DescribeTransitGatewayAttachments  ]

dynamodb:PutItem    ]
dynamodb:GetItem    ]  on the specific LockTable ARN only
dynamodb:DeleteItem ]
dynamodb:UpdateItem ]

logs:CreateLogGroup   — on account-level log group ARN
logs:CreateLogStream  ]  on /aws/lambda/* log group ARN
logs:PutLogEvents     ]
```

**The `TGWName` tag condition** on the mutating EC2 actions is what scopes the
Lambda's blast radius to one TGW. Without tags on the TGW attachments and route
tables, all EC2 write calls will return `AccessDenied` — this is by design.

---

## Operational parameters

| Parameter | Value | Why |
|-----------|-------|-----|
| `ReservedConcurrentExecutions` | 1 | Concurrent executions produce split-brain route state. This is non-negotiable — do not raise it. |
| Lambda `Timeout` | 300 s | Covers the worst case: lock acquisition wait + describe all route tables + replace all routes + API throttle retries |
| DynamoDB lock `lease_duration` | 60 s | Short enough that a crashed execution unblocks the next one quickly |
| DynamoDB lock `expiry_period` | 1200 s | DynamoDB TTL to auto-clean lock items if `release` is never called |
| DynamoDB PITR | Enabled | Recovery option if the lock table is accidentally corrupted |
| `LOGLEVEL` | `INFO` | Change to `DEBUG` temporarily via the Lambda console environment variable for verbose route-table enumeration logging |

---

## Monitoring

### CloudWatch Logs

Log group: `/aws/lambda/<management-stack-name>-FailoverLambda` (us-east-1)

Key log patterns to alert on:

| Pattern | Severity | Meaning |
|---------|----------|---------|
| `Both connections for … are down!` | Info | Failover in progress — expected during an outage |
| `Replacing route` | Info | Route being updated — confirm the correct attachment ID |
| `AccessDenied` | Error | IAM condition not met — check TGWName tags |
| `DynamoDBLockError` | Warning | Lock contention — check for stuck lock |
| `Task timed out` | Error | Lambda timeout — check route table count and API throttling |
| `Provisioning failed` | Error | Provisioning Lambda — check Netskope API response |

### Useful queries

**Find all failover events in the last 24 hours:**

```bash
aws logs filter-log-events \
  --region us-east-1 \
  --log-group-name /aws/lambda/<management-stack-name>-FailoverLambda \
  --filter-pattern "Both connections" \
  --start-time $(date -v-24H +%s000 2>/dev/null || date -d '24 hours ago' +%s000)
```

**Find all route replacements:**

```bash
aws logs filter-log-events \
  --region us-east-1 \
  --log-group-name /aws/lambda/<management-stack-name>-FailoverLambda \
  --filter-pattern "Replacing route"
```

---

## Vendored library — `Lambda/python_dynamodb_lock/`

Version 0.9.1 by Mohan Kishore (2018). Uses only Python stdlib and botocore —
no third-party dependencies. Compatible with Python 3.13.

**Do not modify this directory.** It is intentionally kept identical to upstream
to simplify future updates. If a bug is found, update the whole directory from
the upstream source rather than patching in place.

The library key APIs used:

```python
DynamoDBLockClient(dynamodb_resource, table_name=..., lease_duration=..., expiry_period=...)
lock_client.acquire_lock("my_key")   # blocks until lock available or raises DynamoDBLockError
lock.release()                        # deletes the DynamoDB item
lock_client.close()                   # stops the heartbeat thread
```

---

## Extending the solution

### Adding more TGW route tables

No code change required. The Lambda automatically discovers all route tables for
the TGW via `DescribeTransitGatewayRouteTables`. Tag new route tables with
`TGWName=<TGWName>` and add static routes pointing to `TGWAttachmentID1`.

### Supporting multiple TGWs

Deploy one management stack per TGW. Each stack has its own Lambda, EventBridge
rules, DynamoDB table, and attachment IDs. The Lambda guard clauses ensure each
instance only responds to events for its own TGW.

### Changing the failback behaviour after deployment

Update the management stack with `Fallback=yes|no`. This changes the `FallbackSupport`
environment variable and enables/disables the scheduled EventBridge rule. No Lambda
code change is required.

### Rotating the Netskope API token

The token is managed by the `demo/` provisioning stack (see `demo/docs/troubleshooting.md`).
Update the secret value directly in Secrets Manager — the provisioning Lambda reads
the secret at invocation time. No stack update or Lambda redeploy is needed.
