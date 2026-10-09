import os
import base64
import hmac
import io
import json
import zipfile
import tempfile
import difflib
import secrets
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

import urllib.request
import urllib.error

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

APP_DIR = Path(__file__).parent
MAX_UPLOAD = 30 * 1024 * 1024
MAX_FILES = 1500
MAX_CONTEXT_CHARS = 65000
TEXT_EXTS = {".gd", ".tscn", ".tres", ".godot", ".cfg", ".ini", ".json", ".txt", ".md", ".shader", ".gdshader", ".cs", ".xml", ".csv", ".yml", ".yaml"}
SKIP_PARTS = {".git", ".godot", ".import", "node_modules", "__pycache__"}

app = FastAPI(title="AI Game Studio")

# Simple HTTP Basic Authentication for the public MVP deployment.
# Set STUDIO_USER and STUDIO_PASSWORD in the hosting provider's environment.
@app.middleware("http")
async def require_studio_login(request, call_next):
    username = os.environ.get("STUDIO_USER", "").strip()
    password = os.environ.get("STUDIO_PASSWORD", "")
    # Fail closed: never expose the studio publicly without configured credentials.
    if not username or not password:
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(
            "Studio login is not configured. Set STUDIO_USER and STUDIO_PASSWORD on the server.",
            status_code=503,
        )
    auth = request.headers.get("authorization", "")
    valid = False
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:], validate=True).decode("utf-8")
            supplied_user, supplied_password = decoded.split(":", 1)
            valid = hmac.compare_digest(supplied_user, username) and hmac.compare_digest(supplied_password, password)
        except (ValueError, UnicodeDecodeError):
            valid = False
    if not valid:
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(
            "Login required", status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="AI Game Studio"'},
        )
    return await call_next(request)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "app")), name="static")

# In-memory workspace for this MVP. Use a persistent database for production.
WORKSPACES = {}
MAX_HISTORY = 80

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def workspace_or_404(project_id):
    ws = WORKSPACES.get(project_id)
    if not ws:
        raise HTTPException(404, "Проект не найден. Загрузи ZIP ещё раз.")
    return ws

def file_tree(files):
    return [{"path": p, "size": len(b), "text": Path(p).suffix.lower() in TEXT_EXTS and b"\x00" not in b[:2048]} for p,b in sorted(files.items())]

def add_history(ws, path, before, after, note="Ручное редактирование"):
    ws["history"].append({"id": secrets.token_hex(5), "time": now_iso(), "path": path, "before": before.decode("utf-8", "replace") if before is not None else None, "after": after.decode("utf-8", "replace") if after is not None else None, "note": note})
    ws["history"] = ws["history"][-MAX_HISTORY:]

def snapshot(ws, label):
    ws["versions"].append({"id": secrets.token_hex(5), "label": label[:100], "time": now_iso(), "files": dict(ws["files"])})
    return ws["versions"][-1]

def safe_extract(zip_bytes: bytes):
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise HTTPException(400, "Это не корректный ZIP-файл.")
    files = {}
    infos = zf.infolist()
    if len(infos) > MAX_FILES:
        raise HTTPException(413, f"Слишком много файлов: максимум {MAX_FILES}.")
    total_uncompressed = 0
    for info in infos:
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or not p.parts:
            continue
        if any(part in SKIP_PARTS for part in p.parts):
            continue
        if info.file_size > 5 * 1024 * 1024:
            continue
        total_uncompressed += info.file_size
        if total_uncompressed > 120 * 1024 * 1024:
            raise HTTPException(413, "Распакованный проект слишком большой (лимит 120 МБ).")
        try:
            data = zf.read(info)
        except Exception:
            continue
        files[name] = data
    if not files:
        raise HTTPException(400, "В ZIP не нашлось доступных файлов проекта.")
    return files

def build_context(files):
    names = sorted(files.keys())
    tree = "\n".join(names[:700])
    chunks = []
    remaining = MAX_CONTEXT_CHARS
    # prioritize project settings and scenes/scripts
    def priority(name):
        low = name.lower()
        if low.endswith("project.godot"): return 0
        if low.endswith(".gd"): return 1
        if low.endswith(".tscn"): return 2
        if low.endswith(".tres"): return 3
        return 4
    for name in sorted(names, key=priority):
        if Path(name).suffix.lower() not in TEXT_EXTS:
            continue
        raw = files[name]
        if b"\x00" in raw[:2048]:
            continue
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        part = f"\n\n===== FILE: {name} =====\n{content}\n"
        if len(part) > remaining:
            if remaining > 500:
                chunks.append(part[:remaining] + "\n[TRUNCATED]")
            break
        chunks.append(part)
        remaining -= len(part)
    return "PROJECT FILE TREE:\n" + tree + "\n\nTEXT FILE CONTENTS:\n" + "".join(chunks)

