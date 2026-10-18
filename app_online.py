import streamlit as st
import requests
import os
import re
import io
import json
import uuid
import time
import html
import base64
import zipfile
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

# =========================================================
# PAGE SETTINGS
# =========================================================

st.set_page_config(
    page_title="Document Control Portal",
    page_icon="📁",
    layout="wide"
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BUCKET = "project-files"


# =========================================================
# SECRETS (set in Streamlit Cloud -> App settings -> Secrets)
# =========================================================

def secret(name, default=""):

    try:
        value = st.secrets[name]
    except Exception:
        value = os.environ.get(name, default)

    return str(value).strip()


SUPABASE_URL = secret("SUPABASE_URL").rstrip("/")

SUPABASE_KEY = secret("SUPABASE_ANON_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:

    st.error(
        "Missing SUPABASE_URL or SUPABASE_ANON_KEY. "
        "Add them in the app Secrets."
    )

    st.stop()


# =========================================================
# SUPABASE LOW-LEVEL (plain HTTPS, no extra packages)
# =========================================================

def get_token():
    """Return a valid login token, refreshing it when it is about to expire."""

    auth = st.session_state.get("auth")

    if not auth:
        return None

    if time.time() > auth["expires_at"] - 60:

        try:

            r = requests.post(
                f"{SUPABASE_URL}/auth/v1/token",
                params={"grant_type": "refresh_token"},
                json={"refresh_token": auth["refresh"]},
                headers={"apikey": SUPABASE_KEY},
                timeout=30
            )

            if r.status_code == 200:

                d = r.json()

                auth["token"] = d["access_token"]
                auth["refresh"] = d["refresh_token"]
                auth["expires_at"] = time.time() + d.get("expires_in", 3600)

            else:

                st.session_state.pop("auth", None)

                return None

        except Exception:

            return auth["token"]

    return auth["token"]


def api(method, path, params=None, json_body=None, data=None,
        headers=None, timeout=60):

    h = {"apikey": SUPABASE_KEY}

    token = get_token()

    if token:
        h["Authorization"] = f"Bearer {token}"

    if headers:
        h.update(headers)

    return requests.request(
        method,
        f"{SUPABASE_URL}{path}",
        params=params,
        json=json_body,
        data=data,
        headers=h,
        timeout=timeout
    )


def err(r):
    return f"{r.status_code}: {r.text[:200]}"


def login(email, password):

    try:

        r = requests.post(
            f"{SUPABASE_URL}/auth/v1/token",
            params={"grant_type": "password"},
            json={"email": email, "password": password},
            headers={"apikey": SUPABASE_KEY},
            timeout=30
        )

    except Exception as e:

        return False, f"Connection error: {e}"

    if r.status_code in (400, 401):
        return False, "Wrong email or password."

    if r.status_code != 200:

        return False, (
            f"Service error ({r.status_code}). "
            "The database may be paused or the keys are wrong."
        )

    d = r.json()

    auth = {
        "token": d["access_token"],
        "refresh": d["refresh_token"],
        "expires_at": time.time() + d.get("expires_in", 3600),
        "email": email,
        "user_id": d["user"]["id"],
        "role": "viewer"
    }

    st.session_state.auth = auth

    rr = api(
        "GET",
        "/rest/v1/profiles",
        params={"select": "role", "id": f"eq.{auth['user_id']}"}
    )

    if rr.status_code == 200 and rr.json():
        auth["role"] = rr.json()[0]["role"]

    return True, ""


# =========================================================
# HELPERS
# =========================================================

def fmt_time(value):

    try:

        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))

        return dt.astimezone(
            ZoneInfo("Africa/Cairo")
        ).strftime("%Y-%m-%d %H:%M")

    except Exception:

        return str(value)[:16].replace("T", " ")


def format_size(size):

    size = float(size or 0)

    for unit in ["B", "KB", "MB", "GB"]:

        if size < 1024:
            return f"{size:.1f} {unit}"

        size /= 1024

    return f"{size:.1f} TB"


def md_safe(text):
    """Escape characters that Streamlit would read as formatting."""

    return re.sub(r"([\\`*_\[\]~$:<>#|])", r"\\\1", str(text))


def number_key(number):
    """Sort 1, 2, 10 as numbers, then any non-numeric ones as text."""

    if str(number).isdigit():
        return (0, int(number), str(number))

    return (1, 0, str(number))


def show_table(rows):
    """Simple HTML table (no pyarrow needed)."""

    if not rows:
        return

    headers = list(rows[0].keys())

    head = "".join(
        f"<th style='text-align:left;padding:8px;"
        f"border-bottom:2px solid rgba(128,128,128,0.4)'>"
        f"{html.escape(h)}</th>"
        for h in headers
    )

    body = ""

    for r in rows:

        cells = "".join(
            f"<td style='padding:8px;"
            f"border-bottom:1px solid rgba(128,128,128,0.25)'>"
            f"{html.escape(str(r[h] if r[h] is not None else ''))}</td>"
            for h in headers
        )

        body += f"<tr>{cells}</tr>"

    st.markdown(
        "<div style='overflow-x:auto'>"
        "<table style='width:100%;border-collapse:collapse;font-size:14px'>"
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody>"
        "</table></div>",
        unsafe_allow_html=True
    )


# =========================================================
# PROJECTS (database)
# =========================================================

def get_projects(recent=False):
    """List of (number, name, client, status). recent=True: newest first."""

    params = {
        "select": "id,project_number,project_name,client,status",
        "order": "id.desc",
        "limit": 5000
    }

    r = api("GET", "/rest/v1/projects", params=params)

    if r.status_code != 200:

        st.error(f"Could not load projects ({err(r)})")

        return []

    rows = r.json()

    if not recent:
        rows.sort(key=lambda x: number_key(x["project_number"]))

    return [
        (
            x["project_number"],
            x["project_name"],
            x["client"] or "",
            x["status"]
        )
        for x in rows
    ]


def get_project_id(number):

    r = api(
        "GET",
        "/rest/v1/projects",
        params={"select": "id", "project_number": f"eq.{number}"}
    )

    if r.status_code == 200 and r.json():
        return r.json()[0]["id"]

    return None


def add_project(number, name, client, status):

    r = api(
        "POST",
        "/rest/v1/projects",
        json_body={
            "project_number": number,
            "project_name": name,
            "client": client,
            "status": status
        },
        headers={"Prefer": "return=minimal"}
    )

    if r.status_code in (200, 201, 204):
        return True, "Project added successfully."

    if r.status_code == 409:
        return False, "Project Number already exists."

    if r.status_code in (401, 403):
        return False, "You are not allowed to add projects."

    return False, f"Could not add the project ({err(r)})"


def update_project_status(number, status):
    """Change a project to Active or Closed."""

    r = api(
        "PATCH",
        "/rest/v1/projects",
        params={"project_number": f"eq.{number}"},
        json_body={"status": status},
        headers={"Prefer": "return=representation"}
    )

    if r.status_code == 200:

        if r.json():
            return True, f"Project {number} is now {status}."

        return False, "Nothing changed (not allowed, or project not found)."

    if r.status_code in (401, 403):
        return False, "You are not allowed to change projects."

    return False, f"Could not change the status ({err(r)})"


def bulk_add_projects(rows, status):
    """Insert many projects. Existing numbers are skipped."""

    added = 0

    for i in range(0, len(rows), 200):

        chunk = [
            {
                "project_number": n,
                "project_name": name,
                "client": client,
                "status": status
            }
            for n, name, client in rows[i:i + 200]
        ]

        r = api(
            "POST",
            "/rest/v1/projects",
            params={"on_conflict": "project_number"},
            json_body=chunk,
            headers={
                "Prefer": "resolution=ignore-duplicates,"
                          "return=representation"
            }
        )

        if r.status_code not in (200, 201):
            return None, None, f"Import failed ({err(r)})"

        added += len(r.json())

    return added, len(rows) - added, None


def delete_storage(paths):

    if paths:

        api(
            "DELETE",
            f"/storage/v1/object/{BUCKET}",
            json_body={"prefixes": paths}
        )


def delete_project(number):
    """Delete the project, its file records, AI results and stored files."""

    paths = [d[7] for d in get_documents(number)]

    r = api(
        "DELETE",
        "/rest/v1/projects",
        params={"project_number": f"eq.{number}"},
        headers={"Prefer": "return=representation"}
    )

    if r.status_code not in (200, 204):
        return False, f"Could not delete ({err(r)})"

    if not r.json():
        return False, "Nothing was deleted (not allowed, or project not found)."

    delete_storage(paths)

    return True, f"Project {number} was deleted with its files."


