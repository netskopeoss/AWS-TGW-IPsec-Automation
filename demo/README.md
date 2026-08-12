# Demo — Netskope IPsec Tunnel Provisioning

This directory contains a CloudFormation-based reference implementation for
creating the Netskope IPsec tunnel sites and the corresponding AWS Site-to-Site
VPN connections that the main failover stack manages.

Use this if you do not already have VPN connections to Netskope established and
want a fully automated path to stand up the tunnel infrastructure.

---

## What this creates

Deploying `CFN/TGW_IPsec_provisioning.yaml` produces:

| Resource | Count | Notes |
|----------|-------|-------|
| Secrets Manager secrets | 2 | API token + PSK (created from `NoEcho` parameters) |
| Netskope IPsec tunnel sites | 2 | `<SiteName>-primary` and `<SiteName>-failover` |
| AWS Customer Gateways | 2 | IPs sourced from the Netskope API response |
| AWS Site-to-Site VPN Connections | 2 | Attached to your TGW, static routing |
| TGW Attachment IDs | 2 | Returned as stack outputs |

The stack outputs `TGWAttachmentID1` and `TGWAttachmentID2` are used directly
as inputs to the main management stack (`CFN/TGW_IPsec_management_v2.yaml`).

---

## Relationship to the main solution

```
demo/CFN/TGW_IPsec_provisioning.yaml   ← creates the tunnel infrastructure
         │
         │  outputs: TGWAttachmentID1, TGWAttachmentID2
         ↓
CFN/TGW_IPsec_management_v2.yaml       ← monitors tunnels and manages failover
```

The provisioning stack is independent and disposable. The management stack
only needs the two TGW attachment IDs — these can come from the provisioning
stack, from Terraform, or from a manually created VPN connection.

---

## Quick start

```bash
# 1. Build and upload the provisioning Lambda
cd demo
./build.sh
aws s3 cp Lambda/IPsecProvisioningLambda.zip s3://<bucket>/IPsecManagement/

# 2. Deploy the provisioning stack (TGW's region)
aws cloudformation create-stack \
  --region <tgw-region> \
  --stack-name <provisioning-stack-name> \
  --template-body file://CFN/TGW_IPsec_provisioning.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters file://params.json

# 3. Note the outputs
aws cloudformation describe-stacks \
  --region <tgw-region> \
  --stack-name <provisioning-stack-name> \
  --query "Stacks[0].Outputs"
```

See `demo/docs/deployment.md` for the full parameter reference and step-by-step guide.

---

## After provisioning

Once the stack is complete:

1. Tag the TGW attachments and route tables with `Key=TGWName` — required by
   the failover stack's IAM policy (see `docs/deployment.md` in the root).
2. Add TGW static routes pointing to `TGWAttachmentID1`.
3. Deploy the management stack using the attachment IDs from this stack's outputs.
