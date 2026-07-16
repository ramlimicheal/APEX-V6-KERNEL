"""Layer 3 - Distributed analysis hive, doing real work.

The original `layer3_dag_hive_agents.py` had "agents" that slept 0.3s and always
returned VERIFIED_SECURE. This version:

- Shards a real target directory into modules (one per subdirectory / file group).
- Each async worker runs the real `bandit` static analyzer on its shard via a
  subprocess and parses the JSON findings.
- A supervisor aggregates typed findings and FAILS when high-severity issues
  exceed a threshold or a worker crashes.

The good parts of the original (Semaphore rate limiting, gather with
return_exceptions) are kept - they were correctly written.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger("apex.layer3")


@dataclass
class Finding:
    severity: str          # LOW / MEDIUM / HIGH
    test_id: str
    filename: str
    line: int
    message: str


@dataclass
class ModuleResult:
    module: str
    findings: List[Finding] = field(default_factory=list)
    error: str = ""

    @property
    def crashed(self) -> bool:
        return bool(self.error)


@dataclass
class HiveReport:
    modules: List[ModuleResult]
    high_severity_count: int
    crashed_count: int
    passed: bool

    def all_findings(self) -> List[Finding]:
        return [f for m in self.modules for f in m.findings]


def shard_paths(target: Path, max_modules: int = 16) -> Dict[str, List[Path]]:
    """Group .py files under `target` into modules keyed by top-level folder."""
    target = Path(target)
    py_files = [p for p in target.rglob("*.py") if p.is_file()]
    modules: Dict[str, List[Path]] = {}
    for f in py_files:
        try:
            rel = f.relative_to(target)
        except ValueError:
            rel = f
        key = rel.parts[0] if len(rel.parts) > 1 else "root"
        modules.setdefault(key, []).append(f)
    # Bound the number of modules so a huge repo can't spawn unbounded workers.
    if len(modules) > max_modules:
        merged: Dict[str, List[Path]] = {}
        for i, (k, v) in enumerate(modules.items()):
            merged.setdefault(f"group_{i % max_modules}", []).extend(v)
        modules = merged
    return modules


class BanditHive:
    """Runs bandit across sharded modules concurrently."""

    def __init__(
        self,
        *,
        concurrency: int = 8,
        high_severity_budget: int = 0,
        bandit_cmd: Optional[Sequence[str]] = None,
    ) -> None:
        self.semaphore = asyncio.Semaphore(concurrency)
        self.high_severity_budget = high_severity_budget
        # Invoke bandit as a module so it works regardless of PATH / venv setup.
        self.bandit_cmd = list(bandit_cmd) if bandit_cmd else [sys.executable, "-m", "bandit"]

    async def _analyze_module(self, module: str, files: List[Path]) -> ModuleResult:
        async with self.semaphore:
            args = [*self.bandit_cmd, "-f", "json", *[str(f) for f in files]]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
            except FileNotFoundError as exc:
                return ModuleResult(module, error=f"bandit not found: {exc}")
            except Exception as exc:  # noqa: BLE001
                return ModuleResult(module, error=str(exc))

            # bandit exits non-zero when it finds issues; that's expected, so we
            # parse stdout regardless of exit code.
            try:
                data = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
            except json.JSONDecodeError as exc:
                return ModuleResult(module, error=f"could not parse bandit output: {exc}")

            findings = [
                Finding(
                    severity=r.get("issue_severity", "UNDEFINED"),
                    test_id=r.get("test_id", ""),
                    filename=r.get("filename", ""),
                    line=r.get("line_number", 0),
                    message=r.get("issue_text", ""),
                )
                for r in data.get("results", [])
            ]
            return ModuleResult(module, findings=findings)

    async def run(self, target: Path) -> HiveReport:
        modules = shard_paths(target)
        logger.info("[Layer3] sharded target into %d modules", len(modules))
        tasks = [self._analyze_module(name, files) for name, files in modules.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        module_results: List[ModuleResult] = []
        for res in results:
            if isinstance(res, Exception):
                module_results.append(ModuleResult("unknown", error=str(res)))
            else:
                module_results.append(res)

        high = sum(
            1 for m in module_results for f in m.findings if f.severity.upper() == "HIGH"
        )
        crashed = sum(1 for m in module_results if m.crashed)
        passed = crashed == 0 and high <= self.high_severity_budget

        logger.info(
            "[Layer3] high-severity=%d crashed=%d -> %s",
            high, crashed, "PASS" if passed else "FAIL",
        )
        return HiveReport(module_results, high, crashed, passed)
