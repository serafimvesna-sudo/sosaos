#!/bin/bash
# DJ Nelly для Mac: двойной клик — откроется окошко, куда кидать ссылки.
cd "$(dirname "$0")" || exit 1

for py in python3.14 python3.13 python3.12 python3.11 python3.10 \
          /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
          /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$py" >/dev/null 2>&1 &&
     "$py" -c 'import sys, tkinter; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
    nohup "$py" djnelly.py >/dev/null 2>&1 &
    disown
    exit 0
  fi
done

echo "Нужен Python 3.10 или новее с python.org."
echo "Сейчас открою сайт: скачай, установи и запусти DJ Nelly ещё раз."
open "https://www.python.org/downloads/macos/"
read -r -p "Нажми Enter, чтобы закрыть это окно"
