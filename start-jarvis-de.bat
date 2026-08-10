@echo off
REM ─────────────────────────────────────────────────────────────
REM  JARVIS (isair) — deutscher Start fuer Felix
REM  Config: C:\Users\User\.config\jarvis\config.json
REM  Wichtig: Fenster offen lassen. Schliesst stdin -> Daemon stoppt.
REM ─────────────────────────────────────────────────────────────
setlocal
cd /d "%~dp0"

set PYTHONPATH=%~dp0src
set PYTHONIOENCODING=utf-8
REM Auf 1 setzen fuer ausfuehrliche Audio-Diagnose:
set JARVIS_VOICE_DEBUG=0

echo.
echo  Starte JARVIS ... (erster Start laedt Modelle, dauert ~30-60 s)
echo  🎙️ Sag "Jarvis" am Anfang oder Ende des Satzes. Beenden mit Strg+C.
echo.
echo  Control Center im Browser: http://127.0.0.1:5055
echo  (Die tatsaechliche Adresse steht gleich unten im Log, Zeile "Control centre".)
echo.

".venv\Scripts\python.exe" -X utf8 -m jarvis.main

endlocal
