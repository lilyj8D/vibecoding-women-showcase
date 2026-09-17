"""
Project Showcase & Voting — AWS Lambda (Function URL) backend.

Women in AI/ML (Amazon) x Singapore Computer Society.

Single Lambda that:
  - GET  /          serves the showcase/voting page (showcase.html)
  - GET  /projects  returns the public projects + leaderboard JSON (polling endpoint)
  - GET  /file      streams a private S3-stored uploaded file
  - GET  /thumbnail streams a private S3-stored project thumbnail image
  - POST /submit     creates a project (URL or uploaded file)
  - POST /update     edits a project with its secret edit token
  - POST /delete     removes a project with its secret edit token
  - POST /vote       casts a vote (server-enforced limits + deadline)

Storage: DynamoDB (projects + votes), S3 (private uploads), SES (host + optional submitter mail).
Region: us-west-2. Deployed as a console-uploaded zip.
"""

import os
import re
import json
import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration (Lambda environment variables). See design.md section 8.
# ---------------------------------------------------------------------------

REGION = os.environ.get("APP_REGION", "us-west-2")

PROJECTS_TABLE = os.environ.get("PROJECTS_TABLE", "vibecoding-women-showcase-projects")
VOTES_TABLE = os.environ.get("VOTES_TABLE", "vibecoding-women-showcase-votes")
UPLOAD_BUCKET = os.environ.get("UPLOAD_BUCKET", "vibecoding-women-showcase-uploads")

SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "")
HOST_EMAILS = [e.strip() for e in os.environ.get("HOST_EMAILS", "").split(",") if e.strip()]

# Deadline: 2026-10-31 23:59 SGT == 2026-10-31 15:59 UTC.
DEADLINE_UTC = os.environ.get("DEADLINE_UTC", "2026-10-31T15:59:00Z")

# Submitter confirmation defaults OFF (account SES is in sandbox mode).
SEND_SUBMITTER_CONFIRMATION = os.environ.get("SEND_SUBMITTER_CONFIRMATION", "false").lower() == "true"

MAX_VOTES_PER_EMAIL = int(os.environ.get("MAX_VOTES_PER_EMAIL", "3"))
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", str(6 * 1024 * 1024)))  # 6 MB
MAX_THUMBNAIL_BYTES = int(os.environ.get("MAX_THUMBNAIL_BYTES", str(2 * 1024 * 1024)))  # 2 MB

# Allowed upload content types -> canonical extension.
ALLOWED_CONTENT_TYPES = {
    "application/zip": "zip",
    "application/x-zip-compressed": "zip",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "application/pdf": "pdf",
    "text/html": "html",
}

ALLOWED_THUMBNAIL_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------------------
# Pure helpers (no AWS calls) — safe to unit-test locally.
# ---------------------------------------------------------------------------

def parse_deadline(value=None):
    """Parse the deadline env value (ISO-8601, 'Z' allowed) into an aware UTC datetime."""
    raw = value if value is not None else DEADLINE_UTC
    raw = raw.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def now_utc():
    """Current time as an aware UTC datetime (wrapped for testability)."""
    return datetime.now(timezone.utc)


def is_open(now=None, deadline=None):
    """True while submissions/voting are open (strictly before the deadline)."""
    current = now if now is not None else now_utc()
    end = deadline if deadline is not None else parse_deadline()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current < end


def normalize_email(email):
    """Trim + lowercase an email for consistent dedupe keys."""
    return (email or "").strip().lower()


