from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from keepon.agent import ModelReply
from keepon.application import ACCEPTANCE_ACCESS, Application, RpcFault
from keepon.config import Settings
from keepon.util import dumps, now


@pytest.fixture
async def app(tmp_path, monkeypatch):
    for name in (
        "KEEPON_API_KEY",
        "KEEPON_OPENAI_API_KEY",
        "KEEPON_QWEN_API_KEY",
        "KEEPON_DEEPSEEK_API_KEY",
        "KEEPON_ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace", user_skills_dir=tmp_path / "user-skills", api_key="")
    application = Application(settings)
    await application.start()
    yield application
    await application.close()


def test_rpc_contract_contains_expected_methods(app):
    expected = {
        "approval.approve",
        "approval.deny",
        "approval.get",
        "approval.list",
        "artifact.get",
        "artifact.list",
        "automation.cancel",
        "automation.create",
        "automation.get",
        "automation.list",
        "automation.pause",
        "automation.resume",
        "computer.latest_observation",
        "computer.request_permission",
        "computer.status",
        "conversation.create",
        "conversation.delete",
        "conversation.get",
        "conversation.search",
        "conversation.list",
        "conversation.rename",
        "conversation.send",
        "extension.import.apply",
        "extension.import.preview",
        "extension.list",
        "mcp.add",
        "mcp.delete",
        "mcp.set_enabled",
        "memory.list",
        "memory.propose",
        "memory.revise",
        "memory.approve",
        "memory.quarantine",
        "memory.history",
        "wiki.list",
        "wiki.get",
        "wiki.propose",
        "wiki.approve",
        "wiki.quarantine",
        "wiki.history",
        "skill_candidate.review",
        "skill_candidate.quarantine",
        "skill.history",
        "skill.restore",
        "model_settings.get",
        "model_settings.test",
        "model_settings.update",
        "run.cancel",
        "run.get",
        "run.interrupt",
        "run.list",
        "run.recover",
        "skill.delete",
        "skill.install",
        "skill.set_enabled",
        "system.info",
        "system.restart",
        "task.get",
        "task.list",
        "task.plan_accept",
        "acceptance.get",
        "acceptance.review",
        "acceptance.retry",
        "task.plan_reject",
        "task.spec_revise",
        "trace.get",
    }
    assert set(app.methods()) == expected


async def acceptance_task(app, *checks):
    conversation = (await app.conversation_create({}))["conversation"]
    task = await app.task_service.create(title="Weekly report", goal="Publish the weekly report", conversation_id=conversation["id"])
    return (await app.task_spec_revise({
        "task_id": task["id"], "expected_spec_revision": 1, "acceptance_checks": list(checks), "_access": ACCEPTANCE_ACCESS,
    }))["task"]


async def start_acceptance_run(app, task):
    return await app._new_run(
        task["owner_conversation_id"], "Generate the report", "normal",
        acceptance_task_id=task["id"], expected_spec_revision=task["spec_revision"],
    )


async def finish_acceptance_run(app, run):
    run.update(status="completed", updated_at=now(), completed_at=now(), stop_reason="completed")
    await app._save_run(run)


async def wait_acceptance(app, run, status):
    async with asyncio.timeout(5):
        while (report := await app.acceptance.get(run["id"]))["status"] != status:
            await asyncio.sleep(0.02)
    return report


