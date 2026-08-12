# AWS TGW → Netskope IPsec Failover Automation

Automates IPsec failover between an AWS Transit Gateway (TGW) and Netskope Security Cloud.
When both tunnels on the primary VPN connection fail, a Lambda function updates TGW static
routes to the standby Netskope POP — typically within 60–120 seconds of detection. Routes
optionally revert to the primary when it recovers (fallback mode).

---

## How it works

AWS Network Manager monitors VPN tunnel state and publishes events to EventBridge in
us-east-1. A Lambda function responds to those events, checks live tunnel state via the
EC2 API, and replaces TGW static routes to fail over or fall back between two Netskope POPs.
A scheduled EventBridge rule runs the same check every 10 minutes as a safety net.

A DynamoDB table provides a distributed lock so that only one Lambda execution can update
routes at a time, preventing split-brain route state during rapid event sequences.

---

## What's in this repository

| Path | Purpose |
|------|---------|
| `CFN/TGW_IPsec_management_v2.yaml` | Management stack — deploy this |
| `Lambda/lambda_function.py` | Failover Lambda (Python 3.13) |
| `Lambda/python_dynamodb_lock/` | Vendored DynamoDB lock library — do not modify |
| `build.sh` | Packages `Lambda/IPsecManagementLambda_v2.zip` for S3 upload |
| `docs/` | Full deployment, testing, troubleshooting, and upgrade documentation |
| `demo/` | Optional provisioning stack — creates Netskope tunnel sites and VPN connections from scratch |
| `DEVOPS.md` | Technical reference: Lambda internals, IAM design, locking, monitoring |

---

## Quick start

**If you already have Netskope VPN connections attached to your TGW:**

1. Build and upload the Lambda package:
   ```bash
   ./build.sh
   aws s3 cp Lambda/IPsecManagementLambda_v2.zip s3://<your-bucket>/IPsecManagement/
   ```
2. Tag TGW attachments and route tables with `Key=TGWName, Value=<your-tgw-name>`
3. Create static routes in the TGW route table(s) pointing to the primary attachment
4. Register the TGW with AWS Network Manager (if not already done)
5. Deploy `CFN/TGW_IPsec_management_v2.yaml` in **us-east-1**

Full parameter reference and CLI commands: **[docs/deployment.md](docs/deployment.md)**

**If you need to create VPN connections first**, deploy the provisioning stack in `demo/`
before the steps above — see [demo/README.md](demo/README.md).

---

## Documentation

| Document | Contents |
|----------|---------|
| [docs/deployment.md](docs/deployment.md) | Step-by-step deployment guide with CLI commands |
| [docs/testing.md](docs/testing.md) | Validation tests including failover and failback |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Common failures and resolutions |
| [docs/upgrade-v1-to-v2.md](docs/upgrade-v1-to-v2.md) | Upgrade guide for existing v1 deployments |
| [DEVOPS.md](DEVOPS.md) | Lambda internals, IAM design, DynamoDB locking, monitoring |

---

## Key design decisions

- **us-east-1 only**: The management stack must be deployed in us-east-1 — AWS Network
  Manager publishes tunnel state events to EventBridge in that region regardless of where
  the TGW lives.
- **Single-execution concurrency**: `ReservedConcurrentExecutions: 1` on the Lambda, backed
  by a DynamoDB distributed lock, ensures routes are never updated by concurrent executions.
- **Tag-scoped IAM**: The Lambda IAM role can only modify routes on TGW attachments and route
  tables tagged `TGWName=<your-tgw-name>`. Without this tag, all route updates fail with
  `AccessDenied` — this is intentional and limits blast radius to one TGW per stack.
- **No route creation**: The Lambda replaces existing static routes only. Routes must exist
  before the first failover event.

---

## Requirements

- AWS CLI v2
- Python 3 with `zip` (for `build.sh`)
- An S3 bucket to store the Lambda deployment package
- IAM permissions to create CloudFormation stacks with `CAPABILITY_NAMED_IAM`
- AWS Network Manager global network with the TGW registered (state: `AVAILABLE`)
