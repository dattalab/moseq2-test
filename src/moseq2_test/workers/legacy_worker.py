#!/usr/bin/env python3
"""Python-3.7-compatible target worker.

This file intentionally imports no ``moseq2_test`` module. The controller
copies or invokes it as a standalone script and communicates only through JSON.
"""

import argparse
import importlib
import json
import os
import platform
import site
import subprocess
import sys
import traceback
from collections import Counter
from pathlib import Path

PROTOCOL_VERSION = 1


def jsonable(value):
    try:
        import numpy as np
    except ImportError:
        np = None
    if isinstance(value, bytes):
        try:
            return {"bytes_utf8": value.decode("utf-8")}
        except UnicodeDecodeError:
            return {"bytes_hex": value.hex()}
    if np is not None and isinstance(value, np.generic):
        return jsonable(value.item())
    if np is not None and isinstance(value, np.ndarray):
        return [jsonable(item) for item in value.tolist()]
    if isinstance(value, dict):
        return {str(key): jsonable(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return {"python_type": type(value).__module__ + "." + type(value).__name__}


def array_summary(array):
    import hashlib

    import numpy as np

    array = np.asarray(array)
    digest = hashlib.sha256()
    digest.update(str(array.dtype).encode("utf-8"))
    digest.update(json.dumps(list(array.shape)).encode("utf-8"))
    digest.update(np.ascontiguousarray(array).tobytes(order="C"))
    return {
        "shape": list(array.shape),
        "dtype": str(array.dtype),
        "semantic_sha256": digest.hexdigest(),
    }


def summarize_pickle_value(value, depth=0):
    import numpy as np

    if isinstance(value, np.ndarray):
        result = {"kind": "ndarray"}
        result.update(array_summary(value))
        return result
    if isinstance(value, dict):
        if depth >= 3:
            return {"kind": "dict", "length": len(value), "keys": sorted(map(str, value))}
        return {
            "kind": "dict",
            "length": len(value),
            "values": {
                str(key): summarize_pickle_value(value[key], depth + 1)
                for key in sorted(value, key=str)
            },
        }
    if isinstance(value, (list, tuple)):
        if depth >= 3:
            return {"kind": type(value).__name__, "length": len(value)}
        return {
            "kind": type(value).__name__,
            "length": len(value),
            "items": [summarize_pickle_value(item, depth + 1) for item in value],
        }
    if isinstance(value, (str, int, float, bool, bytes)) or value is None:
        return jsonable(value)
    record = {
        "kind": "python_object",
        "type": type(value).__module__ + "." + type(value).__name__,
    }
    states = getattr(value, "states_list", None)
    if states is not None:
        record["states_list_length"] = len(states)
        record["state_sequences"] = [
            array_summary(state.stateseq)
            for state in states
            if getattr(state, "stateseq", None) is not None
        ]
    return record


def distribution_record(name):
    try:
        try:
            from importlib import metadata
        except ImportError:
            import importlib_metadata as metadata
        distribution = metadata.distribution(name)
    except Exception as error:
        return {"name": name, "installed": False, "error": str(error)}
    metadata_path = Path(distribution._path)
    direct_url_path = metadata_path / "direct_url.json"
    direct_url = None
    if direct_url_path.is_file():
        direct_url = json.loads(direct_url_path.read_text())
    return {
        "name": name,
        "installed": True,
        "canonical_name": distribution.metadata.get("Name", name),
        "version": distribution.version,
        "metadata_path": str(metadata_path.resolve()),
        "root": str(Path(distribution.locate_file("")).resolve()),
        "direct_url": direct_url,
    }


def import_record(name):
    try:
        module = importlib.import_module(name)
        return {
            "module": name,
            "imported": True,
            "file": str(Path(module.__file__).resolve()) if module.__file__ else None,
            "version": getattr(module, "__version__", None),
        }
    except Exception as error:
        return {
            "module": name,
            "imported": False,
            "error_type": type(error).__name__,
            "error": str(error),
        }


def operation_probe(parameters):
    del parameters
    return {
        "python": {"executable": sys.executable, "version": sys.version, "prefix": sys.prefix},
        "platform": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "libc": platform.libc_ver(),
        },
    }


def operation_run_command(parameters):
    command = parameters["command"]
    environment = dict(os.environ)
    environment.update(parameters.get("environment", {}))
    for name in parameters.get("unset_environment", []):
        environment.pop(name, None)
    timeout = int(parameters.get("timeout", 300))
    completed = subprocess.run(
        command,
        cwd=parameters.get("cwd"),
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def operation_seeded_cli(parameters):
    import numpy as np

    np.random.seed(int(parameters.get("seed", 0)))
    if parameters.get("cwd") is not None:
        os.chdir(parameters["cwd"])
    module = importlib.import_module(parameters["module"])
    arguments = list(parameters.get("arguments", []))
    sys.argv = [parameters["module"], *arguments]
    try:
        value = module.cli()
        return {"returncode": int(value or 0), "module": parameters["module"]}
    except SystemExit as error:
        value = int(error.code or 0)
        return {
            "returncode": value,
            "module": parameters["module"],
            "exception": "SystemExit" if value else None,
            "message": str(error),
            "traceback": traceback.format_exc() if value else None,
        }
    except BaseException as error:
        return {
            "returncode": 1,
            "module": parameters["module"],
            "exception": type(error).__name__,
            "message": str(error),
            "traceback": traceback.format_exc(),
        }


def operation_inspect_installation(parameters):
    imports = [import_record(name) for name in parameters.get("imports", [])]
    distributions = [distribution_record(name) for name in parameters.get("distributions", [])]
    extensions = []
    for name in parameters.get("extensions", []):
        record = import_record(name)
        record["compiled"] = bool(
            record.get("file") and Path(record["file"]).suffix in (".so", ".pyd")
        )
        extensions.append(record)
    site_packages = [str(Path(path).resolve()) for path in site.getsitepackages()]
    editable_links = []
    pth_entries = []
    for raw_directory in site_packages:
        directory = Path(raw_directory)
        editable_links.extend(str(path.resolve()) for path in directory.glob("*.egg-link"))
        for pth_file in directory.glob("*.pth"):
            for raw_line in pth_file.read_text(errors="replace").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or line.startswith("import "):
                    continue
                pth_entries.append({"file": str(pth_file.resolve()), "entry": line})
    console_scripts = []
    for name in parameters.get("console_scripts", []):
        path = Path(sys.prefix) / "bin" / name
        console_scripts.append(
            {"name": name, "path": str(path.resolve()), "exists": path.is_file()}
        )
    return {
        "imports": imports,
        "distributions": distributions,
        "extensions": extensions,
        "console_scripts": console_scripts,
        "editable_links": editable_links,
        "pth_entries": pth_entries,
        "site_packages": site_packages,
        "sys_path": [str(Path(path).resolve()) if path else str(Path.cwd()) for path in sys.path],
        "python_executable": str(Path(sys.executable).resolve()),
        "python_version": sys.version,
        "python_version_info": list(sys.version_info[:3]),
        "python_prefix": sys.prefix,
    }


def operation_pickle_manifest(parameters):
    if parameters.get("trusted") is not True:
        raise ValueError("pickle-manifest requires trusted=true")
    import joblib

    value = joblib.load(parameters["path"])
    return {"kind": "pickle", "structure": summarize_pickle_value(value)}


def operation_model_analysis(parameters):
    if parameters.get("trusted") is not True:
        raise ValueError("model-analysis requires trusted=true")
    import h5py
    import joblib
    import numpy as np

    model = joblib.load(parameters["path"])
    labels = [np.asarray(value) for value in model.get("labels", [])]
    keys = [str(value) for value in model.get("keys", [])]
    usage = Counter()
    for label in labels:
        usage.update(int(value) for value in label.tolist() if int(value) >= 0)
    specific_syllable = max(usage.items(), key=lambda item: item[1])[0] if usage else None
    result = {
        "summary": {"kind": "pickle", "structure": summarize_pickle_value(model)},
        "specific_syllable": specific_syllable,
    }
    score_path = parameters.get("score_path")
    if score_path is not None:
        with h5py.File(score_path, "r") as scores:
            expected_lengths = {
                str(key): int(dataset.shape[0]) for key, dataset in scores["scores"].items()
            }
        # ``strict=`` is unavailable in the worker's required Python 3.7 runtime.
        lengths = {key: len(label) for key, label in zip(keys, labels)}  # noqa: B905
        checks = {
            "four_sessions": len(keys) == 4 and len(labels) == 4,
            "all_score_uuids_present": set(keys) == set(expected_lengths),
            "label_lengths_match_scores": lengths == expected_lengths,
            "labels_are_finite_integers": all(
                np.issubdtype(label.dtype, np.integer) and np.isfinite(label).all()
                for label in labels
            ),
            "model_object_saved": model.get("model") is not None,
            "whitening_parameters_saved": bool(model.get("whitening_parameters")),
            "loglikes_recorded": (
                np.size(model.get("loglikes", [])) >= 1 and len(model.get("train_ll", [])) >= 2
            ),
        }
        result["invariants"] = {
            "passed": all(checks.values()),
            "checks": checks,
            "keys": keys,
            "label_lengths": lengths,
            "expected_lengths": expected_lengths,
            "label_minimum": min(int(label.min()) for label in labels) if labels else None,
            "label_maximum": max(int(label.max()) for label in labels) if labels else None,
        }
    return result


def operation_compiled_smoke(parameters):
    del parameters
    checks = []

    def capture(check_id, function):
        try:
            result = function()
            result["id"] = check_id
            checks.append(result)
        except BaseException as error:
            checks.append(
                {
                    "id": check_id,
                    "passed": False,
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "traceback": traceback.format_exc(),
                }
            )

    def pybasicbayes_check():
        import numpy as np
        from pybasicbayes.util.cstats import sample_markov

        states = sample_markov(
            5,
            np.array([[1.0, 0.0], [0.0, 1.0]], order="C"),
            np.array([1.0, 0.0]),
        )
        return {
            "passed": bool(np.array_equal(states, np.zeros(5, dtype=np.int32))),
            "states": states.tolist(),
        }

    def pyhsmm_cstats_check():
        import numpy as np
        from pyhsmm.util.cstats import count_transitions

        counts = count_transitions(np.array([0, 1, 1, 0], dtype=np.int32), 2)
        expected = np.array([[0, 1], [1, 1]], dtype=np.int32)
        return {
            "passed": bool(np.array_equal(counts, expected)),
            "counts": counts.tolist(),
            "expected": expected.tolist(),
        }

    def pyhsmm_hmm_check():
        import numpy as np
        from pyhsmm.internals.hmm_messages_interface import viterbi

        transition = np.log(np.array([[0.9, 0.1], [0.2, 0.8]], order="C"))
        likelihood = np.log(np.array([[0.8, 0.2], [0.7, 0.3], [0.1, 0.9]], order="C"))
        initial = np.log(np.array([0.6, 0.4]))
        states = viterbi(transition, likelihood, initial, np.empty(3, dtype=np.int32))
        expected = np.array([0, 0, 0], dtype=np.int32)
        return {
            "passed": bool(np.array_equal(states, expected)),
            "states": states.tolist(),
            "expected": expected.tolist(),
        }

    def autoregressive_check():
        import numpy as np
        from autoregressive.distributions import AutoRegression
        from autoregressive.models import FastARHMM

        np.random.seed(0)
        observations = [
            AutoRegression(
                nu_0=3,
                S_0=np.eye(1),
                M_0=np.eye(1),
                K_0=np.eye(1),
                affine=False,
            )
            for _ in range(2)
        ]
        model = FastARHMM(alpha=4.0, init_state_distn="uniform", obs_distns=observations)
        model.add_data(np.linspace(-1.0, 1.0, 30).reshape(-1, 1).astype("float32"))
        model.resample_states()
        states = model.states_list[0].stateseq
        return {
            "passed": bool(
                states.shape == (29,)
                and states.dtype == np.dtype("int32")
                and set(np.unique(states)).issubset({0, 1})
            ),
            "shape": list(states.shape),
            "dtype": str(states.dtype),
            "unique_states": np.unique(states).tolist(),
        }

    capture("compiled-operation-pybasicbayes", pybasicbayes_check)
    capture("compiled-operation-pyhsmm-cstats", pyhsmm_cstats_check)
    capture("compiled-operation-pyhsmm-hmm", pyhsmm_hmm_check)
    capture("compiled-operation-autoregressive", autoregressive_check)
    return {"checks": checks, "passed": all(check["passed"] for check in checks)}


def operation_app_smoke(parameters):
    from unittest.mock import patch

    from moseq2_app.scalars.controller import InteractiveScalarViewer
    from moseq2_app.util import index_to_dataframe, merge_labels_with_scalars
    from moseq2_app.viz.controller import _initialize_syll_info_dict
    from moseq2_viz.util import parse_index

    _, index_dataframe = index_to_dataframe(parameters["index"])
    _, sorted_index = parse_index(parameters["index"])
    statistics, scalar_dataframe = merge_labels_with_scalars(sorted_index, parameters["model"])
    with patch("plotly.basedatatypes.BaseFigure.show") as show:
        viewer = InteractiveScalarViewer(parameters["index"])
        figure = viewer.make_graphs()
        display_calls = show.call_count
    syllable_info = _initialize_syll_info_dict(3)
    checks = {
        "index_has_four_sessions": len(index_dataframe) == 4,
        "scalar_dataframe_nonempty": len(scalar_dataframe) > 0,
        "behavioral_statistics_nonempty": len(statistics) > 0,
        "scalar_controller_has_two_default_columns": len(viewer.checked_list.value) == 2,
        "scalar_controller_figure_has_traces": len(figure.data) > 0,
        "scalar_controller_display_was_safely_intercepted": display_calls > 0,
        "syllable_info_has_three_entries": len(syllable_info) == 3,
    }
    return {
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "index_rows": len(index_dataframe),
        "scalar_rows": len(scalar_dataframe),
        "statistics_rows": len(statistics),
        "figure_traces": len(figure.data),
        "intercepted_display_calls": display_calls,
        "scalar_columns": list(scalar_dataframe.columns),
        "statistics_columns": list(statistics.columns),
    }


OPERATIONS = {
    "probe": operation_probe,
    "run-command": operation_run_command,
    "seeded-cli": operation_seeded_cli,
    "inspect-installation": operation_inspect_installation,
    "pickle-manifest": operation_pickle_manifest,
    "model-analysis": operation_model_analysis,
    "compiled-smoke": operation_compiled_smoke,
    "app-smoke": operation_app_smoke,
}


def execute(request):
    if request.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("unsupported worker protocol version")
    request_id = request.get("request_id")
    operation = request.get("operation")
    parameters = request.get("parameters")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("request_id must be a nonempty string")
    if operation not in OPERATIONS:
        raise ValueError(f"unknown worker operation: {operation}")
    if not isinstance(parameters, dict):
        raise ValueError("parameters must be an object")
    return OPERATIONS[operation](parameters)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    arguments = parser.parse_args()
    request = {}
    response = None
    exit_code = 0
    try:
        request = json.loads(arguments.request.read_text())
        result = execute(request)
        response = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request["request_id"],
            "status": "ok",
            "result": jsonable(result),
            "error": None,
        }
    except BaseException as error:
        exit_code = 1
        response = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request.get("request_id", "<invalid>"),
            "status": "error",
            "result": None,
            "error": {
                "type": type(error).__name__,
                "message": str(error),
                "traceback": traceback.format_exc(),
            },
        }
    arguments.response.parent.mkdir(parents=True, exist_ok=True)
    arguments.response.write_text(json.dumps(response, indent=2, sort_keys=True) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