async def test_acceptance_requires_this_run_artifact_and_reports_json_path(app):
    task = await acceptance_task(
        app,
        {"id": "exists", "label": "Workspace file exists", "kind": "file_exists", "config": {"path": "report.json"}},
        {"id": "json", "label": "JSON type", "kind": "json_schema", "config": {
            "filename": "report.json", "schema": {"type": "object", "properties": {"total": {"type": "number"}}, "required": ["total"]},
        }},
        {"id": "published", "label": "Published", "kind": "artifact_published", "config": {"kind": "file", "identifier": "report.json"}},
    )
    path = app.settings.workspace_dir / "report.json"
    path.write_text('{"total": "12"}', encoding="utf-8")
    run = await start_acceptance_run(app, task)
    with pytest.raises(RpcFault, match="already active"):
        await start_acceptance_run(app, task)
    artifact = await app.publish_artifact(path=path, title="Report", description=None, run_id=run["id"])
    await finish_acceptance_run(app, run)
    report = await wait_acceptance(app, run, "needs_changes")
    result = report["attempts"][-1]["results"][1]
    assert result["evidence"]["artifact_id"] == artifact["id"]
    assert result["evidence"]["field_path"] == "/total"
    assert (await app.task_service.get(task["id"]))["status"] == "active"
    second_run = await start_acceptance_run(app, task)
    await finish_acceptance_run(app, second_run)
    second = await wait_acceptance(app, second_run, "needs_changes")
    assert [result["code"] for result in second["attempts"][-1]["results"]] == ["file_exists", "artifact_missing", "artifact_missing"]
    assert not (await app.acceptance.get(run["id"]))["current"]

    third_run = await start_acceptance_run(app, task)
    path.write_text('{"total": 12}', encoding="utf-8")
    await app.publish_artifact(path=path, title="Report", description=None, run_id=third_run["id"])
    path.write_text('{"total": "changed workspace copy"}', encoding="utf-8")
    await finish_acceptance_run(app, third_run)
    await wait_acceptance(app, third_run, "passed")
    assert (await app.task_service.get(task["id"]))["status"] == "completed"


async def test_acceptance_manual_decision_is_protected_and_idempotent(app, monkeypatch):
    async def fake_config():
        return {"api_key": "test"}

    async def fake_complete(*args, **kwargs):
        return ModelReply(content="Report ready", reasoning=None, tool_calls=[], usage={})

    monkeypatch.setattr(app, "_active_model_config", fake_config)
    monkeypatch.setattr("keepon.application.ModelClient.complete", fake_complete)
    task = await acceptance_task(app, {"id": "read", "label": "Read the report", "kind": "manual"})
    cid = task["owner_conversation_id"]
    params = {
        "conversation_id": cid, "content": "Please generate the report", "acceptance_task_id": task["id"],
        "expected_spec_revision": task["spec_revision"],
    }
    with pytest.raises(RpcFault, match="authorized desktop"):
        await app.conversation_send(params)
    with pytest.raises(ValueError, match="delivery acceptance"):
        await app.task_service.update(task["id"], {"status": "completed"})
    response = await app.conversation_send({**params, "_access": ACCEPTANCE_ACCESS})
    report = await wait_acceptance(app, response["run"], "awaiting_review")
    review = {"run_id": report["run_id"], "criterion_id": "read", "decision": "pass", "expected_report_version": report["version"]}
    with pytest.raises(RpcFault, match="authorized desktop"):
        await app.acceptance_review(review)
    review["_access"] = ACCEPTANCE_ACCESS
    results = await asyncio.gather(app.acceptance_review(review), app.acceptance_review(review))
    assert all(result["report"]["status"] == "passed" for result in results)
    finished = await app.task_service.get(task["id"])
    assert finished["status"] == "completed"
    assert finished["spec_bindings"][report["run_id"]] == task["spec_revision"]
    with pytest.raises(ValueError, match="delivery acceptance"):
        await app.task_service.update(task["id"], {"status": "active"})


async def test_unconfigured_model_cannot_close_an_acceptance_run(app):
    task = await acceptance_task(app, {"id": "file", "label": "File exists", "kind": "file_exists", "config": {"path": "ready.txt"}})
    (app.settings.workspace_dir / "ready.txt").touch()
    with pytest.raises(RpcFault) as error:
        await app.conversation_send({
            "conversation_id": task["owner_conversation_id"], "content": "Generate the report",
            "acceptance_task_id": task["id"], "expected_spec_revision": task["spec_revision"], "_access": ACCEPTANCE_ACCESS,
        })
    run_id = error.value.data["run_id"]
    assert (await app.acceptance.get(run_id))["status"] == "not_evaluated"
    assert (await app.task_service.get(task["id"]))["status"] == "active"


