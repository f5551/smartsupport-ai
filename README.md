# SmartSupport AI

وكيل واحد لدعم عملاء متجر إلكترونيات افتراضي بالعربية: يجيب من سياسات المتجر، يستعلم عن الطلبات والتذاكر، ويطلب موافقة صريحة قبل أي كتابة.

**[جرّب العرض على Fly.io](https://smartsupport-ai.fly.dev/)** · [المستودع](https://github.com/f5551/smartsupport-ai) · [مواصفات الوكيل](agent_spec.md) · [تحليل التكلفة](cost_analysis.md)

> العرض العام **Demo ببيانات اصطناعية ومحاكاة النموذج**، مع MCP وSQLite حقيقيين. لا يحتاج مفتاح OpenAI ولا ينفذ OpenAI المدفوع. الدخول مباشر دون مصادقة حساب؛ ليس نظام متجر إنتاجيًا ولا مساحة لإدخال بيانات شخصية.

## البنية

```text
Streamlit → WebBackend → Agent → Responses Tool Calling
                           ↓                 ↑
                     MCP client ↔ MCP server (stdio)
                                      ↓
                              SQLite + بيانات اصطناعية
```

يستخدم المسار الحي المصرح به OpenAI Responses API؛ يستخدم Demo نفس مسار الأدوات مع `DemoOpenAI` بدل النموذج الحقيقي. الشيفرة الرئيسية: [app.py](app.py)، [agent.py](agent.py)، [mcp_transport.py](mcp_transport.py)، [mcp_server.py](mcp_server.py)، [database.py](database.py).

الواجهة عربية RTL بصفحات الرئيسية والمحادثات والطلبات والتذاكر والأدوات والأمان والإعدادات، وتدعم المظهرين الفاتح والداكن.

## الأدوات الست وMCP

| الأداة | الوظيفة | الإذن |
|---|---|---|
| `search_faq` | البحث في الشحن وسياسات المتجر | قراءة تلقائية |
| `get_order_status` | حالة طلب مملوك لعميل الجلسة | قراءة تلقائية |
| `create_ticket` | إنشاء شكوى لطلب مملوك | موافقة صريحة |
| `get_ticket_status` | قراءة حالة تذكرة مملوكة | قراءة تلقائية |
| `save_memory` | حفظ ملخص للجلسة | موافقة صريحة |
| `load_memory` | استرجاع ملخص الجلسة | قراءة تلقائية |

خادم MCP عملية Python مستقلة، يتصل بها العميل عبر stdio وJSON-RPC. هوية العميل والجلسة يثبتها التطبيق خارج معاملات أدوات النموذج؛ لا تقبل الأدوات اختيار `customer_id` أو `session_id`. [اختبارات التكامل](tests/test_mcp_integration.py) تختبر الاكتشاف والاستدعاءات والرفض عبر البروتوكول الفعلي.

## SQLite والأذونات والتدقيق

- تبدأ كل محادثة بمتجر اصطناعي مستقل؛ هوية جلسة Demo عشوائية والعميل التجريبي ثابت. لا تمثل هذه الهوية مصادقة إنتاجية.
- تحفظ SQLite الذاكرة حسب العميل والجلسة، وتسترجعها عملية MCP جديدة ما دام ملف القاعدة باقياً. [اختبار إعادة التشغيل](tests/test_agent_mcp.py) يستخدم تفضيلًا عشوائيًا لا يقدمه في سؤال الاسترجاع أو history.
- السياسة الفعلية `ask_on_write`: الموافقة مربوطة بالعميل والجلسة والأداة والمحتوى وبانتهاء صلاحية؛ النموذج لا يصدرها. idempotency وسجل العمليات والمصالحة يمنعون تكرار الأثر المؤكد. قواعد الموافقة والمتجر منفصلة؛ لا توجد ذرّية كاملة بينهما.
- يسجل [Audit Log](audit_logger.py) أحداث التنفيذ والنتائج والأخطاء الآمنة دون مفاتيح أو رموز موافقة. تنظيف تسجيل الزائر المنتهي لا يحذف المحادثات والسجلات.
- تتوفر نسخ SQLite وفحوص سلامة واستعادة تحت قفل صيانة؛ راجع [التشغيل والصيانة](docs/operations.md).

## التكلفة وحدود التوقف

شروط التوقف الأساسية `final_answer` و`max_turns` و`budget`؛ توجد أيضًا حالات الخطأ وانتظار الموافقة. يفرض [config.py](config.py) و[Agent](agent.py):

| الإعداد | الحد الافتراضي والأقصى |
|---|---:|
| `MAX_AGENT_TURNS` | 4 |
| `MAX_INPUT_TOKENS` / `MAX_OUTPUT_TOKENS` | 3000 / 350 |
| `MAX_TOOL_CALLS` | 3 |
| `MAX_HISTORY_MESSAGES` / `MAX_TOOL_RESULT_CHARS` | 6 / 2000 |
| `MAX_TASK_COST_USD` / `MAX_DAILY_COST_USD` | $0.005 / $0.10 |
| `MAX_API_RETRIES` | 0 |

حجز التكلفة تقديري؛ قد يتجاوز الطلب الجاري التقدير. يسوي المتتبع التكلفة من usage الحقيقي ويحتفظ بالحجز عند غموض النتيجة. الحجز التاريخي **$0.002445 بحالة unknown** ليس مصروفًا مسوّى ولا يدخل المتوسطات.

| العينة الحية التاريخية | المهام الناجحة المختارة | التكلفة USD | متوسط المهمة USD |
|---|---:|---:|---:|
| آخر نجاح لكل سيناريو — 2026-11-04 | 5 | 0.00618075 | 0.00123615 |
| TASK 08 — 2026-11-05 | 5 | 0.00652950 | 0.00130590 |

العينتان منفصلتان، وليستا متوسطًا إنتاجيًا. TASK 08: **7356 إدخال، 225 إخراج**؛ 9 طلبات توليد و9 طلبات عدّ `responses.input_tokens.count` على OpenAI، وليس عدًا محليًا بالكامل. الحلقة الاعتيادية تستعمل عدًا محليًا تقديريًا. مصادر الأرقام: [نتيجة TASK 08](reports/evidence/task08_20261105_6c453a57ee85/result.json)، [دليل العينة السابقة](reports/evidence/live_acceptance_closeout.json)، و[التفصيل المالي](cost_analysis.md).

## التشغيل المحلي

Python **3.13.7** هو الإصدار المختبر والمحدد في `.python-version`. من جذر المستودع، على Windows PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pypdf==7.0.0
$env:SMARTSUPPORT_PUBLIC_DEMO_ONLY="1"
$env:SMARTSUPPORT_DEPLOYMENT="public"
$env:SMARTSUPPORT_ENABLE_LOCAL_LIVE="0"
$env:RUN_LIVE_OPENAI="0"
.\.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8503
```

افتح [الواجهة المحلية](http://127.0.0.1:8503/). على Linux/macOS استخدم `python3 -m venv .venv` ثم `.venv/bin/python`، واضبط المتغيرات نفسها باستخدام `export`. إذا كانت البيئة موجودة فلا تعِد إنشائها. لا يلزم `.env` ولا تهيئة قاعدة التشغيل يدويًا للواجهة؛ تهيئ بياناتها الاصطناعية تلقائيًا. `pypdf` أداة فحص إصدار واختبارات وليست شرط اتصال MCP.

جرّب: «كم تستغرق مدة الشحن؟»، «ما حالة الطلب DEMO-1001؟»، ثم شكوى للطلب DEMO-1002؛ راجع تفاصيل الموافقة قبل قبولها. لا تستخدم بيانات حقيقية.

## الاختبارات والأمان

آخر CI مثبت على Ubuntu 24.04 للـCommit `d3c44492e28b94276ebad509a0f4993f6adf074a`: **433 passed، 0 failed، 1 skipped** خلال 75.39 ثانية؛ [التشغيل الأصلي](https://github.com/f5551/smartsupport-ai/actions/runs/42309196286). المتجاوز هو اختبار OpenAI المدفوع لغياب opt-in؛ اختبار صلاحيات Linux نجح في هذا التشغيل. أرقام التقارير الأقدم تخص تشغيلاتها التاريخية. هذه نتيجة قبل تنظيم التوثيق الحالي، وليست ادعاء إعادة اختبار شامل بعده.

```powershell
$env:RUN_LIVE_OPENAI="0"
.\.venv\Scripts\python.exe -m pytest -q -rs
.\.venv\Scripts\python.exe mcp_client_test.py
.\.venv\Scripts\python.exe release_check.py
.\.venv\Scripts\python.exe -m pip check
```

[اختبارات الحقن الثلاثة](tests/test_phase5_security.py) تستخدم **Fake OpenAI مع MCP حقيقي** وتثبت حواجز التنفيذ ضد الكتابة وانتحال الهوية؛ لا تثبت مقاومة نموذج OpenAI الحي لكل الهجمات. تشمل الاختبارات أيضًا ملكية البيانات، الموافقات، منع التكرار، الذاكرة والجلسات، والتكلفة. [تقرير الأمان](reports/security_acceptance.md) و[فهرس الأدلة](reports/README.md) يوضحان نطاق النتائج وتاريخها.

## النشر والتجربة على Fly.io

العنوان المقدم للتجربة هو **https://smartsupport-ai.fly.dev/**. الإعداد الصحيح للعرض العام هو `SMARTSUPPORT_PUBLIC_DEMO_ONLY=1` مع تعطيل Live؛ [دليل Fly.io](docs/fly_demo.md) يبين المتغيرات وأمر التشغيل وفحوص الصحة وحدود التخزين.

بعد جلب تحديثات `origin/main`، أصبح [Dockerfile](Dockerfile) و[fly.toml](fly.toml) موجودين محليًا: صورة Python 3.12.16 ومنفذ 8501 وMachine بذاكرة 1GB في ams، مع توقف وتشغيل تلقائيين وHTTPS مفروض. إعدادات Secrets وقيم البيئة الفعلية بعيدة وغير متحققة، ولا توجد Volume معرفة في الملف. يبقى [render.yaml](render.yaml) إعدادًا بديلًا تاريخيًا دون تغيير، وليس دليل النشر الحالي. تعليمات الكابستون ذكرت Render أو Railway؛ يلزم قبول المقيم لـFly.io أو استيفاء المنصة المطلوبة، ولا يُفترض اعتماد Fly رسميًا.

**`/tmp` على Fly.io غير دائم**: قد تضيع الذاكرة والمحادثات عند استبدال Machine؛ نجاح إعادة تشغيل MCP محليًا لا يثبت التخزين الدائم في الاستضافة. العرض العام لا يحتاج OpenAI الحقيقي ولا مفتاحه.

## ملفات التسليم والأدلة

| عنصر التسليم | الرابط / الحالة |
|---|---|
| مستودع GitHub | [f5551/smartsupport-ai](https://github.com/f5551/smartsupport-ai)؛ تعديلات التوثيق الحالية محلية حتى الموافقة على رفعها |
| تجربة حية | [Fly.io](https://smartsupport-ai.fly.dev/)؛ قبول المنصة بدل Render/Railway يحتاج تأكيدًا |
| السجلات الثلاثة | [success_1](traces/success_1.jsonl) · [near_failure_recovery](traces/near_failure_recovery.jsonl) · [explicit_failure](traces/explicit_failure.jsonl) |
| مواصفات الوكيل | [agent_spec.md](agent_spec.md)؛ Markdown المختصر، دون تغيير في هذه المراجعة |
| تحليل التكلفة | [cost_analysis.md](cost_analysis.md)؛ القياسات الحقيقية منفصلة عن المحاكاة والحجوزات |

السجلات الثلاثة **محاكاة OpenAI عبر MCP حقيقي** وليست OpenAI حيًا. أدلة السيناريوهات الخمسة الحية محفوظة منفصلة في [TASK 08](reports/evidence/task08_20261105_6c453a57ee85/final_demo_report.md). لم يُعثر محليًا على Rubric رسمي أو أوزان تقييم.

## خريطة التوثيق

- [التشغيل والصيانة والحدود](docs/operations.md).
- [إعداد العرض العام على Fly.io](docs/fly_demo.md).
- [فهرس التقارير](reports/README.md) و[قائمة الكابستون المرجعية](reports/capstone_final_checklist.md).
- [نسخة README التاريخية والتفاصيل السابقة](docs/history/readme_before_documentation_cleanup.md)، [جاهزية النشر التاريخية](docs/history/deployment_readiness.md)، [مراجعة الواجهة الأولى](docs/history/ui_review.md)، [مراجعة SaaS](docs/history/saas_ui_review.md). أُبقيت الأدلة الفريدة وأرقامها في سياقها، لا بوصفها الحالة الراهنة.

الخط العربي محلي بترخيص SIL OFL؛ [تفاصيل الخط والبصمات](assets/fonts/README.md). المشروع بترخيص [MIT](LICENSE).
