import os
import json
import base64
import boto3
from botocore.exceptions import ClientError

# Create an S3 client using the Lambda execution role credentials
s3 = boto3.client("s3")


def lambda_handler(event, context):
    # Read the allowed frontend origin for CORS
    origin = os.environ.get("ALLOWED_ORIGIN", "*")

    # Read S3 bucket and object key from environment variables.
    # These tell Lambda where the CV PDF is stored.
    pdf_bucket = os.environ.get("PDF_BUCKET")
    pdf_key = os.environ.get("PDF_KEY", "Joel-George-CV.pdf")

    try:
        # Fail early if the bucket name has not been provided
        if not pdf_bucket:
            return {
                "statusCode": 500,
                "headers": {
                    "Content-Type": "application/json",
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Methods": "GET,OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type",
                },
                "body": json.dumps({"error": "PDF_BUCKET env var not set"})
            }

        # Get the PDF file from S3
        obj = s3.get_object(Bucket=pdf_bucket, Key=pdf_key)

        # Read the raw PDF bytes from the S3 object
        pdf_bytes = obj["Body"].read()

        # API Gateway requires binary files to be base64 encoded
        pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")

        # Return the PDF as a downloadable file
        return {
            "statusCode": 200,
            "isBase64Encoded": True,
            "headers": {
                # Tell the browser the response is a PDF file
                "Content-Type": "application/pdf",

                # Force download with this filename
                "Content-Disposition": 'attachment; filename="Joel-George-CV.pdf"',

                # CORS headers
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "GET,OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            },
            "body": pdf_b64
        }

    except ClientError:
        # AWS-specific errors, such as:
        # - bucket not found
        # - file not found
        # - permission denied
        return {
            "statusCode": 404,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": origin,
            },
            "body": json.dumps({"error": "PDF not found in S3"})
        }

    except Exception as e:
        # Catch-all for unexpected errors
        return {
            "statusCode": 500,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": origin,
            },
            "body": json.dumps({"error": "Server error", "details": str(e)})
        }