def read_projects_excel(file):
    """Read projects from an Excel file (first sheet, header in row 1).
    Returns (rows, error). Each row is (number, name, client)."""

    try:
        from openpyxl import load_workbook
    except ImportError:
        return None, "The package openpyxl is missing."

    try:
        wb = load_workbook(file, data_only=True, read_only=True)
        data = list(wb.worksheets[0].iter_rows(values_only=True))
        wb.close()
    except Exception as e:
        return None, f"Could not read the file: {e}"

    if not data:
        return None, "The file is empty."

    header = [str(h or "").strip().lower() for h in data[0]]

    def find(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None

    i_client = find("client name", "client")
    i_name = find("project name", "name")
    i_number = find("project number", "project no", "project no.", "number")

    if i_name is None or i_number is None:
        return None, (
            "Could not find the columns 'Project Name' and "
            "'Project Number' in the first row."
        )

    def clean(v):

        if v is None:
            return ""

        if isinstance(v, float) and v.is_integer():
            v = int(v)

        return " ".join(str(v).split())

    def cell(row, i):
        return clean(row[i]) if i is not None and i < len(row) else ""

    rows = []

    for r in data[1:]:

        number = cell(r, i_number)
        name = cell(r, i_name)
        client = cell(r, i_client)

        if number and name:
            rows.append((number, name, client))

    if not rows:
        return None, "No projects found in the file."

    return rows, None


# =========================================================
# DOCUMENTS (database + file storage)
# =========================================================

def add_document(project_number, file_name, storage_path,
                 file_type, file_size, notes):

    r = api(
        "POST",
        "/rest/v1/documents",
        json_body={
            "project_number": project_number,
            "file_name": file_name,
            "storage_path": storage_path,
            "file_type": file_type,
            "file_size": file_size,
            "notes": notes
        },
        headers={"Prefer": "return=minimal"}
    )

    if r.status_code in (200, 201, 204):
        return True, ""

    return False, f"Could not save the file record ({err(r)})"


def get_documents(project_number=None):
    """List of (id, project, name, type, size, notes, uploaded, path)."""

    params = {
        "select": "id,project_number,file_name,file_type,file_size,"
                  "notes,uploaded_at,storage_path",
        "order": "id.desc",
        "limit": 5000
    }

    if project_number:
        params["project_number"] = f"eq.{project_number}"

    r = api("GET", "/rest/v1/documents", params=params)

    if r.status_code != 200:

        st.error(f"Could not load documents ({err(r)})")

        return []

    return [
        (
            d["id"],
            d["project_number"],
            d["file_name"],
            d["file_type"],
            d["file_size"],
            d["notes"] or "",
            fmt_time(d["uploaded_at"]),
            d["storage_path"]
        )
        for d in r.json()
    ]


def count_documents():

    r = api(
        "GET",
        "/rest/v1/documents",
        params={"select": "id", "limit": 1},
        headers={"Prefer": "count=exact"}
    )

    try:
        return int(r.headers.get("Content-Range", "*/0").split("/")[-1])
    except Exception:
        return 0


def delete_document(doc_id, storage_path):
    """Delete one file: its record and the stored file."""

    r = api(
        "DELETE",
        "/rest/v1/documents",
        params={"id": f"eq.{doc_id}"},
        headers={"Prefer": "return=representation"}
    )

    if r.status_code in (401, 403):
        return False, "You are not allowed to delete files."

    if r.status_code != 200:
        return False, f"Could not delete the file ({err(r)})"

    if not r.json():
        return False, "Nothing was deleted (not allowed, or file not found)."

    delete_storage([storage_path])

    return True, "The file was deleted."


def upload_file(project_id, file_name, data, content_type):
    """Store a file in the private bucket. Returns (path, error)."""

    ext = re.sub(
        r"[^a-z0-9]", "",
        os.path.splitext(file_name)[1].lower()
    )

    path = f"p{project_id}/{uuid.uuid4().hex}"

    if ext:
        path += f".{ext}"

    r = api(
        "POST",
        f"/storage/v1/object/{BUCKET}/{path}",
        data=data,
        headers={"Content-Type": content_type or "application/octet-stream"},
        timeout=300
    )

    if r.status_code in (200, 201):
        return path, None

    return None, f"Upload failed ({err(r)})"


def signed_url(path, file_name):
    """Temporary (1 hour) download link for a stored file."""

    r = api(
        "POST",
        f"/storage/v1/object/sign/{BUCKET}/{path}",
        json_body={"expiresIn": 3600}
    )

    if r.status_code != 200:
        return None

    body = r.json()

    signed = body.get("signedURL") or body.get("signedUrl")

    if not signed:
        return None

    if signed.startswith("http"):
        url = signed

    elif signed.startswith("/storage/v1"):
        url = f"{SUPABASE_URL}{signed}"

    else:

        if not signed.startswith("/"):
            signed = "/" + signed

        url = f"{SUPABASE_URL}/storage/v1{signed}"

    joiner = "&" if "?" in url else "?"

    return f"{url}{joiner}download={quote(file_name)}"


def download_bytes(path):

    r = api(
        "GET",
        f"/storage/v1/object/authenticated/{BUCKET}/{path}",
        timeout=300
    )

    return r.content if r.status_code == 200 else None


# =========================================================
# AI
# =========================================================

AI_MODEL = "claude-sonnet-5-5"

AI_URL = "https://api.anthropic.com/v1/messages"

AI_MAX_CHARS = 60000

AI_DEFAULT_INSTRUCTION = (
    "Summarize this file for a document controller: what it is, "
    "the key data (numbers, dates, revisions, quantities), "
    "and anything missing or unclear."
)

AI_SYSTEM_PROMPT = (
    "You are an assistant for a document controller at an engineering "
    "company. Be accurate and concise. Never invent numbers or data that "
    "are not in the file; say clearly when something is missing or unclear. "
    "Reply in the same language as the user's instruction."
)

IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp"
}


def save_ai_result(project_number, file_name, request_text, result):

    api(
        "POST",
        "/rest/v1/ai_results",
        json_body={
            "project_number": project_number,
            "file_name": file_name,
            "request": request_text,
            "result": result
        },
        headers={"Prefer": "return=minimal"}
    )


def get_ai_results(project_number):
    """List of (file_name, request, result, created_at)."""

    r = api(
        "GET",
        "/rest/v1/ai_results",
        params={
            "select": "file_name,request,result,created_at",
            "project_number": f"eq.{project_number}",
            "order": "id.desc"
        }
    )

    if r.status_code != 200:
        return []

    return [
        (
            x["file_name"],
            x["request"] or "",
            x["result"] or "",
            fmt_time(x["created_at"])
        )
        for x in r.json()
    ]


def excel_to_text(data, max_rows=300, max_cols=40):

    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)

    parts = []

    for ws in wb.worksheets:

        parts.append(f"=== Sheet: {ws.title} ===")

        for i, row in enumerate(ws.iter_rows(values_only=True)):

            if i >= max_rows:
                parts.append(f"... (cut after {max_rows} rows)")
                break

            cells = [
                "" if c is None else str(c)
                for c in row[:max_cols]
            ]

            if any(cells):
                parts.append(" | ".join(cells))

    wb.close()

    return "\n".join(parts)[:AI_MAX_CHARS]


def docx_to_text(data):

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")

    xml = xml.replace("</w:p>", "\n")

    text = re.sub(r"<[^>]+>", "", xml)

    return html.unescape(text)[:AI_MAX_CHARS]


def build_file_block(name, data):
    """Turn a file into a message block for the AI. Returns (block, error)."""

    ext = os.path.splitext(name)[1].lower()

    try:

        if ext == ".pdf" or ext in IMAGE_TYPES:

            if len(data) > 20 * 1024 * 1024:
                return None, f"{name} is larger than 20 MB, too big for AI."

            encoded = base64.b64encode(data).decode()

            if ext == ".pdf":

                return {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": encoded
                    }
                }, None

            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": IMAGE_TYPES[ext],
                    "data": encoded
                }
            }, None

        if ext in (".xlsx", ".xlsm"):
            text = excel_to_text(data)

        elif ext == ".docx":
            text = docx_to_text(data)

        elif ext in (".csv", ".txt", ".md", ".json"):
            text = data.decode("utf-8", errors="replace")[:AI_MAX_CHARS]

        else:

            return None, (
                f"File type {ext or '(none)'} is not supported by AI yet. "
                "Supported: Excel (.xlsx), PDF, Word (.docx), "
                "images, CSV/TXT."
            )

    except Exception as e:
        return None, f"Could not read {name}: {e}"

    return {
        "type": "text",
        "text": f"File name: {name}\n\n{text}"
    }, None


GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)

GEMINI_DEFAULT_MODEL = "gemini-3.8-flash"


PROVIDER_LABELS = {
    "gemini": "Google Gemini",
    "openrouter": "OpenRouter (free models)",
    "anthropic": "Anthropic Claude"
}

PROVIDER_KEYS = {
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY"
}


def ai_providers():
    """Providers that have a key in Secrets, in the order they are tried.
    AI_PROVIDER (optional) puts one of them first."""

    order = ["gemini", "openrouter", "anthropic"]

    preferred = secret("AI_PROVIDER").lower()

    if preferred in order:

        order.remove(preferred)

        order.insert(0, preferred)

    return [p for p in order if secret(PROVIDER_KEYS[p])]


def ai_provider_label():

    names = [PROVIDER_LABELS[p] for p in ai_providers()]

    return ", ".join(names) if names else "not set"


