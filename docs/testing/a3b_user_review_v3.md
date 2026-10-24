# A3b 人工复核清单（待你打勾）

- 报告：`2026-10-24T1405.json`
- git commit：`0eb7b91`
- 待复核：**5 条**（`user_recheck=true`）

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

> `proxy_read_timeout` 默认 60 秒，指两次读操作之间的间隔，不是整个请求的总耗时[1]。

**③ 引用片段原文**

*引用 1 · 来源 `nginx-502-upstream` · chunk `86dd2e5a…`*

> n` 中同时设置 `proxy_http_version 1.1` 与 `proxy_set_header Connection ""`，否则长连接不生效。
> 
> 长连接不生效时，高并发下会出现大量 TIME_WAIT，端口耗尽后表现为偶发 502。
> 
> ### proxy_read_timeout
> 
> `proxy_read_timeout` 默认 60 秒，指两次读操作之间的间隔，不是整个请求的总耗时。
> 
> 导出大文件或长轮询接口需要显式调大该值，否则会被 Nginx 主动断开，日志中出现 504。
> 
> ## 3. 排查顺序
> 
> 第一步看 Nginx 的 `error.log` 中 upstream 的具体错误，第二步确认上游进程是否存活，第三步用 `curl` 直连上游地址绕开 Nginx 验证，最后才检查超时与连接数配置。
> 
> `upstream prematurely closed connection` 说明上游主动断开，通常是上游自身的超时或崩溃，而非 Nginx 配置问题。

**④ 判定**

```
判定：[ ] 支撑   [ ] 不支撑
备注：（可选）
```

---

## 2. `q029` · citation_risk

**抽样理由**：引用风险题：最易出现'引用存在但不支持结论'

**① 问题**

> long_query_time 的单位是毫秒吗？

**② 结论句**（答案中引用片段的那句）

> 不是，`long_query_time` 的单位是秒，支持小数 `[3]`。

**③ 引用片段原文**

*引用 1 · 来源 `mysql-slow-query` · chunk `6d4d7f0c…`*

> # MySQL 慢查询与锁等待排查
> 
> ## 1. 慢查询定位
> 
> ### 开启慢查询日志
> 
> 慢查询日志的参数是 `slow_query_log`，阈值参数是 `long_query_time`，单位是秒，支持小数。
> 
> 线上建议把阈值设为业务可接受的 P99 上浮一点，例如 0.5 秒，避免日志量过大。
> 
> ### explain 关注点
> 
> `explain` 重点看三列：`type`、`rows`、`Extra`。
> 
> `type` 出现 `ALL` 表示全表扫描，出现 `index` 表示扫描整棵索引树，都属于需要优化的信号。
> 
> `Extra` 出现 `Using filesort` 说明排序无法用索引完成，出现 `Using temporary` 说明用了临时表。
> 
> ## 2. 索引失效的常见原因
> 
> 第一类是隐式类型转换，例如字符串列用数字查询，索引会失效。
> 
> 第二类是在索引列上使用函数，例如 `where date(created_at) = '2026-01-01'`，应改写为范围查询。

**④ 判定**

```
判定：[ ] 支撑   [ ] 不支撑
备注：（可选）
```

---

## 3. `m001` · multiturn

**抽样理由**：多轮改写路径：引用链最易断

**① 问题**

> 那它怎么配？

**② 结论句**（答案中引用片段的那句）

> 启动慢的应用应改用 `startupProbe` 提供启动宽限期 `[1]`。

**③ 引用片段原文**

*引用 1 · 来源 `k8s-pod-troubleshooting` · chunk `ea4c0bd2…`*

> zed` 是凭据问题。
> 
> ### Pending
> 
> Pending 表示 Pod 未被调度到任何节点。常见原因是资源不足、节点亲和性不满足、PVC 未绑定。
> 
> 用 `kubectl describe pod` 查看 Events 中的 `FailedScheduling` 原因，可以快速区分是资源不足还是亲和性冲突。
> 
> ## 2. 内存与探针配置
> 
> ### resources.limits 与 OOMKilled
> 
> 容器的内存上限由 `resources.limits.memory` 决定。超过该值会被内核 OOM Killer 终止，表现为 Exit Code 137。
> 
> 注意：Java 等运行时应设置堆上限低于容器 limit，建议堆为 limit 的 70% 左右，为堆外内存与元空间留余量。
> 
> ### livenessProbe 与 readinessProbe 的区别
> 
> livenessProbe 失败会导致容器重启，readinessProbe 失败只会把 Pod 从 Service 端点摘除，不会重启。
> 
> 常见错误是把耗时的启动检查放进 livenessProbe，导致应用还没启动完就被反复重启。启动慢的应用应使用 startupProbe 保护。
> 
> ## 3. 排查顺序
> 
> 第一步确认状态与重启次数，第二步看 Events，第三步看上一轮日志，第四步确认资源与探针配置，最后才怀疑节点层面问题。
> 
> 绝大多数 CrashLoopBackOff 能在前三步定位，不需要登录节点。

