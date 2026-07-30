"""Lambda for Face Liveness session management.

Creates sessions and retrieves results. The mobile app calls these endpoints
via the API Gateway; the streaming itself goes directly from the device to
Rekognition using temporary credentials from the Cognito Identity Pool.
"""

import json
import logging

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

rekognition = boto3.client("rekognition")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "content-type,x-api-key",
    "Access-Control-Allow-Methods": "POST,GET,OPTIONS",
}


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", **CORS_HEADERS},
        "body": json.dumps(body),
    }


def _create_session(event: dict) -> dict:
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode("utf-8")

    payload = json.loads(body) if isinstance(body, str) else body
    settings = payload.get("settings", {})

    params = {}
    if settings.get("auditImagesLimit"):
        params["Settings"] = {
            "AuditImagesLimit": int(settings["auditImagesLimit"])
        }

    try:
        result = rekognition.create_face_liveness_session(**params)
        return _response(200, {"sessionId": result["SessionId"]})
    except ClientError as e:
        logger.error("CreateFaceLivenessSession error: %s", e)
        return _response(502, {"message": "Failed to create liveness session"})


def _get_results(event: dict) -> dict:
    path_params = event.get("pathParameters") or {}
    session_id = path_params.get("sessionId")

    if not session_id:
        return _response(400, {"message": "Missing sessionId path parameter"})

    try:
        result = rekognition.get_face_liveness_session_results(SessionId=session_id)
        # Confidence is only meaningful when the session SUCCEEDED; the app
        # must gate on status before trusting it.
        response_body = {
            "sessionId": session_id,
            "status": result.get("Status", ""),
            "confidence": result.get("Confidence", 0.0),
        }
        ref_image = result.get("ReferenceImage") or {}
        # Without OutputConfig the reference image comes back as raw bytes;
        # with OutputConfig it is an S3 pointer {Bucket, Name}.
        if ref_image.get("Bytes"):
            import base64
            response_body["referenceImage"] = {
                "imageBase64": base64.b64encode(ref_image["Bytes"]).decode("ascii"),
            }
        elif ref_image.get("S3Object"):
            response_body["referenceImage"] = {
                "bucket": ref_image["S3Object"].get("Bucket", ""),
                "key": ref_image["S3Object"].get("Name", ""),
            }
        return _response(200, response_body)
    except ClientError as e:
        logger.error("GetFaceLivenessSessionResults error: %s", e)
        code = e.response["Error"]["Code"]
        if code == "SessionNotFoundException":
            return _response(404, {"message": "Session not found"})
        return _response(502, {"message": "Failed to get liveness results"})


def handler(event: dict, _context) -> dict:
    path = (event.get("resource") or event.get("path") or "").rstrip("/")
    method = event.get("httpMethod", "GET")

    if "create-session" in path and method == "POST":
        return _create_session(event)
    elif "session" in path and "result" in path and method == "GET":
        return _get_results(event)
    else:
        return _response(404, {"message": f"Unknown route: {method} {path}"})
