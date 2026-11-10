@echo off
REM Inicia la pagina de administrador de MultiCam Vision.
REM Si habia otra abierta (por ejemplo una version anterior), la cierra sola.
cd /d "%~dp0"
python -c "import flask" 2>nul
if errorlevel 1 (
  echo Falta Flask. Instalalo una vez con:
  echo     python -m pip install flask
  pause
  exit /b 1
)
python -m admin --open %*
pause
