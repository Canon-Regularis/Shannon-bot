"""The queue between accepting a webhook and acting on it.

`queue` is the table and its claims; `worker` is the loop that leases from it, dispatches, and
decides what a failure costs.
"""

from shannon.services.delivery.queue import Delivery, DeliveryQueue, WebhookDeliveryQueue
from shannon.services.delivery.worker import DeliveryWorker, ReadyCheck, WorkerSettings

__all__ = [
    "Delivery",
    "DeliveryQueue",
    "DeliveryWorker",
    "ReadyCheck",
    "WebhookDeliveryQueue",
    "WorkerSettings",
]
