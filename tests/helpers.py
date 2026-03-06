from __future__ import annotations

from mvp_orbit.integrations.object_store import ObjectStoreBackend, StoredObjectMeta


class InMemoryObjectStoreBackend(ObjectStoreBackend):
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str, str], bytes] = {}

    def put_bytes(self, namespace, object_id, payload, *, content_type, filename):
        key = (namespace.value, object_id, filename)
        self.objects[key] = payload
        return StoredObjectMeta(namespace=namespace, object_id=object_id, size=len(payload), storage_ref=filename)

    def get_bytes(self, namespace, object_id, *, filename):
        key = (namespace.value, object_id, filename)
        if key not in self.objects:
            raise FileNotFoundError(f"{namespace.value}:{object_id}")
        return self.objects[key]

    def exists(self, namespace, object_id, *, filename):
        return (namespace.value, object_id, filename) in self.objects

    def get_meta(self, namespace, object_id, *, filename):
        key = (namespace.value, object_id, filename)
        if key not in self.objects:
            raise FileNotFoundError(f"{namespace.value}:{object_id}")
        return StoredObjectMeta(namespace=namespace, object_id=object_id, size=len(self.objects[key]), storage_ref=filename)
