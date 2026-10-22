# MySQL 慢查询与锁等待排查

## 1. 慢查询定位

### 开启慢查询日志

慢查询日志的参数是 `slow_query_log`，阈值参数是 `long_query_time`，单位是秒，支持小数。

线上建议把阈值设为业务可接受的 P99 上浮一点，例如 0.5 秒，避免日志量过大。

### explain 关注点

`explain` 重点看三列：`type`、`rows`、`Extra`。

`type` 出现 `ALL` 表示全表扫描，出现 `index` 表示扫描整棵索引树，都属于需要优化的信号。

`Extra` 出现 `Using filesort` 说明排序无法用索引完成，出现 `Using temporary` 说明用了临时表。

## 2. 索引失效的常见原因

第一类是隐式类型转换，例如字符串列用数字查询，索引会失效。

第二类是在索引列上使用函数，例如 `where date(created_at) = '2026-01-01'`，应改写为范围查询。

第三类是前导通配的模糊匹配，`like '%abc'` 无法使用索引，`like 'abc%'` 可以。

第四类是联合索引未遵循最左前缀，查询条件缺少最左列时索引无法使用。

## 3. 连接数与锁等待

### 连接数打满

报错 `Too many connections` 表示连接数达到 `max_connections` 上限。

先查 `show processlist` 看是否有大量 Sleep 连接，再确认应用连接池是否未正确归还连接。

### 行锁等待

报错 `Lock wait timeout exceeded` 表示等锁超时，默认阈值 50 秒。

用 `information_schema.innodb_trx` 找到阻塞源事务，确认它是否开启了长事务且未提交。

长事务会同时造成锁等待与 undo 膨胀，属于必须优先处理的问题。
