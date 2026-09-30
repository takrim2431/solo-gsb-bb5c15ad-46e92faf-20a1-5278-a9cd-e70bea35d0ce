# 工厂校准时间线服务（Calibration Timeline Service）

一个面向工厂校准流水线的 FastAPI 服务。它接收**同一传感器**在校准批次中
**乱序、时间重叠且可能撤回**的测量片段修订（measurement-fragment revisions），
按照**版本因果关系**与**时间区间切分规则**，生成一条**确定性**的有效标定时间线；
当修订引用了尚未上传的父版本时，返回**可补传的缺口清单（gaps）**；
重复提交通过幂等键保证**幂等**；在真正提交前还可以**无副作用地预览**一次修订
会给所属批次带来的影响（preview）；还可对**两个批次的同一传感器**做
**差异对比审计（compare）**，并对单个传感器的折叠时间线做
**质量门禁评估（quality gate）**。全部状态保存在**进程内存**中，可随时重置。

---

## 1. 一条命令启动

需要 Docker（含 Compose v2）：

```bash
docker compose up --build
```

服务监听并对外暴露 **`http://localhost:8080`**：

- 健康检查：<http://localhost:8080/health>
- 交互式 API 文档（Swagger UI）：<http://localhost:8080/docs>
- OpenAPI 描述：<http://localhost:8080/openapi.json>

停止：`Ctrl+C`（或另一终端执行 `docker compose down`）。

> 依赖在镜像构建阶段安装（见 `Dockerfile`），路由、日志等初始化在进程启动时
> 通过 FastAPI lifespan 完成，无需额外迁移步骤。

### 不用 Docker 时（本地 Python 3.11）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

运行测试：

```bash
pip install -r requirements-dev.txt
pytest -q
```

---

## 2. 必要配置

所有配置均为可选，内置默认值即可直接运行。环境变量统一使用 `CAL_` 前缀
（也可在项目根目录放 `.env`，参考 `.env.example`）：

| 变量             | 默认值     | 说明                       |
| ---------------- | ---------- | -------------------------- |
| `CAL_HOST`       | `0.0.0.0` | 监听地址                   |
| `CAL_PORT`       | `8080`     | 监听/映射端口              |
| `CAL_LOG_LEVEL`  | `info`     | 日志级别                   |

数据**只存在内存**：进程重启或调用重置接口后全部丢失，这是预期行为。

---

## 3. 领域语义（确定性规则）

- 修订按 `(batch_id, sensor_id)` 分组，每个修订有恰好一个父修订 `parent_id`；
  字面量 `"ROOT"` 表示因果根。修订 ID 不允许使用保留值 `"ROOT"`。
- 时间区间为**半开区间** `[start_ns, end_ns)`，单位为纳秒整数；
  相邻片段端点相接但不共享任何采样时刻，且要求 `end_ns > start_ns`。
- 修订可以**任意顺序**到达。只有能沿父链追溯到已知 `ROOT` 的修订才是
  **grounded（落地）**状态，参与时间线；
  - 父版本缺失 → 该修订及其后代为 **blocked**，不写入时间线，缺失父版本
    出现在 `gaps` 缺口清单中（含等待它的子版本 `awaited_by` 与引用来源
    `referenced_from`），可直接按清单补传；
  - 父链成环（含自环）→ 环上及其后代标记为 **invalid**，不参与时间线。
- **折叠（fold）顺序完全确定**：先按因果深度（距 ROOT 的跳数）升序，
  同深度按 `revision_id` 字典序升序；后应用者在重叠时间上覆盖先应用者。
  因此最终时间线与事件到达顺序无关（有排列不变性测试保证）。
- **撤回（withdrawn=true）是墓碑**：在其区间内把已有标定值擦除为
  `state="withdrawn"`、`value=null`；撤回修订的后代仍可在因果上重新写回值。
- **幂等**：
  - 相同 `idempotency_key` + 完全相同负载 → 返回 `status="duplicate"`，状态不变；
  - 相同 `idempotency_key` 但负载不同（或同 `revision_id` 内容不同）→ `409 Conflict`；
  - 同一 `revision_id` + 相同内容即使换了幂等键也算重复。