def ai_provider_choices():
    """Choices for the 'Which AI?' list: label -> code."""

    available = ai_providers()

    choices = {}

    if len(available) > 1:
        choices["Auto (try each AI until one works)"] = "auto"

    for p in available:
        choices[PROVIDER_LABELS[p]] = p

    if len(available) > 1:
        choices["Compare all (see every answer)"] = "compare"

    return choices


def ask_gemini(block, instruction):
    """Send one file block to Google Gemini. Returns (ok, text_or_error)."""

    key = secret("GEMINI_API_KEY")

    if not key:
        return False, "No GEMINI_API_KEY found in the app Secrets."

    model = secret("GEMINI_MODEL") or GEMINI_DEFAULT_MODEL

    if block["type"] == "text":

        part = {"text": block["text"]}

    else:

        source = block["source"]

        if len(source["data"]) > 19_000_000:

            return False, (
                "This file is too large for the free AI "
                "(about 14 MB at most)."
            )

        part = {
            "inline_data": {
                "mime_type": source["media_type"],
                "data": source["data"]
            }
        }

    payload = {
        "system_instruction": {"parts": [{"text": AI_SYSTEM_PROMPT}]},
        "contents": [
            {
                "role": "user",
                "parts": [part, {"text": instruction}]
            }
        ],
        "generationConfig": {"maxOutputTokens": 8192}
    }

    try:

        response = requests.post(
            GEMINI_URL.format(model=model),
            headers={
                "x-goog-api-key": key,
                "content-type": "application/json"
            },
            json=payload,
            timeout=180
        )

    except Exception as e:

        return False, f"Could not connect to the AI service: {e}"

    if response.status_code == 429:

        return False, (
            "The free AI limit was reached (too many requests). "
            "Wait a few minutes and try again."
        )

    if response.status_code != 200:

        return False, (
            f"AI service error {response.status_code}: "
            f"{response.text[:300]}"
        )

    data = response.json()

    texts = []

    for candidate in data.get("candidates", []):

        for p in candidate.get("content", {}).get("parts", []):

            if "text" in p:
                texts.append(p["text"])

        if texts:
            break

    text = "\n".join(texts).strip()

    if not text:

        reason = data.get("promptFeedback", {}).get("blockReason", "")

        return False, "The AI returned no answer" + (
            f" ({reason})." if reason else "."
        )

    return True, text


def ask_claude(block, instruction):
    """Send one file block to Anthropic Claude. Returns (ok, text_or_error)."""

    key = secret("ANTHROPIC_API_KEY")

    if not key:
        return False, "No ANTHROPIC_API_KEY found in the app Secrets."

    payload = {
        "model": AI_MODEL,
        "max_tokens": 2000,
        "system": AI_SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": [block, {"type": "text", "text": instruction}]
            }
        ]
    }

    try:

        response = requests.post(
            AI_URL,
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json=payload,
            timeout=180
        )

    except Exception as e:

        return False, f"Could not connect to the AI service: {e}"

    if response.status_code != 200:

        return False, (
            f"AI service error {response.status_code}: "
            f"{response.text[:300]}"
        )

    text = "\n".join(
        b.get("text", "")
        for b in response.json().get("content", [])
        if b.get("type") == "text"
    ).strip()

    return True, text or "(AI returned an empty answer.)"


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


def openrouter_model():
    """The OpenRouter model to use: OPENROUTER_MODEL from Secrets, or else
    the free text model with the biggest context window."""

    chosen = secret("OPENROUTER_MODEL")

    if chosen:
        return chosen

    cached = st.session_state.get("openrouter_auto_model")

    if cached:
        return cached

    try:

        models = requests.get(OPENROUTER_MODELS_URL, timeout=30).json()

        models = models.get("data", [])

    except Exception:

        return ""

    free = []

    for m in models:

        pricing = m.get("pricing", {}) or {}

        outputs = (m.get("architecture", {}) or {}).get(
            "output_modalities"
        ) or ["text"]

        if (
            str(pricing.get("prompt")) in ("0", "0.0")
            and str(pricing.get("completion")) in ("0", "0.0")
            and "text" in outputs
        ):
            free.append(m)

    free.sort(key=lambda m: m.get("context_length") or 0, reverse=True)

    if not free:
        return ""

    st.session_state.openrouter_auto_model = free[0]["id"]

    return free[0]["id"]


def _openrouter_text(prompt, system):
    """Plain text request to OpenRouter. Returns (text, error)."""

    key = secret("OPENROUTER_API_KEY")

    if not key:
        return None, "No OPENROUTER_API_KEY found in the app Secrets."

    model = openrouter_model()

    if not model:

        return None, (
            "No free OpenRouter model found. "
            "Set OPENROUTER_MODEL in the app Secrets."
        )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 4000
    }

    try:

        response = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {key}",
                "content-type": "application/json"
            },
            json=payload,
            timeout=180
        )

    except Exception as e:

        return None, f"Could not connect to the AI service: {e}"

    if response.status_code == 429:

        return None, (
            "The OpenRouter free limit was reached. "
            "Wait a few minutes and try again."
        )

    if response.status_code != 200:

        return None, (
            f"OpenRouter error {response.status_code}: "
            f"{response.text[:300]}"
        )

    try:

        text = response.json()["choices"][0]["message"]["content"] or ""

    except Exception:

        text = ""

    text = text.strip()

    return (text, None) if text else (None, "OpenRouter returned no answer.")


def ask_openrouter(block, instruction):
    """Send a text file to OpenRouter. Returns (ok, text_or_error)."""

    if block["type"] != "text":

        return False, (
            "OpenRouter free models here read text files only "
            "(Excel, Word, CSV, TXT), not PDF or images."
        )

    text, error = _openrouter_text(
        f"{block['text']}\n\n---\n{instruction}",
        AI_SYSTEM_PROMPT
    )

    return (False, error) if error else (True, text)


def provider_display(provider):
    """Name shown next to an answer, with the model used."""

    if provider == "gemini":
        return f"Google Gemini ({secret('GEMINI_MODEL') or GEMINI_DEFAULT_MODEL})"

    if provider == "openrouter":
        return f"OpenRouter ({openrouter_model() or 'free model'})"

    return f"Anthropic Claude ({AI_MODEL})"


def _ask(provider, block, instruction):

    if provider == "gemini":
        return ask_gemini(block, instruction)

    if provider == "openrouter":
        return ask_openrouter(block, instruction)

    return ask_claude(block, instruction)


def with_footer(text, used):

    return f"{text}\n\n*Answered by {used}*"


def run_ai(name, data, instruction, provider="auto"):
    """Ask the AI about a file. provider: auto | gemini | openrouter |
    anthropic. In auto mode each AI is tried until one works.
    Returns (ok, text_or_error, name_of_the_AI_that_answered)."""

    available = ai_providers()

    if not available:

        return False, (
            "No AI key found. Add GEMINI_API_KEY (free), "
            "OPENROUTER_API_KEY (free models) or ANTHROPIC_API_KEY "
            "in the app Secrets."
        ), ""

    block, error = build_file_block(name, data)

    if error:
        return False, error, ""

    if provider == "auto":

        order = available

    elif provider in available:

        order = [provider]

    else:

        label = PROVIDER_LABELS.get(provider, provider)

        return False, f"{label} has no key in the app Secrets.", ""

    problems = []

    for p in order:

        ok, text = _ask(p, block, instruction)

        if ok:
            return True, text, provider_display(p)

        problems.append(f"{PROVIDER_LABELS[p]}: {text}")

    return False, " | ".join(problems), ""


def compare_ai(name, data, instruction):
    """Ask every available AI the same question.
    Returns (list of (name, ok, text), error)."""

    available = ai_providers()

    if not available:

        return [], "No AI key found in the app Secrets."

    block, error = build_file_block(name, data)

    if error:
        return [], error

    results = []

    for p in available:

        ok, text = _ask(p, block, instruction)

        results.append((provider_display(p), ok, text))

    return results, None


def analyze_file(name, data, instruction):
    """Send one file to the AI (auto mode). Returns (ok, text_or_error)."""

    ok, text, used = run_ai(name, data, instruction)

    return (True, with_footer(text, used)) if ok else (False, text)


# =========================================================
# EXCEL EDITING (the AI plans, the program does the editing)
# =========================================================

