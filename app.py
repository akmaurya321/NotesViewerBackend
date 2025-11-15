# app.py
import os
import base64
import json
import time
import uuid
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS


app = Flask(__name__)
CORS(app)



# ---------- CONFIG (set these as Render env vars) ----------
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")             # token with repo contents:read+write on frontend repo
GITHUB_OWNER = os.getenv("GITHUB_OWNER", "akmaurya321")  # your GitHub username
GITHUB_REPO = os.getenv("GITHUB_REPO", "NotesViewer")    # frontend repo (GitHub Pages)
BRANCH = os.getenv("BRANCH", "main")
USERS_FILE_PATH = os.getenv("USERS_FILE_PATH", "users.json")  # stored in same repo root
NOTES_FOLDER = os.getenv("NOTES_FOLDER", "notes")  # files will be uploaded to notes/<userId>/
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 90 * 1024 * 1024))  # 90 MB safe limit for GitHub Contents API
# ----------------------------------------------------------

if not (GITHUB_TOKEN and GITHUB_OWNER and GITHUB_REPO):
    print("WARNING: Please set GITHUB_TOKEN, GITHUB_OWNER and GITHUB_REPO env vars")

GITHUB_API = "https://api.github.com"
HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json"
}

# ---------- GitHub helpers ----------
def get_repo_file(path):
    url = f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    r = requests.get(url, headers=HEADERS, params={"ref": BRANCH})
    if r.status_code == 200:
        data = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return {"sha": data["sha"], "content": content, "raw": data}
    if r.status_code == 404:
        return None
    else:
        raise Exception(f"GitHub GET error {r.status_code}: {r.text}")

def put_repo_file(path, content_bytes, message, sha=None):
    url = f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    content_b64 = base64.b64encode(content_bytes).decode("utf-8")
    payload = {"message": message, "content": content_b64, "branch": BRANCH}
    if sha:
        payload["sha"] = sha
    r = requests.put(url, headers=HEADERS, json=payload)
    if r.status_code in (200, 201):
        return r.json()
    else:
        raise Exception(f"GitHub PUT error {r.status_code}: {r.text}")

def delete_repo_file(path, sha):
    url = f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    payload = {"message": f"delete {path}", "sha": sha, "branch": BRANCH}
    r = requests.delete(url, headers=HEADERS, json=payload)
    if r.status_code in (200, 204):
        return r.json()
    else:
        raise Exception(f"GitHub DELETE error {r.status_code}: {r.text}")

# ---------- Users registry ----------
def load_users_registry():
    rec = get_repo_file(USERS_FILE_PATH)
    if not rec:
        return {"sha": None, "data": {}}
    try:
        data = json.loads(rec["content"])
        return {"sha": rec["sha"], "data": data}
    except Exception:
        return {"sha": rec["sha"], "data": {}}

def save_users_registry(users_dict, sha=None):
    payload = json.dumps(users_dict, indent=2).encode("utf-8")
    res = put_repo_file(USERS_FILE_PATH, payload, f"update {USERS_FILE_PATH}", sha=sha)
    return res

def safe_filename(name):
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name)

# ---------- Endpoints ----------

@app.route("/")
def index():
    return jsonify({"ok": True, "msg": "GitHub upload backend alive"})

@app.route("/check/<userId>", methods=["GET"])
def check_user(userId):
    """
    Quick availability check. Returns {"available": true/false}
    """
    userId = userId.strip()
    registry = load_users_registry()
    exists = userId in registry["data"]
    return jsonify({"available": not exists})

