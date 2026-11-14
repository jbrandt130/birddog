#
echo  "DROP TABLE IF EXISTS bd_task_kv_store; DROP TABLE IF EXISTS bd_db_update;" | sqlite3 .cache/key_value_store.db
echo "DROP TABLE IF EXISTS bd_task_queue;" | sqlite3 .cache/string_queues.db 
