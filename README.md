# AI Game Studio Tablet — v0.3 Secure MVP

A tablet-friendly browser studio for editing Godot project files with an AI provider.

## Deploy from a tablet with GitHub + Render

1. Upload this ZIP to the root of your GitHub repository. Keep the filename `AI_Game_Studio_Tablet_v0_3_SECURE.zip`.
2. Open https://render.com and sign in with GitHub. Choose **New +** → **Web Service**, then connect `metosint/godot-ai-studio`.
3. Runtime: **Python 3**.
4. Build Command:
   `unzip -o AI_Game_Studio_Tablet_v0_3_SECURE.zip -d . && pip install -r AI_Game_Studio_Tablet/requirements.txt`
5. Start Command:
   `cd AI_Game_Studio_Tablet && uvicorn server:app --host 0.0.0.0 --port $PORT`
6. In Render → Environment, add these variables:
   - `STUDIO_USER` — choose a username for the studio login.
   - `STUDIO_PASSWORD` — choose a long, unique password (at least 20 characters).
   - `OPENAI_API_KEY` — optional; add only if using OpenAI.
   - `ANTHROPIC_API_KEY` — optional; add only if using Claude.
   - `GEMINI_API_KEY` — optional; add only if using Gemini.
7. Click **Create Web Service** and wait for deployment. Open the Render URL. The browser will ask for the studio username/password.

Never put provider API keys in GitHub, HTML, JavaScript, or a public message. Add them only in Render's Environment settings. Do not share the studio login.

## Local run

Requires Python 3.10+.

Set `STUDIO_USER` and `STUDIO_PASSWORD`, then install requirements and run:

`pip install -r requirements.txt`

`uvicorn server:app --host 0.0.0.0 --port 8000`

## MVP limitations

- Workspace, edit history, and snapshots are held in RAM; restarting the service loses them. Download updated project ZIPs regularly.
- Free hosting may sleep or restart, and its filesystem is not persistent.
- The server edits project files; it does not run Godot or validate gameplay in the engine.
- The AI provider may charge for requests. Agent mode makes multiple provider calls.
- Basic Auth is a simple access gate for this MVP, not a full multi-user production security system.
