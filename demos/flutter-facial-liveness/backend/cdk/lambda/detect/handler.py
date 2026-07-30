"""Lambda proxy for Amazon Rekognition.

Receives a base64-encoded image and returns detected faces or labels. This is
the only place that holds AWS credentials (via the execution role) — the mobile
app never does. The role is scoped to detect_faces / detect_labels only.
"""

import base64
import json
import logging

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

rekognition = boto3.client("rekognition")

# Cap decoded image size to protect cost and Rekognition's 5MB sync limit.
MAX_IMAGE_BYTES = 5 * 1024 * 1024

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",  # tighten to the Next.js origin in prod
    "Access-Control-Allow-Headers": "content-type,x-api-key",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
}


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", **CORS_HEADERS},
        "body": json.dumps(body),
    }


def _decode_image(event: dict) -> bytes:
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    payload = json.loads(body)
    image_b64 = payload.get("image")
    if not image_b64:
        raise ValueError("Missing 'image' field")
    image = base64.b64decode(image_b64)
    if len(image) > MAX_IMAGE_BYTES:
        raise ValueError("Image exceeds 5MB limit")
    return image


def handler(event: dict, _context) -> dict:
    # API Gateway proxy integration: the operation is the last path segment.
    path = (event.get("resource") or event.get("path") or "").rstrip("/")
    operation = path.rsplit("/", 1)[-1]

    try:
        image = _decode_image(event)
    except (ValueError, json.JSONDecodeError, base64.binascii.Error) as e:
        logger.warning("Bad request: %s", e)
        return _response(400, {"message": str(e)})

    try:
        if operation == "detect-faces":
            result = rekognition.detect_faces(
                Image={"Bytes": image}, Attributes=["DEFAULT"]
            )
            return _response(200, {"FaceDetails": result.get("FaceDetails", [])})
        elif operation == "detect-labels":
            result = rekognition.detect_labels(
                Image={"Bytes": image}, MaxLabels=20, MinConfidence=70
            )
            return _response(200, {"Labels": result.get("Labels", [])})
        else:
            return _response(404, {"message": f"Unknown operation: {operation}"})
    except ClientError as e:
        logger.error("Rekognition error: %s", e)
        return _response(502, {"message": "Rekognition call failed"})