- **提交预览（preview，见 4.2）**：传入与提交完全相同的负载，按相同的校验、
  幂等冲突与因果折叠规则计算，但**不写入任何事件、不占用幂等键、不创建批次**。
  预览返回本次为 `submittable`（可提交）还是 `duplicate`（重复）、预览前后的
  批次 `timeline_hash`、该传感器**有效状态/值/单位**发生变化的半开区间
  （`changed_intervals`；仅获胜来源修订变化不算有效值变化，不输出），以及
  **新增（`gaps_added`）与消除（`gaps_removed`）的缺失父修订清单**。
  尚不存在的批次按空批次预览；被阻塞的候选修订即使不改变时间线，也会在
  `gaps_added` 中反映新增缺口；补传缺失父修订时，原本被阻塞的后代被激活，
  其影响会体现在 `changed_intervals` 与 `gaps_removed` 中。相同修订或幂等键
  的冲突返回与提交一致的 `409`，非法负载返回 `422`；预览与并发提交各自基于
  同一次加锁内的一致批次快照计算。
- 结果中包含两个 SHA-256 内容哈希：每个传感器的 `segments_hash`
  和整批的 `timeline_hash`，可用于比对一致性与去重校验。
- 相邻且状态、值、单位、来源修订完全相同的片段会被合并；来源修订不同则保留边界。
- **采样点查询**（见 4.4）直接复用上述折叠结果判断单个纳秒时刻：
  落在 `calibrated` 片段上返回有效值与单位，落在 `withdrawn` 墓碑上返回
  `withdrawn` 且 `value=null`，所有已落地片段之外为 `uncovered`；
  若该时刻被某个 **blocked** 修订覆盖，则即使已有 grounded 片段也报告
  `blocked`（补传缺失祖先可能改变折叠赢家），并在 `missing_ancestors`
  中按字典序列出沿父链追溯到的、尚未上传的末端祖先，可直接照单补传。
  成环（invalid）修订不影响采样判定。
- **跨批次对比**（见 4.5）复用同一套确定性折叠结果：对两个批次中同一
  传感器的有效时间线做半开区间差异扫描，只比较有效状态
  （`state`/`value`/`unit`），与修订来源（revision_id）及到达顺序无关；
  任一侧存在缺口或成环修订时标记 `incomplete` 并给出原因。
- **质量门禁**（见 4.6）对单个传感器的折叠时间线做确定性扫描：以该传感器
  全部 grounded 与 blocked 修订的**并集跨度**为扫描窗口（成环 invalid 修订
  不参与），逐基本区间判定——`blocked` 覆盖优先（补传可能改变折叠赢家），
  记为缺口；`calibrated` 值落在闭区间 `[min_value, max_value]` 之外记为
  候选告警；`withdrawn` 墓碑与未覆盖空洞记为缺口。相邻候选告警按纳秒邻接
  合并为一段，**仅当持续时长达到 `min_duration_ns`** 才输出为 violation；
  缺口只与相同状态的相邻缺口合并，**绝不并入告警段**。`missing_policy`
  为 `ignore` 时缺口仅记录、不影响判定；为 `fail` 时存在任何缺口即判
  `fail`。响应中的 `timeline_hash` 是对扫描输入（折叠片段 + blocked 跨度）
  的 SHA-256：扫描输入不变则哈希不变，与门禁参数无关。

---

## 4. HTTP 接口

### `POST /api/v1/batches/{batch_id}/events` — 提交一条测量片段修订

请求体：

```json
{
  "batch_id": "B42",
  "sensor_id": "S1",
  "revision_id": "v2",
  "parent_id": "v1",
  "idempotency_key": "evt-20260921-002",
  "start_ns": 100,
  "end_ns": 200,
  "value": 3.5,
  "unit": "C",
  "withdrawn": false
}
```

说明：

- 路径中的 `batch_id` 必须与请求体一致，否则 `422`；
- `withdrawn=false` 时 `value` 必填；`withdrawn=true` 时 `value` 可省略；
- 响应 `200` 中 `status` 为 `accepted` / `duplicate`，并**直接带回该批次当前
  完整结果**（时间线、缺口、计数与哈希），便于流水线即时判断是否可补传；
- 幂等键冲突或同修订 ID 内容冲突返回 `409`；参数非法返回 `422`。

### `POST /api/v1/batches/{batch_id}/events:preview` — 提交前预览修订影响（不写入）

流水线在**真正提交测量修订前**用本接口预览它对所属批次的影响。请求体与
4.1 的提交接口**完全相同**（同样的字段与校验规则；路径 `batch_id` 仍须与
请求体一致），但本接口**不写入事件、不注册幂等键、不创建批次**，可以安全地
反复调用。

判定与计算完全复用提交路径的规则（同一次加锁内完成判定与求值，与并发提交
各自基于一致的批次快照）：

