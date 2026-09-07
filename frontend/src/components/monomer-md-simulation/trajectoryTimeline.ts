import type {
  MonomerMdTrajectoryAtom,
  MonomerMdTrajectoryPoint,
  MonomerMdTrajectoryTimeline,
  MonomerMdTrajectoryTimelineFrame
} from "../../types";
import { ungzip } from "pako";

const MAX_TIMELINE_FRAMES = 60;
const MAX_TIMELINE_ATOMS = 2_000;
const COORDINATES_PER_ATOM = 3;
const INT16_BYTES = 2;
const MAX_COMPRESSED_BASE64_LENGTH = 1_500_000;
const TARGET_FRAME_INTERVAL_MS = 160;
const MIN_FRAME_INTERVAL_MS = 40;
const MAX_FRAME_INTERVAL_MS = 1_000;

export type DecodedMonomerMdTrajectoryTimeline = {
  sourceFrameCount: number;
  sampledFrameCount: number;
  totalAtoms: number;
  sampledPoints: number;
  coordinateUnit: string;
  atoms: MonomerMdTrajectoryAtom[];
  frames: MonomerMdTrajectoryTimelineFrame[];
  coordinates: Float32Array;
};

export type MonomerMdTrajectoryProjectionBounds = {
  minX: number;
  minY: number;
  minZ: number;
  maxX: number;
  maxY: number;
  maxZ: number;
};

function finiteInteger(value: number, minimum: number, maximum: number): boolean {
  return Number.isInteger(value) && value >= minimum && value <= maximum;
}

