# IPsec Failover Stack — Upgrade Guide (v1 to v2)

This document describes the changes introduced in v2 of the IPsec Management stack
and the steps required to update an existing deployment.

---

## What changed

### Lambda runtime and code

The Lambda function runtime has been updated from **Python 3.8 to Python 3.13**. The
following code issues were also fixed:

| Issue | Detail |
|-------|--------|
| `logging.os.environ` | Fixed to `os.environ` — the original worked by coincidence via a CPython internal import |
| Missing `import datetime` | Now explicitly imported rather than leaking in via a wildcard import |
| `exit()` calls | Replaced with `return` — `exit()` raises `SystemExit` in Lambda which is semantically incorrect |
| `from __future__ import print_function` | Removed — Python 2 compatibility shim, unnecessary in Python 3 |
| Dead code in `update_static_route` | Removed a duplicate `search_transit_gateway_routes` call that was never consumed |
| Loop style | `for i in range(len(...))` replaced with direct iteration |

### CloudFormation template

| Area | Change |
|------|--------|
| **Runtime** | `python3.8` → `python3.13` |
| **S3 source** | Hardcoded `yd-source-us-west-2` Mapping replaced with `LambdaS3Bucket` and `LambdaS3Key` parameters — you now supply your own bucket |
| **Parameter validation** | `AllowedPattern` added for `TGWID` and attachment IDs; `MinLength` on required string fields |
| **Parameter groups** | Metadata `ParameterGroups` added for cleaner console UX |
| **IAM policies** | Added `Version: "2012-10-17"` to all policy documents; removed erroneous EC2 ARN from the DynamoDB policy; scoped EC2 ARNs to `${AWS::AccountId}` |
| **DynamoDB** | Point-in-time recovery (PITR) enabled; `TableName` added |
| **Lambda** | `ReservedConcurrentExecutions: 1` added to enforce single-execution concurrency at the CFN level (previously only enforced by the DynamoDB lock) |
| **IAM role** | `RoleName` and `Description` added; redundant `DependsOn` removed |
| **Tags** | `Name` and `StackName` tags added to all resources |
| **EventBridge rules** | `Name` added to both rules; redundant `DependsOn` removed |
| **Outputs** | Descriptions updated |

---

## Before you begin

1. **Build and upload the Lambda deployment package.**

   The zip is named `IPsecManagementLambda_v2.zip` (rather than the original
   `IPsecManagementLambda.zip`) to avoid ambiguity if both versions exist in the
   same S3 bucket. The original zip was sourced from the upstream author's bucket
   `yd-source-us-west-2`; v2 requires your own bucket.

   ```bash
   ./build.sh
   aws s3 cp Lambda/IPsecManagementLambda_v2.zip s3://<your-bucket>/IPsecManagement/
   ```

2. **Have your S3 bucket name ready.** You will need to supply `LambdaS3Bucket` and
   `LambdaS3Key` as new parameters during the stack update. All other existing
   parameters are carried over automatically.

---

## Update procedure

