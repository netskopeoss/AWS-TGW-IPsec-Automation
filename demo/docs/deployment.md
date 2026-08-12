# Demo Deployment Guide — Netskope IPsec Tunnel Provisioning

This guide covers deploying `demo/CFN/TGW_IPsec_provisioning.yaml` to create
Netskope IPsec tunnel sites and the corresponding AWS Site-to-Site VPN connections.
The TGW attachment IDs produced by this stack feed directly into the main
management stack.

After completing this guide, follow `docs/deployment.md` in the repository root
to deploy the failover management stack.

---

## Prerequisites

### AWS

- A Transit Gateway deployed in your target region
- An S3 bucket to hold the Lambda deployment package
- IAM permissions to create CloudFormation stacks with `CAPABILITY_NAMED_IAM`

### Netskope

- Admin API token with IPsec site management permissions
  - Console: **Settings → Tools → REST API → New Token**
- Two Netskope POP identifiers — find valid POPs:
  - Console: **Settings → Security Cloud Platform → IPSec → Available POPs**
  - API: `GET https://<tenant>.goskope.com/api/v1/ipsec/pops?token=<token>`
- A pre-shared key (PSK):
  - 8–64 characters
  - Alphanumeric, periods (`.`), underscores (`_`) only
  - Must **not** begin with `0`

---

## Step 1 — Build and upload the provisioning Lambda

Run from the `demo/` directory:

```bash
./build.sh

aws s3 cp Lambda/IPsecProvisioningLambda.zip \
  s3://<your-bucket>/IPsecManagement/
```

---

## Step 2 — Deploy the provisioning stack

Deploy in the **TGW's region**.

### Via CloudFormation console (recommended for sensitive parameters)

1. **CloudFormation → Create stack → Upload a template file**
2. Upload `demo/CFN/TGW_IPsec_provisioning.yaml`
3. Fill in all parameters — `NetskopeApiToken` and `PreSharedKey` are masked as you type
4. Acknowledge IAM capabilities and click **Create stack**

### Via AWS CLI with a parameter file

Using a parameter file keeps the API token and PSK out of shell history:

```bash
cat > /tmp/demo-params.json <<'EOF'
[
  {"ParameterKey": "TGWRegion",        "ParameterValue": "<tgw-region>"},
  {"ParameterKey": "TGWID",            "ParameterValue": "<tgw-id>"},
  {"ParameterKey": "TGWName",          "ParameterValue": "<tgw-name>"},
  {"ParameterKey": "NetskopeHostname", "ParameterValue": "<tenant>.goskope.com"},
  {"ParameterKey": "NetskopeApiToken", "ParameterValue": "<your-api-token>"},
  {"ParameterKey": "SiteName",         "ParameterValue": "<site-base-name>"},
  {"ParameterKey": "PrimaryPOP",       "ParameterValue": "<primary-pop>"},
  {"ParameterKey": "FailoverPOP",      "ParameterValue": "<failover-pop>"},
  {"ParameterKey": "SrcIdentity",      "ParameterValue": "<src-identity>"},
  {"ParameterKey": "PreSharedKey",     "ParameterValue": "<your-psk>"},
  {"ParameterKey": "LambdaS3Bucket",   "ParameterValue": "<your-bucket>"},
  {"ParameterKey": "LambdaS3Key",      "ParameterValue": "IPsecManagement/IPsecProvisioningLambda.zip"}
]
EOF

aws cloudformation create-stack \
  --region <tgw-region> \
  --stack-name <provisioning-stack-name> \
  --template-body file://demo/CFN/TGW_IPsec_provisioning.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters file:///tmp/demo-params.json

rm /tmp/demo-params.json
```

### Parameter reference

| Parameter | Example | Notes |
|-----------|---------|-------|
| `TGWRegion` | `us-east-1` | Region where your TGW lives |
| `TGWID` | `tgw-01234567890123456` | From TGW console |
| `TGWName` | `MyProdTGW-us-east-1` | Used for resource tagging |
| `NetskopeHostname` | `mytenant.goskope.com` | No `https://` prefix |
| `NetskopeApiToken` | *(masked)* | Stored in Secrets Manager by this stack |
| `SiteName` | `aws-tgw-us-east-1` | Base name; `-primary` / `-failover` appended |
| `PrimaryPOP` | `jfk1` | First-choice Netskope POP |
| `FailoverPOP` | `bos1` | Standby Netskope POP |
| `SrcIdentity` | `203.0.113.10` | IKE identity agreed with Netskope |
| `PreSharedKey` | *(masked)* | Stored in Secrets Manager by this stack |
| `Bandwidth` | `100` | Mbps; default 100 |
| `Encryption` | `AES256-CBC` | Default |
| `BGPAsn` | `65000` | Required even for static routing |
| `LambdaS3Bucket` | `my-artifacts` | S3 bucket from Step 1 |
| `LambdaS3Key` | `IPsecManagement/IPsecProvisioningLambda.zip` | Default |

### What the stack creates

1. Two Secrets Manager secrets from the `NoEcho` parameters (API token + PSK)
2. Two Netskope IPsec tunnel sites via the Netskope REST API:
   - `<SiteName>-primary` — primary POP first, failover POP as backup
   - `<SiteName>-failover` — failover POP first, primary POP as backup
3. Two AWS Customer Gateways using the POP gateway IPs from the API response
4. Two AWS Site-to-Site VPN Connections attached to the TGW (static routing)
5. A lookup Custom Resource polls until both TGW attachment IDs are available

Stack creation takes approximately **5–15 minutes**.

---

## Step 3 — Record the outputs

```bash
aws cloudformation describe-stacks \
  --region <tgw-region> \
  --stack-name <provisioning-stack-name> \
  --query "Stacks[0].Outputs" \
  --output table
```

| Output | Use |
|--------|-----|
| `TGWAttachmentID1` | `TGWAttachmentID1` parameter in the management stack |
| `TGWAttachmentID2` | `TGWAttachmentID2` parameter in the management stack |
| `VPNConnectionID1` | Reference / troubleshooting |
| `VPNConnectionID2` | Reference / troubleshooting |
| `NetskopeSite1Id` | Retain for Netskope support cases |
| `NetskopeSite2Id` | Retain for Netskope support cases |

---

## Step 4 — Verify in Netskope

1. Log into the Netskope admin UI
2. Go to **Settings → Security Cloud Platform → IPSec**
3. Confirm two tunnel sites appear: `<SiteName>-primary` and `<SiteName>-failover`
4. Both should show as enabled

---

## Next steps

Continue with `docs/deployment.md` in the repository root to:

- Tag the TGW attachments and route tables with `Key=TGWName`
- Add TGW static routes pointing to `TGWAttachmentID1`
- Register the TGW with Network Manager
- Deploy the failover management stack using the attachment IDs above
