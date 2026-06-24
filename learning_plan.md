# 20-30 天求职准备计划

> 时间：2026/06/24
> 目标：20-30 天内完成监控项目搭建 + Python 能力恢复 + 算法题准备
> 求职方向：售后工程师 / 运维工程师 / 运维开发（1-3 年经验，15K 左右）

## 核心原则：以输出倒逼输入

**不要看视频、不要看书、不要"系统学习"。直接做题、直接写代码，卡住了再查。**

---

## 一、Python 速成策略（每天 1-1.5 小时）

### 只需要掌握这些（运维开发面试高频）

每个知识点 30 分钟内能讲清楚：

| 优先级 | 知识点 | 面试怎么考 |
|---|---|---|
| 🔥 P0 | list / dict / set / tuple 区别 + 常用操作 | "dict 和 list 的时间复杂度？" |
| 🔥 P0 | 字符串处理（split/join/strip/正则）| 写个脚本解析日志 |
| 🔥 P0 | 文件读写（with open）| 处理日志文件 |
| 🔥 P0 | 函数参数（*args, **kwargs, 默认值）| 写个工具函数 |
| ⚠️ P1 | 装饰器（decorator）| "写个计时装饰器" |
| ⚠️ P1 | 生成器（yield）| "大文件怎么逐行读？" |
| ⚠️ P1 | 异常处理（try/except/finally）| "资源怎么保证释放？" |
| ⚠️ P1 | 上下文管理器（with）| "自己实现一个 context manager" |
| 🔵 P2 | GIL 是什么 | 八股文，背 |
| 🔵 P2 | 多线程 vs 多进程 | "IO 密集用哪个？CPU 密集用哪个？" |
| 🔵 P2 | 闭包 | 装饰器的前置知识 |

### 练习方法

**每天写 3 个小脚本**，题目来源：

1. **把 auto_packing 里的代码重新手写一遍**（最推荐）
   - 重写 Redis 连接、重写 subprocess 调用、重写文件打包
   - 既练 Python，又复习项目

2. **LeetCode Easy 里的字符串 / 数组题**（一石二鸟）

3. **运维相关的小工具**：
   - 解析 /var/log/syslog，统计 ERROR 出现次数
   - 批量重命名文件
   - 监控某个端口是否通，不通就发告警

### 推荐资源（只看这些）

- **Python 官方教程** 的 4-9 章（快速过一遍，1-2 小时）
- **菜鸟教程 Python3**（当手册查，不要通读）
- **不要看**：《流畅的 Python》、《Python Cookbook》—— 太深，时间不够

---

## 二、算法题策略（每天 1-1.5 小时）

### 运维开发岗的真实情况

- 大部分公司 **不考算法**，或者只考 **LeetCode Easy**
- 少数公司考 Medium，但能做出 Easy + 背几道经典 Medium 就够了
- **30 天刷 50-60 题足够**，不要贪多

### 按模式刷题（不要随机刷）

| 周次 | 模式 | 题数 | 代表题 |
|---|---|---|---|
| 第 1 周 | 数组 / 字符串 | 12 题 | 两数之和、有效的括号、最长无重复子串 |
| 第 2 周 | 双指针 / 滑动窗口 | 10 题 | 移动零、三数之和、最小覆盖子串 |
| 第 3 周 | 链表 / 栈 / 队列 | 10 题 | 反转链表、合并有序链表、最小栈 |
| 第 4 周 | 二叉树 / 哈希 | 10 题 | 二叉树层序遍历、最长连续序列 |
| 第 5 周（如有）| 动态规划入门 | 8 题 | 爬楼梯、零钱兑换、最长递增子序列 |

### 刷题方法

**每题 25 分钟，做不出来直接看答案，理解后自己重写一遍。**

不要死磕！运维开发岗算法不是重点。

### 必刷清单（30 题，按顺序）

1. 两数之和
2. 有效的括号
3. 合并两个有序数组
4. 爬楼梯
5. 二叉树的最大深度
6. 反转链表
7. 有效的字母异位词
8. 二分查找
9. 斐波那契数
10. 移动零
11. 删除排序数组中的重复项
12. 买卖股票的最佳时机
13. 加一
14. x 的平方根
15. 字母异位词分组
16. 三数之和
17. 最长回文子串
18. 盛最多水的容器
19. 三数之和（排序 + 双指针）
20. 最小覆盖子串（滑动窗口，Hard 但经典）
21. 合并区间
22. 字符串相乘
23. 二叉树的层序遍历
24. 验证二叉搜索树
25. 二叉树的最近公共祖先
26. 岛屿数量（BFS/DFS）
27. 最长递增子序列
28. 零钱兑换
29. 单词搜索
30. 全排列

