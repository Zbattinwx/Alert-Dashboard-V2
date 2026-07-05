/**
 * MapLibre GL JS custom layer — NEXRAD radar with two-pass screen-space Gaussian blur.
 *
 * Rendering pipeline (when smoothing > 0):
 *   prerender pass 1: radar polar → RGBA8 FBO  (nearest-neighbour gate lookup)
 *   prerender pass 2: horizontal Gaussian blur FBO→FBO2
 *   render   pass 3: vertical Gaussian blur FBO2 → MapLibre's framebuffer
 *
 * When smoothing = 0 the FBO passes are skipped and the radar is drawn directly
 * to MapLibre's framebuffer in render(), with zero overhead.
 *
 * Screen-space blur gives uniform, zoom-independent smoothing that matches
 * the professional look of WeatherWise and other commercial radar apps.
 */

import { MercatorCoordinate } from 'maplibre-gl';
import type {
  Map as MaplibreMap,
  CustomLayerInterface,
  CustomRenderMethodInput,
} from 'maplibre-gl';
import type { RadarBinaryFrame } from '../types/radar';
import { getLUTForProduct } from '../utils/radarLut';

// ─── Radar shader (pass 1) ────────────────────────────────────────────────────
// Renders raw radar data from polar gate texture to an RGBA8 FBO.
// Outputs premultiplied alpha so the Gaussian blur composites correctly.

const RADAR_VERT = `#version 300 es
uniform mat4 u_matrix;
in  vec2 a_pos;   // MapLibre Mercator [0..1] × [0..1]
out vec2 v_merc;
void main() {
  gl_Position = u_matrix * vec4(a_pos, 0.0, 1.0);
  v_merc = a_pos;
}`;

const RADAR_FRAG = `#version 300 es
precision highp float;
precision highp int;
precision highp sampler2D;
precision highp usampler2D;

uniform sampler2D  u_lut;
uniform sampler2D  u_azimuths;
uniform sampler2D  u_ranges;
uniform usampler2D u_gate_data;
uniform float u_max_range_m;
uniform float u_radar_lat;
uniform float u_radar_lon;
uniform float u_opacity;
uniform int   u_n_rays;
uniform int   u_n_gates;
uniform float u_gate_threshold;   // normalised gate threshold (client-side filter)

in  vec2 v_merc;
out vec4 frag_color;

const float PI      = 3.14159265358979323846;
const float DEG2RAD = PI / 180.0;
const float EARTH_R = 6371000.0;

void main() {
  // MapLibre Mercator inverse (Y = 0 at north, Y = 1 at south)
  float lon     = v_merc.x * 360.0 - 180.0;
  float lat_rad = 2.0 * atan(exp((1.0 - 2.0 * v_merc.y) * PI)) - PI * 0.5;
  float lat     = degrees(lat_rad);

  // Equirectangular AEQD — use pixel's own latitude for accuracy across the disk
  float dlat_rad = (lat - u_radar_lat) * DEG2RAD;
  float dlon_rad = (lon - u_radar_lon) * DEG2RAD;
  float x_m = EARTH_R * dlon_rad * cos(lat_rad);
  float y_m = EARTH_R * dlat_rad;

  float rho = length(vec2(x_m, y_m));
  if (rho > u_max_range_m) { frag_color = vec4(0.0); return; }

  float theta = degrees(atan(x_m, y_m));
  if (theta < 0.0) theta += 360.0;

  // Nearest-neighbour ray and gate (crisp gate boundaries pre-blur)
  float ray_spacing = 360.0 / float(u_n_rays);
  int   ray  = int(theta / ray_spacing) % u_n_rays;

  float r0         = texelFetch(u_ranges, ivec2(0, 0), 0).r;
  float r1         = texelFetch(u_ranges, ivec2(1, 0), 0).r;
  float gate_space = max(r1 - r0, 1.0);
  int   gate       = clamp(int((rho - r0) / gate_space + 0.5), 0, u_n_gates - 1);

  uint v = texelFetch(u_gate_data, ivec2(gate, ray), 0).r;
  if (v == 0u) { frag_color = vec4(0.0); return; }

  float f = float(v) / 255.0;
  if (f < u_gate_threshold || f < 0.004) { frag_color = vec4(0.0); return; }

  vec4 color = texture(u_lut, vec2(f, 0.5));
  // Premultiplied alpha output so Gaussian blur composites correctly
  float alpha = color.a * u_opacity;
  frag_color = vec4(color.rgb * alpha, alpha);
}`;

