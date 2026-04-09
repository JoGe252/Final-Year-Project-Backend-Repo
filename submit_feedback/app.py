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
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ============
# AWS CLIENTS
# ============
dynamodb = boto3.resource("dynamodb")
comprehend = boto3.client("comprehend")
ses = boto3.client("ses")


def _decimal_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError


def _response(status_code: int, body: dict, origin: str):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "POST,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
        "body": json.dumps(body, default=_decimal_default),
    }


def _log_error(context, function_name: str, error: Exception):
    logger.error(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "requestId": context.aws_request_id if context else "unknown",
        "function": function_name,
        "errorType": type(error).__name__,
        "message": str(error)
    }))


def _send_notification_email(
    notify_email: str,
    name: str,
    email: str,
    message: str,
    feedback_id: str,
    sentiment: str,
    confidence_score,
):
    if not notify_email:
        return

    confidence_text = (
        f"{float(confidence_score):.4f}" if confidence_score is not None else "N/A"
    )

    ses.send_email(
        Source=notify_email,
        Destination={"ToAddresses": [notify_email]},
        Message={
            "Subject": {
                "Data": f"New CV feedback from {name}",
                "Charset": "UTF-8",
            },
            "Body": {
                "Text": {
                    "Data": (
                        "A new feedback message was submitted from your CV site.\n\n"
                        f"Feedback ID: {feedback_id}\n"
                        f"Name: {name}\n"
                        f"Email: {email}\n"
                        f"Sentiment: {sentiment}\n"
                        f"Confidence: {confidence_text}\n\n"
                        "Message:\n"
                        f"{message}\n"
                    ),
                    "Charset": "UTF-8",
                }
            },
        },
    )


def lambda_handler(event, context):
    function_name = "submit_feedback"

    table_name = os.environ.get("FEEDBACK_TABLE", "feedback_table")
    origin = os.environ.get("ALLOWED_ORIGIN", "*")
    language_code = os.environ.get("LANGUAGE_CODE", "en")
    notify_email = os.environ.get("NOTIFY_EMAIL", "").strip()

    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return _response(200, {"ok": True}, origin)

    try:
        raw_body = event.get("body") or ""
        if not raw_body:
            return _response(400, {"error": "Request body is required"}, origin)

        if event.get("isBase64Encoded"):
            import base64
            raw_body = base64.b64decode(raw_body).decode("utf-8")

        data = json.loads(raw_body)

        name = (data.get("name") or "").strip()
        email = (data.get("email") or "").strip()
        message = (data.get("message") or "").strip()

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

        sentiment = "UNKNOWN"
        confidence_score = None

        try:
            comp = comprehend.detect_sentiment(
                Text=message,
                LanguageCode=language_code
            )
            sentiment = comp.get("Sentiment", "UNKNOWN")
            scores = comp.get("SentimentScore", {})
            raw_score = scores.get(sentiment.capitalize(), 0.0)
            confidence_score = Decimal(str(raw_score))
        except ClientError as e:
            _log_error(context, f"{function_name}_comprehend", e)

        ttl_days = 90
        ttl_seconds = int(time.time()) + (ttl_days * 24 * 60 * 60)
        feedback_id = str(uuid.uuid4())

        item = {
            "feedbackId": feedback_id,
            "name": name,
            "email": email,
            "message": message,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "sentiment": sentiment,
            "ttl": ttl_seconds
        }

        if confidence_score is not None:
            item["confidenceScore"] = confidence_score

        table = dynamodb.Table(table_name)
        table.put_item(Item=item)

        try:
            _send_notification_email(
                notify_email=notify_email,
                name=name,
                email=email,
                message=message,
                feedback_id=feedback_id,
                sentiment=sentiment,
                confidence_score=confidence_score,
            )
        except ClientError as e:
            _log_error(context, f"{function_name}_ses", e)

        return _response(
            200,
            {
                "message": "Feedback saved",
                "feedbackId": feedback_id,
                "sentiment": sentiment,
                "confidenceScore": float(confidence_score) if confidence_score is not None else None,
            },
            origin,
        )

    except json.JSONDecodeError as e:
        _log_error(context, function_name, e)
        return _response(400, {"error": "Invalid JSON"}, origin)

    except ClientError as e:
        _log_error(context, function_name, e)
        return _response(500, {"error": "Database error"}, origin)

    except Exception as e:
        _log_error(context, function_name, e)
        return _response(500, {"error": "Internal server error"}, origin)