- 相同幂等键 + 相同负载，或相同 `revision_id` + 相同内容 →
  `status="duplicate"`，前后快照一致、变化清单为空；
- 相同幂等键但负载不同，或同 `revision_id` 内容不同 → 与提交一致的 `409`；
- 非法负载（含路径/请求体批次不一致）→ 与提交一致的 `422`；
- 其余情况 `status="submittable"`，按“当前批次 + 候选修订”的**虚拟快照**
  求值。尚不存在的批次按**空批次**预览（空批次 `timeline_hash` 为对 `[]` 的
  SHA-256）。

响应字段：

| 字段 | 说明 |
| --- | --- |
| `status` | `submittable`（新修订，可提交）/ `duplicate`（重复，提交后状态不变） |
| `before_timeline_hash` / `after_timeline_hash` | 预览前 / 预览后（虚拟提交后）的整批 `timeline_hash`，口径与批次结果一致 |
| `before` / `after` | 两个快照的**完整批次结果**（时间线、缺口、计数、各传感器哈希），便于直接比对 |
| `changed_intervals` | 候选修订所属传感器上，**有效状态/值/单位**发生变化的半开区间；仅来源修订（`revision_id`）变化不算有效值变化，不输出 |
| `gaps_added` / `gaps_removed` | 该传感器上**新增 / 消除的缺失父修订缺口**（完整 gap 对象，含 `awaited_by`/`referenced_from`） |

`changed_intervals` 中每个区间的 `kind`：

- `added`：此前未覆盖、预览后被覆盖（`before` 为 `null`）；
- `removed`：此前覆盖、预览后变为未覆盖（`after` 为 `null`；正常新增修订时
  不会出现，仅在形成父环等使修订失效的场景下可能出现）；
- `changed`：两侧都被覆盖，但 `state`/`value`/`unit` 不同。`before`/`after`
  均为 `{state, value, unit}`（`withdrawn` 时 `value`/`unit` 为 `null`）。

相邻且 `kind` 与两侧有效负载完全相同的区间会合并（忽略修订来源边界）。

注意：

- **被阻塞的候选修订**即使不进入折叠、时间线哈希不变，也会在 `gaps_added`
  中给出可补传的缺失父修订；
- **补传缺失父修订**会让此前 blocked 的后代被激活，其折叠影响体现在
  `changed_intervals`，被填补的缺口体现在 `gaps_removed`（链式补传时还可能同时
  在 `gaps_added` 中暴露出更上游的缺口）；
- 预览**绝不改变状态**：冲突的预览不会占用幂等键，之后仍可正常提交原始事件。

示例（尚不存在的批次 B42，先预览一个父修订缺失的子版本 v2）：

```bash
$ curl -s -X POST localhost:8080/api/v1/batches/B42/events:preview \
    -H 'Content-Type: application/json' \
    -d '{"batch_id":"B42","sensor_id":"S1","revision_id":"v2","parent_id":"v1",
         "idempotency_key":"e2","start_ns":100,"end_ns":200,"value":3.5,"unit":"C"}'
{
  "status": "submittable",
  "idempotency_key": "e2",
  "batch_id": "B42",
  "sensor_id": "S1",
  "revision_id": "v2",
  "before_timeline_hash": "4f53cda1…202b945",
  "after_timeline_hash": "7a87bce5…f404f7",
  "before": {"batch_id": "B42", "sensors": [], "gaps": [],
             "revisions_total": 0, "complete": false,
             "timeline_hash": "4f53cda1…202b945"},
  "after":  { … "revisions_blocked": 1, "complete": false, … },
  "changed_intervals": [],
  "gaps_added": [
    {"batch_id": "B42", "sensor_id": "S1", "missing_revision_id": "v1",
     "awaited_by": ["v2"], "referenced_from": "v2"}
  ],
  "gaps_removed": []
}
# 批次仍不存在（GET /result -> 404），幂等键 e2 也未被占用。
```

待 v2 真正提交后，预览补传父版本 v1（根）：缺口 `v1` 消除，此前 blocked 的
v2 被激活，`[0,100)=1.0 | [100,200)=3.5 | [200,300)=1.0` 三段均显示为
`added`：

