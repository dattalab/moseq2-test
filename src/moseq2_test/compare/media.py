"""Image/video structural comparison."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

from moseq2_test.compare.base import ComparatorPolicy, arrays_differences
from moseq2_test.errors import MissingInput


def image_manifest(path: Path, policy: ComparatorPolicy) -> dict[str, Any]:
    del policy
    try:
        from PIL import Image
    except ImportError as error:  # pragma: no cover - optional installation
        raise MissingInput("install moseq2-test[media] for image comparison") from error
    with Image.open(path) as image:
        image.load()
        return {
            "kind": "image",
            "format": image.format,
            "mode": image.mode,
            "size": list(image.size),
            "frames": getattr(image, "n_frames", 1),
        }


def compare_image(expected: Path, actual: Path, policy: ComparatorPolicy) -> list[dict[str, Any]]:
    from PIL import Image

    left_manifest = image_manifest(expected, policy)
    right_manifest = image_manifest(actual, policy)
    structural_keys = ("mode", "size", "frames")
    differences = [
        {
            "kind": "image-property",
            "property": key,
            "expected": left_manifest[key],
            "actual": right_manifest[key],
        }
        for key in structural_keys
        if left_manifest[key] != right_manifest[key]
    ]
    if not differences and bool((policy.model_extra or {}).get("compare_pixels", False)):
        with Image.open(expected) as left, Image.open(actual) as right:
            differences.extend(
                arrays_differences(
                    np.asarray(left), np.asarray(right), path="/pixels", policy=policy
                )
            )
    return differences


def video_manifest(path: Path, policy: ComparatorPolicy) -> dict[str, Any]:
    del policy
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise MissingInput("ffprobe is required for video comparison")
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=format_name,duration,size:stream=index,codec_type,codec_name,width,height,"
            "pix_fmt,r_frame_rate,avg_frame_rate,nb_frames,duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return {"kind": "video", "properties": json.loads(completed.stdout)}


def compare_video(expected: Path, actual: Path, policy: ComparatorPolicy) -> list[dict[str, Any]]:
    left = video_manifest(expected, policy)["properties"]
    right = video_manifest(actual, policy)["properties"]
    ignored = set((policy.model_extra or {}).get("ignored_properties", ["size", "duration"]))
    for record in (left, right):
        for key in ignored:
            record.get("format", {}).pop(key, None)
        for stream in record.get("streams", []):
            for key in ignored:
                stream.pop(key, None)
    return (
        [] if left == right else [{"kind": "video-properties", "expected": left, "actual": right}]
    )
