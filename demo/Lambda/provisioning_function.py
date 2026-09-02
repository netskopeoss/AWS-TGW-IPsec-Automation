"""CloudFormation Custom Resource Lambda for Netskope IPsec tunnel provisioning.

Creates and deletes four Netskope IPsec tunnel sites in response to
CloudFormation stack lifecycle events. Returns the POP gateway IP addresses
needed to configure the corresponding AWS Customer Gateways.

Architecture (per the official Netskope AWS IPsec guide):
  Each AWS Site-to-Site VPN connection has TWO tunnel endpoints (different AZs),
  each with its own outside IP. Netskope requires one IPsec site per tunnel
  endpoint so that each site's srcidentity matches exactly one VGW outside IP.
  Two VPN connections × two tunnels each = FOUR Netskope sites total.

  Naming convention: {SiteName}-{pop}-a  / {SiteName}-{pop}-b
    -a  → Tunnel 1 of the VPN connection (VgwTelemetry[0])
    -b  → Tunnel 2 of the VPN connection (VgwTelemetry[1])

  Site layout:
    {SiteName}-{primary_pop}-a  primary_pop first, failover_pop backup
    {SiteName}-{primary_pop}-b  primary_pop first, failover_pop backup
    {SiteName}-{failover_pop}-a failover_pop first, primary_pop backup
    {SiteName}-{failover_pop}-b failover_pop first, primary_pop backup
"""

import json
import logging
import time
import urllib.request
import urllib.error

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

NETSKOPE_API_BASE = "api/v2/steering/ipsec"


# ---------------------------------------------------------------------------
# CloudFormation response helper
# ---------------------------------------------------------------------------


def cfn_send(event, context, status, data=None, reason=None, physical_id=None):
    """Send a response to the CloudFormation pre-signed S3 URL.

    Args:
        event: The CloudFormation Custom Resource event.
        context: Lambda context object, used for the log stream name.
        status: ``"SUCCESS"`` or ``"FAILED"``.
        data: Optional dict of key/value outputs returned to CloudFormation.
        reason: Human-readable failure reason shown in the CFN console.
        physical_id: Optional physical resource ID to set. If omitted, the
            existing PhysicalResourceId from the event is preserved (or the
            log stream name is used for new resources). Set this on Create to
            encode state that must survive into Delete events.
    """
    body = json.dumps(
        {
            "Status": status,
            "Reason": reason
            or f"See CloudWatch log stream: {context.log_stream_name}",
            "PhysicalResourceId": physical_id
            or event.get("PhysicalResourceId")
            or context.log_stream_name,
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
            "Data": data or {},
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        url=event["ResponseURL"],
        data=body,
        method="PUT",
        headers={
            "Content-Type": "",
            "Content-Length": str(len(body)),
        },
    )
    with urllib.request.urlopen(req) as resp:
        logger.info("CFN response sent: HTTP %s", resp.status)


# ---------------------------------------------------------------------------
# Netskope REST API helpers
# ---------------------------------------------------------------------------


def _get_secret(secret_arn):
    """Retrieve a plaintext secret value from AWS Secrets Manager."""
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_arn)
    return response["SecretString"]