EDIT_SYSTEM_PROMPT = """You help a document controller edit an Excel workbook.
You do NOT edit the file yourself: you return a JSON plan and the program applies it.
You only see a summary of the workbook (sheet names, columns, first rows).
Return ONLY valid JSON, no markdown, in this shape:
{"summary": "<one or two sentences on what will be done, in the same language as the request>", "operations": [ ... ], "warnings": ["<anything unclear or risky>"]}

Rules:
- Use sheet names exactly as in the summary. A column can be a letter ("D") or the exact header text.
- Only use the operations listed below. If the request cannot be done with them, return an empty operations list and explain why in "warnings".
- Never invent data. If the request is ambiguous (which column? which sheet?), return no operations and say what you need in "warnings".
- Row numbers are real sheet rows as shown in the summary.
- If a sheet contains formulas, deleting rows or columns, removing duplicates or sorting would break them. In that case put {"op":"formulas_to_values","sheet":"S"} first and mention it in "warnings".

Operations:
{"op":"delete_columns","sheet":"S","columns":["D","Notes"]}
{"op":"delete_rows","sheet":"S","rows":[5,6]}
{"op":"delete_rows_where","sheet":"S","column":"C","condition":"equals|contains|empty|not_empty","value":"text"}
{"op":"keep_rows_where","sheet":"S","column":"C","condition":"equals|contains|empty|not_empty","value":"text"}
{"op":"rename_header","sheet":"S","column":"B","new_name":"Contract No."}
{"op":"find_replace","sheet":"S","column":"B","find":"old","replace":"new"}   (column can be null for the whole sheet)
{"op":"extract_to_new_sheet","sheet":"S","columns":["A","C"],"new_sheet":"Contracts","unique":true}
{"op":"add_column","sheet":"S","header":"Total","value":"text"}   or   {"op":"add_column","sheet":"S","header":"Total","formula":"=B{row}*C{row}"}
{"op":"trim_whitespace","sheet":"S"}
{"op":"remove_duplicates","sheet":"S","column":"A"}   (column can be null to compare whole rows)
{"op":"sort","sheet":"S","column":"A","descending":false}
{"op":"formulas_to_values","sheet":"S"}   (replaces formulas by their current values)
"""


def _short(value, limit=40):

    if value is None:
        return ""

    return str(value).replace("\n", " ").strip()[:limit]


def detect_header_row(ws, scan=30):
    """First row (within the first rows) that has at least 2 filled cells."""

    first = None

    for r in range(1, min(ws.max_row, scan) + 1):

        filled = [
            ws.cell(row=r, column=c).value
            for c in range(1, ws.max_column + 1)
            if ws.cell(row=r, column=c).value not in (None, "")
        ]

        if len(filled) >= 2:
            return r

        if filled and first is None:
            first = r

    return first or 1


def workbook_summary(data, sample_rows=8, max_cols=40):
    """Short text description of a workbook for the AI (not the whole data)."""

    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    wb = load_workbook(io.BytesIO(data), data_only=True)

    wb_formulas = load_workbook(io.BytesIO(data))

    lines = []

    for ws in wb.worksheets:

        hr = detect_header_row(ws)

        ncols = min(ws.max_column, max_cols)

        formulas = _count_formulas(wb_formulas[ws.title])

        lines.append(
            f'Sheet "{ws.title}": {ws.max_row} rows, '
            f"{ws.max_column} columns, header row = {hr}, "
            f"formulas: {formulas} cell(s)"
        )

        heads = [
            f"{get_column_letter(c)}={_short(ws.cell(row=hr, column=c).value)}"
            for c in range(1, ncols + 1)
        ]

        lines.append("Columns: " + "; ".join(heads))

        lines.append("Sample rows:")

        shown = 0

        for r in range(hr + 1, ws.max_row + 1):

            row = [
                _short(ws.cell(row=r, column=c).value)
                for c in range(1, ncols + 1)
            ]

            if any(row):

                lines.append(f"  row {r}: " + " | ".join(row))

                shown += 1

                if shown >= sample_rows:
                    break

        lines.append("")

    wb.close()

    return "\n".join(lines)[:AI_MAX_CHARS]


def _find_sheet(wb, name):

    if name:

        if name in wb.sheetnames:
            return wb[name]

        for s in wb.sheetnames:
            if s.strip().lower() == str(name).strip().lower():
                return wb[s]

        raise ValueError(f"sheet '{name}' not found")

    if len(wb.sheetnames) == 1:
        return wb[wb.sheetnames[0]]

    raise ValueError("the sheet name is missing")


def _find_col(ws, spec, header_row):
    """Column index from a header text or a letter like D."""

    from openpyxl.utils import column_index_from_string

    text = str(spec).strip()

    for c in range(1, ws.max_column + 1):

        value = ws.cell(row=header_row, column=c).value

        if value is not None and str(value).strip().lower() == text.lower():
            return c

    if re.fullmatch(r"[A-Za-z]{1,3}", text):

        index = column_index_from_string(text.upper())

        if index <= ws.max_column:
            return index

        raise ValueError(f"column {text} is outside the sheet")

    raise ValueError(f"column '{spec}' not found")


def _count_formulas(ws):

    return sum(
        1
        for row in ws.iter_rows()
        for cell in row
        if isinstance(cell.value, str) and cell.value.startswith("=")
    )


def _refuse_if_formulas(ws):

    if _count_formulas(ws):

        raise ValueError(
            "the sheet has formulas and this change would break them. "
            "Convert the formulas to values first (formulas_to_values)"
        )


def _row_has_data(ws, r):

    return any(
        ws.cell(row=r, column=c).value not in (None, "")
        for c in range(1, ws.max_column + 1)
    )


def _matches(value, condition, wanted):

    text = "" if value is None else str(value).strip()

    if condition == "empty":
        return text == ""

    if condition == "not_empty":
        return text != ""

    if condition == "equals":
        return text.lower() == str(wanted).strip().lower()

    if condition == "contains":
        return str(wanted).strip().lower() in text.lower()

    raise ValueError(f"unknown condition '{condition}'")


def describe_op(op):
    """One plain sentence for the preview shown before applying."""

    kind = op.get("op", "?")

    sheet = op.get("sheet")

    where = f" in sheet '{sheet}'" if sheet else ""

    cols = ", ".join(str(x) for x in op.get("columns", []) or [])

    if kind == "delete_columns":
        return f"Delete column(s) {cols}{where}"

    if kind == "delete_rows":
        rows = ", ".join(str(x) for x in op.get("rows", []) or [])
        return f"Delete row(s) {rows}{where}"

    if kind in ("delete_rows_where", "keep_rows_where"):

        verb = "Delete" if kind == "delete_rows_where" else "Keep only"

        return (
            f"{verb} rows where column {op.get('column')} "
            f"{op.get('condition')} {op.get('value', '')}{where}"
        ).replace("  ", " ")

    if kind == "rename_header":
        return (
            f"Rename the header of column {op.get('column')} to "
            f"'{op.get('new_name')}'{where}"
        )

    if kind == "find_replace":

        target = (
            f"column {op.get('column')}" if op.get("column") else "all cells"
        )

        return (
            f"Replace '{op.get('find')}' with '{op.get('replace')}' "
            f"in {target}{where}"
        )

    if kind == "extract_to_new_sheet":

        unique = " (unique values only)" if op.get("unique") else ""

        return (
            f"Copy column(s) {cols}{where} to a new sheet "
            f"'{op.get('new_sheet')}'{unique}"
        )

    if kind == "add_column":

        content = op.get("formula") or op.get("value") or ""

        return (
            f"Add a column '{op.get('header')}' with {content}{where}"
        )

    if kind == "trim_whitespace":
        return f"Remove extra spaces from text cells{where}"

    if kind == "formulas_to_values":
        return f"Replace formulas by their current values{where}"

    if kind == "remove_duplicates":

        target = (
            f"column {op.get('column')}" if op.get("column") else "whole rows"
        )

        return f"Remove duplicate rows by {target}{where}"

    if kind == "sort":

        order = "descending" if op.get("descending") else "ascending"

        return f"Sort by column {op.get('column')} ({order}){where}"

    return f"Unknown operation '{kind}'"


