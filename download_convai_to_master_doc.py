#!/usr/bin/env python3
# download_convai_to_master_doc.py

import os
import time
import requests
import pickle
from datetime import datetime, timedelta
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ----------------- НАСТРОЙКИ -----------------
API_KEY       = "sk_91b455debc341646af393b6582573e06c70458ce8c0e51d4"
PAGE_SIZE     = 100
MIN_DURATION  = 60      # секунды
SINCE         = int(datetime(2025, 4, 1, 0, 0).timestamp())
DOC_ID_FILE   = "doc_id.txt"
LAST_RUN_FILE = "last_run.txt"
CREDENTIALS   = "credentials.json"
SCOPES        = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS") or "4")

# ----------------- OAuth для Google API -----------------
def get_credentials():
    creds = None
    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS, SCOPES)
            creds = flow.run_local_server(port=0, access_type="offline", include_granted_scopes=True)
        with open("token.pickle", "wb") as f:
            pickle.dump(creds, f)
    return creds

creds = get_credentials()
docs_service  = build("docs", "v1", credentials=creds)
drive_service = build("drive", "v3", credentials=creds)

# ----------------- ConvAI API -----------------
session = requests.Session()
session.trust_env = False
session.headers.update({
    "xi-api-key": API_KEY,
    "Accept":     "application/json"
})

def fetch_all_calls():
    url, params, all_calls = "https://api.elevenlabs.io/v1/convai/conversations", {"page_size": PAGE_SIZE}, []
    while True:
        r = session.get(url, params=params); r.raise_for_status()
        data = r.json()
        all_calls.extend(data.get("conversations", []))
        if not data.get("has_more", False):
            break
        params["cursor"] = data["next_cursor"]
    return all_calls

def fetch_call_detail(cid):
    r = session.get(f"https://api.elevenlabs.io/v1/convai/conversations/{cid}")
    r.raise_for_status()
    return r.json()

# ----------------- Вспомогалки -----------------
def load_last_run():
    if not os.path.exists(LAST_RUN_FILE):
        return 0
    return int(open(LAST_RUN_FILE).read().strip())

def save_last_run(ts):
    with open(LAST_RUN_FILE, "w") as f:
        f.write(str(int(ts)))

def load_doc_id():
    env = os.getenv("MASTER_DOC_ID")
    if env:
        return env
    if os.path.exists(DOC_ID_FILE):
        return open(DOC_ID_FILE).read().strip()
    return None

def save_doc_id(doc_id):
    if os.getenv("MASTER_DOC_ID"):
        return
    with open(DOC_ID_FILE, "w") as f:
        f.write(doc_id)

def create_master_doc():
    meta = {"name": "ConvAI_Master_Log", "mimeType": "application/vnd.google-apps.document"}
    file = drive_service.files().create(body=meta, fields="id").execute()
    return file["id"]

# ----------------- Форматирование -----------------
def format_call(detail, fallback_ts):
    st = detail.get("metadata", {}).get("start_time_unix_secs", fallback_ts)
    dt = datetime.utcfromtimestamp(st) + timedelta(hours=TZ_OFFSET_HOURS)
    ts = dt.strftime("%Y-%m-%d %H:%M:%S")
    summ = detail.get("analysis", {}).get("transcript_summary", "").strip()
    transcript = detail.get("transcript", [])
    lines, prev = [], None
    for m in transcript:
        role = (m.get("role") or "").upper()
        message = (m.get("message") or "").strip()
        if not message:
            continue
        tsec = m.get("time_in_call_secs", 0.0)
        prefix = f"[{tsec:06.2f}s] {role}: "
        if role == prev:
            lines[-1] += "\n" + prefix + message
        else:
            if prev and prev != role:
                lines.append("")
            lines.append(prefix + message)
        prev = role

    header = f"=== Call at {ts} ===\n"
    if summ:
        header += f"Summary:\n{summ}\n"
    body = "\n".join(lines)
    return header + "\n" + body + "\n\n" + "―" * 40 + "\n\n"

# ----------------- Основной flow -----------------
def main():
    doc_id = load_doc_id() or create_master_doc()
    save_doc_id(doc_id)

    # 1) все звонки
    calls = fetch_all_calls()
    # 2) фильтр
    sel = [c for c in calls
           if c.get("start_time_unix_secs", 0) >= SINCE
           and c.get("call_duration_secs", 0) > MIN_DURATION]
    last_ts = load_last_run()
    new_calls = [c for c in sel if c["start_time_unix_secs"] > last_ts]
    if not new_calls:
        print("Нет новых звонков.")
        return

    new_calls.sort(key=lambda x: x["start_time_unix_secs"], reverse=True)
    full_text, max_ts = "", last_ts
    for c in new_calls:
        detail = fetch_call_detail(c["conversation_id"])
        full_text += format_call(detail, c["start_time_unix_secs"])
        st_call = detail.get("metadata", {}).get("start_time_unix_secs", 0)
        if st_call > max_ts:
            max_ts = st_call

    # 3) вставка с fallback
    try:
        doc = docs_service.documents().get(documentId=doc_id).execute()
        end_index = doc["body"]["content"][-1]["endIndex"] - 1  # последний допустимый индекс

        # пытаемся вставить наверх
        requests_body = [{
            "insertText": {"location": {"index": 1}, "text": full_text}
        }]
        docs_service.documents().batchUpdate(documentId=doc_id, body={"requests": requests_body}).execute()
        print(f"Добавлено {len(new_calls)} звонков наверх.")

    except HttpError as e:
        if "Precondition check failed" in str(e):
            # фоллбэк: вставить в конец
            requests_body = [{
                "insertText": {"location": {"index": end_index}, "text": full_text}
            }]
            docs_service.documents().batchUpdate(documentId=doc_id, body={"requests": requests_body}).execute()
            print(f"Добавлено {len(new_calls)} звонков в конец.")
        else:
            raise

    save_last_run(max_ts)

if __name__ == "__main__":
    main()