*引用 2 · 来源 `k8s-pod-troubleshooting` · chunk `55b061c8…`*

> # Kubernetes Pod 故障排查手册
> 
> ## 1. Pod 启动失败的常见状态
> 
> ### CrashLoopBackOff
> 
> CrashLoopBackOff 表示容器启动后反复退出，kubelet 按指数退避重启。
> 
> 排查顺序：先看 `kubectl describe pod` 的 Last State 与 Exit Code，再看 `kubectl logs --previous` 的上一轮日志。
> 
> Exit Code 137 通常是 OOMKilled，或进程收到 SIGKILL；Exit Code 1 多为应用自身启动失败；Exit Code 143 是 SIGTERM，常见于存活探针失败被重启。
> 
> ### ImagePullBackOff
> 
> ImagePullBackOff 表示镜像拉取失败，常见原因有三类：镜像名或 tag 不存在、私有仓库未配置 imagePullSecret、节点到仓库网络不通。
> 
> 判断方法：`kubectl describe pod` 的 Events 中会给出具体错误，`manifest unknown` 是 tag 不存在，`unauthorized` 是凭据问题。
> 
> ### Pending
> 
> Pending 表示 Pod 未被调度到任何节点。常见原因是资源不足、节点亲和性不满足、PVC 未绑定。

**④ 判定**

```
判定：[ ] 支撑   [ ] 不支撑
备注：（可选）
```

---

## 4. `q001` · kb_qa

**抽样理由**：kb_qa 长答案：引用是否逐句支撑人工难自动校验

**① 问题**

> Pod 一直 CrashLoopBackOff 应该怎么排查？

**② 结论句**（答案中引用片段的那句）

> - Exit Code 143 是 SIGTERM，常见于存活探针（livenessProbe）失败后被重启 `[1]`。

**③ 引用片段原文**

*引用 1 · 来源 `k8s-pod-troubleshooting` · chunk `55b061c8…`*

> # Kubernetes Pod 故障排查手册
> 
> ## 1. Pod 启动失败的常见状态
> 
> ### CrashLoopBackOff
> 
> CrashLoopBackOff 表示容器启动后反复退出，kubelet 按指数退避重启。
> 
> 排查顺序：先看 `kubectl describe pod` 的 Last State 与 Exit Code，再看 `kubectl logs --previous` 的上一轮日志。
> 
> Exit Code 137 通常是 OOMKilled，或进程收到 SIGKILL；Exit Code 1 多为应用自身启动失败；Exit Code 143 是 SIGTERM，常见于存活探针失败被重启。
> 
> ### ImagePullBackOff
> 
> ImagePullBackOff 表示镜像拉取失败，常见原因有三类：镜像名或 tag 不存在、私有仓库未配置 imagePullSecret、节点到仓库网络不通。
> 
> 判断方法：`kubectl describe pod` 的 Events 中会给出具体错误，`manifest unknown` 是 tag 不存在，`unauthorized` 是凭据问题。
> 
> ### Pending
> 
> Pending 表示 Pod 未被调度到任何节点。常见原因是资源不足、节点亲和性不满足、PVC 未绑定。

