# Nginx 502 与 504 排查

## 1. 502 与 504 的区别

502 Bad Gateway 表示上游返回了非法响应，或连接被上游重置，Nginx 无法解析响应。

504 Gateway Timeout 表示 Nginx 等待上游响应超时，上游可能仍在处理，只是超过了 `proxy_read_timeout`。

一句话区分：502 是"上游答错了"，504 是"上游没答完"。502 要查上游进程，504 要查耗时与超时配置。

## 2. upstream 配置要点

### keepalive 与连接复用

`upstream` 块中配置 `keepalive` 后，必须在 `location` 中同时设置 `proxy_http_version 1.1` 与 `proxy_set_header Connection ""`，否则长连接不生效。

长连接不生效时，高并发下会出现大量 TIME_WAIT，端口耗尽后表现为偶发 502。

### proxy_read_timeout

`proxy_read_timeout` 默认 60 秒，指两次读操作之间的间隔，不是整个请求的总耗时。

导出大文件或长轮询接口需要显式调大该值，否则会被 Nginx 主动断开，日志中出现 504。

## 3. 排查顺序

第一步看 Nginx 的 `error.log` 中 upstream 的具体错误，第二步确认上游进程是否存活，第三步用 `curl` 直连上游地址绕开 Nginx 验证，最后才检查超时与连接数配置。

`upstream prematurely closed connection` 说明上游主动断开，通常是上游自身的超时或崩溃，而非 Nginx 配置问题。