### 工具

- **力扣 APP**：通勤时刷
- **NeetCode 150 题单**：按模式分类，不用自己挑
- **不要看题解视频**（太慢），看文字题解 + 自己重写

---

## 三、监控项目 + Ansible（每天 2-3 小时，核心重点）

> **定位调整**：部署部分你已经熟，快速过；重点放在 **Exporter 开发（练 Python）** 和 **Ansible 批量运维** 上。

### 里程碑（调整后）

| 阶段 | 天数 | 目标 |
|---|---|---|
| **Phase 1** | Day 1-2 | Prometheus + node_exporter + Grafana 跑起来（你熟，快速过）|
| **Phase 2** | Day 3-4 | Alertmanager + 飞书/钉钉告警 + Ansible 批量部署 Exporter |
| **Phase 3** | Day 5-9 | **自定义 Exporter 开发（重点！Python 实战）** |
| **Phase 4** | Day 10-12 | Blackbox Exporter + K8s 监控 + 写进简历 |
| **最后** | Day 21-30 | 复习 + 模拟面试 + 投递 |

### Phase 1：基础监控（Day 1-2，快速过）

```
目标：半天搞定部署，打开 Grafana 能看到 10 台机器的指标
```

- [ ] VM1 用 Docker Compose 一键拉起 Prometheus + Grafana
- [ ] 所有 VM 安装 node_exporter（**用 Ansible 批量装，不要手动**）
- [ ] Prometheus 配置 scrape_configs，抓取所有节点
- [ ] Grafana 导入 Dashboard（ID 1860）
- [ ] 练习 PromQL：avg(cpu)、sum by(instance)(rate(...))

**关键：部署不是重点，快速过。遇到的问题记下来就行。**

### Phase 2：告警 + Ansible（Day 3-4）

```
目标：告警能推到飞书；Ansible 能批量管理 10 台 VM
```

- [ ] Alertmanager 部署 + 和 Prometheus 对接
- [ ] 配置告警规则：CPU / 内存 / 磁盘 / 进程挂了
- [ ] 飞书 webhook 通知
- [ ] 配置 inhibit_rules 抑制规则

**Ansible 部分（重要！）：**

- [ ] 写 Ansible Inventory（10 台 VM 分组：prometheus / exporters / business）
- [ ] Playbook 1：批量安装 node_exporter + 注册 systemd 服务
- [ ] Playbook 2：批量推送 Prometheus scrape_configs + 热加载
- [ ] Playbook 3：批量部署 Blackbox Exporter
- [ ] Playbook 4：一键清理环境（uninstall 所有监控组件）

**Ansible 面试可以讲：**
- "10 台机器的 Exporter 部署用 Ansible 批量完成，不用手动 SSH"
- "配置变更用 Ansible 推送 + Prometheus reload API 热加载，不重启"

### Phase 3：自定义 Exporter 开发（Day 5-9，核心重点）⭐

```
目标：用 Python 写 2-3 个自定义 Exporter，练 Python + 能写进简历
```

这是整个监控项目的**核心价值** — 不是搭 Prometheus（谁都会），而是你能写 Exporter。

#### Exporter 1：HTTP 接口监控 Exporter（Day 5-6）

```python
# 功能：监控一组 HTTP 接口的响应时间和状态码
# 练习点：requests 库、dict 操作、异常处理、多线程

import time
import requests
from prometheus_client import start_http_server, Gauge, Counter

# 指标定义
REQUEST_DURATION = Gauge('http_request_duration_seconds', 'Request duration', ['host', 'endpoint', 'status'])
REQUEST_TOTAL = Counter('http_requests_total', 'Total requests', ['host', 'endpoint', 'status'])

def collect_metrics(targets):
    """遍历目标列表，请求每个接口，记录指标"""
    for target in targets:
        try:
            start = time.time()
            resp = requests.get(target['url'], timeout=5)
            duration = time.time() - start
            REQUEST_DURATION.labels(
                host=target['host'], endpoint=target['endpoint'],
                status=resp.status_code
            ).set(duration)
            REQUEST_TOTAL.labels(
                host=target['host'], endpoint=target['endpoint'],
                status=resp.status_code
            ).inc()
        except requests.RequestException as e:
            # 异常处理：超时、连接拒绝等
            REQUEST_TOTAL.labels(
                host=target['host'], endpoint=target['endpoint'],
                status='error'
            ).inc()

if __name__ == '__main__':
    start_http_server(9200)  # 暴露 /metrics 端口
    targets = [
        {'host': 'web1', 'endpoint': '/api/health', 'url': 'http://10.0.0.1:8080/api/health'},
        # ...
    ]
    while True:
        collect_metrics(targets)
        time.sleep(15)
```