async def test_acceptance_technical_retry_and_manual_rejection(app, monkeypatch):
    task = await acceptance_task(
        app,
        {"id": "file", "label": "File exists", "kind": "file_exists", "config": {"path": "ready.txt"}},
        {"id": "read", "label": "Read the report", "kind": "manual"},
    )
    (app.settings.workspace_dir / "ready.txt").touch()
    run = await start_acceptance_run(app, task)

    def inaccessible(*args):
        raise PermissionError("unreadable test file")

    with monkeypatch.context() as context:
        context.setattr(app.acceptance, "_evaluate_one", inaccessible)
        await finish_acceptance_run(app, run)
        first = await wait_acceptance(app, run, "check_error")

    await app.acceptance_retry({"run_id": run["id"], "expected_report_version": first["version"], "_access": ACCEPTANCE_ACCESS})
    report = await wait_acceptance(app, run, "awaiting_review")
    assert len(report["attempts"]) == 2
    params = {
        "run_id": run["id"], "criterion_id": "read", "decision": "reject", "expected_report_version": report["version"],
        "_access": ACCEPTANCE_ACCESS,
    }
    with pytest.raises(RpcFault, match="reason is required"):
        await app.acceptance_review(params)
    rejected = (await app.acceptance_review({**params, "note": "Missing explanation"}))["report"]
    assert rejected["status"] == "needs_changes"
    assert rejected["reviews"]["read"]["note"] == "Missing explanation"


async def test_old_acceptance_report_is_read_only_after_spec_revision(app):
    task = await acceptance_task(app, {"id": "read", "label": "Read the report", "kind": "manual"})
    run = await start_acceptance_run(app, task)
    updated = (await app.task_spec_revise({
        "task_id": task["id"], "goal": "A revised goal", "expected_spec_revision": task["spec_revision"], "_access": ACCEPTANCE_ACCESS,
    }))["task"]
    await finish_acceptance_run(app, run)
    old = await wait_acceptance(app, run, "awaiting_review")
    assert old["spec_revision"] == task["spec_revision"]
    assert old["current_spec_revision"] == updated["spec_revision"]
    with pytest.raises(RpcFault, match="no longer open"):
        await app.acceptance_review({
            "run_id": run["id"], "criterion_id": "read", "decision": "pass", "expected_report_version": old["version"],
            "_access": ACCEPTANCE_ACCESS,
        })
    assert (await app.task_service.get(task["id"]))["status"] == "active"


async def test_acceptance_restores_check_without_replaying_run(app):
    task = await acceptance_task(app, {"id": "file", "label": "File exists", "kind": "file_exists", "config": {"path": "ready.txt"}})
    path = app.settings.workspace_dir / "ready.txt"
    path.touch()
    run = await start_acceptance_run(app, task)
    app.acceptance_task.cancel()
    await asyncio.gather(app.acceptance_task, return_exceptions=True)
    await finish_acceptance_run(app, run)
    await app.db.execute("UPDATE task_run_acceptance SET status='checking' WHERE run_id=?", (run["id"],))
    await app.acceptance.restore()
    assert await app.acceptance.process_next() == run["id"]
    assert (await app.acceptance.get(run["id"]))["status"] == "passed"
    assert (await app.run_get({"run_id": run["id"]}))["run"]["status"] == "completed"
    assert await app.acceptance.process_next() is None


async def test_conversation_and_offline_run(app):
    conversation = (await app.conversation_create({}))["conversation"]
    response = await app.conversation_send({"conversation_id": conversation["id"], "content": "你好", "mode": "normal"})
    assert response["run"]["status"] == "completed"
    assert "模型尚未配置" in response["content"]
    detail = await app.conversation_get({"conversation_id": conversation["id"]})
    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"]


async def test_first_message_sets_conversation_title_once(app):
    events = []

    async def capture(method, params):
        events.append((method, params))

    app.broadcast = capture
    conversation = (await app.conversation_create({}))["conversation"]
    cid = conversation["id"]
    assert conversation["title"] == "新对话"

    await app.conversation_send({"conversation_id": cid, "content": "  \n  帮我整理这个项目的运行方法和配置步骤  \n其他说明", "mode": "normal"})
    title = (await app.conversation_get({"conversation_id": cid}))["conversation"]["title"]
    assert title == "帮我整理这个项目的运行方法和配置步骤"
    assert ("conversation.updated", {"conversation_id": cid, "title": title}) in events

    await app.conversation_send({"conversation_id": cid, "content": "再补充测试命令", "mode": "normal"})
    assert (await app.conversation_get({"conversation_id": cid}))["conversation"]["title"] == title
    assert len([method for method, _ in events if method == "conversation.updated"]) == 1

    custom = (await app.conversation_create({"title": "我起的标题"}))["conversation"]
    await app.conversation_send({"conversation_id": custom["id"], "content": "不要覆盖标题", "mode": "normal"})
    assert (await app.conversation_get({"conversation_id": custom["id"]}))["conversation"]["title"] == "我起的标题"