// ─── Screen-space Gaussian blur shader (passes 2 and 3) ─────────────────────
// Separable 1D Gaussian.  Direction is controlled by u_dir uniform.
// Uses simple weighted average — correct for premultiplied alpha input.

const BLUR_VERT = `#version 300 es
in  vec2 a_pos;
out vec2 v_tc;
void main() {
  // FBO texture coords: (0,0) bottom-left, (1,1) top-right (same as framebuffer)
  v_tc        = a_pos * 0.5 + 0.5;
  gl_Position = vec4(a_pos, 0.0, 1.0);
}`;

const BLUR_FRAG = `#version 300 es
precision highp float;

uniform sampler2D u_tex;
uniform vec2  u_dir;    // (1/w, 0) for horizontal, (0, 1/h) for vertical
uniform float u_sigma;  // Gaussian sigma in screen pixels; 0 = passthrough

in  vec2 v_tc;
out vec4 frag_color;

void main() {
  if (u_sigma < 0.05) {
    frag_color = texture(u_tex, v_tc);
    return;
  }

  // Cap kernel radius so the loop is finite and fast
  int   radius = min(int(ceil(u_sigma * 2.5)), 20);
  float s2     = 2.0 * u_sigma * u_sigma;

  vec4  total   = vec4(0.0);
  float w_total = 0.0;

  for (int i = -radius; i <= radius; i++) {
    float w  = exp(-float(i * i) / s2);
    vec2  uv = clamp(v_tc + float(i) * u_dir, vec2(0.0), vec2(1.0));
    total   += texture(u_tex, uv) * w;
    w_total += w;
  }

  frag_color = (w_total > 0.001) ? total / w_total : vec4(0.0);
}`;

// ─── Helpers ─────────────────────────────────────────────────────────────────

function compile(gl: WebGL2RenderingContext, type: number, src: string): WebGLShader {
  const s = gl.createShader(type)!;
  gl.shaderSource(s, src);
  gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS))
    throw new Error(`Radar GL shader: ${gl.getShaderInfoLog(s)}`);
  return s;
}

function link(gl: WebGL2RenderingContext, vs: WebGLShader, fs: WebGLShader): WebGLProgram {
  const p = gl.createProgram()!;
  gl.attachShader(p, vs); gl.attachShader(p, fs);
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS))
    throw new Error(`Radar GL link: ${gl.getProgramInfoLog(p)}`);
  gl.deleteShader(vs); gl.deleteShader(fs);
  return p;
}

function makeTex(gl: WebGL2RenderingContext, linear = false): WebGLTexture {
  const t = gl.createTexture()!;
  gl.bindTexture(gl.TEXTURE_2D, t);
  const f = linear ? gl.LINEAR : gl.NEAREST;
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, f);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, f);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  return t;
}

interface FBO { fbo: WebGLFramebuffer; tex: WebGLTexture; }

function createFBO(gl: WebGL2RenderingContext, w: number, h: number): FBO {
  const tex = makeTex(gl, true /* LINEAR for blur sampling */);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
  const fbo = gl.createFramebuffer()!;
  gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
  gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
  gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  return { fbo, tex };
}

// ─── Layer ───────────────────────────────────────────────────────────────────

export class RadarGLLayer implements CustomLayerInterface {
  readonly type          = 'custom' as const;
  readonly renderingMode = '2d'     as const;

  // GL resources
  private _gl: WebGL2RenderingContext | null = null;

  // Radar shader (pass 1)
  private _rProg:   WebGLProgram | null = null;
  private _rVAO:    WebGLVertexArrayObject | null = null;
  private _rPosBuf: WebGLBuffer | null = null;   // updated per-frame with Mercator coords
  private _rU:      Record<string, WebGLUniformLocation | null> = {};

  // Blur shader (passes 2 + 3)
  private _bProg:   WebGLProgram | null = null;
  private _bVAO:    WebGLVertexArrayObject | null = null;
  private _bPosBuf: WebGLBuffer | null = null;   // static fullscreen quad, never changed
  private _bU:      Record<string, WebGLUniformLocation | null> = {};

