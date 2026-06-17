/**
 * MapLibre GL JS custom layer for MRMS national composite reflectivity.
 *
 * Unlike the NEXRAD layer (polar → Cartesian conversion per pixel), MRMS data is
 * already a regular lat/lon Cartesian grid.  The fragment shader just converts each
 * screen pixel's Mercator position to a lat/lon, computes the grid index, and samples
 * the uint8 data texture.  GL_LINEAR filtering handles bilinear interpolation for free.
 *
 * This gives pixel-perfect rendering at any zoom level with no upscaling artefacts.
 *
 * Binary wire format from /api/mrms/binary (all little-endian):
 *   [0:4]   'MRMS' magic
 *   [4:8]   uint32 ni (columns)
 *   [8:12]  uint32 nj (rows, row 0 = north)
 *   [12:20] float64 north   (La1)
 *   [20:28] float64 south   (La2)
 *   [28:36] float64 west    (Lo1 in [-180,180])
 *   [36:44] float64 east    (Lo2 in [-180,180])
 *   [44:48] float32 vmin    (dBZ)
 *   [48:52] float32 vmax    (dBZ)
 *   [52:]   uint8[] gate values; 0=no-data, 1-255=normalised
 */

import { MercatorCoordinate } from 'maplibre-gl';
import type { Map as MaplibreMap, CustomLayerInterface, CustomRenderMethodInput } from 'maplibre-gl';
import { getLUTForProduct } from '../utils/radarLut';

// ─── Shaders ─────────────────────────────────────────────────────────────────

const VERT = `#version 300 es
uniform mat4 u_matrix;
in  vec2 a_pos;   // Mercator [0..1] × [0..1]
out vec2 v_merc;
void main() {
  gl_Position = u_matrix * vec4(a_pos, 0.0, 1.0);
  v_merc = a_pos;
}`;

const FRAG = `#version 300 es
precision highp float;

uniform sampler2D u_lut;    // 256×1 RGBA colour table
uniform sampler2D u_data;   // R8 normalised grid, GL_LINEAR, N→S row order
uniform float u_north;      // La1 (northernmost row, degrees)
uniform float u_south;      // La2 (southernmost row, degrees)
uniform float u_west;       // Lo1 (degrees, negative = west)
uniform float u_east;       // Lo2 (degrees)
uniform float u_opacity;
uniform float u_gate_threshold; // normalised [0,1] below which = transparent

in  vec2 v_merc;
out vec4 frag_color;

const float PI = 3.14159265358979323846;

void main() {
  // MapLibre Mercator → lon/lat
  float lon     = v_merc.x * 360.0 - 180.0;
  float lat_rad = 2.0 * atan(exp((1.0 - 2.0 * v_merc.y) * PI)) - PI * 0.5;
  float lat     = degrees(lat_rad);

  // Outside grid bounds → transparent
  if (lat > u_north || lat < u_south || lon < u_west || lon > u_east) {
    frag_color = vec4(0.0); return;
  }

  // Map lat/lon to normalised texture UV
  // u: 0 = west, 1 = east  |  v: 0 = north (row 0), 1 = south (last row)
  float u_tex = (lon - u_west)   / (u_east  - u_west);
  float v_tex = (u_north - lat)  / (u_north - u_south);

  // Sample the grid — GL_LINEAR gives GPU bilinear interpolation for free
  float f = texture(u_data, vec2(u_tex, v_tex)).r;

  if (f < u_gate_threshold || f < 0.004) { frag_color = vec4(0.0); return; }

  vec4 color = texture(u_lut, vec2(f, 0.5));
  // Premultiplied alpha output (matches RadarGLLayer)
  float alpha = color.a * u_opacity;
  frag_color = vec4(color.rgb * alpha, alpha);
}`;

// ─── Helpers ─────────────────────────────────────────────────────────────────

function compile(gl: WebGL2RenderingContext, type: number, src: string): WebGLShader {
  const s = gl.createShader(type)!;
  gl.shaderSource(s, src); gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS))
    throw new Error(`MRMS shader: ${gl.getShaderInfoLog(s)}`);
  return s;
}

