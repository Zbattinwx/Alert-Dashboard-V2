/**
 * NWS radar product color lookup tables (256-entry RGBA Uint8Arrays).
 *
 * Entry 0 is always fully transparent — it is the no-data sentinel.
 * Entries 1–255 map the normalized gate uint8 value to display color.
 *
 * The stops mirror the Python colormaps in nexrad_service.py so that the
 * WebGL display matches the legacy Pillow renders exactly.
 */

type Stop = [number, number, number, number, number]; // [index, R, G, B, A]

function interpolateLUT(stops: Stop[]): Uint8Array {
  const lut = new Uint8Array(256 * 4);
  // Entry 0: transparent (no-data sentinel)
  lut[0] = 0; lut[1] = 0; lut[2] = 0; lut[3] = 0;

  for (let si = 0; si < stops.length - 1; si++) {
    const [i0, r0, g0, b0, a0] = stops[si];
    const [i1, r1, g1, b1, a1] = stops[si + 1];
    const span = i1 - i0;
    for (let idx = Math.max(1, i0); idx <= Math.min(255, i1); idx++) {
      const t = span > 0 ? (idx - i0) / span : 0;
      const base = idx * 4;
      lut[base + 0] = Math.round(r0 + t * (r1 - r0));
      lut[base + 1] = Math.round(g0 + t * (g1 - g0));
      lut[base + 2] = Math.round(b0 + t * (b1 - b0));
      lut[base + 3] = Math.round(a0 + t * (a1 - a0));
    }
  }
  return lut;
}

/**
 * RadarScope-style reflectivity colormap (vmin=-20, vmax=80 dBZ).
 * Solid color bands with gradient transitions — mirrors _build_reflectivity_cmap.
 *
 * Index derivation: idx = round((dBZ - (-20)) / (80 - (-20)) * 255)
 *   -20 dBZ → 0   (transparent sentinel)
 *   -15 dBZ → 12
 *     5 dBZ → 62
 *  17.5 dBZ → 94
 *  22.5 dBZ → 107
 *  32.5 dBZ → 132
 *  37.5 dBZ → 145
 *  42.5 dBZ → 157
 *    50 dBZ → 177
 *    60 dBZ → 202
 *    70 dBZ → 229
 *    75 dBZ → 242
 *    80 dBZ → 255
 */
export function buildReflectivityLUT(): Uint8Array {
  // Exact match of the GRLevel2/RadarScope BR color table.
  // vmin = -20 dBZ,  vmax = 80 dBZ
  // Index formula: idx = round((dBZ + 20) / 100 × 255)
  //
  //  dBZ     idx   color (R G B)          notes
  // ─────────────────────────────────────────────────────────────
  // -20       0    transparent            no-data sentinel
  // -15      13    transparent            color4: -15 0 0 0 0
  //   5      64    29  37  60             dark navy start (solid)
  //  17.5    96    89 155 171             steel blue start (solid)
  //  22.5   108    33 186  72             green start (solid)
  //  32.5   134     5 101   1             dark green start (solid)
  //  37.5   147   251 252   0 →199 176 0  yellow gradient
  //  42.5   159   253 149   2 →172  92 2  orange gradient
  //  50     179   253  38   0 →135  43 22 red gradient
  //  60     204   193 148 179 →200  23 119 magenta gradient
  //  70     230   165   2 215 → 64   0 146 purple gradient
  //  75     242   135 255 253 → 54 120 142 cyan gradient
  //  80     255   173  99  64             brownish (vmax cap)
  // (85/95 dBZ stops from original table are beyond vmax — not rendered)

  const stops: Stop[] = [
    [0,   0,   0,   0,   0  ],  // no-data sentinel
    [13,  0,   0,   0,   0  ],  // -15 dBZ: below threshold (transparent)
    [63,  0,   0,   0,   0  ],  // hard cutoff — one index before 5 dBZ

    // 5–17.5 dBZ: dark navy, solid band
    [64,  29,  37,  60,  255],
    [95,  29,  37,  60,  255],

    // 17.5–22.5 dBZ: steel blue, solid band
    [96,  89,  155, 171, 255],
    [107, 89,  155, 171, 255],

    // 22.5–32.5 dBZ: green, solid band
    [108, 33,  186, 72,  255],
    [133, 33,  186, 72,  255],

    // 32.5–37.5 dBZ: dark green, solid band
    [134, 5,   101, 1,   255],
    [146, 5,   101, 1,   255],

    // 37.5–42.5 dBZ: yellow gradient  (251 252 0) → (199 176 0)
    [147, 251, 252, 0,   255],
    [158, 199, 176, 0,   255],

    // 42.5–50 dBZ: orange gradient  (253 149 2) → (172 92 2)
    [159, 253, 149, 2,   255],
    [178, 172, 92,  2,   255],

    // 50–60 dBZ: red gradient  (253 38 0) → (135 43 22)
    [179, 253, 38,  0,   255],
    [203, 135, 43,  22,  255],

    // 60–70 dBZ: magenta gradient  (193 148 179) → (200 23 119)
    [204, 193, 148, 179, 255],
    [229, 200, 23,  119, 255],

    // 70–75 dBZ: purple gradient  (165 2 215) → (64 0 146)
    [230, 165, 2,   215, 255],
    [241, 64,  0,   146, 255],

    // 75–80 dBZ: cyan gradient  (135 255 253) → (54 120 142)
    [242, 135, 255, 253, 255],
    [254, 54,  120, 142, 255],

    // 80 dBZ: brownish (vmax cap; 85/95 dBZ beyond range)
    [255, 173, 99,  64,  255],
  ];
  return interpolateLUT(stops);
}

