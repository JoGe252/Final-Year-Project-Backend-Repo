import json
import os
import logging
import boto3
import uuid
import time
from decimal import Decimal
from datetime import datetime, timezone
from botocore.exceptions import ClientError

# ========
# LOGGING
# ========
# CloudWatch Logs will capture anything written through this logger.
# INFO level shows normal operational messages without being too verbose.
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ============
# AWS CLIENTS
# ============
# Created outside the handler so they are reused across warm Lambda invocations.
# This improves performance by avoiding reconnection on every request.

# DynamoDB resource — higher-level API than client, easier to work with
dynamodb = boto3.resource("dynamodb")

# Amazon Comprehend client — used for sentiment analysis on feedback messages
comprehend = boto3.client("comprehend")


# ===============
# DECIMAL HELPER
# ===============
# DynamoDB stores numbers as Decimal objects.
# Python's json.dumps() cannot serialize Decimal by default, so this
# helper converts any Decimal to a regular float when building JSON responses.
def _decimal_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError


# ================
# RESPONSE HELPER
# ================
# Builds a consistent HTTP response for API Gateway.
# Every response needs:
# - statusCode: HTTP status (200, 400, 500 etc.)
# - headers: including CORS headers so the browser can read the response
# - body: a JSON string (API Gateway requires a string, not a dict)
#
# CORS headers are required because the frontend JavaScript runs on a
# different domain to the API, and browsers enforce cross-origin rules.
def _response(status_code: int, body: dict, origin: str):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",

            # Allows the frontend website to call this API from the browser.
            # In production this should be locked to your CloudFront domain.
            "Access-Control-Allow-Origin": origin,

            # Only POST and OPTIONS are needed on this endpoint
            "Access-Control-Allow-Methods": "POST,OPTIONS",

            # Allow the Content-Type header to be sent by the browser
            "Access-Control-Allow-Headers": "Content-Type",
        },

        # json.dumps converts the Python dict to a JSON string for API Gateway.
        # _decimal_default handles any Decimal values from DynamoDB.
        "body": json.dumps(body, default=_decimal_default),
    }


