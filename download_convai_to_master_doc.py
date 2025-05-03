#!/usr/bin/env python3

import os
import time
import datetime
import requests
import pickle
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ----------------- НАСТРОЙКИ -----------------
API_KEY      = "sk_91b455debc341646af393b6582573e06c70458ce8c0e51d4"
PAGE_SIZE    = 100
MIN_DURATION = 60      # секунды
SINCE        = int(datetime.datetime(2025, 4, 1, 0, 0).timestamp())
LAST_RUN_FILE = "last_run.txt"
CREDENTIALS  = "credentials.json"
SCOPES       = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]
TZ_OFFSET_HOURS = 4
AGENT_NAME_FILTER = "LeiaAGI"

# ----------------- Google OAuth -----------------
def get_credentials():
    creds = None
    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS, SCOPES
            )
            creds = flow.run_local_server(
                port=0,
                access_type="offline",
                include_granted_scopes=True,
            )
        with open("token.pickle", "wb") as f:
            pickle.dump(creds, f)
    return creds

creds = get_credentials()
docs_service = build("docs", "v1", credentials=creds)

# ----------------- ConvAI API -----------------
session = requests.Session()
session.headers.update({
    "xi-api-key": API_KEY,
    "Accept":     "application/json"
})

def fetch_all_calls():
    url = "https://api.elevenlabs.io/v1/convai/conversations"
    params = {"page_size": PAGE_SIZE}
    all_calls = []
    while True:
        r = session.get(url, params=params)
        r.raise_for_status()
        data = r.json()
        calls = data.get("conversations", [])
        for call in calls:
            if call.get("agent_name", "") == AGENT_NAME_FILTER:
                all_calls.append(call)
        if not data.get("has_more", False):
            break
        params["cursor"] = data.get("next_cursor")
    return all_calls

def fetch_call_detail(conversation_id):
    r = session.get(f"https://api.elevenlabs.io/v1/convai/conversations/{conversation_id}")
    r.raise_for_status()
    return r.json()

# ----------------- Вспомогательные функции -----------------
def load_last_run():
    return int(open(LAST_RUN_FILE).read().strip()) if os.path.exists(LAST_RUN_FILE) else 0

def save_last_run(timestamp):
    with open(LAST_RUN_FILE, "w") as f:
        f.write(str(int(timestamp)))

# ----------------- Форматирование звонка -----------------
def format_call(detail, fallback_ts):
    st = detail.get("metadata", {}).get("start_time_unix_secs", fallback_ts)
    adjusted_ts = st + (TZ_OFFSET_HOURS * 3600)
    ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(adjusted_ts))
    summary = detail.get("analysis", {}).get("transcript_summary", "").strip()
    transcript = detail.get("transcript", [])
    lines = []
    prev_role = None
    for msg in transcript:
        role = (msg.get("role") or "").upper()
        text = (msg.get("message") or "").strip()
        if not text:
            continue
        tsec = msg.get("time_in_call_secs", 0.0)
        line = f"[{tsec:06.2f}s] {role}: {text}"
        if prev_role and prev_role != role:
            lines.append("")
        if prev_role == role:
            lines[-1] += "\n" + line
        else:
            lines.append(line)
        prev_role = role
    header = f"=== Call at {ts_str} ===\n"
    if summary:
        header += f"Summary:\n{summary}\n"
    return header + "\n" + "\n".join(lines) + "\n\n" + "―" * 40 + "\n\n"

# ----------------- Основной процесс -----------------
def main():
    doc_id = os.environ.get("MASTER_DOC_ID")
    if not doc_id:
        raise RuntimeError("MASTER_DOC_ID environment variable is not set")

    calls = fetch_all_calls()
    print(f"Всего звонков от агента {AGENT_NAME_FILTER}: {len(calls)}")

    relevant_calls = [
        c for c in calls
        if c.get("start_time_unix_secs", 0) >= SINCE
        and c.get("call_duration_secs", 0) > MIN_DURATION
    ]
    print(f"После фильтра по дате и длительности: {len(relevant_calls)}")

    last_ts = load_last_run()
    new_calls = [c for c in relevant_calls if c.get("start_time_unix_secs", 0) > last_ts]
    print(f"Новых звонков: {len(new_calls)}")
    if not new_calls:
        print("Нет новых звонков для добавления.")
        return

    new_calls.sort(key=lambda x: x["start_time_unix_secs"], reverse=True)
    full_text = ""
    max_ts = last_ts
    for call in new_calls:
        cid = call["conversation_id"]
        fallback = call.get("start_time_unix_secs", 0)
        detail = fetch_call_detail(cid)
        block = format_call(detail, fallback)
        full_text += block
        call_ts = detail.get("metadata", {}).get("start_time_unix_secs", fallback)
        if call_ts > max_ts:
            max_ts = call_ts

    requests_body = [{
        "insertText": {
            "location": {"index": 1},
            "text": full_text
        }
    }]

    docs_service.documents().batchUpdate(documentId=doc_id, body={"requests": requests_body}).execute()

    save_last_run(max_ts)
    print(f"Добавлено {len(new_calls)} звонков в Google Doc (ID={doc_id}).")

if __name__ == "__main__":
    main()