/**
 * GRLevel2/RadarScope-style BV (Base Velocity) color table.
 * Ported directly from the user-supplied color table (scale 2.237, step 10 mph).
 *
 * Convention: negative = inbound (toward radar) = blues/greens
 *             positive = outbound (away from radar) = reds/oranges/yellows
 *
 * vmin = -35 m/s (≈ -78.3 mph),  vmax = +35 m/s (≈ +78.3 mph)
 * Index formula: idx = round((ms + 35) / 70 × 255)
 *   mph → m/s = mph / 2.237
 *
 *  mph   m/s    idx   notes
 * ────────────────────────────────────────────────────────
 * -78.3  -35     1    data minimum (idx 0 = transparent)
 *  -58  -25.9   33
 *  -50  -22.3   46
 *  -40  -17.9   62
 *  -10   -4.5  111
 * ≈0      0    127-128 gray transition
 *  +10   +4.5  144
 *  +40  +17.9  193
 *  +50  +22.3  209
 *  +58  +25.9  222
 *  +70  +31.3  242
 * +78.3  +35   255    data maximum
 */
export function buildVelocityLUT(): Uint8Array {
  const stops: Stop[] = [
    // no-data sentinel
    [0,   0,   0,   0,   0  ],
    // ≈ -78 mph: extrapolated between -120 mph (blue) and -58 mph (cyan)
    [1,   48,  163, 245, 255],
    // -58 mph: cyan
    [33,  71,  240, 240, 255],
    // -50 mph: light green
    [46,  82,  247, 89,  255],
    // -40 mph: bright green
    [62,  0,   255, 0,   255],
    // -10 mph: dark green
    [111, 16,  96,  16,  255],
    // ≈ -0.01 mph: gray-green (neutral inbound)
    [127, 112, 128, 112, 255],
    // 0 mph: gray (calm)
    [128, 144, 128, 144, 255],
    // +10 mph: dark maroon
    [144, 112, 0,   0,   255],
    // +40 mph: bright red
    [193, 255, 0,   0,   255],
    // +50 mph: red-orange
    [209, 255, 55,  26,  255],
    // +58 mph: orange
    [222, 254, 154, 39,  255],
    // +70 mph: yellow
    [242, 255, 255, 0,   255],
    // ≈ +78 mph: extrapolated between +70 mph (yellow) and +120 mph (brownish)
    [255, 240, 229, 11,  255],
  ];
  return interpolateLUT(stops);
}

/**
 * Correlation coefficient colormap (vmin=0.2, vmax=1.05).
 * Low CC (non-meteorological / debris) → purple/warm;
 * high CC (rain) → green/white.
 *
 * Index derivation: idx = round((cc - 0.2) / 0.85 * 255)
 *   0.20 → 0
 *   0.80 → 180
 *   0.90 → 210
 *   0.95 → 225
 *   1.00 → 240
 *   1.05 → 255
 */
export function buildCorrCoeffLUT(): Uint8Array {
  const stops: Stop[] = [
    [0,   0,   0,   0,   0],    // transparent below 0.2
    [30,  120, 0,   200, 229],  // deep purple (very low CC)
    [90,  200, 50,  50,  229],  // reddish (low CC)
    [150, 200, 200, 0,   229],  // yellow-green (moderate CC)
    [180, 0,   180, 0,   229],  // green (good CC ~0.80)
    [210, 0,   220, 180, 242],  // teal (high CC ~0.90)
    [225, 0,   240, 240, 242],  // cyan (very high CC ~0.95)
    [240, 180, 240, 255, 255],  // light blue (~1.00)
    [255, 255, 255, 255, 255],  // white (perfect CC)
  ];
  return interpolateLUT(stops);
}

/**
 * Get the LUT builder for a given radar product string.
 */
export function getLUTForProduct(product: string): Uint8Array {
  switch (product) {
    case 'velocity':              return buildVelocityLUT();
    case 'cross_correlation_ratio': return buildCorrCoeffLUT();
    default:                      return buildReflectivityLUT();
  }
}