def apply_one_op(wb, wb_values, changed_sheets, op):
    """Apply one operation to the workbook. Returns a short result text."""

    kind = op.get("op")

    ws = _find_sheet(wb, op.get("sheet"))

    hr = int(op.get("header_row") or detect_header_row(ws))

    last = ws.max_row

    if kind == "delete_columns":

        indexes = sorted(
            {_find_col(ws, spec, hr) for spec in op.get("columns", [])},
            reverse=True
        )

        if not indexes:
            raise ValueError("no columns given")

        _refuse_if_formulas(ws)

        for index in indexes:
            ws.delete_cols(index)

        changed_sheets.add(ws.title)

        return f"Deleted {len(indexes)} column(s) in '{ws.title}'"

    if kind == "delete_rows":

        rows = sorted(
            {int(r) for r in op.get("rows", []) if 1 <= int(r) <= last},
            reverse=True
        )

        if not rows:
            raise ValueError("no valid rows given")

        _refuse_if_formulas(ws)

        for r in rows:
            ws.delete_rows(r)

        changed_sheets.add(ws.title)

        return f"Deleted {len(rows)} row(s) in '{ws.title}'"

    if kind in ("delete_rows_where", "keep_rows_where"):

        col = _find_col(ws, op.get("column"), hr)

        condition = op.get("condition")

        wanted = op.get("value", "")

        hits = [
            r for r in range(hr + 1, last + 1)
            if _matches(ws.cell(row=r, column=col).value, condition, wanted)
        ]

        if hits or kind == "keep_rows_where":
            _refuse_if_formulas(ws)

        if kind == "keep_rows_where":

            hits = [
                r for r in range(hr + 1, last + 1)
                if r not in set(hits) and _row_has_data(ws, r)
            ]

        for r in sorted(hits, reverse=True):
            ws.delete_rows(r)

        changed_sheets.add(ws.title)

        return f"Deleted {len(hits)} row(s) in '{ws.title}'"

    if kind == "rename_header":

        col = _find_col(ws, op.get("column"), hr)

        ws.cell(row=hr, column=col).value = op.get("new_name")

        return f"Renamed a header in '{ws.title}'"

    if kind == "find_replace":

        find = str(op.get("find", ""))

        if not find:
            raise ValueError("nothing to find")

        replace = str(op.get("replace", ""))

        if op.get("column"):
            columns = [_find_col(ws, op.get("column"), hr)]
            first_row = hr + 1
        else:
            columns = range(1, ws.max_column + 1)
            first_row = 1

        count = 0

        for c in columns:

            for r in range(first_row, last + 1):

                cell = ws.cell(row=r, column=c)

                value = cell.value

                if (
                    isinstance(value, str)
                    and not value.startswith("=")
                    and find in value
                ):

                    cell.value = value.replace(find, replace)

                    count += 1

        return f"Replaced text in {count} cell(s) in '{ws.title}'"

    if kind == "extract_to_new_sheet":

        cols = [_find_col(ws, spec, hr) for spec in op.get("columns", [])]

        if not cols:
            raise ValueError("no columns given")

        name = re.sub(r"[\[\]:*?/\\]", "", str(op.get("new_sheet") or "Extract"))

        name = name.strip()[:28] or "Extract"

        final = name
        n = 2

        while final in wb.sheetnames:
            final = f"{name}_{n}"
            n += 1

        new = wb.create_sheet(final)

        source_values = (
            wb_values[ws.title]
            if ws.title not in changed_sheets and ws.title in wb_values.sheetnames
            else None
        )

        def read(r, c):

            value = ws.cell(row=r, column=c).value

            if isinstance(value, str) and value.startswith("="):

                return (
                    source_values.cell(row=r, column=c).value
                    if source_values is not None else None
                )

            return value

        new.append([ws.cell(row=hr, column=c).value for c in cols])

        seen = set()

        written = 0

        for r in range(hr + 1, last + 1):

            row = [read(r, c) for c in cols]

            if all(v in (None, "") for v in row):
                continue

            if op.get("unique"):

                key = tuple(str(v).strip().lower() for v in row)

                if key in seen:
                    continue

                seen.add(key)

            new.append(row)

            written += 1

        return f"Created sheet '{final}' with {written} row(s)"

    if kind == "add_column":

        header = op.get("header") or "New column"

        formula = op.get("formula")

        if formula and not str(formula).startswith("="):
            raise ValueError("a formula must start with =")

        new_col = ws.max_column + 1

        ws.cell(row=hr, column=new_col).value = header

        count = 0

        for r in range(hr + 1, last + 1):

            if not _row_has_data(ws, r):
                continue

            if formula:
                ws.cell(row=r, column=new_col).value = str(formula).replace(
                    "{row}", str(r)
                )
            else:
                ws.cell(row=r, column=new_col).value = op.get("value", "")

            count += 1

        changed_sheets.add(ws.title)

        return f"Added column '{header}' ({count} row(s)) in '{ws.title}'"

    if kind == "trim_whitespace":

        count = 0

        for row in ws.iter_rows(min_row=1, max_row=last):

            for cell in row:

                value = cell.value

                if isinstance(value, str) and not value.startswith("="):

                    cleaned = " ".join(value.split())

                    if cleaned != value:
                        cell.value = cleaned
                        count += 1

        return f"Cleaned spaces in {count} cell(s) in '{ws.title}'"

    if kind == "remove_duplicates":

        col = _find_col(ws, op.get("column"), hr) if op.get("column") else None

        seen = set()

        duplicates = []

        for r in range(hr + 1, last + 1):

            if not _row_has_data(ws, r):
                continue

            if col:
                key = str(ws.cell(row=r, column=col).value).strip().lower()
            else:
                key = tuple(
                    str(ws.cell(row=r, column=c).value)
                    for c in range(1, ws.max_column + 1)
                )

            if key in seen:
                duplicates.append(r)
            else:
                seen.add(key)

        if duplicates:
            _refuse_if_formulas(ws)

        for r in sorted(duplicates, reverse=True):
            ws.delete_rows(r)

        changed_sheets.add(ws.title)

        return f"Removed {len(duplicates)} duplicate row(s) in '{ws.title}'"

    if kind == "sort":

        col = _find_col(ws, op.get("column"), hr)

        rows = []

        for r in range(hr + 1, last + 1):

            values = [
                ws.cell(row=r, column=c).value
                for c in range(1, ws.max_column + 1)
            ]

            if any(isinstance(v, str) and v.startswith("=") for v in values):

                raise ValueError(
                    "the sheet has formulas, sorting would break them"
                )

            if any(v not in (None, "") for v in values):
                rows.append(values)

        def sort_key(values):

            v = values[col - 1]

            if v in (None, ""):
                return (2, 0, "")

            if isinstance(v, (int, float)):
                return (0, v, "")

            return (1, 0, str(v).strip().lower())

        rows.sort(key=sort_key, reverse=bool(op.get("descending")))

        for i, values in enumerate(rows):

            for c, v in enumerate(values, start=1):
                ws.cell(row=hr + 1 + i, column=c).value = v

        changed_sheets.add(ws.title)

        return f"Sorted {len(rows)} row(s) in '{ws.title}' (values only)"

    if kind == "formulas_to_values":

        if ws.title in changed_sheets:

            raise ValueError(
                "do this before other changes to the same sheet"
            )

        source = wb_values[ws.title]

        count = 0

        for row in ws.iter_rows():

            for cell in row:

                if isinstance(cell.value, str) and cell.value.startswith("="):

                    stored = source.cell(
                        row=cell.row, column=cell.column
                    ).value

                    if stored is None:

                        raise ValueError(
                            "the file has no stored results for its "
                            "formulas. Open it in Excel, save it, and "
                            "upload it again"
                        )

                    cell.value = stored

                    count += 1

        return f"Replaced {count} formula(s) by values in '{ws.title}'"

    raise ValueError(f"unknown operation '{kind}'")


def apply_edit_plan(data, operations):
    """Apply a list of operations to a COPY of the workbook.
    Returns (new_file_bytes, log) where log is a list of (ok, text)."""

    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data))

    wb_values = load_workbook(io.BytesIO(data), data_only=True)

    changed_sheets = set()

    log = []

    for n, op in enumerate(operations[:40], start=1):

        try:

            text = apply_one_op(wb, wb_values, changed_sheets, op)

            log.append((True, f"{n}. {text}"))

        except Exception as e:

            log.append((False, f"{n}. {describe_op(op)} -> skipped: {e}"))

    out = io.BytesIO()

    wb.save(out)

    return out.getvalue(), log


def edited_name(file_name):

    base, _ = os.path.splitext(file_name)

    return f"{base} (edited).xlsx"


def parse_plan(text):
    """Read the AI's JSON plan. Returns (plan, error)."""

    cleaned = text.strip()

    cleaned = re.sub(r"^```(?:json)?|```$", "", cleaned).strip()

    try:

        plan = json.loads(cleaned)

    except Exception:

        start = cleaned.find("{")
        end = cleaned.rfind("}")

        if start == -1 or end == -1:
            return None, "The AI did not return a valid plan. Try again."

        try:
            plan = json.loads(cleaned[start:end + 1])
        except Exception:
            return None, "The AI did not return a valid plan. Try again."

    if not isinstance(plan, dict):
        return None, "The AI did not return a valid plan. Try again."

    operations = plan.get("operations", [])

    if not isinstance(operations, list):
        return None, "The AI plan has no operations list."

    plan["operations"] = [o for o in operations if isinstance(o, dict)][:40]

    warnings = plan.get("warnings", [])

    plan["warnings"] = [str(w) for w in warnings] if isinstance(warnings, list) else []

    plan["summary"] = str(plan.get("summary", ""))

    return plan, None


def _gemini_text(prompt, system, json_mode=False):
    """Plain text request to Gemini. Returns (text, error)."""

    key = secret("GEMINI_API_KEY")

    if not key:
        return None, "No GEMINI_API_KEY found in the app Secrets."

    model = secret("GEMINI_MODEL") or GEMINI_DEFAULT_MODEL

    config = {"maxOutputTokens": 8192}

    if json_mode:
        config["responseMimeType"] = "application/json"

    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": config
    }

    try:

        response = requests.post(
            GEMINI_URL.format(model=model),
            headers={
                "x-goog-api-key": key,
                "content-type": "application/json"
            },
            json=payload,
            timeout=180
        )

    except Exception as e:

        return None, f"Could not connect to the AI service: {e}"

    if response.status_code == 429:

        return None, (
            "The free AI limit was reached (too many requests). "
            "Wait a few minutes and try again."
        )

    if response.status_code != 200:

        return None, (
            f"AI service error {response.status_code}: "
            f"{response.text[:300]}"
        )

    texts = []

    for candidate in response.json().get("candidates", []):

        for p in candidate.get("content", {}).get("parts", []):

            if "text" in p:
                texts.append(p["text"])

        if texts:
            break

    text = "\n".join(texts).strip()

    return (text, None) if text else (None, "The AI returned no answer.")