def call_openai(api_key, model, system_prompt, user_prompt):
    body = {"model": model or "gpt-4.1-mini", "input": [
        {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
        {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]}
    ]}
    req = urllib.request.Request("https://api.openai.com/v1/responses", data=json.dumps(body).encode(), headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"
    }, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode())
    texts = []
    for item in data.get("output", []):
        for c in item.get("content", []):
            if c.get("type") == "output_text":
                texts.append(c.get("text", ""))
    return "\n".join(texts).strip()

def call_anthropic(api_key, model, system_prompt, user_prompt):
    body = {"model": model or "claude-3-7-sonnet-latest", "max_tokens": 12000, "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), headers={
        "x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"
    }, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode())
    return "\n".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text").strip()

def call_gemini(api_key, model, system_prompt, user_prompt):
    model = model or "gemini-2.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    body = {"systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode())
    return "".join(p.get("text", "") for p in data.get("candidates", [{}])[0].get("content", {}).get("parts", [])).strip()

def ask_model(provider, model, prompt, context, mode):
    keys = {"openai": os.getenv("OPENAI_API_KEY"), "claude": os.getenv("ANTHROPIC_API_KEY"), "gemini": os.getenv("GEMINI_API_KEY")}
    key = keys.get(provider)
    if not key:
        envname = {"openai":"OPENAI_API_KEY", "claude":"ANTHROPIC_API_KEY", "gemini":"GEMINI_API_KEY"}.get(provider, "API_KEY")
        raise HTTPException(400, f"Не задан ключ {envname} на сервере.")
    if mode == "planning":
        system = "Ты опытный разработчик Godot 4. Составь конкретный план изменений. Ничего не редактируй. Отвечай по-русски."
        user = f"ЗАДАЧА:\n{prompt}\n\nКОНТЕКСТ ПРОЕКТА:\n{context}"
    elif mode == "answer":
        system = "Ты опытный разработчик Godot 4. Дай полезный ответ по-русски. Не создавай патч и не утверждай, что менял файлы."
        user = f"ВОПРОС:\n{prompt}\n\nКОНТЕКСТ ПРОЕКТА:\n{context}"
    else:
        system = """Ты аккуратно редактируешь проекты Godot 4. Верни ТОЛЬКО валидный JSON без markdown:
{"summary":"краткое описание по-русски","changes":[{"path":"relative/path.gd","content":"полное новое содержимое файла"}]}
Можно добавлять новые файлы. Для изменения существующего файла верни полное содержимое. Не меняй файлы, не относящиеся к задаче. Пути должны быть относительными, без .. и без абсолютных путей. Если проектного контекста недостаточно, верни changes пустым и объясни в summary. Не выполняй и не добавляй команды для запуска внешних процессов. Сохраняй совместимость с Godot 4."""
        user = f"ЗАДАЧА:\n{prompt}\n\nКОНТЕКСТ ПРОЕКТА:\n{context}"
    try:
        if provider == "openai":
            return call_openai(key, model, system, user)
        if provider == "claude":
            return call_anthropic(key, model, system, user)
        if provider == "gemini":
            return call_gemini(key, model, system, user)
    except urllib.error.HTTPError as e:
        details = e.read().decode("utf-8", "replace")[:1200]
        raise HTTPException(502, f"Ошибка API провайдера ({e.code}): {details}")
    except Exception as e:
        raise HTTPException(502, f"Не удалось связаться с API провайдера: {str(e)[:500]}")
    raise HTTPException(400, "Неизвестный провайдер.")

def extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise HTTPException(502, "Модель не вернула JSON изменений. Повтори запрос или уточни задачу.")
    try:
        return json.loads(text[start:end+1])
    except json.JSONDecodeError:
        raise HTTPException(502, "Ответ модели содержит некорректный JSON. Попробуй повторить запрос.")

