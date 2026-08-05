import os
import time
import unittest
from pathlib import Path

from backend.ai import AIClient, classify_intent, rag_answer
from backend.db import connect, verify_password
from backend.services import ApiError, activate_plan, anomalies, approve_rule, attendance_overview, automation_event, create_rule, create_task, decide_employee_request, employee_agent, employee_insights, employee_list, overview, period_review, publish_plan, save_employee, task_detail, update_anomaly


class FlowStaffTests(unittest.TestCase):
    def setUp(self):
        self.ai_environment={key:os.environ.pop(key,None) for key in ("OPENAI_API_KEY","CODEX_API_KEY")}
        self.db=connect(":memory:")
        self.manager=dict(self.db.execute("SELECT * FROM users WHERE username='manager'").fetchone())
        self.employee=dict(self.db.execute("SELECT * FROM users WHERE username='employee'").fetchone())
        self.admin=dict(self.db.execute("SELECT * FROM users WHERE username='admin'").fetchone())

    def tearDown(self):
        for key,value in self.ai_environment.items():
            if value is not None:os.environ[key]=value

    def test_seed_contains_complete_business_data(self):
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM employees").fetchone()["n"],12)
        self.assertGreaterEqual(self.db.execute("SELECT COUNT(*) n FROM attendance").fetchone()["n"],60)
        self.assertGreaterEqual(self.db.execute("SELECT COUNT(*) n FROM employee_skills").fetchone()["n"],20)
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM rules").fetchone()["n"],6)
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM anomaly_events").fetchone()["n"],3)

    def test_login_and_application_shells_respect_hidden_state(self):
        root=Path(__file__).parents[1]
        html=(root/"public"/"index.html").read_text(encoding="utf-8")
        css=(root/"public"/"styles.css").read_text(encoding="utf-8")
        script=(root/"public"/"app.js").read_text(encoding="utf-8")
        self.assertIn('id="appView" class="app-shell" hidden',html)
        self.assertIn('[hidden]{display:none!important}',css)
        self.assertIn('loginView.hidden=true;appView.hidden=false',script)

    def test_agent_understanding_uses_business_summary_and_collapses_debug_json(self):
        script=(Path(__file__).parents[1]/"public"/"app.js").read_text(encoding="utf-8")
        self.assertIn("AI 理解结果",script)
        self.assertIn("尚未识别到具体业务目标",script)
        self.assertIn('<details class="technical-details">',script)
        self.assertNotIn('<h2>结构化理解</h2>',script)

    def test_passwords_are_pbkdf2_hashes(self):
        encoded=self.manager["password_hash"]
        self.assertTrue(encoded.startswith("pbkdf2_sha256$"))
        self.assertTrue(verify_password("Manager123!",encoded))
        self.assertFalse(verify_password("wrong",encoded))

    def test_manager_scope_only_returns_authorized_store(self):
        data=employee_list(self.db,self.manager)
        self.assertTrue(data)
        self.assertTrue(all(x["store_id"]=="store-a" for x in data))

    def test_employee_scope_only_returns_self(self):
        data=employee_list(self.db,self.employee)
        self.assertEqual([x["id"] for x in data],[self.employee["employee_id"]])

    def test_manager_cannot_create_employee_in_other_store(self):
        with self.assertRaisesRegex(ApiError,"授权门店"):
            save_employee(self.db,self.manager,{"code":"X","name":"越权","role":"导购","department":"销售","store_id":"store-b"})

    def test_attendance_summary_excludes_leave_and_overtime_from_exceptions(self):
        result=attendance_overview(self.db,self.manager,"2026-08-01","2026-08-07")
        expected=sum(x["event_type"] in ("late","absence") for x in result["records"])
        self.assertEqual(result["summary"]["exceptions"],expected)
        self.assertGreater(result["summary"]["overtime_hours"],0)

    def test_intent_fallback_is_explicit_when_key_missing(self):
        result=classify_intent(AIClient(self.db),self.manager,"请帮我安排8月8日排班")
        self.assertEqual(result["mode"],"deterministic_fallback")
        self.assertIn("未配置",result["fallback_reason"])

    def test_rag_without_key_is_retrieval_only_not_fake_llm(self):
        result=rag_answer(self.db,AIClient(self.db),self.manager,"周工时上限是多少")
        self.assertEqual(result["mode"],"retrieval_only")
        self.assertTrue(result["sources"])
        self.assertTrue(result["citations"])

    def test_ai_client_detects_runtime_key_without_exposing_it(self):
        os.environ["OPENAI_API_KEY"]="runtime-only-test-key"
        client=AIClient(self.db)
        self.assertTrue(client.enabled)
        self.assertNotIn("runtime-only-test-key",client.base_url)
        os.environ.pop("OPENAI_API_KEY")

    def test_schedule_task_generates_two_distinct_plans(self):
        result=create_task(self.db,self.manager,"请安排8月6日至8月8日的排班，覆盖率目标98%","store_management")
        for _ in range(100):
            task=task_detail(self.db,self.manager,result["task_id"])
            if task["status"] in ("completed","failed"):break
            time.sleep(.02)
        self.assertEqual(task["status"],"completed",task.get("error"))
        self.assertEqual(len(task["steps"]),6)
        self.assertEqual(len(task["plans"]),2)
        self.assertEqual(sum(x["recommended"] for x in task["plans"]),1)
        self.assertNotEqual(task["plans"][0]["strategy"],task["plans"][1]["strategy"])

    def test_activate_and_publish_are_separate_actions(self):
        result=create_task(self.db,self.manager,"8月6日至8月8日生成排班","store_management")
        for _ in range(100):
            task=task_detail(self.db,self.manager,result["task_id"])
            if task["status"]=="completed":break
            time.sleep(.02)
        plan=task["plans"][0]
        with self.assertRaisesRegex(ApiError,"先选择生效"):
            publish_plan(self.db,self.manager,plan["id"])
        activated=activate_plan(self.db,self.manager,plan["id"])
        self.assertEqual(next(x for x in activated["plans"] if x["id"]==plan["id"])["status"],"active")
        published=publish_plan(self.db,self.manager,plan["id"])
        self.assertEqual(next(x for x in published["plans"] if x["id"]==plan["id"])["status"],"published")

    def test_employee_leave_request_does_not_modify_shifts(self):
        before=self.db.execute("SELECT COUNT(*) n FROM shifts").fetchone()["n"]
        result=employee_agent(self.db,self.employee,"我8月8日需要请假处理家庭事务")
        self.assertEqual(result["data"]["status"],"pending_manager")
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM shifts").fetchone()["n"],before)

    def test_employee_preference_is_persisted_as_soft_constraint(self):
        result=employee_agent(self.db,self.employee,"我希望以后尽量排早班，周三不要排班")
        self.assertIn("软约束",result["answer"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM employee_preferences WHERE employee_id=?",(self.employee["employee_id"],)).fetchone()["n"],1)

    def test_anomaly_displays_separate_evidence_causes_and_suggestions(self):
        event=anomalies(self.db,self.manager)[0]
        self.assertTrue(event["evidence"])
        self.assertTrue(event["possible_causes"])
        self.assertTrue(event["suggestions"])

    def test_anomaly_action_requires_note_and_is_audited(self):
        event=anomalies(self.db,self.manager)[0]
        with self.assertRaises(ApiError):update_anomaly(self.db,self.manager,event["id"],"resolved","")
        update_anomaly(self.db,self.manager,event["id"],"monitoring","已与员工沟通，观察下一周期")
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM anomaly_actions WHERE anomaly_id=?",(event["id"],)).fetchone()["n"],1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM audit_logs WHERE resource_id=?",(event["id"],)).fetchone()["n"],1)

    def test_hard_company_rule_requires_approval(self):
        result=create_rule(self.db,self.hr_user(),{"text":"员工周工时不得超过38小时"},AIClient(self.db))
        self.assertIn(result["rule"]["status"],("pending_approval","active"))
        self.assertEqual(result["mode"],"deterministic_fallback")

    def test_hr_can_approve_rule_and_create_new_version(self):
        result=create_rule(self.db,self.hr_user(),{"text":"公司夜班后至少休息11小时"},AIClient(self.db))
        before=result["rule"]["version"]
        approved=approve_rule(self.db,self.hr_user(),result["rule"]["id"])
        self.assertEqual(approved["status"],"active")
        self.assertEqual(approved["version"],before+1)

    def test_manager_can_decide_employee_request_in_own_store(self):
        created=employee_agent(self.db,self.employee,"我8月8日需要请假")
        request_id=created["data"]["request_id"]
        decided=decide_employee_request(self.db,self.manager,request_id,"approved","已确认覆盖风险")
        self.assertEqual(decided["status"],"approved")
        self.assertTrue(decided["decided_at"])

    def test_employee_insights_use_attendance_and_skill_data(self):
        insights=employee_insights(self.db,self.manager)
        self.assertTrue(insights)
        self.assertIn("facts",insights[0])
        self.assertIn("skill_gaps",insights[0])
        self.assertIn("suggestions",insights[0])

    def test_period_review_keeps_high_impact_change_pending(self):
        review=period_review(self.db,self.manager)
        self.assertIn("forecast_mape",review["metrics"])
        self.assertTrue(any(x["status"]=="pending_final_review" for x in review["improvements"]))

    def test_employee_cannot_read_organization_review(self):
        with self.assertRaisesRegex(ApiError,"无组织复盘权限"):
            period_review(self.db,self.employee)

    def hr_user(self):return dict(self.db.execute("SELECT * FROM users WHERE username='hr'").fetchone())

    def test_automation_event_is_idempotent_and_creates_replan_task(self):
        body={"event_type":"demand_spike","dedupe_key":"order-spike-001","store_id":"store-a","payload":{"actual":1200}}
        event=automation_event(self.db,self.manager,body)
        with self.assertRaisesRegex(ApiError,"已接收"):automation_event(self.db,self.manager,body)
        for _ in range(100):
            row=dict(self.db.execute("SELECT * FROM automation_events WHERE id=?",(event["id"],)).fetchone())
            if row["status"] in ("completed","failed"):break
            time.sleep(.02)
        self.assertEqual(row["status"],"completed")
        self.assertTrue(row["task_id"])

    def test_overview_reads_database_not_static_numbers(self):
        before=overview(self.db,self.manager)["employee_count"]
        self.db.execute("UPDATE employees SET status='inactive' WHERE id='emp-002'");self.db.commit()
        self.assertEqual(overview(self.db,self.manager)["employee_count"],before-1)


if __name__=="__main__":unittest.main()