function link(gl: WebGL2RenderingContext, vs: WebGLShader, fs: WebGLShader): WebGLProgram {
  const p = gl.createProgram()!;
  gl.attachShader(p, vs); gl.attachShader(p, fs); gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS))
    throw new Error(`MRMS link: ${gl.getProgramInfoLog(p)}`);
  gl.deleteShader(vs); gl.deleteShader(fs);
  return p;
}

// ─── Binary parser ────────────────────────────────────────────────────────────

export interface MRMSFrame {
  ni: number; nj: number;
  north: number; south: number; west: number; east: number;
  vmin: number; vmax: number;
  gate: Uint8Array;
  timestamp?: string;  // ISO string, injected client-side from frame list metadata
}

const MRMS_MAGIC = [0x4d, 0x52, 0x4d, 0x53]; // 'MRMS'

export function parseMRMSBinary(buf: ArrayBuffer): MRMSFrame {
  const v = new DataView(buf);
  for (let i = 0; i < 4; i++)
    if (v.getUint8(i) !== MRMS_MAGIC[i]) throw new Error('Invalid MRMS magic');
  const ni    = v.getUint32(4,  true);
  const nj    = v.getUint32(8,  true);
  const north = v.getFloat64(12, true);
  const south = v.getFloat64(20, true);
  const west  = v.getFloat64(28, true);
  const east  = v.getFloat64(36, true);
  const vmin  = v.getFloat32(44, true);
  const vmax  = v.getFloat32(48, true);
  const gate  = new Uint8Array(buf, 52, ni * nj);
  return { ni, nj, north, south, west, east, vmin, vmax, gate };
}

// ─── Layer class ──────────────────────────────────────────────────────────────

export class MRMSGLLayer implements CustomLayerInterface {
  readonly type          = 'custom' as const;
  readonly renderingMode = '2d'     as const;

  private _gl:      WebGL2RenderingContext | null = null;
  private _prog:    WebGLProgram            | null = null;
  private _vao:     WebGLVertexArrayObject  | null = null;
  private _posBuf:  WebGLBuffer             | null = null;
  private _lut:     WebGLTexture            | null = null;
  private _dataTex: WebGLTexture            | null = null;
  private _u:       Record<string, WebGLUniformLocation | null> = {};

  private _frame:          MRMSFrame | null = null;
  private _pending:        MRMSFrame | null = null;
  private _opacity         = 0.7;
  private _gateDbz         = 10.0;   // dBZ threshold; matches NEXRAD gate filter default
  private _gateThreshNorm  = 0.30;   // pre-computed: (10 - (-20)) / (80 - (-20)) = 0.30

  constructor(public readonly id: string) {}

  setFrame(frame: MRMSFrame | null): void {
    this._pending = frame;
    // Recompute normalised threshold whenever vmin/vmax could change
    if (frame) this._updateThreshold(frame.vmin, frame.vmax);
  }

  setOpacity(o: number): void { this._opacity = Math.max(0, Math.min(1, o)); }

  setGateDbz(dbz: number): void {
    this._gateDbz = dbz;
    const f = this._frame ?? this._pending;
    if (f) this._updateThreshold(f.vmin, f.vmax);
  }

  private _updateThreshold(vmin: number, vmax: number): void {
    const span = vmax - vmin;
    this._gateThreshNorm = span > 0 ? Math.max(0, (this._gateDbz - vmin) / span) : 0;
  }

  onAdd(_map: MaplibreMap, gl: WebGL2RenderingContext): void {
    this._gl = gl;

    const vs = compile(gl, gl.VERTEX_SHADER,   VERT);
    const fs = compile(gl, gl.FRAGMENT_SHADER, FRAG);
    this._prog = link(gl, vs, fs);

    // Static fullscreen quad for coverage of the MRMS bounding box
    this._posBuf = gl.createBuffer()!;
    this._vao    = gl.createVertexArray()!;
    gl.bindVertexArray(this._vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, this._posBuf);
    const aPos = gl.getAttribLocation(this._prog, 'a_pos');
    gl.enableVertexAttribArray(aPos);
    gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);
    gl.bindBuffer(gl.ARRAY_BUFFER, null);

