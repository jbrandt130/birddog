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

# NocoDB base whose per-deployment DynamoDB tables carry a suffix. STAGE and PROD
# share one AWS account (and, today, one EB environment), so state that belongs
# to a base -- task queues/state and wiki-tracker watermarks -- would otherwise
# be shared: tasks queued against one base would run against the other after a
# repoint, and tracker watermarks advanced on STAGE would make PROD skip
# changes. PROD and LOCAL keep the original, unsuffixed table names. The suffix
# keeps names inside the EB role's "bd_*" IAM resource pattern.
_TABLE_SUFFIX_BY_NOCODB_ENV = {
    "CLOUD_STAGE": "_STAGE",
}

def env_table_name(name):
    """Return the KV/queue table name to use for the current NocoDB base
    (BIRDDOG_NOCODB_ENV), e.g. "bd_task_queue" -> "bd_task_queue_STAGE" on
    CLOUD_STAGE. Evaluated at call time so a notebook that sets the variable
    before constructing a store gets the matching table."""
    return name + _TABLE_SUFFIX_BY_NOCODB_ENV.get(os.environ.get("BIRDDOG_NOCODB_ENV", ""), "")
