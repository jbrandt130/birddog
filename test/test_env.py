import os
import unittest
from unittest.mock import patch

from birddog.env import env_table_name


class TestEnvTableName(unittest.TestCase):
    def test_stage_gets_suffix(self):
        with patch.dict(os.environ, {"BIRDDOG_NOCODB_ENV": "CLOUD_STAGE"}):
            self.assertEqual(env_table_name("bd_task_queue"), "bd_task_queue_STAGE")

    def test_prod_and_local_keep_original_names(self):
        for env in ("CLOUD_PROD", "LOCAL"):
            with self.subTest(env=env), patch.dict(os.environ, {"BIRDDOG_NOCODB_ENV": env}):
                self.assertEqual(env_table_name("bd_task_queue"), "bd_task_queue")

    def test_unset_env_keeps_original_name(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(env_table_name("bd_doc_tracker"), "bd_doc_tracker")

    def test_stage_names_stay_inside_iam_pattern(self):
        # the EB role's DynamoDB policy only covers "bd_*" / "birddog_*" tables
        with patch.dict(os.environ, {"BIRDDOG_NOCODB_ENV": "CLOUD_STAGE"}):
            for name in ("bd_task_queue", "bd_task_kv_store", "bd_db_update", "bd_loc_update",
                         "bd_tracker_kv_store", "bd_doc_tracker"):
                with self.subTest(name=name):
                    self.assertTrue(env_table_name(name).startswith("bd_"))

    def test_default_dynamodb_kv_table_follows_env(self):
        from birddog import store
        for env, expected in (("CLOUD_STAGE", "birddog_key_value_store_STAGE"),
                              ("CLOUD_PROD", "birddog_key_value_store")):
            with self.subTest(env=env), patch.dict(os.environ, {"BIRDDOG_NOCODB_ENV": env}), \
                 patch.object(store, "boto3"), \
                 patch.object(store.DynamoDBKeyValueStore, "_ensure_table_exists"):
                self.assertEqual(store.DynamoDBKeyValueStore()._table_name, expected)

    def test_task_manager_uses_env_tables(self):
        from birddog import task
        created = []
        with patch.dict(os.environ, {"BIRDDOG_NOCODB_ENV": "CLOUD_STAGE"}), \
             patch.object(task, "StringQueue", side_effect=lambda table_name: created.append(table_name)), \
             patch.object(task, "KeyValueStore", side_effect=lambda table_name: created.append(table_name)):
            task.TaskManager("EnvTableTest")
        self.assertEqual(created, ["bd_task_queue_STAGE", "bd_task_kv_store_STAGE"])


if __name__ == "__main__":
    unittest.main()
