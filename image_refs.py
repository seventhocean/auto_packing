"""Image reference parsing shared by the API and the export script.

Only tagged references are accepted. Digest references and implicit ``latest``
are intentionally outside the patch package format.
"""
import argparse
import hashlib
import re
from pathlib import Path


DEFAULT_REPOSITORY = "hub.deepflow.yunshan.net/dev/"
_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_PATH_RE = re.compile(rf"^{_COMPONENT}(?:/{_COMPONENT})*$")
_HOST_RE = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


def normalize_image_ref(value, legacy=False):
    """Return a fully qualified tagged reference, or raise ValueError.

    ``legacy`` accepts the generated ``name_tag: tag`` list format. Custom API
    values use literal names, so an image actually named ``foo_tag`` is retained.
    """
    if not isinstance(value, str):
        raise ValueError("镜像引用必须是字符串")
    value = value.strip()
    if legacy:
        value = re.sub(r"_tag:[ \t]+", ":", value)
        value = re.sub(r":\s+", ":", value)
    if not value or re.search(r"\s", value) or "@" in value:
        raise ValueError(f"非法镜像引用：{value!r}（需要 name:tag）")
    name, separator, tag = value.rpartition(":")
    if not separator or not _TAG_RE.fullmatch(tag):
        raise ValueError(f"非法镜像引用：{value!r}（需要有效 tag）")
    first, slash, remaining = name.partition("/")
    is_registry = bool(slash) and ("." in first or ":" in first or first == "localhost")
    if is_registry:
        host, port_separator, port = first.partition(":")
        if not _HOST_RE.fullmatch(host) or (port_separator and (
                not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535)):
            raise ValueError(f"非法镜像仓库地址：{first!r}")
        path = remaining
    else:
        path = name
    if not _PATH_RE.fullmatch(path) or len(name) > 255:
        raise ValueError(f"非法镜像名称：{name!r}")
    full_name = name if is_registry else DEFAULT_REPOSITORY + name
    if len(full_name) > 255:
        raise ValueError("镜像名称过长")
    return f"{full_name}:{tag}"


def validate_image_ref(value):
    try:
        normalize_image_ref(value)
        return True
    except ValueError:
        return False


def normalize_images(images):
    """Validate the complete list before returning unique normalized refs."""
    if not isinstance(images, list) or not images:
        raise ValueError("镜像列表不能为空")
    return list(dict.fromkeys(normalize_image_ref(value) for value in images))


def image_tar_name(full_ref):
    name, _, tag = full_ref.rpartition(":")
    basename = name.rsplit("/", 1)[-1]
    ordinary = name.startswith(DEFAULT_REPOSITORY) and "/" not in name[len(DEFAULT_REPOSITORY):]
    stem = f"{basename}_{tag}"
    # Retain established filenames for the ordinary DeepFlow component images.
    # Other registries/namespaces must not alias those names or each other.
    if not ordinary or len(stem) > 230:
        digest = hashlib.sha256(full_ref.encode("utf-8")).hexdigest()[:16]
        stem = f"{basename[:100]}_{tag[:80]}_{digest}"
    return stem + ".tar"


def main():
    parser = argparse.ArgumentParser(description="校验镜像清单并输出导出计划")
    parser.add_argument("--file", type=Path)
    parser.add_argument("images", nargs="*")
    args = parser.parse_args()
    try:
        if args.file:
            raw = args.file.read_text(encoding="utf-8").splitlines()
            refs = []
            for number, line in enumerate(raw, 1):
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                try:
                    refs.append(normalize_image_ref(line, legacy=True))
                except ValueError as exc:
                    raise ValueError(f"镜像列表第 {number} 行：{exc}") from exc
            images = normalize_images(refs)
        else:
            images = normalize_images(args.images)
        filenames = set()
        for ref in images:
            filename = image_tar_name(ref)
            if filename in filenames:
                raise ValueError("镜像导出文件名冲突")
            filenames.add(filename)
            print(f"{ref}\t{filename}")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"镜像列表校验失败：{exc}\n")


if __name__ == "__main__":
    main()