# ================================
# STRUCTURED ERROR LOGGING HELPER
# ================================
# Writes a consistent JSON-formatted error log to CloudWatch.
# Structured logs are easier to search and filter than plain text.
# Each log entry includes:
# - timestamp: when the error occurred (UTC)
# - requestId: the Lambda request ID for tracing across log entries
# - function: which function the error came from
# - errorType: the Python exception class name
# - message: the error description
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
    POST /feedback

    Full flow:
    1. Handle CORS preflight (OPTIONS)
    2. Parse and validate the request body
    3. Run Amazon Comprehend sentiment analysis on the message
    4. Calculate a TTL so old records expire automatically after 90 days
    5. Store feedback + sentiment + TTL in DynamoDB
    6. Return the result (including sentiment) to the frontend
    """

    # Used in structured error logs to identify which function failed
    function_name = "submit_feedback"

    # -------------------------
    # Read configuration from environment variables
    # -------------------------
    # These are set in template.yaml so nothing is hardcoded in the Lambda.
    # This makes the same code work in both dev and prod environments.

    # Name of the DynamoDB feedback table (e.g. feedback_table_prod)
    table_name = os.environ.get("FEEDBACK_TABLE", "feedback_table")

    # CORS origin — controls which frontend domains can call this API
    origin = os.environ.get("ALLOWED_ORIGIN", "*")

    # Language code passed to Amazon Comprehend (e.g. "en" for English)
    language_code = os.environ.get("LANGUAGE_CODE", "en")

    # -------------------------
    # CORS preflight handling
    # -------------------------
    # Before a browser sends a POST request, it first sends an OPTIONS request
    # to check whether the API allows cross-origin requests.
    # We must respond with 200 and the correct CORS headers, or the real
    # POST request will be blocked by the browser.
    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return _response(200, {"ok": True}, origin)

    try:
        # -------------------------
        # Parse the request body
        # -------------------------
        # API Gateway passes the request body as a string, not a dict.
        # We need to check it exists and parse the JSON ourselves.
        raw_body = event.get("body") or ""
        if not raw_body:
            return _response(400, {"error": "Request body is required"}, origin)

        # In rare cases API Gateway base64-encodes the body (e.g. binary content-type).
        # Decode it if necessary before parsing.
        if event.get("isBase64Encoded"):
            import base64
            raw_body = base64.b64decode(raw_body).decode("utf-8")

        # Parse the JSON string into a Python dictionary
        data = json.loads(raw_body)

        # Extract each field and strip surrounding whitespace.
        # The "or ''" handles the case where the key exists but is None.
        name    = (data.get("name")    or "").strip()
        email   = (data.get("email")   or "").strip()
        message = (data.get("message") or "").strip()

        # -------------------------
        # Input validation
        # -------------------------
        # Reject submissions that are missing required fields or exceed size limits.
        # This protects against empty submissions, overly long inputs, and abuse.
        # These checks mirror the frontend validation for defence-in-depth.

        if not name or not email or not message:
            return _response(400, {"error": "name, email, and message are required"}, origin)

        if len(name) > 80:
            return _response(400, {"error": "name must be <= 80 characters"}, origin)

        if len(email) > 120:
            return _response(400, {"error": "email must be <= 120 characters"}, origin)

        if len(message) < 5:
            return _response(400, {"error": "message is too short"}, origin)

        if len(message) > 1000:
            return _response(400, {"error": "message must be <= 1000 characters"}, origin)

        # -------------------------
        # Amazon Comprehend — Sentiment Analysis
        # -------------------------
        # Comprehend analyses the message text and returns a sentiment label:
        # POSITIVE, NEGATIVE, NEUTRAL, or MIXED
        # It also returns a confidence score (0.0 to 1.0) for each label.
        #
        # Wrapped in its own try/except so that if Comprehend fails
        # (e.g. service error, unsupported language), the feedback is
        # still saved and the user still gets a success response.

        sentiment        = "UNKNOWN"
        confidence_score = None  # Will be set to a Decimal if Comprehend succeeds

        try:
            comp = comprehend.detect_sentiment(
                Text=message,
                LanguageCode=language_code
            )

            # Extract the overall sentiment label (e.g. "POSITIVE")
            sentiment = comp.get("Sentiment", "UNKNOWN")

            # SentimentScore is a dict like:
            # {"Positive": 0.98, "Negative": 0.01, "Neutral": 0.01, "Mixed": 0.0}
            # We extract the score for whichever sentiment won.
            scores    = comp.get("SentimentScore", {})
            raw_score = scores.get(sentiment.capitalize(), 0.0)

            # DynamoDB cannot store Python floats — convert to Decimal first
            confidence_score = Decimal(str(raw_score))

        except ClientError as e:
            # Log the Comprehend failure but continue — feedback will still be saved
            _log_error(context, function_name, e)

        # -------------------------
        # TTL calculation
        # -------------------------
        # DynamoDB supports automatic item expiry via a TTL attribute.
        # We set a 90-day expiry so old feedback records are deleted automatically.
        # This keeps the table tidy, reduces storage costs, and demonstrates
        # awareness of data lifecycle and GDPR-style data minimisation.
        #
        # TTL must be stored as a Unix timestamp (seconds since epoch).
        # DynamoDB checks this value and deletes the item when the time passes.
        ttl_days    = 90
        ttl_seconds = int(time.time()) + (ttl_days * 24 * 60 * 60)

        # -------------------------
        # Store feedback in DynamoDB
        # -------------------------
        # Generate a unique ID for this feedback record using UUID4 (random).
        # This becomes the partition key (feedbackId) in the DynamoDB table.
        feedback_id = str(uuid.uuid4())

        item = {
            "feedbackId": feedback_id,
            "name":       name,
            "email":      email,
            "message":    message,

            # Store the creation time as an ISO 8601 UTC timestamp string.
            # timezone.utc ensures the time is always UTC regardless of Lambda region.
            "createdAt":  datetime.now(timezone.utc).isoformat(),

            "sentiment":  sentiment,

            # TTL attribute — DynamoDB uses this to auto-expire the record after 90 days.
            # Must be enabled on the table in the AWS Console or via the SAM template.
            "ttl":        ttl_seconds
        }

        # Only include confidenceScore if Comprehend returned one successfully
        if confidence_score is not None:
            item["confidenceScore"] = confidence_score  # Stored as Decimal in DynamoDB

        # Write the item to DynamoDB
        table = dynamodb.Table(table_name)
        table.put_item(Item=item)

        # -------------------------
        # Return success response to frontend
        # -------------------------
        # JSON cannot serialize Decimal objects, so convert confidence_score
        # back to a regular Python float before returning it.
        # The frontend uses feedbackId and sentiment to show the user confirmation.
        return _response(
            200,
            {
                "message":         "Feedback saved",
                "feedbackId":      feedback_id,
                "sentiment":       sentiment,
                "confidenceScore": float(confidence_score) if confidence_score is not None else None,
            },
            origin,
        )

    # -------------------------
    # Error handling
    # -------------------------

    except json.JSONDecodeError as e:
        # The request body could not be parsed as valid JSON
        _log_error(context, function_name, e)
        return _response(400, {"error": "Invalid JSON"}, origin)

    except ClientError as e:
        # An AWS service error occurred (DynamoDB write failed, permission denied etc.)
        _log_error(context, function_name, e)
        return _response(500, {"error": "Database error"}, origin)

    except Exception as e:
        # Catch-all for any unexpected Python or runtime error
        _log_error(context, function_name, e)
        return _response(500, {"error": "Internal server error"}, origin)