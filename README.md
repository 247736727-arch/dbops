# DBOps · 企业内数据库实例管理系统

> Django 笔试题 **方案 A（数据库实例管理）** 的完整实现。
> 覆盖全部 5 个考点：多表模型、CRUD + 主动探测、周期任务 + 敏感字段加密、每日统计入库、请求耗时中间件。

**技术栈**：Python 3.12 · Django 6.1 · DRF 3.18 · Celery 5.6 · Redis 7 · PostgreSQL 16 · cryptography(Fernet) · drf-spectacular

---

## 一、快速开始

### 方式 A：Docker（推荐，一条命令起全套）

```bash
cp .env.example .env          # 按需修改
docker compose up --build     # db + redis + web + worker + beat 五个服务
docker compose run --rm web python manage.py migrate
docker compose run --rm web python manage.py seed_demo   # 可选：灌入演示数据
```

| 入口 | 地址 |
| --- | --- |
| Swagger API 文档 | <http://localhost:8000/api/docs/> |
| OpenAPI Schema | <http://localhost:8000/api/schema/> |
| Django Admin | <http://localhost:8000/admin/> |
| 健康检查 | <http://localhost:8000/healthz/> |

> **为什么 beat 是独立服务**：定时任务必须在**唯一**的进程里调度。
> 用 `celery worker -B`（内嵌 beat）一旦 worker 扩容到多副本，轮换与统计就会各触发多次。
> compose 里 `beat` 单独一个 service，`--schedule` 落在卷里避免重启后重复补跑。

### 方式 B：本地虚拟环境（零 Docker）

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
python manage.py migrate
python manage.py seed_demo
python manage.py runserver
```

本地默认使用 **SQLite**（零依赖即可跑通全部 5 题），如需 PostgreSQL 在 `.env` 里配 `DATABASE_URL`。
要验证定时任务，另开两个终端：

```bash
celery -A config worker --loglevel=INFO
celery -A config beat  --loglevel=INFO
```

**登录凭据**：DRF 默认 `IsAuthenticated`。创建用户后可访问 `/admin/` 或用 Token 调用 API：

```bash
python manage.py createsuperuser
python manage.py drf_create_token <username>       # 需要 rest_framework.authtoken（已安装）
curl -H "Authorization: Token <token>" http://127.0.0.1:8000/api/instances/
```

---

## 二、五道题对照表

| # | 考点 | 实现位置 | 核心考点覆盖 |
| --- | --- | --- | --- |
| 1 | 多表模型 + 外键 | `apps/inventory/models.py` | 部门 ⊃ 集群 ⊃ 实例三层；按语义区分 `PROTECT` / `CASCADE`；`host+port` 唯一约束 |
| 2 | CRUD + TCP 探测 | `apps/inventory/services.py`、`views.py` | **必带 timeout**；异常分类（超时/拒绝/DNS/不可达）；结果落 `ProbeLog` |
| 3 | 周期任务 + 加密 `apps/core/crypto.py`、`fields.py`、`apps/inventory/tasks.py` | | **Fernet 可逆加密**（非哈希）；`secrets` 生成；幂等 + 逐实例故障隔离 + 审计留痕 |
| 4 | 每日 00:00 统计入库 | `apps/stats/models.py`、`tasks.py` | 数据库侧聚合；补零；**唯一约束 + upsert 保幂等**；`CELERY_TIMEZONE` |
| 5 | 请求耗时中间件 | `apps/core/middleware.py` | `perf_counter()`；**`try/finally` 保证 500 也统计**；不写库，走结构化日志 |

---

## 三、关键实现取舍（逐题说明）

### 第 1 题｜三层模型为什么不是多对多

```
Department 1 ── n Cluster 1 ── n DBInstance
                                ├── n ProbeLog          (CASCADE)
                                └── n PasswordRotationLog