  // FBOs
  private _fbo1: FBO | null = null;
  private _fbo2: FBO | null = null;
  private _fboW  = 0;
  private _fboH  = 0;

  // Data textures
  private _lut:     WebGLTexture | null = null;
  private _lutProd  = '';
  private _azTex:   WebGLTexture | null = null;
  private _rngTex:  WebGLTexture | null = null;
  private _gateTex: WebGLTexture | null = null;
  // Dimensions the data textures are currently allocated at — the polar grid is
  // constant across frames of a product, so we keep the textures and update them
  // in place (texSubImage2D), reallocating only when these actually change.
  private _texRays  = 0;
  private _texGates = 0;

  // Frame state
  private _pending:  RadarBinaryFrame | null = null;
  private _uploaded: RadarBinaryFrame | null = null;

  // Display parameters
  private _opacity       = 0.7;
  private _smooth        = 0.0;   // 0 = NN, 1.0 = 5-px sigma Gaussian
  private _gateDbz       = 10.0;
  private _gateThreshold = 0.0;

  constructor(public readonly id: string) {}

  // ── Public setters ────────────────────────────────────────────────────────

  setFrame(frame: RadarBinaryFrame | null): void {
    this._pending = frame;
    if (frame && frame.product === 'reflectivity') {
      const span = frame.vmax - frame.vmin;
      this._gateThreshold = span > 0 ? Math.max(0, (this._gateDbz - frame.vmin) / span) : 0;
    } else {
      this._gateThreshold = 0;
    }
  }

  setOpacity(o: number): void { this._opacity = Math.max(0, Math.min(1, o)); }

  setSmooth(s: number): void { this._smooth = Math.max(0, Math.min(1, s)); }

  setGateDbz(dbz: number, frame?: RadarBinaryFrame | null): void {
    this._gateDbz = dbz;
    const f = frame ?? this._uploaded;
    if (f && f.product === 'reflectivity') {
      const span = f.vmax - f.vmin;
      this._gateThreshold = span > 0 ? Math.max(0, (dbz - f.vmin) / span) : 0;
    } else {
      this._gateThreshold = 0;
    }
  }

  // ── Lifecycle ─────────────────────────────────────────────────────────────

  onAdd(_map: MaplibreMap, gl: WebGL2RenderingContext): void {
    this._gl = gl;

    // ── Radar program ──────────────────────────────────────────────────────
    this._rProg = link(gl,
      compile(gl, gl.VERTEX_SHADER,   RADAR_VERT),
      compile(gl, gl.FRAGMENT_SHADER, RADAR_FRAG),
    );
    // Radar uses its own position buffer updated per-frame with Mercator coords.
    // Initialized to a zero-size quad; _uploadFrame writes the real bounds.
    this._rPosBuf = gl.createBuffer()!;
    gl.bindBuffer(gl.ARRAY_BUFFER, this._rPosBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(12), gl.DYNAMIC_DRAW);

    this._rVAO = gl.createVertexArray()!;
    gl.bindVertexArray(this._rVAO);
    gl.bindBuffer(gl.ARRAY_BUFFER, this._rPosBuf);
    const rLoc = gl.getAttribLocation(this._rProg, 'a_pos');
    gl.enableVertexAttribArray(rLoc);
    gl.vertexAttribPointer(rLoc, 2, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);

    const ru = (n: string) => gl.getUniformLocation(this._rProg!, n);
    this._rU = {
      matrix:        ru('u_matrix'),
      radarLat:      ru('u_radar_lat'),  radarLon:      ru('u_radar_lon'),
      maxRange:      ru('u_max_range_m'),
      opacity:       ru('u_opacity'),
      nRays:         ru('u_n_rays'),     nGates:        ru('u_n_gates'),
      gateThreshold: ru('u_gate_threshold'),
      lut:           ru('u_lut'),        az:            ru('u_azimuths'),
      rng:           ru('u_ranges'),     gate:          ru('u_gate_data'),
    };

    // ── LUT texture ────────────────────────────────────────────────────────
    this._lut = makeTex(gl, true);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, 256, 1, 0,
      gl.RGBA, gl.UNSIGNED_BYTE, getLUTForProduct('reflectivity'));
    this._lutProd = 'reflectivity';

