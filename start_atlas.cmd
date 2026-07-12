@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=C:\Users\engtv\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%PYTHON%" (
  echo Runtime Python do Codex nao encontrado.
  echo Reabra o projeto no Codex ou configure o caminho do Python neste arquivo.
  pause
  exit /b 1
)
echo.
echo ATLAS - Gestao de Pendencias FICO
echo Acesse: http://127.0.0.1:8000
echo Pressione Ctrl+C para encerrar.
echo.
"%PYTHON%" backend\server.py
endlocal