def hash_edit_token(token):
    """Return a stable SHA-256 digest; raw edit tokens are never stored."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def is_valid_email(email):
    """Loose but practical email validation."""
    return bool(_EMAIL_RE.match(normalize_email(email)))


def is_valid_http_url(url):
    """Accept only well-formed http(s) URLs with a host."""
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return False
    # require something that looks like a host after the scheme
    remainder = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
    host = remainder.split("/")[0].split("?")[0]
    return "." in host and len(host) >= 3


def validate_file(content_type, raw_bytes):
    """
    Validate an uploaded file's type and size.
    Returns (ok: bool, error: str|None, extension: str|None).
    """
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct not in ALLOWED_CONTENT_TYPES:
        return False, "Unsupported file type. Allowed: ZIP, PNG, JPG, GIF, WEBP, PDF, HTML.", None
    if raw_bytes is None:
        return False, "File content is empty.", None
    if len(raw_bytes) == 0:
        return False, "File content is empty.", None
    if len(raw_bytes) > MAX_FILE_BYTES:
        mb = MAX_FILE_BYTES / (1024 * 1024)
        return False, "File is too large. Maximum size is %.0f MB." % mb, None
    return True, None, ALLOWED_CONTENT_TYPES[ct]


def validate_thumbnail(content_type, raw_bytes):
    """
    Validate an uploaded thumbnail image's type and size.
    Returns (ok: bool, error: str|None, extension: str|None).
    """
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct not in ALLOWED_THUMBNAIL_TYPES:
        return False, "Unsupported thumbnail type. Use JPG, PNG, or WEBP.", None
    if not raw_bytes:
        return False, "Thumbnail image is empty.", None
    if len(raw_bytes) > MAX_THUMBNAIL_BYTES:
        mb = MAX_THUMBNAIL_BYTES / (1024 * 1024)
        return False, "Thumbnail is too large. Maximum size is %.0f MB." % mb, None
    return True, None, ALLOWED_THUMBNAIL_TYPES[ct]


def decode_base64(data):
    """Decode a base64 string (tolerating a data: URI prefix). Returns bytes or None."""
    if not data:
        return None
    if "," in data and data.strip().lower().startswith("data:"):
        data = data.split(",", 1)[1]
    try:
        return base64.b64decode(data)
    except Exception:
        return None


def validate_submission(payload):
    """
    Validate a /submit payload's required fields and URL-or-file choice.
    Returns (ok: bool, error: str|None). Does not touch the file bytes here.
    """
    if not (payload.get("name") or "").strip():
        return False, "Name is required."
    if not is_valid_email(payload.get("email")):
        return False, "A valid email is required."
    if not (payload.get("title") or "").strip():
        return False, "Project title is required."

    link_type = (payload.get("link_type") or "").strip().lower()
    if link_type == "url":
        if not is_valid_http_url(payload.get("project_url")):
            return False, "Please provide a valid http(s) project URL."
    elif link_type == "file":
        if not payload.get("file_base64"):
            return False, "Please attach a file."
        if not payload.get("file_name"):
            return False, "Uploaded file is missing a name."
    else:
        return False, "Provide either a project URL or an uploaded file."
    return True, None


# ---------------------------------------------------------------------------
# AWS clients (created lazily so the pure helpers import without AWS/boto3).
# ---------------------------------------------------------------------------

import uuid

_dynamodb = None
_s3 = None
_ses = None


def _res_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        import boto3
        _dynamodb = boto3.resource("dynamodb", region_name=REGION)
    return _dynamodb


def _client_s3():
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client("s3", region_name=REGION)
    return _s3


def _client_ses():
    global _ses
    if _ses is None:
        import boto3
        _ses = boto3.client("ses", region_name=REGION)
    return _ses


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def json_response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
            "Content-Type": "application/json",
        },
        "body": json.dumps(body),
    }


def _read_asset(filename):
    """Read a bundled asset located next to this file."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, filename), "r", encoding="utf-8") as f:
        return f.read()


def _file_route(project_id):
    return "/file?id=%s" % project_id


def _thumbnail_route(project_id):
    return "/thumbnail?id=%s" % project_id


