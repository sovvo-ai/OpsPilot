# Kubernetes Pod 故障排查手册

## 1. Pod 启动失败的常见状态

### CrashLoopBackOff

CrashLoopBackOff 表示容器启动后反复退出，kubelet 按指数退避重启。

排查顺序：先看 `kubectl describe pod` 的 Last State 与 Exit Code，再看 `kubectl logs --previous` 的上一轮日志。

Exit Code 137 通常是 OOMKilled，或进程收到 SIGKILL；Exit Code 1 多为应用自身启动失败；Exit Code 143 是 SIGTERM，常见于存活探针失败被重启。

### ImagePullBackOff

ImagePullBackOff 表示镜像拉取失败，常见原因有三类：镜像名或 tag 不存在、私有仓库未配置 imagePullSecret、节点到仓库网络不通。

判断方法：`kubectl describe pod` 的 Events 中会给出具体错误，`manifest unknown` 是 tag 不存在，`unauthorized` 是凭据问题。

### Pending

Pending 表示 Pod 未被调度到任何节点。常见原因是资源不足、节点亲和性不满足、PVC 未绑定。

用 `kubectl describe pod` 查看 Events 中的 `FailedScheduling` 原因，可以快速区分是资源不足还是亲和性冲突。

## 2. 内存与探针配置

### resources.limits 与 OOMKilled

容器的内存上限由 `resources.limits.memory` 决定。超过该值会被内核 OOM Killer 终止，表现为 Exit Code 137。

注意：Java 等运行时应设置堆上限低于容器 limit，建议堆为 limit 的 70% 左右，为堆外内存与元空间留余量。

### livenessProbe 与 readinessProbe 的区别

livenessProbe 失败会导致容器重启，readinessProbe 失败只会把 Pod 从 Service 端点摘除，不会重启。

常见错误是把耗时的启动检查放进 livenessProbe，导致应用还没启动完就被反复重启。启动慢的应用应使用 startupProbe 保护。

## 3. 排查顺序

第一步确认状态与重启次数，第二步看 Events，第三步看上一轮日志，第四步确认资源与探针配置，最后才怀疑节点层面问题。

绝大多数 CrashLoopBackOff 能在前三步定位，不需要登录节点。
