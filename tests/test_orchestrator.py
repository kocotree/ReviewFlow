from __future__ import annotations

import pytest

from app.orchestrator import Orchestrator
from app.scoring_workflow import WorkflowOutcome
from app.task_registry import TaskRegistry


class StubWorkflow:
    def __init__(self) -> None:
        self.commands = []

    async def run(self, command):
        self.commands.append(command)
        return WorkflowOutcome.COMPLETED


@pytest.mark.asyncio
async def test_orchestrator_only_coordinates_workflow_command() -> None:
    workflow = StubWorkflow()
    registry = TaskRegistry()
    orchestrator = Orchestrator(workflow=workflow, registry=registry)

    outcome = await orchestrator.process_record("app", "table", "record")

    assert outcome is WorkflowOutcome.COMPLETED
    assert len(workflow.commands) == 1
    assert workflow.commands[0].key.record_id == "record"
