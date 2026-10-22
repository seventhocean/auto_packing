#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读巡检 containerd 镜像/内容一致性。

用途：
  1. 检查 default namespace 是否还有镜像（历史遗留的另一个 namespace）；
  2. 检查 k8s.io 中每个镜像的 manifest/config/layer 是否都能在目标 namespace 读取到；
  3. 发现 "镜像记录在 k8s.io，但 layer content 在 default" 这类跨 namespace 问题。

本脚本只执行 ls / inspect 一类只读命令，不删除、不写入、不 GC。

示例：
  python3 bin/check_containerd.py
  python3 bin/check_containerd.py --namespace k8s.io --default-namespace default
  python3 bin/check_containerd.py --quick   # 只检查 default namespace

退出码：
  0 未发现问题
  1 发现问题（default 有镜像 / 存在缺失 layer / inspect 失败）
  2 无法执行检查（缺少 ctr/nerdctl 等）
"""
import argparse
import json
import os
import shutil
import subprocess
import sys


def run(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)


def list_refs(ctr, namespace):
    result = run([ctr, "-n", namespace, "images", "ls"])
    if result.returncode != 0:
        raise RuntimeError("执行 ctr images ls 失败：{}".format(result.stderr.strip()))
    refs = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("REF"):
            continue
        parts = line.split()
        if parts:
            refs.append(parts[0])
    return refs


def list_content_digests(ctr, namespace):
    result = run([ctr, "-n", namespace, "content", "ls"])
    if result.returncode != 0:
        raise RuntimeError("执行 ctr content ls 失败：{}".format(result.stderr.strip()))
    digests = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("DIGEST"):
            continue
        parts = line.split()
        if parts and parts[0].startswith("sha256:"):
            digests.add(parts[0])
    return digests


def inspect_digests(nerdctl, namespace, ref):
    result = run([nerdctl, "-n", namespace, "image", "inspect", "--mode=native", ref])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "nerdctl image inspect 失败")
    data = json.loads(result.stdout)
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        raise RuntimeError("无法解析 native inspect 输出")

    digests = set()
    for key in ("ManifestDesc", "ImageConfigDesc"):
        digest = (data.get(key) or {}).get("digest")
        if digest:
            digests.add(digest)
    manifest = data.get("Manifest") or {}
    for layer in manifest.get("layers") or []:
        digest = (layer or {}).get("digest")
        if digest:
            digests.add(digest)
    return digests


def main():
    parser = argparse.ArgumentParser(description="只读巡检 containerd 镜像/内容一致性")
    parser.add_argument("--namespace", default=os.environ.get("K8S_CONTAINERD_NAMESPACE", "k8s.io"),
                        help="Kubernetes 使用的 containerd namespace，默认 k8s.io")
    parser.add_argument("--default-namespace", default="default",
                        help="需要保持为空的另一 namespace，默认 default")
    parser.add_argument("--ctr", default=os.environ.get("CTR", "ctr"), help="ctr 可执行文件路径")
    parser.add_argument("--nerdctl", default=os.environ.get("NERDCTL", "nerdctl"),
                        help="nerdctl 可执行文件路径")
    parser.add_argument("--quick", action="store_true", help="只检查 default namespace，不逐镜像检查 layer")
    args = parser.parse_args()

    for binary in (args.ctr, args.nerdctl):
        if not shutil.which(binary) and not os.path.isfile(binary):
            print("[ERROR] 找不到可执行文件：{}".format(binary), file=sys.stderr)
            return 2

    issues = 0

    try:
        default_refs = list_refs(args.ctr, args.default_namespace)
        k8s_refs = list_refs(args.ctr, args.namespace)
        content_digests = list_content_digests(args.ctr, args.namespace)
    except RuntimeError as exc:
        print("[ERROR] {}".format(exc), file=sys.stderr)
        return 2

    print("namespace: {}  镜像: {}  content: {}".format(args.namespace, len(k8s_refs), len(content_digests)))
    print("namespace: {}  镜像: {}".format(args.default_namespace, len(default_refs)))

    if default_refs:
        issues += 1
        print("\n[WARN] {} namespace 仍有镜像（历史遗留会让 layer 被“扣”在另一个 namespace）：".format(args.default_namespace))
        for ref in default_refs:
            print("  - {}".format(ref))

    if args.quick:
        print("\n[INFO] --quick 模式，仅完成 namespace 检查")
        return 1 if issues else 0

    failed_inspect = []
    for ref in k8s_refs:
        try:
            digests = inspect_digests(args.nerdctl, args.namespace, ref)
        except Exception as exc:
            failed_inspect.append((ref, str(exc)))
            continue
        missing = sorted(digest for digest in digests if digest not in content_digests)
        if missing:
            issues += 1
            print("\n[WARN] 镜像 layer/manifest 缺失：{}".format(ref))
            for digest in missing:
                print("  - {}".format(digest))

    if failed_inspect:
        issues += 1
        print("\n[WARN] 以下镜像 inspect 失败，可能是 manifest 也缺失：")
        for ref, error in failed_inspect:
            print("  - {}：{}".format(ref, error))

    if issues:
        print("\n检查完成：发现 {} 类问题。本脚本只读，不会自动修复。".format(issues))
        print("处理建议见 docs/CONTAINERD_RUNBOOK.md")
        return 1

    print("\n检查完成：未发现问题")
    return 0


if __name__ == "__main__":
    sys.exit(main())
