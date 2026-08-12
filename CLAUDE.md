# AWS TGW → Netskope IPsec Automation

## What this project does

Automates IPsec failover between an AWS Transit Gateway (TGW) and Netskope Security Cloud.
When both tunnels on the primary VPN connection fail, a Lambda function updates TGW static
routes to the standby Netskope POP within seconds. Optionally it reverts routes when the
primary recovers (fallback mode).

---

## Architecture

| Stack | Template | Deploy region | Purpose |
|-------|----------|---------------|---------|
| **Management** | `CFN/TGW_IPsec_management_v2.yaml` | **us-east-1** | Failover Lambda, EventBridge rules, DynamoDB lock table. Triggered in real time by AWS Network Manager tunnel-state events. |
| **Provisioning** *(optional)* | `demo/CFN/TGW_IPsec_provisioning.yaml` | TGW's region | Creates Netskope tunnel sites and AWS VPN connections. Use if you do not already have VPN connections established. See `demo/README.md`. |

> The management stack **must** be in **us-east-1** — AWS Network Manager publishes
> tunnel state events to EventBridge in that region regardless of where the TGW lives.

---

## Repository layout

```
CFN/
  TGW_IPsec_management_v2.yaml    # v2 management/failover stack (deploy this)

Lambda/
  lambda_function.py              # failover Lambda — Python 3.13
  python_dynamodb_lock/           # vendored DynamoDB lock library v0.9.1 — DO NOT MODIFY

docs/
  deployment.md                   # step-by-step management stack deployment ← read for fresh installs
  testing.md                      # how to test and validate the deployed solution
  troubleshooting.md              # common failures and resolutions
  upgrade-v1-to-v2.md             # upgrade guide for existing v1 deployments

demo/                             # optional: creates Netskope tunnels + VPN connections from scratch
  CFN/
    TGW_IPsec_provisioning.yaml   # provisioning stack (run before management stack)
  Lambda/
    provisioning_function.py      # Custom Resource Lambda for tunnel provisioning
  docs/
    deployment.md                 # demo-specific deployment guide
    troubleshooting.md            # demo-specific troubleshooting
  README.md                       # demo overview and quick start
  build.sh                        # packages IPsecProvisioningLambda.zip

build.sh                          # packages IPsecManagementLambda_v2.zip for S3 upload
requirements-dev.txt              # dev tools: black, cfn-lint
DEVOPS.md                         # technical deep-dive: Lambda internals, IAM, locking
```

---

## When the user wants to…

| Task | Action |
|------|--------|
| Deploy from scratch | Read **`docs/deployment.md`** |
| Test or validate after deployment | Read **`docs/testing.md`** |
| Diagnose a problem | Read **`docs/troubleshooting.md`** |
| Upgrade an existing v1 stack | Read **`docs/upgrade-v1-to-v2.md`** |
| Understand how the Lambda works | Read **`DEVOPS.md`** |
| Understand a CFN parameter | Read the `Description` field in the relevant template |

---

## Hard constraints — always apply these

1. **Never modify `Lambda/python_dynamodb_lock/`** — vendored third-party library; treat as read-only.
2. **Never hardcode secrets in source code or template defaults.** The management template contains no secrets. The demo provisioning template accepts the Netskope API token and PSK as `NoEcho` parameters and writes them to Secrets Manager. Do not add plaintext secret values to any file.
3. **Lambda runtime is Python 3.13** in both templates and both Lambda functions.
4. **Build artifacts (`*.zip`) are gitignored** — build with `./build.sh` and upload to S3; do not commit.
5. **IAM policy documents must include `Version: "2012-10-17"`** — omitting it causes IAM to silently reject conditions.
6. **All CFN resources must have `Name` and `StackName` tags**.
7. **`cfn-lint` must pass** on both templates before deployment (`pip install cfn-lint`).
8. **Management stack deploys in us-east-1**, not the TGW's region (unless they are the same).
9. **TGW attachments and route tables must be tagged** `TGWName=<TGWName>` — without this the Lambda IAM policy will deny all route updates.
10. **Do not change `ReservedConcurrentExecutions`** on the failover Lambda — concurrent executions produce inconsistent route state.