def _store_thumbnail(payload, project_id):
    """
    Validate and store an optional uploaded thumbnail image in private S3.
    Returns (thumbnail_key|None, thumbnail_content_type|None, error|None).
    A missing thumbnail is not an error; it returns (None, None, None).
    """
    encoded = payload.get("thumbnail_base64")
    if not encoded:
        return None, None, None
    raw = decode_base64(encoded)
    if raw is None:
        return None, None, "Could not read the uploaded thumbnail."
    ok, err, ext = validate_thumbnail(payload.get("thumbnail_content_type"), raw)
    if not ok:
        return None, None, err
    content_type = (payload.get("thumbnail_content_type") or "").split(";")[0].strip()
    # Kept under the existing "uploads/" prefix so the Lambda's least-privilege S3
    # policy (uploads/*) covers thumbnails without any IAM change. The extra
    # "thumb/" segment prevents collisions with a submitter's attachment filename.
    thumbnail_key = "uploads/%s/thumb/image.%s" % (project_id, ext)
    try:
        _client_s3().put_object(
            Bucket=UPLOAD_BUCKET,
            Key=thumbnail_key,
            Body=raw,
            ContentType=content_type or "application/octet-stream",
        )
    except Exception as e:
        print("S3 thumbnail put_object failed:", repr(e))
        return None, None, "Could not store the uploaded thumbnail."
    return thumbnail_key, content_type, None


# ---------------------------------------------------------------------------
# Route: GET / (page)
# ---------------------------------------------------------------------------

def serve_page():
    try:
        html = _read_asset("showcase.html")
    except Exception:
        html = "<h1>Project Showcase</h1><p>Page asset missing.</p>"
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": html,
    }


# ---------------------------------------------------------------------------
# Route: GET /projects  (public payload + leaderboard; polling endpoint)
# ---------------------------------------------------------------------------

def get_projects():
    table = _res_dynamodb().Table(PROJECTS_TABLE)
    items = table.scan().get("Items", [])

    projects = []
    for it in items:
        if it.get("is_deleted"):
            continue
        link_type = it.get("link_type", "url")
        entry = {
            "project_id": it.get("project_id"),
            "title": it.get("title", ""),
            "submitter_name": it.get("submitter_name", ""),
            "category": it.get("category", ""),
            "description": it.get("description", ""),
            "link_type": link_type,
            "image_url": it.get("image_url", ""),
            "video_url": it.get("video_url", ""),
            "github_url": it.get("github_url", ""),
            "live_url": it.get("live_url", ""),
            "thumbnail_route": _thumbnail_route(it.get("project_id")) if it.get("thumbnail_key") else "",
            "vote_count": int(it.get("vote_count", 0) or 0),
            "created_at": it.get("created_at", ""),
        }
        if link_type == "url":
            entry["project_url"] = it.get("project_url", "")
        else:
            entry["file_route"] = _file_route(it.get("project_id"))
            entry["file_content_type"] = it.get("file_content_type", "")
            entry["file_name"] = it.get("file_name", "")
        projects.append(entry)

    # Sort by votes desc, then earliest submission first (tie-break).
    projects.sort(key=lambda p: (-p["vote_count"], p["created_at"] or ""))

    deadline = parse_deadline()
    now = now_utc()
    open_now = is_open(now, deadline)
    return json_response(200, {
        "server_time": now.isoformat(),
        "deadline": deadline.isoformat(),
        "submissions_open": open_now,
        "voting_open": open_now,
        "max_votes_per_email": MAX_VOTES_PER_EMAIL,
        "projects": projects,
    })


# ---------------------------------------------------------------------------
# Route: GET /file?id=<project_id>  (stream a private S3 object)
# ---------------------------------------------------------------------------

def get_file(project_id):
    if not project_id:
        return json_response(400, {"error": "Missing project id"})
    table = _res_dynamodb().Table(PROJECTS_TABLE)
    item = table.get_item(Key={"project_id": project_id}).get("Item")
    if (not item or item.get("is_deleted") or item.get("link_type") != "file" or
            not item.get("file_key")):
        return json_response(404, {"error": "File not found"})

    try:
        obj = _client_s3().get_object(Bucket=UPLOAD_BUCKET, Key=item["file_key"])
        raw = obj["Body"].read()
    except Exception:
        return json_response(404, {"error": "File not found"})

    content_type = item.get("file_content_type", "application/octet-stream")
    file_name = item.get("file_name", "download")
    # Inline for images/pdf/html so they preview; attachment otherwise.
    inline = content_type.startswith("image/") or content_type in ("application/pdf", "text/html")
    disposition = "inline" if inline else "attachment"
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": content_type,
            "Content-Disposition": '%s; filename="%s"' % (disposition, file_name),
            "Cache-Control": "public, max-age=3600",
        },
        "body": base64.b64encode(raw).decode("utf-8"),
        "isBase64Encoded": True,
    }