```

一个集群在组织结构上只归属一个部门。若做成 M2M，**第 4 题"按部门 × 集群统计"会出现同一实例被多个部门重复计数**，口径不再唯一——这是统计题的隐性前置条件。

`on_delete` 按语义分开，没有无脑 `CASCADE`：

- `Cluster.department = PROTECT`：部门下还有集群时禁止删除，否则统计维度悬空；
- `DBInstance.cluster = PROTECT`：**实例是真实资产，不能因为删了集群就把资产记录抹掉**；
- `ProbeLog.instance = CASCADE`：探测记录是附属数据，随实例清理。

另外加了 `(cluster, name)` 与 `(host, port)` 两个唯一约束——host+port 是一个实例在网络上的唯一身份，能从源头挡住重复录入这类 CMDB 最常见的脏数据。

### 第 2 题｜探测接口最容易"能跑但被扣分"

两个致命点，代码里都明确处理了：

1. **必须显式超时**。`socket.create_connection()` 不传 timeout 会用系统默认值（可能数分钟）。目标一旦是不可达 IP，gunicorn 线程被长期占用，**几十个这样的请求就能拖垮整个服务**。
2. **异常必须分类**。超时 / 端口拒绝 / DNS 失败 / 网络不可达对应完全不同的运维处置动作，只返回布尔值等于把排障成本转嫁给调用方。统一返回结构化 `ProbeResult`。

此外做了**连接前的入参校验**（拦截 `0.0.0.0`、组播/未指定地址、端口越界）。探测接口本质是"让服务端代你发起网络连接"，天然具备 SSRF 特征，生产还应叠加内网网段黑白名单与并发上限。

### 第 3 题｜加密不等于哈希 —— 本题最大的区分点

系统之后要拿这个密码**去连接数据库**，所以必须是**可逆**的对称加密，而不是 `make_password` 那种单向哈希。这里用 Fernet（AES-128-CBC + HMAC-SHA256，自带完整性校验，密文被篡改能立刻发现）。

- 生成凭据用 `secrets.token_urlsafe(24)`（≈144 bit 熵）而非 `random`——后者是梅森旋转伪随机，知道部分输出可预测后续序列。
- 字段类型用 `EncryptedTextField` 而非 `CharField(max_length=64)`：Fernet 密文是 base64 后约 100+ 字符，**定长 CharField 会被截断导致解密失败**。
- **密钥可平滑轮换**：`FIELD_ENCRYPTION_KEYS` 支持多把逗号分隔，第一把用于加密、其余用于解密。换新时把新密钥放最前、旧的留后面即可兼容历史密文。
- **生产强制校验**：非 DEBUG 且未配置该变量时直接抛 `ImproperlyConfigured`，不允许从 `SECRET_KEY` 派生（见过太多次换 SECRET_KEY 导致历史密文全部解不开的事故）。

三个容易被忽略、但线上必须成立的性质：

| 性质 | 实现 |
| --- | --- |
| **幂等** | 用 `password_rotated_at + 周期` 做窗口判断，且在事务内 `select_for_update()` 拿到行锁后再判断。重复投递 / 人工补跑都只会跳过，不会把同一实例换两次密码（换两次 = 应用侧缓存失效，直接连不上库） |
| **故障隔离** | 批量任务逐实例 `try/except`，单个失败不影响整批，各自独立写审计；批次上限 `ROTATION_BATCH_SIZE` 防止一次压垮数据库 |
| **可审计** | 每次轮换写 `PasswordRotationLog`（成功/失败、触发来源、消息） |

> **一个额外考虑**：题目要求"轮换"，但真实运维不能做到密码瞬间唯一有效——应用侧缓存的是旧值，轮换瞬间会全线连不上。因此保留了 `previous_password` 与过渡期（默认 24h），**新旧密码并行一段时间**。这是超出题面但符合工程常识的一步。
> 取用明文凭据的接口加了 `IsAdminUser` 限制，其余地方只输出脱敏值。

### 第 4 题｜幂等与补零，两个人人踩的坑

- **幂等**：`(stat_date, department, cluster)` 唯一约束 + `bulk_create(update_conflicts=True)`。任务重跑只会 UPDATE 计数，不会 INSERT 出重复行——**否则报表直接翻倍**。
- **补零**：只从 `DBInstance` 聚合会漏掉"一个实例都没有的集群"，每日快照应完整覆盖所有集群，否则报表上看不出"这个集群空了"。这里以 `Cluster` 为基准表，用聚合结果做字典补零。
- **在数据库侧聚合**：`values(...).annotate(Count("id"))` 一条 SQL 出结果，不查全量实例到 Python 里 for 循环。
- **时区三件套**：`TIME_ZONE = Asia/Shanghai` + `USE_TZ = True` + **`CELERY_TIMEZONE = TIME_ZONE`**。最后一条最容易漏：不设的话 `crontab(hour=0)` 会跑在 UTC 00:00，也就是**北京时间早上 8 点**。
- 外键用 `PROTECT`：统计数据是历史快照，不能因为删了集群就让它消失。

### 第 5 题｜三套方案逐字相同的必得分题

- `perf_counter()` 而非 `time.time()`：前者单调递增，不受 NTP 校时影响。
- **`try/finally`**：异常请求（尤其 500）才是排查的主角，必须同样被统计到；只在正常返回分支记日志的话，最需要的数据恰好会缺失。
- **不写数据库**：每个请求一次 INSERT 会把读流量放大成写流量，高 QPS 下直接成为瓶颈。这里输出结构化日志（含 `status_code` / `duration_ms` / `user_id`），落地交给 ELK/Loki 离线算 P95/P99。
- 跳过静态资源与健康检查；响应头附带 `X-Request-Time` 便于肉眼验证。

---

## 四、API 速查

所有接口需认证（Token 或 Session），均为 `/api/` 前缀。完整可交互文档见 `/api/docs/`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/api/departments/` | 部门列表 / 新增 |
| GET/PATCH/DELETE | `/api/departments/{id}/` | 部门详情 / 修改 / 删除（有集群时 PROTECT 拦截） |
| GET/POST | `/api/clusters/` | 集群列表 / 新增 |
| GET/POST | `/api/instances/` | 实例列表 / 新增（支持 `cluster` `status` 过滤、`search`、`ordering`、分页） |
| **POST** | `/api/instances/{id}/probe/` | **第 2 题：TCP 端口探测并落库** |
| GET | `/api/instances/{id}/probe-logs/` | 该实例历史探测记录 |
| POST | `/api/instances/{id}/rotate-password/` | 手动触发单个实例密码轮换（应急用） |
| GET | `/api/instances/{id}/rotation-logs/` | 该实例轮换审计记录 |
| GET | `/api/instances/{id}/credential/` | 取用明文凭据（**仅管理员**） |
| GET | `/api/instance-stats/` | **第 4 题：每日统计结果**（支持 `stat_date` `department` `cluster` 过滤） |
| GET | `/healthz/` | 健康检查 |