    // ── Blur program ───────────────────────────────────────────────────────
    // Blur uses its OWN static fullscreen-quad buffer, never overwritten.
    this._bPosBuf = gl.createBuffer()!;
    gl.bindBuffer(gl.ARRAY_BUFFER, this._bPosBuf);
    gl.bufferData(gl.ARRAY_BUFFER,
      new Float32Array([-1,-1, 1,-1, -1,1, -1,1, 1,-1, 1,1]), gl.STATIC_DRAW);

    this._bProg = link(gl,
      compile(gl, gl.VERTEX_SHADER,   BLUR_VERT),
      compile(gl, gl.FRAGMENT_SHADER, BLUR_FRAG),
    );
    this._bVAO = gl.createVertexArray()!;
    gl.bindVertexArray(this._bVAO);
    gl.bindBuffer(gl.ARRAY_BUFFER, this._bPosBuf);
    const bLoc = gl.getAttribLocation(this._bProg, 'a_pos');
    gl.enableVertexAttribArray(bLoc);
    gl.vertexAttribPointer(bLoc, 2, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);
    gl.bindBuffer(gl.ARRAY_BUFFER, null);

    const bu = (n: string) => gl.getUniformLocation(this._bProg!, n);
    this._bU = { tex: bu('u_tex'), dir: bu('u_dir'), sigma: bu('u_sigma') };
  }

  /**
   * prerender: runs BEFORE MapLibre renders tiles/labels.
   * When smoothing is enabled, executes passes 1 and 2 into our own FBOs.
   * We save and restore MapLibre's framebuffer so nothing is disrupted.
   */
  prerender(
    _glUnion: WebGLRenderingContext | WebGL2RenderingContext,
    args: CustomRenderMethodInput,
  ): void {
    const gl = this._gl;
    if (!gl || !this._rProg || !this._bProg) return;
    if (this._smooth < 0.01) return; // bypass when no smoothing needed

    // Upload pending frame data
    if (this._pending !== this._uploaded) {
      if (this._pending) this._uploadFrame(gl, this._pending);
      else               this._clearDataTex(gl);
      this._uploaded = this._pending;
    }
    if (!this._uploaded || !this._azTex || !this._rngTex || !this._gateTex) return;

    const w = (gl.canvas as HTMLCanvasElement).width;
    const h = (gl.canvas as HTMLCanvasElement).height;
    this._ensureFBOs(gl, w, h);
    if (!this._fbo1 || !this._fbo2) return;

    // Save MapLibre's framebuffer so we can restore it before returning
    const prevFBO = gl.getParameter(gl.FRAMEBUFFER_BINDING) as WebGLFramebuffer | null;

    const mainMatrix = (args as unknown as { defaultProjectionData: { mainMatrix: Float32Array } })
      .defaultProjectionData.mainMatrix;

    // ── Pass 1: render radar to FBO1 ──────────────────────────────────────
    gl.bindFramebuffer(gl.FRAMEBUFFER, this._fbo1.fbo);
    gl.viewport(0, 0, w, h);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    this._drawRadar(gl, mainMatrix);

    // ── Pass 2: horizontal Gaussian blur FBO1 → FBO2 ─────────────────────
    const sigma = this._smooth * 5.0; // map 0-1 → 0-5 px sigma
    gl.bindFramebuffer(gl.FRAMEBUFFER, this._fbo2.fbo);
    gl.viewport(0, 0, w, h);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    this._drawBlur(gl, this._fbo1.tex, 1.0 / w, 0.0, sigma);

    // Restore MapLibre's framebuffer
    gl.bindFramebuffer(gl.FRAMEBUFFER, prevFBO);
    // Restore viewport (MapLibre will reset it before render() but be safe)
    gl.viewport(0, 0, w, h);
  }

  /**
   * render: composites the final result onto MapLibre's current framebuffer.
   * With smoothing: reads FBO2, applies vertical blur → screen.
   * Without smoothing: draws radar directly → screen (no FBO overhead).
   */
  render(
    _glUnion: WebGLRenderingContext | WebGL2RenderingContext,
    args: CustomRenderMethodInput,
  ): void {
    const gl = this._gl;
    if (!gl || !this._rProg || !this._bProg) return;

    const useFBO = this._smooth >= 0.01 && this._fbo2 != null;

    // Upload frame if we skipped prerender (no-smoothing path)
    if (!useFBO) {
      if (this._pending !== this._uploaded) {
        if (this._pending) this._uploadFrame(gl, this._pending);
        else               this._clearDataTex(gl);
        this._uploaded = this._pending;
      }
      if (!this._uploaded || !this._azTex || !this._rngTex || !this._gateTex) return;
    } else {
      if (!this._fbo2?.tex) return;
    }

    const w = (gl.canvas as HTMLCanvasElement).width;
    const h = (gl.canvas as HTMLCanvasElement).height;

    gl.enable(gl.BLEND);
    // Premultiplied alpha blend: matches the premultiplied output from both shaders
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);

    if (useFBO) {
      // ── Pass 3: vertical blur FBO2 → MapLibre's framebuffer ─────────────
      const sigma = this._smooth * 5.0;
      gl.viewport(0, 0, w, h);
      this._drawBlur(gl, this._fbo2!.tex, 0.0, 1.0 / h, sigma);
    } else {
      // ── Direct render (no smoothing) ─────────────────────────────────────
      const mainMatrix = (args as unknown as { defaultProjectionData: { mainMatrix: Float32Array } })
        .defaultProjectionData.mainMatrix;
      gl.viewport(0, 0, w, h);
      this._drawRadar(gl, mainMatrix);
    }
  }

  onRemove(_map: MaplibreMap, gl: WebGL2RenderingContext): void {
    this._clearDataTex(gl);
    if (this._lut)     gl.deleteTexture(this._lut);
    if (this._rVAO)    gl.deleteVertexArray(this._rVAO);
    if (this._bVAO)    gl.deleteVertexArray(this._bVAO);
    if (this._rPosBuf) gl.deleteBuffer(this._rPosBuf);
    if (this._bPosBuf) gl.deleteBuffer(this._bPosBuf);
    if (this._rProg)   gl.deleteProgram(this._rProg);
    if (this._bProg)   gl.deleteProgram(this._bProg);
    this._destroyFBOs(gl);
    this._gl = null;
  }

  // ── Private rendering helpers ─────────────────────────────────────────────

  private _drawRadar(gl: WebGL2RenderingContext, matrix: Float32Array): void {
    const f = this._uploaded!;
    gl.useProgram(this._rProg);
    gl.uniformMatrix4fv(this._rU.matrix!, false, matrix);
    gl.uniform1f(this._rU.radarLat!,      (f.bounds.north + f.bounds.south) / 2);
    gl.uniform1f(this._rU.radarLon!,      (f.bounds.east  + f.bounds.west)  / 2);
    gl.uniform1f(this._rU.maxRange!,      f.max_range_m);
    gl.uniform1f(this._rU.opacity!,       this._opacity);
    gl.uniform1i(this._rU.nRays!,         f.n_rays);
    gl.uniform1i(this._rU.nGates!,        f.n_gates);
    gl.uniform1f(this._rU.gateThreshold!, this._gateThreshold);
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, this._lut);     gl.uniform1i(this._rU.lut!,  0);
    gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, this._azTex);   gl.uniform1i(this._rU.az!,   1);
    gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_2D, this._rngTex);  gl.uniform1i(this._rU.rng!,  2);
    gl.activeTexture(gl.TEXTURE3); gl.bindTexture(gl.TEXTURE_2D, this._gateTex); gl.uniform1i(this._rU.gate!, 3);
    gl.bindVertexArray(this._rVAO);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
    gl.bindVertexArray(null);
  }

  private _drawBlur(
    gl: WebGL2RenderingContext,
    inputTex: WebGLTexture,
    dx: number, dy: number,
    sigma: number,
  ): void {
    gl.useProgram(this._bProg);
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, inputTex);
    gl.uniform1i(this._bU.tex!,   0);
    gl.uniform2f(this._bU.dir!,   dx, dy);
    gl.uniform1f(this._bU.sigma!, sigma);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.bindVertexArray(this._bVAO);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
    gl.bindVertexArray(null);
  }

  // ── FBO management ────────────────────────────────────────────────────────

  private _ensureFBOs(gl: WebGL2RenderingContext, w: number, h: number): void {
    if (this._fboW === w && this._fboH === h) return;
    this._destroyFBOs(gl);
    this._fbo1 = createFBO(gl, w, h);
    this._fbo2 = createFBO(gl, w, h);
    this._fboW = w;
    this._fboH = h;
  }

  private _destroyFBOs(gl: WebGL2RenderingContext): void {
    if (this._fbo1) { gl.deleteFramebuffer(this._fbo1.fbo); gl.deleteTexture(this._fbo1.tex); this._fbo1 = null; }
    if (this._fbo2) { gl.deleteFramebuffer(this._fbo2.fbo); gl.deleteTexture(this._fbo2.tex); this._fbo2 = null; }
    this._fboW = this._fboH = 0;
  }

  // ── Data texture upload ───────────────────────────────────────────────────

  private _clearDataTex(gl: WebGL2RenderingContext): void {
    if (this._azTex)   { gl.deleteTexture(this._azTex);   this._azTex   = null; }
    if (this._rngTex)  { gl.deleteTexture(this._rngTex);  this._rngTex  = null; }
    if (this._gateTex) { gl.deleteTexture(this._gateTex); this._gateTex = null; }
    this._texRays = this._texGates = 0;
  }

  private _uploadFrame(gl: WebGL2RenderingContext, frame: RadarBinaryFrame): void {
    if (this._lutProd !== frame.product) {
      gl.bindTexture(gl.TEXTURE_2D, this._lut);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, 256, 1, 0,
        gl.RGBA, gl.UNSIGNED_BYTE, getLUTForProduct(frame.product));
      this._lutProd = frame.product;
    }

    // Reuse the data textures across frames — only (re)allocate when the polar
    // grid dimensions change (product switch, super-res 720↔360). Looping steps
    // frames several times/sec for hours; recreating textures each time thrashed
    // the driver allocator (a GPU-TDR aggravator). texSubImage2D updates in place.
    const realloc =
      !this._azTex || !this._rngTex || !this._gateTex ||
      this._texRays !== frame.n_rays || this._texGates !== frame.n_gates;

    if (realloc) {
      this._clearDataTex(gl);
      this._azTex = makeTex(gl);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, frame.n_rays, 1, 0, gl.RED, gl.FLOAT, frame.azimuths);
      this._rngTex = makeTex(gl);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, frame.n_gates, 1, 0, gl.RED, gl.FLOAT, frame.ranges_m);
      this._gateTex = makeTex(gl);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8UI, frame.n_gates, frame.n_rays, 0,
        gl.RED_INTEGER, gl.UNSIGNED_BYTE, frame.gate_values);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
      this._texRays = frame.n_rays;
      this._texGates = frame.n_gates;
    } else {
      gl.bindTexture(gl.TEXTURE_2D, this._azTex);
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, frame.n_rays, 1, gl.RED, gl.FLOAT, frame.azimuths);
      gl.bindTexture(gl.TEXTURE_2D, this._rngTex);
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, frame.n_gates, 1, gl.RED, gl.FLOAT, frame.ranges_m);
      gl.bindTexture(gl.TEXTURE_2D, this._gateTex);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, frame.n_gates, frame.n_rays,
        gl.RED_INTEGER, gl.UNSIGNED_BYTE, frame.gate_values);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
    }

    // Update radar quad to match new frame bounds
    const b = frame.bounds;
    const nw = MercatorCoordinate.fromLngLat({ lng: b.west, lat: b.north });
    const sw = MercatorCoordinate.fromLngLat({ lng: b.west, lat: b.south });
    const ne = MercatorCoordinate.fromLngLat({ lng: b.east, lat: b.north });
    const se = MercatorCoordinate.fromLngLat({ lng: b.east, lat: b.south });
    gl.bindBuffer(gl.ARRAY_BUFFER, this._rPosBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
      nw.x, nw.y,   sw.x, sw.y,   ne.x, ne.y,
      sw.x, sw.y,   se.x, se.y,   ne.x, ne.y,
    ]), gl.DYNAMIC_DRAW);
    gl.bindBuffer(gl.ARRAY_BUFFER, null);
  }
}