@app.route("/register", methods=["POST"])
def register():
    """
    Body JSON: { "userId": "chosenName", "displayName": "optional" }
    On success: { success: True, userId, token }
    Error if userId exists -> 409
    """
    body = request.get_json(force=True, silent=True) or {}
    userId = str(body.get("userId") or "").strip()
    display = str(body.get("displayName") or "").strip()
    if not userId:
        return jsonify({"error": "userId required"}), 400
    userId = userId.replace(" ", "_")
    if len(userId) > 64:
        return jsonify({"error": "userId too long"}), 400

    registry = load_users_registry()
    users = registry["data"]
    if userId in users:
        return jsonify({"error": "userId already taken"}), 409

    token = uuid.uuid4().hex
    users[userId] = {
        "token": token,
        "display": display,
        "createdAt": int(time.time() * 1000)
    }

    try:
        save_users_registry(users, sha=registry["sha"])
    except Exception as e:
        return jsonify({"error": "Failed to write registry", "detail": str(e)}), 500

    return jsonify({"success": True, "userId": userId, "token": token})

@app.route("/upload", methods=["POST"])
def upload():
    """
    multipart form:
    - file
    - userId
    - token
    returns { success: True, url: publicUrl, path: repoPath }
    """
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    userId = str(request.form.get("userId") or "").strip()
    token = str(request.form.get("token") or "").strip()
    if not userId or not token:
        return jsonify({"error": "userId and token required"}), 400

    # verify user/token
    registry = load_users_registry()
    users = registry["data"]
    entry = users.get(userId)
    if not entry or entry.get("token") != token:
        return jsonify({"error": "Invalid userId or token"}), 403

    filename = safe_filename(f.filename or "file")
    timestamp = int(time.time() * 1000)
    safe_name = f"{timestamp}_{filename}"
    repo_path = f"{NOTES_FOLDER}/{userId}/{safe_name}"

    file_bytes = f.read()
    size = len(file_bytes)
    if size > MAX_FILE_SIZE:
        return jsonify({"error": f"File too large. Max allowed is {MAX_FILE_SIZE} bytes"}), 413

    try:
        put_repo_file(repo_path, file_bytes, message=f"upload {safe_name}")
    except Exception as e:
        return jsonify({"error": "GitHub upload failed", "detail": str(e)}), 500

    public_url = f"https://{GITHUB_OWNER}.github.io/{GITHUB_REPO}/{repo_path}"
    return jsonify({"success": True, "url": public_url, "path": repo_path})

@app.route("/list/<userId>", methods=["GET"])
def list_user(userId):
    userId = userId.strip()
    registry = load_users_registry()
    if userId not in registry["data"]:
        return jsonify({"error": "Unknown userId"}), 404
    url = f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{NOTES_FOLDER}/{userId}"
    r = requests.get(url, headers=HEADERS, params={"ref": BRANCH})
    if r.status_code == 200:
        items = r.json()
        files = []
        for it in items:
            if it.get("type") == "file":
                files.append({"name": it.get("name"), "path": it.get("path"), "download_url": it.get("download_url")})
        return jsonify({"success": True, "files": files})
    elif r.status_code == 404:
        return jsonify({"success": True, "files": []})
    else:
        return jsonify({"error": "GitHub list failed", "detail": r.text}), 500

@app.route("/delete", methods=["POST"])
def delete_file():
    """
    JSON: { filePath, userId, token }
    filePath must start with notes/<userId>/
    """
    data = request.get_json(force=True, silent=True) or {}
    filePath = data.get("filePath")
    userId = data.get("userId")
    token = data.get("token")
    if not (filePath and userId and token):
        return jsonify({"error": "filePath, userId and token required"}), 400

    registry = load_users_registry()
    entry = registry["data"].get(userId)
    if not entry or entry.get("token") != token:
        return jsonify({"error": "Invalid userId or token"}), 403

    if not filePath.startswith(f"{NOTES_FOLDER}/{userId}/"):
        return jsonify({"error": "You can only delete your own files"}), 403

    rec = get_repo_file(filePath)
    if not rec:
        return jsonify({"error": "File not found"}), 404
    sha = rec["sha"]
    try:
        delete_repo_file(filePath, sha)
    except Exception as e:
        return jsonify({"error": "Delete failed", "detail": str(e)}), 500

    return jsonify({"success": True})

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