```bash
$ curl -s -X POST localhost:8080/api/v1/batches/B42/events:preview \
    -H 'Content-Type: application/json' \
    -d '{"batch_id":"B42","sensor_id":"S1","revision_id":"v1","parent_id":"ROOT",
         "idempotency_key":"e1","start_ns":0,"end_ns":300,"value":1.0,"unit":"C"}'
{
  "status": "submittable",
  …
  "gaps_added": [],
  "gaps_removed": [
    {"batch_id": "B42", "sensor_id": "S1", "missing_revision_id": "v1",
     "awaited_by": ["v2"], "referenced_from": "v2"}
  ],
  "changed_intervals": [
    {"start_ns": 0,   "end_ns": 100, "kind": "added", "before": null,
     "after": {"state": "calibrated", "value": 1.0, "unit": "C"}},
    {"start_ns": 100, "end_ns": 200, "kind": "added", "before": null,
     "after": {"state": "calibrated", "value": 3.5, "unit": "C"}},
    {"start_ns": 200, "end_ns": 300, "kind": "added", "before": null,
     "after": {"state": "calibrated", "value": 1.0, "unit": "C"}}
  ]
}
```

### `GET /api/v1/batches/{batch_id}/result` — 查询批次结果

返回该批次所有传感器的有效时间线、缺口清单、修订计数与整批哈希；
批次不存在返回 `404`。

### `GET /api/v1/batches/{batch_id}/sensors/{sensor_id}/sample?timestamp_ns=N` — 查询单个采样时刻

按 `batch_id`、`sensor_id` 与纳秒时刻 `timestamp_ns`（非负整数，半开区间）
查询该时刻的有效标定状态：

| `state`       | 含义                                                                 |
| ------------- | -------------------------------------------------------------------- |
| `calibrated`  | 折叠赢家是已落地的值片段；`value`/`unit` 有效                         |
| `withdrawn`   | 折叠赢家是撤回墓碑；`value`/`unit` 为 `null`                          |
| `uncovered`   | 没有任何已落地片段覆盖该时刻                                          |
| `blocked`     | 该时刻被尚未落地的修订覆盖；补传后赢家可能改变，列出 `missing_ancestors` |

解析成功时（`calibrated` / `withdrawn`）还返回：

- `revision_id`：该时刻的获胜修订（确定性折叠中最后应用者）；
- `parent_chain`：从获胜修订回溯到根的**有序**父链，含首尾，
  例如 `["v3", "v2", "v1", "ROOT"]`。

`blocked` 时 `revision_id`、`value`、`unit` 为 `null`，`parent_chain` 为空，
`missing_ancestors` 按字典序列出去重后的缺失末端祖先。
未知批次或未知传感器返回 `404`；`timestamp_ns < 0` 返回 `422`。

示例（沿用第 5 节的 B42 / S1：v2 覆盖 [100,200)、v1 覆盖 [0,300)）：

```bash
$ curl -s 'localhost:8080/api/v1/batches/B42/sensors/S1/sample?timestamp_ns=150'
{
  "batch_id": "B42",
  "sensor_id": "S1",
  "timestamp_ns": 150,
  "state": "calibrated",
  "value": 3.5,
  "unit": "C",
  "revision_id": "v2",
  "parent_chain": ["v2", "v1", "ROOT"],
  "missing_ancestors": []
}
```

父版本 `v1` 缺失时查询同一时刻：

```json
{
  "batch_id": "B42",
  "sensor_id": "S1",
  "timestamp_ns": 150,
  "state": "blocked",
  "value": null,
  "unit": null,
  "revision_id": null,
  "parent_chain": [],
  "missing_ancestors": ["v1"]
}
```

### `GET /api/v1/batches/compare?left_batch_id=...&right_batch_id=...&sensor_id=...` — 跨批次对比审计

对**同一传感器**在两个批次中的有效时间线（复用第 3 节的确定性折叠结果）
做半开区间差异扫描。`intervals` 是两侧覆盖范围并集 `[min_start, max_end)`
的**连续、按时间排序的分区**：每个区间带 `kind` 以及两侧各自的有效负载
（`state`/`value`/`unit`，未覆盖的一侧为 `null`）；`kind` 与两侧负载完全
相同的相邻区间会被合并（合并与折叠片段的修订来源边界无关）。

| `kind`           | 含义                                                              |
| ---------------- | ----------------------------------------------------------------- |
| `equal`          | 两侧有效状态一致（都未覆盖也算一致，此时两侧负载均为 `null`）      |
| `left_only`      | 仅左批次覆盖该区间                                                 |
| `right_only`     | 仅右批次覆盖该区间                                                 |
| `value_mismatch` | 两侧均为 `calibrated`，但 `value` 或 `unit` 不同                   |
| `state_mismatch` | 一侧为 `calibrated`、另一侧为 `withdrawn`                          |

