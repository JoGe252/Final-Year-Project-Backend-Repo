# Standard library imports
import json          # Used to convert Python dicts to JSON strings for HTTP responses
import os            # Used to read environment variables (config without hardcoding)
import logging       # Used to write logs to CloudWatch for debugging/monitoring

# AWS SDK imports
import boto3         # AWS SDK for Python (lets us access DynamoDB, S3, etc.)
from botocore.exceptions import ClientError  # Catches AWS service errors cleanly

# Create a logger that writes to AWS CloudWatch Logs
logger = logging.getLogger()
logger.setLevel(logging.INFO)  # INFO shows normal operational messages (DEBUG is more verbose)

# Create a DynamoDB "resource" object (higher-level API than client)
# This is created outside the handler so it can be reused across warm Lambda invocations
dynamodb = boto3.resource("dynamodb")


def _response(status_code: int, body: dict, origin: str):
    """
    Helper function to create a consistent HTTP response format for API Gateway.

    status_code: HTTP status code (200, 400, 500, etc.)
    body: Python dictionary that will be JSON encoded
    origin: Allowed CORS origin (e.g., '*' or your CloudFront domain)
    """
    return {
        "statusCode": status_code,
        "headers": {
            # Tell the browser we're returning JSON
            "Content-Type": "application/json",

            # CORS header: allows your frontend (website) to call this API from the browser
            "Access-Control-Allow-Origin": origin,

            # Allowed methods for this endpoint
            "Access-Control-Allow-Methods": "GET,OPTIONS",

            # Allowed headers sent from the browser
            "Access-Control-Allow-Headers": "Content-Type",
        },

        # API Gateway expects the body to be a STRING, so we JSON-encode the dict
        "body": json.dumps(body),
    }


def lambda_handler(event, context):
    """
    Main Lambda handler function.
    This function is triggered by API Gateway on GET /count requests.

    event: request data from API Gateway (headers, path, query, etc.)
    context: runtime information from AWS Lambda (request id, memory, etc.)
    """

    # Read config from environment variables so nothing is hardcoded
    table_name = os.environ.get("TABLE_NAME")          # DynamoDB table name (required)
    counter_id = os.environ.get("COUNTER_ID", "main")  # Partition key value for the counter row
    origin = os.environ.get("ALLOWED_ORIGIN", "*")     # CORS origin ('*' for dev; tighten later)

    # If TABLE_NAME wasn't set, the function cannot proceed safely
    if not table_name:
        logger.error("Missing TABLE_NAME environment variable")
        return _response(500, {"error": "Server misconfigured"}, origin)

    # Create a reference to the DynamoDB table
    table = dynamodb.Table(table_name)

    try:
        # update_item performs an atomic update in DynamoDB (safe for concurrent visitors)
        # - Key identifies the item: {"id": "main"}
        # - UpdateExpression increments the count
        # - if_not_exists ensures the counter starts at 0 if the item doesn't exist yet
        result = table.update_item(
            Key={"id": counter_id},

            # Increment logic:
            # count = (count if exists else 0) + 1
            UpdateExpression="SET #c = if_not_exists(#c, :zero) + :inc",

            # "count" is a reserved/normal attribute name, but we use a safe alias (#c)
            ExpressionAttributeNames={"#c": "count"},

            # Values used in the expression
            ExpressionAttributeValues={
                ":inc": 1,    # amount to increment by
                ":zero": 0    # starting value if item doesn't exist
            },

            # Ask DynamoDB to return the updated value
            ReturnValues="UPDATED_NEW",
        )

        # Extract the updated count from DynamoDB response
        new_count = int(result["Attributes"]["count"])

        # Return success JSON to the browser
        return _response(200, {"count": new_count}, origin)

    except ClientError:
        # This catches AWS-specific errors (e.g., AccessDenied, table not found, throttling)
        logger.exception("DynamoDB error occurred while updating count")
        return _response(500, {"error": "Database error"}, origin)

    except Exception:
        # This catches any other unexpected error (bugs, bad data, etc.)
        logger.exception("Unhandled error occurred")
        return _response(500, {"error": "Unhandled server error"}, origin)