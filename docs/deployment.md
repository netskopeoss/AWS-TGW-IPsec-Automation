# Deployment Guide — AWS TGW to Netskope IPsec Automation

This guide covers deploying `CFN/TGW_IPsec_management_v2.yaml` — the failover
management stack. If you are upgrading an existing v1 installation see
`docs/upgrade-v1-to-v2.md` instead.

> **No VPN connections yet?** If you do not already have AWS Site-to-Site VPN
> connections to Netskope established, deploy the provisioning stack first:
> see `demo/README.md` and `demo/docs/deployment.md`. That stack outputs the
> `TGWAttachmentID1` and `TGWAttachmentID2` values you will need here.

---

## Overview

```
  ./build.sh  →  S3 bucket  →  Lambda/IPsecManagementLambda_v2.zip
                                         │
  (pre-existing)                         │
  AWS Site-to-Site VPN connections       │
  attached to TGW                        │
    TGWAttachmentID1 (primary)           │
    TGWAttachmentID2 (failover)          │
                                         ↓
                        CFN/TGW_IPsec_management_v2.yaml  (us-east-1)
                          Failover Lambda + EventBridge rules + DynamoDB
                                         │
                                         ↓
                        Network Manager events trigger Lambda on tunnel failures
```

> The management stack **must** be deployed in **us-east-1** — AWS Network Manager
> publishes tunnel state events to EventBridge in that region regardless of where
> the TGW lives.

---

## Prerequisites

### AWS

- Two AWS Site-to-Site VPN connections attached to your Transit Gateway:
  - `TGWAttachmentID1` — the primary VPN connection TGW attachment
  - `TGWAttachmentID2` — the failover VPN connection TGW attachment
  - Both connections must use static routing (BGP is not used by this solution)
- AWS Network Manager with a Global Network, and the TGW registered in it:
  - Console: **Network Manager → Global Networks → Register Transit Gateway**
  - The TGW registration must be `AVAILABLE` before the failover stack can receive events
- TGW attachments and route tables tagged with `Key=TGWName, Value=<TGWName>`:
  - The failover Lambda's IAM policy uses this tag to scope route mutations
  - Without this tag, all route update attempts will fail with `AccessDenied`
- Static routes in the TGW route table(s) pointing to `TGWAttachmentID1`:
  - The Lambda replaces existing static routes — it does not create new ones
- An S3 bucket (any region) to store the Lambda deployment package
- IAM permissions to create CloudFormation stacks with `CAPABILITY_NAMED_IAM`

### Local tools

```
bash, zip       — for build.sh
aws cli v2      — for S3 upload and CloudFormation deploy
```

---

## Step 1 — Build and upload the management Lambda

From the repository root:

```bash
./build.sh

aws s3 cp Lambda/IPsecManagementLambda_v2.zip \
  s3://<your-bucket>/IPsecManagement/
```

---

## Step 2 — Tag TGW attachments and route tables

The failover Lambda's IAM policy restricts `SearchTransitGatewayRoutes` and
`ReplaceTransitGatewayRoute` to resources tagged with `TGWName`. Without this tag,
all route update attempts will be denied with `AccessDenied`.

### Tag the TGW attachments

```bash
aws ec2 create-tags \
  --region <tgw-region> \
  --resources <TGWAttachmentID1> <TGWAttachmentID2> \
  --tags Key=TGWName,Value=<tgw-name>
```

### Tag the TGW route tables

Apply the same tag to every TGW route table the Lambda needs to update:

```bash
# List route tables for your TGW
aws ec2 describe-transit-gateway-route-tables \
  --region <tgw-region> \
  --filters "Name=transit-gateway-id,Values=<tgw-id>" \
  --query "TransitGatewayRouteTables[*].TransitGatewayRouteTableId"

# Tag each route table
aws ec2 create-tags \
  --region <tgw-region> \
  --resources <route-table-id-1> [<route-table-id-2> ...] \
  --tags Key=TGWName,Value=<tgw-name>
```

---

## Step 3 — Add TGW static routes (if not already present)

Create static routes in the TGW route table(s) pointing to the **primary** attachment
for the destination CIDRs that should route through Netskope. The failover Lambda
updates these existing routes — it does not create new ones.

```bash
aws ec2 create-transit-gateway-route \
  --region <tgw-region> \
  --transit-gateway-route-table-id <route-table-id> \
  --destination-cidr-block 0.0.0.0/0 \
  --transit-gateway-attachment-id <TGWAttachmentID1>
```

Repeat for each destination CIDR and each route table that should fail over.

---

## Step 4 — Register TGW with Network Manager (if not done)