---

## 五、测试

```bash
pytest                                             # 61 项，全部通过
ruff check .                                       # All checks passed
python manage.py check                             # System check identified no issues
python manage.py makemigrations --check --dry-run  # No changes detected
```

测试统一放在根目录 `tests/`（未在 app 内留空壳 `tests.py`）。覆盖如下：

| 文件 | 覆盖内容 |
| --- | --- |
| `test_models.py` | 密文落库（`password` 字段在 DB 中确实是密文）、加解密往返、密钥轮换后历史密文仍可读、**被引用时 PROTECT 拦截删除**、唯一约束（`same host:port` 报错）、密码生命周期与过渡期 |
| `test_probe_api.py` | 超时/拒绝/不可达/DNS 四类结果分别返回正确 `reason`、非法目标被拦截、**必设 timeout**、探测记录确实落库、CRUD 与过滤分页 |
| `test_rotation.py` | **重复调用不重复轮换（幂等）**、周期内跳过、单实例失败不中断整批、失败写入审计、旧密码保留、批量上限生效 |
| `test_stats.py` | 统计数值正确、**空集群补零**、**同一天重跑不产生重复行**、指定日期补跑、时区为 `Asia/Shanghai` |
| `test_middleware.py` | 正常请求统计到耗时、**抛异常（500）同样被统计**（`try/finally` 生效）、静态资源与健康检查被跳过、响应头带 `X-Request-Time` |
| `test_api_docs.py` | OpenAPI Schema 可生成、Swagger UI 可访问 |

测试用独立的 `config/settings_test.py`，注入一次性 Fernet 密钥并缩短探测超时。**把它删掉再跑 pytest 会立刻失败**——这恰好验证了"缺密钥就不允许写敏感字段"的保护是生效的。

---

## 六、端到端验证记录（真实启动服务 + HTTP 请求）

单元测试之外，另做过一次"真起服务、真发请求"的冒烟，用 curl 打本地 runserver：

| 验证项 | 请求 | 结果 |
| --- | --- | --- |
| 健康检查不被计时污染 | `GET /healthz/` | 200，且**没有** `X-Request-Time` 响应头（中间件正确跳过） |
| 未认证访问被拒 | `GET /api/departments/` | 401 |
| 认证后可读取 | `GET /api/departments/` + Token | 200，`X-Request-Time: 29.6ms` |
| **敏感字段不外泄** | `GET /api/instances/` | 无 `password` 明文字段，只有 `masked_password=In******rd` |
| 真实 TCP 探测 | `POST /api/instances/3/probe/` | 200 且结构化返回 `reason=refused`，**没有抛 500** |
| **统计幂等**（连跑两次） | `POST /api/instance-stats/collect/` | 两次均为 `rows=6, total_instances=7`，**未翻倍** |
| 空集群补零 | `GET /api/instance-stats/` | 存在 `dw-mysql-archive … instances=0` 行 |
| API 文档 | `GET /api/docs/`、`/api/schema/` | 200 |