@app.get("/sw.js")
def service_worker():
    return FileResponse(str(APP_DIR / "app" / "sw.js"), media_type="application/javascript", headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

@app.get("/", response_class=HTMLResponse)
def index():
    return (APP_DIR / "app" / "index.html").read_text(encoding="utf-8")

@app.get("/api/status")
def status():
    return {"providers": {
        "openai": bool(os.getenv("OPENAI_API_KEY")),
        "claude": bool(os.getenv("ANTHROPIC_API_KEY")),
        "gemini": bool(os.getenv("GEMINI_API_KEY"))
    }}

@app.post("/api/work")
async def work(project: UploadFile = File(...), provider: str = Form(...), model: str = Form(""),
               mode: str = Form(...), prompt: str = Form(...)):
    if mode not in {"planning", "answer", "auto"}:
        raise HTTPException(400, "Неизвестный режим.")
    if provider not in {"openai", "claude", "gemini"}:
        raise HTTPException(400, "Неизвестный провайдер.")
    if not prompt.strip():
        raise HTTPException(400, "Напиши задачу.")
    data = await project.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "ZIP больше 30 МБ.")
    files = safe_extract(data)
    context = build_context(files)
    result = ask_model(provider, model.strip(), prompt.strip(), context, mode)
    if mode != "auto":
        return JSONResponse({"mode": mode, "answer": result})
    payload = extract_json(result)
    changes = payload.get("changes", [])
    if not isinstance(changes, list):
        raise HTTPException(502, "Модель вернула неверный список changes.")
    applied = []
    for item in changes[:80]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("content"), str):
            continue
        name = item["path"].replace("\\", "/")
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or not p.parts or len(item["content"]) > 2_000_000:
            continue
        if name.startswith(".git/") or name.startswith(".godot/"):
            continue
        files[name] = item["content"].encode("utf-8")
        applied.append(name)
    if not applied:
        return JSONResponse({"mode":"auto", "summary": payload.get("summary", "Нет применённых изменений."), "changed_files": [], "answer": "Модель не предложила подходящих изменений. Уточни задачу."})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return StreamingResponse(io.BytesIO(output.getvalue()), media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="AI_Game_Studio_edited_project.zip"',
                 "X-Changed-Files": json.dumps(applied, ensure_ascii=False),
                 "X-Change-Summary": payload.get("summary", "Проект изменён")[:500]})



@app.post("/api/project/upload")
async def project_upload(project: UploadFile = File(...)):
    data = await project.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "ZIP больше 30 МБ.")
    files = safe_extract(data)
    pid = secrets.token_urlsafe(12)
    ws = {"id": pid, "name": project.filename or "Godot project", "files": files, "history": [], "versions": [], "created": now_iso()}
    WORKSPACES[pid] = ws
    snapshot(ws, "Исходная версия")
    return {"project_id": pid, "name": ws["name"], "tree": file_tree(files), "count": len(files)}

@app.get("/api/project/{project_id}/tree")
def project_tree(project_id: str):
    ws = workspace_or_404(project_id)
    return {"tree": file_tree(ws["files"]), "name": ws["name"]}

@app.get("/api/project/{project_id}/file")
def project_read_file(project_id: str, path: str):
    ws = workspace_or_404(project_id)
    if path not in ws["files"]:
        raise HTTPException(404, "Файл не найден.")
    data = ws["files"][path]
    try:
        content = data.decode("utf-8")
        binary = False
    except UnicodeDecodeError:
        content, binary = "", True
    return {"path": path, "content": content, "binary": binary, "size": len(data)}

@app.put("/api/project/{project_id}/file")
async def project_save_file(project_id: str, payload: dict):
    ws = workspace_or_404(project_id)
    path, content = payload.get("path"), payload.get("content")
    if not isinstance(path, str) or not isinstance(content, str):
        raise HTTPException(400, "Нужны path и content.")
    pp = PurePosixPath(path.replace("\\", "/"))
    if pp.is_absolute() or ".." in pp.parts or not pp.parts or len(content) > 2_000_000:
        raise HTTPException(400, "Недопустимый путь или слишком большой файл.")
    path = str(pp)
    before = ws["files"].get(path)
    after = content.encode("utf-8")
    if before == after:
        return {"ok": True, "changed": False}
    add_history(ws, path, before, after)
    ws["files"][path] = after
    return {"ok": True, "changed": True, "tree": file_tree(ws["files"])}

@app.get("/api/project/{project_id}/history")
def project_history(project_id: str):
    ws = workspace_or_404(project_id)
    return {"history": [{k:v for k,v in h.items() if k not in ("before", "after")} | {"has_before": h["before"] is not None, "has_after": h["after"] is not None} for h in reversed(ws["history"])]}

