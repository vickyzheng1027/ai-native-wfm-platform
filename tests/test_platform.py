import os
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from backend.ai import AIClient, classify_intent, rag_answer
from backend.db import connect, migrate, verify_password
from backend.services import ApiError, activate_plan, anomalies, approve_rule, attendance_overview, automation_event, business_month_period, confirm_employee_request, create_rule, create_task, decide_employee_request, employee_agent, employee_insights, employee_list, normalize_model_date, overview, parse_schedule_parameters, period_review, publish_plan, rule_list, save_employee, task_detail, update_anomaly, update_rule


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
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM employees").fetchone()["n"],32)
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM stores").fetchone()["n"],8)
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM job_positions").fetchone()["n"],10)
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM departments").fetchone()["n"],5)
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM skill_catalog").fetchone()["n"],10)
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

    def test_visible_navigation_and_business_statuses_are_localized(self):
        root=Path(__file__).parents[1]
        html=(root/"public"/"index.html").read_text(encoding="utf-8")
        script=(root/"public"/"app.js").read_text(encoding="utf-8")
        self.assertNotIn("INTELLIGENCE CENTER",html+script)
        self.assertNotIn("WELCOME BACK",html)
        self.assertIn("completed:'已完成'",script)
        self.assertIn("high:'高风险'",script)
        self.assertIn("demand_spike:'客流或订单突增'",script)

    def test_business_text_uses_readable_font_scale(self):
        css=(Path(__file__).parents[1]/"public"/"styles.css").read_text(encoding="utf-8")
        self.assertIn("table{font-size:13px}",css)
        self.assertIn(".badge{font-size:12px",css)
        self.assertIn(".form-field label{font-size:13px}",css)

    def test_agent_panels_have_persistent_accessible_collapse_controls(self):
        script=(Path(__file__).parents[1]/"public"/"app.js").read_text(encoding="utf-8")
        self.assertIn('data-toggle-panel="${panel}"',script)
        self.assertIn('aria-expanded="${!collapsed}"',script)
        self.assertIn("toggleCollapsiblePanel(toggle,state.collapsedPanels)",script)

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

    def test_employee_create_persists_profile_and_certified_skills(self):
        created=save_employee(self.db,self.manager,{"code":"SH099","name":"新增员工","role":"导购","department":"销售服务","store_id":"store-a","hourly_rate":38,"weekly_hour_limit":36,"preferences":{"ai_summary":"优先早班"},"skills":[{"skill":"销售","proficiency":3,"certified":True},{"skill":"顾客服务","proficiency":3,"certified":True}]})
        self.assertEqual(created["code"],"SH099")
        self.assertEqual(created["preferences"]["ai_summary"],"优先早班")
        self.assertEqual(created["skills"][0]["skill"],"销售")
        self.assertEqual(created["skills"][0]["certified"],1)

    def test_organization_expansion_migration_is_idempotent(self):
        migrate(self.db);migrate(self.db)
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM employees").fetchone()["n"],32)
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM stores").fetchone()["n"],8)
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM skill_catalog").fetchone()["n"],10)

    def test_employee_form_uses_database_backed_master_data_selects(self):
        root=Path(__file__).parents[1];script=(root/"public"/"app.js").read_text(encoding="utf-8");app=(root/"backend"/"app.py").read_text(encoding="utf-8")
        self.assertIn('id="employeeRole" name="role" required><option',script)
        self.assertIn('id="employeeDepartment" name="department" required><option',script)
        self.assertIn('id="employeeSkills" name="skills" class="multi-select" multiple',script)
        self.assertIn("formData.getAll('skills')",script)
        self.assertIn('"positions":[dict(x)',app)
        self.assertIn('"departments":[dict(x)',app)
        self.assertIn('"skills":[dict(x)',app)

    def test_employee_page_binds_add_button_to_real_create_api(self):
        script=(Path(__file__).parents[1]/"public"/"app.js").read_text(encoding="utf-8")
        self.assertIn("add.onclick=openEmployeeForm",script)
        self.assertIn("'/api/organization/employees',{method:'POST'",script)

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
        self.assertEqual(len(task["plans"]),3)
        self.assertEqual(sum(x["recommended"] for x in task["plans"]),1)
        self.assertNotEqual(task["plans"][0]["strategy"],task["plans"][1]["strategy"])

    def test_relative_business_dates_resolve_against_current_week(self):
        reference=date(2026,8,5)
        self.assertEqual(parse_schedule_parameters("请安排本周五排班",reference)["start_date"],"2026-08-07")
        self.assertEqual(parse_schedule_parameters("请安排本周五排班",reference)["end_date"],"2026-08-07")
        self.assertEqual(parse_schedule_parameters("安排下周五",reference)["start_date"],"2026-08-14")
        self.assertEqual(parse_schedule_parameters("安排本周末",reference)["start_date"],"2026-08-08")
        self.assertEqual(parse_schedule_parameters("安排本周末",reference)["end_date"],"2026-08-09")

    def test_month_input_resolves_to_full_calendar_month(self):
        parsed=parse_schedule_parameters("请完成八月份整月排班",date(2026,7,20))
        self.assertEqual(parsed["start_date"],"2026-08-01")
        self.assertEqual(parsed["end_date"],"2026-08-31")
        self.assertEqual(business_month_period(date(2026,8,6)),("2026-08-01","2026-08-31"))

    def test_model_relative_date_is_normalized_before_database_query(self):
        self.assertEqual(normalize_model_date("本周五",date(2026,8,5)),"2026-08-07")
        self.assertEqual(normalize_model_date("2026-08-07",date(2026,8,5)),"2026-08-07")

    def test_this_friday_request_generates_shifts_for_resolved_date(self):
        with patch("backend.services.business_today",return_value=date(2026,8,5)):
            result=create_task(self.db,self.manager,"请安排本周五排班，覆盖率目标95%","store_management")
        for _ in range(100):
            task=task_detail(self.db,self.manager,result["task_id"])
            if task["status"] in ("completed","failed"):break
            time.sleep(.02)
        self.assertEqual(task["status"],"completed",task.get("error"))
        self.assertEqual(task["parameters"]["start_date"],"2026-08-07")
        self.assertEqual(task["parameters"]["end_date"],"2026-08-07")
        self.assertTrue(all(plan["shifts"] for plan in task["plans"]))
        self.assertTrue(all(shift["start_at"].startswith("2026-08-07") for plan in task["plans"] for shift in plan["shifts"]))

    def test_explicit_one_salesperson_is_a_hard_demand_constraint(self):
        reference=date(2026,8,5);parsed=parse_schedule_parameters("本周五只需要一名导购",reference)
        self.assertEqual(parsed["role"],"导购")
        self.assertEqual(parsed["headcount"],1)
        with patch("backend.services.business_today",return_value=reference):
            result=create_task(self.db,self.manager,"本周五只需要一名导购，请生成排班方案","store_management")
        for _ in range(100):
            task=task_detail(self.db,self.manager,result["task_id"])
            if task["status"] in ("completed","failed"):break
            time.sleep(.02)
        self.assertEqual(task["status"],"completed",task.get("error"))
        self.assertEqual(task["parameters"]["role"],"导购")
        self.assertEqual(task["parameters"]["headcount"],1)
        for plan in task["plans"]:
            self.assertEqual(plan["metrics"]["required"],1)
            self.assertEqual(plan["metrics"]["assigned"],1)
            self.assertEqual(len(plan["shifts"]),1)
            self.assertEqual(plan["shifts"][0]["role"],"导购")

    def test_explicit_headcount_overrides_larger_existing_demand(self):
        result=create_task(self.db,self.manager,"8月8日只需要1名导购排班","store_management")
        for _ in range(100):
            task=task_detail(self.db,self.manager,result["task_id"])
            if task["status"] in ("completed","failed"):break
            time.sleep(.02)
        self.assertEqual(task["status"],"completed",task.get("error"))
        self.assertTrue(all(plan["metrics"]["required"]==1 for plan in task["plans"]))
        self.assertTrue(all(len(plan["shifts"])==1 for plan in task["plans"]))

    def test_multiple_role_requirements_are_all_preserved_end_to_end(self):
        reference=date(2026,8,5);parsed=parse_schedule_parameters("本周五要一名导购，一名收银",reference)
        self.assertEqual(parsed["demand_items"],[{"role":"收银员","headcount":1},{"role":"导购","headcount":1}])
        self.assertIsNone(parsed["role"])
        self.assertIsNone(parsed["headcount"])
        with patch("backend.services.business_today",return_value=reference):
            result=create_task(self.db,self.manager,"本周五要一名导购，一名收银，请生成排班方案","store_management")
        for _ in range(100):
            task=task_detail(self.db,self.manager,result["task_id"])
            if task["status"] in ("completed","failed"):break
            time.sleep(.02)
        self.assertEqual(task["status"],"completed",task.get("error"))
        self.assertEqual(task["parameters"]["demand_items"],[{"role":"收银员","headcount":1},{"role":"导购","headcount":1}])
        for plan in task["plans"]:
            self.assertEqual(plan["metrics"]["required"],2)
            self.assertEqual(plan["metrics"]["assigned"],2)
            self.assertEqual(len(plan["shifts"]),2)
            self.assertEqual({shift["role"] for shift in plan["shifts"]},{"导购","收银员"})

    def test_schedule_request_without_any_date_does_not_use_hidden_default(self):
        parsed=parse_schedule_parameters("帮我生成排班",date(2026,8,5))
        self.assertIsNone(parsed["start_date"])
        self.assertIsNone(parsed["end_date"])

    def test_schedule_task_forecasts_future_demand_instead_of_recommending_empty_plans(self):
        result=create_task(self.db,self.manager,"请生成8月10日的排班，覆盖率目标95%","store_management")
        for _ in range(100):
            task=task_detail(self.db,self.manager,result["task_id"])
            if task["status"] in ("completed","failed"):break
            time.sleep(.02)
        self.assertEqual(task["status"],"completed",task.get("error"))
        self.assertEqual(len(task["plans"]),3)
        self.assertTrue(all(plan["metrics"]["required"]>0 for plan in task["plans"]))
        self.assertTrue(all(plan["shifts"] for plan in task["plans"]))
        self.assertGreater(self.db.execute("SELECT COUNT(*) n FROM business_demands WHERE demand_date='2026-08-10'").fetchone()["n"],0)

    def test_schedule_task_fails_when_no_demand_baseline_exists(self):
        self.db.execute("DELETE FROM business_demands");self.db.commit()
        result=create_task(self.db,self.manager,"请生成8月10日的排班","store_management")
        for _ in range(100):
            task=task_detail(self.db,self.manager,result["task_id"])
            if task["status"] in ("completed","failed"):break
            time.sleep(.02)
        self.assertEqual(task["status"],"failed")
        self.assertIn("历史岗位需求",task["error"])
        self.assertEqual(task["plans"],[])

    def test_schedule_plan_never_assigns_other_store_or_unavailable_employees(self):
        result=create_task(self.db,self.manager,"请生成8月6日的排班","store_management")
        for _ in range(100):
            task=task_detail(self.db,self.manager,result["task_id"])
            if task["status"] in ("completed","failed"):break
            time.sleep(.02)
        self.assertEqual(task["status"],"completed",task.get("error"))
        unavailable={(x["employee_id"],x["event_date"]) for x in self.db.execute("SELECT employee_id,event_date FROM attendance WHERE event_type IN ('leave','absence')")}
        for plan in task["plans"]:
            self.assertTrue(plan["shifts"])
            self.assertTrue(all(shift["store_id"]=="store-a" for shift in plan["shifts"]))
            self.assertTrue(all((shift["employee_id"],shift["start_at"][:10]) not in unavailable for shift in plan["shifts"]))

    def test_legacy_empty_plan_is_invalid_and_cannot_activate(self):
        result=create_task(self.db,self.manager,"请生成8月6日的排班","store_management")
        task_id=result["task_id"]
        for _ in range(100):
            task=task_detail(self.db,self.manager,task_id)
            if task["status"] in ("completed","failed"):break
            time.sleep(.02)
        plan_id=task["plans"][0]["id"]
        self.db.execute("DELETE FROM shifts WHERE plan_id=?",(plan_id,));self.db.execute("UPDATE schedule_plans SET metrics_json=? WHERE id=?",('{"required":0,"assigned":0,"coverage":0}',plan_id));self.db.commit()
        invalid=next(plan for plan in task_detail(self.db,self.manager,task_id)["plans"] if plan["id"]==plan_id)
        self.assertFalse(invalid["valid"])
        with self.assertRaisesRegex(ApiError,"空方案不能选择生效"):
            activate_plan(self.db,self.manager,plan_id)

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
        self.assertEqual(result["data"]["status"],"pending_confirmation")
        confirmed=confirm_employee_request(self.db,self.employee,result["data"]["request_id"])
        self.assertEqual(confirmed["status"],"pending_manager")
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

    def test_store_rule_returns_applicable_store(self):
        rule=next(item for item in rule_list(self.db) if item["id"]=="rule-5")
        self.assertEqual(rule["store_id"],"store-a")
        self.assertEqual(rule["store_name"],"上海静安旗舰店")
        self.assertTrue(rule["effective_from"])
        self.assertIsNone(rule["effective_to"])

    def test_store_rule_defaults_to_today_and_no_expiration(self):
        with patch("backend.services.business_today",return_value=date(2026,8,6)):
            result=create_rule(self.db,self.manager,{"text":"静安店晚高峰至少安排一名资深导购","store_id":"store-a"},AIClient(self.db))
        self.assertEqual(result["rule"]["effective_from"],"2026-08-06")
        self.assertIsNone(result["rule"]["effective_to"])

    def test_store_rule_uses_dates_stated_by_user(self):
        with patch("backend.services.business_today",return_value=date(2026,8,6)):
            result=create_rule(self.db,self.manager,{"text":"静安店规则从8月8日生效，8月31日失效","store_id":"store-a"},AIClient(self.db))
        self.assertEqual(result["rule"]["effective_from"],"2026-08-08")
        self.assertEqual(result["rule"]["effective_to"],"2026-08-31")

    def test_user_stated_dates_override_model_dates(self):
        class ModelStub:
            enabled=True
            def structured(self,*args):
                return {"name":"门店高峰规则","description":"测试原文日期优先级","scope":"store","strength":"soft","domain":"schedule","effective_from":"2026-09-01","effective_to":"2026-09-30","definition":{},"confidence":.9,"conflicts":[]}
        with patch("backend.services.business_today",return_value=date(2026,8,6)):
            result=create_rule(self.db,self.manager,{"text":"静安店规则从8月8日生效，8月31日失效","store_id":"store-a"},ModelStub())
        self.assertEqual(result["rule"]["effective_from"],"2026-08-08")
        self.assertEqual(result["rule"]["effective_to"],"2026-08-31")

    def test_rule_update_persists_new_version_and_snapshot(self):
        before=next(item for item in rule_list(self.db) if item["id"]=="rule-5")
        updated=update_rule(self.db,self.manager,"rule-5",{"name":"高峰技能覆盖（优化）","description":"晚高峰至少安排一名高熟练员工","scope":"store","store_id":"store-a","strength":"soft","domain":"skills"})
        self.assertEqual(updated["name"],"高峰技能覆盖（优化）")
        self.assertEqual(updated["version"],before["version"]+1)
        snapshot=self.db.execute("SELECT * FROM rule_versions WHERE rule_id='rule-5'").fetchone()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["version"],before["version"])

    def test_company_or_hard_rule_update_requires_approval(self):
        updated=update_rule(self.db,self.admin,"rule-5",{"name":"全公司技能覆盖","description":"所有门店必须满足岗位技能覆盖","scope":"company","store_id":None,"strength":"hard","domain":"skills"})
        self.assertEqual(updated["status"],"pending_approval")
        self.assertIsNone(updated["store_id"])

    def test_rule_update_rejects_reversed_validity_period(self):
        with self.assertRaisesRegex(ApiError,"失效日期不能早于生效日期"):
            update_rule(self.db,self.manager,"rule-5",{"name":"日期错误规则","description":"日期范围不合法","scope":"store","store_id":"store-a","effective_from":"2026-08-31","effective_to":"2026-08-08","strength":"soft","domain":"skills"})

    def test_manager_cannot_update_another_store_rule(self):
        self.db.execute("UPDATE rules SET store_id='store-b' WHERE id='rule-5'");self.db.commit()
        with self.assertRaisesRegex(ApiError,"授权门店"):
            update_rule(self.db,self.manager,"rule-5",{"name":"越权修改","description":"不应保存","scope":"store","store_id":"store-b","strength":"soft","domain":"skills"})

    def test_rule_page_localizes_enums_and_calls_real_update_api(self):
        script=(Path(__file__).parents[1]/"public"/"app.js").read_text(encoding="utf-8")
        self.assertIn("notice:'提示规则'",script)
        self.assertIn("hours:'工时管理'",script)
        self.assertIn("fatigue:'疲劳风险'",script)
        self.assertIn('data-edit-rule="${r.id}"',script)
        self.assertIn("method:'PUT'",script)
        self.assertIn("`/api/rules/${rule.id}`",script)
        self.assertIn("失效日期（留空为长期有效）",script)
        self.assertIn("<th>有效期</th>",script)

    def test_manager_can_decide_employee_request_in_own_store(self):
        created=employee_agent(self.db,self.employee,"我8月8日需要请假")
        confirm_employee_request(self.db,self.employee,created["data"]["request_id"])
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