# ---------------------------------------------------------------------------
# Route: GET /thumbnail?id=<project_id>  (stream a private S3 thumbnail image)
# ---------------------------------------------------------------------------

def get_thumbnail(project_id):
    if not project_id:
        return json_response(400, {"error": "Missing project id"})
    table = _res_dynamodb().Table(PROJECTS_TABLE)
    item = table.get_item(Key={"project_id": project_id}).get("Item")
    if not item or item.get("is_deleted") or not item.get("thumbnail_key"):
        return json_response(404, {"error": "Thumbnail not found"})

    try:
        obj = _client_s3().get_object(Bucket=UPLOAD_BUCKET, Key=item["thumbnail_key"])
        raw = obj["Body"].read()
    except Exception:
        return json_response(404, {"error": "Thumbnail not found"})

    content_type = item.get("thumbnail_content_type", "image/jpeg")
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": content_type,
            "Content-Disposition": "inline",
            "Cache-Control": "public, max-age=3600",
        },
        "body": base64.b64encode(raw).decode("utf-8"),
        "isBase64Encoded": True,
    }


# ---------------------------------------------------------------------------
# Emails (best-effort; a failure never loses a submission)
# ---------------------------------------------------------------------------

def _send_host_email(item):
    if not SENDER_EMAIL or not HOST_EMAILS:
        print("SES host email skipped: SENDER_EMAIL/HOST_EMAILS not configured")
        return
    if item.get("link_type") == "url":
        link_line = "Project URL: %s" % item.get("project_url", "")
    else:
        link_line = "Uploaded file: %s (view via /file?id=%s)" % (
            item.get("file_name", ""), item.get("project_id"))
    body = (
        "New project submission — Women in AI/ML (Amazon) x Singapore Computer Society\n\n"
        "Name: %s\nEmail: %s\nTitle: %s\nCategory: %s\n\nDescription:\n%s\n\n%s\n\nSubmitted: %s UTC"
        % (item.get("submitter_name", ""), item.get("submitter_email", ""),
           item.get("title", ""), item.get("category", ""),
           item.get("description", ""), link_line, item.get("created_at", ""))
    )
    try:
        _client_ses().send_email(
            Source=SENDER_EMAIL,
            Destination={"ToAddresses": HOST_EMAILS},
            Message={
                "Subject": {"Data": "[Showcase] %s - %s" % (item.get("submitter_name", ""), item.get("title", ""))},
                "Body": {"Text": {"Data": body}},
            },
        )
    except Exception as e:  # never fail the submission
        print("SES host email failed:", repr(e))


def _send_submitter_email(item):
    if not SEND_SUBMITTER_CONFIRMATION:
        return
    if not SENDER_EMAIL:
        print("SES submitter email skipped: SENDER_EMAIL not configured")
        return
    to_addr = item.get("submitter_email", "")
    if not to_addr:
        return
    body = (
        "Hi %s,\n\nThanks for submitting your project \"%s\" to the Project Showcase "
        "(Women in AI/ML (Amazon) x Singapore Computer Society).\n\n"
        "It's now live and others can vote for it. Voting closes 31 October 2026, 23:59 SGT.\n\n"
        "Good luck!\n"
        % (item.get("submitter_name", ""), item.get("title", ""))
    )
    try:
        _client_ses().send_email(
            Source=SENDER_EMAIL,
            Destination={"ToAddresses": [to_addr]},
            Message={
                "Subject": {"Data": "Your project was received — Project Showcase"},
                "Body": {"Text": {"Data": body}},
            },
        )
    except Exception as e:
        print("SES submitter email failed:", repr(e))


# ---------------------------------------------------------------------------
# Route: POST /submit
# ---------------------------------------------------------------------------

