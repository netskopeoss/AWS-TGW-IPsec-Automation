# Netskope-TGW-management - Failover automation solution for AWS TGW - Netskope IPsec tunnels

The solution deploys Lambda function that's been triggered by the CloudWatch event rule for IPsec tunnel status change. When called by the CloudWatch event rule, the function checks if the both tunnel to the Netskope PoP are down, and if so, it scans all the TGW route tables for the static route pointing to the TGW VPN attachments for the corresponding Site-to-Site VPN connection, and replaces them with the alternative static route to the failover PoP.

You must deploy this solution in us-west-2 region and enable AWS Transit Gateway Network Manager. AWS Transit Gateway Network Manager is the global AWS service that monitors the status of IPsec Site-to-Site VPN connections and uses Amazon CloudWatch in the us-west-2 region for alerting and logging. This solution used AWS Transit Gateway events in the us-west-2 region to monitor your TGW in any region on the same account. Cross-account monitoring is not currently supported.

You need to deploy one instance of the solution per TGW. You also can customize the lambda function to work with multiple TGWs or to treat a group of TGW attachments differently. 

The solution assumes all TGW VPC attachments have the static route pointing to the same TGW VPN attachments. Therefore, all VPCs connected to this TGW will be utilizing the same IPsec tunnel between AWS TGW and Netskope PoP. The single tunnel bandwidth is limited to 250 Mbps. You may scale this solution by spliting your VPCs  to a number of groups and to route traffic for each group for its own redundant IPsec Site-to-Site VPN connections. The Lambda function can be customized to support this approach. 

You may enable of disable fallback functionality. If enabled, the Lambda function will revert static routes to the primary Netskope PoP if both of its IPsec tunnels are up.

In addition to checking and updating routes when IPsec tunnel status changes, the same Lambda function being triggered every 10 minutes to check that there are no routes left pointing to the IPsec connection which is currently down. This is to prevent the unlikely situation when IPsec connections were intensively bouncing, and the this caused a race condition between Lambda function executions which caused the last execution time out. Note, that only one Lambda function execution can run at any point of time to avoid inconsistent results. Concurrency has been controlled using DynamoDB table also being created by this solution.

The CloudFormation stack creates the IAM role used by the Lambda function. This role implemented based on the least privilege access control model. To limit access to only TGW Attachments, therefore to the Route Tables that belong to this specific TGW, it uses IAM policy condition checking the Tags on the TGW Attachments. You must tag each of your TGW attachments with the tag "Key"="TGWName", "Value"="Your TGW Name". For example, "Key"="TGWName", "Value"="MyProdTGW-us-east-1".


Usage: 
1. Clone this repository to your machine.
git clone https://github.com/dyuriaws/Netskope-TGW-management.git

If you're not going to modify the Lambda function, you can deploy the solution using TGW_IPsec_management.yaml CloudFormation template. 

2. Change the region to US West (Oregon)us-west-2.
3. In the AWS CloudFormation management console click Create Stack and choose With new resources (standard).
4. Choose Upload a template file and click on Choose file.
5. Choose the TGW_IPsec_management.yaml from the disk and click Next.
6. Enter the stack name and the parameters for your deployment:

TGWRegion - The AWS region where your TGW is deployed

TGWName   - TGW name that will be used for access control. Your all TGW attachments must have an attribute "Key"="TGWName", "Value"="This parameter". For example, "MyProdTGW-us-east-1"

TGWID     - TGW ID. For example, tgw-01234567890123456

TGWAttachmentID1 - TGW attachment ID for the first (primary) VPN. For example, tgw-attach-01234567890123456

TGWAttachmentID2 - TGW attachment ID for the second (failover) VPN. For example, tgw-attach-01234567890123456

Fallback         -  Yes/No for the route fallback support to the TGWAttachmentID1 if both of this IPsec tunnels became active.

8. Click Next.
9. Optionally, enter the Tags for your CloudFormation stack and click Next.
10. Acknowledge creating IAM resources and click Create stack.

