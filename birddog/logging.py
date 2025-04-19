# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
Common logging configuration
"""

# LOGGING --------------------------------------------------------------

import logging
import sys
from collections import deque

class InMemoryLogHandler(logging.Handler):
    def __init__(self, capacity=1000):
        super().__init__()
        self.buffer = deque(maxlen=capacity)

    def emit(self, record):
        self.buffer.append(self.format(record))

    def get_logs(self, limit=None):
        if limit:
            return list(self.buffer)[-limit:]
        return list(self.buffer)

LOGGING_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_log_buffer_handler = InMemoryLogHandler()
_log_buffer_handler.setFormatter(logging.Formatter(LOGGING_FORMAT))

def get_logger():
    # Configure the logging system
    logging.basicConfig(
        level=logging.INFO,  # Change to DEBUG for more detailed logs
        format=LOGGING_FORMAT,
        handlers=[
            logging.StreamHandler(sys.stdout),  # <-- Critical for EB log capture
            _log_buffer_handler
        ]
    )
    return logging.getLogger(__name__)

def get_log_buffer():
    return _log_buffer_handler