def post_submit(payload):
    # 1. Deadline gate (server-side).
    if not is_open():
        return json_response(403, {"error": "Submissions are closed."})

    # 2. Validate required fields + URL-or-file choice.
    ok, err = validate_submission(payload)
    if not ok:
        return json_response(400, {"error": err})

    project_id = str(uuid.uuid4())
    created_at = now_utc().isoformat()
    link_type = (payload.get("link_type") or "").strip().lower()
    edit_token = secrets.token_urlsafe(32)
    edit_token_hash = hash_edit_token(edit_token)

    # Optional media and resource links (independent of the URL-or-file choice).
    image_url = (payload.get("image_url") or "").strip()
    video_url = (payload.get("video_url") or "").strip()
    github_url = (payload.get("github_url") or "").strip()
    live_url = (payload.get("live_url") or "").strip()
    optional_urls = (
        ("image", image_url),
        ("video", video_url),
        ("GitHub", github_url),
        ("live app", live_url),
    )
    for label, url in optional_urls:
        if url and not is_valid_http_url(url):
            return json_response(400, {
                "error": "The %s URL isn't a valid http(s) link." % label})

    # Optional uploaded thumbnail image (stored in private S3).
    thumbnail_key, thumbnail_content_type, thumb_err = _store_thumbnail(payload, project_id)
    if thumb_err:
        return json_response(400, {"error": thumb_err})

    item = {
        "project_id": project_id,
        "title": (payload.get("title") or "").strip(),
        "submitter_name": (payload.get("name") or "").strip(),
        "submitter_email": normalize_email(payload.get("email")),
        "description": (payload.get("description") or "").strip(),
        "category": (payload.get("category") or "").strip(),
        "link_type": link_type,
        "image_url": image_url,
        "video_url": video_url,
        "github_url": github_url,
        "live_url": live_url,
        "edit_token_hash": edit_token_hash,
        "vote_count": 0,
        "created_at": created_at,
    }
    if thumbnail_key:
        item["thumbnail_key"] = thumbnail_key
        item["thumbnail_content_type"] = thumbnail_content_type

    # 3. Handle URL vs file.
    if link_type == "url":
        item["project_url"] = (payload.get("project_url") or "").strip()
    else:
        raw = decode_base64(payload.get("file_base64"))
        if raw is None:
            return json_response(400, {"error": "Could not read the uploaded file."})
        valid, ferr, _ext = validate_file(payload.get("file_content_type"), raw)
        if not valid:
            return json_response(400, {"error": ferr})
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", (payload.get("file_name") or "file"))
        file_key = "uploads/%s/%s" % (project_id, safe_name)
        try:
            _client_s3().put_object(
                Bucket=UPLOAD_BUCKET,
                Key=file_key,
                Body=raw,
                ContentType=(payload.get("file_content_type") or "application/octet-stream").split(";")[0].strip(),
            )
        except Exception as e:
            print("S3 put_object failed:", repr(e))
            return json_response(500, {"error": "Could not store the uploaded file."})
        item["file_key"] = file_key
        item["file_name"] = safe_name
        item["file_content_type"] = (payload.get("file_content_type") or "").split(";")[0].strip()

    # 4. Store project.
    try:
        _res_dynamodb().Table(PROJECTS_TABLE).put_item(Item=item)
    except Exception as e:
        print("DynamoDB put_item failed:", repr(e))
        return json_response(500, {"error": "Could not save your submission."})

    # 5 & 6. Best-effort notifications.
    _send_host_email(item)
    _send_submitter_email(item)

    # 7. Confirmation summary.
    return json_response(200, {
        "message": "Submission received",
        "edit_token": edit_token,
        "project": {
            "project_id": project_id,
            "title": item["title"],
            "category": item["category"],
            "link_type": link_type,
            "project_url": item.get("project_url", ""),
            "file_route": _file_route(project_id) if link_type == "file" else "",
        },
    })


# ---------------------------------------------------------------------------
# Route: POST /update  (edit-token protected; preserves votes and identity)
# ---------------------------------------------------------------------------

