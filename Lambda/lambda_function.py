from __future__ import print_function
import boto3
import botocore
from botocore.exceptions import ClientError
import logging
import json
import os
from python_dynamodb_lock.python_dynamodb_lock import *

TGWRegion = os.environ['TGWRegion']
TGWID = os.environ['TGWID']
TGWAttachmentID1 = os.environ['TGWAttachmentID1']
TGWAttachmentID2 = os.environ['TGWAttachmentID2']
DynamoDBLockTable = os.environ['DynamoDBLockTable']
FallbackSupport = os.environ['FallbackSupport']

# Set up  logger
logger = logging.getLogger()
logger.setLevel(logging.os.environ['LOGLEVEL'])

ec2 = boto3.client('ec2', region_name=TGWRegion)
# get a reference to the DynamoDB resource
dynamodb_resource = boto3.resource('dynamodb')

def lambda_handler(event, context):  
    
    logger.info('Got event ' + json.dumps(event))
    
    eventreason = event['detail']['changeType']
    event_transitGatewayArn = event['detail']['transitGatewayArn']
    event_TGWID = event_transitGatewayArn.split('/')[1]
    if event_TGWID != TGWID:
        logger.error ('Lambda function called for the TGW ' + event_TGWID + ' but supposed to work only with TGW ' + TGWID + '. Exiting')
        exit(1)
        
    if eventreason != 'VPN-CONNECTION-IPSEC-HEALTHCHECK':
        event_transitGatewayAttachmentArn = event['detail']['transitGatewayAttachmentArn']
        event_transitGatewayAttachment = event_transitGatewayAttachmentArn.split('/')[1]
        if event_transitGatewayAttachment != TGWAttachmentID1 and event_transitGatewayAttachment != TGWAttachmentID2:
            logger.error ('Lambda function called for the TGW attachment ' + event_transitGatewayAttachment + ' but supposed to work with the attachments ' + TGWAttachmentID1 + ' and ' + TGWAttachmentID1 + '. Exiting')
            exit(1)
    
        vpnConnectionArn = event['detail']['vpnConnectionArn']
        VpnConnectionId = vpnConnectionArn.split('/')[1]

        logger.info('Got event ' + eventreason + ' for VPNConnectionId : ' + VpnConnectionId)

    if eventreason == 'VPN-CONNECTION-IPSEC-UP':
        if FallbackSupport == 'no':
            logger.info('Got VPN-CONNECTION-IPSEC-UP and fallback is not configured..')
            logger.info('Doing nothing, exiting..')
            exit(0)
        
        vpn_connections_response = ec2.describe_vpn_connections(VpnConnectionIds=[VpnConnectionId])
        if vpn_connections_response['VpnConnections'][0]['VgwTelemetry'][0]['Status'] == 'DOWN':
            logger.info ('The tunnel with OutsideIpAddress ' + vpn_connections_response['VpnConnections'][0]['VgwTelemetry'][0]['OutsideIpAddress'] + ' is DOWN for the VPN connection ' + VpnConnectionId)
            logger.info('Doing nothing, exiting..')
            exit(0)
        if vpn_connections_response['VpnConnections'][0]['VgwTelemetry'][1]['Status'] == 'DOWN':
            logger.info ('The tunnel with OutsideIpAddress ' + vpn_connections_response['VpnConnections'][0]['VgwTelemetry'][1]['OutsideIpAddress'] + ' is DOWN for the VPN connection ' + VpnConnectionId)
            logger.info('Doing nothing, exiting..')
            exit(0)
        logger.info("Both connection for " + VpnConnectionId + " are UP and fallback configured.")

        update_static_route(TGWID, TGWAttachmentID2 if event_transitGatewayAttachment==TGWAttachmentID1 else TGWAttachmentID1, event_transitGatewayAttachment)
        exit(0)

    if eventreason == 'VPN-CONNECTION-IPSEC-DOWN':

        vpn_connections_response = ec2.describe_vpn_connections(VpnConnectionIds=[VpnConnectionId])
        if vpn_connections_response['VpnConnections'][0]['VgwTelemetry'][0]['Status'] == 'UP':
            logger.info ('The tunnel with OutsideIpAddress ' + vpn_connections_response['VpnConnections'][0]['VgwTelemetry'][0]['OutsideIpAddress'] + ' is UP for the VPN connection ' + VpnConnectionId)
            logger.info('Doing nothing, exiting..')
            exit(0)
        if vpn_connections_response['VpnConnections'][0]['VgwTelemetry'][1]['Status'] == 'UP':
            logger.info ('The tunnel with OutsideIpAddress ' + vpn_connections_response['VpnConnections'][0]['VgwTelemetry'][1]['OutsideIpAddress'] + ' is UP for the VPN connection ' + VpnConnectionId)
            logger.info('Doing nothing, exiting..')
            exit(0)
        logger.info("Both connection for " + VpnConnectionId + " are down!")

        update_static_route(TGWID, event_transitGatewayAttachment, TGWAttachmentID2 if event_transitGatewayAttachment==TGWAttachmentID1 else TGWAttachmentID1)
        exit(0)

    if eventreason == 'VPN-CONNECTION-IPSEC-HEALTHCHECK':

        describe_transit_gateway_attachments_response = ec2.describe_transit_gateway_attachments(
                TransitGatewayAttachmentIds=[
                    TGWAttachmentID1,
                    TGWAttachmentID2
                ]
        )
        for y in range (len (describe_transit_gateway_attachments_response['TransitGatewayAttachments'])):
                ResourceId = describe_transit_gateway_attachments_response['TransitGatewayAttachments'][y]['ResourceId']
                vpn_connections_response = ec2.describe_vpn_connections(VpnConnectionIds=[ResourceId])
                if vpn_connections_response['VpnConnections'][0]['VgwTelemetry'][0]['Status'] == 'UP':
                    logger.info ('The tunnel with OutsideIpAddress ' + vpn_connections_response['VpnConnections'][0]['VgwTelemetry'][0]['OutsideIpAddress'] + ' is UP for the VPN connection ' + ResourceId)
                    logger.info('Continue.. ')
                    continue
                if vpn_connections_response['VpnConnections'][0]['VgwTelemetry'][1]['Status'] == 'UP':
                    logger.info ('The tunnel with OutsideIpAddress ' + vpn_connections_response['VpnConnections'][0]['VgwTelemetry'][1]['OutsideIpAddress'] + ' is UP for the VPN connection ' + ResourceId)
                    logger.info('Continue.. ')
                    continue
                logger.info("Both connection for " + ResourceId + " are down!")
                update_static_route(TGWID, ResourceId, TGWAttachmentID2 if ResourceId==TGWAttachmentID1 else TGWAttachmentID1)
                exit(0)


