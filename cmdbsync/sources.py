"""Inventory sources. AWS Config is implemented; Azure and GCP are stubs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Protocol

TYPE_RE = re.compile(r"^AWS::[A-Za-z0-9]+::[A-Za-z0-9]+$")


class SourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Record:
    account_id: str
    region: str
    resource_type: str
    resource_id: str
    name: str
    arn: str
    tags: tuple[tuple[str, str], ...]  # sorted, so equal tags hash equally

    @property
    def key(self) -> str:
        # Bare resource IDs are not unique across accounts, regions and types.
        return f"{self.account_id}/{self.region}/{self.resource_type}/{self.resource_id}"

    @property
    def content_hash(self) -> str:
        payload = json.dumps([self.name, self.arn, self.tags], separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


class Source(Protocol):
    def fetch(self) -> list[Record]: ...


class AwsConfigSource:
    """Reads current configuration items with a Config advanced query."""

    FIELDS = "resourceId, resourceType, resourceName, awsRegion, accountId, arn, tags"

    def __init__(self, client, resource_types: list[str]):
        if not resource_types:
            raise ValueError("at least one resource type is required")
        bad = [t for t in resource_types if not TYPE_RE.match(t)]
        if bad:
            # The types go into the query text, so only allow the plain AWS::X::Y shape.
            raise ValueError(f"invalid resource type(s): {bad}")
        self.client = client
        self.types = resource_types

    def expression(self) -> str:
        types = ", ".join(f"'{t}'" for t in self.types)
        return f"SELECT {self.FIELDS} WHERE resourceType IN ({types})"

    def fetch(self) -> list[Record]:
        kwargs = {"Expression": self.expression()}
        records: list[Record] = []
        while True:
            page = self.client.select_resource_config(**kwargs)
            for raw in page.get("Results", []):
                records.append(_to_record(raw))
            token = page.get("NextToken")
            if not token:
                return records
            kwargs["NextToken"] = token


def _to_record(raw: str) -> Record:
    try:
        item = json.loads(raw)
        tags = tuple(sorted((t["key"], t["value"]) for t in item.get("tags", [])))
        return Record(
            account_id=item["accountId"],
            region=item["awsRegion"],
            resource_type=item["resourceType"],
            resource_id=item["resourceId"],
            name=item.get("resourceName", ""),
            arn=item.get("arn", ""),
            tags=tags,
        )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SourceError(f"unusable Config result {raw[:120]!r}: {exc}") from exc


class AzureResourceGraphSource:
    """Interface placeholder. Not implemented."""

    def fetch(self) -> list[Record]:
        raise NotImplementedError("Azure Resource Graph is not implemented")


class GcpAssetInventorySource:
    """Interface placeholder. Not implemented."""

    def fetch(self) -> list[Record]:
        raise NotImplementedError("GCP Asset Inventory is not implemented")