async def test_session_search_finds_other_conversations_and_tracks_deletion(app):
    first = (await app.conversation_create({"title": "First"}))["conversation"]
    second = (await app.conversation_create({"title": "Second"}))["conversation"]
    await app._add_message(first["id"], {"role": "user", "content": "青色星球的季度报告"})
    await app._add_message(second["id"], {"role": "user", "content": "无关的消息"})
    result = await app.conversation_search({"query": "星球"})
    assert [(item["conversation_id"], item["title"]) for item in result["matches"]] == [(first["id"], "First")]
    assert len((await app.conversation_search({"query": "青色星球"}))["matches"]) == 1
    await app.conversation_delete({"conversation_id": first["id"]})
    assert (await app.conversation_search({"query": "星球"}))["matches"] == []


async def test_model_requests_are_durable_and_tool_activation_survives_checkpoint(app, monkeypatch):
    conversation = (await app.conversation_create({}))["conversation"]
    await app._add_message(conversation["id"], {"role": "user", "content": "Find memory tools"})
    run = await app._new_run(conversation["id"], "Find memory tools", "normal")
    calls = []

    async def fake_complete(_self, messages, extra_tools, base_tools, **_kwargs):
        calls.append((deepcopy(messages), [*base_tools, *extra_tools]))
        if len(calls) == 1:
            return ModelReply(content="", reasoning=None, tool_calls=[{
                "id": "find-tools", "name": "tool_catalog", "arguments": {"query": "memory"},
            }], usage={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110})
        return ModelReply(content="Found tools", reasoning=None, tool_calls=[], usage={
            "input_tokens": 120, "output_tokens": 10, "total_tokens": 130,
        })

    async def no_post_run(*_args):
        return None

    monkeypatch.setattr("keepon.application.ModelClient.complete", fake_complete)
    monkeypatch.setattr(app, "_post_run", no_post_run)
    await app._execute_run(run)
    checkpoint = await app.checkpoints.get(run["id"])
    assert "memory_read" in checkpoint["state"]["activated_tools"]
    assert "memory_read" in {tool["function"]["name"] for tool in calls[1][1]}
    trace = await app.trace_get({"run_id": run["id"], "include_model_requests": True})
    requests = [event for event in trace["events"] if event["type"] == "model_request"]
    assert len(requests) == 2
    assert requests[1]["request"]["messages"] == calls[1][0]


async def test_model_test_never_sends_saved_key_to_custom_endpoint(app, monkeypatch):
    async def unexpected_complete(*_args, **_kwargs):
        pytest.fail("custom endpoint received saved key")
    monkeypatch.setattr("keepon.application.ModelClient.complete", unexpected_complete)
    monkeypatch.setattr(app.secrets, "get", lambda provider: "saved-secret")
    blocked = await app.model_settings_test({
        "provider": "qwen", "model": "qwen-test", "base_url": "https://api.example.invalid/v1",
    })
    assert blocked["success"] is False
    assert "官方 HTTPS" in blocked["error"]
    blocked_http = await app.model_settings_test({
        "provider": "qwen", "model": "qwen-test", "base_url": "http://dashscope.aliyuncs.com/compatible-mode/v1",
    })
    assert blocked_http["success"] is False


async def test_skill_scopes_are_isolated_and_project_name_wins(app):
    user = (await app.skill_install({"name": "report", "scope": "user", "description": "User", "instructions": "User instructions"}))["skill"]
    project = (await app.skill_install({
        "name": "report", "scope": "project", "description": "Project", "instructions": "Project instructions",
    }))["skill"]
    assert user["location"].startswith(str(app.settings.user_skills_dir))
    assert project["location"].startswith(str(app.settings.data_dir))
    catalog = app._skill_catalog(await app._skills())
    assert "report (project): Project" in catalog
    assert "report (user)" not in catalog
    conversation = (await app.conversation_create({}))['conversation']
    run = await app._new_run(conversation['id'], "Read", "normal")
    result = await app.tool_call("skill_read", {"name": "report"}, run_id=run["id"], mode="normal")
    assert "Project instructions" in result["content"]