响应还包含：

- `left_segments_hash` / `right_segments_hash`：两侧折叠片段的内容哈希，
  与批次结果中的 `segments_hash` 一致，便于交叉校验；
- `incomplete` 与 `incomplete_reasons`：任一侧存在缺口（修订 blocked，
  `reason="gaps"`，含可补传的 `missing_revision_ids`）或成环修订
  （`reason="invalid_revisions"`）时 `incomplete=true`——补传后差异结果
  可能改变；差异本身仍按当前已落地的部分计算。

参数缺失或为空返回 `422`；任一批次不存在、或传感器在任一批次中不存在
返回 `404`。

示例（B42 与 B43 的 S1：B42 为 [0,300)=1.0 且 [100,200)=3.5，B43 整体为
[0,300)=1.0）：

```bash
$ curl -s 'localhost:8080/api/v1/batches/compare?left_batch_id=B42&right_batch_id=B43&sensor_id=S1'
{
  "sensor_id": "S1",
  "left_batch_id": "B42",
  "right_batch_id": "B43",
  "left_segments_hash": "…",
  "right_segments_hash": "…",
  "incomplete": false,
  "incomplete_reasons": [],
  "intervals": [
    {"start_ns": 0, "end_ns": 100, "kind": "equal",
     "left": {"state": "calibrated", "value": 1.0, "unit": "C"},
     "right": {"state": "calibrated", "value": 1.0, "unit": "C"}},
    {"start_ns": 100, "end_ns": 200, "kind": "value_mismatch",
     "left": {"state": "calibrated", "value": 3.5, "unit": "C"},
     "right": {"state": "calibrated", "value": 1.0, "unit": "C"}},
    {"start_ns": 200, "end_ns": 300, "kind": "equal",
     "left": {"state": "calibrated", "value": 1.0, "unit": "C"},
     "right": {"state": "calibrated", "value": 1.0, "unit": "C"}}
  ]
}
```

### `POST /api/v1/batches/{batch_id}/quality-gate` — 质量门禁评估

对指定批次中**单个传感器**的折叠时间线执行确定性质量门禁扫描（规则见第 3 节）。

请求体：

```json
{
  "sensor_id": "S1",
  "min_value": 0.0,
  "max_value": 3.0,
  "min_duration_ns": 100,
  "missing_policy": "ignore"
}
```

参数说明：

- `min_value` / `max_value`：允许值的**闭区间**上下限（有限浮点数，拒绝
  NaN/Inf），且必须满足 `min_value <= max_value`；
- `min_duration_ns`：最短持续时长（整数，≥ 1）。超界连续段合并后达到该
  时长才输出为 violation，未达阈值的短暂越界被抑制；
- `missing_policy`：缺失数据策略。`ignore` = 缺口（`withdrawn` /
  `uncovered` / `blocked`）仅记录；`fail` = 存在任何缺口即判 `fail`。

响应字段：

- `status`：`pass` / `fail`。存在 violation 即 `fail`；`missing_policy=fail`
  且存在缺口时亦为 `fail`；
- `violations`：合并后的超界区间，含 `start_ns`/`end_ns`/`duration_ns`
  及区间内观测到的极值 `min_value`/`max_value`；
- `gaps`：合并后的缺失数据区间，含 `state`；`blocked` 区间另附去重排序的
  `missing_ancestors`（可直接照单补传）；
- `scanned_start_ns` / `scanned_end_ns`：实际扫描的半开窗口（无可扫描
  内容时为 `null`）；
- `timeline_hash`：扫描输入的内容哈希，可用于审计与一致性比对。

批次不存在或传感器不存在返回 `404`；参数非法（含上下限颠倒、阈值小于 1、
未知策略、多余字段）返回 `422`。

示例（沿用第 5 节的 B42 / S1：v2 在 [100,200) 取值 3.5，超出上限 3.0）：

```bash
$ curl -s -X POST localhost:8080/api/v1/batches/B42/quality-gate \
    -H 'Content-Type: application/json' \
    -d '{"sensor_id":"S1","min_value":0.0,"max_value":3.0,
         "min_duration_ns":100,"missing_policy":"ignore"}'
{
  "batch_id": "B42",
  "sensor_id": "S1",
  "status": "fail",
  "scanned_start_ns": 0,
  "scanned_end_ns": 300,
  "violations": [
    {"start_ns": 100, "end_ns": 200, "duration_ns": 100,
     "min_value": 3.5, "max_value": 3.5}
  ],
  "gaps": [],
  "timeline_hash": "…"
}
```

