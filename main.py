import sys
import io
from pathlib import Path

# Додаємо корінь проекту в sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
from app import run

if __name__ == "__main__":
    run()