```bash
aws networkmanager register-transit-gateway \
  --global-network-id <global-network-id> \
  --transit-gateway-arn arn:aws:ec2:<tgw-region>:<account-id>:transit-gateway/<tgw-id>
```

Wait until the registration state shows `AVAILABLE`:

```bash
aws networkmanager get-transit-gateway-registrations \
  --global-network-id <global-network-id> \
  --query "TransitGatewayRegistrations[?contains(TransitGatewayArn, '<tgw-id>')].State"
```

---

## Step 5 — Deploy the management stack

Deploy `CFN/TGW_IPsec_management_v2.yaml` in **us-east-1**.

### Via AWS CLI

```bash
aws cloudformation create-stack \
  --region us-east-1 \
  --stack-name <management-stack-name> \
  --template-body file://CFN/TGW_IPsec_management_v2.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters \
    ParameterKey=TGWRegion,ParameterValue=<tgw-region> \
    ParameterKey=TGWID,ParameterValue=<tgw-id> \
    ParameterKey=TGWName,ParameterValue=<tgw-name> \
    ParameterKey=TGWAttachmentID1,ParameterValue=<primary-attachment-id> \
    ParameterKey=TGWAttachmentID2,ParameterValue=<failover-attachment-id> \
    ParameterKey=Fallback,ParameterValue=yes \
    ParameterKey=LambdaS3Bucket,ParameterValue=<your-bucket> \
    ParameterKey=LambdaS3Key,ParameterValue=IPsecManagement/IPsecManagementLambda_v2.zip
```

Set `Fallback=no` if you do not want automatic reversion to the primary tunnel after recovery.

### Via CloudFormation console

1. **CloudFormation → Create stack → Upload a template file**
2. Upload `CFN/TGW_IPsec_management_v2.yaml`
3. Fill in all parameters
4. Acknowledge IAM capabilities and click **Create stack**

### Parameter reference

| Parameter | Example | Notes |
|-----------|---------|-------|
| `TGWRegion` | `us-east-1` | Region where your TGW lives |
| `TGWID` | `tgw-01234567890123456` | From TGW console |
| `TGWName` | `MyProdTGW-us-east-1` | Must match the tag applied in Step 2 |
| `TGWAttachmentID1` | `tgw-attach-…` | Primary VPN connection TGW attachment |
| `TGWAttachmentID2` | `tgw-attach-…` | Failover VPN connection TGW attachment |
| `Fallback` | `yes` | `yes` = revert to primary when it recovers |
| `LambdaS3Bucket` | `my-artifacts` | Bucket from Step 1 |
| `LambdaS3Key` | `IPsecManagement/IPsecManagementLambda_v2.zip` | Default |

---

## Step 6 — Verify the deployment

```bash
# Confirm Lambda runtime and timeout
aws lambda get-function-configuration \
  --region us-east-1 \
  --function-name <management-stack-name>-FailoverLambda \
  --query "{Runtime:Runtime,Timeout:Timeout}"
# Expected: Runtime=python3.13, Timeout=300

# Confirm reserved concurrency (get-function-configuration does not include this)
aws lambda get-function-concurrency \
  --region us-east-1 \
  --function-name <management-stack-name>-FailoverLambda \
  --query "ReservedConcurrentExecutions"
# Expected: 1

# Confirm EventBridge rules are ENABLED
aws events list-rules \
  --region us-east-1 \
  --name-prefix <management-stack-name> \
  --query "Rules[*].{Name:Name,State:State}"

# Confirm DynamoDB PITR is enabled
aws dynamodb describe-continuous-backups \
  --region us-east-1 \
  --table-name <management-stack-name>-LockTable \
  --query "ContinuousBackupsDescription.PointInTimeRecoveryDescription.PointInTimeRecoveryStatus"
# Expected: ENABLED
```

Then run the healthcheck smoke test from `docs/testing.md`.

---

## Deployment checklist

- [ ] Lambda zip built and uploaded to S3
- [ ] TGW attachments tagged: `Key=TGWName, Value=<tgw-name>`
- [ ] TGW route tables tagged: `Key=TGWName, Value=<tgw-name>`
- [ ] Static routes exist in TGW route table(s) pointing to `TGWAttachmentID1`
- [ ] TGW registered with Network Manager (state: AVAILABLE)
- [ ] Management stack deployed in us-east-1 and status: CREATE_COMPLETE
- [ ] Lambda runtime = Python 3.13, reserved concurrency = 1
- [ ] EventBridge rules: ENABLED
- [ ] Healthcheck test passed (see `docs/testing.md`)