*引用 2 · 来源 `k8s-pod-troubleshooting` · chunk `ea4c0bd2…`*

> zed` 是凭据问题。
> 
> ### Pending
> 
> Pending 表示 Pod 未被调度到任何节点。常见原因是资源不足、节点亲和性不满足、PVC 未绑定。
> 
> 用 `kubectl describe pod` 查看 Events 中的 `FailedScheduling` 原因，可以快速区分是资源不足还是亲和性冲突。
> 
> ## 2. 内存与探针配置
> 
> ### resources.limits 与 OOMKilled
> 
> 容器的内存上限由 `resources.limits.memory` 决定。超过该值会被内核 OOM Killer 终止，表现为 Exit Code 137。
> 
> 注意：Java 等运行时应设置堆上限低于容器 limit，建议堆为 limit 的 70% 左右，为堆外内存与元空间留余量。
> 
> ### livenessProbe 与 readinessProbe 的区别
> 
> livenessProbe 失败会导致容器重启，readinessProbe 失败只会把 Pod 从 Service 端点摘除，不会重启。
> 
> 常见错误是把耗时的启动检查放进 livenessProbe，导致应用还没启动完就被反复重启。启动慢的应用应使用 startupProbe 保护。
> 
> ## 3. 排查顺序
> 
> 第一步确认状态与重启次数，第二步看 Events，第三步看上一轮日志，第四步确认资源与探针配置，最后才怀疑节点层面问题。
> 
> 绝大多数 CrashLoopBackOff 能在前三步定位，不需要登录节点。

**④ 判定**

```
判定：[ ] 支撑   [ ] 不支撑
备注：（可选）
```

---

## 5. `q002` · kb_qa

**抽样理由**：kb_qa 长答案：引用是否逐句支撑人工难自动校验

**① 问题**

> Exit Code 137 一般代表什么？

**② 结论句**（答案中引用片段的那句）

> Exit Code 137 通常表示容器因内存超限被内核 OOM Killer 终止，即 OOMKilled [1]。

**③ 引用片段原文**

*引用 1 · 来源 `k8s-pod-troubleshooting` · chunk `ea4c0bd2…`*

> zed` 是凭据问题。
> 
> ### Pending
> 
> Pending 表示 Pod 未被调度到任何节点。常见原因是资源不足、节点亲和性不满足、PVC 未绑定。
> 
> 用 `kubectl describe pod` 查看 Events 中的 `FailedScheduling` 原因，可以快速区分是资源不足还是亲和性冲突。
> 
> ## 2. 内存与探针配置
> 
> ### resources.limits 与 OOMKilled
> 
> 容器的内存上限由 `resources.limits.memory` 决定。超过该值会被内核 OOM Killer 终止，表现为 Exit Code 137。
> 
> 注意：Java 等运行时应设置堆上限低于容器 limit，建议堆为 limit 的 70% 左右，为堆外内存与元空间留余量。
> 
> ### livenessProbe 与 readinessProbe 的区别
> 
> livenessProbe 失败会导致容器重启，readinessProbe 失败只会把 Pod 从 Service 端点摘除，不会重启。
> 
> 常见错误是把耗时的启动检查放进 livenessProbe，导致应用还没启动完就被反复重启。启动慢的应用应使用 startupProbe 保护。
> 
> ## 3. 排查顺序
> 
> 第一步确认状态与重启次数，第二步看 Events，第三步看上一轮日志，第四步确认资源与探针配置，最后才怀疑节点层面问题。
> 
> 绝大多数 CrashLoopBackOff 能在前三步定位，不需要登录节点。

**④ 判定**

```
判定：[ ] 支撑   [ ] 不支撑
备注：（可选）
```

---

## 填完之后

告诉我一声，我会：

1. 把判定回填进报告 `a3b_reviews`（`judge="user"`、`judged_at=<时间>`、`verdict=supported/unsupported`）
2. 重算**用户口径**与**混合口径**的「引用内容支持率」
3. 按 `verdict` 重算「任务成功率」（支撑 + 引用有效 + 无幻觉）
4. 出报告 v2，补齐那两个 `NOT RUN`
