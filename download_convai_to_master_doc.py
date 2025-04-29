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
SCOPES         = ["https://www.googleapis.com/auth/documents",
                  "https://www.googleapis.com/auth/drive.file"]
TZ_OFFSET_HRS  = int(os.getenv("TZ_OFFSET_HOURS") or "4")

# ----------------- OAuth Google API -----------------
def get_creds():
    creds = None
    if os.path.exists("token.pickle"):
        creds = pickle.load(open("token.pickle","rb"))
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS, SCOPES)
            creds = flow.run_local_server(port=0, access_type="offline")
        pickle.dump(creds, open("token.pickle","wb"))
    return creds

creds         = get_creds()
docs_service  = build("docs","v1",credentials=creds)
drive_service = build("drive","v3",credentials=creds)

# ----------------- ConvAI API -----------------
session = requests.Session()
session.trust_env = False
session.headers.update({"xi-api-key": API_KEY, "Accept": "application/json"})

def fetch_all_calls():
    url, params, out = "https://api.elevenlabs.io/v1/convai/conversations", {"page_size":PAGE_SIZE}, []
    while True:
        r = session.get(url, params=params); r.raise_for_status()
        j = r.json()
        out.extend(j.get("conversations",[]))
        if not j.get("has_more",False): break
        params["cursor"] = j["next_cursor"]
    return out

def fetch_detail(cid):
    r = session.get(f"https://api.elevenlabs.io/v1/convai/conversations/{cid}")
    r.raise_for_status()
    return r.json()

# ----------------- State -----------------
def load_last(): 
    return int(open(LAST_RUN_FILE).read()) if os.path.exists(LAST_RUN_FILE) else 0

def save_last(ts):
    open(LAST_RUN_FILE,"w").write(str(int(ts)))

def load_doc():
    e = os.getenv("MASTER_DOC_ID")
    if e: return e
    return open(DOC_ID_FILE).read().strip() if os.path.exists(DOC_ID_FILE) else None

def save_doc(d):
    if os.getenv("MASTER_DOC_ID"): return
    open(DOC_ID_FILE,"w").write(d)

def mk_doc():
    f = drive_service.files().create(
        body={"name":"ConvAI_Master_Log","mimeType":"application/vnd.google-apps.document"},
        fields="id"
    ).execute()
    return f["id"]

# ----------------- Formatter -----------------
def format_call(d, fallback_ts):
    st = d.get("metadata",{}).get("start_time_unix_secs",fallback_ts)
    dt = datetime.utcfromtimestamp(st)+timedelta(hours=TZ_OFFSET_HRS)
    ts = dt.strftime("%Y-%m-%d %H:%M:%S")
    summ = d.get("analysis",{}).get("transcript_summary","").strip()
    lines, prev = [], None
    for m in d.get("transcript",[]):
        role = (m.get("role") or "").upper()
        txt  = (m.get("message") or "").strip()
        if not txt: continue
        sec  = m.get("time_in_call_secs",0.0)
        pre  = f"[{sec:06.2f}s] {role}: "
        if role==prev:
            lines[-1]+= "\n"+pre+txt
        else:
            if prev and prev!=role: lines.append("")
            lines.append(pre+txt)
        prev=role
    header = f"=== Call at {ts} ===\n"
    if summ: header += f"Summary:\n{summ}\n"
    return header+"\n"+"\n".join(lines)+"\n\n"+"―"*40+"\n\n"

# ----------------- Main -----------------
def main():
    doc_id = load_doc() or mk_doc()
    save_doc(doc_id)

    calls   = fetch_all_calls()
    sel     = [c for c in calls if c.get("start_time_unix_secs",0)>=SINCE and c.get("call_duration_secs",0)>MIN_DURATION]
    last_ts = load_last()
    new     = [c for c in sel if c["start_time_unix_secs"]>last_ts]
    if not new:
        print("No new calls."); return

    new.sort(key=lambda x:x["start_time_unix_secs"],reverse=True)
    txt,max_ts = "", last_ts
    for c in new:
        d = fetch_detail(c["conversation_id"])
        txt += format_call(d, c["start_time_unix_secs"])
        st = d.get("metadata",{}).get("start_time_unix_secs",0)
        if st>max_ts: max_ts=st

    # ВСЕГДА append в конец через endOfSegmentLocation
    req = [{"insertText":{"endOfSegmentLocation":{},"text":txt}}]
    try:
        docs_service.documents().batchUpdate(documentId=doc_id, body={"requests":req}).execute()
        print(f"Appended {len(new)} calls.")
    except HttpError as e:
        print("Fatal batchUpdate error:", e)
        return

    save_last(max_ts)

if __name__=="__main__":
    main()