def _claude_text(prompt, system):
    """Plain text request to Claude. Returns (text, error)."""

    key = secret("ANTHROPIC_API_KEY")

    if not key:
        return None, "No ANTHROPIC_API_KEY found in the app Secrets."

    payload = {
        "model": AI_MODEL,
        "max_tokens": 4000,
        "system": system,
        "messages": [{"role": "user", "content": prompt}]
    }

    try:

        response = requests.post(
            AI_URL,
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json=payload,
            timeout=180
        )

    except Exception as e:

        return None, f"Could not connect to the AI service: {e}"

    if response.status_code != 200:

        return None, (
            f"AI service error {response.status_code}: "
            f"{response.text[:300]}"
        )

    text = "\n".join(
        b.get("text", "")
        for b in response.json().get("content", [])
        if b.get("type") == "text"
    ).strip()

    return (text, None) if text else (None, "The AI returned no answer.")


def ask_ai_plan(summary_text, instruction):
    """Ask the AI for an edit plan, trying each AI until one gives a valid
    plan. Returns (plan, error)."""

    available = ai_providers()

    if not available:

        return None, (
            "No AI key found. Add GEMINI_API_KEY (free), "
            "OPENROUTER_API_KEY (free models) or ANTHROPIC_API_KEY "
            "in the app Secrets."
        )

    prompt = (
        f"WORKBOOK SUMMARY:\n{summary_text}\n\n"
        f"USER REQUEST:\n{instruction}"
    )

    problems = []

    for p in available:

        if p == "gemini":
            text, error = _gemini_text(prompt, EDIT_SYSTEM_PROMPT, json_mode=True)

        elif p == "openrouter":
            text, error = _openrouter_text(prompt, EDIT_SYSTEM_PROMPT)

        else:
            text, error = _claude_text(prompt, EDIT_SYSTEM_PROMPT)

        if error:

            problems.append(f"{PROVIDER_LABELS[p]}: {error}")

            continue

        plan, error = parse_plan(text)

        if error:

            problems.append(f"{PROVIDER_LABELS[p]}: {error}")

            continue

        plan["provider"] = provider_display(p)

        return plan, None

    return None, " | ".join(problems)


# ===== UI =====

# =========================================================
# LOGO
# =========================================================

logo = None

logo_path = os.path.join(BASE_DIR, "logo.png")

if os.path.exists(logo_path):

    with open(logo_path, "rb") as image_file:
        logo = base64.b64encode(image_file.read()).decode()

if logo:

    st.markdown(
        f"""
        <div style="width:100%;text-align:center;
                    margin-top:-35px;margin-bottom:15px;">
            <img src="data:image/png;base64,{logo}"
                 style="width:200px;height:auto;">
        </div>
        """,
        unsafe_allow_html=True
    )


# =========================================================
# LOGIN
# =========================================================

if not get_token():

    st.title("📁 Document Control Portal")

    st.write("Please sign in to continue.")

    with st.form("login_form"):

        email = st.text_input("Email")

        password = st.text_input("Password", type="password")

        submitted = st.form_submit_button(
            "Sign in",
            use_container_width=True
        )

    if submitted:

        if not email or not password:

            st.error("Please enter your email and password.")

        else:

            ok, message = login(email.strip(), password)

            if ok:
                st.rerun()
            else:
                st.error(message)

    st.stop()


auth = st.session_state["auth"]

is_admin = auth["role"] == "admin"


# =========================================================
# DOCUMENTS TABLE WITH DELETE (red X on every row)
# =========================================================

@st.dialog("Delete file")
def confirm_delete_dialog(doc_id, file_name, project, storage_path):

    st.write(
        f"Are you sure you want to delete **{md_safe(file_name)}** "
        f"(project {md_safe(project)})?"
    )

    st.caption("This cannot be undone.")

    yes_col, no_col = st.columns(2)

    if yes_col.button(
        "Yes, delete",
        type="primary",
        use_container_width=True
    ):

        ok, message = delete_document(doc_id, storage_path)

        st.session_state.doc_msg = (ok, message)

        st.rerun()

    if no_col.button("Cancel", use_container_width=True):

        st.rerun()


DOCS_CSS = """
<style>
.st-key-docs_table p { font-size: 14px; margin-bottom: 0; }

.st-key-docs_table [class*="st-key-del_doc_"] button {
    min-height: 26px;
    height: 26px;
    width: 26px;
    min-width: 26px;
    padding: 0;
    border: none;
    background: transparent;
    box-shadow: none;
    border-radius: 6px;
    color: #c62828;
}

.st-key-docs_table [class*="st-key-del_doc_"] button p {
    font-size: 15px;
    line-height: 1;
}

.st-key-docs_table [class*="st-key-del_doc_"] button:hover {
    background: rgba(198, 40, 40, 0.12);
    color: #b71c1c;
}
</style>
"""


def show_documents_table(documents, can_delete):

    st.markdown(DOCS_CSS, unsafe_allow_html=True)

    widths = [1, 5, 1, 1.3, 2, 2]

    labels = [
        "Project", "File Name", "Type", "Size", "Notes", "Uploaded At"
    ]

    if can_delete:

        widths.append(0.6)

        labels.append("")

    head_style = (
        "font-size:12px;font-weight:600;text-transform:uppercase;"
        "letter-spacing:0.05em;opacity:0.6"
    )

    line = "<hr style='margin:2px 0;opacity:0.15'>"

    with st.container(key="docs_table"):

        header = st.columns(widths, vertical_alignment="center")

        for col, label in zip(header, labels):

            if label:

                col.markdown(
                    f"<span style='{head_style}'>{label}</span>",
                    unsafe_allow_html=True
                )

        st.markdown(
            "<hr style='margin:4px 0;opacity:0.4'>",
            unsafe_allow_html=True
        )

        for d in documents:

            cols = st.columns(widths, vertical_alignment="center")

            cols[0].markdown(md_safe(d[1]))
            cols[1].markdown(md_safe(d[2]))
            cols[2].markdown(md_safe(d[3]))
            cols[3].markdown(md_safe(format_size(d[4])))
            cols[4].markdown(md_safe(d[5]))
            cols[5].markdown(md_safe(d[6]))

            if can_delete:

                if cols[6].button(
                    "✕",
                    key=f"del_doc_{d[0]}",
                    help="Delete this file"
                ):

                    confirm_delete_dialog(d[0], d[2], d[1], d[7])

            st.markdown(line, unsafe_allow_html=True)


# =========================================================
# EXCEL EDITOR (AI page, admin only)
# =========================================================

XLSX_MIME = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


def show_excel_editor(project_number, docs):

    xlsx_docs = [d for d in docs if str(d[2]).lower().endswith(".xlsx")]

    if not xlsx_docs:

        st.info(
            "No .xlsx files in this project yet. "
            "Only .xlsx files can be edited."
        )

        return

    options = {f"{d[2]} | {d[6]}": d for d in xlsx_docs}

    choice = st.selectbox(
        "Excel file",
        list(options.keys()),
        key="edit_file"
    )

    doc = options[choice]

    st.warning(
        "The original file is never changed: you get an edited COPY. "
        "Charts, images, pivot tables and macros inside the file "
        "are not kept in the copy."
    )

    instruction = st.text_area(
        "What should be changed?",
        placeholder=(
            "Example: delete column D, and copy all contract numbers "
            "to a new sheet (unique values)"
        ),
        height=100,
        key="edit_instruction"
    )

    if st.button("🧠 Plan the edit", key="edit_plan_button"):

        if not instruction.strip():

            st.error("Please write what you want to change.")

        else:

            error = None

            with st.spinner("The AI is reading the file and planning..."):

                data = download_bytes(doc[7])

                if data is None:

                    error = "Could not read the file."

                else:

                    try:
                        summary = workbook_summary(data)
                    except Exception as e:
                        summary = None
                        error = f"Could not read the Excel file: {e}"

                if error is None:
                    plan, error = ask_ai_plan(summary, instruction.strip())

            if error:

                st.error(error)

            else:

                st.session_state.edit_plan = {"doc_id": doc[0], "plan": plan}

                st.session_state.pop("edit_result", None)

    state = st.session_state.get("edit_plan")

    if state and state["doc_id"] == doc[0]:

        plan = state["plan"]

        st.subheader("The plan")

        if plan["summary"]:
            st.write(plan["summary"])

        if plan.get("provider"):
            st.caption(f"Plan made by {plan['provider']}")

        for warning in plan["warnings"]:
            st.caption(f"⚠️ {warning}")

        operations = plan["operations"]

        if not operations:

            st.info("The AI found nothing it can change with this request.")

        else:

            for i, op in enumerate(operations, start=1):
                st.markdown(f"{i}. {md_safe(describe_op(op))}")

            if st.button(
                "✅ Apply and create the file",
                key="edit_apply_button",
                use_container_width=True
            ):

                with st.spinner("Editing the file..."):

                    data = download_bytes(doc[7])

                    if data is None:

                        st.error("Could not read the file.")

                    else:

                        new_bytes, log = apply_edit_plan(data, operations)

                        st.session_state.edit_result = {
                            "doc_id": doc[0],
                            "bytes": new_bytes,
                            "name": edited_name(doc[2]),
                            "log": log
                        }

    result = st.session_state.get("edit_result")

    if result and result["doc_id"] == doc[0]:

        st.subheader("Result")

        for ok, text in result["log"]:

            st.write(("✅ " if ok else "⚠️ ") + text)

        st.download_button(
            "⬇️ Download the edited file",
            data=result["bytes"],
            file_name=result["name"],
            mime=XLSX_MIME,
            use_container_width=True
        )

        if st.button(
            "💾 Also save the edited file in this project",
            key="edit_save_button",
            use_container_width=True
        ):

            project_id = get_project_id(project_number)

            if project_id is None:

                st.error("Project not found.")

            else:

                path, error = upload_file(
                    project_id,
                    result["name"],
                    result["bytes"],
                    XLSX_MIME
                )

                if error:

                    st.error(error)

                else:

                    ok, message = add_document(
                        project_number,
                        result["name"],
                        path,
                        "XLSX",
                        len(result["bytes"]),
                        f"AI-edited copy of {doc[2]}"
                    )

                    if ok:
                        st.success("Saved. It now appears in Documents.")
                    else:
                        delete_storage([path])
                        st.error(message)