**练习到的 Python 知识点：**
- `requests` 库发 HTTP 请求
- `dict` 带 label 操作（`labels(host=...).set()`）
- `try/except` 异常处理
- `time` 模块计时
- 函数定义 + 参数传递

#### Exporter 2：进程/端口存活监控 Exporter（Day 7）

```python
# 功能：监控指定进程是否存活、端口是否通
# 练习点：subprocess、socket、文件读写（读配置）

import socket
import subprocess
from prometheus_client import start_http_server, Gauge

PROCESS_UP = Gauge('process_up', 'Whether process is running', ['process_name'])
PORT_ALIVE = Gauge('port_alive', 'Whether port is reachable', ['host', 'port'])

def check_process(name):
    """检查进程是否存活（用 ps + grep 模拟）"""
    try:
        result = subprocess.run(
            ['pgrep', '-f', name],
            capture_output=True, text=True, timeout=5
        )
        return 1 if result.returncode == 0 else 0
    except Exception:
        return 0

def check_port(host, port):
    """检查端口是否可达"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        result = sock.connect_ex((host, port))
        sock.close()
        return 1 if result == 0 else 0
    except Exception:
        return 0

if __name__ == '__main__':
    start_http_server(9201)
    # 从配置文件读取要监控的进程和端口
    processes = ['prometheus', 'grafana-server', 'alertmanager']
    ports = [('10.0.0.1', 9090), ('10.0.0.1', 3000)]

    while True:
        for p in processes:
            PROCESS_UP.labels(process_name=p).set(check_process(p))
        for host, port in ports:
            PORT_ALIVE.labels(host=host, port=port).set(check_port(host, port))
        time.sleep(10)
```

**练习到的 Python 知识点：**
- `subprocess.run()` 调用系统命令
- `socket` 网络连接
- 文件读写（读配置文件）
- 函数封装

#### Exporter 3（可选）：MySQL 慢查询监控 Exporter（Day 8-9）

```python
# 功能：连接 MySQL，查询慢查询数量
# 练习点：pymysql/mysql-connector、SQL 查询、数据解析
# 如果你 MySQL 基础 OK，可以加上这个
```

**如果 MySQL 不熟，跳过这个，把前两个做扎实就行。**

### Phase 4：收尾 + 写简历（Day 10-12）

- [ ] Blackbox Exporter 部署，探测 HTTP/TCP 站点
- [ ] 整理所有 Exporter 代码，写 README
- [ ] 用 Ansible Playbook 管理所有 Exporter 的部署/更新
- [ ] 整理成简历描述 + 准备面试问题

### 简历描述（更新版）

**云原生监控告警体系** — 个人实践 | 2026.06

- 基于 Prometheus + Grafana + Alertmanager 搭建监控告警体系，覆盖 10+ 节点，使用 Ansible 批量管理 Exporter 部署
- 使用 Python（prometheus_client）开发自定义 Exporter：HTTP 接口响应监控、进程/端口存活探测，暴露指标供 Prometheus 采集
- Alertmanager 按严重等级路由告警，飞书 webhook 通知，配置 inhibit_rules 避免告警风暴
- Grafana 分层面板（基础设施/中间件/应用），结合业务指标实现端到端可观测

### 面试官会问的问题（准备 5 个）

1. **Exporter 的 pull 模型是什么？和 push 模型有什么区别？**
   → Prometheus 主动拉 /metrics 端点；Pushgateway 用于短生命周期任务

2. **你的自定义 Exporter 怎么暴露指标？prometheus_client 的 Gauge 和 Counter 区别？**
   → Gauge 可增可减（温度、响应时间）；Counter 只增不减（请求总数）

3. **PromQL 的 rate() 和 irate() 区别？**
   → rate 看时间段内平均增长率；irate 看最后两个数据点的瞬时增长率

4. **Alertmanager 的 group_wait / group_interval / repeat_interval 分别是什么？**
   → 分组等待 / 分组间隔 / 重复通知间隔

