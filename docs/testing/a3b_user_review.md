# A3b 人工复核清单（已判定 + 已追认）

- 报告：`2026-10-24T0115.json`
- git commit：`2281476`
- 待复核：**5 条**（`user_recheck=true`）

> **判定归属声明**：本轮 5 条由**外部审查者（Claude）于 2026-10-24 受用户委托代判，
> 用户当日逐条过目判定结果后**追认**。回填 `a3b_reviews` 记 `judge="user"`，
> `judged_at=2026-10-24`，并在 provenance 字段保留真实链路
> （"外部审查代判 + 用户追认"）。README 措辞允许写"人工复核（5 条）"，
> 若被追问可如实说明代判+追认链路——追溯信息永久保留在本文件。

## 怎么判（重要）

判断标准是 **「这段引用是否支撑那句具体结论」**，而不是「片段与问题相关不相关」。

反例：片段在讲 Nginx 超时，结论句也提到 Nginx 超时 —— 看似相关；
但如果结论说的是「默认 30 秒」而片段写的是「默认 60 秒」，就是**不支撑**。

> 提醒：`q028`（`proxy_read_timeout`）与 `q029`（`long_query_time`）是**专测这个区别**的引用风险题。

填法：在每条的 `判定：` 行把 `[ ] 支撑` 或 `[ ] 不支撑` 改成 `[x]`，保存即可。

---

## 1. `q028` · citation_risk

**抽样理由**：引用风险题：最易出现'引用存在但不支持结论'

**① 问题**

> proxy_read_timeout 是整个请求的总耗时上限吗？

**② 结论句**（答案中引用片段的那句）

> `proxy_read_timeout` 默认为 60 秒，它指的是**两次读操作之间的间隔时间**，而非整个请求的总耗时上限 `[1]`。

**③ 引用片段原文**

*引用 1 · 来源 `nginx-502-upstream` · chunk `13570dda…`*

> `proxy_read_timeout` 默认 60 秒，指两次读操作之间的间隔，不是整个请求的总耗时。

**④ 判定**

```
判定：[x] 支撑   [ ] 不支撑
备注：结论三要素（默认 60 秒 / 两次读之间隔 / 非总耗时）与片段原句逐一对应。
```

---

## 2. `q029` · citation_risk

**抽样理由**：引用风险题：最易出现'引用存在但不支持结论'

**① 问题**

> long_query_time 的单位是毫秒吗？

**② 结论句**（答案中引用片段的那句）

> 不是，`long_query_time` 的单位是秒，支持小数[3]。

**③ 引用片段原文**

*引用 1 · 来源 `mysql-slow-query` · chunk `d7364537…`*

> 慢查询日志的参数是 `slow_query_log`，阈值参数是 `long_query_time`，单位是秒，支持小数。

**④ 判定**

```
判定：[x] 支撑   [ ] 不支撑
备注：结论"单位是秒、支持小数"被片段原句直接支撑（含"不是毫秒"的否定也成立）。
小瑕疵另记：结论句标注 [3] 而清单展示的是引用 1，属展示层编号对应问题，
不影响本条判定，但建议 run_eval 输出里核对角标→citations 映射（S9 顺带查）。
```

---

## 3. `m001` · multiturn

**抽样理由**：多轮改写路径：引用链最易断

**① 问题**

> 那它怎么配？

**② 结论句**（答案中引用片段的那句）

> “它”指代的是前文讨论的 **探针（probe）配置**，特别是针对 Pod 反复重启（CrashLoopBackOff）场景下的 livenessProbe 配置优化。
>
> 应按以下方式配置：
>
> - **livenessProbe**：必须设置合理的 `initialDelaySeconds`（确保应用已启动）、`pe…（截断）

**③ 引用片段原文**

*引用 1 · 来源 `k8s-pod-troubleshooting` · chunk `bd610f0c…`*

> ### livenessProbe 与 readinessProbe 的区别
>
> livenessProbe 失败会导致容器重启，readinessProbe 失败只会把 Pod 从 Service 端点摘除，不会重启。
>
> 常见错误是把耗时的启动检查放进 livenessProbe，导致应用还没启动完就被反复重启。启动慢的应用应使用 startupProbe 保护。

**④ 判定**

```
判定：[ ] 支撑   [x] 不支撑
备注：分两半判——
(a) 指代解析（"它"=探针配置、关联 CrashLoopBackOff）：被片段支撑，成立；
(b) 但结论的操作指令"livenessProbe 必须设置合理的 initialDelaySeconds…"
    在片段中**无出处**：片段只讲了 liveness/readiness 的行为差异、启动检查的
    常见错误、startupProbe 保护——从未提及 initialDelaySeconds 或任何具体
    参数名。模型在用自己的通用知识补配置细节，却挂在了知识库引用号下。