async def test_trace_reports_each_model_call_once_and_keeps_auxiliary_usage(app):
    conversation = (await app.conversation_create({}))["conversation"]
    run = await app._new_run(conversation["id"], "Inspect usage", "normal")
    await app._event(run, "assistant_message", step=1, message={"role": "assistant", "content": "one"}, usage={
        "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
        "cached_input_tokens": 60, "uncached_input_tokens": 40, "cache_read_input_tokens": 60,
    })
    await app._event(run, "assistant_message", step=2, message={"role": "assistant", "content": "two"}, usage={
        "input_tokens": 80, "output_tokens": 10, "total_tokens": 90,
    })
    await app._event(run, "agent_completed", usage={"input_tokens": 180, "output_tokens": 30, "total_tokens": 210})
    await app._event(run, "auxiliary_model_completed", role="memory_reflection", usage={
        "input_tokens": 30, "output_tokens": 5, "total_tokens": 35,
    })
    report = await app.trace_get({"run_id": run["id"]})
    assert report["trace"]["total_tokens"] == 210
    assert report["usage_summary"]["main_agent"]["model_calls"] == 2
    assert report["usage_summary"]["main_agent"]["cached_input_tokens"] is None
    assert report["usage_summary"]["memory_reflection"]["total_tokens"] == 35
    assert report["usage_summary"]["provider_total"]["total_tokens"] == 245


async def test_recovered_run_budget_includes_inherited_usage_without_double_counting_model_usage(app):
    conversation = (await app.conversation_create({}))["conversation"]
    run = await app._new_run(conversation["id"], "Resume", "normal")
    await app._event(run, "run_recovered", step=3, inherited_usage={
        "input_tokens": 500, "output_tokens": 100, "total_tokens": 600,
    }, inherited_model_calls=2)
    await app._event(run, "assistant_message", step=3, usage={
        "input_tokens": 50, "output_tokens": 20, "total_tokens": 70,
    })
    report = await app.trace_get({"run_id": run["id"]})
    assert report["usage_summary"]["main_agent"]["total_tokens"] == 70
    assert report["usage_summary"]["budget"]["run_budget_chargeable_tokens"] == 670
    assert report["usage_summary"]["budget"]["run_budget_model_calls"] == 3


async def test_artifact_list_filters_run_before_limit(app):
    target = {"id": "target", "run_id": "older-run", "conversation_id": "conversation", "title": "Wanted"}
    await app.db.execute("INSERT INTO artifacts VALUES(?,?,?)", (target["id"], dumps(target), "2020-01-01T00:00:00Z"))
    for index in range(3):
        item = {"id": f"other-{index}", "run_id": "newer-run", "title": "Other"}
        await app.db.execute("INSERT INTO artifacts VALUES(?,?,?)", (item["id"], dumps(item), now()))
    result = await app.artifact_list({"run_id": "older-run", "limit": 1})
    assert [item["id"] for item in result["artifacts"]] == ["target"]


async def test_data_is_created_only_under_new_namespace(app, tmp_path):
    await app.conversation_create({})
    assert app.settings.database.is_file()
    assert ".keepon" not in str(tmp_path) or app.settings.data_dir == tmp_path / "data"
    assert not (tmp_path / ".vesta").exists()


async def test_automation_lifecycle(app):
    item = (await app.automation_create({"title": "Daily", "prompt": "Summarize", "kind": "interval", "interval_seconds": 3600}))["automation"]
    assert item["status"] == "active"
    assert (await app.automation_pause({"automation_id": item["id"]}))["automation"]["status"] == "paused"
    assert (await app.automation_resume({"automation_id": item["id"]}))["automation"]["status"] == "active"
    assert (await app.automation_cancel({"automation_id": item["id"]}))["automation"]["status"] == "cancelled"


async def test_automation_occurrence_is_atomic_and_unique(app):
    item = (await app.automation_create({
        "title": "Once", "prompt": "Run once", "kind": "once", "run_at": "2020-01-01T00:00:00Z",
    }))["automation"]
    first = await app._claim_automation_occurrence(item["id"])
    assert first is not None
    assert await app._claim_automation_occurrence(item["id"]) is None
    assert (await app.automation_get({"automation_id": item["id"]}))["automation"]["status"] == "completed"
    messages = await app.db.fetchall("SELECT * FROM messages WHERE conversation_id=?", (first["conversation_id"],))
    occurrences = await app.db.fetchall("SELECT * FROM automation_occurrences WHERE automation_id=?", (item["id"],))
    assert len(messages) == len(occurrences) == 1
    assert occurrences[0]["run_id"] == first["id"]
    with pytest.raises(RpcFault, match="Cannot change automation"):
        await app.automation_resume({"automation_id": item["id"]})


