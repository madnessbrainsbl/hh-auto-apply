"""Temporary local noVNC window for logging into the container's own Chromium."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def main():
    processes = []
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        processes.append(subprocess.Popen(['Xvfb', ':99', '-screen', '0', '1280x900x24', '-nolisten', 'tcp']))
        for _ in range(50):
            if Path('/tmp/.X11-unix/X99').exists():
                break
            if processes[0].poll() is not None:
                raise RuntimeError('Xvfb не запустился')
            time.sleep(0.1)
        else:
            raise RuntimeError('Xvfb не готов за 5 секунд')
        os.environ['DISPLAY'] = ':99'
        processes.append(subprocess.Popen(['x11vnc', '-display', ':99', '-localhost', '-forever', '-shared', '-nopw']))
        processes.append(subprocess.Popen(['websockify', '--web=/usr/share/novnc', '6080', 'localhost:5900']))
        print('Вход в HH: http://localhost:6080/vnc.html?autoconnect=true', flush=True)
        login = subprocess.Popen([sys.executable, 'hh.py', 'login'])
        processes.append(login)
        return login.wait()
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
