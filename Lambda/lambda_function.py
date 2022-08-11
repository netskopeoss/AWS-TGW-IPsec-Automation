# pylint: disable=C0301,W1203
"""VPN auto failover solution"""
import json
import logging
import os
import datetime
import sys

import requests
import boto3
import botocore

from python_dynamodb_lock.python_dynamodb_lock import DynamoDBLockClient

tgw_region = os.environ["TGWRegion"]
tgw_id = os.environ["TGWID"]
tgw_attachment_id_1 = os.environ["TGWAttachmentID1"]
tgw_attachment_id_2 = os.environ["TGWAttachmentID2"]
dynamodb_lock_table = os.environ["DynamoDBLockTable"]
fallback_support = os.environ["FallbackSupport"]
slack_incoming_webhook = os.environ["SlackIncomingWebhook"]

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ec2 = boto3.client("ec2", region_name=tgw_region)
sns = boto3.client("sns", region_name=tgw_region)
dynamodb_resource = boto3.resource("dynamodb")


def send_slack_notification(message):
    """
    Sends notification to the specified slack channel
    """
    response = requests.post(
        slack_incoming_webhook,
        data=json.dumps({"text": f"`{message}`"}),
        headers={"Content-type": "application/json"},
    )
    logger.info(f"slack response: {response.text}")


def update_static_route(
    message, target_tgw_id, tgw_attachment_id_current, tgw_attachment_id_new
):
    """Update static route to use another vpn tgw attachment"""
    lock_client = DynamoDBLockClient(
        dynamodb_resource,
        table_name=dynamodb_lock_table,
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
                        "Values": [
                            target_tgw_id,
                        ],
                    },
                ]
            )
        )
        logger.debug(describe_transit_gateway_route_tables_response)
        logger.debug(
            len(
                describe_transit_gateway_route_tables_response[
                    "TransitGatewayRouteTables"
                ]
            )
        )
        search_transit_gateway_routes_response = ec2.search_transit_gateway_routes(
            TransitGatewayRouteTableId=describe_transit_gateway_route_tables_response[
                "TransitGatewayRouteTables"
            ][1]["TransitGatewayRouteTableId"],
            Filters=[
                {
                    "Name": "attachment.transit-gateway-attachment-id",
                    "Values": [
                        tgw_attachment_id_current,
                    ],
                },
                {
                    "Name": "type",
                    "Values": [
                        "static",
                    ],
                },
            ],
        )
        logger.debug(search_transit_gateway_routes_response)
        for i in range(
            len(
                describe_transit_gateway_route_tables_response[
                    "TransitGatewayRouteTables"
                ]
            )
        ):
            search_transit_gateway_routes_response = ec2.search_transit_gateway_routes(
                TransitGatewayRouteTableId=describe_transit_gateway_route_tables_response[
                    "TransitGatewayRouteTables"
                ][
                    i
                ][
                    "TransitGatewayRouteTableId"
                ],
                Filters=[
                    {
                        "Name": "attachment.transit-gateway-attachment-id",
                        "Values": [
                            tgw_attachment_id_current,
                        ],
                    },
                    {
                        "Name": "type",
                        "Values": [
                            "static",
                        ],
                    },
                ],
            )
            logger.debug(search_transit_gateway_routes_response)
            if len(search_transit_gateway_routes_response["Routes"]) == 0:
                logger.info(
                    f"No routes to {tgw_attachment_id_current} found in "
                    f'{describe_transit_gateway_route_tables_response["TransitGatewayRouteTables"][i]["TransitGatewayRouteTableId"]}'
                )
                continue

            for j in range(len(search_transit_gateway_routes_response["Routes"])):
                record = search_transit_gateway_routes_response["Routes"][j]
                message += (
                    f' Replacing route {record["DestinationCidrBlock"]} to {tgw_attachment_id_new} in TGW route table '
                    f'{describe_transit_gateway_route_tables_response["TransitGatewayRouteTables"][i]["TransitGatewayRouteTableId"]}.'
                )
                replace_transit_gateway_route_response = ec2.replace_transit_gateway_route(
                    DestinationCidrBlock=record["DestinationCidrBlock"],
                    TransitGatewayRouteTableId=describe_transit_gateway_route_tables_response[
                        "TransitGatewayRouteTables"
                    ][
                        i
                    ][
                        "TransitGatewayRouteTableId"
                    ],
                    TransitGatewayAttachmentId=tgw_attachment_id_new,
                )
                logger.info(replace_transit_gateway_route_response)

            logger.warning(message)
            if slack_incoming_webhook and "Replacing route" in message:
                send_slack_notification(message)
    except botocore.exceptions.ClientError as err:
        lock.release()
        lock_client.close()
        raise err

    lock.release()
    lock_client.close()