async def test_automation_transaction_rolls_back_before_run_creation(app, monkeypatch):
    item = (await app.automation_create({
        "title": "Repeat", "prompt": "Run", "kind": "interval", "interval_seconds": 60,
    }))["automation"]
    item["next_run_at"] = "2020-01-01T00:00:00Z"
    await app.db.execute(
        "UPDATE automations SET data_json=?,next_run_at=? WHERE id=?",
        (dumps(item), item["next_run_at"], item["id"]),
    )
    original = app._next_schedule
    monkeypatch.setattr(app, "_next_schedule", lambda *_: (_ for _ in ()).throw(RuntimeError("injected crash")))
    with pytest.raises(RuntimeError, match="injected crash"):
        await app._claim_automation_occurrence(item["id"])
    assert await app.db.fetchall("SELECT * FROM runs WHERE source_id=?", (item["id"],)) == []
    assert await app.db.fetchall("SELECT * FROM automation_occurrences WHERE automation_id=?", (item["id"],)) == []
    monkeypatch.setattr(app, "_next_schedule", original)
    assert await app._claim_automation_occurrence(item["id"]) is not None


async def test_automation_restart_resumes_claimed_run_without_duplicate_occurrence(app, monkeypatch):
    app.automation_task.cancel()
    await asyncio.gather(app.automation_task, return_exceptions=True)
    item = (await app.automation_create({
        "title": "Restart once", "prompt": "Resume after crash", "kind": "once",
        "run_at": "2020-01-01T00:00:00Z",
    }))["automation"]
    claimed = await app._claim_automation_occurrence(item["id"])
    assert claimed is not None

    restarted = Application(app.settings)
    await restarted.db.initialize()
    executed = []

    async def record_run(run):
        executed.append(run["id"])

    monkeypatch.setattr(restarted, "_execute_run", record_run)
    await restarted._restore_automation_runs()
    await asyncio.sleep(0)
    assert executed == [claimed["id"]]
    assert await restarted._claim_automation_occurrence(item["id"]) is None
    assert len(await restarted.db.fetchall(
        "SELECT * FROM automation_occurrences WHERE automation_id=?", (item["id"],)
    )) == 1


async def test_automation_restart_interrupts_started_run_without_replay(app, monkeypatch):
    app.automation_task.cancel()
    await asyncio.gather(app.automation_task, return_exceptions=True)
    item = (await app.automation_create({
        "title": "Started once", "prompt": "Do not replay", "kind": "once",
        "run_at": "2020-01-01T00:00:00Z",
    }))["automation"]
    claimed = await app._claim_automation_occurrence(item["id"])
    assert claimed is not None
    await app.db.execute("UPDATE runs SET status='running' WHERE id=?", (claimed["id"],))
    restarted = Application(app.settings)
    await restarted.db.initialize()
    executed = []

    async def record_run(run):
        executed.append(run["id"])

    monkeypatch.setattr(restarted, "_execute_run", record_run)
    await restarted._restore_automation_runs()
    assert executed == []
    row = await restarted.db.fetchone("SELECT status,stop_reason FROM runs WHERE id=?", (claimed["id"],))
    assert (row["status"], row["stop_reason"]) == ("interrupted", "host_restart")
    assert await restarted._claim_automation_occurrence(item["id"]) is None


async def test_approval_is_single_use(app):
    waiter = asyncio.create_task(app._request_approval("run", "call", {"command": "echo hi"}, "test"))
    approvals = []
    for _ in range(50):
        approvals = (await app.approval_list({"status": "pending"}))["approvals"]
        if approvals:
            break
        await asyncio.sleep(0.01)
    approval = approvals[0]
    await app.approval_approve({"approval_id": approval["id"]})
    assert await waiter is True
    with pytest.raises(RpcFault) as error:
        await app.approval_deny({"approval_id": approval["id"]})
    assert error.value.code == -32001


