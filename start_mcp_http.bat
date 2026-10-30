@echo off
REM Serve ChemGraph's MCP tools over HTTP, with the read_artifact extension.
REM
REM Run this from the directory that holds your chemgraph venv and artifacts,
REM or point CHEMGRAPH_DIR / PYTHON at them explicitly:
REM     set CHEMGRAPH_DIR=D:\work\cg-artifacts
REM     set PYTHON=C:\venvs\chemgraph\Scripts\python.exe
setlocal
set "HERE=%~dp0"
if "%CHEMGRAPH_DIR%"=="" set "CHEMGRAPH_DIR=%CD%"
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
set OPENBLAS_NUM_THREADS=1
set OMP_NUM_THREADS=1
cd /d "%CHEMGRAPH_DIR%"

echo [ChemGraph] artifact root : %CHEMGRAPH_DIR%
echo [ChemGraph] python        : %PYTHON%
echo [ChemGraph] listening     : http://0.0.0.0:9003/mcp  (Ctrl+C to stop)
"%PYTHON%" "%HERE%mcp_http_server.py" --module chemgraph.mcp.mcp_tools --host 0.0.0.0 --port 9003 %*
pause
