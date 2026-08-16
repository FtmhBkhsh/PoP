import logging
import time

from Config import load_config, setup_logging
from PoUW import PoUW
from Scheduler import Scheduler
from Worker import Worker
from rpc_server import start_rpc_server

config = load_config()
setup_logging(config)

logger = logging.getLogger(__name__)

logger.debug("start")
worker = Worker()

start_rpc_server(
    worker,
    host=config.get("listen_host", "0.0.0.0"),
    port=config.get("listen_port", 5000),
)

print("DPF STARTED", flush=True)

while True:

    worker.sync_chain()

    start = time.perf_counter()

    task = worker.select_task()
    if task is None:
        logger.debug("No pending task available")
        time.sleep(1)
        continue

    worker.broadcast_processing(task)

    result = worker.execute_task(task)
    logger.debug(
        "result: task_id=%s records=%d",
        result["task_id"], result["processed_records"]
    )

    m = worker.measure_cpu(start, result["processed_records"])

    worker.send_result(result)
    worker.broadcast_completed(task, m)

    win, proof = PoUW.scheduler_election(m, worker.difficulty)

    if win:
        logger.info("[%s] Won PoUW election, becoming scheduler", worker.node_id)

        scheduler = Scheduler(worker)
        scheduler.collect_completed_tasks()
        scheduler.collect_pending_tasks()
        scheduler.dispatch_tasks()

        block = scheduler.create_block(proof)
        scheduler.broadcast_block(block)

        elapsed = time.perf_counter() - start
        print(f"[{worker.node_id}] scheduled block {block['index']} in {elapsed:.3f}s", flush=True)