### `POST /admin/reset` — 清空全部内存状态

返回被清除的批次数与事件数。

### `GET /health` — 存活检查

---

## 5. 典型使用流程

```bash
# 0) 提交前先预览：父修订 v1 缺失 → status="submittable"，gaps_added=[v1]，
#    changed_intervals 为空（blocked 不进时间线）；不写入事件、不占用幂等键
curl -s -X POST localhost:8080/api/v1/batches/B42/events:preview \
  -H 'Content-Type: application/json' \
  -d '{"batch_id":"B42","sensor_id":"S1","revision_id":"v2","parent_id":"v1",
       "idempotency_key":"e2","start_ns":100,"end_ns":200,"value":3.5,"unit":"C"}'

# 1) 子版本先到（父 v1 缺失）→ 返回缺口 v1，时间线为空
curl -s -X POST localhost:8080/api/v1/batches/B42/events \
  -H 'Content-Type: application/json' \
  -d '{"batch_id":"B42","sensor_id":"S1","revision_id":"v2","parent_id":"v1",
       "idempotency_key":"e2","start_ns":100,"end_ns":200,"value":3.5,"unit":"C"}'

# 2) 同一请求重放（提交或预览均可）→ "status":"duplicate"，结果不变

# 3) 正式补传前再预览父版本 v1：gaps_removed=[v1]、三段 added、
#    after.complete=true；预览不改变任何状态，确认后再真正提交
curl -s -X POST localhost:8080/api/v1/batches/B42/events:preview \
  -H 'Content-Type: application/json' \
  -d '{"batch_id":"B42","sensor_id":"S1","revision_id":"v1","parent_id":"ROOT",
       "idempotency_key":"e1","start_ns":0,"end_ns":300,"value":1.0,"unit":"C"}'

# 3b) 按缺口清单补传父版本 v1（根）→ complete=true，时间线变为
#    [0,100)=1.0(v1) | [100,200)=3.5(v2) | [200,300)=1.0(v1)
curl -s -X POST localhost:8080/api/v1/batches/B42/events \
  -H 'Content-Type: application/json' \
  -d '{"batch_id":"B42","sensor_id":"S1","revision_id":"v1","parent_id":"ROOT",
       "idempotency_key":"e1","start_ns":0,"end_ns":300,"value":1.0,"unit":"C"}'

# 4) 查询 / 采样点查询 / 重置
curl -s localhost:8080/api/v1/batches/B42/result
curl -s 'localhost:8080/api/v1/batches/B42/sensors/S1/sample?timestamp_ns=150'
#   -> state="calibrated", value=3.5, revision_id="v2",
#      parent_chain=["v2","v1","ROOT"]

# 5) 质量门禁：S1 允许值闭区间 [0,3]，超界持续 ≥100ns 才计违规
curl -s -X POST localhost:8080/api/v1/batches/B42/quality-gate \
  -H 'Content-Type: application/json' \
  -d '{"sensor_id":"S1","min_value":0.0,"max_value":3.0,
       "min_duration_ns":100,"missing_policy":"ignore"}'
#   -> status="fail", violations=[{"start_ns":100,"end_ns":200,
#      "duration_ns":100,"min_value":3.5,"max_value":3.5}], gaps=[]
curl -s -X POST localhost:8080/admin/reset
```

撤回某一段（墓碑，区间内被擦除）：

```json
{
  "batch_id": "B42", "sensor_id": "S1", "revision_id": "w1",
  "parent_id": "v2", "idempotency_key": "w1",
  "start_ns": 120, "end_ns": 180, "withdrawn": true
}
```

---

## 6. 项目结构

```
.
├── app/
│   ├── main.py       # FastAPI 路由：ingest / preview / result / sample / compare / quality-gate / reset / health
│   ├── engine.py     # 因果分类、区间切分折叠、预览影响计算、采样点查询、跨批次差异对比、质量门禁扫描、缺口与哈希（纯函数引擎）
│   ├── storage.py    # 线程安全的内存存储、幂等判定与无副作用的提交预览判定
│   ├── models.py     # Pydantic 请求/响应模型与校验
│   └── config.py     # CAL_ 前缀环境变量配置
├── tests/            # pytest（API 端到端 + 引擎单测，共 104 个用例）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── requirements-dev.txt
└── .env.example
```