# =========================================================
# SIDEBAR
# =========================================================

st.sidebar.title("📁 ECE")

st.sidebar.caption("Document Control Portal")

st.sidebar.divider()

menu = [
    "🏠 Dashboard",
    "📁 Projects",
    "📄 Documents"
]

if is_admin:
    menu.append("📤 Upload")

menu += ["📊 Reports", "🪄 AI"]

page = st.sidebar.radio("Menu", menu)

st.sidebar.divider()

st.sidebar.caption(
    f"👤 {auth['email']}  \n"
    f"Role: **{'Admin' if is_admin else 'Viewer (read only)'}**"
)

if st.sidebar.button("Sign out", use_container_width=True):

    st.session_state.pop("auth", None)

    st.rerun()


# =========================================================
# DASHBOARD
# =========================================================

if page == "🏠 Dashboard":

    st.title("📊 Document Control Portal")

    st.write("Welcome to your Document Management System")

    projects = get_projects(recent=True)

    col1, col2, col3, col4 = st.columns(4)

    col1.metric("Projects", len(projects))

    col2.metric("Documents", count_documents())

    pending_count = len([p for p in projects if p[3] == "Active"])

    approved_count = len([p for p in projects if p[3] == "Closed"])

    col3.metric("Pending", pending_count, help="Active projects")

    col4.metric("Approved", approved_count, help="Closed projects")

    st.divider()

    st.subheader("Recent Projects")

    if projects:

        for project in projects[:5]:

            st.write(
                f"📁 **{project[0]}** — "
                f"{project[1]} — "
                f"{project[3]}"
            )

    else:

        st.info("No projects have been added yet.")


# =========================================================
# PROJECTS
# =========================================================

elif page == "📁 Projects":

    st.title("📁 Projects")

    st.write("Manage and track all project information.")

    for key_name in ("import_msg", "delete_msg", "status_msg"):

        if key_name in st.session_state:
            st.success(st.session_state.pop(key_name))

    st.divider()

    search = st.text_input(
        "🔍 Search Project",
        placeholder="Enter project number or project name..."
    )

    projects = get_projects()

    if search:

        projects = [
            p for p in projects
            if search.lower() in p[0].lower()
            or search.lower() in p[1].lower()
        ]

    if projects:

        for project in projects:

            with st.container(border=True):

                if is_admin:

                    col1, col2, col3, col4, col5 = st.columns(
                        [1.3, 3, 2, 1.4, 1.6]
                    )

                else:

                    col1, col2, col3, col4 = st.columns([1.5, 3, 2, 1.5])

                    col5 = None

                col1.write(f"**{project[0]}**")

                col2.write(f"**{project[1]}**")

                col3.write(project[2])

                col4.write(
                    "🟢 Active" if project[3] == "Active" else "🔴 Closed"
                )

                if col5 is not None:

                    with col5.popover("⚙️ Status"):

                        st.write(f"Project **{project[0]}**: what is it now?")

                        new_status = st.radio(
                            "Set the project as:",
                            ["Active", "Closed"],
                            index=0 if project[3] == "Active" else 1,
                            format_func=lambda x: (
                                "🟢 Active (still running)"
                                if x == "Active"
                                else "🔴 Closed (finished)"
                            ),
                            key=f"status_radio_{project[0]}"
                        )

                        if st.button(
                            "💾 Save",
                            key=f"status_save_{project[0]}",
                            use_container_width=True
                        ):

                            ok, message = update_project_status(
                                project[0],
                                new_status
                            )

                            if ok:

                                st.session_state.status_msg = message

                                st.rerun()

                            else:

                                st.error(message)

    else:

        st.info("No projects found.")

    # -----------------------------------------------------
    # ADMIN ONLY: add / import / delete
    # -----------------------------------------------------

    if is_admin:

        st.divider()

        st.subheader("➕ Add New Project")

        with st.form("add_project_form"):

            project_number = st.text_input(
                "Project Number",
                placeholder="Example: 0000"
            )

            project_name = st.text_input(
                "Project Name",
                placeholder="Example Project"
            )

            client = st.text_input("Client")

            status = st.selectbox("Status", ["Active", "Closed"])

            submitted = st.form_submit_button(
                "💾 Save Project",
                use_container_width=True
            )

            if submitted:

                if not project_number:

                    st.error("Please enter Project Number.")

                elif not project_name:

                    st.error("Please enter Project Name.")

                else:

                    success, message = add_project(
                        project_number.strip(),
                        project_name.strip(),
                        client.strip(),
                        status
                    )

                    if success:

                        st.session_state.import_msg = message

                        st.rerun()

                    else:

                        st.error(message)

        st.divider()

        with st.expander("📥 Import Projects from Excel"):

            st.write(
                "The first row must have these columns: "
                "**Client Name**, **Project Name**, **Project Number**. "
                "Project numbers that already exist are skipped."
            )

            import_file = st.file_uploader(
                "Choose Excel file (.xlsx)",
                type=["xlsx"],
                key="import_projects_file"
            )

            import_status = st.selectbox(
                "Status for imported projects",
                ["Active", "Closed"],
                key="import_projects_status"
            )

            if import_file is not None:

                import_rows, import_error = read_projects_excel(import_file)

                if import_error:

                    st.error(import_error)

                else:

                    st.info(
                        f"{len(import_rows)} projects found in the file."
                    )

                    if st.button(
                        "📥 Import",
                        key="import_projects_button",
                        use_container_width=True
                    ):

                        added, skipped, problem = bulk_add_projects(
                            import_rows,
                            import_status
                        )

                        if problem:

                            st.error(problem)

                        else:

                            st.session_state.import_msg = (
                                f"Added {added} project(s). "
                                f"Skipped {skipped} (number already exists)."
                            )

                            st.rerun()

        with st.expander("🗑️ Delete Project"):

            all_projects = get_projects()

            if not all_projects:

                st.info("No projects to delete.")

            else:

                if "delete_counter" not in st.session_state:
                    st.session_state.delete_counter = 0

                counter = st.session_state.delete_counter

                del_options = {
                    f"{p[0]} — {p[1]}": p for p in all_projects
                }

                del_choice = st.selectbox(
                    "Select the project to delete",
                    list(del_options.keys()),
                    key=f"delete_select_{counter}"
                )

                del_project = del_options[del_choice]

                del_number = del_project[0]

                file_count = len(get_documents(del_number))

                warning = (
                    f"You are about to delete project {del_number} — "
                    f"{del_project[1]}. This cannot be undone."
                )

                if file_count:
                    warning += (
                        f" Its {file_count} uploaded file(s) and "
                        "AI results will be deleted too."
                    )

                st.warning(warning)

                confirm_delete = st.checkbox(
                    "Yes, I want to delete this project",
                    key=f"delete_confirm_{counter}_{del_number}"
                )

                if st.button(
                    "🗑️ Delete Project",
                    key=f"delete_button_{counter}",
                    disabled=not confirm_delete,
                    use_container_width=True
                ):

                    ok, message = delete_project(del_number)

                    st.session_state.delete_counter += 1

                    st.session_state.delete_msg = message

                    st.rerun()


# =========================================================
# DOCUMENTS
# =========================================================

