"""One data directory per account; the default keeps existing installations intact."""
import os
import re
from pathlib import Path

CODE_DIR = str(Path(__file__).resolve().parent)


def profile_directory(profile_id, root=None):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', profile_id):
        raise ValueError('Имя профиля: 1–64 латинских букв, цифр, _ или -')
    if profile_id.lower() in {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)),
                             *(f'lpt{i}' for i in range(1, 10))}:
        raise ValueError('Это имя зарезервировано Windows')
    base = Path(root or os.environ.get('HH_DATA_DIR') or CODE_DIR).resolve()
    target = base if profile_id == 'default' else (base / 'profiles' / profile_id).resolve()
    if not target.is_relative_to(base):
        raise ValueError('Папка профиля находится вне папки данных')
    return target


PROFILE_ID = os.environ.get('HH_PROFILE_ID', 'default')
if os.environ.get('HH_DATA_DIR'):
    # Child processes may change cwd; keep the account root stable.
    os.environ['HH_DATA_DIR'] = str(Path(os.environ['HH_DATA_DIR']).resolve())
DATA_DIR = str(profile_directory(PROFILE_ID))
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