function decodeBase64(value: string): Uint8Array {
  if (!value || value.length > MAX_COMPRESSED_BASE64_LENGTH) {
    throw new Error("轨迹时间轴压缩数据大小无效");
  }
  let binary: string;
  try {
    binary = atob(value);
  } catch {
    throw new Error("轨迹时间轴压缩数据不是有效的 Base64");
  }
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

function gunzip(bytes: Uint8Array): Uint8Array {
  try {
    return ungzip(bytes);
  } catch {
    throw new Error("轨迹时间轴压缩数据无法解码");
  }
}

function validateTimeline(timeline: MonomerMdTrajectoryTimeline): number {
  if (
    timeline.schema_version !== 1
    || timeline.coordinate_encoding !== "int16-delta-gzip-base64"
    || timeline.coordinate_byte_order !== "little"
    || !finiteInteger(timeline.sampled_frame_count, 1, MAX_TIMELINE_FRAMES)
    || !finiteInteger(timeline.sampled_points, 1, MAX_TIMELINE_ATOMS)
    || !finiteInteger(timeline.source_frame_count, timeline.sampled_frame_count, Number.MAX_SAFE_INTEGER)
    || !finiteInteger(timeline.total_atoms, timeline.sampled_points, Number.MAX_SAFE_INTEGER)
    || !Number.isFinite(timeline.coordinate_scale)
    || timeline.coordinate_scale <= 0
    || timeline.coordinate_scale > 1
    || timeline.frames.length !== timeline.sampled_frame_count
    || timeline.atoms.length !== timeline.sampled_points
  ) {
    throw new Error("轨迹时间轴元数据无效");
  }
  const expectedBytes = timeline.sampled_frame_count
    * timeline.sampled_points
    * COORDINATES_PER_ATOM
    * INT16_BYTES;
  if (timeline.decoded_byte_length !== expectedBytes) {
    throw new Error("轨迹时间轴坐标长度与元数据不一致");
  }
  return expectedBytes;
}

export async function decodeMonomerMdTrajectoryTimeline(
  timeline: MonomerMdTrajectoryTimeline
): Promise<DecodedMonomerMdTrajectoryTimeline> {
  const expectedBytes = validateTimeline(timeline);
  const compressed = decodeBase64(timeline.coordinates);
  if (
    timeline.compressed_byte_length != null
    && timeline.compressed_byte_length !== compressed.byteLength
  ) {
    throw new Error("轨迹时间轴压缩长度与元数据不一致");
  }
  const decoded = gunzip(compressed);
  if (decoded.byteLength !== expectedBytes) {
    throw new Error("轨迹时间轴解压后长度与元数据不一致");
  }

  const coordinateCount = expectedBytes / INT16_BYTES;
  const coordinateValues = new Float32Array(coordinateCount);
  const previous = new Int32Array(timeline.sampled_points * COORDINATES_PER_ATOM);
  const view = new DataView(decoded.buffer, decoded.byteOffset, decoded.byteLength);
  const frameStride = previous.length;
  for (let frameIndex = 0; frameIndex < timeline.sampled_frame_count; frameIndex += 1) {
    const offset = frameIndex * frameStride;
    for (let coordinateIndex = 0; coordinateIndex < frameStride; coordinateIndex += 1) {
      const encoded = view.getInt16((offset + coordinateIndex) * INT16_BYTES, true);
      const quantized = frameIndex === 0
        ? encoded
        : previous[coordinateIndex] + encoded;
      previous[coordinateIndex] = quantized;
      coordinateValues[offset + coordinateIndex] = quantized * timeline.coordinate_scale;
    }
  }

  return {
    sourceFrameCount: timeline.source_frame_count,
    sampledFrameCount: timeline.sampled_frame_count,
    totalAtoms: timeline.total_atoms,
    sampledPoints: timeline.sampled_points,
    coordinateUnit: timeline.coordinate_unit,
    atoms: timeline.atoms,
    frames: timeline.frames,
    coordinates: coordinateValues
  };
}

export function monomerMdTimelineFramePoints(
  timeline: DecodedMonomerMdTrajectoryTimeline,
  framePosition: number
): MonomerMdTrajectoryPoint[] {
  if (!finiteInteger(framePosition, 0, timeline.sampledFrameCount - 1)) return [];
  const frameOffset = framePosition * timeline.sampledPoints * COORDINATES_PER_ATOM;
  return timeline.atoms.map((atom, atomIndex) => {
    const offset = frameOffset + atomIndex * COORDINATES_PER_ATOM;
    return {
      ...atom,
      x: timeline.coordinates[offset],
      y: timeline.coordinates[offset + 1],
      z: timeline.coordinates[offset + 2]
    };
  });
}

export function monomerMdTimelineProjectionBounds(
  timeline: DecodedMonomerMdTrajectoryTimeline
): MonomerMdTrajectoryProjectionBounds {
  const bounds: MonomerMdTrajectoryProjectionBounds = {
    minX: Number.POSITIVE_INFINITY,
    minY: Number.POSITIVE_INFINITY,
    minZ: Number.POSITIVE_INFINITY,
    maxX: Number.NEGATIVE_INFINITY,
    maxY: Number.NEGATIVE_INFINITY,
    maxZ: Number.NEGATIVE_INFINITY
  };
  for (let index = 0; index < timeline.coordinates.length; index += 3) {
    const x = timeline.coordinates[index];
    const y = timeline.coordinates[index + 1];
    const z = timeline.coordinates[index + 2];
    bounds.minX = Math.min(bounds.minX, x);
    bounds.minY = Math.min(bounds.minY, y);
    bounds.minZ = Math.min(bounds.minZ, z);
    bounds.maxX = Math.max(bounds.maxX, x);
    bounds.maxY = Math.max(bounds.maxY, y);
    bounds.maxZ = Math.max(bounds.maxZ, z);
  }
  timeline.frames.forEach((frame) => {
    const box = frame.box;
    if (!box) return;
    if (Number.isFinite(box.lx) && box.lx > 0) {
      bounds.minX = Math.min(bounds.minX, 0);
      bounds.maxX = Math.max(bounds.maxX, box.lx);
    }
    if (Number.isFinite(box.ly) && box.ly > 0) {
      bounds.minY = Math.min(bounds.minY, 0);
      bounds.maxY = Math.max(bounds.maxY, box.ly);
    }
    if (Number.isFinite(box.lz) && box.lz > 0) {
      bounds.minZ = Math.min(bounds.minZ, 0);
      bounds.maxZ = Math.max(bounds.maxZ, box.lz);
    }
  });
  if (!Object.values(bounds).every(Number.isFinite)) {
    return { minX: -1, minY: -1, minZ: -1, maxX: 1, maxY: 1, maxZ: 1 };
  }
  return bounds;
}

export function monomerMdTimelineFrameDelayMs(
  timeline: DecodedMonomerMdTrajectoryTimeline,
  framePosition: number,
  speed = 1
): number {
  const frameCount = timeline.frames.length;
  const playbackSpeed = Number.isFinite(speed) && speed > 0 ? speed : 1;
  if (frameCount < 2 || framePosition < 0 || framePosition >= frameCount - 1) {
    return TARGET_FRAME_INTERVAL_MS / playbackSpeed;
  }

  const timeAxis = timeline.frames.map((frame) => frame.time_ps);
  const hasMonotonicTime = timeAxis.every((value, index) => (
    typeof value === "number"
    && Number.isFinite(value)
    && (index === 0 || value > (timeAxis[index - 1] as number))
  ));
  const axis = hasMonotonicTime
    ? timeAxis as number[]
    : timeline.frames.map((frame) => frame.frame_index);
  const totalSpan = axis[axis.length - 1] - axis[0];
  const frameSpan = axis[framePosition + 1] - axis[framePosition];
  if (!Number.isFinite(totalSpan) || totalSpan <= 0 || !Number.isFinite(frameSpan) || frameSpan <= 0) {
    return TARGET_FRAME_INTERVAL_MS / playbackSpeed;
  }

  const targetDuration = TARGET_FRAME_INTERVAL_MS * (frameCount - 1);
  const proportionalDelay = targetDuration * frameSpan / totalSpan;
  return Math.min(
    MAX_FRAME_INTERVAL_MS,
    Math.max(MIN_FRAME_INTERVAL_MS, proportionalDelay)
  ) / playbackSpeed;
}