async def test_skill_and_mcp_management(app):
    skill = (
        await app.skill_install(
            {
                "name": "writer",
                "description": "Writes",
                "instructions": "Write clearly",
                "scope": "project",
            }
        )
    )["skill"]
    assert skill["enabled"] is True
    disabled = (await app.skill_set_enabled({"name": "writer", "scope": "project", "enabled": False}))["skill"]
    assert disabled["enabled"] is False
    mcp = await app.mcp_add({"name": "files", "command": "node", "args": ["server.js"], "env": {}, "enabled": True})
    assert mcp["restart_required"] is True
    overview = await app.extension_list({})
    assert overview["skills"][0]["name"] == "writer"
    assert overview["mcp"]["servers"][0]["name"] == "files"


async def test_reviewed_wiki_and_memory_are_the_only_new_context_entries(app):
    checks = {key: True for key in ("relevance", "source", "applicability", "sensitive_information", "prompt_injection")}
    wiki = (await app.wiki_propose({
        "title": "API convention", "summary": "Use JSON-RPC", "content": "Host operations use JSON-RPC.",
        "source_type": "manual", "source_ref": "maintainer note", "applicability": "Host API",
    }))["entry"]
    memory = (await app.memory_propose({
        "title": "Language", "summary": "Chinese replies", "content": "Reply in Chinese.",
        "source_ref": "user preference", "applicability": "user",
    }))["memory"]
    assert await app.wiki_service.index() == ""
    assert await app.memory_service.index() == ""
    conversation = (await app.conversation_create({}))["conversation"]
    run = await app._new_run(conversation["id"], "Review knowledge", "normal")
    with pytest.raises(ValueError, match="not active"):
        await app.tool_call("wiki_read", {"entry_id": wiki["id"]}, run_id=run["id"], mode="normal")
    await app.wiki_approve({"entry_id": wiki["id"], "checks": checks})
    await app.memory_approve({"memory_id": memory["id"], "checks": checks})
    assert "API convention" in await app.wiki_service.index()
    assert "Language" in await app.memory_service.index()
    await app.wiki_quarantine({"entry_id": wiki["id"], "reason": "Outdated"})
    await app.memory_quarantine({"memory_id": memory["id"], "reason": "Wrong preference"})
    assert await app.wiki_service.index() == ""
    assert await app.memory_service.index() == ""


async def test_skill_versions_can_restore(app):
    first = (await app.skill_install({
        "name": "writer", "scope": "project", "description": "Write", "instructions": "Draft then verify the output.",
    }))["skill"]
    second = (await app.skill_install({
        "name": "writer", "scope": "project", "description": "Write", "instructions": "Draft, revise, then verify.",
    }))["skill"]
    assert (first["version"], second["version"]) == (1, 2)
    assert [version["version"] for version in (await app.skill_history({"name": "writer", "scope": "project"}))["versions"]] == [2, 1]
    restored = (await app.skill_restore({"name": "writer", "scope": "project", "version": 1}))["skill"]
    assert restored["version"] == 3
    conversation = (await app.conversation_create({}))["conversation"]
    run = await app._new_run(conversation["id"], "Read skill", "normal")
    assert "Draft then verify" in (await app.tool_call("skill_read", {"name": "writer"}, run_id=run["id"], mode="normal"))["content"]


async def test_quarantining_promoted_skill_disables_it(app):
    item = (await app.skill_install({
        "name": "writer", "scope": "project", "description": "Write", "instructions": "Draft and verify.",
    }))["skill"]
    candidate = {"id": "candidate-1", "status": "promoted", "promoted_skill": "writer", "name": "writer"}
    await app.db.execute(
        "INSERT INTO skill_candidates(id,status,data_json,created_at,updated_at) VALUES(?,?,?,?,?)",
        (candidate["id"], "promoted", dumps(candidate), now(), now()),
    )
    skills = await app._skills()
    skills[0]["candidate_id"] = candidate["id"]
    await app.db.set_json("skills", skills)
    await app.skill_candidate_quarantine({"candidate_id": candidate["id"], "reason": "Faulty steps"})
    updated = next(skill for skill in await app._skills() if skill["name"] == item["name"])
    assert updated["enabled"] is False
    with pytest.raises(RpcFault, match="Quarantined Skill cannot be enabled"):
        await app.skill_set_enabled({"name": "writer", "scope": "project", "enabled": True})
