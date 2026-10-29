#!/usr/bin/env python3
"""Keep the checked-in deployment derived from config.yaml, without reformatting it."""
import argparse
import os
import re
import tempfile
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
DEPLOYMENTS = ("maintenance-app", "maintenance-worker")


def _replace_config(document, source):
    lines = document.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if re.fullmatch(r"  config\.yaml: \|[+-]?\s*\n?", line):
            end = index + 1
            while end < len(lines) and (lines[end].startswith("    ") or not lines[end].strip()):
                end += 1
            payload = "".join(("    " + item.rstrip() if item.strip() else "") + "\n"
                              for item in source.splitlines())
            return "".join(lines[:index + 1]) + payload + "".join(lines[end:])
    raise ValueError("ConfigMap 缺少可同步的 config.yaml 块")


def _replace_image(document, container_name, image):
    lines = document.splitlines(keepends=True)
    active_container = None
    count = 0
    for index, line in enumerate(lines):
        match = re.fullmatch(r"      - name: ([^\s#]+)\s*\n?", line)
        if match:
            active_container = match.group(1)
        image_line = re.fullmatch(r"(        image:)\s+[^\s#]+(?:\s+#.*)?\n?", line)
        if image_line and active_container == container_name:
            lines[index] = f"{image_line.group(1)} {image}\n"
            count += 1
    if count != 1:
        raise ValueError(f"期望替换一个 {container_name} 镜像，实际 {count} 个")
    return "".join(lines)


def render_manifest(manifest, config, image=None):
    source = config.read_text(encoding="utf-8").rstrip() + "\n"
    loaded_config = yaml.safe_load(source)
    if not isinstance(loaded_config, dict):
        raise ValueError("config.yaml 必须包含配置对象")
    # A worker with private scratch space cannot deliver local-only packages.
    if (loaded_config.get("package_storage") or {}).get("backend") != "oss":
        raise ValueError("当前多节点部署使用独立临时目录，package_storage.backend 必须为 oss")
    if image and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", image):
        raise ValueError("非法发布镜像引用")

    original = manifest.read_text(encoding="utf-8")
    parts = re.split(r"(?m)(^---[ \t]*\n)", original)
    found_config = 0
    found_deployments = set()
    for index in range(0, len(parts), 2):
        document = parts[index]
        data = yaml.safe_load(document)
        if not isinstance(data, dict):
            continue
        name = (data.get("metadata") or {}).get("name")
        if data.get("kind") == "ConfigMap" and name == "maintenance-app-config":
            parts[index] = _replace_config(document, source)
            found_config += 1
        elif data.get("kind") == "Deployment" and name in DEPLOYMENTS:
            found_deployments.add(name)
            if image:
                parts[index] = _replace_image(document, name, image)
    if found_config != 1 or found_deployments != set(DEPLOYMENTS):
        raise ValueError("清单必须包含一个应用 ConfigMap 和 app/worker Deployment")
    return "".join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--manifest", type=Path, default=ROOT / "apply/maintenance-app-deployment.yaml")
    parser.add_argument("--image")
    parser.add_argument("--check", action="store_true", help="只检查配置是否同步，不写文件")
    args = parser.parse_args()
    try:
        rendered = render_manifest(args.manifest, args.config, args.image)
        current = args.manifest.read_text(encoding="utf-8")
        if rendered == current:
            return
        if args.check:
            parser.exit(1, "部署清单与配置不一致，请运行 python bin/sync_manifest.py\n")
        descriptor, temporary = tempfile.mkstemp(prefix=args.manifest.name + ".", dir=args.manifest.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(rendered)
            os.chmod(temporary, args.manifest.stat().st_mode)
            os.replace(temporary, args.manifest)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        parser.exit(1, f"部署清单同步失败：{exc}\n")


if __name__ == "__main__":
    main()