### 定时任务的时区：实测而非假设

`settings.py` 里同时有 `CELERY_TIMEZONE = "Asia/Shanghai"` 和 `CELERY_ENABLE_UTC = True`，这两条乍看会打架。实测结果：

```
app.timezone   = Asia/Shanghai
crontab.tz     = Asia/Shanghai
daily cron hour=0  → 下一次触发落在 Asia/Shanghai 00:00
rotation cron */12 → 下一次触发落在 Asia/Shanghai 12:00
```

依据见 `celery/app/base.py`：`enable_utc` 只在**没有显式设置 timezone** 时才作为兜底；本项目已显式设置，因此不会被覆盖为 UTC。结论已写进 `config/settings.py` 的注释，避免后来者误改。

### 关于本机探测耗时 —— 一个容易被误判的现象

Windows 本机对照实验中，`127.0.0.1` 上**未监听**端口的探测耗时约 **2 秒**，看起来很像"超时没生效"。这其实是误导：

| 目标 | 结果 | 耗时 |
| --- | --- | --- |
| 本机已监听端口 | `reachable=true` | **4.5 ms** |
| 本机未监听端口（`127.0.0.1:3309`、`:1`） | `refused` | ~2030 ms |
| 不可路由地址（`192.0.2.1:80`） | `timeout` | **3002 ms ≈ 配置的 3.0s** |
| 不存在的域名 | `dns_error` | — |

关键点：**不可路由地址精确在配置的 3.0 秒返回**，说明 `socket.create_connection(timeout=...)` 的超时保护是完全生效的；而回环 RST 的 2 秒延迟来自本机网络栈/安全软件，**属于环境特性，与代码无关**（同一台机器上不同未监听端口耗时一致）。在 Linux/CI 上重复该场景，回环拒绝通常是毫秒级。

四类异常的分类也随之验证：`ok` / `refused` / `timeout` / `dns_error` 全部返回正确的结构化结果。

### PostgreSQL 路径：单独真跑过，不只是写进矩阵

CI 里的 `postgres` job 是 SQLite 无论如何覆盖不到的部分，因此在本机单独起了一个**真实的 PostgreSQL 16.2** 实例做完整验证（不装系统服务，用自带二进制的临时实例，验证完即销毁）：

| 项目 | 结果 |
| --- | --- |
| 服务端版本 | PostgreSQL 16.2（与 CI 的 `postgres:16-alpine` 同大版本） |
| `manage.py migrate` | 全部 OK（含 `inventory.0001_initial`、`stats.0001_initial`） |
| `pytest` | **61 passed**（5.40s） |
| `manage.py check` | 0 issues |
| `makemigrations --check --dry-run` | No changes detected |

**一个只有真跑 PostgreSQL 才会暴露的事实**：`select_for_update()` 在 SQLite 上是**静默空操作**。同一段代码在两种库上的实测对比：

| | PostgreSQL 16.2 | SQLite |
| --- | --- | --- |
| 生成的 SQL | 结尾带 `... FOR UPDATE` | 结尾**没有** `FOR UPDATE` |
| 第二个事务 `select_for_update(nowait=True)` | `OperationalError: could not obtain lock on row`（112 ms 内失败） | 立刻拿到锁（3.4 ms），**没有任何互斥** |

结论：第 3 题的并发幂等保护（行锁 + `password_rotated_at` 判断）**只有在 PostgreSQL 上才真正生效**。本地用 SQLite 并行跑两个 worker 时这层保护形同虚设——但那是 SQLite 的能力边界，不是实现缺陷；生产用 PostgreSQL，保护真实存在，且并发冲突会立刻报错而不是静默双写。

> 过程中的两个坑（与项目实现无关，但值得记录）：① 临时用的 PG 构建**不带时区数据**（`share/postgresql/timezone` 缺失），会让 Django 连接时的 `set_config('TimeZone','UTC')` 抛 `invalid value for parameter "TimeZone"`，补上 tzdata 即恢复；官方 `postgres:16` 镜像自带 tzdata，CI 不会遇到。② Windows 上 PostgreSQL 拒绝以管理员令牌运行，需降权启动。

---

## 七、CI

`.github/workflows/ci.yml` 在 push/PR 时自动执行：

- **静态检查**：`ruff check .` → `manage.py check` → `makemigrations --check --dry-run`
  （最后一条专门防"改了模型却忘记生成迁移"，这类疏漏本地不易发现，会在部署后炸）
