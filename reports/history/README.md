# التقارير التاريخية والأدلة

تبقى الملفات الأصلية في مساراتها حفاظًا على الروابط والاختبارات وبصمات الأدلة. اختلاف الأعداد بين مراحل مختلفة لا يجعل التقارير نسخًا مكررة.

| المجموعة | الملفات الأصلية |
|---|---|
| قبول OpenAI وإعادة الاختبار | `../live_acceptance.md`, `../live_acceptance_raw.json`, `../live_acceptance_recovery.md`, `../live_acceptance_recovery_raw.json`, `../ticket_retest_final.md`, `../ticket_retest_plan.md`, `../ticket_retest_dd43cfb703db46edba7be2c308887753.json`, `../ticket_retest_preflight_blocked.json` |
| التكلفة والميزانية | `../final_cost_audit.md`, `../cost_database_integrity.md`, `../ticket_budget_verification.md`, `../token_optimization.md`؛ التحليل الحالي في `../../cost_analysis.md` |
| الواجهة والأمان | `../ui_acceptance.md`, `../ui_reference_redesign.md`, `../ui_responsive_verification.md`, `../security_acceptance.md`, `../security_audit.md`, `../authentication_phase4.md`؛ نتائج الدخول السابقة تاريخية بعد إزالة نموذج الدخول |
| مراحل الكابستون والإغلاق | `../capstone_stage1.md`, `../capstone_compliance.md`, `../capstone_final_audit.md`, `../final_verification.md`, `../final_release_gate.md`, `../submission_audit.md`, `../github_readiness.md` |
| الجودة والأداء والاستمرارية | `../code_quality_hardening.md`, `../performance_report.md`, `../functional_tests.md`, `../phase5_backup_verification.md`, `../ai_evaluation.md`, `../known_issues.md` |

## TASK 08

[المجلد الأصلي](../evidence/task08_20261105_6c453a57ee85/) يحوي النتائج وusage والتكلفة والتقرير الحي؛ هذه محفوظة دون إعادة كتابتها. `runner.py` و`guard.py` هما مشغّل الصيانة المصحح، و`runner.original.py` و`guard.original.py` نسختا الشيفرة التاريخية قبل التصحيح. نجاح Preflight محلي لا يعني إعادة تنفيذ OpenAI الحي.

استثناءات `.gitignore` تسمح بخمسة سجلات TASK 08 المنقحة تحديدًا: `shipping.jsonl`, `order_status.jsonl`, `create_ticket.jsonl`, `memory_followup.jsonl`, `ambiguous.jsonl`. الملفات التشغيلية الأخرى وقواعد SQLite تبقى مستبعدة.

## سجلات الكابستون

[success_1](../../traces/success_1.jsonl)، [near_failure_recovery](../../traces/near_failure_recovery.jsonl)، و[explicit_failure](../../traces/explicit_failure.jsonl) آثار محاكاة OpenAI مع MCP فعلي. تبقى أدلة مستقلة عن TASK 08 الحي، ولا تتغير أسماؤها أو محتوياتها.
