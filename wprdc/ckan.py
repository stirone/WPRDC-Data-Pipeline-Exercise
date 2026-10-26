"""Thin CKAN action-API wrapper over LoggedHttp."""
from __future__ import annotations

from pathlib import Path

from wprdc import config
from wprdc.http_client import DownloadResult, JsonResponse, LoggedHttp


class Ckan:
    def __init__(self, http: LoggedHttp, base_url: str = config.CKAN_BASE_URL):
        self.http = http
        self.base_url = base_url.rstrip("/")

    def _action(self, action: str, request_key: str, params: dict) -> JsonResponse:
        return self.http.get_json(
            action, request_key, f"{self.base_url}/api/3/action/{action}", params)

    def package_show(self, package_id: str) -> dict:
        return self._action("package_show", f"package_show:{package_id}",
                            {"id": package_id}).payload["result"]

    def datastore_search(self, resource_id: str, *, limit: int, offset: int = 0,
                         sort: str | None = "_id") -> JsonResponse:
        """One page of records; the raw JsonResponse is returned so callers can land it unchanged."""
        params = {"resource_id": resource_id, "limit": limit, "offset": offset}
        if sort:
            params["sort"] = sort
        key = f"datastore_search:{resource_id}:offset={offset}:limit={limit}"
        return self._action("datastore_search", key, params)

    def datastore_total(self, resource_id: str) -> int:
        resp = self.datastore_search(resource_id, limit=0, sort=None)
        return int(resp.payload["result"]["total"])

    def dump(self, resource_id: str, dest: Path) -> DownloadResult:
        """Whole datastore table as CSV in a single request (/datastore/dump)."""
        return self.http.download(
            "datastore_dump", f"datastore_dump:{resource_id}",
            f"{self.base_url}/datastore/dump/{resource_id}", dest,
            expected_prefix=b"_id,",
        )
