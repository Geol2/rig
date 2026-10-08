"""Dependency graph between hands."""

from __future__ import annotations


class CycleError(ValueError):
    pass


def layers(nodes: list[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    """Group nodes into layers; every node's upstreams are in earlier layers.

    Hands within a layer have no dependency on each other and can run in parallel.
    """
    upstream = {n: set() for n in nodes}
    for a, b in edges:
        upstream[b].add(a)

    done: set[str] = set()
    result: list[list[str]] = []
    while len(done) < len(nodes):
        ready = [n for n in nodes if n not in done and upstream[n] <= done]
        if not ready:
            stuck = sorted(set(nodes) - done)
            raise CycleError(f"lines form a cycle among {stuck}")
        result.append(ready)
        done.update(ready)
    return result


def upstreams(node: str, edges: list[tuple[str, str]]) -> list[str]:
    return [a for a, b in edges if b == node]
