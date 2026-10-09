@echo off
cd /d "%~dp0"
py -m pip install -r requirements.txt
echo.
echo Set your API key first, for example:
echo set OPENAI_API_KEY=your_key_here
echo Then run: py -m uvicorn server:app --host 0.0.0.0 --port 8000
pause
