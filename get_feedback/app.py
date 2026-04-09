import json
import os
import logging
import boto3
from decimal import Decimal
from datetime import datetime, timezone
from botocore.exceptions import ClientError

# ========
# LOGGING
# ========
# CloudWatch captures anything sent through this logger.
# INFO is enough for normal operational visibility without being too noisy.
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ============
# AWS CLIENTS
# ============
# Created outside the handler so the connection can be reused across warm starts.
dynamodb = boto3.resource("dynamodb")


# ===============
# DECIMAL HELPER
# ===============
# DynamoDB returns numbers as Decimal objects.
# json.dumps() cannot serialize Decimal directly, so convert them to float.
def _decimal_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError


# ================
# RESPONSE HELPER
# ================
# Returns a consistent API Gateway response with CORS headers.
def _response(status_code: int, body: dict, origin: str):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",

            # In production, you should eventually replace * with your CloudFront domain
            "Access-Control-Allow-Origin": origin,

            # This endpoint supports GET and browser preflight OPTIONS
            "Access-Control-Allow-Methods": "GET,OPTIONS",

            # Allow browser to send Content-Type if needed
            "Access-Control-Allow-Headers": "Content-Type",
        },
        "body": json.dumps(body, default=_decimal_default),
    }


# ================================
# STRUCTURED ERROR LOGGING HELPER
# ================================
# Logs errors in JSON format for easier searching/filtering in CloudWatch Logs.
def _log_error(context, function_name: str, error: Exception):
    logger.error(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "requestId": context.aws_request_id if context else "unknown",
        "function": function_name,
        "errorType": type(error).__name__,
        "message": str(error)
    }))


# ==============
# MAIN HANDLER
# ==============
def lambda_handler(event, context):
    """
    GET /feedback

    What this endpoint does:
    1. Handles browser CORS preflight requests
    2. Reads all feedback items from DynamoDB
    3. Sorts feedback by createdAt descending (newest first)
    4. Returns the full feedback list as JSON
    """

    function_name = "get_feedback"

    # Read environment configuration
    table_name = os.environ.get("FEEDBACK_TABLE", "feedback_table")
    origin = os.environ.get("ALLOWED_ORIGIN", "*")

    # Handle browser CORS preflight request
    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return _response(200, {"ok": True}, origin)

    try:
        # Connect to the DynamoDB feedback table
        table = dynamodb.Table(table_name)

        # Scan returns all items in the table.
        # For this project, scan is fine because feedback volume is small.
        response = table.scan()

        # If the table has no items, default to an empty list
        items = response.get("Items", [])

        # ============================================
        # Sort feedback newest first by createdAt
        # ============================================
        # createdAt is stored as an ISO 8601 timestamp string,
        # so string sorting works correctly for chronological order.
        items.sort(key=lambda x: x.get("createdAt", ""), reverse=True)

        # Return the sorted feedback list
        return _response(200, {"items": items}, origin)

    except ClientError as e:
        # AWS/DynamoDB service error
        _log_error(context, function_name, e)
        return _response(500, {"error": "Database error"}, origin)

    except Exception as e:
        # Any unexpected Python/runtime error
        _log_error(context, function_name, e)
        return _response(500, {"error": "Internal server error"}, origin)