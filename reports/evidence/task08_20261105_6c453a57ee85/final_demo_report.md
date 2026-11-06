# TASK 08 — التحقق النهائي الحي والعرض

- التاريخ: 2026-11-05 (Asia/Riyadh)؛ معرّف التشغيل: `6c453a57ee85`.
- OpenAI: PASS؛ النموذج الفعلي: `gpt-5.4-mini-2026-03-17`؛ TLS موثّق؛ لا إعادة تلقائية.
- إجمالي الاستخدام المسوّى: 7356 input + 225 output = 7581 tokens؛ cached: 0.
- التكلفة: $0.00652950؛ سقف المهمة $0.005 لكل سيناريو، وسقف يومي $0.10، وسقف هذه المهمة $1.
- الحجز القديم المجهول: بقي دون تغيير؛ التكلفة اليومية بعد التشغيل: $0.0089745 مع حجز مجهول واحد.

## السيناريوهات

| السيناريو | النتيجة | أدوات MCP | الرموز (إدخال/إخراج) | التكلفة |
|---|---|---|---:|---:|
| shipping | PASS | search_faq | 1692/48 | $0.00148500 |
| order_status | PASS | get_order_status | 1574/41 | $0.0013650 |
| create_ticket | PASS | create_ticket | 1664/80 | $0.00160800 |
| memory_followup | PASS | load_memory | 1686/33 | $0.00141300 |
| ambiguous | PASS | لا يلزم استدعاء أداة | 740/23 | $0.0006585 |

- `shipping`: إجابة الشحن جاءت من `search_faq`.
- `order_status`: حالة الطلب الاصطناعي تحققت عبر `get_order_status`.
- `create_ticket`: موافقة معلقة مطابقة سبقت الكتابة؛ تذكرة اصطناعية واحدة، نتيجة محفوظة، وإعادة بنفس المفتاح أعادت النتيجة دون تكرار.
- `memory_followup`: استعاد الوكيل ملخص الذاكرة الاصطناعي المحفوظ عبر MCP بعد إغلاق الخادم وإعادة اتصاله.
- `ambiguous`: طلب الوكيل توضيحًا ولم يستدع أداة.

## الواجهة والحزمة والاختبارات

- UI: PASS؛ 7 passed / 0 failed / 0 skipped؛ شمل تشغيل Streamlit منفصلًا وفحص عدم تقديم الملفات الخاصة.
- SHA-256 ZIP المعتمد: `31ca48fe78c40f799b8d3811245902853db587fc98f2ca5623ff73581ba40ac9`؛ مطابق للـ sidecar: True؛ الحزمة بقيت دون تغيير.
- اختبارات الضبط المركزية قبل السيناريوهات: 35 passed؛ اختبار OpenAI+MCP الحي المضمن: 1 passed. اختبارات UI أعلاه منفصلة.
- لم يتغير أي دليل تاريخي، وبقي الحجز السابق unknown.

## فحوص الاتساق

```json
{
  "zip_sha256_matches_approved_sidecar": true,
  "evidence_scenarios_all_pass": true,
  "all_costs_settled": true,
  "each_task_within_005": true,
  "daily_cap_with_unknown_reservation": true,
  "no_historical_evidence_changed": true,
  "old_unknown_unchanged": true,
  "package_unchanged": true
}
```
