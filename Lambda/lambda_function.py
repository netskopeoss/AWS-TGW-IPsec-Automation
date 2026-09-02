import datetime
import json
import logging
import os

import boto3
import botocore
from botocore.exceptions import ClientError

from python_dynamodb_lock.python_dynamodb_lock import (
    DynamoDBLockClient,
    DynamoDBLockError,
)

TGWRegion = os.environ["TGWRegion"]
TGWID = os.environ["TGWID"]
TGWAttachmentID1 = os.environ["TGWAttachmentID1"]
TGWAttachmentID2 = os.environ["TGWAttachmentID2"]
DynamoDBLockTable = os.environ["DynamoDBLockTable"]
FallbackSupport = os.environ["FallbackSupport"]

logger = logging.getLogger()
logger.setLevel(os.environ["LOGLEVEL"])

ec2 = boto3.client("ec2", region_name=TGWRegion)
dynamodb_resource = boto3.resource("dynamodb")


def lambda_handler(event, context):
    """Handle TGW IPsec tunnel status changes and update static routes on failover.

    Responds to three event types emitted by AWS Network Manager via EventBridge:

    - ``VPN-CONNECTION-IPSEC-DOWN``: Both tunnels on a VPN connection are down.
      Fails over static routes from the affected TGW attachment to the alternate
      attachment.
    - ``VPN-CONNECTION-IPSEC-UP``: Both tunnels on a VPN connection are back up.
      If fallback is enabled, restores static routes to the primary TGW attachment.
    - ``VPN-CONNECTION-IPSEC-HEALTHCHECK``: Periodic sweep injected by a scheduled
      EventBridge rule. Fails over any attachment whose both tunnels are down.

    Args:
        event: EventBridge event dict. Must contain ``detail.changeType`` and
            ``detail.transitGatewayArn``. Non-healthcheck events must also contain
            ``detail.transitGatewayAttachmentArn`` and ``detail.vpnConnectionArn``.
        context: Lambda context object (unused).
    """
    logger.info("Got event " + json.dumps(event))

    eventreason = event["detail"]["changeType"]
    event_transitGatewayArn = event["detail"]["transitGatewayArn"]
    event_TGWID = event_transitGatewayArn.split("/")[1]
    if event_TGWID != TGWID:
        logger.error(
            "Lambda function called for the TGW "
            + event_TGWID
            + " but supposed to work only with TGW "
            + TGWID
            + ". Exiting"
        )
        return

    if eventreason != "VPN-CONNECTION-IPSEC-HEALTHCHECK":
        event_transitGatewayAttachmentArn = event["detail"][
            "transitGatewayAttachmentArn"
        ]
        event_transitGatewayAttachment = event_transitGatewayAttachmentArn.split("/")[1]
        if (
            event_transitGatewayAttachment != TGWAttachmentID1
            and event_transitGatewayAttachment != TGWAttachmentID2
        ):
            logger.error(
                "Lambda function called for the TGW attachment "
                + event_transitGatewayAttachment
                + " but supposed to work with the attachments "
                + TGWAttachmentID1
                + " and "
                + TGWAttachmentID2
                + ". Exiting"
            )
            return

        vpnConnectionArn = event["detail"]["vpnConnectionArn"]
        VpnConnectionId = vpnConnectionArn.split("/")[1]
        logger.info(
            "Got event " + eventreason + " for VPNConnectionId : " + VpnConnectionId
        )

    if eventreason == "VPN-CONNECTION-IPSEC-UP":
        if FallbackSupport == "no":
            logger.info("Got VPN-CONNECTION-IPSEC-UP and fallback is not configured..")
            logger.info("Doing nothing, exiting..")
            return

        vpn_connections_response = ec2.describe_vpn_connections(
            VpnConnectionIds=[VpnConnectionId]
        )
        if (
            vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][0]["Status"]
            == "DOWN"
        ):
            logger.info(
                "The tunnel with OutsideIpAddress "
                + vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][0][
                    "OutsideIpAddress"
                ]
                + " is DOWN for the VPN connection "
                + VpnConnectionId
            )
            logger.info("Doing nothing, exiting..")
            return
        if (
            vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][1]["Status"]
            == "DOWN"
        ):
            logger.info(
                "The tunnel with OutsideIpAddress "
                + vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][1][
                    "OutsideIpAddress"
                ]
                + " is DOWN for the VPN connection "
                + VpnConnectionId
            )
            logger.info("Doing nothing, exiting..")
            return
        logger.info(
            "Both connections for "
            + VpnConnectionId
            + " are UP and fallback configured."
        )
        update_static_route(
            TGWID,
            TGWAttachmentID2
            if event_transitGatewayAttachment == TGWAttachmentID1
            else TGWAttachmentID1,
            event_transitGatewayAttachment,
        )
        return

    if eventreason == "VPN-CONNECTION-IPSEC-DOWN":
        vpn_connections_response = ec2.describe_vpn_connections(
            VpnConnectionIds=[VpnConnectionId]
        )
        if (
            vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][0]["Status"]
            == "UP"
        ):
            logger.info(
                "The tunnel with OutsideIpAddress "
                + vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][0][
                    "OutsideIpAddress"
                ]
                + " is UP for the VPN connection "
                + VpnConnectionId
            )
            logger.info("Doing nothing, exiting..")
            return
        if (
            vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][1]["Status"]
            == "UP"
        ):
            logger.info(
                "The tunnel with OutsideIpAddress "
                + vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][1][
                    "OutsideIpAddress"
                ]
                + " is UP for the VPN connection "
                + VpnConnectionId
            )
            logger.info("Doing nothing, exiting..")
            return
        logger.info("Both connections for " + VpnConnectionId + " are down!")
        update_static_route(
            TGWID,
            event_transitGatewayAttachment,
            TGWAttachmentID2
            if event_transitGatewayAttachment == TGWAttachmentID1
            else TGWAttachmentID1,
        )
        return

    if eventreason == "VPN-CONNECTION-IPSEC-HEALTHCHECK":
        describe_transit_gateway_attachments_response = (
            ec2.describe_transit_gateway_attachments(
                TransitGatewayAttachmentIds=[
                    TGWAttachmentID1,
                    TGWAttachmentID2,
                ]
            )
        )
        for attachment in describe_transit_gateway_attachments_response[
            "TransitGatewayAttachments"
        ]:
            ResourceId = attachment["ResourceId"]
            vpn_connections_response = ec2.describe_vpn_connections(
                VpnConnectionIds=[ResourceId]
            )
            if (
                vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][0][
                    "Status"
                ]
                == "UP"
            ):
                logger.info(
                    "The tunnel with OutsideIpAddress "
                    + vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][0][
                        "OutsideIpAddress"
                    ]
                    + " is UP for the VPN connection "
                    + ResourceId
                )
                continue
            if (
                vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][1][
                    "Status"
                ]
                == "UP"
            ):
                logger.info(
                    "The tunnel with OutsideIpAddress "
                    + vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][1][
                        "OutsideIpAddress"
                    ]
                    + " is UP for the VPN connection "
                    + ResourceId
                )
                continue
            logger.info("Both connections for " + ResourceId + " are down!")
            current_attachment = attachment["TransitGatewayAttachmentId"]
            update_static_route(
                TGWID,
                current_attachment,
                TGWAttachmentID2
                if current_attachment == TGWAttachmentID1
                else TGWAttachmentID1,
            )
            return


