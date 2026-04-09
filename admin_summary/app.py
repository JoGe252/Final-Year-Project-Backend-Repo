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
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ============
# AWS CLIENTS
# ============
dynamodb = boto3.resource("dynamodb")


# ===============
# DECIMAL HELPER
# ===============
def _decimal_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError


# ================
# RESPONSE HELPER
# ================
def _response(status_code: int, body: dict, origin: str):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
        "body": json.dumps(body, default=_decimal_default),
    }


# ================================
# STRUCTURED ERROR LOGGING HELPER
# ================================
def _log_error(context, function_name: str, error: Exception):
    logger.error(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "requestId": context.aws_request_id if context else "unknown",
        "function": function_name,
        "errorType": type(error).__name__,
        "message": str(error)
    }))


def lambda_handler(event, context):
    """
    GET /admin/summary

    Returns dashboard summary data:
    - total visitors
    - total feedback count
    - sentiment counts
    - sentiment percentages
    - active alert count
    """

    function_name = "admin_summary"

    origin = os.environ.get("ALLOWED_ORIGIN", "*")
    visitor_table_name = os.environ.get("VISITOR_TABLE", "visitor_counter_prod")
    feedback_table_name = os.environ.get("FEEDBACK_TABLE", "feedback_table_prod")
    counter_id = os.environ.get("COUNTER_ID", "main")

    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return _response(200, {"ok": True}, origin)

    try:
        visitor_table = dynamodb.Table(visitor_table_name)
        feedback_table = dynamodb.Table(feedback_table_name)

        # ---------------------------------
        # Get visitor count
        # ---------------------------------
        visitor_response = visitor_table.get_item(Key={"id": counter_id})
        visitor_item = visitor_response.get("Item", {})
        total_visitors = int(visitor_item.get("count", 0))

        # ---------------------------------
        # Get feedback items
        # ---------------------------------
        feedback_response = feedback_table.scan()
        feedback_items = feedback_response.get("Items", [])

        feedback_count = len(feedback_items)

        positive_count = 0
        neutral_count = 0
        negative_count = 0
        mixed_count = 0
        active_alert_count = 0

        for item in feedback_items:
            sentiment = item.get("sentiment", "UNKNOWN")
            confidence = float(item.get("confidenceScore", 0))

            if sentiment == "POSITIVE":
                positive_count += 1
                if confidence > 0.95:
                    active_alert_count += 1

            elif sentiment == "NEUTRAL":
                neutral_count += 1

            elif sentiment == "NEGATIVE":
                negative_count += 1
                active_alert_count += 1

            elif sentiment == "MIXED":
                mixed_count += 1
                active_alert_count += 1

            # Low confidence can also be treated as alert-worthy
            if confidence and confidence < 0.60:
                active_alert_count += 1

        # Traffic milestone alert logic
        if total_visitors >= 50:
            active_alert_count += 1
        if total_visitors >= 100:
            active_alert_count += 1
        if total_visitors >= 250:
            active_alert_count += 1

        # ---------------------------------
        # Calculate percentages
        # ---------------------------------
        if feedback_count > 0:
            positive_percent = round((positive_count / feedback_count) * 100, 2)
            neutral_percent = round((neutral_count / feedback_count) * 100, 2)
            negative_percent = round((negative_count / feedback_count) * 100, 2)
            mixed_percent = round((mixed_count / feedback_count) * 100, 2)
        else:
            positive_percent = 0.0
            neutral_percent = 0.0
            negative_percent = 0.0
            mixed_percent = 0.0

        return _response(
            200,
            {
                "totalVisitors": total_visitors,
                "feedbackCount": feedback_count,
                "sentimentBreakdown": {
                    "POSITIVE": positive_count,
                    "NEUTRAL": neutral_count,
                    "NEGATIVE": negative_count,
                    "MIXED": mixed_count
                },
                "sentimentPercentages": {
                    "positive": positive_percent,
                    "neutral": neutral_percent,
                    "negative": negative_percent,
                    "mixed": mixed_percent
                },
                "activeAlerts": active_alert_count
            },
            origin
        )

    except ClientError as e:
        _log_error(context, function_name, e)
        return _response(500, {"error": "Database error"}, origin)

    except Exception as e:
        _log_error(context, function_name, e)
        return _response(500, {"error": "Internal server error"}, origin)