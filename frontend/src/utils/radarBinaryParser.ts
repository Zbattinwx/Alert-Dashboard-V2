import type { RadarBinaryFrame } from '../types/radar';

const MAGIC = [0x52, 0x44, 0x52, 0x46]; // 'RDRF'

interface RdrfMeta {
  product: string;
  site: string;
  timestamp: string;
  elevation: number;
  vmin: number;
  vmax: number;
  max_range_m: number;
  bounds: { south: number; north: number; west: number; east: number };
}

/**
 * Parse a RDRF binary radar frame into a RadarBinaryFrame.
 *
 * Wire format (all multi-byte integers little-endian):
 *   [0:4]   magic 'RDRF'
 *   [4:8]   uint32 metadata_len
 *   [8:12]  uint32 n_rays
 *   [12:16] uint32 n_gates
 *   [16:]   UTF-8 JSON metadata (metadata_len bytes)
 *   [...]   float32[] azimuths (degrees, n_rays entries)
 *   [...]   float32[] ranges_m (metres, n_gates entries)
 *   [...]   uint8[]   gate_values row-major [ray][gate]; 0 = no-data
 *
 * All typed-array views are zero-copy — the caller must keep the ArrayBuffer
 * alive as long as the returned RadarBinaryFrame is in use.
 */
export function parseRadarBinaryFrame(buffer: ArrayBuffer): RadarBinaryFrame {
  const view = new DataView(buffer);

  for (let i = 0; i < 4; i++) {
    if (view.getUint8(i) !== MAGIC[i]) {
      throw new Error(`Invalid RDRF magic bytes at offset ${i}`);
    }
  }

  const metaLen = view.getUint32(4, true);
  const nRays   = view.getUint32(8, true);
  const nGates  = view.getUint32(12, true);

  const metaStart = 16;
  const metaBytes = new Uint8Array(buffer, metaStart, metaLen);
  const meta: RdrfMeta = JSON.parse(new TextDecoder().decode(metaBytes));

  const azStart   = metaStart + metaLen;
  const rngStart  = azStart  + nRays  * 4;
  const gateStart = rngStart + nGates * 4;

  const expectedTotal = gateStart + nRays * nGates;
  if (buffer.byteLength < expectedTotal) {
    throw new Error(
      `Buffer too small: expected ${expectedTotal} bytes, got ${buffer.byteLength}`
    );
  }

  return {
    product:     meta.product,
    site:        meta.site,
    elevation:   meta.elevation,
    timestamp:   meta.timestamp,
    bounds:      meta.bounds,
    frame_id:    `${meta.site}_${meta.timestamp}_${meta.product}`,
    has_binary:  true,
    n_rays:      nRays,
    n_gates:     nGates,
    azimuths:    new Float32Array(buffer, azStart,   nRays),
    ranges_m:    new Float32Array(buffer, rngStart,  nGates),
    gate_values: new Uint8Array  (buffer, gateStart, nRays * nGates),
    vmin:        meta.vmin,
    vmax:        meta.vmax,
    max_range_m: meta.max_range_m,
  };
}