def _netskope_request(hostname, token, method, resource_path, body=None):
    """Make an authenticated request to the Netskope REST API v2.

    Args:
        hostname: Netskope tenant hostname (e.g. ``mytenant.goskope.com``).
        token: Netskope API token passed via the ``Netskope-Api-Token`` header.
        method: HTTP method string (``"GET"``, ``"POST"``, ``"PATCH"``, ``"DELETE"``).
        resource_path: Full API resource path
            (e.g. ``"api/v2/steering/ipsec/tunnels"``).
        body: Optional dict serialised as JSON in the request body.

    Returns:
        Parsed JSON response as a dict.

    Raises:
        urllib.error.HTTPError: If the API returns a non-2xx status.
    """
    url = f"https://{hostname}/{resource_path}"
    encoded = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(
        url=url,
        data=encoded,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Netskope-Api-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        logger.error("Netskope API %s %s returned %s: %s", method, url, exc.code, body)
        raise


def _get_tunnel(hostname, token, tunnel_id):
    """Retrieve the current configuration of a Netskope IPsec tunnel.

    Args:
        hostname: Netskope tenant hostname.
        token: Netskope API token.
        tunnel_id: Integer ID of the tunnel.

    Returns:
        The tunnel object dict from the Netskope API response.
    """
    response = _netskope_request(
        hostname, token, "GET", f"{NETSKOPE_API_BASE}/tunnels/{tunnel_id}"
    )
    return response["result"][0]


def _create_tunnel(hostname, token, site_name, primary_pop, secondary_pop, props):
    """Create a single Netskope IPsec tunnel site with a placeholder srcidentity.

    The tunnel is created with a placeholder srcidentity derived from the site
    name (``<site_name>.placeholder.internal``). Netskope requires srcidentity
    to be unique across the tenant, so basing it on the already-unique site
    name avoids conflicts. The UpdateSrcIdentity Custom Resource will patch it
    with the real AWS VPN outside IP once the VPN connections exist.

    Args:
        hostname: Netskope tenant hostname.
        token: Netskope API token.
        site_name: Descriptive name for this tunnel site.
        primary_pop: POP name listed first (active gateway).
        secondary_pop: POP name listed second (backup gateway).
        props: CloudFormation resource properties dict containing ``Bandwidth``,
            ``Encryption``, and ``PreSharedKey``.

    Returns:
        The tunnel object dict from the Netskope API response (``data[0]``).
    """
    placeholder_srcidentity = f"{site_name}.placeholder.internal"

    payload = {
        "site": site_name,
        "pops": [primary_pop, secondary_pop],
        "psk": props["PreSharedKey"],
        "srcidentity": placeholder_srcidentity,
        "bandwidth": int(props.get("Bandwidth", 100)),
        "encryption": props.get("Encryption", "AES256-CBC"),
        "enable": True,
    }

    response = _netskope_request(
        hostname, token, "POST", f"{NETSKOPE_API_BASE}/tunnels", payload
    )
    logger.info("Created Netskope tunnel '%s': id=%s", site_name, response["data"][0]["id"])
    return response["data"][0]


def _patch_tunnel_src_identities(hostname, token, tunnel_id, src_ip, psk):
    """PATCH a tunnel's srcidentity and srcipidentity.

    The Netskope PATCH endpoint performs full field replacement — omitting any
    field resets it to its default (e.g. encryption resets to "Null"). This
    function GETs the current tunnel config first and includes all existing
    fields in the PATCH body so that only the src identity fields change.

    Args:
        hostname: Netskope tenant hostname.
        token: Netskope API token.
        tunnel_id: Integer ID of the tunnel to update.
        src_ip: The AWS VPN tunnel outside IP to set as both ``srcidentity``
            and ``srcipidentity`` (the IKE identity the VGW presents).
        psk: The IPsec pre-shared key (required in the full PATCH body).
    """
    current = _get_tunnel(hostname, token, tunnel_id)
    payload = {
        "site": current["site"],
        "pops": [p["name"] for p in current["pops"]],
        "psk": psk,
        "srcidentity": src_ip,
        "srcipidentity": src_ip,
        "bandwidth": current["bandwidth"],
        "encryption": current["encryption"],
        "enable": current["enabled"],
    }
    _netskope_request(
        hostname, token, "PATCH",
        f"{NETSKOPE_API_BASE}/tunnels/{tunnel_id}",
        payload,
    )
    logger.info(
        "Updated tunnel id=%s site=%s srcidentity/srcipidentity=%s",
        tunnel_id, current["site"], src_ip,
    )


def _delete_tunnel(hostname, token, tunnel_id):
    """Delete a Netskope IPsec tunnel site by ID.

    Args:
        hostname: Netskope tenant hostname.
        token: Netskope API token.
        tunnel_id: Integer ID of the tunnel to delete.
    """
    try:
        _netskope_request(
            hostname, token, "DELETE", f"{NETSKOPE_API_BASE}/tunnels/{tunnel_id}"
        )
        logger.info("Deleted Netskope tunnel id=%s", tunnel_id)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            logger.warning("Tunnel id=%s not found during delete — skipping", tunnel_id)
        else:
            raise


# ---------------------------------------------------------------------------
# TGW attachment lookup
# ---------------------------------------------------------------------------


def _lookup_tgw_attachments(vpn_id1, vpn_id2):
    """Poll EC2 until TGW attachment IDs are available for two VPN connections.

    Args:
        vpn_id1: VPN Connection ID for the primary connection (e.g. ``vpn-…``).
        vpn_id2: VPN Connection ID for the failover connection.

    Returns:
        Dict with keys ``AttachmentID1`` and ``AttachmentID2``.

    Raises:
        RuntimeError: If a TGW attachment is not found within the retry window.
    """
    ec2 = boto3.client("ec2")
    result = {}
    for key, vpn_id in (("AttachmentID1", vpn_id1), ("AttachmentID2", vpn_id2)):
        for attempt in range(18):  # up to ~3 minutes (18 × 10 s)
            response = ec2.describe_transit_gateway_attachments(
                Filters=[{"Name": "resource-id", "Values": [vpn_id]}]
            )
            attachments = response.get("TransitGatewayAttachments", [])
            if attachments:
                result[key] = attachments[0]["TransitGatewayAttachmentId"]
                logger.info(
                    "Found TGW attachment %s for VPN connection %s",
                    result[key],
                    vpn_id,
                )
                break
            logger.info(
                "Attempt %d: TGW attachment for %s not yet available — retrying in 10 s",
                attempt + 1,
                vpn_id,
            )
            time.sleep(10)
        else:
            raise RuntimeError(
                f"TGW attachment for VPN connection {vpn_id} not found after 3 minutes"
            )
    return result


# ---------------------------------------------------------------------------
# VPN outside IP lookup
# ---------------------------------------------------------------------------


def _lookup_vpn_outside_ips(vpn_id1, vpn_id2):
    """Poll EC2 until all four outside IPs are available for two VPN connections.

    Each AWS Site-to-Site VPN connection has two tunnel endpoints in different
    Availability Zones, each with its own outside IP. This function returns
    both IPs for each connection (four total). Each IP maps to one Netskope
    IPsec site.

    Args:
        vpn_id1: VPN Connection ID for the primary connection.
        vpn_id2: VPN Connection ID for the failover connection.

    Returns:
        Dict with keys ``OutsideIP1a``, ``OutsideIP1b``, ``OutsideIP2a``,
        ``OutsideIP2b`` containing the four VGW outside IP addresses.

    Raises:
        RuntimeError: If outside IPs are not available within the retry window.
    """
    ec2 = boto3.client("ec2")
    result = {}
    for prefix, vpn_id in (("OutsideIP1", vpn_id1), ("OutsideIP2", vpn_id2)):
        for attempt in range(18):  # up to ~3 minutes (18 × 10 s)
            response = ec2.describe_vpn_connections(VpnConnectionIds=[vpn_id])
            vpn = response["VpnConnections"][0]
            telemetry = vpn.get("VgwTelemetry", [])
            ips = [t["OutsideIpAddress"] for t in telemetry if t.get("OutsideIpAddress")]
            if len(ips) >= 2:
                result[f"{prefix}a"] = ips[0]
                result[f"{prefix}b"] = ips[1]
                logger.info(
                    "Outside IPs for VPN %s: %s (tunnel a), %s (tunnel b)",
                    vpn_id, ips[0], ips[1],
                )
                break
            logger.info(
                "Attempt %d: outside IPs for VPN %s not yet available — retrying in 10 s",
                attempt + 1,
                vpn_id,
            )
            time.sleep(10)
        else:
            raise RuntimeError(
                f"Outside IPs for VPN connection {vpn_id} not found after 3 minutes"
            )
    return result


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


def lambda_handler(event, context):
    """Handle CloudFormation Custom Resource lifecycle events.

    Dispatches to one of three modes based on the ``Mode`` property:

    **Netskope provisioning mode** (default, no ``Mode`` property):
        On ``Create``: creates four Netskope IPsec tunnel sites with a
        placeholder ``srcidentity``. Returns POP gateway IPs and site IDs.
        On ``Delete``: deletes all four tunnel sites.
        On ``Update``: deletes old sites and creates replacements.

    **TGW attachment lookup mode** (``Mode: Lookup``):
        On ``Create`` / ``Update``: polls ``ec2:DescribeTransitGatewayAttachments``
        until attachment IDs are available, then returns them as outputs.
        On ``Delete``: no-op.

    **Update srcidentity mode** (``Mode: UpdateSrcIdentity``):
        On ``Create`` / ``Update``: polls ``ec2:DescribeVpnConnections`` until
        all four outside IPs are available, then PATCHes each Netskope tunnel
        with its corresponding AWS VPN outside IP as both ``srcidentity`` and
        ``srcipidentity``. The PATCH includes the full tunnel config to avoid
        resetting fields (e.g. encryption) to defaults.
        On ``Delete``: no-op (tunnel deletion handled by provisioning resource).

    Args:
        event: CloudFormation Custom Resource event dict.
        context: Lambda context object.
    """
    logger.info("Event: %s", json.dumps(event))
    request_type = event["RequestType"]
    props = event["ResourceProperties"]

    try:
        mode = props.get("Mode")

        if mode == "Lookup":
            if request_type == "Delete":
                cfn_send(event, context, "SUCCESS")
                return
            data = _lookup_tgw_attachments(
                props["VPNConnectionID1"],
                props["VPNConnectionID2"],
            )
            cfn_send(event, context, "SUCCESS", data)
            return

        if mode == "UpdateSrcIdentity":
            if request_type == "Delete":
                cfn_send(event, context, "SUCCESS")
                return
            hostname = props["NetskopeHostname"]
            token = _get_secret(props["NetskopeTokenSecretArn"])
            psk = _get_secret(props["PskSecretArn"])
            outside_ips = _lookup_vpn_outside_ips(
                props["VPNConnectionID1"],
                props["VPNConnectionID2"],
            )
            _patch_tunnel_src_identities(
                hostname, token, int(props["Site1aId"]), outside_ips["OutsideIP1a"], psk
            )
            _patch_tunnel_src_identities(
                hostname, token, int(props["Site1bId"]), outside_ips["OutsideIP1b"], psk
            )
            _patch_tunnel_src_identities(
                hostname, token, int(props["Site2aId"]), outside_ips["OutsideIP2a"], psk
            )
            _patch_tunnel_src_identities(
                hostname, token, int(props["Site2bId"]), outside_ips["OutsideIP2b"], psk
            )
            cfn_send(event, context, "SUCCESS", {
                "Site1aSrcIdentity": outside_ips["OutsideIP1a"],
                "Site1bSrcIdentity": outside_ips["OutsideIP1b"],
                "Site2aSrcIdentity": outside_ips["OutsideIP2a"],
                "Site2bSrcIdentity": outside_ips["OutsideIP2b"],
            })
            return

        # Default: Netskope tunnel provisioning
        hostname = props["NetskopeHostname"]
        token = _get_secret(props["NetskopeTokenSecretArn"])

        if request_type == "Create":
            data = _provision(hostname, token, props)
            # Encode all four tunnel IDs in PhysicalResourceId so they survive
            # into Update and Delete events (output attributes are not available
            # in ResourceProperties during rollback/delete).
            physical_id = f"{data['Site1aId']}:{data['Site1bId']}:{data['Site2aId']}:{data['Site2bId']}"
            cfn_send(event, context, "SUCCESS", data, physical_id=physical_id)

        elif request_type == "Update":
            old_props = event.get("OldResourceProperties", {})
            old_token = _get_secret(
                old_props.get("NetskopeTokenSecretArn", props["NetskopeTokenSecretArn"])
            )
            _deprovision_by_physical_id(
                old_props.get("NetskopeHostname", hostname),
                old_token,
                event.get("PhysicalResourceId", ""),
            )
            data = _provision(hostname, token, props)
            physical_id = f"{data['Site1aId']}:{data['Site1bId']}:{data['Site2aId']}:{data['Site2bId']}"
            cfn_send(event, context, "SUCCESS", data, physical_id=physical_id)

        elif request_type == "Delete":
            _deprovision_by_physical_id(
                hostname, token, event.get("PhysicalResourceId", "")
            )
            cfn_send(event, context, "SUCCESS")

        else:
            raise ValueError(f"Unknown RequestType: {request_type}")

    except Exception as exc:
        logger.exception("Provisioning failed")
        cfn_send(event, context, "FAILED", reason=str(exc))


def _provision(hostname, token, props):
    """Create four IPsec tunnel sites and return their gateway IPs and IDs.

    Creates sites following the naming convention from the Netskope AWS guide:
      {SiteName}-{primary_pop}-a  Tunnel 1 of VPN1, primary_pop first
      {SiteName}-{primary_pop}-b  Tunnel 2 of VPN1, primary_pop first
      {SiteName}-{failover_pop}-a Tunnel 1 of VPN2, failover_pop first
      {SiteName}-{failover_pop}-b Tunnel 2 of VPN2, failover_pop first

    Args:
        hostname: Netskope tenant hostname.
        token: Netskope API token.
        props: CloudFormation resource properties dict.

    Returns:
        Dict with ``PrimaryGatewayIP``, ``FailoverGatewayIP``,
        ``Site1aId``, ``Site1bId``, ``Site2aId``, ``Site2bId``.
    """
    site_name = props["SiteName"]
    primary_pop = props["PrimaryPOP"]
    failover_pop = props["FailoverPOP"]

    # Fetch PSK at runtime — never stored in env vars or CFN parameters
    props["PreSharedKey"] = _get_secret(props["PskSecretArn"])

    # Two sites per VPN connection (one per AWS tunnel endpoint / VGW outside IP)
    site1a = _create_tunnel(
        hostname, token, f"{site_name}-{primary_pop}-a", primary_pop, failover_pop, props
    )
    site1b = _create_tunnel(
        hostname, token, f"{site_name}-{primary_pop}-b", primary_pop, failover_pop, props
    )
    site2a = _create_tunnel(
        hostname, token, f"{site_name}-{failover_pop}-a", failover_pop, primary_pop, props
    )
    site2b = _create_tunnel(
        hostname, token, f"{site_name}-{failover_pop}-b", failover_pop, primary_pop, props
    )

    # Gateway IP is the first POP's gateway in each pair
    primary_gateway = site1a["pops"][0]["gateway"]
    failover_gateway = site2a["pops"][0]["gateway"]

    logger.info(
        "Provisioned 4 tunnels — primary gateway: %s, failover gateway: %s",
        primary_gateway,
        failover_gateway,
    )

    return {
        "PrimaryGatewayIP": primary_gateway,
        "FailoverGatewayIP": failover_gateway,
        "Site1aId": str(site1a["id"]),
        "Site1bId": str(site1b["id"]),
        "Site2aId": str(site2a["id"]),
        "Site2bId": str(site2b["id"]),
    }


def _deprovision_by_physical_id(hostname, token, physical_id):
    """Delete all IPsec tunnel sites using IDs encoded in the PhysicalResourceId.

    The PhysicalResourceId encodes tunnel IDs as colon-separated integers:
    ``"<site1a_id>:<site1b_id>:<site2a_id>:<site2b_id>"`` for the current
    4-site design, or ``"<site1_id>:<site2_id>"`` for the legacy 2-site format.

    Args:
        hostname: Netskope tenant hostname.
        token: Netskope API token.
        physical_id: CloudFormation PhysicalResourceId string.
    """
    if not physical_id or ":" not in physical_id:
        logger.warning(
            "PhysicalResourceId '%s' does not contain tunnel IDs — skipping delete",
            physical_id,
        )
        return
    tunnel_ids = physical_id.split(":")
    for tunnel_id in tunnel_ids:
        if tunnel_id:
            _delete_tunnel(hostname, token, int(tunnel_id))
