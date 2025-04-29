#!/usr/bin/env python3
# download_convai_to_master_doc.py

import os, pickle, requests
from datetime import datetime, timedelta
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ----------------- КОНФИГ -----------------
API_KEY        = "sk_91b455debc341646af393b6582573e06c70458ce8c0e51d4"
PAGE_SIZE      = 100
MIN_DURATION   = 60
SINCE          = int(datetime(2025, 4, 1).timestamp())
DOC_ID_FILE    = "doc_id.txt"
LAST_RUN_FILE  = "last_run.txt"
CREDENTIALS    = "credentials.json"
SCOPES         = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]
TZ_OFFSET_HRS  = int(os.getenv("TZ_OFFSET_HOURS") or "4")

# ----------------- OAuth Google Docs -----------------
def get_creds():
    creds = None
    if os.path.exists("token.pickle"):
        creds = pickle.load(open("token.pickle", "rb"))
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS, SCOPES)
            creds = flow.run_local_server(port=0, access_type="offline")
        pickle.dump(creds, open("token.pickle", "wb"))
    return creds

creds         = get_creds()
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
    url, params, out = "https://api.elevenlabs.io/v1/convai/conversations", {"page_size": PAGE_SIZE}, []
    while True:
        r = session.get(url, params=params); r.raise_for_status()
        j = r.json()
        out.extend(j.get("conversations", []))
        if not j.get("has_more", False):
            break
        params["cursor"] = j["next_cursor"]
    return out

def fetch_detail(cid):
    r = session.get(f"https://api.elevenlabs.io/v1/convai/conversations/{cid}")
    r.raise_for_status()
    return r.json()

# ----------------- State -----------------
def load_last():
    return int(open(LAST_RUN_FILE).read().strip()) if os.path.exists(LAST_RUN_FILE) else 0

def save_last(ts):
    open(LAST_RUN_FILE, "w").write(str(int(ts)))

def load_doc_id():
    env = os.getenv("MASTER_DOC_ID")
    if env:
        return env
    return open(DOC_ID_FILE).read().strip() if os.path.exists(DOC_ID_FILE) else None

def save_doc_id(did):
    if os.getenv("MASTER_DOC_ID"):
        return
    open(DOC_ID_FILE, "w").write(did)

def create_doc():
    meta = {"name":"ConvAI_Master_Log","mimeType":"application/vnd.google-apps.document"}
    return drive_service.files().create(body=meta, fields="id").execute()["id"]

# ----------------- Formatter -----------------
def format_call(d, fallback_ts):
    st = d.get("metadata",{}).get("start_time_unix_secs", fallback_ts)
    dt = datetime.utcfromtimestamp(st) + timedelta(hours=TZ_OFFSET_HRS)
    ts = dt.strftime("%Y-%m-%d %H:%M:%S")
    summ = d.get("analysis",{}).get("transcript_summary","").strip()
    transcript = d.get("transcript",[])
    lines, prev = [], None
    for m in transcript:
        role = (m.get("role") or "").upper()
        txt  = (m.get("message") or "").strip()
        if not txt:
            continue
        sec = m.get("time_in_call_secs",0.0)
        prefix = f"[{sec:06.2f}s] {role}: "
        if role == prev:
            lines[-1] += "\n" + prefix + txt
        else:
            if prev and prev != role:
                lines.append("")
            lines.append(prefix + txt)
        prev = role

    header = f"=== Call at {ts} ===\n"
    if summ:
        header += f"Summary:\n{summ}\n"
    body = "\n".join(lines)
    return header + "\n" + body + "\n\n" + "―" * 40 + "\n\n"

# ----------------- MAIN -----------------
def main():
    # 1) Document
    doc_id = load_doc_id() or create_doc()
    save_doc_id(doc_id)

    # 2) Fetch & filter
    calls = fetch_all_calls()
    sel   = [c for c in calls
             if c.get("start_time_unix_secs",0) >= SINCE
             and c.get("call_duration_secs",0) > MIN_DURATION]
    last_ts = load_last()
    new_calls = [c for c in sel if c["start_time_unix_secs"] > last_ts]
    if not new_calls:
        print("Нет новых звонков.")
        return

    new_calls.sort(key=lambda x: x["start_time_unix_secs"], reverse=True)
    full_text, max_ts = "", last_ts
    for c in new_calls:
        detail = fetch_detail(c["conversation_id"])
        full_text += format_call(detail, c["start_time_unix_secs"])
        st = detail.get("metadata",{}).get("start_time_unix_secs",0)
        if st > max_ts:
            max_ts = st

    # 3) Insert at top via startOfSegmentLocation
    try:
        req = [{
            "insertText": {
                "startOfSegmentLocation": {},
                "text": full_text
            }
        }]
        docs_service.documents().batchUpdate(documentId=doc_id, body={"requests":req}).execute()
        print(f"Добавлено {len(new_calls)} звонков наверх.")
    except HttpError as e:
        print("Не удалось вставить наверх, добавляем в конец:", e)
        req = [{
            "insertText": {
                "endOfSegmentLocation": {},
                "text": full_text
            }
        }]
        docs_service.documents().batchUpdate(documentId=doc_id, body={"requests":req}).execute()
        print(f"Добавлено {len(new_calls)} звонков в конец.")

    save_last(max_ts)

if __name__ == "__main__":
    main()