5. **Ansible 怎么实现批量部署？Playbook 和 ad-hoc 命令的区别？**
   → Playbook 是声明式 YAML，可重复执行；ad-hoc 是一次性命令

---

## 四、Ansible 速成（穿插在监控项目中）

> 不用专门花时间学，在做监控项目的过程中顺便练。

### 你需要掌握的

| 概念 | 用途 | 练习场景 |
|---|---|---|
| Inventory | 主机分组 | 10 台 VM 分成 prometheus / exporters / business 组 |
| Playbook | 声明式自动化任务 | 批量安装 node_exporter |
| Handler | 触发式操作 | 配置变更后 reload Prometheus |
| Template (Jinja2) | 动态配置文件 | 每台机器不同的 scrape_configs |
| Roles | 复用 Playbook | 把 Exporter 部署封装成 role |
| Ad-hoc 命令 | 一次性批量操作 | `ansible all -m ping`、`ansible all -m shell -a "uptime"` |

### 学习路径

1. **Day 3**：先写一个 Inventory + 一个最简单的 Playbook（批量 ping）
2. **Day 4**：写批量安装 node_exporter 的 Playbook
3. **Day 10**：把后续所有部署操作都用 Ansible 做
4. 遇到问题直接问 AI 或查文档，不要看教程视频

### 推荐资源

- [Ansible 官方入门](https://docs.ansible.com/ansible/latest/getting_started/)
- 遇到问题直接 `ansible-doc <module_name>` 查模块文档

---

## 四、每日时间分配（建议）

假设能投入 5-6 小时/天：

| 时段 | 时长 | 内容 |
|---|---|---|
| 上午（精力好）| 2-3 小时 | **监控项目**（动手搭建）|
| 下午 | 1-1.5 小时 | **算法题**（刷 2-3 题）|
| 晚上 | 1-1.5 小时 | **Python 速成**（写小脚本 + 复习白天项目）|
| 通勤/碎片 | 30 分钟 | 八股文（Linux / K8s / Redis）|

周末可以多花时间在监控项目上，算法题可以暂停。

---

## 五、检查点

### Day 10 应该能做到：

- 不看笔记写出：dict 常用操作、文件读写、try/except、装饰器、生成器
- LeetCode 前 15 题做完
- Prometheus + Grafana 跑起来，能看到主机指标

**做不到 → 调整节奏**：
- 算法题做不出 → 只看答案 + 重写，别死磕
- Python 记不住 → 把 auto_packing 的代码抄一遍，比看书有效

### Day 20 应该能做到：

- 监控项目完整跑通，能讲 5 个面试问题
- LeetCode 30+ 题完成
- 简历新版本已写好
- 开始投递

---

## 六、关键提醒

1. **Python 速成的最好方法是重写 auto_packing**
   - 一边写一边查文档，比看教程有效 10 倍
   - 顺便复习项目，面试时能讲出代码细节

2. **算法题不要追求"真正理解"**
   - 目标是面试够用，不是当竞赛选手
   - 看懂思路 → 自己重写 → 下一题

3. **监控项目一定要记笔记**
   - 每个报错、每个解决过程都记下来
   - 面试时 "我当时遇到了 XX 问题，通过 YY 解决" 是最有说服力的回答

4. **第 15 天开始投递**
   - 不要等"完全准备好"，边面试边学
   - 前 3 个面试当模拟考

---

## 七、推荐资源汇总

### Python

- [Python 官方教程](https://docs.python.org/zh-cn/3/tutorial/) — 4-9 章快速过
- [菜鸟教程 Python3](https://www.runoob.com/python3/) — 当手册查

### 算法

- [力扣](https://leetcode.cn/) — 按上面的 30 题清单刷
- [NeetCode 150](https://neetcode.io/) — 按模式分类的题单

### 监控

- [Prometheus 官方文档](https://prometheus.io/docs/) — 跟着 Getting Started 走
- [Grafana 官方文档](https://grafana.com/docs/) — 直接看 Dashboard 导入
- [Awesome Prometheus Alerts](https://samber.github.io/awesome-prometheus-alerts/) — 告警规则模板集合

### 面试八股

- [小林 coding](https://xiaolincoding.com/) — Linux / 网络 / MySQL / Redis 图解
- [JavaGuide](https://javaguide.cn/) — 虽然是 Java 向，但 Redis / Linux 部分通用
