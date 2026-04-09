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
    GET /admin/alerts

    Returns a list of recent alert-style events based on:
    - negative sentiment
    - strong positive sentiment
    - mixed sentiment
    - low-confidence sentiment
    - visitor milestones
    """

    function_name = "admin_alerts"

    origin = os.environ.get("ALLOWED_ORIGIN", "*")
    visitor_table_name = os.environ.get("VISITOR_TABLE", "visitor_counter_prod")
    feedback_table_name = os.environ.get("FEEDBACK_TABLE", "feedback_table_prod")
    counter_id = os.environ.get("COUNTER_ID", "main")

    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return _response(200, {"ok": True}, origin)

    try:
        visitor_table = dynamodb.Table(visitor_table_name)
        feedback_table = dynamodb.Table(feedback_table_name)

        alerts = []

        # ---------------------------------
        # Visitor milestone alerts
        # ---------------------------------
        visitor_response = visitor_table.get_item(Key={"id": counter_id})
        visitor_item = visitor_response.get("Item", {})
        total_visitors = int(visitor_item.get("count", 0))

        if total_visitors >= 250:
            alerts.append({
                "type": "TRAFFIC_MILESTONE",
                "severity": "medium",
                "message": f"Visitor count reached {total_visitors} visits.",
                "createdAt": datetime.now(timezone.utc).isoformat()
            })
        elif total_visitors >= 100:
            alerts.append({
                "type": "TRAFFIC_MILESTONE",
                "severity": "medium",
                "message": f"Visitor count passed 100 visits.",
                "createdAt": datetime.now(timezone.utc).isoformat()
            })
        elif total_visitors >= 50:
            alerts.append({
                "type": "TRAFFIC_MILESTONE",
                "severity": "low",
                "message": f"Visitor count passed 50 visits.",
                "createdAt": datetime.now(timezone.utc).isoformat()
            })

        # ---------------------------------
        # Feedback-based alerts
        # ---------------------------------
        feedback_response = feedback_table.scan()
        items = feedback_response.get("Items", [])

        # Sort newest first
        items.sort(key=lambda x: x.get("createdAt", ""), reverse=True)

        for item in items[:10]:
            name = item.get("name", "Unknown")
            sentiment = item.get("sentiment", "UNKNOWN")
            confidence = float(item.get("confidenceScore", 0))
            created_at = item.get("createdAt", "")

            if sentiment == "NEGATIVE":
                alerts.append({
                    "type": "NEGATIVE_FEEDBACK",
                    "severity": "high",
                    "message": f"Negative feedback received from {name}.",
                    "createdAt": created_at
                })

            elif sentiment == "POSITIVE" and confidence > 0.95:
                alerts.append({
                    "type": "STRONG_POSITIVE_FEEDBACK",
                    "severity": "low",
                    "message": f"Strong positive feedback received from {name}.",
                    "createdAt": created_at
                })

            elif sentiment == "MIXED":
                alerts.append({
                    "type": "MIXED_FEEDBACK",
                    "severity": "medium",
                    "message": f"Mixed feedback received from {name}.",
                    "createdAt": created_at
                })

            elif confidence and confidence < 0.60:
                alerts.append({
                    "type": "LOW_CONFIDENCE_FEEDBACK",
                    "severity": "medium",
                    "message": f"Low-confidence sentiment detected for feedback from {name}.",
                    "createdAt": created_at
                })

        # Sort final alerts newest first
        alerts.sort(key=lambda x: x.get("createdAt", ""), reverse=True)

        return _response(200, {"alerts": alerts}, origin)

    except ClientError as e:
        _log_error(context, function_name, e)
        return _response(500, {"error": "Database error"}, origin)

    except Exception as e:
        _log_error(context, function_name, e)
        return _response(500, {"error": "Internal server error"}, origin)