> **If you only want to update the Lambda code and runtime** without a full CFN stack
> update, see [Option C — Lambda CLI only](#option-c--lambda-cli-only) below.

### Option A — CloudFormation console

1. Open the **CloudFormation** console and select your existing stack.
2. Click **Update** → **Replace current template**.
3. Upload `CFN/TGW_IPsec_management_v2.yaml`.
4. On the parameters page, all existing parameters will show **"Use existing value"** —
   leave them as-is. Only fill in the two new parameters:
   - **`LambdaS3Bucket`** — your S3 bucket name
   - **`LambdaS3Key`** — e.g. `IPsecManagement/IPsecManagementLambda_v2.zip`
5. Review the change set. Expected changes:
   - Lambda function updated (runtime + code source)
   - DynamoDB table updated (PITR added)
   - IAM role updated (policy corrections)
   - EventBridge rules updated (names added)
6. Execute the change set.

### Option B — AWS CLI

Pass `UsePreviousValue=true` for all existing parameters and only supply values for
the two new ones:

```bash
aws cloudformation update-stack \
  --stack-name <your-stack-name> \
  --template-body file://CFN/TGW_IPsec_management_v2.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters \
    ParameterKey=TGWRegion,UsePreviousValue=true \
    ParameterKey=TGWID,UsePreviousValue=true \
    ParameterKey=TGWName,UsePreviousValue=true \
    ParameterKey=TGWAttachmentID1,UsePreviousValue=true \
    ParameterKey=TGWAttachmentID2,UsePreviousValue=true \
    ParameterKey=Fallback,UsePreviousValue=true \
    ParameterKey=LambdaS3Bucket,ParameterValue=<your-s3-bucket> \
    ParameterKey=LambdaS3Key,ParameterValue=IPsecManagement/IPsecManagementLambda_v2.zip
```

### Option C — Lambda CLI only

Use this if you want to update only the Lambda code and runtime without touching the
rest of the CFN stack (DynamoDB, IAM, EventBridge rules). This is faster but leaves
the stack template at v1 — the CFN console will show the stack as drifted.

**Step 1 — Find the Lambda function name.**

For stacks deployed from the original v1 template the function name is auto-generated
and will not follow the v2 `<stack-name>-FailoverLambda` convention. Look it up with:

```bash
aws cloudformation describe-stack-resource \
  --stack-name <your-stack-name> \
  --logical-resource-id IPsecManagementLambda \
  --query "StackResourceDetail.PhysicalResourceId" \
  --output text
```

**Step 2 — Upload the zip and update the function code:**

```bash
./build.sh
aws s3 cp Lambda/IPsecManagementLambda_v2.zip s3://<your-bucket>/IPsecManagement/

aws lambda update-function-code \
  --function-name <function-name-from-step-1> \
  --s3-bucket <your-bucket> \
  --s3-key IPsecManagement/IPsecManagementLambda_v2.zip
```

**Step 3 — Update the runtime to Python 3.13:**

```bash
aws lambda update-function-configuration \
  --function-name <function-name-from-step-1> \
  --runtime python3.13
```

### Option D — Lambda console (zip upload)

Use this if you prefer a point-and-click workflow over the CLI. Like Option C,
this leaves the stack template at v1 and the CFN console will show the stack as
drifted after you finish.

**Step 1 — Build the zip.**

```bash
./build.sh
```

This produces `Lambda/IPsecManagementLambda_v2.zip` in the project directory.

**Step 2 — Upload the new code.**

1. Open the **Lambda** console and navigate to the function. If you do not know
   the function name, look it up with the CLI command in Option C Step 1.
2. On the **Code** tab, click **Upload from** → **.zip file**.
3. Select `Lambda/IPsecManagementLambda_v2.zip` and click **Save**.

**Step 3 — Update the runtime.**

1. On the **Code** tab, scroll to **Runtime settings** and click **Edit**.
2. Set **Runtime** to **Python 3.13**.
3. Click **Save**.

The change takes effect immediately — no deployment or stack update needed.

---

## Testing the update

After the update completes:

1. **Confirm the runtime** in the Lambda console — should show Python 3.13.
2. **Send a test event** from the Lambda console using the healthcheck payload:
   ```json
   {
     "detail": {
       "changeType": "VPN-CONNECTION-IPSEC-HEALTHCHECK",
       "transitGatewayArn": "arn:aws:ec2:<region>:<account-id>:transit-gateway/<tgw-id>"
     }
   }
   ```
   The function should complete without error. Check CloudWatch Logs for the output.
3. **Confirm concurrency limit** — the Lambda console should show Reserved concurrency = 1.
4. **Confirm PITR** — the DynamoDB table should show Point-in-time recovery as enabled.

---

## Rehearsing the upgrade in a test account

The original template is preserved at `CFN/TGW_IPsec_management.yaml`.

> **Known issues with the v1 template when deploying outside us-west-2:**
>
> 1. **S3 region mismatch** — The v1 template hardcodes the Lambda zip source as
>    `yd-source-us-west-2` (a bucket in us-west-2). Deploying the stack in us-east-1
>    causes a SigV4 region mismatch: Lambda cannot retrieve the zip and the stack
>    rolls back with `AuthorizationHeaderMalformed`. To work around this, update the
>    `Mappings.SourceCode.General.S3Bucket` value in the template and upload the
>    original `Lambda/IPsecManagementLambda.zip` to your own bucket.
>
> 2. **AllowedValues validation failure** — Several parameters (`TGWRegion`, `TGWID`,
>    `TGWName`, `TGWAttachmentID1`, `TGWAttachmentID2`) have `Default: ""` while their
>    `AllowedValues` list does not include `""`. CloudFormation validates defaults
>    against `AllowedValues` and rejects the template with
>    `Parameter '<Name>' must be one of AllowedValues`. Remove or update the `Default`
>    field on those parameters before deploying.
>
> 3. **Deprecated runtime** — The v1 template specifies `python3.8`, which AWS no
>    longer supports for new Lambda deployments. Substitute `python3.11` or later.

1. Deploy a corrected copy of `CFN/TGW_IPsec_management.yaml` (with the issues above
   resolved) as a new stack in a test account.
2. Follow the update procedure above using `CFN/TGW_IPsec_management_v2.yaml`.
3. Verify the change set matches the expected changes listed above.
4. Execute and confirm a successful update.

---

## Rollback

CloudFormation will automatically roll back the stack on update failure. To manually
roll back to v1, retrieve the original v1 template from your git history:

```bash
git show HEAD~1:CFN/TGW_IPsec_management.yaml > /tmp/TGW_IPsec_management_v1.yaml
```

Then update the stack:

```bash
aws cloudformation update-stack \
  --stack-name <your-stack-name> \
  --template-body file:///tmp/TGW_IPsec_management_v1.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters \
    ParameterKey=TGWRegion,UsePreviousValue=true \
    ParameterKey=TGWID,UsePreviousValue=true \
    ParameterKey=TGWName,UsePreviousValue=true \
    ParameterKey=TGWAttachmentID1,UsePreviousValue=true \
    ParameterKey=TGWAttachmentID2,UsePreviousValue=true \
    ParameterKey=Fallback,UsePreviousValue=true
```

> **Note:** The v1 template sources the Lambda zip from the upstream author's bucket
> `yd-source-us-west-2`. This will fail if deploying in a region other than us-west-2
> due to a SigV4 region mismatch. Update the `Mappings.SourceCode.General.S3Bucket`
> value to point to your own bucket and upload the original
> `Lambda/IPsecManagementLambda.zip` there before running the rollback.