def post_update(payload):
    if not is_open():
        return json_response(403, {"error": "Project editing is closed."})

    project_id = (payload.get("project_id") or "").strip()
    edit_token = (payload.get("edit_token") or "").strip()
    title = (payload.get("title") or "").strip()
    if not project_id or not edit_token:
        return json_response(400, {"error": "Project id and edit token are required."})
    if not title:
        return json_response(400, {"error": "Project title is required."})

    table = _res_dynamodb().Table(PROJECTS_TABLE)
    try:
        project = table.get_item(Key={"project_id": project_id}).get("Item")
    except Exception as e:
        print("DynamoDB edit lookup failed:", repr(e))
        return json_response(500, {"error": "Could not load the project."})
    if not project or project.get("is_deleted"):
        return json_response(404, {"error": "Project not found."})

    stored_hash = project.get("edit_token_hash", "")
    provided_hash = hash_edit_token(edit_token)
    if not stored_hash:
        return json_response(403, {
            "error": "Editing isn't available for this earlier submission."})
    if not hmac.compare_digest(stored_hash, provided_hash):
        return json_response(403, {"error": "The edit token is invalid."})

    link_type = (payload.get("link_type") or project.get("link_type") or "").strip().lower()
    if link_type not in ("url", "file"):
        return json_response(400, {"error": "Choose a project URL or uploaded file."})

    optional_urls = {
        "image_url": (payload.get("image_url") or "").strip(),
        "video_url": (payload.get("video_url") or "").strip(),
        "github_url": (payload.get("github_url") or "").strip(),
        "live_url": (payload.get("live_url") or "").strip(),
    }
    labels = {
        "image_url": "image",
        "video_url": "video",
        "github_url": "GitHub",
        "live_url": "live app",
    }
    for field, url in optional_urls.items():
        if url and not is_valid_http_url(url):
            return json_response(400, {
                "error": "The %s URL isn't a valid http(s) link." % labels[field]})

    updates = {
        "title": title,
        "description": (payload.get("description") or "").strip(),
        "link_type": link_type,
        "updated_at": now_utc().isoformat(),
    }
    updates.update(optional_urls)

    # Optional replacement thumbnail. If none is uploaded, the existing one is kept.
    if payload.get("thumbnail_base64"):
        new_key, new_ct, thumb_err = _store_thumbnail(payload, project_id)
        if thumb_err:
            return json_response(400, {"error": thumb_err})
        updates["thumbnail_key"] = new_key
        updates["thumbnail_content_type"] = new_ct

    if link_type == "url":
        project_url = (payload.get("project_url") or "").strip()
        if not is_valid_http_url(project_url):
            return json_response(400, {"error": "Please provide a valid http(s) project URL."})
        updates["project_url"] = project_url
    else:
        encoded_file = payload.get("file_base64")
        if encoded_file:
            raw = decode_base64(encoded_file)
            if raw is None:
                return json_response(400, {"error": "Could not read the uploaded file."})
            valid, file_error, _ext = validate_file(payload.get("file_content_type"), raw)
            if not valid:
                return json_response(400, {"error": file_error})
            safe_name = re.sub(
                r"[^A-Za-z0-9._-]", "_", (payload.get("file_name") or "file"))
            file_key = "uploads/%s/%s" % (project_id, safe_name)
            try:
                _client_s3().put_object(
                    Bucket=UPLOAD_BUCKET,
                    Key=file_key,
                    Body=raw,
                    ContentType=(payload.get("file_content_type") or
                                 "application/octet-stream").split(";")[0].strip(),
                )
            except Exception as e:
                print("S3 edit put_object failed:", repr(e))
                return json_response(500, {"error": "Could not store the replacement file."})
            updates["file_key"] = file_key
            updates["file_name"] = safe_name
            updates["file_content_type"] = (
                payload.get("file_content_type") or "").split(";")[0].strip()
        elif project.get("link_type") != "file" or not project.get("file_key"):
            return json_response(400, {
                "error": "Attach a file when changing this project to file upload."})

    # Update only editable fields. vote_count, submitter identity, created_at and
    # edit_token_hash are intentionally absent, so concurrent votes cannot be lost.
    names = {"#token": "edit_token_hash"}
    values = {":expected_token": provided_hash}
    assignments = []
    for index, (field, value) in enumerate(updates.items()):
        name_key = "#field%d" % index
        value_key = ":value%d" % index
        names[name_key] = field
        values[value_key] = value
        assignments.append("%s = %s" % (name_key, value_key))

    try:
        table.update_item(
            Key={"project_id": project_id},
            UpdateExpression="SET " + ", ".join(assignments),
            ConditionExpression="#token = :expected_token",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            return json_response(403, {"error": "The edit token is invalid."})
        print("DynamoDB project update failed:", repr(e))
        return json_response(500, {"error": "Could not save the project changes."})

    return json_response(200, {
        "message": "Project updated",
        "project": {
            "project_id": project_id,
            "title": title,
            "link_type": link_type,
        },
    })


# ---------------------------------------------------------------------------
# Route: POST /delete  (edit-token protected soft deletion)
# ---------------------------------------------------------------------------

def post_delete(payload):
    if not is_open():
        return json_response(403, {"error": "Project deletion is closed."})

    project_id = (payload.get("project_id") or "").strip()
    edit_token = (payload.get("edit_token") or "").strip()
    if not project_id or not edit_token:
        return json_response(400, {"error": "Project id and edit token are required."})

    table = _res_dynamodb().Table(PROJECTS_TABLE)
    try:
        project = table.get_item(Key={"project_id": project_id}).get("Item")
    except Exception as e:
        print("DynamoDB delete lookup failed:", repr(e))
        return json_response(500, {"error": "Could not load the project."})
    if not project or project.get("is_deleted"):
        return json_response(404, {"error": "Project not found."})

    stored_hash = project.get("edit_token_hash", "")
    provided_hash = hash_edit_token(edit_token)
    if not stored_hash:
        return json_response(403, {
            "error": "Deletion isn't available for this earlier submission."})
    if not hmac.compare_digest(stored_hash, provided_hash):
        return json_response(403, {"error": "The edit token is invalid."})

    try:
        table.update_item(
            Key={"project_id": project_id},
            UpdateExpression="SET #deleted = :yes, #deleted_at = :now",
            ConditionExpression="#token = :expected_token AND "
                                "(attribute_not_exists(#deleted) OR #deleted = :no)",
            ExpressionAttributeNames={
                "#deleted": "is_deleted",
                "#deleted_at": "deleted_at",
                "#token": "edit_token_hash",
            },
            ExpressionAttributeValues={
                ":yes": True,
                ":no": False,
                ":now": now_utc().isoformat(),
                ":expected_token": provided_hash,
            },
        )
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            return json_response(403, {
                "error": "The edit token is invalid or the project was already deleted."})
        print("DynamoDB project soft delete failed:", repr(e))
        return json_response(500, {"error": "Could not delete the project."})

    return json_response(200, {
        "message": "Project deleted",
        "project_id": project_id,
    })


# ---------------------------------------------------------------------------
# Route: POST /vote  (server-enforced: <=3 per email, <=1 per project, deadline)
# ---------------------------------------------------------------------------

def post_vote(payload):
    # 1. Deadline gate.
    if not is_open():
        return json_response(403, {"error": "Voting is closed."})

    # 2. Validate email + project.
    email = normalize_email(payload.get("email"))
    if not is_valid_email(email):
        return json_response(400, {"error": "A valid email is required to vote."})
    project_id = (payload.get("project_id") or "").strip()
    if not project_id:
        return json_response(400, {"error": "Missing project id."})

    projects = _res_dynamodb().Table(PROJECTS_TABLE)
    votes = _res_dynamodb().Table(VOTES_TABLE)

    project = projects.get_item(Key={"project_id": project_id}).get("Item")
    if not project or project.get("is_deleted"):
        return json_response(404, {"error": "Project not found."})

    # Deleted/missing projects no longer consume a voter's allowance. Vote rows
    # remain as an audit trail, but only votes pointing to active projects count.
    from boto3.dynamodb.conditions import Key as _Key
    existing = votes.query(KeyConditionExpression=_Key("voter_email").eq(email)).get("Items", [])
    active_existing = []
    for vote in existing:
        voted_project_id = vote.get("project_id")
        if not voted_project_id:
            continue
        voted_project = projects.get_item(
            Key={"project_id": voted_project_id}).get("Item")
        if voted_project and not voted_project.get("is_deleted"):
            active_existing.append(vote)

    if any(v.get("project_id") == project_id for v in active_existing):
        return json_response(409, {"error": "You already voted for this project."})
    if len(active_existing) >= MAX_VOTES_PER_EMAIL:
        return json_response(409, {
            "error": "You've used all %d of your votes." % MAX_VOTES_PER_EMAIL})

    # 5. Conditional put (guards double-submit races) + atomic counter increment.
    try:
        votes.put_item(
            Item={"voter_email": email, "project_id": project_id, "created_at": now_utc().isoformat()},
            ConditionExpression="attribute_not_exists(voter_email) AND attribute_not_exists(project_id)",
        )
    except Exception as e:
        # Condition failed => concurrent duplicate for the same project.
        print("Vote conditional put rejected:", repr(e))
        return json_response(409, {"error": "You already voted for this project."})

    try:
        updated = projects.update_item(
            Key={"project_id": project_id},
            UpdateExpression="SET vote_count = if_not_exists(vote_count, :zero) + :one",
            ExpressionAttributeValues={":one": 1, ":zero": 0},
            ReturnValues="UPDATED_NEW",
        )
        new_count = int(updated["Attributes"]["vote_count"])
    except Exception as e:
        print("Vote counter update failed:", repr(e))
        new_count = int(project.get("vote_count", 0) or 0) + 1

    votes_used = len(active_existing) + 1
    return json_response(200, {
        "message": "Vote recorded",
        "project_id": project_id,
        "vote_count": new_count,
        "votes_used": votes_used,
        "votes_remaining": max(0, MAX_VOTES_PER_EMAIL - votes_used),
    })


# ---------------------------------------------------------------------------
# Lambda entry point / router
# ---------------------------------------------------------------------------

def _extract_method(event):
    """Method from HTTP API / Function URL (v2) or REST API (v1)."""
    m = event.get("requestContext", {}).get("http", {}).get("method")
    if m:
        return m
    return event.get("httpMethod", "GET")


def _extract_path(event):
    """
    Path from HTTP API / Function URL (v2 `rawPath`) or REST API (v1 `path`).
    Normalized to a leading-slash route with any API Gateway stage prefix removed,
    so `/prod/projects` and `/projects` both map to `/projects`.
    """
    path = event.get("rawPath") or event.get("path") or "/"
    known = ("/submit", "/update", "/delete", "/vote", "/projects", "/file", "/thumbnail")
    for route in known:
        if path == route or path.endswith(route):
            return route
    # Strip a single stage segment if present (e.g. /prod -> /)
    parts = [p for p in path.split("/") if p]
    if len(parts) <= 1:
        return "/"
    return "/" + "/".join(parts[1:])


def _extract_body(event):
    """Return the decoded request body string, handling base64-encoded bodies."""
    body = event.get("body")
    if body is None:
        return "{}"
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except Exception:
            pass
    return body


def lambda_handler(event, context):
    method = _extract_method(event)
    path = _extract_path(event)
    qs = event.get("queryStringParameters") or {}

    if method == "OPTIONS":
        return json_response(200, {"ok": True})

    if method == "GET":
        if path == "/" or path == "":
            return serve_page()
        if path == "/projects":
            return get_projects()
        if path == "/file":
            return get_file(qs.get("id"))
        if path == "/thumbnail":
            return get_thumbnail(qs.get("id"))
        # Unknown GET -> serve the page (SPA-style fallback).
        return serve_page()

    if method == "POST":
        try:
            payload = json.loads(_extract_body(event) or "{}")
        except (json.JSONDecodeError, TypeError):
            return json_response(400, {"error": "Invalid JSON."})
        if path == "/submit":
            return post_submit(payload)
        if path == "/update":
            return post_update(payload)
        if path == "/delete":
            return post_delete(payload)
        if path == "/vote":
            return post_vote(payload)
        return json_response(404, {"error": "Unknown endpoint."})

    return json_response(405, {"error": "Method not allowed."})
