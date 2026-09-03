// @vitest-environment jsdom

import { describe, expect, it } from "vitest";
import type { MonomerMdTrajectoryTimeline } from "../../types";
import {
  decodeMonomerMdTrajectoryTimeline,
  monomerMdTimelineFrameDelayMs,
  monomerMdTimelineFramePoints,
  monomerMdTimelineProjectionBounds,
  type DecodedMonomerMdTrajectoryTimeline
} from "./trajectoryTimeline";

const timeline: MonomerMdTrajectoryTimeline = {
  schema_version: 1,
  source_frame_count: 10,
  sampled_frame_count: 2,
  total_atoms: 4,
  sampled_points: 2,
  coordinate_unit: "angstrom",
  coordinate_scale: 0.01,
  coordinate_encoding: "int16-delta-gzip-base64",
  coordinate_byte_order: "little",
  decoded_byte_length: 24,
  compressed_byte_length: 44,
  sampling_strategy: "whole_residue_component_stratified",
  atoms: [
    { atom_id: 1, chain_id: 1, atom_type: "C", residue_name: "SOL", element: "C" },
    { atom_id: 2, chain_id: 1, atom_type: "O", residue_name: "SOL", element: "O" }
  ],
  frames: [
    { frame_index: 0, time_ps: 1, box: { lx: 20, ly: 20, lz: 20, unit: "angstrom" } },
    { frame_index: 9, time_ps: 10, box: { lx: 19, ly: 19, lz: 19, unit: "angstrom" } }
  ],
  coordinates: "H4sIAAAAAAACA0thOMGgwziB8QtjBBMXgwiDHIMGgxGDDQMA3AVZwhgAAAA="
};

describe("trajectory timeline decoder", () => {
  it("decodes little-endian int16 deltas into frame coordinates", async () => {
    const decoded = await decodeMonomerMdTrajectoryTimeline(timeline);

    expect(decoded.sampledFrameCount).toBe(2);
    expect(decoded.sampledPoints).toBe(2);
    expect(monomerMdTimelineFramePoints(decoded, 0)).toEqual([
      expect.objectContaining({ atom_id: 1, x: 1, y: 2, z: 3 }),
      expect.objectContaining({ atom_id: 2, x: 4, y: 5, z: 6 })
    ]);
    expect(monomerMdTimelineFramePoints(decoded, 1)).toEqual([
      expect.objectContaining({ atom_id: 1, x: expect.closeTo(1.1), y: expect.closeTo(2.2), z: expect.closeTo(3.3) }),
      expect.objectContaining({ atom_id: 2, x: expect.closeTo(4.4), y: expect.closeTo(5.5), z: expect.closeTo(6.6) })
    ]);
    expect(monomerMdTimelineProjectionBounds(decoded)).toEqual({
      minX: 0,
      minY: 0,
      minZ: 0,
      maxX: 20,
      maxY: 20,
      maxZ: 20
    });
  });

  it("uses simulated-time spacing instead of equal keyframe delays", () => {
    const decoded: DecodedMonomerMdTrajectoryTimeline = {
      sourceFrameCount: 100,
      sampledFrameCount: 4,
      totalAtoms: 1,
      sampledPoints: 1,
      coordinateUnit: "angstrom",
      atoms: [{ atom_id: 1, atom_type: "C" }],
      frames: [
        { frame_index: 0, time_ps: 0 },
        { frame_index: 1, time_ps: 1 },
        { frame_index: 2, time_ps: 2 },
        { frame_index: 99, time_ps: 10 }
      ],
      coordinates: new Float32Array(12)
    };

    expect(monomerMdTimelineFrameDelayMs(decoded, 0)).toBe(48);
    expect(monomerMdTimelineFrameDelayMs(decoded, 2)).toBe(384);
    expect(monomerMdTimelineFrameDelayMs(decoded, 2, 2)).toBe(192);
  });

  it("falls back to source-frame spacing when timeline times are incomplete", () => {
    const decoded: DecodedMonomerMdTrajectoryTimeline = {
      sourceFrameCount: 11,
      sampledFrameCount: 3,
      totalAtoms: 1,
      sampledPoints: 1,
      coordinateUnit: "angstrom",
      atoms: [{ atom_id: 1, atom_type: "C" }],
      frames: [
        { frame_index: 0, time_ps: 0 },
        { frame_index: 1 },
        { frame_index: 10, time_ps: 10 }
      ],
      coordinates: new Float32Array(9)
    };

    expect(monomerMdTimelineFrameDelayMs(decoded, 0)).toBe(40);
    expect(monomerMdTimelineFrameDelayMs(decoded, 1)).toBe(288);
  });

  it("rejects dimensions above the bounded 60-frame, 2000-atom contract", async () => {
    await expect(decodeMonomerMdTrajectoryTimeline({
      ...timeline,
      sampled_frame_count: 61
    })).rejects.toThrow("元数据无效");
  });
});
