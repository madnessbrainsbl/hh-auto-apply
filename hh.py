"""Profile-aware entry point and sequential, restartable scheduler."""
import argparse
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from contextlib import contextmanager

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, 'reconfigure'):
        stream.reconfigure(encoding='utf-8', errors='replace')


@contextmanager
def profile_lock(directory):
    with open(Path(directory) / '.run.lock', 'a+b') as lock:
        if os.fstat(lock.fileno()).st_size == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Этот профиль уже запущен; сначала остановите другую копию') from exc
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def schedule(args):
    if (not math.isfinite(args.every_minutes) or args.every_minutes < 1
            or args.every_minutes > threading.TIMEOUT_MAX / 60):
        raise ValueError('Интервал должен быть конечным числом не меньше минуты')
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    command = [sys.executable, str(Path(__file__).resolve()), '--profile-id', args.profile_id, 'run']
    if args.headless:
        command.append('--headless')
    if args.limit is not None:
        command += ['--limit', str(args.limit)]
    while not stopped.is_set():
        print(f'Запуск профиля {args.profile_id}', flush=True)
        child = subprocess.Popen(command)
        try:
            while child.poll() is None and not stopped.wait(1):
                pass
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        print(f'Профиль {args.profile_id}: код завершения {child.returncode}', flush=True)
        stopped.wait(args.every_minutes * 60)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='HH: аккаунты, настройки и расписание')
    parser.add_argument('--profile-id', default=os.environ.get('HH_PROFILE_ID', 'default'))
    parser.add_argument('command', choices=('menu', 'login', 'run', 'schedule', 'settings', 'profiles'), nargs='?', default='menu')
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--every-minutes', type=float, default=240)
    parser.add_argument('--ai-filter', choices=('off', 'light', 'heavy', 'custom'))
    parser.add_argument('--filter-prompt')
    parser.add_argument('--captcha', choices=('on', 'off'))
    parser.add_argument('--captcha-model')
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error('--limit должен быть больше нуля')
    os.environ['HH_PROFILE_ID'] = args.profile_id
    try:
        from app_paths import CODE_DIR, DATA_DIR, profile_directory
        # Validate before imports that might open account data or launch a browser.
        profile_directory(args.profile_id)
        if args.command == 'profiles':
            root = Path(os.environ.get('HH_DATA_DIR') or CODE_DIR)
            print('default')
            profiles = root / 'profiles'
            if profiles.exists():
                for folder in sorted(profiles.iterdir()):
                    if folder.is_dir():
                        print(folder.name)
            return 0
        if args.command == 'schedule':
            return schedule(args)
        with profile_lock(DATA_DIR):
            from config_manager import load_config, save_config
            config = load_config()
            if args.command == 'settings':
                if args.ai_filter:
                    config.setdefault('ai_filter', {})['mode'] = args.ai_filter
                if args.filter_prompt is not None:
                    config.setdefault('ai_filter', {})['prompt'] = args.filter_prompt
                if ((config.get('ai_filter') or {}).get('mode') == 'custom'
                        and not (config.get('ai_filter') or {}).get('prompt', '').strip()):
                    raise ValueError('Для custom задайте --filter-prompt')
                if args.captcha:
                    config.setdefault('captcha', {})['enabled'] = args.captcha == 'on'
                if args.captcha_model:
                    config.setdefault('captcha', {})['model'] = args.captcha_model
                if not save_config(config):
                    return 1
                print(json.dumps({'profile': args.profile_id, 'directory': DATA_DIR,
                    'ai_filter': (config.get('ai_filter') or {}).get('mode', 'off'),
                    'captcha': bool((config.get('captcha') or {}).get('enabled'))}, ensure_ascii=False))
                return 0
            if args.command == 'menu':
                import runpy
                sys.argv = [str(Path(CODE_DIR) / 'test.py'), '--menu']
                runpy.run_path(sys.argv[0], run_name='__main__')
                return 0
            if args.command == 'run':
                resume_id = str(config.get('resume_id') or '').strip()
                if not resume_id or resume_id.startswith('YOUR_'):
                    raise ValueError('Сначала войдите командой login и выберите резюме через menu → P → R')
            if args.command == 'login' and args.headless:
                raise ValueError('Для входа нужно видимое окно браузера')
            from hh_selenium import HHSeleniumBot
            bot = HHSeleniumBot(headless=args.headless, pause_before_close=False)
            if args.command == 'login':
                try:
                    if not bot.init_driver():
                        return 1
                    return 0 if bot.check_login() or bot.login() else 1
                finally:
                    bot.close_driver()
            if args.limit is not None:
                bot.config['max_applications'] = args.limit
                bot.config['stop_at_local_limit'] = True
            def interrupt(*_):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, interrupt)
            bot.run_api_cache()
            return 1 if getattr(bot, 'run_failed', False) or bot.errors else 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