def lambda_handler(event, context):
    """Lambda handler"""
    # pylint: disable=unused-argument
    logger.info(f"Received event: {json.dumps(event)}")

    event_reason = event["detail"]["changeType"]
    event_tgw_arn = event["detail"]["transitGatewayArn"]
    event_tgw_id = event_tgw_arn.split("/")[1]

    if event_tgw_id != tgw_id:
        logger.error(f"Irrelevant tgw {event_tgw_id}")
        sys.exit(1)

    # NON VPN-CONNECTION-IPSEC-HEALTHCHECK event handler
    if event_reason != "VPN-CONNECTION-IPSEC-HEALTHCHECK":
        event_tgw_attachment_arn = event["detail"]["transitGatewayAttachmentArn"]
        event_tgw_attachment_id = event_tgw_attachment_arn.split("/")[1]

        if event_tgw_attachment_id not in (tgw_attachment_id_1, tgw_attachment_id_2):
            logger.error(f"Irrelevant tgw attachment {event_tgw_attachment_id}")
            sys.exit(1)

        vpn_connection_arn = event["detail"]["vpnConnectionArn"]
        vpn_connection_id = vpn_connection_arn.split("/")[1]

        logger.info(f"Event {event_reason} for vpn {vpn_connection_id}")

    # VPN-CONNECTION-IPSEC-UP event handler
    if event_reason == "VPN-CONNECTION-IPSEC-UP":
        if fallback_support == "no":
            logger.info("Ignore it as fallback is not configured.")
            sys.exit(0)

        vpn_connections_response = ec2.describe_vpn_connections(
            VpnConnectionIds=[vpn_connection_id]
        )

        if (
            vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][0]["Status"]
            == "DOWN"
        ):
            logger.info(
                f'Tunnel {vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][0]["OutsideIpAddress"]} is still DOWN in {vpn_connection_id}, do not fallback.'
            )
            sys.exit(0)

        if (
            vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][1]["Status"]
            == "DOWN"
        ):
            logger.info(
                f'Tunnel {vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][1]["OutsideIpAddress"]} is still DOWN in {vpn_connection_id}, do not fallback.'
            )
            sys.exit(0)

        warning_message = (
            f"Both tunnels for {vpn_connection_id} are UP, kick off fallback."
        )

        update_static_route(
            warning_message,
            tgw_id,
            tgw_attachment_id_2
            if event_tgw_attachment_id == tgw_attachment_id_1
            else tgw_attachment_id_1,
            event_tgw_attachment_id,
        )
        sys.exit(0)

    # VPN-CONNECTION-IPSEC-DOWN event handler
    if event_reason == "VPN-CONNECTION-IPSEC-DOWN":

        vpn_connections_response = ec2.describe_vpn_connections(
            VpnConnectionIds=[vpn_connection_id]
        )

        if (
            vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][0]["Status"]
            == "UP"
        ):
            logger.info(
                f'Tunnel {vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][0]["OutsideIpAddress"]} is still UP in {vpn_connection_id}, do not failover.'
            )
            sys.exit(0)

        if (
            vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][1]["Status"]
            == "UP"
        ):
            logger.info(
                f'Tunnel {vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][1]["OutsideIpAddress"]} is still UP in {vpn_connection_id}, do not failover.'
            )
            sys.exit(0)

        warning_message = (
            f"Both tunnels for {vpn_connection_id} are DOWN, kick off failover."
        )

        update_static_route(
            warning_message,
            tgw_id,
            event_tgw_attachment_id,
            tgw_attachment_id_2
            if event_tgw_attachment_id == tgw_attachment_id_1
            else tgw_attachment_id_1,
        )
        sys.exit(0)

    # VPN-CONNECTION-IPSEC-HEALTHCHECK (custom) event handler
    if event_reason == "VPN-CONNECTION-IPSEC-HEALTHCHECK":

        describe_transit_gateway_attachments_response = (
            ec2.describe_transit_gateway_attachments(
                TransitGatewayAttachmentIds=[tgw_attachment_id_1, tgw_attachment_id_2]
            )
        )

        for i in range(
            len(
                describe_transit_gateway_attachments_response[
                    "TransitGatewayAttachments"
                ]
            )
        ):
            resource_id = describe_transit_gateway_attachments_response[
                "TransitGatewayAttachments"
            ][i]["ResourceId"]
            vpn_connections_response = ec2.describe_vpn_connections(
                VpnConnectionIds=[resource_id]
            )
            if (
                vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][0][
                    "Status"
                ]
                == "UP"
            ):
                logger.info(
                    f'Health checking - Tunnel {vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][0]["OutsideIpAddress"]} is UP for {resource_id}'
                )
                continue

            if (
                vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][1][
                    "Status"
                ]
                == "UP"
            ):
                logger.info(
                    f'Health checking - Tunnel {vpn_connections_response["VpnConnections"][0]["VgwTelemetry"][1]["OutsideIpAddress"]} is UP for {resource_id}'
                )
                continue

            warning_message = f"Health checking - Both connection for {resource_id} are DOWN, updating route table."

            if i == 0:
                update_static_route(
                    warning_message, tgw_id, tgw_attachment_id_1, tgw_attachment_id_2
                )
            else:
                update_static_route(
                    warning_message, tgw_id, tgw_attachment_id_2, tgw_attachment_id_1
                )

            sys.exit(0)
