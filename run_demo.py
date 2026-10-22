"""запускає два незалежні вузли nexus без docker."""

import signal
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent
    # кожен процес слухає окремий порт і вказує інший процес як вузол-партнер
    commands = [
        [
            sys.executable,
            "run_node.py",
            "--node-id",
            "node-a",
            "--port",
            "8001",
            "--peer",
            "http://127.0.0.1:8002",
        ],
        [
            sys.executable,
            "run_node.py",
            "--node-id",
            "node-b",
            "--port",
            "8002",
            "--peer",
            "http://127.0.0.1:8001",
        ],
    ]
    processes = [subprocess.Popen(command, cwd=root) for command in commands]
    print("\nNexus запущено:")
    print("  Node A: http://localhost:8001")
    print("  Node B: http://localhost:8002")
    print("Натисніть Ctrl+C для завершення обох вузлів.\n")
    try:
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        # зупинка батьківського скрипту завершує обидва вузли, щоб не залишати фонові процеси
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    main()
