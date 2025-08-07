# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

"""
deployment environment sniffer
"""

import os

def detect_environment():
    if os.environ.get("BIRDDOG_AWS_ENVIRONMENT"):
        return "aws"
    return "local"