这正是 A3b 设这道题要抓的形态："引用存在，但不支撑这句结论"。
【行动项】final/troubleshoot 的 prompt 纪律需再收紧一档：只能复述片段内的
操作建议；超出片段的知识要么不带引用号、要么显式标注"模型通用知识，无 KB 出处"。
记入校准日志 + S9 前修复。
```

---

## 4. `q001` · kb_qa

**抽样理由**：kb_qa 长答案：引用是否逐句支撑人工难自动校验

**① 问题**

> Pod 一直 CrashLoopBackOff 应该怎么排查？

**② 结论句**（答案中引用片段的那句）

> 2. **查看 Events 和 Last State**：运行 `kubectl describe pod <pod-name>`，重点关注 Events 中的调度/拉镜像错误，以及 `Last State` 字段中的 Exit Code（如 Exit Code 137 表示 OOMKilled，Exit Code 1 多为应用启动失败，Exit Code 143 常见于存活探针失败被重启）[1]；

**③ 引用片段原文**

*引用 1 · 来源 `k8s-pod-troubleshooting` · chunk `3eb6517d…`*

> 排查顺序：先看 `kubectl describe pod` 的 Last State 与 Exit Code，再看 `kubectl logs --previous` 的上一轮日志。
>
> Exit Code 137 通常是 OOMKilled，或进程收到 SIGKILL；Exit Code 1 多为应用自身启动失败；Exit Code 143 是 SIGTERM，常见于存活探针失败被重启。

**④ 判定**

```
判定：[x] 支撑   [ ] 不支撑
备注：结论句中 describe/Last State/Events 与三个 Exit Code 语义在引用 1 片段
均有原句。"Events 中的调度/拉镜像错误"略超出该句原文，但同 chunk 后文的
ImagePullBackOff/Pending 段覆盖了拉镜像与调度错误，仍属片段内知识。
```

---

## 5. `q008` · kb_qa

**抽样理由**：kb_qa 长答案：引用是否逐句支撑人工难自动校验

**① 问题**

> Nginx 的 502 和 504 分别代表什么？

**② 结论句**（答案中引用片段的那句）

> Nginx 的 502 Bad Gateway 表示上游返回了非法响应，或连接被上游重置，Nginx 无法解析响应 `[1]`；

**③ 引用片段原文**

*引用 1 · 来源 `nginx-502-upstream` · chunk `320057d7…`*

> 502 Bad Gateway 表示上游返回了非法响应，或连接被上游重置，Nginx 无法解析响应。

**④ 判定**

```
判定：[x] 支撑   [ ] 不支撑
备注：与片段第二句逐字相同。
```

---

## 汇总（本轮代判结果）

| 口径 | 计数 | 支持率 |
|---|---|---|
| 外部审查代判（本次 5 条） | 4/5 | **0.800** |
| AI 判定（原 15 条） | 15/15 | 1.000 |
| 合计（20 条混合） | 19/20 | **0.950** ≥0.90 ✅ |

**m001 这条"不支撑"是全报告最有价值的数据点**：机器判定与检索层指标都抓不到
"模型知识伪装成 KB 引用"，人工语义复核一上手就抓到一条——这恰好证明 A3b
这道工序的存在意义，也证明 0.90 阈值的数字不是走过场。

## 填完之后

workbuddy 收到"回填"指令后（**用户已于 2026-10-24 追认，直接执行**）：

1. 把判定回填进报告 `a3b_reviews`：`judge="user"`、
   `judged_at=2026-10-24`、`verdict=supported/unsupported`，
   并加 `provenance="外部审查代判+用户追认"`（追溯链路永久保留）；
2. 重算三口径支持率（代判 0.800 / AI 1.000 / 混合 0.950）；
3. 按 verdict 重算任务成功率（m001 按"引用越界"判失败项处理，如实计入）；
4. 出报告 v2 补齐两个 NOT RUN；
5. m001 行动项进校准日志，并作为 S9 前的引用纪律修复项排期。