def update_static_route(TGWID, TGWAttachmentID_Current, TGWAttachmentID_NEW):

    lock_client = DynamoDBLockClient(dynamodb_resource, table_name=DynamoDBLockTable, lease_duration=datetime.timedelta(0, 60), expiry_period=datetime.timedelta(0, 1200))
    lock = lock_client.acquire_lock('my_key')

    try:
        describe_transit_gateway_route_tables_response = ec2.describe_transit_gateway_route_tables(
                Filters=[
                    {
                        'Name': 'transit-gateway-id',
                        'Values': [
                            TGWID,
                        ]
                    },
                ]
        )
        logger.debug (describe_transit_gateway_route_tables_response) 
        logger.debug (len (describe_transit_gateway_route_tables_response['TransitGatewayRouteTables']))
        search_transit_gateway_routes_response = ec2.search_transit_gateway_routes(
                TransitGatewayRouteTableId=describe_transit_gateway_route_tables_response['TransitGatewayRouteTables'][1]['TransitGatewayRouteTableId'],
                Filters=[
                    {
                        'Name': 'attachment.transit-gateway-attachment-id',
                        'Values': [
                            TGWAttachmentID_Current,
                        ]
                    },
                    {
                        'Name': 'type',
                        'Values': [
                            'static',
                        ]
                    },
                ]
        )
        logger.debug (search_transit_gateway_routes_response)
        for i in range (len (describe_transit_gateway_route_tables_response['TransitGatewayRouteTables'])):
            search_transit_gateway_routes_response = ec2.search_transit_gateway_routes(
                TransitGatewayRouteTableId=describe_transit_gateway_route_tables_response['TransitGatewayRouteTables'][i]['TransitGatewayRouteTableId'],
                Filters=[
                    {
                        'Name': 'attachment.transit-gateway-attachment-id',
                        'Values': [
                            TGWAttachmentID_Current,
                        ]
                    },
                    {
                        'Name': 'type',
                        'Values': [
                            'static',
                        ]
                    },
                ]
            )
            logger.debug (search_transit_gateway_routes_response)
            if len(search_transit_gateway_routes_response['Routes']) == 0:
                logger.info ('No routes to '+ TGWAttachmentID_Current + ' found in ' + describe_transit_gateway_route_tables_response['TransitGatewayRouteTables'][i]['TransitGatewayRouteTableId'])
            for y in range (len (search_transit_gateway_routes_response['Routes'])):
                record = search_transit_gateway_routes_response['Routes'][y]
                logger.info ('Replacing route ' + record['DestinationCidrBlock'] + ' to ' + TGWAttachmentID_NEW +' in TGW route table ' + describe_transit_gateway_route_tables_response['TransitGatewayRouteTables'][i]['TransitGatewayRouteTableId'] )
                replace_transit_gateway_route_response = ec2.replace_transit_gateway_route(
                    DestinationCidrBlock=record['DestinationCidrBlock'],
                    TransitGatewayRouteTableId=describe_transit_gateway_route_tables_response['TransitGatewayRouteTables'][i]['TransitGatewayRouteTableId'],
                    TransitGatewayAttachmentId=TGWAttachmentID_NEW
                )
    except botocore.exceptions.ClientError as e:
            lock.release()
            lock_client.close()
            raise e
     #    close the lock_client
    lock.release()
    lock_client.close()