    // LUT texture (same reflectivity colormap as NEXRAD)
    this._lut = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, this._lut);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, 256, 1, 0,
      gl.RGBA, gl.UNSIGNED_BYTE, getLUTForProduct('reflectivity'));

    const u = (n: string) => gl.getUniformLocation(this._prog!, n);
    this._u = {
      matrix:    u('u_matrix'),
      north:     u('u_north'),   south: u('u_south'),
      west:      u('u_west'),    east:  u('u_east'),
      opacity:   u('u_opacity'),
      gateThreshold: u('u_gate_threshold'),
      lut:       u('u_lut'),
      data:      u('u_data'),
    };
  }

  prerender(_glUnion: WebGLRenderingContext | WebGL2RenderingContext, _args: CustomRenderMethodInput): void {
    const gl = this._gl;
    if (!gl || !this._prog) return;

    // Upload new frame if pending
    if (this._pending !== this._frame) {
      if (this._pending) this._uploadFrame(gl, this._pending);
      else               this._clearData(gl);
      this._frame = this._pending;
    }
  }

  render(_glUnion: WebGLRenderingContext | WebGL2RenderingContext, args: CustomRenderMethodInput): void {
    const gl = this._gl;
    if (!gl || !this._prog || !this._frame || !this._dataTex) return;

    const f = this._frame;
    gl.useProgram(this._prog);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);

    gl.uniformMatrix4fv(this._u.matrix!, false,
      (args as unknown as { defaultProjectionData: { mainMatrix: Float32Array } })
        .defaultProjectionData.mainMatrix);
    gl.uniform1f(this._u.north!,  f.north);
    gl.uniform1f(this._u.south!,  f.south);
    gl.uniform1f(this._u.west!,   f.west);
    gl.uniform1f(this._u.east!,   f.east);
    gl.uniform1f(this._u.opacity!, this._opacity);
    gl.uniform1f(this._u.gateThreshold!, this._gateThreshNorm);

    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, this._lut);     gl.uniform1i(this._u.lut!,  0);
    gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, this._dataTex); gl.uniform1i(this._u.data!, 1);

    gl.bindVertexArray(this._vao);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
    gl.bindVertexArray(null);
  }

  onRemove(_map: MaplibreMap, gl: WebGL2RenderingContext): void {
    this._clearData(gl);
    if (this._lut)    gl.deleteTexture(this._lut);
    if (this._vao)    gl.deleteVertexArray(this._vao);
    if (this._posBuf) gl.deleteBuffer(this._posBuf);
    if (this._prog)   gl.deleteProgram(this._prog);
    this._gl = null;
  }

  private _clearData(gl: WebGL2RenderingContext): void {
    if (this._dataTex) { gl.deleteTexture(this._dataTex); this._dataTex = null; }
  }

  private _uploadFrame(gl: WebGL2RenderingContext, frame: MRMSFrame): void {
    this._clearData(gl);

    const { ni, nj, north, south, west, east } = frame;

    // Update vertex quad to cover the MRMS bounding box in Mercator
    const nw = MercatorCoordinate.fromLngLat({ lng: west, lat: north });
    const sw = MercatorCoordinate.fromLngLat({ lng: west, lat: south });
    const ne = MercatorCoordinate.fromLngLat({ lng: east, lat: north });
    const se = MercatorCoordinate.fromLngLat({ lng: east, lat: south });
    gl.bindBuffer(gl.ARRAY_BUFFER, this._posBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
      nw.x, nw.y,  sw.x, sw.y,  ne.x, ne.y,
      sw.x, sw.y,  se.x, se.y,  ne.x, ne.y,
    ]), gl.DYNAMIC_DRAW);
    gl.bindBuffer(gl.ARRAY_BUFFER, null);

    // Upload data as R8 (normalised float) with LINEAR filtering.
    // GL_LINEAR lets the GPU handle bilinear interpolation between grid points —
    // this is the key to getting smooth WeatherWise-quality rendering.
    this._dataTex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, this._dataTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);

    // R8 (not R8UI) — normalised uint8 so GL_LINEAR works and shader gets [0,1]
    gl.texImage2D(
      gl.TEXTURE_2D, 0, gl.R8,
      ni, nj, 0,
      gl.RED, gl.UNSIGNED_BYTE,
      frame.gate,
    );
  }
}
