# -*- coding: utf-8 -*-
"""P2 工程成熟度：并发压测脚本（进程内直测核心链路，不依赖后端已启动）。

两种模式：
- 默认（进程内）：ThreadPool 并发跑 LLMService（用编译命中的问题，避开 LLM 延迟
  主导），测核心链路吞吐 / P95 / 错误率——这是"代码链路"的真实并发表现；
- `--http http://localhost:8010`：打真实 HTTP 接口 /api/agent/ask，测全链路
  （含网络/框架/鉴权开销）。

用法：
    python scripts/load_test.py --concurrency 8 --total 40
    python scripts/load_test.py --http http://localhost:8010 --concurrency 4 --total 20
"""

import argparse
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, ".")

_QUERIES = [
    "各产线的产量是多少",
    "各产品的不良数量",
    "各工序的良率对比",
    "最近7天各产线的产量趋势",
]


def _run_inproc(q: str) -> tuple[float, bool]:
    from agent.llm_service import LLMService
    t0 = time.time()
    ok = False
    for ev in LLMService(q).run():
        if ev.get("type") == "done":
            ok = bool((ev.get("response") or {}).get("result"))
    return (time.time() - t0) * 1000, ok


def _run_http(base: str, q: str) -> tuple[float, bool]:
    import urllib.request
    body = ('{"query": "%s", "history": []}' % q).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + "/api/agent/ask", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            # 流式响应：读完即可（结果在 done 事件里，这里只统计请求是否完成）
            resp.read()
        ok = True
    except Exception:
        ok = False
    return (time.time() - t0) * 1000, ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=8, help="并发数")
    ap.add_argument("--total", type=int, default=40, help="总请求数")
    ap.add_argument("--http", default="", help="HTTP 模式：后端 base_url（如 http://localhost:8010）")
    args = ap.parse_args()

    concurrency = max(1, args.concurrency)
    total = max(1, args.total)
    if args.http:
        from functools import partial
        run = partial(_run_http, args.http)   # 绑定 base_url，统一 run(q) 签名
        mode = f"HTTP({args.http})"
    else:
        run = _run_inproc
        mode = "进程内直测"

    # 预热一轮（编译命中缓存 / 连接池）
    q0 = _QUERIES[0]
    try:
        run(q0)
    except Exception:
        pass

    lat: list[float] = []
    stats = {"ok": 0, "fail": 0}          # 用可变 dict 避免嵌套函数 += 的局部变量陷阱
    err_msgs: list[str] = []
    _lock = threading.Lock()

    def worker(i: int):
        q = _QUERIES[i % len(_QUERIES)]
        try:
            ms, ok = run(q)
        except Exception as e:
            with _lock:
                stats["fail"] += 1
                if len(err_msgs) < 5:
                    err_msgs.append(str(e)[:100])
            return
        with _lock:
            lat.append(ms)
            stats["ok" if ok else "fail"] += 1

    print(f"压测开始：{mode} | 并发 {concurrency} | 总请求 {total}")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(worker, i) for i in range(total)]
        for f in as_completed(futs):
            f.result()
    wall = time.time() - t0

    print("\n===== 压测报告 =====")
    print(f"墙钟耗时:      {wall:.1f}s")
    print(f"总请求:        {total}（成功 {stats['ok']} / 失败 {stats['fail']}）")
    print(f"吞吐:          {total / wall:.1f} req/s" if wall > 0 else "吞吐: n/a")
    if lat:
        lat.sort()
        n = len(lat)
        print(f"延迟 (ms):     均值 {statistics.mean(lat):.0f} | P50 {lat[n // 2]:.0f} | "
              f"P90 {lat[int(n * 0.9)]:.0f} | P95 {lat[min(int(n * 0.95), n - 1)]:.0f} | "
              f"P99 {lat[min(int(n * 0.99), n - 1)]:.0f} | 最大 {lat[-1]:.0f}")
    if err_msgs:
        print("失败示例:")
        for e in err_msgs:
            print(f"  - {e}")
    return 1 if stats["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