def update_static_route(TGWID, TGWAttachmentID_Current, TGWAttachmentID_NEW):
    """Replace all TGW static routes from the failing attachment to the healthy one.

    Acquires a DynamoDB distributed lock before modifying routes to prevent concurrent
    Lambda executions from producing inconsistent route table state. Iterates over all
    TGW route tables for the given TGW and replaces every static route pointing to
    ``TGWAttachmentID_Current`` with ``TGWAttachmentID_NEW``.

    Args:
        TGWID: Transit Gateway ID used to enumerate all associated route tables.
        TGWAttachmentID_Current: The failing TGW VPN attachment whose routes will be
            replaced.
        TGWAttachmentID_NEW: The healthy TGW VPN attachment that routes will point to
            after replacement.

    Raises:
        botocore.exceptions.ClientError: Re-raised after releasing the DynamoDB lock
            if any AWS API call fails.
    """
    lock_client = DynamoDBLockClient(
        dynamodb_resource,
        table_name=DynamoDBLockTable,
        lease_duration=datetime.timedelta(0, 60),
        expiry_period=datetime.timedelta(0, 1200),
    )
    lock = lock_client.acquire_lock("my_key")

    try:
        describe_transit_gateway_route_tables_response = (
            ec2.describe_transit_gateway_route_tables(
                Filters=[
                    {
                        "Name": "transit-gateway-id",
                        "Values": [TGWID],
                    },
                ]
            )
        )
        logger.debug(describe_transit_gateway_route_tables_response)

        route_tables = describe_transit_gateway_route_tables_response[
            "TransitGatewayRouteTables"
        ]
        logger.debug(len(route_tables))

        for route_table in route_tables:
            route_table_id = route_table["TransitGatewayRouteTableId"]
            search_response = ec2.search_transit_gateway_routes(
                TransitGatewayRouteTableId=route_table_id,
                Filters=[
                    {
                        "Name": "attachment.transit-gateway-attachment-id",
                        "Values": [TGWAttachmentID_Current],
                    },
                    {
                        "Name": "type",
                        "Values": ["static"],
                    },
                ],
            )
            logger.debug(search_response)

            if len(search_response["Routes"]) == 0:
                logger.info(
                    "No routes to "
                    + TGWAttachmentID_Current
                    + " found in "
                    + route_table_id
                )
                continue

            for record in search_response["Routes"]:
                logger.info(
                    "Replacing route "
                    + record["DestinationCidrBlock"]
                    + " to "
                    + TGWAttachmentID_NEW
                    + " in TGW route table "
                    + route_table_id
                )
                ec2.replace_transit_gateway_route(
                    DestinationCidrBlock=record["DestinationCidrBlock"],
                    TransitGatewayRouteTableId=route_table_id,
                    TransitGatewayAttachmentId=TGWAttachmentID_NEW,
                )
    except botocore.exceptions.ClientError as e:
        lock.release()
        lock_client.close()
        raise e

    lock.release()
    lock_client.close()
