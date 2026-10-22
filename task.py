"""
task.py — модель однієї задачі в черзі.
"""

import uuid


class Task:
    def __init__(self, task_type: str, data: dict, duration_minutes: float):
        self.task_id   = str(uuid.uuid4())[:8]
        self.task_type = task_type        # "order" | "carrier_invoice"
        self.data      = data             # розпарсені дані + налаштування
        self.duration  = duration_minutes * 60   # в секундах
        self.status    = "pending"        # pending → running → done / error
        self.error     = None