elif page == "📄 Documents":

    st.title("📄 Documents")

    if "doc_msg" in st.session_state:

        doc_ok, doc_text = st.session_state.pop("doc_msg")

        if doc_ok:
            st.success(doc_text)
        else:
            st.error(doc_text)

    projects = get_projects()

    if not projects:

        st.info("No projects yet.")

    else:

        options = {"All Projects": None}

        for p in projects:
            options[f"{p[0]} — {p[1]}"] = p[0]

        col1, col2 = st.columns([2, 2])

        choice = col1.selectbox("Project", list(options.keys()))

        search_file = col2.text_input(
            "🔍 Search File Name",
            placeholder="Type part of the file name..."
        )

        documents = get_documents(options[choice])

        if search_file:

            documents = [
                d for d in documents
                if search_file.lower() in d[2].lower()
            ]

        if documents:

            show_documents_table(documents, is_admin)

            st.divider()

            st.subheader("⬇️ Download a File")

            file_options = {
                f"{d[1]} | {d[2]} | {d[6]}": d for d in documents
            }

            selected = st.selectbox(
                "Select file",
                list(file_options.keys())
            )

            doc = file_options[selected]

            url = signed_url(doc[7], doc[2])

            if url:

                st.link_button("⬇️ Download", url)

                st.caption("The download link works for one hour.")

            else:

                st.error("Could not create a download link for this file.")

        else:

            st.info("No documents found.")


# =========================================================
# UPLOAD (admin only)
# =========================================================

elif page == "📤 Upload" and is_admin:

    st.title("📤 Upload Documents")

    st.write(
        "Choose the project, then pick any files "
        "(Excel, CAD, PDF, ...). They are stored online under the project."
    )

    projects = get_projects()

    if not projects:

        st.warning("No projects yet. Add a project first.")

    else:

        if "uploader_key" not in st.session_state:
            st.session_state.uploader_key = 0

        key = st.session_state.uploader_key

        options = {f"{p[0]} — {p[1]}": p for p in projects}

        choice = st.selectbox("📁 Select Project", list(options.keys()))

        project = options[choice]

        project_number = project[0]

        files = st.file_uploader(
            "Choose files",
            accept_multiple_files=True,
            key=f"uploader_{key}"
        )

        notes = st.text_input("Notes (optional)", key=f"notes_{key}")

        send_ai = st.checkbox(
            "🪄 Send to AI after saving",
            value=False,
            key=f"send_ai_{key}"
        )

        ai_instruction = AI_DEFAULT_INSTRUCTION

        if send_ai:

            ai_instruction = st.text_area(
                "What should AI do with the file?",
                value=AI_DEFAULT_INSTRUCTION,
                height=100,
                key=f"ai_instr_{key}"
            )

        if st.button("💾 Save to Project", use_container_width=True):

            if not files:

                st.error("Please choose at least one file.")

            else:

                project_id = get_project_id(project_number)

                if project_id is None:

                    st.error("Project not found.")

                else:

                    ai_outputs = []

                    problems = []

                    saved = 0

                    for f in files:

                        data = f.getvalue()

                        with st.spinner(f"Uploading {f.name} ..."):

                            path, error = upload_file(
                                project_id,
                                f.name,
                                data,
                                f.type
                            )

                        if error:

                            problems.append(f"{f.name}: {error}")

                            continue

                        ext = os.path.splitext(f.name)[1] \
                            .replace(".", "").upper()

                        ok, message = add_document(
                            project_number,
                            f.name,
                            path,
                            ext or "FILE",
                            len(data),
                            notes.strip()
                        )

                        if not ok:

                            delete_storage([path])

                            problems.append(f"{f.name}: {message}")

                            continue

                        saved += 1

                        if send_ai:

                            with st.spinner(
                                f"🪄 AI is analyzing {f.name} ..."
                            ):

                                ai_ok, result = analyze_file(
                                    f.name,
                                    data,
                                    ai_instruction
                                )

                            if ai_ok:

                                save_ai_result(
                                    project_number,
                                    f.name,
                                    ai_instruction,
                                    result
                                )

                            ai_outputs.append((f.name, ai_ok, result))

                    st.session_state.ai_outputs = ai_outputs

                    st.session_state.upload_problems = problems

                    st.session_state.upload_msg = (
                        f"{saved} file(s) saved to project "
                        f"{project_number}."
                    )

                    st.session_state.uploader_key += 1

                    st.rerun()

        if "upload_msg" in st.session_state:

            st.success(st.session_state.pop("upload_msg"))

        for problem in st.session_state.pop("upload_problems", []):
            st.error(problem)

        for name, ok, text in st.session_state.pop("ai_outputs", []):

            if ok:

                with st.expander(f"🪄 AI result — {name}", expanded=True):
                    st.markdown(text)

            else:

                st.error(f"🪄 AI could not analyze {name}: {text}")

        st.divider()

        st.subheader("Files in this project")

        documents = get_documents(project_number)

        if documents:

            show_table(
                [
                    {
                        "File Name": d[2],
                        "Type": d[3],
                        "Size": format_size(d[4]),
                        "Notes": d[5],
                        "Uploaded At": d[6]
                    }
                    for d in documents
                ]
            )

        else:

            st.info("No files uploaded to this project yet.")


# =========================================================
# REPORTS
# =========================================================

elif page == "📊 Reports":

    st.title("📊 Reports")

    projects = get_projects()

    col1, col2, col3, col4 = st.columns(4)

    col1.metric("Total Projects", len(projects))

    col2.metric(
        "Active Projects",
        len([p for p in projects if p[3] == "Active"])
    )

    col3.metric(
        "Closed Projects",
        len([p for p in projects if p[3] == "Closed"])
    )

    col4.metric("Total Documents", count_documents())


# =========================================================
# AI PAGE
# =========================================================

elif page == "🪄 AI":

    st.title("🪄 AI")

    st.caption(f"AI ready: {ai_provider_label()}")

    if is_admin:

        st.write(
            "Analyze project files with AI. "
            "Every result is saved and visible to all users."
        )

    else:

        st.write("AI results saved for each project (read only).")

    projects = get_projects()

    if not projects:

        st.info("No projects yet.")

    else:

        ai_options = {f"{p[0]} — {p[1]}": p[0] for p in projects}

        ai_choice = st.selectbox(
            "Project",
            list(ai_options.keys()),
            key="ai_report_project"
        )

        ai_project = ai_options[ai_choice]

        if is_admin:

            ai_mode = st.radio(
                "What do you want?",
                [
                    "📝 Analyze a file (text answer)",
                    "✏️ Edit an Excel file (get a new file)"
                ],
                horizontal=True,
                key="ai_mode"
            )

            ai_docs = get_documents(ai_project)

            if ai_mode.startswith("✏️"):

                show_excel_editor(ai_project, ai_docs)

            elif ai_docs:

                ai_file_options = {
                    f"{d[2]} | {d[6]}": d for d in ai_docs
                }

                ai_file = st.selectbox(
                    "File",
                    list(ai_file_options.keys()),
                    key="ai_report_file"
                )

                ai_prompt = st.text_area(
                    "What should AI do with the file?",
                    value=AI_DEFAULT_INSTRUCTION,
                    height=100,
                    key="ai_report_prompt"
                )

                provider_choices = ai_provider_choices()

                if not provider_choices:

                    st.warning(
                        "No AI key found. Add GEMINI_API_KEY (free), "
                        "OPENROUTER_API_KEY (free models) or "
                        "ANTHROPIC_API_KEY in the app Secrets."
                    )

                else:

                    which_label = st.selectbox(
                        "Which AI?",
                        list(provider_choices.keys()),
                        key="ai_report_provider"
                    )

                    which = provider_choices[which_label]

                    if st.button("🪄 Run AI", key="ai_report_run"):

                        chosen = ai_file_options[ai_file]

                        with st.spinner("🪄 AI is analyzing..."):

                            file_data = download_bytes(chosen[7])

                            if file_data is None:

                                st.error("Could not read the file.")

                            elif which == "compare":

                                answers, problem = compare_ai(
                                    chosen[2],
                                    file_data,
                                    ai_prompt
                                )

                                if problem:

                                    st.error(problem)

                                else:

                                    saved = 0

                                    for label, ok, text in answers:

                                        with st.expander(label, expanded=True):

                                            if ok:
                                                st.markdown(text)
                                            else:
                                                st.error(text)

                                        if ok:

                                            save_ai_result(
                                                ai_project,
                                                chosen[2],
                                                ai_prompt,
                                                with_footer(text, label)
                                            )

                                            saved += 1

                                    st.success(
                                        f"{saved} answer(s) saved below."
                                    )

                            else:

                                ok, text, used = run_ai(
                                    chosen[2],
                                    file_data,
                                    ai_prompt,
                                    which
                                )

                                if ok:

                                    save_ai_result(
                                        ai_project,
                                        chosen[2],
                                        ai_prompt,
                                        with_footer(text, used)
                                    )

                                    st.success(
                                        f"Done ({used}). "
                                        "The result is saved below."
                                    )

                                else:

                                    st.error(text)

            else:

                st.info("No files in this project yet. Upload files first.")

        st.subheader("Previous AI results")

        ai_results = get_ai_results(ai_project)

        if ai_results:

            for i, r in enumerate(ai_results):

                with st.expander(f"{r[0]} — {r[3]}", expanded=(i == 0)):

                    st.caption(f"Request: {r[1]}")

                    st.markdown(r[2])

        else:

            st.info("No AI results for this project yet.")
