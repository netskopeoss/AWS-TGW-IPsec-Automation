#!/usr/bin/env bash
# build.sh — Package the management Lambda deployment zip for upload to S3.
#
# Usage:
#   ./build.sh
#
# Output:
#   Lambda/IPsecManagementLambda_v2.zip
#
# Upload before deploying the management stack:
#   aws s3 cp Lambda/IPsecManagementLambda_v2.zip s3://<bucket>/IPsecManagement/
#
# To build the provisioning Lambda (demo/ only):
#   cd demo && ./build.sh

set -euo pipefail

LAMBDA_DIR="$(cd "$(dirname "$0")/Lambda" && pwd)"

echo "Building Lambda packages from: $LAMBDA_DIR"

echo "  Packaging IPsecManagementLambda_v2.zip..."
(
  cd "$LAMBDA_DIR"
  zip -r IPsecManagementLambda_v2.zip \
    lambda_function.py \
    python_dynamodb_lock/
)
echo "  -> Lambda/IPsecManagementLambda_v2.zip"

echo "Done."