@app.post("/api/project/{project_id}/rollback/{history_id}")
def project_rollback(project_id: str, history_id: str):
    ws = workspace_or_404(project_id)
    h = next((x for x in ws["history"] if x["id"] == history_id), None)
    if not h:
        raise HTTPException(404, "Запись истории не найдена.")
    path = h["path"]
    current = ws["files"].get(path)
    if h["before"] is None:
        ws["files"].pop(path, None)
        restored = None
    else:
        restored = h["before"].encode("utf-8")
        ws["files"][path] = restored
    add_history(ws, path, current, restored, "Откат: " + h["note"])
    return {"ok": True, "tree": file_tree(ws["files"])}

@app.post("/api/project/{project_id}/versions")
def project_create_version(project_id: str, payload: dict):
    ws = workspace_or_404(project_id)
    v = snapshot(ws, payload.get("label", "Сохранённая версия"))
    return {"id": v["id"], "label": v["label"], "time": v["time"]}

@app.get("/api/project/{project_id}/versions")
def project_versions(project_id: str):
    ws = workspace_or_404(project_id)
    return {"versions": [{"id":v["id"],"label":v["label"],"time":v["time"],"files":len(v["files"])} for v in reversed(ws["versions"])]}

@app.get("/api/project/{project_id}/diff")
def project_diff(project_id: str, a: str, b: str, path: str):
    ws = workspace_or_404(project_id)
    va = next((v for v in ws["versions"] if v["id"] == a), None)
    vb = next((v for v in ws["versions"] if v["id"] == b), None)
    if not va or not vb: raise HTTPException(404, "Версия не найдена.")
    def text_of(v):
        raw = v["files"].get(path, b"")
        return raw.decode("utf-8", "replace").splitlines(True)
    diff = "".join(difflib.unified_diff(text_of(va), text_of(vb), fromfile=f"{va['label']}:{path}", tofile=f"{vb['label']}:{path}"))
    return {"diff": diff or "Нет различий для этого файла."}

@app.get("/api/project/{project_id}/download")
def project_download(project_id: str):
    ws = workspace_or_404(project_id)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in ws["files"].items(): zf.writestr(name, content)
    return StreamingResponse(io.BytesIO(output.getvalue()), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="Godot_Studio_Project.zip"'})

@app.post("/api/project/{project_id}/agents")
def project_agents(project_id: str, payload: dict):
    ws = workspace_or_404(project_id)
    provider = payload.get("provider", "openai")
    model = payload.get("model", "")
    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.strip(): raise HTTPException(400, "Напиши задачу для команды агентов.")
    context = build_context(ws["files"])
    # Three focused passes: architect plan, implementation patch, reviewer critique.
    plan = ask_model(provider, model, "Ты архитектор Godot 4. Составь краткий технический план. Не редактируй файлы. Отвечай по-русски.", f"Задача: {prompt}\n\nПроект:\n{context}", "planning")
    implement_prompt = f"Задача пользователя: {prompt}\nПлан архитектора: {plan}\n\nКонтекст проекта:\n{context}"
    implementation = ask_model(provider, model, "Ты ведущий разработчик Godot 4. Верни ТОЛЬКО JSON: {\"summary\":\"...\",\"changes\":[{\"path\":\"relative/file.gd\",\"content\":\"полное содержимое\"}]}. Не используй абсолютные пути или .. .", implement_prompt, "auto")
    payload_json = extract_json(implementation)
    changes = payload_json.get("changes", [])
    review = ask_model(provider, model, "Ты независимый ревьюер Godot 4. Оцени план и патч на возможные ошибки, совместимость и недостающие проверки. Не редактируй файлы. Отвечай по-русски.", f"Задача: {prompt}\nПлан: {plan}\nПатч: {json.dumps(changes, ensure_ascii=False)[:24000]}", "answer")
    applied=[]
    for item in changes[:80]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("content"), str): continue
        name=item["path"].replace("\\", "/"); pp=PurePosixPath(name)
        if pp.is_absolute() or ".." in pp.parts or not pp.parts or len(item["content"])>2_000_000: continue
        if name.startswith((".git/", ".godot/")): continue
        before=ws["files"].get(name); after=item["content"].encode("utf-8")
        if before != after:
            add_history(ws,name,before,after,"ИИ-команда: " + prompt[:100]); ws["files"][name]=after; applied.append(name)
    if applied: snapshot(ws, "ИИ: " + prompt[:60])
    return {"plan":plan,"summary":payload_json.get("summary","Изменения подготовлены"),"review":review,"changed_files":applied,"tree":file_tree(ws["files"])}