- **双数据库测试矩阵**：SQLite × PostgreSQL 各跑一遍 pytest。
  本地开发默认 SQLite，生产是 PostgreSQL（见 `docker-compose.yml`），两种库都跑才能确认没用到某个库独有的行为。
  PostgreSQL 那一格已在本机用真实 16.2 实例跑通（见上节），不是"只写进矩阵、没真跑过"。

本地复现 CI 的全过程：

```bash
export DJANGO_SETTINGS_MODULE=config.settings_test
ruff check .
python manage.py check
python manage.py makemigrations --check --dry-run
pytest
```

---

## 八、环境变量

见 `.env.example`（已注释说明）。真正需要关注的只有两个：

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `FIELD_ENCRYPTION_KEYS` | **生产必填** | 逗号分隔的 Fernet 密钥，首个用于加密。生成：`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `SECRET_KEY` | 生产必填 | 切勿使用仓库内的默认值 |

其余（`PROBE_TIMEOUT_SECONDS`、`PASSWORD_ROTATION_INTERVAL_HOURS`、`PREVIOUS_PASSWORD_GRACE_HOURS`、`ROTATION_BATCH_SIZE`）都有合理默认值，见 `config/settings.py` 的策略区注释。

---

## 九、项目结构

```
dbops/
├── config/                       # 项目配置
│   ├── settings.py               # 时区三件套、CELERY_BEAT_SCHEDULE、策略参数
│   ├── settings_test.py          # 测试专用（一次性密钥 + 缩短超时）
│   ├── celery.py / urls.py / wsgi.py / asgi.py
├── apps/
│   ├── core/                     # 与业务无关的横切能力
│   │   ├── crypto.py             # Fernet 加解密、多密钥轮换、脱敏
│   │   ├── fields.py             # EncryptedTextField
│   │   ├── middleware.py         # ★ 第 5 题
│   │   └── views.py              # healthz
│   ├── inventory/                # ★ 第 1/2/3 题
│   │   ├── models.py             # Department / Cluster / DBInstance / ProbeLog / PasswordRotationLog
│   │   ├── services.py           # tcp_probe（超时 + 异常分类）
│   │   ├── tasks.py              # 轮换任务（幂等 + 隔离 + 审计）
│   │   ├── serializers.py / views.py / urls.py / admin.py
│   │   └── management/commands/seed_demo.py
│   └── stats/                    # ★ 第 4 题
│       ├── models.py             # InstanceStat（唯一约束）
│       ├── tasks.py              # 聚合 + upsert
│       └── serializers.py / views.py / urls.py
├── tests/                        # 61 项测试（按考点分文件）
├── docker-compose.yml            # 5 个服务（web / worker / beat / db / redis）
├── .github/workflows/ci.yml      # ruff + check + migrations + 双数据库 pytest
├── Dockerfile / requirements*.txt / pytest.ini / pyproject.toml
└── README.md
```

---

## 十、我做过的假设（便于评审对齐）

诚实交代几处题面未明确、我自行决定的地方：

1. **认证方式**：题目未提权限，采用 DRF `TokenAuthentication` + 全局 `IsAuthenticated`，明文凭据接口额外收紧为 `IsAdminUser`。
2. **"每日统计"的口径**：按**服务器本地日期（Asia/Shanghai）**快照当日**当前**实例数量，而非统计过去 24 小时的增量。`stat_date` 支持传参以便补跑历史日期。
3. **密码轮换粒度**：题目说"随机重置"，实现为生成随机强密码并**更新入本机 CMDB**，但**不会真的下推到目标数据库**（那需要各引擎的驱动与账号权限，超出范围）。真实落地在此处留出对接点即可。
4. **双密码过渡期**（24h）是我加的，题面未要求，理由见第 3 题说明。
5. **离线（OFFLINE）实例不参与轮换**：已下线的资产没有轮换意义，避免产生噪声与无效失败。
6. **本地默认 SQLite** 以便零依赖跑通；Docker 与生产走 PostgreSQL（`DATABASE_URL` 切换即可）。

### 若时间充裕，下一步会做什么

- 用 **`django-celery-beat`** 把调度放进数据库，支持运营后台改周期，不用改代码重新发版；
- 凭据取用接入**审批流 + 一次性有效期**，并在 `PasswordRotationLog` 上补操作人 IP；
- 中间件日志改为 JSON 输出并对接 Loki，配 P95/P99 看板与慢请求告警；
- CI（GitHub Actions）已补：`ruff` + `pytest`（SQLite/PostgreSQL 双跑）+ `makemigrations --check` 三连，防止迁移漏提交；下一步可给覆盖率设阈值卡门禁，并增加 Docker 镜像构建与漏洞扫描。
