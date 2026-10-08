#!/usr/bin/env python3
"""DJ Nelly: вставил ссылку с YouTube, mp3 упала в папку «DJ Nelly/YouTube».

Окно запускается на обычном Python (нужен tkinter). Качалка (yt-dlp, ffmpeg,
deno) живёт в отдельном окружении в папке данных приложения и обновляется
при каждом запуске, потому что YouTube постоянно всё ломает.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # Homebrew python без python-tk и подобное
    sys.exit('DJ Nelly: нужен Python с tkinter. Поставь Python с python.org')

APP = 'DJ Nelly'
SUBFOLDER = 'YouTube'
PACKAGES = ['yt-dlp[default]', 'deno', 'imageio-ffmpeg']
PYTHON_URL = 'https://www.python.org/downloads/'

IS_WIN = sys.platform == 'win32'
IS_MAC = sys.platform == 'darwin'
HOME = Path.home()
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WIN else 0

URL_RE = re.compile(r'https?://[^\s<>"\']+')
# Так может называться папка: «DJ Nelly», «DJ_Nelly», «djnelly», «Диджей Нелли»...
DJ_FOLDER_NAMES = {'djnelly', 'диджейнелли', 'djнелли'}
SKIP_DIRS = {
    'library', 'appdata', 'application data', 'applications', 'pictures',
    'node_modules', 'program files', 'program files (x86)', 'programdata',
    'windows', '$recycle.bin', 'system volume information', 'system',
    'private', 'cores', 'dev', 'bin', 'sbin', 'usr', 'opt', 'etc', 'var',
    'tmp', 'proc', 'snap', 'boot',
}
SKIP_SUFFIXES = (
    '.app', '.photoslibrary', '.musiclibrary', '.tvlibrary', '.bundle',
    '.framework', '.plugin', '.vst', '.vst3', '.component', '.logicx',
    '.band', '.git',
)
# Сначала смотрим туда, где музыка обычно и лежит
PRIORITY_DIRS = ['music', 'музыка', 'desktop', 'рабочий стол', 'documents', 'документы', 'downloads', 'загрузки']


def data_dir():
    if IS_WIN:
        base = Path(os.environ.get('LOCALAPPDATA') or HOME / 'AppData' / 'Local')
    elif IS_MAC:
        base = HOME / 'Library' / 'Application Support'
    else:
        base = Path(os.environ.get('XDG_DATA_HOME') or HOME / '.local' / 'share')
    return base / 'DJ Nelly Downloader'


def child_env():
    env = dict(os.environ)
    env.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PIP_DISABLE_PIP_VERSION_CHECK='1')
    return env


def run(args, **kwargs):
    return subprocess.run(
        [str(a) for a in args], capture_output=True, text=True, encoding='utf-8',
        errors='replace', env=child_env(), creationflags=NO_WINDOW, **kwargs)


def open_in_file_manager(path):
    if IS_WIN:
        os.startfile(path)
    else:
        subprocess.Popen(['open' if IS_MAC else 'xdg-open', str(path)])


# ---------------------------------------------------------------- поиск папки

def is_dj_folder(name):
    return re.sub(r'[\W_]+', '', name.lower()) in DJ_FOLDER_NAMES


def search_roots():
    roots = [(HOME, 5)]
    if IS_MAC:
        volumes = Path('/Volumes')
        if volumes.is_dir():
            roots += [(v, 3) for v in sorted(volumes.iterdir()) if not v.is_symlink()]
    elif IS_WIN:
        import ctypes
        import string
        ctypes.windll.kernel32.SetErrorMode(1)  # без окон «вставьте диск»
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        roots += [(Path(letter + ':\\'), 3)
                  for i, letter in enumerate(string.ascii_uppercase) if mask >> i & 1]
    else:
        for base in (Path('/media') / HOME.name, Path('/run/media') / HOME.name, Path('/mnt')):
            if base.is_dir():
                roots.append((base, 3))
    return roots


def priority(name):
    name = name.lower()
    return PRIORITY_DIRS.index(name) if name in PRIORITY_DIRS else len(PRIORITY_DIRS)


def find_dj_folder(time_limit=20):
    """Ищет папку DJ Nelly: сначала в домашней папке, потом на дисках и флешках."""
    deadline = time.monotonic() + time_limit
    seen = set()
    for root, max_depth in search_roots():
        level = [root]
        for _ in range(max_depth):
            next_level = []
            for folder in level:
                if time.monotonic() > deadline:
                    return None
                try:
                    entries = sorted(os.scandir(folder), key=lambda e: (priority(e.name), e.name.lower()))
                except OSError:
                    continue
                for entry in entries:
                    name = entry.name
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if (name.startswith(('.', '$')) or name.lower() in SKIP_DIRS
                            or name.lower().endswith(SKIP_SUFFIXES)):
                        continue
                    if is_dj_folder(name):
                        return Path(entry.path)
                    key = os.path.normcase(entry.path)
                    if key not in seen:
                        seen.add(key)
                        next_level.append(entry.path)
            level = next_level
    return None


# ---------------------------------------------------------------- качалка

TOOLS_PROBE = '''
try:
    import deno; print(deno.find_deno_bin())
except Exception:
    print()
try:
    import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())
except Exception:
    print()
'''


class Downloader:
    def __init__(self):
        self.dir = data_dir()
        self.env = self.dir / 'env'
        self.python = self.env / ('Scripts/python.exe' if IS_WIN else 'bin/python')
        self.ready_marker = self.env / '.dj-nelly-ready'
        self.temp = self.dir / 'tmp'
        self.deno = self.ffmpeg = None
        self.updated_to_nightly = False
        self.proc = None

    def setup(self, status):
        if self.ready_marker.exists() and self._works():
            status('Проверяю обновления...')
            self.pip_install(PACKAGES)  # нет интернета — не страшно, работаем на старой
        else:
            status('Первый запуск: ставлю качалку, это минута-две...')
            self.dir.mkdir(parents=True, exist_ok=True)
            self._check(run([sys.executable, '-m', 'venv', '--clear', self.env]), 'создать окружение')
            self._check(self.pip_install(PACKAGES), 'скачать качалку (есть интернет?)')
            self.ready_marker.touch()
        self._find_tools()

    def _works(self):
        try:
            return run([self.python, '-c', 'import yt_dlp']).returncode == 0
        except OSError:
            return False

    def pip_install(self, packages, pre=False):
        args = [self.python, '-m', 'pip', 'install', '--upgrade', '--quiet', '--retries', '1', '--timeout', '15']
        return run(args + (['--pre'] if pre else []) + packages)

    def _find_tools(self):
        probe = run([self.python, '-c', TOOLS_PROBE]).stdout.split('\n') + ['', '']
        self.deno = probe[0].strip() or shutil.which('deno')
        self.ffmpeg = probe[1].strip() or shutil.which('ffmpeg')
        if not self.ffmpeg:
            raise RuntimeError('Не нашёл ffmpeg. Закрой и открой DJ Nelly ещё раз.')

    @staticmethod
    def _check(result, what):
        if result.returncode:
            tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
            raise RuntimeError('Не получилось ' + what + ': ' + ' '.join(tail))

    def command(self, url, dest):
        cmd = [
            self.python, '-m', 'yt_dlp', url,
            '--ignore-config', '--no-playlist', '--color', 'never',
            '--quiet', '--no-warnings', '--progress', '--newline', '--no-simulate',
            '--ffmpeg-location', self.ffmpeg,
            '-f', 'bestaudio/best', '-x', '--audio-format', 'mp3', '--audio-quality', '320K',
            '--embed-metadata', '--embed-thumbnail', '--convert-thumbnails', 'jpg',
            # обложка квадратом, как у нормальных треков
            '--ppa', 'ThumbnailsConvertor+ffmpeg_o:-c:v mjpeg -qmin 1 -qscale:v 1 '
                     '-vf crop="\'if(gt(ih,iw),iw,ih)\':\'if(gt(iw,ih),ih,iw)\'"',
            '--no-mtime', '--windows-filenames',
            '-P', dest, '-P', 'temp:' + str(self.temp), '-o', '%(title)s.%(ext)s',
            '--print', 'before_dl:[djt] %(title)s',
            '--print', 'before_dl:[dje] %(filename)s',
            '--print', 'after_move:[djf] %(filepath)s',
            '--progress-template', 'download:[djn] %(progress._percent_str)s',
            '--progress-template', 'postprocess:[djp] %(progress.postprocessor)s',
        ]
        if self.deno:
            cmd += ['--js-runtimes', 'deno:' + self.deno]
        return [str(c) for c in cmd]

    def download(self, url, dest, on_event):
        """Качает ссылку, на каждый шаг зовёт on_event(вид, текст). Возвращает (файлы, ошибка)."""
        files, error = self._download_once(url, dest, on_event)
        if error and not files and not self.updated_to_nightly:
            # Часто YouTube что-то поменял, а фикс уже есть в свежей сборке yt-dlp
            self.updated_to_nightly = True
            on_event('status', 'Не вышло, обновляю качалку и пробую ещё раз...')
            self.pip_install(['yt-dlp[default]'], pre=True)
            files, error = self._download_once(url, dest, on_event)
        return files, error

    def _download_once(self, url, dest, on_event):
        self.temp.mkdir(parents=True, exist_ok=True)
        files, errors, existed = [], [], set()
        self.proc = subprocess.Popen(
            self.command(url, dest), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', env=child_env(),
            creationflags=NO_WINDOW)
        for line in self.proc.stdout:
            line = line.strip()
            tag, _, text = line.partition(' ')
            if tag == '[djt]':
                on_event('title', text)
            elif tag == '[djn]':
                on_event('progress', text.strip())
            elif tag == '[dje]':
                mp3 = Path(text).with_suffix('.mp3')
                if mp3.exists():
                    existed.add(os.path.normcase(str(mp3)))
            elif tag == '[djp]':
                if text in ('ExtractAudio', 'FFmpegExtractAudio'):
                    on_event('convert', '')
            elif tag == '[djf]':
                path = Path(text)
                files.append((path, os.path.normcase(text) in existed))
                on_event('done', path.stem)
            elif line.startswith('ERROR:'):
                errors.append(line[6:].strip())
        self.proc.wait()
        code, self.proc = self.proc.returncode, None
        if code and not errors:
            errors.append('yt-dlp завершился с кодом %s' % code)
        return files, (errors[-1] if errors else None)

    def stop(self):
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()


# ---------------------------------------------------------------- окно

class App:
    def __init__(self, root):
        self.root = root
        self.ui_queue = queue.Queue()
        self.links = queue.Queue()
        self.config_path = data_dir() / 'config.json'
        self.config = self._load_config()
        self.downloader = Downloader()
        self.ready = threading.Event()
        self.busy = False
        self.title = ''
        self.done_count = 0
        self.failed = []

        root.title(APP)
        root.resizable(False, False)
        frame = ttk.Frame(root, padding=14)
        frame.grid()
        ttk.Label(frame, text='Вставь ссылку с YouTube, mp3 сама упадёт в папку').grid(sticky='w')
        self.entry = ttk.Entry(frame, width=56)
        self.entry.grid(sticky='we', pady=(6, 8))
        self.status = ttk.Label(frame, width=56, wraplength=430, text='Запускаюсь...')
        self.status.grid(sticky='w')
        self.folder_label = ttk.Label(frame, foreground='#3d7fd9', cursor='hand2', text=' ')
        self.folder_label.grid(sticky='w', pady=(6, 0))

        paste_mod = 'Command' if IS_MAC else 'Control'
        self.entry.bind('<%s-KeyPress>' % paste_mod, self._on_ctrl_key)
        self.entry.bind('<<Paste>>', self._paste)
        self.entry.bind('<Return>', self._submit_typed)
        menu = tk.Menu(root, tearoff=False)
        menu.add_command(label='Вставить ссылку', command=self._paste)
        menu.add_command(label='Открыть папку', command=self._open_folder)
        menu.add_command(label='Сменить папку...', command=self._choose_folder)
        right_click = ('<Button-2>', '<Control-Button-1>') if IS_MAC else ('<Button-3>',)
        for seq in right_click:
            for widget in (self.entry, self.status, self.folder_label, frame):
                widget.bind(seq, lambda e: menu.tk_popup(e.x_root, e.y_root))
        self.folder_label.bind('<Button-1>', lambda e: self._open_folder())

        root.protocol('WM_DELETE_WINDOW', self._on_close)
        self.entry.focus_set()
        self._show_folder()
        self._poll_ui()
        threading.Thread(target=self._prepare, daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()

    # --- настройки

    def _load_config(self):
        try:
            return json.loads(self.config_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def _save_config(self):
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError:
            pass

    def dj_folder(self):
        folder = self.config.get('dj_folder')
        return Path(folder) if folder else None

    def target(self):
        folder = self.dj_folder()
        return folder / SUBFOLDER if folder else None

    def _set_dj_folder(self, folder):
        self.config['dj_folder'] = str(folder)
        self._save_config()
        try:
            (folder / SUBFOLDER).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._show_folder()

    # --- из фоновых потоков в окно

    def ui(self, fn, *args):
        self.ui_queue.put((fn, args))

    def _poll_ui(self):
        try:
            while True:
                fn, args = self.ui_queue.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_ui)

    def set_status(self, text):
        self.ui(self.status.configure, {'text': text})

    def _show_folder(self):
        target = self.target()
        if target:
            text = str(target)
            if text.startswith(str(HOME)):
                text = '~' + text[len(str(HOME)):]
            self.folder_label.configure(text='Папка: ' + text)
        else:
            self.folder_label.configure(text='Ищу папку DJ Nelly...')

    # --- подготовка: качалка + папка

    def _prepare(self):
        try:
            self.downloader.setup(self.set_status)
        except Exception as exc:  # noqa: BLE001 — любую ошибку показываем в окне
            self.set_status(str(exc)[:300])
            return
        self._ensure_folder()
        self.ready.set()
        if not self.busy and self.links.empty():
            self.set_status('Готов. Кидай ссылку.')

    def _ensure_folder(self):
        folder = self.dj_folder()
        if folder and folder.is_dir():
            return
        if folder:
            self.set_status('Не вижу папку %s (флешку вытащили?), ищу...' % folder)
        else:
            self.set_status('Ищу папку DJ Nelly на компе...')
        found = find_dj_folder()
        if found:
            self._set_dj_folder_from_thread(found)
        else:
            answer = threading.Event()
            self.ui(self._ask_folder, answer)
            answer.wait()

    def _set_dj_folder_from_thread(self, folder):
        self.config['dj_folder'] = str(folder)
        done = threading.Event()
        self.ui(lambda: (self._set_dj_folder(folder), done.set()))
        done.wait()

    def _ask_folder(self, answer):
        messagebox.showinfo(APP, 'Не нашёл папку DJ Nelly. Покажи, где она.\n'
                                 'Нажмёшь «Отмена» — создам её в папке «Музыка».')
        chosen = filedialog.askdirectory(title='Где папка DJ Nelly?', initialdir=HOME, mustexist=True)
        self._set_dj_folder(Path(chosen) if chosen else HOME / 'Music' / APP)
        answer.set()

    def _choose_folder(self):
        chosen = filedialog.askdirectory(title='Папка DJ Nelly (внутри будет YouTube)', initialdir=self.dj_folder() or HOME,
                                         mustexist=True)
        if chosen:
            self._set_dj_folder(Path(chosen))

    def _open_folder(self):
        target = self.target()
        if target:
            target.mkdir(parents=True, exist_ok=True)
            open_in_file_manager(target)

    # --- ссылки

    def _on_ctrl_key(self, event):
        # Ctrl/Cmd+V работает и на русской раскладке (там вместо v буква «м»)
        if event.keysym.lower() in ('v', 'cyrillic_em') or (IS_WIN and event.keycode == 86):
            return self._paste()
        return None

    def _paste(self, event=None):
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            text = ''
        self._add_links(text)
        return 'break'

    def _submit_typed(self, event=None):
        self._add_links(self.entry.get())
        return 'break'

    def _add_links(self, text):
        urls = URL_RE.findall(text)
        self.entry.delete(0, 'end')
        if not urls:
            self.status.configure(text='Это не ссылка. Скопируй ссылку на видео и вставь сюда.')
            return
        for url in urls:
            self.links.put(url)
        if self.busy or not self.ready.is_set():
            self.status.configure(text=self._progress_text('в очереди'))

    def _progress_text(self, what):
        waiting = self.links.qsize()
        line = what + (' — ' + self.title if self.title else '')
        if waiting:
            line += ' (ещё %d в очереди)' % waiting
        return line[:120]

    def _worker(self):
        while True:
            url = self.links.get()
            self.ready.wait()
            self.busy = True
            self.title = ''
            files = self._download(url)
            self.busy = False
            if self.links.empty():
                self._finish(files)

    def _download(self, url):
        self._ensure_folder()
        target = self.target()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.failed.append((url, 'не могу создать папку: %s' % exc))
            return []
        self.set_status(self._progress_text('Начинаю'))
        files, error = self.downloader.download(url, target, self._on_download_event)
        if error and not files:
            self.failed.append((self.title or url, error))
        self.done_count += sum(1 for _, already in files if not already)
        return files

    def _on_download_event(self, kind, text):
        if kind == 'title':
            self.title = text
            self.set_status(self._progress_text('Качаю'))
        elif kind == 'progress':
            self.set_status(self._progress_text('Качаю ' + text))
        elif kind == 'convert':
            self.set_status(self._progress_text('Делаю mp3'))
        elif kind == 'status':
            self.set_status(text)

    def _finish(self, last_files):
        if self.failed:
            name, reason = self.failed[-1]
            text = 'Скачано: %d. Не вышло %d: %s — %s' % (self.done_count, len(self.failed), name, reason)
        elif len(last_files) == 1 and last_files[0][1] and self.done_count == 0:
            text = 'Уже есть в папке: ' + last_files[0][0].stem
        elif self.done_count == 1 and len(last_files) == 1:
            text = 'Готово: ' + last_files[0][0].stem
        else:
            text = 'Готово, скачано: %d' % self.done_count
        self.done_count = 0
        self.failed = []
        self.set_status(text[:160])

    def _on_close(self):
        if self.busy and not messagebox.askyesno(APP, 'Ещё качается. Всё равно закрыть?'):
            return
        self.downloader.stop()
        self.root.destroy()


def main():
    if sys.version_info < (3, 10):
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP, 'Нужен Python поновее (3.10+). Сейчас открою сайт, '
                                  'скачай оттуда Python и запусти DJ Nelly ещё раз.')
        import webbrowser
        webbrowser.open(PYTHON_URL)
        return
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
