@echo off
chcp 65001 >nul
rem DJ Nelly для Windows: двойной клик — откроется окошко, куда кидать ссылки.
cd /d "%~dp0"

where pyw >nul 2>nul && (
  start "" pyw -3 "%~dp0djnelly.py"
  exit /b
)
where pythonw >nul 2>nul && (
  start "" pythonw "%~dp0djnelly.py"
  exit /b
)

echo Нужен Python с python.org. Сейчас открою сайт: скачай и установи.
echo При установке поставь галочку "Add python.exe to PATH", потом запусти DJ Nelly ещё раз.
start "" https://www.python.org/downloads/windows/
pause
