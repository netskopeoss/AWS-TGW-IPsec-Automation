#!/usr/bin/env bash
# demo/build.sh — Package the provisioning Lambda zip for upload to S3.
#
# Usage (run from the demo/ directory):
#   ./build.sh
#
# Output:
#   demo/Lambda/IPsecProvisioningLambda.zip
#
# Upload before deploying the provisioning stack:
#   aws s3 cp Lambda/IPsecProvisioningLambda.zip s3://<bucket>/IPsecManagement/

set -euo pipefail

LAMBDA_DIR="$(cd "$(dirname "$0")/Lambda" && pwd)"

echo "Building Lambda package from: $LAMBDA_DIR"

echo "  Packaging IPsecProvisioningLambda.zip..."
(
  cd "$LAMBDA_DIR"
  zip -r IPsecProvisioningLambda.zip \
    provisioning_function.py
)
echo "  -> Lambda/IPsecProvisioningLambda.zip"

echo "Done."
