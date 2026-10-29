"""One expendable reader process. Scientific imports never reach the HTTP loop."""
from __future__ import annotations

from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import sys

from .config import AnalysisSettings
from .contracts import AnalysisError, DistanceAnalysisRequest, TrajectoryAnalysisRequest
from .files import prepare_sources, strict_json

RESPONSE_LIMIT = 16 * 1024 ** 2


def execute(envelope):
    options = envelope["settings"]
    settings = AnalysisSettings(job_root=Path(options["job_root"]), spool_root=Path(options["spool_root"]),
        spool_budget_bytes=options["spool_budget_bytes"], min_free_bytes=options["min_free_bytes"], free_fraction=options["free_fraction"])
    operation = envelope["operation"]
    model = DistanceAnalysisRequest if operation == "distance" else TrajectoryAnalysisRequest
    source = model.model_validate(envelope["request"])
    private = Path(envelope["private"])
    result = prepare_sources(settings, source, private)
    import mdtraj
    with mdtraj.formats.XTCTrajectoryFile(str(private / "trajectory.xtc")) as trajectory:
        if len(trajectory) != result["results"]["trajectory"]["frame_count"]:
            raise AnalysisError("invalid_analysis_data", "真实轨迹帧数与阶段记录不匹配。", 422)
    from workers.polymer_md_worker.app.scientific_analysis import analyze_distance, read_trajectory
    if operation == "distance":
        data = analyze_distance(private, result, source.distance.model_dump())
        frame_count = result["results"]["trajectory"]["frame_count"]
        end = source.distance.stop_frame if source.distance.stop_frame is not None else frame_count
        expected = (end - source.distance.start_frame + source.distance.stride - 1) // source.distance.stride
        if len(data["points"]) != expected:
            raise AnalysisError("invalid_analysis_data", "真实轨迹样本数与阶段记录不匹配。", 422)
    else:
        data = read_trajectory(private, result, source.frame_index, source.limit)
    return {**data, "job_id": str(source.job_id), "stage_run_id": source.stage_run_id}


def main():
    try:
        # Native libraries may print diagnostics. Only the final bounded JSON
        # envelope belongs on stdout; private diagnostics are never returned.
        with redirect_stdout(sys.stderr):
            if "--probe" in sys.argv:
                import mdtraj
                import numpy
                import pandas
                import scipy
                result = {"ready": True, "mdtraj_version": mdtraj.__version__}
            else:
                raw = sys.stdin.buffer.read(2 * 1024 ** 2 + 65537)
                if len(raw) > 2 * 1024 ** 2 + 65536:
                    raise AnalysisError("analysis_request_too_large", "分析请求超过允许大小。", 413)
                result = execute(strict_json(raw))
        response = {"ok": True, "result": result}
        encoded = json.dumps(response, allow_nan=False, ensure_ascii=False).encode()
        if len(encoded) > RESPONSE_LIMIT:
            raise AnalysisError("analysis_response_invalid", "分析结果超过允许大小。", 502)
    except AnalysisError as error:
        encoded = json.dumps({"ok": False, "error": {**error.body(), "status": error.status}}, ensure_ascii=False).encode()
    except (ValueError, TypeError, KeyError, IndexError):
        encoded = json.dumps({"ok": False, "error": {"code": "invalid_analysis_data", "message": "轨迹或分析参数无效。", "status": 422}}).encode()
    except (FileNotFoundError, NotADirectoryError):
        encoded = json.dumps({"ok": False, "error": {"code": "artifact_unavailable", "message": "轨迹输入已移除或暂不可读。", "status": 409}}).encode()
    except OSError:
        encoded = json.dumps({"ok": False, "error": {"code": "analysis_storage_unavailable", "message": "只读分析存储暂不可用。", "status": 503}}).encode()
    except Exception:
        encoded = json.dumps({"ok": False, "error": {"code": "analysis_failed", "message": "距离或轨迹分析失败；后台模拟继续运行。", "status": 503}}).encode()
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
